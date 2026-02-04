"""
This test module acts as a unit test specifically for the InheritanceTracker class itself. It tests the tracker's internal logic, query capabilities, and caching mechanisms in isolation.

Methodology:
- Directly instantiates GraphStore, GraphTraversal, and InheritanceTracker.
- Manually populates the GraphStore with predefined nodes and REL_TYPE_INHERITS/REL_TYPE_IMPLEMENTS edges to create specific test scenarios (simple, multiple, diamond inheritance, interfaces).
- Calls various methods directly on the InheritanceTracker instance and asserts their behavior based on the manually constructed graph.
- Covers a wider range of InheritanceTracker methods, including those related to hierarchy building (_build_superclass_hierarchy), metrics (calculate_inheritance_metrics), complex pattern detection (find_diamond_inheritance), and cache management (clear_caches).
- Uses unittest.TestCase and mocks where necessary to isolate the tracker's logic.
"""

import os
import sys
import unittest
from unittest.mock import Mock, patch


# Add the parent directory to the path
# Ensure this path adjustment is correct for your test runner environment
# It might be better to configure PYTHONPATH or use relative imports if possible
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from code_analysis.graph.inheritance_tracker import InheritanceTracker
from code_analysis.relationship_types import (
    REL_TYPE_INHERITS,
    REL_TYPE_IMPLEMENTS
)
from code_analysis.graph.store import GraphStore
from code_analysis.graph.traversal import GraphTraversal


