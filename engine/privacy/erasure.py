"""Erasure requests (M48 item 3): find every place a data principal's id is held, and remove it.

**Find.** `find_principal` reads every artefact in the store - uploads (the file, its profile's
samples and preview), onboarding sources, built datasets and their samples, scores, row
explanations, copy messages, every other run document, knowledge indexes, the LLM completion cache
and the model directories - and asks `engine.privacy.rewrite` whether the id is in it. A tabular
file is matched on its primary-key column(s), read from the file's own record through
`engine.privacy.layout.StoreIndex`; JSON and text are matched on exact values and exact token
occurrences. The whole store is read rather than a list of "known" files, because the list is what
goes stale: an artefact a later phase adds is covered on the day it is written (DEC-741).

**Erase.** Each file is rewritten *in its own format* (CSV stays CSV with its delimiter, Parquet
keeps its schema, a `profile.json` still validates) with the principal's rows deleted or tombstoned
per `erasure.mode` in `configs/privacy.yaml` (DEC-742). An LLM cache entry that mentions the id is
deleted outright: a cache is by definition re-derivable, and editing a cached completion would make
it return an answer no model gave. A binary file (a pickled model) cannot be rewritten; it is
reported under `unrewritable_keys` and its model is flagged, which is what the next item is for.
After rewriting, every rewritten file is scanned again, and a file that still holds the id fails the
request (`ERASURE_INCOMPLETE`) rather than reporting success.

**Models.** A model version whose training run read an upload, dataset or source that held the
person is flagged in `model_retrain_flag`. It is **not** retrained here and not retrained now: the
flag is read by the scheduler (`models_flagged_for_retraining`) and the model is retrained at the
**next scheduled retraining cycle**, which produces a challenger that still goes through the
champion rule and human approval; `clear_flag` is called when that challenger exists (DEC-743).
Retraining inside an erasure request would put a model fit - minutes to hours, and a champion
decision - inside an HTTP request, and could crown a model nobody approved.

**Record.** The request is written to `erasure_request` with the principal as `principal_hash`
only, the per-store counts and the flagged models. When an `AuditLog` is passed (a job, a script),
one `privacy.erasure` event is appended; an API route passes none and enriches the single event the
audit middleware writes with `erasure_audit_details(outcome)` instead, so a request never produces
two events (the Phase 4b audit rule).

What this cannot reach, stated rather than implied: copies outside the store (a scores file a user
downloaded, an S3 object version kept by bucket versioning until the lifecycle backstop's
`NoncurrentVersionExpiration` removes it, a database backup) and a model's learned parameters.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final

from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from engine.access.roles import Principal
from engine.audit.events import AuditEvent, AuditLog, content_hash, principal_hash
from engine.privacy.config import ErasureMode, load_privacy_config
from engine.privacy.consent import ConsentLedger, principal_key
from engine.privacy.contracts import (
    ErasureOutcome,
    ErasureRequestRecord,
    PrincipalFindings,
    PrincipalLocation,
    RetrainFlag,
    StoreCount,
    StoreProgress,
)
from engine.privacy.errors import PrivacyError, require_principal_id
from engine.privacy.layout import Store, StoreIndex, is_artefact_key, purge_old_versions, store_of
from engine.privacy.rewrite import FileKind, Matcher, file_kind, rewrite_bytes, scan_bytes
from engine.privacy.tables import (
    ErasureProgressRow,
    ErasureRequestRow,
    ModelRetrainFlagRow,
    create_privacy_tables,
)
from engine.registry import aware_utc
from engine.storage import Storage, StorageError
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "clear_flag",
    "erase",
    "erasure_audit_details",
    "erasure_request",
    "erasure_requests",
    "fail_if_unfinished",
    "fail_interrupted",
    "find_principal",
    "models_flagged_for_retraining",
    "queue_request",
    "requeue_failed",
    "retrain_flags",
]

_LOGGER = get_logger(__name__)

_CHUNK: Final[int] = 1 << 20
INTERRUPTED: Final[str] = "ERASURE_INTERRUPTED"
"""The error code of a request the API found unfinished when it started (the process stopped mid-job)."""

_UNAVAILABLE: Final[str] = "training_input_unavailable"
"""`model_retrain_flag.reason` of a model whose training inputs are gone (exposure unknown, DEC-709)."""

_KEYED: Final[frozenset[FileKind]] = frozenset({FileKind.CSV, FileKind.PARQUET, FileKind.JSON})
"""Kinds whose rows are decided by the file's recorded key columns (JSON: its rows of records)."""


