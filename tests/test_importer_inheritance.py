"""
Tests verifying that ImportTracker correctly utilizes RelationshipTracker methods.
"""

import pytest
from unittest.mock import MagicMock, call, patch, ANY
from typing import Generator, Any, Tuple, Dict

from code_analysis.graph.models import ImportRecord
from code_analysis.graph.store import GraphStore
from code_analysis.graph.traversal import GraphTraversal
from code_analysis.graph.relationships import RelationshipTracker
from code_analysis.graph.importer import ImportTracker
from code_analysis.relationship_types import REL_TYPE_IMPORTS, REL_TYPE_MODULE_ALIAS, REL_TYPE_NAME_ALIAS, REL_TYPE_WILDCARD_IMPORT

# --- Pytest Fixtures ---

@pytest.fixture
def mock_graph_store() -> MagicMock:
    """Fixture for a mock GraphStore."""
    return MagicMock(spec=GraphStore)

@pytest.fixture
def mock_graph_traversal(mock_graph_store: MagicMock) -> MagicMock:
    """Fixture for a mock GraphTraversal."""
    return MagicMock(spec=GraphTraversal)

@pytest.fixture
def import_tracker_with_mocks(mock_graph_store: MagicMock, mock_graph_traversal: MagicMock) -> Generator[Tuple[ImportTracker, Dict[str, MagicMock]], None, None]:
    """
    Fixture for an ImportTracker instance with mocked base RelationshipTracker methods.
    Yields the tracker and a dictionary of the mocks.
    """
    # Patch the base class methods during instantiation or setup
    with patch.object(RelationshipTracker, 'add_relationship') as mock_add_rel, patch.object(RelationshipTracker, 'find_relationships') as mock_find_rel, patch.object(RelationshipTracker, 'get_outgoing_relationships') as mock_get_out, patch.object(RelationshipTracker, 'get_relationship_count') as mock_get_count, patch.object(RelationshipTracker, 'has_relationship') as mock_has_rel:
        tracker = ImportTracker(store=mock_graph_store, traversal=mock_graph_traversal)
        mocks = {
            "add_relationship": mock_add_rel,
            "find_relationships": mock_find_rel,
            "get_outgoing_relationships": mock_get_out,
            "get_relationship_count": mock_get_count,
            "has_relationship": mock_has_rel
        }
        yield tracker, mocks

# --- Test Cases ---

def test_is_instance_of_relationship_tracker(import_tracker_with_mocks: Tuple[ImportTracker, Dict[str, MagicMock]]):
    """Test that ImportTracker is an instance of RelationshipTracker."""
    tracker, _ = import_tracker_with_mocks
    assert isinstance(tracker, RelationshipTracker)


def test_add_import_calls_add_relationship_correctly(import_tracker_with_mocks: Tuple[ImportTracker, Dict[str, MagicMock]]):
    """Test that add_import calls the base add_relationship method for different import types."""
    tracker, mocks = import_tracker_with_mocks
    mock_add_relationship = mocks["add_relationship"]

    importer_fqn = "module.importer"
    
    # Case 1: from module.source import item_name as item_alias
    source_module_fqn_1 = "module.source1"
    item_name_1 = "item1"
    item_alias_1 = "alias1"
    item_fqn_1 = f"{source_module_fqn_1}.{item_name_1}"
    record1 = ImportRecord(
        importer_module_fqn=importer_fqn, line_number=1, raw_module_specifier=source_module_fqn_1,
        raw_imported_name=item_name_1, raw_alias=item_alias_1, is_relative=False, level=0, is_wildcard=False,
        source_module_fqn=source_module_fqn_1, imported_entity_fqn=item_fqn_1, is_source_internal=True,
        name_bound_in_importer=item_alias_1, name_bound_points_to_fqn=item_fqn_1
    )
    tracker.add_import(record1)
    
    # Assert IMPORTS call for record1
    mock_add_relationship.assert_any_call(
        importer_fqn, source_module_fqn_1, REL_TYPE_IMPORTS, metadata=ANY
    )
    # Assert NAME_ALIAS call for record1
    mock_add_relationship.assert_any_call(
        importer_fqn, item_fqn_1, REL_TYPE_NAME_ALIAS, metadata=ANY
    )

    # Case 2: import package.module as mod_alias
    mock_add_relationship.reset_mock() # Reset for next case
    imported_module_path_2 = "package.module2"
    mod_alias_2 = "palias2"
    record2 = ImportRecord(
        importer_module_fqn=importer_fqn, line_number=2, raw_module_specifier=imported_module_path_2,
        raw_imported_name=imported_module_path_2, raw_alias=mod_alias_2, is_relative=False, level=0, is_wildcard=False,
        source_module_fqn=imported_module_path_2, imported_entity_fqn=imported_module_path_2, is_source_internal=True,
        name_bound_in_importer=mod_alias_2, name_bound_points_to_fqn=imported_module_path_2
    )
    tracker.add_import(record2)
    mock_add_relationship.assert_any_call(
        importer_fqn, imported_module_path_2, REL_TYPE_IMPORTS, metadata=ANY
    )
    mock_add_relationship.assert_any_call(
        importer_fqn, imported_module_path_2, REL_TYPE_MODULE_ALIAS, metadata=ANY
    )
    
    # Case 3: from module.star_source import *
    mock_add_relationship.reset_mock()
    star_source_3 = "module.star_source3"
    record3 = ImportRecord(
        importer_module_fqn=importer_fqn, line_number=3, raw_module_specifier=star_source_3,
        raw_imported_name='*', raw_alias=None, is_relative=False, level=0, is_wildcard=True,
        source_module_fqn=star_source_3, imported_entity_fqn=star_source_3, is_source_internal=True,
        name_bound_in_importer='*', name_bound_points_to_fqn=star_source_3
    )
    tracker.add_import(record3)
    mock_add_relationship.assert_any_call(
        importer_fqn, star_source_3, REL_TYPE_IMPORTS, metadata=ANY
    )
    mock_add_relationship.assert_any_call(
        importer_fqn, star_source_3, REL_TYPE_WILDCARD_IMPORT, metadata=ANY
    )


