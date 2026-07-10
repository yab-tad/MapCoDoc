"""
Build the fidelity results tables from the trace-unit artifacts.

Reads:
  tests/trace_fidelity_metrics.csv   (per member x source: F1/F2/F3 + counts)
  tests/trace_fidelity_units.csv     (per trace unit: global/section scores, placement)
  tests/trace_fidelity_recall.csv    (per completeness recall item: type, covered)

All numbers are conditioned on the manual trace-link census (correct_doc == 'yes').

Emits a readable console report and IEEEtran-ready LaTeX tables to
tests/trace_fidelity_tables.tex.

LOCKED BINS
  Member-level construct score : Perfect (=1.0) | High [0.90,1.0) | Moderate [0.75,0.90) | Low (<0.75)
  Faithfulness unit similarity : Faithful (>=0.995) | Cosmetic [0.90,0.995) | Paraphrase [0.75,0.90)
                                 | Weak [0.55,0.75) | Unsupported (<0.55)
"""
from __future__ import annotations

import csv
import os
from collections import Counter, defaultdict

TESTS = os.path.dirname(os.path.abspath(__file__))
SOURCES = ["web", "pdf"]
LIBS = ["numpy", "pandas", "requests", "sklearn", "sqlalchemy", "torch", "xgboost"]

CONSTRUCT_BINS = [
    ("Perfect (=1.0)", lambda x: x >= 0.9999),
    ("High [0.90,1.0)", lambda x: 0.90 <= x < 0.9999),
    ("Moderate [0.75,0.90)", lambda x: 0.75 <= x < 0.90),
    ("Low (<0.75)", lambda x: x < 0.75),
]
FAITH_BANDS = [
    ("Faithful (>=.995)", lambda x: x >= 0.995),
    ("Cosmetic [.90,.995)", lambda x: 0.90 <= x < 0.995),
    ("Paraphrase [.75,.90)", lambda x: 0.75 <= x < 0.90),
    ("Weak [.55,.75)", lambda x: 0.55 <= x < 0.75),
    ("Unsupported (<.55)", lambda x: x < 0.55),
]
RECALL_TYPES = ["src_signature", "src_param", "src_purpose", "src_returns"]
RECALL_LABEL = {"src_signature": "Signature", "src_param": "Parameters",
                "src_purpose": "Purpose", "src_returns": "Returns", "src_examples": "Examples"}
FAITH_KINDS = ["signature", "purpose", "param_name_type", "param_desc",
               "return_type", "return_desc", "example"]


def load(name):
    with open(os.path.join(TESTS, name), encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r.get("correct_doc") == "yes"]


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def binned(values, bins):
    c = Counter()
    for v in values:
        for name, pred in bins:
            if pred(v):
                c[name] += 1
                break
    return c


# --------------------------------------------------------------------------- #
def table_constructs(metrics):
    """Headline: F1/F2/F3 mean + %perfect by source."""
    rows = []
    for src in SOURCES:
        sub = [r for r in metrics if r["source"] == src]
        row = {"source": src, "n": len(sub)}
        for key in ("f1_faithfulness", "f2_categorization", "f3_completeness"):
            vals = [fnum(r[key]) for r in sub]
            vals = [v for v in vals if v is not None]
            row[key + "_mean"] = mean(vals)
            row[key + "_perfect"] = 100 * sum(1 for v in vals if v >= 0.9999) / len(vals)
        rows.append(row)
    return rows


def table_construct_bins(metrics, key):
    out = {}
    for src in SOURCES:
        vals = [fnum(r[key]) for r in metrics if r["source"] == src]
        vals = [v for v in vals if v is not None]
        out[src] = (binned(vals, CONSTRUCT_BINS), len(vals))
    return out


def table_faith_bands(units):
    """Faithfulness unit-level artifact-vs-defect bands by source, plus by kind."""
    by_src = {}
    by_src_kind = {}
    for src in SOURCES:
        urows = [u for u in units if u["source"] == src and u["kind"] in FAITH_KINDS
                 and fnum(u["global_score"]) is not None]
        vals = [fnum(u["global_score"]) for u in urows]
        by_src[src] = (binned(vals, FAITH_BANDS), len(vals))
        kd = {}
        for k in FAITH_KINDS:
            kv = [fnum(u["global_score"]) for u in urows if u["kind"] == k]
            kd[k] = (binned(kv, FAITH_BANDS), len(kv))
        by_src_kind[src] = kd
    return by_src, by_src_kind


def table_recall(recall):
    out = {}
    for src in SOURCES:
        d = {}
        for t in RECALL_TYPES + ["src_examples"]:
            sub = [r for r in recall if r["source"] == src and r["recall_type"] == t]
            cov = sum(1 for r in sub if r["covered"] == "True")
            d[t] = (cov, len(sub))
        out[src] = d
    return out


