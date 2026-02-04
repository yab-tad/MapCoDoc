# """
# Module defining standard relationship types for code analysis.

# This module provides a central registry of all relationship types used in the
# codebase, along with their categories, descriptions, and bidirectional mappings.
# """

# import enum
# from typing import Dict, List, Set, Optional, Any
# from dataclasses import dataclass, field


# # --- Relationship Type Constants ---

# # Core Relationships
# REL_TYPE_CALLS = "CALLS"
# REL_TYPE_CALLED_BY = "CALLED_BY"
# REL_TYPE_IMPORTS = "IMPORTS"
# REL_TYPE_IMPORTED_BY = "IMPORTED_BY"
# REL_TYPE_INHERITS = "INHERITS"
# REL_TYPE_INHERITED_BY = "INHERITED_BY"
# REL_TYPE_IMPLEMENTS = "IMPLEMENTS"
# REL_TYPE_IMPLEMENTED_BY = "IMPLEMENTED_BY"
# REL_TYPE_OVERRIDES = "OVERRIDES"
# REL_TYPE_OVERRIDDEN_BY = "OVERRIDDEN_BY"
# REL_TYPE_CONTAINS = "CONTAINS" # e.g., Package CONTAINS Module, Class CONTAINS Method
# REL_TYPE_CONTAINED_BY = "CONTAINED_BY" # e.g., Class contained by Module

# # Definition and Scope
# REL_TYPE_DEFINES = "DEFINES" # e.g., Class defines Method
# REL_TYPE_DEFINED_IN = "DEFINED_IN" # e.g., Function DEFINED_IN Module

# # Usage and References
# REL_TYPE_USES = "USES" # General usage (e.g., function uses variable)
# REL_TYPE_USED_BY = "USED_BY"
# REL_TYPE_REFERENCES = "REFERENCES" # More specific reference (e.g., type hint)
# REL_TYPE_REFERENCED_BY = "REFERENCED_BY"
# REL_TYPE_ASSIGNS = "ASSIGNS"
# REL_TYPE_ASSIGNED_BY = "ASSIGNED_BY"


# # Exporting
# REL_TYPE_EXPORTS = "EXPORTS" # A module exports a name (defined locally or re-exported)
# REL_TYPE_REEXPORTS = "REEXPORTS" # A module explicitly re-exports a name imported from elsewhere
# REL_TYPE_EXPORTED_FROM = "EXPORTED_FROM" # e.g., Name exported from Module

# # Annotations and Typing
# REL_TYPE_ANNOTATES = "ANNOTATES" # e.g., Type annotates Variable/Parameter/Return
# REL_TYPE_ANNOTATED_BY = "ANNOTATED_BY"
# REL_TYPE_PARAMETER_OF = "PARAMETER_OF" # e.g., Type is parameter of Function
# REL_TYPE_HAS_PARAMETER = "HAS_PARAMETER" # e.g., Function has parameter Type
# REL_TYPE_RETURN_TYPE_OF = "RETURN_TYPE_OF" # e.g., Type is return type of Function
# REL_TYPE_HAS_RETURN_TYPE = "HAS_RETURN_TYPE" # e.g., Function has return type Type

# # Decorators
# REL_TYPE_DECORATES = "DECORATES"
# REL_TYPE_DECORATED_BY = "DECORATED_BY"

# # Dependencies
# REL_TYPE_DEPENDS_ON = "DEPENDS_ON" # General dependency
# REL_TYPE_DEPENDENCY_OF = "DEPENDENCY_OF"

# # Exceptions (Consider if needed)
# # REL_TYPE_THROWS = "THROWS"
# # REL_TYPE_CAUGHT_BY = "CAUGHT_BY" # Inverse of THROWS? Or separate CATCHES?
# # REL_TYPE_CATCHES = "CATCHES"

# # Extensions (Consider if needed for plugin systems etc.)
# REL_TYPE_EXTENDS = "EXTENDS"
# REL_TYPE_EXTENDED_BY = "EXTENDED_BY"


