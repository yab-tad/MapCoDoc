"""
Tests the CallGraphTracker class (code_analysis/graph/call_graph.py).

Focus: Verifies the logic specific to tracking call relationships, such as adding calls, finding callers/callees, handling recursion, and calculating call depth. Uses both mocked and real graph components.
"""

import pytest
import networkx as nx
from collections import defaultdict
from unittest.mock import MagicMock, call
from typing import Generator, Tuple, Dict, List

from code_analysis.graph.store import GraphStore
from code_analysis.graph.traversal import GraphTraversal
from code_analysis.graph.call_graph import CallGraphTracker
from code_analysis.relationship_types import RELATIONSHIP_PAIRS, REL_TYPE_CALLS, REL_TYPE_CALLED_BY, REL_TYPE_IMPORTS

# --- Fixtures ---

@pytest.fixture
def mock_graph_store() -> MagicMock:
    """Provides a MagicMock for GraphStore."""
    return MagicMock(spec=GraphStore)

@pytest.fixture
def mock_graph_traversal() -> MagicMock:
    """Provides a MagicMock for GraphTraversal."""
    return MagicMock(spec=GraphTraversal)

@pytest.fixture
def call_tracker_mocked(mock_graph_store: MagicMock, mock_graph_traversal: MagicMock) -> CallGraphTracker:
    """Provides a CallGraphTracker instance with mocked dependencies."""
    # Reset mocks for each test using this fixture
    mock_graph_store.reset_mock()
    mock_graph_traversal.reset_mock()
    
    mock_graph_store.graph = MagicMock(spec=nx.MultiDiGraph) 
    
    # Simulate the _initialize_cache call during init
    
    # Store the original find_relationships method to restore it later if needed, or ensure that the RelationshipTracker's __init__ doesn't heavily rely on it for mocked stores.
    # For CallGraphTracker, _initialize_cache calls self.find_relationships, which will call self.store.get_edges.
    # So, we mock what self.store.get_edges would return.
    original_get_edges = mock_graph_store.get_edges
    mock_graph_store.get_edges.return_value = []
    
    tracker = CallGraphTracker(store=mock_graph_store, traversal=mock_graph_traversal)
    
    # Restore or reset the mock for get_edges if tests need to set their own return values.
    # For this fixture, we assume _initialize_cache should see no edges.
    # If individual tests need to mock get_edges for find_relationships, they can do so.
    mock_graph_store.get_edges = original_get_edges # Restore it if it's a MagicMock, or re-assign if it was a function
                                                 # This line might need adjustment based on how MagicMock works with spec
                                                 # A simpler way if tests will re-mock:
    
    mock_graph_store.get_edges.reset_mock() # Reset calls made during init
    # Ensure the return value is reset if tests expect to set it.
    # If get_edges was not a mock attribute on mock_graph_store before, this might error.
    # A safer way is to ensure mock_graph_store has get_edges as a mock attribute from the start
    # if spec=GraphStore doesn't automatically create it for all GraphStore methods.
   
    return tracker


@pytest.fixture
def real_graph_store() -> GraphStore:
    """Provides a real, empty GraphStore instance."""
    return GraphStore()

@pytest.fixture
def real_graph_traversal(real_graph_store: GraphStore) -> GraphTraversal:
    """Provides a real GraphTraversal instance linked to the real store."""
    return GraphTraversal(real_graph_store)

@pytest.fixture
def call_tracker_real(real_graph_store: GraphStore, real_graph_traversal: GraphTraversal) -> CallGraphTracker:
    """Provides a CallGraphTracker instance with real dependencies."""
    return CallGraphTracker(store=real_graph_store, traversal=real_graph_traversal)

@pytest.fixture
def populated_call_tracker(call_tracker_real: CallGraphTracker) -> CallGraphTracker:
    """Provides a CallGraphTracker with real dependencies and populated call data."""
    tracker = call_tracker_real
    # function_a -> function_b -> function_d (depth 2)
    # function_a -> function_c (depth 1)
    # function_b -> function_b (recursive)
    # function_e -> function_f -> function_e (cycle)
    tracker.add_call("mod.func_a", "mod.func_b")
    tracker.add_call("mod.func_a", "mod.func_c")
    tracker.add_call("mod.func_b", "mod.func_d")
    tracker.add_call("mod.func_b", "mod.func_b") # Recursive
    tracker.add_call("mod.func_e", "mod.func_f")
    tracker.add_call("mod.func_f", "mod.func_e") # Cycle
    tracker.add_call("mod.func_g", "mod.func_h") # Simple call
    return tracker


# --- Test Cases ---

def test_initialization_mocked(call_tracker_mocked: CallGraphTracker, mock_graph_store: MagicMock, mock_graph_traversal: MagicMock):
    """Test that the CallGraphTracker initializes correctly with mocks."""
    assert call_tracker_mocked is not None
    assert call_tracker_mocked.store is mock_graph_store
    assert call_tracker_mocked.traversal is mock_graph_traversal
    # Check that cache is initialized with expected structure
    assert isinstance(call_tracker_mocked._function_cache, dict)
    assert 'calls' in call_tracker_mocked._function_cache
    assert 'called_by' in call_tracker_mocked._function_cache
    assert 'call_edges' in call_tracker_mocked._function_cache
    assert isinstance(call_tracker_mocked._function_cache['calls'], defaultdict)
    assert isinstance(call_tracker_mocked._function_cache['called_by'], defaultdict)
    # Check initial state (should be empty sets/dicts after init BEFORE population)
    # _initialize_cache reads from the store which is mocked to return [] initially in fixture
    assert call_tracker_mocked._function_cache['calls'] == defaultdict(set)
    assert call_tracker_mocked._function_cache['called_by'] == defaultdict(set)
    assert call_tracker_mocked._function_cache['call_edges'] == {}


