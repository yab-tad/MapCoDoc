"""Tests for the artifact store - directory layout, JSON encoding, atomicity."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from doc_quality.artifacts.store import ArtifactStore, _sanitize_filename
from doc_quality.issue_types import IssueType
from doc_quality.models import (
    Dimension,
    DimensionScore,
    EvaluationReport,
    Issue,
    Severity,
)


def test_sanitize_basic():
    assert _sanitize_filename("a.b.c") == "a.b.c"
    # Colons (illegal on Windows) become underscores.
    assert _sanitize_filename("torch:ext") == "torch_ext"


def test_sanitize_collapses_underscores():
    assert _sanitize_filename("a<<b") == "a_b"


def test_save_and_load_original(tmp_path):
    store = ArtifactStore(tmp_path, "lib", "0.1")
    store.save_original("foo.bar", {"x": 1})
    assert store.load_original("foo.bar") == {"x": 1}


def test_save_original_is_idempotent(tmp_path):
    store = ArtifactStore(tmp_path, "lib", "0.1")
    store.save_original("foo", {"first": True})
    store.save_original("foo", {"second": True})
    assert store.load_original("foo") == {"first": True}


def test_save_evaluation_serializes_report(tmp_path):
    store = ArtifactStore(tmp_path, "lib", "0.1")
    issue = Issue(
        issue_type=IssueType.COMP_PURPOSE_MISSING,
        dimension=Dimension.COMPLETENESS,
        severity=Severity.HIGH,
        section="purpose",
        target=None,
        json_path="$.module_member_description.purpose",
        detail="missing",
    )
    report = EvaluationReport(
        member_id=1,
        member_fqn="lib.foo",
        member_api_name="lib.foo",
        member_type="function",
        is_inherited=False,
        code_truth_available=True,
        overall_score=0.5,
        dimensions={
            Dimension.COMPLETENESS: DimensionScore(score=0.5, issues=[issue]),
            Dimension.ACCURACY: DimensionScore(score=1.0, issues=[]),
            Dimension.READABILITY: DimensionScore(score=1.0, issues=[]),
            Dimension.MAINTAINABILITY: DimensionScore(score=1.0, issues=[]),
        },
        evaluation_timestamp=datetime.now(timezone.utc),
    )
    path = store.save_evaluation(report)
    assert path.exists()
    payload = store.load_evaluation("lib.foo")
    assert payload["member_api_name"] == "lib.foo"
    # The Issue's IssueType should round-trip via its code field.
    issues = payload["dimensions"]["completeness"]["issues"]
    assert issues[0]["issue_type"] == "COMP_PURPOSE_MISSING"
    # Severity serialized as its enum value.
    assert issues[0]["severity"] == "high"


def test_log_appends(tmp_path):
    store = ArtifactStore(tmp_path, "lib", "0.1")
    store.save_original("foo", {})
    store.save_original("bar", {})
    log_lines = store.log_path.read_text().strip().splitlines()
    assert len(log_lines) == 2