# ---------------------------------------------------------------------------
# Find
# ---------------------------------------------------------------------------
def find_principal(
    storage: Storage, principal_id: str, *, index: StoreIndex | None = None
) -> PrincipalFindings:
    """Every file holding the principal, and the models whose training data included them.

    Reads the whole store; changes nothing. The result carries keys and counts, never the id.
    """
    matcher = Matcher.for_id(require_principal_id(principal_id))
    layout = index or StoreIndex(storage)
    locations: list[PrincipalLocation] = []
    searched = 0
    for key in layout.keys:
        if not is_artefact_key(key):
            continue
        searched += 1
        location = _locate(storage, key, matcher, layout)
        if location is not None:
            locations.append(location)
    runs, models, unknown = _training_exposure(layout, locations)
    _LOGGER.info("privacy.find searched=%d locations=%d models=%d", searched, len(locations), len(models))
    return PrincipalFindings(
        searched_keys=searched,
        locations=tuple(locations),
        models=tuple(models),
        training_runs=tuple(runs),
        models_exposure_unknown=tuple(unknown),
    )


def _locate(storage: Storage, key: str, matcher: Matcher, layout: StoreIndex) -> PrincipalLocation | None:
    kind = file_kind(key)
    store = store_of(key)
    columns = layout.key_columns(key) if kind in _KEYED else None
    if kind is FileKind.BINARY:
        count = _binary_count(storage, key, matcher.principal_id.encode("utf-8"))
        if not count:
            return None
        return PrincipalLocation(
            key=key, store=store.value, file_kind=kind.value, occurrences=count, rewritable=False
        )
    try:
        hits = scan_bytes(kind, storage.read_bytes(key), matcher, columns)
    except StorageError:
        raise
    except Exception:  # an unparseable file is searched as bytes rather than skipped
        _LOGGER.warning("privacy.find: %s could not be parsed as %s; searching its bytes", key, kind.value)
        count = _binary_count(storage, key, matcher.principal_id.encode("utf-8"))
        return (
            PrincipalLocation(
                key=key,
                store=store.value,
                file_kind=FileKind.BINARY.value,
                occurrences=count,
                rewritable=False,
            )
            if count
            else None
        )
    if not hits.any:
        if not hits.ambiguous:
            return None
        return PrincipalLocation(  # a numeric id in a keyless file: reported for review (DEC-737)
            key=key,
            store=store.value,
            file_kind=kind.value,
            key_columns=columns,
            occurrences=hits.ambiguous,
            rewritable=False,
            needs_review=True,
        )
    return PrincipalLocation(
        key=key,
        store=store.value,
        file_kind=kind.value,
        key_columns=columns,
        rows=hits.rows,
        cells=hits.cells,
        occurrences=hits.occurrences,
    )


def _binary_count(storage: Storage, key: str, needle: bytes) -> int:
    """Occurrences of `needle` in a file, streamed so a large model directory is never held whole."""
    count = 0
    tail = b""
    with storage.open_read(key) as handle:
        while chunk := handle.read(_CHUNK):
            window = tail + chunk
            count += window.count(needle) - tail.count(needle)
            tail = window[-(len(needle) - 1) :] if len(needle) > 1 else b""
    return count


def _training_exposure(
    layout: StoreIndex, locations: list[PrincipalLocation]
) -> tuple[list[str], list[str], list[str]]:
    """Training runs whose inputs held the principal, the model versions they produced, and the
    versions whose training inputs are gone so that nobody can tell (DEC-709).

    Retention deletes a training run's upload or dataset (and its row explanations) after
    `retention_days` but keeps the model, so a model older than that would otherwise never be
    flagged although it learned from the person. Such a model is reported as *exposure unknown* and
    flagged, the conservative reading: the next cycle retrains it on data that can be checked.
    """
    uploads: set[str] = set()
    datasets: set[str] = set()
    sources: set[str] = set()
    run_dirs: set[str] = set()
    for location in locations:
        if location.needs_review:  # undecided is not evidence the person was in the training data
            continue
        parts = location.key.split("/")
        if location.store == Store.UPLOADS:
            uploads.add(parts[1])
        elif location.store == Store.DATASETS:
            datasets.add(parts[1])
        elif location.store == Store.SOURCES:
            sources.add(parts[3])
        elif parts[0] == "runs":
            run_dirs.add(parts[1])
    runs: list[str] = []
    models: list[str] = []
    unknown: list[str] = []
    for run in sorted(layout.runs.values(), key=lambda item: item.run_id):
        if run.mode != "train":
            continue
        lineage = layout.datasets.get(run.dataset_id or "")
        exposed = (
            (run.upload_id is not None and run.upload_id in uploads)
            or (run.dataset_id is not None and run.dataset_id in datasets)
            or run.run_id in run_dirs
            or (lineage is not None and bool(sources & set(lineage.source_ids)))
        )
        if exposed:
            runs.append(run.run_id)
            if run.model_version_id and run.model_version_id not in models:
                models.append(run.model_version_id)
            continue
        inputs_gone = (run.upload_id is not None and run.upload_id not in layout.uploads) or (
            run.dataset_id is not None and run.dataset_id not in layout.datasets
        )
        if inputs_gone and run.model_version_id and run.model_version_id not in unknown:
            unknown.append(run.model_version_id)
    return runs, models, [model for model in unknown if model not in models]


