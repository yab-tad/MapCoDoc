"""
Hybrid Search Functions for PDF Documentation Extraction

This module provides scoring functions for lexical matching of API signatures
within PDF-extracted text. It implements a line-level matching strategy that
finds signatures as substrings within lines (since multiline signatures are
pre-joined by SignatureJoiner).

Key Design Principles:
    1. Signatures exist as substrings within lines (not exact line matches)
    2. Lines may have leading content (class prefix, indentation) and trailing
       content (return types, description start)
    3. Matching is case-insensitive with whitespace normalization
    4. Tiered matching: exact > anchor patterns

Functions:
    - normalize_for_match: Normalize text for case/whitespace-insensitive matching
    - find_needle_in_lines: Line-level substring matching with tiered priorities
    - section_match_score: Compute section-level scores based on line matching
    - fuzzy_line_score: Improved fuzzy matching using partial_ratio on lines
    - detect_reference_pattern: Identify reference mentions vs primary definitions
    - cosine_similarity: Vector similarity for semantic search
"""

from __future__ import annotations
import re
import numpy as np
from typing import List, Dict, Tuple, Optional
from rapidfuzz import fuzz


# Tokens that PDF documentation engines insert into otherwise-clean signature
# lines. They must be stripped per-line BEFORE matching so that needles built
# from canonical signatures still substring-match the annotated text.
#
#   (#page=N) - Sphinx page-anchor cross-references (SQLAlchemy)
#   (Keyword-only parameters separator (PEP 3102)) - pandas annotation on the bare ``*`` separator
#
# The original line text is preserved for context-scoring heuristics that may
# legitimately want to see those tokens (e.g. the "page-reference at end of line" penalty in _line_context_score).
_NOISE_TOKEN_RES = (
    re.compile(r'\(#page=\d+\)'),
    re.compile(r'\s*\(Keyword-only parameters separator\s*\(PEP 3102\)\)'),
)
def _strip_noise_tokens(text: str) -> str:
    """Strip all known PDF-noise tokens from a line for matching purposes."""
    for pat in _NOISE_TOKEN_RES:
        text = pat.sub('', text)
    return text

# =============================================================================
# Text Normalization
# =============================================================================

def normalize_for_match(s: str) -> str:
    """
    Normalize text for case-insensitive, whitespace-tolerant matching.
    
    Transformations:
        - Convert to lowercase
        - Collapse multiple whitespace to single space
        - Strip leading/trailing whitespace
    
    Args:
        s: Input string to normalize.
        
    Returns:
        Normalized string suitable for substring matching.
        
    Example:
        >>> normalize_for_match("  torch.nn.Conv1d(  in_channels,  out_channels )")
        'torch.nn.conv1d( in_channels, out_channels )'
    """
    return ' '.join(s.lower().split())


# =============================================================================
# Code Block Utilities
# =============================================================================

def _looks_like_code_example(line: str, match_pos: int = -1) -> bool:
    """
    Check if a line looks like a code example or parameter list rather than a standalone API definition.
    
    Detects:
        - REPL prompts: >>>, ...
        - Assignment patterns before a call: m = Conv2d(...)
        - Sphinx/Markdown bullet-list parameter items: "- name (Type) - desc"
    
    Args:
        line: The line to check (stripped).
        match_pos: Position where the needle match was found (-1 if unknown).
        
    Returns:
        True if this looks like a code example, False if it looks like a definition.
    """
    # REPL prompts
    if line.startswith('>>>') or line.startswith('...'):
        return True
    
    # Sphinx/Markdown parameter-description list items:
    #   "- feature_names (Sequence[str] | None) - Set names for features."
    #   "* weight (float) - per-sample weight"
    if re.match(r'^[-*•►▪]\s+\w', line):
        return True
    
    # Check for assignment pattern BEFORE the signature/match
    # Default params have '=' INSIDE parens, assignments have '=' BEFORE '('
    #
    # Code example:   "m = nn.Conv2d(16, 33)"      -> '=' at pos 2, '(' at pos 14 -> assignment
    # Real signature: "Conv2d(in_channels, stride=1)" -> '=' at pos 27, '(' at pos 6 -> NOT assignment
    
    # Assignment pattern before opening paren
    eq_pos = line.find('=')
    paren_pos = line.find('(')
    
    if eq_pos >= 0 and paren_pos >= 0:
        # '=' appears BEFORE '(' -> this is an assignment (code example)
        if eq_pos < paren_pos:
            return True
    elif eq_pos >= 0 and paren_pos < 0:
        # Has '=' but no '(' at all -> likely code example
        return True
    return False
    

