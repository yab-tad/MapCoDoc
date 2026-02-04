"""
Module for analyzing call graph relationships stored in a GraphStore.

Provides functions for cycle detection, path finding, metrics calculation, etc.
"""

import logging
from collections import deque, defaultdict
from typing import Dict, List, Optional, Set, Tuple, Any

from code_analysis.graph.store import GraphStore
from code_analysis.graph.traversal import GraphTraversal
from code_analysis.relationship_types import REL_TYPE_CALLS

logger = logging.getLogger(__name__)


def find_call_cycles(store: GraphStore, traversal: GraphTraversal) -> List[List[str]]:
    """
    Finds cycles in the call graph.

    Args:
        store: The GraphStore containing the call graph data.
        traversal: The GraphTraversal utility.

    Returns:
        A list of lists, where each inner list represents a cycle (path of FQNs).
    """
    logger.debug("Finding call cycles...")
    # Use the traversal utility which should have cycle detection logic
    cycles = traversal.find_cycles(edge_type=REL_TYPE_CALLS)
    logger.info(f"Found {len(cycles)} call cycles.")
    return cycles


def get_entry_points(store: GraphStore) -> List[str]:
    """
    Identifies functions/methods that are not called by any other tracked function/method within the analyzed scope.

    Args:
        store: The GraphStore containing the call graph data.

    Returns:
        A list of fully qualified names of entry point functions/methods.
    """
    logger.debug("Identifying entry points...")
    function_nodes = {node_id for node_id, _ in store.get_nodes(node_type='function')}
    method_nodes = {node_id for node_id, _ in store.get_nodes(node_type='method')}
    all_nodes = function_nodes | method_nodes
    
    called_nodes = set()
    for _, target, data in store.get_edges(edge_type=REL_TYPE_CALLS):
        called_nodes.add(target)

    entry_points = list(all_nodes - called_nodes)
    logger.info(f"Found {len(entry_points)} potential entry points.")
    return entry_points


def get_terminal_functions(store: GraphStore) -> List[str]:
    """
    Identifies functions/methods that do not call any other tracked function/method.

    Args:
        store: The GraphStore containing the call graph data.

    Returns:
        A list of fully qualified names of terminal functions/methods.
    """
    logger.debug("Identifying terminal functions...")
    function_nodes = {node_id for node_id, _ in store.get_nodes(node_type='function')}
    method_nodes = {node_id for node_id, _ in store.get_nodes(node_type='method')}
    all_nodes = function_nodes | method_nodes
    
    calling_nodes = set()
    for source, _, data in store.get_edges(edge_type=REL_TYPE_CALLS):
        calling_nodes.add(source)

    terminal_functions = list(all_nodes - calling_nodes)
    logger.info(f"Found {len(terminal_functions)} potential terminal functions.")
    return terminal_functions


def get_call_chain(traversal: GraphTraversal, start_node: str, end_node: str) -> List[str]:
    """
    Finds the shortest call chain (path) between two functions/methods.

    Args:
        traversal: The GraphTraversal utility.
        start_node: The FQN of the starting function/method.
        end_node: The FQN of the ending function/method.

    Returns:
        A list representing the shortest path, or an empty list if no path exists.
    """
    logger.debug(f"Finding shortest call chain from {start_node} to {end_node}...")
    path = traversal.find_shortest_path(start_node, end_node, edge_type=REL_TYPE_CALLS)
    logger.info(f"Shortest path found: {' -> '.join(path) if path else 'None'}")
    return path


def get_all_call_chains(traversal: GraphTraversal, start_node: str, end_node: str) -> List[List[str]]:
    """
    Finds all possible call chains (paths) between two functions/methods.

    Args:
        traversal: The GraphTraversal utility.
        start_node: The FQN of the starting function/method.
        end_node: The FQN of the ending function/method.

    Returns:
        A list of lists, where each inner list represents a path.
    """
    logger.debug(f"Finding all call chains from {start_node} to {end_node}...")
    paths = traversal.find_all_paths(start_node, end_node, edge_type=REL_TYPE_CALLS)
    logger.info(f"Found {len(paths)} paths from {start_node} to {end_node}.")
    return paths


