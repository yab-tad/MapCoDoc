"""
Cross-member aggregation of evaluation reports.

The evaluator produces one ``EvaluationReport`` per documented member.
For library-wide insight we want the aggregate picture: average scores
per dimension, distribution of issue types, top offenders, etc.

This module provides ``LibraryAggregate`` and the ``aggregate_reports``
entry point.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple

from doc_quality.models import (
    Dimension,
    EvaluationReport,
    Issue,
    Severity
)


@dataclass
class LibraryAggregate:
    """Aggregate quality picture across many members of a library."""

    library: str
    version: str
    member_count: int
    skipped_count: int
    overall_score_mean: float
    overall_score_median: float
    dimension_means: Dict[str, float] = field(default_factory=dict)
    dimension_medians: Dict[str, float] = field(default_factory=dict)
    issue_type_counts: Dict[str, int] = field(default_factory=dict)
    severity_counts: Dict[str, int] = field(default_factory=dict)
    # Lowest-scoring members. Tuples of ``(api_name, score)`` for the bottom 10 members; used by reports to highlight worst offenders.
    worst_members: List[Tuple[str, float]] = field(default_factory=list)
    best_members: List[Tuple[str, float]] = field(default_factory=list)


def aggregate_reports(
    reports: Iterable[EvaluationReport],
    library: str = "",
    version: str = "",
    top_n: int = 10
) -> LibraryAggregate:
    """
    Compute a ``LibraryAggregate`` from a list of evaluation reports.

    Skipped reports are counted but excluded from score statistics so
    they don't drag down the means with zero values that don't reflect
    actual document quality.
    """
    reports_list = list(reports)
    member_count = len(reports_list)

    # Partition into evaluable vs skipped.
    evaluable = [r for r in reports_list if not r.skipped]
    skipped_count = member_count - len(evaluable)

    overall_scores = [r.overall_score for r in evaluable]
    overall_mean = statistics.fmean(overall_scores) if overall_scores else 0.0
    overall_median = statistics.median(overall_scores) if overall_scores else 0.0

    # Per-dimension stats.
    dim_scores: Dict[str, List[float]] = defaultdict(list)
    for r in evaluable:
        for dim, ds in r.dimensions.items():
            dim_scores[dim.value].append(ds.score)
    dim_means = {
        dim: (statistics.fmean(scores) if scores else 0.0)
        for dim, scores in dim_scores.items()
    }
    dim_medians = {
        dim: (statistics.median(scores) if scores else 0.0)
        for dim, scores in dim_scores.items()
    }

    # Issue counts by type and severity, across all reports.
    issue_type_counter: Counter = Counter()
    severity_counter: Counter = Counter()
    for r in evaluable:
        for ds in r.dimensions.values():
            for issue in ds.issues:
                issue_type_counter[issue.issue_type.value.code] += 1
                severity_counter[issue.severity.value] += 1

    # Worst and best members by overall score.
    sorted_by_score = sorted(
        ((r.member_api_name, r.overall_score) for r in evaluable),
        key=lambda x: x[1]
    )
    worst = sorted_by_score[:top_n]
    best = sorted_by_score[-top_n:][::-1]

    return LibraryAggregate(
        library=library,
        version=version,
        member_count=member_count,
        skipped_count=skipped_count,
        overall_score_mean=overall_mean,
        overall_score_median=overall_median,
        dimension_means=dim_means,
        dimension_medians=dim_medians,
        issue_type_counts=dict(issue_type_counter),
        severity_counts=dict(severity_counter),
        worst_members=worst,
        best_members=best
    )
