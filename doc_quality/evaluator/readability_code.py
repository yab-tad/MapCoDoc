"""
Code-readability metrics for the documentation's ``examples`` section.

The metric set is adapted from Scalabrino et al. (2018) *A Comprehensive
Model for Code Readability*. Their model was trained on production code
methods; API documentation examples have different characteristics and
therefore different reasonable weights:

* Examples are short by design (3-15 lines is typical). Penalizing low LOC is wrong here.
* Examples are illustrative, so comments add value rather than noise.
* Single-character identifiers, fine in tight numerical code, hurt illustrative clarity.

This module extracts a Scalabrino-style feature vector from each example
and reduces it to a [0,1] score using weights tuned for examples. It also
emits issues for each example that violates a specific threshold.

``radon`` is treated as an optional dependency; without it Halstead and cyclomatic complexity features are reported as ``None`` and excluded from the score.
"""

from __future__ import annotations

import ast
import logging
import re
import statistics
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from doc_quality.config import EvaluatorConfig
from doc_quality.doc_views import DocView
from doc_quality.issue_types import IssueType
from doc_quality.models import Dimension, DimensionScore, Issue
from doc_quality.presence import is_present


logger = logging.getLogger(__name__)


try:
    from radon.complexity import cc_visit  # type: ignore
    from radon.metrics import h_visit  # type: ignore
    _RADON_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-dependent
    cc_visit = None  # type: ignore
    h_visit = None  # type: ignore
    _RADON_AVAILABLE = False


# ---------------------------------------------------------------------------
# Code-fence stripping
# ---------------------------------------------------------------------------
# Examples are typically wrapped in triple-backtick fenced code blocks (sometimes with a language tag). We strip those before AST parsing.
# Output annotations like ``>>> ...`` and ``... continuation`` are preserved because they are meaningful for readability assessment.
_FENCE_RE = re.compile(r"^```(?:python|py|pycon)?\s*\n?(.*?)\n?```$", re.DOTALL)
# REPL-style prompts: ``>>>`` and ``...`` continuation. We strip these only when probing AST parseability; otherwise they're left intact for the textual metrics.
_REPL_PROMPT_RE = re.compile(r"^\s*(?:>>>|\.\.\.)\s?", re.MULTILINE)
# Detect output annotation patterns (lines starting with ``# Output:`` or REPL prompts) for the "no explanation" check.
_OUTPUT_ANNOT_RE = re.compile(r"(?:^\s*#\s*Output\b|^\s*>>>|^\s*\.\.\.)", re.MULTILINE)


@dataclass
class CodeMetrics:
    """Per-example feature vector."""

    loc: int
    blank_line_ratio: float
    max_indent_depth: int
    avg_identifier_length: float
    single_char_ratio: float
    comment_line_ratio: float
    has_output_annotation: bool
    parse_success: bool
    import_count: int
    halstead_volume: Optional[float]
    cyclomatic_complexity: Optional[int]


