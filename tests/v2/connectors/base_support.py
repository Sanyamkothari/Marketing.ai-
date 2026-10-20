"""A fake SaaS platform and a connector for it, so the connector base can be tested with no network.

`FakePlatform` answers like a typical commerce API (shapes follow the documented patterns the
real connectors meet): a pinned version header, bearer auth, `updated_min`/`updated_max` filters,
opaque page cursors that can expire, `429` with `Retry-After`, `401` when the token is revoked,
an OAuth token endpoint (code exchange and refresh with rotation), webhook registration, and
HMAC-signed webhooks. `DemoConnector` is a minimal `SaasConnector` for it, written the way the
Shopify and Klaviyo connectors are meant to be written.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

from autonomy.connectors import (
    ApiVersion,
    AppCredentials,
    AuthMethod,
    Connection,
    ConnectionContext,
    ConnectorCapabilities,
    ConnectorEngine,
    ConnectorRegistry,
    ConnectorStore,
    CursorExpired,
    HealthMonitor,
    Identity,
    IncidentDesk,
    ManualClock,
    MappedRecords,
    OAuthProvider,
    PrivacyKind,
    RateSpec,
    RecordKey,
    RemoteError,
    SaasConnector,
    SetupMode,
    Stream,
    StreamPage,
    StreamSpec,
    SyncWindow,
    WebhookDelivery,
    WebhookTopic,
    create_connector_tables,
    hmac_matches,
    map_with,
)
from autonomy.connectors.tokens import SqlTokenVault
from autonomy.contracts import EventType
from autonomy.data.mapping import EventMapping
from autonomy.runtime.stores import InMemoryAlertStore
from autonomy.store import SqlEventStore, SqlJobQueue, create_all, memory_engine

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
API_HOST = "demo.example"
AUTH_HOST = "auth.example"
VERSION = "2026-07"
WEBHOOK_SECRET = "secret_1"
"""Deliveries are signed with the app's client secret (as Shopify does)."""


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass
class FakePlatform:
    """The platform. Tests change its records and inject failures; every request is recorded."""

    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    customers: dict[str, dict[str, Any]] = field(default_factory=dict)
    page_size: int = 2
    access_token: str = "tok_1"
    refresh_token: str = "ref_1"
    revoked: bool = False
    rotate_refresh: bool = True
    token_seq: int = 1
    failures: list[httpx.Response] = field(default_factory=list)
    """Responses to return, in order, before serving normally."""
    expired_cursors: set[str] = field(default_factory=set)
    webhooks: set[str] = field(default_factory=set)
    calls: list[httpx.Request] = field(default_factory=list)
    token_calls: list[dict[str, str]] = field(default_factory=list)

    def add_order(
        self,
        oid: str,
        *,
        customer: str,
        total: float,
        created: datetime,
        updated: datetime | None = None,
        **extra: Any,
    ) -> None:
        self.orders[oid] = {
            "id": oid,
            "customer_id": customer,
            "total": total,
            "created_at": iso(created),
            "updated_at": iso(updated or created),
            **extra,
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == AUTH_HOST:
            return self._token(request)
        if self.failures:
            return self.failures.pop(0)
        if request.headers.get("x-demo-version") != VERSION:
            return httpx.Response(400, json={"error": "unsupported_version"})
        if self.revoked or request.headers.get("authorization") != f"Bearer {self.access_token}":
            return httpx.Response(401, json={"error": "invalid_token"})
        path = request.url.path
        if path == "/api/account":
            return httpx.Response(200, json={"id": "acct_9", "name": "Demo Shop", "plan": "plus"})
        if path == "/api/webhooks" and request.method == "POST":
            body = json.loads(request.content)
            self.webhooks.update(body["topics"])
            return httpx.Response(200, json={"topics": sorted(self.webhooks)})
        if path in ("/api/orders", "/api/customers"):
            return self._page(request, self.orders if path == "/api/orders" else self.customers)
        return httpx.Response(404, json={"error": "not_found"})

    def _page(self, request: httpx.Request, table: Mapping[str, Mapping[str, Any]]) -> httpx.Response:
        params = request.url.params
        cursor = params.get("cursor")
        if cursor and cursor in self.expired_cursors:
            self.expired_cursors.discard(cursor)  # a cursor issued again later is a fresh one
            return httpx.Response(400, json={"error": "cursor_expired"})
        low = parse_ts(params["updated_min"]) if params.get("updated_min") else None
        high = parse_ts(params["updated_max"]) if params.get("updated_max") else None
        rows = sorted(
            (
                r
                for r in table.values()
                if (low is None or parse_ts(r["updated_at"]) >= low)
                and (high is None or parse_ts(r["updated_at"]) < high)
            ),
            key=lambda r: (r["updated_at"], r["id"]),
        )
        offset = int(cursor.split(":", 1)[1]) if cursor else 0
        page = rows[offset : offset + self.page_size]
        nxt = f"c:{offset + self.page_size}" if offset + self.page_size < len(rows) else None
        return httpx.Response(
            200,
            json={"data": page, "next": nxt, "meta": {"unexpected": True}},
            headers={"RateLimit-Remaining": "40", "RateLimit-Reset": "1"},
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_calls.append(form)
        auth = request.headers.get("authorization", "")
        if auth != "Basic " + base64.b64encode(b"client_1:secret_1").decode():
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") == "authorization_code":
            if form.get("code") != "code_ok":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._issue(new_refresh=True))
        if form.get("grant_type") == "refresh_token":
            if self.revoked or form.get("refresh_token") != self.refresh_token:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})
            return httpx.Response(200, json=self._issue(new_refresh=self.rotate_refresh))
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _issue(self, *, new_refresh: bool) -> dict[str, Any]:
        self.token_seq += 1
        self.access_token = f"tok_{self.token_seq}"
        body: dict[str, Any] = {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "orders:read customers:read",
            "brand_new_field": {"ignored": True},
        }
        if new_refresh:
            self.refresh_token = f"ref_{self.token_seq}"
            body["refresh_token"] = self.refresh_token
        return body


