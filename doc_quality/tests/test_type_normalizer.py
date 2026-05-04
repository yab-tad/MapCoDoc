"""Tests for the type normalization layer.

The test cases cover the rule families documented in
``type_normalizer._TYPING_REWRITES`` and the alias table.
"""

import pytest

from doc_quality.type_normalizer import (
    is_builtin_type,
    normalize,
    tokenize,
    types_equivalent,
)


# ---------------------------------------------------------------------------
# normalize() golden-table tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("inp,expected", [
    # URL stripping
    ("bool(https://docs.python.org/foo)", "bool"),
    # ``, optional`` is rewritten to ``or none`` so it canonicalizes to the
    # same form as ``Optional[bool]``.
    ("bool(https://docs.python.org/foo), optional", "bool or none"),
    ("bool, optional", "bool or none"),
    # Markdown link
    ("[bool](https://docs.python.org/foo)", "bool"),
    # Optional / Union rewrites
    ("Optional[int]", "int or none"),
    ("Union[int, str]", "int or str"),
    ("Union[int, Optional[str]]", "int or str or none"),
    # Container rewrites
    ("List[str]", "list of str"),
    ("Tuple[int, int]", "tuple of int, int"),
    ("Dict[str, int]", "dict of str, int"),
    # Aliases
    ("integer", "int"),
    ("string", "str"),
    ("Boolean", "bool"),
    # PEP 604 union
    ("int | str", "int or str"),
    # N/A and absence sentinels
    (None, ""),
    ("", ""),
    ("N/A", ""),
])
def test_normalize_table(inp, expected):
    assert normalize(inp) == expected


# ---------------------------------------------------------------------------
# types_equivalent() tests - the most important consumer of normalize
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("Optional[bool]", "bool, optional"),
    ("bool, optional", "Optional[bool]"),
    # The L1Loss case
    ("Optional[bool]", "bool(https://docs.python.org/foo), optional"),
    # Union order should not matter
    ("Union[int, str]", "Union[str, int]"),
    # Aliases
    ("integer", "int"),
    ("Boolean", "bool"),
    # PEP 604
    ("int | None", "Optional[int]"),
])
def test_equivalent_pairs(a, b):
    assert types_equivalent(a, b)


@pytest.mark.parametrize("a,b", [
    # Different concrete types should not collapse.
    ("int", "str"),
    ("List[int]", "List[str]"),
    ("Optional[int]", "Optional[bool]"),
])
def test_inequivalent_pairs(a, b):
    assert not types_equivalent(a, b)


def test_both_empty_is_equivalent():
    assert types_equivalent("", None)
    assert types_equivalent(None, "")


def test_one_empty_is_inequivalent():
    assert not types_equivalent("int", "")
    assert not types_equivalent(None, "int")


# ---------------------------------------------------------------------------
# tokenize() / is_builtin_type()
# ---------------------------------------------------------------------------

def test_tokenize_strips_operators():
    # 'or' is an operator and must not appear in the resulting set.
    assert tokenize("Union[int, str]") == {"int", "str"}


def test_is_builtin_simple():
    assert is_builtin_type("int")
    assert is_builtin_type("Boolean")  # alias-folded


def test_is_builtin_compound():
    # 'list of int' is "all builtin" tokens.
    assert is_builtin_type("List[int]")


def test_is_builtin_negative():
    # 'Tensor' is not a builtin even though 'list of Tensor' contains 'list'.
    assert not is_builtin_type("Tensor")
    assert not is_builtin_type("List[Tensor]")
