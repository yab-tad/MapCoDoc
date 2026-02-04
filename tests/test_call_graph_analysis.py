import pytest
from unittest.mock import MagicMock, patch
from typing import Dict, List, Set, Tuple, Any

from code_analysis.graph.store import GraphStore
from code_analysis.graph.traversal import GraphTraversal
from code_analysis.graph.call_graph import CallGraphTracker
from code_analysis.modules import call_graph_analysis
from code_analysis.relationship_types import REL_TYPE_CALLS

# --- Fixtures ---

@pytest.fixture
def graph_store_instance() -> GraphStore:
    """Provides an empty GraphStore."""
    return GraphStore()

@pytest.fixture
def call_tracker(graph_store: GraphStore) -> CallGraphTracker:
    """Provides a CallGraphTracker instance linked to the graph_store."""
    return CallGraphTracker(store=graph_store)


@pytest.fixture
def populated_analysis_objects(graph_store_instance: GraphStore) -> Any: # Returns an object with store and traversal
    """
    Provides a populated GraphStore and a GraphTraversal instance for it.
    Wraps them in a simple object for convenience, similar to the original MockTracker.
    """
    store = graph_store_instance # Use the fresh graph_store_instance
    
    nodes_with_types = {
        "entry1": {"node_type": "function"}, "mod.main": {"node_type": "function"},
        "mod.func_a": {"node_type": "function"}, "mod.func_b": {"node_type": "function"},
        "mod.func_c": {"node_type": "function"}, "mod.func_d": {"node_type": "function"},
        "mod.func_e": {"node_type": "function"}, "mod.func_f": {"node_type": "function"},
        "terminal1": {"node_type": "function"}, "mod.isolated": {"node_type": "function"}
    }
    for node_id, attrs in nodes_with_types.items():
        store.add_node(node_id, **attrs)

    call_edges = [
        ("entry1", "mod.func_a"), ("mod.func_a", "mod.func_b"), ("mod.func_a", "mod.func_c"),
        ("mod.func_b", "mod.func_d"), ("mod.func_b", "mod.func_b"), ("mod.func_c", "mod.func_d"),
        ("mod.func_d", "terminal1"), ("mod.func_e", "mod.func_f"), ("mod.func_f", "mod.func_e")
        # Assuming mod.main and mod.isolated have no calls for this basic setup
    ]
    for src, tgt in call_edges:
        store.add_edge(src, tgt, edge_type=REL_TYPE_CALLS)

    traversal = GraphTraversal(store)
    
    class AnalysisFixtureHelper:
        def __init__(self, store, traversal):
            self.store = store
            self.traversal = traversal
            # self.graph = store.graph # Less likely needed by analysis functions directly

    return AnalysisFixtureHelper(store, traversal)


@pytest.fixture
def populated_tracker() -> CallGraphTracker:
    store = GraphStore()
    # Populate store with nodes and CALLS edges based on your test scenarios
    # Nodes: 'entry1', 'mod.main', 'mod.func_a', 'mod.func_b', 'mod.func_c', 'mod.func_d',
    #        'mod.func_e', 'mod.func_f', 'terminal1', 'mod.isolated'
    # Edges (CALLS):
    # entry1 -> mod.func_a
    # mod.func_a -> mod.func_b
    # mod.func_a -> mod.func_c
    # mod.func_b -> mod.func_d
    # mod.func_b -> mod.func_b (self-loop/recursion)
    # mod.func_c -> mod.func_d
    # mod.func_d -> terminal1
    # mod.func_e -> mod.func_f
    # mod.func_f -> mod.func_e (cycle)
    # (mod.main and mod.isolated are also present but might not have calls in this basic setup)
    
    # Example nodes
    nodes_with_types = {
        "entry1": {"node_type": "function"}, 
        "mod.main": {"node_type": "function"},
        "mod.func_a": {"node_type": "function"}, 
        "mod.func_b": {"node_type": "function"},
        "mod.func_c": {"node_type": "function"}, 
        "mod.func_d": {"node_type": "function"},
        "mod.func_e": {"node_type": "function"}, 
        "mod.func_f": {"node_type": "function"},
        "terminal1": {"node_type": "function"}, 
        "mod.isolated": {"node_type": "function"}
    }
    for node_id, attrs in nodes_with_types.items():
        store.add_node(node_id, **attrs)

    # Example edges
    call_edges = [
        ("entry1", "mod.func_a"), ("mod.func_a", "mod.func_b"), ("mod.func_a", "mod.func_c"),
        ("mod.func_b", "mod.func_d"), ("mod.func_b", "mod.func_b"), ("mod.func_c", "mod.func_d"),
        ("mod.func_d", "terminal1"), ("mod.func_e", "mod.func_f"), ("mod.func_f", "mod.func_e")
    ]
    for src, tgt in call_edges:
        store.add_edge(src, tgt, edge_type=REL_TYPE_CALLS)

    traversal = GraphTraversal(store)
    # The CallGraphTracker in the tests seems to be directly used,
    # but analysis functions take store/traversal.
    # We'll pass store and traversal from a tracker instance or directly.
    # For now, let's assume the fixture provides a tracker that has these.
    class MockTracker: # Simple mock if CallGraphTracker isn't directly providing store/traversal easily
        def __init__(self, store, traversal):
            self.store = store
            self.traversal = traversal
            self.graph = store.graph # if CallGraphTracker exposes .graph

    # tracker = CallGraphTracker(store, traversal) # If CallGraphTracker can be initialized like this
    # For the purpose of these tests, let's assume populated_tracker has .store and .traversal
    return MockTracker(store, traversal) # Replace with your actual CallGraphTracker setup