# =============================================================================
# Line-Level Matching (Primary Strategy)
# =============================================================================

def _line_context_score(line_stripped: str) -> float:
    """
    Return a score adjustment for a candidate match line based on its documentation context.

    Positive adjustments reward lines that look like primary API definitions.
    Negative adjustments penalise lines that look like cross-references or
    parameter descriptions.

    Bonuses / penalties applied:
        +20  Line starts with ``class <name>``     (class definition line)
        +20  Line starts with ``property <name>``  (standalone property definition)
        +15  Line starts with ``method`` ``function`` ``attribute`` (SQLAlchemy / Sphinx-PDF doc-keyword prefixes)
        +10  Line starts with ``classmethod`` or ``staticmethod``
        +15  Line ends with a canonical URL in parentheses  (Sphinx web docs)
        +10  Line contains a return-type annotation ``-> Type``
        -25  Line contains a PDF page cross-reference ``(#page=N)``
        -30  Line starts with a bullet character (Sphinx parameter list item)
    """
    score = 0.0

    # --- Penalty: parameter/attribute list item ---
    if re.match(r'^[-*•►▪]\s+\w', line_stripped):
        score -= 30.0
        return score   # Early return: no bonuses apply to list items

    # --- Bonus: documentation keyword prefix ---
    if re.match(r'^class\s+\w', line_stripped, re.IGNORECASE):
        score += 20.0
    elif re.match(r'^property\s+\w', line_stripped, re.IGNORECASE):
        score += 20.0
    elif re.match(r'^(?:method|function|attribute)\s+\w', line_stripped, re.IGNORECASE):
        score += 15.0   # SQLAlchemy / Sphinx-PDF doc-keyword prefixes
    elif re.match(r'^(?:classmethod|staticmethod)\s+\w', line_stripped, re.IGNORECASE):
        score += 10.0

    # --- Bonus: canonical URL terminus (Sphinx web docs) ---
    # Definition lines end with "(https://...#member_name)"
    if re.search(r'\(https?://[^\s)]+\)\s*$', line_stripped):
        score += 15.0

    # --- Bonus: explicit return-type annotation ---
    if re.search(r'\)\s*->\s*\w', line_stripped):
        score += 10.0
        
    # --- Penalty: PDF page cross-reference at end of line ---
    # A page reference at the END of the line is almost certainly a cross-reference
    # (e.g. "Like update()(#page=176)"). When the page reference appears in the
    # middle of a qualifier (SQLAlchemy: "URL.(#page=1686)render_as_string(...)")
    # it is part of a real definition line, and the −25 penalty should not fire.
    if re.search(r'\(#page=\d+\)\s*$', line_stripped):
        score -= 25.0

    return score