# # --- Node Type Constants ---
# NODE_TYPE_UNKNOWN = "UNKNOWN"
# NODE_TYPE_PACKAGE = "PACKAGE"
# NODE_TYPE_MODULE = "MODULE"
# NODE_TYPE_CLASS = "CLASS"
# NODE_TYPE_FUNCTION = "FUNCTION"
# NODE_TYPE_METHOD = "METHOD"
# NODE_TYPE_VARIABLE = "VARIABLE"
# NODE_TYPE_PARAMETER = "PARAMETER"
# NODE_TYPE_DECORATOR = "DECORATOR"
# NODE_TYPE_INTERFACE = "INTERFACE"
# NODE_TYPE_FILE = "FILE"  # For representing non-Python files if needed
# NODE_TYPE_ANY = "ANY"
# NODE_TYPE_EXTERNAL = "EXTERNAL" # For external dependencies/libraries

# NODE_TYPES = {
#     NODE_TYPE_UNKNOWN,
#     NODE_TYPE_PACKAGE,
#     NODE_TYPE_MODULE,
#     NODE_TYPE_CLASS,
#     NODE_TYPE_FUNCTION,
#     NODE_TYPE_METHOD,
#     NODE_TYPE_VARIABLE,
#     NODE_TYPE_PARAMETER,
#     NODE_TYPE_DECORATOR,
#     NODE_TYPE_INTERFACE,
#     NODE_TYPE_FILE,
#     NODE_TYPE_ANY,
#     NODE_TYPE_EXTERNAL,
# }



# # --- Relationship Pairs ---
# # Defines the inverse relationship for bidirectional linking.
# # Ensure all constants used here are defined above.
# RELATIONSHIP_PAIRS = {
    
#     REL_TYPE_IMPORTS: (NODE_TYPE_MODULE, NODE_TYPE_MODULE),
#     REL_TYPE_INHERITS: (NODE_TYPE_CLASS, NODE_TYPE_CLASS),
#     REL_TYPE_IMPLEMENTS: (NODE_TYPE_CLASS, NODE_TYPE_INTERFACE),
#     REL_TYPE_CALLS: (NODE_TYPE_FUNCTION, NODE_TYPE_FUNCTION), # Or METHOD
#     REL_TYPE_CONTAINS: (NODE_TYPE_PACKAGE, NODE_TYPE_MODULE), # Or PACKAGE -> PACKAGE
#     REL_TYPE_DEFINES: (NODE_TYPE_MODULE, NODE_TYPE_ANY), # Module defines Function/Class/Variable
#     REL_TYPE_USES: (NODE_TYPE_FUNCTION, NODE_TYPE_VARIABLE), # Function uses Variable
#     REL_TYPE_EXPORTS: (NODE_TYPE_MODULE, NODE_TYPE_ANY), # Module exports Name (could be func, class, var)
#     REL_TYPE_REEXPORTS: (NODE_TYPE_MODULE, NODE_TYPE_ANY), # Module re-exports Name
    
#     # REL_TYPE_CALLS: REL_TYPE_CALLED_BY,
#     REL_TYPE_CALLED_BY: REL_TYPE_CALLS,
#     # REL_TYPE_CONTAINS: REL_TYPE_CONTAINED_BY,
#     REL_TYPE_CONTAINED_BY: REL_TYPE_CONTAINS,
#     # REL_TYPE_IMPORTS: REL_TYPE_IMPORTED_BY,
#     REL_TYPE_IMPORTED_BY: REL_TYPE_IMPORTS,
#     # REL_TYPE_INHERITS: REL_TYPE_INHERITED_BY,
#     REL_TYPE_INHERITED_BY: REL_TYPE_INHERITS,
#     REL_TYPE_OVERRIDES: REL_TYPE_OVERRIDDEN_BY,
#     REL_TYPE_OVERRIDDEN_BY: REL_TYPE_OVERRIDES,
#     # REL_TYPE_CREATES: REL_TYPE_CREATED_BY, # Uncomment if CREATES/CREATED_BY are defined
#     # REL_TYPE_CREATED_BY: REL_TYPE_CREATES,
#     # REL_TYPE_USES: REL_TYPE_USED_BY,
#     REL_TYPE_USED_BY: REL_TYPE_USES,
#     # REL_TYPE_DEFINES: REL_TYPE_DEFINED_IN, # Now defined above
#     REL_TYPE_DEFINED_IN: REL_TYPE_DEFINES, # Now defined above
#     # REL_TYPE_IMPLEMENTS: REL_TYPE_IMPLEMENTED_BY,
#     REL_TYPE_IMPLEMENTED_BY: REL_TYPE_IMPLEMENTS,
#     # REL_TYPE_EXPORTS: REL_TYPE_EXPORTED_FROM, # Now defined above
#     REL_TYPE_EXPORTED_FROM: REL_TYPE_EXPORTS, # Now defined above
#     REL_TYPE_ANNOTATES: REL_TYPE_ANNOTATED_BY, # Now defined above
#     REL_TYPE_ANNOTATED_BY: REL_TYPE_ANNOTATES, # Now defined above
#     REL_TYPE_PARAMETER_OF: REL_TYPE_HAS_PARAMETER, # Now defined above
#     REL_TYPE_HAS_PARAMETER: REL_TYPE_PARAMETER_OF, # Now defined above
#     REL_TYPE_RETURN_TYPE_OF: REL_TYPE_HAS_RETURN_TYPE, # Now defined above
#     REL_TYPE_HAS_RETURN_TYPE: REL_TYPE_RETURN_TYPE_OF, # Now defined above
#     REL_TYPE_DECORATES: REL_TYPE_DECORATED_BY,
#     REL_TYPE_DECORATED_BY: REL_TYPE_DECORATES,
#     REL_TYPE_REFERENCES: REL_TYPE_REFERENCED_BY,
#     REL_TYPE_REFERENCED_BY: REL_TYPE_REFERENCES,
#     REL_TYPE_DEPENDS_ON: REL_TYPE_DEPENDENCY_OF, # Now defined above
#     REL_TYPE_DEPENDENCY_OF: REL_TYPE_DEPENDS_ON, # Now defined above
#     # Add pairs for EXTENDS, THROWS/CATCHES, ACCESSES/MODIFIES if those constants are defined
# }

