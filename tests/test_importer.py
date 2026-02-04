"""
Tests for the ImportTracker class.
"""

import pytest
from typing import Generator
from unittest.mock import MagicMock, patch, call, ANY

from code_analysis.graph.models import ImportRecord
from code_analysis.graph.store import GraphStore
from code_analysis.graph.traversal import GraphTraversal
from code_analysis.graph.relationships import RelationshipTracker
from code_analysis.graph.importer import ImportTracker
from code_analysis.relationship_types import REL_TYPE_IMPORTS, REL_TYPE_NAME_ALIAS, REL_TYPE_MODULE_ALIAS, REL_TYPE_WILDCARD_IMPORT

# --- Pytest Fixtures ---

@pytest.fixture
def graph_store() -> GraphStore:
    """Fixture for a mock GraphStore."""
    return MagicMock(spec=GraphStore)

@pytest.fixture
def graph_traversal(graph_store: GraphStore) -> GraphTraversal:
    """Fixture for a mock GraphTraversal."""
    return MagicMock(spec=GraphTraversal)

@pytest.fixture
def import_tracker(graph_store: GraphStore, graph_traversal: GraphTraversal) -> Generator[ImportTracker, None, None]:
    """Fixture for an ImportTracker instance with mocked store and traversal."""
    # Patch the base class methods during instantiation or setup
    with patch.object(RelationshipTracker, 'add_relationship') as mock_add_rel, patch.object(RelationshipTracker, 'find_relationships') as mock_find_rel, patch.object(RelationshipTracker, 'get_outgoing_relationships') as mock_get_out, patch.object(RelationshipTracker, 'get_relationship_count') as mock_get_count, patch.object(RelationshipTracker, 'has_relationship') as mock_has_rel:
        
        tracker = ImportTracker(store=graph_store, traversal=graph_traversal)
        # Store mocks on the tracker instance for access in tests
        tracker._mock_add_relationship = mock_add_rel
        tracker._mock_find_relationships = mock_find_rel
        tracker._mock_get_outgoing_relationships = mock_get_out
        tracker._mock_get_relationship_count = mock_get_count
        tracker._mock_has_relationship = mock_has_rel
        yield tracker
    

# --- Test Cases ---

def test_inheritance(import_tracker: ImportTracker):
    """Test that ImportTracker inherits from RelationshipTracker."""
    assert isinstance(import_tracker, RelationshipTracker)

def test_init(import_tracker: ImportTracker, graph_store: GraphStore, graph_traversal: GraphTraversal):
    """Test initialization."""
    assert import_tracker.store is graph_store
    assert import_tracker.traversal is graph_traversal


def test_add_import_standard_with_alias(import_tracker: ImportTracker):
    """Test adding a standard 'from module import name as alias'."""
    importer_fqn = "module.importer"
    source_module_fqn = "module.source"
    imported_item_name = "func_x"
    item_alias = "fx"
    item_fqn = f"{source_module_fqn}.{imported_item_name}"

    record = ImportRecord(
        importer_module_fqn=importer_fqn,
        line_number=10,
        raw_module_specifier=source_module_fqn, # 'from module.source ...'
        raw_imported_name=imported_item_name,   # '... import func_x ...'
        raw_alias=item_alias,                   # '... as fx'
        is_relative=False, 
        level=0, 
        is_wildcard=False,
        source_module_fqn=source_module_fqn,    # Module the item is resolved from
        imported_entity_fqn=item_fqn,           # FQN of the imported item
        is_source_internal=True,                # Assume internal for test
        name_bound_in_importer=item_alias,      # 'fx' is bound in importer
        name_bound_points_to_fqn=item_fqn       # 'fx' points to 'module.source.func_x'
    )
    import_tracker.add_import(record)

    # Expected IMPORTS relationship
    expected_imports_meta = {
        "line": 10, 
        "is_internal_source": True, 
        "is_relative": False, 
        "level": 0,
        "is_wildcard_statement": False, 
        "raw_module_specifier": source_module_fqn,
        "raw_imported_name": imported_item_name, 
        "raw_alias": item_alias,
        "imported_entity_fqn": item_fqn,
        "name_bound_in_importer": item_alias,
        "name_bound_points_to_fqn": item_fqn
    }
    # Expected NAME_ALIAS relationship
    expected_name_alias_meta = {
        "alias_name": item_alias,
        "original_name_in_source": imported_item_name,
        "source_module_fqn": source_module_fqn,
        "line": 10
    }

    calls = import_tracker._mock_add_relationship.call_args_list
    assert len(calls) == 2
        
    found_imports_call = False
    found_name_alias_call = False

    for call_obj in calls:
        assert not call_obj.args, f"Expected only keyword arguments, got positional: {call_obj.args}"
        kws = call_obj.kwargs

        if kws.get('relationship_type') == REL_TYPE_IMPORTS and \
            kws.get('source') == importer_fqn and \
                kws.get('target') == source_module_fqn and \
                    kws.get('metadata') == expected_imports_meta:
            
            found_imports_call = True
        
        elif kws.get('relationship_type') == REL_TYPE_NAME_ALIAS and \
            kws.get('source') == importer_fqn and \
                kws.get('target') == item_fqn and \
                    kws.get('metadata') == expected_name_alias_meta:
            
            found_name_alias_call = True
    
    assert found_imports_call, f"Expected IMPORTS call with metadata {expected_imports_meta} not found in actual calls: {calls}"
    assert found_name_alias_call, f"Expected NAME_ALIAS call with metadata {expected_name_alias_meta} not found in actual calls: {calls}"
    