def test_initialization_real(call_tracker_real: CallGraphTracker, real_graph_store: GraphStore, real_graph_traversal: GraphTraversal):
    """Test that the CallGraphTracker initializes correctly with real components."""
    assert call_tracker_real is not None
    assert call_tracker_real.store is real_graph_store
    assert call_tracker_real.traversal is real_graph_traversal
    # Check that cache is initialized with expected structure
    assert isinstance(call_tracker_real._function_cache, dict)
    assert 'calls' in call_tracker_real._function_cache
    assert 'called_by' in call_tracker_real._function_cache
    assert 'call_edges' in call_tracker_real._function_cache
    assert isinstance(call_tracker_real._function_cache['calls'], defaultdict)
    assert isinstance(call_tracker_real._function_cache['called_by'], defaultdict)
    # Real tracker starts with empty graph, so cache should be empty sets/dicts
    assert call_tracker_real._function_cache['calls'] == defaultdict(set)
    assert call_tracker_real._function_cache['called_by'] == defaultdict(set)
    assert call_tracker_real._function_cache['call_edges'] == {}


def test_add_call_mocked(call_tracker_mocked: CallGraphTracker):
    """Test adding a call relationship using mocked store."""
    caller = "module.function_a"
    callee = "module.function_b"
    call_site = "file.py:10"
    args_count = 2
    kwargs_count = 1

    # Scenario 1: No pre-existing edge in the inverse direction
    call_tracker_mocked.store.get_edge.return_value = None 
    call_tracker_mocked.store.has_edge.return_value = False

    call_tracker_mocked.add_call(
        caller=caller,
        callee=callee,
        call_site=call_site,
        args_count=args_count,
        kwargs_count=kwargs_count
        # is_direct=True is default
    )

    assert call_tracker_mocked.store.add_edge.call_count == 2
    call_args_list_s1 = call_tracker_mocked.store.add_edge.call_args_list

    primary_call_s1_found = False
    for call_obj in call_args_list_s1:
        pos_args, kw_args = call_obj
        passed_metadata = kw_args.get('metadata', {})
        if pos_args == (caller, callee) and \
            kw_args.get('edge_type') == REL_TYPE_CALLS and \
            kw_args.get('key') == REL_TYPE_CALLS and \
            passed_metadata.get('call_site') == call_site and \
            passed_metadata.get('args_count') == args_count and \
            passed_metadata.get('kwargs_count') == kwargs_count and \
            passed_metadata.get('is_direct') is True:
            primary_call_s1_found = True
            break
    assert primary_call_s1_found, f"Scenario 1: Primary CALLS edge not correctly added. Calls: {call_args_list_s1}"

    inverse_call_s1_found = False
    for call_obj in call_args_list_s1:
        pos_args, kw_args = call_obj
        passed_metadata = kw_args.get('metadata', {})
        if pos_args == (callee, caller) and \
            kw_args.get('edge_type') == REL_TYPE_CALLED_BY and \
            kw_args.get('key') == REL_TYPE_CALLED_BY and \
            passed_metadata.get('call_site') == call_site and \
            passed_metadata.get('args_count') == args_count and \
            passed_metadata.get('kwargs_count') == kwargs_count and \
            passed_metadata.get('is_direct') is True:
            inverse_call_s1_found = True
            break
    assert inverse_call_s1_found, f"Scenario 1: Inverse CALLED_BY edge not correctly added. Calls: {call_args_list_s1}"

    # Scenario 2: Adding a call, ensuring primary and inverse are distinct and correct.
    call_tracker_mocked.store.reset_mock()
    call_tracker_mocked.store.get_edge.return_value = None 
    call_tracker_mocked.store.has_edge.return_value = False
    s2_call_site = "s2_site.py"
    call_tracker_mocked.add_call(caller, callee, call_site=s2_call_site, args_count=0, kwargs_count=0)
    
    assert call_tracker_mocked.store.add_edge.call_count == 2, \
        f"Scenario 2: Expected 2 add_edge calls. Got {call_tracker_mocked.store.add_edge.call_count}. Calls: {call_tracker_mocked.store.add_edge.call_args_list}"
    call_args_list_s2 = call_tracker_mocked.store.add_edge.call_args_list
    
    primary_s2_found = False
    for call_obj in call_args_list_s2:
        pos_args, kw_args = call_obj
        passed_metadata_s2 = kw_args.get('metadata', {})
        if pos_args == (caller, callee) and \
            kw_args.get('edge_type') == REL_TYPE_CALLS and \
            kw_args.get('key') == REL_TYPE_CALLS and \
            passed_metadata_s2.get('call_site') == s2_call_site and \
            passed_metadata_s2.get('args_count') == 0 and \
            passed_metadata_s2.get('kwargs_count') == 0 and \
            passed_metadata_s2.get('is_direct') is True:
            primary_s2_found = True
            break
    assert primary_s2_found, f"Scenario 2: Primary CALLS edge not correctly added/found. Calls: {call_args_list_s2}"
    
    inverse_s2_found = False
    for call_obj in call_args_list_s2:
        pos_args, kw_args = call_obj
        passed_metadata_s2 = kw_args.get('metadata', {})
        if pos_args == (callee, caller) and \
            kw_args.get('edge_type') == REL_TYPE_CALLED_BY and \
            kw_args.get('key') == REL_TYPE_CALLED_BY and \
            passed_metadata_s2.get('call_site') == s2_call_site and \
            passed_metadata_s2.get('args_count') == 0 and \
            passed_metadata_s2.get('kwargs_count') == 0 and \
            passed_metadata_s2.get('is_direct') is True:
            inverse_s2_found = True
            break
    assert inverse_s2_found, f"Scenario 2: Inverse CALLED_BY edge not correctly added/found. Calls: {call_args_list_s2}"

    # Scenario 3: Adding a call that results in updating an existing edge (due to same key)
    # and adding its distinct inverse.
    call_tracker_mocked.store.reset_mock()
    call_tracker_mocked.store.get_edge.return_value = None # For inverse relationship checks
    call_tracker_mocked.store.has_edge.return_value = False # Assume no specific inverse edge exists yet

    new_call_site_s3 = "new_site_s3.py:1"
    call_tracker_mocked.add_call(caller, callee, call_site=new_call_site_s3, args_count=1, kwargs_count=1)

    assert call_tracker_mocked.store.add_edge.call_count == 2, \
        f"Scenario 3: Expected 2 add_edge calls. Got {call_tracker_mocked.store.add_edge.call_count}. Calls: {call_tracker_mocked.store.add_edge.call_args_list}"

    call_args_list_s3 = call_tracker_mocked.store.add_edge.call_args_list

    primary_call_s3_updated = False
    for call_obj in call_args_list_s3:
        pos_args, kw_args = call_obj
        passed_metadata_s3 = kw_args.get('metadata', {})
        if pos_args == (caller, callee) and \
            kw_args.get('key') == REL_TYPE_CALLS and \
            kw_args.get('edge_type') == REL_TYPE_CALLS and \
            passed_metadata_s3.get('call_site') == new_call_site_s3 and \
            passed_metadata_s3.get('args_count') == 1 and \
            passed_metadata_s3.get('kwargs_count') == 1:
            primary_call_s3_updated = True
            break
    assert primary_call_s3_updated, \
        f"Scenario 3: Expected call to update A->B (CALLS) with new attributes not found or incorrect. All calls: {call_args_list_s3}"

    inverse_call_s3_added = False
    for call_obj in call_args_list_s3:
        pos_args, kw_args = call_obj
        passed_metadata_s3 = kw_args.get('metadata', {})
        if pos_args == (callee, caller) and \
            kw_args.get('key') == REL_TYPE_CALLED_BY and \
            kw_args.get('edge_type') == REL_TYPE_CALLED_BY and \
            passed_metadata_s3.get('call_site') == new_call_site_s3 and \
            passed_metadata_s3.get('args_count') == 1 and \
            passed_metadata_s3.get('kwargs_count') == 1:
            inverse_call_s3_added = True
            break
    assert inverse_call_s3_added, \
        f"Scenario 3: Expected call to add/update B->A (CALLED_BY) with new attributes not found or incorrect. All calls: {call_args_list_s3}"


