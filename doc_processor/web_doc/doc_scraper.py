import re
import sys
import json
import html
import logging
import asyncio
import requests
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup, Tag, NavigableString


ROOT = Path(__file__).resolve().parents[1]  # project root
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
        
from web_doc.network import URLFetcher

BASE_DIR = Path(__file__).resolve().parents[2] / 'doc_processor' / 'doc_artifacts'

logger = logging.getLogger(__name__)


__all__ = ["scrape_doc"]


class DocScraper:
    """
    A class to extract reference documentation from a given URL while preserving structure and embedded URLs.

    This class uses BeautifulSoup to parse HTML content and extract text from the main content area.
    It handles various documentation structures and aims to maintain the formatting of the original documentation.
    """

    # Mappings from unicode characters to ASCII equivalents
    UNICODE_MAPPINGS = {
        '\u2026': '...',     # Horizontal ellipsis
        '\u2013': '-',       # En dash
        '\u2014': '--',      # Em dash
        '\u00A0': ' ',       # Non-breaking space
        '\u00B7': '·',       # Middle dot
        '\u2022': '*',       # Bullet
        '\u2192': '->',      # Right arrow
        '\u2190': '<-',      # Left arrow
        '\u2194': '<->',     # Left-right arrow
        '\u21D2': '=>',      # Right double arrow
        '\u21D0': '<=',      # Left double arrow
        '\u21D4': '<=>',     # Left-right double arrow
        '\u2018': "'",       # Left single quote
        '\u2019': "'",       # Right single quote
        '\u201C': '"',       # Left double quote
        '\u201D': '"',       # Right double quote
        '\u00AB': '<<',      # Left-pointing double angle
        '\u00BB': '>>',      # Right-pointing double angle
    }

    def __init__(self, url: str):
        """
        Initialize the extractor with the URL.

        Args:
            url (str): The URL of the documentation page to extract.
        """
        self.url = url
        self.session = requests.Session()
        self.soup = None
        self.lib_name = ''
        self.metadata = None

    def clean_text(self, text: str) -> str:
        """
        Clean text while preserving exact formatting and special characters.

        Args:
            text (str): The text to clean.

        Returns:
            str: The cleaned text.
        """
        # Basic HTML entity decoding
        text = html.unescape(text)
        # Replace unicode characters with ASCII equivalents
        for uni_char, ascii_equiv in self.UNICODE_MAPPINGS.items():
            text = text.replace(uni_char, ascii_equiv)
        # Remove zero-width and control characters
        text = re.sub(r'[\u200b\u200c\u200d\ufeff]', '', text)
        # Preserve original whitespace and newlines
        return text

    
    async def fetch_page_async(self, net: URLFetcher) -> Optional[str]:
        """
        Async fetch of the page using shared URLFetcher (proxies, limiter, robots, TLS, fallback).
        
        Returns:
            Optional[str]: The HTML content of the page, or None if the request fails.
        """
        try:
            html_text = await net.get_html(self.url, referer=self.url)
            return html_text
        except Exception as e:
            logger.error(f"Failed to fetch page async: {e}")
            return None
    

    def find_main_content(self) -> Optional[Tag]:
        """
        Find the main content container using various heuristics.

        Returns:
            Optional[Tag]: The main content tag, or None if not found.
        """
        # Try to find main content based on known classes
        content_classes = ['main-content', 'content', 'docs-content', 'api-content', 'reference-content', 'markdown-content']
        for class_name in content_classes:
            content = self.soup.find(class_=re.compile(class_name, re.IGNORECASE))
            if content and self._has_documentation_structure(content):
                return content

        # Try semantic HTML5 elements
        for tag_name in ['main', 'article', 'section']:
            contents = self.soup.find_all(tag_name)
            for content in contents:
                if self._has_documentation_structure(content):
                    return content

        # Fall back to body content
        body = self.soup.find('body')
        if body and self._has_documentation_structure(body):
            return body

        return self.soup

    def _has_documentation_structure(self, element: Tag) -> bool:
        """
        Verify if the element contains structured documentation content.

        Args:
            element (Tag): The BeautifulSoup tag to check.

        Returns:
            bool: True if it contains documentation content, False otherwise.
        """
        if not element:
            return False
        # Check for common documentation structural elements
        has_headings = element.find(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']) is not None
        has_code = element.find(['pre', 'code']) is not None
        has_definitions = element.find(['dl', 'dt', 'dd']) is not None
        has_tables = element.find('table') is not None  # Added to check for tables
        # Check for meaningful text content
        text_content = element.get_text(strip=True)
        has_substantial_content = bool(text_content)
        # Element should have at least some structural elements and meaningful content
        return has_substantial_content and (has_headings or has_code or has_definitions or has_tables)

    
    async def extract_text_async(self, net: URLFetcher) -> Optional[str]:
        """
        Extract text with error handling and validation.

        Returns:
            Optional[str]: The extracted text, or None if extraction fails.
        """
        try:
            html_content = await self.fetch_page_async(net)
            if not html_content:
                return None

            self.soup = BeautifulSoup(html_content, 'lxml')

            # Remove unwanted elements early
            for element in self.soup.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                element.decompose()
                
            for br in self.soup.find_all('br'):
                br.replace_with("\n")

            main_content = self.find_main_content()
            if not main_content:
                logger.warning("Could not find main content section")
                return None

            extracted_text = self.process_element(main_content)
            
            # Validate extracted content
            if not self.validate_extracted_text(extracted_text):
                logger.warning("Extracted text validation failed")
                return None

            # Remove extra blank lines
            extracted_text = re.sub(r'\n{3,}', '\n\n', extracted_text)
            return extracted_text.strip()
        except Exception:
            logger.error("Error during async text extraction", exc_info=True)
            return None
        

    def validate_extracted_text(self, text: str) -> bool:
        """
        Validate the structural integrity of extracted documentation text.

        Args:
            text (str): The extracted text to validate.

        Returns:
            bool: True if valid, False otherwise.
        """
        if not text or not text.strip():
            return False
        # Check for basic structural elements
        has_content = len(text.split()) > 0
        has_structure = '\n' in text
        # Verify the text contains meaningful content
        contains_meaningful_content = has_content and has_structure and not text.isspace()
        return contains_meaningful_content

    def process_element(self, element: Tag) -> str:
        """
        Process an element recursively, handling different tags appropriately.

        Args:
            element (Tag): The BeautifulSoup tag to process.

        Returns:
            str: The text content of the element.
        """
        # If it's a NavigableString, clean and return it
        if isinstance(element, NavigableString):
            return self.clean_text(str(element))

        # Skip certain elements to prevent unwanted content
        if element.name == 'annotation':
            return ''
        if element.get('class'):
            if 'hidden' in element.get('class'):
                return ''

        text_parts = []

        # Handle KaTeX equations
        if element.name == 'span' and 'katex' in element.get('class', []):
            # Find the 'annotation' element within 'katex-mathml'
            annotation = element.find('annotation')
            if annotation and annotation.string:
                latex_code = annotation.string.strip()
                # Determine if it's display math or inline math
                display_math = element.find_parents(class_='display')
                return latex_code
            else:
                # Fallback to extracting text content from 'katex-html'
                katex_html = element.find('span', class_='katex-html')
                if katex_html:
                    text = katex_html.get_text()
                    return self.clean_text(text)
                else:
                    return self.clean_text(element.get_text())

        # Handle input elements (e.g., within forms)
        if element.name == 'input':
            input_type = element.get('type', '').lower()
            input_value = element.get('value', '').strip()
            if input_type == 'submit' and input_value:
                return self.clean_text(input_value)
            elif input_value:
                return self.clean_text(input_value)
            else:
                return ''

        # Handle 'form' elements
        if element.name == 'form':
            content = ''.join(self.process_element(child) for child in element.children)
            return content

        # Handle block-level elements (preserve spaces and newlines)
        if element.name in ['div', 'section', 'article']:
            content = ''.join(self.process_element(child) for child in element.children)
            return f"{content.strip()}\n\n"

        # Handle paragraphs with class 'rubric' (subsection headers)
        if element.name == 'p' and 'rubric' in element.get('class', []):
            content = self.clean_text(element.get_text())
            return f"{content}\n\n"
            
        # Handle paragraphs
        # elif
        if element.name == 'p':
            content = ''.join(self.process_element(child) for child in element.children)
            return f"{content.strip()}\n\n"

        # Handle headings ###
        if element.name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            # Exclude 'headerlink' symbols within the heading
            heading_content = ''.join(
                self.process_element(child) for child in element.contents if not (
                    isinstance(child, Tag) and 'headerlink' in child.get('class', [])
                )
            ).strip()
            heading_level = int(element.name[1])
            return f"{'#' * heading_level} {heading_content}\n\n"

        # Handle lists
        if element.name in ['ul', 'ol']:
            items = []
            for idx, li in enumerate(element.find_all('li', recursive=False)):
                li_text = self.process_element(li).strip()
                prefix = '-' if element.name == 'ul' else f"{idx + 1}."
                items.append(f"{prefix} {li_text}")
            return '\n'.join(items) + '\n\n'

        # Handle list items
        if element.name == 'li':
            content = ''.join(self.process_element(child) for child in element.children)
            return content.strip()  # List item processing handled in parent

        # Handle definition lists (parameters, definitions)
        if element.name == 'dl':
            terms = []
            dt_elements = element.find_all('dt', recursive=False)
            for dt in dt_elements:
                dd = dt.find_next_sibling('dd')
                if dd:
                    dt_text = self.process_definition_term(dt).strip()
                    dd_text = self.process_element(dd).strip()
                    terms.append(f"{dt_text}\n    {dd_text}")
            return '\n\n'.join(terms) + '\n\n'

        if element.name == 'dd':
            content = ''.join(self.process_element(child) for child in element.children)
            return content.strip()

        # Handle code blocks
        if element.name == 'pre':
            code_text = element.get_text()
            return f"```\n{code_text}\n```\n\n"

        # Handle inline code
        if element.name == 'code':
            code_text = ''.join(self.process_element(child) for child in element.children)
            # Check if the code is within a link
            if element.parent and element.parent.name == 'a':
                return code_text.strip()  # Do not wrap in backticks if inside a link
            else:
                return f"{code_text.strip()}"

        # Handle hyperlinks
        if element.name == 'a':
            href = element.get('href', '')
            link_text = ''.join(self.process_element(child) for child in element.children).strip()
            if href and not href.startswith('javascript:'):
                url = urljoin(self.url, href)
                if " " in link_text:
                    return f"[{link_text}]({url})"
                else:
                    return f"{link_text}({url})"
            else:
                return link_text

        # Handle tables
        if element.name == 'table':
            return self.process_table(element)

        # Handle class/function definitions
        if element.name == 'dt':
            # return self.process_class_or_function_definition(element)  
            if self.is_class_or_function_definition(element):
                return self.process_class_or_function_definition(element)
            else:
                dt_text = ''.join(self.process_element(child) for child in element.children).strip()
                return f"{dt_text}\n"

        # Handle other inline elements
        if element.name in ['strong', 'b', 'em', 'i', 'u', 'span', 'var']:
            content = ''.join(self.process_element(child) for child in element.children)
            return content

        # Process other tags by processing their children
        for child in element.children:
            child_text = self.process_element(child)
            if child_text:
                text_parts.append(child_text)
        return ''.join(text_parts)
    
    def process_definition_term(self, element: Tag) -> str:
        """
        Process a definition term (<dt>), handling parameter names and types.
        """
        param_name = ''
        param_type = ''
        rest = ''
        for child in element.children:
            if isinstance(child, Tag):
                if child.name == 'strong':
                    param_name = self.process_element(child).strip()
                elif child.name == 'span' and 'classifier' in child.get('class', []): ### checking for 'classifier maybe a bit too strict and not adaptable'
                    param_type = self.process_element(child).strip()
                else:
                    rest += self.process_element(child)
            elif isinstance(child, NavigableString):
                rest += self.clean_text(str(child))
        if param_type:
            dt_text = f"{param_name} : {param_type}{rest}"
        else:
            dt_text = f"{param_name}{rest}"
        return dt_text.strip()

    def is_class_or_function_definition(self, element: Tag) -> bool:
        """
        Check if an element is a class or function definition.

        Args:
            element (Tag): The element to check.

        Returns:
            bool: True if it's a class or function definition, False otherwise.
        """
        # Check if the element contains 'class' or 'def' or has a function signature
        text = element.get_text().strip()
        # Common patterns for class or function definitions
        return bool(re.match(r'^(class)\s+\w+', text)) or ('(' in text and ')' in text)

    def process_class_or_function_definition(self, element: Tag) -> str:
        """
        Process class or function definitions, preserving signatures and associated URLs.

        Args:
            element (Tag): The definition term <dt> tag.

        Returns:
            str: The processed class or function definition.
        """
        
        # Extract the signature text, including all nested elements
        signature = ''.join(self.process_element(child) for child in element.children).strip()
        
        # Include any associated URL using the element's ID
        if 'id' in element.attrs:
            anchor_id = element['id']
            # Construct the full URL to the definition
            url = urljoin(self.url, f"#{anchor_id}")
            if url:
                signature += f" ({url})"
        
        return f"{signature}\n\n"

    def process_table(self, element: Tag) -> str:
        """
        Process an HTML table and convert it into markdown format.
        """
        def get_cell_text(cell):
            return self.process_element(cell).strip()

        headers = []
        rows = []

        # Process table headers
        thead = element.find('thead')
        if thead:
            header_rows = thead.find_all('tr')
        else:
            # If no thead, consider the first row as header
            header_rows = ''
        for header_row in header_rows:
            header_cells = header_row.find_all(['th', 'td'], recursive=False)
            headers = [get_cell_text(cell) for cell in header_cells]

        # Process table rows
        tbody = element.find('tbody')
        if tbody:
            row_elements = tbody.find_all('tr', recursive=False)
        else:
            # If no tbody, get all rows excluding headers
            row_elements = element.find_all('tr', recursive=False)[len(header_rows):]
        for row in row_elements:
            cells = row.find_all(['th', 'td'], recursive=False)
            rows.append([get_cell_text(cell) for cell in cells])

        # Determine column widths
        col_count = max(len(headers), max((len(row) for row in rows), default=0))
        col_widths = [0] * col_count
        for idx in range(col_count):
            header_len = len(headers[idx]) if idx < len(headers) else 0
            max_cell_len = max((len(row[idx]) for row in rows if len(row) > idx), default=0)
            col_widths[idx] = max(header_len, max_cell_len)

        # Build markdown table
        table_lines = []

        # Header
        if headers:
            header_line = ' | '.join(headers[idx].ljust(col_widths[idx]) for idx in range(col_count))
            separator_line = ' | '.join('-' * col_widths[idx] for idx in range(col_count))
            table_lines.append(header_line)
            table_lines.append(separator_line)

        # Rows
        for row in rows:
            row_line = ' | '.join((row[idx] if idx < len(row) else '').ljust(col_widths[idx]) for idx in range(col_count))
            table_lines.append(row_line)

        return '\n'.join(table_lines) + '\n\n'
        
        
def _merge_members_json(path: Path, new_data: dict) -> None:
    """
    Merge new members with existing members.json, rather than overwriting.
    
    Handles two formats:
        - per_page: {"API_names": [...]}
        - per_module: {"container_name": [...members...], ...}
    
    Args:
        path: Path to members.json file
        new_data: New data to merge in
    """
    existing = {}
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            existing = {}
    
    # Merge logic based on format
    if 'API_names' in new_data:
        # per_page format: {"API_names": [...]}
        existing_set = set(existing.get('API_names', []))
        existing_set.update(new_data.get('API_names', []))
        existing['API_names'] = sorted(existing_set)
    else:
        # per_module format: {"container": [members], ...}
        for container, members in new_data.items():
            existing_set = set(existing.get(container, []))
            existing_set.update(members)
            existing[container] = sorted(existing_set)
    
    # Write merged result
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(existing, f, indent=4, ensure_ascii=False)
            

async def scrape_doc(lib_name: str, version: str, doc_url_path: str, metadata: dict, respect_robots: bool = True, name_fn=None):
    """
    Scrape documentation and save it along with its associated module member metadata.
    
    Adapts to the documentation layout:
        - per_member: One HTML page per API member
        - per_module: One page per module/class with multiple members
        - per_page: Single page containing all API docs (like XGBoost)
    
    Args:
        lib_name: Library name
        version: Version string
        doc_url_path: Path to scraped_urls.txt containing URLs to scrape
        metadata: Dict with 'base_url' and 'sub_path' from crawler
        respect_robots: Whether to respect robots.txt allow/deny rules
        name_fn: name used for per-member files
    """
    base_url = metadata['base_url']
    sub_path = metadata['sub_path']
    url_pattern = sub_path if sub_path else base_url
    
    # Read URLs first to determine the actual layout
    with open(doc_url_path, 'r', encoding='utf-8') as f:
        doc_url_lines = f.readlines()
    
    # Determine per_page_flag by counting unique base URLs (before #)
    # If ALL URLs share the same base (ignoring fragments), it's per_page
    unique_base_urls = set()
    for line in doc_url_lines:
        line = line.strip()
        if not line:
            continue
        # Extract base URL (before the fragment)
        base = line.split('#')[0].strip()
        if base:
            unique_base_urls.add(base)
    
    # per_page: Only ONE unique base URL (all APIs on single page)
    # per_module/per_member: Multiple unique base URLs
    per_page_flag = len(unique_base_urls) == 1 and url_pattern.endswith('.html')
    
    # Group URLs by type and collect fragment information
    url_grouped = {'per_member': [], 'per_module': [], 'per_page': []}
    per_page_fragments = {'API_names': set()}
    per_module_fragments = {}  # {container_name: set(member_names)}
    
    visited_urls = set()
    
    for line in doc_url_lines:
        line = line.strip()
        if not line:
            continue
        
        if per_page_flag:
            # per_page: All APIs on single page with #fragment anchors
            # URL format: "https://xgboost.readthedocs.io/python/python_api.html#xgboost.DMatrix"
            if '#' in line:
                url_to_check, fragment = line.rsplit('#', 1)
                per_page_fragments['API_names'].add(fragment)
            else:
                url_to_check = line
            
            url_to_check = url_to_check.strip()
            if url_to_check not in visited_urls:
                visited_urls.add(url_to_check)
                url_grouped['per_page'].append(url_to_check)
        else:
            # Not per_page: Could be per_module or per_member
            if '#' in line:
                url_to_check, fragment = line.rsplit('#', 1)
            else:
                url_to_check = line
                fragment = None
            
            url_to_check = url_to_check.strip()
            
            if fragment:
                # per_module: Page has multiple members identified by fragments
                # URL format: "https://pytorch.org/docs/torch.Tensor.html#torch.Tensor.abs"
                # Extract container name from URL path
                try:
                    # Get filename without .html extension
                    container_name = url_to_check.split('/')[-1].split('.html')[0]
                    per_module_fragments.setdefault(container_name, set()).add(fragment)
                except (IndexError, ValueError):
                    pass
                
                if url_to_check not in visited_urls:
                    visited_urls.add(url_to_check)
                    url_grouped['per_module'].append(url_to_check)
            else:
                # per_member: Each page is dedicated to one member
                if url_to_check not in visited_urls:
                    visited_urls.add(url_to_check)
                    url_grouped['per_member'].append(url_to_check)
    
    # Create output directories and save members.json
    scraped_doc_file_path = BASE_DIR / "scraped_doc" / lib_name / f"v_{version}"
    
    per_page_path = None
    per_module_path = None
    per_member_path = None
    
    # --- per_page setup ---
    if url_grouped['per_page'] or per_page_fragments['API_names']:
        per_page_path = scraped_doc_file_path / "per_page"
        per_page_path.mkdir(parents=True, exist_ok=True)
        
        # Merge with existing members.json
        per_page_serializable = {
            'API_names': list(per_page_fragments['API_names'])
        }
        _merge_members_json(per_page_path / "members.json", per_page_serializable)
        logger.info(f"Updated per_page/members.json with {len(per_page_fragments['API_names'])} API names")
    
    # --- per_module setup ---
    if url_grouped['per_module'] or per_module_fragments:
        per_module_path = scraped_doc_file_path / "per_module"
        per_module_path.mkdir(parents=True, exist_ok=True)
        
        # Merge with existing members.json
        per_module_serializable = {k: list(v) for k, v in per_module_fragments.items()}
        _merge_members_json(per_module_path / "members.json", per_module_serializable)
        logger.info(f"Updated per_module/members.json with {len(per_module_fragments)} containers")
    
    # --- per_member setup ---
    if url_grouped['per_member']:
        per_member_path = scraped_doc_file_path / "per_member"
        per_member_path.mkdir(parents=True, exist_ok=True)
    
    # --- Scrape all URLs concurrently ---
    max_concurrency = 6
    sem = asyncio.Semaphore(max_concurrency)
    
    async with URLFetcher(respect_robots=respect_robots) as net:
        async def fetch_and_write(url: str, out_path: str):
            """Fetch URL and write normalized text to file."""
            async with sem:
                try:
                    ds = DocScraper(url)
                    text = await ds.extract_text_async(net)
                    if text:
                        with open(out_path, 'w', encoding='utf-8') as f:
                            f.write(text)
                        logger.debug(f"Scraped: {out_path}")
                except Exception as e:
                    logger.warning(f"Failed to scrape {url}: {e}")
        
        tasks = []
        
        # per_page: All go to single APIs.txt
        for url in url_grouped['per_page']:
            if per_page_path:
                out_path = str(per_page_path / "APIs.txt")
                tasks.append(fetch_and_write(url, out_path))
        
        # per_module: Each page becomes {container_name}.txt
        for url in url_grouped['per_module']:
            if per_module_path:
                filename = url.split('/')[-1].split('.html')[0] + ".txt"
                out_path = str(per_module_path / filename)
                tasks.append(fetch_and_write(url, out_path))
        
        # per_member: Each page becomes {member_name}.txt
        for url in url_grouped['per_member']:
            if per_member_path:
                stem = url.split('/')[-1].split('.html')[0]
                if name_fn:
                    stem = name_fn(stem)        # disambiguate case-shared names (e.g. torch.xpu.stream)
                out_path = str(per_member_path / f"{stem}.txt")
                tasks.append(fetch_and_write(url, out_path))
        
        if tasks:
            logger.info(f"Scraping {len(tasks)} pages...")
            await asyncio.gather(*tasks)
            logger.info("Scraping complete.")
        

    
