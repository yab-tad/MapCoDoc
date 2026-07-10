"""
Fetch ORIGINAL web docs for the web-verification step, reusing the project's
DocScraper (same extractor that produced preprocessed_doc). Caches cleaned text
into tests/webverify/pages/ ONLY -- never writes to doc_artifacts/scraped_doc.

Only fetches the pages required by the (review+no) target set under the type-based
routing in web_verify_plan (methods/inherited + anchor-lib members). Idempotent:
skips URLs already cached.

Usage: python web_fetch_originals.py <lib> [<lib> ...]
"""
import asyncio, csv, hashlib, json, os, sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "doc_processor"))

from doc_processor.web_doc.doc_scraper import DocScraper          # noqa: E402
from doc_processor.web_doc.network import URLFetcher              # noqa: E402
import web_verify_plan as W                          # noqa: E402
from mapcodoc_db.db_manager import MapCoDocDB        # noqa: E402
from mapcodoc_db.query import QueryManager           # noqa: E402

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webverify", "pages")
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webverify", "page_index.json")
os.makedirs(CACHE, exist_ok=True)

def cache_name(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".txt"

def target_fetch_pages(lib):
    """Return {page_url: [api,...]} for fetch-routed members of the (review+no) set."""
    db = MapCoDocDB(W.resolve_db(lib)); session = db.get_session(); qm = QueryManager(session)
    umap = W.load_url_map(lib)
    tgt = set()
    for src in ("web", "pdf"):
        ap = os.path.join(W.TESTS, f"{lib}_{src}_ground_truth_audit.csv")
        if not os.path.exists(ap):
            continue
        for r in csv.DictReader(open(ap, encoding="utf-8")):
            if r["correct_doc"] in ("no", "review"):
                tgt.add(r["api_name"])
    pages = {}
    for api in tgt:
        mtype, kind = W.member_type(qm, api)
        route_fetch = (lib in W.ANCHOR) or kind == "inherited" or (mtype not in W.TRUST_TYPES)
        if not route_fetch:
            continue
        u = umap.get(api) or umap.get(api.split(".")[-1])
        if u:
            pages.setdefault(u.split("#", 1)[0], []).append(api)
    session.close()
    return pages

async def fetch_pages(urls):
    out = {}
    # read-only verification fetch of public docs the pipeline already scraped;
    # robots disallows some hosts (e.g. docs.pytorch.org /docs), so skip robots here.
    async with URLFetcher(respect_robots=False) as net:
        for u in urls:
            try:
                ds = DocScraper(u)
                out[u] = await ds.extract_text_async(net)
            except Exception as e:
                out[u] = None
                print(f"  ERROR {u}: {e}")
    return out

def main():
    libs = sys.argv[1:] or ["requests", "xgboost", "sqlalchemy", "sklearn", "torch", "numpy", "pandas"]
    index = {}
    if os.path.exists(INDEX):
        index = json.load(open(INDEX, encoding="utf-8"))
    for lib in libs:
        pages = target_fetch_pages(lib)
        todo = [u for u in pages if u not in index or not os.path.exists(os.path.join(CACHE, index.get(u, "")))]
        print(f"{lib}: {len(pages)} fetch-pages ({len(todo)} new)")
        if not todo:
            continue
        results = asyncio.run(fetch_pages(todo))
        for u, text in results.items():
            if text:
                fn = cache_name(u)
                with open(os.path.join(CACHE, fn), "w", encoding="utf-8") as f:
                    f.write(text)
                index[u] = fn
                print(f"  cached {len(text):>7} chars  {u}")
            else:
                print(f"  FAILED {u}")
    json.dump(index, open(INDEX, "w", encoding="utf-8"), indent=2)
    print("index entries:", len(index))

if __name__ == "__main__":
    main()
