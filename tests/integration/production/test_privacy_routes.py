"""The M48 API (DEC-746…749): who may use it, consent through it, and retention's dry run = real run.

Every request goes through the real app - the access dependency, the audit middleware and the
routes of `api/routes/privacy.py` - over a data directory the test lays out with the product's own
writers. The consent section ends in a real score flow (`tests/unit/test_run_score.py`'s stubs for
the three expensive stages, the shipped actions and export) reading the ledger the API wrote, so the
excluded counts asserted are the ones the pipeline produced from it. Erasure and access requests
have their own module, `test_privacy_erasure_api.py`.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.privacy import POLICIES
from engine.access.roles import Role
from engine.audit.events import AuditEvent, AuditQuery, principal_hash
from engine.config import RunMode, resolve_config
from engine.contracts import RunState
from engine.platform_db import PLATFORM_DB_FILENAME
from engine.privacy.contracts import CONSENT_REPORT_FILENAME
from engine.registry import LocalModelRegistry
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.integration.production.access_support import audit_log_at, bearer, local_app, make_user
from tests.unit.production.privacy_support import (
    customers,
    write_copy_messages,
    write_row_explanations,
    write_run,
    write_scores,
    write_upload,
)
from tests.unit.test_run_score import (
    RUN_ID,
    SCHEMA_KEY,
    UPLOAD_KEY,
    USE_CASE,
    StageStubs,
    make_schema,
    run_flow,
)

pytestmark = pytest.mark.integration

SALT = "acme"
PURPOSE = "marketing_communication"
PERSON = "C-555-PRIVATE"


class World:
    """One app with sign-in on, the deployment's client id `acme`, and a user per role combination."""

    def __init__(self, root: Path, **settings: Any) -> None:
        self.root = root
        self.app: FastAPI = local_app(root, **({"client_id": SALT} | settings))
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.admin = bearer(self.app, make_user(self.app, "admin", [Role.ADMIN]))
        self.viewer = bearer(self.app, make_user(self.app, "viewer", [Role.VIEWER]))
        self.all_but_admin = bearer(
            self.app, make_user(self.app, "everyone-else", [Role.VIEWER, Role.ANALYST, Role.APPROVER])
        )

    def events(self, **query: Any) -> tuple[AuditEvent, ...]:
        return audit_log_at(self.root).query(AuditQuery(**query))

    def events_of(self, response: Any) -> tuple[AuditEvent, ...]:
        request_id = response.headers["x-request-id"]
        return tuple(event for event in self.events(limit=10_000) if event.request_id == request_id)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def platform_bytes(root: Path) -> bytes:
    """The platform database and its WAL, as bytes: where the ledger and the audit trail live."""
    return b"".join(path.read_bytes() for path in sorted(root.glob(f"{PLATFORM_DB_FILENAME}*")))


# ---------------------------------------------------------------------------
# Who may use it
# ---------------------------------------------------------------------------
ADMIN_ROUTES = sorted(key for key, policy in POLICIES.items() if policy.role is Role.ADMIN)


def _url(path: str) -> str:
    return path.replace("{request_id}", "er_missing").replace("{run_id}", "r_missing")


def test_every_privacy_route_but_the_consent_report_is_admin_only() -> None:
    assert {key for key, policy in POLICIES.items() if policy.role is not Role.ADMIN} == {
        ("GET", "/privacy/runs/{run_id}/consent-report")
    }
    assert POLICIES[("GET", "/privacy/runs/{run_id}/consent-report")].role is Role.VIEWER


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES, ids=lambda value: str(value))
def test_every_other_role_together_is_refused(world: World, method: str, path: str) -> None:
    body = {"principal_id": PERSON}
    kwargs: dict[str, Any] = {"json": body} if method == "POST" else {}
    refused = world.client.request(method, _url(path), headers=world.all_but_admin, **kwargs)
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "ROLE_REQUIRED"
    assert refused.json()["detail"]["message"].startswith("Only an Admin can ")
    anonymous = world.client.request(method, _url(path), **kwargs)
    assert anonymous.status_code == 401


def test_a_refused_erasure_is_one_denied_event_that_holds_no_id(world: World) -> None:
    response = world.client.post(
        "/privacy/erasure", json={"principal_id": PERSON}, headers=world.all_but_admin
    )
    assert response.status_code == 403
    (event,) = world.events_of(response)
    assert (event.action, event.outcome) == ("privacy.erasure", "denied")
    assert PERSON not in event.model_dump_json()
    assert PERSON.encode() not in platform_bytes(world.root)


