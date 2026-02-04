"""
Tests the core graph components: GraphStore (code_analysis/graph/store.py) and GraphTraversal (code_analysis/graph/traversal.py). Includes enhanced tests for export chain traversal.
"""

import pytest
from pathlib import Path
from typing import Dict, Any, Generator

from code_analysis.relationship_types import (
    NODE_TYPE_MODULE, NODE_TYPE_PACKAGE, NODE_TYPE_CLASS, NODE_TYPE_FUNCTION,
    REL_TYPE_IMPORTS, REL_TYPE_CONTAINS, REL_TYPE_DEFINES, REL_TYPE_DEFINED_IN, REL_TYPE_INHERITS,
    REL_TYPE_EXPORTS, REL_TYPE_NAME_ALIAS, REL_TYPE_MODULE_ALIAS, REL_TYPE_WILDCARD_IMPORT
)

from code_analysis.graph.models import ExportStep
from code_analysis.graph.store import GraphStore
from code_analysis.graph.traversal import GraphTraversal

# --- Fixtures ---

@pytest.fixture
def graph_store() -> GraphStore:
    """Provides an empty GraphStore instance."""
    return GraphStore()

@pytest.fixture
def basic_populated_graph_store(graph_store: GraphStore) -> GraphStore:
    """Provides a GraphStore populated with basic sample nodes and edges for general tests."""
    # Packages and Modules
    graph_store.add_node("pkg", node_type=NODE_TYPE_PACKAGE, file_path="pkg/__init__.py")
    graph_store.add_node("pkg.mod1", node_type=NODE_TYPE_MODULE, file_path="pkg/mod1.py")
    graph_store.add_node("pkg.mod2", node_type=NODE_TYPE_MODULE, file_path="pkg/mod2.py")
    graph_store.add_node("pkg.sub", node_type=NODE_TYPE_PACKAGE, file_path="pkg/sub/__init__.py")
    graph_store.add_node("pkg.sub.mod3", node_type=NODE_TYPE_MODULE, file_path="pkg/sub/mod3.py")
    graph_store.add_node("other_pkg", node_type=NODE_TYPE_PACKAGE, file_path="other_pkg/__init__.py")
    graph_store.add_node("other_pkg.utils", node_type=NODE_TYPE_MODULE, file_path="other_pkg/utils.py")

    # Classes and Functions
    graph_store.add_node("pkg.mod1.ClassA", node_type=NODE_TYPE_CLASS)
    graph_store.add_node("pkg.mod1.func_x", node_type=NODE_TYPE_FUNCTION)
    graph_store.add_node("pkg.mod2.ClassB", node_type=NODE_TYPE_CLASS)
    graph_store.add_node("pkg.sub.mod3.ClassC", node_type=NODE_TYPE_CLASS)
    graph_store.add_node("other_pkg.utils.Helper", node_type=NODE_TYPE_CLASS)

    # Relationships
    # Containment
    graph_store.add_edge("pkg", "pkg.mod1", edge_type=REL_TYPE_CONTAINS)
    graph_store.add_edge("pkg", "pkg.mod2", edge_type=REL_TYPE_CONTAINS)
    graph_store.add_edge("pkg", "pkg.sub", edge_type=REL_TYPE_CONTAINS)
    graph_store.add_edge("pkg.sub", "pkg.sub.mod3", edge_type=REL_TYPE_CONTAINS)
    graph_store.add_edge("other_pkg", "other_pkg.utils", edge_type=REL_TYPE_CONTAINS)

    # Definition (Simplified)
    graph_store.add_edge("pkg.mod1", "pkg.mod1.ClassA", edge_type=REL_TYPE_DEFINES)
    graph_store.add_edge("pkg.mod1", "pkg.mod1.func_x", edge_type=REL_TYPE_DEFINES)
    graph_store.add_edge("pkg.mod2", "pkg.mod2.ClassB", edge_type=REL_TYPE_DEFINES)
    graph_store.add_edge("pkg.sub.mod3", "pkg.sub.mod3.ClassC", edge_type=REL_TYPE_DEFINES)
    graph_store.add_edge("other_pkg.utils", "other_pkg.utils.Helper", edge_type=REL_TYPE_DEFINES)

    # Imports (Module level)
    graph_store.add_edge("pkg.mod1", "other_pkg.utils", edge_type=REL_TYPE_IMPORTS)
    graph_store.add_edge("pkg.mod2", "pkg.mod1", edge_type=REL_TYPE_IMPORTS)
    graph_store.add_edge("pkg.sub.mod3", "pkg.mod1", edge_type=REL_TYPE_IMPORTS)
    graph_store.add_edge("pkg.sub.mod3", "pkg.mod2", edge_type=REL_TYPE_IMPORTS)

    # Inheritance
    graph_store.add_edge("pkg.mod2.ClassB", "pkg.mod1.ClassA", edge_type=REL_TYPE_INHERITS)
    graph_store.add_edge("pkg.sub.mod3.ClassC", "pkg.mod2.ClassB", edge_type=REL_TYPE_INHERITS)

    return graph_store


