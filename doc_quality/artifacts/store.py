"""
ArtifactStore - filesystem-backed provenance for evaluation and maintenance.

Layout under the configured root ``<output_root>/<library>/<version>/``::

    original/<api_name>.json          # immutable snapshot of api_reference at evaluation time
    evaluation/<api_name>.json        # full EvaluationReport
    maintained/<api_name>.json        # MaintenanceCandidate (candidate api_reference + patches)
    applied/<api_name>.json           # post-approval, what was written to DB
    log.jsonl                         # append-only operation log
    summary.json                      # cross-member library-level aggregate (optional)

API names may contain characters illegal on Windows (``:``, ``*``, etc.).
The store sanitizes them once and uses the sanitized form throughout.
Lookups by original API name work because the sanitization is deterministic.

Reports and candidates are serialized as JSON with timestamps in ISO 8601
format and dimension/severity/strategy values as strings (the underlying
enums are str subclasses, so this happens automatically).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from doc_quality.issue_types import IssueType
from doc_quality.models import (
    Dimension,
    DimensionScore,
    EvaluationReport,
    Issue,
    MaintainerStrategy,
    MaintenanceCandidate,
    MaintenancePatch,
    Severity,
)


logger = logging.getLogger(__name__)


# Characters that would be illegal in Windows filenames or that complicate path handling. Replace each occurrence with an underscore
# The mapping is intentionally lossy but stable: the same input always produces the same output, which is what matters for round-tripping.
_FILENAME_INVALID = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")


def _sanitize_filename(name: str) -> str:
    """Map an api_name to a filesystem-safe filename.

    Trailing dots are stripped because Windows treats ``foo.`` as ``foo``;
    repeated underscores from invalid-character substitution are
    collapsed for cosmetic reasons.
    """
    s = _FILENAME_INVALID.sub("_", name)
    # Collapse runs of underscores to one for tidiness.
    s = re.sub(r"_+", "_", s)
    s = s.strip(" .")
    return s or "unnamed"


# ---------------------------------------------------------------------------
# JSON encoder for dataclasses + enums + datetimes
# ---------------------------------------------------------------------------

class _ArtifactEncoder(json.JSONEncoder):
    """Encode dataclass / enum / datetime instances for ``json.dump``."""

    def default(self, o: Any) -> Any:
        # Datetime: ISO 8601 with microsecond precision.
        if isinstance(o, datetime):
            return o.isoformat()
        # Enum: emit the value (string) rather than the qualified name.
        if isinstance(o, Enum):
            # IssueType wraps an IssueTypeSpec; export its ``code`` for
            # round-trip-friendliness.
            if isinstance(o, IssueType):
                return o.value.code
            return o.value
        # Dataclasses (Issue, EvaluationReport, etc.).
        if is_dataclass(o):
            return _dataclass_to_json(o)
        # Sets are not natively JSON-serializable; convert to sorted lists.
        if isinstance(o, set):
            return sorted(o)
        return super().default(o)


def _dataclass_to_json(obj: Any) -> Any:
    """Convert a dataclass tree to JSON-friendly primitives.

    ``dataclasses.asdict`` doesn't know about Enums, so we walk the tree
    ourselves. This is more code than calling ``asdict`` + a recursive
    fixup but it produces nicer error messages.
    """
    if is_dataclass(obj):
        return {f: _dataclass_to_json(v) for f, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(_dataclass_to_json(k)): _dataclass_to_json(v)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_dataclass_to_json(v) for v in obj]
    if isinstance(obj, Enum):
        if isinstance(obj, IssueType):
            return obj.value.code
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class ArtifactStore:
    """Filesystem store for evaluation/maintenance artifacts."""

    def __init__(
        self, output_root: Path, library: str, version: str,
    ) -> None:
        self.root = Path(output_root) / library / version
        self.library = library
        self.version = version
        # Ensure all subdirectories exist; ``mkdir(parents=True,
        # exist_ok=True)`` is idempotent.
        for sub in ("original", "evaluation", "maintained", "applied"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.log_path = self.root / "log.jsonl"
        self.summary_path = self.root / "summary.json"

    # ----- Save operations --------------------------------------------

    def save_original(self, api_name: str, api_reference: Dict) -> Path:
        """Persist the pre-maintenance snapshot of ``api_reference``.

        Subsequent calls with the same api_name are *idempotent*: the
        first save wins. This is intentional - the "original" represents
        the state at first observation and should not be overwritten.
        """
        path = self._path("original", api_name)
        if path.exists():
            return path
        self._write_json(path, api_reference)
        self._append_log("save_original", api_name, {})
        return path

    def save_evaluation(self, report: EvaluationReport) -> Path:
        """Persist a full ``EvaluationReport`` for a member."""
        path = self._path("evaluation", report.member_api_name)
        # Each save replaces the previous evaluation; we keep only the
        # latest. Historical evaluations are recoverable from the log.
        self._write_json(path, report)
        self._append_log("save_evaluation", report.member_api_name, {
            "overall_score": report.overall_score,
            "skipped": report.skipped,
            "issue_counts": _summarize_issue_counts(report),
        })
        return path

    def save_candidate(self, candidate: MaintenanceCandidate) -> Path:
        """Persist a maintenance candidate awaiting approval."""
        path = self._path("maintained", candidate.member_api_name)
        self._write_json(path, candidate)
        self._append_log("save_candidate", candidate.member_api_name, {
            "patch_count": len(candidate.patches),
        })
        return path

    def mark_applied(
        self, api_name: str, applied_at: Optional[datetime] = None,
    ) -> Path:
        """Copy the maintained candidate into ``applied/`` and stamp it.

        Called by the approval workflow after a successful DB writeback.
        Returns the path of the applied artifact.
        """
        candidate_path = self._path("maintained", api_name)
        applied_path = self._path("applied", api_name)
        if not candidate_path.exists():
            raise FileNotFoundError(
                f"No maintained candidate to apply for api_name={api_name!r}",
            )
        # Read the candidate JSON, stamp ``applied_to_db=True`` and a
        # fresh timestamp, then write to the applied subdirectory.
        with candidate_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        payload["applied_to_db"] = True
        payload["applied_at"] = (
            applied_at or datetime.now(timezone.utc)
        ).isoformat()
        with applied_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        self._append_log("apply", api_name, {})
        return applied_path

    def mark_rolled_back(self, api_name: str) -> None:
        """Note in the log that a previously-applied member was rolled back."""
        # We deliberately do not delete the ``applied/`` artifact - it
        # preserves the history. The log entry is the canonical record.
        self._append_log("rollback", api_name, {})

    # ----- Load operations --------------------------------------------

    def load_original(self, api_name: str) -> Optional[Dict]:
        """Return the original ``api_reference`` for ``api_name`` or None."""
        return self._read_json(self._path("original", api_name))

    def load_evaluation(self, api_name: str) -> Optional[Dict]:
        """Return the latest stored evaluation as a JSON-decoded dict.

        We don't reconstruct the dataclass here - callers that need typed
        objects should use the higher-level ``reporting`` module which
        handles the deserialization.
        """
        return self._read_json(self._path("evaluation", api_name))

    def load_candidate(self, api_name: str) -> Optional[Dict]:
        return self._read_json(self._path("maintained", api_name))

    def load_applied(self, api_name: str) -> Optional[Dict]:
        return self._read_json(self._path("applied", api_name))

    # ----- Iteration --------------------------------------------------

    def iter_evaluations(self) -> List[Path]:
        """Return a sorted list of paths in ``evaluation/``."""
        return sorted((self.root / "evaluation").glob("*.json"))

    def iter_candidates(self) -> List[Path]:
        return sorted((self.root / "maintained").glob("*.json"))

    # ----- Internals --------------------------------------------------

    def _path(self, subdir: str, api_name: str) -> Path:
        """Return the file path for ``api_name`` in ``subdir``."""
        return self.root / subdir / f"{_sanitize_filename(api_name)}.json"

    def _write_json(self, path: Path, payload: Any) -> None:
        """Write ``payload`` to ``path`` using the artifact encoder."""
        # Write to a temp file and rename for atomicity. On Windows
        # ``os.replace`` performs an atomic rename even when the target
        # exists.
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, cls=_ArtifactEncoder, indent=2,
                      ensure_ascii=False)
        tmp.replace(path)

    def _read_json(self, path: Path) -> Optional[Dict]:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _append_log(self, op: str, api_name: str, details: Dict) -> None:
        """Append a single JSONL operation record."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "api_name": api_name,
            "details": details,
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Aux helpers
# ---------------------------------------------------------------------------

def _summarize_issue_counts(report: EvaluationReport) -> Dict[str, int]:
    """Tally issues by dimension and by severity for the log entry."""
    summary: Dict[str, int] = {}
    for dim, ds in report.dimensions.items():
        summary[f"dim_{dim.value}"] = len(ds.issues)
    for severity in (Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        count = 0
        for ds in report.dimensions.values():
            count += sum(1 for i in ds.issues if i.severity == severity)
        summary[f"sev_{severity.value}"] = count
    return summary
