"""
Collect & Group Retrieved Docs per Member
=========================================
For each API name in a sample .txt file, find the retrieved web and PDF docs
that belong to the SAME database member (even if the doc file is named with an
alias API path), and group them into:

    <output-dir>/<sample_api_name>/web_doc/<matching files>
    <output-dir>/<sample_api_name>/pdf_doc/<matching files>

Member identity is established by resolving BOTH the sample API name and each
doc filename through the database and comparing the unique member id
(DBMember.id / DBInheritedMember.id), not by string/signature matching.

Inputs:
    --db-path       Path to the MapCoDoc SQLite database
    --web-doc-dir   Folder of web-retrieved docs   (files named <api_name>.txt)
    --pdf-doc-dir   Folder of pdf-retrieved docs    (files named <api_name>.txt)
    --names-file    Sample API names, one per line
    --output-dir    Destination root for grouped docs

Usage:
    python tests/collect_member_docs.py \\
        --db-path mapcodoc_output/doc_test/torch_2.9.1.db \\
        --web-doc-dir doc_processor/doc_artifacts/scraped_doc/torch/webDoc_v_2.9.1/per_member \\
        --pdf-doc-dir doc_processor/doc_artifacts/scraped_doc/torch/pdfDoc_v_2.9.1/per_member \\
        --names-file "C:/.../RQ2/api_names/torch.txt" \\
        --output-dir tests/collected_docs/torch \\
        --report-file tests/torch_doc_collection.json
"""

from pathlib import Path
import argparse
import json
import shutil
import sys

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]  # project root
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mapcodoc_db.db_manager import MapCoDocDB
from mapcodoc_db.query import QueryManager
from mapcodoc_db.db_models import DBMember


# ---------------------------------------------------------------------------
# Member resolution (identity = unique DB id)
# ---------------------------------------------------------------------------

def find_db_member(session, api_name: str):
    """Locate a DBMember by primary_api_name OR membership in all_api_names."""
    member = (
        session.query(DBMember)
        .filter(DBMember.primary_api_name == api_name)
        .first()
    )
    if member:
        return member

    # NOTE: qualify 'members.all_api_names' to avoid ambiguity if joins are added.
    return (
        session.query(DBMember)
        .filter(
            text(
                "EXISTS (SELECT 1 FROM json_each(members.all_api_names) "
                "WHERE json_each.value = :n)"
            )
        )
        .params(n=api_name)
        .first()
    )


def resolve_member_key(session, qm: QueryManager, api_name: str, cache: dict):
    """
    Resolve an API name to a stable identity key:
        'member:<id>'    for a direct member
        'inherited:<id>' for an inherited member record
        None             if it cannot be resolved
    Cached by api_name to avoid repeated DB hits.
    """
    if api_name in cache:
        return cache[api_name]

    member = find_db_member(session, api_name)
    if member is not None:
        key = f"member:{member.id}"
    else:
        inh = qm.get_inherited_member_by_api_name(api_name)
        key = f"inherited:{inh.id}" if inh else None

    cache[api_name] = key
    return key


# ---------------------------------------------------------------------------
# Doc folder indexing
# ---------------------------------------------------------------------------

def _doc_api_name(path: Path) -> str:
    """File stem as an API name (strip a trailing '.txt'; keep dotted name intact)."""
    return path.name[:-4] if path.name.endswith(".txt") else path.stem


def build_doc_index(folder: Path, session, qm, cache: dict, recursive: bool):
    """
    Map member identity key -> list of doc files in `folder`.
    Returns (index, unresolved) where unresolved lists files whose API name
    could not be matched to any DB member.
    """
    index, unresolved = {}, []
    if not folder or not folder.exists():
        return index, unresolved

    files = sorted(folder.rglob("*.txt") if recursive else folder.glob("*.txt"))
    for f in files:
        api = _doc_api_name(f)
        key = resolve_member_key(session, qm, api, cache)
        if key:
            index.setdefault(key, []).append(f)
        else:
            unresolved.append(str(f))
    return index, unresolved


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_api_names(path: str) -> list:
    seen, names = set(), []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def sanitize_dirname(name: str) -> str:
    """Make an API name safe as a single folder name (dots are fine on Windows)."""
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad else c for c in name)


