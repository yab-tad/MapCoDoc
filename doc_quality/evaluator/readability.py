"""
Readability dimension aggregator.

Combines the text-readability and code-readability sub-evaluators into a
single ``DimensionScore`` for the readability dimension. The weights for
the text/code blend live in ``EvaluatorConfig.weights_readability``; the
default favours text (60%) over code (40%) because text sections vastly
outnumber example sections in a typical structured document.
"""

from __future__ import annotations

from typing import List, Optional

from doc_quality.class_member_lister import ClassMembers
from doc_quality.code_truth_resolver import CodeTruth
from doc_quality.config import EvaluatorConfig
from doc_quality.doc_views import DocView
from doc_quality.evaluator import readability_code, readability_text
from doc_quality.models import DimensionScore, Issue


def evaluate(
    view: DocView,
    code_truth: Optional[CodeTruth],
    class_members: Optional[ClassMembers],
    config: EvaluatorConfig
) -> DimensionScore:
    """Run both sub-evaluators and combine into one DimensionScore."""
    issues: List[Issue] = []
    breakdown: dict[str, float] = {}

    # --- Text readability --------------------------------------------
    text_score, text_issues, text_breakdown = readability_text.evaluate(view, config)
    issues.extend(text_issues)
    breakdown.update(text_breakdown)

    # --- Code readability --------------------------------------------
    code_score, code_issues, code_breakdown = readability_code.evaluate(view, config)
    issues.extend(code_issues)
    breakdown.update(code_breakdown)

    # --- Combine -----------------------------------------------------
    weights = config.weights_readability
    text_w = weights.get("text", 0.5)
    code_w = weights.get("code", 0.5)
    total = text_w + code_w
    if total <= 0:
        # Defensive: misconfigured weights => uniform.
        score = (text_score + code_score) / 2.0
    else:
        score = (text_score * text_w + code_score * code_w) / total

    return DimensionScore(
        score=max(0.0, min(1.0, score)),
        issues=issues,
        metric_breakdown=breakdown
    )
