"""End-to-end rehearsal of zero-touch setup: a simulated Shopify + Klaviyo shop, from Connect to Autopilot.

    python -m scripts.v2_zero_touch_rehearsal --json /tmp/rehearsal.json   # 5,000 customers, ~30 minutes
    python -m scripts.v2_zero_touch_rehearsal --customers 3000 --autopilot-anyway   # see --help

No network and no clock reads in the logic: everything runs on a manual clock.

**The shop.** A Gym world (`autonomy.gym`, the project's validated simulator; scenario
`baseline_shop`) is the shop's customers: they sign up, consent, buy, read the shop's own weekly
campaign, and answer our messages by their planted (hidden) response. The Gym's events are exposed
only through the platforms: customers, consent and orders become records of a fake Shopify, and
profiles, consent, the shop's own campaign deliveries and clicks become records of a fake Klaviyo
(`tests/v2/connectors`: the documented API shapes behind `httpx.MockTransport`). Autonomy never sees
a Gym event. It reads both platforms through the real connector engine, exactly as in production.

**Autonomy.** The production pieces, as two processes over one SQLite database: the API process
(`api.v2.app.app_for_runtime`, the same app `create_production_app` builds) takes the clicks, and the
worker (`autonomy.runtime.service.worker_for_runtime`, what `build_worker` runs) syncs, ticks, sends
and keeps the inbox. Sending is Klaviyo-first: a decision is handed to the shop's Klaviyo (the
"Autonomy Decision" event that Autonomy's flow sends), the rehearsal plays Klaviyo's flow by handing
the message to the Gym customer, and the customer's reactions come back as Klaviyo engagement
(Received Email / Clicked Email with Autonomy's `$flow`) and Shopify orders.

**The script of the run.**

1. Connect Shopify (OAuth) and Klaviyo (OAuth): the two connect clicks. The platforms' own consent
   screens are simulated and not counted.
2. Backfill: the worker runs the connector jobs on the manual clock until both histories are in and
   the proposals carry money. "Simulated time to proposals" is that clock time: it counts the
   connector's own scheduling (bulk-operation polls, paging, pacing), not real network latency.
3. Proposals -> "Use this goal" (the preselected one) -> limits -> "Sign and start Shadow"
   (the margin is typed, not clicked) -> the brand kit is read without a click -> "Approve this voice".
4. Shadow, one simulated hour per step, until the Autopilot offer appears (or `--max-shadow-days`).
   Once a week the owner reads the inbox (the weekly read).
5. "Turn on Autopilot", then `--autopilot-days` days on Autopilot with Klaviyo sending, during which:
   one hand-off is silently dropped by Klaviyo (no delivery ever comes), Shopify answers 503 for
   `--outage-hours` (a stale-data hold), and the Shopify app is uninstalled and re-installed (one
   reconnect click, a few hours later).

The result (printed, and written with `--json`) is the ZERO_TOUCH.md success table: clicks, time to
proposals, inbox items per week, decisions a person had to make inside the mandate, and a trace check
that every proposed number, limit and brand rule names its source in the shop's data or a cited prior.
"""

from __future__ import annotations

import argparse
import importlib
import json
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from autonomy.connectors import ManualClock
from autonomy.connectors.base import Lane
from autonomy.connectors.klaviyo.api import DECISION_METRIC
from autonomy.connectors.klaviyo.setup import EMAIL_FLOW
from autonomy.contracts import (
    BatchStatus,
    Channel,
    ContentStatus,
    Decision,
    Event,
    EventType,
)
from autonomy.gym.scenarios import GYM_OFFERS, get_scenario
from autonomy.gym.score import policy_view
from autonomy.gym.world import GymWorld
from autonomy.runtime.service import worker_for_runtime
from autonomy.runtime.settings import RuntimeSettings, build_connector_engine, build_runtime, engine_for
from autonomy.runtime.stores import SqlAlertStore, create_record_tables
from autonomy.runtime.worker import tick_job
from autonomy.store import SqlEventStore, create_all

# The platforms are the connector tests' fakes (the documented API shapes). They are test code, which
# mypy does not hold to the strict bar, so they are loaded by name rather than imported.
_base = importlib.import_module("tests.v2.connectors.base_support")
_klaviyo = importlib.import_module("tests.v2.connectors.klaviyo_support")
_shopify = importlib.import_module("tests.v2.connectors.shopify_support")
_brand = importlib.import_module("tests.v2.zero_touch.brand_support")
TestCipher: Any = _base.TestCipher
subscriptions: Any = _klaviyo.subscriptions
money: Any = _shopify.money
sign_query: Any = _shopify.sign_query
SHOP: str = _shopify.SHOP
CLIENT_ID: str = _shopify.CLIENT_ID
CLIENT_SECRET: str = _shopify.CLIENT_SECRET
BrandKlaviyo: Any = _brand.BrandKlaviyo
BrandShopify: Any = _brand.BrandShopify
email_html: Any = _brand.email_html

