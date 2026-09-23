"""Erasure and access requests through the API (plan section 4: "erasure removes the ID from every store").

The data directory is `tests/unit/production/sentinel_store.plant`'s: one distinctive id planted by
the product's own writers in uploads, onboarding sources, a built dataset, a real scoring run's
scores and explanations, copy messages, a root-cause summary, a knowledge index, the LLM cache and
the consent ledger. The app is pointed at that directory and the requests go through the real
access dependency and audit middleware. Afterwards the *whole directory* - the platform database
and its WAL, where the audit trail lives, included - is searched byte by byte.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.routes.privacy import _STORE_REWRITE_LOCK
from engine.access.roles import Role
from engine.audit.events import AuditEvent, AuditQuery, principal_hash
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy import erasure as erasure_module
from engine.privacy.consent import principal_key
from engine.privacy.erasure import queue_request
from engine.privacy.erasure_jobs import ErasureJobs
from engine.privacy.errors import PrivacyError
from engine.storage import LocalStorage, StorageError
from engine.utils.time import utc_now
from tests.integration.production.access_support import audit_log_at, bearer, local_app, make_user
from tests.unit.production.sentinel_store import (
    CLIENT,
    MODEL_ON_DATASET,
    MODEL_ON_UPLOAD,
    SALT,
    SENTINEL,
    Planted,
    files_holding,
    plant,
)

pytestmark = pytest.mark.integration


class FlakyStorage:
    """The planted store, whose writes under `prefix` fail `failures` times before they work (DEC-863)."""

    def __init__(self, inner: LocalStorage, prefix: str, failures: int) -> None:
        self._inner = inner
        self.prefix = prefix
        self.failures = failures

    def write_bytes(self, key: str, data: bytes) -> None:
        if key.startswith(self.prefix) and self.failures > 0:
            self.failures -= 1
            raise StorageError("WRITE_FAILED", "the disk said no", key=key)
        self._inner.write_bytes(key, data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class Api:
    def __init__(self, planted: Planted) -> None:
        self.root = planted.root
        # the deployment's secret salt is the one the planted ledger was hashed with (DEC-860)
        self.app = local_app(planted.root, client_id="acme", privacy_salt=SALT)
        self.waits: list[int] = []
        # the job's backoff is recorded rather than slept, so a retry test takes no wall-clock time
        self.app.state.erasure_jobs = ErasureJobs(rewrite_lock=_STORE_REWRITE_LOCK, sleep=self.waits.append)  # type: ignore[arg-type]
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.admin = bearer(self.app, make_user(self.app, "admin", [Role.ADMIN]))
        self.analyst = bearer(
            self.app, make_user(self.app, "analyst", [Role.ANALYST, Role.APPROVER, Role.VIEWER])
        )

    def events(self, **query: object) -> tuple[AuditEvent, ...]:
        return audit_log_at(self.root).query(AuditQuery(limit=10_000, **query))  # type: ignore[arg-type]

    def events_of(self, response: object) -> tuple[AuditEvent, ...]:
        request_id = response.headers["x-request-id"]  # type: ignore[attr-defined]
        return tuple(event for event in self.events() if event.request_id == request_id)

    def finish(self, request_id: str) -> dict[str, Any]:
        """Wait for the background job, then read the completion report."""
        jobs = self.app.state.erasure_jobs
        assert isinstance(jobs, ErasureJobs)
        jobs.wait(request_id, timeout=120)
        read = self.client.get(f"/privacy/erasure/{request_id}", headers=self.admin)
        assert read.status_code == 200, read.text
        return dict(read.json())

    def flaky(self, prefix: str, failures: int) -> FlakyStorage:
        storage = FlakyStorage(LocalStorage(self.root), prefix, failures)
        self.app.state.storage = storage
        return storage


@pytest.fixture
def planted(tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> Planted:
    return plant(tmp_path / "data", config_root, monkeypatch)


@pytest.fixture
def api(planted: Planted) -> Api:
    return Api(planted)


def _erase(api: Api, headers: dict[str, str] | None = None) -> Any:
    return api.client.post(
        "/privacy/erasure", json={"principal_id": SENTINEL, "client_id": CLIENT}, headers=headers or api.admin
    )


def _retry(
    api: Api, request_id: str, principal_id: str = SENTINEL, headers: dict[str, str] | None = None
) -> Any:
    return api.client.post(
        f"/privacy/erasure/{request_id}/retry",
        json={"principal_id": principal_id},
        headers=headers or api.admin,
    )


# ---------------------------------------------------------------------------
# Erasure
# ---------------------------------------------------------------------------
def test_erasure_through_the_api_leaves_no_byte_of_the_id_anywhere(api: Api) -> None:
    holding = files_holding(api.root, SENTINEL)
    assert len(holding) >= 15, "the guard against a vacuous pass: the id must have been everywhere"

    response = _erase(api)
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["status"] == "queued"
    assert accepted["principal_hash"] == principal_hash(SENTINEL, salt=SALT)
    assert accepted["progress_url"] == f"/privacy/erasure/{accepted['request_id']}/progress"
    assert SENTINEL not in response.text

    outcome = api.finish(accepted["request_id"])
    assert outcome["status"] == "completed"
    assert outcome["principal_hash"] == principal_hash(SENTINEL, salt=SALT)
    assert set(outcome["store_counts"]) >= {
        "uploads",
        "sources",
        "datasets",
        "scores",
        "row_explanations",
        "copy_messages",
        "llm_cache",
    }
    assert outcome["models_flagged"] == [MODEL_ON_UPLOAD, MODEL_ON_DATASET]
    assert {row["store"] for row in outcome["progress"]} >= set(outcome["store_counts"])
    assert all(
        row["status"] == "done" and row["files_done"] == row["files_total"] for row in outcome["progress"]
    )

    # every byte of the data directory, the audit trail's database and WAL included
    assert files_holding(api.root, SENTINEL) == []

    # audited at the start (the request's own event) and at the end (the job's), the person as a hash
    (start,) = api.events_of(response)
    assert (start.action, start.outcome, start.object_type, start.object_id) == (
        "privacy.erasure",
        "success",
        "erasure_request",
        accepted["request_id"],
    )
    assert start.details["principal_hash"] == principal_hash(SENTINEL, salt=SALT)
    assert (start.details["request_kind"], start.details["trigger"]) == ("erasure", "queued")
    (end,) = api.events(action="privacy.erasure.complete")
    assert (end.outcome, end.object_id, end.actor_id) == ("success", accepted["request_id"], start.actor_id)
    assert end.details["principal_hash"] == principal_hash(SENTINEL, salt=SALT)
    assert end.details["models_flagged"] == 2
    assert end.details["deleted"] == outcome["rows_deleted"]
    for event in (start, end):
        assert SENTINEL not in event.model_dump_json()
    assert len(api.events(action="privacy.erasure")) == 1


def test_the_register_and_the_retraining_flags_are_readable_afterwards(api: Api) -> None:
    accepted = _erase(api).json()
    request_id = accepted["request_id"]
    api.finish(request_id)

    read = api.client.get(f"/privacy/erasure/{request_id}", headers=api.admin)
    assert read.status_code == 200, read.text
    record = read.json()
    assert (record["status"], record["principal_hash"], record["client_id"]) == (
        "completed",
        accepted["principal_hash"],
        CLIENT,
    )
    assert record["models_flagged"] == [MODEL_ON_UPLOAD, MODEL_ON_DATASET]
    (event,) = api.events_of(read)
    assert (event.action, event.object_id) == ("privacy.erasure.read", request_id), "an audited read"

    listing = api.client.get("/privacy/erasure", headers=api.admin)
    assert [item["request_id"] for item in listing.json()["requests"]] == [request_id]
    assert api.events_of(listing) == (), "the register listing is a plain read"

    progress = api.client.get(f"/privacy/erasure/{request_id}/progress", headers=api.admin)
    assert progress.status_code == 200
    assert progress.json()["status"] == "completed"
    assert "principal_hash" not in progress.text and api.events_of(progress) == (), "polling is a plain read"

    flags = api.client.get("/privacy/retrain-flags", headers=api.admin).json()["flags"]
    assert sorted(flag["model_id"] for flag in flags) == sorted([MODEL_ON_UPLOAD, MODEL_ON_DATASET])
    assert {flag["request_id"] for flag in flags} == {request_id}

    for path in ("/privacy/erasure/er_nope", "/privacy/erasure/er_nope/progress"):
        missing = api.client.get(path, headers=api.admin)
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "ERASURE_REQUEST_NOT_FOUND"


def test_a_non_admin_cannot_erase_and_nothing_changes(api: Api) -> None:
    before = files_holding(api.root, SENTINEL)
    response = _erase(api, api.analyst)
    assert response.status_code == 403
    assert response.json()["detail"]["message"] == "Only an Admin can erase a person's data."
    assert files_holding(api.root, SENTINEL) == before
    (event,) = api.events_of(response)
    assert (event.outcome, event.object_id) == ("denied", None)


def test_a_non_admin_can_neither_follow_nor_retry_an_erasure(api: Api) -> None:
    request_id = _erase(api).json()["request_id"]
    api.finish(request_id)
    progress = api.client.get(f"/privacy/erasure/{request_id}/progress", headers=api.analyst)
    assert progress.status_code == 403
    assert progress.json()["detail"]["message"] == "Only an Admin can follow an erasure request."
    retry = _retry(api, request_id, headers=api.analyst)
    assert retry.status_code == 403
    assert retry.json()["detail"]["message"] == "Only an Admin can retry an erasure request."
    (event,) = api.events_of(retry)
    assert (event.action, event.outcome) == ("privacy.erasure.retry", "denied")
    assert SENTINEL not in event.model_dump_json()


def test_a_failed_erasure_is_recorded_as_failed_and_audited_at_the_end(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    def incomplete(*args: object, **kwargs: object) -> None:
        raise PrivacyError("ERASURE_INCOMPLETE", "1 rewritten file(s) still hold the principal.")

    monkeypatch.setattr(erasure_module, "_erase", incomplete)
    response = _erase(api)
    assert response.status_code == 202
    request_id = response.json()["request_id"]
    record = api.finish(request_id)
    assert (record["status"], record["error_code"]) == ("failed", "ERASURE_INCOMPLETE")
    (end,) = api.events(action="privacy.erasure.complete")
    assert (end.outcome, end.object_id, end.details["reason_code"]) == (
        "failed",
        request_id,
        "ERASURE_INCOMPLETE",
    )
    assert end.details["principal_hash"] == principal_hash(SENTINEL, salt=SALT)


def test_a_store_that_fails_briefly_is_retried_within_the_job(api: Api) -> None:
    storage = api.flaky("runs/", failures=2)
    record = api.finish(_erase(api).json()["request_id"])
    assert record["status"] == "completed", record
    assert storage.failures == 0
    runs = [
        row for row in record["progress"] if row["store"] in {"scores", "row_explanations", "copy_messages"}
    ]
    assert runs and any(row["attempts"] >= 2 for row in runs), "the failing store took more than one attempt"
    assert all(row["status"] == "done" for row in record["progress"])
    assert api.waits == [2.0, 8.0][: len(api.waits)] and api.waits, "the job backed off between attempts"
    assert files_holding(api.root, SENTINEL) == []


def test_a_store_that_keeps_failing_fails_the_request_and_a_retry_finishes_it(api: Api) -> None:
    storage = api.flaky("uploads/", failures=10_000)
    request_id = _erase(api).json()["request_id"]
    failed = api.finish(request_id)
    assert (failed["status"], failed["error_code"]) == ("failed", "ERASURE_STORE_FAILED")
    by_store = {row["store"]: row for row in failed["progress"]}
    assert by_store["uploads"]["status"] == "failed"
    assert by_store["uploads"]["attempts"] == 3 and by_store["uploads"]["error_code"] == "STORE_WRITE_FAILED"
    assert all(
        row["status"] == "done" for store, row in by_store.items() if store != "uploads"
    ), "the other stores carried on"
    assert failed["models_flagged"] == [
        MODEL_ON_UPLOAD,
        MODEL_ON_DATASET,
    ], "flagged even though a store failed"
    left = files_holding(api.root, SENTINEL)
    assert left and all("uploads" in str(path) for path in left), left
    (end,) = api.events(action="privacy.erasure.complete")
    assert (end.outcome, end.details["reason_code"], end.details["failed_stores"]) == (
        "failed",
        "ERASURE_STORE_FAILED",
        "uploads",
    )

    wrong = _retry(api, request_id, principal_id="SOMEBODY-ELSE")
    assert wrong.status_code == 422 and wrong.json()["detail"]["code"] == "PRINCIPAL_MISMATCH"

    storage.failures = 0
    again = _retry(api, request_id)
    assert again.status_code == 202, again.text
    (retry_event,) = api.events_of(again)
    assert (retry_event.action, retry_event.details["trigger"]) == ("privacy.erasure.retry", "retry")
    done = api.finish(request_id)
    assert (done["status"], done["error_code"]) == ("completed", None)
    assert done["store_counts"]["uploads"]["files"] >= 1
    assert done["files_rewritten"] > failed["files_rewritten"], "the retry's counts add to the first run's"
    assert files_holding(api.root, SENTINEL) == []
    flags = api.client.get("/privacy/retrain-flags", headers=api.admin).json()["flags"]
    assert sorted(flag["model_id"] for flag in flags) == sorted(
        [MODEL_ON_UPLOAD, MODEL_ON_DATASET]
    ), "no double flag"

    finished = _retry(api, request_id)
    assert finished.status_code == 409 and finished.json()["detail"]["code"] == "ERASURE_NOT_RETRYABLE"


def _failed_request(api: Api, body: dict[str, str] | None = None) -> tuple[str, FlakyStorage]:
    """A request whose uploads would not take a write, so it ends `failed`; the storage is returned to fix."""
    storage = api.flaky("uploads/", failures=10_000)
    response = api.client.post(
        "/privacy/erasure", json=body or {"principal_id": SENTINEL, "client_id": CLIENT}, headers=api.admin
    )
    assert response.status_code == 202, response.text
    request_id = str(response.json()["request_id"])
    assert api.finish(request_id)["status"] == "failed"
    return request_id, storage


def test_a_retry_is_claimed_at_once_and_a_second_retry_is_refused(api: Api) -> None:
    """DEC-869: the row reads `queued` as soon as the retry is answered, so a double submit starts one job."""
    request_id, storage = _failed_request(api)
    storage.failures = 0
    with _STORE_REWRITE_LOCK:  # the job cannot start until the test lets it
        first = _retry(api, request_id)
        assert first.status_code == 202, first.text
        progress = api.client.get(f"/privacy/erasure/{request_id}/progress", headers=api.admin).json()
        assert (progress["status"], progress["error_code"], progress["completed_at"]) == (
            "queued",
            None,
            None,
        )
        second = _retry(api, request_id)
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["code"] == "ERASURE_NOT_RETRYABLE"
        assert "queued" in second.json()["detail"]["message"]
        (refused,) = api.events_of(second)
        assert refused.details["reason_code"] == "ERASURE_NOT_RETRYABLE"
    assert api.finish(request_id)["status"] == "completed"
    ends = api.events(action="privacy.erasure.complete")
    assert sorted(event.outcome for event in ends) == ["failed", "success"], "one job for the retry, not two"


def test_a_retry_deletes_the_consent_history_the_request_was_made_for(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-870: no client named means every client's history - on the retry too, though `client_id` is set."""
    jobs = api.app.state.erasure_jobs
    assert isinstance(jobs, ErasureJobs)
    submitted: list[bool] = []
    original = jobs.submit

    def spy(**kwargs: Any) -> None:
        submitted.append(kwargs["history_all_clients"])
        original(**kwargs)

    monkeypatch.setattr(jobs, "submit", spy)
    request_id, storage = _failed_request(api, {"principal_id": SENTINEL})
    record = api.client.get(f"/privacy/erasure/{request_id}", headers=api.admin).json()
    assert (record["client_id"], record["history_all_clients"]) == ("acme", True)
    storage.failures = 0
    assert _retry(api, request_id).status_code == 202
    assert api.finish(request_id)["status"] == "completed"
    assert submitted == [True, True]

    named = _erase(api).json()["request_id"]  # a client named: only that client's history
    assert api.finish(named)["history_all_clients"] is False
    assert submitted[2:] == [False]


