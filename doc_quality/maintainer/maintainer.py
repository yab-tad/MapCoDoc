"""
DocQualityMaintainer - orchestrates targeted documentation maintenance.

Given an ``EvaluationReport`` (or a freshly-evaluated member), the
maintainer:

1. Filters issues by configured minimum severity.
2. Dispatches each remaining issue to the appropriate strategy patcher
   (DB_QUERY, AST_DERIVED, LLM, MANUAL).
3. Collects the produced ``MaintenancePatch`` list.
4. Applies the patches to a deep copy of ``api_reference``.
5. Wraps the result in a ``MaintenanceCandidate`` and persists it to the
   artifact store.

The maintainer never writes to the database itself - that step is gated by manual approval (see ``maintainer.approval``).
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from doc_quality.code_truth_resolver import CodeTruth, CodeTruthResolver
from doc_quality.config import EvaluatorConfig
from doc_quality.maintainer.ast_patcher import AstPatcher
from doc_quality.maintainer.db_patcher import DbPatcher
from doc_quality.maintainer.llm_patcher import LlmPatcher
from doc_quality.maintainer.patch_applicator import PatchApplicator
from doc_quality.models import (
    EvaluationReport,
    Issue,
    MaintainerStrategy,
    MaintenanceCandidate,
    MaintenancePatch,
    Severity
)

if TYPE_CHECKING:
    from mapcodoc_db.query import QueryManager


logger = logging.getLogger(__name__)


class DocQualityMaintainer:
    """Targeted documentation maintainer driven by EvaluationReports."""

    def __init__(
        self,
        query_manager: "QueryManager",
        config: Optional[EvaluatorConfig] = None,
        artifact_store=None,
        llm_patcher: Optional[LlmPatcher] = None
    ) -> None:
        self.qm = query_manager
        self.cfg = config or EvaluatorConfig()
        self.artifact_store = artifact_store

        # Patcher strategies. Defaults are deterministic; LLM is a stub.
        self._db = DbPatcher()
        self._ast = AstPatcher()
        self._llm = llm_patcher or LlmPatcher()
        self._applicator = PatchApplicator()

        # Used to fetch CodeTruth on demand for AST-derived patches.
        self._truth_resolver = CodeTruthResolver(query_manager)

    # ------------------------------------------------------------------
    # Single-member maintenance
    # ------------------------------------------------------------------

    def maintain_one(
        self,
        report: EvaluationReport,
        original_api_reference: dict
    ) -> MaintenanceCandidate:
        """
        Produce a maintenance candidate for a single member.

        Args:
            report: The evaluation report whose issues drive the patches.
            original_api_reference: The api_reference JSON snapshot.

        Returns:
            A MaintenanceCandidate. Even when no patches are applicable
            (e.g. all issues are MANUAL) a candidate is returned so the
            artifact store records the maintainer's pass for the member.
        """
        # 1. Collect all issues across dimensions, filtered by min severity.
        all_issues: List[Issue] = []
        for ds in report.dimensions.values():
            for issue in ds.issues:
                if self._meets_severity(issue.severity):
                    all_issues.append(issue)

        if not all_issues:
            # Nothing to do - return a candidate identical to the original.
            return self._make_candidate(
                report=report,
                original=original_api_reference,
                candidate=copy.deepcopy(original_api_reference),
                patches=[]
            )

        # 2. Resolve code truth once for AST-derived patches that need it.
        code_truth = self._resolve_code_truth_for_report(report)

        # 3. Build patches from each enabled strategy.
        patches: List[MaintenancePatch] = []
        if MaintainerStrategy.DB_QUERY in self.cfg.enabled_strategies:
            patches.extend(self._db.build_patches(all_issues))
        if MaintainerStrategy.AST_DERIVED in self.cfg.enabled_strategies:
            patches.extend(self._ast.build_patches(all_issues, code_truth))
        if MaintainerStrategy.LLM in self.cfg.enabled_strategies:
            patches.extend(self._llm.build_patches(all_issues, code_truth))

        # 4. Apply patches to a copy.
        candidate_ref = self._applicator.apply_patches(original_api_reference, patches)

        # 5. Wrap and persist.
        candidate = self._make_candidate(
            report=report,
            original=original_api_reference,
            candidate=candidate_ref,
            patches=patches
        )
        
        if self.artifact_store is not None:
            self.artifact_store.save_candidate(candidate)
        return candidate

    # ------------------------------------------------------------------
    # Batch maintenance
    # ------------------------------------------------------------------

    def maintain_batch(self, pairs: List[tuple]) -> List[MaintenanceCandidate]:
        """
        Run ``maintain_one`` over a list of ``(report, api_reference)`` pairs.

        Returns one MaintenanceCandidate per pair.
        """
        candidates: List[MaintenanceCandidate] = []
        for report, api_ref in pairs:
            candidates.append(self.maintain_one(report, api_ref))
        return candidates

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _meets_severity(self, severity: Severity) -> bool:
        """Return True if ``severity`` meets the configured minimum."""
        # Convert both to numeric ranks for comparison.
        ranks = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}
        return ranks[severity] >= ranks[self.cfg.min_severity_for_maintenance]

    def _resolve_code_truth_for_report(self, report: EvaluationReport) -> Optional[CodeTruth]:
        """
        Re-resolve code truth so AST patchers have access to signatures.

        We do this lazily, once per report, because most patches don't
        actually need it. The resolver handles the inherited vs direct
        dispatch and returns None for external inherited members.
        """
        if not report.code_truth_available:
            return None
        # Use the API name as the lookup key; ``find_member_by_any_path``
        # handles direct, alias, and inherited paths uniformly.
        try:
            resolution = self.qm.find_member_by_any_path(report.member_api_name)
        except Exception as exc:
            logger.debug("find_member_by_any_path failed for %s: %s", report.member_api_name, exc)
            return None
        
        if not resolution:
            return None
        
        if resolution["type"] == "direct":
            return self._truth_resolver.resolve_direct(resolution["member"])
        
        # Inherited
        return self._truth_resolver.resolve_inherited(resolution["member"])


    def _make_candidate(
        self,
        *,
        report: EvaluationReport,
        original: dict,
        candidate: dict,
        patches: List[MaintenancePatch]
    ) -> MaintenanceCandidate:
        """Construct a MaintenanceCandidate value object."""
        return MaintenanceCandidate(
            member_fqn=report.member_fqn,
            member_api_name=report.member_api_name,
            original_api_reference=original,
            candidate_api_reference=candidate,
            patches=patches,
            timestamp=datetime.now(timezone.utc)
        )
