import os
import unittest
from unittest.mock import MagicMock, patch

from code_analysis.feature_flags import (
    Feature, is_enabled, enable, disable,
    set_feature_state, get_all_feature_states,
    list_flags, enable_for_test, restore_states,
    set_registry
)


class TestFeatureFlags(unittest.TestCase):
    """Test the feature flags system."""

    def setUp(self):
        """Reset feature states before each test."""
        # Reset all features to disabled
        for feature in Feature:
            disable(feature)

    def tearDown(self):
        """Clean up environment after each test."""
        # Remove any environment variables we set during testing
        for feature in Feature:
            env_key1 = f"MAPCODOC_FEATURE_{feature.name}"
            env_key2 = f"MAPCOD_ENABLE_{feature.name}"
            
            if env_key1 in os.environ:
                del os.environ[env_key1]
            if env_key2 in os.environ:
                del os.environ[env_key2]

    def test_default_state(self):
        """Test default state of features."""
        for feature in Feature:
            self.assertFalse(is_enabled(feature), f"Feature {feature.name} should be disabled by default")

    def test_enable_disable(self):
        """Test enabling and disabling features."""
        feature = Feature.API_BOUNDARY_DETECTION
        
        # Initially disabled
        self.assertFalse(is_enabled(feature))
        
        # Enable
        enable(feature)
        self.assertTrue(is_enabled(feature))
        
        # Disable
        disable(feature)
        self.assertFalse(is_enabled(feature))

    def test_set_feature_state(self):
        """Test set_feature_state function."""
        feature = Feature.DYNAMIC_ALL_EVALUATION
        
        # Set to True
        set_feature_state(feature, True)
        self.assertTrue(is_enabled(feature))
        
        # Set to False
        set_feature_state(feature, False)
        self.assertFalse(is_enabled(feature))

    def test_get_all_feature_states(self):
        """Test get_all_feature_states function."""
        # Enable some features
        enable(Feature.API_BOUNDARY_DETECTION)
        enable(Feature.CHAIN_CANDIDATE_COLLECTION)
        
        # Get all states
        states = get_all_feature_states()
        
        # Verify
        self.assertEqual(len(states), len(Feature))
        self.assertTrue(states[Feature.API_BOUNDARY_DETECTION.name])
        self.assertTrue(states[Feature.CHAIN_CANDIDATE_COLLECTION.name])
        self.assertFalse(states[Feature.DYNAMIC_ALL_EVALUATION.name])

    def test_list_flags(self):
        """Test list_flags function (alias for get_all_feature_states)."""
        # Enable a feature
        enable(Feature.INCREMENTAL_WATCH_MODE)
        
        # Get states via list_flags
        states = list_flags()
        
        # Verify
        self.assertEqual(len(states), len(Feature))
        self.assertTrue(states[Feature.INCREMENTAL_WATCH_MODE.name])

    def test_environment_variable_override(self):
        """Test environment variable override."""
        feature = Feature.ADVANCED_EXPORT_HEURISTICS
        
        # Verify initially disabled
        self.assertFalse(is_enabled(feature))
        
        # Set environment variable
        os.environ[f"MAPCODOC_FEATURE_{feature.name}"] = "1"
        
        # Verify it's now enabled
        self.assertTrue(is_enabled(feature))
        
        # Override with environment variable to disable
        os.environ[f"MAPCODOC_FEATURE_{feature.name}"] = "0"
        
        # Verify it's now disabled
        self.assertFalse(is_enabled(feature))
        
        # Test alternate format
        os.environ[f"MAPCOD_ENABLE_{feature.name}"] = "yes"
        self.assertTrue(is_enabled(feature))

    def test_enable_for_test(self):
        """Test enable_for_test and restore_states functions."""
        # Get initial states (all should be disabled)
        initial_states = get_all_feature_states()
        for state in initial_states.values():
            self.assertFalse(state)
        
        # Enable features for test
        test_features = {Feature.API_BOUNDARY_DETECTION, Feature.CHAIN_CANDIDATE_COLLECTION}
        original_states = enable_for_test(test_features)
        
        # Verify features are enabled
        self.assertTrue(is_enabled(Feature.API_BOUNDARY_DETECTION))
        self.assertTrue(is_enabled(Feature.CHAIN_CANDIDATE_COLLECTION))
        
        # Restore original states
        restore_states(original_states)
        
        # Verify restored
        self.assertFalse(is_enabled(Feature.API_BOUNDARY_DETECTION))
        self.assertFalse(is_enabled(Feature.CHAIN_CANDIDATE_COLLECTION))

    @patch('code_analysis.feature_flags.registry')
    def test_registry_integration(self, mock_registry):
        """Test registry integration for feature flag changes."""
        # Setup mock registry
        mock_registry.publish_event = MagicMock()
        
        # Enable a feature
        enable(Feature.API_BOUNDARY_DETECTION)
        
        # Verify event was published
        mock_registry.publish_event.assert_called_once_with(
            'feature_flag_changed', 
            {
                'feature': Feature.API_BOUNDARY_DETECTION.name,
                'enabled': True,
                'source': 'api'
            }
        )
        
        # Reset mock
        mock_registry.publish_event.reset_mock()
        
        # Disable a feature
        disable(Feature.API_BOUNDARY_DETECTION)
        
        # Verify event was published
        mock_registry.publish_event.assert_called_once_with(
            'feature_flag_changed', 
            {
                'feature': Feature.API_BOUNDARY_DETECTION.name,
                'enabled': False,
                'source': 'api'
            }
        )

    def test_set_registry(self):
        """Test setting registry."""
        mock_registry = MagicMock()
        
        # Set registry
        set_registry(mock_registry)
        
        # Enable a feature (should trigger event)
        with patch('code_analysis.feature_flags.registry', mock_registry):
            enable(Feature.API_BOUNDARY_DETECTION)
            
            # Verify event was published
            mock_registry.publish_event.assert_called_once()


if __name__ == '__main__':
    unittest.main() 