"""
Format ``EvaluationReport`` lists or ``LibraryAggregate`` instances.

Three output formats are supported:

* JSON  - canonical, machine-readable, lossless.
* CSV   - one row per member, suitable for spreadsheet review.
* HTML  - a single-page summary intended for human reviewers.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Iterable, List

from doc_quality.artifacts.store import _ArtifactEncoder  # reuse encoder
from doc_quality.models import EvaluationReport
from doc_quality.reporting.aggregator import LibraryAggregate


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def format_json(
    reports: Iterable[EvaluationReport],
    aggregate: LibraryAggregate | None = None,
    indent: int = 2
) -> str:
    """Render reports (and optional aggregate) as a JSON document."""
    payload = {
        "reports": list(reports),
        "aggregate": aggregate,
    }
    return json.dumps(payload, cls=_ArtifactEncoder, indent=indent, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

# Columns chosen for at-a-glance review. Per-issue detail is too verbose for CSV; reviewers should drill into the JSON or HTML output for that.
_CSV_FIELDS = [
    "api_name",
    "fqn",
    "type",
    "is_inherited",
    "skipped",
    "overall_score",
    "completeness",
    "accuracy",
    "readability",
    "maintainability",
    "issue_count_high",
    "issue_count_medium",
    "issue_count_low"
]


def format_csv(reports: Iterable[EvaluationReport]) -> str:
    """Render reports as CSV.  Returns the full CSV text."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS)
    writer.writeheader()
    
    for r in reports:
        # Tally severity counts across all dimensions for this report.
        sev_counts = {"high": 0, "medium": 0, "low": 0}
        
        for ds in r.dimensions.values():
            for issue in ds.issues:
                sev_counts[issue.severity.value] += 1
                
        row = {
            "api_name": r.member_api_name,
            "fqn": r.member_fqn,
            "type": r.member_type,
            "is_inherited": r.is_inherited,
            "skipped": r.skipped,
            "overall_score": f"{r.overall_score:.3f}",
            "completeness": _dim_score(r, "completeness"),
            "accuracy": _dim_score(r, "accuracy"),
            "readability": _dim_score(r, "readability"),
            "maintainability": _dim_score(r, "maintainability"),
            "issue_count_high": sev_counts["high"],
            "issue_count_medium": sev_counts["medium"],
            "issue_count_low": sev_counts["low"]
        }
        writer.writerow(row)
    return buf.getvalue()


def _dim_score(r: EvaluationReport, dim_value: str) -> str:
    """Format a single dimension's score for the CSV row."""
    for dim, ds in r.dimensions.items():
        if dim.value == dim_value:
            return f"{ds.score:.3f}"
    return ""


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

