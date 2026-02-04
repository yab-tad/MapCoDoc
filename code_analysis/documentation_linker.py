# """
# Documentation URL linking with support for complex patterns,
# re-exports, and hierarchical matching.

# This module provides:
# 1. URL pattern parsing and validation
# 2. Documentation URL matching
# 3. Support for re-exported components
# 4. Caching and performance optimization
# """

# import json
# import logging
# import re
# from pathlib import Path
# from dataclasses import dataclass, field
# from typing import Dict, List, Optional, Set, Tuple, Any
# from urllib.parse import urlparse, unquote, urljoin
# import functools
# import hashlib
# from concurrent.futures import ThreadPoolExecutor
# import requests
# from collections import defaultdict

# from .config import AnalysisConfig
# from .module_analyzer import ModuleDefinition
# from .utils import ErrorContext, DocumentationError, safe_read_file, Timer, Cache

# logger = logging.getLogger(__name__)



# @dataclass
# class URLPattern:
#     """
#     URL pattern for documentation matching.
    
#     Attributes:
#         base_url: Base documentation URL
#         sub_path: URL subpath pattern
#     """
#     base_url: str
#     sub_path: Optional[str] = None
    
#     def __post_init__(self):
#         """Validate and normalize pattern."""
#         try:
#             parsed = urlparse(self.base_url)
#             if not parsed.scheme or not parsed.netloc:
#                 raise ValueError("Invalid base URL format")
            
#             # Normalize URLs
#             self.base_url = self.base_url.rstrip('/')
#             if self.sub_path:
#                 self.sub_path = self.sub_path.strip('/')
            
#         except Exception as e:
#             raise DocumentationError(f"Invalid URL pattern: {e}")
    
#     def matches(self, url: str) -> bool:
#         """Check if URL matches this pattern."""
#         if not url.startswith(self.base_url):
#             return False
            
#         if self.sub_path:
#             # Check if sub_path is present after base_url
#             remaining = url[len(self.base_url):].lstrip('/')
#             return remaining.startswith(self.sub_path)
            
#         return True ## maybe return False and put it under an else statement since both base_url and sub_path should be present

    
# @dataclass
# class ModulePathInfo:
#     """
#     Extracted module member path information from URL.
    
#     Attributes:
#         path_before_html: The module path (part before .html)
#         anchor_path: The component name (from anchor or path)
#         full_url: The complete documentation URL
#     """
#     path_before_html: Optional[str]  # Path before .html if exists
#     anchor_path: Optional[str]       # Path in anchor if exists
#     full_url: str                    # Complete URL
    
#     @property
#     def module_member_path(self) -> Optional[str]:
#         """
#         Determine the actual module member path based on available information.
        
#         Returns:
#             Validated module path or None if invalid
#         """
        
#         if not self.path_before_html and not self.anchor_path:
#             return None
            
#         if self.path_before_html and self.anchor_path:
#             # Case 2: Both exist - verify consistency
#             if self.anchor_path.startswith(self.path_before_html):
#                 return self.anchor_path
#             # If anchor provides more specific path that includes path_before_html
#             path_parts = self.path_before_html.split('.')
#             anchor_parts = self.anchor_path.split('.')
#             if all(p in anchor_parts for p in path_parts):
#                 return self.anchor_path
#             return None
            
#         # Case 1: Only path before html
#         if self.path_before_html:
#             return self.path_before_html
            
#         # Case 3: Only anchor path
#         return self.anchor_path


# @dataclass
# class ImportInfo:
#     """
#     Information about imported/exported names.
    
#     Attributes:
#         source_module: Original module
#         imported_as: Name used in importing module
#         is_reexported: Whether name is in __all__
#         package_path: Package-level import path if re-exported
#     """
#     source_module: str
#     imported_as: str
#     is_reexported: bool = False
#     package_path: Optional[str] = None


# class URLCache:
#     """
#     Cache for documentation URLs.
    
#     Handles:
#     - URL lookup caching
#     - Pattern matching results
#     - Module path resolution
#     """
    
#     def __init__(self, max_size: int = 1000):
#         """Initialize cache."""
#         self.max_size = max_size
#         self._path_cache: Dict[str, str] = {}
#         self._pattern_cache: Dict[str, str] = {}
#         self._url_cache: Dict[str, Tuple[str, Optional[str]]] = {}
        
#     def get_url_path(self, key: str) -> Optional[str]:
#         """Get cached URL."""
#         return self._url_cache.get(key)
        
#     def set_url_path(self, key: str, url: str, module_path: Optional[str]):
#         """Cache URL lookup result."""
#         if len(self._url_cache) >= self.max_size:
#             # Simple LRU: clear half when full
#             self._url_cache = dict(sorted(self._url_cache.items())[:self.max_size//2])
            
#         self._url_cache[key] = (url, module_path)
        
#     def clear(self):
#         """Clear all caches."""
#         self._url_cache.clear()
#         self._pattern_cache.clear()
#         self._path_cache.clear()


# class HierarchicalMatcher:
#     """
#     Matcher for hierarchical module paths.
    
#     Features:
#     - Flexible path matching
#     - Support for re-exports
#     - Caching of results
#     """
    
#     def __init__(self):
#         """Initialize matcher."""
#         self.match_cache: Dict[Tuple[str, str], bool] = {}
        
#     def match_paths(self, doc_path: str, code_path: str,
#                    allow_partial: bool = True) -> bool:
#         """
#         Match documentation path to code path.
        
#         Args:
#             doc_path: Documentation path
#             code_path: Code path
#             allow_partial: Allow partial matches
            
#         Returns:
#             Whether paths match
#         """
        
#         cache_key = (doc_path, code_path)
#         if cache_key in self.match_cache:
#             return self.match_cache[cache_key]
            
#         # Normalize paths
#         doc_parts = self._split_path(doc_path)
#         code_parts = self._split_path(code_path)
        
#         # Try exact match first
#         if doc_parts == code_parts:
#             self.match_cache[cache_key] = True
#             return True
            
#         if allow_partial:
#             # Try partial matching
#             result = self._matches_hierarchy(doc_parts, code_parts)
#             self.match_cache[cache_key] = result
#             return result
            
#         return False
        
#     def _split_path(self, path: str) -> List[str]:
#         """Split path into normalized components."""
#         return [p.strip() for p in path.split('.') if p.strip()]
        
#     def _matches_hierarchy(self, doc_parts: List[str],
#                          code_parts: List[str]) -> bool:
#         """Check if paths match hierarchically."""
        
#         if not doc_parts or not code_parts:
#             return False
            
#         # Must share leaf node
#         if doc_parts[-1] != code_parts[-1]:
#             return False
            
#         # we need info about nested structure
#         # # For nested matches, parent must also match
#         # if len(doc_parts) > 2 and len(code_parts) > 2:
#         #     if doc_parts[-2] != code_parts[-2]:
#         #         return False
                
#         # Check remaining hierarchy
#         doc_idx = code_idx = 0
#         while doc_idx < len(doc_parts) and code_idx < len(code_parts):
#             if doc_parts[doc_idx] == code_parts[code_idx]:
#                 doc_idx += 1
#                 code_idx += 1
#             else:
#                 code_idx += 1
                
#         return doc_idx == len(doc_parts)


# class DocumentationLinker:
#     """
#     Documentation URL linking.
    
#     Features:
#     - Intelligent extraction of module and component names
#     - Caching and performance optimization
#     - Re-export handling
#     - Flexible matching strategies
#     - URL validation
#     """
    
#     def __init__(self, url_file: str, pattern_file: str, config: Optional[AnalysisConfig] = None):
#         """Initialize documentation linker."""
        
#         self.config = config or AnalysisConfig()
#         self.urls = self._load_urls(url_file)
#         self.patterns = self._load_patterns(pattern_file)
        
#         self.cache = URLCache()
#         self.matcher = HierarchicalMatcher()
        
#         # Import tracking
#         self.import_paths: Dict[str, ImportInfo] = {}
#         self.package_exports: Dict[str, str] = {}
#         self.module_exports: Dict[str, Set[str]] = {}
        
#         # Pre-process URLs for faster lookup
#         self.processed_urls = self._process_urls()
        
#     def _load_patterns(self, pattern_file: str) -> List[URLPattern]:
#         """Load URL patterns from file."""
#         try:
#             with open(pattern_file, 'r') as f:
#                 data = json.load(f)
                
#             patterns = []
#             # Handle different pattern file formats
#             if "patterns" in data:
#                 # Standard format with patterns list
#                 for pattern_data in data["patterns"]:
#                     base_url = pattern_data.get("base_url")
#                     sub_path = pattern_data.get("sub_path")
                    
#                     if base_url:
#                         patterns.append(URLPattern(
#                             base_url=base_url,
#                             sub_path=sub_path
#                         ))
#             elif "base_url" in data:
#                 # Simple format with single pattern
#                 patterns.append(URLPattern(
#                     base_url=data["base_url"],
#                     sub_path=data.get("sub_path")
#                 ))
                
#             if not patterns:
#                 raise DocumentationError("No valid patterns found in file")
                
#             return patterns
            
#         except Exception as e:
#             raise DocumentationError(f"Failed to load patterns: {e}")
            
#     def _load_urls(self, url_file: str) -> List[str]:
#         """Load URLs from file."""
#         try:
#             with open(url_file, 'r') as f:
#                 return [line.strip() for line in f if line.strip()]
#         except Exception as e:
#             raise DocumentationError(f"Failed to load URLs: {e}")
        
#     def _process_urls(self) -> Dict[str, ModulePathInfo]:
#         """
#         Process URLs to extract path information.
        
#         Returns:
#             Dictionary mapping module paths to URL info
#         """
        
#         processed = {}
        
#         for url in self.urls:
#             try:
#                 # Find matching pattern
#                 pattern = next((p for p in self.patterns if p.matches(url)), None)
#                 if not pattern:
#                     continue
                    
#                 # Extract path information
#                 info = self._extract_path_info(url, pattern)
#                 if info and info.module_member_path:
#                     processed[info.module_member_path] = info
                    
#             except Exception as e:
#                 logger.debug(f"Failed to process URL {url}: {e}")
                
#         return processed
    
#     def _extract_path_info(self, url: str, pattern: URLPattern) -> Optional[ModulePathInfo]:
#         """
#         Extract module and component information from URL.
        
#         Args:
#             url: Documentation URL
#             pattern: Matching URL pattern
            
#         Returns:
#             ModulePathInfo with extracted paths
#         """
             
#         try:
#             # Remove base_url
#             remaining_path = url[len(pattern.base_url):].lstrip('/')
            
#             # Handle different URL patterns
#             if pattern.sub_path and pattern.sub_path + '.html' in remaining_path:
#                 # Case 3: sub_path.html#module_member_path
#                 parts = remaining_path.split('.html')
#                 path_before_html = None
#                 anchor_path = parts[1][1:] if len(parts) > 1 and parts[1].startswith('#') else None
#             else:
#                 if pattern.sub_path:
#                     # Check if sub_path exists in the URL
#                     sub_path_index = remaining_path.find(pattern.sub_path)
#                     if sub_path_index != -1:
#                         # Get everything after sub_path without skipping any characters
#                         module_section = remaining_path[sub_path_index + len(pattern.sub_path):].lstrip('/')
                        
#                         # Split into path and anchor
#                         parts = module_section.split('.html')
#                         path_before_html = parts[0] if parts[0] else None
#                         anchor_path = parts[1][1:] if len(parts) > 1 and parts[1].startswith('#') else None
#                     else:
#                         path_before_html = None
#                         anchor_path = None
#                         # return None
#                 else:
#                     # No sub_path, extract directly from remaining path
#                     parts = remaining_path.split('.html')
#                     path_before_html = parts[0] if parts[0] else None
#                     anchor_path = parts[1][1:] if len(parts) > 1 and parts[1].startswith('#') else None
            
#             return ModulePathInfo(
#                 path_before_html=path_before_html,
#                 anchor_path=anchor_path,
#                 full_url=url
#             )
            
#         except Exception as e:
#             logger.debug(f"Failed to extract path info from {url}: {e}")
#             return None
            
