"""Run the retention job: list (by default) or delete what has outlived `governance.retention_days`.

A dry run is the default and it is the plan itself - the same `plan_retention` the real run
executes - so what an operator reads here is, key for key, what `--apply` deletes (DEC-736). Nothing
is deleted without `--apply`.

    python -m scripts.run_retention                 # dry run: what would be deleted, and why
    python -m scripts.run_retention --json          # the same, as JSON for a ticket or a diff
    python -m scripts.run_retention --apply         # delete it, and write one audit event

The job acts as `engine.privacy.retention.RETENTION_JOB` (an Admin task run by the system). With
`--apply` it writes one `privacy.retention.apply` event to the platform database's audit log; that
event carries counts, never a key or a value. It is meant to be run on a schedule (daily is plenty:
retention is measured in days); the S3 lifecycle rules of `engine.privacy.lifecycle` are only a
backstop behind it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.audit.events import AuditLog
from engine.config import ConfigError
from engine.privacy.retention import RETENTION_JOB, apply_retention, plan_retention, plan_summary
from engine.registry import to_utc
from engine.settings import Settings, SettingsError, build_storage, load_settings
from engine.utils.logging import configure_logging

if TYPE_CHECKING:
    from engine.clients import ClientStore

__all__ = ["main"]

COMMAND: str = "python -m scripts.run_retention"
EXIT_OK: int = 0
EXIT_CONFIG: int = 2
"""The settings or the configuration could not be read; nothing was planned or deleted."""

CLIENTS_DB_FILENAME: str = "clients.db"


def build_parser() -> argparse.ArgumentParser:
    """`--apply` to delete; the default is a dry run."""
    parser = argparse.ArgumentParser(prog=COMMAND, description="Plan or apply the data-retention job.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", default=True, help="List what would be deleted (default)."
    )
    mode.add_argument("--apply", action="store_true", help="Delete what the plan lists and audit it.")
    parser.add_argument("--json", action="store_true", help="Print the plan (and result) as JSON.")
    parser.add_argument("--now", default=None, help="ISO time to plan as of (default: now). For rehearsals.")
    parser.add_argument("--config-dir", default=None, help="Configuration root (default: the deployment's).")
    return parser


def main(argv: Sequence[str] | None = None, *, audit_log: AuditLog | None = None) -> int:
    """Plan, and with `--apply` execute, one retention pass over the deployment's store."""
    args = build_parser().parse_args(argv)
    configure_logging()
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(exc.message, file=sys.stderr)
        return EXIT_CONFIG
    storage = build_storage(settings)
    config_root = Path(args.config_dir) if args.config_dir else settings.config_dir
    now = to_utc(datetime.fromisoformat(args.now)) if args.now else None  # a naive time is read as UTC
    client_store = _client_store(settings.data_dir) if settings.storage_backend == "local" else None
    try:
        plan = plan_retention(storage, config_root, now, client_store=client_store)
    except ConfigError as exc:
        print(exc.message, file=sys.stderr)
        return EXIT_CONFIG
    summary = plan_summary(plan)
    if not args.apply:
        _print(summary, as_json=args.json, verb="would delete")
        return EXIT_OK
    log = audit_log if audit_log is not None else _audit_log(settings)
    result = apply_retention(plan, storage, log, RETENTION_JOB, client_store=client_store)
    summary["result"] = result.model_dump(mode="json")
    _print(summary, as_json=args.json, verb="deleted")
    return EXIT_OK


def _print(summary: dict[str, Any], *, as_json: bool, verb: str) -> None:
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return
    counts = summary["counts"]
    total = sum(counts.values()) if isinstance(counts, dict) else 0
    print(f"retention plan {summary['plan_id']} as of {summary['planned_at']}: {verb} or strip {total} keys")
    if isinstance(counts, dict):
        for category, count in sorted(counts.items()):
            print(f"  {category}: {count}")
    keys = summary["keys"]
    for key in keys if isinstance(keys, list) else []:
        print(f"  - {key}")


def _client_store(data_dir: Path) -> ClientStore | None:
    """The onboarding client store beside the artefacts, when this deployment has one."""
    path = data_dir / CLIENTS_DB_FILENAME
    if not path.is_file():
        return None
    from engine.clients import LocalClientStore

    return LocalClientStore(path)


def _audit_log(settings: Settings) -> AuditLog:
    """The platform database's audit log (Phase 4b M47), imported only when a run really deletes."""
    from engine.audit.store import SqlAuditLog
    from engine.platform_db import platform_engine

    return SqlAuditLog(platform_engine(settings))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
