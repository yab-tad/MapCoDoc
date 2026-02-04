"""
Module for tracking and analyzing import relationships in Python code using specific edge types and detailed attributes stored in the graph.
"""

import logging
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from code_analysis.graph.store import GraphStore
from code_analysis.graph.traversal import GraphTraversal
from code_analysis.graph.relationships import RelationshipTracker
from code_analysis.graph.models import ImportRecord
from code_analysis.relationship_types import (
    REL_TYPE_IMPORTS, 
    REL_TYPE_NAME_ALIAS, 
    REL_TYPE_MODULE_ALIAS, 
    REL_TYPE_WILDCARD_IMPORT
)

logger = logging.getLogger(__name__)


class ImportTracker(RelationshipTracker):
    """
    Tracks imports between modules in the codebase.
    Inherits from RelationshipTracker to provide general relationship management.
    """

    def __init__(self, store: Optional[GraphStore] = None, traversal: Optional[GraphTraversal] = None):
        """
        Initialize the import tracker.

        Args:
            store: The graph store instance to use. If None, a new one is created.
            traversal: The graph traversal instance to use. If None, a new one is created.
        """
        super().__init__(store=store, traversal=traversal)
        logger.debug("Initialized ImportTracker")


    def add_import(self, record: ImportRecord) -> None:
        """
        Add an import relationship to the graph based on an ImportRecord.
        This method will create a primary IMPORTS relationship and potentially additional relationships for aliases or wildcard imports.

        Args:
            record: An ImportRecord object containing all details of the import.
        """
        
        if not record.importer_module_fqn:
            logger.warning(f"ImportRecord is missing importer_module_fqn. Skipping.")
            return

        # 1. Core Module Dependency (IMPORTS relationship)
        # This edge always goes from the importer to the module it is importing FROM.
        if record.source_module_fqn:
            import_rel_properties = {
                "line": record.line_number,
                "is_internal_source": record.is_source_internal, # Whether the source_module_fqn is internal
                "is_relative": record.is_relative,
                "level": record.level,
                "is_wildcard_statement": record.is_wildcard, # Was this from a 'from X import *' statement?
                
                # Details from the original statement
                "raw_module_specifier": record.raw_module_specifier,
                "raw_imported_name": record.raw_imported_name, # The name text from the statement (e.g., "item", "*")
                "raw_alias": record.raw_alias,
                
                # Resolved FQNs involved
                "imported_entity_fqn": record.imported_entity_fqn, # FQN of the specific item/module directly named
                
                # Effect in importer's scope
                "name_bound_in_importer": record.name_bound_in_importer,
                "name_bound_points_to_fqn": record.name_bound_points_to_fqn,
            }
            
            self.add_relationship(
                source=record.importer_module_fqn,
                target=record.source_module_fqn, # Edge target is the module imported FROM
                relationship_type=REL_TYPE_IMPORTS,
                metadata=import_rel_properties
            )
            log_msg_parts = [
                f"Added IMPORTS: {record.importer_module_fqn} -> {record.source_module_fqn}",
                f"(bound: {record.name_bound_in_importer} -> {record.name_bound_points_to_fqn})"
            ]
            if record.raw_alias: log_msg_parts.append(f"[alias: {record.raw_alias}]")
            if record.is_wildcard: log_msg_parts.append("[wildcard]")
            logger.debug(' '.join(log_msg_parts))

        else:
            # This case implies a failure in CodeVisitor to resolve source_module_fqn
            logger.warning(
                f"ImportRecord for importer '{record.importer_module_fqn}' has no source_module_fqn. "
                f"Raw specifier: '{record.raw_module_specifier}', raw imported name: '{record.raw_imported_name}'. "
                f"Skipping primary IMPORTS edge."
            )

        # 2. Handle Wildcard Imports (REL_TYPE_WILDCARD_IMPORT relationship)
        if record.is_wildcard and record.source_module_fqn:
            self.add_relationship(
                source=record.importer_module_fqn,
                target=record.source_module_fqn, # Importer depends on the wildcard source module
                relationship_type=REL_TYPE_WILDCARD_IMPORT,
                metadata={
                    "line": record.line_number,
                    "is_internal_source": record.is_source_internal
                }
            )
            logger.debug(
                f"Added WILDCARD_IMPORT: {record.importer_module_fqn} imports * from {record.source_module_fqn}"
            )

        # 3. Handle Aliases (REL_TYPE_MODULE_ALIAS or REL_TYPE_NAME_ALIAS)
        if record.raw_alias:
            # The alias 'record.raw_alias' (which is 'record.name_bound_in_importer')
            # points to 'record.name_bound_points_to_fqn'.
            # The target of the alias edge is what the alias points to.

            # Heuristic to distinguish 'import module as m_alias' from 'from module import name as n_alias':
            # In 'import foo.bar as fb':
            #   record.raw_module_specifier == "foo.bar"
            #   record.raw_imported_name == "foo.bar"
            #   record.imported_entity_fqn == "foo.bar" (the module FQN)
            #   record.name_bound_points_to_fqn == "foo.bar"
            # In 'from foo import bar as b':
            #   record.raw_module_specifier == "foo"
            #   record.raw_imported_name == "bar"
            #   record.imported_entity_fqn == "foo.bar" (the item FQN)
            #   record.name_bound_points_to_fqn == "foo.bar"

            if record.raw_module_specifier == record.raw_imported_name and \
               record.imported_entity_fqn == record.name_bound_points_to_fqn and \
               not record.is_relative and not record.is_wildcard : # Checks for 'import x.y.z as alias'
                # This is a REL_TYPE_MODULE_ALIAS
                # The alias 'record.raw_alias' defined in 'importer_module_fqn'
                # makes 'record.raw_alias' point to the module 'record.name_bound_points_to_fqn'.
                self.add_relationship(
                    source=record.importer_module_fqn,    # Module where alias is defined
                    target=record.name_bound_points_to_fqn, # The FQN of the module being aliased
                    relationship_type=REL_TYPE_MODULE_ALIAS,
                    metadata={
                        "alias_name": record.raw_alias,
                        "line": record.line_number,
                        "original_module_fqn": record.imported_entity_fqn # For clarity, this is the target module
                    }
                )
                logger.debug(
                    f"Added MODULE_ALIAS: {record.importer_module_fqn} defines alias "
                    f"'{record.raw_alias}' for module '{record.name_bound_points_to_fqn}'"
                )
            elif not record.is_wildcard: # Should cover 'from module import name as alias'
                # This is a REL_TYPE_NAME_ALIAS
                # The alias 'record.raw_alias' defined in 'importer_module_fqn'
                # makes 'record.raw_alias' point to the item 'record.name_bound_points_to_fqn'.
                self.add_relationship(
                    source=record.importer_module_fqn,    # Module where alias is defined
                    target=record.name_bound_points_to_fqn, # The FQN of the specific item being aliased
                    relationship_type=REL_TYPE_NAME_ALIAS,
                    metadata={
                        "alias_name": record.raw_alias,
                        "original_name_in_source": record.raw_imported_name, # e.g., 'bar' in 'from foo import bar as b'
                        "source_module_fqn": record.source_module_fqn, # e.g., 'foo'
                        "line": record.line_number
                    }
                )
                logger.debug(
                    f"Added NAME_ALIAS: {record.importer_module_fqn} defines alias "
                    f"'{record.raw_alias}' for item '{record.name_bound_points_to_fqn}' "
                    f"(original: {record.raw_imported_name} from {record.source_module_fqn})"
                )
            else:
                # This case (alias with wildcard) should not occur based on Python syntax
                logger.warning(
                    f"Encountered an unexpected alias with wildcard: {record.raw_alias} "
                    f"for wildcard import from {record.source_module_fqn} in {record.importer_module_fqn}"
                )


    def find_imports(self, 
                     importer_module: Optional[str] = None,
                     imported_module_from: Optional[str] = None,
                     name_bound_in_importer_filter: Optional[str] = None,
                     name_bound_points_to_fqn_filter: Optional[str] = None) -> List[Dict]:
        """
        Finds IMPORTS relationships based on search criteria.
        Note: This searches the primary REL_TYPE_IMPORTS edges.

        Args:
            importer_module: The source module of the import (optional filter).
            imported_module_from: The target module (the module imported FROM) (optional filter).
            name_bound_in_importer_filter: Filter by the name bound in the importer's scope (optional).
            name_bound_points_to_fqn_filter: Filter by the FQN the bound name points to (optional).

        Returns:
            List of import relationships (dictionaries) matching the criteria.
        """
        filter_properties = {}
        if name_bound_in_importer_filter is not None:
            filter_properties["name_bound_in_importer"] = name_bound_in_importer_filter
        if name_bound_points_to_fqn_filter is not None:
            filter_properties["name_bound_points_to_fqn"] = name_bound_points_to_fqn_filter
        
        # Ensure properties is None if empty, as expected by find_relationships
        relationships = self.find_relationships(
            relationship_type=REL_TYPE_IMPORTS,
            source=importer_module,
            target=imported_module_from,
            properties=filter_properties if filter_properties else None
        )
        return relationships


    def get_module_imports(self, module_name: str) -> List[Dict]:
        """
        Get all imports originating from a specific module.

        Args:
            module_name: The module to get imports for.

        Returns:
            List of import relationships originating from the module.
        """
        # Use parent class's get_outgoing_relationships method
        # This returns a dict {rel_type: [relationships]}, we need IMPORTS
        outgoing_map = self.get_outgoing_relationships(module_name)
        return outgoing_map.get(REL_TYPE_IMPORTS, [])


    def get_imports_to_module(self, module_name: str) -> List[Dict]:
        """
        Get all imports targeting a specific module.

        Args:
            module_name: The module being imported.

        Returns:
            List of import relationships targeting the module.
        """
        # Use parent class's get_incoming_relationships method
        # This returns a dict {rel_type: [relationships]}, we need IMPORTS
        incoming_map = self.get_incoming_relationships(module_name)
        return incoming_map.get(REL_TYPE_IMPORTS, [])


    def is_raw_name_imported(self, importer_module: str, raw_name_in_statement: str) -> bool: # replaced method: is_name_imported(self, module_name: str, imported_name: str)
        """
        Checks if the given 'raw_name_in_statement' (e.g., "foo" in "from x import foo", or "x.y.z")
        appears as 'raw_imported_name' in any IMPORTS relationship from 'importer_module'.
        """
        relationships = self.find_relationships(
            relationship_type=REL_TYPE_IMPORTS,
            source=importer_module,
            properties={"raw_imported_name": raw_name_in_statement}
        )
        return len(relationships) > 0


    def is_name_bound_by_import(self, importer_module: str, name_in_scope: str) -> bool:
        """
        Checks if 'name_in_scope' is made available (bound) in 'importer_module'
        by any IMPORTS relationship.
        """
        relationships = self.find_relationships(
            relationship_type=REL_TYPE_IMPORTS,
            source=importer_module,
            properties={"name_bound_in_importer": name_in_scope}
        )
        return len(relationships) > 0


    def is_specific_entity_imported(self, importer_module: str, entity_fqn: str) -> bool:
        """
        Checks if the specific 'entity_fqn' is made available (pointed to by a bound name)
        in 'importer_module' by any IMPORTS relationship.
        """
        relationships = self.find_relationships(
            relationship_type=REL_TYPE_IMPORTS,
            source=importer_module,
            properties={"name_bound_points_to_fqn": entity_fqn}
        )
        # Also check if the imported_entity_fqn itself matches, for cases like 'import foo.bar' where foo.bar is the entity.
        # This might be redundant if name_bound_points_to_fqn is comprehensive.
        if relationships:
            return True
        
        relationships_alt = self.find_relationships(
            relationship_type=REL_TYPE_IMPORTS,
            source=importer_module,
            properties={"imported_entity_fqn": entity_fqn}
        )
        return len(relationships_alt) > 0


    def has_import(self, source_module: str, target_module: str) -> bool:
        """
        Check if a module imports from another module.

        Args:
            source_module: The importing module.
            target_module: The imported module.

        Returns:
            True if source imports from target, False otherwise.
        """
        # Use parent class's has_relationship method
        return self.has_relationship(source_module, target_module, REL_TYPE_IMPORTS)


    def get_import_count(self) -> int:
        """
        Get the total number of imports tracked.

        Returns:
            The count of import relationships.
        """
        # Use parent class's get_relationship_count method
        return self.get_relationship_count(REL_TYPE_IMPORTS)


    def get_outgoing_imports(self, module: str) -> List[str]:
        """
        Get a list of all modules that are imported by the specified module.

        Args:
            module: The importing module.

        Returns:
            List of unique imported module names (targets).
        """
        # Use the corrected get_module_imports which uses the parent method
        imports = self.get_module_imports(module)
        # Extract unique target module names
        return list(set(imp.get('target') for imp in imports if imp.get('target')))


    def get_incoming_imports(self, module: str) -> List[str]:
        """
        Get a list of all modules that import the specified module.

        Args:
            module: The imported module.

        Returns:
            List of unique importing module names (sources).
        """
        # Use the corrected get_imports_to_module which uses the parent method
        imports = self.get_imports_to_module(module)
        # Extract unique source module names
        return list(set(imp.get('source') for imp in imports if imp.get('source')))


    def get_import_path(self, name: str, source_module: str, target_module: str) -> List[str]:
        """
        Find the import path from source_module to target_module for a specific name.

        This method follows import relationships to determine how a name from target_module is accessible in source_module (possibly through intermediate modules).
        NOTE: This currently only finds paths between modules, not validating the specific name.

        Args:
            name: The name to trace (currently unused in path finding).
            source_module: The starting module.
            target_module: The module where the name is defined.

        Returns:
            List of modules forming the shortest import path, or empty list if no path found.
        """
        
        if not self._traversal:
            logger.warning("No traversal utility available for finding import paths")
            return []

        # Use the traversal utility to find paths between modules
        # Assuming find_paths exists and works as intended
        if hasattr(self._traversal, 'find_paths'):
            paths = self._traversal.find_paths(source_module, target_module,
                                            edge_types=[REL_TYPE_IMPORTS], max_depth=10)

            if not paths:
                return []

            # Return the shortest path found
            # Ensure paths are lists of nodes before sorting by length
            valid_paths = [p for p in paths if isinstance(p, list)]
            if not valid_paths:
                logger.warning(f"find_paths returned non-list paths: {paths}")
                return []
            shortest_path = sorted(valid_paths, key=len)[0]
            return shortest_path
        else:
            logger.warning("GraphTraversal object missing 'find_paths' method.")
            return []


    def get_unused_imports(self) -> List[Dict[str, Any]]:
        """
        Find all unused imports in the codebase.

        This method requires usage information (e.g., from call graph analysis)
        to determine which imports are unused, which is beyond the scope of the
        import tracker alone.

        Returns:
            List of unused import relationships (placeholder for now).
        """
        # This would require integration with other analyzers (e.g., checking if imported names are used)
        logger.warning("Finding unused imports requires additional context (e.g., usage analysis) and is not implemented solely within ImportTracker.")
        return []


    def get_aliases_for_target(self, target_fqn: str) -> List[Dict]:
        """
        Finds all module or name aliases that point to a given target_fqn.
        """
        module_aliases = self.find_relationships(target=target_fqn, relationship_type=REL_TYPE_MODULE_ALIAS)
        name_aliases = self.find_relationships(target=target_fqn, relationship_type=REL_TYPE_NAME_ALIAS)
        return module_aliases + name_aliases


    def remove_imports_by_module(self, module_fqn: str) -> int:
        """
        Removes all import-related relationships connected to module_fqn.
        This includes IMPORTS, WILDCARD_IMPORT, MODULE_ALIAS, and NAME_ALIAS.
        It removes edges where module_fqn is either the source (importer) or the target (imported_from, or the entity being aliased).

        Args:
            module_fqn: The FQN of the module whose import relationships are to be removed.

        Returns:
            The total number of relationships actually removed.
        """
        
        removed_count = 0
        types_and_roles = [
            (REL_TYPE_IMPORTS, True, True), (REL_TYPE_WILDCARD_IMPORT, True, True),
            (REL_TYPE_MODULE_ALIAS, True, True), (REL_TYPE_NAME_ALIAS, True, False)
        ]
        for rel_type, can_be_source, can_be_target in types_and_roles:
            if can_be_source:
                removed_count += self.remove_relationships(source=module_fqn, relationship_type=rel_type)
            if can_be_target:
                removed_count += self.remove_relationships(target=module_fqn, relationship_type=rel_type)
        logger.info(f"Removed {removed_count} import-related relationships for module {module_fqn}.")
        return removed_count
        
    
    def get_importers_of_module(self, target_module_fqn: str) -> Set[str]:
        """
        Finds all modules that directly import the given target_module_fqn.

        This method looks for REL_TYPE_IMPORTS relationships where the target_module_fqn is the 'target' of the import edge (i.e., the module being imported FROM).

        Args:
            target_module_fqn: The fully qualified name of the module whose importers are to be found.

        Returns:
            A set of FQNs of modules that import the target_module_fqn.
        """
        
        importer_modules: Set[str] = set()
        if not target_module_fqn:
            logger.warning("get_importers_of_module called with empty target_module_fqn.")
            return importer_modules

        # find_relationships returns a list of dictionaries, each representing an edge.
        # The 'source' of these edges will be the importer_module_fqn.
        import_relationships = self.find_relationships(
            relationship_type=REL_TYPE_IMPORTS,
            target=target_module_fqn
            # No source specified, as we are looking for any source importing this target.
            # No specific properties needed for this query, the edge type and target are key.
        )

        for rel in import_relationships:
            importer = rel.get('source')
            if importer:
                importer_modules.add(importer)
            else:
                logger.warning(f"Found an IMPORTS relationship to {target_module_fqn} with no source: {rel}")
        
        if importer_modules:
            logger.debug(f"Found {len(importer_modules)} importers for module {target_module_fqn}: {importer_modules}")
        else:
            logger.debug(f"No direct importers found for module {target_module_fqn}.")
            
        return importer_modules
    