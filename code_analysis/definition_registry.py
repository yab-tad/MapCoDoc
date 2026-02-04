"""
Definition Registry module for authoritative component definition tracking.
Serves as the single source of truth for component definitions across the pipeline.
"""

import time
import logging
import traceback
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Set, List, Tuple, TYPE_CHECKING

from .graph.store import GraphStore
from .feature_flags import Feature, is_enabled
from .relationship_types import REL_TYPE_DEFINED_IN
from .events import DEFINITION_REGISTERED, DEFINITION_UPDATED, DEFINITION_REMOVED

if TYPE_CHECKING:
    from .mapcodocreg import MapCoDocRegistry
    

logger = logging.getLogger(__name__)


@dataclass
class DefinitionInfo:
    """Information about a component's authoritative definition."""
    name: str # Simple name (e.g., 'MyClass', 'my_function')
    module: str # Module where defined (e.g., 'package.submodule')
    fully_qualified_name: str # FQN (e.g., 'package.submodule.MyClass')
    component_type: str
    line_number: int
    source_file: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    package: str = field(init=False)
    
    
    def __post_init__(self):
        if self.module: # If module is not an empty string (e.g. for top-level package itself)
            self.package = self.module.split('.')[0]
        elif '.' in self.fully_qualified_name: # Fallback if module is empty but FQN has parts
            self.package = self.fully_qualified_name.split('.')[0]
        else: # Likely a top-level module/item with no separate package part in its FQN's
            self.package = self.fully_qualified_name # Or None, depending on convention
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            'name': self.name,
            'module': self.module,
            'fully_qualified_name': self.fully_qualified_name,
            'type': self.component_type, # 'type' is common in dicts, but 'component_type' is attr name
            'line_number': self.line_number,
            'source_file': self.source_file,
            'metadata': self.metadata,
            'package': self.package
        }