def test_add_call_real(call_tracker_real: CallGraphTracker):
    """Test adding a call relationship using real store."""
    caller = "module.function_a"
    callee = "module.function_b"
    call_site = "file.py:10"

    added_key = call_tracker_real.add_call(caller=caller, callee=callee, call_site=call_site)
    # add_call in CallGraphTracker now returns a bool. The RelationshipTracker.add_relationship returns key or None.
    # For the test, we are more interested in the success status.
    # Let's assume `add_call` in `CallGraphTracker` itself returns a bool indicating overall success of adding primary and inverse.
    # The fixture `call_tracker_real.add_call` calls `self.add_relationship` from `RelationshipTracker`
    # `RelationshipTracker.add_relationship` calls `self.store.add_edge` which returns a key or None.
    # If `self.store.add_edge` returns a key, `add_relationship` returns True.
    # So `added_key` here will be the key of the primary edge if successful, or None.
    # The `CallGraphTracker.add_call` method itself returns True/False based on the success of the primary `add_relationship` call.

    assert added_key, "CallGraphTracker.add_call should return True on success of adding the primary relationship"

    # Check with GraphStore.has_edge
    # We need to know the actual key used by add_relationship for the primary edge
    # If add_call doesn't pass an explicit 'key' to add_relationship,
    # RelationshipTracker.add_relationship defaults to using relationship_type as the key.
    primary_edge_key = REL_TYPE_CALLS 
    inverse_edge_key = RELATIONSHIP_PAIRS[REL_TYPE_CALLS]

    assert call_tracker_real.store.has_edge(caller, callee, key=primary_edge_key), f"Primary CALLS edge not found with key {primary_edge_key}"
    assert call_tracker_real.store.has_edge(callee, caller, key=inverse_edge_key), f"Inverse CALLED_BY edge not found with key {inverse_edge_key}"

    # Verify attributes on the primary edge
    # GraphStore.get_edges yields (source, target, key, data_dict)
    primary_edges_generator = call_tracker_real.store.get_edges(source=caller, target=callee, edge_type=REL_TYPE_CALLS)
    primary_edges_list = list(primary_edges_generator) # Materialize generator to list

    assert len(primary_edges_list) == 1, "Should find exactly one primary CALLS edge"
    
    source_found, target_found, key_found, data_found = primary_edges_list[0]
    assert source_found == caller
    assert target_found == callee
    assert key_found == primary_edge_key # Check the key used in the graph
    assert data_found.get("type") == REL_TYPE_CALLS
    
    # Check metadata (which is nested)
    metadata = data_found.get("metadata", {})
    assert metadata.get("call_site") == call_site
    assert metadata.get("is_direct") is True # Default for add_call