def table_categorization(units, metrics):
    """Placement misplacement by kind + example classification, by source."""
    out = {}
    for src in SOURCES:
        urows = [u for u in units if u["source"] == src]
        place = {}
        for k in ("signature", "purpose", "param_name_type", "param_desc", "return_type", "return_desc"):
            ku = [u for u in urows if u["kind"] == k and u["grounded"] == "True"]
            mis = sum(1 for u in ku if u["misplaced"] == "True")
            place[k] = (mis, len(ku))
        ep = [u for u in urows if u["kind"] == "example_placement"]
        ep_ok = sum(1 for u in ep if u["grounded"] == "True")
        ep_def = sum(1 for u in ep if u["misplaced"] == "True")
        out[src] = {"placement": place, "example_ok": ep_ok, "example_def": ep_def, "example_n": len(ep)}
    return out


def table_per_library(metrics):
    out = {}
    for lib in LIBS:
        out[lib] = {}
        for src in SOURCES:
            sub = [r for r in metrics if r["library"] == lib and r["source"] == src]
            d = {"n": len(sub)}
            for key in ("f1_faithfulness", "f2_categorization", "f3_completeness"):
                vals = [fnum(r[key]) for r in sub if fnum(r[key]) is not None]
                d[key] = mean(vals)
            out[lib][src] = d
    return out


# --------------------------------------------------------------------------- #
def console_report(metrics, units, recall):
    print("=" * 78)
    print("FIDELITY RESULTS  (manual trace-link census; correct_doc == yes)")
    print("=" * 78)

    print("\n[A] Sub-construct headline (mean / % perfect)")
    print(f"{'source':6} {'N':>5}  {'F1 faith':>16} {'F2 categ':>16} {'F3 compl':>16}")
    for r in table_constructs(metrics):
        print(f"{r['source']:6} {r['n']:>5}  "
              f"{r['f1_faithfulness_mean']:.3f}/{r['f1_faithfulness_perfect']:5.1f}%  "
              f"{r['f2_categorization_mean']:.3f}/{r['f2_categorization_perfect']:5.1f}%  "
              f"{r['f3_completeness_mean']:.3f}/{r['f3_completeness_perfect']:5.1f}%")

    print("\n[B] Member-level construct bins")
    for key, lbl in [("f1_faithfulness", "F1"), ("f2_categorization", "F2"), ("f3_completeness", "F3")]:
        b = table_construct_bins(metrics, key)
        print(f"  {lbl}:")
        for src in SOURCES:
            c, n = b[src]
            dist = "  ".join(f"{name.split()[0]}={c.get(name,0)}({100*c.get(name,0)/n:.1f}%)"
                             for name, _ in CONSTRUCT_BINS)
            print(f"    [{src}] n={n}: {dist}")

    print("\n[C] Faithfulness unit similarity bands (artifact vs defect)")
    by_src, by_kind = table_faith_bands(units)
    for src in SOURCES:
        c, n = by_src[src]
        print(f"  [{src}] units={n}")
        for name, _ in FAITH_BANDS:
            k = c.get(name, 0)
            print(f"      {name:22s} {k:5d}  {100*k/n:5.1f}%")

    print("\n[D] Completeness recall by trace-unit type (covered %)")
    rec = table_recall(recall)
    print(f"  {'type':12}  {'web':>14}  {'pdf':>14}")
    for t in RECALL_TYPES + ["src_examples"]:
        cells = []
        for src in SOURCES:
            cov, n = rec[src][t]
            cells.append(f"{cov}/{n} ({100*cov/n:4.1f}%)" if n else "n/a")
        tag = RECALL_LABEL[t] + (" *diag" if t == "src_examples" else "")
        print(f"  {tag:12}  {cells[0]:>14}  {cells[1]:>14}")

    print("\n[E] Categorization detail")
    cat = table_categorization(units, metrics)
    for src in SOURCES:
        d = cat[src]
        print(f"  [{src}] example classification: {d['example_ok']}/{d['example_n']} "
              f"into examples segment; {d['example_def']} misplaced to prose")
        mis = "  ".join(f"{k}={v[0]}/{v[1]}" for k, v in d["placement"].items())
        print(f"        placement misplacements: {mis}")

    print("\n[F] Per-library construct means (F1/F2/F3)")
    pl = table_per_library(metrics)
    print(f"  {'library':11} {'web (F1/F2/F3)':>26}  {'pdf (F1/F2/F3)':>26}")
    for lib in LIBS:
        cells = []
        for src in SOURCES:
            d = pl[lib][src]
            cells.append(f"{d['f1_faithfulness']:.3f}/{d['f2_categorization']:.3f}/{d['f3_completeness']:.3f} (n={d['n']})")
        print(f"  {lib:11} {cells[0]:>26}  {cells[1]:>26}")


