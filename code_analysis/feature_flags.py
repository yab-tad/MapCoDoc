import os
import sys
import logging
from enum import Enum, auto
from typing import Dict, Union, Optional, Set, Any, Callable, TYPE_CHECKING
from .events import FEATURE_FLAG_CHANGED

if TYPE_CHECKING:
    from .mapcodocreg import MapCoDocRegistry

logger = logging.getLogger(__name__)


class Feature(Enum):
    """Enumeration of optional / experimental features in the MapCoDoc pipeline.

    Use these toggles to switch behaviour at runtime without commenting code
    blocks out. This allows unfinished subsystems to live in the main branch
    while remaining disabled in production runs.
    """

    # Graph‑level processing
    API_BOUNDARY_DETECTION = auto()   # Enables detection of package API boundaries
    CHAIN_CANDIDATE_COLLECTION = auto()  # Collects candidates for export chains
    DYNAMIC_ALL_EVALUATION = auto()   # Controls dynamic __all__ evaluation
    CALL_GRAPH_ANALYSIS = auto()      # Enables collection of function/method call relationships
    GRAPH_ANALYSIS = auto()           # Enables the entire graph building and relationship tracking process.
    # Watch mode and incremental analysis
    INCREMENTAL_WATCH_MODE = auto()   # Enables file system watching and incremental updates
    # Advanced features
    ADVANCED_EXPORT_HEURISTICS = auto()  # Enables advanced export chain detection heuristics

# Global runtime state of each feature flag
# This dictionary stores the current state of each feature
_feature_state: Dict[Feature, bool] = {
    Feature.API_BOUNDARY_DETECTION: True,
    Feature.CHAIN_CANDIDATE_COLLECTION: True,
    Feature.DYNAMIC_ALL_EVALUATION: False,
    Feature.CALL_GRAPH_ANALYSIS: False, # Default to OFF for performance
    Feature.GRAPH_ANALYSIS: False, # Default to OFF for fallback and other features
    Feature.INCREMENTAL_WATCH_MODE: False,
    Feature.ADVANCED_EXPORT_HEURISTICS: True,
}

# Global registry instance (optional)
registry: Optional['MapCoDocRegistry'] = None

def set_registry(reg: Optional['MapCoDocRegistry']):
    """Set the registry instance for event publishing."""
    global registry
    registry = reg
    logger.info(f"Feature flag system registry set to: {type(reg).__name__ if reg else 'None'}")

def _get_env_var(feature: Feature) -> Optional[str]:
    """Check environment variables for feature flag overrides."""
    # Check primary format: MAPCODOC_FEATURE_FEATURE_NAME
    env_key1 = f"MAPCODOC_FEATURE_{feature.name}"
    value = os.environ.get(env_key1)
    if value is not None:
        logger.debug(f"Found env var {env_key1}={value}")
        return value

    # Check alternate format: MAPCOD_ENABLE_FEATURE_NAME
    env_key2 = f"MAPCOD_ENABLE_{feature.name}"
    value = os.environ.get(env_key2)
    if value is not None:
        logger.debug(f"Found env var {env_key2}={value}")
        return value

    return None

def _parse_env_var(value: Optional[str]) -> Optional[bool]:
    """Parse environment variable string to boolean."""
    if value is None:
        return None
    value_lower = value.strip().lower()
    if value_lower in ('1', 'true', 'yes', 'on'):
        return True
    if value_lower in ('0', 'false', 'no', 'off'):
        return False
    logger.warning(f"Could not parse environment variable value '{value}' as boolean.")
    return None

def _publish_feature_change_event(feature: Feature, enabled: bool, source: str):
    """Publish an event when a feature flag changes."""
    if registry:
        try:
            # --- Use event constant and pass source ---
            event_data = {
                'feature': feature.name,
                'enabled': enabled,
                'source': source # e.g., 'env', 'api'
            }
            registry.publish_event(FEATURE_FLAG_CHANGED, event_data, source_component='FeatureFlags')
            # -----------------------------------------
        except Exception as e:
            logger.error(f"Failed to publish feature flag change event: {e}", exc_info=True)

def is_enabled(feature: Feature) -> bool:
    """Check if a feature flag is enabled, considering environment variables."""
    # Check environment variable override first
    env_value = _get_env_var(feature)
    parsed_value = _parse_env_var(env_value)
    if parsed_value is not None:
        # If env var is set and valid, it overrides the internal state
        # Check if the state needs updating and publish event if changed
        current_state = _feature_state.get(feature, False)
        if parsed_value != current_state:
            logger.info(f"Feature {feature.name} state overridden by environment variable to {parsed_value}.")
            set_feature_state(feature, parsed_value, source='env') # Update internal state and publish
        return parsed_value

    # Fallback to internal state
    return _feature_state.get(feature, False)

def enable(feature: Feature):
    """Enable a feature flag."""
    set_feature_state(feature, True, source='api')

def disable(feature: Feature):
    """Disable a feature flag."""
    set_feature_state(feature, False, source='api')

def set_feature_state(feature: Feature, enabled: bool, source: str = 'api'):
    """Set the state of a feature flag and publish an event."""
    current_state = _feature_state.get(feature)
    if current_state != enabled:
        _feature_state[feature] = enabled
        logger.info(f"Feature {feature.name} set to {enabled} via {source}.")
        _publish_feature_change_event(feature, enabled, source)
    else:
        logger.debug(f"Feature {feature.name} already set to {enabled}.")

def get_all_feature_states() -> Dict[str, bool]:
    """Get the current state of all features.
    
    Returns:
        Dictionary mapping feature names to their current state
    """
    return {feature.name: is_enabled(feature) for feature in Feature}

def list_flags() -> Dict[str, bool]:
    """Return a copy of the current flag status mapping.
    
    Alias for get_all_feature_states() for backwards compatibility.
    
    Returns:
        Dictionary mapping feature names to their current state
    """
    return get_all_feature_states()

def enable_for_test(features: Set[Feature]) -> Dict[Feature, bool]:
    """Enable features temporarily for testing, returning original states.
    
    This is useful in test functions to enable features temporarily and
    restore them afterwards.
    
    Args:
        features: Set of features to enable
        
    Returns:
        Dictionary mapping features to their original states
    """
    original_states = {feature: is_enabled(feature) for feature in features}
    for feature in features:
        enable(feature)
    return original_states

def restore_states(states: Dict[Feature, bool]) -> None:
    """Restore feature states to their original values after testing.
    
    Args:
        states: Dictionary mapping features to their original states
    """
    for feature, state in states.items():
        set_feature_state(feature, state)

# Backward compatibility aliases
enable_feature = enable
disable_feature = disable