def test_add_call_with_incomplete_data(call_tracker_mocked: CallGraphTracker):
    """Test adding a call with incomplete data doesn't call store."""
    # Check call_count of store.add_edge
    initial_call_count = call_tracker_mocked.store.add_edge.call_count

    call_tracker_mocked.add_call(caller=None, callee="module.function_b") # type: ignore
    call_tracker_mocked.add_call(caller="module.function_a", callee=None) # type: ignore
    
    # Assert store.add_edge call count hasn't changed
    assert call_tracker_mocked.store.add_edge.call_count == initial_call_count


def test_find_calls_mocked(call_tracker_mocked: CallGraphTracker):
    """Test finding call relationships using mocked find_relationships."""
    
    # This is the dictionary of attributes as stored on the edge in GraphStore
    # In our corrected model, 'is_direct' is top-level, and 'metadata' contains the rest.
    mock_edge_attributes_as_stored = {
        'is_direct': True, # Top-level
        'metadata': { 
            'call_site': 'file.py:10',
            'args_count': 2,
            'kwargs_count': 1,
            'is_direct': True # Also in metadata for completeness
            # 'edge_type' is not typically part of the *data* dict passed to add_edge,
            # but rather a parameter to add_edge itself, which GraphStore then puts into data['type'].
            # However, find_relationships returns it as part of the main dict.
        }
    }
    
    # This is the list of dicts that RelationshipTracker.find_relationships would return.
    # It includes top-level attributes from the edge.
    mock_results_from_find_relationships = [
        {
            'source': 'module.function_a',
            'target': 'module.function_b',
            'relationship_type': REL_TYPE_CALLS, # Semantic type
            'key': REL_TYPE_CALLS, # Example NetworkX key
            # All other attributes stored on the edge are spread here by find_relationships
            'is_direct': mock_edge_attributes_as_stored['is_direct'],
            'metadata': mock_edge_attributes_as_stored['metadata']
        }
    ]
    
    call_tracker_mocked.find_relationships = MagicMock(return_value=mock_results_from_find_relationships)

    result = call_tracker_mocked.find_calls(
        caller='module.function_a',
        callee='module.function_b',
        is_direct=True # This filter will be passed as a property to find_relationships
    )

    # Verify find_relationships was called correctly by find_calls.
    # When is_direct is True, find_calls should pass properties={'is_direct': True}
    call_tracker_mocked.find_relationships.assert_called_once_with(
        relationship_type=REL_TYPE_CALLS,
        source='module.function_a',
        target='module.function_b',
        properties={'is_direct': True} # Expect 'properties' argument here
    )

    assert len(result) == 1, f"Expected 1 call, got {len(result)}."
    call_info = result[0]
    assert call_info['caller'] == 'module.function_a'
    assert call_info['callee'] == 'module.function_b'
    
    # These details come from the 'metadata' dict within call_info
    assert call_info['call_site'] == 'file.py:10' 
    assert call_info['args_count'] == 2
    assert call_info['kwargs_count'] == 1
    
    # 'is_direct' comes from the top-level of call_info
    assert call_info['is_direct'] is True 
    
    # The 'metadata' field in call_info should be the *inner* metadata dictionary
    # that was stored on the edge.
    assert call_info['metadata'] == mock_edge_attributes_as_stored['metadata']


def test_find_calls_real(populated_call_tracker: CallGraphTracker):
    """Test finding call relationships using real store."""
    # Find the specific call added in the fixture
    results = populated_call_tracker.find_calls(caller="mod.func_a", callee="mod.func_b")
    assert len(results) == 1
    call_info = results[0]
    assert call_info['caller'] == 'mod.func_a'
    assert call_info['callee'] == 'mod.func_b'
    assert call_info['metadata'] is not None # Properties were added

    # Find all calls
    all_calls = populated_call_tracker.find_calls()
    assert len(all_calls) == 7 # Based on populated_call_tracker fixture

def test_get_outgoing_calls_mocked(call_tracker_mocked: CallGraphTracker):
    """Test getting outgoing calls using mocked get_outgoing_relationships."""
    function = "module.function_a"
    call_tracker_mocked.get_outgoing_relationships = MagicMock(return_value={
        REL_TYPE_CALLS: [
            {'source': function, 'target': 'module.function_b'},
            {'source': function, 'target': 'module.function_c'},
            {'source': function, 'target': 'module.function_b'}  # Duplicate
        ]
    })

    result = call_tracker_mocked.get_outgoing_calls(function)

    assert len(result) == 2
    assert 'module.function_b' in result
    assert 'module.function_c' in result
    call_tracker_mocked.get_outgoing_relationships.assert_called_once_with(function)


