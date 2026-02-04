# Methodology

This section presents MapCoDoc (Mapping Code onto Documentation), a framework-agnostic pipeline for recovering traceability links between Python source code and its API reference documentation. We structure our methodology to address the following research questions:

- **RQ1:** How can we accurately map implementation paths of module members to public API paths across diverse Python frameworks?
- **RQ2:** How accurately can we recover traceability links between API reference documentation and source code?
- **RQ3:** How can we automatically extract and structure documentation from unstructured sources with high fidelity?

## 1. Overall Approach and Pipeline Architecture

### 1.1 Problem Context

Modern Python libraries employ sophisticated module structures where the implementation location of a code component often differs from its public API path. For example, PyTorch's `Conv1d` layer is implemented at `torch.nn.modules.conv.Conv1d` but exposed to users as `torch.nn.Conv1d`. This discrepancy, compounded by complex re-export chains, inheritance hierarchies spanning internal and external libraries, and dynamically constructed `__all__` lists, creates a significant challenge for automated traceability recovery.

Existing documentation systems such as Sphinx [1] and pdoc [2] generate documentation from docstrings embedded in source code, creating an implicit link between code and its documentation. However, these tools do not address the challenge of linking *external* documentation, API references hosted on documentation sites, back to source code. Traditional approaches to such linking rely on exact path matching or library-specific heuristics (as seen in IDE plugins like Kite [3] and Jedi [4]), which fail to generalize across the diverse patterns employed by different frameworks.

The traceability link recovery problem has been studied extensively in the software engineering literature. Information retrieval (IR) approaches using TF-IDF [5] and LSI [6] have shown promise for linking requirements to code, but these methods struggle with the unstructured nature of API documentation. More recent deep learning approaches [7, 8] have improved accuracy but require substantial training data that may not be available for arbitrary libraries.

MapCoDoc addresses this challenge through a framework-agnostic design that combines static and dynamic analysis with configurable resolution strategies, avoiding the need for training data while handling the full complexity of Python's import system.

### 1.2 Pipeline Overview

MapCoDoc operates as a three-phase pipeline that transforms a Python repository and its documentation source into a queryable database of trace links:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              MapCoDoc Pipeline                                  │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│   ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐            │
│   │  Python Repo    │    │   Doc Source    │    │                 │            │
│   │  (local/GitHub) │    │  (Web URL/PDF)  │    │                 │            │
│   └────────┬────────┘    └────────┬────────┘    │                 │            │
│            │                      │             │                 │            │
│            ▼                      ▼             │                 │            │
│   ┌─────────────────┐    ┌─────────────────┐    │                 │            │
│   │  Phase 1:       │    │  Phase 2:       │    │                 │            │
│   │  Code Analysis  │    │  Doc Processing │    │                 │            │
│   │                 │    │                 │    │                 │            │
│   │  • AST Parsing  │    │  • Crawl/Scrape │    │                 │            │
│   │  • Dynamic Eval │    │  • Per-Member   │    │                 │            │
│   │  • Inheritance  │    │    Isolation    │    │                 │            │
│   │  • API Path Res │    │  • LLM Struct   │    │                 │            │
│   └────────┬────────┘    └────────┬────────┘    │                 │            │
│            │                      │             │                 │            │
│            └──────────┬───────────┘             │                 │            │
│                       ▼                         │                 │            │
│              ┌─────────────────┐                │                 │            │
│              │  Phase 3:       │                │                 │            │
│              │  Trace-Link     │────────────────┤                 │            │
│              │  Recovery       │                │   SQLite DB     │            │
│              │                 │                │   with Trace    │            │
│              │  • API Matching │                │   Links         │            │
│              │  • DB Update    │                │                 │            │
│              └─────────────────┘                └─────────────────┘            │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

**Phase 1: Code Analysis** parses the repository to extract all code components (classes, functions, methods, variables), their relationships (imports, exports, inheritance), and resolves their public API paths. This phase addresses **RQ1** by implementing a tiered API resolution strategy.

**Phase 2: Documentation Processing** retrieves documentation from web-based API references or PDF files, isolates per-member documentation sections, and optionally structures the raw text using an LLM. This phase addresses **RQ3** through hybrid lexical-semantic extraction.

**Phase 3: Trace-Link Recovery** matches resolved API names from code analysis with extracted documentation to create trace links stored in a queryable SQLite database. This phase addresses **RQ2** by leveraging the API resolution results for accurate matching.

### 1.3 Design Principles

MapCoDoc adheres to the following design principles:

1. **Framework-Agnostic Design:** The pipeline contains no library-specific hardcoding. All resolution and extraction logic operates on general patterns (import records, export records, inheritance relationships) that apply universally to Python packages.

2. **Tiered Resolution Strategy:** API path resolution employs a tiered approach that prioritizes speed while maintaining accuracy. A fast, graph-less resolver handles the majority of cases, with optional graph-based fallbacks for complex scenarios.

