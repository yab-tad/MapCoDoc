# Code Analysis Module

The `code_analysis` module provides comprehensive static and dynamic analysis of Python codebases. It extracts code structure, tracks relationships (imports, exports, inheritance, calls), resolves public API names, and prepares analysis results for database ingestion.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Analysis Workflow](#analysis-workflow)
  - [Phase 1: Initialization](#phase-1-initialization)
  - [Phase 2: File-by-File Analysis](#phase-2-file-by-file-analysis)
  - [Phase 3: Post-Processing](#phase-3-post-processing)
  - [Phase 4: API Resolution](#phase-4-api-resolution)
  - [Phase 5: Output & Database Ingestion](#phase-5-output--database-ingestion)
- [Directory Structure](#directory-structure)
- [Key Components](#key-components)
- [Data Models](#data-models)
- [Configuration](#configuration)
- [Feature Flags](#feature-flags)
- [Usage](#usage)

---

## Overview

The code analysis module performs the following high-level tasks:

1. **Parse Python source files** using AST-based analysis
2. **Extract code components** (classes, functions, methods, variables)
3. **Track relationships** (imports, exports, inheritance, calls)
4. **Resolve re-exports** to find true definition locations
5. **Resolve inherited members** for classes
6. **Compute public API names** for all components
7. **Generate structured output** ready for database ingestion

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           AnalyzerIntegration                               │
│                    (analyzers/analyzer_integration.py)                      │
│                         Main Orchestrator                                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
       ┌──────────────────────────────┼──────────────────────────────┐
       │                              │                              │
       ▼                              ▼                              ▼
┌──────────────────┐       ┌──────────────────┐       ┌───────────────────┐
│   CodeVisitor    │       │  APIPathResolver │       │InheritanceResolver│
│ (code_visitor.py)│       │ (api_resolver.py)│       │(inheritance_      │
│                  │       │                  │       │  resolver.py)     │
│ AST Parsing &    │       │ API Name         │       │ Inherited Member  │
│ Component        │       │ Resolution       │       │ Propagation       │
│ Extraction       │       │                  │       │                   │
└──────────────────┘       └──────────────────┘       └─────────┬─────────┘
                                                                │
                                                                ▼
                                                      ┌───────────────────┐
                                                      │ExternalIntrospector│
                                                      │(inheritance_      │
                                                      │  resolver.py)     │
                                                      │                   │
                                                      │ Dynamic External  │
                                                      │ Member Discovery  │
                                                      └───────────────────┘
       │                              │
       │                              │
       ▼                              ▼
┌──────────────────┐       ┌───────────────────┐
│DefinitionRegistry│       │   DynamicAnalyzer │
│(definition_      │       │(dynamic_analyzer. │
│ registry.py)     │       │  py)              │
│                  │       │                   │
│ Central FQN →    │       │ Runtime __all__   │
│ Definition       │       │ Evaluation        │
│ Tracking         │       │ (optional)        │
└──────────────────┘       └───────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────┐
│                    graph/ Submodule                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   │
│  │ GraphStore  │  │ImportTracker│  │ExportTracker│   │
│  │  (store.py) │  │(importer.py)│  │(exporter.py)│   │
│  └─────────────┘  └─────────────┘  └─────────────┘   │
│  ┌─────────────┐  ┌─────────────┐                    │
│  │Inheritance  │  │ CallGraph   │                    │ 
│  │ Tracker     │  │ Tracker     │  (Optional)        │
│  └─────────────┘  └─────────────┘                    │
└──────────────────────────────────────────────────────┘
                         │
                         ▼
              ┌──────────────────┐
              │   MapCoDoc DB    │
              │   (ingestion)    │
              └──────────────────┘
```

---

## Analysis Workflow

The analysis workflow proceeds in five distinct phases:

### Phase 1: Initialization

**Components Involved:** `AnalyzerIntegration`, `AnalysisConfig`, `FeatureFlags`

```
1.1 Configuration Setup
    ↓ Load AnalysisConfig (repo_path, exclusion patterns, etc.)
    ↓ Check feature flags (GRAPH_ANALYSIS, DYNAMIC_ALL_EVALUATION, etc.)
    
1.2 Repository Discovery
    ↓ _find_top_level_packages() - Identify package roots
    ↓ find_python_package_roots() - Handle multi-language repos
    ↓ Detect module_name_prefix from project metadata
    
1.3 File Discovery
    ↓ _find_python_files() - Scan for .py files
    ↓ Apply exclusion patterns (exclude __pycache__, tests, etc.)
    
1.4 Pre-computation
    ↓ _precompute_known_modules() - Build module FQN lookup
    ↓ _build_package_module_graph() - Create package/module hierarchy
```

### Phase 2: File-by-File Analysis

**Components Involved:** `CodeVisitor`, `DefinitionRegistry`, `DynamicAnalyzer` (optional)

For each Python file:

```
2.1 Static Analysis (code_visitor.py)
    ↓ Parse file with ast.parse()
    ↓ Visit AST nodes (ClassDef, FunctionDef, Import, etc.)
    ↓ Extract:
    ↓   • Components (classes, functions, methods, variables)
    ↓   • Import records
    ↓   • Export records (from __all__ or implicit public names)
    ↓   • Base classes and inheritance relationships
    ↓   • Decorators and their effects
    ↓   • Signatures and parameters
    ↓   • Docstrings

2.2 Module Interface Extraction
    ↓ Determine if __all__ is defined
    ↓ Check if __all__ is static or dynamic
    ↓ Track wildcard imports and aggregation sources
    
2.3 Dynamic Analysis (optional, if enabled)
    ↓ Create isolated virtual environment
    ↓ Execute module to evaluate dynamic __all__
    ↓ Merge dynamic results with static analysis
    
2.4 Component Registration
    ↓ Register all components in DefinitionRegistry
    ↓ Store results in file_analysis_results dict
    ↓ Update module_results_by_fqn index
    
2.5 IR Generation (optional)
    ↓ Convert to Intermediate Representation (IR)
    ↓ Cache IR for incremental analysis
```

**Output per file:**
```python
{
    "module_name": "package.module",
    "package_name": "package",
    "module_interface": {
        "is_init_file": False,
        "has_all": True,
        "all_is_dynamic": False,
        "all_values": ["Class1", "function1"],
        "wildcard_imports": [],
        "needs_dynamic_analysis": False
    },
    "components": {
        "package.module.Class1": {
            "name": "Class1",
            "fully_qualified_name": "package.module.Class1",
            "component_kind": "class",
            "bases": ["BaseClass"],
            "base_fqns": ["package.base.BaseClass"],
            "methods": [...],
            "line_number": 10,
            "end_line": 50,
            "docstring": "...",
            "signature": {...},
            "parameters": [...],
            "decorators": [...]
        },
        ...
    },
    "import_records": [...],
    "export_records": [...],
    "module_statistics": {
        "num_classes": 5,
        "num_functions": 10,
        "num_methods": 25,
        "loc": 500
    }
}
```

### Phase 3: Post-Processing

**Components Involved:** `AnalyzerIntegration`, `InheritanceResolver`

After all files are analyzed:

```
3.1 Resolve Aggregated __all__ Exports
    ↓ For modules with dynamic __all__ patterns (e.g., numpy)
    ↓ _resolve_aggregated_all_exports() expands source references
    
3.2 Finalize Target FQNs (First Pass)
    ↓ _finalize_all_target_fqns()
    ↓ Chase re-export chains to find true definitions
    ↓ Update target_item_fqn in export_records
    
3.3 Resolve Final Unlinked Exports
    ↓ _perform_final_iterative_resolution()
    ↓ Handle circular or complex re-export chains
    
3.4 Finalize Target FQNs (Second Pass)
    ↓ Catch any newly resolved references
    
3.5 Finalize Import Records
    ↓ _finalize_all_import_records()
    ↓ Correct name_bound_points_to_fqn to true definitions
    ↓ Update is_source_internal flags
    
3.6 Finalize Base Class FQNs
    ↓ _finalize_all_base_fqns()
    ↓ Resolve base classes to true definitions
    ↓ Handle local definitions, imports, wildcards
    
3.7 Resolve Inherited Members
    ↓ InheritanceResolver.update_analysis_results()
    ↓ For each class, find inherited methods via BFS
    ↓ Classify bases as internal/external
    ↓ For INTERNAL bases: Extract inherited methods from analysis results
    ↓ For EXTERNAL bases: 
    ↓   • ExternalIntrospector discovers methods via dynamic introspection
    ↓   • Creates isolated temp venv, installs packages, introspects
    ↓   • Extracts method signatures from external libraries
    ↓   • Caches results to avoid repeated installations
    ↓ Handle exception fallback classes (e.g., try/except import patterns)
    ↓ Populate inherited_methods dict in class components
```

**Post-processing updates to components:**
```python
{
    "package.module.ChildClass": {
        ...
        "base_fqns": ["package.base.ParentClass"], 
        "inherited_methods": {
            "method_name": {
                "name": "method_name",
                "source_class_fqn": "package.base.ParentClass",
                "original_fqn": "package.base.ParentClass.method_name",
                "member_type": "method",
                "is_external": False,
                "signature": {...}
            }
        },
        "external_bases": []
    }
}
```

### Phase 4: API Resolution

**Components Involved:** `APIPathResolver`, `AnalyzerIntegration`

```
4.1 Build Chain Candidates Map
    ↓ _build_chain_candidates_map()
    ↓ Identify components that are re-exported
    ↓ Track all export paths for each candidate
    
4.2 Set Aggregated Module Statistics
    ↓ api_resolver.set_aggregated_module_statistics()
    ↓ Module stats used for chain scoring
    
4.3 Drive API Path Resolution
    ↓ drive_api_path_resolution()
    ↓ For each candidate:
    ↓   • Score all possible export chains
    ↓   • Select best chain based on heuristics
    ↓   • Compute API name (e.g., "torch.nn.Conv1d")
    
4.4 Update Results with API Data
    ↓ _update_result_with_api_resolution()
    ↓ Set API_name and API_names on components
    ↓ Set api_name_sources mapping
    ↓ Set best_export_chain for traceability
    
4.5 Propagate API Names to Children
    ↓ _propagate_api_names_to_children()
    ↓ Methods inherit API path from parent class
    ↓ Nested classes get proper API names
    
4.6 Propagate API Names to Inherited Members
    ↓ _propagate_api_names_to_inherited_members()
    ↓ Each inherited method gets:
    ↓   • inherited_api_name (via inheriting class)
    ↓   • original_api_name (via original class)
```

**API resolution output:**
```python
{
    "package.module.Class1": {
        ...
        "API_name": "package.Class1",           # Primary public name
        "API_names": ["package.Class1", "package.module.Class1"],
        "api_name_sources": {
            "package.Class1": "package",
            "package.module.Class1": "package.module"
        },
        "best_export_chain": [
            {"exporting_module_fqn": "package", "exported_name": "Class1", ...}
        ]
    }
}
```

### Phase 5: Output & Database Ingestion

**Components Involved:** `AnalyzerIntegration`, `MapCoDocDB`

```
5.1 Generate Final Output
    ↓ analyze_codebase() returns:
    ↓   • project_metadata
    ↓   • metrics (aggregated statistics)
    ↓   • files_analyzed count
    ↓   • analysis_details (file_analysis_results)
    ↓   • errors list
    
5.2 JSON Serialization (optional)
    ↓ Save to mapcodoc_output/mapcodoc_results.json
    
5.3 Database Ingestion
    ↓ MapCoDocDB.ingest_analysis_results(analysis_results)
    ↓ Phase 1: Create DBModule and DBMember records
    ↓ Phase 1.5: Link parent-child relationships
    ↓ Phase 2: Create DBExport records
    ↓ Phase 3: Create DBImport records
    ↓ Phase 4: Create DBInheritedMember records
```

---

## Directory Structure

```
code_analysis/
├── __init__.py                 # Package exports
├── config.py                   # AnalysisConfig dataclass
├── feature_flags.py            # Feature toggles (GRAPH_ANALYSIS, etc.)
├── events.py                   # Event definitions for pub/sub
│
├── code_visitor.py             # AST visitor - core parsing logic
├── code_components.py          # Component dataclasses (Class, Function, etc.)
├── parameter_analysis.py       # Signature and parameter parsing
├── decorator_analysis.py       # Decorator effect analysis
│
├── definition_registry.py      # Central FQN → definition tracking
├── api_resolver.py             # API path resolution and chain scoring
├── inheritance_resolver.py     # Inherited member resolution
├── dynamic_analyzer.py         # Runtime __all__ evaluation
│
├── analyzers/
│   └── analyzer_integration.py # Main orchestrator
│
├── graph/                      # Graph-based relationship tracking
│   ├── store.py                # GraphStore (node/edge storage)
│   ├── models.py               # ImportRecord, ExportDetails, etc.
│   ├── importer.py             # ImportTracker
│   ├── exporter.py             # ExportTracker
│   ├── inheritance_tracker.py  # InheritanceTracker
│   ├── call_graph.py           # CallGraphTracker (optional)
│   ├── traversal.py            # GraphTraversal utilities
│   └── relationships.py        # Relationship utilities
│
├── ir/                         # Intermediate Representation
│   ├── models.py               # Pydantic IR models
│   ├── converter.py            # Analysis result → IR conversion
│   ├── cache.py                # IR disk caching
│   ├── serialization.py        # IR serialization utilities
│   └── validation.py           # IR validation
│
├── modules/
│   └── call_graph_analysis.py  # Call graph analysis utilities
│
├── utils.py                    # Common utilities
├── relationship_types.py       # Relationship type constants
├── project_metadata.py         # Project metadata discovery
├── repo_manager.py             # Repository management
├── watcher.py                  # File system watcher (incremental mode)
├── mapcodocreg.py              # MapCoDoc Registry (component registry)
└── exclusions.json             # Default exclusion patterns
```

---

## Key Components

### `AnalyzerIntegration`

The main orchestrator class that coordinates all analysis phases.

```python
from code_analysis.analyzers.analyzer_integration import AnalyzerIntegration
from code_analysis.config import AnalysisConfig

config = AnalysisConfig(repo_path="/path/to/repo")
analyzer = AnalyzerIntegration(config=config)

# Analyze entire codebase
results = analyzer.analyze_codebase("/path/to/repo")

# Access results
file_results = analyzer.file_analysis_results
module_results = analyzer.module_results_by_fqn
```

### `CodeVisitor`

AST-based visitor that extracts components from Python source files.

```python
from code_analysis.code_visitor import analyze_code

result = analyze_code(
    file_path="/path/to/file.py",
    module_name="package.module",
    package_name="package",
    config=config
)
```

### `InheritanceResolver`

Post-analysis resolver for inherited members.

```python
from code_analysis.inheritance_resolver import InheritanceResolver

resolver = InheritanceResolver(
    file_analysis_results=analyzer.file_analysis_results,
    top_level_packages={"mypackage"}
)
resolver.update_analysis_results()
```

### `ExternalIntrospector`

Dynamically discovers inherited members from external libraries using isolated virtual environments.

```python
from code_analysis.inheritance_resolver import ExternalIntrospector

introspector = ExternalIntrospector(cache_dir=Path("./introspection_cache"))

# Discover methods from external base classes
methods = introspector.introspect_external_bases(
    ["sklearn.base.ClassifierMixin", "sklearn.base.BaseEstimator"]
)

# Returns: {
#     "score": {
#         "name": "score",
#         "source_class_fqn": "sklearn.base.ClassifierMixin",
#         "original_fqn": "sklearn.base.ClassifierMixin.score",
#         "signature": {"full": "score(self, X, y, sample_weight=None)"},
#         "is_external": True
#     },
#     ...
# }
```

**Features:**
- **Isolated venv**: Creates temporary virtual environment for package installation
- **Automatic cleanup**: Removes temp venv after introspection
- **PyPI discovery**: Framework-agnostic package name resolution (handles `sklearn` → `scikit-learn`)
- **Caching**: Caches results to avoid repeated installations
- **Exception fallback detection**: Handles try/except import patterns where fallback classes shadow real imports

### `APIPathResolver`

Resolves implementation FQNs to public API names.

```python
from code_analysis.api_resolver import APIPathResolver

resolver = APIPathResolver(config=config)
resolver.set_aggregated_module_statistics(module_stats)
api_name = resolver.resolve("package.internal.module.Class")
# Returns: "package.Class"
```

### `DefinitionRegistry`

Central registry tracking where each component is defined.

```python
from code_analysis.definition_registry import DefinitionRegistry

registry = DefinitionRegistry()
registry.register("package.module.Class", definition_info)
definition = registry.get("package.module.Class")
```

---

## Data Models

### Component Types

| Type | Description |
|------|-------------|
| `class` | Class definition |
| `function` | Module-level function |
| `method` | Method within a class |
| `variable` | Module-level or class-level variable |
| `property` | Property-decorated method |

### `ImportRecord`

Represents a single import statement:

```python
@dataclass
class ImportRecord:
    importer_module_fqn: str        # Who is importing
    line_number: int
    raw_module_specifier: str       # "from X import Y" → X
    raw_imported_name: str          # "from X import Y" → Y
    raw_alias: str                  # "import X as alias" → alias
    is_relative: bool
    level: int                      # Relative import level
    is_wildcard: bool               # "from X import *"
    source_module_fqn: str          # Resolved source module
    imported_entity_fqn: str        # Resolved entity FQN
    is_source_internal: bool        # From same codebase
    name_bound_in_importer: str     # Name available in namespace
    name_bound_points_to_fqn: str   # What the name resolves to
```

### `InheritedMember`

Represents an inherited method/property:

```python
@dataclass
class InheritedMember:
    name: str                       # Method name
    source_class_fqn: str           # Parent class FQN
    original_fqn: str               # Original method FQN
    member_type: str                # 'method', 'property'
    is_external: bool               # From external package
    signature: Dict                 # Signature info
    
    # Populated by API propagation:
    inherited_api_name: str         # Via inheriting class
    inherited_api_names: List[str]
    inheriting_class_fqn: str
    inheriting_class_api_name: str
```

---

## Configuration

### `AnalysisConfig`

```python
from code_analysis.config import AnalysisConfig, AnalysisMode

config = AnalysisConfig(
    # Repository
    repo_path="/path/to/repo",
    exclude_patterns=['__pycache__', '.git', 'tests'],
    max_file_size=1000000,
    
    # Analysis mode
    analysis_mode=AnalysisMode.HYBRID,  # STATIC_ONLY, DYNAMIC_PREFERRED, HYBRID
    max_analysis_depth=5,
    follow_imports=True,
    
    # Dynamic analysis
    dynamic_all_check=False,  # Enable dynamic __all__ evaluation
    dynamic_analysis_timeout=30,
    use_virtual_env=True,
    
    # Memory management
    max_memory_percentage=70.0,
    enable_memory_monitoring=True
)
```

### Exclusion Patterns

Default exclusions in `exclusions.json`:
- `__pycache__`
- `.git`, `.svn`
- `node_modules`
- `build`, `dist`
- `*.egg-info`
- Test directories (configurable)

---

## Feature Flags

Feature flags control optional/experimental behavior:

```python
from code_analysis.feature_flags import Feature, enable, disable, is_enabled

# Check if a feature is enabled
if is_enabled(Feature.GRAPH_ANALYSIS):
    # Graph-based tracking available
    pass

# Enable/disable at runtime
enable(Feature.CALL_GRAPH_ANALYSIS)
disable(Feature.DYNAMIC_ALL_EVALUATION)
```

| Flag | Default | Description |
|------|---------|-------------|
| `GRAPH_ANALYSIS` | `False` | Enable NetworkX-based graph building and relationship tracking (fallback for API resolution) |
| `CALL_GRAPH_ANALYSIS` | `False` | Track function/method call relationships (requires GRAPH_ANALYSIS) |
| `DYNAMIC_ALL_EVALUATION` | `False` | Evaluate `__all__` at runtime in isolated venv |
| `CHAIN_CANDIDATE_COLLECTION` | `True` | Collect re-export chain candidates for API path resolution |
| `API_BOUNDARY_DETECTION` | `True` | Detect package API boundaries for scoring export chains |
| `ADVANCED_EXPORT_HEURISTICS` | `True` | Enable advanced export chain detection heuristics |
| `INCREMENTAL_WATCH_MODE` | `False` | File system watching for incremental updates |

**Environment Variable Override:**
```bash
export MAPCODOC_FEATURE_GRAPH_ANALYSIS=true
export MAPCODOC_FEATURE_DYNAMIC_ALL_EVALUATION=false
```

---

## Usage

### Basic Usage

```python
from code_analysis.analyzers.analyzer_integration import AnalyzerIntegration
from code_analysis.config import AnalysisConfig

# Configure
config = AnalysisConfig(repo_path="/path/to/library")

# Initialize and run
analyzer = AnalyzerIntegration(config=config)
results = analyzer.analyze_codebase("/path/to/library")

# Access results
print(f"Analyzed {results['files_analyzed']} files")
print(f"Errors: {len(results['errors'])}")

# Get specific module results
module_data = analyzer.module_results_by_fqn.get("mylib.module")
if module_data:
    components = module_data.get("components", {})
    for fqn, comp in components.items():
        print(f"{comp['component_kind']}: {comp['API_name']}")
```

### With Database Ingestion

```python
from code_analysis.analyzers.analyzer_integration import AnalyzerIntegration
from code_analysis.config import AnalysisConfig
from mapcodoc_db import MapCoDocDB

# Analyze
config = AnalysisConfig(repo_path="/path/to/library")
analyzer = AnalyzerIntegration(config=config)
results = analyzer.analyze_codebase("/path/to/library")

# Ingest into database
db = MapCoDocDB("mapcodoc_output/mapcodoc.db")
db.init_db()
db.ingest_analysis_results(analyzer.file_analysis_results)
```

### CLI Usage

```bash
# Full analysis with database output
python -m cli.main analyze /path/to/library \
    --output mapcodoc_analysis_results.json

# With specific options
python -m cli.main analyze /path/to/library \
    --enable-dynamic-all \
    --enable-graph-analysis \
    --auto-install-dependencies \
    --log-level DEBUG

# With project metadata overrides
python -m cli.main analyze /path/to/library \
    --project-name "mylib" \
    --project-version "1.0.0" \
    --pypi-package-name "my-library"
```

---

## Output Format

### Analysis Results Structure

```python
{
    "project_metadata": {
        "name": "mylib",
        "version": "1.0.0",
        ...
    },
    "metrics": {
        "total_modules": 50,
        "total_classes": 120,
        "total_functions": 300,
        "total_methods": 800,
        "total_loc": 25000
    },
    "files_analyzed": 50,
    "analysis_details": {
        "/path/to/file.py": {
            "module_name": "mylib.module",
            "package_name": "mylib",
            "module_interface": {...},
            "components": {...},
            "import_records": [...],
            "export_records": [...],
            "module_statistics": {...}
        },
        ...
    },
    "errors": [
        {"file": "broken.py", "error": "SyntaxError", "details": {...}}
    ]
}
```

### Component Structure

```python
{
    "mylib.module.MyClass": {
        "name": "MyClass",
        "fully_qualified_name": "mylib.module.MyClass",
        "definition_module_fqn": "mylib.module",
        "component_kind": "class",
        "line_number": 10,
        "end_line": 100,
        "docstring": "Class documentation...",
        
        # Inheritance
        "bases": ["BaseClass"],
        "base_fqns": ["mylib.base.BaseClass"],
        "inherited_methods": {...},
        "external_bases": [],
        
        # Signature
        "signature": {
            "full": "MyClass(param1: int, param2: str = 'default')",
            "default": "MyClass(param1, param2='default')"
        },
        "parameters": [
            {"name": "param1", "type": "int", "default": null},
            {"name": "param2", "type": "str", "default": "'default'"}
        ],
        
        # API Names
        "API_name": "mylib.MyClass",
        "API_names": ["mylib.MyClass", "mylib.module.MyClass"],
        "api_name_sources": {
            "mylib.MyClass": "mylib",
            "mylib.module.MyClass": "mylib.module"
        },
        "best_export_chain": [...],
        
        # Nested members
        "methods": [
            {
                "name": "method1",
                "fully_qualified_name": "mylib.module.MyClass.method1",
                "component_kind": "method",
                ...
            }
        ],
        
        # Access & visibility
        "access_modifier": "public",
        "is_public": true,
        "is_async": false,
        "is_abstract": false,
        "decorators": ["@dataclass"]
    }
}
```

---

## Error Handling

The analysis pipeline handles errors gracefully:

1. **Parse Errors**: Files with syntax errors are logged and skipped
2. **Analysis Errors**: Individual component errors don't stop file analysis
3. **Resolution Errors**: Unresolved references are logged as warnings
4. **Dynamic Analysis Errors**: Falls back to static analysis

All errors are collected in the `errors` list of the final output.

---

## Performance Considerations

1. **Large Codebases**: Use exclusion patterns to skip test directories
2. **Memory**: Enable `aggressive_memory_cleanup` for very large repos
3. **IR Caching**: Enable IR caching for incremental analysis
4. **Graph Analysis**: Disable `GRAPH_ANALYSIS` if not needed (faster)
5. **Dynamic Analysis**: Disable unless `__all__` is truly dynamic

---

## Integration with Doc Processor

The analysis output is designed for seamless integration with the documentation processor:

1. **API Names**: Used to match documentation to code members
2. **Signatures**: Used for stop signal detection in doc extraction
3. **Inherited Members**: Enable documentation linking for inherited methods
4. **Export Chains**: Provide traceability for API name derivation

See `doc_processor/README.md` for documentation extraction workflow.