CONNECT_AT = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
"""Monday 08:00 UTC: the owner opens the app and connects (the brand fakes date their copy from it)."""
BUSINESS = "rehearsal_shop"
PUBLIC_URL = "https://autonomy.example"
ENV = {
    "AUTONOMY_KLAVIYO_CLIENT_ID": "kl_client",
    "AUTONOMY_KLAVIYO_CLIENT_SECRET": "kl_secret",
    "AUTONOMY_SHOPIFY_CLIENT_ID": CLIENT_ID,
    "AUTONOMY_SHOPIFY_CLIENT_SECRET": CLIENT_SECRET,
    "AUTONOMY_MANDATE_SECRET": "rehearsal-mandate-secret",
}
EMAIL_DOMAIN = "customers.example.com"
TICK_MINUTES = 60
"""The worker's tick here (production's default is 5 minutes; hourly keeps the run to minutes)."""
PRODUCTION_TICK_MINUTES = 5
BACKFILL_STEP = timedelta(minutes=1)
PRODUCT_TYPES = {
    "apparel": "Apparel",
    "beauty": "Skincare",
    "home": "Home",
    "electronics": "Electronics",
    "outdoor": "Outdoor",
}
SHOP_ZONES = {
    "America/New_York": 0.35,
    "America/Chicago": 0.1,
    "America/Los_Angeles": 0.15,
    "Europe/London": 0.25,
    "Australia/Sydney": 0.15,
}
"""Where the customers live (the first markets: US, UK, Australia); `--utc` puts everyone in UTC."""
SETUP_CLICKS = (
    "Continue to Shopify",
    "Connect Klaviyo",
    "Use this goal",
    "Sign and start Shadow",
    "Approve this voice",
)


# --- the platforms ------------------------------------------------------------------------------------


class RehearsalShopify(BrandShopify):  # type: ignore[misc]  # a test fake, loaded by name
    """The brand fake plus an outage window (503s) on the manual clock."""

    clock: ManualClock | None = None
    down_from: datetime | None = None
    down_until: datetime | None = None
    outage_calls: int = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.clock is not None:
            self.clock_now = self.clock.now()
            now = self.clock.now()
            if (
                self.down_from is not None
                and self.down_until is not None
                and self.down_from <= now < self.down_until
            ):
                self.outage_calls += 1
                self.calls.append(request)
                return httpx.Response(503, json={"errors": "Service unavailable"})
        answer: httpx.Response = super().handle(request)
        return answer


