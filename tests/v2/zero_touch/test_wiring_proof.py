"""Proof does not count a hand-off the customer's tool never delivered as sent, and stays intent-to-treat."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autonomy.channels.klaviyo import NOT_DELIVERED
from autonomy.contracts import Action, Channel, Decision, DecisionKind, Event, EventType, Group, Objective
from autonomy.proof.rewards import Costs, build_rewards, proof_rewards, sent_decisions, undelivered_decisions

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
COSTS = Costs(channel={Channel.EMAIL: 0.5})


def decision(did: str, cid: str, group: Group = Group.AGENT) -> Decision:
    return Decision(
        decision_id=did,
        customer_id=cid,
        made_at=T0,
        kind=DecisionKind.ACT,
        action=Action(goal_id="g", channel=Channel.EMAIL, offer_id="none", content_id="c", send_at=T0),
        group=group,
        probability=1.0,
        reason="test",
        policy_version="v",
    )


def ev(eid: str, cid: str, etype: EventType, at: datetime, did: str | None = None, **attrs: object) -> Event:
    return Event(
        event_id=eid,
        customer_id=cid,
        type=etype,
        ts=at,
        source="test",
        channel=Channel.EMAIL if etype is not EventType.PURCHASE else None,
        decision_id=did,
        value=30.0 if etype is EventType.PURCHASE else None,
        attrs=dict(attrs),  # type: ignore[arg-type]
    )


def test_a_hand_off_the_tool_never_delivered_costs_nothing_but_keeps_its_group_and_its_outcome() -> None:
    events = {
        "a": [
            ev("s1", "a", EventType.SEND, T0, "d1", provider_message_id="klaviyo:c:d1"),
            ev("f1", "a", EventType.FAIL, T0 + timedelta(hours=6), "d1", reason=NOT_DELIVERED),
            ev("p1", "a", EventType.PURCHASE, T0 + timedelta(days=2)),
        ],
        "b": [
            ev("s2", "b", EventType.SEND, T0, "d2"),
            ev("r2", "b", EventType.DELIVER, T0 + timedelta(minutes=2), "d2"),
        ],
        "c": [  # a provider error that a retry got past is sent
            ev("f3", "c", EventType.FAIL, T0, "d3", error="ambiguous: timeout"),
            ev("s3", "c", EventType.SEND, T0 + timedelta(minutes=2), "d3"),
        ],
    }
    flat = [e for evs in events.values() for e in evs]
    assert undelivered_decisions(flat) == {"d1"}
    assert sent_decisions(flat) == {"d2", "d3"}
    decided = [decision("d1", "a"), decision("d2", "b"), decision("d3", "c")]
    now = T0 + timedelta(days=30)
    rewards = {
        r.decision_id: r for r in build_rewards(decided, events, 14, Objective.REVENUE, COSTS, now=now)
    }
    assert rewards["d1"].cost == 0.0 and rewards["d2"].cost == 0.5 and rewards["d3"].cost == 0.5
    # Intent to treat: the undelivered decision is still judged, with the purchase in its window.
    assert rewards["d1"].converted and rewards["d1"].revenue == 30.0
    proof = {r.decision_id: r for r in proof_rewards(decided, events, 14, Objective.REVENUE, COSTS, now=now)}
    assert set(proof) == {"d1", "d2", "d3"} and proof["d1"].net == 30.0
    assert [d.group for d in decided] == [Group.AGENT] * 3


def test_a_late_delivery_wins_over_a_not_delivered_verdict() -> None:
    flat = [
        ev("s1", "a", EventType.SEND, T0, "d1"),
        ev("f1", "a", EventType.FAIL, T0 + timedelta(hours=6), "d1", reason=NOT_DELIVERED),
        ev("r1", "a", EventType.DELIVER, T0 + timedelta(hours=7), "d1"),
    ]
    assert undelivered_decisions(flat) == frozenset() and sent_decisions(flat) == {"d1"}


def test_a_bounced_message_was_sent_and_keeps_its_cost() -> None:
    events = {
        "a": [
            ev("s1", "a", EventType.SEND, T0, "d1"),
            ev("b1", "a", EventType.BOUNCE, T0 + timedelta(minutes=1), "d1"),
        ]
    }
    flat = events["a"]
    assert undelivered_decisions(flat) == {"d1"}  # it did not reach the person (what learning reads)
    assert sent_decisions(flat) == {"d1"}  # but the provider sent and billed it
    rewards = build_rewards(
        [decision("d1", "a")], events, 14, Objective.REVENUE, COSTS, now=T0 + timedelta(days=30)
    )
    assert rewards[0].cost == 0.5 and rewards[0].net == -0.5
