# MapCoDoc: Code ⟷ API Documentation Trace-Link Recovery Pipeline

MapCoDoc **(Mapping Code onto Documentation)** is a comprehensive analysis pipeline designed to bridge the gap between source code and its reference documentation. It meticulously analyzes Python repositories to discover their true public API surface and then systematically crawls, processes, and links documentation to the corresponding code elements, enabling a new level of code comprehension and traceability.

## Core Goals

*   **Discover the True Public API:** Go beyond static analysis by handling dynamic `__all__` attributes, complex re-exports, and wildcard imports to find out what is *actually* exposed to end-users.
*   **Link Code to Docs:** Automatically find and create trace links between a function, class, or method in the code and its corresponding API reference documentation page.
*   **Enable Downstream Tooling:** Provide a rich, structured output (including a SQLite database, JSON analysis results, and structured documentation) that can power code intelligence tools, documentation validators, and automated software maintenance tasks.

## Features

-   **Tiered API Resolution:** A high-speed, graph-less resolver for common cases with an optional, robust graph-based fallback for maximum accuracy (disabled by default for performance).
-   **Static & Dynamic Analysis:** Combines AST-based static analysis with isolated dynamic execution for unparalleled accuracy.
-   **Inheritance Tracking:** Resolves inherited members across multi-level inheritance chains (e.g., `XGBRFClassifier` → `XGBClassifier` → `XGBModel`) and propagates API names to enable documentation linking for inherited methods.
-   **SQLite Database Storage:** Persists analysis results in a queryable SQLite database with modules, members, imports, exports, inherited members, and documentation.
-   **Web Documentation Processing:** Crawls web-based documentation, scrapes content, and extracts individual API docs using lexical + semantic search with class anchor propagation for accurate method extraction.
-   **PDF Documentation Support:** Extracts API documentation from PDF files using hybrid search techniques with two-phase extraction (classes first, then scoped methods).
-   **LLM-Powered Structuring:** Uses GPT-4o to convert raw documentation into structured JSON schemas (optional, requires `OPENAI_API_KEY`).
-   **Configurable Analysis:** Fine-tune the analysis for speed or exhaustiveness with a simple feature flag system.
-   **(Optional) Deep Graph Analysis:** Build a complete in-memory graph of all code relationships for advanced queries (requires `--enable-graph-analysis`).

## Installation

First, ensure you have Python 3.9+ installed.

```bash
# Clone the repository
git clone https://github.com/yab-tad/MapCoDoc.git
cd MapCoDoc

# Create and activate a virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install the package in editable mode with development dependencies
pip install -e ".[dev]"

# Set up environment variables (see Configuration section below)
cp .env.example .env
# Edit .env with your API keys
```

## Configuration

### Environment Variables

MapCoDoc uses environment variables for API keys and optional settings. Create a `.env` file in the project root:

```bash
# Copy the example file
cp .env.example .env

# Edit with your values
nano .env  # or use your preferred editor
```

