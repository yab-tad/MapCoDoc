"""
Merge DB-grounded audit + web-grounded webcheck into FINAL review CSVs.

For each (lib, source):
  - base = <lib>_<source>_ground_truth_audit.csv (DB-grounded explicit columns)
  - override correct_doc with the WEB-grounded verdict (webcheck <source>_correct) for
    the (review+no) target members the web step actually resolved (yes/no).
  - members the web step could not resolve stay as their DB verdict (review).
  - DB-confirmed 'yes' members (not in target) are unchanged.

Writes tests/<lib>_<source>_fidelity_review.csv  (overwrites; explicit columns + comment)
"""
import csv, os, json
from collections import Counter

TESTS = os.path.dirname(os.path.abspath(__file__))
LIBS = ["requests", "numpy", "pandas", "sklearn", "sqlalchemy", "torch", "xgboost"]
SOURCES = ["web", "pdf"]
OUT_COLS = ["api_name", "member_type", "fidelity_group", "preprocessed_accurate",
            "structured_vs_preprocessed", "structured_vs_original_db", "structured_vs_original_web",
            "correct_doc", "source_of_truth", "scope", "root_cause_layer", "comment"]

# Manual adjudications from reading DB docstring + structured + cached web original.
# verdict 'yes' is gated on the per-source structured extraction being non-empty.
MANUAL = {
 ("sqlalchemy","sqlalchemy.orm.composite"): ("yes","web-extractor artifact; structured matches DB summary 'Return a composite column-based property for use with a Mapper'"),
 ("sqlalchemy","sqlalchemy.orm.declared_attr.directive"): ("yes","structured matches source 'Mark a declared_attr as decorating a Declarative directive'"),
 ("sqlalchemy","sqlalchemy.orm.declared_attr.cascading"): ("yes","structured matches source 'Mark a declared_attr as cascading'"),
 ("xgboost","xgboost.XGBRFClassifier"): ("yes","structured = correct 'scikit-learn API for XGBoost random forest classification'"),
 ("xgboost","xgboost.XGBRFRegressor"): ("yes","structured = correct 'scikit-learn API for XGBoost random forest regression'"),
 ("xgboost","xgboost.XGBRanker.apply"): ("yes","structured = correct 'Return the predicted leaf...'; web extractor grabbed a fragment"),
 ("sqlalchemy","sqlalchemy.orm.QueryContext"): ("no","wrong-member: structured holds InstrumentedAttribute's descriptor doc; QueryContext has no own description in source"),
 ("sqlalchemy","sqlalchemy.orm.Session.close"): ("no","right member but purpose captured a usage note, not the authoritative summary 'Close out the transactional resources...'"),
 ("sqlalchemy","sqlalchemy.orm.Mapper.all_orm_descriptors"): ("no","thin: only signature captured; missing 'A namespace of all InspectionAttr attributes...'"),
 ("sqlalchemy","sqlalchemy.orm.InstanceState.was_deleted"): ("no","signature-only; missing description"),
 ("sqlalchemy","sqlalchemy.orm.NotExtension"): ("review","no docstring in source (only base-class line); structured faithfully minimal - nothing to verify"),
 ("sqlalchemy","sqlalchemy.orm.UOWTransaction"): ("review","internal class, no docstring in source; nothing substantive to verify"),
 ("sqlalchemy","sqlalchemy.orm.LoaderCallableStatus"): ("review","Enum, no docstring in source; nothing to verify"),
 ("numpy","numpy.lib.format.isfileobj"): ("review","no docstring in source (tiny internal util); nothing to verify"),
 ("numpy","numpy.polynomial.chebyshev.Chebyshev.domain"): ("review","class attribute (default domain value); no prose doc in source"),
 ("numpy","numpy.polynomial.laguerre.Laguerre.domain"): ("review","class attribute (default domain value); no prose doc in source"),
 ("numpy","numpy.polynomial.polynomial.Polynomial.domain"): ("review","class attribute (default domain value); no prose doc in source"),
 ("pandas","pandas.ExcelFile.sheet_names"): ("review","property; no/minimal docstring in source to verify"),
 ("pandas","pandas.IntervalIndex.length"): ("review","property; no/minimal docstring in source to verify"),
}

