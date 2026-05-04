"""
Resolve "code truth" for a documented member.

Most accuracy and completeness checks need the canonical, code-side view of
a member: its parameters (with names, types, defaults), its return type, its
signature variants, its decorators, its source code, and its docstring.

This information lives in different places depending on the kind of member:

* For a direct member (``DBMember``) the data is on the row itself.
* For an internal inherited member (``DBInheritedMember`` whose
  ``original_member_id`` is set) the data lives on the *original* member's
  ``DBMember`` row, found via the original FQN.
* For an external inherited member (``is_external == True`` and no
  ``original_member_id``) only the recorded signature is available; the
  source code, docstring, and full parameter info are all unknown. The
  resolver returns ``None`` in this case so the evaluator knows to skip
  the member.

The resolver returns a uniform ``CodeTruth`` value object that is the input
to every accuracy/completeness check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from mapcodoc_db.query import (
        InheritedMemberDetails,
        MemberDetails,
        QueryManager,
    )

logger = logging.getLogger(__name__)


@dataclass
class CodeTruth:
    """The unified code-side view of a member used by the evaluator.

    Fields mirror the most-used parts of ``MemberDetails`` plus a few
    derived fields that simplify accuracy checks.
    """

    fqn: str
    api_name: str
    member_type: str

    # Parameter list as stored in the DB. Each entry is a dict shaped
    # like ``{"name": str, "type": Optional[str], "default": Optional[str],
    # "is_positional_only": bool, "is_keyword_only": bool, "is_vararg":
    # bool, "is_kwarg": bool}``. Per the schema ``self`` and ``cls`` are
    # *not* stripped here - the consumer decides whether to ignore them.
    parameters: List[Dict] = field(default_factory=list)

    # Return-type info. Populated only for callables.
    returns: Optional[Dict] = None

    # Mapping ``variant -> signature_text`` from the DBSignature table.
    # The ``"full"`` variant contains everything; ``"no_types"`` is the
    # most useful for textual comparison against doc signatures.
    signatures: Dict[str, str] = field(default_factory=dict)

    decorators: List[str] = field(default_factory=list)
    is_async: bool = False
    is_static: bool = False
    is_abstract: bool = False
    is_property: bool = False
    is_inherited: bool = False

    source_code: Optional[str] = None
    docstring: Optional[str] = None

    # ---- Convenience views -------------------------------------------

    @property
    def parameter_names(self) -> List[str]:
        """Names of parameters in declaration order, *including* ``self``/``cls``."""
        return [p.get("name", "") for p in self.parameters if p.get("name")]

    @property
    def public_parameter_names(self) -> List[str]:
        """Parameter names with ``self`` and ``cls`` filtered out.

        This is what the documentation-side parameter list should match.
        """
        # The structured doc list documents *callable inputs*. ``self`` and
        # ``cls`` are implicit context, not inputs, so they are excluded.
        return [n for n in self.parameter_names if n not in ("self", "cls")]

    @property
    def public_parameters(self) -> List[Dict]:
        """Full parameter dicts with ``self``/``cls`` excluded."""
        return [p for p in self.parameters
                if p.get("name") not in ("self", "cls")]

    @property
    def is_void_callable(self) -> bool:
        """True if this is a callable with no return type annotation.

        Used by completeness checks to decide whether ``returns``
        documentation is required.
        """
        if self.member_type not in ("function", "method"):
            return False
        # ``returns`` may be ``None``, ``{}``, or a dict whose ``type`` is
        # missing/None - all of these mean "no annotated return type".
        if not self.returns:
            return True
        return not self.returns.get("type")

    @property
    def is_deprecated(self) -> bool:
        """Heuristic: True if any decorator's text contains ``deprecated``."""
        # We tolerate variations such as ``@deprecated``, ``@deprecate``,
        # ``@deprecation.deprecated``. A plain substring check is enough
        # in practice; a false positive (e.g. a decorator literally named
        # ``not_deprecated``) would still flag a real concern.
        return any("deprecated" in (d or "").lower() for d in self.decorators)


class CodeTruthResolver:
    """Build ``CodeTruth`` objects from MapCoDoc DB rows."""

    def __init__(self, query_manager: "QueryManager"):
        self.qm = query_manager

    def resolve_direct(self, member: "MemberDetails") -> CodeTruth:
        """Build CodeTruth for a directly-defined ``DBMember``."""
        # ``MemberDetails`` already stores almost everything we need; we
        # repackage it into ``CodeTruth`` so downstream code doesn't have
        # to know about the DB-side type name.
        return CodeTruth(
            fqn=member.fqn,
            api_name=member.api_name or member.fqn,
            member_type=member.type,
            parameters=member.parameters or [],
            returns=member.returns,
            signatures=member.signatures or {},
            decorators=member.decorators or [],
            is_async=bool(member.is_async),
            is_static=bool(member.is_static),
            is_abstract=bool(member.is_abstract),
            is_property=bool(member.is_property),
            is_inherited=False,
            source_code=member.source_code,
            docstring=member.docstring,
        )

    def resolve_inherited(
        self, inherited: "InheritedMemberDetails",
    ) -> Optional[CodeTruth]:
        """Build CodeTruth for an inherited member.

        For internal inheritance we resolve the original member and reuse
        ``resolve_direct``, then mark ``is_inherited`` and override
        ``fqn``/``api_name`` with the inherited path so downstream
        artifacts use the user-facing name.

        For external inheritance there is no original member in our DB;
        we return None to signal that accuracy/completeness checks
        should be skipped.
        """
        if inherited.is_external or not inherited.original_member_id:
            # External: nothing to resolve. ``signature`` JSON on the
            # inherited record exists but is too thin for accuracy
            # comparison; we treat it as "code truth unavailable".
            return None

        original = self.qm.get_original_member_for_inherited(
            inherited.inherited_api_name,
        )
        if not original:
            # Original was promised but not found. Log and treat as
            # unresolvable so the caller can record an explanatory skip.
            logger.warning(
                "Inherited member %s claims original_member_id=%s but "
                "lookup returned no row.",
                inherited.inherited_api_name,
                inherited.original_member_id,
            )
            return None

        truth = self.resolve_direct(original)
        # Re-stamp identity fields so the report uses the inherited path
        # ('xgboost.XGBRFClassifier.evals_result') rather than the
        # original definition's path ('xgboost.XGBModel.evals_result').
        truth.fqn = inherited.inherited_api_name
        truth.api_name = inherited.inherited_api_name
        truth.is_inherited = True
        return truth