def test_get_outgoing_calls_real(populated_call_tracker: CallGraphTracker):
    """Test getting outgoing calls using real store."""
    result_a = populated_call_tracker.get_outgoing_calls("mod.func_a")
    assert set(result_a) == {"mod.func_b", "mod.func_c"}

    result_b = populated_call_tracker.get_outgoing_calls("mod.func_b")
    assert set(result_b) == {"mod.func_d", "mod.func_b"} # Includes recursive call

    result_d = populated_call_tracker.get_outgoing_calls("mod.func_d")
    assert set(result_d) == set()


def test_get_incoming_calls_mocked(call_tracker_mocked: CallGraphTracker):
    """Test getting incoming calls using mocked get_incoming_relationships."""
    function = "module.function_b"
    call_tracker_mocked.get_incoming_relationships = MagicMock(return_value={
        REL_TYPE_CALLS: [
            {'source': 'module.function_a', 'target': function},
            {'source': 'module.function_c', 'target': function},
            {'source': 'module.function_a', 'target': function}  # Duplicate
        ]
    })

    result = call_tracker_mocked.get_incoming_calls(function)

    assert len(result) == 2
    assert 'module.function_a' in result
    assert 'module.function_c' in result
    call_tracker_mocked.get_incoming_relationships.assert_called_once_with(function)


def test_get_incoming_calls_real(populated_call_tracker: CallGraphTracker):
    """Test getting incoming calls using real store."""
    result_b = populated_call_tracker.get_incoming_calls("mod.func_b")
    assert set(result_b) == {"mod.func_a", "mod.func_b"} # Includes recursive call

    result_e = populated_call_tracker.get_incoming_calls("mod.func_e")
    assert set(result_e) == {"mod.func_f"}

    result_h = populated_call_tracker.get_incoming_calls("mod.func_h")
    assert set(result_h) == {"mod.func_g"}


def test_is_function_called_mocked(call_tracker_mocked: CallGraphTracker):
    """Test checking if a function is called using mocked relationships."""
    function = "module.function_b"

    # Case 1: Function is called
    call_tracker_mocked.get_incoming_relationships = MagicMock(return_value={
        REL_TYPE_CALLS: [{'source': 'module.function_a', 'target': function}]
    })
    assert call_tracker_mocked.is_function_called(function) is True

    # Case 2: Function is not called
    call_tracker_mocked.get_incoming_relationships = MagicMock(return_value={})
    assert call_tracker_mocked.is_function_called(function) is False

    # Case 3: Empty function name
    assert call_tracker_mocked.is_function_called("") is False


def test_is_function_called_real(populated_call_tracker: CallGraphTracker):
    """Test checking if a function is called using real store."""
    assert populated_call_tracker.is_function_called("mod.func_b") is True
    assert populated_call_tracker.is_function_called("mod.func_e") is True
    assert populated_call_tracker.is_function_called("mod.func_a") is False # No incoming calls
    assert populated_call_tracker.is_function_called("mod.non_existent") is False


def test_get_call_count_mocked(call_tracker_mocked: CallGraphTracker):
    """Test getting the call count using mocked get_relationship_count."""
    expected_count = 42
    call_tracker_mocked.get_relationship_count = MagicMock(return_value=expected_count)

    result = call_tracker_mocked.get_call_count()

    assert result == expected_count
    call_tracker_mocked.get_relationship_count.assert_called_once_with(REL_TYPE_CALLS)


def test_get_call_count_real(populated_call_tracker: CallGraphTracker):
    """Test getting the call count using real store."""
    # Count the edges defined in the populated_call_tracker fixture.
    # "mod.func_a" -> "mod.func_b"
    # "mod.func_a" -> "mod.func_c"
    # "mod.func_b" -> "mod.func_d"
    # "mod.func_b" -> "mod.func_b" (Recursive)
    # "mod.func_e" -> "mod.func_f"
    # "mod.func_f" -> "mod.func_e" (Cycle)
    # "mod.func_g" -> "mod.func_h"
    # Total = 7 calls
    assert populated_call_tracker.get_call_count() == 7


