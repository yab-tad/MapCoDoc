"""
MapCoDoc: Main Command Line Interface

Provides commands to analyze code repositories, manage features, and (planned) query/visualize relationships.
"""

import os
import re
import sys
import json
import time
import argparse
import logging
import traceback
import networkx as nx
from pathlib import Path
from typing import List, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor # For parallel repo analysis
from dotenv import load_dotenv

# Attempt to import rich, but make it optional
try:
    from rich.console import Console
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
    from rich import box
    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False
    # Define a dummy console if rich is not available
    class DummyConsole:
        def print(self, *args, **kwargs):
            built_in_print_args = []
            for arg in args:
                if isinstance(arg, str) and arg.startswith("[") and "]" in arg:
                    # Basic stripping of rich tags for non-rich print
                    tag_end = arg.find("]")
                    if tag_end != -1:
                        processed_arg = arg[tag_end+1:]
                        # Strip closing tags like [/red]
                        processed_arg = re.sub(r"\[/[a-zA-Z]+\]", "", processed_arg)
                        built_in_print_args.append(processed_arg)
                    else:
                        built_in_print_args.append(arg)
                else:
                    built_in_print_args.append(arg)
            print(*built_in_print_args)

    console = DummyConsole()
    Table = None # Placeholder
    Progress = None # Placeholder

import yaml # For config file loading

# --- MapCoDoc Core Imports ---
from mapcodoc_db.db_manager import MapCoDocDB
from code_analysis.config import AnalysisConfig, AnalysisMode
from code_analysis.mapcodocreg import MapCoDocRegistry
from code_analysis.repo_manager import RepoManager
from code_analysis.analyzers.analyzer_integration import AnalyzerIntegration
from code_analysis.watcher import FileSystemWatcher
from code_analysis.feature_flags import Feature, enable, disable, list_flags, get_all_feature_states, is_enabled
from code_analysis.utils import configure_logging, AnalysisError, format_error # Assuming Timer is not used directly in CLI main now
from doc_processor.doc_runner import DocProcessingRunner

# --- Other imports from the old relationship/visualization handlers (if kept) ---
from code_analysis.graph.store import GraphStore
# from code_analysis.graph.traversal import GraphTraversal
# from code_analysis.graph.relationships import RelationshipTracker


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mapcodoc.cli")

load_dotenv()


def display_feature_flags():
    """Displays current feature flags state in a table if rich is available."""
    features = get_all_feature_states() # Uses is_enabled() for current state
    if RICH_AVAILABLE and Table:
        table = Table(title="MapCoDoc Feature Flags", box=box.ROUNDED, show_lines=True)
        table.add_column("Feature Flag", style="cyan", no_wrap=True)
        table.add_column("Status", justify="center")
        table.add_column("Description", style="dim")

        # Get descriptions from Feature enum docstrings if possible
        descriptions = {f.name: (f.__doc__.splitlines()[0] if f.__doc__ else "No description.") for f in Feature}
        
        for feature_name, status in features.items():
            table.add_row(
                feature_name,
                "[green]Enabled" if status else "[red]Disabled",
                descriptions.get(feature_name, "")
            )
        console.print(table)
        console.print("\nEnable features via --enable-<feature_name> flags, environment variables, or config file.")
    else:
        console.print("Feature Flags:")
        for feature_name, status in features.items():
            console.print(f"- {feature_name}: {'Enabled' if status else 'Disabled'}")


def save_feature_flags(output_path_str: str):
    """Saves the current state of all feature flags to a JSON file."""
    features = get_all_feature_states()
    config_to_save = {
        "feature_flags_current_state": features,
        "description": "MapCoDoc feature flags configuration snapshot.",
        "notes": "This file shows the runtime state of flags. To control them, use CLI args, env vars, or an AnalysisConfig file."
    }
    output_path = Path(output_path_str)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(config_to_save, f, indent=2)
        console.print(f"Current feature flag states saved to [cyan]{output_path}[/]")
    except Exception as e:
        console.print(f"[red]Error saving feature flags: {e}[/]")


