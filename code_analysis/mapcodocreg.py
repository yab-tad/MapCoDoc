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

    # def checkpoint(self, name: str) -> Dict[str, Any]:
    #     """
    #     Create a synchronization checkpoint between pipeline phases.

    #     Args:
    #         name: Checkpoint name

    #     Returns:
    #         Dictionary with checkpoint results
    #     """

    #     if name not in self.checkpoints:
    #         logger.warning(f"Unknown checkpoint: {name}")
    #         return {'success': False, 'error': f"Unknown checkpoint: {name}"}

    #     checkpoint = self.checkpoints[name]

    #     # Take snapshot
    #     snapshot_info = checkpoint.take_snapshot(self)

    #     # Verify consistency
    #     verification_results = checkpoint.verify_consistency(self)

    #     # If inconsistencies found, attempt recovery
    #     if not verification_results['success']:
    #         logger.warning(f"Inconsistencies detected at {name} checkpoint: {verification_results['inconsistencies']}")
    #         recovery_results = checkpoint.recover(self)

    #         # Log recovery results
    #         if recovery_results['success']:
    #             logger.info(f"Successfully recovered from inconsistencies at {name} checkpoint")
    #         else:
    #             logger.error(f"Failed to recover from inconsistencies at {name} checkpoint")

    #         # Update verification results
    #         verification_results = checkpoint.verify_consistency(self)

    #     # Publish event
    #     self.publish_event('checkpoint_completed', {
    #         'name': name,
    #         'success': verification_results['success'],
    #         'issues': verification_results.get('inconsistencies', []),
    #         'state': {
    #             'component_readiness': {k: v.name for k, v in self._component_readiness.items()},
    #             'verification_timestamp': checkpoint.verification_timestamp
    #         }
    #     })

    #     return {
    #         'checkpoint': name,
    #         'snapshot': snapshot_info,
    #         'verification': verification_results,
    #         'timestamp': time.time()
    #     }


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

    # def begin_transaction(self, name: str = "", components: Optional[List[str]] = None) -> int:
    #     """Begin a transaction for batch operations with named context."""
    #     return self.transaction_manager.begin_transaction(name, components)

    # def commit_transaction(self) -> bool:
    #     """Commit the current transaction."""
    #     return self.transaction_manager.commit_transaction()

    # def rollback_transaction(self) -> bool:
    #     """Roll back the current transaction."""
    #     return self.transaction_manager.rollback_transaction()

    def _get_components_with_method(self, method_name: str) -> List[Tuple[str, Any]]:
        """Get components that have a specific method."""
        result = []
        with self._lock:
            for name, component in self._components.items():
                if hasattr(component, method_name) and callable(getattr(component, method_name)):
                    result.append((name, component))
        return result


    # def synchronize_components(self, force: bool = False) -> Dict[str, Any]:
    #     """
    #     Synchronize state between all components. (Simplified - relies on event-driven updates mostly)

    #     Args:
    #         force: Force synchronization even if recent

    #     Returns:
    #         Synchronization results (basic status for now)
    #     """
    #     logger.info("Synchronizing components (event-driven approach)...")
    #     # In the refactored model, explicit sync is less critical.
    #     # Components should update based on events.
    #     # This method can be used for periodic checks or forced updates if needed.

    #     # Check integrity as part of sync
    #     integrity_status = self._check_integration_integrity()

    #     # Potentially trigger state updates based on shared state
    #     current_shared_state = self._shared_state.get_all()
    #     updated_components = []
    #     failed_components = []

    #     with self._lock:
    #         for name, component in self._components.items():
    #             if hasattr(component, 'sync_state'):
    #                 try:
    #                     # Only sync if component's local version is behind shared state
    #                     local_version = self._component_local_versions.get(name, -1)
    #                     if force or local_version < self._shared_state.version:
    #                         logger.debug(f"Syncing state to {name} (Local: {local_version}, Shared: {self._shared_state.version})")
    #                         if component.sync_state(current_shared_state):
    #                             self._component_local_versions[name] = self._shared_state.version
    #                             updated_components.append(name)
    #                         else:
    #                             failed_components.append(name)
    #                 except Exception as e:
    #                     logger.error(f"Error syncing state to {name}: {e}", exc_info=True)
    #                     failed_components.append(name)


    #     sync_result = {
    #         'integrity_status': integrity_status,
    #         'updated_components': updated_components,
    #         'failed_components': failed_components,
    #         'shared_state_version': self._shared_state.version
    #     }
    #     self.publish_event(REGISTRY_STATE_SYNCED, sync_result, self.__class__.__name__)
    #     return sync_result


    # def _check_state_consistency(self, component_states: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    #     """
    #     Check for inconsistencies between component states using updated state keys.
    #     Focuses on comparing state reported by primary components.
    #     """
    #     inconsistencies = []
    #     analyzer_state = component_states.get('analyzer_integration')
    #     resolver_state = component_states.get('api_resolver')
    #     def_reg_state = component_states.get('definition_registry')

    #     # --- 1. Check Definition Counts ---
    #     if analyzer_state and def_reg_state:
    #         # Analyzer state might not directly expose definition count, but maybe module count?
    #         # Let's compare DefinitionRegistry count with Analyzer's analyzed module count
    #         analyzer_modules = analyzer_state.get('files_analyzed', 0) # Or len(analyzer_state.get('analysis_details', {}))
    #         registry_defs = def_reg_state.get('total_definitions')

    #         # This comparison is weak, but better than nothing
    #         # if tracker_defs is not None and registry_defs is not None and tracker_defs != registry_defs:
    #         #     inconsistencies.append({ ... })
    #         pass # Skip direct definition count comparison for now

    #     # --- 2. Check API Boundary Counts/Consistency ---
    #     # API Resolver is the primary source for boundaries now
    #     resolver_boundaries = resolver_state.get('api_boundaries') if resolver_state else None
    #     # If AnalyzerIntegration also reports boundaries, compare here
    #     # analyzer_boundaries = analyzer_state.get('detected_boundaries') # Hypothetical key
    #     # if resolver_boundaries is not None and analyzer_boundaries is not None:
    #     #     if set(resolver_boundaries) != set(analyzer_boundaries):
    #     #          inconsistencies.append({ ... })
    #     pass # Skip boundary consistency check for now unless Analyzer reports it

    #     # --- 3. Check Chain Candidate Counts ---
    #     # API Resolver is the primary source
    #     resolver_candidates_count = resolver_state.get('chain_candidates_count') if resolver_state else None
    #     # Compare if AnalyzerIntegration also reports candidates
    #     # analyzer_candidates_count = analyzer_state.get('candidate_count') # Hypothetical
    #     # if resolver_candidates_count is not None and analyzer_candidates_count is not None:
    #     #      if resolver_candidates_count != analyzer_candidates_count:
    #     #          inconsistencies.append({ ... })
    #     pass # Skip candidate consistency check

    #     # --- 4. Check Module Counts ---
    #     analyzer_module_count = analyzer_state.get('files_analyzed') if analyzer_state else None
    #     resolver_module_cache_size = resolver_state.get('module_cache_size') if resolver_state else None # API Resolver might still cache

    #     module_counts = {}
    #     if analyzer_module_count is not None: module_counts['analyzer'] = analyzer_module_count
    #     if resolver_module_cache_size is not None: module_counts['resolver_cache'] = resolver_module_cache_size

    #     if len(module_counts) > 1:
    #         counts = list(module_counts.values())
    #         if max(counts) - min(counts) > max(5, max(counts) * 0.1): # Allow 10% or 5 diff
    #             inconsistencies.append({
    #                 'type': 'module_count_mismatch',
    #                 'components': list(module_counts.keys()),
    #                 'counts': module_counts,
    #                 'details': "Reported module counts differ significantly between components."
    #             })

    #     # --- 5. Check API Map Size ---
    #     resolver_map_size = resolver_state.get('api_map_size') if resolver_state else None
    #     # Compare if other components report map size

    #     if inconsistencies:
    #         logger.warning(f"Found {len(inconsistencies)} state inconsistencies during check.")
    #     else:
    #         logger.debug("State consistency check passed.")

    #     return inconsistencies


    # def _resolve_inconsistencies(self, inconsistencies: List[Dict[str, Any]],
    #                            component_states: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    #     """Resolve inconsistencies between components (Simplified)."""
    #     # This method becomes less critical if state is primarily managed by VersionedState and components react to events. Focus on logging inconsistencies for now.
    #     resolved = []
    #     logger.warning(f"Inconsistencies detected, manual review or component-specific recovery might be needed: {inconsistencies}")
    #     # Add logic here to trigger specific recovery actions if needed based on inconsistency type
    #     return resolved


    # def _create_component_sync_state(self, component_name: str, component_states: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    #     """Create component-specific state for synchronization (Simplified)."""
    #     # Return the full shared state for simplicity, components can pick what they need
    #     return self._shared_state.get_all()


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


    # def verify_definition_location(self, component_path: str) -> Optional[str]:
    #     """
    #     Centralized definition location verification using PathVerificationService.

    #     Args:
    #         component_path: Component path to verify

    #     Returns:
    #         Verified path (potentially corrected) or original if no correction needed/possible.
    #         Returns None only if the input path itself is invalid (e.g., empty).
    #     """
        
    #     if not component_path:
    #          return None

    #     # PathVerificationService is initialized in __init__
    #     result = self.path_verification_service.verify_path(component_path)

    #     # Log the correction only if it happened
    #     if result['corrected'] and result['verified_path'] != component_path:
    #         logger.debug(f"Path correction: {component_path} -> {result['verified_path']} (method: {result['verification_method']})")

    #         # Publish correction event
    #         self.publish_event('path_correction', {
    #             'original': component_path,
    #             'corrected': result['verified_path'],
    #             'method': result['verification_method'],
    #             'confidence': result['confidence']
    #         }, source_component=self.__class__.__name__)

    #     # Return the final verified path from the result dictionary
    #     return result['verified_path']


    # def get_verification_stats(self):
    #     """Get statistics on definition verification."""
    #     return self.path_verification_service.get_statistics()


    # def _verify_component_integration(self) -> Dict[str, Any]:
    #     """
    #     Verify and fix component integration issues with enhanced diagnostics. (Simplified)
    #     """
    #     logger.debug("Verifying component integration...")
    #     verification_results = {
    #         'success': True,
    #         'issues_detected': [],
    #         'issues_fixed': [],
    #     }

    #     # Check basic references
    #     analyzer = self.get_component("analyzer_integration")
    #     api_resolver = self.get_component("api_resolver")
    #     def_reg = self.get_component("definition_registry")

    #     if not analyzer: verification_results['issues_detected'].append("AnalyzerIntegration missing")
    #     if not api_resolver: verification_results['issues_detected'].append("APIPathResolver missing")
    #     if not def_reg: verification_results['issues_detected'].append("DefinitionRegistry missing")

    #     if analyzer and api_resolver:
    #         if getattr(api_resolver, '_analyzer_integration', None) is not analyzer:
    #             issue = "API Resolver missing/incorrect AnalyzerIntegration reference"
    #             verification_results['issues_detected'].append(issue)
    #             # Attempt fix
    #             setattr(api_resolver, '_analyzer_integration', analyzer)
    #             verification_results['issues_fixed'].append(issue)

    #     if analyzer and def_reg:
    #         if getattr(analyzer, 'definition_registry', None) is not def_reg:
    #             issue = "AnalyzerIntegration missing/incorrect DefinitionRegistry reference"
    #             verification_results['issues_detected'].append(issue)
    #             setattr(analyzer, 'definition_registry', def_reg)
    #             verification_results['issues_fixed'].append(issue)

    #     if api_resolver and def_reg:
    #         if getattr(api_resolver, 'definition_registry', None) is not def_reg:
    #             issue = "API Resolver missing/incorrect DefinitionRegistry reference"
    #             verification_results['issues_detected'].append(issue)
    #             setattr(api_resolver, 'definition_registry', def_reg)
    #             verification_results['issues_fixed'].append(issue)


    #     verification_results['success'] = not verification_results['issues_detected'] or len(verification_results['issues_detected']) == len(verification_results['issues_fixed'])

    #     if verification_results['issues_fixed']:
    #         logger.info(f"Fixed integration issues: {verification_results['issues_fixed']}")
    #     if verification_results['issues_detected'] and not verification_results['success']:
    #         logger.warning(f"Detected unfixed integration issues: {verification_results['issues_detected']}")

    #     return verification_results


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

    
    # def sync_state(self, state: Dict[str, Any]) -> bool:
    #     """Synchronize state with another registry instance (Simplified)."""
    #     logger.info(f"Syncing registry state from external source...")
    #     # This is complex. For now, just update the shared state.
    #     # A full sync would involve registering/updating components based on the state.
    #     conflicts = self._shared_state.update(state.get('_shared_state', {}), "external_sync")
    #     if conflicts:
    #         logger.warning(f"Conflicts detected during external state sync: {conflicts}")
    #         # Attempt resolution?
    #         # self._resolve_state_conflicts(conflicts)

    #     # Potentially trigger updates in components based on new shared state
    #     self.synchronize_components(force=True)

    #     logger.info("Registry state sync completed.")
    #     return True


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
            
            
#--------------------------------possible code to remove--------------------------------

