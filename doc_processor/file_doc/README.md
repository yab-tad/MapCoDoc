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
| Code blocks vs prose | Font-based detection (monospace families) + visual code-block rectangles |
| Multi-line API signatures | Heuristic-based signature joining (SignatureJoiner) |
| Merged-line signatures (PDF artifact) | Page-level post-processing splits return-type values and next-method signatures that share the same y-coordinate in the PDF |
| Equation extraction | OCR (pix2tex) or character-grid reconstruction |
| Finding relevant sections | Hybrid search (line-level matching + semantic) |
| Similar API names (Conv1d vs Conv2d) | Lexical name verification with tiered FQN matching |
| Knowing where to stop | Type-aware peer signature detection with sibling-class scoping and alphabetically ordered fallback |
| False-positive anchor matches | Definition-position check (pos ≤ 25, doc-keyword prefix only), valid-suffix check (`:`, `(`, or end-of-line), min-length filter for short ambiguous anchors |
| Members absent from PDF (private/internal) | Max-lexical-signal guard suppresses semantic and cross-section search when no lexical evidence exists in any section |
| Methods documented in a different PDF section than their parent class | Strategy 5a cross-section fallback searches beyond the parent class's primary section |

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
│    3. Anchor finding via find_needle_in_lines (fence-aware, guarded)    │
│    4. Cross-section fallback for inherited/split-section methods        │
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

**Merged-line post-processing**

Sphinx-generated PDFs sometimes place a method's return-type value and the next
method's signature at the same y-coordinate, causing the extractor to output
them on a single line (e.g. `_SparkXGBParams      transform(dataset, params=None)`).
Three cascading fixes are applied during `_extract_page_text`:

1. **Per-span** (Phase 4): when a gap ≥ 5 character-widths precedes a callable
   signature in a single span, a newline is inserted instead of spaces.
2. **Post-row** (Phase 4): after row text assembly, a regex splits multi-span
   callables where the method name and `(params)` are in separate PDF spans.
