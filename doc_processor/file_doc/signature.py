"""
Signature Pattern Building for PDF Documentation Extraction

This module provides utilities for building search patterns and queries for API members.
It generates tiered lexical needles for line-level matching and regex patterns for
anchor finding.

Key Design Principles:
    1. Generate multiple specificity levels (exact -> anchor)
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

    Attributes:
        api_name: Fully qualified name, e.g., "torch.nn.Conv1d" or "pandas.DataFrame.to_csv".
        signature_variants: Named signature renderings keyed by variant name.
                           Keys are from parameter_analysis.py variations:
                           'full', 'no_types', 'defaults_only', 'no_special',
                           'no_slash', 'no_asterisk', 'no_types_no_slash', 'no_types_no_asterisk'.
                           Values are complete signature strings, e.g. 'Conv1d(in_channels, ...)'.
                           An empty dict means no known signature (variable or no-arg property).
        docstring: Optional docstring from source code, used for semantic hints.
        member_type: One of 'class', 'function', 'method', 'variable'.
                    Affects how needles and queries are constructed.

    Example:
        >>> member = MemberInput(
        ...     api_name="torch.nn.Conv1d",
        ...     signature_variants={"full": "Conv1d(in_channels, out_channels, ...)"},
        ...     member_type="class"
        ... )
    """
    api_name: str
    signature_variants: Dict[str, str] = field(default_factory=dict)
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
    for v in member.signature_variants.values():
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

# Priority order for signature variants when building needles.
# Earlier entries are tried first in find_needle_in_lines.
_VARIANT_PRIORITY: List[str] = [
    'full', 'no_types', 'defaults_only', 'no_special',
    'no_slash', 'no_asterisk', 'no_types_no_slash', 'no_types_no_asterisk'
]


def _first_variant(variants: Dict[str, str]) -> str:
    """Return the most informative signature string from a named-variant dict."""
    for key in _VARIANT_PRIORITY:
        if key in variants:
            return variants[key]
    if variants:
        return next(iter(variants.values()))
    return ""


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


def _has_no_user_params(member: MemberInput) -> bool:
    """
    Return True if the member has no user-facing parameters.

    This covers:
      - Variables (member_type == "variable")
      - Properties and no-arg methods: all stored signature variants have an
        empty parameter list after ``(``, ignoring the implicit ``self``

    For these members the anchor tier short-name fallback is safe: the name
    appears in very few lines and the ``property``/URL bonuses in
    _line_context_score reliably identify the definition line.

    For callables with real parameters the short name is too promiscuous
    (it matches parameter-type annotations, prose, URL fragments, etc.), so
    anchor-eligible returns False and the short name is excluded from the
    anchor tier.
    """
    if member.member_type == "variable":
        return True

    if not member.signature_variants:
        # No stored signature at all — treat conservatively as anchor-eligible
        # so we have at least some fallback signal.
        return True

    for sig in member.signature_variants.values():
        paren_idx = sig.find('(')
        if paren_idx < 0:
            # Bare name stored without parens (e.g. a property stored as "feature_names")
            return True
        close_idx = sig.rfind(')')
        params_raw = sig[paren_idx + 1 : close_idx].strip() if close_idx > paren_idx else ""
        # Remove 'self' — it is not a user-facing parameter
        params_no_self = re.sub(r'\bself\b,?\s*', '', params_raw).strip().strip(',').strip()
        if not params_no_self:
            return True   # Empty or self-only → no-arg callable

    return False  # At least one variant has real user-facing params