#### Required for Full Pipeline

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key for LLM-based documentation structuring. Get yours at [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |

**Note:** If `OPENAI_API_KEY` is not set, the LLM structuring step (Step 5) is automatically skipped and raw documentation text is stored instead.

#### Optional Settings

| Variable | Description |
|----------|-------------|
| `MAPCODOC_PROXIES` | Proxy configuration for web doc crawling. Format: `http://host:port` or comma-separated list |

Example `.env` file:

```bash
# Required for LLM documentation structuring
OPENAI_API_KEY=sk-your-api-key-here

# Optional: Proxy for web crawling (if behind firewall)
MAPCODOC_PROXIES=http://proxy.example.com:8080
```

See `.env.example` for all available options including feature flag overrides.

## Quick Start

The `mapcodoc` CLI provides several commands for analysis and documentation processing.

### 1. Code Analysis Only

Analyze a repository and resolve its public API paths:

```bash
python -m cli.main analyze ./path/to/your/repo \
    --output results.json 
    --project-name "project_name"
    --project-version "v1.0"
    --pypi-package-name "project installer name"
```

Key feature flags (`CHAIN_CANDIDATE_COLLECTION`, `API_BOUNDARY_DETECTION`, `ADVANCED_EXPORT_HEURISTICS`) are enabled by default. Graph-based analysis and dynamic `__all__` evaluation are disabled by default for performance.

Optional (--project-name --project-version --pypi-package-name) but improve accuracy for some packages.

### 2. End-to-End Analysis (Code + Documentation)

Perform full analysis including documentation extraction and trace-link creation:

```bash
# With web documentation
python -m cli.main analyze ./path/to/your/repo \
    --output results.json \
    --doc-source "https://docs.yourlibrary.com/stable/api/..."
    --project-name "project_name"
    --project-version "v1.0"
    --pypi-package-name "project installer name"

# With PDF documentation
python -m cli.main analyze ./path/to/your/repo \
    --output results.json \
    --doc-source "./docs/api-reference.pdf"

# Skip LLM processing (no OPENAI_API_KEY required)
python -m cli.main analyze ./path/to/your/repo \
    --doc-source "https://docs.yourlibrary.com/stable/api/..." \
    --skip-llm
```

The repo path provided can either be your GitHub repo link or local path to your codebase.

The doc-source input for a web-doc should be the full URL of a module member's (class, function) API reference documentation. If the source is PDF, the link provided should be either a downloadable link or local path to the file.


### 3. Standalone Documentation Extraction

Extract documentation for an existing database:

```bash
python -m cli.main extract-docs \
    --db-path mapcodoc_output/mylib_1.0.db \
    --library-name mylib \
    --version 1.0 \
    --doc-source "https://docs.mylib.com/api/..."
```

### 4. Feature Flag Management

```bash
# List all feature flags and their current states
python -m cli.main list-features

# Save feature flag states to file
python -m cli.main save-features --output my_flags.json
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `analyze` | Analyze Python repository, ingest to database, optionally process docs |
| `extract-docs` | Standalone documentation extraction for existing database |
| `list-features` | Display all feature flags and their current states |
| `save-features` | Save current feature flag states to JSON file |

## CLI Arguments (analyze)

| Argument | Description |
|----------|-------------|
| `repository_paths` | Path(s) to Python repository to analyze (positional, required) |
| `--output`, `-o` | Output JSON file path (default: not saved if omitted) |
| `--config-file` | Path to a YAML or JSON AnalysisConfig file |
| `--doc-source` | URL or PDF path for documentation extraction |
| `--target-module` | Module prefix filter for documentation (default: auto-detect from library) |
| `--skip-llm` | Skip LLM-based structured extraction |
| `--project-name` | Override auto-detected project/library name |
| `--project-version` | Override auto-detected project version |
| `--pypi-package-name` | PyPI package name if different from project name |
| `--enable-dynamic-all` | Enable dynamic `__all__` evaluation (default: disabled) |
| `--enable-graph-analysis` | Enable graph-based fallback resolution (default: disabled) |
| `--enable-call-graph` | Enable call graph analysis (requires `--enable-graph-analysis`) |
| `--enable-chain-candidates` | Enable re-export chain candidate collection (default: enabled) |
| `--enable-api-boundaries` | Enable heuristic boundary scoring (default: enabled) |
| `--enable-advanced-exports` | Enable advanced export heuristics (default: enabled) |
| `--enable-watch-mode` | Enable incremental watch mode |
| `--auto-install-dependencies` | Install project dependencies for dynamic analysis |
| `--log-level` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `--log-file` | Path to log output to a file |

## Output

MapCoDoc produces several outputs:

1. **SQLite Database** (`mapcodoc_output/{lib}_{version}.db`)
   - Modules, members, signatures, imports, exports
   - Inherited members with derived API names (for documentation linking)
   - Documentation fields (if processed)
   - Queryable via `QueryManager` API

2. **JSON Analysis Results** (`mapcodoc_analysis_results.json`)
   - Full analysis details per module
   - Export chains and resolved API paths
   - Inherited methods with source class tracking
   - Module statistics and metrics

3. **Structured Documentation** (`doc_processor/doc_artifacts/`)
   - Per-member documentation files (including inherited members)
   - Structured JSON schemas (if LLM enabled)

## Programmatic API

```python
from code_analysis import MapCoDocRegistry, AnalysisConfig
from code_analysis.feature_flags import Feature, enable
from mapcodoc_db.db_manager import MapCoDocDB
from mapcodoc_db.query import QueryManager
from doc_processor.doc_runner import DocProcessingRunner

# Core feature flags (CHAIN_CANDIDATE_COLLECTION, API_BOUNDARY_DETECTION, 
# ADVANCED_EXPORT_HEURISTICS) are enabled by default.
# Optionally enable additional features:
enable(Feature.DYNAMIC_ALL_EVALUATION)  # For libraries with dynamic __all__ (default: disabled)
enable(Feature.GRAPH_ANALYSIS)  # For graph-based fallback (default: disabled)

# Initialize registry and run code analysis
registry = MapCoDocRegistry(
    repo_path="path/to/your/repo",
    auto_init=True
)

analyzer = registry.get_component("analyzer_integration")
results = analyzer.analyze_codebase("path/to/your/repo")

# Ingest results into database
db = MapCoDocDB("mapcodoc_output/mylib_1.0.db")
db.init_db(reset=True)
db.ingest_analysis_results(results['analysis_details'])

# Query the database
session = db.get_session()
qm = QueryManager(session)

# Get all public members
members = qm.get_all_public_members()
for m in members:
    print(f"{m.fqn} -> API: {m.api_name}")

# Query inherited members (for inherited method documentation linking)
# Example: find "xgboost.XGBRFClassifier.evals_result" even though
# evals_result is defined in XGBModel
result = qm.find_member_by_any_path("xgboost.XGBRFClassifier.evals_result")
if result:
    if result['type'] == 'inherited':
        print(f"Inherited from: {result['member'].source_class_fqn}")

# Get all inherited members for a class
inherited = qm.get_inherited_members_for_class("mylib.ChildClass")
for im in inherited:
    print(f"{im.member_name}: {im.inherited_api_name}")

# Process documentation (includes inherited members automatically)
doc_runner = DocProcessingRunner(
    db_path="mapcodoc_output/mylib_1.0.db",
    library_name="mylib",
    version="1.0"
)
doc_runner.run(
    doc_source="https://docs.mylib.com/api/...",
    skip_llm=False  # Set True to skip LLM processing
)

# Query documentation
member_doc = qm.get_member_documentation("mylib.MyClass")
print(f"Description: {member_doc.description}")
```

## Environment Variables

See the [Configuration](#configuration) section for detailed setup instructions. Create a `.env` file from `.env.example`:

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key for LLM-based documentation structuring |
| `MAPCODOC_PROXIES` | Proxy configuration for web doc crawling (optional) |
| `MAPCODOC_FEATURE_*` | Feature flag overrides (e.g., `MAPCODOC_FEATURE_GRAPH_ANALYSIS=1`) |

**Feature Flag Defaults:**
| Flag | Default |
|------|---------|
| `CHAIN_CANDIDATE_COLLECTION` | Enabled |
| `API_BOUNDARY_DETECTION` | Enabled |
| `ADVANCED_EXPORT_HEURISTICS` | Enabled |
| `DYNAMIC_ALL_EVALUATION` | Disabled |
| `GRAPH_ANALYSIS` | Disabled |
| `CALL_GRAPH_ANALYSIS` | Disabled |
| `INCREMENTAL_WATCH_MODE` | Disabled |

## Documentation

- **[Workflow](docs/workflow.md)** – End-to-end pipeline workflow with diagrams
- **[API Reference](docs/api_reference.md)** – Component API documentation
- **[Feature Flags](docs/features.md)** – Feature flag system and configurations
- **[Events](docs/events.md)** – Event system for component communication
- **[Code Analysis](code_analysis/README.md)** – Code analysis module, workflow phases, and inheritance resolution
- **[Doc Processor](doc_processor/README.md)** – Documentation processing module details
- **[Database](mapcodoc_db/README.md)** – Database schema, inherited member queries, and ingestion

## Development

### Testing

```bash
# Run all tests
pytest

# Run tests with code coverage
pytest --cov=code_analysis
```

### Code Quality

This project uses `black` for formatting, `isort` for imports, and `mypy` for type checking.

```bash
# Format code
black .

# Sort imports
isort .

# Type check
mypy .
```

## License


