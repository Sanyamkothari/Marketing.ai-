"""Exceptions-only inbox, one-click Autopilot (with automatic demotion) and the quiet data watchdog."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from autonomy.connectors.health import HealthMonitor
from autonomy.contracts import (
    Action,
    Alert,
    AutonomyLevel,
    Bid,
    Channel,
    ChannelStats,
    CustomerState,
    Decision,
    DecisionKind,
    Event,
    EventType,
    GoalStatus,
    Group,
    stable_id,
)
from autonomy.fakes import InMemoryDecisionLog, InMemoryEventStore, InMemoryStateStore, InMemoryWorkspace
from autonomy.fakes.samples import sample_brief, sample_goal
from autonomy.inbox import (
    ROUTES,
    Button,
    Condition,
    DataWatchdog,
    Inbox,
    InboxKind,
    InboxSources,
    InMemoryInboxStore,
    InMemoryWatchdogStore,
    SqlInboxStore,
    collect,
    quiet_week,
    route_alert,
)
from autonomy.inbox.install import ZeroTouch, install, zero_touch_for_runtime
from autonomy.inbox.watchdog import poisson_cdf
from autonomy.mandate.models import Mandate, MandateStatus
from autonomy.runtime.promotion import (
    AUTO_PROMOTE_CLAUSE,
    AutopilotGuard,
    InMemoryAutonomyLedger,
    SqlAutonomyLedger,
    assess,
    with_auto_promotion,
)
from autonomy.runtime.stores import InMemoryAlertStore
from autonomy.store import memory_engine

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
GOAL = "goal_winback"
BIZ = "biz_1"


# --- helpers ----------------------------------------------------------------------------------------


def cond(cause: str, fingerprint: str, since: datetime, **kw: Any) -> Condition:
    return Condition(
        cause=cause,
        kind=kw.pop("kind", InboxKind.DATA),
        sentence=kw.pop("sentence", "Something needs you."),
        action="Do the thing.",
        button=kw.pop("button", Button(label="Do it", action="mark_handled")),
        fingerprint=fingerprint,
        since=since,
        **kw,
    )


def alert(code: str, at: datetime, *, goal_id: str | None = GOAL, message: str = "", **kw: Any) -> Alert:
    return Alert(
        alert_id=stable_id("alert", code, goal_id, at.isoformat()),
        at=at,
        goal_id=goal_id,
        severity=kw.pop("severity", "critical"),
        code=code,
        message=message or f"{code} tripped. Paused the goal.",
        **kw,
    )


def purchase(cid: str, at: datetime, value: float = 50.0, margin: float | None = 20.0, n: int = 0) -> Event:
    return Event(
        event_id=stable_id("ev", cid, at.isoformat(), n),
        customer_id=cid,
        type=EventType.PURCHASE,
        ts=at,
        source="shopify",
        value=value,
        margin=margin,
    )


def act_decision(cid: str, at: datetime, *, expected: float = 0.8, value: float = 20.0) -> Decision:
    action = Action(goal_id=GOAL, channel=Channel.EMAIL, content_id="c1", send_at=at)
    bid = Bid(
        goal_id=GOAL,
        action=action,
        uplift=0.05,
        value_if_converted=value,
        channel_cost=0.01,
        expected_net=expected,
        uncertainty=0.5,
    )
    return Decision(
        decision_id=stable_id("dec", cid, at.isoformat()),
        customer_id=cid,
        made_at=at,
        kind=DecisionKind.ACT,
        action=action,
        group=Group.AGENT,
        probability=0.5,
        expected_net=expected,
        reason="Sent a reminder.",
        bids=(bid,),
        policy_version="test",
    )


def nothing(cid: str, at: datetime, group: Group = Group.HOLDOUT) -> Decision:
    bid_action = Action(goal_id=GOAL, channel=Channel.EMAIL, send_at=at)
    bid = Bid(goal_id=GOAL, action=bid_action, uplift=0.0, value_if_converted=0.0, expected_net=0.0)
    return Decision(
        decision_id=stable_id("dec", cid, at.isoformat(), "nothing"),
        customer_id=cid,
        made_at=at,
        kind=DecisionKind.NOTHING,
        group=group,
        probability=1.0,
        reason="Left alone.",
        bids=(bid,),
        policy_version="test",
    )


@dataclass
class World:
    workspace: InMemoryWorkspace
    decisions: InMemoryDecisionLog
    events: InMemoryEventStore
    alerts: InMemoryAlertStore
    ledger: InMemoryAutonomyLedger

    def guard(self, **kw: Any) -> AutopilotGuard:
        return AutopilotGuard(
            business_id=BIZ,
            workspace=self.workspace,
            decisions=self.decisions,
            events=self.events,
            ledger=self.ledger,
            alerts=self.alerts,
            **kw,
        )


def world(
    level: AutonomyLevel,
    *,
    agent: int = 400,
    holdout: int = 60,
    agent_buys_every: int = 3,
    holdout_buys_every: int = 3,
    sent: bool = False,
    days: int = 11,
    horizon: int = 7,
) -> World:
    """A goal at `level` since T0: one contact decision per agent customer over `days` days, one
    "do nothing" per holdout customer, and every n-th customer of each group buying two days later."""
    ws = InMemoryWorkspace()
    ws.put_goal(sample_goal(T0).model_copy(update={"autonomy": level, "status": GoalStatus.ACTIVE}))
    ws.put_brief(sample_brief(T0).model_copy(update={"horizon_days": horizon}))
    log = InMemoryDecisionLog()
    events = InMemoryEventStore()
    for i in range(agent):
        cid = f"a{i}"
        at = T0 + timedelta(days=i % days, hours=1)
        d = act_decision(cid, at)
        log.record([d])
        if sent:
            events.append(
                [
                    Event(
                        event_id=f"send_{cid}",
                        customer_id=cid,
                        type=EventType.SEND,
                        ts=at,
                        source="ses",
                        channel=Channel.EMAIL,
                        decision_id=d.decision_id,
                    )
                ]
            )
        if i % agent_buys_every == 0:
            events.append([purchase(cid, at + timedelta(days=2))])
    for i in range(holdout):
        cid = f"h{i}"
        at = T0 + timedelta(days=i % days, hours=1)
        log.record([nothing(cid, at)])
        if i % holdout_buys_every == 0:
            events.append([purchase(cid, at + timedelta(days=2))])
    return World(ws, log, events, InMemoryAlertStore(), InMemoryAutonomyLedger())


def active_mandate(note: str = "") -> Mandate:
    return Mandate(
        mandate_id="m1",
        business_id=BIZ,
        goal_ids=(GOAL,),
        budget_ceiling=1000,
        channels=(Channel.EMAIL,),
        valid_from=T0 - timedelta(days=1),
        valid_until=T0 + timedelta(days=90),
        status=MandateStatus.ACTIVE,
        note=note,
    )


# --- the inbox state machine ------------------------------------------------------------------------


def test_one_item_per_cause_that_resolves_itself_when_the_condition_clears() -> None:
    inbox = Inbox(InMemoryInboxStore(), BIZ)
    c = cond("reconnect:k1", "needs_reconnect:1", T0, kind=InboxKind.RECONNECT)
    for minutes in (0, 5, 10):  # every tick sees it: still one item
        inbox.sync([c, c], T0 + timedelta(minutes=minutes))
    assert [i.cause for i in inbox.open_items()] == ["reconnect:k1"]
    report = inbox.sync([], T0 + timedelta(minutes=15))
    assert len(report.resolved) == 1 and inbox.open_items() == []
    item = inbox.store.latest(BIZ, "reconnect:k1")
    assert item is not None and item.resolved_by == "cleared"
    assert len(inbox.appearances(T0, T0 + timedelta(days=1))) == 1


def test_a_resolved_instance_never_returns_and_a_new_one_reopens_within_the_cool_down() -> None:
    inbox = Inbox(InMemoryInboxStore(), BIZ)
    inbox.sync([cond("stale:s1", "f1", T0)], T0)
    inbox.sync([], T0 + timedelta(hours=1))
    first = inbox.store.latest(BIZ, "stale:s1")
    assert first is not None and not first.open
    inbox.sync([cond("stale:s1", "f1", T0)], T0 + timedelta(hours=2))  # the same instance: never again
    assert inbox.open_items() == []
    inbox.sync([cond("stale:s1", "f2", T0 + timedelta(hours=3))], T0 + timedelta(hours=3))
    again = inbox.store.latest(BIZ, "stale:s1")
    assert again is not None and again.open and again.item_id == first.item_id and again.reopened == 1
    inbox.sync([], T0 + timedelta(hours=4))
    later = T0 + timedelta(days=5)  # after the 3-day cool-down: a new item
    inbox.sync([cond("stale:s1", "f3", later)], later)
    newest = inbox.store.latest(BIZ, "stale:s1")
    assert newest is not None and newest.episode == 2 and newest.item_id != first.item_id
    assert len(inbox.appearances(T0, later + timedelta(minutes=1))) == 3


def test_a_short_outage_never_reaches_the_inbox_and_a_flicker_does_not_close_an_item() -> None:
    inbox = Inbox(InMemoryInboxStore(), BIZ)
    c = cond("stale:s1", "f1", T0, persist_after=timedelta(hours=6), clear_after=timedelta(hours=1))
    report = inbox.sync([c], T0 + timedelta(hours=2))
    assert report.waiting == ["stale:s1"] and inbox.open_items() == []
    inbox.sync([], T0 + timedelta(hours=3))  # healed within the hour: nobody was told
    assert inbox.store.latest(BIZ, "stale:s1") is None
    inbox.sync([c], T0 + timedelta(hours=6))
    assert len(inbox.open_items()) == 1
    inbox.sync([], T0 + timedelta(hours=6, minutes=5))  # gone for 5 minutes: kept
    inbox.sync([c], T0 + timedelta(hours=6, minutes=10))
    inbox.sync([], T0 + timedelta(hours=6, minutes=15))
    assert len(inbox.open_items()) == 1
    inbox.sync([], T0 + timedelta(hours=7, minutes=20))  # gone for over an hour: resolved
    assert inbox.open_items() == []


def test_a_person_resolving_an_item_closes_that_instance_for_good() -> None:
    store = SqlInboxStore(memory_engine())
    inbox = Inbox(store, BIZ)
    inbox.sync([cond("replies", "r1", T0, kind=InboxKind.CUSTOMER)], T0)
    item = inbox.open_items()[0]
    assert inbox.resolve(item.item_id, T0 + timedelta(minutes=1), by="owner") is not None
    inbox.sync([cond("replies", "r1", T0, kind=InboxKind.CUSTOMER)], T0 + timedelta(minutes=2))
    assert inbox.open_items() == [] and inbox.handled_at("replies") == T0 + timedelta(minutes=1)
    inbox.sync(
        [cond("replies", "r2", T0 + timedelta(hours=1), kind=InboxKind.CUSTOMER)], T0 + timedelta(hours=1)
    )
    assert [i.episode for i in inbox.open_items()] == [2]


def test_every_alert_code_raised_today_has_a_destination_and_noise_goes_to_the_briefing() -> None:
    from autonomy.channels import klaviyo as klaviyo_channel
    from autonomy.connectors import health

    raised = {getattr(health, name) for name in dir(health) if name.startswith("ALERT_")}
    raised |= {klaviyo_channel.ALERT_SETUP, klaviyo_channel.ALERT_UNLINKED, "proof.negative_lift"}
    assert raised <= set(ROUTES)
    at = T0
    assert route_alert(alert("connector.row_drop", at, severity="warning")).destination == "briefing"
    assert route_alert(alert("connector.stale", at, severity="warning")).destination == "briefing"
    assert route_alert(alert("connector.stale", at, severity="critical")).kind is InboxKind.DATA
    assert route_alert(alert("decide.error", at, severity="warning")).destination == "ops"
    assert route_alert(alert("breaker.complaint_rate", at)).kind is InboxKind.SAFETY_STOP
    assert route_alert(alert("something.new", at)).destination == "briefing"
    section = quiet_week(
        [alert("connector.row_drop", at, severity="warning"), alert("decide.error", at, severity="warning")],
        [],
        at - timedelta(days=1),
        at + timedelta(days=1),
    )
    assert section.body.startswith("Nothing needed you this week.") and section.numbers["notes_data"] == 1.0
    assert section.numbers["faults_for_operator"] == 1.0


# --- the condition builders -------------------------------------------------------------------------


def test_a_breaker_pause_is_one_safety_item_with_one_button_that_clears_on_resume() -> None:
    w = world(AutonomyLevel.AUTOPILOT, agent=0, holdout=0)
    w.workspace.put_goal(w.workspace.goals()[0].model_copy(update={"status": GoalStatus.PAUSED}))
    w.alerts.put([alert("breaker.complaint_rate", T0, auto_paused=True)])
    inbox = Inbox(InMemoryInboxStore(), BIZ)
    sources = InboxSources(
        business_id=BIZ, workspace=w.workspace, alerts=w.alerts, handled_at=inbox.handled_at
    )
    inbox.sync(collect(sources, T0 + timedelta(minutes=5)).conditions, T0 + timedelta(minutes=5))
    [item] = inbox.open_items()
    assert item.kind is InboxKind.SAFETY_STOP and item.button.action == "resume_in_approve"
    # Still paused six weeks on: still one open item (a paused goal waits for a person, however long).
    later = T0 + timedelta(days=42)
    inbox.sync(collect(sources, later).conditions, later)
    assert [i.item_id for i in inbox.open_items()] == [item.item_id]
    assert item.sentence.count(".") == 1 and "complaints" in item.sentence
    zt = ZeroTouch(inbox=inbox, sources=sources, guard=w.guard())
    result = zt.act(item.item_id, T0 + timedelta(minutes=10), by="owner")
    goal = w.workspace.goals()[0]
    assert result.done and goal.status is GoalStatus.ACTIVE and goal.autonomy is AutonomyLevel.APPROVE
    assert [e.reason for e in w.ledger.entries()] == ["resumed_in_approve"]
    inbox.sync(collect(sources, T0 + timedelta(minutes=15)).conditions, T0 + timedelta(minutes=15))
    assert inbox.open_items() == []


def test_a_goal_paused_for_losing_money_is_one_item_worded_without_holdout() -> None:
    w = world(AutonomyLevel.AUTOPILOT, agent=0, holdout=0)
    w.workspace.put_goal(w.workspace.goals()[0].model_copy(update={"status": GoalStatus.PAUSED}))
    message = (
        "Against the no-contact holdout, this goal's incremental profit is -120.00 (95% interval -200.00 to "
        "-40.00; 400 agent and 60 holdout customers over closed outcome windows). Paused the goal."
    )
    w.alerts.put([alert("proof.negative_lift", T0, auto_paused=True, message=message)])
    sources = InboxSources(business_id=BIZ, workspace=w.workspace, alerts=w.alerts)
    [c] = collect(sources, T0 + timedelta(minutes=5)).conditions
    assert c.kind is InboxKind.LOSING and c.cause == f"losing:{GOAL}"
    assert "holdout" not in (c.sentence + c.detail).lower() and "people we never contact" in c.detail


def test_replies_are_one_item_and_marking_them_read_waits_for_new_ones() -> None:
    w = world(AutonomyLevel.AUTOPILOT, agent=0, holdout=0)
    w.alerts.put(
        [
            alert(
                "reply.needs_human",
                T0,
                goal_id=None,
                severity="warning",
                message="Customer c1 replied (complaint); a person should read it.",
            ),
            alert(
                "reply.needs_human",
                T0 + timedelta(minutes=1),
                goal_id=None,
                severity="warning",
                message="Customer c2 replied (question); a person should read it.",
            ),
        ]
    )
    inbox = Inbox(InMemoryInboxStore(), BIZ)
    sources = InboxSources(
        business_id=BIZ, workspace=w.workspace, alerts=w.alerts, handled_at=inbox.handled_at
    )
    zt = ZeroTouch(inbox=inbox, sources=sources)
    zt.tick(T0 + timedelta(minutes=5))
    [item] = inbox.open_items()
    assert item.sentence == "2 customers replied and need a person, including a complaint."
    zt.act(item.item_id, T0 + timedelta(minutes=6), by="owner")
    zt.tick(T0 + timedelta(minutes=7))
    assert inbox.open_items() == []
    w.alerts.put(
        [
            alert(
                "reply.needs_human",
                T0 + timedelta(minutes=8),
                goal_id=None,
                severity="warning",
                message="Customer c3 replied (interested); a person should read it.",
            )
        ]
    )
    zt.tick(T0 + timedelta(minutes=9))
    assert [i.sentence for i in inbox.open_items()] == ["1 customer replied and needs a person."]


def test_beyond_the_mandate_reaches_the_inbox_only_when_it_keeps_happening() -> None:
    ws = InMemoryWorkspace()
    ws.put_goal(
        sample_goal(T0).model_copy(update={"autonomy": AutonomyLevel.AUTOPILOT, "status": GoalStatus.ACTIVE})
    )
    log = InMemoryDecisionLog()
    for i in range(60):
        d = act_decision(f"c{i}", T0 + timedelta(hours=i))
        if i < 25:
            d = d.model_copy(update={"blocked_by": ("mandate_offer_not_approved",)})
        log.record([d])
    sources = InboxSources(business_id=BIZ, workspace=ws, decisions=log)
    found = collect(sources, T0 + timedelta(days=3)).conditions
    [c] = found
    assert (
        c.cause == f"beyond:{GOAL}:offer"
        and c.money is not None
        and c.money.low <= c.money.point <= c.money.high
    )
    assert "25 times" in c.sentence and c.button.label == "Review the offer"
    few = InMemoryDecisionLog()
    for i in range(60):
        d = act_decision(f"c{i}", T0 + timedelta(hours=i))
        few.record([d.model_copy(update={"blocked_by": ("mandate_offer_not_approved",)}) if i < 5 else d])
    assert (
        collect(InboxSources(business_id=BIZ, workspace=ws, decisions=few), T0 + timedelta(days=3)).conditions
        == []
    )


# --- the data watchdog ------------------------------------------------------------------------------


def orders(
    days: int,
    per_day: int,
    *,
    skip: Sequence[int] = (),
    source: str = "shopify",
    digits: bool = True,
    start: datetime = T0,
) -> list[Event]:
    out: list[Event] = []
    for d in range(days):
        if d in skip:
            continue
        for n in range(per_day):
            cid = f"{1000 + n}" if digits else f"gid://shopify/Customer/{1000 + n}"
            at = start + timedelta(days=d, hours=1 + (n % 12))
            out.append(purchase(cid, at, n=d).model_copy(update={"source": source}))
    return out


def test_a_missing_day_of_orders_holds_decisions_and_a_quiet_small_shop_is_never_held() -> None:
    events = InMemoryEventStore()
    events.append(orders(40, 40, skip=(38,)))
    now = T0 + timedelta(days=40, hours=3)
    dog = DataWatchdog(BIZ, events)
    [finding] = dog.findings(now)
    assert finding.code == "missing_day" and finding.level == "hold"
    assert (
        finding.sentence.startswith("No shopify orders arrived for") and "usual 40 a day" in finding.sentence
    )
    # The data gate holds: ready_to_act carries the watchdog's reason.
    monitor = HealthMonitor(_NoConnections(), _NoRegistry(), watchdog=dog)  # type: ignore[arg-type]
    verdict = monitor.ready_to_act(BIZ, now)
    assert not verdict.can_act and verdict.reasons == (finding.sentence,)
    # A person vouches for it: the hold is released.
    dog.acknowledge(finding.fingerprint, now, by="owner")
    assert monitor.ready_to_act(BIZ, now).can_act
    small = InMemoryEventStore()
    small.append(orders(40, 3, skip=(38,)))
    assert DataWatchdog(BIZ, small).findings(now) == []
    assert poisson_cdf(0, 3.0) > 1e-4 > poisson_cdf(0, 40.0)


def test_a_customer_id_that_changes_form_holds_and_a_lost_field_is_only_a_note() -> None:
    events = InMemoryEventStore()
    events.append(orders(37, 20))
    late = [
        e.model_copy(update={"margin": None})
        for e in orders(3, 20, digits=False, start=T0 + timedelta(days=37))
    ]
    events.append(late)
    now = T0 + timedelta(days=40, hours=3)
    found = {(f.code, f.level) for f in DataWatchdog(BIZ, events).findings(now)}
    assert ("schema_change", "hold") in found and ("schema_change", "note") in found


def test_a_consent_jump_holds_a_consent_drop_is_a_notice_and_duplicates_are_a_notice() -> None:
    states = InMemoryStateStore()
    reach = ChannelStats(address_known=True, consent=True)
    states.put(
        CustomerState(
            customer_id=f"c{i}",
            as_of=T0,
            channels={Channel.EMAIL: reach if i < 360 else ChannelStats(address_known=True)},
        )
        for i in range(400)
    )
    store = InMemoryWatchdogStore()
    store.put_snapshot(BIZ, (T0 - timedelta(days=7)).date(), "consent_coverage", 0.5)
    dog = DataWatchdog(BIZ, InMemoryEventStore(), states=states, store=store)
    [jump] = dog.findings(T0)
    assert jump.code == "consent_jump" and jump.level == "hold"
    store2 = InMemoryWatchdogStore()
    store2.put_snapshot(BIZ, (T0 - timedelta(days=7)).date(), "consent_coverage", 0.95)
    states2 = InMemoryStateStore()
    states2.put(
        CustomerState(
            customer_id=f"c{i}",
            as_of=T0,
            channels={Channel.EMAIL: reach if i < 200 else ChannelStats(address_known=True)},
        )
        for i in range(400)
    )
    [drop] = DataWatchdog(BIZ, InMemoryEventStore(), states=states2, store=store2).findings(T0)
    assert drop.code == "consent_drop" and drop.level == "notice"

    @dataclass
    class Summary:
        shopify_customers: int = 1000
        conflicts: int = 80

    class Identity:
        def summary(self, business_id: str) -> Summary:
            return Summary()

    [dupes] = DataWatchdog(BIZ, InMemoryEventStore(), identity=Identity()).findings(T0)
    assert dupes.code == "duplicates" and dupes.level == "notice" and "8%" in dupes.sentence


class _NoConnections:
    def connections(self, *, business_id: str | None = None, kind: str | None = None) -> list[Any]:
        return []


class _NoRegistry:
    def find(self, kind: str) -> None:
        return None


# --- Shadow -> Autopilot -------------------------------------------------------------------------------


def test_the_shadow_bar_states_its_rule_and_a_fair_matched_forecast_earns_autopilot() -> None:
    w = world(AutonomyLevel.SHADOW)
    goal, brief = w.workspace.goals()[0], w.workspace.briefs(GOAL)[0]
    decisions = list(w.decisions.scan())
    by_customer: dict[str, list[Event]] = {}
    for e in w.events.scan():
        by_customer.setdefault(e.customer_id, []).append(e)
    early = assess(goal, brief, decisions, by_customer, T0 + timedelta(days=10), since=T0)
    assert not early.ready and early.sentence == "Shadow needs 4 more days before Autopilot can be judged."
    r = assess(goal, brief, decisions, by_customer, T0 + timedelta(days=20), since=T0)
    assert r.ready, r.checks
    assert r.forecast is not None and 0 < r.forecast.low < r.forecast.point < r.forecast.high
    assert r.sentence.startswith("Shadow ran 20 days: it would have made about $")
    assert "matched within 0%" in r.sentence
    assert {c.name for c in r.checks} >= {"days", "forecasts", "pays", "value", "fair", "safety", "data"}
    # Holdout customers who buy far more than everyone else while nothing is sent: not fair, not ready.
    skew = world(AutonomyLevel.SHADOW, holdout_buys_every=1)
    sk_by: dict[str, list[Event]] = {}
    for e in skew.events.scan():
        sk_by.setdefault(e.customer_id, []).append(e)
    bad = assess(goal, brief, list(skew.decisions.scan()), sk_by, T0 + timedelta(days=20), since=T0)
    assert not bad.ready and not next(c for c in bad.checks if c.name == "fair").ok


def test_one_click_turns_on_autopilot_and_logs_it_against_the_signed_mandate() -> None:
    w = world(AutonomyLevel.SHADOW)
    mandate = active_mandate()
    guard = w.guard(mandate_for=lambda gid: mandate, mandate_ledger_head=lambda: "a" * 64)
    now = T0 + timedelta(days=20)
    inbox = Inbox(InMemoryInboxStore(), BIZ)
    zt = ZeroTouch(
        inbox=inbox, sources=InboxSources(business_id=BIZ, workspace=w.workspace, guard=guard), guard=guard
    )
    zt.tick(now)
    [offer] = inbox.open_items()
    assert offer.button.action == "turn_on_autopilot" and offer.money is not None
    result = zt.act(offer.item_id, now, by="owner")
    assert result.done and w.workspace.goals()[0].autonomy is AutonomyLevel.AUTOPILOT
    [entry] = w.ledger.entries()
    assert entry.actor == "person:owner" and entry.reason == "turned_on"
    assert entry.mandate_id == "m1" and entry.mandate_body_hash and entry.mandate_ledger_head == "a" * 64
    assert entry.evidence["forecast"] > 0 and w.ledger.verify_chain().ok
    tampered = InMemoryAutonomyLedger([entry.model_copy(update={"actor": "autopilot"})])
    assert tampered.verify_chain().problem == "hash_mismatch"
    zt.tick(now + timedelta(minutes=5))
    assert inbox.open_items() == []


def test_autopilot_turns_itself_on_only_when_the_signed_mandate_pre_authorises_it() -> None:
    now = T0 + timedelta(days=20)
    plain = world(AutonomyLevel.SHADOW)
    plain.guard(mandate_for=lambda gid: active_mandate()).run(now)
    assert plain.workspace.goals()[0].autonomy is AutonomyLevel.SHADOW
    pre = world(AutonomyLevel.SHADOW)
    signed = active_mandate(note=AUTO_PROMOTE_CLAUSE)
    report = pre.guard(mandate_for=lambda gid: signed).run(now)
    assert report.promoted == [GOAL] and pre.workspace.goals()[0].autonomy is AutonomyLevel.AUTOPILOT
    assert pre.ledger.entries()[0].actor == "autopilot"
    nxt = with_auto_promotion(active_mandate())
    assert nxt.version == 2 and nxt.status is MandateStatus.DRAFT and nxt.signers == ()


def test_a_safety_stop_drops_autopilot_to_approve_with_one_item_and_no_automatic_return() -> None:
    w = world(AutonomyLevel.AUTOPILOT, sent=True)
    signed = active_mandate(note=AUTO_PROMOTE_CLAUSE)
    guard = w.guard(mandate_for=lambda gid: signed)
    inbox = Inbox(InMemoryInboxStore(), BIZ)
    sources = InboxSources(
        business_id=BIZ, workspace=w.workspace, alerts=w.alerts, guard=guard, handled_at=inbox.handled_at
    )
    zt = ZeroTouch(inbox=inbox, sources=sources, guard=guard)
    trip = T0 + timedelta(days=12)
    w.alerts.put([alert("breaker.unsubscribe_multiple", trip, auto_paused=True)])
    w.workspace.put_goal(w.workspace.goals()[0].model_copy(update={"status": GoalStatus.PAUSED}))
    result = zt.tick(trip + timedelta(minutes=5))
    assert result.guard is not None and result.guard.demoted == [GOAL]
    goal = w.workspace.goals()[0]
    assert goal.autonomy is AutonomyLevel.APPROVE
    [entry] = w.ledger.entries()
    assert entry.reason == "safety_stop:breaker.unsubscribe_multiple" and entry.actor == "autopilot"
    [item] = inbox.open_items()  # the breaker's pause and the demotion are one item
    assert item.kind is InboxKind.SAFETY_STOP and item.cause == f"safety_stop:{GOAL}"
    # Even when it earns the bar again, it does not promote itself until a person turns it on.
    w.workspace.put_goal(goal.model_copy(update={"status": GoalStatus.ACTIVE}))
    assert guard.run(trip + timedelta(days=30)).promoted == []


def test_proof_turning_negative_drops_autopilot_to_approve_with_the_loss_and_its_range() -> None:
    w = world(AutonomyLevel.AUTOPILOT, sent=True, agent_buys_every=20, holdout_buys_every=2)
    guard = w.guard()
    early = guard.run(T0 + timedelta(days=10))  # under two outcome windows: not judged yet
    assert early.demoted == []
    now = T0 + timedelta(days=21)
    inbox = Inbox(InMemoryInboxStore(), BIZ)
    sources = InboxSources(
        business_id=BIZ, workspace=w.workspace, alerts=w.alerts, guard=guard, handled_at=inbox.handled_at
    )
    zt = ZeroTouch(inbox=inbox, sources=sources, guard=guard)
    result = zt.tick(now)
    assert result.guard is not None and result.guard.demoted == [GOAL]
    [entry] = w.ledger.entries()
    assert entry.reason == "losing" and entry.evidence["incremental_profit_high"] < 0
    [item] = inbox.open_items()
    assert item.kind is InboxKind.LOSING and item.button.action == "pause_goal"
    assert item.money is not None and item.money.high < 0 and "people we never contact" in item.sentence


def test_a_level_changed_on_the_plan_page_is_logged_as_a_person_s_change() -> None:
    w = world(AutonomyLevel.SHADOW)
    guard = w.guard(mandate_for=lambda gid: active_mandate(note=AUTO_PROMOTE_CLAUSE))
    guard.run(T0 + timedelta(days=20))
    w.workspace.put_goal(w.workspace.goals()[0].model_copy(update={"autonomy": AutonomyLevel.APPROVE}))
    report = guard.run(T0 + timedelta(days=21))
    assert report.logged == [GOAL]
    assert [(e.from_level, e.to_level, e.actor) for e in w.ledger.entries()] == [
        (AutonomyLevel.SHADOW, AutonomyLevel.AUTOPILOT, "autopilot"),
        (AutonomyLevel.AUTOPILOT, AutonomyLevel.APPROVE, "person:plan"),
    ]


def test_the_sql_autonomy_ledger_chains_across_writers() -> None:
    db = memory_engine()
    one, two = SqlAutonomyLedger(db), SqlAutonomyLedger(db)
    for i, ledger in enumerate((one, two, one)):
        ledger.append(
            at=T0 + timedelta(minutes=i),
            business_id=BIZ,
            goal_id=GOAL,
            from_level=AutonomyLevel.SHADOW,
            to_level=AutonomyLevel.AUTOPILOT,
            actor="person:owner",
            reason="turned_on",
            words="On.",
        )
    assert [e.seq for e in two.entries()] == [1, 2, 3] and two.verify_chain().ok


# --- the API --------------------------------------------------------------------------------------------


def test_the_inbox_api_lists_items_presses_the_button_and_offers_autopilot() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.v2.inbox import InboxServices, attach_inbox_services, autopilot_router, router

    w = world(AutonomyLevel.SHADOW)
    guard = w.guard()
    inbox = Inbox(InMemoryInboxStore(), BIZ)
    events = InMemoryEventStore()
    events.append(orders(40, 40, skip=(38,)))
    dog = DataWatchdog(BIZ, events)
    sources = InboxSources(
        business_id=BIZ,
        workspace=w.workspace,
        alerts=w.alerts,
        guard=guard,
        watchdog=dog,
        handled_at=inbox.handled_at,
    )
    zt = ZeroTouch(inbox=inbox, sources=sources, guard=guard, watchdog=dog)
    clock = {"now": T0 + timedelta(days=20)}
    app = FastAPI()
    attach_inbox_services(app, InboxServices(zero_touch=zt, clock=lambda: clock["now"]))
    app.include_router(router)
    app.include_router(autopilot_router)
    client = TestClient(app)
    clock["now"] = T0 + timedelta(days=40, hours=3)
    body = client.get("/v2/inbox", params={"refresh": True}).json()
    kinds = [i["kind"] for i in body["items"]]
    assert kinds == ["data", "approval"] and body["words"] == "2 things need you."
    data_item = body["items"][0]
    assert data_item["button"]["label"] == "It was a quiet day"
    pressed = client.post(f"/v2/inbox/{data_item['item_id']}/act", json={"by": "owner"}).json()
    assert pressed["done"] and pressed["item"]["resolved_by"] == "person"
    assert client.get("/v2/inbox/data").json()["trusted"] is True
    assert client.post("/v2/inbox/nope/act").status_code == 404
    offer = client.get(f"/v2/autopilot/{GOAL}").json()
    assert offer["ready"] and offer["forecast"]["low"] > 0
    on = client.post(f"/v2/autopilot/{GOAL}/turn-on", json={"by": "owner"})
    assert on.status_code == 200 and on.json()["to_level"] == "autopilot"
    assert client.post(f"/v2/autopilot/{GOAL}/turn-on").status_code == 409
    ledger = client.get("/v2/autopilot/ledger").json()
    assert ledger["chain_ok"] and len(ledger["entries"]) == 1
    assert client.get("/v2/inbox").json()["items"] == []
    history = client.get("/v2/inbox/history", params={"weeks": 8}).json()
    assert len(history["weeks"]) == 8 and sum(wk["items"] for wk in history["weeks"]) == 2


# --- end to end ------------------------------------------------------------------------------------------


def test_a_connected_shop_raises_one_reconnect_item_and_the_watchdog_holds_its_runtime() -> None:
    from autonomy.connectors import NeedsReconnect
    from tests.v2.zero_touch.wiring_support import SWEEP, make_shop

    shop = make_shop()
    connection = shop.connect_klaviyo()
    shop.add_customers(["101", "102", "103"])
    shop.fresh(connection, SWEEP)
    zt = zero_touch_for_runtime(shop.runtime, engine=shop.db, wiring=shop.wiring)
    install(shop.runtime, zt, monitor=shop.engine.monitor)
    shop.engine.tokens.on_revoked(connection, NeedsReconnect("the key was deleted"), SWEEP)
    shop.runtime.tick(shop.at(SWEEP))
    shop.runtime.tick(shop.at(SWEEP + timedelta(minutes=5)))
    [item] = zt.inbox.open_items()
    assert item.kind is InboxKind.RECONNECT and item.button.label == "Reconnect Klaviyo"
    shop.engine.disconnect(connection.connection_id, shop.at(SWEEP + timedelta(minutes=10)))
    shop.runtime.tick(shop.at(SWEEP + timedelta(minutes=15)))
    assert zt.inbox.open_items() == []
    # A day of orders that never arrived: the watchdog's hold reaches the runtime through ready_to_act.
    shop.runtime.events.append(orders(30, 30, skip=(28,), start=SWEEP - timedelta(days=30)))
    zt.watchdog.invalidate()  # type: ignore[union-attr]
    report = shop.runtime.tick(shop.at(SWEEP + timedelta(minutes=20)))
    assert report.data_held and report.data_held[0].startswith("No shopify orders arrived for")
    assert report.decisions == []


def test_the_demo_shop_needs_a_person_at_most_twice_a_week() -> None:
    from autonomy.runtime.demo import DECIDE_HOUR, build_system

    days = 21
    system = build_system(days=days, customers=300, seed=0)
    zt = zero_touch_for_runtime(system.runtime, business_id="demo")
    install(system.runtime, zt)
    first = system.start.replace(hour=DECIDE_HOUR, minute=0, second=0, microsecond=0)
    for day in range(days):
        system.day(first + timedelta(days=day))
    end = first + timedelta(days=days)
    system.step_to(end)
    zt.tick(end)
    weekly = [w.appearances for w in zt.inbox.weekly_counts(first, end)]
    assert len(weekly) == 3 and max(weekly) <= 2, weekly
    assert all(f.level != "hold" for f in zt.watchdog.findings(end))  # type: ignore[union-attr]
    assert zt.guard is not None and all(e.actor != "autopilot" for e in zt.guard.ledger.entries())


@pytest.mark.parametrize("level", [AutonomyLevel.SHADOW, AutonomyLevel.APPROVE])
def test_a_goal_without_enough_closed_windows_is_not_offered(level: AutonomyLevel) -> None:
    w = world(level, agent=50, holdout=40)
    r = w.guard().readiness(GOAL, T0 + timedelta(days=30))
    assert r is not None and not r.ready
    assert any(c.name == "forecasts" and not c.ok for c in r.checks)


# --- review fixes -----------------------------------------------------------------------------------------


def _zero_touch(w: World, guard: AutopilotGuard) -> tuple[Inbox, ZeroTouch]:
    inbox = Inbox(InMemoryInboxStore(), BIZ)
    sources = InboxSources(
        business_id=BIZ, workspace=w.workspace, alerts=w.alerts, guard=guard, handled_at=inbox.handled_at
    )
    return inbox, ZeroTouch(inbox=inbox, sources=sources, guard=guard)


def test_resuming_a_goal_autopilot_dropped_and_its_breaker_paused_does_not_page_again() -> None:
    w = world(AutonomyLevel.AUTOPILOT, sent=True)
    guard = w.guard()
    inbox, zt = _zero_touch(w, guard)
    trip = T0 + timedelta(days=12)
    w.alerts.put([alert("breaker.unsubscribe_multiple", trip, auto_paused=True)])
    w.workspace.put_goal(w.workspace.goals()[0].model_copy(update={"status": GoalStatus.PAUSED}))
    zt.tick(trip + timedelta(minutes=5))  # the guard drops it to Approve while it is paused
    [item] = inbox.open_items()
    zt.act(item.item_id, trip + timedelta(minutes=10), by="owner")
    zt.tick(trip + timedelta(minutes=15))
    assert inbox.open_items() == []  # the click was the answer: no "dropped to Approve" item after it
    assert [(e.actor, e.reason) for e in w.ledger.entries()] == [
        ("autopilot", "safety_stop:breaker.unsubscribe_multiple"),
        ("person:owner", "resumed_in_approve"),
    ]
    # Resumed on the plan page instead (no inbox click): a person resumed it, so nothing waits either.
    w2 = world(AutonomyLevel.AUTOPILOT, sent=True)
    inbox2, zt2 = _zero_touch(w2, w2.guard())
    w2.alerts.put([alert("breaker.unsubscribe_multiple", trip, auto_paused=True)])
    w2.workspace.put_goal(w2.workspace.goals()[0].model_copy(update={"status": GoalStatus.PAUSED}))
    zt2.tick(trip + timedelta(minutes=5))
    w2.workspace.put_goal(w2.workspace.goals()[0].model_copy(update={"status": GoalStatus.ACTIVE}))
    zt2.tick(trip + timedelta(minutes=10))
    assert inbox2.open_items() == []


def test_a_step_back_to_approve_without_a_pause_has_a_button_that_closes_it() -> None:
    w = world(AutonomyLevel.AUTOPILOT, sent=True)
    guard = w.guard()
    inbox, zt = _zero_touch(w, guard)
    trip = T0 + timedelta(days=12)
    # A channel breaker: the goal keeps running, Autopilot drops to Approve.
    w.alerts.put([alert("breaker.delivery_rate_below", trip, auto_paused=True, message="Low. Paused sms.")])
    zt.tick(trip + timedelta(minutes=5))
    [item] = [i for i in inbox.open_items() if i.cause == f"safety_stop:{GOAL}"]
    assert item.button.action == "mark_handled" and item.button.label == "Keep in Approve"
    result = zt.act(item.item_id, trip + timedelta(minutes=10), by="owner")
    assert result.done and w.workspace.goals()[0].autonomy is AutonomyLevel.APPROVE
    assert w.ledger.entries()[-1].reason == "kept_in_approve"
    zt.tick(trip + timedelta(minutes=15))
    assert [i for i in inbox.open_items() if i.cause == f"safety_stop:{GOAL}"] == []


def test_approve_is_confirmed_only_when_the_whole_range_is_above_zero() -> None:
    # The point is above zero and the forecast inside the range, but the range reaches below zero.
    w = world(AutonomyLevel.APPROVE, sent=True, agent_buys_every=3, holdout_buys_every=4)
    r = w.guard().readiness(GOAL, T0 + timedelta(days=30))
    assert r is not None and r.observed is not None and r.observed.point > 0 > r.observed.low
    assert not r.ready and not next(c for c in r.checks if c.name == "proof").ok
    sure = world(AutonomyLevel.APPROVE, sent=True, agent_buys_every=2, holdout_buys_every=3)
    r2 = sure.guard().readiness(GOAL, T0 + timedelta(days=30))
    assert r2 is not None and r2.ready and "confirm it" in r2.sentence


def test_a_vouched_consent_jump_does_not_hold_again_as_the_share_moves_a_little() -> None:
    def states_with(contactable: int) -> InMemoryStateStore:
        states = InMemoryStateStore()
        reach = ChannelStats(address_known=True, consent=True)
        states.put(
            CustomerState(
                customer_id=f"c{i}",
                as_of=T0,
                channels={Channel.EMAIL: reach if i < contactable else ChannelStats(address_known=True)},
            )
            for i in range(400)
        )
        return states

    store = InMemoryWatchdogStore()
    store.put_snapshot(BIZ, (T0 - timedelta(days=7)).date(), "consent_coverage", 0.5)
    dog = DataWatchdog(BIZ, InMemoryEventStore(), states=states_with(378), store=store)
    [jump] = dog.findings(T0)
    dog.acknowledge(jump.fingerprint, T0, by="owner")
    assert dog.trusted(T0)
    dog.states = states_with(382)  # 94.5% -> 95.5%: another rounded fingerprint, the same jump
    assert dog.findings(T0 + timedelta(days=1)) == []
    dog.states = states_with(396)  # jumped further: a new finding, held again
    dog.invalidate()
    assert not dog.trusted(T0 + timedelta(days=2))


def test_the_production_api_does_not_read_or_resume_the_worker_s_channel_pauses() -> None:
    from types import SimpleNamespace

    from api.v2.inbox import production_inbox_services
    from tests.v2.zero_touch.wiring_support import make_shop

    shop = make_shop()
    services = SimpleNamespace(
        runtime=shop.runtime, business_id=shop.runtime.business_id, clock=lambda: T0, alerts=lambda: []
    )
    zt = production_inbox_services(services, engine=shop.db, wiring=shop.wiring).zero_touch
    assert zt.sources.paused_channels is None and zt.resume_channel is None


def test_install_joins_the_watchdog_to_a_data_gate_it_finds_without_a_monitor() -> None:
    from autonomy.runtime.service import DataHold

    events = InMemoryEventStore()
    events.append(orders(40, 40, skip=(38,)))
    dog = DataWatchdog(BIZ, events)
    runtime = SimpleRuntime(data_gate=lambda now: DataHold(reasons=("Shopify is stale.",)))
    zt = ZeroTouch(
        inbox=Inbox(InMemoryInboxStore(), BIZ), sources=InboxSources(BIZ, InMemoryWorkspace()), watchdog=dog
    )
    install(runtime, zt)
    install(runtime, zt)  # twice is once
    hold = runtime.data_gate(T0 + timedelta(days=40, hours=3))
    assert hold is not None and hold.reasons[0] == "Shopify is stale."
    assert len(hold.reasons) == 2 and hold.reasons[1].startswith("No shopify orders arrived for")
    assert runtime.before_tick == [zt.tick]


@dataclass
class SimpleRuntime:
    data_gate: Any = None
    before_tick: list[Any] | None = None

    def __post_init__(self) -> None:
        self.before_tick = []
