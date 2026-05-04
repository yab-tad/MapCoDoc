"""
Manual approval workflow for maintenance candidates.

The maintainer never writes to the database directly. Instead, it
deposits a ``MaintenanceCandidate`` JSON in the artifact store. This
module provides the approval gate: a small set of operations that a human
operator (or a CLI) invokes to inspect a candidate, approve it (which
writes it back to the DB), or roll back a previously-applied member.

Operations:

* ``list_pending``  - candidates that exist in ``maintained/`` but have not
  yet been applied.
* ``show_diff``     - render a unified diff between original and candidate.
* ``approve``       - write the candidate's ``api_reference`` back to the
  DB and stamp the artifact as applied.
* ``rollback``      - write the original snapshot back to the DB and log
  the rollback.

The approval workflow updates the quick-access mirror columns
(``doc_signature``, ``doc_description``, ``doc_examples``) when applying
a candidate, so they remain consistent with ``api_reference``.
"""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from doc_quality.artifacts.store import ArtifactStore


logger = logging.getLogger(__name__)


class ApprovalWorkflow:
    """Manual-gated DB writeback for maintenance candidates."""

    def __init__(self, store: "ArtifactStore", session: "Session") -> None:
        self.store = store
        self.session = session

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def list_pending(self) -> List[str]:
        """Return api_names of candidates that exist but are not applied.

        A candidate is "pending" if a maintained file exists but no
        applied file does.
        """
        pending: List[str] = []
        for cand_path in self.store.iter_candidates():
            api_name = cand_path.stem
            if not (self.store.root / "applied" / cand_path.name).exists():
                pending.append(api_name)
        return pending

    def show_diff(self, api_name: str) -> str:
        """Return a unified diff string between original and candidate."""
        original = self.store.load_original(api_name)
        candidate = self.store.load_candidate(api_name)
        if original is None or candidate is None:
            raise FileNotFoundError(
                f"Missing artifacts for api_name={api_name!r}",
            )
        # Compare just the candidate's api_reference field.
        cand_ref = candidate.get("candidate_api_reference") or candidate
        original_text = json.dumps(original, indent=2, sort_keys=True).splitlines()
        candidate_text = json.dumps(cand_ref, indent=2, sort_keys=True).splitlines()
        diff_iter = difflib.unified_diff(
            original_text, candidate_text,
            fromfile=f"original/{api_name}.json",
            tofile=f"maintained/{api_name}.json",
            lineterm="",
        )
        return "\n".join(diff_iter)

    # ------------------------------------------------------------------
    # Apply / Rollback
    # ------------------------------------------------------------------

    def approve(self, api_name: str) -> None:
        """Persist the maintained candidate to the DB and stamp the artifact.

        The DB write is wrapped in a transaction so a failure halfway
        through (e.g. one mirror column updates but the JSON column
        write fails) leaves the row unchanged.
        """
        candidate = self.store.load_candidate(api_name)
        if candidate is None:
            raise FileNotFoundError(
                f"No maintained candidate for api_name={api_name!r}",
            )
        new_api_ref = candidate.get("candidate_api_reference") or candidate

        # Find the target row. Try direct member first, then inherited.
        member_row = self._find_db_row(api_name)
        if member_row is None:
            raise LookupError(
                f"Could not locate DB row for api_name={api_name!r}",
            )

        # Update api_reference and the quick-access mirrors. The mirrors
        # are kept in sync because several downstream queries
        # (search, list_with_documentation) read them.
        member_row.api_reference = new_api_ref
        member_row.doc_signature = new_api_ref.get("module_member_signature")
        # Description: callable schema is a string; class schema is an
        # object. The mirror column is text - extract appropriately.
        desc = new_api_ref.get("module_member_description")
        if isinstance(desc, dict):
            member_row.doc_description = desc.get("purpose")
        elif isinstance(desc, str):
            member_row.doc_description = desc
        # Examples mirror.
        member_row.doc_examples = new_api_ref.get("examples") or []

        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

        self.store.mark_applied(api_name, applied_at=datetime.now(timezone.utc))

    def rollback(self, api_name: str) -> None:
        """Restore the original snapshot to the DB and log the rollback."""
        original = self.store.load_original(api_name)
        if original is None:
            raise FileNotFoundError(
                f"No original snapshot for api_name={api_name!r}",
            )
        member_row = self._find_db_row(api_name)
        if member_row is None:
            raise LookupError(
                f"Could not locate DB row for api_name={api_name!r}",
            )

        # Restore api_reference and rebuild the mirror columns from it.
        member_row.api_reference = original
        member_row.doc_signature = original.get("module_member_signature")
        desc = original.get("module_member_description")
        if isinstance(desc, dict):
            member_row.doc_description = desc.get("purpose")
        elif isinstance(desc, str):
            member_row.doc_description = desc
        member_row.doc_examples = original.get("examples") or []

        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

        self.store.mark_rolled_back(api_name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_db_row(self, api_name: str):
        """Locate the DBMember (or DBInheritedMember) row for ``api_name``."""
        # Imported locally to avoid a hard package-level dependency on
        # mapcodoc_db at import time.
        from mapcodoc_db.db_models import DBInheritedMember, DBMember

        # Try direct DBMember first - by primary_api_name and FQN.
        row = (
            self.session.query(DBMember)
            .filter(DBMember.primary_api_name == api_name)
            .one_or_none()
        )
        if row is not None:
            return row
        row = (
            self.session.query(DBMember)
            .filter(DBMember.fully_qualified_name == api_name)
            .one_or_none()
        )
        if row is not None:
            return row
        # Try inherited row.
        row = (
            self.session.query(DBInheritedMember)
            .filter(DBInheritedMember.inherited_api_name == api_name)
            .one_or_none()
        )
        return row
