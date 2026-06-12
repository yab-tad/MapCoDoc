"""
API Implementation Audit
========================
Given a list of API names, classify each by whether it has a genuine Python
implementation (a real def/class body in a .py file) versus a re-exported
native object (C/C++/Cython), an alias/variable binding, an inherited member,
or simply not present in the database.

Because an input API path may differ from the path MapCoDoc traced
(e.g. 'pandas.core.groupby.SeriesGroupBy.value_counts' vs the traced
'pandas.core.groupby.generic.SeriesGroupBy.value_counts'), a fallback
short-name / ClassName.method resolution stage flags such cases for manual
review instead of discarding them.
"""

from pathlib import Path
import argparse
import json
import sys

from sqlalchemy import text
from sqlalchemy.orm import joinedload

ROOT = Path(__file__).resolve().parents[1]  # project root
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mapcodoc_db.db_manager import MapCoDocDB
from mapcodoc_db.query import QueryManager
from mapcodoc_db.db_models import DBMember


# Member types that correspond to an actual Python definition.
PY_DEF_TYPES = {"class", "function", "method"}

# A member qualifies only when a Python code body is verified in this database
KEEP_CATEGORIES = {"python_impl", "inherited_internal_python"}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def find_db_member(session, api_name: str):
    """
    Locate a DBMember whose primary_api_name OR all_api_names contains api_name.
    Returns the ORM object so we can inspect source_code, line numbers, and the
    defining module path.
    """
    member = (
        session.query(DBMember)
        .filter(DBMember.primary_api_name == api_name)
        .options(joinedload(DBMember.module), joinedload(DBMember.parent))
        .first()
    )
    if member:
        return member

    return (
        session.query(DBMember)
        .filter(
            text(
                "EXISTS (SELECT 1 FROM json_each(members.all_api_names) "
                "WHERE json_each.value = :n)"
            )
        )
        .params(n=api_name)
        .options(joinedload(DBMember.module), joinedload(DBMember.parent))
        .first()
    )


def _member_api_names(member: DBMember) -> list:
    names = list(member.all_api_names or [])
    if member.primary_api_name:
        names.append(member.primary_api_name)
    names.append(member.fully_qualified_name)
    return names


def resolve_by_short_name(session, api_name: str):
    """
    Fallback resolution when exact/inherited lookups fail.

    - class/function : match DBMember.name == <last segment>
    - method         : match DBMember.name == <last segment>
                       AND parent class name == <second-to-last segment>

    Each candidate is verified by suffix: the DB FQN or one of its API names
    must end with the input's trailing 'Parent.short' (or 'short'). This filters
    out unrelated members that merely share a short name.

    Returns a list of (DBMember, match_kind) tuples.
    """
    parts = api_name.split(".")
    short = parts[-1]
    parent_short = parts[-2] if len(parts) >= 2 else None
    suffix = ".".join(parts[-2:]) if len(parts) >= 2 else short

    rows = (
        session.query(DBMember)
        .filter(DBMember.name == short)
        .options(joinedload(DBMember.module), joinedload(DBMember.parent))
        .all()
    )

    candidates, seen = [], set()
    for m in rows:
        if m.id in seen:
            continue

        match_kind = None
        if m.member_type in ("class", "function"):
            match_kind = "short_name"
        elif (
            m.member_type == "method"
            and m.parent is not None
            and parent_short is not None
            and m.parent.name == parent_short
        ):
            match_kind = "class.method"

        if not match_kind:
            continue

        # Suffix verification against FQN and any traced API name.
        paths = _member_api_names(m)
        if any(p.endswith(suffix) or p.endswith(short) for p in paths):
            candidates.append((m, match_kind))
            seen.add(m.id)

    return candidates


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def looks_like_python_body(member: DBMember) -> bool:
    """A real Python def/class has a non-empty body and a start line."""
    body = (member.source_code or "").strip()
    if not body or member.source_start_line is None:
        return False

    if member.member_type == "class":
        return body.startswith("class ")
    if member.member_type in ("function", "method"):
        return body.startswith("def ") or body.startswith("async def ") or body.startswith("@")
    return False


