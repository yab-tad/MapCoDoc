"""
Filesystem watcher for incremental code analysis.

Uses the 'watchdog' library to monitor file changes and trigger updates in the analysis pipeline.
"""

import logging
import time
import os
from pathlib import Path
from threading import Timer, Lock
from typing import Optional, TYPE_CHECKING, Dict, Tuple, Set, Any, List

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    # Define dummy classes if watchdog is not installed to avoid import errors elsewhere
    class Observer: pass
    class FileSystemEventHandler: pass
    class FileSystemEvent: pass
    class FileModifiedEvent: pass
    class FileCreatedEvent: pass
    class FileDeletedEvent: pass


from .config import AnalysisConfig
from .events import FILE_CREATED, FILE_MODIFIED, FILE_DELETED, WATCHER_STARTED, WATCHER_STOPPED, WATCHER_ERROR

if TYPE_CHECKING:
    from .mapcodocreg import MapCoDocRegistry

logger = logging.getLogger(__name__)

# Type alias for debounced events
DebounceKey = Tuple[str, str] # (event_type, src_path)

class ChangeEventHandler(FileSystemEventHandler):
    """Handles file system events and debounces them."""

    def __init__(self, watcher: 'FileSystemWatcher'):
        super().__init__()
        self.watcher = watcher
        self._lock = Lock()
        self._debounced_events: Dict[DebounceKey, Timer] = {}

    def _is_excluded(self, path_str: str) -> bool:
        """Check if a path should be excluded based on config."""
        # Normalize path for consistent matching
        normalized_path = os.path.normpath(path_str).replace('\\', '/')
        for pattern in self.watcher.config.exclude_patterns:
            if pattern in normalized_path:
                return True
        # Also check specific file exclusions if they exist
        if hasattr(self.watcher.config, 'exclude_files'):
             if normalized_path in getattr(self.watcher.config, 'exclude_files', []):
                 return True
        return False

    def _should_process(self, event: FileSystemEvent) -> bool:
        """Determine if an event should be processed."""
        if event.is_directory:
            return False # Ignore directory events for now
        if not event.src_path.endswith(".py"):
            return False # Only process Python files
        if self._is_excluded(event.src_path):
            logger.debug(f"Ignoring excluded path: {event.src_path}")
            return False
        return True

    def _debounce_event(self, event_type: str, src_path: str):
        """Debounce events to avoid rapid firing."""
        key: DebounceKey = (event_type, src_path)
        with self._lock:
            # Cancel existing timer for this event if it exists
            if key in self._debounced_events:
                self._debounced_events[key].cancel()

            # Schedule new timer
            timer = Timer(
                self.watcher.config.watch_debounce_delay,
                self._process_debounced_event,
                args=[event_type, src_path]
            )
            self._debounced_events[key] = timer
            timer.start()
            logger.debug(f"Debouncing event: {key} for {self.watcher.config.watch_debounce_delay}s")

    def _process_debounced_event(self, event_type: str, src_path: str):
        """Process the event after the debounce delay."""
        key: DebounceKey = (event_type, src_path)
        with self._lock:
            # Remove timer reference
            self._debounced_events.pop(key, None)

        logger.info(f"Processing debounced event: {event_type} for {src_path}")
        self.watcher.publish_file_change(event_type, src_path)

    def on_modified(self, event: FileModifiedEvent):
        if self._should_process(event):
            logger.debug(f"File modified detected: {event.src_path}")
            self._debounce_event(FILE_MODIFIED, event.src_path)

    def on_created(self, event: FileCreatedEvent):
        if self._should_process(event):
            logger.debug(f"File created detected: {event.src_path}")
            self._debounce_event(FILE_CREATED, event.src_path)

    def on_deleted(self, event: FileDeletedEvent):
        if self._should_process(event):
            logger.debug(f"File deleted detected: {event.src_path}")
            self._debounce_event(FILE_DELETED, event.src_path)

    # on_moved can be complex, handle later if needed


