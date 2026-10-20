"""Rendering, the dispatcher and the circuit breakers, against fake adapters and in-memory stores."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.channels import (
    Dispatcher,
    DispatchStatus,
    Guards,
    RenderError,
    SkipReason,
    evaluate,
    evaluate_goals,
    render,
)
from autonomy.channels.breakers import split_by_goal
from autonomy.channels.dispatcher import SEND_JOB_KIND, message_id_for
from autonomy.contracts import (
    Action,
    AutonomyLevel,
    Channel,
    ChannelStats,
    ContentItem,
    ContentStatus,
    CustomerState,
    Decision,
    DecisionKind,
    Event,
    EventType,
    GoalStatus,
    Group,
    Message,
    SendReceipt,
    StopMetric,
    StopRule,
    Theme,
)
from autonomy.fakes import (
    FakeChannelAdapter,
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryJobQueue,
    InMemoryStateStore,
    InMemoryWorkspace,
)
from autonomy.fakes.samples import TEN_OFF, sample_brief, sample_content, sample_goal

NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
GOAL = "goal_winback"

# sample_content: ci_0 email reminder (no offer), ci_1 email 10% off, ci_2 email education,
# ci_3 WhatsApp reminder template, ci_4 WhatsApp free shipping template.


def state(cid: str = "c1", **kw: object) -> CustomerState:
    base: dict[str, object] = {
        "customer_id": cid,
        "as_of": NOW,
        "top_categories": ("trail shoes",),
        "channels": {
            Channel.EMAIL: ChannelStats(address_known=True, consent=True),
            Channel.WHATSAPP: ChannelStats(address_known=True, consent=True),
        },
    }
    base.update(kw)
    return CustomerState.model_validate(base)


def decision(
    cid: str = "c1",
    *,
    channel: Channel = Channel.EMAIL,
    content_id: str | None = "ci_0",
    offer_id: str = "none",
    send_at: datetime = NOW,
    group: Group = Group.AGENT,
    goal_id: str = GOAL,
    did: str | None = None,
) -> Decision:
    return Decision(
        decision_id=did or f"dec_{cid}_{channel.value}_{content_id}",
        customer_id=cid,
        made_at=NOW,
        kind=DecisionKind.ACT,
        action=Action(
            goal_id=goal_id, channel=channel, offer_id=offer_id, content_id=content_id, send_at=send_at
        ),
        group=group,
        probability=0.9,
        reason="test",
        policy_version="t",
    )


def nothing(cid: str = "c1") -> Decision:
    return Decision(
        decision_id=f"dec_{cid}_nothing",
        customer_id=cid,
        made_at=NOW,
        kind=DecisionKind.NOTHING,
        probability=1.0,
        reason="test",
        policy_version="t",
    )


def address(cid: str, channel: Channel) -> str | None:
    if cid == "c_noaddr":
        return None
    return f"{cid}@example.test" if channel is Channel.EMAIL else "+15550001111"


class World:
    def __init__(self, *, autonomy: AutonomyLevel = AutonomyLevel.AUTOPILOT, queue: bool = False) -> None:
        self.ws = InMemoryWorkspace()
        self.ws.put_goal(
            sample_goal(NOW).model_copy(update={"status": GoalStatus.ACTIVE, "autonomy": autonomy})
        )
        self.ws.put_brief(sample_brief(NOW))
        self.ws.put_content(sample_content(NOW))
        self.events = InMemoryEventStore()
        self.queue = InMemoryJobQueue() if queue else None
        self.email = FakeChannelAdapter(Channel.EMAIL)
        self.whatsapp = FakeChannelAdapter(Channel.WHATSAPP)
        self.profiles: dict[str, Mapping[str, str]] = {"c1": {"first_name": "Priya"}}
        self.dispatcher = self.make()

    def make(self) -> Dispatcher:
        return Dispatcher(
            {Channel.EMAIL: self.email, Channel.WHATSAPP: self.whatsapp},
            address,
            self.ws,
            self.events,
            self.queue,
            profiles=lambda cid: self.profiles.get(cid, {}),
        )


def item(body: str, *, subject: str | None = None, template: str | None = None) -> ContentItem:
    return ContentItem(
        content_id="ci_t",
        channel=Channel.WHATSAPP if template else Channel.EMAIL,
        theme=Theme.REMINDER,
        subject=subject,
        body=body,
        template_name=template,
        status=ContentStatus.APPROVED,
        created_at=NOW,
    )


# --- render ---------------------------------------------------------------------------------------


def test_render_uses_safe_fallbacks() -> None:
    out = render(
        item("New in {{top_category}}.", subject="Hi {{ first_name }}"),
        state(top_categories=()),
        {},
        None,
        NOW,
    )
    assert out.subject == "Hi there"
    assert out.body == "New in our latest picks."
    assert out.template_params == ()


def test_render_cleans_values_but_keeps_content_text() -> None:
    profile = {"first_name": "Pri\x00ya\n\t Rao"}
    out = render(item("Hi {{first_name}},\nnew in {{top_category}}"), state(), profile, None, NOW)
    assert out.body == "Hi Priya Rao,\nnew in trail shoes"
    assert render(item("{{first_name}}"), state(), {"name": "Ana Lima"}, None, NOW).body == "Ana"


def test_render_refuses_unknown_and_malformed_slots() -> None:
    with pytest.raises(RenderError, match="unknown slot"):
        render(item("Hi {{nickname}}"), state(), {}, None, NOW)
    with pytest.raises(RenderError, match="malformed"):
        render(item("Hi {{first_name}"), state(), {}, None, NOW)


def test_offer_label_needs_a_real_offer() -> None:
    with pytest.raises(RenderError, match="offer"):
        render(item("Take {{offer_label}}"), state(), {}, None, NOW)
    assert render(item("Take {{offer_label}}"), state(), {}, TEN_OFF, NOW).body == "Take 10% off"


def test_whatsapp_params_follow_body_order_and_weekday_uses_local_time() -> None:
    wa = item("Hi {{first_name}}, {{top_category}} for {{first_name}} this {{weekday}}", template="t1")
    out = render(wa, state(timezone="Pacific/Auckland"), {"first_name": "Mo<b>"}, None, NOW)
    # 10:00 UTC on Tuesday is 23:00 Tuesday in Auckland (UTC+13 in October).
    assert out.template_params == ("Mo<b>", "trail shoes", "Tuesday")
    late = NOW.replace(hour=12)
    assert render(wa, state(timezone="Pacific/Auckland"), {}, None, late).template_params[-1] == "Wednesday"


# --- dispatcher -----------------------------------------------------------------------------------


def test_sends_once_and_logs_a_send_event_with_the_decision() -> None:
    w = World()
    d = decision()
    report = w.dispatcher.dispatch([d, nothing()], {"c1": state()}, NOW)
    assert [r.status for r in report.results] == [DispatchStatus.SENT]
    assert report.ignored == 1
    [msg] = w.email.sent
    assert msg.message_id == message_id_for(d.decision_id)
    assert (msg.to, msg.subject, msg.body) == (
        "c1@example.test",
        "We miss you, Priya",
        "New in trail shoes since your last visit.",
    )
    [ev] = w.events.for_decision(d.decision_id)
    assert ev.type is EventType.SEND and ev.channel is Channel.EMAIL and ev.ts == NOW
    assert ev.attrs["goal_id"] == GOAL and ev.attrs["content_id"] == "ci_0"
    assert report.events == [ev]

    again = w.dispatcher.dispatch([d], {"c1": state()}, NOW + timedelta(minutes=5))
    fresh = w.make().dispatch([d, d], {"c1": state()}, NOW + timedelta(hours=1))
    assert again.skip_reasons() == {SkipReason.ALREADY_SENT.value: 1}
    assert fresh.skip_reasons() == {SkipReason.ALREADY_SENT.value: 1, SkipReason.DUPLICATE_IN_BATCH.value: 1}
    assert len(w.email.sent) == 1


def test_whatsapp_template_and_offer() -> None:
    w = World()
    d = decision(channel=Channel.WHATSAPP, content_id="ci_4", offer_id="freeship")
    report = w.dispatcher.dispatch([d], {"c1": state()}, NOW)
    assert report.results[0].status is DispatchStatus.SENT
    [msg] = w.whatsapp.sent
    assert msg.template_name == "winback_4" and msg.template_params == ("Priya",)
    assert msg.to == "+15550001111"


@pytest.mark.parametrize(
    ("d", "st", "reason"),
    [
        (decision(group=Group.HOLDOUT), state(), SkipReason.HOLDOUT),
        (decision(), state(group=Group.HOLDOUT), SkipReason.HOLDOUT),
        (decision(goal_id="goal_other"), state(), SkipReason.UNKNOWN_GOAL),
        (decision(channel=Channel.SMS), state(), SkipReason.NO_ADAPTER),
        (decision("c_noaddr"), state("c_noaddr"), SkipReason.NO_ADDRESS),
        (
            decision(),
            state(
                channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True, unsubscribed=True)}
            ),
            SkipReason.NOT_CONTACTABLE,
        ),
        (decision(content_id=None), state(), SkipReason.NO_CONTENT),
        (decision(content_id="ci_missing"), state(), SkipReason.CONTENT_NOT_FOUND),
        (decision(content_id="ci_3"), state(), SkipReason.CONTENT_MISMATCH),
        (decision(content_id="ci_1"), state(), SkipReason.CONTENT_MISMATCH),
        (decision(content_id="ci_0", offer_id="bogo"), state(), SkipReason.CONTENT_MISMATCH),
    ],
)
def test_skips_are_reported_with_a_reason(d: Decision, st: CustomerState, reason: SkipReason) -> None:
    w = World()
    report = w.dispatcher.dispatch([d], {st.customer_id: st}, NOW)
    assert report.skip_reasons() == {reason.value: 1}
    assert not w.email.sent and not w.whatsapp.sent and len(w.events) == 0


def test_missing_state_offer_outside_envelope_unapproved_content_and_bad_render() -> None:
    w = World()
    assert w.dispatcher.dispatch([decision()], {}, NOW).skip_reasons() == {SkipReason.NO_STATE.value: 1}
    bad_offer = sample_content(NOW)[1].model_copy(update={"content_id": "ci_x", "offer_id": "bogo"})
    draft = sample_content(NOW)[2].model_copy(update={"content_id": "ci_d", "status": ContentStatus.DRAFT})
    broken = sample_content(NOW)[2].model_copy(update={"content_id": "ci_b", "body": "Hi {{nickname}}"})
    w.ws.put_content([bad_offer, draft, broken])
    report = w.dispatcher.dispatch(
        [
            decision(content_id="ci_x", offer_id="bogo"),
            decision(content_id="ci_d"),
            decision(content_id="ci_b"),
        ],
        {"c1": state()},
        NOW,
    )
    assert [r.reason for r in report.results] == [
        SkipReason.OFFER_NOT_ALLOWED.value,
        SkipReason.CONTENT_NOT_APPROVED.value,
        SkipReason.RENDER_FAILED.value,
    ]
    assert "nickname" in report.results[2].detail


def test_shadow_paused_and_inactive_goals_send_nothing() -> None:
    shadow = World(autonomy=AutonomyLevel.SHADOW)
    assert shadow.dispatcher.dispatch([decision()], {"c1": state()}, NOW).skip_reasons() == {
        SkipReason.SHADOW_MODE.value: 1
    }
    w = World()
    w.dispatcher.pause(goals=[GOAL])
    assert w.dispatcher.dispatch([decision()], {"c1": state()}, NOW).skip_reasons() == {
        SkipReason.PAUSED_GOAL.value: 1
    }
    w.dispatcher.resume(goals=[GOAL])
    w.dispatcher.pause(channels=[Channel.EMAIL])
    report = w.dispatcher.dispatch(
        [decision(), decision(channel=Channel.WHATSAPP, content_id="ci_3")], {"c1": state()}, NOW
    )
    assert [r.status for r in report.results] == [DispatchStatus.SKIPPED, DispatchStatus.SENT]
    w.ws.put_goal(w.ws.goals()[0].model_copy(update={"status": GoalStatus.PAUSED}))
    assert w.dispatcher.dispatch([decision(cid="c2")], {"c2": state("c2")}, NOW).skip_reasons() == {
        SkipReason.GOAL_NOT_ACTIVE.value: 1
    }


def test_future_send_is_queued_then_sent_once_by_the_job() -> None:
    w = World(queue=True)
    assert w.queue is not None
    later = NOW + timedelta(hours=3)
    d = decision(send_at=later)
    log, states = InMemoryDecisionLog(), InMemoryStateStore()
    log.record([d])
    states.put([state()])

    first = w.dispatcher.dispatch([d], {"c1": state()}, NOW)
    second = w.dispatcher.dispatch([d], {"c1": state()}, NOW + timedelta(minutes=1))
    assert [r.status for r in first.results + second.results] == [DispatchStatus.QUEUED] * 2
    assert not w.email.sent
    assert w.queue.claim([SEND_JOB_KIND], worker="w", now=NOW) is None
    job = w.queue.claim([SEND_JOB_KIND], worker="w", now=later)
    assert job is not None and job.payload["decision_id"] == d.decision_id
    assert w.queue.claim([SEND_JOB_KIND], worker="w", now=later) is None  # one job, not two

    done = w.dispatcher.run_send_job(job, log, states, later)
    assert [r.status for r in done.results] == [DispatchStatus.SENT]
    [msg] = w.email.sent
    assert msg.send_at == later
    assert w.dispatcher.run_send_job(job, log, states, later).skip_reasons() == {
        SkipReason.ALREADY_SENT.value: 1
    }


def test_job_rechecks_consent_and_missing_decisions() -> None:
    w = World(queue=True)
    assert w.queue is not None
    later = NOW + timedelta(hours=3)
    d = decision(send_at=later)
    log, states = InMemoryDecisionLog(), InMemoryStateStore()
    log.record([d])
    w.dispatcher.dispatch([d], {"c1": state()}, NOW)
    gone = ChannelStats(address_known=True, consent=True, unsubscribed=True)
    states.put([state(channels={Channel.EMAIL: gone})])
    job = w.queue.claim([SEND_JOB_KIND], worker="w", now=later)
    assert job is not None
    assert w.dispatcher.run_send_job(job, log, states, later).skip_reasons() == {
        SkipReason.NOT_CONTACTABLE.value: 1
    }
    orphan = job.model_copy(update={"payload": {"decision_id": "dec_unknown"}})
    assert w.dispatcher.run_send_job(orphan, log, states, later).skip_reasons() == {
        SkipReason.DECISION_NOT_FOUND.value: 1
    }
    assert not w.email.sent


def test_future_send_without_a_queue_goes_now_with_its_send_at() -> None:
    w = World()
    later = NOW + timedelta(hours=3)
    report = w.dispatcher.dispatch([decision(send_at=later)], {"c1": state()}, NOW)
    assert report.results[0].status is DispatchStatus.SENT
    assert w.email.sent[0].send_at == later


class FlakyAdapter(FakeChannelAdapter):
    def __init__(self) -> None:
        super().__init__(Channel.EMAIL)
        self.mode = "reject"

    def send(self, message: Message) -> SendReceipt:
        if self.mode == "reject":
            return SendReceipt(accepted=False, error="throttled")
        if self.mode == "raise":
            raise ConnectionError("network down")
        return super().send(message)


def test_failures_are_logged_and_retry_later() -> None:
    w = World()
    flaky = FlakyAdapter()
    w.email = flaky
    w.dispatcher = w.make()
    d = decision()
    rejected = w.dispatcher.dispatch([d], {"c1": state()}, NOW)
    assert rejected.results[0].status is DispatchStatus.FAILED and rejected.results[0].reason == "throttled"
    flaky.mode = "raise"
    raised = w.dispatcher.dispatch([d], {"c1": state()}, NOW + timedelta(minutes=5))
    assert "network down" in raised.results[0].reason
    fails = [e for e in w.events.for_decision(d.decision_id) if e.type is EventType.FAIL]
    assert len(fails) == 2 and fails[0].attrs["error"] == "throttled"
    flaky.mode = "ok"
    ok = w.dispatcher.dispatch([d], {"c1": state()}, NOW + timedelta(minutes=10))
    assert ok.results[0].status is DispatchStatus.SENT and len(flaky.sent) == 1


# --- breakers -------------------------------------------------------------------------------------


def outbound(
    n: int,
    kind: EventType,
    *,
    channel: Channel = Channel.EMAIL,
    at: datetime = NOW - timedelta(hours=6),
    goal: str = GOAL,
    start: int = 0,
) -> list[Event]:
    return [
        Event(
            event_id=f"ev_{channel.value}_{kind.value}_{goal}_{i}",
            customer_id=f"c{i}",
            type=kind,
            ts=at,
            source="test",
            channel=channel,
            decision_id=f"d_{goal}_{channel.value}_{i}",
            attrs={"goal_id": goal} if kind is EventType.SEND else {},
        )
        for i in range(start, start + n)
    ]


def delivered(n: int, **kw: object) -> list[Event]:
    return outbound(n, EventType.SEND, **kw) + outbound(n, EventType.DELIVER, **kw)  # type: ignore[arg-type]


def rule(metric: StopMetric, threshold: float, action: str = "pause_goal") -> StopRule:
    return StopRule(metric=metric, threshold=threshold, action=action)  # type: ignore[arg-type]


def test_complaint_rate_trips_with_volume_but_not_on_tiny_samples() -> None:
    r = [rule(StopMetric.COMPLAINT_RATE, 0.0008)]
    harm = delivered(300) + outbound(3, EventType.COMPLAINT)
    res = evaluate(r, harm, {}, 0, 0, NOW, goal_id=GOAL)
    assert res.paused_goals == {GOAL} and res.paused_channels == set()
    [alert] = res.alerts
    assert alert.code == "breaker.complaint_rate" and alert.auto_paused and alert.severity == "critical"
    assert "1.00%" in alert.message and "last batch" in alert.message

    tiny = delivered(20) + outbound(2, EventType.COMPLAINT)
    single = delivered(5000) + outbound(1, EventType.BLOCK)
    assert evaluate(r, tiny, {}, 0, 0, NOW, goal_id=GOAL).alerts == []
    assert evaluate(r, single, {}, 0, 0, NOW, goal_id=GOAL).alerts == []
    old = delivered(300, at=NOW - timedelta(days=8)) + outbound(
        3, EventType.COMPLAINT, at=NOW - timedelta(days=8)
    )
    assert evaluate(r, old, {}, 0, 0, NOW, goal_id=GOAL).alerts == []


def test_slow_trickle_trips_the_seven_day_window() -> None:
    r = [rule(StopMetric.COMPLAINT_RATE, 0.0008)]
    events: list[Event] = []
    for day in range(1, 7):
        at = NOW - timedelta(days=day, hours=1)
        events += delivered(60, at=at, start=day * 1000)
    events += outbound(1, EventType.COMPLAINT, at=NOW - timedelta(days=2)) + outbound(
        1, EventType.COMPLAINT, at=NOW - timedelta(days=4), start=5
    )
    res = evaluate(r, events, {}, 0, 0, NOW, goal_id=GOAL)
    assert len(res.alerts) == 1 and "7 days" in res.alerts[0].message


def test_unsubscribe_multiple_needs_a_baseline() -> None:
    r = [rule(StopMetric.UNSUBSCRIBE_MULTIPLE, 3.0)]
    events = delivered(500) + outbound(8, EventType.UNSUBSCRIBE)  # 1.6% vs a 0.4% baseline = 4x
    assert evaluate(r, events, {}, 0, 0, NOW, goal_id=GOAL).alerts == []
    res = evaluate(r, events, {"unsubscribe_rate": 0.004}, 0, 0, NOW, goal_id=GOAL)
    assert res.paused_goals == {GOAL} and "4.0x" in res.alerts[0].message
    per_channel = {"unsubscribe_rate": 0.004, "unsubscribe_rate/email": 0.01}
    assert evaluate(r, events, per_channel, 0, 0, NOW, goal_id=GOAL).alerts == []


def test_delivery_rate_below_pauses_the_channel() -> None:
    r = [rule(StopMetric.DELIVERY_RATE_BELOW, 0.95, "pause_channel")]
    events = (
        outbound(200, EventType.SEND)
        + outbound(180, EventType.DELIVER)
        + outbound(5, EventType.BOUNCE, start=190)
    )
    res = evaluate(r, events, {}, 0, 0, NOW, goal_id=GOAL)
    assert res.paused_channels == {Channel.EMAIL} and res.paused_goals == set()
    assert "90.00%" in res.alerts[0].message

    young = outbound(200, EventType.SEND, at=NOW - timedelta(minutes=10)) + outbound(
        1, EventType.DELIVER, at=NOW - timedelta(minutes=5)
    )
    assert evaluate(r, young, {}, 0, 0, NOW, goal_id=GOAL).alerts == []
    no_receipts = outbound(200, EventType.SEND, channel=Channel.WEBHOOK)
    assert evaluate(r, no_receipts, {}, 0, 0, NOW, goal_id=GOAL).alerts == []


def test_spend_pace_and_its_guard() -> None:
    r = [rule(StopMetric.SPEND_PACE, 1.2)]
    assert evaluate(r, [], {}, 130.0, 100.0, NOW, goal_id=GOAL).paused_goals == {GOAL}
    assert evaluate(r, [], {}, 110.0, 100.0, NOW, goal_id=GOAL).alerts == []
    assert evaluate(r, [], {}, 10.0, 5.0, NOW, goal_id=GOAL).alerts == []
    assert evaluate(r, [], {}, 40.0, 0.0, NOW, goal_id=GOAL).paused_goals == {GOAL}
    # Programme-wide spend has neither a goal nor a channel to pause: alert, but no auto-pause.
    [alert] = evaluate(r, [], {}, 130.0, 100.0, NOW).alerts
    assert not alert.auto_paused


def quality(level: str, at: datetime) -> Event:
    return Event(
        event_id=f"ev_quality_{level}_{at.isoformat()}",
        customer_id="account",
        type=EventType.DELIVER,
        ts=at,
        source="whatsapp",
        channel=Channel.WHATSAPP,
        attrs={"quality_rating": level},
    )


def test_whatsapp_quality_uses_the_latest_rating() -> None:
    r = [rule(StopMetric.WHATSAPP_QUALITY, 1, "pause_channel")]
    yellow = [quality("GREEN", NOW - timedelta(days=20)), quality("YELLOW", NOW - timedelta(days=10))]
    res = evaluate(r, yellow, {}, 0, 0, NOW, goal_id=GOAL)
    assert res.paused_channels == {Channel.WHATSAPP} and "yellow" in res.alerts[0].message
    recovered = [*yellow, quality("GREEN", NOW - timedelta(days=1))]
    assert evaluate(r, recovered, {}, 0, 0, NOW, goal_id=GOAL).alerts == []
    assert evaluate([rule(StopMetric.WHATSAPP_QUALITY, 2)], yellow, {}, 0, 0, NOW).alerts == []


def test_pause_goal_without_a_goal_pauses_the_channel() -> None:
    harm = delivered(300) + outbound(3, EventType.COMPLAINT)
    res = evaluate([rule(StopMetric.COMPLAINT_RATE, 0.0008)], harm, {}, 0, 0, NOW)
    assert res.paused_goals == set() and res.paused_channels == {Channel.EMAIL}


def test_evaluate_goals_splits_events_by_goal_and_feeds_the_dispatcher() -> None:
    brief_a = sample_brief(NOW, GOAL)
    brief_b = sample_brief(NOW, "goal_quiet")
    harm = delivered(300) + outbound(3, EventType.COMPLAINT)
    calm = delivered(300, goal="goal_quiet")
    shared = [quality("GREEN", NOW - timedelta(days=1))]
    split = split_by_goal(harm + calm + shared)
    assert len(split[GOAL]) == 604 and len(split["goal_quiet"]) == 601
    res = evaluate_goals([brief_a, brief_b], harm + calm + shared, {}, {}, {}, NOW, guards=Guards())
    assert res.paused_goals == {GOAL} and [a.goal_id for a in res.alerts] == [GOAL]

    w = World()
    w.dispatcher.pause(goals=res.paused_goals, channels=res.paused_channels)
    assert w.dispatcher.dispatch([decision()], {"c1": state()}, NOW).skip_reasons() == {
        SkipReason.PAUSED_GOAL.value: 1
    }


# --- review fixes ---------------------------------------------------------------------------------


def test_envelope_channel_brief_and_goal_dates_are_enforced() -> None:
    w = World()
    brief = sample_brief(NOW)
    email_only = brief.model_copy(
        update={"version": 2, "envelope": brief.envelope.model_copy(update={"channels": (Channel.EMAIL,)})}
    )
    w.ws.put_brief(email_only)
    wa = decision(channel=Channel.WHATSAPP, content_id="ci_3")
    assert w.dispatcher.dispatch([wa], {"c1": state()}, NOW).skip_reasons() == {
        SkipReason.CHANNEL_NOT_ALLOWED.value: 1
    }

    goal = w.ws.goals()[0]
    w.ws.put_goal(goal.model_copy(update={"ends_at": NOW}))
    assert w.dispatcher.dispatch([decision()], {"c1": state()}, NOW).skip_reasons() == {
        SkipReason.GOAL_NOT_ACTIVE.value: 1
    }
    w.ws.put_goal(goal.model_copy(update={"starts_at": NOW + timedelta(days=1)}))
    assert w.dispatcher.dispatch([decision()], {"c1": state()}, NOW).skip_reasons() == {
        SkipReason.GOAL_NOT_ACTIVE.value: 1
    }

    bare = World()
    bare.ws = InMemoryWorkspace()
    bare.ws.put_goal(w.ws.goals()[0].model_copy(update={"starts_at": NOW, "ends_at": None}))
    bare.ws.put_content(sample_content(NOW))
    bare.dispatcher = bare.make()
    assert bare.dispatcher.dispatch([decision()], {"c1": state()}, NOW).skip_reasons() == {
        SkipReason.NO_BRIEF.value: 1
    }
    assert not w.email.sent and not w.whatsapp.sent and not bare.email.sent


def fails(n: int, *, channel: Channel, start: int = 0) -> list[Event]:
    return [
        Event(
            event_id=f"ev_fail_{channel.value}_{i}",
            customer_id=f"c{i}",
            type=EventType.FAIL,
            ts=NOW - timedelta(hours=6),
            source="dispatcher",
            channel=channel,
            decision_id=f"d_{GOAL}_{channel.value}_{i}",
        )
        for i in range(start, start + n)
    ]


def test_delivery_rate_on_channels_without_delivery_receipts() -> None:
    r = [rule(StopMetric.DELIVERY_RATE_BELOW, 0.95, "pause_channel")]
    # One retried dispatcher failure on a webhook channel is not a delivery problem.
    webhook = outbound(200, EventType.SEND, channel=Channel.WEBHOOK) + fails(1, channel=Channel.WEBHOOK)
    assert evaluate(r, webhook, {}, 0, 0, NOW, goal_id=GOAL).alerts == []
    # A client endpoint that is down (every attempt fails) still trips.
    dead = fails(150, channel=Channel.WEBHOOK)
    assert evaluate(r, dead, {}, 0, 0, NOW, goal_id=GOAL).paused_channels == {Channel.WEBHOOK}
    # SES with bounce notices only: judged on sends minus bounces, not as "nothing delivered".
    few_bounces = outbound(200, EventType.SEND) + outbound(4, EventType.BOUNCE)
    assert evaluate(r, few_bounces, {}, 0, 0, NOW, goal_id=GOAL).alerts == []
    many_bounces = outbound(200, EventType.SEND) + outbound(30, EventType.BOUNCE)
    assert evaluate(r, many_bounces, {}, 0, 0, NOW, goal_id=GOAL).paused_channels == {Channel.EMAIL}


def test_a_goal_without_its_own_events_is_not_judged_on_unattributed_ones() -> None:
    # Complaints and deliveries whose SEND fell outside the window cannot be tied to a goal.
    orphans = [e for e in delivered(300, goal="goal_gone") if e.type is EventType.DELIVER] + outbound(
        3, EventType.COMPLAINT, goal="goal_gone"
    )
    res = evaluate_goals([sample_brief(NOW, "goal_quiet")], orphans, {}, {}, {}, NOW)
    assert res.alerts == [] and res.paused_goals == set()
