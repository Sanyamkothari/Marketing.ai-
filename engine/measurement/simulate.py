"""Populations with a known effect, to test that our measurement is honest (Plan J M95).

`measure_incrementality` (`engine.uplift.incrementality`) says "treated 6.2%, control 5.1%, a lift of
1.1 points, 95% interval 0.2 to 2.0". Whether that interval really holds the truth 95 times in 100 can
only be learned from data whose truth is known. This module generates that data: one call makes one
campaign - who was held back, who converted, when each customer was treated - as the two frames the
measurement reads, plus the true effect the interval is supposed to cover. The nightly suite
(`tests/statistical/`, `make test-statistical`) calls it thousands of times with fixed seeds.

**The generative model.** For each of `n` customers, from one `numpy` `Generator` seeded with `seed`
(so one seed is one campaign, bit for bit, on every machine):

1. *Assignment.* `round(control_share * n)` customers, chosen uniformly at random without
   replacement, are held back (`control_group`); the rest are assigned to treatment. This is the
   complete randomisation of the engine's own holdout, so assignment is independent of everything
   below.
2. *Receipt.* An assigned-treated customer actually receives the treatment with probability
   `compliance`; a held-back customer receives it anyway with probability `contamination` (a leak: they
   were contacted through another channel, or by another campaign). The measurement never sees who
   received it - it compares the *assigned* arms, the intent to treat (ITT).
3. *Conversion.* A customer who received the treatment converts with probability `base_rate + effect`,
   one who did not with probability `base_rate`. `effect` is therefore the effect **on a customer who
   receives the treatment**. Because only some assigned customers receive it, the effect the ITT
   comparison estimates is smaller:

       true_itt = (compliance - contamination) * effect          (see :attr:`SimulatedCampaign.true_itt`)

   With the defaults (`compliance=1`, `contamination=0`) it is `effect`. An ITT interval is judged
   against `true_itt`, never against `effect` (that is the effect on the treated, which M103 measures
   with its own estimator).
4. *When.* Every customer was treated on a date. A mature customer was treated 31 to 90 days before
   `AS_OF`, so the 30-day outcome window (`OUTCOME_WINDOW_DAYS`) has closed; an *immature* one
   (`immature_share` of customers, chosen at random and independently of arm) was treated 1 to 29 days
   before it, so the window is still open. Those dates are written to the outcomes frame, and the
   measurement's maturity rule (`date + window <= as_of`) then decides who is counted.
5. *What the file shows.* A customer who will convert does so on a day uniform over the window. An
   immature customer treated `e` days ago shows a conversion only if it fell within those `e` days, so
   the outcomes file under-reports exactly as a real one does before the window closes: counting such
   a row as "did not convert" biases the lift towards zero. The suite checks that the measurement
   leaves those rows out (so they do not bias it) and that counting them anyway would (so the harness
   can see the bias it guards against).

The frames are what the engine consumes, not simplified copies: `scores` has the run's `control_group`,
`suppressed_reason` and `intended_treatment` columns, `outcomes` has the outcome and the treatment date,
both keyed by `customer_id`. :attr:`SimulatedCampaign.measure_kwargs` holds the matching arguments of
`measure_incrementality`. M100 adds several arms and M102 continuous outcomes; they extend this module.

Pure and deterministic: no storage, no network, no clock. `pandas` is imported inside the function so
`import engine` stays fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import numpy as np

if TYPE_CHECKING:
    import pandas as pd
    from numpy.typing import NDArray

__all__ = [
    "AS_OF",
    "CONTROL_SHARE",
    "KEY_COLUMN",
    "OUTCOME_COLUMN",
    "OUTCOME_WINDOW_DAYS",
    "TREATMENT_DATE_COLUMN",
    "SimulatedCampaign",
    "population",
]

KEY_COLUMN: Final[str] = "customer_id"
OUTCOME_COLUMN: Final[str] = "converted"
TREATMENT_DATE_COLUMN: Final[str] = "treatment_date"
OUTCOME_WINDOW_DAYS: Final[int] = 30
AS_OF: Final[datetime] = datetime(2026, 6, 30, tzinfo=UTC)
"""The day the outcomes were read. A fixed date, not the clock, so a seed always makes the same campaign."""

CONTROL_SHARE: Final[float] = 0.5
"""The default share held back. Larger than a real holdout's 10%, so a small simulated population still
has enough control conversions at a 2% base rate for the interval's coverage to be what is tested."""

_MATURE_SPREAD_DAYS: Final[int] = 60
"""A mature customer was treated between `OUTCOME_WINDOW_DAYS + 1` and this many days beyond it, before AS_OF."""