# class ConflictResolver:
#     """
#     Formal conflict resolution protocol for reconciling contradictory information from different components.
    
#     This resolver uses a weighted scoring system to determine the most reliable information when components provide conflicting data.
#     """
    
#     def __init__(self, registry):
#         """Initialize resolver with reference to the registry."""
#         self.registry = registry
#         self.resolution_log = []
#         self.resolution_strategies = {
#             'api_path': self._resolve_api_path_conflict,
#             'definition_location': self._resolve_definition_location_conflict,
#             'api_boundary': self._resolve_api_boundary_conflict
#         }
        
#         # Component trust scores - can be adjusted based on component reliability
#         self.component_trust = {
#             'definition_registry': 0.9,    # Highest trust for definition data
#             'relationship_tracker': 0.8,   # High trust for import/export relationships
#             'api_resolver': 0.7            # Good trust for API paths
#         }
    
#     def resolve_conflict(self, conflict_type: str, values: Dict[str, Any], 
#                          context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
#         """
#         Resolve a conflict between different components.
        
#         Args:
#             conflict_type: Type of conflict to resolve
#             values: Dictionary mapping component_name -> value
#             context: Optional context for resolution
            
#         Returns:
#             Resolution result with selected value and justification
#         """
#         strategy = self.resolution_strategies.get(conflict_type)
#         if not strategy:
#             return self._resolve_generic_conflict(conflict_type, values, context or {})
            
#         result = strategy(values, context or {})
        
#         # Log the resolution
#         resolution_record = {
#             'type': conflict_type,
#             'values': values,
#             'context': context,
#             'result': result,
#             'timestamp': time.time()
#         }
#         self.resolution_log.append(resolution_record)
        
#         logger.debug(f"Resolved {conflict_type} conflict: {result.get('selected')} from {result.get('source')}")
#         return result
    
#     def _resolve_generic_conflict(self, conflict_type: str, values: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
#         """Generic conflict resolution based on component trust scores."""
#         if not values:
#             return {
#                 'selected': None,
#                 'source': None,
#                 'score': 0.0,
#                 'justification': 'No values provided'
#             }

#         # Calculate weighted scores
#         scored_values = []
#         for component, value in values.items():
#             # Base score from component trust
#             base_score = self.component_trust.get(component, 0.3)

#             # Adjust score based on value characteristics
#             if value is None:
#                 adj_score = 0.0  # No value provided
#             elif isinstance(value, str) and not value:
#                 adj_score = 0.1  # Empty string
#             else:
#                 adj_score = base_score

#             scored_values.append((component, value, adj_score))

#         # Sort by score in descending order
#         scored_values.sort(key=lambda x: x[2], reverse=True)

#         # Select highest scoring value
#         best_component, best_value, best_score = scored_values[0]

#         return {
#             'selected': best_value,
#             'source': best_component,
#             'score': best_score,
#             'justification': f'Selected based on component trust score: {best_score}'
#         }
    
#     def _resolve_api_path_conflict(self, values: Dict[str, str], context: Dict[str, Any]) -> Dict[str, Any]:
#         """Resolve conflicts between different API path suggestions."""
#         scored_paths = []
#         component_path = context.get('component_path', '')
        
#         for component, api_path in values.items():
#             if not api_path:
#                 continue
                
#             # Base score from component trust
#             base_score = self.component_trust.get(component, 0.3)
            
#             # Scoring factors
#             factors = {
#                 'trust_score': base_score * 10,  # Weight trust score heavily
#                 'api_boundary': 0.0,
#                 'path_quality': 0.0,
#                 'definition_match': 0.0
#             }
            
#             # Factor 1: API Boundary score
#             # Access boundaries via registry's central state or API resolver directly
#             api_resolver = self.registry.get_component('api_resolver')
#             api_boundaries = getattr(api_resolver, 'api_boundaries', []) if api_resolver else []
#             if api_path and '.' in api_path:
#                 api_module = api_path.rsplit('.', 1)[0]
#                 if api_module in api_boundaries:
#                     factors['api_boundary'] = 10.0  # Strong signal

#             # Factor 2: Path quality score - shorter paths, fewer dots
#             if component_path and api_path:
#                 comp_depth = component_path.count('.')
#                 api_depth = api_path.count('.')
#                 if api_depth < comp_depth:
#                     # Shallower API is often better (surfacing to the API)
#                     factors['path_quality'] = 5.0

#                 # Avoid suspicious patterns
#                 if api_path.split('.')[0] == component_path.split('.')[0]:
#                     # Same package - more likely valid
#                     factors['path_quality'] += 3.0

#             # Factor 3: Definition registry match
#             def_reg = self.registry.get_component('definition_registry')
#             if def_reg:
#                 if component_path and '.' in component_path:
#                     comp_name = component_path.rsplit('.', 1)[1]
#                     if '.' in api_path:
#                         api_module = api_path.rsplit('.', 1)[0]
#                         module_path = component_path.rsplit('.', 1)[0]
#                         definition_module = def_reg.get_definition_module(comp_name, module_path)

#                         if definition_module:
#                             # Check if API module is an ancestor of definition module
#                             if definition_module.startswith(f"{api_module}."):
#                                 factors['definition_match'] = 5.0
#                             # Or check if API module is a package-level exposure
#                             elif api_module.count('.') < definition_module.count('.'):
#                                 factors['definition_match'] = 3.0

#             # Calculate total score
#             total_score = sum(factors.values())

#             scored_paths.append({
#                 'component': component,
#                 'path': api_path,
#                 'score': total_score,
#                 'factors': factors
#             })

#         # Sort by score in descending order
#         scored_paths.sort(key=lambda x: x['score'], reverse=True)

#         if not scored_paths:
#             return {
#                 'selected': component_path,  # Fallback to original path
#                 'source': 'fallback',
#                 'score': 0.0,
#                 'justification': 'No valid API paths provided'
#             }

#         best = scored_paths[0]
#         return {
#             'selected': best['path'],
#             'source': best['component'],
#             'score': best['score'],
#             'factors': best['factors'],
#             'justification': f'Selected based on weighted scoring: {best["score"]}'
#         }
    
#     def _resolve_definition_location_conflict(self, values: Dict[str, str], context: Dict[str, Any]) -> Dict[str, Any]:
#         """Resolve conflicts about the true definition location of a component."""
#         component_name = context.get('component_name', '')
        
#         if not component_name:
#             return {
#                 'selected': None,
#                 'source': None,
#                 'score': 0.0,
#                 'justification': 'Missing component name in context'
#             }
            
#         scored_locations = []
        
#         for component, module_path in values.items():
#             if not module_path:
#                 continue
                
#             # Base score from component trust
#             base_score = self.component_trust.get(component, 0.3)
            
#             # Scoring factors
#             factors = {
#                 'trust_score': base_score * 10,  # Weight trust score heavily
#                 'registry_match': 0.0,
#                 'ast_definition': 0.0 # Score based on AnalyzerIntegration result
#             }
            
#             # Factor 1: Registry verification
#             def_reg = self.registry.get_component('definition_registry')
#             if def_reg:
#                 definition_module = def_reg.get_definition_module(component_name, module_path)
#                 if definition_module and definition_module == module_path:
#                     factors['registry_match'] = 15.0  # Very strong signal

#             # Factor 2: AST definition presence (via AnalyzerIntegration)
#             analyzer = self.registry.get_component('analyzer_integration')
#             if analyzer:
#                 analysis_result = analyzer.get_analysis_result(module_path)
#                 if analysis_result:
#                     # Check if component_name is defined in this module's analysis result
#                     components_in_module = analysis_result.get("components", {})
#                     fqn_to_check = f"{module_path}.{component_name}"
#                     if fqn_to_check in components_in_module:
#                         factors['ast_definition'] = 12.0 # High confidence from direct analysis

#             # Calculate total score
#             total_score = sum(factors.values())

#             scored_locations.append({
#                 'component': component,
#                 'module': module_path,
#                 'score': total_score,
#                 'factors': factors
#             })

#         # Sort by score in descending order
#         scored_locations.sort(key=lambda x: x['score'], reverse=True)

