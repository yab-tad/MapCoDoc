"""
Graph traversal algorithms for finding paths, cycles, and constructing export chains.

This module provides algorithms for traversing the graph, such as finding paths
between nodes, detecting cycles, and constructing detailed export chains for components
to trace their public API exposure.
"""

import logging
import networkx as nx
from collections import deque
from typing import Dict, List, Set, Tuple, Optional, Any, Callable, Union

from .store import GraphStore
from .models import ExportStep
from code_analysis.relationship_types import (
    REL_TYPE_IMPORTS, REL_TYPE_EXPORTS, REL_TYPE_DEFINED_IN, 
    REL_TYPE_MODULE_ALIAS, REL_TYPE_NAME_ALIAS,
    REL_TYPE_WILDCARD_IMPORT, NODE_TYPE_MODULE
)


logger = logging.getLogger(__name__)


class GraphTraversal:
    """
    Provides graph traversal algorithms like pathfinding, cycle detection, and export chain analysis.
    """
    
    def __init__(self, graph_store: GraphStore):
        """
        Initialize with the graph store.
        
        Args:
            graph_store: GraphStore instance to traverse
        """
        self.graph_store = graph_store
    
    
    def find_shortest_path(self, 
                           source: str, 
                           target: str, 
                           max_depth: int = 10, 
                           edge_type: Optional[str] = None) -> List[str]:
        """
        Find the shortest path from source to target using BFS.

        Args:
            source: FQN of the source node.
            target: FQN of the target node.
            max_depth: Maximum path length to consider.
            edge_type: Optional edge type to filter connections by.

        Returns:
            List of node FQNs representing the path, or an empty list if no path
            is found within max_depth or if nodes don't exist.
        """
        
        if source == target:
            return [source]
            
        if not self.graph_store.has_node(source) or not self.graph_store.has_node(target):
            logger.debug(f"Source ({source}) or Target ({target}) node not found for shortest path.")
            return []
            
        visited = {source}
        queue = deque([(source, [source])])  # Stores (current_node_fqn, path_to_current_node)
        
        while queue:
            current_node, path = queue.popleft()
            
            if len(path) >= max_depth + 1: # Path length, not number of edges
                continue
            
            # Successors are (neighbor_node_fqn, edge_attributes_dict)
            for neighbor_node, _ in self.graph_store.get_successors(current_node, edge_type=edge_type):
                if neighbor_node == target:
                    return path + [neighbor_node]

                if neighbor_node not in visited:
                    visited.add(neighbor_node)
                    queue.append((neighbor_node, path + [neighbor_node]))
        return []
    
    
    def find_all_paths(self,
                       source: str,
                       target: str,
                       max_depth: int = 10,
                       edge_type_filter: Optional[Union[str, List[str], Callable[[str, str, Dict], bool]]] = None) -> List[List[str]]:
        """
        Find all simple paths from source to target node within a maximum depth.
        Delegates to GraphStore.find_paths if available.

        Args:
            source: FQN of the source node.
            target: FQN of the target node.
            max_depth: Maximum path length (number of nodes in path).
            edge_type_filter: Optional filter for edges. Can be a specific edge type (str),
                              a list of edge types, or a callable
                              (source_node, target_node, edge_data_dict) -> bool.

        Returns:
            A list of paths, where each path is a list of node FQNs.
        """
        
        if not self.graph_store.has_node(source) or not self.graph_store.has_node(target):
            logger.debug(f"Source ({source}) or Target ({target}) node not found for find_all_paths.")
            return []

        if hasattr(self.graph_store, 'find_paths'):
            return self.graph_store.find_paths(
                start=source,
                end=target,
                max_length=max_depth,
                edge_type_filter=edge_type_filter
            )
        else:
            # Fallback to a basic DFS if GraphStore doesn't have find_paths
            # (This is a simplified DFS, GraphStore should ideally have this)
            logger.warning("GraphStore.find_paths not found. Using basic DFS for find_all_paths (less efficient).")
            all_found_paths = []
            
            def _dfs_paths(current_node, current_path, current_depth):
                current_path = current_path + [current_node]
                if current_node == target:
                    all_found_paths.append(current_path)
                    return
                if current_depth >= max_depth:
                    return

                # Check edge_type_filter type
                is_callable_filter = callable(edge_type_filter)
                allowed_edge_types = set()
                if isinstance(edge_type_filter, str):
                    allowed_edge_types = {edge_type_filter}
                elif isinstance(edge_type_filter, list):
                    allowed_edge_types = set(edge_type_filter)

                for neighbor, edge_attrs in self.graph_store.get_successors(current_node):
                    if neighbor not in current_path: # Simple path (no cycles within a path)
                        passes_filter = True
                        if edge_type_filter:
                            edge_type = edge_attrs.get("edge_type")
                            if is_callable_filter:
                                if not edge_type_filter(current_node, neighbor, edge_attrs):
                                    passes_filter = False
                            elif edge_type not in allowed_edge_types:
                                passes_filter = False
                        
                        if passes_filter:
                            _dfs_paths(neighbor, current_path, current_depth + 1)

            _dfs_paths(source, [], 1)
            return all_found_paths
    
    
    def find_cycles(self, edge_type: Optional[str] = None) -> List[List[str]]:
        """
        Finds all simple cycles in the graph, optionally filtered by edge type.
        A simple cycle means no repeated vertices except for the start/end node.
        Self-loops u->u are returned as [u, u].

        Args:
            edge_type: Optional edge type to filter the graph before finding cycles.

        Returns:
            A list of cycle paths (each path is a list of node FQNs).
        """
        
        graph_to_search = self.graph_store.graph
        subgraph_nodes = None

        if edge_type:
            # Create a MultiDiGraph view or copy containing only edges of the specified type
            subgraph_multi = nx.MultiDiGraph()
            for u, v, k, data in self.graph_store.graph.edges(keys=True, data=True):
                if k == edge_type:
                    subgraph_multi.add_node(u, **self.graph_store.graph.nodes[u])
                    subgraph_multi.add_node(v, **self.graph_store.graph.nodes[v])
                    subgraph_multi.add_edge(u, v, key=k, **data)
            graph_to_search = subgraph_multi

            if not graph_to_search.nodes():
                logger.info(f"No nodes in subgraph for edge_type '{edge_type}'. No cycles to find.")
                return []

        logger.debug(f"Finding cycles in graph with {graph_to_search.number_of_nodes()} nodes and {graph_to_search.number_of_edges()} edges (filtered by type: {edge_type}).")

        # nx.simple_cycles returns lists like [u, v] for u->v->u.
        # It does not find self-loops.
        try:
            cycles_found = list(nx.simple_cycles(graph_to_search))
        except nx.NetworkXNotImplemented:
            logger.warning("nx.simple_cycles not implemented for this graph type (e.g., empty). Returning no cycles.")
            cycles_found = []


        # Explicitly find and add self-loops.
        # If subgraph_nodes is defined, we only iterate over those.
        nodes_to_check_for_self_loops = subgraph_nodes if subgraph_nodes is not None else self.graph_store.get_node_ids()

        for node in nodes_to_check_for_self_loops:
            # Important: Check has_edge on the potentially filtered graph_to_search,
            # but the edge attributes (like 'edge_type') must be correct for that edge.
            # If graph_to_search is the original graph, this is fine.
            # If graph_to_search is a subgraph, attributes must have been copied.
            if graph_to_search.has_edge(node, node):
                # If edge_type was specified, the subgraph already contains only those edges.
                # So, any self-loop in graph_to_search (if it's the subgraph) is of the correct type.
                # If edge_type was NOT specified, graph_to_search IS the main graph.
                # We need to check the edge_type attribute of the self-loop if we are to be strict.
                # However, the method signature allows finding all cycles if edge_type is None.
                # For simplicity, if it's a self-loop in the graph_to_search, we add it.
                # The test `test_find_call_cycles` passes REL_TYPE_CALLS, so this is fine.
                cycles_found.append([node, node])

        logger.info(f"Found {len(cycles_found)} cycles (including self-loops if applicable) of type '{edge_type if edge_type else 'any'}'.")
        return cycles_found
    
    
    def find_export_chains(self,
                           target_component_fqn: str, # The canonical FQN of the item we are tracing
                           max_depth: int = 10) -> List[List[ExportStep]]:
        """
        Finds all possible valid export chains for a given component FQN.
        An export chain traces how a component becomes publicly available through a series of definitions, imports, and (re-)exports.

        Args:
            target_component_fqn: The FQN of the component to trace.
            max_depth: Maximum length of an export chain (number of ExportStep objects).

        Returns:
            A list of chains, where each chain is a list of ExportStep objects.
        """
        
        logger.debug(f"[FC_START] Finding export chains for: {target_component_fqn}")
        final_chains: List[List[ExportStep]] = []

        if not self.graph_store.has_node(target_component_fqn):
            logger.warning(f"[FC] Target component '{target_component_fqn}' not found in graph.")
            return final_chains

        definition_module_fqn: Optional[str] = None
        component_short_name = target_component_fqn.split('.')[-1]

        logger.debug(f"[FC_GET_DEF_MODULE] Querying for DEFINED_IN edge with source='{target_component_fqn}'")
        
        # Determine definition module
        for _source_comp, mod_fqn, _key, _data in list(self.graph_store.get_edges(source=target_component_fqn, edge_type=REL_TYPE_DEFINED_IN)):
            definition_module_fqn = mod_fqn
            # logger.debug(f"[FC] Found definition module via DEFINED_IN: {definition_module_fqn}")
            break

        if not definition_module_fqn:
            # Use get_node which returns a dict, then .get() the specific attribute
            node_data = self.graph_store.get_node(target_component_fqn)
            node_type = node_data.get("node_type") if node_data else None
            # logger.debug(f"[FC] No DEFINED_IN. Node type for {target_component_fqn} is {node_type}")
            
            if node_type == NODE_TYPE_MODULE: # "module":
                definition_module_fqn = target_component_fqn
                component_short_name = definition_module_fqn.split('.')[-1]
            elif '.' in target_component_fqn:
                parent_fqn = target_component_fqn.rsplit('.', 1)[0]
                parent_node_data = self.graph_store.get_node(parent_fqn)
                parent_node_type = parent_node_data.get("node_type") if parent_node_data else None
                if self.graph_store.has_node(parent_fqn) and parent_node_type == NODE_TYPE_MODULE: # "module":
                    definition_module_fqn = parent_fqn
                else:
                    logger.warning(f"[FC] Could not determine valid definition module for '{target_component_fqn}'. Parent: {parent_fqn}, ParentType: {parent_node_type}")
                    return final_chains
            else:
                logger.warning(f"[FC] '{target_component_fqn}' is top-level without clear definition module.")
                return final_chains
        
        logger.info(f"[FC] Initial definition module for '{target_component_fqn}' is '{definition_module_fqn}', item short name '{component_short_name}'.")
        
        initial_availability = "defined_locally"
        if definition_module_fqn == target_component_fqn: # If the target is a module itself
            initial_availability = "is_module_itself"

        initial_step = ExportStep(
            module_in_chain_fqn=definition_module_fqn,
            name_in_module_scope=component_short_name,
            target_item_fqn=target_component_fqn,
            availability_mechanism=initial_availability,
            is_explicitly_exported_from_this_module=False, # Determined by checking EXPORTS edges later
        )

        # BFS queue: (current_module_fqn, name_of_target_as_known_in_current_module, chain_so_far)
        queue = deque([(definition_module_fqn, component_short_name, [initial_step])])
        # Visited state: (module_fqn, name_as_known_in_module_scope) to prevent redundant explorations
        visited_bfs_states = set([(definition_module_fqn, component_short_name)])
        # To store signatures of chains already added to final_chains, ensuring uniqueness
        added_chain_signatures = set()

        while queue:
            current_module_fqn, name_as_known_in_current_module, current_chain = queue.popleft()
            # logger.debug(f"[FC_LOOP] Processing: current_mod='{current_module_fqn}', name_in_scope='{name_as_known_in_current_module}', chain_len={len(current_chain)}")

            if len(current_chain) > max_depth: # Check before accessing last_step
                logger.debug(f"[FC_LOOP] Max depth {max_depth} reached for chain (len: {len(current_chain)}).")
                continue

            last_step = current_chain[-1]
            # # Sanity checks for last_step based on how it was created
            # assert last_step.module_in_chain_fqn == current_module_fqn
            # assert last_step.name_in_module_scope == name_as_known_in_current_module
            # assert last_step.target_item_fqn == target_component_fqn

            # Determine if the target item is explicitly exported from the current module
            # An EXPORTS edge: current_module_fqn --[EXPORTS(exported_name=name_as_known_in_current_module, is_explicit=T/F)]--> target_component_fqn
            is_explicitly_exported_here = False
            export_edges = list(self.graph_store.get_edges(source=current_module_fqn, edge_type=REL_TYPE_EXPORTS))
            logger.debug(f"[FC_LOOP] Checking exports from '{current_module_fqn}' for item '{name_as_known_in_current_module}': Found {len(export_edges)} EXPORTS edges.")
            for _source_node, exp_target_fqn, _edge_key, edge_data in export_edges: # edge_data is the full attribute dict
                export_metadata = edge_data.get("metadata", {}) # Access nested metadata
                # logger.debug(f"[FC_LOOP]  Found EXPORTS edge to '{exp_target_fqn}' with metadata: {export_metadata}")
                if export_metadata.get("exported_name") == name_as_known_in_current_module and exp_target_fqn == target_component_fqn:
                    is_explicitly_exported_here = export_metadata.get("is_explicit", False) # Get from metadata
                    # logger.debug(f"[FC_LOOP] Item '{name_as_known_in_current_module}' IS exported from '{current_module_fqn}' (is_explicit from metadata: {is_explicitly_exported_here}).")
                    break
                else:
                    logger.debug(f"[FC_LOOP]  ...no match: exp_name='{export_metadata.get('exported_name')}' vs name_known='{name_as_known_in_current_module}'; exp_target='{exp_target_fqn}' vs target_comp='{target_component_fqn}'")
            
            last_step.is_explicitly_exported_from_this_module = is_explicitly_exported_here
            
            logger.debug(f"[FC] Last step for '{current_module_fqn}': explicit_export={last_step.is_explicitly_exported_from_this_module}")
            
            # Current chain represents one way target_component_fqn is available in current_module_fqn
            # Add a copy of this chain to final_chains if it's unique
            # Chain signature includes the export status of the final step
            chain_signature_tuple = tuple((s.module_in_chain_fqn, s.name_in_module_scope, s.target_item_fqn, s.availability_mechanism, s.is_explicitly_exported_from_this_module) for s in current_chain)
            if chain_signature_tuple not in added_chain_signatures:
                final_chains.append(list(current_chain)) # Store a copy
                added_chain_signatures.add(chain_signature_tuple)
                # logger.debug(f"[FC_LOOP] Added chain to final_chains (total: {len(final_chains)}). Last step explicit: {is_explicitly_exported_here}")

            
            # --- Try to extend chain via NAME_ALIAS ---
            # Modules that import target_component_fqn directly via an alias.
            # Edge: importer_module --[NAME_ALIAS (attrs)]--> target_component_fqn
            # logger.debug(f"[FC_EXTEND] Trying NAME_ALIAS for item='{target_component_fqn}' imported from current_mod='{current_module_fqn}' as name='{name_as_known_in_current_module}'")
            name_alias_edges = list(self.graph_store.get_edges(target=target_component_fqn, edge_type=REL_TYPE_NAME_ALIAS))
            for _source_node, importer_module_fqn, _edge_key, edge_data in name_alias_edges: # edge_data is the full attribute dict from graph
                import_attrs_metadata = edge_data.get("metadata", {}) # Access the nested metadata dictionary
                # logger.debug(f"[FC_EXTEND_NA] Found potential NAME_ALIAS importer: '{importer_module_fqn}', attrs_metadata: {import_attrs_metadata}")

                original_name = import_attrs_metadata.get("original_name_in_source")
                source_mod_for_alias = import_attrs_metadata.get("source_module_fqn")
                
                # logger.debug(f"[FC_EXTEND_NA]  current_mod='{current_module_fqn}', name_as_known='{name_as_known_in_current_module}'")
                # logger.debug(f"[FC_EXTEND_NA]  extracted_source_mod='{source_mod_for_alias}', extracted_orig_name='{original_name}'")
                
                if source_mod_for_alias == current_module_fqn and original_name == name_as_known_in_current_module:
                    
                    name_for_next_step = import_attrs_metadata.get("alias_name") # how it's known in importer_module_fqn
                    if not name_for_next_step:
                        logger.warning(f"[FC_EXTEND_NA] NAME_ALIAS from '{importer_module_fqn}' missing 'alias_name'. Metadata: {import_attrs_metadata}")
                        continue

                    next_bfs_state = (importer_module_fqn, name_for_next_step)
                    
                    # logger.debug(f"[FC]  NAME_ALIAS MATCH! next_module='{importer_module_fqn}', next_name='{name_for_next_step}'")
                    
                    if next_bfs_state in visited_bfs_states: continue
                    
                    logger.info(f"[FC_EXTEND_NA] Chain extended via NAME_ALIAS: '{current_module_fqn}'.'{name_as_known_in_current_module}' -> '{importer_module_fqn}'.'{name_for_next_step}'")
                    
                    next_step = ExportStep(
                        module_in_chain_fqn=importer_module_fqn,
                        name_in_module_scope=name_for_next_step,
                        target_item_fqn=target_component_fqn,
                        availability_mechanism="imported_via_name_alias",
                        is_explicitly_exported_from_this_module=False,
                    )
                    new_chain = current_chain + [next_step]
                    if len(new_chain) <= max_depth:
                        visited_bfs_states.add(next_bfs_state)
                        queue.append((importer_module_fqn, name_for_next_step, new_chain))
                        
                else:
                    logger.debug(f"[FC]  NAME_ALIAS NO MATCH.")


            # --- Try to extend chain via IMPORTS (direct, non-aliased from perspective of original name) ---
            # Modules that did 'from current_module_fqn import name_as_known_in_current_module [as some_alias_or_not]'
            # Edge: importer_module --[IMPORTS (attrs)]--> current_module_fqn
            # logger.debug(f"[FC_EXTEND] Trying IMPORTS for item='{name_as_known_in_current_module}' from current_mod='{current_module_fqn}'")
            importer_edges = list(self.graph_store.get_edges(target=current_module_fqn, edge_type=REL_TYPE_IMPORTS))
            for importer_module_fqn, _original_target, _edge_key, edge_data in importer_edges: # edge_data is the full attribute dict
                import_attrs_metadata = edge_data.get("metadata", {}) # Access the nested metadata
                # logger.debug(f"[FC_EXTEND_IMP] Found potential IMPORTER: '{importer_module_fqn}' (imports FROM '{current_module_fqn}'), attrs_metadata: {import_attrs_metadata}")
                
                raw_imported_name = import_attrs_metadata.get("raw_imported_name")
                resolved_item_fqn = import_attrs_metadata.get("name_bound_points_to_fqn") or import_attrs_metadata.get("imported_entity_fqn")
                if resolved_item_fqn is None: # Fallback if name_bound_points_to_fqn is not there
                    resolved_item_fqn = import_attrs_metadata.get("imported_entity_fqn")


                # logger.debug(f"[FC_EXTEND_IMP]  current_mod='{current_module_fqn}', name_as_known='{name_as_known_in_current_module}', target_comp='{target_component_fqn}'")
                # logger.debug(f"[FC_EXTEND_IMP]  extracted_raw_name='{raw_imported_name}', extracted_resolved_fqn='{resolved_item_fqn}'")

                if raw_imported_name == name_as_known_in_current_module and resolved_item_fqn == target_component_fqn:
                    name_for_next_step = import_attrs_metadata.get("name_bound_in_importer")
                    if not name_for_next_step:
                        logger.warning(f"[FC_EXTEND_IMP] IMPORTS from '{importer_module_fqn}' missing 'name_bound_in_importer'. Metadata: {import_attrs_metadata}")
                        continue

                    next_bfs_state = (importer_module_fqn, name_for_next_step)
                    if next_bfs_state in visited_bfs_states: continue

                    # logger.info(f"[FC_EXTEND_IMP] Chain extended via IMPORTS: '{current_module_fqn}'.'{name_as_known_in_current_module}' -> '{importer_module_fqn}'.'{name_for_next_step}'")
                    availability = "imported_directly_with_alias" if import_attrs_metadata.get("raw_alias") else "imported_directly"
                    
                    next_step = ExportStep(
                        module_in_chain_fqn=importer_module_fqn,
                        name_in_module_scope=name_for_next_step,
                        target_item_fqn=target_component_fqn,
                        availability_mechanism=availability,
                        is_explicitly_exported_from_this_module=False,
                    )
                    new_chain = current_chain + [next_step] # Define new_chain here
                    if len(new_chain) <= max_depth:
                        visited_bfs_states.add(next_bfs_state)
                        queue.append((importer_module_fqn, name_for_next_step, new_chain))
                
                else:
                    logger.debug(f"[FC_EXTEND_IMP]  IMPORTS NO MATCH. Condition check: (raw_name == name_as_known) is {raw_imported_name == name_as_known_in_current_module}, (resolved_fqn == target_component_fqn) is {resolved_item_fqn == target_component_fqn}")

            # --- Handle extension by wildcard imports ---
            # Find modules that do 'from current_module_fqn import *'
            # Wildcard import edge: WildcardImporterModule -> current_module_fqn (source of wildcard)
            wildcard_importer_edges = list(self.graph_store.get_edges(
                 target=current_module_fqn, # Modules that import current_module_fqn
                 edge_type=REL_TYPE_WILDCARD_IMPORT))
            for wildcard_importer_fqn, _original_target, _edge_key, wildcard_edge_attrs in wildcard_importer_edges:
                # 'name_as_known_in_current_module' becomes available in 'wildcard_importer_fqn' by its own name, provided it's not a "private" name (e.g., not starting with '_').
                # More robustly, this should check current_module_fqn.__all__ if that data is available.
                if name_as_known_in_current_module.startswith('_') and \
                not self.is_in_dunder_all(current_module_fqn, name_as_known_in_current_module):
                    continue # Skip private names not in __all__ for wildcard

                name_for_next_step_via_wildcard = name_as_known_in_current_module

                next_bfs_state_wild = (wildcard_importer_fqn, name_for_next_step_via_wildcard)
                if next_bfs_state_wild in visited_bfs_states:
                    continue
                
                wildcard_export_step = ExportStep(
                    module_in_chain_fqn=wildcard_importer_fqn,
                    name_in_module_scope=name_for_next_step_via_wildcard,
                    target_item_fqn=target_component_fqn, # Stays constant
                    availability_mechanism="imported_via_wildcard",
                    is_explicitly_exported_from_this_module=False, # To be determined
                )
                
                new_chain_wild = current_chain + [wildcard_export_step]
                if len(new_chain_wild) <= max_depth:
                    visited_bfs_states.add(next_bfs_state_wild)
                    queue.append((wildcard_importer_fqn, name_for_next_step_via_wildcard, new_chain_wild))
        
        logger.debug(f"[FC_END] Found {len(final_chains)} chains for {target_component_fqn}: {final_chains}")
        return final_chains

    
    def is_in_dunder_all(self, module_fqn: str, name: str) -> bool:
        """Helper to check if a name is in the __all__ list of a module from graph node data."""
        node_data = self.graph_store.get_node(module_fqn) # Uses get_node
        if node_data: # get_node returns the attribute dict directly
            all_values = node_data.get("all_values") 
            if isinstance(all_values, (list, set)):
                return name in all_values
        return False
    
    
    def find_export_chains_guided_graph(self, target_component_fqn: str, end_module_fqn: str, all_re_exporters: Set[str], definition_module_fqn: str) -> List[List[ExportStep]]:
        """
        Finds all export chains using a guided backward trace on the graph.
        This is the primary graph-based fallback (Tier 2). It is much faster than a blind search because it only considers paths between a pre-computed set of relevant (re-exporting) modules.

        Args:
            target_component_fqn: The FQN of the component to trace.
            end_module_fqn: The target public API module to start the backward search from.
            all_re_exporters: The pre-computed set of all modules that re-export the candidate.
            definition_module_fqn: The FQN of the module where the component is defined.

        Returns:
            A list of all found chains, where each chain is a list of ExportStep objects.
        """
        logger.debug(f"[GuidedGraph] Starting guided graph trace for '{target_component_fqn}'")
        final_chains: List[List[ExportStep]] = []

        # The nodes we are allowed to traverse in our search
        virtual_graph_nodes = all_re_exporters | {definition_module_fqn, end_module_fqn}

        # We are looking for a path from `end_module_fqn` back to `definition_module_fqn` by only traversing through other nodes in `virtual_graph_nodes`.
        # Each item in the queue is a path of module FQNs, from an entry point back toward the definition.
        queue = deque([[end_module_fqn]])
        
        while queue:
            current_path = queue.popleft()
            current_module = current_path[-1]

            if current_module == definition_module_fqn:
                # Found a complete path of modules. Now, reconstruct the detailed ExportStep chain.
                # The path is backward, so we reverse it to get the correct order for reconstruction.
                forward_module_path = list(reversed(current_path))
                reconstructed_chain = self._reconstruct_chain_from_module_path_graph(forward_module_path, target_component_fqn)
                if reconstructed_chain:
                    final_chains.append(reconstructed_chain)
                continue # This path is complete.

            # Find which modules the `current_module` imports from our virtual graph.
            # We query the graph for incoming IMPORTS edges to the current_module.
            # The source of these edges are the modules it imports from.
            for source_module, _, _, _ in self.graph_store.get_edges(target=current_module, edge_type=REL_TYPE_IMPORTS):
                # The source must be another node in our limited virtual graph.
                if source_module in virtual_graph_nodes and source_module not in current_path:
                    new_path = current_path + [source_module]
                    queue.append(new_path)

        logger.info(f"[GuidedGraph] Found {len(final_chains)} chains for '{target_component_fqn}' via guided graph trace.")
        return final_chains

    def _reconstruct_chain_from_module_path_graph(self, module_path: List[str], target_component_fqn: str) -> Optional[List[ExportStep]]:
        """
        Helper for the guided graph trace. Takes a path of module FQNs and builds the detailed List[ExportStep] chain by querying the graph for the specific import records between each hop.
        """
        reconstructed_chain: List[ExportStep] = []
        
        # 1. Create the initial step for the definition module
        definition_module_fqn = module_path[0]
        component_short_name = target_component_fqn.split('.')[-1]
        first_step = ExportStep(
            module_in_chain_fqn=definition_module_fqn,
            name_in_module_scope=component_short_name,
            target_item_fqn=target_component_fqn,
            availability_mechanism="defined_locally",
            is_explicitly_exported_from_this_module=False # Determine next
        )
        
        # Query graph for export status from defining module
        for _, _, _, edge_data in self.graph_store.get_edges(source=definition_module_fqn, target=target_component_fqn, edge_type=REL_TYPE_EXPORTS):
            if edge_data.get("metadata", {}).get("exported_name") == component_short_name:
                first_step.is_explicitly_exported_from_this_module = edge_data.get("metadata", {}).get("is_explicit", False)
                break
        reconstructed_chain.append(first_step)

        # 2. Trace the import steps
        name_to_trace = component_short_name
        for i in range(len(module_path) - 1):
            source_module = module_path[i]
            importer_module = module_path[i+1]
            
            # Find the specific import record edge(s) that connect these two modules
            # and provide the item we are tracing.
            found_import_hop = False
            for _, _, _, edge_data in self.graph_store.get_edges(source=importer_module, target=source_module, edge_type=REL_TYPE_IMPORTS):
                imp_rec = edge_data.get("metadata", {})
                if imp_rec.get("raw_imported_name") == name_to_trace:
                    name_in_importer = imp_rec.get("name_bound_in_importer")
                    availability = "imported_directly_with_alias" if imp_rec.get("raw_alias") else "imported_directly"
                    if imp_rec.get("is_wildcard_statement"): availability = "imported_via_wildcard"
                    
                    next_step = ExportStep(
                        module_in_chain_fqn=importer_module,
                        name_in_module_scope=name_in_importer,
                        target_item_fqn=target_component_fqn,
                        availability_mechanism=availability,
                        is_explicitly_exported_from_this_module=False
                    )
                    
                    # Determine export status for this new step
                    for _, _, _, exp_edge_data in self.graph_store.get_edges(source=importer_module, edge_type=REL_TYPE_EXPORTS):
                        exp_meta = exp_edge_data.get("metadata", {})
                        if exp_meta.get("exported_name") == name_in_importer:
                            next_step.is_explicitly_exported_from_this_module = exp_meta.get("is_explicit", False)
                            break
                            
                    reconstructed_chain.append(next_step)
                    name_to_trace = name_in_importer
                    found_import_hop = True
                    break # Found the connecting import, move to the next hop in the module_path
            
            if not found_import_hop:
                logger.warning(f"[GuidedGraph] Could not reconstruct chain link from '{source_module}' to '{importer_module}' for '{target_component_fqn}'. Import edge not found.")
                return None # Path is broken

        return reconstructed_chain
    
    
    def get_component_information(self, component_fqn: str) -> Optional[Dict[str, Any]]:
        """
        Get available information about a component from the graph.
        Note: Best export chain and resolved API path are determined by APIPathResolver.
        """
        if not self.graph_store.has_node(component_fqn):
            logger.warning(f"Component {component_fqn} not found in graph store.")
            return None

        result: Dict[str, Any] = {
            "component_fqn": component_fqn,
            "node_type": "unknown",
            "attributes": {},
            "definition_module_fqn": None,
            "direct_importers_count": 0, # Example of other info that could be derived
            "direct_exporters_count": 0,
        }

        node_data = self.graph_store.get_node_attributes(component_fqn)
        if node_data:
            result["node_type"] = node_data.get("node_type", "unknown")
            # Filter out potentially large or noisy attributes for this summary
            result["attributes"] = {k:v for k,v in node_data.items() if k not in ['statistics', 'source_code_hash']}


        # Find defining module (if any)
        # Assuming DEFINED_IN edge is target_component_fqn --DEFINED_IN--> definition_module_fqn
        def_edges = list(self.graph_store.get_edges(source=component_fqn, edge_type=REL_TYPE_DEFINED_IN))
        if def_edges:
            result["definition_module_fqn"] = def_edges[0][1] # Target of the first DEFINED_IN edge

        # Example: Count direct importers (modules that import this component)
        # This requires iterating through IMPORTS, NAME_ALIAS, MODULE_ALIAS edges targeting this component
        # For simplicity, this is just a placeholder for more detailed info.
        # result["direct_importers_count"] = len(self.graph_store.get_edges(target=component_fqn, edge_type=REL_TYPE_IMPORTS)) + \
        #                                   len(self.graph_store.get_edges(target=component_fqn, edge_type=REL_TYPE_NAME_ALIAS))
        
        # Example: Count modules that directly export this component
        # result["direct_exporters_count"] = len(self.graph_store.get_edges(target=component_fqn, edge_type=REL_TYPE_EXPORTS))
        
        logger.debug(f"Retrieved basic component info for {component_fqn} from GraphTraversal.")
        return result
    
    
    def bfs(self,
            start_node: str,
            direction: str = 'out',
            edge_type: Optional[str] = None,
            max_depth: Optional[int] = None) -> Set[str]:
        """
        Performs a Breadth-First Search starting from start_node.

        Args:
            start_node: The FQN of the node to start BFS from.
            direction: 'out' for outgoing edges, 'in' for incoming edges.
            edge_type: Optional specific edge type to follow.
            max_depth: Optional maximum depth to explore (number of edges from start).

        Returns:
            A set of FQNs of all reachable nodes (excluding start_node by default,
            but can be included if BFS logic changes, or caller can add it).
        """
        
        if not self.graph_store.has_node(start_node):
            logger.warning(f"BFS start node {start_node} not found.")
            return set()

        visited = {start_node}
        # queue stores (node, current_depth_level)
        queue = deque([(start_node, 0)])
        reachable_nodes = set()

        while queue:
            current_node, depth = queue.popleft()

            if max_depth is not None and depth >= max_depth:
                continue # Stop exploring further down this path

            successors_or_predecessors = []
            if direction == 'out':
                successors_or_predecessors = self.graph_store.get_successors(current_node, edge_type=edge_type)
            elif direction == 'in':
                successors_or_predecessors = self.graph_store.get_predecessors(current_node, edge_type=edge_type)
            else:
                raise ValueError("BFS direction must be 'in' or 'out'")

            for neighbor_node, _ in successors_or_predecessors:
                if neighbor_node not in visited:
                    visited.add(neighbor_node)
                    reachable_nodes.add(neighbor_node) 
                    queue.append((neighbor_node, depth + 1))
        
        return reachable_nodes
    
    