def test_add_import_module_as_alias(import_tracker: ImportTracker):
    """Test adding an 'import module.sub as sub_alias'."""
    importer_fqn = "module.importer"
    imported_module_path = "package.actual_module" # This is record.raw_module_specifier, .raw_imported_name, .source_module_fqn, .imported_entity_fqn, .name_bound_points_to_fqn
    module_alias = "pm_alias" # This is record.raw_alias, record.name_bound_in_importer

    record = ImportRecord(
        importer_module_fqn=importer_fqn, 
        line_number=5,
        raw_module_specifier=imported_module_path, 
        raw_imported_name=imported_module_path, 
        raw_alias=module_alias,
        is_relative=False, 
        level=0, 
        is_wildcard=False, 
        source_module_fqn=imported_module_path,
        imported_entity_fqn=imported_module_path, 
        is_source_internal=True,
        name_bound_in_importer=module_alias, 
        name_bound_points_to_fqn=imported_module_path
    )
    import_tracker.add_import(record)

    expected_imports_meta = {
        "line": 5, 
        "is_internal_source": True, 
        "is_relative": False, 
        "level": 0,
        "is_wildcard_statement": False, 
        "raw_module_specifier": imported_module_path,
        "raw_imported_name": imported_module_path, 
        "raw_alias": module_alias,
        "imported_entity_fqn": imported_module_path,
        "name_bound_in_importer": module_alias, 
        "name_bound_points_to_fqn": imported_module_path
    }
    expected_module_alias_meta = {
        "alias_name": module_alias, "line": 5, "original_module_fqn": imported_module_path
    }
    
    calls = import_tracker._mock_add_relationship.call_args_list
    print(f"Number of calls to add_relationship: {len(calls)}")
    
    assert len(calls) == 2
        
    found_imports_call = False
    found_name_MODULE_ALIAS_call = False

    for call_obj in calls:
        assert not call_obj.args, f"Expected only keyword arguments, got positional: {call_obj.args}"
        kws = call_obj.kwargs

        if kws.get('relationship_type') == REL_TYPE_IMPORTS and \
            kws.get('source') == importer_fqn and \
                kws.get('target') == imported_module_path and \
                    kws.get('metadata') == expected_imports_meta:
            
            found_imports_call = True
        
        elif kws.get('relationship_type') == REL_TYPE_MODULE_ALIAS and \
            kws.get('source') == importer_fqn and \
                kws.get('target') == imported_module_path and \
                    kws.get('metadata') == expected_module_alias_meta:
            
            found_name_MODULE_ALIAS_call = True
    
    assert found_imports_call, f"Expected IMPORTS call with metadata {expected_imports_meta} not found in actual calls: {calls}"
    assert found_name_MODULE_ALIAS_call, f"Expected WILDCARD_IMPORT call with metadata {expected_module_alias_meta} not found in actual calls: {calls}"
    