class TestInheritanceTracker(unittest.TestCase):
    """Unit tests for the InheritanceTracker class logic."""

    def setUp(self):
        """Set up a graph store and tracker instance for each test."""
        # Use real GraphStore and GraphTraversal for testing tracker logic
        self.store = GraphStore()
        self.traversal = GraphTraversal(self.store)
        self.tracker = InheritanceTracker(self.store, self.traversal)

        # --- Populate graph directly for unit testing tracker methods ---
        # Nodes (FQNs)
        self.store.add_node("pkg.A", node_type="class")
        self.store.add_node("pkg.B", node_type="class")
        self.store.add_node("pkg.C", node_type="class")
        self.store.add_node("pkg.D", node_type="class")
        self.store.add_node("pkg.E", node_type="class")
        self.store.add_node("pkg.Mixin1", node_type="class")
        self.store.add_node("pkg.Mixin2", node_type="class")
        self.store.add_node("pkg.Interface1", node_type="interface") # Assuming interface type
        self.store.add_node("pkg.Interface2", node_type="interface")
        self.store.add_node("pkg.X", node_type="class") # For diamond
        self.store.add_node("pkg.Y", node_type="class") # For diamond
        self.store.add_node("pkg.Z", node_type="class") # For diamond

        # Edges (Inheritance & Implementation)
        # B inherits A, C inherits A
        self.store.add_edge("pkg.B", "pkg.A", edge_type=REL_TYPE_INHERITS)
        self.store.add_edge("pkg.C", "pkg.A", edge_type=REL_TYPE_INHERITS)
        # D inherits B and C (Multiple Inheritance)
        self.store.add_edge("pkg.D", "pkg.B", edge_type=REL_TYPE_INHERITS)
        self.store.add_edge("pkg.D", "pkg.C", edge_type=REL_TYPE_INHERITS)
        # E inherits D and Mixin1, Mixin2
        self.store.add_edge("pkg.E", "pkg.D", edge_type=REL_TYPE_INHERITS)
        self.store.add_edge("pkg.E", "pkg.Mixin1", edge_type=REL_TYPE_INHERITS)
        self.store.add_edge("pkg.E", "pkg.Mixin2", edge_type=REL_TYPE_INHERITS)
        # Implementations
        self.store.add_edge("pkg.B", "pkg.Interface1", edge_type=REL_TYPE_IMPLEMENTS)
        self.store.add_edge("pkg.D", "pkg.Interface2", edge_type=REL_TYPE_IMPLEMENTS)
        # Diamond: X -> A, Y -> A, Z -> X, Z -> Y
        self.store.add_edge("pkg.X", "pkg.A", edge_type=REL_TYPE_INHERITS)
        self.store.add_edge("pkg.Y", "pkg.A", edge_type=REL_TYPE_INHERITS)
        self.store.add_edge("pkg.Z", "pkg.X", edge_type=REL_TYPE_INHERITS)
        self.store.add_edge("pkg.Z", "pkg.Y", edge_type=REL_TYPE_INHERITS)
        # --- End of direct graph population ---


    def test_init(self):
        """Test tracker initialization."""
        self.assertIsNotNone(self.tracker.store)
        self.assertIsNotNone(self.tracker.traversal)
        self.assertIsInstance(self.tracker.store, GraphStore)
        self.assertIsInstance(self.tracker.traversal, GraphTraversal)
        # Caches should be initialized
        # TODO: Update these assertions if cache attribute names change or are removed
        # self.assertIsInstance(self.tracker._superclass_cache, dict)
        # self.assertIsInstance(self.tracker._subclass_cache, dict)


    def test_add_inheritance(self):
        """Test adding a valid inheritance relationship."""
        # Use the tracker's method to add, which should use the store
        self.tracker.add_inheritance("pkg.NewChild", "pkg.A", metadata={"source_info": "test"})

        # Verify using the store directly (as tracker doesn't expose direct edge check)
        edges_from_store = list(self.store.get_edges(source="pkg.NewChild"))
        self.assertEqual(len(edges_from_store), 1)
        
        # The edge tuple is (actual_source, actual_target, actual_data_dict)
        actual_source, actual_target, data = edges_from_store[0]
        
        self.assertEqual(actual_source, "pkg.NewChild")
        self.assertEqual(actual_target, "pkg.A")
        self.assertEqual(data.get('type'), REL_TYPE_INHERITS)
        # Assert based on the new key if 'source' from metadata is intended to be stored
        self.assertEqual(data.get('source_info'), "test") # Check for the new key
        self.assertIsNone(data.get('source')) # Ensure 'source' from metadata wasn't added directly if it conflicts


    def test_add_inheritance_with_empty_class_names(self):
        """Test adding inheritance with empty class names (should be ignored)."""
        initial_edge_count = len(list(self.store.get_edges()))

        # Mock logger to check warnings
        with patch('code_analysis.graph.inheritance_tracker.logger') as mock_logger:
            self.tracker.add_inheritance("", "pkg.A")
            self.tracker.add_inheritance("pkg.Child", "")
            self.tracker.add_inheritance("", "")

            # Verify warnings were logged
            self.assertGreaterEqual(mock_logger.warning.call_count, 2) # Should warn for empty child and parent

        # Verify no edges were added
        final_edge_count = len(list(self.store.get_edges()))
        self.assertEqual(final_edge_count, initial_edge_count)


    def test_add_inheritance_with_self_inheritance(self):
        """Test adding self-inheritance (should likely be allowed by tracker, but potentially flagged)."""
        # This might indicate an issue in the calling code (CodeVisitor)
        self.tracker.add_inheritance("pkg.A", "pkg.A", metadata={})
        # Check if the edge exists (store allows it)
        self.assertTrue(self.store.has_edge("pkg.A", "pkg.A", edge_type=REL_TYPE_INHERITS))


    def test_add_interface_implementation(self):
        """Test adding an interface implementation relationship."""
        self.tracker.add_interface_implementation("pkg.ImplClass", "pkg.Interface3", metadata={"detail": "abc"})

        # Verify using the store
        edges_from_store = list(self.store.get_edges(source="pkg.ImplClass"))
        found = False
        for src, target, data in edges_from_store:
            if src == "pkg.ImplClass" and target == "pkg.Interface3" and data.get('type') == REL_TYPE_IMPLEMENTS:
                self.assertEqual(data.get('detail'), "abc")
                found = True
                break
        self.assertTrue(found, "Implementation edge not found")


    def test_add_interface_implementation_with_empty_names(self):
        """Test adding implementation with empty names (should be ignored)."""
        initial_edge_count = len(list(self.store.get_edges()))
        with patch('code_analysis.graph.inheritance_tracker.logger') as mock_logger:
            self.tracker.add_interface_implementation("", "pkg.Interface1")
            self.tracker.add_interface_implementation("pkg.Impl", "")
            self.assertGreaterEqual(mock_logger.warning.call_count, 2)
        final_edge_count = len(list(self.store.get_edges()))
        self.assertEqual(final_edge_count, initial_edge_count)


    def test_add_interface_implementation_with_self_implementation(self):
        """Test adding self-implementation (should be allowed by store)."""
        self.tracker.add_interface_implementation("pkg.Interface1", "pkg.Interface1", metadata={})
        self.assertTrue(self.store.has_edge("pkg.Interface1", "pkg.Interface1", edge_type=REL_TYPE_IMPLEMENTS))


    def test_find_inheritance(self):
        """Test finding inheritance relationships."""
        # Find parents of D
        parents_of_d = self.tracker.find_relationships(source="pkg.D", relationship_type=REL_TYPE_INHERITS)
        parent_targets = {rel['target'] for rel in parents_of_d}
        self.assertEqual(parent_targets, {"pkg.B", "pkg.C"})

        # Find children of A
        children_of_a = self.tracker.find_relationships(target="pkg.A", relationship_type=REL_TYPE_INHERITS)
        child_sources = {rel['source'] for rel in children_of_a}
        self.assertEqual(child_sources, {"pkg.B", "pkg.C", "pkg.X", "pkg.Y"}) # Includes diamond


    def test_find_implementations(self):
        """Test finding implementation relationships."""
        # Find implementers of Interface1
        impls_of_i1 = self.tracker.find_relationships(target="pkg.Interface1", relationship_type=REL_TYPE_IMPLEMENTS)
        impl_sources = {rel['source'] for rel in impls_of_i1}
        self.assertEqual(impl_sources, {"pkg.B"})

        # Find interfaces implemented by D
        interfaces_by_d = self.tracker.find_relationships(source="pkg.D", relationship_type=REL_TYPE_IMPLEMENTS)
        interface_targets = {rel['target'] for rel in interfaces_by_d}
        self.assertEqual(interface_targets, {"pkg.Interface2"})


    def test_get_direct_subclasses_cached(self):
        """Test getting direct subclasses when result is cached."""
        # Pre-populate cache
        self.tracker._subclass_cache["pkg.A"] = ["pkg.B", "pkg.C", "pkg.X", "pkg.Y"]
        # Mock the store's get_in_edges to ensure cache is used
        self.store.get_in_edges = Mock(return_value=[])

        subclasses = self.tracker.get_direct_subclasses("pkg.A")
        self.assertEqual(set(subclasses), {"pkg.B", "pkg.C", "pkg.X", "pkg.Y"})
        self.store.get_in_edges.assert_not_called() # Verify cache hit


    def test_get_direct_subclasses_not_cached(self):
        """Test getting direct subclasses when result is not cached."""
        # Ensure cache is empty
        self.tracker._subclass_cache.clear()

        subclasses = self.tracker.get_direct_subclasses("pkg.A")
        self.assertEqual(set(subclasses), {"pkg.B", "pkg.C", "pkg.X", "pkg.Y"})
        # Verify cache is populated
        self.assertIn("pkg.A", self.tracker._subclass_cache)
        self.assertEqual(set(self.tracker._subclass_cache["pkg.A"]), {"pkg.B", "pkg.C", "pkg.X", "pkg.Y"})


    def test_get_direct_superclasses_cached(self):
        """Test getting direct superclasses when result is cached."""
        self.tracker._superclass_cache["pkg.D"] = ["pkg.B", "pkg.C"]
        self.store.get_out_edges = Mock(return_value=[]) # Mock store method

        superclasses = self.tracker.get_direct_superclasses("pkg.D")
        self.assertEqual(set(superclasses), {"pkg.B", "pkg.C"})
        self.store.get_out_edges.assert_not_called()


    def test_get_direct_superclasses_not_cached(self):
        """Test getting direct superclasses when result is not cached."""
        self.tracker._superclass_cache.clear()

        superclasses = self.tracker.get_direct_superclasses("pkg.D")
        self.assertEqual(set(superclasses), {"pkg.B", "pkg.C"})
        self.assertIn("pkg.D", self.tracker._superclass_cache)
        self.assertEqual(set(self.tracker._superclass_cache["pkg.D"]), {"pkg.B", "pkg.C"})


    def test_get_all_subclasses(self):
        """Test getting all (direct and indirect) subclasses."""
        # Mock get_direct_subclasses to control recursion
        def get_direct_subclasses_side_effect(cls):
            if cls == "pkg.A": return ["pkg.B", "pkg.C", "pkg.X", "pkg.Y"]
            if cls == "pkg.B": return ["pkg.D"]
            if cls == "pkg.C": return ["pkg.D"]
            if cls == "pkg.D": return ["pkg.E"]
            if cls == "pkg.X": return ["pkg.Z"]
            if cls == "pkg.Y": return ["pkg.Z"]
            return []
        self.tracker.get_direct_subclasses = Mock(side_effect=get_direct_subclasses_side_effect)

        all_subs = self.tracker.get_all_subclasses("pkg.A")
        self.assertEqual(all_subs, {"pkg.B", "pkg.C", "pkg.D", "pkg.E", "pkg.X", "pkg.Y", "pkg.Z"})


    def test_get_all_ancestors(self):
        """Test getting all (direct and indirect) ancestors."""
        # Mock get_direct_superclasses
        def get_direct_superclasses_side_effect(cls):
            if cls == "pkg.E": return ["pkg.D", "pkg.Mixin1", "pkg.Mixin2"]
            if cls == "pkg.D": return ["pkg.B", "pkg.C"]
            if cls == "pkg.B": return ["pkg.A"]
            if cls == "pkg.C": return ["pkg.A"]
            if cls == "pkg.Z": return ["pkg.X", "pkg.Y"]
            if cls == "pkg.X": return ["pkg.A"]
            if cls == "pkg.Y": return ["pkg.A"]
            return []
        self.tracker.get_direct_superclasses = Mock(side_effect=get_direct_superclasses_side_effect)

        all_ancs_e = self.tracker.get_all_ancestors("pkg.E")
        self.assertEqual(all_ancs_e, {"pkg.D", "pkg.Mixin1", "pkg.Mixin2", "pkg.B", "pkg.C", "pkg.A"})

        all_ancs_z = self.tracker.get_all_ancestors("pkg.Z")
        self.assertEqual(all_ancs_z, {"pkg.X", "pkg.Y", "pkg.A"})


    def test_get_inheritance_hierarchy_cached(self):
        """Test getting hierarchy when cached."""
        # Pre-populate cache (example structure)
        cached_hierarchy = {"pkg.D": {"parents": ["pkg.B", "pkg.C"], "children": ["pkg.E"]}}
        self.tracker._hierarchy_cache["pkg.D"] = cached_hierarchy
        # Mock underlying build methods
        self.tracker._build_superclass_hierarchy = Mock()
        self.tracker._build_subclass_hierarchy = Mock()

        hierarchy = self.tracker.get_inheritance_hierarchy("pkg.D")
        self.assertEqual(hierarchy, cached_hierarchy)
        self.tracker._build_superclass_hierarchy.assert_not_called()
        self.tracker._build_subclass_hierarchy.assert_not_called()


    @patch.object(InheritanceTracker, '_build_superclass_hierarchy')
    def test_get_inheritance_hierarchy_up(self, mock_build_superclass):
        """Test getting hierarchy upwards (ancestors)."""
        mock_build_superclass.return_value = {"pkg.B": {"parents": ["pkg.A"]}, "pkg.C": {"parents": ["pkg.A"]}}
        self.tracker._hierarchy_cache.clear() # Ensure not cached

        hierarchy = self.tracker.get_inheritance_hierarchy("pkg.D", direction="up")
        expected = {"pkg.D": {"parents": ["pkg.B", "pkg.C"]}, # Direct parents from store
                    "pkg.B": {"parents": ["pkg.A"]},
                    "pkg.C": {"parents": ["pkg.A"]}}
        self.assertEqual(hierarchy, expected)
        mock_build_superclass.assert_called_once_with("pkg.D", {})
        self.assertIn("pkg.D", self.tracker._hierarchy_cache) # Check cache population


    @patch.object(InheritanceTracker, '_build_subclass_hierarchy')
    def test_get_inheritance_hierarchy_down(self, mock_build_subclass):
        """Test getting hierarchy downwards (descendants)."""
        mock_build_subclass.return_value = {"pkg.B": {"children": ["pkg.D"]}, "pkg.C": {"children": ["pkg.D"]}}
        self.tracker._hierarchy_cache.clear()

        hierarchy = self.tracker.get_inheritance_hierarchy("pkg.A", direction="down")
        expected = {"pkg.A": {"children": ["pkg.B", "pkg.C", "pkg.X", "pkg.Y"]}, # Direct children from store
                    "pkg.B": {"children": ["pkg.D"]},
                    "pkg.C": {"children": ["pkg.D"]}} # Mock only returns B, C children
        # Note: The expected result depends heavily on the mock return value.
        # The actual direct children are added first.
        self.assertEqual(hierarchy["pkg.A"], expected["pkg.A"])
        self.assertEqual(hierarchy["pkg.B"], expected["pkg.B"])
        self.assertEqual(hierarchy["pkg.C"], expected["pkg.C"])
        # Check for X, Y added by direct lookup but not in mock result
        self.assertIn("pkg.X", hierarchy)
        self.assertIn("pkg.Y", hierarchy)

        mock_build_subclass.assert_called_once_with("pkg.A", {})
        self.assertIn("pkg.A", self.tracker._hierarchy_cache)


    def test_build_superclass_hierarchy(self):
        """Test the internal method for building superclass hierarchy."""
        # Mock get_direct_superclasses
        def get_direct_superclasses_side_effect(cls):
            if cls == "pkg.E": return ["pkg.D", "pkg.Mixin1", "pkg.Mixin2"]
            if cls == "pkg.D": return ["pkg.B", "pkg.C"]
            if cls == "pkg.B": return ["pkg.A"]
            if cls == "pkg.C": return ["pkg.A"]
            return []
        self.tracker.get_direct_superclasses = Mock(side_effect=get_direct_superclasses_side_effect)

        hierarchy = {}
        self.tracker._build_superclass_hierarchy("pkg.E", hierarchy)

        expected = {
            "pkg.E": {"parents": ["pkg.D", "pkg.Mixin1", "pkg.Mixin2"]},
            "pkg.D": {"parents": ["pkg.B", "pkg.C"]},
            "pkg.Mixin1": {"parents": []},
            "pkg.Mixin2": {"parents": []},
            "pkg.B": {"parents": ["pkg.A"]},
            "pkg.C": {"parents": ["pkg.A"]},
            "pkg.A": {"parents": []}
        }
        self.assertEqual(hierarchy, expected)


    def test_build_subclass_hierarchy(self):
        """Test the internal method for building subclass hierarchy."""
        # Mock get_direct_subclasses
        def get_direct_subclasses_side_effect(cls):
            if cls == "pkg.A": return ["pkg.B", "pkg.C", "pkg.X", "pkg.Y"]
            if cls == "pkg.B": return ["pkg.D"]
            if cls == "pkg.C": return ["pkg.D"]
            if cls == "pkg.D": return ["pkg.E"]
            if cls == "pkg.X": return ["pkg.Z"]
            if cls == "pkg.Y": return ["pkg.Z"]
            return []
        self.tracker.get_direct_subclasses = Mock(side_effect=get_direct_subclasses_side_effect)

        hierarchy = {}
        self.tracker._build_subclass_hierarchy("pkg.A", hierarchy)

        expected = {
            "pkg.A": {"children": ["pkg.B", "pkg.C", "pkg.X", "pkg.Y"]},
            "pkg.B": {"children": ["pkg.D"]},
            "pkg.C": {"children": ["pkg.D"]},
            "pkg.X": {"children": ["pkg.Z"]},
            "pkg.Y": {"children": ["pkg.Z"]},
            "pkg.D": {"children": ["pkg.E"]},
            "pkg.E": {"children": []},
            "pkg.Z": {"children": []}
        }
        # Need to handle potential duplicates in children list if graph has cycles or complex paths
        # The current implementation might add duplicates if not careful. Let's check the sets.
        self.assertEqual(set(hierarchy["pkg.A"]["children"]), set(expected["pkg.A"]["children"]))
        self.assertEqual(set(hierarchy["pkg.B"]["children"]), set(expected["pkg.B"]["children"]))
        # ... compare other sets ...
        self.assertEqual(len(hierarchy), len(expected)) # Check all nodes were visited


    def test_class_depth(self):
        """Test calculating the depth of a class in the hierarchy."""
        # Mock get_direct_superclasses
        def get_direct_superclasses_side_effect(cls):
            if cls == "pkg.E": return ["pkg.D"] # Simplified for depth test
            if cls == "pkg.D": return ["pkg.B"]
            if cls == "pkg.B": return ["pkg.A"]
            return []
        self.tracker.get_direct_superclasses = Mock(side_effect=get_direct_superclasses_side_effect)

        depth_e = self.tracker.get_class_depth("pkg.E")
        self.assertEqual(depth_e, 3) # E -> D -> B -> A (depth 3)

        depth_a = self.tracker.get_class_depth("pkg.A")
        self.assertEqual(depth_a, 0)

        depth_mixin = self.tracker.get_class_depth("pkg.Mixin1") # No parents in mock
        self.assertEqual(depth_mixin, 0)


    def test_inheritance_path(self):
        """Test finding the inheritance path between two classes."""
        # Mock get_direct_superclasses for path E -> A
        def get_direct_superclasses_side_effect(cls):
            if cls == "pkg.E": return ["pkg.D"]
            if cls == "pkg.D": return ["pkg.B"]
            if cls == "pkg.B": return ["pkg.A"]
            return []
        self.tracker.get_direct_superclasses = Mock(side_effect=get_direct_superclasses_side_effect)

        path = self.tracker.get_inheritance_path("pkg.E", "pkg.A")
        self.assertEqual(path, ["pkg.E", "pkg.D", "pkg.B", "pkg.A"])


    def test_inheritance_path_same_class(self):
        """Test finding path when start and end are the same."""
        path = self.tracker.get_inheritance_path("pkg.A", "pkg.A")
        self.assertEqual(path, ["pkg.A"])


    def test_inheritance_path_no_path(self):
        """Test finding path when no inheritance relationship exists."""
        # Mock get_direct_superclasses or rely on traversal mock
        # For this test, ensuring traversal returns empty is clearest
        if self.tracker.traversal:
            self.tracker.traversal.find_shortest_path = Mock(return_value=[])
        
        # Also mock get_all_ancestors to ensure the fallback check also indicates no relation
        self.tracker.get_all_ancestors = Mock(return_value=[])

        path = self.tracker.get_inheritance_path("pkg.Mixin1", "pkg.A")
        self.assertEqual(path, []) # MODIFIED: Expect empty list for no path


    def test_is_subclass_of(self):
        """Test checking if a class is a subclass of another."""
        # Mock get_all_ancestors
        self.tracker.get_all_ancestors = Mock(side_effect=lambda cls: {
            "pkg.E": {"pkg.D", "pkg.Mixin1", "pkg.Mixin2", "pkg.B", "pkg.C", "pkg.A"},
            "pkg.D": {"pkg.B", "pkg.C", "pkg.A"},
            "pkg.B": {"pkg.A"},
            "pkg.C": {"pkg.A"},
            "pkg.Z": {"pkg.X", "pkg.Y", "pkg.A"},
            "pkg.X": {"pkg.A"},
            "pkg.Y": {"pkg.A"},
        }.get(cls, set()))

        self.assertTrue(self.tracker.is_subclass_of("pkg.E", "pkg.A"))
        self.assertTrue(self.tracker.is_subclass_of("pkg.D", "pkg.A"))
        self.assertTrue(self.tracker.is_subclass_of("pkg.B", "pkg.A"))
        self.assertTrue(self.tracker.is_subclass_of("pkg.Z", "pkg.A"))
        self.assertFalse(self.tracker.is_subclass_of("pkg.A", "pkg.E"))
        self.assertFalse(self.tracker.is_subclass_of("pkg.Mixin1", "pkg.A"))
        self.assertFalse(self.tracker.is_subclass_of("pkg.A", "pkg.A")) # Should not be subclass of itself


    def test_find_common_ancestor(self):
        """Test finding the lowest common ancestor."""
        # Mock get_all_ancestors and get_class_depth
        ancestors_data = {
            "pkg.E": {"pkg.D", "pkg.Mixin1", "pkg.Mixin2", "pkg.B", "pkg.C", "pkg.A"},
            "pkg.D": {"pkg.B", "pkg.C", "pkg.A"},
            "pkg.B": {"pkg.A"},
            "pkg.C": {"pkg.A"},
            "pkg.Z": {"pkg.X", "pkg.Y", "pkg.A"},
            "pkg.X": {"pkg.A"},
            "pkg.Y": {"pkg.A"},
            "pkg.A": set(),
            "pkg.Mixin1": set(),
        }
        depths_data = {"pkg.A": 0, "pkg.B": 1, "pkg.C": 1, "pkg.X": 1, "pkg.Y": 1,
                  "pkg.D": 2, "pkg.Z": 2, "pkg.E": 3, "pkg.Mixin1": 0, "pkg.Mixin2": 0}

        # MODIFIED: Mock to accept use_cache or *args, **kwargs
        self.tracker.get_all_ancestors = Mock(side_effect=lambda cls, *args, **kwargs: list(ancestors_data.get(cls, set())))
        self.tracker.get_class_depth = Mock(side_effect=lambda cls, *args, **kwargs: depths_data.get(cls, -1))

        lca_bc = self.tracker.find_common_ancestor("pkg.B", "pkg.C")
        self.assertEqual(lca_bc, "pkg.A")

        lca_de = self.tracker.find_common_ancestor("pkg.D", "pkg.E")
        self.assertEqual(lca_de, "pkg.D") # E inherits D

        lca_ze = self.tracker.find_common_ancestor("pkg.Z", "pkg.E") # Common ancestor is A
        self.assertEqual(lca_ze, "pkg.A")


    def test_find_common_ancestor_no_common(self):
        """Test finding common ancestor when none exists."""
        ancestors_data = {
            "pkg.E": {"pkg.D", "pkg.Mixin1", "pkg.Mixin2", "pkg.B", "pkg.C", "pkg.A"},
            "pkg.Mixin1": set(),
        }
        depths_data = {"pkg.A": 0, "pkg.B": 1, "pkg.C": 1, "pkg.D": 2, "pkg.E": 3, "pkg.Mixin1": 0}
        # MODIFIED: Mock to accept use_cache or *args, **kwargs
        self.tracker.get_all_ancestors = Mock(side_effect=lambda cls, *args, **kwargs: list(ancestors_data.get(cls, set())))
        self.tracker.get_class_depth = Mock(side_effect=lambda cls, *args, **kwargs: depths_data.get(cls, -1))

        lca = self.tracker.find_common_ancestor("pkg.E", "pkg.Mixin1")
        self.assertIsNone(lca)


    def test_get_interface_implementations(self):
        """Test getting classes that implement an interface."""
        direct_implementers_data = {
            "pkg.Interface1": ["pkg.B"],
            "pkg.Interface2": ["pkg.D"],
        }
        subclasses_data = {
            "pkg.B": ["pkg.D", "pkg.E"], 
            "pkg.D": ["pkg.E"],
        }
        # Mock find_relationships for direct implementers
        def mock_find_relationships_for_implementers(target, relationship_type, **kwargs):
            if relationship_type == REL_TYPE_IMPLEMENTS: # target is the interface
                return [{"source": cls} for cls in direct_implementers_data.get(target, [])]
            return []
        self.tracker.find_relationships = Mock(side_effect=mock_find_relationships_for_implementers)
        # MODIFIED: Mock to accept use_cache or *args, **kwargs
        self.tracker.get_all_subclasses = Mock(side_effect=lambda cls, *args, **kwargs: subclasses_data.get(cls, []))

        impls_i1 = self.tracker.get_interface_implementations("pkg.Interface1")
        self.assertEqual(set(impls_i1), {"pkg.B", "pkg.D", "pkg.E"})

        impls_i2 = self.tracker.get_interface_implementations("pkg.Interface2")
        self.assertEqual(set(impls_i2), {"pkg.D", "pkg.E"})
        
        impls_i3 = self.tracker.get_interface_implementations("pkg.Interface3") # Not implemented
        self.assertEqual(set(impls_i3), set())


    def test_get_implemented_interfaces(self):
        """Test getting interfaces implemented by a class."""
        direct_interfaces_data = {
            "pkg.B": ["pkg.Interface1"],
            "pkg.D": ["pkg.Interface2"],
        }
        ancestors_data = {
            "pkg.E": ["pkg.D", "pkg.B", "pkg.A"], # Simplified, order might matter for some logic but not for set
            "pkg.D": ["pkg.B", "pkg.A"],
            "pkg.B": ["pkg.A"],
        }
        # Mock find_relationships for direct implementations
        def mock_find_relationships_for_interfaces(source, relationship_type, **kwargs):
            if relationship_type == REL_TYPE_IMPLEMENTS:
                return [{"target": iface} for iface in direct_interfaces_data.get(source, [])]
            return []
        self.tracker.find_relationships = Mock(side_effect=mock_find_relationships_for_interfaces)
        # MODIFIED: Mock to accept use_cache or *args, **kwargs
        self.tracker.get_all_ancestors = Mock(side_effect=lambda cls, *args, **kwargs: ancestors_data.get(cls, []))

        interfaces_e = self.tracker.get_implemented_interfaces("pkg.E")
        self.assertEqual(set(interfaces_e), {"pkg.Interface1", "pkg.Interface2"})

        interfaces_b = self.tracker.get_implemented_interfaces("pkg.B")
        self.assertEqual(set(interfaces_b), {"pkg.Interface1"})

        interfaces_a = self.tracker.get_implemented_interfaces("pkg.A") # No direct or indirect interfaces
        self.assertEqual(set(interfaces_a), set())


    def test_find_diamond_inheritance(self):
        """Test detecting diamond inheritance patterns."""
        # Mock get_direct_superclasses and get_all_ancestors
        def get_direct_superclasses_side_effect(cls):
            if cls == "pkg.Z": return ["pkg.X", "pkg.Y"]
            if cls == "pkg.X": return ["pkg.A"]
            if cls == "pkg.Y": return ["pkg.A"]
            if cls == "pkg.A": return []
            return [] # Default case
        self.tracker.get_direct_parents = Mock(side_effect=get_direct_superclasses_side_effect)
        ancestors = { "pkg.Z": {"pkg.X", "pkg.Y", "pkg.A"}, "pkg.X": {"pkg.A"}, "pkg.Y": {"pkg.A"}, "pkg.A": set() }
        def get_all_ancestors_side_effect(cls):
            # Need to handle recursion properly if the actual method uses it
            # For testing, return precomputed ancestors
            return ancestors.get(cls, set())
        self.tracker.get_all_ancestors = Mock(side_effect=lambda cls, *args, **kwargs: list(ancestors.get(cls, set())))

        diamonds = self.tracker.find_diamond_inheritance()
        # Expecting diamond involving Z -> (X, Y) -> A
        self.assertIn("pkg.Z", diamonds)
        self.assertEqual(diamonds["pkg.Z"], "pkg.A") # Common ancestor is A


    def test_find_mixin_classes(self):
        """Test identifying mixin classes (multiple inheritance, no deep hierarchy)."""
        # Mock get_direct_subclasses and get_direct_superclasses
        def get_direct_subclasses_side_effect(cls):
            if cls == "pkg.Mixin1": return ["pkg.E"]
            if cls == "pkg.Mixin2": return ["pkg.E"]
            return []
        self.tracker.get_direct_subclasses = Mock(side_effect=get_direct_subclasses_side_effect)
        def get_direct_superclasses_side_effect(cls):
            if cls == "pkg.Mixin1": return [] # Mixins have no parents in this setup
            if cls == "pkg.Mixin2": return []
            if cls == "pkg.E": return ["pkg.D", "pkg.Mixin1", "pkg.Mixin2"] # E uses mixins
            return []
        self.tracker.get_direct_superclasses = Mock(side_effect=get_direct_superclasses_side_effect)

        mixins = self.tracker.find_mixin_classes(max_depth=0) # Mixins should have depth 0
        self.assertEqual(mixins, {"pkg.Mixin1", "pkg.Mixin2"})

        # Test with depth 1 (should not find Mixin1/2)
        mixins_depth1 = self.tracker.find_mixin_classes(max_depth=1)
        self.assertEqual(mixins_depth1, set())


    def test_calculate_inheritance_metrics(self):
        """Test calculating various inheritance metrics."""
        # Mock methods used by calculate_inheritance_metrics
        self.tracker.get_all_classes = Mock(return_value={"pkg.A", "pkg.B", "pkg.C", "pkg.D", "pkg.E", "pkg.Mixin1", "pkg.Mixin2", "pkg.X", "pkg.Y", "pkg.Z"})
        self.tracker.get_class_depth = Mock(side_effect=lambda cls: {
            "pkg.A": 0, "pkg.B": 1, "pkg.C": 1, "pkg.D": 2, "pkg.E": 3,
            "pkg.Mixin1": 0, "pkg.Mixin2": 0, "pkg.X": 1, "pkg.Y": 1, "pkg.Z": 2
        }.get(cls, 0))
        self.tracker.get_direct_subclasses = Mock(side_effect=lambda cls: {
            "pkg.A": ["pkg.B", "pkg.C", "pkg.X", "pkg.Y"], "pkg.B": ["pkg.D"], "pkg.C": ["pkg.D"],
            "pkg.D": ["pkg.E"], "pkg.X": ["pkg.Z"], "pkg.Y": ["pkg.Z"]
        }.get(cls, []))
        self.tracker.find_diamond_inheritance = Mock(return_value={"pkg.Z": "pkg.A"})
        self.tracker.find_mixin_classes = Mock(return_value={"pkg.Mixin1", "pkg.Mixin2"})

        metrics = self.tracker.calculate_inheritance_metrics()

        self.assertEqual(metrics["total_classes"], 10)
        self.assertEqual(metrics["max_depth"], 3)
        self.assertAlmostEqual(metrics["average_depth"], (0+1+1+2+3+0+0+1+1+2)/10)
        # Width at depth 1: B, C, X, Y (4)
        self.assertEqual(metrics["max_width"], 4)
        self.assertEqual(metrics["width_at_depth"][1], 4)
        self.assertEqual(metrics["width_at_depth"][2], 2) # D, Z
        self.assertEqual(metrics["width_at_depth"][3], 1) # E
        self.assertEqual(metrics["diamond_inheritance_count"], 1)
        self.assertEqual(metrics["mixin_class_count"], 2)
        # Number of children (NOC)
        self.assertEqual(metrics["average_noc"], (4+1+1+1+0+0+0+1+1+0)/10) # Sum NOC / total classes
        self.assertEqual(metrics["max_noc"], 4) # Class A has 4 children


    def test_find_complex_hierarchies(self):
        """Test finding hierarchies exceeding complexity thresholds."""
        # Mock metrics calculation
        self.tracker.calculate_inheritance_metrics = Mock(return_value={
            "total_classes": 10, "max_depth": 3, "average_depth": 1.1,
            "max_width": 4, "width_at_depth": {0: 1, 1: 4, 2: 2, 3: 1},
            "diamond_inheritance_count": 1, "mixin_class_count": 2,
            "average_noc": 0.9, "max_noc": 4,
            "noc_distribution": {0: 4, 1: 4, 4: 1} # Example NOC distribution
        })
        # Mock get_all_classes and individual class metrics if needed by find_complex_hierarchies
        self.tracker.get_all_classes = Mock(return_value={"pkg.A", "pkg.B", "pkg.C", "pkg.D", "pkg.E", "pkg.Mixin1", "pkg.Mixin2", "pkg.X", "pkg.Y", "pkg.Z"})
        self.tracker.get_class_depth = Mock(side_effect=lambda cls: {
            "pkg.A": 0, "pkg.B": 1, "pkg.C": 1, "pkg.D": 2, "pkg.E": 3,
            "pkg.Mixin1": 0, "pkg.Mixin2": 0, "pkg.X": 1, "pkg.Y": 1, "pkg.Z": 2
        }.get(cls, 0))
         # Mock get_direct_subclasses to return counts for NOC check
        self.tracker.get_direct_subclasses = Mock(side_effect=lambda cls: {
            "pkg.A": ["pkg.B", "pkg.C", "pkg.X", "pkg.Y"], # NOC = 4
            "pkg.B": ["pkg.D"], # NOC = 1
            "pkg.C": ["pkg.D"], # NOC = 1
            "pkg.D": ["pkg.E"], # NOC = 1
            "pkg.X": ["pkg.Z"], # NOC = 1
            "pkg.Y": ["pkg.Z"], # NOC = 1
             # Others have NOC = 0
        }.get(cls, []))


        # Find hierarchies with depth > 2 OR NOC > 3
        complex_classes = self.tracker.find_complex_hierarchies(min_depth=3, min_noc=4)
        # Expected: E (depth 3), A (NOC 4)
        # Compare as sets to ignore order
        self.assertEqual(set(complex_classes), {"pkg.E", "pkg.A"})

        # Find hierarchies with depth > 3 (only E)
        complex_depth = self.tracker.find_complex_hierarchies(min_depth=4, min_noc=10)
        self.assertEqual(complex_depth, set()) # Should be empty based on mock

        # Find hierarchies with NOC > 1 (A, B, C, D, X, Y)
        complex_noc = self.tracker.find_complex_hierarchies(min_depth=10, min_noc=2)
        self.assertEqual(complex_noc, {"pkg.A"}) # Only A has NOC > 1


    def test_get_all_classes(self):
        """Test retrieving all unique class names from the graph."""
        # Add an interface node to ensure only classes are returned
        self.store.add_node("pkg.AnotherInterface", node_type="interface")
        self.store.add_node("pkg.StandaloneClass", node_type="class")

        all_classes = self.tracker.get_all_classes()
        expected = {"pkg.A", "pkg.B", "pkg.C", "pkg.D", "pkg.E",
                    "pkg.Mixin1", "pkg.Mixin2", "pkg.X", "pkg.Y", "pkg.Z",
                    "pkg.StandaloneClass"} # Added class
        self.assertEqual(all_classes, expected)


    def test_clear_caches_specific_class(self):
        """Test clearing caches for a specific class."""
        # Populate caches
        self.tracker._superclass_cache["pkg.D"] = ["pkg.B", "pkg.C"]
        self.tracker._subclass_cache["pkg.A"] = ["pkg.B", "pkg.C"]
        self.tracker._hierarchy_cache["pkg.D"] = {"parents": ["pkg.B", "pkg.C"]}

        self.tracker.clear_caches("pkg.D")

        self.assertNotIn("pkg.D", self.tracker._superclass_cache)
        self.assertNotIn("pkg.D", self.tracker._hierarchy_cache)
        # Subclass cache for A should remain
        self.assertIn("pkg.A", self.tracker._subclass_cache)


    def test_clear_caches_all(self):
        """Test clearing all caches."""
        self.tracker._superclass_cache["pkg.D"] = ["pkg.B", "pkg.C"]
        self.tracker._subclass_cache["pkg.A"] = ["pkg.B", "pkg.C"]
        self.tracker._hierarchy_cache["pkg.D"] = {"parents": ["pkg.B", "pkg.C"]}
        self.tracker._class_depth_cache["pkg.D"] = 2
        self.tracker._implemented_interfaces_cache["pkg.D"] = ["pkg.Interface1"]
        self.tracker._interface_implementations_cache["pkg.Interface1"] = ["pkg.D"]

        self.tracker.clear_all_inheritance_caches()

        self.assertEqual(self.tracker._superclass_cache, {})
        self.assertEqual(self.tracker._subclass_cache, {})
        self.assertEqual(self.tracker._hierarchy_cache, {})
        self.assertEqual(self.tracker._class_depth_cache, {})
        self.assertEqual(self.tracker._implemented_interfaces_cache, {})
        self.assertEqual(self.tracker._interface_implementations_cache, {})


if __name__ == '__main__':
    unittest.main()