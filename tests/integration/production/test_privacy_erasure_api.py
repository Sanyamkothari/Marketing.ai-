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
import re
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.audit.events import AuditEvent, AuditQuery, principal_hash
from engine.privacy import erasure as erasure_module
from engine.privacy.errors import PrivacyError
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


class Api:
    def __init__(self, planted: Planted) -> None:
        self.root = planted.root
        # the deployment's client id is the salt the planted ledger was hashed with (privacy_salt)
        app = local_app(planted.root, client_id=SALT)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.admin = bearer(app, make_user(app, "admin", [Role.ADMIN]))
        self.analyst = bearer(app, make_user(app, "analyst", [Role.ANALYST, Role.APPROVER, Role.VIEWER]))

    def events(self, **query: object) -> tuple[AuditEvent, ...]:
        return audit_log_at(self.root).query(AuditQuery(limit=10_000, **query))  # type: ignore[arg-type]

    def events_of(self, response: object) -> tuple[AuditEvent, ...]:
        request_id = response.headers["x-request-id"]  # type: ignore[attr-defined]
        return tuple(event for event in self.events() if event.request_id == request_id)


@pytest.fixture
def planted(tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> Planted:
    return plant(tmp_path / "data", config_root, monkeypatch)


@pytest.fixture
def api(planted: Planted) -> Api:
    return Api(planted)


def _erase(api: Api, headers: dict[str, str] | None = None) -> object:
    return api.client.post(
        "/privacy/erasure", json={"principal_id": SENTINEL, "client_id": CLIENT}, headers=headers or api.admin
    )


# ---------------------------------------------------------------------------
# Erasure
# ---------------------------------------------------------------------------
def test_erasure_through_the_api_leaves_no_byte_of_the_id_anywhere(api: Api) -> None:
    holding = files_holding(api.root, SENTINEL)
    assert len(holding) >= 15, "the guard against a vacuous pass: the id must have been everywhere"

    response = _erase(api)
    assert response.status_code == 201, response.text  # type: ignore[attr-defined]
    outcome = response.json()  # type: ignore[attr-defined]
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
    assert SENTINEL not in response.text  # type: ignore[attr-defined]

    # every byte of the data directory, the audit trail's database and WAL included
    assert files_holding(api.root, SENTINEL) == []

    # exactly one audit event for the request, under the erasure request's id, the person as a hash
    (event,) = api.events_of(response)
    assert (event.action, event.outcome, event.object_type, event.object_id) == (
        "privacy.erasure",
        "success",
        "erasure_request",
        outcome["request_id"],
    )
    assert event.details["principal_hash"] == principal_hash(SENTINEL, salt=SALT)
    assert event.details["request_kind"] == "erasure"
    assert event.details["models_flagged"] == 2
    assert event.details["deleted"] == outcome["rows_deleted"]
    assert SENTINEL not in event.model_dump_json()
    assert len(api.events(action="privacy.erasure")) == 1, "the engine was given no log of its own"


def test_the_register_and_the_retraining_flags_are_readable_afterwards(api: Api) -> None:
    outcome = _erase(api).json()  # type: ignore[attr-defined]
    request_id = outcome["request_id"]

    read = api.client.get(f"/privacy/erasure/{request_id}", headers=api.admin)
    assert read.status_code == 200, read.text
    record = read.json()
    assert (record["status"], record["principal_hash"], record["client_id"]) == (
        "completed",
        outcome["principal_hash"],
        CLIENT,
    )
    assert record["models_flagged"] == [MODEL_ON_UPLOAD, MODEL_ON_DATASET]
    (event,) = api.events_of(read)
    assert (event.action, event.object_id) == ("privacy.erasure.read", request_id), "an audited read"

    listing = api.client.get("/privacy/erasure", headers=api.admin)
    assert [item["request_id"] for item in listing.json()["requests"]] == [request_id]
    assert api.events_of(listing) == (), "the register listing is a plain read"

    flags = api.client.get("/privacy/retrain-flags", headers=api.admin).json()["flags"]
    assert sorted(flag["model_id"] for flag in flags) == sorted([MODEL_ON_UPLOAD, MODEL_ON_DATASET])
    assert {flag["request_id"] for flag in flags} == {request_id}

    missing = api.client.get("/privacy/erasure/er_nope", headers=api.admin)
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "ERASURE_REQUEST_NOT_FOUND"


def test_a_non_admin_cannot_erase_and_nothing_changes(api: Api) -> None:
    before = files_holding(api.root, SENTINEL)
    response = _erase(api, api.analyst)
    assert response.status_code == 403  # type: ignore[attr-defined]
    assert response.json()["detail"]["message"] == "Only an Admin can erase a person's data."  # type: ignore[attr-defined]
    assert files_holding(api.root, SENTINEL) == before
    (event,) = api.events_of(response)
    assert (event.outcome, event.object_id) == ("denied", None)


def test_a_failed_erasure_is_a_500_that_names_its_request_and_is_recorded_as_failed(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    def incomplete(*args: object, **kwargs: object) -> None:
        raise PrivacyError("ERASURE_INCOMPLETE", "1 rewritten file(s) still hold the principal.")

    monkeypatch.setattr(erasure_module, "_erase", incomplete)
    response = _erase(api)
    assert response.status_code == 500  # type: ignore[attr-defined]
    detail = response.json()["detail"]  # type: ignore[attr-defined]
    assert detail["code"] == "ERASURE_INCOMPLETE"
    match = re.search(r"\ber_[0-9a-f]{20}\b", detail["message"])
    assert match is not None
    (event,) = api.events_of(response)
    assert (event.outcome, event.object_id, event.details["reason_code"]) == (
        "failed",
        match.group(0),
        "ERASURE_INCOMPLETE",
    )
    assert event.details["principal_hash"] == principal_hash(SENTINEL, salt=SALT)
    record = api.client.get(f"/privacy/erasure/{match.group(0)}", headers=api.admin).json()
    assert (record["status"], record["error_code"]) == ("failed", "ERASURE_INCOMPLETE")


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
    assert _erase(api).status_code == 201  # type: ignore[attr-defined]
    response = api.client.post("/privacy/access-requests", json={"principal_id": SENTINEL}, headers=api.admin)
    assert response.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert not any(name.startswith("records/") for name in names)
    assert "erasure_history.json" in names