@dataclass
class ShopFront:
    """Puts Gym events into the fake Shopify and Klaviyo, the only way Autonomy learns about them."""

    shopify: RehearsalShopify
    klaviyo: BrandKlaviyo
    gym_ids: Sequence[str]
    sid: dict[str, int] = field(default_factory=dict)
    gid: dict[str, str] = field(default_factory=dict)
    ours: set[str] = field(default_factory=set)
    """Decision ids handed to Klaviyo: their engagement carries Autonomy's `$flow`."""
    orders: int = 0
    counts: Counter[str] = field(default_factory=Counter)

    def __post_init__(self) -> None:
        for i, g in enumerate(self.gym_ids):
            self.sid[g] = 100_001 + i
            self.gid[str(100_001 + i)] = g
        for category, ptype in PRODUCT_TYPES.items():
            pid = self.product_id(category)
            self.shopify.add_product(
                pid, title=f"{ptype} favourite", ptype=ptype, price=30.0, updated=CONNECT_AT
            )
            self.shopify.add_image_product(pid, f"{ptype} favourite", ptype, alt=f"{ptype} favourite")

    @staticmethod
    def product_id(category: str | None) -> int:
        names = list(PRODUCT_TYPES)
        return 9_000 + (names.index(category) if category in names else 0)

    def flow_id(self) -> str | None:
        """Autonomy's email flow in the shop's Klaviyo (created by the connector's send setup)."""
        return next(
            (fid for fid, f in self.klaviyo.flows.items() if f["attributes"]["name"] == EMAIL_FLOW), None
        )

    def email(self, gym_id: str) -> str:
        return f"{gym_id.replace('_', '.')}@{EMAIL_DOMAIN}"

    def apply(self, events: Iterable[Event]) -> None:
        for e in sorted(events, key=lambda x: (x.ts, x.event_id)):
            self._one(e)

    def _one(self, e: Event) -> None:
        sid = self.sid.get(e.customer_id)
        if sid is None:
            return
        pid = f"P{sid}"
        email_channel = e.channel in (None, Channel.EMAIL)
        if e.type is EventType.SIGNUP:
            tz = str(e.attrs.get("timezone") or "UTC")
            self.shopify.add_customer(sid, created=e.ts, email_state=None)
            self.klaviyo.add_profile(
                pid,
                email=self.email(e.customer_id),
                created=e.ts,
                updated=e.ts,
                subs=subscriptions(email="NEVER_SUBSCRIBED", can_email=False, at=e.ts),
                location={"country": "US", "timezone": tz},
                external_id=str(sid),
            )
        elif e.type in (EventType.CONSENT_GRANTED, EventType.CONSENT_REVOKED) and e.channel is Channel.EMAIL:
            self._consent(sid, granted=e.type is EventType.CONSENT_GRANTED, at=e.ts)
        elif e.type is EventType.PURCHASE:
            self._order(sid, e)
        elif e.type is EventType.DELIVER and email_channel:
            self._klaviyo("Received Email", pid, e)
        elif e.type is EventType.CLICK and email_channel:
            self._klaviyo("Clicked Email", pid, e)
        elif e.type is EventType.UNSUBSCRIBE and email_channel:
            self._klaviyo("Unsubscribed from Email Marketing", pid, e)
            self._consent(sid, granted=False, at=e.ts)
        elif e.type is EventType.COMPLAINT and email_channel:
            self._klaviyo("Marked Email as Spam", pid, e)
        elif e.type is EventType.VISIT:
            self._klaviyo("Active on Site", pid, e)
        # SEND (the Gym's own copy) and FAIL (Klaviyo suppressed it: nothing is recorded, and the
        # delivery watch reports a hand-off with no delivery) leave no record.

    def _klaviyo(self, metric: str, pid: str, e: Event) -> None:
        props: dict[str, Any] = {"$message": e.decision_id or "campaign"}
        flow = self.flow_id()
        if e.decision_id is not None and e.decision_id in self.ours and flow is not None:
            props["$flow"] = flow  # Klaviyo stamps the messages of Autonomy's flow with its id
        self.klaviyo.add_event(metric, pid=pid, at=e.ts, props=props)
        self.counts[metric] += 1

    def _consent(self, sid: int, *, granted: bool, at: datetime) -> None:
        state = "SUBSCRIBED" if granted else "UNSUBSCRIBED"
        node = self.shopify.customers.get(str(sid))
        if node is not None:
            node["defaultEmailAddress"] = {
                "marketingState": state,
                "marketingOptInLevel": "SINGLE_OPT_IN",
                "marketingUpdatedAt": at.isoformat(),
            }
            node["updatedAt"] = at.isoformat()
        profile = self.klaviyo.profiles.get(f"P{sid}")
        if profile is not None:
            attrs = profile["attributes"]
            attrs["subscriptions"] = subscriptions(email=state, can_email=granted, at=at)
            attrs["updated"] = at.isoformat()
        self.counts["consent " + state.lower()] += 1

    def _order(self, sid: int, e: Event) -> None:
        self.orders += 1
        oid = 700_000 + self.orders
        value = float(e.value or 0.0)
        discount = float(e.attrs.get("offer_cost") or 0.0)
        codes = (str(e.attrs["offer_id"]).upper(),) if e.attrs.get("offer_id") else ()
        ptype = PRODUCT_TYPES.get(str(e.category), "Apparel")
        self.shopify.add_order(
            oid,
            customer=sid,
            total=value,
            created=e.ts,
            lines=[(self.product_id(e.category), ptype, 1, value)],
            codes=codes,
            totalDiscountsSet=money(discount),
        )
        node = self.shopify.customers.get(str(sid))
        if node is not None:
            node["updatedAt"] = max(str(node["updatedAt"]), e.ts.isoformat())
        self.counts["order"] += 1


def seed_klaviyo_copy(klaviyo: BrandKlaviyo) -> None:
    """The shop's own email templates and sent campaigns: what the brand kit reads its voice from."""
    for i in range(4):
        klaviyo.add_template(f"TPL_SHOP_{i}", f"Acme newsletter {i}", email_html(i))
    for i in range(12):
        offer = "Take 10% off with code TEN10." if i % 4 == 0 else ""
        klaviyo.add_campaign(
            f"C{i}",
            subject=f"Trail ready: new gear for the weekend {i}",
            html=email_html(i, discount=offer),
            days_ago=7 * (i + 1),
        )


# --- the run ------------------------------------------------------------------------------------------


