"""
DB-query strategy patcher.

Builds ``MaintenancePatch`` objects from issues whose maintainer strategy
is ``DB_QUERY`` - that is, issues where the corrective value can be read
directly from the issue's ``code_value`` field (which the evaluator
populated from the MapCoDoc database). The strategy is fully deterministic
and requires no LLM.

The patcher implements one builder per ``IssueType`` it knows how to
handle. Issue types whose strategy is ``DB_QUERY`` but for which no builder
is registered get logged at WARNING and returned without a patch (the
maintainer will downgrade them to MANUAL).
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from doc_quality.evaluator.maintainability import BUILTIN_TYPE_URLS
from doc_quality.issue_types import IssueType
from doc_quality.models import Issue, MaintainerStrategy, MaintenancePatch


logger = logging.getLogger(__name__)


# A builder maps a single Issue to a single MaintenancePatch (or None when
# no patch can be produced even in principle - e.g. missing code_value).
_Builder = Callable[[Issue], Optional[MaintenancePatch]]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class DbPatcher:
    """Construct deterministic patches from DB-query strategy issues."""

    def __init__(self) -> None:
        # Builder registry keyed by IssueType. New builders are added by
        # decorating a function with ``@self._register(IssueType.X)`` -
        # but simple class-level registration is clearer here, so we use
        # an explicit dict initialized in __init__.
        self._builders: Dict[IssueType, _Builder] = {
            IssueType.COMP_PARAM_TYPE_MISSING: self._patch_param_type_missing,
            IssueType.COMP_PARAM_NAME_MISSING: self._patch_param_name_missing,
            IssueType.COMP_RETURN_TYPE_MISSING: self._patch_return_type_missing,
            IssueType.ACC_PARAM_NAME_UNKNOWN: self._patch_param_name_unknown,
            IssueType.ACC_PARAM_MISSING_FROM_DOC: self._patch_param_missing_from_doc,
            IssueType.ACC_PARAM_TYPE_MISMATCH: self._patch_param_type_mismatch,
            IssueType.ACC_PARAM_DEFAULT_MISMATCH: self._patch_param_default_mismatch,
            IssueType.ACC_RETURN_TYPE_MISMATCH: self._patch_return_type_mismatch,
            IssueType.MAINT_BUILTIN_NOT_LINKED: self._patch_builtin_not_linked,
            IssueType.MAINT_TYPE_NOT_LINKED: self._patch_type_not_linked
        }

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def build_patches(self, issues: List[Issue]) -> List[MaintenancePatch]:
        """Return one MaintenancePatch per applicable DB_QUERY issue."""
        patches: List[MaintenancePatch] = []
        for issue in issues:
            if issue.maintainer_strategy != MaintainerStrategy.DB_QUERY:
                continue
            builder = self._builders.get(issue.issue_type)
            if builder is None:
                logger.debug(
                    "No DB_QUERY builder registered for issue type %s",
                    issue.issue_type.value.code,
                )
                continue
            patch = builder(issue)
            if patch is not None:
                patches.append(patch)
        return patches

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    @staticmethod
    def _patch_param_type_missing(issue: Issue) -> Optional[MaintenancePatch]:
        """Fill in a missing parameter type from code_value."""
        if not issue.code_value:
            # No type to copy; downgrade to manual.
            return None
        return MaintenancePatch(
            issue=issue,
            json_path=issue.json_path,
            old_value=issue.doc_value,
            new_value=issue.code_value,
            strategy=MaintainerStrategy.DB_QUERY,
            rationale=("Filled missing type from code annotation.")
        )

    @staticmethod
    def _patch_param_name_missing(issue: Issue) -> Optional[MaintenancePatch]:
        """Insert an entire parameter entry that is missing from the doc."""
        if not issue.code_value:
            return None
        # ``code_value`` is the full parameter dict from MemberDetails.
        # We construct the doc-side shape, leaving the description blank
        # for downstream LLM enrichment.
        cp = issue.code_value
        new_param = {
            "name": cp.get("name", "<unknown>"),
            "type": cp.get("type") or "N/A",
            "description": "N/A",
            "additional_information": "N/A"
        }
        # If code-side default is present, surface it in the description.
        if cp.get("default") is not None:
            new_param["description"] = f"Default: {cp['default']}"
        return MaintenancePatch(
            issue=issue,
            json_path=issue.json_path,
            old_value=None,
            new_value=new_param,
            strategy=MaintainerStrategy.DB_QUERY,
            rationale=(
                f"Inserted missing parameter '{new_param['name']}' from code; description left for LLM enrichment."
            )
        )

    @staticmethod
    def _patch_return_type_missing(issue: Issue) -> Optional[MaintenancePatch]:
        if not issue.code_value:
            return None
        return MaintenancePatch(
            issue=issue,
            json_path=issue.json_path,
            old_value=issue.doc_value,
            new_value=issue.code_value,
            strategy=MaintainerStrategy.DB_QUERY,
            rationale="Filled missing return type from code annotation."
        )

    @staticmethod
    def _patch_param_name_unknown(issue: Issue) -> Optional[MaintenancePatch]:
        """Remove a param entry whose name is not in the code signature.

        We can't *delete* via JSONPath update; instead we overwrite the
        entry with a sentinel that the patch applicator can recognize.
        For simplicity here we leave this one MANUAL by emitting None - a
        future enhancement could implement a true delete patch.
        """
        # Removal is genuinely tricky with JSONPath updates; rather than implement a half-working delete, return None so the maintainer surfaces the issue for manual review
        return None

    @staticmethod
    def _patch_param_missing_from_doc(issue: Issue) -> Optional[MaintenancePatch]:
        """Same shape as COMP_PARAM_NAME_MISSING."""
        return DbPatcher._patch_param_name_missing(issue)

    @staticmethod
    def _patch_param_type_mismatch(issue: Issue) -> Optional[MaintenancePatch]:
        if not issue.code_value:
            return None
        return MaintenancePatch(
            issue=issue,
            json_path=issue.json_path,
            old_value=issue.doc_value,
            new_value=issue.code_value,
            strategy=MaintainerStrategy.DB_QUERY,
            rationale=(f"Replaced documented type {issue.doc_value!r} with code annotation {issue.code_value!r}.")
        )

    @staticmethod
    def _patch_param_default_mismatch(issue: Issue) -> Optional[MaintenancePatch]:
        """
        Update the description with a corrected ``Default: X`` clause.

        We don't try to surgically rewrite the description; instead, we
        append a ``"Default: <code_value>."`` suffix which, on review,
        a maintainer can trim of duplicate clauses. This keeps the patch deterministic.
        """
        if not issue.code_value:
            return None
        param_target = issue.target
        original_desc = (issue.doc_value or "")
        # If the original description is itself the doc_value (as set by
        # the accuracy evaluator) it's only the *parsed* default - we
        # don't have the full description here. Surface the corrected
        # default and a marker so reviewers can see what changed.
        new_desc = (
            f"Default: {issue.code_value}.  (corrected; previously: {issue.doc_value!r})"
        )
        return MaintenancePatch(
            issue=issue,
            # The path was registered as the parameter description.
            json_path=issue.json_path,
            old_value=original_desc,
            new_value=new_desc,
            strategy=MaintainerStrategy.DB_QUERY,
            rationale=(f"Recorded corrected default value {issue.code_value!r}; reviewer should remove the previous mention.")
        )

    @staticmethod
    def _patch_return_type_mismatch(issue: Issue) -> Optional[MaintenancePatch]:
        if not issue.code_value:
            return None
        return MaintenancePatch(
            issue=issue,
            json_path=issue.json_path,
            old_value=issue.doc_value,
            new_value=issue.code_value,
            strategy=MaintainerStrategy.DB_QUERY,
            rationale=(f"Replaced documented return type {issue.doc_value!r} with code annotation {issue.code_value!r}.")
        )

    @staticmethod
    def _patch_builtin_not_linked(issue: Issue) -> Optional[MaintenancePatch]:
        """Wrap a builtin type mention with its docs.python.org URL."""
        builtin = (issue.metadata or {}).get("builtin")
        if not builtin or builtin not in BUILTIN_TYPE_URLS:
            return None
        
        url = BUILTIN_TYPE_URLS[builtin]
        
        # The current type field may be ``"bool"`` or ``"bool, optional"``.
        # Replace the first whole-word occurrence of the builtin with ``"builtin(URL)"`` to preserve any qualifiers that follow.
        original = issue.doc_value or ""
        if not original:
            return None
        
        # Build the replacement as ``builtin(URL)``. Use a non-regex substitution to avoid escaping issues.
        # Do a small regex here because word-boundary matching is wanted.
        import re as _re
        pattern = _re.compile(rf"\b{_re.escape(builtin)}\b")
        
        # Only replace the first match; if no match is found (unlikely given how this issue is detected), bail.
        new_value, n = pattern.subn(f"{builtin}({url})", original, count=1)
        if n == 0:
            return None
        
        return MaintenancePatch(
            issue=issue,
            json_path=issue.json_path,
            old_value=original,
            new_value=new_value,
            strategy=MaintainerStrategy.DB_QUERY,
            rationale=(f"Linked builtin '{builtin}' to its Python docs URL.")
        )

    @staticmethod
    def _patch_type_not_linked(issue: Issue) -> Optional[MaintenancePatch]:
        """Linking non-builtin types requires a doc base URL we don't have.

        For v1 this strategy returns None - the issue is reported but
        deferred to LLM/manual treatment until a per-library doc base URL
        configuration is wired in.
        """
        return None
