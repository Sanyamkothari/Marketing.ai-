"""The retention job (M48 item 2): `governance.retention_days` becomes a deadline that is kept.

Since DEC-074 the setting was recorded with every run and read by nothing. This module reads it.
`plan_retention` works out what has outlived it, `apply_retention` deletes exactly that, and the two
are separate on purpose: **a dry run is the plan**, so what an operator reviews is, key for key, what
the real run deletes (`tests/unit/production/test_retention.py` proves the two equal, DEC-736).

**What goes.** Uploads (the file and everything in its directory), onboarding raw sources (the file
and its profile, plus the source's row in the client store when one is passed), built datasets (the
whole directory), and the **row-level** artefacts of finished runs - the files listed under
`retention.row_level_run_artefacts` in `configs/privacy.yaml` (scores, row explanations, copy
messages). **What stays.** Models, `run.json`, `status.json`, manifests and every aggregate report:
the Results page of a run a year old still renders, it just no longer offers the scores file.
Aggregate reports that carry a few example rows (`profile.json`'s samples, `scoring_summary.json`'s
sample rows) are kept with those fields emptied, per `retention.sample_fields`.

**What "older than" means.** Every item is dated from its owner's own record - `created_at` of
`upload.json` and `run.json`, `built_at` of a dataset manifest, the source row's `created_at` - never
from a file's modification time, which a copy, a restore or an S3 sync resets. Something that cannot
be dated is not deleted; it is listed under `skipped` as `UNDATED` for a person to look at.

**Which setting applies.** A run: its use case's current value, or the value in its own
`run_config.json` when that is *shorter* - a per-run override can shorten retention, never extend
it, because retention is the Admin's setting and run overrides are the Analyst's (DEC-744). An upload or a dataset: the *largest* of its use case's value and
the value of every run that read it, because deleting an input earlier than any of its runs promised
would break that promise. A source feeds datasets of several use cases, so it takes the largest value
among the datasets built from it, or the engine default when none has been built.

**Zero.** The help text of `governance.retention_days` says "0 = delete uploads right after the run".
So with 0 an input is due as soon as at least one finished run has read it - not the moment it is
uploaded, which would delete a file while its owner is still on the Setup screen - or, if no run
ever reads it, once `ZERO_DAYS_GRACE` has passed; a finished run's row-level artefacts are due at
once.

**In use.** An input read by a run that has not finished is never scheduled (`IN_USE`), whatever its
age: deleting it would fail that run for a reason nobody could see.

This job is the enforcement; the S3 lifecycle rules of `engine.privacy.lifecycle` are only a
backstop behind it (DEC-740).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from engine.access.roles import Principal, Role
from engine.audit.events import AuditEvent, AuditLog
from engine.clients import ClientStoreError
from engine.config import load_all_use_cases, load_engine_config
from engine.privacy.config import PrivacyConfig, load_privacy_config
from engine.privacy.contracts import (
    RetentionAction,
    RetentionCategory,
    RetentionItem,
    RetentionPlan,
    RetentionResult,
    RetentionSkip,
)
from engine.privacy.layout import StoreIndex, parse_time, purge_old_versions, read_json
from engine.storage import Storage, StorageError
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.clients import ClientStore

__all__ = [
    "RETENTION_JOB",
    "apply_retention",
    "plan_retention",
    "plan_summary",
    "retention_days_by_use_case",
    "strip_fields",
]

_LOGGER = get_logger(__name__)

RETENTION_JOB: Final[Principal] = Principal(
    user_id="system:retention",
    username="retention job",
    roles=frozenset({Role.ADMIN}),
    kind="system",
)
"""Who the scheduled retention job acts as in the audit trail: an Admin task (plan M46), run by the system."""

ZERO_DAYS_GRACE: Final[timedelta] = timedelta(days=1)
"""With `retention_days: 0`, how long an input nobody has run yet is kept - long enough for its owner
to finish the Setup screen, short enough that 0 never means "forever" (DEC-744)."""

_FALLBACK_DAYS: Final[int] = 90
"""Only if `engine.yaml` itself has no `governance.retention_days` default, which a valid file always has."""


def retention_days_by_use_case(config_root: Path | None = None) -> tuple[dict[str, int], int]:
    """`(use-case id -> governance.retention_days, the engine default)` for one config root."""
    defaults = load_engine_config(config_root).defaults
    governance = defaults.get("governance")
    default = (
        int(governance.get("retention_days", _FALLBACK_DAYS))
        if isinstance(governance, dict)
        else _FALLBACK_DAYS
    )
    days = {
        use_case_id: config.governance.retention_days
        for use_case_id, config in load_all_use_cases(config_root).items()
    }
    return days, default


def plan_retention(
    storage: Storage,
    config_root: Path | None = None,
    now: datetime | None = None,
    *,
    client_store: ClientStore | None = None,
) -> RetentionPlan:
    """Everything due for deletion (or sample stripping) at `now`. Reads; writes nothing."""
    moment = now or utc_now()
    privacy = load_privacy_config(config_root)
    by_use_case, default_days = retention_days_by_use_case(config_root)
    index = StoreIndex(storage)
    keys = index.keys
    present = frozenset(keys)
    items: list[RetentionItem] = []
    skipped: list[RetentionSkip] = []
    run_days = {
        run_id: _run_days(storage, run_id, run.use_case_id, by_use_case, default_days)
        for run_id, run in index.runs.items()
    }
    in_use_uploads = {run.upload_id for run in index.runs.values() if not run.terminal and run.upload_id}
    in_use_datasets = {run.dataset_id for run in index.runs.values() if not run.terminal and run.dataset_id}

    # -- finished runs: row-level artefacts, and samples inside kept reports ---------------------
    for run_id, run in sorted(index.runs.items()):
        if not run.terminal:
            continue
        if run.created_at is None:
            skipped.append(
                RetentionSkip(
                    owner_id=run_id, category=RetentionCategory.RUN_ROW_LEVEL, reason_code="UNDATED"
                )
            )
            continue
        days = run_days[run_id]
        if not _due(run.created_at, days, moment, consumed=True):
            continue
        common: dict[str, Any] = {
            "owner_id": run_id,
            "use_case_id": run.use_case_id,
            "client_id": run.client_id,
            "created_at": run.created_at,
            "retention_days": days,
        }
        for name in privacy.retention.row_level_run_artefacts:
            key = f"runs/{run_id}/{name}"
            if key in present:
                items.append(
                    RetentionItem(
                        key=key,
                        category=RetentionCategory.RUN_ROW_LEVEL,
                        action=RetentionAction.DELETE,
                        **common,
                    )
                )
        for name, paths in sorted(privacy.retention.sample_fields.items()):
            key = f"runs/{run_id}/{name}"
            if key in present and _has_samples(storage, key, paths):
                items.append(
                    RetentionItem(
                        key=key,
                        category=RetentionCategory.RUN_SAMPLES,
                        action=RetentionAction.STRIP_SAMPLES,
                        fields=paths,
                        **common,
                    )
                )

    # -- uploads ------------------------------------------------------------------------------
    for upload_id, upload in sorted(index.uploads.items()):
        readers = index.runs_reading_upload(upload_id)
        if upload_id in in_use_uploads:
            skipped.append(
                RetentionSkip(owner_id=upload_id, category=RetentionCategory.UPLOAD, reason_code="IN_USE")
            )
            continue
        if upload.created_at is None:
            skipped.append(
                RetentionSkip(owner_id=upload_id, category=RetentionCategory.UPLOAD, reason_code="UNDATED")
            )
            continue
        days = max(
            [by_use_case.get(upload.use_case_id or "", default_days), *(run_days[r.run_id] for r in readers)]
        )
        if _due(upload.created_at, days, moment, consumed=any(r.terminal for r in readers)):
            items.extend(
                RetentionItem(
                    key=key,
                    category=RetentionCategory.UPLOAD,
                    action=RetentionAction.DELETE,
                    owner_id=upload_id,
                    use_case_id=upload.use_case_id,
                    created_at=upload.created_at,
                    retention_days=days,
                )
                for key in _under(keys, f"uploads/{upload_id}/")
            )

    # -- built datasets ----------------------------------------------------------------------
    dataset_days: dict[str, int] = {}
    for dataset_id, dataset in sorted(index.datasets.items()):
        readers = index.runs_reading_dataset(dataset_id)
        days = max(
            [by_use_case.get(dataset.use_case_id or "", default_days), *(run_days[r.run_id] for r in readers)]
        )
        dataset_days[dataset_id] = days
        if dataset_id in in_use_datasets:
            skipped.append(
                RetentionSkip(owner_id=dataset_id, category=RetentionCategory.DATASET, reason_code="IN_USE")
            )
            continue
        if dataset.created_at is None:
            skipped.append(
                RetentionSkip(owner_id=dataset_id, category=RetentionCategory.DATASET, reason_code="UNDATED")
            )
            continue
        if _due(dataset.created_at, days, moment, consumed=any(r.terminal for r in readers)):
            items.extend(
                RetentionItem(
                    key=key,
                    category=RetentionCategory.DATASET,
                    action=RetentionAction.DELETE,
                    owner_id=dataset_id,
                    use_case_id=dataset.use_case_id,
                    client_id=dataset.client_id,
                    created_at=dataset.created_at,
                    retention_days=days,
                )
                for key in _under(keys, f"datasets/{dataset_id}/")
            )

    # -- onboarding raw sources --------------------------------------------------------------
    items.extend(
        _source_items(storage, keys, index, dataset_days, default_days, moment, skipped, client_store)
    )

    plan = RetentionPlan(
        plan_id=f"ret_{uuid.uuid4().hex[:16]}",
        planned_at=moment,
        items=tuple(sorted(items, key=lambda item: item.key)),
        skipped=tuple(sorted(skipped, key=lambda skip: (skip.category.value, skip.owner_id))),
    )
    _LOGGER.info(
        "retention.plan items=%d skipped=%d counts=%s",
        len(plan.items),
        len(plan.skipped),
        _counts_text(plan.counts()),
    )
    return plan


def apply_retention(
    plan: RetentionPlan,
    storage: Storage,
    audit_log: AuditLog | None,
    principal: Principal,
    *,
    client_store: ClientStore | None = None,
    now: datetime | None = None,
) -> RetentionResult:
    """Execute `plan` exactly: delete its `delete` keys, empty its `strip_samples` fields, audit once.

    Nothing is re-planned here, so what was reviewed as a dry run is what happens. A planned key that
    has meanwhile disappeared is reported as `already_gone`, not treated as an error. When a client
    store is given, a deleted source's registry row goes with its raw file, so the mapping screen
    does not list a file that no longer exists. One audit event (`privacy.retention.apply`) records
    the counts; it holds no key and no value.
    """
    moment = now or utc_now()
    deleted: list[str] = []
    stripped: list[str] = []
    gone: list[str] = []
    rows_deleted = 0
    for item in plan.items:
        if not storage.exists(item.key):
            gone.append(item.key)
            continue
        if item.action is RetentionAction.DELETE:
            storage.delete(item.key)
            purge_old_versions(storage, item.key)
            deleted.append(item.key)
            if item.category is RetentionCategory.SOURCE and client_store is not None and _is_raw(item.key):
                rows_deleted += _delete_source_row(client_store, item.owner_id)
        else:
            document = json.loads(storage.read_bytes(item.key))
            storage.write_text(
                item.key, json.dumps(strip_fields(document, item.fields), indent=2, ensure_ascii=False) + "\n"
            )
            stripped.append(item.key)
    result = RetentionResult(
        plan_id=plan.plan_id,
        applied_at=moment,
        deleted=tuple(deleted),
        stripped=tuple(stripped),
        already_gone=tuple(gone),
        registry_rows_deleted=rows_deleted,
    )
    if audit_log is not None:
        audit_log.append(
            AuditEvent(
                event_id=uuid.uuid4().hex,
                occurred_at=moment,
                actor_id=principal.user_id,
                actor_kind=principal.kind,
                action="privacy.retention.apply",
                object_type="retention_plan",
                object_id=plan.plan_id,
                outcome="success",
                details={
                    "request_kind": "retention",
                    "deleted": len(deleted),
                    "count": len(stripped),
                    "stores": _counts_text(plan.counts()),
                    "dry_run": False,
                },
            )
        )
    _LOGGER.info(
        "retention.apply plan=%s deleted=%d stripped=%d already_gone=%d",
        plan.plan_id,
        len(deleted),
        len(stripped),
        len(gone),
    )
    return result


def strip_fields(document: Any, paths: Iterable[str]) -> Any:
    """`document` with every dotted path emptied: a list becomes `[]`, anything else `None`.

    `*` matches every element of a list; a path that does not exist is ignored, so an older report
    without the field is left as it is.
    """
    for path in paths:
        _strip(document, path.split("."))
    return document


def _strip(node: Any, parts: list[str]) -> None:
    if not parts:
        return
    head, rest = parts[0], parts[1:]
    if head == "*":
        if isinstance(node, list):
            for element in node:
                _strip(element, rest)
        return
    if not isinstance(node, dict) or head not in node:
        return
    if rest:
        _strip(node[head], rest)
    else:
        node[head] = [] if isinstance(node[head], list) else None


def _has_samples(storage: Storage, key: str, paths: tuple[str, ...]) -> bool:
    """Whether stripping `paths` would change the document - so a second plan does not list it again."""
    try:
        document = json.loads(storage.read_bytes(key))
    except (StorageError, ValueError):
        return False
    before = json.dumps(document, sort_keys=True)
    return json.dumps(strip_fields(document, paths), sort_keys=True) != before


def _due(created_at: datetime, days: int, now: datetime, *, consumed: bool) -> bool:
    """Whether an item created at `created_at` has outlived `days` at `now`.

    0: once consumed, or once `ZERO_DAYS_GRACE` has passed unconsumed - the strictest setting must
    not keep an abandoned upload, dataset or source forever (DEC-744).
    """
    if days == 0:
        return consumed or now >= created_at + ZERO_DAYS_GRACE
    return now >= created_at + timedelta(days=days)


def _run_days(
    storage: Storage, run_id: str, use_case_id: str | None, by_use_case: Mapping[str, int], default: int
) -> int:
    """The retention a run's data is kept for: its use case's value, or less when it was run with less.

    The value in the run's `run_config.json` can only *shorten* the use case's: a run's settings are
    an Analyst's per-run overrides, and retention is the Admin's setting (plan M46), so a run asking
    for 730 days must not keep an upload, its dataset and its scores longer than the use case allows
    (DEC-744).
    """
    ceiling = by_use_case.get(use_case_id or "", default)
    resolved = read_json(storage, f"runs/{run_id}/run_config.json")
    if resolved is not None:
        value = resolved.get("config", {}).get("governance", {}).get("retention_days")
        if isinstance(value, int) and not isinstance(value, bool):
            return min(value, ceiling)
    return ceiling


def _under(keys: tuple[str, ...], prefix: str) -> list[str]:
    return [key for key in keys if key.startswith(prefix)]


def _is_raw(key: str) -> bool:
    return key.rsplit("/", 1)[-1].startswith("raw.")


def _source_items(
    storage: Storage,
    keys: tuple[str, ...],
    index: StoreIndex,
    dataset_days: Mapping[str, int],
    default_days: int,
    now: datetime,
    skipped: list[RetentionSkip],
    client_store: ClientStore | None,
) -> list[RetentionItem]:
    """Raw source files and their profiles whose longest-promising dataset has expired."""
    sources: dict[tuple[str, str], list[str]] = {}
    for key in keys:
        parts = key.split("/")
        if len(parts) == 5 and parts[0] == "clients" and parts[2] == "sources":
            sources.setdefault((parts[1], parts[3]), []).append(key)
    items: list[RetentionItem] = []
    for (client_id, source_id), source_keys in sorted(sources.items()):
        built = [dataset_id for dataset_id, info in index.datasets.items() if source_id in info.source_ids]
        created = _source_created_at(storage, client_id, source_id, client_store)
        if created is None:
            skipped.append(
                RetentionSkip(owner_id=source_id, category=RetentionCategory.SOURCE, reason_code="UNDATED")
            )
            continue
        days = max([dataset_days[dataset_id] for dataset_id in built], default=default_days)
        if not _due(created, days, now, consumed=bool(built)):
            continue
        items.extend(
            RetentionItem(
                key=key,
                category=RetentionCategory.SOURCE,
                action=RetentionAction.DELETE,
                owner_id=source_id,
                client_id=client_id,
                created_at=created,
                retention_days=days,
            )
            for key in sorted(source_keys)
        )
    return items


def _source_created_at(
    storage: Storage, client_id: str, source_id: str, client_store: ClientStore | None
) -> datetime | None:
    """The source's `created_at` from its registry row, else when its profile was taken."""
    if client_store is not None:
        try:
            return client_store.get_source(source_id).created_at
        except ClientStoreError:  # an unknown source falls back to its profile; it never fails the plan
            _LOGGER.info("retention: source %s has no registry row; dating it by its profile", source_id)
    profile = read_json(storage, f"clients/{client_id}/sources/{source_id}/profile.json")
    if profile is None:
        return None
    inner = profile.get("profile")
    return parse_time(inner.get("profiled_at")) if isinstance(inner, dict) else None


def _delete_source_row(client_store: ClientStore, source_id: str) -> int:
    try:
        client_store.delete_source(source_id)
    except ClientStoreError:  # a row already gone is the outcome that was wanted
        return 0
    return 1


def _counts_text(counts: Mapping[str, int]) -> str:
    """`category:n,...` - short enough for an audit detail (200 characters)."""
    return ",".join(f"{name}:{count}" for name, count in sorted(counts.items()))[:200]


def plan_summary(plan: RetentionPlan) -> dict[str, Any]:
    """A JSON-ready summary of a plan: counts by category and the keys, for the CLI and the API."""
    return {
        "plan_id": plan.plan_id,
        "planned_at": plan.planned_at.isoformat(),
        "counts": plan.counts(),
        "keys": list(plan.planned_keys()),
        "skipped": [skip.model_dump(mode="json") for skip in plan.skipped],
    }


def privacy_policy(config_root: Path | None = None) -> PrivacyConfig:
    """The privacy configuration retention reads; re-exported for the CLI."""
    return load_privacy_config(config_root)