def load_config(args: argparse.Namespace) -> AnalysisConfig:
    """
    Loads and merges configuration.
    Priority:
    1. CLI Feature Flag Toggles (e.g., --enable-watch-mode) set global flag states.
    2. AnalysisConfig loaded from --config file (initializes based on current global flag states for relevant fields, then applies file values).
    3. AnalysisConfig default instantiation (initializes based on current global flag states).
    4. Direct CLI argument overrides for specific AnalysisConfig fields (e.g., --log-level).
    """
    # 1. Apply CLI feature flag toggles FIRST
    if hasattr(args, 'enable_api_boundaries') and args.enable_api_boundaries: enable(Feature.API_BOUNDARY_DETECTION)
    if hasattr(args, 'enable_chain_candidates') and args.enable_chain_candidates: enable(Feature.CHAIN_CANDIDATE_COLLECTION)
    if hasattr(args, 'enable_dynamic_all') and args.enable_dynamic_all: enable(Feature.DYNAMIC_ALL_EVALUATION)
    if hasattr(args, 'enable_watch_mode') and args.enable_watch_mode: enable(Feature.INCREMENTAL_WATCH_MODE)
    if hasattr(args, 'enable_advanced_exports') and args.enable_advanced_exports: enable(Feature.ADVANCED_EXPORT_HEURISTICS)
    if hasattr(args, 'enable_graph_analysis') and args.enable_graph_analysis: enable(Feature.GRAPH_ANALYSIS)
    if hasattr(args, 'enable_call_graph') and args.enable_call_graph: enable(Feature.CALL_GRAPH_ANALYSIS)

    # 2. Load or create AnalysisConfig (it will use current global flag states for its defaults)
    config: AnalysisConfig
    if hasattr(args, 'config_file') and args.config_file: # Updated arg name
        path = Path(args.config_file)
        try:
            if path.suffix.lower() in {'.yaml', '.yml'}:
                with open(path, 'r', encoding='utf-8') as f: config_data = yaml.safe_load(f)
            else:
                with open(path, 'r', encoding='utf-8') as f: config_data = json.load(f)
            config = AnalysisConfig(**config_data)
            logger.info(f"Loaded configuration from: {path}")
        except Exception as e:
            logger.error(f"Error loading config {args.config_file}: {e}. Using defaults.", exc_info=True)
            config = AnalysisConfig()
    else:
        config = AnalysisConfig()

    # 3. Apply other direct CLI overrides to the config object
    # These args are part of the 'analyze_parser' specifically
    if hasattr(args, 'dynamic_all_check_override') and args.dynamic_all_check_override is not None: # A specific override for the config
        config.dynamic_all_check = args.dynamic_all_check_override
    if hasattr(args, 'include_special'): config.include_special_members = args.include_special
    if hasattr(args, 'log_level'): config.logging_level = args.log_level.upper()
    if hasattr(args, 'log_file'): config.log_file = args.log_file
    if hasattr(args, 'url_patterns'): config.url_patterns_file = args.url_patterns
    if hasattr(args, 'doc_urls'): config.doc_url_file = args.doc_urls
    if hasattr(args, 'parallel'): config.parallel_processing = args.parallel
    if hasattr(args, 'max_workers'): config.max_workers = args.max_workers
    if hasattr(args, 'verify_urls') and args.verify_urls is False: config.verify_doc_urls = False # store_false
    if hasattr(args, 'auto_install_dependencies'): config.auto_install_dependencies = args.auto_install_dependencies
    if hasattr(args, 'api_resolution_mode'): config.analysis_mode = AnalysisMode(args.api_resolution_mode)
    if hasattr(args, 'exclusions'): config.exclusion_file = args.exclusions
    
    # Project metadata overrides
    if hasattr(args, 'project_name') and args.project_name: 
        config.project_name = args.project_name
    if hasattr(args, 'project_version') and args.project_version: 
        config.project_version = args.project_version
    if hasattr(args, 'pypi_package_name') and args.pypi_package_name: 
        config.pypi_package_name = args.pypi_package_name

    # Ensure config fields driven by flags correctly reflect the final flag state
    # (AnalysisConfig init should do this, this is a sanity check or for overrides)
    config.enable_watch_mode = is_enabled(Feature.INCREMENTAL_WATCH_MODE)
    config.dynamic_all_check = is_enabled(Feature.DYNAMIC_ALL_EVALUATION) if not (hasattr(args, 'dynamic_all_check_override') and args.dynamic_all_check_override is not None) else config.dynamic_all_check


    return config


