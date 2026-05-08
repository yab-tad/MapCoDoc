"""Smoke tests for the maintainability evaluator."""

import copy

from doc_quality.doc_views import doc_view
from doc_quality.evaluator import maintainability
from doc_quality.issue_types import IssueType


def test_unreplaced_placeholder_flagged_high(sample_callable_doc, callable_code_truth, config):
    sample_callable_doc["module_member_description"] = ("Adds two numbers. See url_placeholder_3 for details.")
    view = doc_view(sample_callable_doc, "function")
    score = maintainability.evaluate(view, callable_code_truth, None, config)
    types = {i.issue_type for i in score.issues}
    assert IssueType.MAINT_BROKEN_PLACEHOLDER in types


def test_builtin_not_linked_emits_issue(sample_callable_doc, callable_code_truth, config):
    # The fixture's params have plain ``int`` types - no link.
    view = doc_view(sample_callable_doc, "function")
    score = maintainability.evaluate(view, callable_code_truth, None, config)
    types = {i.issue_type for i in score.issues}
    # 'int' parameters should be flagged for missing builtin link.
    assert IssueType.MAINT_BUILTIN_NOT_LINKED in types


def test_linked_builtin_does_not_emit_issue(sample_callable_doc, callable_code_truth, config):
    # The L1Loss-style ``bool(URL)`` shape suppresses the issue.
    sample_callable_doc["parameters"][0]["type"] = ("int(https://docs.python.org/3/library/functions.html#int)")
    sample_callable_doc["parameters"][1]["type"] = ("int(https://docs.python.org/3/library/functions.html#int)")
    sample_callable_doc["returns"]["type"] = ("int(https://docs.python.org/3/library/functions.html#int)")
    view = doc_view(sample_callable_doc, "function")
    score = maintainability.evaluate(view, callable_code_truth, None, config)
    types = {i.issue_type for i in score.issues}
    assert IssueType.MAINT_BUILTIN_NOT_LINKED not in types
