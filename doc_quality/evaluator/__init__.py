"""Evaluator subpackage - per-dimension checks plus the orchestrator.

Each dimension lives in its own module and exposes a single function

    evaluate(view, code_truth, class_members, config) -> DimensionScore

The orchestrator (``evaluator.py``) calls them in turn, aggregates the
DimensionScores into an EvaluationReport, and emits artifacts.
"""

from doc_quality.evaluator.evaluator import DocQualityEvaluator

__all__ = ["DocQualityEvaluator"]