def save_analysis_results(results: Dict[str, Any], output_path_str: str, output_format: str = 'json'):
    """Saves analysis results to a file."""
    output_path = Path(output_path_str)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            if output_format == 'yaml':
                yaml.dump(results, f, sort_keys=False, indent=2)
            else: # Default to JSON
                json.dump(results, f, indent=2)
        console.print(f"Analysis results saved to [cyan]{output_path}[/]")
    except Exception as e:
        console.print(f"[red]Error saving results to {output_path}: {e}[/]")


def display_analysis_summary(analysis_results_all_repos: Dict[str, Dict[str, Any]]):
    """Displays a summary of the analysis results using Rich if available."""
    if not RICH_AVAILABLE or not Table or not Progress:
        console.print("\nAnalysis Summary (basic):")
        for repo, data in analysis_results_all_repos.items():
            console.print(f"  Repository: {repo}")
            stats = data.get('metrics', {})
            errors = data.get('errors', [])
            console.print(f"    Modules: {stats.get('module_count', 'N/A')}, Relationships: {stats.get('total_relationship_count', 'N/A')}, Errors: {len(errors)}")
        return

    summary_table = Table(title="MapCoDoc Analysis Summary", box=box.DOUBLE_EDGE, show_lines=True)
    summary_table.add_column("Repository Path", style="cyan", overflow="fold")
    summary_table.add_column("Modules", justify="right")
    summary_table.add_column("Files", justify="right")
    summary_table.add_column("Chain Candidates", justify="right")
    summary_table.add_column("API Mappings", justify="right")
    summary_table.add_column("Errors", justify="right", style="red")
    summary_table.add_column("Time (s)", justify="right")

    for repo_path, result_data in analysis_results_all_repos.items():
        metrics = result_data.get("metrics", {})
        analysis_details = result_data.get("analysis_details", {})
        errors_list = result_data.get("errors", [])
        
        # These need to be populated correctly by AnalyzerIntegration results
        chain_candidate_count = metrics.get("final_chain_candidate_count", "N/A") 
        api_map_count = metrics.get("api_map_entry_count", "N/A") 
        
        summary_table.add_row(
            repo_path,
            str(metrics.get("module_count", 0)),
            str(metrics.get("files_analyzed", 0)),
            str(chain_candidate_count),
            str(api_map_count),
            str(len(errors_list)),
            f"{metrics.get('total_analysis_time_cli', 0.0):.2f}"
        )
    console.print(summary_table)
    # TODO: Add display_errors part if needed


# --- Argument Parser Setup ---