#     def update_from_module_definition(self, module_name: str, module_def: 'ModuleDefinition'):
#         """
#         Update path mappings from ModuleDefinition.
        
#         Args:
#             module_name: Module being analyzed
#             module_def: Module definition with import and export information
#         """
        
#         # Track exports from this module
#         if module_def.all_values:
#             self.module_exports[module_name] = module_def.all_values
                
#         # Process imports and potential re-exports
#         for name, original_path in module_def.imported_names.items():
#             is_reexported = name in module_def.all_values
            
#             # Extract source module from original path
#             source_module = original_path
#             if '.' in original_path:
#                 source_module, imported_name = original_path.rsplit('.', 1)
#                 # Only set source_module if the imported name matches
#                 if imported_name != name:
#                     source_module = original_path
            
#             # Track import path
#             self.import_paths[name] = ImportInfo(
#                 source_module=source_module,
#                 imported_as=name,
#                 is_reexported=is_reexported
#             )
            
#             # Track package exports
#             if is_reexported:
#                 # Track package-level path for re-exports
#                 package = self._get_package_name(module_name)
#                 if package:
#                     self.package_exports[name] = f"{package}.{name}"        
                            
#     def _resolve_import_module(self, module: str, level: int,
#                              current_module: str) -> str:
#         """Resolve relative import to absolute."""
        
#         if level == 0:
#             return module
            
#         parts = current_module.split('.')
#         if len(parts) < level:
#             raise DocumentationError(
#                 f"Invalid relative import in {current_module}"
#             )
            
#         base = '.'.join(parts[:-level])
#         return f"{base}.{module}" if module else base
        
#     def _get_package_name(self, module_name: str) -> Optional[str]:
#         """Get package name from module name."""
#         parts = module_name.split('.')
#         return parts[0] if parts else None
        

#     def _normalize_module_path(self, path: str) -> str:
#         """
#         Normalize module path from documentation URL.
        
#         Handles:
#         - Replacing slashes with dots
#         - Handling common documentation formats
#         - Removing index references
        
#         Args:
#             path: Raw path from URL
            
#         Returns:
#             Normalized module path
#         """
        
#         if not path:
#             return ""
            
#         # Replace slashes with dots
#         path = path.replace('/', '.').replace('\\', '.')
        
#         # Remove common suffixes
#         for suffix in ['_module', '_class', '_function', '.module', '.index']:
#             if path.endswith(suffix):
#                 path = path[:-len(suffix)]
                
#         # Handle paths with index or similar references
#         parts = path.split('.')
#         filtered_parts = []
#         for part in parts:
#             # Skip common non-module parts
#             if part in ['index', 'modules', 'api', 'reference', 
#                     'ipynb_checkpoints', '__pycache__']:
#                 continue
#             # Skip empty parts
#             if not part:
#                 continue
#             # Skip checkpoint file parts
#             if '-checkpoint' in part:
#                 continue
#             filtered_parts.append(part)
            
#         return '.'.join(filtered_parts)
    
    
#     def find_documentation_url(self, 
#                              fully_qualified_name: str,
#                              api_name: Optional[str] = None,
#                              module_all: Optional[Set[str]] = None) -> Tuple[Optional[str], Optional[str]]:
#         """
#         Find matching documentation URL for a component.
        
#         Strategy:
#         1. Use API name as primary if available
#         2. Fallback to fully qualified name if needed
#         3. Try normalized versions of both
        
#         Args:
#             fully_qualified_name: Component's fully qualified name
#             api_name: Optional API name if different from FQN
#             module_all: Optional __all__ from component's module
            
#         Returns:
#             Tuple of (documentation_url, module_member_path) if found
#         """
        
#         # Check cache first
#         cache_key = f"{fully_qualified_name}:{api_name or ''}"
#         cached_result = self.cache.get_url_path(cache_key)
#         if cached_result:
#             return cached_result
            
#         # Prepare name variations to try to match       
#         names_to_try = []
        
#         # Use API name as primary if available
#         if api_name:
#             names_to_try.append(api_name)
#             # Add normalized version
#             normalized_api_name = self._normalize_module_path(api_name)
#             if normalized_api_name and normalized_api_name not in names_to_try:
#                 names_to_try.append(normalized_api_name)
        
#         # Add fully qualified name as backup
#         if fully_qualified_name and fully_qualified_name not in names_to_try:
#             names_to_try.append(fully_qualified_name)
#             # Add normalized version
#             normalized_fqn = self._normalize_module_path(fully_qualified_name)
#             if normalized_fqn and normalized_fqn not in names_to_try:
#                 names_to_try.append(normalized_fqn)
        
#         # Try exact match first and then hierarchical matching (structural understanding)
#         best_hierarchical_match = None
#         for path, info in self.processed_urls.items():
#             for name in names_to_try:
#                 if self.matcher.match_paths(path, name):
#                     # Return first hierarchical match (or could rank them)
#                     self.cache.set_url_path(cache_key, info.full_url, path)
#                     return info.full_url, path
        
#         return None, None
    
        
#     def analyze_coverage(self, components: Dict[str, List[Dict[str, Any]]]) \
#             -> Dict[str, Any]:
#         """
#         Analyze documentation coverage for components.
        
#         Args:
#             components: Dictionary mapping files to their components
            
#         Returns:
#             Coverage statistics and missing documentation info
#         """
        
#         stats = {
#             'total_components': 0,
#             'documented_components': 0,
#             'undocumented_public': 0,
#             'coverage_percentage': 0.0
#         }
        
#         missing_docs = []
        
#         for file_path, file_components in components.items():
#             for component in file_components:
#                 stats['total_components'] += 1
                
#                 if component.get('doc_url'):
#                     stats['documented_components'] += 1
#                 elif component.get('is_public', True):
#                     stats['undocumented_public'] += 1
#                     missing_docs.append({
#                         'name': component['name'],
#                         'fqn': component.get('fully_qualified_name', ''),
#                         'api_name': component.get('API_name', ''),
#                         'file': file_path
#                     })
                    
#         if stats['total_components'] > 0:
#             stats['coverage_percentage'] = (
#                 stats['documented_components'] / stats['total_components'] * 100
#             )
            
#         return {
#             'stats': stats,
#             'missing_documentation': missing_docs
#         }
        
#     def clear_caches(self):
#         """Clear all caches."""
#         self.cache.clear()
#         self.matcher.match_cache.clear()
#         self.import_paths.clear()
#         self.package_exports.clear()
#         self.module_exports.clear()



"""
Documentation URL linking with support for complex patterns,
re-exports, and hierarchical matching.

This module provides:
1. URL pattern parsing and validation
2. Documentation URL matching with direct vs. hierarchical priority
3. Support for re-exported components and export chains
4. Decorator-aware matching to avoid duplicates
5. Weighted conflict resolution for URL candidates
6. Caching and performance optimization
"""


import re
import gc
import json
import time
import logging
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, unquote, urljoin
from typing import Dict, List, Optional, Set, Tuple, Union, Any, DefaultDict

from .config import AnalysisConfig
# from .module_analyzer import ModuleDefinition
from .utils import ErrorContext, DocumentationError, safe_read_file, Timer, Cache, get_component_property


logger = logging.getLogger(__name__)



@dataclass
class URLPattern:
    """
    URL pattern for documentation matching.
    
    Attributes:
        base_url: Base documentation URL
        sub_path: URL subpath pattern
    """
    base_url: str
    sub_path: str
    
    
    def __post_init__(self):
        """Validate and normalize pattern."""
        try:
            parsed = urlparse(self.base_url)
            if not parsed.scheme or not parsed.netloc:
                raise ValueError("Invalid base URL format")
            
            # Normalize URLs
            self.base_url = self.base_url.rstrip('/')
            if self.sub_path:
                self.sub_path = self.sub_path.strip('/')
            
        except Exception as e:
            raise DocumentationError(f"Invalid URL pattern: {e}")
    
    
    def matches(self, url: str) -> bool:
        """Check if URL matches this pattern."""
        if not url.startswith(self.base_url):
            return False
            
        # Check if sub_path is present after base_url
        remaining = url[len(self.base_url):].lstrip('/')
        return remaining.startswith(self.sub_path)


@dataclass
class ModulePathInfo:
    """
    Extracted module member path information from URL.
    
    Attributes:
        path_before_html: The module path (part before .html)
        anchor_path: The component name (from anchor or path)
        full_url: The complete documentation URL
    """
    path_before_html: Optional[str]  # Path before .html if exists
    anchor_path: Optional[str]       # Path in anchor if exists
    full_url: str                    # Complete URL
    
    @property
    def module_member_path(self) -> Optional[str]:
        """
        Determine the actual module member path based on available information.
        
        Returns:
            Validated module path or None if invalid
        """
        
        if not self.path_before_html and not self.anchor_path:
            return None
            
        if self.path_before_html and self.anchor_path:
            # Case 2: Both exist - verify consistency
            if self.anchor_path.startswith(self.path_before_html):
                return self.anchor_path
            # If anchor provides more specific path that includes path_before_html
            path_parts = self.path_before_html.split('.')
            anchor_parts = self.anchor_path.split('.')
            if all(p in anchor_parts for p in path_parts):
                return self.anchor_path
            return None
            
        # Case 1: Only path before html
        if self.path_before_html:
            return self.path_before_html
            
        # Case 3: Only anchor path
        return self.anchor_path



@dataclass
class ImportInfo:
    """
    Information about imported/exported names.
    
    Attributes:
        source_module: Original module
        imported_as: Name used in importing module
        is_reexported: Whether name is in __all__
        package_path: Package-level import path if re-exported
    """
    source_module: str
    imported_as: str
    is_reexported: bool = False
    package_path: Optional[str] = None



@dataclass
class DecoratorAnalysisResult:
    """
    Results of decorator analysis for documentation matching.
    
    Attributes:
        is_property: Whether component is a property
        property_type: Type of property (getter, setter, deleter)
        is_classmethod: Whether component is a classmethod
        is_staticmethod: Whether component is a staticmethod
        is_abstractmethod: Whether component is an abstractmethod
        is_overload: Whether component is an overloaded function
        affects_name: Whether decorators affect the component name
        original_name: Original name before decorator application
        modified_name: Modified name after decorator application
    """
    is_property: bool = False
    property_type: Optional[str] = None
    is_classmethod: bool = False
    is_staticmethod: bool = False
    is_abstractmethod: bool = False
    is_overload: bool = False
    affects_name: bool = False
    original_name: Optional[str] = None
    modified_name: Optional[str] = None



@dataclass
class URLMatchCandidate:
    """
    Candidate for URL matching with scoring information.
    
    Attributes:
        url: Documentation URL
        module_path: Module path in URL
        score: Match score for ranking
        match_type: Type of match (direct or hierarchical)
        metadata: Additional matching metadata
        component_fqn: Fully qualified name of the component being matched
    """
    url: str
    module_path: str
    component_fqn: str
    score: float = 0.0
    match_type: str = "unknown"  # direct, hierarchical, api_path, export_chain
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __lt__(self, other):
        """Compare candidates by score for sorting."""
        return self.score < other.score


class LRUCache:
    """
    LRU (Least Recently Used) cache with size limit.
    
    This cache automatically evicts least recently used items
    when the size limit is reached.
    """
    
    def __init__(self, max_size: int = 1000):
        """
        Initialize LRU cache.
        
        Args:
            max_size: Maximum number of items to store
        """
        self.max_size = max_size
        self.cache = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
    
    def get(self, key: Any) -> Optional[Any]:
        """
        Get item from cache.
        
        Args:
            key: Cache key
            
        Returns:
            Cached value or None if not found
        """
        if key in self.cache:
            # Move to end (most recently used)
            value = self.cache.pop(key)
            self.cache[key] = value
            self.hits += 1
            return value
        
        self.misses += 1
        return None
    
    def set(self, key: Any, value: Any) -> None:
        """
        Set item in cache.
        
        Args:
            key: Cache key
            value: Value to cache
        """
        # If key exists, remove it first
        if key in self.cache:
            self.cache.pop(key)
            
        # Add to cache
        self.cache[key] = value
        
        # Evict oldest item if over size limit
        if len(self.cache) > self.max_size:
            self.cache.popitem(last=False)  # Remove first item (oldest)
            self.evictions += 1
    
    def clear(self) -> None:
        """Clear cache."""
        self.cache.clear()
        
    def get_stats(self) -> Dict[str, int]:
        """Get cache statistics."""
        return {
            'size': len(self.cache),
            'max_size': self.max_size,
            'hits': self.hits,
            'misses': self.misses,
            'evictions': self.evictions
        }


