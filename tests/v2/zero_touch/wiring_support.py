"""A connected shop for the wiring tests: the real runtime, the real connector engine, fake platforms.

`make_shop()` builds what `python -m autonomy.runtime.worker` builds with `AUTONOMY_CONNECTORS=true`
(`build_runtime(..., connectors=build_connector_engine(...))`) on one in-memory SQLite database,
with `FakeKlaviyo` and `FakeShopify` (the documented shapes, `tests/v2/connectors/`) behind
`httpx.MockTransport`, a fake SES adapter as our own fallback, a manual clock and a test cipher.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from autonomy.connectors import Connection, ManualClock
from autonomy.connectors.base import Lane
from autonomy.connectors.store import SyncPosition
from autonomy.contracts import (
    Action,
    AutonomyLevel,
    Channel,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    GoalStatus,
    Reward,
    stable_id,
)
from autonomy.fakes import FakeChannelAdapter
from autonomy.fakes.samples import sample_brief, sample_content, sample_goal
from autonomy.runtime.service import AutonomyRuntime
from autonomy.runtime.settings import ConnectorWiring, RuntimeSettings, build_connector_engine, build_runtime
from autonomy.runtime.stores import SqlAlertStore, create_record_tables
from autonomy.store import SqlEventStore, create_all, memory_engine
from tests.v2.connectors.base_support import TestCipher
from tests.v2.connectors.klaviyo_support import API_KEY, FakeKlaviyo
from tests.v2.connectors.shopify_support import CLIENT_ID, CLIENT_SECRET, SHOP, FakeShopify

T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)  # a Monday, before the 10:00 sweep
SWEEP = T0.replace(hour=10)
START = T0 - timedelta(days=1)
BUSINESS = "biz_1"
GOAL = "goal_winback"
ENV = {
    "AUTONOMY_KLAVIYO_CLIENT_ID": "kl_client",
    "AUTONOMY_KLAVIYO_CLIENT_SECRET": "kl_secret",
    "AUTONOMY_SHOPIFY_CLIENT_ID": CLIENT_ID,
    "AUTONOMY_SHOPIFY_CLIENT_SECRET": CLIENT_SECRET,
}


class TeardownKlaviyo(FakeKlaviyo):
    """FakeKlaviyo plus Delete Flow / Delete Template and the OAuth revoke endpoint."""

    revoked_tokens: list[str]
    deleted: list[str]

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.revoked_tokens = []
        self.deleted = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oauth/revoke" and request.method == "POST":
            self.calls.append(request)
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.revoked_tokens.append(form.get("token", ""))
            return httpx.Response(200)
        if request.method == "DELETE" and path.startswith(("/api/flows/", "/api/templates/")):
            self.calls.append(request)
            object_id = path.rsplit("/", 1)[1]
            table = self.flows if path.startswith("/api/flows/") else self.templates
            if table.pop(object_id, None) is None:
                return httpx.Response(404)
            self.deleted.append(path)
            return httpx.Response(204)
        return super().handle(request)


class TeardownShopify(FakeShopify):
    """FakeShopify plus `DELETE /admin/api_permissions/current.json` (the app uninstalls itself)."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and request.url.path == "/admin/api_permissions/current.json":
            self.calls.append(request)
            if request.headers.get("x-shopify-access-token") != self.access_token:
                return httpx.Response(401)
            self.revoked = True
            self.subscriptions.clear()
            return httpx.Response(200, json={})
        return super().handle(request)


@dataclass
class OfferPolicy:
    """Emails everyone in the audience (content ci_1, 10% off), sending at once."""

    version: str = "offer-v0"
    decided: list[str] = field(default_factory=list)

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        brief = ctx.briefs[0]
        self.decided.append(state.customer_id)
        return Decision(
            decision_id=stable_id("dec", state.customer_id, ctx.now.isoformat()),
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.ACT,
            action=Action(
                goal_id=brief.goal_id,
                channel=Channel.EMAIL,
                offer_id="pct10",
                content_id="ci_1",
                send_at=ctx.now,
            ),
            group=state.group,
            probability=1.0,
            reason="Test: 10% off for everyone.",
            policy_version=self.version,
        )

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        return None


