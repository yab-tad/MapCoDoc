"""
Intermediate Representation (IR) Models for Code Analysis.

These models define a language-agnostic representation of code structures,
suitable for serialization and caching. They use Pydantic for validation
and schema definition.
"""

from typing import List, Optional, Dict, Any, Union, Literal
from pydantic import BaseModel, Field
import datetime


# --- Core Component Models ---

class IRLocation(BaseModel):
    """Represents a location in the source code."""
    file_path: str
    line_start: int
    line_end: int
    col_start: Optional[int] = None
    col_end: Optional[int] = None


class IRMetadata(BaseModel):
    """Generic metadata container."""
    analysis_timestamp: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    tool_version: Optional[str] = None # e.g., MapCoDoc version
    custom: Dict[str, Any] = Field(default_factory=dict)


class IRComponent(BaseModel):
    """Base model for any code component (function, class, variable)."""
    name: str # Simple name within its scope
    qualified_name: str # Fully qualified name (e.g., package.module.Class.method)
    component_type: str # e.g., 'function', 'class', 'method', 'variable', 'module'
    location: Optional[IRLocation] = None
    docstring: Optional[str] = None
    metadata: IRMetadata = Field(default_factory=IRMetadata)
    # Add common attributes here if needed


class IRParameter(BaseModel):
    """Represents a function or method parameter."""
    name: str
    annotation: Optional[str] = None
    default_value: Optional[str] = None # Represent default as string for simplicity
    kind: str # e.g., POSITIONAL_OR_KEYWORD, VAR_POSITIONAL, KEYWORD_ONLY, etc.


class IRFunction(IRComponent):
    """IR model for a function or method."""
    component_type: Literal["function"] = "function"
    parameters: List[IRParameter] = Field(default_factory=list)
    return_annotation: Optional[str] = None
    is_async: bool = False
    decorators: List[str] = Field(default_factory=list) # Store decorator names/reprs


class IRClass(IRComponent):
    """IR model for a class."""
    component_type: Literal["class"] = "class"
    base_classes: List[str] = Field(default_factory=list) # List of qualified names
    methods: List[IRFunction] = Field(default_factory=list) # Nested methods
    class_variables: List['IRVariable'] = Field(default_factory=list) # Nested variables
    nested_classes: List['IRClass'] = Field(default_factory=list) # Nested classes


class IRVariable(IRComponent):
    """IR model for a variable (module-level, class-level, instance-level)."""
    component_type: Literal["variable"] = "variable"
    annotation: Optional[str] = None
    value_repr: Optional[str] = None # String representation of the assigned value


# --- Module Level Models ---

class IRImport(BaseModel):
    """Represents an import statement."""
    source_module: Optional[str] = None # Module being imported from (e.g., 'os' in 'import os')
    imported_name: str # Name being imported (e.g., 'path' in 'from os import path', or 'os')
    alias: Optional[str] = None # Alias used (e.g., 'np' in 'import numpy as np')
    is_relative: bool = False
    relative_level: Optional[int] = None
    is_star_import: bool = False
    location: Optional[IRLocation] = None


class IRExport(BaseModel):
    """Represents an exported name from a module (e.g., via __all__)."""
    name: str # The name as it's exported
    original_qualified_name: Optional[str] = None # FQN of the item being exported if re-exported
    is_reexport: bool = False


class IRModule(IRComponent):
    """IR model for a Python module."""
    component_type: Literal["module"] = "module"
    imports: List[IRImport] = Field(default_factory=list)
    exports: Optional[List[IRExport]] = None # None if no __all__, empty list if __all__ = []
    components: List[Union[IRFunction, IRClass, IRVariable]] = Field(default_factory=list) # Top-level components


# Update forward refs for nested types
IRClass.model_rebuild()