def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MapCoDoc: Code to Documentation Traceability Pipeline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('--version', action='version', version='%(prog)s 0.2.0') # Placeholder

    subparsers = parser.add_subparsers(dest='command', title='Available Commands',
                                       help='Run `mapcodoc <command> --help` for command-specific help.')
    subparsers.required = True # A command must be specified

    # --- Analyze Sub-command ---
    analyze_parser = subparsers.add_parser('analyze', help='Analyze a Python repository for code-to-documentation traceability.')
    analyze_parser.add_argument(
        "repository_paths",
        nargs="+",
        help="Paths to local or remote code repositories."
    )
    analyze_parser.add_argument(
        "--config-file", metavar="PATH",
        help="Path to a YAML or JSON AnalysisConfig file."
    )
    analyze_parser.add_argument(
        "--output", "-o", default=None,
        help="Output file path for comprehensive analysis results (JSON or YAML)."
    )
    analyze_parser.add_argument(
        "--output-graph-file", metavar="PATH",
        help="Optional: Path to save the GraphStore object (e.g., as GraphML). If not provided, graph data might only be in the main output if embedded."
    )
    analyze_parser.add_argument(
        "--format", choices=["json", "yaml"], # For main output file
        default="json", help="Output format for the main results file (default: inferred from --output extension, else json)."
    )
    
    # all other arguments for 'analyze' including feature flags, logging, specific config overrides, doc processing args
    analyze_parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO", help="Logging level.")
    analyze_parser.add_argument("--log-file", metavar="PATH", help="Path to log output to a file.")
    analyze_parser.add_argument("--dynamic-all-check-override", action=argparse.BooleanOptionalAction, help="Specifically enable/disable dynamic __all__ check for AnalysisConfig.")
    analyze_parser.add_argument("--include-special", action="store_true", help="Include special (dunder) methods.")
    analyze_parser.add_argument("--parallel", action="store_true", help="Enable parallel processing for multiple repositories.")
    analyze_parser.add_argument("--max-workers", type=int, help="Max workers for parallel processing (default: CPU count).")
    analyze_parser.add_argument("--exclusions", metavar="PATH", help="Path to JSON file with exclusion patterns.")
    analyze_parser.add_argument("--auto-install-dependencies", action="store_true", help="Attempt to install project dependencies for dynamic analysis venv.")
    
    # Project metadata overrides
    metadata_group = analyze_parser.add_argument_group('Project Metadata', 'Override auto-detected project name and version.')
    metadata_group.add_argument("--project-name", metavar="NAME", help="Override auto-detected project/library name (e.g., 'sklearn' for scikit-learn).")
    metadata_group.add_argument("--project-version", metavar="VERSION", help="Override auto-detected project version.")
    metadata_group.add_argument("--pypi-package-name", metavar="NAME", help="PyPI package name if different from project name (e.g., 'scikit-learn' for sklearn).")
    
    feature_group = analyze_parser.add_argument_group('Feature Flags', 'Control optional features globally for this analysis run.')
    feature_group.add_argument("--enable-api-boundaries", action="store_true", help="Enable heuristic boundary scoring in APIPathResolver.")
    feature_group.add_argument("--enable-chain-candidates", action="store_true", help="Enable collection of re-exports as chain candidates.")
    feature_group.add_argument("--enable-dynamic-all", action="store_true", help="Globally enable dynamic __all__ evaluation feature.")
    feature_group.add_argument("--enable-watch-mode", action="store_true", help="Enable incremental watch mode (for the first repository path).")
    feature_group.add_argument("--enable-advanced-exports", action="store_true", help="Enable advanced export heuristics for API path resolution.")
    feature_group.add_argument("--enable-graph-analysis", action="store_true", help="Enable NetworkX-based graph analysis. (Default: Disabled)")
    feature_group.add_argument("--enable-call-graph", action="store_true", help="Enable the analysis of function and method call relationships. (Default: Disabled)")
    
    doc_group = analyze_parser.add_argument_group('Documentation Processing', 'Options for automatic documentation extraction after analysis.')
    doc_group.add_argument("--doc-source", metavar="PATH_OR_URL", help="Path to PDF or URL for documentation extraction after analysis.")
    doc_group.add_argument("--target-module", metavar="MODULE", help="Module to extract docs for (default: inferred from repo). Use with --doc-source.")
    doc_group.add_argument("--skip-llm", action="store_true", help="Skip LLM-based structured extraction in doc processing.")
    
    analyze_parser.set_defaults(func=handle_analyze_command) # Link to handler

    # --- Feature Management Sub-command ---
    features_parser = subparsers.add_parser('save-features', help='Save the current runtime state of all feature flags to a JSON file.')
    features_parser.add_argument('--output', '-o', default='mapcodoc_feature_states.json', help='Output JSON file path.')
    # Allow setting flags before saving their state
    save_feature_flags_group = features_parser.add_argument_group('Toggle Flags Before Saving')
    save_feature_flags_group.add_argument("--enable-api-boundaries", action="store_true", help="Set API_BOUNDARY_DETECTION before saving.")
    # ... add all other --enable-* flags if desired for this command too
    features_parser.set_defaults(func=handle_save_features_command)

    # --- Doc Processing Sub-command ---
    docs_parser = subparsers.add_parser('extract-docs', help='Extract documentation for a module from PDF/Web.')
    docs_parser.add_argument("--db-path", required=True, help="Path to the MapCoDoc database (e.g., mapcodoc.db)")
    docs_parser.add_argument("--library-name", required=True, help="Library name (e.g., torch, numpy)")
    docs_parser.add_argument("--version", required=True, help="Library version (e.g., 2, 1.0.0)")
    docs_parser.add_argument("--target-module", required=False, help="Optional: Module prefix filter (default: auto-detect from library name)")
    docs_parser.add_argument("--doc-source", required=True, help="Path to PDF or URL of documentation")
    docs_parser.add_argument("--skip-llm", action="store_true", help="Skip LLM-based structured extraction (useful if no OPENAI_API_KEY)")
    docs_parser.set_defaults(func=handle_extract_docs_command)
    
    # --- List Features Command (Top Level or as subcommand) ---
    # If top-level: parser.add_argument("--list-features", action="store_true", help="List all feature flags and exit.")
    # If subcommand:
    list_features_parser = subparsers.add_parser('list-features', help='List all available feature flags and their current runtime state.')
    list_features_parser.set_defaults(func=handle_list_features_command)


    # --- Relationships Command Group ---
    rel_parser = subparsers.add_parser('relationships', help="Query and analyze relationships from a previous analysis run.")
    rel_subparsers = rel_parser.add_subparsers(dest="rel_command", title="Relationship Commands", required=True)

    # Common arguments for most relationship commands that need loaded data
    rel_common_parser = argparse.ArgumentParser(add_help=False)
    rel_common_parser.add_argument(
        "--graph-file", metavar="PATH", required=True,
        help="Path to the graph data file (e.g., .gml, .graphml) saved by a previous 'analyze' run."
    )

    # Relationship: list
    rel_list_parser = rel_subparsers.add_parser('list', parents=[rel_common_parser], help="List relationships from the graph.")
    rel_list_parser.add_argument("--rel-type", nargs='*', help="Filter by relationship type(s) (e.g., IMPORTS INHERITS).")
    rel_list_parser.add_argument("--source", help="Filter by source node FQN.")
    rel_list_parser.add_argument("--target", help="Filter by target node FQN.")
    rel_list_parser.add_argument("--output-format", choices=["text", "json", "csv"], default="text", help="Output format.")
    # # Output to file or console handled by common handler logic
    # rel_list_parser.set_defaults(func=handle_relationships_list_command)

    # # Relationship: stats
    # rel_stats_parser = rel_subparsers.add_parser('stats', parents=[rel_common_parser], help="Show statistics about relationships in the graph.")
    # rel_stats_parser.add_argument("--rel-type", nargs='*', help="Calculate stats for specific relationship type(s).")
    # rel_stats_parser.add_argument("--output-format", choices=["text", "json"], default="text", help="Output format.")
    # rel_stats_parser.set_defaults(func=handle_relationships_stats_command)
    
    return parser

