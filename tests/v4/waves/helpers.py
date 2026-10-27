"""Small helpers for the waves tests."""

from __future__ import annotations

from datetime import timedelta

from autonomy.contracts import Action, Channel, Decision, DecisionKind, Event, EventType, stable_id
from autonomy.gym.waves.config import get_world
from autonomy.gym.waves.data import WavesData
from autonomy.gym.waves.setup import GOAL_ID, waves_spec
from autonomy.gym.waves.world import WavesWorld


def make_world(
    data: WavesData, world: str = "heterogeneous", *, days: int = 28, seed: int = 0, **overrides: float
) -> WavesWorld:
    spec = get_world(world).with_overrides(**overrides) if overrides else get_world(world)
    return WavesWorld(waves_spec(spec, data, seed=seed, days=days), data, spec)


def send(
    world: WavesWorld, customer_id: str, channel: Channel = Channel.SMS, *, hour: int = 11, day: int = 0
) -> Decision:
    """An ACT decision sending `channel` to the customer at `hour` UTC on `day`."""
    at = world.start + timedelta(days=day, hours=hour)
    return Decision(
        decision_id=stable_id("t", customer_id, day, channel.value),
        customer_id=customer_id,
        made_at=at,
        kind=DecisionKind.ACT,
        action=Action(goal_id=GOAL_ID, channel=channel, send_at=at),
        probability=1.0,
        reason="test",
        policy_version="test",
    )


def purchases(events: list[Event] | tuple[Event, ...]) -> list[Event]:
    return [e for e in events if e.type is EventType.PURCHASE]