def find_needle_in_lines(
    text: str,
    needles: Dict[str, List[str]],
    early_stop: bool = True,
    prioritize_outside_code_blocks: bool = True,
    member_type: str = "",
    initial_inside_fence: bool = False
) -> Tuple[int, int, float, str]:
    """
    Find the best needle match by scanning lines, prioritizing definition-like matches.

    Scoring model (base + adjustments):
        "exact"  needles: 100 base
        "anchor" needles:  50 base
        Position bonus:   +10 if match starts within the first 20 chars of the line
        Context bonuses/penalties from _line_context_score():
            +20  ``property <name>`` prefix
            +10  ``classmethod``/``staticmethod`` prefix
            +15  canonical URL terminus  (web docs)
            +10  ``-> ReturnType`` annotation
            -30  bullet-list item  (Sphinx parameter list)

    Tie-breaking: when two lines achieve equal final score the later line is
    preferred (``>=`` comparison).  Combined with the -30 parameter-list penalty,
    standalone definition lines naturally outscore same-name parameter mentions.

    Args:
        text: Section text to search (text_norm from Section, or combined_text slice).
        needles: Dict with keys "exact" and "anchor" from build_lexical_needles().
                 A "prefix" key is accepted but ignored (tier removed).
        early_stop: If True, return immediately on an unpenalised exact match
                    within the first 20 chars of a definition line.
        prioritize_outside_code_blocks: If True (default), prefer matches outside
                    code blocks; code-block matches are kept only as fallback.
        member_type: Optional member type string ('class', 'method', etc.).
                    Reserved for future per-type adjustments; currently the context
                    bonuses in _line_context_score are type-agnostic.

    Returns:
        (line_index, char_offset, score, match_type)
    """
    lines = text.split('\n')

    best_definition   = (-1, -1, 0.0, "none")
    best_code_example = (-1, -1, 0.0, "none")
    inside_fence = initial_inside_fence 

    # for line_idx, line in enumerate(lines):
    #     line_stripped = line.strip()
    #     line_norm = normalize_for_match(line)

    #     if len(line_norm) < 3:
    #         continue
    #     if line_stripped.startswith('```'):
    #         inside_fence = not inside_fence            
    #         continue

    #     # Compute context bonus/penalty once per line (shared across all needle tiers)
    #     context_adj = _line_context_score(line_stripped)
    
    for line_idx, line in enumerate(lines):
        line_stripped_orig = line.strip()
        if line_stripped_orig.startswith('```'):
            inside_fence = not inside_fence
            continue
        
        # SQLAlchemy and similar Sphinx-built PDFs interpolate "(#page=N)" between qualified-name parts, e.g. "method sqlalchemy.X.Y.(#page=1686)Z(...)".
        # Strip these tokens for matching only (the original line is retained for context-scoring heuristics that may want to see the page reference)
        line_stripped = _strip_noise_tokens(line_stripped_orig)
        line_norm = normalize_for_match(line_stripped)
        
        if len(line_norm) < 3:
            continue
        
        # Compute context bonus/penalty once per line (shared across all needle tiers)
        # Use the ORIGINAL line so cross-reference penalties still fire on standalone "see also" pages such as "Like update()(#page=176)" whose entire content is a cross-reference.
        context_adj = _line_context_score(line_stripped_orig)     
           
        force_code = inside_fence and prioritize_outside_code_blocks

        # ------------------------------------------------------------------
        # Priority 1: Exact signature matches (score base 100)
        # ------------------------------------------------------------------
        for needle in needles.get("exact", []):
            needle_norm = normalize_for_match(needle)
            if not needle_norm:
                continue
            pos = line_norm.find(needle_norm)
            if pos >= 0:
                start_bonus = 10 if pos < 20 else 0
                score = 100 + start_bonus + context_adj
                
                # Definition-start check: the prefix before the matched needle must be empty
                prefix_text = line_norm[:pos].strip()
                _valid_prefix = (
                    re.match(r'^(?:property|class|classmethod|staticmethod|method|function|attribute)\s*$', prefix_text)
                    if prefix_text else True
                )

                is_code = force_code or not _valid_prefix or _looks_like_code_example(line_stripped, pos)
                current_best = best_code_example if is_code else best_definition

                if score >= current_best[2]:
                    if is_code:
                        best_code_example = (line_idx, pos, score, "exact")
                    else:
                        best_definition = (line_idx, pos, score, "exact")
                        # Early-stop only when the match is unpenalised and near the line start (a clear primary-definition signal)
                        if early_stop and pos < 20 and context_adj >= 0:
                            return best_definition

        # ------------------------------------------------------------------
        # Priority 2: Anchor matches just the API name (score base 50)
        # ------------------------------------------------------------------
        for needle in needles.get("anchor", []):
            needle_norm = normalize_for_match(needle)
            if not needle_norm:
                continue
            
            # Skip single/double-character anchors that have no dots: they are too ambiguous to be reliable anchors.  E.g. "r" from "xgboost.query_contributors.r" would match "w.r.t." in prose.
            # FQN forms (containing ".") are retained because they are specific.
            if len(needle_norm) <= 2 and '.' not in needle_norm:
                continue
            
            pattern = r'\b' + re.escape(needle_norm) + r'\b'
            match = re.search(pattern, line_norm)
            if match:
                pos = match.start()
                start_bonus = 10 if pos < 20 else 0
                
                # Standalone-definition bonus: the normalised line is the member name (possibly followed by a type colon).  This distinguishes:
                #   "best_score"              -> bare attribute definition  (+15)
                #   "best_score: float"       -> typed attribute definition (+15)
                #   "best_score=None"         -> constructor parameter      ( 0)
                #   "…class …best_score…"     -> name buried in long sig    ( 0)
                # Without this, the class-definition line's +20 class-keyword bonus beats the bare attribute definition's score
                standalone_bonus = 0
                if line_norm.startswith(needle_norm):
                    remainder = line_norm[len(needle_norm):].strip()
                    if not remainder or remainder.startswith(':'):
                        standalone_bonus = 15
                
                score = 50 + start_bonus + context_adj + standalone_bonus

                # ── Definition-position check ────────────────────────────────
                # A valid definition line has the anchor within the first 25 normalised characters, with at most a documentation keyword (property, class, classmethod, staticmethod) before it.
                # Anchors buried deeper in the line are almost certainly cross-references or parameter mentions, not definitions: 
                prefix_text = line_norm[:pos].strip()
                _doc_kw_only = (
                    re.match(
                        r'^(?:property|class|classmethod|staticmethod|method|function|attribute)\s*$',
                        prefix_text,
                    )
                    if prefix_text else True   # empty prefix (always OK)
                )
                at_definition_pos = pos <= 60 and bool(_doc_kw_only)
                
                # ── Valid-suffix check ───────────────────────────────────────
                # After the matched name, the line must end, show a colon (typed attribute/property) or an opening paren (callable)
                if at_definition_pos:
                    suffix_after = line_norm[match.end():].strip()
                    valid_suffix = (
                        not suffix_after                # bare attribute (end of line)
                        or suffix_after.startswith(':') # typed property: "name: type"
                        or suffix_after.startswith('(') # callable: "name(params)"
                    )
                    
                    # CLASS members: a bare name (empty suffix) is not a valid class anchor unless it is preceded by the "class" keyword
                    # Valid forms: "class ClassName(...)" or "ClassName(...)".
                    if valid_suffix and member_type == "class":
                        has_paren_suffix = suffix_after.startswith('(')
                        has_class_prefix = (prefix_text == 'class')
                        if not has_paren_suffix and not has_class_prefix:
                            valid_suffix = False  # bare "ClassName" ≠ class definition
                    
                    if not valid_suffix: at_definition_pos = False
                
                is_code = (not at_definition_pos) or force_code or _looks_like_code_example(line_stripped, pos)
                current_best = best_code_example if is_code else best_definition

                if score >= current_best[2]:
                    if is_code:
                        best_code_example = (line_idx, match.start(), score, "anchor")
                    else:
                        best_definition = (line_idx, match.start(), score, "anchor")

    if best_definition[0] >= 0:
        return best_definition
    
    # No definition-position match found. Cap the code-example score at 1.0 so that callers (find_anchor_in_raw, section_match_score) can distinguish
    # "found only in prose/code-examples" from a genuine definition match.
    # With score capped at 1.0, section_match_score totals stay well below min_lexical (~30–50), suppressing false Strategy-1 hits.
    line_idx_c, off_c, score_c, mt_c = best_code_example
    return (
        (line_idx_c, off_c, min(score_c, 1.0), mt_c)
        if line_idx_c >= 0
        else best_code_example
    )