# --- Test Functions ---

def test_find_call_cycles(populated_analysis_objects: Any):
    """Test finding call cycles."""
    # Pass store and traversal from the fixture
    cycles = call_graph_analysis.find_call_cycles(
        populated_analysis_objects.store, 
        populated_analysis_objects.traversal
    )
    
    found_b_cycle = any(set(cycle) == {"mod.func_b"} for cycle in cycles) # nx.simple_cycles won't include end node for self-loop like [b,b]
                                                                        # but GraphTraversal.find_cycles was modified to add [node,node] for self-loops
    found_b_self_loop_format = any(cycle == ["mod.func_b", "mod.func_b"] for cycle in cycles)


    found_ef_cycle = any(set(cycle) == {"mod.func_e", "mod.func_f"} for cycle in cycles)
    
    # Assert based on how find_cycles in GraphTraversal formats them:
    # It uses nx.simple_cycles (e.g. [e,f] for e->f->e) AND adds [node,node] for self-loops.
    assert found_b_self_loop_format, "Self-loop for mod.func_b [b,b] not found"
    assert found_ef_cycle, "Cycle involving mod.func_e and mod.func_f not found"
    
    distinct_cycle_sets = {frozenset(c if c[0]!=c[-1] else [c[0]]) for c in cycles} # Treat [b,b] as {b} for distinct count
    assert len(distinct_cycle_sets) >= 2, f"Expected at least 2 distinct cycles, found {len(distinct_cycle_sets)}"


def test_get_entry_points(populated_analysis_objects: Any):
    """Test finding entry points (nodes with no incoming calls)."""
    entry_points = call_graph_analysis.get_entry_points(populated_analysis_objects.store)
    # Based on the fixture:
    # entry1: No incoming.
    # mod.main: No incoming (by assumption, fixture needs to ensure this).
    # mod.isolated: No incoming.
    # mod.func_e: Has incoming from mod.func_f. NOT an entry point.
    expected_entry_points = {"entry1", "mod.main", "mod.isolated"}
    assert set(entry_points) == expected_entry_points


def test_get_terminal_functions(populated_analysis_objects: Any):
    """Test finding terminal functions (nodes with no outgoing calls)."""
    terminal_functions = call_graph_analysis.get_terminal_functions(populated_analysis_objects.store)
    # Based on the fixture:
    # terminal1: No outgoing.
    # mod.main: No outgoing (by assumption for this test).
    # mod.isolated: No outgoing.
    # All other functions have outgoing calls.
    expected_terminals = {"terminal1", "mod.isolated", "mod.main"}
    assert set(terminal_functions) == expected_terminals


# def test_find_entry_points(populated_tracker: CallGraphTracker):
#     """Test finding entry points (nodes with no incoming calls)."""
#     entry_points = call_graph_analysis.find_entry_points(populated_tracker)
#     expected_entry_points = {"entry1", "entry2", "entry3", "entry4", "func_g", "another_caller"}
#     assert set(entry_points) == expected_entry_points

# def test_find_terminal_functions(populated_tracker: CallGraphTracker):
#     """Test finding terminal functions (nodes with no outgoing calls)."""
#     terminal_functions = call_graph_analysis.find_terminal_functions(populated_tracker)
#     expected_terminal_functions = {"terminal1", "terminal2", "terminal3", "terminal4", "func_g"}
#     assert set(terminal_functions) == expected_terminal_functions