# # --- Visualization Constants ---
# NODE_COLORS = {
#     NODE_TYPE_MODULE: "#cce5ff",
#     NODE_TYPE_PACKAGE: "#e5ccff",
#     NODE_TYPE_CLASS: "#fff0b3",
#     NODE_TYPE_FUNCTION: "#cceeff",
#     NODE_TYPE_METHOD: "#d9ead3",
#     NODE_TYPE_VARIABLE: "#f3e5f5",
#     NODE_TYPE_INTERFACE: "#d1e7dd",
#     NODE_TYPE_UNKNOWN: "#e0e0e0",
# }

# RELATIONSHIP_STYLES = {
#     REL_TYPE_IMPORTS: {'color': '#888888', 'style': 'dashed', 'arrowhead': 'open'},
#     REL_TYPE_INHERITS: {'color': '#0000ff', 'style': 'solid', 'arrowhead': 'empty'},
#     REL_TYPE_CALLS: {'color': '#ff0000', 'style': 'solid', 'arrowhead': 'normal'},
#     REL_TYPE_IMPLEMENTS: {'color': '#008000', 'style': 'dashed', 'arrowhead': 'empty'},
#     REL_TYPE_CONTAINS: {'color': '#ff8c00', 'style': 'dotted', 'arrowhead': 'none'},
#     REL_TYPE_REFERENCES: {'color': '#555555', 'style': 'dotted', 'arrowhead': 'vee'},
#     REL_TYPE_USES: {'color': '#777777', 'style': 'dotted', 'arrowhead': 'normal'},
#     # Add styles for other relationships
# }

# class RelationshipCategory(enum.Enum):
#     """Categories of relationship types to group similar relationships."""
#     IMPORT = "import"
#     INHERITANCE = "inheritance"
#     CALL = "call"
#     STRUCTURE = "structure"
#     REFERENCE = "reference"
#     DEPENDENCY = "dependency"


# @dataclass
# class RelationshipDefinition:
#     """Definition of a relationship type with metadata."""
#     name: str
#     category: RelationshipCategory
#     description: str
#     bidirectional_pair: Optional[str] = None
#     directed: bool = True
#     properties: Dict[str, Any] = field(default_factory=dict)
    

# # Registry of all relationship types
# registry: Dict[str, RelationshipDefinition] = {}

# # Bidirectional mapping of relationship types
# bidirectional_map: Dict[str, str] = {}


# def register_relationship(name: str, category: RelationshipCategory, description: str,
#                         bidirectional_pair: Optional[str] = None, 
#                         directed: bool = True,
#                         properties: Optional[Dict[str, Any]] = None) -> None:
#     """
#     Register a relationship type in the central registry.
    
