"""Synthetic uplift data with a KNOWN treatment effect per customer (plan B §9).

Every row belongs to one of four planted segments, decided by its features alone, and each segment
has a known pair of outcome probabilities - one if treated, one if not:

| Segment        | Rule on the features                               | P(y | control) | P(y | treated) |
|----------------|----------------------------------------------------|----------------|----------------|
| sleeping dog   | `region == "north"` and `tenure_months >= 36`      | 0.30           | 0.08           |
| sure thing     | `plan == "premium"` (and not a sleeping dog)       | 0.60           | 0.62           |
| persuadable    | `visits_30d >= 6` (and neither of the above)       | 0.05           | 0.30           |
| lost cause     | everything else                                    | 0.03           | 0.035          |

Inside the persuadables the effect also grows with `visits_30d`, so the true uplift is not a
step function and a rank correlation against it means something. `effect_scale` multiplies every
treated-minus-control gap; `0.0` is the null world where treatment changes nothing.

Assignment is random with `treat_share` unless `targeted=True`, in which case customers with many
visits are far more likely to be treated - the non-random design `TREATMENT_NOT_RANDOM` exists to
catch.

The truth (`p_control`, `p_treated`, `true_uplift`, `true_segment`) is returned in a SEPARATE frame,
keyed by `customer_id`, so it can never leak into the features a learner sees.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

SEGMENT_PROBABILITIES: dict[str, tuple[float, float]] = {
    "sleeping_dog": (0.30, 0.08),
    "sure_thing": (0.60, 0.62),
    "persuadable": (0.05, 0.30),
    "lost_cause": (0.03, 0.035),
}
"""(P(y | control), P(y | treated)) per planted segment, before `effect_scale`."""

REGIONS: tuple[str, ...] = ("north", "south", "east", "west")
PLANS: tuple[str, ...] = ("basic", "plus", "premium")


@dataclass(frozen=True)
class UpliftDataset:
    """What a generator returns: the uploadable frame and, apart from it, the ground truth."""

    frame: pd.DataFrame
    truth: pd.DataFrame


def _segments(frame: pd.DataFrame) -> np.ndarray:
    sleeping = (frame["region"] == "north") & (frame["tenure_months"] >= 36)
    sure = (frame["plan"] == "premium") & ~sleeping
    persuadable = (frame["visits_30d"] >= 6) & ~sleeping & ~sure
    return np.select(
        [sleeping.to_numpy(), sure.to_numpy(), persuadable.to_numpy()],
        ["sleeping_dog", "sure_thing", "persuadable"],
        default="lost_cause",
    )


def make_uplift_data(
    n: int = 20_000,
    *,
    seed: int = 7,
    treat_share: float = 0.5,
    effect_scale: float = 1.0,
    targeted: bool = False,
    outcome_column: str = "reactivated_90d",
    treatment_column: str = "treatment",
    treatment_date: datetime | None = None,
) -> UpliftDataset:
    """A one-row-per-customer uplift table with planted segments; see the module docstring."""
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{index:07d}" for index in range(n)],
            "age": rng.integers(18, 80, size=n),
            "tenure_months": rng.integers(1, 72, size=n),
            "visits_30d": rng.poisson(4.0, size=n),
            "monthly_spend": np.round(rng.gamma(2.0, 30.0, size=n), 2),
            "plan": rng.choice(PLANS, size=n, p=[0.5, 0.3, 0.2]),
            "region": rng.choice(REGIONS, size=n),
            "support_tickets_90d": rng.poisson(1.0, size=n),
            "noise_a": rng.normal(size=n),
        }
    )
    segment = _segments(frame)
    p_control = np.array([SEGMENT_PROBABILITIES[s][0] for s in segment])
    p_treated_base = np.array([SEGMENT_PROBABILITIES[s][1] for s in segment])
    # Inside the persuadables the effect grows with visits, so the truth is graded, not a step.
    boost = np.where(
        segment == "persuadable", np.clip((frame["visits_30d"].to_numpy() - 6) * 0.02, 0, 0.12), 0
    )
    p_treated_base = np.clip(p_treated_base + boost, 0.0, 0.95)
    p_treated = np.clip(p_control + effect_scale * (p_treated_base - p_control), 0.0, 1.0)

    if targeted:
        propensity = np.where(frame["visits_30d"].to_numpy() >= 5, 0.9, 0.1)
    else:
        propensity = np.full(n, treat_share)
    treatment = (rng.random(n) < propensity).astype(int)
    probability = np.where(treatment == 1, p_treated, p_control)
    outcome = (rng.random(n) < probability).astype(int)

    frame[treatment_column] = treatment
    frame[outcome_column] = outcome
    if treatment_date is not None:
        frame["treatment_date"] = treatment_date.date().isoformat()

    truth = pd.DataFrame(
        {
            "customer_id": frame["customer_id"],
            "p_control": p_control,
            "p_treated": p_treated,
            "true_uplift": p_treated - p_control,
            "true_segment": segment,
            "propensity": propensity,
        }
    )
    return UpliftDataset(frame=frame, truth=truth)


def make_winback_campaign(
    n: int = 12_000,
    *,
    seed: int = 11,
    control_fraction: float = 0.10,
    sent_at: datetime | None = None,
) -> UpliftDataset:
    """A finished win-back campaign: 10% random control group, outcomes known (plan B §11).

    Same features and planted effects as :func:`make_uplift_data`; `treatment` is 1 for the 90% who
    received the win-back message. `treatment_date` is the send date, 2026-05-01 by default, so a
    90-day outcome window has elapsed by any `as_of` after 2026-07-30.
    """
    sent = sent_at or (datetime(2026, 5, 1, tzinfo=UTC))
    return make_uplift_data(
        n,
        seed=seed,
        treat_share=1.0 - control_fraction,
        treatment_date=sent,
    )


def outcomes_for(
    dataset: UpliftDataset,
    *,
    treated_keys: set[str],
    seed: int = 3,
    outcome_column: str = "reactivated_90d",
    effect_scale: float = 1.0,
) -> pd.DataFrame:
    """Outcomes observed AFTER a scoring run acted: treated rows draw from p_treated, others p_control.

    `treated_keys` is who the run actually contacted (not the control group, not the suppressed);
    the result is the outcomes file a user uploads to the Campaign results page.
    """
    rng = np.random.default_rng(seed)
    truth = dataset.truth
    treated = truth["customer_id"].isin(treated_keys).to_numpy()
    p_t = truth["p_control"].to_numpy() + effect_scale * (truth["p_treated"] - truth["p_control"]).to_numpy()
    probability = np.where(treated, p_t, truth["p_control"].to_numpy())
    return pd.DataFrame(
        {
            "customer_id": truth["customer_id"],
            outcome_column: (rng.random(len(truth)) < probability).astype(int),
        }
    )


def days_ago(days: int, *, now: datetime | None = None) -> datetime:
    """A UTC timestamp `days` before `now` (default: the current time)."""
    return (now or datetime.now(tz=UTC)) - timedelta(days=days)
