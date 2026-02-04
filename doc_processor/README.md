# Documentation Processor

The `doc_processor` module orchestrates the end-to-end extraction and structuring of API reference documentation from both PDF files and web-based documentation sites. It bridges the code analysis database with documentation sources to create trace links between code members and their official documentation.

## Table of Contents

- [Overview](#overview)
  - [Inherited Member Support](#inherited-member-support)
- [Architecture](#architecture)
- [Directory Structure](#directory-structure)
- [Pipeline Stages](#pipeline-stages)
  - [Web Pipeline](#web-pipeline)
  - [PDF Pipeline](#pdf-pipeline)
  - [Skip-LLM Mode](#skip-llm-mode)
- [Module Reference](#module-reference)
- [Usage](#usage)
  - [Programmatic Usage](#programmatic-usage)
  - [CLI Usage](#cli-usage)
- [Configuration](#configuration)
- [Output Formats](#output-formats)

---

## Overview

The documentation processor performs the following high-level tasks:

1. **Retrieve target members** from the MapCoDoc database (including inherited members)
2. **Extract raw documentation** from PDF or web sources
3. **Isolate per-member documentation** from combined pages
4. **Preprocess** URLs with placeholders for LLM processing (optional)
5. **Structure documentation** using LLM (GPT-4o) (optional)
6. **Postprocess** to restore URLs (optional)
7. **Update database** with structured documentation

Steps 4-6 are automatically skipped if `OPENAI_API_KEY` is not set or `--skip-llm` is used.

### Inherited Member Support

The doc processor fully supports **inherited members** - methods that are inherited from parent classes, including those from **external libraries**. This is critical for documentation linking because:

- Documentation often references inherited methods via the inheriting class's API path (e.g., `xgboost.XGBClassifier.score`)
- The actual method definition may be in a parent class (e.g., `sklearn.base.ClassifierMixin.score`)

**How it works:**
1. For each class in the target members, query `DBInheritedMember` records
2. Each inherited member is added to `pipeline_inputs` using its **derived API name** (via the inheriting class)
3. Inherited members are included in peer signatures for stop signal detection
4. Extraction lookup checks both direct members AND inherited members
5. Documentation storage:
   - **Internal inherited members**: Documentation is linked to the original `DBMember` record
   - **External inherited members**: Documentation is stored directly on the `DBInheritedMember` record (since no original member exists in the DB)

**External inherited members** (e.g., `sklearn.base.ClassifierMixin.score` inherited by `xgboost.XGBClassifier`) have signatures obtained via dynamic introspection during code analysis, enabling accurate stop signal detection during extraction.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           DocProcessingRunner                               │
│                         (doc_runner.py - Orchestrator)                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          │                           │                           │
          ▼                           ▼                           ▼
┌─────────────────┐       ┌─────────────────┐       ┌─────────────────────┐
│   Web Pipeline  │       │   PDF Pipeline  │       │   Common Stages     │
├─────────────────┤       ├─────────────────┤       ├─────────────────────┤
│ url_crawler.py  │       │ pipeline_pdf.py │       │ process_crossRef.py │
│ doc_scraper.py  │       │ extraction_     │       │ structured_doc_     │
│ filter_doc.py   │       │   utils.py      │       │   extracter.py      │
│ (WebMember      │       │ signature.py    │       │ (Concurrent         │
│  Extractor)     │       │ embeddings.py   │       │  DocExtractor)      │
└─────────────────┘       │ hybrid_search.py│       └─────────────────────┘
                          └─────────────────┘
                                      │
                                      ▼
                          ┌─────────────────────┐
                          │   MapCoDoc Database │
                          │   (DB Update)       │
                          └─────────────────────┘
```

---

## Directory Structure

```
doc_processor/
├── doc_runner.py              # Main orchestrator
├── filter_doc.py              # Stop signal matching, WebMemberExtractor
├── process_crossRef.py        # URL preprocessing/postprocessing
├── structured_doc_extracter.py # LLM-based doc structuring (+ ConcurrentDocExtractor)
├── README.md                  # This file
│
├── file_doc/                  # PDF extraction components
│   ├── extraction_utils.py    # Shared config (MemberExtractorConfig, _windows, etc.)
│   ├── pipeline_pdf.py        # PDF extraction pipeline
│   ├── signature.py           # Signature pattern building (build_lexical_needles)
│   ├── embeddings.py          # Sentence embedding model
│   ├── hybrid_search.py       # Lexical + semantic search
│   ├── chunk_selector.py      # Section/chunk selection
│   ├── pdf_localizer.py       # PDF parsing utilities
│   └── multiline_signature.py # Multi-line signature handling
│
├── web_doc/                   # Web extraction components
│   ├── url_crawler.py         # URL discovery and crawling
│   ├── doc_scraper.py         # HTML to text extraction
│   └── network.py             # Network utilities (rate limiting, proxies)
│
└── doc_artifacts/             # Generated artifacts (gitignored)
    ├── crawled_URLs/          # Discovered URLs per library
    ├── local_doc/             # Downloaded/stored PDFs
    ├── scraped_doc/           # Raw extracted text
    │   ├── per_member/        # Individual API member docs
    │   ├── per_module/        # Combined module/class docs' members.json
    │   ├── per_page/          # Single page with all APIs
    │   └── combined/          # Relocated combined docs (after extraction)
    ├── preprocessed_doc/      # URL placeholders applied
    │   ├── doc/               # Preprocessed text files
    │   └── url_context/       # URL mapping JSONs
    ├── structured_doc/        # LLM-structured JSONs
    └── postprocessed_doc/     # Final documentation
```

---

## Pipeline Stages

### Web Pipeline

```
Step 0: Build Pipeline Inputs
    ↓ Retrieve target members from database
    ↓ Query inherited members for all classes
    ↓ Convert to MemberInput objects (direct + inherited)
    ↓ Each inherited member uses derived API name (via inheriting class)

Step 1: Crawl URLs
    ↓ save_urls_to_file(url, lib_name, version)
    ↓ Output: crawled_URLs/{lib}/{version}/scraped_urls.txt
    
Step 2: Scrape HTML Pages
    ↓ scrape_doc(lib_name, version, url_file, stat_info)
    ↓ Output: scraped_doc/{lib}/{version}/per_member|per_module|per_page/
    
Step 3: Extract Per-Member Documentation
    ↓ _build_extraction_list() resolves API names via:
    ↓   1. Direct member lookup (get_member_by_any_api_name)
    ↓   2. Inherited member lookup (get_inherited_member_by_api_name)
    ↓   3. Local member_map fallback
    ↓   4. Short name search
    ↓ WebMemberExtractor.find_anchor_position()
    ↓ StopSignalMatcher for boundaries (includes inherited member signatures)
    ↓ Output: scraped_doc/{lib}/{version}/per_member/{api_name}.txt
    
Step 3d: Fallback - Missing Methods from Class Docs
    ↓ Search parent class doc for method documentation
    ↓ Includes BOTH direct methods AND inherited methods ← ENHANCED
    ↓ Output: scraped_doc/{lib}/{version}/per_member/{method_api_name}.txt

Step 3e: Filter Container Docs
    ↓ _filter_container_doc() - Check if container is API member via DB
    ↓ If class: extract class-only doc to per_member/{api_name}.txt
    ↓ If module: skip (not an API member)

Step 3f: Relocate Combined Docs
    ↓ _relocate_combined_docs()
    ↓ Move combined/module docs from per_member/ to combined/
    ↓ Output: scraped_doc/{lib}/{version}/combined/{name}.txt

Step 3g: Filter Pipeline Inputs
    ↓ Filter to only members with extracted docs in per_member/
    ↓ Skip members without documentation
    
Step 4: Preprocess URLs (if LLM enabled)
    ↓ preprocess_crossRef(scraped_doc, preprocessed_doc, url_context)
    ↓ Output: preprocessed_doc/{lib}/{version}/doc/{api_name}.txt
    ↓ Output: preprocessed_doc/{lib}/{version}/url_context/{api_name}.json
    
Step 5: LLM Structuring (if LLM enabled)
    ↓ ConcurrentDocExtractor.extract_batch() - 10+ concurrent requests
    ↓ Output: structured_doc/{lib}/{version}/{api_name}.json
    
Step 6: Postprocess URLs (if LLM enabled)
    ↓ postprocess_crossRef(url_context, structured_doc, output)
    ↓ Output: postprocessed_doc/{lib}/{version}/{api_name}.json
    
Step 7: Database Update
    ↓ Update DBMember.api_reference with structured JSON (direct members)
    ↓ For INTERNAL inherited members: 
    ↓   • Update original DBMember record with documentation
    ↓   • Inherited member record links to original
    ↓ For EXTERNAL inherited members:
    ↓   • Store documentation directly on DBInheritedMember record
    ↓   • Set doc_format, doc_raw_text/api_reference, doc_signature, doc_description
```

### PDF Pipeline

```
Step 0: Build Pipeline Inputs
    ↓ Retrieve target members from database
    ↓ Query inherited members for all classes
    ↓ Convert to MemberInput objects (direct + inherited)
    ↓ Build peer_signatures map (includes inherited member signatures)

Step 1: Store PDF Locally
    ↓ Copy or download PDF
    ↓ Output: local_doc/{lib}/{version}/{filename}.pdf
    
Step 2: Extract Documentation
    ↓ extract_api_docs_from_pdf(pdf_path, members, peer_signatures, ...)
    ↓ Uses MemberExtractor with lexical + semantic search
    ↓ peer_signatures includes inherited members for stop signal detection
    ↓ Output: scraped_doc/{lib}/{version}/per_member/{api_name}.txt
    ↓ Output: scraped_doc/{lib}/{version}/extracted_docs.json
    
Steps 4-7: Same as Web Pipeline
```

#### `_filter_container_doc`

Filters combined container docs to extract only the main class/API description.
Uses database lookup to determine if container is an API member.

- If container is a **class**: Extracts filtered doc to `per_member/{api_name}.txt`
- If container is a **module**: Skips (not an API member, will be relocated)

#### `_relocate_combined_docs`

Moves combined/module docs out of `per_member/` to `combined/` folder.
Ensures `per_member/` only contains individual API member docs.


### Skip-LLM Mode

When `skip_llm=True` or `OPENAI_API_KEY` is not set:

```
Steps 1-3: Same as above (extraction)
    ↓
Steps 4-6: SKIPPED (no preprocessing, LLM, or postprocessing)
    ↓
Step 7: Database Update (from raw scraped docs)
    ↓ _update_database_from_raw()
    ↓ Uses scraped_doc/{lib}/{version}/per_member/ directly
```

This mode is useful for:
- Quick testing without API costs
- Environments without OpenAI access
- When raw documentation is sufficient

---

## Module Reference

### `doc_runner.py`

Main orchestrator class.

```python
from doc_processor.doc_runner import DocProcessingRunner

runner = DocProcessingRunner(
    db_path="path/to/mapcodoc.db",
    library_name="torch",
    version="2"
)

# Process documentation (full pipeline)
runner.run(
    doc_source="https://docs.pytorch.org/docs/stable/generated/torch.nn.L1Loss.html#torch.nn.L1Loss",  # URL or PDF path
    target_module="torch.nn",  # Optional: filter by module prefix
    skip_llm=False  # Optional: skip LLM processing
)

# Skip LLM processing (uses raw scraped docs)
runner.run(
    doc_source="https://docs.pytorch.org/docs/stable/generated/torch.nn.L1Loss.html",
    skip_llm=True
)
```

**`run()` Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `doc_source` | `str` | Required | URL to web docs OR path to PDF file |
| `target_module` | `str` | `None` | Module prefix filter (auto-detected if not set) |
| `skip_llm` | `bool` | `False` | Skip LLM structuring (Steps 4-6) |

#### Inherited Member Handling Methods

**`_get_inherited_members_for_pipeline(class_members)`**

Queries all inherited members for classes in the target list. Returns tuples of `(InheritedMemberDetails, original_MemberDetails)`.

```python
# Internal usage - called automatically during run()
inherited_members = runner._get_inherited_members_for_pipeline(class_members)
# Example result: [(InheritedMemberDetails for XGBRFClassifier.evals_result, 
#                   MemberDetails for XGBModel.evals_result), ...]
```

**`_inherited_to_member_input(inherited, original_member)`**

Converts an `InheritedMemberDetails` to a `MemberInput` for pipeline processing:
- Uses the **inherited API name** (e.g., `xgboost.XGBRFClassifier.evals_result`)
- Gets signature variants from the original definition
- Sets member_type from inherited metadata

### `filter_doc.py`

#### StopSignalMatcher

Type-aware boundary detection for extracting individual member docs from combined pages.

```python
from doc_processor.filter_doc import StopSignalMatcher

matcher = StopSignalMatcher(
    peer_signatures=["Conv2d(", "class torch.nn.Conv2d("],
    target_member_type="class",
    target_api_name="torch.nn.Conv1d"
)

# Check if a line marks the start of a peer member (stop signal)
if matcher.is_stop_signal(line):
    # End extraction here
    pass
```

#### WebMemberExtractor

Two-stage (lexical + semantic) member extraction for web docs with adaptive confidence gating.

```python
from doc_processor.filter_doc import WebMemberExtractor, WebMemberInfo
from doc_processor.file_doc.extraction_utils import MemberExtractorConfig

cfg = MemberExtractorConfig(semantic_mode="auto")
extractor = WebMemberExtractor(cfg, embedder)

# Find anchor position in combined doc
pos, score, match_type = extractor.find_anchor_position(
    combined_text, member_info, model_name
)
```

**Features:**
- Statistical confidence gating via `_should_use_semantic_member`
- Adaptive length penalty for varying document sizes
- Two-stage semantic search (coarse window -> fine anchor)

### `process_crossRef.py`

URL placeholder preprocessing and postprocessing.

```python
from doc_processor.process_crossRef import preprocess_crossRef, postprocess_crossRef

# Replace URLs with placeholders before LLM processing
preprocess_crossRef(
    scraped_doc_path="path/to/scraped.txt",
    doc_file_path="path/to/preprocessed.txt",
    url_file_path="path/to/url_context.json"
)

# Restore URLs in structured output
postprocess_crossRef(
    url_mapping_path="path/to/url_context.json",
    structured_doc_path="path/to/structured.json",
    processed_doc_path="path/to/final.json"
)
```

### `structured_doc_extracter.py`

LLM-based documentation structuring using GPT-4o.

#### DocumentationExtractor (Single document)

```python
from doc_processor.structured_doc_extracter import DocumentationExtractor

extractor = DocumentationExtractor(
    MM_type="class",  # 'class', 'function', 'method'
    MM_signature="torch.nn.L1Loss(size_average=None, reduce=None, reduction='mean')",
    MM_code_body="",
    MM_methods_and_attributes_signature="",
    scraped_doc_path="path/to/preprocessed.txt",
    api_key="your-openai-api-key",
    input_choice='module_member_signature'
)

extractor.extract_and_save_documentation()
```

#### ConcurrentDocExtractor

```python
from doc_processor.structured_doc_extracter import ConcurrentDocExtractor
import asyncio

extractor = ConcurrentDocExtractor(
    api_key="your-openai-api-key",
    max_concurrent=10  # Parallel requests
)

# Prepare extraction requests
requests = [
    {
        "api_name": "torch.nn.Conv1d",
        "member_type": "class",
        "signature": "Conv1d(in_channels, out_channels, ...)",
        "doc_path": "path/to/preprocessed/torch.nn.Conv1d.txt",
        "output_path": "path/to/structured/torch.nn.Conv1d.json"
    },
    # ... more requests
]

def progress_callback(completed, total):
    print(f"Progress: {completed}/{total}")

# Run concurrent extraction
results = asyncio.run(extractor.extract_batch(requests, progress_callback))
```

**Benefits over sequential:**
- 10-50x faster with concurrent requests
- Automatic rate limit handling
- Progress tracking per-request

### `file_doc/extraction_utils.py`

Shared extraction configuration and utilities.

```python
from doc_processor.file_doc.extraction_utils import (
    MemberExtractorConfig,
    _windows,
    _should_use_semantic_member,
    _dynamic_threshold
)

# Configuration for extraction behavior
cfg = MemberExtractorConfig(
    semantic_mode="auto",      # {"auto", "never", "always", "only"}
    lexical_sigma_k=0.25,      # Confidence threshold
    lexical_margin_min=0.20,   # Top-1 vs top-2 margin
    window_chars=3000,         # Semantic search window size
    window_stride=2000         # Window overlap
)

# Generate sliding windows for semantic search
windows = _windows(text, window_chars=3000, stride=2000)

# Check if semantic search should be triggered
need_semantic = _should_use_semantic_member(lex_scores, sigma_k=0.25, margin_min=0.20)
```

### `file_doc/signature.py`

Signature pattern and needle generation.

```python
from doc_processor.file_doc.signature import MemberInput, build_lexical_needles

member = MemberInput(
    api_name="torch.nn.Conv1d",
    signature_variants=["Conv1d(in_channels, out_channels, kernel_size, ...)"],
    member_type="class"
)

# Build tiered search needles
needles = build_lexical_needles(member)
# Returns: {
#     "exact": ["torch.nn.Conv1d(in_channels, ...", "class torch.nn.Conv1d(..."],
#     "prefix": ["Conv1d(", "torch.nn.Conv1d("],
#     "anchor": ["Conv1d", "torch.nn.Conv1d"]
# }
```

**Needle Generation:**
- Automatically combines API names with parameter parts from signatures
- Generates class-prefixed variants for classes
- Creates truncated signatures (first 3 params) for robust matching

---

## Usage

### Programmatic Usage

```python
from doc_processor.doc_runner import DocProcessingRunner

# Initialize runner
runner = DocProcessingRunner(
    db_path="mapcodoc.db",
    library_name="numpy",
    version="2"
)

# Process from web documentation (full pipeline)
runner.run(
    doc_source="https://numpy.org/devdocs/reference/generated/numpy.apply_along_axis.html",
    target_module="numpy"  # Optional
)

# Process from PDF
runner.run(
    doc_source="doc_processor/doc_artifacts/local_doc/numpy-ref.pdf"
)

# Skip LLM processing (raw docs only)
runner.run(
    doc_source="https://numpy.org/devdocs/reference/generated/numpy.apply_along_axis.html",
    skip_llm=True
)
```

### CLI Usage

Documentation processing is integrated into the MapCoDoc CLI:

```bash
# Standalone documentation extraction
python -m cli.main extract-docs \
    --db-path mapcodoc_output/numpy_2.db \
    --library-name numpy \
    --version 2 \
    --doc-source "https://numpy.org/devdocs/reference/generated/numpy.apply_along_axis.html"

# Skip LLM processing
python -m cli.main extract-docs \
    --db-path mapcodoc_output/numpy_2.db \
    --library-name numpy \
    --version 2 \
    --doc-source "https://numpy.org/devdocs/reference/generated/numpy.apply_along_axis.html" \
    --skip-llm

# Combined code analysis + documentation extraction
python -m cli.main analyze \
    --target ./path/to/numpy \
    --doc-source "https://numpy.org/doc/stable/reference/..."

# With explicit target module filter
python -m cli.main analyze \
    --target ./path/to/numpy \
    --doc-source "https://numpy.org/doc/stable/reference/..." \
    --target-module numpy.core
```

**CLI Arguments:**

| Argument | Description |
|----------|-------------|
| `--doc-source` | URL to web docs OR path to PDF file |
| `--target-module` | Optional module prefix filter |
| `--skip-llm` | Skip LLM-based structured extraction |
| `--library-name` | Library name (for `extract-docs` command) |
| `--version` | Library version (for `extract-docs` command) |
| `--db-path` | Path to database (for `extract-docs` command) |

### Environment Variables

```bash
# Optional - LLM structuring (Step 5) is auto-skipped if not set
export OPENAI_API_KEY="sk-..."

# Or use a .env file in the project root:
# OPENAI_API_KEY=sk-...
```

**Behavior when `OPENAI_API_KEY` is not set:**
- Warning message displayed
- Steps 4-6 automatically skipped
- Database updated with raw scraped documentation
- No error thrown

---

## Configuration

### MemberExtractorConfig

Controls extraction behavior for both PDF and web pipelines:

```python
from doc_processor.file_doc.extraction_utils import MemberExtractorConfig

cfg = MemberExtractorConfig(
    semantic_mode="auto",       # Semantic search strategy
    lexical_sigma_k=0.25,       # Confidence strictness
    lexical_margin_min=0.20,    # Top-1 vs top-2 margin
    topK_sections=50,           # Sections for refinement
    window_chars=3000,          # Window size for semantic search
    window_stride=2000,         # Overlap between windows
    max_workers=8,              # Parallel workers
    snippet_boost_weight=0.5    # Semantic boost weight
)
```

| Mode | Description | Use Case |
|------|-------------|----------|
| `auto` | Lexical first, semantic fallback if low confidence | Default - balanced |
| `never` | Lexical only (fastest) | Well-structured docs |
| `always` | Both lexical and semantic | Noisy documentation |
| `only` | Semantic only (slowest) | When lexical fails |

### Semantic Gating

The `_should_use_semantic_member` function uses statistical confidence to decide when semantic search is needed:

```python
# Semantic triggered when:
# 1. Top lexical score < mean + sigma_k * std (not a clear winner)
# 2. OR (top-1 - top-2) < margin_min * top-1 (too close to runner-up)
```

### Adaptive Length Penalty

`WebMemberExtractor` uses adaptive length penalty for semantic gating:

| Document Size | Penalty | Effect |
|---------------|---------|--------|
| < 500 chars | 0.0 | Lexical trusted |
| 500-15000 chars | 0.0-0.15 | Gradual increase |
| > 15000 chars | 0.15 (capped) | Moderate skepticism |

---

## Output Formats

### Structured Documentation JSON (Class)

```json
{
    "module_member_signature": "class torch.nn.L1Loss(size_average=None, reduce=None, reduction='mean')",
    "module_member_description": {
        "purpose": "Creates a criterion that measures the mean absolute error...",
        "additional_information": ["..."]
    },
    "parameters": [
        {
            "name": "size_average",
            "type": "bool, optional",
            "description": "Deprecated...",
            "additional_information": "N/A"
        }
    ],
    "attributes": [],
    "methods": [],
    "examples": [
        {
            "example": ">>> loss = nn.L1Loss()\n>>> input = torch.randn(3, 5)...",
            "additional_information": "N/A"
        }
    ],
    "additional_notes": {
        "supplementary_information": ["..."],
        "edge_cases": ["..."]
    }
}
```

### Structured Documentation JSON (Function/Method)

```json
{
    "module_member_signature": "apply_along_axis(func1d, axis, arr, *args, **kwargs)",
    "module_member_description": "Apply a function to 1-D slices along the given axis.",
    "parameters": [...],
    "returns": {
        "type": "ndarray",
        "description": "The output array...",
        "additional_information": "N/A"
    },
    "examples": [...],
    "additional_notes": {
        "supplementary_information": [...],
        "edge_cases": [...]
    }
}
```

---

## Scraped Documentation Layouts

The web scraper automatically detects three documentation layouts:

| Layout | Description | Example Libraries |
|--------|-------------|-------------------|
| `per_member` | One HTML page per API member | pandas, scikit-learn |
| `per_module` | One page per module/class, multiple members | PyTorch, pygame |
| `per_page` | Single page with all APIs | XGBoost |

Detection is based on URL patterns and fragment structure.

---

## Dependencies

```
# Core
requests
beautifulsoup4
lxml

# PDF Processing
pymupdf (fitz)

# Embeddings
sentence-transformers
numpy

# LLM
openai

# Async
aiohttp
asyncio
```

---

## Error Handling

- **Missing members**: Logged as warnings, processing continues
- **Network failures**: Retried with exponential backoff
- **LLM failures**: Skipped with warning, raw doc preserved
- **URL preprocessing failures**: Falls back to raw text
- **Missing OPENAI_API_KEY**: Auto-skips LLM steps (not an error)

---

## Performance Tips

1. Use `semantic_mode="never"` for well-structured docs (faster)
2. Use `ConcurrentDocExtractor` for batch processing (10x+ speedup)
3. Process modules in batches to amortize embedding model loading
4. Enable embedding caching with `cache_dir` parameter
5. Use `per_member` layout detection to skip extraction step when possible
6. Use `--skip-llm` for quick testing without API costs
