"""
Module for integrating various analyzers and trackers.

This module provides functions to analyze Python code using the specialized trackers (ImportTracker, InheritanceTracker, CallGraphTracker) and making the results available to other components.
"""

import os
import logging
import fnmatch
import json
import subprocess
import tempfile
import importlib
import inspect
from typing import Set
from pathlib import Path
from collections import deque, defaultdict
from typing import Dict, List, Optional, Set, Tuple, Any, TYPE_CHECKING

from code_analysis.code_visitor import analyze_code
from code_analysis.dynamic_analyzer import DynamicAnalyzer
from code_analysis.graph.store import GraphStore
from code_analysis.graph.traversal import GraphTraversal
from code_analysis.graph.models import ImportRecord
from code_analysis.graph.importer import ImportTracker
from code_analysis.graph.exporter import ExportTracker
from code_analysis.graph.call_graph import CallGraphTracker
from code_analysis.graph.inheritance_tracker import InheritanceTracker
from code_analysis.inheritance_resolver import InheritanceResolver, ExternalIntrospector
from code_analysis.api_resolver import APIPathResolver
from code_analysis.definition_registry import DefinitionRegistry
from code_analysis.project_metadata import find_python_package_roots
from code_analysis.relationship_types import REL_TYPE_IMPORTS, REL_TYPE_INHERITS, REL_TYPE_CALLS, REL_TYPE_CONTAINS, NODE_TYPE_PACKAGE, NODE_TYPE_MODULE
from code_analysis.config import AnalysisConfig
from code_analysis.ir.models import IRModule
from code_analysis.ir.converter import convert_analysis_result_to_ir
from code_analysis.ir.cache import (generate_cache_key, get_cache_file_path, read_from_cache, write_to_cache)
from code_analysis.feature_flags import Feature, is_enabled 
from code_analysis.events import (
    EventPayload,
    FILE_CREATED,
    FILE_MODIFIED,
    FILE_DELETED,
    MODULE_ANALYSIS_INVALIDATED,
    MODULE_ANALYSIS_UPDATED,
    CHAIN_CANDIDATES_UPDATED,
    API_MAP_UPDATED
)

if TYPE_CHECKING:
    from code_analysis.mapcodocreg import MapCoDocRegistry
    


logger = logging.getLogger(__name__)