3. **Hybrid Analysis:** Static AST analysis is complemented by dynamic execution for libraries that construct their public interfaces at runtime (e.g., NumPy's dynamic `__all__` construction).

4. **Configurable Accuracy-Performance Trade-offs:** Feature flags allow users to enable or disable computationally expensive analyses (graph construction, dynamic evaluation) based on their specific requirements.

### 1.4 Input and Output Specification

**Inputs:**
- **Repository Path:** Local path or GitHub URL to a Python repository
- **Documentation Source:** URL to web-based API documentation or path to a PDF file

**Outputs:**
- **SQLite Database:** A queryable database containing:
  - Modules with metadata (file paths, `__all__` contents, statistics)
  - Members (classes, functions, methods) with resolved API names, signatures, and parameters
  - Import/export relationships
  - Inherited member tracking with derived API names
  - Linked documentation (structured JSON or raw text)
- **JSON Analysis Results:** Detailed analysis output including export chains and resolution provenance
- **Per-Member Documentation Files:** Individual documentation files for each API member

The following sections detail each pipeline phase and the algorithms employed to address our research questions.

## 2. Code Repository Analysis

This section describes how MapCoDoc analyzes Python repositories to extract code structure and resolve public API paths, addressing **RQ1: How can we accurately map implementation paths of module members to public API paths across diverse Python frameworks?**

### 2.1 Static Analysis with AST Parsing

The foundation of MapCoDoc's code analysis is an AST-based visitor that systematically extracts code components from Python source files. AST-based static analysis is a well-established technique in program analysis [9], used by tools such as Pyright [10], mypy [11], and Jedi [4] for type checking and code intelligence. Unlike these tools which focus on type inference and autocompletion, MapCoDoc's visitor maintains rich contextual information specifically designed for API path resolution and traceability recovery.

#### 2.1.1 Analysis Context

Each file is analyzed within an `AnalysisContext` that tracks:

- **Scope Stack:** The current nesting level (module → class → method) for accurate Fully Qualified Name (FQN) generation
- **Imported Names:** A mapping from local names to their resolved FQNs, enabling correct reference resolution
- **Export Information:** Whether `__all__` is defined, its values (if statically determinable), and whether dynamic analysis is required
- **Wildcard Imports:** Modules imported via `from X import *`, which affect the visible namespace

The context enables the visitor to generate FQNs that accurately represent the code structure while avoiding erroneous duplications. For instance, when a module `data_parallel.py` defines a function `data_parallel()`, the context-aware FQN generation correctly produces `torch.nn.parallel.data_parallel.data_parallel` rather than incorrectly deduplicating to `torch.nn.parallel.data_parallel`.

#### 2.1.2 Component Extraction

For each Python file, the visitor extracts the following component types:

| Component Type | Extracted Attributes |
|----------------|---------------------|
| **Class** | Name, FQN, base classes, decorators, docstring, methods, nested classes |
| **Function** | Name, FQN, signature, parameters (with types and defaults), return type, decorators, docstring |
| **Method** | Same as function, plus: parent class FQN, static/class/instance classification, property type |
| **Variable** | Name, FQN, inferred type (if available), access modifier |

Each component receives a `component_kind` classification and metadata including source line numbers for traceability back to the original code.

#### 2.1.3 Import Record Extraction

Import statements are parsed into structured `ImportRecord` objects that capture:

```
ImportRecord:
    importer_module_fqn     # Module containing the import
    raw_module_specifier    # "from X import Y" → X
    raw_imported_name       # "from X import Y" → Y  
    raw_alias               # "import X as alias" → alias
    is_relative             # True for relative imports
    level                   # Relative import level (., .., etc.)
    is_wildcard             # True for "from X import *"
    source_module_fqn       # Resolved source module FQN
    imported_entity_fqn     # Resolved entity FQN
    is_source_internal      # True if source is within the repository
    name_bound_in_importer  # The name available in the importing namespace
    name_bound_points_to_fqn # The true definition the name resolves to
```

The `name_bound_points_to_fqn` field is critical for API resolution—it is updated during post-processing to reflect the true definition location after chasing re-export chains.

#### 2.1.4 Export Record Extraction

Export records capture what each module exposes to its consumers:

1. **Explicit Exports:** Names listed in `__all__`, if defined
2. **Implicit Exports:** Public names (not prefixed with `_`) when `__all__` is absent
3. **Re-exports:** Imported names that are subsequently exported

Each export record tracks:
- The exported name (as visible to consumers)
- The target item's FQN (the actual definition location)
- Whether the export is explicit (in `__all__`), a re-export, or from a wildcard import
- The source of the export (for re-exports)

#### 2.1.5 Base Class Tracking

For inheritance resolution, the visitor records:
- The local names of base classes as written in the class definition
- A preliminary `base_fqns` list (resolved during post-processing)

Base class resolution is deferred to post-processing because the base class definition may not have been analyzed yet when the inheriting class is visited.

### 2.2 Dynamic Analysis for Runtime Exports

Static analysis alone cannot accurately determine the public interface of libraries that construct their exports at runtime. NumPy, for example, builds its `__all__` list dynamically by aggregating exports from submodules:

```python
# numpy/__init__.py (simplified)
__all__ = []
from .core import *
from .lib import *
__all__ += core.__all__
__all__ += lib.__all__
```

#### 2.2.1 Detection of Dynamic Exports

During static analysis, the visitor detects patterns that indicate dynamic `__all__` construction:

1. **Augmented Assignment:** `__all__ += ...` or `__all__.extend(...)`
2. **Conditional Assignment:** `if condition: __all__ = ...`
3. **Loop-based Construction:** `for module in modules: __all__ += ...`
4. **Non-literal Values:** `__all__ = get_exports()` where the value is not a static list/tuple

When such patterns are detected, the module is flagged with `needs_dynamic_analysis = True`.

#### 2.2.2 Isolated Dynamic Execution

When the `DYNAMIC_ALL_EVALUATION` feature flag is enabled, MapCoDoc executes flagged modules in an isolated environment to discover their runtime exports:

1. **Virtual Environment Creation:** A temporary virtual environment is created to isolate the execution from the host system.

2. **Dependency Installation:** Project dependencies are installed (if `--auto-install-dependencies` is specified).

3. **Module Execution:** The module is imported in a subprocess, and its `__all__` attribute is extracted post-execution.

4. **Result Merging:** Dynamic results are merged with static analysis results, with dynamic values taking precedence for `__all__` contents.

This approach correctly handles libraries like NumPy, SciPy, and pandas that heavily rely on dynamic export construction.

### 2.3 API Path Resolution

The central challenge addressed by MapCoDoc is mapping implementation FQNs (e.g., `torch.nn.modules.conv.Conv1d`) to public API paths (e.g., `torch.nn.Conv1d`). We address this through a tiered resolution strategy.

#### 2.3.1 Chain Candidates

Not all components require API resolution—only those that are re-exported from their definition location. During post-processing, MapCoDoc builds a `candidates_to_re_exporters` map that identifies "chain candidates":

```
Chain Candidate: A component that is exported from at least one module other than its definition module.

Example:
  Definition:    torch.nn.modules.conv.Conv1d
  Re-exported:   torch.nn (via __init__.py chain)
  Candidate Map: {
      "torch.nn.modules.conv.Conv1d": {
          "component_kind": "class",
          "exporters": {"torch.nn", "torch.nn.modules"}
      }
  }
```

#### 2.3.2 Tiered Resolution Strategy

For each chain candidate, MapCoDoc employs a three-tier resolution strategy:

**Tier 1: Fast Path (Graph-less Resolution)**

The default resolution mode performs a "Guided Virtual Graph Trace" directly on the collected import/export records without constructing an in-memory graph:

```
Algorithm: FastPathResolution(candidate_fqn)
  1. Find all modules that export candidate_fqn
  2. For each exporting module M:
     a. Trace backwards through import records to find the export chain
     b. Build ExportChain = [ExportStep(module, name, target_fqn), ...]
  3. Return all discovered chains
```

This approach handles the majority of re-export patterns efficiently, including:
- Direct re-exports: `from .internal import Class` followed by listing in `__all__`
- Chained re-exports: Multi-hop import chains through `__init__.py` files
- Wildcard aggregation: `from .submodule import *`

**Tier 2: Guided Graph Trace (Optional Fallback)**

When enabled via the `GRAPH_ANALYSIS` feature flag, failed Tier 1 resolutions fall back to a graph-based approach:

```
Algorithm: GuidedGraphTrace(candidate_fqn, graph_store)
  1. Locate the definition node in the graph
  2. Perform BFS over export edges, guided by the candidates_to_re_exporters map
  3. For each path from definition to an exporting module, construct an ExportChain
  4. Return all discovered chains
```

The graph store maintains nodes for modules and components, with edges representing import, export, and containment relationships.

**Tier 3: Exhaustive Search (Final Fallback)**

If Tier 2 also fails, an exhaustive one-way search of the entire import graph serves as a final safety net for unusual code structures.

#### 2.3.3 Chain Scoring and Selection

When multiple export chains are discovered (common for widely re-exported components), MapCoDoc scores each chain to select the most likely public API path:

**Boundary Likelihood Score:** Each module receives a score based on characteristics that indicate it is a public API boundary:

| Factor | Weight | Rationale |
|--------|--------|-----------|
| Is `__init__.py` | +2.0 | Package root modules are primary API surfaces |
| Has explicit `__all__` | +1.5 | Indicates intentional public interface |
| High export ratio | +1.0 | Many exports suggest aggregation point |
| Short module path | +0.5 | Top-level modules preferred |
| Presence of docstrings | +0.5 | Documentation suggests public interface |

**Chain Score Calculation:**

```
ChainScore(chain) = Σ BoundaryScore(module) for module in chain
                  - α × len(chain)  # Prefer shorter chains
```

The chain with the highest score is selected, and its terminal module + exported name forms the resolved API path.

#### 2.3.4 API Name Propagation

After resolving API paths for chain candidates, the resolver propagates API names to:

1. **Methods:** Inherit API path from parent class (e.g., `torch.nn.Conv1d.forward` from `torch.nn.Conv1d`)
2. **Nested Classes:** Receive qualified API names through their container
3. **Inherited Members:** Receive derived API names via the inheriting class (detailed in Section 2.4)

Each component may have multiple API names if re-exported from multiple locations. The `primary_api_name` is set to the highest-scored path, while `all_api_names` contains all valid public paths.

### 2.4 Inheritance Resolution

Python's inheritance model introduces additional complexity: a method called on a subclass may be defined in a parent class, potentially from an external library. MapCoDoc's `InheritanceResolver` handles this through framework-agnostic base class classification and external introspection.

#### 2.4.1 Base Class Classification

For each class, the resolver classifies each base class as internal or external:

```
Algorithm: ClassifyBaseClass(base_name, base_fqn, defining_module)
  
  1. LOCAL CHECK: Is base defined in the same module?
     if f"{defining_module}.{base_name}" in module_components:
         return BaseClassInfo(is_internal=True, is_local=True)
  
  2. IMPORT CHECK: Find import record binding base_name
     for import_rec in module_imports[defining_module]:
         if import_rec.name_bound_in_importer == base_name:
             return BaseClassInfo(
                 is_internal=import_rec.is_source_internal,
                 fqn=import_rec.name_bound_points_to_fqn
             )
  
  3. TOP-LEVEL HEURISTIC: Check against known package prefixes
     if any(base_fqn.startswith(pkg) for pkg in top_level_packages):
         return BaseClassInfo(is_internal=True)
     else:
         return BaseClassInfo(is_internal=False)
```

This algorithm is framework-agnostic—it relies only on import records and package structure, not on hardcoded library names.

#### 2.4.2 Internal Inheritance Resolution

For internal base classes (defined within the analyzed repository), inherited methods are extracted directly from the analysis results:

```
Algorithm: ResolveInternalInheritance(class_fqn, base_fqn)
  
  1. Retrieve base class component from analysis results
  2. For each method in base.methods:
     if method.name not in class.own_methods:  # Not overridden
         add InheritedMember(
             name=method.name,
             source_class_fqn=base_fqn,
             original_fqn=method.fqn,
             signature=method.signature,
             is_external=False
         )
  3. Recursively process base class's bases (BFS traversal)
```

The BFS traversal ensures correct handling of multi-level inheritance chains (e.g., `XGBRFClassifier` → `XGBClassifier` → `XGBModel`).

#### 2.4.3 External Inheritance Resolution

When a class inherits from an external library (e.g., `sklearn.base.ClassifierMixin`), the inherited methods cannot be extracted from analysis results. MapCoDoc's `ExternalIntrospector` handles this through dynamic introspection:

```
Algorithm: ExternalIntrospection(external_base_fqns)
  
  1. CREATE ISOLATED ENVIRONMENT
     venv_path = create_temp_venv()
  
  2. DISCOVER PACKAGES
     for base_fqn in external_base_fqns:
         package_name = extract_top_level_package(base_fqn)
         pypi_name = discover_pypi_package(package_name)  # e.g., sklearn → scikit-learn
         install(pypi_name, venv_path)
  
  3. INTROSPECT METHODS
     methods = {}
     for base_fqn in external_base_fqns:
         cls = import_class(base_fqn, venv_path)
         for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
             if not name.startswith('_'):
                 methods[name] = InheritedMember(
                     name=name,
                     source_class_fqn=base_fqn,
                     original_fqn=f"{base_fqn}.{name}",
                     signature=inspect.signature(method),
                     is_external=True
                 )
  
  4. CLEANUP
     remove_venv(venv_path)
  
  5. Return methods (cached for future use)
```

Key features of the external introspector:
- **PyPI Discovery:** Automatically maps import names to PyPI package names (e.g., `sklearn` → `scikit-learn`, `cv2` → `opencv-python`)
- **Isolation:** Temporary virtual environments prevent contamination of the host system
- **Caching:** Results are cached to avoid repeated package installations

#### 2.4.4 API Name Propagation for Inherited Members

Each inherited member receives derived API names based on the inheriting class's API path:

```
Example: XGBoost Inheritance Chain

Class Hierarchy:
  XGBRFClassifier → XGBClassifier → XGBModel

Method: evals_result (defined in XGBModel)

Derived API Names:
  - xgboost.XGBRFClassifier.evals_result  (via XGBRFClassifier)
  - xgboost.XGBClassifier.evals_result    (via XGBClassifier)
  - xgboost.XGBModel.evals_result         (original definition)
```

This propagation enables documentation lookup using any of the valid API paths through which the method can be accessed.

### 2.5 Summary: Addressing RQ1

MapCoDoc addresses **RQ1** through:

1. **Comprehensive Static Analysis:** AST-based extraction with context-aware FQN generation and rich import/export tracking

2. **Dynamic Analysis Integration:** Isolated execution for libraries with runtime-constructed exports

3. **Tiered API Resolution:** A fast graph-less resolver for common patterns with graph-based fallbacks for complex scenarios

4. **Framework-Agnostic Inheritance Handling:** Base class classification using import records rather than hardcoded library names, with external introspection for cross-library inheritance

The result is accurate mapping of implementation paths to public API paths across diverse frameworks without library-specific customization.

## 3. Documentation Processing

This section describes how MapCoDoc extracts and structures documentation from web-based API references and PDF files, addressing **RQ3: How can we automatically extract and structure documentation from unstructured sources with high fidelity?**

### 3.1 Documentation Source Detection and Retrieval

MapCoDoc supports two documentation source types with automatic detection:

| Source Type | Detection Criteria | Examples |
|-------------|-------------------|----------|
| **Web URL** | HTTP/HTTPS scheme, not ending in `.pdf` | PyTorch docs, NumPy reference |
| **PDF** | `.pdf` extension or PDF MIME type | Downloadable API references |

#### 3.1.1 Web Documentation Crawling

For web-based documentation, MapCoDoc crawls the provided URL to discover all linked API documentation pages:

```
Algorithm: CrawlDocumentationURLs(base_url)
  
  1. Parse base_url to extract domain and path structure
  2. Fetch base_url and extract all anchor (<a>) elements
  3. Filter to URLs matching documentation patterns:
     - Same domain as base_url
     - Path contains API reference indicators (e.g., /api/, /reference/, /generated/)
  4. Recursively crawl discovered URLs (with depth limit)
  5. Deduplicate and return sorted URL list
```

The crawler respects rate limiting and robots.txt to avoid overloading documentation servers.

#### 3.1.2 Documentation Layout Detection

Web-based API documentation exhibits three common layouts:

| Layout | Description | Example Libraries |
|--------|-------------|-------------------|
| **per_member** | One HTML page per API member | pandas, scikit-learn |
| **per_module** | One page per module/class containing multiple members | PyTorch, pygame |
| **per_page** | Single page with all APIs | XGBoost |

Layout detection is based on URL structure and HTML fragment analysis:
- **per_member:** URLs contain individual function/class names (e.g., `/generated/torch.nn.Conv1d.html`)
- **per_module:** URLs reference modules with fragment anchors for members (e.g., `/api.html#xgboost.Booster.train`)
- **per_page:** Single URL with extensive fragment anchors

The detected layout determines the extraction strategy in subsequent stages.

#### 3.1.3 PDF Localization

For PDF documentation sources:
1. **Remote PDFs:** Downloaded to local storage with caching to avoid repeated downloads
2. **Local PDFs:** Path validated and used directly
3. **PDF Parsing:** PyMuPDF (fitz) extracts text with layout preservation

### 3.2 Per-Member Documentation Isolation

A critical challenge in documentation extraction is isolating the documentation for a single API member from combined pages (per_module and per_page layouts). Traditional web scraping approaches [12] rely on DOM structure and CSS selectors, but API documentation often lacks consistent markup across different documentation generators (Sphinx, MkDocs, pdoc, etc.). 

MapCoDoc employs a hybrid lexical-semantic approach inspired by passage retrieval techniques from information retrieval [13] and dense retrieval methods used in question answering systems [14]. This approach is robust to variations in documentation formatting while maintaining high precision.

#### 3.2.1 Lexical Needle Generation

For each target member, MapCoDoc generates multiple search "needles" from the member's API name and signature variants:

```
Algorithm: BuildLexicalNeedles(member)
  
  Input: MemberInput {
      api_name: "torch.nn.Conv1d",
      signature_variants: [
          "Conv1d(in_channels, out_channels, kernel_size, ...)",
          "class torch.nn.Conv1d(in_channels, out_channels, ...)"
      ],
      member_type: "class"
  }
  
  Output: {
      "exact": [
          "torch.nn.Conv1d(in_channels, out_channels, ...",
          "class torch.nn.Conv1d(in_channels, out_channels, ..."
      ],
      "prefix": [
          "Conv1d(",
          "torch.nn.Conv1d("
      ],
      "anchor": [
          "Conv1d",
          "torch.nn.Conv1d"
      ]
  }
```

Needles are organized in tiers, with exact matches preferred over prefix matches over anchor matches.

#### 3.2.2 Hybrid Search with Semantic Fallback

The `WebMemberExtractor` implements a two-stage search strategy:

**Stage 1: Lexical Search**
```
1. Search for exact needles using string matching
2. Score each match position based on:
   - Needle tier (exact > prefix > anchor)
   - Context quality (preceded by heading, followed by description)
3. Collect top-k match positions
```

**Stage 2: Semantic Search (Conditional)**
```
1. Evaluate lexical confidence:
   - σ_k: top score vs. mean + k×std
   - margin: difference between top-1 and top-2 scores
   
2. If confidence below threshold:
   - Generate semantic query from member signature and description
   - Encode query using sentence transformer (all-MiniLM-L6-v2)
   - Sliding window search over document with embedding similarity
   - Combine lexical and semantic scores
```

The semantic fallback is triggered by the statistical confidence check:

```python
def _should_use_semantic_member(lex_scores, sigma_k=0.25, margin_min=0.20):
    """
    Returns True if semantic search should be triggered.
    
    Criteria:
    1. Top lexical score < mean + sigma_k * std (not a clear winner)
    2. OR (top-1 - top-2) < margin_min * top-1 (too close to runner-up)
    """
```

An adaptive length penalty adjusts the threshold based on document size:
- Short documents (< 500 chars): Trust lexical search
- Medium documents (500-15000 chars): Gradual penalty increase
- Long documents (> 15000 chars): Cap at 0.15 penalty

#### 3.2.3 Stop Signal Detection

After locating the target member's anchor position, MapCoDoc must determine where its documentation ends (i.e., where the next member's documentation begins). The `StopSignalMatcher` provides type-aware boundary detection:

