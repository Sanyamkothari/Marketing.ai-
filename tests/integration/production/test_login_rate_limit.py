"""Sign-in rate limiting through the API (Plan D M54, DEC-861): 429, `Retry-After`, the audit trail.

The throttle on `app.state` is replaced by one with a controlled clock, so a lock-out and its end are
tested without sleeping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.access.throttle import LoginThrottle
from engine.audit.events import AuditQuery
from engine.settings import Settings
from tests.integration.production.access_support import PASSWORD, audit_log_at, local_app, make_user

pytestmark = pytest.mark.integration


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def app(tmp_path: Path, clock: Clock) -> FastAPI:
    built = local_app(tmp_path)
    built.state.login_throttle = LoginThrottle(
        max_failures_per_account=3,
        max_failures_per_address=10,
        window_seconds=900,
        lockout_seconds=600,
        clock=clock,
    )
    return built


def attempt(client: TestClient, username: str, password: str, address: str | None = None) -> object:
    headers = {} if address is None else {"X-Forwarded-For": address}
    return client.post("/auth/login", json={"username": username, "password": password}, headers=headers)


def login_events(tmp_path: Path) -> list:  # type: ignore[type-arg]
    return list(audit_log_at(tmp_path).query(AuditQuery(action="auth.login")))


def test_an_account_locks_after_its_limit_even_with_the_right_password(
    app: FastAPI, tmp_path: Path, clock: Clock
) -> None:
    user_id = make_user(app, "Asha", [Role.ANALYST])
    client = TestClient(app)
    codes = [attempt(client, "asha", "wrong password!").status_code for _ in range(3)]  # type: ignore[attr-defined]
    assert codes == [401, 401, 401]

    locked = attempt(client, "ASHA", PASSWORD)
    assert locked.status_code == 429  # type: ignore[attr-defined]
    assert locked.json()["detail"]["code"] == "LOGIN_LOCKED"  # type: ignore[attr-defined]
    assert locked.headers["Retry-After"] == "600"  # type: ignore[attr-defined]
    assert "10 minute" in locked.json()["detail"]["message"]  # type: ignore[attr-defined]

    clock.now += timedelta(seconds=599)
    assert attempt(client, "asha", PASSWORD).status_code == 429  # type: ignore[attr-defined]
    clock.now += timedelta(seconds=1)
    ok = attempt(client, "asha", PASSWORD)
    assert ok.status_code == 200, ok.text  # type: ignore[attr-defined]

    events = login_events(tmp_path)
    outcomes = [(e.outcome, e.details.get("reason_code"), e.details.get("lockout")) for e in events]
    assert sorted(outcomes, key=str) == sorted(
        [
            ("failed", "BAD_CREDENTIALS", None),
            ("failed", "BAD_CREDENTIALS", None),
            ("failed", "BAD_CREDENTIALS", "account"),  # the failure that started the lock-out
            ("denied", "LOGIN_LOCKED", "account"),
            ("denied", "LOGIN_LOCKED", "account"),
            ("success", None, None),
        ],
        key=str,
    )
    refused = [e for e in events if e.details.get("reason_code") == "LOGIN_LOCKED"]
    assert {e.object_id for e in refused} == {user_id}
    for event in events:
        dumped = event.model_dump_json()
        assert PASSWORD not in dumped and "wrong password!" not in dumped and "asha" not in dumped.lower()


def test_an_unknown_username_locks_exactly_like_a_known_one(app: FastAPI) -> None:
    make_user(app, "Asha", [Role.ANALYST])
    client = TestClient(app)
    for name in ("asha", "nobody-here"):
        for _ in range(3):
            attempt(client, name, "wrong password!")
    known = attempt(client, "asha", "wrong password!")
    unknown = attempt(client, "nobody-here", "wrong password!")
    assert known.status_code == unknown.status_code == 429  # type: ignore[attr-defined]
    assert known.json() == unknown.json()  # type: ignore[attr-defined]


def test_a_success_resets_the_account_count(app: FastAPI) -> None:
    make_user(app, "Asha", [Role.ANALYST])
    client = TestClient(app)
    for _ in range(2):
        attempt(client, "asha", "wrong password!")
    assert attempt(client, "asha", PASSWORD).status_code == 200  # type: ignore[attr-defined]
    for _ in range(2):
        attempt(client, "asha", "wrong password!")
    assert attempt(client, "asha", PASSWORD).status_code == 200  # type: ignore[attr-defined]


def test_the_address_limit_stops_a_spray_across_accounts(tmp_path: Path, clock: Clock) -> None:
    app = local_app(tmp_path)
    app.state.login_throttle = LoginThrottle(
        max_failures_per_account=100,
        max_failures_per_address=4,
        window_seconds=900,
        lockout_seconds=60,
        clock=clock,
    )
    make_user(app, "Asha", [Role.ANALYST])
    client = TestClient(app)
    for index in range(4):
        assert attempt(client, f"user{index}", "Summer2026!!").status_code == 401  # type: ignore[attr-defined]
    refused = attempt(client, "asha", PASSWORD)
    assert refused.status_code == 429  # type: ignore[attr-defined]
    (event,) = [e for e in login_events(tmp_path) if e.details.get("reason_code") == "LOGIN_LOCKED"]
    assert event.details["lockout"] == "address"


def test_forwarded_for_is_ignored_unless_a_proxy_is_trusted(tmp_path: Path, clock: Clock) -> None:
    """With no trusted proxy, a client cannot dodge the address limit by writing X-Forwarded-For."""
    for hops, expect_locked in ((0, True), (1, False)):
        root = tmp_path / str(hops)
        root.mkdir()
        app = local_app(root, trusted_proxy_hops=hops)
        app.state.login_throttle = LoginThrottle(
            max_failures_per_account=100,
            max_failures_per_address=2,
            window_seconds=900,
            lockout_seconds=60,
            clock=clock,
        )
        client = TestClient(app)
        for index in range(2):
            attempt(client, f"user{index}", "wrong password!", address=f"198.51.100.{index}")
        third = attempt(client, "user9", "wrong password!", address="198.51.100.9")
        assert (third.status_code == 429) is expect_locked, hops  # type: ignore[attr-defined]


def test_the_limits_are_settings() -> None:
    settings = Settings.from_env(
        {
            "MARKETING_AI_LOGIN_MAX_FAILURES_PER_ACCOUNT": "7",
            "MARKETING_AI_LOGIN_MAX_FAILURES_PER_ADDRESS": "70",
            "MARKETING_AI_LOGIN_FAILURE_WINDOW_SECONDS": "120",
            "MARKETING_AI_LOGIN_LOCKOUT_SECONDS": "1800",
            "MARKETING_AI_TRUSTED_PROXY_HOPS": "1",
        }
    )
    assert (
        settings.login_max_failures_per_account,
        settings.login_max_failures_per_address,
        settings.login_failure_window_seconds,
        settings.login_lockout_seconds,
        settings.trusted_proxy_hops,
    ) == (7, 70, 120, 1800, 1)
    defaults = Settings()
    assert (defaults.login_max_failures_per_account, defaults.login_lockout_seconds) == (5, 900)