#         if not scored_locations:
#             return {
#                 'selected': None,
#                 'source': None,
#                 'score': 0.0,
#                 'justification': 'No valid definition locations provided'
#             }

#         best = scored_locations[0]
#         return {
#             'selected': best['module'],
#             'source': best['component'],
#             'score': best['score'],
#             'factors': best['factors'],
#             'justification': f'Selected based on weighted scoring: {best["score"]}'
#         }



# class VersionedState:
#     """State container with version tracking for conflict detection."""
    
#     def __init__(self, initial_state: Dict[str, Any] = None):
#         self.state = initial_state or {}
#         self.version = 0
#         self.last_updated = {}  # key -> version
#         self.update_conflicts = []

#     def get(self, key: str, default: Any = None) -> Any:
#         """Get a state value with provided default."""
#         return self.state.get(key, default)

#     def get_all(self) -> Dict[str, Any]:
#         """Get full state dictionary."""
#         return self.state.copy()

#     def update(self, updates: Dict[str, Any], source: str = "unknown") -> Dict[str, Any]:
#         """
#         Update state with conflict resolution and fallback mechanisms.

#         Args:
#             updates: Dictionary of updates to apply
#             source: Source of updates (for diagnostic purposes)

#         Returns:
#             Dictionary of conflicts that were resolved
#         """
#         conflicts = {}
#         resolved_updates = {}

#         # Process each update
#         for key, value in updates.items():
#             # Check for conflict
#             if key in self.state and key in self.last_updated:
#                 # Only conflict if different value
#                 if self.state[key] != value:
#                     conflicts[key] = {
#                         'old_value': self.state[key],
#                         'new_value': value,
#                         'last_update_version': self.last_updated[key],
#                         'current_version': self.version,
#                         'resolution_status': 'pending'
#                     }

#         # Resolve conflicts
#         if conflicts:
#             resolved_conflicts = self._resolve_conflicts(conflicts, source)

#             # Apply resolved values
#             for key, resolution in resolved_conflicts.items():
#                 if resolution['status'] == 'accepted':
#                     resolved_updates[key] = resolution['value']
#                     # Update state with resolved value
#                     self.state[key] = resolution['value']
#                     self.last_updated[key] = self.version
#                 elif resolution['status'] == 'rejected':
#                     # Keep original value, no update needed
#                     pass
#                 elif resolution['status'] == 'merged':
#                     # Apply merged value
#                     resolved_updates[key] = resolution['value']
#                     self.state[key] = resolution['value']
#                     self.last_updated[key] = self.version

#         # Apply non-conflicting updates
#         for key, value in updates.items():
#             if key not in conflicts:
#                 self.state[key] = value
#                 self.last_updated[key] = self.version
#                 resolved_updates[key] = value

#         self.version += 1
#         return resolved_updates

    
#     def _resolve_conflicts(self, conflicts: Dict[str, Dict[str, Any]], source: str) -> Dict[str, Dict[str, Any]]:
#         """
#         Resolve state conflicts using strategy-based resolution.

#         Args:
#             conflicts: Dictionary of conflicts
#             source: Source of the conflicting updates

#         Returns:
#             Dictionary of resolved conflicts
#         """
#         resolved = {}

#         # Define resolution strategies for different data types
#         for key, conflict in conflicts.items():
#             old_value = conflict['old_value']
#             new_value = conflict['new_value']

#             # Strategy 1: List/Set type merging
#             if isinstance(old_value, (list, set)) and isinstance(new_value, (list, set)):
#                 # Convert to sets for union
#                 old_set = set(old_value)
#                 new_set = set(new_value)

#                 # Merge using union
#                 merged = old_set.union(new_set)

#                 # Convert back to original type
#                 if isinstance(old_value, list):
#                     merged_value = sorted(list(merged)) # Sort for consistency
#                 else:
#                     merged_value = merged

#                 resolved[key] = {
#                     'status': 'merged',
#                     'value': merged_value,
#                     'resolution': 'union',
#                     'conflict': conflict
#                 }

#             # Strategy 2: Dictionary merging
#             elif isinstance(old_value, dict) and isinstance(new_value, dict):
#                 # Deep merge dictionaries
#                 merged = self._deep_merge_dicts(old_value, new_value)

#                 resolved[key] = {
#                     'status': 'merged',
#                     'value': merged,
#                     'resolution': 'deep_merge',
#                     'conflict': conflict
#                 }

#             # Strategy 3: Strings - use newer value
#             elif isinstance(old_value, str) and isinstance(new_value, str):
#                 resolved[key] = {
#                     'status': 'accepted',
#                     'value': new_value,
#                     'resolution': 'newer_value',
#                     'conflict': conflict
#                 }

#             # Strategy 4: Number types - use newer value if significantly different
#             elif isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
#                 # Use newer value if significantly different
#                 if abs(new_value - old_value) / max(1, abs(old_value)) > 0.1:  # 10% change
#                     resolved[key] = {
#                         'status': 'accepted',
#                         'value': new_value,
#                         'resolution': 'significant_change',
#                         'conflict': conflict
#                     }
#                 else:
#                     # Minor change, keep old value
#                     resolved[key] = {
#                         'status': 'rejected',
#                         'value': old_value,
#                         'resolution': 'insignificant_change',
#                         'conflict': conflict
#                     }

#             # Strategy 5: Object types - default to newer value
#             else:
#                 resolved[key] = {
#                     'status': 'accepted',
#                     'value': new_value,
#                     'resolution': 'default_to_newer',
#                     'conflict': conflict
#                 }

#         return resolved

#     def _deep_merge_dicts(self, d1: Dict[str, Any], d2: Dict[str, Any]) -> Dict[str, Any]:
#         """Deep merge two dictionaries."""
#         result = d1.copy()

#         for key, value in d2.items():
#             if key in result and isinstance(result[key], dict) and isinstance(value, dict):
#                 # Recursively merge nested dictionaries
#                 result[key] = self._deep_merge_dicts(result[key], value)
#             else:
#                 # Replace or add value
#                 result[key] = value

#         return result



# class TransactionManager:
#     """
#     Manages cross-component transactions with isolation and rollback.
#     Ensures that either all operations across components complete or none do.
#     """
    
#     def __init__(self, registry):
#         self.registry = registry
#         self.active_transaction = False
#         self.transaction_id = 0
#         self.transaction_components = set()
#         self.transaction_start_time = 0
#         self.transaction_operations = {}  # component -> operation count
#         self.transaction_state = {}  # pre-transaction state per component
#         self.transaction_errors = []
#         self.transaction_timeout = 300  # 5 minutes
#         self.isolation_level = "READ_COMMITTED"
#         self._deadlock_check_time = 0
#         self._deadlock_wait_graph = {}
#         self._resource_holders = {}
#         self._deadlock_timeout = 60

#     def begin_transaction(self, name: str = "", components: Optional[List[str]] = None, isolation_level: str = "READ_COMMITTED") -> int:
#         """
#         Begin a transaction with specified isolation level.

#         Isolation levels:
#         - READ_UNCOMMITTED: No isolation, can see uncommitted changes
#         - READ_COMMITTED: Can only see committed changes (default)
#         - REPEATABLE_READ: Ensures repeated reads within transaction yield same results
#         - SERIALIZABLE: Complete isolation, transactions executed as if serial

#         Args:
#             name: Transaction name for logging
#             components: List of components to include (None = all)
#             isolation_level: Transaction isolation level

#         Returns:
#             Transaction ID
#         """
#         if self.active_transaction:
#             logger.warning("Transaction already in progress, nesting not supported")
#             return self.transaction_id

#         self.active_transaction = True
#         self.transaction_id += 1
#         self.transaction_start_time = time.time()
#         self.transaction_errors = []
#         self.isolation_level = isolation_level

#         # Determine components for this transaction
#         if components is None:
#             # Use all components with transaction support
#             self.transaction_components = set()
#             # Use registry._components which holds the actual instances
#             for component_name in self.registry._components:
#                 component = self.registry._components.get(component_name)
#                 if component and hasattr(component, 'begin_transaction'):
#                     self.transaction_components.add(component_name)
#         else:
#             self.transaction_components = set(components)

#         # Set up transaction-specific state based on isolation level
#         if isolation_level == "REPEATABLE_READ" or isolation_level == "SERIALIZABLE":
#             # Take snapshot of state that will be consistent throughout transaction
#             self.transaction_snapshot = self._capture_component_state(self.transaction_components)

#         # Store initial state for each component
#         self.transaction_state = {}
#         self.transaction_operations = {}

#         for component_name in self.transaction_components:
#             component = self.registry._components.get(component_name) # Use _components
#             if component:
#                 # Store initial state
#                 if hasattr(component, 'get_state'):
#                     self.transaction_state[component_name] = component.get_state()

#                 # Initialize operation counter
#                 self.transaction_operations[component_name] = 0

#                 # Start transaction in component
#                 if hasattr(component, 'begin_transaction'):
#                     try:
#                         transaction_args = {}
#                         # Pass isolation level if component supports it
#                         # Assume components don't support isolation levels for now unless explicitly checked
#                         # if hasattr(component, 'supports_isolation_levels') and component.supports_isolation_levels:
#                         #     transaction_args['isolation_level'] = isolation_level

#                         component.begin_transaction(**transaction_args)
#                     except Exception as e:
#                         logger.error(f"Error beginning transaction in {component_name}: {e}")
#                         self.transaction_errors.append({
#                             'component': component_name,
#                             'phase': 'begin',
#                             'error': str(e)
#                         })

#         logger.info(f"Transaction {self.transaction_id} ({name}) started with {len(self.transaction_components)} components")

#         # Set up deadlock detection timer
#         self._setup_deadlock_detection()

#         return self.transaction_id

#     def _capture_component_state(self, components: Set[str]) -> Dict[str, Any]:
#         """Capture the state of components for isolation purposes."""
#         snapshot = {}

#         for component_name in components:
#             component = self.registry._components.get(component_name) # Use _components
#             if component and hasattr(component, 'get_state'):
#                 try:
#                     snapshot[component_name] = component.get_state()
#                 except Exception as e:
#                     logger.error(f"Error capturing state for {component_name}: {e}")

#         return snapshot

#     def _setup_deadlock_detection(self):
#         """Set up deadlock detection for this transaction."""
#         self._deadlock_check_time = time.time()
#         self._deadlock_wait_graph = {}  # Component waiting for resource -> components holding resource
#         self._resource_holders = {}  # Resource ID -> set of components holding it

#         # Initialize deadlock timeout
#         self._deadlock_timeout = 60  # 1 minute default timeout

#     def check_for_deadlocks(self) -> bool:
#         """
#         Check for potential deadlocks in the transaction.