3. **Page-level** (cleanup): applied to the fully assembled page `raw` string.
   Pattern: `r'^([^(\n]+?)\s{5,}([a-z_]\w*\((?!https?://))'`
   — requires no space before `(` (excludes type annotations like `param (Type)`)
   and excludes URL hyperlinks (excludes return values like `bool(https://...)`).

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
            - METHOD: Stop at sibling methods of the SAME parent class only
            - FUNCTION: Stop at other functions/classes

        Phase 2 (Fallback): Broader patterns if primary fails
            - CLASS fallback uses own methods/inherited members as boundaries,
              sorted alphabetically so the first alphabetical method in the
              document fires — matching the typical alphabetical listing in docs.

    Key design decisions:
        - All stop-signal patterns are anchored to the LINE START with ``^\s*``
          to prevent false matches against Sphinx parameter-description list items
          such as "- feature_names (Sequence[str] | None) - Set names for features."
        - For Pattern 0b (short-name signatures WITH parameters), ``(?:^|\\s)`` is
          used instead of ``^\s*`` so that stop signals fire even when the signature
          appears mid-line after a previous method's return type (PDF merged-line
          artifact). The full parameter list is a strong discriminator against
          false positives in this case.
        - Type-aware optional keyword prefix: class peers use ``(?:class\\s+)?``,
          method/property peers use ``(?:property\\s+|classmethod\\s+|staticmethod\\s+)?``.
        - Empty-paren properties (e.g. ``feature_names()``) also get a no-paren
          variant pattern (``name\\b(?!\\s*[\\(=])``) so that stop signals fire on
          PDF lines like ``property feature_names:`` which have no ``()`` suffix.
        - ``_looks_like_code_example`` detects Sphinx bullet-list parameter items
          (``^[-*•]\\s+\\w``) and treats them as low-priority (non-definitive).
        - ``_classify_peer`` returns ``Optional[bool]``: ``True`` = PRIMARY,
          ``False`` = FALLBACK, ``None`` = EXCLUDED (e.g. methods of a different
          class when the target is a method, to prevent cross-class false stops).
        - Pre-scan determines if fallback patterns are needed before scanning.
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
                                or parameter-list items
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
        Expand from start_char_idx until stop signal or limit.

        start_char_idx is used directly (no snap-to-line-start via rfind).
        This allows extraction to begin at an exact mid-line position when
        find_anchor_in_raw returns an exact-tier match inside a merged PDF line
        (e.g. the signature starts after a previous method's return type).

        Priority handling:
            - High-priority stop (real definition): Stop immediately
            - Low-priority stop (code example or inside fence): Record as fallback, continue
            - If max_chars reached with only low-priority stops: truncate at fallback
        """
```

##### `MemberExtractor`
Main extraction orchestrator with multi-strategy search.

```python
class MemberExtractor:
    """
    Extraction Strategies (in order):
        1. Direct anchor search in text_raw using find_needle_in_lines
           (prioritize_outside_code_blocks=False disables fence-state guessing
            for scoped slices; at_definition_pos + valid_suffix guards filter
            false positives instead)
        2. Fallback anchor search across all ranked sections
        3. Regex anchor fallback
        4. Semantic window search — SKIPPED if max_raw_lex == 0 (member has no
           lexical signal in any section, meaning it is absent from the PDF)
        5a. Cross-section fallback (scoped members only): searches adjacent
            sections when the member's documentation is in a different PDF
            section from its parent class (e.g. inherited sklearn methods).
            Also suppressed when max_raw_lex == 0.
        5b. Section-start fallback (non-scoped only): classes and functions use
            the top-ranked section start as a last resort.

    Parent-class scoping:
        - Methods are scoped to [parent_class_anchor, next_class_anchor].
        - If parent class not in class_anchors → immediate not_found, to prevent
          cross-class false matches.
        - Partial class name matching is disabled when the parent class's immediate
          containing module starts with '_' (private module guard).
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
    api_name: str                       # "torch.nn.Conv1d"
    signature_variants: Dict[str, str]  # Named variants from parameter_analysis.py:
                                        #   {'full':            'Conv1d(in_channels: int, ...)',
                                        #    'no_types':        'Conv1d(in_channels, ...)',
                                        #    'defaults_only':   'Conv1d(in_channels, out_channels, ...)',
                                        #    'no_special':      ...,  # no / or *
                                        #    'no_slash':        ...,
                                        #    'no_asterisk':     ...,
                                        #    'no_types_no_slash':    ...,
                                        #    'no_types_no_asterisk': ...}
                                        # Empty dict for variables or members with no stored sig.
    docstring: Optional[str] = None     # Source code docstring (semantic query hint)
    member_type: str = "function"       # "class", "function", "method", "variable"
```

`signature_variants` is sourced from the `DBSignature` table (`variant` + `signature_text` columns) and populated via `{s.variant: s.signature_text for s in member.signatures}` in `query.py`.

#### Functions

| Function | Purpose |
|----------|---------|
| `build_signature_patterns(member)` | Compile regex patterns for anchor finding |
| `build_lexical_needles(member)` | Build tiered dict: `{"exact": [...], "anchor": [...]}` |
| `build_semantic_query(member, model_name)` | Build natural language query for section ranking |
| `build_signature_query(member, model_name)` | Build structural query for signature line finding |
| `build_passage_text(text, model_name)` | Format passage for embedding (e5 prefix) |
| `_first_variant(variants)` | Return highest-priority signature string from a named-variant dict |

##### Two-Tier Needle Generation

`build_lexical_needles` returns **two** tiers (the old "prefix" tier has been removed):

```python
def build_lexical_needles(member: MemberInput) -> Dict[str, List[str]]:
    """
    Returns:
        {
            "exact":  [
                # Processed in _VARIANT_PRIORITY order:
                # ('full', 'no_types', 'defaults_only', 'no_special', ...)
                # For each variant, multiple qualified forms are generated:

                # --- classes ---
                "class torch.nn.Conv1d(in_channels: int, out_channels: int, ...)",  # FQN + class kw (web)
                "torch.nn.Conv1d(in_channels: int, out_channels: int, ...)",         # FQN (web)
                "class Conv1d(in_channels: int, out_channels: int, ...)",            # short + class kw (PDF)
                "Conv1d(in_channels: int, out_channels: int, ...)",                  # short (PDF)
                # ... same pattern repeated for no_types, defaults_only, etc.

                # --- methods (e.g. pandas.DataFrame.add) ---
                "add(other, axis='columns', level=None, fill_value=None)",           # short (web)
                "DataFrame.add(other, axis='columns', level=None, fill_value=None)", # class-qual (PDF)
                "pandas.DataFrame.add(other, axis='columns', ...)",                  # FQN (URL match)
                # ... repeated for no_types, defaults_only variants
            ],
            "anchor": [
                "Conv1d",               # short name  (fallback, no-arg properties)
                "torch.nn.Conv1d",      # FQN         (matches URL fragment in web docs)
                # For methods:
                "add",
                "pandas.DataFrame.add",
                "DataFrame.add"         # class-qualified anchor
            ]
        }
    """
```

**Design rationale for removing the "prefix" tier:**
The old `prefix` tier (`Conv1d(`, `torch.nn.Conv1d(`) was the primary source of false-positive stop-signal matches. For example, `feature_names(` matched `- feature_names (Sequence[str])` in a Sphinx parameter-list item. With named variants covering `no_types` and `defaults_only`, the `exact` tier already handles the cases where the prefix tier was useful, with higher specificity and no false-positive risk.

**Variant priority** (`_VARIANT_PRIORITY` constant):
```
['full', 'no_types', 'defaults_only', 'no_special',
 'no_slash', 'no_asterisk', 'no_types_no_slash', 'no_types_no_asterisk']
```
`full` (most specific, with type annotations) is tried first; `no_types` second (matches PDFs that strip annotations); `defaults_only` third (maximally compact form).

---

### hybrid_search.py

**Purpose**: Line-level matching and scoring functions.

#### Functions

```python
def section_match_score(
    text: str,
    needles: Dict[str, List[str]],
    section_title: str = "",
    member_type: str = "",          # NEW: passed through to find_needle_in_lines
) -> Tuple[float, int, int, str]:
    """
    Compute section-level match score using line-level matching.

    Returns:
        (score, line_idx, char_offset, match_type)

    Match types: "exact", "anchor", "none"
    """

def find_needle_in_lines(
    text: str,
    needles: Dict[str, List[str]],
    early_stop: bool = True,
    prioritize_outside_code_blocks: bool = True,
    initial_inside_fence: bool = False,   # initial fence state for scoped text slices
    member_type: str = "",
) -> Tuple[int, int, float, str]:
    """
    Find best needle match by scanning lines with context-aware scoring.

    Tier priority:
        1. "exact" needles: base score 100
        2. "anchor" needles: base score 50
        ("prefix" tier removed — it caused false-positive stop signals)

    Per-line score adjustments (applied via _line_context_score):
        +20  Line starts with "class <name>"     (class definition)
        +20  Line starts with "property <name>"  (standalone property definition)
        +10  Line starts with "classmethod" or "staticmethod"
        +15  Line ends with canonical URL "(https://...)"  (Sphinx web docs)
        +10  Line contains return-type annotation "-> Type"  (PDF docs)
        -25  Line contains a PDF page cross-reference "(#page=N)"
        -30  Line starts with a bullet character "- " or "* "
             (Sphinx parameter-list item — strong false-positive guard)

    Anchor-tier guards (in addition to scoring):
        - Min-length filter: anchors ≤ 2 chars with no dots are skipped.
        - Definition-position: anchor must be at pos ≤ 25 with only a
          doc-keyword prefix (property/class/classmethod/staticmethod).
        - Valid-suffix: after the name, line must end, start with ':', or start
          with '(' — prevents matching type annotations like "param (Type)".
        - Standalone bonus: +15 if the normalised line IS the member name
          (bare attribute "best_score" vs constructor parameter mention).
        - Code-example detection (_looks_like_code_example) is ALWAYS called
          regardless of prioritize_outside_code_blocks, so assignment patterns
          like "config = xgb.get_config()" are never treated as definitions.

    Code-example-only results: score is capped at 1.0 (signals to callers such
    as find_anchor_in_raw that no definition-position match was found).

    Position bonus: +10 if match starts within first 20 chars of the line.

    Tie-breaking: later match wins when scores are equal (>= comparison).

    Early-stop: fires only on an unpenalised exact match at position < 20
    (context_adj >= 0 guard prevents stopping on a penalised list item).
    """

def _line_context_score(line_stripped: str) -> float:
    """
    Compute a score adjustment for a candidate line based on its documentation
    context. Called once per line inside find_needle_in_lines.

    Bonuses: +20 class keyword, +20 property keyword, +10 classmethod/
    staticmethod, +15 canonical URL terminus, +10 return-type annotation.
    Penalties: -25 PDF page cross-reference (#page=N), -30 bullet-list item.
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
                     # or "Found outside primary class scope; likely split-section method."
                     # or "Member not found in PDF; may be inherited and documented only
                     #     under its original parent class."
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
5. **Anchor Finding** via `find_anchor_in_raw` (uses `find_needle_in_lines` with
   `prioritize_outside_code_blocks=False`; initial fence state computed from
   text before `scoped_start` to correctly handle mid-fence scoped slices):
    - Strategy 1: Direct tiered needle search in text_raw (scoped to class region)
    - Strategy 2: Fallback search across all ranked sections (same scope)
    - Strategy 3: Regex patterns (scoped)
    - Strategy 4: Semantic window search — skipped if `max_raw_lex == 0`
    - Strategy 5a: Cross-section fallback for scoped members — skipped if `max_raw_lex == 0`
    - Strategy 5b: Section-start fallback for non-scoped members (classes/functions)
6. **Expansion**: Type-aware stop signal matching with code example and fence filtering
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
        # signature_variants is Dict[str, str]: variant_name -> signature_text.
        # Keys match the variation names produced by parameter_analysis.Parameter.
        signature_variants={
            "full":          "Conv1d(in_channels: int, out_channels: int, kernel_size: _size_1_t, ...)",
            "no_types":      "Conv1d(in_channels, out_channels, kernel_size, stride=1, ...)",
            "defaults_only": "Conv1d(in_channels, out_channels, kernel_size)",
        },
        member_type="class"
    )
]

# peer_signatures: values are flat lists of signature strings (exact needles only).
# build_lexical_needles() + .get("exact") is the recommended way to produce these.
peer_signatures = {
    "torch.nn.Conv1d": [
        "class torch.nn.Conv2d(in_channels: int, out_channels: int, ...)",
        "Conv2d(in_channels, out_channels, kernel_size)",
        "class torch.nn.Conv3d(in_channels: int, out_channels: int, ...)",
        "Conv3d(in_channels, out_channels, kernel_size)",
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