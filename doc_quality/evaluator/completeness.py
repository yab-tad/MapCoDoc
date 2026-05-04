"""
Completeness dimension evaluator.

For each documented section, the completeness checker answers two questions:

1. Is the field populated with informational content (not absent, not "N/A")?
2. Is the populated content sufficient to be useful (e.g. a parameter description with at least ``min_desc_tokens`` tokens)?

The metrics emitted here are deterministic and rule-based; no LLM is
involved. The output is a ``DimensionScore`` whose ``issues`` list holds an
``Issue`` for every absent or trivial field, and whose ``score`` is the weighted mean of per-metric coverages.
"""

from __future__ import annotations

import re
from typing import List, Optional

from doc_quality.class_member_lister import ClassMembers
from doc_quality.code_truth_resolver import CodeTruth
from doc_quality.config import EvaluatorConfig
from doc_quality.doc_views import ClassDocView, DocView
from doc_quality.issue_types import IssueType
from doc_quality.models import DimensionScore, Dimension, Issue
from doc_quality.presence import is_present


# Splits text on whitespace; "tokens" here is the rough word count used to
# decide whether a description is informative. We deliberately don't use a
# linguistic tokenizer - the goal is a simple length proxy, not accurate
# segmentation.
_WS = re.compile(r"\s+")


def _token_count(text: Optional[str]) -> int:
    """Return the whitespace-token count of ``text``.

    Returns 0 for None, empty, or whitespace-only strings.
    """
    if not text:
        return 0
    text = text.strip()
    if not text:
        return 0
    return len([t for t in _WS.split(text) if t])