@dataclass
class Shop:
    db: Any
    clock: ManualClock
    klaviyo: TeardownKlaviyo
    shopify: TeardownShopify
    wiring: ConnectorWiring
    runtime: AutonomyRuntime
    ses: FakeChannelAdapter
    policy: OfferPolicy
    alerts: SqlAlertStore

    @property
    def engine(self) -> Any:
        return self.wiring.engine

    def at(self, when: datetime) -> datetime:
        self.clock.current = when
        return when

    def connect_klaviyo(self, **config: str) -> Connection:
        """A Klaviyo private key, with the send setup finished (metric, template, live flows)."""
        from autonomy.channels.klaviyo import ensure_klaviyo_setup

        connection = self.engine.connect_api_key(
            "klaviyo", business_id=BUSINESS, api_key=API_KEY, now=self.clock.now(), config=config or None
        )
        status = ensure_klaviyo_setup(self.engine, connection.connection_id, self.clock.now())
        assert status is not None and status.state == "ready", status
        return connection

    def connect_shopify_oauth(self, **config: str) -> Connection:
        from urllib.parse import parse_qs

        from tests.v2.connectors.shopify_support import CALLBACK, sign_query

        now = self.clock.now()
        url = self.engine.begin_oauth(
            "shopify", business_id=BUSINESS, redirect_uri=CALLBACK, now=now, params={"shop": SHOP, **config}
        )
        state = parse_qs(url.split("?", 1)[1])["state"][0]
        query = sign_query(
            {"code": "code_ok", "shop": SHOP, "state": state, "timestamp": str(int(now.timestamp()))}
        )
        return self.engine.finish_oauth("shopify", query, now, None)

    def fresh(self, connection: Connection, at: datetime | None = None) -> None:
        """Mark every stream of the connection synced at `at` with its history loaded."""
        when = at or self.clock.now()
        connector = self.engine.registry.get(connection.kind)
        for spec in connector.capabilities.streams:
            for lane in (Lane.LIVE, Lane.HISTORY):
                position = SyncPosition(
                    connection_id=connection.connection_id,
                    stream=spec.stream,
                    lane=lane,
                    high_watermark=when if lane is Lane.LIVE else None,
                    last_success_at=when,
                    last_attempt_at=when,
                    done=lane is Lane.HISTORY,
                    reached=(
                        connection.anchor - timedelta(days=spec.backfill_days)
                        if lane is Lane.HISTORY
                        else None
                    ),
                )
                self.engine.store.put_position(position, when)

    def add_customers(self, ids: Sequence[str]) -> None:
        """Consented to email long ago, last bought 90 days ago: the sample win-back audience."""
        out: list[Event] = []
        for cid in ids:
            out.append(
                Event(
                    event_id=f"consent_{cid}",
                    customer_id=cid,
                    type=EventType.CONSENT_GRANTED,
                    ts=T0 - timedelta(days=200),
                    source="shopify",
                    channel=Channel.EMAIL,
                )
            )
            out.append(
                Event(
                    event_id=f"buy_{cid}",
                    customer_id=cid,
                    type=EventType.PURCHASE,
                    ts=T0 - timedelta(days=90),
                    source="shopify",
                    value=40.0,
                )
            )
        self.runtime.events.append(out)


def make_shop(
    *,
    linking: bool = False,
    hold: bool = True,
    autonomy: AutonomyLevel = AutonomyLevel.AUTOPILOT,
    klaviyo: TeardownKlaviyo | None = None,
    shopify: TeardownShopify | None = None,
) -> Shop:
    db = memory_engine()
    create_all(db)
    create_record_tables(db)
    clock = ManualClock(T0)
    fake_k = klaviyo or TeardownKlaviyo()
    fake_k.clock = clock
    fake_s = shopify or TeardownShopify(clock_now=T0)
    k_transport, s_transport = fake_k.transport(), fake_s.transport()
    settings = RuntimeSettings(
        database_url="sqlite://",
        business_id=BUSINESS,
        connectors=True,
        identity_linking=linking,
        hold_on_stale_data=hold,
        public_url="https://autonomy.example",
    )
    alerts = SqlAlertStore(db)
    wiring = build_connector_engine(
        settings,
        db=db,
        events=SqlEventStore(db),
        alerts=alerts,
        environ=ENV,
        clock=clock,
        cipher=TestCipher(),
        transport_for=lambda base: k_transport if "klaviyo" in base else s_transport,
    )
    ses = FakeChannelAdapter(Channel.EMAIL)
    policy = OfferPolicy()
    runtime = build_runtime(
        settings,
        engine=db,
        connectors=wiring,
        policy_factory=lambda briefs, content: policy,
        adapters={Channel.EMAIL: ses},
        run_send_jobs=None,
    )
    runtime.workspace.put_goal(
        sample_goal(START).model_copy(update={"autonomy": autonomy, "status": GoalStatus.ACTIVE})
    )
    runtime.workspace.put_brief(sample_brief(START))
    runtime.workspace.put_content(sample_content(START))
    return Shop(db, clock, fake_k, fake_s, wiring, runtime, ses, policy, alerts)
