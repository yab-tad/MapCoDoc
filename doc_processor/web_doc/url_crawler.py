import os
import re
import sys
import lxml
import json
import time
import asyncio
import logging
from pathlib import Path
from collections import deque
from urllib.parse import urljoin, urlparse
from typing import Set, Optional, Dict, Any, Tuple
from bs4 import BeautifulSoup, NavigableString, Tag

ROOT = Path(__file__).resolve().parents[1]  # project root
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from doc_processor.web_doc.network import URLFetcher


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2] / 'doc_processor' / 'doc_artifacts'


__all__ = ["save_urls_to_file"]


class URLScraperError(Exception):
    """Exception raised for URL scraping related errors."""
    def __init__(self, message: str, url: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.url = url
        self.details = details
    
    def __str__(self) -> str:
        error_msg = self.message
        if self.url:
            error_msg = f"{error_msg} (url: {self.url})"
        if self.details:
            error_msg = f"{error_msg} - details: {self.details}"
        return error_msg


class URLScraper:
    def __init__(self, max_workers=None, max_connections=100):
        
        self.max_workers = self._optimize_workers(max_workers)
        logger.info(f"Using {self.max_workers} workers for parallel processing")
        # print(f"Using {self.max_workers} workers for parallel processing")
        
        # URL tracking
        self.visited_urls = set()
        self.raw_urls = set()
        self.relevant_urls = set()
        # newly added URL tracking
        self.queued_urls = set()
        
        # Documentation scope
        self.base_url = None
        self.sub_path = None
        self.current_url = None
        
        # Common documentation static/special directories
        self.common_skip_dirs = {
            '_static', '_sources', '_modules',  # Common doc generators (Sphinx)
            'static', 'assets', 'images',       # Common static content
            '_images', '_downloads', 'genindex', # More Sphinx dirs
            '_next', '_app'                     # Next.js and similar frameworks
        }
        
        # Skip patterns for irrelevant files
        self.skip_extensions = re.compile(r'\.(?:pdf|zip|jpg|jpeg|png|gif|js|css|rst\.txt|ipynb|py|woff|woff2|ttf|eot|ico)$')
        self.seed_dir = None
        
        # Statistics
        self.stat_dict = dict()
        self.stats = {
            'start_time': None,
            'end_time': None,
            'processed_urls': 0,
            'failed_urls': 0
        }

    def _optimize_workers(self, max_workers):
        """Optimize number of workers based on system resources."""
        if max_workers is not None:
            return max_workers
        
        try:
            if hasattr(os, 'sched_getaffinity'):
                cpu_count = len(os.sched_getaffinity(0))
            else:
                cpu_count = os.cpu_count() or 4
        except Exception:
            cpu_count = 4
            
        return max(4, min(cpu_count * 2, 20))
    
    
    def is_relevant_url(self, url: str) -> bool:
        """
        Check if URL is within documentation scope.
        Accept only:
            - directory mode: base_url/sub_path/<name>.html (optionally with #anchor)
            - single-page mode: base_url/sub_path (where sub_path endswith .html, optionally with #anchor)
            - directory-served mode: base_url/sub_path/ (directory serving index.html, with #anchor)
            - multi-segment sub_path mode: base_url/modules/generated/<name>.html
        """
        if not self.base_url:
            return True

        if not url.startswith(self.base_url):
            return False

        # Allow the base page to seed the crawl
        if url == self.base_url:
            return True

        if not self.sub_path:
            # Fallback: only accept pages directly under the seed directory, *.html
            url_no_frag = url.split('#', 1)[0]
            if not self.seed_dir:
                return False
            if not url_no_frag.startswith(self.seed_dir):
                return False
            tail = url_no_frag[len(self.seed_dir):].strip('/')
            segs = [s for s in tail.split('/') if s]
            if len(segs) != 1 or not segs[0].lower().endswith('.html'):
                return False
            if segs[0].startswith(('genindex', 'modindex', 'search')):
                return False
            return True

        # Ignore fragment for path checks
        url_no_frag = url.split('#', 1)[0]
        rel = url_no_frag[len(self.base_url):].lstrip('/')
        segments = [seg for seg in rel.split('/') if seg]

        # --- Single-page mode: sub_path is a file like "python_api.html" ---
        if self.sub_path.lower().endswith('.html'):
            # Must be exactly that file under base_url (no extra segments)
            return len(segments) == 1 and segments[0].lower() == self.sub_path.lower()

        # --- Directory-served single-page mode ---
        # URL IS the sub_path directory (e.g., /api/ serving index.html)
        # Accepts: base_url/sub_path/ or base_url/sub_path (with or without trailing slash)
        url_path = url_no_frag.rstrip('/')
        # Handle both single-segment and multi-segment sub_paths
        expected_dir_path = (self.base_url.rstrip('/') + '/' + self.sub_path).rstrip('/')
        if url_path == expected_dir_path:
            return True  # This is the API directory page itself, anchors are valid

        # --- Multi-segment sub_path mode (e.g., "modules/generated") ---
        sub_path_segs = self.sub_path.split('/') if self.sub_path else []
        
        # Check if URL path starts with sub_path segments
        if len(segments) < len(sub_path_segs):
            return False
        
        if segments[:len(sub_path_segs)] != sub_path_segs:
            return False
        
        # Get what's after the sub_path
        after = segments[len(sub_path_segs):]

        # Require exactly one segment after sub_path and it must be an .html file
        if len(after) != 1 or not after[0].lower().endswith('.html'):
            return False

        # Skip common non-content dirs/files
        if any(seg in self.common_skip_dirs for seg in segments):
            return False
        if after[0].startswith(('genindex', 'modindex', 'search')):
            return False

        return True

    def should_skip_url(self, url: str) -> bool:
        """Check if URL should be skipped based on extension or pattern."""
        return bool(self.skip_extensions.search(url.lower()))
    
    def extract_urls_from_soup(self, soup) -> set:
        """
        Extract URLs from HTML, including:
        1. Regular <a href> links
        2. Anchor references (#fragment)
        3. Element IDs that represent documentation anchors
        """
        urls = set()
        # Remove any existing anchor from current_url for validation
        base_current_url = self.current_url.split('#')[0] # canonical page URL (no fragment)
        
        for a in soup.find_all('a', href=True):
            href = a['href']
            # skip javascript and mailto
            if href.startswith(('javascript:', 'mailto:')):
                continue
            
            # For anchor references (e.g., #module.class.method)
            if href.startswith('#'):
                reference = href[1:]  # Remove '#'
                if self.is_valid_reference_for_page(base_current_url, reference):
                    url = urljoin(self.current_url, href)
                    if self.is_valid_url_construction(url) and self.is_relevant_url(url) and not self.should_skip_url(url):
                        urls.add(url)
            else:
                # Regular URL (not an anchor)
                url = urljoin(self.current_url, href)
                if self.is_valid_url_construction(url) and self.is_relevant_url(url) and not self.should_skip_url(url):
                    urls.add(url)
                    
        # --- Extract anchor IDs from documentation elements ---
        # Sphinx-generated docs have IDs on dt/div elements for API members
        # This captures anchors even if no <a href> points to them
        for elem in soup.find_all(['dt', 'div', 'section'], id=True):
            anchor_id = elem.get('id', '')
            if anchor_id and '.' in anchor_id:
                # Validate this anchor ID
                if self.is_valid_reference_for_page(base_current_url, anchor_id):
                    url = f"{base_current_url}#{anchor_id}"
                    if self.is_valid_url_construction(url) and self.is_relevant_url(url) and not self.should_skip_url(url):
                        urls.add(url)
                
        return urls
    
    
    def is_possible_api_documentation(self, url: str) -> bool:
        """
        Determine if URL is for API documentation or module-specific page.
        
        Args:
            url (str): The URL to analyze
            
        Returns:
            bool: False if module-specific-based representation, True if API documentation page with only anchor extensions
        """
        
        try:
            # Remove base_url to analyze remaining path
            if not self.base_url:
                return False
            
            url_no_frag = url.split('#')[0]
            
            # --- Handle directory-served pages (e.g., /api/ instead of /api.html) ---
            # These serve index.html implicitly and contain all API members as anchors
            url_path = url_no_frag.rstrip('/')
            expected_dir_path = (self.base_url.rstrip('/') + '/' + self.sub_path).rstrip('/')
            if url_path == expected_dir_path:
                return True  # Directory-served single-page API
            # ---
            
            last = (url[len(self.base_url):]).split('/')[-1].split('#', 1)[0]
            if self.sub_path.lower().endswith('.html'):
                # Single-page API: page file equals sub_path exactly
                return last.lower() == self.sub_path.lower()
            else:
                # Directory mode: page file equals "<sub_path>.html"
                return last.lower() == (self.sub_path + '.html').lower()
            
            # components = (url[len(self.base_url):]).split('/')
            # if (self.sub_path+'.html').lower() == (components[-1]).lower():
            #     return True
            # else:
            #     return False
                        
        except Exception as e:
            print(f"Error determining documentation type: {str(e)}")
            return False

    def get_module_parts(self, url: str) -> tuple[list, list]:
        """
        Extract module parts from URL, returning both split and unsplit versions.
        
        Args:
            url (str): URL to process
            
        Returns:
            tuple: (split_parts, unsplit_parts)
        """
        
        # Remove any anchor if present
        base_url = url.split('#')[0]
        
        # Get filename without .html
        module_path = base_url.split('/')[-1].replace('.html', '')
        module_path = module_path.lower()
        
        # Split by dots only, preserve underscores
        unsplit_parts = module_path.split('.')
        
        # Split by both dots and underscores, filter out empty strings
        split_parts = []
        for part in module_path.split('.'):
            split_parts.extend([p for p in part.split('_') if p])
        return split_parts, unsplit_parts

    
    def is_valid_reference_for_page(self, url: str, reference: str) -> bool:
        """
        Validate anchor reference based on documentation type.
        
        Handles:
        - Module names with underscores (torch_cuda vs torch.cuda)
        - Class methods vs nested module members
        - References with different prefixes
        
        Args:
            url (str): URL to validate against
            reference (str): Anchor reference to check
            
        Returns:
            bool: True if reference is valid for the given URL
        """
        try:
            # Basic structure validation
            if not reference or '.' not in reference:
                return False
                
            # For single-page API docs, accept any valid dotted reference
            if self.is_possible_api_documentation(url):
                return len(reference.split('.')) >= 2
            
            # Get module paths (handles underscores)
            split_parts, unsplit_parts = self.get_module_parts(url)
            reference_parts = reference.lower().split('.')
            ref_str = '.'.join(reference_parts)
            
            # Build comparison strings
            split_url_str = '.'.join(split_parts)
            unsplit_url_str = '.'.join(unsplit_parts)
            
            # Determine if reference uses underscored module names
            unsplit_flag = any('_' in part and part in ref_str for part in unsplit_parts)
            
            # Choose which module representation to use
            module_prefix = unsplit_url_str if unsplit_flag else split_url_str
            module_parts = unsplit_parts if unsplit_flag else split_parts
            
            # Case 1: Exact match - reference IS the module
            if ref_str == module_prefix:
                return True
            
            # Case 2: Reference starts with module prefix
            if ref_str.startswith(module_prefix + '.'):
                # Filter out empty parts from the suffix
                suffix = ref_str[len(module_prefix) + 1:]
                suffix_parts = [p for p in suffix.split('.') if p]
                
                if not suffix_parts:
                    return True
                
                # Single suffix part - always valid (method or attribute)
                if len(suffix_parts) == 1:
                    return True
                
                # Two suffix parts - valid if first part looks like a class (capitalized)
                # This allows: Module.InnerClass.method
                # But rejects: module.submodule.subsubmodule (deeply nested modules)
                if len(suffix_parts) == 2:
                    # Check original case of first suffix part
                    original_ref_parts = reference.split('.')
                    first_suffix_idx = len(module_parts)
                    if first_suffix_idx < len(original_ref_parts):
                        first_suffix = original_ref_parts[first_suffix_idx]
                        # Accept if first suffix is capitalized (class) or if it's a known pattern
                        if first_suffix and (first_suffix[0].isupper() or '_' in first_suffix):
                            return True
                    # Also accept if we can't determine (be permissive)
                    return True
                
                # More than 2 suffix parts - likely too deep, reject
                return False
            
            # Case 3: Module path appears mid-reference (different package prefix)
            # e.g., "somepackage.torch.Stream.method" for torch.Stream.html
            if '.' + module_prefix + '.' in ref_str:
                # Find where the module prefix ends and check suffix depth
                idx = ref_str.index('.' + module_prefix + '.') + len(module_prefix) + 2
                suffix = ref_str[idx:]
                suffix_parts = [p for p in suffix.split('.') if p]
                return len(suffix_parts) <= 2
            
            # Case 4: Reference ends with module path (rare but valid)
            if ref_str.endswith('.' + module_prefix):
                return True
                        
            return False
                        
        except Exception as e:
            logger.warning(f"Error validating reference '{reference}' for '{url}': {e}")
            return False
    
    
    def is_valid_url_construction(self, url: str) -> bool:
        """
        Validate basic URL structure.
        
        Args:
            url (str): The URL to validate
            
        Returns:
            bool: True if URL has valid construction
        """
        
        try:
            # Check for multiple '#' symbols
            if url.count('#') > 1:
                return False
                
            if '#' in url:
                base_url, anchor = url.split('#')
                
                # Skip empty anchors
                if not anchor:
                    return False
                    
                # Ensure anchor has proper structure (contains dots for module reference)
                if not '.' in anchor:
                    return False
                
            return True
                
        except Exception as e:
            print(f"Error validating URL construction: {str(e)}")
            return False

    async def test_url(self, url: str, fetcher: URLFetcher) -> tuple[bool, str]:
        """
        Probe whether a URL resolves to an HTML page using the shared fetcher.
        Returns (is_html, content_type_stringish) where content_type is 'text/html' on success.
        """
        try:
            html = await fetcher.get_html(url, referer=url)
            return (html is not None, 'text/html' if html else '')
        except Exception:
            return (False, '')
    
    async def truncate_url_segments(self, url: str, fetcher: URLFetcher) -> tuple[str, str]:
        """
        Walk backwards from the input URL, probing candidate base URLs.
        Returns (base_url, sub_path) where:
        - base_url: The deepest working container URL
        - sub_path: All segments between base_url and the .html file
        
        Examples:
        - xgboost: .../python/python_api.html → base=.../python/, sub_path=python_api
        - sklearn: .../stable/modules/generated/sklearn...html → base=.../stable/, sub_path=modules/generated
        - pytorch: .../stable/generated/torch.nn.Conv2d.html → base=.../stable/, sub_path=generated
        """
        parsed = urlparse(url.split('#', 1)[0])
        site_root = f"{parsed.scheme}://{parsed.netloc}/"
        path_stripped = parsed.path.strip('/')
        segs = path_stripped.split('/') if path_stripped else []

        # Find the .html file segment (typically the last one)
        html_file_idx = None
        if segs and segs[-1].lower().endswith('.html'):
            html_file_idx = len(segs) - 1

        async def probe_prefix(prefix_segs: list[str]) -> Optional[str]:
            base = site_root
            prefix = '/'.join(prefix_segs)
            candidates = []
            if prefix:
                candidates.append(f"{base}{prefix}/")
                candidates.append(f"{base}{prefix}/index.html")
                candidates.append(f"{base}{prefix}")
            else:
                candidates.append(base)

            for cand in candidates:
                ok, _ = await self.test_url(cand, fetcher)
                if ok:
                    if cand.endswith('/index.html'):
                        return cand[:-11]
                    if cand.endswith('/'):
                        return cand
                    if prefix and cand.endswith(prefix):
                        return f"{base}{prefix}/"
                    return cand
            return None

        # Walk upward from deepest to shallowest
        for i in range(len(segs) - 1, -1, -1):
            container = await probe_prefix(segs[:i])
            if container:
                # Determine sub_path: everything between base and .html file
                if html_file_idx is not None and i <= html_file_idx:
                    sub_path_segs = segs[i:html_file_idx]
                    if sub_path_segs:
                        # Multi-segment sub-path (e.g., "modules/generated")
                        sub_path = '/'.join(sub_path_segs)
                    else:
                        # The .html file itself is the sub-path (e.g., "python_api.html")
                        html_name = segs[html_file_idx]
                        sub_path = html_name
                else:
                    sub_path = segs[i] if i < len(segs) else ""
                
                return container, sub_path

        # Last resort: site root
        ok, _ = await self.test_url(site_root, fetcher)
        return (site_root, "") if ok else (site_root, "")
    
    
    async def process_url(self, current_url: str, fetcher: URLFetcher) -> set:
        """
        Fetch a canonical documentation page (fragment stripped) using URLFetcher.
        Respects robots, limiter and proxies via the shared fetcher.
        
        Args:
            current_url (str): The current URL to process
            fetcher (URLFetcher): The shared fetcher to use
        Returns:
            set[str]: Discovered URLs (may include anchors), or empty set on failure.
        """
        fetch_url = current_url.split('#', 1)[0] # canonical (no fragment)
        
        # Avoid reprocessing the same page
        if fetch_url in self.visited_urls or fetch_url in self.queued_urls:
            logger.debug(f"Skipping already processed URL: {current_url}")
            return set()

        self.queued_urls.add(fetch_url)
        self.current_url = fetch_url
        logger.debug(f"Processing URL: {fetch_url}")
        
        try:
            if self.is_relevant_url(current_url): # relevance can include anchors
                html = await fetcher.get_html(fetch_url, referer=self.base_url or fetch_url)
                if not html:
                    return set()
                soup = BeautifulSoup(html, 'lxml')
                extracted_urls = self.extract_urls_from_soup(soup)
                logger.debug(f"Found {len(extracted_urls)} URLs in {fetch_url}")
                return extracted_urls
            return set()
                
        
        except Exception as e:
            logger.error(f"Error processing {fetch_url}: {str(e)}")
            self.stats['failed_urls'] += 1
        finally:
            # Mark as visited regardless of outcome to avoid retries in this run
            self.visited_urls.add(fetch_url)
            self.queued_urls.remove(fetch_url)
        return set()           
    
    
    async def scrape_urls(self, start_url: str):
        """Main scraping method using shared URLFetcher (robots, limiter, proxies)."""
        self.stats['start_time'] = time.time()
        logger.info(f"Starting URL scraping from: {start_url}")

        try:
            async with URLFetcher() as fetcher:
                # Determine base URL and sub-path (used by relevance rules)
                self.base_url, self.sub_path = await self.truncate_url_segments(start_url, fetcher)
                logger.info(f"Base URL: {self.base_url}")
                logger.info(f"Sub-path: {self.sub_path}")
                print(f"Base URL: {self.base_url}")
                print(f"Sub-path: {self.sub_path}")

                start_canonical = start_url.split('#', 1)[0]
                self.seed_dir = start_canonical.rsplit('/', 1)[0] + '/'
                urls_to_visit = deque([start_canonical])

                while urls_to_visit:
                    batch = []
                    batch_size = min(self.max_workers, len(urls_to_visit))
                    for _ in range(batch_size):
                        if urls_to_visit:
                            u = urls_to_visit.popleft()
                            if u not in self.queued_urls:
                                batch.append(u)
                    if not batch:
                        continue

                    logger.debug(f"Processing batch of {len(batch)} URLs")
                    tasks = [self.process_url(u, fetcher) for u in batch]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    for result in results:
                        if isinstance(result, set):
                            self.stats['processed_urls'] += 1
                            # Keep raw extracted URLs (including anchors) for final dedup/output
                            self.raw_urls.update(result)
                            # Queue only canonical pages (strip anchors) to avoid refetching same page
                            canonical_new = {url.split('#', 1)[0] for url in result}
                            # Filter out already seen/queued canonical pages
                            new_pages = [u for u in canonical_new if u not in self.visited_urls and u not in self.queued_urls]
                            urls_to_visit.extend(new_pages)
                        else:
                            self.stats['failed_urls'] += 1
                            logger.error(f"Failed to process URL in batch: {str(result)}")

                logger.info("Analyzing and deduplicating URLs...")
                self.relevant_urls = self._analyze_and_deduplicate_urls()
                logger.info(f"Found {len(self.relevant_urls)} relevant URLs")
                return self.relevant_urls

        except Exception as e:
            logger.error(f"Error during URL scraping: {str(e)}")
            raise URLScraperError(
                message="URL scraping failed",
                url=start_url,
                details={"error": str(e)}
            )
        finally:
            self.stats['end_time'] = time.time()
            self._get_stats()
    
    
    def _analyze_and_deduplicate_urls(self):
        """
        Deduplicate URLs while handling dot/hyphen variants in fragments.
        """
        def split_url(url):
            parsed = urlparse(url)
            base = parsed._replace(fragment='').geturl()
            return base, parsed.fragment

        url_groups = {}
        for url in self.raw_urls:
            base_url, fragment = split_url(url)
            if base_url not in url_groups:
                url_groups[base_url] = {
                    'base': base_url,
                    'fragments': set()
                }
            if fragment:
                url_groups[base_url]['fragments'].add(fragment)

        deduplicated_urls = set()
        
        for group in url_groups.values():
            base_url = group['base']
            fragments = group['fragments']
            
            # Always include base URL
            deduplicated_urls.add(base_url)
            
            if fragments:
                # Group by normalized form
                normalized_fragments = {}
                for fragment in fragments:
                    norm_fragment = fragment.replace('-', '.')
                    if norm_fragment not in normalized_fragments:
                        normalized_fragments[norm_fragment] = []
                    normalized_fragments[norm_fragment].append(fragment)

                # Process each normalized group
                for norm_fragment, variants in normalized_fragments.items():
                    base_mmPath = base_url[len(self.base_url)+len(self.sub_path):].lstrip('/').split('.html')[0]
                    # Filter identical module member path in url and fragment
                    if base_mmPath == norm_fragment or base_mmPath == variants[0]:
                        continue
                    
                    if len(variants) > 1:
                        # Multiple variants - use dot notation
                        deduplicated_urls.add(f"{base_url}#{norm_fragment}")
                    else:
                        # Single variant - keep original
                        deduplicated_urls.add(f"{base_url}#{variants[0]}")

        return sorted(deduplicated_urls)

        
    def _get_stats(self):
        """
        Compute comprehensive scraping statistics.
        """
        
        duration = self.stats['end_time'] - self.stats['start_time']
        
        self.stat_dict["base_url"] = self.base_url
        self.stat_dict["sub_path"] = self.sub_path
        
        self.stat_dict["Duration"] = f"{duration:.2f} seconds"
        self.stat_dict["Total_URLs_processed"] = f"{self.stats['processed_urls']}"
        self.stat_dict["Failed_URLs"] = f"{self.stats['failed_urls']}"
        self.stat_dict["Raw_URLs_found"] = f"{len(self.raw_urls)}"
        self.stat_dict["Final_deduplicated_URLs"] = f"{len(self.relevant_urls)}"
        self.stat_dict["Deduplication_ratio"] = (
            "N/A" if len(self.raw_urls) == 0
            else f"{len(self.relevant_urls)/len(self.raw_urls)*100:.1f}%"
        )
        self.stat_dict["Success_rate"] = (
            "N/A" if self.stats['processed_urls'] == 0
            else f"{(self.stats['processed_urls'] - self.stats['failed_urls']) / self.stats['processed_urls'] * 100:.2f}%"
        )
        self.stat_dict["URLs_processed_per_second"] = (
            "N/A" if (self.stats['end_time'] - self.stats['start_time']) <= 0
            else f"{self.stats['processed_urls'] / (self.stats['end_time'] - self.stats['start_time']):.2f}"
        )
        
        
async def save_urls_to_file(doc_url: str, lib_name: str, version: str):
    """
    Save the scraped URLs and statistics.
    
    Args:
        urls (set): Set of URLs to save
        base_filename (str): Base name for the output file
    """
    try:
        
        scraper = URLScraper()
        urls = await scraper.scrape_urls(doc_url)
        stat_info = scraper.stat_dict
        
        logger.info(f"\nTotal relevant URLs found: {len(urls)}")
        
        if not urls:
            file_path = BASE_DIR / "crawled_URLs" / lib_name / f"v_{version}" 
            file_path.mkdir(parents=True, exist_ok=True)
            stat_file = file_path / f"{lib_name}_statistics.json"
            with open(stat_file, "w") as f:
                json.dump(stat_info, f, indent=4)
            return None, str(stat_file)
        
        file_path = BASE_DIR / "crawled_URLs" / lib_name / f"v_{version}"
        try:
            file_path.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Created directory: {file_path}")
        except PermissionError as e:
            raise URLScraperError(
                message="Permission denied creating directory",
                details={"path": str(file_path), "error": str(e)}
            )
        except Exception as e:
            raise URLScraperError(
                message="Failed to create directory",
                details={"path": str(file_path), "error": str(e)}
            )
        
        url_file = file_path / "scraped_urls.txt"
        stat_file = file_path / "statistics.json"
        
        try:
            # Save URLs
            with open(url_file, 'w', encoding='utf-8') as f:
                for url in sorted(urls):
                    f.write(f"{url}\n")
            logger.debug(f"Saved URLs to: {url_file}")
        except Exception as e:
            raise URLScraperError(
                message="Error saving scraped URLs to file",
                details={"path": str(url_file), "error": str(e)}
            )
        
        try:
            # Save statistics
            with open(stat_file, "w") as f:
                json.dump(stat_info, f, indent=4)
            logger.debug(f"Saved statistics to: {stat_file}")
        except Exception as e:
            raise URLScraperError(
                message="Error saving stat_info to file",
                details={"path": str(stat_file), "error": str(e)}
            )
        
        return str(url_file), stat_info
    
    except Exception as e:
        raise URLScraperError(
            message="Failed to save URL and stat files",
            details={
                "error": str(e),
                "urls_count": len(urls),
                "lib_name": lib_name if 'lib_name' in locals() else None
            }
        )

