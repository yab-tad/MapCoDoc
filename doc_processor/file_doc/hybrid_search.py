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
    4. Tiered matching: exact > prefix > anchor patterns

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
    Check if a line looks like it's from a code example rather than a definition.
    
    Code examples typically have:
        - REPL prompts: >>>, ...
        - Assignment patterns BEFORE any signature: m = Conv2d(...)
    
    Real definitions (documentation format):
        - ClassName(params) - signature at/near line start
        - class ClassName(...) - class definitions
        - torch.nn.Conv2d(in_channels, ...) - FQN signatures
    
    Args:
        line: The line to check (stripped).
        match_pos: Position where the needle match was found (-1 if unknown).
        
    Returns:
        True if this looks like a code example, False if it looks like a definition.
    """
    # REPL prompts are definitely code examples
    if line.startswith('>>>') or line.startswith('...'):
        return True
    
    # Check for assignment pattern BEFORE the signature/match
    # Key insight: default params have '=' INSIDE parens, assignments have '=' BEFORE '('
    #
    # Code example:   "m = nn.Conv2d(16, 33)"      -> '=' at pos 2, '(' at pos 14 -> assignment
    # Real signature: "Conv2d(in_channels, stride=1)" -> '=' at pos 27, '(' at pos 6 -> NOT assignment
    
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

def find_needle_in_lines(
    text: str, 
    needles: Dict[str, List[str]],
    early_stop: bool = True,
    prioritize_outside_code_blocks: bool = True
) -> Tuple[int, int, float, str]:
    """
    Find the best needle match by scanning lines, prioritizing definition-like matches.
    
    Uses content-based detection to distinguish code examples from definitions:
        - Matches in definition-like lines (no REPL prompt, no assignment) = high priority
        - Matches in code example lines (>>>, m = ...) = low priority fallback
    
    Location Priority:
        1. Matches OUTSIDE code blocks (primary - these are definitions)
        2. Matches INSIDE code blocks (fallback - for edge cases where
           signature is rendered as code)
    
    Within each location category, match types are prioritized:
        1. "exact" needles: Full signature matches (score: 100)
        2. "prefix" needles: Name + opening paren (score: 80)
        3. "anchor" needles: Just the API name (score: 50)
    
    Each match receives a position bonus (+10) if it occurs near the start of 
    the line (position < 20 chars), indicating a primary definition.
    
    Args:
        text: The section text to search (typically text_norm from Section).
        needles: Dictionary with keys "exact", "prefix", "anchor", each mapping to a list of needle strings to search for.
        early_stop: If True, return immediately upon finding an "exact" match at line start OUTSIDE a code block (optimization).
        prioritize_outside_code_blocks: If True (default), prefer matches outside code blocks. If False, treat all matches equally.
    
    Returns:
        Tuple of (line_index, char_offset, score, match_type) where:
            - line_index: 0-based index of the matching line (-1 if no match)
            - char_offset: Character offset within the line where match starts
            - score: Numeric score (0-110) indicating match quality
            - match_type: One of "exact", "prefix", "anchor", or "none"
    
    Example:
        >>> text = "class torch.nn.Conv1d(in_channels, ...) -> Tensor"
        >>> needles = {
        ...     "exact": ["Conv1d(in_channels, out_channels, kernel_size"],
        ...     "prefix": ["Conv1d(", "torch.nn.Conv1d("],
        ...     "anchor": ["Conv1d", "torch.nn.Conv1d"]
        ... }
        >>> line_idx, pos, score, match_type = find_needle_in_lines(text, needles)
        >>> print(f"Found {match_type} at line {line_idx}, pos {pos}, score {score}")
    """
    lines = text.split('\n')
    
    # Track best matches: definitions vs code examples
    best_definition = (-1, -1, 0.0, "none")
    best_code_example = (-1, -1, 0.0, "none")
    
    for line_idx, line in enumerate(lines):
        line_stripped = line.strip()
        line_norm = normalize_for_match(line)
        
        # Skip very short lines (unlikely to contain signatures)
        if len(line_norm) < 3:
            continue
        
        # Skip obvious code fence markers
        if line_stripped.startswith('```'):
            continue
        
        # Determine which "best" tracker to compare against based on content
        # (determined per-match below since position affects classification)
        
        # --- Priority 1: Exact signature matches ---
        for needle in needles.get("exact", []):
            needle_norm = normalize_for_match(needle)
            if not needle_norm:
                continue
            pos = line_norm.find(needle_norm)
            if pos >= 0:
                start_bonus = 10 if pos < 20 else 0
                score = 100 + start_bonus
                
                # Content-based: is this line a code example?
                is_code = _looks_like_code_example(line_stripped, pos) if prioritize_outside_code_blocks else False
                current_best = best_code_example if is_code else best_definition
                
                if score > current_best[2]:
                    if is_code:
                        best_code_example = (line_idx, pos, score, "exact")
                    else:
                        best_definition = (line_idx, pos, score, "exact")
                        # Early stop: exact match at start in a definition line
                        if early_stop and pos < 20:
                            return best_definition
        
        # --- Priority 2: Prefix matches (name + opening paren) ---
        for needle in needles.get("prefix", []):
            needle_norm = normalize_for_match(needle)
            if not needle_norm:
                continue
            pos = line_norm.find(needle_norm)
            if pos >= 0:
                start_bonus = 10 if pos < 20 else 0
                score = 80 + start_bonus
                
                is_code = _looks_like_code_example(line_stripped, pos) if prioritize_outside_code_blocks else False
                current_best = best_code_example if is_code else best_definition
                
                if score > current_best[2]:
                    if is_code:
                        best_code_example = (line_idx, pos, score, "prefix")
                    else:
                        best_definition = (line_idx, pos, score, "prefix")
        
        # --- Priority 3: Anchor matches (just the name with word boundaries) ---
        for needle in needles.get("anchor", []):
            needle_norm = normalize_for_match(needle)
            if not needle_norm:
                continue
            pattern = r'\b' + re.escape(needle_norm) + r'\b'
            match = re.search(pattern, line_norm)
            if match:
                start_bonus = 10 if match.start() < 20 else 0
                score = 50 + start_bonus
                
                is_code = _looks_like_code_example(line_stripped, match.start()) if prioritize_outside_code_blocks else False
                current_best = best_code_example if is_code else best_definition
                
                if score > current_best[2]:
                    if is_code:
                        best_code_example = (line_idx, match.start(), score, "anchor")
                    else:
                        best_definition = (line_idx, match.start(), score, "anchor")
    
    # Return definition match if found, otherwise fallback to code example match
    if best_definition[0] >= 0:
        return best_definition
    else:
        return best_code_example


def section_match_score(
    text: str, 
    needles: Dict[str, List[str]],
    section_title: str = ""
) -> Tuple[float, int, int, str]:
    """
    Compute a comprehensive match score for a section.
    
    This function combines line-level matching with contextual signals:
        1. Line match quality (from find_needle_in_lines)
        2. Density bonus: Extra points for multiple occurrences
        3. Title bonus: Extra points if section title contains API name
        4. Reference penalty: Deduction if matches look like references
    
    Args:
        text: The section text (typically text_norm from Section).
        needles: Tiered needle dictionary from build_lexical_needles().
        section_title: The section's title for context matching.
    
    Returns:
        Tuple of (total_score, line_index, char_offset, match_type) where:
            - total_score: Combined score with all bonuses/penalties
            - line_index: Best matching line index
            - char_offset: Position within that line
            - match_type: Type of match found
    
    Score Components:
        - Base match: 0-110 (from find_needle_in_lines)
        - Density bonus: +0.5 per additional occurrence (capped at +10)
        - Title bonus: +15 if section title contains the short API name
        - Reference penalty: -20 if matches appear to be references only
    """
    # Get line-level match
    line_idx, char_offset, base_score, match_type = find_needle_in_lines(text, needles)
    
    # --- Density Bonus ---
    # Count occurrences of prefix/anchor patterns for additional signal
    density_bonus = 0.0
    text_norm = normalize_for_match(text)
    
    for needle in needles.get("prefix", []) + needles.get("anchor", []):
        needle_norm = normalize_for_match(needle)
        if needle_norm:
            count = text_norm.count(needle_norm)
            # Diminishing returns: first occurrence already counted in base_score
            if count > 1:
                density_bonus += (count - 1) * 0.5
    
    # Cap density bonus to avoid over-weighting repetitive sections
    density_bonus = min(density_bonus, 10.0)
    
    # --- Title Bonus ---
    # If the section title contains the API name, it's likely the correct section
    title_bonus = 0.0
    if section_title and needles.get("anchor"):
        title_lower = section_title.lower()
        for anchor in needles["anchor"]:
            if anchor.lower() in title_lower:
                title_bonus = 15.0
                break
    
    # --- Reference Penalty ---
    # Detect if this section only references the API (e.g., "See Conv1d(#page=189)")
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
        This function is intended as a fallback when line-level exact/prefix
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