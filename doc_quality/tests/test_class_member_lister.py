"""Tests for the AST-based portion of ClassMemberLister."""

from doc_quality.class_member_lister import ClassMemberLister


def test_extract_self_attrs_basic():
    src = (
        "class Foo:\n"
        "    def __init__(self):\n"
        "        self.x = 1\n"
        "        self.y = 2\n"
    )
    found = ClassMemberLister._extract_instance_attrs(src)
    assert "x" in found and "y" in found


def test_extract_self_attrs_annotated():
    src = (
        "class Foo:\n"
        "    def __init__(self):\n"
        "        self.size: int = 10\n"
    )
    found = ClassMemberLister._extract_instance_attrs(src)
    assert "size" in found


def test_extract_class_body_assigns():
    src = (
        "class Foo:\n"
        "    MAX_LEN = 100\n"
        "    NAME: str = 'foo'\n"
    )
    found = ClassMemberLister._extract_instance_attrs(src)
    assert "MAX_LEN" in found
    assert "NAME" in found


def test_excludes_private_names():
    src = (
        "class Foo:\n"
        "    def __init__(self):\n"
        "        self._private = 1\n"
        "        self.__dunder__ = 1\n"
        "        self.public = 1\n"
    )
    found = ClassMemberLister._extract_instance_attrs(src)
    assert "public" in found
    assert "_private" not in found
    assert "__dunder__" not in found


def test_does_not_hoist_nested_class_attrs():
    src = (
        "class Outer:\n"
        "    OUTER_VAR = 1\n"
        "    class Inner:\n"
        "        INNER_VAR = 2\n"
        "        def __init__(self):\n"
        "            self.inner_attr = 3\n"
    )
    found = ClassMemberLister._extract_instance_attrs(src)
    # Outer's class-body assignments are present; Inner's class-body
    # assignment is NOT (we don't hoist).
    assert "OUTER_VAR" in found
    assert "INNER_VAR" not in found
    # ``self.inner_attr`` is collected by the global self.X walk.
    # Whether we want it depends on philosophy; the current implementation
    # *does* collect it because there's no scope tracking.
    assert "inner_attr" in found


def test_handles_unparseable_source_gracefully():
    src = "class Foo:\n    def __init__(self)\n        self.x = 1\n"
    # SyntaxError - the static method propagates the exception; the
    # caller in ClassMemberLister.list_members catches it.
    import pytest
    with pytest.raises(SyntaxError):
        ClassMemberLister._extract_instance_attrs(src)
