"""
Command-line entry point for doc_quality.

Subcommands:

* ``evaluate``  - run the evaluator over members of a library; write
                  evaluations and originals to the artifact store.
* ``maintain``  - read existing evaluations, build maintenance candidates,
                  write them to ``maintained/``.
* ``apply``     - apply an approved candidate to the database.
* ``rollback``  - restore a member's original snapshot to the database.
* ``diff``      - print a unified diff between original and candidate.
* ``report``    - generate a JSON / CSV / HTML report from the artifact store.

The CLI never invokes LLM strategies in v1 - those are deferred per the
implementation plan. ``--strategies`` may be passed to override the
default DB_QUERY + AST_DERIVED set, but the user is responsible for
ensuring the corresponding patcher is implemented.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from doc_quality.artifacts.store import ArtifactStore
from doc_quality.config import EvaluatorConfig
from doc_quality.evaluator import DocQualityEvaluator
from doc_quality.maintainer import DocQualityMaintainer
from doc_quality.maintainer.approval import ApprovalWorkflow
from doc_quality.models import EvaluationReport, MaintainerStrategy, Severity


logger = logging.getLogger("doc_quality")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser with all subcommands."""
    p = argparse.ArgumentParser(
        prog="doc_quality",
        description=(
            "Documentation Quality Evaluation and Targeted Maintenance for "
            "MapCoDoc structured documentation."
        ),
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )

    sub = p.add_subparsers(dest="command", required=True)

    # ---- evaluate ---------------------------------------------------
    e = sub.add_parser(
        "evaluate",
        help="Run the evaluator over a library; write artifacts.",
    )
    _add_db_args(e)
    _add_target_args(e)
    e.add_argument("--include-inherited", action="store_true",
                   help="Also evaluate inherited members.")
    e.add_argument("--config", type=Path, default=None,
                   help="Path to a YAML EvaluatorConfig file.")
    e.set_defaults(func=_cmd_evaluate)

    # ---- maintain ---------------------------------------------------
    m = sub.add_parser(
        "maintain",
        help="Build maintenance candidates from existing evaluations.",
    )
    _add_db_args(m)
    _add_target_args(m)
    m.add_argument("--strategies", type=str,
                   default="db_query,ast_derived",
                   help="Comma-separated maintainer strategies to enable.")
    m.add_argument("--min-severity", type=str, default="medium",
                   choices=["low", "medium", "high"],
                   help="Minimum issue severity to attempt maintaining.")
    m.add_argument("--config", type=Path, default=None,
                   help="Path to a YAML EvaluatorConfig file.")
    m.set_defaults(func=_cmd_maintain)

    # ---- apply ------------------------------------------------------
    ap = sub.add_parser(
        "apply",
        help="Apply an approved candidate to the database.",
    )
    _add_db_args(ap)
    _add_artifact_args(ap)
    ap.add_argument("--api-name", required=True,
                    help="API name of the member to apply.")
    ap.set_defaults(func=_cmd_apply)

    # ---- rollback ---------------------------------------------------
    rb = sub.add_parser(
        "rollback",
        help="Restore the original snapshot of a member to the database.",
    )
    _add_db_args(rb)
    _add_artifact_args(rb)
    rb.add_argument("--api-name", required=True,
                    help="API name of the member to roll back.")
    rb.set_defaults(func=_cmd_rollback)

    # ---- diff -------------------------------------------------------
    df = sub.add_parser(
        "diff",
        help="Print a unified diff of original vs candidate.",
    )
    _add_artifact_args(df)
    df.add_argument("--api-name", required=True,
                    help="API name of the member to diff.")
    df.set_defaults(func=_cmd_diff)

    # ---- report -----------------------------------------------------
    rp = sub.add_parser(
        "report",
        help="Generate a JSON/CSV/HTML report from existing artifacts.",
    )
    _add_artifact_args(rp)
    rp.add_argument("--format", choices=["json", "csv", "html"], default="json",
                    help="Output format (default: json).")
    rp.add_argument("--output-file", type=Path, default=None,
                    help="Write to file instead of stdout.")
    rp.set_defaults(func=_cmd_report)

    return p


