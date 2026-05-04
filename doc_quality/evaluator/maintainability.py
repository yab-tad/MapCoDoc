"""
Maintainability dimension evaluator.

Maintainability captures how well the documentation uses cross-references
and hyperlinks to canonical sources instead of inlining content that would
otherwise need to be kept in sync. The hypothesis (well-supported in the
documentation-engineering literature) is that documentation with rich,
correct cross-references is easier to keep accurate over time because
canonical content updates propagate via the link rather than having to be manually copied.

The metrics implemented here:

* **HD**     - hyperlink density per text section. Sections with type or
               member mentions but no links are flagged.
* **TXRefC** - parameter type cross-reference coverage. A type that names
               a known DB member should be hyperlinked.
* **BTRefC** - builtin type reference coverage. Mentions of ``int``,
               ``str``, ``bool`` etc. should link to the Python docs.
* **IXRefC** - internal cross-reference coverage. Mentions of other library
               members in prose text should be hyperlinked.
* **ITR**    - inline type restatement detection. A description that
               restates the type in prose (rather than linking) is a
               maintainability anti-pattern.

The maintainer can fix many of these deterministically (e.g. wrap a
builtin type with a link to the Python docs) which makes maintainability
particularly amenable to ``DB_QUERY``-strategy patches.
"""

from __future__ import annotations

import logging
import re
import statistics
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from doc_quality.class_member_lister import ClassMembers
from doc_quality.code_truth_resolver import CodeTruth
from doc_quality.config import EvaluatorConfig
from doc_quality.doc_views import DocView
from doc_quality.issue_types import IssueType
from doc_quality.models import Dimension, DimensionScore, Issue
from doc_quality.presence import is_present
from doc_quality.type_normalizer import is_builtin_type, tokenize

if TYPE_CHECKING:
    from mapcodoc_db.query import QueryManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hyperlink detection patterns
# ---------------------------------------------------------------------------
# The structured doc pipeline replaces ``url_placeholder_N`` tokens with
# real URLs. Three patterns are seen in practice:
#
#   1. ``token(https://...)``   - the dominant pattern in MapCoDoc output
#   2. ``[label](https://...)`` - markdown style
#   3. bare URLs                - rare but present
#
# We compile each separately so the downstream code can decide whether to
# keep the visible text or strip the link entirely.

_TOKEN_LINK_RE = re.compile(r"(\w[\w\.]*)\((https?://[^)\s]+)\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\w(])https?://\S+")
_PLACEHOLDER_RE = re.compile(r"url_placeholder_\d+")

# Sentence splitter. Keeping it consistent with readability_text.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Identifier-like tokens used to spot mentions of other DB members in
# prose. We consider dotted names (e.g. ``torch.Tensor``) as well as
# bare names (``Tensor``). The pattern intentionally avoids matching
# inside URLs by virtue of operating on hyperlink-stripped text.
_IDENTIFIER_TOKEN_RE = re.compile(
    r"(?<![\w./])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)"
    r"|(?<![\w./])([A-Z][A-Za-z0-9_]*)",  # CapWord likely class name
)


# ---------------------------------------------------------------------------
# Builtin reference map
# ---------------------------------------------------------------------------

_PY_DOCS = "https://docs.python.org/3"

