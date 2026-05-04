"""
AST-derived strategy patcher.

Builds patches whose corrective values are derived from the code-side
signatures stored in the DB. The most common application is repairing a
drifted documented signature - copy the ``full`` signature variant
verbatim, optionally prefixed with ``async`` if the code is async.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from doc_quality.code_truth_resolver import CodeTruth
from doc_quality.issue_types import IssueType
from doc_quality.models import Issue, MaintainerStrategy, MaintenancePatch


logger = logging.getLogger(__name__)


_Builder = Callable[[Issue, Optional[CodeTruth]], Optional[MaintenancePatch]]


class AstPatcher:
    """Construct patches whose value is derived from code-side signatures."""

    def __init__(self) -> None:
        self._builders: Dict[IssueType, _Builder] = {
            IssueType.COMP_SIGNATURE_MISSING: self._patch_signature_missing,
            IssueType.ACC_SIGNATURE_DRIFT: self._patch_signature_drift,
            IssueType.ACC_ASYNC_MARKER_MISSING: self._patch_async_marker,
            IssueType.ACC_CLASS_METHOD_SIG_DRIFT: self._patch_class_method_sig,
        }

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def build_patches(
        self,
        issues: List[Issue],
        code_truth: Optional[CodeTruth] = None
    ) -> List[MaintenancePatch]:
        """Return one MaintenancePatch per applicable AST_DERIVED issue."""
        patches: List[MaintenancePatch] = []
        for issue in issues:
            if issue.maintainer_strategy != MaintainerStrategy.AST_DERIVED:
                continue
            builder = self._builders.get(issue.issue_type)
            if builder is None:
                logger.debug(
                    "No AST_DERIVED builder registered for %s",
                    issue.issue_type.value.code,
                )
                continue
            patch = builder(issue, code_truth)
            if patch is not None:
                patches.append(patch)
        return patches

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    @staticmethod
    def _patch_signature_missing(issue: Issue, code_truth: Optional[CodeTruth]) -> Optional[MaintenancePatch]:
        """Fill an empty ``module_member_signature`` from code."""
        if not code_truth or not code_truth.signatures:
            return None
        sig = AstPatcher._best_signature(code_truth)
        if not sig:
            return None
        return MaintenancePatch(
            issue=issue,
            json_path="$.module_member_signature",
            old_value=issue.doc_value,
            new_value=sig,
            strategy=MaintainerStrategy.AST_DERIVED,
            rationale="Used 'full' code signature variant."
        )

    @staticmethod
    def _patch_signature_drift(issue: Issue, code_truth: Optional[CodeTruth]) -> Optional[MaintenancePatch]:
        """Replace a drifted documented signature with the code one."""
        if not code_truth or not code_truth.signatures:
            return None
        sig = AstPatcher._best_signature(code_truth)
        if not sig:
            return None
        return MaintenancePatch(
            issue=issue,
            json_path="$.module_member_signature",
            old_value=issue.doc_value,
            new_value=sig,
            strategy=MaintainerStrategy.AST_DERIVED,
            rationale=(
                f"Documented signature drifted (≈{issue.metadata.get('drift', 0):.2f}); replaced with code signature."
            )
        )

    @staticmethod
    def _patch_async_marker(issue: Issue, code_truth: Optional[CodeTruth]) -> Optional[MaintenancePatch]:
        """Prefix the existing signature with ``async``."""
        if not code_truth:
            return None
        old = issue.doc_value or ""
        if not old:
            # No existing signature to prefix; defer.
            return None
        if old.lstrip().lower().startswith("async"):
            # Already correct; nothing to do.
            return None
        new = f"async {old.lstrip()}"
        return MaintenancePatch(
            issue=issue,
            json_path="$.module_member_signature",
            old_value=old,
            new_value=new,
            strategy=MaintainerStrategy.AST_DERIVED,
            rationale="Prepended 'async' to documented signature."
        )

    @staticmethod
    def _patch_class_method_sig(issue: Issue, code_truth: Optional[CodeTruth]) -> Optional[MaintenancePatch]:
        """Replace a drifted class-method signature inside ``methods[]``.

        Note: this requires the ``code_value`` field to carry the
        replacement signature; the accuracy evaluator populates it.
        """
        if not issue.code_value:
            return None
        return MaintenancePatch(
            issue=issue,
            json_path=issue.json_path,
            old_value=issue.doc_value,
            new_value=issue.code_value,
            strategy=MaintainerStrategy.AST_DERIVED,
            rationale="Replaced class-method signature with code signature."
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _best_signature(code_truth: CodeTruth) -> Optional[str]:
        """Pick the 'best' signature variant for human-readable display."""
        # ``full`` includes types and defaults, which is what users expect in API documentation. We fall back to ``default`` (default values only) and finally to any variant we have
        for variant in ("full", "default", "no_types"):
            sig = code_truth.signatures.get(variant)
            if sig:
                # If the function is async, prepend.
                if code_truth.is_async and not sig.lower().startswith("async"):
                    return f"async {sig}"
                return sig
        # Last resort: the first available variant.
        if code_truth.signatures:
            sig = next(iter(code_truth.signatures.values()))
            if code_truth.is_async and not sig.lower().startswith("async"):
                return f"async {sig}"
            return sig
        return None