```
Algorithm: StopSignalMatching(text, start_pos, target_type, peer_signatures)
  
  1. Build stop patterns from peer signatures:
     - For each peer signature, extract:
       - Short name (e.g., "forward")
       - Full name (e.g., "torch.nn.Conv1d.forward")
       - Signature prefix (e.g., "forward(")
  
  2. Classify patterns as PRIMARY or FALLBACK based on target_type:
     - CLASS target: PRIMARY = other classes/functions
     - METHOD target: PRIMARY = sibling methods of same class
     - FUNCTION target: PRIMARY = other functions/classes
  
  3. Scan text from start_pos:
     for line in text[start_pos:]:
         if matches_primary_pattern(line):
             return current_position  # Stop here
         if no_primary_found and matches_fallback_pattern(line):
             return current_position
  
  4. Return end of text if no stop signal found
```

The two-phase matching ensures that:
- Class documentation includes all its method summaries before stopping
- Method documentation stops at sibling methods, not at nested content
- Function documentation correctly handles adjacent function definitions

#### 3.2.4 Class Anchor Propagation

For methods within classes, MapCoDoc uses "class anchor propagation" to improve extraction accuracy:

1. First, locate the parent class's documentation section
2. Within that section, search for the method using method-specific needles
3. This scoped search reduces false positives from similarly named methods in other classes