def get_transitive_callees(traversal: GraphTraversal, start_node: str) -> Set[str]:
    """
    Finds all functions/methods transitively called by a starting function/method.

    Args:
        traversal: The GraphTraversal utility.
        start_node: The FQN of the starting function/method.

    Returns:
        A set of FQNs of all reachable callees.
    """
    logger.debug(f"Finding transitive callees for {start_node}...")
    # Use BFS or DFS from the traversal utility
    reachable_nodes = traversal.bfs(start_node, direction='out', edge_type=REL_TYPE_CALLS)
    # Remove the start node itself from the result
    reachable_nodes.discard(start_node)
    logger.info(f"Found {len(reachable_nodes)} transitive callees for {start_node}.")
    return reachable_nodes


def get_transitive_callers(traversal: GraphTraversal, start_node: str) -> Set[str]:
    """
    Finds all functions/methods that transitively call a starting function/method.

    Args:
        traversal: The GraphTraversal utility.
        start_node: The FQN of the starting function/method.

    Returns:
        A set of FQNs of all functions/methods that can reach the start_node.
    """
    logger.debug(f"Finding transitive callers for {start_node}...")
    # Use BFS or DFS from the traversal utility in the reverse direction
    reachable_nodes = traversal.bfs(start_node, direction='in', edge_type=REL_TYPE_CALLS)
    # Remove the start node itself
    reachable_nodes.discard(start_node)
    logger.info(f"Found {len(reachable_nodes)} transitive callers for {start_node}.")
    return reachable_nodes


def calculate_call_metrics(store: GraphStore) -> Dict[str, Any]:
    """
    Calculates various metrics about the call graph.

    Args:
        store: The GraphStore containing the call graph data.

    Returns:
        A dictionary containing metrics like node count, edge count, density, etc.
    """
    logger.debug("Calculating call graph metrics...")
    nodes = set(store.get_nodes(node_type='function')) | set(store.get_nodes(node_type='method')) # Adjust node types if needed
    edges = list(store.get_edges(edge_type=REL_TYPE_CALLS))

    node_count = len(nodes)
    edge_count = len(edges)
    density = (edge_count / (node_count * (node_count - 1))) if node_count > 1 else 0

    # Calculate Fan-in and Fan-out
    fan_in = defaultdict(int)
    fan_out = defaultdict(int)
    for source, target, _ in edges:
        fan_out[source] += 1
        fan_in[target] += 1

    avg_fan_in = sum(fan_in.values()) / node_count if node_count else 0
    avg_fan_out = sum(fan_out.values()) / node_count if node_count else 0
    max_fan_in = max(fan_in.values()) if fan_in else 0
    max_fan_out = max(fan_out.values()) if fan_out else 0

    metrics = {
        "node_count": node_count,
        "edge_count": edge_count,
        "graph_density": density,
        "average_fan_in": avg_fan_in,
        "average_fan_out": avg_fan_out,
        "max_fan_in": max_fan_in,
        "max_fan_out": max_fan_out,
        # Add more metrics as needed (e.g., components, centrality)
    }
    logger.info(f"Calculated call graph metrics: {metrics}")
    return metrics


def find_most_called_functions(store: GraphStore, top_n: int = 10) -> List[Tuple[str, int]]:
    """
    Finds the functions/methods that are called most often (highest fan-in).

    Args:
        store: The GraphStore containing the call graph data.
        top_n: The number of top functions to return.

    Returns:
        A list of tuples (function_fqn, call_count), sorted by call_count descending.
    """
    logger.debug(f"Finding top {top_n} most called functions...")
    fan_in = defaultdict(int)
    for _, target, _ in store.get_edges(edge_type=REL_TYPE_CALLS):
        fan_in[target] += 1

    sorted_functions = sorted(fan_in.items(), key=lambda item: item[1], reverse=True)
    return sorted_functions[:top_n]


