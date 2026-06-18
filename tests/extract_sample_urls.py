"""
Extract Crawled URLs for Sample API Names
=========================================
Given a sample API-names file, a crawled scraped_urls.txt, and its
statistics.json, write the subset of URLs whose page documents one of the
sample API names.

An API name is matched against each URL in two ways (both checked):
    1. URL fragment        .../python_api.html#xgboost.XGBClassifier
    2. Path stem (.html)   .../generated/torch.nn.Conv2d.html

Inherited members / methods (e.g. torch.nn.Conv2d.cpu) frequently have no page
or anchor of their own, so if neither exact match is found the name is shortened
one dotted component at a time (torch.nn.Conv2d.cpu -> torch.nn.Conv2d -> ...)
until the owning page is located.

Output (into --output-dir):
    scraped_urls.txt   - de-duplicated crawled URLs (fragments preserved) for the
                         sample (drop-in for test_extraction.py --url-file)
    statistics.json    - copied verbatim from the source (so base_url/sub_path
                         travel with the URL file)

Usage:
    python tests/extract_sample_urls.py \\
        --names-file "C:/.../RQ2/api_names/xgboost.txt" \\
        --url-file   doc_processor/doc_artifacts/crawled_URLs/xgboost/v_3.2.0-dev/scraped_urls.txt \\
        --output-dir tests/sample_urls/xgboost \\
        --report-file tests/sample_urls/xgboost_url_extraction.json

    # --stat-file is optional; by default it is auto-discovered next to --url-file.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, unquote


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load_api_names(path: str) -> List[str]:
    """Read API names (one per line), preserving order and dropping blanks/dupes."""
    seen, names = set(), []
    with open(path, encoding="utf-8") as f:
        for line in f:
            n = line.strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)
    return names


def load_urls(path: str) -> List[str]:
    """Read crawled URLs (one per line), dropping blanks."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if u:
                out.append(u)
    return out