#     Args:
#         name: Relationship type name
#         category: Category enum value
#         description: Description of the relationship
#         bidirectional_pair: Name of the inverse relationship (if applicable)
#         directed: Whether the relationship is directed (has a clear source and target)
#         properties: Additional properties for the relationship type
#     """
#     registry[name] = RelationshipDefinition(
#         name=name,
#         category=category,
#         description=description,
#         bidirectional_pair=bidirectional_pair,
#         directed=directed,
#         properties=properties or {}
#     )
    
#     # Register bidirectional mapping if provided
#     if bidirectional_pair:
#         bidirectional_map[name] = bidirectional_pair
#         # Ensure the pair is also registered if not already present
#         if bidirectional_pair not in registry:
#             inv_desc = f"Inverse of '{name}': {description}"
#             registry[bidirectional_pair] = RelationshipDefinition(
#                 name=bidirectional_pair,
#                 category=category,
#                 description=inv_desc,
#                 bidirectional_pair=name,
#                 directed=directed,
#                 properties=properties or {}
#             )
#             bidirectional_map[bidirectional_pair] = name


# def get_inverse_relationship(rel_type: str) -> Optional[str]:
#     """
#     Get the inverse relationship type for a given relationship.
    
#     Args:
#         rel_type: The relationship type to find the inverse for
        
#     Returns:
#         The inverse relationship type or None if not bidirectional
#     """
#     return bidirectional_map.get(rel_type)


# def get_relationships_by_category(category: RelationshipCategory) -> List[str]:
#     """
#     Get all relationship types in a given category.
    
#     Args:
#         category: The category to filter by
        
#     Returns:
#         List of relationship type names in the category
#     """
#     return [name for name, definition in registry.items() 
#             if definition.category == category]


# def validate_relationship_type(rel_type: str) -> bool:
#     """
#     Validate if a relationship type is registered.
    
#     Args:
#         rel_type: Relationship type to validate
        
#     Returns:
#         True if the relationship type is registered, False otherwise
#     """
#     return rel_type in registry


# # Register all relationship types
# # Import relationships
# register_relationship(
#     REL_TYPE_IMPORTS, 
#     RelationshipCategory.IMPORT,
#     "Module imports another module or component",
#     bidirectional_pair=REL_TYPE_IMPORTED_BY
# )

# register_relationship(
#     REL_TYPE_USES, 
#     RelationshipCategory.REFERENCE,
#     "Component uses (references) another component",
#     bidirectional_pair=REL_TYPE_USED_BY
# )

# # Class relationships
# register_relationship(
#     REL_TYPE_INHERITS, 
#     RelationshipCategory.INHERITANCE,
#     "Class inherits from another class",
#     bidirectional_pair=REL_TYPE_INHERITED_BY
# )

# register_relationship(
#     REL_TYPE_IMPLEMENTS, 
#     RelationshipCategory.INHERITANCE,
#     "Class implements an interface",
#     bidirectional_pair=REL_TYPE_IMPLEMENTED_BY,
#     properties={"is_interface": True}
# )

# register_relationship(
#     REL_TYPE_EXTENDS, 
#     RelationshipCategory.INHERITANCE,
#     "Class extends functionality via a mixin",
#     bidirectional_pair=REL_TYPE_EXTENDED_BY,
#     properties={"is_mixin": True}
# )

# # Function relationships
# register_relationship(
#     REL_TYPE_CALLS, 
#     RelationshipCategory.CALL,
#     "Function calls another function",
#     bidirectional_pair=REL_TYPE_CALLED_BY
# )

# register_relationship(
#     REL_TYPE_OVERRIDES, 
#     RelationshipCategory.INHERITANCE,
#     "Method overrides a method from a parent class",
#     bidirectional_pair=REL_TYPE_OVERRIDDEN_BY
# )

# register_relationship(
#     REL_TYPE_DECORATES, 
#     RelationshipCategory.STRUCTURE,
#     "Function decorates another function",
#     bidirectional_pair=REL_TYPE_DECORATED_BY
# )

# # Module relationships
# register_relationship(
#     REL_TYPE_EXPORTS, 
#     RelationshipCategory.STRUCTURE,
#     "Module exports a component",
#     bidirectional_pair=REL_TYPE_EXPORTED_FROM
# )

