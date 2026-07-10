# MapCoDoc API Reference

This document provides an API reference for the key components involved in the code analysis, database management, and documentation processing parts of the MapCoDoc pipeline.

---

## Core Analysis Orchestration & Data Flow

The analysis process involves several key components working together in a tiered fashion. The usage of graph-based components is optional and controlled by the `GRAPH_ANALYSIS` feature flag.

1.  **`CodeVisitor` & `DynamicAnalyzer`**: These are the primary data extractors. They parse individual Python files to produce a rich set of `analysis_results`, including definitions, import records, export records, and module statistics, without needing a graph.

2.  **`AnalyzerIntegration`**: This is the central orchestrator.
    *   It manages the analysis of all files, calling the data extractors.
    *   It merges the static and dynamic results for each file.
    *   It performs "on-the-fly" and "final" linking of unresolved re-exports by directly using the collected `analysis_results`.
    *   It aggregates all final statistics and identifies "chain candidates."
    *   Crucially, it drives the tiered API path resolution process.

3.  **`APIPathResolver`**: The primary engine for API path resolution.
    *   **Fast Path (Default):** It contains the primary, high-speed, graph-less resolution algorithm (`resolve_chains_via_direct_lookup`). This "Guided Virtual Graph Trace" works by directly querying the `analysis_results` maps provided by `AnalyzerIntegration`.
    *   **Scoring & Selection:** It is the final authority for scoring all found export chains (whether from the fast path or the graph-based fallback) and selecting the best one.

4.  **Graph-based Components (Optional - If `Feature.GRAPH_ANALYSIS` is `True`)**
    *   **`GraphStore`**: The underlying graph database (using NetworkX) that gets populated with all code relationships (imports, exports, etc.). It is not used by the Fast Path but is essential for the fallback and other analyses.
    *   **`Trackers` (`ImportTracker`, `ExportTracker`, etc.)**: Specialized classes that populate the `GraphStore`.
    *   **`GraphTraversal`**: Provides graph traversal algorithms that operate on the `GraphStore`. It contains the "Guided Graph Trace" and "Exhaustive Search" methods used as **fallbacks** if the Fast Path in `APIPathResolver` fails.

---

## Code Analysis Components

### 1. `CodeVisitor`
*(Located in `code_analysis/code_visitor.py`)*

Parses a single Python source file using the `ast` module to extract a detailed, static view of its contents.

*   **Key Method:** `analyze_code(code, module_name, ...)` (called by `AnalyzerIntegration`).
*   **Responsibilities:**
    *   Traverses the AST to identify class, function, method, and variable definitions.
    *   Records all import statements as `ImportRecord` objects, including regular, alias, and wildcard imports.
    *   Determines exports based on the `__all__` list or public naming conventions.
    *   For each export, it attempts to determine if it's a re-export of an imported item. If the origin cannot be determined statically (e.g., from a wildcard import), the `ExportRecord` is flagged with `needs_linking: True`.
    *   Calculates raw `module_statistics` based on the static content (e.g., import/export counts, `is_init_file`, `has_docstring`).
*   **Output (per file):** A dictionary containing all extracted information, which becomes the foundation for all subsequent analysis.

### 2. `DynamicAnalyzer`
*(Located in `code_analysis/dynamic_analyzer.py`)*

Executes Python modules in an isolated environment to discover runtime behaviors that static analysis cannot see.

*   **Key Method:** `evaluate_module_exports(self, module_abs_path: str, static_info: Dict[str, Any]) -> Optional[Dict[str, Any]]`
    *   Called by `AnalyzerIntegration` when a module is flagged as needing dynamic analysis.
    *   `static_info` provides essential context from `CodeVisitor` (e.g., static imports) to help the dynamic script interpret the runtime environment accurately.
*   **Key Enhancements:**
    *   **Monkey-patches `importlib.import_module`** to intercept and record any dynamic imports that occur at runtime.
    *   Uses the provided static context and runtime introspection (`inspect`, `__module__`, etc.) to produce a definitive list of `discovered_exports` with their true origins, even for items imported dynamically or via wildcards.
    *   Returns a list of all module FQNs that were actually imported at runtime, allowing `AnalyzerIntegration` to filter out unused conditional static imports.