@pytest.fixture
def export_chain_graph_store() -> GraphStore:
    """
    Provides a GraphStore populated with specific nodes and relationships
    to test export chain scenarios, especially re-exports and aliases.
    """
    store = GraphStore()
    # Packages
    store.add_node("mylib", node_type=NODE_TYPE_PACKAGE, file_path="mylib/__init__.py", is_api_boundary=True)
    store.add_node("mylib.subpkg", node_type=NODE_TYPE_PACKAGE, file_path="mylib/subpkg/__init__.py", is_api_boundary=False)
    store.add_node("other_lib", node_type=NODE_TYPE_PACKAGE, file_path="other_lib/__init__.py", is_api_boundary=True)

    # Modules
    store.add_node("mylib.mod_a", node_type=NODE_TYPE_MODULE, file_path="mylib/mod_a.py")
    store.add_node("mylib.mod_b", node_type=NODE_TYPE_MODULE, file_path="mylib/mod_b.py")
    store.add_node("mylib.subpkg.mod_b", node_type=NODE_TYPE_MODULE, file_path="mylib/subpkg/mod_b.py") # ADDED this module for func_b

    # Functions/Classes (Targets of chains)
    store.add_node("mylib.mod_a.func_a", node_type=NODE_TYPE_FUNCTION, file_path="mylib/mod_a.py", line_number=10, defined_in_module="mylib.mod_a")
    store.add_node("mylib.mod_b.ClassB", node_type=NODE_TYPE_CLASS, file_path="mylib/mod_b.py", line_number=20, defined_in_module="mylib.mod_b")
    store.add_node("other_lib.func_c", node_type=NODE_TYPE_FUNCTION, file_path="other_lib/func_c.py", line_number=30, defined_in_module="other_lib")
    store.add_node("mylib.subpkg.mod_b.func_b", node_type=NODE_TYPE_FUNCTION, file_path="mylib/subpkg/mod_b.py", line_number=50, defined_in_module="mylib.subpkg.mod_b") # ADDED func_b

    # --- Scenario 1: mylib.mod_a.func_a re-exported as mylib.func ---
    store.add_edge("mylib.mod_a.func_a", "mylib.mod_a", edge_type=REL_TYPE_DEFINED_IN)
    store.add_edge(
        source="mylib.mod_a", target="mylib.mod_a.func_a", edge_type=REL_TYPE_EXPORTS,
        metadata={"exported_name": "func_a", "is_explicit": False, "is_reexport": False, "line_number": 10}
    )
    store.add_edge(
        source="mylib", target="mylib.mod_a.func_a", edge_type=REL_TYPE_NAME_ALIAS,
        metadata={"alias_name": "func", "original_name_in_source": "func_a", "source_module_fqn": "mylib.mod_a", "line": 5}
    )
    store.add_edge(
        source="mylib", target="mylib.mod_a", edge_type=REL_TYPE_IMPORTS,
        metadata={"line": 5, "is_internal_source": True, "is_relative": True, "level": 1, "is_wildcard_statement": False, "raw_module_specifier": ".mod_a", "raw_imported_name": "func_a", "raw_alias": "func", "imported_entity_fqn": "mylib.mod_a.func_a", "name_bound_in_importer": "func", "name_bound_points_to_fqn": "mylib.mod_a.func_a", "source_module_fqn": "mylib.mod_a", "original_name_in_source": "func_a"}
    )
    store.add_edge(
        source="mylib", target="mylib.mod_a.func_a", edge_type=REL_TYPE_EXPORTS,
        metadata={"exported_name": "func", "is_explicit": True, "is_reexport": True, "reexport_source_fqn": "mylib.mod_a.func_a", "line_number": 5}
    )

    # --- Scenario 2: mylib.mod_b.ClassB re-exported as mylib.MyClass AND explicitly in __all__ of mylib ---
    store.add_edge("mylib.mod_b.ClassB", "mylib.mod_b", edge_type=REL_TYPE_DEFINED_IN)
    store.add_edge(
        source="mylib.mod_b", target="mylib.mod_b.ClassB", edge_type=REL_TYPE_EXPORTS,
        metadata={"exported_name": "ClassB", "is_explicit": True, "is_reexport": False, "line_number": 20}
    )
    store.add_edge(
        source="mylib", target="mylib.mod_b.ClassB", edge_type=REL_TYPE_NAME_ALIAS,
        metadata={"alias_name": "MyClass", "original_name_in_source": "ClassB", "source_module_fqn": "mylib.mod_b", "line": 15}
    )
    store.add_edge(
        source="mylib", target="mylib.mod_b", edge_type=REL_TYPE_IMPORTS,
        metadata={"line": 15, "raw_module_specifier": ".mod_b", "raw_imported_name": "ClassB", "raw_alias": "MyClass", "name_bound_in_importer": "MyClass", "name_bound_points_to_fqn": "mylib.mod_b.ClassB", "imported_entity_fqn": "mylib.mod_b.ClassB", "source_module_fqn": "mylib.mod_b", "original_name_in_source": "ClassB"}
    )
    store.add_edge(
        source="mylib", target="mylib.mod_b.ClassB", edge_type=REL_TYPE_EXPORTS,
        metadata={"exported_name": "MyClass", "is_explicit": True, "is_reexport": True, "reexport_source_fqn": "mylib.mod_b.ClassB", "line_number": 20}
    )

    # --- Scenario 3: Wildcard import and re-export from mylib ---
    store.add_edge("other_lib.func_c", "other_lib", edge_type=REL_TYPE_DEFINED_IN)
    store.add_edge(
        source="other_lib", target="other_lib.func_c", edge_type=REL_TYPE_EXPORTS,
        metadata={"exported_name": "func_c", "is_explicit": True, "is_reexport": False, "line_number": 30}
    )
    store.add_edge(
        source="mylib", target="other_lib", edge_type=REL_TYPE_IMPORTS,
        metadata={"line": 25, "raw_module_specifier": "other_lib", "raw_imported_name": "*", "raw_alias": None, "is_wildcard_statement": True, "name_bound_in_importer": "*", "name_bound_points_to_fqn": "other_lib", "imported_entity_fqn": "other_lib"}
    )
    store.add_edge(
        source="mylib", target="other_lib", edge_type=REL_TYPE_WILDCARD_IMPORT,
        metadata={"line": 25, "is_internal_source": False}
    )
    store.add_edge(
        source="mylib", target="other_lib.func_c", edge_type=REL_TYPE_EXPORTS,
        metadata={"exported_name": "imported_c", "is_explicit": True, "is_reexport": True, "via_wildcard": True, "reexport_source_fqn": "other_lib.func_c"}
    )

    # --- Scenario 4: Direct export from a sub-package module, not re-exported by parent package ---
    # mylib/subpkg/mod_b.py defines func_b
    # mylib/subpkg/__init__.py exists (making subpkg a package)
    # mylib.subpkg.mod_b.func_b is available as such
    store.add_edge("mylib.subpkg.mod_b.func_b", "mylib.subpkg.mod_b", edge_type=REL_TYPE_DEFINED_IN)
    store.add_edge( # mylib.subpkg.mod_b exports func_b
        source="mylib.subpkg.mod_b", target="mylib.subpkg.mod_b.func_b", edge_type=REL_TYPE_EXPORTS,
        metadata={"exported_name": "func_b", "is_explicit": True, "is_reexport": False, "line_number": 50}
    )
    # Ensure mylib.subpkg contains mylib.subpkg.mod_b
    store.add_edge("mylib.subpkg", "mylib.subpkg.mod_b", edge_type=REL_TYPE_CONTAINS)
    # Ensure mylib contains mylib.subpkg
    store.add_edge("mylib", "mylib.subpkg", edge_type=REL_TYPE_CONTAINS)
    
    return store


