"""
Web-grounded verification of the (review + no) target set.

For each target member, establish the WEB ORIGINAL reference:
  - trusted (class/function on per-member-page lib): the web preprocessed_doc itself.
  - fetched (methods/inherited + anchor libs): the cached web page
      * anchor page -> extract the member's section via its #anchor
      * per-member page -> whole page
Then compare the WEB and PDF samples' preprocessed_doc + structured_doc against that
single web original (tolerant of web/pdf build differences). PDF is never self-trusted.

Writes tests/<lib>_<source>_webcheck.csv
Usage: python web_verify.py <lib> [<lib> ...]
"""
import csv, json, os, re, sys
from rapidfuzz import fuzz

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)

import web_verify_plan as W
from audit_ground_truth import norm, summary_of, leaves, struct_purpose, is_empty
from mapcodoc_db.db_manager import MapCoDocDB
from mapcodoc_db.query import QueryManager

CACHE = os.path.join(TESTS, "webverify", "pages")
INDEX = json.load(open(os.path.join(TESTS, "webverify", "page_index.json"), encoding="utf-8"))
COLLECT = W.__dict__.get("COLLECT_BASE")
from web_verify_plan import ANCHOR, TRUST_TYPES, PER_MEMBER_PAGE
COLLECT_BASE = os.environ.get("MAPCODOC_COLLECT_BASE", "collected_fidelity")

DEF_RE = re.compile(r"^(class|function|exception|property|method|attribute|static|async)\s|^\s*[\w.]+\s*\(|#[\w.]+\)\s*$", re.I)

def collected_src_root(lib, src):
    r = os.path.join(COLLECT_BASE, lib, src)
    if not os.path.isdir(r) and os.path.isdir(os.path.join(COLLECT_BASE, lib, lib, src)):
        r = os.path.join(COLLECT_BASE, lib, lib, src)
    return r

