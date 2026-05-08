"""
URL to API Name Extraction

Handles extraction of API names from documentation URLs across different
documentation frameworks (Sphinx, MkDocs, etc.) and URL patterns.
"""

import re
import json
import logging
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import Set, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ExtractionConfig:
    """Configuration for API name extraction."""
    # Prefixes to skip in URL paths (e.g., 'generated/', 'reference/')
    skip_prefixes: List[str] = field(default_factory=lambda: ['generated', 'reference', 'api', '_autosummary', 'autoapi', 'apidoc', 'modules', 'source'])
    # File extensions to strip
    strip_extensions: List[str] = field(default_factory=lambda: ['.html', '.htm', '.rst', '.md'])
    # Patterns indicating non-API pages
    non_api_patterns: List[str] = field(default_factory=lambda: [
        'index', 'genindex', 'modindex', 'search', 'py-modindex',
        'changelog', 'contributing', 'license', 'readme', 'installation',
        'quickstart', 'tutorial', 'guide', 'faq', 'glossary'
    ])


def extract_api_name_from_url(
    url: str, 
    base_url: str, 
    sub_path: str = "",
    config: ExtractionConfig = None
) -> Optional[str]:
    """
    Extract API name from a documentation URL.
    
    Handles multiple URL patterns:
    - Fragment anchors: base.html#torch.nn.Conv1d -> torch.nn.Conv1d
    - Direct path: torch.nn.Conv1d.html -> torch.nn.Conv1d
    - Hyphenated: torch-nn-Conv1d.html -> torch.nn.Conv1d
    - Subdirs: torch/nn/Conv1d.html -> torch.nn.Conv1d
    - Generated prefix: generated/torch.nn.Conv1d.html -> torch.nn.Conv1d
    
    Args:
        url: The documentation URL to parse
        base_url: The base URL of the documentation site
        sub_path: The API documentation sub-path (e.g., 'reference/api')
        config: Extraction configuration
        
    Returns:
        Extracted API name, or None if extraction fails
    """
    if config is None:
        config = ExtractionConfig()
    
    try:
        parsed = urlparse(url)
        
        # Case 1: URL has fragment anchor -> use fragment as API name
        if parsed.fragment:
            api_name = parsed.fragment.replace('-', '.')
            # Clean up any URL encoding
            api_name = unquote(api_name)
            return _clean_api_name(api_name)
        
        # Case 2: Extract from path
        full_path = unquote(parsed.path)
        
        # Build the prefix to remove (base_url path + sub_path)
        base_parsed = urlparse(base_url)
        prefix = base_parsed.path.rstrip('/')
        if sub_path:
            prefix = prefix + '/' + sub_path.strip('/')
        
        # Get the remainder after removing prefix
        if full_path.startswith(prefix):
            remainder = full_path[len(prefix):].lstrip('/')
        else:
            # Fallback: just use the filename
            remainder = full_path.split('/')[-1]
        
        # Strip file extensions
        for ext in config.strip_extensions:
            if remainder.endswith(ext):
                remainder = remainder[:-len(ext)]
                break
        
        # Skip common prefixes
        for skip in config.skip_prefixes:
            if remainder.startswith(skip + '/'):
                remainder = remainder[len(skip) + 1:]
            elif remainder.startswith(skip + '.'):
                remainder = remainder[len(skip) + 1:]
        
        # Check for non-API pages
        remainder_lower = remainder.lower()
        for pattern in config.non_api_patterns:
            if remainder_lower == pattern or remainder_lower.endswith('/' + pattern):
                return None
        
        # Normalize: hyphens and slashes to dots
        api_name = remainder.replace('-', '.').replace('/', '.')
        
        return _clean_api_name(api_name)
        
    except Exception as e:
        logger.warning(f"Failed to extract API name from URL '{url}': {e}")
        return None


