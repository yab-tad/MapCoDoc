"""
MapCoDocRegistry module for centralized service registry.
Provides cross-module integration and shared access to key services.
"""

import time
import logging
import datetime
import threading
from pathlib import Path
from enum import Enum, auto
from collections import defaultdict, deque
from typing import Dict, Any, Optional, Union, Tuple, Set, List, Protocol, runtime_checkable, Callable, TYPE_CHECKING

from .config import AnalysisConfig
from .project_metadata import extract_project_metadata
from .feature_flags import Feature, is_enabled
from .api_resolver import APIPathResolver
from .graph.store import GraphStore
from .definition_registry import DefinitionRegistry
from .analyzers.analyzer_integration import AnalyzerIntegration
from .watcher import FileSystemWatcher
from .events import (
    REGISTRY_COMPONENT_REGISTERED,
    REGISTRY_COMPONENT_UNREGISTERED,
    REGISTRY_STATE_SYNCED,
    DEPENDENCY_READY,
    EventPayload
)

# Setup logging
logger = logging.getLogger(__name__)


@runtime_checkable
class ComponentInterface(Protocol):
    """Base interface that all registry components must implement."""
    def get_state(self) -> Dict[str, Any]:
        """Get component state for synchronization."""
        ...
        
    def sync_state(self, state: Dict[str, Any]) -> bool:
        """Synchronize state with other components."""
        ...

    def on_dependency_ready(self, dependency_name: str) -> None:
        """Called when a dependency becomes ready."""
        ...


@runtime_checkable
class RegisterableComponent(ComponentInterface, Protocol):
    """Interface for components that can be registered with the registry."""
    
    DEPENDENCIES: Set[str]
    
    def initialize(self) -> None:
        """Initialize the component after registration."""
        ...


