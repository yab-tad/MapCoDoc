"""
PDF anchoring breakdown crossed with manual trace-link retrieval verdicts.

For each of the 1442 sampled PDF API members we read the anchoring strategy that
the PDF extractor actually used (``scores.match_type`` in ``extracted_docs.json``)
and cross it with the manual retrieval verdict (``correct_doc`` in the per-library
``*_pdf_fidelity_review.csv`` census).

The ``extracted_docs.json`` keys use the extraction-time (public / re-export)
module path, whereas the census ``api_name`` uses the fully-resolved definition
path. Because per library the two are a bijection over the same member set, we
align them with: (1) exact match, then (2) equal-leaf + maximum shared dot-token
overlap for the leftovers, plus a tiny manual override table for the handful of
members whose leaf differs (aliases such as ``C`` == ``ConstantKernel``).

Outputs:
  tests/pdf_anchoring_breakdown.csv   per-member rows
  tests/pdf_anchoring_summary.csv     per (library x category) and totals
  tests/pdf_anchoring_tables.tex      IEEEtran LaTeX tables
"""
import csv
import glob
import json
import os
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRAPED = os.path.join(ROOT, "doc_processor", "doc_artifacts", "scraped_doc")

LIBS = ["numpy", "pandas", "requests", "sklearn", "sqlalchemy", "torch", "xgboost"]

# match_type -> reported anchoring category.
#   lexical_direct       : in-scope lexical anchor on a definition line
#                          (exact needle / api-name anchor / regex / raw search)
#   lexical_cross_section: lexical anchor found OUTSIDE the member's parent-class
#                          section (inherited / split-section methods)
#   semantic             : embedding "semantic window" fallback (no lexical anchor)
#   section_start_guess  : low-confidence guess -- top-ranked section start, the
#                          member itself was never anchored
#   not_found            : nothing retrieved
CATEGORY = {
    "exact": "lexical_direct",
    "anchor": "lexical_direct",
    "regex": "lexical_direct",
    "raw_search": "lexical_direct",
    "cross_section_fallback": "lexical_cross_section",
    "semantic_window": "semantic",
    "fallback": "section_start_guess",
    "not_found": "not_found",
}
CATEGORY_ORDER = [
    "lexical_direct",
    "lexical_cross_section",
    "semantic",
    "section_start_guess",
    "not_found",
]
CATEGORY_LABEL = {
    "lexical_direct": "Lexical (direct)",
    "lexical_cross_section": "Lexical (cross-section)",
    "semantic": "Semantic (window)",
    "section_start_guess": "Section-start guess",
    "not_found": "Not found",
}

# Members whose extraction key and census api_name share no leaf (true aliases).
MANUAL_MAP = {
    ("sklearn", "sklearn.gaussian_process._gpc.C"):
        "sklearn.gaussian_process.kernels.ConstantKernel",
}


def _leaf(name):
    return name.split(".")[-1]


def _tokens(name):
    return set(name.split("."))


def load_extracted(lib):
    path = glob.glob(os.path.join(
        SCRAPED, lib, "samples", f"{lib}_PDF", "*", "extracted_docs.json"))[0]
    with open(path, encoding="utf-8") as fh:
        return json.load(fh), path