def test_add_call_with_dfa_metadata(call_tracker_real: CallGraphTracker):
    """Test adding a call with DFA metadata and retrieving it."""
    tracker = call_tracker_real
    caller_fqn = "my_module.caller_func"
    callee_fqn = "my_module.callee_func"
    call_site_loc = "my_module.py:42"
    
    dfa_meta_payload = { # This is what CodeVisitor would pass in 'metadata' key
        "line": 42,
        "arguments": [
            {"position": 0, "source_type": "constant", "value": "123"},
            {"position": 1, "source_type": "variable", "name": "x"}
        ],
        "keyword_arguments": [
            {"name": "param_a", "source_type": "constant", "value": "'hello'"}
        ]
    }

    tracker.add_call(
        caller=caller_fqn,
        callee=callee_fqn,
        call_site=call_site_loc, # Explicit param
        args_count=2,            # Explicit param
        kwargs_count=1,          # Explicit param
        is_direct=True,          # Explicit param
        metadata=dfa_meta_payload # DFA and other custom data passed here
    )

    # Retrieve the call using find_calls (which should be fixed)
    found_calls = tracker.find_calls(caller=caller_fqn, callee=callee_fqn)
    assert len(found_calls) == 1
    call_info = found_calls[0]
    
    assert call_info['caller'] == caller_fqn
    assert call_info['callee'] == callee_fqn
    # These should now be correctly extracted by the fixed find_calls
    assert call_info['call_site'] == call_site_loc
    assert call_info['args_count'] == 2
    assert call_info['kwargs_count'] == 1
    assert call_info['is_direct'] is True
    
    # Check the specific DFA metadata within the returned 'metadata' field
    retrieved_inner_metadata = call_info.get('metadata', {})
    assert retrieved_inner_metadata.get("line") == 42 # From dfa_meta_payload
    assert retrieved_inner_metadata.get("arguments") == dfa_meta_payload["arguments"]
    assert retrieved_inner_metadata.get("keyword_arguments") == dfa_meta_payload["keyword_arguments"]
    
    # Verify that explicit params also ended up in the stored metadata
    assert retrieved_inner_metadata.get("call_site") == call_site_loc
    assert retrieved_inner_metadata.get("args_count") == 2
    assert retrieved_inner_metadata.get("kwargs_count") == 1
    assert retrieved_inner_metadata.get("is_direct") is True

    # Retrieve using get_edge_data (which relies on RelationshipTracker.get_relationship_properties)
    # The get_relationship_properties should return the entire edge data dict stored by add_relationship.
    # This data dict will have 'metadata' as one of its keys, containing final_metadata.
    edge_attributes = tracker.get_edge_data(caller_fqn, callee_fqn) 
    assert edge_attributes is not None
    assert edge_attributes.get("type") == REL_TYPE_CALLS # Stored by RelationshipTracker
    
    # The metadata CallGraphTracker constructed should be under the 'metadata' key of the edge_attributes
    stored_call_metadata = edge_attributes.get("metadata", {})
    assert stored_call_metadata.get("line") == 42
    assert stored_call_metadata.get("arguments") == dfa_meta_payload["arguments"]
    assert stored_call_metadata.get("keyword_arguments") == dfa_meta_payload["keyword_arguments"]
    assert stored_call_metadata.get("call_site") == call_site_loc
    assert stored_call_metadata.get("args_count") == 2
    assert stored_call_metadata.get("kwargs_count") == 1
    assert stored_call_metadata.get("is_direct") is True


def test_get_edge_data_mocked(call_tracker_mocked: CallGraphTracker, mock_graph_store: MagicMock):
    """Test get_edge_data using mocked store.get_edges, which it uses internally."""
    caller = "mod.caller"
    callee = "mod.callee"
    mock_key = "mock_edge_key_123"
    # This is the expected structure of the *attributes dictionary* of the edge
    expected_edge_attributes_data = { # This is the 'data' part of (u,v,k,data)
        "type": REL_TYPE_CALLS, 
        "metadata": {"call_site": "test.py:1", "is_direct": True, "custom": "value"}
    }

    # CallGraphTracker.get_edge_data uses self.store.get_edges
    # self.store.get_edges (real one) yields tuples of (source, target, key, data_dict)
    # So, the mock should return an iterator yielding such tuples.
    mock_graph_store.get_edges.return_value = iter([(
        caller, 
        callee, 
        mock_key, # The graph edge key
        expected_edge_attributes_data # The edge's data dictionary
    )])

    retrieved_attributes = call_tracker_mocked.get_edge_data(caller, callee)

    # get_edge_data should return the data_dict part of the first yielded edge
    assert retrieved_attributes == expected_edge_attributes_data, \
        f"Retrieved attributes {retrieved_attributes} did not match expected {expected_edge_attributes_data}"
    
    # Verify that get_edges was called correctly by get_edge_data
    mock_graph_store.get_edges.assert_called_once_with(
        source=caller, 
        target=callee, 
        edge_type=REL_TYPE_CALLS
    )


def test_get_edge_data_real(call_tracker_real: CallGraphTracker):
    """Test get_edge_data with real store, verifying metadata merging."""
    tracker = call_tracker_real
    caller = "real.caller"
    callee = "real.callee"

    # Metadata that might be passed from CodeVisitor (e.g., DFA info)
    visitor_provided_metadata = {"info": "test_data", "line": 10}

    # Explicit parameters to add_call
    call_site_param = "real.py:5"
    is_direct_param = False # Test overriding the default
    args_count_param = 2
    kwargs_count_param = 1


    tracker.add_call(
        caller,
        callee,
        call_site=call_site_param,
        args_count=args_count_param,
        kwargs_count=kwargs_count_param,
        is_direct=is_direct_param,
        metadata=visitor_provided_metadata.copy() # Pass original metadata from "visitor"
    )

    edge_attributes = tracker.get_edge_data(caller, callee) # This should get the full edge data dict
    assert edge_attributes is not None, "Edge data should be found"

    # get_edge_data is supposed to return the *entire* attributes dictionary of the edge
    # In CallGraphTracker.add_call, we construct a 'metadata' sub-dictionary.
    # So, edge_attributes will contain {'type': REL_TYPE_CALLS, 'metadata': {...}, ...}

    assert edge_attributes.get("type") == REL_TYPE_CALLS
    
    # The call-specific details are inside the 'metadata' sub-dictionary
    call_metadata = edge_attributes.get("metadata")
    assert call_metadata is not None, "Call metadata dictionary should exist on the edge"
    
    # Verify explicit parameters are correctly set in metadata
    assert call_metadata.get("call_site") == call_site_param
    assert call_metadata.get("is_direct") == is_direct_param
    assert call_metadata.get("args_count") == args_count_param
    assert call_metadata.get("kwargs_count") == kwargs_count_param
    
    # Verify that the original visitor_provided_metadata is also present
    assert call_metadata.get("info") == "test_data"
    assert call_metadata.get("line") == 10