def _exposure_reason(layout: StoreIndex, model_id: str, findings: PrincipalFindings) -> str:
    """A short code for why a model is flagged, for `model_retrain_flag.reason`."""
    keys = {location.key.split("/")[1] for location in findings.locations if location.store == Store.UPLOADS}
    for run in layout.runs.values():
        if run.model_version_id == model_id:
            if run.upload_id in keys:
                return "training_input:upload"
            if run.dataset_id is not None:
                return "training_input:dataset"
            return "training_run_artefacts"
    return "training_input"


# ---------------------------------------------------------------------------
# Erase
# ---------------------------------------------------------------------------
def erase(
    storage: Storage,
    principal_id: str,
    *,
    engine: Engine,
    principal: Principal,
    salt: str,
    client_id: str | None = None,
    config_root: Path | None = None,
    audit_log: AuditLog | None = None,
    now: datetime | None = None,
    request_id: str | None = None,
    history_all_clients: bool = False,
    max_attempts: int = 1,
    wait: Callable[[int], None] | None = None,
    resume: bool = False,
    audit_action: str = "privacy.erasure",
) -> ErasureOutcome:
    """Erase one data principal from every store, record the request, and flag affected models.

    `principal` is who asked (an Admin through the API, or a job). `salt` is the deployment's
    `privacy_salt` (DEC-860). `history_all_clients` deletes the consent history under every client
    rather than `client_id`'s only - what the API does when the Admin named no client, since the same
    person's consent may be recorded under the deployment's id and under an onboarding client's
    (DEC-738).

    **Store by store, with retries** (Plan D M54, DEC-863). The files found are rewritten one store at
    a time; `erasure_progress` holds each store's count of files done, its attempts and its status. A
    file that cannot be written (`StorageError`, `OSError`) is tried again, with the rest of its
    store's failures, up to `max_attempts` times, calling `wait(attempt)` between tries (the job's
    backoff); a store still failing after that is `failed`, the other stores carry on, and the request
    ends `failed` with `ERASURE_STORE_FAILED` - its counts recorded, its models flagged - so a retry
    (`resume=True` on the same `request_id`, which re-finds whatever still holds the person) finishes
    the job. `resume` means the request row already exists and is `queued` (the route queued it, or
    re-queued a failed one with `requeue_failed`): it is moved to `in_progress` rather than inserted,
    and must belong to the same principal. The models are flagged before any file is rewritten, so a
    run that fails half way has flagged them already - a retry could no longer find the person in the
    stores this one erased (DEC-870).

    Raises `PrivacyError` - `PRINCIPAL_ID_INVALID`, `ERASURE_RUNS_IN_PROGRESS`, `ERASURE_INCOMPLETE`
    when a rewritten file still holds the id, `ERASURE_STORE_FAILED` - after recording the request as
    `failed`; the request row is written first, so even a crash leaves evidence the request was received.
    """
    cleaned = require_principal_id(principal_id)
    policy = load_privacy_config(config_root).erasure
    create_privacy_tables(engine)
    matcher = Matcher.for_id(cleaned)
    hashed = principal_hash(principal_key(cleaned), salt=salt)
    request = request_id or f"er_{uuid.uuid4().hex[:20]}"
    started = now or utc_now()
    if resume:
        _reopen_row(engine, request, hashed)
    else:
        queue_request(
            engine,
            request_id=request,
            principal_hash=hashed,
            client_id=client_id,
            mode=policy.mode.value,
            requested_by=principal.user_id,
            requested_at=started,
            status="in_progress",
            history_all_clients=history_all_clients,
        )
    progress = _Progress(engine, request)
    try:
        outcome = _erase(
            storage,
            cleaned,
            matcher,
            engine=engine,
            policy_mode=policy.mode,
            tombstone=policy.tombstone,
            delete_consent=policy.consent_history == "delete",
            salt=salt,
            client_id=client_id,
            history_client_id=None if history_all_clients else client_id,
            request_id=request,
            hashed=hashed,
            principal=principal,
            started=started,
            progress=progress,
            max_attempts=max(1, max_attempts),
            wait=wait or (lambda _attempt: None),
        )
    except Exception as exc:
        code = exc.code if isinstance(exc, PrivacyError) else "ERASURE_FAILED"
        _finish_row(engine, request, status="failed", error_code=code, merge=resume)
        if audit_log is not None:
            details: dict[str, str | int | float | bool | None] = {
                "request_kind": "erasure",
                "principal_hash": hashed,
                "reason_code": code,
            }
            audit_log.append(
                _event(principal, request, outcome="failed", details=details, action=audit_action)
            )
        raise
    if outcome.failed_stores:
        _finish_row(
            engine, request, status="failed", outcome=outcome, error_code="ERASURE_STORE_FAILED", merge=resume
        )
        if audit_log is not None:
            audit_log.append(
                _event(
                    principal,
                    request,
                    outcome="failed",
                    details={
                        **erasure_audit_details(outcome),
                        "reason_code": "ERASURE_STORE_FAILED",
                        "failed_stores": ",".join(outcome.failed_stores)[:200],
                    },
                    after=outcome,
                    action=audit_action,
                )
            )
        raise PrivacyError(
            "ERASURE_STORE_FAILED",
            f"{len(outcome.failed_stores)} store(s) could not be rewritten after every retry "
            f"({', '.join(outcome.failed_stores)}); the request is recorded as failed and can be retried.",
        )
    _finish_row(engine, request, status=outcome.status, outcome=outcome, merge=resume)
    if audit_log is not None:
        audit_log.append(
            _event(
                principal,
                request,
                outcome="success",
                details=erasure_audit_details(outcome),
                after=outcome,
                action=audit_action,
            )
        )
    return outcome


