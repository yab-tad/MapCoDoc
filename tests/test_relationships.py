import unittest
from unittest.mock import MagicMock, patch

from code_analysis.graph.relationships import RelationshipTracker
from code_analysis.graph.store import GraphStore
from code_analysis.relationship_types import (
    REL_TYPE_CALLS, REL_TYPE_CALLED_BY,
    REL_TYPE_CONTAINS, REL_TYPE_CONTAINED_BY,
    REL_TYPE_IMPORTS, REL_TYPE_IMPORTED_BY,
    REL_TYPE_INHERITS, REL_TYPE_INHERITED_BY
)


class TestRelationshipTracker(unittest.TestCase):
    """Test cases for the RelationshipTracker class."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_store = MagicMock()
        self.mock_traversal = MagicMock()
        self.tracker = RelationshipTracker(store=self.mock_store, traversal=self.mock_traversal)

    def test_initialization(self):
        """Test that the RelationshipTracker initializes correctly."""
        # Should create a new store if none provided
        tracker = RelationshipTracker()
        self.assertIsNotNone(tracker._store)
        
        # Should use provided store
        self.assertEqual(self.tracker._store, self.mock_store)
        self.assertEqual(self.tracker._traversal, self.mock_traversal)

    def test_add_relationship(self):
        """Test adding a relationship."""
        source = "module_a"
        target = "module_b"
        rel_type = REL_TYPE_IMPORTS
        properties = {"name": "test_import"}

        self.tracker.add_relationship(source, target, rel_type, properties)
        
        # Check if store.add_relationship was called correctly
        self.mock_store.add_relationship.assert_called_once_with(
            source, target, rel_type, properties
        )

    def test_add_relationship_with_incomplete_data(self):
        """Test handling of incomplete data when adding a relationship."""
        # Test with empty source
        self.tracker.add_relationship("", "target", REL_TYPE_IMPORTS, {})
        self.mock_store.add_relationship.assert_not_called()
        
        # Test with empty target
        self.mock_store.reset_mock()
        self.tracker.add_relationship("source", "", REL_TYPE_IMPORTS, {})
        self.mock_store.add_relationship.assert_not_called()

    def test_find_relationships(self):
        """Test finding relationships by criteria."""
        self.mock_store.get_relationships.return_value = [
            ("source1", "target1", REL_TYPE_IMPORTS, {"name": "import1"}),
            ("source2", "target2", REL_TYPE_IMPORTS, {"name": "import2"})
        ]
        
        # Test finding by relationship type
        results = self.tracker.find_relationships(REL_TYPE_IMPORTS)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["source"], "source1")
        self.assertEqual(results[0]["target"], "target1")
        self.assertEqual(results[0]["relationship_type"], REL_TYPE_IMPORTS)
        self.assertEqual(results[0]["properties"]["name"], "import1")
        
        # Test finding by source
        self.mock_store.reset_mock()
        self.mock_store.get_relationships.return_value = [
            ("source1", "target1", REL_TYPE_IMPORTS, {"name": "import1"})
        ]
        results = self.tracker.find_relationships(REL_TYPE_IMPORTS, source="source1")
        self.assertEqual(len(results), 1)
        self.mock_store.get_relationships.assert_called_once_with(
            source="source1", target=None, relationship_type=REL_TYPE_IMPORTS
        )
        
        # Test finding by properties
        self.mock_store.reset_mock()
        self.mock_store.get_relationships.return_value = [
            ("source1", "target1", REL_TYPE_IMPORTS, {"name": "import1", "is_star": True}),
            ("source2", "target2", REL_TYPE_IMPORTS, {"name": "import2", "is_star": False})
        ]
        results = self.tracker.find_relationships(
            REL_TYPE_IMPORTS, properties={"is_star": True}
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "source1")

    def test_get_outgoing_relationships(self):
        """Test getting outgoing relationships for a node."""
        self.mock_store.get_relationships.return_value = [
            ("source", "target1", REL_TYPE_IMPORTS, {"prop": "value1"}),
            ("source", "target2", REL_TYPE_CALLS, {"prop": "value2"})
        ]
        
        results = self.tracker.get_outgoing_relationships("source")
        
        self.assertEqual(len(results[REL_TYPE_IMPORTS]), 1)
        self.assertEqual(len(results[REL_TYPE_CALLS]), 1)
        self.assertEqual(results[REL_TYPE_IMPORTS][0]["target"], "target1")
        self.assertEqual(results[REL_TYPE_CALLS][0]["target"], "target2")

    def test_get_incoming_relationships(self):
        """Test getting incoming relationships for a node."""
        self.mock_store.get_relationships.return_value = [
            ("source1", "target", REL_TYPE_IMPORTS, {"prop": "value1"}),
            ("source2", "target", REL_TYPE_CALLS, {"prop": "value2"})
        ]
        
        results = self.tracker.get_incoming_relationships("target")
        
        self.assertEqual(len(results[REL_TYPE_IMPORTS]), 1)
        self.assertEqual(len(results[REL_TYPE_CALLS]), 1)
        self.assertEqual(results[REL_TYPE_IMPORTS][0]["source"], "source1")
        self.assertEqual(results[REL_TYPE_CALLS][0]["source"], "source2")

    def test_has_relationship(self):
        """Test checking if a relationship exists."""
        # Scenario: relationship exists
        self.mock_store.relationship_exists.return_value = True
        
        result = self.tracker.has_relationship(
            "source", "target", REL_TYPE_IMPORTS
        )
        
        self.assertTrue(result)
        self.mock_store.relationship_exists.assert_called_once_with(
            "source", "target", REL_TYPE_IMPORTS
        )
        
        # Scenario: relationship doesn't exist
        self.mock_store.reset_mock()
        self.mock_store.relationship_exists.return_value = False
        
        result = self.tracker.has_relationship(
            "source", "target", REL_TYPE_IMPORTS
        )
        
        self.assertFalse(result)

    def test_get_relationship_count(self):
        """Test getting the count of relationships."""
        self.mock_store.count_relationships.return_value = 42
        
        # Count for a specific relationship type
        count = self.tracker.get_relationship_count(REL_TYPE_IMPORTS)
        
        self.assertEqual(count, 42)
        self.mock_store.count_relationships.assert_called_once_with(
            relationship_type=REL_TYPE_IMPORTS
        )
        
        # Count for all relationship types
        self.mock_store.reset_mock()
        self.mock_store.count_relationships.return_value = 100
        
        count = self.tracker.get_relationship_count()
        
        self.assertEqual(count, 100)
        self.mock_store.count_relationships.assert_called_once_with(
            relationship_type=None
        )

    def test_remove_relationship(self):
        """Test removing a relationship."""
        self.tracker.remove_relationship("source", "target", REL_TYPE_IMPORTS)
        
        self.mock_store.remove_relationship.assert_called_once_with(
            "source", "target", REL_TYPE_IMPORTS
        )


if __name__ == "__main__":
    unittest.main() 