def classify_member(member: DBMember) -> dict:
    """Classify a resolved DBMember row into an implementation category."""
    file_path = (member.module.file_path or "") if member.module else ""
    is_py_file = file_path.endswith(".py")
    is_stub = file_path.endswith(".pyi")
    has_py_body = looks_like_python_body(member)

    if member.member_type in PY_DEF_TYPES and has_py_body and is_py_file:
        category = "python_impl"
    elif is_stub:
        category = "stub_only"
    elif member.member_type == "variable":
        category = "variable_or_alias"
    else:
        category = "present_no_python_body"

    return {
        "category": category,
        "member_type": member.member_type,
        "fqn": member.fully_qualified_name,
        "primary_api_name": member.primary_api_name,
        "parent_fqn": member.parent.fully_qualified_name if member.parent else None,
        "file_path": file_path,
        "has_py_body": has_py_body
    }


def classify(session, qm: QueryManager, api_name: str) -> dict:
    """
    Classify a single API name.

    Categories:
      python_impl               - real def/class body in a .py file  (KEEP)
      present_no_python_body    - DB row exists but no real .py definition
      variable_or_alias         - assignment binding (often to native code)
      stub_only                 - defined in a .pyi stub (signature, no impl)
      inherited_internal_python - inherited; original member's body verified in DB  (KEEP)
      inherited_internal_no_body- inherited; original row exists but no Python body
      inherited_no_body_in_db   - inherited; no original member row in this DB (external sources like sklearn mixins, torch._C)
      needs_manual_review       - only resolvable via short-name / ClassName.method
      not_found                 - no member, no inherited record, no short-name hit
    """
    # 1. Exact match on traced API names.
    member = find_db_member(session, api_name)
    if member is not None:
        info = classify_member(member)
        info["api_name"] = api_name
        info["resolved_via"] = "exact"
        info["member_key"] = f"member:{member.id}"   # unique, stable identity
        return info

    # 2. Inherited member paths.
    inh = qm.get_inherited_member_by_api_name(api_name)
    if inh:
        # --- Verify the ORIGINAL member's code body in this database ---
        if inh.original_member_id:
            orig_db = session.get(DBMember, inh.original_member_id)
            if orig_db is not None:
                info = classify_member(orig_db)  # same body + .py checks as direct members
                return {
                    "api_name": api_name,
                    "category": (
                        "inherited_internal_python"
                        if info["category"] == "python_impl"
                        else "inherited_internal_no_body"
                    ),
                    "member_type": inh.member_type,
                    "fqn": inh.original_fqn or inh.inherited_api_name,
                    "original_fqn": orig_db.fully_qualified_name,
                    "file_path": info["file_path"],
                    "resolved_via": "inherited",
                    "is_external": False,
                    "member_key": f"inherited:{inh.id}"
                }
        # External (or unlinked) inherited member: no DBMember row exists in this database, so no code body can be verified -> does NOT qualify.
        return {
            "api_name": api_name,
            "category": "inherited_no_body_in_db",
            "member_type": inh.member_type,
            "fqn": inh.original_fqn or inh.inherited_api_name,
            "source_class_fqn": inh.source_class_fqn,
            "file_path": None,
            "resolved_via": "inherited",
            "is_external": inh.is_external,
            "member_key": f"inherited:{inh.id}"
        }

    # 3. Fallback: short-name / ClassName.method resolution (manual review).
    candidates = resolve_by_short_name(session, api_name)
    if candidates:
        cand_dicts = []
        for m, match_kind in candidates:
            c = classify_member(m)
            c["match_kind"] = match_kind
            cand_dicts.append(c)
        return {
            "api_name": api_name,
            "category": "needs_manual_review",
            "resolved_via": "short_name",
            "candidate_count": len(cand_dicts),
            "candidates": cand_dicts,
            "member_key": None  # ambiguous -> never auto-dedup
        }

    # 4. Genuinely unresolved.
    return {
        "api_name": api_name,
        "category": "not_found",
        "resolved_via": None,
        "recheck": double_check_not_found(qm, api_name),
        "member_key": None
    }


def double_check_not_found(qm: QueryManager, api_name: str) -> dict:
    """Distinguish a genuine absence from a residual coverage gap."""
    via_any_path = qm.find_member_by_any_path(api_name)
    if via_any_path:
        return {"resolved": True, "how": via_any_path["type"]}

    short = api_name.split(".")[-1]
    candidates = qm.search_members(short, limit=5)
    return {"resolved": False, "short_name_candidates": [c.fqn for c in candidates] if candidates else []}


#-------------------------------------------------------------------------
# deduplication helpers
#-------------------------------------------------------------------------

def _api_sort_key(name: str):
    """Fewer dotted segments first; ties broken by length, then lexicographic."""
    return (name.count("."), len(name), name)