# register_relationship(
#     REL_TYPE_CONTAINS, 
#     RelationshipCategory.STRUCTURE,
#     "Component contains another component",
#     bidirectional_pair=REL_TYPE_CONTAINED_BY
# )

# # Variable relationships
# register_relationship(
#     REL_TYPE_REFERENCES, 
#     RelationshipCategory.REFERENCE,
#     "Component references a variable or attribute",
#     bidirectional_pair=REL_TYPE_REFERENCED_BY
# )

# register_relationship(
#     REL_TYPE_ASSIGNS, 
#     RelationshipCategory.REFERENCE,
#     "Component assigns to a variable or attribute",
#     bidirectional_pair=REL_TYPE_ASSIGNED_BY
# )

# # Dependency relationships
# register_relationship(
#     REL_TYPE_DEPENDS_ON, 
#     RelationshipCategory.DEPENDENCY,
#     "Component depends on another component",
#     bidirectional_pair=REL_TYPE_DEPENDENCY_OF
# ) 




# get_relationship_style,
# get_relationship_category,
# get_category_color,
# DEFAULT_STYLES

# VALID_REL_TYPES


"""
Defines constants for node and relationship types used in the graph store.
"""

from typing import Optional, Dict

# --- Node Types ---
NODE_TYPE_UNKNOWN = "UNKNOWN"
NODE_TYPE_PACKAGE = "PACKAGE"
NODE_TYPE_MODULE = "MODULE"
NODE_TYPE_CLASS = "CLASS"
NODE_TYPE_FUNCTION = "FUNCTION"
NODE_TYPE_METHOD = "METHOD"
NODE_TYPE_VARIABLE = "VARIABLE"
NODE_TYPE_PARAMETER = "PARAMETER"
NODE_TYPE_DECORATOR = "DECORATOR"
NODE_TYPE_FILE = "FILE" # For representing non-Python files if needed
NODE_TYPE_EXTERNAL = "EXTERNAL" # For external dependencies/libraries
NODE_TYPE_INTERFACE = "INTERFACE"
NODE_TYPE_ANY = "ANY" # Useful for broad queries

NODE_TYPES = {
    NODE_TYPE_UNKNOWN,
    NODE_TYPE_PACKAGE,
    NODE_TYPE_MODULE,
    NODE_TYPE_CLASS,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_VARIABLE,
    NODE_TYPE_PARAMETER,
    NODE_TYPE_DECORATOR,
    NODE_TYPE_FILE,
    NODE_TYPE_EXTERNAL,
    NODE_TYPE_INTERFACE,
    NODE_TYPE_ANY
}


# --- Relationship Types ---

# Core Relationships
REL_TYPE_DEFINES = "DEFINES" # e.g., Class defines Method, or the user-added one for test_graph
REL_TYPE_DEFINED_IN = "DEFINED_IN" # e.g., Function DEFINED_IN Module
REL_TYPE_CONTAINS = "CONTAINS"     # e.g., Package CONTAINS Module, Class CONTAINS Method
REL_TYPE_CONTAINED_BY = "CONTAINED_BY" # e.g., Class contained by Module

# Import/Export Relationships
REL_TYPE_IMPORTS = "IMPORTS"           # Module IMPORTS Module/Name
REL_TYPE_IMPORTED_BY = "IMPORTED_BY"   # Module/Name IMPORTED_BY Module (Inverse of IMPORTS)
REL_TYPE_WILDCARD_IMPORT = "WILDCARD_IMPORT" # Signifies that a module imports all names from another module
REL_TYPE_EXPORTS = "EXPORTS"           # Module EXPORTS Name (Directly defined or explicit in __all__)
REL_TYPE_REEXPORTS = "REEXPORTS"       # Module REEXPORTS Name (Imports and makes available, often via __init__.py or __all__)
REL_TYPE_EXPORTED_FROM = "EXPORTED_FROM" # e.g., Name exported from Module
REL_TYPE_USES_ALIAS = "USES_ALIAS"     # Import USES_ALIAS Name
REL_TYPE_MODULE_ALIAS = "MODULE_ALIAS" # An entire module is imported under an alias
REL_TYPE_NAME_ALIAS = "NAME_ALIAS" # A specific name (function, class, variable) is imported from a module under an alias

