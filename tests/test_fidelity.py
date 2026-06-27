"""
Fidelity Test Script
====================
Checks structured docs against their PREPROCESSED sources using
doc_quality.evaluator.fidelity. Comparison happens in placeholder space, so it
pairs:

    structured_doc/{lib}/v_{ver}_{src}/{api}.json     (still has url_placeholder_X)
    preprocessed_doc/{lib}/v_{ver}_{src}/doc/{api}.txt

For each member it records the additive issues (doc content not grounded in the
source) and omissions (source content absent from the doc), with locations, so a
human can validate them. No DB and no LLM are needed.

Usage:
    python tests/test_fidelity.py \\
        --db-path requests_2.32.5.db \
        --library-name requests --version 2.32.5 \\
        --sources web pdf \\
        --report-file tests/requests_fidelity.json \\
        --csv-file tests/requests_fidelity.csv
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from collections import Counter
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from doc_quality.config import EvaluatorConfig
from doc_quality.doc_views import doc_view
from doc_quality.evaluator import fidelity
from doc_quality.models import DimensionScore, Issue
from mapcodoc_db.db_manager import MapCoDocDB
from mapcodoc_db.query import QueryManager
from doc_processor.file_doc.extraction_utils import parse_artifact_stem


ARTIFACTS_BASE = PROJECT_ROOT / "doc_processor" / "doc_artifacts"
_OMISSION_CODE = "FID_SOURCE_OMITTED"
_EMPTY_CODE = "FID_EMPTY_EXTRACTION"
_LLM_TYPES = {"class", "function", "method"}


def _normalize_member_type(member_type: Optional[str]) -> str:
    """Same normalization the structuring step used to pick the schema, so the DocView matches the JSON shape actually emitted."""
    mt = (member_type or "").lower()
    return mt if mt in _LLM_TYPES else ""

def _resolve_member_type(qm: QueryManager, api_name: str) -> Optional[str]:
    """Authoritative member type from the DB. Returns None if the member is not found - the caller skips it rather than assuming a type."""
    m = qm.get_member_by_any_api_name(api_name)
    if m:
        return _normalize_member_type(m.type)
    inh = qm.get_inherited_member_by_api_name(api_name)
    if inh:
        return _normalize_member_type(inh.member_type)
    return None


def _dirs_for(library: str, version: str, source: str):
    lib_ver = f"{library}/v_{version}_{source}"
    structured = ARTIFACTS_BASE / "structured_doc" / lib_ver
    preprocessed = ARTIFACTS_BASE / "preprocessed_doc" / lib_ver / "doc"
    return structured, preprocessed


def _issue_to_dict(i: Issue) -> Dict:
    md = i.metadata or {}
    return {
        "issue_type": i.issue_type.value.code,
        "section": i.section,
        "json_path": i.json_path,
        "severity": i.severity.value,
        "similarity": md.get("similarity"),
        "category": md.get("category"),
        "detail": i.detail,
        "doc_value": i.doc_value,        # additive: doc text / omission: closest doc span
        "code_value": i.code_value,      # additive: closest source span / omission: omitted source text
        "source_location": (
            {
                "file": md.get("source_file"),
                "section": md.get("source_section"),
                "line_start": md.get("source_line_start"),
                "line_end": md.get("source_line_end"),
                "char_start": md.get("source_char_start"),
                "char_end": md.get("source_char_end")
            }
            if i.issue_type.value.code == _OMISSION_CODE else None
        )
    }


def _split_issues(ds: DimensionScore):
    additive, omissions, empty = [], [], []
    for i in ds.issues:
        code = i.issue_type.value.code
        if code == _EMPTY_CODE:
            empty.append(i)
        elif code == _OMISSION_CODE:
            omissions.append(i)
        else:
            additive.append(i)
    # Worst (lowest similarity) first for easy triage.
    key = lambda x: (x.metadata or {}).get("similarity", 0.0)
    return sorted(additive, key=key), sorted(omissions, key=key), empty


def _section_group(section: str) -> str:
    """Coarse section bucket: 'parameters[axis]' -> 'parameters', 'source:examples' -> 'examples', 'returns' -> 'returns'."""
    if section.startswith("source:"):
        section = section[len("source:"):]
    for sep in ("[", "."):
        idx = section.find(sep)
        if idx != -1:
            section = section[:idx]
    return section or "(unknown)"


def evaluate_source(library: str, version: str, source: str, config: EvaluatorConfig, qm: QueryManager, limit: Optional[int]) -> Dict:
    structured_dir, preprocessed_dir = _dirs_for(library, version, source)
    members: List[Dict] = []
    skipped: List[Dict] = []

    if not structured_dir.exists():
        return {"summary": {"source": source, "error": f"missing {structured_dir}"}, "members": [], "skipped": []}

    files = sorted(p for p in structured_dir.glob("*.json") if not p.name.endswith(".raw.json"))
    if limit:
        files = files[:limit]

    for sp in files:
        api = sp.stem
        try:
            api_ref = json.loads(sp.read_text(encoding="utf-8"))
        except Exception as exc:
            skipped.append({"api_name": api, "reason": f"invalid JSON: {exc}"})
            continue

        pre_path = preprocessed_dir / f"{api}.txt"
        if not pre_path.exists():
            skipped.append({"api_name": api, "reason": "no preprocessed source"})
            continue
        source_text = pre_path.read_text(encoding="utf-8")

        true_api, _ = parse_artifact_stem(api)
        mtype = _resolve_member_type(qm, true_api)
        if mtype is None:
            skipped.append({"api_name": api, "reason": "member type not found in DB"})
            continue
        view = doc_view(api_ref, mtype)
        ds = fidelity.evaluate(view, source_text, config, source_path=str(pre_path))

        if ds.metric_breakdown.get("source_missing"):
            skipped.append({"api_name": api, "reason": "empty preprocessed source"})
            continue

        additive, omissions, empty = _split_issues(ds)
        is_empty = bool(empty) or bool(ds.metric_breakdown.get("empty_extraction"))
        members.append({
            "api_name": api,
            "type": mtype,
            "grounding_score": round(ds.score, 4),
            "source_coverage_ratio": ds.metric_breakdown.get("source_coverage_ratio"),
            "units_checked": int(ds.metric_breakdown.get("units_checked", 0)),
            "source_units_checked": int(ds.metric_breakdown.get("source_units_checked", 0)),
            "additive_issue_count": len(additive),
            "omission_count": len(omissions),
            "empty_extraction": is_empty,
            "additive_issues": [_issue_to_dict(i) for i in additive],
            "omissions": [_issue_to_dict(i) for i in omissions],
            "empty_issues": [_issue_to_dict(i) for i in empty],
            "omission_scope": "n/a" if is_empty else ("skipped" if ds.metric_breakdown.get("omission_skipped") else "block")
        })
        
    sec_breakdown = {"additive": Counter(), "omission": Counter()}
    for m in members:
        for it in m["additive_issues"]:
            sec_breakdown["additive"][_section_group(it["section"])] += 1
        for it in m["omissions"]:
            sec_breakdown["omission"][_section_group(it["section"])] += 1

    evaluated = len(members)
    scoped = [m for m in members if m["source_coverage_ratio"] is not None]
    summary = {
        "source": source,
        "structured_dir": str(structured_dir),
        "evaluated": evaluated,
        "skipped": len(skipped),
        "mean_grounding": round(sum(m["grounding_score"] for m in members) / evaluated, 4) if evaluated else None,
        "mean_coverage": round(sum(m["source_coverage_ratio"] for m in scoped) / len(scoped), 4) if scoped else None,
        "omission_skipped_members": sum(1 for m in members if m["omission_scope"] == "skipped"),
        "omission_skipped_api_names": [m["api_name"] for m in members if m["omission_scope"] == "skipped"],
        "total_additive_issues": sum(m["additive_issue_count"] for m in members),
        "total_omissions": sum(m["omission_count"] for m in members),
        "empty_extractions": sum(1 for m in members if m["empty_extraction"]),
        "empty_extraction_api_names": [m["api_name"] for m in members if m["empty_extraction"]],
        "members_fully_faithful": sum(
            1 for m in members
            if not m["empty_extraction"] and m["additive_issue_count"] == 0 and m["omission_count"] == 0
        ),
        "section_breakdown": {k: dict(v) for k, v in sec_breakdown.items()}
    }
    return {"summary": summary, "members": members, "skipped": skipped}


def _print_summary(report: Dict) -> None:
    for src, r in report["sources"].items():
        s = r["summary"]
        print("\n" + "=" * 70)
        print(f"  FIDELITY REPORT — {src.upper()} DOCS")
        print("=" * 70)
        if s.get("error"):
            print(f"  ERROR: {s['error']}")
            continue
        print(f"  Evaluated            : {s['evaluated']}  (skipped {s['skipped']})")
        print(f"  Mean grounding       : {s['mean_grounding']}")
        print(f"  Mean source coverage : {s['mean_coverage']}")
        print(f"  Omission-scope skipped: {s.get('omission_skipped_members', 0)}")
        print(f"  Additive issues      : {s['total_additive_issues']}")
        print(f"  Omissions            : {s['total_omissions']}")
        print(f"  Empty extractions    : {s.get('empty_extractions', 0)}")
        print(f"  Fully faithful       : {s['members_fully_faithful']}/{s['evaluated']}")
        print("=" * 70)
        
        sb = s.get("section_breakdown") or {}
        for kind in ("additive", "omission"):
            counts = sb.get(kind) or {}
            if counts:
                print(f"  {kind} issues by section:")
                for sec, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True):
                    print(f"      {sec:<28} {n}")
        print("-" * 70)
        
        omit_skipped = s.get("omission_skipped_api_names") or []
        if omit_skipped:
            print(f"  omission-scope skipped ({len(omit_skipped)}) — signature not in first "
                  f"{ '20' } source lines:")
            for name in omit_skipped:
                print(f"      {name}")
        
        empty_names = s.get("empty_extraction_api_names") or []
        if empty_names:
            print(f"  empty extractions ({len(empty_names)}) — all content fields 'N/A' / missing:")
            for name in empty_names:
                print(f"      {name}")
        
        fully_skipped = r.get("skipped") or []
        if fully_skipped:
            print(f"  fully skipped ({len(fully_skipped)}):")
            for sk in fully_skipped:
                print(f"      {sk['api_name']}  ({sk['reason']})")
        print("-" * 70)
        
        worst = sorted(r["members"],
                       key=lambda m: m["additive_issue_count"] + m["omission_count"],
                       reverse=True)[:10]
        for m in worst:
            if m["additive_issue_count"] or m["omission_count"]:
                print(f"  {m['api_name']}  ({m['type']})  "
                      f"add={m['additive_issue_count']} omit={m['omission_count']} "
                      f"ground={m['grounding_score']} cov={m['source_coverage_ratio']}")
    print()


_CSV_COLUMNS = [
    "api_name", "source", "member_type", "kind", "issue_type", "category",
    "severity", "similarity", "section", "json_path",
    "source_file", "line_start", "line_end", "char_start", "char_end",
    "doc_value", "code_value"
]


def _flat(s) -> str:
    """Collapse newlines/runs of whitespace so snippets stay on one CSV cell."""
    return "" if s is None else " ".join(str(s).split())


def _rows_for_member(member: Dict, source: str):
    for kind, issues in (("additive", member["additive_issues"]),
                         ("omission", member["omissions"]),
                         ("empty", member.get("empty_issues", []))):
        for it in issues:
            loc = it.get("source_location") or {}
            yield {
                "api_name": member["api_name"],
                "source": source,
                "member_type": member["type"],
                "kind": kind,
                "issue_type": it["issue_type"],
                "category": it.get("category"),
                "severity": it.get("severity"),
                "similarity": it.get("similarity"),
                "section": it.get("section"),
                "json_path": it.get("json_path"),
                "source_file": loc.get("file"),
                "line_start": loc.get("line_start"),
                "line_end": loc.get("line_end"),
                "char_start": loc.get("char_start"),
                "char_end": loc.get("char_end"),
                "doc_value": _flat(it.get("doc_value")),
                "code_value": _flat(it.get("code_value"))
            }


def _write_csv(report: Dict, path: str) -> int:
    """Flatten every flagged span (additions + omissions) across sources into one CSV for manual triage, worst (lowest similarity) first."""
    rows = []
    for source, r in report["sources"].items():
        for m in r.get("members", []):
            rows.extend(_rows_for_member(m, source))
    rows.sort(key=lambda row: row["similarity"] if row["similarity"] is not None else 0.0)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _parse_args():
    p = argparse.ArgumentParser(description="Check structured docs against preprocessed sources via doc_quality fidelity.")
    p.add_argument("--db-path", required=True, help="MapCoDoc SQLite DB for authoritative member types.")
    p.add_argument("--library-name", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--sources", nargs="+", choices=["web", "pdf"], default=["web", "pdf"])
    p.add_argument("--config", default=None, help="Optional EvaluatorConfig YAML (else defaults).")
    p.add_argument("--limit", type=int, default=None, help="Evaluate only the first N members per source.")
    p.add_argument("--report-file", default=None)
    p.add_argument("--csv-file", default=None, help="Write a flat CSV of all flagged spans for manual triage.")
    return p.parse_args()


if __name__ == "__main__":
    
    args = _parse_args()
    cfg = EvaluatorConfig.from_yaml(Path(args.config)) if args.config else EvaluatorConfig()
    
    db = MapCoDocDB(args.db_path)
    qm = QueryManager(db.get_session())

    report = {
        "library": args.library_name,
        "version": args.version,
        "thresholds": {
            "fidelity_exact_min": cfg.fidelity_exact_min,
            "fidelity_partial_min": cfg.fidelity_partial_min,
            "fidelity_min_unit_chars": cfg.fidelity_min_unit_chars,
            "fidelity_omission_exact_min": cfg.fidelity_omission_exact_min,
            "fidelity_omission_partial_min": cfg.fidelity_omission_partial_min
        },
        "sources": {}
    }
    for source in args.sources:
        report["sources"][source] = evaluate_source(args.library_name, args.version, source, cfg, qm, args.limit)

    _print_summary(report)
    if args.report_file:
        Path(args.report_file).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Report saved to: {args.report_file}")
        
    if args.csv_file:
        n = _write_csv(report, args.csv_file)
        print(f"CSV written: {args.csv_file} ({n} flagged spans)")