def test_get_call_chain(populated_analysis_objects: Any):
    """Test finding a specific call chain."""
    chain = call_graph_analysis.get_call_chain(
        populated_analysis_objects.traversal, "entry1", "terminal1"
    )
    assert chain is not None, "Call chain should not be None"
    assert len(chain) > 0, "Call chain should not be empty for existing path"
    assert chain[0] == "entry1"
    assert chain[-1] == "terminal1"
    
    expected_path1 = ["entry1", "mod.func_a", "mod.func_b", "mod.func_d", "terminal1"]
    expected_path2 = ["entry1", "mod.func_a", "mod.func_c", "mod.func_d", "terminal1"]
    assert chain == expected_path1 or chain == expected_path2, f"Unexpected chain: {chain}"

    non_existent_chain = call_graph_analysis.get_call_chain(
        populated_analysis_objects.traversal, "entry1", "mod.isolated"
    )
    assert non_existent_chain == [], "Chain to isolated node should be empty"

    self_chain = call_graph_analysis.get_call_chain(
        populated_analysis_objects.traversal, "mod.func_a", "mod.func_a"
    )
    assert self_chain == ["mod.func_a"], "Chain to self should be just the node"


def test_get_longest_call_chain(populated_analysis_objects: Any):
    """Test finding the longest call chain."""
    longest_chain = call_graph_analysis.get_longest_call_chain(
        populated_analysis_objects.traversal, populated_analysis_objects.store
    )
    assert isinstance(longest_chain, list)
    if longest_chain: 
        # Based on the fixture, longest simple paths are entry1->...->terminal1 (length 5)
        assert len(longest_chain) == 5, \
            f"Expected longest chain to be 5, got {len(longest_chain)}: {longest_chain}"
        expected_path1 = ["entry1", "mod.func_a", "mod.func_b", "mod.func_d", "terminal1"]
        expected_path2 = ["entry1", "mod.func_a", "mod.func_c", "mod.func_d", "terminal1"]
        assert longest_chain == expected_path1 or longest_chain == expected_path2, \
            f"Unexpected longest chain: {longest_chain}"
    else:
        # This should not happen if graph has paths.
        # If it does, it means no paths were found, which would be an issue with the fixture or function.
        assert False, "Longest chain was unexpectedly empty for the populated graph."


def test_find_highly_coupled_functions(populated_analysis_objects: Any):
    """Test finding highly coupled functions."""
    # The function signature is find_highly_coupled_functions(store: GraphStore, threshold: int)
    # We need to provide an integer threshold.
    default_threshold = 5 # Example threshold, adjust if function has a different default or typical use
    coupled_info = call_graph_analysis.find_highly_coupled_functions(
        populated_analysis_objects.store,
        threshold=default_threshold 
    )
    assert isinstance(coupled_info, list) 
    # Add more specific assertions here based on the expected output for the fixture
    # For example, if ("mod.func_a", "mod.func_b") is expected with score > threshold:
    # found_specific_coupling = any(
    #    (isinstance(item, tuple) and len(item) == 2 and 
    #     set(item[0] if isinstance(item[0], tuple) else [item[0]]) == {"mod.func_a", "mod.func_b"}) # if item[0] can be a tuple of nodes
    #    for item in coupled_info
    # )
    # This part needs to be adapted to the actual return structure of find_highly_coupled_functions


def test_get_call_metrics_for_function(populated_analysis_objects: Any):
    """Test calculating call metrics for a specific function."""
    try:
        metrics_a = call_graph_analysis.get_call_metrics_for_function(
            populated_analysis_objects.store,
            "mod.func_a"
        )
    except TypeError as e:
        # If it still fails, it might be (store, traversal, fqn) and the error message was misleading
        # or there's another overload. For now, proceeding with (store, fqn).
        # If this fails, the function signature in source code needs verification.
        raise e # Re-raise if the assumption is wrong

    assert isinstance(metrics_a, dict)
    assert "fan_out" in metrics_a
    assert "fan_in" in metrics_a
    # Based on fixture in test_call_graph_analysis.py:
    # entry1 -> mod.func_a
    # mod.func_a -> mod.func_b
    # mod.func_a -> mod.func_c
    assert metrics_a["fan_out"] == 2 
    assert metrics_a["fan_in"] == 1 # Only from entry1

    metrics_d = call_graph_analysis.get_call_metrics_for_function(
        populated_analysis_objects.store,
        "mod.func_d"
    )
    assert metrics_d["fan_out"] == 1 # d calls terminal1
    assert metrics_d["fan_in"] == 2 # b calls d, c calls d

