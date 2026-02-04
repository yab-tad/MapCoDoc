"""
Module for tracking function call relationships in code.

This module provides the CallGraphTracker class that handles tracking and analyzing function calls between different parts of the codebase.
"""

import logging
from collections import deque,defaultdict
from typing import Dict, List, Optional, Set, Tuple, Union, Any

from code_analysis.graph.store import GraphStore
from code_analysis.graph.traversal import GraphTraversal
from code_analysis.graph.relationships import RelationshipTracker
from code_analysis.relationship_types import (
    REL_TYPE_CALLS, RELATIONSHIP_PAIRS
)

logger = logging.getLogger(__name__)


class CallGraphTracker(RelationshipTracker):
    """
    Tracks and analyzes function call relationships in code.
    
    This class is responsible for tracking function calls between different parts of the codebase, including method calls, function calls, and callbacks.
    
    It inherits from RelationshipTracker and specializes in call relationships.
    """
    
    def __init__(self, store: GraphStore = None, traversal: GraphTraversal = None):
        """
        Initialize the CallGraphTracker.
        
        Args:
            store: The graph store to use for tracking, or None to create a new one
            traversal: Optional graph traversal utility
        """
        super().__init__(store, traversal)
        self._function_cache: Dict[str, Any] = {
            "calls": defaultdict(set),  # caller_fqn -> {callee_fqn, ...}
            "called_by": defaultdict(set), # callee_fqn -> {caller_fqn, ...}
            # (caller_fqn, callee_fqn) -> call_info dict (mirroring find_calls output for one representative call)
            "call_edges": {} 
        }
        self._initialize_cache()
        logger.debug("CallGraphTracker initialized and cache populated.")
    
    def _initialize_cache(self):
        """Initialize parts of the function call cache from the store if needed."""
        self._function_cache["calls"].clear()
        self._function_cache["called_by"].clear()
        self._function_cache["call_edges"].clear()

        # Use self.find_calls() to get all call relationships with processed metadata
        # Pass no arguments to find_calls to get all calls.
        try:
            all_calls = self.find_calls() # This will use the refined find_calls
            for call in all_calls:
                caller = call.get('caller')
                callee = call.get('callee')
                if caller and callee:
                    self._function_cache["calls"][caller].add(callee)
                    self._function_cache["called_by"][callee].add(caller)
                    # Store the detailed call_info dict for the (caller, callee) pair.
                    # If multiple distinct calls exist (e.g. different call sites, different keys in multigraph),
                    # this simple cache will only hold one version (likely the first one encountered by find_calls).
                    # For full details of all calls, the graph store is the source of truth.
                    self._function_cache['call_edges'][(caller, callee)] = call
            logger.info(f"CallGraphTracker._function_cache rebuilt: {len(all_calls)} call relationships loaded.")
        except Exception as e:
            logger.error(f"Error during CallGraphTracker._initialize_cache while calling find_calls: {e}", exc_info=True)
        
    
    # def add_call(self, caller: str, callee: str,
    #              call_site: Optional[str] = None,
    #              args_count: Optional[int] = None,
    #              kwargs_count: Optional[int] = None,
    #              is_direct: bool = True,
    #              **additional_attributes: Any) -> bool:
    #     """
    #     Adds a call relationship (and its inverse CALLED_BY) to the graph.

    #     Call-specific details (call_site, args_count, etc.) are stored in a 'metadata'
    #     dictionary on the edge. Other **additional_attributes can be passed to
    #     add_relationship for further processing (e.g., a custom 'key').

    #     Args:
    #         caller: The FQN of the calling function/method.
    #         callee: The FQN of the function/method being called.
    #         call_site: Optional, e.g., "filename:lineno" of the call.
    #         args_count: Optional, number of positional arguments.
    #         kwargs_count: Optional, number of keyword arguments.
    #         is_direct: True if a direct call, False for indirect (e.g., via a decorator).
    #         **additional_attributes: Other attributes to pass to add_relationship.
    #                                  If 'metadata' is in here, it will be merged.
    #                                  If 'key' is in here, it will be used by add_relationship.

    #     Returns:
    #         True if the relationship was added successfully, False otherwise.
    #     """
    #     if not caller or not callee:
    #         logger.warning(f"CallGraphTracker: Incomplete call data: caller={caller}, callee={callee}. Skipping call.")
    #         return False

    #     attributes_for_relationship = additional_attributes.copy()

    #     # Define the call-specific details that should go into 'metadata'.
    #     # Prioritize explicit parameters for these standard fields.
    #     call_specific_details = {}
    #     if call_site is not None:
    #         call_specific_details["call_site"] = call_site
    #     if args_count is not None:
    #         call_specific_details["args_count"] = args_count
    #     if kwargs_count is not None:
    #         call_specific_details["kwargs_count"] = kwargs_count
    #     # is_direct is not Optional, it has a default True.
    #     # It should always be set from the parameter passed to add_call.
    #     call_specific_details["is_direct"] = is_direct

    #     # Merge call_specific_details into the 'metadata' field.
    #     # The passed additional_attributes['metadata'] acts as a base.
    #     # call_specific_details (from explicit params) overwrites keys if they conflict.
        
    #     final_metadata = {}
    #     # Start with metadata from additional_attributes if present and valid
    #     if "metadata" in attributes_for_relationship:
    #         if isinstance(attributes_for_relationship["metadata"], dict):
    #             final_metadata = attributes_for_relationship["metadata"].copy()
    #         else:
    #             logger.warning(
    #                 f"CallGraphTracker: 'metadata' in additional_attributes for call {caller}->{callee} "
    #                 f"was not a dict (type: {type(attributes_for_relationship['metadata'])}). Ignoring it."
    #             )
        
    #     # Update with (and potentially overwrite with) call_specific_details from explicit params
    #     final_metadata.update(call_specific_details)
    #     attributes_for_relationship["metadata"] = final_metadata
        
    #     logger.debug(
    #         f"CallGraphTracker: Delegating to add_relationship for call: {caller} -> {callee}. "
    #         f"Relationship Type: {REL_TYPE_CALLS}, Attributes for Relationship: {attributes_for_relationship}"
    #     )

    #     success = self.add_relationship(
    #         source=caller,
    #         target=callee,
    #         relationship_type=REL_TYPE_CALLS,
    #         **attributes_for_relationship
    #     )

    #     if success:
    #         logger.debug(f"CallGraphTracker: Added call from {caller} to {callee}")
    #         # Update internal cache
    #         if 'calls' not in self._function_cache: self._function_cache['calls'] = defaultdict(set)
    #         if 'called_by' not in self._function_cache: self._function_cache['called_by'] = defaultdict(set)
            
    #         self._function_cache['calls'][caller].add(callee)
    #         self._function_cache['called_by'][callee].add(caller)
    #     else:
    #         logger.error(f"CallGraphTracker: Failed to add call from {caller} to {callee} via add_relationship.")

    #     return success
    
    
    def add_call(self, caller: str, callee: str,
                 call_site: Optional[str] = None,
                 args_count: Optional[int] = None,
                 kwargs_count: Optional[int] = None,
                 is_direct: bool = True,
                 metadata: Optional[Dict[str, Any]] = None, # For DFA, etc.
                 **additional_attributes: Any # For other top-level attributes if needed
                 ) -> bool:
        """
        Adds a call relationship to the graph.

        Args:
            caller: FQN of the calling function/method.
            callee: FQN of the called function/method.
            call_site: Location of the call (e.g., "file.py:line_number").
            args_count: Number of positional arguments.
            kwargs_count: Number of keyword arguments.
            is_direct: True if the call is direct, False for indirect (e.g., via a decorator).
            metadata: Additional metadata, often from DFA (e.g., argument details).
            **additional_attributes: Other attributes to store at the top-level of the edge.

        Returns:
            True if the call relationship was successfully added, False otherwise.
        """
        if not caller or not callee:
            logger.warning(f"Attempted to add call with missing caller ('{caller}') or callee ('{callee}')")
            return False

        # Consolidate all call-specific details into a 'metadata' dictionary.
        # Start with a copy of the passed 'metadata' to avoid modifying the original dict.
        call_specific_metadata = metadata.copy() if metadata else {}

        # Explicit parameters override or set values within this call_specific_metadata.
        if call_site is not None:
            call_specific_metadata['call_site'] = call_site
        if args_count is not None:
            call_specific_metadata['args_count'] = args_count
        if kwargs_count is not None:
            call_specific_metadata['kwargs_count'] = kwargs_count
        
        # 'is_direct' is also included in the metadata dict for completeness and easy access
        # if one just has the metadata blob.
        call_specific_metadata['is_direct'] = is_direct

        # Prepare attributes to be stored at the top-level of the edge by RelationshipTracker.
        # This allows for easier filtering on commonly queried attributes like 'is_direct'.
        top_level_attributes_for_edge = {
            "is_direct": is_direct,  # Promoted for direct filtering
            "metadata": call_specific_metadata  # Nested dictionary with all other details
        }
        
        # Merge any other explicitly passed **additional_attributes into the top-level.
        if additional_attributes:
            top_level_attributes_for_edge.update(additional_attributes)
        
        logger.debug(
            f"Attempting to add call: {caller} -> {callee} "
            f"(is_direct={is_direct}, call_site={call_site}). "
            f"Metadata bundle: {call_specific_metadata}. "
            f"Top-level for store: {top_level_attributes_for_edge}"
        )

        # Add the relationship using the parent class method.
        # RelationshipTracker.add_relationship will pass **top_level_attributes_for_edge to GraphStore.add_edge, making 'is_direct' and 'metadata' top-level edge attributes.
        # We explicitly use REL_TYPE_CALLS as the NetworkX key for these call edges.
        success = self.add_relationship(
            source=caller,
            target=callee,
            relationship_type=REL_TYPE_CALLS,
            key=REL_TYPE_CALLS, 
            **top_level_attributes_for_edge
        )

        if success:
            logger.debug(f"Successfully added call relationship: {caller} -> {callee}")
            # Update the internal quick-access cache (_function_cache)
            self._function_cache["calls"][caller].add(callee)
            self._function_cache["called_by"][callee].add(caller)

            # Construct a call_info-like dictionary for the _function_cache['call_edges']
            # This mirrors the structure returned by find_calls for consistency.
            edge_data_for_cache = {
                'caller': caller,
                'callee': callee,
                'call_site': call_specific_metadata.get('call_site'),
                'args_count': call_specific_metadata.get('args_count'),
                'kwargs_count': call_specific_metadata.get('kwargs_count'),
                'is_direct': is_direct, # The top-level value
                'dfa_metadata': call_specific_metadata # The consolidated metadata
            }
            self._function_cache['call_edges'][(caller, callee)] = edge_data_for_cache
            logger.debug(f"Updated _function_cache['call_edges'] for ({caller}, {callee})")
        else:
            logger.warning(f"Failed to add call relationship: {caller} -> {callee}")

        # Clear relevant caches in RelationshipTracker and CallGraphTracker
        self._clear_caches(caller, callee, relationship_type_affected=REL_TYPE_CALLS)
        return success
    
    
    # def find_calls(self, caller: Optional[str] = None, callee: Optional[str] = None,
    #               is_direct: Optional[bool] = None,
    #               properties: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    #     """
    #     Find call relationships.
    #     'properties' and 'is_direct' are matched against the *inner metadata* of the call edge.
        
    #     Args:
    #         caller: Optional caller to filter by
    #         callee: Optional callee to filter by
    #         is_direct: Whether to filter for direct calls only
    #         properties: Optional dictionary of additional properties to match
            
    #     Returns:
    #         List of dictionaries containing call information
    #     """
    #     search_metadata_props = properties.copy() if properties else {}
    #     if is_direct is not None:
    #         search_metadata_props['is_direct'] = is_direct
        
    #     # find_relationships returns a list of edge data dictionaries.
    #     # Each dict has 'source', 'target', 'edge_type', and 'properties' (the full edge attr dict).
    #     candidate_edges_data = self.find_relationships(
    #         relationship_type=REL_TYPE_CALLS,
    #         source=caller,
    #         target=callee
    #         # Cannot directly filter by sub-dictionary 'metadata' content here,
    #         # so we filter after getting candidate edges.
    #     )
        
    #     calls = []
    #     for edge_data_dict in candidate_edges_data:
    #         # The 'properties' key from find_relationships holds the actual edge attributes from the store
    #         full_edge_attributes = edge_data_dict.get('properties', {})
    #         # The call-specific metadata we constructed is nested under 'metadata' within these attributes
    #         actual_call_metadata = full_edge_attributes.get('metadata', {})

    #         match = True
    #         if search_metadata_props:
    #             for key, value in search_metadata_props.items():
    #                 if actual_call_metadata.get(key) != value:
    #                     match = False
    #                     break
            
    #         if match:
    #             calls.append({
    #                 'caller': edge_data_dict['source'],
    #                 'callee': edge_data_dict['target'],
    #                 'call_site': actual_call_metadata.get('call_site'),
    #                 'args_count': actual_call_metadata.get('args_count'),
    #                 'kwargs_count': actual_call_metadata.get('kwargs_count'),
    #                 'is_direct': actual_call_metadata.get('is_direct', True), # Default to True if not in metadata
    #                 'metadata': actual_call_metadata # Return the inner metadata dict
    #             })
        
    #     return calls
    
    
    def find_calls(self, caller: Optional[str] = None, callee: Optional[str] = None,
                   is_direct: Optional[bool] = None) -> List[Dict[str, Any]]:
        """
        Finds call relationships in the graph.

        Args:
            caller: Optional FQN of the calling function/method.
            callee: Optional FQN of the called function/method.
            is_direct: Optional boolean to filter by direct or indirect calls.

        Returns:
            A list of dictionaries, each representing a call and its details.
        """
        properties_to_match = {}
        if is_direct is not None:
            properties_to_match['is_direct'] = is_direct

        candidate_edges_data = self.find_relationships(
            relationship_type=REL_TYPE_CALLS,
            source=caller,
            target=callee,
            properties=properties_to_match if properties_to_match else None
        )

        results = []
        for edge_data in candidate_edges_data:
            # 'metadata' is expected to be a dictionary stored as a top-level attribute on the edge.
            metadata_from_edge = edge_data.get('metadata', {}) # Get the dict stored under 'metadata' key
            
            call_info = {
                'caller': edge_data.get('source'),
                'callee': edge_data.get('target'),
                'call_site': metadata_from_edge.get('call_site'),
                'args_count': metadata_from_edge.get('args_count'),
                'kwargs_count': metadata_from_edge.get('kwargs_count'),
                'is_direct': edge_data.get('is_direct'), # From top-level edge attribute
                'metadata': metadata_from_edge # Use 'metadata' as the key here
            }
            results.append(call_info)
        
        logger.debug(f"find_calls query (caller='{caller}', callee='{callee}', is_direct={is_direct}) found {len(results)} results.")
        return results
    
    
    def get_outgoing_calls(self, function: str) -> List[str]:
        """
        Get list of functions called by the specified function.
        
        Args:
            function: Function to check outgoing calls for
            
        Returns:
            List of functions that are called by the specified function
        """
        if not function:
            logger.warning("Attempted to get outgoing calls for empty function name")
            return []
        
        # Get all outgoing relationships of type CALLS
        outgoing = self.get_outgoing_relationships(function) # This calls RelationshipTracker.get_outgoing_relationships
        call_relationships = outgoing.get(REL_TYPE_CALLS, [])
        
        # Extract unique target functions
        functions = list(set(rel['target'] for rel in call_relationships))
        return functions
    
    
    def get_incoming_calls(self, function: str) -> List[str]:
        """
        Get list of functions that call the specified function.
        
        Args:
            function: Function to check incoming calls for
            
        Returns:
            List of functions that call the specified function
        """
        if not function:
            logger.warning("Attempted to get incoming calls for empty function name")
            return []
        
        # Get all incoming relationships of type CALLS
        incoming = self.get_incoming_relationships(function)
        call_relationships = incoming.get(REL_TYPE_CALLS, [])
        
        # Extract unique source functions
        functions = list(set(rel['source'] for rel in call_relationships))
        return functions
    
    def is_function_called(self, function: str) -> bool:
        """
        Check if a function is called by any other function.
        
        Args:
            function: Function to check
            
        Returns:
            True if the function is called, False otherwise
        """
        if not function:
            logger.warning("Attempted to check if function is called with empty function name")
            return False
        
        # Check if there are any incoming CALLS relationships
        incoming = self.get_incoming_relationships(function)
        return bool(incoming.get(REL_TYPE_CALLS, []))
    
    def get_call_count(self) -> int:
        """
        Get the number of call relationships tracked.
        
        Returns:
            Total number of call relationships
        """
        return self.get_relationship_count(REL_TYPE_CALLS)
    
    def get_call_path(self, caller: str, callee: str) -> List[str]:
        """
        Find the call path from caller to callee.
        
        Args:
            caller: Starting function
            callee: Target function
            
        Returns:
            List of functions in the call path, or empty list if no path exists
        """
        if not caller or not callee:
            logger.warning("Attempted to get call path with incomplete data")
            return []
        
        # Use traversal to find shortest path
        if not self.traversal:
            logger.warning("No traversal utility available for path finding")
            return []
        
        path = self.traversal.find_shortest_path(
            caller, 
            callee,
            edge_type=REL_TYPE_CALLS
        )
        
        return path
    
    
    def get_recursive_functions(self) -> List[str]:
        """
        Get list of functions that call themselves directly by querying the store.
        This avoids relying on the potentially incomplete cache built during initialization.

        Returns:
            List of recursive functions identified by having a CALLS edge to themselves.
        """
        logger.debug("Querying store for recursive functions (type=CALLS self-loops)...")
        recursive = set()
        
        # Query the store directly for CALLS relationships
        # This ensures we get the current state, regardless of when _initialize_cache ran.
        all_calls = self.find_relationships(relationship_type=REL_TYPE_CALLS)
        
        for call in all_calls:
            caller = call.get("source")
            callee = call.get("target")
            # Check if it's a self-loop
            if caller and caller == callee:
                recursive.add(caller)
                
        logger.debug(f"Found recursive functions by direct query: {recursive}")
        return list(recursive)
    
    
    def get_call_depth(self, function: str) -> int:
        """
        Get the maximum depth of calls for a function (longest shortest path from this function)
        
        Depth 0 if function calls nothing or doesn't exist.
        Depth 1 if A calls B, and B calls nothing.
        
        Args:
            function: The starting function
            
        Returns:
            Maximum depth of the call tree
        """
        
        if not function or not self.store.has_node(function):
            logger.debug(f"Call depth for non-existent/empty function '{function}': 0")
            return 0
        
        q = deque([(function, 0)])  # Store (node, depth)
        # visited_bfs is crucial to handle cycles and prevent re-processing nodes in a way that would distort simple path depth or lead to infinite loops
        visited_bfs = {function} 
        max_d = 0

        while q:
            curr, d = q.popleft()
            max_d = max(max_d, d) # Update max depth found

            # get_outgoing_calls should provide direct callees
            callees = self.get_outgoing_calls(curr)

            for neighbor in callees:
                if neighbor not in visited_bfs:
                    visited_bfs.add(neighbor)
                    q.append((neighbor, d + 1))
        
        logger.debug(f"Calculated call depth for '{function}': {max_d}")
        return max_d

    
    def remove_calls_by_module(self, module_name: str) -> int:
        """
        Removes all call relationships involving functions/methods defined
        within the specified module.

        Args:
            module_name: The fully qualified name of the module.

        Returns:
            The number of relationships removed.
        """
        if not module_name:
            logger.warning("Attempted to remove calls for empty module name.")
            return 0

        logger.debug(f"Removing call relationships involving module: {module_name}")
        removed_count = 0
        prefix = module_name + "."

        # Similar to inheritance, iterate through all call edges
        all_calls = self.find_relationships(relationship_type=REL_TYPE_CALLS)

        rels_to_remove = []
        for rel in all_calls:
            caller = rel['source']
            callee = rel['target']
            # Check if either caller or callee belongs to the module
            # Need to handle nested classes/functions correctly if FQN includes them
            # Simple prefix check might be sufficient if FQNs are consistent
            if caller.startswith(prefix) or callee.startswith(prefix):
                 rels_to_remove.append((caller, callee))

        for caller, callee in rels_to_remove:
             if self.remove_relationship(caller, callee, REL_TYPE_CALLS):
                 removed_count += 1
             # Handle inverse CALLED_BY if necessary

        # Clear relevant internal caches if any
        # Example: self._function_cache = {k: v for k, v in self._function_cache.items() if not k.startswith(prefix)}
        logger.info(f"Removed {removed_count} call relationships involving {module_name}.")
        return removed_count 

    # def _clear_caches(self, source: Optional[str] = None, target: Optional[str] = None):
    #     """Clear relevant call graph caches."""
    #     super()._clear_caches(source, target) # Call parent cache clearing
    #     # Clear specific call graph cache entries
    #     if source and source in self._function_cache:
    #         # If source is modified, its outgoing calls might change
    #         del self._function_cache[source]
    #     # If target is modified, callers might change, but our cache is caller->callee
    #     # A full rebuild might be safer on target changes, or track inverse relationships
    #     # For now, just clearing the source cache entry. Consider clearing all if target changes.
    #     # if target:
    #     #     # Need to find all callers of target to clear their cache entries
    #     #     pass 
    
    
    def _clear_caches(self, 
                      source: Optional[str] = None, 
                      target: Optional[str] = None,
                      relationship_type_affected: Optional[str] = None) -> None:
        """Clear CallGraphTracker specific caches and then call super to clear RelationshipTracker caches."""
        
        # Clear CallGraphTracker's own _function_cache entries if affected.
        # This is more targeted than a full _initialize_cache() on every modification.
        cache_cleared_locally = False
        if source and target and relationship_type_affected == REL_TYPE_CALLS:
            if source in self._function_cache["calls"] and target in self._function_cache["calls"][source]:
                self._function_cache["calls"][source].remove(target)
                if not self._function_cache["calls"][source]:
                    del self._function_cache["calls"][source]
                cache_cleared_locally = True
            
            if target in self._function_cache["called_by"] and source in self._function_cache["called_by"][target]:
                self._function_cache["called_by"][target].remove(source)
                if not self._function_cache["called_by"][target]:
                    del self._function_cache["called_by"][target]
                cache_cleared_locally = True
            
            edge_key_tuple = (source, target)
            if edge_key_tuple in self._function_cache["call_edges"]:
                del self._function_cache["call_edges"][edge_key_tuple]
                cache_cleared_locally = True
        
        if cache_cleared_locally:
            logger.debug(f"CallGraphTracker._function_cache selectively updated for source='{source}', target='{target}'.")
        
        # Now call the superclass's _clear_caches to handle RelationshipTracker's _relationship_cache.
        # This is crucial as find_relationships (and thus find_calls) relies on it.
        super()._clear_caches(source, target, relationship_type_affected=relationship_type_affected)
        
        logger.debug(f"CallGraphTracker _clear_caches called for source: {source}, target: {target}, type: {relationship_type_affected}")

        
    def get_edge_data(self, caller: str, callee: str) -> Optional[Dict[str, Any]]:
        """Helper to get edge data for a CALLS relationship."""
        # GraphStore.get_edges yields (source, target, key, data_dict)
        # We want the attributes for the specific CALLS edge from caller to callee.
        edges_generator = self.store.get_edges(source=caller, target=callee, edge_type=REL_TYPE_CALLS)
        
        # Consume the generator to get the first (and hopefully only) edge
        # If multiple CALLS edges could exist between caller and callee (e.g. different keys),
        # this will only return the data of the first one found.
        try:
            first_edge = next(edges_generator)
            # first_edge is (u, v, k, data). We need data.
            return first_edge[3] 
        except StopIteration:
            # No edge found
            return None
    
    
    def _get_module_from_entity_fqn(self, entity_fqn: str) -> Optional[str]:
        """
        Extracts the module FQN from a component's FQN.
        Assumes FQNs are like module.submodule.component_name or module.submodule.Class.method.
        This is a heuristic and might need refinement or DefinitionRegistry access for full accuracy.
        """
        if not entity_fqn:
            return None
        
        parts = entity_fqn.split('.')
        if not parts:
            return None

        # Try to find a node in the graph store that matches a prefix of the FQN
        # and is a module or package. This is more robust than simple string splitting.
        # This requires GraphStore to be aware of node types.
        if self.store:
            for i in range(len(parts), 0, -1):
                potential_module_fqn = ".".join(parts[:i])
                node_data = self.store.get_node_attributes(potential_module_fqn)
                if node_data:
                    node_type = node_data.get('node_type')
                    if node_type == 'module' or node_type == 'package':
                        return potential_module_fqn
        
        # Fallback heuristic if store check fails or store is not available
        # This is less reliable.
        # If 'MyClass.my_method', parts = ['MyClass', 'my_method'], we want to avoid returning 'MyClass'
        # If 'pkg.mod.MyClass.my_method', parts = ['pkg','mod','MyClass','my_method'], we want 'pkg.mod'
        # If 'pkg.mod.my_func', parts = ['pkg','mod','my_func'], we want 'pkg.mod'
        # A common pattern: if the second to last part is uppercase (likely class), go one more up.
        if len(parts) > 1:
            if len(parts) > 2 and parts[-2][0].isupper(): # Likely Class.method
                return ".".join(parts[:-2]) if len(parts[:-2]) > 0 else None
            else: # Likely module.function or module.Class
                return ".".join(parts[:-1]) if len(parts[:-1]) > 0 else None
        return None # Cannot determine module for single-part FQN like a global builtin

    
    def get_modules_calling_target_module(self, target_module_fqn: str) -> Set[str]:
        """
        Finds all modules that contain functions/methods calling any entity
        defined within the target_module_fqn or its sub-packages/modules.

        Args:
            target_module_fqn: The FQN of the module whose callers are sought.

        Returns:
            A set of FQNs of modules containing callers.
        """
        calling_modules: Set[str] = set()
        if not target_module_fqn:
            logger.warning("get_modules_calling_target_module called with empty target_module_fqn.")
            return calling_modules

        all_calls = self.find_relationships(relationship_type=REL_TYPE_CALLS)

        for call_rel in all_calls:
            caller_entity_fqn = call_rel.get('source')
            callee_entity_fqn = call_rel.get('target')

            if not caller_entity_fqn or not callee_entity_fqn:
                continue

            # Determine the module of the callee
            # This is the critical part: does callee_entity_fqn belong to target_module_fqn?
            # A simple check is if callee_entity_fqn starts with target_module_fqn + "."
            # or if callee_entity_fqn is exactly target_module_fqn (if target is a callable module itself).
            
            is_callee_in_target_module_or_subpackage = False
            if callee_entity_fqn == target_module_fqn: # Target module itself is callable (e.g. __call__ on module object)
                 is_callee_in_target_module_or_subpackage = True
            elif callee_entity_fqn.startswith(target_module_fqn + "."):
                 is_callee_in_target_module_or_subpackage = True
            # Alternative: Use _get_module_from_entity_fqn if it's robust
            # callee_module = self._get_module_from_entity_fqn(callee_entity_fqn)
            # if callee_module and (callee_module == target_module_fqn or callee_module.startswith(target_module_fqn + ".")):
            #    is_callee_in_target_module_or_subpackage = True


            if is_callee_in_target_module_or_subpackage:
                caller_module = self._get_module_from_entity_fqn(caller_entity_fqn)
                if caller_module:
                    calling_modules.add(caller_module)
        
        if calling_modules:
            logger.debug(f"Found {len(calling_modules)} modules calling entities in/under {target_module_fqn}: {calling_modules}")
        else:
            logger.debug(f"No modules found calling entities in/under {target_module_fqn}.")

        return calling_modules