def test_add_import_star(import_tracker: ImportTracker):
    """Test adding a 'from module import *'."""
    importer_fqn = "module.importer"
    source_module_fqn = "module.source_for_star" # This is record.raw_module_specifier, .source_module_fqn, .imported_entity_fqn, .name_bound_points_to_fqn

    record = ImportRecord(
        importer_module_fqn=importer_fqn, 
        line_number=15,
        raw_module_specifier=source_module_fqn, 
        raw_imported_name='*', 
        raw_alias=None,
        is_relative=False, 
        level=0, 
        is_wildcard=True, 
        source_module_fqn=source_module_fqn,
        imported_entity_fqn=source_module_fqn, 
        is_source_internal=True,
        name_bound_in_importer='*', 
        name_bound_points_to_fqn=source_module_fqn
    )
    import_tracker.add_import(record)

    expected_imports_meta = {
        "line": 15, 
        "is_internal_source": True, 
        "is_relative": False, 
        "level": 0,
        "is_wildcard_statement": True, 
        "raw_module_specifier": source_module_fqn,
        "raw_imported_name": "*", 
        "raw_alias": None, 
        "imported_entity_fqn": source_module_fqn,
        "name_bound_in_importer": "*", 
        "name_bound_points_to_fqn": source_module_fqn
    }
    expected_wildcard_meta = {"line": 15, "is_internal_source": True}
    
    
    calls = import_tracker._mock_add_relationship.call_args_list
    print(f"Number of calls to add_relationship: {len(calls)}")
    
    assert len(calls) == 2
        
    found_imports_call = False
    found_name_WILDCARD_IMPORT_call = False

    for call_obj in calls:
        assert not call_obj.args, f"Expected only keyword arguments, got positional: {call_obj.args}"
        kws = call_obj.kwargs

        if kws.get('relationship_type') == REL_TYPE_IMPORTS and \
            kws.get('source') == importer_fqn and \
                kws.get('target') == source_module_fqn and \
                    kws.get('metadata') == expected_imports_meta:
            
            found_imports_call = True
        
        elif kws.get('relationship_type') == REL_TYPE_WILDCARD_IMPORT and \
            kws.get('source') == importer_fqn and \
                kws.get('target') == source_module_fqn and \
                    kws.get('metadata') == expected_wildcard_meta:
            
            found_name_WILDCARD_IMPORT_call = True
    
    assert found_imports_call, f"Expected IMPORTS call with metadata {expected_imports_meta} not found in actual calls: {calls}"
    assert found_name_WILDCARD_IMPORT_call, f"Expected WILDCARD_IMPORT call with metadata {expected_wildcard_meta} not found in actual calls: {calls}"
    

def test_add_import_missing_importer_fqn(import_tracker: ImportTracker):
    """Test adding an import with missing importer_module_fqn in record."""
    record = ImportRecord(
        importer_module_fqn=None, # Missing
        line_number=1, 
        raw_module_specifier="mod.b", 
        raw_imported_name="item", raw_alias=None,
        is_relative=False, 
        level=0, 
        is_wildcard=False, 
        source_module_fqn="mod.b",
        imported_entity_fqn="mod.b.item", 
        is_source_internal=False,
        name_bound_in_importer="item", 
        name_bound_points_to_fqn="mod.b.item"
    )
    import_tracker.add_import(record)
    import_tracker._mock_add_relationship.assert_not_called()


def test_add_import_missing_source_module_fqn(import_tracker: ImportTracker):
    """Test adding an import with missing source_module_fqn in record (skips IMPORTS edge)."""
    importer_fqn = "module.importer"
    raw_spec = "some_spec" # This isn't a resolvable FQN for source_module_fqn
    item_name = "item"
    item_alias = "ia"
    resolved_entity_fqn = f"{raw_spec}.{item_name}" # Hypothetical if some_spec was a module

    record = ImportRecord(
        importer_module_fqn=importer_fqn, 
        line_number=1,
        raw_module_specifier=raw_spec, 
        raw_imported_name=item_name, 
        raw_alias=item_alias,
        is_relative=False, 
        level=0, 
        is_wildcard=False,
        source_module_fqn=None, # Key: source_module_fqn is None
        imported_entity_fqn=resolved_entity_fqn, 
        is_source_internal=False,
        name_bound_in_importer=item_alias, 
        name_bound_points_to_fqn=resolved_entity_fqn
    )
    import_tracker.add_import(record)

    # IMPORTS edge should be skipped. Only NAME_ALIAS should be created.
    expected_alias_meta = {
        "alias_name": item_alias, 
        "original_name_in_source": item_name,
        "source_module_fqn": None, 
        "line": 1
    }
    # Use assert_called_with because we expect only ONE call in this scenario
    import_tracker._mock_add_relationship.assert_called_once_with(
        source=importer_fqn, 
        target=resolved_entity_fqn,
        relationship_type=REL_TYPE_NAME_ALIAS, 
        metadata=expected_alias_meta
    )


def test_add_import_incomplete_data(import_tracker: ImportTracker):
    """Test adding an import with missing source or target."""
    # # Missing target
    # import_tracker.add_import(importer_module="mod.a", imported_module=None, imported_name="func")
    # # Missing source
    # import_tracker.add_import(importer_module=None, imported_module="mod.b", imported_name="func")
    
    import_tracker.add_import(
        ImportRecord(
            importer_module_fqn="mod.a",
            line_number=1,
            raw_module_specifier=None,
            raw_imported_name="func",
            raw_alias=None,
            is_relative=False,
            level=0,
            is_wildcard=False,
            source_module_fqn=None,
            imported_entity_fqn=None,
            is_source_internal=False,
            name_bound_in_importer="func",
            name_bound_points_to_fqn="func",
        )
    )

    # Ensure add_relationship was NOT called
    import_tracker._mock_add_relationship.assert_not_called()