def test_a_request_a_stopped_process_left_running_is_failed_at_startup_and_can_be_retried(api: Api) -> None:
    """DEC-869: the jobs live in the API process; one that stopped leaves rows nothing would finish."""
    engine = sqlite_engine(api.root / PLATFORM_DB_FILENAME)
    for request_id, status in (("er_orphan_running", "in_progress"), ("er_orphan_queued", "queued")):
        queue_request(
            engine,
            request_id=request_id,
            principal_hash=principal_hash(principal_key(SENTINEL), salt=SALT),
            client_id=CLIENT,
            mode="delete",
            requested_by="u_gone",
            requested_at=utc_now(),
            status=status,
        )
    with TestClient(api.app) as started:  # entering the client runs the startup hooks
        for request_id in ("er_orphan_running", "er_orphan_queued"):
            record = started.get(f"/privacy/erasure/{request_id}", headers=api.admin).json()
            assert (record["status"], record["error_code"]) == ("failed", "ERASURE_INTERRUPTED")
        retry = started.post(
            "/privacy/erasure/er_orphan_running/retry", json={"principal_id": SENTINEL}, headers=api.admin
        )
        assert retry.status_code == 202, retry.text
    assert api.finish("er_orphan_running")["status"] == "completed"
    assert files_holding(api.root, SENTINEL) == []