#         Returns:
#             True if deadlock detected, False otherwise
#         """
#         current_time = time.time()

#         # Only check periodically
#         if current_time - self._deadlock_check_time < 5:  # Every 5 seconds
#             return False

#         self._deadlock_check_time = current_time

#         # Check for timeout
#         if current_time - self.transaction_start_time > self._deadlock_timeout:
#             logger.warning(f"Transaction timeout after {self._deadlock_timeout} seconds, possible deadlock")
#             return True

#         # Check dependency cycle (if we have wait graph data)
#         if not self._deadlock_wait_graph:
#             return False

#         # Simple cycle detection
#         visited = set()
#         path = set()

#         def dfs(node):
#             """Depth-first search to detect cycles."""
#             if node in path:
#                 return True  # Cycle detected

#             if node in visited:
#                 return False

#             visited.add(node)
#             path.add(node)

#             for neighbor in self._deadlock_wait_graph.get(node, []):
#                 if dfs(neighbor):
#                     return True

#             path.remove(node)
#             return False

#         # Check for cycles starting from each node
#         for node in self._deadlock_wait_graph:
#             if dfs(node):
#                 logger.warning(f"Deadlock detected in transaction {self.transaction_id}")
#                 return True

#         return False


#     def commit_transaction(self) -> bool:
#         """
#         Commit the current transaction across all components.

#         Returns:
#             True if commit was successful
#         """
#         if not self.active_transaction:
#             logger.warning("No active transaction to commit")
#             return False

#         # Verify no errors occurred during transaction
#         if self.transaction_errors:
#             logger.error(f"Cannot commit transaction with errors: {len(self.transaction_errors)} errors occurred")
#             return self.rollback_transaction()

#         success = True
#         commit_errors = []

#         # Two-phase commit: preparation phase (optional, depends on components)
#         prepared_components = []
#         can_prepare = all(hasattr(self.registry._components.get(name), 'prepare_commit') for name in self.transaction_components)

#         if can_prepare:
#             for component_name in self.transaction_components:
#                 component = self.registry._components.get(component_name)
#                 if component and hasattr(component, 'prepare_commit'):
#                     try:
#                         if component.prepare_commit():
#                             prepared_components.append(component_name)
#                         else:
#                             logger.error(f"Component {component_name} failed to prepare for commit")
#                             commit_errors.append({
#                                 'component': component_name,
#                                 'phase': 'prepare',
#                                 'error': 'Failed to prepare for commit'
#                             })
#                             success = False
#                             break
                    
#                     except Exception as e:
#                         logger.error(f"Error preparing commit in {component_name}: {e}")
#                         commit_errors.append({
#                             'component': component_name,
#                             'phase': 'prepare',
#                             'error': str(e)
#                         })
#                         success = False
#                         break

#             # If preparation failed, roll back
#             if not success:
#                 logger.error(f"Preparation phase failed, rolling back transaction")
#                 self.transaction_errors.extend(commit_errors)
#                 return self.rollback_transaction()

#         # Commit phase
#         for component_name in self.transaction_components:
#             component = self.registry._components.get(component_name)
#             if component and hasattr(component, 'commit_transaction'):
#                 try:
#                     # Assume commit_transaction returns bool or raises error
#                     if not component.commit_transaction():
#                         logger.error(f"Failed to commit transaction in {component_name}")
#                         success = False
#                         commit_errors.append({
#                             'component': component_name,
#                             'phase': 'commit',
#                             'error': 'Commit failed'
#                         })
#                 except Exception as e:
#                     logger.error(f"Error committing transaction in {component_name}: {e}")
#                     success = False
#                     commit_errors.append({
#                         'component': component_name,
#                         'phase': 'commit',
#                         'error': str(e)
#                     })

#         # If commit failed, attempt rollback but this is problematic
#         # as some components may have committed successfully
#         if not success:
#             logger.error(f"Commit phase failed, attempting recovery")
#             self.transaction_errors.extend(commit_errors)

#             # Try to recover - this is complex and may not be fully possible
#             # Rollback components that were prepared but might not have committed
#             components_to_rollback = prepared_components if can_prepare else self.transaction_components
#             for component_name in components_to_rollback:
#                 component = self.registry._components.get(component_name)
#                 # Attempt rollback even if commit failed
#                 if component and hasattr(component, 'rollback_transaction'):
#                     try:
#                         component.rollback_transaction()
#                     except Exception as e:
#                         logger.error(f"Error during rollback after failed commit for {component_name}: {e}")
#                 # Check for specific recovery methods if available
#                 elif component and hasattr(component, 'recover_partial_commit'):
#                     try:
#                         component.recover_partial_commit()
#                     except Exception as e:
#                         logger.error(f"Error in recovery for {component_name}: {e}")

#         # Record transaction in log
#         transaction_time = time.time() - self.transaction_start_time

#         if success:
#             logger.info(f"Transaction {self.transaction_id} committed in {transaction_time:.2f}s")
#         else:
#             logger.error(f"Transaction {self.transaction_id} failed in {transaction_time:.2f}s")

#         # Reset transaction state
#         self.active_transaction = False
#         self.transaction_components = set()
#         self.transaction_state = {}
#         self.transaction_operations = {}

#         return success


#     def rollback_transaction(self) -> bool:
#         """
#         Roll back with partial completion handling.

#         Returns:
#             True if rollback succeeded, False otherwise
#         """
#         if not self.active_transaction:
#             logger.warning("No active transaction to roll back")
#             return False

#         success = True
#         rollback_errors = []
#         rollback_operations = []

#         # Track rollback operations for retry
#         rollback_status = {
#             'complete': [],
#             'partial': [],
#             'failed': []
#         }

#         # Roll back each component in reverse dependency order
#         rollback_order = self._get_reverse_dependency_order()
#         logger.info(f"Rolling back components in order: {rollback_order}")

#         for component_name in rollback_order:
#             component = self.registry._components.get(component_name) # Use _components
#             if component and hasattr(component, 'rollback_transaction'):
#                 try:
#                     result = component.rollback_transaction()

#                     # Handle enhanced return format with status (optional)
#                     if isinstance(result, dict) and 'status' in result:
#                         if result['status'] == 'complete':
#                             rollback_status['complete'].append(component_name)
#                         elif result['status'] == 'partial':
#                             rollback_status['partial'].append(component_name)
#                             # Record specific operations that failed
#                             if 'failed_operations' in result:
#                                 rollback_operations.extend(result['failed_operations'])
#                         else:
#                             rollback_status['failed'].append(component_name)
#                             success = False
#                     elif result is True:  # Legacy boolean result
#                         rollback_status['complete'].append(component_name)
#                     else: # Assume False means failure
#                         rollback_status['failed'].append(component_name)
#                         success = False
#                 except Exception as e:
#                     logger.error(f"Error rolling back transaction in {component_name}: {e}")
#                     success = False
#                     rollback_errors.append({
#                         'component': component_name,
#                         'phase': 'rollback',
#                         'error': str(e)
#                     })
#                     rollback_status['failed'].append(component_name)

#         # Handle partial rollbacks - attempt to fix inconsistent state
#         if rollback_status['partial'] or rollback_status['failed']:
#             self._handle_partial_rollback(rollback_status, rollback_operations)

#         # Log transaction rollback status
#         transaction_time = time.time() - self.transaction_start_time
#         operation_counts = {comp: count for comp, count in self.transaction_operations.items() if count > 0}

#         if success:
#             logger.info(f"Transaction {self.transaction_id} rolled back in {transaction_time:.2f}s, operations: {operation_counts}")
#         else:
#             logger.error(f"Transaction {self.transaction_id} rollback failed in {transaction_time:.2f}s")
#             self.transaction_errors.extend(rollback_errors)

#         # Reset transaction state
#         self.active_transaction = False
#         self.transaction_components = set()
#         self.transaction_state = {}
#         self.transaction_operations = {}
#         self._deadlock_wait_graph = {}
#         self._resource_holders = {}

#         return success

#     def _get_reverse_dependency_order(self) -> List[str]:
#         """Get components in reverse dependency order for safe rollback."""
#         # Use the dependency graph for accurate ordering
#         if hasattr(self.registry, '_dependency_graph'):
#             try:
#                 # Get initialization order (dependency first)
#                 init_order = self.registry._dependency_graph.get_initialization_order()

#                 # Filter to include only transaction components
#                 ordered = [c for c in init_order if c in self.transaction_components]

#                 # Reverse for rollback order (dependent first)
#                 return list(reversed(ordered))
#             except Exception as e:
#                 logger.warning(f"Error getting dependency order: {e}, using basic reverse order.")
#                 # Fallback to basic reverse order of components involved
#                 return list(reversed(list(self.transaction_components)))
#         else:
#             logger.warning("Dependency graph not available, using basic reverse order for rollback.")
#             return list(reversed(list(self.transaction_components)))


#     def _handle_partial_rollback(self, rollback_status: Dict[str, List[str]],
#                                 failed_operations: List[Dict[str, Any]]) -> None:
#         """
#         Handle partial rollback by trying to fix the state.

#         Args:
#             rollback_status: Status of rollback by component
#             failed_operations: Operations that failed to roll back
#         """
#         logger.warning("Handling partial rollback recovery")

#         # Approach 1: Try a second pass on failed components
#         for component_name in rollback_status['failed']:
#             component = self.registry._components.get(component_name) # Use _components
#             if component and hasattr(component, 'rollback_transaction'):
#                 try:
#                     logger.info(f"Attempting second rollback pass for {component_name}")
#                     component.rollback_transaction()
#                 except Exception as e:
#                     logger.error(f"Second rollback attempt failed for {component_name}: {e}")

#         # Approach 2: Try to rebuild component state from stored snapshots
#         for component_name in rollback_status['failed'] + rollback_status['partial']:
#             if component_name in self.transaction_state:
#                 component = self.registry._components.get(component_name) # Use _components
#                 # Check for a specific state restoration method (less common)
#                 if component and hasattr(component, 'restore_state'):
#                     try:
#                         logger.info(f"Attempting state restoration for {component_name}")
#                         component.restore_state(self.transaction_state[component_name])
#                     except Exception as e:
#                         logger.error(f"State restoration failed for {component_name}: {e}")
#                 # More common: Re-sync the component with the last known good state (if available)
#                 elif component and hasattr(component, 'sync_state'):
#                     # Need a way to get the "good" state - maybe from registry's shared state?
#                     # This is complex. For now, just log.
#                     logger.warning(f"State restoration via sync_state after partial rollback not fully implemented for {component_name}.")


