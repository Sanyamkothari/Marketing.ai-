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
`measure_incrementality`. M96 adds `uplift_population` (a known per-customer effect, for the equal-budget
comparison of `engine.measurement.compare`); M100 adds several arms (`multi_arm_campaign`, a campaign of
several offers against one shared control, and `multi_arm_population`, an uplift training file where
different segments answer different offers and some are put off by both); M102 adds continuous
outcomes; they extend this module.

Pure and deterministic: no storage, no network, no clock. `pandas` is imported inside the function so
`import engine` stays fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

import numpy as np

if TYPE_CHECKING:
    import pandas as pd
    from numpy.typing import NDArray

__all__ = [
    "ARM_COLUMN",
    "AS_OF",
    "CONTROL_SHARE",
    "KEY_COLUMN",
    "MULTI_ARM_LEVELS",
    "MULTI_ARM_SEGMENTS",
    "OUTCOME_COLUMN",
    "OUTCOME_WINDOW_DAYS",
    "TREATMENT_DATE_COLUMN",
    "UPLIFT_FEATURES",
    "SimulatedCampaign",
    "SimulatedMultiArmCampaign",
    "SimulatedMultiArmPopulation",
    "SimulatedUpliftPopulation",
    "multi_arm_campaign",
    "multi_arm_population",
    "population",
    "uplift_population",
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


# ---------------------------------------------------------------------------
# Plan J M96: a population whose effect varies by customer, for the equal-budget comparison
# ---------------------------------------------------------------------------
UPLIFT_FEATURES: Final[tuple[str, ...]] = ("x1", "x2", "x3", "x4")
"""The simulated customers' features: `x1` drives risk, `x2` drives the effect, `x3`, `x4` are noise."""


@dataclass(frozen=True)
class SimulatedUpliftPopulation:
    """Randomised rows with a known per-customer effect: what `engine.measurement.compare` reads.

    The treatment was drawn per row with the recorded probability `propensity`, the way M92's
    holdout and explore slice draw it: "selected" customers (the riskiest `selected_share`) are
    treated unless held out, the rest are explored at a lower rate. So `propensity` depends on the
    features, and an estimator that ignored it would be biased.
    """

    features: pd.DataFrame
    t: NDArray[np.int_]
    y: NDArray[np.int_]
    propensity: NDArray[np.float64]
    """P(treated | x) as recorded: `1 - holdout_fraction` for a selected row, `(1 - h) * explore_fraction` otherwise."""
    base: NDArray[np.float64]
    """P(outcome | not treated, x): the true risk."""
    tau: NDArray[np.float64]
    """P(outcome | treated, x) - P(outcome | not treated, x): the true effect of treating each customer."""

    def true_incremental(self, policy: NDArray[np.float64]) -> float:
        """What treating the rows `policy` marks would add on these customers: `Σ π·τ`."""
        return float(np.sum(np.asarray(policy, dtype=np.float64) * self.tau))


def uplift_population(
    n: int,
    *,
    seed: int,
    effect: Literal["heterogeneous", "null", "risk"],
    selected_share: float = 0.3,
    holdout_fraction: float = 0.2,
    explore_fraction: float = 0.5,
) -> SimulatedUpliftPopulation:
    """`n` randomised customers whose effect is known per row (Plan J M96).

    * Risk: `base = sigmoid(-1.2 + 0.9 x1)`, about 25% on average.
    * `effect="heterogeneous"`: `tau = 0.16 sigmoid(2 x2) - 0.04`, between -4 and +12 points and
      unrelated to risk, so uplift ranking should beat risk ranking.
    * `effect="null"`: `tau = 0` for everyone: neither ranking adds anything, and they tie.
    * `effect="risk"`: `tau = 0.4 base`, the effect grows with risk: risk ranking is the right one.

    `p(outcome | treated) = clip(base + tau, 0, 1)`. Deterministic for a seed.
    """
    import pandas as pd

    if n < 10:
        raise ValueError(f"n must be at least 10, not {n}.")
    _check_share("selected_share", selected_share)
    _check_share("holdout_fraction", holdout_fraction, below_one=True)
    _check_share("explore_fraction", explore_fraction)
    if holdout_fraction <= 0.0 or explore_fraction <= 0.0:
        raise ValueError(
            "holdout_fraction and explore_fraction must be above 0 for every row to be randomised."
        )
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((n, len(UPLIFT_FEATURES)))
    base = 1.0 / (1.0 + np.exp(-(-1.2 + 0.9 * x[:, 0])))
    if effect == "heterogeneous":
        tau = 0.16 / (1.0 + np.exp(-2.0 * x[:, 1])) - 0.04
    elif effect == "null":
        tau = np.zeros(n)
    elif effect == "risk":
        tau = 0.4 * base
    else:  # pragma: no cover - the Literal says which
        raise ValueError(f"Unknown effect {effect!r}.")
    treated_rate = np.clip(base + tau, 0.0, 1.0)
    tau = treated_rate - base
    cut = np.quantile(x[:, 0], 1.0 - selected_share) if selected_share > 0.0 else np.inf
    selected = x[:, 0] >= cut
    propensity = np.where(selected, 1.0 - holdout_fraction, (1.0 - holdout_fraction) * explore_fraction)
    t = (rng.random(n) < propensity).astype(np.int_)
    y = (rng.random(n) < np.where(t == 1, treated_rate, base)).astype(np.int_)
    features = pd.DataFrame(x, columns=list(UPLIFT_FEATURES))
    return SimulatedUpliftPopulation(
        features=features, t=t, y=y, propensity=propensity.astype(np.float64), base=base, tau=tau
    )


# ---------------------------------------------------------------------------
# Plan J M100: several offers against one shared control
# ---------------------------------------------------------------------------
ARM_COLUMN: Final[str] = "offer"
"""The column of a simulated multi-offer campaign naming each customer's offer (blank for the control);
not `arm`, which a campaign's `assignment.parquet` uses for treated, holdout or suppressed (M94)."""

MULTI_ARM_LEVELS: Final[tuple[str, str, str]] = ("none", "offer_a", "offer_b")
"""The planted population's treatment levels: the control first, then the two offers."""

MULTI_ARM_SEGMENTS: Final[tuple[str, str, str, str]] = (
    "offer_a_responders",
    "offer_b_responders",
    "unmoved",
    "sleeping_dogs",
)
"""The planted segments, cut on `offer_affinity`: who answers offer A, who answers B, nobody, and who
is put off by both."""


@dataclass(frozen=True)
class SimulatedMultiArmCampaign:
    """A campaign of several offers with a known effect per offer (Plan J M100)."""

    scores: pd.DataFrame
    """`customer_id`, `control_group`, `suppressed_reason` (empty), `intended_treatment` (True) and `offer`
    (the offer's level; blank for a held-out customer)."""
    outcomes: pd.DataFrame
    """`customer_id`, `converted` (0/1) and `treatment_date`; every outcome is mature on `AS_OF`."""
    levels: tuple[str, ...]
    """The control level, then the offers."""
    base_rate: float
    effects: tuple[float, ...]
    """Each offer's true effect (the difference in conversion rate it causes), in `levels[1:]` order."""

    @property
    def measure_kwargs(self) -> dict[str, Any]:
        """The arguments of `engine.measurement.measure.measure_campaign` that read these frames."""
        return {
            "run_id": "simulated",
            "primary_key": KEY_COLUMN,
            "outcome_column": OUTCOME_COLUMN,
            "treatment_date_column": TREATMENT_DATE_COLUMN,
            "outcome_window_days": OUTCOME_WINDOW_DAYS,
            "treatment_time": AS_OF - timedelta(days=OUTCOME_WINDOW_DAYS),
            "as_of": AS_OF,
            "arm_column": ARM_COLUMN,
            "arms": self.levels[1:],
            "control_level": self.levels[0],
        }


def multi_arm_campaign(
    n: int,
    base_rate: float,
    effects: tuple[float, ...],
    *,
    seed: int,
    control_share: float | None = None,
    levels: tuple[str, ...] | None = None,
) -> SimulatedMultiArmCampaign:
    """`n` customers split at random between a shared control and `len(effects)` offers (Plan J M100).

    Complete randomisation: `round(control_share * n)` customers are held back (an equal share with the
    offers when `control_share` is None), the rest are dealt to the offers in equal numbers at random.
    A customer converts with probability `base_rate` (+ the offer's effect). Every outcome is mature.
    Deterministic for a seed.
    """
    import pandas as pd

    arms = len(effects)
    if arms < 1:
        raise ValueError("A multi-offer campaign needs at least one offer.")
    names = levels if levels is not None else ("none", *(f"offer_{k}" for k in range(1, arms + 1)))
    if len(names) != arms + 1:
        raise ValueError("levels names the control and one level per effect.")
    share = 1.0 / (arms + 1) if control_share is None else control_share
    _check_share("control_share", share, below_one=True)
    _check_share("base_rate", base_rate)
    for effect in effects:
        if not 0.0 <= base_rate + effect <= 1.0:
            raise ValueError(f"base_rate + effect must be in [0, 1], not {base_rate + effect}.")
    n_control = round(share * n)
    if not 1 <= n_control <= n - arms:
        raise ValueError("control_share leaves the control or an offer empty.")
    rng = np.random.default_rng(seed)
    order = rng.permutation(n)
    code = np.zeros(n, dtype=np.int_)
    treated = order[n_control:]
    code[treated] = 1 + np.arange(len(treated)) % arms
    lift = np.concatenate([[0.0], np.asarray(effects, dtype=np.float64)])
    converts = rng.random(n) < base_rate + lift[code]
    age_days = rng.integers(OUTCOME_WINDOW_DAYS + 1, OUTCOME_WINDOW_DAYS + _MATURE_SPREAD_DAYS + 1, size=n)
    keys = np.char.add("C", np.char.zfill(np.arange(n).astype(str), 7))
    treated_on = np.datetime64(AS_OF.date()) - age_days.astype("timedelta64[D]")
    labels = np.asarray(names, dtype=object)[code]
    labels[code == 0] = ""
    scores = pd.DataFrame(
        {
            KEY_COLUMN: keys,
            "control_group": code == 0,
            "suppressed_reason": np.full(n, "", dtype=object),
            "intended_treatment": np.ones(n, dtype=bool),
            ARM_COLUMN: labels,
        }
    )
    outcomes = pd.DataFrame(
        {
            KEY_COLUMN: keys,
            OUTCOME_COLUMN: converts.astype(np.int64),
            TREATMENT_DATE_COLUMN: treated_on.astype(str),
        }
    )
    return SimulatedMultiArmCampaign(
        scores=scores,
        outcomes=outcomes,
        levels=tuple(names),
        base_rate=base_rate,
        effects=tuple(float(e) for e in effects),
    )


@dataclass(frozen=True)
class SimulatedMultiArmPopulation:
    """Customers with a planted effect per offer, as an uplift training file (Plan J M100).

    `frame` has `customer_id`, the features (`offer_affinity`, `tenure_months`, `noise_1`, `noise_2`),
    the treatment column (a level of `levels`) and the 0/1 outcome column.
    """

    frame: pd.DataFrame
    levels: tuple[str, ...]
    segment: NDArray[np.object_]
    """Each customer's planted segment (:data:`MULTI_ARM_SEGMENTS`)."""
    base: NDArray[np.float64]
    """P(outcome | control, x)."""
    tau: NDArray[np.float64]
    """True effect of each offer on each customer: `tau[:, k-1]` is offer `k`'s."""
    arm: NDArray[np.int_]
    """The level each customer was dealt: 0 the control, 1..K the offers."""

    def true_ate(self, k: int, rows: NDArray[np.int_] | None = None) -> float:
        """Offer `k`'s average effect over `rows` (every customer when None)."""
        values = self.tau[:, k - 1] if rows is None else self.tau[rows, k - 1]
        return float(np.mean(values))


def multi_arm_population(
    n: int,
    *,
    seed: int,
    outcome_column: str = OUTCOME_COLUMN,
    treatment_column: str = "offer",
    effect: float = 0.25,
    harm: float = 0.25,
    levels: tuple[str, str, str] = MULTI_ARM_LEVELS,
) -> SimulatedMultiArmPopulation:
    """`n` customers, a third dealt to each of the control and two offers at random (Plan J M100).

    The segments are cut on `offer_affinity`, uniform on [0, 1): below 0.35 a customer answers offer A
    (+`effect`) and not B; from 0.35 to 0.70 offer B (+`effect`) and not A; from 0.70 to 0.85 neither;
    from 0.85 both offers put them off (-`harm`, from a higher base rate of 35%, so they are sleeping
    dogs for both). Everyone else converts at 15% without an offer, a little more with longer tenure.
    The default effects (25 points either way) are planted large on purpose: a meta-learner's
    per-customer estimate on a few thousand customers per arm scatters by several points, and the
    fixture tests the choice, not the learner's resolution. Deterministic for a seed.
    """
    import pandas as pd

    if n < 30:
        raise ValueError(f"n must be at least 30, not {n}.")
    rng = np.random.default_rng(seed)
    affinity = rng.random(n)
    tenure = rng.integers(1, 72, size=n)
    noise = rng.standard_normal((n, 2))
    segment = np.select(
        [affinity < 0.35, affinity < 0.70, affinity < 0.85],
        list(MULTI_ARM_SEGMENTS[:3]),
        default=MULTI_ARM_SEGMENTS[3],
    ).astype(object)
    base = np.where(segment == MULTI_ARM_SEGMENTS[3], 0.35, 0.15) + 0.03 * (tenure / 72.0)
    tau = np.zeros((n, 2), dtype=np.float64)
    tau[segment == MULTI_ARM_SEGMENTS[0], 0] = effect
    tau[segment == MULTI_ARM_SEGMENTS[1], 1] = effect
    tau[segment == MULTI_ARM_SEGMENTS[3], :] = -harm
    arm = np.asarray(rng.permutation(np.arange(n) % 3), dtype=np.int_)
    lift = np.where(arm == 0, 0.0, tau[np.arange(n), np.maximum(arm - 1, 0)])
    y = (rng.random(n) < np.clip(base + lift, 0.0, 1.0)).astype(np.int64)
    frame = pd.DataFrame(
        {
            KEY_COLUMN: np.char.add("M", np.char.zfill(np.arange(n).astype(str), 7)),
            "offer_affinity": np.round(affinity, 4),
            "tenure_months": tenure,
            "noise_1": noise[:, 0],
            "noise_2": noise[:, 1],
            treatment_column: np.asarray(levels, dtype=object)[arm],
            outcome_column: y,
        }
    )
    return SimulatedMultiArmPopulation(
        frame=frame, levels=tuple(levels), segment=segment, base=base, tau=tau, arm=arm
    )
