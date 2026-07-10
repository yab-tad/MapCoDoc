"""
Convert DB-grounded audit CSVs into final review CSVs (explicit columns + comment).
Reads tests/<lib>_<source>_ground_truth_audit.csv
Writes tests/<lib>_<source>_fidelity_review.csv   (overwrites prior review CSVs)
"""
import csv, os
from collections import Counter

TESTS = os.path.dirname(os.path.abspath(__file__))
LIBS = ["requests", "numpy", "pandas", "sklearn", "sqlalchemy", "torch", "xgboost"]
SOURCES = ["web", "pdf"]

OUT_COLS = ["api_name", "member_type", "fidelity_group", "preprocessed_accurate",
            "structured_vs_preprocessed", "structured_vs_original", "correct_doc",
            "scope", "root_cause_layer", "comment"]

def comment_for(r):
    cd = r["correct_doc"]; pa = r["preprocessed_accurate"]
    svp = r["structured_vs_preprocessed"]; svo = r["structured_vs_original"]
    gt = (r.get("gt_doc_head") or "").strip()
    sp = (r.get("struct_purpose_head") or "").strip()
    if cd == "yes":
        return "faithful: structured matches DB ground-truth summary/signature (presentation diffs - urls/refs/glyphs - normalized)"
    if cd == "review" or svo == "no_groundtruth" or pa == "no_groundtruth":
        return f"DB has no docstring/signature to verify against (internal/alias member); structured purpose: \"{sp[:80]}\" - manual confirmation"
    # cd == no
    if svp == "empty":
        return f"retrieval failure -> empty extraction; retrieved doc lacked the member's real doc. DB truth: \"{gt[:90]}\""
    if pa == "no" and svp in ("yes", "partial"):
        return f"wrong content retrieved (retrieval-layer): structured \"{sp[:70]}\" != DB truth \"{gt[:70]}\""
    if r["root_cause_layer"].startswith("structuring"):
        return f"structuring-layer: retrieved doc present but structured output does not match DB truth \"{gt[:80]}\""
    return f"correct_doc=no: structured \"{sp[:60]}\" vs DB truth \"{gt[:60]}\""

def main():
    grand = Counter()
    for lib in LIBS:
        for src in SOURCES:
            ap = os.path.join(TESTS, f"{lib}_{src}_ground_truth_audit.csv")
            if not os.path.exists(ap):
                continue
            with open(ap, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            out_rows = []
            for r in rows:
                out_rows.append({
                    "api_name": r["api_name"], "member_type": r["member_type"],
                    "fidelity_group": r["fidelity_group"],
                    "preprocessed_accurate": r["preprocessed_accurate"],
                    "structured_vs_preprocessed": r["structured_vs_preprocessed"],
                    "structured_vs_original": r["structured_vs_original"],
                    "correct_doc": r["correct_doc"], "scope": r["scope"],
                    "root_cause_layer": r["root_cause_layer"], "comment": comment_for(r),
                })
            outp = os.path.join(TESTS, f"{lib}_{src}_fidelity_review.csv")
            with open(outp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=OUT_COLS); w.writeheader(); w.writerows(out_rows)
            c = Counter(r["correct_doc"] for r in out_rows)
            grand.update(c)
            print(f"{lib}/{src}: {len(out_rows)} rows  correct={c.get('yes',0)} wrong={c.get('no',0)} review={c.get('review',0)}")
    print("TOTAL:", dict(grand))

if __name__ == "__main__":
    main()