# Inheritance Relationships
REL_TYPE_INHERITS = "INHERITS"         # Class INHERITS Class (Superclass)
REL_TYPE_INHERITED_BY = "INHERITED_BY"
REL_TYPE_SUBCLASS_OF = "SUBCLASS_OF"   # Class SUBCLASS_OF Class (Inverse of INHERITS)
REL_TYPE_IMPLEMENTS = "IMPLEMENTS"     # Class IMPLEMENTS Interface (Abstract Base Class)
REL_TYPE_IMPLEMENTED_BY = "IMPLEMENTED_BY" # Interface IMPLEMENTED_BY Class

# Call Relationships
REL_TYPE_CALLS = "CALLS"               # Function/Method CALLS Function/Method
REL_TYPE_CALLED_BY = "CALLED_BY"       # Function/Method CALLED_BY Function/Method (Inverse of CALLS)

# Usage Relationships
REL_TYPE_USES = "USES"                 # Function/Method USES Variable/Class/Function
REL_TYPE_USED_BY = "USED_BY"           # Variable/Class/Function USED_BY Function/Method

# Decorator Relationships
REL_TYPE_DECORATES = "DECORATES"       # Decorator DECORATES Function/Method/Class
REL_TYPE_DECORATED_BY = "DECORATED_BY" # Function/Method/Class DECORATED_BY Decorator

# Documentation Relationships
REL_TYPE_DOC_LINK = "DOC_LINK"         # Component DOC_LINK URL/Path

# Other Potential Relationships
REL_TYPE_OVERRIDES = "OVERRIDES"       # Method OVERRIDES Method (In superclass)
REL_TYPE_OVERRIDDEN_BY = "OVERRIDDEN_BY" # Method OVERRIDDEN_BY Method (In subclass)
REL_TYPE_HAS_PARAMETER = "HAS_PARAMETER" # Function/Method HAS_PARAMETER Parameter
REL_TYPE_PARAMETER_OF = "PARAMETER_OF" # Parameter PARAMETER_OF Function/Method
REL_TYPE_RETURNS = "RETURNS"           # Function/Method RETURNS Type
REL_TYPE_RETURNED_BY = "RETURNED_BY"   # Type RETURNED_BY Function/Method
REL_TYPE_INSTANTIATES = "INSTANTIATES" # Function/Method INSTANTIATES Class
REL_TYPE_INSTANTIATED_BY = "INSTANTIATED_BY" # Class INSTANTIATED_BY Function/Method
REL_TYPE_REFERENCES = "REFERENCES" # More specific reference (e.g., type hint)
REL_TYPE_REFERENCED_BY = "REFERENCED_BY"
REL_TYPE_DEPENDS_ON = "DEPENDS_ON" # General dependency
REL_TYPE_DEPENDENCY_OF = "DEPENDENCY_OF"


RELATIONSHIP_TYPES = {
    REL_TYPE_DEFINED_IN,
    REL_TYPE_DEFINES,
    REL_TYPE_CONTAINS,
    REL_TYPE_CONTAINED_BY,
    REL_TYPE_IMPORTS,
    REL_TYPE_IMPORTED_BY,
    REL_TYPE_WILDCARD_IMPORT,
    REL_TYPE_EXPORTS,
    REL_TYPE_REEXPORTS,
    REL_TYPE_EXPORTED_FROM,
    REL_TYPE_USES_ALIAS,
    REL_TYPE_MODULE_ALIAS,
    REL_TYPE_NAME_ALIAS,
    REL_TYPE_INHERITS,
    REL_TYPE_INHERITED_BY,
    REL_TYPE_SUBCLASS_OF,
    REL_TYPE_IMPLEMENTS,
    REL_TYPE_IMPLEMENTED_BY,
    REL_TYPE_CALLS,
    REL_TYPE_CALLED_BY,
    REL_TYPE_USES,
    REL_TYPE_USED_BY,
    REL_TYPE_DECORATES,
    REL_TYPE_DECORATED_BY,
    REL_TYPE_DOC_LINK,
    REL_TYPE_OVERRIDES,
    REL_TYPE_OVERRIDDEN_BY,
    REL_TYPE_HAS_PARAMETER,
    REL_TYPE_PARAMETER_OF,
    REL_TYPE_RETURNS,
    REL_TYPE_RETURNED_BY,
    REL_TYPE_INSTANTIATES,
    REL_TYPE_INSTANTIATED_BY,
    REL_TYPE_REFERENCES,
    REL_TYPE_REFERENCED_BY,
    REL_TYPE_DEPENDS_ON,
    REL_TYPE_DEPENDENCY_OF
}

