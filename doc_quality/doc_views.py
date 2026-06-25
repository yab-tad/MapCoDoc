"""
DocView polymorphism over the structured documentation schema.

The structured documentation extractor uses two different JSON schemas depending on the member type (see ``doc_processor/structured_doc_extracter.py``):

* **Class schema**: ``module_member_description`` is an object ``{purpose, additional_information[]}``; classes have ``attributes[]`` and ``methods[]`` but no top-level ``returns``.
* **Function/method schema**: ``module_member_description`` is a plain string; there is a top-level ``returns`` object; no ``attributes``/``methods``.

Every check downstream of this module benefits from a uniform interface, so this module provides ``DocView`` (abstract) and two concrete implementations - one per schema - exposing the same set of getters.

All metric implementations should consume ``DocView`` rather than reaching into the raw ``api_reference`` dictionary directly. 
This isolates schema knowledge to one place; if the upstream schema changes, only the views need to be updated.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class DocView(ABC):
    """Abstract base for member-type-specific views over ``api_reference``.

    The constructor takes the raw JSON dict and the member type for record
    keeping. Use the module-level ``doc_view`` factory to obtain an instance
    of the correct concrete subclass.
    """

    def __init__(self, api_reference: Dict, member_type: str):
        # ``raw`` is exposed as a public attribute because some checks need
        # to walk the schema in unusual ways (e.g. searching every text
        # field for hyperlink presence). Mutation is discouraged but not
        # technically prevented.
        self.raw = api_reference
        self.member_type = member_type

    # -- Common getters --------------------------------------------------

    @abstractmethod
    def get_signature(self) -> Optional[str]:
        """Return the documented signature string or None if absent."""

    @abstractmethod
    def get_purpose(self) -> Optional[str]:
        """Return the primary purpose description text.

        For classes this is ``module_member_description.purpose``; for callables it is the ``module_member_description`` string itself.
        """

    @abstractmethod
    def get_purpose_additional_info(self) -> List[str]:
        """Return any free-form additional information for the purpose section.

        Only present in the class schema; callables return ``[]``.
        """

    @abstractmethod
    def get_parameters(self) -> List[Dict]:
        """Return the list of parameter dicts as stored in the doc."""

    @abstractmethod
    def get_examples(self) -> List[Dict]:
        """Return the list of example dicts."""

    @abstractmethod
    def get_additional_notes(self) -> Dict:
        """Return the ``additional_notes`` block (always a dict, possibly empty)."""

    # -- Schema-specific getters with safe defaults --------------------------------------------------------------------------------
    # The base class returns "absent" defaults so that metric code can query both schemas uniformly without conditional dispatch

    def get_returns(self) -> Optional[Dict]:
        """Return the top-level ``returns`` block.

        Defined on functions/methods only; classes return None.
        """
        return None

    def get_attributes(self) -> List[Dict]:
        """Return the class-level ``attributes`` list.

        Empty for callables.
        """
        return []

    def get_methods(self) -> List[Dict]:
        """Return the class-level ``methods`` list.

        Empty for callables.
        """
        return []

    # -- Convenience helpers ---------------------------------------------

    def iter_text_sections(self) -> List[tuple]:
        """Yield ``(section_label, text, json_path)`` triples for prose.

        Used by readability and maintainability evaluators that need to sweep every text-bearing field. The triples deliberately exclude
        code (examples) and structural fields like signatures, both of which are evaluated separately.

        Concrete subclasses override to add schema-specific sections.
        """
        # The base implementation contributes any sections common to both
        # schemas; subclasses extend the list.
        sections: list[tuple] = []
        # Examples' ``additional_information`` is a prose annotation that
        # exists in both schemas.
        for idx, ex in enumerate(self.get_examples()):
            ai = ex.get("additional_information")
            if ai:
                sections.append(
                    (f"examples[{idx}].additional_information",
                     ai,
                     f"$.examples[{idx}].additional_information")
                )
        # Additional notes are bulleted prose collections in both schemas.
        notes = self.get_additional_notes() or {}
        for i, item in enumerate(notes.get("supplementary_information", []) or []):
            sections.append(
                (f"additional_notes.supplementary_information[{i}]",
                 item,
                 f"$.additional_notes.supplementary_information[{i}]")
            )
        for i, item in enumerate(notes.get("edge_cases", []) or []):
            sections.append(
                (f"additional_notes.edge_cases[{i}]",
                 item,
                 f"$.additional_notes.edge_cases[{i}]")
            )
        return sections


class ClassDocView(DocView):
    """View over the class-shaped structured documentation schema."""

    def get_signature(self) -> Optional[str]:
        return self.raw.get("module_member_signature")

    def get_purpose(self) -> Optional[str]:
        # In the class schema ``module_member_description`` is itself an object; the actual purpose text lives under ``.purpose``.
        desc = self.raw.get("module_member_description") or {}
        return desc.get("purpose")

    def get_purpose_additional_info(self) -> List[str]:
        desc = self.raw.get("module_member_description") or {}
        return desc.get("additional_information") or []

    def get_parameters(self) -> List[Dict]:
        # Constructor parameters in the class schema.
        return self.raw.get("parameters") or []

    def get_attributes(self) -> List[Dict]:
        return self.raw.get("attributes") or []

    def get_methods(self) -> List[Dict]:
        return self.raw.get("methods") or []

    def get_examples(self) -> List[Dict]:
        return self.raw.get("examples") or []

    def get_additional_notes(self) -> Dict:
        return self.raw.get("additional_notes") or {}

    def iter_text_sections(self) -> List[tuple]:
        sections = super().iter_text_sections()
        # Class purpose sits inside the description object.
        if self.get_purpose():
            sections.append(
                ("module_member_description.purpose",
                 self.get_purpose(),
                 "$.module_member_description.purpose")
            )
        for i, item in enumerate(self.get_purpose_additional_info()):
            sections.append(
                (f"module_member_description.additional_information[{i}]",
                 item,
                 f"$.module_member_description.additional_information[{i}]")
            )
        # Parameter descriptions
        for p in self.get_parameters():
            name = p.get("name", "?")
            sections.append(
                (f"parameters[{name}].description",
                 p.get("description"),
                 f"$.parameters[?name=='{name}'].description")
            )
        # Class attribute descriptions (shallow context)
        for a in self.get_attributes():
            ident = a.get("identifier", "?")
            sections.append(
                (f"attributes[{ident}].description",
                 a.get("description"),
                 f"$.attributes[?identifier=='{ident}'].description")
            )
        # Class method descriptions (shallow context)
        for m in self.get_methods():
            mname = m.get("name", "?")
            sections.append(
                (f"methods[{mname}].description",
                 m.get("description"),
                 f"$.methods[?name=='{mname}'].description")
            )
        return sections


class CallableDocView(DocView):
    """View over the function/method structured documentation schema."""

    def get_signature(self) -> Optional[str]:
        return self.raw.get("module_member_signature")

    def get_purpose(self) -> Optional[str]:
        # In the callable schema ``module_member_description`` is a string,
        # not an object.
        val = self.raw.get("module_member_description")
        if isinstance(val, str):
            return val
        # Defensive fallback: occasionally a callable doc may have been extracted with the class shape; honour either form rather than silently returning None.
        if isinstance(val, dict):
            return val.get("purpose")
        return None

    def get_purpose_additional_info(self) -> List[str]:
        # Not present in the callable schema.
        return []

    def get_parameters(self) -> List[Dict]:
        return self.raw.get("parameters") or []

    def get_returns(self) -> Optional[Dict]:
        return self.raw.get("returns")

    def get_examples(self) -> List[Dict]:
        return self.raw.get("examples") or []

    def get_additional_notes(self) -> Dict:
        return self.raw.get("additional_notes") or {}

    def iter_text_sections(self) -> List[tuple]:
        sections = super().iter_text_sections()
        # Callable purpose is the string-valued top-level field.
        purpose = self.get_purpose()
        if purpose:
            sections.append(
                ("module_member_description",
                 purpose,
                 "$.module_member_description"),
            )
        # Parameter descriptions
        for p in self.get_parameters():
            name = p.get("name", "?")
            sections.append(
                (f"parameters[{name}].description",
                 p.get("description"),
                 f"$.parameters[?name=='{name}'].description")
            )
        # Returns description
        ret = self.get_returns() or {}
        if ret.get("description"):
            sections.append(
                ("returns.description",
                 ret.get("description"),
                 "$.returns.description")
            )
        return sections


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def doc_view(api_reference: Dict, member_type: str) -> DocView:
    """Construct the appropriate ``DocView`` subclass for ``member_type``.

    Args:
        api_reference: The raw structured documentation JSON.
        member_type: ``"class"``, ``"function"``, or ``"method"``.

    Returns:
        A ``ClassDocView`` for classes, a ``CallableDocView`` otherwise.
    """
    # ``member_type`` is normalized to lowercase to tolerate inconsistent casing in the DB
    # The "class" branch is intentionally exact rather than substring; ``"classmethod"`` is a callable, not a class doc.
    if member_type and member_type.lower() == "class":
        return ClassDocView(api_reference, member_type)
    return CallableDocView(api_reference, member_type)
