"""Smoke tests for the completeness evaluator."""

import copy

from doc_quality.evaluator import completeness
from doc_quality.issue_types import IssueType


def test_well_formed_callable_has_high_score(callable_view, callable_code_truth, config):
    score = completeness.evaluate(callable_view, callable_code_truth, None, config)
    assert score.score > 0.9
    assert all(i.issue_type != IssueType.COMP_PURPOSE_MISSING for i in score.issues)


def test_missing_purpose_emits_issue(sample_callable_doc, callable_code_truth, config):
    sample_callable_doc["module_member_description"] = "N/A"
    from doc_quality.doc_views import doc_view
    view = doc_view(sample_callable_doc, "function")
    score = completeness.evaluate(view, callable_code_truth, None, config)
    issue_types = {i.issue_type for i in score.issues}
    assert IssueType.COMP_PURPOSE_MISSING in issue_types


def test_missing_param_type_emits_issue(sample_callable_doc, callable_code_truth, config):
    sample_callable_doc["parameters"][0]["type"] = "N/A"
    from doc_quality.doc_views import doc_view
    view = doc_view(sample_callable_doc, "function")
    score = completeness.evaluate(view, callable_code_truth, None, config)
    issue_types = {i.issue_type for i in score.issues}
    assert IssueType.COMP_PARAM_TYPE_MISSING in issue_types
    # The patcher needs the code_value to fix it; verify it was populated.
    type_issue = next(i for i in score.issues
                      if i.issue_type == IssueType.COMP_PARAM_TYPE_MISSING)
    assert type_issue.code_value == "int"


def test_no_examples_emits_issue(sample_callable_doc, callable_code_truth, config):
    sample_callable_doc["examples"] = []
    from doc_quality.doc_views import doc_view
    view = doc_view(sample_callable_doc, "function")
    score = completeness.evaluate(view, callable_code_truth, None, config)
    issue_types = {i.issue_type for i in score.issues}
    assert IssueType.COMP_NO_EXAMPLES in issue_types


def test_class_has_no_returns_check(class_view, class_code_truth, config):
    """Classes should never produce COMP_RETURN_TYPE_MISSING."""
    score = completeness.evaluate(class_view, class_code_truth, None, config)
    assert IssueType.COMP_RETURN_TYPE_MISSING not in {i.issue_type for i in score.issues}


def test_void_callable_does_not_require_returns(callable_code_truth, config):
    """A function with no return annotation shouldn't be flagged for missing returns doc."""
    callable_code_truth.returns = None
    doc = {
        "module_member_signature": "def void_func()",
        "module_member_description": "Does nothing.",
        "parameters": [],
        "returns": {"type": "N/A", "description": "N/A", "additional_information": "N/A"},
        "examples": [{"example": "void_func()", "additional_information": "N/A"}],
        "additional_notes": {"supplementary_information": [], "edge_cases": []},
    }
    from doc_quality.doc_views import doc_view
    view = doc_view(doc, "function")
    score = completeness.evaluate(view, callable_code_truth, None, config)
    types = {i.issue_type for i in score.issues}
    assert IssueType.COMP_RETURN_TYPE_MISSING not in types
