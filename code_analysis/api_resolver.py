"""
API Path Resolver for mapping component implementation FQNs to their public API FQNs.

This module is responsible for:
- Receiving chain candidates (via event) and aggregated module statistics (via setter).
- For a given chain candidate and all its potential export chains, it scores these chains based on module boundary characteristics and other heuristics to select the "best" chain.
- Resolving API paths for non-candidate components (e.g., locally defined items) by analyzing their direct export from their defining module and the API path of that module.
- Maintaining the final API map (implementation FQN -> public API FQN).
"""

import logging
import threading
from pathlib import Path
from enum import Enum, auto
from collections import deque, defaultdict
from typing import Dict, List, Set, Optional, Any, Union, Tuple, TypeVar, TYPE_CHECKING

from .config import AnalysisConfig
from .graph.models import ExportStep
from .feature_flags import Feature, is_enabled
from .utils import Cache
from code_analysis.relationship_types import REL_TYPE_EXPORTS
from .events import (
    API_PATH_RESOLVED,
    CHAIN_CANDIDATES_UPDATED,
    MODULE_ANALYSIS_INVALIDATED,
    API_MAP_UPDATED,
    DEPENDENCY_READY,
    EventPayload
)

if TYPE_CHECKING:
    from .mapcodocreg import MapCoDocRegistry
    from .definition_registry import DefinitionRegistry, DefinitionInfo
    from .graph.traversal import GraphTraversal
    from .graph.exporter import ExportTracker


logger = logging.getLogger(__name__)


T = TypeVar('T') # For generic type hints


class APIResolverAnalysisState(Enum):
    """Defines the operational state of the APIPathResolver."""
    NOT_INITIALIZED = auto()
    INITIALIZED = auto()     # Dependencies met, ready for data (stats, candidates)
    READY_TO_RESOLVE = auto() # Stats and candidates list received, can resolve on demand or batch
    BATCH_RESOLVING = auto()# Actively processing a batch of candidates (e.g. via AnalyzerIntegration)
    COMPLETED = auto()       # Initial batch resolution pass is complete (if applicable)


