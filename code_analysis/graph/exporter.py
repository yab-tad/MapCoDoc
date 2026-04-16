"""
Module for tracking export relationships in code.
"""

import logging
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from code_analysis.graph.store import GraphStore
from code_analysis.graph.traversal import GraphTraversal
from code_analysis.graph.relationships import RelationshipTracker
from code_analysis.relationship_types import REL_TYPE_EXPORTS

logger = logging.getLogger(__name__)


class ExportTracker(RelationshipTracker):
    """
    Tracks how components are exported or re-exported from modules.

    Uses a single edge type REL_TYPE_EXPORTS with attributes to indicate details.
    Edge: ExportingModuleFQN --[REL_TYPE_EXPORTS]--> TargetComponentFQN
    Attributes include:
        - exported_name: (str) The name under which the target is exported.
        - is_explicit: (bool) True if exported via __all__ or similar mechanism.
        - is_reexport: (bool) True if the target component was imported into the exporting module.
        - (Optional) line_number: (int) Line where the export occurs (e.g., in __all__ or definition).
        - (Optional) import_details: (Dict) If is_reexport=True, details about the original import.
    """

    def __init__(self, store: Optional[GraphStore] = None, traversal: Optional[GraphTraversal] = None):
        """
        Initialize the ExportTracker.

        Args:
            store: The graph store to use for tracking. If None, a new one is created.
            traversal: Optional graph traversal utility. If None, a new one is created.
        """
        super().__init__(store, traversal)
        logger.debug("ExportTracker initialized (using single EXPORTS type)")

    
    def add_export(self,
                   exporting_module_fqn: str,
                   target_component_fqn: str,
                   exported_name: str,
                   is_explicit: bool,
                   is_reexport: bool,
                   metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        Adds an export relationship to the graph.

        Args:
            exporting_module_fqn: The FQN of the module performing the export.
            target_component_fqn: The canonical FQN of the component being exported.
            exported_name: The name under which the component is exported from this module.
            is_explicit: True if this export is explicit (e.g., via __all__).
            is_reexport: True if the exported component was imported into this module.
            metadata: Optional dictionary of additional properties (e.g., line_number).

        Returns:
            True if the relationship was added successfully, False otherwise.
        """
        if not exporting_module_fqn or not target_component_fqn or not exported_name:
            logger.warning(f"Attempted to add export with missing info: "
                           f"Module='{exporting_module_fqn}', "
                           f"Target='{target_component_fqn}', "
                           f"ExportedName='{exported_name}'")
            return False

        properties = metadata or {}
        properties.update({
            "exported_name": exported_name,
            "is_explicit": is_explicit,
            "is_reexport": is_reexport
        })

        logger.debug(f"Adding/Updating EXPORT: {exporting_module_fqn} --[EXPORT(name={exported_name}, explicit={is_explicit}, reexport={is_reexport})]--> {target_component_fqn}")

        # The key for an export relationship must be unique for each exported name from a given module, even if they point to the same target (less common, but possible)
        # We will use a composite key.
        export_key = f"EXPORT_{exported_name}"
        
        return self.add_relationship(
            source=exporting_module_fqn,
            target=target_component_fqn,
            relationship_type=REL_TYPE_EXPORTS,
            key=export_key,  # pass the unique key for this specific export
            metadata=properties  # pass the combined, structured properties
        )


   # --- Methods for querying exports (adapt to use only REL_TYPE_EXPORTS and properties) ---

    def find_exports_from_module(self, module_fqn: str, is_explicit: Optional[bool] = None, is_reexport: Optional[bool] = None) -> List[Dict[str, Any]]:
        """
        Find all export relationships originating from a module, with optional filters.

        Args:
            module_fqn: The FQN of the exporting module.
            is_explicit: Optional filter for explicit exports.
            is_reexport: Optional filter for re-exports.

        Returns:
            List of export relationship dictionaries.
        """
        property_filters = {}
        if is_explicit is not None:
            property_filters["is_explicit"] = is_explicit
        if is_reexport is not None:
            property_filters["is_reexport"] = is_reexport

        # Use find_relationships from parent, searching only REL_TYPE_EXPORTS
        return self.find_relationships(
            source=module_fqn,
            rel_type=REL_TYPE_EXPORTS,
            properties=property_filters if property_filters else None
        )

    
    def find_modules_exporting(self, component_fqn: str, is_explicit: Optional[bool] = None, is_reexport: Optional[bool] = None) -> List[Dict[str, Any]]:
        """
        Find all modules that export a given component, with optional filters.

        Args:
            component_fqn: The FQN of the component being exported.
            is_explicit: Optional filter for explicit exports.
            is_reexport: Optional filter for re-exports.

        Returns:
            List of export relationship dictionaries where the target matches component_fqn.
        """
        property_filters = {}
        if is_explicit is not None:
            property_filters["is_explicit"] = is_explicit
        if is_reexport is not None:
            property_filters["is_reexport"] = is_reexport

        # Use find_relationships from parent, searching only REL_TYPE_EXPORTS
        return self.find_relationships(
            target=component_fqn,
            rel_type=REL_TYPE_EXPORTS,
            properties=property_filters if property_filters else None
        )

    
    def remove_exports_by_module(self, module_fqn: str) -> int:
        """
        Removes all EXPORT relationships originating from or targeting a module.

        Args:
            module_fqn: The FQN of the module whose exports should be removed.

        Returns:
            The total number of relationships removed.
        """
        
        logger.debug(f"Removing EXPORTS relationships associated with module: {module_fqn}")
        removed_count = 0
        # Exports are typically where module_fqn is the source
        removed_count += self.remove_relationships(source=module_fqn, relationship_type=REL_TYPE_EXPORTS)
        
        # If module_fqn can also be a target (e.g. a module itself is the target_component_fqn of an export)
        # This is less common for typical export semantics where target is a specific item.
        # If this is a valid scenario for your system:
        # removed_count += self.remove_relationships(target=module_fqn, relationship_type=REL_TYPE_EXPORTS)
        
        logger.info(f"Removed {removed_count} EXPORT relationships for module {module_fqn}.")
        return removed_count
    