#         # Approach 3: Reset component connections for integration recovery
#         if hasattr(self.registry, '_verify_component_integration'):
#             try:
#                 logger.info("Verifying component integration after partial rollback")
#                 self.registry._verify_component_integration()
#             except Exception as e:
#                 logger.error(f"Integration verification failed: {e}")

#     def record_operation(self, component_name: str) -> None:
#         """Record an operation in the current transaction."""
#         if self.active_transaction and component_name in self.transaction_operations:
#             self.transaction_operations[component_name] += 1

#             # Check for potential deadlocks periodically
#             if self.transaction_operations[component_name] % 10 == 0:  # Every 10 operations
#                 if self.check_for_deadlocks():
#                     logger.warning(f"Potential deadlock detected for {component_name}")

#     def is_transaction_active(self) -> bool:
#         """Check if a transaction is currently active."""
#         return self.active_transaction

#     def get_transaction_errors(self) -> List[Dict[str, Any]]:
#         """Get any errors that occurred during the current transaction."""
#         return self.transaction_errors.copy()



# class SynchronizationCheckpoint:
#     """Represents a synchronization point between pipeline phases."""
    
#     def __init__(self, name: str, required_components: List[str]):
#         self.name = name
#         self.required_components = set(required_components)
#         self.state_snapshots = {}  # component -> state
#         self.verification_results = {}  # component -> verification result
#         self.is_verified = False
#         self.verification_timestamp = 0
    
#     def take_snapshot(self, registry) -> Dict[str, Any]:
#         """
#         Take state snapshots of all required components.

#         Returns:
#             Dictionary of state verification info
#         """
#         self.state_snapshots = {}
#         missing_components = []

#         for component_name in self.required_components:
#             component = registry.get_component(component_name) # Use registry's get_component
#             if component and hasattr(component, 'get_state'):
#                 self.state_snapshots[component_name] = component.get_state()
#             else:
#                 missing_components.append(component_name)

#         return {
#             'checkpoint': self.name,
#             'components_captured': len(self.state_snapshots),
#             'components_missing': missing_components
#         }


#     def verify_consistency(self, registry) -> Dict[str, Any]:
#         """
#         Verify state consistency across components.

#         Returns:
#             Dictionary with verification results
#         """
#         self.verification_results = {}
#         inconsistencies = []

#         # Check critical shared data structures using updated component names/logic
#         api_boundaries_consistent = self._verify_api_boundaries(registry)
#         if not api_boundaries_consistent:
#             inconsistencies.append("API boundaries mismatch")

#         chain_candidates_consistent = self._verify_chain_candidates(registry)
#         if not chain_candidates_consistent:
#             inconsistencies.append("Chain candidates mismatch")

#         # Check component-specific consistency
#         for component_name in self.required_components:
#             component = registry.get_component(component_name)
#             if component and hasattr(component, 'verify_consistency'): # Assuming components might have this
#                 try:
#                     result = component.verify_consistency()
#                     self.verification_results[component_name] = result
#                     if not result.get('success', False):
#                         inconsistencies.append(f"{component_name} internal inconsistency: {result.get('issues', [])}")
#                 except Exception as e:
#                     inconsistencies.append(f"{component_name} verification error: {e}")

#         self.is_verified = len(inconsistencies) == 0
#         self.verification_timestamp = time.time()

#         return {
#             'checkpoint': self.name,
#             'success': self.is_verified,
#             'inconsistencies': inconsistencies,
#             'component_results': self.verification_results,
#             'timestamp': self.verification_timestamp
#         }


#     def _verify_api_boundaries(self, registry) -> bool:
#         """Verify API boundaries consistency across components."""
#         boundary_sets = []

#         # Check API Resolver and potentially AnalyzerIntegration if it stores boundaries
#         for component_name in ['api_resolver']: # Add 'analyzer_integration' if relevant
#             component = registry.get_component(component_name)
#             if component and hasattr(component, 'api_boundaries'):
#                 boundary_sets.append((component_name, set(component.api_boundaries)))

#         if len(boundary_sets) <= 1:
#             return True # No comparison needed

#         # Check that all boundary sets are the same
#         first_set = boundary_sets[0][1]
#         return all(s[1] == first_set for s in boundary_sets)


#     def _verify_chain_candidates(self, registry) -> bool:
#         """Verify chain candidates consistency across components."""
#         candidate_sets = []

#         # Check API Resolver and potentially AnalyzerIntegration
#         for component_name in ['api_resolver']: # Add 'analyzer_integration' if relevant
#             component = registry.get_component(component_name)
#             if component and hasattr(component, 'chain_candidates'):
#                 candidate_sets.append((component_name, set(component.chain_candidates)))

#         if len(candidate_sets) <= 1:
#             return True

#         # Check that all candidate sets are the same
#         first_set = candidate_sets[0][1]
#         return all(s[1] == first_set for s in candidate_sets)


#     def recover(self, registry) -> Dict[str, Any]:
#         """
#         Attempt to recover from inconsistencies.

#         Returns:
#             Dictionary with recovery results
#         """
#         fixed_issues = []

#         # API boundaries recovery
#         if not self._verify_api_boundaries(registry):
#             try:
#                 fixed = self._fix_api_boundaries(registry)
#                 if fixed:
#                     fixed_issues.append("API boundaries synchronized")
#             except Exception as e:
#                 logger.error(f"Error fixing API boundaries: {e}")

#         # Chain candidates recovery
#         if not self._verify_chain_candidates(registry):
#             try:
#                 fixed = self._fix_chain_candidates(registry)
#                 if fixed:
#                     fixed_issues.append("Chain candidates synchronized")
#             except Exception as e:
#                 logger.error(f"Error fixing chain candidates: {e}")

#         # Component-specific recovery
#         for component_name in self.required_components:
#             component = registry.get_component(component_name)
#             if component and hasattr(component, 'recover_consistency'): # Assuming components might have this
#                 try:
#                     result = component.recover_consistency()
#                     if result.get('fixed_issues', []):
#                         fixed_issues.extend([f"{component_name}: {issue}" for issue in result['fixed_issues']])
#                 except Exception as e:
#                     logger.error(f"Error in recovery for {component_name}: {e}")

#         success = len(fixed_issues) > 0

#         # Re-verify after recovery
#         if success:
#             self.verify_consistency(registry)

#         return {
#             'checkpoint': self.name,
#             'success': success,
#             'fixed_issues': fixed_issues,
#             'is_consistent': self.is_verified
#         }


#     def _fix_api_boundaries(self, registry) -> bool:
#         """Fix API boundaries inconsistency."""
#         boundary_sets = []

#         # Collect all boundary sets
#         for component_name in ['api_resolver']: # Add others if needed
#             component = registry.get_component(component_name)
#             if component and hasattr(component, 'api_boundaries'):
#                 boundary_sets.append((component_name, set(component.api_boundaries)))

#         if not boundary_sets:
#             return False

#         # Determine which set to use - prefer api_resolver? Or merge? Let's merge.
#         merged_set = set()
#         for _, boundaries in boundary_sets:
#             merged_set.update(boundaries)

#         # Update all components
#         for component_name in ['api_resolver']: # Add others if needed
#             component = registry.get_component(component_name)
#             if component and hasattr(component, 'api_boundaries'):
#                 # Ensure the attribute is assignable (might need a setter method)
#                 if hasattr(component, 'set_api_boundaries'):
#                     component.set_api_boundaries(list(merged_set))
#                 else:
#                     component.api_boundaries = list(merged_set) # Direct assignment if possible

#         return True


#     def _fix_chain_candidates(self, registry) -> bool:
#         """Fix chain candidates inconsistency."""
#         candidate_sets = []

#         for component_name in ['api_resolver']: # Add others if needed
#             component = registry.get_component(component_name)
#             if component and hasattr(component, 'chain_candidates'):
#                 candidates = getattr(component, 'chain_candidates', set())
#                 if hasattr(candidates, 'copy'):
#                     candidate_sets.append((component_name, candidates.copy()))
#                 else:
#                     candidate_sets.append((component_name, set(candidates)))

#         if not candidate_sets:
#             return False

#         # Take union of all candidate sets
#         all_candidates = set()
#         for _, candidates in candidate_sets:
#             all_candidates.update(candidates)

#         # Update all components
#         for component_name in ['api_resolver']: # Add others if needed
#             component = registry.get_component(component_name)
#             if component:
#                 if hasattr(component, 'set_chain_candidates'):
#                     component.set_chain_candidates(all_candidates, source="registry_fix") # Pass source
#                 elif hasattr(component, 'chain_candidates'):
#                     # Direct assignment if setter not available
#                     component.chain_candidates = all_candidates.copy()

#         return True



# class PathVerificationService:
#     """
#     Unified path verification service for consistent path verification across components.
#     Centralizes verification logic and decision making for path corrections.
#     """
    
#     def __init__(self, registry):
#         self.registry = registry
#         # References will be set/updated when components register or via direct access
#         self.definition_registry = None
#         self.analyzer_integration = None # Use AnalyzerIntegration instead of RelationshipTracker
#         self.api_resolver = None

#         # Stats tracking
#         self.verification_stats = {
#             'total': 0,
#             'verified': 0,
#             'corrected': 0,
#             'registry_used': 0,
#             'analyzer_used': 0, # Changed from relationship_used
#             'suspicious_rejected': 0,
#             'bidirectional_verified': 0,
#             'cache_hits': 0,
#             'cache_misses': 0
#         }

#         # Cache for verification results (path -> verified_path)
#         # Using a simple dictionary-based cache for now.
#         # Consider using a more sophisticated cache (like LRU) if needed.
#         self.verification_cache: Dict[str, Optional[str]] = {}
#         self._cache_max_size = 5000 # Example cache size limit

#         # Impact tracking for corrections
#         self.correction_impacts = []

#         # Safety mode flags
#         self.safe_mode = False  # When True, only make low-risk corrections
    
    
#     def _update_component_refs(self):
#         """Ensure component references are up-to-date."""
#         if not self.definition_registry:
#             self.definition_registry = self.registry.get_component('definition_registry')
#         if not self.analyzer_integration:
#             self.analyzer_integration = self.registry.get_component('analyzer_integration')
#         if not self.api_resolver:
#             self.api_resolver = self.registry.get_component('api_resolver')


#     def verify_path(self, component_path: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
#         """
#         Verify a component path with full context awareness and multi-stage pipeline.

#         Args:
#             component_path: Component path to verify
#             context: Optional context for verification (caller, import chain, etc)

#         Returns:
#             Dictionary with verification results
#         """
#         self._update_component_refs() # Ensure refs are current
#         self.verification_stats['total'] += 1

#         # Initialize context
#         if context is None:
#             context = {}