BUILTIN_TYPE_URLS: Dict[str, str] = {
    "int":      f"{_PY_DOCS}/library/functions.html#int",
    "str":      f"{_PY_DOCS}/library/stdtypes.html#str",
    "bool":     f"{_PY_DOCS}/library/functions.html#bool",
    "float":    f"{_PY_DOCS}/library/functions.html#float",
    "list":     f"{_PY_DOCS}/library/stdtypes.html#list",
    "dict":     f"{_PY_DOCS}/library/stdtypes.html#dict",
    "tuple":    f"{_PY_DOCS}/library/stdtypes.html#tuple",
    "set":      f"{_PY_DOCS}/library/stdtypes.html#set",
    "frozenset":f"{_PY_DOCS}/library/stdtypes.html#frozenset",
    "bytes":    f"{_PY_DOCS}/library/stdtypes.html#bytes",
    "bytearray":f"{_PY_DOCS}/library/stdtypes.html#bytearray",
    "complex":  f"{_PY_DOCS}/library/functions.html#complex",
    "none":     f"{_PY_DOCS}/library/constants.html#None",
    "object":   f"{_PY_DOCS}/library/functions.html#object",
    "type":     f"{_PY_DOCS}/library/functions.html#type",
    "callable": f"{_PY_DOCS}/library/functions.html#callable",
    "iter":     f"{_PY_DOCS}/library/functions.html#iter",
}


def evaluate(
    view: DocView,
    code_truth: Optional[CodeTruth],
    class_members: Optional[ClassMembers],
    config: EvaluatorConfig,
    query_manager: Optional["QueryManager"] = None
) -> DimensionScore:
    """
    Run maintainability checks and return an aggregated DimensionScore.

    ``query_manager`` is optional because the IXRefC metric depends on a
    DB lookup. If not provided, IXRefC is skipped (set to 1.0) while the other metrics still run.
    """
    issues: List[Issue] = []
    breakdown: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # PHCheck - any unreplaced url_placeholder_N tokens?
    # ------------------------------------------------------------------
    # Run this first so reports surface upstream pipeline failures early.
    placeholder_issues = _check_placeholders(view)
    issues.extend(placeholder_issues)

    # ------------------------------------------------------------------
    # HD - hyperlink density across text sections
    # ------------------------------------------------------------------
    hd_score, hd_issues = _evaluate_hyperlink_density(view, config)
    issues.extend(hd_issues)
    breakdown["HD"] = hd_score

    # ------------------------------------------------------------------
    # TXRefC - parameter type cross-reference coverage
    # ------------------------------------------------------------------
    txref_score, txref_issues = _evaluate_type_xrefs(view)
    issues.extend(txref_issues)
    breakdown["TXRefC"] = txref_score

    # ------------------------------------------------------------------
    # BTRefC - builtin type reference coverage
    # ------------------------------------------------------------------
    btref_score, btref_issues = _evaluate_builtin_refs(view)
    issues.extend(btref_issues)
    breakdown["BTRefC"] = btref_score

    # ------------------------------------------------------------------
    # IXRefC - internal cross-reference coverage
    # ------------------------------------------------------------------
    if query_manager is not None:
        ixref_score, ixref_issues = _evaluate_internal_xrefs(view, query_manager)
        issues.extend(ixref_issues)
    else:
        ixref_score = 1.0
    breakdown["IXRefC"] = ixref_score

    # ------------------------------------------------------------------
    # ITR - inline type restatement
    # ------------------------------------------------------------------
    itr_score, itr_issues = _evaluate_inline_restatement(view)
    issues.extend(itr_issues)
    breakdown["ITR"] = itr_score

    # ------------------------------------------------------------------
    # Aggregate
    # ------------------------------------------------------------------
    score = _weighted_mean(breakdown, config.weights_maintainability)
    return DimensionScore(score=score, issues=issues, metric_breakdown=breakdown)


# ---------------------------------------------------------------------------
# Sub-evaluators
# ---------------------------------------------------------------------------