def test_a_viewer_may_read_a_consent_report(world: World) -> None:
    response = world.client.get("/privacy/runs/r_missing/consent-report", headers=world.viewer)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"


def test_the_policy_route_lists_the_purposes(world: World) -> None:
    response = world.client.get("/privacy/purposes", headers=world.admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert PURPOSE in {purpose["purpose_id"] for purpose in body["purposes"]}
    assert body["use_case_purposes"][USE_CASE] == PURPOSE
    assert body["erasure_mode"] in {"delete", "tombstone"}


# ---------------------------------------------------------------------------
# Consent: record, look up, import
# ---------------------------------------------------------------------------
def _record(world: World, status: str, days_ago: int, **extra: Any) -> Any:
    body = {
        "principal_id": PERSON,
        "purpose": PURPOSE,
        "status": status,
        "recorded_at": (utc_now() - timedelta(days=days_ago)).isoformat(),
        **extra,
    }
    return world.client.post("/privacy/consent", json=body, headers=world.admin)


def test_a_recorded_consent_is_stored_and_answered_as_a_hash_only(world: World) -> None:
    response = _record(world, "granted", 2)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["principal_hash"] == principal_hash(PERSON, salt=SALT)
    assert (body["client_id"], body["purpose"], body["status"]) == (SALT, PURPOSE, "granted")
    assert PERSON not in response.text
    (event,) = world.events_of(response)
    assert (event.action, event.outcome, event.object_id) == (
        "privacy.consent.record",
        "success",
        f"consent_{body['seq']}",
    )
    assert event.details["principal_hash"] == body["principal_hash"]
    assert PERSON.encode() not in platform_bytes(world.root)


def test_the_latest_record_decides_and_a_lookup_is_audited_under_its_own_id(world: World) -> None:
    assert _record(world, "granted", 3).status_code == 201
    assert _record(world, "withdrawn", 1).status_code == 201
    response = world.client.post(
        "/privacy/consent/lookup", json={"principal_id": PERSON}, headers=world.admin
    )
    assert response.status_code == 200, response.text
    body = response.json()
    states = {item["purpose"]: item["state"] for item in body["purposes"]}
    assert states == {PURPOSE: "withdrawn", "account_servicing": "none"}
    assert [record["status"] for record in body["records"]] == ["granted", "withdrawn"]
    assert PERSON not in response.text

    # the lookup's own id, never the person, is the audit object; the person is only a hash
    (event,) = world.events_of(response)
    assert body["lookup_id"] == response.headers["x-request-id"] == event.object_id
    assert (event.action, event.object_type) == ("privacy.consent.lookup", "consent_lookup")
    assert event.details["principal_hash"] == principal_hash(PERSON, salt=SALT)
    assert PERSON.encode() not in platform_bytes(world.root)

    # as of before the withdrawal, the grant decided
    earlier = (utc_now() - timedelta(days=2)).isoformat()
    past = world.client.post(
        "/privacy/consent/lookup", json={"principal_id": PERSON, "as_of": earlier}, headers=world.admin
    )
    assert {item["purpose"]: item["state"] for item in past.json()["purposes"]}[PURPOSE] == "valid"


def test_an_expired_grant_is_expired(world: World) -> None:
    expires = (utc_now() - timedelta(days=1)).isoformat()
    assert _record(world, "granted", 5, expires_at=expires).status_code == 201
    body = world.client.post(
        "/privacy/consent/lookup", json={"principal_id": PERSON}, headers=world.admin
    ).json()
    assert {item["purpose"]: item["state"] for item in body["purposes"]}[PURPOSE] == "expired"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"purpose": "cold_calling"}, "CONSENT_PURPOSE_UNKNOWN"),
        ({"recorded_at": (utc_now() + timedelta(days=2)).isoformat()}, "TIMESTAMP_IN_FUTURE"),
        ({"expires_at": (utc_now() - timedelta(days=30)).isoformat()}, "CONSENT_EXPIRY_BEFORE_RECORDED"),
        ({"client_id": "not a client/id"}, "CLIENT_ID_INVALID"),
    ],
)
def test_a_wrong_consent_record_is_refused_by_code(world: World, change: dict[str, str], code: str) -> None:
    response = _record(world, "granted", 2, **change)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == code
    assert PERSON not in response.text
    (event,) = world.events_of(response)
    assert event.outcome == "failed"


