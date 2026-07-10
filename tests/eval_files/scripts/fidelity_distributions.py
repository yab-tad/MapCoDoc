"""
Fidelity-construct distributions and artifact-vs-defect bucketing.

Reads the per-source fidelity report JSONs (tests/<lib>_fidelity_<web|pdf>.json),
joins them to the manual trace-link census in the review CSVs, and reports the
THREE fidelity sub-constructs as continuous distributions so we can finalize bins
from real numbers:

  F1  Faithfulness (precision)  = grounding_score          (grounded fields / total)
  F2  Categorization            = (not measurable here; whole-source matcher is
                                   blind to misplacement -> human/section-aware only)
  F3  Completeness (recall)     = source_coverage_ratio    (covered source units / checked)

Everything is reported (a) overall and (b) conditioned on correct trace-link
(correct_doc == 'yes'), since fidelity is only meaningful for correctly recovered docs.

The artifact-vs-defect section bins the per-issue similarity so we can separate
benign normalization/glyph drift from genuine structuring defects, split by source
(web vs pdf) to test the PDF-glyph hypothesis.

Outputs:
  - a readable console report
  - tests/fidelity_metrics.csv : one row per evaluated (member x source) with the
    numeric metrics + trace-link/human labels, for downstream table building.

Read-only w.r.t. the reports/CSVs; only writes fidelity_metrics.csv.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import statistics as st
from collections import Counter, defaultdict

TESTS = os.path.dirname(os.path.abspath(__file__))
LIBS = ["numpy", "pandas", "requests", "sklearn", "sqlalchemy", "torch", "xgboost"]
SOURCES = ["web", "pdf"]

# current checker thresholds (doc_quality/config.py), for reference in the report
EXACT_MIN = 0.995
PARTIAL_MIN = 0.75
OMIT_EXACT_MIN = 0.90
OMIT_PARTIAL_MIN = 0.55


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_review_labels():
    """(lib, src, api_name) -> {correct_doc, human_verified, member_type}."""
    labels = {}
    for f in glob.glob(os.path.join(TESTS, "*_fidelity_review.csv")):
        base = os.path.basename(f)[:-len("_fidelity_review.csv")]
        lib, src = base.rsplit("_", 1)
        for r in csv.DictReader(open(f, encoding="utf-8")):
            labels[(lib, src, r["api_name"])] = {
                "correct_doc": r.get("correct_doc", ""),
                "human_verified": r.get("human_verified", ""),
                "member_type": r.get("member_type", ""),
            }
    return labels


def load_members():
    """Yield per-member records from the per-source fidelity report JSONs."""
    recs = []
    for lib in LIBS:
        for src in SOURCES:
            p = os.path.join(TESTS, f"{lib}_fidelity_{src}.json")
            if not os.path.exists(p):
                continue
            data = json.load(open(p, encoding="utf-8"))
            for _srckey, sec in (data.get("sources") or {}).items():
                for m in (sec.get("members") or []):
                    recs.append((lib, src, m))
    return recs


# --------------------------------------------------------------------------- #
# Distribution helpers
# --------------------------------------------------------------------------- #
GROUND_BINS = [
    ("exact (=1.0)",      lambda x: x >= 0.9999),
    ("[0.95,1.0)",        lambda x: 0.95 <= x < 0.9999),
    ("[0.90,0.95)",       lambda x: 0.90 <= x < 0.95),
    ("[0.75,0.90)",       lambda x: 0.75 <= x < 0.90),
    ("[0.50,0.75)",       lambda x: 0.50 <= x < 0.75),
    ("(0,0.50)",          lambda x: 0.0 < x < 0.50),
    ("zero (=0.0)",       lambda x: x <= 0.0),
]


def hist(values, bins):
    c = Counter()
    for v in values:
        for name, pred in bins:
            if pred(v):
                c[name] += 1
                break
    return c


def summarize(values):
    if not values:
        return "n=0"
    vs = sorted(values)
    return (f"n={len(vs)}  mean={sum(vs)/len(vs):.3f}  median={st.median(vs):.3f}  "
            f"min={vs[0]:.3f}  p25={vs[len(vs)//4]:.3f}  p75={vs[(3*len(vs))//4]:.3f}")


def print_hist(title, values, bins):
    print(f"  {title}: {summarize(values)}")
    if not values:
        return
    h = hist(values, bins)
    n = len(values)
    for name, _ in bins:
        k = h.get(name, 0)
        bar = "#" * int(round(40 * k / n)) if n else ""
        print(f"      {name:14s} {k:5d}  {100*k/n:5.1f}%  {bar}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    labels = load_review_labels()
    recs = load_members()

    print("=" * 78)
    print("FIDELITY DISTRIBUTIONS  (checker thresholds: exact>=%.3f, partial>=%.2f; "
          "omission covered>=%.2f, partial>=%.2f)" % (EXACT_MIN, PARTIAL_MIN, OMIT_EXACT_MIN, OMIT_PARTIAL_MIN))
    print("=" * 78)
    print(f"evaluated (member x source) in reports: {len(recs)}")
    unmatched = sum(1 for (lib, src, m) in recs if (lib, src, m["api_name"]) not in labels)
    print(f"unmatched to review CSV: {unmatched}")
    print()

    # attach labels + write per-member metrics CSV
    out_rows = []
    for (lib, src, m) in recs:
        lab = labels.get((lib, src, m["api_name"]), {})
        out_rows.append({
            "library": lib, "source": src, "api_name": m["api_name"],
            "member_type": m.get("type", ""),
            "grounding_score": m.get("grounding_score"),
            "source_coverage_ratio": m.get("source_coverage_ratio"),
            "units_checked": m.get("units_checked"),
            "source_units_checked": m.get("source_units_checked"),
            "additive_issue_count": m.get("additive_issue_count"),
            "omission_count": m.get("omission_count"),
            "empty_extraction": m.get("empty_extraction"),
            "omission_scope": m.get("omission_scope"),
            "correct_doc": lab.get("correct_doc", ""),
            "human_verified": lab.get("human_verified", ""),
        })
    out_path = os.path.join(TESTS, "fidelity_metrics.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    def tl_yes(r):
        return r["correct_doc"] == "yes"

    # ---- F1 Faithfulness (grounding_score) ----
    print("-" * 78)
    print("F1  FAITHFULNESS  (grounding_score = grounded fields / total fields)")
    print("-" * 78)
    for src in SOURCES:
        allv = [r["grounding_score"] for r in out_rows
                if r["source"] == src and r["grounding_score"] is not None]
        tlv = [r["grounding_score"] for r in out_rows
               if r["source"] == src and tl_yes(r) and r["grounding_score"] is not None]
        print(f"[{src}] ALL evaluated")
        print_hist("grounding_score", allv, GROUND_BINS)
        print(f"[{src}] conditioned on correct trace-link (correct_doc==yes)")
        print_hist("grounding_score", tlv, GROUND_BINS)
        print()

    # ---- F3 Completeness (source_coverage_ratio) ----
    print("-" * 78)
    print("F3  COMPLETENESS  (source_coverage_ratio = covered source units / checked;"
          " only members whose omission scope was computed)")
    print("-" * 78)
    for src in SOURCES:
        scoped = [r for r in out_rows if r["source"] == src
                  and r["source_coverage_ratio"] not in (None, "")]
        allv = [float(r["source_coverage_ratio"]) for r in scoped]
        tlv = [float(r["source_coverage_ratio"]) for r in scoped if tl_yes(r)]
        nscope = Counter(r["omission_scope"] for r in out_rows if r["source"] == src)
        print(f"[{src}] omission_scope dist: {dict(nscope)}")
        print(f"[{src}] ALL scoped")
        print_hist("source_coverage_ratio", allv, GROUND_BINS)
        print(f"[{src}] conditioned on correct trace-link")
        print_hist("source_coverage_ratio", tlv, GROUND_BINS)
        print()

    # ---- Joint faithful+complete ----
    print("-" * 78)
    print("JOINT  (conditioned on correct trace-link)")
    print("-" * 78)
    for src in SOURCES:
        sub = [r for r in out_rows if r["source"] == src and tl_yes(r)]
        n = len(sub)
        fully = sum(1 for r in sub if not r["empty_extraction"]
                    and (r["additive_issue_count"] or 0) == 0
                    and (r["omission_count"] or 0) == 0)
        empty = sum(1 for r in sub if r["empty_extraction"])
        add = sum(1 for r in sub if (r["additive_issue_count"] or 0) > 0)
        omit = sum(1 for r in sub if (r["omission_count"] or 0) > 0)
        print(f"[{src}] trace-link-correct N={n}: fully_faithful_and_complete={fully} "
              f"({100*fully/n:.1f}%)  empty={empty}  has_additive={add}  has_omission={omit}")
    print()

    # ---- Artifact-vs-defect: per-issue similarity bands ----
    print("-" * 78)
    print("ARTIFACT-vs-DEFECT  (per-issue similarity bands, from <lib>_fidelity_<src>.csv)")
    print("  faithfulness issues: category in {partial_support, unsupported_content}")
    print("  completeness issues: category in {partially_omitted, omitted}")
    print("-" * 78)
    issue_rows = []
    for f in glob.glob(os.path.join(TESTS, "*_fidelity_web.csv")) + \
             glob.glob(os.path.join(TESTS, "*_fidelity_pdf.csv")):
        src = "web" if f.endswith("_web.csv") else "pdf"
        for r in csv.DictReader(open(f, encoding="utf-8")):
            r["_src"] = src
            issue_rows.append(r)

    SIM_BANDS = [
        (">=0.90 (cosmetic/glyph?)", lambda x: x >= 0.90),
        ("[0.75,0.90) paraphrase",   lambda x: 0.75 <= x < 0.90),
        ("[0.55,0.75) weak",         lambda x: 0.55 <= x < 0.75),
        ("<0.55 unsupported",        lambda x: x < 0.55),
    ]
    for src in SOURCES:
        faith = [r for r in issue_rows if r["_src"] == src
                 and r.get("category") in ("partial_support", "unsupported_content")]
        sims = []
        for r in faith:
            try:
                sims.append(float(r.get("similarity") or 0))
            except ValueError:
                pass
        print(f"[{src}] faithfulness issues n={len(faith)}  "
              f"category={dict(Counter(r.get('category') for r in faith))}")
        h = hist(sims, SIM_BANDS)
        for name, _ in SIM_BANDS:
            k = h.get(name, 0)
            print(f"      sim {name:26s} {k:5d}")
        # by section to see where additive content concentrates
        sec = Counter(r.get("section", "").split("[")[0] for r in faith)
        print(f"      top sections: {dict(sec.most_common(8))}")
        print()

    for src in SOURCES:
        omit = [r for r in issue_rows if r["_src"] == src
                and r.get("category") in ("partially_omitted", "omitted")]
        print(f"[{src}] completeness (omission) issues n={len(omit)}  "
              f"category={dict(Counter(r.get('category') for r in omit))}")
    print()
    print(f"wrote per-member metrics -> {out_path}")


if __name__ == "__main__":
    main()