def evaluate(view: DocView, config: EvaluatorConfig) -> Tuple[float, List[Issue], Dict[str, float]]:
    """
    Compute the code-readability score for the document's examples.

    Returns a ``(score, issues, breakdown)`` tuple. When the document has
    no examples the score is 1.0 (no examples => no readability concerns
    in this dimension; missing-examples is a *completeness* concern).
    """
    issues: List[Issue] = []
    breakdown: Dict[str, float] = {}

    examples = view.get_examples() or []
    real_examples = [
        (idx, ex) for idx, ex in enumerate(examples)
        if is_present(ex.get("example"))
    ]
    if not real_examples:
        breakdown["CodeReadability"] = 1.0
        return 1.0, [], breakdown

    sub_scores: List[float] = []
    for idx, ex in real_examples:
        code = _strip_fences(ex.get("example") or "")
        metrics = _extract_metrics(code)
        sub_score = _score_metrics(metrics, config)
        sub_scores.append(sub_score)

        # Emit issues for specific defects.
        if not metrics.parse_success:
            issues.append(Issue(
                issue_type=IssueType.READ_EXAMPLE_PARSE_FAILED,
                dimension=Dimension.READABILITY,
                severity=IssueType.READ_EXAMPLE_PARSE_FAILED.value.default_severity,
                section="examples",
                target=str(idx),
                json_path=f"$.examples[{idx}].example",
                detail=f"Example #{idx} does not parse as valid Python.",
                doc_value=code,
                maintainer_strategy=IssueType.READ_EXAMPLE_PARSE_FAILED.value.default_strategy
            ))

        if metrics.single_char_ratio >= config.single_char_ratio_threshold:
            issues.append(Issue(
                issue_type=IssueType.READ_EXAMPLE_SINGLE_CHAR_VAR,
                dimension=Dimension.READABILITY,
                severity=IssueType.READ_EXAMPLE_SINGLE_CHAR_VAR.value.default_severity,
                section="examples",
                target=str(idx),
                json_path=f"$.examples[{idx}].example",
                detail=(f"Example #{idx} has single-char identifier ratio {metrics.single_char_ratio:.2f} (>= {config.single_char_ratio_threshold})."),
                doc_value=code,
                metadata={"single_char_ratio": metrics.single_char_ratio},
                maintainer_strategy=IssueType.READ_EXAMPLE_SINGLE_CHAR_VAR.value.default_strategy
            ))

        if metrics.loc > config.example_max_loc or (
            metrics.cyclomatic_complexity is not None
            and metrics.cyclomatic_complexity > config.example_max_cc
        ):
            issues.append(Issue(
                issue_type=IssueType.READ_EXAMPLE_HIGH_COMPLEXITY,
                dimension=Dimension.READABILITY,
                severity=IssueType.READ_EXAMPLE_HIGH_COMPLEXITY.value.default_severity,
                section="examples",
                target=str(idx),
                json_path=f"$.examples[{idx}].example",
                detail=(f"Example #{idx} is too long/complex: loc={metrics.loc}, cc={metrics.cyclomatic_complexity}."),
                doc_value=code,
                metadata={"loc": metrics.loc,
                          "cyclomatic_complexity": metrics.cyclomatic_complexity},
                maintainer_strategy=IssueType.READ_EXAMPLE_HIGH_COMPLEXITY.value.default_strategy
            ))

        # Explanation check: no comments, no output annotation, and no accompanying additional_information => readability concern.
        if (
            metrics.comment_line_ratio == 0.0
            and not metrics.has_output_annotation
            and not is_present(ex.get("additional_information"))
        ):
            issues.append(Issue(
                issue_type=IssueType.READ_EXAMPLE_NO_EXPLANATION,
                dimension=Dimension.READABILITY,
                severity=IssueType.READ_EXAMPLE_NO_EXPLANATION.value.default_severity,
                section="examples",
                target=str(idx),
                json_path=f"$.examples[{idx}].additional_information",
                detail=(f"Example #{idx} has no comments, output annotation, or accompanying explanation."),
                doc_value=code,
                maintainer_strategy=IssueType.READ_EXAMPLE_NO_EXPLANATION.value.default_strategy
            ))

    code_score = statistics.fmean(sub_scores) if sub_scores else 1.0
    breakdown["CodeReadability"] = code_score
    return code_score, issues, breakdown


def _strip_fences(code: str) -> str:
    """Remove triple-backtick code-fence wrappers from an example string."""
    # The structured doc preserves Markdown fences. Strip them so AST parsing
    # works. Preserve the inner content verbatim - whitespace matters for indent metrics.
    m = _FENCE_RE.match(code.strip())
    if m:
        return m.group(1)
    return code


