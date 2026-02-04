"""
Defines standard event names and payload structures for the MapCoDoc system.
"""

from typing import TypedDict, Any, Optional, Dict, TYPE_CHECKING
import datetime
import time
import logging

# Avoid circular import if MapCoDocRegistry needs events
if TYPE_CHECKING:
    from .mapcodocreg import MapCoDocRegistry

logger = logging.getLogger(__name__)

# --- Standard Event Names ---

# Definition Registry Events
DEFINITION_REGISTERED = "definition_registered"
DEFINITION_UPDATED = "definition_updated"
DEFINITION_REMOVED = "definition_removed" # Example: If definitions can be removed

# API Resolver Events
API_PATH_RESOLVED = "api_path_resolved"
API_PATH_RESOLUTION_FAILED = "api_path_resolution_failed"
API_BOUNDARIES_UPDATED = "api_boundaries_updated"
EXPORT_CHAINS_BUILT = "export_chains_built"
CHAIN_CANDIDATES_UPDATED = "chain_candidates_updated"
API_MAP_UPDATED = "api_map_updated"

# Analyzer Integration Events
FILE_ANALYSIS_STARTED = "file_analysis_started"
FILE_ANALYSIS_COMPLETED = "file_analysis_completed"
FILE_ANALYSIS_FAILED = "file_analysis_failed"
CODEBASE_ANALYSIS_STARTED = "codebase_analysis_started"
CODEBASE_ANALYSIS_COMPLETED = "codebase_analysis_completed"
MODULE_CACHE_UPDATED = "module_cache_updated"
MODULE_ANALYSIS_INVALIDATED = "module_analysis_invalidated" # For incremental updates
MODULE_ANALYSIS_UPDATED = "module_analysis_updated" # For incremental updates

# Cache Events
CACHE_HIT = "cache_hit"
CACHE_MISS = "cache_miss"
CACHE_UPDATED = "cache_updated"
CACHE_CLEARED = "cache_cleared"

# Configuration Events
CONFIG_LOADED = "config_loaded"
CONFIG_UPDATED = "config_updated"

# Feature Flag Events
FEATURE_FLAG_CHANGED = "feature_flag_changed"

# General Registry Events
REGISTRY_COMPONENT_REGISTERED = "registry_component_registered"
REGISTRY_COMPONENT_UNREGISTERED = "registry_component_unregistered" 
REGISTRY_STATE_SYNCED = "registry_state_synced"
DEPENDENCY_READY = "dependency_ready"

# --- Watcher Events ---
# File System Events (published by Watcher, consumed by AnalyzerIntegration)
FILE_CREATED = "file_created"
FILE_MODIFIED = "file_modified"
FILE_DELETED = "file_deleted"
WATCHER_STARTED = "watcher_started"
WATCHER_STOPPED = "watcher_stopped"
WATCHER_ERROR = "watcher_error"
# --------------------

# --- IR Events ---
IR_MODULE_GENERATED = "ir_module_generated" # Payload: {'module_name': str, 'source_file': str, 'ir_module_summary': Dict}
IR_MODULE_UPDATED = "ir_module_updated"   # Similar payload
IR_CACHE_INVALIDATED = "ir_cache_invalidated" # Payload: {'module_name': str} or {'scope': 'all'}


# --- Standard Event Payload Structure ---

class EventPayload(TypedDict):
    """
    Standard structure for data published with events.
    """
    timestamp: str  # ISO 8601 format string
    source_component: str # Name of the component class publishing the event
    event_specific_data: Dict[str, Any] # Data specific to this event type
    transaction_id: Optional[str] # Optional transaction ID if applicable 

# --- Event Publishing Helper ---

def publish_event(registry: Optional['MapCoDocRegistry'],
                  event_name: str,
                  event_data: Dict[str, Any],
                  source_component: Optional[str] = "unknown") -> None:
    """
    Helper function to create payload and publish an event via the registry.

    Args:
        registry: The MapCoDocRegistry instance (or None if unavailable).
        event_name: The name of the event (use constants defined above).
        event_data: The dictionary containing event-specific data.
        source_component: The name of the component publishing the event.
    """
    if not registry:
        logger.debug(f"Registry not available, cannot publish event: {event_name}")
        return

    payload: EventPayload = {
        "timestamp": time.time(),
        "source_component": source_component,
        "event_specific_data": event_data
    }
    try:
        # Assuming the registry object has a method like this
        if hasattr(registry, 'publish_event') and callable(registry.publish_event):
             registry.publish_event(event_name, payload)
             # logger.debug(f"Event '{event_name}' published by {source_component}.") # Can be noisy
        else:
             logger.warning(f"Registry object provided to publish_event lacks a 'publish_event' method.")
    except Exception as e:
        logger.error(f"Error publishing event '{event_name}' from {source_component}: {e}", exc_info=True)