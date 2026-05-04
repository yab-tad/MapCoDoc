"""Command-line interface for the doc_quality package.

The CLI is exposed as ``python -m doc_quality`` (see ``__main__.py``) and
wraps the evaluator, maintainer, and approval workflow with argparse
subcommands. It is intentionally thin - argument parsing only - so the
business logic stays testable without needing to spawn a process.
"""

from doc_quality.cli.main import main

__all__ = ["main"]