def test_starting_the_api_on_a_fresh_directory_creates_no_platform_database(tmp_path: Path) -> None:
    from api.main import create_app
    from engine.settings import Settings

    app = create_app(data_dir=tmp_path)
    app.state.settings = Settings(data_dir=tmp_path, privacy_salt=SALT)  # type: ignore[arg-type]
    with TestClient(app) as started:
        assert started.get("/healthz").status_code == 200
    assert not (tmp_path / PLATFORM_DB_FILENAME).exists(), "nothing to sweep, and nothing created"


class CrashingStorage(FlakyStorage):
    """Writes under `prefix` fail with an error no retry within the job would help (a bug, a crash)."""

    def write_bytes(self, key: str, data: bytes) -> None:
        if key.startswith(self.prefix) and self.failures > 0:
            raise RuntimeError("the worker fell over")
        self._inner.write_bytes(key, data)


def test_a_job_that_crashes_mid_rewrite_has_flagged_the_models_already(api: Api) -> None:
    """DEC-870: flagged before any rewrite, so the retry - which cannot find the person any more in the
    stores the first run erased - does not lose the flag."""
    storage = CrashingStorage(LocalStorage(api.root), "uploads/", failures=1)
    api.app.state.storage = storage
    request_id = _erase(api).json()["request_id"]
    failed = api.finish(request_id)
    assert (failed["status"], failed["error_code"]) == ("failed", "ERASURE_FAILED")
    assert failed["models_flagged"] == [MODEL_ON_UPLOAD, MODEL_ON_DATASET]
    flags = api.client.get("/privacy/retrain-flags", headers=api.admin).json()["flags"]
    assert sorted(flag["model_id"] for flag in flags) == sorted([MODEL_ON_UPLOAD, MODEL_ON_DATASET])

    storage.failures = 0
    assert _retry(api, request_id).status_code == 202
    done = api.finish(request_id)
    assert done["status"] == "completed"
    assert done["models_flagged"] == [MODEL_ON_UPLOAD, MODEL_ON_DATASET]
    again = api.client.get("/privacy/retrain-flags", headers=api.admin).json()["flags"]
    assert len(again) == 2, "no flag twice"
    assert files_holding(api.root, SENTINEL) == []