def section_match_score(
    text: str,
    needles: Dict[str, List[str]],
    section_title: str = "",
    member_type: str = ""
) -> Tuple[float, int, int, str]:
    """
    Compute a comprehensive match score for a section.
    Combines line-level matching with contextual signals:
        1. Line match quality (from find_needle_in_lines, now with context bonuses)
        2. Density bonus: +0.5 per additional anchor occurrence (capped at +10)
        3. Title bonus: +15 if section title contains the short API name
        4. Reference penalty: -20 if matches appear to be cross-references only
    Args:
        text: Section text (text_norm from Section).
        needles: Tiered needle dict from build_lexical_needles().
        section_title: Section title for title-bonus calculation.
        member_type: Passed through to find_needle_in_lines (reserved for future use).
    
    Returns:
        Tuple of (total_score, line_index, char_offset, match_type) where:
            - total_score: Combined score with all bonuses/penalties
            - line_index: Best matching line index
            - char_offset: Position within that line
            - match_type: Type of match found
    """
    # Get line-level match
    line_idx, char_offset, base_score, match_type = find_needle_in_lines(text, needles, member_type=member_type)
    
    # --- Density Bonus ---
    density_bonus = 0.0
    text_norm = normalize_for_match(text)
    
    for needle in needles.get("anchor", []):
        needle_norm = normalize_for_match(needle)
        if needle_norm:
            count = text_norm.count(needle_norm)
            if count > 1:
                density_bonus += (count - 1) * 0.5
                
    density_bonus = min(density_bonus, 10.0)
    
    # --- Title Bonus ---
    title_bonus = 0.0
    if section_title and needles.get("anchor"):
        title_lower = section_title.lower()
        for anchor in needles["anchor"]:
            if anchor.lower() in title_lower:
                title_bonus = 15.0
                break
    
    # --- Reference Penalty ---
    reference_penalty = 0.0
    if needles.get("anchor"):
        for anchor in needles["anchor"]:
            if detect_reference_pattern(text, anchor):
                reference_penalty = 20.0
                break
    
    total_score = base_score + density_bonus + title_bonus - reference_penalty
    return (total_score, line_idx, char_offset, match_type)