def _add_db_args(p: argparse.ArgumentParser) -> None:
    """Add DB-connection arguments common to evaluate/maintain/apply."""
    p.add_argument("--db-path", type=Path, required=True,
                   help="Path to the MapCoDoc SQLite database.")


def _add_artifact_args(p: argparse.ArgumentParser) -> None:
    """Add artifact-store arguments."""
    p.add_argument("--library", required=True,
                   help="Library name (e.g. 'sklearn').")
    p.add_argument("--version", required=True,
                   help="Library version string used in the artifact path.")
    p.add_argument("--output-root", type=Path,
                   default=Path("doc_quality/artifacts"),
                   help="Root directory for artifact storage.")


def _add_target_args(p: argparse.ArgumentParser) -> None:
    """Add per-member targeting arguments to evaluate/maintain commands."""
    _add_artifact_args(p)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--api-name", help="Evaluate only this API name.")
    g.add_argument("--module-prefix",
                   help="Evaluate every member whose api_name starts with this prefix.")


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_evaluate(args: argparse.Namespace) -> int:
    """Handle ``doc_quality evaluate``."""
    qm, session = _open_db(args.db_path)
    try:
        store = ArtifactStore(args.output_root, args.library, args.version)
        cfg = (
            EvaluatorConfig.from_yaml(args.config)
            if args.config else EvaluatorConfig()
        )
        evaluator = DocQualityEvaluator(qm, config=cfg, artifact_store=store)

        members = _select_members(qm, args, include_inherited=args.include_inherited)
        if not members:
            print("No structured-doc members matched the selection.", file=sys.stderr)
            return 1

        total = len(members)
        print(f"Evaluating {total} member(s)...", file=sys.stderr)

        def progress(done: int, _total: int) -> None:
            # Lightweight stderr progress; suitable for piping output.
            print(f"  [{done}/{_total}]", file=sys.stderr)

        reports = evaluator.evaluate_batch(members, progress_callback=progress)

        # Write a quick summary to stdout for scripting.
        skipped = sum(1 for r in reports if r.skipped)
        print(json.dumps({
            "evaluated": total,
            "skipped": skipped,
            "artifacts_root": str(store.root),
        }, indent=2))
        return 0
    finally:
        session.close()


def _cmd_maintain(args: argparse.Namespace) -> int:
    """Handle ``doc_quality maintain``."""
    qm, session = _open_db(args.db_path)
    try:
        store = ArtifactStore(args.output_root, args.library, args.version)

        # Build config: parse strategies + min_severity from CLI.
        cfg = (
            EvaluatorConfig.from_yaml(args.config)
            if args.config else EvaluatorConfig()
        )
        cfg.enabled_strategies = [
            MaintainerStrategy(s.strip()) for s in args.strategies.split(",")
            if s.strip()
        ]
        cfg.min_severity_for_maintenance = Severity(args.min_severity)

        maintainer = DocQualityMaintainer(qm, config=cfg, artifact_store=store)

        # Need an evaluator too: maintenance requires a fresh report
        # because the artifact-store-stored evaluations are JSON dicts,
        # not typed dataclasses.
        evaluator = DocQualityEvaluator(qm, config=cfg, artifact_store=store)

        members = _select_members(
            qm, args,
            include_inherited=getattr(args, "include_inherited", False),
        )
        if not members:
            print("No structured-doc members matched the selection.", file=sys.stderr)
            return 1

        total = len(members)
        print(f"Maintaining {total} member(s)...", file=sys.stderr)

        candidate_count = 0
        for i, m in enumerate(members, 1):
            report = evaluator.evaluate_one(m)
            if report.skipped:
                continue
            api_ref = store.load_original(report.member_api_name)
            if api_ref is None:
                # Should not happen because evaluate_one stamps the original;
                # log and continue defensively.
                logger.warning(
                    "No original snapshot for %s; skipping.",
                    report.member_api_name,
                )
                continue
            candidate = maintainer.maintain_one(report, api_ref)
            if candidate.patches:
                candidate_count += 1
            if i % 25 == 0 or i == total:
                print(f"  [{i}/{total}] candidates with patches: {candidate_count}",
                      file=sys.stderr)

        print(json.dumps({
            "evaluated": total,
            "candidates_with_patches": candidate_count,
            "artifacts_root": str(store.root),
        }, indent=2))
        return 0
    finally:
        session.close()


