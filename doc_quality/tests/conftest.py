"""
Shared pytest fixtures for doc_quality tests.

These fixtures construct minimal in-memory DocViews and CodeTruths for
unit tests of the dimension evaluators. The fixtures are intentionally
hand-built rather than mocked; that way an evaluator change that breaks
expectations is visible as a concrete diff in the fixture data.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from doc_quality.code_truth_resolver import CodeTruth
from doc_quality.config import EvaluatorConfig
from doc_quality.doc_views import doc_view


# ---------------------------------------------------------------------------
# Sample structured documentation fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_class_doc() -> Dict:
    """A complete, well-formed class api_reference (modeled after L1Loss)."""
    return {
        "module_member_signature": (
            "class torch.nn.L1Loss(size_average=None, reduce=None, reduction='mean')"
        ),
        "module_member_description": {
            "purpose": "Creates a criterion that measures the mean absolute error.",
            "additional_information": [
                "The unreduced loss is computed element-wise.",
            ],
        },
        "parameters": [
            {
                "name": "size_average",
                "type": "bool(https://docs.python.org/3/library/functions.html#bool), optional",
                "description": "Deprecated; see reduction.",
                "additional_information": "N/A",
            },
            {
                "name": "reduce",
                "type": "bool, optional",
                "description": "Deprecated; see reduction.",
                "additional_information": "N/A",
            },
            {
                "name": "reduction",
                "type": "str, optional",
                "description": "Specifies the reduction. Default: 'mean'",
                "additional_information": "N/A",
            },
        ],
        "attributes": [],
        "methods": [],
        "examples": [
            {
                "example": "```\n>>> loss = nn.L1Loss()\n```",
                "additional_information": "N/A",
            },
        ],
        "additional_notes": {
            "supplementary_information": [],
            "edge_cases": [],
        },
    }


@pytest.fixture
def sample_callable_doc() -> Dict:
    """A complete, well-formed function/method api_reference."""
    return {
        "module_member_signature": "def add(x: int, y: int) -> int",
        "module_member_description": "Add two integers.",
        "parameters": [
            {
                "name": "x", "type": "int",
                "description": "The first integer.",
                "additional_information": "N/A",
            },
            {
                "name": "y", "type": "int",
                "description": "The second integer.",
                "additional_information": "N/A",
            },
        ],
        "returns": {
            "type": "int",
            "description": "The sum of x and y.",
            "additional_information": "N/A",
        },
        "examples": [
            {
                "example": "```python\n# Add two ints.\nadd(1, 2)\n```",
                "additional_information": "N/A",
            },
        ],
        "additional_notes": {
            "supplementary_information": [],
            "edge_cases": [],
        },
    }


@pytest.fixture
def class_view(sample_class_doc):
    """ClassDocView wrapping the L1Loss-like fixture."""
    return doc_view(sample_class_doc, "class")


@pytest.fixture
def callable_view(sample_callable_doc):
    """CallableDocView wrapping the add(x, y) fixture."""
    return doc_view(sample_callable_doc, "function")


# ---------------------------------------------------------------------------
# CodeTruth fixtures
# ---------------------------------------------------------------------------

def _ct_param(name: str, type_: str = None, default: Any = None) -> Dict:
    """Helper to build a code-truth parameter dict."""
    return {
        "name": name,
        "type": type_,
        "default": default,
        "is_positional_only": False,
        "is_keyword_only": False,
        "is_vararg": False,
        "is_kwarg": False,
    }


@pytest.fixture
def class_code_truth() -> CodeTruth:
    """CodeTruth matching ``sample_class_doc`` (L1Loss constructor)."""
    return CodeTruth(
        fqn="torch.nn.modules.loss.L1Loss",
        api_name="torch.nn.L1Loss",
        member_type="class",
        parameters=[
            _ct_param("self"),
            _ct_param("size_average", "Optional[bool]", "None"),
            _ct_param("reduce", "Optional[bool]", "None"),
            _ct_param("reduction", "str", "'mean'"),
        ],
        returns=None,
        signatures={
            "full": "L1Loss(self, size_average: Optional[bool]=None, reduce: Optional[bool]=None, reduction: str='mean')",
            "no_types": "L1Loss(self, size_average=None, reduce=None, reduction='mean')",
        },
        decorators=[],
        is_async=False,
        is_static=False,
        is_abstract=False,
        is_property=False,
        is_inherited=False,
        source_code="class L1Loss:\n    def __init__(self, size_average=None, reduce=None, reduction='mean'):\n        self.reduction = reduction\n",
        docstring="L1Loss criterion.",
    )


@pytest.fixture
def callable_code_truth() -> CodeTruth:
    """CodeTruth matching ``sample_callable_doc`` (add(x, y))."""
    return CodeTruth(
        fqn="example.add",
        api_name="example.add",
        member_type="function",
        parameters=[
            _ct_param("x", "int"),
            _ct_param("y", "int"),
        ],
        returns={"type": "int", "description": ""},
        signatures={
            "full": "add(x: int, y: int) -> int",
            "no_types": "add(x, y)",
        },
        decorators=[],
        is_async=False,
        is_static=False,
        is_abstract=False,
        is_property=False,
        is_inherited=False,
        source_code="def add(x: int, y: int) -> int:\n    return x + y",
        docstring="Add two integers.",
    )


# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def config() -> EvaluatorConfig:
    """Default EvaluatorConfig used in unit tests."""
    return EvaluatorConfig()