#         # --- Cache Check ---
#         cache_key = component_path # Simple key for now
#         if cache_key in self.verification_cache:
#             cached_verified_path = self.verification_cache[cache_key]
#             self.verification_stats['cache_hits'] += 1
#             logger.debug(f"Path verification cache hit for '{component_path}' -> '{cached_verified_path}'")
#             # Reconstruct a basic result dict from the cached verified path
#             # Note: This bypasses detailed verification steps and confidence calculation for cached hits.
#             # If more detail is needed from cache, store the full result dict instead.
#             return {
#                 'original_path': component_path,
#                 'verified_path': cached_verified_path if cached_verified_path is not None else component_path,
#                 'corrected': cached_verified_path is not None and cached_verified_path != component_path,
#                 'verification_method': 'cache',
#                 'confidence': 1.0 if cached_verified_path is not None else 0.0, # High confidence for cached result
#                 'warnings': [],
#                 'safe_mode': self.safe_mode
#             }
#         else:
#             self.verification_stats['cache_misses'] += 1
#         # ---
        
#         result = {
#             'original_path': component_path,
#             'verified_path': component_path,  # Default to no change
#             'corrected': False,
#             'verification_method': 'none',
#             'confidence': 0.0,
#             'warnings': [],
#             'safe_mode': self.safe_mode
#         }

#         verified_path_final: Optional[str] = component_path # Track the final verified path through stages

#         # Stage 1: Basic validation and normalization
#         if '.' not in component_path:
#             result['verification_method'] = 'basic'
#             result['confidence'] = 1.0  # High confidence for top-level components
#             self.verification_stats['verified'] += 1
#             verified_path_final = component_path # Path is verified as is
#             # --- Cache Update ---
#             self._update_cache(cache_key, verified_path_final)
#             # ---
#             return result

#         module_path, component_name = component_path.rsplit('.', 1)

#         # Stage 2: Definition registry verification
#         definition_module = None # Initialize
#         if self.definition_registry:
#             self.verification_stats['registry_used'] += 1 # Mark registry as used for lookup
#             definition_module = self.definition_registry.get_definition_module(component_name, module_path)

#             if definition_module is not None and definition_module != module_path:
#                 # Found difference between path and definition location
#                 is_api_path = self._is_legitimate_api_path(component_path, definition_module, context)

#                 if is_api_path:
#                     result['verification_method'] = 'definition_registry_api'
#                     result['confidence'] = 0.8
#                     result['warnings'].append("Path differs from definition location but appears to be legitimate API path")
#                     verified_path_final = component_path # Path confirmed as API path
#                 else:
#                     # Correct to definition location
#                     corrected_path = f"{definition_module}.{component_name}"
#                     impact = self._assess_correction_impact(component_path, corrected_path)

#                     if self.safe_mode and impact['risk_level'] == 'high':
#                         result['warnings'].append(f"Skipped high-risk correction in safe mode: {impact['risk_factors']}")
#                         verified_path_final = component_path # Keep original in safe mode
#                     else:
#                         result['verified_path'] = corrected_path
#                         result['corrected'] = True
#                         result['verification_method'] = 'definition_registry'
#                         result['confidence'] = 0.9
#                         result['impact'] = impact
#                         self.verification_stats['corrected'] += 1
#                         verified_path_final = corrected_path # Update final path

#                         self.correction_impacts.append({
#                             'original': component_path,
#                             'corrected': corrected_path,
#                             'impact': impact,
#                             'timestamp': time.time()
#                         })
#             elif definition_module is not None and definition_module == module_path:
#                 # Definition registry confirms this path
#                 result['verification_method'] = 'definition_registry_confirmed'
#                 result['confidence'] = 0.9
#                 self.verification_stats['verified'] += 1
#                 verified_path_final = component_path # Path confirmed
#             else:
#                 # Definition registry doesn't know about this component/path
#                 result['verification_method'] = 'definition_registry_miss'
#                 result['confidence'] = 0.0 # Low confidence if registry doesn't confirm
#                 verified_path_final = component_path # Keep original path
#                 self.verification_stats['no_registry_data'] += 1 # Increment miss counter

#         else: # No definition registry available
#             self.verification_stats['no_registry_data'] += 1
#             verified_path_final = component_path # Keep original path


#         # Stage 3: Relationship-based verification (using AnalyzerIntegration)
#         # Only run if not already corrected by registry OR if registry missed
#         if (not result['corrected'] or result['verification_method'] == 'definition_registry_miss') and self.analyzer_integration:
#             self.verification_stats['analyzer_used'] += 1 # Mark analyzer as used
#             rel_verification = self._verify_with_relationships(verified_path_final, context) # Verify the current path

#             if rel_verification['corrected'] and rel_verification['path'] != verified_path_final:
#                 impact = self._assess_correction_impact(verified_path_final, rel_verification['path'])

#                 if self.safe_mode and impact['risk_level'] == 'high':
#                     result['warnings'].append(f"Skipped high-risk relationship correction in safe mode: {impact['risk_factors']}")
#                     # Keep verified_path_final as is
#                 else:
#                     # Apply correction from relationship analysis
#                     previous_path = verified_path_final
#                     verified_path_final = rel_verification['path']
#                     result['verified_path'] = verified_path_final
#                     result['corrected'] = True # Mark as corrected overall
#                     result['verification_method'] = 'analyzer_integration' # Update method
#                     result['confidence'] = max(result['confidence'], rel_verification['confidence']) # Use higher confidence
#                     result['impact'] = impact
#                     # Adjust stats: if previously verified/missed, now corrected
#                     if self.verification_stats['verified'] > 0 and result['verification_method'] != 'definition_registry_confirmed':
#                         self.verification_stats['verified'] -= 1
#                     if self.verification_stats['no_registry_data'] > 0 and result['verification_method'] == 'definition_registry_miss':
#                         self.verification_stats['no_registry_data'] -= 1
#                     self.verification_stats['corrected'] += 1 # Increment corrected count

#                     self.correction_impacts.append({
#                         'original': previous_path, # Track the path before this stage's correction
#                         'corrected': verified_path_final,
#                         'impact': impact,
#                         'timestamp': time.time()
#                     })

#         # Stage 4: Bidirectional verification (can remain similar)
#         # Verify the *final* path against the original input path
#         if result['corrected']:
#             round_trip = self._verify_round_trip(component_path, verified_path_final)
#             if round_trip['success']:
#                 result['bidirectional_verified'] = True
#                 result['semantic_equivalence'] = round_trip.get('semantic_equivalence', False)
#                 self.verification_stats['bidirectional_verified'] += 1
#             else:
#                 result['warnings'].append(f"Bidirectional verification failed: {round_trip['issue']}")
#                 if round_trip['confidence'] > 0.8:
#                     # Revert correction
#                     reverted_path = verified_path_final
#                     verified_path_final = component_path
#                     result['verified_path'] = verified_path_final
#                     result['corrected'] = False
#                     result['warnings'].append("Reverted correction due to failed bidirectional verification")
#                     if self.verification_stats['corrected'] > 0:
#                         self.verification_stats['corrected'] -= 1
#                     # Remove the last correction impact entry if it matches the reverted path
#                     if self.correction_impacts and self.correction_impacts[-1]['corrected'] == reverted_path:
#                         self.correction_impacts.pop()


#         # Stage 5: Suspicious path detection (can remain similar)
#         # Check the *final* path against the original input path
#         if result['corrected']:
#             is_suspicious = self._is_suspicious_path(verified_path_final, component_path)
#             if is_suspicious:
#                 result['warnings'].append("Suspicious path correction detected")
#                 # Revert suspicious corrections
#                 reverted_path = verified_path_final
#                 verified_path_final = component_path
#                 result['verified_path'] = verified_path_final
#                 result['corrected'] = False
#                 result['warnings'].append("Reverted suspicious correction")
#                 if self.verification_stats['corrected'] > 0:
#                     self.verification_stats['corrected'] -= 1
#                 self.verification_stats['suspicious_rejected'] += 1
#                 # Remove the last correction impact entry if it matches the reverted path
#                 if self.correction_impacts and self.correction_impacts[-1]['corrected'] == reverted_path:
#                     self.correction_impacts.pop()

#         # --- Cache Update ---
#         # Cache the final verified path (could be None if verification failed badly, though unlikely here)
#         # Store original path if not corrected, otherwise store the corrected path.
#         self._update_cache(cache_key, verified_path_final if result['corrected'] else component_path)
#         # ---

#         return result


#     def _update_cache(self, key: str, value: Optional[str]):
#         """Update the verification cache, managing size."""
#         if len(self.verification_cache) >= self._cache_max_size:
#             # Simple eviction: remove the first item (FIFO-like)
#             try:
#                 first_key = next(iter(self.verification_cache))
#                 del self.verification_cache[first_key]
#             except StopIteration:
#                 pass # Cache was empty
#         self.verification_cache[key] = value
    
    
#     def _is_legitimate_api_path(self, component_path: str, definition_module: str, context: Optional[Dict[str, Any]]=None) -> bool:
#         """
#         Determine if a path is a legitimate API path that shouldn't be "corrected" back to implementation.
#         """
#         self._update_component_refs()
#         if component_path == definition_module: # Path points directly to definition module
#             return True

#         module_path, component_name = component_path.rsplit('.', 1)

#         # 1. Is this an explicit API module? (Check API Resolver)
#         if self.api_resolver and hasattr(self.api_resolver, 'api_boundaries'):
#             if module_path in self.api_resolver.api_boundaries:
#                 return True

#         # 2. Does this component exist in an export chain? (Check API Resolver)
#         if self.api_resolver and hasattr(self.api_resolver, 'export_chains'):
#             # Check if component_path is the *result* of an export chain
#             for impl_path, chain in self.api_resolver.export_chains.items():
#                 if chain:
#                     final_step = chain[-1]
#                     chain_api_path = f"{final_step.module_path}.{final_step.export_name}"
#                     if chain_api_path == component_path:
#                         return True # This path is the result of a known export chain

#         # 3. Check API map (Check API Resolver)
#         if self.api_resolver and hasattr(self.api_resolver, 'api_map'):
#             # Check if this is a known API path value in the map
#             if component_path in self.api_resolver.api_map.values():
#                 return True

#         # 4. Structure comparison between current path and definition module
#         def_depth = definition_module.count('.')
#         path_depth = module_path.count('.')

#         if path_depth < def_depth:
#             # Current path is shallower than definition - likely intentional API path
#             return True

#         # 5. Check if module_path is an __init__.py (Check AnalyzerIntegration results)
#         if self.analyzer_integration:
#             analysis_result = self.analyzer_integration.get_analysis_result(module_path)
#             if analysis_result and analysis_result.get("source_file", "").endswith("__init__.py"):
#                 # __init__ modules often intentionally expose implementation components
#                 return True