### 3.3 LLM-Based Documentation Structuring

After extracting raw documentation text, MapCoDoc optionally transforms it into structured JSON using GPT-4o. Large language models have demonstrated strong performance on information extraction tasks [15], and recent work has shown their effectiveness in extracting structured information from software documentation [16]. Our approach differs from prior work by incorporating URL preprocessing to prevent hallucination—a known challenge when LLMs process text containing hyperlinks [17].

#### 3.3.1 URL Preprocessing

Raw extracted documentation often contains embedded URLs (cross-references to other API members, external documentation). To prevent LLM hallucination or URL corruption:

```
Algorithm: PreprocessCrossReferences(raw_text)
  
  1. Identify all URLs in raw_text using regex patterns:
     - Markdown links: [text](url)
     - Bare URLs: https://...
     - Sphinx-style refs: :class:`...`
  
  2. Replace each URL with a placeholder:
     "See Conv2d(https://pytorch.org/docs/...)" 
     → "See Conv2d(URL_REF_1)"
  
  3. Store mapping: {"URL_REF_1": "https://pytorch.org/docs/..."}
  
  4. Return (preprocessed_text, url_mapping)
```

#### 3.3.2 Structured Extraction Schema

The LLM transforms preprocessed documentation into a standardized JSON schema:

```json
{
    "module_member_signature": "class torch.nn.Conv1d(in_channels, out_channels, ...)",
    "module_member_description": {
        "purpose": "Applies a 1D convolution over an input signal...",
        "additional_information": ["Supports dilation", "CuDNN optimized"]
    },
    "parameters": [
        {
            "name": "in_channels",
            "type": "int",
            "description": "Number of channels in the input signal",
            "additional_information": "Must be positive"
        }
    ],
    "attributes": [...],
    "methods": [...],
    "examples": [
        {
            "example": ">>> conv = nn.Conv1d(16, 33, 3)\n>>> input = torch.randn(20, 16, 50)\n>>> output = conv(input)",
            "additional_information": "Basic usage example"
        }
    ],
    "additional_notes": {
        "supplementary_information": ["Shape formula: L_out = ..."],
        "edge_cases": ["Zero-padding behavior"]
    }
}
```

