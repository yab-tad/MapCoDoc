"""
Core data contracts for the doc_quality package.

These dataclasses define the boundaries between the evaluator, the
maintainer, the artifact store, and the CLI/reporting layer. Keeping them
in a single module (rather than scattered across submodules) makes the
contract trivial to inspect and prevents circular imports - every other
module in the package may depend on ``models`` but ``models`` depends on
nothing else inside the package.

The dataclasses fall into three groups:

1. Enums - ``Dimension``, ``Severity``, ``MaintainerStrategy``. These are
   string enums so that JSON serialization is human-readable.
2. Issue / Score / Report - the artifacts produced by the evaluator.
3. Patch / Candidate - the artifacts produced by the maintainer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING


def _utc_now() -> datetime:
    """
    Return a timezone-aware ``datetime`` in UTC.

    Centralized helper because ``datetime.utcnow()`` is deprecated on
    Python 3.12+; using ``datetime.now(timezone.utc)`` produces an
    aware object that round-trips correctly through JSON via
    ``isoformat()``.
    """
    return datetime.now(timezone.utc)

# Avoid a circular import: ``IssueType`` is defined in issue_types.py which itself uses ``Dimension``/``Severity``/``MaintainerStrategy`` from this module
# The string-quoted forward reference in ``Issue.issue_type`` keeps this resolution lazy and tooling-friendly
if TYPE_CHECKING:
    from doc_quality.issue_types import IssueType


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class Dimension(str, Enum):
    """
    The four orthogonal quality dimensions assessed by the evaluator.

    Inheriting from ``str`` makes JSON serialization round-trip cleanly:
    ``json.dumps(Dimension.COMPLETENESS)`` yields ``"completeness"``.
    """

    COMPLETENESS = "completeness"
    ACCURACY = "accuracy"
    READABILITY = "readability"
    MAINTAINABILITY = "maintainability"
    FIDELITY = "fidelity"   # source-grounding; not part of overall quality weights


class Severity(str, Enum):
    """
    Severity of an individual ``Issue``.

    Severities are intentionally coarse - HIGH/MEDIUM/LOW - to keep
    triage decisions simple. Numeric weights for aggregation live in
    ``EvaluatorConfig`` so they can be tuned without touching the enum.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MaintainerStrategy(str, Enum):
    """
    How a given issue should be addressed by the maintainer.

    * ``DB_QUERY``: copy authoritative value directly from MapCoDoc's database
      (parameter type from AST, return type from annotations, etc.). Fully
      deterministic, no LLM cost.
    * ``AST_DERIVED``: regenerate a value via small AST manipulation
      (e.g. reformat a signature). Deterministic.
    * ``LLM``: requires a generative model to produce or rewrite content
      (e.g. write a missing description, paraphrase a verbose one). Held
      behind a feature flag in v1.
    * ``MANUAL``: the issue is ambiguous or signals an upstream pipeline
      failure; do not attempt automatic fixing.
    """

    DB_QUERY = "db_query"
    AST_DERIVED = "ast_derived"
    LLM = "llm"
    MANUAL = "manual"


# ---------------------------------------------------------------------------
# Evaluation artifacts
# ---------------------------------------------------------------------------

