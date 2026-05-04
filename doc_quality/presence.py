"""
Presence predicate for structured-doc field evaluation.

The structured documentation extractor (see
``doc_processor/structured_doc_extracter.py``) is instructed to populate every
required schema field, marking absent values as the literal string ``"N/A"``.
Empty arrays may also indicate absence depending on the field's semantics.

Almost every check in the evaluator needs to ask "does this field carry
informational content?" and a single, consistent answer is critical:
treating ``"N/A"`` as informational would mask real completeness issues, while
treating empty strings as informational would inflate completeness scores.
This module provides the canonical ``is_present`` predicate so the answer is
the same wherever it is asked.
"""

from __future__ import annotations

from typing import Any


# Sentinel strings that the upstream pipeline writes to indicate absence.
# Comparison is performed case-insensitively because LLM output occasionally
# emits ``"n/a"`` or ``"N/a"`` despite the prompt's instruction.
_ABSENCE_SENTINELS = frozenset({"N/A", "NA", "NONE", "NULL", "TBD"})


def is_present(value: Any, *, allow_zero_length: bool = False) -> bool:
    """Return True if ``value`` carries informational content.

    The function recognizes the following as *not* present:

    * ``None``
    * The empty string and any whitespace-only string
    * Any string equal (case-insensitively) to one of the sentinel
      values such as ``"N/A"``, ``"NA"``, ``"None"``
    * Empty containers (lists, tuples, sets, dicts), unless
      ``allow_zero_length`` is True

    All other values are considered present, including ``0``, ``False``,
    and other "falsy" Python values - those are legitimate informational
    contents in this domain.

    Args:
        value: The value to test.
        allow_zero_length: If True, empty containers count as present
            (useful for fields that *may* legitimately be empty arrays,
            e.g. a class without any decorators).

    Returns:
        True if the value is non-trivially populated, False otherwise.
    """
    # Explicit None check first; this avoids surprises on instances of
    # types that override __bool__.
    if value is None:
        return False

    # Strings: strip whitespace and compare against the sentinel set.
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return False
        # Sentinel comparison is case-insensitive to tolerate inconsistent
        # casing in upstream artifacts.
        if stripped.upper() in _ABSENCE_SENTINELS:
            return False
        return True

    # Containers: empty means absent unless the caller has explicitly
    # opted into "empty is allowed" semantics. We deliberately use a tuple
    # of types (rather than abc.Container) because we want to exclude
    # strings - those are handled above.
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        if not value and not allow_zero_length:
            return False
        return True

    # All other types (numbers, booleans, custom objects) are considered
    # present. We do not invoke truthiness because 0 and False are valid
    # informational contents.
    return True


def first_present(*values: Any) -> Any:
    """Return the first argument for which ``is_present`` returns True.

    Convenience helper for cascading fallbacks - e.g.
    ``first_present(doc_signature, code_signatures.get('full'))``.
    Returns ``None`` if no argument is present.
    """
    for v in values:
        if is_present(v):
            return v
    return None


def coalesce_text(value: Any) -> str:
    """Return the text content of ``value`` if present, otherwise the empty string.

    Useful when piping potentially-absent fields into NLP utilities that
    crash on ``None`` or sentinel inputs (text-stat readability, etc.).
    """
    if not is_present(value):
        return ""
    if isinstance(value, str):
        return value.strip()
    # Preserve list/tuple aggregation: join element-wise after recursive
    # coalescing. This is intentionally non-recursive past one level to
    # avoid surprising behaviour on deeply nested structures.
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if is_present(v)]
        return " ".join(parts)
    return str(value)
