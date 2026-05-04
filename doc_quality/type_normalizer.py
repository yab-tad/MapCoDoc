"""
Type-string normalization and equivalence comparison.

The MapCoDoc database stores parameter and return types as strings produced
by ``ast.unparse()`` of Python type annotations. The structured documentation
extractor stores types as strings produced by an LLM that preserves the
formatting of the upstream documentation. The two strings are rarely
character-identical even when they refer to the same type:

* code:  ``Optional[bool]``
* doc:   ``bool(https://docs.python.org/3/library/functions.html#bool), optional``

The accuracy and maintainability evaluators rely on a deterministic
normalization step that maps both forms to a common canonical
representation, then compares either by exact equality or by Jaccard
similarity over the resulting tokens.

The rule set below covers the equivalences observed across PyTorch, scikit
-learn, NumPy, SQLAlchemy, and Requests documentation. New rules can be
appended without restructuring the code.
"""

from __future__ import annotations

import re
from typing import Optional, Set


# ---------------------------------------------------------------------------
# Pattern compilation
# ---------------------------------------------------------------------------

# Hyperlinks come in three flavours in the structured docs:
#   1. ``[label](url)``        - markdown style. Processed first because the
#                                ``(url)`` portion would otherwise be eaten
#                                by the bare-paren pattern below.
#   2. ``token(https://...)``  - the dominant pattern, e.g. ``bool(https://...)``
#   3. bare URLs               - rare but present
# All three must be removed before character comparisons; the maintainability
# evaluator preserves the link information separately before normalization.
#
# The order in this list matters: markdown links must come before the
# parenthesized-URL pattern.
_MARKDOWN_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^)]+)\)')
_URL_PATTERNS = [
    # Parenthesized URL immediately following a token: drop the (...) tail
    # without consuming the leading token.
    re.compile(r'\((https?://[^)]+)\)'),
    # Stale placeholders that survive substitution.
    re.compile(r'url_placeholder_\d+'),
]

# After the link patterns we want to scrub orphan ``()`` left behind by the
# first replacement pass, plus any orphan whitespace runs.
_EMPTY_PARENS_RE = re.compile(r'\(\s*\)')

# Aliases for built-in or commonly-aliased type names. The keys are typed in
# their "doc-side" forms; values are the canonical "code-side" forms. Care
# is taken to use word boundaries during substitution so that ``"int"``
# matches ``"int"`` but not ``"point"`` or ``"integer8"``.
_BUILTIN_ALIASES = {
    "integer": "int",
    "string": "str",
    "boolean": "bool",
    "floating-point": "float",
    "floating point": "float",
    "nonetype": "none",
    "none type": "none",
    # ``array_like`` is the de-facto NumPy convention; preserve it but
    # collapse common variants.
    "array-like": "array_like",
    "arraylike": "array_like",
}

# Container-type rewrites. Each rule converts a Python ``typing`` form to a
# natural-language equivalent so that ``List[str]`` and ``list of str``
# produce the same canonical form.
#
# These run *before* alias expansion, otherwise ``str`` inside ``List[str]``
# might be touched twice.
_TYPING_REWRITES = [
    # ``Optional[X]`` => ``X or none``
    (re.compile(r'optional\[(.+?)\]', re.IGNORECASE), r'\1 or none'),
    # ``Union[X, Y, ...]`` => ``X or Y or ...``
    (re.compile(r'union\[([^\[\]]+?)\]', re.IGNORECASE),
     lambda m: ' or '.join(p.strip() for p in _split_top_level_commas(m.group(1)))),
    # Generic containers
    (re.compile(r'list\[(.+?)\]', re.IGNORECASE), r'list of \1'),
    (re.compile(r'tuple\[(.+?)\]', re.IGNORECASE), r'tuple of \1'),
    (re.compile(r'dict\[(.+?)\]', re.IGNORECASE), r'dict of \1'),
    (re.compile(r'sequence\[(.+?)\]', re.IGNORECASE), r'sequence of \1'),
    (re.compile(r'iterable\[(.+?)\]', re.IGNORECASE), r'iterable of \1'),
    (re.compile(r'set\[(.+?)\]', re.IGNORECASE), r'set of \1'),
    # PEP 604 union syntax: ``int | str`` => ``int or str``
    (re.compile(r'\s*\|\s*'), r' or '),
]

