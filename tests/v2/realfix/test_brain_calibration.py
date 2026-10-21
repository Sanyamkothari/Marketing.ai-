"""The agent's value of a message, calibrated on a past randomised campaign (failures 1 and 3).

Synthetic, inline and small: a shop whose past campaign sent one message to a random half of its
customers (logged probability 0.5) and left the other half alone. Clicks run at 20% (a visit counts
as a click, as on real e-mail data), but the message adds only 2 purchases per 100 recipients. The
agent must value a message at what the control group shows, not at the prior's quarter purchase
per click, and on a channel that logs no clicks it must learn from purchases instead of contacting
nobody.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

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
from autonomy.learning.calibration import HistoryDigest, difference, randomised_arms, with_prior
from autonomy.learning.observations import LoggedSend, logged_sends
from autonomy.runtime.agent import (
    LIFT_PER_CLICK_PRIOR,
    LIFT_PER_SEND_PRIOR,
    AgentPolicy,
    ClickResponseModel,
    build_agent_policy,
)

CAMPAIGN = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
NOW = CAMPAIGN + timedelta(days=15)
GOAL = "goal_calibration"
ORDER_VALUE, MARGIN = 50.0, 20.0  # a purchase is worth 20 of margin
BASE_BUY, EXTRA_BUY = 0.05, 0.02


def brief(channel: Channel) -> Brief:
    return Brief(
        brief_id=f"brief_{channel.value}",
        goal_id=GOAL,
        objective=Objective.GROSS_PROFIT,
        audience=Audience(description="everyone"),
        horizon_days=14,
        envelope=Envelope(channels=(channel,), budget=1e9, max_contacts_per_week=2),
        rationale="test",
        created_at=CAMPAIGN - timedelta(days=30),
        approved_by="test",
        approved_at=CAMPAIGN - timedelta(days=30),
    )


def content(channel: Channel) -> list[ContentItem]:
    return [
        ContentItem(
            content_id=f"msg_{channel.value}",
            goal_id=GOAL,
            channel=channel,
            theme=Theme.REMINDER,
            subject="Hello" if channel is Channel.EMAIL else None,
            body="Something new for you.",
            status=ContentStatus.APPROVED,
            created_at=CAMPAIGN - timedelta(days=30),
        )
    ]


def shop(
    n: int,
    channel: Channel,
    *,
    seed: int = 0,
    clicks: bool = True,
    p_send: float = 0.5,
    prefix: str = "c",
    extra: float = EXTRA_BUY,
) -> dict[str, list[Event]]:
    """Each customer: signup and consent a year ago, a purchase or two before the campaign; the
    campaign sends to `p_send` of them at random; clicks at 20% of recipients; window purchases at
    `BASE_BUY` (+ `extra` if sent)."""
    rng = np.random.default_rng(seed)
    out: dict[str, list[Event]] = {}
    for i in range(n):
        cid = f"{prefix}{i}"
        signup = CAMPAIGN - timedelta(days=365)
        evs = [
            Event(event_id=f"su_{cid}", customer_id=cid, type=EventType.SIGNUP, ts=signup, source="t"),
            Event(
                event_id=f"co_{cid}",
                customer_id=cid,
                type=EventType.CONSENT_GRANTED,
                ts=signup,
                source="t",
                channel=channel,
            ),
        ]
        for k in range(1 + int(rng.integers(2))):
            evs.append(
                Event(
                    event_id=f"pre_{cid}_{k}",
                    customer_id=cid,
                    type=EventType.PURCHASE,
                    ts=CAMPAIGN - timedelta(days=int(rng.integers(20, 300))),
                    source="t",
                    value=ORDER_VALUE,
                    margin=MARGIN,
                )
            )
        sent = rng.random() < p_send
        if sent:
            did = f"camp_{cid}"
            evs.append(
                Event(
                    event_id=f"send_{cid}",
                    customer_id=cid,
                    type=EventType.SEND,
                    ts=CAMPAIGN,
                    source="t",
                    channel=channel,
                    decision_id=did,
                    attrs={"content_id": f"msg_{channel.value}", "logged_probability": p_send},
                )
            )
            if clicks and rng.random() < 0.2:
                evs.append(
                    Event(
                        event_id=f"click_{cid}",
                        customer_id=cid,
                        type=EventType.CLICK,
                        ts=CAMPAIGN + timedelta(hours=2),
                        source="t",
                        channel=channel,
                        decision_id=did,
                    )
                )
        if rng.random() < BASE_BUY + (extra if sent else 0.0):
            evs.append(
                Event(
                    event_id=f"buy_{cid}",
                    customer_id=cid,
                    type=EventType.PURCHASE,
                    ts=CAMPAIGN + timedelta(days=4),
                    source="t",
                    value=ORDER_VALUE,
                    margin=MARGIN,
                )
            )
        out[cid] = evs
    return out


def fold(history: dict[str, list[Event]], *, before: datetime | None = None) -> list[CustomerState]:
    folder = StateFolder()
    states: list[CustomerState] = []
    for evs in history.values():
        st: CustomerState | None = None
        for e in sorted(evs, key=lambda e: e.ts):
            if before is None or e.ts < before:
                st = folder.fold(st, e)
        if st is not None:
            states.append(folder.refresh(st, NOW))
    return states


def warmed(
    history: dict[str, list[Event]], channel: Channel, cost: float
) -> tuple[AgentPolicy, DecisionContext]:
    policy = build_agent_policy([brief(channel)], content(channel), costs={channel: cost})
    policy.nightly([], history, NOW)
    ctx = DecisionContext(now=NOW, briefs=(brief(channel),), content=tuple(content(channel)), seed=1)
    policy.prepare_day(fold(history), ctx)
    return policy, ctx


def clicks_model(policy: AgentPolicy) -> ClickResponseModel:
    model = policy.bundle.response
    assert isinstance(model, ClickResponseModel)
    return model


def decide_all(
    policy: AgentPolicy, ctx: DecisionContext, states: Sequence[CustomerState]
) -> tuple[float, float]:
    """(contact rate, mean expected net of the ACT decisions)."""
    acts = [d for d in (policy.decide(s, ctx) for s in states) if d.kind is DecisionKind.ACT]
    mean = sum(d.expected_net for d in acts) / len(acts) if acts else float("nan")
    return len(acts) / len(states), mean


# --- the pure parts --------------------------------------------------------------------------


def test_randomised_campaign_and_its_control_are_found_and_a_targeted_one_is_not() -> None:
    history = shop(600, Channel.EMAIL)
    digest = HistoryDigest()
    for evs in history.values():
        digest.extend(evs)
    sends = logged_sends(history, {c.content_id: c for c in content(Channel.EMAIL)})
    arms, notes = randomised_arms(sends, digest, window=timedelta(days=14), now=NOW)
    assert notes == [] and len(arms) == 1
    arm = arms[0]
    # its window still open: part of the purchases it brings are still to come, so it waits
    early, notes_early = randomised_arms(
        sends, digest, window=timedelta(days=14), now=CAMPAIGN + timedelta(days=5)
    )
    assert early == [] and "has not closed yet" in notes_early[0]
    assert not set(arm.treated) & set(arm.control)
    assert len(arm.treated) + len(arm.control) == 600  # everyone eligible, nobody contaminated

    # the same campaign with propensities that depend on the customer is a targeted design
    targeted = [
        LoggedSend(**{**s.__dict__, "propensity": 0.8 if i % 2 else 0.4}) for i, s in enumerate(sends)
    ]
    arms, notes = randomised_arms(targeted, digest, window=timedelta(days=14), now=NOW)
    assert arms == [] and "targeted design" in notes[0]

    # a pool far larger than the design implies (customers the campaign never meant to reach)
    extra = shop(900, Channel.EMAIL, seed=5, p_send=0.0, prefix="x")
    for evs in extra.values():
        digest.extend(evs)
    arms, notes = randomised_arms(sends, digest, window=timedelta(days=14), now=NOW)
    assert arms == [] and "implied by its logged probabilities" in notes[0]


def test_history_digest_counts_each_event_once_and_reads_margin() -> None:
    digest = HistoryDigest()
    evs = shop(3, Channel.EMAIL, seed=2)["c0"]
    digest.extend(evs)
    digest.extend(evs)  # the whole log handed twice
    pre = sum(MARGIN for e in evs if e.type is EventType.PURCHASE and e.ts < CAMPAIGN)
    assert digest.money("c0", 0.0, CAMPAIGN.timestamp(), 0.4) == pytest.approx(pre)
    assert digest.reachable("c0", Channel.EMAIL, CAMPAIGN.timestamp())
    assert not digest.reachable("c0", Channel.EMAIL, (CAMPAIGN - timedelta(days=400)).timestamp())


def test_difference_is_cuped_and_unbiased_and_the_prior_combines_by_precision() -> None:
    rng = np.random.default_rng(3)
    habit = rng.gamma(2.0, 1.0, 4000)
    y = habit + rng.normal(0, 0.5, 4000)
    treat = rng.random(4000) < 0.5
    y = y + 0.3 * treat
    d = difference(list(y[treat]), list(y[~treat]), list(habit[treat]), list(habit[~treat]))
    assert d is not None and abs(d.effect - 0.3) < 3 * d.se
    plain = difference(list(y[treat]), list(y[~treat]), [0.0] * int(treat.sum()), [0.0] * int((~treat).sum()))
    assert plain is not None and d.se < 0.5 * plain.se  # the habit's variance is gone
    k, sd = with_prior((0.25, 0.25), (0.01, 0.001))
    assert k == pytest.approx(0.01, abs=0.001) and sd < 0.001


# --- the agent -------------------------------------------------------------------------------


def test_a_randomised_control_sets_the_value_of_a_message_not_the_click_prior() -> None:
    history = shop(3000, Channel.EMAIL)
    policy, ctx = warmed(history, Channel.EMAIL, cost=0.05)
    model = clicks_model(policy)
    found = policy.calibration["click"]
    assert isinstance(found, dict)
    measured = found["measured"]
    # truth: 0.02 extra purchases x 20 margin per recipient, at 20% clicks and 20 a purchase
    # -> 0.1 extra purchases per click
    assert 0.02 < measured[0] < 0.25 and model.lift[0] < 0.6 * LIFT_PER_CLICK_PRIOR[0]
    newcomers = fold(shop(400, Channel.EMAIL, seed=9, p_send=0.0, prefix="n"), before=CAMPAIGN)
    rate, expected = decide_all(policy, ctx, newcomers)
    truth = EXTRA_BUY * MARGIN - 0.05
    assert rate > 0.8  # sending pays
    assert truth / 2.5 < expected < truth * 2.5  # within the noise of 3,000 customers
    # the prior alone would say a quarter purchase per click: 0.25 x 0.2 x 20 - 0.05 = 0.95, 2.7x
    assert LIFT_PER_CLICK_PRIOR[0] * 0.2 * MARGIN - 0.05 > 2.5 * truth


def test_without_a_control_history_clicks_set_the_scale_only_part_way() -> None:
    policy, _ = warmed(shop(1500, Channel.EMAIL, p_send=0.999), Channel.EMAIL, cost=0.05)
    found = policy.calibration.get("no_control")
    assert isinstance(found, dict) and found["history_click_rate"] > 0.15
    k0 = LIFT_PER_CLICK_PRIOR[0]
    assert clicks_model(policy).lift[0] == pytest.approx(
        k0 * (0.05 / found["history_click_rate"]) ** 0.5, rel=0.1
    )


def test_a_channel_without_clicks_is_valued_from_purchases_and_used_only_when_it_pays() -> None:
    history = shop(3000, Channel.SMS, clicks=False, extra=0.04)
    newcomers = fold(shop(300, Channel.SMS, seed=11, p_send=0.0, clicks=False, prefix="n"), before=CAMPAIGN)
    cheap, ctx = warmed(history, Channel.SMS, cost=0.2)
    model = clicks_model(cheap)
    assert Channel.SMS.value in model.no_click
    assert model.send_lift != LIFT_PER_SEND_PRIOR
    rate, expected = decide_all(cheap, ctx, newcomers)
    assert rate > 0.8 and expected > 0  # 0.04 x 20 = 0.8 a message against 0.2: send
    dear, ctx2 = warmed(history, Channel.SMS, cost=3.0)
    rate2, _ = decide_all(dear, ctx2, newcomers)
    assert rate2 < 0.1  # 0.8 a message against 3.0: leave them alone (the floor explores a little)
