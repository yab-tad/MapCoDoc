"""Reporting subpackage - cross-member aggregation and formatting."""

from doc_quality.reporting.aggregator import LibraryAggregate, aggregate_reports
from doc_quality.reporting.formatters import (
    format_csv,
    format_html,
    format_json,
)

__all__ = [
    "LibraryAggregate",
    "aggregate_reports",
    "format_csv",
    "format_html",
    "format_json",
]
