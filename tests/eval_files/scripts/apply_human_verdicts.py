"""
Record HUMAN manual-validation verdicts onto the review CSVs (and patch the matching
eval sidecars in collected_verification).

This study's trace-link recovery accuracy is a FULL MANUAL CENSUS: the human reviewed
every member the triage classified as correct (and every one it flagged as incorrect,
reverting the few misclassifications). This script therefore:

  1. Snapshots the pre-human automated verdict once into  correct_doc_auto  (preserved
     for the triage-vs-human comparison; never overwritten on re-runs).
  2. Sets  correct_doc  to the FINAL MANUAL trace-link verdict:
        human 'yes'/'partial' -> 'yes'   (the correct member's doc WAS recovered;
                                           'partial' carries a fidelity caveat only)
        human 'no'            -> 'no'
     For members without an explicit VERDICTS entry, correct_doc already equals the
     human census (every 'yes' was confirmed; every 'no' was confirmed bar the listed
     reverts), so it is left as-is.
  3. Records the bulk census into  human_verified  for ALL rows:
        explicit VERDICTS rows keep their nuanced call (yes|no|partial) + human_note;
        all other rows get  human_verified = correct_doc  (yes|no).
  4. Fixes root_cause_layer made inconsistent by the flips (a row that is now a correct
     trace-link cannot also be a retrieval/structuring FAILURE).

Columns added if missing:
  correct_doc_auto : the original automated/LLM trace-link verdict (provenance)
  human_verified   : yes | no | partial      (human reviewer's call)
  human_note       : free-text reviewer note

Extend VERDICTS as you validate more members, then re-run:
  python tests/apply_human_verdicts.py
Then refresh indices:
  python tests/build_verification_index.py [output_dir]
"""
import csv, os, sys

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import web_verify as V  # for COLLECT_BASE (eval sidecar location)

NEW = ["human_verified", "human_note"]
AUTO_SNAP = "correct_doc_auto"


def target_cd(hv):
    """Map a human verdict to the binary trace-link label used in the accuracy table.
    'partial' means the correct member's doc WAS recovered (link=yes) with a fidelity
    caveat, so it counts as 'yes' for trace-link recovery."""
    return "yes" if hv in ("yes", "partial") else "no"