def test_without_a_client_id_anywhere_consent_needs_one(tmp_path: Path) -> None:
    world = World(tmp_path, client_id=None)
    response = _record(world, "granted", 1)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "CLIENT_ID_REQUIRED"
    assert _record(world, "granted", 1, client_id="cl_1").status_code == 201


def consent_csv(rows: int = 40, *, broken_row: int | None = None) -> bytes:
    """The unit test's ledger as a file: a quarter granted, withdrawn, expired and absent each."""
    now = utc_now()
    granted = (now - timedelta(days=10)).isoformat()
    withdrawn = (now - timedelta(days=9)).isoformat()
    expired = (now - timedelta(days=1)).isoformat()
    lines = ["principal_id,purpose,status,recorded_at,expires_at,source,notes"]
    for index in range(rows):
        key = f"C-{index:05d}"
        if index % 4 == 0:
            lines.append(f"{key},{PURPOSE},granted,{granted},,crm,x")
        elif index % 4 == 1:
            lines.append(f"{key},{PURPOSE},granted,{granted},,crm,x")
            lines.append(f"{key},{PURPOSE},withdrawn,{withdrawn},,crm,x")
        elif index % 4 == 2:
            lines.append(f"{key},{PURPOSE},granted,{granted},{expired},crm,x")
    if broken_row is not None:
        lines.insert(broken_row, f"{PERSON},{PURPOSE},maybe,{granted},,crm,x")
    return ("\n".join(lines) + "\n").encode()


def _import(world: World, data: bytes, **form: str) -> Any:
    return world.client.post(
        "/privacy/consent/imports",
        files={"file": ("consent.csv", data, "text/csv")},
        data=form,
        headers=world.admin,
    )


def test_a_clean_consent_file_is_imported(world: World) -> None:
    response = _import(world, consent_csv())
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["imported"], body["rows_read"], body["rows_imported"], body["errors"]) == (True, 40, 40, [])
    assert body["ignored_columns"] == ["notes"]
    (event,) = world.events_of(response)
    assert (event.action, event.outcome, event.details["count"]) == ("privacy.consent.import", "success", 40)


def test_a_file_with_one_bad_row_writes_nothing_and_says_where(world: World) -> None:
    response = _import(world, consent_csv(broken_row=3))
    assert response.status_code == 422, response.text
    body = response.json()
    assert (body["imported"], body["rows_imported"]) == (False, 0)
    (error,) = body["errors"]
    # inserted at list index 3, i.e. the file's fourth line, the header being row 1
    assert (error["row"], error["column"], error["code"]) == (4, "status", "CONSENT_STATUS_INVALID")
    assert PERSON not in response.text and "maybe" not in error["message"]
    lookup = world.client.post(
        "/privacy/consent/lookup", json={"principal_id": "C-00000"}, headers=world.admin
    )
    assert lookup.json()["records"] == []
    (event,) = world.events_of(response)
    assert event.outcome == "failed"

    partial = _import(world, consent_csv(broken_row=3), partial="true")
    assert partial.status_code == 201
    assert partial.json()["rows_imported"] == 40


def test_a_consent_file_that_is_not_utf8_is_refused(world: World) -> None:
    response = _import(world, "principal_id,purpose\n\xff\xfe".encode("latin-1"))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "CONSENT_FILE_NOT_UTF8"


# ---------------------------------------------------------------------------
# Consent in a scoring run, end to end
# ---------------------------------------------------------------------------
def score_here(root: Path, config_root: Path) -> LocalStorage:
    """The score flow of `tests/unit/test_run_score.py` over the app's own data directory."""
    storage = LocalStorage(root)
    storage.write_bytes(UPLOAD_KEY, b"customer_id,visits_last_7d\nC-00000,3\n")
    storage.write_model(SCHEMA_KEY, make_schema())
    run_flow(resolve_config(USE_CASE, root=config_root), storage, LocalModelRegistry(root.parent / "reg.db"))
    return storage


def suppression(world: World) -> dict[str, int]:
    response = world.client.get(f"/runs/{RUN_ID}/artefacts/scoring_summary.json", headers=world.viewer)
    assert response.status_code == 200, response.text
    return {item["reason"]: item["rows"] for item in response.json()["suppressed"]}