def test_remove_calls_by_module(call_tracker_real: CallGraphTracker):
    """Test removing calls related to a specific module."""
    tracker = call_tracker_real
    
    # Calls within module1
    tracker.add_call("module1.funcA", "module1.funcB", call_site="m1.py:1")
    tracker.add_call("module1.funcB", "module1.funcC", call_site="m1.py:2")
    
    # Calls involving module1 and module2
    tracker.add_call("module2.funcX", "module1.funcA", call_site="m2.py:1") # Incoming to module1
    tracker.add_call("module1.funcC", "module2.funcY", call_site="m1.py:3") # Outgoing from module1
    
    # Call within module2 (should remain)
    tracker.add_call("module2.funcY", "module2.funcZ", call_site="m2.py:2")
    
    assert tracker.get_call_count() == 5
    
    removed_count = tracker.remove_calls_by_module("module1")
    # Expected removals:
    # module1.funcA -> module1.funcB
    # module1.funcB -> module1.funcC
    # module2.funcX -> module1.funcA (target in module1)
    # module1.funcC -> module2.funcY (source in module1)
    # The current implementation of remove_calls_by_module iterates all CALLS edges
    # and removes if either source or target is in the module.
    # Each removal also removes the inverse CALLED_BY.
    # So, 4 primary CALLS edges are removed.
    assert removed_count == 4 
    
    assert tracker.get_call_count() == 1 # Only module2.funcY -> module2.funcZ should remain
    
    remaining_calls = tracker.find_calls()
    assert len(remaining_calls) == 1
    assert remaining_calls[0]['caller'] == "module2.funcY"
    assert remaining_calls[0]['callee'] == "module2.funcZ"

    # Test removing from a module with no calls
    assert tracker.remove_calls_by_module("module3") == 0


def test_add_call_is_direct_param(call_tracker_real: CallGraphTracker):
    """Test the is_direct parameter in add_call and its retrieval via find_calls."""
    tracker = call_tracker_real
    caller = "direct.test.caller"
    callee_direct = "direct.test.callee_direct"
    callee_indirect = "direct.test.callee_indirect"

    tracker.add_call(caller, callee_direct, is_direct=True)
    tracker.add_call(caller, callee_indirect, is_direct=False)

    # find_calls should return 'is_direct' at the top level of the call_info dict
    direct_call_info_list = tracker.find_calls(caller=caller, callee=callee_direct)
    assert len(direct_call_info_list) == 1
    assert direct_call_info_list[0]['is_direct'] is True # Check top-level key

    indirect_call_info_list = tracker.find_calls(caller=caller, callee=callee_indirect)
    assert len(indirect_call_info_list) == 1
    assert indirect_call_info_list[0]['is_direct'] is False # Check top-level key

    # Also verify it's in the 'metadata' sub-dictionary
    assert direct_call_info_list[0]['metadata'].get('is_direct') is True
    assert indirect_call_info_list[0]['metadata'].get('is_direct') is False


def test_get_call_path_mocked(call_tracker_mocked: CallGraphTracker, mock_graph_traversal: MagicMock):
    """Test finding the call path using mocked traversal."""
    caller = "module.function_a"
    callee = "module.function_c"
    expected_path = [caller, "module.function_b", callee]
    
    # Ensure the traversal attribute on the instance is the mock we want to control
    call_tracker_mocked._traversal = mock_graph_traversal  # Assign to internal attribute
    mock_graph_traversal.find_shortest_path.return_value = expected_path

    result = call_tracker_mocked.get_call_path(caller, callee)

    assert result == expected_path
    # Verify find_shortest_path was called correctly (check args if needed)
    mock_graph_traversal.find_shortest_path.assert_called_once()
    call_args, call_kwargs = mock_graph_traversal.find_shortest_path.call_args
    assert call_args[0] == caller
    assert call_args[1] == callee
    assert call_kwargs.get('edge_type') == REL_TYPE_CALLS

    # Edge case: missing traversal
    original_traversal_attr = call_tracker_mocked._traversal
    try:
        call_tracker_mocked._traversal = None
        # Assert expected behavior when traversal is None (e.g., returns empty list)
        assert call_tracker_mocked.get_call_path(caller, callee) == []
    finally:
        call_tracker_mocked._traversal = original_traversal_attr # Restore traversal

    # Edge case: incomplete data
    assert call_tracker_mocked.get_call_path("", callee) == []
    assert call_tracker_mocked.get_call_path(caller, "") == []


def test_get_call_path_real(populated_call_tracker: CallGraphTracker):
    """Test finding the call path using real traversal."""
    path = populated_call_tracker.get_call_path("mod.func_a", "mod.func_d")
    # Path options:
    # 1. mod.func_a -> mod.func_b -> mod.func_d
    # 2. mod.func_a -> mod.func_c -> mod.func_d
    # find_shortest_path can return either if they have the same length.
    # We need to accept either.
    expected_path1 = ["mod.func_a", "mod.func_b", "mod.func_d"]
    expected_path2 = ["mod.func_a", "mod.func_c", "mod.func_d"]
    assert path == expected_path1 or path == expected_path2, f"Path {path} was not one of the expected shortest paths."

    # Test path involving cycle (should still find shortest simple path to self)
    path_cycle_to_self = populated_call_tracker.get_call_path("mod.func_e", "mod.func_e")
    # A standard shortest_path function, when source equals target, returns [source].
    # It does not typically traverse a cycle to return to the source unless explicitly asked.
    assert path_cycle_to_self == ["mod.func_e"], "Shortest path from a node to itself should be just the node."

    # Test path from e to f (part of a cycle)
    path_e_to_f = populated_call_tracker.get_call_path("mod.func_e", "mod.func_f")
    assert path_e_to_f == ["mod.func_e", "mod.func_f"], "Shortest path from e to f should be direct."

    # Test for a non-existent path
    path_non_existent = populated_call_tracker.get_call_path("mod.func_a", "mod.isolated_func") # Assuming isolated_func is not reachable
    assert path_non_existent == [], "Path to a non-existent or isolated node should be empty."


