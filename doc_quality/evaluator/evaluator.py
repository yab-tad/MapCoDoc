"""
DocQualityEvaluator - the orchestrator that runs all dimension checks.

The evaluator is the user-facing entry point of this package: given a
configured ``QueryManager`` it can evaluate a single member, a batch of
members, or the entire structured-doc population for a library.
Each evaluation produces an ``EvaluationReport`` that is both returned to
the caller and (optionally) persisted to the artifact store.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Union

from doc_quality.class_member_lister import ClassMemberLister, ClassMembers
from doc_quality.code_truth_resolver import CodeTruth, CodeTruthResolver
from doc_quality.config import EvaluatorConfig
from doc_quality.doc_views import DocView, doc_view
from doc_quality.evaluator import accuracy, completeness, maintainability, readability
from doc_quality.models import (
    Dimension,
    DimensionScore,
    EvaluationReport,
    Issue
)

if TYPE_CHECKING:
    from mapcodoc_db.query import (
        InheritedMemberDetails,
        MemberDetails,
        QueryManager
    )

logger = logging.getLogger(__name__)


# Type alias for "a thing the evaluator can evaluate". Both kinds of
# member details can be passed in; the orchestrator dispatches on the
# value's class at runtime.
MemberLike = Union["MemberDetails", "InheritedMemberDetails"]


class DocQualityEvaluator:
    """
    Evaluate structured documentation across all four quality dimensions.

    The orchestrator is constructed once with a configured DB session and
    reused for many evaluations. The artifact store, when provided, is
    written to after each evaluation; passing ``None`` makes evaluations in-memory only.
    """

    def __init__(
        self,
        query_manager: "QueryManager",
        config: Optional[EvaluatorConfig] = None,
        artifact_store=None # ArtifactStore or None (typed loosely to avoid import cycles)
    ) -> None:
        self.qm = query_manager
        self.cfg = config or EvaluatorConfig()
        # Compose the helper objects up-front so each evaluate_one call avoids re-creating them
        self.code_truth_resolver = CodeTruthResolver(query_manager)
        self.class_member_lister = ClassMemberLister(query_manager)
        self.artifact_store = artifact_store

    # ------------------------------------------------------------------
    # Single-member evaluation
    # ------------------------------------------------------------------

    def evaluate_one(self, member: MemberLike) -> EvaluationReport:
        """
        Evaluate a single direct or inherited member.

        ``member`` may be a ``MemberDetails`` (from
        ``QueryManager.get_member_details``) or an ``InheritedMemberDetails``
        (from inherited-member queries). The orchestrator dispatches on thetype and produces an ``EvaluationReport``.
        """
        # Detect inherited vs direct by attribute presence rather than isinstance to avoid a runtime import of mapcodoc_db
        is_inherited = hasattr(member, "inherited_api_name")

        if is_inherited:
            return self._evaluate_inherited(member)
        return self._evaluate_direct(member)


    def _evaluate_direct(self, member: "MemberDetails") -> EvaluationReport:
        """Evaluate a member retrieved as a ``MemberDetails`` row."""
        
        # Fetch api_reference. ``MemberDetails`` does not carry it (the dataclass omits it for brevity), so go back to the DB
        api_ref = self._fetch_api_reference(member.fqn, member.api_name or member.fqn)
        if not api_ref:
            return self._make_skipped_report(
                member_id=member.id,
                fqn=member.fqn,
                api_name=member.api_name or member.fqn,
                member_type=member.type,
                is_inherited=False,
                reason="No structured api_reference found."
            )

        view = doc_view(api_ref, member.type)
        truth = self.code_truth_resolver.resolve_direct(member)
        class_members = (
            self.class_member_lister.list_members(member.fqn)
            if member.type == "class" else None
        )

        report = self._run_all_dimensions(
            member_id=member.id,
            fqn=member.fqn,
            api_name=member.api_name or member.fqn,
            member_type=member.type,
            view=view,
            truth=truth,
            class_members=class_members,
            is_inherited=False
        )

        # Persist the snapshot + report if an artifact store was provided.
        if self.artifact_store is not None:
            self.artifact_store.save_original(report.member_api_name, api_ref)
            self.artifact_store.save_evaluation(report)

        return report

    def _evaluate_inherited(self, inherited: "InheritedMemberDetails") -> EvaluationReport:
        """
        Evaluate an inherited member.

        Only internal inherited members (those with an ``original_member_id``)
        carry enough information for accuracy checks. External inherited members are reported skipped.
        """
        if inherited.is_external or not inherited.original_member_id:
            return self._make_skipped_report(
                member_id=inherited.id,
                fqn=inherited.inherited_api_name,
                api_name=inherited.inherited_api_name,
                member_type=inherited.member_type or "method",
                is_inherited=True,
                reason="External inherited member: no code-truth available."
            )

        # Locate the structured doc - may live on the inherited record itself or, more often for internal inheritance, on the original member
        api_ref = inherited.api_reference
        if not api_ref:
            api_ref = self._fetch_api_reference(
                None,  # no FQN; api_name is the right key
                inherited.inherited_api_name
            )
        if not api_ref:
            return self._make_skipped_report(
                member_id=inherited.id,
                fqn=inherited.inherited_api_name,
                api_name=inherited.inherited_api_name,
                member_type=inherited.member_type or "method",
                is_inherited=True,
                reason="Inherited member has no structured api_reference."
            )

        truth = self.code_truth_resolver.resolve_inherited(inherited)
        if truth is None:
            return self._make_skipped_report(
                member_id=inherited.id,
                fqn=inherited.inherited_api_name,
                api_name=inherited.inherited_api_name,
                member_type=inherited.member_type or "method",
                is_inherited=True,
                reason="Code-truth resolution returned None (likely external)."
            )

        view = doc_view(api_ref, truth.member_type)
        class_members = None  # Inherited members are not classes themselves.

        report = self._run_all_dimensions(
            member_id=inherited.id,
            fqn=inherited.inherited_api_name,
            api_name=inherited.inherited_api_name,
            member_type=truth.member_type,
            view=view,
            truth=truth,
            class_members=class_members,
            is_inherited=True
        )

        if self.artifact_store is not None:
            self.artifact_store.save_original(report.member_api_name, api_ref)
            self.artifact_store.save_evaluation(report)

        return report

    # ------------------------------------------------------------------
    # Dimension dispatch
    # ------------------------------------------------------------------

    def _run_all_dimensions(
        self,
        *,
        member_id: int,
        fqn: str,
        api_name: str,
        member_type: str,
        view: DocView,
        truth: Optional[CodeTruth],
        class_members: Optional[ClassMembers],
        is_inherited: bool
    ) -> EvaluationReport:
        """Invoke each dimension evaluator and assemble a report."""
        
        dimensions: Dict[Dimension, DimensionScore] = {}

        # Each evaluator is called with the same input contract; failures in one dimension should not stop the others. Catches broad exceptions, log, and substitute a 0-score DimensionScore so the report still contains all dimensions for downstream tooling
        dimensions[Dimension.COMPLETENESS] = self._safe_eval(
            "completeness",
            lambda: completeness.evaluate(view, truth, class_members, self.cfg)
        )
        dimensions[Dimension.ACCURACY] = self._safe_eval(
            "accuracy",
            lambda: accuracy.evaluate(view, truth, class_members, self.cfg)
        )
        dimensions[Dimension.READABILITY] = self._safe_eval(
            "readability",
            lambda: readability.evaluate(view, truth, class_members, self.cfg)
        )
        dimensions[Dimension.MAINTAINABILITY] = self._safe_eval(
            "maintainability",
            lambda: maintainability.evaluate(
                view, truth, class_members, self.cfg, query_manager=self.qm
            )
        )

        overall = self._weighted_overall(dimensions)

        return EvaluationReport(
            member_id=member_id,
            member_fqn=fqn,
            member_api_name=api_name,
            member_type=member_type,
            is_inherited=is_inherited,
            code_truth_available=truth is not None,
            overall_score=overall,
            dimensions=dimensions,
            evaluation_timestamp=datetime.now(timezone.utc)
        )

    def _safe_eval(self, name: str, fn: Callable[[], DimensionScore]) -> DimensionScore:
        """
        Run a dimension evaluator with broad exception protection.

        On failure, returns an empty DimensionScore with score=0 and a
        descriptive issue-free record. Logs at WARNING so failures are visible without aborting batch runs.
        """
        try:
            return fn()
        except Exception as exc:  # pragma: no cover - safety net
            logger.warning("Dimension '%s' evaluator failed: %s", name, exc, exc_info=True)
            return DimensionScore(score=0.0, issues=[], metric_breakdown={})

    def _weighted_overall(self, dims: Dict[Dimension, DimensionScore]) -> float:
        """Combine dimension scores using the configured overall weights."""
        weights = self.cfg.weights_overall
        total = 0.0
        denom = 0.0
        for dim, ds in dims.items():
            w = weights.get(dim, 0.0)
            total += ds.score * w
            denom += w
        if denom <= 0:
            # Misconfigured: fall back to mean of dimension scores.
            return sum(ds.score for ds in dims.values()) / max(len(dims), 1)
        return max(0.0, min(1.0, total / denom))

    # ------------------------------------------------------------------
    # Batch / library-level entry points
    # ------------------------------------------------------------------

    def evaluate_batch(
        self,
        members: Iterable[MemberLike],
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> List[EvaluationReport]:
        """
        Evaluate every member in ``members``.

        ``progress_callback`` (if provided) is invoked as
        ``progress_callback(completed, total)`` after each evaluation so
        a CLI or notebook can render a progress bar without coupling to any specific UI library.
        """
        members_list = list(members)
        total = len(members_list)
        reports: List[EvaluationReport] = []
        for i, member in enumerate(members_list, start=1):
            try:
                report = self.evaluate_one(member)
            except Exception as exc:  # pragma: no cover - safety net
                # Convert hard failures into skipped reports so the batch output is total over the input.
                logger.error(
                    "evaluate_one failed for member at index %d: %s",
                    i, exc, exc_info=True,
                )
                fqn = getattr(member, "fqn", None) or getattr(member, "inherited_api_name", "?")
                report = self._make_skipped_report(
                    member_id=getattr(member, "id", -1),
                    fqn=fqn,
                    api_name=fqn,
                    member_type=getattr(member, "type", None) or getattr(member, "member_type", "unknown"),
                    is_inherited=hasattr(member, "inherited_api_name"),
                    reason=f"evaluate_one raised {type(exc).__name__}: {exc}"
                )
            reports.append(report)
            if progress_callback is not None:
                progress_callback(i, total)
        return reports


    def evaluate_library(
        self,
        library_prefix: str,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> List[EvaluationReport]:
        """
        Evaluate every structured-doc member under ``library_prefix``.

        The search is limited to direct members; inherited members for the
        same library can be evaluated separately with ``evaluate_inherited _library``.
        """
        
        # Direct members with structured documentation only.
        direct = self.qm.get_members_by_doc_format("structured", library_prefix)
        return self.evaluate_batch(direct, progress_callback=progress_callback)


    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_api_reference(self, fqn: Optional[str], api_name: str) -> Optional[Dict]:
        """
        Resolve the structured ``api_reference`` JSON for a member.

        Tries (in order): direct lookup by FQN, by API name, and by the
        comprehensive ``find_member_by_any_path`` helper. Returns None if
        no structured documentation is found.
        """
        
        # 1. Direct - best when an FQN is in hand.
        if fqn:
            # ``get_member_documentation`` accepts either FQN or api_name.
            doc = self.qm.get_member_documentation(fqn)
            if doc and doc.api_reference:
                return doc.api_reference
        # 2. Direct by api_name
        if api_name:
            doc = self.qm.get_member_documentation(api_name)
            if doc and doc.api_reference:
                return doc.api_reference
        # 3. Inherited path lookup. ``find_member_by_any_path`` returns
        # a dict whose 'member' / 'original_member' may carry doc info;
        # but it's a different shape, so we use the dedicated inherited
        # documentation getter.
        if api_name:
            inherited_doc = self.qm.get_inherited_member_documentation(api_name)
            if inherited_doc and inherited_doc.get("api_reference"):
                return inherited_doc["api_reference"]
        return None


    def _make_skipped_report(
        self,
        *,
        member_id: int,
        fqn: str,
        api_name: str,
        member_type: str,
        is_inherited: bool,
        reason: str
    ) -> EvaluationReport:
        """Construct a skipped EvaluationReport with a reason."""
        
        # Skipped reports still capture the four dimension keys (with zero scores and empty issue lists) so downstream consumers can iterate uniformly
        empty = {dim: DimensionScore(score=0.0, issues=[]) for dim in Dimension}
        return EvaluationReport(
            member_id=member_id,
            member_fqn=fqn,
            member_api_name=api_name,
            member_type=member_type,
            is_inherited=is_inherited,
            code_truth_available=False,
            overall_score=0.0,
            dimensions=empty,
            evaluation_timestamp=datetime.now(timezone.utc),
            skipped=True,
            skip_reason=reason
        )
