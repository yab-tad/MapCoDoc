"""
DB-grounded 3-layer fidelity auditor.
=====================================
For every sampled member of a library/source, cross-examines the pipeline output
against the AUTHORITATIVE ground truth in the library database (the Sphinx-source
docstring + signature variants + parameters), establishing three comparisons:

  preprocessed_accurate    retrieved doc  vs  DB ground truth   (retrieval correctness)
  structured_vs_preprocessed  structured  vs  retrieved doc      (structuring fidelity)
  structured_vs_original      structured  vs  DB ground truth    (end-to-end correctness)

It also localizes the fault layer (retrieval | structuring | none) and estimates
scope (how much of the member's real doc the structured output captured).

Usage:  python audit_ground_truth.py <library> <source>
Reads:  tests/<library>_fidelity_collection.csv      (member universe + stem + bucket)
        collected_fidelity/<library>/<source>/<bucket>/<stem>.{txt,json}
        mapcodoc_output/<db file for library>
Writes: tests/<library>_<source>_ground_truth_audit.csv
"""
import csv, json, os, re, sys
from rapidfuzz import fuzz

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from mapcodoc_db.db_manager import MapCoDocDB
from mapcodoc_db.query import QueryManager

COLLECT_BASE = os.environ.get("MAPCODOC_COLLECT_BASE", "collected_fidelity")
TESTS = os.path.dirname(os.path.abspath(__file__))

DB_FILES = {
    "requests": "requests_2.32.5.db", "numpy": "numpy_2.4.0.db",
    "pandas": "pandas_10.9.db", "sklearn": "sklearn_1.8.0.db",
    "sqlalchemy": "sqlalchemy_2.0.45.db", "torch": "torch_2.9.1.db",
    "xgboost": "xgboost_3.2.0.db"
}

_PLACE = re.compile(r"\(?url_placeholder_\d+\)?", re.I)
_ROLE = re.compile(r":[a-z:]+:`~?([^`]+)`")      # :class:`~requests.Response` -> Response path
_ALNUM = re.compile(r"[^a-z0-9]+")
TRIVIAL = {"", "n/a", "na", "none"}

def usable_truth(summary: str) -> bool:
    """Whether the DB docstring summary is a usable ground truth (not empty,
    not an autodoc placeholder, not a bare cross-reference stub)."""
    s = (summary or "").strip()
    sn = norm(s)
    if len(sn) < 8:
        return False
    low = s.lower()
    if "docstring will be overwritten" in low or low.startswith("alias of") or low.startswith("see also"):
        return False
    # bare cross-ref stub like "See split" / "See :func:`x`"
    if re.match(r"^see\b", low) and len(s.split()) <= 4:
        return False
    return True

def _role_text(m):
    inner = m.group(1).split("<")[0].strip()  # display text before " <target>"
    return inner.split(".")[-1]

def clean_rst(s: str) -> str:
    """Reduce a raw Sphinx/RST docstring to presentation-neutral prose so it can be
    compared to rendered web/pdf text (which differ in urls, cross-refs, directives,
    page markers, glyphs). We keep semantic words, drop markup."""
    s = s or ""
    s = _ROLE.sub(_role_text, s)                       # :class:`Response <X>` -> Response
    s = re.sub(r"\.\.\s+\w+::[^\n]*", " ", s)          # .. note:: / .. versionadded:: 1.0 ...
    s = re.sub(r":param\s+[^:]+:", " ", s)             # :param url:
    s = re.sub(r":(rtype|returns?|raises?|type|meta|ivar|var|cvar|keyword)\s*[^:]*:", " ", s)
    s = re.sub(r":[a-z]+:", " ", s)                    # leftover bare roles
    s = s.replace("``", "").replace("`", "").replace("\\", "")
    return s

def norm(s: str) -> str:
    s = (s or "").lower()
    s = _PLACE.sub(" ", s)
    return _ALNUM.sub("", s)