def apply_dedup(results: list) -> list:
    """
    Group results that resolve to the SAME member (same member_key) and keep only the shortest API name; mark the rest as duplicates.

    Identity is the resolved DB row (member.id / inherited.id), NOT signatures.
    """
    groups = {}
    for r in results:
        key = r.get("member_key")
        if key:
            groups.setdefault(key, []).append(r)

    for group in groups.values():
        primary = min(group, key=lambda r: _api_sort_key(r["api_name"]))
        for r in group:
            r["is_duplicate"] = (r is not primary)
            if r is not primary:
                r["duplicate_of"] = primary["api_name"]
                r["duplicate_aliases"] = sorted(g["api_name"] for g in group if g is not primary)
            else:
                r["duplicate_aliases"] = sorted(g["api_name"] for g in group if g is not primary)

    for r in results:                # ensure the flag exists everywhere
        r.setdefault("is_duplicate", False)
    return results


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def load_api_names(path: str) -> list:
    """Read API names, one per line; skip blanks; de-duplicate (order-preserving)."""
    seen, names = set(), []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def run_audit(db_path: str, names_file: str, report_file: str = None) -> dict:
    db = MapCoDocDB(db_path)
    session = db.get_session()
    qm = QueryManager(session)

    try:
        api_names = load_api_names(names_file)
        results = [classify(session, qm, name) for name in api_names]
        results = apply_dedup(results)

        counts = {}
        for r in results:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
        
        keep = []
        duplicates = []
        manual = []
        to_exclude = []
        for r in results:
            if r["category"] in KEEP_CATEGORIES and not r["is_duplicate"]:
                keep.append(r["api_name"])
            elif r["is_duplicate"]:
                duplicates.append(r)
            elif r["category"] == "needs_manual_review":
                manual.append(r)
            else:
                to_exclude.append(r["api_name"])

        report = {
            "db_path": db_path,
            "total": len(results),
            "counts": counts,
            "keep_count": len(keep),
            "duplicate_count": len(duplicates),
            "keep": keep,
            "duplicates": duplicates,
            "needs_manual_review": manual,
            "exclude": to_exclude,
            "members": results
        }

        _print_summary(report)

        if report_file:
            Path(report_file).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"\nFull report written to: {report_file}")

        return report
    finally:
        session.close()


def _print_summary(report: dict) -> None:
    print("\n" + "=" * 70)
    print("  API IMPLEMENTATION AUDIT")
    print("=" * 70)
    print(f"  DB        : {report['db_path']}")
    print(f"  Total     : {report['total']}")
    print(f"  Keep      : {report['keep_count']}  (categories: {sorted(KEEP_CATEGORIES)})")

    print("\n  CATEGORY BREAKDOWN:")
    for cat in sorted(report["counts"], key=lambda c: -report["counts"][c]):
        print(f"    {cat:24s}: {report['counts'][cat]}")

    manual = report["needs_manual_review"]
    if manual:
        print("\n  NEEDS MANUAL REVIEW (input path != traced path):")
        for r in manual:
            print(f"    {r['api_name']}  ->  {r['candidate_count']} candidate(s):")
            for c in r["candidates"]:
                print(f"        [{c['category']:22s}] ({c['match_kind']}) {c['fqn']}  <{c['member_type']}>")

    nf = [r for r in report["members"] if r["category"] == "not_found"]
    if nf:
        print("\n  NOT FOUND:")
        for r in nf:
            extra = ""
            if r.get("recheck", {}).get("resolved"):
                extra = f"  (resolvable via {r['recheck']['how']} -> review!)"
            print(f"    {r['api_name']}{extra}")
            
    inh_excluded = [r for r in report["members"] if r["category"] in ("inherited_no_body_in_db", "inherited_internal_no_body")]
    if inh_excluded:
        print("\n  INHERITED EXCLUDED (no verifiable code body in DB):")
        for r in inh_excluded:
            src = r.get("source_class_fqn") or r.get("fqn") or "?"
            print(f"    [{r['category']:26s}] {r['api_name']}  <- {src}")
            
    dups = [r for r in report["members"] if r.get("is_duplicate")]
    if dups:
        print("\n  ALIAS DUPLICATES (dropped, kept shorter name):")
        for r in dups:
            print(f"    {r['api_name']}  ->  kept '{r['duplicate_of']}'")
            
    print()


def _parse_args():
    p = argparse.ArgumentParser(description="Audit API names for Python implementations.")
    p.add_argument("--db-path", required=True, help="Path to the MapCoDoc SQLite DB")
    p.add_argument("--names-file", required=True, help="Text file: one API name per line")
    p.add_argument("--report-file", default=None, help="Optional JSON report output path")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_audit(args.db_path, args.names_file, args.report_file)