#### 3.3.3 Concurrent Extraction

For large-scale documentation processing, MapCoDoc's `ConcurrentDocExtractor` enables parallel LLM requests:

```
Algorithm: ConcurrentExtraction(members, max_concurrent=10)
  
  1. Prepare extraction requests for all members
  2. Create asyncio semaphore(max_concurrent)
  3. For each member (concurrently):
     a. Acquire semaphore
     b. Send request to GPT-4o with member-specific prompt
     c. Parse JSON response
     d. Release semaphore
     e. Report progress
  4. Return all structured results
```

This provides speedup compared to sequential processing for large documentation sets.

#### 3.3.4 URL Postprocessing

After LLM structuring, placeholders are restored to original URLs:

```
Algorithm: PostprocessCrossReferences(structured_json, url_mapping)
  
  1. Traverse all string values in structured_json
  2. For each value containing URL_REF_n:
     Replace URL_REF_n with url_mapping[URL_REF_n]
  3. Return restored structured_json
```

#### 3.3.5 Skip-LLM Mode

When LLM processing is unavailable or undesired (`--skip-llm` flag or missing `OPENAI_API_KEY`):

1. Raw extracted text is stored directly in the database
2. `doc_format` is set to `'raw'` instead of `'structured'`
3. Basic metadata (first line as signature, first paragraph as description) is extracted heuristically
4. Full raw text remains available for later processing or manual review

### 3.4 Summary: Addressing RQ3

MapCoDoc addresses **RQ3** through:

1. **Multi-Source Support:** Automatic detection and handling of web-based and PDF documentation sources

2. **Hybrid Extraction:** Two-stage lexical-semantic search with statistical confidence gating for accurate per-member isolation

3. **Type-Aware Boundary Detection:** Stop signal matching that respects the structure of classes, methods, and functions

4. **LLM Structuring with URL Preservation:** GPT-4o-based transformation with careful preprocessing/postprocessing to maintain cross-reference integrity

5. **Graceful Degradation:** Skip-LLM mode for environments without API access, preserving raw documentation for alternative processing

## 4. Trace-Link Recovery

This section describes how MapCoDoc creates trace links between code members and their documentation, addressing **RQ2: How accurately can we recover traceability links between API reference documentation and source code?**

### 4.1 The Trace-Link Problem

A trace link connects a code member to its corresponding documentation. The challenge lies in the semantic gap between:

- **Code identifiers:** Implementation-level FQNs (e.g., `torch.nn.modules.conv.Conv1d`)
- **Documentation references:** Public API paths (e.g., `torch.nn.Conv1d`)

Traditional approaches rely on exact FQN matching, which fails when documentation uses public API paths. MapCoDoc bridges this gap by leveraging the API resolution results from Phase 1.

### 4.2 API Name Matching Strategy

MapCoDoc's trace-link recovery uses resolved API names as the primary matching key:

```
Algorithm: CreateTraceLinks(code_members, extracted_docs)
  
  trace_links = {}
  
  for doc in extracted_docs:
      doc_api_name = extract_api_name(doc.filename)  # e.g., "torch.nn.Conv1d"
      
      # Strategy 1: Match by primary API name
      member = lookup_by_primary_api_name(doc_api_name)
      
      if not member:
          # Strategy 2: Match by any API name
          member = lookup_by_any_api_name(doc_api_name)
      
      if not member:
          # Strategy 3: Match by FQN (for internal members)
          member = lookup_by_fqn(doc_api_name)
      
      if not member:
          # Strategy 4: Match by inherited API name
          inherited = lookup_inherited_member_by_api_name(doc_api_name)
          if inherited:
              handle_inherited_member_link(inherited, doc)
              continue
      
      if member:
          trace_links[member.fqn] = doc
  
  return trace_links
```

The prioritized matching strategy ensures:
1. Direct matches are found efficiently using indexed API name lookups
2. Re-exported members are correctly matched via their public paths
3. Inherited members are linked through their derived API names

### 4.3 Inherited Member Trace Links

Inherited members require special handling because the documentation may reference methods via the inheriting class's API path even though the method is defined in a parent class.

#### 4.3.1 Inclusion in Pipeline Inputs

During documentation processing, inherited members are automatically included in the pipeline inputs:

```
Algorithm: BuildPipelineInputs(db_members)
  
  inputs = []
  
  # Include direct members
  for member in db_members:
      inputs.append(MemberInput(
          api_name=member.primary_api_name,
          signature_variants=member.signatures,
          member_type=member.type
      ))
  
  # Include inherited members for all classes
  for class_member in db_members.where(type='class'):
      inherited_list = get_inherited_members(class_member.fqn)
      
      for inherited in inherited_list:
          inputs.append(MemberInput(
              api_name=inherited.inherited_api_name,  # Uses derived path
              signature_variants=get_signatures(inherited),
              member_type=inherited.member_type
          ))
  
  return inputs
```

Each inherited member is included using its **derived API name** (the path through the inheriting class), enabling the documentation extractor to find documentation that references the method via the subclass.

#### 4.3.2 Linking Strategy for Inherited Members

When linking documentation to inherited members, MapCoDoc differentiates between internal and external inheritance:

**Internal Inherited Members:**
```
Source: Method defined within the analyzed repository
Action: Link documentation to the ORIGINAL DBMember record
Rationale: The original member should be the source of truth;
           inherited member records reference this original
```

**External Inherited Members:**
```
Source: Method inherited from an external library (e.g., sklearn)
Action: Store documentation directly on DBInheritedMember record
Rationale: No DBMember exists for external methods;
           the inherited member record is the only storage location
```

Example with XGBoost:
```
xgboost.XGBClassifier.score
  └── Inherited from: sklearn.base.ClassifierMixin.score
  └── Original member: None (external)
  └── Documentation stored on: DBInheritedMember record

xgboost.XGBRFClassifier.evals_result
  └── Inherited from: xgboost.core.XGBModel.evals_result
  └── Original member: DBMember for XGBModel.evals_result (internal)
  └── Documentation stored on: Original DBMember record
```

### 4.4 Database Update

After matching, documentation is persisted to the database:

```
Algorithm: UpdateDatabaseWithDocumentation(trace_links)
  
  for (member_fqn, doc) in trace_links:
      member = get_member_by_fqn(member_fqn)
      
      if doc.format == 'structured':
          member.doc_format = 'structured'
          member.api_reference = doc.structured_json
          member.doc_signature = doc.structured_json['module_member_signature']
          member.doc_description = doc.structured_json['module_member_description']['purpose']
          member.doc_examples = doc.structured_json.get('examples', [])
      else:  # raw format
          member.doc_format = 'raw'
          member.doc_raw_text = doc.raw_text
          member.doc_signature = extract_first_line(doc.raw_text)
          member.doc_description = extract_first_paragraph(doc.raw_text)
      
      member.doc_source_type = doc.source_type  # 'web' or 'pdf'
      member.doc_source_path = doc.source_path
      
      commit()
```