def find_highest_fan_out_functions(store: GraphStore, top_n: int = 10) -> List[Tuple[str, int]]:
    """
    Finds the functions/methods that call the most other functions (highest fan-out).

    Args:
        store: The GraphStore containing the call graph data.
        top_n: The number of top functions to return.

    Returns:
        A list of tuples (function_fqn, call_count), sorted by call_count descending.
    """
    logger.debug(f"Finding top {top_n} highest fan-out functions...")
    fan_out = defaultdict(int)
    for source, _, _ in store.get_edges(edge_type=REL_TYPE_CALLS):
        fan_out[source] += 1

    sorted_functions = sorted(fan_out.items(), key=lambda item: item[1], reverse=True)
    return sorted_functions[:top_n]


def get_longest_call_chain(traversal: GraphTraversal, store: GraphStore) -> List[str]:
    """
    Finds the longest simple call chain (path with no repeated nodes) in the graph.
    This can be computationally expensive for large, dense graphs.

    Args:
        traversal: The GraphTraversal utility. (Used to access find_all_paths eventually)
        store: The GraphStore containing the call graph data. (Used to get all function/method nodes)

    Returns:
        A list representing the longest simple call path, or an empty list if no paths exist
        or no relevant nodes are found.
    """
    logger.debug("Attempting to find the longest call chain...")
    # Consider only function and method nodes for pathfinding
    # This assumes 'node_type' is an attribute on nodes.
    # If GraphStore.get_nodes doesn't support filtering by a list of types, might need to call it twice and combine results or adjust.
    # For simplicity, let's assume node_type exists and can be 'function' or 'method'
    
    # Get all function/method nodes.
    # The current GraphStore.get_nodes returns List[Tuple[str, Dict]], we need just node_ids.
    # Also, it filters by attributes. Let's assume we have a way to get relevant nodes.
    # A more robust way might be to iterate through all nodes and check their type attribute.
    
    # For now, let's assume 'get_node_ids_by_type' or similar exists or can be added to GraphStore or that GraphStore.get_nodes can filter by a list of types.
    # If not, we'd fetch all nodes and filter them here.
    
    # This is a simplified approach. A full pairwise longest path is very expensive.
    # Typically, "longest path" in a general graph (especially with cycles) is ill-defined
    # or NP-hard. "Longest simple path" is what's usually sought.
    # We'll iterate over all pairs of nodes and find the longest simple path among them.
    # This is still O(N^2 * P), where P is cost of all_simple_paths.
    
    # Get all nodes that could be part of a call chain.
    # Let's assume 'function' and 'method' are the relevant node_type values.
    all_call_nodes = set()
    # Assuming GraphStore has a way to get all nodes or nodes by type.
    # If get_nodes() returns all nodes:
    for node_id, data in store.get_nodes(): # This might be inefficient if get_nodes returns all nodes without type filter
        if data.get('node_type') in ['function', 'method']: # Check if node_type attribute exists
            all_call_nodes.add(node_id)
    
    if not all_call_nodes:
        logger.info("No function or method nodes found to determine longest call chain.")
        return []

    longest_path_found = []
    
    # This is computationally very intensive.
    # Consider if a more targeted "longest path from entry points" or similar is more practical.
    # For now, implementing based on the general idea of "longest simple path in the call graph".
    
    processed_pairs = set() # To avoid (a,b) and (b,a) if graph is treated as undirected for this purpose (which it isn't)

    for source_node in all_call_nodes:
        for target_node in all_call_nodes:
            if source_node == target_node:
                continue
            
            # Using traversal.find_all_paths which should use store.find_paths
            # that finds simple paths.
            # We need to set a reasonable max_depth for find_all_paths.
            # If max_depth is too low, we might miss the actual longest path.
            # If too high, it's too slow. Default in find_all_paths is 10.
            # For finding the "longest", this max_depth is a practical limit.
            
            # The find_all_paths in traversal takes an edge_type_filter.
            # It seems GraphStore.find_paths (which it calls) expects edge_type_filter, not just edge_type.
            paths = traversal.find_all_paths(source_node, target_node, edge_type_filter=REL_TYPE_CALLS, max_depth=20) # Increased max_depth
            
            for path in paths:
                if len(path) > len(longest_path_found):
                    longest_path_found = path
                    
    if longest_path_found:
        logger.info(f"Longest call chain found: {' -> '.join(longest_path_found)} (length {len(longest_path_found)})")
    else:
        logger.info("No call chains found.")
    return longest_path_found


