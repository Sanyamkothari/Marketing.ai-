"""`KlaviyoConnector`: read Klaviyo continuously, and hand Autonomy's decisions to it to send.

**Auth.** A public OAuth app (PKCE, Basic client auth, refresh before the hour is up, a keep-alive
refresh every 30 days because Klaviyo revokes refresh tokens unused for 90) or, for pilots, a
private API key pasted once (`engine.connect_api_key`), sent as `Authorization: Klaviyo-API-Key`.
Every request carries the pinned `revision` header (`api.API_REVISION`).

**Streams** (no webhooks: Klaviyo has none for this data, so every stream is polled hourly with an
overlap, which is also the catch-up):

| Stream | Klaviyo endpoint | Incremental by | Gives |
|---|---|---|---|
| consent (critical) | `GET /api/profiles` + `additional-fields[profile]=subscriptions` | `updated` | SIGNUP (country, time zone) and email/SMS consent granted or revoked |
| engagement (critical) | `GET /api/events`, one pass per message metric | `datetime` | DELIVER, OPEN, CLICK, BOUNCE, COMPLAINT, UNSUBSCRIBE, FAIL; linked to the decision when it came from Autonomy's flow |
| events | `GET /api/events`, one pass per browsing metric | `datetime` | VIEW, CART, VISIT |

Events are read per metric id (`filter=equals(metric_id,...)`), so the shop's Placed Order and
other metrics are never downloaded. A page cursor is `<metric id>|<links.next>`, so a window resumes
inside the metric it stopped in, and the metric list can change between runs without skipping one.

**Writing.** `write(ctx, "decision_event", payload, idempotency_key=...)` creates one event of
`Autonomy Decision` on the person's profile with `unique_id` = the decision id: Klaviyo records only
the first event per (profile, metric, unique_id), so a retried send is never sent twice. The flow
that sends it is created by `send_setup` (`setup.ensure_send_setup`).

**Pacing.** On top of the base client's bucket and `RateLimit-*` / `Retry-After` handling, every
call takes a token from its endpoint's burst and steady buckets (`api.EndpointPacer`).
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from autonomy.connectors.base import (
    AccessDenied,
    AuthMethod,
    Connection,
    ConnectionContext,
    ConnectorCapabilities,
    ConnectorFailure,
    CursorExpired,
    Identity,
    MappedRecords,
    RateSpec,
    RecordKey,
    RemoteError,
    SaasConnector,
    SetupMode,
    Stream,
    StreamPage,
    StreamSpec,
    SyncWindow,
    WriteActionSpec,
    WriteResult,
    map_with,
)
from autonomy.connectors.http import jsonapi_next
from autonomy.connectors.klaviyo.api import (
    API_BASE,
    API_REVISION,
    AUTONOMY_FLOW_PREFIX,
    DECISION_METRIC,
    KLAVIYO_OAUTH,
    EndpointPacer,
    klaviyo_time,
    parse_klaviyo_time,
)
from autonomy.connectors.klaviyo.client import KlaviyoClient
from autonomy.connectors.klaviyo.mapping import (
    BEHAVIOUR_METRICS,
    ENGAGEMENT_METRICS,
    KLAVIYO_MAPPING,
    HandoffLookup,
    _list,
    event_rows,
    profile_rows,
)
from autonomy.connectors.klaviyo.setup import SetupStatus, _mapping, _text, ensure_send_setup
from autonomy.connectors.tokens import TokenSet
from autonomy.contracts import Channel

__all__ = [
    "DECISION_EVENT",
    "MAX_EVENT_PROPERTIES",
    "Audience",
    "KlaviyoConnector",
    "decision_event_body",
]

DECISION_EVENT: Final[str] = "decision_event"
"""The write action: one `Autonomy Decision` event that Autonomy's send flow turns into a message."""
MAX_EVENT_PROPERTIES: Final[int] = 400
MAX_PROPERTY_CHARS: Final[int] = 100_000
"""Klaviyo's Create Event limits: at most 400 properties, no string over 100 KB."""
PROFILE_PAGE: Final[int] = 100
EVENT_PAGE: Final[int] = 200
METRIC_PLAN_TTL: Final[timedelta] = timedelta(hours=6)
OUR_FLOWS_TTL: Final[timedelta] = timedelta(hours=1)
SETUP_READY_TTL: Final[timedelta] = timedelta(hours=1)
SETUP_RETRY_TTL: Final[timedelta] = timedelta(minutes=10)
ACCOUNT_TTL: Final[timedelta] = timedelta(hours=24)