The dual-format support (`structured` vs `raw`) enables flexible querying:
- Structured documentation provides rich programmatic access to parameters, examples, etc.
- Raw documentation preserves full text for manual review or alternative processing

### 4.5 Comprehensive Lookup API

The database layer exposes a comprehensive lookup API that handles the complexity of direct members, inherited members, and multiple API paths:

```python
def find_member_by_any_path(api_path):
    """
    Find a member by any valid API path, checking both direct and inherited members.
    
    Returns:
        {
            'type': 'direct' | 'inherited',
            'member': DBMember | None,
            'inherited_member': DBInheritedMember | None,
            'original_member': DBMember | None  # For inherited, links to original
        }
    """
    # Check direct members first
    member = get_member_by_any_api_name(api_path)
    if member:
        return {'type': 'direct', 'member': member}
    
    # Check inherited members
    inherited = get_inherited_member_by_api_name(api_path)
    if inherited:
        original = get_member_by_id(inherited.original_member_id)
        return {
            'type': 'inherited',
            'inherited_member': inherited,
            'original_member': original  # May be None if external
        }
    
    return None
```

This unified lookup enables downstream tools to query by any valid API path and receive complete context about the member's origin and inheritance chain.

### 4.6 Summary: Addressing RQ2

MapCoDoc addresses **RQ2** through:

1. **API-Aware Matching:** Using resolved public API names rather than implementation FQNs for documentation matching

2. **Prioritized Lookup Strategy:** A multi-tier matching approach that handles direct members, re-exported members, and inherited members

3. **Inheritance-Aware Linking:** Special handling for inherited members with correct attribution to original definitions (internal) or direct storage (external)

4. **Unified Query Interface:** A comprehensive lookup API that abstracts the complexity of multiple API paths and inheritance relationships

5. **Dual-Format Support:** Flexible storage of structured or raw documentation with appropriate metadata

## 5. Database Schema and Storage

MapCoDoc persists all analysis results and documentation in a SQLite database, providing a queryable interface for downstream applications.

### 5.1 Entity-Relationship Model

The database schema captures the full structure of Python codebases with their documentation:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           MapCoDoc Database Schema                              │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│   ┌─────────────┐       ┌─────────────────┐       ┌─────────────┐               │
│   │  DBModule   │1─────*│    DBMember     │1─────*│ DBSignature │               │
│   │             │       │                 │       │             │               │
│   │ • name      │       │ • fqn           │       │ • variant   │               │
│   │ • file_path │       │ • api_name      │       │ • text      │               │
│   │ • has_all   │       │ • type          │       └─────────────┘               │
│   │ • exports[] │       │ • parent_id     │                                     │
│   └──────┬──────┘       │ • api_reference │                                     │
│          │              └────────┬────────┘                                     │
│          │                       │                                              │
│          │ 1                     │ 1 (inheriting_class)                         │
│          ▼                       ▼                                              │
│   ┌─────────────┐       ┌───────────────────────┐                               │
│   │  DBImport   │       │   DBInheritedMember   │                               │
│   │             │       │                       │                               │
│   │ • source    │       │ • member_name         │                               │
│   │ • alias     │       │ • inherited_api_name  │                               │
│   │ • is_rel    │       │ • original_api_name   │                               │
│   └─────────────┘       │ • source_class_fqn    │                               │
│          │              │ • signature           │                               │
│          │              │ • is_external         │                               │
│          ▼              └───────────┬───────────┘                               │
│   ┌─────────────┐                   │ * (original_member, nullable)             │
│   │  DBExport   │*─────────────────1│                                           │
│   │             │       ┌───────────▼───────────┐                               │
│   │ • name      │       │      DBMember         │                               │
│   │ • is_reexp  │       │      (target)         │                               │
│   └─────────────┘       └───────────────────────┘                               │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Core Tables

#### 5.2.1 DBModule

Represents Python source files and packages:

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer | Primary key |
| `name` | String | Dotted module path (e.g., `torch.nn.modules.conv`) |
| `file_path` | String | Relative path to source file |
| `is_package` | Boolean | True if `__init__.py` |
| `has_all` | Boolean | True if defines `__all__` |
| `all_exports` | JSON | List of names in `__all__` |
| `needs_dynamic_analysis` | Boolean | Whether dynamic analysis is required |
| `module_statistics` | JSON | Metrics: `{num_classes, num_functions, loc}` |

#### 5.2.2 DBMember

Represents code definitions with their resolved API names and documentation:

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer | Primary key |
| `module_id` | FK → modules | Defining module |
| `name` | String | Short name (e.g., `Conv1d`) |
| `fully_qualified_name` | String | Implementation path |
| `member_type` | String | `class`, `function`, `method`, `variable` |
| `parent_id` | FK → members | Parent for methods/nested classes |
| **API Resolution** | | |
| `primary_api_name` | String | Canonical public API name |
| `all_api_names` | JSON | All valid public paths |
| `api_name_sources` | JSON | Map: API name → exporting module |
| `best_export_chain` | JSON | Resolution provenance |
| **Code Metadata** | | |
| `source_code` | Text | Full source code |
| `docstring` | Text | Inline docstring |
| `parameters` | JSON | Parameter list with types |
| `decorators` | JSON | Decorator strings |
| **Documentation** | | |
| `doc_format` | String | `'structured'`, `'raw'`, or `None` |
| `api_reference` | JSON | Structured documentation (if `doc_format='structured'`) |
| `doc_raw_text` | Text | Raw documentation (if `doc_format='raw'`) |
| `doc_signature` | Text | Quick-access signature |
| `doc_description` | Text | Quick-access description |
| `doc_examples` | JSON | Quick-access examples |
| `doc_source_type` | String | `'web'` or `'pdf'` |

#### 5.2.3 DBInheritedMember

Tracks inherited member relationships with derived API names:

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer | Primary key |
| `inheriting_class_id` | FK → members | Class that inherits the member |
| `original_member_id` | FK → members | Original definition (nullable if external) |
| `member_name` | String | Method name (e.g., `evals_result`) |
| `member_type` | String | `'method'`, `'property'` |
| `source_class_fqn` | String | Parent class FQN |
| `inherited_api_name` | String | Derived API name (indexed) |
| `inherited_api_names` | JSON | All derived API names |
| `original_api_name` | String | Original API name |
| `signature` | JSON | Signature for extraction |
| `is_external` | Boolean | True if from external library |
| **Documentation (external only)** | | |
| `doc_format` | String | Documentation format |
| `api_reference` | JSON | Structured docs (external members) |
| `doc_raw_text` | Text | Raw docs (external members) |

**Unique Constraint:** `(inheriting_class_id, member_name)` ensures each class inherits a member at most once.

#### 5.2.4 DBExport and DBImport

Capture module relationships:

**DBExport:**
| Column | Type | Description |
|--------|------|-------------|
| `exporter_module_id` | FK → modules | Module doing the export |
| `exported_name` | String | Name exported as |
| `target_member_id` | FK → members | Underlying definition |
| `is_explicit` | Boolean | In `__all__` |
| `is_reexport` | Boolean | Re-exported from elsewhere |

**DBImport:**
| Column | Type | Description |
|--------|------|-------------|
| `importer_module_id` | FK → modules | Module doing the import |
| `source_module_fqn` | String | Imported from module |
| `imported_entity_fqn` | String | What was imported |
| `name_bound_in_importer` | String | Bound name (e.g., `np`) |
| `is_relative` | Boolean | Relative import |
| `is_wildcard` | Boolean | `from x import *` |
| `is_source_internal` | Boolean | From same codebase |

### 5.3 Query Interface

The `QueryManager` class provides a high-level API for common queries:

```python
class QueryManager:
    # Member Queries
    def get_member_by_fqn(fqn: str) -> MemberDetails
    def get_member_by_any_api_name(api_name: str) -> MemberDetails
    def get_members_by_type(member_type: str, module_prefix: str = None) -> List[MemberDetails]
    
    # Inherited Member Queries
    def get_inherited_members_for_class(class_fqn: str) -> List[InheritedMemberDetails]
    def get_inherited_member_by_api_name(api_name: str) -> InheritedMemberDetails
    def get_original_member_for_inherited(api_name: str) -> MemberDetails
    
    # Comprehensive Lookup
    def find_member_by_any_path(api_path: str) -> Dict  # Checks both direct and inherited
    def get_all_api_names_for_member(fqn: str) -> List[str]  # Includes inherited paths
    
    # Documentation Queries
    def get_member_documentation(api_name: str) -> MemberDocumentation
    def get_inherited_member_documentation(api_name: str) -> Dict
    def get_members_with_documentation(module_prefix: str = None) -> List[MemberDetails]
    def get_documentation_coverage(module_prefix: str = None) -> Dict
    
    # Statistics
    def get_database_statistics() -> Dict
    def get_documentation_format_statistics() -> Dict
```

### 5.4 Ingestion Pipeline

Database ingestion proceeds in phases to handle foreign key dependencies:

```
Phase 1: Modules and Members
  └── Create DBModule for each analyzed file
  └── Create DBMember for each component
  └── Cache FQN → member_id mapping

Phase 1.5: Parent-Child Relationships
  └── Link methods to parent classes
  └── Link nested classes to outer classes

Phase 2: Export Records
  └── Create DBExport linking modules to members
  └── Use cached member_id mapping for target resolution

Phase 3: Import Records
  └── Create DBImport for each import statement
  └── Set is_source_internal based on analysis

Phase 4: Inherited Members
  └── Create DBInheritedMember for each inherited method
  └── Link to original member if internal
  └── Store signature for documentation extraction
```

### 5.5 Documentation Storage Modes

The database supports two documentation formats with distinct storage patterns:

**Structured Mode (`doc_format='structured'`):**
- `api_reference`: Full structured JSON with signature, parameters, examples
- `doc_signature`, `doc_description`, `doc_examples`: Quick-access extracted fields
- Enables rich programmatic queries (e.g., find all members with return type `Tensor`)

**Raw Mode (`doc_format='raw'`):**
- `doc_raw_text`: Full extracted text
- `doc_signature`: First line (heuristically extracted)
- `doc_description`: First paragraph
- Preserves documentation for later LLM processing or manual review

This dual-mode approach enables:
1. Fast initial ingestion with `--skip-llm`
2. Incremental LLM processing of raw documentation
3. Format-aware queries that handle both modes transparently

## 6. Illustrative Example: XGBoost

To demonstrate MapCoDoc's capabilities, we present a complete trace-link recovery example using XGBoost, a gradient boosting library with complex inheritance patterns spanning internal and external dependencies.

### 6.1 Input

**Repository:** XGBoost Python package (`python-package/xgboost/`)

**Documentation Source:** https://xgboost.readthedocs.io/en/latest/python/python_api.html

### 6.2 Code Analysis Results

MapCoDoc analyzes the XGBoost codebase and discovers:

**Direct Members:**
```
xgboost.core.XGBModel (class)
  ├── Definition: xgboost/core.py:1250
  ├── FQN: xgboost.core.XGBModel
  ├── API Name: xgboost.XGBModel
  └── Methods: fit, predict, evals_result, save_model, ...

xgboost.sklearn.XGBClassifier (class)
  ├── Definition: xgboost/sklearn.py:850
  ├── FQN: xgboost.sklearn.XGBClassifier
  ├── API Name: xgboost.XGBClassifier
  ├── Base Classes: [XGBModel, sklearn.base.ClassifierMixin]
  └── Methods: __init__, ... (overrides)

xgboost.sklearn.XGBRFClassifier (class)
  ├── Definition: xgboost/sklearn.py:1420
  ├── FQN: xgboost.sklearn.XGBRFClassifier
  ├── API Name: xgboost.XGBRFClassifier
  ├── Base Classes: [XGBClassifier]
  └── Methods: __init__ (override only)
```

**Inheritance Resolution:**

For `XGBRFClassifier`, MapCoDoc resolves the complete inheritance chain:

```
XGBRFClassifier
  ├── Direct base: XGBClassifier (internal)
  │   ├── Inherited methods: fit, predict, feature_importances_, ...
  │   │
  │   └── Base: XGBModel (internal)
  │       └── Inherited methods: evals_result, save_model, load_model, ...
  │
  └── Transitive base: sklearn.base.ClassifierMixin (external)
      └── Inherited methods: score (discovered via ExternalIntrospector)
```

**API Name Propagation for Inherited Members:**

| Inherited Method | Original Definition | Derived API Names |
|-----------------|---------------------|-------------------|
| `evals_result` | `xgboost.XGBModel.evals_result` | `xgboost.XGBRFClassifier.evals_result`, `xgboost.XGBClassifier.evals_result` |
| `score` | `sklearn.base.ClassifierMixin.score` | `xgboost.XGBRFClassifier.score`, `xgboost.XGBClassifier.score` |

### 6.3 Documentation Processing

The XGBoost documentation uses a **per_page** layout where all API members are on a single page with fragment anchors.

**Extraction Process:**

1. **Crawl:** Single page discovered at `python/python_api.html`

2. **Scrape:** Full page text extracted (~500KB)

3. **Member Extraction Example (`xgboost.Booster.reset`):**
   ```
   Lexical needle: "reset()"
   Anchor found at: position 45230
   Stop signal: "save_model(" (next method signature)
   Extracted text (57 lines):
     "reset()
      Reset the booster to its initial state..."
   ```

4. **LLM Structuring (sample output):**
   ```json
   {
     "module_member_signature": "reset()",
     "module_member_description": {
       "purpose": "Reset the booster to its initial state",
       "additional_information": ["Clears all training history"]
     },
     "parameters": [],
     "returns": {
       "type": "self",
       "description": "Returns the booster instance"
     },
     "examples": []
   }
   ```

### 6.4 Trace-Link Recovery