@pytest.fixture
def basic_graph_traversal(basic_populated_graph_store: GraphStore) -> GraphTraversal:
    """Provides a GraphTraversal instance linked to the basic populated store."""
    return GraphTraversal(basic_populated_graph_store)

@pytest.fixture
def export_chain_graph_traversal(export_chain_graph_store: GraphStore) -> GraphTraversal:
    """Provides a GraphTraversal instance linked to the export chain store."""
    return GraphTraversal(export_chain_graph_store)


# --- Test Graph Models ---

def test_export_step():
    """Test the ExportStep data model."""
    step = ExportStep(
        module_in_chain_fqn="pkg.mod",
        name_in_module_scope="MyClass",
        target_item_fqn="pkg.mod.MyClass",
        availability_mechanism="alias",
        is_explicitly_exported_from_this_module=True,
        is_module_api_boundary=False
    )
    
    assert step.module_in_chain_fqn == "pkg.mod"
    assert step.name_in_module_scope == "MyClass"
    assert step.target_item_fqn == "pkg.mod.MyClass"
    assert step.availability_mechanism == "alias"
    assert step.is_explicitly_exported_from_this_module is True
    assert step.is_module_api_boundary is False


# --- Test GraphStore ---

def test_add_node(graph_store: GraphStore):
    """Test adding nodes to the GraphStore."""
    graph_store.add_node("node1", node_type="typeA", prop1="value1")
    graph_store.add_node("node2", node_type="typeB", prop2=123)

    assert graph_store.has_node("node1")
    assert graph_store.has_node("node2")
    assert not graph_store.has_node("node3")

    node1_data = graph_store.get_node("node1")
    assert node1_data is not None
    assert node1_data["node_type"] == "typeA"
    assert node1_data["prop1"] == "value1"

    node2_data = graph_store.get_node("node2")
    assert node2_data is not None
    assert node2_data["node_type"] == "typeB"
    assert node2_data["prop2"] == 123