def _extract_metrics(code: str) -> CodeMetrics:
    """Run the feature extractors over a single example's code body."""
    # Visual & structural features ------------------------------------
    raw_lines = code.splitlines()
    non_blank = [ln for ln in raw_lines if ln.strip()]
    loc = len(non_blank)
    blank_ratio = (
        (len(raw_lines) - loc) / max(len(raw_lines), 1)
    )

    # Max indent depth: count leading whitespace and convert to "indent units". Tabs count as one indent; ``    `` (4 spaces) as one indent.
    max_indent = 0
    for ln in non_blank:
        leading = len(ln) - len(ln.lstrip(" \t"))
        # Heuristic: if line starts with tab(s) count tabs, else divide spaces by 4. No need to be exact because Python tolerates mixed indentation across files.
        if ln.startswith("\t"):
            depth = len(ln) - len(ln.lstrip("\t"))
        else:
            depth = leading // 4
        max_indent = max(max_indent, depth)

    # Comment ratio: lines whose first non-whitespace char is ``#``. This catches both whole-line comments and trailing-only comments are not counted (intentionally - trailing comments are noise more often than commentary).
    comment_count = sum(
        1 for ln in non_blank if ln.lstrip().startswith("#")
    )
    comment_ratio = comment_count / loc if loc else 0.0

    has_output = bool(_OUTPUT_ANNOT_RE.search(code))

    # AST-based features ------------------------------------------------
    # Strip REPL prompts before parsing so ``>>> a = 1`` becomes ``a = 1``.
    parseable = _REPL_PROMPT_RE.sub("", code)

    parse_success = True
    identifiers: List[str] = []
    import_count = 0
    halstead_volume: Optional[float] = None
    cyclomatic: Optional[int] = None

    try:
        tree = ast.parse(parseable)
    except SyntaxError:
        parse_success = False
        tree = None
    except Exception:  # pragma: no cover - should be SyntaxError-only
        parse_success = False
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            # Identifiers: names + arg names + def names.
            if isinstance(node, ast.Name):
                identifiers.append(node.id)
            elif isinstance(node, ast.arg):
                identifiers.append(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                identifiers.append(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                import_count += 1

    avg_id_len = (statistics.fmean(len(i) for i in identifiers) if identifiers else 0.0)
    single_char_count = sum(1 for i in identifiers if len(i) == 1)
    single_char_ratio = (single_char_count / len(identifiers) if identifiers else 0.0)

    # radon-derived features (optional) --------------------------------
    if _RADON_AVAILABLE and parse_success:
        try:
            # h_visit returns an object with .total.volume; older versions
            # return a list. We probe the .total attribute conservatively.
            hv = h_visit(parseable)
            total = getattr(hv, "total", None)
            halstead_volume = (
                float(total.volume) if total and total.volume else None
            )
        except Exception as exc:
            logger.debug("radon h_visit failed: %s", exc)
        try:
            cc_blocks = cc_visit(parseable)
            if cc_blocks:
                cyclomatic = max(b.complexity for b in cc_blocks)
            else:
                # No top-level functions/classes: treat as CC=1 (linear).
                cyclomatic = 1
        except Exception as exc:
            logger.debug("radon cc_visit failed: %s", exc)

    return CodeMetrics(
        loc=loc,
        blank_line_ratio=blank_ratio,
        max_indent_depth=max_indent,
        avg_identifier_length=avg_id_len,
        single_char_ratio=single_char_ratio,
        comment_line_ratio=comment_ratio,
        has_output_annotation=has_output,
        parse_success=parse_success,
        import_count=import_count,
        halstead_volume=halstead_volume,
        cyclomatic_complexity=cyclomatic
    )


def _score_metrics(metrics: CodeMetrics, config: EvaluatorConfig) -> float:
    """Reduce a ``CodeMetrics`` instance to a [0,1] readability score.

    Weight assignments (Scalabrino-adapted for examples):

    * Parse success = 0.30   - the strongest single signal
    * Identifier quality = 0.20  - includes single-char ratio (penalty)
    * Output annotation = 0.10
    * Comment presence = 0.10
    * Indent reasonableness = 0.10
    * LOC reasonableness = 0.10
    * CC reasonableness = 0.10  - used only if radon is available
    """
    contributions: List[Tuple[float, float]] = []  # (weight, score)

    # Parse success
    contributions.append((0.30, 1.0 if metrics.parse_success else 0.0))

    # Identifier quality: average length >= 3 chars + low single-char ratio.
    # Combine into a single contribution.
    id_score = 1.0
    if metrics.avg_identifier_length and metrics.avg_identifier_length < 3.0:
        id_score *= metrics.avg_identifier_length / 3.0
    if metrics.single_char_ratio:
        id_score *= max(0.0, 1.0 - metrics.single_char_ratio * 1.5)
    contributions.append((0.20, id_score))

    # Output annotation - either a comment-output or REPL prompt.
    contributions.append((0.10, 1.0 if metrics.has_output_annotation else 0.5))

    # Comment presence: any whole-line comment is enough to score full.
    contributions.append((0.10, 1.0 if metrics.comment_line_ratio > 0 else 0.6))

    # Indent reasonableness: penalize deep nesting.
    indent_score = 1.0 if metrics.max_indent_depth <= 3 else max(
        0.0, 1.0 - 0.2 * (metrics.max_indent_depth - 3),
    )
    contributions.append((0.10, indent_score))

    # LOC reasonableness: linear penalty above the cap.
    loc_score = 1.0 if metrics.loc <= config.example_max_loc else max(
        0.0, 1.0 - (metrics.loc - config.example_max_loc) / 30.0,
    )
    contributions.append((0.10, loc_score))

    if metrics.cyclomatic_complexity is not None:
        cc_score = 1.0 if metrics.cyclomatic_complexity <= config.example_max_cc else max(
            0.0, 1.0 - 0.15 * (metrics.cyclomatic_complexity - config.example_max_cc),
        )
        contributions.append((0.10, cc_score))

    total_weight = sum(w for w, _ in contributions)
    if total_weight <= 0:
        return 1.0
    weighted = sum(w * s for w, s in contributions) / total_weight
    return max(0.0, min(1.0, weighted))
