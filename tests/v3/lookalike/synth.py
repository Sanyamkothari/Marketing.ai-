"""A small made-up shop with a known answer, for the look-alike tests.

Customers differ in how recently they bought and how often. Contact lifts the chance of buying by an
amount that falls with recency (people who bought long ago respond less). Nothing here is the Gym's
truth: it is a function of the same fields a real state carries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from autonomy.contracts import Channel, ChannelStats, CustomerState
from autonomy.decide.lookalike import LookalikeResponseModel, Outcome, profile_of

T0 = datetime(2026, 1, 1, tzinfo=UTC)
KEY = "email|none|reminder"
WINDOW = timedelta(days=7)


def make_state(i: int, rng: np.random.Generator) -> CustomerState:
    rec = int(rng.integers(1, 400))
    n = int(rng.integers(1, 12))
    return CustomerState(
        customer_id=f"c{i}",
        as_of=T0,
        first_seen_at=T0 - timedelta(days=int(rng.integers(400, 800))),
        last_purchase_at=T0 - timedelta(days=rec),
        purchases=n,
        revenue=50.0 * n,
        contacts_7d=int(rng.integers(0, 3)),
        segment=str(rng.choice(["new", "lapsed", "light", "medium", "heavy"])),
        channels={
            Channel.EMAIL: ChannelStats(delivers=int(rng.integers(0, 30)), clicks=int(rng.integers(0, 5)))
        },
    )


def chance(state: CustomerState, treated: bool) -> float:
    """The made-up truth: chance of buying in the window."""
    rec = (state.as_of - (state.last_purchase_at or state.as_of)).days
    logit = -3.0 + 0.6 * state.purchases / 12 - 0.003 * rec
    if treated:
        logit += 0.5 - 0.004 * rec
    return float(1.0 / (1.0 + np.exp(-logit)))


def outcomes(n: int, seed: int, *, holdout: float = 0.2) -> tuple[list[CustomerState], list[Outcome]]:
    rng = np.random.default_rng(seed)
    states = [make_state(i, rng) for i in range(n)]
    out: list[Outcome] = []
    for st in states:
        treated = bool(rng.random() >= holdout)
        bought = bool(rng.random() < chance(st, treated))
        out.append(Outcome(st.customer_id, profile_of(st), KEY if treated else None, bought, T0, T0 + WINDOW))
    return states, out


def fitted(n: int = 3000, seed: int = 1) -> tuple[LookalikeResponseModel, list[CustomerState]]:
    states, rows = outcomes(n, seed)
    model = LookalikeResponseModel()
    model.observe(rows)
    assert model.fit(T0 + timedelta(days=8))
    return model, states