def queue_request(
    engine: Engine,
    *,
    request_id: str,
    principal_hash: str,
    client_id: str | None,
    mode: str,
    requested_by: str,
    requested_at: datetime,
    status: str = "queued",
    history_all_clients: bool = False,
) -> None:
    """Write the register row of a request before any work starts (DEC-752, DEC-863).

    `history_all_clients` is recorded so a retry deletes the same consent history (DEC-870).
    """
    create_privacy_tables(engine)
    with Session(engine) as session:
        session.add(
            ErasureRequestRow(
                request_id=request_id,
                client_id=client_id,
                principal_hash=principal_hash,
                status=status,
                mode=mode,
                requested_by=requested_by,
                requested_at=requested_at,
                history_all_clients=history_all_clients,
            )
        )
        session.commit()


def fail_if_unfinished(engine: Engine, request_id: str, code: str) -> None:
    """Mark a request that a job left `queued` or `in_progress` as failed with `code` (a crash)."""
    with Session(engine) as session:
        row = session.get(ErasureRequestRow, request_id)
        if row is None or row.status not in {"queued", "in_progress"}:
            return
        row.status, row.error_code, row.completed_at = "failed", code, utc_now()
        session.add(row)
        session.commit()


def fail_interrupted(engine: Engine) -> int:
    """Mark every request left `queued` or `in_progress` as failed with `ERASURE_INTERRUPTED`.

    Called when the API starts (`api.routes.privacy.install_privacy_checks`, DEC-869). The jobs live in
    the API process's memory, so a request still unfinished at start-up belongs to a process that has
    stopped and will never finish on its own; `failed` makes it retryable. Safe because a deployment
    runs one API process (DEC-861's premise): no other live process can own a running job. Returns how
    many rows were marked.
    """
    create_privacy_tables(engine)
    statement = (
        update(ErasureRequestRow)
        .where(col(ErasureRequestRow.status).in_(("queued", "in_progress")))
        .values(status="failed", error_code=INTERRUPTED, completed_at=utc_now())
    )
    with engine.begin() as connection:
        marked = connection.execute(statement).rowcount
    if marked:
        _LOGGER.warning("privacy.erasure %d unfinished request(s) marked failed %s", marked, INTERRUPTED)
    return int(marked or 0)


def requeue_failed(engine: Engine, request_id: str) -> bool:
    """Move a `failed` request back to `queued` in one statement; False when it was not `failed`.

    The retry route's claim on the request (DEC-869): of two retries at once only one changes the row,
    and the progress route reads `queued` from the moment the retry is answered.
    """
    statement = (
        update(ErasureRequestRow)
        .where(col(ErasureRequestRow.request_id) == request_id)
        .where(col(ErasureRequestRow.status) == "failed")
        .values(status="queued", error_code=None, completed_at=None)
    )
    with engine.begin() as connection:
        return bool(connection.execute(statement).rowcount == 1)


def _reopen_row(engine: Engine, request_id: str, hashed: str) -> None:
    """Move a `queued` request of this person to `in_progress`; refuse anything else (DEC-869)."""
    statement = (
        update(ErasureRequestRow)
        .where(col(ErasureRequestRow.request_id) == request_id)
        .where(col(ErasureRequestRow.status) == "queued")
        .where(col(ErasureRequestRow.principal_hash) == hashed)
        .values(status="in_progress", error_code=None, completed_at=None)
    )
    with engine.begin() as connection:
        if connection.execute(statement).rowcount == 1:
            return
    with Session(engine) as session:
        row = session.get(ErasureRequestRow, request_id)
    if row is None:
        raise PrivacyError("ERASURE_REQUEST_NOT_FOUND", "There is no erasure request with this id.")
    if row.principal_hash != hashed:
        raise PrivacyError("PRINCIPAL_MISMATCH", "This id is not the one the erasure request was made for.")
    raise PrivacyError(
        "ERASURE_NOT_RETRYABLE", f"Only a queued erasure request can be started; this one is {row.status}."
    )