#         # By default, not considered a legitimate API path
#         return False

    
#     def _verify_with_relationships(self, component_path: str, context: Dict[str, Any]) -> Dict[str, Any]:
#         """
#         Verify path using AnalyzerIntegration results (imports/exports).
#         """
#         self._update_component_refs()
#         result = {
#             'path': component_path,
#             'corrected': False,
#             'confidence': 0.0,
#             'method': 'analyzer'
#         }

#         if not self.analyzer_integration or '.' not in component_path:
#             return result

#         module_path, component_name = component_path.rsplit('.', 1)

#         # Try to find the true definition module using analysis results
#         defining_module = None

#         # Method 1: Use Definition Registry first (higher confidence)
#         if self.definition_registry:
#             defining_module = self.definition_registry.get_definition_module(component_name, module_path)

#         # Method 2: Infer from import/export chains if definition registry fails
#         # This requires more complex logic within AnalyzerIntegration or APIResolver
#         # to trace origins based on the graph. Let's assume APIResolver handles this.
#         if not defining_module and self.api_resolver:
#             # Ask API resolver to trace the origin (if such a method exists)
#             # Placeholder:
#             # origin_info = self.api_resolver.trace_origin(component_path)
#             # if origin_info: defining_module = origin_info.get('definition_module')
#             pass # Keep definition_module as None if not found by registry

#         # If we found a defining module different from the current path
#         if defining_module and defining_module != module_path:
#             # Verify this isn't a legitimate API path
#             if not self._is_legitimate_api_path(component_path, defining_module, context):
#                 result['path'] = f"{defining_module}.{component_name}"
#                 result['corrected'] = True
#                 result['confidence'] = 0.7 # Confidence based on combined info

#         return result


#     def _verify_round_trip(self, original_path: str, corrected_path: str) -> Dict[str, Any]:
#         """
#         Round-trip verification with semantic equivalence validation.
#         """
#         self._update_component_refs()
#         result = {
#             'success': True,
#             'confidence': 0.5,
#             'issue': None,
#             'semantic_equivalence': False
#         }

#         if original_path == corrected_path:
#             result['confidence'] = 1.0
#             result['semantic_equivalence'] = True
#             return result

#         # Extract module and component parts
#         original_module, component_name = original_path.rsplit('.', 1) if '.' in original_path else ('', original_path)
#         corrected_module, corrected_name = corrected_path.rsplit('.', 1) if '.' in corrected_path else ('', corrected_path)


#         # 1. Component name should be the same or have known relationship
#         if component_name != corrected_name:
#             name_relationship = self._check_name_relationship(component_name, corrected_name)
#             if not name_relationship['related']:
#                 result['success'] = False
#                 result['issue'] = f"Component name mismatch: {component_name} vs {corrected_name}"
#                 result['confidence'] = 0.9
#                 return result

#         # 2. Check if importing corrected_path gives access to the same component
#         semantic_equiv = self._check_semantic_equivalence(original_path, corrected_path)
#         result['semantic_equivalence'] = semantic_equiv['equivalent']
#         result['semantic_details'] = semantic_equiv['details']

#         if not semantic_equiv['equivalent']:
#             result['success'] = False
#             result['issue'] = f"Semantic equivalence check failed: {semantic_equiv['details']}"
#             result['confidence'] = 0.85
#             # Don't return immediately, gather more info

#         # 3. Verify both modules exist based on analysis results
#         original_valid = False
#         corrected_valid = False
#         if self.analyzer_integration:
#             original_valid = self.analyzer_integration.get_analysis_result(original_module) is not None
#             corrected_valid = self.analyzer_integration.get_analysis_result(corrected_module) is not None

#         if not original_valid:
#             result['success'] = False
#             result['issue'] = f"Original module not found in analysis results: {original_module}"
#             result['confidence'] = min(result['confidence'], 0.7)

#         if not corrected_valid:
#             result['success'] = False
#             result['issue'] = f"Corrected module not found in analysis results: {corrected_module}"
#             result['confidence'] = min(result['confidence'], 0.8)


#         # 4. Check for valid transit path between the modules (using API Resolver chains)
#         chain_found = False
#         if self.api_resolver and hasattr(self.api_resolver, 'export_chains'):
#             # Check if a chain exists starting near original_path and ending at corrected_path
#             # This is complex. Simplified check: Does corrected_path appear as an API path for original_path?
#             resolved_api = self.api_resolver.resolve_api_path(original_path)
#             if resolved_api == corrected_path:
#                 chain_found = True
#             else:
#                 # Check if corrected_path is *one of* the possible API paths
#                 all_paths = self.api_resolver.resolve_api_path(original_path, collect_all=True)
#                 if isinstance(all_paths, list) and corrected_path in all_paths:
#                     chain_found = True

#         if not chain_found:
#             result['success'] = False
#             result['issue'] = f"No valid export chain found from {original_path} to {corrected_path}"
#             result['confidence'] = min(result['confidence'], 0.7)


#         # 5. Verify import chain exists in actual code (using AnalyzerIntegration imports)
#         transit_verification = self._verify_import_transit(original_module, corrected_module, component_name)

#         if not transit_verification['valid']:
#             result['success'] = False
#             result['issue'] = f"Import transit verification failed: {transit_verification['reason']}"
#             result['confidence'] = min(result['confidence'], transit_verification['confidence'])

#         return result


#     def _check_name_relationship(self, name1: str, name2: str) -> Dict[str, Any]:
#         """Check if two component names have a known relationship."""
#         result = {
#             'related': False,
#             'relationship_type': None,
#             'confidence': 0.0
#         }

#         if name1 == name2:
#             result['related'] = True
#             result['relationship_type'] = 'identical'
#             result['confidence'] = 1.0
#             return result

#         # Check for common prefix/suffix patterns
#         common_prefixes = ['_', 'Base', 'Abstract', 'Generic']
#         for prefix in common_prefixes:
#             if name1 == f"{prefix}{name2}" or name2 == f"{prefix}{name1}":
#                 result['related'] = True
#                 result['relationship_type'] = 'prefix_variation'
#                 result['confidence'] = 0.8
#                 return result

#         # Check for common implementation/interface patterns
#         if name1.endswith('Impl') and name1[:-4] == name2:
#             result['related'] = True
#             result['relationship_type'] = 'implementation'
#             result['confidence'] = 0.9
#             return result

#         # Check for name registry if available (assuming registry might have this)
#         # if hasattr(self.registry, 'name_registry'):
#         #     name_registry = self.registry.name_registry
#         #     if hasattr(name_registry, 'are_names_related'):
#         #         registry_result = name_registry.are_names_related(name1, name2)
#         #         if registry_result['related']:
#         #             return registry_result

#         # Check for high string similarity
#         similarity = self._calculate_string_similarity(name1, name2)
#         if similarity > 0.85:  # High similarity
#             result['related'] = True
#             result['relationship_type'] = 'string_similarity'
#             result['confidence'] = similarity
#             return result

#         return result

#     def _calculate_string_similarity(self, s1: str, s2: str) -> float:
#         """Calculate string similarity between two strings using Levenshtein distance."""
#         if s1 == s2:
#             return 1.0

#         # Simple implementation of Levenshtein distance
#         if not s1:
#             return 0.0 if s2 else 1.0
#         if not s2:
#             return 0.0

#         # Initialize matrix
#         m, n = len(s1), len(s2)
#         d = [[0] * (n + 1) for _ in range(m + 1)]

#         # First row and column
#         for i in range(m + 1):
#             d[i][0] = i
#         for j in range(n + 1):
#             d[0][j] = j

#         # Fill matrix
#         for j in range(1, n + 1):
#             for i in range(1, m + 1):
#                 if s1[i-1] == s2[j-1]:
#                     d[i][j] = d[i-1][j-1]
#                 else:
#                     d[i][j] = min(d[i-1][j], d[i][j-1], d[i-1][j-1]) + 1

#         # Calculate similarity score (0 to 1)
#         max_len = max(len(s1), len(s2))
#         distance = d[m][n]
#         if max_len == 0: return 1.0 # Both empty
#         similarity = 1.0 - (distance / max_len)

#         return similarity

#     def _check_semantic_equivalence(self, path1: str, path2: str) -> Dict[str, Any]:
#         """
#         Check if two paths refer to the same component semantically.
#         """
#         self._update_component_refs()
#         result = {
#             'equivalent': False,
#             'confidence': 0.0,
#             'details': '',
#             'equivalence_type': None
#         }

#         # Skip check if paths are identical
#         if path1 == path2:
#             result['equivalent'] = True
#             result['confidence'] = 1.0
#             result['details'] = 'Identical paths'
#             result['equivalence_type'] = 'identical'
#             return result

#         # Method 1: Check API map for known equivalence (API Resolver)
#         if self.api_resolver and hasattr(self.api_resolver, 'api_map'):
#             api_map = self.api_resolver.api_map
#             # Check if one path maps to the other
#             if api_map.get(path1) == path2 or api_map.get(path2) == path1:
#                 result['equivalent'] = True
#                 result['confidence'] = 0.95
#                 result['details'] = 'Direct mapping in API map'
#                 result['equivalence_type'] = 'api_map_direct'
#                 return result

#             # Check for common API mapping
#             path1_api = api_map.get(path1)
#             path2_api = api_map.get(path2)
#             if path1_api and path2_api and path1_api == path2_api:
#                 result['equivalent'] = True
#                 result['confidence'] = 0.9
#                 result['details'] = 'Common API mapping'
#                 result['equivalence_type'] = 'api_map_common'
#                 return result

#         # Method 2: Check export chains (API Resolver)
#         if self.api_resolver and hasattr(self.api_resolver, 'export_chains'):
#             chain1 = self.api_resolver.export_chains.get(path1, [])
#             chain2 = self.api_resolver.export_chains.get(path2, [])

#             if chain1 and chain2:
#                 # Check if chains originate from the same definition FQN
#                 # This assumes ExportStep has origin_fqn or similar
#                 origin1 = getattr(chain1[0], 'origin_fqn', None) if chain1 else None
#                 origin2 = getattr(chain2[0], 'origin_fqn', None) if chain2 else None
#                 if origin1 and origin2 and origin1 == origin2:
#                     result['equivalent'] = True
#                     result['confidence'] = 0.88 # High confidence if chains trace to same origin
#                     result['details'] = f'Export chains originate from same definition: {origin1}'
#                     result['equivalence_type'] = 'shared_chain_origin'
#                     return result

