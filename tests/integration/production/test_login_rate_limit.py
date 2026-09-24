"""Sign-in rate limiting through the API (Plan D M54, DEC-861): 429, `Retry-After`, the audit trail.

The throttle on `app.state` is replaced by one with a controlled clock, so a lock-out and its end are
tested without sleeping.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.access.throttle import LoginThrottle
from engine.audit.events import AuditQuery
from engine.settings import Settings
from tests.integration.production.access_support import (
    PASSWORD,
    audit_log_at,
    bearer,
    local_app,
    make_user,
    store_of,
)

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


# ---------------------------------------------------------------------------
# DEC-867: every X-Forwarded-For line; parallel attempts; a reset or re-enable lifts the lock
# ---------------------------------------------------------------------------
def test_every_forwarded_for_line_is_read_and_the_proxy_entry_counts(tmp_path: Path, clock: Clock) -> None:
    """Behind one proxy, a client's own X-Forwarded-For line comes first and the proxy's last.

    Reading only the first line would count whatever the client wrote - a fresh value per request,
    so the address limit would never trip.
    """
    app = local_app(tmp_path, trusted_proxy_hops=1)
    app.state.login_throttle = LoginThrottle(
        max_failures_per_account=100,
        max_failures_per_address=2,
        window_seconds=900,
        lockout_seconds=60,
        clock=clock,
    )
    client = TestClient(app)

    def spray(index: int) -> int:
        headers = [
            ("X-Forwarded-For", f"198.51.100.{index}, 192.0.2.{index}"),  # the client's own line
            ("X-Forwarded-For", "203.0.113.5"),  # the line the load balancer added
        ]
        response = client.post(
            "/auth/login",
            json={"username": f"user{index}", "password": "wrong password!"},
            headers=headers,
        )
        return response.status_code

    assert [spray(index) for index in range(3)] == [401, 401, 429]
    throttle = app.state.login_throttle
    assert throttle.failures("address", "203.0.113.5") == 2
    assert throttle.failures("address", "198.51.100.0") == 0


def test_parallel_wrong_passwords_get_no_more_401s_than_the_limit(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_user(app, "Asha", [Role.ANALYST])
    store = store_of(app)
    check = store.check_credentials

    def slow_check(username: str, password: str) -> object:
        time.sleep(0.05)  # a real password hash is slower still; this is the window the race needs
        return check(username, password)

    monkeypatch.setattr(store, "check_credentials", slow_check)
    attempts = 10
    barrier = threading.Barrier(attempts)

    def one(index: int) -> int:
        client = TestClient(app)
        barrier.wait()
        response = attempt(client, "asha", "wrong password!", address=f"10.0.0.{index}")
        return int(response.status_code)  # type: ignore[attr-defined]

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        codes = sorted(pool.map(one, range(attempts)))
    assert codes == [401] * 3 + [429] * (attempts - 3)


def lock_out(client: TestClient, username: str) -> None:
    for _ in range(3):
        attempt(client, username, "wrong password!")
    assert attempt(client, username, PASSWORD).status_code == 429  # type: ignore[attr-defined]


def test_an_admin_password_reset_lifts_the_lock(app: FastAPI) -> None:
    admin = make_user(app, "Root", [Role.ADMIN])
    asha = make_user(app, "Asha", [Role.ANALYST])
    client = TestClient(app)
    lock_out(client, "asha")
    reset = client.post(
        f"/users/{asha}/password", json={"password": "reset by the admin"}, headers=bearer(app, admin)
    )
    assert reset.status_code == 204, reset.text
    assert attempt(client, "Asha", "reset by the admin").status_code == 200  # type: ignore[attr-defined]


def test_changing_your_own_password_does_not_lift_a_lock_on_someone_else(app: FastAPI) -> None:
    bob = make_user(app, "Bob", [Role.ANALYST])
    make_user(app, "Asha", [Role.ANALYST])
    client = TestClient(app)
    lock_out(client, "asha")
    own = client.post(
        f"/users/{bob}/password",
        json={"password": "a new password for bob", "current_password": PASSWORD},
        headers=bearer(app, bob),
    )
    assert own.status_code == 204, own.text
    assert attempt(client, "asha", PASSWORD).status_code == 429  # type: ignore[attr-defined]


def test_re_enabling_a_user_lifts_the_lock(app: FastAPI) -> None:
    admin = make_user(app, "Root", [Role.ADMIN])
    asha = make_user(app, "Asha", [Role.ANALYST])
    client = TestClient(app)
    lock_out(client, "asha")
    disabled = client.patch(f"/users/{asha}", json={"disabled": True}, headers=bearer(app, admin))
    assert disabled.status_code == 200, disabled.text
    assert attempt(client, "asha", PASSWORD).status_code == 429, "disabling alone lifts nothing"  # type: ignore[attr-defined]
    enabled = client.patch(f"/users/{asha}", json={"disabled": False}, headers=bearer(app, admin))
    assert enabled.status_code == 200, enabled.text
    assert attempt(client, "asha", PASSWORD).status_code == 200  # type: ignore[attr-defined]


def test_an_unrelated_change_does_not_lift_the_lock(app: FastAPI) -> None:
    admin = make_user(app, "Root", [Role.ADMIN])
    asha = make_user(app, "Asha", [Role.ANALYST])
    client = TestClient(app)
    lock_out(client, "asha")
    renamed = client.patch(f"/users/{asha}", json={"display_name": "Asha K"}, headers=bearer(app, admin))
    assert renamed.status_code == 200, renamed.text
    assert attempt(client, "asha", PASSWORD).status_code == 429  # type: ignore[attr-defined]


def test_the_address_lock_does_not_promise_that_a_reset_helps(tmp_path: Path, clock: Clock) -> None:
    app = local_app(tmp_path)
    app.state.login_throttle = LoginThrottle(
        max_failures_per_account=100,
        max_failures_per_address=2,
        window_seconds=900,
        lockout_seconds=60,
        clock=clock,
    )
    client = TestClient(app)
    for index in range(2):
        attempt(client, f"user{index}", "wrong password!")
    refused = attempt(client, "user9", "wrong password!")
    assert refused.status_code == 429  # type: ignore[attr-defined]
    message = refused.json()["detail"]["message"]  # type: ignore[attr-defined]
    assert "reset" not in message and "1 minute" in message