def test_get_recursive_functions_mocked(call_tracker_mocked: CallGraphTracker):
    """Test finding recursive functions using mocked store interaction."""
    # Mock the result of find_relationships called by get_recursive_functions
    mock_recursive_calls = [
        # Represent the B->B self-loop
        {"source": "module.function_b", "target": "module.function_b", "relationship_type": REL_TYPE_CALLS, "properties": {}},
        # Represent the D->D self-loop
        {"source": "module.function_d", "target": "module.function_d", "relationship_type": REL_TYPE_CALLS, "properties": {}},
        # Include a non-recursive call to ensure it's filtered out
        {"source": "module.function_a", "target": "module.function_b", "relationship_type": REL_TYPE_CALLS, "properties": {}},
    ]
    call_tracker_mocked.find_relationships = MagicMock(return_value=mock_recursive_calls)

    result = call_tracker_mocked.get_recursive_functions()

    # Verify find_relationships was called correctly
    call_tracker_mocked.find_relationships.assert_called_once_with(relationship_type=REL_TYPE_CALLS)
    
    # Check the result
    assert set(result) == {'module.function_b', 'module.function_d'}
    assert len(result) == 2


def test_get_recursive_functions_real(populated_call_tracker: CallGraphTracker):
    """Test finding recursive functions using real cache."""
    result = populated_call_tracker.get_recursive_functions()
    assert set(result) == {"mod.func_b"} # Only func_b calls itself directly

def test_get_call_depth_mocked(call_tracker_mocked: CallGraphTracker):
    """Test getting the maximum call depth using mocked get_outgoing_calls."""
    function = "module.function_a"

    # Mock get_outgoing_calls to create a call tree
    # function_a -> function_b -> function_d
    #            -> function_c
    def mock_get_outgoing_calls(func_name):
        if func_name == "module.function_a":
            return ["module.function_b", "module.function_c"]
        elif func_name == "module.function_b":
            return ["module.function_d"]
        else:
            return []

    call_tracker_mocked.get_outgoing_calls = mock_get_outgoing_calls

    result = call_tracker_mocked.get_call_depth(function)
    assert result == 2

    # Edge cases
    assert call_tracker_mocked.get_call_depth("") == 0

    # # No traversal available
    # original_traversal = call_tracker_mocked.traversal
    # call_tracker_mocked.traversal = None
    # assert call_tracker_mocked.get_call_depth(function) == 0
    # call_tracker_mocked.traversal = original_traversal

    # Function with no outgoing calls
    assert call_tracker_mocked.get_call_depth("module.function_d") == 0

    # Test cyclic calls don't cause infinite loop
    def mock_cyclic_calls(func):
        if func == "module.function_e": return ["module.function_f"]
        if func == "module.function_f": return ["module.function_e"]
        return []

    call_tracker_mocked.get_outgoing_calls = mock_cyclic_calls
    depth = call_tracker_mocked.get_call_depth("module.function_e")
    assert depth == 1 # Should handle the cycle

def test_get_call_depth_real(populated_call_tracker: CallGraphTracker):
    """Test getting the maximum call depth using real traversal."""
    assert populated_call_tracker.get_call_depth("mod.func_a") == 2
    assert populated_call_tracker.get_call_depth("mod.func_b") == 1

    # Debugging prints (keep them or comment out once fixed)
    print(f"DEBUG: Node e exists: {populated_call_tracker.store.has_node('mod.func_e')}")
    print(f"DEBUG: Node f exists: {populated_call_tracker.store.has_node('mod.func_f')}")
    
    edge_attributes_ef = populated_call_tracker.store.get_edge(
        source='mod.func_e', 
        target='mod.func_f',
        edge_type=REL_TYPE_CALLS # Provide the key for get_edge
    )
    if edge_attributes_ef:
        print(f"DEBUG: Edge e->f attributes from store.get_edge(key=CALLS): {edge_attributes_ef}")
        is_calls_edge = edge_attributes_ef.get('edge_type') == REL_TYPE_CALLS
        print(f"DEBUG: Edge e->f is CALLS type: {is_calls_edge}")
    else:
        print(f"DEBUG: Edge e->f with key {REL_TYPE_CALLS} not found by store.get_edge()")

    print(f"DEBUG: Outgoing for e (from get_outgoing_calls): {populated_call_tracker.get_outgoing_calls('mod.func_e')}")
    
    assert populated_call_tracker.get_call_depth("mod.func_e") == 1
    assert populated_call_tracker.get_call_depth("mod.func_g") == 1
    assert populated_call_tracker.get_call_depth("mod.func_d") == 0
    assert populated_call_tracker.get_call_depth("mod.func_c") == 0 
    # func_h has no outgoing calls in this fixture
    if populated_call_tracker.store.has_node("mod.func_h"):
        assert populated_call_tracker.get_call_depth("mod.func_h") == 0