def test_add_edge(graph_store: GraphStore):
    """Test adding edges to the GraphStore."""
    graph_store.add_node("src_node")
    graph_store.add_node("dst_node")
    graph_store.add_edge("src_node", "dst_node", edge_type="CONNECTS", weight=5)

    assert graph_store.has_edge("src_node", "dst_node", edge_type="CONNECTS")
    edge_data = graph_store.get_edge("src_node", "dst_node", edge_type="CONNECTS")
    assert edge_data is not None
    assert edge_data.get("weight") == 5
    assert edge_data.get("edge_type") == "CONNECTS"
    
    # Test adding another edge to ensure it's distinct if MultiDiGraph
    graph_store.add_edge("src_node", "dst_node", edge_type="RELATES_TO", strength="high")
    assert graph_store.has_edge("src_node", "dst_node", edge_type="RELATES_TO")
    edge_data_2 = graph_store.get_edge("src_node", "dst_node", edge_type="RELATES_TO")
    assert edge_data_2 is not None
    assert edge_data_2.get("strength") == "high"
    assert edge_data_2.get("edge_type") == "RELATES_TO"

    # Ensure the first edge is still there and correct
    edge_data_1_again = graph_store.get_edge("src_node", "dst_node", edge_type="CONNECTS")
    assert edge_data_1_again is not None
    assert edge_data_1_again.get("weight") == 5
    assert edge_data_1_again.get("edge_type") == "CONNECTS"

    # Test adding edge where nodes don't exist (should add nodes)
    graph_store.add_edge("new_src", "new_dst", edge_type="LINKS")
    assert graph_store.has_node("new_src")
    assert graph_store.has_node("new_dst")
    assert graph_store.has_edge("new_src", "new_dst", edge_type="LINKS")
    new_edge_data = graph_store.get_edge("new_src", "new_dst", edge_type="LINKS")
    assert new_edge_data is not None
    assert new_edge_data.get("edge_type") == "LINKS"


