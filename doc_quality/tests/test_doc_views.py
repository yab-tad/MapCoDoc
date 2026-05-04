"""Tests for the DocView polymorphism."""

import pytest

from doc_quality.doc_views import ClassDocView, CallableDocView, doc_view


def test_factory_returns_class_view_for_class(sample_class_doc):
    view = doc_view(sample_class_doc, "class")
    assert isinstance(view, ClassDocView)


def test_factory_returns_callable_view_for_function(sample_callable_doc):
    view = doc_view(sample_callable_doc, "function")
    assert isinstance(view, CallableDocView)


def test_factory_returns_callable_view_for_method(sample_callable_doc):
    view = doc_view(sample_callable_doc, "method")
    assert isinstance(view, CallableDocView)


def test_class_purpose_extracted(class_view):
    p = class_view.get_purpose()
    assert p and "criterion" in p


def test_class_no_returns(class_view):
    assert class_view.get_returns() is None


def test_class_attributes_methods_default_to_lists(class_view):
    assert class_view.get_attributes() == []
    assert class_view.get_methods() == []


def test_callable_purpose_is_string(callable_view):
    assert callable_view.get_purpose() == "Add two integers."


def test_callable_returns_present(callable_view):
    ret = callable_view.get_returns()
    assert ret is not None
    assert ret["type"] == "int"


def test_callable_attributes_empty(callable_view):
    assert callable_view.get_attributes() == []


def test_iter_text_sections_contains_purpose(class_view):
    sections = class_view.iter_text_sections()
    paths = [s[2] for s in sections]
    assert "$.module_member_description.purpose" in paths


def test_iter_text_sections_contains_param_descriptions(class_view):
    sections = class_view.iter_text_sections()
    labels = [s[0] for s in sections]
    # Each parameter description should be a section.
    assert any("size_average" in lab for lab in labels)
    assert any("reduction" in lab for lab in labels)


def test_callable_iter_text_sections_contains_returns(callable_view):
    sections = callable_view.iter_text_sections()
    labels = [s[0] for s in sections]
    assert "returns.description" in labels


def test_callable_purpose_falls_back_when_class_shape(sample_class_doc):
    # Defensive: a callable view over an accidentally class-shaped dict
    # should still surface a purpose string.
    view = doc_view(sample_class_doc, "function")
    assert view.get_purpose() and "criterion" in view.get_purpose()