# (library, source, api_name) -> (human_verified, human_note)
VERDICTS = {
    ("xgboost", "web", "xgboost.XGBRanker.apply"): (
        "yes",
        "Human-verified accurate. preprocessed and structured both accurate. The returns field "
        "omits the name 'X_leaves' because the returns schema carries a TYPE field but no NAME "
        "field; return name is usually redundant vs type, but this is a noted structured-doc SCHEMA limitation."),
    ("xgboost", "web", "xgboost.XGBRFClassifier"): (
        "yes",
        "Human-verified accurate. Class doc correctly extracted including methods/attributes to the "
        "extent present in preprocessed. NOTE: method/attribute docs are partially truncated in the "
        "preprocessed_doc due to the extractor MAX-LENGTH limit; the class doc itself is present and "
        "correct (the truncation of member docs may be why it was flagged)."),
    ("xgboost", "web", "xgboost.XGBRFRegressor"): (
        "yes",
        "Human-verified accurate. Same as XGBRFClassifier: class doc correct; method/attribute docs "
        "partially truncated in preprocessed_doc due to the extractor MAX-LENGTH limit; class doc present and correct."),

    ("xgboost", "pdf", "xgboost.XGBRanker.apply"): (
        "yes",
        "Human-verified accurate. Same as web counterpart: accurate except the returns NAME (X_leaves) is "
        "absent because the returns schema has a type field but no name field (noted structured-doc SCHEMA limitation)."),
    ("xgboost", "pdf", "xgboost.XGBRFClassifier"): (
        "yes",
        "Human-verified accurate. Same as web counterpart: preprocessed text truncation (extractor MAX-LENGTH limit) "
        "may omit some of the class's method docs - NOT a retrieval-accuracy or structuring problem; class doc correct."),
    ("xgboost", "pdf", "xgboost.XGBRFRegressor"): (
        "yes",
        "Human-verified accurate. Same as XGBRFClassifier (pdf): class doc correct; some class-method docs omitted due to "
        "preprocessed text truncation (extractor MAX-LENGTH limit) - not a retrieval or structuring accuracy problem."),

    # numpy / web
    ("numpy", "web", "numpy.lib.format.isfileobj"): (
        "yes", "Human-verified accurate. Both preprocessed and structured docs accurate."),
    ("numpy", "web", "numpy.ma.array"): (
        "yes", "Human-verified accurate. Both preprocessed and structured docs accurate."),
    ("numpy", "web", "numpy.ma.inner"): (
        "yes", "Human-verified accurate. Both preprocessed and structured docs accurate."),
    ("numpy", "web", "numpy.polynomial.chebyshev.Chebyshev.domain"): (
        "yes",
        "Human-verified accurate. Both preprocessed and structured correct. Member is a class ATTRIBUTE; the structured "
        "doc finds no signature for it so the extraction looks out of place - a structured-doc SCHEMA limitation for "
        "signature-less class attributes, not an extraction/retrieval error."),
    ("numpy", "web", "numpy.polynomial.polynomial.Polynomial.domain"): (
        "yes",
        "Human-verified accurate. Same as Chebyshev.domain: correct extraction; class ATTRIBUTE with no signature in the "
        "structured doc (schema limitation), not an extraction error."),
    ("numpy", "web", "numpy.polynomial.laguerre.Laguerre.domain"): (
        "partial",
        "Human-verified: doc correctly traced (preprocessed accurate) BUT structured doc is INCOMPLETE - it only records "
        "that it's an attribute and misses some information. Signature-less class attributes sit awkwardly in the structured "
        "schema; retrieval is fine, structured extraction is incomplete."),

    # numpy / pdf  (class attributes - NOT accurately extracted at either stage)
    ("numpy", "pdf", "numpy.lib.format.isfileobj"): (
        "no", "Human-verified: NOT accurately extracted (both preprocessed and structured) for pdf."),
    ("numpy", "pdf", "numpy.polynomial.chebyshev.Chebyshev.domain"): (
        "no", "Human-verified: class attribute NOT accurately extracted (both preprocessed and structured) for pdf."),
    ("numpy", "pdf", "numpy.polynomial.laguerre.Laguerre.domain"): (
        "no", "Human-verified: class attribute NOT accurately extracted (both preprocessed and structured) for pdf."),
    ("numpy", "pdf", "numpy.polynomial.polynomial.Polynomial.domain"): (
        "no", "Human-verified: class attribute NOT accurately extracted (both preprocessed and structured) for pdf."),

    # pandas / web
    ("pandas", "web", "pandas.ExcelFile.sheet_names"): (
        "yes", "Human-verified accurate (both preprocessed and structured)."),
    ("pandas", "web", "pandas.IntervalIndex.length"): (
        "yes", "Human-verified accurate (both preprocessed and structured)."),

    # pandas / pdf
    ("pandas", "pdf", "pandas.ExcelFile.sheet_names"): (
        "yes", "Human-verified accurate (both preprocessed and structured)."),
    ("pandas", "pdf", "pandas.IntervalIndex.length"): (
        "no", "Human-verified: neither the preprocessed nor the structured doc was extracted (pdf)."),

    # sqlalchemy / web
    ("sqlalchemy", "web", "sqlalchemy.orm.NotExtension"): (
        "yes",
        "Human-verified accurate (both). Class attributes appear twice in the original/preprocessed (table summary + "
        "standalone); the structured doc documented BOTH (duplicates) under the attributes field."),
    ("sqlalchemy", "web", "sqlalchemy.orm.LoaderCallableStatus"): (
        "yes",
        "Human-verified accurate (both). Class attributes appear twice (table + standalone); structured extracted only the "
        "standalone attribute docs into attributes (no duplicates)."),
    ("sqlalchemy", "web", "sqlalchemy.orm.UOWTransaction"): (
        "yes",
        "Human-verified accurate (both). Extracted standalone method docs (not the table summaries) into methods. ALSO "
        "captured extraneous page footer/navigation as supplementary_information (minor additive/web-chrome)."),
    ("sqlalchemy", "web", "sqlalchemy.orm.RelationshipProperty.Comparator"): (
        "no", "Human-verified: incorrect preprocessed extraction and, consequently, incorrect structured doc."),
    ("sqlalchemy", "web", "sqlalchemy.orm.InstrumentedAttribute"): (
        "yes",
        "Human-verified: preprocessed correct; structured content accurate EXCEPT the class signature is 'Name()' (empty "
        "parens) instead of 'class <api_name>'. Treat as correct retrieval / doc-link; fidelity takes a hit on the signature field only."),

    # sqlalchemy / pdf
    ("sqlalchemy", "pdf", "sqlalchemy.orm.NotExtension"): (
        "yes",
        "Human-verified accurate (both). Signature rendered as 'Name()' instead of 'class <api_name>', with the intended "
        "signature text placed in member_description.purpose. Two attribute descriptions (table + standalone) both documented under attributes."),
    ("sqlalchemy", "pdf", "sqlalchemy.orm.LoaderCallableStatus"): (
        "yes",
        "Human-verified accurate (both). Signature 'Name()' instead of 'class <api_name>' (api in purpose). Structured ignored "
        "the table-summary attributes and documented the standalone attribute docs (no duplicates)."),
    ("sqlalchemy", "pdf", "sqlalchemy.orm.UOWTransaction"): (
        "yes",
        "Human-verified accurate (both). Structured ignored table-summary attributes and documented the standalone attribute docs (no duplicates)."),

    # sqlalchemy / pdf  (mode_sample)
    ("sqlalchemy", "pdf", "sqlalchemy.orm.RelationshipProperty.Comparator"): (
        "no", "Human-verified: preprocessed inaccurate; structured not extracted."),
    ("sqlalchemy", "pdf", "sqlalchemy.orm.InstrumentedAttribute"): (
        "yes", "Human-verified accurate (both preprocessed and structured)."),
    ("sqlalchemy", "pdf", "sqlalchemy.orm.Session.rollback"): (
        "no", "Human-verified: preprocessed inaccurate; consequently the structured doc is also incorrect."),
    ("sqlalchemy", "pdf", "sqlalchemy.orm.QueryContext"): (
        "partial",
        "Human-verified: preprocessed accurate; structured only extracted the signature (other fields empty). Doc-link/signature "
        "recovered correctly, but the structured doc is incomplete so fidelity takes a hit."),
    ("sqlalchemy", "pdf", "sqlalchemy.orm.Mapper.all_orm_descriptors"): (
        "yes",
        "Human-verified accurate (both). Structured tends to place all member_description text under purpose (tolerable); the "
        "signature holds only the attribute short-name instead of 'attribute <api_name>'. Correct doc-link/retrieval; fidelity hit on the signature field."),

    # pandas / web
    ("pandas", "web", "pandas.DataFrame.cummin"): (
        "yes", "Human-verified accurate (both). Usage examples are all collapsed under a single example field in the structured doc."),
    ("pandas", "web", "pandas.Series.cummin"): (
        "yes", "Human-verified accurate (both). Usage examples are all collapsed under a single example field in the structured doc."),
    ("pandas", "web", "pandas.Series.cumsum"): (
        "yes", "Human-verified accurate (both). Usage examples are split into their own example fields in the structured doc."),
    ("pandas", "web", "pandas.DataFrame.swaplevel"): (
        "yes", "Human-verified accurate (both). Usage examples are split into their own example fields in the structured doc."),

    # pandas / pdf
    ("pandas", "pdf", "pandas.DataFrame.cummin"): (
        "yes", "Human-verified accurate (both). Usage examples are all collapsed under a single example field in the structured doc."),
    ("pandas", "pdf", "pandas.Series.cummin"): (
        "yes", "Human-verified accurate (both). Usage examples are all collapsed under a single example field in the structured doc."),
    ("pandas", "pdf", "pandas.Series.cumsum"): (
        "yes", "Human-verified accurate (both). Usage examples are split into their own example fields in the structured doc."),
    ("pandas", "pdf", "pandas.DataFrame.swaplevel"): (
        "yes", "Human-verified accurate (both). Usage examples are split into their own example fields in the structured doc."),
    ("pandas", "pdf", "pandas.core.window.expanding.Expanding.max"): (
        "no",
        "Human-verified: incorrect preprocessed extraction (and consequently structured). Anchored to a same-signature doc from a "
        "DIFFERENT class (signature matches except the class name in the API path)."),
    ("pandas", "pdf", "pandas.core.window.expanding.Expanding.sum"): (
        "no",
        "Human-verified: incorrect preprocessed extraction (and consequently structured). Anchored to a same-signature doc from a "
        "DIFFERENT class (signature matches except the class name in the API path)."),

    # requests / web
    ("requests", "web", "requests.Session.request"): (
        "yes", "Human-verified accurate (both preprocessed and structured)."),
    ("requests", "web", "requests.Session.prepare_request"): (
        "yes", "Human-verified accurate (both preprocessed and structured)."),

    # requests / pdf
    ("requests", "pdf", "requests.Session.request"): (
        "no", "Human-verified: preprocessed inaccurate; consequently the structured doc is also incorrect."),

    # sklearn / web
    ("sklearn", "web", "sklearn.linear_model.LogisticRegressionCV.set_score_request"): (
        "yes", "Human-verified accurate (both preprocessed and structured)."),
    ("sklearn", "web", "sklearn.gaussian_process.kernels.Exponentiation.bounds"): (
        "partial",
        "Human-verified: correct doc retrieved, but anchoring/extraction started halfway down and dropped the first half of the "
        "relevant content; the structured doc inherited this premature/truncated start."),
    ("sklearn", "web", "sklearn.model_selection.ValidationCurveDisplay.from_estimator"): (
        "yes",
        "Human-verified accurate (both) AFTER a structured-extractor bug (which had left it missing) was fixed by the user."),

    # sklearn / pdf
    ("sklearn", "pdf", "sklearn.gaussian_process.kernels.Exponentiation.requires_vector_input"): (
        "no",
        "Human-verified: preprocessed inaccurate - retrieved a same-named property from a DIFFERENT class/description; structured doc consequently inaccurate."),

    # torch / web
    ("torch", "web", "torch.autograd.profiler_util.Interval"): (
        "partial",
        "Human-verified: preprocessed accurate and structured retrieved the right doc, BUT structured fabricated parameter entries "
        "from the signature (the original doc lists no parameters) - additive content."),
    ("torch", "web", "torch.Tensor.split"): (
        "yes",
        "Human-verified accurate (both). Structured omitted one cross-reference line ('See torch.split()'); otherwise faithful."),
    ("torch", "web", "torch.autograd.profiler.parse_nvprof_trace"): (
        "yes", "Human-verified accurate (both preprocessed and structured)."),
    ("torch", "web", "torch.functional.align_tensors"): (
        "yes", "Human-verified accurate (both preprocessed and structured)."),
    ("torch", "web", "torch.Tensor.storage"): (
        "yes", "Human-verified accurate (both preprocessed and structured)."),
    ("torch", "web", "torch.Tensor.norm"): (
        "yes", "Human-verified accurate (both preprocessed and structured)."),
    ("torch", "web", "torch.autograd.graph.Node.name"): (
        "partial",
        "Human-verified: preprocessed accurate; structured retrieved the right doc but misplaced the code examples into "
        "module_member_description.additional_information, and the signature is missing the 'abstract' prefix present in the original."),
    ("torch", "web", "torch.nn.utils.prune.CustomFromMask"): (
        "partial",
        "Human-verified: preprocessed accurate (includes the class's classmethod/method docs); structured retrieved the same doc but "
        "(1) dropped the 'class <api_name>' prefix from the signature (uses 'Name(params)'), (2) misattributed the classmethod's "
        "description as the class's own purpose, and (3) misattributed the classmethod's parameters as its own. It DID correctly "
        "classify the class's methods into the methods field."),

    # torch / pdf
    ("torch", "pdf", "torch.autograd.profiler_util.Interval"): (
        "yes",
        "Human-verified accurate (both). Structured echoed the signature into the purpose field (likely a short-doc-without-description "
        "quirk) and correctly classified the class method into the methods field."),
    ("torch", "pdf", "torch.autograd.profiler.parse_nvprof_trace"): (
        "yes", "Human-verified accurate (both preprocessed and structured)."),
    ("torch", "pdf", "torch.func.stack_module_state"): (
        "partial",
        "Human-verified: correct doc retrieved, but structured placed code examples in module_member_description.additional_information, "
        "inferred a parameter name from the signature (with a description copied from purpose), and inferred a returns description copied "
        "from purpose - additive/misplaced content."),
    ("torch", "pdf", "torch.functional.align_tensors"): (
        "no",
        "Human-verified failure. NOTE: user listed this member twice with conflicting descriptions ('no docs extracted' AND 'incorrect "
        "docs extracted'); recorded as 'no' since both indicate failure - confirm whether one line was meant for a different member."),
    ("torch", "pdf", "torch.nn.functional.tanh"): (
        "partial",
        "Human-verified: correct doc retrieved, but structured inferred parameter and returns info from the signature and placed them in "
        "their fields, whereas the original/preprocessed docs document those only within the signature - additive content."),
}