#                 # Check for common modules (less reliable)
#                 modules1 = {step.module_path for step in chain1}
#                 modules2 = {step.module_path for step in chain2}
#                 common_modules = modules1.intersection(modules2)
#                 if common_modules:
#                     result['equivalent'] = True
#                     result['confidence'] = 0.7 # Lower confidence
#                     result['details'] = f'Chains share {len(common_modules)} common modules'
#                     result['equivalence_type'] = 'shared_chain_modules'
#                     # Don't return yet, check other methods

#         # Method 3: Check definition source (Definition Registry)
#         module_path1, name1 = path1.rsplit('.', 1) if '.' in path1 else ('', path1)
#         module_path2, name2 = path2.rsplit('.', 1) if '.' in path2 else ('', path2)

#         if name1 == name2 and self.definition_registry:
#             # Check if both paths point to same definition
#             def1 = self.definition_registry.get_definition_module(name1, module_path1)
#             def2 = self.definition_registry.get_definition_module(name2, module_path2)

#             if def1 and def2 and def1 == def2:
#                 result['equivalent'] = True
#                 result['confidence'] = max(result['confidence'], 0.8) # Update confidence if higher
#                 result['details'] = f'Common definition module: {def1}'
#                 result['equivalence_type'] = 'common_definition'
#                 # Don't return yet

#         # Method 4: Check AnalyzerIntegration import/export data (More complex)
#         # This would involve checking if module1 exports name1 which is then imported by module2 as name2, etc.
#         # Let's skip this complex check for now and rely on the other methods.

#         # If any method found equivalence, return the highest confidence result
#         if result['equivalent']:
#             return result

#         # Not equivalent by any method
#         result['details'] = 'No equivalence detected'
#         return result

#     def _verify_import_transit(self, source_module: str, target_module: str,
#                             component_name: str) -> Dict[str, Any]:
#         """
#         Verify that a component can actually be imported through the proposed transit path.
#         Uses AnalyzerIntegration results.
#         """
#         self._update_component_refs()
#         result = {
#             'valid': False,
#             'reason': '',
#             'confidence': 0.0
#         }

#         # Skip if modules are the same
#         if source_module == target_module:
#             result['valid'] = True
#             result['confidence'] = 1.0
#             return result

#         if not self.analyzer_integration:
#             result['reason'] = "AnalyzerIntegration not available for verification."
#             return result

#         # Check if target_module imports component_name from source_module (directly or via *)
#         target_analysis = self.analyzer_integration.get_analysis_result(target_module)
#         if not target_analysis:
#             result['reason'] = f"Analysis results for target module '{target_module}' not found."
#             return result

#         imports_in_target = target_analysis.get("module_interface", {}).get("imports", [])
#         direct_import_found = False
#         for imp in imports_in_target:
#             # imp is now a dict from ImportInfo serialization
#             imp_source_fqn = imp.get('source_module_fqn')
#             imp_name = imp.get('imported_name')
#             imp_alias = imp.get('alias')
#             local_name = imp_alias or imp_name

#             # Check direct import: from source_module import component_name [as alias]
#             if imp_source_fqn == source_module and imp_name == component_name:
#                 direct_import_found = True
#                 break
#             # Check wildcard import: from source_module import *
#             # We need to know if component_name is exported by source_module
#             if imp.get('is_wildcard') and imp_source_fqn == source_module:
#                 source_analysis = self.analyzer_integration.get_analysis_result(source_module)
#                 if source_analysis:
#                     source_exports = self.api_resolver.get_module_exports(source_module) # Use API resolver's method
#                     if component_name in source_exports:
#                         direct_import_found = True
#                         break
#             # Check import module: import source_module [as alias]
#             # Then check if target uses alias.component_name or source_module.component_name
#             # This requires checking the *usage* within target_module's code (AST), which is complex here.
#             # Let's rely on direct/wildcard for now.

#         if direct_import_found:
#             result['valid'] = True
#             result['confidence'] = 0.9
#             return result

#         # Check for transitive import path (using API Resolver chains)
#         if self.api_resolver and hasattr(self.api_resolver, 'export_chains'):
#             # Check if a chain exists starting near source_module and ending at target_module
#             # This is complex. Simplified: Check if target_module is part of the chain for component_name originating from source_module
#             # Need the FQN of the component defined in source_module
#             source_fqn = f"{source_module}.{component_name}"
#             chain = self.api_resolver.export_chains.get(source_fqn, [])
#             if chain:
#                 chain_modules = {step.module_path for step in chain}
#                 if target_module in chain_modules:
#                     # Check if the export name matches in the target module step
#                     for step in chain:
#                         if step.module_path == target_module and step.export_name == component_name:
#                             result['valid'] = True
#                             result['confidence'] = 0.8
#                             return result

#         # Check if component is explicitly exported by target_module (using API Resolver)
#         if self.api_resolver:
#             target_exports = self.api_resolver.get_module_exports(target_module)
#             if component_name in target_exports:
#                 # We need to verify if this exported name *originates* from source_module
#                 # This requires tracing back, which is complex. Assume API resolver handles this.
#                 # Let's give it moderate confidence if exported.
#                 result['valid'] = True
#                 result['confidence'] = 0.7
#                 # Don't return yet, maybe other checks fail

#         # If no validation method succeeded
#         if not result['valid']:
#             result['reason'] = f"No verified import path from {source_module} to {target_module} for {component_name}"

#         return result

#     def _is_suspicious_path(self, new_path: str, original_path: str) -> bool:
#         """
#         Check if a path transformation seems suspicious and should be rejected.
#         """
#         self._update_component_refs()
#         # Ensure self.api_resolver is available
#         # Check if paths are the same
#         if new_path == original_path:
#             return False

#         # Extract key parts for analysis
#         if '.' not in new_path or '.' not in original_path:
#             return False # Cannot compare non-qualified paths meaningfully here

#         orig_pkg = original_path.split('.')[0]
#         new_pkg = new_path.split('.')[0]

#         # Check for package mismatch (suspicious)
#         if orig_pkg != new_pkg:
#             # Only allow if explicitly in API map
#             if self.api_resolver and hasattr(self.api_resolver, 'api_map') and original_path in self.api_resolver.api_map:
#                 if self.api_resolver.api_map[original_path] == new_path:
#                     return False # Explicitly mapped, allow package change
#             return True # Package changed without explicit mapping

#         # Check for repeated package segments (suspicious if longer)
#         new_parts = new_path.split('.')
#         if len(new_parts) >= 3 and new_parts[0] == new_parts[1]:
#             # e.g., package.package.module - usually indicates an error
#             return True

#         # Check if new path is significantly longer than original (suspicious)
#         # Allow one extra level for potential __init__ exposure
#         if new_path.count('.') > original_path.count('.') + 1:
#             # Only allow if explicitly in API map
#             if self.api_resolver and hasattr(self.api_resolver, 'api_map') and original_path in self.api_resolver.api_map:
#                 if self.api_resolver.api_map[original_path] == new_path:
#                     return False # Explicitly mapped, allow longer path
#             return True # Path became much longer without explicit mapping

#         return False

#     def _assess_correction_impact(self, original_path: str, corrected_path: str) -> Dict[str, Any]:
#         """
#         Assess the potential impact of a path correction.
#         """
#         self._update_component_refs()
#         impact = {
#             'risk_level': 'low', # low, medium, high
#             'risk_factors': [],
#             'affected_components': [], # List of FQNs that import the original_path
#             'recommended': True
#         }

#         # Check if original exists in API map (API Resolver)
#         if self.api_resolver and hasattr(self.api_resolver, 'api_map') and original_path in self.api_resolver.api_map:
#             impact['risk_level'] = 'medium'
#             impact['risk_factors'].append("Original path exists in API map")

#             # Check if correction conflicts with API map
#             api_path = self.api_resolver.api_map[original_path]
#             if api_path != corrected_path:
#                 impact['risk_level'] = 'high'
#                 impact['risk_factors'].append(f"Correction conflicts with API map: {api_path}")
#                 impact['recommended'] = False

#         # If correction changes package, that's higher risk
#         orig_pkg = original_path.split('.')[0] if '.' in original_path else original_path
#         corr_pkg = corrected_path.split('.')[0] if '.' in corrected_path else corrected_path

#         if orig_pkg != corr_pkg:
#             impact['risk_level'] = 'high'
#             impact['risk_factors'].append("Correction changes package")

#         # Check if correction makes path longer
#         if corrected_path.count('.') > original_path.count('.'):
#             impact['risk_level'] = max(impact['risk_level'], 'medium') # Use max to elevate risk
#             impact['risk_factors'].append("Correction increases path depth")

#         # Check for components that reference this path (AnalyzerIntegration)
#         affected_importers = []
#         if self.analyzer_integration and '.' in original_path:
#             original_module, component_name = original_path.rsplit('.', 1)
#             # Need a way to query AnalyzerIntegration/ImportTracker for incoming imports
#             # Placeholder: Assume analyzer_integration has a method get_importers(fqn)
#             # affected_importers = self.analyzer_integration.get_importers(original_path)
#             # Simpler approach: Iterate through all analysis results (less efficient)
#             if hasattr(self.analyzer_integration, 'file_analysis_results'):
#                 for mod_path, analysis_result in self.analyzer_integration.file_analysis_results.items():
#                     imports_in_mod = analysis_result.get("module_interface", {}).get("imports", [])
#                     for imp in imports_in_mod:
#                         imp_source_fqn = imp.get('source_module_fqn')
#                         imp_name = imp.get('imported_name')
#                         # Check if this module imports the original component from its original module
#                         if imp_source_fqn == original_module and imp_name == component_name:
#                             affected_importers.append(mod_path) # Add the importing module path

#         impact['affected_components'] = affected_importers

#         # If many components affected, higher risk
#         if len(impact['affected_components']) > 10:
#             impact['risk_level'] = max(impact['risk_level'], 'medium')
#             impact['risk_factors'].append(f"Affects {len(impact['affected_components'])} components")

#         if len(impact['affected_components']) > 30:
#             impact['risk_level'] = 'high'
#             impact['risk_factors'].append(f"Affects many components ({len(impact['affected_components'])})")

#         return impact

#     def get_statistics(self) -> Dict[str, Any]:
#         """Get statistics about path verification."""
#         return {
#             'stats': self.verification_stats,
#             'cache_size': len(self.verification_cache),
#             'corrections_tracked': len(self.correction_impacts),
#             'safe_mode': self.safe_mode
#         }

#     def toggle_safe_mode(self, enabled: bool = None) -> bool:
#         """
#         Toggle safe mode for path corrections.

#         Args:
#             enabled: Set to specific state, or toggle if None

#         Returns:
#             New safe mode state
#         """
#         if enabled is None:
#             self.safe_mode = not self.safe_mode
#         else:
#             self.safe_mode = enabled

#         return self.safe_mode
