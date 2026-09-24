"""Retrying an erasure request (Plan D, DEC-881, DEC-882): the claim, the restart, the flags, the report.

A background erasure can end `failed` part way - a store that would not take a write, a crash - and
be run again under the same request id. These tests pin what a retry must not lose: a request left
unfinished by a stopped process becomes retryable; a retry is claimed in one statement; the models
the first run should have flagged are flagged even though the retry can no longer find the person in
the stores the first run erased; and the retry's report still says which models nobody could check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, col, select

from engine.access.roles import LOCAL_OPERATOR
from engine.config import RunMode
from engine.platform_db import sqlite_engine
from engine.privacy.erasure import (
    INTERRUPTED,
    erase,
    erasure_request,
    fail_interrupted,
    models_flagged_for_retraining,
    queue_request,
    requeue_failed,
)
from engine.privacy.errors import PrivacyError
from engine.privacy.retention import RETENTION_JOB, apply_retention, plan_retention
from engine.privacy.tables import ErasureRequestRow, ModelRetrainFlagRow
from engine.storage import LocalStorage, StorageError
from tests.unit.production.privacy_support import customers, write_row_explanations, write_run, write_upload
from tests.unit.production.sentinel_store import (
    MODEL_ON_DATASET,
    MODEL_ON_UPLOAD,
    SALT,
    SENTINEL,
    Planted,
    files_holding,
    plant,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)


class BrokenStorage:
    """A store whose writes under `prefix` raise `error` while `broken` is set."""

    def __init__(self, inner: LocalStorage, prefix: str, error: Exception) -> None:
        self._inner = inner
        self.prefix = prefix
        self.error = error
        self.broken = True

    def write_bytes(self, key: str, data: bytes) -> None:
        if self.broken and key.startswith(self.prefix):
            raise self.error
        self._inner.write_bytes(key, data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _queue(engine: Any, request_id: str, status: str) -> None:
    queue_request(
        engine,
        request_id=request_id,
        principal_hash="h",
        client_id=None,
        mode="delete",
        requested_by="u_1",
        requested_at=NOW,
        status=status,
    )


def _flags(engine: Any, request_id: str) -> list[tuple[str, str]]:
    with Session(engine) as session:
        rows = session.exec(
            select(ModelRetrainFlagRow)
            .where(col(ModelRetrainFlagRow.request_id) == request_id)
            .order_by(col(ModelRetrainFlagRow.flag_id))
        ).all()
        return [(row.model_id, row.reason) for row in rows]


# --- DEC-881: the restart and the claim ---------------------------------------------------------------
def test_requests_a_stopped_process_left_unfinished_are_marked_failed(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path / "platform.db")
    for request_id, status in (
        ("er_queued", "queued"),
        ("er_running", "in_progress"),
        ("er_done", "completed"),
        ("er_failed", "failed"),
    ):
        _queue(engine, request_id, status)
    assert fail_interrupted(engine) == 2
    for request_id in ("er_queued", "er_running"):
        record = erasure_request(engine, request_id)
        assert record is not None and (record.status, record.error_code) == ("failed", INTERRUPTED)
        assert record.completed_at is not None
    done, failed = erasure_request(engine, "er_done"), erasure_request(engine, "er_failed")
    assert done is not None and done.status == "completed", "a finished request is left alone"
    assert failed is not None and failed.error_code is None, "an earlier failure keeps its own code"
    assert fail_interrupted(engine) == 0
    assert requeue_failed(engine, "er_running"), "an interrupted request can be retried"


def test_a_failed_request_is_claimed_once(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path / "platform.db")
    _queue(engine, "er_1", "failed")
    assert requeue_failed(engine, "er_1") is True
    record = erasure_request(engine, "er_1")
    assert record is not None and (record.status, record.error_code, record.completed_at) == (
        "queued",
        None,
        None,
    )
    assert requeue_failed(engine, "er_1") is False, "the second claim changes nothing"
    assert requeue_failed(engine, "er_nope") is False
    _queue(engine, "er_2", "completed")
    assert requeue_failed(engine, "er_2") is False


def test_a_job_starts_only_a_queued_request(tmp_path: Path, config_root: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    engine = sqlite_engine(tmp_path / "platform.db")
    first = erase(
        storage, SENTINEL, engine=engine, principal=LOCAL_OPERATOR, salt=SALT, config_root=config_root
    )
    for status in ("failed", "in_progress", "completed"):
        with Session(engine) as session:
            row = session.get(ErasureRequestRow, first.request_id)
            assert row is not None
            row.status = status
            session.add(row)
            session.commit()
        with pytest.raises(PrivacyError) as caught:
            erase(
                storage,
                SENTINEL,
                engine=engine,
                principal=LOCAL_OPERATOR,
                salt=SALT,
                config_root=config_root,
                request_id=first.request_id,
                resume=True,
            )
        assert caught.value.code == "ERASURE_NOT_RETRYABLE"
        record = erasure_request(engine, first.request_id)
        assert record is not None and record.status == status, "the row is left as it was"


# --- DEC-882: flags before rewrites, and the retry's report -------------------------------------------
@pytest.fixture
def planted(tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> Planted:
    return plant(tmp_path / "data", config_root, monkeypatch)


def test_a_run_that_crashes_mid_rewrite_has_flagged_its_models_and_the_retry_keeps_them(
    planted: Planted, config_root: Path
) -> None:
    engine = sqlite_engine(planted.platform_db)
    storage = BrokenStorage(planted.storage, "uploads/", RuntimeError("the disk went away"))
    kwargs: dict[str, Any] = {
        "engine": engine,
        "principal": LOCAL_OPERATOR,
        "salt": SALT,
        "config_root": config_root,
        "request_id": "er_crash",
    }
    with pytest.raises(RuntimeError):
        erase(storage, SENTINEL, **kwargs)  # type: ignore[arg-type]
    failed = erasure_request(engine, "er_crash")
    assert failed is not None and (failed.status, failed.error_code) == ("failed", "ERASURE_FAILED")
    left = files_holding(planted.root, SENTINEL)
    assert left and not any(path.startswith("datasets/") for path in left), (
        "the guard: the dataset the second model trained on was erased before the crash",
        left,
    )
    assert models_flagged_for_retraining(engine) == tuple(sorted((MODEL_ON_UPLOAD, MODEL_ON_DATASET)))
    assert failed.models_flagged == (MODEL_ON_UPLOAD, MODEL_ON_DATASET), "on the row as well"

    assert requeue_failed(engine, "er_crash")
    storage.broken = False
    done = erase(storage, SENTINEL, resume=True, **kwargs)  # type: ignore[arg-type]
    assert done.status == "completed"
    assert done.models_flagged == (MODEL_ON_UPLOAD, MODEL_ON_DATASET), "every flag of the request is reported"
    assert [model for model, _ in _flags(engine, "er_crash")] == [
        MODEL_ON_UPLOAD,
        MODEL_ON_DATASET,
    ], "none twice"
    record = erasure_request(engine, "er_crash")
    assert record is not None and record.models_flagged == (MODEL_ON_UPLOAD, MODEL_ON_DATASET)
    assert files_holding(planted.root, SENTINEL) == []


def test_a_retry_still_reports_the_models_nobody_could_check(tmp_path: Path, config_root: Path) -> None:
    inner = LocalStorage(tmp_path / "data")
    trained = datetime(2026, 1, 1, tzinfo=UTC)
    keys = ["C-1", SENTINEL, "C-3"]
    write_upload(inner, "u_1", customers(keys), created_at=trained)
    write_run(
        inner, "r_train", mode=RunMode.TRAIN, created_at=trained, upload_id="u_1", model_version_id="m_1"
    )
    write_row_explanations(inner, "r_train", keys)
    later = trained + timedelta(days=100)
    apply_retention(plan_retention(inner, config_root, later), inner, None, RETENTION_JOB)
    write_upload(inner, "u_2", customers(keys), created_at=later)  # a later upload, still held
    storage = BrokenStorage(inner, "uploads/u_2/", StorageError("WRITE_FAILED", "the disk said no", key="k"))
    engine = sqlite_engine(tmp_path / "platform.db")
    kwargs: dict[str, Any] = {
        "engine": engine,
        "principal": LOCAL_OPERATOR,
        "salt": SALT,
        "config_root": config_root,
        "request_id": "er_unknown",
    }
    with pytest.raises(PrivacyError) as caught:
        erase(storage, SENTINEL, **kwargs)  # type: ignore[arg-type]
    assert caught.value.code == "ERASURE_STORE_FAILED"
    assert _flags(engine, "er_unknown") == [("m_1", "training_input_unavailable")]

    assert requeue_failed(engine, "er_unknown")
    storage.broken = False
    outcome = erase(storage, SENTINEL, resume=True, **kwargs)  # type: ignore[arg-type]
    assert outcome.models_exposure_unknown == ("m_1",), "the first run's flag counts"
    assert outcome.status == "completed_with_exceptions", "nobody could check what m_1 learned from"
    assert _flags(engine, "er_unknown") == [("m_1", "training_input_unavailable")], "not flagged twice"
    record = erasure_request(engine, "er_unknown")
    assert record is not None and record.status == "completed_with_exceptions"