def review_path(lib, src):
    return os.path.join(TESTS, f"{lib}_{src}_fidelity_review.csv")

def ensure_cols_and_apply():
    # group verdicts by file
    files = {}
    for (lib, src, api), v in VERDICTS.items():
        files.setdefault((lib, src), {})[api] = v
    touched = 0       # explicit VERDICTS rows updated
    bulk = 0          # rows whose human_verified was filled from the census
    stranded = []     # rows still 'review' with no explicit verdict (should be none)
    import glob
    for p in glob.glob(os.path.join(TESTS, "*_fidelity_review.csv")):
        with open(p, newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            cols = list(rd.fieldnames)
            rows = list(rd)
        # snapshot the automated verdict ONCE, the first time this column is introduced
        snap_now = AUTO_SNAP not in cols
        if snap_now:
            cols.append(AUTO_SNAP)
        for c in NEW:
            if c not in cols:
                cols.append(c)
        base = os.path.basename(p)[:-len("_fidelity_review.csv")]
        lib, src = base.rsplit("_", 1)
        vmap = files.get((lib, src), {})
        for r in rows:
            if snap_now:
                r[AUTO_SNAP] = r.get("correct_doc", "")
            for c in NEW:
                r.setdefault(c, r.get(c, ""))
            api = r["api_name"]
            if api in vmap:
                hv, note = vmap[api]
                r["human_verified"], r["human_note"] = hv, note
                r["correct_doc"] = target_cd(hv)
                # repair root_cause_layer made inconsistent by the flip
                if r["correct_doc"] == "yes":
                    if r.get("root_cause_layer") not in ("none", ""):
                        r["root_cause_layer"] = "none"
                else:
                    if r.get("root_cause_layer") in ("unresolved", "review", "", None):
                        r["root_cause_layer"] = "retrieval"
                touched += 1
            else:
                # bulk census: fill human_verified from the (manual) correct_doc label
                if not r.get("human_verified"):
                    cd = r.get("correct_doc", "")
                    if cd in ("yes", "no"):
                        r["human_verified"] = cd
                        bulk += 1
                    elif cd == "review":
                        stranded.append((lib, src, api))
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in cols})
    if stranded:
        print(f"WARNING: {len(stranded)} row(s) still 'review' with no explicit verdict "
              f"(human_verified left blank):")
        for s in stranded:
            print("   ", s)
    return touched, bulk