class _Progress:
    """Writes `erasure_progress` as the stores are worked through (DEC-863)."""

    def __init__(self, engine: Engine, request_id: str) -> None:
        self._engine = engine
        self._request_id = request_id

    def plan(self, totals: dict[str, int]) -> None:
        """One `pending` row per store about to be worked on (a retry resets its stores' rows)."""
        with Session(self._engine) as session:
            for store, total in totals.items():
                row = session.get(ErasureProgressRow, (self._request_id, store))
                if row is None:
                    row = ErasureProgressRow(
                        request_id=self._request_id, store=store, status="pending", updated_at=utc_now()
                    )
                row.status, row.files_total, row.files_done, row.error_code = "pending", total, 0, None
                row.updated_at = utc_now()
                session.add(row)
            session.commit()

    def update(
        self,
        store: str,
        *,
        status: str | None = None,
        done: int | None = None,
        attempts: int | None = None,
        error_code: str | None = None,
    ) -> None:
        with Session(self._engine) as session:
            row = session.get(ErasureProgressRow, (self._request_id, store))
            if row is None:
                return
            if status is not None:
                row.status = status
            if done is not None:
                row.files_done = done
            if attempts is not None:
                row.attempts = attempts
            row.error_code = error_code if status == "failed" else row.error_code if status is None else None
            row.updated_at = utc_now()
            session.add(row)
            session.commit()


_RETRYABLE: Final[tuple[type[BaseException], ...]] = (StorageError, OSError)
"""Failures of one file's write that a later attempt may not meet: the store, not the request, is at fault."""

CONSENT_STORE: Final[str] = "consent_ledger"
"""The consent history's step in the progress rows; it is a table, not an artefact store."""


def _erase(
    storage: Storage,
    principal_id: str,
    matcher: Matcher,
    *,
    engine: Engine,
    policy_mode: ErasureMode,
    tombstone: str,
    delete_consent: bool,
    salt: str,
    client_id: str | None,
    history_client_id: str | None,
    request_id: str,
    hashed: str,
    principal: Principal,
    started: datetime,
    progress: _Progress,
    max_attempts: int,
    wait: Callable[[int], None],
) -> ErasureOutcome:
    layout = StoreIndex(storage)
    findings = find_principal(storage, principal_id, index=layout)
    busy = _runs_in_progress(layout, findings)
    if busy:
        raise PrivacyError(
            "ERASURE_RUNS_IN_PROGRESS",
            f"{len(busy)} run(s) still reading this person's data have not finished; a run that has "
            "already read it would write it back after the erasure. Nothing was changed; ask again "
            "once they have finished.",
        )
    # Flag first (DEC-870): once a store is rewritten the person can no longer be found there, so a
    # run that fails after it - and the retry that follows - would never flag its models.
    flags, flagged = _flag_models(engine, layout, findings, request_id, started)
    _record_flags(engine, request_id, tuple(flags))
    by_store: dict[str, list[PrincipalLocation]] = {}
    for location in findings.locations:
        by_store.setdefault(location.store, []).append(location)
    totals = {
        store: sum(1 for loc in items if loc.rewritable or loc.store == Store.LLM_CACHE)
        for store, items in by_store.items()
    }
    progress.plan({**totals, **({CONSENT_STORE: 1} if delete_consent else {})})
    counts: dict[str, StoreCount] = {}
    tally = {"rows": 0, "cells": 0, "rewritten": 0, "deleted": 0}
    unrewritable: list[str] = []
    touched: list[tuple[str, FileKind]] = []
    failed_stores: list[str] = []
    for store, locations in by_store.items():
        unrewritable.extend(
            loc.key for loc in locations if not loc.rewritable and loc.store != Store.LLM_CACHE
        )
        remaining = [loc for loc in locations if loc.rewritable or loc.store == Store.LLM_CACHE]
        done, attempt = 0, 0
        progress.update(store, status="running")
        while remaining:
            attempt += 1
            failed: list[PrincipalLocation] = []
            for location in remaining:
                try:
                    changed_rows, changed_cells, kind = _erase_file(
                        storage, location, matcher, policy_mode=policy_mode, tombstone=tombstone
                    )
                except _RETRYABLE as exc:
                    _LOGGER.warning(
                        "privacy.erase request=%s store=%s attempt=%d file failed error=%s",
                        request_id,
                        store,
                        attempt,
                        type(exc).__name__,
                    )
                    failed.append(location)
                    continue
                if kind is None:
                    tally["deleted"] += 1
                else:
                    tally["rewritten"] += 1
                    touched.append((location.key, kind))
                tally["rows"] += changed_rows
                tally["cells"] += changed_cells
                previous = counts.get(store, StoreCount())
                counts[store] = StoreCount(
                    files=previous.files + 1,
                    rows=previous.rows + changed_rows,
                    cells=previous.cells + changed_cells,
                )
                done += 1
                progress.update(store, done=done, attempts=attempt)
            remaining = failed
            if remaining and attempt < max_attempts:
                wait(attempt)
                continue
            break
        if remaining:
            failed_stores.append(store)
            progress.update(store, status="failed", attempts=attempt, error_code="STORE_WRITE_FAILED")
        else:
            progress.update(store, status="done", attempts=max(attempt, 1))
    remaining_keys = [
        key
        for key, kind in touched
        if scan_bytes(
            kind, storage.read_bytes(key), matcher, layout.key_columns(key) if kind in _KEYED else None
        ).any
    ]
    if remaining_keys:
        raise PrivacyError(
            "ERASURE_INCOMPLETE",
            f"{len(remaining_keys)} rewritten file(s) still hold the principal; the request is recorded as failed.",
        )
    consent_deleted = 0
    if delete_consent:
        progress.update(CONSENT_STORE, status="running")
        consent_deleted = ConsentLedger(engine, salt=salt).delete_history(
            principal_id, client_id=history_client_id
        )
        progress.update(CONSENT_STORE, status="done", done=1, attempts=1)
    # every flag this request holds, a failed earlier run's included, so a retry reports them too
    unknown = tuple(model for model, reason in flags.items() if reason == _UNAVAILABLE)
    finished = utc_now()
    _LOGGER.info(
        "privacy.erase request=%s files_rewritten=%d files_deleted=%d rows=%d cells=%d unrewritable=%d models_flagged=%d failed_stores=%d",
        request_id,
        tally["rewritten"],
        tally["deleted"],
        tally["rows"],
        tally["cells"],
        len(unrewritable),
        len(flagged),
        len(failed_stores),
    )
    rows = tally["rows"]
    return ErasureOutcome(
        request_id=request_id,
        status=(
            "failed"
            if failed_stores
            else "completed_with_exceptions" if unrewritable or unknown else "completed"
        ),
        principal_hash=hashed,
        client_id=client_id,
        mode=policy_mode.value,
        store_counts=counts,
        rows_deleted=rows if policy_mode is ErasureMode.DELETE else 0,
        rows_tombstoned=rows if policy_mode is ErasureMode.TOMBSTONE else 0,
        cells_masked=tally["cells"],
        files_rewritten=tally["rewritten"],
        files_deleted=tally["deleted"],
        unrewritable_keys=tuple(unrewritable),
        models_flagged=tuple(flags),  # every model this request flagged, in any of its runs
        models_exposure_unknown=unknown,
        consent_records_deleted=consent_deleted,
        failed_stores=tuple(failed_stores),
        requested_by=principal.user_id,
        requested_at=started,
        completed_at=finished,
    )