class FileSystemWatcher:
    """
    Watches the filesystem for changes relevant to code analysis.
    Designed to be registered as a component in MapCoDocRegistry.
    """
    COMPONENT_NAME = "watcher"
    DEPENDENCIES: Set[str] = {"config_component"}

    def __init__(self, repo_path: str, config: Optional[AnalysisConfig] = None):
        """Initialize the watcher component."""
        if not WATCHDOG_AVAILABLE:
            logger.warning("Watchdog library not available. Watcher component will be disabled.")
            self._enabled = False
            return

        self.repo_path = str(Path(repo_path).resolve())
        self.config = config or AnalysisConfig()
        self.registry: Optional['MapCoDocRegistry'] = None # Will be set by registry
        self.observer: Optional[Observer] = None
        self.event_handler = ChangeEventHandler(self)
        self._running = False
        self._enabled = True # Enabled if watchdog is available

    def set_registry(self, registry: 'MapCoDocRegistry'):
        """Set the registry instance (called by MapCoDocRegistry)."""
        self.registry = registry
        logger.info(f"{self.COMPONENT_NAME} received registry instance.")

    def initialize(self):
        """Initialize and start the watcher if enabled in config."""
        if not self._enabled:
            logger.info(f"{self.COMPONENT_NAME} is disabled (watchdog not installed).")
            return

        if self.config.enable_watch_mode:
            logger.info(f"Initializing {self.COMPONENT_NAME}...")
            self.start()
        else:
            logger.info(f"{self.COMPONENT_NAME} is disabled by configuration (enable_watch_mode=False).")

    def publish_file_change(self, event_type: str, file_path: str):
        """Publish a standardized file change event to the registry."""
        if not self.registry:
            logger.warning(f"{self.COMPONENT_NAME} cannot publish event: Registry not set.")
            return

        payload = {
            'event_type': event_type.replace('file_', ''), # 'created', 'modified', 'deleted'
            'file_path': file_path,
            'is_directory': False # We filter directories earlier
        }
        try:
            self.registry.publish_event(event_type, payload, source_component=self.COMPONENT_NAME)
            logger.info(f"Published event {event_type} for {file_path}")
        except Exception as e:
            logger.error(f"Failed to publish {event_type} event for {file_path}: {e}", exc_info=True)

    def start(self):
        """Start the filesystem watcher."""
        if not self._enabled: return
        if self._running:
            logger.warning(f"{self.COMPONENT_NAME} is already running.")
            return

        watch_path = self.repo_path # Watch the repo path provided during init
        if not Path(watch_path).is_dir():
             logger.error(f"Watch path is not a valid directory: {watch_path}")
             if self.registry:
                 self.registry.publish_event(WATCHER_ERROR, {'error': 'Invalid watch path'}, source_component=self.COMPONENT_NAME)
             return

        try:
            self.observer = Observer() # Recreate observer if stopped previously
            self.observer.schedule(self.event_handler, watch_path, recursive=True)
            self.observer.start()
            self._running = True
            logger.info(f"{self.COMPONENT_NAME} started on: {watch_path}")
            if self.registry:
                self.registry.publish_event(WATCHER_STARTED, {'path': watch_path}, source_component=self.COMPONENT_NAME)
        except Exception as e:
            logger.error(f"Failed to start {self.COMPONENT_NAME}: {e}", exc_info=True)
            if self.registry:
                self.registry.publish_event(WATCHER_ERROR, {'error': str(e)}, source_component=self.COMPONENT_NAME)


    def stop(self):
        """Stop the filesystem watcher."""
        if not self._enabled or not self._running:
            # logger.warning(f"{self.COMPONENT_NAME} is not running or disabled.")
            return # Silently return if not running or disabled

        try:
            if self.observer and self.observer.is_alive():
                self.observer.stop()
                self.observer.join() # Wait for the thread to finish
            self._running = False
            logger.info(f"{self.COMPONENT_NAME} stopped.")
            if self.registry:
                self.registry.publish_event(WATCHER_STOPPED, {}, source_component=self.COMPONENT_NAME)
        except Exception as e:
            logger.error(f"Error stopping {self.COMPONENT_NAME}: {e}", exc_info=True)
            if self.registry:
                self.registry.publish_event(WATCHER_ERROR, {'error': f'Stop error: {e}'}, source_component=self.COMPONENT_NAME)
        finally:
             self._running = False # Ensure state is updated even on error

    def cleanup(self):
        """Cleanup resources, ensuring the watcher is stopped."""
        logger.info(f"Cleaning up {self.COMPONENT_NAME}...")
        self.stop()
        self.observer = None # Release observer object

    def is_running(self) -> bool:
        """Check if the watcher is currently running."""
        return self._enabled and self._running and self.observer is not None and self.observer.is_alive()

    # --- Standard Component Methods ---
    def get_state(self) -> Dict[str, Any]:
        """Get the current state of the watcher."""
        return {
            'component_name': self.COMPONENT_NAME,
            'is_running': self.is_running(),
            'watch_path': self.repo_path,
            'enabled_by_config': self.config.enable_watch_mode,
            'watchdog_available': WATCHDOG_AVAILABLE,
            'enabled': self._enabled,
        }

    def sync_state(self, state: Dict[str, Any]) -> bool:
        """Synchronize state (not typically needed for watcher)."""
        logger.warning(f"{self.COMPONENT_NAME} does not support state synchronization.")
        return False # Watcher state is mostly runtime status

    def get_readiness_state(self) -> str:
         """Return the readiness state of the component."""
         if not self._enabled:
             return "DISABLED"
         if self.config.enable_watch_mode:
             return "READY" if self.is_running() else "INITIALIZING" # Or "STOPPED"?
         else:
             return "DISABLED" # Disabled by config

    def on_dependency_ready(self, dependency_name: str):
        """Handle dependency readiness notifications (if needed)."""
        # Currently, watcher doesn't have explicit dependencies to wait for
        pass

    def on_all_dependencies_ready(self):
         """Called when all dependencies are ready."""
         # Could potentially auto-start here if not started in initialize
         pass 