def build_lexical_needles(member: MemberInput) -> Dict[str, List[str]]:
    """
    Build tiered lexical needles for line-level substring matching.

    Two tiers:
        1. "exact": Full signatures in multiple qualified forms, ordered by specificity.
                    Variants are processed in _VARIANT_PRIORITY order.
                    For each variant the following qualified forms are generated:

                    Classes  ->  "class {api_name}(params)"   (Sphinx web, most specific)
                                 "{api_name}(params)"
                                 "class {short_name}(params)"
                                 "{short_name}(params)"

                    Methods  ->  "{short_name}(params)"        (web docs – sig uses short name)
                                 "{ClassName}.{short_name}(params)"  (PDF docs)
                                 "{api_name}(params)"          (FQN, present via URL in web)

                    Functions -> "{short_name}(params)"
                                 "{api_name}(params)"

        2. "anchor": Plain qualified names used as fallback.
                     Primary signal for no-arg properties and variables.

    Args:
        member: MemberInput with api_name, signature_variants, and member_type.

    Returns:
        Dictionary with keys "exact" and "anchor", each a de-duplicated list
        ordered from most to least specific.
    """
    
    api_name = member.api_name
    short_name = api_name.split('.')[-1]
    parts = api_name.split('.')

    needles: Dict[str, List[str]] = {"exact": [], "anchor": []}

    # -------------------------------------------------------------------------
    # Variables: anchor only (no callable signature)
    # -------------------------------------------------------------------------
    if member.member_type == "variable":
        needles["anchor"] = [api_name, short_name]
        return needles

    seen_exact: set = set()

    def _add_exact(s: str) -> None:
        s = _normalize_whitespace(s)
        if s and s not in seen_exact:
            seen_exact.add(s)
            needles["exact"].append(s)
            
    # -------------------------------------------------------------------------
    # Class heading without constructor on the same line (common in PDF/Sphinx)
    # -------------------------------------------------------------------------
    if member.member_type == "class":
        _add_exact(f"class {api_name}")
        _add_exact(f"class {short_name}")

    # -------------------------------------------------------------------------
    # Exact tier: iterate variants in priority order
    # -------------------------------------------------------------------------
    for variant_key in _VARIANT_PRIORITY:
        sig_text = member.signature_variants.get(variant_key)
        if not sig_text:
            continue

        sig_text = sig_text.strip()
        paren_idx = sig_text.find('(')

        if paren_idx < 0:
            # No parentheses → bare name, belongs in anchor tier only
            continue

        params_part = sig_text[paren_idx:]   # "(param1, param2, ...)" or "()"

        if member.member_type == "class":
            # Most specific first: FQN with "class" keyword (Sphinx HTML form)
            _add_exact(f"class {api_name}{params_part}")
            _add_exact(f"{api_name}{params_part}")
            _add_exact(f"class {short_name}{params_part}")
            _add_exact(f"{short_name}{params_part}")

        elif member.member_type == "method":
            # Web docs use the short name; PDFs use ClassName.method_name
            _add_exact(sig_text)                                          # as stored (short name)
            if len(parts) >= 2:
                class_qualified = f"{parts[-2]}.{short_name}{params_part}"
                _add_exact(class_qualified)
            _add_exact(f"{api_name}{params_part}")                        # FQN (present in web URL)

        else:  # function
            _add_exact(sig_text)                                          # as stored
            _add_exact(f"{api_name}{params_part}")

    # Also iterate any variants present that are NOT in the priority list
    # (future-proofing for additional variant names)
    for variant_key, sig_text in member.signature_variants.items():
        if variant_key in _VARIANT_PRIORITY:
            continue
        sig_text = sig_text.strip()
        paren_idx = sig_text.find('(')
        if paren_idx < 0:
            continue
        params_part = sig_text[paren_idx:]
        if member.member_type == "class":
            _add_exact(f"class {api_name}{params_part}")
            _add_exact(f"{api_name}{params_part}")
            _add_exact(f"class {short_name}{params_part}")
            _add_exact(f"{short_name}{params_part}")
        elif member.member_type == "method":
            _add_exact(sig_text)
            if len(parts) >= 2:
                _add_exact(f"{parts[-2]}.{short_name}{params_part}")
            _add_exact(f"{api_name}{params_part}")
        else:
            _add_exact(sig_text)
            _add_exact(f"{api_name}{params_part}")

    # -------------------------------------------------------------------------
    # Anchor tier: plain qualified names (fallback and no-arg property support)
    # -------------------------------------------------------------------------
    # SHORT NAME is only included for members that have no user-facing parameters (no-arg properties, no-arg methods, variables).
    # For callables with params (classes, functions, methods), the short name matches too many lines on the
    # page (parameter-type annotations, prose references, URL fragments) to be a reliable anchor.
    # In those cases only the FQN (and ClassName.method) are included; they appear verbatim in the member's
    # canonical URL and provide a useful, low-noise discriminating signal even when exact matching fails.
    
    seen_anchor = set()

    def _add_anchor(s: str) -> None:
        s = s.strip()
        if s and s not in seen_anchor:
            seen_anchor.add(s)
            needles["anchor"].append(s)

    if _has_no_user_params(member):
        # Anchor-eligible: include short name for primary anchor matching.
        _add_anchor(short_name)
    # FQN always included (matches canonical URL fragment in web docs).
    _add_anchor(api_name)
    # ClassName.method form always included (PDF docs; also present in web URLs).
    if member.member_type == "method" and len(parts) >= 2:
        _add_anchor(f"{parts[-2]}.{short_name}")

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
        Flat list of all needles (exact and anchor).
    """
    tiered = build_lexical_needles(member)
    flat = []
    for key in ["exact", "anchor"]:
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
        sig =  _first_variant(member.signature_variants)
        if sig:
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
    # Use the most informative signature variant if available
    sig = _first_variant(member.signature_variants) if member.signature_variants else ""
    if sig:
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