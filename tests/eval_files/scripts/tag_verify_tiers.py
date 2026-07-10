"""
Tag each member in the final review CSVs with a manual-verification grouping:

  verify_tier:
    must_verify        - low confidence, read ALL: AI-adjudicated (source_of_truth==manual) /
                         review / db-vs-web conflict / web-source 'no'
    mode_sample        - pdf 'no' failures: sample a few per (library x failure-mode)
    confidence_sample  - 'yes' verdicts: partial-scope (all) + a light stratified sample

  verify_reason : ai_adjudicated | review | conflict | web_no | pdf_no:<mode> | partial_scope | yes_web | yes_db
  sample_pick   : all (verify all) | yes (in the sample) | no (not sampled)

Note: source_of_truth=='manual' means AI-adjudicated in the prior pass (not human-verified),
so those are routed to must_verify for the human to confirm.

Adds the three columns to tests/<lib>_<source>_fidelity_review.csv (idempotent).
"""
import csv, glob, os, collections

TESTS = os.path.dirname(os.path.abspath(__file__))
BASE_COLS = ["api_name", "member_type", "fidelity_group", "preprocessed_accurate",
             "structured_vs_preprocessed", "structured_vs_original_db",
             "structured_vs_original_web", "correct_doc", "source_of_truth", "scope",
             "root_cause_layer", "comment"]
NEW_COLS = ["verify_tier", "verify_reason", "sample_pick"]
PRESERVE_COLS = ["human_verified", "human_note"]   # carried through if already present

MODE_SAMPLE_PER_CELL = 5          # pdf 'no': reads per (library x mode)
YES_SAMPLE_RATE = 0.03            # plain 'yes': fraction per (library x source x source_of_truth)
YES_SAMPLE_MIN = 2

def decisive(v):
    return v in ("yes", "no")

def pdf_mode(r):
    c = (r.get("comment") or "").lower()
    if r.get("structured_vs_preprocessed") == "empty" or "empty extraction" in c or "empty/retrieval" in c:
        return "empty"
    if "wrong-member" in c or "contamination" in c or "wrong content" in c:
        return "contamination"
    return "mismatch"

def load_all():
    recs = []
    for f in sorted(glob.glob(os.path.join(TESTS, "*_fidelity_review.csv"))):
        stem = os.path.basename(f)[:-len("_fidelity_review.csv")]
        lib, src = stem.rsplit("_", 1)
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                r["_file"] = f; r["_lib"] = lib; r["_src"] = src
                recs.append(r)
    return recs

def assign_tier(r):
    cd = r["correct_doc"]; sot = r["source_of_truth"]; src = r["_src"]
    db, web = r["structured_vs_original_db"], r["structured_vs_original_web"]
    if sot == "manual":   # AI-adjudicated in the prior pass -> human should confirm
        return "must_verify", "ai_adjudicated", "all"
    if cd == "review":
        return "must_verify", "review", "all"
    if decisive(db) and decisive(web) and db != web:
        return "must_verify", "conflict", "all"
    if cd == "no" and src == "web":
        return "must_verify", "web_no", "all"
    if cd == "no" and src == "pdf":
        return "mode_sample", f"pdf_no:{pdf_mode(r)}", "no"   # sample_pick set later
    # cd == yes
    if (r.get("scope") or "").startswith("partial"):
        return "confidence_sample", "partial_scope", "yes"   # review all partial-scope
    return "confidence_sample", f"yes_{sot}", "no"            # sampled later

def main():
    recs = load_all()
    for r in recs:
        r["verify_tier"], r["verify_reason"], r["sample_pick"] = assign_tier(r)

    # ---- deterministic stratified sampling ----
    # mode_sample: first N per (lib, mode)
    cells = collections.defaultdict(list)
    for r in recs:
        if r["verify_tier"] == "mode_sample":
            cells[(r["_lib"], r["verify_reason"])].append(r)
    for cell, members in cells.items():
        for r in sorted(members, key=lambda x: x["api_name"])[:MODE_SAMPLE_PER_CELL]:
            r["sample_pick"] = "yes"

    # confidence_sample (plain yes, not partial): rate per (lib, src, sot)
    ystrata = collections.defaultdict(list)
    for r in recs:
        if r["verify_tier"] == "confidence_sample" and r["verify_reason"] != "partial_scope":
            ystrata[(r["_lib"], r["_src"], r["source_of_truth"])].append(r)
    for strat, members in ystrata.items():
        members.sort(key=lambda x: x["api_name"])
        k = max(YES_SAMPLE_MIN, round(len(members) * YES_SAMPLE_RATE))
        step = max(1, len(members) // k)
        for i in range(0, len(members), step):
            members[i]["sample_pick"] = "yes"

    # ---- write back per file ----
    by_file = collections.defaultdict(list)
    for r in recs:
        by_file[r["_file"]].append(r)
    for f, rs in by_file.items():
        out_cols = BASE_COLS + NEW_COLS + PRESERVE_COLS
        with open(f, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=out_cols)
            w.writeheader()
            for r in rs:
                w.writerow({c: r.get(c, "") for c in out_cols})

    # ---- summary ----
    print("verify_tier:", dict(collections.Counter(r["verify_tier"] for r in recs)))
    print("verify_reason:", dict(collections.Counter(r["verify_reason"] for r in recs)))
    picks = collections.Counter(r["sample_pick"] for r in recs)
    print("sample_pick:", dict(picks))
    to_read = sum(1 for r in recs if r["sample_pick"] in ("all", "yes"))
    print(f"\nTOTAL members to read manually: {to_read}  (of {len(recs)})")
    print("  must_verify (all):", sum(1 for r in recs if r["verify_tier"] == "must_verify"),
          "->", dict(collections.Counter(r["verify_reason"] for r in recs if r["verify_tier"] == "must_verify")))
    print("  mode_sample picks:", sum(1 for r in recs if r["verify_tier"] == "mode_sample" and r["sample_pick"] == "yes"))
    print("  confidence picks :", sum(1 for r in recs if r["verify_tier"] == "confidence_sample" and r["sample_pick"] == "yes"))

if __name__ == "__main__":
    main()