def _clean_api_name(api_name: str) -> Optional[str]:
    """Clean and validate an extracted API name."""
    if not api_name:
        return None
    
    # Remove leading/trailing dots
    api_name = api_name.strip('.')
    
    # Remove consecutive dots
    while '..' in api_name:
        api_name = api_name.replace('..', '.')
    
    # Must have at least one component
    if not api_name or api_name == '.':
        return None
    
    # Basic validation: should look like a Python qualified name
    # Allow letters, digits, underscores, and dots
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$', api_name):
        # Try one more cleanup - might have trailing numbers or special chars
        parts = api_name.split('.')
        cleaned_parts = []
        for part in parts:
            # Keep only valid identifier characters
            cleaned = re.sub(r'[^a-zA-Z0-9_]', '', part)
            if cleaned and (cleaned[0].isalpha() or cleaned[0] == '_'):
                cleaned_parts.append(cleaned)
        
        if cleaned_parts:
            api_name = '.'.join(cleaned_parts)
        else:
            return None
    
    return api_name


class URLAPIExtractor:
    """
    Extract API names from crawled URL results.
    
    Usage:
        extractor = URLAPIExtractor(base_url, sub_path)
        api_names = extractor.extract_from_file('crawl_results.txt')
    """
    
    def __init__(
        self, 
        base_url: str, 
        sub_path: str = "",
        config: ExtractionConfig = None
    ):
        self.base_url = base_url.rstrip('/')
        self.sub_path = sub_path.strip('/')
        self.config = config or ExtractionConfig()
        
        # Statistics
        self.stats = {
            'total_urls': 0,
            'extracted': 0,
            'failed': 0,
            'filtered_non_api': 0
        }
    
    def extract_from_file(self, filepath: str) -> Set[str]:
        """
        Extract API names from a file containing crawled URLs.
        
        Args:
            filepath: Path to file with one URL per line, or JSON file
            
        Returns:
            Set of extracted API names
        """
        filepath = Path(filepath)
        
        if filepath.suffix == '.json':
            return self._extract_from_json(filepath)
        else:
            return self._extract_from_text(filepath)
    
    def _extract_from_text(self, filepath: Path) -> Set[str]:
        """Extract from plain text file (one URL per line)."""
        api_names = set()
        
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                url = line.strip()
                if not url or url.startswith('#'):
                    continue
                
                self.stats['total_urls'] += 1
                api_name = extract_api_name_from_url(
                    url, self.base_url, self.sub_path, self.config
                )
                
                if api_name:
                    api_names.add(api_name)
                    self.stats['extracted'] += 1
                else:
                    self.stats['failed'] += 1
        
        return api_names
    
    def _extract_from_json(self, filepath: Path) -> Set[str]:
        """Extract from JSON file (expects 'urls' key or list)."""
        api_names = set()
        
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Handle different JSON structures
        if isinstance(data, list):
            urls = data
        elif isinstance(data, dict):
            urls = data.get('urls', data.get('relevant_urls', []))
        else:
            logger.error(f"Unexpected JSON structure in {filepath}")
            return api_names
        
        for url in urls:
            if isinstance(url, dict):
                url = url.get('url', '')
            
            if not url:
                continue
            
            self.stats['total_urls'] += 1
            api_name = extract_api_name_from_url(
                url, self.base_url, self.sub_path, self.config
            )
            
            if api_name:
                api_names.add(api_name)
                self.stats['extracted'] += 1
            else:
                self.stats['failed'] += 1
        
        return api_names
    
    def extract_from_urls(self, urls: List[str]) -> Set[str]:
        """Extract API names from a list of URLs."""
        api_names = set()
        
        for url in urls:
            self.stats['total_urls'] += 1
            api_name = extract_api_name_from_url(
                url, self.base_url, self.sub_path, self.config
            )
            
            if api_name:
                api_names.add(api_name)
                self.stats['extracted'] += 1
            else:
                self.stats['failed'] += 1
        
        return api_names
    
    def get_stats(self) -> Dict:
        """Get extraction statistics."""
        return self.stats.copy()