@dataclass(frozen=True)
class SimulatedCampaign:
    """One simulated campaign: the frames the measurement reads, and the truth it should recover."""

    scores: pd.DataFrame
    """One row per customer: `customer_id`, `control_group` (bool), `suppressed_reason` (empty: nobody is
    suppressed) and `intended_treatment` (True), as a scoring run's `scores` has them."""
    outcomes: pd.DataFrame
    """One row per customer: `customer_id`, `converted` (0/1 as observed on `AS_OF`) and `treatment_date`
    (ISO date), as an uploaded outcomes file has them."""
    received_treatment: NDArray[np.bool_]
    """Who actually received the treatment, in `scores` order. The measurement never sees this."""
    immature: NDArray[np.bool_]
    """Who is still inside the outcome window on `AS_OF`, in `scores` order."""
    base_rate: float
    effect: float
    compliance: float
    contamination: float
    immature_share: float

    @property
    def true_itt(self) -> float:
        """The expected difference between the assigned arms' eventual conversion rates:
        `(compliance - contamination) * effect`. What an ITT interval has to cover."""
        return (self.compliance - self.contamination) * self.effect

    @property
    def measure_kwargs(self) -> dict[str, Any]:
        """The arguments of `measure_incrementality` that read these frames, as the engine reads a run:
        `measure_incrementality(c.scores, c.outcomes, **c.measure_kwargs)`."""
        return {
            "run_id": "simulated",
            "primary_key": KEY_COLUMN,
            "outcome_column": OUTCOME_COLUMN,
            "treatment_date_column": TREATMENT_DATE_COLUMN,
            "outcome_window_days": OUTCOME_WINDOW_DAYS,
            "treatment_time": AS_OF - timedelta(days=OUTCOME_WINDOW_DAYS),
            "as_of": AS_OF,
        }


def _check_share(name: str, value: float, *, below_one: bool = False) -> None:
    """Raise `ValueError` unless `value` is in [0, 1] (or [0, 1) when `below_one`)."""
    if not (0.0 <= value < 1.0 if below_one else 0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be in {'[0, 1)' if below_one else '[0, 1]'}, not {value}.")


def population(
    n: int,
    base_rate: float,
    effect: float,
    *,
    compliance: float = 1.0,
    contamination: float = 0.0,
    immature_share: float = 0.0,
    seed: int,
    control_share: float = CONTROL_SHARE,
) -> SimulatedCampaign:
    """One campaign of `n` customers with a known effect; see the module docstring for the model.

    `base_rate` is the conversion probability without the treatment and `effect` the absolute change
    it makes to a customer who receives it; `base_rate + effect` must stay inside [0, 1]. `compliance`
    and `contamination` are the shares of assigned-treated customers who receive the treatment and of
    held-back customers who receive it anyway. `immature_share` is the share still inside the outcome
    window (below 1, so some outcome is settled). `control_share` is the share held back (an addition to
    the plan's signature, defaulted, so a caller can ask for a realistic 10% holdout). Raises
    `ValueError` for a value out of range, or an `n` too small to put a customer in each arm.
    """
    import pandas as pd

    if n < 2:
        raise ValueError(f"n must be at least 2, not {n}.")
    _check_share("base_rate", base_rate)
    _check_share("compliance", compliance)
    _check_share("contamination", contamination)
    _check_share("immature_share", immature_share, below_one=True)
    _check_share("control_share", control_share, below_one=True)
    if not 0.0 <= base_rate + effect <= 1.0:
        raise ValueError(f"base_rate + effect must be in [0, 1], not {base_rate + effect}.")
    n_control = round(control_share * n)
    if not 1 <= n_control <= n - 1:
        raise ValueError("control_share leaves one of the two arms empty.")

    rng = np.random.default_rng(seed)
    control = np.zeros(n, dtype=bool)
    control[rng.choice(n, size=n_control, replace=False)] = True
    # Receipt: one draw per customer, compared with the probability of their arm.
    received = rng.random(n) < np.where(control, contamination, compliance)
    # Conversion: one uniform per customer against the probability their treatment status implies.
    converts = rng.random(n) < np.where(received, base_rate + effect, base_rate)
    # When: immature customers (a random, arm-independent share) are `elapsed` days into their window.
    immature = rng.random(n) < immature_share
    elapsed = rng.integers(1, OUTCOME_WINDOW_DAYS, size=n)  # 1..29 days, used only where immature
    settled = rng.integers(OUTCOME_WINDOW_DAYS + 1, OUTCOME_WINDOW_DAYS + _MATURE_SPREAD_DAYS + 1, size=n)
    age_days = np.where(immature, elapsed, settled)
    # What the file shows: a converter converts on a uniform day of the window, and an immature
    # customer shows it only if that day has already come.
    conversion_day = rng.integers(1, OUTCOME_WINDOW_DAYS + 1, size=n)
    observed = converts & (~immature | (conversion_day <= age_days))

    keys = np.char.add("C", np.char.zfill(np.arange(n).astype(str), 7))
    treated_on = np.datetime64(AS_OF.date()) - age_days.astype("timedelta64[D]")
    scores = pd.DataFrame(
        {
            KEY_COLUMN: keys,
            "control_group": control,
            "suppressed_reason": np.full(n, "", dtype=object),
            "intended_treatment": np.ones(n, dtype=bool),
        }
    )
    outcomes = pd.DataFrame(
        {
            KEY_COLUMN: keys,
            OUTCOME_COLUMN: observed.astype(np.int64),
            TREATMENT_DATE_COLUMN: treated_on.astype(str),
        }
    )
    return SimulatedCampaign(
        scores=scores,
        outcomes=outcomes,
        received_treatment=received,
        immature=immature,
        base_rate=base_rate,
        effect=effect,
        compliance=compliance,
        contamination=contamination,
        immature_share=immature_share,
    )
