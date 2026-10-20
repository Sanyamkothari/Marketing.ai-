"""A fake Klaviyo that answers in the documented JSON:API shapes, and a rig around the real engine.

Shapes follow Klaviyo's OpenAPI document for revision 2026-10-15 (github.com/klaviyo/openapi,
`openapi/stable.json`): `GET /api/profiles` with `additional-fields[profile]=subscriptions` and the
`updated` greater-than / less-than filter, `GET /api/events` filtered by `metric_id` and `datetime`
with `include=profile` (profiles come back in `included`), `GET /api/metrics`, `GET /api/accounts`,
`POST /api/events` (202, no body; `unique_id` deduplicates per profile and metric), Create / Get /
Update Flow, Get / Create Template, cursor pages through `links.next` absolute URLs, JSON:API
errors (`{"errors": [{"code", "title", "detail"}]}`), 429 with `Retry-After`, the `revision` header
(refused without it) and the OAuth token endpoint (HTTP Basic client auth, PKCE S256, form body).
Unknown fields are sprinkled in so the connector is tested against drift.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx

from autonomy.connectors import (
    AppCredentials,
    Connection,
    ConnectorEngine,
    ConnectorRegistry,
    ConnectorStore,
    HealthMonitor,
    IncidentDesk,
    ManualClock,
    create_connector_tables,
)
from autonomy.connectors.klaviyo import KlaviyoConnector
from autonomy.connectors.tokens import SqlTokenVault
from autonomy.runtime.stores import InMemoryAlertStore
from autonomy.store import SqlEventStore, SqlJobQueue, create_all, memory_engine
from tests.v2.connectors.base_support import TestCipher

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
REVISION = "2026-10-15"
HOST = "a.klaviyo.com"
ACCOUNT_ID = "AbC123"
API_KEY = "pk_live_secret_0123456789abcdef"
APP = AppCredentials(client_id="kl_client", client_secret="kl_secret")
_FILTER = re.compile(
    r"(equals|greater-than|less-than|greater-or-equal|less-or-equal|starts-with)\(([a-z_.]+),(\"?)([^\"),]*)\3\)"
)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def ts_of(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def jsonapi_error(
    status: int, code: str, detail: str = "", headers: Mapping[str, str] | None = None
) -> httpx.Response:
    body = {
        "errors": [
            {"id": "e1", "status": status, "code": code, "title": code, "detail": detail, "source": {}}
        ]
    }
    return httpx.Response(status, json=body, headers=dict(headers or {}))


def subscriptions(
    email: str | None = "SUBSCRIBED",
    *,
    can_email: bool = True,
    sms: str | None = None,
    can_sms: bool = False,
    at: datetime = T0,
    suppressed: str | None = None,
) -> dict[str, Any]:
    subs: dict[str, Any] = {}
    if email is not None:
        subs["email"] = {
            "marketing": {
                "can_receive_email_marketing": can_email,
                "consent": email,
                "consent_timestamp": iso(at),
                "last_updated": iso(at),
                "method": "PREFERENCE_PAGE",
                "suppression": [{"reason": suppressed, "timestamp": iso(at)}] if suppressed else [],
                "list_suppressions": [],
                "brand_new_flag": True,
            }
        }
    if sms is not None:
        subs["sms"] = {
            "marketing": {
                "can_receive_sms_marketing": can_sms,
                "consent": sms,
                "consent_timestamp": iso(at),
                "method": "TEXT",
                "last_updated": iso(at),
            }
        }
    return subs


@dataclass
class FakeKlaviyo:
    """The platform. Tests change its records and queue failures; every request is recorded."""

    api_key: str = API_KEY
    access_token: str = "kl_at_1"
    refresh_token: str = "kl_rt_1"
    token_seq: int = 1
    revoked: bool = False
    expected_challenge: str | None = None
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    metrics: dict[str, str] = field(default_factory=dict)
    """metric id -> name."""
    events: list[dict[str, Any]] = field(default_factory=list)
    created_events: list[dict[str, Any]] = field(default_factory=list)
    """Every Create Event body Klaviyo recorded (after unique_id dedupe)."""
    event_posts: int = 0
    flows: dict[str, dict[str, Any]] = field(default_factory=dict)
    templates: dict[str, dict[str, Any]] = field(default_factory=dict)
    flow_status_on_create: str = "draft"
    flow_create_error: dict[str, httpx.Response] = field(default_factory=dict)
    """channel ('email'/'sms') -> the response Create Flow gives for it."""
    clock: ManualClock | None = None
    """The rig's clock (set by `make_klaviyo_rig`): when profiles made by events were created."""
    backfill_creates_metric: bool = True
    """False: a `backfill` event is recorded (and its profile made) but creates no metric."""
    metric_lag: int = 0
    """How many metric listings a newly created metric stays invisible for (Klaviyo is async)."""
    default_sender: str | None = "hello@acme.example"
    page_size: int | None = None
    failures: list[httpx.Response | Exception] = field(default_factory=list)
    expired_cursors: set[str] = field(default_factory=set)
    calls: list[httpx.Request] = field(default_factory=list)
    token_calls: list[dict[str, str]] = field(default_factory=list)
    _seq: int = 0
    _unique: set[tuple[str, str, str]] = field(default_factory=set)
    _pending_metrics: dict[str, int] = field(default_factory=dict)

    # -- seeding ---------------------------------------------------------------------------------

    def add_profile(
        self,
        pid: str,
        *,
        email: str | None,
        updated: datetime,
        created: datetime | None = None,
        phone: str | None = None,
        subs: dict[str, Any] | None = None,
        location: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        attributes: dict[str, Any] = {
            "email": email,
            "phone_number": phone,
            "external_id": None,
            "first_name": "Pat",
            "created": iso(created or updated - timedelta(days=30)),
            "updated": iso(updated),
            "location": location if location is not None else {"country": "GB", "timezone": "Europe/London"},
            "properties": {"favourite": "trail"},
            "subscriptions": subs if subs is not None else subscriptions(at=updated),
            **extra,
        }
        self.profiles[pid] = {"type": "profile", "id": pid, "attributes": attributes, "links": {"self": "x"}}

    def add_metric(self, name: str, mid: str | None = None) -> str:
        mid = mid or "M" + hashlib.sha1(name.encode()).hexdigest()[:6]
        self.metrics[mid] = name
        return mid

    def metric_id(self, name: str) -> str:
        return next(mid for mid, n in self.metrics.items() if n == name)

    def add_event(
        self,
        metric: str,
        *,
        pid: str,
        at: datetime,
        props: dict[str, Any] | None = None,
        eid: str | None = None,
    ) -> str:
        mid = self.metric_id(metric) if metric in self.metrics.values() else self.add_metric(metric)
        self._seq += 1
        eid = eid or f"EV{self._seq:05d}"
        self.events.append(
            {"id": eid, "metric_id": mid, "profile_id": pid, "datetime": at, "props": props or {}}
        )
        return eid

    # -- transport -------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path == "/oauth/token":
            return self._token(request)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                if request.method == "POST" and request.url.path == "/api/events":
                    self._create_event(request)  # the request arrived; only the answer was lost
                raise failure
            return failure
        if request.headers.get("revision") != REVISION:
            return jsonapi_error(400, "invalid_revision", "a revision header is required")
        auth = request.headers.get("authorization", "")
        if self.revoked or auth not in (f"Klaviyo-API-Key {self.api_key}", f"Bearer {self.access_token}"):
            return jsonapi_error(401, "not_authenticated", "Incorrect authentication credentials.")
        path, method = request.url.path, request.method
        if path == "/api/accounts" and method == "GET":
            return self._accounts()
        if path == "/api/profiles" and method == "GET":
            return self._profiles(request)
        if path == "/api/metrics" and method == "GET":
            return self._metrics(request)
        if path == "/api/events" and method == "GET":
            return self._events(request)
        if path == "/api/events" and method == "POST":
            return self._create_event(request)
        if path == "/api/flows" and method == "GET":
            return self._list(request, self.flows)
        if path == "/api/flows" and method == "POST":
            return self._create_flow(request)
        if path.startswith("/api/flows/") and method == "PATCH":
            fid = path.rsplit("/", 1)[1]
            body = json.loads(request.content)
            self.flows[fid]["attributes"]["status"] = body["data"]["attributes"]["status"]
            return httpx.Response(200, json={"data": self.flows[fid]})
        if path == "/api/templates" and method == "GET":
            return self._list(request, self.templates)
        if path == "/api/templates" and method == "POST":
            body = json.loads(request.content)["data"]["attributes"]
            tid = f"TPL{len(self.templates) + 1}"
            self.templates[tid] = {"type": "template", "id": tid, "attributes": {**body, "created": iso(T0)}}
            return httpx.Response(201, json={"data": self.templates[tid]})
        if path in ("/api/lists", "/api/segments") and method == "GET":
            kind = "list" if path.endswith("lists") else "segment"
            item = {
                "type": kind,
                "id": f"{kind[0].upper()}1",
                "attributes": {"name": f"VIP {kind}", "updated": iso(T0)},
            }
            return httpx.Response(
                200, json={"data": [item], "links": {"self": str(request.url), "next": None}}
            )
        return jsonapi_error(404, "not_found", f"{method} {path}")

    # -- routes ----------------------------------------------------------------------------------

    def _accounts(self) -> httpx.Response:
        info: dict[str, Any] = {"organization_name": "Acme Outdoors", "default_sender_name": "Acme"}
        if self.default_sender:
            info["default_sender_email"] = self.default_sender
        account = {
            "type": "account",
            "id": ACCOUNT_ID,
            "attributes": {
                "test_account": False,
                "contact_information": info,
                "timezone": "Europe/London",
                "preferred_currency": "GBP",
                "public_api_key": "PUB123",
                "locale": "en-GB",
            },
            "links": {"self": "x"},
        }
        return httpx.Response(200, json={"data": [account], "links": {"self": "x"}})

    def _page(self, request: httpx.Request, rows: list[Any], default: int) -> tuple[list[Any], str | None]:
        params = request.url.params
        cursor = params.get("page[cursor]")
        if cursor and cursor in self.expired_cursors:
            self.expired_cursors.discard(cursor)
            raise _ExpiredError()
        size = self.page_size or int(params.get("page[size]") or default)
        offset = int(cursor) if cursor else 0
        page = rows[offset : offset + size]
        if offset + size >= len(rows):
            return page, None
        query = {k: v for k, v in params.items() if k != "page[cursor]"} | {
            "page[cursor]": str(offset + size)
        }
        return page, f"https://{HOST}{request.url.path}?{urlencode(query)}"

    def _filters(self, request: httpx.Request) -> list[tuple[str, str, str]]:
        text = request.url.params.get("filter") or ""
        return [(op, fld, value) for op, fld, _q, value in _FILTER.findall(text)]

    def _profiles(self, request: httpx.Request) -> httpx.Response:
        rows = list(self.profiles.values())
        for op, fld, value in self._filters(request):
            if fld in ("id", "email", "phone_number", "external_id") and op == "equals":
                rows = [r for r in rows if (r["id"] if fld == "id" else r["attributes"].get(fld)) == value]
                continue
            if fld != "updated":
                return jsonapi_error(400, "invalid", f"filter on {fld} is not supported")
            if op not in ("greater-than", "less-than"):
                return jsonapi_error(400, "invalid", f"{op} is not allowed on updated")
            bound = ts_of(value)
            rows = [
                r
                for r in rows
                if (
                    ts_of(r["attributes"]["updated"]) > bound
                    if op == "greater-than"
                    else ts_of(r["attributes"]["updated"]) < bound
                )
            ]
        rows.sort(key=lambda r: (r["attributes"]["updated"], r["id"]))
        if request.url.params.get("additional-fields[profile]") != "subscriptions":
            rows = [
                {**r, "attributes": {k: v for k, v in r["attributes"].items() if k != "subscriptions"}}
                for r in rows
            ]
        try:
            page, nxt = self._page(request, rows, 20)
        except _ExpiredError:
            return jsonapi_error(400, "invalid", "page cursor is no longer valid")
        return httpx.Response(
            200, json={"data": page, "links": {"self": str(request.url), "next": nxt, "prev": None}}
        )

    def _metrics(self, request: httpx.Request) -> httpx.Response:
        hidden = {mid for mid, left in self._pending_metrics.items() if left > 0}
        for mid in list(self._pending_metrics):
            self._pending_metrics[mid] -= 1
            if self._pending_metrics[mid] <= 0:
                del self._pending_metrics[mid]
        rows = [
            {
                "type": "metric",
                "id": mid,
                "attributes": {
                    "name": name,
                    "created": iso(T0),
                    "updated": iso(T0),
                    "integration": {"name": "Klaviyo"},
                },
            }
            for mid, name in sorted(self.metrics.items())
            if mid not in hidden
        ]
        page, nxt = self._page(request, rows, 200)
        return httpx.Response(200, json={"data": page, "links": {"self": str(request.url), "next": nxt}})

    def _events(self, request: httpx.Request) -> httpx.Response:
        rows = list(self.events)
        for op, fld, value in self._filters(request):
            if fld == "metric_id":
                rows = [r for r in rows if r["metric_id"] == value]
            elif fld == "datetime":
                rows = [r for r in rows if _compare(op, r["datetime"], ts_of(value))]
        rows.sort(key=lambda r: (r["datetime"], r["id"]))
        try:
            page, nxt = self._page(request, rows, 200)
        except _ExpiredError:
            return jsonapi_error(400, "invalid", "page cursor is no longer valid")
        data = [
            {
                "type": "event",
                "id": r["id"],
                "attributes": {
                    "timestamp": int(r["datetime"].timestamp()),
                    "datetime": r["datetime"].isoformat(),
                    "uuid": f"uuid-{r['id']}",
                    "event_properties": r["props"],
                    "surprise": [1, 2],
                },
                "relationships": {
                    "profile": {"data": {"type": "profile", "id": r["profile_id"]}},
                    "metric": {"data": {"type": "metric", "id": r["metric_id"]}},
                },
                "links": {"self": "x"},
            }
            for r in page
        ]
        included = []
        if "profile" in (request.url.params.get("include") or ""):
            for pid in sorted({r["profile_id"] for r in page}):
                profile = self.profiles.get(pid)
                if profile is not None:
                    a = profile["attributes"]
                    included.append(
                        {
                            "type": "profile",
                            "id": pid,
                            "attributes": {
                                "email": a.get("email"),
                                "phone_number": a.get("phone_number"),
                                "external_id": a.get("external_id"),
                            },
                        }
                    )
        return httpx.Response(
            200, json={"data": data, "included": included, "links": {"self": str(request.url), "next": nxt}}
        )

    def _create_event(self, request: httpx.Request) -> httpx.Response:
        self.event_posts += 1
        try:
            attributes = json.loads(request.content)["data"]["attributes"]
            name = attributes["metric"]["data"]["attributes"]["name"]
            profile = attributes["profile"]["data"]["attributes"]
        except (KeyError, TypeError, ValueError):
            return jsonapi_error(400, "invalid", "the payload provided in the request is invalid")
        if not any(profile.get(k) for k in ("email", "phone_number", "external_id")):
            return jsonapi_error(400, "invalid", "a profile identifier is required")
        if profile.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(profile["email"])):
            return jsonapi_error(400, "invalid", "Invalid email address")
        who = profile.get("email") or profile.get("phone_number") or profile.get("external_id")
        unique = (str(who), name, str(attributes.get("unique_id") or attributes.get("time")))
        if unique in self._unique:
            return httpx.Response(202)
        self._unique.add(unique)
        self._profile_for_event(profile)
        if name not in self.metrics.values() and (
            self.backfill_creates_metric or not attributes.get("backfill")
        ):
            mid = self.add_metric(name)
            if self.metric_lag:
                self._pending_metrics[mid] = self.metric_lag
        self.created_events.append(attributes)
        return httpx.Response(202)

    def _profile_for_event(self, identifiers: Mapping[str, Any]) -> None:
        """Klaviyo creates the profile an event names when no profile has those identifiers."""
        for key in ("email", "phone_number", "external_id"):
            value = identifiers.get(key)
            if value and any(p["attributes"].get(key) == value for p in self.profiles.values()):
                return
        pid = f"PX{len(self.profiles) + 1}"
        self.add_profile(
            pid,
            email=identifiers.get("email"),
            phone=identifiers.get("phone_number"),
            updated=self.clock.now() if self.clock else T0,
            created=self.clock.now() if self.clock else T0,
            subs={},
            location={},
            external_id=identifiers.get("external_id"),
        )

    def _list(self, request: httpx.Request, table: Mapping[str, dict[str, Any]]) -> httpx.Response:
        rows = sorted(table.values(), key=lambda r: r["id"])
        for op, fld, value in self._filters(request):
            if fld == "name" and op == "equals":
                rows = [r for r in rows if r["attributes"].get("name") == value]
            elif fld == "name" and op == "starts-with":
                rows = [r for r in rows if str(r["attributes"].get("name", "")).startswith(value)]
            elif fld == "archived":
                rows = [r for r in rows if not r["attributes"].get("archived")]
        page, nxt = self._page(request, rows, 50)
        return httpx.Response(200, json={"data": page, "links": {"self": str(request.url), "next": nxt}})

    def _create_flow(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)["data"]["attributes"]
        definition = body["definition"]
        trigger = definition["triggers"][0]
        channel = trigger["trigger_filter"]["condition_groups"][0]["conditions"][0]["filter"]["value"]
        if channel in self.flow_create_error:
            return self.flow_create_error[channel]
        if trigger["id"] not in self.metrics:
            return jsonapi_error(400, "invalid", "unknown metric")
        fid = f"FL{len(self.flows) + 1}"
        self.flows[fid] = {
            "type": "flow",
            "id": fid,
            "attributes": {
                "name": body["name"],
                "status": self.flow_status_on_create,
                "archived": False,
                "trigger_type": "Metric",
                "definition": definition,
            },
        }
        return httpx.Response(201, json={"data": self.flows[fid], "links": {"self": "x"}})

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_calls.append(form)
        expected = "Basic " + base64.b64encode(f"{APP.client_id}:{APP.client_secret}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") == "authorization_code":
            verifier = form.get("code_verifier", "")
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            )
            if form.get("code") != "code_ok" or (
                self.expected_challenge and challenge != self.expected_challenge
            ):
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "Code challenge failed"}
                )
            return httpx.Response(200, json=self._issue(new_refresh=True))
        if form.get("grant_type") == "refresh_token":
            if self.revoked or form.get("refresh_token") != self.refresh_token:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._issue(new_refresh=False))
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _issue(self, *, new_refresh: bool) -> dict[str, Any]:
        self.token_seq += 1
        self.access_token = f"kl_at_{self.token_seq}"
        body: dict[str, Any] = {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "accounts:read events:read events:write flows:read flows:write profiles:read",
        }
        if new_refresh:
            body["refresh_token"] = self.refresh_token
        return body