def _check_placeholders(view: DocView) -> List[Issue]:
    """
    Emit a HIGH-severity issue per surviving ``url_placeholder_N``.

    These indicate that the URL substitution step failed, leaving the
    consumer with an unusable cross-reference. The maintainer cannot
    repair this automatically; the issue is MANUAL by design.
    """
    issues: List[Issue] = []
    for label, text, json_path in view.iter_text_sections():
        if not is_present(text):
            continue
        if _PLACEHOLDER_RE.search(text):
            issues.append(Issue(
                issue_type=IssueType.MAINT_BROKEN_PLACEHOLDER,
                dimension=Dimension.MAINTAINABILITY,
                severity=IssueType.MAINT_BROKEN_PLACEHOLDER.value.default_severity,
                section=label,
                target=None,
                json_path=json_path,
                detail="Unreplaced 'url_placeholder_N' token detected.",
                doc_value=text,
                maintainer_strategy=IssueType.MAINT_BROKEN_PLACEHOLDER.value.default_strategy
            ))
    return issues


def _evaluate_hyperlink_density(view: DocView, config: EvaluatorConfig) -> Tuple[float, List[Issue]]:
    """Per-section HD score and issues for over/under linking."""
    issues: List[Issue] = []
    section_scores: List[float] = []

    for label, text, json_path in view.iter_text_sections():
        if not is_present(text):
            continue
        sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
        sent_count = max(len(sentences), 1)
        link_count = _count_links(text)
        density = link_count / sent_count

        # Has the section any candidate cross-reference targets at all?
        # If not (pure prose like "this is the default behaviour") then absence of links is fine and it's not penalized
        candidates = _find_xref_candidates(text)
        has_candidates = bool(candidates)

        if has_candidates and link_count == 0:
            issues.append(Issue(
                issue_type=IssueType.MAINT_NO_HYPERLINKS_IN_SECTION,
                dimension=Dimension.MAINTAINABILITY,
                severity=IssueType.MAINT_NO_HYPERLINKS_IN_SECTION.value.default_severity,
                section=label,
                target=None,
                json_path=json_path,
                detail=(f"Section '{label}' references identifiers but has no hyperlinks. Candidates: {sorted(list(candidates))[:6]}"),
                doc_value=text,
                metadata={"candidates": sorted(candidates)},
                maintainer_strategy=IssueType.MAINT_NO_HYPERLINKS_IN_SECTION.value.default_strategy
            ))
            section_scores.append(0.0)
            continue
        if density > config.hyperlink_density_max:
            issues.append(Issue(
                issue_type=IssueType.MAINT_OVER_LINKING,
                dimension=Dimension.MAINTAINABILITY,
                severity=IssueType.MAINT_OVER_LINKING.value.default_severity,
                section=label,
                target=None,
                json_path=json_path,
                detail=(f"Section '{label}' hyperlink density {density:.2f} exceeds upper bound {config.hyperlink_density_max}."),
                doc_value=text,
                metadata={"density": density},
                maintainer_strategy=IssueType.MAINT_OVER_LINKING.value.default_strategy
            ))
            section_scores.append(0.6)
            continue
        # Score: full credit when at least the lower bound is met OR the section has no cross-reference targets to link.
        if not has_candidates or density >= config.hyperlink_density_min:
            section_scores.append(1.0)
        else:
            # Has candidates but density too low. Linear in [0,1].
            section_scores.append(min(1.0, density / config.hyperlink_density_min))

    score = statistics.fmean(section_scores) if section_scores else 1.0
    return score, issues


