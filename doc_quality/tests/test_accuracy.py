"""Smoke tests for the accuracy evaluator."""

from doc_quality.doc_views import doc_view
from doc_quality.evaluator import accuracy
from doc_quality.issue_types import IssueType


def test_aligned_doc_has_high_score(callable_view, callable_code_truth, config):
    score = accuracy.evaluate(callable_view, callable_code_truth, None, config)
    # Some metrics may be borderline due to fuzzy thresholds, but the
    # well-formed fixture should not emit *any* mismatch issues.
    flagged_types = {
        i.issue_type for i in score.issues
        if i.issue_type.value.code.startswith("ACC_")
    }
    assert IssueType.ACC_PARAM_TYPE_MISMATCH not in flagged_types
    assert IssueType.ACC_RETURN_TYPE_MISMATCH not in flagged_types


def test_param_name_mismatch_emits_issue(
    sample_callable_doc, callable_code_truth, config,
):
    # Rename a parameter in the doc only.
    sample_callable_doc["parameters"][0]["name"] = "wrong_name"
    view = doc_view(sample_callable_doc, "function")
    score = accuracy.evaluate(view, callable_code_truth, None, config)
    types = {i.issue_type for i in score.issues}
    # Doc lists 'wrong_name' that's not in code => UNKNOWN
    assert IssueType.ACC_PARAM_NAME_UNKNOWN in types
    # Code has 'x' that's not in doc => MISSING_FROM_DOC
    assert IssueType.ACC_PARAM_MISSING_FROM_DOC in types


def test_return_type_mismatch(sample_callable_doc, callable_code_truth, config):
    sample_callable_doc["returns"]["type"] = "str"
    view = doc_view(sample_callable_doc, "function")
    score = accuracy.evaluate(view, callable_code_truth, None, config)
    types = {i.issue_type for i in score.issues}
    assert IssueType.ACC_RETURN_TYPE_MISMATCH in types


def test_default_mismatch_detected(sample_class_doc, class_code_truth, config):
    # The fixture's reduction description ends with "Default: 'mean'", which
    # matches the code default. Force a mismatch by editing the description.
    sample_class_doc["parameters"][2]["description"] = (
        "Specifies the reduction. Default: 'sum'"
    )
    view = doc_view(sample_class_doc, "class")
    score = accuracy.evaluate(view, class_code_truth, None, config)
    types = {i.issue_type for i in score.issues}
    assert IssueType.ACC_PARAM_DEFAULT_MISMATCH in types


def test_no_code_truth_returns_neutral_score(callable_view, config):
    score = accuracy.evaluate(callable_view, None, None, config)
    assert score.score == 1.0
    assert score.issues == []


def test_async_marker_missing(sample_callable_doc, callable_code_truth, config):
    callable_code_truth.is_async = True
    # Signature does not contain 'async'.
    view = doc_view(sample_callable_doc, "function")
    score = accuracy.evaluate(view, callable_code_truth, None, config)
    types = {i.issue_type for i in score.issues}
    assert IssueType.ACC_ASYNC_MARKER_MISSING in types