def _erase_file(
    storage: Storage,
    location: PrincipalLocation,
    matcher: Matcher,
    *,
    policy_mode: ErasureMode,
    tombstone: str,
) -> tuple[int, int, FileKind | None]:
    """Erase the principal from one file: `(rows, cells, kind)`, `kind` None for a deleted cache entry."""
    if location.store == Store.LLM_CACHE:
        storage.delete(location.key)
        purge_old_versions(storage, location.key)
        return 0, location.rows + location.cells + location.occurrences, None
    kind = FileKind(location.file_kind)
    data = storage.read_bytes(location.key)
    new, hits = rewrite_bytes(
        kind, data, matcher, location.key_columns, mode=policy_mode, tombstone=tombstone
    )
    storage.write_bytes(location.key, new)
    purge_old_versions(storage, location.key)
    return hits.rows, hits.cells + hits.occurrences, kind


def _flag_models(
    engine: Engine, layout: StoreIndex, findings: PrincipalFindings, request_id: str, now: datetime
) -> tuple[dict[str, str], list[str]]:
    """Flag the models `findings` names: `(every flag of this request -> reason, the ones added now)`.

    A retry of the same request flags nothing twice (DEC-863); the first return value holds the flags
    earlier runs of the request recorded as well, which is what the outcome reports (DEC-870).
    """
    flagged: list[str] = []
    with Session(engine) as session:
        rows = session.exec(
            select(ModelRetrainFlagRow)
            .where(col(ModelRetrainFlagRow.request_id) == request_id)
            .order_by(col(ModelRetrainFlagRow.flag_id))
        ).all()
        flags: dict[str, str] = {}
        for row in rows:
            flags.setdefault(row.model_id, row.reason)
        for model_id in (*findings.models, *findings.models_exposure_unknown):
            if model_id in flags:
                continue
            reason = (
                _UNAVAILABLE
                if model_id in findings.models_exposure_unknown
                else _exposure_reason(layout, model_id, findings)
            )
            session.add(
                ModelRetrainFlagRow(model_id=model_id, request_id=request_id, reason=reason, created_at=now)
            )
            flags[model_id] = reason
            flagged.append(model_id)
        session.commit()
    return flags, flagged