@dataclass(frozen=True)
class Audience:
    """A Klaviyo list or segment, for the Connect screen and briefs (membership is not synced)."""

    kind: str
    audience_id: str
    name: str
    updated_at: datetime | None


@dataclass
class _Cached:
    value: Any
    at: datetime


class KlaviyoConnector(SaasConnector):
    capabilities = ConnectorCapabilities(
        kind="klaviyo",
        label="Klaviyo",
        description="Reads profiles, consent, and email/SMS engagement; sends Autonomy's decisions through "
        "your own Klaviyo flows, so your unsubscribes, sender reputation and footer stay in Klaviyo.",
        auth=AuthMethod.OAUTH2,
        setup=SetupMode.AUTOMATIC,
        setup_note="Autonomy creates its own send flows and template in your Klaviyo account on connect. "
        "If your account does not allow that, the Connect screen shows the few steps to do once by hand.",
        streams=(
            StreamSpec(
                stream=Stream.CONSENT,
                cadence=timedelta(hours=1),
                overlap=timedelta(hours=2),
                backfill_days=3650,  # consent given years ago still counts: read every profile
                backfill_chunk=timedelta(days=90),
                min_history_days=30,
                required_fields=("id", "attributes"),
                critical=True,
            ),
            StreamSpec(
                stream=Stream.ENGAGEMENT,
                cadence=timedelta(hours=1),
                overlap=timedelta(hours=3),  # Klaviyo processes message events asynchronously
                backfill_days=180,
                backfill_chunk=timedelta(days=7),
                min_history_days=30,
                required_fields=("id", "attributes"),
                critical=True,  # delivery read-back keeps the holdout honest
            ),
            StreamSpec(
                stream=Stream.EVENTS,
                cadence=timedelta(hours=1),
                overlap=timedelta(hours=2),
                backfill_days=90,
                backfill_chunk=timedelta(days=7),
                min_history_days=14,
                required_fields=("id", "attributes"),
                critical=False,
            ),
        ),
        write_actions=(
            WriteActionSpec(
                name=DECISION_EVENT,
                description=f"Create one {DECISION_METRIC!r} event on the person's profile; Autonomy's "
                "flow sends it. unique_id = decision id, so a repeat is recorded once.",
            ),
        ),
        api_version=API_REVISION,
        rate=RateSpec(requests=50, per=timedelta(seconds=1), burst=100),
        sends=True,
    )
    oauth = KLAVIYO_OAUTH

    def __init__(
        self,
        *,
        handoffs: HandoffLookup | None = None,
        pacer: EndpointPacer | None = None,
        auto_setup: bool = True,
    ) -> None:
        """`handoffs` links engagement from Autonomy's flows to decisions (see
        `autonomy.channels.klaviyo.EventStoreHandoffs`); without it those events carry no decision id.
        `auto_setup` creates the send flows when the account is first identified (connect time)."""
        self.handoffs = handoffs
        self.pacer = pacer or EndpointPacer()
        self.auto_setup = auto_setup
        self._lock = threading.Lock()
        self._metric_plans: dict[tuple[str, Stream], _Cached] = {}
        self._our_flows: dict[str, _Cached] = {}
        self._accounts: dict[str, _Cached] = {}
        self._setup: dict[str, _Cached] = {}
        self._setup_locks: dict[str, threading.Lock] = {}

    # -- talking to Klaviyo ---------------------------------------------------------------------

    def base_url(self, connection: Connection) -> str:  # noqa: ARG002 - one API host for every account
        return API_BASE

    def auth_headers(self, connection: Connection, tokens: TokenSet) -> dict[str, str]:  # noqa: ARG002
        if tokens.token_type == "api_key":
            return {"Authorization": f"Klaviyo-API-Key {tokens.access_token}"}
        return {"Authorization": f"Bearer {tokens.access_token}"}

    def client(self, ctx: ConnectionContext) -> KlaviyoClient:
        return KlaviyoClient(ctx, self.pacer)

    def account(self, ctx: ConnectionContext, *, fresh: bool = False) -> Mapping[str, Any]:
        """The account document (`GET /api/accounts`, one item), cached for a day per connection."""
        key = ctx.connection.connection_id
        cached = self._accounts.get(key)
        if cached is not None and not fresh and ctx.now - cached.at < ACCOUNT_TTL:
            return dict(cached.value)
        doc = self.client(ctx).get(
            "accounts", "accounts", {"fields[account]": "contact_information,timezone,test_account"}
        )
        items = [i for i in _list(doc.get("data")) if isinstance(i, Mapping)]
        if not items:
            raise RemoteError("Klaviyo returned no account for these credentials", status=404)
        account = dict(items[0])
        with self._lock:
            self._accounts[key] = _Cached(account, ctx.now)
        return account

    def identify(self, ctx: ConnectionContext) -> Identity | None:
        account = self.account(ctx, fresh=True)
        account_id = _text(account.get("id"))
        if account_id is None:
            raise RemoteError("Klaviyo returned an account without an id", status=404)
        info = _mapping(_mapping(account.get("attributes")).get("contact_information"))
        label = _text(info.get("organization_name")) or f"Klaviyo account {account_id}"
        if self.auto_setup and ctx.connection.config.get("send", "on") != "off":
            # best effort: connecting must not fail because the send side is not ready yet
            with contextlib.suppress(ConnectorFailure):
                self.send_setup(ctx, channels_for(ctx.connection.config), refresh=True)
        return Identity(external_id=account_id, label=label)

    def check(self, ctx: ConnectionContext) -> None:
        self.account(ctx, fresh=True)

    # -- the send side ----------------------------------------------------------------------------

    def send_setup(
        self, ctx: ConnectionContext, channels: Sequence[Channel], *, refresh: bool = False
    ) -> SetupStatus:
        """Make sure Autonomy's send flows exist (`setup.ensure_send_setup`); cached per connection
        (an hour when ready, ten minutes otherwise) so the send path does not query flows each time."""
        key = ctx.connection.connection_id + ":" + ",".join(sorted(c.value for c in channels))
        cached = self._setup.get(key)
        if cached is not None and not refresh:
            status: SetupStatus = cached.value
            ttl = SETUP_READY_TTL if status.state == "ready" else SETUP_RETRY_TTL
            if ctx.now - cached.at < ttl:
                return status
        with self._setup_lock(ctx.connection.connection_id):
            # one check at a time per connection in this process, so two never both create a flow
            # (across processes, `setup` turns any duplicate live flow back to draft)
            return self._send_setup(ctx, channels, key)

    def _setup_lock(self, connection_id: str) -> threading.Lock:
        with self._lock:
            return self._setup_locks.setdefault(connection_id, threading.Lock())

    def _send_setup(self, ctx: ConnectionContext, channels: Sequence[Channel], key: str) -> SetupStatus:
        status = ensure_send_setup(
            self.client(ctx),
            account=self.account(ctx),
            channels=channels,
            config=ctx.connection.config,
            now=ctx.now,
        )
        with self._lock:
            self._setup[key] = _Cached(status, ctx.now)
            if status.flow_ids():
                known: frozenset[str] = frozenset()
                held = self._our_flows.get(ctx.connection.connection_id)
                if held is not None:
                    known = held.value
                self._our_flows[ctx.connection.connection_id] = _Cached(known | status.flow_ids(), ctx.now)
        return status

    def write(
        self,
        ctx: ConnectionContext,
        action: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> WriteResult:
        if action != DECISION_EVENT:
            return super().write(ctx, action, payload, idempotency_key=idempotency_key)
        body = decision_event_body(payload)
        unique_id = str(body["data"]["attributes"]["unique_id"])
        # unique_id deduplicates in Klaviyo, so a retry after a timeout is safe: the key lets the base
        # client retry the POST (and travels as Idempotency-Key, which Klaviyo ignores).
        self.client(ctx).post("events", "events:write", body, idempotency_key=idempotency_key)
        return WriteResult(
            accepted=True,
            provider_id=f"klaviyo:{ctx.connection.connection_id}:{unique_id}",
            detail="recorded by Klaviyo; its flow sends it",
        )

    def audiences(self, ctx: ConnectionContext) -> tuple[Audience, ...]:
        """Lists and segments (names and ids), for the Connect screen and briefs."""
        client = self.client(ctx)
        out: list[Audience] = []
        for kind, path in (("list", "lists"), ("segment", "segments")):
            for item in client.collect(path, path, {f"fields[{kind}]": "name,updated"}):
                audience_id = _text(item.get("id"))
                attributes = _mapping(item.get("attributes"))
                if audience_id is None:
                    continue
                out.append(
                    Audience(
                        kind=kind,
                        audience_id=audience_id,
                        name=_text(attributes.get("name")) or audience_id,
                        updated_at=parse_klaviyo_time(attributes.get("updated")),
                    )
                )
        return tuple(sorted(out, key=lambda a: (a.kind, a.name.casefold(), a.audience_id)))

    # -- reading ----------------------------------------------------------------------------------

    def read(
        self, ctx: ConnectionContext, stream: Stream, window: SyncWindow, cursor: str | None
    ) -> StreamPage:
        if stream is Stream.CONSENT:
            return self._read_profiles(ctx, window, cursor)
        if stream in (Stream.ENGAGEMENT, Stream.EVENTS):
            return self._read_events(ctx, stream, window, cursor)
        return StreamPage()

    def _read_profiles(self, ctx: ConnectionContext, window: SyncWindow, cursor: str | None) -> StreamPage:
        client = self.client(ctx)
        if cursor is None:
            # `updated` takes only greater-than / less-than: start one second early (the overlap and
            # the record versions absorb the repeat) so a profile updated exactly at `start` is read.
            bounds = [f"less-than(updated,{klaviyo_time(window.end)})"]
            if window.start is not None:
                bounds.insert(0, f"greater-than(updated,{klaviyo_time(window.start - timedelta(seconds=1))})")
            params: dict[str, str | int] = {
                "additional-fields[profile]": "subscriptions",
                "filter": bounds[0] if len(bounds) == 1 else f"and({','.join(bounds)})",
                "page[size]": PROFILE_PAGE,
                "sort": "updated",
            }
            doc = client.get("profiles", "profiles", params)
        else:
            doc = _follow(client, cursor, "profiles")
        records = tuple(i for i in _list(doc.get("data")) if isinstance(i, Mapping))
        return StreamPage(records=records, next_cursor=jsonapi_next(doc))

    def _read_events(
        self, ctx: ConnectionContext, stream: Stream, window: SyncWindow, cursor: str | None
    ) -> StreamPage:
        client = self.client(ctx)
        # A window starts with a fresh metric list: a metric created since the last one (the first
        # "Received SMS" of an account) must not be missed for hours and then fall out of the overlap.
        plan = self.metric_plan(ctx, stream, fresh=cursor is None)
        if not plan:
            return StreamPage()
        metric_id, next_url = _split_cursor(cursor)
        if metric_id is None:
            metric_id = plan[0][0]
        elif metric_id not in dict(plan):
            # the metric went away since this cursor was saved: carry on with the next one
            metric_id, next_url = _next_metric(plan, metric_id), None
            if metric_id is None:
                return StreamPage()
        names = dict(plan)
        if next_url is None:
            bounds = [f'equals(metric_id,"{metric_id}")']
            if window.start is not None:
                bounds.append(f"greater-or-equal(datetime,{klaviyo_time(window.start)})")
            bounds.append(f"less-than(datetime,{klaviyo_time(window.end)})")
            params: dict[str, str | int] = {
                "filter": f"and({','.join(bounds)})",
                "include": "profile",
                "fields[profile]": "email,phone_number,external_id",
                "fields[event]": "datetime,timestamp,event_properties,uuid",
                "sort": "datetime",
                "page[size]": EVENT_PAGE,
            }
            doc = client.get("events", "events:read", params)
        else:
            doc = _follow(client, next_url, "events:read")
        profiles = {
            str(i.get("id")): {"id": i.get("id"), **_mapping(i.get("attributes"))}
            for i in _list(doc.get("included"))
            if isinstance(i, Mapping) and i.get("type") == "profile"
        }
        ours = self.our_flows(ctx) if stream is Stream.ENGAGEMENT else frozenset()
        records: list[Mapping[str, Any]] = []
        for item in _list(doc.get("data")):
            if not isinstance(item, Mapping):
                continue
            profile_ref = _mapping(_mapping(_mapping(item.get("relationships")).get("profile")).get("data"))
            profile_id = _text(profile_ref.get("id"))
            props = _mapping(_mapping(item.get("attributes")).get("event_properties"))
            flow = _text(props.get("$flow"))
            records.append(
                {
                    **item,
                    "_metric": names.get(metric_id, ""),
                    "_profile": profiles.get(profile_id or "", {"id": profile_id}),
                    "_ours": flow is not None and flow in ours,
                }
            )
        nxt = jsonapi_next(doc)
        if nxt is not None:
            following: str | None = f"{metric_id}|{nxt}"
        else:
            after = _next_metric(plan, metric_id)
            following = after if after is not None else None
        return StreamPage(records=tuple(records), next_cursor=following)

    def metric_plan(
        self, ctx: ConnectionContext, stream: Stream, *, fresh: bool = False
    ) -> list[tuple[str, str]]:
        """(metric id, name) of the metrics a stream reads, sorted by id; cached for six hours (the
        reader asks for a fresh one at the start of every window, and uses the cache to resume)."""
        key = (ctx.connection.connection_id, stream)
        cached = self._metric_plans.get(key)
        if cached is not None and not fresh and ctx.now - cached.at < METRIC_PLAN_TTL:
            return list(cached.value)
        wanted = ENGAGEMENT_METRICS if stream is Stream.ENGAGEMENT else BEHAVIOUR_METRICS
        plan: list[tuple[str, str]] = []
        for item in self.client(ctx).collect("metrics", "metrics", {"fields[metric]": "name,integration"}):
            metric_id = _text(item.get("id"))
            name = _text(_mapping(item.get("attributes")).get("name"))
            if metric_id and name and name.casefold() in wanted:
                plan.append((metric_id, name))
        plan.sort()
        with self._lock:
            self._metric_plans[key] = _Cached(tuple(plan), ctx.now)
        return plan

    def our_flows(self, ctx: ConnectionContext) -> frozenset[str]:
        """Ids of Autonomy's send flows in this account (by name prefix), cached for an hour. A
        connection without `flows:read` gets none: its engagement is simply not linked to decisions."""
        key = ctx.connection.connection_id
        cached = self._our_flows.get(key)
        if cached is not None and ctx.now - cached.at < OUR_FLOWS_TTL:
            return frozenset(cached.value)
        try:
            items = self.client(ctx).collect(
                "flows",
                "flows:read",
                {"filter": f'starts-with(name,"{AUTONOMY_FLOW_PREFIX}")', "fields[flow]": "name,status"},
                max_pages=4,
            )
        except (AccessDenied, RemoteError):
            return frozenset(cached.value) if cached is not None else frozenset()
        ids = frozenset(t for i in items if (t := _text(i.get("id"))))
        with self._lock:
            self._our_flows[key] = _Cached(ids, ctx.now)
        return ids

    def record_key(self, stream: Stream, record: Mapping[str, Any]) -> RecordKey:
        record_id = record["id"]
        if not isinstance(record_id, str | int) or isinstance(record_id, bool):
            raise TypeError("a Klaviyo record id is a string")
        attributes = _mapping(record.get("attributes"))
        field = "updated" if stream is Stream.CONSENT else "datetime"
        return RecordKey(str(record_id), parse_klaviyo_time(attributes.get(field)))

    def map_records(
        self, stream: Stream, records: Sequence[Mapping[str, Any]], connection: Connection
    ) -> MappedRecords:
        identity = connection.config.get("identity", "auto")
        if stream is Stream.CONSENT:
            email_consent = connection.config.get("email_consent", "subscribed")
            rows = [
                row
                for record in records
                for row in profile_rows(record, identity=identity, email_consent=email_consent)
            ]
        elif stream in (Stream.ENGAGEMENT, Stream.EVENTS):
            rows = event_rows(records, identity=identity, handoffs=self.handoffs)
        else:
            return MappedRecords()
        account = connection.external_id or connection.connection_id
        return map_with(KLAVIYO_MAPPING, rows, source="klaviyo", account=account)


def channels_for(config: Mapping[str, str]) -> tuple[Channel, ...]:
    """The channels Autonomy sends through Klaviyo for a connection: email, plus SMS when `sms=on`."""
    return (Channel.EMAIL, Channel.SMS) if config.get("sms") == "on" else (Channel.EMAIL,)


def decision_event_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The Create Event body for a handed-off decision. `payload` has `profile` (`email`,
    `phone_number` and/or `external_id`), `properties` (flat), `unique_id` and optionally `time`."""
    profile = {
        k: v
        for k, v in _mapping(payload.get("profile")).items()
        if k in ("email", "phone_number", "external_id") and isinstance(v, str) and v
    }
    if not profile:
        raise ValueError("a Klaviyo decision event needs the person's email, phone number or external id")
    properties = dict(_mapping(payload.get("properties")))
    unique_id = payload.get("unique_id")
    if not isinstance(unique_id, str) or not unique_id:
        raise ValueError("a Klaviyo decision event needs a unique_id (the decision id)")
    if len(properties) > MAX_EVENT_PROPERTIES:
        raise ValueError(f"a Klaviyo event takes at most {MAX_EVENT_PROPERTIES} properties")
    for name, value in properties.items():
        if isinstance(value, str) and len(value) > MAX_PROPERTY_CHARS:
            raise ValueError(f"the {name} property is longer than Klaviyo accepts")
    attributes: dict[str, Any] = {
        "properties": properties,
        "metric": {"data": {"type": "metric", "attributes": {"name": DECISION_METRIC}}},
        "profile": {"data": {"type": "profile", "attributes": profile}},
        "unique_id": unique_id,
    }
    when = payload.get("time")
    if isinstance(when, str) and when:
        attributes["time"] = when
    return {"data": {"type": "event", "attributes": attributes}}


# --- helpers ------------------------------------------------------------------------------------


def _follow(client: KlaviyoClient, url: str, group: str) -> Mapping[str, Any]:
    """A saved `links.next`; one Klaviyo no longer accepts restarts the window (ids dedupe)."""
    try:
        return client.get(url, group)
    except RemoteError as exc:
        if exc.status in (400, 404, 410):
            raise CursorExpired("Klaviyo no longer accepts the saved page cursor") from exc
        raise


def _split_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    if not cursor:
        return None, None
    metric_id, _, url = cursor.partition("|")
    return (metric_id or None), (url or None)


def _next_metric(plan: Sequence[tuple[str, str]], after: str) -> str | None:
    return next((metric_id for metric_id, _ in plan if metric_id > after), None)