@dataclass
class Rehearsal:
    customers: int = 5000
    seed: int = 0
    max_shadow_days: int = 49
    autopilot_days: int = 10
    outage_hours: int = 9
    utc: bool = False
    workdir: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="zt_rehearsal_")))
    log: list[str] = field(default_factory=list)
    clicks: list[tuple[datetime, str]] = field(default_factory=list)
    other_clicks: list[tuple[datetime, str]] = field(default_factory=list)
    weekly_reads: list[dict[str, Any]] = field(default_factory=list)
    handoffs: int = 0
    dropped: list[str] = field(default_factory=list)
    ticks: int = 0
    held_ticks: list[tuple[datetime, tuple[str, ...]]] = field(default_factory=list)
    _handoff_seen: int = 0

    # -- building ---------------------------------------------------------------------------------

    def build(self) -> None:
        self.clock = ManualClock(CONNECT_AT)
        self.spec = get_scenario(
            "baseline_shop",
            n_customers=self.customers,
            days=self.max_shadow_days + self.autopilot_days + 14,
            seed=self.seed,
            **({} if self.utc else {"timezones": SHOP_ZONES}),
        )
        self.world = GymWorld(self.spec, start=CONNECT_AT, offers=GYM_OFFERS)
        history = policy_view(self.world.reset(self.seed))
        self.klaviyo = BrandKlaviyo()
        self.klaviyo.clock = self.clock
        self.shopify = RehearsalShopify(clock_now=CONNECT_AT)
        self.shopify.clock = self.clock
        seed_klaviyo_copy(self.klaviyo)
        self.front = ShopFront(self.shopify, self.klaviyo, self.world.customer_ids)
        self.front.apply(history)
        self.history_orders = self.front.orders
        self.say(
            f"Shop: {self.customers:,} customers, {self.front.orders:,} orders over "
            f"{self.spec.history_days} days, {self.front.counts['Received Email']:,} campaign emails "
            "received, in fake Shopify and Klaviyo."
        )
        k_transport, s_transport = self.klaviyo.transport(), self.shopify.transport()

        def transport_for(base: str) -> Any:
            return k_transport if "klaviyo" in base else s_transport

        self.db = engine_for(f"sqlite:///{self.workdir / 'autonomy.db'}")
        create_all(self.db)
        create_record_tables(self.db)
        self.settings = RuntimeSettings(
            database_url="sqlite://",
            business_id=BUSINESS,
            connectors=True,
            public_url=PUBLIC_URL,
            tick_every_minutes=TICK_MINUTES,
            # The rehearsed shop sells in dollars; the shop's money is always set (docs/v3/CURRENCY.md).
            currency="USD",
            channel_costs={Channel.EMAIL: 0.001, Channel.WHATSAPP: 0.03},
            typical_order_value=50.0,
        )

        def wiring(holder: str) -> Any:
            return build_connector_engine(
                self.settings,
                db=self.db,
                events=SqlEventStore(self.db),
                alerts=SqlAlertStore(self.db),
                environ=ENV,
                clock=self.clock,
                cipher=TestCipher(),
                transport_for=transport_for,
                holder=holder,
            )

        # The worker process.
        self.worker_wiring = wiring("worker-1")
        self.runtime = build_runtime(
            self.settings, engine=self.db, run_send_jobs=False, connectors=self.worker_wiring
        )
        inner_tick = self.runtime.tick

        def observed_tick(now: datetime) -> Any:
            report = inner_tick(now)
            self.ticks += 1
            held = getattr(report, "data_held", None)
            if held:
                self.held_ticks.append((now, tuple(held) if not isinstance(held, str) else (held,)))
            return report

        self.runtime.tick = observed_tick  # type: ignore[method-assign]  # the rehearsal watches each tick
        self.worker = worker_for_runtime(self.runtime, name="worker-1", wiring=self.worker_wiring, db=self.db)
        self.worker.queue.enqueue(tick_job(CONNECT_AT, every_minutes=TICK_MINUTES))
        # The API process.
        from fastapi.testclient import TestClient

        from api.v2.app import app_for_runtime

        api_wiring = wiring("api")
        api_runtime = build_runtime(self.settings, engine=self.db, run_send_jobs=False, connectors=api_wiring)
        app = app_for_runtime(
            api_runtime,
            wiring=api_wiring,
            db=self.db,
            clock=self.clock.now,
            public_url=PUBLIC_URL,
            environ=ENV,
        )
        self.api = TestClient(app, base_url=PUBLIC_URL)

    # -- helpers ----------------------------------------------------------------------------------

    def say(self, line: str) -> None:
        stamp = self.clock.now().strftime("%a %d %b %H:%M") if hasattr(self, "clock") else ""
        self.log.append(f"[{stamp}] {line}")
        print(f"[{stamp}] {line}", flush=True)

    def click(self, label: str, *, setup: bool = True) -> None:
        (self.clicks if setup else self.other_clicks).append((self.clock.now(), label))

    def get(self, path: str, **params: Any) -> Any:
        r = self.api.get(path, params=params or None)
        if r.status_code >= 400:
            raise RuntimeError(f"GET {path}: {r.status_code} {r.text[:400]}")
        return r.json()

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        r = self.api.post(path, json=body, params=params or None)
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path}: {r.status_code} {r.text[:400]}")
        return r.json()

    def step(self, until: datetime, *, drop_next: bool = False) -> None:
        """Move the world, the platforms and the worker to `until`, then play Klaviyo's flow."""
        self.front.apply(policy_view(self.world.advance(until)))
        self.clock.current = until
        self.worker.drain(until)
        self._play_flow(drop_next)

    def backfill_step(self, until: datetime) -> None:
        """A backfill minute. The runtime folds new records into customer state on its tick; this run
        ticks hourly to keep the simulated weeks fast, so during the backfill it also ticks on
        production's 5-minute grid, or the proposals would wait up to an hour for consent to fold."""
        self.step(until)
        if until.minute % PRODUCTION_TICK_MINUTES == 0 and until.minute != 0:
            self.runtime.tick(until)

    def _play_flow(self, drop_next: bool) -> None:
        """Klaviyo's flow: each new "Autonomy Decision" event is a message to that customer."""
        created = self.klaviyo.created_events
        new, self._handoff_seen = created[self._handoff_seen :], len(created)
        batch: list[Decision] = []
        for attributes in new:
            name = attributes["metric"]["data"]["attributes"]["name"]
            if name != DECISION_METRIC:
                continue
            props = attributes.get("properties") or {}
            decision = self.runtime.decisions.get(str(props.get("decision_id")))
            if decision is None or decision.action is None:
                continue
            self.handoffs += 1
            if drop_next and not self.dropped:
                self.dropped.append(decision.decision_id)  # Klaviyo accepted it and never sent it
                continue
            gym_id = self.front.gid.get(decision.customer_id)
            if gym_id is None:
                continue
            self.front.ours.add(decision.decision_id)
            action = decision.action.model_copy(
                update={"send_at": max(decision.action.send_at, self.clock.now())}
            )
            batch.append(decision.model_copy(update={"customer_id": gym_id, "action": action}))
        if batch:
            self.world.act(batch)

    def run_until(self, end: datetime, *, every: timedelta = timedelta(hours=1), on_step: Any = None) -> None:
        t = self.clock.now()
        while t < end:
            t = min(t + every, end)
            self.step(t, drop_next=bool(on_step and on_step(t)))

    def histories_done(self) -> bool:
        store = self.worker_wiring.engine.store
        connections = store.connections(business_id=BUSINESS)
        if len(connections) < 2:
            return False
        registry = self.worker_wiring.engine.registry
        for c in connections:
            done = {p.stream for p in store.positions(c.connection_id) if p.lane is Lane.HISTORY and p.done}
            wanted = {s.stream for s in registry.get(c.kind).capabilities.streams if s.backfill_days > 0}
            if not wanted <= done:
                return False
        return True

    # -- the script -------------------------------------------------------------------------------

    def connect(self) -> None:
        start = self.post("/v2/connectors/shopify/connect", {"config": {"shop": SHOP}})
        self.click("Continue to Shopify")
        state = parse_qs(urlsplit(start["url"]).query)["state"][0]
        now = self.clock.now()
        query = sign_query(
            {"code": "code_ok", "shop": SHOP, "state": state, "timestamp": str(int(now.timestamp()))}
        )
        r = self.api.get("/v2/connectors/shopify/callback", params=query, follow_redirects=False)
        assert r.status_code == 303 and "connected=shopify" in r.headers["location"], r.headers.get(
            "location"
        )
        start = self.post("/v2/connectors/klaviyo/connect", {})
        self.click("Connect Klaviyo")
        q = parse_qs(urlsplit(start["url"]).query)
        self.klaviyo.expected_challenge = q["code_challenge"][0]
        r = self.api.get(
            "/v2/connectors/klaviyo/callback",
            params={"state": q["state"][0], "code": "code_ok"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "connected=klaviyo" in r.headers["location"], r.headers.get(
            "location"
        )
        self.say("Connected Shopify and Klaviyo (2 clicks; the platforms' own consent screens not counted).")

    def backfill(self) -> dict[str, Any]:
        began = self.clock.now()
        calls_before = len(self.shopify.calls) + len(self.klaviyo.calls)
        t = began
        first: datetime | None = None
        first_view: dict[str, Any] = {}
        while not self.histories_done():
            t += BACKFILL_STEP
            self.backfill_step(t)
            if first is None and t.minute % 5 == 0:  # the Getting started screen asks again as history loads
                view = self.get("/v2/onboarding/proposals")
                if view.get("preselected_id") is not None:
                    first, first_view = t, view
            if t - began > timedelta(hours=6):
                raise RuntimeError("the backfill did not finish in 6 simulated hours")
        histories = self.clock.now() - began
        scan = self.get("/v2/onboarding/proposals")
        while scan.get("preselected_id") is None and self.clock.now() - began < timedelta(hours=2):
            self.backfill_step(self.clock.now() + BACKFILL_STEP)
            scan = self.get("/v2/onboarding/proposals")
        took = self.clock.now() - began
        if first is None:
            first, first_view = self.clock.now(), scan

        def money(view: dict[str, Any]) -> dict[str, float]:
            return {
                p["opportunity_id"]: p["monthly_profit"]["point"]
                for p in view["proposals"]
                if p["monthly_profit"]
            }

        moved = [abs(money(first_view).get(k, 0.0) - v) for k, v in money(scan).items()]
        setup = self.get(f"/v2/connectors/connections/{self._klaviyo_id()}/setup")
        out = {
            "histories_minutes": round(histories.total_seconds() / 60, 1),
            "to_proposals_minutes": round(took.total_seconds() / 60, 1),
            "first_proposals_minutes": round((first - began).total_seconds() / 60, 1),
            "first_pick_same": first_view.get("preselected_id") == scan.get("preselected_id"),
            "first_money_moved_at_most": round(max(moved, default=0.0), 2),
            "api_calls": len(self.shopify.calls) + len(self.klaviyo.calls) - calls_before,
            "klaviyo_setup": setup.get("state"),
            "events_stored": sum(1 for _ in self.runtime.events.scan()),
        }
        self.say(
            f"Money-sized proposals first shown after {out['first_proposals_minutes']} simulated minutes; "
            f"backfill done in {out['histories_minutes']} minutes; proposals at the end of it after "
            f"{out['to_proposals_minutes']} minutes ({out['api_calls']:,} platform API calls; Klaviyo sending "
            f"setup: {out['klaviyo_setup']})."
        )
        self.scan = scan
        return out

    def _klaviyo_id(self) -> str:
        store = self.worker_wiring.engine.store
        return str(
            next(c.connection_id for c in store.connections(business_id=BUSINESS) if c.kind == "klaviyo")
        )

    def accept(self) -> Any:
        scan = self.scan
        chosen = next(p for p in scan["proposals"] if p["opportunity_id"] == scan["preselected_id"])
        self.click("Use this goal")
        limits = self.get("/v2/onboarding/limits", opportunity_id=chosen["opportunity_id"])
        margin = next((q for q in limits.get("questions", []) if "margin" in str(q).lower()), None)
        body: dict[str, Any] = {"opportunity_id": chosen["opportunity_id"]}
        if margin is not None or not scan["margin"]["from_data"]:
            body["margin_pct"] = round(self.spec.margin_rate * 100, 1)  # typed by the owner, not clicked
        result = self.post("/v2/onboarding/accept", body)
        self.click("Sign and start Shadow")
        self.goal_id = result["goal"]["goal_id"]
        self.limits = result["limits"]
        self.say(f"Accepted '{chosen['title']}': {result['words']}")
        return result

    def approve_voice(self) -> Any:
        page = self.post("/v2/onboarding/brand/read", goal_id=self.goal_id)  # the screen reads it itself
        approved = self.post("/v2/onboarding/brand/approve", {"fingerprint": page["fingerprint"]})
        self.click("Approve this voice")
        content = self.runtime.workspace.content(goal_id=self.goal_id)
        # The Gym knows a message's theme by its content id (its planted response to themes).
        self.world.planted.themes.update({c.content_id: c.theme for c in content})
        self.say(
            f"Voice approved: {page['messages_read']} of the shop's messages read, "
            f"{sum(1 for f in page['found'] if f['hard'])} hard rules, {len(page['samples'])} samples; "
            f"the goal got {len(content)} approved messages."
        )
        self.brand = page
        self.approved_page = approved
        return page

    def weekly_read(self, label: str) -> dict[str, Any]:
        view = self.get("/v2/inbox", refresh="true")
        read = {
            "at": self.clock.now().isoformat(),
            "label": label,
            "open": view["count"],
            "this_week": view["this_week"],
            "items": [
                {"kind": i["kind"], "sentence": i["sentence"], "button": i["button"]["label"]}
                for i in view["items"]
            ],
            "quiet": view["quiet"].get("body") if isinstance(view.get("quiet"), dict) else None,
        }
        self.weekly_reads.append(read)
        self.say(
            f"Weekly read ({label}): {view['words']} {read['open']} open, {read['this_week']} this week."
        )
        return read

    def readiness(self) -> dict[str, Any] | None:
        r = self.api.get(f"/v2/autopilot/{self.goal_id}")
        return r.json() if r.status_code == 200 else None

    def shadow(self) -> dict[str, Any] | None:
        started = self.clock.now()
        next_read = started + timedelta(days=7)
        ready: dict[str, Any] | None = None
        while self.clock.now() - started < timedelta(days=self.max_shadow_days):
            day_end = self.clock.now() + timedelta(days=1)
            self.run_until(day_end)
            if self.clock.now() >= next_read:
                self.weekly_read(f"Shadow week {round((self.clock.now() - started).days / 7)}")
                next_read += timedelta(days=7)
            ready = self.readiness()
            if ready is not None and ready.get("ready"):
                break
        days = (self.clock.now() - started).total_seconds() / 86400
        if ready is not None:
            self.say(f"After {days:.0f} Shadow days: ready={ready.get('ready')}. {ready.get('sentence', '')}")
        return ready

    def autopilot(self, *, earned: bool = True) -> dict[str, Any]:
        if earned:
            change = self.post(f"/v2/autopilot/{self.goal_id}/turn-on")
            self.click("Turn on Autopilot", setup=False)
            self.say(f"Autopilot on: {change.get('words', '')}")
        else:  # --autopilot-anyway: the plan page's level switch, logged as a person's change
            self.post(f"/v2/goals/{self.goal_id}/approve", {"autonomy": "autopilot"})
            self.click("Autopilot (plan page, not earned)", setup=False)
            self.say("Autopilot switched on from the plan page WITHOUT the offer (--autopilot-anyway).")
        started = self.clock.now()
        outage_at = started + timedelta(days=2, hours=2)
        uninstall_at = started + timedelta(days=5, hours=3)
        reconnect_at = uninstall_at + timedelta(hours=5)
        self.shopify.down_from, self.shopify.down_until = outage_at, outage_at + timedelta(
            hours=self.outage_hours
        )
        held: list[dict[str, Any]] = []
        reconnected = False
        dropping = {"armed": True}

        def on_step(t: datetime) -> bool:
            return dropping["armed"] and t - started > timedelta(hours=26)

        end = started + timedelta(days=self.autopilot_days)
        next_read = started + timedelta(days=7)
        while self.clock.now() < end:
            t = self.clock.now() + timedelta(hours=1)
            if t >= uninstall_at and not self.shopify.revoked and not reconnected:
                self.shopify.revoked = True
                self.say("Shopify app uninstalled by the shop (tokens stop working).")
            if t >= reconnect_at and self.shopify.revoked:
                self.shopify.revoked = False
                self.reconnect_shopify()
                reconnected = True
            self.step(t, drop_next=on_step(t))
            if self.dropped:
                dropping["armed"] = False
            report = self.inbox_now()
            if report["open"]:
                held.append(report)
            if t >= next_read:
                self.weekly_read("Autopilot week 1")
                next_read += timedelta(days=7)
        return {"incidents_seen": held}

    def reconnect_shopify(self) -> None:
        start = self.post("/v2/connectors/shopify/connect", {"config": {"shop": SHOP}})
        self.click("Reconnect Shopify", setup=False)
        state = parse_qs(urlsplit(start["url"]).query)["state"][0]
        now = self.clock.now()
        query = sign_query(
            {"code": "code_ok", "shop": SHOP, "state": state, "timestamp": str(int(now.timestamp()))}
        )
        r = self.api.get("/v2/connectors/shopify/callback", params=query, follow_redirects=False)
        self.say(f"Owner reconnected Shopify (1 click): {r.headers.get('location', '')[-60:]}")

    def inbox_now(self) -> dict[str, Any]:
        view = self.get("/v2/inbox")
        return {
            "at": self.clock.now().isoformat(),
            "open": view["count"],
            "items": [(i["kind"], i["sentence"]) for i in view["items"]],
        }

    # -- the record -------------------------------------------------------------------------------

    def trace(self) -> dict[str, Any]:
        """Every number on the setup screens names where it came from."""
        problems: list[str] = []
        sized = 0
        for p in self.scan["proposals"]:
            if p["monthly_profit"] is not None:
                sized += 1
                m = p["monthly_profit"]
                if not m["low"] <= m["point"] <= m["high"]:
                    problems.append(f"{p['opportunity_id']}: money without a proper range")
                if not p["basis"] or any(not b["source"] for b in p["basis"]):
                    problems.append(f"{p['opportunity_id']}: a number without a source")
            if "holdout" in json.dumps(p).lower():
                problems.append(f"{p['opportunity_id']}: says 'holdout'")
        assumed = [b["label"] for p in self.scan["proposals"] for b in p["basis"] if b["assumed"]]
        for lim in self.limits["limits"]:
            if not lim["source"]:
                problems.append(f"limit {lim['key']}: no source")
        defaults = [lim["key"] for lim in self.limits["limits"] if not lim["from_data"]]
        for f in self.brand["found"]:
            if not f["source"] or f["of"] < 0 or f["seen"] > f["of"]:
                problems.append(f"brand rule {f['key']}: evidence does not add up")
        return {
            "proposals_sized": sized,
            "proposals": len(self.scan["proposals"]),
            "assumed_inputs": sorted(set(assumed)),
            "limits": len(self.limits["limits"]),
            "limits_from_defaults": defaults,
            "brand_rules": len(self.brand["found"]),
            "problems": problems,
        }

    def record(
        self, backfill: dict[str, Any], ready: dict[str, Any] | None, auto: dict[str, Any]
    ) -> dict[str, Any]:
        history = self.get("/v2/inbox/history", weeks=8)
        weeks = [w["items"] for w in history["weeks"]]
        pending = (
            self.runtime.batches.list_batches(statuses=(BatchStatus.PENDING,)) if self.runtime.batches else []
        )
        alerts = Counter(a.code for a in self.runtime.alert_store.list_alerts())
        decisions = list(self.runtime.decisions.scan())
        acts = [d for d in decisions if d.action is not None and d.action.goal_id == self.goal_id]
        events = list(self.runtime.events.scan(since=CONNECT_AT))
        kinds = Counter(e.type.value for e in events)
        not_delivered = [
            e for e in events if e.type is EventType.FAIL and e.attrs.get("reason") == "not_delivered_by_tool"
        ]
        proof = self.get("/v2/proof", goal_id=self.goal_id)
        ledger = self.get("/v2/autopilot/ledger")
        approved = [
            c
            for c in self.runtime.workspace.content(goal_id=self.goal_id)
            if c.status is ContentStatus.APPROVED
        ]
        return {
            "shop": {
                "customers": self.customers,
                "history_days": self.spec.history_days,
                "orders_in_history": self.history_orders,
                "orders_since_connect": self.front.orders - self.history_orders,
            },
            "clicks_to_shadow": [label for _, label in self.clicks],
            "clicks_after": [f"{t.date()} {label}" for t, label in self.other_clicks],
            "backfill": backfill,
            "proposals": [
                {
                    "id": p["opportunity_id"],
                    "title": p["title"],
                    "audience_now": p["audience_now"],
                    "money": p["monthly_profit"],
                    "preselected": p["preselected"],
                    "not_enough": p["not_enough"],
                }
                for p in self.scan["proposals"]
            ],
            "scan_notes": self.scan["notes"],
            "limits": [
                {"key": x["key"], "found": x["found"], "use": x["use"], "from_data": x["from_data"]}
                for x in self.limits["limits"]
            ],
            "brand": {
                "messages_read": self.brand["messages_read"],
                "hard_rules": [f["label"] for f in self.brand["found"] if f["hard"]],
                "samples": len(self.brand["samples"]),
                "goal_messages_approved": len(approved),
            },
            "readiness": ready,
            "inbox_weeks": weeks,
            "inbox_causes": [w["causes"] for w in history["weeks"]],
            "weekly_reads": self.weekly_reads,
            "pending_batches": len(pending),
            "decisions": {
                "total": len(decisions),
                "goal_acts": len(acts),
                "handoffs_to_klaviyo": self.handoffs,
                "silently_dropped": self.dropped,
                "not_delivered_by_tool": len(not_delivered),
            },
            "events_since_connect": dict(kinds),
            "alerts": dict(alerts),
            "proof": proof,
            "ledger": [
                {"at": e["at"], "to": e["to_level"], "actor": e["actor"], "reason": e["reason"]}
                for e in ledger["entries"]
            ],
            "ledger_ok": ledger["chain_ok"],
            "incidents": auto["incidents_seen"],
            "ticks": self.ticks,
            "held_ticks": [{"at": t.isoformat(), "why": list(w)} for t, w in self.held_ticks],
            "trace": self.trace(),
            "outage_calls_refused": self.shopify.outage_calls,
            "log": self.log,
        }


def run(args: argparse.Namespace) -> dict[str, Any]:
    began = time.monotonic()
    r = Rehearsal(
        customers=args.customers,
        seed=args.seed,
        max_shadow_days=args.max_shadow_days,
        autopilot_days=args.autopilot_days,
        outage_hours=args.outage_hours,
        utc=args.utc,
    )
    r.build()
    r.connect()
    backfill = r.backfill()
    r.accept()
    r.approve_voice()
    r.weekly_read("setup day")
    ready = r.shadow()
    auto: dict[str, Any] = {"incidents_seen": []}
    if ready is not None and ready.get("ready"):
        auto = r.autopilot()
    elif args.autopilot_anyway:
        r.say("Autopilot was not offered within the Shadow window.")
        auto = r.autopilot(earned=False)
    else:
        r.say("Autopilot was not offered within the Shadow window; the run stops here.")
    out = r.record(backfill, ready, auto)
    out["wall_seconds"] = round(time.monotonic() - began, 1)
    return out


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.v2_zero_touch_rehearsal", description=__doc__)
    parser.add_argument("--customers", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-shadow-days", type=int, default=49)
    parser.add_argument("--autopilot-days", type=int, default=10)
    parser.add_argument("--outage-hours", type=int, default=9)
    parser.add_argument(
        "--autopilot-anyway",
        action="store_true",
        help="when the offer does not come, switch Autopilot on from the plan page to rehearse the incidents",
    )
    parser.add_argument("--utc", action="store_true", help="every customer in UTC (the Gym's own default)")
    parser.add_argument("--json", type=Path, help="write the full record here")
    args = parser.parse_args(argv)
    out = run(args)
    if args.json:
        args.json.write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in out.items() if k not in ("log", "proposals", "weekly_reads", "incidents")},
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