def _record_flags(engine: Engine, request_id: str, models: tuple[str, ...]) -> None:
    """Put the request's flagged models on its row at once, so a run that fails later still shows them."""
    with Session(engine) as session:
        row = session.get(ErasureRequestRow, request_id)
        if row is None:
            return
        listed = list(json.loads(row.models_flagged_json or "[]"))
        row.models_flagged_json = json.dumps(listed + [model for model in models if model not in listed])
        session.add(row)
        session.commit()


def _runs_in_progress(layout: StoreIndex, findings: PrincipalFindings) -> list[str]:
    """Unfinished runs that read, or are writing, something that holds the principal (DEC-728)."""
    uploads: set[str] = set()
    datasets: set[str] = set()
    sources: set[str] = set()
    run_dirs: set[str] = set()
    for location in findings.locations:
        parts = location.key.split("/")
        if location.store == Store.UPLOADS:
            uploads.add(parts[1])
        elif location.store == Store.DATASETS:
            datasets.add(parts[1])
        elif location.store == Store.SOURCES:
            sources.add(parts[3])
        elif parts[0] == "runs":
            run_dirs.add(parts[1])
    busy: list[str] = []
    for run in layout.runs.values():
        if run.terminal:
            continue
        lineage = layout.datasets.get(run.dataset_id or "")
        if (
            (run.upload_id is not None and run.upload_id in uploads)
            or (run.dataset_id is not None and run.dataset_id in datasets)
            or run.run_id in run_dirs
            or (lineage is not None and bool(sources & set(lineage.source_ids)))
        ):
            busy.append(run.run_id)
    return sorted(busy)


def _finish_row(
    engine: Engine,
    request_id: str,
    *,
    status: str,
    outcome: ErasureOutcome | None = None,
    error_code: str | None = None,
    merge: bool = False,
) -> None:
    """Record how a request ended. `merge` adds a retry's counts to what the earlier runs recorded."""
    with Session(engine) as session:
        row = session.get(ErasureRequestRow, request_id)
        if row is None:
            return
        row.status = status
        row.error_code = error_code
        row.completed_at = utc_now()
        if outcome is not None:
            counts = (
                {
                    store: StoreCount.model_validate(value)
                    for store, value in json.loads(row.store_counts_json or "{}").items()
                }
                if merge
                else {}
            )
            for store, count in outcome.store_counts.items():
                before = counts.get(store, StoreCount())
                counts[store] = StoreCount(
                    files=before.files + count.files,
                    rows=before.rows + count.rows,
                    cells=before.cells + count.cells,
                )
            flagged = list(json.loads(row.models_flagged_json or "[]")) if merge else []
            flagged += [model for model in outcome.models_flagged if model not in flagged]
            row.store_counts_json = json.dumps(
                {store: count.model_dump() for store, count in sorted(counts.items())}
            )
            row.models_flagged_json = json.dumps(flagged)
            keep = 1 if merge else 0  # a retry adds to what the earlier runs of the request did
            row.rows_deleted = keep * row.rows_deleted + outcome.rows_deleted
            row.rows_tombstoned = keep * row.rows_tombstoned + outcome.rows_tombstoned
            row.cells_masked = keep * row.cells_masked + outcome.cells_masked
            row.files_rewritten = keep * row.files_rewritten + outcome.files_rewritten
            row.files_deleted = keep * row.files_deleted + outcome.files_deleted
            row.completed_at = outcome.completed_at
        session.add(row)
        session.commit()


def _event(
    principal: Principal,
    request_id: str,
    *,
    outcome: str,
    details: dict[str, str | int | float | bool | None],
    after: ErasureOutcome | None = None,
    action: str = "privacy.erasure",
) -> AuditEvent:
    return AuditEvent(
        event_id=uuid.uuid4().hex,
        occurred_at=utc_now(),
        actor_id=principal.user_id,
        actor_kind=principal.kind,
        action=action,
        object_type="erasure_request",
        object_id=request_id,
        after_hash=content_hash(after),
        outcome=outcome,
        details=details,
    )


def erasure_audit_details(outcome: ErasureOutcome) -> dict[str, str | int | float | bool | None]:
    """The audit `details` of an erasure: hash, counts and store tokens - never the id (DEC-705).

    For an API route to pass to `set_audit_context(details=...)`, so the single event the audit
    middleware writes for the request carries the outcome.
    """
    stores = ",".join(f"{store}:{count.files}" for store, count in sorted(outcome.store_counts.items()))
    return {
        "request_kind": "erasure",
        "principal_hash": outcome.principal_hash,
        "client_id": outcome.client_id,
        "deleted": outcome.rows_deleted,
        "tombstoned": outcome.rows_tombstoned,
        "count": outcome.files_rewritten + outcome.files_deleted,
        "stores": stores[:200],
        "models_flagged": len(outcome.models_flagged),
    }