# Qualifier words sometimes attached to documented types (e.g. ``", optional"``
# or ``"required"``). The treatment is type-specific:
#
# * ``optional`` historically signals "this parameter accepts None / has a
#   default". For comparison purposes we collapse it onto the typing.Optional
#   form by rewriting ``, optional`` -> ``or none``. That way
#   ``Optional[bool]`` and ``bool, optional`` produce identical canonical
#   forms.
# * ``required`` and ``default`` carry no type information and are stripped
#   outright.
# The negative lookahead ``(?!\s*\[)`` is essential: without it the regex
# also matches the ``Optional`` typing keyword inside ``Union[X, Optional[Y]]``,
# corrupting the output. Only the trailing-qualifier form (``, optional``
# or ``optional`` adjacent to end-of-string) should be rewritten.
_OPTIONAL_QUALIFIER_RE = re.compile(
    r'\s*,\s*optional\b(?!\s*\[)|\s+optional\b(?!\s*\[)',
    re.IGNORECASE,
)
_OTHER_QUALIFIER_RE = re.compile(
    r'\s*,?\s*\b(required|default)\b',
    re.IGNORECASE,
)

# Whitespace collapse - any run of whitespace becomes a single space.
_WHITESPACE_RE = re.compile(r'\s+')

# Token splitter for Jaccard comparisons. We split on whitespace, commas, and
# the natural-language operators introduced by ``_TYPING_REWRITES``. The
# operators themselves are dropped from the token set so that
# ``Optional[int]`` and ``int or none`` produce identical tokens.
_TOKEN_SPLIT_RE = re.compile(r'\b(or|and|of)\b|[\s,;:|]+')

# The set of "operator" tokens removed from the Jaccard set above.
_TOKEN_DROPS = frozenset({"or", "and", "of", ""})


