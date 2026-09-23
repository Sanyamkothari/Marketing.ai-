"""The audit middleware (M47): exactly one event per mutating request, whatever happened.

Plan section 4: "every mutating route writes exactly one audit event". Driven over the live route
list three ways - anonymous (401, `denied`), signed in without the role (403, `denied`), and with
sign-in off so the handler really runs (2xx, 4xx or 5xx: `success` or `failed`) - and each request
must leave exactly one new row, carrying the request id the response returned.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.audit.events import AuditQuery
from tests.integration.production.access_support import (
    LiveRoute,
    audit_log_at,
    bearer,
    event_count,
    live_routes,
    local_app,
    make_user,
)

pytestmark = pytest.mark.integration

ROUTES = live_routes()
MUTATING = [route for route in ROUTES if route.method in {"POST", "PUT", "PATCH", "DELETE"}]
AUDITED_READS = [
    route for route in ROUTES if route.method == "GET" and route.policy and route.policy.audit_reads
]
PLAIN_READS = [
    route for route in ROUTES if route.method == "GET" and route.policy and not route.policy.audit_reads
]


def _latest(tmp_path: Path):  # type: ignore[no-untyped-def]
    (event,) = audit_log_at(tmp_path).query(AuditQuery(limit=1))
    return event


@pytest.fixture(scope="module")
def signed_in(tmp_path_factory: pytest.TempPathFactory) -> tuple[TestClient, Path, str]:
    tmp_path = tmp_path_factory.mktemp("audit-local")
    app = local_app(tmp_path)
    viewer = make_user(app, "viewer-only", [Role.VIEWER])
    client = TestClient(app, raise_server_exceptions=False)
    client.app_state = app  # type: ignore[attr-defined]
    return client, tmp_path, viewer


@pytest.fixture(scope="module")
def auth_off(tmp_path_factory: pytest.TempPathFactory) -> tuple[TestClient, Path]:
    tmp_path = tmp_path_factory.mktemp("audit-off")
    return TestClient(local_app(tmp_path, auth_mode="off"), raise_server_exceptions=False), tmp_path


@pytest.mark.parametrize("route", MUTATING, ids=lambda route: route.id)
def test_an_anonymous_mutation_leaves_exactly_one_event(
    signed_in: tuple[TestClient, Path, str], route: LiveRoute
) -> None:
    client, tmp_path, _ = signed_in
    before = event_count(tmp_path)
    response = route.call(client)
    assert event_count(tmp_path) == before + 1
    event = _latest(tmp_path)
    assert event.request_id == response.headers["x-request-id"]
    assert event.details["status_code"] == response.status_code
    assert event.details["method"] == route.method
    assert event.details["route"] == route.path
    assert route.policy is not None and event.action == route.policy.action
    if route.policy.role is None:
        assert event.outcome in {"success", "failed"}
    else:
        assert (event.outcome, event.actor_id, event.actor_kind) == ("denied", "anonymous", "anonymous")


@pytest.mark.parametrize("route", MUTATING, ids=lambda route: route.id)
def test_a_refused_mutation_names_who_was_refused(
    signed_in: tuple[TestClient, Path, str], route: LiveRoute
) -> None:
    client, tmp_path, viewer = signed_in
    before = event_count(tmp_path)
    response = route.call(client, bearer(client.app_state, viewer))  # type: ignore[attr-defined]
    assert event_count(tmp_path) == before + 1
    event = _latest(tmp_path)
    assert event.request_id == response.headers["x-request-id"]
    if response.status_code == 403 and response.json()["detail"]["code"] == "ROLE_REQUIRED":
        assert (event.outcome, event.actor_id) == ("denied", viewer)


@pytest.mark.parametrize("route", MUTATING, ids=lambda route: route.id)
def test_a_mutation_that_runs_leaves_exactly_one_event_whatever_its_status(
    auth_off: tuple[TestClient, Path], route: LiveRoute
) -> None:
    client, tmp_path = auth_off
    before = event_count(tmp_path)
    response = route.call(client)
    assert event_count(tmp_path) == before + 1, f"{route.id} answered {response.status_code}"
    event = _latest(tmp_path)
    assert event.request_id == response.headers["x-request-id"]
    assert event.details["status_code"] == response.status_code
    expected = (
        "success"
        if response.status_code < 400
        else ("denied" if response.status_code in (401, 403) else "failed")
    )
    if route.policy is not None and route.policy.role is not None:
        assert event.actor_id == "local-operator"
        assert event.outcome == expected


@pytest.mark.parametrize("route", AUDITED_READS, ids=lambda route: route.id)
def test_an_audited_read_leaves_exactly_one_event(
    auth_off: tuple[TestClient, Path], route: LiveRoute
) -> None:
    client, tmp_path = auth_off
    before = event_count(tmp_path)
    route.call(client)
    assert event_count(tmp_path) == before + 1
    assert route.policy is not None and _latest(tmp_path).action == route.policy.action


@pytest.mark.parametrize("route", PLAIN_READS, ids=lambda route: route.id)
def test_a_plain_read_leaves_none(auth_off: tuple[TestClient, Path], route: LiveRoute) -> None:
    client, tmp_path = auth_off
    before = event_count(tmp_path)
    route.call(client)
    assert event_count(tmp_path) == before


def test_an_unmatched_mutation_is_still_one_event_and_records_no_path(tmp_path: Path) -> None:
    client = TestClient(local_app(tmp_path, auth_mode="off"))
    response = client.post("/no/such/route/with-someone@example.com", json={})
    assert response.status_code == 404
    event = _latest(tmp_path)
    assert (event.action, event.outcome, event.details["route"]) == ("request.unmatched", "failed", None)
    assert "example.com" not in event.model_dump_json()


def test_a_2xx_json_body_is_hashed_from_the_bytes_sent(tmp_path: Path) -> None:
    from api.access_policy import RoutePolicy, register

    app = local_app(tmp_path, auth_mode="off")
    register({("POST", "/echo-for-this-test"): RoutePolicy(role=Role.ANALYST, action="test.echo")})

    @app.post("/echo-for-this-test", status_code=201)
    def echo() -> dict[str, object]:
        return {"b": [1, 2, 3], "a": "x"}

    response = TestClient(app).post("/echo-for-this-test", json={})
    assert response.status_code == 201
    event = _latest(tmp_path)
    assert event.after_hash == hashlib.sha256(response.content).hexdigest()
    assert event.before_hash is None


def test_a_streamed_audited_read_is_passed_through_unbuffered_and_unhashed(tmp_path: Path) -> None:
    from fastapi.responses import StreamingResponse

    from api.access_policy import RoutePolicy, register

    app = local_app(tmp_path, auth_mode="off")
    register(
        {
            ("GET", "/stream-for-this-test"): RoutePolicy(
                role=Role.VIEWER, action="test.stream", audit_reads=True
            )
        }
    )
    chunks = [b"id,score\n", b"r-1,0.5\n", b"r-2,0.7\n"]

    @app.get("/stream-for-this-test")
    def stream() -> StreamingResponse:
        return StreamingResponse(iter(chunks), media_type="text/csv")

    response = TestClient(app).get("/stream-for-this-test")
    assert response.content == b"".join(chunks)
    event = _latest(tmp_path)
    assert (event.action, event.outcome, event.after_hash) == ("test.stream", "success", None)


def test_a_204_has_no_hash_and_an_enriched_route_keeps_its_own(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    admin = make_user(app, "admin", [Role.ADMIN])
    client = TestClient(app)
    response = client.patch(f"/users/{admin}", json={"display_name": "The Admin"}, headers=bearer(app, admin))
    assert response.status_code == 200
    event = _latest(tmp_path)
    assert event.action == "users.update"
    assert event.before_hash is not None and event.after_hash is not None
    assert event.before_hash != event.after_hash
    assert event.after_hash != hashlib.sha256(response.content).hexdigest()  # the route's hash won
    assert client.post("/auth/logout", headers=bearer(app, admin)).status_code == 204
    assert _latest(tmp_path).after_hash is None


def test_request_ids_are_echoed_only_when_safe(tmp_path: Path) -> None:
    client = TestClient(local_app(tmp_path, auth_mode="off"))
    safe = client.post("/runs", json={}, headers={"X-Request-ID": "client-req-0001"})
    assert safe.headers["x-request-id"] == "client-req-0001"
    assert _latest(tmp_path).request_id == "client-req-0001"
    unsafe = client.post("/runs", json={}, headers={"X-Request-ID": "<script>alert(1)</script>"})
    assert unsafe.headers["x-request-id"] != "<script>alert(1)</script>"
    assert len(unsafe.headers["x-request-id"]) == 32
    assert client.get("/healthz").headers["x-request-id"]


def test_an_unsafe_path_parameter_is_withheld_from_the_object_id(tmp_path: Path) -> None:
    client = TestClient(local_app(tmp_path, auth_mode="off"))
    client.post("/runs/r-123/cancel", json={})
    assert _latest(tmp_path).object_id == "r-123"
    client.post("/runs/someone@example.com/cancel", json={})
    event = _latest(tmp_path)
    assert event.object_id is None
    assert "example.com" not in event.model_dump_json()


def test_a_failed_audit_write_still_answers_and_is_counted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = local_app(tmp_path, auth_mode="off")

    class Broken:
        def append(self, event: object) -> None:
            raise RuntimeError("database is down: secret-value-QZLEAK")

    app.state.audit_log = Broken()
    client = TestClient(app)
    with caplog.at_level(logging.ERROR, logger="api.access"):
        response = client.post("/runs", json={})
    assert response.status_code == 422  # the request's own answer, untouched by the failure
    assert app.state.audit_write_failures == 1
    (record,) = [r for r in caplog.records if r.name == "api.access" and r.levelno == logging.ERROR]
    assert "RuntimeError" in record.getMessage() and "runs.create" in record.getMessage()
    assert "QZLEAK" not in record.getMessage()


def test_a_crashing_handler_is_one_failed_event(tmp_path: Path) -> None:
    from api.access_policy import RoutePolicy, register

    app = local_app(tmp_path, auth_mode="off")
    register({("POST", "/crash-for-this-test"): RoutePolicy(role=Role.ANALYST, action="test.crash")})

    @app.post("/crash-for-this-test")
    def crash() -> None:
        raise RuntimeError("boom")

    response = TestClient(app, raise_server_exceptions=False).post("/crash-for-this-test", json={})
    assert response.status_code == 500
    event = _latest(tmp_path)
    assert (event.action, event.outcome, event.details["status_code"]) == ("test.crash", "failed", 500)
    assert event_count(tmp_path) == 1