def struct_purpose_for(lib, src, api):
    p = os.path.join(TESTS, f"{lib}_fidelity_collection.csv")
    stem = bucket = None
    for r in csv.DictReader(open(p, newline="", encoding="utf-8")):
        if r["sample_api_name"] == api and r["source"] == src:
            stem, bucket = r["stem"], r["bucket"]; break
    if not stem:
        return ""
    base = os.path.join(COLLECT_BASE, lib, src)
    if not os.path.isdir(base) and os.path.isdir(os.path.join(COLLECT_BASE, lib, lib, src)):
        base = os.path.join(COLLECT_BASE, lib, lib, src)
    jp = os.path.join(base, bucket, stem + ".json")
    if not os.path.exists(jp):
        return ""
    try:
        j = json.load(open(jp, encoding="utf-8"))
    except Exception:
        return ""
    return ((j.get("module_member_description") or {}).get("purpose") or "")

COLLECT_BASE = os.environ.get("MAPCODOC_COLLECT_BASE", "collected_fidelity")

def load_webcheck(lib):
    p = os.path.join(TESTS, f"{lib}_webcheck.csv")
    out = {}
    if os.path.exists(p):
        for r in csv.DictReader(open(p, newline="", encoding="utf-8")):
            out[r["api_name"]] = r
    return out

def comment(final, src, audit, wc):
    summ = (wc.get("original_summary") if wc else "") or ""
    if wc and final in ("yes", "no"):
        if final == "yes":
            return f"web-confirmed correct vs original: \"{summ[:90]}\""
        sacc = wc.get(f"{src}_struct_acc")
        if sacc == "empty":
            return f"web-confirmed wrong: empty/retrieval failure; original: \"{summ[:80]}\""
        return f"web-confirmed wrong vs original (retrieval/wrong-member): original says \"{summ[:80]}\""
    if final == "review":
        return "unresolved: neither DB docstring nor web original yielded a usable reference"
    # DB-confirmed yes (not a target member)
    return "faithful: DB-confirmed match to authoritative docstring/signature"

def main():
    grand = Counter()
    for lib in LIBS:
        wcheck = load_webcheck(lib)
        for src in SOURCES:
            ap = os.path.join(TESTS, f"{lib}_{src}_ground_truth_audit.csv")
            if not os.path.exists(ap):
                continue
            rows_out = []
            cnt = Counter()
            for r in csv.DictReader(open(ap, newline="", encoding="utf-8")):
                api = r["api_name"]
                db_correct = r["correct_doc"]
                wc = wcheck.get(api)
                web_verdict = wc.get(f"{src}_correct") if wc else ""   # yes/no/review/'' 
                # decide final verdict + source of truth
                if wc and web_verdict in ("yes", "no"):
                    final, sot = web_verdict, "web"          # web original resolved it
                elif db_correct in ("yes", "no"):
                    final, sot = db_correct, "db"            # fall back to DB ground truth
                else:
                    final, sot = "review", "db"              # neither DB nor web could verify
                man = MANUAL.get((lib, api))
                man_comment = None
                if man:
                    mv, mc = man
                    if mv == "yes":
                        pp = (struct_purpose_for(lib, src, api) or "").strip().lower()
                        if pp in ("", "n/a", "na", "none"):
                            final, sot, man_comment = "no", "manual", "empty extraction for this source though source has a real doc"
                        else:
                            final, sot, man_comment = "yes", "manual", mc
                    else:
                        final, sot, man_comment = mv, "manual", mc
                if final == "yes":
                    root = "none"
                elif final == "review":
                    root = "unresolved"
                else:
                    root = r["root_cause_layer"] if r["root_cause_layer"] not in ("none", "") else "retrieval"
                rows_out.append({
                    "api_name": api, "member_type": r["member_type"], "fidelity_group": r["fidelity_group"],
                    "preprocessed_accurate": r["preprocessed_accurate"],
                    "structured_vs_preprocessed": r["structured_vs_preprocessed"],
                    "structured_vs_original_db": r["structured_vs_original"],
                    "structured_vs_original_web": (wc.get(f"{src}_struct_acc") if wc else ""),
                    "correct_doc": final, "source_of_truth": sot, "scope": r["scope"],
                    "root_cause_layer": root,
                    "comment": (("manual-confirmed: " + man_comment) if man_comment else comment(final, src, r, wc)),
                })
                cnt[final] += 1
            outp = os.path.join(TESTS, f"{lib}_{src}_fidelity_review.csv")
            with open(outp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=OUT_COLS); w.writeheader(); w.writerows(rows_out)
            grand.update(cnt)
            print(f"{lib}/{src}: {sum(cnt.values()):4d}  yes={cnt.get('yes',0):4d} no={cnt.get('no',0):4d} review={cnt.get('review',0):3d}")
    print("\nGRAND TOTAL:", dict(grand))

if __name__ == "__main__":
    main()
