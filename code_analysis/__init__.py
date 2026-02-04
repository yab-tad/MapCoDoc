"""
MapCoDoc Code Analysis Core Package.

Provides tools for comprehensive static and dynamic analysis of Python repositories,
construction of code relationship graphs, and resolution of public API paths.
"""

# Core Configuration & Entry Point
from .config import AnalysisConfig, AnalysisMode
from .mapcodocreg import MapCoDocRegistry
from .analyzers.analyzer_integration import AnalyzerIntegration
from .api_resolver import APIPathResolver, APIResolverAnalysisState
from .definition_registry import DefinitionRegistry, DefinitionInfo
from .dynamic_analyzer import DynamicAnalyzer

# Graph Components (for advanced use or type hinting)
from .graph.store import GraphStore
from .graph.traversal import GraphTraversal
from .graph.models import ImportRecord, ExportStep # Add other relevant models like ExportDetails if they exist
from .graph.importer import ImportTracker
from .graph.exporter import ExportTracker
from .graph.call_graph import CallGraphTracker
from .graph.inheritance_tracker import InheritanceTracker

# Static Analysis Core
from .code_visitor import analyze_code # For single-file static analysis
from .code_components import CodeComponent, Function, Method, Class, UnwrappedFunction
from .parameter_analysis import Parameter, Signature, analyze_signature

# Utilities & Exceptions
from .utils import AnalysisError, ParseError, ComponentError, ResourceError, Timer, Cache, configure_logging, get_logger
from .feature_flags import Feature, enable, disable, is_enabled, get_all_feature_states




__all__ = [
    # Main entry point
    'analyze_repository_fully',
    
    # Core Configuration & Components (for advanced library use)
    'AnalysisConfig', 
    'AnalysisMode',
    'MapCoDocRegistry',
    'AnalyzerIntegration',
    'APIPathResolver', 
    'DefinitionRegistry',
    'DynamicAnalyzer',
    'GraphTraversal',
    'ExportTracker', # Exposing trackers might be useful
    'ImportTracker',
    'CallGraphTracker',
    'InheritanceTracker',

    # Data Models
    'DefinitionInfo',
    'ExportStep',
    'ImportRecord',
    'CodeComponent', 'Function', 'Method', 'Class', 'UnwrappedFunction',
    'Parameter', 'Signature',

    # Utilities & Exceptions
    'AnalysisError', 'ParseError', 'ComponentError', 'ResourceError',
    'Timer', 'Cache', 
    'configure_logging', 'get_logger',

    # Feature Flags Management
    'Feature', 'enable', 'disable', 'is_enabled', 'get_all_feature_states',
]

# Initialize default logging for library users if they don't configure it.
# configure_logging() # Or let the application using the library configure logging.