def test_a_ledger_imported_through_the_api_gates_the_next_scoring_run(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKETING_AI_CLIENT_ID", SALT)  # what the pipeline's own settings read
    StageStubs().install(monkeypatch)
    world = World(tmp_path / "data")
    assert _import(world, consent_csv()).status_code == 201
    storage = score_here(world.root, config_root)

    response = world.client.get(f"/privacy/runs/{RUN_ID}/consent-report", headers=world.viewer)
    assert response.status_code == 200, response.text
    report = response.json()
    assert (report["client_id"], report["purpose"], report["principals_checked"]) == (SALT, PURPOSE, 40)
    assert report["principals_with_valid_consent"] == 10
    assert (report["excluded_withdrawn"], report["excluded_expired"], report["excluded_no_consent"]) == (
        10,
        10,
        10,
    )
    assert report["excluded_total"] == 30
    assert suppression(world)["consent_false"] == 30, "Phase 1's own rule and counts report the exclusions"
    scores = pd.read_csv(io.BytesIO(storage.read_bytes(run_key(RUN_ID, "scores.csv"))))
    kept = scores.loc[scores["suppressed_reason"] != "consent_false", "customer_id"]
    assert set(kept) == {f"C-{index:05d}" for index in range(0, 40, 4)}


def test_with_an_empty_ledger_the_run_is_phase_1(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKETING_AI_CLIENT_ID", SALT)
    StageStubs().install(monkeypatch)
    world = World(tmp_path / "data")
    # the platform database and its consent table exist, but this client recorded nothing
    assert (
        world.client.post(
            "/privacy/consent/lookup", json={"principal_id": "C-1"}, headers=world.admin
        ).status_code
        == 200
    )
    storage = score_here(world.root, config_root)

    response = world.client.get(f"/privacy/runs/{RUN_ID}/consent-report", headers=world.viewer)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CONSENT_REPORT_NOT_FOUND"
    assert not storage.exists(run_key(RUN_ID, CONSENT_REPORT_FILENAME))
    assert "consent_false" not in suppression(world)


# ---------------------------------------------------------------------------
# Retention: the dry run is the real run
# ---------------------------------------------------------------------------
def build_store(root: Path) -> LocalStorage:
    """Something due and something recent in each kind, dated from now."""
    now = utc_now()
    old, recent = now - timedelta(days=400), now - timedelta(days=3)
    storage = LocalStorage(root)
    keys = ["C-1", "C-2", "C-3"]
    write_upload(storage, "u_old", customers(keys), created_at=old)
    write_upload(storage, "u_new", customers(keys), created_at=recent)
    write_run(
        storage, "r_old_train", mode=RunMode.TRAIN, created_at=old, upload_id="u_old", model_version_id="m_1"
    )
    storage.write_bytes(run_key("r_old_train", "model/predictor.pkl"), b"\x80\x04model-bytes")
    write_row_explanations(storage, "r_old_train", keys)
    write_run(storage, "r_old_score", mode=RunMode.SCORE, created_at=old, upload_id="u_old")
    write_scores(storage, "r_old_score", keys)
    write_copy_messages(storage, "r_old_score", keys)
    storage.write_text(
        run_key("r_old_score", "scoring_summary.json"),
        json.dumps({"rows_scored": 3, "sample_rows": [{"primary_key": "C-1"}]}, indent=2) + "\n",
    )
    write_run(storage, "r_new", mode=RunMode.SCORE, created_at=recent, upload_id="u_new")
    write_scores(storage, "r_new", keys)
    write_run(
        storage, "r_running", mode=RunMode.SCORE, created_at=old, state=RunState.RUNNING, upload_id="u_new"
    )
    return storage


def _plan(world: World, **params: str) -> Any:
    return world.client.get("/privacy/retention/plan", params=params, headers=world.admin)


def _apply(world: World, plan: dict[str, Any], **override: str) -> Any:
    body = {
        "plan_id": plan["plan"]["plan_id"],
        "planned_at": plan["plan"]["planned_at"],
        "plan_hash": plan["plan_hash"],
        **override,
    }
    return world.client.post("/privacy/retention/apply", json=body, headers=world.admin)


def test_the_dry_run_deletes_nothing_and_the_real_run_deletes_exactly_it(world: World) -> None:
    storage = build_store(world.root)
    before = set(storage.list_keys(""))
    response = _plan(world)
    assert response.status_code == 200, response.text
    plan = response.json()
    items = plan["plan"]["items"]
    delete = [item["key"] for item in items if item["action"] == "delete"]
    strip = [item["key"] for item in items if item["action"] == "strip_samples"]
    assert set(storage.list_keys("")) == before, "a dry run deletes nothing"
    assert set(storage.list_keys("uploads/u_old/")) <= set(delete)
    assert {"runs/r_old_score/scores.csv", "runs/r_old_score/copy_messages.csv"} <= set(delete)
    assert strip == ["runs/r_old_score/scoring_summary.json"]
    assert not any(key.startswith(("uploads/u_new/", "runs/r_new/")) for key in delete)
    assert plan["counts"] == {
        category: sum(1 for item in items if item["category"] == category)
        for category in {item["category"] for item in items}
    }
    assert world.events(action="privacy.retention.") == (), "the dry run is a plain read"

    applied = _apply(world, plan)
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["plan_id"] == plan["plan"]["plan_id"] and result["plan_hash"] == plan["plan_hash"]
    assert list(result["result"]["deleted"]) == delete
    assert list(result["result"]["stripped"]) == strip
    assert result["result"]["already_gone"] == []
    after = set(storage.list_keys(""))
    assert after == before - set(delete)
    assert storage.exists(run_key("r_old_train", "model/predictor.pkl")), "models are kept"
    assert storage.exists(run_key("r_old_score", "run.json"))
    summary = json.loads(storage.read_text(run_key("r_old_score", "scoring_summary.json")))
    assert summary["sample_rows"] == [] and summary["rows_scored"] == 3

    (event,) = world.events_of(applied)
    assert (event.action, event.object_type, event.object_id) == (
        "privacy.retention.apply",
        "retention_plan",
        plan["plan"]["plan_id"],
    )
    assert event.before_hash == plan["plan_hash"]
    assert (event.details["deleted"], event.details["count"], event.details["dry_run"]) == (
        len(delete),
        len(strip),
        False,
    )
    assert len(world.events(action="privacy.retention.apply")) == 1, "the engine wrote no second event"


def test_a_plan_that_no_longer_matches_is_refused_and_touches_nothing(world: World) -> None:
    storage = build_store(world.root)
    plan = _plan(world).json()
    before = set(storage.list_keys(""))

    tampered = _apply(world, plan, plan_hash="0" * 64)
    assert tampered.status_code == 409
    assert tampered.json()["detail"]["code"] == "RETENTION_PLAN_CHANGED"
    assert set(storage.list_keys("")) == before

    # the store changes between review and apply: a new old upload appears
    write_upload(storage, "u_late", customers(["C-9"]), created_at=utc_now() - timedelta(days=500))
    changed = _apply(world, plan)
    assert changed.status_code == 409
    assert storage.exists("uploads/u_late/upload.json") and storage.exists("uploads/u_old/upload.json")

    fresh = _plan(world).json()
    assert _apply(world, fresh).status_code == 200
    again = _apply(world, fresh)
    assert again.status_code == 409, "a plan is applied once"
    (event,) = world.events_of(again)
    assert (event.outcome, event.details["reason_code"]) == ("failed", "PLAN_CHANGED")


def test_a_plan_as_of_the_future_is_refused(world: World) -> None:
    future = (utc_now() + timedelta(days=30)).isoformat()
    response = _plan(world, as_of=future)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "TIMESTAMP_IN_FUTURE"
    past = (utc_now() - timedelta(days=1)).isoformat()
    rehearsal = _plan(world, as_of=past)
    assert rehearsal.status_code == 200
    assert datetime.fromisoformat(rehearsal.json()["plan"]["planned_at"]) == datetime.fromisoformat(past)


# ---------------------------------------------------------------------------
# A configuration with no privacy policy
# ---------------------------------------------------------------------------
def test_without_a_privacy_policy_the_controls_say_so(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    bare = tmp_path / "configs"
    shutil.copytree(config_root, bare)
    (bare / "privacy.yaml").unlink()
    world = World(tmp_path / "data")
    world.app.state.config_root = bare
    for method, path in (("GET", "/privacy/purposes"), ("GET", "/privacy/retention/plan")):
        response = world.client.request(method, path, headers=world.admin)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "PRIVACY_NOT_CONFIGURED"
    erasure = world.client.post("/privacy/erasure", json={"principal_id": PERSON}, headers=world.admin)
    assert erasure.status_code == 409
    (event,) = world.events_of(erasure)
    assert event.outcome == "failed"