def _cmd_apply(args: argparse.Namespace) -> int:
    """Handle ``doc_quality apply``."""
    qm, session = _open_db(args.db_path)
    try:
        store = ArtifactStore(args.output_root, args.library, args.version)
        workflow = ApprovalWorkflow(store, session)
        workflow.approve(args.api_name)
        print(json.dumps({"applied": args.api_name}, indent=2))
        return 0
    finally:
        session.close()


def _cmd_rollback(args: argparse.Namespace) -> int:
    """Handle ``doc_quality rollback``."""
    qm, session = _open_db(args.db_path)
    try:
        store = ArtifactStore(args.output_root, args.library, args.version)
        workflow = ApprovalWorkflow(store, session)
        workflow.rollback(args.api_name)
        print(json.dumps({"rolled_back": args.api_name}, indent=2))
        return 0
    finally:
        session.close()


def _cmd_diff(args: argparse.Namespace) -> int:
    """Handle ``doc_quality diff``."""
    # Diff uses the artifact store directly; no DB session needed. We
    # still construct an ApprovalWorkflow for its show_diff method, but
    # without a session - so we avoid touching the DB.
    store = ArtifactStore(args.output_root, args.library, args.version)
    # Minimal stand-in: ApprovalWorkflow's show_diff doesn't use ``session``.
    workflow = ApprovalWorkflow(store, session=None)  # type: ignore[arg-type]
    print(workflow.show_diff(args.api_name))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    """Handle ``doc_quality report``.

    Loads existing evaluation artifacts and renders an aggregate report.
    Does not re-evaluate; stale artifacts produce stale reports.
    """
    from doc_quality.reporting import (
        aggregate_reports,
        format_csv,
        format_html,
        format_json,
    )
    store = ArtifactStore(args.output_root, args.library, args.version)
    # Load all evaluations as raw dicts and reconstruct minimal report
    # objects for aggregation. We don't need full fidelity - aggregation
    # only consumes ``overall_score``, ``dimensions``, and ``skipped``.
    reports: List[EvaluationReport] = []
    for path in store.iter_evaluations():
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        reports.append(_inflate_report(payload))

    aggregate = aggregate_reports(
        reports, library=args.library, version=args.version,
    )

    if args.format == "json":
        rendered = format_json(reports, aggregate)
    elif args.format == "csv":
        rendered = format_csv(reports)
    else:
        rendered = format_html(reports, aggregate)

    if args.output_file:
        args.output_file.parent.mkdir(parents=True, exist_ok=True)
        args.output_file.write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.format.upper()} report to {args.output_file}")
    else:
        sys.stdout.write(rendered)
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_db(db_path: Path):
    """Open a SQLAlchemy session against the SQLite DB at ``db_path``.

    Returns a ``(QueryManager, session)`` tuple. Imported lazily so the
    CLI module can be imported without sqlalchemy installed.
    """
    if not db_path.exists():
        print(f"Database file not found: {db_path}", file=sys.stderr)
        raise SystemExit(2)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from mapcodoc_db.query import QueryManager

    engine = create_engine(f"sqlite:///{db_path}")
    Session = sessionmaker(bind=engine)
    session = Session()
    return QueryManager(session), session


