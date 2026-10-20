"""The `/v2/connectors` API (api/v2/connectors.py) and the Connect screen's platform cards.

The API half drives the routes over FastAPI's test client against the connector base's fake platforms
(`base_support.FakePlatform`, `shopify_support.FakeShopify`, `klaviyo_support.FakeKlaviyo`) through
`httpx.MockTransport`: connect by OAuth and by a pasted key, the callback's redirect back to the screen, health in
words (first sync, loading history, up to date, behind, needs reconnect), sync now, disconnect, the guided setup
steps, webhooks, and the install link a platform starts. The UI half runs `connect_screen.test.mjs` in node with a
tiny DOM shim: the cards, the one loud state, the forms, and what an older API without these routes shows.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

pytest.importorskip("fastapi", reason="the API tests need FastAPI (the v2 CI job installs it)")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v2.connectors import (
    ConnectorServices,
    ConnectorsOverview,
    SetupView,
    ago,
    attach_connector_services,
    local_connector_services,
    router,
)
from autonomy.connectors import ConnectionStatus, Lane, Stream

from .base_support import T0, FakePlatform, Rig, make_rig, signed

ROOT = Path(__file__).resolve().parents[3]
MOCK = ROOT / "ui" / "v2" / "mock"
NODE_TEST = Path(__file__).resolve().parent / "connect_screen.test.mjs"
CODES = re.compile(r"connector\.[a-z_]+|Traceback|HTTP_\d+")


def _client(services: ConnectorServices) -> TestClient:
    app = FastAPI()
    attach_connector_services(app, services)
    app.include_router(router)
    return TestClient(app, follow_redirects=False)


def _services(rig: Rig, business_id: str = "biz_1") -> ConnectorServices:
    return ConnectorServices(
        engine=rig.engine,
        queue=rig.queue,
        business_id=business_id,
        clock=rig.clock.now,
        public_url="https://autonomy.example",
    )


def _oauth_connect(client: TestClient, kind: str = "demo", config: dict[str, str] | None = None) -> str:
    """Start OAuth through the API and come back through the callback; return where the browser lands."""
    started = client.post(f"/v2/connectors/{kind}/connect", json={"config": config or {}})
    assert started.status_code == 200, started.text
    query = parse_qs(urlsplit(started.json()["url"]).query)
    assert query["redirect_uri"] == [f"https://autonomy.example/v2/connectors/{kind}/callback"]
    back = client.get(
        f"/v2/connectors/{kind}/callback", params={"state": query["state"][0], "code": "code_ok"}
    )
    assert back.status_code == 303
    return str(back.headers["location"])


def _queued(rig: Rig) -> list[Any]:
    from .shopify_support import queued

    return queued(rig)


def _drain(rig: Rig, rounds: int = 12) -> None:
    """Run the queued sync jobs that are due, as a worker would."""
    from autonomy.connectors import SYNC_KIND

    handlers = rig.engine.handlers(rig.queue)
    for _ in range(rounds):
        job = rig.queue.claim([SYNC_KIND], worker="w", now=rig.clock.now())
        if job is None:
            return
        handlers[SYNC_KIND](job, rig.clock.now())
        rig.queue.complete(job.job_id)


def _view(client: TestClient, kind: str = "demo") -> dict[str, Any]:
    overview = client.get("/v2/connectors").json()
    platform = next(p for p in overview["platforms"] if p["kind"] == kind)
    assert platform["connections"], platform
    return dict(platform["connections"][0])


# --- the catalogue ------------------------------------------------------------------------------------------


def test_the_catalogue_lists_shopify_then_klaviyo_and_says_what_this_server_can_do() -> None:
    services = local_connector_services(business_id="biz_1", sink=lambda events: 0, environ={})
    body = _client(services).get("/v2/connectors").json()
    ConnectorsOverview.model_validate(body)
    kinds = [p["kind"] for p in body["platforms"]]
    assert kinds[:2] == ["shopify", "klaviyo"]
    shopify, klaviyo = body["platforms"][:2]
    assert shopify["tagline"] == "Brings in orders, customers and products."
    assert klaviyo["tagline"] == "Sends Autonomy's messages through your own flows." and klaviyo["sends"]
    # No app credentials on this server: OAuth is not offered, the pasted key is, and the operator learns what to set.
    assert not shopify["oauth_ready"] and shopify["key_allowed"]
    assert shopify["missing_settings"] == ["AUTONOMY_SHOPIFY_CLIENT_ID", "AUTONOMY_SHOPIFY_CLIENT_SECRET"]
    assert [f["name"] for f in shopify["config_fields"]] == ["shop"] and "read_orders" in shopify["scopes"]
    assert shopify["install_url"] is None and shopify["api_version"] == "2026-10"
    assert body["can_act"] is True and body["platforms"][0]["connections"] == []

    configured = local_connector_services(
        business_id="biz_1",
        sink=lambda events: 0,
        environ={
            "AUTONOMY_SHOPIFY_CLIENT_ID": "id",
            "AUTONOMY_SHOPIFY_CLIENT_SECRET": "secret",
            "AUTONOMY_PUBLIC_URL": "https://autonomy.example/",
        },
    )
    shop = _client(configured).get("/v2/connectors").json()["platforms"][0]
    assert shop["oauth_ready"] and shop["missing_settings"] == []
    assert shop["install_url"] == "https://autonomy.example/v2/connectors/shopify/install"
    assert configured.engine.webhook_base_url == "https://autonomy.example/v2/connectors/webhooks"


def test_the_sample_files_have_the_apis_shape() -> None:
    overview = ConnectorsOverview.model_validate(json.loads((MOCK / "connectors.json").read_text()))
    setups = json.loads((MOCK / "connector_setup.json").read_text())
    ids = {c.connection_id for p in overview.platforms for c in p.connections}
    assert set(setups) <= ids and setups
    for value in setups.values():
        SetupView.model_validate(value)
    assert {p.kind for p in overview.platforms} >= {"shopify", "klaviyo"}


def test_relative_times_read_as_a_person_says_them() -> None:
    assert [ago(s) for s in (0, 59, 60, 240, 3600, 7200, 86400 * 3)] == [
        "just now",
        "just now",
        "1 minute ago",
        "4 minutes ago",
        "1 hour ago",
        "2 hours ago",
        "3 days ago",
    ]


# --- connect by OAuth, then health in words -----------------------------------------------------------------


def test_oauth_connect_returns_to_the_screen_and_health_reads_in_words_as_the_sync_runs() -> None:
    platform = FakePlatform()
    for i in range(6):
        platform.add_order(f"o{i}", customer=f"c{i}", total=10.0 + i, created=T0 - timedelta(days=3 * i + 1))
    rig = make_rig(platform)
    client = _client(_services(rig))

    landing = _oauth_connect(client)
    assert landing.startswith("/v2/app/#/connect?connected=demo&connection=conn_")
    view = _view(client)
    assert (view["phase"], view["words"], view["label"]) == ("syncing", "First sync running", "Demo Shop")
    assert view["status"] == "active" and not view["can_act"]
    assert {j.kind for j in _queued(rig)} >= {"connector_sync", "connector_daily"}  # the jobs are seeded

    _drain(rig)  # the first live window and the whole 28-day history (7-day chunks)
    rig.clock.advance(timedelta(minutes=4))
    view = _view(client)
    assert (view["phase"], view["words"]) == ("healthy", "Up to date, 4 minutes ago"), view
    assert view["can_act"] and view["history_progress"] is None
    orders = next(s for s in view["streams"] if s["stream"] == "orders")
    assert orders["label"] == "Orders" and orders["history_words"] == "History loaded"
    assert orders["words"] == "Up to date, 4 minutes ago" and orders["last_rows"] >= 0
    assert "every hour, and right away" in view["detail"]  # this platform sends webhooks
    assert client.get("/v2/connectors").json()["can_act"] is True

    rig.clock.advance(timedelta(hours=4))  # nothing ran for four hours: behind, decisions wait
    view = _view(client)
    assert (view["phase"], view["words"]) == ("stale", "Last synced 4 hours ago")
    words = {p["stream"]: p["words"] for p in view["problems"]}
    assert words["orders"] == "Orders last synced 4 hours ago. Decisions wait until it catches up."
    assert (
        words.get("customers", "Customers last synced 4 hours ago.") == "Customers last synced 4 hours ago."
    )
    overview = client.get("/v2/connectors").json()
    assert overview["can_act"] is False and overview["on_hold"] == [
        "Demo Shop (Demo Shop): last synced 4 hours ago."
    ]
    shown = [view["words"], view["detail"], [p["words"] for p in view["problems"]], overview["on_hold"]]
    assert not CODES.search(json.dumps(shown))


def test_loading_history_shows_its_progress() -> None:
    platform = FakePlatform()
    rig = make_rig(platform)
    client = _client(_services(rig))
    _oauth_connect(client)
    connection_id = _view(client)["connection_id"]
    rig.engine.sync(connection_id, Stream.ORDERS, Lane.LIVE, rig.clock.now())
    rig.engine.sync(connection_id, Stream.CUSTOMERS, Lane.LIVE, rig.clock.now())
    rig.engine.sync(connection_id, Stream.ORDERS, Lane.HISTORY, rig.clock.now())  # one 7-day chunk of 28 days
    view = _view(client)
    assert (view["phase"], view["words"]) == ("backfilling", "Loading history, 25%")
    assert view["history_progress"] == pytest.approx(0.25)
    assert "once it has 14 days" in view["detail"]


def test_a_denied_or_reused_callback_goes_back_to_the_screen_in_words() -> None:
    rig = make_rig()
    client = _client(_services(rig))
    started = client.post("/v2/connectors/demo/connect", json={}).json()
    state = parse_qs(urlsplit(started["url"]).query)["state"][0]
    denied = client.get("/v2/connectors/demo/callback", params={"state": state, "error": "access_denied"})
    assert denied.status_code == 303
    where = unquote(denied.headers["location"])
    assert where.startswith("/v2/app/#/connect?connect_error=") and "not approved in Demo Shop" in where
    again = client.get("/v2/connectors/demo/callback", params={"state": state, "code": "code_ok"})
    assert "expired or was already used" in unquote(again.headers["location"])
    assert rig.store.connections() == []
    assert client.post("/v2/connectors/nope/connect", json={}).status_code == 404


def test_revoked_access_is_the_one_loud_state_and_a_reconnect_resumes_the_same_connection() -> None:
    platform = FakePlatform()
    rig = make_rig(platform)
    client = _client(_services(rig))
    _oauth_connect(client)
    first = _view(client)["connection_id"]
    _drain(rig)
    platform.revoked = True
    rig.clock.advance(timedelta(hours=2))  # past the token's hour: the refresh is refused
    rig.engine.sync(first, Stream.ORDERS, Lane.LIVE, rig.clock.now())
    view = _view(client)
    assert (view["phase"], view["words"]) == ("needs_reconnect", "Needs reconnecting")
    assert "reconnect" in view["detail"] and not CODES.search(view["detail"])
    reconnect_alerts = [a for a in rig.alerts.list_alerts() if a.code == "connector.needs_reconnect"]
    assert len(reconnect_alerts) == 1
    assert client.post(f"/v2/connectors/connections/{first}/sync").status_code == 409
    assert client.get("/v2/connectors").json()["on_hold"] == ["Demo Shop (Demo Shop): needs reconnecting."]

    platform.revoked = False
    _oauth_connect(client)
    view = _view(client)
    assert view["connection_id"] == first and view["status"] == "active"


# --- connect with a key -------------------------------------------------------------------------------------


def test_a_shopify_custom_app_token_connects_with_a_loosely_typed_shop_address() -> None:
    from .shopify_support import SHOP, make_shopify_rig

    rig, fake = make_shopify_rig()
    client = _client(_services(rig))
    bad = client.post(
        "/v2/connectors/shopify/key", json={"api_key": "shpat_live_1", "config": {"shop": "acme.com"}}
    )
    assert bad.status_code == 400
    assert (
        bad.json()["detail"]
        == "Shop address is not valid. Your shop's permanent address, ending in .myshopify.com."
    )
    missing = client.post("/v2/connectors/shopify/key", json={"api_key": "shpat_live_1", "config": {}})
    assert missing.json()["detail"] == "Shop address is needed to connect Shopify."
    wrong = client.post(
        "/v2/connectors/shopify/key", json={"api_key": "shpat_wrong_1", "config": {"shop": "Acme"}}
    )
    assert (
        wrong.status_code == 400
        and wrong.json()["detail"] == "Shopify did not accept this key. Check it and try again."
    )
    assert "shpat_wrong_1" not in wrong.text

    ok = client.post(
        "/v2/connectors/shopify/key",
        json={"api_key": "shpat_live_1", "config": {"shop": "https://Acme.myshopify.com/admin"}},
    )
    assert ok.status_code == 200, ok.text
    view = ok.json()
    assert view["external_id"] == SHOP and view["label"] == "Acme Goods" and view["phase"] == "syncing"
    assert "shpat_live_1" not in ok.text and "shpat_live_1" not in client.get("/v2/connectors").text
    assert fake.token_calls == []  # a custom-app token never refreshes
    # This rig's server holds our Shopify app's credentials, so sign-in is offered too.
    started = client.post("/v2/connectors/shopify/connect", json={"config": {"shop": "acme"}})
    assert started.status_code == 200 and urlsplit(started.json()["url"]).netloc == SHOP


def test_oauth_is_refused_in_words_when_the_server_has_no_app_credentials() -> None:
    rig = make_rig()
    rig.engine.apps.pop("demo")
    client = _client(_services(rig))
    refused = client.post("/v2/connectors/demo/connect", json={})
    assert refused.status_code == 409
    assert (
        refused.json()["detail"]
        == "This server is not set up to connect Demo Shop by sign-in yet. Paste an access key instead."
    )


def test_klaviyo_guided_setup_steps_come_from_its_own_check() -> None:
    from .klaviyo_support import API_KEY, FakeKlaviyo, jsonapi_error, make_klaviyo_rig

    fake = FakeKlaviyo(metric_lag=2)  # Klaviyo makes the new metric visible a little later
    fake.flow_create_error["email"] = jsonapi_error(
        400, "invalid", "This account cannot create live flows via API."
    )
    rig = make_klaviyo_rig(fake)
    services = ConnectorServices(engine=rig.engine, queue=rig.queue, business_id="biz_1", clock=rig.clock.now)
    client = _client(services)
    made = client.post("/v2/connectors/klaviyo/key", json={"api_key": API_KEY, "config": {}})
    assert made.status_code == 200, made.text
    assert API_KEY not in made.text
    connection_id = made.json()["connection_id"]
    # The connect also started Klaviyo's hourly setup-and-delivery watch (wiring.after_connect).
    from autonomy.channels.klaviyo import watch_job

    assert rig.queue.get(watch_job(connection_id, rig.clock.now()).job_id) is not None
    # The metric is not visible yet: setup is finishing, in words.
    pending = client.get(f"/v2/connectors/connections/{connection_id}/setup").json()
    SetupView.model_validate(pending)
    assert (pending["state"], pending["words"]) == (
        "pending",
        "Finishing setup in Klaviyo; this takes a few minutes.",
    )
    assert pending["steps"] == []
    # Then Klaviyo refuses to create the flow: the exact one-time steps, in order, and no reconnect.
    for _ in range(3):
        guided = client.get(
            f"/v2/connectors/connections/{connection_id}/setup", params={"refresh": "true"}
        ).json()
        if guided["state"] != "pending":
            break
    assert (guided["state"], guided["words"]) == ("guided", "One step to finish in Klaviyo.")
    assert (
        guided["steps"][0].startswith("In Klaviyo, open Flows") and "Autonomy · email" in guided["steps"][0]
    )
    assert [c["words"] for c in guided["channels"]] == ["Email: not ready yet"]
    stored = rig.store.get_connection(connection_id)
    assert stored is not None and stored.status is ConnectionStatus.ACTIVE

    other = make_rig()
    client = _client(_services(other))
    _oauth_connect(client)
    demo = _view(client)["connection_id"]
    assert client.get(f"/v2/connectors/connections/{demo}/setup").json()["state"] == "not_needed"


# --- sync now, disconnect, ownership -------------------------------------------------------------------------


def test_sync_now_queues_one_sync_per_stream_however_often_it_is_pressed() -> None:
    rig = make_rig()
    client = _client(_services(rig))
    _oauth_connect(client)
    connection_id = _view(client)["connection_id"]
    before = {j.job_id for j in _queued(rig)}
    first = client.post(f"/v2/connectors/connections/{connection_id}/sync").json()
    rig.clock.advance(timedelta(seconds=20))
    client.post(f"/v2/connectors/connections/{connection_id}/sync")
    added = {j.job_id for j in _queued(rig)} - before
    assert len(added) == 2  # orders and customers, once each
    assert first["streams"] == ["orders", "customers"]
    assert first["words"] == "A sync of orders and customers starts now."


def test_disconnect_forgets_the_tokens_and_a_connection_of_another_business_is_not_found() -> None:
    rig = make_rig()
    client = _client(_services(rig))
    _oauth_connect(client)
    connection_id = _view(client)["connection_id"]
    other = _client(_services(rig, business_id="someone_else"))
    assert other.get(f"/v2/connectors/connections/{connection_id}").status_code == 404
    assert other.delete(f"/v2/connectors/connections/{connection_id}").status_code == 404

    gone = client.delete(f"/v2/connectors/connections/{connection_id}")
    assert gone.status_code == 200
    assert (gone.json()["phase"], gone.json()["words"]) == ("disconnected", "Disconnected")
    assert rig.engine.tokens.vault.load(connection_id) is None
    platform = next(p for p in client.get("/v2/connectors").json()["platforms"] if p["kind"] == "demo")
    assert [c["phase"] for c in platform["connections"]] == ["disconnected"]  # shown once, with why
    assert client.post(f"/v2/connectors/connections/{connection_id}/sync").status_code == 409


# --- webhooks and installs ----------------------------------------------------------------------------------


def test_webhooks_are_verified_deduped_and_answered_at_once() -> None:
    rig = make_rig()
    client = _client(_services(rig))
    _oauth_connect(client)
    connection_id = _view(client)["connection_id"]
    order = {
        "id": "o9",
        "customer_id": "c9",
        "total": 12.0,
        "created_at": "2026-10-01T11:00:00Z",
        "updated_at": "2026-10-01T11:00:00Z",
    }
    headers, raw = signed(order, topic="orders/updated", delivery="d1")
    url = f"/v2/connectors/webhooks/demo/{connection_id}"
    assert client.post(url, content=raw, headers=headers).json() == {"status": "accepted"}
    assert client.post(url, content=raw, headers=headers).json() == {"status": "duplicate"}
    forged = {**headers, "X-Demo-Hmac": "bm9wZQ=="}
    assert client.post(url, content=raw, headers=forged).status_code == 401
    assert client.post("/v2/connectors/webhooks/nope", content=raw, headers=headers).status_code == 404
    app_level = client.post("/v2/connectors/webhooks/demo", content=raw, headers=headers)
    assert app_level.status_code == 200  # found by the account header; the same delivery id: a duplicate
    assert sum(1 for j in _queued(rig) if j.kind == "connector_webhook") == 1
    huge = client.post(url, content=b"x" * 2_000_001, headers=headers)
    assert huge.status_code == 413


def test_an_install_started_in_the_shopify_admin_is_checked_then_sent_to_consent() -> None:
    from .shopify_support import SHOP, make_shopify_rig, sign_query

    rig, _fake = make_shopify_rig()
    client = _client(_services(rig))
    stamp = str(int(T0.timestamp()))
    good = sign_query({"shop": SHOP, "timestamp": stamp, "host": "YWNtZQ"})
    started = client.get("/v2/connectors/shopify/install", params=good)
    assert started.status_code == 303
    consent = urlsplit(started.headers["location"])
    assert (consent.netloc, consent.path) == (SHOP, "/admin/oauth/authorize")
    forged = client.get("/v2/connectors/shopify/install", params={**good, "shop": "other.myshopify.com"})
    assert "connect_error=" in forged.headers["location"]
    demo = _client(_services(make_rig()))
    assert demo.get("/v2/connectors/demo/install").status_code == 404  # the demo platform has no installs


def test_the_routes_need_their_services() -> None:
    app = FastAPI()
    app.include_router(router)
    answer = TestClient(app).get("/v2/connectors")
    assert (
        answer.status_code == 503
        and answer.json()["detail"] == "Connecting platforms is not set up on this server."
    )


# --- the Connect screen ---------------------------------------------------------------------------------------


def _node() -> list[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the screen's half of this test needs it")
    out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=False)
    match = re.match(r"v(\d+)\.(\d+)", out.stdout.strip())
    version = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    if version < (20, 10):
        pytest.skip(f"node {out.stdout.strip()} is too old to load ES modules by syntax")
    return [node] if version >= (22, 7) else [node, "--experimental-detect-module"]


def test_the_connect_screen_draws_the_platform_cards() -> None:
    done = subprocess.run(
        [*_node(), str(NODE_TEST)], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]


# --- review regressions -------------------------------------------------------------------------------------


def test_a_mistyped_token_for_a_connected_shop_keeps_the_working_connection() -> None:
    from .shopify_support import make_shopify_rig

    rig, _fake = make_shopify_rig()
    client = _client(_services(rig))
    ok = client.post(
        "/v2/connectors/shopify/key", json={"api_key": "shpat_live_1", "config": {"shop": "acme"}}
    )
    assert ok.status_code == 200, ok.text
    connection_id = ok.json()["connection_id"]
    wrong = client.post(
        "/v2/connectors/shopify/key", json={"api_key": "shpat_wrong_1", "config": {"shop": "acme"}}
    )
    assert wrong.status_code == 400
    kept = rig.engine.tokens.vault.load(connection_id)
    assert kept is not None and kept.access_token == "shpat_live_1"
    stored = rig.store.get_connection(connection_id)
    assert stored is not None and stored.status is ConnectionStatus.ACTIVE


def test_identity_settings_cannot_be_smuggled_in_as_free_settings() -> None:
    rig = make_rig()
    client = _client(_services(rig))
    refused = client.post(
        "/v2/connectors/demo/key", json={"api_key": "key_12345678", "config": {"account_id": "x"}}
    )
    assert refused.status_code == 400 and "not one Autonomy knows" in refused.json()["detail"]


def test_a_refused_key_is_never_echoed_back() -> None:
    from api.v2.app import create_app
    from api.v2.deps import build_demo_services

    short = _client(_services(make_rig())).post(
        "/v2/connectors/demo/key", json={"api_key": "sk_12", "config": {}}
    )
    assert short.status_code == 400 and "sk_12" not in short.text
    app = create_app(build_demo_services(T0, n_customers=5, days=1), connectors=_services(make_rig()))
    typed = TestClient(app).post("/v2/connectors/demo/key", json={"api_key": "sk_live_secret_1", "config": 5})
    assert typed.status_code == 422 and "sk_live_secret_1" not in typed.text
    as_string = TestClient(app).post("/v2/connectors/demo/key", json="sk_live_secret_2")
    assert as_string.status_code == 422 and "sk_live_secret_2" not in as_string.text


def test_a_chunked_webhook_over_the_cap_is_refused_without_reading_it_all() -> None:
    import asyncio

    rig = make_rig()
    services = _services(rig)
    client = _client(services)
    _oauth_connect(client)
    connection_id = _view(client)["connection_id"]
    app = client.app
    read = 0
    answer: dict[str, Any] = {}

    async def receive() -> dict[str, Any]:  # a chunked body (no content-length) of 40 MB if read whole
        nonlocal read
        read += 1
        return {"type": "http.request", "body": b"x" * 100_000, "more_body": read < 400}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            answer["status"] = message["status"]

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": f"/v2/connectors/webhooks/demo/{connection_id}",
        "raw_path": f"/v2/connectors/webhooks/demo/{connection_id}".encode(),
        "query_string": b"",
        "headers": [(b"transfer-encoding", b"chunked")],
        "server": ("testserver", 443),
        "client": ("127.0.0.1", 1234),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))  # type: ignore[arg-type]
    assert answer["status"] == 413
    assert read <= 21  # stopped at the 2 MB cap, not after the whole body


def test_behind_names_the_stream_that_never_synced() -> None:
    from api.v2.connectors import _phase
    from autonomy.connectors import Freshness, Problem
    from autonomy.connectors.health import ALERT_STALE

    rig = make_rig()
    client = _client(_services(rig))
    _oauth_connect(client)
    _drain(rig)
    connection = rig.store.connections()[0]
    now = rig.clock.now()
    health = rig.engine.monitor.connection_health(connection, now)
    orders, customers = health.streams
    assert orders.freshness is Freshness.FRESH
    # A second critical stream that never synced, past the grace period: the connection is behind.
    never = customers.model_copy(
        update={"critical": True, "freshness": Freshness.NEVER, "last_success_at": None, "lag_seconds": None}
    )
    stale = Problem(code=ALERT_STALE, severity="critical", message="never synced", stream="customers")
    behind = health.model_copy(update={"state": "stale", "streams": (orders, never), "problems": (stale,)})
    phase, words, _detail = _phase(connection, behind, "Demo Shop", now, {})
    assert (phase, words) == ("stale", "Customers not synced yet")  # not "Last synced just now" from orders