def load_stat_info(url_file: Path, stat_file: Optional[str]) -> Dict:
    """
    Load base_url / sub_path. Explicit --stat-file wins; otherwise look for
    statistics.json (or *statistics*.json) next to the URL file. Mirrors the
    discovery used by tests/test_extraction.py::_load_stat_info.
    """
    if stat_file:
        sf = Path(stat_file)
    else:
        sf = url_file.parent / "statistics.json"
        if not sf.exists():
            cands = list(url_file.parent.glob("*statistics*.json"))
            sf = cands[0] if cands else sf

    if sf.exists():
        with open(sf, encoding="utf-8") as f:
            data = json.load(f)
        data["__stat_file__"] = str(sf)
        return data

    # Fallback: infer base_url from the first URL.
    with open(url_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                p = urlparse(line)
                base = f"{p.scheme}://{p.netloc}{p.path.rsplit('/', 1)[0]}/"
                return {"base_url": base, "sub_path": "", "__stat_file__": None}
    return {"base_url": "", "sub_path": "", "__stat_file__": None}


# ---------------------------------------------------------------------------
# URL parsing & indexing
# ---------------------------------------------------------------------------

_HTML_EXTS = (".html", ".htm")


def url_keys(url: str, base_url: str) -> Tuple[str, str, str]:
    """
    Return (absolute_url, fragment, path_stem).
    - absolute_url: the URL resolved against base_url with its #fragment PRESERVED
      (this is what gets written to the output file).
    - fragment: the part after '#', URL-decoded (e.g. 'xgboost.XGBClassifier.fit').
    - path_stem: last path segment minus .html/.htm, URL-decoded
      (e.g. 'torch.nn.Conv2d' from '.../generated/torch.nn.Conv2d.html').
    """
    absu = urljoin(base_url, url) if base_url else url
    p = urlparse(absu)
    
    fragment = unquote(p.fragment).strip() if p.fragment else ""
    
    last = p.path.rsplit("/", 1)[-1]
    stem = last
    for ext in _HTML_EXTS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    stem = unquote(stem).strip()
    
    return absu, fragment, stem


def build_indexes(urls: List[str], base_url: str):
    """
    Build fragment->urls and stem->urls indexes.
    Values are ordered, de-duplicated lists of full crawled URLs (fragments preserved). NOTE: on single-page sites every line shares one path stem
    (e.g. 'python_api'), so the stem index is effectively unused there and fragment matching carries everything.
    """
    frag_idx: Dict[str, List[str]] = {}
    stem_idx: Dict[str, List[str]] = {}
    
    def _add(idx: Dict[str, List[str]], key: str, url: str):
        if not key:
            return
        bucket = idx.setdefault(key, [])
        if url not in bucket:
            bucket.append(url)

    for u in urls:
        absu, frag, stem = url_keys(u, base_url)
        if frag:
            _add(frag_idx, frag, absu)   # member anchor -> fragment index only
        else:
            _add(stem_idx, stem, absu)   # the page itself -> stem index only
    return frag_idx, stem_idx


def name_prefixes(api_name: str):
    """Yield dotted prefixes longest-first: a.b.c -> a.b.c, a.b, a."""
    parts = api_name.split(".")
    for i in range(len(parts), 0, -1):
        yield ".".join(parts[:i])


def match_api_name(api_name: str, frag_idx, stem_idx, ignore_case: bool):
    """
    Resolve an API name to crawled URLs.
    
    Precedence (per prefix, most specific first): a URL whose path stem (the
    name before '.html') wins over one whose #fragment matches, so a dedicated
    member page is preferred to a class-page anchor.
        for each prefix (full name down to top-level):
            exact path-stem match -> ('path',     prefix, urls)
            exact fragment match  -> ('fragment', prefix, urls)
    Returns (matched_via, matched_key, urls) or (None, None, []).
    """
    def _lookup(idx, key):
        if key in idx:
            return idx[key]
        if ignore_case:
            lk = key.lower()
            for k, v in idx.items():
                if k.lower() == lk:
                    return v
        return None
    
    for prefix in name_prefixes(api_name):
        urls = _lookup(stem_idx, prefix)
        if urls:
            return "path", prefix, list(urls)
        urls = _lookup(frag_idx, prefix)
        if urls:
            return "fragment", prefix, list(urls)
    return None, None, []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def extract(names_file, url_file, output_dir, stat_file=None, report_file=None, ignore_case=False):
    url_file = Path(url_file)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    api_names = load_api_names(names_file)
    urls = load_urls(str(url_file))
    stat = load_stat_info(url_file, stat_file)
    base_url = stat.get("base_url", "") or ""

    frag_idx, stem_idx = build_indexes(urls, base_url)

    matches, unmatched = [], []
    selected_urls: List[str] = []
    seen_urls: Set[str] = set()
    
    for name in api_names:
        via, key, matched_urls = match_api_name(name, frag_idx, stem_idx, ignore_case)
        if matched_urls:
            for u in matched_urls:
                if u not in seen_urls:
                    seen_urls.add(u)
                    selected_urls.append(u)
            matches.append({
                "api_name": name,
                "matched_via": via,            # 'fragment' | 'path'
                "matched_key": key,            # exact name, or parent prefix
                "is_parent_url": key != name, # True => resolved to owning page
                "urls": matched_urls,
            })
        else:
            unmatched.append(name)
            
    selected_urls.sort()
    
    # --- Write filtered URL file (drop-in for test_extraction.py) ---
    out_url_file = out_dir / "scraped_urls.txt"
    out_url_file.write_text("".join(f"{u}\n" for u in selected_urls), encoding="utf-8")

    # --- Copy statistics.json next to it so base_url/sub_path travel along ---
    src_stat = stat.get("__stat_file__")
    if src_stat and Path(src_stat).exists():
        shutil.copy2(src_stat, out_dir / "statistics.json")
    else:
        (out_dir / "statistics.json").write_text(json.dumps({"base_url": base_url, "sub_path": stat.get("sub_path", "")}, indent=4), encoding="utf-8")

    report = {
        "names_file": names_file,
        "url_file": str(url_file),
        "base_url": base_url,
        "sub_path": stat.get("sub_path", ""),
        "total_api_names": len(api_names),
        "matched": len(matches),
        "unmatched": len(unmatched),
        "total_urls_in": len(urls),
        "total_urls_out": len(selected_urls),
        "output_url_file": str(out_url_file),
        "unmatched_names": unmatched,
        "matches": matches
    }

    _print_summary(report)

    if report_file:
        Path(report_file).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nReport written to: {report_file}")

    return report


def _print_summary(r: Dict) -> None:
    print("\n" + "=" * 70)
    print("  SAMPLE URL EXTRACTION")
    print("=" * 70)
    print(f"  Names file     : {r['names_file']}")
    print(f"  base_url       : {r['base_url']}")
    print(f"  API names      : {r['total_api_names']}")
    print(f"  Matched        : {r['matched']}")
    print(f"  Unmatched      : {r['unmatched']}")
    print(f"  URLs in        : {r['total_urls_in']}")
    print(f"  URLs out       : {r['total_urls_out']}")
    print(f"  Output URLs    : {r['output_url_file']}")

    n_parent = sum(1 for m in r["matches"] if m["is_parent_url"])
    if n_parent:
        print(f"\n  {n_parent} name(s) resolved to a PARENT page "
              f"(likely inherited members/methods):")
        for m in r["matches"]:
            if m["is_parent_url"]:
                print(f"    - {m['api_name']}  ->  {m['matched_key']} "
                      f"({m['matched_via']})")

    if r["unmatched_names"]:
        print("\n  UNMATCHED API NAMES:")
        for n in r["unmatched_names"]:
            print(f"    - {n}")
    print()


def _parse_args():
    p = argparse.ArgumentParser(description="Extract crawled URLs that document a set of sample API names.")
    p.add_argument("--names-file", required=True, help="Sample API names, one per line.")
    p.add_argument("--url-file", required=True, help="Crawled scraped_urls.txt.")
    p.add_argument("--stat-file", default=None, help="statistics.json (default: auto-discover next to --url-file).")
    p.add_argument("--output-dir", required=True, help="Destination dir for filtered scraped_urls.txt + statistics.json.")
    p.add_argument("--report-file", default=None, help="Optional JSON report path.")
    p.add_argument("--ignore-case", action="store_true", help="Case-insensitive fallback when exact matching fails.")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    extract(
        names_file=args.names_file,
        url_file=args.url_file,
        output_dir=args.output_dir,
        stat_file=args.stat_file,
        report_file=args.report_file,
        ignore_case=args.ignore_case
    )