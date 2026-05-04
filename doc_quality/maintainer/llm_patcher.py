"""
LLM-strategy patcher (v1.x stub).

This module defines the interface the maintainer would call to obtain
LLM-generated patches for issues whose strategy is ``LLM`` (e.g. missing
descriptions, prose readability rewrites, deprecation notes).

In v1 the implementation is intentionally a stub: the ``build_patches``
method always returns an empty list. The seam exists so v1.x can plug in a
concrete generator (likely re-using the existing OpenAI client wired in
``doc_processor/structured_doc_extracter.py``) without restructuring the
maintainer.

When implementing v1.x, keep the contract in mind:

* One LLM call per issue, not one call per member; targeted edits keep
  prompts short and outputs validatable.
* Each call returns a single value (the new field content) plus a
  confidence score. Both are stored on the produced ``MaintenancePatch``.
* Issues whose LLM output fails JSON-schema or sanity validation should
  result in *no* patch (an empty list contribution); the maintainer
  surfaces them for manual review.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from doc_quality.code_truth_resolver import CodeTruth
from doc_quality.models import Issue, MaintenancePatch


logger = logging.getLogger(__name__)


class LlmPatcher:
    """Stub for LLM-driven patch generation. v1.x will provide a real impl."""

    def __init__(self, client=None) -> None:
        # Accept a client argument so v1.x can pass in a configured
        # OpenAI / equivalent without a constructor change.
        self._client = client

    def build_patches(
        self,
        issues: List[Issue],
        code_truth: Optional[CodeTruth] = None
    ) -> List[MaintenancePatch]:
        """Return an empty list. Subclasses or v1.x replace this method."""
        if any(i.maintainer_strategy.value == "llm" for i in issues):
            logger.info(
                "LlmPatcher (stub) skipping %d LLM-strategy issues - implement v1.x to enable.",
                sum(1 for i in issues if i.maintainer_strategy.value == "llm")
            )
        return []