class DefinitionRegistry:
    """
    Central registry for authoritative component definitions.
    Acts as the single source of truth for where components are actually defined. Uses Fully Qualified Names (FQNs) as primary keys.
    """
    
    COMPONENT_NAME = "definition_registry"

    def __init__(self, registry: Optional['MapCoDocRegistry'] = None):
        """
        Initialize the DefinitionRegistry.

        Args:
            registry: Optional link to the main MapCoDocRegistry for event publishing.
        """
        self.registry = registry
        self._graph_store_instance: Optional['GraphStore'] = None # will be set in initialize
        
        # --- Dynamically declare dependencies ---
        self.DEPENDENCIES: Set[str] = {"config_component"}
        if is_enabled(Feature.GRAPH_ANALYSIS):
            self.DEPENDENCIES.add("graph_store")
            logger.debug(f"[{self.COMPONENT_NAME}] Declared dependency on 'graph_store' because GRAPH_ANALYSIS is enabled.")
        else:
            logger.debug(f"[{self.COMPONENT_NAME}] 'graph_store' is not a dependency because GRAPH_ANALYSIS is disabled.")
        
        
        # Core storage: FQN -> DefinitionInfo
        self.definitions: Dict[str, DefinitionInfo] = {}
        # Indices for faster lookups
        # Module FQN -> Dict[Simple Name -> DefinitionInfo]
        self._definitions_by_module: Dict[str, Dict[str, DefinitionInfo]] = defaultdict(dict)
        # Simple Name -> List[DefinitionInfo] (Handles potential name collisions across modules)
        self._definitions_by_name: Dict[str, List[DefinitionInfo]] = defaultdict(list)
        # Thread lock (kept for safety with potential event handlers)
        self._lock = threading.RLock()
        # Statistics
        self._stats = defaultdict(int)

        logger.info(f"{self.COMPONENT_NAME} initialized.")
    
    
    def initialize(self) -> None:
        """
        Post-registration initialization. Fetches GraphStore if not already set.
        """
        if is_enabled(Feature.GRAPH_ANALYSIS):
            if self.registry:
                try:
                    graph_store_component = self.registry.get_component("graph_store")
                    if graph_store_component and isinstance(graph_store_component, GraphStore): # Check type
                        self._graph_store_instance = graph_store_component
                        logger.info(f"{self.COMPONENT_NAME} obtained GraphStore instance successfully from registry.")
                    elif graph_store_component:
                        logger.error(f"{self.COMPONENT_NAME} received 'graph_store' component, but it's not a GraphStore instance. Type: {type(graph_store_component)}")
                    else:
                        logger.error(f"{self.COMPONENT_NAME} FAILED to obtain 'graph_store' component from registry. This is a critical issue as it's a dependency.")
                except Exception as e:
                    logger.error(f"{self.COMPONENT_NAME} critical error obtaining GraphStore during initialization: {e}", exc_info=True)
            else:
                logger.warning(f"{self.COMPONENT_NAME} has no registry link, cannot obtain GraphStore. This will likely lead to errors.")
            
            logger.info(f"{self.COMPONENT_NAME} component fully initialized by registry. GraphStore available: {self._graph_store_instance is not None}")
    
    
    def _add_to_indices(self, definition_info: DefinitionInfo):
        """Helper to add a definition to secondary lookup indices."""
        if definition_info.module: # Only index by module if module is not empty
            self._definitions_by_module[definition_info.module][definition_info.name] = definition_info
        self._definitions_by_name[definition_info.name].append(definition_info)

    def _remove_from_indices(self, definition_info: DefinitionInfo):
        """Helper to remove a definition from secondary lookup indices."""
        if definition_info.module and definition_info.module in self._definitions_by_module:
            if definition_info.name in self._definitions_by_module[definition_info.module]:
                del self._definitions_by_module[definition_info.module][definition_info.name]
            if not self._definitions_by_module[definition_info.module]:
                del self._definitions_by_module[definition_info.module]
        # Remove from name index
        if definition_info.name in self._definitions_by_name:
            try:
                # Find the specific instance to remove, comparing by FQN for uniqueness
                self._definitions_by_name[definition_info.name] = [
                    d for d in self._definitions_by_name[definition_info.name]
                    if d.fully_qualified_name != definition_info.fully_qualified_name
                ]
                # Clean up empty list for the name
                if not self._definitions_by_name[definition_info.name]:
                    del self._definitions_by_name[definition_info.name]
            except ValueError:
                logger.warning(f"Definition {definition_info.fully_qualified_name} not found in name index '{definition_info.name}' during removal.")
    
    
    def register_definition(self,
                            module_name: str,
                            simple_name: str,
                            fully_qualified_name: str,
                            component_type: str,
                            line_number: int,
                            source_file: Optional[str] = None,
                            metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        Register the authoritative definition of a component.

        Handles potential conflicts and updates existing definitions if confidence is higher.
        Publishes events on registration or update.

        Args:
            module_name: The name of the module where the definition is located.
            simple_name: The node name of the component.
            fully_qualified_name: The unique FQN of the component.
            component_type: Type of the component (e.g., 'class', 'function').
            line_number: Line number where the definition starts.
            source_file: Optional path to the source file.
            metadata: Optional additional metadata.

        Returns:
            True if the definition was registered or updated, False otherwise.
        """
        
        if not fully_qualified_name:
            logger.error("Cannot register definition with empty fully_qualified_name.")
            return False

        if not component_type:
            logger.error(f"Cannot register definition for {fully_qualified_name}: component_type is empty.")
            return False
        
        # Ensure metadata is a dict
        current_metadata = metadata.copy() if metadata else {}
        
        # # Extract name and module from FQN
        # parts = fully_qualified_name.rsplit('.', 1)
        # module_name_for_def: str
        # simple_name_for_def: str

        # if len(parts) == 2:
        #     module_name_for_def, simple_name_for_def = parts
        # else: # Likely a top-level module/item
        #     module_name_for_def = "" # Convention for top-level item's "module"
        #     simple_name_for_def = fully_qualified_name
        #     # logger.debug(f"Interpreting '{fully_qualified_name}' as a top-level item with no explicit module part for DefinitionInfo.")
        
        module_name_for_def = module_name if module_name else ""
        simple_name_for_def = simple_name if simple_name else ""
        
        updated_definition_info = DefinitionInfo(
            name=simple_name_for_def,
            module=module_name_for_def,
            fully_qualified_name=fully_qualified_name,
            component_type=component_type,
            line_number=line_number,
            source_file=source_file,
            metadata=current_metadata
        )

        event_to_publish: Optional[str] = None
        event_data: Dict[str, Any] = {}
        success = False

        with self._lock:
            existing_definition = self.definitions.get(fully_qualified_name)
            is_update = False
            previous_def_dict = None

            if existing_definition:
                is_update = True
                previous_def_dict = existing_definition.to_dict()
                
                # Update existing DefinitionInfo object's fields
                existing_definition.component_type = updated_definition_info.component_type
                existing_definition.line_number = updated_definition_info.line_number
                existing_definition.source_file = updated_definition_info.source_file
                existing_definition.metadata = updated_definition_info.metadata # Overwrite metadata
                # FQN, name, module should not change for an update of the same FQN
                existing_definition.__post_init__() # Recalculate package

                self._stats["definitions_updated"] += 1
                event_to_publish = DEFINITION_UPDATED
                event_data = {"definition": existing_definition.to_dict(), "previous_definition": previous_def_dict}
                logger.debug(f"[{self.COMPONENT_NAME}] Updated definition for: {fully_qualified_name}")
                success = True
            else:
                self.definitions[fully_qualified_name] = updated_definition_info
                self._add_to_indices(updated_definition_info)
                self._stats["definitions_registered"] += 1
                event_to_publish = DEFINITION_REGISTERED
                event_data = {"definition": updated_definition_info.to_dict()}
                logger.debug(f"[{self.COMPONENT_NAME}] Registered new definition for: {fully_qualified_name}")
                success = True
                
            # Add DEFINED_IN edge to GraphStore
            if is_enabled(Feature.GRAPH_ANALYSIS) and success and module_name_for_def: # Only if there's a module to link to
                if self._graph_store_instance:
                    logger.debug(f"[{self.COMPONENT_NAME}] graph_store instance confirmed (type: {type(self._graph_store_instance)}, id: {id(self._graph_store_instance)}) "
                                 f"for adding DEFINED_IN for {fully_qualified_name}.")
                    try:
                        if not self._graph_store_instance.has_node(module_name_for_def):
                            self._graph_store_instance.add_node(module_name_for_def, node_type="module", label=module_name_for_def.split('.')[-1])
                        
                        if not self._graph_store_instance.has_node(fully_qualified_name):
                            self._graph_store_instance.add_node(fully_qualified_name, node_type=component_type, label=simple_name_for_def)

                        logger.debug(f"[{self.COMPONENT_NAME}] Attempting to add DEFINED_IN edge: "
                                     f"Source='{fully_qualified_name}', Target='{module_name_for_def}', "
                                     f"Edge Type (semantic)='{REL_TYPE_DEFINED_IN}', Key='{REL_TYPE_DEFINED_IN}'")
                        
                        edge_added = self._graph_store_instance.add_edge(
                            source=fully_qualified_name,
                            target=module_name_for_def,
                            edge_type=REL_TYPE_DEFINED_IN, # semantic type
                            key=REL_TYPE_DEFINED_IN, # NetworkX key
                            metadata={'description': f'{fully_qualified_name} is defined in {module_name_for_def}'}
                        )
                        if edge_added:
                            logger.debug(f"[{self.COMPONENT_NAME}] SUCCESSFULLY added/updated DEFINED_IN edge: "
                                         f"{fully_qualified_name} -> {module_name_for_def} (Actual Key: {edge_added})")
                        else:
                            logger.warning(f"[{self.COMPONENT_NAME}] FAILED to add/update DEFINED_IN edge for "
                                           f"{fully_qualified_name} -> {module_name_for_def} "
                                           f"(GraphStore.add_edge returned: {edge_added})")
                    except Exception as e_gs:
                        logger.error(f"[{self.COMPONENT_NAME}] Error adding DEFINED_IN to GraphStore for {fully_qualified_name}: {e_gs}", exc_info=True)
                else:
                    logger.error(f"[{self.COMPONENT_NAME}] GraphStore not available to DefinitionRegistry (self._graph_store_instance is None). "
                                 f"Cannot add DEFINED_IN edge for {fully_qualified_name}. "
                                 "This indicates a severe initialization problem or the instance was unset after initialization.")

            if event_to_publish and self.registry and hasattr(self.registry, 'publish_event'):
                try:
                    self.registry.publish_event(event_name=event_to_publish, data=event_data, source_component=self.COMPONENT_NAME)
                except Exception as e_pub:
                    logger.error(f"[{self.COMPONENT_NAME}] Failed to publish {event_to_publish} for {fully_qualified_name}: {e_pub}", exc_info=True)
        return success
    
    
    def get_state(self) -> Dict[str, Any]:
        """Returns a serializable state of the component for checkpointing or synchronization."""
        # For simplicity, just returning counts. A full state would be self.definitions.
        return {"definition_count": len(self.definitions), "stats": self._stats.copy()}

    def sync_state(self, state: Dict[str, Any]) -> bool:
        """
        Synchronizes the component's state from a provided state dictionary.
        This can be complex for DefinitionRegistry; a simple version might just log.
        A full implementation would involve careful merging of definitions.
        """
        logger.warning(f"{self.COMPONENT_NAME} sync_state called. Full state sync might be complex and is currently minimal.")
        if "definition_count" in state: # Just log, not fully restoring
             logger.info(f"Syncing state for {self.COMPONENT_NAME}: Received state with {state['definition_count']} definitions.")
        # To fully sync, you would need to clear existing and repopulate from state['definitions']
        # self.clear_all()
        # for fqn, def_info_dict in state.get('definitions_full_dump', {}).items():
        #     def_info = DefinitionInfo(**def_info_dict) # Assuming def_info_dict matches DefinitionInfo fields
        #     self.definitions[fqn] = def_info
        #     self._add_to_indices(def_info)
        # self._stats = state.get('stats', defaultdict(int))
        return True
    
    def on_dependency_ready(self, dependency_name: str) -> None:
        """Called by the registry when a declared dependency is ready."""
        logger.debug(f"{self.COMPONENT_NAME} notified that dependency '{dependency_name}' is ready.")
        # No specific actions needed here for DefinitionRegistry based on current dependencies.
    
    def get_definition_module(self, name: str, context_module: Optional[str] = None) -> Optional[str]:
        """
        Find the most likely module where a simple name is defined, optionally using context.

        Args:
            name: The simple name of the component (e.g., 'MyClass').
            context_module: The FQN of the module where the lookup is happening (e.g., 'package.user_module').

        Returns:
            The FQN of the module where the name is authoritatively defined, or None if not found or ambiguous without context.
        """
        
        with self._lock:
            self._stats["lookups_by_name"] += 1
            possible_definitions = self._definitions_by_name.get(name, [])

            if not possible_definitions:
                logger.debug(f"No definition found for name: {name}")
                return None

            if len(possible_definitions) == 1:
                # Only one definition exists for this name, return its module
                logger.debug(f"Found unique definition for '{name}' in module: {possible_definitions[0].module}")
                return possible_definitions[0].module

            # Multiple definitions exist, use context if provided
            if context_module:
                # Prefer definition in the exact context module
                for definition in possible_definitions:
                    if definition.module == context_module:
                        logger.debug(f"Found definition for '{name}' in context module: {context_module}")
                        return definition.module

                # Prefer definition in a parent package of the context module
                context_parts = context_module.split('.')
                for i in range(len(context_parts) - 1, 0, -1):
                    parent_package = '.'.join(context_parts[:i])
                    for definition in possible_definitions:
                        if definition.module == parent_package:
                            logger.debug(f"Found definition for '{name}' in parent package: {parent_package}")
                            return definition.module

                # Prefer definition in a sibling module within the same package
                if len(context_parts) > 1:
                    current_package = '.'.join(context_parts[:-1])
                    for definition in possible_definitions:
                        if definition.module.startswith(current_package + '.') and definition.module.count('.') == context_module.count('.'):
                            logger.debug(f"Found definition for '{name}' in sibling module: {definition.module}")
                            return definition.module # Return the first sibling found

            # If context doesn't help or wasn't provided, it's ambiguous
            modules = [d.module for d in possible_definitions]
            logger.warning(f"Ambiguous definition for name '{name}'. Found in modules: {modules}. Context: {context_module}")
            # Optionally, return the first one found as a best guess, or None
            return modules[0] # Best guess, might be wrong
    
    
    def get_all_definition_modules(self, name: str) -> List[str]:
        """Get all modules where a simple name is defined."""
        with self._lock:
            self._stats["lookups_by_name"] += 1
            definitions = self._definitions_by_name.get(name, [])
            return [d.module for d in definitions]


    def get_definition(self, fqn: str) -> Optional[DefinitionInfo]:
        """Get the DefinitionInfo for a specific Fully Qualified Name."""
        with self._lock:
            self._stats["lookups_by_fqn"] += 1
            return self.definitions.get(fqn)


    def get_module_definitions(self, module_fqn: str) -> Dict[str, DefinitionInfo]:
        """Get all definitions within a specific module."""
        with self._lock:
            self._stats["lookups_by_module"] += 1
            return self._definitions_by_module.get(module_fqn, {}).copy()
    
    def get_all_definitions_for_name(self, name: str) -> List[DefinitionInfo]:
        with self._lock:
            self._stats["lookups_by_name"] += 1
            return list(self._definitions_by_name.get(name, []))
        
    def get_all_definitions(self) -> List[DefinitionInfo]:
        with self._lock:
            return list(self.definitions.values())


    def get_fqn_for_component(self, name: str, context_module: Optional[str] = None) -> Optional[str]:
        """
        Resolve a simple name to its authoritative Fully Qualified Name using context.

        Args:
            name: The simple name of the component.
            context_module: The FQN of the module where the lookup is happening.

        Returns:
            The authoritative FQN, or None if not found or ambiguous.
        """
        # Reuse get_definition_module logic to find the most likely module
        definition_module = self.get_definition_module(name, context_module)

        if definition_module is not None: # Allow empty string for top-level module
            # Construct the FQN
            fqn = f"{definition_module}.{name}" if definition_module else name
            # Verify this FQN actually exists in the registry
            if fqn in self.definitions:
                return fqn
            else:
                # This case might happen if get_definition_module made a wrong guess
                logger.warning(f"Resolved module '{definition_module}' for name '{name}', but FQN '{fqn}' not in registry.")
                # Fallback: Check if the name exists directly as FQN (e.g., top-level module name)
                if name in self.definitions:
                    return name
                return None
        else:
            # Check if the name itself is a registered FQN (e.g., a module name)
            if name in self.definitions:
                return name
            return None # Not found or ambiguous
    

    def get_statistics(self) -> Dict[str, Any]:
        """Get registry statistics."""
        with self._lock:
            stats = self._stats.copy()
            stats["total_definitions"] = len(self.definitions)
            stats["tracked_modules_with_defs"] = len(self._definitions_by_module)
            stats["tracked_distinct_names"] = len(self._definitions_by_name)
            return stats

    
    def remove_definitions_by_module(self, module_name: str) -> int:
        """
        Remove all definitions associated with a specific module.

        Args:
            module_name: The FQN of the module whose definitions should be removed.

        Returns:
            The number of definitions removed.
        """
        removed_count = 0
        definitions_to_remove_fqns: List[str] = []
        with self._lock:
            # Find FQNs to remove (safer to collect first then iterate for removal)
            for fqn, def_info in self.definitions.items():
                if def_info.module == module_name:
                    definitions_to_remove_fqns.append(fqn)
            
            if not definitions_to_remove_fqns: return 0

            logger.info(f"Removing {len(definitions_to_remove_fqns)} definitions for module: {module_name}")
            for fqn in definitions_to_remove_fqns:
                if fqn in self.definitions:
                    def_info_to_remove = self.definitions.pop(fqn)
                    self._remove_from_indices(def_info_to_remove)
                    removed_count += 1
                    self._stats["definitions_removed"] += 1
                    
                    if self.registry and hasattr(self.registry, 'publish_event'):
                        # DEFINITION_REMOVED should be imported from .events
                        self.registry.publish_event(
                            DEFINITION_REMOVED, 
                            {"fully_qualified_name": fqn, "definition": def_info_to_remove.to_dict()}, 
                            self.COMPONENT_NAME
                        )
        logger.info(f"Removed {removed_count} definitions for module {module_name}.")
        return removed_count


    def clear_all(self):
        """Cleans up resources used by the component."""
        with self._lock:
            self.definitions.clear()
            self._definitions_by_module.clear()
            self._definitions_by_name.clear()
            self._stats = defaultdict(int)
            logger.info(f"{self.COMPONENT_NAME} cleared all definitions and statistics.")
    