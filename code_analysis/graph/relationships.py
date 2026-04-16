"""
Module for tracking general code relationships.

This module provides the RelationshipTracker class that handles
tracking relationships between code elements such as dependencies,
references, usages, and other custom relationships.
"""

import logging
import copy
import json
from typing import Any, Dict, List, Optional, Set, Tuple, Union, Callable
from collections import defaultdict

from .store import GraphStore
from .traversal import GraphTraversal
from code_analysis.relationship_types import (
    RELATIONSHIP_PAIRS,
    REL_TYPE_CALLS,
    REL_TYPE_CALLED_BY,
    REL_TYPE_INHERITS,
    REL_TYPE_REFERENCES,
    REL_TYPE_CONTAINS,
    REL_TYPE_DEPENDS_ON,
    REL_TYPE_IMPLEMENTS,
    REL_TYPE_USES,
    REL_TYPE_OVERRIDES,
    REL_TYPE_IMPORTS,
    NODE_TYPE_UNKNOWN,
    NODE_COLORS,
    RELATIONSHIP_STYLES
)


logger = logging.getLogger(__name__)


class RelationshipTracker:
    """
    Base class for tracking and managing relationships between code elements.
    Provides an interface to the underlying graph store.
    """

    def __init__(self, store: Optional['GraphStore'] = None, traversal: Optional['GraphTraversal'] = None):
        """
        Initialize the RelationshipTracker.
        
        Args:
            store: The graph store instance to use. If None, a new one is created.
            traversal: The graph traversal instance. If None, a new one is created based on the store.
        """

        logger.info(f"[{self.__class__.__name__}.__init__] ENTERING for instance ID: {id(self)}. Received store type: {type(store)}, store ID: {id(store) if store else 'N/A'}")
        self._store = store or GraphStore()
        logger.info(f"[{self.__class__.__name__}.__init__] self._store is now type: {type(self._store)}, self._store ID: {id(self._store)}. self._store.graph type: {type(self._store.graph) if self._store else 'N/A'}")
        self._traversal = traversal or GraphTraversal(self._store)
        # Caches for optimization
        self._relationship_cache: Dict[Tuple, List[Dict[str, Any]]] = {}
        self._node_cache: Dict[str, Dict[str, Any]] = {} # Cache node properties if needed
        logger.info(f"{self.__class__.__name__} initialized.")

    @property
    def store(self):
        """Provides access to the underlying graph store."""
        return self._store

    @property
    def traversal(self):
        """Provides access to the graph traversal utility."""
        return self._traversal

    # def _clear_caches(self, source: Optional[str] = None, target: Optional[str] = None) -> None:
    #     """
    #     Clear internal caches of RelationshipTracker more selectively.
    #     This avoids wiping the full GraphStore cache.
    #     """
    #     # Clear only relevant RelationshipTracker caches 
    #     # instead of self._relationship_cache.clear()
    #     keys_to_remove = []
        
    #     # If source or target is provided, selectively clear cache entries
    #     if source or target:
    #         for key in list(self._relationship_cache.keys()):
    #             key_source = key[1] if len(key) > 1 else None
    #             key_target = key[2] if len(key) > 2 else None
                
    #             if (source and key_source == source) or (target and key_target == target):
    #                 keys_to_remove.append(key)
                    
    #         for key in keys_to_remove:
    #             if key in self._relationship_cache:
    #                 del self._relationship_cache[key]
                    
    #         # Also selectively clear node cache if needed
    #         if source and source in self._node_cache:
    #             del self._node_cache[source]
    #         if target and target in self._node_cache:
    #             del self._node_cache[target]
                
    #         logger.debug(f"Cleared {len(keys_to_remove)} RelationshipTracker cache entries")
    #     else:
    #         # If no specific nodes, clear all caches
    #         self._relationship_cache.clear()
    #         self._node_cache.clear()
    #         logger.debug("Cleared all RelationshipTracker caches")
        
    #     # DO NOT call GraphStore's cache clearing method directly
    #     # Let GraphStore handle its own cache invalidation when it executes operations
    #     # This avoids the inefficient self._store.clear_cache() that wipes all caches
    
    
    def _clear_caches(self,
                      source: Optional[str] = None,
                      target: Optional[str] = None,
                      relationship_type_affected: Optional[str] = None) -> None:
        """
        Clear internal caches of RelationshipTracker.
        If source and target are given, clears specific entries.
        If relationship_type_affected is given, clears general "find all" queries for that type.
        """
        keys_to_remove = []
        
        if source and target and relationship_type_affected:
            # Specific edge affected, clear its direct and inverse cache entries
            # and also the general "find all" for this type and its inverse.
            
            # 1. Specific entry for (type, source, target, properties_variant)
            for key_tuple, _value in list(self._relationship_cache.items()): # Changed _ to _value for clarity
                # ---- START DIAGNOSTIC LOGGING ----
                if not isinstance(key_tuple, tuple) or len(key_tuple) != 4:
                    logger.error(
                        f"[{self.__class__.__name__}._clear_caches] CRITICAL: Encountered malformed key_tuple in cache (Stage 1)! "
                        f"Key: {key_tuple}, Type: {type(key_tuple)}, Len: {len(key_tuple) if isinstance(key_tuple, (tuple, list, str)) else 'N/A'}"
                    )
                    # Potentially skip this key or handle error to prevent crash
                    continue 
                # ---- END DIAGNOSTIC LOGGING ----
                cache_rel_type, cache_source, cache_target, _props_variant = key_tuple # Changed _ to _props_variant
                if cache_rel_type == relationship_type_affected and \
                   cache_source == source and \
                   cache_target == target:
                    keys_to_remove.append(key_tuple)
            
            # 2. Inverse specific entry
            inverse_type = RELATIONSHIP_PAIRS.get(relationship_type_affected)
            if inverse_type:
                for key_tuple, _value in list(self._relationship_cache.items()): # Changed _ to _value
                    # ---- START DIAGNOSTIC LOGGING ----
                    if not isinstance(key_tuple, tuple) or len(key_tuple) != 4:
                        logger.error(
                            f"[{self.__class__.__name__}._clear_caches] CRITICAL: Encountered malformed key_tuple in cache (Stage 2 - Inverse)! "
                            f"Key: {key_tuple}, Type: {type(key_tuple)}, Len: {len(key_tuple) if isinstance(key_tuple, (tuple, list, str)) else 'N/A'}"
                        )
                        continue
                    # ---- END DIAGNOSTIC LOGGING ----
                    cache_rel_type, cache_source, cache_target, _props_variant = key_tuple # Changed _ to _props_variant
                    if cache_rel_type == inverse_type and \
                       cache_source == target and \
                       cache_target == source: # Note: source/target swapped for inverse
                        keys_to_remove.append(key_tuple)

            # 3. General "find all" for this relationship_type_affected
            for key_tuple, _value in list(self._relationship_cache.items()): # Changed _ to _value
                # ---- START DIAGNOSTIC LOGGING ----
                if not isinstance(key_tuple, tuple) or len(key_tuple) != 4:
                    logger.error(
                        f"[{self.__class__.__name__}._clear_caches] CRITICAL: Encountered malformed key_tuple in cache (Stage 3 - General)! "
                        f"Key: {key_tuple}, Type: {type(key_tuple)}, Len: {len(key_tuple) if isinstance(key_tuple, (tuple, list, str)) else 'N/A'}"
                    )
                    continue
                # ---- END DIAGNOSTIC LOGGING ----
                cache_rel_type, cache_source, cache_target, _props_variant = key_tuple # Changed _ to _props_variant
                if cache_rel_type == relationship_type_affected and \
                   cache_source is None and cache_target is None:
                    keys_to_remove.append(key_tuple)

            # 4. General "find all" for the inverse type
            if inverse_type:
                for key_tuple, _value in list(self._relationship_cache.items()): # Changed _ to _value
                    # ---- START DIAGNOSTIC LOGGING ----
                    if not isinstance(key_tuple, tuple) or len(key_tuple) != 4:
                        logger.error(
                            f"[{self.__class__.__name__}._clear_caches] CRITICAL: Encountered malformed key_tuple in cache (Stage 4 - General Inverse)! "
                            f"Key: {key_tuple}, Type: {type(key_tuple)}, Len: {len(key_tuple) if isinstance(key_tuple, (tuple, list, str)) else 'N/A'}"
                        )
                        continue
                    # ---- END DIAGNOSTIC LOGGING ----
                    cache_rel_type, cache_source, cache_target, _props_variant = key_tuple # Changed _ to _props_variant
                    if cache_rel_type == inverse_type and \
                       cache_source is None and cache_target is None:
                        keys_to_remove.append(key_tuple)
            
            if source in self._node_cache:
                del self._node_cache[source]
            if target in self._node_cache:
                del self._node_cache[target]

        elif relationship_type_affected: # Only relationship type given
            # Clear all general queries for this type and its inverse
            for key_tuple, _value in list(self._relationship_cache.items()): # Changed _ to _value
                # ---- START DIAGNOSTIC LOGGING ----
                if not isinstance(key_tuple, tuple) or len(key_tuple) != 4:
                    logger.error(
                        f"[{self.__class__.__name__}._clear_caches] CRITICAL: Encountered malformed key_tuple in cache (Stage 5 - Type Specific General)! "
                        f"Key: {key_tuple}, Type: {type(key_tuple)}, Len: {len(key_tuple) if isinstance(key_tuple, (tuple, list, str)) else 'N/A'}"
                    )
                    continue
                # ---- END DIAGNOSTIC LOGGING ----
                cache_rel_type, cache_source, cache_target, _props_variant = key_tuple # Changed _ to _props_variant
                if cache_rel_type == relationship_type_affected and \
                   cache_source is None and cache_target is None:
                    keys_to_remove.append(key_tuple)
            
            inverse_type = RELATIONSHIP_PAIRS.get(relationship_type_affected)
            if inverse_type:
                for key_tuple, _value in list(self._relationship_cache.items()): # Changed _ to _value
                    # ---- START DIAGNOSTIC LOGGING ----
                    if not isinstance(key_tuple, tuple) or len(key_tuple) != 4:
                        logger.error(
                            f"[{self.__class__.__name__}._clear_caches] CRITICAL: Encountered malformed key_tuple in cache (Stage 6 - Type Specific General Inverse)! "
                            f"Key: {key_tuple}, Type: {type(key_tuple)}, Len: {len(key_tuple) if isinstance(key_tuple, (tuple, list, str)) else 'N/A'}"
                        )
                        continue
                    # ---- END DIAGNOSTIC LOGGING ----
                    cache_rel_type, cache_source, cache_target, _props_variant = key_tuple # Changed _ to _props_variant
                    if cache_rel_type == inverse_type and \
                       cache_source is None and cache_target is None:
                        keys_to_remove.append(key_tuple)
        else: # Fallback: no specifics given
            self._relationship_cache.clear()
            self._node_cache.clear()
            logger.debug(f"[{self.__class__.__name__}._clear_caches] Cleared all RelationshipTracker caches (fallback).")
            return

        removed_count = 0
        for key_to_remove in keys_to_remove:
            if key_to_remove in self._relationship_cache:
                del self._relationship_cache[key_to_remove]
                removed_count += 1
        
        if removed_count > 0:
            logger.debug(f"[{self.__class__.__name__}._clear_caches] Cleared {removed_count} RelationshipTracker cache entries for source='{source}', target='{target}', type='{relationship_type_affected}'.")
        else:
            logger.debug(f"[{self.__class__.__name__}._clear_caches] No specific RelationshipTracker cache entries found to clear for source='{source}', target='{target}', type='{relationship_type_affected}'.")


    # def add_relationship(self, source: str, target: str, relationship_type: str, **attributes: Any) -> bool:
    #     """
    #     Adds a relationship (edge) to the graph store, managing bidirectional relationships if specified.
    #     Updates existing edges if they already exist with the same key, merging attributes.

    #     Args:
    #         source: The source node FQN.
    #         target: The target node FQN.
    #         relationship_type: The type of the relationship (e.g., REL_TYPE_CALLS).
    #         **attributes: Additional properties for the relationship.
    #                       If 'key' is provided in attributes, it's used as the primary edge key;
    #                       otherwise, relationship_type is used.
    #                       If 'inverse_key' is provided, it's used for the inverse edge key;
    #                       otherwise, the inverse_type is used.
    #                       Other attributes are deepcopied and stored (often within a 'metadata' dict).

    #     Returns:
    #         True if the primary relationship was successfully added/updated, False otherwise.
    #     """
        
    #     if not source or not target or not relationship_type:
    #         logger.warning(f"Attempted to add relationship with missing source, target, or type: ({source}, {target}, {relationship_type})")
    #         return False

    #     self._store.add_node(source)
    #     self._store.add_node(target)
        
    #     # Make a mutable copy of the provided attributes to safely extract control keys and to form the basis of what will be stored.
    #     processed_attributes = attributes.copy()

    #     # Determine the primary edge key. Pop 'key' from processed_attributes.
    #     # This ensures 'key' is used for keying but not stored as a data attribute itself via spreading.
    #     edge_key_primary = processed_attributes.pop("key", relationship_type)
        
    #     # Determine the inverse edge key (if provided). Pop 'inverse_key'.
    #     # This is only used for keying the inverse; not stored as data.
    #     inverse_key_for_inverse_edge_logic = processed_attributes.pop("inverse_key", None)
        
    #     # Now, processed_attributes contains only the actual data attributes intended for storage on the edge (it's clean of 'key' and 'inverse_key').
    #     # Deepcopy these to ensure the graph stores independent copies.
    #     final_data_attributes_to_store = copy.deepcopy(processed_attributes)
        
    #     logger.debug(
    #         f"Adding/Updating primary relationship: {source} --[{relationship_type}]--> {target} (key='{edge_key_primary}') "
    #         f"with FINAL STORED DATA attributes: {final_data_attributes_to_store}"
    #     )
        
    #     add_success = self._store.add_edge(
    #         source,
    #         target,
    #         edge_type=relationship_type, # Pass relationship_type as data
    #         key=edge_key_primary,        # Pass the determined key for NetworkX
    #         **final_data_attributes_to_store # Pass the cleaned, deepcopied data attributes
    #     )
    #     if not add_success:
    #         logger.error(f"Failed to add/update primary edge: {source} -[{relationship_type} / key={edge_key_primary}]-> {target}")
    #         return False
        
        
    #     # Handle bidirectional relationships
    #     inverse_type = RELATIONSHIP_PAIRS.get(relationship_type)
    #     if inverse_type:
    #         actual_inverse_key = inverse_key_for_inverse_edge_logic if inverse_key_for_inverse_edge_logic is not None else inverse_type
            
    #         # Attributes for inverse are typically the same deepcopied ones, unless specific logic dictates otherwise.
    #         # final_data_attributes_to_store is already prepared.

    #         if source == target: # Self-loop
    #             if inverse_type != relationship_type or actual_inverse_key != edge_key_primary:
    #                 logger.debug(
    #                     f"Self-loop: Adding distinct inverse keyed edge {target} --[{inverse_type}]--> {source} (key='{actual_inverse_key}')"
    #                 )
    #                 self._store.add_edge(target, source, edge_type=inverse_type, key=actual_inverse_key, **final_data_attributes_to_store)
    #             else:
    #                 logger.debug(f"Self-loop: Inverse {target} --[{inverse_type}]--> {source} (key='{actual_inverse_key}') is same as primary. Attributes updated on primary.")
    #         else: # Not a self-loop
    #             # Check if the specific inverse we intend to add/update already exists to avoid redundant logging or complex checks here.
    #             # GraphStore.add_edge will handle the update if (target, source, actual_inverse_key) exists.
    #             logger.debug(
    #                 f"Adding/Updating inverse relationship: {target} --[{inverse_type}]--> {source} (key='{actual_inverse_key}') "
    #                 f"with attributes: {final_data_attributes_to_store}"
    #             )
    #             self._store.add_edge(target, source, edge_type=inverse_type, key=actual_inverse_key, **final_data_attributes_to_store)
        
    #     self._clear_caches(source, target) # Might need refinement for MultiDiGraph context
    #     return add_success
    
    
    def add_relationship(self, source: str, target: str, relationship_type: str, **attributes: Any) -> bool:
        """
        Adds a relationship (edge) to the graph store, managing bidirectional relationships if specified.
        Updates existing edges if they already exist with the same key, merging attributes.

        Args:
            source: The source node FQN.
            target: The target node FQN.
            relationship_type: The semantic type of the relationship (e.g., REL_TYPE_CALLS).
                               This will be stored as data['type'] on the edge.
            **attributes: Additional properties for the relationship.
                          If 'key' is provided in attributes, it's used as the MultiDiGraph edge key;
                          otherwise, `relationship_type` string itself is used as the key for the primary edge.
                          If 'inverse_key' is provided, it's used for the inverse edge key;
                          otherwise, the inverse_type string is used as its key.
                          Other attributes are deepcopied and stored (often within a 'metadata' dict).

        Returns:
            True if the primary relationship was successfully added/updated, False otherwise.
        """
        
        if not source or not target or not relationship_type:
            logger.warning(f"Attempted to add relationship with missing source, target, or type: ({source}, {target}, {relationship_type})")
            return False

        # self._store.add_node(source) # GraphStore.add_edge will handle node creation
        # self._store.add_node(target)
        
        # Make a mutable copy of the provided attributes to safely extract control keys.
        processed_attributes = attributes.copy()

        # Determine the primary edge key. Pop 'key' from processed_attributes.
        # This ensures 'key' is used for keying by GraphStore but not also stored as a data attribute by it 
        # if it came from **attributes here.
        key_for_primary_edge = processed_attributes.pop("key", relationship_type) 
        
        # Determine the inverse edge key (if provided). Pop 'inverse_key'.
        inverse_type = RELATIONSHIP_PAIRS.get(relationship_type)
        key_for_inverse_edge = None
        if inverse_type:
            key_for_inverse_edge = processed_attributes.pop("inverse_key", inverse_type)
        
        # Now, processed_attributes contains only the actual data attributes intended for storage on the edge.
        # Deepcopy these to ensure the graph stores independent copies.
        final_data_attributes_to_store = copy.deepcopy(processed_attributes)
        
        logger.debug(
            f"[RelationshipTracker.add_relationship] Adding/Updating primary: {source} --[{relationship_type}]--> {target} "
            f"(Key to be used by GraphStore: '{key_for_primary_edge}') "
            f"with data attributes: {final_data_attributes_to_store}"
        )
        
        # Call GraphStore.add_edge, passing the determined key_for_primary_edge EXPLICITLY as the 'key' parameter.
        # relationship_type is passed as edge_type, which GraphStore will store as data['type'].
        # final_data_attributes_to_store are the other data properties.
        primary_add_actual_key = self._store.add_edge(
            source,
            target,
            edge_type=relationship_type,    # For data['type']
            key=key_for_primary_edge,       # Explicit MultiDiGraph key
            **final_data_attributes_to_store 
        )
        if not primary_add_actual_key: # GraphStore.add_edge now returns the key or None
            logger.error(f"[RelationshipTracker.add_relationship] Failed to add/update primary edge: {source} -[{relationship_type} / key={key_for_primary_edge}]-> {target}")
            return False
        
        # Handle bidirectional relationships
        if inverse_type and key_for_inverse_edge: # Ensure inverse_type and its key are determined
            logger.debug(
                f"[RelationshipTracker.add_relationship] Adding/Updating inverse: {target} --[{inverse_type}]--> {source} "
                f"(Key to be used by GraphStore: '{key_for_inverse_edge}') "
                f"with data attributes: {final_data_attributes_to_store}"
            )
            self._store.add_edge(
                target, 
                source, 
                edge_type=inverse_type,         # For data['type'] on inverse edge
                key=key_for_inverse_edge,       # Explicit MultiDiGraph key for inverse
                **final_data_attributes_to_store # Same data attributes usually
            )
        
        self._clear_caches(source, target)
        return True

    
    def find_relationships(self,
                           relationship_type: Optional[str] = None,
                           source: Optional[str] = None,
                           target: Optional[str] = None,
                           properties: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Find relationships matching the given criteria.

        Args:
            relationship_type: The type of relationship to find (e.g., "IMPORTS").
                               This should match the 'type' attribute stored on edges.
            source: Optional source node identifier.
            target: Optional target node identifier.
            properties: Optional dictionary of properties to match against the edge's data attributes
                        (excluding the 'type' attribute itself, which is handled by relationship_type).

        Returns:
            A list of dictionaries, each representing a matching relationship.
            Each dictionary contains: 'source', 'target', 'relationship_type', 'key', and 'properties'
            where 'properties' is a dict of all attributes stored on the graph edge *except* for 'type'.
        """
        # Ensure properties is hashable for cache key
        frozen_props = frozenset(properties.items()) if properties else None
        cache_key = (relationship_type, source, target, frozen_props)

        # ---- START DIAGNOSTIC LOGGING FOR KEY CONSTRUCTION ----
        if not isinstance(cache_key, tuple) or len(cache_key) != 4:
            logger.error(
                f"[{self.__class__.__name__}.find_relationships] CRITICAL: Malformed cache_key constructed! "
                f"Key: {cache_key}, Type: {type(cache_key)}, Len: {len(cache_key) if isinstance(cache_key, (tuple,list,str)) else 'N/A'}"
            )
            logger.error(
                f"Parameters were: type='{relationship_type}', src='{source}', tgt='{target}', props='{properties}'"
            )
            # Consider raising an error during debugging if this state is unexpected.
            # raise ValueError(f"Malformed cache key created: {cache_key}")
        # ---- END DIAGNOSTIC LOGGING FOR KEY CONSTRUCTION ----

        if cache_key in self._relationship_cache:
            logger.debug(f"Cache hit for find_relationships: {cache_key}")
            return copy.deepcopy(self._relationship_cache[cache_key]) # Return deepcopy to prevent external modification

        logger.debug(f"Cache miss for find_relationships: {cache_key}. Querying store.")
        
        # Actual query to the store
        # The GraphStore.get_edges method yields (u, v, key, data)
        # We need to filter these based on relationship_type (semantic type in data['type']) and properties (match against data or data['metadata'])
        
        results = []
        # Assuming self._store.get_edges takes these specific parameters now and its 'edge_type' parameter filters on data['type'].
        # Its 'properties' parameter should match against the edge's data dictionary.
        
        # Construct the property filter for GraphStore, ensuring 'type' is included if relationship_type is given
        store_property_filter = {}
        if properties: # User-defined properties
            store_property_filter.update(properties)
        # `relationship_type` here is the semantic type, so it becomes a property filter for `data['type']`
        # GraphStore.get_edges() has an `edge_type` parameter which is for data['type']
        
        found_edges_generator = self._store.get_edges(
            source=source, 
            target=target, 
            edge_type=relationship_type, # This will filter by data['type'] == relationship_type
            properties=properties # This will filter by other properties in edge data
        )

        for s_node, t_node, edge_key_nx, edge_data in found_edges_generator:
            # Construct the full edge representation as expected by callers of find_relationships
            # This typically includes source, target, relationship_type, and all other attributes.
            # The edge_data from store already contains 'type' (semantic type) and 'metadata' etc.
            
            full_edge_info = {
                "source": s_node,
                "target": t_node,
                "relationship_type": edge_data.get("type", relationship_type), # Semantic type from data
                "key": edge_key_nx, # Actual NetworkX key
                **edge_data # Spread all other attributes from edge_data
            }
            results.append(full_edge_info)

        logger.debug(f"Found {len(results)} relationships for query: type={relationship_type}, src={source}, tgt={target}, props={properties}")
        
        self._relationship_cache[cache_key] = copy.deepcopy(results) # Store deepcopy
        return results
    
    
    def _manage_bidirectional_relationship(self, source: str, target: str, relationship_type: str,
                                          properties: Dict[str, Any]) -> None:
        """
        Create the inverse relationship if applicable.
        
        Args:
            source: Source element
            target: Target element
            relationship_type: Type of the relationship
            properties: Properties of the original relationship
        """
        inverse_rel_type = RELATIONSHIP_PAIRS.get(relationship_type)
        if not inverse_rel_type:
            return
            
        # Check if inverse relationship already exists
        if self.store.has_edge(target, source, edge_type=inverse_rel_type):
            logger.debug(f"Inverse relationship already exists: {target} -{inverse_rel_type}-> {source}")
            return
            
        # Clone properties for inverse relationship
        inverse_properties = properties.copy()
        
        # Add special flag to avoid infinite recursion
        inverse_properties["_bidirectional_managed"] = True
        
        # Check if current operation is part of bidirectional management
        is_managed_operation = properties.get("_bidirectional_managed", False)
        
        # Only add inverse if this isn't already a managed operation
        if not is_managed_operation:
            # Create the inverse relationship
            logger.debug(f"Creating inverse relationship: {target} -{inverse_rel_type}-> {source}")
            
            # Add to graph directly to bypass add_relationship recursion
            self.store.add_edge(target, source, edge_type=inverse_rel_type, **inverse_properties)
            
            # Update relationship counts
            if inverse_rel_type not in self._relationship_counts:
                self._relationship_counts[inverse_rel_type] = 0
            self._relationship_counts[inverse_rel_type] += 1
    
    
    def get_outgoing_relationships(self, source_node: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get all outgoing relationships from a source element.
        
        Args:
            source_node: Source element to get relationships for
            
        Returns:
            Dictionary mapping relationship types to lists of relationship information.
            Each item in the list contains 'target', 'key', and 'properties' (the full edge data).
        """
        results = {}
        
        # GraphStore.get_edges yields (u, v, k, data)
        edges_generator = self.store.get_edges(source=source_node) 
        
        for _, target_node, key, edge_data in edges_generator: # Unpack 4 items
            # The semantic relationship type is stored under the 'type' key in edge_data
            relationship_type = edge_data.get("type", "unknown") 
            
            if relationship_type not in results:
                results[relationship_type] = []
            
            results[relationship_type].append({
                "target": target_node,
                "key": key, # The actual NetworkX key of the edge
                "properties": edge_data # The full attribute dictionary of the edge
            })
            
        return results
    
    
    def get_incoming_relationships(self, target_node: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get all incoming relationships to a target element.
        
        Args:
            target_node: Target element to get relationships for
            
        Returns:
            Dictionary mapping relationship types to lists of relationship information.
            Each item in the list contains 'source', 'key', and 'properties' (the full edge data).
        """
        results = {}
        
        # GraphStore.get_edges yields (u, v, k, data)
        edges_generator = self.store.get_edges(target=target_node)
        
        for source_node, _, key, edge_data in edges_generator: # Unpack 4 items
            # The semantic relationship type is stored under the 'type' key in edge_data
            relationship_type = edge_data.get("type", "unknown")
            
            if relationship_type not in results:
                results[relationship_type] = []
            
            results[relationship_type].append({
                "source": source_node,
                "key": key, # The actual NetworkX key of the edge
                "properties": edge_data # The full attribute dictionary of the edge
            })
            
        return results
    
    
    def has_relationship(self, source: str, target: str, relationship_type: str) -> bool: 
        """
        Check if a specific relationship exists between two nodes.
        
        Args:
            source: The source node identifier.
            target: The target node identifier.
            relationship_type: The type of the relationship (it's the key).
            
        Returns:
            True if the relationship exists, False otherwise.
        """
        return self._store.has_edge(source, target, edge_type=relationship_type)
    
    
    def get_relationship_count(self, relationship_type: Optional[str] = None) -> int:
        """
        Get the number of relationships of a specific type, or total relationships.

        Args:
            relationship_type: Optional type of relationship to count.

        Returns:
            The count of matching relationships.
        """
        if relationship_type:
            # Directly use the store's edge_count if it supports filtering by type
            if hasattr(self.store, 'edge_count') and callable(getattr(self.store, 'edge_count')):
                try: # Attempt to pass edge_type; if GraphStore.edge_count was updated
                    return self.store.edge_count(edge_type=relationship_type)
                except TypeError: # If GraphStore.edge_count doesn't take edge_type
                    logger.warning(f"GraphStore.edge_count does not support direct filtering by type. Falling back to slower counting for relationship_type '{relationship_type}'.")
                    # Fallback to getting all edges of that type and counting them
                    edges_of_type = self.store.get_edges(edge_type=relationship_type)
                    return len(list(edges_of_type))
            else: # Should not happen if store is GraphStore
                edges_of_type = self.store.get_edges(edge_type=relationship_type)
                return len(list(edges_of_type))

        else:
            # Total relationships
            return self.store.edge_count() # Call without edge_type for total
    
    
    def remove_relationship(self, source: str, target: str, 
                            relationship_type: str, 
                            **attributes: Any) -> bool:
        """
        Removes a relationship and its inverse if applicable.
        
        Args:
            source: Source node FQN.
            target: Target node FQN.
            relationship_type: Type of relationship.
            **attributes: Used to determine the 'key' for removal.
                          If 'key' is in attributes, it's used. Else, relationship_type is used.
                          If 'inverse_key' is in attributes, it's used for the inverse. Else, inverse_type.
        
        Returns:
            True if the primary relationship was found and removed, False otherwise.
        """
        if not source or not target or not relationship_type:
            logger.warning(f"Attempted to remove relationship with missing source, target, or type: ({source}, {target}, {relationship_type})")
            return False

        # Determine the key for the primary edge removal
        key_to_remove = attributes.get("key", relationship_type)
        
        logger.debug(f"[RT.remove_relationship] Attempting to remove primary: {source} -[{relationship_type} key={key_to_remove}]-> {target}")
        removed_primary = self._store.remove_edge(source, target, key=key_to_remove, edge_type=relationship_type)

        if removed_primary:
            logger.info(f"[RT.remove_relationship] Successfully removed primary edge: {source} -[{relationship_type} key={key_to_remove}]-> {target}")
        else:
            logger.warning(f"[RT.remove_relationship] Primary edge not found or not removed: {source} -[{relationship_type} key={key_to_remove}]-> {target}")

        # Handle inverse relationship
        inverse_type = RELATIONSHIP_PAIRS.get(relationship_type)
        if inverse_type:
            inverse_key_to_remove = attributes.get("inverse_key", inverse_type)
            logger.debug(f"[RT.remove_relationship] Attempting to remove inverse: {target} -[{inverse_type} key={inverse_key_to_remove}]-> {source}")
            removed_inverse = self._store.remove_edge(target, source, key=inverse_key_to_remove, edge_type=inverse_type)
            if removed_inverse:
                logger.info(f"[RT.remove_relationship] Successfully removed inverse edge: {target} -[{inverse_type} key={inverse_key_to_remove}]-> {source}")
            else:
                logger.warning(f"[RT.remove_relationship] Inverse edge not found or not removed: {target} -[{inverse_type} key={inverse_key_to_remove}]-> {source}")

        if removed_primary: # Only clear cache if something was actually removed
            self._clear_caches(source, target, relationship_type_affected=relationship_type)
            
        return removed_primary
        
    
    def remove_relationships(self, 
                             source: Optional[str] = None, 
                             target: Optional[str] = None, 
                             relationship_type: Optional[str] = None, 
                             properties: Optional[Dict[str, Any]] = None) -> int:
        """
        Removes all relationships matching the given criteria.

        Args:
            source: Optional source node identifier.
            target: Optional target node identifier.
            relationship_type: Optional type of relationship to remove.
            properties: Optional dictionary of properties to match for removal.
                        If None, all relationships matching other criteria are removed.
                        If provided, only edges with exactly matching properties are removed.

        Returns:
            The number of relationships actually removed.
        """
        if not source and not target and not relationship_type and not properties:
            logger.warning("remove_relationships called without any criteria. This would clear all edges of all types if allowed. Aborting.")
            # Or, if you want to allow clearing all edges of a specific type when only relationship_type is given,
            # this initial check might be too strict. For now, being cautious.
            # If relationship_type is given, it should proceed. Let's refine this.
            if not relationship_type: # At least relationship_type or other specifiers should be present
                logger.warning("remove_relationships called without at least a relationship_type or other specific criteria. Aborting.")
                return 0


        logger.debug(
            f"Attempting to remove relationships: Source='{source}', Target='{target}', "
            f"Type='{relationship_type}', Properties='{properties}'"
        )
        
        # Find all relationships matching the criteria.
        # self.find_relationships should handle the property matching correctly.
        relationships_to_remove = self.find_relationships(
            relationship_type=relationship_type,
            source=source,
            target=target,
            properties=properties 
        )
        
        removed_count = 0
        if not relationships_to_remove:
            logger.debug("No relationships found matching the criteria for removal.")
            return 0
            
        logger.debug(f"Found {len(relationships_to_remove)} relationships to remove.")

        for rel_data in relationships_to_remove:
            s = rel_data.get("source")
            t = rel_data.get("target")
            # Use the specific relationship_type from the found edge, 
            # as find_relationships might return multiple types if relationship_type was None in the query.
            # However, our current call passes relationship_type, so it should match.
            actual_rel_type = rel_data.get("relationship_type") 

            if s and t and actual_rel_type:
                # Call the singular remove_relationship method which handles store interaction
                # and inverse relationship removal if configured (though for bulk, inverse is tricky).
                # For now, assume remove_relationship handles its own logic correctly for a single edge.
                if self.remove_relationship(s, t, actual_rel_type):
                    removed_count += 1
            else:
                logger.warning(f"Skipping removal of malformed relationship data: {rel_data}")
                
        if removed_count > 0:
            logger.info(f"Successfully removed {removed_count} relationships matching criteria.")
        else:
            logger.debug("No relationships were ultimately removed (either not found or removal failed).")
            
        return removed_count

        
        
    # ----------------------------------------------------
    # Specialized methods for common relationship types
    # ----------------------------------------------------
    
    def add_call_relationship(self, caller: str, callee: str, 
                            location: Optional[Dict[str, Any]] = None) -> None:
        """
        Add a function call relationship.
        
        Args:
            caller: The function/method that makes the call
            callee: The function/method being called
            location: Optional location information (file, line, etc.)
        """
        properties = {"rel_value": callee}
        if location:
            properties.update(location)
            
        self.add_relationship(caller, callee, REL_TYPE_CALLS, properties)
        logger.debug(f"Added call relationship: {caller} calls {callee}")
    
    def add_inheritance_relationship(self, child: str, parent: str, 
                                    metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add an inheritance relationship.
        
        Args:
            child: The child class
            parent: The parent class
            metadata: Optional metadata about the inheritance
        """
        properties = {"rel_value": parent}
        if metadata:
            properties.update(metadata)
            
        self.add_relationship(child, parent, REL_TYPE_INHERITS, properties)
        logger.debug(f"Added inheritance relationship: {child} inherits from {parent}")
    
    def add_reference_relationship(self, source: str, target: str, 
                                  reference_type: str = "variable",
                                  metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add a reference relationship (e.g., variable usage).
        
        Args:
            source: The component making the reference
            target: The component being referenced
            reference_type: Type of reference (variable, class, etc.)
            metadata: Optional metadata about the reference
        """
        properties = {
            "rel_value": target,
            "reference_type": reference_type
        }
        if metadata:
            properties.update(metadata)
            
        self.add_relationship(source, target, REL_TYPE_REFERENCES, properties)
        logger.debug(f"Added reference relationship: {source} references {target} as {reference_type}")
    
    def add_containment_relationship(self, container: str, contained: str,
                                    container_type: str = "module",
                                    metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add a containment relationship.
        
        Args:
            container: The container component (module, class, etc.)
            contained: The contained component
            container_type: Type of container
            metadata: Optional metadata
        """
        properties = {
            "rel_value": contained,
            "container_type": container_type
        }
        if metadata:
            properties.update(metadata)
            
        self.add_relationship(container, contained, REL_TYPE_CONTAINS, properties)
        logger.debug(f"Added containment relationship: {container} contains {contained}")
    
    def add_dependency_relationship(self, dependent: str, dependency: str,
                                    dependency_type: str = "runtime",
                                    metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add a dependency relationship.
        
        Args:
            dependent: The component that depends on another
            dependency: The component being depended on
            dependency_type: Type of dependency (runtime, compile-time, etc.)
            metadata: Optional metadata
        """
        properties = {
            "rel_value": dependency,
            "dependency_type": dependency_type
        }
        if metadata:
            properties.update(metadata)
            
        self.add_relationship(dependent, dependency, REL_TYPE_DEPENDS_ON, properties)
        logger.debug(f"Added dependency relationship: {dependent} depends on {dependency} ({dependency_type})")
    
    def find_calls(self, caller: Optional[str] = None, 
                  callee: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Find call relationships.
        
        Args:
            caller: Optional caller to filter by
            callee: Optional callee to filter by
            
        Returns:
            List of call relationships
        """
        return self.find_relationships(REL_TYPE_CALLS, source=caller, target=callee)
    
    def find_inheritances(self, child: Optional[str] = None,
                         parent: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Find inheritance relationships.
        
        Args:
            child: Optional child class to filter by
            parent: Optional parent class to filter by
            
        Returns:
            List of inheritance relationships
        """
        return self.find_relationships(REL_TYPE_INHERITS, source=child, target=parent)
    
    def find_references(self, source: Optional[str] = None,
                       target: Optional[str] = None,
                       reference_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Find reference relationships.
        
        Args:
            source: Optional source to filter by
            target: Optional target to filter by
            reference_type: Optional reference type to filter by
            
        Returns:
            List of reference relationships
        """
        properties = None
        if reference_type:
            properties = {"reference_type": reference_type}
            
        return self.find_relationships(REL_TYPE_REFERENCES, source=source, target=target, properties=properties)
    
    def add_implementation_relationship(self, implementer: str, interface: str, 
                                       metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add an implementation relationship (e.g., class implementing an interface).
        
        Args:
            implementer: The class implementing the interface
            interface: The interface being implemented
            metadata: Optional metadata about the implementation
        """
        properties = {"rel_value": interface}
        if metadata:
            properties.update(metadata)
            
        self.add_relationship(implementer, interface, REL_TYPE_IMPLEMENTS, properties)
        logger.debug(f"Added implementation relationship: {implementer} implements {interface}")
    
    def add_usage_relationship(self, user: str, used: str, 
                              usage_type: str = "module",
                              metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add a usage relationship.
        
        Args:
            user: The component using another
            used: The component being used
            usage_type: Type of usage
            metadata: Optional metadata
        """
        properties = {
            "rel_value": used,
            "usage_type": usage_type
        }
        if metadata:
            properties.update(metadata)
            
        self.add_relationship(user, used, REL_TYPE_USES, properties)
        logger.debug(f"Added usage relationship: {user} uses {used} ({usage_type})")
    
    def add_override_relationship(self, overrider: str, overridden: str, 
                                 metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add an override relationship (e.g., method overriding).
        
        Args:
            overrider: The method overriding another
            overridden: The method being overridden
            metadata: Optional metadata
        """
        properties = {"rel_value": overridden}
        if metadata:
            properties.update(metadata)
            
        self.add_relationship(overrider, overridden, REL_TYPE_OVERRIDES, properties)
        logger.debug(f"Added override relationship: {overrider} overrides {overridden}")
    
    def add_access_relationship(self, accessor: str, accessed: str, 
                               access_type: str = "read",
                               metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add an access relationship (e.g., reading a field).
        
        Args:
            accessor: The component accessing another
            accessed: The component being accessed
            access_type: Type of access (read/write)
            metadata: Optional metadata
        """
        relationship_type = REL_TYPE_CONTAINS if access_type.lower() == "write" else REL_TYPE_CONTAINS
            
        properties = {
            "rel_value": accessed,
            "access_type": access_type
        }
        if metadata:
            properties.update(metadata)
            
        self.add_relationship(accessor, accessed, relationship_type, properties)
        logger.debug(f"Added access relationship: {accessor} {access_type}s {accessed}")
        
    def get_bidirectional_relationships(self, element: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get bidirectional relationships for an element.
        
        This method returns relationships in both directions - where the element
        is both a source and a target.
        
        Args:
            element: The element to get relationships for
            
        Returns:
            Dictionary of relationship types to lists of relationship information
        """
        results = {}
        
        # Get outgoing relationships
        outgoing = self.get_outgoing_relationships(element)
        
        # Get incoming relationships
        incoming = self.get_incoming_relationships(element)
        
        # Combine results
        for rel_type, relationships in outgoing.items():
            if rel_type not in results:
                results[rel_type] = []
            for rel in relationships:
                results[rel_type].append({
                    "direction": "outgoing",
                    "source": element,
                    "target": rel["target"],
                    "properties": rel["properties"]
                })
                
        for rel_type, relationships in incoming.items():
            if rel_type not in results:
                results[rel_type] = []
            for rel in relationships:
                results[rel_type].append({
                    "direction": "incoming",
                    "source": rel["source"],
                    "target": element,
                    "properties": rel["properties"]
                })
                
        return results
        
    def find_related_elements(self, element: str, 
                             max_distance: int = 1,
                             relationship_types: Optional[List[str]] = None) -> Dict[str, List[Dict[str, Any]]]:
        """
        Find elements related to the given element within a certain distance.
        
        Args:
            element: The central element to find relationships for
            max_distance: Maximum distance (hops) to search
            relationship_types: Optional list of relationship types to include
            
        Returns:
            Dictionary mapping distances to lists of related elements
        """
        if not self.traversal:
            logger.warning("No traversal utility available for finding related elements")
            return {}
            
        results = {}
        
        # Get elements at each distance
        for distance in range(1, max_distance + 1):
            # Find elements at this distance
            related = self.traversal.find_nodes_at_distance(
                element, 
                distance=distance,
                edge_types=relationship_types
            )
            
            if related:
                results[distance] = []
                for node, path in related:
                    path_info = []
                    for i in range(len(path) - 1):
                        edge_data = self.store.get_edge(path[i], path[i+1]) or {}
                        path_info.append({
                            "source": path[i],
                            "target": path[i+1],
                            "relationship_type": edge_data.get("edge_type", "unknown")
                        })
                        
                    results[distance].append({
                        "element": node,
                        "path": path_info
                    })
                    
        return results 
    
    
    # ----------------------------------------------------
    # Visualization methods
    # ----------------------------------------------------
    
    def get_visualization_data(self, 
                             nodes: Optional[List[str]] = None,
                             relationship_types: Optional[List[str]] = None,
                             include_node_properties: bool = True,
                             include_edge_properties: bool = True) -> Dict[str, Any]:
        """
        Get data for visualization purposes.
        
        Args:
            nodes: Optional list of nodes to include (all nodes if None)
            relationship_types: Optional list of relationship types to include
            include_node_properties: Whether to include node properties in the output
            include_edge_properties: Whether to include edge properties in the output
            
        Returns:
            Dictionary with nodes and links for visualization
        """
        # Get nodes
        all_nodes = nodes or self.store.get_all_nodes()
        
        # Prepare data structure
        vis_data = {
            "nodes": [],
            "links": []
        }
        
        # Process nodes
        for node_id in all_nodes:
            node_data = {"id": node_id}
            
            # Include node properties if requested
            if include_node_properties:
                node_props = self.store.get_node_properties(node_id) or {}
                
                # Set node type and color
                node_type = node_props.get("node_type", NODE_TYPE_UNKNOWN)
                node_data["type"] = node_type
                node_data["color"] = NODE_COLORS.get(node_type, NODE_COLORS[NODE_TYPE_UNKNOWN])
                
                # Add other properties
                if node_props:
                    node_data["properties"] = {k: v for k, v in node_props.items() 
                                             if k != "node_type"}
            
            vis_data["nodes"].append(node_data)
        
        # Process edges/links
        all_edges = []
        for source in all_nodes:
            for target, edge_data in self.store.get_successors(source):
                # Skip if target node is not in our filtered set
                if nodes and target not in nodes:
                    continue
                    
                edge_type = edge_data.get("edge_type", "unknown")
                
                # Filter by relationship type if provided
                if relationship_types and edge_type not in relationship_types:
                    continue
                
                all_edges.append((source, target, edge_data))
        
        # Add edges to visualization data
        for source, target, edge_data in all_edges:
            edge_type = edge_data.get("edge_type", "unknown")
            link_data = {
                "source": source,
                "target": target,
                "type": edge_type
            }
            
            # Add styling information based on relationship type
            if edge_type in RELATIONSHIP_STYLES:
                link_data.update(RELATIONSHIP_STYLES[edge_type])
            
            # Include edge properties if requested
            if include_edge_properties and edge_data:
                filtered_props = {k: v for k, v in edge_data.items() 
                                if k not in ("edge_type", "_bidirectional_managed")}
                if filtered_props:
                    link_data["properties"] = filtered_props
            
            vis_data["links"].append(link_data)
        
        return vis_data
    
    def export_visualization(self, 
                           output_path: str,
                           nodes: Optional[List[str]] = None,
                           relationship_types: Optional[List[str]] = None,
                           format: str = "json") -> bool:
        """
        Export visualization data to a file.
        
        Args:
            output_path: Path to save the output file
            nodes: Optional list of nodes to include
            relationship_types: Optional list of relationship types to include
            format: Output format (currently only 'json' supported)
            
        Returns:
            True if the export was successful, False otherwise
        """
        if format.lower() != "json":
            logger.warning(f"Unsupported format: {format}. Only 'json' is currently supported.")
            return False
            
        try:
            # Get visualization data
            vis_data = self.get_visualization_data(
                nodes=nodes,
                relationship_types=relationship_types
            )
            
            # Export to file
            with open(output_path, 'w') as f:
                json.dump(vis_data, f, indent=2)
                
            logger.info(f"Exported visualization data to {output_path}")
            return True
        except Exception as e:
            logger.error(f"Error exporting visualization data: {e}")
            return False
    
    def filter_graph(self, 
                    node_filter: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
                    relationship_filter: Optional[Callable[[str, str, Dict[str, Any]], bool]] = None) -> 'RelationshipTracker':
        """
        Create a filtered view of the relationship graph.
        
        Args:
            node_filter: Optional function that takes (node_id, properties) and returns
                        True to include the node, False to exclude it
            relationship_filter: Optional function that takes (source, target, properties)
                                and returns True to include the relationship, False to exclude it
                                
        Returns:
            A new RelationshipTracker instance with the filtered data
        """
        # Create a new tracker
        filtered = RelationshipTracker(manage_bidirectional=False)
        
        # Get all nodes
        all_nodes = self.store.get_all_nodes()
        
        # Filter and add nodes
        included_nodes = set()
        for node_id in all_nodes:
            node_props = self.store.get_node_properties(node_id) or {}
            
            # Check node filter
            if node_filter and not node_filter(node_id, node_props):
                continue
                
            # Add node to new graph
            filtered.store.add_node(node_id, **node_props)
            included_nodes.add(node_id)
        
        # Add edges between included nodes
        for source in included_nodes:
            for target, edge_data in self.store.get_successors(source):
                # Skip if target was filtered out
                if target not in included_nodes:
                    continue
                    
                # Check relationship filter
                if relationship_filter and not relationship_filter(source, target, edge_data):
                    continue
                    
                # Add edge to new graph
                edge_type = edge_data.get("edge_type", "unknown")
                filtered.store.add_edge(source, target, edge_type, **edge_data)
                
                # Update relationship counts
                if edge_type not in filtered._relationship_counts:
                    filtered._relationship_counts[edge_type] = 0
                filtered._relationship_counts[edge_type] += 1
        
        return filtered

    def highlight_paths(self, 
                      source: str, 
                      target: str,
                      max_depth: int = 5,
                      relationship_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Find and highlight paths between two nodes.
        
        Args:
            source: Source node
            target: Target node
            max_depth: Maximum path depth to search
            relationship_types: Optional list of relationship types to consider
            
        Returns:
            Visualization data with paths highlighted
        """
        if not self.traversal:
            logger.warning("No traversal utility available for finding paths")
            return {}
            
        # Find all paths between source and target
        paths = self.traversal.find_all_paths(
            source, 
            target,
            max_depth=max_depth,
            edge_types=relationship_types
        )
        
        if not paths:
            logger.info(f"No paths found between {source} and {target}")
            return {"nodes": [], "links": [], "paths": []}
        
        # Collect all nodes and edges in the paths
        path_nodes = set()
        path_edges = set()
        
        for path in paths:
            # Add all nodes in the path
            for node in path:
                path_nodes.add(node)
                
            # Add all edges in the path
            for i in range(len(path) - 1):
                source_node = path[i]
                target_node = path[i+1]
                # Edges are identified by their endpoints
                path_edges.add((source_node, target_node))
        
        # Get visualization data for just these nodes
        vis_data = self.get_visualization_data(nodes=list(path_nodes))
        
        # Add path information
        vis_data["paths"] = [
            {"path": path, "length": len(path) - 1}
            for path in paths
        ]
        
        # Mark edges that are part of paths
        for link in vis_data["links"]:
            source = link["source"]
            target = link["target"]
            if (source, target) in path_edges:
                link["in_path"] = True
                link["highlighted"] = True
                # Make path edges more prominent
                link["width"] = 2.5
        
        return vis_data
        
    # ----------------------------------------------------
    # Performance optimization methods
    # ----------------------------------------------------
    
    def create_index(self, relationship_type: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Create an index for faster relationship lookups by type.
        
        This builds an in-memory index for relationships of a specific type,
        which can greatly speed up frequent lookups.
        
        Args:
            relationship_type: Type of relationship to index
            
        Returns:
            Dictionary mapping source nodes to their relationships of this type
        """
        index = {}
        
        # Find all relationships of the given type
        relationships = self.find_relationships(relationship_type)
        
        # Build the index
        for rel in relationships:
            source = rel["source"]
            if source not in index:
                index[source] = []
            index[source].append(rel)
            
        logger.debug(f"Created index for relationship type {relationship_type} with {len(index)} entries")
        return index
    
    def precompute_paths(self, 
                        important_nodes: List[str],
                        max_depth: int = 3,
                        relationship_types: Optional[List[str]] = None) -> Dict[Tuple[str, str], List[List[str]]]:
        """
        Precompute paths between important nodes for faster path queries.
        
        Args:
            important_nodes: List of nodes to precompute paths between
            max_depth: Maximum path depth
            relationship_types: Optional list of relationship types to consider
            
        Returns:
            Dictionary mapping node pairs to lists of paths
        """
        if not self.traversal:
            logger.warning("No traversal utility available for precomputing paths")
            return {}
            
        path_cache = {}
        total_node_pairs = len(important_nodes) * (len(important_nodes) - 1) // 2
        
        logger.info(f"Precomputing paths between {len(important_nodes)} nodes ({total_node_pairs} pairs)")
        
        # Compute paths between each pair of nodes
        for i, source in enumerate(important_nodes):
            for j, target in enumerate(important_nodes):
                # Skip self-paths and duplicates (we only need pairs once)
                if i >= j:
                    continue
                    
                # Find paths
                paths = self.traversal.find_all_paths(
                    source, 
                    target,
                    max_depth=max_depth,
                    edge_types=relationship_types
                )
                
                if paths:
                    path_cache[(source, target)] = paths
                    # Also store the reverse lookup
                    path_cache[(target, source)] = [path[::-1] for path in paths]
        
        logger.info(f"Precomputed {len(path_cache)} path entries")
        return path_cache
    
    def compute_node_metrics(self) -> Dict[str, Dict[str, Any]]:
        """
        Compute various metrics for each node in the graph.
        
        This calculates metrics like:
        - Degree centrality (number of connections)
        - Page rank importance
        - Number of incoming and outgoing relationships by type
        
        Returns:
            Dictionary mapping node IDs to their metrics
        """
        node_metrics = {}
        all_nodes = self.store.get_all_nodes()
        
        for node_id in all_nodes:
            # Initialize metrics structure
            metrics = {
                "degree": 0,
                "in_degree": 0,
                "out_degree": 0,
                "relationship_counts": {},
                "importance": 0.0
            }
            
            # Get incoming and outgoing relationships
            incoming = self.get_incoming_relationships(node_id)
            outgoing = self.get_outgoing_relationships(node_id)
            
            # Count relationships by type
            incoming_count = 0
            for rel_type, rels in incoming.items():
                incoming_count += len(rels)
                if rel_type not in metrics["relationship_counts"]:
                    metrics["relationship_counts"][rel_type] = {"in": 0, "out": 0}
                metrics["relationship_counts"][rel_type]["in"] = len(rels)
                
            outgoing_count = 0
            for rel_type, rels in outgoing.items():
                outgoing_count += len(rels)
                if rel_type not in metrics["relationship_counts"]:
                    metrics["relationship_counts"][rel_type] = {"in": 0, "out": 0}
                metrics["relationship_counts"][rel_type]["out"] = len(rels)
            
            # Set degree metrics
            metrics["in_degree"] = incoming_count
            metrics["out_degree"] = outgoing_count
            metrics["degree"] = incoming_count + outgoing_count
            
            node_metrics[node_id] = metrics
        
        # Compute approximate Page Rank
        if self.traversal:
            try:
                # Simple PageRank implementation (if traversal utility supports it)
                page_ranks = self.traversal.compute_page_rank()
                for node_id, rank in page_ranks.items():
                    if node_id in node_metrics:
                        node_metrics[node_id]["importance"] = rank
            except (AttributeError, NotImplementedError):
                # If traversal doesn't support PageRank, use degree as approximation
                total_degree = sum(m["degree"] for m in node_metrics.values()) or 1
                for node_id, metrics in node_metrics.items():
                    metrics["importance"] = metrics["degree"] / total_degree
        
        logger.info(f"Computed metrics for {len(node_metrics)} nodes")
        return node_metrics
    
    def optimize_memory_usage(self) -> Dict[str, Any]:
        """
        Optimize memory usage by compacting internal data structures.
        
        Returns:
            Dictionary with memory optimization statistics
        """
        stats = {
            "nodes_before": len(self.store.get_all_nodes()),
            "edges_before": sum(1 for _ in self.store.get_all_edges()),
            "nodes_after": 0,
            "edges_after": 0,
            "memory_reduction_estimate": 0
        }
        
        try:
            # Remove any orphaned nodes (nodes with no connections)
            nodes_to_remove = []
            for node in self.store.get_all_nodes():
                if (not list(self.store.get_successors(node)) and 
                    not list(self.store.get_predecessors(node))):
                    nodes_to_remove.append(node)
            
            for node in nodes_to_remove:
                self.store.remove_node(node)
                
            logger.info(f"Removed {len(nodes_to_remove)} orphaned nodes")
            
            # Compact the internal storage if the store supports it
            if hasattr(self.store, "compact") and callable(self.store.compact):
                self.store.compact()
                logger.info("Compacted graph store")
            
            # Get updated stats
            stats["nodes_after"] = len(self.store.get_all_nodes())
            stats["edges_after"] = sum(1 for _ in self.store.get_all_edges())
            
            # Estimate memory reduction (very approximate)
            nodes_reduction = stats["nodes_before"] - stats["nodes_after"]
            edges_reduction = stats["edges_before"] - stats["edges_after"]
            
            # Rough estimate: ~100 bytes per node, ~50 bytes per edge
            memory_reduction = (nodes_reduction * 100) + (edges_reduction * 50)
            stats["memory_reduction_estimate"] = memory_reduction
            
            logger.info(f"Memory optimization complete. Estimated reduction: {memory_reduction/1024:.2f} KB")
        except Exception as e:
            logger.error(f"Error during memory optimization: {e}")
            stats["error"] = str(e)
        
        return stats
    
    def build_relationship_cache(self, relationship_types: Optional[List[str]] = None) -> Dict[str, Dict[str, List]]:
        """
        Build a cache of relationships for faster lookups.
        
        Args:
            relationship_types: List of relationship types to cache, or None for all types
            
        Returns:
            Dictionary mapping relationship types to caches
        """
        rel_types = relationship_types or self.get_relationship_count().keys()
        
        cache = {}
        for rel_type in rel_types:
            # Skip if no relationships of this type
            if self.get_relationship_count(rel_type) == 0:
                continue
                
            # Create an index for this relationship type
            cache[rel_type] = self.create_index(rel_type)
            
        logger.info(f"Built relationship cache for {len(cache)} relationship types")
        return cache
        
    def analyze_query_performance(self, 
                                query_samples: List[Tuple[str, str, str]],
                                iterations: int = 10) -> Dict[str, Any]:
        """
        Analyze query performance for typical relationship queries.
        
        Args:
            query_samples: List of (source, target, relationship_type) tuples to benchmark
            iterations: Number of iterations for each query
            
        Returns:
            Dictionary with performance statistics
        """
        import time
        
        results = {
            "queries": [],
            "total_time": 0,
            "average_time": 0,
            "slowest_query": None,
            "fastest_query": None
        }
        
        if not query_samples:
            logger.warning("No query samples provided for performance analysis")
            return results
            
        total_time = 0
        slowest_time = 0
        fastest_time = float('inf')
        
        for source, target, rel_type in query_samples:
            query_info = {
                "source": source,
                "target": target,
                "relationship_type": rel_type,
                "times": []
            }
            
            # Run the query multiple times
            for _ in range(iterations):
                start_time = time.time()
                
                # Perform the query
                if source and target and rel_type:
                    self.has_relationship(source, target, rel_type)
                elif source and rel_type:
                    self.find_relationships(rel_type, source=source)
                elif target and rel_type:
                    self.find_relationships(rel_type, target=target)
                elif rel_type:
                    self.find_relationships(rel_type)
                
                end_time = time.time()
                query_time = end_time - start_time
                
                query_info["times"].append(query_time)
                total_time += query_time
                
                # Track slowest and fastest queries
                if query_time > slowest_time:
                    slowest_time = query_time
                    results["slowest_query"] = (source, target, rel_type)
                
                if query_time < fastest_time:
                    fastest_time = query_time
                    results["fastest_query"] = (source, target, rel_type)
            
            # Calculate average time for this query
            query_info["average_time"] = sum(query_info["times"]) / len(query_info["times"])
            results["queries"].append(query_info)
        
        # Calculate overall statistics
        results["total_time"] = total_time
        results["average_time"] = total_time / (len(query_samples) * iterations)
        
        logger.info(f"Query performance analysis complete. Avg time: {results['average_time']:.6f}s")
        return results 

    def _analysis_functions(self) -> Dict[str, Callable]:
        """
        Get available analysis functions.
        
        Returns:
            Dict mapping analysis names to their functions
        """
        return {
            "centrality": self._analyze_centrality,
            "path_metrics": self._analyze_path_metrics,
            "coupling": self._analyze_coupling,
            "clustering": self._analyze_clustering,
            "dependency_impact": self._analyze_dependency_impact
        }
    
    def analyze_relationships(self,
                              analysis_type: str,
                              nodes: Optional[List[str]] = None,
                            relationship_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Run a specific analysis on relationships in the graph.
        
        Args:
            analysis_type: Type of analysis (e.g., 'centrality', 'coupling').
            nodes: Optional list of nodes to focus the analysis on.
            relationship_types: Optional list of relationship types to consider.
            
        Returns:
            Dictionary containing the analysis results.
        """
        logger.info(f"Running relationship analysis: {analysis_type}")
        # Ensure traversal utility is available
        if not self._traversal:
            logger.warning("Graph traversal utility not available for analysis.")
            return {"error": "Traversal utility not initialized"}

        # Delegate to GraphTraversal for complex analyses
        try:
            if analysis_type == 'centrality':
                return self._traversal.calculate_degree_centrality(nodes, relationship_types)
            elif analysis_type == 'path_metrics':
                return self._traversal.calculate_path_metrics(nodes, relationship_types)
            elif analysis_type == 'coupling':
                 # Use existing constants
                 coupling_rels = relationship_types or [REL_TYPE_CALLS, REL_TYPE_INHERITS, REL_TYPE_USES]
                 return self._traversal.analyze_coupling(nodes, coupling_rels)
            elif analysis_type == 'clustering':
                 return self._traversal.calculate_clustering_coefficient(nodes, relationship_types)
            elif analysis_type == 'dependency_impact':
                 # Use existing constants
                 impact_rels = relationship_types or [REL_TYPE_CALLS, REL_TYPE_IMPORTS, REL_TYPE_DEPENDS_ON]
                 if not nodes:
                     return {"error": "Nodes must be specified for dependency impact analysis"}
                 return self._traversal.analyze_dependency_impact(nodes, impact_rels)
            else:
                logger.warning(f"Unsupported analysis type: {analysis_type}")
                return {"error": f"Unsupported analysis type: {analysis_type}"}
        except Exception as e:
            logger.error(f"Error during relationship analysis '{analysis_type}': {e}", exc_info=True)
            return {"error": f"Analysis failed: {e}"}
    
    def _analyze_centrality(self, nodes: Optional[List[str]] = None, 
                          relationship_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Analyze node centrality in the graph.
        
        Args:
            nodes: Optional list of nodes to analyze
            relationship_types: Optional list of relationship types to include
            
        Returns:
            Centrality metrics for nodes
        """
        # Filter graph if needed
        if nodes or relationship_types:
            subgraph = self.filter_graph(
                node_filter=lambda n: nodes is None or n in nodes,
                relationship_filter=lambda s, t, data: relationship_types is None 
                                                    or data.get("edge_type") in relationship_types
            )
            graph = subgraph.store
        else:
            graph = self.store
            
        # Calculate degree centrality (normalized by max possible connections)
        node_count = graph.node_count()
        if node_count <= 1:
            return {"centrality": {}}
            
        normalize_factor = 1.0 / (node_count - 1)
        centrality = {}
        
        for node_id in graph.get_node_ids():
            # Count both incoming and outgoing edges
            in_degree = len(graph.get_predecessors(node_id))
            out_degree = len(graph.get_successors(node_id))
            total_degree = in_degree + out_degree
            
            # Normalize
            centrality[node_id] = {
                "raw_degree": total_degree,
                "in_degree": in_degree,
                "out_degree": out_degree,
                "normalized_degree": total_degree * normalize_factor
            }
            
        # Find top nodes by centrality
        top_nodes = sorted(
            centrality.items(), 
            key=lambda x: x[1]["normalized_degree"], 
            reverse=True
        )[:10]
        
        return {
            "centrality": centrality,
            "top_nodes": [{"node_id": n[0], "centrality": n[1]} for n in top_nodes]
        }
    
    def _analyze_path_metrics(self, nodes: Optional[List[str]] = None, 
                            relationship_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Analyze path-based metrics like average path length.
        
        Args:
            nodes: Optional list of nodes to analyze
            relationship_types: Optional list of relationship types to include
            
        Returns:
            Path metrics
        """
        # This requires the traversal component
        if not self.traversal:
            return {"error": "Traversal functionality not available"}
            
        # Filter nodes if provided
        if nodes is None:
            nodes = list(self.store.get_node_ids())
            
        if len(nodes) <= 1:
            return {"avg_path_length": 0, "diameter": 0, "path_count": 0}
            
        # Sample nodes if too many (for performance)
        if len(nodes) > 100:
            import random
            nodes = random.sample(nodes, 100)
            
        # Calculate metrics
        path_lengths = []
        max_length = 0
        
        # Use a batched approach to avoid excessive computation
        for i, source in enumerate(nodes):
            for target in nodes[i+1:]:
                # Find paths with optional relationship type filtering
                paths = self.traversal.find_all_paths(
                    source, target, max_depth=10, 
                    edge_type_filter=relationship_types
                )
                
                if paths:
                    min_length = min(len(path) for path in paths)
                    path_lengths.append(min_length)
                    max_length = max(max_length, min_length)
                    
        # Calculate metrics
        if path_lengths:
            avg_path_length = sum(path_lengths) / len(path_lengths)
        else:
            avg_path_length = 0
            
        return {
            "avg_path_length": avg_path_length,
            "diameter": max_length,
            "path_count": len(path_lengths),
            "connected": len(path_lengths) > 0
        }
    
    def _analyze_coupling(self, nodes: Optional[List[str]] = None, 
                        relationship_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Analyze coupling between components.
        
        Args:
            nodes: Optional list of nodes to analyze
            relationship_types: Optional list of relationship types to include
            
        Returns:
            Coupling metrics
        """
        # Filter graph if needed
        if nodes or relationship_types:
            subgraph = self.filter_graph(
                node_filter=lambda n: nodes is None or n in nodes,
                relationship_filter=lambda s, t, data: relationship_types is None 
                                                    or data.get("edge_type") in relationship_types
            )
            graph = subgraph.store
        else:
            graph = self.store
            
        # Get all nodes
        all_nodes = list(graph.get_node_ids())
        
        # Calculate coupling for each pair of nodes
        coupling = {}
        highly_coupled = []
        
        for i, node1 in enumerate(all_nodes):
            coupling[node1] = {}
            
            # Get outgoing and incoming relationships
            out_edges = {edge[0] for edge in graph.get_successors(node1)}
            in_edges = {edge[0] for edge in graph.get_predecessors(node1)}
            
            for node2 in all_nodes[i+1:]:
                # Skip same node
                if node1 == node2:
                    continue
                    
                # Calculate bidirectional coupling
                out_edges2 = {edge[0] for edge in graph.get_successors(node2)}
                in_edges2 = {edge[0] for edge in graph.get_predecessors(node2)}
                
                # Calculate relationship counts in each direction
                n1_to_n2 = node2 in out_edges
                n2_to_n1 = node1 in out_edges2
                
                # Check for shared targets (outgoing dependencies)
                shared_out = len(out_edges.intersection(out_edges2))
                
                # Check for shared sources (incoming dependencies)
                shared_in = len(in_edges.intersection(in_edges2))
                
                # Total coupling score
                coupling_score = (n1_to_n2 + n2_to_n1) * 2 + shared_out + shared_in
                
                if coupling_score > 0:
                    coupling[node1][node2] = coupling_score
                    
                    if coupling_score >= 3:
                        highly_coupled.append((node1, node2, coupling_score))
                        
        # Sort by coupling score
        highly_coupled.sort(key=lambda x: x[2], reverse=True)
        
        return {
            "coupling": coupling,
            "highly_coupled": [{"source": s, "target": t, "score": c} 
                              for s, t, c in highly_coupled[:10]]
        }
    
    def _analyze_clustering(self, nodes: Optional[List[str]] = None, 
                          relationship_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Analyze clustering of nodes in the graph.
        
        Args:
            nodes: Optional list of nodes to analyze
            relationship_types: Optional list of relationship types to include
            
        Returns:
            Clustering metrics
        """
        # Filter graph if needed
        if nodes or relationship_types:
            subgraph = self.filter_graph(
                node_filter=lambda n: nodes is None or n in nodes,
                relationship_filter=lambda s, t, data: relationship_types is None 
                                                    or data.get("edge_type") in relationship_types
            )
            graph = subgraph.store
        else:
            graph = self.store
            
        # Get all nodes
        all_nodes = list(graph.get_node_ids())
        clustering = {}
        
        for node in all_nodes:
            # Get neighbors (both incoming and outgoing)
            neighbors = set()
            for succ, _ in graph.get_successors(node):
                neighbors.add(succ)
            for pred, _ in graph.get_predecessors(node):
                neighbors.add(pred)
                
            # Skip nodes with fewer than 2 neighbors (clustering undefined)
            if len(neighbors) < 2:
                clustering[node] = 0
                continue
                
            # Count connections between neighbors
            connections = 0
            for n1 in neighbors:
                for n2 in neighbors:
                    if n1 != n2 and graph.has_edge(n1, n2):
                        connections += 1
                        
            # Calculate clustering coefficient
            max_connections = len(neighbors) * (len(neighbors) - 1)
            if max_connections > 0:
                coefficient = connections / max_connections
            else:
                coefficient = 0
                
            clustering[node] = coefficient
            
        # Calculate global metrics
        avg_clustering = sum(clustering.values()) / len(clustering) if clustering else 0
        
        return {
            "clustering": clustering,
            "average_clustering": avg_clustering
        }
    
    def _analyze_dependency_impact(self, nodes: Optional[List[str]] = None, 
                                 relationship_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Analyze impact of changes to specific nodes.
        
        Args:
            nodes: Optional list of nodes to analyze
            relationship_types: Optional list of relationship types to include
            
        Returns:
            Impact analysis metrics
        """
        # Filter graph if needed
        if nodes or relationship_types:
            subgraph = self.filter_graph(
                node_filter=lambda n: nodes is None or n in nodes,
                relationship_filter=lambda s, t, data: relationship_types is None 
                                                    or data.get("edge_type") in relationship_types
            )
            graph = subgraph.store
        else:
            graph = self.store
            
        # Get nodes to analyze
        if nodes is None:
            nodes = list(graph.get_node_ids())
            
        # Calculate impact for each node
        impact = {}
        
        for node in nodes:
            # Breadth-first search to find all affected nodes
            visited = {node}
            queue = [node]
            depth_map = {node: 0}
            max_depth = 0
            
            while queue:
                current = queue.pop(0)
                current_depth = depth_map[current]
                
                # Find nodes depending on the current node
                for dependent, _ in graph.get_predecessors(current):
                    if dependent not in visited:
                        visited.add(dependent)
                        queue.append(dependent)
                        depth_map[dependent] = current_depth + 1
                        max_depth = max(max_depth, current_depth + 1)
                        
            # Calculate impact metrics
            affected_count = len(visited) - 1  # Don't count the node itself
            
            impact[node] = {
                "affected_nodes": list(visited - {node}),
                "affected_count": affected_count,
                "max_depth": max_depth
            }
            
        # Sort nodes by impact
        high_impact = sorted(
            impact.items(), 
            key=lambda x: x[1]["affected_count"], 
            reverse=True
        )[:10]
        
        return {
            "impact": impact,
            "high_impact_nodes": [{"node_id": n[0], "impact": n[1]} for n in high_impact]
        }
    
    def bulk_add_relationships(self, relationships: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        Add multiple relationships in a batch operation.
        
        Args:
            relationships: List of relationship dictionaries, each with:
                           - source: source node
                           - target: target node
                           - relationship_type: type of relationship
                           - properties: optional properties
                           
        Returns:
            Dictionary with counts of relationships added
        """
        if not relationships:
            return {"added": 0, "skipped": 0, "total": 0}
            
        # Start a batch operation in the store
        self.store.begin_batch()
        
        added = 0
        skipped = 0
        total = len(relationships)
        
        for rel in relationships:
            # Extract relationship data
            source = rel.get("source")
            target = rel.get("target")
            rel_type = rel.get("relationship_type")
            properties = rel.get("properties", {})
            
            # Skip incomplete data
            if not source or not target or not rel_type:
                logger.warning(f"Skipping relationship with incomplete data: {rel}")
                skipped += 1
                continue
                
            # Add the relationship
            self.add_relationship(source, target, rel_type, properties)
            added += 1
            
        # Commit the batch
        self.store.commit_batch()
        
        return {
            "added": added,
            "skipped": skipped,
            "total": total
        }
    
    def detect_communities(self, relationship_types: Optional[List[str]] = None, min_community_size: int = 2) -> Dict[str, List[str]]:
        """Detects communities (clusters) of related nodes in the graph."""
        if not self._traversal:
            logger.warning("Traversal utility not available for community detection.")
            return {}

        # Default to common structural relationships if none specified
        effective_rel_types = relationship_types or [REL_TYPE_CALLS, REL_TYPE_INHERITS, REL_TYPE_IMPORTS] # Use imported constants

        try:
            communities = self._traversal.detect_communities(effective_rel_types, min_community_size)
            logger.info(f"Detected {len(communities)} communities.")
            return communities
        except Exception as e:
            logger.error(f"Error during community detection: {e}", exc_info=True)
            return {}
    
    def calculate_pagerank(self, damping: float = 0.85, max_iterations: int = 100, relationship_types: Optional[List[str]] = None) -> Dict[str, float]:
        """Calculates PageRank scores for nodes in the graph."""
        if not self._traversal:
            logger.warning("Traversal utility not available for PageRank calculation.")
            return {}

        # Default relationships to consider for PageRank
        effective_rel_types = relationship_types or [REL_TYPE_CALLS, REL_TYPE_IMPORTS] # Use imported constants

        try:
            pagerank_scores = self._traversal.calculate_pagerank(damping, max_iterations, effective_rel_types)
            logger.info(f"Calculated PageRank for {len(pagerank_scores)} nodes.")
            return pagerank_scores
        except Exception as e:
            logger.error(f"Error during PageRank calculation: {e}", exc_info=True)
            return {}
    
    
    # --- Graph Data Generation for Visualization ---

    def get_call_graph_data(self, nodes: Optional[List[str]] = None) -> Dict[str, Any]:
        """Get data specifically for visualizing the call graph."""
        return self._get_graph_data_for_types([REL_TYPE_CALLS], nodes)

    def get_inheritance_graph_data(self, nodes: Optional[List[str]] = None) -> Dict[str, Any]:
        """Get data specifically for visualizing the inheritance graph."""
        return self._get_graph_data_for_types([REL_TYPE_INHERITS], nodes)

    def get_reference_graph_data(self, nodes: Optional[List[str]] = None) -> Dict[str, Any]:
         """Get data for visualizing reference relationships."""
         return self._get_graph_data_for_types([REL_TYPE_REFERENCES], nodes)

    def get_containment_graph_data(self, nodes: Optional[List[str]] = None) -> Dict[str, Any]:
         """Get data for visualizing containment relationships."""
         return self._get_graph_data_for_types([REL_TYPE_CONTAINS], nodes)

    def get_dependency_graph_data(self, nodes: Optional[List[str]] = None) -> Dict[str, Any]:
         """Get data for visualizing general dependency relationships."""
         return self._get_graph_data_for_types([REL_TYPE_DEPENDS_ON], nodes)

    def get_implementation_graph_data(self, nodes: Optional[List[str]] = None) -> Dict[str, Any]:
         """Get data for visualizing implementation relationships (interfaces)."""
         return self._get_graph_data_for_types([REL_TYPE_IMPLEMENTS], nodes)

    def get_usage_graph_data(self, nodes: Optional[List[str]] = None) -> Dict[str, Any]:
         """Get data for visualizing usage relationships (e.g., variable usage)."""
         return self._get_graph_data_for_types([REL_TYPE_USES], nodes)

    def get_override_graph_data(self, nodes: Optional[List[str]] = None) -> Dict[str, Any]:
         """Get data for visualizing method override relationships."""
         return self._get_graph_data_for_types([REL_TYPE_OVERRIDES], nodes)

    def get_data_access_graph_data(self, nodes: Optional[List[str]] = None) -> Dict[str, Any]:
         """Get data for visualizing data access/modification relationships."""
         return self._get_graph_data_for_types([REL_TYPE_CONTAINS], nodes)


    def _get_graph_data_for_types(self, rel_types: List[str], nodes: Optional[List[str]] = None) -> Dict[str, Any]:
        """Helper to get nodes and edges for specific relationship types."""
        # ... (implementation likely uses find_relationships) ...
        pass # Placeholder for actual implementation
    
    
    # --- Visualization Helpers ---

    def _get_node_type(self, node_id: str) -> str:
        """Attempt to determine the type of a node (e.g., 'class', 'function')."""
        # This might involve looking at relationship types or stored node properties
        # Placeholder implementation
        if '.' in node_id:
            parts = node_id.split('.')
            last_part = parts[-1]
            if last_part.startswith('_'): # Private/protected often functions/methods/vars
                 if '(' in last_part: return 'method' # Crude guess
                 else: return 'variable'
            if last_part[0].isupper(): return 'class'
            if last_part[0].islower(): return 'function' # Simplified guess
        if node_id and all(part.islower() for part in node_id.split('.')):
            return 'module'
        return NODE_TYPE_UNKNOWN # Use imported constant

    def _get_node_style(self, node_type: str) -> Dict[str, str]:
        """Get Graphviz style attributes based on node type."""
        # Assumes NODE_COLORS is imported or defined
        color = NODE_COLORS.get(node_type, NODE_COLORS.get(NODE_TYPE_UNKNOWN, "#cccccc")) # Use imported constants
        style_map = {
            'module': {'shape': 'box', 'style': 'filled', 'fillcolor': color},
            'package': {'shape': 'folder', 'style': 'filled', 'fillcolor': color},
            'class': {'shape': 'ellipse', 'style': 'filled', 'fillcolor': color},
            'function': {'shape': 'ellipse', 'fillcolor': color, 'style': 'filled'},
            'method': {'shape': 'ellipse', 'fillcolor': color, 'style': 'filled'},
            'variable': {'shape': 'plaintext'}, # Example for variable
            'interface': {'shape': 'diamond', 'style': 'filled', 'fillcolor': color},
            NODE_TYPE_UNKNOWN: {'shape': 'point', 'fillcolor': color}
        }
        return style_map.get(node_type, style_map[NODE_TYPE_UNKNOWN])

    def _get_edge_style(self, relationship_type: str) -> Dict[str, str]:
        """Get Graphviz style attributes based on relationship type."""
        # Assumes RELATIONSHIP_STYLES is imported or defined
        default_style = {'color': '#888888', 'arrowhead': 'normal'}
        style = RELATIONSHIP_STYLES.get(relationship_type, default_style) # Use imported constant
        # Ensure essential keys are present
        final_style = default_style.copy()
        final_style.update(style)
        return final_style
    

    def remove_relationship_by_node_prefix(self, node_prefix: str, relationship_type: str) -> int:
        logger.info(f"[RelationshipTracker.remove_relationship_by_node_prefix] CALLED. Prefix: '{node_prefix}', Type: '{relationship_type}'")
        if not self.store:
            logger.error("[RelationshipTracker.remove_relationship_by_node_prefix] Store not initialized!")
            return 0

        edges_to_remove: List[Tuple[str, str, str]] = []
        
        # get_edges is called with relationship_type, which corresponds to the 'type' attribute on edges.
        # It yields (source, target, key, data_dict) where data_dict['type'] should match relationship_type.
        all_edges_of_type_gen = self.store.get_edges(edge_type=relationship_type)
        
        # It's safer to materialize the generator before modification if the underlying graph is changed by store.remove_edge
        # However, store.remove_edge in this loop is by specific key, so concurrent modification of the iterator source (self.graph.edges)
        # might be okay for NetworkX if it's robust, but it's cleaner to collect keys first.

        for source, target, key, data in all_edges_of_type_gen:
            # The semantic type of the edge is in data.get('type')
            current_semantic_type = data.get('type') 
            
            # This check should ideally not be necessary if get_edges correctly filtered by relationship_type (as edge_type param)
            if current_semantic_type != relationship_type:
                logger.warning(f"[RelationshipTracker.remove_relationship_by_node_prefix] Edge {source}->{target} (Key: {key}) from get_edges had unexpected type '{current_semantic_type}', expected '{relationship_type}'. Skipping.")
                continue

            if source.startswith(node_prefix) or target.startswith(node_prefix):
                logger.debug(f"[RelationshipTracker.remove_relationship_by_node_prefix] MATCH: Queuing edge for removal: {source}->{target} (Key: {key}, Type: {current_semantic_type})")
                edges_to_remove.append((source, target, key))

        removed_count = 0
        if not edges_to_remove:
            logger.info(f"[RelationshipTracker.remove_relationship_by_node_prefix] No edges of type '{relationship_type}' queued for removal for prefix '{node_prefix}'.")
        else:
            logger.info(f"[RelationshipTracker.remove_relationship_by_node_prefix] Attempting to remove {len(edges_to_remove)} queued edges of type '{relationship_type}'.")

        for i, (source_node, target_node, edge_key) in enumerate(edges_to_remove):
            logger.debug(f"[RelationshipTracker.remove_relationship_by_node_prefix] Removing item {i+1}/{len(edges_to_remove)}: {source_node}->{target_node} (Key: {edge_key}, Expected Semantic Type for removal op: {relationship_type})")
            
            # Pass the specific relationship_type (which is the semantic type) to ensure store.remove_edge checks it if it needs to.
            # The key `edge_key` is the primary identifier for removal.
            if self.store.remove_edge(source_node, target_node, edge_type=relationship_type, key=edge_key):
                removed_count += 1
                logger.info(f"[RelationshipTracker.remove_relationship_by_node_prefix] Store CONFIRMED removal of {source_node}->{target_node} (Key: {edge_key}). Current removed_count for type '{relationship_type}': {removed_count}")
                
                # Also remove the inverse relationship
                inverse_rel_type = RELATIONSHIP_PAIRS.get(relationship_type)
                if inverse_rel_type:
                    # Attempt to remove the inverse. The key for inverse might be just its type, or more complex.
                    # Assuming default keying for inverse for now (inverse_type).
                    # This part needs to be robust to how inverse keys are actually determined/stored.
                    # For now, let's assume RelationshipTracker.add_relationship sets up inverse key as inverse_type.
                    inverse_key_assumed = inverse_rel_type 
                    logger.debug(f"Attempting to remove corresponding inverse {target_node} -> {source_node} of type {inverse_rel_type} with assumed key {inverse_key_assumed}")
                    if self.store.remove_edge(target_node, source_node, edge_type=inverse_rel_type, key=inverse_key_assumed):
                         logger.info(f"Successfully removed inverse edge {target_node} -> {source_node} (Key: {inverse_key_assumed})")
                    # else: # Be less verbose if inverse wasn't found or failed, primary removal is main goal here
                        # logger.warning(f"Failed or inverse not found for {target_node} -> {source_node} (Key: {inverse_key_assumed})")

            else:
                logger.warning(f"[RelationshipTracker.remove_relationship_by_node_prefix] Store DENIED/FAILED removal of {source_node}->{target_node} (Key: {edge_key}, Type: {relationship_type})")

        logger.info(f"[RelationshipTracker.remove_relationship_by_node_prefix] COMPLETED. Total edges of type '{relationship_type}' confirmed removed for prefix '{node_prefix}': {removed_count} out of {len(edges_to_remove)} considered for removal.")
        if removed_count > 0:
            self._clear_caches() # Broad cache clear if any modification happened
        return removed_count