class URLMatchRegistry:
    """
    Registry for tracking URL matches to components.
    
    This registry ensures that URLs are not matched to multiple
    components inappropriately by tracking match types and priorities.
    """
    
    def __init__(self, max_entries: int = 10000):
        """
        Initialize registry.
        
        Args:
            max_entries: Maximum number of entries to track
        """
        self.max_entries = max_entries
        self.direct_matches: Dict[str, Set[str]] = defaultdict(set)  # url -> set of component FQNs
        self.hierarchical_matches: Dict[str, Set[str]] = defaultdict(set)  # url -> set of component FQNs
        self.conflict_resolutions: List[Dict[str, Any]] = []  # Log of conflict resolutions
        self.api_boundaries: List[str] = []  # API boundaries for scoring
        
        # Statistics
        self.stats = {
            'direct_match_count': 0,
            'hierarchical_match_count': 0,
            'conflicts': 0,
            'resolutions': 0,
            'true_conflicts': 0  # Conflicts that couldn't be fully resolved
        }
        
        # Transaction support
        self._transaction = None  # Current active transaction
    
    
    def set_api_boundaries(self, boundaries: List[str]) -> None:
        """Set API boundaries for conflict resolution."""
        self.api_boundaries = boundaries
    
    def register_match(self, url: str, component_fqn: str, match_type: str) -> bool:
        """
        Register a URL match to a component.
        
        Args:
            url: Documentation URL
            component_fqn: Fully qualified component name
            match_type: Match type (direct or hierarchical)
            
        Returns:
            True if match is allowed, False if blocked
        """
        
        logger.debug(f"Registering match: {url} -> {component_fqn} ({match_type})")
        
        # Check if this would create a conflict
        if match_type == 'direct':
            # Direct matches can override hierarchical matches
            if url in self.hierarchical_matches:
                
                hierarchical_matches = list(self.hierarchical_matches[url])
                logger.info(f"URL conflict: direct match {component_fqn} overrides hierarchical matches: {hierarchical_matches}")
                
                self.conflict_resolutions.append({
                    'url': url,
                    'direct_match': component_fqn,
                    'hierarchical_matches': hierarchical_matches,
                    'resolution': 'direct_override',
                    'timestamp': time.time()
                })
                self.stats['conflicts'] += 1
                self.stats['resolutions'] += 1
                
                # Always remove hierarchical matches when direct match is found
                self.hierarchical_matches.pop(url)
                
            # Check if URL already directly matched to a different component
            if url in self.direct_matches and component_fqn not in self.direct_matches[url]:
                # This is a serious conflict that needs resolution
                existing_matches = list(self.direct_matches[url])
                
                logger.warning(f"URL direct match conflict: {url} matches both {component_fqn} and {existing_matches}")

                # Extract module and component parts for both paths
                new_module, new_name = component_fqn.rsplit('.', 1) if '.' in component_fqn else ('', component_fqn)
                existing_module, existing_name = existing_matches[0].rsplit('.', 1) if '.' in existing_matches[0] else ('', existing_matches[0])
                
                # Calculate canonical scores (lower is better)
                new_score = self._calculate_canonical_score(new_module, new_name)
                existing_score = self._calculate_canonical_score(existing_module, existing_name)
                
                # CHANGE: Check for overloaded function scenario
                is_overload_conflict = new_name == existing_name and new_module == existing_module
                
                # Log detailed conflict information for diagnostics
                self.conflict_resolutions.append({
                    'url': url,
                    'new_match': component_fqn,
                    'new_score': new_score,
                    'existing_match': existing_matches[0],
                    'existing_score': existing_score,
                    'is_overload_conflict': is_overload_conflict,
                    'resolution': 'canonical_scoring',
                    'timestamp': time.time()
                })
                
                self.stats['conflicts'] += 1
                
                # Handling for overloaded functions
                if is_overload_conflict:
                    # For overloaded functions, add both to match set
                    self.direct_matches[url].add(component_fqn)
                    self.stats['true_conflicts'] += 1
                    logger.info(f"Overload conflict: Both {component_fqn} and {existing_matches[0]} match {url}")
                    return True
                
                # Resolve based on canonical score
                if new_score < existing_score:
                    # New match is more canonical - replace existing
                    self.direct_matches[url] = {component_fqn}
                    self.stats['resolutions'] += 1
                    logger.debug(f"URL conflict resolved: {component_fqn} preferred over {existing_matches[0]}")
                    return True
                elif new_score > existing_score:
                    # Existing match is more canonical - reject new
                    self.stats['resolutions'] += 1
                    logger.debug(f"URL conflict resolved: keeping {existing_matches[0]} over {component_fqn}")
                    return False
                else:
                    # Equal scores - keep both but flag as true conflict
                    self.direct_matches[url].add(component_fqn)
                    self.stats['true_conflicts'] += 1
                    logger.warning(f"True URL conflict: both {component_fqn} and {existing_matches[0]} match {url}")
                    return True
            else:
                # New direct match
                if url not in self.direct_matches:
                    self.direct_matches[url] = set()
                self.direct_matches[url].add(component_fqn)
                self.stats['direct_match_count'] += 1
            
            return True
            
        elif match_type == 'hierarchical':
            # Hierarchical matches are not allowed for URLs already directly matched
            if url in self.direct_matches:
                direct_matches = list(self.direct_matches[url])
                logger.debug(f"Blocked hierarchical match {component_fqn} for URL {url} (direct matches: {direct_matches})")
                
                self.conflict_resolutions.append({
                    'url': url,
                    'hierarchical_match': component_fqn,
                    'direct_matches': direct_matches,
                    'resolution': 'blocked_hierarchical',
                    'timestamp': time.time()
                })
                self.stats['conflicts'] += 1
                self.stats['url_reuse_blocks'] = self.stats.get('url_reuse_blocks', 0) + 1
                return False
            
            # Allow hierarchical match
            if url not in self.hierarchical_matches:
                self.hierarchical_matches[url] = set()
            self.hierarchical_matches[url].add(component_fqn)
            self.stats['hierarchical_match_count'] += 1
            return True
        
        # Unknown match type
        logger.warning(f"Unknown match type: {match_type}")
        return False
    
    
    def _calculate_canonical_score(self, module_path: str, name: str) -> float:
        """
        Calculate how canonical a path is (lower is better).
        
        This scoring mechanism helps to determine which of two competing paths should be considered more "canonical" for documentation purposes.
        
        Args:
            module_path: Module path to score
            name: Component name
            
        Returns:
            Score (lower is more canonical)
        """
        
        score = 0.0
        
        # Shorter paths are generally more canonical
        path_length = len(module_path.split('.'))
        score += path_length * 10
        
        # Stronger penalties for implementation paths
        impl_terms = ['internal', 'impl', '_impl', 'detail', '_detail', 'backend', '_utils', 'compat', '_compat', 'modules', 'private']
        impl_penalty = 0
        for term in impl_terms:
            if term in module_path.lower():
                impl_penalty -= 20  # Increased penalty
                break
        score += impl_penalty  # Apply penalty
        
        # Component name factors - prefer "clean" names
        if name.startswith('_') and not (name.startswith('__') and name.endswith('__')):
            score += 30  # Protected/private names are less canonical
        
        # API boundaries are much more canonical
        if hasattr(self, 'api_boundaries') and self.api_boundaries:
            if module_path in self.api_boundaries:
                score -= 50  # Large bonus for direct API boundary match
            else:
                # Also check for parent matches (being in an API boundary package)
                for boundary in self.api_boundaries:
                    if module_path.startswith(boundary + '.'):
                        score -= 25  # Smaller bonus for being inside an API boundary
                        break
        
        # Special cases for package structure
        module_parts = module_path.split('.')
        if len(module_parts) >= 2:
            # Package root is likely more canonical
            if len(module_parts) == 1:
                score -= 20
                
            # __init__ modules at API boundaries are highly canonical
            if module_parts[-1] == '__init__' and module_path in getattr(self, 'api_boundaries', []):
                score -= 40
            
            # Direct subpackage of root package (common API location)
            if len(module_parts) == 2:
                score -= 15
        
        logger.debug(f"Canonical score for {module_path}.{name}: {score} (length: {path_length}, impl: {impl_penalty})")
        
        return score
    
    
    def check_match_allowed(self, url: str, match_type: str) -> bool:
        """
        Check if a match would be allowed without registering it.
        
        Args:
            url: Documentation URL
            match_type: Match type (direct or hierarchical)
            
        Returns:
            True if match would be allowed
        """
        
        if match_type == 'direct':
            # Direct matches are always allowed
            return True
        
        if match_type == 'hierarchical':
            # Hierarchical matches are not allowed for URLs already directly matched
            return url not in self.direct_matches
        
        return False
    
    
    def clear(self) -> None:
        """Clear registry."""
        self.direct_matches.clear()
        self.hierarchical_matches.clear()
        self.conflict_resolutions.clear()
        
        # Reset statistics
        self.stats = {
            'direct_match_count': 0,
            'hierarchical_match_count': 0,
            'conflicts': 0,
            'resolutions': 0,
            'true_conflicts': 0
        }
    
    
    def get_stats(self) -> Dict[str, Any]:
        """Get registry statistics."""
        return {
            'direct_matches': len(self.direct_matches),
            'hierarchical_matches': len(self.hierarchical_matches),
            'conflict_resolutions': len(self.conflict_resolutions),
            'stats': self.stats
        }

    def get_conflicts(self) -> List[Dict[str, Any]]:
        """Get all conflict resolutions for debugging."""
        return self.conflict_resolutions
    
    def begin_transaction(self):
        """
        Begin a new batch transaction with cleanup of any existing transaction.
        
        This ensures that any existing transaction is properly rolled back before starting a new one, preventing resource leaks.
        """
        if self._transaction:
            logger.warning("Transaction already in progress, aborting previous transaction")
            self.rollback_transaction()
            
        self._transaction = {
            'direct': defaultdict(set),  # url -> set of component FQNs
            'hierarchical': defaultdict(set),  # url -> set of component FQNs
            'conflicts': []  # Conflicts detected during transaction
        }
        logger.debug("URL match transaction started")
    
    
    def register_match_in_transaction(self, url: str, component_fqn: str, match_type: str) -> bool:
        """
        Register a match within the current transaction.
        
        Args:
            url: Documentation URL
            component_fqn: Fully qualified component name
            match_type: Match type (direct or hierarchical)
            
        Returns:
            True if match is allowed, False if rejected
        """
        
        if not self._transaction:
            # No active transaction, use regular registration
            return self.register_match(url, component_fqn, match_type)
            
        logger.debug(f"Adding match to transaction: {url} -> {component_fqn} ({match_type})")
        
        # Simulate conflict detection but store in transaction
        if match_type == 'direct':
            # Check for conflict with existing transaction matches
            if url in self._transaction['direct'] and component_fqn not in self._transaction['direct'][url]:
                # This is a potential conflict within the transaction
                existing = list(self._transaction['direct'][url])
                
                # Record conflict for later resolution
                self._transaction['conflicts'].append({
                    'url': url,
                    'components': [component_fqn] + existing,
                    'match_type': match_type,
                    'conflict_type': 'direct_vs_direct'
                })
                logger.debug(f"Transaction conflict: {url} -> {component_fqn} vs {existing}")
            
            # Add to transaction's direct matches
            self._transaction['direct'][url].add(component_fqn)
            
            # Check for conflict with hierarchical matches
            if url in self._transaction['hierarchical']:
                # Record potential override
                self._transaction['conflicts'].append({
                    'url': url,
                    'direct': component_fqn,
                    'hierarchical': list(self._transaction['hierarchical'][url]),
                    'conflict_type': 'direct_vs_hierarchical'
                })
                
            return True
            
        elif match_type == 'hierarchical':
            # Check if URL already has direct matches in transaction
            if url in self._transaction['direct']:
                # Reject hierarchical match - direct matches take precedence
                logger.debug(f"Transaction rejected hierarchical match: {url} -> {component_fqn}")
                return False
                
            # Add to transaction's hierarchical matches
            self._transaction['hierarchical'][url].add(component_fqn)
            return True
            
        return False
    
    
    def register_alternate_path_in_transaction(self, url: str, path: str, match_type: str) -> bool:
        """
        Register an alternate path for a documentation URL within a transaction.
        
        Args:
            url: Documentation URL
            path: Alternate component path
            match_type: Type of match (direct, hierarchical)
            
        Returns:
            True if registration was successful
        """
        if not hasattr(self, '_transaction') or not self._transaction:
            logger.warning("No active transaction for alternate path registration")
            return False
        
        # Check if we already have this path registered
        if path in self._transaction.get('path_matches', {}):
            return False
        
        # Add to transaction
        if 'alternate_paths' not in self._transaction:
            self._transaction['alternate_paths'] = {}
        
        # Group by URL
        if url not in self._transaction['alternate_paths']:
            self._transaction['alternate_paths'][url] = []
        
        # Add alternate path
        self._transaction['alternate_paths'][url].append({
            'path': path,
            'match_type': match_type,
            'timestamp': time.time()
        })
        
        return True
    
    
    def commit_transaction(self) -> bool:
        """
        Commit all matches in the transaction.
        
        This method resolves conflicts and atomically applies all matches.
        
        Returns:
            True if transaction committed successfully
        """
        
        if not self._transaction:
            logger.warning("No active transaction to commit")
            return False
            
        logger.debug(f"Committing URL match transaction with {len(self._transaction['direct'])} direct and {len(self._transaction['hierarchical'])} hierarchical matches")
        
        # First resolve direct vs direct conflicts within transaction
        direct_conflicts = [c for c in self._transaction['conflicts'] if c['conflict_type'] == 'direct_vs_direct']
        resolved_urls = set()
        
        for conflict in direct_conflicts:
            url = conflict['url']
            components = conflict['components']
            
            # Simple resolution - use existing algorithm
            resolved = self._resolve_direct_conflict(url, components)
            resolved_urls.add(url)
            
            # Update transaction with resolution
            self._transaction['direct'][url] = set(resolved['components'])
            self.conflict_resolutions.append(resolved['info'])
        
        # Now apply all direct matches
        for url, components in self._transaction['direct'].items():
            for component in components:
                # Add to main registry
                if url not in self.direct_matches:
                    self.direct_matches[url] = set()
                self.direct_matches[url].add(component)
                self.stats['direct_match_count'] += 1
        
        # Process hierarchical matches that don't conflict with direct matches
        for url, components in self._transaction['hierarchical'].items():
            # Skip if URL now has direct matches
            if url in self.direct_matches:
                continue
                
            for component in components:
                if url not in self.hierarchical_matches:
                    self.hierarchical_matches[url] = set()
                self.hierarchical_matches[url].add(component)
                self.stats['hierarchical_match_count'] += 1
        
         # Process alternate paths
        for url, alternates in self._transaction.get('alternate_paths', {}).items():
            for alt in alternates:
                # Register as a secondary path
                if not hasattr(self, 'alternate_paths'):
                    self.alternate_paths = {}
                
                if url not in self.alternate_paths:
                    self.alternate_paths[url] = []
                    
                self.alternate_paths[url].append({
                    'path': alt['path'],
                    'match_type': alt['match_type'],
                    'timestamp': alt['timestamp']
                })
        
        # Update statistics
        self.stats['conflicts'] += len(self._transaction['conflicts'])
        self.stats['resolutions'] += len(resolved_urls)
        
        # Clear transaction
        self._transaction = None
        
        logger.debug("URL match transaction committed successfully")
        return True
    
    
    def rollback_transaction(self):
        """Discard the current transaction."""
        if not self._transaction:
            logger.warning("No active transaction to roll back")
            return
            
        # Clear all temporary structures
        self._transaction = None
        logger.debug("Transaction rolled back successfully")
    
    
    def _resolve_direct_conflict(self, url: str, components: List[str]) -> Dict[str, Any]:
        """
        Resolve a direct match conflict.
        
        Args:
            url: The disputed URL
            components: List of conflicting component FQNs
            
        Returns:
            Dictionary with resolved components and conflict info
        """
        
        # Extract scoring information from components
        component_scores = []
        for component_fqn in components:
            # Calculate canonical score
            if '.' in component_fqn:
                module_path, name = component_fqn.rsplit('.', 1)
                score = self._calculate_canonical_score(module_path, name)
            else:
                score = 100  # Arbitrary high score for direct components
                
            component_scores.append((score, component_fqn))
        
        # Sort by score (lower is better)
        component_scores.sort()
        
        # Check for tie at lowest score
        best_score = component_scores[0][0]
        best_components = [comp for score, comp in component_scores if score == best_score]
        
        # Resolution details
        resolution_info = {
            'url': url,
            'candidates': [comp for _, comp in component_scores],
            'scores': {comp: score for score, comp in component_scores},
            'resolution': 'canonical_scoring',
            'timestamp': time.time()
        }
        
        if len(best_components) > 1:
            # Tie detected - check names for overloaded methods
            names = [comp.split('.')[-1] if '.' in comp else comp for comp in best_components]
            if len(set(names)) == 1:
                # Same name - likely overloads
                resolution_info['resolution'] = 'overload_tie'
                self.stats['true_conflicts'] += 1
                return {
                    'components': best_components,
                    'info': resolution_info
                }
            else:
                # Tie but different names - use API boundary information
                api_boundary_components = [
                    comp for comp in best_components 
                    if any(comp.startswith(b) for b in self.api_boundaries)
                ]
                
                if api_boundary_components:
                    resolution_info['resolution'] = 'api_boundary_tiebreak'
                    return {
                        'components': api_boundary_components,
                        'info': resolution_info
                    }
        
        # No tie or resolved tie
        return {
            'components': best_components,
            'info': resolution_info
        }
    