def test_find_imports(import_tracker: ImportTracker):
    """Test finding imports using various filters."""
    expected_rels = [{"source": "a", "target": "b", "relationship_type": REL_TYPE_IMPORTS, "properties": {"name": "x"}}]
    import_tracker._mock_find_relationships.return_value = expected_rels

    # Find all imports
    result_all = import_tracker.find_imports()
    import_tracker._mock_find_relationships.assert_called_with(relationship_type=REL_TYPE_IMPORTS, source=None, target=None, properties=None)
    assert result_all == expected_rels

    # Find imports from source 'a'
    result_source = import_tracker.find_imports(importer_module="a")
    import_tracker._mock_find_relationships.assert_called_with(relationship_type=REL_TYPE_IMPORTS, source="a", target=None, properties=None)
    assert result_source == expected_rels

    # Find imports to target 'b' (module imported FROM)
    result_target = import_tracker.find_imports(imported_module_from="b") # Parameter name updated if changed in method
    import_tracker._mock_find_relationships.assert_called_with(relationship_type=REL_TYPE_IMPORTS, source=None, target="b", properties=None)
    assert result_target == expected_rels

    # Filter by name_bound_in_importer
    result_name_bound = import_tracker.find_imports(name_bound_in_importer_filter="x_bound")
    import_tracker._mock_find_relationships.assert_called_with(relationship_type=REL_TYPE_IMPORTS, source=None, target=None, properties={"name_bound_in_importer": "x_bound"})
    assert result_name_bound == expected_rels
    
    # Filter by name_bound_points_to_fqn
    result_name_points = import_tracker.find_imports(name_bound_points_to_fqn_filter="target.fqn.x")
    import_tracker._mock_find_relationships.assert_called_with(relationship_type=REL_TYPE_IMPORTS, source=None, target=None, properties={"name_bound_points_to_fqn": "target.fqn.x"})
    assert result_name_points == expected_rels


def test_get_module_imports(import_tracker: ImportTracker):
    """Test getting imports for a specific module."""
    module_name = "module.a"
    mock_import_details = [
        {"target": "module.b", "properties": {"name_bound_in_importer": "f1"}},
        {"target": "module.c", "properties": {"name_bound_in_importer": "ClassA"}}
    ]
    import_tracker._mock_get_outgoing_relationships.return_value = {
        REL_TYPE_IMPORTS: mock_import_details,
        "OTHER_REL_TYPE": [{"target": "foo", "properties": {}}]
    }
    result = import_tracker.get_module_imports(module_name)
    import_tracker._mock_get_outgoing_relationships.assert_called_once_with(module_name)
    assert result == mock_import_details


def test_is_raw_name_imported(import_tracker: ImportTracker):
    """Test checking if a raw name from statement is imported by a module."""
    module_name = "module.a"
    raw_imported_name_from_statement = "func_name_in_statement"
    # Mock find_relationships to return a non-empty list
    import_tracker._mock_find_relationships.return_value = [{"source": module_name, "target": "b", "relationship_type": REL_TYPE_IMPORTS, "properties": {"raw_imported_name": raw_imported_name_from_statement}}]

    result = import_tracker.is_raw_name_imported(module_name, raw_imported_name_from_statement)

    import_tracker._mock_find_relationships.assert_called_once_with(
        source=module_name,
        relationship_type=REL_TYPE_IMPORTS,
        properties={"raw_imported_name": raw_imported_name_from_statement} # Corrected property key
    )
    assert result is True


def test_is_raw_name_imported_false(import_tracker: ImportTracker):
    """Test checking if a raw name is imported when it is not."""
    module_name = "module.a"
    raw_imported_name_from_statement = "non_existent_raw_name"
    import_tracker._mock_find_relationships.return_value = [] # Empty list

    result = import_tracker.is_raw_name_imported(module_name, raw_imported_name_from_statement)

    import_tracker._mock_find_relationships.assert_called_once_with(
        source=module_name,
        relationship_type=REL_TYPE_IMPORTS,
        properties={"raw_imported_name": raw_imported_name_from_statement} # Corrected property key
    )
    assert result is False


def test_get_import_count(import_tracker: ImportTracker):
    """Test getting the total count of import relationships."""
    expected_count = 42
    import_tracker._mock_get_relationship_count.return_value = expected_count

    result = import_tracker.get_import_count()

    import_tracker._mock_get_relationship_count.assert_called_once_with(REL_TYPE_IMPORTS)
    assert result == expected_count
