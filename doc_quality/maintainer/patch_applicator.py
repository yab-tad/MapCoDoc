"""
Apply patches to a deep copy of ``api_reference`` using JSONPath addresses.

The applicator is intentionally minimal: it doesn't decide *what* to patch,
only *how* to apply a list of pre-computed patches. The patch list is
produced by ``db_patcher`` / ``ast_patcher`` / (future) ``llm_patcher``.

JSONPath expressions are parsed via the third-party ``jsonpath-ng`` package,
which supports filter expressions like ``$.parameters[?name=='reduction']``.
That syntax is essential for our use-case because the structured doc
addresses parameters by name, not index.

If ``jsonpath-ng`` is not available the applicator falls back to a small
internal evaluator that handles the subset of paths we actually emit.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict, List

from doc_quality.models import MaintenancePatch


logger = logging.getLogger(__name__)


# Try the third-party JSONPath library first; fall back to a local
# evaluator if it isn't installed. The fallback understands the subset
# of expressions actually emitted by our IssueType templates.
try:
    from jsonpath_ng.ext import parse as jsonpath_parse  # type: ignore
    _HAS_JSONPATH_NG = True
except ImportError:  # pragma: no cover - environment dependent
    jsonpath_parse = None
    _HAS_JSONPATH_NG = False


# Pattern for the local fallback evaluator. Recognizes the four shapes:
#   $.field
#   $.field.subfield
#   $.array[?key=='value']
#   $.array[index]
#   $.array[?key=='value'].field
# Anything else is rejected.
_PATH_TOKEN_RE = re.compile(
    r"\.(?P<field>[A-Za-z_][A-Za-z0-9_]*)"
    r"|\[(?P<index>\d+)\]"
    r"|\[\?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*==\s*'(?P<val>[^']*)'\]"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class PatchApplicator:
    """Apply a list of MaintenancePatch operations to an api_reference."""

    def apply_patches(
        self,
        api_reference: Dict,
        patches: List[MaintenancePatch]
    ) -> Dict:
        """
        Return a new dict containing the result of applying ``patches``.

        The input dict is *not* mutated; a deep copy is made up front so
        the caller still has the original to compare against. Patches
        whose ``new_value`` is None are skipped (the maintainer uses None
        to mean "no auto-fix available").
        """
        result = copy.deepcopy(api_reference)
        for patch in patches:
            if patch.new_value is None:
                # No-op: the issue exists but no automatic value is available.
                # The candidate will still report it for manual review.
                continue
            try:
                self._apply_one(result, patch)
            except Exception as exc:
                logger.warning(
                    "Failed to apply patch at %s: %s. Patch skipped.",
                    patch.json_path, exc,
                )
        return result

    def _apply_one(self, doc: Dict, patch: MaintenancePatch) -> None:
        """Apply a single patch in-place to ``doc``."""
        if _HAS_JSONPATH_NG:
            self._apply_with_jsonpath_ng(doc, patch)
        else:
            self._apply_with_fallback(doc, patch)

    # ------------------------------------------------------------------
    # jsonpath-ng path
    # ------------------------------------------------------------------

    def _apply_with_jsonpath_ng(self, doc: Dict, patch: MaintenancePatch) -> None:
        """Use the third-party jsonpath_ng library to apply the patch."""
        # Special case: appending a new parameter dict to ``$.parameters``.
        # jsonpath_ng's ``update`` on an array path *replaces* the array
        # with the new value, which is wrong here - we want to append.
        # Detect this case before delegating.
        if (patch.json_path == "$.parameters" and isinstance(patch.new_value, dict)):
            doc.setdefault("parameters", []).append(patch.new_value)
            return

        expr = jsonpath_parse(patch.json_path)
        matches = expr.find(doc)
        if not matches:
            # If the path resolves nothing it could mean (a) the patch was
            # written for a field that doesn't exist (insertion case) or
            # (b) the path is malformed. Try insertion via the parent
            # path before giving up.
            self._insert_or_warn(doc, patch)
            return
        for match in matches:
            expr.update(doc, patch.new_value)

    def _insert_or_warn(self, doc: Dict, patch: MaintenancePatch) -> None:
        """
        Attempt insertion when a JSONPath has no existing match.

        We support insertion only for two specific shapes that arise in
        practice:

        1. ``$.parameters`` - append a new parameter dict.
        2. ``$.parameters[?name=='X'].<field>`` - the parameter exists
           but the field is absent; jsonpath_ng's ``update`` does not
           create missing keys, so we resolve the parameter and set the
           field manually.
        """
        path = patch.json_path
        # Case 1: append-to-array
        if path == "$.parameters" and isinstance(patch.new_value, dict):
            doc.setdefault("parameters", []).append(patch.new_value)
            return
        # Case 2: field-on-existing-parameter
        m = re.match(
            r"\$\.parameters\[\?name=='([^']+)'\]\.(\w+)$",
            path
        )
        if m:
            target_name, field_name = m.group(1), m.group(2)
            for p in doc.get("parameters", []):
                if p.get("name") == target_name:
                    p[field_name] = patch.new_value
                    return
        logger.warning("Patch path %s found no targets and no insertion fallback applies.", path)

    # ------------------------------------------------------------------
    # Local fallback evaluator
    # ------------------------------------------------------------------

    def _apply_with_fallback(self, doc: Dict, patch: MaintenancePatch) -> None:
        """
        Local evaluator for paths emitted by our IssueType templates.

        Handles the same shapes as ``_insert_or_warn`` plus simple dotted
        field access.
        """
        path = patch.json_path
        if not path.startswith("$"):
            raise ValueError(f"Non-rooted JSONPath: {path!r}")
        tokens = list(_PATH_TOKEN_RE.finditer(path[1:]))
        if not tokens:
            raise ValueError(f"Unparseable JSONPath: {path!r}")

        # Walk to the parent of the final segment, creating dict intermediates as needed.
        current: Any = doc
        for tok in tokens[:-1]:
            current = self._descend(current, tok)

        # Apply final segment.
        last = tokens[-1]
        self._set_final(current, last, patch.new_value)


    def _descend(self, current: Any, tok: re.Match) -> Any:
        """Step one path token deeper, materializing nodes as needed."""
        if tok.group("field"):
            field = tok.group("field")
            if field not in current:
                current[field] = {}
            return current[field]
        
        if tok.group("index") is not None:
            idx = int(tok.group("index"))
            return current[idx]
        
        if tok.group("key"):
            key, val = tok.group("key"), tok.group("val")
            for entry in current:
                if entry.get(key) == val:
                    return entry
            raise KeyError(f"No element with {key}=={val!r}")
        raise ValueError("Unrecognized path token")


    def _set_final(self, parent: Any, tok: re.Match, new_value: Any) -> None:
        """Write ``new_value`` at the final path token."""
        if tok.group("field"):
            parent[tok.group("field")] = new_value
            return
        
        if tok.group("index") is not None:
            parent[int(tok.group("index"))] = new_value
            return
        
        if tok.group("key"):
            # Replace or insert by key match.
            key, val = tok.group("key"), tok.group("val")
            for i, entry in enumerate(parent):
                if entry.get(key) == val:
                    parent[i] = new_value
                    return
            parent.append(new_value)
            return
        raise ValueError("Unrecognized final path token")