def summary_of(doc: str) -> str:
    """First meaningful sentence/line of a cleaned docstring (the 'purpose')."""
    doc = (doc or "").strip()
    if not doc:
        return ""
    # skip leading directive/blank lines to reach the first prose line
    lines = [ln for ln in doc.split("\n")]
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("..")):
        lines.pop(0)
    doc = "\n".join(lines).strip()
    # stop at the first blank line (end of summary paragraph)
    para = re.split(r"\n\s*\n", doc, maxsplit=1)[0].replace("\n", " ").strip()
    # first sentence within that paragraph
    m = re.split(r"(?<=[.!?])\s+", para, maxsplit=1)
    first = m[0].strip()
    return first if len(first) >= 8 else para

def present(needle: str, haystack_norm: str, thresh=85) -> bool:
    n = norm(needle)[:400]
    if len(n) < 8:
        return bool(n) and n in haystack_norm
    return n in haystack_norm or fuzz.partial_ratio(n, haystack_norm) >= thresh

def leaves(obj):
    out = []
    if isinstance(obj, str):
        if obj.strip().lower() not in TRIVIAL:
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(leaves(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(leaves(v))
    return out

def struct_purpose(j):
    return ((j.get("module_member_description") or {}).get("purpose") or "")

def struct_param_names(j):
    out = []
    for p in (j.get("parameters") or []):
        nm = (p.get("name") or "").strip()
        if nm and nm.lower() not in TRIVIAL:
            out.append(nm.split(":")[0].split("(")[0].strip().lstrip("*"))
    return [p for p in out if p]

def is_empty(j):
    sig = (j.get("module_member_signature") or "").strip().lower()
    if sig not in TRIVIAL:
        return False
    return len(leaves({k: v for k, v in j.items() if k != "module_member_signature"})) == 0

def gt_for(qm, api):
    m = qm.get_member_by_any_api_name(api)
    if m:
        return m, "direct"
    res = qm.find_member_by_any_path(api)
    if res:
        if res.get("original_member"):
            return res["original_member"], "inherited->original"
        return res["member"], "inherited"
    return None, "missing"

def audit(library, source):
    db = MapCoDocDB(os.path.join(PROJECT_ROOT, "mapcodoc_output", "doc_test", DB_FILES[library]))
    session = db.get_session()
    qm = QueryManager(session)
    manifest = os.path.join(TESTS, f"{library}_fidelity_collection.csv")
    rows = []
    with open(manifest, newline="", encoding="utf-8") as f:
        members = [r for r in csv.DictReader(f) if r["source"] == source]
    # Some libraries (e.g. torch) were collected double-nested as <lib>/<lib>/<source>.
    src_root = os.path.join(COLLECT_BASE, library, source)
    if not os.path.isdir(src_root) and os.path.isdir(os.path.join(COLLECT_BASE, library, library, source)):
        src_root = os.path.join(COLLECT_BASE, library, library, source)

    for r in members:
        api, stem, bucket = r["sample_api_name"], r["stem"], r["bucket"]
        bdir = os.path.join(src_root, bucket)
        tpath, jpath = os.path.join(bdir, stem + ".txt"), os.path.join(bdir, stem + ".json")
        retrieved = open(tpath, encoding="utf-8").read() if os.path.exists(tpath) else ""
        j = {}
        if os.path.exists(jpath):
            try:
                j = json.load(open(jpath, encoding="utf-8"))
            except Exception:
                j = {}
        ret_n = norm(retrieved)

        gt, gt_kind = gt_for(qm, api)
        gt_doc = clean_rst(gt.docstring) if gt else ""
        gt_summary = summary_of(gt_doc)
        gt_sigs = list((gt.signatures or {}).values()) if gt else []
        gt_params = [p.get("name") for p in (gt.parameters or []) if p.get("name") not in (None, "self", "cls")] if (gt and gt.parameters) else []
        gt_sum_norm = norm(gt_summary)
        has_gt_doc = usable_truth(gt_summary)

        # ---- Layer 1: retrieved vs DB ground truth ----
        if not retrieved.strip():
            preproc_acc = "no_doc"
        elif gt is None:
            preproc_acc = "no_groundtruth"
        elif not has_gt_doc and not any(gt_sigs):
            preproc_acc = "no_groundtruth"   # DB has neither docstring nor signature to verify against
        else:
            ds_present = has_gt_doc and present(gt_summary, ret_n)
            sig_present = any(present(s, ret_n) for s in gt_sigs if s)
            preproc_acc = "yes" if (ds_present or (not has_gt_doc and sig_present)) else "no"

        # ---- Layer 2: structured vs retrieved ----
        empty = is_empty(j) if j else True
        if empty:
            struct_vs_pre = "empty"
        else:
            lv = [l for l in leaves(j) if len(norm(l)) >= 12]
            g = sum(1 for l in lv if present(l, ret_n))
            ratio = g / len(lv) if lv else 1.0
            struct_vs_pre = "yes" if ratio >= 0.8 else ("partial" if ratio >= 0.5 else "no")

        # ---- Layer 3: structured vs DB ground truth ----
        purpose = struct_purpose(j)
        if gt is None:
            struct_vs_orig = "no_groundtruth"
        elif empty:
            # empty extraction is only a confirmed failure if the DB HAS a real doc
            struct_vs_orig = "no" if has_gt_doc else "no_groundtruth"
        elif has_gt_doc:
            pn = norm(purpose)
            all_struct = norm(purpose + " " + " ".join(leaves(j)))
            ok = bool(pn) and (
                fuzz.partial_ratio(pn, gt_sum_norm) >= 85
                or fuzz.partial_ratio(gt_sum_norm, all_struct) >= 85
            )
            struct_vs_orig = "yes" if ok else "no"
        else:
            # structured has content but DB has no usable docstring to verify against
            struct_vs_orig = "no_groundtruth"

        # ---- scope: completeness vs DB ----
        if empty or gt is None:
            scope = "n/a"
        else:
            sp = set(x.lower() for x in struct_param_names(j))
            if gt_params:
                cov = sum(1 for p in gt_params if p.lower() in sp) / len(gt_params)
                scope = "full" if cov >= 0.99 else (f"partial({cov:.2f})" if cov > 0 else "missing_params")
            else:
                scope = "full" if struct_vs_orig == "yes" else "n/a"

        # ---- root cause layer ----
        if struct_vs_orig in ("yes",):
            root = "none"
        elif preproc_acc == "no":
            root = "retrieval"
        elif struct_vs_pre in ("no", "partial") and preproc_acc == "yes":
            root = "structuring"
        elif empty:
            root = "retrieval" if preproc_acc in ("no", "no_doc") else "structuring/empty"
        else:
            root = "review"

        correct_doc = {"yes": "yes", "no": "no", "empty": "no", "no_groundtruth": "review"}[struct_vs_orig]

        flag = ""
        if struct_vs_orig != "yes" or preproc_acc in ("no", "no_doc") or gt_kind == "missing":
            flag = "REVIEW"

        rows.append(dict(
            api_name=api, member_type=(gt.type if gt else r.get("member_type", "")),
            fidelity_group=bucket, gt_kind=gt_kind,
            preprocessed_accurate=preproc_acc, structured_vs_preprocessed=struct_vs_pre,
            structured_vs_original=struct_vs_orig, correct_doc=correct_doc,
            scope=scope, root_cause_layer=root, flag=flag,
            gt_doc_head=gt_doc[:80].replace("\n", " "),
            struct_purpose_head=purpose[:80].replace("\n", " "),
        ))
    session.close()
    return rows

def main():
    library, source = sys.argv[1], sys.argv[2]
    rows = audit(library, source)
    cols = ["api_name","member_type","fidelity_group","gt_kind","preprocessed_accurate",
            "structured_vs_preprocessed","structured_vs_original","correct_doc","scope",
            "root_cause_layer","flag","gt_doc_head","struct_purpose_head"]
    out = os.path.join(TESTS, f"{library}_{source}_ground_truth_audit.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader(); w.writerows(rows)
    from collections import Counter
    print(f"{library}/{source}: {len(rows)} members")
    print("correct_doc:", dict(Counter(r["correct_doc"] for r in rows)))
    print("preprocessed_accurate:", dict(Counter(r["preprocessed_accurate"] for r in rows)))
    print("root_cause_layer:", dict(Counter(r["root_cause_layer"] for r in rows)))
    print("flagged for manual read:", sum(1 for r in rows if r["flag"]))

if __name__ == "__main__":
    main()