def find_highly_coupled_functions(store: GraphStore, threshold: int) -> List[Tuple[str, int]]:
    """
    Finds functions/methods that are highly coupled based on fan-in + fan-out.

    Args:
        store: The GraphStore containing the call graph data.
        threshold: The minimum coupling score (fan_in + fan_out) to be considered highly coupled.

    Returns:
        A list of tuples (function_fqn, coupling_score), sorted by coupling_score descending.
    """
    logger.debug(f"Finding highly coupled functions with threshold {threshold}...")
    
    fan_in: Dict[str, int] = defaultdict(int)
    fan_out: Dict[str, int] = defaultdict(int)
    
    all_relevant_nodes = set()

    # Iterate through all CALLS edges to calculate fan-in and fan-out
    for source, target, data in store.get_edges(edge_type=REL_TYPE_CALLS):
        # Assuming source and target of CALLS edges are functions/methods
        fan_out[source] += 1
        fan_in[target] += 1
        all_relevant_nodes.add(source)
        all_relevant_nodes.add(target)
        
    coupled_functions: Dict[str, int] = {}
    for node_id in all_relevant_nodes:
        # Consider nodes that are explicitly typed as function or method if attribute is available
        node_data = store.get_node(node_id)
        if node_data and node_data.get('node_type') not in ['function', 'method']:
            # Skip if it's not a function or method node (e.g., module node if it somehow got here)
            # This check depends on 'node_type' attribute being reliably set.
            # If CALLS edges are strictly between functions/methods, this might be redundant.
            pass # For now, assume all nodes in fan_in/fan_out from CALLS edges are relevant.

        coupling_score = fan_in.get(node_id, 0) + fan_out.get(node_id, 0)
        if coupling_score >= threshold:
            coupled_functions[node_id] = coupling_score
            
    sorted_coupled = sorted(coupled_functions.items(), key=lambda item: item[1], reverse=True)
    logger.info(f"Found {len(sorted_coupled)} highly coupled functions.")
    return sorted_coupled


def get_call_metrics_for_function(store: GraphStore, function_fqn: str) -> Optional[Dict[str, Any]]:
    """
    Calculates call metrics (fan-in, fan-out) for a specific function/method.

    Args:
        store: The GraphStore containing the call graph data.
        function_fqn: The fully qualified name of the function/method.

    Returns:
        A dictionary with 'fan_in' and 'fan_out' keys, or None if the function is not found
        or has no call relationships.
    """
    if not store.has_node(function_fqn):
        logger.warning(f"Function {function_fqn} not found in graph. Cannot get call metrics.")
        return None
        
    # Check if it's actually a function/method node
    node_data = store.get_node(function_fqn)
    if node_data and node_data.get('node_type') not in ['function', 'method']:
        logger.warning(f"Node {function_fqn} is not a function/method. Cannot get call metrics.")
        return None

    logger.debug(f"Calculating call metrics for function: {function_fqn}")
    
    fan_in_count = 0
    # Get incoming CALLS edges
    for _s, _t, _d in store.get_edges(target=function_fqn, edge_type=REL_TYPE_CALLS):
        fan_in_count += 1
        
    fan_out_count = 0
    # Get outgoing CALLS edges
    for _s, _t, _d in store.get_edges(source=function_fqn, edge_type=REL_TYPE_CALLS):
        fan_out_count += 1
        
    metrics = {
        "fan_in": fan_in_count,
        "fan_out": fan_out_count,
        # Potentially add other metrics like "cyclomatic_complexity" if available
        # or "is_recursive" if self-loops of type CALLS are tracked distinctly.
    }
    logger.info(f"Call metrics for {function_fqn}: {metrics}")
    return metrics




# Add other analysis functions (e.g., build_call_tree, find_isolated_functions) adapting them similarly to take GraphStore/GraphTraversal as arguments.
