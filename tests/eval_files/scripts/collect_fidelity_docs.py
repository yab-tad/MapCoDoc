"""
Collect Structured/Preprocessed Doc Pairs by Fidelity Outcome
=============================================================
Driven by the sample API-names list (authoritative universe), this scans the
fidelity report(s) and copies each member's PREPROCESSED (.txt) and STRUCTURED
(.json) docs into a bucket per (library, source):

    clean        additive==0 and omission==0 and not empty
    empty        empty_extraction == True
    issues       any additive or omission issue
    unevaluated  no preprocessed doc, OR preprocessed but no structured doc,
                 OR present but absent from every report

Layout:
    <out>/<library>/<source>/<bucket>/<stem>/<stem>.txt
    <out>/<library>/<source>/<bucket>/<stem>/<stem>.json

`stem` is the on-disk artifact stem (may carry a '-class'/'-function' suffix for
case-colliding names like Stream vs stream), so members never clobber each other.

Usage:
    python tests/collect_fidelity_docs.py \\
        --db-path requests_2.32.5.db \\
        --library-name requests --version 2.32.5 \\
        --sources web pdf \\
        --names-file path/to/api_names/requests.txt \\
        --report-file tests/requests_fidelity.json \\
        --output-dir tests/collected_fidelity/requests \\
        --manifest-file tests/requests_fidelity_collection.csv \\
        --overwrite
"""

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mapcodoc_db.db_manager import MapCoDocDB
from mapcodoc_db.query import QueryManager
from doc_processor.file_doc.extraction_utils import (build_shared_name_set, to_artifact_stem, _MEMBER_TYPE_SUFFIXES)

ARTIFACTS_BASE = PROJECT_ROOT / "doc_processor" / "doc_artifacts"

_MANIFEST_COLUMNS = [
    "library", "source", "sample_api_name", "stem", "member_type", "bucket",
    "reason", "grounding_score", "additive_issue_count", "omission_count",
    "empty_extraction", "preprocessed_present", "structured_present",
]


# ---------------------------------------------------------------------------
# Paths & identity
# ---------------------------------------------------------------------------

def _dirs_for(library: str, version: str, source: str):
    lib_ver = f"{library}/v_{version}_{source}"
    structured = ARTIFACTS_BASE / "structured_doc" / lib_ver
    preprocessed = ARTIFACTS_BASE / "preprocessed_doc" / lib_ver / "doc"
    return structured, preprocessed


def load_api_names(path: str) -> List[str]:
    seen, names = set(), []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _member_type_for(qm: QueryManager, api_name: str) -> Optional[str]:
    """Member type used only to build the artifact stem suffix. None if unknown."""
    m = qm.get_member_by_any_api_name(api_name)
    if m and m.type:
        return m.type
    inh = qm.get_inherited_member_by_api_name(api_name)
    if inh and inh.member_type:
        return inh.member_type
    return None


def _candidate_stems(api_name: str, member_type: Optional[str], shared: set) -> List[str]:
    """Stems to probe on disk, most-likely first. Robust to differences between the pipeline's shared-name set and this run's, by also trying the bare name
    and every '-<type>' variant."""
    cands = [to_artifact_stem(api_name, member_type, shared),   # expected
             to_artifact_stem(api_name, None, set())]           # bare
    for t in _MEMBER_TYPE_SUFFIXES:
        cands.append(to_artifact_stem(api_name, t, {api_name}))  # forced '-t'
    out, seen = [], set()
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _resolve_stem(api_name, member_type, shared, structured_dir: Path, preprocessed_dir: Path):
    """Pick the stem whose .txt or .json actually exists; else the expected stem."""
    cands = _candidate_stems(api_name, member_type, shared)
    for stem in cands:
        if (structured_dir / f"{stem}.json").exists() or (preprocessed_dir / f"{stem}.txt").exists():
            return stem
    return cands[0]


# ---------------------------------------------------------------------------
# Report indexing
# ---------------------------------------------------------------------------

def load_reports(report_files: List[str]) -> Dict[str, Dict[str, dict]]:
    """source -> {'members': {stem: member}, 'skipped': {stem: reason}} merged across all report files."""
    reports: Dict[str, Dict[str, dict]] = {}
    for rf in report_files:
        data = json.loads(Path(rf).read_text(encoding="utf-8"))
        for src, sec in (data.get("sources") or {}).items():
            bucket = reports.setdefault(src, {"members": {}, "skipped": {}})
            for m in sec.get("members", []) or []:
                bucket["members"][m["api_name"]] = m
            for sk in sec.get("skipped", []) or []:
                bucket["skipped"][sk["api_name"]] = sk.get("reason", "skipped")
    return reports


def _bucket_for(rec: dict) -> str:
    if rec.get("empty_extraction"):
        return "empty"
    if (rec.get("additive_issue_count", 0) == 0) and (rec.get("omission_count", 0) == 0):
        return "clean"
    return "issues"


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

