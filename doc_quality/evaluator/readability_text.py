"""
Text-readability metrics for prose sections of the structured documentation.

The module wraps the ``textstat`` library to compute Flesch-Kincaid Grade
Level, Coleman-Liau Index, and Gunning Fog Index, and adds two lightweight
heuristics that don't require additional dependencies:

* a regex-based passive-voice detector
* a sentence-length variance computation

``textstat`` is treated as an *optional* dependency: if it is not installed
the readability evaluator still runs, but only the heuristic checks
contribute to the score and the FK/CL/GF metrics are reported as ``None``.
This keeps the package usable in minimal installs.
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from doc_quality.config import EvaluatorConfig
from doc_quality.doc_views import DocView
from doc_quality.issue_types import IssueType
from doc_quality.models import Dimension, DimensionScore, Issue
from doc_quality.presence import is_present


logger = logging.getLogger(__name__)


# ``textstat`` is optional; import lazily so a missing install does not
# break the package import. We probe it once and cache the import attempt.
try:
    import textstat as _textstat  # type: ignore
    _TEXTSTAT_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-dependent
    _textstat = None
    _TEXTSTAT_AVAILABLE = False


# Passive-voice heuristic: "to be" forms followed by a past-participle.
# Far simpler than a real dependency parse but useful enough for the
# small piece of signal we need. Overfires occasionally but is consistent.
_PASSIVE_RE = re.compile(
    r"\b(?:is|are|was|were|been|being|be)\b\s+"
    r"(?:[a-z]+ed|written|done|seen|made|known|given|taken|sent|found|"
    r"set|paid|kept|left|put|run|cut|read|built|brought|caught|told)\b",
    re.IGNORECASE,
)

# Sentence splitter. Keep it simple - linguistic accuracy is not needed, only a stable sentence count for FK and variance calculations.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Word/token splitter for length statistics.
_WORD_RE = re.compile(r"\w+")

# Minimum length below which it's not worth running readability formulas.
# Below this many words the formulas are dominated by single-sentence noise and produce unstable scores.
_MIN_TEXT_WORDS = 15


@dataclass
class TextMetrics:
    """Per-section readability metrics. ``None`` indicates "not computed"."""

    text_length_words: int
    sentence_count: int
    fk_grade: Optional[float]
    coleman_liau: Optional[float]
    gunning_fog: Optional[float]
    passive_density: float
    sentence_length_variance: float


def evaluate(view: DocView, config: EvaluatorConfig) -> Tuple[float, List[Issue], Dict[str, float]]:
    """
    sCompute text readability score, issues, and per-metric breakdown.

    Returns a tuple suitable for the readability aggregator. The score is in [0,1] (higher = more readable); 
    issues enumerate sections that breach configured thresholds.
    """
    issues: List[Issue] = []
    breakdown: Dict[str, float] = {}

    # Iterate through every text section in the document. Each section contributes a sub-score; the overall text score is the mean.
    sub_scores: List[float] = []

    # Track terse-purpose specifically because it has its own issue type and severity rather than the generic READ_TEXT_GRADE_TOO_HIGH
    purpose_text = view.get_purpose() or ""
    purpose_path = (
        "$.module_member_description.purpose"
        if "purpose" in str(view.get_purpose() or "") or hasattr(view, "get_purpose_additional_info")
        and view.get_purpose_additional_info() != []
        else "$.module_member_description"
    )
    if is_present(purpose_text):
        word_count = len(_WORD_RE.findall(purpose_text))
        if word_count < config.min_purpose_tokens:
            issues.append(Issue(
                issue_type=IssueType.READ_PURPOSE_TOO_TERSE,
                dimension=Dimension.READABILITY,
                severity=IssueType.READ_PURPOSE_TOO_TERSE.value.default_severity,
                section="module_member_description.purpose",
                target=None,
                json_path=purpose_path,
                detail=(f"Purpose has {word_count} tokens, below the recommended minimum of {config.min_purpose_tokens}."),
                doc_value=purpose_text,
                metadata={"word_count": word_count},
                maintainer_strategy=IssueType.READ_PURPOSE_TOO_TERSE.value.default_strategy
            ))

    # Sweep all prose sections via the DocView's iter_text_sections helper.
    for label, text, path in view.iter_text_sections():
        if not is_present(text):
            continue
        metrics, section_issues = _evaluate_section(label=label, text=text, json_path=path, config=config)
        issues.extend(section_issues)
        sub_score = _section_score(metrics, config)
        sub_scores.append(sub_score)

    text_score = (statistics.fmean(sub_scores) if sub_scores else 1.0)
    breakdown["TextReadability"] = text_score

    return text_score, issues, breakdown


def _evaluate_section(
    label: str,
    text: str,
    json_path: str,
    config: EvaluatorConfig,
) -> Tuple[TextMetrics, List[Issue]]:
    """Compute metrics and emit issues for a single prose section."""
    issues: List[Issue] = []

    # Word/sentence statistics.
    words = _WORD_RE.findall(text)
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    word_count = len(words)
    sent_count = max(len(sentences), 1)

    # Sentence-length variance. Useful as a stand-alone signal of
    # inconsistent prose structure even when grade levels look reasonable.
    sent_lens = [len(_WORD_RE.findall(s)) for s in sentences if s.strip()]
    sent_variance = (
        statistics.pstdev(sent_lens) if len(sent_lens) > 1 else 0.0
    )

    # Passive density: passive-construction count per sentence.
    passive_count = len(_PASSIVE_RE.findall(text))
    passive_density = passive_count / sent_count

    # textstat-backed metrics. Skip on short text to avoid spurious values.
    fk = cl = gf = None
    if _TEXTSTAT_AVAILABLE and word_count >= _MIN_TEXT_WORDS:
        try:
            fk = float(_textstat.flesch_kincaid_grade(text))
            cl = float(_textstat.coleman_liau_index(text))
            gf = float(_textstat.gunning_fog(text))
        except Exception as exc:  # pragma: no cover - textstat edge cases
            # textstat is robust but can throw on degenerate inputs.
            # Fall back to "not computed" rather than crashing.
            logger.debug("textstat failed on section %s: %s", label, exc)

    metrics = TextMetrics(
        text_length_words=word_count,
        sentence_count=sent_count,
        fk_grade=fk,
        coleman_liau=cl,
        gunning_fog=gf,
        passive_density=passive_density,
        sentence_length_variance=sent_variance
    )

    # ---- Issue emission ----
    # Grade-too-high issues use a single IssueType with severity determined by the worst-offender threshold breached.
    if fk is not None and fk >= config.fk_grade_high:
        issues.append(_grade_issue(
            label, text, json_path, fk,
            f"Flesch-Kincaid Grade Level {fk:.1f} >= {config.fk_grade_high}",
            severity_override=None
        ))
    elif fk is not None and fk >= config.fk_grade_medium:
        issues.append(_grade_issue(
            label, text, json_path, fk,
            f"Flesch-Kincaid Grade Level {fk:.1f} >= {config.fk_grade_medium}",
            severity_override="medium"
        ))
    if cl is not None and cl >= config.coleman_liau_medium:
        issues.append(_grade_issue(
            label, text, json_path, cl,
            f"Coleman-Liau index {cl:.1f} >= {config.coleman_liau_medium}",
            severity_override="medium"
        ))
    if gf is not None and gf >= config.gunning_fog_medium:
        issues.append(_grade_issue(
            label, text, json_path, gf,
            f"Gunning Fog index {gf:.1f} >= {config.gunning_fog_medium}",
            severity_override="medium"
        ))

    if passive_density >= config.passive_density_threshold and sent_count >= 3:
        issues.append(Issue(
            issue_type=IssueType.READ_PASSIVE_VOICE_HIGH,
            dimension=Dimension.READABILITY,
            severity=IssueType.READ_PASSIVE_VOICE_HIGH.value.default_severity,
            section=label,
            target=None,
            json_path=json_path,
            detail=(f"Passive-voice density ≈ {passive_density:.2f} per sentence (threshold {config.passive_density_threshold})."),
            doc_value=text,
            metadata={"passive_count": passive_count,
                      "sentence_count": sent_count},
            maintainer_strategy=IssueType.READ_PASSIVE_VOICE_HIGH.value.default_strategy
        ))

    return metrics, issues


def _grade_issue(
    label: str,
    text: str,
    json_path: str,
    metric_value: float,
    detail: str,
    severity_override: Optional[str],
) -> Issue:
    """Construct a READ_TEXT_GRADE_TOO_HIGH issue with the given details."""
    from doc_quality.models import Severity
    sev_default = IssueType.READ_TEXT_GRADE_TOO_HIGH.value.default_severity
    sev_map = {"low": Severity.LOW, "medium": Severity.MEDIUM, "high": Severity.HIGH}
    severity = sev_map.get(severity_override or "", sev_default)
    return Issue(
        issue_type=IssueType.READ_TEXT_GRADE_TOO_HIGH,
        dimension=Dimension.READABILITY,
        severity=severity,
        section=label,
        target=None,
        json_path=json_path,
        detail=detail,
        doc_value=text,
        metadata={"metric_value": metric_value},
        maintainer_strategy=IssueType.READ_TEXT_GRADE_TOO_HIGH.value.default_strategy
    )


def _section_score(metrics: TextMetrics, config: EvaluatorConfig) -> float:
    """Reduce the per-section TextMetrics to a [0,1] score.

    Each metric contributes a 0-or-1 penalty: above-threshold => 0, otherwise
    1. The section score is the mean of the contributing penalties so any
    single bad metric produces a partial deduction rather than a binary fail.
    """
    contributions: List[float] = []

    if metrics.fk_grade is not None:
        contributions.append(0.0 if metrics.fk_grade >= config.fk_grade_high else 1.0)
    if metrics.coleman_liau is not None:
        contributions.append(
            0.5 if metrics.coleman_liau >= config.coleman_liau_medium else 1.0
        )
    if metrics.gunning_fog is not None:
        contributions.append(
            0.5 if metrics.gunning_fog >= config.gunning_fog_medium else 1.0
        )

    # Passive density contribution: a soft penalty proportional to how far
    # over threshold we are, capped at the threshold's worth of penalty.
    if metrics.sentence_count >= 3:
        excess = max(metrics.passive_density - config.passive_density_threshold, 0.0)
        # Convert excess in [0, 1] to a penalty in [0, 0.5]; multiplier chosen
        # so being 0.4 over threshold removes a quarter of the section's score.
        contributions.append(max(0.0, 1.0 - min(excess * 1.25, 0.5)))

    if not contributions:
        return 1.0
    return statistics.fmean(contributions)