def test_get_in_edges(basic_populated_graph_store: GraphStore):
    """Test retrieving incoming edges."""
    # GraphStore.get_edges(target=...) returns List[Tuple[source, target, attributes_dict]]
    in_edges_mod1_tuples = list(basic_populated_graph_store.get_edges(target="pkg.mod1"))
    sources = {src for src, _, _ in in_edges_mod1_tuples}
    edge_types = {data.get("edge_type") for _, _, data in in_edges_mod1_tuples}
    assert sources == {"pkg", "pkg.mod2", "pkg.sub.mod3"} # Adjusted based on likely incoming edges
    assert REL_TYPE_CONTAINS in edge_types # pkg contains pkg.mod1
    assert REL_TYPE_IMPORTS in edge_types  # pkg.mod2 imports pkg.mod1, pkg.sub.mod3 imports pkg.mod1

    in_edges_classA_tuples = list(basic_populated_graph_store.get_edges(target="pkg.mod1.ClassA"))
    sources_classA = {src for src, _, _ in in_edges_classA_tuples}
    edge_types_classA = {data.get("edge_type") for _, _, data in in_edges_classA_tuples}
    assert sources_classA == {"pkg.mod1", "pkg.mod2.ClassB"}
    assert edge_types_classA == {REL_TYPE_DEFINES, REL_TYPE_INHERITS}


def test_get_out_edges(basic_populated_graph_store: GraphStore):
    """Test retrieving outgoing edges."""
    # GraphStore.get_edges(source=...) returns List[Tuple[source, target, attributes_dict]]
    out_edges_pkg_tuples = list(basic_populated_graph_store.get_edges(source="pkg"))
    targets = {tgt for _, tgt, _ in out_edges_pkg_tuples}
    assert targets == {"pkg.mod1", "pkg.mod2", "pkg.sub"}
    assert all(data.get("edge_type") == REL_TYPE_CONTAINS for _, _, data in out_edges_pkg_tuples)

    out_edges_mod1_tuples = list(basic_populated_graph_store.get_edges(source="pkg.mod1"))
    targets_mod1 = {tgt for _, tgt, _ in out_edges_mod1_tuples}
    edge_types_mod1 = {data.get("edge_type") for _, _, data in out_edges_mod1_tuples}
    assert targets_mod1 == {"pkg.mod1.ClassA", "pkg.mod1.func_x", "other_pkg.utils"}
    assert edge_types_mod1 == {REL_TYPE_DEFINES, REL_TYPE_IMPORTS}


