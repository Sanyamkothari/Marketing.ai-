"""The figures docs/v3/WHO.md quotes from the test fixtures, in one command (a few seconds):

    python -m tests.v3.who.probe

1. The slow models fitted on `synth.shop(500)` at its end: their held-out checks, and how many
   customers the value model puts at exactly 0.
2. The planted trait population of `test_trait_harm.py` (60 dogs with ratio 0.4 and thin records,
   60 neutral, 370 others): how many of each group are left alone, and on their own record alone.
3. A mixed group (60 dogs among 300, the others helped): how many helped members are left alone, and
   a newcomer's chance below 1.

Not collected as a test; synthetic data only, no claim to resemble a shop.
"""

from __future__ import annotations

import time

import numpy as np

from autonomy.decide.bidders import LEAVE_ALONE_CREDIBLE
from autonomy.decide.slow_models import CHURN_RISK, CLV_90D, SlowModels
from autonomy.decide.who import TraitHarm
from autonomy.profile import profile_for
from tests.v3.who.synth import END, shop
from tests.v3.who.test_trait_harm import BACKGROUND, own_alone, population, record


def slow_models() -> None:
    events = shop(500)
    models = SlowModels()
    t0 = time.perf_counter()
    models.fit(events, END)
    took = time.perf_counter() - t0
    print(f"[slow models, shop(500)] {models.describe()} (fit {took:.2f} s)")
    by: dict[str, list[object]] = {}
    for e in events:
        by.setdefault(e.customer_id, []).append(e)
    scores = [models.scores(profile_for(evs, END)) for evs in by.values()]  # type: ignore[arg-type]
    values = [s[CLV_90D] for s in scores if CLV_90D in s]
    lapsed = [s[CLV_90D] for s in scores if CLV_90D in s and CHURN_RISK not in s]
    print(
        f"  clv_90d: {len(values)} scored, {sum(v == 0.0 for v in values)} at exactly 0, lowest "
        f"{min(values):.2f}; lapsed (no risk score): {len(lapsed)}, lowest {min(lapsed):.2f}, "
        f"highest {max(lapsed):.2f}"
    )


def planted() -> None:
    records, keys = population({**BACKGROUND, "dogs|heavy": (60, 0.4), "neutral|heavy": (60, 1.0)})
    harm = TraitHarm()
    harm.fit(records, keys)
    dogs = [c for c in records if keys[c] == "dogs|heavy"]
    neutral = [c for c in records if keys[c] == "neutral|heavy"]
    left_dogs = sum(harm.leave_alone(c, 1.0) for c in dogs)
    left_neutral = sum(harm.leave_alone(c, 1.0) for c in neutral)
    alone = np.mean([own_alone(records, c) >= LEAVE_ALONE_CREDIBLE for c in dogs])
    n, ratio, p = harm.groups()["dogs|heavy"]
    print(
        f"[planted] dogs left alone {left_dogs} of {len(dogs)}; neutral {left_neutral} of {len(neutral)}; "
        f"dogs on their own record alone {alone:.0%}; dogs' group: {n} customers, ratio {ratio:.2f}, "
        f"chance below 1 {p:.2f}, tau {harm.group_tau()['dogs|heavy']:.2f}; a newcomer to it "
        f"{harm.p_harm('', 'dogs|heavy'):.2f}, to the neutral group {harm.p_harm('', 'neutral|heavy'):.2f}"
    )


def mixed() -> None:
    records, keys = population(BACKGROUND, seed=1)
    rng = np.random.default_rng(7)
    for i in range(300):
        cid = f"mix-{i:03d}"
        records[cid] = record(rng, 0.3 if i < 60 else 1.1, max(int(rng.poisson(4)), 1))
        keys[cid] = "mix"
    harm = TraitHarm()
    harm.fit(records, keys)
    n, ratio, p = harm.groups()["mix"]
    helped = sum(harm.leave_alone(f"mix-{i:03d}", 1.0) for i in range(60, 300))
    dogs = sum(harm.leave_alone(f"mix-{i:03d}", 1.0) for i in range(60))
    print(
        f"[mixed] group of {n}: ratio {ratio:.2f}, chance below 1 {p:.4f}, tau {harm.group_tau()['mix']:.2f}; "
        f"helped members left alone {helped} of 240, dogs {dogs} of 60; a newcomer's chance below 1 "
        f"{harm.p_harm('', 'mix'):.2f}"
    )


if __name__ == "__main__":
    slow_models()
    planted()
    mixed()
