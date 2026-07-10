"""
Plan web-verification of the (review + no) target set.

Routing (per user's rule):
  - per-member-page libs (numpy/pandas/sklearn/torch): a CLASS or FUNCTION is scraped
    correctly -> trust its preprocessed_doc as the original (NO fetch).
  - method / inherited / property / variable members may live on the class page ->
    FETCH the original page.
  - anchor-based libs (requests/sqlalchemy/xgboost): always FETCH (shared pages).
One fetched original serves BOTH the web and pdf preprocessed/structured checks.
"""
import csv, os, re, sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from mapcodoc_db.db_manager import MapCoDocDB
from mapcodoc_db.query import QueryManager

TESTS = os.path.dirname(os.path.abspath(__file__))
CU = os.path.join(PROJECT_ROOT, "doc_processor", "doc_artifacts", "crawled_URLs")
DB_DIR = os.path.join(PROJECT_ROOT, "mapcodoc_output", "doc_test")
import glob

URL_FILES = {
    "numpy": os.path.join(CU, "numpy", "v_stable", "samples", "scraped_urls.txt"),
    "pandas": os.path.join(CU, "pandas", "v_2.3.3", "samples", "scraped_urls.txt"),
    "requests": os.path.join(CU, "requests", "v_latest", "samples", "scraped_urls.txt"),
    "sklearn": os.path.join(CU, "sklearn", "v_stable", "samples", "scraped_urls.txt"),
    "sqlalchemy": os.path.join(CU, "sqlalchemy", "v_latest", "samples", "scraped_urls.txt"),
    "torch": os.path.join(CU, "torch", "v_2.9.1", "samples", "scraped_urls.txt"),
    "xgboost": os.path.join(CU, "xgboost", "v_3.2.0", "samples", "scraped_urls.txt"),
}
PER_MEMBER_PAGE = {"numpy", "pandas", "sklearn", "torch"}
ANCHOR = {"requests", "sqlalchemy", "xgboost"}
TRUST_TYPES = {"class", "function"}

def url_key(u):
    u = u.strip()
    if "#" in u:
        return u.split("#", 1)[1]
    stem = u.rstrip("/").split("/")[-1]
    return stem[:-5] if stem.endswith(".html") else stem

def load_url_map(lib):
    m = {}
    with open(URL_FILES[lib], encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if u:
                m[url_key(u)] = u
    return m

def member_type(qm, api):
    mm = qm.get_member_by_any_api_name(api)
    if mm:
        return mm.type, "direct"
    res = qm.find_member_by_any_path(api)
    if res:
        if res.get("original_member"):
            return res["original_member"].type, "inherited"
        return getattr(res["member"], "member_type", "inherited"), "inherited"
    return None, "missing"

def resolve_db(library):
    g = sorted(glob.glob(os.path.join(DB_DIR, f"{library}_*.db")))
    return g[0] if g else None

def plan(lib):
    db = MapCoDocDB(resolve_db(lib)); session = db.get_session(); qm = QueryManager(session)
    umap = load_url_map(lib)
    # target = union of (no, review) members across web+pdf audits
    target = {}
    for src in ("web", "pdf"):
        ap = os.path.join(TESTS, f"{lib}_{src}_ground_truth_audit.csv")
        if not os.path.exists(ap):
            continue
        with open(ap, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["correct_doc"] in ("no", "review"):
                    target.setdefault(r["api_name"], set()).add(src)
    trust = fetch_members = 0
    fetch_pages = set()
    no_url = []
    for api in target:
        mtype, kind = member_type(qm, api)
        route_fetch = (lib in ANCHOR) or kind == "inherited" or (mtype not in TRUST_TYPES)
        if route_fetch:
            u = umap.get(api) or umap.get(api.split(".")[-1])
            if not u:
                no_url.append(api); continue
            fetch_members += 1
            fetch_pages.add(u.split("#", 1)[0])
        else:
            trust += 1
    session.close()
    return dict(lib=lib, target=len(target), trust_no_fetch=trust,
                fetch_members=fetch_members, unique_fetch_pages=len(fetch_pages),
                url_unmapped=len(no_url))

if __name__ == "__main__":
    libs = sys.argv[1:] or ["requests","numpy","pandas","sklearn","sqlalchemy","torch","xgboost"]
    tot_pages = tot_trust = 0
    print(f"{'lib':12} {'target':>7} {'trust':>7} {'fetchMbr':>9} {'pages':>6} {'noURL':>6}")
    for lib in libs:
        p = plan(lib)
        tot_pages += p["unique_fetch_pages"]; tot_trust += p["trust_no_fetch"]
        print(f"{p['lib']:12} {p['target']:7d} {p['trust_no_fetch']:7d} {p['fetch_members']:9d} {p['unique_fetch_pages']:6d} {p['url_unmapped']:6d}")
    print(f"\nTOTAL unique pages to fetch: {tot_pages}   |   members resolved without fetch (trusted): {tot_trust}")
