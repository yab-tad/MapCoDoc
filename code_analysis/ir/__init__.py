"""
Intermediate Representation (IR) package for code analysis.

Provides models for representing code structures and utilities for conversion and caching.
"""

from .models import (
    IRModule,
    IRComponent,
    IRFunction,
    IRClass,
    IRVariable,
    IRImport,
    IRExport,
    IRLocation,
    IRMetadata,
    IRParameter
)
from .converter import convert_analysis_result_to_ir
from .cache import (
    generate_cache_key,
    get_cache_file_path,
    read_from_cache,
    write_to_cache
)


__all__ = [
    "IRModule", "IRComponent", "IRFunction", "IRClass", "IRVariable",
    "IRImport", "IRExport", "IRLocation", "IRMetadata", "IRParameter",
    "convert_module_to_ir",
    "generate_cache_key", "get_cache_file_path", "read_from_cache", "write_to_cache"
]
