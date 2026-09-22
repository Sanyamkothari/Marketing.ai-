"""Rebuild the run index from the run documents, which are the only authoritative thing there is.

The run index exists so a list page does not have to read every run in the bucket, and it is
written best-effort *after* `run.json`: a database that is down when a run finishes costs that run
its row and nothing else (DEC-342). This command is the other half of that bargain. It walks
`runs/*/run.json`, upserts a row for each, and drops rows whose document is gone.

Run it after an outage, after a restore, or the first time a deployment gets a database over a
bucket that already has runs in it. It is idempotent, so running it when nothing is wrong is
harmless; it is also a full listing of the runs prefix, which is precisely the expensive operation
the index removes from the request path, so it is a command somebody runs and never a thing that
happens on its own.

`--dry-run` reports what a rebuild would do without writing a row - the same scan, an index that
refuses every write - so an operator can see the size of the job first.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from engine.aws.run_index import (
    RUNS_PREFIX,
    ReconcileReport,
    RunIndex,
    RunIndexEntry,
    reconcile_runs,
)
from engine.config import RunMode
from engine.settings import MetadataBackend, Settings, SettingsError, build_storage
from engine.utils.logging import configure_logging

__all__ = ["DryRunIndex", "main"]

COMMAND: str = "python -m scripts.reconcile_runs"
"""How this is invoked; quoted in the messages so a reader can copy the fix."""

EXIT_OK: int = 0
EXIT_NO_INDEX: int = 2
"""There is no table to rebuild. Not a crash and not a success: a deployment shaped differently."""

EXIT_INCOMPLETE: int = 3
"""The scan finished but some documents could not be read or some rows could not be written."""


class DryRunIndex:
    """A `RunIndex` that reads the real one and writes nothing, for `--dry-run`.

    It delegates the reads so the report is about the *actual* index - which rows exist, and
    therefore which would be forgotten - and swallows the writes, counting them instead.
    """

    def __init__(self, inner: RunIndex) -> None:
        self._inner = inner
        self.upserts: int = 0
        self.forgets: int = 0

    def upsert(self, entry: RunIndexEntry) -> None:
        """Count the row this would have written."""
        del entry
        self.upserts += 1

    def get(self, run_id: str) -> RunIndexEntry | None:
        """The real index's answer; a dry run still reads the truth."""
        return self._inner.get(run_id)

    def list_run_ids(
        self, *, limit: int, use_case_id: str | None = None, mode: RunMode | None = None
    ) -> tuple[str, ...]:
        """Delegated unchanged; `reconcile_runs` does not call it, but the protocol requires it."""
        return self._inner.list_run_ids(limit=limit, use_case_id=use_case_id, mode=mode)

    def run_ids(self) -> tuple[str, ...]:
        """The rows that are really there, so `--dry-run` can report what would be dropped."""
        return self._inner.run_ids()

    def forget(self, run_id: str) -> None:
        """Count the row this would have dropped."""
        del run_id
        self.forgets += 1


def build_parser() -> argparse.ArgumentParser:
    """The command line: which prefix, and whether to write."""
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description="Rebuild the run index from runs/*/run.json.",
    )
    parser.add_argument(
        "--prefix",
        default=RUNS_PREFIX,
        help=f"Storage prefix the run directories live under (default {RUNS_PREFIX!r}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report without writing or dropping a single row.",
    )
    return parser


def render(report: ReconcileReport, *, dry_run: bool) -> str:
    """One line an operator can read, and a second one only when something went wrong."""
    verb = "would index" if dry_run else "indexed"
    forget = "would forget" if dry_run else "forgot"
    line = (
        f"scanned {report.scanned} run documents, {verb} {report.indexed}, "
        f"{forget} {report.forgotten} rows with no document"
    )
    if report.unreadable or report.failed:
        line += (
            f"\n{report.unreadable} documents could not be read and {report.failed} rows could not "
            f"be written; the reasons are in the log. Fix them and run `{COMMAND}` again."
        )
    return line


def main(argv: Sequence[str] | None = None) -> int:
    """Rebuild the index described by the environment; see the module docstring."""
    args = build_parser().parse_args(argv)
    configure_logging()
    try:
        settings = Settings.load()
    except SettingsError as exc:
        print(exc.message, file=sys.stderr)
        return EXIT_NO_INDEX
    if settings.metadata_backend is not MetadataBackend.POSTGRES:
        print(
            f"metadata_backend={settings.metadata_backend.value} keeps no run index: the run "
            "documents in storage are the whole history, and there is nothing to rebuild.",
            file=sys.stderr,
        )
        return EXIT_NO_INDEX

    from engine.aws.postgres import postgres_run_index

    storage = build_storage(settings)
    index: RunIndex = postgres_run_index(settings)
    if args.dry_run:
        index = DryRunIndex(index)
    report = reconcile_runs(storage, index, prefix=args.prefix)
    print(render(report, dry_run=args.dry_run))
    return EXIT_INCOMPLETE if (report.unreadable or report.failed) else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