class AnalyzerIntegration:
    """
    Integrates various static and dynamic analysis capabilities for a codebase.
    Orchestrates parsing, graph building, statistics aggregation, chain candidate identification, and drives API path resolution for identified candidates.
    """
    
    COMPONENT_NAME = "analyzer_integration"
    DEPENDENCIES: Set[str] = {"definition_registry", "config_component"} 

    def __init__(self,
                 config: Optional[AnalysisConfig] = None,
                 registry: Optional['MapCoDocRegistry'] = None,
                 definition_registry: Optional['DefinitionRegistry'] = None):
        """Initialize the AnalyzerIntegration."""
        
        self.registry = registry
        self._initialized_event_handlers = False # Track if event handlers are set up
        
        # Configuration handling
        if config:
            self.config = config
        elif self.registry and hasattr(self.registry, 'get_component'):
            config_provider = self.registry.get_component('config_component')
            if isinstance(config_provider, AnalysisConfig):
                self.config = config_provider
            elif hasattr(config_provider, 'get_config') and callable(config_provider.get_config):
                self.config = config_provider.get_config()
                if not isinstance(self.config, AnalysisConfig): # Check type from provider
                    logger.error(f"{self.COMPONENT_NAME}: Config provider 'config_component' did not return AnalysisConfig. Using default.")
                    self.config = AnalysisConfig()
            else:
                self.config = AnalysisConfig()
                logger.warning(f"{self.COMPONENT_NAME}: Could not retrieve AnalysisConfig from 'config_component' of type {type(config_provider)}. Using default.")
        else:
            self.config = AnalysisConfig()
            logger.warning(f"{self.COMPONENT_NAME}: No explicit config or registry for config retrieval. Using default.")

        
        # --- Conditional Instantiation of All Graph Components ---
        if is_enabled(Feature.GRAPH_ANALYSIS):
            logger.info("Graph analysis is ENABLED. Initializing GraphStore and Trackers.")
            # If a graph_store is passed from the registry, use it. Otherwise, create one.
            self.store: Optional[GraphStore] = GraphStore()
            self.traversal: Optional[GraphTraversal] = GraphTraversal(self.store)
            self.import_tracker: Optional[ImportTracker] = ImportTracker(self.store, self.traversal)
            self.export_tracker: Optional[ExportTracker] = ExportTracker(self.store, self.traversal)
            self.inheritance_tracker: Optional[InheritanceTracker] = InheritanceTracker(self.store, self.traversal)
            
            # The CALL_GRAPH_ANALYSIS flag is now a sub-condition of GRAPH_ANALYSIS
            if is_enabled(Feature.CALL_GRAPH_ANALYSIS):
                self.call_tracker: Optional[CallGraphTracker] = CallGraphTracker(self.store, self.traversal)
                logger.info("Call graph analysis is ENABLED.")
            else:
                self.call_tracker = None
                logger.info("Call graph analysis is DISABLED (within GRAPH_ANALYSIS).")
        else:
            logger.info("Graph analysis is DISABLED. GraphStore and all Trackers will not be initialized.")
            # Set all graph-related attributes to None
            self.store = None
            self.traversal = None
            self.import_tracker = None
            self.export_tracker = None
            self.inheritance_tracker = None
            self.call_tracker = None
        
        # DefinitionRegistry handling
        self.definition_registry: Optional['DefinitionRegistry'] = None
        if definition_registry:
            self.definition_registry = definition_registry
        elif self.registry and hasattr(self.registry, 'get_component'):
            # Attempt to get it now, but on_dependency_ready is the primary way if it's not ready yet
            def_reg_comp = self.registry.get_component(DefinitionRegistry.COMPONENT_NAME)
            if isinstance(def_reg_comp, DefinitionRegistry): # Check type if not TYPE_CHECKING
                self.definition_registry = def_reg_comp
            else:
                logger.debug(f"{self.COMPONENT_NAME}: DefinitionRegistry '{DefinitionRegistry.COMPONENT_NAME}' not immediately available or wrong type at __init__.")
        
        if not self.definition_registry:
            logger.info(f"{self.COMPONENT_NAME}: DefinitionRegistry not available at __init__. Expecting via on_dependency_ready.")
        
        # Per-session caches and state
        self.ir_cache: Dict[str, IRModule] = {} # In-memory IR cache, keyed by relative path
        self.file_analysis_results: Dict[str, Dict[str, Any]] = {} # stores results from analyze_code
        self.final_unlinked_exports: List[Dict[str, Any]] = []
        self.candidates_to_re_exporters: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"component_kind": None, "exporters": set(), "is_internal": bool()})
        self.module_results_by_fqn: Dict[str, Dict[str, Any]] = {} # module-indexed view for fast lookup in API resolver
        # Cache for finalized target_item_fqn values
        self._resolved_fqn_cache: Dict[str, str] = {}  # original_fqn -> resolved_fqn
        
        self.repo_path: Optional[str] = None
        self.python_package_roots: List[Path] = []
        self.top_level_packages: Set[str] = set()
        self.module_name_prefix: Optional[str] = None
        self.dynamic_analyzer: Optional[DynamicAnalyzer] = None
        self._api_resolver_instance: Optional['APIPathResolver'] = None
        self._external_introspector: Optional[ExternalIntrospector] = None
        # Compiled extension detection (populated during analysis)
        self._compiled_extension_imports: Set[str] = set()
        self._requires_target_package_install: bool = False
        self._target_package_installed: bool = False
        
        # Repo path logic: Config -> Registry -> (analyze_codebase path if still None)
        config_repo_path = getattr(self.config, 'repo_path', None)
        if config_repo_path:
            self.repo_path = os.path.abspath(config_repo_path)
            logger.debug(f"{self.COMPONENT_NAME}: repo_path set from config: {self.repo_path}")
        elif self.registry and hasattr(self.registry, 'repo_path') and self.registry.repo_path:
            # Assuming registry.repo_path is already an absolute Path object or string
            self.repo_path = str(Path(self.registry.repo_path).resolve()) # Ensure absolute string
            logger.debug(f"{self.COMPONENT_NAME}: repo_path set from registry: {self.repo_path}")
        
        logger.info(f"{self.COMPONENT_NAME} instance created (config {'set' if self.config else 'defaulted'}, def_reg {'set during init' if self.definition_registry else 'pending'}).")

    
    # --- Registry Interface Methods ---
    def get_state(self) -> Dict[str, Any]:
        """Returns the current state of the component for serialization or transfer."""
        return {
            "component_name": self.COMPONENT_NAME,
            "files_analyzed_count": len(self.file_analysis_results),
            "ir_cache_size": len(self.ir_cache),
            "repo_path": self.repo_path,
            "top_level_packages_count": len(self.top_level_packages),
            "event_handlers_initialized": self._initialized_event_handlers,
        }

    def sync_state(self, new_state: Dict[str, Any]) -> None:
        """
        Updates the component's state from an external source.
        This is for synchronizing state, e.g., in a distributed setup or after deserialization.
        """
        logger.info(f"{self.COMPONENT_NAME}: Syncing state (current implementation is basic).")
        if "repo_path" in new_state and isinstance(new_state["repo_path"], str):
            new_abs_path = os.path.abspath(new_state["repo_path"])
            if self.repo_path != new_abs_path:
                self.repo_path = new_abs_path
                logger.info(f"{self.COMPONENT_NAME}: Synced repo_path to {self.repo_path}. Consider cache invalidation.")
    
    
    def on_dependency_ready(self, dependency_name: str, dependency_instance: Any) -> None:
        """
        Called by the registry when a declared dependency is ready.
        """
        logger.info(f"{self.COMPONENT_NAME} received: dependency '{dependency_name}' ready.")
        
        if dependency_name == DefinitionRegistry.COMPONENT_NAME:
            if isinstance(dependency_instance, DefinitionRegistry):
                if not self.definition_registry:
                    self.definition_registry = dependency_instance
                    logger.info(f"{self.COMPONENT_NAME}: DefinitionRegistry set via on_dependency_ready.")
                elif self.definition_registry != dependency_instance: # Should ideally be same instance
                    logger.warning(f"{self.COMPONENT_NAME}: DefinitionRegistry already set, received different instance for {dependency_name}. Overwriting.")
                    self.definition_registry = dependency_instance # Or keep original based on policy
            else:
                logger.error(f"{self.COMPONENT_NAME}: Dependency '{dependency_name}' (expected DefinitionRegistry) is type {type(dependency_instance)}.")

        elif dependency_name == "config_component":
            resolved_config = None
            if isinstance(dependency_instance, AnalysisConfig):
                resolved_config = dependency_instance
            elif hasattr(dependency_instance, 'get_config') and callable(dependency_instance.get_config):
                provided_conf = dependency_instance.get_config()
                if isinstance(provided_conf, AnalysisConfig):
                    resolved_config = provided_conf
                else:
                    logger.error(f"{self.COMPONENT_NAME}: Config provider '{dependency_name}' get_config() returned type {type(provided_conf)}.")
            else:
                logger.error(f"{self.COMPONENT_NAME}: Dependency 'config_component' is type {type(dependency_instance)} and not/does not provide AnalysisConfig.")

            if resolved_config:
                if self.config == AnalysisConfig() or self.config is None: # If using default or none
                    self.config = resolved_config
                    logger.info(f"{self.COMPONENT_NAME}: AnalysisConfig set from '{dependency_name}' via on_dependency_ready.")
                    # If repo_path was not set and config has it now:
                    if not self.repo_path and hasattr(self.config, 'repo_path') and getattr(self.config, 'repo_path', None):
                        self.repo_path = os.path.abspath(self.config.repo_path)
                        logger.info(f"{self.COMPONENT_NAME}: repo_path updated from config received via on_dependency_ready: {self.repo_path}")
        
        self._check_and_perform_post_dependency_setup()
        
    def _check_and_perform_post_dependency_setup(self):
        if self.registry and self.config and self.config != AnalysisConfig() and self.definition_registry:
            if not self._initialized_event_handlers:
                logger.info(f"{self.COMPONENT_NAME}: Core dependencies (Registry, non-default Config, DefinitionRegistry) met. Ready for event handler setup by registry.")
        else:
            if not self._initialized_event_handlers:
                missing = [dep for dep, present in [("Registry", self.registry), ("Config", self.config and self.config != AnalysisConfig()), ("DefinitionRegistry", self.definition_registry)] if not present]
                logger.debug(f"{self.COMPONENT_NAME}: Still waiting for core dependencies for full setup: {missing}")
    
    
    @property
    def api_resolver(self) -> Optional['APIPathResolver']:
        """Lazily fetches and caches the APIPathResolver instance from the registry."""
        if self._api_resolver_instance is None and self.registry:
            api_resolver_comp = self.registry.get_component(APIPathResolver.COMPONENT_NAME) # Use component name string
            if isinstance(api_resolver_comp, APIPathResolver): # Use direct class for isinstance
                self._api_resolver_instance = api_resolver_comp
            elif api_resolver_comp is not None: # Found something, but wrong type
                logger.error(f"{self.COMPONENT_NAME}: Expected APIPathResolver from registry for '{APIPathResolver.COMPONENT_NAME}', got {type(api_resolver_comp)}.")
            # else: component not found, will log from get_component or here
            if not self._api_resolver_instance: # Redundant log if get_component logs, but safe
                logger.warning(f"{self.COMPONENT_NAME}: APIPathResolver component '{APIPathResolver.COMPONENT_NAME}' not found or not ready in registry.")
        return self._api_resolver_instance
    
    @property
    def external_introspector(self) -> Optional[ExternalIntrospector]:
        """Lazily creates and caches the ExternalIntrospector instance."""
        if self._external_introspector is None:
            # Use cache directory from config if available
            cache_dir = None
            if self.config and hasattr(self.config, 'external_introspection_cache_dir'):
                cache_dir = self.config.external_introspection_cache_dir
            elif self.config and hasattr(self.config, 'ir_cache_dir') and self.config.ir_cache_dir:
                # Fallback: use a subdirectory of the IR cache
                cache_dir = str(Path(self.config.ir_cache_dir) / "external_introspection")
            
            self._external_introspector = ExternalIntrospector(cache_dir=cache_dir)
            logger.info(f"Initialized ExternalIntrospector with cache_dir: {cache_dir}")
        return self._external_introspector

    def get_analysis_result(self, module_path: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves the cached analysis result dictionary for a given module path.

        Args:
            module_path: The fully qualified name of the module.

        Returns:
            The cached analysis result dictionary, or None if not found.
        """
        # Use module-indexed cache first
        if module_path in self.module_results_by_fqn:
            return self.module_results_by_fqn[module_path]
        # Fallback scan 
        for result in self.file_analysis_results.values():
            if result.get("module_name") == module_path: return result
        return None


    def initialize(self): # This is the method the registry calls
        """
        Perform post-registration initialization, like subscribing to events.
        Should be called after the component is registered with the registry and its direct dependencies (as per DEPENDENCIES) are ready.
        """
        if self._initialized_event_handlers:
            logger.debug(f"{self.COMPONENT_NAME} event handlers already initialized.")
            return

        logger.info(f"Initializing {self.COMPONENT_NAME} internal components (event handlers, etc.)...")
        # Ensure critical dependencies for handler setup are truly ready
        if self.registry and self.definition_registry and self.config and self.config != AnalysisConfig():
            self._register_event_handlers()
            self._initialized_event_handlers = True
            logger.info(f"{self.COMPONENT_NAME} event handlers setup complete.")
        else:
            missing = []
            if not self.registry: missing.append("Registry")
            if not self.definition_registry: missing.append("DefinitionRegistry")
            if not self.config or self.config == AnalysisConfig(): missing.append("Non-default Config")
            logger.error(f"{self.COMPONENT_NAME}: Cannot initialize event handlers, critical dependencies missing/default: {missing}.")


    def _register_event_handlers(self):
        """Register handlers for relevant events."""
        # This method is assumed to be called by self.initialize()
        logger.info(f"{self.COMPONENT_NAME}: (Private) Attempting to subscribe to events...")
        if not self.registry:
            logger.error(f"{self.COMPONENT_NAME}: No registry provided, cannot subscribe to events.")
            return

        if not hasattr(self.registry, 'subscribe_to_event'):
            logger.error(f"{self.COMPONENT_NAME}: Registry object lacks 'subscribe_to_event' method.")
            # Consider raising an error or handling gracefully
            raise AttributeError(f"Registry object passed to {self.COMPONENT_NAME} is missing 'subscribe_to_event' method.")

        try:
            self.registry.subscribe_to_event(FILE_CREATED, self._handle_file_created)
            self.registry.subscribe_to_event(FILE_MODIFIED, self._handle_file_modified)
            self.registry.subscribe_to_event(FILE_DELETED, self._handle_file_deleted)
            # Example: Subscribe to know when DefinitionRegistry is updated, if needed
            # self.registry.subscribe_to_event(DefinitionRegistry.DEFINITION_INVALIDATED_EVENT, self._handle_def_invalidated)
            logger.info(f"{self.COMPONENT_NAME}: Successfully subscribed to file and module events.")
        except Exception as e:
            logger.error(f"{self.COMPONENT_NAME}: Failed to subscribe to events: {e}", exc_info=True)

    
    def analyze_codebase(self, path: str) -> Dict[str, Any]:
        """
        Analyzes an entire codebase.
        1. Parses all files, populating trackers and collecting per-module stats & exports.
        2. Aggregates module statistics.
        3. Identifies final chain candidates (re-exported items).
        4. Triggers API path resolution for these candidates via APIPathResolver.
        
        Args:
            path: Path to the codebase
            
        Returns:
            Dictionary of analysis results including metrics and file details.
        """
        
        # Ensure repo_path is set and absolute. Prioritize existing, then from config, then from input path.
        if not self.repo_path:
            config_repo_path = getattr(self.config, 'repo_path', None)
            if config_repo_path:
                self.repo_path = os.path.abspath(config_repo_path)
                logger.info(f"{self.COMPONENT_NAME}: repo_path was not set, using from config: {self.repo_path}")
            elif path:
                self.repo_path = os.path.abspath(path)
                logger.info(f"{self.COMPONENT_NAME}: repo_path was not set, using from analyze_codebase argument: {self.repo_path}")
            else:
                logger.error(f"{self.COMPONENT_NAME}: repo_path could not be determined. Analysis cannot proceed.")
                return {"errors": [{"type": "ConfigurationError", "message": "Repository path not set."}]}

        if not os.path.exists(self.repo_path):
            logger.error(f"{self.COMPONENT_NAME}: Repository path does not exist: {self.repo_path}")
            return {"errors": [{"type": "ConfigurationError", "message": f"Repository path not found: {self.repo_path}"}]}
        if not os.path.isdir(self.repo_path):
            logger.error(f"{self.COMPONENT_NAME}: Repository path is not a directory: {self.repo_path}")
            return {"errors": [{"type": "ConfigurationError", "message": f"Repository path is not a directory: {self.repo_path}"}]}

        logger.info(f"Analyzing repository with effective repo_path: {self.repo_path}")
        
        self.top_level_packages = self._find_top_level_packages(self.repo_path) # Identify Top-Level Packages
        
        # ---------------------------------------------------------------------------
        # Framework-Agnostic Module Name Prefix Detection
        # ---------------------------------------------------------------------------
        self.module_name_prefix = None
        if self.registry and hasattr(self.registry, "get_project_metadata"):
            project_metadata = self.registry.get_project_metadata()
            project_name = project_metadata.get("name") if project_metadata else None
            
            # Detect Python package roots for multi-language repos
            repo_path_obj = Path(self.repo_path)
            self.python_package_roots = find_python_package_roots(repo_path_obj, project_name)
            logger.info(f"Detected Python package roots: {[str(r) for r in self.python_package_roots]}")
            
            if project_name:
                # Check 1: If project name is already a top-level package
                project_in_top_level = project_name in self.top_level_packages
                
                # Check 2: If any top-level package contains a subpackage named after the project
                project_as_subpackage = False
                for pkg_root in self.python_package_roots:
                    for top_pkg in self.top_level_packages:
                        subpkg_path = pkg_root / top_pkg / project_name / "__init__.py"
                        if subpkg_path.is_file():
                            project_as_subpackage = True
                            logger.debug(f"Found project '{project_name}' as subpackage in {top_pkg}")
                            break
                    if project_as_subpackage:
                        break
                
                # If project name not found in package structure, prefix modules with it
                if not project_in_top_level and not project_as_subpackage:
                    self.module_name_prefix = project_name
                    self.top_level_packages.add(project_name)  # For is_internal checks
                    logger.info(f"Using project name '{project_name}' as module prefix (not found in package structure)")
                else:
                    logger.debug(f"No prefix needed: project_in_top_level={project_in_top_level}, project_as_subpackage={project_as_subpackage}")
        else:
            logger.warning("Project/library metadata not found. Analysis may be incomplete.")
        
        python_files = self._find_python_files(self.repo_path)
        logger.info(f"Found {len(python_files)} Python files to analyze in {self.repo_path}")
        if not python_files: logger.warning(f"No Python files found in {self.repo_path} matching criteria. Analysis may be empty.")
        self._precompute_known_modules(python_files)
        self._build_package_module_graph(python_files, self.repo_path) # Populates graph with module/package structure
        
        # Initialize collections for aggregated results
        aggregated_module_statistics: Dict[str, Dict[str, Any]] = {}
        errors_in_codebase: List[Dict[str, Any]] = []
        
        # ---- Analyze each file and aggregate results incrementally ----
        for file_path in python_files:
            try:
                analysis_result_for_file = self.analyze_file(file_path) # static + dynamic merged internally
                
                module_name = analysis_result_for_file.get("module_name")
                if not module_name:
                    logger.warning(f"Skipping aggregation for file {file_path} as module_name is missing in results.")
                    if analysis_result_for_file.get("errors"):
                        errors_in_codebase.extend([{"file": file_path, "error": err.get("message", "Unknown error during aggregation"), "details": err} for err in analysis_result_for_file["errors"]])
                    continue

                if "module_statistics" in analysis_result_for_file:
                    aggregated_module_statistics[module_name] = analysis_result_for_file["module_statistics"]
                    # --- Update GraphStore module node immediately with these detailed stats ---
                    if self.store:
                        attributes_to_set = {
                            "statistics": analysis_result_for_file["module_statistics"],
                            "has_all": analysis_result_for_file.get("module_interface", {}).get("has_all", False),
                            "all_values": analysis_result_for_file.get("module_interface", {}).get("all_values")
                        }
                        self.store.update_node_attributes(module_name, attributes_to_set)
                                            
                        logger.debug(f"Updated graph node attributes for {module_name} with final stats and __all__ info.")
                                
                # Collect errors from this file's analysis
                if analysis_result_for_file.get("errors"):
                    abs_file_path = os.path.abspath(file_path)
                    rel_path_for_error = os.path.relpath(abs_file_path, self.repo_path) if self.repo_path and abs_file_path.startswith(self.repo_path) else os.path.basename(abs_file_path)
                    errors_in_codebase.extend([{"file": rel_path_for_error, "error": err.get("message", "Unknown analysis error"), "details": err} for err in analysis_result_for_file["errors"]])

            except Exception as e: 
                rel_path_for_error = os.path.relpath(file_path, path) if path else os.path.basename(file_path)
                errors_in_codebase.append({"file": rel_path_for_error, "error": f"Critical error in analyze_codebase loop for {file_path}: {str(e)}"})
                logger.error(f"Critical error in analyze_codebase loop for {rel_path_for_error}: {e}", exc_info=True)
        
        # First Pass: Finalize all target_item_fqn references
        self._finalize_all_target_fqns()
        
        # ---- Final linking phase for unresolved re-exports----
        if self.final_unlinked_exports:
            self._perform_final_iterative_resolution()
            
            # Second Pass: Finalize all target_item_fqn references
            self._finalize_all_target_fqns()
            
        # --- Finalize all import record FQNs to point to true definitions ---
        self._finalize_all_import_records()

        # --- Finalize all base_fqns using corrected import records ---
        self._finalize_all_base_fqns()
        
        # Resolve aggregated explicit exports for modules with dynamic __all__
        # This runs when dynamic analysis is disabled or as a fallback
        if not self.config.dynamic_all_check: self._resolve_aggregated_all_exports()
        
        logger.info(f"Aggregated statistics for {len(aggregated_module_statistics)} modules.")
        
        # Resolve inheritance for classes
        self._resolve_inherited_members()
        # Discover methods from external base classes (requires external libraries to be importable)
        if self.config.discover_external_methods:
            self._discover_external_inherited_methods()
            
        # Discover runtime-injected members (delegated, metaclass, etc.)
        if getattr(self.config, 'discover_runtime_members', True) and self._target_package_installed:
            self._discover_runtime_injected_members()
            
        # Discover accessor chain members (Class.accessor.method patterns)
        if getattr(self.config, 'discover_accessor_chains', True) and self._target_package_installed:
            self._discover_accessor_chain_members()
        
        # Build candidates map using corrected FQNs
        self._build_chain_candidates_map()
        
        # ---- API Path Resolution Orchestration ----
        if self.api_resolver:
            self.api_resolver.set_aggregated_module_statistics(aggregated_module_statistics)
            self.drive_api_path_resolution()
        else:
            logger.warning("APIPathResolver component not found. Candidate API paths will not be resolved.")

        return {
            "project_metadata": project_metadata,
            "metrics": self._calculate_metrics(), 
            "files_analyzed": len(python_files), 
            "analysis_details": self.file_analysis_results.copy(), 
            "errors": errors_in_codebase
        }
    

    def _resolve_inherited_members(self):
        """
        Post-analysis step: resolve inherited members for all classes.
        
        This must run AFTER all files are analyzed, as parent classes might be defined in different modules.
        """
        resolver = InheritanceResolver(
            file_analysis_results=self.file_analysis_results,
            top_level_packages=self.top_level_packages,
        )
        resolver.update_analysis_results()
        logger.info("Completed inherited member resolution for all classes")
    
    def _discover_external_inherited_methods(self) -> None:
        """
        Dynamically introspect external base classes to discover their methods.
        
        Uses ExternalIntrospector to safely install external libraries in a 
        temporary virtual environment, introspect them, and clean up afterward.
        
        The method:
        1. Collects all external_bases across all classes
        2. Uses ExternalIntrospector for isolated introspection
        3. Distributes discovered methods to appropriate classes
        4. Respects MRO priority (doesn't override existing inherited methods)
        """
        logger.info("Discovering methods from external base classes via ExternalIntrospector...")
        
        if not self.external_introspector:
            logger.warning("ExternalIntrospector not available. Skipping external method discovery.")
            return
        
        # Step 1: Collect all external bases and their inheriting classes
        # Map: external_base_fqn -> list of {class_fqn, already_have, all_external_bases}
        external_bases_to_classes = defaultdict(list)
        
        # Also build a per-class mapping to track all external bases for dynamic introspection
        class_to_all_external_bases: Dict[str, List[str]] = {}
        
        for file_key, mod_result in self.file_analysis_results.items():
            if file_key in ("metrics", "errors"): continue
            
            components = mod_result.get("components", {})
            
            for comp_fqn, comp_data in components.items():
                if comp_data.get("component_kind") != "class": continue
                
                external_bases = comp_data.get("external_bases", [])
                if not external_bases: continue
                
                # Collect ALL methods this class already has from static analysis
                # This includes: own methods, inherited from internal bases, constructor
                existing_inherited = comp_data.get("inherited_methods", {})
                own_methods = {m.get("name", "") for m in comp_data.get("methods", [])}
                
                # Also include constructor if present
                constructor = comp_data.get("constructor")
                if constructor: own_methods.add(constructor.get("name", "__init__"))
                
                # Combine all statically-known methods
                already_have = set(existing_inherited.keys()) | own_methods
                
                # Track all external bases for this class
                class_to_all_external_bases[comp_fqn] = external_bases
                
                for base_fqn in external_bases:
                    external_bases_to_classes[base_fqn].append({
                        'class_fqn': comp_fqn,
                        'already_have': already_have
                    })
        
        if not external_bases_to_classes: logger.debug("No external bases found to introspect."); return
        
        # Step 2: Use ExternalIntrospector to get methods from all unique external bases
        all_external_bases = list(external_bases_to_classes.keys())
        logger.info(f"Introspecting {len(all_external_bases)} unique external base classes...")
        
        # Get union of all "already_have" sets to exclude common methods
        global_already_have: Set[str] = set()
        for _, classes_info in external_bases_to_classes.items():
            for info in classes_info:
                global_already_have.update(info['already_have'])
        
        # Introspect all external bases at once
        all_discovered = self.external_introspector.introspect_external_bases(
            external_bases=all_external_bases,
            already_have=set()  # filter per-class below
        )
        
        # Step 3: Distribute discovered methods to appropriate classes
        total_added = 0
        classes_updated = 0
        
        for file_key, mod_result in self.file_analysis_results.items():
            if file_key in ("metrics", "errors"): continue
            
            components = mod_result.get("components", {})
            for comp_fqn, comp_data in components.items():
                if comp_data.get("component_kind") != "class": continue
                
                external_bases = comp_data.get("external_bases", [])
                if not external_bases: continue
                
                # Get what this class already has
                existing_inherited = comp_data.get("inherited_methods", {})
                own_methods = {m.get("name") for m in comp_data.get("methods", [])}
                already_have = set(existing_inherited.keys()) | own_methods
                
                added_for_class = 0
                
                for base_fqn in external_bases:
                    # Skip builtins
                    if base_fqn in ('object', 'type', 'builtins.object'): continue
                    
                    # Get methods for this specific base
                    base_methods = all_discovered.get(base_fqn, {})
                    
                    for method_name, method_info in base_methods.items():
                        if method_name in already_have: continue
                        
                        # Initialize inherited_methods if needed
                        if "inherited_methods" not in comp_data:
                            comp_data["inherited_methods"] = {}
                        
                        # Add method with inheriting class context
                        enriched_info = method_info.copy()
                        enriched_info["inheriting_class_fqn"] = comp_fqn
                        enriched_info["inheriting_class_api_name"] = None  # Set later by propagation
                        enriched_info["inherited_api_name"] = None
                        enriched_info["inherited_api_names"] = []
                        
                        comp_data["inherited_methods"][method_name] = enriched_info
                        already_have.add(method_name)  # Prevent duplicates from other bases
                        added_for_class += 1
                
                if added_for_class > 0:
                    classes_updated += 1
                    total_added += added_for_class
                    logger.debug(f"Added {added_for_class} external methods to {comp_fqn}")
        
        # Step 4: Batch discover dynamically-generated methods on inheriting classes
        logger.info("Discovering dynamically-generated methods from external inheritance...")
        
        # Collect all introspection requests (avoiding duplicates)
        introspection_requests = []
        processed_classes: Set[str] = set()
        
        for base_fqn, inheritors_info in external_bases_to_classes.items():
            for info in inheritors_info:
                class_fqn = info['class_fqn']
                if class_fqn in processed_classes:
                    continue
                processed_classes.add(class_fqn)
                
                introspection_requests.append({
                    'class_fqn': class_fqn,
                    'external_base_fqns': class_to_all_external_bases.get(class_fqn, []),
                    'already_have': info['already_have']
                })
        
        # Batch introspect ALL classes at once (single venv per package group)
        if introspection_requests:
            batch_results = self.external_introspector.introspect_dynamic_methods_batch(introspection_requests)
            
            # Update analysis results with discovered methods
            for class_fqn, dynamic_methods in batch_results.items():
                if not dynamic_methods:
                    continue
                
                logger.info(f"Found {len(dynamic_methods)} dynamic methods on {class_fqn}")
                total_added += len(dynamic_methods)
                classes_updated += 1
                
                # Find and update class in analysis results
                for file_key, mod_data in self.file_analysis_results.items():
                    if file_key in ("metrics", "errors"): continue
                    
                    components = mod_data.get("components", {})
                    class_data = components.get(class_fqn)
                    
                    if class_data:
                        existing = class_data.get("inherited_methods", {})
                        class_api_name = class_data.get("primary_api_name") or class_fqn
                        
                        for method_name, method_info in dynamic_methods.items():
                            if method_name not in existing:
                                method_info["inheriting_class_fqn"] = class_fqn
                                method_info["inheriting_class_api_name"] = class_api_name
                                method_info["inherited_api_name"] = f"{class_api_name}.{method_name}"
                                method_info["inherited_api_names"] = [
                                    f"{class_api_name}.{method_name}",
                                    f"{class_fqn}.{method_name}"
                                ]
                                existing[method_name] = method_info
                        
                        class_data["inherited_methods"] = existing
                        break
                
        if total_added > 0:
            logger.info(f"Discovered {total_added} methods from external bases across {classes_updated} classes")
        else:
            logger.debug("No external methods discovered (libraries may not be installable)")
    
    def _discover_runtime_injected_members(self) -> None:
        """
        Discover methods that are available on classes at runtime but weren't
        detected by static analysis (delegated methods, metaclass-injected, etc.).
        
        This is framework-agnostic - it inspects what's actually available on
        a class at runtime, regardless of how the methods were added.
        
        Prerequisites:
        - Target package must be installed (_target_package_installed = True)
        - Dynamic analyzer venv must be available
        """
        if not self._target_package_installed:
            logger.debug("Skipping runtime member discovery - target package not installed")
            return
        
        if not self.dynamic_analyzer or not self.dynamic_analyzer.venv:
            logger.debug("Skipping runtime member discovery - no dynamic analyzer venv")
            return
        
        logger.info("Discovering runtime-injected members for classes...")
        
        # Collect classes to inspect (only from main package)
        classes_to_inspect = []
        main_package = None
        
        # Get main package name
        if self.registry and hasattr(self.registry, "get_project_metadata"):
            project_metadata = self.registry.get_project_metadata()
            if project_metadata:
                main_package = project_metadata.get("name", "").replace('-', '_')
        
        if not main_package and self.top_level_packages:
            main_package = next(iter(self.top_level_packages))
        
        if not main_package:
            logger.debug("No main package identified for runtime member discovery")
            return
        
        for file_key, mod_result in self.file_analysis_results.items():
            if file_key in ("metrics", "errors"):
                continue
            
            components = mod_result.get("components", {})
            for comp_fqn, comp_data in components.items():
                if comp_data.get("component_kind") != "class":
                    continue
                
                # Only inspect classes from the main package
                if not comp_fqn.startswith(main_package + ".") and not comp_fqn.startswith(main_package):
                    continue
                
                # Get API name for this class
                api_name = comp_data.get("API_name")
                if not api_name or not api_name.startswith(main_package):
                    continue
                
                # Collect statically known methods
                static_methods = set()
                for m in comp_data.get("methods", []):
                    if m.get("name"):
                        static_methods.add(m["name"])
                # Include inherited methods
                for name in comp_data.get("inherited_methods", {}).keys():
                    static_methods.add(name)
                # Include constructor
                constructor = comp_data.get("constructor")
                if constructor and constructor.get("name"):
                    static_methods.add(constructor["name"])
                
                classes_to_inspect.append({
                    'fqn': comp_fqn,
                    'api_name': api_name,
                    'static_methods': static_methods,
                })
        
        if not classes_to_inspect:
            logger.debug("No classes found to inspect for runtime members")
            return
        
        logger.info(f"Inspecting {len(classes_to_inspect)} classes for runtime-injected members...")
        
        # Generate and run introspection script
        script = self._generate_runtime_discovery_script(classes_to_inspect, main_package)
        discovered = self._run_runtime_discovery_script(script)
        
        if not discovered:
            logger.debug("No runtime members discovered")
            return
        
        # Merge discovered members into analysis results
        self._merge_runtime_discovered_members(discovered, classes_to_inspect)


    def _generate_runtime_discovery_script(self, classes_to_inspect: List[Dict], main_package: str) -> str:
        """Generate script to discover runtime-available methods."""
    
        # Build class list for the script
        class_entries = []
        for info in classes_to_inspect:
            api_name = info['api_name']
            parts = api_name.rsplit('.', 1)
            if len(parts) == 2:
                module_name, class_name = parts
                class_entries.append(f'    ("{module_name}", "{class_name}", "{info["fqn"]}"),')
        
        class_list_code = "classes_to_check = [\n" + "\n".join(class_entries) + "\n]"
        
        script = f'''import json
import sys
import importlib

results = {{}}

def get_method_origin(cls, method_name):
    """Find which class in the MRO defines this method."""
    for base in cls.__mro__:
        if base is object:
            continue
        if method_name in vars(base):
            return f"{{base.__module__}}.{{base.__name__}}"
    return None

def is_callable_method(attr):
    """Check if attribute is a callable method or property."""
    if callable(attr):
        return True
    if isinstance(attr, property):
        return True
    # Check for property-like descriptors
    if hasattr(type(attr), '__get__'):
        return True
    return False

def discover_class_members(module_name, class_name, class_fqn):
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name, None)
        if cls is None:
            return {{"error": f"Class {{class_name}} not found in {{module_name}}"}}
        
        members = []
        for name in dir(cls):
            # Skip private/dunder
            if name.startswith('_'):
                continue
            
            try:
                attr = getattr(cls, name)
            except Exception:
                continue
            
            # Only include methods and properties
            if not is_callable_method(attr):
                continue
            
            # Determine member type
            if isinstance(attr, property) or (hasattr(type(attr), '__get__') and not callable(attr)):
                member_type = 'property'
            else:
                member_type = 'method'
            
            # Find origin
            origin = get_method_origin(cls, name)
            attr_module = getattr(attr, '__module__', None)
            
            members.append({{
                'name': name,
                'type': member_type,
                'origin': origin,
                'attr_module': attr_module,
            }})
        
        return members
    except Exception as e:
        return {{"error": str(e)}}

# Classes to inspect
{class_list_code}

# Inspect each class
for module_name, class_name, class_fqn in classes_to_check:
    result = discover_class_members(module_name, class_name, class_fqn)
    results[class_fqn] = result

print(json.dumps(results))
'''
        return script

    def _run_runtime_discovery_script(self, script: str) -> Optional[Dict[str, Any]]:
        """Run the runtime discovery script in the dynamic analyzer's venv."""
        python_exe = self.dynamic_analyzer.venv.effective_python_executable
        if not python_exe:
            logger.warning("No Python executable available for runtime discovery")
            return None
        
        try:
            # Write script to temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='_runtime_discover.py', delete=False) as f:
                f.write(script)
                script_path = f.name
            
            # Run script
            result = subprocess.run(
                [str(python_exe), script_path],
                capture_output=True,
                text=True,
                timeout=120,  # 2 minute timeout for large codebases
            )
            
            # Clean up
            try:
                os.unlink(script_path)
            except:
                pass
            
            if result.returncode != 0:
                logger.warning(f"Runtime discovery script failed: {result.stderr[:500]}")
                return None
            
            # Parse JSON output
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse runtime discovery output: {e}")
                logger.debug(f"Script output: {result.stdout[:1000]}")
                return None
                
        except subprocess.TimeoutExpired:
            logger.warning("Runtime discovery script timed out")
            return None
        except Exception as e:
            logger.warning(f"Runtime discovery error: {e}")
            return None


    def _merge_runtime_discovered_members(self, discovered: Dict[str, Any], classes_info: List[Dict]) -> None:
        """
        Add runtime-discovered members to analysis results as inherited_methods.
        
        This reuses the existing inherited_methods infrastructure, just with
        an 'is_runtime_discovered' flag to distinguish them.
        """
        fqn_to_static = {info['fqn']: info['static_methods'] for info in classes_info}
        fqn_to_api = {info['fqn']: info.get('api_name') for info in classes_info}
        
        total_discovered = 0
        classes_updated = 0
        
        for file_key, mod_result in self.file_analysis_results.items():
            if file_key in ("metrics", "errors"):
                continue
            
            components = mod_result.get("components", {})
            for comp_fqn, comp_data in components.items():
                if comp_fqn not in discovered:
                    continue
                
                runtime_members = discovered[comp_fqn]
                
                if isinstance(runtime_members, dict) and 'error' in runtime_members:
                    logger.debug(f"Skipping {comp_fqn}: {runtime_members['error']}")
                    continue
                
                if not isinstance(runtime_members, list):
                    continue
                
                static_names = fqn_to_static.get(comp_fqn, set())
                class_api_name = fqn_to_api.get(comp_fqn) or comp_data.get("API_name")
                
                # Ensure inherited_methods exists
                if "inherited_methods" not in comp_data:
                    comp_data["inherited_methods"] = {}
                
                for member in runtime_members:
                    member_name = member.get('name')
                    if not member_name or member_name in static_names:
                        continue
                    
                    # Skip if already in inherited_methods
                    if member_name in comp_data["inherited_methods"]:
                        continue
                    
                    # Build derived API name
                    derived_api_name = f"{class_api_name}.{member_name}" if class_api_name else None
                    
                    # Add as inherited member (reusing existing structure)
                    comp_data["inherited_methods"][member_name] = {
                        "name": member_name,
                        "member_type": member.get('type', 'method'),
                        "source_class_fqn": member.get('origin'),
                        "original_fqn": f"{member.get('origin')}.{member_name}" if member.get('origin') else None,
                        "is_external": False,  # Internal to target package
                        "is_runtime_discovered": True,  # Distinguishing flag
                        "discovery_method": "runtime_introspection",
                        "inherited_api_name": derived_api_name,
                        "inherited_api_names": [derived_api_name] if derived_api_name else [],
                        "inheriting_class_fqn": comp_fqn,
                        "inheriting_class_api_name": class_api_name,
                    }
                    
                    total_discovered += 1
                
                if total_discovered > 0:
                    classes_updated += 1
        
        if total_discovered > 0:
            logger.info(f"Added {total_discovered} runtime-discovered members to inherited_methods across {classes_updated} classes")
    
    def _discover_accessor_chain_members(self) -> None:
        """
        Discover accessor chain methods (e.g., DataFrame.plot.area, Series.str.contains).
        
        Framework-agnostic approach:
        1. Find properties on classes that return accessor objects
        2. Inspect the accessor object's methods
        3. Create composite API names (OwnerClass.accessor.method)
        
        Prerequisites:
        - Target package must be installed (_target_package_installed = True)
        - Dynamic analyzer venv must be available
        """
        if not self._target_package_installed:
            logger.debug("Skipping accessor chain discovery - target package not installed")
            return
        
        if not self.dynamic_analyzer or not self.dynamic_analyzer.venv:
            logger.debug("Skipping accessor chain discovery - no dynamic analyzer venv")
            return
        
        logger.info("Discovering accessor chain members for classes...")
        
        # Get main package name
        main_package = None
        if self.registry and hasattr(self.registry, "get_project_metadata"):
            project_metadata = self.registry.get_project_metadata()
            if project_metadata:
                main_package = project_metadata.get("name", "").replace('-', '_')
        
        if not main_package and self.top_level_packages:
            main_package = next(iter(self.top_level_packages))
        
        if not main_package:
            logger.debug("No main package identified for accessor chain discovery")
            return
        
        # Collect classes with their API names
        classes_to_inspect = []
        for file_key, mod_result in self.file_analysis_results.items():
            if file_key in ("metrics", "errors"):
                continue
            
            components = mod_result.get("components", {})
            for comp_fqn, comp_data in components.items():
                if comp_data.get("component_kind") != "class":
                    continue
                
                api_name = comp_data.get("API_name")
                if not api_name or not api_name.startswith(main_package):
                    continue
                
                # Get existing method names to avoid duplicates
                existing_methods = set()
                for m in comp_data.get("methods", []):
                    if m.get("name"):
                        existing_methods.add(m["name"])
                for name in comp_data.get("inherited_methods", {}).keys():
                    existing_methods.add(name)
                
                classes_to_inspect.append({
                    'fqn': comp_fqn,
                    'api_name': api_name,
                    'existing_methods': existing_methods,
                })
        
        if not classes_to_inspect:
            logger.debug("No classes found for accessor chain discovery")
            return
        
        logger.info(f"Inspecting {len(classes_to_inspect)} classes for accessor chains...")
        
        # Generate and run accessor discovery script
        script = self._generate_accessor_discovery_script(classes_to_inspect, main_package)
        discovered = self._run_accessor_discovery_script(script)
        
        if not discovered:
            logger.debug("No accessor chains discovered")
            return
        
        # Merge discovered accessor members into analysis results
        self._merge_accessor_chain_members(discovered, classes_to_inspect)

    def _generate_accessor_discovery_script(self, classes_to_inspect: List[Dict], main_package: str) -> str:
        """Generate script to discover accessor chain methods."""
    
        # Build class list for the script
        class_entries = []
        for info in classes_to_inspect:
            api_name = info['api_name']
            parts = api_name.rsplit('.', 1)
            if len(parts) == 2:
                module_name, class_name = parts
                class_entries.append(f'    ("{module_name}", "{class_name}", "{info["fqn"]}", "{api_name}"),')
        
        class_list_code = "classes_to_check = [\n" + "\n".join(class_entries) + "\n]"
        
        script = f'''import json
import importlib

results = {{}}

def get_accessor_class(cls, attr_name):
    """
    Get the accessor class for a descriptor attribute.
    Framework-agnostic: handles property, CachedAccessor, and other descriptors.
    """
    try:
        descriptor = getattr(type(cls), attr_name, None)
        if descriptor is None:
            return None
        
        # Case 1: Regular property with return annotation
        if isinstance(descriptor, property):
            if descriptor.fget and hasattr(descriptor.fget, '__annotations__'):
                ret_type = descriptor.fget.__annotations__.get('return')
                if ret_type and isinstance(ret_type, type):
                    return ret_type
            return None
        
        # Case 2: CachedAccessor or similar (has _accessor attribute)
        if hasattr(descriptor, '_accessor'):
            return descriptor._accessor
        
        # Case 3: Descriptor with cls attribute (common pattern)
        if hasattr(descriptor, 'cls'):
            return descriptor.cls
        
        # Case 4: Check if it's a descriptor class itself
        if hasattr(descriptor, '__get__') and hasattr(type(descriptor), '__mro__'):
            # Try to get the accessor class from the descriptor type
            desc_type = type(descriptor)
            if hasattr(desc_type, '_accessor'):
                return getattr(desc_type, '_accessor', None)
        
        return None
    except Exception:
        return None


def get_accessor_methods(accessor_cls):
    """Get public methods/properties from an accessor class."""
    if accessor_cls is None:
        return []
    
    methods = []
    try:
        for name in dir(accessor_cls):
            if name.startswith('_'):
                continue
            try:
                attr = getattr(accessor_cls, name, None)
                if attr is None:
                    continue
                # Include methods, classmethods, staticmethods, properties
                if callable(attr) or isinstance(attr, property):
                    methods.append(name)
            except:
                continue
    except:
        pass
    
    return methods


def discover_accessors(module_name, class_name, class_fqn, class_api_name):
    """Discover accessor patterns on a class."""
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name, None)
        if cls is None:
            return {{"error": f"Class {{class_name}} not found"}}
        
        accessor_members = []
        
        # Find all potential accessor attributes on the class
        for attr_name in dir(type(cls)):
            if attr_name.startswith('_'):
                continue
            
            # Get the accessor class
            accessor_cls = get_accessor_class(cls, attr_name)
            if accessor_cls is None:
                continue
            
            # Get methods from the accessor
            accessor_methods = get_accessor_methods(accessor_cls)
            if len(accessor_methods) < 2:
                # Needs at least 2 methods to be considered an accessor
                continue
            
            # Get accessor class info
            accessor_module = getattr(accessor_cls, '__module__', '')
            accessor_class_name = getattr(accessor_cls, '__name__', '')
            accessor_fqn = f"{{accessor_module}}.{{accessor_class_name}}" if accessor_module and accessor_class_name else None
            
            # Create entries for each accessor method
            for method_name in accessor_methods:
                try:
                    method_attr = getattr(accessor_cls, method_name, None)
                    member_type = 'property' if isinstance(method_attr, property) else 'method'
                    
                    accessor_members.append({{
                        'accessor_name': attr_name,
                        'method_name': method_name,
                        'composite_name': f"{{attr_name}}.{{method_name}}",
                        'member_type': member_type,
                        'accessor_class_fqn': accessor_fqn,
                        'original_fqn': f"{{accessor_fqn}}.{{method_name}}" if accessor_fqn else None,
                    }})
                except:
                    continue
        
        return accessor_members
    except Exception as e:
        return {{"error": str(e)}}

# Classes to inspect
{class_list_code}

# Discover accessors for each class
for module_name, class_name, class_fqn, class_api_name in classes_to_check:
    result = discover_accessors(module_name, class_name, class_fqn, class_api_name)
    if result:
        results[class_fqn] = {{
            'class_api_name': class_api_name,
            'accessor_members': result
        }}

print(json.dumps(results))
'''
        return script

    def _run_accessor_discovery_script(self, script: str) -> Optional[Dict[str, Any]]:
        """Run the accessor discovery script in the dynamic analyzer's venv."""
        python_exe = self.dynamic_analyzer.venv.effective_python_executable
        if not python_exe:
            logger.warning("No Python executable available for accessor discovery")
            return None
        
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='_accessor_discover.py', delete=False) as f:
                f.write(script)
                script_path = f.name
            
            result = subprocess.run(
                [str(python_exe), script_path],
                capture_output=True,
                text=True,
                timeout=180,  # 3 minute timeout - accessor discovery may take longer
            )
            
            try:
                os.unlink(script_path)
            except:
                pass
            
            if result.returncode != 0:
                logger.warning(f"Accessor discovery script failed: {result.stderr[:500]}")
                return None
            
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse accessor discovery output: {e}")
                logger.debug(f"Script stdout: {result.stdout[:1000]}")
                return None
                
        except subprocess.TimeoutExpired:
            logger.warning("Accessor discovery script timed out")
            return None
        except Exception as e:
            logger.warning(f"Accessor discovery error: {e}")
            return None


    def _merge_accessor_chain_members(
        self, 
        discovered: Dict[str, Any], 
        classes_info: List[Dict]
    ) -> None:
        """
        Add accessor chain members to analysis results as inherited_methods.
        
        Creates composite API names like 'pandas.DataFrame.plot.area'.
        """
        fqn_to_existing = {info['fqn']: info['existing_methods'] for info in classes_info}
        
        total_discovered = 0
        classes_updated = 0
        
        for file_key, mod_result in self.file_analysis_results.items():
            if file_key in ("metrics", "errors"):
                continue
            
            components = mod_result.get("components", {})
            for comp_fqn, comp_data in components.items():
                if comp_fqn not in discovered:
                    continue
                
                class_info = discovered[comp_fqn]
                accessor_members = class_info.get('accessor_members', [])
                class_api_name = class_info.get('class_api_name')
                
                if isinstance(accessor_members, dict) and 'error' in accessor_members:
                    logger.debug(f"Skipping {comp_fqn}: {accessor_members['error']}")
                    continue
                
                if not isinstance(accessor_members, list):
                    continue
                
                existing_methods = fqn_to_existing.get(comp_fqn, set())
                
                # Ensure inherited_methods exists
                if "inherited_methods" not in comp_data:
                    comp_data["inherited_methods"] = {}
                
                class_had_additions = False
                for member in accessor_members:
                    composite_name = member.get('composite_name')  # e.g., "plot.area"
                    if not composite_name:
                        continue
                    
                    # Skip if already exists
                    if composite_name in existing_methods or composite_name in comp_data["inherited_methods"]:
                        continue
                    
                    # Build composite API name (e.g., pandas.DataFrame.plot.area)
                    composite_api_name = f"{class_api_name}.{composite_name}" if class_api_name else None
                    
                    comp_data["inherited_methods"][composite_name] = {
                        "name": composite_name,
                        "member_type": member.get('member_type', 'method'),
                        "source_class_fqn": member.get('accessor_class_fqn'),
                        "original_fqn": member.get('original_fqn'),
                        "is_external": False,
                        "is_runtime_discovered": True,
                        "discovery_method": "accessor_introspection",
                        "inherited_api_name": composite_api_name,
                        "inherited_api_names": [composite_api_name] if composite_api_name else [],
                        "inheriting_class_fqn": comp_fqn,
                        "inheriting_class_api_name": class_api_name,
                        "accessor_name": member.get('accessor_name'),  # e.g., "plot"
                        "accessor_method": member.get('method_name'),  # e.g., "area"
                    }
                    
                    total_discovered += 1
                    class_had_additions = True
                
                if class_had_additions:
                    classes_updated += 1
        
        if total_discovered > 0:
            logger.info(f"Added {total_discovered} accessor chain members across {classes_updated} classes")
    
    def _resolve_aggregated_all_exports(self) -> None:
        """
        Post-processing pass to resolve exports for modules with aggregated __all__ patterns when dynamic analysis is disabled.
        
        For modules like numpy.__init__.py that have:
            __all__ = list(set(lib._shape_base_impl.__all__) | set(fft.__all__) | ...)
        
        This method:
        1. Identifies modules with needs_dynamic_analysis=True and all_aggregation_sources
        2. Resolves the aggregation source references to actual module FQNs
        3. Pulls exports from those source modules
        4. Creates export records in the aggregating module
        """
        logger.info("Starting aggregated __all__ resolution pass (dynamic analysis disabled)...")
        
        modules_to_process: List[str] = []
        
        # Step 1: Identify modules that need processing
        for mod_fqn, mod_result in self.module_results_by_fqn.items():
            module_interface = mod_result.get("module_interface", {})
            if (module_interface.get("needs_dynamic_analysis") and 
                module_interface.get("all_is_dynamic") and
                module_interface.get("all_aggregation_sources")):
                modules_to_process.append(mod_fqn)
        
        if not modules_to_process:
            logger.info("No modules with aggregated __all__ patterns found.")
            return
        
        logger.info(f"Processing {len(modules_to_process)} modules with aggregated __all__: {modules_to_process}")
        
        for aggregator_fqn in modules_to_process:
            self._expand_aggregated_all_for_module(aggregator_fqn)

    
    def _expand_aggregated_all_for_module(self, aggregator_fqn: str) -> None:
        """
        Expand exports for a single module that aggregates __all__ from submodules.
        """
        mod_result = self.get_analysis_result(aggregator_fqn)
        if not mod_result:
            return
        
        module_interface = mod_result.get("module_interface", {})
        aggregation_sources = module_interface.get("all_aggregation_sources", [])
        
        # Get the package prefix for resolving relative references
        # e.g., for 'numpy', local refs like 'lib._shape_base_impl' become 'numpy.lib._shape_base_impl'
        package_prefix = aggregator_fqn.rsplit('.', 1)[0] if '.' in aggregator_fqn else aggregator_fqn
        
        expanded_exports: List[Dict[str, Any]] = []
        all_values_collected: Set[str] = set()
        
        for source_ref in aggregation_sources:
            # Resolve the reference to a full module FQN
            source_fqn = self._resolve_aggregation_source(source_ref, aggregator_fqn, package_prefix)
            
            if not source_fqn:
                logger.warning(f"Could not resolve aggregation source '{source_ref}' in {aggregator_fqn}")
                continue
            
            # Get the source module's exports
            source_result = self.get_analysis_result(source_fqn)
            if not source_result:
                logger.warning(f"Source module '{source_fqn}' not found for aggregated __all__ in {aggregator_fqn}")
                continue
            
            source_all_values = source_result.get("module_interface", {}).get("all_values", [])
            source_exports = source_result.get("export_records", [])
            
            # Create re-export records for each item from the source
            for source_export in source_exports:
                exported_name = source_export.get("exported_name")
                if not exported_name:
                    continue
                
                # Avoid duplicates
                if exported_name in all_values_collected:
                    continue
                
                all_values_collected.add(exported_name)
                
                # Create a re-export record
                target_fqn = source_export.get("target_item_fqn")
                if not target_fqn:
                    target_fqn = f"{source_fqn}.{exported_name}"
                
                new_export = {
                    "exporting_package_fqn": mod_result.get("package_name"),
                    "exporting_module_fqn": aggregator_fqn,
                    "exported_name": exported_name,
                    "target_item_fqn": target_fqn,
                    "is_explicit": True,  # Part of explicit __all__
                    "is_reexport": True,
                    "is_wildcard_reexport": False,
                    "is_aggregated_reexport": True, 
                    "needs_linking": False,
                    "source_module": source_fqn,
                    "component_kind": source_export.get("component_kind", "member"),
                    "is_internal": source_export.get("is_internal", True),
                    "metadata": {
                        "aggregation_source": source_ref,
                        "resolved_source": source_fqn
                    }
                }
                expanded_exports.append(new_export)
                logger.debug(f"Added aggregated export '{exported_name}' to {aggregator_fqn} from {source_fqn}")
        
        # Update the module result
        mod_result["export_records"].extend(expanded_exports)
        mod_result["module_interface"]["all_values"] = list(all_values_collected)
        mod_result["module_interface"]["all_is_dynamic"] = False  # Now resolved
        mod_result["module_interface"]["needs_dynamic_analysis"] = False  # Resolved statically
        
        logger.info(f"Expanded {len(expanded_exports)} aggregated exports for {aggregator_fqn}")

    def _resolve_aggregation_source(self, source_ref: str, aggregator_fqn: str, package_prefix: str) -> Optional[str]:
        """
        Resolve a relative module reference from __all__ aggregation to an absolute FQN.
        
        Examples:
            - 'lib._shape_base_impl' in 'numpy' -> 'numpy.lib._shape_base_impl'
            - 'np' in 'numpy.matlib' (where `import numpy as np`) -> 'numpy'
            - 'fft' in 'numpy' -> 'numpy.fft'
        """
        mod_result = self.get_analysis_result(aggregator_fqn)
        if not mod_result:
            return None
        
        first_part = source_ref.split('.')[0]
        rest = source_ref[len(first_part):]  # e.g., '' or '._shape_base_impl'
        
        # Check import_records for alias resolution (handles `import numpy as np`)
        import_records = mod_result.get("import_records", [])
        for imp_rec in import_records:
            # Match by the name bound in the importer's namespace
            if imp_rec.get("name_bound_in_importer") == first_part:
                base_fqn = imp_rec.get("name_bound_points_to_fqn")
                if base_fqn:
                    resolved = f"{base_fqn}{rest}" if rest else base_fqn
                    logger.debug(f"Resolved '{source_ref}' via import alias '{first_part}' -> '{resolved}'")
                    return resolved if self.get_analysis_result(resolved) else None
        
        # Try direct resolution: aggregator_fqn.source_ref (e.g., numpy.lib._shape_base_impl)
        candidate_fqn = f"{aggregator_fqn}.{source_ref}"
        if self.get_analysis_result(candidate_fqn):
            return candidate_fqn
        
        # Try with package prefix (for subpackages)
        if package_prefix and package_prefix != aggregator_fqn:
            candidate_fqn = f"{package_prefix}.{source_ref}"
            if self.get_analysis_result(candidate_fqn):
                return candidate_fqn
        
        # Try as absolute path (already fully qualified)
        if self.get_analysis_result(source_ref):
            return source_ref
        
        return None
    
    
    def _find_top_level_packages(self, base_path_str: str) -> Set[str]:
        """
        Finds top-level Python packages under all detected Python package roots.
        """
        packages: Set[str] = set()
        
        # Use Python package roots if available, otherwise fall back to base_path
        roots_to_scan = self.python_package_roots if self.python_package_roots else [Path(base_path_str)]
        
        for base_path in roots_to_scan:
            if not base_path.is_dir():
                continue
                
            logger.debug(f"Scanning for top-level packages in '{base_path}'")
            
            try:
                for item_path in base_path.iterdir():
                    item_name = item_path.name
                    if item_path.is_dir() and item_name.isidentifier():
                        # Skip common non-package directories
                        if item_name in {'test', 'tests', 'docs', 'examples', '__pycache__', 'build', 'dist'}:
                            continue
                        if item_name.startswith(('.', '_')):
                            continue
                            
                        init_file = item_path / "__init__.py"
                        if init_file.exists():
                            packages.add(item_name)
                            logger.debug(f"Found package: {item_name} in {base_path}")
                        else:
                            # Check for namespace package (has .py files or subpackages)
                            has_python_content = any(
                                f.suffix == '.py' or (f.is_dir() and (f / '__init__.py').exists())
                                for f in item_path.iterdir()
                            )
                            if has_python_content:
                                packages.add(item_name)
                                logger.debug(f"Found namespace package: {item_name} in {base_path}")
            except Exception as e:
                logger.error(f"Error scanning {base_path}: {e}")
        
        return packages

    
    def _build_package_module_graph(self, python_files: List[str], base_path: str):
        """
        Populates the GraphStore with package and module nodes and CONTAINS relationships.
        """
        if not self.store:
            return
        
        logger.info("Building package/module structure in the graph...")
        added_nodes = set()
        added_edges = set()
        abs_base_path = Path(base_path).resolve() # ensure base path is absolute for reliable relative path calculation

        for file_path_str in python_files:
            try:
                abs_file_path = Path(file_path_str).resolve()
                module_fqn = self._get_module_name(str(abs_file_path), str(abs_base_path))
                is_init = abs_file_path.name == '__init__.py'

                if not module_fqn:
                    logger.warning(f"Could not determine module name for {file_path_str}, skipping graph structure.")
                    continue

                # Add node for the module/package itself
                node_id = module_fqn
                node_type = NODE_TYPE_PACKAGE if is_init else NODE_TYPE_MODULE
                rel_file_path_str =  str(abs_file_path.relative_to(abs_base_path)) if abs_file_path.is_relative_to(abs_base_path) else str(abs_file_path)
                if node_id not in added_nodes:
                    self.store.add_node(node_id, node_type=node_type, file_path=rel_file_path_str)
                    added_nodes.add(node_id)
                    logger.debug(f"Added graph node: {node_id} (Type: {node_type})")

                # Add nodes and CONTAINS edges for parent packages
                parts = module_fqn.split('.')
                current_parent_fqn_parts = []
                
                # Iterate to build parent package chain
                for i in range(len(parts) -1): # up to the parent of the current module/package
                    part = parts[i]
                    current_parent_fqn_parts.append(part)
                    parent_fqn = ".".join(current_parent_fqn_parts)
                    
                    # The child is the next segment in the FQN
                    child_segment_fqn = ".".join(parts[:i+2])


                    if parent_fqn not in added_nodes:
                        parent_init_path = abs_base_path.joinpath(*parts[:i+1], '__init__.py')
                        parent_file_rel_path_str = str(parent_init_path.relative_to(abs_base_path)) if parent_init_path.exists() and parent_init_path.is_relative_to(abs_base_path) else None
                        self.store.add_node(parent_fqn, node_type=NODE_TYPE_PACKAGE, file_path=parent_file_rel_path_str)
                        added_nodes.add(parent_fqn)
                    
                    edge = (parent_fqn, child_segment_fqn) # Edge from parent to its direct child segment
                    if child_segment_fqn and edge not in added_edges :
                        # Ensure child node (as a segment) exists; its full module/package node also added.
                        # This typically means child_segment_fqn refers to module_fqn if it's the last part.
                        if child_segment_fqn not in added_nodes:
                            # This case is tricky; if child_segment_fqn is module_fqn, it's already added.
                            # If it's an intermediate package, it would have been added as a parent in a previous step.
                            # This indicates the node representing this segment might need explicit adding if not covered.
                            # For now, assume module_fqn (the leaf) is the primary node for its path.
                            pass 
                        self.store.add_edge(parent_fqn, child_segment_fqn, edge_type=REL_TYPE_CONTAINS)
                        added_edges.add(edge)
            except Exception as e:
                logger.error(f"Error building graph structure for {file_path_str}: {e}", exc_info=True)
        logger.info(f"Finished package/module graph. Added {len(added_nodes)} nodes, {len(added_edges)} CONTAINS edges.")
    
    
    def _precompute_known_modules(self, python_files: List[str]) -> None:
        """
        Precomputes all known module and package FQNs, including namespace packages.
        """
        self.known_modules = {}
        package_dirs = set()  # Track all potential package directories

        # First pass: collect all .py files and identify regular packages (with __init__.py)
        for file_path in python_files:
            module_fqn, is_init = self._get_module_name(file_path, self.repo_path)
            if module_fqn:
                self.known_modules[module_fqn] = {"is_package": is_init}
                if is_init:
                    package_dirs.add(Path(file_path).parent)

        # Second pass: detect namespace packages (dirs without __init__.py but containing .py or subpackages)
        all_dirs = set()
        for file_path in python_files:
            dir_path = Path(file_path).parent
            all_dirs.add(dir_path)
            # Walk up the tree to collect all parent dirs
            while dir_path != Path(self.repo_path):
                all_dirs.add(dir_path)
                dir_path = dir_path.parent

        for dir_path in all_dirs:
            rel_dir = os.path.relpath(dir_path, self.repo_path)
            parts = rel_dir.split(os.sep)
            fqn = '.'.join(p for p in parts if p)  # Skip empty parts
            if not fqn:  # Root dir
                continue

            init_path = dir_path / "__init__.py"
            has_init = init_path.exists()
            if has_init:
                # Already marked as package via __init__.py
                if fqn in self.known_modules:
                    self.known_modules[fqn]["is_package"] = True
                continue

            # Check for Python content (has .py files or subpackages)
            has_python_content = False
            for sub_item in dir_path.iterdir():
                if sub_item.is_file() and sub_item.suffix == '.py':
                    has_python_content = True
                    break
                if sub_item.is_dir() and (sub_item in package_dirs or (sub_item / "__init__.py").exists()):
                    has_python_content = True
                    break

            if has_python_content:
                # It's a namespace package
                self.known_modules[fqn] = {"is_package": True}
                logger.debug(f"Detected namespace package: {fqn}")

        logger.info(f"Precomputed {len(self.known_modules)} known modules/packages (including namespace packages).")
    
    def _detect_compiled_extensions_in_imports(self, import_records: List[Dict]) -> Set[str]:
        """
        Detects compiled extension imports from a single file's import records.
        
        An import references a compiled extension if:
        1. It's internal (source module starts with a top-level package)
        2. The source module does NOT exist in self.known_modules (no .py file)
        
        This per-file version is called during analyze_file() before dynamic 
        analysis runs, enabling early detection and target package installation.
        
        Args:
            import_records: Import records from static analysis of one file
            
        Returns:
            Set of module FQNs that are compiled extensions
        """
        compiled_extensions: Set[str] = set()
        
        if not self.known_modules:
            return compiled_extensions
        
        for import_rec in import_records:
            # Handle both dict and ImportRecord objects
            if hasattr(import_rec, 'source_module_fqn'):
                source_module = import_rec.source_module_fqn
                is_internal = getattr(import_rec, 'is_source_internal', False)
            elif isinstance(import_rec, dict):
                source_module = import_rec.get("source_module_fqn")
                is_internal = import_rec.get("is_source_internal", False)
            else: continue
            
            if not source_module or not is_internal: continue
            
            # Internal import that's not in known_modules = compiled extension
            if source_module not in self.known_modules:
                module_parts = source_module.split('.')
                is_compiled = True
                
                # Check if direct parent exists (might be attribute access, not extension)
                for i in range(len(module_parts), 0, -1):
                    parent_module = '.'.join(module_parts[:i])
                    if parent_module in self.known_modules:
                        if i == len(module_parts) - 1:
                            is_compiled = False
                        break
                
                if is_compiled:
                    compiled_extensions.add(source_module)
        
        return compiled_extensions
    
    def _should_install_target_package(self) -> bool:
        """
        Determines if the target package should be installed for dynamic analysis.
        
        Returns True if:
        1. Compiled extensions were detected
        2. auto_install_target_package config is enabled
        3. Package hasn't been installed yet
        4. Package has a valid build configuration
        
        Returns:
            True if target package should be installed
        """
        if not self.config.auto_install_target_package:
            logger.debug("Target package auto-install disabled in config")
            return False
        
        if self._target_package_installed:
            logger.debug("Target package already installed")
            return False
        
        if not self._compiled_extension_imports:
            logger.debug("No compiled extensions detected - no need to install target package")
            return False
        
        # Check if package has build configuration
        repo_path = Path(self.repo_path)
        has_build_config = any([
            (repo_path / "pyproject.toml").exists(),
            (repo_path / "setup.py").exists(),
            (repo_path / "setup.cfg").exists(),
        ])
        
        if not has_build_config:
            logger.warning(f"Compiled extensions detected ({len(self._compiled_extension_imports)}) but no build configuration found. Cannot install target package.")
            return False
        return True
    
    def _install_target_package_for_dynamic_analysis(self) -> bool:
        """
        Installs the target package for dynamic analysis.
        
        This enables dynamic analysis of packages with compiled extensions by making
        those extensions available for import.
        
        Returns:
            True if installation succeeded, False otherwise
        """
        if self._target_package_installed:
            return True
        
        # Get package name and version from project metadata
        package_name = None
        package_version = None
        if self.registry and hasattr(self.registry, "get_project_metadata"):
            project_metadata = self.registry.get_project_metadata()
            if project_metadata:
                package_name = project_metadata.get("name")
                package_version = project_metadata.get("version")
        
        if not package_name:
            # Try to infer from top-level packages
            if self.top_level_packages:
                repo_name = Path(self.repo_path).name
                if repo_name in self.top_level_packages:
                    package_name = repo_name
                else:
                    package_name = next(iter(self.top_level_packages))
            else:
                package_name = Path(self.repo_path).name
        
        # Use PyPI package name if configured (for cases like sklearn -> scikit-learn)
        # Otherwise fall back to project name (import name)
        pypi_name = getattr(self.config, 'pypi_package_name', None) or package_name
        
        logger.info(f"Installing target package '{pypi_name}' (import name: {package_name}, version: {package_version}) for compiled extension support. "
                   f"Detected extensions: {list(self._compiled_extension_imports)[:5]}...")
        
        # Initialize dynamic analyzer if needed
        if self.dynamic_analyzer is None:
            self.dynamic_analyzer = DynamicAnalyzer(
                self.repo_path, 
                self.config,
                python_package_roots=self.python_package_roots
            )
        
        success = self.dynamic_analyzer.install_target_package(
            package_path=Path(self.repo_path), 
            package_name=pypi_name,  # Use PyPI name for installation
            package_version=package_version
        )
        
        if success:
            self._target_package_installed = True
            logger.info(f"Target package '{pypi_name}' installed successfully. Compiled extensions now available for dynamic analysis.")
            return True
        else:
            logger.warning(f"Failed to install target package '{pypi_name}'. Dynamic analysis may fail for modules importing compiled extensions.")
            return False
    
    def _build_chain_candidates_map(self):
        """
        Populates self.candidates_to_re_exporters from the fully resolved file analysis results.
        Should be called after _finalize_all_target_fqns().
        """
        logger.info("Building chain candidate map from finalized export records...")
        
        for _, result in self.file_analysis_results.items():
            for export_record in result.get("export_records", []):
                if export_record.get("is_reexport", False) and export_record.get("target_item_fqn"):
                    target_fqn = export_record["target_item_fqn"]
                    exporter_fqn = export_record["exporting_module_fqn"]
                    
                    if target_fqn not in self.candidates_to_re_exporters:
                        self.candidates_to_re_exporters[target_fqn] = {
                            "component_kind": export_record.get("component_kind", None),
                            "is_internal": export_record.get("is_internal", None),
                            "exporters": set()
                        }
                    
                    self.candidates_to_re_exporters[target_fqn]["exporters"].add(exporter_fqn)
        logger.info(f"Built chain candidate map with {len(self.candidates_to_re_exporters)} entries.")
    
    
    def drive_api_path_resolution(self):
        """
        Drives the API path resolution using a tiered, configurable strategy.
        Tier 1: Fast, graph-less resolution via APIPathResolver.
        Tier 2 & 3: Slower, graph-based resolution via GraphTraversal as a fallback.
        """
        if not self.api_resolver:
            logger.error("Cannot drive API path resolution: APIPathResolver is missing.")
            return

        logger.info(f"Driving API path resolution for {len(self.candidates_to_re_exporters)} candidates.")
        
        # --- Tier 1: Fast Path (Graph-less, in APIPathResolver) ---
        logger.debug(f"Attempting Tier 1 (fast path) resolution for chain candidates")
        API_names_and_chains = self.api_resolver.derive_api_name_via_direct_lookup(self.candidates_to_re_exporters, self.module_results_by_fqn)
        
        if not is_enabled(Feature.GRAPH_ANALYSIS):
            self._update_result_with_api_resolution(API_names_and_chains)
        
        if is_enabled(Feature.GRAPH_ANALYSIS):
            for candidate_fqn, ctx in self.candidates_to_re_exporters.items():
                re_exporters = ctx.get("exporters", set())
                item_kind = ctx.get("component_kind", None)
                resolved_chains = []

                # --- Tier 2 & 3: Fallback Paths (Graph-based, if enabled) ---
                if not resolved_chains and is_enabled(Feature.GRAPH_ANALYSIS):
                    if not self.traversal:
                        logger.error("Graph analysis is enabled, but GraphTraversal is not available for Tier 2/3 fallback.")
                    else:
                        # --- Tier 2: Guided Graph Search ---
                        logger.warning(f"Tier 1 resolution failed for {candidate_fqn}. Attempting Tier 2 (guided graph trace).")
                        try:
                            end_module_fqn = self.api_resolver._determine_target_module_for_candidate(candidate_fqn, re_exporters)
                            if end_module_fqn and self.definition_registry:
                                definition = self.definition_registry.get_definition(candidate_fqn)
                                if definition:
                                    resolved_chains = self.traversal.find_export_chains_guided_graph(
                                        target_component_fqn=candidate_fqn,
                                        end_module_fqn=end_module_fqn,
                                        all_re_exporters=re_exporters,
                                        definition_module_fqn=definition.module
                                    )
                        except Exception as e:
                            logger.error(f"Error during Tier 2 resolution for {candidate_fqn}: {e}", exc_info=True)

                        # --- Tier 3: Exhaustive Unidirectional Search ---
                        if not resolved_chains:
                            logger.warning(f"Tier 2 resolution failed for {candidate_fqn}. Attempting Tier 3 (exhaustive graph search).")
                            try:
                                resolved_chains = self.traversal.find_export_chains(candidate_fqn)
                            except Exception as e:
                                logger.error(f"Error during Tier 3 resolution for {candidate_fqn}: {e}", exc_info=True)
                
                # --- Final Scoring and Update ---
                if not resolved_chains:
                    logger.warning(f"Could not resolve any export chains for candidate: {candidate_fqn}")
                    continue

                resolved_path, best_chain, all_chains_list = self.api_resolver.determine_best_api_path_for_candidate(candidate_fqn, resolved_chains)
                
                # Update component data in the final analysis results
                self._update_component_with_resolution(candidate_fqn, resolved_path, best_chain, all_chains_list)

    def _update_component_with_resolution(self, candidate_fqn, resolved_path, best_chain, all_chains_list):
        """Helper to update the component dictionary in file_analysis_results."""
        updated = False
        if is_enabled(Feature.GRAPH_ANALYSIS):
            updated = False
            for file_result in self.file_analysis_results.values():
                if candidate_fqn in file_result.get("components", {}):
                    comp_dict = file_result["components"][candidate_fqn]
                    comp_dict['is_chain_candidate'] = True
                    comp_dict['resolved_api_path'] = resolved_path
                    comp_dict['best_export_chain'] = [step.__dict__ for step in best_chain] if best_chain else []
                    comp_dict['all_export_chains'] = [[step.__dict__ for step in chain] for chain in all_chains_list]
                    updated = True
                    break
                if not updated:
                    logger.warning(f"Resolved API path for {candidate_fqn}, but could not find its original component dictionary to update.")
    
    
    def _update_result_with_api_resolution(self, api_dict: Dict[str, Dict[str, Any]]):
        """
        Updates both component data and export records in file_analysis_results with final API resolution data.
        """
        if not api_dict:
            return

        # 1. Create a flat map of all components for direct lookup.
        all_components = {}
        for file_result in self.file_analysis_results.values():
            all_components.update(file_result.get("components", {}))

        # 2. Update the component dictionaries first.
        # This ensures the primary component definitions have the API data.
        for comp_fqn, api_info in api_dict.items():
            if comp_fqn in all_components:
                comp_dict = all_components[comp_fqn]
                api_names = api_info.get("API_names")
                if comp_fqn not in api_names:
                    api_names.add(comp_fqn)

                if api_names:
                    sorted_names = sorted(list(api_names)) # Convert set to a sorted list for JSON serialization
                    comp_dict['api_name_sources'] = api_info.get("api_name_sources", {})
                    if comp_fqn not in comp_dict['api_name_sources']:
                        comp_dict['api_name_sources'][comp_fqn] = comp_dict['definition_module_fqn']
                    comp_dict['API_names'] = sorted_names
                    best_api_name = min(sorted_names, key=lambda p: (len(p.split('.')), p))
                    comp_dict['API_name'] = best_api_name
                    comp_dict['is_chain_candidate'] = True
                    comp_dict['best_export_chain'] = api_info.get("export_chain", [])
                    
        # 2.5: Set fallback api_name_sources and best_export_chain for non-chain-candidates
        # Populate components that are not re-exported with their definition module
        for comp_fqn, comp_dict in all_components.items():
            def_module = comp_dict.get('definition_module_fqn')
            if not def_module:
                continue
            
            # Set api_name_sources if not already set
            if not comp_dict.get('api_name_sources'):
                api_name = comp_dict.get('API_name', comp_fqn)
                comp_dict['api_name_sources'] = {
                    api_name: def_module,
                    comp_fqn: def_module
                }
            
            # Set best_export_chain if empty - definition module is both start and end
            if not comp_dict.get('best_export_chain'):
                comp_dict['best_export_chain'] = [{
                    "module_fqn": def_module,
                    "export_type": "definition",
                    "exported_name": comp_dict.get('name', comp_fqn.rsplit('.', 1)[-1])
                }]

        # 3. Propagate API names to child components (methods, nested classes)
        self._propagate_api_names_to_children(all_components)
        
        # 4. Propagate API names to INHERITED members
        self._propagate_api_names_to_inherited_members(all_components)
        
        # 5. Iterate through all modules again to update their export_records.
        # Re-exports of a component also get the resolved API data
        for file_result in self.file_analysis_results.values():
            for export_rec in file_result.get("export_records", []):
                target_fqn = export_rec.get("target_item_fqn")
                if target_fqn and target_fqn in api_dict:
                    resolved_info = api_dict[target_fqn]
                    # Convert set to a sorted list here as well
                    export_rec["API_names"] = sorted(list(resolved_info.get("API_names", [])))
                    export_rec["export_chain"] = resolved_info.get("export_chain", [])
                
    def _propagate_api_names_to_children(self, all_components: Dict[str, Dict[str, Any]]):
        """
        Propagate API names from classes to their methods and nested classes.
        Handles multiple levels of nesting iteratively.
        
        For a method with FQN 'pkg.internal.MyClass.method' where the class has
        API_name 'pkg.MyClass', derive the method's API_name as 'pkg.MyClass.method'.
        
        For nested classes like 'pkg.internal.Outer.Inner.method', if Outer has API_name 'pkg.Outer',
        then Inner gets 'pkg.Outer.Inner' and the method gets 'pkg.Outer.Inner.method'.
        """
        # Build initial map of FQN -> resolved API name for components that have one
        resolved_api_names: Dict[str, str] = {}
        for comp_fqn, comp_dict in all_components.items():
            api_name = comp_dict.get('API_name')
            if api_name and api_name != comp_fqn:
                resolved_api_names[comp_fqn] = api_name
        
        if not resolved_api_names:
            logger.debug("No resolved API names to propagate to children.")
            return
        
        # Build parent->children relationships for efficient lookup
        children_by_parent: Dict[str, List[str]] = {}
        for comp_fqn, comp_dict in all_components.items():
            parent_fqn = comp_dict.get('parent_fqn')
            if parent_fqn:
                if parent_fqn not in children_by_parent:
                    children_by_parent[parent_fqn] = []
                children_by_parent[parent_fqn].append(comp_fqn)
        
        # Iteratively propagate API names until no more changes
        # This handles arbitrary nesting depth (Outer -> Inner -> InnerInner -> method)
        max_iterations = 10  # Safety limit to prevent infinite loops
        iteration = 0
        changes_made = True
        
        while changes_made and iteration < max_iterations:
            changes_made = False
            iteration += 1
            
            for parent_fqn, parent_api_name in list(resolved_api_names.items()):
                # Get all children of this parent
                child_fqns = children_by_parent.get(parent_fqn, [])
                
                for child_fqn in child_fqns:
                    # Skip if child already has a resolved API name
                    if child_fqn in resolved_api_names:
                        continue
                    
                    comp_dict = all_components.get(child_fqn)
                    if not comp_dict:
                        continue
                    
                    comp_name = comp_dict.get('name')
                    if not comp_name:
                        continue
                    
                    # Derive child's API name: parent_api_name + "." + child_name
                    child_api_name = f"{parent_api_name}.{comp_name}"
                    
                    # Update the component
                    comp_dict['API_name'] = child_api_name
                    comp_dict['API_names'] = sorted([child_api_name, child_fqn])
                    comp_dict['api_name_sources'] = {
                        child_api_name: parent_fqn,
                        child_fqn: comp_dict.get('definition_module_fqn')
                    }
                    
                    # Propagate export chain from parent class
                    parent_chain = all_components.get(parent_fqn, {}).get('best_export_chain', [])
                    if parent_chain:
                        # Append child step to parent's chain
                        comp_dict['best_export_chain'] = parent_chain + [{
                            "module_fqn": parent_fqn,
                            "export_type": "member_of",
                            "exported_name": comp_name
                        }]
                    
                    # Track this resolution for next iteration (for nested children)
                    resolved_api_names[child_fqn] = child_api_name
                    changes_made = True
                    
                    logger.debug(f"Propagated API name to child: {child_fqn} -> {child_api_name}")
        
        # ====== Update methods/nested_classes inside class dictionaries ======
        # The class's `methods` and `nested_classes` lists are separate from all_components so we need to update them too
        self._update_nested_members_in_classes(all_components, resolved_api_names)
        
        if iteration >= max_iterations:
            logger.warning(f"API name propagation reached max iterations ({max_iterations}). Some deeply nested components may not have API names.")
        else:
            logger.info(f"Propagated API names to child components in {iteration} iteration(s). Total resolved: {len(resolved_api_names)}")
    
    
    def _update_nested_members_in_classes(self, all_components: Dict[str, Dict[str, Any]], resolved_api_names: Dict[str, str]):
        """
        Update the API names for methods and nested classes that are stored inside class dictionaries.
        
        This is needed because Class.to_dict() creates separate dictionaries for its methods/nested_classes,
        which are different from the ones in the flat all_components dict.
        """
        for comp_fqn, comp_dict in all_components.items():
            # Only process classes
            if comp_dict.get('component_kind') != 'class':
                continue
            
            # Update constructor if present
            constructor = comp_dict.get('constructor')
            if constructor:
                constructor_fqn = constructor.get('fully_qualified_name')
                if constructor_fqn and constructor_fqn in resolved_api_names:
                    api_name = resolved_api_names[constructor_fqn]
                    constructor['API_name'] = api_name
                    constructor['API_names'] = sorted([api_name, constructor_fqn])
            
            # Update methods list
            methods_list = comp_dict.get('methods', [])
            for method_dict in methods_list:
                method_fqn = method_dict.get('fully_qualified_name')
                if method_fqn and method_fqn in resolved_api_names:
                    api_name = resolved_api_names[method_fqn]
                    method_dict['API_name'] = api_name
                    method_dict['API_names'] = sorted([api_name, method_fqn])
                    method_dict['api_name_sources'] = {
                        api_name: comp_fqn,  # Parent class FQN
                        method_fqn: method_dict.get('definition_module_fqn')
                    }
                # Propagate export chain from parent class
                parent_chain = comp_dict.get('best_export_chain', [])
                if parent_chain:
                    method_dict['best_export_chain'] = parent_chain + [{
                        "module_fqn": comp_fqn,
                        "export_type": "member_of",
                        "exported_name": method_dict.get('name')
                    }]
            
            # Update nested_classes list (recursively)
            nested_classes = comp_dict.get('nested_classes', [])
            for nested_class_dict in nested_classes:
                nested_fqn = nested_class_dict.get('fully_qualified_name')
                if nested_fqn and nested_fqn in resolved_api_names:
                    api_name = resolved_api_names[nested_fqn]
                    nested_class_dict['API_name'] = api_name
                    nested_class_dict['API_names'] = sorted([api_name, nested_fqn])
                    # Recursively update methods in nested classes
                    self._update_nested_members_in_classes({nested_fqn: nested_class_dict}, resolved_api_names)
    
    
    def _propagate_api_names_to_inherited_members(self, all_components: Dict[str, Dict[str, Any]]):
        """
        Propagate API names to inherited members (internal AND external).
        
        For each class with inherited_methods:
        1. Derive API names for accessing the method via this class
        2. For internal methods: also include the original method's API name
        3. For external methods: original_api_name is the external FQN
        """
        logger.info("Propagating API names to inherited members...")
        total_inherited = 0
        total_external = 0
        
        for comp_fqn, comp_dict in all_components.items():
            if comp_dict.get('component_kind') != 'class':
                continue
        
            inherited_methods = comp_dict.get('inherited_methods', {})
            if not inherited_methods: continue
            
            # Get this class's API name
            class_api_name = comp_dict.get('API_name', comp_fqn)
            class_all_api_names = set(comp_dict.get('API_names', [comp_fqn]))
            class_all_api_names.add(class_api_name)
            class_all_api_names.add(comp_fqn)
            
            for method_name, method_info in inherited_methods.items():
                if not isinstance(method_info, dict): continue
                
                is_external = method_info.get('is_external', False)
                
                # --- Derive API names for this inherited method via THIS class ---
                inherited_api_names = [f"{api_name}.{method_name}" for api_name in class_all_api_names]
                primary_inherited_api = f"{class_api_name}.{method_name}"
                
                method_info['inherited_api_name'] = primary_inherited_api
                method_info['inherited_api_names'] = sorted(set(inherited_api_names))
                method_info['inheriting_class_fqn'] = comp_fqn
                method_info['inheriting_class_api_name'] = class_api_name
                
                if is_external:
                    # External method: original_api_name is already set to external FQN
                    if not method_info.get('original_api_name'):
                        source_fqn = method_info.get('source_class_fqn', '')
                        method_info['original_api_name'] = f"{source_fqn}.{method_name}"
                        method_info['original_api_names'] = [method_info['original_api_name']]
                    
                    total_external += 1
                else:
                    # Internal method: get API names from source class
                    source_class_fqn = method_info.get('source_class_fqn')
                    if source_class_fqn and source_class_fqn in all_components:
                        source_class_dict = all_components[source_class_fqn]
                        source_class_api_name = source_class_dict.get('API_name', source_class_fqn)
                        
                        original_api_name = f"{source_class_api_name}.{method_name}"
                        method_info['original_api_name'] = original_api_name
                        
                        source_all_api_names = set(source_class_dict.get('API_names', [source_class_fqn]))
                        source_all_api_names.add(source_class_api_name)
                        source_all_api_names.add(source_class_fqn)
                        
                        original_method_api_names = [f"{api}.{method_name}" for api in source_all_api_names]
                        method_info['original_api_names'] = sorted(set(original_method_api_names))
                    else:
                        original_fqn = method_info.get('original_fqn', '')
                        method_info['original_api_name'] = original_fqn
                        method_info['original_api_names'] = [original_fqn] if original_fqn else []
                
                total_inherited += 1
                logger.debug(f"{'External' if is_external else 'Internal'} inherited method {method_name} in {comp_fqn}: API names = {method_info['inherited_api_names']}")
        
        if total_inherited > 0:
            logger.info(f"Propagated API names to {total_inherited} inherited methods ({total_external} external, {total_inherited - total_external} internal)")
        else:
            logger.debug("No inherited methods needed API name propagation.")
    
    
    def analyze_file(self, file_path: str) -> Dict[str, Any]:
        """
        Analyzes a single Python file. This is the core per-file processing method.
        
        Workflow:
        1. Handles basic path normalization and in-memory caching.
        2. Checks for pre-computed IR in the disk cache.
        3. Performs static analysis using `code_visitor.analyze_code()`.
        4. Performs an "on-the-fly" resolution pass to link exports and expand wildcards.
        5. If dynamic analysis is necessary (by static analysis and config):
            a. Ensures the dynamic analysis environment (venv) is set up.
            b. Executes the dynamic analysis script for the module, passing static context.
            c. Merges dynamically discovered/confirmed export information with static exports.
            d. Updates the module interface based on dynamically resolved `__all__`.
        6. Recalculates module statistics based on the final (potentially merged) export data.
        7. Applies the final import and export records to the graph trackers.
        8. Generates and caches an Intermediate Representation (IR) if configured and not already loaded. Includes validation of the generated IR (via converter).
        9. Stores the final, comprehensive analysis result for the file.
        10. Publishes a MODULE_ANALYSIS_UPDATED event.

        Args:
            file_path: Absolute path to the Python file.
            
        Returns:
            A dictionary containing the final analysis results for the file.
        """
        
        if not self.repo_path: # Ensure repo_path is set; crucial for relative path calculations
            # Try to get from registry first if this instance didn't have it
            if self.registry and hasattr(self.registry, 'repo_path') and self.registry.repo_path:
                self.repo_path = os.path.abspath(self.registry.repo_path)
            else: # Fallback: infer from the first file analyzed if not set globally
                self.repo_path = os.path.dirname(os.path.abspath(file_path)) # Or a more sophisticated root finding
                logger.warning(f"Repo path was not set for AnalyzerIntegration, inferred to {self.repo_path} based on {file_path}. This might affect relative path consistency if files are from different roots.")
        
        abs_file_path = os.path.abspath(file_path)
        try:
            rel_path = os.path.relpath(abs_file_path, self.repo_path) if self.repo_path and abs_file_path.startswith(self.repo_path) else os.path.basename(abs_file_path)
        except ValueError: # Handles cases like different drives on Windows
            rel_path = os.path.basename(abs_file_path)
            logger.warning(f"Could not make {abs_file_path} relative to {self.repo_path}. Using basename: {rel_path}")
            
        logger.info(f"Starting analysis for file: {rel_path} (Abs: {abs_file_path})")
        
        #--- 1. Check In-Memory Cache (for results already fully processed in this session) ---
        if rel_path in self.file_analysis_results:
            logger.debug(f"Returning fully cached analysis result for {rel_path} from memory (not IR, but raw analysis result).")
            return self.file_analysis_results[rel_path]

        #--- 2. IR Disk Cache Handling (Load IR if available, before heavy analysis) ---
        ir_module_from_disk_cache: Optional[IRModule] = None
        
        # More descriptive status for ir_cache_hit:
        # 'none', 'disk_hit', 'disk_miss_attempt_generation', 'disk_miss_generated_cached', 
        # 'disk_miss_generated_not_cached', 'disk_miss_generation_failed_validation', 
        # 'disk_miss_generation_exception', 'ir_generation_disabled'
        ir_cache_status: str = "none"
        
        ir_cache_key_for_file: Optional[str] = None
        ir_cache_file_path_for_file: Optional[Path] = None

        if self.config.generate_ir: # Only interact with IR cache if IR generation is enabled
            if self.config.ir_cache_dir: # Check if ir_cache_dir is configured (was cache_dir before)
                try:
                    ir_cache_key_for_file = generate_cache_key(abs_file_path, self.config)
                    ir_cache_file_path_for_file = get_cache_file_path(self.config.ir_cache_dir, ir_cache_key_for_file)
                    
                    if ir_cache_file_path_for_file.exists(): # Check existence before reading
                        logger.debug(f"Attempting to load IR from disk cache: {ir_cache_file_path_for_file}")
                        ir_module_from_disk_cache = read_from_cache(ir_cache_file_path_for_file) # This now handles validation if configured in cache.py
                        if ir_module_from_disk_cache:
                            self.ir_cache[rel_path] = ir_module_from_disk_cache # Add to in-memory IR cache
                            ir_cache_status = "disk_hit"
                            logger.info(f"IR disk cache hit for {rel_path} (key: {ir_cache_key_for_file})")
                        else:
                            # read_from_cache returned None, means file might be corrupted or failed validation
                            logger.warning(f"IR disk cache miss for {rel_path} (key: {ir_cache_key_for_file}) despite file existence. File might be invalid or failed validation.")
                            ir_cache_status = "disk_miss_invalid_file" 
                    else:
                        ir_cache_status = "disk_miss_no_file" # File does not exist
                        logger.debug(f"IR disk cache miss for {rel_path} (key: {ir_cache_key_for_file}). File not found.")
                except Exception as e:
                    logger.warning(f"Error reading from IR disk cache for {rel_path}: {e}", exc_info=False) # exc_info=False to avoid too much noise for cache errors
                    ir_cache_status = "disk_miss_read_error"
            else: # No ir_cache_dir configured
                logger.warning("IR generation is enabled, but no ir_cache_dir is configured in AnalysisConfig. IR disk caching will be disabled.")
                ir_cache_status = "ir_disk_cache_disabled"
        else: # IR generation is disabled
            ir_cache_status = "ir_generation_disabled"
        
        # Initialize basic module information
        module_name, is_init = self._get_module_name(abs_file_path, self.repo_path)
        # package_name = module_name.split('.')[0] if '.' in module_name else ""
        if is_init: package_name = module_name
        else: package_name = module_name.rsplit('.', 1)[0] if '.' in module_name else ""
        
        # This will store the final, comprehensive result for this file
        final_analysis_result: Dict[str, Any] = {
            "module_name": module_name, 
            "package_name": package_name, 
            "source_file": abs_file_path, 
            "relative_path": rel_path,
            "errors": [],
            "success": False, 
            "dynamic_analysis_attempted": False,
            "dynamic_analysis_success": False,
            "ir_generated": ir_module_from_disk_cache is not None, # initial state if loaded from disk
            "ir_cache_status": ir_cache_status 
        }
        # If IR was loaded from disk, add its dict representation to the result now
        if ir_module_from_disk_cache and "ir_module_dict" not in final_analysis_result:
             final_analysis_result["ir_module_dict"] = ir_module_from_disk_cache.model_dump(exclude_none=True)


        try:
            # --- 3. Static Analysis (CodeVisitor) ---
            with open(abs_file_path, 'r', encoding='utf-8') as f: code = f.read()
            
            static_analysis_result = analyze_code(code, module_name, package_name, abs_file_path, self.config, self.definition_registry, 
                                                  self.inheritance_tracker, self.call_tracker, self.top_level_packages, self.known_modules)
            # Merge static results into final_analysis_result, prioritizing new fields but allowing static_analysis_result to overwrite defaults
            final_analysis_result.update(static_analysis_result) 
            
            # --- On-the-fly Linking (Post-Static) ---
            self._resolve_and_expand_module_exports(module_name, final_analysis_result)
            
            # Ensure essential lists/dicts exist if static_analysis_result was minimal due to early error
            current_export_records = final_analysis_result.setdefault("export_records", [])
            current_module_interface = final_analysis_result.setdefault("module_interface", {}).copy() # Work on a copy
            # current_module_stats = final_analysis_result.setdefault("module_statistics", {}).copy() # Will be fully recalculated

            # --- 4. Dynamic Analysis (if needed) ---
            skip_dynamic_for_this_module = False  # Local flag to control flow
            if self.config.dynamic_all_check and (current_module_interface.get("needs_dynamic_analysis") or self.config.force_dynamic_check):
                
                # --- 4a. Compiled Extension Detection and Target Package Installation ---
                file_compiled_imports = self._detect_compiled_extensions_in_imports(final_analysis_result.get("import_records", []))

                # If module has compiled imports, target package must be installed
                has_compiled_imports = bool(file_compiled_imports)
                if has_compiled_imports:
                    if not self._target_package_installed:
                        if self.config.auto_install_target_package:
                            self._compiled_extension_imports.update(file_compiled_imports)
                            logger.info(f"Module {module_name} imports compiled extensions: {list(file_compiled_imports)[:3]}...")
                            
                            if self._should_install_target_package():
                                install_success = self._install_target_package_for_dynamic_analysis()
                                if not install_success:
                                    # Installation failed: skip dynamic analysis for this module
                                    logger.warning(f"Skipping dynamic analysis for {module_name}: target package installation failed")
                                    final_analysis_result['dynamic_analysis_attempted'] = True 
                                    final_analysis_result['dynamic_analysis_skipped'] = True
                                    final_analysis_result['dynamic_skip_reason'] = "Target package installation failed - compiled extensions unavailable"
                                    current_module_interface.update({"needs_dynamic_analysis": False, "all_is_dynamic": False})
                                    final_analysis_result["module_interface"] = current_module_interface
                                    skip_dynamic_for_this_module = True
                                    # Continue to next section, skip dynamic analysis
                        else:
                            # Auto-install disabled but compiled extensions needed
                            logger.warning(f"Skipping dynamic analysis for {module_name}: compiled extensions detected but auto_install_target_package is disabled")
                            final_analysis_result['dynamic_analysis_attempted'] = True
                            final_analysis_result['dynamic_analysis_skipped'] = True
                            final_analysis_result['dynamic_skip_reason'] = "Compiled extensions detected but auto-install disabled"
                            current_module_interface.update({"needs_dynamic_analysis": False, "all_is_dynamic": False})
                            final_analysis_result["module_interface"] = current_module_interface
                            skip_dynamic_for_this_module = True
                
                # --- 4b. Lazy initialize DynamicAnalyzer ---
                if not skip_dynamic_for_this_module and self.dynamic_analyzer is None:
                    repo_root_for_da = self.repo_path or os.path.dirname(abs_file_path)
                    if os.path.isdir(repo_root_for_da):
                        self.dynamic_analyzer = DynamicAnalyzer(
                            repo_root_for_da, 
                            self.config,
                            python_package_roots=self.python_package_roots
                        )
                        if not self.dynamic_analyzer._ensure_environment(): 
                            logger.error(f"DynamicAnalyzer environment setup failed for {module_name}. Dynamic analysis will be skipped.")
                            self.dynamic_analyzer = None 
                            skip_dynamic_for_this_module = True
                        else:
                            # --- Proactive package importability check (runs once on first init) ---
                            if self.config.auto_install_target_package and not self._target_package_installed:
                                can_import, import_error = self._test_package_importability()
                                if not can_import:
                                    # If import test failed, install the target package regardless of error type
                                    logger.info(f"Package import test failed ({import_error[:100] if import_error else 'unknown error'}), installing target package...")
                                    install_success = self._install_target_package_for_dynamic_analysis()
                                    if install_success:
                                        logger.info("Target package installed successfully after import test failure")
                                    else:
                                        logger.warning("Target package installation failed after import test failure")
                    else:
                        logger.error(f"Invalid repo_root '{repo_root_for_da}' for DynamicAnalyzer. Dynamic analysis skipped.")
                        skip_dynamic_for_this_module = True
                
                # --- 4c. Run Dynamic Analysis ---
                if not skip_dynamic_for_this_module and self.dynamic_analyzer:
                    logger.info(f"Running dynamic export evaluation for {module_name}...")
                    locally_defined_fqns = [
                        comp_data.get("fully_qualified_name") 
                        for comp_data in static_analysis_result.get("components", {}).values() # Use original static components
                        if comp_data.get("fully_qualified_name")
                    ]
                    static_info_for_script = {
                        "module_fqn": module_name,
                        "import_records": static_analysis_result.get("import_records", []),
                        "local_definition_fqns": locally_defined_fqns,
                        "static_all_values": static_analysis_result.get("module_interface",{}).get("all_values"),
                        "has_static_all": static_analysis_result.get("module_interface",{}).get("has_all", False),
                        "top_level_packages": list(self.top_level_packages),
                        "stub_external_imports": getattr(self.config, "dynamic_stub_external_imports", False)
                    }
                    
                    # Detect if this module has compiled extension imports
                    has_compiled_imports_for_script = True if (file_compiled_imports or self._target_package_installed) else False
                    
                    # # Only use compiled extension mode if:
                    # # 1. THIS module has compiled imports, OR
                    # # 2. We installed the package AND there were actual compiled extensions detected
                    # has_compiled_imports_for_script = bool(file_compiled_imports) or (self._target_package_installed and bool(self._compiled_extension_imports))
                    
                    dynamic_eval_results = self.dynamic_analyzer.evaluate_module_exports(abs_file_path, static_info_for_script, has_compiled_imports_for_script)
                    final_analysis_result['dynamic_analysis_attempted'] = True

                    if dynamic_eval_results:
                        # Handle skipped case first
                        if dynamic_eval_results.get("dynamic_execution_skipped"):
                            # This is not an error - just means static analysis is sufficient
                            skip_reason = dynamic_eval_results.get("skip_reason", "Unknown")
                            logger.debug(f"Dynamic analysis gracefully skipped for {module_name}: {skip_reason}")
                            final_analysis_result['dynamic_analysis_skipped'] = True
                            final_analysis_result['dynamic_skip_reason'] = skip_reason
                            # Preserve static analysis result as the authoritative result
                            current_module_interface.update({"needs_dynamic_analysis": False, "all_is_dynamic": False})
                            final_analysis_result["module_interface"] = current_module_interface
                        
                        elif dynamic_eval_results.get("dynamic_execution_error"):
                            # Dynamic failed with actual error: preserve static exports and static __all__ info
                            err_msg = dynamic_eval_results['dynamic_execution_error']
                            final_analysis_result["errors"].append({
                                "type": "DynamicScriptError", "message": err_msg,
                                "traceback": dynamic_eval_results.get("traceback_info")})
                            # Treat static analysis result as fallback; do NOT clear export_records or all_values
                            current_module_interface.update({
                                "needs_dynamic_analysis": False,
                                "all_is_dynamic": False # mark as tried and won't retry this run
                            })
                            final_analysis_result["module_interface"] = current_module_interface
                        
                        else: # Dynamic analysis succeeded: merge observations
                            final_analysis_result['dynamic_analysis_success'] = True
                            discovered_dyn_exports = dynamic_eval_results.get("discovered_exports", [])
                            
                            if discovered_dyn_exports:
                                # Merge the import records (intersection with actually-seen runtime imports)
                                runtime_imports = dynamic_eval_results.get("runtime_imports", [])
                                if runtime_imports:
                                    final_analysis_result["import_records"] = self._merge_dynamic_and_static_imports(final_analysis_result.get("import_records", []), runtime_imports)
                                # Merge the export records (dynamic takes precedence where it provides targets)
                                current_export_records = self._merge_dynamic_and_static_exports(current_export_records, discovered_dyn_exports, module_name)
                                final_analysis_result["export_records"] = current_export_records # Update with merged
                            
                                # Update module_interface based on dynamically resolved __all__
                                module_has_explicit_all = bool(dynamic_eval_results.get("module_has_explicit_all", False))
                                # Note: dynamic script uses "module_all_values" when __all__ is explicit, 
                                # "resolved_all_values" (empty) when falling back to dir()
                                dyn_resolved_all_values = dynamic_eval_results.get("module_all_values", []) or dynamic_eval_results.get("resolved_all_values", [])

                                if module_has_explicit_all:
                                    current_module_interface.update({
                                        "all_values": dyn_resolved_all_values,
                                        "all_is_dynamic": False,
                                        "has_all": True,
                                        "needs_dynamic_analysis": False
                                    })
                                elif current_module_interface.get("has_all"):
                                    # Static detected __all__ but dynamic didn't find explicit list
                                    # Use whatever dynamic analysis discovered
                                    current_module_interface.update({
                                        "all_values": dyn_resolved_all_values if dyn_resolved_all_values else [],
                                        "all_is_dynamic": False,
                                        "needs_dynamic_analysis": False
                                    })
                                elif current_module_interface.get("all_is_dynamic"):
                                    # DA ran; no explicit __all__ detected at runtime
                                    current_module_interface.update({
                                        "all_values": [],
                                        "all_is_dynamic": False,
                                        "has_all": current_module_interface.get("has_all", False),
                                        "needs_dynamic_analysis": False
                                    })
                                # else: implicit export case; leave has_all/all_values as they were (static)
                            final_analysis_result["module_interface"] = current_module_interface
                    else: # dynamic_eval_results is None or empty object (e.g., early failure)
                        logger.warning(f"Dynamic export evaluation for {module_name} returned no data or failed to execute.")
                        # Preserve static analysis result as fallback and prevent re-attempting dynamic analysis
                        current_module_interface.update({
                            "needs_dynamic_analysis": False,
                            "all_is_dynamic": False # mark we tried and won't retry this run
                        })
                        final_analysis_result["module_interface"] = current_module_interface
            
            # --- Collect Remaining Unlinked Exports for Final Pass ---
            needs_linking_count = 0
            for record in final_analysis_result.get("export_records", []):
                if record.get("needs_linking"):
                    self.final_unlinked_exports.append(record)
                    needs_linking_count += 1
            if needs_linking_count == 0 and final_analysis_result.get("module_interface").get("module_needs_linking"):
                self.final_unlinked_exports.append(next(iter(final_analysis_result.get("export_records", []))))
            
            # --- 5. Recalculate Module Statistics (using final export_records and module_interface) ---
            final_analysis_result["module_statistics"] = self._recalculate_module_statistics(
                module_fqn=module_name,
                final_export_records=final_analysis_result.get("export_records", []), # Use potentially merged
                static_import_records=static_analysis_result.get("import_records", []), # Original static
                static_module_interface=final_analysis_result.get("module_interface",{}), # Potentially updated
                static_components_count=len(static_analysis_result.get("components", {})),
                is_init_file=static_analysis_result.get("module_statistics",{}).get("is_init_file", False),
                module_docstring=static_analysis_result.get("module_interface",{}).get("docstring")
            )
            
            # --- 6. Apply Final Records to Graph Trackers ---
            if self.import_tracker: self.import_tracker.remove_imports_by_module(module_name)
            self._apply_import_records(final_analysis_result.get("import_records", [])) 
            
            if self.export_tracker: self.export_tracker.remove_exports_by_module(module_name)
            self._apply_export_records(final_analysis_result.get("export_records", []))

            # --- 7. IR Generation (if not from disk cache earlier) ---
            final_analysis_result["ir_generated"] = (ir_module_from_disk_cache is not None) # True if loaded from disk initially

            if self.config.generate_ir:
                if ir_cache_status == "disk_hit": # Successfully loaded from disk
                    logger.debug(f"Using IR for {module_name} from disk cache. No new IR generation or disk write needed.")
                    # `ir_module_from_disk_cache` (which is now `generated_ir_module_obj`) is already in self.ir_cache
                    final_analysis_result["ir_generated"] = True # Ensure it's marked true
                
                else: # Not a disk hit, or disk hit failed validation, so attempt generation
                    if ir_cache_status != "ir_disk_cache_disabled" and ir_cache_status != "ir_generation_disabled":
                         # Only attempt if disk caching is configured or IR gen is generally on
                        logger.info(f"Attempting IR generation for {module_name} (Disk cache status: {ir_cache_status}).")
                        ir_cache_status = "disk_miss_attempting_generation" # Update status
                        try:
                            # Core Change: Pass self.config to the converter.
                            # The converter now handles validation and may return None.
                            converted_ir_module = convert_analysis_result_to_ir(final_analysis_result, self.config)
                            
                            if converted_ir_module:
                                self.ir_cache[rel_path] = converted_ir_module # Store in in-memory cache
                                final_analysis_result["ir_generated"] = True
                                # Store the dict representation if needed by other parts of final_analysis_result
                                final_analysis_result["ir_module_dict"] = converted_ir_module.model_dump(exclude_none=True)
                                
                                if ir_cache_file_path_for_file: # If disk caching is configured and path is valid
                                    write_success = write_to_cache(ir_cache_file_path_for_file, converted_ir_module)
                                    if write_success:
                                        logger.info(f"Successfully generated and disk-cached IR for {module_name} at {ir_cache_file_path_for_file}")
                                        ir_cache_status = "disk_miss_generated_and_cached"
                                    else:
                                        logger.warning(f"Generated IR for {module_name} but failed to write to disk cache: {ir_cache_file_path_for_file}")
                                        ir_cache_status = "disk_miss_generated_not_cached"
                                else: # No disk cache dir configured, or path was invalid
                                    logger.debug(f"Successfully generated IR for {module_name} (disk cache not configured or path error).")
                                    ir_cache_status = "disk_miss_generated_no_disk_cache"
                            else:
                                # convert_analysis_result_to_ir returned None (likely validation failure)
                                logger.warning(f"IR conversion for {module_name} resulted in None. IR not generated.")
                                final_analysis_result["ir_generated"] = False
                                ir_cache_status = "disk_miss_generation_failed_validation"
                        
                        except Exception as e_ir_conv:
                            logger.error(f"Failed to generate or cache IR for {module_name}: {e_ir_conv}", exc_info=True)
                            final_analysis_result["ir_generated"] = False
                            ir_cache_status = "disk_miss_generation_exception"
                    # else: ir_cache_status remains 'ir_disk_cache_disabled' or 'ir_generation_disabled'
            else: # self.config.generate_ir is False
                final_analysis_result["ir_generated"] = False
                ir_cache_status = "ir_generation_disabled"
            
            final_analysis_result["ir_cache_status"] = ir_cache_status # Update final status

            final_analysis_result["success"] = not bool(final_analysis_result.get("errors"))
            
        except SyntaxError as se:
            logger.error(f"Syntax error in {rel_path}: {se}")
            final_analysis_result["errors"].append({"type": "SyntaxError", "message": str(se), "file": abs_file_path, "line": se.lineno})
            final_analysis_result["success"] = False
        except Exception as e:
            logger.error(f"Critical error analyzing file {rel_path}: {e}", exc_info=True)
            final_analysis_result["errors"].append({"type": "FileAnalysisFatalError", "message": str(e), "file": abs_file_path})
            final_analysis_result["success"] = False
        
        # Store the complete, final result
        self.file_analysis_results[rel_path] = final_analysis_result
        
        # Also store by module FQN for API resolver fast path
        mod_fqn = final_analysis_result.get("module_name")
        if mod_fqn: self.module_results_by_fqn[mod_fqn] = final_analysis_result

        # Publish update event
        if self.registry:
            event_data = {
                "module_path": module_name, 
                "file_path": rel_path,
                "success": final_analysis_result["success"],
                "ir_generated": final_analysis_result["ir_generated"],
                "ir_cache_status": final_analysis_result["ir_cache_status"],
                "component_count": len(final_analysis_result.get("components", {})),
                "error_count": len(final_analysis_result.get("errors", [])),
                "dynamic_analysis_attempted": final_analysis_result['dynamic_analysis_attempted'],
                "dynamic_analysis_success": final_analysis_result['dynamic_analysis_success'],
            }
            self.registry.publish_event(MODULE_ANALYSIS_UPDATED, event_data, self.COMPONENT_NAME)
        
        return final_analysis_result
    
    
    def _find_python_files(self, path: str) -> List[str]:
        """Find all Python files in the codebase, respecting exclusions."""
        
        python_files = []
        exclude_patterns = self.config.exclude_patterns if self.config else []
        # Add patterns from exclusion file if loaded in config
        # Ensure exclude_files attribute exists before extending
        if self.config and hasattr(self.config, 'exclude_files') and self.config.exclude_files:
            exclude_patterns.extend(self.config.exclude_files)
        
        # Normalize exclude patterns to forward slashes once
        normalized_patterns = [(p or "").replace("\\", "/") for p in exclude_patterns]
        logger.debug(f"Using exclusion patterns: {normalized_patterns}")

        for root, dirs, files in os.walk(path, topdown=True):
            # Filter directories based on exclusion patterns
            dirs[:] = [d for d in dirs if not self._is_excluded(os.path.join(root, d), normalized_patterns)]

            for file in files:
                if file.endswith('.py'):
                    full_path = os.path.join(root, file)
                    if not self._is_excluded(full_path, normalized_patterns):
                        python_files.append(full_path)
                    else:
                        logger.debug(f"Excluding file: {full_path}")
        
        return python_files
    
    def _is_excluded(self, path: str, patterns: List[str]) -> bool:
        """Check if a path matches any exclusion pattern."""
        # Normalize path for consistent matching
        normalized_path = os.path.normpath(path).replace('\\', '/')
        
        for pat in patterns:
            if not pat: 
                continue
            norm_pat = pat.replace('\\', '/')
            
            # If pattern contains glob chars, use fnmatch
            if any(ch in norm_pat for ch in ['*', '?', '[', ']']):
                # Match pattern anywhere in path OR as full path
                # Use **/pattern to match pattern as a segment
                if fnmatch.fnmatch(normalized_path, norm_pat):
                    return True
                # Also check if pattern matches end of path
                if fnmatch.fnmatch(normalized_path, f"*/{norm_pat}"):
                    return True
            else:
                # For non-glob patterns, match at directory/file boundaries only
                path_segments = normalized_path.split('/')
                
                if norm_pat.endswith('/'):
                    # Directory pattern - match against directory segments only
                    dir_name = norm_pat.rstrip('/')
                    if dir_name in path_segments[:-1]:
                        return True
                else:
                    # Could be file or directory - match against any complete segment
                    if norm_pat in path_segments:
                        return True
        return False
    
    def _get_module_name(self, file_path: str, base_path: Optional[str] = None) -> Tuple[str, bool]:
        """
        Get the Python module name from a file path.
        
        Uses detected Python package roots to correctly handle multi-language repos
        where Python code lives in subdirectories (e.g., python-package/, python/).
        """
        abs_file_path = Path(file_path).resolve()
        is_init = abs_file_path.name == '__init__.py'
        
        # Find the best matching Python package root for this file
        effective_base_path: Optional[Path] = None
        
        # Check Python package roots first (more specific)
        for pkg_root in self.python_package_roots:
            try:
                # Check if file is under this package root
                abs_file_path.relative_to(pkg_root)
                effective_base_path = pkg_root
                break
            except ValueError:
                continue
        
        # Fallback to repo_path if no package root matched
        if effective_base_path is None:
            if self.repo_path:
                effective_base_path = Path(self.repo_path).resolve()
            elif base_path:
                effective_base_path = Path(base_path).resolve()
            else:
                # Ultimate fallback
                effective_base_path = abs_file_path.parent.parent
                logger.debug(f"No base path found, defaulting to {effective_base_path}")
        
        try:
            rel_path = abs_file_path.relative_to(effective_base_path)
            module_parts = list(rel_path.parts)
            
            if module_parts[-1] == '__init__.py':
                module_parts.pop()
                is_init = True
            else:
                module_parts[-1] = module_parts[-1].replace('.py', '')
            
            module_name = '.'.join(part for part in module_parts if part)
            
            if not module_name and rel_path.name.endswith('.py'):
                module_name = rel_path.name.replace('.py', '')
            
            # --- Apply project name prefix if needed ---
            if module_name and self.module_name_prefix:
                # Don't double-prefix if already starts with the prefix
                if not module_name.startswith(self.module_name_prefix + '.') and module_name != self.module_name_prefix:
                    module_name = f"{self.module_name_prefix}.{module_name}"
                    logger.debug(f"Applied prefix: {module_name}")
            # ---
            
            return module_name, is_init
            
        except ValueError as ve:
            logger.warning(f"Could not compute relative path for {file_path}: {ve}")
            module_name = abs_file_path.stem
            if module_name == "__init__":
                module_name = abs_file_path.parent.name
                is_init = True
            return module_name, is_init
        except Exception as e:
            logger.error(f"Unexpected error in _get_module_name for {file_path}: {e}", exc_info=True)
            return abs_file_path.stem, False
    
    
    def _calculate_metrics(self) -> Dict[str, Any]:
        """Calculate metrics based on the analysis stored in trackers."""
        import_count = self.import_tracker.get_import_count() if self.import_tracker else 0
        inheritance_count = self.inheritance_tracker.get_inheritance_count() if self.inheritance_tracker else 0
        call_count = self.call_tracker.get_call_count() if self.call_tracker else 0
        total_relationships = import_count + inheritance_count + call_count
        module_count = len([res for res in self.file_analysis_results.values() if res.get("success")])
        metrics = {
            "import_count": import_count, "inheritance_count": inheritance_count,
            "call_count": call_count, "total_relationship_count": total_relationships,
            "module_count": module_count,
        }
        logger.info(f"Calculated metrics: {metrics}")
        return metrics

    
    # --- Public API for accessing tracker data ---

    def get_imports(self, module_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get import relationships, optionally filtered by module."""
        if not self.import_tracker: return []
        if module_name:
            # Assuming ImportTracker has a method like this
            return self.import_tracker.get_module_imports(module_name)
        else:
            # Find all import relationships
            return self.import_tracker.find_imports()

    def get_exports(self, 
                    module_name: str, 
                    include_reexports: Optional[bool] = None, 
                    is_explicit: Optional[bool] = None) -> List[Dict[str, Any]]:
        """
        Get export relationships from a module, with filters.

        Args:
            module_name: The FQN of the exporting module.
            include_reexports: 
                - If True, includes both direct exports and re-exports.
                - If False, includes only direct exports (is_reexport=False).
                - If None (default), includes all exports regardless of re-export status.
            is_explicit: Optional filter for explicit exports (e.g., via __all__).

        Returns:
            List of export relationship dictionaries.
        """
        if not self.export_tracker: return []
        
        filter_is_reexport: Optional[bool] = None
        if include_reexports is False: filter_is_reexport = False
        elif include_reexports is True: filter_is_reexport = None 
        return self.export_tracker.find_exports_from_module(module_fqn=module_name, is_explicit=is_explicit, is_reexport=filter_is_reexport)
    
    
    def get_reexports(self, module_name: str, is_explicit: Optional[bool] = None) -> List[Dict[str, Any]]:
        """
        Get only re-export relationships from a module.
        This is a convenience method.

        Args:
            module_name: The FQN of the exporting module.
            is_explicit: Optional filter for explicit re-exports.

        Returns:
            List of re-export relationship dictionaries.
        """
        if not self.export_tracker: return []
        return self.export_tracker.find_exports_from_module(module_fqn=module_name, is_explicit=is_explicit, is_reexport=True)
    
    
    def get_direct_defined_exports(self, module_name: str, is_explicit: Optional[bool] = None) -> List[Dict[str, Any]]:
        """
        Get exports that are definitions local to the module (not re-exports).

        Args:
            module_name: The FQN of the exporting module.
            is_explicit: Optional filter for explicit exports.
        Returns:
            List of direct, non-re-export relationship dictionaries.
        """
        if not self.export_tracker: return []
        return self.export_tracker.find_exports_from_module(module_fqn=module_name, is_explicit=is_explicit, is_reexport=False)
    
    
    def get_inheritance(self, class_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get inheritance relationships, optionally filtered by class."""
        if not self.inheritance_tracker: return []
        if class_name:
            parents_fqns = self.inheritance_tracker.get_direct_parents(class_name)
            children_fqns = self.inheritance_tracker.get_direct_children(class_name)
            results = []
            # Convert FQNs to richer dicts if needed, or ensure trackers return dicts
            # Assuming get_direct_parents/children return lists of FQNs as per their current sig
            for p_fqn in parents_fqns: results.append({"child": class_name, "parent": p_fqn, "type": "parent"}) # Or relationship details
            for c_fqn in children_fqns: results.append({"child": c_fqn, "parent": class_name, "type": "child"})
            return results
        return self.inheritance_tracker.find_relationships(rel_types=[REL_TYPE_INHERITS])

    
    def get_calls(self, function_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get call relationships, optionally filtered by function."""
        if not self.call_tracker: return []
        return self.call_tracker.find_calls(caller=function_name, callee=function_name if function_name else None)
        

    def get_all_relationships(self) -> List[Dict[str, Any]]:
        """Get all relationships from the underlying store."""
        # This bypasses the specialized trackers' formatting but gives raw data
        all_rels = []
        if self.store and hasattr(self.store, 'get_edges') and callable(self.store.get_edges):
            for src, dst, data in self.store.get_edges(): # data is the attribute dict
                rel = {
                    "source": src,
                    "target": dst,
                    "relationship_type": data.get('edge_type', data.get('type', 'UNKNOWN')), # Accommodate 'type' or 'edge_type'
                    "properties": {k: v for k, v in data.items() if k not in ['edge_type', 'type']}
                }
                all_rels.append(rel)
        return all_rels

    
    # --- Event Handlers for Incremental Updates ---

    def _handle_file_created(self, payload: EventPayload):
        """Handle file creation events from the watcher."""
        file_path = payload.event_specific_data.get("file_path")
        if not file_path or not file_path.endswith(".py") or self._is_excluded(file_path, self.config.exclude_patterns if self.config else []):
            return
        logger.info(f"File created: {file_path}. Triggering analysis.")
        try:
            # When a file is created, it might now satisfy dependencies of other existing modules.
            # Or, existing modules might now import this new module after their next modification.
            # For now, just analyze the new file. 
            # A more advanced system could check if this new module resolves previously unresolvable imports.
            self.analyze_file(file_path)
            
            # After analyzing the new file, its definitions and exports are in the system.
            # Check if any existing modules were waiting for parts of this new module.
            # This is complex; for now, rely on future modifications of dependents to trigger their re-analysis.
            # However, we can check if this newly added module is now imported by any other module.
            # If so, those importers should be re-analyzed.
            new_module_fqn = self._get_module_name(os.path.abspath(file_path), self.repo_path)
            if new_module_fqn:
                importers = self._get_dependent_modules(new_module_fqn) # This will use the new graph state
                # Filter out the new module itself if it somehow appears as its own importer
                importers_to_reanalyze = {imp_fqn for imp_fqn in importers if imp_fqn != new_module_fqn}
                if importers_to_reanalyze:
                    logger.info(f"New module {new_module_fqn} is imported by existing modules: {importers_to_reanalyze}. Triggering their re-analysis.")
                    self._reanalyze_modules(importers_to_reanalyze, f"new module {new_module_fqn} was created and is imported")

        except Exception as e:
            logger.error(f"Error processing created file {file_path}: {e}", exc_info=True)

    
    def _handle_file_modified(self, payload: EventPayload):
        """Handle file modification events from the watcher."""
        file_path = payload.event_specific_data.get("file_path")
        if not file_path or not file_path.endswith(".py") or self._is_excluded(file_path, self.config.exclude_patterns if self.config else []):
            return
        
        abs_file_path = os.path.abspath(file_path)
        module_name_changed = self._get_module_name(abs_file_path, self.repo_path)
        if not module_name_changed:
            logger.error(f"Could not determine module name for modified file {file_path}. Aborting.")
            return
        
        logger.info(f"File modified: {module_name_changed} ({file_path}). Triggering dependency-aware invalidation and re-analysis.")
        already_invalidated_this_cascade = set()
        self._invalidate_module_recursive(module_name_changed, already_invalidated_this_cascade, is_deleted_initial=False)
        self._reanalyze_modules(already_invalidated_this_cascade, f"modification of {module_name_changed}")

    
    def _handle_file_deleted(self, payload: EventPayload):
        """Handle file deletion events from the watcher."""
        file_path = payload.event_specific_data.get("file_path")
        if not file_path or not file_path.endswith(".py"): return

        abs_file_path = os.path.abspath(file_path)
        module_name_deleted = self._get_module_name(abs_file_path, self.repo_path)
        if not module_name_deleted:
            logger.error(f"Could not determine module name for deleted file {file_path}. Aborting.")
            return
            
        logger.info(f"File deleted: {module_name_deleted} ({file_path}). Triggering dependency-aware invalidation.")
        already_invalidated_this_cascade = set()
        self._invalidate_module_recursive(module_name_deleted, already_invalidated_this_cascade, is_deleted_initial=True)
        
        dependents_to_reanalyze = already_invalidated_this_cascade - {module_name_deleted}
        self._reanalyze_modules(dependents_to_reanalyze, f"deletion of {module_name_deleted}")


    def _determine_reanalysis_order(self, modules_to_reanalyze: Set[str]) -> List[str]:
        """
        Determines a safe order for re-analyzing modules based on dependencies.
        """
        if not modules_to_reanalyze: return []

        existing_modules_map: Dict[str, str] = {} # FQN -> file_path
        for module_fqn in modules_to_reanalyze:
            fp = self._get_file_path_from_module_fqn(module_fqn)
            if fp and os.path.exists(fp):
                existing_modules_map[module_fqn] = fp
            else:
                logger.debug(f"Module {module_fqn} excluded from re-analysis order (no existing file path).")
        
        valid_modules_for_sort = set(existing_modules_map.keys())
        if not valid_modules_for_sort: return []

        adj: Dict[str, Set[str]] = defaultdict(set) # module -> set of modules it imports (dependencies)
        in_degree: Dict[str, int] = defaultdict(int)

        for module_m in valid_modules_for_sort:
            in_degree[module_m] = 0 # Initialize for all valid modules
            adj[module_m] = set()   # Initialize for all valid modules


        if self.import_tracker:
            for module_m in valid_modules_for_sort:
                # get_outgoing_imports returns list of FQNs of modules that module_M imports
                imported_modules_by_m = self.import_tracker.get_outgoing_imports(module_m)
                for imported_module_d in imported_modules_by_m:
                    if imported_module_d in valid_modules_for_sort: # Is this dependency part of our current batch?
                        adj[module_m].add(imported_module_d) # M depends on D
                        in_degree[module_m] += 1 # M has one more dependency it's waiting for
        
        # Standard Kahn's: process nodes with 0 in-degree (dependencies satisfied first)
        # Here, adj[M] = {D1, D2} means M imports D1 and D2.
        # In-degree for Kahn's should be "number of incoming edges".
        # So, if M imports D, there's an edge M -> D. D's in-degree increases.
        # We need to build the graph where an edge M -> D means M depends on D.
        # Then process nodes with an in-degree of 0 (no unsatisfied dependencies).

        # Corrected graph construction for topological sort (dependencies first)
        # Graph: node -> list of nodes that depend on it (reverse graph for Kahn's)
        # Or, standard graph (node -> list of nodes it depends on) and adapt Kahn's
        
        # Use: module -> list of modules it depends on (adj as defined above)
        # And in_degree_kahn: module -> count of modules that depend on IT (for starting queue) - NO, this is wrong for Kahn's
        
        # Kahn's:
        # 1. Compute in-degree for each node U: number of V such that V -> U edge exists (V depends on U).
        #    Our adj list is M -> {D1,D2} (M depends on D1, M depends on D2).
        #    So, D1 has an incoming edge from M. D2 has an incoming edge from M.
        
        # Rebuild in_degree correctly for Kahn's algorithm
        # In-degree: how many other modules in the current batch depend on this module.
        # No, standard Kahn's: in_degree[U] = number of nodes V such that V is a prerequisite for U.
        # So, if M imports D (M depends on D), M has D as a prerequisite.
        # The `in_degree` calculated above (adj[M] depends on D, so in_degree[M]++) is correct.

        queue = deque([m for m in valid_modules_for_sort if in_degree[m] == 0])
        sorted_order = []
        
        # To find nodes that depend on `u` (the one just processed):
        # Iterate all nodes `v` in `valid_modules_for_sort`. If `u` is in `adj[v]`, then `v` depends on `u`.
        
        # Create a reverse graph for easier lookup of dependents for Kahn's
        reverse_adj: Dict[str, Set[str]] = defaultdict(set)
        for module_m, dependencies_of_m in adj.items():
            for dep_d in dependencies_of_m:
                reverse_adj[dep_d].add(module_m) # dep_d is a prerequisite for module_m

        count_processed = 0
        while queue:
            u = queue.popleft()
            sorted_order.append(u)
            count_processed +=1

            # For each module V that depends on U (U is a prerequisite for V)
            for v_dependent_on_u in reverse_adj.get(u, []): # Iterate through modules that have U as a dependency
                in_degree[v_dependent_on_u] -= 1
                if in_degree[v_dependent_on_u] == 0:
                    queue.append(v_dependent_on_u)

        if count_processed != len(valid_modules_for_sort):
            # Cycle detected or other issue
            remaining_modules = valid_modules_for_sort - set(sorted_order)
            logger.warning(f"Cycle detected or graph issue during topological sort for re-analysis. Processed: {count_processed}/{len(valid_modules_for_sort)}. Remaining: {remaining_modules}")
            # Add remaining modules, perhaps sorted alphabetically, to ensure they are processed.
            sorted_order.extend(sorted(list(remaining_modules)))
            
        logger.info(f"Determined re-analysis order ({len(sorted_order)} modules): {sorted_order}")
        return sorted_order
    

    def _reanalyze_modules(self, modules_fqn_set: Set[str], trigger_event_description: str):
        """Helper to re-analyze a set of modules in a determined order."""
        if not modules_fqn_set: return

        reanalysis_order = self._determine_reanalysis_order(modules_fqn_set)
        if not reanalysis_order: 
            logger.info(f"No modules to re-analyze for {trigger_event_description} after filtering/ordering.")
            return
            
        logger.info(f"Re-analyzing {len(reanalysis_order)} modules due to {trigger_event_description}. Order: {reanalysis_order}")
        for module_fqn in reanalysis_order:
            file_path = self._get_file_path_from_module_fqn(module_fqn) 
            if file_path: # Path should exist from _determine_reanalysis_order's filtering
                logger.info(f"Re-analyzing {module_fqn} from file {file_path}")
                try:
                    self.analyze_file(file_path)
                except Exception as e:
                    logger.error(f"Error re-analyzing module {module_fqn} at {file_path}: {e}", exc_info=True)
            else: 
                 logger.error(f"File path for module {module_fqn} not found during scheduled re-analysis. This implies an issue in filtering or path resolution.")

    
    def _handle_module_analysis_updated(self, payload: EventPayload):
        """Placeholder handler for MODULE_ANALYSIS_UPDATED event."""
        # Extract data using .get for safety
        module_path = payload.event_specific_data.get("module_path")
        success = payload.event_specific_data.get("success")
        logger.debug(f"{self.COMPONENT_NAME} received MODULE_ANALYSIS_UPDATED for {module_path} (Success: {success}) - Placeholder")
        # TODO: Implement logic if AnalyzerIntegration needs to react to module updates
        #       (e.g., update internal state, trigger dependent analyses)
        pass

        
    def _handle_module_analysis_invalidated(self, payload: EventPayload):
        """Placeholder handler for MODULE_ANALYSIS_INVALIDATED event."""
        # Extract data using .get for safety
        module_path = payload.event_specific_data.get("module_path")
        logger.debug(f"{self.COMPONENT_NAME} received MODULE_ANALYSIS_INVALIDATED for {module_path} - Placeholder")
        # TODO: Implement logic if AnalyzerIntegration needs to react to invalidation
        #       (e.g., clear specific caches related to the module)
        pass
    
    
    def _get_file_path_from_module_fqn(self, module_fqn: str) -> Optional[str]:
        """
        Tries to find the absolute file path for a given module FQN.
        
        This is a helper for invalidation and re-analysis of dependent modules.
        Relies on DefinitionRegistry or cached analysis results.
        """
        if not module_fqn: return None

        if self.definition_registry:
            module_defs = self.definition_registry.get_module_definitions(module_fqn)
            if module_defs:
                # Get the first DefinitionInfo object and its source_file
                first_def_info = next(iter(module_defs.values()), None)
                if first_def_info and first_def_info.source_file:
                    return os.path.abspath(first_def_info.source_file) # Ensure it's absolute (DefinitionRegistry should store absolute paths)
        
        for result_data in self.file_analysis_results.values():
            if result_data.get("module_name") == module_fqn:
                sf = result_data.get("source_file") # Should be absolute
                if sf and os.path.exists(sf): return sf # Check existence
        
        if self.repo_path: # Fallback reconstruction
            parts = module_fqn.split('.')
            rel_path_py = os.path.join(*parts) + ".py"
            abs_path_py = os.path.join(self.repo_path, rel_path_py)
            if os.path.exists(abs_path_py): return abs_path_py
            
            rel_path_pkg = os.path.join(*parts, "__init__.py")
            abs_path_pkg = os.path.join(self.repo_path, rel_path_pkg)
            if os.path.exists(abs_path_pkg): return abs_path_pkg
            
        logger.debug(f"Could not find file path for module {module_fqn}")
        return None

    
    def _get_dependent_modules(self, module_fqn: str) -> Set[str]:
        """
        Identifies all modules that directly depend on the given module_fqn.
        Queries import, call, and inheritance trackers.
        """
        dependents: Set[str] = set()
        if not module_fqn: return dependents

        if self.import_tracker: dependents.update(self.import_tracker.get_importers_of_module(module_fqn))
        if self.call_tracker: dependents.update(self.call_tracker.get_modules_calling_target_module(module_fqn))
        if self.inheritance_tracker: dependents.update(self.inheritance_tracker.get_modules_with_children_of_classes_in_module(module_fqn))
        dependents.discard(module_fqn)
        return dependents

    
    def _perform_single_module_invalidation(self, file_path: str, module_name: str, is_deleted: bool):
        """
        The original logic from _invalidate_file_data, focused on a single module's data cleanup.
        Separated for clarity and reuse in recursive invalidation.
        """
        abs_file_path = os.path.abspath(file_path)
        # Ensure rel_path is calculated correctly even if repo_path is None initially
        rel_path_key = os.path.relpath(abs_file_path, self.repo_path) if self.repo_path and abs_file_path.startswith(self.repo_path) else os.path.basename(abs_file_path)

        logger.debug(f"Performing single module invalidation for {module_name} (File: {rel_path_key}, Deleted: {is_deleted})")

        if rel_path_key in self.file_analysis_results: del self.file_analysis_results[rel_path_key]
        if rel_path_key in self.ir_cache: del self.ir_cache[rel_path_key]
        
        if self.config.generate_ir and self.config.ir_cache_dir:
            try:
                if not is_deleted: 
                    cache_key = generate_cache_key(abs_file_path, self.config)
                    cache_file_path_obj = get_cache_file_path(self.config.ir_cache_dir, cache_key)
                    if cache_file_path_obj.exists(): cache_file_path_obj.unlink()
            except Exception as cache_err: logger.warning(f"Error handling disk IR cache for {module_name}: {cache_err}")

        try:
            if self.definition_registry: self.definition_registry.remove_definitions_by_module(module_name)
            if self.import_tracker: self.import_tracker.remove_imports_by_module(module_name)
            if self.export_tracker: self.export_tracker.remove_exports_by_module(module_name)
            if self.inheritance_tracker: self.inheritance_tracker.remove_inheritance_by_module(module_name)
            if self.call_tracker: self.call_tracker.remove_calls_by_module(module_name)
        except Exception as e: logger.error(f"Error invalidating tracker/registry data for {module_name}: {e}", exc_info=True)

        if self.registry:
            self.registry.publish_event(MODULE_ANALYSIS_INVALIDATED, {"module_path": module_name, "file_path": rel_path_key, "is_deleted": is_deleted}, self.COMPONENT_NAME)
    
    
    def _invalidate_module_recursive(self, module_fqn_to_invalidate: str, already_invalidated_in_cascade: Set[str], is_deleted_initial: bool = False):
        """
        Core logic to invalidate a module and its dependents recursively.
        """
        if module_fqn_to_invalidate in already_invalidated_in_cascade: return

        logger.info(f"Invalidating (recursively): {module_fqn_to_invalidate} (Initial delete: {is_deleted_initial})")
        already_invalidated_in_cascade.add(module_fqn_to_invalidate)

        file_path_to_invalidate = self._get_file_path_from_module_fqn(module_fqn_to_invalidate)
        
        current_module_actually_deleted = is_deleted_initial
        if not file_path_to_invalidate and not is_deleted_initial :
             current_module_actually_deleted = True # Treat as deleted if path not found for a dependent

        if file_path_to_invalidate and os.path.exists(file_path_to_invalidate): # File must exist to be invalidated unless it's the initial deletion
            self._perform_single_module_invalidation(file_path_to_invalidate, module_fqn_to_invalidate, current_module_actually_deleted)
        elif current_module_actually_deleted : # No file path, but treat as deleted (e.g. initial delete or dependent of deleted)
            logger.info(f"Data cleanup for {module_fqn_to_invalidate} (no direct file path, assumed/confirmed deleted).")
            try: # Minimal invalidation - trackers and registry
                if self.definition_registry: self.definition_registry.remove_definitions_by_module(module_fqn_to_invalidate)
                if self.import_tracker: self.import_tracker.remove_imports_by_module(module_fqn_to_invalidate)
                if self.export_tracker: self.export_tracker.remove_exports_by_module(module_fqn_to_invalidate)
                if self.inheritance_tracker: self.inheritance_tracker.remove_inheritance_by_module(module_fqn_to_invalidate)
                if self.call_tracker: self.call_tracker.remove_calls_by_module(module_fqn_to_invalidate)
            except Exception as e: logger.error(f"Error during data cleanup for {module_fqn_to_invalidate} (no file path): {e}", exc_info=True)
            if self.registry:
                 self.registry.publish_event(MODULE_ANALYSIS_INVALIDATED, 
                                     {"module_path": module_fqn_to_invalidate, "file_path": None, "is_deleted": True}, 
                                     self.COMPONENT_NAME)
        else:
            logger.warning(f"Skipping data invalidation for {module_fqn_to_invalidate} as its file path was not found and it was not marked as deleted.")


        # Recursive step: invalidate dependents
        # Only find dependents if the current module was not the one initially marked for deletion.
        # If A -> B, and B is deleted, A becomes stale. We invalidate B, then A.
        # We don't then ask for dependents of B (which is gone) to invalidate further.
        if not is_deleted_initial: # If current module is just stale due to dependency change
            dependents = self._get_dependent_modules(module_fqn_to_invalidate)
            for dep_module_fqn in dependents:
                # Pass is_deleted_initial=False for these dependents, as they are becoming stale, not deleted themselves by this event.
                self._invalidate_module_recursive(dep_module_fqn, already_invalidated_in_cascade, is_deleted_initial=False)
    
    
    def cleanup(self):
        """Clean up resources, including the dynamic analyzer."""
        logger.info(f"Cleaning up {self.COMPONENT_NAME}...")
        if self.dynamic_analyzer and hasattr(self.dynamic_analyzer, 'cleanup'):
            logger.info("Cleaning up DynamicAnalyzer...")
            try:
                self.dynamic_analyzer.cleanup()
                self.dynamic_analyzer = None
            except Exception as e:
                logger.error(f"Error cleaning up DynamicAnalyzer: {e}", exc_info=True)
        # Clear own caches
        self.file_analysis_results.clear()
        self.ir_cache.clear()
        # Clear graph store if this component owns it exclusively
        if self.store:
            self.store.clear() # Or a more specific clear if store is shared
        
        # Clean up external introspector cache
        if self._external_introspector:
            self._external_introspector._save_cache()  # ensure cache is persisted
            self._external_introspector = None
            
        self._initialized_event_handlers = False
        logger.info(f"{self.COMPONENT_NAME} cleanup complete.")
    

    # ------------------------------------------------------------------
    # Helper methods for centralised record application
    # ------------------------------------------------------------------
    
    def _perform_final_iterative_resolution(self) -> None:
        """
        Manages the final resolution pass for any remaining unresolved exports using a dependency-aware topological sort to ensure modules are processed in the correct order.
        
        This method resolves two types of unresolved contexts collected during the initial pass:
        1.  Exports flagged with `needs_linking=True`.
        2.  Modules that perform implicit exports and contain wildcard imports (`module_needs_linking=True`).
        """
        
        if not self.final_unlinked_exports:
            logger.info("Skipping final resolution pass: no unlinked export contexts to process.")
            return

        logger.info(f"Starting final resolution pass for {len(self.final_unlinked_exports)} unlinked export contexts.")

        # --- 1. Build Dependency Graph and Initial Work Set ---
        # The set of all modules that need to be re-processed.
        modules_in_scope = {ctx['exporting_module_fqn'] for ctx in self.final_unlinked_exports}
        
        # adj: An adjacency list where adj[A] = {B} means B depends on A (B imports * from A).
        # in_degree: A map of module -> number of its dependencies within our scope.
        adj = defaultdict(set)
        in_degree = {module_fqn: 0 for module_fqn in modules_in_scope}

        for context in self.final_unlinked_exports:
            importer_module = context['exporting_module_fqn']
            # The 'wildcard_sources' are the modules this importer depends on.
            wildcard_dependencies = context.get('wildcard_sources', [])
            
            for dependency_module in wildcard_dependencies:
                if dependency_module in modules_in_scope:
                    # We have a dependency: importer_module depends on dependency_module.
                    # This means dependency_module is a prerequisite for importer_module.
                    adj[dependency_module].add(importer_module)
                    in_degree[importer_module] += 1

        # --- 2. Perform Topological Sort (Kahn's Algorithm) ---
        # Initialize the queue with all modules that have no dependencies within our work set.
        queue = deque([m for m in modules_in_scope if in_degree[m] == 0])
        processing_order = []
        
        while queue:
            module_to_process = queue.popleft()
            processing_order.append(module_to_process)
            
            # For each module that depends on the one we just processed...
            for dependent_module in adj.get(module_to_process, []):
                in_degree[dependent_module] -= 1
                # If all its dependencies are now met, add it to the queue.
                if in_degree[dependent_module] == 0:
                    queue.append(dependent_module)
        
        # --- 3. Handle Cycles and Finalize Processing Order ---
        if len(processing_order) != len(modules_in_scope):
            cyclical_modules = modules_in_scope - set(processing_order)
            logger.warning(
                f"A cycle was detected in wildcard import dependencies involving: {cyclical_modules}. "
                "These modules will be processed in a fallback order, which may lead to incomplete resolution."
            )
            processing_order.extend(sorted(list(cyclical_modules)))

        logger.info(f"Final resolution processing order ({len(processing_order)} modules): {processing_order}")

        # --- 4. Execute the Resolution Pass in the Correct Order ---
        for module_fqn in processing_order:
            module_result = self.get_analysis_result(module_fqn)
            if not module_result:
                logger.warning(f"Could not find analysis result for '{module_fqn}' during final resolution pass.")
                continue

            # This single call performs both link resolution and wildcard expansion.
            _, updated_module_result = self._resolve_and_expand_module_exports(module_fqn, module_result)
    
            self._apply_export_records(updated_module_result.get("export_records", [])) # Apply this single, now-complete record
        
        logger.info("Final iterative resolution pass complete.")


    def _resolve_and_expand_module_exports(self, module_fqn: str, module_result: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        The unified resolver method. It attempts to resolve unlinked exports and expand exports from wildcard imports for a given module.

        This method modifies the `module_result`'s `export_records` in-place.

        Returns:
            Tuple[bool, Dict[str, Any]]: True if any change was made to the export records, False otherwise along with the updated module_result.
        """
        
        missing_result_count = 0
        made_change = False
        wildcard_source_needs_linking = False
        
        # --- Part 1: Wildcard Expansion and Resolution of Existing Links ---
        wildcard_imports = module_result.get("module_interface", {}).get("wildcard_imports", [])
        
        # Build a temporary map of names available from wildcards for quick lookup
        wildcard_namespace: Dict[str, Dict] = {} # name -> {export_record, providing_module_fqn}
        
        for source_module_fqn in wildcard_imports:
            if not source_module_fqn:
                continue
            
            source_module_result = self.get_analysis_result(source_module_fqn)            
            if not source_module_result:
                missing_result_count += 1
                continue # Source module not analyzed yet

            if source_module_result.get("module_interface", {}).get("module_needs_linking"):
                wildcard_source_needs_linking = True

            for source_export_rec in source_module_result.get("export_records", []):
                exported_name = source_export_rec.get("exported_name")
                if not exported_name: continue
                # If name is not already defined locally or shadowed by another import, add it
                if exported_name not in wildcard_namespace:
                    # Store both the export record AND which module provided it
                    wildcard_namespace[exported_name] = {
                        "export_record": source_export_rec,
                        "providing_module": source_module_fqn  # Track where we got this from
                    }
                    

        # Iterate through this module's exports to resolve unlinked ones
        current_exports = []
        for export_rec in module_result.get("export_records", []):
            exported_name = export_rec.get("exported_name")
            if exported_name: current_exports.append(exported_name)
            if export_rec.get("needs_linking", False) and exported_name in wildcard_namespace:
                
                source_info = wildcard_namespace[exported_name]
                source_rec = source_info["export_record"]
                providing_module = source_info["providing_module"]
                
                target_fqn = source_rec.get("target_item_fqn")
                if target_fqn:
                    # Link it!
                    # Resolve the pointer to find the true definition FQN
                    final_target_fqn = self._resolve_pointer(target_fqn)
                    
                    export_rec["target_item_fqn"] = final_target_fqn
                    export_rec["component_kind"] = source_rec.get("component_kind", None)
                    export_rec["source_module"] = providing_module
                    export_rec["is_internal"] = source_rec.get("is_internal", None)
                    export_rec["needs_linking"] = False
                    logger.debug(f"Resolved link for '{exported_name}' in {module_fqn} -> {export_rec['target_item_fqn']} via wildcard import from {providing_module}.")
                    made_change = True
        
        # --- Part 2: Implicit Export Expansion ---
        # If this module exports implicitly, add the names from the wildcard namespace to the export records
        if module_result.get("module_interface", {}).get("module_needs_linking"):
            logger.debug("--- Started implicit export expansion ---")
            all_vals = module_result["module_interface"]["all_values"] or []
            if isinstance(all_vals, list): all_vals_set = set(all_vals)
            elif isinstance(all_vals, set): all_vals_set = all_vals
            else: all_vals_set = set(all_vals)
            
            for name, source_info in wildcard_namespace.items():
                if name and name not in current_exports:
                    source_rec = source_info["export_record"]
                    providing_module = source_info["providing_module"]
                    
                    target_fqn = source_rec.get("target_item_fqn")
                    if not target_fqn: continue
                    
                    # Resolve pointer to final definition
                    final_target_fqn = self._resolve_pointer(target_fqn)
                    
                    new_re_export_rec = {
                        "exporting_module_fqn": module_fqn,
                        "exported_name": name,
                        "target_item_fqn": final_target_fqn,
                        "is_reexport": True,
                        "is_explicit": False,
                        "is_wildcard_reexport": True,
                        "needs_linking": False,
                        "wildcard_sources": source_rec.get("wildcard_sources", []),
                        "is_internal": source_rec.get("is_internal", None),
                        "source_module": providing_module,
                        "component_kind": source_rec.get("component_kind", None),
                        "metadata": source_rec.get("metadata", {})
                    }
                    module_result["export_records"].append(new_re_export_rec)
                    all_vals_set.add(name)
                    logger.debug(f"Expanded implicit export '{name}' in {module_fqn} from wildcard import.")
                    made_change = True
            module_result["module_interface"]["all_values"] = list(all_vals_set)
            
            # If the wildcard source doesn't need linking, the export expansion for this module is complete
            if missing_result_count == 0 and not wildcard_source_needs_linking:
                module_result.get("module_interface")["module_needs_linking"] = False # Reset the flag
                # Remove any dummy placeholder export record
                module_result["export_records"] = [rec for rec in module_result["export_records"] if not hasattr(rec, "is_dummy")]

        return made_change, module_result

    
    def _resolve_pointer(self, fqn: str, visited: set = None) -> str:
        """
        Recursively follows re-export chains to find the true definition FQN.
        This handles cases where an export points to another re-export rather than the definition.
        
        Also handles fallback classes: if the target is an exception fallback class,
        checks if there's an import in the same module that provides the "real" source.
        
        Uses a cache to avoid redundant resolution.
        
        Args:
            fqn: The FQN to resolve.
            visited: Set of visited FQNs to prevent infinite loops.
            
        Returns:
            The resolved definition FQN or the original fqn.
        """
        # Check cache first
        if fqn in self._resolved_fqn_cache:
            return self._resolved_fqn_cache[fqn]
        
        if not fqn: 
            return fqn
        if visited is None: 
            visited = set()
        if fqn in visited: 
            return fqn  # Cycle detected
        visited.add(fqn)
        
        # Check if this FQN structure implies a module member (has a dot)
        if '.' not in fqn:
            self._resolved_fqn_cache[fqn] = fqn
            return fqn
        
        mod_name, member_name = fqn.rsplit('.', 1)
        mod_result = self.module_results_by_fqn.get(mod_name)
        if not mod_result: 
            return fqn
        
        # Check if it is defined here as a component
        components = mod_result.get("components", {})
        if fqn in components:
            comp_data = components[fqn]
            
            # Check if this is an exception fallback class
            if comp_data.get("is_exception_fallback", False):
                # Look for an import with the same name in this module
                # That import is the "real" source
                for import_rec in mod_result.get("import_records", []):
                    if import_rec.get("name_bound_in_importer") == member_name:
                        real_fqn = import_rec.get("name_bound_points_to_fqn")
                        if real_fqn and real_fqn != fqn:
                            logger.debug(
                                f"Fallback class {fqn} has import alias, using: {real_fqn}"
                            )
                            # DON'T resolve further - use the import's original value (which points to the external module)
                            self._resolved_fqn_cache[fqn] = real_fqn
                            return real_fqn
            
            # Normal component (this is the definition)
            self._resolved_fqn_cache[fqn] = fqn 
            return fqn
        
        # Follow the link if it's re-exported
        for exp in mod_result.get("export_records", []):
            if exp.get("exported_name") == member_name:
                next_target = exp.get("target_item_fqn")
                # If it points to something else, recurse
                if next_target and next_target != fqn:
                    resolved = self._resolve_pointer(next_target, visited)
                    self._resolved_fqn_cache[fqn] = resolved
                    return resolved
        
        return fqn
    
    
    def _merge_dynamic_and_static_imports(self, static_import_records: List[Dict[str, Any]], runtime_imported_modules: List[str]) -> List[Dict[str, Any]]:
        """
        Filters static import records against the list of modules actually imported at runtime.

        Args:
            static_import_records: The list of ImportRecord dicts from CodeVisitor.
            runtime_imported_modules: A list of FQNs of modules confirmed to be imported at runtime by DynamicAnalyzer.

        Returns:
            A new, filtered list of import records that reflects runtime reality.
        """
        if not runtime_imported_modules:
            # If dynamic analysis didn't run or returned no imports, trust the static data.
            return static_import_records

        final_imports: List[Dict[str, Any]] = []
        runtime_imports_set = set(runtime_imported_modules)

        for record in static_import_records:
            # The `source_module_fqn` is the module being imported from. This is what we need to check against the runtime list
            if record.get("source_module_fqn") in runtime_imports_set:
                final_imports.append(record)
            else:
                logger.debug(f"Filtering out static import of '{record.get('source_module_fqn')}' in module '{record.get('importer_module_fqn')}' because it was not observed at runtime.")
        
        return final_imports
    
    def _merge_dynamic_and_static_exports(self,
                                          static_export_records: List[Dict[str, Any]],
                                          dynamic_export_observations: List[Dict[str, Any]],
                                          module_fqn: str) -> List[Dict[str, Any]]:
        """
        Merges export records from static analysis with observations from dynamic analysis.

        The dynamic analysis results are considered more authoritative because they reflect the module's final runtime state. This method will:
        - Prioritize records found by dynamic analysis.
        - Use dynamic analysis results to resolve static records that were marked `needs_linking`.
        - Add new export records discovered only during dynamic analysis.
        - Retain purely static exports that were not observed dynamically, marking them for potential review.

        Args:
            static_export_records: The list of export records from CodeVisitor.
            dynamic_export_observations: The list of export records from DynamicAnalyzer.
            module_fqn: The FQN of the module being analyzed.

        Returns:
            A single, comprehensive list of the final `ExportRecord` dictionaries for the module.
        """
        final_exports: List[Dict[str, Any]] = []
        static_exports_map = {rec['exported_name']: rec for rec in static_export_records}
        dynamic_exports_map = {obs['exported_name']: obs for obs in dynamic_export_observations}

        # Combine all unique exported names from both sources to ensure we process everything.
        all_observed_names = set(static_exports_map.keys()) | set(dynamic_exports_map.keys())

        for name in all_observed_names:
            static_rec = static_exports_map.get(name)
            dynamic_obs = dynamic_exports_map.get(name)

            if dynamic_obs:
                # --- Case 1: The export was observed by Dynamic Analysis ---
                # This is our primary source of truth.
                final_rec = dynamic_obs.copy() # Work with a copy

                # Ensure the exporting module FQN is correctly set from the context
                final_rec['exporting_module_fqn'] = module_fqn
                
                # Carry over component_kind from static if dynamic didn't set it
                if not final_rec.get("component_kind", True) and static_rec and static_rec.get("component_kind"):
                    final_rec["component_kind"] = static_rec.get("component_kind")
                
                # Check if this dynamic observation resolves a previously unlinked static record
                if static_rec and static_rec.get("needs_linking"):
                    if final_rec.get("target_item_fqn"):
                        # Success! The dynamic analysis provided the missing link.
                        final_rec["needs_linking"] = False
                        logger.debug(f"Dynamic analysis resolved link for '{name}' in {module_fqn} -> {final_rec['target_item_fqn']}")
                    else:
                        # Dynamic analysis saw the export but also couldn't find its FQN.
                        # This is rare but possible. Keep it marked for the final linking pass.
                        final_rec["needs_linking"] = True
                        logger.debug(f"Dynamic analysis observed '{name}' but could not determine its FQN. Deferring to final linking.")
                
                final_exports.append(final_rec)

            elif static_rec:
                # --- Case 2: The export was ONLY found by Static Analysis ---
                # This could be a valid export that dynamic analysis missed (e.g., in a conditional block `if sys.version_info ...` that wasn't met), or it could be an export that was dynamically removed. We will trust the static record for now but could add a flag for further analysis if needed
                logger.debug(f"Export '{name}' from {module_fqn} was found statically but not dynamically. Retaining static record as is.")
                
                # We add the static record directly. Its `needs_linking` flag (if any) will be carried forward to the on-the-fly or final linking phases
                final_exports.append(static_rec)

        return final_exports
    
    
    def _recalculate_module_statistics(self,
                                     module_fqn: str,
                                     final_export_records: List[Dict[str, Any]],
                                     static_import_records: List[Dict[str, Any]], # From original static pass
                                     static_module_interface: Dict[str, Any],   # From original static pass
                                     static_components_count: int,                # From original static pass
                                     is_init_file: bool,                          # From original static pass
                                     module_docstring: Optional[str]              # From original static pass
                                     ) -> Dict[str, Any]:
        """Recalculates module statistics based on final (merged) export records."""
        logger.debug(f"Recalculating module statistics for {module_fqn} based on final export records.")
        
        export_count = len(final_export_records)
        implicit_export_count = len([e for e in final_export_records if not e.get("is_explicit", False)])
        explicit_export_count = export_count - implicit_export_count
        reexport_count = len([e for e in final_export_records if e.get("is_reexport", False)])
        direct_export_count = export_count - reexport_count
        
        import_count = len(static_import_records)
        external_import_count = 0
        for imp_rec_dict in static_import_records:
            source_mod = imp_rec_dict.get("source_module_fqn")
            is_internal = False
            if source_mod:
                # Check against top_level_packages of the repository
                imp_top_level = source_mod.split('.')[0]
                if imp_top_level in self.top_level_packages:
                    is_internal = True
            if not is_internal and source_mod: # If not internal and has a source, it's external
                external_import_count +=1
                
        internal_import_count = import_count - external_import_count
        wildcard_import_count = len(static_module_interface.get("wildcard_imports", []))
        
        export_import_ratio = export_count / import_count if import_count > 0 else float(export_count)
        external_import_ratio = external_import_count / import_count if import_count > 0 else 0.0
        reexport_ratio = reexport_count / export_count if export_count > 0 else 0.0
        
        has_docstring = bool(module_docstring)
        # Use __all__ status from the module_interface, which might have been updated by dynamic analysis
        has_explicit_all = static_module_interface.get("has_all", False) and \
                           not static_module_interface.get("all_is_dynamic", False) # True if __all__ is present and resolved
        module_depth = module_fqn.count('.')
        
        return {
            "export_count": export_count, "implicit_export_count": implicit_export_count,
            "explicit_export_count": explicit_export_count, "reexport_count": reexport_count,
            "direct_export_count": direct_export_count, "import_count": import_count,
            "external_import_count": external_import_count, "internal_import_count": internal_import_count,
            "wildcard_import_count": wildcard_import_count, "export_import_ratio": export_import_ratio,
            "external_import_ratio": external_import_ratio, "reexport_ratio": reexport_ratio,
            "is_init_file": is_init_file, "has_docstring": has_docstring, 
            "has_explicit_all": has_explicit_all, "module_depth": module_depth, 
            "module_name": module_fqn,
            "needs_dynamic_analysis": static_module_interface.get("needs_dynamic_analysis", False), 
             # component_counts and total_component_count are from static analysis, not directly affected by export merging
            "component_counts": static_module_interface.get("component_counts", {}),
            "total_component_count": static_components_count,
        }
    
    
    def _apply_import_records(self, records: List[Dict[str, Any]]) -> None:
        """Apply import records to ImportTracker."""
        if not self.import_tracker or not self.store or not records: return
        # Batch-mode write for performance
        self.store.begin_batch()
        for record_dict in records:
            try:
                record = ImportRecord(**record_dict)
                self.import_tracker.add_import(record)
            except Exception as exc:
                logger.warning(f"Could not add ImportRecord {record_dict.get('importer_module_fqn', 'UNKNOWN')}.{record_dict.get('name_bound_in_importer','UNKNOWN')}: {exc}")
        self.store.commit_batch()

    
    def _apply_export_records(self, records: List[Dict[str, Any]]) -> None:
        """Apply export records to ExportTracker."""
        if not self.export_tracker or not self.store or not records: return
        self.store.begin_batch()
        for record in records:
            try:
                if not record.get("needs_linking") and record.get("target_item_fqn"):
                    self.export_tracker.add_export(
                    exporting_module_fqn=record["exporting_module_fqn"],
                    target_component_fqn=record["target_item_fqn"],
                    exported_name=record["exported_name"],
                    is_reexport=record.get("is_reexport", False),
                    is_explicit=record.get("is_explicit", False),
                    metadata=record.get("metadata",{})
                )
                
            except Exception as exc:
                logger.warning(f"Could not add export record {record.get('exporting_module_fqn')}.{record.get('exported_name')}: {exc}")
        self.store.commit_batch()
    
        
    def _finalize_all_target_fqns(self):
        """
        Post-processing pass to ensure all target_item_fqn values point to true definitions.
        
        Runs after all module wildcard resolutions are complete. 
        Iterates all export records and uses _resolve_pointer to chase down chains.
        Also sets is_internal for exports that didn't have it set (Case 3: wildcard/dynamic).
        """
        logger.info("Finalizing all target_item_fqn references...")
        changes_made = 0
        is_internal_set = 0
        
        for mod_fqn, mod_result in self.module_results_by_fqn.items():
            for exp_rec in mod_result.get("export_records", []):
                original_target = exp_rec.get("target_item_fqn")
                if original_target:
                    resolved_target = self._resolve_pointer(original_target)
                    if resolved_target != original_target:
                        exp_rec["target_item_fqn"] = resolved_target
                        changes_made += 1
                        logger.debug(f"Finalized {mod_fqn}.{exp_rec.get('exported_name')}: {original_target} -> {resolved_target}")
                        
                    # Set is_internal if not already set (handles Case 3: wildcard/dynamic exports)
                    if "is_internal" not in exp_rec or exp_rec.get("is_internal") is None:
                        target_top_level = resolved_target.split('.')[0]
                        exp_rec["is_internal"] = target_top_level in self.top_level_packages
                        is_internal_set += 1
                        logger.debug(f"Set is_internal={exp_rec['is_internal']} for {mod_fqn}.{exp_rec.get('exported_name')} -> {resolved_target}")
        
        if changes_made > 0:
            logger.info(f"Finalization complete. Updated {changes_made} export records.")
        if is_internal_set > 0:
            logger.info(f"Set is_internal for {is_internal_set} export records.")
        if changes_made == 0 and is_internal_set == 0:
            logger.debug("Finalization pass: no changes needed (already resolved or cached).")
            
    
    def _finalize_all_import_records(self):
        """
        Post-processing pass to ensure all import record FQNs point to true definitions.
        
        For each import record, resolves name_bound_points_to_fqn via _resolve_pointer
        to find the true definition module (not re-exporting module).
        
        Also updates is_source_internal based on the resolved FQN's top-level package.
        
        Must run AFTER _finalize_all_target_fqns() so export chains are already resolved.
        """
        logger.info("Finalizing all import record FQNs...")
        fqn_changes = 0
        internal_changes = 0
        
        for mod_fqn, mod_result in self.module_results_by_fqn.items():
            for import_rec in mod_result.get("import_records", []):
                # Skip wildcard imports - they don't have a specific target
                if import_rec.get("is_wildcard"):
                    continue
                
                original_fqn = import_rec.get("name_bound_points_to_fqn")
                if not original_fqn:
                    continue
                
                # Resolve to true definition
                resolved_fqn = self._resolve_pointer(original_fqn)
                
                if resolved_fqn != original_fqn:
                    import_rec["name_bound_points_to_fqn"] = resolved_fqn
                    import_rec["original_import_fqn"] = original_fqn  # Keep original for reference
                    fqn_changes += 1
                    logger.debug(f"Finalized import in {mod_fqn}: '{import_rec.get('name_bound_in_importer')}' {original_fqn} -> {resolved_fqn}")
                
                # Recalculate is_source_internal based on resolved FQN
                resolved_top_level = resolved_fqn.split('.')[0] if resolved_fqn else ""
                new_is_internal = resolved_top_level in self.top_level_packages
                
                if import_rec.get("is_source_internal") != new_is_internal:
                    import_rec["is_source_internal"] = new_is_internal
                    internal_changes += 1
                    logger.debug(f"Updated is_source_internal for '{import_rec.get('name_bound_in_importer')}' in {mod_fqn}: {not new_is_internal} -> {new_is_internal}")
        
        if fqn_changes > 0:
            logger.info(f"Finalized {fqn_changes} import record FQNs.")
        if internal_changes > 0:
            logger.info(f"Updated is_source_internal for {internal_changes} import records.")
        if fqn_changes == 0 and internal_changes == 0:
            logger.debug("Import record finalization: no changes needed.")
    
    
    def _finalize_all_base_fqns(self):
        """
        Post-processing pass to ensure all base_fqns point to true definitions.
        
        For each class, resolves base_fqns by:
        1. Parse the base string structure (simple name vs attribute chain)
        2. Check if defined locally in the same module
        3. Look up in (now-corrected) import records
        4. Apply _resolve_pointer to chase re-export chains
        
        Must run AFTER _finalize_all_import_records() so import FQNs are correct.
        """
        logger.info("Finalizing all base_fqns references...")
        changes_made = 0
        
        for mod_fqn, mod_result in self.module_results_by_fqn.items():
            import_records = mod_result.get("import_records", [])
            components = mod_result.get("components", {})
            
            # Build lookup maps
            local_component_names = {
                comp_fqn.rsplit('.', 1)[-1]: comp_fqn 
                for comp_fqn in components.keys()
            }
            
            imported_names = {}
            for import_rec in import_records:
                if not import_rec.get("is_wildcard"):
                    bound_name = import_rec.get("name_bound_in_importer")
                    resolved_fqn = import_rec.get("name_bound_points_to_fqn")
                    if bound_name and resolved_fqn:
                        imported_names[bound_name] = resolved_fqn
            
            # Process each class
            for comp_fqn, comp_data in components.items():
                if comp_data.get("component_kind") != "class":
                    continue
                
                bases = comp_data.get("bases", [])
                original_base_fqns = comp_data.get("base_fqns", [])
                
                if not bases:
                    continue
                
                resolved_base_fqns = []
                
                for i, base_str in enumerate(bases):
                    original_fqn = original_base_fqns[i] if i < len(original_base_fqns) else None
                    
                    # Parse the base string to understand its structure
                    resolved_fqn = self._resolve_single_base(
                        base_str=base_str,
                        original_fqn=original_fqn,
                        local_components=local_component_names,
                        imported_names=imported_names,
                        current_module=mod_fqn
                    )
                    
                    resolved_base_fqns.append(resolved_fqn)
                    
                    if original_fqn and resolved_fqn != original_fqn:
                        changes_made += 1
                        logger.debug(f"Finalized base for {comp_fqn}: {original_fqn} -> {resolved_fqn}")
                
                comp_data["base_fqns"] = resolved_base_fqns
        
        if changes_made > 0:
            logger.info(f"Finalized {changes_made} base_fqns.")
        else:
            logger.debug("Base FQN finalization: no changes needed.")


    def _resolve_single_base(
        self,
        base_str: str,
        original_fqn: Optional[str],
        local_components: Dict[str, str],
        imported_names: Dict[str, str],
        current_module: str
    ) -> str:
        """
        Resolve a single base class string to its true definition FQN.
        
        Resolution priority:
        1. Handle complex bases (generics, calls) - can't resolve
        2. Check if it's a local definition
            - But if local is an exception fallback AND there's an import, prefer the import
        3. Check if it's a direct import
        4. Check if it's a runtime alias
        5. Check wildcard imports
        6. Fallback to original FQN or assume local
        
        Args:
            base_str: The base class as written in code (e.g., "ABC", "module.Class")
            original_fqn: The preliminary FQN from CodeVisitor
            local_components: Map of simple names -> FQN for local definitions
            imported_names: Map of imported names -> corrected FQN
            current_module: FQN of the module containing the class
            
        Returns:
            Resolved FQN for the base class
        """
        # Handle complex bases (with brackets, calls, etc.) - can't resolve
        if '[' in base_str or '(' in base_str:
            return original_fqn or f"{current_module}.{base_str}"
        
        parts = base_str.split('.')
        first_part = parts[0]
        rest_parts = parts[1:] if len(parts) > 1 else []
        
        # Priority 1: Check if it's a local definition
        if not rest_parts and first_part in local_components:
            local_fqn = local_components[first_part]
            
            # Check if this local definition is an exception fallback
            is_fallback = self._is_exception_fallback_class(local_fqn)
            
            if is_fallback and first_part in imported_names:
                # Local is a fallback, and there's an import with the same name
                # Prefer the import (e.g., from sklearn.base import ClassifierMixin as XGBClassifierBase)
                imported_fqn = imported_names[first_part]
                logger.debug(f"Base '{base_str}' has fallback local definition, preferring import: {imported_fqn}")
                return self._resolve_pointer(imported_fqn)
            else:
                # Use normal local definition
                return self._resolve_pointer(local_fqn)
        
        # Priority 2: Check if first part is a direct import
        if first_part in imported_names:
            imported_fqn = imported_names[first_part]
            if rest_parts:
                # Reconstruct: imported_fqn + rest of chain
                resolved = f"{imported_fqn}.{'.'.join(rest_parts)}"
            else:
                resolved = imported_fqn
            return self._resolve_pointer(resolved)
        
        # Priority 3: Try the full base_str as an import
        if base_str in imported_names:
            return self._resolve_pointer(imported_names[base_str])
        
        # Priority 4: Check if it's a runtime alias [Handles: BaseAlias = ImportedClass; class Child(BaseAlias)]
        alias_resolved = self._resolve_base_through_alias(
            base_str=base_str,
            local_components=local_components,
            imported_names=imported_names,
            current_module=current_module
        )
        if alias_resolved:
            logger.debug(f"Resolved base '{base_str}' through alias -> {alias_resolved}")
            return alias_resolved
        
        # Priority 5: Could be a wildcard import
        candidate = self._try_resolve_wildcard_import(first_part, current_module)
        if candidate:
            if rest_parts:
                resolved = f"{candidate}.{'.'.join(rest_parts)}"
            else:
                resolved = candidate
            return self._resolve_pointer(resolved)
        
        # Priority 6: Fallback assumes local or uses original FQN
        if original_fqn:
            return self._resolve_pointer(original_fqn)
        else:
            return f"{current_module}.{base_str}"

    def _is_exception_fallback_class(self, class_fqn: str) -> bool:
        """
        Check if a class is an exception fallback (defined inside an except block).
        
        Args:
            class_fqn: Fully qualified name of the class
            
        Returns:
            True if the class was defined inside an except handler
        """
        # Find the module containing this class
        parts = class_fqn.rsplit('.', 1)
        if len(parts) < 2:
            return False
        
        module_fqn = parts[0]
        class_name = parts[1]
        
        # Check deeper nesting (e.g., Module.OuterClass.InnerClass)
        # Walk up the hierarchy to find the module
        mod_result = self.module_results_by_fqn.get(module_fqn)
        while not mod_result and '.' in module_fqn:
            module_fqn = module_fqn.rsplit('.', 1)[0]
            mod_result = self.module_results_by_fqn.get(module_fqn)
        
        if not mod_result: return False
        
        components = mod_result.get("components", {})
        comp_data = components.get(class_fqn)
        
        if not comp_data: return False
        
        return comp_data.get("is_exception_fallback", False)

    def _try_resolve_wildcard_import(self, name: str, current_module: str) -> Optional[str]:
        """
        Try to resolve a name that might come from a wildcard import.
        
        Checks:
            1. Modules that current_module wildcard-imports
            2. What those modules export
            3. Whether 'name' is among the exports
        
        Args:
            name: The name to resolve
            current_module: Module that has the wildcard import
            
        Returns:
            The target FQN if found, None otherwise
        """
        mod_result = self.module_results_by_fqn.get(current_module)
        if not mod_result:
            return None
        
        # Get wildcard import sources from module interface
        wildcard_sources = mod_result.get("module_interface", {}).get("wildcard_imports", [])
        
        # Also check import records for wildcard imports
        for import_rec in mod_result.get("import_records", []):
            if import_rec.get("is_wildcard"):
                source_mod = import_rec.get("source_module_fqn")
                if source_mod and source_mod not in wildcard_sources:
                    wildcard_sources.append(source_mod)
        
        for source_mod in wildcard_sources:
            source_result = self.module_results_by_fqn.get(source_mod)
            if not source_result:
                continue
            
            # Check 1: If 'name' is in the source module's export_records
            for export_rec in source_result.get("export_records", []):
                if export_rec.get("exported_name") == name:
                    target = export_rec.get("target_item_fqn")
                    if target:
                        return target
            
            # Check 2: If 'name' is defined as a component in the source module
            components = source_result.get("components", {})
            potential_fqn = f"{source_mod}.{name}"
            if potential_fqn in components:
                comp_data = components[potential_fqn]
                # Only return if it's a class (what we're looking for as a base)
                if comp_data.get("component_kind") == "class":
                    return potential_fqn
            
            # Check 3: If 'name' is in the module's __all__
            module_interface = source_result.get("module_interface", {})
            all_values = module_interface.get("all_values", [])
            if name in all_values:
                # It's explicitly exported, check if we can find its FQN
                potential_fqn = f"{source_mod}.{name}"
                if potential_fqn in components:
                    return potential_fqn
        
        return None
    
    def _resolve_runtime_alias(
        self, 
        name: str, 
        current_module: str
    ) -> Optional[str]:
        """
        Check if 'name' is a runtime alias (variable assignment pointing to a class).
        
        Handles patterns like:
            BaseAlias = ImportedClass
            MyModel = module.SomeClass
        
        If 'name' is a module-level variable whose value_repr references another
        class/name, this traces back to find the original name.
        
        Args:
            name: The name used in class definition (e.g., "BaseAlias")
            current_module: FQN of the module containing the class
            
        Returns:
            The original name that the alias points to, or None if not an alias.
            Returns the VALUE (right-hand side), not the FQN.
        """
        mod_result = self.module_results_by_fqn.get(current_module)
        if not mod_result:
            return None
        
        components = mod_result.get("components", {})
        
        # Look for a Variable component with this name
        variable_fqn = f"{current_module}.{name}"
        variable_data = components.get(variable_fqn)
        
        if not variable_data:
            return None
        
        # Must be a variable component
        if variable_data.get("component_kind") != "variable":
            return None
        
        # Get the value_repr (right-hand side of assignment)
        value_repr = variable_data.get("value") or variable_data.get("value_repr")
        
        if not value_repr:
            return None
        
        # Clean up the value_repr
        # It could be a simple name: "ImportedClass"
        # Or a dotted path: "module.SomeClass"
        # Or a complex expression: "get_class()" - can't resolve these
        
        # Skip complex expressions
        if '(' in value_repr or '[' in value_repr or ' ' in value_repr:
            return None
        
        # Return the original name (what the alias points to)
        return value_repr.strip()

    
    def _resolve_base_through_alias(
        self,
        base_str: str,
        local_components: Dict[str, str],
        imported_names: Dict[str, str],
        current_module: str
    ) -> Optional[str]:
        """
        Resolve a base class name that might be a runtime alias.
        
        This method:
        1. Checks if base_str is a runtime alias (variable assignment)
        2. If so, gets the original name it points to
        3. Then resolves that original name through local defs, imports, or wildcards
        
        Args:
            base_str: The base class name as used in code
            local_components: Map of simple names -> FQN for local definitions
            imported_names: Map of imported names -> corrected FQN
            current_module: FQN of the current module
            
        Returns:
            Resolved FQN if found through alias resolution, None otherwise
        """
        # Step 1: Check if this is a runtime alias
        original_name = self._resolve_runtime_alias(base_str, current_module)
        
        if not original_name:
            return None  # Not an alias, let caller handle normally
        
        logger.debug(f"Found runtime alias: {base_str} -> {original_name}")
        
        # Step 2: Parse the original name (could be dotted: "module.Class")
        parts = original_name.split('.')
        first_part = parts[0]
        rest_parts = parts[1:] if len(parts) > 1 else []
        
        # Step 3: Check if original name is a local definition
        if not rest_parts and first_part in local_components:
            resolved = local_components[first_part]
            return self._resolve_pointer(resolved)
        
        # Step 4: Check if original name is a direct import
        if first_part in imported_names:
            imported_fqn = imported_names[first_part]
            if rest_parts:
                resolved = f"{imported_fqn}.{'.'.join(rest_parts)}"
            else:
                resolved = imported_fqn
            return self._resolve_pointer(resolved)
        
        # Step 5: Check if original name is the full import path
        if original_name in imported_names:
            return self._resolve_pointer(imported_names[original_name])
        
        # Step 6: Check wildcard imports for the original name
        candidate = self._try_resolve_wildcard_import(first_part, current_module)
        if candidate:
            if rest_parts:
                resolved = f"{candidate}.{'.'.join(rest_parts)}"
            else:
                resolved = candidate
            return self._resolve_pointer(resolved)
        
        # Step 7: Recursively check if original_name is ALSO an alias
        # (handles chained aliases: A = B, B = C, class X(A))
        nested_result = self._resolve_base_through_alias(
            base_str=first_part,
            local_components=local_components,
            imported_names=imported_names,
            current_module=current_module
        )
        
        if nested_result:
            if rest_parts:
                return f"{nested_result}.{'.'.join(rest_parts)}"
            return nested_result
        
        return None  # Could not resolve through alias

    def _test_package_importability(self) -> Tuple[bool, Optional[str]]:
        """
        Test if the main package can be imported in the venv.
        
        This detects cases where stub .py files exist but require compiled
        extensions or dependencies that aren't installed.
        
        Returns:
            Tuple of (can_import: bool, error_message: Optional[str])
        """
        if not self.dynamic_analyzer or not self.dynamic_analyzer.venv:
            return True, None
        
        # Get the main package name from project metadata (authoritative source)
        main_package = None
        if self.registry and hasattr(self.registry, "get_project_metadata"):
            project_metadata = self.registry.get_project_metadata()
            if project_metadata:
                main_package = project_metadata.get("name")
        
        # Fallback: try first top-level package if no project metadata
        if not main_package and self.top_level_packages:
            main_package = next(iter(self.top_level_packages))
        
        if not main_package:
            return True, None
        
        # Normalize package name for import (hyphens → underscores)
        import_name = main_package.replace('-', '_')
        
        # Quick test import
        test_script = f'''import sys
try:
    import {import_name}
    print("OK")
except ImportError as e:
    print(f"IMPORT_ERROR: {{e}}")
except Exception as e:
    print(f"ERROR: {{e}}")
'''
        python_exe = self.dynamic_analyzer.venv.effective_python_executable
        if not python_exe:
            return True, None
        try:
            logger.debug(f"Testing import of '{import_name}' in venv...")
            result = subprocess.run(
                [str(python_exe), "-c", test_script],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self.repo_path) if self.repo_path else None
            )
            
            output = result.stdout.strip()
            stderr = result.stderr.strip()
            
            logger.debug(f"Import test stdout: {output[:100] if output else '(empty)'}")
            if stderr:
                logger.debug(f"Import test stderr: {stderr[:200]}")
            
            if output == "OK":
                logger.debug(f"Package '{import_name}' import test succeeded")
                return True, None
            elif output.startswith("IMPORT_ERROR:"):
                error_msg = output[len("IMPORT_ERROR:"):].strip()
                logger.info(f"Package '{import_name}' import test failed: {error_msg}")
                return False, error_msg
            elif "IndentationError" in stderr or "SyntaxError" in stderr:
                logger.error(f"Test script has syntax error: {stderr[:200]}")
                return True, None
            else:
                return True, None
                
        except subprocess.TimeoutExpired:
            logger.warning(f"Import test for '{import_name}' timed out")
            return True, None
        except Exception as e:
            logger.debug(f"Import test error: {e}")
            return True, None
