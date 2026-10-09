"""A planted population for choosing the offer in a real run (Plan J M100 part B, DEC-1310 (q) on).

Like `engine.measurement.simulate.multi_arm_population`, customers are dealt at random to no offer,
offer A and offer B, and their outcomes are drawn from a planted truth. It adds the one segment part B
needs: customers who answer **both** offers (A more than B), so a customer who cannot be reached on
offer A's only channel has another offer worth giving. The segments, cut on `offer_affinity`:

* below 0.30 `a_only`: offer A adds 25 points, offer B nothing;
* 0.30 to 0.55 `b_only`: offer B adds 25 points, offer A nothing;
* 0.55 to 0.75 `both`: offer A adds 25 points, offer B 20;
* 0.75 to 0.88 `neither`: no offer changes anything;
* from 0.88 `sleeping_dogs`: both offers take 25 points off a 35% base rate.

Everyone else converts at 15% without an offer, a little more with longer tenure. Each customer also
carries the per-channel consent columns a client sends (`sms_opt_in`, `email_opt_in`), drawn at random
and unrelated to the outcome, so a training file holds them as a client's would. Deterministic for a seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import pandas as pd
from numpy.typing import NDArray

LEVELS: Final[tuple[str, str, str]] = ("none", "offer_a", "offer_b")
SEGMENTS: Final[tuple[str, ...]] = ("a_only", "b_only", "both", "neither", "sleeping_dogs")
FEATURES: Final[tuple[str, ...]] = ("offer_affinity", "tenure_months", "noise_1", "noise_2")
CHANNEL_COLUMNS: Final[tuple[str, str]] = ("sms_opt_in", "email_opt_in")


@dataclass(frozen=True)
class OfferPopulation:
    frame: pd.DataFrame
    segment: NDArray[np.object_]
    tau: NDArray[np.float64]
    """True effect of each offer on each customer: `tau[:, k-1]` is offer `k`'s."""


def offer_population(
    n: int, *, seed: int, outcome_column: str = "reactivated_90d", treatment_column: str = "offer"
) -> OfferPopulation:
    rng = np.random.default_rng(seed)
    affinity = rng.random(n)
    tenure = rng.integers(1, 72, size=n)
    noise = rng.standard_normal((n, 2))
    segment = np.select(
        [affinity < 0.30, affinity < 0.55, affinity < 0.75, affinity < 0.88],
        list(SEGMENTS[:4]),
        default=SEGMENTS[4],
    ).astype(object)
    dogs = segment == "sleeping_dogs"
    base = np.where(dogs, 0.35, 0.15) + 0.03 * (tenure / 72.0)
    tau = np.zeros((n, 2), dtype=np.float64)
    tau[segment == "a_only", 0] = 0.25
    tau[segment == "b_only", 1] = 0.25
    tau[segment == "both", 0] = 0.25
    tau[segment == "both", 1] = 0.20
    tau[dogs, :] = -0.25
    arm = np.asarray(rng.permutation(np.arange(n) % 3), dtype=np.int_)
    lift = np.where(arm == 0, 0.0, tau[np.arange(n), np.maximum(arm - 1, 0)])
    y = (rng.random(n) < np.clip(base + lift, 0.0, 1.0)).astype(np.int64)
    frame = pd.DataFrame(
        {
            "customer_id": np.char.add("K", np.char.zfill(np.arange(n).astype(str), 7)),
            "offer_affinity": np.round(affinity, 4),
            "tenure_months": tenure,
            "noise_1": noise[:, 0],
            "noise_2": noise[:, 1],
            "sms_opt_in": np.where(rng.random(n) < 0.7, "true", "false"),
            "email_opt_in": np.where(rng.random(n) < 0.7, "yes", "no"),
            treatment_column: np.asarray(LEVELS, dtype=object)[arm],
            outcome_column: y,
        }
    )
    return OfferPopulation(frame=frame, segment=segment, tau=tau)
