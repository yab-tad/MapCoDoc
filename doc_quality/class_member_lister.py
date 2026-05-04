"""
Enumerate the methods and attributes of a class for shallow doc cross-reference.

The structured class documentation may include short descriptions of a
class's methods and attributes (see PyTorch ``L1Loss`` or pandas
``DataFrame``). Per the design decision, these nested entries are evaluated
shallowly: we verify that each documented entry refers to a real member of
the class and that its description is non-trivial. Detailed evaluation of
the method/attribute itself is performed against its standalone structured
documentation.

For shallow cross-referencing we need a single canonical answer to "what
methods and attributes does this class have?" The information lives in
multiple places:

* Methods are stored as ``DBMember`` rows with ``parent_id`` pointing
  to the class. ``QueryManager.get_class_methods`` retrieves them.
* Properties (``@property``-decorated members) appear as method-typed
  ``DBMember`` rows with ``is_property == True``; they are class-facing
  attributes from the user's perspective.
* Class-level variables (``MAX_SIZE = 100`` at the class body) may
  appear as ``DBMember`` rows of type ``variable``.
* Instance attributes (``self.x = ...`` inside ``__init__``) are not
  routinely stored; they need to be extracted via AST from the class's
  source code.

This module unifies all four sources into a single ``ClassMembers`` value
object that the cross-reference checks consume.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Set

if TYPE_CHECKING:
    # Imported only for type hints. We avoid the runtime import to allow
    # this module to be imported without a configured DB session - useful
    # for unit tests that exercise the AST extractor in isolation.
    from mapcodoc_db.query import QueryManager, MemberDetails

logger = logging.getLogger(__name__)


@dataclass
class ClassMembers:
    """The enumerated members of a class, with provenance per name.

    ``method_names`` maps a method's short name to its ``MemberDetails``
    (so callers can compare signatures cheaply). ``attribute_names`` is a
    flat set; ``properties`` is the subset that came from
    ``@property``-decorated members. ``sources`` records where each name
    was learned from for debugging and reporting.
    """

    method_names: Dict[str, "MemberDetails"] = field(default_factory=dict)
    attribute_names: Set[str] = field(default_factory=set)
    properties: Set[str] = field(default_factory=set)
    # ``name -> 'method'|'property'|'class_var'|'instance_var'``
    sources: Dict[str, str] = field(default_factory=dict)

    def has_method(self, name: str) -> bool:
        """Return True if ``name`` is a method of the class."""
        return name in self.method_names

    def has_attribute(self, name: str) -> bool:
        """Return True if ``name`` is an attribute (property, class-var, or instance-var)."""
        return name in self.attribute_names


class ClassMemberLister:
    """Builds ``ClassMembers`` from the DB plus an AST-based fallback.

    The lister does not own a session itself; it accepts a configured
    ``QueryManager`` and uses it to walk the relevant ``DBMember`` rows.
    """

    def __init__(self, query_manager: "QueryManager"):
        self.qm = query_manager

    def list_members(self, class_fqn: str) -> ClassMembers:
        """Return the union of methods and attributes for ``class_fqn``.

        The function never raises if a class is missing or its source code
        is unparseable; it logs a warning and returns whatever could be
        retrieved. This is intentional - the cross-reference check
        downgrades severity when the lister is uncertain rather than
        failing the whole evaluation.
        """
        result = ClassMembers()

        # ---------------- Source 1: DB children of the class -----------
        children = self.qm.get_class_methods(class_fqn) or []
        for child in children:
            # Each ``MemberDetails`` carries a ``type`` field; the
            # branching here is per-type because a property is *both* a
            # method on disk and an attribute conceptually.
            if getattr(child, "is_property", False):
                # Treat properties as user-visible attributes.
                result.attribute_names.add(child.name)
                result.properties.add(child.name)
                result.sources[child.name] = "property"
            elif child.type == "method":
                result.method_names[child.name] = child
                result.sources[child.name] = "method"
            elif child.type == "variable":
                # Class-level variables - these *do* count as attributes.
                result.attribute_names.add(child.name)
                result.sources.setdefault(child.name, "class_var")
            else:
                # Unknown child type (e.g. nested classes). We don't add
                # them as either methods or attributes - they have their
                # own documentation entry.
                continue

        # ---------------- Source 2: AST instance attributes ------------
        # Instance attributes set inside ``__init__`` (``self.x = ...``)
        # are usually not in the DB. Extract them by walking the class's
        # source code if available.
        cls_member = self.qm.get_member_details(class_fqn)
        if cls_member and getattr(cls_member, "source_code", None):
            try:
                instance_attrs = self._extract_instance_attrs(cls_member.source_code)
            except SyntaxError as exc:
                # A syntactically-invalid source_code is unusual but
                # possible (e.g. truncated extraction). Log and continue
                # with what we already have.
                logger.warning(
                    "AST parse failed for %s: %s. Instance attributes will be incomplete.",
                    class_fqn, exc,
                )
                instance_attrs = set()
            for name in instance_attrs:
                # Don't overwrite a higher-precedence source. ``setdefault``
                # leaves earlier "property"/"class_var" markers in place.
                result.attribute_names.add(name)
                result.sources.setdefault(name, "instance_var")

        return result

    # ------------------------------------------------------------------
    # AST-based instance-attribute extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_instance_attrs(source_code: str) -> Set[str]:
        """Find ``self.<name> = ...`` and class-body assignments.

        The walker handles:

        * ``self.X = value``                     -> attribute X
        * ``self.X: T = value``                  -> attribute X (annotated)
        * ``X = value`` at class-body scope       -> attribute X (class var)
        * ``X: T = value`` at class-body scope    -> attribute X (class var)

        Nested classes' assignments are *not* hoisted into the outer
        class - that would conflate inner/outer namespaces.
        """
        # Parse once, then walk. ``ast.walk`` gives us every node, but we
        # need ClassDef-scoped iteration to capture class-body assignments
        # without consuming nested classes. So we iterate top-down,
        # finding ``ClassDef`` nodes and treating their immediate body as
        # the class body. ``self.X`` assignments are collected globally
        # because they may appear anywhere, but in practice they sit
        # inside methods of the outermost class.
        tree = ast.parse(source_code)
        found: Set[str] = set()

        # First pass: collect ``self.X = ...`` from anywhere in the tree.
        # In typical usage ``source_code`` is the body of *one* class
        # (extracted by the analyzer), so all ``self.<name>`` references
        # belong to that class.
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = ClassMemberLister._self_attr_name(tgt)
                    if name is not None:
                        found.add(name)
            elif isinstance(node, ast.AnnAssign):
                name = ClassMemberLister._self_attr_name(node.target)
                if name is not None:
                    found.add(name)

        # Second pass: class-body assignments (``X = ...`` at class scope).
        # We look at the immediate body of each top-level ClassDef only.
        # Nested ClassDefs are skipped - their attributes belong to the
        # nested class, not the outer one.
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for tgt in stmt.targets:
                            if isinstance(tgt, ast.Name):
                                found.add(tgt.id)
                    elif isinstance(stmt, ast.AnnAssign):
                        if isinstance(stmt.target, ast.Name):
                            found.add(stmt.target.id)

        # Filter out dunder names and private leading-underscore names from
        # the *attribute* set: while these are technically attributes,
        # they are not part of the public API the doc cross-references
        # against. The structured doc only lists public attributes.
        return {n for n in found if not n.startswith("_")}

    @staticmethod
    def _self_attr_name(target_node: ast.expr) -> str | None:
        """Return the attribute name if ``target_node`` is a ``self.X`` access.

        Returns None for any other target shape (subscripts, plain names,
        chained attributes such as ``self.x.y``).
        """
        if (
            isinstance(target_node, ast.Attribute)
            and isinstance(target_node.value, ast.Name)
            and target_node.value.id == "self"
        ):
            return target_node.attr
        return None