# =============================================================================
# Reference Detection
# =============================================================================

def detect_reference_pattern(text: str, api_name: str) -> bool:
    """
    Detect if mentions of an API name are references rather than definitions.
    
    In PDF documentation, sections may reference other APIs without defining them.
    Common patterns include:
        - "See Conv1d" or "see torch.nn.Conv1d"
        - "Conv1d(#page=189)" - hyperlink style references
        - "Conv1d (page 189)" - inline page references
    
    This function returns True only if the section appears to contain ONLY
    references and no actual definition. If both reference patterns and
    definition-like patterns exist, it returns False (benefit of the doubt).
    
    Args:
        text: The section text to analyze.
        api_name: The API name to check (can be FQN or short name).
    
    Returns:
        True if the text appears to only reference the API without defining it.
        
    Example:
        >>> text = "See Conv1d(#page=189) for details on 1D convolution."
        >>> detect_reference_pattern(text, "Conv1d")
        True
        
        >>> text = "class Conv1d(in_channels, out_channels, ...)"
        >>> detect_reference_pattern(text, "Conv1d")
        False
    """
    # Extract short name if FQN provided
    short_name = api_name.split('.')[-1]
    
    # Reference patterns that suggest this is not the primary definition
    ref_patterns = [
        rf'[Ss]ee\s+{re.escape(short_name)}\b',              # "See Conv1d"
        rf'{re.escape(short_name)}\s*\(\s*#page=\d+\)',      # "Conv1d(#page=189)"
        rf'{re.escape(short_name)}\s*\(\s*page\s+\d+\)',     # "Conv1d(page 189)"
        rf'{re.escape(short_name)}\s+on\s+page\s+\d+',       # "Conv1d on page 189"
    ]
    
    # Definition patterns that suggest this IS the primary definition
    def_patterns = [
        rf'^(?:class\s+)?{re.escape(short_name)}\s*\(',      # "Conv1d(" at line start
        rf'^\s*{re.escape(short_name)}\s*\(',                # "  Conv1d(" with indent
    ]
    
    text_lower = text.lower()
    
    # Check for definition patterns first
    for pattern in def_patterns:
        if re.search(pattern, text, re.MULTILINE | re.IGNORECASE):
            return False  # Found definition, not just references
    
    # Check for reference patterns
    for pattern in ref_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True  # Found reference pattern
    
    return False


