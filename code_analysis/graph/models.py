"""
Dataclasses for representing structured information about graph components and conceptual relationships.

These models define the shape of data. They do not store the graph itself (see GraphStore) nor do they define relationship type string constants (see code_analysis.relationship_types).
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


@dataclass
class ImportRecord:
    """
    Represents a single import operation within a module, capturing its textual form,
    resolved meaning, and effect on the importer's namespace.
    """

    # --- Context of the import operation ---
    importer_module_fqn: str        # Fully qualified name of the module performing the import.
    line_number: Optional[int]      # Line number of the import statement in the source file.

    # --- Original statement components (raw text as parsed from the AST) ---
    # Example: 'from package.module import item as alias'
    #   raw_module_specifier = "package.module"
    #   raw_imported_name    = "item"
    #   raw_alias            = "alias"
    # Example: 'import package.module as pm'
    #   raw_module_specifier = "package.module"
    #   raw_imported_name    = "package.module" (represents the full path imported)
    #   raw_alias            = "pm"
    # Example: 'from .relative import item'
    #   raw_module_specifier = ".relative" (or similar based on exact AST representation for relative path)
    #   raw_imported_name    = "item"
    #   raw_alias            = None

    raw_module_specifier: Optional[str] # The module path text as it appears in the import statement.
                                        # For `from X import ...`, this is X.
                                        # For `import X.Y.Z`, this is X.Y.Z.
                                        # Can be None for `from . import Y` if AST gives None for module.
    raw_imported_name: str              # The name of the item being imported as it appears in the statement.
                                        # For `from X import Y`, this is Y.
                                        # For `from X import *`, this is "*".
                                        # For `import X.Y.Z`, this is X.Y.Z.
    raw_alias: Optional[str]            # The alias if 'as' is used (e.g., "item_alias").

    # --- Nature of the import statement ---
    is_relative: bool                   # True if it's a relative import (e.g., 'from .module ...').
    level: int                          # Relative import level (0 for absolute, 1 for '.', 2 for '..', etc.).
    is_wildcard: bool                   # True for 'from module import *'.

    # --- Resolved Fully Qualified Names (FQN) ---
    # FQN of the module from which 'raw_imported_name' is actually sourced.
    # For 'from X import Y', this is the resolved FQN of X.
    # For 'import X.Y.Z', this is the resolved FQN of X.Y.Z (the module itself is its own source here).
    source_module_fqn: Optional[str]

    # FQN of the specific 'raw_imported_name' after full resolution.
    # For 'from X import Y', this is the resolved FQN of Y.
    # For 'import X.Y.Z', this is the resolved FQN of X.Y.Z.
    # For 'from X import *', this is typically the FQN of X or None.
    imported_entity_fqn: Optional[str]

    is_source_internal: Optional[bool]  # True if 'source_module_fqn' belongs to the project being analyzed.

    # --- Effect of the import in the importer's scope ---
    # The actual name that becomes available/bound in the importer module's namespace.
    # - 'item_alias' for '... import item as item_alias'
    # - 'item' for '... import item'
    # - 'package' for 'import package.module.sub_module'
    name_bound_in_importer: str

    # The FQN that 'name_bound_in_importer' ultimately points to.
    # - FQN of 'item' for '... import item as item_alias'
    # - FQN of 'package.module.sub_module' for 'import package.module.sub_module as pmsm'
    # - FQN of 'package' for 'import package.module.sub_module' (no alias)
    name_bound_points_to_fqn: str
    
    # --- Classification (populated post-parse) ---
    # 'module' | 'package' | 'member'
    imported_is_member: Optional[bool] = None
    imported_is_module: Optional[bool] = None
    imported_is_package: Optional[bool] = None


@dataclass
class ExportDetails:
    """
    Details about a specific name being exported from a module.
    This would correspond to a REL_TYPE_EXPORTS edge.
    """
    exporting_module_fqn: str
    exported_name: str          # The name under which the item is exported
    target_item_fqn: str        # The canonical FQN of the actual item being exported
    is_explicit: bool = False   # True if via __all__, False if implicit (e.g. public top-level items)
    line_number: Optional[int] = None # Line of definition or re-export if applicable


@dataclass
class ExportStep:
    """
    Represents a single step in the export chain of a component.
    Tracks how a component is imported/defined and re-exported at each level.
    """
    module_in_chain_fqn: str    # FQN of the module at this step of the chain
    name_in_module_scope: str   # How the target_item_fqn is known/named within this module_in_chain_fqn
    target_item_fqn: str        # Canonical FQN of the component being traced through the chain (should be constant for a given chain)
    
    # How target_item_fqn (as name_in_module_scope) became available in module_in_chain_fqn:
    # e.g., "defined_locally", "imported_directly", "imported_via_name_alias",
    #       "imported_via_module_alias_access", "imported_via_wildcard"
    availability_mechanism: str
    
    is_explicitly_exported_from_this_module: bool # Whether this module_in_chain_fqn explicitly exports 'name_in_module_scope'


@dataclass
class ChainCandidate:
    """
    Tracks a component that needs an export chain built. Utility for processing.
    """
    component_fqn: str
    priority: int = 0
    reason: str = ""
    is_processed: bool = False

    def __hash__(self):
        return hash(self.component_fqn)


@dataclass
class GraphNodeRepresentation:
    """A structured representation of a node's data from the graph."""
    id: str  # Node identifier (usually FQN or module path)
    node_type: str = "unknown"
    attributes: Dict[str, Any] = field(default_factory=dict)

    def __hash__(self):
        return hash(self.id)


@dataclass
class GraphEdgeRepresentation:
    """A structured representation of an edge's data from the graph."""
    source_id: str
    target_id: str
    edge_type: str # The relationship type string (e.g., "IMPORTS", "CALLS")
    attributes: Dict[str, Any] = field(default_factory=dict)

    def __hash__(self):
        return hash((self.source_id, self.target_id, self.edge_type))