def test_add_import_calls_add_relationship(import_tracker_with_mocks: Tuple[ImportTracker, Dict[str, MagicMock]]):
    """Test that add_import calls the base add_relationship method."""
    tracker, mocks = import_tracker_with_mocks
    mock_add_relationship = mocks["add_relationship"]

    source = "module.a"
    target = "module.b"
    
    # name = "func"
    # alias = "f"
    # metadata = {'line': 10}
    # expected_properties = {
    #     "imported_name": name,
    #     "is_star": False,
    #     "alias": alias,
    #     **metadata
    # }

    # tracker.add_import(
    #     importer_module=source,
    #     imported_module=target,
    #     imported_name=name,
    #     alias=alias,
    #     is_star=False,
    #     metadata=metadata
    # )

    # mock_add_relationship.assert_called_once_with(
    #     source, target, REL_TYPE_IMPORTS, properties=expected_properties
    # )
    
    record = ImportRecord(
        importer_module_fqn=source,
        line_number=10,
        raw_module_specifier=target,
        raw_imported_name="func",
        raw_alias="f",
        is_relative=False,
        level=0,
        is_wildcard=False,
        source_module_fqn=target,
        imported_entity_fqn=f"{target}.'func'",
        is_source_internal=True,
        name_bound_in_importer="f",
        name_bound_points_to_fqn=f"{target}.'func'",
    )
    
    tracker.add_import(record)
    mock_add_relationship.assert_called_once_with(source, target, REL_TYPE_IMPORTS, metadata=ANY)
    

def test_find_imports_calls_find_relationships(import_tracker_with_mocks: Tuple[ImportTracker, Dict[str, MagicMock]]):
    """Test that find_imports calls the base find_relationships method."""
    tracker, mocks = import_tracker_with_mocks
    mock_find_relationships = mocks["find_relationships"]
    expected_result = [{"source": "a", "target": "b", "type": REL_TYPE_IMPORTS}] # Simplified
    mock_find_relationships.return_value = expected_result

    # Test without filters - primary IMPORTS edge
    result = tracker.find_imports()
    # For find_imports, the RelationshipTracker.find_relationships is called with 'relationship_type'
    mock_find_relationships.assert_called_with(relationship_type=REL_TYPE_IMPORTS, source=None, target=None, properties=None)
    assert result == expected_result

    # Test with importer_module filter
    result = tracker.find_imports(importer_module="a")
    mock_find_relationships.assert_called_with(relationship_type=REL_TYPE_IMPORTS, source="a", target=None, properties=None)
    assert result == expected_result

    # Test with imported_module_from filter
    result = tracker.find_imports(imported_module_from="b")
    mock_find_relationships.assert_called_with(relationship_type=REL_TYPE_IMPORTS, source=None, target="b", properties=None)
    assert result == expected_result

    # Test with name_bound_in_importer_filter
    result = tracker.find_imports(name_bound_in_importer_filter="x_bound")
    mock_find_relationships.assert_called_with(relationship_type=REL_TYPE_IMPORTS, source=None, target=None, properties={"name_bound_in_importer": "x_bound"})
    assert result == expected_result