# --- Command Handlers ---

def handle_analyze_command(args: argparse.Namespace, config: AnalysisConfig):
    """Handles the main 'analyze' command logic."""
    logger.info(f"Analysis Config: {config}")
    
    errors_in_codebase: List[Dict[str, Any]] = [] 

    if config.enable_watch_mode:
        if not args.repository_paths:
            console.print("[red]Error: Repository path required for watch mode.[/red]")
            return 1 # Indicate error
        if len(args.repository_paths) > 1:
            logger.warning(f"Watch mode enabled, but multiple repository paths provided. Watching only the first: {args.repository_paths[0]}")
        
        repo_path_to_watch = args.repository_paths[0]
        console.print(f"[yellow]Watch mode starting for: {repo_path_to_watch}...[/yellow]")
        logger.info(f"Watch mode starting for: {repo_path_to_watch}...")

        registry = None # Ensure registry is defined in this scope for finally block
        try:
            registry = MapCoDocRegistry(repo_path=repo_path_to_watch, config=config)
            registry.initialize_components() 

            analyzer = registry.get_component(AnalyzerIntegration.COMPONENT_NAME)
            if not analyzer:
                console.print("[red]FATAL: AnalyzerIntegration component not found. Watch mode cannot start.[/red]")
                logger.critical("AnalyzerIntegration component not found in registry. Watch mode cannot start.")
                if registry: registry.shutdown() # Attempt shutdown
                return 1
            
            console.print("[yellow]Performing initial full analysis...[/yellow]")
            logger.info("Performing initial full analysis in watch mode...")
            initial_results = analyzer.analyze_codebase(repo_path_to_watch)
            
            # Collect errors from initial scan
            if initial_results.get("errors"):
                errors_in_codebase.extend(initial_results["errors"])

            # Display summary and save results for the initial scan
            # Ensure display_analysis_summary and save_analysis_results are defined or imported
            if RICH_AVAILABLE: # Assuming RICH_AVAILABLE is defined globally in the script
                 display_analysis_summary({repo_path_to_watch: initial_results})
            else:
                 logger.info(f"Initial scan summary (basic print): {initial_results.get('metrics')}")

            if args.output:
                output_format = 'yaml' if args.output.endswith(('.yaml', '.yml')) or args.format == 'yaml' else 'json'
                save_analysis_results({repo_path_to_watch: initial_results}, args.output, output_format)
                logger.info(f"Initial analysis complete. Results saved to {args.output}")
            else:
                logger.info("Initial analysis complete. No --output specified; skipping file save.")
            
            console.print("[green]Initial analysis complete. Watching for file changes. Press Ctrl+C to stop.[/green]")
            logger.info("Watching for file changes. Press Ctrl+C to stop.")
            while True:
                time.sleep(1)
                watcher_comp = registry.get_component(FileSystemWatcher.COMPONENT_NAME)
                if watcher_comp and not watcher_comp.is_running():
                    logger.warning("File watcher thread appears to have stopped. Exiting watch mode.")
                    console.print("[yellow]File watcher stopped. Exiting.[/yellow]")
                    break
        except KeyboardInterrupt:
            logger.info("Watch mode interrupted by user.")
            console.print("\n[yellow]Watch mode interrupted. Shutting down...[/yellow]")
        except Exception as e_watch: # Catch other exceptions during watch mode setup/run
            logger.error(f"Error during watch mode operation: {e_watch}", exc_info=True)
            console.print(f"[red]Error during watch mode: {e_watch}[/red]")
            # Add error to codebase errors if appropriate, though this is a runtime error
            errors_in_codebase.append({"file": repo_path_to_watch, "error": f"Watch mode runtime error: {str(e_watch)}"})
            return 1 # Indicate error
        finally:
            if registry: registry.shutdown()
            logger.info("MapCoDoc watch mode shut down.")
            console.print("[blue]MapCoDoc watch mode shut down.[/blue]")
        
        # For watch mode, decide on final exit code based on initial scan errors
        return 1 if errors_in_codebase else 0 
    
    else: # Standard single run
        logger.info("Starting standard single analysis run.")
        all_repo_results: Dict[str, Any] = {} # Ensure this is defined for the else block
        has_errors_overall = False
        
        repo_manager = RepoManager()
        
        try:
            for repo_path in args.repository_paths:
                
                console.print(f"[cyan]Preparing repository: {repo_path}[/cyan]")
                try:
                    repo_path, is_temp = repo_manager.prepare_repository(repo_path)
                except Exception as e:
                    console.print(f"[red]Failed to prepare repository {repo_path}: {e}[/red]")
                    all_repo_results[repo_path] = {"errors": [{"message": str(e)}]}
                    has_errors_overall = True
                    continue
                
                console.print(f"[cyan]Analyzing repository: {repo_path}[/cyan]")
                logger.info(f"Analyzing repository: {repo_path}")
                registry_run = None # Define for finally block
                try:
                    # 1. Create and fully initialize the registry
                    registry_run = MapCoDocRegistry(repo_path=repo_path, config=config, auto_initialize=True)
                    # 2. Get the fully initialized analyzer instance from the registry
                    analyzer = registry_run.get_component(AnalyzerIntegration.COMPONENT_NAME)

                    if not analyzer:
                        err_msg = f"AnalyzerIntegration component not found for {repo_path}. Skipping."
                        console.print(f"[red]{err_msg}[/red]")
                        logger.error(err_msg)
                        all_repo_results[repo_path] = {"errors": [{"message": err_msg, "details": "Component not registered or failed to initialize."}]}
                        has_errors_overall = True
                        raise RuntimeError("Failed to retrieve AnalyzerIntegration component from the registry.")
                    
                    start_time_repo = time.perf_counter()
                    # 3. Run the analysis
                    repo_result = analyzer.analyze_codebase(repo_path)
                    
                    # --- DB Ingestion Start ---
                    try:
                        analysis_data = repo_result.get("analysis_details", {})
                        project_metadata = registry_run.get_project_metadata()
                        lib_name, lib_version = project_metadata.get("name"), project_metadata.get("version")
                        
                        console.print("[cyan]Ingesting results into database...[/cyan]")
                        db = MapCoDocDB(f'mapcodoc_output/{lib_name}_{lib_version}.db')
                        db.init_db()
                        
                        if analysis_data:
                            db.ingest_analysis_results(analysis_data)
                            console.print(f"[green]Database ingestion successful. ({len(analysis_data)} modules)[/green]")
                        else:
                            console.print("[yellow]No analysis details found to ingest.[/yellow]")
                    except Exception as e:
                        console.print(f"[red]Database ingestion failed: {e}[/red]")
                        logger.error(f"DB Ingestion error: {e}", exc_info=True)
                    # --- DB Ingestion End ---
                    
                    # --- Documentation Processing Start ---
                    if hasattr(args, 'doc_source') and args.doc_source:
                        try:
                            console.print("[cyan]Starting documentation extraction...[/cyan]")
                            
                            # Check for OPENAI_API_KEY
                            openai_key = os.environ.get("OPENAI_API_KEY")
                            skip_llm = getattr(args, 'skip_llm', False) or not openai_key
                            
                            if not openai_key and not getattr(args, 'skip_llm', False):
                                console.print("[yellow]Warning: OPENAI_API_KEY not found. LLM extraction will be skipped.[/yellow]")
                            
                            # Determine target module
                            target_module = getattr(args, 'target_module', None) or lib_name
                            
                            doc_runner = DocProcessingRunner(
                                db_path=f'mapcodoc_output/{lib_name}_{lib_version}.db',
                                library_name=lib_name,
                                version=lib_version
                            )
                            doc_runner.run(args.doc_source, target_module=target_module, skip_llm=skip_llm)
                            console.print(f"[green]Documentation extraction complete for {lib_name}[/green]")
                        except Exception as e:
                            console.print(f"[yellow]Documentation extraction failed: {e}[/yellow]")
                            logger.warning(f"Doc processing error (non-fatal): {e}", exc_info=True)
                            # Don't fail the entire analysis for doc processing errors
                    # --- Documentation Processing End ---
                    
                    elapsed_time_repo = time.perf_counter() - start_time_repo
                    repo_result.setdefault('metrics', {})['total_analysis_time_cli'] = elapsed_time_repo
                    all_repo_results[repo_path] = repo_result
                    
                    if repo_result.get("errors"):
                        has_errors_overall = True
                        errors_in_codebase.extend(repo_result["errors"]) # Aggregate errors
                    logger.info(f"Analysis of {repo_path} completed in {elapsed_time_repo:.2f}s.")

                except Exception as e_run:
                    err_msg = f"Critical error analyzing {repo_path}: {e_run}"
                    console.print(f"[red]{err_msg}[/red]")
                    logger.error(err_msg, exc_info=True)
                    all_repo_results[repo_path] = {"errors": [{"message": str(e_run), "details": traceback.format_exc()}]}
                    has_errors_overall = True
                finally:
                    if registry_run: registry_run.shutdown()
        finally:
            # Cleanup any temporary clones
            repo_manager.cleanup()
        
        if RICH_AVAILABLE:
            display_analysis_summary(all_repo_results)
        else:
            logger.info(f"Analysis summary (basic print): {json.dumps({k: v.get('metrics') for k,v in all_repo_results.items()}, indent=2)}")
        # Only save if --output was explicitly provided
        if args.output:
            output_format = 'yaml' if args.output.endswith(('.yaml', '.yml')) or args.format == 'yaml' else 'json'
            save_analysis_results(all_repo_results, args.output, output_format)
            logger.info(f"Analysis results saved to {args.output}")
        else:
            logger.info("No --output specified; skipping code analysis results file save.")
        
        return 1 if has_errors_overall else 0