def patch_eval_sidecars():
    """Append a HUMAN VERIFICATION block to the member's eval.txt (best-effort)."""
    # locate stem via collection manifest
    patched = 0
    for (lib, src, api), (hv, note) in VERDICTS.items():
        man = {}
        mp = os.path.join(TESTS, f"{lib}_fidelity_collection.csv")
        for r in csv.DictReader(open(mp, newline="", encoding="utf-8")):
            if r["sample_api_name"] == api and r["source"] == src:
                man = r; break
        if not man:
            continue
        stem = man["stem"]
        # tier from review CSV
        tier = None
        for r in csv.DictReader(open(review_path(lib, src), newline="", encoding="utf-8")):
            if r["api_name"] == api:
                tier = r.get("verify_tier"); break
        if not tier:
            continue
        ev = os.path.join(os.path.dirname(V.COLLECT_BASE), "collected_verification",
                          lib, src, tier, stem + ".eval.txt")
        if os.path.exists(ev):
            marker = "--- HUMAN VERIFICATION ---"
            body = open(ev, encoding="utf-8").read()
            # idempotent: drop any prior human-verification block before re-appending
            if marker in body:
                body = body[:body.index(marker)].rstrip()
            with open(ev, "w", encoding="utf-8") as f:
                f.write(body + f"\n\n{marker}\nhuman_verified: {hv}\nhuman_note: {note}\n")
            patched += 1
    return patched

if __name__ == "__main__":
    n, bulk = ensure_cols_and_apply()
    p = patch_eval_sidecars()
    print(f"applied {n} explicit verdict(s); filled {bulk} census row(s) into human_verified; "
          f"patched {p} eval sidecar(s)")