# A single-template HTML report. Use a self-contained
# inline-CSS template rather than a templating engine to keep the package
# dependency-light. The output is ~20 KB even for hundreds of members.
_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Doc Quality Report - {library} {version}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  h1, h2 {{ border-bottom: 2px solid #eee; padding-bottom: 0.3em; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ padding: 0.5em 0.75em; text-align: left; border-bottom: 1px solid #eee; }}
  th {{ background: #f7f7f7; }}
  .score {{ font-family: "SF Mono", Consolas, monospace; }}
  .high {{ color: #b00; font-weight: 600; }}
  .medium {{ color: #c70; }}
  .low {{ color: #555; }}
  .skipped {{ background: #fafafa; color: #888; font-style: italic; }}
  .bar {{ display: inline-block; height: 8px; background: linear-gradient(90deg,
          #c00 0%, #cc0 50%, #0a0 100%); border-radius: 4px; }}
  details {{ margin: 0.5rem 0; }}
  summary {{ cursor: pointer; }}
  pre {{ background: #f7f7f7; padding: 0.6em; overflow-x: auto;
         font-size: 0.9em; }}
</style>
</head>
<body>
<h1>Doc Quality Report</h1>
<p>Library: <strong>{library}</strong> &mdash; Version: <strong>{version}</strong>
   &mdash; Members: <strong>{member_count}</strong>
   (skipped: {skipped_count})</p>

<h2>Aggregate Scores</h2>
<table>
<tr><th>Dimension</th><th>Mean</th><th>Median</th></tr>
<tr><td>Overall</td><td class="score">{overall_mean:.2f}</td><td class="score">{overall_median:.2f}</td></tr>
{dimension_rows}
</table>

<h2>Severity Distribution</h2>
<table>
<tr><th>Severity</th><th>Count</th></tr>
{severity_rows}
</table>

<h2>Top Issue Types</h2>
<table>
<tr><th>Issue Type</th><th>Occurrences</th></tr>
{issue_type_rows}
</table>

<h2>Worst Members</h2>
<table>
<tr><th>API Name</th><th>Score</th></tr>
{worst_rows}
</table>

<h2>Best Members</h2>
<table>
<tr><th>API Name</th><th>Score</th></tr>
{best_rows}
</table>

<h2>Per-Member Reports</h2>
{per_member_blocks}

</body>
</html>
"""


def format_html(reports: Iterable[EvaluationReport], aggregate: LibraryAggregate) -> str:
    """
    Render a self-contained HTML report.

    The function is deliberately straightforward (string formatting
    rather than templating) so the package has no template-engine dependency.
    """
    reports = list(reports)
    # Aggregate sections
    dim_rows = "".join(
        f"<tr><td>{dim.title()}</td>"
        f"<td class='score'>{aggregate.dimension_means.get(dim, 0):.2f}</td>"
        f"<td class='score'>{aggregate.dimension_medians.get(dim, 0):.2f}</td></tr>"
        for dim in ("completeness", "accuracy", "readability", "maintainability")
    )
    sev_rows = "".join(
        f"<tr><td class='{sev}'>{sev.title()}</td><td>{count}</td></tr>"
        for sev, count in sorted(aggregate.severity_counts.items())
    )
    issue_rows = "".join(
        f"<tr><td>{code}</td><td>{count}</td></tr>"
        for code, count in sorted(
            aggregate.issue_type_counts.items(), key=lambda kv: -kv[1],
        )[:25]
    )
    worst_rows = "".join(
        f"<tr><td>{name}</td><td class='score'>{score:.2f}</td></tr>"
        for name, score in aggregate.worst_members
    )
    best_rows = "".join(
        f"<tr><td>{name}</td><td class='score'>{score:.2f}</td></tr>"
        for name, score in aggregate.best_members
    )

    # Per-member detail blocks.  Each is a <details> for compactness.
    per_member: List[str] = []
    for r in reports:
        if r.skipped:
            block = (f"<details class='skipped'><summary>{_e(r.member_api_name)} "
                     f"(skipped: {_e(r.skip_reason or '?')})</summary></details>")
            per_member.append(block)
            continue
        issue_blocks = []
        for dim, ds in r.dimensions.items():
            if not ds.issues:
                continue
            issue_blocks.append(
                f"<h4>{dim.value.title()} (score: {ds.score:.2f})</h4><ul>"
            )
            for issue in ds.issues:
                issue_blocks.append(
                    f"<li class='{issue.severity.value}'>"
                    f"<strong>[{_e(issue.issue_type.value.code)}]</strong> "
                    f"{_e(issue.detail)} <code>{_e(issue.json_path)}</code></li>"
                )
            issue_blocks.append("</ul>")
        block = (
            f"<details><summary>{_e(r.member_api_name)} "
            f"(score: {r.overall_score:.2f})</summary>"
            f"{''.join(issue_blocks)}"
            f"</details>"
        )
        per_member.append(block)

    return _HTML_TEMPLATE.format(
        library=_e(aggregate.library),
        version=_e(aggregate.version),
        member_count=aggregate.member_count,
        skipped_count=aggregate.skipped_count,
        overall_mean=aggregate.overall_score_mean,
        overall_median=aggregate.overall_score_median,
        dimension_rows=dim_rows,
        severity_rows=sev_rows,
        issue_type_rows=issue_rows,
        worst_rows=worst_rows,
        best_rows=best_rows,
        per_member_blocks="\n".join(per_member)
    )


def _e(s) -> str:
    """Minimal HTML-escape sufficient for the report template."""
    if s is None:
        return ""
    return (
        str(s).replace("&", "&amp;")
              .replace("<", "&lt;")
              .replace(">", "&gt;")
              .replace('"', "&quot;")
    )