def test_get_module_imports_calls_get_outgoing_relationships(import_tracker_with_mocks: Tuple[ImportTracker, Dict[str, MagicMock]]):
    """Test that get_module_imports calls the base get_outgoing_relationships method."""
    tracker, mocks = import_tracker_with_mocks
    mock_get_outgoing = mocks["get_outgoing_relationships"]
    module_name = "module.a"
    # get_module_imports filters the result of get_outgoing_relationships
    expected_mock_return = {
        REL_TYPE_IMPORTS: [{"source": module_name, "target": "b", "type": REL_TYPE_IMPORTS}],
        "OTHER_TYPE": [{"source": module_name, "target": "c"}]
    }
    mock_get_outgoing.return_value = expected_mock_return
    result = tracker.get_module_imports(module_name) # This method extracts the REL_TYPE_IMPORTS list
    mock_get_outgoing.assert_called_once_with(module_name) # get_outgoing_relationships doesn't take rel_type
    assert result == expected_mock_return[REL_TYPE_IMPORTS]


def test_is_raw_name_imported_calls_find_relationships(import_tracker_with_mocks: Tuple[ImportTracker, Dict[str, MagicMock]]):
    tracker, mocks = import_tracker_with_mocks
    mock_find_relationships = mocks["find_relationships"]
    module_name = "module.a"
    raw_name = "func_in_statement"

    # Test case where name is imported
    mock_find_relationships.return_value = [{"source": module_name, "target": "b", "type": REL_TYPE_IMPORTS}]
    result_true = tracker.is_raw_name_imported(module_name, raw_name)
    mock_find_relationships.assert_called_with(
        source=module_name,
        relationship_type=REL_TYPE_IMPORTS, # Corrected: rel_type -> relationship_type
        properties={"raw_imported_name": raw_name} # is_raw_name_imported uses this specific property
    )
    assert result_true is True

    # Test case where name is not imported
    mock_find_relationships.return_value = []
    result_false = tracker.is_raw_name_imported(module_name, "other_raw_name")
    mock_find_relationships.assert_called_with(
        source=module_name,
        relationship_type=REL_TYPE_IMPORTS,
        properties={"raw_imported_name": "other_raw_name"}
    )
    assert result_false is False


def test_is_name_imported_calls_find_relationships(import_tracker_with_mocks: Tuple[ImportTracker, Dict[str, MagicMock]]):
    """Test that is_name_imported calls the base find_relationships method."""
    tracker, mocks = import_tracker_with_mocks
    mock_find_relationships = mocks["find_relationships"]
    module_name = "module.a"
    imported_name = "func"

    # Test case where name is imported
    mock_find_relationships.return_value = [{"source": module_name, "target": "b", "type": REL_TYPE_IMPORTS}]
    result_true = tracker.is_raw_name_imported(module_name, imported_name)
    mock_find_relationships.assert_called_with(
        source=module_name,
        relationship_type=REL_TYPE_IMPORTS,
        properties={"imported_name": imported_name}
    )
    assert result_true is True

    # Test case where name is not imported
    mock_find_relationships.return_value = []
    result_false = tracker.is_raw_name_imported(module_name, "other_func")
    mock_find_relationships.assert_called_with(
        source=module_name,
        relationship_type=REL_TYPE_IMPORTS,
        properties={"imported_name": "other_func"}
    )
    assert result_false is False

def test_has_import_calls_has_relationship(import_tracker_with_mocks: Tuple[ImportTracker, Dict[str, MagicMock]]):
    """Test that has_import calls the base has_relationship method."""
    tracker, mocks = import_tracker_with_mocks
    mock_has_relationship = mocks["has_relationship"]
    source = "module.a"
    target = "module.b"

    # Test case where relationship exists
    mock_has_relationship.return_value = True
    result_true = tracker.has_import(source, target)
    mock_has_relationship.assert_called_with(source, target, REL_TYPE_IMPORTS)
    assert result_true is True

    # Test case where relationship does not exist
    mock_has_relationship.return_value = False
    result_false = tracker.has_import(source, "module.c")
    mock_has_relationship.assert_called_with(source, "module.c", REL_TYPE_IMPORTS)
    assert result_false is False

def test_get_import_count_calls_get_relationship_count(import_tracker_with_mocks: Tuple[ImportTracker, Dict[str, MagicMock]]):
    """Test that get_import_count calls the base get_relationship_count method."""
    tracker, mocks = import_tracker_with_mocks
    mock_get_count = mocks["get_relationship_count"]
    expected_count = 15
    mock_get_count.return_value = expected_count

    result = tracker.get_import_count()

    mock_get_count.assert_called_once_with(REL_TYPE_IMPORTS)
    assert result == expected_count
