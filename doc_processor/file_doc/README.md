# PDF Documentation Extraction Module

> `doc_processor/file_doc/`

This module provides a complete pipeline for extracting API reference documentation from PDF files. It implements a sophisticated **coarse-to-fine retrieval** strategy using a combination of structural analysis, lexical matching, and semantic search with lexical verification.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Module Reference](#module-reference)
   - [pdf_localizer.py](#pdf_localizerpy)
   - [pipeline_pdf.py](#pipeline_pdfpy)
   - [signature.py](#signaturepy)
   - [multiline_signature.py](#multiline_signaturepy)
   - [embeddings.py](#embeddingspy)
   - [hybrid_search.py](#hybrid_searchpy)
   - [chunk_selector.py](#chunk_selectorpy)
4. [Data Structures](#data-structures)
5. [Processing Pipeline](#processing-pipeline)
6. [Usage Examples](#usage-examples)
7. [Configuration](#configuration)

---

## Overview

The PDF extraction pipeline solves the problem of locating and extracting documentation for specific API members (classes, functions, methods) from large PDF documentation files.

### Key Challenges Addressed

| Challenge | Solution |
|-----------|----------|
| Multi-column layouts | X-coordinate histogram analysis for reading order |
| Code blocks vs prose | Font-based detection (monospace families) |
| Multi-line API signatures | Heuristic-based signature joining |
| Equation extraction | OCR (pix2tex) or character-grid reconstruction |
| Finding relevant sections | Hybrid search (line-level matching + semantic) |
| Similar API names (Conv1d vs Conv2d) | Lexical name verification with tiered FQN matching |
| Knowing where to stop | Type-aware peer signature detection with code example filtering |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        PDF Documentation File                           │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      PDFSectionizer (pdf_localizer.py)                  │
│  • Parse PDF structure (TOC, outline)                                   │
│  • Extract text with font metadata                                      │
│  • Handle two-column layouts                                            │
│  • Detect code blocks, equations, tables                                │
│  • Join multi-line signatures                                           │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                           List[Section]
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                   APIReferenceLocator (chunk_selector.py)               │
│  • Build section hierarchy tree                                         │
│  • Find API reference sections by title keywords                        │
│  • Prune candidate sections to API-relevant subset                      │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                      Candidate Sections (reduced)
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     MemberExtractor (pipeline_pdf.py)                   │
│  • For each API member:                                                 │
│    1. Lexical search: section_match_score (line-level tiered matching)  │
│    2. Semantic search: paragraph-level max pooling + length penalty     │
│    3. Cross-section evaluation with lexical name verification           │
│    4. Anchor finding (regex + semantic with FQN bonus)                  │
│    5. "Anchor and Expand" with type-aware stop signals                  │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                     Extracted Documentation Text
```

---

## Module Reference

### pdf_localizer.py

**Purpose**: Low-level PDF parsing and text extraction with layout awareness.

#### Classes

##### `Section`
```python
@dataclass
class Section:
    id: str              # Unique identifier (e.g., "toc-0-1" or "heading-2-42")
    title: str           # Section heading text
    level: int           # Hierarchy level (0 = top-level)
    page_start: int      # First page (0-indexed)
    page_end: int        # Last page (exclusive)
    text_raw: str        # Original extracted text
    text_norm: str       # Normalized text (for matching)
    path: List[str]      # Breadcrumb path ["Parent", "Child"]
    children: List[Section]  # Child sections in tree
```

##### `PDFSectionizer`
Main class for PDF analysis and text extraction.

```python
class PDFSectionizer:
    def __init__(self, pdf_path: str, use_equation_ocr: bool = False):
        """
        Args:
            pdf_path: Path to PDF file.
            use_equation_ocr: If True, use pix2tex LaTeX OCR for equations.
                              If False, use character-grid heuristic.
        """

    def sectionize(self) -> Tuple[List[Section], List[Section]]:
        """Parse PDF and return (flat list, tree roots)."""
    
    def toc_end_page(self) -> int:
        """Estimate where Table of Contents ends."""
```

---

### pipeline_pdf.py

**Purpose**: High-level orchestration of the extraction pipeline with hybrid search strategies.

**Note**: `MemberExtractorConfig` is now defined in `extraction_utils.py` and shared across both PDF and web extraction pipelines.

#### Classes

##### `StopSignalMatcher`
Type-aware peer signature detection with fallback strategy.

```python
class StopSignalMatcher:
    """
    Two-phase matching:
        Phase 1 (Primary): Type-specific patterns
            - CLASS: Stop at other classes/functions
            - METHOD: Stop at sibling methods of same class
            - FUNCTION: Stop at other functions/classes
        
        Phase 2 (Fallback): Broader patterns if primary fails
    
    Features:
        - Code example filtering (ignores >>>, assignments before parentheses)
        - Flexible signature patterns (handles "class X", FQN, short names)
        - Pre-scan to determine if fallback patterns needed
    """
    
    def __init__(
        self, 
        peer_signatures: List[str], 
        target_member_type: str = "function",  # "class", "method", "function"
        target_api_name: str = ""
    ):
        """Build type-aware stop patterns with fallback support."""
    
    def checks_stop(self, line: str) -> Tuple[bool, bool]:
        """
        Returns (matched, is_high_priority) where:
            - matched: True if a stop pattern was matched
            - is_high_priority: True for real definitions, False for code examples
        """
```

##### `PDFExtractor`
"Anchor and Expand" extraction with priority-based stop handling.

```python
class PDFExtractor:
    def __init__(self, max_chars: int = 25000):
        """Initialize with max extraction length."""
    
    def extract_by_line_expansion(
        self, 
        section: Section, 
        start_char_idx: int, 
        stop_matcher: Optional[StopSignalMatcher] = None
    ) -> Tuple[str, List[int]]:
        """
        Expand from anchor until stop signal or limit.
        
        Priority handling:
            - High-priority stop (real definition): Stop immediately
            - Low-priority stop (code example): Record as fallback, continue
            - If max_chars reached with only low-priority stops: truncate at fallback
        """
```

##### `MemberExtractor`
Main extraction orchestrator with multi-strategy search.

```python
class MemberExtractor:
    """
    Extraction Strategies (in order):
        1. Direct anchor search in text_raw using tiered needles
        2. Fallback anchor search across all ranked sections
        3. Regex anchor fallback
        4. Semantic window search with:
            - Cross-section evaluation (top 2-3 sections)
            - Paragraph-based max pooling (prevents content accumulation bias)
            - Length penalty for window scoring
            - **Window-level name presence bonus** (FQN or Parent.Name in window)
            - Fine-grained lexical name verification with tiered FQN matching
        5. Final fallback: top section start
    """
    
    def _semantic_window_search(
        self, 
        embedder: EmbeddingModel, 
        sec_obj: Section, 
        q_vec: np.ndarray,
        sig_query_vec: Optional[np.ndarray] = None,
        api_name: str = ""
    ) -> Optional[Tuple[int, float]]:
        """
        Two-stage semantic search with lexical verification at BOTH stages:
        
        Stage 1 (Coarse - Window Selection):
            - Paragraph-based max pooling
            - Length penalty
            - **Name presence bonus**: +0.15 if FQN found, +0.105 if Parent.Name found
            
        Stage 2 (Fine - Line Selection):
            - Line-level semantic similarity
            - Tiered FQN matching bonus (1.0/0.7/0.3 × 0.30)
            - Signature structure bonus
        
        Returns (anchor_position, fine_score) for cross-section comparison.
        """
```

#### Top-Level Function

```python
def extract_api_docs_from_pdf(
    pdf_path: str,
    members: List[MemberInput],
    out_json_path: str,
    model_name: str = "intfloat/e5-base-v2",
    cache_dir: str = None,
    per_api_txt_dir: Optional[str] = None,
    member_cfg: MemberExtractorConfig = MemberExtractorConfig(),
    peer_signatures: Optional[Dict[str, List[str]]] = None
) -> Dict[str, Any]:
    """
    Complete pipeline entry point.
    
    Args:
        pdf_path: Path to PDF documentation file.
        members: List of API members to extract.
        out_json_path: Path to save JSON results.
        model_name: Sentence transformer model.
        cache_dir: Optional embedding cache directory.
        per_api_txt_dir: Optional directory for per-member .txt files.
        member_cfg: Extraction configuration.
        peer_signatures: Dict mapping api_name -> list of peer signatures.
    
    Returns:
        Dict mapping api_name -> extraction result.
    """
```

---

### signature.py

**Purpose**: Build search patterns and queries for API members.

#### Data Classes

##### `MemberInput`
```python
@dataclass
class MemberInput:
    api_name: str                    # "torch.nn.Conv1d"
    signature_variants: List[str]    # ["Conv1d(in_channels, ...)"]
    docstring: Optional[str] = None  # Source code docstring
    member_type: str = "function"    # "class", "function", "method", "variable"
```

#### Functions

| Function | Purpose |
|----------|---------|
| `build_signature_patterns(member)` | Compile regex patterns for anchor finding |
| `build_lexical_needles(member)` | Build tiered dict: `{"exact": [...], "prefix": [...], "anchor": [...]}` |
| `build_semantic_query(member, model_name)` | Build natural language query for section ranking |
| `build_signature_query(member, model_name)` | Build structural query for signature line finding |
| `build_passage_text(text, model_name)` | Format passage for embedding (e5 prefix) |

##### Tiered Needle Generation
```python
def build_lexical_needles(member: MemberInput) -> Dict[str, List[str]]:
    """
    Returns:
        {
            "exact": ["torch.nn.Conv1d(in_channels, out_channels, ...)"],  # Full signature
            "prefix": ["torch.nn.Conv1d(", "Conv1d("],                     # Name + paren
            "anchor": ["torch.nn.Conv1d", "Conv1d"]                        # Just names
        }
    """
```

---

### hybrid_search.py

**Purpose**: Line-level matching and scoring functions.

#### Functions

```python
def section_match_score(
    text: str, 
    needles: Dict[str, List[str]], 
    section_title: str = ""
) -> Tuple[float, int, int, str]:
    """
    Compute section-level match score using line-level matching.
    
    Returns:
        (score, line_idx, char_offset, match_type)
    
    Match types: "exact", "prefix", "anchor", "none"
    """

def find_needle_in_lines(
    text: str, 
    needles: Dict[str, List[str]],
    early_stop: bool = True,
    prioritize_outside_code_blocks: bool = True
) -> Tuple[int, int, float, str]:
    """
    Find best needle match by scanning lines.
    
    Priority:
        1. Matches OUTSIDE code blocks (real definitions)
        2. Matches INSIDE code blocks (fallback for edge cases)
    
    Within each category:
        1. "exact" needles: score 100
        2. "prefix" needles: score 80
        3. "anchor" needles: score 50
    
    Position bonus: +10 if match at line start (position < 20 chars)
    """

def normalize_for_match(text: str) -> str:
    """Case-insensitive, whitespace-normalized text for matching."""

def cosine_similarity(q: np.ndarray, X: np.ndarray) -> np.ndarray:
    """Compute cosine similarity (assumes L2-normalized inputs)."""
```

---

### chunk_selector.py

**Purpose**: Identify API reference sections in document structure.

#### Classes

##### `APIReferenceLocator`
```python
class APIReferenceLocator:
    """
    Locate candidate sections containing API reference documentation.
    
    Keywords searched:
        - "api reference", "python api", "reference guide"
        - "python library reference", "api documentation"
    """
    
    @classmethod
    def collect_candidates(
        cls, 
        sections: List[Section], 
        max_depth: int = 2,
        toc_end_page: int = 0
    ) -> List[Section]:
        """Find API-relevant sections after the TOC."""
```

---

## Data Structures

### Extraction Result

Each extracted member returns:

```python
{
    "api_name": "torch.nn.Conv1d",
    "text": "class torch.nn.Conv1d(in_channels, out_channels, ...)\n\nApplies a 1D convolution...",
    "pages": [189, 190, 191],
    "section_path": ["torch", "torch.nn", "Conv1d"],
    "scores": {
        "lexical": 85.0,
        "semantic": 0.87,
        "final": 12.5,
        "match_type": "exact"  # or "prefix", "anchor", "semantic_window", "fallback"
    },
    "warning": null  # or "No direct anchor found; semantic window fallback used."
}
```

---

## Processing Pipeline

### Phase 1: Document Parsing
```
PDF -> PDFSectionizer.sectionize() -> List[Section]
```
- Extracts text with font metadata
- Handles two-column layouts
- Detects and formats code blocks
- Joins multi-line signatures
- Optionally OCRs equations to LaTeX

### Phase 2: Candidate Selection
```
List[Section] -> APIReferenceLocator.collect_candidates() -> Reduced List[Section]
```
- Builds section hierarchy tree
- Finds sections with API-related titles
- Filters out TOC and front matter

### Phase 3: Member Extraction (per member)
```
MemberInput -> MemberExtractor.extract() -> Dict
```

1. **Pre-compute Lexical Scores**: `section_match_score` for all sections
2. **Auto-Gate Decision**: Determine if semantic search needed based on lexical confidence
3. **Semantic Scoring** (if enabled): Window embeddings with paragraph max-pooling
4. **Section Ranking**: Combine lexical + semantic scores based on mode
5. **Anchor Finding**: 
    - Strategy 1: Direct tiered needle search in text_raw
    - Strategy 2: Fallback search across all ranked sections
    - Strategy 3: Regex patterns
    - Strategy 4: Semantic window search with **dual-layer lexical verification**:
        - Coarse: Window-level name presence bonus
        - Fine: Line-level tiered FQN matching
    - Strategy 5: Top section start (last resort)
6. **Expansion**: Type-aware stop signal matching with code example filtering
7. **Snippet Boost**: Optional final semantic boost for result validation

---

## Usage Examples

### Basic Extraction

```python
from doc_processor.file_doc.pipeline_pdf import extract_api_docs_from_pdf
from doc_processor.file_doc.extraction_utils import MemberExtractorConfig
from doc_processor.file_doc.signature import MemberInput

members = [
    MemberInput(
        api_name="torch.nn.Conv1d",
        signature_variants=["torch.nn.Conv1d(in_channels, out_channels, kernel_size, ...)"],
        member_type="class"
    )
]

# With peer signatures for accurate stop detection
peer_signatures = {
    "torch.nn.Conv1d": [
        "torch.nn.Conv2d(in_channels, out_channels, kernel_size, ...)",
        "torch.nn.Conv3d(in_channels, out_channels, kernel_size, ...)"
    ]
}

results = extract_api_docs_from_pdf(
    pdf_path="pytorch_docs.pdf",
    members=members,
    out_json_path="extracted.json",
    per_api_txt_dir="per_api/",
    member_cfg=MemberExtractorConfig(semantic_mode="auto"),
    peer_signatures=peer_signatures
)
```

### Semantic-Only Mode

```python
# For PDFs where lexical matching struggles (OCR errors, non-standard formatting)
config = MemberExtractorConfig(semantic_mode="only")

results = extract_api_docs_from_pdf(
    pdf_path="scanned_docs.pdf",
    members=members,
    out_json_path="extracted.json",
    member_cfg=config
)
```

### Pure Lexical Mode (Fast)

```python
# For high-quality PDFs with exact signature matches
config = MemberExtractorConfig(semantic_mode="never")

results = extract_api_docs_from_pdf(
    pdf_path="clean_docs.pdf",
    members=members,
    out_json_path="extracted.json",
    member_cfg=config
)
```

---

## Configuration

### Semantic Modes

| Mode | Behavior | Use Case |
|------|----------|----------|
| `"auto"` | Lexical-first; semantic if low confidence | Default, balanced |
| `"never"` | Pure lexical matching | Fast, clean PDFs |
| `"always"` | Always compute semantic | Noisy PDFs |
| `"only"` | Skip lexical, semantic ranking | OCR'd or non-standard PDFs |

### Performance Tuning

| Setting | Impact | Recommendation |
|---------|--------|----------------|
| `semantic_mode="never"` | Fastest, lexical-only | Clean PDFs with exact signatures |
| `topK_sections=10` | Faster, less fallback | Confident section structure |
| `cache_dir` set | Avoid re-encoding | Always set for repeated runs |
| `max_workers=1` | Lower memory | Limited RAM systems |
| `window_chars=2000` | Faster semantic | Shorter API docs |

### GPU Memory

- **Embedding Model** (`e5-base-v2`): ~400MB VRAM
- **LaTeX OCR** (`pix2tex`): ~800MB VRAM
- **Batch encoding**: Scales with batch size (64 on GPU, 8 on CPU)

---

## Dependencies

```
PyMuPDF (fitz)        # PDF parsing
sentence-transformers # Semantic embeddings
numpy                 # Numerical operations
rapidfuzz             # Fuzzy string matching
torch                 # GPU support
pix2tex               # Optional: LaTeX OCR
```

---

## File Structure

```
doc_processor/file_doc/
├── __init__.py
├── pdf_localizer.py       # PDF parsing, Section extraction
├── pipeline_pdf.py        # Main extraction pipeline
├── extraction_utils.py    # Shared config (MemberExtractorConfig), utilities
├── signature.py           # MemberInput, pattern/query builders
├── multiline_signature.py # Multi-line signature joining
├── embeddings.py          # GPU-accelerated embeddings
├── hybrid_search.py       # Line-level matching, scoring
├── chunk_selector.py      # API section locator
└── README.md              # This documentation
```

### MemberExtractorConfig (extraction_utils.py)

Shared configuration for both PDF and web extraction pipelines:

```python
from doc_processor.file_doc.extraction_utils import MemberExtractorConfig

config = MemberExtractorConfig(
    semantic_mode="auto",       # "auto", "never", "always", "only"
    lexical_sigma_k=0.25,       # Lexical confidence threshold
    lexical_margin_min=0.20,    # Top-1 vs top-2 margin
    topK_sections=50,           # Candidate sections for refinement
    window_chars=3000,          # Semantic window size
    window_stride=2000,         # Window overlap
    max_workers=8,              # Parallel workers
    snippet_boost_weight=0.5    # Snippet-level semantic boost
)
```

**semantic_mode options:**
- `"auto"`: Lexical-first; use semantic if lexical confidence is low (default)
- `"never"`: Pure lexical matching only (fastest)
- `"always"`: Always compute semantic scores
- `"only"`: Skip lexical, use semantic ranking only