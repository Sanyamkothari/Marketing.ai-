"""Shared synthetic campaigns for the uplift tests (inline, no downloads, a fraction of a second).

No tests here: the helpers the `test_uplift_*` modules share.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from autonomy.contracts import (
    Audience,
    Brief,
    Channel,
    ChannelStats,
    ContentItem,
    ContentStatus,
    CustomerState,
    Envelope,
    Event,
    EventType,
    Objective,
    Theme,
)

T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)
NOW = T0 + timedelta(days=15)
GOAL = "goal_up"


def brief() -> Brief:
    """Every customer, e-mail only, no offers: the goal the synthetic campaign was for."""
    return Brief(
        brief_id=f"brief_{GOAL}_1",
        goal_id=GOAL,
        objective=Objective.GROSS_PROFIT,
        audience=Audience(description="Everyone"),
        horizon_days=14,
        envelope=Envelope(channels=(Channel.EMAIL,), budget=1e9, max_contacts_per_week=2),
        rationale="uplift tests",
        created_at=T0 - timedelta(days=30),
        approved_by="tests",
        approved_at=T0 - timedelta(days=30),
    )


def reachable(customer_id: str, tier: str, *, at: datetime = NOW) -> CustomerState:
    """A new customer of the given tier, reachable by e-mail."""
    return CustomerState(
        customer_id=customer_id,
        as_of=at,
        preferences={"tier": tier, "age": "30"},
        first_seen_at=T0 - timedelta(days=200),
        channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
    )


def content() -> list[ContentItem]:
    return [
        ContentItem(
            content_id=cid,
            goal_id=GOAL,
            channel=Channel.EMAIL,
            theme=theme,
            subject="Hello",
            body="Come and see",
            status=ContentStatus.APPROVED,
            created_at=T0 - timedelta(days=30),
        )
        for cid, theme in (("c_quality", Theme.QUALITY), ("c_novelty", Theme.NOVELTY))
    ]


def campaign(
    n: int = 4000,
    *,
    seed: int = 7,
    p_send: float = 0.5,
    lift_a: float = 0.12,
    lift_b: float = 0.0,
    base: float = 0.05,
    targeted: bool = False,
    logged: bool = True,
    two_arms: bool = False,
    varying: bool = False,
    confounded: bool = False,
) -> tuple[dict[str, list[Event]], dict[str, CustomerState]]:
    """`n` customers; tier a gains `lift_a` from the e-mail, tier b `lift_b`. Randomised with
    probability `p_send` (split over two arms when `two_arms`), unless `targeted` (tier a only) or
    `varying` (0.3 under 40, 0.7 from 40, logged per send). `confounded`: the over-40s also buy
    anyway three times as often (`3 x base`), so with `varying` a naive sent-vs-not comparison is
    biased upwards and only the propensities put it right."""
    rng = np.random.default_rng(seed)
    events: dict[str, list[Event]] = {}
    states: dict[str, CustomerState] = {}
    for i in range(n):
        cid = f"u{i:05d}"
        tier = "a" if rng.random() < 0.5 else "b"
        age = float(rng.integers(18, 70))
        signup = T0 - timedelta(days=200)
        evs = [
            Event(event_id=f"{cid}_s", customer_id=cid, type=EventType.SIGNUP, ts=signup, source="t"),
            Event(
                event_id=f"{cid}_c",
                customer_id=cid,
                type=EventType.CONSENT_GRANTED,
                ts=signup,
                source="t",
                channel=Channel.EMAIL,
            ),
        ]
        if targeted:
            sent = tier == "a" and rng.random() < 0.8
            p = 0.8
        elif varying:
            p = 0.3 if age < 40 else 0.7
            sent = rng.random() < p
        else:
            sent = rng.random() < p_send
            p = p_send
        arm_content = "c_quality"
        if sent and two_arms:
            arm_content = "c_quality" if rng.random() < 0.5 else "c_novelty"
            p = p_send / 2
        lift = (lift_a if tier == "a" else lift_b) if sent else 0.0
        if sent and two_arms and arm_content == "c_novelty":
            lift /= 2
        if sent:
            attrs: dict[str, str | float] = {"content_id": arm_content}
            if logged:
                attrs["logged_probability"] = p
            evs.append(
                Event(
                    event_id=f"{cid}_send",
                    customer_id=cid,
                    type=EventType.SEND,
                    ts=T0,
                    source="t",
                    channel=Channel.EMAIL,
                    decision_id=f"camp_{cid}",
                    attrs=attrs,
                )
            )
        organic = base * 3 if confounded and age >= 40 else base
        if rng.random() < organic + lift:
            evs.append(
                Event(
                    event_id=f"{cid}_buy",
                    customer_id=cid,
                    type=EventType.PURCHASE,
                    ts=T0 + timedelta(days=3),
                    source="t",
                    value=50.0,
                )
            )
        events[cid] = evs
        states[cid] = CustomerState(
            customer_id=cid,
            as_of=T0,
            preferences={"tier": tier, "age": f"{age:g}"},
            first_seen_at=signup,
        )
    return events, states