def _evaluate_type_xrefs(view: DocView) -> Tuple[float, List[Issue]]:
    """
    Score: do parameter/return types hyperlink to canonical sources?

    A non-builtin type that's a single bare identifier (e.g. ``Tensor``)
    or a dotted path (e.g. ``torch.Tensor``) is a strong cross-reference
    candidate. Don't require a link as some types are too generic to
    link meaningfully, but score the per-type linking ratio.
    """
    issues: List[Issue] = []
    counts = [0, 0]  # [linked, total]

    # Parameters
    for p in view.get_parameters() or []:
        type_str = p.get("type")
        if not is_present(type_str):
            continue
        if is_builtin_type(type_str):
            continue
        counts[1] += 1
        if _has_link(type_str):
            counts[0] += 1
        else:
            pname = p.get("name") or "<unnamed>"
            issues.append(Issue(
                issue_type=IssueType.MAINT_TYPE_NOT_LINKED,
                dimension=Dimension.MAINTAINABILITY,
                severity=IssueType.MAINT_TYPE_NOT_LINKED.value.default_severity,
                section="parameters",
                target=pname,
                json_path=IssueType.MAINT_TYPE_NOT_LINKED.value.render_path(target=pname),
                detail=(f"Parameter '{pname}' type '{type_str}' is not hyperlinked but appears to reference a known entity."),
                doc_value=type_str,
                maintainer_strategy=IssueType.MAINT_TYPE_NOT_LINKED.value.default_strategy
            ))
    # Returns (callables only)
    ret = view.get_returns() or {}
    if ret:
        ret_type = ret.get("type")
        if is_present(ret_type) and not is_builtin_type(ret_type):
            counts[1] += 1
            if _has_link(ret_type):
                counts[0] += 1
            else:
                issues.append(Issue(
                    issue_type=IssueType.MAINT_TYPE_NOT_LINKED,
                    dimension=Dimension.MAINTAINABILITY,
                    severity=IssueType.MAINT_TYPE_NOT_LINKED.value.default_severity,
                    section="returns",
                    target=None,
                    json_path="$.returns.type",
                    detail=(f"Return type '{ret_type}' is not hyperlinked but appears to reference a known entity."),
                    doc_value=ret_type,
                    maintainer_strategy=IssueType.MAINT_TYPE_NOT_LINKED.value.default_strategy
                ))

    score = (counts[0] / counts[1]) if counts[1] else 1.0
    return score, issues


def _evaluate_builtin_refs(view: DocView) -> Tuple[float, List[Issue]]:
    """
    Score: do builtin-type mentions link to docs.python.org?

    The structured doc convention (visible in MapCoDoc's output for e.g.
    PyTorch) is that builtin types carry a parenthesized URL: ``bool(
    https://docs.python.org/...)``. Score linkage as a fraction.
    """
    issues: List[Issue] = []
    counts = [0, 0]  # [linked, total]

    for p in view.get_parameters() or []:
        type_str = p.get("type") or ""
        if not is_present(type_str):
            continue
        for builtin in _list_builtins_in_text(type_str):
            counts[1] += 1
            if _builtin_is_linked(type_str, builtin):
                counts[0] += 1
            else:
                pname = p.get("name") or "<unnamed>"
                issues.append(Issue(
                    issue_type=IssueType.MAINT_BUILTIN_NOT_LINKED,
                    dimension=Dimension.MAINTAINABILITY,
                    severity=IssueType.MAINT_BUILTIN_NOT_LINKED.value.default_severity,
                    section="parameters",
                    target=pname,
                    json_path=f"$.parameters[?name=='{pname}'].type",
                    detail=(f"Parameter '{pname}' type mentions builtin '{builtin}' without a hyperlink."),
                    code_value=BUILTIN_TYPE_URLS.get(builtin),
                    doc_value=type_str,
                    metadata={"builtin": builtin},
                    maintainer_strategy=IssueType.MAINT_BUILTIN_NOT_LINKED.value.default_strategy
                ))

    # Returns: same scan
    ret = view.get_returns() or {}
    if is_present(ret.get("type")):
        for builtin in _list_builtins_in_text(ret["type"]):
            counts[1] += 1
            if _builtin_is_linked(ret["type"], builtin):
                counts[0] += 1
            else:
                issues.append(Issue(
                    issue_type=IssueType.MAINT_BUILTIN_NOT_LINKED,
                    dimension=Dimension.MAINTAINABILITY,
                    severity=IssueType.MAINT_BUILTIN_NOT_LINKED.value.default_severity,
                    section="returns",
                    target=None,
                    json_path="$.returns.type",
                    detail=(f"Return type mentions builtin '{builtin}' without a hyperlink."),
                    code_value=BUILTIN_TYPE_URLS.get(builtin),
                    doc_value=ret["type"],
                    metadata={"builtin": builtin},
                    maintainer_strategy=IssueType.MAINT_BUILTIN_NOT_LINKED.value.default_strategy
                ))

    score = (counts[0] / counts[1]) if counts[1] else 1.0
    return score, issues