# --------------------------------------------------------------------------- #
def latex_tables(metrics, units, recall):
    L = []
    A = table_constructs(metrics)

    def pct(x):
        return f"{x:.1f}"

    # Table 1: headline
    L.append(r"\begin{table}[t]")
    L.append(r"\centering")
    L.append(r"\caption{Trace-unit fidelity of the structured documentation, conditioned on "
             r"correctly recovered trace links. Values are mean construct score with the "
             r"percentage of members scoring perfectly ($=1.0$) in parentheses.}")
    L.append(r"\label{tab:fidelity-headline}")
    L.append(r"\begin{tabular}{lccc}")
    L.append(r"\toprule")
    L.append(r"Source & Faithfulness & Categorization & Completeness \\")
    L.append(r"\midrule")
    for r in A:
        L.append(f"{r['source'].upper()} ($n{{=}}{r['n']}$) & "
                 f"{r['f1_faithfulness_mean']:.3f} ({pct(r['f1_faithfulness_perfect'])}\\%) & "
                 f"{r['f2_categorization_mean']:.3f} ({pct(r['f2_categorization_perfect'])}\\%) & "
                 f"{r['f3_completeness_mean']:.3f} ({pct(r['f3_completeness_perfect'])}\\%) \\\\")
    L.append(r"\bottomrule")
    L.append(r"\end{tabular}")
    L.append(r"\end{table}")
    L.append("")

    # Table 2: faithfulness bands
    by_src, _ = table_faith_bands(units)
    L.append(r"\begin{table}[t]")
    L.append(r"\centering")
    L.append(r"\caption{Distribution of structured trace units by similarity to their source "
             r"span. The Cosmetic band captures normalization/glyph drift (a measurement "
             r"artifact); only the Paraphrase and lower bands are genuine fidelity defects.}")
    L.append(r"\label{tab:fidelity-bands}")
    L.append(r"\begin{tabular}{lrr}")
    L.append(r"\toprule")
    L.append(r"Similarity band & WEB (\%) & PDF (\%) \\")
    L.append(r"\midrule")
    for name, _ in FAITH_BANDS:
        cells = []
        for src in SOURCES:
            c, n = by_src[src]
            k = c.get(name, 0)
            cells.append(f"{100*k/n:.1f}")
        nm = name.replace(">=", "$\\geq$").replace("<", "$<$")
        L.append(f"{nm} & {cells[0]} & {cells[1]} \\\\")
    L.append(r"\bottomrule")
    L.append(r"\end{tabular}")
    L.append(r"\end{table}")
    L.append("")

    # Table 3: completeness by trace unit
    rec = table_recall(recall)
    L.append(r"\begin{table}[t]")
    L.append(r"\centering")
    L.append(r"\caption{Completeness: percentage of source-documented trace units represented "
             r"in the structured doc. Examples are reported as a diagnostic only "
             r"(PDF pages concatenate inherited members, inflating apparent omission).}")
    L.append(r"\label{tab:fidelity-completeness}")
    L.append(r"\begin{tabular}{lrr}")
    L.append(r"\toprule")
    L.append(r"Trace unit & WEB (\%) & PDF (\%) \\")
    L.append(r"\midrule")
    for t in RECALL_TYPES:
        cells = []
        for src in SOURCES:
            cov, n = rec[src][t]
            cells.append(f"{100*cov/n:.1f}" if n else "--")
        L.append(f"{RECALL_LABEL[t]} & {cells[0]} & {cells[1]} \\\\")
    L.append(r"\midrule")
    ce = []
    for src in SOURCES:
        cov, n = rec[src]["src_examples"]
        ce.append(f"{100*cov/n:.1f}" if n else "--")
    L.append(f"Examples (diag.) & {ce[0]} & {ce[1]} \\\\")
    L.append(r"\bottomrule")
    L.append(r"\end{tabular}")
    L.append(r"\end{table}")
    L.append("")

    # Table 4: per-library
    pl = table_per_library(metrics)
    L.append(r"\begin{table}[t]")
    L.append(r"\centering")
    L.append(r"\caption{Per-library mean sub-construct scores (Faithfulness / Categorization / "
             r"Completeness) on correctly recovered trace links.}")
    L.append(r"\label{tab:fidelity-perlib}")
    L.append(r"\begin{tabular}{lccc|ccc}")
    L.append(r"\toprule")
    L.append(r"& \multicolumn{3}{c|}{WEB} & \multicolumn{3}{c}{PDF} \\")
    L.append(r"Library & F1 & F2 & F3 & F1 & F2 & F3 \\")
    L.append(r"\midrule")
    for lib in LIBS:
        w, p = pl[lib]["web"], pl[lib]["pdf"]
        L.append(f"{lib} & {w['f1_faithfulness']:.2f} & {w['f2_categorization']:.2f} & "
                 f"{w['f3_completeness']:.2f} & {p['f1_faithfulness']:.2f} & "
                 f"{p['f2_categorization']:.2f} & {p['f3_completeness']:.2f} \\\\")
    L.append(r"\bottomrule")
    L.append(r"\end{tabular}")
    L.append(r"\end{table}")
    return "\n".join(L)


def main():
    metrics = load("trace_fidelity_metrics.csv")
    units = load("trace_fidelity_units.csv")
    recall = load("trace_fidelity_recall.csv")
    console_report(metrics, units, recall)
    tex = latex_tables(metrics, units, recall)
    out = os.path.join(TESTS, "trace_fidelity_tables.tex")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(tex + "\n")
    print(f"\nwrote LaTeX tables -> {out}")


if __name__ == "__main__":
    main()