class URLCache:
    """
    Cache for documentation URLs.
    
    Handles:
    - URL lookup caching
    - Pattern matching results
    - Module path resolution
    """
    
    def __init__(self, max_size: int = 1000):
        """Initialize cache."""
        self.max_size = max_size
        self.path_cache = LRUCache(max_size)
        self.pattern_cache = LRUCache(max_size)
        self.url_cache = LRUCache(max_size)
    
    
    def get_url_path(self, key: str) -> Optional[Tuple[str, Optional[str]]]:
        """Get cached URL."""
        return self.url_cache.get(key)
    
     
    def set_url_path(self, key: str, url: str, module_path: Optional[str]):
        """Cache URL lookup result."""
        self.url_cache.set(key, (url, module_path))
        
    def clear(self):
        """Clear all caches."""
        self.url_cache.clear()
        self.pattern_cache.clear()
        self.path_cache.clear()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return {
            'url_cache': self.url_cache.get_stats(),
            'pattern_cache': self.pattern_cache.get_stats(),
            'path_cache': self.path_cache.get_stats()
        }



class DecoratorAnalyzer:
    """
    Analyzes decorators to guide documentation matching.
    
    This class extracts semantic information from decorators to facilitate better URL matching decisions.
    It also integrates parameter information to handle overloaded functions/methods.
    """
    
    def __init__(self):
        # Registry of known decorators and their effects
        self.decorator_registry = {
            "overload": {"doc_priority": "overload", "is_overload": True},
            "property": {"doc_priority": "property", "is_property": True},
            "staticmethod": {"doc_priority": "method_type", "is_static": True},
            "classmethod": {"doc_priority": "method_type", "is_class": True},
            "abstractmethod": {"doc_priority": "abstract", "is_abstract": True},
            "deprecated": {"doc_priority": "deprecated", "is_deprecated": True},
        }
    
    
    def analyze(self, component: Dict[str, Any]) -> DecoratorAnalysisResult:
        """
        Analyze component decorators with improved error handling.
        
        Handles missing or malformed decorator information to prevent the 'decorators' variable access error.
        
        Args:
            component: Component dictionary with decorators and signature
            
        Returns:
            Decorator analysis result
        """
        
        result = DecoratorAnalysisResult()
        
        # Extract decorator information with robust type safety
        decorators = []
        
        if 'decorators' in component:
            if isinstance(component['decorators'], list):
                decorators = component['decorators']
            elif isinstance(component['decorators'], dict):
                # Handle case where decorators is a single dict instead of list
                decorators = [component['decorators']]
            # If it's any other type, leave decorators as empty list
        
        # Process known decorators with type safety
        for decorator in decorators:
            # Skip if not a dictionary
            if not isinstance(decorator, dict):
                continue
            
            name = decorator.get('name', '')
            if name in self.decorator_registry:
                for key, value in self.decorator_registry[name].items():
                    setattr(result, key, value)
                        
        # Extract parameter information if available
        parameters = []
        signature = component.get('signature', {})
        
        # Try different formats for getting parameters
        if isinstance(signature, dict):
            if 'parameters' in signature:
                params = signature.get('parameters', [])
                if isinstance(params, list):
                    parameters = params
            # Also check component directly
            elif 'parameters' in component and isinstance(component['parameters'], list):
                parameters = component['parameters']
        
        # Enhance overload analysis using parameter information
        if result.is_overload and parameters:
            self._enhance_overload_analysis(result, signature, parameters)
        
        return result
    
    
    def _enhance_overload_analysis(self, result, signature, parameters):
        """
        Enhance overload analysis using parameter information.
        
        Args:
            result: Decorator analysis result to update
            signature: Signature dictionary
            parameters: List of parameter dictionaries
        """
        # Use signature specificity score if available
        if isinstance(signature, dict) and 'specificity_score' in signature:
            specificity = signature.get('specificity_score', 50)
            overload_category = signature.get('overload_category', 'secondary')
        else:
            # Calculate simple specificity
            specificity = self._calculate_simple_specificity(parameters)
            overload_category = self._determine_overload_category(parameters)
        
        # Store results
        result.specificity_score = specificity
        result.overload_category = overload_category
        
        # Determine documentation priority based on overload category
        if overload_category == 'primary':
            result.doc_priority = 'primary'
        elif overload_category == 'secondary':
            result.doc_priority = 'secondary'
        else:
            result.doc_priority = 'specific'
    
    
    def _calculate_simple_specificity(self, parameters):
        """
        Calculate simple parameter specificity when full score not available.
        
        Args:
            parameters: List of parameter dictionaries
            
        Returns:
            Specificity score (lower = more general)
        """
        score = 0
        
        for param in parameters:
            param_type = param.get('type')
            
            # Generality scoring
            if not param_type:
                score += 0  # No type = most general
            elif 'Union' in param_type or 'Optional' in param_type:
                score += 5  # Union types = moderately general
            elif param_type in ['Any', 'object', 'typing.Any']:
                score += 2  # Any/object types = quite general
            else:
                score += 10  # Concrete types = most specific
                
            # Default values make parameters more general
            if param.get('default'):
                score -= 3
                
            # Variadic parameters are very general
            if param.get('is_vararg') or param.get('is_kwarg'):
                score -= 15
        
        return score
    
    
    def _determine_overload_category(self, parameters):
        """
        Determine overload category from parameters.
        
        Args:
            parameters: List of parameter dictionaries
            
        Returns:
            Category string: 'primary', 'secondary', or 'specific'
        """
        # Check for patterns indicating a primary implementation
        has_vararg = any(p.get('is_vararg') for p in parameters)
        has_kwarg = any(p.get('is_kwarg') for p in parameters)
        
        if has_vararg and has_kwarg:
            return 'primary'
            
        # Check for patterns indicating a specific implementation
        concrete_type_count = sum(1 for p in parameters 
                                if p.get('type') and not ('Union' in p.get('type', '') or 
                                                        'Any' in p.get('type', '')))
        if concrete_type_count == len(parameters) and len(parameters) >= 2:
            return 'specific'
            
        # Default
        return 'secondary'



