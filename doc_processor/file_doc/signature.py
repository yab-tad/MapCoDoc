"""
Signature Pattern Building for PDF Documentation Extraction

This module provides utilities for building search patterns and queries for API members.
It generates tiered lexical needles for line-level matching and regex patterns for
anchor finding.

Key Design Principles:
    1. Generate multiple specificity levels (exact -> prefix -> anchor)
    2. Account for common PDF formatting variations
    3. Handle different member types (class, function, method, variable)

Classes:
    - MemberInput: Metadata for an API member to extract documentation for.

Functions:
    - build_signature_patterns: Build regex patterns for anchor finding
    - build_lexical_needles: Build tiered needles for line-level matching
    - build_semantic_query: Build natural-language query for embeddings
    - build_passage_text: Format passage for embedding models
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import List, Optional, Pattern, Dict


@dataclass
class MemberInput:
    """
    Metadata for a single API member to extract documentation for.
    
    This dataclass holds all information needed to search for and extract
    documentation for an API member from a PDF.

    Attributes:
        api_name: Fully qualified name, e.g., "torch.nn.Conv1d" or "pandas.DataFrame.to_csv".
        signature_variants: Different renderings of the full signature (FQN + params).
                           These may come from source code introspection and may differ
                           from PDF formatting.
        docstring: Optional docstring from source code, used for semantic hints.
        member_type: One of 'class', 'function', 'method', 'variable'.
                    Affects how needles and queries are constructed.
    
    Example:
        >>> member = MemberInput(
        ...     api_name="torch.nn.Conv1d",
        ...     signature_variants=["torch.nn.Conv1d(in_channels, out_channels, ...)"],
        ...     member_type="class"
        ... )
    """
    api_name: str
    signature_variants: List[str] = field(default_factory=list)
    docstring: Optional[str] = None
    member_type: str = "function"


# =============================================================================
# Regex Pattern Building (for Anchor Finding)
# =============================================================================

def _escape_dot_whitespace(name: str) -> str:
    """
    Escape dots and allow flexible whitespace around them.
    
    PDF extraction may introduce whitespace around dots in qualified names
    (e.g., "torch . nn . Conv1d"). This function creates a regex pattern
    that matches with optional whitespace.
    
    Args:
        name: A qualified name like "torch.nn.Conv1d".
        
    Returns:
        Regex pattern string with escaped dots and optional whitespace.
        
    Example:
        >>> _escape_dot_whitespace("torch.nn.Conv1d")
        'torch\\s*\\.\\s*nn\\s*\\.\\s*Conv1d'
    """
    parts = [re.escape(p) for p in name.split('.')]
    return r'\s*\.\s*'.join(parts)


def _loosen_token(name: str) -> str:
    """
    Allow underscores to flex into underscores or whitespace.
    
    PDF extraction occasionally splits underscored names. This creates
    a pattern that matches both "my_func" and "my func".
    
    Args:
        name: A name potentially containing underscores.
        
    Returns:
        Regex pattern with flexible underscore matching.
    """
    return name.replace('_', r'[_\s]*')


def build_signature_patterns(member: MemberInput) -> List[Pattern]:
    """
    Build tolerant regex patterns for anchor finding within sections.
    
    These patterns are used to locate the exact position of an API signature
    within a section's text. They are designed to be tolerant of:
        - Whitespace variations around dots
        - Flexible underscore handling
        - Optional 'class' prefix
        - Case variations
    
    Pattern Types Generated:
        1. FQN pattern: "torch.nn.Conv1d(" with flexible whitespace
        2. Short name pattern: "Conv1d(" with word boundary
        3. Signature variant patterns: From provided signature_variants
    
    Args:
        member: MemberInput with api_name and signature_variants.
    
    Returns:
        List of compiled regex patterns (case-insensitive, DOTALL mode).
        
    Example:
        >>> member = MemberInput(api_name="torch.nn.Conv1d", member_type="class")
        >>> patterns = build_signature_patterns(member)
        >>> any(p.search("class torch.nn.Conv1d(in_channels, ...)") for p in patterns)
        True
    """
    pats: List[str] = []

    # FQN pattern: "torch.nn.Conv1d(" with flexible dots/underscores
    fqn = _loosen_token(_escape_dot_whitespace(member.api_name))
    pats.append(rf'\b{fqn}\s*\(')

    # Short name pattern: "Conv1d(" with word boundary
    short = member.api_name.split('.')[-1]
    short = _loosen_token(re.escape(short))
    pats.append(rf'\b{short}\s*\(')
    
    # For classes, also match "class Conv1d(" pattern
    if member.member_type == "class":
        pats.append(rf'\bclass\s+{short}\s*\(')
        pats.append(rf'\bclass\s+{fqn}\s*\(')

    # Signature variant name patterns
    for v in member.signature_variants:
        nm = v.split('(')[0].strip()
        if not nm:
            continue
        nm = _loosen_token(_escape_dot_whitespace(nm))
        pats.append(rf'\b{nm}\s*\(')

    # Compile with case-insensitive and DOTALL flags
    compiled = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in pats]
    return compiled


# =============================================================================
# Lexical Needle Building (for Line-Level Matching)
# =============================================================================

def _normalize_whitespace(s: str) -> str:
    """Collapse multiple whitespace to single space and strip."""
    return ' '.join(s.split())


def _extract_signature_prefix(sig: str, max_params: int = 3) -> str:
    """
    Extract a truncated signature prefix for matching.
    
    Full signatures may be very long and have formatting variations.
    This extracts just the name and first few parameters for more
    reliable matching.
    
    Args:
        sig: Full signature string.
        max_params: Maximum number of parameters to include.
        
    Returns:
        Truncated signature like "Conv1d(in_channels, out_channels, kernel_size".
    """
    if '(' not in sig:
        return sig
    
    name_part = sig.split('(')[0]
    params_part = sig.split('(', 1)[1] if '(' in sig else ""
    
    # Extract first few parameters
    if params_part:
        # Split by comma, take first N, rejoin
        params = []
        depth = 0
        current = []
        for char in params_part:
            if char in '([{':
                depth += 1
                current.append(char)
            elif char in ')]}':
                depth -= 1
                if depth < 0:
                    break
                current.append(char)
            elif char == ',' and depth == 0:
                params.append(''.join(current).strip())
                current = []
                if len(params) >= max_params:
                    break
            else:
                current.append(char)
        
        if current and len(params) < max_params:
            params.append(''.join(current).strip())
        
        if params:
            return f"{name_part}({', '.join(params[:max_params])}"
    
    return f"{name_part}("


def build_lexical_needles(member: MemberInput) -> Dict[str, List[str]]:
    """
    Build tiered lexical needles for line-level substring matching.
    
    This function generates needles at multiple specificity levels:
    
    Tiers (in priority order):
        1. "exact": Full or truncated signature strings
        2. "prefix": Name + opening parenthesis (most reliable for locating)
        3. "anchor": Just the API name (fallback for fuzzy matching)
    
    Design Rationale:
        - Signatures in PDFs are joined into single lines by SignatureJoiner
        - Lines may have leading content (class prefix, indent) and trailing
          content (return type, description)
        - We search for signatures as substrings within lines
        - Multiple specificity levels handle varying PDF formatting quality
    
    Args:
        member: MemberInput with api_name, signature_variants, and member_type.
    
    Returns:
        Dictionary with keys "exact", "prefix", "anchor", each mapping to
        a list of needle strings (most specific to least specific).
        
    Example:
        >>> member = MemberInput(
        ...     api_name="torch.nn.Conv1d",
        ...     signature_variants=["Conv1d(in_channels, out_channels, ...)"],
        ...     member_type="class"
        ... )
        >>> needles = build_lexical_needles(member)
        >>> print(needles["prefix"])
        ['Conv1d(', 'torch.nn.Conv1d(', 'class Conv1d(', 'class torch.nn.Conv1d(']
    """
    api_name = member.api_name
    short_name = api_name.split('.')[-1]
    parts = api_name.split('.')
    
    needles: Dict[str, List[str]] = {
        "exact": [],
        "prefix": [],
        "anchor": []
    }
    
    # Handle variables specially (no signatures)
    if member.member_type == "variable":
        needles["anchor"] = [api_name, short_name]
        needles["prefix"] = [f"{api_name} =", f"{short_name} ="]
        return needles
    
    # --- Build "exact" needles from signature variants ---
    for sig in member.signature_variants:
        sig_clean = sig.strip()
        if not sig_clean:
            continue
        
        # Normalize whitespace for matching
        sig_norm = _normalize_whitespace(sig_clean)
        if sig_norm:
            needles["exact"].append(sig_norm)
        
        # Also add truncated version (first 3 params) for robustness
        sig_prefix = _extract_signature_prefix(sig_clean, max_params=3)
        if sig_prefix and sig_prefix != sig_norm:
            needles["exact"].append(_normalize_whitespace(sig_prefix))
            
        # Also generate full signatures with api_name
        paren_idx = sig_clean.find('(')
        if paren_idx != -1:
            params_part = sig_clean[paren_idx:]  # "(in_channels, ...)"
            
            # Full signature with FQN: "torch.nn.Conv1d(in_channels, ...)"
            full_sig = f"{api_name}{params_part}"
            needles["exact"].append(_normalize_whitespace(full_sig))
            
            # Truncated full signature
            full_prefix = _extract_signature_prefix(full_sig, max_params=3)
            if full_prefix:
                needles["exact"].append(_normalize_whitespace(full_prefix))
            
            # For classes, also add "class FQN(...)" variant
            if member.member_type == "class":
                class_full = f"class {api_name}{params_part}"
                needles["exact"].append(_normalize_whitespace(class_full))
                
                class_prefix = _extract_signature_prefix(class_full, max_params=3)
                if class_prefix:
                    needles["exact"].append(_normalize_whitespace(class_prefix))
    
    # --- Build "prefix" needles (name + opening paren) ---
    # These are the most reliable for locating signatures
    
    # Short name with paren: "Conv1d("
    needles["prefix"].append(f"{short_name}(")
    
    # FQN with paren: "torch.nn.Conv1d("
    needles["prefix"].append(f"{api_name}(")
    
    # For methods, also try "ClassName.method_name("
    if member.member_type == "method" and len(parts) >= 2:
        class_method = f"{parts[-2]}.{parts[-1]}("
        needles["prefix"].append(class_method)
    
    # For classes, try with 'class' prefix
    if member.member_type == "class":
        needles["prefix"].append(f"class {short_name}(")
        needles["prefix"].append(f"class {api_name}(")
    
    # --- Build "anchor" needles (just the name) ---
    # Used for fallback fuzzy matching
    needles["anchor"].append(short_name)
    needles["anchor"].append(api_name)
    
    # For methods, also include "ClassName.method_name"
    if member.member_type == "method" and len(parts) >= 2:
        needles["anchor"].append(f"{parts[-2]}.{parts[-1]}")
    
    # --- Deduplicate while preserving order ---
    for key in needles:
        seen = set()
        unique = []
        for item in needles[key]:
            if item and item not in seen:
                seen.add(item)
                unique.append(item)
        needles[key] = unique
    
    return needles


# =============================================================================
# Legacy Function (Kept for Backward Compatibility)
# =============================================================================

def build_lexical_needles_flat(member: MemberInput) -> List[str]:
    """
    Build a flat list of lexical needles (legacy format).
    
    DEPRECATED: Use build_lexical_needles() which returns tiered needles.
    
    This function is kept for backward compatibility with code that expects
    a flat list. It concatenates all needle tiers into a single list.
    
    Args:
        member: MemberInput with api_name, signature_variants, and member_type.
    
    Returns:
        Flat list of all needles (exact + prefix + anchor).
    """
    tiered = build_lexical_needles(member)
    flat = []
    for key in ["exact", "prefix", "anchor"]:
        flat.extend(tiered.get(key, []))
    return flat


# =============================================================================
# Semantic Query Construction
# =============================================================================

def build_semantic_query(member: MemberInput, model_name: str = "") -> str:
    """
    Build a natural-language query optimized for semantic retrieval.
    
    This constructs a query that embedding models can use to find relevant
    documentation sections. The query includes:
        - API type and name in natural language
        - Module/class context
        - First sentence of docstring (if available)
        - Signature hint
    
    For e5-family models, the query is prefixed with "query: " as required
    by their training format.
    
    Args:
        member: MemberInput with api_name, docstring, signature_variants.
        model_name: Embedding model name (used to detect e5 prefix requirement).
    
    Returns:
        Natural-language query string suitable for embedding.
        
    Example:
        >>> member = MemberInput(
        ...     api_name="torch.nn.Conv1d",
        ...     member_type="class",
        ...     docstring="Applies a 1D convolution over an input signal."
        ... )
        >>> query = build_semantic_query(member, "intfloat/e5-base-v2")
        >>> print(query)
        'query: API reference documentation for Conv1d class in torch.nn: Applies a 1D convolution...'
    """
    short_name = member.api_name.split('.')[-1]
    parts = member.api_name.split('.')

    # Describe the member type naturally
    type_desc = {
        "class": "class",
        "function": "function",
        "method": "method",
        "variable": "variable"
    }.get(member.member_type, "API")

    # Build query parts
    if member.member_type == "method" and len(parts) >= 2:
        class_name = parts[-2]
        query_parts = [f"API reference documentation for the {short_name} {type_desc} of {class_name} with fully qualified name {member.api_name}"]
    else:
        module_path = member.api_name.rsplit('.', 1)[0] if '.' in member.api_name else ""
        query_parts = [f"API reference documentation for {short_name} {type_desc}"]
        if module_path:
            query_parts.append(f"in {module_path} with fully qualified name {member.api_name}")

    # Include first sentence of docstring
    if member.docstring:
        sentences = re.split(r'\.(?:\s|$)', member.docstring.strip())
        first_sentence = sentences[0].strip() if sentences else ""
        if first_sentence and len(first_sentence) > 10:
            query_parts.append(f": {first_sentence}")

    # Add signature hint (not for variables)
    if member.signature_variants and member.member_type != "variable":
        sig = member.signature_variants[0]
        query_parts.append(f"(signature: {sig})")

    query_text = " ".join(query_parts)

    # Add e5 model prefix if needed
    if "e5" in model_name.lower():
        return f"query: {query_text}"

    return query_text


def build_passage_text(text: str, model_name: str = "") -> str:
    """
    Format passage text for embedding models.
    
    For e5-family models, passages should be prefixed with "passage: ".
    
    Args:
        text: Raw passage text (e.g., a section window).
        model_name: Embedding model name.
    
    Returns:
        Passage text, possibly prefixed for the model.
    """
    if "e5" in model_name.lower():
        return f"passage: {text}"
    return text

def build_signature_query(member: MemberInput, model_name: str = "") -> str:
    """
    Build a query specifically for finding the signature line.
    
    Unlike build_semantic_query which optionally includes docstring context for section-level
    matching, this focuses purely on the signature for fine-grained line-level anchoring within a section.
    
    The query emphasizes the signature structure to help semantic search
    identify the exact line where the API definition appears.
    
    Args:
        member: MemberInput with api_name, signature_variants, member_type.
        model_name: Embedding model name (for e5 prefix detection).
    
    Returns:
        Query string optimized for matching signature lines.
        
    Example:
        >>> member = MemberInput(
        ...     api_name="torch.nn.Conv1d",
        ...     signature_variants=["Conv1d(in_channels, out_channels, kernel_size)"],
        ...     member_type="class"
        ... )
        >>> query = build_signature_query(member, "intfloat/e5-base-v2")
        >>> print(query)
        'query: class definition line Conv1d(in_channels, out_channels, kernel_size) ...'
    """
    # Use the first signature variant if available, otherwise just the API name
    if member.signature_variants:
        sig = member.signature_variants[0]
        # Clean up: normalize whitespace, remove newlines
        sig_clean = ' '.join(sig.split())
    else:
        sig_clean = member.api_name + "()"
    
    # Prefix with member type for better semantic matching
    type_word = member.member_type or "function"
    
    # For classes, the signature line often starts with "class"
    if type_word == "class":
        query = f"class with API name {member.api_name} starting definition line: {sig_clean}"
    elif type_word == "method":
        query = f"method with API name {member.api_name} starting signature line: {sig_clean}"
    else:
        query = f"{type_word} with API name {member.api_name} starting signature line: {sig_clean}"
    
    # Add e5 prefix if needed
    if "e5" in model_name.lower():
        return f"query: {query}"
    return query