ORDER_MAPPING = EventMapping(
    customer_id="customer_id",
    timestamp="created_at",
    event_type=EventType.PURCHASE,
    value_column="total",
    row_key=("id",),
)

DEMO_PROVIDER = OAuthProvider(
    name="Demo",
    authorize_url="https://auth.example/oauth/authorize",
    token_url="https://auth.example/oauth/token",
    scopes=("orders:read", "customers:read"),
    use_pkce=True,
    client_auth="basic",
    keep_alive_every=timedelta(days=30),
)


class DemoConnector(SaasConnector):
    capabilities = ConnectorCapabilities(
        kind="demo",
        label="Demo Shop",
        description="A fake platform for tests.",
        auth=AuthMethod.OAUTH2,
        setup=SetupMode.AUTOMATIC,
        streams=(
            StreamSpec(
                stream=Stream.ORDERS,
                backfill_days=28,
                backfill_chunk=timedelta(days=7),
                min_history_days=14,
                required_fields=("id", "customer_id", "total", "updated_at"),
                webhook_topics=("orders/updated",),
            ),
            StreamSpec(stream=Stream.CUSTOMERS, backfill_days=0, critical=False),
        ),
        webhook_topics=(
            WebhookTopic(topic="orders/updated", stream=Stream.ORDERS),
            WebhookTopic(topic="app/uninstalled", uninstall=True),
            WebhookTopic(topic="customers/redact", privacy=PrivacyKind.REDACT_CUSTOMER, mandatory=True),
        ),
        api_version=ApiVersion(
            value=VERSION,
            header="X-Demo-Version",
            doc_date=date(2026, 10, 1),
            docs_url="https://demo.example/docs",
            review_by=date(2027, 7, 1),
        ),
        rate=RateSpec(requests=50),
    )
    oauth = DEMO_PROVIDER

    def base_url(self, connection: Connection) -> str:
        return f"https://{API_HOST}/api"

    def identify(self, ctx: ConnectionContext) -> Identity | None:
        body = ctx.http.json("GET", "account")
        return Identity(external_id=str(body["id"]), label=str(body["name"]))

    def read(
        self, ctx: ConnectionContext, stream: Stream, window: SyncWindow, cursor: str | None
    ) -> StreamPage:
        params: dict[str, str | int] = {"updated_max": iso(window.end), "limit": 2}
        if window.start is not None:
            params["updated_min"] = iso(window.start)
        if cursor is not None:
            params["cursor"] = cursor
        try:
            body = ctx.http.json("GET", stream.value, params=params)
        except RemoteError as exc:
            if exc.status == 400:
                raise CursorExpired("the page cursor expired") from exc
            raise
        return StreamPage(records=tuple(body.get("data") or ()), next_cursor=body.get("next"))

    def record_key(self, stream: Stream, record: Mapping[str, Any]) -> RecordKey:
        updated = record.get("updated_at")
        return RecordKey(str(record["id"]), parse_ts(updated) if isinstance(updated, str) else None)

    def map_records(
        self, stream: Stream, records: Sequence[Mapping[str, Any]], connection: Connection
    ) -> MappedRecords:
        if stream is not Stream.ORDERS:
            return MappedRecords()
        return map_with(
            ORDER_MAPPING, records, source="demo", account=connection.external_id or connection.connection_id
        )

    # webhooks: signed like Shopify (base64 HMAC-SHA256 of the raw body)
    def webhook_account(self, headers: Mapping[str, str], body: bytes) -> str | None:
        return headers.get("x-demo-account")

    def verify_webhook(self, secret: str, headers: Mapping[str, str], body: bytes) -> bool:
        return hmac_matches(secret, body, headers.get("x-demo-hmac"))

    def webhook_delivery_id(self, headers: Mapping[str, str], body: bytes) -> str | None:
        return headers.get("x-demo-delivery")

    def webhook_topic(self, headers: Mapping[str, str], body: bytes) -> str | None:
        return headers.get("x-demo-topic")

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookDelivery:
        topic = headers.get("x-demo-topic", "")
        delivery = headers.get("x-demo-delivery", "")
        data = json.loads(body)
        if topic == "app/uninstalled":
            return WebhookDelivery(delivery_id=delivery, topic=topic, uninstalled=True)
        if topic == "customers/redact":
            return WebhookDelivery(
                delivery_id=delivery, topic=topic, privacy=PrivacyKind.REDACT_CUSTOMER, subject=data
            )
        return WebhookDelivery(delivery_id=delivery, topic=topic, stream=Stream.ORDERS, records=(data,))

    def register_webhooks(
        self, ctx: ConnectionContext, callback_url: str, topics: Sequence[str]
    ) -> tuple[str, ...]:
        body = ctx.http.json(
            "POST", "webhooks", json_body={"url": callback_url, "topics": list(topics)}, idempotency_key="reg"
        )
        return tuple(body["topics"])


