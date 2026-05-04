"""
doc_quality - Documentation Quality Evaluation and Targeted Maintenance for MapCoDoc.

This package operates exclusively on structured documentation
(``DBMember.doc_format == 'structured'``) and assesses each documented public
API member across four quality dimensions:

* Completeness - whether every required section/field is populated.
* Accuracy - whether documented signatures, parameter names/types/defaults,
  and return types agree with the code-truth captured in the database.
* Readability - the textual readability of prose sections and the
  illustrative quality of code examples.
* Maintainability - how well the documentation uses cross-references and
  hyperlinks instead of inline restatement of canonical content.

The evaluator emits a structured ``EvaluationReport`` with per-dimension scores
and a list of ``Issue`` records pinpointed to JSON paths within the
``api_reference`` blob. The maintainer consumes those issues, dispatches each
to a strategy (``DB_QUERY``, ``AST_DERIVED``, ``LLM``, ``MANUAL``), and
produces a ``MaintenanceCandidate`` whose ``api_reference`` contains the
proposed corrections. A separate manual approval step gates any DB writeback.

Public entry points are re-exported here for convenient access::

    from doc_quality import (
        DocQualityEvaluator,
        DocQualityMaintainer,
        EvaluationReport,
        Issue,
        IssueType,
        Dimension,
        Severity,
    )
"""

# Core data contracts.  These are deliberately exposed at the package root so
# that consumers (CLI, scripts, notebooks) can construct, persist, and inspect
# evaluation results without reaching into submodules.
from doc_quality.models import (
    Dimension,
    Severity,
    MaintainerStrategy,
    Issue,
    DimensionScore,
    EvaluationReport,
    MaintenancePatch,
    MaintenanceCandidate,
)

# The IssueType enum is the formal contract between the evaluator and
# maintainer; both produce and consume issues whose .issue_type values must
# be drawn from this enumeration.
from doc_quality.issue_types import IssueType, IssueTypeSpec

# The package version is exposed for provenance bookkeeping in artifacts.
__version__ = "0.1.0"

__all__ = [
    "Dimension",
    "Severity",
    "MaintainerStrategy",
    "Issue",
    "DimensionScore",
    "EvaluationReport",
    "MaintenancePatch",
    "MaintenanceCandidate",
    "IssueType",
    "IssueTypeSpec",
    "__version__"
]
