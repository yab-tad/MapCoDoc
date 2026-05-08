"""Tests for the DB-query patcher."""

from doc_quality.evaluator.maintainability import BUILTIN_TYPE_URLS
from doc_quality.issue_types import IssueType
from doc_quality.maintainer.db_patcher import DbPatcher
from doc_quality.models import (
    Dimension,
    Issue,
    MaintainerStrategy,
    Severity
)


def _issue(
    issue_type=IssueType.COMP_PARAM_TYPE_MISSING,
    code_value="int",
    doc_value="N/A",
    target="x",
    metadata=None
):
    """Build a minimal Issue object for tests."""
    spec = issue_type.value
    return Issue(
        issue_type=issue_type,
        dimension=spec.dimension,
        severity=spec.default_severity,
        section="parameters",
        target=target,
        json_path=spec.render_path(target=target) if target else spec.json_path_template,
        detail="test",
        code_value=code_value,
        doc_value=doc_value,
        maintainer_strategy=spec.default_strategy,
        metadata=metadata or {}
    )


def test_param_type_missing_patch_uses_code_value():
    patcher = DbPatcher()
    patches = patcher.build_patches([_issue()])
    assert len(patches) == 1
    assert patches[0].new_value == "int"
    assert patches[0].strategy == MaintainerStrategy.DB_QUERY


def test_param_name_missing_patch_inserts_param():
    patcher = DbPatcher()
    issue = _issue(
        issue_type=IssueType.COMP_PARAM_NAME_MISSING,
        code_value={"name": "y", "type": "int", "default": "0"},
        target="y"
    )
    patches = patcher.build_patches([issue])
    assert len(patches) == 1
    p = patches[0]
    assert p.new_value["name"] == "y"
    assert p.new_value["type"] == "int"
    # Default should be reflected in the description.
    assert "Default: 0" in p.new_value["description"]


def test_no_code_value_yields_no_patch():
    patcher = DbPatcher()
    patches = patcher.build_patches([_issue(code_value=None)])
    assert patches == []


def test_builtin_link_patch_wraps_with_url():
    patcher = DbPatcher()
    issue = _issue(
        issue_type=IssueType.MAINT_BUILTIN_NOT_LINKED,
        doc_value="bool",
        target="x",
        metadata={"builtin": "bool"}
    )
    patches = patcher.build_patches([issue])
    assert len(patches) == 1
    assert "bool(" in patches[0].new_value
    assert BUILTIN_TYPE_URLS["bool"] in patches[0].new_value


def test_only_processes_db_query_issues():
    """An issue with non-DB_QUERY strategy is not picked up."""
    issue = _issue()
    issue.maintainer_strategy = MaintainerStrategy.LLM
    patches = DbPatcher().build_patches([issue])
    assert patches == []