class HierarchicalMatcher:
    """
    Matcher for hierarchical module member path.
    
    Features:
    - Flexible path matching
    - Support for re-exports
    - Caching of results
    """
    
    def __init__(self, max_cache_size: int = 10000):
        """Initialize matcher."""
        self.match_cache = LRUCache(max_cache_size) #self.match_cach: Dict[Tuple[str, str], bool] = {}
    
    
    def match_paths(self, doc_path: str, code_path: str,
                   allow_partial: bool = True) -> bool:
        """
        Match documentation path to code path.
        
        Args:
            doc_path: Documentation path
            code_path: Code path
            allow_partial: Allow partial matches
            
        Returns:
            Whether paths match
        """
        
        cache_key = (doc_path, code_path)
        cached_result = self.match_cache.get(cache_key)
        if cached_result is not None:
            return cached_result
            
        # Normalize paths
        doc_parts = self._split_path(doc_path)
        code_parts = self._split_path(code_path)
            
        if allow_partial:
            # Try partial matching
            result = self._matches_hierarchy(doc_parts, code_parts)
            self.match_cache.set(cache_key, result)
            return result
            
        return False
    
    
    def _split_path(self, path: str) -> List[str]:
        """Split path into normalized components."""
        return [p.strip() for p in path.split('.') if p.strip()]
    
    
    def _matches_hierarchy(self, doc_parts: List[str], code_parts: List[str]) -> bool:
        """Check if paths match hierarchically."""
        
        if not doc_parts or not code_parts:
            return False
            
        # Must share leaf node
        if doc_parts[-1] != code_parts[-1]:
            return False
                
        # Check remaining hierarchy
        doc_idx = code_idx = 0
        while doc_idx < len(doc_parts) and code_idx < len(code_parts):
            if doc_parts[doc_idx] == code_parts[code_idx]:
                doc_idx += 1
                code_idx += 1
            else:
                code_idx += 1
                
        return doc_idx == len(doc_parts)
    
    
    def clear(self) -> None:
        """Clear match cache."""
        self.match_cache.clear()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get matcher statistics."""
        return {
            'cache': self.match_cache.get_stats()
        }



class URLConflictResolver:
    """
    Resolves conflicts between multiple URL match candidates.
    
    This class implements a weighted scoring system to select the best URL match when multiple candidates are available.
    """
    
    def __init__(self, scoring_weights: Optional[Dict[str, float]] = None):
        """
        Initialize resolver with configurable scoring weights.
        
        Args:
            scoring_weights: Optional dictionary of scoring weights to override defaults
        """
        
        # Default scoring weights
        self.match_type_scores = {
            "direct": 100.0,             # Direct matches are preferred
            "api_path": 90.0,            # Matches to API path are good
            "export_chain": 80.0,        # Matches from export chain are good
            "hierarchical": 70.0,        # Hierarchical matches are acceptable
            "implementation_path": 60.0  # Implementation path matches are least preferred
        }
        
        # Path type weights
        self.path_type_scores = {
            "api_boundary": 30.0,           # API boundaries are important locations
            "init_module": 10.0,            # __init__ modules often define public APIs
            "short_path": 5.0,              # Shorter paths tend to be more public
            "implementation_detail": -15.0  # Implementation details should be avoided
        }
        
        # Decorator weights
        self.decorator_scores = {
            "non_overloaded": 50.0,      # Non-overloaded functions are better targets
            "property_match": 15.0,      # Properly matched properties are good
            "method_type_match": 12.0    # Matched method types (static, class) are good
        }
        
        # Override with custom weights if provided
        if scoring_weights:
            for category, weights in scoring_weights.items():
                if category == 'match_type':
                    self.match_type_scores.update(weights)
                elif category == 'path_type':
                    self.path_type_scores.update(weights)
                elif category == 'decorator':
                    self.decorator_scores.update(weights)
        
        # Statistics
        self.resolved_conflicts = 0
        self.resolution_details = []
        
        # Store API boundaries for scoring
        self.api_boundaries = []
    
    
    def set_api_boundaries(self, boundaries: List[str]) -> None:
        """Set API boundaries for conflict resolution."""
        self.api_boundaries = boundaries
    
    def resolve(self, candidates: List[URLMatchCandidate],
               fully_qualified_name: str,
               api_name: Optional[str] = None,
               decorator_analysis: Optional[DecoratorAnalysisResult] = None) -> Optional[URLMatchCandidate]:
        """
        Resolve conflicts between multiple candidates.
        
        Args:
            candidates: List of URL match candidates
            fully_qualified_name: Component's fully qualified name
            api_name: Optional API name
            decorator_analysis: Optional decorator analysis results
            
        Returns:
            Best candidate or None if no suitable candidate
        """
        
        if not candidates:
            return None
            
        if len(candidates) == 1:
            return candidates[0]
            
        # Calculate scores for all candidates
        for candidate in candidates:
            self._calculate_score(
                candidate=candidate, 
                fully_qualified_name=fully_qualified_name, 
                api_name=api_name,
                decorator_analysis=decorator_analysis
            )
            
        # Sort by score (descending)
        sorted_candidates = sorted(candidates, reverse=True)
        
        # Check for ties at the top score
        top_score = sorted_candidates[0].score
        top_candidates = [c for c in sorted_candidates if c.score == top_score]
        
        # If we have multiple top candidates, use tie-breaking logic
        if len(top_candidates) > 1:
            # Prefer direct matches
            direct_matches = [c for c in top_candidates if c.match_type == "direct"]
            if direct_matches:
                top_candidates = direct_matches
                
            # If still tied, prefer API path matches
            if len(top_candidates) > 1:
                api_matches = [c for c in top_candidates if c.match_type == "api_path"]
                if api_matches:
                    top_candidates = api_matches
                    
            # If still tied, prefer matches with explicit boundary markers
            if len(top_candidates) > 1:
                boundary_matches = [c for c in top_candidates if c.metadata.get('is_package_boundary', False)]
                if boundary_matches:
                    top_candidates = boundary_matches
                    
            # If still tied, prefer the shortest path (usually more public)
            if len(top_candidates) > 1:
                top_candidates.sort(key=lambda c: len(c.module_path.split('.')))
        
        # Record resolution for diagnostics
        resolution_detail = {
            'candidates': len(candidates),
            'winner': top_candidates[0].url,
            'winner_fqn': top_candidates[0].component_fqn,
            'winner_module_path': top_candidates[0].module_path,
            'winner_match_type': top_candidates[0].match_type,
            'winner_score': top_candidates[0].score,
            'other_candidates': [
                {
                    'url': c.url,
                    'module_path': c.module_path,
                    'match_type': c.match_type,
                    'score': c.score
                } for c in sorted_candidates[1:3]  # Include next 2 candidates
            ] if len(sorted_candidates) > 1 else []
        }
        self.resolution_details.append(resolution_detail)
        self.resolved_conflicts += 1
        
        # Return highest scoring candidate
        return top_candidates[0]
    
    
    def _calculate_score(self, candidate: URLMatchCandidate,
                      fully_qualified_name: str,
                      api_name: Optional[str] = None,
                      decorator_analysis: Optional[DecoratorAnalysisResult] = None) -> None:
        """
        Calculate score for a candidate based on multiple factors.
        
        Args:
            candidate: Candidate to score
            fully_qualified_name: Component's fully qualified name
            api_name: Optional API name
            decorator_analysis: Optional decorator analysis results
        """
        
        score = 0.0
        
        # Safely get match_type
        match_type = getattr(candidate, 'match_type', 'unknown')
        
        # CHANGE: Increase weight for direct matches
        match_type_scores = {
            "direct": 120.0,             # Direct matches are strongly preferred
            "api_path": 100.0,           # Matches to API path are very good
            "export_chain": 85.0,        # Matches from export chain are good
            "hierarchical": 70.0,        # Hierarchical matches are acceptable
            "implementation_path": 60.0  # Implementation path matches are least preferred
        }
        
        # Base score by match type
        match_type_score = self.match_type_scores.get(match_type, 50.0)
        score += match_type_score
        
        # Extract metadata
        metadata = getattr(candidate, 'metadata', {}) or {}
        
        # Higher boost for API matches
        # Check path similarity with API name or fully qualified name
        if api_name and candidate.module_path == api_name:
            score += 75.0  # Increased from 50.0
        elif candidate.module_path == fully_qualified_name:
            score += 50.0  # Increased from 40.0
        
        # Check for partial matches with API name
        if api_name:
            if api_name.endswith(candidate.module_path):
                score += 25.0  # Slight increase
            elif candidate.module_path.endswith(api_name):
                score += 20.0  # Slight increase
        
        # Better API boundary handling
        # Check if path comes from an API boundary
        module_part = candidate.module_path.rsplit('.', 1)[0] if '.' in candidate.module_path else candidate.module_path
        if hasattr(self, 'api_boundaries') and self.api_boundaries:
            is_boundary = False
            # Direct boundary match
            if module_part in self.api_boundaries:
                is_boundary = True
                score += 40.0  # Increased for direct boundary match
            else:
                # Check for parent/child boundary relationships
                for boundary in self.api_boundaries:
                    # Child of boundary
                    if module_part.startswith(boundary + '.'):
                        is_boundary = True
                        score += 30.0  # Bonus for being inside a boundary
                        break
                    # Parent of boundary - less likely but possible
                    elif boundary.startswith(module_part + '.'):
                        is_boundary = True
                        score += 20.0
                        break
            
            # Add to metadata
            if is_boundary and isinstance(metadata, dict):
                metadata['is_api_boundary'] = True
        else:
            # Fallback to metadata
            is_boundary = metadata.get('is_api_boundary', False)
            if is_boundary:
                score += 30.0
            
        # Decorator-specific adjustments
        if decorator_analysis:
            # Significant boost for non-overloaded functions
            if not getattr(decorator_analysis, 'is_overload', False):
                score += 75.0  # Strongly prefer non-overloaded functions
            else:
                # Handle overloaded functions with more nuance
                overload_category = getattr(decorator_analysis, 'overload_category', 'secondary')
                
                # Primary overloads should be preferred over secondary/specific ones
                if overload_category == 'primary':
                    score += 40.0
                elif overload_category == 'secondary':
                    score += 20.0
                else:  # 'specific'
                    score += 10.0
                    
                # Check if URL specifically mentions overloads
                if 'overload' in candidate.url.lower():
                    score += 25.0  # Boost for URLs with overload mentions
            
            # Property handling
            is_property = getattr(decorator_analysis, 'is_property', False)
            if is_property:
                # Big boost for property matches in URL
                if "property" in candidate.url.lower():
                    score += 30.0
                    
                # Match property type with extra boost
                property_type = getattr(decorator_analysis, 'property_type', None)
                if property_type and property_type in candidate.url.lower():
                    score += 25.0
            
            # Method type matching
            is_staticmethod = getattr(decorator_analysis, 'is_staticmethod', False)
            is_classmethod = getattr(decorator_analysis, 'is_classmethod', False)
            is_abstractmethod = getattr(decorator_analysis, 'is_abstractmethod', False)
            
            if is_staticmethod and "staticmethod" in candidate.url.lower():
                score += 30.0
            if is_classmethod and "classmethod" in candidate.url.lower():
                score += 30.0
            if is_abstractmethod and "abstract" in candidate.url.lower():
                score += 30.0
        
        # Leverage export chain info 
        chain_length = metadata.get('export_chain_length', 0)
        if chain_length > 0:
            score += chain_length * 3  # Small bonus per chain step
            
            # Additional bonus for chains that cross API boundaries
            boundary_crossings = metadata.get('boundary_crossings', 0)
            if boundary_crossings > 0:
                score += boundary_crossings * 5
        
        # Add name pattern match bonus
        if hasattr(self, 'names_likely_match') and fully_qualified_name and api_name:
            fqn_component = fully_qualified_name.split('.')[-1]
            api_component = api_name.split('.')[-1]
            
            if fqn_component != api_component and self.names_likely_match(fqn_component, api_component):
                # Names match through pattern transformation - high confidence
                score += 25.0
                if isinstance(metadata, dict):
                    metadata['name_pattern_match'] = True
        
        # Set the calculated score
        candidate.score = score
        
        # Enhanced score breakdown for better diagnostics
        breakdown = {
            'base_match_type': match_type_score,
            'match_type': match_type,
            'api_boundary_bonus': 40.0 if is_boundary else 0.0,
            'path_similarity': 75.0 if api_name and candidate.module_path == api_name else 
                            (50.0 if candidate.module_path == fully_qualified_name else 0.0),
            'property_bonus': 30.0 if decorator_analysis and getattr(decorator_analysis, 'is_property', False) 
                            and "property" in candidate.url.lower() else 0.0,
            'overload_handling': 75.0 if decorator_analysis and not getattr(decorator_analysis, 'is_overload', False) else 0.0,
            'total': score
        }
        
        # Add score breakdown to metadata
        if hasattr(candidate, 'metadata'):
            if isinstance(candidate.metadata, dict):
                candidate.metadata['score_breakdown'] = breakdown
                
        # Log detailed scoring for debugging
        logger.debug(f"URL candidate scored {score:.1f} for {candidate.url}: {breakdown}")
    
    
    def update_name_patterns(self, prefixes: List[str], suffixes: List[str]) -> None:
        """Update with name patterns detected in codebase."""
        
        self.name_prefixes = set(prefixes)
        self.name_suffixes = set(suffixes)
        
        # Use these patterns in scoring
        def names_likely_match(impl_name: str, api_name: str) -> bool:
            # Direct match
            if impl_name == api_name:
                return True
                
            # Common prefix removal (e.g., _BatchNorm -> BatchNorm)
            for prefix in self.name_prefixes:
                if impl_name.startswith(prefix) and impl_name[len(prefix):] == api_name:
                    return True
                    
            # Common suffix removal (e.g., BatcherIterDataPipe -> Batcher)
            for suffix in self.name_suffixes:
                if impl_name.endswith(suffix) and impl_name[:-len(suffix)] == api_name:
                    return True
                    
            return False
        
        self.names_likely_match = names_likely_match
    
    
    def get_stats(self) -> Dict[str, Any]:
        """Get resolver statistics."""
        return {
            'resolved_conflicts': self.resolved_conflicts,
            'weights': {
                'match_type': self.match_type_scores,
                'path_type': self.path_type_scores,
                'decorator': self.decorator_scores
            }
        }
        
    def clear(self) -> None:
        """Clear resolution history."""
        self.resolved_conflicts = 0
        self.resolution_details = []



class DocumentationLinker:
    """
    Documentation URL linking with resolution strategies.
    
    Features:
    - Extraction of module and component names
    - Caching and performance optimization
    - Re-export handling
    - Flexible matching strategies
    - URL validation
    - Export chain awareness
    - Weighted conflict resolution
    - Distinction between direct and hierarchical matches
    """
    
    def __init__(self, url_file: str, pattern_file: str, config: Optional[AnalysisConfig] = None):
        """Initialize documentation linker."""
        
        self.config = config or AnalysisConfig()
        self.urls = self._load_urls(url_file)
        self.patterns = self._load_patterns(pattern_file)
        
        # Analysis infrastructure
        self.cache = URLCache(max_size=5000)
        self.matcher = HierarchicalMatcher(max_cache_size=10000)
        self.decorator_analyzer = DecoratorAnalyzer()
        self.conflict_resolver = URLConflictResolver()
        self.match_registry = URLMatchRegistry(max_entries=10000)
        
        # Import tracking
        self.import_paths: Dict[str, ImportInfo] = {}
        self.package_exports: Dict[str, str] = {}
        self.module_exports: Dict[str, Set[str]] = {}
        
        # Track URL match conflicts
        self.url_conflicts: List[Dict[str, Any]] = []
        
        # Pre-process URLs for faster lookup
        self.processed_urls = self._process_urls()
        
        # Track analysis start time for cache management
        self.analysis_start_time = time.time()
        
        # Statistics
        self.stats = {
            'total_urls': len(self.urls),
            'processed_urls': len(self.processed_urls),
            'match_attempts': 0,
            'successful_matches': 0,
            'cache_hits': 0,
            'overload_skips': 0,
            'url_reuse_blocks': 0
        }
    

    def _load_patterns(self, pattern_file: str) -> List[URLPattern]:
        """Load URL patterns from file."""
        try:
            with open(pattern_file, 'r') as f:
                data = json.load(f)
                
            patterns = []
            # Handle different pattern file formats
            if "patterns" in data:
                # Standard format with patterns list
                for pattern_data in data["patterns"]:
                    base_url = pattern_data.get("base_url")
                    sub_path = pattern_data.get("sub_path")
                    
                    if base_url:
                        patterns.append(URLPattern(
                            base_url=base_url,
                            sub_path=sub_path
                        ))
            elif "base_url" in data:
                # Simple format with single pattern
                patterns.append(URLPattern(
                    base_url=data["base_url"],
                    sub_path=data["sub_path"]
                ))
                
            if not patterns:
                raise DocumentationError("No valid patterns found in file")
                
            return patterns
            
        except Exception as e:
            raise DocumentationError(f"Failed to load patterns: {e}")
            
    
    def _load_urls(self, url_file: str) -> List[str]:
        """Load URLs from file."""
        try:
            with open(url_file, 'r') as f:
                return [line.strip() for line in f if line.strip()]
        except Exception as e:
            raise DocumentationError(f"Failed to load URLs: {e}")
    
    
    def _process_urls(self) -> Dict[str, ModulePathInfo]:
        """
        Process URLs to extract path information.
        
        Returns:
            Dictionary mapping module paths to URL info
        """
        
        processed = {}
        
        for url in self.urls:
            try:
                # Find matching pattern
                pattern = next((p for p in self.patterns if p.matches(url)), None)
                if not pattern:
                    continue
                    
                # Extract path information
                info = self._extract_path_info(url, pattern)
                if info and info.module_member_path:
                    processed[info.module_member_path] = info
                    
            except Exception as e:
                logger.debug(f"Failed to process URL {url}: {e}")
                
        return processed
    
    
    def _extract_path_info(self, url: str, pattern: URLPattern) -> Optional[ModulePathInfo]:
        """
        Extract module and component information from URL.
        
        Args:
            url: Documentation URL
            pattern: Matching URL pattern
            
        Returns:
            ModulePathInfo with extracted paths
        """
             
        try:
            # Remove base_url
            remaining_path = url[len(pattern.base_url):].lstrip('/')
            
            # Handle different URL patterns
            if pattern.sub_path and pattern.sub_path + '.html' in remaining_path:
                # Case 3: sub_path.html#module_member_path
                parts = remaining_path.split('.html')
                path_before_html = None
                anchor_path = parts[1][1:] if len(parts) > 1 and parts[1].startswith('#') else None
            else:
                if pattern.sub_path:
                    # Check if sub_path exists in the URL
                    sub_path_index = remaining_path.find(pattern.sub_path)
                    if sub_path_index != -1:
                        # Get everything after sub_path without skipping any characters
                        module_section = remaining_path[sub_path_index + len(pattern.sub_path):].lstrip('/')
                        
                        # Split into path and anchor
                        parts = module_section.split('.html')
                        path_before_html = parts[0] if parts[0] else None
                        anchor_path = parts[1][1:] if len(parts) > 1 and parts[1].startswith('#') else None
                    else:
                        path_before_html = None
                        anchor_path = None
                else:
                    # No sub_path, extract directly from remaining path
                    parts = remaining_path.split('.html')
                    path_before_html = parts[0] if parts[0] else None
                    anchor_path = parts[1][1:] if len(parts) > 1 and parts[1].startswith('#') else None
            
            return ModulePathInfo(
                path_before_html=path_before_html,
                anchor_path=anchor_path,
                full_url=url
            )
            
        except Exception as e:
            logger.debug(f"Failed to extract path info from {url}: {e}")
            return None
        
    
    def update_from_module_definition(self, module_name: str, module_def: 'ModuleDefinition'):
        """
        Update path mappings from ModuleDefinition.
        
        Args:
            module_name: Module being analyzed
            module_def: Module definition with import and export information
        """
        
        # Track exports from this module
        if module_def.all_values:
            self.module_exports[module_name] = module_def.all_values
                
        # Process imports and potential re-exports
        for name, original_path in module_def.imported_names.items():
            is_reexported = name in module_def.all_values
            
            # Extract source module from original path
            source_module = original_path
            if '.' in original_path:
                source_module, imported_name = original_path.rsplit('.', 1)
                # Only set source_module if the imported name matches
                if imported_name != name:
                    source_module = original_path
            
            # Track import path
            self.import_paths[name] = ImportInfo(
                source_module=source_module,
                imported_as=name,
                is_reexported=is_reexported
            )
            
            # Track package exports
            if is_reexported:
                # Track package-level path for re-exports
                package = self._get_package_name(module_name)
                if package:
                    self.package_exports[name] = f"{package}.{name}"        
     
                           
    def _resolve_import_module(self, module: str, level: int,
                             current_module: str) -> str:
        """Resolve relative import to absolute."""
        
        if level == 0:
            return module
            
        parts = current_module.split('.')
        if len(parts) < level:
            raise DocumentationError(
                f"Invalid relative import in {current_module}"
            )
            
        base = '.'.join(parts[:-level])
        return f"{base}.{module}" if module else base

    
    def _get_package_name(self, module_name: str) -> Optional[str]:
        """Get package name from module name."""
        parts = module_name.split('.')
        return parts[0] if parts else None
    
    
    def _build_synthetic_export_chain(self, fully_qualified_name: str, api_name: Optional[str]) -> List[Dict[str, Any]]:
        """
        Build a synthetic export chain when a real one is not available.
        
        Args:
            fully_qualified_name: Component's implementation path
            api_name: Component's API path
            
        Returns:
            Synthetic chain or empty list if not possible
        """
        if not api_name or api_name == fully_qualified_name:
            return []
        
        # Extract component name from FQN
        if '.' not in fully_qualified_name:
            return []
            
        fqn_module, fqn_name = fully_qualified_name.rsplit('.', 1)
        
        # Extract component name from API path
        if '.' not in api_name:
            return []
            
        api_module, api_name_part = api_name.rsplit('.', 1)
        
        # Names must match for a valid chain
        if fqn_name != api_name_part:
            return []
        
        # Build a minimal synthetic chain
        synthetic_chain = [
            # Step 1: Definition
            {
                'module_path': fqn_module,
                'export_name': fqn_name,
                'is_explicit': False,
                'import_type': 'definition',
                'is_package_boundary': False
            },
            # Step 2: API export
            {
                'module_path': api_module,
                'export_name': api_name_part,
                'is_explicit': True,
                'import_type': 'direct',
                'is_package_boundary': True
            }
        ]
        
        return synthetic_chain
    
    
    def find_documentation_url_with_relationships(self, 
                                               fully_qualified_name: str,
                                               api_name: Optional[str] = None,
                                               export_chain: Optional[List[Any]] = None,
                                               decorator_info: Optional[Any] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        Find documentation URL with import/export relationship and decorator awareness.
        
        Args:
            fully_qualified_name: Component's fully qualified name
            api_name: Optional API name if different from FQN
            export_chain: Optional export chain information
            decorator_info: Optional decorator information
            
        Returns:
            Tuple of (documentation_url, module_member_path) if found
        """
        
        self.stats['match_attempts'] += 1
        
        logger.debug(f"Finding documentation URL for {fully_qualified_name} (API name: {api_name})" if api_name else "")
        
        # Decorator analysis
        decorator_analysis = None
        if decorator_info:
            try:
                if isinstance(decorator_info, list):
                    decorator_analysis = self.decorator_analyzer.analyze(
                        {'decorators': decorator_info})
                else:
                    decorator_analysis = self.decorator_analyzer.analyze(decorator_info)
                    
                # CHANGE: Better overload handling
                if decorator_analysis and decorator_analysis.is_overload:
                    # Still process overloaded functions - but log it
                    logger.debug(f"Processing overloaded function: {fully_qualified_name}")
            except Exception as e:
                logger.debug(f"Error analyzing decorators: {e}")
                # Continue without decorator analysis
        
        # Log export chain information if available
        if export_chain:
            chain_str = " -> ".join(str(step) for step in export_chain[:3])
            if len(export_chain) > 3:
                chain_str += f" -> ... ({len(export_chain)} steps total)"
            logger.debug(f"Export chain: {chain_str}")
        
        # Check cache first
        cache_key = f"relationship:{fully_qualified_name}:{api_name or ''}"
        if self.cache:
            cached_result = self.cache.get_url_path(cache_key)
            if cached_result:
                self.stats['cache_hits'] += 1
                logger.debug(f"Cache hit for {fully_qualified_name}")
                return cached_result
        
        # Normalize export chain with type safety
        normalized_chain = []
        if export_chain:
            # Handle multiple export chain formats
            if isinstance(export_chain, list):
                for step in export_chain:
                    if isinstance(step, str):
                        normalized_chain.append(step)
                    elif isinstance(step, dict) and 'module_path' in step:
                        normalized_chain.append(step['module_path'])
            
        # Collect all match candidates
        candidates = []
        
        # Try API path direct match first (highest priority)
        if api_name and api_name != fully_qualified_name:
            for path, info in self.processed_urls.items():
                if path == api_name:  # Direct match
                    # Check if this module path is an API boundary
                    module_part = api_name.rsplit('.', 1)[0] if '.' in api_name else api_name
                    is_boundary = module_part in getattr(self, 'api_boundaries', [])
                    
                    candidates.append(URLMatchCandidate(
                        url=info.full_url,
                        component_fqn=fully_qualified_name,
                        module_path=path,
                        match_type="direct",
                        metadata={
                            "is_api_path": True,
                            "is_api_boundary": is_boundary,
                            "match_comment": "Direct API path match"
                        }
                    ))
                    logger.debug(f"Found direct API path match: {path} -> {info.full_url}")
        
        # Try implementation path direct match
        for path, info in self.processed_urls.items():
            if path == fully_qualified_name:  # Direct match
                # Check if this module path is an API boundary
                module_part = fully_qualified_name.rsplit('.', 1)[0] if '.' in fully_qualified_name else fully_qualified_name
                is_boundary = module_part in getattr(self, 'api_boundaries', [])
                
                candidates.append(URLMatchCandidate(
                    url=info.full_url,
                    module_path=path,
                    component_fqn=fully_qualified_name,
                    match_type="direct",
                    metadata={
                        "is_implementation_path": True,
                        "is_api_boundary": is_boundary,
                        "match_comment": "Direct implementation path match"
                    }
                ))
                logger.debug(f"Found direct implementation path match: {path} -> {info.full_url}")
                
        # Try export chain paths
        if normalized_chain:
            chain_paths_processed = set()  # Track already processed paths

            # First try modules that are API boundaries
            for module_path in normalized_chain:
                # Skip already processed paths
                if module_path in chain_paths_processed:
                    continue
                    
                chain_paths_processed.add(module_path)
                
                # Check if module is an API boundary
                is_boundary = module_path in getattr(self, 'api_boundaries', [])
                
                # Try to extract component name from FQN
                if '.' in fully_qualified_name:
                    component_name = fully_qualified_name.rsplit('.', 1)[1]
                    # Try matching at this chain step
                    test_path = f"{module_path}.{component_name}"
                    
                    # First priority: Direct match at an API boundary
                    if is_boundary:
                        for path, info in self.processed_urls.items():
                            if path == test_path:
                                candidates.append(URLMatchCandidate(
                                    url=info.full_url,
                                    module_path=path,
                                    component_fqn=fully_qualified_name,
                                    match_type="export_chain",
                                    metadata={
                                        "is_package_boundary": True,
                                        "is_api_boundary": True,
                                        "export_chain_length": len(normalized_chain),
                                        "chain_position": normalized_chain.index(module_path),
                                        "boundary_crossings": sum(1 for m in normalized_chain 
                                                            if m in getattr(self, 'api_boundaries', [])),
                                        "match_comment": "API boundary in export chain"
                                    }
                                ))
                                logger.debug(f"Found API boundary match in export chain: {test_path} -> {info.full_url}")
                                break
                    
                    # Next priority: Non-boundary direct match
                    if not is_boundary:
                        for path, info in self.processed_urls.items():
                            if path == test_path:
                                candidates.append(URLMatchCandidate(
                                    url=info.full_url,
                                    module_path=path,
                                    component_fqn=fully_qualified_name,
                                    match_type="export_chain",
                                    metadata={
                                        "is_package_boundary": False,
                                        "is_api_boundary": False,
                                        "export_chain_length": len(normalized_chain),
                                        "chain_position": normalized_chain.index(module_path),
                                        "boundary_crossings": sum(1 for m in normalized_chain 
                                                            if m in getattr(self, 'api_boundaries', [])),
                                        "match_comment": "Non-boundary module in export chain"
                                    }
                                ))
                                logger.debug(f"Found non-boundary match in export chain: {test_path} -> {info.full_url}")
                                break
                        
                    # Last priority: Hierarchical match at any chain step
                    for path, info in self.processed_urls.items():
                        # Skip if hierarchical match not allowed
                        if not self.match_registry.check_match_allowed(info.full_url, 'hierarchical'):
                            continue
                            
                        if self.matcher.match_paths(path, test_path):
                            candidates.append(URLMatchCandidate(
                                url=info.full_url,
                                module_path=path,
                                component_fqn=fully_qualified_name,
                                match_type="hierarchical",
                                metadata={
                                    "is_package_boundary": is_boundary,
                                    "is_api_boundary": is_boundary,
                                    "export_chain_length": len(normalized_chain),
                                    "chain_position": normalized_chain.index(module_path),
                                    "boundary_crossings": sum(1 for m in normalized_chain 
                                                        if m in getattr(self, 'api_boundaries', [])),
                                    "match_comment": "Hierarchical match in export chain"
                                }
                            ))
                            logger.debug(f"Found hierarchical match in export chain: {path} ~ {test_path} -> {info.full_url}")
        
        
        # Try hierarchical matches for API name 
        if api_name:
            api_module = api_name.rsplit('.', 1)[0] if '.' in api_name else api_name
            is_boundary = api_module in getattr(self, 'api_boundaries', [])
            
            for path, info in self.processed_urls.items():
                # Skip if hierarchical match not allowed
                if not self.match_registry.check_match_allowed(info.full_url, 'hierarchical'):
                    continue
                    
                if self.matcher.match_paths(path, api_name):
                    candidates.append(URLMatchCandidate(
                        url=info.full_url,
                        module_path=path,
                        component_fqn=fully_qualified_name,
                        match_type="hierarchical",
                        metadata={
                            "is_api_path": True,
                            "is_api_boundary": is_boundary,
                            "match_comment": "Hierarchical API path match"
                        }
                    ))
                    logger.debug(f"Found hierarchical API path match: {path} ~ {api_name} -> {info.full_url}")
        
        # Try hierarchical matches for implementation path
        for path, info in self.processed_urls.items():
            # Skip if hierarchical match not allowed
            if not self.match_registry.check_match_allowed(info.full_url, 'hierarchical'):
                continue
                
            if self.matcher.match_paths(path, fully_qualified_name):
                module_part = fully_qualified_name.rsplit('.', 1)[0] if '.' in fully_qualified_name else fully_qualified_name
                is_boundary = module_part in getattr(self, 'api_boundaries', [])
                
                candidates.append(URLMatchCandidate(
                    url=info.full_url,
                    module_path=path,
                    component_fqn=fully_qualified_name,
                    match_type="hierarchical",
                    metadata={
                        "is_implementation_path": True,
                        "is_api_boundary": is_boundary,
                        "match_comment": "Hierarchical implementation path match"
                    }
                ))
                logger.debug(f"Found hierarchical implementation path match: {path} ~ {fully_qualified_name} -> {info.full_url}")
        
        # Resolve conflicts if multiple candidates
        if candidates:
            # Log candidate count
            logger.debug(f"Found {len(candidates)} URL candidates for {fully_qualified_name}")
            
            # Use conflict resolver to select best candidate
            best_candidate = self.conflict_resolver.resolve(
                candidates=candidates,
                fully_qualified_name=fully_qualified_name,
                api_name=api_name,
                decorator_analysis=decorator_analysis
            )
            
            if best_candidate:
                # Register the match in the URL match registry
                match_type = 'direct' if best_candidate.match_type in ['direct', 'api_path'] else 'hierarchical'
                if self.match_registry.register_match(best_candidate.url, fully_qualified_name, match_type):
                    if self.cache:
                        self.cache.set_url_path(cache_key, best_candidate.url, best_candidate.module_path)
                    self.stats['successful_matches'] += 1
                    logger.info(f"Selected best URL for {fully_qualified_name}: {best_candidate.url}")
                    return best_candidate.url, best_candidate.module_path
                else:
                    logger.warning(f"Match registration rejected for {fully_qualified_name} -> {best_candidate.url}")
        else:
            logger.debug(f"No URL candidates found for {fully_qualified_name}")
        
        # Nothing found
        return None, None
    
    
    def find_documentation_url(self, 
                       fully_qualified_name: str,
                       api_name: Optional[Union[str, List[str]]] = None,
                       export_chain: Optional[List[Any]] = None,
                       decorator_info: Optional[Any] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        Find documentation URL without triggering recursive API resolution.
        
        Args:
            fully_qualified_name: Component's fully qualified name
            api_name: Optional API name if different from FQN
            export_chain: Optional export chain information
            decorator_info: Optional decorator information
            
        Returns:
            Tuple of (documentation_url, module_member_path) if found
        """
        
        try:
            # Validate and sanitize inputs
            if not fully_qualified_name:
                return None, None
                
            # Track matching attempt
            self.stats['match_attempts'] += 1
            
            # Check cache first
            cache_key = f"url:{fully_qualified_name}:{api_name or ''}"
            if self.cache:
                cached_result = self.cache.get_url_path(cache_key)
                if cached_result:
                    self.stats['cache_hits'] += 1
                    logger.debug(f"Cache hit for {fully_qualified_name}")
                    return cached_result
            
            # If no explicit API name provided but we have an API map, use it
            if api_name is None and hasattr(self, 'api_map'):
                if fully_qualified_name in self.api_map:
                    api_name = self.api_map[fully_qualified_name]
                    logger.debug(f"Using API name from map: {api_name}")
                    
            # Normalize export chain
            normalized_export_chain = None
            if export_chain:
                if isinstance(export_chain, list):
                    # Handle different export chain formats
                    if export_chain and isinstance(export_chain[0], dict):
                        # Format from serialized export chain
                        normalized_export_chain = [
                            step.get('module_path', '') 
                            for step in export_chain 
                            if isinstance(step, dict) and 'module_path' in step
                        ]
                    elif export_chain and isinstance(export_chain[0], str):
                        # Simple list of module paths
                        normalized_export_chain = export_chain
                else:
                    logger.debug(f"Unexpected export_chain type: {type(export_chain)}")
                    
            # Normalize decorator info
            normalized_decorator_info = None
            if decorator_info:
                if isinstance(decorator_info, list):
                    normalized_decorator_info = decorator_info
                elif isinstance(decorator_info, dict):
                    normalized_decorator_info = [decorator_info]
                else:
                    logger.debug(f"Unexpected decorator_info type: {type(decorator_info)}")
                
            # Use relationship-aware URL matching with normalized inputs
            # This special method does not call resolve_api_path
            url, module_path = self.find_documentation_url_with_relationships(
                fully_qualified_name=fully_qualified_name,
                api_name=api_name,
                export_chain=normalized_export_chain,
                decorator_info=normalized_decorator_info
            )
            
            # Cache result if found
            if url and self.cache:
                self.cache.set_url_path(cache_key, url, module_path)
                self.stats['successful_matches'] += 1
                
            return url, module_path
            
        except TypeError as e:
            logger.warning(f"Type error resolving URL for {fully_qualified_name}: {e}")
            return None, None
        except AttributeError as e:
            logger.warning(f"Attribute error resolving URL for {fully_qualified_name}: {e}")
            return None, None
        except Exception as e:
            logger.warning(f"Error resolving URL for {fully_qualified_name}: {e}")
            return None, None
    
    
    def _find_best_matching_component_for_url(self, url: str, components: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Find the best component match for a URL that has multiple candidates.
        
        Args:
            url: Documentation URL
            components: List of components that match this URL
            
        Returns:
            Best matching component or None
        """
        
        if not components:
            return None
            
        if len(components) == 1:
            return components[0]
            
        # Score each component
        scored_components = []
        for component in components:
            score = 0
        
            # Get fully qualified name and API name
            fqn = get_component_property(component, "fully_qualified_name") #component.get('fully_qualified_name', '')
            api_name = get_component_property(component, "API_name")#component.get('API_name', '')
            
            # Check if URL contains the component name
            component_name = fqn.split('.')[-1] if '.' in fqn else fqn
            if component_name and component_name in url:
                score += 15
                
            # Prefer components where API path is at end of an export chain
            if component.get('export_chain') and len(component.get('export_chain', [])) > 0:
                # More points for longer export chains
                score += min(15, len(component.get('export_chain', [])) * 3)
                
                # Check if any part of the chain is in the URL
                chain_modules = []
                for step in component.get('export_chain', []):
                    if isinstance(step, dict) and 'module_path' in step:
                        module = step['module_path']
                        chain_modules.append(module)
                        if module in url:
                            score += 10
                            break
            
            # Prefer components in __init__ modules
            if '__init__' in fqn:
                score += 5
                
            # Prefer components with shorter paths
            path_parts = fqn.split('.')
            score -= len(path_parts)
            
            # Prefer components with specific types
            comp_type = component.get('type', '')
            if comp_type == 'class':
                score += 8
            elif comp_type == 'function':
                score += 5
                
            # Prefer components with documentation
            if component.get('docstring'):
                score += 3
                
            scored_components.append((score, component))
            
        # Sort by score (descending)
        scored_components.sort(reverse=True, key=lambda x: x[0])
        
        # Return highest scoring component
        return scored_components[0][1]
    
    
    def set_api_boundaries(self, boundaries: List[str]) -> None:
        """
        Set API boundaries for improved URL matching.
        
        This method stores the provided API boundaries and ensures they're 
        properly used during URL matching and conflict resolution.
        
        Args:
            boundaries: List of module paths that serve as API boundaries
        """
        self.api_boundaries = boundaries
        logger.info(f"Received {len(boundaries)} API boundaries for URL matching")
        
        # Also share with the conflict resolver for scoring purposes
        if hasattr(self, 'conflict_resolver') and self.conflict_resolver:
            self.conflict_resolver.set_api_boundaries(boundaries)
        

    def update_name_patterns(self, prefixes: List[str], suffixes: List[str]) -> None:
        """Update name patterns from API resolver's analysis."""
        self.common_name_prefixes = set(prefixes)
        self.common_name_suffixes = set(suffixes)
        logger.info(f"Received name patterns: {len(prefixes)} prefixes, {len(suffixes)} suffixes")
        
        # Update conflict resolver to use these patterns
        if hasattr(self, 'conflict_resolver'):
            self.conflict_resolver.update_name_patterns(prefixes, suffixes)
    
    
    def _find_url_candidates(self, fully_qualified_name: str, api_name: Optional[str] = None,
                        export_chain: Optional[List[Any]] = None) -> List[URLMatchCandidate]:
        """
        Find all URL candidates for a component without calling API resolution.
        
        Args:
            fully_qualified_name: Component's fully qualified name
            api_name: Optional API name if different from FQN
            export_chain: Optional export chain information
            
        Returns:
            List of URL match candidates
        """
        candidates = []
        
        # Log the search
        logger.debug(f"Finding URL candidates for {fully_qualified_name}" + 
                    (f" with API name {api_name}" if api_name else ""))
        
        # Strategy 1: Try API path direct match first (highest priority)
        if api_name and api_name != fully_qualified_name:
            for path, info in self.processed_urls.items():
                if path == api_name:  # Direct match
                    # Check if this module path is an API boundary
                    module_part = api_name.rsplit('.', 1)[0] if '.' in api_name else api_name
                    is_boundary = module_part in getattr(self, 'api_boundaries', [])
                    
                    candidates.append(URLMatchCandidate(
                        url=info.full_url,
                        component_fqn=fully_qualified_name,
                        module_path=path,
                        match_type="direct",
                        metadata={
                            "is_api_path": True,
                            "is_api_boundary": is_boundary,
                            "match_comment": "Direct API path match"
                        }
                    ))
                    logger.debug(f"Found direct API path match: {path} -> {info.full_url}")
        
        # Strategy 2: Try implementation path direct match
        for path, info in self.processed_urls.items():
            if path == fully_qualified_name:  # Direct match
                # Check if this module path is an API boundary
                module_part = fully_qualified_name.rsplit('.', 1)[0] if '.' in fully_qualified_name else fully_qualified_name
                is_boundary = module_part in getattr(self, 'api_boundaries', [])
                
                candidates.append(URLMatchCandidate(
                    url=info.full_url,
                    module_path=path,
                    component_fqn=fully_qualified_name,
                    match_type="direct",
                    metadata={
                        "is_implementation_path": True,
                        "is_api_boundary": is_boundary,
                        "match_comment": "Direct implementation path match"
                    }
                ))
                logger.debug(f"Found direct implementation path match: {path} -> {info.full_url}")
                
        # Strategy 3: Try export chain paths
        normalized_chain = []
        if export_chain:
            # Handle multiple export chain formats
            if isinstance(export_chain, list):
                for step in export_chain:
                    if isinstance(step, str):
                        normalized_chain.append(step)
                    elif isinstance(step, dict) and 'module_path' in step:
                        normalized_chain.append(step['module_path'])
        
        if normalized_chain:
            chain_paths_processed = set()  # Track already processed paths

            # Process chain modules
            for module_path in normalized_chain:
                # Skip already processed paths
                if module_path in chain_paths_processed:
                    continue
                    
                chain_paths_processed.add(module_path)
                
                # Check if module is an API boundary
                is_boundary = module_path in getattr(self, 'api_boundaries', [])
                
                # Try to extract component name from FQN
                if '.' in fully_qualified_name:
                    component_name = fully_qualified_name.rsplit('.', 1)[1]
                    # Try matching at this chain step
                    test_path = f"{module_path}.{component_name}"
                    
                    # First priority: Direct match at an API boundary
                    for path, info in self.processed_urls.items():
                        if path == test_path:
                            candidates.append(URLMatchCandidate(
                                url=info.full_url,
                                module_path=path,
                                component_fqn=fully_qualified_name,
                                match_type="export_chain",
                                metadata={
                                    "is_package_boundary": is_boundary,
                                    "is_api_boundary": is_boundary,
                                    "export_chain_length": len(normalized_chain),
                                    "chain_position": normalized_chain.index(module_path),
                                    "boundary_crossings": sum(1 for m in normalized_chain 
                                                        if m in getattr(self, 'api_boundaries', [])),
                                    "match_comment": "Chain module match"
                                }
                            ))
                            logger.debug(f"Found chain match: {test_path} -> {info.full_url}")
                            break
        
        # Strategy 4: Try hierarchical matches for API name and implementation path
        # (only if URL matching registry allows)
        if api_name:
            for path, info in self.processed_urls.items():
                # Skip if hierarchical match not allowed
                if hasattr(self, 'match_registry') and not self.match_registry.check_match_allowed(info.full_url, 'hierarchical'):
                    continue
                    
                if self.matcher.match_paths(path, api_name):
                    module_part = api_name.rsplit('.', 1)[0] if '.' in api_name else api_name
                    is_boundary = module_part in getattr(self, 'api_boundaries', [])
                    
                    candidates.append(URLMatchCandidate(
                        url=info.full_url,
                        module_path=path,
                        component_fqn=fully_qualified_name,
                        match_type="hierarchical",
                        metadata={
                            "is_api_path": True,
                            "is_api_boundary": is_boundary,
                            "match_comment": "Hierarchical API path match"
                        }
                    ))
                    logger.debug(f"Found hierarchical API path match: {path} ~ {api_name} -> {info.full_url}")
        
        for path, info in self.processed_urls.items():
            # Skip if hierarchical match not allowed
            if hasattr(self, 'match_registry') and not self.match_registry.check_match_allowed(info.full_url, 'hierarchical'):
                continue
                
            if self.matcher.match_paths(path, fully_qualified_name):
                module_part = fully_qualified_name.rsplit('.', 1)[0] if '.' in fully_qualified_name else fully_qualified_name
                is_boundary = module_part in getattr(self, 'api_boundaries', [])
                
                candidates.append(URLMatchCandidate(
                    url=info.full_url,
                    module_path=path,
                    component_fqn=fully_qualified_name,
                    match_type="hierarchical",
                    metadata={
                        "is_implementation_path": True,
                        "is_api_boundary": is_boundary,
                        "match_comment": "Hierarchical implementation path match"
                    }
                ))
                logger.debug(f"Found hierarchical implementation path match: {path} ~ {fully_qualified_name} -> {info.full_url}")
        
        logger.debug(f"Found {len(candidates)} URL candidates for {fully_qualified_name}")
        return candidates
    
    
    def analyze_coverage(self, components: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
        """
        Analyze documentation coverage for components.
        
        Args:
            components: Dictionary mapping files to their components
            
        Returns:
            Coverage statistics and missing documentation info
        """
        
        stats = {
            'total_components': 0,
            'documented_components': 0,
            'undocumented_public': 0,
            'coverage_percentage': 0.0
        }
        
        missing_docs = []
        
        for file_path, file_components in components.items():
            for component in file_components:
                stats['total_components'] += 1
                
                if component.get('doc_url'):
                    stats['documented_components'] += 1
                elif component.get('is_public', True):
                    stats['undocumented_public'] += 1
                    missing_docs.append({
                        'name': component['name'],
                        'fqn': component.get('fully_qualified_name', ''),
                        'api_name': component.get('API_name', ''),
                        'file': file_path
                    })
                    
        if stats['total_components'] > 0:
            stats['coverage_percentage'] = (
                stats['documented_components'] / stats['total_components'] * 100
            )
            
        return {
            'stats': stats,
            'missing_documentation': missing_docs
        }
    
    
    def clear_caches(self):
        """Clear all caches and registries."""
        self.cache.clear()
        self.matcher.clear()
        self.match_registry.clear()
        self.conflict_resolver.clear()
        self.import_paths.clear()
        self.package_exports.clear()
        self.module_exports.clear()
        
        # Force garbage collection
        gc.collect()
    
    
    def mark_analysis_completed(self):
        """
        Mark analysis as completed and clean up resources.
        
        This method should be called when repository analysis is complete.
        """
        # Calculate final statistics
        analysis_duration = time.time() - self.analysis_start_time
        
        # Add combined statistics
        self.stats.update({
            'analysis_duration': analysis_duration,
            'cache_stats': self.cache.get_stats(),
            'matcher_stats': self.matcher.get_stats(),
            'registry_stats': self.match_registry.get_stats(),
            'resolver_stats': self.conflict_resolver.get_stats()
        })
        
        # Log completion
        logger.info(f"Documentation linking completed in {analysis_duration:.2f}s")
        logger.info(f"Processed {self.stats['total_urls']} URLs")
        logger.info(f"Successfully matched {self.stats['successful_matches']} components")
        
        # Clear caches to free memory
        self.clear_caches()
    
    
    def get_diagnostics(self) -> Dict[str, Any]:
        """Get diagnostic information about documentation linking."""
        return {
            'stats': self.stats,
            'url_conflicts': self.url_conflicts,
            'resolver_details': self.conflict_resolver.resolution_details
        }
