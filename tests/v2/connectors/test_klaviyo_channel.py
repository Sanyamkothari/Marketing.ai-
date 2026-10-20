"""Decide here, send there: the Klaviyo channel adapter, delivery read-back and silent-drop detection."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from autonomy.channels import Dispatcher, DispatchStatus
from autonomy.channels.klaviyo import (
    ALERT_UNLINKED,
    NOT_DELIVERED,
    PROVIDER,
    WATCH_JOB_KIND,
    KlaviyoChannelAdapter,
    KlaviyoDeliveryWatch,
    details_from,
    klaviyo_handlers,
    provider_rates,
    seed_klaviyo_watch,
    watch_job,
)
from autonomy.channels.ratelimit import RateLimiter
from autonomy.connectors import ConnectionStatus, Lane, Stream
from autonomy.connectors.klaviyo import DECISION_METRIC, EMAIL_FLOW, KlaviyoConnector
from autonomy.contracts import (
    Action,
    AutonomyLevel,
    Channel,
    ChannelStats,
    CustomerState,
    Decision,
    DecisionKind,
    EventType,
    GoalStatus,
    Group,
    Message,
)
from autonomy.fakes import InMemoryDecisionLog, InMemoryWorkspace
from autonomy.fakes.samples import sample_brief, sample_content, sample_goal
from tests.v2.connectors.klaviyo_support import (
    T0,
    FakeKlaviyo,
    KlaviyoRig,
    connect_key,
    connect_ready,
    jsonapi_error,
    make_klaviyo_rig,
)

ANN = "ann@example.com"


class Shop:
    """A business with one active goal, its content, a Klaviyo connection and a real dispatcher."""

    def __init__(
        self,
        fake: FakeKlaviyo | None = None,
        *,
        ready: bool = True,
        customer: str = ANN,
        external_id: str | None = None,
    ) -> None:
        fake = fake or FakeKlaviyo()
        fake.add_profile("P1", email=ANN, updated=T0 - timedelta(days=100), external_id=external_id)
        self.customer = customer
        for name in ("Received Email", "Opened Email", "Clicked Email", "Dropped Email"):
            fake.add_metric(name)
        self.rig: KlaviyoRig = make_klaviyo_rig(fake)
        self.fake = fake
        self.connection = connect_ready(self.rig) if ready else connect_key(self.rig)
        self.ws = InMemoryWorkspace()
        self.ws.put_goal(
            sample_goal(T0).model_copy(
                update={"status": GoalStatus.ACTIVE, "autonomy": AutonomyLevel.AUTOPILOT}
            )
        )
        self.ws.put_brief(sample_brief(T0))
        self.ws.put_content(sample_content(T0))
        self.decisions = InMemoryDecisionLog()
        self.adapter = KlaviyoChannelAdapter(
            self.rig.engine,
            self.connection.connection_id,
            details=details_from(
                self.decisions, self.ws, discount_code=lambda d, offer: "BACK10" if offer else None
            ),
        )
        self.dispatcher = Dispatcher(
            {Channel.EMAIL: self.adapter},
            lambda cid, ch: cid if ch is Channel.EMAIL else None,  # an email, or the customer id itself
            self.ws,
            self.rig.events,
            profiles=lambda cid: {"first_name": "Ann"},
        )

    @property
    def now(self) -> datetime:
        return self.rig.clock.now()

    def decide(
        self, did: str, *, group: Group = Group.AGENT, content: str = "ci_1", offer: str = "pct10"
    ) -> Decision:
        decision = Decision(
            decision_id=did,
            customer_id=self.customer,
            made_at=self.now,
            kind=DecisionKind.ACT,
            action=Action(
                goal_id="goal_winback",
                channel=Channel.EMAIL,
                offer_id=offer,
                content_id=content,
                send_at=self.now,
            ),
            group=group,
            probability=0.8,
            reason="test",
            policy_version="t",
        )
        self.decisions.record([decision])
        return decision

    def send(self, decision: Decision) -> DispatchStatus:
        state = CustomerState(
            customer_id=self.customer,
            as_of=self.now,
            group=decision.group,
            top_categories=("trail shoes",),
            channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
        )
        report = self.dispatcher.dispatch([decision], {self.customer: state}, self.now)
        return report.results[0].status

    def decision_events(self) -> list[dict[str, object]]:
        return [
            e
            for e in self.fake.created_events
            if e["metric"]["data"]["attributes"]["name"] == DECISION_METRIC
            and not e["properties"].get("autonomy_setup")
        ]

    def flow_id(self) -> str:
        return next(f["id"] for f in self.fake.flows.values() if f["attributes"]["name"] == EMAIL_FLOW)

    def sync_engagement(self) -> None:
        report = self.rig.engine.sync(self.connection.connection_id, Stream.ENGAGEMENT, Lane.LIVE, self.now)
        assert report.status == "ok", report


def test_a_decision_is_handed_to_klaviyo_once_with_everything_the_template_needs() -> None:
    shop = Shop()
    decision = shop.decide("dec_1")
    assert shop.send(decision) is DispatchStatus.SENT
    assert shop.send(decision) is DispatchStatus.SKIPPED  # exactly once, even if dispatched again
    [handed] = shop.decision_events()
    assert handed["unique_id"] == "dec_1"
    assert handed["profile"]["data"]["attributes"] == {"email": ANN}
    props = handed["properties"]
    assert props["decision_id"] == "dec_1" and props["channel"] == "email"
    assert props["subject"] == "10% off, just for you"
    assert props["body"] == "Come back to trail shoes with 10% off this week."
    assert (
        props["content_id"] == "ci_1" and props["offer_id"] == "pct10" and props["offer_label"] == "10% off"
    )
    assert props["discount_code"] == "BACK10" and props["send_at"] == "2026-10-01T12:00:00Z"
    assert props["autonomy_schema"] == "autonomy.klaviyo.v1"
    [send] = [e for e in shop.rig.events.for_decision("dec_1") if e.type is EventType.SEND]
    assert send.attrs["provider_message_id"] == f"klaviyo:{shop.connection.connection_id}:dec_1"


def test_holdout_customers_never_reach_klaviyo() -> None:
    shop = Shop()
    assert shop.send(shop.decide("dec_h", group=Group.HOLDOUT)) is DispatchStatus.SKIPPED
    assert shop.decision_events() == []


def test_a_retry_after_a_lost_answer_is_recorded_once_by_klaviyo() -> None:
    shop = Shop()
    shop.fake.failures.append(httpx.ReadTimeout("the answer was lost"))
    posts_before = shop.fake.event_posts
    assert shop.send(shop.decide("dec_2")) is DispatchStatus.SENT
    assert shop.fake.event_posts == posts_before + 2  # sent twice by the client...
    assert len(shop.decision_events()) == 1  # ...recorded once (unique_id = decision id)


def test_nothing_is_handed_over_before_the_flow_is_live() -> None:
    shop = Shop(FakeKlaviyo(metric_lag=5), ready=False)  # Klaviyo is still creating the metric
    status = shop.send(shop.decide("dec_3"))
    assert status is DispatchStatus.FAILED
    [fail] = [e for e in shop.rig.events.for_decision("dec_3") if e.type is EventType.FAIL]
    assert str(fail.attrs["error"]).startswith("not_ready:")
    assert shop.decision_events() == []


def test_refusals_carry_the_codes_the_gateway_acts_on() -> None:
    shop = Shop()
    message = Message(
        message_id="m1",
        decision_id="dec_x",
        customer_id=ANN,
        channel=Channel.EMAIL,
        to="ann@nowhere",
        subject="s",
        body="b",
        send_at=T0,
    )
    receipt = shop.adapter.send(message)
    assert not receipt.accepted and (receipt.error or "").startswith(
        "invalid:"
    )  # never retried or failed over
    shop.fake.failures.append(jsonapi_error(429, "throttled", "", {"Retry-After": "300"}))
    receipt = shop.adapter.send(message.model_copy(update={"to": ANN}))
    assert not receipt.accepted and (receipt.error or "").startswith("rate_limited:")
    shop.fake.failures.extend([httpx.ConnectError("reset")] * 5)
    receipt = shop.adapter.send(message.model_copy(update={"to": ANN, "decision_id": "dec_y"}))
    assert not receipt.accepted and (receipt.error or "").startswith("ambiguous:")
    wrong = shop.adapter.send(message.model_copy(update={"channel": Channel.SMS}))
    assert (wrong.error or "").startswith("invalid:")
    assert shop.adapter.verify_webhook({}, b"{}") is False and shop.adapter.parse_webhook({}, b"{}") == []


def test_revoked_access_while_sending_asks_for_a_reconnect_once() -> None:
    shop = Shop()
    shop.fake.revoked = True
    first = shop.send(shop.decide("dec_4"))
    second = shop.send(shop.decide("dec_5"))
    assert first is DispatchStatus.FAILED and second is DispatchStatus.FAILED
    stored = shop.rig.store.get_connection(shop.connection.connection_id)
    assert stored is not None and stored.status is ConnectionStatus.NEEDS_RECONNECT
    assert [a.code for a in shop.rig.alerts.list_alerts()] == ["connector.needs_reconnect"]


def test_engagement_from_our_flow_is_linked_to_the_decision_and_counts_as_delivered() -> None:
    shop = Shop()
    shop.send(shop.decide("dec_6"))
    handed = shop.now
    shop.fake.add_event(
        "Received Email",
        pid="P1",
        at=handed + timedelta(minutes=1),
        props={"$flow": shop.flow_id(), "$message": "MSG1"},
    )
    shop.fake.add_event(
        "Opened Email", pid="P1", at=handed + timedelta(minutes=9), props={"$flow": shop.flow_id()}
    )
    shop.fake.add_event(
        "Opened Email", pid="P1", at=handed + timedelta(minutes=20), props={"$flow": "SHOP_NEWSLETTER"}
    )
    shop.rig.clock.advance(timedelta(hours=1))
    shop.sync_engagement()
    linked = {e.type for e in shop.rig.events.for_decision("dec_6")}
    assert {EventType.SEND, EventType.DELIVER, EventType.OPEN} <= linked
    theirs = [e for e in shop.rig.events.for_customer(ANN) if e.attrs.get("flow_id") == "SHOP_NEWSLETTER"]
    assert theirs and theirs[0].decision_id is None  # the shop's own newsletter is not our decision
    deliver = next(e for e in shop.rig.events.for_decision("dec_6") if e.type is EventType.DELIVER)
    assert deliver.attrs["via_autonomy"] is True and deliver.source == PROVIDER
    shop.rig.clock.advance(timedelta(hours=9))
    shop.sync_engagement()
    report = KlaviyoDeliveryWatch(shop.rig.engine, shop.rig.events, shop.connection.connection_id).check(
        shop.now
    )
    assert report.delivered == 1 and report.not_delivered == 0


def test_a_silent_drop_becomes_not_delivered_but_only_once_engagement_is_fresh() -> None:
    shop = Shop()
    shop.send(shop.decide("dec_ok"))  # delivered: proof that Klaviyo's deliveries link to hand-offs
    shop.fake.add_event(
        "Received Email", pid="P1", at=shop.now + timedelta(minutes=1), props={"$flow": shop.flow_id()}
    )
    shop.rig.clock.advance(timedelta(minutes=10))
    shop.send(shop.decide("dec_7"))  # Klaviyo will never deliver it (suppressed, filtered, ...)
    handed = shop.now
    watch = KlaviyoDeliveryWatch(shop.rig.engine, shop.rig.events, shop.connection.connection_id)
    shop.rig.clock.advance(timedelta(hours=3))
    shop.sync_engagement()
    early = watch.check(shop.now)
    assert early.waiting == 1 and early.not_delivered == 0
    shop.rig.clock.advance(timedelta(hours=4))  # past the 6 h window, but engagement read only to +3 h
    held = watch.check(shop.now)
    assert held.not_delivered == 0 and held.held is not None
    shop.sync_engagement()  # now read up to +7 h (minus the 3 h overlap: +4 h); still not enough
    assert watch.check(shop.now).not_delivered == 0
    shop.rig.clock.advance(timedelta(hours=3))
    shop.sync_engagement()
    done = watch.check(shop.now)
    assert done.not_delivered == 1
    [fail] = [e for e in shop.rig.events.for_decision("dec_7") if e.type is EventType.FAIL]
    assert fail.attrs["reason"] == NOT_DELIVERED and fail.channel is Channel.EMAIL
    assert fail.ts == handed + timedelta(hours=6) and fail.attrs["offer_id"] == "pct10"
    again = watch.check(shop.now + timedelta(hours=1))
    assert again.not_delivered == 0
    assert len([e for e in shop.rig.events.for_decision("dec_7") if e.type is EventType.FAIL]) == 1
    assert not [e for e in shop.rig.events.for_decision("dec_ok") if e.type is EventType.FAIL]


def test_the_watch_never_concludes_not_delivered_while_deliveries_do_not_link() -> None:
    # Klaviyo delivers, but its profiles key by email while Autonomy's customer ids are Shopify ids
    # and the profile has no external id: nothing links, so every hand-off would look dropped.
    shop = Shop(customer="7001")
    for n in range(4):
        shop.send(shop.decide(f"dec_u{n}"))
        shop.fake.add_event(
            "Received Email", pid="P1", at=shop.now + timedelta(minutes=1), props={"$flow": shop.flow_id()}
        )
        shop.rig.clock.advance(timedelta(minutes=30))
    shop.rig.clock.advance(timedelta(hours=12))
    shop.sync_engagement()
    shop.rig.clock.advance(timedelta(hours=4))
    shop.sync_engagement()
    watch = KlaviyoDeliveryWatch(shop.rig.engine, shop.rig.events, shop.connection.connection_id)
    for hour in range(3):
        report = watch.check(shop.now + timedelta(hours=hour))
        assert report.not_delivered == 0 and report.delivered == 0 and report.held is not None
    assert not [e for e in shop.rig.events.scan() if e.type is EventType.FAIL]
    unlinked = [a for a in shop.rig.alerts.list_alerts() if a.code == ALERT_UNLINKED]
    assert len(unlinked) == 1 and "identity" in unlinked[0].message  # once, not hourly


def test_the_watch_holds_when_the_connector_links_nothing() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector())  # registry default: no handoffs lookup
    connection = connect_ready(rig)
    report = KlaviyoDeliveryWatch(rig.engine, rig.events, connection.connection_id).check(
        T0 + timedelta(days=1)
    )
    assert report.held is not None and "handoffs" in report.held and report.events == []


def test_no_discount_code_is_minted_for_a_hand_off_that_is_refused() -> None:
    minted: list[str] = []

    def code(decision: Decision, offer: object) -> str:
        minted.append(decision.decision_id)
        return "ONE-OFF"

    shop = Shop(FakeKlaviyo(metric_lag=5), ready=False)
    shop.adapter.details = details_from(shop.decisions, shop.ws, discount_code=code)
    assert shop.send(shop.decide("dec_nr")) is DispatchStatus.FAILED  # not_ready
    assert minted == []


def test_klaviyos_own_drop_report_counts_without_waiting() -> None:
    shop = Shop()
    shop.send(shop.decide("dec_8"))
    shop.fake.add_event(
        "Dropped Email",
        pid="P1",
        at=shop.now + timedelta(minutes=1),
        props={"$flow": shop.flow_id(), "Reason": "suppressed"},
    )
    shop.rig.clock.advance(timedelta(hours=1))
    shop.sync_engagement()
    dropped = [e for e in shop.rig.events.for_decision("dec_8") if e.type is EventType.FAIL]
    assert dropped and dropped[0].attrs["reason"] == "suppressed"
    shop.rig.clock.advance(timedelta(hours=10))
    shop.sync_engagement()
    report = KlaviyoDeliveryWatch(shop.rig.engine, shop.rig.events, shop.connection.connection_id).check(
        shop.now
    )
    assert report.not_delivered == 0  # already known as not delivered; no second FAIL


def test_the_hourly_watch_job_rechecks_setup_and_requeues_itself() -> None:
    shop = Shop()
    queue = shop.rig.queue
    job = seed_klaviyo_watch(queue, shop.connection.connection_id, shop.now)
    handlers = klaviyo_handlers(shop.rig.engine, shop.rig.events, queue)
    claimed = queue.claim([WATCH_JOB_KIND], worker="w", now=shop.now)
    assert claimed is not None and claimed.job_id == job.job_id
    handlers[WATCH_JOB_KIND](claimed, shop.now)
    nxt = watch_job(shop.connection.connection_id, T0 + timedelta(hours=1))
    assert queue.get(nxt.job_id) is not None


def test_gateway_rates_match_klaviyos_create_event_limits() -> None:
    limiter = RateLimiter(provider_rates=provider_rates())
    allowed = sum(1 for _ in range(400) if limiter.take_provider("klaviyo", Channel.EMAIL, T0).allowed)
    assert allowed == 350  # the burst
    later = T0 + timedelta(minutes=1)
    assert limiter.take_provider("klaviyo", Channel.EMAIL, later).allowed


def test_with_shopify_customer_ids_the_profile_is_named_by_external_id_and_engagement_links_back() -> None:
    shop = Shop(customer="7001", external_id="7001")  # Klaviyo's Shopify integration keys the profile
    assert shop.send(shop.decide("dec_9")) is DispatchStatus.SENT
    [handed] = shop.decision_events()
    assert handed["profile"]["data"]["attributes"] == {"external_id": "7001"}
    shop.fake.add_event(
        "Received Email", pid="P1", at=shop.now + timedelta(minutes=2), props={"$flow": shop.flow_id()}
    )
    shop.rig.clock.advance(timedelta(hours=1))
    shop.sync_engagement()
    assert EventType.DELIVER in {e.type for e in shop.rig.events.for_decision("dec_9")}
    assert all(e.customer_id == "7001" for e in shop.rig.events.for_decision("dec_9"))