# Define pairs for easier inverse lookups or bidirectional processing
RELATIONSHIP_PAIRS = {
    REL_TYPE_IMPORTS: REL_TYPE_IMPORTED_BY,
    REL_TYPE_IMPORTED_BY: REL_TYPE_IMPORTS,
    REL_TYPE_INHERITS: REL_TYPE_INHERITED_BY,
    REL_TYPE_INHERITED_BY: REL_TYPE_INHERITS,
    REL_TYPE_IMPLEMENTS: REL_TYPE_IMPLEMENTED_BY,
    REL_TYPE_IMPLEMENTED_BY: REL_TYPE_IMPLEMENTS,
    REL_TYPE_CONTAINS: REL_TYPE_CONTAINED_BY,
    REL_TYPE_CONTAINED_BY: REL_TYPE_CONTAINS,
    REL_TYPE_CALLS: REL_TYPE_CALLED_BY,
    REL_TYPE_CALLED_BY: REL_TYPE_CALLS,
    REL_TYPE_USES: REL_TYPE_USED_BY,
    REL_TYPE_USED_BY: REL_TYPE_USES,
    REL_TYPE_DECORATES: REL_TYPE_DECORATED_BY,
    REL_TYPE_DECORATED_BY: REL_TYPE_DECORATES,
    REL_TYPE_OVERRIDES: REL_TYPE_OVERRIDDEN_BY,
    REL_TYPE_OVERRIDDEN_BY: REL_TYPE_OVERRIDES,
    REL_TYPE_HAS_PARAMETER: REL_TYPE_PARAMETER_OF,
    REL_TYPE_PARAMETER_OF: REL_TYPE_HAS_PARAMETER,
    REL_TYPE_RETURNS: REL_TYPE_RETURNED_BY,
    REL_TYPE_INSTANTIATES: REL_TYPE_INSTANTIATED_BY,
    REL_TYPE_DEFINES: REL_TYPE_DEFINED_IN,
    REL_TYPE_DEFINED_IN: REL_TYPE_DEFINES, #REL_TYPE_CONTAINS, # Assuming CONTAINS is the inverse of DEFINED_IN
    REL_TYPE_REFERENCES: REL_TYPE_REFERENCED_BY,
    REL_TYPE_REFERENCED_BY: REL_TYPE_REFERENCES,
    REL_TYPE_DEPENDS_ON: REL_TYPE_DEPENDENCY_OF,
    REL_TYPE_EXPORTS: REL_TYPE_EXPORTED_FROM, # This might need more thought (is EXPORTED_FROM the true inverse of EXPORTS or REEXPORTS?)
    REL_TYPE_EXPORTED_FROM: REL_TYPE_EXPORTS, # Assuming symmetric for now
}

# Add inverse pairs automatically
for key, value in list(RELATIONSHIP_PAIRS.items()):
    if value not in RELATIONSHIP_PAIRS:
        RELATIONSHIP_PAIRS[value] = key

# --- Relationship Properties ---
# Define common property keys used in relationship metadata

# For IMPORTS/REEXPORTS
PROP_IMPORT_ALIAS = "alias"
PROP_IMPORT_IS_STAR = "is_star"
PROP_IMPORT_LINE = "line"
PROP_IMPORT_TYPE = "type" # 'direct', 'from', 'relative'
PROP_IMPORT_LEVEL = "level" # For relative imports

# For CALLS
PROP_CALL_SITE = "call_site" # e.g., file:line
PROP_CALL_ARGS_COUNT = "args_count"
PROP_CALL_KWARGS_COUNT = "kwargs_count"
PROP_CALL_IS_DIRECT = "is_direct" # True if direct call, False if indirect (e.g., via callback)

# For INHERITS/IMPLEMENTS
PROP_INHERITANCE_ORDER = "mro_index" # Position in Method Resolution Order

# For DEFINED_IN
PROP_DEFINITION_LINE = "line"

# For USES
PROP_USAGE_CONTEXT = "context" # e.g., 'annotation', 'assignment', 'call_arg'
PROP_USAGE_LINE = "line"

# --- Node Properties ---
# Define common property keys used in node metadata

