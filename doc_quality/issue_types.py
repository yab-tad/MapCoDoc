"""
The complete enumeration of issue types emitted by the evaluator.

Every check in every dimension produces issues whose ``issue_type`` is drawn
from the ``IssueType`` enum below. Each enum value carries an
``IssueTypeSpec`` that records the issue's dimension, default severity,
default maintainer strategy, and a JSONPath template. Together this is the
formal contract between evaluator and maintainer:

* The evaluator may emit *only* these issue types.
* The maintainer's strategy dispatch table is keyed by issue type.
* The reporting layer groups issues by issue type and dimension.

Changing this file is therefore a contract change and should propagate to
the maintainer dispatch tables and to any persisted artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from doc_quality.models import Dimension, MaintainerStrategy, Severity


# ---------------------------------------------------------------------------
# Issue type metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IssueTypeSpec:
    """Static metadata associated with a single ``IssueType`` value.

    The spec encodes everything the evaluator and maintainer need to know
    *without* looking at the runtime context of an individual issue: which
    dimension it contributes to, what severity to default to, what strategy
    the maintainer should use, and where in the ``api_reference`` JSON the
    issue lives. ``json_path_template`` may contain ``{target}`` which the
    evaluator substitutes at issue-construction time with the per-instance
    target identifier (e.g. a parameter name).

    The class is frozen so instances may be hashed and compared by value.
    """

    code: str
    dimension: Dimension
    default_severity: Severity
    default_strategy: MaintainerStrategy
    json_path_template: str
    description: str

    def render_path(self, target: str | None = None,
                    section: str | None = None) -> str:
        """Substitute ``{target}`` and ``{section}`` placeholders.

        ``{target}`` is the per-instance identifier (e.g. parameter name).
        ``{section}`` is used by the maintainability checks where the same
        issue type may apply to several text sections.
        """
        rendered = self.json_path_template
        if target is not None:
            # Quote the target so JSONPath filter expressions like
            # ``[?name=='reduction']`` come out correctly.
            rendered = rendered.replace("{target}", _quote(target))
        if section is not None:
            rendered = rendered.replace("{section}", section)
        return rendered


def _quote(value: str) -> str:
    """Wrap ``value`` in single quotes, escaping any embedded quotes."""
    # Parameter names should never contain quotes in practice, but defend
    # anyway so generated JSONPaths are always parseable.
    escaped = value.replace("'", "\\'")
    return f"'{escaped}'"


# ---------------------------------------------------------------------------
# IssueType enum
#
# Naming convention:
#   <DIMENSION_PREFIX>_<TARGET>_<DEFECT>
# Prefixes:
#   COMP_   completeness
#   ACC_    accuracy
#   READ_   readability (text or code)
#   MAINT_  maintainability
# ---------------------------------------------------------------------------

class IssueType(Enum):
    """Closed enumeration of every quality issue the evaluator can emit."""

    # ------------------------------------------------------------------
    # Completeness - "is the field populated?"
    # ------------------------------------------------------------------

    COMP_SIGNATURE_MISSING = IssueTypeSpec(
        code="COMP_SIGNATURE_MISSING",
        dimension=Dimension.COMPLETENESS,
        default_severity=Severity.HIGH,
        default_strategy=MaintainerStrategy.AST_DERIVED,
        json_path_template="$.module_member_signature",
        description="Module member signature is missing or 'N/A'."
    )

    COMP_PURPOSE_MISSING = IssueTypeSpec(
        code="COMP_PURPOSE_MISSING",
        dimension=Dimension.COMPLETENESS,
        default_severity=Severity.HIGH,
        default_strategy=MaintainerStrategy.LLM,
        # The path differs between class and callable schemas; the
        # evaluator selects the correct concrete path when constructing
        # the issue.
        json_path_template="$.module_member_description.purpose",
        description="Purpose description is missing or 'N/A'."
    )

    COMP_PARAM_NAME_MISSING = IssueTypeSpec(
        code="COMP_PARAM_NAME_MISSING",
        dimension=Dimension.COMPLETENESS,
        default_severity=Severity.HIGH,
        default_strategy=MaintainerStrategy.DB_QUERY,
        json_path_template="$.parameters[?name=={target}].name",
        description="Parameter has no documented name."
    )

    COMP_PARAM_TYPE_MISSING = IssueTypeSpec(
        code="COMP_PARAM_TYPE_MISSING",
        dimension=Dimension.COMPLETENESS,
        default_severity=Severity.HIGH,
        default_strategy=MaintainerStrategy.DB_QUERY,
        json_path_template="$.parameters[?name=={target}].type",
        description="Parameter type is missing or 'N/A'."
    )

    COMP_PARAM_DESCRIPTION_MISSING = IssueTypeSpec(
        code="COMP_PARAM_DESCRIPTION_MISSING",
        dimension=Dimension.COMPLETENESS,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.parameters[?name=={target}].description",
        description="Parameter description is missing or 'N/A'."
    )

    COMP_RETURN_TYPE_MISSING = IssueTypeSpec(
        code="COMP_RETURN_TYPE_MISSING",
        dimension=Dimension.COMPLETENESS,
        default_severity=Severity.HIGH,
        default_strategy=MaintainerStrategy.DB_QUERY,
        json_path_template="$.returns.type",
        description="Return type is missing or 'N/A' for a callable that returns a value."
    )

    COMP_RETURN_DESCRIPTION_MISSING = IssueTypeSpec(
        code="COMP_RETURN_DESCRIPTION_MISSING",
        dimension=Dimension.COMPLETENESS,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.returns.description",
        description="Return value description is missing or 'N/A'."
    )

    COMP_NO_EXAMPLES = IssueTypeSpec(
        code="COMP_NO_EXAMPLES",
        dimension=Dimension.COMPLETENESS,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.examples",
        description="No usage examples are provided."
    )

    COMP_EXAMPLE_EMPTY = IssueTypeSpec(
        code="COMP_EXAMPLE_EMPTY",
        dimension=Dimension.COMPLETENESS,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.LLM,
        # ``{target}`` is the example index in this case.
        json_path_template="$.examples[{target}].example",
        description="An example entry exists but its content is empty or 'N/A'."
    )

    COMP_CLASS_METHOD_DESC_MISSING = IssueTypeSpec(
        code="COMP_CLASS_METHOD_DESC_MISSING",
        dimension=Dimension.COMPLETENESS,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.methods[?name=={target}].description",
        description="A method listed under the class has no description (shallow check)."
    )

    COMP_CLASS_ATTR_DESC_MISSING = IssueTypeSpec(
        code="COMP_CLASS_ATTR_DESC_MISSING",
        dimension=Dimension.COMPLETENESS,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.attributes[?identifier=={target}].description",
        description="An attribute listed under the class has no description (shallow check)."
    )

    # ------------------------------------------------------------------
    # Accuracy - "does the doc agree with the code?"
    # ------------------------------------------------------------------

    ACC_PARAM_NAME_UNKNOWN = IssueTypeSpec(
        code="ACC_PARAM_NAME_UNKNOWN",
        dimension=Dimension.ACCURACY,
        default_severity=Severity.HIGH,
        default_strategy=MaintainerStrategy.DB_QUERY,
        json_path_template="$.parameters[?name=={target}]",
        description="A parameter is documented but not present in the code signature."
    )

    ACC_PARAM_MISSING_FROM_DOC = IssueTypeSpec(
        code="ACC_PARAM_MISSING_FROM_DOC",
        dimension=Dimension.ACCURACY,
        default_severity=Severity.HIGH,
        default_strategy=MaintainerStrategy.DB_QUERY,
        json_path_template="$.parameters",
        description="A parameter is present in the code signature but not documented."  
    )

    ACC_PARAM_TYPE_MISMATCH = IssueTypeSpec(
        code="ACC_PARAM_TYPE_MISMATCH",
        dimension=Dimension.ACCURACY,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.DB_QUERY,
        json_path_template="$.parameters[?name=={target}].type",
        description="Parameter type in the doc does not match the code annotation."
    )

    ACC_PARAM_DEFAULT_MISMATCH = IssueTypeSpec(
        code="ACC_PARAM_DEFAULT_MISMATCH",
        dimension=Dimension.ACCURACY,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.DB_QUERY,
        json_path_template="$.parameters[?name=={target}].description",
        description="Default value in the doc disagrees with the code default."
    )

    ACC_RETURN_TYPE_MISMATCH = IssueTypeSpec(
        code="ACC_RETURN_TYPE_MISMATCH",
        dimension=Dimension.ACCURACY,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.DB_QUERY,
        json_path_template="$.returns.type",
        description="Return type in the doc disagrees with the code annotation."
    )

    ACC_SIGNATURE_DRIFT = IssueTypeSpec(
        code="ACC_SIGNATURE_DRIFT",
        dimension=Dimension.ACCURACY,
        default_severity=Severity.HIGH,
        default_strategy=MaintainerStrategy.AST_DERIVED,
        json_path_template="$.module_member_signature",
        description="Documented signature has drifted from the code signature."
    )

    ACC_ASYNC_MARKER_MISSING = IssueTypeSpec(
        code="ACC_ASYNC_MARKER_MISSING",
        dimension=Dimension.ACCURACY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.AST_DERIVED,
        json_path_template="$.module_member_signature",
        description="Code member is async but the documented signature lacks 'async'."
    )

    ACC_DEPRECATED_NOT_NOTED = IssueTypeSpec(
        code="ACC_DEPRECATED_NOT_NOTED",
        dimension=Dimension.ACCURACY,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.additional_notes.supplementary_information",
        description="Code is decorated @deprecated but the doc does not mention deprecation."
    )

    ACC_CLASS_METHOD_NOT_FOUND = IssueTypeSpec(
        code="ACC_CLASS_METHOD_NOT_FOUND",
        dimension=Dimension.ACCURACY,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.MANUAL,
        json_path_template="$.methods[?name=={target}]",
        description="A method listed under the class doc is not found in the class definition."
    )

    ACC_CLASS_ATTR_NOT_FOUND = IssueTypeSpec(
        code="ACC_CLASS_ATTR_NOT_FOUND",
        dimension=Dimension.ACCURACY,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.MANUAL,
        json_path_template="$.attributes[?identifier=={target}]",
        description="An attribute listed under the class doc is not found in the class definition."
    )

    ACC_CLASS_METHOD_SIG_DRIFT = IssueTypeSpec(
        code="ACC_CLASS_METHOD_SIG_DRIFT",
        dimension=Dimension.ACCURACY,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.AST_DERIVED,
        json_path_template="$.methods[?name=={target}].signature",
        description="Class method signature in doc disagrees with the code signature."
    )

    # ------------------------------------------------------------------
    # Readability - "is the prose clear and the code example illustrative?"
    # ------------------------------------------------------------------

    READ_PURPOSE_TOO_TERSE = IssueTypeSpec(
        code="READ_PURPOSE_TOO_TERSE",
        dimension=Dimension.READABILITY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.module_member_description.purpose",
        description="Purpose description is below the minimum informative length."
    )

    READ_TEXT_GRADE_TOO_HIGH = IssueTypeSpec(
        code="READ_TEXT_GRADE_TOO_HIGH",
        dimension=Dimension.READABILITY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.LLM,
        # The concrete path is rendered by the readability evaluator, which
        # may apply this issue type to multiple sections.
        json_path_template="$.{section}",
        description="Text readability indices indicate above-target reading difficulty."
    )

    READ_PASSIVE_VOICE_HIGH = IssueTypeSpec(
        code="READ_PASSIVE_VOICE_HIGH",
        dimension=Dimension.READABILITY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.{section}",
        description="Passive voice density exceeds the recommended threshold."  
    )

    READ_PARAM_DESC_TYPE_ECHO = IssueTypeSpec(
        code="READ_PARAM_DESC_TYPE_ECHO",
        dimension=Dimension.READABILITY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.parameters[?name=={target}].description",
        description="Parameter description merely restates the type."
    )

    READ_EXAMPLE_NO_EXPLANATION = IssueTypeSpec(
        code="READ_EXAMPLE_NO_EXPLANATION",
        dimension=Dimension.READABILITY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.examples[{target}].additional_information",
        description="Example has no comments, output annotation, or accompanying explanation."
    )

    READ_EXAMPLE_SINGLE_CHAR_VAR = IssueTypeSpec(
        code="READ_EXAMPLE_SINGLE_CHAR_VAR",
        dimension=Dimension.READABILITY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.examples[{target}].example",
        description="Example uses single-character identifiers heavily, hurting readability."
    )

    READ_EXAMPLE_HIGH_COMPLEXITY = IssueTypeSpec(
        code="READ_EXAMPLE_HIGH_COMPLEXITY",
        dimension=Dimension.READABILITY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.examples[{target}].example",
        description="Example is too long or too complex to serve as an illustration."
    )

    READ_EXAMPLE_PARSE_FAILED = IssueTypeSpec(
        code="READ_EXAMPLE_PARSE_FAILED",
        dimension=Dimension.READABILITY,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.MANUAL,
        json_path_template="$.examples[{target}].example",
        description="Example does not parse as valid Python."
    )

    # ------------------------------------------------------------------
    # Maintainability - "are cross-references and hyperlinks used well?"
    # ------------------------------------------------------------------

    MAINT_NO_HYPERLINKS_IN_SECTION = IssueTypeSpec(
        code="MAINT_NO_HYPERLINKS_IN_SECTION",
        dimension=Dimension.MAINTAINABILITY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.{section}",
        description="Section contains type/member mentions but no hyperlinks."
    )

    MAINT_TYPE_NOT_LINKED = IssueTypeSpec(
        code="MAINT_TYPE_NOT_LINKED",
        dimension=Dimension.MAINTAINABILITY,
        default_severity=Severity.MEDIUM,
        default_strategy=MaintainerStrategy.DB_QUERY,
        json_path_template="$.parameters[?name=={target}].type",
        description="Parameter type references a known entity but is not hyperlinked."
    )

    MAINT_INTERNAL_REFERENCE_NOT_LINKED = IssueTypeSpec(
        code="MAINT_INTERNAL_REFERENCE_NOT_LINKED",
        dimension=Dimension.MAINTAINABILITY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.DB_QUERY,
        json_path_template="$.{section}",
        description="A mention of another library member is not hyperlinked."
    )

    MAINT_BUILTIN_NOT_LINKED = IssueTypeSpec(
        code="MAINT_BUILTIN_NOT_LINKED",
        dimension=Dimension.MAINTAINABILITY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.DB_QUERY,
        json_path_template="$.{section}",
        description="A mention of a Python builtin/stdlib type is not hyperlinked."
    )

    MAINT_INLINE_TYPE_RESTATEMENT = IssueTypeSpec(
        code="MAINT_INLINE_TYPE_RESTATEMENT",
        dimension=Dimension.MAINTAINABILITY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.LLM,
        json_path_template="$.parameters[?name=={target}].description",
        description="Description restates the type in prose; consider rewording with a cross-ref."
    )

    MAINT_BROKEN_PLACEHOLDER = IssueTypeSpec(
        code="MAINT_BROKEN_PLACEHOLDER",
        dimension=Dimension.MAINTAINABILITY,
        default_severity=Severity.HIGH,
        default_strategy=MaintainerStrategy.MANUAL,
        json_path_template="$.{section}",
        description="An unreplaced 'url_placeholder_N' token survives in the doc."
    )

    MAINT_OVER_LINKING = IssueTypeSpec(
        code="MAINT_OVER_LINKING",
        dimension=Dimension.MAINTAINABILITY,
        default_severity=Severity.LOW,
        default_strategy=MaintainerStrategy.MANUAL,
        json_path_template="$.{section}",
        description="Hyperlink density above readable threshold; reader may be distracted."
    )

    # ------------------------------------------------------------------
    # Convenience: lookup by string code (for deserialization)
    # ------------------------------------------------------------------

    @classmethod
    def by_code(cls, code: str) -> "IssueType":
        """Return the IssueType whose spec.code matches ``code``.

        Used by JSON deserialization of persisted reports/candidates so
        the caller doesn't need to know the exact enum identifier.
        """
        for member in cls:
            if member.value.code == code:
                return member
        raise ValueError(f"Unknown IssueType code: {code!r}")