def copy_into(files, dest_dir: Path) -> list:
    """Copy files into dest_dir, avoiding basename collisions. Returns copied names."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for src in files:
        target = dest_dir / src.name
        n = 1
        while target.exists():
            target = dest_dir / f"{src.stem}__{n}{src.suffix}"
            n += 1
        shutil.copy2(src, target)
        copied.append(target.name)
    return copied


# ---------------------------------------------------------------------------
# Main collection
# ---------------------------------------------------------------------------

def collect(db_path, web_dir, pdf_dir, names_file, output_dir,
            recursive=True, report_file=None):
    db = MapCoDocDB(db_path)
    session = db.get_session()
    qm = QueryManager(session)
    cache = {}

    try:
        web_dir = Path(web_dir) if web_dir else None
        pdf_dir = Path(pdf_dir) if pdf_dir else None
        out_root = Path(output_dir)
        out_root.mkdir(parents=True, exist_ok=True)

        web_index, web_unresolved = build_doc_index(web_dir, session, qm, cache, recursive)
        pdf_index, pdf_unresolved = build_doc_index(pdf_dir, session, qm, cache, recursive)

        # Fallback filename sets (used when a sample name can't be resolved in DB)
        web_by_name = {_doc_api_name(f): f for f in (web_dir.rglob("*.txt") if (web_dir and web_dir.exists() and recursive) else (web_dir.glob("*.txt") if web_dir and web_dir.exists() else []))}
        pdf_by_name = {_doc_api_name(f): f for f in (pdf_dir.rglob("*.txt") if (pdf_dir and pdf_dir.exists() and recursive) else (pdf_dir.glob("*.txt") if pdf_dir and pdf_dir.exists() else []))}

        api_names = load_api_names(names_file)
        results = []
        key_to_samples = {}  # detect multiple sample names hitting the same member

        for sample in api_names:
            key = resolve_member_key(session, qm, sample, cache)

            if key:
                web_matches = list(web_index.get(key, []))
                pdf_matches = list(pdf_index.get(key, []))
                match_mode = "db_identity"
            else:
                # Could not resolve in DB -> fall back to exact filename match.
                web_matches = [web_by_name[sample]] if sample in web_by_name else []
                pdf_matches = [pdf_by_name[sample]] if sample in pdf_by_name else []
                match_mode = "filename_fallback"

            member_dir = out_root / sanitize_dirname(sample)
            web_copied = copy_into(web_matches, member_dir / "web_doc") if web_matches else []
            pdf_copied = copy_into(pdf_matches, member_dir / "pdf_doc") if pdf_matches else []

            if key:
                key_to_samples.setdefault(key, []).append(sample)

            results.append({
                "sample_api_name": sample,
                "member_key": key,
                "match_mode": match_mode,
                "web_doc_count": len(web_copied),
                "pdf_doc_count": len(pdf_copied),
                "web_doc_files": [str(p) for p in web_matches],
                "pdf_doc_files": [str(p) for p in pdf_matches],
                "output_dir": str(member_dir),
            })

        # Sample names that collapse to the same DB member (alias duplicates)
        alias_groups = {k: v for k, v in key_to_samples.items() if len(v) > 1}

        report = {
            "db_path": db_path,
            "total_samples": len(results),
            "with_web_doc": sum(1 for r in results if r["web_doc_count"]),
            "with_pdf_doc": sum(1 for r in results if r["pdf_doc_count"]),
            "with_no_docs": sum(1 for r in results if not r["web_doc_count"] and not r["pdf_doc_count"]),
            "unresolved_samples": [r["sample_api_name"] for r in results if r["member_key"] is None],
            "alias_groups": alias_groups,
            "web_docs_unresolved_to_member": web_unresolved,
            "pdf_docs_unresolved_to_member": pdf_unresolved,
            "members": results,
        }

        _print_summary(report)

        if report_file:
            Path(report_file).write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"\nReport written to: {report_file}")

        return report
    finally:
        session.close()


def _print_summary(report: dict) -> None:
    print("\n" + "=" * 70)
    print("  MEMBER DOC COLLECTION")
    print("=" * 70)
    print(f"  DB             : {report['db_path']}")
    print(f"  Sample members : {report['total_samples']}")
    print(f"  With web doc   : {report['with_web_doc']}")
    print(f"  With pdf doc   : {report['with_pdf_doc']}")
    print(f"  With NO docs   : {report['with_no_docs']}")

    if report["unresolved_samples"]:
        print("\n  SAMPLE NAMES NOT RESOLVED IN DB (used filename fallback):")
        for n in report["unresolved_samples"]:
            print(f"    - {n}")

    if report["alias_groups"]:
        print("\n  ALIAS GROUPS (multiple sample names -> same member):")
        for key, names in report["alias_groups"].items():
            print(f"    {key}: {names}")

    nodoc = [r["sample_api_name"] for r in report["members"]
             if not r["web_doc_count"] and not r["pdf_doc_count"]]
    if nodoc:
        print("\n  MEMBERS WITH NO DOCS FOUND:")
        for n in nodoc:
            print(f"    - {n}")
    print()


def _parse_args():
    p = argparse.ArgumentParser(description="Collect and group retrieved web/pdf docs per member.")
    p.add_argument("--db-path", required=True)
    p.add_argument("--web-doc-dir", required=True)
    p.add_argument("--pdf-doc-dir", required=True)
    p.add_argument("--names-file", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--no-recursive", action="store_true", help="Only scan the top level of the doc folders (default: recursive)")
    p.add_argument("--report-file", default=None)
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    collect(
        db_path=args.db_path,
        web_dir=args.web_doc_dir,
        pdf_dir=args.pdf_doc_dir,
        names_file=args.names_file,
        output_dir=args.output_dir,
        recursive=not args.no_recursive,
        report_file=args.report_file,
    )
