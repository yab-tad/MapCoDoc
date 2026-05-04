"""Artifact storage for evaluation reports and maintenance candidates.

The ``ArtifactStore`` writes immutable snapshots of every documented
member's pre-maintenance state, the evaluation reports it produced, the
candidate post-maintenance JSON, and an append-only operation log.
Together these provide a complete provenance trail and enable rollback
of any DB writeback.
"""

from doc_quality.artifacts.store import ArtifactStore

__all__ = ["ArtifactStore"]
