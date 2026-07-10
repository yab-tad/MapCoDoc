"""
Build navigation indices into collected_verification/ so the worklist can target
each member's docs directly. Reads the review CSVs (+ collection manifests for the
on-disk stem). Does NOT copy docs.

Writes:
  tests/verification_worklist.csv         - the to-verify rows (sample_pick in all/yes) + stem + doc_path
  <collected_verification>/master_index.csv - ALL 1,442 members x 2 sources, with doc_path
Each doc_path points to <library>/<source>/<verify_tier>/<stem>, with the member's
files being <stem>.{structured.json, preprocessed.txt, original.txt, eval.txt}.
"""
import csv, os, glob, collections, sys
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import web_verify as V                       # for COLLECT_BASE

# Output base (where master_index.csv is written) provided as input; defaults beside
# collected_fidelity. Pass the SAME path used for collect_verification_docs.py.
DEFAULT_OUT = os.path.join(os.path.dirname(V.COLLECT_BASE), "collected_verification")
OUT_BASE = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
LIBS = ["requests", "numpy", "pandas", "sklearn", "sqlalchemy", "torch", "xgboost"]
SOURCES = ["web", "pdf"]

def manifest_stems(lib):
    m = {}
    p = os.path.join(TESTS, f"{lib}_fidelity_collection.csv")
    for r in csv.DictReader(open(p, newline="", encoding="utf-8")):
        m[(r["sample_api_name"], r["source"])] = r["stem"]
    return m

def main():
    master, worklist = [], []
    per_member_sources = collections.defaultdict(set)   # api -> set(sources) for the 1,442 count
    for lib in LIBS:
        stems = manifest_stems(lib)
        for src in SOURCES:
            p = os.path.join(TESTS, f"{lib}_{src}_fidelity_review.csv")
            if not os.path.exists(p):
                continue
            for r in csv.DictReader(open(p, newline="", encoding="utf-8")):
                api = r["api_name"]
                stem = stems.get((api, src), api)
                tier = r.get("verify_tier", "")
                doc_path = f"{lib}/{src}/{tier}/{stem}"
                per_member_sources[(lib, api)].add(src)
                row = {
                    "library": lib, "source": src, "api_name": api,
                    "member_type": r.get("member_type", ""), "verify_tier": tier,
                    "verify_reason": r.get("verify_reason", ""), "sample_pick": r.get("sample_pick", ""),
                    "correct_doc": r.get("correct_doc", ""),
                    "structured_vs_original_db": r.get("structured_vs_original_db", ""),
                    "structured_vs_original_web": r.get("structured_vs_original_web", ""),
                    "scope": r.get("scope", ""), "stem": stem, "doc_path": doc_path,
                    "human_verified": r.get("human_verified", ""), "human_note": r.get("human_note", ""),
                    "comment": r.get("comment", ""),
                }
                master.append(row)
                if r.get("sample_pick") in ("all", "yes"):
                    worklist.append(row)

    cols = ["library", "source", "api_name", "member_type", "verify_tier", "verify_reason",
            "sample_pick", "correct_doc", "structured_vs_original_db", "structured_vs_original_web",
            "scope", "stem", "doc_path", "human_verified", "human_note", "comment"]

    os.makedirs(OUT_BASE, exist_ok=True)
    with open(os.path.join(OUT_BASE, "master_index.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        w.writerows(sorted(master, key=lambda x: (x["library"], x["source"], x["verify_tier"], x["api_name"])))

    with open(os.path.join(TESTS, "verification_worklist.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        w.writerows(sorted(worklist, key=lambda x: (x["verify_tier"], x["library"], x["source"], x["api_name"])))

    print("distinct sample members:", len(per_member_sources))
    print("master_index rows (member x source):", len(master))
    print("worklist rows:", len(worklist))
    print("master by source:", dict(collections.Counter(r["source"] for r in master)))

if __name__ == "__main__":
    main()