def handle_extract_docs_command(args: argparse.Namespace, config: Optional[AnalysisConfig]):
    """Handles the 'extract-docs' command for documentation processing."""
    logger.info(f"Executing 'extract-docs' from '{args.doc_source}'")
    
    # Determine effective skip_llm
    openai_key = os.environ.get("OPENAI_API_KEY") # Check for OPENAI_API_KEY
    skip_llm = args.skip_llm or not openai_key
    
    if not openai_key and not args.skip_llm:
        console.print("[yellow]Warning: OPENAI_API_KEY not found in environment or .env file.[/yellow]")
        console.print("[yellow]LLM-based structured extraction (Step 5) will be skipped.[/yellow]")
        console.print("[dim]Set OPENAI_API_KEY in .env to enable LLM extraction.[/dim]")
    elif args.skip_llm:
        console.print("[dim]LLM extraction skipped by user request (--skip-llm)[/dim]")
    
    try:
        runner = DocProcessingRunner(
            db_path=args.db_path,
            library_name=args.library_name,
            version=args.version
        )
        runner.run(args.doc_source, target_module=getattr(args, 'target_module', None), skip_llm=skip_llm)
        console.print(f"[green]Documentation extraction complete[/green]")
        return 0
    except Exception as e:
        console.print(f"[red]Documentation extraction failed: {e}[/red]")
        logger.error(f"Doc extraction error: {e}", exc_info=True)
        return 1

