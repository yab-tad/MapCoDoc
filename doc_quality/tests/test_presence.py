"""Tests for the ``is_present`` predicate and helpers."""

from doc_quality.presence import coalesce_text, first_present, is_present


def test_none_is_absent():
    assert not is_present(None)


def test_empty_string_is_absent():
    assert not is_present("")
    assert not is_present("   ")


def test_na_sentinels_are_absent():
    # All recognized sentinels and their case variants.
    for s in ("N/A", "n/a", "NA", "na", "None", "NULL", "TBD"):
        assert not is_present(s), f"{s!r} should be absent"


def test_empty_collections_are_absent():
    assert not is_present([])
    assert not is_present({})
    assert not is_present(())
    assert not is_present(set())


def test_empty_collections_with_allow_zero_length():
    # When the caller opts in, empties become "present".
    assert is_present([], allow_zero_length=True)


def test_zero_and_false_are_present():
    # Falsy Python values are still informational content.
    assert is_present(0)
    assert is_present(False)


def test_real_strings_are_present():
    assert is_present("hello")
    assert is_present("  hello  ")


def test_first_present_picks_first():
    assert first_present(None, "", "N/A", "real") == "real"
    assert first_present(None, "") is None


def test_coalesce_text_returns_empty_for_absent():
    assert coalesce_text(None) == ""
    assert coalesce_text("N/A") == ""


def test_coalesce_text_joins_lists():
    assert coalesce_text(["a", "b", "N/A", "c"]) == "a b c"
