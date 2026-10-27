"""An untracked WhatsApp is valued from purchases, never from clicks (item 1); email and SMS are unchanged.

The WhatsApp Cloud API reports sent, delivered, read and failed, never a click. Before v4 a WhatsApp
send counted as a clickable send that never got a click, so its predicted value decayed toward zero.
With `never_click_channels=UNTRACKED_WHATSAPP` WhatsApp starts as a channel that logs no clicks: its lift
is per delivered message, measured against the holdout's purchases. By default no channel is forced
click-less (docs/v4/RESULTS.md: forcing it halved the Gym's whatsapp_fatigue value where clicks are
tracked); a channel whose sends show no click is found by `NO_CLICK_MIN_SENDS`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autonomy.contracts import (
    Audience,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    CustomerState,
    DecisionContext,
    DecisionKind,
    Envelope,
    Event,
    EventType,
    Objective,
    Theme,
)
from autonomy.data.folder import StateFolder
from autonomy.runtime.agent import (
    NEVER_CLICK_CHANNELS,
    UNTRACKED_WHATSAPP,
    AgentPolicy,
    ClickResponseModel,
    build_agent_policy,
)

START = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
NOW = START + timedelta(days=15)
GOAL = "goal_wa"


def brief(*channels: Channel) -> Brief:
    return Brief(
        brief_id="brief_wa",
        goal_id=GOAL,
        objective=Objective.GROSS_PROFIT,
        audience=Audience(description="everyone"),
        horizon_days=14,
        envelope=Envelope(channels=channels, budget=1e9, max_contacts_per_week=3),
        rationale="test",
        created_at=START - timedelta(days=30),
        approved_by="test",
        approved_at=START - timedelta(days=30),
    )


def content(channel: Channel) -> ContentItem:
    return ContentItem(
        content_id=f"msg_{channel.value}",
        goal_id=GOAL,
        channel=channel,
        theme=Theme.REMINDER,
        subject="Hello" if channel is Channel.EMAIL else None,
        body="Something new for you.",
        template_name="winback_1" if channel is Channel.WHATSAPP else None,
        status=ContentStatus.APPROVED,
        created_at=START - timedelta(days=30),
    )


def state(channel: Channel, cid: str = "c1") -> CustomerState:
    folder = StateFolder()
    st: CustomerState | None = None
    year_ago = START - timedelta(days=365)
    for e in (
        Event(event_id=f"su_{cid}", customer_id=cid, type=EventType.SIGNUP, ts=year_ago, source="t"),
        Event(
            event_id=f"co_{cid}",
            customer_id=cid,
            type=EventType.CONSENT_GRANTED,
            ts=year_ago,
            source="t",
            channel=channel,
        ),
        Event(
            event_id=f"p_{cid}",
            customer_id=cid,
            type=EventType.PURCHASE,
            ts=START - timedelta(days=60),
            source="t",
            value=50.0,
            margin=20.0,
        ),
    ):
        st = folder.fold(st, e)
    assert st is not None
    return folder.refresh(st, NOW)


def model_of(policy: AgentPolicy) -> ClickResponseModel:
    model = policy.bundle.response
    assert isinstance(model, ClickResponseModel)
    return model


def test_whatsapp_is_a_no_click_channel_from_the_first_send() -> None:
    policy = build_agent_policy(
        [brief(Channel.WHATSAPP)], [content(Channel.WHATSAPP)], never_click_channels=UNTRACKED_WHATSAPP
    )
    model = model_of(policy)
    assert not NEVER_CLICK_CHANNELS and "whatsapp" in UNTRACKED_WHATSAPP
    assert Channel.WHATSAPP.value in model.no_click
    assert not model.counts_clicks("whatsapp|none|reminder")
    # Email and SMS still count clicks until they have shown 200 deliveries and no click.
    assert model.counts_clicks("email|none|reminder")
    assert Channel.EMAIL.value not in model.no_click and Channel.SMS.value not in model.no_click


def test_a_click_reported_on_whatsapp_never_turns_the_click_model_on() -> None:
    model = model_of(
        build_agent_policy(
            [brief(Channel.WHATSAPP)], [content(Channel.WHATSAPP)], never_click_channels=UNTRACKED_WHATSAPP
        )
    )
    model.observe_click("c1", None, "whatsapp|none|reminder")
    assert Channel.WHATSAPP.value in model.no_click
    # An email click still clears a channel the model had taken to log no clicks.
    model.no_click.add(Channel.EMAIL.value)
    model.observe_click("c1", None, "email|none|reminder")
    assert Channel.EMAIL.value not in model.no_click


def test_whatsapp_value_is_per_message_not_per_click() -> None:
    policy = build_agent_policy(
        [brief(Channel.WHATSAPP, Channel.EMAIL)],
        [content(Channel.WHATSAPP)],
        never_click_channels=UNTRACKED_WHATSAPP,
    )
    model = model_of(policy)
    wa, email = "whatsapp|none|reminder", "email|none|reminder"
    wa_before, email_before = model._unit(None, wa), model._unit(None, email)
    assert wa_before[2:] == (1.0, 0.0)  # one unit per message, known exactly
    # 2,000 email sends and not one click: the email click rate falls toward zero and takes its value
    # with it; WhatsApp, valued per message, does not move.
    for i in range(2000):
        model.observe_send(f"s{i}", None, email)
    assert model._unit(None, wa) == wa_before
    assert model._unit(None, email)[2] < email_before[2] / 5


def decided(channel: Channel) -> tuple[AgentPolicy, DecisionContext]:
    policy = build_agent_policy(
        [brief(channel)],
        [content(channel)],
        costs={channel: 0.01},
        exploration_floor=0.0,
        never_click_channels=UNTRACKED_WHATSAPP,
    )
    policy.nightly([], {}, NOW)
    ctx = DecisionContext(now=NOW, briefs=(brief(channel),), content=(content(channel),), seed=1)
    policy.prepare_day([state(channel)], ctx)
    return policy, ctx


def test_the_learner_counts_a_delivered_whatsapp_message_not_a_click() -> None:
    results: dict[Channel, tuple[int, int, int]] = {}
    for channel in (Channel.WHATSAPP, Channel.EMAIL):
        policy, ctx = decided(channel)
        decision = policy.decide(state(channel), ctx)
        assert decision.kind is DecisionKind.ACT, channel
        events = [
            Event(
                event_id=f"{kind.value}_{channel.value}",
                customer_id="c1",
                type=kind,
                ts=NOW + timedelta(hours=hours),
                source="t",
                channel=channel,
                decision_id=decision.decision_id,
            )
            for kind, hours in ((EventType.DELIVER, 1), (EventType.CLICK, 2))
        ]
        policy.nightly([decision], {"c1": events}, NOW + timedelta(days=1))
        results[channel] = (
            len(policy.lift.click_times),
            len(policy.lift.send_times),
            len(policy.reading.per_customer),
        )
    # WhatsApp: one delivered message; the click (two hours late) feeds neither lift nor reading hours
    assert results[Channel.WHATSAPP] == (0, 1, 0)
    assert results[Channel.EMAIL] == (1, 0, 1)  # unchanged: a clicked message, a reading hour


def test_by_default_whatsapp_counts_clicks_like_any_other_channel() -> None:
    model = model_of(build_agent_policy([brief(Channel.WHATSAPP)], [content(Channel.WHATSAPP)]))
    assert Channel.WHATSAPP.value not in model.no_click
    assert model.counts_clicks("whatsapp|none|reminder")