def _copy_pair(out_root: Path, library: str, source: str, bucket: str, stem: str, pre_path: Optional[Path], struct_path: Optional[Path]) -> None:
    dest = out_root / library / source / bucket
    dest.mkdir(parents=True, exist_ok=True)
    if pre_path and pre_path.exists():
        shutil.copy2(pre_path, dest / f"{stem}.txt")
    if struct_path and struct_path.exists():
        shutil.copy2(struct_path, dest / f"{stem}.json")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def collect(db_path, library, version, sources, names_file, report_files, output_dir, manifest_file=None, overwrite=False) -> List[dict]:
    db = MapCoDocDB(db_path)
    session = db.get_session()
    qm = QueryManager(session)
    try:
        out_root = Path(output_dir)
        if overwrite and out_root.exists():
            shutil.rmtree(out_root)
        out_root.mkdir(parents=True, exist_ok=True)

        names = load_api_names(names_file)
        shared = build_shared_name_set(names)
        reports = load_reports(report_files)

        rows: List[dict] = []
        for source in sources:
            structured_dir, preprocessed_dir = _dirs_for(library, version, source)
            rep = reports.get(source, {"members": {}, "skipped": {}})

            for sample in names:
                member_type = _member_type_for(qm, sample)
                stem = _resolve_stem(sample, member_type, shared, structured_dir, preprocessed_dir)
                pre_path = preprocessed_dir / f"{stem}.txt"
                struct_path = structured_dir / f"{stem}.json"
                pre_ok, struct_ok = pre_path.exists(), struct_path.exists()

                rec, bucket, reason = None, None, ""
                if not pre_ok:
                    bucket, reason = "unevaluated", "no_preprocessed_doc"
                elif not struct_ok:
                    bucket, reason = "unevaluated", "no_structured_doc"
                else:
                    rec = rep["members"].get(stem)
                    if rec is not None:
                        bucket = _bucket_for(rec)
                        reason = bucket
                    elif stem in rep["skipped"]:
                        bucket, reason = "unevaluated", rep["skipped"][stem]
                    else:
                        bucket, reason = "unevaluated", "not_in_report"

                _copy_pair(out_root, library, source, bucket, stem, pre_path if pre_ok else None, struct_path if struct_ok else None)

                rows.append({
                    "library": library,
                    "source": source,
                    "sample_api_name": sample,
                    "stem": stem,
                    "member_type": member_type or "",
                    "bucket": bucket,
                    "reason": reason,
                    "grounding_score": (rec or {}).get("grounding_score", ""),
                    "additive_issue_count": (rec or {}).get("additive_issue_count", ""),
                    "omission_count": (rec or {}).get("omission_count", ""),
                    "empty_extraction": (rec or {}).get("empty_extraction", ""),
                    "preprocessed_present": pre_ok,
                    "structured_present": struct_ok
                })

        if manifest_file:
            with open(manifest_file, "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=_MANIFEST_COLUMNS)
                w.writeheader()
                w.writerows(rows)

        _print_summary(rows, out_root, manifest_file)
        return rows
    finally:
        session.close()


def _print_summary(rows, out_root, manifest_file) -> None:
    sources = sorted({r["source"] for r in rows})
    print("\n" + "=" * 70)
    print("  FIDELITY DOC COLLECTION")
    print("=" * 70)
    for source in sources:
        sub = [r for r in rows if r["source"] == source]
        print(f"\n  [{source}]  {len(sub)} sample members")
        for bucket in ("clean", "empty", "issues", "unevaluated"):
            n = sum(1 for r in sub if r["bucket"] == bucket)
            print(f"      {bucket:<12} {n}")
        reasons = {}
        for r in sub:
            if r["bucket"] == "unevaluated":
                reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        for reason, n in sorted(reasons.items(), key=lambda kv: kv[1], reverse=True):
            print(f"          - {reason}: {n}")
    print("-" * 70)
    print(f"  Output : {out_root}")
    if manifest_file:
        print(f"  Manifest: {manifest_file}")
    print()


def _parse_args():
    p = argparse.ArgumentParser(description="Collect structured/preprocessed doc pairs bucketed by fidelity outcome.")
    p.add_argument("--db-path", required=True, help="MapCoDoc SQLite DB (for authoritative member type / stem suffix).")
    p.add_argument("--library-name", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--sources", nargs="+", choices=["web", "pdf"], default=["web", "pdf"])
    p.add_argument("--names-file", required=True, help="Sample API names, one per line (the authoritative universe).")
    p.add_argument("--report-file", nargs="+", required=True, help="One or more fidelity report JSONs (merged by their 'sources' key).")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--manifest-file", default=None, help="Optional CSV manifest of every (sample, source).")
    p.add_argument("--overwrite", action="store_true", help="Clear the output dir first.")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    collect(
        db_path=args.db_path,
        library=args.library_name,
        version=args.version,
        sources=args.sources,
        names_file=args.names_file,
        report_files=args.report_file,
        output_dir=args.output_dir,
        manifest_file=args.manifest_file,
        overwrite=args.overwrite
    )
