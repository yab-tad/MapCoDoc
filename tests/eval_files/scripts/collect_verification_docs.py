"""
Group every member's docs for manual verification, organized by verify_tier.

Layout (beside collected_fidelity):
  collected_verification/<library>/<source>/<verify_tier>/
      <stem>.structured.json   - the structured doc (LLM output)
      <stem>.preprocessed.txt  - the retrieved/preprocessed doc (pipeline input to structuring)
      <stem>.original.txt      - the WEB original reference used to judge accuracy
      <stem>.eval.txt          - verdict + evidence + comment + DB docstring + original summary

Also writes, per (library, source), an _index.csv, and a single consolidated
tests/verification_worklist.csv (rows with sample_pick in {all, yes}).

Reuses the already-collected doc pairs in collected_fidelity and the cached web
originals in tests/webverify/.  Does NOT touch doc_artifacts.
"""
import csv, json, os, shutil, sys, glob, collections

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import web_verify as V
import web_verify_plan as W
from audit_ground_truth import clean_rst
from mapcodoc_db.db_manager import MapCoDocDB
from mapcodoc_db.query import QueryManager

DEFAULT_OUT = os.path.join(os.path.dirname(V.COLLECT_BASE), "collected_verification")
# Output base is provided as input (first CLI arg); falls back to the default beside
# collected_fidelity. e.g.:  python collect_verification_docs.py "D:\path\to\output"
OUT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
LIBS = ["requests", "numpy", "pandas", "sklearn", "sqlalchemy", "torch", "xgboost"]
SOURCES = ["web", "pdf"]
EVIDENCE = ["fidelity_group", "preprocessed_accurate", "structured_vs_preprocessed",
            "structured_vs_original_db", "structured_vs_original_web", "scope",
            "root_cause_layer", "source_of_truth"]

def db_docstring(qm, api):
    m = qm.get_member_by_any_api_name(api)
    if not m:
        res = qm.find_member_by_any_path(api)
        m = (res or {}).get("original_member") or (res or {}).get("member")
    return clean_rst(getattr(m, "docstring", "") or "") if m else ""

def web_original(lib, api, qm, umap, manifest):
    """The web-original reference text for a member (shared across web/pdf)."""
    mtype, kind = W.member_type(qm, api)
    route_fetch = (lib in W.ANCHOR) or kind == "inherited" or (mtype not in W.TRUST_TYPES)
    orig = V.get_original(lib, api, route_fetch, umap)
    if orig is None:                       # trusted -> the web preprocessed IS the original
        st = manifest.get((api, "web"))
        if st:
            return V.read_collected(lib, "web", st[0], st[1], ".txt"), "web_preprocessed(trusted)"
        return "", "web_preprocessed(missing)"
    return orig, "fetched_web_page"

def write_eval(path, lib, src, api, row, dboc, orig_summary, orig_kind):
    lines = [
        f"api_name            : {api}",
        f"library / source    : {lib} / {src}",
        f"member_type         : {row.get('member_type','')}",
        f"VERDICT (correct_doc): {row.get('correct_doc','')}",
        f"verify_tier         : {row.get('verify_tier','')}",
        f"verify_reason       : {row.get('verify_reason','')}",
        f"sample_pick         : {row.get('sample_pick','')}",
        "",
        "--- evidence ---",
    ] + [f"{k:24}: {row.get(k,'')}" for k in EVIDENCE] + [
        "",
        f"comment             : {row.get('comment','')}",
        f"web_original_kind   : {orig_kind}",
        f"web_original_summary: {orig_summary}",
        "",
        "--- DB authoritative docstring (Sphinx source) ---",
        dboc if dboc else "(none in DB)",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

def main():
    print(f"Output base: {OUT}")
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    worklist = []
    for lib in LIBS:
        manifest = V.load_manifest(lib)
        umap = W.load_url_map(lib)
        db = MapCoDocDB(W.resolve_db(lib)); session = db.get_session(); qm = QueryManager(session)
        # rows per source from the review CSVs
        review = {}
        for src in SOURCES:
            p = os.path.join(TESTS, f"{lib}_{src}_fidelity_review.csv")
            if os.path.exists(p):
                review[src] = {r["api_name"]: r for r in csv.DictReader(open(p, newline="", encoding="utf-8"))}
        # one web-original per api (shared)
        apis = set().union(*[set(d) for d in review.values()]) if review else set()
        orig_cache = {}
        for api in apis:
            txt, kind = web_original(lib, api, qm, umap, manifest)
            orig_cache[api] = (txt, kind, V.original_summary(txt))
        idx_rows = collections.defaultdict(list)
        for src in SOURCES:
            for api, row in review.get(src, {}).items():
                tier = row.get("verify_tier", "unknown")
                st = manifest.get((api, src))
                if not st:
                    continue
                stem, bucket = st
                src_root = V.collected_src_root(lib, src)
                pre = os.path.join(src_root, bucket, stem + ".txt")
                struct = os.path.join(src_root, bucket, stem + ".json")
                dest = os.path.join(OUT, lib, src, tier)
                os.makedirs(dest, exist_ok=True)
                if os.path.exists(struct):
                    shutil.copy2(struct, os.path.join(dest, stem + ".structured.json"))
                if os.path.exists(pre):
                    shutil.copy2(pre, os.path.join(dest, stem + ".preprocessed.txt"))
                otext, okind, osum = orig_cache.get(api, ("", "n/a", ""))
                with open(os.path.join(dest, stem + ".original.txt"), "w", encoding="utf-8") as f:
                    f.write(otext or "(no web-original reference available)")
                write_eval(os.path.join(dest, stem + ".eval.txt"), lib, src, api, row,
                           db_docstring(qm, api), osum, okind)
                idx_rows[(src)].append({
                    "api_name": api, "stem": stem, "verify_tier": tier,
                    "verify_reason": row.get("verify_reason", ""), "correct_doc": row.get("correct_doc", ""),
                    "sample_pick": row.get("sample_pick", ""), "member_type": row.get("member_type", ""),
                })
                if row.get("sample_pick") in ("all", "yes"):
                    worklist.append({"library": lib, "source": src, **{k: row.get(k, "") for k in
                        ["api_name", "member_type", "verify_tier", "verify_reason", "correct_doc",
                         "structured_vs_original_db", "structured_vs_original_web", "scope", "comment"]}})
        # per (lib, source) index
        for src, rows in idx_rows.items():
            ip = os.path.join(OUT, lib, src, "_index.csv")
            os.makedirs(os.path.dirname(ip), exist_ok=True)
            with open(ip, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["api_name", "stem", "verify_tier", "verify_reason",
                                                  "correct_doc", "sample_pick", "member_type"])
                w.writeheader()
                w.writerows(sorted(rows, key=lambda x: (x["verify_tier"], x["api_name"])))
        session.close()
        print(f"{lib}: grouped {sum(len(v) for v in review.values())} member-rows")

    wl = os.path.join(TESTS, "verification_worklist.csv")
    with open(wl, "w", newline="", encoding="utf-8") as f:
        cols = ["library", "source", "api_name", "member_type", "verify_tier", "verify_reason",
                "correct_doc", "structured_vs_original_db", "structured_vs_original_web", "scope", "comment"]
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        w.writerows(sorted(worklist, key=lambda x: (x["verify_tier"], x["library"], x["source"], x["api_name"])))
    print(f"\nOutput: {OUT}")
    print(f"Worklist ({len(worklist)} rows): {wl}")

if __name__ == "__main__":
    main()