def _select_members(qm, args: argparse.Namespace,
                    include_inherited: bool = False) -> List:
    """Resolve --api-name / --module-prefix into a member list."""
    members: List = []
    if getattr(args, "api_name", None):
        # Single-member selection - must be a direct DBMember row.
        details = qm.get_member_by_any_api_name(args.api_name)
        if details:
            members.append(details)
    elif getattr(args, "module_prefix", None):
        members.extend(
            qm.get_members_by_doc_format("structured", args.module_prefix),
        )
    else:
        # Default: all structured-doc members in the DB.
        members.extend(qm.get_members_by_doc_format("structured"))

    if include_inherited:
        # Pull inherited members with structured docs, restricted to the
        # same prefix when one was given.
        from mapcodoc_db.db_models import DBInheritedMember, DBMember
        q = qm.session.query(DBInheritedMember).filter(
            DBInheritedMember.doc_format == "structured",
        )
        if getattr(args, "module_prefix", None):
            prefix = args.module_prefix
            q = q.filter(
                DBInheritedMember.inherited_api_name.like(f"{prefix}%"),
            )
        # For each, build a lightweight detail object the evaluator can use.
        for row in q.all():
            details = qm._inherited_to_details(row)  # internal but stable
            members.append(details)

    return members


def _inflate_report(payload: dict) -> EvaluationReport:
    """Reconstruct an ``EvaluationReport`` from a JSON-decoded dict.

    Used by the report command. Lossy in the sense that ``IssueType``
    instances are recovered via ``IssueType.by_code``, and timestamps
    parsed from ISO 8601 strings.
    """
    from datetime import datetime, timezone
    from doc_quality.issue_types import IssueType
    from doc_quality.models import (
        Dimension,
        DimensionScore,
        Issue,
        MaintainerStrategy,
        Severity,
    )

    dimensions = {}
    # Dimension keys may have been serialized as the enum value (string)
    # or the name; handle both defensively.
    for k, ds_payload in payload.get("dimensions", {}).items():
        # Normalize to enum.
        try:
            dim = Dimension(k)
        except ValueError:
            dim = Dimension(k.lower())
        issues = []
        for i in ds_payload.get("issues", []) or []:
            try:
                it = IssueType.by_code(i["issue_type"])
            except (KeyError, ValueError):
                # Unknown issue type from a newer artifact - skip.
                continue
            issues.append(Issue(
                issue_type=it,
                dimension=Dimension(i.get("dimension", dim.value)),
                severity=Severity(i.get("severity", "low")),
                section=i.get("section", ""),
                target=i.get("target"),
                json_path=i.get("json_path", ""),
                detail=i.get("detail", ""),
                code_value=i.get("code_value"),
                doc_value=i.get("doc_value"),
                maintainer_strategy=MaintainerStrategy(
                    i.get("maintainer_strategy", "manual"),
                ),
                metadata=i.get("metadata", {}) or {},
            ))
        dimensions[dim] = DimensionScore(
            score=ds_payload.get("score", 0.0),
            issues=issues,
            metric_breakdown=ds_payload.get("metric_breakdown", {}) or {},
        )
    ts_str = payload.get("evaluation_timestamp")
    ts = (
        datetime.fromisoformat(ts_str) if ts_str else datetime.now(timezone.utc)
    )
    return EvaluationReport(
        member_id=payload.get("member_id", -1),
        member_fqn=payload.get("member_fqn", ""),
        member_api_name=payload.get("member_api_name", ""),
        member_type=payload.get("member_type", ""),
        is_inherited=payload.get("is_inherited", False),
        code_truth_available=payload.get("code_truth_available", False),
        overall_score=payload.get("overall_score", 0.0),
        dimensions=dimensions,
        evaluation_timestamp=ts,
        schema_version=payload.get("schema_version", "1.0"),
        skipped=payload.get("skipped", False),
        skip_reason=payload.get("skip_reason"),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Configure logging based on the requested level.
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Dispatch to the chosen subcommand. ``func`` is set via
    # ``set_defaults`` on each subparser.
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