class APIPathResolver:
    """
    Resolves component implementation FQNs to their most likely public API FQNs.
    """
    COMPONENT_NAME = "api_resolver"
    DEPENDENCIES = {"definition_registry", "config_component", "analyzer_integration"} 

    _thread_local_data = threading.local() # For recursion depth tracking

    def __init__(self, 
                 config: Optional[AnalysisConfig] = None,
                 registry: Optional['MapCoDocRegistry'] = None,
                 definition_registry: Optional['DefinitionRegistry'] = None,
                 graph_traversal: Optional['GraphTraversal'] = None,
                 export_tracker: Optional['ExportTracker'] = None):
        
        self.registry = registry
        self.config = config or (self.registry.get('config') if self.registry else AnalysisConfig())
        
        # Dependencies injected or fetched from registry
        self.definition_registry: Optional['DefinitionRegistry'] = definition_registry
        self._graph_traversal: Optional['GraphTraversal'] = graph_traversal
        self._export_tracker: Optional['ExportTracker'] = export_tracker

        # Core data stores
        self.api_map: Dict[str, str] = {}  # Main cache: implementation_fqn -> resolved_api_fqn
        self.selected_export_chains: Dict[str, List[ExportStep]] = {} # Stores the best chain for resolved candidates
        
        # Data received from other components
        self.chain_candidates: Set[str] = set() # Populated by CHAIN_CANDIDATES_UPDATED event
        self.all_module_statistics: Dict[str, Dict[str, Any]] = {} # Populated by set_aggregated_module_statistics

        # Internal state
        self._initialized = False
        self._analysis_state = APIResolverAnalysisState.NOT_INITIALIZED
        self._required_dependencies = self.DEPENDENCIES.copy()
        self._dependencies_ready = set()
        self._errors: List[Dict[str, Any]] = []
        
        self.repo_path: Optional[Path] = None
        if self.registry and hasattr(self.registry, 'repo_path') and self.registry.repo_path:
            self.repo_path = Path(self.registry.repo_path).resolve()

        # General cache for results of resolve_api_path calls
        self.resolution_cache = Cache(Path(self.config.cache_dir or ".mapcodoc_cache") / "api_resolver_paths")
        
        self._max_resolve_recursion = 20 # Max depth for recursive calls to resolve_api_path
        logger.info(f"{self.COMPONENT_NAME} instance created.")


    def initialize(self) -> None:
        """
        Deferred initialization. Fetches dependencies from the registry if not already provided.
        Registers event handlers.
        """
        if self._initialized: return
        if not self.are_dependencies_ready():
            logger.info(f"{self.COMPONENT_NAME} waiting for dependencies: {sorted(list(self._required_dependencies - self._dependencies_ready))}")
            return
        
        logger.info(f"Initializing {self.COMPONENT_NAME}...")
        if not self.definition_registry and self.registry:
            self.definition_registry = self.registry.get_component("definition_registry")
        if not self._graph_traversal and self.registry:
            self._graph_traversal = self.registry.get_component("graph_traversal")
        if not self._export_tracker and self.registry:
            self._export_tracker = self.registry.get_component("export_tracker")

        # Critical dependency checks
        if not self.definition_registry: logger.error(f"{self.COMPONENT_NAME} init failed: DefinitionRegistry missing."); return
        if is_enabled(Feature.GRAPH_ANALYSIS) and not self._graph_traversal: logger.error(f"{self.COMPONENT_NAME} init failed: GraphTraversal missing."); return
        if not self._export_tracker: logger.error(f"{self.COMPONENT_NAME} init failed: ExportTracker missing."); return

        self._register_events()
        self._initialized = True
        self._analysis_state = APIResolverAnalysisState.INITIALIZED
        logger.info(f"{self.COMPONENT_NAME} initialized successfully and is ready for data.")


    def are_dependencies_ready(self) -> bool:
        """Checks if all required dependencies are met."""
        return self._required_dependencies.issubset(self._dependencies_ready)

    def on_dependency_ready(self, dependency_name: str) -> None:
        """Callback when a declared dependency becomes ready."""
        logger.debug(f"{self.COMPONENT_NAME} received dependency ready: {dependency_name}")
        self._dependencies_ready.add(dependency_name)
        if self.are_dependencies_ready() and not self._initialized:
            self.initialize()

    def _register_events(self) -> None:
        """Registers handlers for relevant events from the MapCoDocRegistry."""
        if not self.registry: 
            logger.error(f"{self.COMPONENT_NAME}: No registry, cannot subscribe to events.")
            return
        try:
            self.registry.subscribe_to_event(DEPENDENCY_READY, self._on_dependency_ready_event)
            self.registry.subscribe_to_event(CHAIN_CANDIDATES_UPDATED, self._on_chain_candidates_updated)
            self.registry.subscribe_to_event(MODULE_ANALYSIS_INVALIDATED, self._handle_module_analysis_invalidated)
            logger.info(f"{self.COMPONENT_NAME} event handlers registered.")
        except Exception as e:
            logger.error(f"Error subscribing {self.COMPONENT_NAME} to events: {e}", exc_info=True)


    def _on_dependency_ready_event(self, payload: EventPayload) -> None:
         dependency_name = payload.event_specific_data.get("dependency_name")
         if dependency_name in self._required_dependencies:
             self.on_dependency_ready(dependency_name)

    def _on_chain_candidates_updated(self, payload: EventPayload) -> None:
        """Handles updates to the list of chain candidates."""
        event_data = payload.event_specific_data
        candidates = event_data.get('candidates')
        source = event_data.get('source', 'event') # Get source from event if available
        if candidates is not None:
            self.set_chain_candidates(set(candidates), source=source)

    def set_chain_candidates(self, candidates: Set[str], source: str = "unknown") -> None:
        """
        Sets or updates the set of fully qualified names (FQNs) for components 
        that are candidates for API path resolution via export chain analysis.
        This method can be called directly or triggered by an event.
        """
        # Using union to accumulate candidates.
        self.chain_candidates.update(candidates) 
        logger.info(f"{self.COMPONENT_NAME} received/updated {len(candidates)} chain candidates from {source}. Total candidates: {len(self.chain_candidates)}")
        
        # Potentially update state if other conditions are met
        if self._analysis_state == APIResolverAnalysisState.INITIALIZED and self.all_module_statistics:
            self._analysis_state = APIResolverAnalysisState.READY_TO_RESOLVE
            logger.info(f"{self.COMPONENT_NAME} is now READY_TO_RESOLVE after receiving chain candidates.")
        elif self._analysis_state != APIResolverAnalysisState.INITIALIZED:
            logger.debug(f"{self.COMPONENT_NAME} received chain candidates but is not in INITIALIZED state (current: {self._analysis_state}). Statistics received: {bool(self.all_module_statistics)}")
    
    
    def build_export_chains(self) -> None:
        """
        Builds and scores export chains for all current chain candidates.
        This method is expected by the PathResolverInterface.
        
        Note: The actual chain building and selection logic might be primarily
        driven by calls to `resolve_api_path` for each candidate, which internally
        uses graph traversal and scoring. This method could act as a trigger
        or orchestrator if a bulk build process is needed.
        """
        logger.info(f"{self.COMPONENT_NAME}: build_export_chains called. Processing {len(self.chain_candidates)} candidates.")
        if not self.chain_candidates:
            logger.warning(f"{self.COMPONENT_NAME}: No chain candidates to build export chains for.")
            return

        if self._analysis_state != APIResolverAnalysisState.READY_TO_RESOLVE and self._analysis_state != APIResolverAnalysisState.COMPLETED : # Allow re-building
            logger.warning(f"{self.COMPONENT_NAME}: Not ready to build export chains. Current state: {self._analysis_state}")
            return
        
        logger.info(f"{self.COMPONENT_NAME}: build_export_chains - Placeholder implementation. Actual chain processing is typically on-demand via resolve_api_path.")
        # If a batch process is truly needed, this is where it would be orchestrated.
        # For instance, ensuring all known candidates have been processed by iterating 
        # `self.chain_candidates` and calling `self.resolve_api_path(candidate)` for any candidate not yet in `self.api_map`
    
    
    def set_aggregated_module_statistics(self, stats: Dict[str, Dict[str, Any]]) -> None:
        """
        Stores the aggregated module statistics map. Called by AnalyzerIntegration.
        """
        logger.info(f"{self.COMPONENT_NAME} received aggregated module statistics for {len(stats)} modules.")
        self.all_module_statistics = stats
        if self._analysis_state == APIResolverAnalysisState.INITIALIZED and self.chain_candidates: # Check if candidates also received
            self._analysis_state = APIResolverAnalysisState.READY_TO_RESOLVE


    def _determine_target_module_for_candidate(self, candidate_fqn: str, re_exporters: Set[str]) -> Optional[str]:
        """
        Heuristically determines the best "target" module for a bidirectional search.

        It scores each potential re-exporting module based on a set of heuristics, including package depth, whether it's an __init__.py file, its export behavior, and common "private" naming conventions. 
        The module with the highest score is chosen as the target.

        Args:
            candidate_fqn: The FQN of the chain candidate being analyzed.
            re_exporters: A set of module FQNs that re-export the candidate.

        Returns:
            The FQN of the best target module, or None if no suitable target is found.
        """
        if not re_exporters:
            logger.debug(f"No re-exporters provided for candidate '{candidate_fqn}', cannot determine target.")
            return None

        if len(re_exporters) == 1:
            return next(iter(re_exporters))

        best_target = None
        max_score = -float('inf')
        
        # Sort for deterministic tie-breaking
        sorted_re_exporters = sorted(list(re_exporters))

        for module_fqn in sorted_re_exporters:
            score = 0.0
            stats = self.all_module_statistics.get(module_fqn, {})

            # 1. Heavily reward shallow package depth
            depth = stats.get("module_depth", module_fqn.count('.'))
            
            # Special case: Package root (no dots in name)
            if '.' not in module_fqn:
                score += 500.0  # Overwhelmingly prefer the package root
            elif depth == 0:
                score += 200.0
            
            # if depth == 0:
            #     score += 100.0  # Top-level module, very strong signal
            # elif depth == 1:
            #     score += 50.0
            else:
                score -= depth * 10.0 # Penalize deeper modules

            # 2. Reward API-defining characteristics
            if stats.get("is_init_file", False):
                score += 40.0  # Very strong signal for __init__.py
            
            # if stats.get("has_explicit_all", False):
            #     score += 20.0

            # 3. Reward "facade" or "aggregator" behavior
            # reexport_ratio = stats.get("reexport_ratio", 0.0)
            # if reexport_ratio > 0.5:
            #     score += reexport_ratio * 15.0 # Max +15

            # 4. Heavily penalize "private" naming conventions
            private_indicators = {'_internal', 'impl', 'private', 'utils', 'helpers', 'compat', 'tests', 'examples'}
            module_parts = set(module_fqn.split('.'))
            if any(part.startswith('_') for part in module_parts) or private_indicators.intersection(module_parts):
                score -= 200.0 # Very strong penalty

            logger.debug(f"Target score for '{module_fqn}' (for candidate '{candidate_fqn}'): {score:.2f} (Depth: {depth})")

            if score > max_score:
                max_score = score
                best_target = module_fqn
        
        logger.info(f"Determined best target for '{candidate_fqn}' from {len(re_exporters)} choices -> '{best_target}' (Score: {max_score:.2f})")
        return best_target
    
    
    def _score_module_as_boundary(self, module_fqn: str) -> float:
        """
        Scores a module based on its likelihood of being an API boundary,
        using its pre-calculated statistics. Checks Feature.API_BOUNDARY_DETECTION.
        Higher score means more likely to be a significant boundary.
        """
        if not is_enabled(Feature.API_BOUNDARY_DETECTION):
            return 1.0 # Neutral score if feature is disabled

        module_stats = self.all_module_statistics.get(module_fqn)
        if not module_stats:
            logger.debug(f"No statistics for module {module_fqn} during boundary scoring. Neutral score.")
            return 1.0 

        score = 0.0
        # Apply heuristics (weights can be tuned)
        if module_stats.get("is_init_file", False): score += 5.0
        if module_stats.get("export_import_ratio", 0.0) > 1.5: score += min(module_stats.get("export_import_ratio", 0.0) / 2.0, 3.0) # Max +3
        if module_stats.get("external_import_ratio", 0.0) > 0.3: score += min(module_stats.get("external_import_ratio", 0.0) * 2.0, 2.0) # Max +2
        if module_stats.get("reexport_ratio", 0.0) > 0.2: score += min(module_stats.get("reexport_ratio", 0.0) * 3.0, 3.0) # Max +3
        if module_stats.get("has_explicit_all", False): score += 2.0
        if module_stats.get("has_docstring", False): score += 1.0
        
        module_depth = module_stats.get("module_depth", 0) # 0 for top-level, 1 for pkg.mod, etc.
        if module_depth <= 1: score += (2.0 - (module_depth * 0.5)) # Max +2 for top, +1.5 for depth 1
        elif module_depth > 3: score -= min((module_depth - 3) * 0.5, 1.5) # Max -1.5 penalty
        
        module_parts = module_fqn.split('.')
        if any(p.startswith('_') or p in ['impl', 'internal', 'private', 'utils', 'helpers', 'tests', 'examples', 'compat'] for p in module_parts):
            score -= 3.0 # Stronger penalty for private-like module names in path
            
        return max(0.1, score + 1.0) # Base of 1.0, ensure positive


    def _score_single_chain(self, chain: List[ExportStep], candidate_fqn: str) -> float:
        """Scores a single export chain based on its properties and module boundary scores."""
        if not chain: return 0.0
        
        score = 20.0 # Base score for any valid chain
        score -= len(chain) * 0.5 # Prefer shorter chains

        for i, step in enumerate(chain):
            step_module_boundary_score = self._score_module_as_boundary(step.module_in_chain_fqn)
            # Weight boundary score more if it's later in the chain (closer to public API)
            # and if the module itself has a good boundary score
            score += (step_module_boundary_score / 10.0) * ((i + 1) / len(chain)) * 2.0 

            if step.is_explicitly_exported_from_this_module: score += 1.0
            if step.availability_mechanism == "imported_via_wildcard": score -= 2.0
            elif "alias" in step.availability_mechanism: score -= 0.5
            if step.module_in_chain_fqn.endswith("__init__"): score += 0.5 # Slight preference for __init__ re-exports

        final_step_module_score = self._score_module_as_boundary(chain[-1].module_in_chain_fqn)
        if final_step_module_score > 7.0: # Higher threshold for final step strong boundary
            score += 5.0 
        if chain[-1].is_explicitly_exported_from_this_module: score += 1.0

        final_score = max(0.1, score)
        if is_enabled(Feature.ADVANCED_EXPORT_HEURISTICS):
            logger.debug(f"ADV_HEURISTICS applying for chain of {candidate_fqn}.")
            final_module_stats = self.all_module_statistics.get(chain[-1].module_in_chain_fqn, {})
            final_module_depth = final_module_stats.get("module_depth", 10)
            if final_module_depth == 0: final_score += 5.0 # Strong bonus for top-level exposure
            elif final_module_depth == 1: final_score += 2.5
            # Add more heuristics, e.g., penalize chains ending in `_internal` like modules
            if any(p.startswith('_') for p in chain[-1].module_in_chain_fqn.split('.')): final_score -= 3.0
        return final_score


    def derive_api_name_via_direct_lookup(self, candidates_to_re_exporters: Dict[str, Dict[str, Any]], analysis_results_map: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """
        Derives the API name for a chain candidate via a direct lookup in the analysis results.
        
        Args:
            candidate_to_re_exporters: A nested dictionary of chain candidates to their item kind and set of re-exporting modules
            analysis_results_map: The main map of {module_fqn: analysis_result}.
        
        Returns:
            A nested dictionary of chain candidates' FQN to their set of API names and export chains.
        """
        
        # if not self.definition_registry:
        #     logger.error("[FastPath] DefinitionRegistry is missing.")
        #     return {}
        
        API_names_and_chains: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "API_names": set(),
            "export_chain": [],
            "api_name_sources": {}  # Maps each API name to its exporting module
        })
        
        for target_component_fqn, ctx in candidates_to_re_exporters.items():
            re_exporters = ctx.get("exporters", set())
            component_kind = ctx.get("component_kind", None)
            is_internal = ctx.get("is_internal", None)
            
            # if component_kind == "member" and is_internal:
            #     definition = self.definition_registry.get_definition(target_component_fqn)
            #     if not definition:
            #         logger.warning(f"[FastPath] Could not find definition for '{target_component_fqn}'.")
            #         return {}
            #     definition_module_fqn = definition.module
            # else: definition_module_fqn = target_component_fqn
            # logger.debug(f"[FastPath] Definition module found: '{definition_module_fqn}'")
            
            end_module_fqn = self._determine_target_module_for_candidate(target_component_fqn, re_exporters)
            if not end_module_fqn:
                logger.warning(f"[FastPath] Could not determine a target module for '{target_component_fqn}'.")
                continue
            logger.debug(f"[FastPath] Determined target end module: '{target_component_fqn}' -> '{end_module_fqn}'")
            
            if not is_internal:
                API_names_and_chains[target_component_fqn]["API_names"].add(f"{end_module_fqn}.{target_component_fqn}")
                logger.debug(f"[FastPath] Added API name for external component: '{target_component_fqn}' -> '{end_module_fqn}.{target_component_fqn}'")
                continue
            
            end_module_result = analysis_results_map.get(end_module_fqn, {})
            if not end_module_result: logger.warning(f"[FastPath] No analysis result found for end module '{end_module_fqn}'."); continue
            
            name_in_end_module, end_package_fqn = None, None
            for exp_rec in end_module_result.get("export_records", []):
                if exp_rec.get("target_item_fqn") == target_component_fqn:
                    end_package_fqn = exp_rec.get("exporting_package_fqn")
                    name_in_end_module = exp_rec["exported_name"]
                    # initial_path = [end_module_fqn]
                    # initial_state = (end_module_fqn, name_in_end_module)
                    break
            
            if not name_in_end_module: 
                logger.warning(f"[FastPath] Could not find how '{target_component_fqn}' is exported from end module '{end_module_fqn}'.") 
                continue
            
            if component_kind == 'member':
                # Add API name for the BEST target (primary)
                api_name = f"{end_module_fqn}.{name_in_end_module}"
                API_names_and_chains[target_component_fqn]["API_names"].add(api_name)
                API_names_and_chains[target_component_fqn]["api_name_sources"][api_name] = end_module_fqn
                logger.debug(f"[FastPath] Added API name for member: '{target_component_fqn}' -> '{api_name}'")
                
                # Also add API names for ALL other re-exporters (candidates)
                for other_re_exporter in re_exporters:
                    if other_re_exporter == end_module_fqn:
                        continue  # Already added above
                    other_module_result = analysis_results_map.get(other_re_exporter, {})
                    if not other_module_result:
                        continue
                    for exp_rec in other_module_result.get("export_records", []):
                        if exp_rec.get("target_item_fqn") == target_component_fqn:
                            other_name = exp_rec.get("exported_name")
                            if other_name:
                                other_api_name = f"{other_re_exporter}.{other_name}"
                                API_names_and_chains[target_component_fqn]["API_names"].add(other_api_name)
                                API_names_and_chains[target_component_fqn]["api_name_sources"][other_api_name] = other_re_exporter
                                logger.debug(f"[FastPath] Added candidate API name for member: '{target_component_fqn}' -> '{other_api_name}'")
                            break
            
            elif component_kind == 'variable':
                # Add API name for the BEST target (primary)
                api_name = f"{end_module_fqn}.{name_in_end_module}"
                API_names_and_chains[target_component_fqn]["API_names"].add(api_name)
                API_names_and_chains[target_component_fqn]["api_name_sources"][api_name] = end_module_fqn
                logger.debug(f"[FastPath] Added API name for variable: '{target_component_fqn}' -> '{api_name}'")
                
                # Also add API names for ALL other re-exporters (candidates)
                for other_re_exporter in re_exporters:
                    if other_re_exporter == end_module_fqn:
                        continue  # Already added above
                    other_module_result = analysis_results_map.get(other_re_exporter, {})
                    if not other_module_result:
                        continue
                    for exp_rec in other_module_result.get("export_records", []):
                        if exp_rec.get("target_item_fqn") == target_component_fqn:
                            other_name = exp_rec.get("exported_name")
                            if other_name:
                                other_api_name = f"{other_re_exporter}.{other_name}"
                                API_names_and_chains[target_component_fqn]["API_names"].add(other_api_name)
                                API_names_and_chains[target_component_fqn]["api_name_sources"][other_api_name] = other_re_exporter
                                logger.debug(f"[FastPath] Added candidate API name for variable: '{target_component_fqn}' -> '{other_api_name}'")
                            break
            
            elif component_kind == 'module':    
                candidate_module_result = analysis_results_map.get(target_component_fqn, {})
                if not candidate_module_result: logger.warning(f"[FastPath] No analysis result found for module '{target_component_fqn}'."); continue
                api_prefix = f"{end_module_fqn}.{name_in_end_module}"
                for exp_rec in candidate_module_result.get("export_records", []):
                    sub_target_item_fqn = exp_rec.get("target_item_fqn")
                    name_in_sub_end_module = exp_rec.get("exported_name")
                    # sub_initial_sub_path = [end_module_fqn]
                    # initial_sub_state = (end_module_fqn, name_in_end_module)
                    if not sub_target_item_fqn or not name_in_sub_end_module: continue
                    API_names_and_chains[sub_target_item_fqn]["API_names"].add(f"{api_prefix}.{name_in_sub_end_module}")
                    API_names_and_chains[sub_target_item_fqn]["api_name_sources"][f"{api_prefix}.{name_in_sub_end_module}"] = end_module_fqn
                    logger.debug(f"[FastPath] Added API name for module: '{target_component_fqn}' -> '{api_prefix}.{name_in_sub_end_module}'")
                    
                    
            elif component_kind == 'package':
                # Only subpackages under the end_package_fqn will be processed
                sub_pkg_name = target_component_fqn.rsplit('.', 1)[-1]
                if end_package_fqn and target_component_fqn.startswith(end_package_fqn):
                    remainder_name = target_component_fqn[len(end_package_fqn):]
                    if remainder_name.startswith('.') and remainder_name == f'.{sub_pkg_name}':
                        
                        pkg_result = analysis_results_map.get(target_component_fqn, {})
                        # If analysis result for the package is available, check if it's a regular package as the FQN of the package and its __init__.py file is the same. 
                        is_regular_package = bool(pkg_result.get("module_interface", {}).get("is_init_file", False)) if pkg_result else False # confirm that the package is a regular package by checking if the analysis result is for its __init__.py file
                        if is_regular_package: logger.debug(f"[FastPath] Package '{target_component_fqn}' is a regular package.")
                        else: logger.debug(f"[FastPath] Package '{target_component_fqn}' is a namespace package.")
                        
                        api_prefix = f"{end_module_fqn}.{name_in_end_module}"
                        visited_package_submodules = set()
                        if is_regular_package:
                            for exp in pkg_result.get("export_records", []):
                                exported_submodule_name = exp.get("exported_name")
                                submodule_target_item_fqn = exp.get("target_item_fqn")
                                if not exported_submodule_name or not submodule_target_item_fqn: continue
                                API_names_and_chains[submodule_target_item_fqn]["API_names"].add(f"{api_prefix}.{exported_submodule_name}")
                                API_names_and_chains[submodule_target_item_fqn]["api_name_sources"][f"{api_prefix}.{exported_submodule_name}"] = api_prefix
                                logger.debug(f"[FastPath] Added API name for package submodule: '{target_component_fqn}' -> '{api_prefix}.{exported_submodule_name}'")
                                # The API name derived for items exported from the __init__.py file of the package should take prescedence over the one derived from their defining modules.
                                visited_package_submodules.add(submodule_target_item_fqn)
                                
                        for submodule_fqn, submodule_result in analysis_results_map.items():
                            # Flag to check if the module is a submodule of the package
                            # Analysis result is structured based on file hierarchy, so once the flag switches from True to False, the rest of the analysis result is not the submodule of the package.
                            submodule_scope = False
                            if submodule_fqn.startswith(target_component_fqn): # locate submodules under the package
                                submodule_scope = True
                                if not submodule_result: continue
                                is_regular_submodule = bool(submodule_result.get("module_interface", {}).get("is_init_file"))
                                for exp in submodule_result.get("export_records", []):
                                    exported_submodule_name = exp.get("exported_name")
                                    submodule_target_item_fqn = exp.get("target_item_fqn")
                                    if not exported_submodule_name or not submodule_target_item_fqn or submodule_target_item_fqn in visited_package_submodules: continue
                                    
                                    # Validate that the target actually belongs to this package
                                    if not submodule_target_item_fqn.startswith(target_component_fqn + '.'):
                                        continue  # Skip items from other packages
                                    
                                    api_suffix = f"{submodule_target_item_fqn[len(target_component_fqn):].rsplit('.',1)[0]}.{exported_submodule_name}"
                                    API_names_and_chains[submodule_target_item_fqn]["API_names"].add(f"{api_prefix}.{api_suffix}")
                                    API_names_and_chains[submodule_target_item_fqn]["api_name_sources"][f"{api_prefix}.{api_suffix}"] = f"{api_prefix}.{api_suffix.rsplit('.', 1)[0]}"
                                    logger.debug(f"[FastPath] Added API name for package submodule: '{target_component_fqn}' -> '{api_prefix}.{api_suffix}'")
                                    if is_regular_submodule: visited_package_submodules.add(submodule_target_item_fqn)
                        
                            elif not submodule_fqn.startswith(target_component_fqn) and submodule_scope: break  

            if component_kind not in ['module', 'package'] and name_in_end_module:
                # --- Trace the export chain for the candidate ---
                logger.debug(f"Attempting Tier 1 (fast path) export chain tracing for {target_component_fqn}")
                try:
                    # Note: resolve_chains_via_direct_lookup re-determines the target module internally. 
                    # This is acceptable for now to keep the method self-contained.
                    resolved_chains = self.resolve_chains_via_direct_lookup(target_component_fqn, end_module_fqn, analysis_results_map)
                    
                    if resolved_chains and API_names_and_chains[target_component_fqn]["API_names"]:
                        # Pick the chain that corresponds to the shortest API name
                        # Each chain's last step contains the public API module FQN
                        api_names = API_names_and_chains[target_component_fqn]["API_names"]
                        
                        # Find the shortest API name
                        shortest_api_name = min(api_names, key=len)
                        
                        # Extract the module part from the shortest API name
                        # API name format: "module.fqn.member_name"
                        shortest_api_module = shortest_api_name.rsplit('.', 1)[0]
                        
                        # Find the chain that ends at this module
                        best_chain = None
                        for chain in resolved_chains:
                            if chain and chain[-1].module_in_chain_fqn == shortest_api_module:
                                best_chain = chain
                                break
                        
                        # Fallback: if no exact match, use the shortest chain
                        if not best_chain:
                            logger.warning(f"No chain found ending at '{shortest_api_module}' for {target_component_fqn}. Using shortest chain as fallback.")
                            best_chain = min(resolved_chains, key=len)
                        
                        if best_chain:
                            API_names_and_chains[target_component_fqn]["export_chain"] = [step.__dict__ for step in best_chain]
                            
                except Exception as e:
                    logger.error(f"Error during Tier 1 resolution for {target_component_fqn}: {e}", exc_info=True)
            
        return API_names_and_chains


    def resolve_chains_via_direct_lookup(self, target_component_fqn: str, end_module_fqn: str, analysis_results_map: Dict[str, Dict[str, Any]]) -> List[List[ExportStep]]:
        """
        Finds all export chains using a guided backward trace on the virtual graph of re-exporting modules, derived directly from analysis results.
        This is the primary, graph-less "Fast Path" for resolution.
        
        Args:
            target_component_fqn: The FQN of the component to trace.
            end_module_fqn: The target public API module to start the backward search from.
            analysis_results_map: The main map of {module_fqn: analysis_result}.

        Returns:
            A list of all found chains, where each chain is a list of ExportStep objects.
        """
        
        final_chains: List[List[ExportStep]] = []

        if not self.definition_registry:
            logger.error("[FastPath] DefinitionRegistry is missing.")
            return []

        definition = self.definition_registry.get_definition(target_component_fqn)
        if not definition:
            logger.warning(f"[FastPath] Could not find definition for '{target_component_fqn}'.")
            return []
        definition_module_fqn = definition.module
        logger.debug(f"[FastPath] Definition module found: '{definition_module_fqn}'")

        # end_module_fqn = self._determine_target_module_for_candidate(target_component_fqn, all_re_exporters)
        if not end_module_fqn:
            logger.warning(f"[FastPath] Could not determine a target module for '{target_component_fqn}'.")
            return []
        # logger.debug(f"[FastPath] Determined target end module: '{end_module_fqn}'")
        
        # --- Perform a backward BFS tracing the name ---
        end_module_result = analysis_results_map.get(end_module_fqn, {})
        if not end_module_result:
            logger.warning(f"[FastPath] No analysis result found for end module '{end_module_fqn}'.")
            return []
        logger.debug(f"[FastPath] Seeding search from '{end_module_fqn}'. Inspecting its {len(end_module_result.get('export_records', []))} export records...")
        
        # Queue state: (current_module_fqn, name_in_current_module, path_of_modules_so_far)
        queue = deque()
        visited_states = set()
        
        # Seed the queue: find how the target component is named in the end module
        for exp_rec in end_module_result.get("export_records", []):
            if exp_rec.get("target_item_fqn") == target_component_fqn:
                name_in_end_module = exp_rec["exported_name"]
                initial_path = [end_module_fqn]
                initial_state = (end_module_fqn, name_in_end_module)
                if initial_state not in visited_states:
                    queue.append((end_module_fqn, name_in_end_module, initial_path))
                    visited_states.add(initial_state)
                    logger.debug(f"[FastPath|Seed]  - Checking export: '{exp_rec.get('exported_name')}' -> Target: '{exp_rec.get('target_item_fqn')}' ... MATCH! Enqueueing state: {initial_state}")
                    break
        
        logger.debug(f"[FastPath] Seeded queue size: {len(queue)}")

        completed_module_paths: List[List[str]] = []

        first_queue = True
        while queue:
            current_module, name_to_trace, path = queue.popleft()

            if current_module == definition_module_fqn:
                # We reached the end. The name must be defined locally here
                # The path is backward, so reverse it for correct order
                completed_module_paths.append(list(reversed(path)))
                continue

            # Find the import record in `current_module` that provided `name_to_trace`
            if first_queue: current_module_result = end_module_result; first_queue = False
            else: current_module_result = analysis_results_map.get(current_module, {})
            found_step = False
            
            # Get the raw imported name and source module of the item to trace
            for exp_rec in current_module_result.get("export_records", []):
                if exp_rec.get("exported_name") == name_to_trace:
                    source_module = exp_rec.get("source_module")
                    
                    # If source_module is None, skip this export record or try to infer it
                    if not source_module:
                        logger.warning(f"[FastPath] Export record for '{name_to_trace}' in '{current_module}' has no source_module. Skipping.")
                        continue
                    
                    original_name = None # Initialize
                    
                    for imp_rec in current_module_result.get("import_records", []):
                        # Check for explicit import: Source matches AND bound name matches what we are exporting
                        if imp_rec.get("source_module_fqn") == source_module and not imp_rec.get("is_wildcard"):
                            if imp_rec.get("name_bound_in_importer") == name_to_trace:
                                original_name = imp_rec["raw_imported_name"]
                                break
                        
                        # Check for wildcard import: Source matches AND is wildcard
                        elif imp_rec.get("source_module_fqn") == source_module and imp_rec.get("is_wildcard"):
                            original_name = name_to_trace # Wildcard preserves name
                            break
                    
                    if original_name:
                        new_state = (source_module, original_name)
                        if new_state not in visited_states and source_module not in path:
                            new_path = path + [source_module]
                            queue.append((source_module, original_name, new_path))
                            visited_states.add(new_state)
                        found_step = True
                    break
            
            if found_step: continue
        
        if not completed_module_paths:
            logger.info(f"[FastPath] Found 0 paths for '{target_component_fqn}' via direct lookup.")
            return []

        # Reconstruct the detailed ExportStep chain for each successful module path.
        for module_path in completed_module_paths:
            reconstructed_chain = self._reconstruct_chain_from_module_path(module_path, target_component_fqn, analysis_results_map)
            if reconstructed_chain:
                final_chains.append(reconstructed_chain)
        
        logger.info(f"[FastPath] Found {len(final_chains)} distinct chains for '{target_component_fqn}'.")
        return final_chains

    
    def _reconstruct_chain_from_module_path(self, module_path: List[str], target_component_fqn: str, analysis_results_map: Dict[str, Dict[str, Any]]) -> Optional[List[ExportStep]]:
        """
        Takes a path of module FQNs (from defining module to public API) and builds the detailed List[ExportStep] chain by inspecting the analysis results.
        
        Args:
            module_path: A list of module FQNs from the defining module to the public API.
            target_component_fqn: The FQN of the component to trace.
            analysis_results_map: The main map of {module_fqn: analysis_result}.

        Returns:
            A list of ExportStep objects representing the chain, or None if reconstruction fails.
        """
        reconstructed_chain: List[ExportStep] = []
        name_to_trace = target_component_fqn.split('.')[-1]

        for i, module_fqn in enumerate(module_path):
            is_defining_module = (i == 0)
            
            if is_defining_module:
                availability = "defined_locally"
                name_in_scope = name_to_trace
            else:
                # Find the import record in the current module that brought the item from the previous module.
                previous_module_fqn = module_path[i-1]
                importer_module_result = analysis_results_map.get(module_fqn, {})
                
                found_import_record = None
                for imp_rec in importer_module_result.get("import_records", []):
                    # Look for an import in `module_fqn` that comes `from previous_module_fqn` and imports the `name_to_trace`
                    # Check explicit import: Source matches AND bound name matches the name we are looking for in this scope
                    if imp_rec.get("source_module_fqn") == previous_module_fqn and not imp_rec.get("is_wildcard"):
                        if imp_rec.get("name_bound_in_importer") == name_to_trace:
                            found_import_record = imp_rec
                            break
                            
                    # Check wildcard import
                    elif imp_rec.get("source_module_fqn") == previous_module_fqn and imp_rec.get("is_wildcard"):
                        found_import_record = imp_rec
                        break
                
                if not found_import_record:
                    logger.warning(f"Reconstruction failed: Could not find import of '{name_to_trace}' from '{previous_module_fqn}' in '{module_fqn}'.")
                    return None
                
                name_in_scope = found_import_record.get("name_bound_in_importer")
                # If wildcard, name_bound might be None or '*', need to use name_to_trace
                if found_import_record.get("is_wildcard"):
                    name_in_scope = name_to_trace
                    availability = "imported_via_wildcard"
                else:
                    name_in_scope = found_import_record.get("name_bound_in_importer")
                    availability = "imported_directly_with_alias" if found_import_record.get("raw_alias") else "imported_directly"

            # Determine the export status for the item in the current module
            is_explicit = False
            module_result = analysis_results_map.get(module_fqn, {})
            for exp_rec in module_result.get("export_records", []):
                if exp_rec.get("exported_name") == name_in_scope:
                    is_explicit = exp_rec.get("is_explicit", False)
                    break
            
            step = ExportStep(
                module_in_chain_fqn=module_fqn,
                name_in_module_scope=name_in_scope,
                target_item_fqn=target_component_fqn,
                availability_mechanism=availability,
                is_explicitly_exported_from_this_module=is_explicit
            )
            reconstructed_chain.append(step)
            
            # The name for the next hop is the name it has in the current module's scope.
            name_to_trace = name_in_scope

        return reconstructed_chain

    
    def determine_best_api_path_for_candidate(self, candidate_fqn: str, all_chains_for_this_candidate: List[List[ExportStep]]) -> Tuple[Optional[str], Optional[List[ExportStep]], List[List[ExportStep]]]:
        """
        Determines the best API path for a candidate from its chains, updates internal maps, and returns the resolved path, the best chain, and all chains.
        """
        if not self._initialized: # Basic check
            logger.error(f"{self.COMPONENT_NAME} not initialized for {candidate_fqn}.")
            return candidate_fqn, None, all_chains_for_this_candidate # Fallback, pass all chains back
        if not self.all_module_statistics:
             logger.warning(f"Aggregated module statistics not set. Boundary scoring for {candidate_fqn} will be default/limited.")

        if not all_chains_for_this_candidate:
            logger.warning(f"No export chains for candidate: {candidate_fqn}. API path set to impl path.")
            self.api_map[candidate_fqn] = candidate_fqn 
            self.selected_export_chains[candidate_fqn] = []
            return candidate_fqn, [], [] # Return empty list for best_chain too

        scored_chains = sorted([(self._score_single_chain(chain, candidate_fqn), chain) for chain in all_chains_for_this_candidate], key=lambda x: x[0], reverse=True)
        
        if not scored_chains: # Should not happen if input chains was not empty
            self.api_map[candidate_fqn] = candidate_fqn
            self.selected_export_chains[candidate_fqn] = []
            return candidate_fqn, [], all_chains_for_this_candidate

        best_score, best_chain_obj = scored_chains[0]
        if not best_chain_obj: # Should be caught by `if not scored_chains`
            self.api_map[candidate_fqn] = candidate_fqn
            self.selected_export_chains[candidate_fqn] = []
            return candidate_fqn, [], all_chains_for_this_candidate
            
        final_step = best_chain_obj[-1]
        resolved_api_path = f"{final_step.module_in_chain_fqn}.{final_step.name_in_module_scope}"

        # --- logging for debugging ---
        if logger.isEnabledFor(logging.DEBUG):
            chain_str = " -> ".join([f"{step.module_in_chain_fqn}.{step.name_in_module_scope}" for step in best_chain_obj])
            logger.debug(
                f"API Path for '{candidate_fqn}' resolved to '{resolved_api_path}' with score {best_score:.2f}.\n"
                f"  Winning Chain: {chain_str}"
            )
        # --- 
        
        if self.api_map.get(candidate_fqn) != resolved_api_path:
            self.api_map[candidate_fqn] = resolved_api_path
            self.selected_export_chains[candidate_fqn] = best_chain_obj 
            logger.info(f"API Path for '{candidate_fqn}' resolved to '{resolved_api_path}' (Score: {best_score:.2f})")
            if self.registry:
                self.registry.publish_event(API_PATH_RESOLVED, {
                    "input_path": candidate_fqn, "resolved_path": resolved_api_path,
                    "resolution_source": "export_chain_selection", "is_correction": resolved_api_path != candidate_fqn,
                    "chain_score": best_score, 
                    "best_chain_details": [step.__dict__ for step in best_chain_obj]
                }, self.COMPONENT_NAME)
                # API_MAP_UPDATED can be published in batch by AnalyzerIntegration
        
        return resolved_api_path, best_chain_obj, all_chains_for_this_candidate


    def resolve_api_path(self, component_path: str, collect_all: bool = False) -> Union[str, List[str]]:
        """
        Main method to resolve an implementation FQN to its public API FQN(s).
        
        Args:
            component_path: The implementation FQN to resolve.
            collect_all: If True, return all possible API paths for the component.
        
        Returns:
            A single API path if not collect_all, otherwise a list of all possible API paths.
        """
        
        if not self._initialized or not self.definition_registry or not self._graph_traversal or not self._export_tracker:
            logger.warning(f"{self.COMPONENT_NAME} not ready/missing deps. Returning original path for {component_path}")
            return [component_path] if collect_all else component_path
        
        # Thread-local recursion depth management
        recursion_depth_attr = f'_resolve_depth_{threading.get_ident()}' # Ensure thread safety for key
        current_depth = getattr(APIPathResolver._thread_local_data, recursion_depth_attr, 0)
        if current_depth >= self._max_resolve_recursion:
            logger.warning(f"Max recursion depth for {component_path}. Thread: {threading.get_ident()}")
            return [component_path] if collect_all else component_path
        setattr(APIPathResolver._thread_local_data, recursion_depth_attr, current_depth + 1)

        try:
            # 1. Check primary API Map (for single best path, which is the common case)
            if not collect_all and component_path in self.api_map:
                return self.api_map[component_path]
            
            # 2. Check general resolution cache if not in api_map or if collect_all is true
            cache_type = 'api_path_all' if collect_all else 'api_path_single'
            cached_val = self.resolution_cache.get(cache_type, component_path)
            if cached_val is not None:
                # If not collect_all, ensure api_map is also populated from this cache hit for future direct lookups.
                if not collect_all and component_path not in self.api_map and isinstance(cached_val, str):
                    self.api_map[component_path] = cached_val
                return cached_val

            resolved_api_path: Optional[str] = None # For the single best path
            all_possible_api_paths: List[str] = [] # For collect_all

            # 3. Determine if it's a known chain candidate (populated by event)
            is_candidate = component_path in self.chain_candidates

            if is_candidate:
                logger.debug(f"{self.COMPONENT_NAME}: {component_path} is a chain candidate. Finding/selecting best chain.")
                # This candidate should ideally have been processed by AnalyzerIntegration's loop which calls determine_best_api_path_for_candidate
                # If it's not in self.api_map, it means an on-demand resolution is occurring.
                if component_path not in self.api_map:
                    all_chains = self._graph_traversal.find_export_chains(component_path)
                    if all_chains:
                        # This call updates self.api_map and self.selected_export_chains
                        self.determine_best_api_path_for_candidate(component_path, all_chains)
                    else: # No chains found, even for a candidate
                        logger.warning(f"No export chains found for CANDIDATE {component_path}. Defaulting to implementation path.")
                        self.api_map[component_path] = component_path 
                        self.selected_export_chains[component_path] = []
                
                resolved_api_path = self.api_map.get(component_path) # Should now be populated

                if collect_all:
                    # Fetch all chains again (or retrieve if APIPathResolver were to store them all) and score them for ranking.
                    all_chains_for_candidate = self._graph_traversal.find_export_chains(component_path)
                    if all_chains_for_candidate:
                        scored_paths = sorted(
                            [(self._score_single_chain(ch, component_path), ch) for ch in all_chains_for_candidate],
                            key=lambda x: x[0], reverse=True
                        )
                        all_possible_api_paths = [f"{sc_ch[-1].module_in_chain_fqn}.{sc_ch[-1].name_in_module_scope}" for _, sc_ch in scored_paths if sc_ch]
                    elif resolved_api_path: # If no chains but a path was somehow resolved (e.g. to itself)
                        all_possible_api_paths = [resolved_api_path]

            # 4. Not a chain candidate (or treated as such if chain resolution failed)
            if not is_candidate or resolved_api_path is None: # If it was a candidate but no chains led to resolution
                logger.debug(f"{self.COMPONENT_NAME}: {component_path} not resolved as candidate. Applying local/module definition logic.")
                definition = self.definition_registry.get_definition(component_path)
                if not definition:
                    resolved_api_path = component_path # Fallback
                else:
                    defining_module_fqn = definition.module
                    simple_name = definition.name

                    if not defining_module_fqn or component_path == defining_module_fqn: # Is a module or similar top-level
                        resolved_api_path = component_path
                    else:
                        defining_module_api_path = self.resolve_api_path(defining_module_fqn) # Recursive
                        
                        if defining_module_api_path == defining_module_fqn: # Defining module is accessed directly
                            exports = self._export_tracker.find_exports_from_module(module_fqn=defining_module_fqn, is_reexport=False)
                            is_directly_exported = any(
                                ex.get("properties",{}).get("exported_name") == simple_name and \
                                ex.get("target") == component_path # Ensure the export target is the component itself
                                for ex in exports
                            )
                            # If directly exported, its API path is its FQN. Otherwise, it's internal relative to this module.
                            resolved_api_path = component_path # Public if exported, "internal" if not (still its FQN)
                            logger.debug(f"Non-candidate {component_path} (in direct module {defining_module_fqn}) directly exported: {is_directly_exported}. API path: {resolved_api_path}")
                        else: # Defining module itself is re-exported/aliased
                            resolved_api_path = f"{defining_module_api_path}.{simple_name}"
                            logger.debug(f"Non-candidate {component_path} API path via module alias: {resolved_api_path}")
            
            
            final_resolved_single_path = self.api_map.get(component_path)
            if final_resolved_single_path is None: # Should have been set by now
                final_resolved_single_path = component_path # Fallback

            if collect_all and is_candidate:
                all_possible_api_paths_for_candidate = set()
                if final_resolved_single_path: # Add the primary one
                    all_possible_api_paths_for_candidate.add(final_resolved_single_path)

                best_chain_obj = self.selected_export_chains.get(component_path)
                if best_chain_obj and self._export_tracker:
                    final_module_fqn = best_chain_obj[-1].module_in_chain_fqn
                    
                    # Find all ways 'component_path' is exported from 'final_module_fqn'
                    exports_of_target_from_final_module = self._export_tracker.find_relationships(
                        source=final_module_fqn,
                        target=component_path, # The original component's FQN
                        relationship_type=REL_TYPE_EXPORTS # from relationship_types.py
                    )
                    for ex_rel in exports_of_target_from_final_module:
                        exported_as_name = ex_rel.get("properties", {}).get("exported_name")
                        if exported_as_name:
                            all_possible_api_paths_for_candidate.add(f"{final_module_fqn}.{exported_as_name}")
                
                result_to_return = sorted(list(all_possible_api_paths_for_candidate)) if all_possible_api_paths_for_candidate else [final_resolved_single_path or component_path]
                self.resolution_cache.set(cache_type, component_path, result_to_return)
                return result_to_return
            
            elif collect_all: # For non-candidates
                result_to_return = [final_resolved_single_path]
            else: # Not collect_all
                result_to_return = final_resolved_single_path
            
            self.resolution_cache.set(cache_type, component_path, result_to_return)
            return result_to_return
        finally:
            current_depth_after = getattr(APIPathResolver._thread_local_data, recursion_depth_attr, 1) -1 
            setattr(APIPathResolver._thread_local_data, recursion_depth_attr, current_depth_after)


    def _handle_module_analysis_invalidated(self, payload: EventPayload):
        module_path = payload.event_specific_data.get("module_path")
        if not module_path: return
        logger.info(f"{self.COMPONENT_NAME} invalidating data related to module: {module_path}")
        
        # Invalidate api_map: entries defined IN the module or whose resolved path IS in the module
        keys_to_remove_map = {fqn for fqn, resolved_path in self.api_map.items() 
                              if fqn.startswith(module_path + '.') or resolved_path.startswith(module_path + '.')}
        for key in keys_to_remove_map: del self.api_map[key]
        
        # Invalidate selected_export_chains
        keys_to_remove_chains = {fqn for fqn in self.selected_export_chains if fqn.startswith(module_path + '.')}
        for fqn, chain_steps in list(self.selected_export_chains.items()): # Iterate copy
            if any(step.module_in_chain_fqn == module_path for step in chain_steps):
                keys_to_remove_chains.add(fqn)
        for key in keys_to_remove_chains:
             if key in self.selected_export_chains: del self.selected_export_chains[key]

        # Invalidate general cache entries that might reference this module
        self.resolution_cache.invalidate(f":{module_path}") # Example broad invalidation
        
        if (keys_to_remove_map or keys_to_remove_chains) and self.registry:
            self.registry.publish_event(API_MAP_UPDATED, {"invalidated_module": module_path, "reason": "module_data_invalidated"}, self.COMPONENT_NAME)
        
        # This component might need to re-resolve if its dependencies changed, but primarily AnalyzerIntegration will drive re-analysis.
        # Setting state to INITIALIZED might be too aggressive if only a small part is affected.
        # For now, we rely on new calls to resolve_api_path to repopulate.
        logger.debug(f"APIPathResolver: Cache and map entries related to {module_path} cleared.")


    def get_state(self) -> Dict[str, Any]:
        return {
            'api_map_size': len(self.api_map),
            'chain_candidates_count': len(self.chain_candidates), 
            'resolved_export_chains_count': len(self.selected_export_chains),
            'analysis_state': self._analysis_state.name,
            'all_module_statistics_count': len(self.all_module_statistics),
        }


    def sync_state(self, state: Dict[str, Any]) -> bool: 
        if 'api_map' in state: self.api_map = state['api_map']
        if 'selected_export_chains' in state: self.selected_export_chains = state['selected_export_chains']
        if 'chain_candidates' in state: self.chain_candidates = set(state['chain_candidates'])
        if 'all_module_statistics' in state: self.all_module_statistics = state['all_module_statistics']
        try: self._analysis_state = APIResolverAnalysisState[state.get('analysis_state', 'INITIALIZED')]
        except KeyError: self._analysis_state = APIResolverAnalysisState.INITIALIZED
        logger.info(f"{self.COMPONENT_NAME} state synced.")
        return True
    
    def cleanup(self):
        self.api_map.clear()
        self.selected_export_chains.clear()
        self.chain_candidates.clear()
        self.all_module_statistics.clear()
        self.resolution_cache.clear()
        self._analysis_state = APIResolverAnalysisState.NOT_INITIALIZED
        logger.info(f"{self.COMPONENT_NAME} cleanup complete.")