def _evaluate_internal_xrefs(view: DocView, qm: "QueryManager") -> Tuple[float, List[Issue]]:
    """
    Score: do prose mentions of known DB members hyperlink to them?

    Only looks at the description fields of parameters and the purpose
    section, since those are where library-internal cross-references most
    commonly belong. Class methods/attributes are handled by the shallow accuracy check, not here.
    """
    issues: List[Issue] = []
    counts = [0, 0]  # [linked, total]

    # Build (text, json_path, label) tuples to scan.
    sections: List[Tuple[str, str, str]] = []
    for p in view.get_parameters() or []:
        desc = p.get("description")
        if is_present(desc):
            pname = p.get("name") or "<unnamed>"
            sections.append((
                desc,
                f"$.parameters[?name=='{pname}'].description",
                f"parameters[{pname}].description",
            ))

    for label, text, json_path in view.iter_text_sections():
        # iter_text_sections already includes everything we want; collapse
        # duplicates by tracking by path.
        if (text, json_path, label) not in sections:
            sections.append((text, json_path, label))

    for text, json_path, label in sections:
        text_no_links = _strip_links(text)
        for token in _IDENTIFIER_TOKEN_RE.finditer(text_no_links):
            tok = token.group(1) or token.group(2)
            if not tok or tok.lower() in {"true", "false", "none", "self", "cls", "args", "kwargs"}:
                continue
            # Resolve via DB. ``find_member_by_any_path`` accepts FQNs, API names, and inherited names alike.
            try:
                resolved = qm.find_member_by_any_path(tok)
            except Exception as exc:
                logger.debug("DB lookup failed for token %r: %s", tok, exc)
                resolved = None
            if not resolved:
                continue
            counts[1] += 1
            # Is this mention hyperlinked?
            if _token_is_linked(text, tok):
                counts[0] += 1
            else:
                issues.append(Issue(
                    issue_type=IssueType.MAINT_INTERNAL_REFERENCE_NOT_LINKED,
                    dimension=Dimension.MAINTAINABILITY,
                    severity=IssueType.MAINT_INTERNAL_REFERENCE_NOT_LINKED.value.default_severity,
                    section=label,
                    target=tok,
                    json_path=json_path,
                    detail=(f"Mention of '{tok}' in section '{label}' is not hyperlinked but resolves to a known DB member."),
                    doc_value=text,
                    metadata={"resolved_type": resolved.get("type")},
                    maintainer_strategy=IssueType.MAINT_INTERNAL_REFERENCE_NOT_LINKED.value.default_strategy
                ))

    score = (counts[0] / counts[1]) if counts[1] else 1.0
    return score, issues