class TestCipher:
    """A reversible stand-in for Fernet in the light test environment (no `cryptography`). Not secure."""

    __test__ = False

    def __init__(self, fingerprint: str = "test-key") -> None:
        self.fingerprint = fingerprint

    def encrypt(self, data: bytes) -> str:
        return base64.b64encode(data[::-1]).decode()

    def decrypt(self, token: str) -> bytes:
        return base64.b64decode(token)[::-1]


APP = AppCredentials(client_id="client_1", client_secret="secret_1")


@dataclass
class Rig:
    platform: FakePlatform
    clock: ManualClock
    engine: ConnectorEngine
    store: ConnectorStore
    queue: SqlJobQueue
    events: SqlEventStore
    alerts: InMemoryAlertStore
    history_calls: list[int]


def make_rig(
    platform: FakePlatform | None = None, *, now: datetime = T0, connector: SaasConnector | None = None
) -> Rig:
    platform = platform or FakePlatform()
    db = memory_engine()
    create_all(db)
    create_connector_tables(db)
    store = ConnectorStore(db)
    events = SqlEventStore(db)
    alerts = InMemoryAlertStore()
    registry = ConnectorRegistry([connector or DemoConnector()])
    monitor = HealthMonitor(store, registry, desk=IncidentDesk(store, alerts))
    clock = ManualClock(now)
    history: list[int] = []
    transport = platform.transport()
    engine = ConnectorEngine(
        registry=registry,
        store=store,
        vault=SqlTokenVault(store, TestCipher()),
        sink=events.append,
        clock=clock,
        apps={"demo": APP},
        transport_for=lambda base: transport,
        monitor=monitor,
        history_hook=lambda connection, n, at: history.append(n),
        webhook_base_url="https://autonomy.example/v2/connectors/webhooks",
    )
    return Rig(platform, clock, engine, store, SqlJobQueue(db), events, alerts, history)


def connect(rig: Rig, now: datetime = T0, *, seed: bool = True) -> Connection:
    """Connect through the real OAuth flow against the fake platform."""
    url = rig.engine.begin_oauth(
        "demo", business_id="biz_1", redirect_uri="https://autonomy.example/cb", now=now
    )
    state = parse_qs(url.split("?", 1)[1])["state"][0]
    return rig.engine.finish_oauth(
        "demo", {"state": state, "code": "code_ok"}, now, rig.queue if seed else None
    )


def signed(
    body: dict[str, Any], *, topic: str, delivery: str, account: str = "acct_9", secret: str = WEBHOOK_SECRET
) -> tuple[dict[str, str], bytes]:
    from autonomy.connectors.webhooks import sign

    raw = json.dumps(body).encode()
    headers = {
        "X-Demo-Hmac": sign(secret, raw),
        "X-Demo-Topic": topic,
        "X-Demo-Delivery": delivery,
        "X-Demo-Account": account,
    }
    return headers, raw