# ---------------------------------------------------------------------------
# The register and the retraining flags
# ---------------------------------------------------------------------------
def erasure_requests(
    engine: Engine, *, principal_hash: str | None = None
) -> tuple[ErasureRequestRecord, ...]:
    """Every erasure request, newest first; only one principal's when `principal_hash` is given."""
    create_privacy_tables(engine)
    statement = select(ErasureRequestRow)
    if principal_hash is not None:
        statement = statement.where(col(ErasureRequestRow.principal_hash) == principal_hash)
    statement = statement.order_by(col(ErasureRequestRow.requested_at).desc())
    with Session(engine) as session:
        return tuple(_request_contract(row) for row in session.exec(statement).all())


def erasure_request(engine: Engine, request_id: str) -> ErasureRequestRecord | None:
    """One erasure request, or None."""
    create_privacy_tables(engine)
    with Session(engine) as session:
        row = session.get(ErasureRequestRow, request_id)
        return None if row is None else _request_contract(row, _progress_of(session, request_id))


def _request_contract(
    row: ErasureRequestRow, progress: tuple[StoreProgress, ...] = ()
) -> ErasureRequestRecord:
    counts = json.loads(row.store_counts_json or "{}")
    return ErasureRequestRecord(
        request_id=row.request_id,
        client_id=row.client_id,
        principal_hash=row.principal_hash,
        status=row.status,
        mode=row.mode,
        store_counts={store: StoreCount.model_validate(value) for store, value in counts.items()},
        models_flagged=tuple(json.loads(row.models_flagged_json or "[]")),
        rows_deleted=row.rows_deleted,
        rows_tombstoned=row.rows_tombstoned,
        cells_masked=row.cells_masked,
        files_rewritten=row.files_rewritten,
        files_deleted=row.files_deleted,
        requested_by=row.requested_by,
        requested_at=aware_utc(row.requested_at),
        completed_at=None if row.completed_at is None else aware_utc(row.completed_at),
        error_code=row.error_code,
        progress=progress,
        history_all_clients=row.history_all_clients,
    )


def _progress_of(session: Session, request_id: str) -> tuple[StoreProgress, ...]:
    rows = session.exec(
        select(ErasureProgressRow)
        .where(col(ErasureProgressRow.request_id) == request_id)
        .order_by(col(ErasureProgressRow.store))
    ).all()
    return tuple(
        StoreProgress(
            store=row.store,
            status=row.status,  # type: ignore[arg-type]
            files_total=row.files_total,
            files_done=row.files_done,
            attempts=row.attempts,
            error_code=row.error_code,
            updated_at=aware_utc(row.updated_at),
        )
        for row in rows
    )


def models_flagged_for_retraining(engine: Engine) -> tuple[str, ...]:
    """Model versions with an open retraining flag, sorted: what the next retraining cycle must redo.

    Read by the scheduler (M49). A flag does not retrain anything and does not demote a champion; it
    makes the model due at the next scheduled cycle, whose challenger still goes through the champion
    rule and approval (DEC-743).
    """
    create_privacy_tables(engine)
    statement = select(ModelRetrainFlagRow.model_id).where(col(ModelRetrainFlagRow.cleared_at).is_(None))
    with Session(engine) as session:
        return tuple(sorted(set(session.exec(statement).all())))


def retrain_flags(engine: Engine, *, open_only: bool = True) -> tuple[RetrainFlag, ...]:
    """The flags themselves, oldest first, for an API listing."""
    create_privacy_tables(engine)
    statement = select(ModelRetrainFlagRow)
    if open_only:
        statement = statement.where(col(ModelRetrainFlagRow.cleared_at).is_(None))
    statement = statement.order_by(col(ModelRetrainFlagRow.flag_id))
    with Session(engine) as session:
        return tuple(
            RetrainFlag(
                flag_id=row.flag_id or 0,
                model_id=row.model_id,
                request_id=row.request_id,
                reason=row.reason,
                created_at=aware_utc(row.created_at),
                cleared_at=None if row.cleared_at is None else aware_utc(row.cleared_at),
            )
            for row in session.exec(statement).all()
        )


def clear_flag(engine: Engine, model_id: str, *, now: datetime | None = None) -> int:
    """Close every open flag on `model_id` - called once the retraining cycle has produced its
    challenger. Returns how many flags were closed (0 when none was open)."""
    create_privacy_tables(engine)
    moment = now or utc_now()
    with Session(engine) as session:
        rows = session.exec(
            select(ModelRetrainFlagRow)
            .where(col(ModelRetrainFlagRow.model_id) == model_id)
            .where(col(ModelRetrainFlagRow.cleared_at).is_(None))
        ).all()
        for row in rows:
            row.cleared_at = moment
            session.add(row)
        session.commit()
        return len(rows)