class _ExpiredError(Exception):
    pass


def _compare(op: str, value: datetime, bound: datetime) -> bool:
    if op == "greater-or-equal":
        return value >= bound
    if op == "greater-than":
        return value > bound
    if op == "less-or-equal":
        return value <= bound
    return value < bound


@dataclass
class KlaviyoRig:
    fake: FakeKlaviyo
    clock: ManualClock
    engine: ConnectorEngine
    store: ConnectorStore
    queue: SqlJobQueue
    events: SqlEventStore
    alerts: InMemoryAlertStore
    connector: KlaviyoConnector


def make_klaviyo_rig(
    fake: FakeKlaviyo | None = None, *, now: datetime = T0, connector: KlaviyoConnector | None = None
) -> KlaviyoRig:
    fake = fake or FakeKlaviyo()
    db = memory_engine()
    create_all(db)
    create_connector_tables(db)
    store = ConnectorStore(db)
    events = SqlEventStore(db)
    alerts = InMemoryAlertStore()
    if connector is None:
        from autonomy.channels.klaviyo import EventStoreHandoffs

        connector = KlaviyoConnector(handoffs=EventStoreHandoffs(events))
    registry = ConnectorRegistry([connector])
    clock = ManualClock(now)
    fake.clock = clock
    transport = fake.transport()
    engine = ConnectorEngine(
        registry=registry,
        store=store,
        vault=SqlTokenVault(store, TestCipher()),
        sink=events.append,
        clock=clock,
        apps={"klaviyo": APP},
        transport_for=lambda base: transport,
        monitor=HealthMonitor(store, registry, desk=IncidentDesk(store, alerts)),
    )
    return KlaviyoRig(fake, clock, engine, store, SqlJobQueue(db), events, alerts, connector)