@dataclass
class Issue:
    """
    A single quality issue pinpointed to a location in ``api_reference``.

    Each issue carries enough information for the maintainer to either fix
    it programmatically (``code_value`` plus ``json_path``) or to flag it
    for human review. ``json_path`` follows JSONPath expression syntax
    understood by the ``jsonpath-ng`` library.
    """

    # Reference into the IssueType enum, kept as a forward reference to
    # avoid importing issue_types at module-import time.
    issue_type: "IssueType"

    # Which of the four dimensions this issue belongs to. Duplicates the
    # value carried on ``issue_type.value.dimension`` but stored explicitly
    # so reports remain self-describing without enum resolution.
    dimension: Dimension

    severity: Severity

    # Human-readable section label (e.g. ``"parameters"``, ``"returns"``).
    # Used for grouping in reports; not parsed by code.
    section: str

    # A specific item identifier within the section. For per-parameter
    # issues this is the parameter name; for whole-section issues it is None.
    target: Optional[str]

    # A concrete JSONPath expression that addresses the offending field
    # within the ``api_reference`` JSON blob. The maintainer uses this as
    # the location for any patch it generates.
    json_path: str

    # Free-form one-line description that appears in human-facing reports.
    detail: str

    # The "ground truth" value, when one exists in the DB or AST. For
    # DB_QUERY/AST_DERIVED strategies the maintainer simply writes this
    # value to ``json_path``.
    code_value: Optional[Any] = None

    # The current value at the doc-side location. Useful for diffs and
    # for human review.
    doc_value: Optional[Any] = None

    # Default strategy assigned by the issue type; the maintainer may
    # downgrade (e.g. to MANUAL) based on configuration or context.
    maintainer_strategy: MaintainerStrategy = MaintainerStrategy.MANUAL

    # Catch-all for context that doesn't deserve its own field
    # (e.g. raw metric values, normalized type strings, etc.).
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DimensionScore:
    """
    Aggregated score plus issue list for a single quality dimension.

    ``score`` is in [0.0, 1.0] where 1.0 means "no detected issues".
    ``metric_breakdown`` records the per-metric subscores so reports can
    explain why a dimension scored as it did.
    """

    score: float
    issues: List[Issue] = field(default_factory=list)
    metric_breakdown: Dict[str, float] = field(default_factory=dict)


@dataclass
class EvaluationReport:
    """
    The full evaluation result for a single documented member.

    Members that are out of scope (e.g. external inherited members where no
    code-truth is available) are still represented in the report with
    ``skipped=True`` and a ``skip_reason``; this keeps batch outputs total
    over the input population.
    """

    member_id: int
    member_fqn: str
    member_api_name: str
    member_type: str  # 'class', 'function', or 'method'

    # True when the member came from DBInheritedMember rather than DBMember.
    is_inherited: bool

    # Whether code-side ground truth was retrievable. Some checks (accuracy,
    # type completeness) are skipped when False; the report still contains
    # what *could* be evaluated.
    code_truth_available: bool

    overall_score: float
    dimensions: Dict[Dimension, DimensionScore]
    evaluation_timestamp: datetime

    # Bumped whenever the report shape changes; downstream tools can use
    # this to migrate older artifacts.
    schema_version: str = "1.0"

    # When True, evaluation was not performed (e.g. external inherited
    # member with no code-truth source). The dimensions dict is empty.
    skipped: bool = False
    skip_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Maintenance artifacts
# ---------------------------------------------------------------------------

@dataclass
class MaintenancePatch:
    """
    A single proposed patch at a JSON path within ``api_reference``.

    The maintainer constructs one of these for every actionable issue.
    The patch applicator consumes them in order and produces a candidate
    JSON document; the patch itself is never applied directly to the DB.
    """

    issue: Issue
    json_path: str
    old_value: Any
    new_value: Any
    strategy: MaintainerStrategy

    # Confidence in [0,1] for LLM-strategy patches. Always None for deterministic strategies (DB_QUERY, AST_DERIVED)
    confidence: Optional[float] = None

    # Optional human-readable explanation, e.g. why the LLM chose this
    # rewording. Surfaced in the diff view during manual approval.
    rationale: Optional[str] = None

    # Defaults to "now" so callers don't have to remember to set it.
    timestamp: datetime = field(default_factory=_utc_now)


@dataclass
class MaintenanceCandidate:
    """
    Bundles an original ``api_reference``, a candidate ``api_reference``,
    and the ordered patch list that connects them.

    Persisted to ``artifacts/maintained/<api_name>.json`` and reviewed by a
    human before any DB writeback.
    """

    member_fqn: str
    member_api_name: str

    # Snapshot of api_reference at evaluation time. Together with
    # ``patches`` it provides everything needed to roll back a write.
    original_api_reference: Dict

    # Result of applying ``patches`` to the original. Stored explicitly so
    # the approval workflow can show diffs without re-running the
    # patch applicator.
    candidate_api_reference: Dict

    patches: List[MaintenancePatch]
    timestamp: datetime = field(default_factory=_utc_now)

    # Set by the approval workflow. Maintained candidates start unapproved;
    # only after a human review does ``approved`` flip to True, at which
    # point a separate "apply" step writes back to the DB.
    approved: bool = False

    # Set after the candidate's content has been persisted to ``DBMember.api_reference`` (or the inherited equivalent)
    applied_to_db: bool = False