# ---------------------------------------------------------------------------
# Access requests
# ---------------------------------------------------------------------------
def test_the_access_export_holds_every_file_the_id_is_in_and_stores_nothing(api: Api) -> None:
    holding = files_holding(api.root, SENTINEL)
    response = api.client.post(
        "/privacy/access-requests", json={"principal_id": SENTINEL, "client_id": CLIENT}, headers=api.admin
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert SENTINEL not in response.headers["content-disposition"]

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    assert names[0] == "manifest.json"
    for key in holding:
        members = [name for name in names if name.startswith("records/") and key.replace("/", "__") in name]
        assert len(members) == 1, (key, members)
        assert SENTINEL in archive.read(members[0]).decode("utf-8"), members[0]
    assert b"withdrawn" in archive.read("consent_history.json"), "the consent history is included"

    # returned, not written: the same files hold the id afterwards, and no new one does
    assert files_holding(api.root, SENTINEL) == holding

    (event,) = api.events_of(response)
    manifest = archive.read("manifest.json").decode("utf-8")
    assert (event.action, event.object_type) == ("privacy.access_request", "access_request")
    assert event.object_id is not None and event.object_id.startswith("ar_") and event.object_id in manifest
    assert event.after_hash == hashlib.sha256(response.content).hexdigest()
    assert event.details["principal_hash"] == principal_hash(SENTINEL, salt=SALT)
    assert event.details["count"] == len(names)
    assert SENTINEL not in event.model_dump_json()


def test_after_erasure_the_access_export_finds_nothing(api: Api) -> None:
    accepted = _erase(api)
    assert accepted.status_code == 202
    assert api.finish(accepted.json()["request_id"])["status"] == "completed"
    response = api.client.post("/privacy/access-requests", json={"principal_id": SENTINEL}, headers=api.admin)
    assert response.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert not any(name.startswith("records/") for name in names)
    assert "erasure_history.json" in names
