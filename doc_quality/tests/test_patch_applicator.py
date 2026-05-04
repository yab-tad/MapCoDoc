"""Tests for the JSON patch applicator."""

import copy

from doc_quality.issue_types import IssueType
from doc_quality.maintainer.patch_applicator import PatchApplicator
from doc_quality.models import Issue, MaintainerStrategy, MaintenancePatch


def _patch(json_path, new_value, old_value=None):
    """Build a minimal MaintenancePatch for tests."""
    spec = IssueType.COMP_PARAM_TYPE_MISSING.value
    issue = Issue(
        issue_type=IssueType.COMP_PARAM_TYPE_MISSING,
        dimension=spec.dimension,
        severity=spec.default_severity,
        section="parameters",
        target=None,
        json_path=json_path,
        detail="t",
    )
    return MaintenancePatch(
        issue=issue,
        json_path=json_path,
        old_value=old_value,
        new_value=new_value,
        strategy=MaintainerStrategy.DB_QUERY,
    )


def test_simple_field_patch():
    doc = {"module_member_signature": "old"}
    patches = [_patch("$.module_member_signature", "new", "old")]
    out = PatchApplicator().apply_patches(doc, patches)
    assert out["module_member_signature"] == "new"
    # Original is untouched.
    assert doc["module_member_signature"] == "old"


def test_param_field_patch():
    doc = {
        "parameters": [
            {"name": "x", "type": "N/A", "description": "?", "additional_information": "N/A"},
        ]
    }
    patches = [_patch("$.parameters[?name=='x'].type", "int")]
    out = PatchApplicator().apply_patches(doc, patches)
    assert out["parameters"][0]["type"] == "int"


def test_param_insertion_via_parameters_root():
    doc = {"parameters": []}
    new_param = {"name": "x", "type": "int", "description": "?", "additional_information": "N/A"}
    patches = [_patch("$.parameters", new_param)]
    out = PatchApplicator().apply_patches(doc, patches)
    assert out["parameters"][0]["name"] == "x"


def test_none_new_value_is_skipped():
    """A patch with new_value=None must be silently skipped."""
    doc = {"k": "old"}
    patches = [_patch("$.k", None)]
    out = PatchApplicator().apply_patches(doc, patches)
    assert out == {"k": "old"}