class DefinitionProviderInterface(ComponentInterface, Protocol):
    """Interface for components that provide definitions."""
    def get_definition_module(self, name: str, context_module: Optional[str] = None) -> Optional[str]:
        """Get the module where a component is defined."""
        ...
        
    def get_all_definition_modules(self, name: str) -> List[str]:
        """Get all modules where a component is defined."""
        ...
        
    def register_definition(self,
                            fully_qualified_name: str,
                            component_type: str,
                            line_number: int,
                            ast_node: Any = None,
                            source_file: Optional[str] = None,
                            confidence: float = 1.0,
                            metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Register a definition with the registry."""
        ...


class PathResolverInterface(ComponentInterface, Protocol):
    """Interface for components that resolve API paths."""
    def resolve_api_path(self, component_path: str, collect_all: bool = False) -> Union[str, List[str]]:
        """Resolve a component path to its public API path."""
        ...
        
    def build_export_chains(self) -> None:
        """Build an export chains for components."""
        ...
        
    def set_chain_candidates(self, candidates: Set[str], source: str = "unknown") -> None:
        """Set the components that need export chains built."""
        ...


@runtime_checkable
class RegistryComponent(Protocol):
    """Interface for components that can be registered with the registry."""
    
    COMPONENT_NAME: str
    DEPENDENCIES: Set[str]
    
    def initialize(self) -> None:
        """Initialize the component after registration."""
        ...
        
    def get_state(self) -> Dict[str, Any]:
        """Get component state for synchronization."""
        ...
        
    def sync_state(self, state: Dict[str, Any]) -> bool:
        """Synchronize state with other components."""
        ...
        
    def cleanup(self) -> None:
        """Clean up resources before shutdown."""
        ...


class RegistryValidator:
    """Validates that components implement required interfaces."""
    
    @staticmethod
    def validate_component(component_name: str, component: Any) -> List[str]:
        """
        Validate that a component implements the required interface.
        
        Args:
            component_name: Name of the component
            component: Component instance
            
        Returns:
            List of validation errors, empty if valid
        """
        errors = []
        
        # Validate basic ComponentInterface
        if not isinstance(component, ComponentInterface):
            methods = ['get_state', 'sync_state', 'on_dependency_ready']
            for method in methods:
                if not hasattr(component, method) or not callable(getattr(component, method)):
                    errors.append(f"{component_name} missing required method: {method}")
            if not hasattr(component, 'COMPONENT_NAME'):
                errors.append(f"{component_name} missing required attribute: COMPONENT_NAME")
        
        # Check if it's a registerable component (most components are)
        if not isinstance(component, RegisterableComponent):
            methods = ['initialize']
            for method in methods:
                if not hasattr(component, method) or not callable(getattr(component, method)):
                    errors.append(f"{component_name} missing required method: {method}")
            if not hasattr(component, 'DEPENDENCIES'):
                errors.append(f"{component_name} missing required attribute: DEPENDENCIES")

        # Validate component-specific interfaces based on name (adjust as needed)
        if component_name == 'definition_registry':
            # Methods required for DefinitionProviderInterface
            required_methods = ['get_definition_module', 'register_definition', 'get_module_definitions', 'get_all_definition_modules']
            for method in required_methods:
                if not hasattr(component, method) or not callable(getattr(component, method)):
                    errors.append(f"{component_name} missing required method: {method}")

        # --- Validation for individual trackers (Import, Inheritance, Call) or AnalyzerIntegration can be added if needed ---
        elif component_name == 'analyzer_integration':
            # Check methods expected from AnalyzerIntegration
            required_methods = ['analyze_codebase', 'analyze_file', 'get_analysis_result', 'get_imports', 'get_inheritance', 'get_calls']
            for method in required_methods:
                if not hasattr(component, method) or not callable(getattr(component, method)):
                    errors.append(f"{component_name} missing required method: {method}")

        elif component_name == 'api_resolver':
            # Methods required for PathResolverInterface
            required_methods = ['resolve_api_path', 'build_export_chains', 'set_chain_candidates']
            for method in required_methods:
                if not hasattr(component, method) or not callable(getattr(component, method)):
                    errors.append(f"{component_name} missing required method: {method}")

        return errors


class ComponentState(Enum):
    """Component states for better lifecycle management."""
    UNINITIALIZED = auto()
    INITIALIZING = auto()
    READY = auto()
    ERROR = auto()
    

class ComponentEventBus:
    """
    Event bus for inter-component communication.
    Provides a standardized way for components to send events and subscribe to them.
    """
    
    def __init__(self):
        self.subscribers = defaultdict(list)
        self.event_log = []
        self.max_log_size = 100
        self._active_events = set()
        self._recursion_depth = defaultdict(int)
        self._max_recursion_depth = 10
    
    def subscribe(self, event_type: str, callback: callable):
        """
        Subscribe to an event type.
        
        Args:
            event_type: Type of event to subscribe to
            callback: Function to call when event occurs
            
        Returns:
            Unsubscribe function to cancel subscription
        """
        self.subscribers[event_type].append(callback)
        return lambda: self.unsubscribe(event_type, callback)
    
    def unsubscribe(self, event_type: str, callback: callable):
        """
        Unsubscribe from an event type.
        
        Args:
            event_type: Type of event
            callback: Callback to remove
        """
        if event_type in self.subscribers and callback in self.subscribers[event_type]:
            self.subscribers[event_type].remove(callback)
    
    def publish(self, event_type: str, payload: EventPayload):
        """
        Publish an event to all subscribers with recursion protection.

        Args:
            event_type: Type of event
            payload: Standardized EventPayload dictionary
        """
        # Create a unique event identifier for tracking recursion
        # Use timestamp + source + event_type for better uniqueness
        event_id = f"{payload.get('timestamp', '')}:{payload.get('source_component', '')}:{event_type}"

        # Check if we're already processing this exact event
        if event_id in self._active_events:
            logger.warning(f"Skipping duplicate event: {event_type} from {payload.get('source_component')}")
            return

        # Check recursion depth for this event type
        current_depth = self._recursion_depth.get(event_type, 0)
        if current_depth >= self._max_recursion_depth:
            logger.error(f"Maximum recursion depth exceeded for event: {event_type}")
            return

        # Increment recursion depth and track active event
        self._recursion_depth[event_type] = current_depth + 1
        self._active_events.add(event_id)

        try:
            # Add event to log with timestamp
            self.event_log.append({
                'event_type': event_type,
                'timestamp': payload.get('timestamp'),
                'source': payload.get('source_component'),
                'data_summary': str(payload.get('event_specific_data', {}))[:100]
            })

            # Trim log if needed
            if len(self.event_log) > self.max_log_size:
                self.event_log = self.event_log[-self.max_log_size:]

            # Call subscribers
            for callback in self.subscribers.get(event_type, []):
                try:
                    callback(payload) # Pass the full standard payload
                except Exception as e:
                    logger.error(f"Error in event handler for {event_type}: {e}", exc_info=True)
        finally:
            # Decrement recursion depth and remove from active events
            self._recursion_depth[event_type] = current_depth
            if event_id in self._active_events:
                self._active_events.remove(event_id)
    
    def get_event_log(self):
        """Get the event log."""
        return self.event_log



class DependencyGraph:
    """Manages component dependencies as a directed acyclic graph."""
    
    def __init__(self):
        self.dependencies: Dict[str, Set[str]] = {}  # component -> dependencies
        self.dependents: Dict[str, Set[str]] = defaultdict(set)    # component -> dependents
        self.state: Dict[str, ComponentState] = {}         # component -> state
    
    def add_component(self, component_name: str, dependencies: List[str]):
        """Add a component with its dependencies to the graph."""
        self.dependencies[component_name] = set(dependencies)
        self.state[component_name] = ComponentState.UNINITIALIZED

        # Update dependents
        for dep in dependencies:
            self.dependents[dep].add(component_name)
    
    def mark_ready(self, component_name: str) -> Set[str]:
        """
        Mark a component as ready and return list of components
        that should now be initialized.
        """
        if component_name not in self.state:
            return set()

        self.state[component_name] = ComponentState.READY

        # Find components that might now be ready
        ready_to_init = set()

        if component_name in self.dependents:
            for dependent in self.dependents[component_name]:
                # Check if all dependencies are ready
                if dependent in self.dependencies:
                    if all(self.state.get(dep, ComponentState.UNINITIALIZED) == ComponentState.READY for dep in self.dependencies[dependent]):
                        # Check if the dependent itself is not already ready or initializing
                        if self.state.get(dependent, ComponentState.UNINITIALIZED) == ComponentState.UNINITIALIZED:
                            ready_to_init.add(dependent)

        return ready_to_init
    
    
    def is_acyclic(self) -> bool:
        """Check if the dependency graph is acyclic."""
        path = set()
        visiting = set()
        visited = set()

        def visit(node):
            visited.add(node)
            visiting.add(node)
            path.add(node)
            for neighbour in self.dependents.get(node, set()):
                if neighbour not in visited:
                    if visit(neighbour):
                        return True
                elif neighbour in visiting:
                    return True # Cycle detected
            path.remove(node)
            visiting.remove(node)
            return False

        nodes = list(self.dependencies.keys()) + list(self.dependents.keys())
        for node in set(nodes):
            if node not in visited:
                if visit(node):
                    logger.error(f"Dependency cycle detected involving node: {node}")
                    return False
        return True
    
    def get_initialization_order(self) -> List[str]:
        """Get the order in which components should be initialized."""
        
        if not self.is_acyclic():
            raise ValueError("Dependency graph has cycles, cannot determine initialization order")

        # Kahn's algorithm for topological sort
        in_degree = {node: 0 for node in self.dependencies}
        for node in self.dependencies:
            for dep in self.dependencies[node]:
                if dep in in_degree:
                    in_degree[dep] = in_degree.get(dep, 0) + 1 # Should be dependent count? Let's reverse logic.

        # Corrected Kahn's algorithm logic
        # Calculate in-degrees (number of dependencies for each node)
        in_degree = {node: 0 for node in self.dependencies}
        all_nodes = set(self.dependencies.keys())
        for u in self.dependencies:
            for v in self.dependencies[u]:
                all_nodes.add(v)
                in_degree[u] = in_degree.get(u, 0) + 1 # Node u depends on v

        # Re-calculate in-degree based on dependents
        in_degree = {node: 0 for node in all_nodes}
        for node in all_nodes:
            if node in self.dependencies:
                for dep in self.dependencies[node]:
                    if dep in self.dependents: # Check if dep is a node in the graph
                        in_degree[node] += 1 # This seems wrong. Let's use the standard definition.

        # Standard Kahn's: Calculate in-degree (number of incoming edges)
        in_degree = {node: 0 for node in all_nodes}
        for node in all_nodes:
            if node in self.dependents:
                for dependent in self.dependents[node]:
                    if dependent in in_degree:
                        in_degree[dependent] += 1

        # Queue of nodes with in-degree 0
        queue = deque([node for node in all_nodes if in_degree.get(node, 0) == 0])
        result = []

        while queue:
            u = queue.popleft()
            result.append(u)

            # For each neighbor v of u
            if u in self.dependents:
                # Sort dependents alphabetically for deterministic order
                sorted_dependents = sorted(list(self.dependents[u]))
                for v in sorted_dependents:
                    if v in in_degree:
                        in_degree[v] -= 1
                        if in_degree[v] == 0:
                            queue.append(v)

        if len(result) != len(all_nodes):
            # This indicates a cycle, which should have been caught by is_acyclic
            logger.error("Cycle detected during topological sort, though is_acyclic passed.")
            # Find missing nodes to help debug
            missing_nodes = all_nodes - set(result)
            logger.error(f"Nodes involved in cycle or missing: {missing_nodes}")
            raise ValueError("Dependency graph has cycles, cannot determine initialization order (Kahn's algorithm failed).")

        return result


class ConfigComponentWrapper:
    COMPONENT_NAME = "config_component"
    DEPENDENCIES: Set[str] = set()

    def __init__(self, config: 'AnalysisConfig', registry: Optional['MapCoDocRegistry'] = None):
        self.config = config
        self.registry = registry # Optional, for consistency or future event publishing
        self._initialized = False
        logger.info(f"{self.COMPONENT_NAME} created with config id: {id(self.config)}")

    def initialize(self) -> None:
        self._initialized = True
        logger.info(f"{self.COMPONENT_NAME} initialized by registry.")

    def get_state(self) -> Dict[str, Any]:
        # Return a serializable version of the config or relevant parts
        try:
            # Assuming AnalysisConfig is a Pydantic model or has a .model_dump() or similar
            if hasattr(self.config, 'model_dump'):
                return {"config_data": self.config.model_dump()}
            # Fallback: convert to dict if possible, otherwise just indicate readiness
            return {"config_data": self.config.__dict__ if hasattr(self.config, '__dict__') else str(self.config) }
        except Exception as e:
            logger.error(f"Error getting state for {self.COMPONENT_NAME}: {e}")
            return {"error": "Failed to serialize config"}


    def sync_state(self, state: Dict[str, Any]) -> bool:
        # Config is usually read-only after init for many components,
        # but this could allow dynamic updates if designed for it.
        logger.warning(f"{self.COMPONENT_NAME} received sync_state. Config updates not implemented via sync.")
        return True # Indicate no error

    def on_dependency_ready(self, dependency_name: str) -> None:
        pass # No dependencies

    def cleanup(self) -> None:
        logger.info(f"{self.COMPONENT_NAME} cleanup.")
        pass

    def get_config(self) -> 'AnalysisConfig':
        return self.config


class MapCoDocRegistry:
    """
    Central registry service for all MapCoDoc components with event-based integration.
    Provides shared access to core services and manages component dependencies.
    """
    
    def __init__(self,
                 repo_path: Optional[str] = None,
                 config: Optional['AnalysisConfig'] = None,
                 initial_state: Optional[Dict[str, Any]] = None,
                 auto_initialize: bool = True):
        """
        Initialize the registry.

        Args:
            repo_path: The absolute path to the root of the repository being analyzed.
            config: Optional AnalysisConfig object.
            initial_state: Optional dictionary to initialize component states.
            auto_initialize: Whether to automatically initialize default components.
        """
        # Store the configuration
        self.repo_path: Optional[Path] = None
        self.config: Optional[AnalysisConfig] = config or AnalysisConfig()

        if repo_path:
            try:
                self.repo_path = Path(repo_path).resolve(strict=True) # Check existence
                self.config.repo_path = self.repo_path
                logger.info(f"Registry initialized with repo_path: {self.repo_path}")
            except FileNotFoundError:
                logger.error(f"Registry initialization: repo_path '{repo_path}' not found.")
                self.repo_path = None # Or raise error
            except Exception as e:
                logger.error(f"Registry initialization: Error resolving repo_path '{repo_path}': {e}")
                self.repo_path = None
        
        # if not self.config: # If no config passed, create a default one
        #     logger.warning("Registry initialized without a specific config, using default AnalysisConfig.")
        #     self.config = AnalysisConfig()
        
        # Store repo_path from config if not already set by direct argument
        # and if config has a repo_path that's valid.
        if not self.repo_path and hasattr(self.config, 'repo_path') and getattr(self.config, 'repo_path', None):
            try:
                config_repo_p = Path(self.config.repo_path).resolve(strict=True)
                self.repo_path = config_repo_p
                logger.info(f"Registry repo_path set from AnalysisConfig: {self.repo_path}")
            except FileNotFoundError:
                logger.error(f"Registry: repo_path '{self.config.repo_path}' from config not found.")
            except Exception as e:
                logger.error(f"Registry: Error resolving repo_path '{self.config.repo_path}' from config: {e}")
        
        # Project/library metadata
        self.project_metadata: Dict[str, Optional[str]] = {
            "name": None,
            "version": None,
            "source": None,
        }
        if self.repo_path:
            self._load_project_metadata()
        
        # Core components dictionary
        self._components: Dict[str, Any] = {}

        # Event bus for component communication
        self.event_bus = ComponentEventBus()

        # Register standard events (can be expanded)
        self.event_types = {
            REGISTRY_COMPONENT_REGISTERED: 'When a component is registered',
            REGISTRY_COMPONENT_UNREGISTERED: 'When a component is unregistered',
            REGISTRY_STATE_SYNCED: 'When registry state is synced',
            DEPENDENCY_READY: 'When a dependency component becomes ready',
            # Add other core events like MODULE_ANALYSIS_UPDATED, API_MAP_UPDATED etc.
            # These are defined in events.py but good to list known types here.
        }

        # Shared data structures - Centralized state (managed by VersionedState)
        # self._shared_state = VersionedState(initial_state or {})
        self._component_local_versions: Dict[str, int] = defaultdict(int) # component -> version map

        # Component dependency management
        self._dependency_graph = DependencyGraph()
        self._component_creation_order: List[str] = [] # Order components were registered
        self._initialization_order: List[str] = [] # Order components were successfully initialized
        self._component_readiness: Dict[str, ComponentState] = {} # Track readiness state

        # # Transaction management
        # self.transaction_manager = TransactionManager(self)

        # Integrity tracking
        self._last_integrity_check: Optional[float] = None
        self._integrity_check_interval: float = 60  # check every 60 seconds
        self._integrity_status: Dict[str, Any] = {
            'last_check': None,
            'status': None,
            'issues': []
        }

        # # Path verification service
        # self.path_verification_service = PathVerificationService(self)

        # # Synchronization checkpoints
        # self.checkpoints = {
        #     'definition_identification': SynchronizationCheckpoint(
        #         'definition_identification',
        #         ['definition_registry']
        #     ),
        #     'analysis_complete': SynchronizationCheckpoint(
        #         'analysis_complete',
        #         ['definition_registry', 'analyzer_integration'] # Check after analysis run
        #     ),
        #     'api_resolution': SynchronizationCheckpoint(
        #         'api_resolution',
        #         ['definition_registry', 'analyzer_integration', 'api_resolver']
        #     ),
        # }

        # Event processing queue and thread (optional, for async handling)
        self._subscribers: Dict[str, List[Callable[[EventPayload], None]]] = defaultdict(list)
        # self._event_queue = queue.Queue()
        # self._processing_event = threading.Event()
        # self._shutdown_event = threading.Event()
        self._lock = threading.RLock() # Lock for thread safety if using threads

        logger.info(f"MapCoDocRegistry initialized. Auto-initialize: {auto_initialize}")

        if auto_initialize:
            self._initialize_default_components(str(self.repo_path), self.config)
            self.initialize_components() # Initialize all registered in order
        else:
            logger.info("Skipping automatic default component initialization.")

        # # Process any initial state provided via VersionedState
        # if initial_state:
        #     logger.info("Applying initial state to VersionedState.")
        #     # No direct sync needed here, VersionedState handles initial state


    def _load_project_metadata(self) -> None:
        """Load project/library metadata from the repository.
        
        Uses user-provided values from config if available, otherwise falls back
        to auto-detection from repository packaging files.
        """
        if not self.repo_path:
            return
        
        # Check for user-provided overrides in config
        user_name = self.config.project_name if self.config else None
        user_version = self.config.project_version if self.config else None
        
        try:
            # Always run auto-detection to get whatever we can
            meta = extract_project_metadata(str(self.repo_path))
            
            # Use user-provided values if available, otherwise use auto-detected
            final_name = user_name if user_name else meta.get("name")
            final_version = user_version if user_version else meta.get("version")
            
            # Build source description
            sources = []
            if user_name:
                sources.append("name:user-provided")
            elif meta.get("name"):
                # Extract just the name source from the combined source string
                meta_source = meta.get("source", "")
                name_source = next((s.split(":")[1] for s in meta_source.split(", ") if s.startswith("name:")), "auto-detected")
                sources.append(f"name:{name_source}")
            
            if user_version:
                sources.append("version:user-provided")
            elif meta.get("version"):
                meta_source = meta.get("source", "")
                version_source = next((s.split(":")[1] for s in meta_source.split(", ") if s.startswith("version:")), "auto-detected")
                sources.append(f"version:{version_source}")
            
            self.project_metadata.update({
                "name": final_name,
                "version": final_version,
                "source": ", ".join(sources) if sources else None,
            })
            
            logger.info(
                "Project metadata: name=%r version=%r (source=%s)",
                self.project_metadata.get("name"),
                self.project_metadata.get("version"),
                self.project_metadata.get("source"),
            )
            
            if user_name:
                logger.info("Using user-provided project name: %r", user_name)
            if user_version:
                logger.info("Using user-provided project version: %r", user_version)
                
        except Exception as exc:
            logger.warning("Unable to extract project metadata: %s", exc)
            # If auto-detection fails but user provided values, use them
            if user_name or user_version:
                self.project_metadata.update({
                    "name": user_name,
                    "version": user_version,
                    "source": "user-provided",
                })
                logger.info(
                    "Using user-provided metadata after auto-detection failure: name=%r version=%r",
                    user_name, user_version
                )

    def get_project_metadata(self) -> Dict[str, Optional[str]]:
        """Return a copy of the repository metadata discovered at initialization."""
        return dict(self.project_metadata)
    
    
    def _initialize_default_components(self, repo_path_str_for_components: Optional[str], config_for_components: AnalysisConfig):
        logger.info("Initializing default registry components sequence...")
        try:
            # This config is what components will use if they fetch/are given one.
            # It should be the one the registry itself is configured with.
            effective_config = config_for_components 
            if not effective_config: # Should not happen if __init__ sets self.config
                logger.error("Registry's effective_config is None in _initialize_default_components. This is an issue.")
                effective_config = AnalysisConfig() # Last resort

            logger.info(f"Registry initialized with repo_path: {repo_path_str_for_components}")
            
            # 0. Configuration Component (special, used by others during their init)
            self.config = config_for_components # Keep a direct reference to the main config
            config_comp = ConfigComponentWrapper(config=self.config, registry=self)
            self.register(ConfigComponentWrapper.COMPONENT_NAME, config_comp) 
            logger.info(f"config_component created with config id: {id(self.config)}")
            
            # 1. Graph Store (Core graph data structure)
            # Ensure self.store is initialized as GraphStore.
            # If AnalyzerIntegration or other components also create it, ensure it's the same instance or coordinated.
            graph_store_instance = None
            if is_enabled(Feature.GRAPH_ANALYSIS):
                logger.info("GRAPH_ANALYSIS is enabled. Initializing and registering graph components.")
                graph_store_instance = GraphStore()
                # Register the store so other components can declare it as a dependency
                self.register(GraphStore.COMPONENT_NAME, graph_store_instance)
            else:
                logger.info("GRAPH_ANALYSIS is disabled. Skipping graph component initialization.")
            
            # 2. Definition Registry
            # Check by its defined COMPONENT_NAME attribute
            def_reg = DefinitionRegistry(registry=self)
            self.register(DefinitionRegistry.COMPONENT_NAME, def_reg)
            
            # 3. Analyzer Integration
            analyzer_integration = AnalyzerIntegration(registry=self, config=self.config, definition_registry=def_reg)
            self.register(AnalyzerIntegration.COMPONENT_NAME, analyzer_integration)
            
            # 4. API Resolver (depends on config, definition_registry, and analyzer_integration to get traversal/export trackers)
            api_resolver = APIPathResolver(registry=self, config=self.config, definition_registry=def_reg)
            self.register(APIPathResolver.COMPONENT_NAME, api_resolver)
            
            # 5. FileSystemWatcher
            if is_enabled(Feature.INCREMENTAL_WATCH_MODE) and self.config and self.config.enable_watch_mode:
                # Ensure watcher also uses the registry for config and event bus
                self.watcher = FileSystemWatcher(config=self.config, registry=self)
                self.register(FileSystemWatcher.COMPONENT_NAME, self.watcher)
            else:
                self.watcher = None
            
            # if effective_config.enable_watch_mode:
            #     if FileSystemWatcher.COMPONENT_NAME not in self._components: # Assuming it has COMPONENT_NAME
            #         if not repo_path_str_for_components: # Uses the path passed for components
            #             logger.error("Cannot init FileSystemWatcher: repo_path not available.")
            #         else:
            #             # Watcher might need config too.
            #             watcher_config = effective_config
            #             config_comp_inst = self.get_component(ConfigComponentWrapper.COMPONENT_NAME)
            #             if config_comp_inst and hasattr(config_comp_inst, 'get_config'):
            #                 watcher_config = config_comp_inst.get_config()
            #             elif config_comp_inst : # if it's the config object itself
            #                 watcher_config = config_comp_inst

            #             watcher = FileSystemWatcher(repo_path_str_for_components, self, watcher_config)
            #             self.register(FileSystemWatcher.COMPONENT_NAME, watcher)

            logger.info("Default components registration process completed.")
        except NameError as ne: # Catches if a component ClassName is used before import
            logger.critical(f"NameError during default component registration: {ne}. Check imports.", exc_info=True)
        except Exception as e:
            logger.error(f"Error during default components registration sequence: {e}", exc_info=True)


    def initialize_components(self):
        """Initialize all registered components in dependency order."""
        logger.info("Initializing all registered components...")
        try:
            init_order = self._dependency_graph.get_initialization_order()
            logger.debug(f"Determined initialization order: {init_order}")
            for component_name in init_order:
                component = self.get_component(component_name)
                if component:
                    self._initialize_single_component(component_name)
                # if component_name in self._components:
                #     self.get_component
                #     # Check if already initialized
                #     if self._component_readiness.get(component_name) != ComponentState.READY:
                #         self._initialize_single_component(component_name)
                else:
                    logger.warning(f"Component '{component_name}' found in init order but not registered.")
            logger.info("Component initialization complete.")
        except ValueError as e:
             logger.error(f"Cannot initialize components: {e}") # Cycle detected
        except Exception as e:
             logger.error(f"Error during component initialization: {e}", exc_info=True)


    def _initialize_single_component(self, component_name: str):
        """Initialize a single component if its dependencies are ready."""
        if component_name not in self._components:
            logger.warning(f"Attempted to initialize unregistered component: {component_name}")
            return False
        if self._component_readiness.get(component_name) == ComponentState.READY:
            # logger.debug(f"Component {component_name} already initialized.")
            return True # Already ready

        component = self._components[component_name]
        dependencies = getattr(component, 'DEPENDENCIES', set())

        # Check if all dependencies are ready
        deps_ready = True
        missing_deps = []
        for dep in dependencies:
            if self._component_readiness.get(dep) != ComponentState.READY:
                deps_ready = False
                missing_deps.append(dep)

        if deps_ready:
            logger.info(f"Initializing component: {component_name}")
            self._component_readiness[component_name] = ComponentState.INITIALIZING
            try:
                if hasattr(component, 'initialize'):
                    component.initialize()
                self._component_readiness[component_name] = ComponentState.READY
                self._initialization_order.append(component_name)
                logger.info(f"Component {component_name} initialized successfully.")
                # Notify dependents that this component is ready
                self.notify_dependency_ready(component_name)
                return True
            except Exception as e:
                logger.error(f"Error initializing component {component_name}: {e}", exc_info=True)
                self._component_readiness[component_name] = ComponentState.ERROR
                return False
        else:
            logger.debug(f"Component {component_name} waiting for dependencies: {missing_deps}")
            self._component_readiness[component_name] = ComponentState.UNINITIALIZED # Mark as waiting
            return False


    def shutdown(self):
        """Shutdown registry and cleanup components."""
        logger.info("Shutting down MapCoDocRegistry...")

        # --- Stop the watcher first if it exists and is running ---
        watcher = self.get_component("watcher") # Use generic name
        if watcher and hasattr(watcher, 'stop'):
            logger.info(f"Stopping FileSystemWatcher...")
            try:
                watcher.stop()
            except Exception as e:
                 logger.error(f"Error stopping watcher: {e}", exc_info=True)
        # ---------------------------------------------------------

        # Cleanup other components in reverse initialization order
        cleanup_order = list(reversed(self._initialization_order))
        logger.debug(f"Cleanup order: {cleanup_order}")

        for component_name in cleanup_order:
            component = self._components.get(component_name)
            if component and hasattr(component, 'cleanup'):
                logger.debug(f"Cleaning up component: {component_name}")
                try:
                    component.cleanup()
                except Exception as e:
                    logger.error(f"Error during cleanup of {component_name}: {e}", exc_info=True)

        # Clear internal state
        self._components.clear()
        self._component_readiness.clear()
        self._initialization_order.clear()
        self._component_creation_order.clear()
        self._dependency_graph = DependencyGraph() # Reset graph
        # self._shared_state = VersionedState() # Reset state
        self.event_bus = ComponentEventBus() # Reset event bus
        self._subscribers.clear()

        logger.info("MapCoDocRegistry shutdown complete.")


    def publish_event(self, event_name: str, data: Dict[str, Any], source_component: Optional[str] = None):
        """
        Publish an event to all registered subscribers with a standardized payload.

        Args:
            event_name: Name of the event.
            data: Event-specific data dictionary.
            source_component: Optional name of the component publishing the event.
                              If None, it might be inferred or set to 'Unknown'.
        """
        if not isinstance(data, dict):
            logger.error(f"Event data for '{event_name}' must be a dictionary. Received type: {type(data)}. Skipping publish.")
            return

        # Determine the source component name
        resolved_source = source_component or self.__class__.__name__ # Default to registry if not provided

        # Construct the standardized payload
        standard_payload: EventPayload = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source_component": resolved_source,
            "event_specific_data": data, # The original data goes here
            # "transaction_id": self.transaction_manager.transaction_id if self.transaction_manager.is_transaction_active() else None
        }

        logger.debug(f"Publishing event '{event_name}' from '{resolved_source}'") # Reduced log verbosity

        # Publish using the event bus
        self.event_bus.publish(event_name, standard_payload)


    def subscribe_to_event(self, event_type: str, callback: Callable[[EventPayload], None]):
        """
        Subscribe to an event on the event bus.

        Args:
            event_type: Type of event
            callback: Function to call when event occurs (should accept EventPayload)

        Returns:
            Unsubscribe function
        """
        return self.event_bus.subscribe(event_type, callback)


    def register(self, component_name: str, component: Any, strict_interface: bool = True) -> bool:
        """
        Register a component instance with the registry.

        Args:
            component_name: A unique name for the component.
            component: The component instance to register.
            strict_interface: If True, enforce interface validation strictly.

        Returns:
            True if registration was successful, False otherwise.
        """
        with self._lock:
            if not isinstance(component_name, str) or not component_name:
                logger.error(f"Invalid component name provided for registration: {component_name}")
                return False

            if component_name in self._components:
                logger.warning(f"Component '{component_name}' already registered. Overwriting.")
                # Consider cleanup of the old component?

            # Validate component interface
            validation_errors = RegistryValidator.validate_component(component_name, component)
            if validation_errors:
                for error in validation_errors:
                    logger.error(error)
                if strict_interface:
                    logger.error(f"Component {component_name} failed interface validation. Registration aborted.")
                    return False
                else:
                    logger.warning(f"Component {component_name} has interface issues but will be registered in non-strict mode.")

            # Store component
            self._components[component_name] = component
            self._component_creation_order.append(component_name)
            self._component_readiness[component_name] = ComponentState.UNINITIALIZED

            # Add to dependency graph
            dependencies = getattr(component, 'DEPENDENCIES', set())
            self._dependency_graph.add_component(component_name, list(dependencies))

            logger.info(f"Component '{component_name}' registered with dependencies: {dependencies}")

            # Attempt initialization immediately if dependencies are met
            self._initialize_single_component(component_name)

            # Publish registration event
            self.publish_event(REGISTRY_COMPONENT_REGISTERED, {
                'name': component_name,
                'dependencies': list(dependencies)
            }, source_component=self.__class__.__name__)

            return True

    def _get_components_with_method(self, method_name: str) -> List[Tuple[str, Any]]:
        """Get components that have a specific method."""
        result = []
        with self._lock:
            for name, component in self._components.items():
                if hasattr(component, method_name) and callable(getattr(component, method_name)):
                    result.append((name, component))
        return result


    def get_component(self, name: str) -> Any:
        """
        Get a component by name with proper error handling.

        Args:
            name: Component name

        Returns:
            Component instance or None if not found
        """
        with self._lock:
            component = self._components.get(name)
            if not component:
                logger.debug(f"Component '{name}' not found in registry.")
            return component


    def get_definition_module(self, name: str, context_module: Optional[str] = None) -> Optional[str]:
        """
        Get authoritative definition module for a component using DefinitionRegistry.

        Args:
            name: Component simple name.
            context_module: Optional FQN of the module where the name is used.

        Returns:
            Module path FQN or None if not found.
        """
        def_reg = self.get_component('definition_registry')
        if def_reg:
            return def_reg.get_definition_module(name, context_module)
        logger.warning("DefinitionRegistry not available to get definition module.")
        return None


    def get_module_definitions(self, module: str) -> Dict[str, Any]:
        """
        Get all definitions in a module using DefinitionRegistry.

        Args:
            module: Module path FQN.

        Returns:
            Dictionary of FQN -> definition info (or empty dict).
        """
        def_reg = self.get_component('definition_registry')
        if def_reg:
            # Assuming get_module_definitions returns Dict[fqn, DefinitionInfo]
            defs_info = def_reg.get_module_definitions(module)
            # Convert DefinitionInfo objects to dicts if needed by caller
            return {fqn: info.to_dict() if hasattr(info, 'to_dict') else info for fqn, info in defs_info.items()}
        logger.warning("DefinitionRegistry not available to get module definitions.")
        return {}

    
    def _check_integration_integrity(self):
        """
        Verify integrity of the integration between components. (Simplified)
        """
        issues = []
        status = True
        current_time = time.time()

        # Basic check: Ensure required components exist
        required = ["definition_registry", "analyzer_integration", "api_resolver"]
        for req in required:
            if req not in self._components:
                issues.append(f"Required component '{req}' is missing.")
                status = False

        # Check if components that need refs have them (can be complex)
        # Example: Check if API Resolver has a ref to AnalyzerIntegration
        api_resolver = self.get_component("api_resolver")
        analyzer = self.get_component("analyzer_integration")
        if api_resolver and analyzer:
            if getattr(api_resolver, '_analyzer_integration', None) is not analyzer:
                issues.append("API Resolver missing or incorrect reference to AnalyzerIntegration.")
                status = False
                # Attempt basic fix if possible (might need setter)
                # setattr(api_resolver, '_analyzer_integration', analyzer)

        self._integrity_status = {
            'last_check': current_time,
            'status': status,
            'issues': issues
        }

        if not status:
            logger.warning(f"Integration integrity issues detected: {issues}")
            # self._attempt_integrity_repair(issues) # Keep repair logic simple/removed for now

        return self._integrity_status


    def cleanup(self) -> None:
        """
        Clean up all components in reverse initialization order.
        """
        logger.info("Cleaning up registry components")

        # Get components in reverse initialization order
        cleanup_order = list(reversed(self._initialization_order))

        for component_name in cleanup_order:
            component = self._components.get(component_name)
            if not component:
                continue

            try:
                # Call component cleanup method if available
                if hasattr(component, 'cleanup'):
                    logger.debug(f"Cleaning up component: {component_name}")
                    component.cleanup()
            except Exception as e:
                logger.error(f"Error during cleanup of {component_name}: {e}", exc_info=True)

        # Clear internal state
        self._components.clear()
        self._component_readiness.clear()
        self._initialization_order.clear()
        self._component_creation_order.clear()
        self._dependency_graph = DependencyGraph()
        # self._shared_state = VersionedState()
        self.event_bus = ComponentEventBus()
        self._subscribers.clear()

        logger.info("Registry cleanup complete")


    def notify_dependency_ready(self, component_name: str):
        """Notify subscribers that a specific component/dependency is ready."""
        logger.info(f"Notifying readiness for dependency: {component_name}")

        # Publish general event
        event_data = {'dependency_name': component_name}
        self.publish_event(DEPENDENCY_READY, event_data, source_component=self.__class__.__name__)

        # --- Directly notify components waiting for this specific dependency ---
        # Need to iterate through all components and check their dependencies
        components_to_notify = []
        with self._lock:
            for waiting_comp_name, component in self._components.items():
                if self._component_readiness.get(waiting_comp_name) == ComponentState.UNINITIALIZED:
                    dependencies = getattr(component, 'DEPENDENCIES', set())
                    if component_name in dependencies:
                        # Check if *all* dependencies are now ready for this component
                        if all(self._component_readiness.get(dep) == ComponentState.READY for dep in dependencies):
                            components_to_notify.append(waiting_comp_name)

        # Attempt initialization for components whose dependencies are now met
        for comp_to_init in components_to_notify:
            self._initialize_single_component(comp_to_init)
            