# =============================================================================
# Fuzzy Matching (Fallback Strategy)
# =============================================================================

def fuzzy_line_score(
    text: str, 
    needles: List[str], 
    top_n: int = 5
) -> float:
    """
    Compute fuzzy match score using line-level partial matching.
    
    Unlike token_set_ratio (which compares bags of tokens), this function
    uses partial_ratio on individual lines. partial_ratio finds the best
    matching substring within each line, making it suitable for finding
    signatures as substrings within lines.
    
    Strategy:
        1. Split text into lines
        2. For each line, compute partial_ratio against each needle
        3. Collect all scores and return average of top N
    
    Using top-N averaging (instead of max) provides robustness against
    outlier matches while still rewarding sections with multiple good matches.
    
    Args:
        text: The section text to search.
        needles: List of needle strings (typically "anchor" needles for fallback).
        top_n: Number of top scores to average (default: 5).
    
    Returns:
        Average of top N fuzzy scores (0-100 scale).
        
    Note:
        This function is intended as a fallback when line-level exact
        matching fails. It's more expensive than exact matching but more
        tolerant of formatting variations.
    """
    lines = text.split('\n')
    all_scores = []
    
    for line in lines:
        stripped = line.strip()
        # Skip very short lines
        if len(stripped) < 5:
            continue
        
        for needle in needles:
            if not needle:
                continue
            # partial_ratio finds best matching substring in line
            score = fuzz.partial_ratio(stripped, needle)
            all_scores.append(score)
    
    if not all_scores:
        return 0.0
    
    # Return average of top N scores
    all_scores.sort(reverse=True)
    top_scores = all_scores[:top_n]
    return sum(top_scores) / len(top_scores)


# =============================================================================
# Legacy Functions (Kept for Backward Compatibility)
# =============================================================================

def exact_density(text: str, needles: List[str]) -> int:
    """
    Count exact substring occurrences of needles in text.
    
    DEPRECATED: Use section_match_score() for better accuracy.
    
    This function performs case-sensitive exact matching, which fails when
    signatures have formatting differences. Kept for backward compatibility.
    
    Args:
        text: Text to search.
        needles: List of needle strings.
    
    Returns:
        Total count of all needle occurrences.
    """
    cnt = 0
    for n in needles:
        cnt += text.count(n)
    return cnt

def hybrid_score(
    exact: int, 
    fuzzy: float, 
    semantic: float, 
    w_exact: float = 1.0, 
    w_fuzzy: float = 0.01, 
    w_sem: float = 1.0, 
    length_penalty: float = 0.0
) -> float:
    """
    Combine lexical and semantic scores into a hybrid score.
    
    DEPRECATED for lexical-only scoring. Use section_match_score() instead.
    
    This function is still used when semantic search is enabled to combine
    the semantic similarity with lexical signals.
    
    Args:
        exact: Exact match count (from exact_density).
        fuzzy: Fuzzy score (from fuzzy_score).
        semantic: Semantic similarity score (from embeddings).
        w_exact: Weight for exact matches.
        w_fuzzy: Weight for fuzzy score.
        w_sem: Weight for semantic score.
        length_penalty: Penalty subtracted from final score.
    
    Returns:
        Weighted combination of all scores.
    """
    return w_exact * exact + w_fuzzy * fuzzy + w_sem * semantic - length_penalty


# =============================================================================
# Semantic Search Utilities
# =============================================================================

def cosine_similarity(q: np.ndarray, X: np.ndarray) -> np.ndarray:
    """
    Compute cosine similarity between a query vector and a matrix of vectors.
    
    Assumes inputs are already L2-normalized, so cosine similarity reduces
    to a dot product.
    
    Args:
        q: Query vector of shape (D,).
        X: Matrix of vectors of shape (N, D).
    
    Returns:
        Array of shape (N,) containing similarity scores.
    """
    return X @ q  # (N, D) @ (D,) -> (N,)