def load_review(lib):
    """Return {api_name: row-dict} for the full PDF census."""
    path = os.path.join(HERE, f"{lib}_pdf_fidelity_review.csv")
    rows = {}
    with open(path, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            rows[r["api_name"]] = r
    return rows


def build_crosswalk(lib, extracted_keys, review_names):
    """Map each extracted_docs key -> canonical census api_name (bijection)."""
    review_set = set(review_names)
    mapping, used = {}, set()

    # (1) exact
    for key in extracted_keys:
        if key in review_set:
            mapping[key] = key
            used.add(key)

    # (2) manual overrides
    for key in extracted_keys:
        if key in mapping:
            continue
        override = MANUAL_MAP.get((lib, key))
        if override and override in review_set and override not in used:
            mapping[key] = override
            used.add(override)

    # (3) leaf + shared-token overlap for the remaining bijection leftovers
    left_e = [k for k in extracted_keys if k not in mapping]
    left_r = [r for r in review_names if r not in used]
    by_leaf = defaultdict(list)
    for r in left_r:
        by_leaf[_leaf(r)].append(r)

    candidates = []
    for e in left_e:
        for r in by_leaf.get(_leaf(e), []):
            candidates.append((len(_tokens(e) & _tokens(r)), e, r))
    candidates.sort(reverse=True)
    for _, e, r in candidates:
        if e in mapping or r in used:
            continue
        mapping[e] = r
        used.add(r)

    unmatched = [k for k in extracted_keys if k not in mapping]
    return mapping, unmatched


def main():
    per_member = []
    unmatched_all = []

    for lib in LIBS:
        docs, _ = load_extracted(lib)
        review = load_review(lib)
        mapping, unmatched = build_crosswalk(lib, list(docs), list(review))
        unmatched_all.extend((lib, k) for k in unmatched)

        for key, entry in docs.items():
            scores = (entry or {}).get("scores") or {}
            match_type = scores.get("match_type", "not_found")
            canonical = mapping.get(key)
            rrow = review.get(canonical, {}) if canonical else {}
            per_member.append({
                "library": lib,
                "api_name": canonical or "",
                "extracted_key": key,
                "member_type": rrow.get("member_type", ""),
                "match_type": match_type,
                "anchor_category": CATEGORY.get(match_type, match_type),
                "lexical_score": scores.get("lexical"),
                "semantic_score": scores.get("semantic"),
                "final_score": scores.get("final"),
                "correct_doc": (rrow.get("correct_doc") or "").strip().lower(),
            })

    # ---- per-member CSV ----
    out_rows = os.path.join(HERE, "pdf_anchoring_breakdown.csv")
    with open(out_rows, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(per_member[0].keys()))
        w.writeheader()
        w.writerows(per_member)

    # ---- summary CSV (library x category) ----
    tot = defaultdict(Counter)   # tot[lib][cat]
    ok = defaultdict(Counter)    # ok[lib][cat] where correct_doc == yes
    for r in per_member:
        lib, cat = r["library"], r["anchor_category"]
        tot[lib][cat] += 1
        if r["correct_doc"] == "yes":
            ok[lib][cat] += 1

    out_sum = os.path.join(HERE, "pdf_anchoring_summary.csv")
    with open(out_sum, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["library", "anchor_category", "total", "accurately_retrieved",
                    "retrieval_accuracy_pct"])
        for lib in LIBS:
            for cat in CATEGORY_ORDER:
                t, o = tot[lib][cat], ok[lib][cat]
                if t:
                    w.writerow([lib, cat, t, o, round(100.0 * o / t, 1)])
        grand_t, grand_o = Counter(), Counter()
        for lib in LIBS:
            for cat in CATEGORY_ORDER:
                grand_t[cat] += tot[lib][cat]
                grand_o[cat] += ok[lib][cat]
        for cat in CATEGORY_ORDER:
            t, o = grand_t[cat], grand_o[cat]
            w.writerow(["ALL", cat, t, o, round(100.0 * o / t, 1) if t else ""])

    # ---- console report + reconciliation ----
    n_total = len(per_member)
    n_correct = sum(1 for r in per_member if r["correct_doc"] == "yes")
    print(f"PDF members: {n_total}   accurately retrieved: {n_correct} "
          f"({100.0 * n_correct / n_total:.1f}%)   unmatched keys: {len(unmatched_all)}")
    if unmatched_all:
        print("  UNMATCHED:", unmatched_all)
    print()
    hdr = "category".ljust(24) + "total".rjust(8) + "correct".rjust(9) + "accuracy".rjust(11)
    print(hdr)
    for cat in CATEGORY_ORDER:
        t, o = grand_t[cat], grand_o[cat]
        acc = f"{100.0 * o / t:.1f}%" if t else "-"
        print(CATEGORY_LABEL[cat].ljust(24) + str(t).rjust(8) + str(o).rjust(9) + acc.rjust(11))

    # ---- LaTeX ----
    tex = []
    tex.append("% Auto-generated by tests/pdf_anchoring_breakdown.py")
    tex.append(r"\begin{table}[t]")
    tex.append(r"\centering")
    tex.append(r"\caption{PDF trace-link retrieval accuracy by anchoring strategy "
               r"(full manual census, $N=1442$).}")
    tex.append(r"\label{tab:pdf-anchoring}")
    tex.append(r"\begin{tabular}{lrrr}")
    tex.append(r"\toprule")
    tex.append(r"Anchoring strategy & Members & Correct & Accuracy \\")
    tex.append(r"\midrule")
    for cat in CATEGORY_ORDER:
        t, o = grand_t[cat], grand_o[cat]
        acc = f"{100.0 * o / t:.1f}\\%" if t else "--"
        tex.append(f"{CATEGORY_LABEL[cat]} & {t} & {o} & {acc} \\\\")
    tex.append(r"\midrule")
    tex.append(f"\\textbf{{All}} & {n_total} & {n_correct} & "
               f"{100.0 * n_correct / n_total:.1f}\\% \\\\")
    tex.append(r"\bottomrule")
    tex.append(r"\end{tabular}")
    tex.append(r"\end{table}")
    out_tex = os.path.join(HERE, "pdf_anchoring_tables.tex")
    with open(out_tex, "w", encoding="utf-8") as fh:
        fh.write("\n".join(tex) + "\n")

    print(f"\nWrote:\n  {out_rows}\n  {out_sum}\n  {out_tex}")


if __name__ == "__main__":
    main()