PROP_NODE_TYPE = "node_type" # Redundant? Store already handles this. Maybe for export?
PROP_NODE_FILE_PATH = "file_path"
PROP_NODE_LINE_START = "line_start"
PROP_NODE_LINE_END = "line_end"
PROP_NODE_DOCSTRING_EXISTS = "has_docstring"
PROP_NODE_IS_PUBLIC = "is_public"
PROP_NODE_IS_ASYNC = "is_async" # For functions/methods
PROP_NODE_IS_CLASSVAR = "is_classvar" # For variables
PROP_NODE_IS_STATIC = "is_static" # For methods
PROP_NODE_IS_PROPERTY = "is_property" # For methods


# --- Graph Query Constants ---
DIRECTION_OUTGOING = "outgoing"
DIRECTION_INCOMING = "incoming"
DIRECTION_BOTH = "both"


# --- Visualization Constants ---
NODE_COLORS: Dict[str, str] = {
    NODE_TYPE_MODULE: "#cce5ff",
    NODE_TYPE_PACKAGE: "#e5ccff",
    NODE_TYPE_CLASS: "#fff0b3",
    NODE_TYPE_FUNCTION: "#cceeff",
    NODE_TYPE_METHOD: "#d9ead3",
    NODE_TYPE_VARIABLE: "#f3e5f5",
    NODE_TYPE_INTERFACE: "#d1e7dd",
    NODE_TYPE_UNKNOWN: "#e0e0e0",
    NODE_TYPE_EXTERNAL: "#f5f5f5", 
    NODE_TYPE_DECORATOR: "#fadfad",
    NODE_TYPE_PARAMETER: "#eeeeee",
    NODE_TYPE_FILE: "#cccccc",
    NODE_TYPE_ANY: "#ffffff" 
}


RELATIONSHIP_STYLES = {
    REL_TYPE_IMPORTS: {'color': '#888888', 'style': 'dashed', 'arrowhead': 'open'},
    REL_TYPE_INHERITS: {'color': '#0000ff', 'style': 'solid', 'arrowhead': 'empty'},
    REL_TYPE_CALLS: {'color': '#ff0000', 'style': 'solid', 'arrowhead': 'normal'},
    REL_TYPE_IMPLEMENTS: {'color': '#008000', 'style': 'dashed', 'arrowhead': 'empty'},
    REL_TYPE_CONTAINS: {'color': '#ff8c00', 'style': 'dotted', 'arrowhead': 'none'},
    REL_TYPE_REFERENCES: {'color': '#555555', 'style': 'dotted', 'arrowhead': 'vee'},
    REL_TYPE_USES: {'color': '#777777', 'style': 'dotted', 'arrowhead': 'normal'},
    REL_TYPE_DEFINES: {'color': '#006400', 'style': 'solid', 'arrowhead': 'none'}, # DarkGreen
    REL_TYPE_DECORATES: {'color': '#9c27b0', 'style': 'dashed', 'arrowhead': 'curve'}, # Purple
    REL_TYPE_EXPORTS: {'color': '#4caf50', 'style': 'solid', 'arrowhead': 'box'}, # Green
    REL_TYPE_REEXPORTS: {'color': '#8bc34a', 'style': 'dashed', 'arrowhead': 'box'}, # LightGreen
    # Add other styles as needed
}


# --- Validation ---
def validate_node_type(node_type: str) -> bool:
    """Check if a node type is valid."""
    return node_type in NODE_TYPES

def validate_relationship_type(rel_type: str) -> bool:
    """Check if a relationship type is valid."""
    return rel_type in RELATIONSHIP_TYPES

def get_inverse_relationship(rel_type: str) -> Optional[str]:
    """Get the inverse relationship type for a given relationship using the basic RELATIONSHIP_PAIRS."""
    return RELATIONSHIP_PAIRS.get(rel_type)

def get_relationship_style(rel_type: str) -> Dict[str, str]:
    """
    Get the visualization style for a given relationship type.
    Returns a default style if the type is not found.
    """
    return RELATIONSHIP_STYLES.get(rel_type, {'color': '#000000', 'style': 'solid', 'arrowhead': 'normal'})

def get_node_color(node_type: str) -> str:
    """
    Get the color for a given node type.
    Returns a default color if the type is not found.
    """
    return NODE_COLORS.get(node_type, '#d3d3d3') # Default to light gray