### 3. `APIPathResolver`
*(Located in `code_analysis/api_resolver.py`)*

The primary engine for API path resolution. It contains the high-speed "Fast Path" logic and the final scoring and selection heuristics.

*   **Key Methods:**
    *   **`resolve_chains_via_direct_lookup(...)` (Tier 1 - Fast Path)**: This is the primary resolution algorithm. It performs the "Guided Virtual Graph Trace" to find export chains by directly querying the `analysis_results` maps provided by `AnalyzerIntegration`. It does **not** use `GraphStore` and is therefore very fast and memory-efficient.
    *   **`determine_best_api_path_for_candidate(...)`**: This method takes a list of potential export chains (found by either the Fast Path or a graph-based fallback) and uses scoring heuristics to select the single best chain and determine the final API path.
    *   **`_score_single_chain(...)`**: The internal method that calculates a score for a given chain, considering factors like chain length, module depth, and module characteristics (e.g., if it's an `__init__.py`). Its behavior is influenced by the `API_BOUNDARY_DETECTION` and `ADVANCED_EXPORT_HEURISTICS` feature flags.
    *   **`_determine_target_module_for_candidate(...)`**: A helper used to intelligently select the best target module for the graph-based fallback searches, based on a scoring of the known re-exporting modules.

### 4. `AnalyzerIntegration`
*(Located in `code_analysis/analyzers/analyzer_integration.py`)*

The central orchestrator that manages the entire analysis pipeline and drives the tiered resolution strategy.

*   **Key Responsibilities & Methods:**
    *   **`__init__(...)`**: Conditionally instantiates graph-based components (`GraphStore`, `Trackers`, `GraphTraversal`) only if `Feature.GRAPH_ANALYSIS` is enabled.
    *   **`analyze_file(...)`**: Manages the static analysis, optional dynamic analysis, and on-the-fly linking for a single file.
    *   **`analyze_codebase(...)`**: The main entry point. It orchestrates the file analysis loop, the final linking phase, and the API resolution phase.
    *   **`drive_api_path_resolution(...)`**: This method contains the core tiered logic:
        1.  It first calls `APIPathResolver` to attempt resolution via the **Tier 1 Fast Path**.
        2.  If that fails, and if `GRAPH_ANALYSIS` is enabled, it proceeds to **Tier 2** by calling the `find_export_chains_guided_graph` method from its `GraphTraversal` instance.
        3.  If Tier 2 fails, it can proceed to **Tier 3** by calling the exhaustive `find_export_chains` method from `GraphTraversal`.
        4.  It passes the chains from whichever tier was successful to `APIPathResolver` for final scoring.

### 5. `GraphTraversal`
*(Located in `code_analysis/graph/traversal.py`)*

Provides graph traversal algorithms that operate on the `GraphStore`. **This component and its methods are only available if `Feature.GRAPH_ANALYSIS` is enabled.** They serve as the fallback for API path resolution.

*   **Key Methods for API Resolution Fallback:**
    *   **`find_export_chains_guided_graph(...)` (Tier 2 Fallback)**: Performs the "Guided Graph Trace." It is much faster than an exhaustive search because it limits its queries on the `GraphStore` to a small, pre-computed set of known re-exporting modules.
    *   **`find_export_chains(...)` (Tier 3 Fallback)**: Performs a classic, exhaustive, one-way Breadth-First Search on the `GraphStore`. It is the most robust but slowest method, serving as the ultimate safety net.

*   **Other Utilities:** `find_shortest_path`, `find_cycles`, etc., for other potential downstream analyses that require the populated graph.

### 6. `DefinitionRegistry`
*(Located in `code_analysis/definition_registry.py`)*

Acts as the authoritative source of truth for all code definitions found during the analysis.

*   **`register_definition(...)`**: Called by `CodeVisitor` for each class, function, or method found.
*   **`DEFINED_IN` Relationship**: If `GRAPH_ANALYSIS` is enabled, this method is also responsible for adding the crucial `DEFINED_IN` edge to the `GraphStore`, linking a component FQN to the module FQN where it is defined. This edge is essential for all export chain analyses.

---

## Database Components

### 7. `MapCoDocDB`
*(Located in `mapcodoc_db/db_manager.py`)*

Manages the SQLite database for persistent storage of analysis results.

*   **Key Methods:**
    *   **`init_db(reset: bool = False)`**: Creates all tables. If `reset=True`, deletes existing DB first.
    *   **`ingest_analysis_results(analysis_data: Dict)`**: Ingests the full analysis output including modules, members, imports, and exports.
    *   **`ingest_documentation_results(doc_results: Dict)`**: Updates member records with structured documentation.
    *   **`get_session()`**: Returns a SQLAlchemy session for queries.

*   **Database Models (in `mapcodoc_db/db_models.py`):**
    *   **`DBModule`**: Python modules with path, name, statistics
    *   **`DBMember`**: Classes, functions, methods, variables with API names, signatures, docstrings, documentation
    *   **`DBSignature`**: Signature variants for each member
    *   **`DBImport`**: Import relationships
    *   **`DBExport`**: Export relationships

### 8. `QueryManager`
*(Located in `mapcodoc_db/query.py`)*

High-level interface for querying the database with common patterns.

*   **Key Methods:**
    *   **Module Queries:**
        *   `get_module_details(module_fqn)` → `ModuleDetails`
        *   `get_packages()` → List of package module names
    *   **Member Queries:**
        *   `get_member_details(member_fqn)` → `MemberDetails`
        *   `get_member_by_api_name(api_name)` → `MemberDetails`
        *   `get_class_methods(class_fqn)` → List of methods
        *   `get_members_by_type(member_type)` → List of members
        *   `get_all_public_members()` → All public members
        *   `search_members(query, limit)` → Fuzzy search
    *   **Documentation Queries:**
        *   `get_member_documentation(fqn)` → `MemberDocumentation`
        *   `get_documentation_coverage()` → Coverage statistics
    *   **Relationship Queries:**
        *   `get_public_peers(module_fqn)` → Exports with signatures
        *   `get_module_imports(module_fqn)` → Import records
        *   `get_reverse_dependencies(module_fqn)` → Who imports this module

---

## Documentation Processing Components

### 9. `DocProcessingRunner`
*(Located in `doc_processor/doc_runner.py`)*

Orchestrates the entire documentation extraction workflow.

*   **Constructor:**
    ```python
    runner = DocProcessingRunner(
        db_path="mapcodoc.db",
        library_name="torch",
        version="2"
    )
    ```

*   **Key Method:**
    ```python
    runner.run(
        doc_source="https://pytorch.org/docs/stable/",  # URL or PDF path
        target_module="torch.nn",  # Optional: filter by module prefix
        skip_llm=False  # Optional: skip LLM structuring
    )
    ```

*   **Directory Structure Created:**
    ```
    doc_processor/doc_artifacts/
    ├── crawled_URLs/{lib}/{version}/     # Discovered URLs
    ├── local_doc/{lib}/{version}/        # Local/downloaded PDFs
    ├── scraped_doc/{lib}/{version}/      # Raw extracted text
    │   ├── per_member/                   # Individual API docs
    │   ├── per_module/                   # Combined module/class docs
    │   ├── per_page/                     # Single-page all-API docs
    │   └── combined/                     # Relocated combined docs
    ├── preprocessed_doc/{lib}/{version}/ # URL placeholders applied
    ├── structured_doc/{lib}/{version}/   # LLM-structured JSONs
    └── postprocessed_doc/{lib}/{version}/ # Final JSONs with URLs
    ```

### 10. `WebMemberExtractor`
*(Located in `doc_processor/filter_doc.py`)*

Two-stage (lexical + semantic) member extraction for web documentation.

*   **Constructor:**
    ```python
    from doc_processor.filter_doc import WebMemberExtractor, WebMemberInfo
    from doc_processor.file_doc.extraction_utils import MemberExtractorConfig
    
    cfg = MemberExtractorConfig(semantic_mode="auto")
    extractor = WebMemberExtractor(cfg, embedder)
    ```

*   **Key Method:**
    ```python
    pos, score, match_type = extractor.find_anchor_position(
        combined_text,
        member_info,
        model_name
    )
    ```

*   **Features:**
    *   Statistical confidence gating via `_should_use_semantic_member`
    *   Adaptive length penalty for varying document sizes
    *   Two-stage semantic search (coarse window → fine anchor)

### 11. `StopSignalMatcher`
*(Located in `doc_processor/filter_doc.py`)*

Type-aware boundary detection for extracting individual member docs from combined pages.

*   **Constructor:**
    ```python
    from doc_processor.filter_doc import StopSignalMatcher
    
    matcher = StopSignalMatcher(
        peer_signatures=["Conv2d(", "class torch.nn.Conv2d("],
        target_member_type="class",
        target_api_name="torch.nn.Conv1d"
    )
    ```

*   **Key Method:**
    ```python
    matched, is_high_priority = matcher.checks_stop(line)
    ```

*   **Features:**
    *   Content-based code example detection (not fence tracking)
    *   High-priority (real definition) vs low-priority (code example) signals
    *   Fallback pattern strategies for robustness

### 12. `MemberExtractor` (PDF)
*(Located in `doc_processor/file_doc/pipeline_pdf.py`)*

Extracts individual member documentation from PDF files using lexical + semantic search.

*   **Key Function:**
    ```python
    from doc_processor.file_doc.pipeline_pdf import extract_api_docs_from_pdf
    
    results = extract_api_docs_from_pdf(
        pdf_path="./docs/reference.pdf",
        members=member_inputs,
        output_dir="./scraped_doc/per_member/"
    )
    ```

### 13. `DocumentationExtractor` & `ConcurrentDocExtractor`
*(Located in `doc_processor/structured_doc_extracter.py`)*

LLM-based documentation structuring using `gpt-4.1-2025-04-14` (OpenAI Structured Outputs, strict JSON Schema).

*   **Single Document:**
    ```python
    from doc_processor.structured_doc_extracter import DocumentationExtractor
    
    extractor = DocumentationExtractor(
        MM_type="class",
        MM_signature="torch.nn.L1Loss(...)",
        scraped_doc_path="path/to/preprocessed.txt",
        api_key="your-openai-api-key"
    )
    extractor.extract_and_save_documentation()
    ```

*   **Batch Processing (Recommended):**
    ```python
    from doc_processor.structured_doc_extracter import ConcurrentDocExtractor
    
    extractor = ConcurrentDocExtractor(api_key, max_concurrent=10)
    results = await extractor.extract_batch(requests, progress_callback)
    ```

### 14. `MemberExtractorConfig`
*(Located in `doc_processor/file_doc/extraction_utils.py`)*

Shared configuration for extraction behavior in both PDF and web pipelines.

```python
from doc_processor.file_doc.extraction_utils import MemberExtractorConfig

cfg = MemberExtractorConfig(
    semantic_mode="auto",       # "auto", "never", "always", "only"
    lexical_sigma_k=0.25,       # Confidence strictness
    lexical_margin_min=0.20,    # Top-1 vs top-2 margin
    window_chars=3000,          # Semantic search window size
    window_stride=2000          # Window overlap
)
```

### 15. `build_lexical_needles`
*(Located in `doc_processor/file_doc/signature.py`)*

Generates tiered search patterns for member identification.

```python
from doc_processor.file_doc.signature import MemberInput, build_lexical_needles

member = MemberInput(
    api_name="torch.nn.Conv1d",
    signature_variants=["Conv1d(in_channels, out_channels, ...)"],
    member_type="class"
)

needles = build_lexical_needles(member)
# Returns: {
#     "exact": ["torch.nn.Conv1d(in_channels, ...", ...],
#     "prefix": ["Conv1d(", "torch.nn.Conv1d("],
#     "anchor": ["Conv1d", "torch.nn.Conv1d"]
# }
```