def load_manifest(lib):
    """(api, source) -> (stem, bucket)"""
    m = {}
    with open(os.path.join(TESTS, f"{lib}_fidelity_collection.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            m[(r["sample_api_name"], r["source"])] = (r["stem"], r["bucket"])
    return m

def read_collected(lib, src, stem, bucket, ext):
    p = os.path.join(collected_src_root(lib, src), bucket, stem + ext)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

_DEFKW = re.compile(r"^(class|function|exception|property|method|attribute|static|async)\s", re.I)

def extract_section(page_text, anchor):
    """Pull the member's block out of an anchor/multi-member page using its #anchor.
    The anchor may appear several times (TOC link, summary-table row, real definition);
    choose the real definition (keyword-prefixed or followed by an indented description)."""
    key = "#" + anchor
    lines = page_text.split("\n")
    cands = [i for i, ln in enumerate(lines) if key + ")" in ln or key in ln]
    if not cands:
        seg = anchor.split(".")[-1]
        for i, ln in enumerate(lines):
            if re.search(r"\b" + re.escape(seg) + r"\b\s*\(", ln):
                cands = [i]; break
    if not cands:
        return ""
    best = None
    for i in cands:
        s = lines[i].strip()
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        indented_desc = nxt.startswith(("    ", "\t")) and nxt.strip() and not nxt.lstrip().startswith("-")
        if _DEFKW.match(s) or indented_desc:
            best = i; break
    if best is None:
        best = cands[-1]   # last occurrence is usually the definition, not the TOC
    out = [lines[best]]
    for ln in lines[best + 1:]:
        s = ln.strip()
        if not s:
            out.append(ln); continue
        if _DEFKW.match(s) or re.search(r"#[\w.]+\)\s*$", s):
            break
        out.append(ln)
        if len("\n".join(out)) > 2000:
            break
    return "\n".join(out).strip()

def get_original(lib, api, route_fetch, umap):
    if not route_fetch:
        return None  # signal: use web preprocessed as original
    url = umap.get(api) or umap.get(api.split(".")[-1])
    if not url:
        return ""
    base = url.split("#", 1)[0]
    cf = INDEX.get(base)
    if not cf:
        return ""
    page = open(os.path.join(CACHE, cf), encoding="utf-8").read()
    if "#" in url:
        return extract_section(page, url.split("#", 1)[1])
    return page

_SIG = re.compile(r"^(classmethod|staticmethod|abstractmethod|abstract|class|function|exception|property|method|attribute|static|async)\b|^[\w.]+\s*\(|^[\w.]+\s*:\s", re.I)

def is_sig_line(s):
    return bool(_SIG.match(s)) or bool(re.search(r"#[\w.]+\)\s*$", s))

_CHROME = ("rate this page", "toggle", "previous", "next", "edit on", "show source",
           "on this page", "table of contents", "© copyright", "created using",
           "search", "navigation", "skip to", "back to top", "view page source",
           "pytorch libraries", "hidden breadcrumb", "get started", "ecosystem",
           "docs ›", "developer resources", "find resources")
# A line is a SECTION HEADER only when it is *just* the label (optionally with a
# trailing colon) - NOT a sentence that merely starts with "Return"/"See", etc.
_HEADER = re.compile(
    r"^(parameters|returns?|return type|raises|yields|see also|examples?|notes?|"
    r"warnings?|attributes?|methods?)\s*:?\s*$", re.I)
# Footer/nav region markers: once seen, no member description follows -> stop scanning.
_STOP = ("pytorch libraries",)

# Link stripping so real URLs / permalinks don't pollute matching or get mistaken
# for signatures. Converts "[txt](url)"->"txt", drops "(http..)"/"(#anchor)" and bare URLs.
_LINK_MD = re.compile(r"\[([^\]]+)\]\((?:https?://|#)[^)]*\)")
_LINK_PAREN = re.compile(r"\((?:https?://|#)[^)]*\)")
_URL_BARE = re.compile(r"https?://\S+")

def _nolink(s):
    s = s or ""
    s = _LINK_MD.sub(r"\1", s)
    s = _LINK_PAREN.sub("", s)
    s = _URL_BARE.sub("", s)
    return s

def original_summary(text):
    """Description sentence of a member: the first real prose line AFTER its signature,
    skipping headings, page chrome, and bare section labels. Links are stripped first so
    a description ending in a permalink isn't mistaken for a signature."""
    lines = [l.rstrip() for l in (text or "").split("\n")]
    sig_idx = -1
    for i, raw in enumerate(lines):
        if is_sig_line(_nolink(raw.strip())):
            sig_idx = i
            break
    for raw in (lines[sig_idx + 1:] if sig_idx >= 0 else lines):
        s = _nolink(raw.strip()).strip()
        if not s or s == "```" or s.startswith("#"):
            continue
        low = s.lower()
        if any(st in low for st in _STOP):   # footer/nav begins -> no description follows
            break
        if any(c in low for c in _CHROME) or _HEADER.match(low):
            continue
        if len(re.sub(r"[^a-z0-9]", "", low)) < 3:   # stars, bullets, separators, glyphs
            continue
        if low.startswith(("inherits from", "bases:", "bases ", "member name")):
            continue
        if is_sig_line(s):
            continue
        return re.split(r"(?<=[.!?])\s+", s, maxsplit=1)[0]
    return ""

def match(a, b, thresh=85):
    an, bn = norm(_nolink(a)), norm(_nolink(b))
    if not an or not bn:
        return False
    if len(an) < 8 or len(bn) < 8:        # short, legit docs ("No-op.", "sum.") -> exact/containment
        return an == bn or an in bn or bn in an
    return an in bn or bn in an or fuzz.partial_ratio(an, bn) >= thresh

def verdict(structured_json, preprocessed_text, original_text, orig_is_preproc, web_preproc_text):
    """Return (preproc_acc, struct_acc) of a sample vs the web original."""
    orig = web_preproc_text if orig_is_preproc else original_text
    orig_sum = original_summary(orig) if orig else ""
    # preprocessed accuracy: does this sample's preprocessed contain the original's summary?
    if not orig_sum:
        preproc_acc = "no_ref"
    elif orig_is_preproc:
        preproc_acc = "yes"  # original IS the (web) preprocessed
    else:
        preproc_acc = "yes" if match(orig_sum, preprocessed_text) else "no"
    # structured accuracy: structured purpose / content matches original summary
    if is_empty(structured_json):
        struct_acc = "empty" if orig_sum else "no_ref"
    elif not orig_sum:
        struct_acc = "no_ref"
    else:
        pj = struct_purpose(structured_json)
        allj = " ".join(leaves(structured_json))
        struct_acc = "yes" if (match(pj, orig_sum) or match(orig_sum, allj)) else "no"
    return preproc_acc, struct_acc

def run(lib):
    db = MapCoDocDB(W.resolve_db(lib)); session = db.get_session(); qm = QueryManager(session)
    umap = W.load_url_map(lib)
    manifest = load_manifest(lib)
    # target = review+no across both sources
    target = set()
    audit = {}
    for src in ("web", "pdf"):
        ap = os.path.join(TESTS, f"{lib}_{src}_ground_truth_audit.csv")
        if not os.path.exists(ap):
            continue
        for r in csv.DictReader(open(ap, encoding="utf-8")):
            audit[(r["api_name"], src)] = r
            if r["correct_doc"] in ("no", "review"):
                target.add(r["api_name"])
    rows = []
    for api in sorted(target):
        mtype, kind = W.member_type(qm, api)
        route_fetch = (lib in ANCHOR) or kind == "inherited" or (mtype not in TRUST_TYPES)
        original = get_original(lib, api, route_fetch, umap)
        orig_is_preproc = (original is None)
        # web preprocessed text (also the original when trusted)
        wst = manifest.get((api, "web"))
        web_pre = read_collected(lib, "web", wst[0], wst[1], ".txt") if wst else ""
        rec = dict(api_name=api, member_type=mtype or "", route=("trusted" if orig_is_preproc else "fetched"))
        for src in ("web", "pdf"):
            st = manifest.get((api, src))
            if not st:
                rec[f"{src}_preproc_acc"] = "no_sample"; rec[f"{src}_struct_acc"] = "no_sample"; rec[f"{src}_correct"] = "n/a"
                continue
            pre = read_collected(lib, src, st[0], st[1], ".txt")
            try:
                sj = json.loads(read_collected(lib, src, st[0], st[1], ".json") or "{}")
            except Exception:
                sj = {}
            pa, sa = verdict(sj, pre, original if not orig_is_preproc else None, orig_is_preproc, web_pre)
            rec[f"{src}_preproc_acc"] = pa
            rec[f"{src}_struct_acc"] = sa
            rec[f"{src}_correct"] = {"yes": "yes", "no": "no", "empty": "no", "no_ref": "review"}[sa]
        ref_sum = original_summary(web_pre if orig_is_preproc else (original or ""))[:90]
        rec["original_summary"] = ref_sum.replace("\n", " ")
        rows.append(rec)
    session.close()
    cols = ["api_name","member_type","route","web_preproc_acc","web_struct_acc","web_correct",
            "pdf_preproc_acc","pdf_struct_acc","pdf_correct","original_summary"]
    out = os.path.join(TESTS, f"{lib}_webcheck.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rows: w.writerow({c: r.get(c, "") for c in cols})
    from collections import Counter
    print(f"{lib}: {len(rows)} target members")
    print("  web_correct:", dict(Counter(r.get("web_correct") for r in rows)))
    print("  pdf_correct:", dict(Counter(r.get("pdf_correct") for r in rows)))

if __name__ == "__main__":
    for lib in (sys.argv[1:] or ["requests"]):
        run(lib)