def test_get_nodes_by_type(basic_populated_graph_store: GraphStore):
    """Test retrieving nodes by their type."""
    # GraphStore.get_nodes(**filters) can be used with node_type
    packages_tuples = basic_populated_graph_store.get_nodes(node_type=NODE_TYPE_PACKAGE)
    packages = {node_id for node_id, _ in packages_tuples}
    assert packages == {"pkg", "pkg.sub", "other_pkg"}

    modules_tuples = basic_populated_graph_store.get_nodes(node_type=NODE_TYPE_MODULE)
    modules = {node_id for node_id, _ in modules_tuples}
    assert modules == {"pkg.mod1", "pkg.mod2", "pkg.sub.mod3", "other_pkg.utils"}

    classes_tuples = basic_populated_graph_store.get_nodes(node_type=NODE_TYPE_CLASS)
    classes = {node_id for node_id, _ in classes_tuples}
    assert classes == {"pkg.mod1.ClassA", "pkg.mod2.ClassB", "pkg.sub.mod3.ClassC", "other_pkg.utils.Helper"}

    functions_tuples = basic_populated_graph_store.get_nodes(node_type=NODE_TYPE_FUNCTION)
    functions = {node_id for node_id, _ in functions_tuples}
    assert functions == {"pkg.mod1.func_x"}

# --- Test GraphTraversal ---

def test_find_shortest_path(basic_graph_traversal: GraphTraversal):
    """Test finding the shortest path between nodes."""
    # Path through inheritance
    path = basic_graph_traversal.find_shortest_path("pkg.sub.mod3.ClassC", "pkg.mod1.ClassA")
    assert path == ["pkg.sub.mod3.ClassC", "pkg.mod2.ClassB", "pkg.mod1.ClassA"]

    # Path through imports/containment
    path_mod3_to_utils = basic_graph_traversal.find_shortest_path("pkg.sub.mod3", "other_pkg.utils")
    assert path_mod3_to_utils == ["pkg.sub.mod3", "pkg.mod1", "other_pkg.utils"]

    # No path
    path_no = basic_graph_traversal.find_shortest_path("pkg.mod1.func_x", "other_pkg")
    assert path_no == []
    

def test_find_all_paths(basic_graph_traversal: GraphTraversal):
    """Test finding all paths between nodes."""
    # Find all paths from ClassC up the inheritance chain
    paths = list(basic_graph_traversal.find_all_paths("pkg.sub.mod3.ClassC", "pkg.mod1.ClassA", edge_type_filter=lambda u, v, data: data.get('type') == REL_TYPE_INHERITS))
    assert len(paths) >= 1
    
    expected_path_one = ["pkg.sub.mod3.ClassC", "pkg.mod2.ClassB", "pkg.mod1.ClassA"]
    assert expected_path_one in paths

    # Find paths from mod3 to mod1
    paths_mod3_mod1 = list(basic_graph_traversal.find_all_paths("pkg.sub.mod3", "pkg.mod1"))
    # Expected paths: [mod3, mod1] (direct import)
    # Check if the direct path exists among potentially others
    assert ["pkg.sub.mod3", "pkg.mod1"] in paths_mod3_mod1

# --- Test Export Chain Logic (Using Enhanced Fixture) ---