def evaluate(
    view: DocView,
    code_truth: Optional[CodeTruth],
    class_members: Optional[ClassMembers],
    config: EvaluatorConfig
) -> DimensionScore:
    """Run completeness checks and return an aggregated ``DimensionScore``.

    Args:
        view: The DocView wrapping the structured ``api_reference``.
        code_truth: Code-side ground truth (None if external inherited).
        class_members: Methods/attributes of the class for shallow class
            doc cross-reference. None for non-class members.
        config: Tunables and weights.

    Returns:
        A DimensionScore. ``score`` is in [0,1]. ``issues`` enumerates
        every individual completeness defect. ``metric_breakdown`` records
        per-metric subscores for reporting.
    """
    issues: List[Issue] = []
    breakdown: dict[str, float] = {}

    is_class = isinstance(view, ClassDocView)

    # ------------------------------------------------------------------
    # SigCov - signature presence
    # ------------------------------------------------------------------
    sig_cov = 1.0 if is_present(view.get_signature()) else 0.0
    if sig_cov == 0.0:
        issues.append(Issue(
            issue_type=IssueType.COMP_SIGNATURE_MISSING,
            dimension=Dimension.COMPLETENESS,
            severity=IssueType.COMP_SIGNATURE_MISSING.value.default_severity,
            section="module_member_signature",
            target=None,
            json_path=IssueType.COMP_SIGNATURE_MISSING.value.render_path(),
            detail="Signature is missing or 'N/A'.",
            doc_value=view.get_signature(),
            # Code-side ground truth is the 'full' signature variant, used
            # by the AST_DERIVED maintainer strategy.
            code_value=(code_truth.signatures.get("full")
                        if code_truth else None),
            maintainer_strategy=IssueType.COMP_SIGNATURE_MISSING.value.default_strategy
        ))
    breakdown["SigCov"] = sig_cov

    # ------------------------------------------------------------------
    # PurpCov - purpose description presence
    # ------------------------------------------------------------------
    purpose = view.get_purpose()
    purp_cov = 1.0 if is_present(purpose) else 0.0
    if purp_cov == 0.0:
        # Path differs between class (object-shaped description) and
        # callable (string-shaped). The IssueTypeSpec template is for the
        # class shape; we override at construction time for callables.
        path = ("$.module_member_description.purpose" if is_class
                else "$.module_member_description")
        issues.append(Issue(
            issue_type=IssueType.COMP_PURPOSE_MISSING,
            dimension=Dimension.COMPLETENESS,
            severity=IssueType.COMP_PURPOSE_MISSING.value.default_severity,
            section="module_member_description",
            target=None,
            json_path=path,
            detail="Purpose description is missing or 'N/A'.",
            doc_value=purpose,
            # No deterministic code-truth source for prose; LLM strategy.
            maintainer_strategy=IssueType.COMP_PURPOSE_MISSING.value.default_strategy
        ))
    breakdown["PurpCov"] = purp_cov

    # ------------------------------------------------------------------
    # PaC, PTC, PDC - parameter coverage / type / description
    # ------------------------------------------------------------------
    # These metrics are only meaningful when the code side has parameters to compare against. If no code truth (external inherited) we still
    # check intra-doc completeness (a parameter entry should at least have a name+type+description) but skip cross-coverage.
    doc_params = view.get_parameters() or []

    # Index documented parameters by name for O(1) lookup. ``"N/A"`` is treated as missing-name and excluded so we don't count nameless entries against coverage
    doc_param_by_name = {
        p.get("name"): p for p in doc_params
        if is_present(p.get("name"))
    }

    if code_truth and (code_truth.member_type in ("class", "function", "method")):
        public_code_params = code_truth.public_parameters
        public_code_names = [p.get("name") for p in public_code_params if p.get("name")]

        if public_code_names:
            # PaC - what fraction of code parameters appear in doc by name?
            matched = sum(1 for n in public_code_names if n in doc_param_by_name)
            pac = matched / len(public_code_names)
            # Emit COMP_PARAM_NAME_MISSING for each code param missing
            # from the doc. The maintainer can fill these in deterministically.
            for code_param in public_code_params:
                cname = code_param.get("name")
                if cname and cname not in doc_param_by_name:
                    issues.append(Issue(
                        issue_type=IssueType.COMP_PARAM_NAME_MISSING,
                        dimension=Dimension.COMPLETENESS,
                        severity=IssueType.COMP_PARAM_NAME_MISSING.value.default_severity,
                        section="parameters",
                        target=cname,
                        json_path="$.parameters",
                        detail=f"Parameter '{cname}' is missing from documentation.",
                        code_value=code_param,
                        doc_value=None,
                        maintainer_strategy=IssueType.COMP_PARAM_NAME_MISSING.value.default_strategy
                    ))
        else:
            # No public code parameters means "no params expected" - PaC is undefined; we treat it as 1.0 (no completeness deficit).
            pac = 1.0
    else:
        # Without code truth, fall back to a presence-only score: every doc-side entry that has a non-N/A name counts.
        pac = (
            len(doc_param_by_name) / max(len(doc_params), 1)
            if doc_params else 1.0
        )
    breakdown["PaC"] = pac

    # PTC - per-doc-param: does the entry have a non-N/A type?
    if doc_params:
        type_present = sum(1 for p in doc_params if is_present(p.get("type")))
        ptc = type_present / len(doc_params)
        for p in doc_params:
            pname = p.get("name") or "<unnamed>"
            if not is_present(p.get("type")):
                # Code-truth lookup so the maintainer can patch directly.
                code_value = None
                if code_truth and pname != "<unnamed>":
                    matching = [c for c in code_truth.public_parameters
                                if c.get("name") == pname]
                    if matching:
                        code_value = matching[0].get("type")
                issues.append(Issue(
                    issue_type=IssueType.COMP_PARAM_TYPE_MISSING,
                    dimension=Dimension.COMPLETENESS,
                    severity=IssueType.COMP_PARAM_TYPE_MISSING.value.default_severity,
                    section="parameters",
                    target=pname,
                    json_path=IssueType.COMP_PARAM_TYPE_MISSING.value.render_path(target=pname),
                    detail=f"Parameter '{pname}' has no documented type.",
                    code_value=code_value,
                    doc_value=p.get("type"),
                    maintainer_strategy=IssueType.COMP_PARAM_TYPE_MISSING.value.default_strategy
                ))
    else:
        ptc = 1.0
    breakdown["PTC"] = ptc

    # PDC - per-doc-param: description is present *and* informative.
    # "Informative" is operationalized as token count >= min_desc_tokens.
    if doc_params:
        desc_present = sum(
            1 for p in doc_params
            if is_present(p.get("description"))
            and _token_count(p.get("description")) >= config.min_desc_tokens
        )
        pdc = desc_present / len(doc_params)
        for p in doc_params:
            pname = p.get("name") or "<unnamed>"
            desc = p.get("description")
            if (not is_present(desc) or _token_count(desc) < config.min_desc_tokens):
                issues.append(Issue(
                    issue_type=IssueType.COMP_PARAM_DESCRIPTION_MISSING,
                    dimension=Dimension.COMPLETENESS,
                    severity=IssueType.COMP_PARAM_DESCRIPTION_MISSING.value.default_severity,
                    section="parameters",
                    target=pname,
                    json_path=IssueType.COMP_PARAM_DESCRIPTION_MISSING.value.render_path(target=pname),
                    detail=(f"Parameter '{pname}' description is missing or shorter than {config.min_desc_tokens} tokens."),
                    doc_value=desc,
                    metadata={"token_count": _token_count(desc)},
                    maintainer_strategy=IssueType.COMP_PARAM_DESCRIPTION_MISSING.value.default_strategy
                ))
    else:
        pdc = 1.0
    breakdown["PDC"] = pdc

    # ------------------------------------------------------------------
    # RC - return coverage (callables only)
    # ------------------------------------------------------------------
    # Classes never have a top-level returns section. For callables, the check is conditional on the code-side return type being non-void: functions that return None don't *need* a return-type annotation
    if is_class:
        rc = 1.0
    else:
        ret = view.get_returns() or {}
        ret_type_present = is_present(ret.get("type"))
        ret_desc_present = is_present(ret.get("description"))
        # If we have code truth, only require docs when code returns something. Without code truth, require docs unconditionally.
        code_returns_value = (
            (code_truth.returns.get("type") if code_truth and code_truth.returns else None)
            if code_truth else None
        )
        return_required = (
            (not code_truth)
            or (code_truth and not code_truth.is_void_callable)
        )
        if return_required:
            # Type
            if not ret_type_present:
                issues.append(Issue(
                    issue_type=IssueType.COMP_RETURN_TYPE_MISSING,
                    dimension=Dimension.COMPLETENESS,
                    severity=IssueType.COMP_RETURN_TYPE_MISSING.value.default_severity,
                    section="returns",
                    target=None,
                    json_path="$.returns.type",
                    detail="Return type is missing or 'N/A' for a returning callable.",
                    code_value=code_returns_value,
                    doc_value=ret.get("type"),
                    maintainer_strategy=IssueType.COMP_RETURN_TYPE_MISSING.value.default_strategy
                ))
            # Description
            if not ret_desc_present:
                issues.append(Issue(
                    issue_type=IssueType.COMP_RETURN_DESCRIPTION_MISSING,
                    dimension=Dimension.COMPLETENESS,
                    severity=IssueType.COMP_RETURN_DESCRIPTION_MISSING.value.default_severity,
                    section="returns",
                    target=None,
                    json_path="$.returns.description",
                    detail="Return value description is missing or 'N/A'.",
                    doc_value=ret.get("description"),
                    maintainer_strategy=IssueType.COMP_RETURN_DESCRIPTION_MISSING.value.default_strategy
                ))
            # Score is the average of the two binary checks.
            rc = (float(ret_type_present) + float(ret_desc_present)) / 2.0
        else:
            # Void callable: no return doc required; score full.
            rc = 1.0
    breakdown["RC"] = rc

    # ------------------------------------------------------------------
    # ExC - example coverage
    # ------------------------------------------------------------------
    examples = view.get_examples()
    # Count an example as "real" only if its code body is present.
    real_examples = [e for e in examples if is_present(e.get("example"))]
    exc = 1.0 if real_examples else 0.0
    if not real_examples and config.require_examples:
        issues.append(Issue(
            issue_type=IssueType.COMP_NO_EXAMPLES,
            dimension=Dimension.COMPLETENESS,
            severity=IssueType.COMP_NO_EXAMPLES.value.default_severity,
            section="examples",
            target=None,
            json_path="$.examples",
            detail="No usage examples are provided.",
            doc_value=examples,
            maintainer_strategy=IssueType.COMP_NO_EXAMPLES.value.default_strategy
        ))
    # Empty example slots
    for idx, ex in enumerate(examples):
        if not is_present(ex.get("example")):
            issues.append(Issue(
                issue_type=IssueType.COMP_EXAMPLE_EMPTY,
                dimension=Dimension.COMPLETENESS,
                severity=IssueType.COMP_EXAMPLE_EMPTY.value.default_severity,
                section="examples",
                target=str(idx),
                json_path=f"$.examples[{idx}].example",
                detail=f"Example #{idx} content is empty or 'N/A'.",
                doc_value=ex.get("example"),
                maintainer_strategy=IssueType.COMP_EXAMPLE_EMPTY.value.default_strategy
            ))
    breakdown["ExC"] = exc

    # ------------------------------------------------------------------
    # Class shallow checks: ClsMethCov, ClsAttrCov
    # These are reported as issues but excluded from the headline score because deep evaluation is performed on the standalone docs
    # ------------------------------------------------------------------
    if is_class:
        for m in view.get_methods():
            mname = m.get("name") or "<unnamed>"
            if not is_present(m.get("description")):
                issues.append(Issue(
                    issue_type=IssueType.COMP_CLASS_METHOD_DESC_MISSING,
                    dimension=Dimension.COMPLETENESS,
                    severity=IssueType.COMP_CLASS_METHOD_DESC_MISSING.value.default_severity,
                    section="methods",
                    target=mname,
                    json_path=IssueType.COMP_CLASS_METHOD_DESC_MISSING.value.render_path(target=mname),
                    detail=f"Method '{mname}' (listed under class) has no description.",
                    doc_value=m.get("description"),
                    maintainer_strategy=IssueType.COMP_CLASS_METHOD_DESC_MISSING.value.default_strategy
                ))
        for a in view.get_attributes():
            aname = a.get("identifier") or "<unnamed>"
            if not is_present(a.get("description")):
                issues.append(Issue(
                    issue_type=IssueType.COMP_CLASS_ATTR_DESC_MISSING,
                    dimension=Dimension.COMPLETENESS,
                    severity=IssueType.COMP_CLASS_ATTR_DESC_MISSING.value.default_severity,
                    section="attributes",
                    target=aname,
                    json_path=IssueType.COMP_CLASS_ATTR_DESC_MISSING.value.render_path(target=aname),
                    detail=f"Attribute '{aname}' (listed under class) has no description.",
                    doc_value=a.get("description"),
                    maintainer_strategy=IssueType.COMP_CLASS_ATTR_DESC_MISSING.value.default_strategy
                ))

    # ------------------------------------------------------------------
    # Aggregate
    # ------------------------------------------------------------------
    score = _weighted_mean(breakdown, config.weights_completeness, is_class)
    return DimensionScore(score=score, issues=issues, metric_breakdown=breakdown)


def _weighted_mean(breakdown: dict, weights: dict, is_class: bool) -> float:
    """
    Compute weighted mean while handling the class-specific RC redistribution.

    The default weight table includes ``RC`` (return coverage). For classes
    that metric is forced to 1.0 and contributes no signal; we redistribute
    its weight evenly across the other present metrics so the resulting
    score is on the same [0,1] scale as a callable's.
    """
    
    # Filter to keys that are actually present in the breakdown (defensive against a metric being added to weights but not yet computed).
    active = {k: v for k, v in weights.items() if k in breakdown}
    if not active:
        # Should never happen given the default weight table, but guard for malformed configs.
        return 0.0

    if is_class and "RC" in active:
        # Pull RC's weight and redistribute.
        rc_weight = active.pop("RC")
        share = rc_weight / max(len(active), 1)
        for k in active:
            active[k] += share

    total_weight = sum(active.values())
    if total_weight <= 0:
        return 0.0
    weighted_sum = sum(breakdown[k] * w for k, w in active.items())
    return max(0.0, min(1.0, weighted_sum / total_weight))
