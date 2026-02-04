"""
Graph store implementation with performance optimizations.

This module provides a graph database abstraction for storing code relationships.
The GraphStore class manages nodes (modules, functions, etc.) and edges (imports,
exports, etc.) in a dependency graph with optimizations for large codebases.
"""

import time
import logging
from collections import defaultdict, deque
import networkx as nx
from typing import Dict, List, Set, Tuple, Any, Optional, Union, Iterator, Callable, Generator

import uuid 

# Optional memory profiling
try:
    import psutil
    MEMORY_PROFILING_AVAILABLE = True
except ImportError:
    MEMORY_PROFILING_AVAILABLE = False



logger = logging.getLogger(__name__)


class GraphStore:
    """
    A graph database abstraction for code relationships with performance optimizations.
    
    Uses NetworkX as the underlying graph implementation with additional indexing
    and caching for improved performance with large codebases.
    """
    
    COMPONENT_NAME = "graph_store"
    DEPENDENCIES: Set[str] = set()
    
    def __init__(self, memory_threshold_mb: int = 1000, enable_indices: bool = True, enable_caching: bool = True):
        """
        Initialize an empty directed graph with optimizations.
        
        Args:
            memory_threshold_mb: Memory threshold for optimization (MB)
            enable_indices: Whether to enable indices for faster queries
            enable_caching: Whether to enable caching for frequently used queries
        """
        self.graph = nx.MultiDiGraph()
        
        # Configuration
        self.memory_threshold_mb = memory_threshold_mb
        self.enable_indices = enable_indices
        self.enable_caching = enable_caching
        
        # Performance tracking
        self.query_times = defaultdict(list)
        
        self._graph_changed = True # Initialize a flag to track graph changes for index/cache
        self.log_performance = True # Assume true, or make configurable
        
        # Create indices for faster lookups
        if self.enable_indices:
            # Index edges by semantic relationship_type: {relationship_type: set((source, target, actual_networkx_key))}
            self._edge_type_index = defaultdict(set)
            # Index edges by source: {source_node: {relationship_type: {target_node: set_of_actual_networkx_keys}}}
            self._source_edge_index = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
            # Index edges by target: {target_node: {relationship_type: {source_node: set_of_actual_networkx_keys}}}
            self._target_edge_index = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
            self._node_attr_index = defaultdict(lambda: defaultdict(set))
        
        # Cache for expensive queries
        if self.enable_caching:
            self._cache: Dict[str, Any] = {}
            self._cache_hits = 0
            self._cache_misses = 0
            self._cache_size = 0 # Number of items in cache
            self._max_cache_size = 1000 # Maximum number of cached results
            
        # Batch operation support
        self._batch_mode = False
        self._pending_nodes: List[Tuple[str, Dict]] = []
        self._pending_edges: List[Tuple[str, str, str, Dict]] = [] # edge_type is the key
        
        logger.debug(f"GraphStore initialized with MultiDiGraph, {'indices enabled' if enable_indices else 'indices disabled'}, "
                    f"{'caching enabled' if enable_caching else 'caching disabled'}")
    
    def initialize(self) -> None:
        """
        Initialize the component after registration.
        For GraphStore, most initialization happens in __init__.
        This method is called by the registry after dependencies are met.
        """
        logger.info(f"{self.COMPONENT_NAME} ({id(self)}) initialized by registry.")
        
    def _update_node_indices(self, node_id: str, attributes: Dict[str, Any]) -> None:
        """Update node attribute indices when a node is added or updated."""
        if not self.enable_indices:
            return
        for attr_name, attr_value in attributes.items():
            if not isinstance(attr_value, (str, int, bool, float, tuple)): # Ensure hashable
                continue # Skip non-hashable or uninteresting types for indexing
            if attr_name == "node_type":
                 self._node_attr_index[attr_name][attr_value].add(node_id)
            # Add other specific attribute indexing if needed
        logger.debug(f"[GS_INDEX] Updated node indices for node '{node_id}' with attributes: {attributes}")
    
    def get_state(self) -> Dict[str, Any]:
        """Get component state for synchronization or checkpointing."""
        return {
            "node_count": self.node_count(),
            "edge_count_total": self.edge_count(),
            "indexed_edge_types": list(self._edge_type_index.keys()) if self.enable_indices else [],
            "cache_size": self._cache_size if self.enable_caching else 0,
            "graph_changed_since_last_index_rebuild": self._graph_changed # Important for persistence
        }

    def sync_state(self, state: Dict[str, Any]) -> bool:
        """
        Synchronize state with other components or from a checkpoint.
        For GraphStore, a full state sync would mean loading a graph.
        This is a complex operation, so a simple version just logs.
        """
        logger.warning(f"{self.COMPONENT_NAME} sync_state called. Full graph state synchronization is complex and not fully implemented here. Received state keys: {list(state.keys())}")
        return True # Placeholder

    def on_dependency_ready(self, dependency_name: str) -> None:
        """Called when a declared dependency becomes ready."""
        logger.debug(f"{self.COMPONENT_NAME} notified that dependency '{dependency_name}' is ready.")
        # GraphStore currently has no declared registry dependencies, so this might not be called unless DEPENDENCIES is changed
    
    def cleanup(self) -> None:
        """Clean up resources before shutdown."""
        logger.info(f"{self.COMPONENT_NAME} ({id(self)}) cleanup initiated.")
        self.clear() # Uses the existing clear method
        if self.enable_caching:
            self._cache.clear()
            logger.info(f"{self.COMPONENT_NAME} cache cleared.")
        logger.info(f"{self.COMPONENT_NAME} cleanup complete.")
    
    
    def _update_edge_indices(self, source: str, target: str, 
                             relationship_type: str, # The semantic type of the edge
                             actual_networkx_key: str, # The unique key used in MultiDiGraph
                             edge_attributes: Dict[str, Any]): # Full attributes of the edge
        """
        Update edge indices when an edge is added or updated.
        Indices are primarily based on relationship_type.
        """
        if not self.enable_indices:
            return

        # 1. Edge Type Index: {relationship_type: set((source, target, actual_networkx_key))}
        #    This allows finding all edges of a specific semantic type.
        self._edge_type_index[relationship_type].add((source, target, actual_networkx_key))
        # 2. Source Edge Index: {source_node: {relationship_type: {target_node: set_of_actual_networkx_keys}}}
        self._source_edge_index[source][relationship_type][target].add(actual_networkx_key)
        # 3. Target Edge Index: {target_node: {relationship_type: {source_node: set_of_actual_networkx_keys}}}
        self._target_edge_index[target][relationship_type][source].add(actual_networkx_key)
        
        # Example: Indexing 'CALLS' edges by 'is_direct' property (if present in metadata)
        if relationship_type == "CALLS": # Or use actual constant REL_TYPE_CALLS
            metadata = edge_attributes.get("metadata", {})
            if isinstance(metadata, dict):
                is_direct_value = metadata.get("is_direct")
                if isinstance(is_direct_value, bool): # Ensure it's a boolean
                    # Example: self._edge_property_index["CALLS_is_direct"][is_direct_value].add((source, target, actual_networkx_key))
                    # You would need to define self._edge_property_index = defaultdict(lambda: defaultdict(set)) in __init__
                    pass # Placeholder for actual index update
        
        logger.debug(f"[GS_INDEX] Updated indices for edge ({source})-[{relationship_type} key={actual_networkx_key}]->({target})")
    
    
    def _remove_from_edge_indices(self, source: str, target: str, relationship_type: str, actual_networkx_key: str):
        """
        Helper to remove an edge from all relevant edge indices.
        """
        if not self.enable_indices:
            return

        edge_tuple_with_key = (source, target, actual_networkx_key)
        if relationship_type in self._edge_type_index and edge_tuple_with_key in self._edge_type_index[relationship_type]:
            self._edge_type_index[relationship_type].remove(edge_tuple_with_key)
            if not self._edge_type_index[relationship_type]:
                del self._edge_type_index[relationship_type]

        if source in self._source_edge_index and \
           relationship_type in self._source_edge_index[source] and \
           target in self._source_edge_index[source][relationship_type] and \
           actual_networkx_key in self._source_edge_index[source][relationship_type][target]:
            self._source_edge_index[source][relationship_type][target].remove(actual_networkx_key)
            if not self._source_edge_index[source][relationship_type][target]:
                del self._source_edge_index[source][relationship_type][target]
            if not self._source_edge_index[source][relationship_type]:
                del self._source_edge_index[source][relationship_type]
            if not self._source_edge_index[source]:
                del self._source_edge_index[source]

        if target in self._target_edge_index and \
           relationship_type in self._target_edge_index[target] and \
           source in self._target_edge_index[target][relationship_type] and \
           actual_networkx_key in self._target_edge_index[target][relationship_type][source]:
            self._target_edge_index[target][relationship_type][source].remove(actual_networkx_key)
            if not self._target_edge_index[target][relationship_type][source]:
                del self._target_edge_index[target][relationship_type][source]
            if not self._target_edge_index[target][relationship_type]:
                del self._target_edge_index[target][relationship_type]
            if not self._target_edge_index[target]:
                del self._target_edge_index[target]
        logger.debug(f"[GS_INDEX] Removed from indices for edge ({source})-[{relationship_type} key={actual_networkx_key}]->({target})")
    
    
    def add_node(self, node_id: str, **attributes: Any) -> None:
        """
        Add a node to the graph with optional attributes.
        
        Args:
            node_id: Unique identifier for the node (typically a fully qualified name)
            **attributes: Additional attributes to store with the node
        """
        # In batch mode, collect operations to apply later
        if self._batch_mode:
            self._pending_nodes.append((node_id, attributes))
            return
            
        node_exists = self.graph.has_node(node_id)
        self.graph.add_node(node_id, **attributes) # add_node in NX updates attributes if node exists
        logger.debug(f"[GS_ADD_NODE] Node '{node_id}' added/updated with attributes: {attributes}")
        if self.enable_indices:
            self._update_node_indices(node_id, self.graph.nodes[node_id])
        if self.enable_caching and node_exists:
            self._invalidate_node_caches(node_id)
        self._graph_changed = True
        
    
    # def add_edge(self, source: str, target: str, 
    #              edge_type: str, # This is the semantic relationship type string (e.g., "CALLS")
    #              key: Optional[str] = None, # This is the intended MultiDiGraph edge key
    #              **attributes: Any # These are other data attributes  (e.g., {'metadata': {...}})
    #              ) -> bool:
    #     """
    #     Add an edge between two nodes with a specific type, key, and optional attributes.
    #     Updates the edge if one with the same source, target, and key already exists.

    #     Args:
    #         source: Source node ID.
    #         target: Target node ID.
    #         edge_type: Type of relationship (e.g., REL_TYPE_CALLS). This is stored as an attribute.
    #         key: The unique key for this specific edge between source and target.
    #              If None, edge_type is used as the key. This allows multiple edges of the
    #              same type if their keys differ, or multiple edges of different types.
    #         **attributes: Additional attributes to store with the edge. 
    #                       Should not contain 'key' as it's handled by the named 'key' param.
    #                       Should contain 'metadata' for detailed properties.

    #     Returns:
    #         True if the edge was added/updated successfully, False otherwise.
    #     """
        
    #     # In batch mode, collect operations to apply later
    #     if self._batch_mode:
    #         self._pending_edges.append((source, target, edge_type, key, attributes))
    #         return True # Assuming success for batch, actual add happens on commit
            
    #     if source not in self.graph: self.add_node(source)
    #     if target not in self.graph: self.add_node(target)
        
    #     actual_networkx_key = key if key is not None else edge_type
        
    #     attrs_for_graph = {"edge_type": edge_type, **attributes} 
        
    #     logger.debug(
    #         f"[GS_ADD_EDGE] Attempting to add/update edge: ({source})-[{edge_type}]->({target}) "
    #         f"with actual_networkx_key='{actual_networkx_key}' and passed attributes: {attributes}"
    #     )
    #     logger.debug(f"[GS_ADD_EDGE]   Final attributes for graph edge: {attrs_for_graph}")
    #     logger.debug(f"[GS_ADD_EDGE]   Actual NetworkX key to be used: {actual_networkx_key}")
        
    #     if self.graph.has_edge(source, target, key=actual_networkx_key):
    #         logger.debug(f"[GS_ADD_EDGE]   Edge ({source})-[{edge_type} key={actual_networkx_key}]->({target}) already exists. Will be updated.")
    #     else:
    #         logger.debug(f"[GS_ADD_EDGE]   Edge ({source})-[{edge_type} key={actual_networkx_key}]->({target}) is new.")

    #     try:
    #         self.graph.add_edge(source, target, key=actual_networkx_key, **attrs_for_graph)
            
    #         if self.enable_indices:
    #             # Use the semantic 'edge_type' for indexing, along with the specific key
    #             self._update_edge_indices(source, target, edge_type, actual_networkx_key, attrs_for_graph)

    #         if self.enable_caching:
    #             self._invalidate_edge_caches(source, target, edge_type, actual_networkx_key)
            
    #         self._graph_changed = True
    #         # logger.info(f"[GS_ADD_EDGE] Edge ({source})-[{edge_type}]->({target}) with key '{actual_networkx_key}' added/updated.")
    #         return True
    #     except TypeError as te:
    #         logger.error(f"TypeError adding edge to NetworkX graph ({source})-[{edge_type} key={actual_networkx_key}]->({target}) "
    #                      f"with attributes {attrs_for_graph}: {te}", exc_info=True)
    #         raise 
    #     except Exception as e: # Catch other potential NetworkX errors
    #         logger.error(f"NetworkX error adding edge ({source})-[{edge_type} key={actual_networkx_key}]->({target}): {e}", exc_info=True)
    #         return False
    
    
    def add_edge(self, source: str, target: str, 
                 edge_type: str,  # This is the SEMANTIC type, will be stored as data['type']
                 key: Optional[str] = None, # This is the EXPLICIT MultiDiGraph key from the caller
                 **attributes: Any # Other data attributes for the edge
                 ) -> Optional[str]:
        """
        Add/update an edge in the MultiDiGraph.

        Args:
            source: Source node ID.
            target: Target node ID.
            edge_type: The semantic type of the edge (e.g., "CALLS", "IMPORTS"). 
                       This is stored as an attribute `{'type': edge_type}` on the edge.
            key: The explicit key for the MultiDiGraph edge. If None, a UUID is generated.
                 This allows multiple edges between the same nodes, even of the same semantic type,
                 if their keys differ.
            **attributes: Additional data attributes to store on the edge. 
                          The 'type' attribute will be overwritten by edge_type.
                          If 'key' is present in attributes, it's ignored in favor of the 'key' parameter.

        Returns:
            The actual_networkx_key used for the edge if successful, None otherwise.
        """
        
        if not isinstance(self.graph, nx.MultiDiGraph):
            logger.critical(
            f"[GraphStore.add_edge] CRITICAL: self.graph is NOT a MultiDiGraph for instance ID: {id(self)}! "
            f"Type: {type(self.graph)}. This should not occur. Aborting add_edge."
        )
            return None # Still return None if this critical, unexpected state occurs
        
        
        # In batch mode, collect operations to apply later
        if self._batch_mode:
            # Store edge_type (semantic) and key (networkx) separately for commit_batch
            self._pending_edges.append((source, target, edge_type, key, attributes))
            # For batch mode, we can't confirm success until commit, but can return anticipated key
            return key if key is not None else edge_type 
            
        

        # Ensure nodes exist (NetworkX add_node/add_edge adds them if not, but explicit can be good)
        if source not in self.graph: self.add_node(source)
        if target not in self.graph: self.add_node(target)
        
        actual_networkx_key = key if key is not None else edge_type
        
        # Prepare attributes for NetworkX. Store the semantic 'edge_type' as 'type'.
        # Any passed 'type' or 'key' in **attributes will be overwritten.
        attrs_for_graph = attributes.copy() # Start with a copy to avoid modifying input dict
        attrs_for_graph["type"] = edge_type # Store semantic type as 'type'
        if 'key' in attrs_for_graph:
            del attrs_for_graph['key'] # Remove if a user accidentally passed it in attributes

        logger.debug(
            f"[GS_ADD_EDGE] Attempting: ({source})-[key='{actual_networkx_key}', type='{edge_type}']->({target}) "
            f"| Other attrs: {attributes}"
        )
        
        edge_exists_before_add = self.graph.has_edge(source, target, key=actual_networkx_key)
        
        try:
            self.graph.add_edge(source, target, key=actual_networkx_key, **attrs_for_graph)
            
            if self.enable_indices:
                # Use the semantic 'edge_type' (which is now stored as 'type' in attrs_for_graph) for indexing.
                self._update_edge_indices(source, target, edge_type, actual_networkx_key, self.graph[source][target][actual_networkx_key])

            if self.enable_caching:
                self._invalidate_edge_caches(source, target, edge_type, actual_networkx_key)
            
            self._graph_changed = True
            logger.debug(f"[GS_ADD_EDGE]   Successfully {'updated' if edge_exists_before_add else 'added'} edge: "
                         f"({source})-[key='{actual_networkx_key}', type='{edge_type}']->({target})")
            return actual_networkx_key # Return the key used
            
        except Exception as e:
            logger.error(f"[GS_ADD_EDGE] Error adding/updating edge ({source})-"
                         f"[key='{actual_networkx_key}', type='{edge_type}']"
                         f"->({target}): {e}", exc_info=True)
            return None
    
    
    def begin_batch(self) -> None:
        """Begin a batch operation for adding multiple nodes and edges efficiently."""
        self._batch_mode = True
        self._pending_nodes = []
        self._pending_edges = []
        logger.debug("Batch mode started")
    
    
    def commit_batch(self) -> Tuple[int, int]:
        """
        Commit all pending batch operations.
        
        Returns:
            Tuple of (nodes_added, edges_added) counts
        """
        if not self._batch_mode:
            logger.warning("commit_batch called when not in batch mode")
            return (0, 0)
            
        # Temporarily disable direct index/cache updates during batch
        original_indices_enabled_for_add = self.enable_indices
        self.enable_indices = False 

        node_count = 0
        for node_id, attributes in self._pending_nodes:
            self.graph.add_node(node_id, **attributes)
            node_count += 1
            
        edge_count = 0
        # pending_edges stores: source, target, semantic_edge_type, explicit_key, attributes_for_storage
        for source, target, semantic_edge_type, explicit_key, edge_attrs_for_storage in self._pending_edges:
            actual_graph_key = explicit_key if explicit_key is not None else semantic_edge_type
            
            final_graph_attrs = {"edge_type": semantic_edge_type, **edge_attrs_for_storage}

            if source not in self.graph: self.graph.add_node(source)
            if target not in self.graph: self.graph.add_node(target)
            self.graph.add_edge(source, target, key=actual_graph_key, **final_graph_attrs)
            edge_count += 1
            
        # Restore original setting and rebuild if it was enabled
        self.enable_indices = original_indices_enabled_for_add
        
        if self.enable_indices:
            self._rebuild_indices() # Rebuild all indices after batch
        
        if self.enable_caching:
            self._clear_cache() # Full clear after batch is simplest
            
        self._batch_mode = False
        self._pending_nodes = []
        self._pending_edges = []
        
        logger.debug(f"Batch committed: {node_count} nodes, {edge_count} edges")
        self._graph_changed = True # Mark as changed as indices/cache were rebuilt/cleared
        return (node_count, edge_count)
    
    
    def _rebuild_indices(self) -> None:
        """Rebuild all indices from scratch based on the current graph data."""
        if not self.enable_indices:
            logger.debug(f"[{self.COMPONENT_NAME}] Indices are disabled, skipping rebuild.")
            return

        logger.info(f"[{self.COMPONENT_NAME}] Rebuilding all graph indices...")
        start_time = time.perf_counter()
        
        # Clear existing indices before rebuilding
        self._edge_type_index.clear()
        self._source_edge_index.clear()
        self._target_edge_index.clear()
        self._node_attr_index.clear() # Assuming node indices are also rebuilt
        
        logger.debug(f"[{self.COMPONENT_NAME}] Re-indexing nodes...")
        for node_id, attributes in self.graph.nodes(data=True):
            # Call _update_node_indices to populate node-related indices
            # Ensure attributes is the full dictionary of node attributes as expected by _update_node_indices
            self._update_node_indices(node_id, attributes) 
            
        logger.debug(f"[{self.COMPONENT_NAME}] Re-indexing edges...")
        # Iterate through all edges in the graph
        # For a MultiDiGraph, edges(keys=True, data=True) yields (u, v, key, data_dict)
        for source_node, target_node, actual_networkx_key, edge_data_dict in self.graph.edges(keys=True, data=True):
            # Retrieve the semantic type of the relationship, which is stored
            # under the 'type' attribute in the edge_data_dict by GraphStore.add_edge.
            semantic_relationship_type = edge_data_dict.get("type") 
            
            if semantic_relationship_type is None:
                # This is a fallback. Ideally, all edges managed by GraphStore should have a 'type' attribute.
                # If 'type' is missing, we'll use the NetworkX key as a proxy for the semantic type for indexing purposes.
                # This might happen if edges were added to the graph externally or by an older version of the code.
                semantic_relationship_type = actual_networkx_key # Use the NetworkX key as a last resort
                logger.warning(
                    f"[{self.COMPONENT_NAME}] Edge ({source_node})->({target_node}) with NetworkX key '{actual_networkx_key}' "
                    f"is missing the 'type' attribute (expected semantic type). "
                    f"Using key '{actual_networkx_key}' as semantic_relationship_type for indexing. Edge data: {edge_data_dict}"
                )
            
            # Update the edge indices using the determined semantic_relationship_type, the actual NetworkX key, and the full edge_data_dict
            self._update_edge_indices(
                source_node, 
                target_node, 
                semantic_relationship_type, # This is the crucial semantic type for indexing
                actual_networkx_key,        # The unique key for this edge in the MultiDiGraph
                edge_data_dict              # All attributes of the edge
            )
            
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.info(f"[{self.COMPONENT_NAME}] Rebuilt all indices in {duration_ms:.2f}ms.")
        self._graph_changed = False # Reset the flag as indices are now synchronized with the graph
    
    
    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a node and its attributes by ID.
        
        Args:
            node_id: ID of the node to retrieve
            
        Returns:
            Dictionary of node attributes or None if not found
        """
        if not self.graph or not self.graph.has_node(node_id):
            return None
        return self.graph.nodes[node_id].copy()
    
    
    # def get_nodes(self, **filters) -> List[Tuple[str, Dict[str, Any]]]:
    #     """
    #     Get nodes matching specified attribute filters.

    #     Args:
    #         **filters: Keyword arguments where key is attribute name and value is desired attribute value.

    #     Returns:
    #         List of (node_id, attribute_dict) tuples for matching nodes.
    #     """
        
    #     if self.enable_indices and filters:
    #         candidate_sets: List[Set[str]] = []
    #         for attr_name, attr_value in filters.items():
    #             if attr_name in self._node_attr_index and attr_value in self._node_attr_index[attr_name]:
    #                 candidate_sets.append(self._node_attr_index[attr_name][attr_value].copy())
    #             else:
    #                 return []
    #         if not candidate_sets: return []
            
    #         candidate_sets.sort(key=len)
    #         final_node_ids = candidate_sets[0]
    #         for i in range(1, len(candidate_sets)):
    #             final_node_ids.intersection_update(candidate_sets[i])
    #         return [(node_id, self.graph.nodes[node_id].copy()) for node_id in final_node_ids if self.graph.has_node(node_id)]
        
    #     result_nodes = []
    #     for node_id, attributes in self.graph.nodes(data=True):
    #         match = True
    #         if filters:
    #             for key, value in filters.items():
    #                 if attributes.get(key) != value:
    #                     match = False; break
    #         if match: result_nodes.append((node_id, attributes.copy()))
    #     return result_nodes
    
    
    def get_nodes(self, **filters) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Get nodes matching given attribute filters.
        Example: get_nodes(node_type="function", module="example.module")
        """
        if not self.graph:
            return []

        # Optimized lookup if filtering by a single indexed attribute
        if self.enable_indices and len(filters) == 1:
            attr_name, attr_value = next(iter(filters.items()))
            if attr_name in self._node_attr_index and attr_value in self._node_attr_index[attr_name]:
                logger.debug(f"[GS_GET_NODES_INDEX] Using index for filter: {attr_name}={attr_value}")
                node_ids = self._node_attr_index[attr_name][attr_value]
                return [(node_id, self.graph.nodes[node_id].copy()) for node_id in node_ids if self.graph.has_node(node_id)]

        logger.debug(f"[GS_GET_NODES_ITER] Iterating nodes for filters: {filters}")
        results = []
        for node_id, attributes in self.graph.nodes(data=True):
            match = True
            for key, value in filters.items():
                if attributes.get(key) != value: # This is for node attributes, 'type' here would be 'node_type' if used
                    match = False
                    break
            if match:
                results.append((node_id, attributes.copy()))
        return results
    
    
    # def get_edge(self, source: str, target: str, edge_type: str) -> Optional[Dict[str, Any]]:
    #     """
    #     Get the attributes of an edge between source and target.
    #     Assumes only one edge (or the first found) if multiple exist without specifying type.
    #     Prefer using get_edges if specific edge type matters or multiple edges can exist.
        
    #     Args:
    #         source: Source node ID
    #         target: Target node ID
    #         edge_type: Key to filter by
            
    #     Returns:
    #         Dictionary of edge attributes or None if not found
    #     """
    #     if self.graph.has_edge(source, target, key=edge_type):
    #         return self.graph[source][target][edge_type].copy()
    #     return None
    
    def get_edge(self, source: str, target: str, edge_type: str) -> Optional[Dict[str, Any]]:
        # This method seems to imply a single edge for a given type.
        # If multiple edges of the same type can exist (e.g. via different keys),
        # this will return the first one found.
        # For specific key, use get_edge_data_by_key
        if not self.graph or not self.graph.has_edge(source, target):
            return None
        # Iterates over edges between source and target if they exist
        # self.graph[source][target] gives {key: data_dict}
        if source in self.graph and target in self.graph[source]: # Check nodes and path exist
            for key, data in self.graph[source][target].items():
                if data.get('type') == edge_type: # Correctly check 'type'
                    return data # type: ignore
        return None
    
    
    def get_edge_data_by_key(self, source: str, target: str, key: str) -> Optional[Dict[str, Any]]:
        """Get edge data for a specific source, target, and MultiDiGraph key."""
        if not self.graph or not self.graph.has_edge(source, target, key=key):
            return None
        return self.graph.get_edge_data(source, target, key=key)
    
    
    # def get_edges(self,
    #               source: Optional[str] = None,
    #               target: Optional[str] = None, 
    #               edge_type: Optional[Union[str, List[str]]] = None, # This is the SEMANTIC edge type
    #               properties: Optional[Dict[str, Any]] = None) -> List[Tuple[str, str, Dict[str, Any]]]:
    #     """
    #     Get edges from the graph, optionally filtered by source, target, edge type(s), and properties.
        
    #     Args:
    #         source: Optional source node ID to filter by
    #         target: Optional target node ID to filter by
    #         edge_type: Optional edge type to filter by
    #         properties: Optional dictionary of property name-value pairs to filter by
            
    #     Returns:
    #         List of (source, target, edge_attributes) tuples
    #     """
        
    #     # 1. Handle Cache Key Generation for list of edge_types
    #     edge_type_for_cache_key: Any = edge_type
    #     if isinstance(edge_type, list):
    #         # Use a sorted tuple of types for a canonical cache key part
    #         edge_type_for_cache_key = tuple(sorted(list(set(edge_type)))) # Deduplicate and sort

    #     # Ensure properties are hashable for cache key
    #     prop_key_part = None
    #     if properties:
    #         try:
    #             prop_key_part = hash(frozenset(properties.items()))
    #         except TypeError: # Handle unhashable items in properties, e.g. dicts within dicts
    #             prop_key_part = hash(str(properties)) # Fallback to string representation
        
    #     cache_key = (f"get_edges_v3:{source}:{target}:{edge_type_for_cache_key}:{prop_key_part}")
        
    #     if self.enable_caching:
    #         cached = self._get_from_cache(cache_key)
    #         if cached is not None: return cached

    #     start_time = time.perf_counter()
    #     collected_edges: List[Tuple[str, str, Dict[str, Any]]] = []
        
    #     # 2. Normalize edge_type to a list for iteration
    #     types_to_iterate: List[Optional[str]]
    #     if edge_type is None: types_to_iterate = [None] # Iterate once with no type filter
    #     elif isinstance(edge_type, str): types_to_iterate = [edge_type]
    #     elif isinstance(edge_type, list):
    #         types_to_iterate = list(set(edge_type)) # Deduplicate types to check
    #         if not types_to_iterate: # Empty list of types means no types to match
    #             if self.enable_caching: self._add_to_cache(cache_key, [])
    #             return []
    #     else:
    #         logger.warning(f"Invalid edge_type format: {edge_type}. Returning empty list.")
    #         if self.enable_caching: self._add_to_cache(cache_key, [])
    #         return []
        

    #     for current_semantic_type_filter in types_to_iterate: 
    #         temp_edges_for_this_type: List[Tuple[str, str, Dict[str, Any]]] = []
    #         # Branch 1: Source node specified
    #         if source is not None:
    #             if not self.graph.has_node(source): continue
    #             # For source-based, we iterate graph.out_edges as primary strategy,
    #             # as _source_edge_index is {source: {sem_type: {target: {nx_key}}}}.
    #             # If current_semantic_type_filter is None, we check all outgoing edges' 'edge_type' attribute.
    #             # If current_semantic_type_filter is specified, we can be more direct if indexed,
    #             # or filter during iteration if not.
                
    #             # Option A: Use refined index lookup for source, semantic_type, target
    #             # This requires _source_edge_index[source][semantic_type][target] to yield keys.
    #             # For now, let's stick to iterating graph edges and filtering for max clarity given index structure.

    #             # logger.debug(f"[GS_GET_EDGES] Source-based: source='{source}', semantic_type_filter='{current_semantic_type_filter}'")
    #             for s_node, t_node, nx_key, data_dict in self.graph.out_edges(source, data=True, keys=True):
    #                 if target is not None and t_node != target:
    #                     continue # Skip if target is specified and doesn't match

    #                 actual_semantic_type_on_edge = data_dict.get("edge_type")
    #                 if current_semantic_type_filter is not None and actual_semantic_type_on_edge != current_semantic_type_filter:
    #                     continue # Skip if semantic type filter is given and doesn't match attribute

    #                 if self._matches_properties(data_dict, properties):
    #                     temp_edges_for_this_type.append((s_node, t_node, data_dict.copy()))
            
    #         # Branch 2: Target node specified (and source is None)
    #         elif target is not None:
    #             if not self.graph.has_node(target): continue
    #             # logger.debug(f"[GS_GET_EDGES] Target-based: target='{target}', semantic_type_filter='{current_semantic_type_filter}'")
    #             for s_node, t_node, nx_key, data_dict in self.graph.in_edges(target, data=True, keys=True):
    #                 # source is None here, so no s_node check needed against a 'source' parameter
    #                 actual_semantic_type_on_edge = data_dict.get("edge_type")
    #                 if current_semantic_type_filter is not None and actual_semantic_type_on_edge != current_semantic_type_filter:
    #                     continue
                    
    #                 if self._matches_properties(data_dict, properties):
    #                     temp_edges_for_this_type.append((s_node, t_node, data_dict.copy()))

    #         # Branch 3: Only semantic edge_type specified (source and target are None)
    #         elif current_semantic_type_filter is not None:
    #             # Here we can potentially use _edge_type_index[semantic_type] which stores (s, t, nx_key)
    #             use_index = (self.enable_indices and current_semantic_type_filter in self._edge_type_index)
    #             if use_index:
    #                 # logger.debug(f"[GS_GET_EDGES] Using _edge_type_index for semantic_type='{current_semantic_type_filter}'.")
    #                 # _edge_type_index stores (source, target, actual_networkx_key)
    #                 for s_node, t_node, nx_key in self._edge_type_index[current_semantic_type_filter]:
    #                     if self.graph.has_edge(s_node, t_node, key=nx_key): # Ensure edge still exists
    #                         edge_data = self.graph[s_node][t_node][nx_key]
    #                         # The index is already filtered by semantic type, so edge_data.get("edge_type") should match.
    #                         if self._matches_properties(edge_data, properties):
    #                             temp_edges_for_this_type.append((s_node, t_node, edge_data.copy()))
    #                     else:
    #                         logger.warning(f"[GS_GET_EDGES] Edge ({s_node})->({t_node}) key '{nx_key}' from index not found in graph. Stale index?")
    #             else: # Fallback
    #                 # logger.debug(f"[GS_GET_EDGES] Fallback iteration for semantic_type='{current_semantic_type_filter}'.")
    #                 for s_node, t_node, _, data_dict in self.graph.edges(data=True, keys=True): # Iterate all edges
    #                     if data_dict.get("edge_type") == current_semantic_type_filter:
    #                         if self._matches_properties(data_dict, properties):
    #                             temp_edges_for_this_type.append((s_node, t_node, data_dict.copy()))
            
    #         # Branch 4: No filters at all (source, target, edge_type are all None)
    #         else: # current_type_filter is None (because original edge_type was None)
    #             # logger.debug(f"[GS_GET_EDGES] No filters. Iterating all graph edges.")
    #             for s_node, t_node, _, data_dict in self.graph.edges(data=True, keys=True):
    #                 if self._matches_properties(data_dict, properties):
    #                     temp_edges_for_this_type.append((s_node, t_node, data_dict.copy()))
            
    #         collected_edges.extend(temp_edges_for_this_type)

    #     # Deduplicate (important if types_to_iterate had multiple entries that could lead to the same edge instance)
    #     # However, if an edge (u,v,k,data) is added, it should only be added once to collected_edges
    #     # unless the logic above has flaws. A simple set comprehension can ensure uniqueness if needed.
    #     # final_edges = list(set(collected_edges)) # This would break if data dicts are not hashable
        
    #     # A more robust deduplication if order doesn't matter:
    #     unique_edges_by_id = {}
    #     for s, t, data in collected_edges:
    #         # Create a unique identifier for the edge instance if possible (e.g. using its NetworkX key if available and relevant)
    #         # For now, assume (s, t, frozenset(data.items())) for MultiDiGraph might be too complex if data contains unhashables
    #         # The iteration logic should prevent duplicates if each edge is processed once per unique current_type_filter.
    #         # The main risk of duplicates is if types_to_iterate had redundant entries (now handled by set())
    #         # or if a single edge could match multiple `current_type_filter` values if `edge_type` was a list
    #         # and filtering was not perfect.
    #         # The current logic iterates types_to_iterate; if an edge matches a type, it's added.
    #         # If an edge has multiple 'edge_type' attributes (not standard) or matches multiple filters, it could be duplicated.
    #         # Given we store one semantic 'edge_type' per edge, this should be fine.
    #         pass # No explicit deduplication here, assuming iteration logic handles it for now.
    #     final_edges = collected_edges


    #     if self.enable_caching: self._add_to_cache(cache_key, final_edges)

    #     duration = (time.perf_counter() - start_time) * 1000
    #     log_key_for_perf = ('get_edges', str(source), str(target), str(edge_type_for_cache_key)) # Make hashable
    #     if log_key_for_perf not in self.query_times: 
    #         self.query_times[log_key_for_perf] = []
    #     self.query_times[log_key_for_perf].append(duration)
        
    #     # logger.debug(f"get_edges (source='{source}', target='{target}', type(s)='{edge_type_for_cache_key}') query took {duration:.2f} ms, found {len(final_edges)} edges.")
    #     return final_edges


    def get_edges(self,
                  source: Optional[str] = None,
                  target: Optional[str] = None,
                  edge_type: Optional[str] = None, # Semantic type (data['type'])
                  properties: Optional[Dict[str, Any]] = None
                  ) -> Generator[Tuple[str, str, str, Dict[str, Any]], None, None]: # u, v, key, data
        """
        Generator that yields all edges matching the given criteria.
        Each yielded item is a tuple: (source_node, target_node, edge_key, edge_data_dict).
        """
        
        # --- START DIAGNOSTIC CONFIGURATION ---
        # Set to True to force this method to bypass index usage and use direct graph iteration.
        # Set to False for normal operation (allowing index usage if self.enable_indices is True).
        _FORCE_FALLBACK_PATH_FOR_DIAGNOSTIC = False 
        # --- END DIAGNOSTIC CONFIGURATION ---

        if not isinstance(self.graph, nx.MultiDiGraph):
            logger.critical("[GraphStore.get_edges] Graph not initialized or not a MultiDiGraph!")
            return 
            
        nodes_to_iterate_for_nx = None
        if source: # If source is specified, NetworkX can optimize its iteration start point.
            nodes_to_iterate_for_nx = source
        
        logger.debug(
            f"[GS_GET_EDGES] Query: src='{source}', tgt='{target}', semantic_type='{edge_type}'. "
            f"NX nbunch for iteration: '{nodes_to_iterate_for_nx}'. Properties filter: {properties}. "
            f"Force fallback (diagnostic): {_FORCE_FALLBACK_PATH_FOR_DIAGNOSTIC}."
        )

        edges_yielded_count = 0
        
        # Determine if the optimized indexed path should be attempted.
        # It's attempted if:
        # 1. Indices are globally enabled (self.enable_indices)
        # 2. The diagnostic override to force fallback is OFF
        # 3. An edge_type is provided (as indices are primarily keyed by edge_type)
        attempt_indexed_path = self.enable_indices and not _FORCE_FALLBACK_PATH_FOR_DIAGNOSTIC and edge_type is not None

        if attempt_indexed_path:
            logger.debug(f"[GS_GET_EDGES_IDX_尝试] Attempting to use index for edge_type='{edge_type}'") # "尝试" means "attempt"
            indexed_path_taken_and_completed = False # Flag to indicate if we successfully used an index path
            
            # Case 1: source, target, and edge_type provided (most specific)
            if source and target:
                if source in self._source_edge_index and \
                   edge_type in self._source_edge_index[source] and \
                   target in self._source_edge_index[source][edge_type]:
                    logger.debug(f"[GS_GET_EDGES_IDX] Using S->T index for ({source})-[{edge_type}]->({target})")
                    for actual_key in list(self._source_edge_index[source][edge_type][target]): # Iterate copy
                        try:
                            edge_data = self.graph[source][target][actual_key]
                            if self._matches_properties(edge_data, properties):
                                yield source, target, actual_key, edge_data
                                edges_yielded_count += 1
                        except KeyError:
                            logger.warning(f"[GS_GET_EDGES_IDX_FAIL] Indexed key '{actual_key}' for ({source})->({target}) type '{edge_type}' not found in graph. Index might be stale.")
                    indexed_path_taken_and_completed = True
                else:
                    logger.debug(f"[GS_GET_EDGES_IDX] No S->T index entry for ({source})-[{edge_type}]->({target})")
            
            # Case 2: source and edge_type provided (no target)
            elif source: # and not target (implicitly)
                if source in self._source_edge_index and edge_type in self._source_edge_index[source]:
                    logger.debug(f"[GS_GET_EDGES_IDX] Using S->any index for ({source})-[{edge_type}]->(*)")
                    for tgt_node, keys_set in self._source_edge_index[source][edge_type].items():
                        for actual_key in list(keys_set):
                            try:
                                edge_data = self.graph[source][tgt_node][actual_key]
                                if self._matches_properties(edge_data, properties):
                                    yield source, tgt_node, actual_key, edge_data
                                    edges_yielded_count += 1
                            except KeyError:
                                logger.warning(f"[GS_GET_EDGES_IDX_FAIL] Indexed key '{actual_key}' for ({source})->({tgt_node}) type '{edge_type}' not found in graph. Index might be stale.")
                    indexed_path_taken_and_completed = True
                else:
                    logger.debug(f"[GS_GET_EDGES_IDX] No S->any index entry for ({source})-[{edge_type}]->(*)")

            # Case 3: target and edge_type provided (no source)
            elif target: # and not source (implicitly)
                if target in self._target_edge_index and edge_type in self._target_edge_index[target]:
                    logger.debug(f"[GS_GET_EDGES_IDX] Using any->T index for (*)-[{edge_type}]->({target})")
                    for src_node, keys_set in self._target_edge_index[target][edge_type].items():
                        for actual_key in list(keys_set):
                            try:
                                edge_data = self.graph[src_node][target][actual_key]
                                if self._matches_properties(edge_data, properties):
                                    yield src_node, target, actual_key, edge_data
                                    edges_yielded_count += 1
                            except KeyError:
                                logger.warning(f"[GS_GET_EDGES_IDX_FAIL] Indexed key '{actual_key}' for ({src_node})->({target}) type '{edge_type}' not found in graph. Index might be stale.")
                    indexed_path_taken_and_completed = True
                else:
                     logger.debug(f"[GS_GET_EDGES_IDX] No any->T index entry for (*)-[{edge_type}]->({target})")

            # Case 4: only edge_type provided (no source, no target)
            else: 
                if edge_type in self._edge_type_index:
                    logger.debug(f"[GS_GET_EDGES_IDX] Using edge_type_only index for type '{edge_type}'")
                    for s_node_idx, t_node_idx, actual_key_idx in list(self._edge_type_index[edge_type]):
                        try:
                            edge_data = self.graph[s_node_idx][t_node_idx][actual_key_idx]
                            if self._matches_properties(edge_data, properties): # Apply property filter even for general type index
                                yield s_node_idx, t_node_idx, actual_key_idx, edge_data
                                edges_yielded_count += 1
                        except KeyError:
                            logger.warning(f"[GS_GET_EDGES_IDX_FAIL] Indexed key '{actual_key_idx}' for ({s_node_idx})->({t_node_idx}) type '{edge_type}' not found in graph. Index might be stale.")
                    indexed_path_taken_and_completed = True
                else:
                    logger.debug(f"[GS_GET_EDGES_IDX] No edge_type_only index entry for type '{edge_type}'")

            if indexed_path_taken_and_completed:
                logger.debug(f"[GS_GET_EDGES_IDX_路径完成] Finished indexed path. Yielded {edges_yielded_count} edges.") # "路径完成" means "path completed"
                return # If any indexed path was taken and completed, we assume it's exhaustive for that query type.
            else:
                logger.debug(f"[GS_GET_EDGES_IDX] Indexed path attempted but no specific index case matched fully or yielded results; proceeding to fallback if necessary.")


        # Fallback: Iterate graph edges if indices are disabled, diagnostic override is on, or no suitable index was hit.
        logger.debug(f"[GS_GET_EDGES_FALLBACK] Using direct graph iteration. NX nbunch: '{nodes_to_iterate_for_nx}'")
        
        # NX `edges` method iterates efficiently based on nbunch.
        # If nbunch is a single node, it iterates edges incident to that node.
        # If nbunch is None, it iterates all edges.
        edge_iterator = self.graph.edges(nbunch=nodes_to_iterate_for_nx, data=True, keys=True)

        for s_node, t_node, k_edgekey, edge_data_dict in edge_iterator:
            # Filter 1: Source node (if nbunch wasn't specific enough or target was primary filter)
            if source is not None and s_node != source:
                continue
            # Filter 2: Target node
            if target is not None and t_node != target:
                continue
            
            # Filter 3: Semantic edge_type (stored in edge_data_dict['type'])
            if edge_type is not None and edge_data_dict.get("type") != edge_type:
                continue
            
            # Filter 4: Custom properties
            if not self._matches_properties(edge_data_dict, properties):
                continue
            
            # If all filters pass, yield the edge
            yield s_node, t_node, k_edgekey, edge_data_dict
            edges_yielded_count +=1
        
        logger.debug(f"[GS_GET_EDGES_FALLBACK] Finished fallback iteration. Yielded {edges_yielded_count} edges for query '{source=}, {target=}, {edge_type=}'.")


    
    # def _matches_properties(self, edge_data: Dict, properties: Optional[Dict[str, Any]]) -> bool:
    #     if not properties:
    #         return True
    #     for key, value in properties.items():
    #         if edge_data.get(key) != value:
    #             return False
    #     return True

    def _matches_properties(self, edge_data: Dict, properties: Optional[Dict[str, Any]]) -> bool:
        """
        Check if the edge_data matches all specified properties.
        The 'type' attribute is handled by the edge_type parameter in get_edges.
        This method should check other properties.
        """
        if not properties:
            return True
        for prop_key, prop_value in properties.items():
            # If the property to match is 'type', it should have been handled by 'edge_type' param.
            # However, to be robust, if 'type' is in 'properties', it must match edge_data.get('type').
            if prop_key == 'type':
                if edge_data.get('type') != prop_value:
                    return False
            elif edge_data.get(prop_key) != prop_value:
                # Special handling for nested dicts, e.g. 'metadata'
                if isinstance(prop_value, dict) and isinstance(edge_data.get(prop_key), dict):
                    # For now, only support exact match for nested dicts if this becomes a requirement.
                    # A simple check:
                    if edge_data.get(prop_key) != prop_value: #This will compare the dicts
                        # For more complex nested matching, a recursive helper would be needed.
                        # Example: return self._matches_nested_properties(edge_data.get(prop_key), prop_value)
                        return False 
                else: # Simple value comparison
                    return False
        return True
    
    # def get_successors(self, node_id: str, edge_type: Optional[str] = None) -> List[Tuple[str, Dict[str, Any]]]:
    #     """
    #     Get all nodes that are targets of edges from the given node.
        
    #     Args:
    #         node_id: Source node ID
    #         edge_type: Optional edge type filter
            
    #     Returns:
    #         List of tuples (target_id, edge_attributes)
    #     """
    #     # Check cache first
    #     if self.enable_caching:
    #         cache_key = f"get_successors:{node_id}:{edge_type}"
    #         cached = self._get_from_cache(cache_key)
    #         if cached is not None:
    #             return cached
                
    #     start_time = time.time()
    #     result = []
        
    #     if node_id not in self.graph:
    #         return []
            
    #     edges_data = self.get_edges(source=node_id, edge_type=edge_type)
    #     result = [(target, data) for _, target, data in edges_data]
                
    #     # Track query time for performance analysis
    #     query_time = time.time() - start_time
    #     self.query_times["get_successors"].append(query_time)
        
    #     # Cache the result if appropriate
    #     if self.enable_caching:
    #         self._add_to_cache(cache_key, result)
                
    #     return result
    
    
    def get_successors(self, node_id: str, edge_type: Optional[str] = None) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Get all nodes that are targets of edges from the given node.

        Args:
            node_id: Source node ID
            edge_type: Optional edge type filter

        Returns:
            List of tuples (target_id, edge_attributes_dict for the specific edge)
        """
        # Check cache first
        if self.enable_caching:
            cache_key = f"get_successors:{node_id}:{edge_type}"
            cached = self._get_from_cache(cache_key)
            if cached is not None:
                return cached # type: ignore

        start_time = time.time()
        result = []

        if not self.graph or node_id not in self.graph:
            logger.debug(f"[GS_SUCCESSORS] Node '{node_id}' not in graph.")
            return []

        # Use the updated get_edges method for consistency
        # get_edges yields (source, target, key, data)
        # We need (target, data)
        logger.debug(f"[GS_SUCCESSORS] Getting successors for '{node_id}', type filter: '{edge_type}'")
        for _, target_node, _, edge_data in self.get_edges(source=node_id, edge_type=edge_type):
            result.append((target_node, edge_data))
        
        if self.log_performance:
            duration = (time.time() - start_time) * 1000
            self.query_times[f"get_successors_{edge_type or 'any'}"].append(duration)
            logger.debug(f"[GS_SUCCESSORS] Found {len(result)} successors for '{node_id}' (type: {edge_type}) in {duration:.2f}ms.")

        if self.enable_caching:
            self._add_to_cache(cache_key, result)
        return result
    
    
    # def get_predecessors(self, node_id: str, edge_type: Optional[str] = None) -> List[Tuple[str, Dict[str, Any]]]:
    #     """
    #     Get all nodes that have edges pointing to the given node.
        
    #     Args:
    #         node_id: Target node ID
    #         edge_type: Optional edge type filter
            
    #     Returns:
    #         List of tuples (source_id, edge_attributes)
    #     """
    #     # Check cache first
    #     if self.enable_caching:
    #         cache_key = f"get_predecessors:{node_id}:{edge_type}"
    #         cached = self._get_from_cache(cache_key)
    #         if cached is not None:
    #             return cached
                
    #     start_time = time.time()
    #     result = []
        
    #     if node_id not in self.graph:
    #         return []
            
    #     edges_data = self.get_edges(target=node_id, edge_type=edge_type)
    #     result = [(source, data) for source, _, data in edges_data]
                
    #     # Track query time for performance analysis
    #     query_time = time.time() - start_time
    #     self.query_times["get_predecessors"].append(query_time)
        
    #     # Cache the result if appropriate
    #     if self.enable_caching:
    #         self._add_to_cache(cache_key, result)
                
    #     return result
    
    
    def get_predecessors(self, node_id: str, edge_type: Optional[str] = None) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Get all nodes that are sources of edges to the given node.

        Args:
            node_id: Target node ID
            edge_type: Optional edge type filter

        Returns:
            List of tuples (source_id, edge_attributes_dict for the specific edge)
        """
        # Check cache first
        if self.enable_caching:
            cache_key = f"get_predecessors:{node_id}:{edge_type}"
            cached = self._get_from_cache(cache_key)
            if cached is not None:
                return cached # type: ignore

        start_time = time.time()
        result = []
        if not self.graph or node_id not in self.graph:
            logger.debug(f"[GS_PREDECESSORS] Node '{node_id}' not in graph.")
            return []

        # Use the updated get_edges method for consistency
        # get_edges yields (source, target, key, data)
        # We need (source, data)
        logger.debug(f"[GS_PREDECESSORS] Getting predecessors for '{node_id}', type filter: '{edge_type}'")
        for source_node, _, _, edge_data in self.get_edges(target=node_id, edge_type=edge_type):
            result.append((source_node, edge_data))

        if self.log_performance:
            duration = (time.time() - start_time) * 1000
            self.query_times[f"get_predecessors_{edge_type or 'any'}"].append(duration)
            logger.debug(f"[GS_PREDECESSORS] Found {len(result)} predecessors for '{node_id}' (type: {edge_type}) in {duration:.2f}ms.")

        if self.enable_caching:
            self._add_to_cache(cache_key, result)
        return result
    
    
    def has_node(self, node_id: str) -> bool:
        """Check if a node exists in the graph."""
        return self.graph.has_node(node_id)
        
    
    def has_edge(self, source: str, target: str, 
                 edge_type: Optional[str] = None, # Semantic type (can be None if checking by key only)
                 key: Optional[str] = None) -> bool:     # Explicit MultiDiGraph key
        """
        Check if an edge exists between source and target, optionally filtered by
        semantic edge_type and/or specific MultiDiGraph key.

        If 'key' is provided, it checks for an edge with that specific MultiDiGraph key.
        If 'edge_type' is also provided (and is different from the key, or provides clarity),
        it further verifies that the found edge has an attribute 'edge_type' matching the provided semantic edge_type.

        If only 'edge_type' is provided (and 'key' is None), 'edge_type' is used as the
        key for the NetworkX has_edge check. The edge found must also have its 'edge_type' attribute match this.

        If neither 'key' nor 'edge_type' is provided, it checks if ANY edge exists
        between source and target.

        Args:
            source: Source node ID.
            target: Target node ID.
            edge_type: Optional semantic edge type to check as an attribute on the edge,
                       or to use as a key if 'key' is None.
            key: Optional specific MultiDiGraph key for the edge.

        Returns:
            True if a matching edge exists, False otherwise.
        """
        if not self.graph or not self.graph.has_node(source) or not self.graph.has_node(target):
            return False

        if key is not None: # If a specific MultiDiGraph key is provided
            if not self.graph.has_edge(source, target, key=key):
                return False
            # If key is found, optionally also check if its semantic type matches edge_type
            if edge_type is not None:
                edge_data = self.graph.get_edge_data(source, target, key=key)
                if not edge_data or edge_data.get('type') != edge_type: # Correctly check 'type'
                    return False
            return True # Edge with this key exists (and type matches if specified)
        
        # No key provided, check based on edge_type if specified, or any edge if edge_type is None
        if not self.graph.has_edge(source, target): # Check if any edge exists at all
             return False

        # Iterate over all edges between source and target
        # self.graph[source][target] is a dict of {key: data_dict}
        for k, data in self.graph[source][target].items():
            if edge_type is None: # If no type specified, any edge is a match
                return True
            if data.get('type') == edge_type: # Correctly check 'type'
                return True
        return False
    
        
    def remove_node(self, node_id: str) -> bool:
        """
        Remove a node and all its connected edges from the graph.
        
        Args:
            node_id: The ID of the node to remove
            
        Returns:
            True if node was found and removed, False if not found
        """
        if node_id not in self.graph:
            return False
        
        # Collect connected edges before removing the node
        connected_edges = []
        for s, t, data in list(self.graph.in_edges(node_id, data=True)) + list(self.graph.out_edges(node_id, data=True)):
            edge_type = data.get("edge_type")
            connected_edges.append((s, t, edge_type))
        
        # Remove the node from the graph (this also removes all connected edges)
        self.graph.remove_node(node_id)
        
        # Update indices if enabled
        if self.enable_indices:
            # Safe index update by iterating over copies
            
            # 1. Clean edge type index
            for edge_type in list(self._edge_type_index.keys()):
                # Use a set comprehension to create a new filtered set
                self._edge_type_index[edge_type] = {
                    (s, t) for s, t in self._edge_type_index[edge_type]
                    if s != node_id and t != node_id
                }
                # Remove empty sets
                if not self._edge_type_index[edge_type]:
                    del self._edge_type_index[edge_type]
            
            # 2. Clean source edge index
            if node_id in self._source_edge_index:
                del self._source_edge_index[node_id]
                
            # Remove node from target indices (when it was a target)
            for s in list(self._source_edge_index.keys()):
                for edge_type in list(self._source_edge_index[s].keys()):
                    # Use a set comprehension for safe filtering
                    self._source_edge_index[s][edge_type] = {t for t in self._source_edge_index[s][edge_type] if t != node_id}
                    # Clean up empty entries
                    if not self._source_edge_index[s][edge_type]:
                        del self._source_edge_index[s][edge_type]
                if not self._source_edge_index[s]:
                    del self._source_edge_index[s]
            
            # 3. Clean target edge index
            if node_id in self._target_edge_index:
                del self._target_edge_index[node_id]
                
            # Remove node from source indices (when it was a source)
            for t in list(self._target_edge_index.keys()):
                for edge_type in list(self._target_edge_index[t].keys()):
                    # Use a set comprehension for safe filtering
                    self._target_edge_index[t][edge_type] = {s for s in self._target_edge_index[t][edge_type] if s != node_id}
                    # Clean up empty entries
                    if not self._target_edge_index[t][edge_type]:
                        del self._target_edge_index[t][edge_type]
                if not self._target_edge_index[t]:
                    del self._target_edge_index[t]
            
            # 4. Clean node attribute indices
            for attr_name in self._node_attr_index:
                for attr_value in list(self._node_attr_index[attr_name].keys()):
                    self._node_attr_index[attr_name][attr_value].discard(node_id)
                    if not self._node_attr_index[attr_name][attr_value]:
                        del self._node_attr_index[attr_name][attr_value]
                if not self._node_attr_index[attr_name]:
                    del self._node_attr_index[attr_name]
        
        # Invalidate affected caches
        if self.enable_caching:
            # Invalidate caches for the node itself
            self._invalidate_node_caches(node_id)
            
            # Also invalidate caches for connected edges
            for s, t, edge_type in connected_edges:
                self._invalidate_edge_caches(s, t, edge_type)
        
        return True
            
    
    # def remove_edge(self, source: str, target: str, 
    #                 edge_type: Optional[str] = None, # Semantic type to filter by
    #                 key: Optional[str] = None # Specific MultiDiGraph key
    #                 ) -> bool:
    #     """
    #     Remove an edge between two nodes.
        
    #     Args:
    #         source: Source node ID
    #         target: Target node ID
    #         edge_type: Optional edge type to match
    #         key: Optional specific edge key to match
            
    #     Returns:
    #         bool: True if edge was removed, False if not found
    #     """
        
    #     if self._batch_mode:
    #         logger.warning("remove_edge called during batch mode. This operation is not batched. Removing directly.")
        
    #     if not self.graph.has_node(source) or not self.graph.has_node(target):
    #         logger.debug(f"Source or target node not found for edge removal: {source} -> {target}")
    #         return False

    #     actual_key_to_remove = key if key is not None else edge_type
    #     if actual_key_to_remove is None:
    #         # This case means both 'key' and 'edge_type' were None.
    #         # Removing an edge from MultiDiGraph without a key removes *all* edges between source and target.
    #         # This is likely not intended by callers who usually mean to remove a specific type of edge.
    #         logger.error("[GS_REMOVE_EDGE] Cannot remove edge: A specific 'key' or an 'edge_type' (to act as default key) must be provided to identify the edge in the MultiDiGraph.")
    #         return False

    #     logger.debug(f"[GS_REMOVE_EDGE] Attempting to remove edge: ({source})--[NXKey:'{actual_key_to_remove}']-->({target}) (Semantic type for indexing: '{edge_type}')")
        
    #     if self.graph.has_edge(source, target, key=actual_key_to_remove):
    #         # Before removing, get the attributes to correctly update/remove from indices
    #         # The 'edge_type' for indexing purposes is stored in the attributes.
    #         edge_data_for_index = self.graph[source][target][actual_key_to_remove].copy()
    #         # The semantic type for indexing is stored in the 'edge_type' attribute of the edge.
    #         # Fallback to actual_key_to_remove if 'edge_type' attribute is somehow missing.
    #         semantic_type_for_indexing = edge_data_for_index.get('edge_type', actual_key_to_remove)

    #         try:
    #             self.graph.remove_edge(source, target, key=actual_key_to_remove) # THE NetworkX REMOVAL
                
    #             # DIAGNOSTIC CHECK:
    #             if self.graph.has_edge(source, target, key=actual_key_to_remove):
    #                 logger.error(f"[GS_REMOVE_EDGE_DIAG] Edge ({source})--[NXKey:'{actual_key_to_remove}']-->({target}) STILL EXISTS in NetworkX graph immediately after remove_edge call!")
    #             else:
    #                 logger.info(f"[GS_REMOVE_EDGE_DIAG] Edge ({source})--[NXKey:'{actual_key_to_remove}']-->({target}) successfully removed from NetworkX graph.")

    #         except Exception as e_nx_remove: # Should not happen if has_edge was true, but defensive
    #             logger.error(f"[GS_REMOVE_EDGE] NetworkX error during remove_edge for ({source})--[NXKey:'{actual_key_to_remove}']-->({target}): {e_nx_remove}", exc_info=True)
    #             return False # Removal failed at NetworkX level
            
    #         if self.enable_indices:
    #             # Use the determined semantic_type_for_indexing and the actual_networkx_key for index removal
    #             self._remove_from_edge_indices(source, target, semantic_type_for_indexing, actual_key_to_remove)
            
    #         if self.enable_caching:
    #             # Invalidate caches based on the semantic type and the specific key
    #             self._invalidate_edge_caches(source, target, semantic_type_for_indexing, actual_key_to_remove)
            
    #         self._graph_changed = True
    #         logger.debug(f"[GS_REMOVE_EDGE] Edge ({source})--[SemType:'{semantic_type_for_indexing}', NXKey:'{actual_key_to_remove}']-->({target}) processed for removal from GraphStore.")
    #         return True # Indicates an attempt was made and no error thrown by NX, and edge was initially present.
    #     else:
    #         logger.debug(f"[GS_REMOVE_EDGE] Edge ({source})->({target}) with NetworkX key '{actual_key_to_remove}' not found for removal.")
    #         return False
    
    
    def remove_edge(self, source: str, target: str,
                    edge_type: Optional[str] = None,
                    key: Optional[str] = None) -> bool:
        logger.info(f"[GraphStore.remove_edge] CALLED. Target: {source}->{target}, Type: {edge_type}, Key: {key}")
        if self.graph is None: 
            logger.error("[GraphStore.remove_edge] Graph not initialized!")
            return False

        if key:
            logger.debug(f"[GraphStore.remove_edge] Key provided: {key}")
            if self.graph.has_edge(source, target, key=key):
                logger.info(f"[GraphStore.remove_edge] Edge {source}->{target} (Key: {key}) EXISTS according to has_edge.")
                edge_data = self.graph.get_edge_data(source, target, key=key)
                if not edge_data: # Should not happen if has_edge(..., key=key) is true
                    logger.error(f"[GraphStore.remove_edge] Edge {source}->{target} (Key: {key}) found by has_edge but get_edge_data returned None. Inconsistency.")
                    return False

                actual_semantic_type = edge_data.get('type') # Correctly get 'type'
                logger.debug(f"[GraphStore.remove_edge] Edge actual semantic type: {actual_semantic_type}. Expected type for check (if provided): {edge_type}")

                if edge_type and actual_semantic_type != edge_type:
                    logger.warning(f"[GraphStore.remove_edge] Edge {source}->{target} (Key: {key}) semantic type mismatch. "
                                   f"Expected: {edge_type}, Actual: {actual_semantic_type}. NOT REMOVING.")
                    return False 
                
                try:
                    logger.info(f"[GraphStore.remove_edge] EXECUTING NetworkX remove_edge for {source}->{target} (Key: {key})")
                    # Before removing, capture details for index removal
                    # The 'relationship_type' for indexing is the semantic type stored in edge_data.get('type')
                    type_for_index_removal = actual_semantic_type or "UNKNOWN" # Fallback if 'type' wasn't on edge_data

                    self.graph.remove_edge(source, target, key=key)
                    
                    if self.enable_indices:
                         self._remove_from_edge_indices(source, target, type_for_index_removal, key)
                    if self.enable_caching:
                         self._invalidate_edge_caches(source, target, type_for_index_removal, key)
                    self._graph_changed = True
                    
                    if not self.graph.has_edge(source, target, key=key):
                        logger.info(f"[GraphStore.remove_edge] SUCCESSFULLY REMOVED {source}->{target} (Key: {key}). Verified by has_edge immediately after removal.")
                        return True
                    else:
                        logger.error(f"[GraphStore.remove_edge] FAILED TO REMOVE {source}->{target} (Key: {key}) DESPITE NO ERROR from nx.remove_edge. Edge still exists according to has_edge!")
                        if self.graph.has_node(source) and self.graph.has_node(target) and source in self.graph and target in self.graph[source]:
                             edges_between = self.graph[source][target]
                             logger.error(f"[GraphStore.remove_edge] Current edges between {source} and {target}: {edges_between}")
                        else:
                             logger.error(f"[GraphStore.remove_edge] Nodes {source} or {target} or path between them might not exist for detailed edge listing.")
                        return False 
                except nx.NetworkXError as e:
                    logger.error(f"[GraphStore.remove_edge] NetworkXError when removing edge {source}->{target} (Key: {key}): {e}")
                    return False
            else:
                logger.warning(f"[GraphStore.remove_edge] Edge {source}->{target} with Key: {key} NOT FOUND by has_edge at the start.")
                return False
        else: 
            logger.warning(f"[GraphStore.remove_edge] Key NOT provided for {source}->{target}. Type-based removal attempt (edge_type: {edge_type}).")
            if not edge_type:
                logger.error("[GraphStore.remove_edge] Cannot remove edge without key or edge_type.")
                return False

            keys_to_remove = []
            if self.graph.has_edge(source, target): 
                for k, data_dict in self.graph[source][target].items():
                    if data_dict.get('type') == edge_type: # Correctly check 'type'
                        keys_to_remove.append(k)
            
            if not keys_to_remove:
                logger.warning(f"[GraphStore.remove_edge] No edges found matching type '{edge_type}' between {source} and {target} for removal without key.")
                return False
            
            logger.info(f"[GraphStore.remove_edge] Found keys {keys_to_remove} for type '{edge_type}' between {source} and {target}. Attempting removal.")
            removed_any = False
            for k_to_remove in keys_to_remove:
                try:
                    # For index removal, we need the semantic type, which is `edge_type` in this branch
                    type_for_index_removal = edge_type 
                    self.graph.remove_edge(source, target, key=k_to_remove)
                    if self.enable_indices:
                        self._remove_from_edge_indices(source, target, type_for_index_removal, k_to_remove)
                    if self.enable_caching:
                        self._invalidate_edge_caches(source, target, type_for_index_removal, k_to_remove)
                    self._graph_changed = True
                    logger.info(f"[GraphStore.remove_edge] Removed edge {source}->{target} (Key: {k_to_remove}, Type: {edge_type})")
                    removed_any = True
                except nx.NetworkXError as e:
                    logger.error(f"[GraphStore.remove_edge] Failed to remove edge {source}->{target} (Key: {k_to_remove}, Type: {edge_type}): {e}")
            return removed_any

    
    def get_subgraph(self, node_ids: List[str]) -> 'GraphStore':
        """
        Create a new GraphStore containing only the specified nodes and their relationships.
        
        Args:
            node_ids: List of node IDs to include
            
        Returns:
            New GraphStore instance with the subgraph
        """
        subgraph_store = GraphStore(
            memory_threshold_mb=self.memory_threshold_mb,
            enable_indices=self.enable_indices,
            enable_caching=self.enable_caching
        )
        
        # Use NetworkX's subgraph method for nodes, then add edges manually to preserve keys/types
        valid_node_ids = [n for n in node_ids if self.graph.has_node(n)]
        temp_nx_subgraph = self.graph.subgraph(valid_node_ids) # This is a view
        
        subgraph_store.begin_batch()
        for node_id, attributes in temp_nx_subgraph.nodes(data=True):
            subgraph_store.add_node(node_id, **attributes) # add_node handles batching
                
        # Iterate original graph edges to ensure all parallel edges are considered with their keys
        for u, v, key, data in self.graph.edges(keys=True, data=True):
            if u in valid_node_ids and v in valid_node_ids:
                # key is the edge_type
                # data already contains 'edge_type' as an attribute as well from our add_edge
                subgraph_store.add_edge(u, v, edge_type=key, **data) 
                    
        subgraph_store.commit_batch()           
        return subgraph_store
    
    
    def clear(self) -> None:
        """Clear all nodes and edges from the graph."""
        self.graph.clear()
        
        # Clear indices and caches
        if self.enable_indices:
            self._edge_type_index.clear()
            self._source_edge_index.clear()
            self._target_edge_index.clear()
            self._node_attr_index.clear()
            
        if self.enable_caching:
            self._clear_cache()
    
    
    def node_count(self) -> int:
        """Get the total number of nodes in the graph."""
        return self.graph.number_of_nodes()
    
    
    # def edge_count(self, edge_type: Optional[str] = None) -> int:
    #     """
    #     Get the number of edges, optionally filtered by edge_type.
        
    #     Args:
    #         edge_type: Optional type of edge to count
            
    #     Returns:
    #         The number of matching edges
    #     """
    #     if edge_type: # Count edges of a specific semantic type
    #         if self.enable_indices and self._edge_type_index and edge_type in self._edge_type_index:
    #             return len(self._edge_type_index[edge_type])
    #         else: 
    #             count = 0
    #             # Iterate all edges with their keys and data to check 'edge_type' attribute
    #             # For MultiDiGraph, edges(data=True, keys=True) yields (u, v, key, data_dict)
    #             for _, _, _, data_dict in self.graph.edges(data=True, keys=True): 
    #                 if data_dict.get("edge_type") == edge_type:
    #                     count += 1
    #             return count
    #     else: 
    #         return self.graph.number_of_edges() # Total number of all edges (respects parallel edges)
    
    
    def edge_count(self, edge_type: Optional[str] = None) -> int:
        if self.graph is None: # Check if graph is None
            logger.warning("[GraphStore.edge_count] Graph not initialized, returning 0.")
            return 0
            
        if edge_type:
            if self.enable_indices and edge_type in self._edge_type_index: # If indices are enabled AND type is a key in the index
                return len(self._edge_type_index[edge_type])
            count = 0
            # logger.debug(f"[GraphStore.edge_count] Counting edges of type: {edge_type}. Graph has {self.graph.number_of_edges()} total edges.")
            for u, v, k, data in self.graph.edges(data=True, keys=True): # Iterate with keys to be safe
                logger.debug(f"[GraphStore.edge_count] Checking edge {u}->{v} (Key: {k}, Data: {data})")
                if data.get('type') == edge_type:
                    count += 1
            logger.debug(f"[GraphStore.edge_count] Count for type {edge_type} is {count}.")
            return count
        logger.debug(f"[GraphStore.edge_count] No edge_type specified, returning total_edges: {self.graph.number_of_edges()}.")
        return self.graph.number_of_edges()
    

    def relationship_count(self, edge_type: Optional[str] = None) -> Union[int, Dict[str, int]]:
        """
        Get the count of relationships by type.
        
        Args:
            edge_type: Optional specific edge type to count
            
        Returns:
            If edge_type is specified, returns the count for that type.
            Otherwise, returns a dictionary mapping types to counts.
        """
        # Check cache first when appropriate
        if self.enable_caching:
            cache_key = f"relationship_count:{edge_type if edge_type else 'all'}"
            cached = self._get_from_cache(cache_key)
            if cached is not None:
                return cached
        
        # Handle single edge type count
        if edge_type:
            # Use index if available (most efficient)
            if self.enable_indices and edge_type in self._edge_type_index:
                result = len(self._edge_type_index[edge_type])
                if self.enable_caching:
                    self._add_to_cache(cache_key, result)
                return result
            
            # Fallback to iteration
            count = 0
            for _, _, key, _ in self.graph.edges(keys=True, data=False):
                if key == edge_type:
                    count += 1
            if self.enable_caching:
                self._add_to_cache(cache_key, result)
            return count
        else: # Handle all edge types (return a dictionary)
            if self.enable_indices:
                # _edge_type_index keys are edge types, values are sets of (s,t) tuples
                result = {type_key: len(st_tuples) for type_key, st_tuples in self._edge_type_index.items()}
            else:
                counts = defaultdict(int)
                for _, _, key, _ in self.graph.edges(keys=True, data=False):
                    counts[key] += 1 # key is the edge_type
                result = dict(counts)
            
            if self.enable_caching:
                self._add_to_cache(cache_key, result)
            return result
    
    
    def get_node_ids(self) -> Set[str]:
        """Get the set of all node IDs in the graph."""
        return set(self.graph.nodes())
    
    def get_edge_types(self) -> Set[str]:
        """Get the set of all edge types used in the graph."""
        if self.enable_indices and self._edge_type_index: # Check if index has content
            return set(self._edge_type_index.keys())
        else:
            # Iterate over edge keys from the graph directly
            return {key for _, _, key in self.graph.edges(keys=True)} # data=False not needed
    
    
    def optimize(self, full: bool = False) -> None:
        """
        Optimize the graph store for better performance.
        
        Args:
            full: Whether to perform a full optimization (more expensive)
        """
        logger.info("Optimizing GraphStore...")
        start_time = time.time()
        
        current_memory_mb = self._get_memory_usage_mb()
        logger.info(f"Current memory usage: {current_memory_mb:.1f} MB")
        
        if self.enable_indices:
            self._rebuild_indices() # This already compacts index structures
            
        if self.enable_caching:
            self._clear_cache() # Clears query cache
            
        if full and current_memory_mb > self.memory_threshold_mb:
            logger.info("Performing full graph reconstruction for optimization...")
            # Create a new MultiDiGraph with the same data
            new_multi_graph = nx.MultiDiGraph()
            
            # Copy nodes
            for node_id, data in self.graph.nodes(data=True): # self.graph is the current MultiDiGraph
                new_multi_graph.add_node(node_id, **data)
                
            # Copy edges, preserving keys and all attributes
            for source, target, key, data in self.graph.edges(keys=True, data=True): # Iterate with keys
                new_multi_graph.add_edge(source, target, key=key, **data)
                
            # Replace the old graph
            self.graph = new_multi_graph # self.graph remains a MultiDiGraph
            logger.info(f"Graph reconstructed. New node count: {self.graph.number_of_nodes()}, New edge count: {self.graph.number_of_edges()}")
            
            # Rebuilding indices again after graph replacement is crucial if not done by default
            if self.enable_indices:
                self._rebuild_indices()

            # Force garbage collection
            import gc
            gc.collect()
            
        logger.info(f"Optimization completed in {time.time() - start_time:.2f}s")
        
        new_memory_mb = self._get_memory_usage_mb()
        logger.info(f"Memory usage after optimization: {new_memory_mb:.1f} MB")
    
        
    def _get_memory_usage_mb(self) -> float:
        """Get current memory usage in MB if psutil is available."""
        if MEMORY_PROFILING_AVAILABLE:
            try:
                import psutil
                process = psutil.Process()
                return process.memory_info().rss / (1024 * 1024)
            except Exception:
                pass
        return 0.0
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics for the graph store."""
        stats = {
            "node_count": self.node_count(),
            "edge_count": self.edge_count(),
            "memory_usage_mb": self._get_memory_usage_mb(),
            "indices_enabled": self.enable_indices,
            "caching_enabled": self.enable_caching,
        }
        
        # Add query time statistics
        for query_type, times in self.query_times.items():
            if times:
                stats[f"{query_type}_avg_time"] = sum(times) / len(times)
                stats[f"{query_type}_min_time"] = min(times)
                stats[f"{query_type}_max_time"] = max(times)
                stats[f"{query_type}_count"] = len(times)
                
        # Add cache statistics if enabled
        if self.enable_caching:
            stats["cache_size"] = self._cache_size
            stats["cache_hits"] = self._cache_hits
            stats["cache_misses"] = self._cache_misses
            if self._cache_hits + self._cache_misses > 0:
                stats["cache_hit_ratio"] = self._cache_hits / (self._cache_hits + self._cache_misses)
            else:
                stats["cache_hit_ratio"] = 0.0
                
        return stats
    
    def iterate_edges_by_type(self, edge_type: str) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
        """
        Efficiently iterate through all edges of a specific type.
        
        Args:
            edge_type: Type of edges to iterate over
            
        Returns:
            Iterator of (source, target, edge_data) tuples
        """
        if self.enable_indices and edge_type in self._edge_type_index:
            for source, target in self._edge_type_index[edge_type]:
                # Ensure the specific keyed edge still exists
                if self.graph.has_edge(source, target, key=edge_type):
                    yield source, target, self.graph[source][target][edge_type].copy()
        else: # Fallback or if index not populated for this type
            for source, target, key, data in self.graph.edges(keys=True, data=True):
                if key == edge_type:
                    yield source, target, data.copy()
    
    
    def find_all_paths(self, source: str, target: str, max_depth: int = 10, edge_type_filter: Optional[Union[str, List[str], Callable]] = None) -> List[List[str]]:
        """
        Find all simple paths between source and target nodes.
        Delegates to find_paths.
        
        Args:
            source: Source node ID
            target: Target node ID  
            max_depth: Maximum path length
            edge_type_filter: Edge type filter (string, list, or callable)
            
        Returns:
            List of paths (each path is a list of node IDs)
        """
        # Use the main find_paths implementation
        return self.find_paths(source, target, max_length=max_depth, edge_type_filter=edge_type_filter)

    
    def find_paths(self, start: str, end: str,
                max_length: int = 5, 
                edge_type_filter: Optional[Union[str, List[str], Callable]] = None,
                path_filter: Optional[Callable[[List[str]], bool]] = None) -> List[List[str]]:
        """
        Find paths between start and end nodes with filtering.
        
        Args:
            start: Starting node ID
            end: Ending node ID
            max_length: Maximum path length
            edge_type_filter: Edge type(s) (str or List[str]) to include in the path, 
                            OR a Callable (u, v, data) -> bool to filter edges.
                            If Callable, it's used by the BFS/DFS directly.
            path_filter: Optional function to filter paths
            
        Returns:
            List of node ID paths from start to end
        """
        
        if start not in self.graph or end not in self.graph:
            return []
        
        actual_edge_types_set = None
        edge_filter_func = None

        if callable(edge_type_filter):
            edge_filter_func = edge_type_filter
        elif isinstance(edge_type_filter, str):
            actual_edge_types_set = {edge_type_filter}
        elif isinstance(edge_type_filter, list):
            actual_edge_types_set = set(edge_type_filter)
        # If edge_type_filter is None, actual_edge_types_set remains None, all edge types considered.
        
        # Cache key construction
        cache_key = f"find_paths:{start}:{end}:{max_length}:{hash(frozenset(actual_edge_types_set)) if actual_edge_types_set else None}"
        if self.enable_caching and not edge_filter_func and not path_filter: # Only cache if not using complex callables
            cached = self._get_from_cache(cache_key)
            if cached is not None:
                return cached
        
        paths_q = deque([(start, [start])]) # Store (current_node, current_path_list)
        valid_paths = []
        
        while paths_q:
            curr_node, path = paths_q.popleft()

            if len(path) > max_length:
                continue

            # Handle different edge_type_filter formats
            potential_next_steps = []
            if actual_edge_types_set:
                for etype in actual_edge_types_set:
                    edges = self.get_edges(source=curr_node, edge_type=etype)
                    potential_next_steps.extend(edges)
            else: # Get all outgoing edges if no string/list filter, or if it's a callable filter
                potential_next_steps = self.get_edges(source=curr_node)

            for s, neighbor, edge_data in potential_next_steps:
                if edge_filter_func and not edge_filter_func(s, neighbor, edge_data):
                    continue # Skip if callable filter returns False

                if neighbor == end:
                    new_path = path + [neighbor]
                    if path_filter is None or path_filter(new_path):
                        valid_paths.append(new_path)
                elif neighbor not in path: # Avoid simple cycles in the current path
                    if len(path) < max_length: # Check before appending to queue
                        paths_q.append((neighbor, path + [neighbor]))
        
        # Cache the result for future queries
        if self.enable_caching and not edge_filter_func and not path_filter:
            self._add_to_cache(cache_key, valid_paths)
            
        return valid_paths
    
    
    def _get_filtered_successors(self, node: str, edge_types: Optional[Set[str]] = None) -> List[Tuple[str, Dict]]:
        """Get successors filtered by edge types."""
        if edge_types is None:
            return [(target, self.graph[node][target]) for target in self.graph.successors(node)]
            
        if self.enable_indices:
            result = []
            for edge_type in edge_types:
                for target in self._source_edge_index.get(node, {}).get(edge_type, set()):
                    if self.graph.has_edge(node, target):
                        result.append((target, self.graph[node][target]))
            return result
        else:
            return [(target, edge_data) for target, edge_data in 
                   [(t, self.graph[node][t]) for t in self.graph.successors(node)]
                   if edge_data.get("edge_type") in edge_types]
    
    # Cache management methods
    
    def clear_cache(self) -> None:
        """Clears the internal query cache."""
        self._cache.clear()
        self._cache_hits = 0
        self._cache_misses = 0
        logger.info("GraphStore cache cleared.")
        
    def _get_from_cache(self, key: str) -> Optional[Any]:
        """Get a value from the cache, if it exists."""
        if key in self._cache:
            self._cache_hits += 1
            return self._cache.get(key)
        self._cache_misses += 1
        return None
        
    def _add_to_cache(self, key: str, value: Any) -> None:
        """Add a value to the cache, evicting if necessary."""
        # Don't cache extremely large results
        if isinstance(value, (list, tuple, set)) and len(value) > 10000:
            return
            
        # Check if cache is full
        if self._cache_size >= self._max_cache_size:
            # Simple LRU: remove a random item
            if self._cache:
                random_key = next(iter(self._cache))
                del self._cache[random_key]
                self._cache_size -= 1
                
        # Add the new item
        self._cache[key] = value
        self._cache_size += 1
        
    def _clear_cache(self) -> None:
        """Clear the entire cache."""
        self._cache.clear()
        self._cache_size = 0
        
    def _invalidate_node_caches(self, node_id: str) -> None:
        """
        Invalidate caches related to a specific node with more precision.
        """
        if not self.enable_caching or not self._cache:
            return
            
        keys_to_remove = []
        for key in self._cache:
            # Check for common cache key patterns with the node_id
            if (
                # Direct node queries (by ID)
                f":{node_id}:" in key or key.endswith(f":{node_id}") or 
                # Node queries where node_id is part of a path
                any(part == node_id for part in key.split(':'))
            ):
                keys_to_remove.append(key)
                
        for key in keys_to_remove:
            if key in self._cache:
                del self._cache[key]
                self._cache_size -= 1
                
        logger.debug(f"Invalidated {len(keys_to_remove)} cache entries for node {node_id}")

    
    def _invalidate_edge_caches(self, source: str, target: str, relationship_type: Optional[str] = None, actual_key: Optional[str] = None) -> None:
        """Invalidate cache entries relevant to a specific edge or set of edges."""
        if not self.enable_caching: return
        
        keys_to_delete = []
        # Example cache key pattern: f"get_edges_v3:{source}:{target}:{edge_type_for_cache_key}:{properties_hash}"
        # Example for successors: f"get_successors:{node_id}:{relationship_type}"
        
        for cache_key_str in list(self._cache.keys()): # Iterate over a copy of keys
            # Precise invalidation for get_edges
            if cache_key_str.startswith("get_edges_v3:"):
                parts = cache_key_str.split(':')
                # Expected format: "get_edges_v3", src, tgt, type_tuple_str, props_hash_str
                if len(parts) >= 4: 
                    cached_src = parts[1]
                    cached_tgt = parts[2]
                    cached_types_str = parts[3] # This was tuple(sorted(list(set(types)))) as string

                    match_source = (source == cached_src or cached_src == 'None')
                    match_target = (target == cached_tgt or cached_tgt == 'None')
                    
                    type_match = False
                    if relationship_type:
                        # This is complex because cached_types_str is a string representation of a tuple
                        # A simpler (broader) invalidation might be needed if type matching is too tricky here.
                        # For now, if a specific edge (s,t,type,key) changes, invalidate caches that *could* include it.
                        if f"'{relationship_type}'" in cached_types_str or relationship_type == cached_types_str: # Simplified check
                            type_match = True
                    elif not relationship_type: # If no specific type given for invalidation, consider it a broader match factor
                        type_match = True


                    if match_source and match_target and type_match:
                        keys_to_delete.append(cache_key_str)

            # Precise invalidation for get_successors/predecessors
            elif cache_key_str.startswith(f"get_successors:{source}"):
                if relationship_type and cache_key_str == f"get_successors:{source}:{relationship_type}":
                    keys_to_delete.append(cache_key_str)
                elif not relationship_type: # Invalidate all successor caches for this source
                    keys_to_delete.append(cache_key_str)
            elif cache_key_str.startswith(f"get_predecessors:{target}"):
                if relationship_type and cache_key_str == f"get_predecessors:{target}:{relationship_type}":
                    keys_to_delete.append(cache_key_str)
                elif not relationship_type: # Invalidate all predecessor caches for this target
                    keys_to_delete.append(cache_key_str)
            
            # General relationship counts
            elif relationship_type and cache_key_str == f"relationship_count:{relationship_type}":
                keys_to_delete.append(cache_key_str)

        # Always invalidate "all" type queries if any edge changes
        if "relationship_count:all" in self._cache:
            keys_to_delete.append("relationship_count:all")
        if "get_edges_v3:None:None:None" in self._cache.get("get_edges_v3:None:None:None:None", ''): # Check specific for None properties
            keys_to_delete.append("get_edges_v3:None:None:None:None")


        deleted_count = 0
        for k_del in set(keys_to_delete):
            if k_del in self._cache:
                del self._cache[k_del]
                self._cache_size -=1
                deleted_count +=1
        
        if deleted_count > 0:
            logger.debug(f"[GS_CACHE_INV] Invalidated {deleted_count} cache entries due to edge change involving ({source}, {target}, type={relationship_type}, key={actual_key}).")
    
    
    def update_node_attributes(self, node_id: str, attributes_to_update: Dict[str, Any]) -> bool:
        """
        Updates existing attributes or adds new ones to a node.
        Does not remove attributes not specified in attributes_to_update.
        Returns True if the node exists and attributes were updated, False otherwise.
        """
        if not self.graph.has_node(node_id):
            logger.warning(f"[GS_UPDATE_NODE] Node '{node_id}' not found. Cannot update attributes.")
            return False
        
        try:
            # Calling add_node again on an existing node updates its attributes.
            # Attributes in attributes_to_update will be added or will overwrite existing ones.
            # Attributes not in attributes_to_update that already exist on the node will be preserved.
            self.graph.add_node(node_id, **attributes_to_update)
            logger.debug(f"[GS_UPDATE_NODE] Attributes for node '{node_id}' updated with: {attributes_to_update}")

            if self.enable_indices:
                # _update_node_indices expects all current attributes for proper re-indexing.
                self._update_node_indices(node_id, self.graph.nodes[node_id]) 
            if self.enable_caching:
                self._invalidate_node_caches(node_id) 
            self._graph_changed = True
            return True
        except Exception as e:
            logger.error(f"Error updating node attributes for '{node_id}' using add_node: {e}", exc_info=True)
            return False