def handle_list_features_command(args: argparse.Namespace, config: Optional[AnalysisConfig]): # Config might not be needed
    logger.info("Executing 'list-features' command.")
    display_feature_flags() # Assumes display_feature_flags is defined in this file
    return 0

def handle_save_features_command(args: argparse.Namespace, config: Optional[AnalysisConfig]): # Config might not be needed
    logger.info(f"Executing 'save-features' command. Output to: {args.output}")
    save_feature_flags(args.output) # Assumes save_feature_flags is defined
    return 0


# --- Main Execution ---
def main_cli():
    parser = create_parser()
    args = parser.parse_args()
    
    config: Optional[AnalysisConfig] = None # Define config here

    # Handle top-level --list-features before attempting to load config for a command
    if hasattr(args, 'list_features') and args.list_features and not args.command:
        display_feature_flags() # Assumes this function is defined
        return 0

    if args.command == "analyze":
        if args.enable_api_boundaries:
            enable(Feature.API_BOUNDARY_DETECTION)
        if args.enable_chain_candidates:
            enable(Feature.CHAIN_CANDIDATE_COLLECTION)
        if args.enable_dynamic_all:
            enable(Feature.DYNAMIC_ALL_EVALUATION)
        if args.enable_watch_mode:
            enable(Feature.INCREMENTAL_WATCH_MODE)
        if args.enable_advanced_exports:
            enable(Feature.ADVANCED_EXPORT_HEURISTICS)
        if args.enable_graph_analysis:
            enable(Feature.GRAPH_ANALYSIS)
        if args.enable_call_graph:
            enable(Feature.CALL_GRAPH_ANALYSIS)
        
        config = load_config(args) # load_config also handles its own --list-features if it's an arg of analyze
        configure_logging(level=getattr(logging, config.logging_level, logging.INFO), log_file=config.log_file)
        
    elif args.command in ["relationships", "visualize", "save-features"]: # Commands that might not need full config load initially
        # Basic logging for these commands, can be overridden by their specific args if they have them
        log_level_str = "INFO"
        if hasattr(args, 'log_level') and args.log_level: # if specific subcommand has log_level
            log_level_str = args.log_level.upper()
        configure_logging(level=getattr(logging, log_level_str, logging.INFO))
    else: # Default basic logging
        configure_logging()

    logger.info(f"MapCoDoc CLI invoked with command: {args.command}")

    exit_code = 0
    try:
        if args.command == 'analyze':
            exit_code = handle_analyze_command(args, config) # config is already loaded
        elif args.command == 'extract-docs':
            exit_code = handle_extract_docs_command(args, config)
        elif args.command == 'save-features':
            exit_code = handle_save_features_command(args, config) # Pass config if save_features needs it (e.g. for flag descriptions)
        elif args.command == 'list-features': # If it's a subcommand
             display_feature_flags()
             exit_code = 0
        # elif args.command == 'relationships':
        #     if args.rel_command == 'list': exit_code = handle_relationships_list_command(args, config)
        #     elif args.rel_command == 'stats': exit_code = handle_relationships_stats_command(args, config)
        #     else: parser.parse_args(['relationships', '--help']); exit_code = 1
        # elif args.command == 'visualize':
        #     if args.vis_command == 'imports': exit_code = handle_visualize_imports_command(args, config)
        #     elif args.vis_command == 'inheritance': exit_code = handle_visualize_inheritance_command(args, config)
        #     elif args.vis_command == 'callgraph': exit_code = handle_visualize_callgraph_command(args, config)
        #     elif args.vis_command == 'export-gexf': exit_code = handle_export_gexf_command(args, config)
        #     else: parser.parse_args(['visualize', '--help']); exit_code = 1
        else:
            parser.print_help()
            exit_code = 1
            
    except Exception as e: # General fallback
        logger.critical(f"Unhandled error in CLI: {e}", exc_info=True)
        console.print(f"[bold red]Unhandled CLI Error: {e}[/]")
        exit_code = 1
    
    return exit_code