def _split_top_level_commas(s: str) -> list[str]:
    """Split a comma-separated string while ignoring commas inside brackets.

    Used to handle ``Union[Dict[str, int], float]`` correctly: a naive
    ``str.split(',')`` would split on the inner comma. This implementation
    walks the string and tracks bracket depth.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in s:
        # Track bracket depth so commas inside ``Dict[str, int]`` etc. are
        # treated as part of the inner expression, not the outer split.
        if char in "[(":
            depth += 1
        elif char in "])":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return parts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize(type_str: Optional[str]) -> str:
    """Normalize a type string to a canonical form for comparison.

    The function is deliberately lossy - it strips hyperlinks, qualifier
    words, and case information - but preserves enough structure that two
    semantically equivalent types collapse to the same string.

    ``None``, ``""``, and the absence sentinels recognized by the
    presence module all map to the empty string so callers can treat
    them uniformly.

    Args:
        type_str: The raw type string from either the code or doc side.

    Returns:
        Lower-cased canonical form, or the empty string if the input is
        absent.
    """
    if not type_str:
        return ""
    s = type_str.strip()

    # Treat the documented absence sentinel up-front to avoid running
    # rewrites against meaningless input.
    if s.upper() in {"N/A", "NA", "NONE TYPE"}:
        # ``"NONE TYPE"`` here is a literal sentinel; the alias for the
        # actual NoneType is handled below.
        return ""
    s = s.lower()

    # 1. Markdown links first (so their URL portion isn't eaten by the
    #    parenthesized-URL pattern). Keep the visible text.
    s = _MARKDOWN_LINK_RE.sub(r'\1', s)
    # 2. Other URL/placeholder patterns.
    for pattern in _URL_PATTERNS:
        s = pattern.sub('', s)
    # Clean up empty ``()`` left behind by the URL stripping pass.
    s = _EMPTY_PARENS_RE.sub('', s)

    # 3. Rewrite the ``, optional`` qualifier as ``or none`` so it
    #    collapses onto the typing.Optional form. Done before container
    #    rewrites so a string like ``List[int], optional`` becomes
    #    ``list of int or none``.
    s = _OPTIONAL_QUALIFIER_RE.sub(' or none', s)
    s = _OTHER_QUALIFIER_RE.sub('', s)

    # 4. Container/typing rewrites. The order here matters: ``Optional[X]``
    #    must be rewritten before ``Union`` so that nested forms expand
    #    cleanly.
    for pattern, replacement in _TYPING_REWRITES:
        s = pattern.sub(replacement, s)

    # 5. Built-in aliases. Word boundaries ensure ``"integer"`` becomes
    #    ``"int"`` but ``"integerlike"`` (hypothetical) is left alone.
    for src, dest in _BUILTIN_ALIASES.items():
        s = re.sub(rf'\b{re.escape(src)}\b', dest, s)

    # 6. Final whitespace collapse and trim. Strip leading/trailing
    #    punctuation that may be left after removing qualifiers.
    s = _WHITESPACE_RE.sub(' ', s).strip(' ,;:')

    return s


def tokenize(type_str: Optional[str]) -> Set[str]:
    """Tokenize a normalized type into a set of comparable tokens.

    Used by ``types_equivalent`` for fuzzy comparison. Operator tokens
    (``or``, ``and``, ``of``) are removed so that types with reordered
    union members still tokenize identically.
    """
    s = normalize(type_str)
    if not s:
        return set()
    raw = _TOKEN_SPLIT_RE.split(s)
    return {tok for tok in (r.strip() if r else r for r in raw)
            if tok and tok not in _TOKEN_DROPS}


def types_equivalent(code_type: Optional[str],
                     doc_type: Optional[str],
                     fuzzy_threshold: float = 0.8) -> bool:
    """Test whether two type strings are equivalent after normalization.

    The function tries strict equality first - the cheap and most common
    case - and falls back to Jaccard similarity over the tokenized forms.
    The threshold is conservative: the default 0.8 allows for one or two
    extra modifier tokens (e.g. ``"or none"`` present on one side and not
    the other) without permitting genuinely different types to collide.

    Empty inputs (after normalization) are treated as "unknown" and
    compared symmetrically: both empty => equivalent, exactly one empty
    => not equivalent.

    Args:
        code_type: Type string from the code side (typically
            ``ast.unparse``-produced).
        doc_type:  Type string from the doc side (LLM-extracted).
        fuzzy_threshold: Jaccard similarity at or above which the types
            are considered equivalent.

    Returns:
        True if equivalent, False otherwise.
    """
    n_code = normalize(code_type)
    n_doc = normalize(doc_type)

    # Strict path: both empty, or character-identical canonical forms.
    if n_code == n_doc:
        return True

    # Treat empty + non-empty as a mismatch - we can't say they agree
    # if one side has no information at all.
    if not n_code or not n_doc:
        return False

    # Fuzzy path. Tokenize and compute Jaccard similarity.
    a = tokenize(code_type)
    b = tokenize(doc_type)
    if not a or not b:
        return False
    intersection = len(a & b)
    union = len(a | b)
    return (intersection / union) >= fuzzy_threshold


def is_builtin_type(type_str: Optional[str]) -> bool:
    """Heuristic: does ``type_str`` refer to a Python built-in type?

    Used by the maintainability evaluator to decide whether a type
    mention should be linked to ``docs.python.org``.
    """
    if not type_str:
        return False
    tokens = tokenize(type_str)
    builtins = {
        "int", "str", "bool", "float", "list", "dict", "tuple", "set",
        "bytes", "bytearray", "frozenset", "complex", "none", "object",
        "type", "callable",
    }
    # A type is "builtin" if it consists entirely of builtin tokens (so
    # ``list of int`` qualifies, but ``Tensor`` does not).
    return bool(tokens) and tokens.issubset(builtins)
