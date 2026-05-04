"""
Accuracy dimension evaluator.

Compares the structured documentation against the code-side ground truth
captured by ``CodeTruthResolver``. The checks are deliberately rule-based:
they detect mismatches that are reliably distinguishable without a
generative model. Semantic checks (does the description describe the code's
actual behaviour?) are deferred to v1.x with an LLM strategy.

The metrics implemented here:

* PCA  - parameter count agreement
* PNA  - parameter name agreement (set-equality of names)
* PTC_acc - parameter type consistency
* PDA  - default-value agreement
* RTC  - return-type consistency
* SigDrift - documented signature vs. code signature similarity
* AsyncMarker - ``async`` keyword presence in signature aligns with code
* DeprecatedMarker - ``@deprecated`` decorator surfaced in doc
* Class shallow cross-references - methods/attributes belong to class
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Dict, List, Optional

from doc_quality.class_member_lister import ClassMembers
from doc_quality.code_truth_resolver import CodeTruth
from doc_quality.config import EvaluatorConfig
from doc_quality.doc_views import ClassDocView, DocView
from doc_quality.issue_types import IssueType
from doc_quality.models import Dimension, DimensionScore, Issue, MaintainerStrategy
from doc_quality.presence import is_present
from doc_quality.type_normalizer import normalize, types_equivalent


logger = logging.getLogger(__name__)


# Pattern used to scan a parameter description for a "Default: X" hint
# Tolerates "default: X", "(default X)", "Default is X". The captured value is one of three alternative groups: single-quoted, double-quoted, or unquoted (a single non-whitespace token)
# Splitting into alternatives avoids the non-greedy-tail problem where ``([^,;\n)]+?)`` would match only the opening quote
_DEFAULT_HINT_RE = re.compile(
    r"default(?:\s*[:=]\s*|\s+is\s+|\s+\()\s*"
    r"(?:'([^']+)'|\"([^\"]+)\"|([^\s,;\n)]+))",
    re.IGNORECASE
)

# URL stripper used before comparing signatures: documented signatures often embed URLs ("torch.nn.L1Loss(...)[source](...)¶(...)") that have no code counterpart
# Strip them before edit-distance comparison
_SIG_URL_RE = re.compile(r"\(?https?://\S+\)?")
_SIG_BRACKET_RE = re.compile(r"\[[^\]]*\]")  # ``[source]``-style brackets
_SIG_NONWORD_RE = re.compile(r"[^A-Za-z0-9_,()=\s\.\*\[\]]")


def evaluate(
    view: DocView,
    code_truth: Optional[CodeTruth],
    class_members: Optional[ClassMembers],
    config: EvaluatorConfig
) -> DimensionScore:
    """Run accuracy checks against ``code_truth``.

    When ``code_truth`` is None (external inherited member) every metric
    that requires comparison is skipped; the resulting DimensionScore has
    score 1.0 and an empty issue list, signalling "nothing to disagree
    with". The orchestrator marks the report ``code_truth_available=False``
    so this score is interpretable.
    """
    issues: List[Issue] = []
    breakdown: Dict[str, float] = {}

    if code_truth is None:
        # Class-shallow checks still run if ``class_members`` is available
        # (we know what the class contains even without per-member code
        # truth in the standard sense). Otherwise return a perfect score.
        if isinstance(view, ClassDocView) and class_members is not None:
            issues.extend(_class_shallow_xref(view, class_members))
        return DimensionScore(score=1.0, issues=issues, metric_breakdown={})

    is_class = isinstance(view, ClassDocView)
    doc_params = view.get_parameters() or []

    # Build name-indexed lookups on both sides for efficient comparisons.
    code_param_by_name = {p.get("name"): p for p in code_truth.public_parameters if p.get("name")}
    doc_param_by_name = {p.get("name"): p for p in doc_params if is_present(p.get("name"))}

    # ------------------------------------------------------------------
    # PCA - parameter count agreement
    # ------------------------------------------------------------------
    code_n = len(code_param_by_name)
    doc_n = len(doc_param_by_name)
    # Score PCA as 1.0 only if counts match exactly. A symmetric tolerance would let big drift score well; better to flag and let the maintainer decide.
    pca = 1.0 if code_n == doc_n else 0.0
    breakdown["PCA"] = pca

    # ------------------------------------------------------------------
    # PNA - parameter name agreement (set-level)
    # ------------------------------------------------------------------
    code_names = set(code_param_by_name.keys())
    doc_names = set(doc_param_by_name.keys())

    # Doc lists a name not in code: ACC_PARAM_NAME_UNKNOWN
    extras_in_doc = doc_names - code_names
    # Code lists a name not in doc: ACC_PARAM_MISSING_FROM_DOC. This is also caught by completeness COMP_PARAM_NAME_MISSING but both are emitted since they have different strategies (DB_QUERY for missing, DB_QUERY for unknown removal) and severities
    missing_in_doc = code_names - doc_names

    if code_names or doc_names:
        # PNA is the symmetric set-agreement: |intersection| / |union|.
        # Range [0,1]; both empty trivially yields 1.0.
        union = code_names | doc_names
        pna = (len(code_names & doc_names) / len(union)) if union else 1.0
    else:
        pna = 1.0

    for extra in extras_in_doc:
        issues.append(Issue(
            issue_type=IssueType.ACC_PARAM_NAME_UNKNOWN,
            dimension=Dimension.ACCURACY,
            severity=IssueType.ACC_PARAM_NAME_UNKNOWN.value.default_severity,
            section="parameters",
            target=extra,
            json_path=IssueType.ACC_PARAM_NAME_UNKNOWN.value.render_path(target=extra),
            detail=f"Documented parameter '{extra}' is not in the code signature.",
            doc_value=doc_param_by_name[extra],
            code_value=None,
            maintainer_strategy=IssueType.ACC_PARAM_NAME_UNKNOWN.value.default_strategy
        ))
    for missing in missing_in_doc:
        issues.append(Issue(
            issue_type=IssueType.ACC_PARAM_MISSING_FROM_DOC,
            dimension=Dimension.ACCURACY,
            severity=IssueType.ACC_PARAM_MISSING_FROM_DOC.value.default_severity,
            section="parameters",
            target=missing,
            json_path="$.parameters",
            detail=f"Code parameter '{missing}' is missing from the documentation.",
            code_value=code_param_by_name[missing],
            doc_value=None,
            maintainer_strategy=IssueType.ACC_PARAM_MISSING_FROM_DOC.value.default_strategy
        ))
    breakdown["PNA"] = pna

    # ------------------------------------------------------------------
    # PTC_acc - parameter type consistency for name-matched params
    # ------------------------------------------------------------------
    matched_names = code_names & doc_names
    if matched_names:
        type_agreements = 0
        for name in matched_names:
            code_type = code_param_by_name[name].get("type")
            doc_type = doc_param_by_name[name].get("type")
            # Skip if both sides are absent
            if not is_present(code_type) and not is_present(doc_type):
                type_agreements += 1
                continue
            if types_equivalent(code_type, doc_type, config.type_fuzzy_threshold):
                type_agreements += 1
            else:
                issues.append(Issue(
                    issue_type=IssueType.ACC_PARAM_TYPE_MISMATCH,
                    dimension=Dimension.ACCURACY,
                    severity=IssueType.ACC_PARAM_TYPE_MISMATCH.value.default_severity,
                    section="parameters",
                    target=name,
                    json_path=IssueType.ACC_PARAM_TYPE_MISMATCH.value.render_path(target=name),
                    detail=(f"Parameter '{name}' type mismatch: code={code_type!r}, doc={doc_type!r}."),
                    code_value=code_type,
                    doc_value=doc_type,
                    metadata={
                        "normalized_code": normalize(code_type),
                        "normalized_doc": normalize(doc_type)
                    },
                    maintainer_strategy=IssueType.ACC_PARAM_TYPE_MISMATCH.value.default_strategy
                ))
        ptc_acc = type_agreements / len(matched_names)
    else:
        ptc_acc = 1.0
    breakdown["PTC_acc"] = ptc_acc

    # ------------------------------------------------------------------
    # PDA - default value agreement
    # ------------------------------------------------------------------
    # The structured doc carries default information narratively in the description ("Default: True")
    # Extract candidate defaults via a tolerant regex and compare against the code-side default string
    if matched_names:
        agreements = 0
        comparable = 0
        for name in matched_names:
            code_default = code_param_by_name[name].get("default")
            if code_default is None:
                # Code parameter has no default; nothing to compare. Don't penalize doc for not mentioning a non-existent default
                continue
            comparable += 1
            doc_desc = doc_param_by_name[name].get("description") or ""
            doc_default = _extract_doc_default(doc_desc)
            if doc_default is not None and _defaults_match(code_default, doc_default):
                agreements += 1
            elif doc_default is not None and not _defaults_match(code_default, doc_default):
                issues.append(Issue(
                    issue_type=IssueType.ACC_PARAM_DEFAULT_MISMATCH,
                    dimension=Dimension.ACCURACY,
                    severity=IssueType.ACC_PARAM_DEFAULT_MISMATCH.value.default_severity,
                    section="parameters",
                    target=name,
                    json_path=IssueType.ACC_PARAM_DEFAULT_MISMATCH.value.render_path(target=name),
                    detail=(f"Parameter '{name}' default mismatch: code={code_default!r}, doc says {doc_default!r}."),
                    code_value=code_default,
                    doc_value=doc_default,
                    maintainer_strategy=IssueType.ACC_PARAM_DEFAULT_MISMATCH.value.default_strategy
                ))
            # else: doc didn't mention a default at all. We don't flag
            # that here as accuracy - it's a completeness/maintainability
            # concern. PDA scoring excludes these cases.
        pda = (agreements / comparable) if comparable else 1.0
    else:
        pda = 1.0
    breakdown["PDA"] = pda

    # ------------------------------------------------------------------
    # RTC - return-type consistency (callables only)
    # ------------------------------------------------------------------
    if not is_class:
        ret_doc = view.get_returns() or {}
        code_ret_type = (code_truth.returns or {}).get("type")
        doc_ret_type = ret_doc.get("type")
        if is_present(code_ret_type) or is_present(doc_ret_type):
            if types_equivalent(code_ret_type, doc_ret_type, config.type_fuzzy_threshold):
                rtc = 1.0
            else:
                rtc = 0.0
                issues.append(Issue(
                    issue_type=IssueType.ACC_RETURN_TYPE_MISMATCH,
                    dimension=Dimension.ACCURACY,
                    severity=IssueType.ACC_RETURN_TYPE_MISMATCH.value.default_severity,
                    section="returns",
                    target=None,
                    json_path="$.returns.type",
                    detail=(f"Return type mismatch: code={code_ret_type!r}, doc={doc_ret_type!r}."),
                    code_value=code_ret_type,
                    doc_value=doc_ret_type,
                    maintainer_strategy=IssueType.ACC_RETURN_TYPE_MISMATCH.value.default_strategy
                ))
        else:
            rtc = 1.0
    else:
        rtc = 1.0
    breakdown["RTC"] = rtc

    # ------------------------------------------------------------------
    # SigDrift - documented signature vs code signature
    # ------------------------------------------------------------------
    sig_drift = _signature_drift_score(view.get_signature(), code_truth)
    if sig_drift is not None and sig_drift >= config.sig_drift_threshold:
        issues.append(Issue(
            issue_type=IssueType.ACC_SIGNATURE_DRIFT,
            dimension=Dimension.ACCURACY,
            severity=IssueType.ACC_SIGNATURE_DRIFT.value.default_severity,
            section="module_member_signature",
            target=None,
            json_path="$.module_member_signature",
            detail=(f"Documented signature differs from code signature (normalized edit distance ≈ {sig_drift:.2f})."),
            code_value=code_truth.signatures.get("full"),
            doc_value=view.get_signature(),
            metadata={"drift": sig_drift,
                      "threshold": config.sig_drift_threshold},
            maintainer_strategy=IssueType.ACC_SIGNATURE_DRIFT.value.default_strategy
        ))
    # Convert drift to a [0,1] *agreement* score for aggregation.
    breakdown["SigDrift"] = (
        1.0 if sig_drift is None else max(0.0, 1.0 - sig_drift)
    )

    # ------------------------------------------------------------------
    # AsyncMarker
    # ------------------------------------------------------------------
    sig_text = (view.get_signature() or "").lower()
    if code_truth.is_async and "async" not in sig_text:
        issues.append(Issue(
            issue_type=IssueType.ACC_ASYNC_MARKER_MISSING,
            dimension=Dimension.ACCURACY,
            severity=IssueType.ACC_ASYNC_MARKER_MISSING.value.default_severity,
            section="module_member_signature",
            target=None,
            json_path="$.module_member_signature",
            detail="Code member is async but the signature lacks 'async'.",
            code_value=code_truth.signatures.get("full"),
            doc_value=view.get_signature(),
            maintainer_strategy=IssueType.ACC_ASYNC_MARKER_MISSING.value.default_strategy
        ))
        async_score = 0.0
    else:
        async_score = 1.0
    breakdown["AsyncMarker"] = async_score

    # ------------------------------------------------------------------
    # DeprecatedMarker
    # ------------------------------------------------------------------
    if code_truth.is_deprecated:
        # Search the doc text broadly for any mention of deprecation.
        haystack_parts = [
            view.get_purpose() or "",
            *(view.get_purpose_additional_info() or []),
        ]
        notes = view.get_additional_notes() or {}
        haystack_parts.extend(notes.get("supplementary_information") or [])
        haystack_parts.extend(notes.get("edge_cases") or [])
        haystack = " ".join(str(p) for p in haystack_parts).lower()
        if "deprecated" in haystack or "deprecation" in haystack:
            dep_score = 1.0
        else:
            dep_score = 0.0
            issues.append(Issue(
                issue_type=IssueType.ACC_DEPRECATED_NOT_NOTED,
                dimension=Dimension.ACCURACY,
                severity=IssueType.ACC_DEPRECATED_NOT_NOTED.value.default_severity,
                section="additional_notes",
                target=None,
                json_path="$.additional_notes.supplementary_information",
                detail=("Member is decorated @deprecated but the doc does not "
                        "mention deprecation."),
                code_value=code_truth.decorators,
                doc_value=None,
                maintainer_strategy=IssueType.ACC_DEPRECATED_NOT_NOTED.value.default_strategy
            ))
    else:
        dep_score = 1.0
    breakdown["DeprecatedMarker"] = dep_score

    # ------------------------------------------------------------------
    # Class-shallow cross-references
    # ------------------------------------------------------------------
    if is_class and class_members is not None:
        issues.extend(_class_shallow_xref(view, class_members))

    # ------------------------------------------------------------------
    # Aggregate
    # ------------------------------------------------------------------
    score = _weighted_mean(breakdown, config.weights_accuracy)
    return DimensionScore(score=score, issues=issues, metric_breakdown=breakdown)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_doc_default(description: str) -> Optional[str]:
    """
    Pull a documented default value out of a parameter description.

    Returns the unquoted value when the regex finds a quoted form, or the
    raw token otherwise. Returns None when no Default clause is found.
    """
    if not description:
        return None
    m = _DEFAULT_HINT_RE.search(description)
    if not m:
        return None
    # Exactly one of the three alternative groups matches (the rest are
    # None). Pick the first non-None group to be the captured value.
    candidate = m.group(1) or m.group(2) or m.group(3) or ""
    candidate = candidate.strip(" .,;'\"")
    return candidate or None


def _defaults_match(code_default: str, doc_default: str) -> bool:
    """
    Compare two default-value strings tolerantly.

    Accepts equivalences such as ``True`` vs ``true``, ``'mean'`` vs ``mean``, ``None`` vs ``none``, and shrugs at numeric formatting differences (``1.0`` vs ``1``).
    """
    if code_default is None or doc_default is None:
        return False
    a = (code_default or "").strip().strip("'\"").lower()
    b = (doc_default or "").strip().strip("'\"").lower()
    if a == b:
        return True
    # Try numeric equivalence so ``1`` and ``1.0`` agree.
    try:
        return float(a) == float(b)
    except ValueError:
        return False


def _signature_drift_score(doc_signature: Optional[str], code_truth: CodeTruth) -> Optional[float]:
    """
    Return a [0,1] drift score; None when comparison is impossible.

    The score is ``1 - difflib.SequenceMatcher.ratio()`` between the
    normalized doc signature and the closest available code signature
    variant. Tries the ``no_types`` variant first because it usually
    matches the doc signature shape; fall back to ``full``.
    """
    if not is_present(doc_signature):
        return None
    if not code_truth.signatures:
        return None

    # Try the variants in order of preference - no_types is usually the
    # closest match to a doc signature, which often elides annotations.
    candidates: list[str] = []
    for key in ("no_types", "default", "full"):
        if code_truth.signatures.get(key):
            candidates.append(code_truth.signatures[key])
    if not candidates:
        # Fall back to whatever variant available
        candidates = list(code_truth.signatures.values())

    norm_doc = _normalize_signature(doc_signature)
    best = 1.0  # worst-case drift; minimize it
    for cand in candidates:
        norm_code = _normalize_signature(cand)
        if not norm_code:
            continue
        # ratio() returns similarity in [0,1]; we want drift = 1 - sim.
        sim = difflib.SequenceMatcher(None, norm_doc, norm_code).ratio()
        drift = 1.0 - sim
        if drift < best:
            best = drift
    return best


def _normalize_signature(sig: str) -> str:
    """Strip URLs, brackets, and unusual characters before comparing."""
    s = sig or ""
    s = _SIG_URL_RE.sub("", s)
    s = _SIG_BRACKET_RE.sub("", s)
    s = _SIG_NONWORD_RE.sub("", s)
    # Collapse whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower()


def _class_shallow_xref(view: DocView, class_members: ClassMembers) -> List[Issue]:
    """
    Run the shallow cross-reference checks for class methods/attrs.

    Three sub-checks per documented method, two per attribute, exactly as specified in the design.
    """
    issues: List[Issue] = []

    for m in view.get_methods():
        mname = m.get("name")
        if not is_present(mname):
            # Without a name there is nothing to cross-reference; the missing-name issue is handled by the maintainability evaluator (or could be raised here as completeness).
            continue
        if not class_members.has_method(mname):
            issues.append(Issue(
                issue_type=IssueType.ACC_CLASS_METHOD_NOT_FOUND,
                dimension=Dimension.ACCURACY,
                severity=IssueType.ACC_CLASS_METHOD_NOT_FOUND.value.default_severity,
                section="methods",
                target=mname,
                json_path=IssueType.ACC_CLASS_METHOD_NOT_FOUND.value.render_path(target=mname),
                detail=(f"Method '{mname}' listed in class doc was not found among the class's actual methods."),
                doc_value=m,
                # MEDIUM severity reflects that lister coverage may be incomplete; manual review is appropriate.
                maintainer_strategy=MaintainerStrategy.MANUAL
            ))
            continue
        # Method exists; do a light signature drift check.
        actual_method = class_members.method_names[mname]
        doc_method_sig = m.get("signature")
        if is_present(doc_method_sig) and actual_method.signatures:
            # Compare against the actual method's full signature.
            sigs = actual_method.signatures
            best_drift = None
            for key in ("no_types", "default", "full"):
                if key in sigs:
                    sim = difflib.SequenceMatcher(
                        None,
                        _normalize_signature(doc_method_sig),
                        _normalize_signature(sigs[key]),
                    ).ratio()
                    drift = 1.0 - sim
                    if best_drift is None or drift < best_drift:
                        best_drift = drift
            if best_drift is not None and best_drift >= 0.4:
                issues.append(Issue(
                    issue_type=IssueType.ACC_CLASS_METHOD_SIG_DRIFT,
                    dimension=Dimension.ACCURACY,
                    severity=IssueType.ACC_CLASS_METHOD_SIG_DRIFT.value.default_severity,
                    section="methods",
                    target=mname,
                    json_path=IssueType.ACC_CLASS_METHOD_SIG_DRIFT.value.render_path(target=mname),
                    detail=(f"Method '{mname}' documented signature differs from code signature."),
                    code_value=sigs.get("full"),
                    doc_value=doc_method_sig,
                    metadata={"drift": best_drift},
                    maintainer_strategy=IssueType.ACC_CLASS_METHOD_SIG_DRIFT.value.default_strategy
                ))

    for a in view.get_attributes():
        aident = a.get("identifier")
        if not is_present(aident):
            continue
        # The identifier may include trailing parens, decorations, or URL
        # placeholders. Extract the bare name for lookup.
        bare = _bare_attribute_name(aident)
        if not class_members.has_attribute(bare):
            issues.append(Issue(
                issue_type=IssueType.ACC_CLASS_ATTR_NOT_FOUND,
                dimension=Dimension.ACCURACY,
                severity=IssueType.ACC_CLASS_ATTR_NOT_FOUND.value.default_severity,
                section="attributes",
                target=aident,
                json_path=IssueType.ACC_CLASS_ATTR_NOT_FOUND.value.render_path(target=aident),
                detail=(f"Attribute '{aident}' listed in class doc was not found among the class's actual attributes."),
                doc_value=a,
                metadata={"resolved_name": bare},
                maintainer_strategy=MaintainerStrategy.MANUAL
            ))
    return issues


def _bare_attribute_name(identifier: str) -> str:
    """
    Strip parens, URL fragments, and whitespace from an identifier.

    The structured-doc ``attributes[].identifier`` field preserves the
    surrounding context of the attribute name as it appeared in the
    scraped docs (e.g. ``"shape (https://...)"``). For cross-reference we want just the symbol name.
    """
    s = identifier.strip()
    # Drop parenthesized content (``shape (https://...)`` -> ``shape``).
    s = re.sub(r"\s*\([^)]*\)", "", s)
    # First whitespace-separated token only.
    s = s.split()[0] if s.split() else s
    return s


def _weighted_mean(breakdown: Dict[str, float], weights: Dict[str, float]) -> float:
    """Plain weighted mean used for the dimension aggregate score."""
    active = {k: v for k, v in weights.items() if k in breakdown}
    if not active:
        return 0.0
    total = sum(active.values())
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, sum(breakdown[k] * w for k, w in active.items()) / total))