def _evaluate_inline_restatement(view: DocView) -> Tuple[float, List[Issue]]:
    """Score: parameter descriptions that merely paraphrase the type."""
    issues: List[Issue] = []
    counts = [0, 0]  # [clean, total]
    for p in view.get_parameters() or []:
        type_str = p.get("type")
        desc = p.get("description")
        if not is_present(type_str) or not is_present(desc):
            continue
        counts[1] += 1
        # If the description is dominated by a single sentence that rephrases the type and contains no link, count it as a restatement
        first_sentence = desc.strip().split(".")[0].lower()
        type_tokens = tokenize(type_str)
        # Trivial intersection: do the type's tokens dominate the first sentence? Threshold chosen empirically.
        first_tokens = set(re.findall(r"[a-z_]+", first_sentence))
        if not type_tokens or not first_tokens:
            counts[0] += 1
            continue
        overlap = len(type_tokens & first_tokens) / max(len(type_tokens), 1)
        if overlap >= 0.6 and not _has_link(desc):
            pname = p.get("name") or "<unnamed>"
            issues.append(Issue(
                issue_type=IssueType.MAINT_INLINE_TYPE_RESTATEMENT,
                dimension=Dimension.MAINTAINABILITY,
                severity=IssueType.MAINT_INLINE_TYPE_RESTATEMENT.value.default_severity,
                section="parameters",
                target=pname,
                json_path=IssueType.MAINT_INLINE_TYPE_RESTATEMENT.value.render_path(target=pname),
                detail=(f"Parameter '{pname}' description appears to restate the type without using a cross-reference."),
                doc_value=desc,
                metadata={"overlap": overlap, "type": type_str},
                maintainer_strategy=IssueType.MAINT_INLINE_TYPE_RESTATEMENT.value.default_strategy
            ))
        else:
            counts[0] += 1

    score = (counts[0] / counts[1]) if counts[1] else 1.0
    return score, issues


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_links(text: str) -> int:
    """Total hyperlinks in ``text`` regardless of style."""
    return (
        len(_TOKEN_LINK_RE.findall(text))
        + len(_MARKDOWN_LINK_RE.findall(text))
        + len(_BARE_URL_RE.findall(text))
    )


def _has_link(text: str) -> bool:
    return _count_links(text) > 0


def _strip_links(text: str) -> str:
    """Replace links with their visible text so we can scan for mentions."""
    s = _MARKDOWN_LINK_RE.sub(r"\1", text)
    s = _TOKEN_LINK_RE.sub(r"\1", s)
    s = _BARE_URL_RE.sub("", s)
    return s


def _find_xref_candidates(text: str) -> Set[str]:
    """Return tokens in ``text`` that *could* be cross-reference targets."""
    s = _strip_links(text)
    candidates: Set[str] = set()
    for m in _IDENTIFIER_TOKEN_RE.finditer(s):
        tok = m.group(1) or m.group(2)
        if tok:
            candidates.add(tok)
    return candidates


def _list_builtins_in_text(text: str) -> List[str]:
    """
    Return the canonical builtin names mentioned in ``text``.

    The function splits ``text`` into normalized tokens and intersects
    with the BUILTIN_TYPE_URLS keys. Each builtin is reported at most
    once per text input even if mentioned multiple times.
    """
    found = set()
    for m in re.finditer(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", text.lower()):
        tok = m.group(1)
        if tok in BUILTIN_TYPE_URLS:
            found.add(tok)
    return list(found)


def _builtin_is_linked(text: str, builtin: str) -> bool:
    """
    Is the specific builtin already linked?

    Looks for the ``builtin(http...)`` pattern with the builtin token as
    the visible label. Falls back to "any link present in text" if the structure is markdown.
    """
    pattern = re.compile(rf"\b{re.escape(builtin)}\s*\(\s*https?://", re.IGNORECASE)
    if pattern.search(text):
        return True
    # Markdown ``[builtin](url)``
    md = re.compile(rf"\[\s*{re.escape(builtin)}\s*\]\s*\(\s*https?://", re.IGNORECASE)
    return bool(md.search(text))


def _token_is_linked(text: str, token: str) -> bool:
    """Is ``token`` followed by a hyperlink in ``text``?"""
    pat = re.compile(rf"\b{re.escape(token)}\s*\(\s*https?://")
    return bool(pat.search(text))


def _weighted_mean(breakdown: Dict[str, float], weights: Dict[str, float]) -> float:
    """Plain weighted mean used for the dimension aggregate score."""
    active = {k: v for k, v in weights.items() if k in breakdown}
    if not active:
        return 1.0
    total = sum(active.values())
    if total <= 0:
        return 1.0
    return max(0.0, min(1.0, sum(breakdown[k] * w for k, w in active.items()) / total))