def connect_key(rig: KlaviyoRig, now: datetime | None = None, **config: str) -> Connection:
    return rig.engine.connect_api_key(
        "klaviyo", business_id="biz_1", api_key=API_KEY, now=now or rig.clock.now(), config=config or None
    )


def connect_oauth(rig: KlaviyoRig, now: datetime | None = None) -> tuple[Connection, str]:
    when = now or rig.clock.now()
    url = rig.engine.begin_oauth(
        "klaviyo", business_id="biz_1", redirect_uri="https://autonomy.example/cb", now=when
    )
    query = parse_qs(url.split("?", 1)[1])
    rig.fake.expected_challenge = query["code_challenge"][0]
    connection = rig.engine.finish_oauth(
        "klaviyo", {"state": query["state"][0], "code": "code_ok"}, when, rig.queue
    )
    return connection, url


def connect_ready(rig: KlaviyoRig, **config: str) -> Connection:
    """A key connection whose send setup has finished (the metric exists, the flows are live)."""
    from autonomy.channels.klaviyo import ensure_klaviyo_setup

    connection = connect_key(rig, **config)
    status = ensure_klaviyo_setup(rig.engine, connection.connection_id, rig.clock.now())
    assert status is not None and status.state == "ready", status
    return connection


def requests_to(fake: FakeKlaviyo, method: str, path: str) -> list[httpx.Request]:
    return [r for r in fake.calls if r.method == method and r.url.path == path]
