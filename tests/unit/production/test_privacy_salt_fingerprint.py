"""The privacy salt's fingerprint (Plan D, DEC-871): a changed salt is refused, not silently used.

Every consent record and erasure request stores a hash salted with `privacy_salt`. A deployment that
switches salt - from Phase 4b's client id, or to a different secret - would otherwise go on working
while every stored hash quietly stopped matching anyone. The platform database keeps a fingerprint of
the salt (never the salt) and `privacy_salt` compares against it.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session

from engine.access.roles import Role
from engine.platform_db import PLATFORM_DB_FILENAME, PlatformSettingRow, sqlite_engine
from engine.privacy import config as privacy_config_module
from engine.privacy.config import SALT_FINGERPRINT_KEY, privacy_salt, salt_fingerprint
from engine.privacy.consent import ConsentLedger
from engine.privacy.contracts import ConsentStatus
from engine.privacy.tables import create_privacy_tables
from engine.settings import Settings, SettingsError
from tests.integration.production.access_support import bearer, local_app, make_user

ONE = "salt-number-one-0001"
TWO = "salt-number-two-0002"


def _settings(salt: str | None = None, **extra: object) -> Settings:
    return Settings(privacy_salt=salt, **extra)  # type: ignore[arg-type]


def _stored_fingerprint(engine: Engine) -> str | None:
    with Session(engine) as session:
        row = session.get(PlatformSettingRow, SALT_FINGERPRINT_KEY)
        return None if row is None else row.value


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.WARNING]


def test_the_fingerprint_is_kept_and_a_different_salt_is_refused(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    assert privacy_salt(_settings(ONE), engine=engine) == ONE
    stored = _stored_fingerprint(engine)
    assert stored == salt_fingerprint(ONE)
    assert stored != hashlib.sha256(ONE.encode()).hexdigest(), "domain-separated"
    assert ONE.encode() not in (tmp_path / PLATFORM_DB_FILENAME).read_bytes(), "never the salt itself"

    with pytest.raises(SettingsError) as excinfo:
        privacy_salt(_settings(TWO), engine=engine)
    assert (excinfo.value.code, excinfo.value.env_var) == ("SETTING_INVALID", "MARKETING_AI_PRIVACY_SALT")
    assert "MARKETING_AI_PRIVACY_SALT" in excinfo.value.message
    assert "different salt" in excinfo.value.message
    assert "salt-number" not in excinfo.value.message, "neither salt is quoted"
    assert _stored_fingerprint(engine) == stored, "the recorded fingerprint is not replaced"

    assert privacy_salt(_settings(ONE), engine=engine) == ONE, "the original salt still works"


def test_a_new_process_still_refuses_the_other_salt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    privacy_salt(_settings(ONE), engine=engine)
    monkeypatch.setattr(privacy_config_module, "_VERIFIED", {})  # nothing remembered in memory
    with pytest.raises(SettingsError) as excinfo:
        privacy_salt(_settings(TWO), engine=engine)
    assert excinfo.value.code == "SETTING_INVALID"
    assert privacy_salt(_settings(ONE), engine=engine) == ONE


def test_switching_from_the_generated_salt_to_a_secret_is_refused(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    generated = privacy_salt(_settings(client_id="acme"), engine=engine)
    with pytest.raises(SettingsError) as excinfo:
        privacy_salt(_settings("a-brand-new-secret-01"), engine=engine)
    assert excinfo.value.code == "SETTING_INVALID"
    assert privacy_salt(_settings(generated), engine=engine) == generated, "the same salt, set as a secret"


def test_hashes_from_before_the_fingerprint_are_warned_about_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    ConsentLedger(engine, salt="acme-phase-4b-salt").record(  # Phase 4b salted with the client id
        client_id="acme",
        principal_id="C-OLD-1",
        purpose="marketing_communication",
        status=ConsentStatus.GRANTED,
        source="test",
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with caplog.at_level(logging.WARNING, logger="engine.privacy.config"):
        assert privacy_salt(_settings(ONE), engine=engine) == ONE
    (warning,) = _warnings(caplog)
    message = warning.getMessage()
    assert "old salt (the client id)" in message and "re-imported" in message
    assert ONE not in message and "C-OLD-1" not in message and "acme" not in message, "no values logged"
    assert _stored_fingerprint(engine) == salt_fingerprint(ONE), "recorded all the same"

    caplog.clear()
    monkeypatch.setattr(privacy_config_module, "_VERIFIED", {})  # a restart: the database decides
    with caplog.at_level(logging.WARNING, logger="engine.privacy.config"):
        privacy_salt(_settings(ONE), engine=engine)
    assert _warnings(caplog) == [], "once: the fingerprint now exists"


def test_an_erasure_register_from_before_the_fingerprint_is_warned_about(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from engine.privacy.erasure import queue_request

    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    queue_request(
        engine,
        request_id="er_old",
        principal_hash="0" * 64,
        client_id=None,
        mode="delete",
        requested_by="u_1",
        requested_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="completed",
    )
    with caplog.at_level(logging.WARNING, logger="engine.privacy.config"):
        privacy_salt(_settings(ONE), engine=engine)
    assert len(_warnings(caplog)) == 1


def test_an_empty_database_records_the_fingerprint_without_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    create_privacy_tables(engine)
    with caplog.at_level(logging.WARNING, logger="engine.privacy.config"):
        privacy_salt(_settings(ONE), engine=engine)
    assert _warnings(caplog) == []
    assert _stored_fingerprint(engine) == salt_fingerprint(ONE)


def test_without_an_engine_nothing_is_recorded_or_checked() -> None:
    assert privacy_salt(_settings(ONE), engine=None) == ONE
    assert privacy_salt(_settings(TWO), engine=None) == TWO


def test_an_api_route_answers_503_when_the_salt_changed(tmp_path: Path) -> None:
    privacy_salt(_settings(ONE), engine=sqlite_engine(tmp_path / PLATFORM_DB_FILENAME))
    app = local_app(tmp_path, client_id="acme", privacy_salt=TWO)
    client = TestClient(app, raise_server_exceptions=False)
    admin = bearer(app, make_user(app, "admin", [Role.ADMIN]))
    response = client.post("/privacy/erasure", json={"principal_id": "C-1"}, headers=admin)
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "SETTING_INVALID"
    assert "salt-number" not in response.text
