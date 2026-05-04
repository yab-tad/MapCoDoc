"""
Configuration for the doc_quality evaluator and maintainer.

All thresholds, weights, and feature flags live here so that calibration
adjustments can be made by editing a YAML file rather than the code.
A default-configured ``EvaluatorConfig()`` gives sensible behaviour for the
v1 metric set; advanced uses load a YAML file via ``EvaluatorConfig.from_yaml``.

The configuration is split into per-dimension blocks plus a top-level
section that controls the maintainer and orchestrator. Where weights are
expressed as dicts they sum to 1.0 (or are normalized at usage time so they
do).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from doc_quality.models import Dimension, MaintainerStrategy, Severity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default weight tables
# ---------------------------------------------------------------------------
# Documented per-metric weights. These are the values used when no config
# file overrides them. They are deliberately tuned to favor structural
# concerns (parameter coverage, type completeness) over discretionary ones
# (additional notes), reflecting the empirical findings of Aghajani et al.
# and Uddin & Robillard on what developers care about in API docs.

_DEFAULT_COMPLETENESS_WEIGHTS: Dict[str, float] = {
    "SigCov": 0.10,    # signature presence
    "PurpCov": 0.20,   # purpose description presence
    "PaC": 0.20,       # parameter name coverage
    "PTC": 0.15,       # parameter type completeness
    "PDC": 0.15,       # parameter description completeness
    "RC": 0.10,        # return coverage (callables only; redistributed for classes)
    "ExC": 0.10,       # example coverage
}

_DEFAULT_ACCURACY_WEIGHTS: Dict[str, float] = {
    "PCA": 0.10,
    "PNA": 0.15,
    "PTC_acc": 0.20,
    "PDA": 0.15,
    "RTC": 0.10,
    "SigDrift": 0.20,
    "AsyncMarker": 0.05,
    "DeprecatedMarker": 0.05,
}

_DEFAULT_READABILITY_WEIGHTS: Dict[str, float] = {
    "text": 0.6,
    "code": 0.4,
}

_DEFAULT_MAINTAINABILITY_WEIGHTS: Dict[str, float] = {
    "HD": 0.20,         # hyperlink density
    "TXRefC": 0.30,     # type cross-ref coverage
    "BTRefC": 0.20,     # builtin type ref coverage
    "IXRefC": 0.20,     # internal cross-ref coverage
    "ITR": 0.10,        # inline type restatement (penalty)
}

_DEFAULT_OVERALL_WEIGHTS: Dict[Dimension, float] = {
    Dimension.COMPLETENESS: 0.35,
    Dimension.ACCURACY: 0.35,
    Dimension.READABILITY: 0.15,
    Dimension.MAINTAINABILITY: 0.15,
}


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class EvaluatorConfig:
    """All tunables for the evaluator and maintainer in one place.

    Defaults are reasonable for general use; calibration against a labelled
    set typically nudges the readability thresholds and the maintainability
    hyperlink-density bounds.
    """

    # -- Completeness thresholds -----------------------------------------
    # Below this many tokens, a description is treated as effectively
    # absent for the "param description completeness" check. Three tokens
    # accommodates short types like "the index" while excluding terse
    # restatements like "an int".
    min_desc_tokens: int = 3

    # Below this many tokens, the purpose description is flagged as too
    # terse (READ_PURPOSE_TOO_TERSE).
    min_purpose_tokens: int = 8

    # If True, missing examples produce a COMP_NO_EXAMPLES issue. Some
    # libraries (notably internal helpers) deliberately omit examples;
    # the flag lets calibrators turn this off.
    require_examples: bool = True

    # -- Accuracy thresholds ---------------------------------------------
    # Jaccard similarity at or above which two type strings are deemed
    # equivalent. 0.8 tolerates one extra qualifier token without
    # collapsing genuinely different types.
    type_fuzzy_threshold: float = 0.8

    # Normalized edit-distance at or above which the documented signature
    # is considered to have drifted from the code signature.
    sig_drift_threshold: float = 0.30

    # -- Readability text thresholds -------------------------------------
    fk_grade_high: float = 18.0     # FK Grade Level above this -> HIGH severity
    fk_grade_medium: float = 14.0   # FK Grade Level above this -> MEDIUM severity
    coleman_liau_medium: float = 16.0
    gunning_fog_medium: float = 17.0
    passive_density_threshold: float = 0.4

    # -- Readability code thresholds -------------------------------------
    # An example with more than this many lines of code is flagged as
    # too long to serve as a clean illustration.
    example_max_loc: int = 30
    # McCabe cyclomatic complexity above this is flagged.
    example_max_cc: int = 5
    # If more than this fraction of identifiers are single-character,
    # the example is flagged for poor identifier quality.
    single_char_ratio_threshold: float = 0.5

    # -- Maintainability thresholds --------------------------------------
    # Below this fraction of (links/sentences), section is "no hyperlinks".
    hyperlink_density_min: float = 0.05
    # Above this, "over-linking" warning.
    hyperlink_density_max: float = 1.0

    # -- Aggregation weights ---------------------------------------------
    weights_completeness: Dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_COMPLETENESS_WEIGHTS),
    )
    weights_accuracy: Dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_ACCURACY_WEIGHTS),
    )
    weights_readability: Dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_READABILITY_WEIGHTS),
    )
    weights_maintainability: Dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_MAINTAINABILITY_WEIGHTS),
    )
    weights_overall: Dict[Dimension, float] = field(
        default_factory=lambda: dict(_DEFAULT_OVERALL_WEIGHTS),
    )

    # -- Maintainer settings ---------------------------------------------
    # Strategies the maintainer is allowed to execute. Default excludes
    # LLM and MANUAL: deterministic patches only in v1.
    enabled_strategies: List[MaintainerStrategy] = field(
        default_factory=lambda: [
            MaintainerStrategy.DB_QUERY,
            MaintainerStrategy.AST_DERIVED,
        ],
    )

    # Issues with severity below this threshold are *not* attempted by the
    # maintainer (they still appear in evaluation reports). Default:
    # MEDIUM, so LOW issues are surfaced for review but not auto-patched.
    min_severity_for_maintenance: Severity = Severity.MEDIUM

    # -- Misc ------------------------------------------------------------
    # When True, the orchestrator skips members where ``code_truth`` is
    # None (external inherited). When False, those members still get a
    # report with ``skipped=True`` for record-keeping.
    skip_when_no_code_truth: bool = False

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: Path) -> "EvaluatorConfig":
        """Load an ``EvaluatorConfig`` from a YAML file.

        Unknown keys are ignored with a warning; missing keys retain
        their dataclass defaults. This makes config files
        forward-compatible: a config written for v0.1 still loads under
        v0.2 even if new tunables have been added.
        """
        # Imported lazily so that ``import doc_quality`` doesn't pull
        # PyYAML at import time. PyYAML is a transitive dependency in
        # this project but a runtime ImportError here is more useful
        # than an import-time one.
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"Config file {path} must contain a top-level mapping; "
                f"got {type(raw).__name__}",
            )

        # Discover the field names of this dataclass so we can ignore
        # unknown keys with a meaningful warning.
        from dataclasses import fields
        valid = {f.name for f in fields(cls)}

        accepted: Dict[str, object] = {}
        for key, val in raw.items():
            if key not in valid:
                logger.warning(
                    "Ignoring unknown config key %r in %s", key, path,
                )
                continue
            accepted[key] = val

        # Special handling for fields whose YAML representation differs
        # from the dataclass form: enums and Severity values.
        if "min_severity_for_maintenance" in accepted and isinstance(
            accepted["min_severity_for_maintenance"], str,
        ):
            accepted["min_severity_for_maintenance"] = Severity(
                accepted["min_severity_for_maintenance"].lower(),
            )
        if "enabled_strategies" in accepted and isinstance(
            accepted["enabled_strategies"], list,
        ):
            accepted["enabled_strategies"] = [
                MaintainerStrategy(s) for s in accepted["enabled_strategies"]
            ]
        if "weights_overall" in accepted:
            # Convert string-keyed dimension names to Dimension enum.
            accepted["weights_overall"] = {
                Dimension(k): v for k, v in accepted["weights_overall"].items()
            }

        return cls(**accepted)

    def get_default_severity(self, severity: Severity) -> int:
        """Convert a Severity to a numeric ordering for comparisons."""
        # Used by the maintainer to apply ``min_severity_for_maintenance``.
        return {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}[severity]
