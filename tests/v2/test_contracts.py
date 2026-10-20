"""The shared shapes reject what they should and keep what they should."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from autonomy.contracts import (
    Action,
    Channel,
    Decision,
    DecisionKind,
    Envelope,
    Estimate,
    Event,
    EventType,
    Prior,
    PriorSource,
    stable_id,
)
from autonomy.fakes.samples import sample_brief, sample_content, sample_goal

NOW = datetime(2026, 10, 20, 10, tzinfo=UTC)


def test_outbound_events_need_a_channel() -> None:
    with pytest.raises(ValidationError):
        Event(event_id="e1", customer_id="c1", type=EventType.SEND, ts=NOW, source="t")


def test_purchases_need_a_value() -> None:
    with pytest.raises(ValidationError):
        Event(event_id="e1", customer_id="c1", type=EventType.PURCHASE, ts=NOW, source="t")


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValidationError):
        Event(event_id="e1", customer_id="c1", type=EventType.VISIT, ts=datetime(2026, 1, 1), source="t")


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        Event(event_id="e1", customer_id="c1", type=EventType.VISIT, ts=NOW, source="t", colour="red")  # type: ignore[call-arg]


def test_decision_kind_and_action_agree() -> None:
    action = Action(goal_id="g", channel=Channel.EMAIL, send_at=NOW)
    with pytest.raises(ValidationError):
        Decision(
            decision_id="d",
            customer_id="c",
            made_at=NOW,
            kind=DecisionKind.NOTHING,
            action=action,
            probability=1,
            reason="x",
            policy_version="v",
        )
    with pytest.raises(ValidationError):
        Decision(
            decision_id="d",
            customer_id="c",
            made_at=NOW,
            kind=DecisionKind.ACT,
            probability=1,
            reason="x",
            policy_version="v",
        )
    with pytest.raises(ValidationError):
        Decision(
            decision_id="d",
            customer_id="c",
            made_at=NOW,
            kind=DecisionKind.NOTHING,
            probability=0,
            reason="x",
            policy_version="v",
        )


def test_envelope_leaves_room_for_the_agent() -> None:
    with pytest.raises(ValidationError):
        Envelope(channels=(Channel.EMAIL,), budget=1, holdout_share=0.5, baseline_share=0.5)
    with pytest.raises(ValidationError):
        Envelope(channels=(), budget=1)


def test_client_priors_need_five_clients() -> None:
    with pytest.raises(ValidationError):
        Prior(key="k", source=PriorSource.CLIENTS, n_clients=4, values={"a": 1.0}, updated_at=NOW)
    Prior(key="k", source=PriorSource.GYM, values={"a": 1.0}, updated_at=NOW)


def test_estimates_are_ordered() -> None:
    with pytest.raises(ValidationError):
        Estimate(point=1, low=2, high=3)


def test_samples_are_valid_and_round_trip() -> None:
    goal, brief, content = sample_goal(NOW), sample_brief(NOW), sample_content(NOW)
    assert brief.approved and goal.goal_id == brief.goal_id
    assert type(brief).model_validate_json(brief.model_dump_json()) == brief
    assert {c.channel for c in content} == {Channel.EMAIL, Channel.WHATSAPP}


def test_stable_ids_repeat() -> None:
    assert stable_id("x", 1, "a") == stable_id("x", 1, "a") != stable_id("x", 1, "b")