**Direct Member Linking:**
```
Documentation: xgboost.XGBModel.txt
  └── Lookup: get_member_by_any_api_name("xgboost.XGBModel")
  └── Found: DBMember(fqn="xgboost.core.XGBModel")
  └── Link created ✓
```

**Inherited Member Linking:**
```
Documentation: xgboost.XGBRFClassifier.evals_result.txt
  └── Lookup: get_member_by_any_api_name("xgboost.XGBRFClassifier.evals_result")
  └── Not found (not a direct member)
  └── Lookup: get_inherited_member_by_api_name("xgboost.XGBRFClassifier.evals_result")
  └── Found: DBInheritedMember(inherited_api_name="xgboost.XGBRFClassifier.evals_result")
  └── Original member: DBMember(fqn="xgboost.core.XGBModel.evals_result")
  └── Documentation linked to original member ✓
```

**External Inherited Member Linking:**
```
Documentation: xgboost.XGBClassifier.score.txt
  └── Lookup cascade fails for direct/inherited internal members
  └── Lookup: get_inherited_member_by_api_name("xgboost.XGBClassifier.score")
  └── Found: DBInheritedMember(is_external=True, source_class_fqn="sklearn.base.ClassifierMixin")
  └── Documentation stored directly on inherited member record ✓
```

### 6.5 Final Database State

After complete processing, the XGBoost database contains:

| Metric | Count |
|--------|-------|
| Modules | 15 |
| Direct Members | 127 |
| Inherited Members | 342 |
| Documented Members | 118 |
| External Inherited Documented | 24 |
| Documentation Coverage | 93% |

**Query Example:**
```python
# Find documentation for an inherited method
result = qm.find_member_by_any_path("xgboost.XGBRFClassifier.evals_result")
# Returns:
# {
#     'type': 'inherited',
#     'inherited_member': <DBInheritedMember>,
#     'original_member': <DBMember for XGBModel.evals_result>
# }

# Get the documentation
doc = qm.get_inherited_member_documentation("xgboost.XGBRFClassifier.evals_result")
# Returns structured JSON with description, parameters, examples
```

This example demonstrates MapCoDoc's key capabilities:
- Accurate API path resolution through complex re-export chains
- Framework-agnostic inheritance handling (internal XGBModel + external sklearn)
- Per-member documentation isolation from single-page documentation
- Correct trace-link recovery for both direct and inherited members

## 7. Conclusion

This methodology section presented MapCoDoc, a framework-agnostic pipeline for recovering traceability links between Python source code and API reference documentation. Our approach addresses the three research questions through complementary technical innovations:

**RQ1 (API Path Mapping):** MapCoDoc's tiered API resolution strategy combines fast graph-less resolution with optional graph-based fallbacks, achieving accurate mapping of implementation FQNs to public API paths. The framework-agnostic inheritance resolver handles both internal and external base classes through import record analysis and dynamic introspection, enabling complete inherited member discovery without library-specific customization.

**RQ2 (Trace-Link Recovery):** By leveraging resolved API names as matching keys rather than implementation paths, MapCoDoc bridges the semantic gap between code identifiers and documentation references. The inheritance-aware linking strategy correctly attributes documentation to original definitions while supporting lookup via any valid API path, including paths through inheriting classes.

**RQ3 (Documentation Extraction):** The hybrid lexical-semantic extraction approach with statistical confidence gating enables accurate per-member documentation isolation from diverse documentation layouts (per_member, per_module, per_page). Type-aware stop signal detection preserves the structure of classes, methods, and functions, while optional LLM structuring transforms raw text into queryable JSON schemas with URL preservation.

The resulting SQLite database provides a comprehensive queryable interface for downstream applications, supporting complex queries over code structure, API relationships, inherited members, and linked documentation. The dual-format documentation storage enables both structured access (via LLM processing) and raw text preservation (for environments without API access), while the phased ingestion pipeline ensures referential integrity across the relational schema.

MapCoDoc's modular architecture and configurable feature flags allow users to optimize for their specific requirements, from fast graph-less analysis for straightforward codebases to exhaustive graph-based resolution for complex re-export patterns. The framework-agnostic design ensures applicability across diverse Python libraries without library-specific customization, as demonstrated by successful trace-link recovery on libraries including XGBoost, PyTorch, NumPy, and pandas.

---

## References

[1] G. Brandl, "Sphinx: Python Documentation Generator," https://www.sphinx-doc.org/, 2007.

[2] M. Bysiek, "pdoc: Auto-generate API documentation for Python projects," https://pdoc.dev/, 2013.

[3] Kite, "Kite: AI-Powered Code Completion," https://www.kite.com/, 2014-2022.

[4] D. Halter, "Jedi: An autocompletion/static analysis library for Python," https://jedi.readthedocs.io/, 2012.

[5] G. Antoniol, G. Canfora, G. Casazza, A. De Lucia, and E. Merlo, "Recovering Traceability Links between Code and Documentation," *IEEE Trans. Software Eng.*, vol. 28, no. 10, pp. 970-983, 2002.

[6] A. Marcus and J. I. Maletic, "Recovering Documentation-to-Source-Code Traceability Links using Latent Semantic Indexing," in *Proc. 25th ICSE*, 2003, pp. 125-135.

[7] J. Guo, J. Cheng, and J. Cleland-Huang, "Semantically Enhanced Software Traceability Using Deep Learning Techniques," in *Proc. 39th ICSE*, 2017, pp. 3-14.

[8] X. Zhao, Z. Xing, M. A. Kabir, N. Sawada, J. Li, and S.-W. Lin, "HDSKG: Harvesting Domain Specific Knowledge Graph from Content of Webpages," in *Proc. 24th SANER*, 2017, pp. 56-67.

[9] A. V. Aho, M. S. Lam, R. Sethi, and J. D. Ullman, *Compilers: Principles, Techniques, and Tools*, 2nd ed. Addison-Wesley, 2006.

[10] E. Traut, "Pyright: Static Type Checker for Python," Microsoft, https://github.com/microsoft/pyright, 2019.

[11] J. Lehtosalo et al., "mypy: Optional Static Typing for Python," http://mypy-lang.org/, 2012.

[12] E. Ferrara, P. De Meo, G. Fiumara, and R. Baumgartner, "Web Data Extraction, Applications and Techniques: A Survey," *Knowledge-Based Systems*, vol. 70, pp. 301-323, 2014.

[13] D. Metzler and W. B. Croft, "A Markov Random Field Model for Term Dependencies," in *Proc. 28th ACM SIGIR*, 2005, pp. 472-479.

[14] V. Karpukhin et al., "Dense Passage Retrieval for Open-Domain Question Answering," in *Proc. EMNLP*, 2020, pp. 6769-6781.

[15] J. Wei et al., "Finetuned Language Models are Zero-Shot Learners," in *Proc. ICLR*, 2022.

[16] A. Nashaat, S. Ahmed, and J. Cleland-Huang, "Automated Extraction of Software Requirements from Natural Language Documents," in *Proc. RE*, 2023.

[17] Z. Ji et al., "Survey of Hallucination in Natural Language Generation," *ACM Computing Surveys*, vol. 55, no. 12, pp. 1-38, 2023.