def test_find_export_chains(export_chain_graph_traversal: GraphTraversal):
    """Test finding export chains with re-exports and aliases."""
    traversal = export_chain_graph_traversal

    # 1. Test chain for func_a (defined in mod_a, re-exported by mylib as func)
    chains_func_a = traversal.find_export_chains("mylib.mod_a.func_a")
    
    # Print what find_export_chains returned, for debugging
    print(f"DEBUG_TEST: Chains for func_a found by find_export_chains: {chains_func_a}")
    for idx, ch in enumerate(chains_func_a):
        print(f"  Chain {idx}:")
        for step_idx, st_obj in enumerate(ch):
            print(f"    Step {step_idx}: {st_obj}")
    
    assert len(chains_func_a) >= 1, f"Expected at least one export chain for mylib.mod_a.func_a, found {len(chains_func_a)}. Chains: {chains_func_a}"
    # Expected chain: [ExportStep(mod_a, func_a), ExportStep(mylib, func)]
    found_expected_chain_fa = False
    for chain in chains_func_a:
        if len(chain) == 2 and chain[0].module_in_chain_fqn == "mylib.mod_a" and chain[0].name_in_module_scope == "func_a" and chain[1].module_in_chain_fqn == "mylib" and chain[1].name_in_module_scope == "func":
            
            print("DEBUG_TEST: Found candidate 2-step chain for func_a.")
            print(f"DEBUG_TEST: Step 1 attributes: is_explicit={chain[0].is_explicitly_exported_from_this_module}, is_boundary={chain[0].is_module_api_boundary}")
            print(f"DEBUG_TEST: Step 2 attributes: is_explicit={chain[1].is_explicitly_exported_from_this_module}, is_boundary={chain[1].is_module_api_boundary}")

            assert chain[1].is_module_api_boundary is True, f"Chain[1] API boundary unexpected: {chain[1].is_module_api_boundary} for chain {chain}"
            assert chain[1].is_explicitly_exported_from_this_module is True, f"Chain[1] explicit export unexpected: {chain[1].is_explicitly_exported_from_this_module} for chain {chain}"
            found_expected_chain_fa = True
            print(f"  Chain matched criteria. Breaking.")
            break
            
    assert found_expected_chain_fa, f"Expected 2-step export chain for mylib.mod_a.func_a -> mylib.func not found or attributes mismatch. Chains found: {chains_func_a}"
    
    # 2. Test chain for func_b (defined in mod_b, not re-exported by subpkg)
    chains_func_b = traversal.find_export_chains("mylib.subpkg.mod_b.func_b")
    # Expected chain: [ExportStep(mod_b, func_b)] - only defined, not exported further up
    assert len(chains_func_b) == 1
    assert len(chains_func_b[0]) == 1
    assert chains_func_b[0][0].module_in_chain_fqn == "mylib.subpkg.mod_b"
    assert chains_func_b[0][0].name_in_module_scope == "func_b"


def test_find_best_export_chain(export_chain_graph_traversal: GraphTraversal):
    """Test finding the best export chain (shortest/most direct public)."""
    traversal = export_chain_graph_traversal

    # 1. Best chain for func_a should end at mylib.func
    best_chain_fa = traversal.find_best_export_chain("mylib.mod_a.func_a")
    print(f"DEBUG_TEST: Best chain for func_a from find_best_export_chain: {best_chain_fa}")
    
    assert best_chain_fa is not None, "Best chain for func_a should not be None"
    assert len(best_chain_fa) > 0, "Best chain for func_a should not be empty"
    
    # If find_export_chains returns the 2-step chain and scoring is correct, this should pass
    assert len(best_chain_fa) == 2, f"Expected best chain for func_a to have 2 steps, got {len(best_chain_fa)}. Chain: {best_chain_fa}"
    
    assert best_chain_fa[0].module_in_chain_fqn == "mylib.mod_a"
    assert best_chain_fa[0].name_in_module_scope == "func_a"
    assert best_chain_fa[1].module_in_chain_fqn == "mylib"
    assert best_chain_fa[1].name_in_module_scope == "func"
    assert best_chain_fa[1].is_module_api_boundary is True
    assert best_chain_fa[1].is_explicitly_exported_from_this_module is True
    
    # 2. Best chain for func_b is just its definition location
    best_chain_fb = traversal.find_best_export_chain("mylib.subpkg.mod_b.func_b")
    assert len(best_chain_fb) == 1
    assert best_chain_fb[0].module_in_chain_fqn == "mylib.subpkg.mod_b" and best_chain_fb[0].name_in_module_scope == "func_b"
