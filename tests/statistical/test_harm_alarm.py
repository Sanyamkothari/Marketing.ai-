"""The harm alarm rarely fires on a campaign that does no harm, however often it is read (Plan K M138, DEC-1403).

`engine.measurement.harm` judges every interim reading of a campaign with design §9.4's one-sided mixture
statistic: an alarm when `L >= 1 / alpha` and the difference lies on the harmful side. Its promise is the
always-valid property: on a campaign that changes nothing, the chance that the alarm *ever* fires before the
planned analysis date is at most `alpha`, whether the campaign is read every day, every week or at odd moments.

**The simulated campaigns.** Customers are drawn at random into the contacted and the held-back group and enter
the campaign on a day uniform over eight weeks (staggered sends); each outcome is in once its 14-day window has
closed, as the measurement's maturity rule reads it. A reading on day `d` therefore sees every customer whose
window closed by `d`, the very setting a sequential test is for. The analysis date is the day after the last
window closes; every reading before it is an early look.

a. **A/A over many refresh schedules** (no effect): daily, every third day, weekly, at irregular moments (gaps
   of one to ten days, drawn per campaign), and twice only; a common outcome (10%, smallest effect 3 points)
   and a rare one (2%, smallest effect 1 point, small groups, where the plug-in variance is least reliable);
   and an outcome the campaign exists to prevent (the harmful side is up). The share of campaigns with any
   alarm must not exceed `alpha` by more than four Monte Carlo standard errors (DEC-1305's band,
   `tests/statistical/bands.py`), one-sided as the backfire check is. Reading a plain one-sided 95% test at
   every daily reading instead must cry wolf far more often: that is what the mixture is for.
b. **Planted harm of the smallest effect** is caught before the analysis date in at least 80% of campaigns read
   daily, with twice the customers per group that the planner (`engine.measurement.planner.n_for_mde`) says a
   single reading needs for 80% power. That is the price of reading every day without false alarms; the share
   caught at the planner's own size is printed (`-s`), not gated.
d. **Inside an open window, as the monthly loop reads it** (`engine.measurement.cycle.interim_reading`). Every
   customer is contacted on day 0 and counted from the first reading; each outcome happens on a day uniform over
   the 30-day window and counts from that day on, so each reading compares what has happened so far, over the
   same days in both groups. Read daily, every third day or weekly until the window closes and then until the
   analysis date (the readings after the window are the whole result, as the loop's early looks read it), the
   share of A/A campaigns with any alarm stays within the same band as (a), and planted harm of the smallest
   effect is caught before the window closes in at least 80% of campaigns read daily at (b)'s size.
c. **The production chain agrees.** On campaigns measured by `measure_campaign` at weekly readings (the per-row
   treatment dates, the maturity rule, the report) and judged by `harm_looks`, every reading's decision is the
   one the arithmetic of (a) gives; so (a) and (b) speak for the engine's own path.

The readings of (a) and (b) are judged by the engine's own `rate_difference` and `judge` on each reading's
counts (computed once per campaign by cumulative sums, so many thousand campaigns of many readings stay fast).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest

from engine.measurement.harm import HarmAlarmSettings, harm_looks, judge, rate_difference
from engine.measurement.measure import measure_campaign
from engine.measurement.planner import n_for_mde
from tests.statistical.bands import band, seeds

pytestmark = pytest.mark.statistical

SETTINGS = HarmAlarmSettings(enabled=True)
ALPHA = SETTINGS.alpha
SIMS_AA = 4_000
SIMS_POWER = 2_000
SIMS_CHAIN = 200
WINDOW = 14
ENROL_DAYS = 56
ANALYSIS_DAY = ENROL_DAYS + WINDOW
"""Every window has closed by the end of this day; readings are on the days before it."""
START = datetime(2026, 1, 1, tzinfo=UTC)

Schedule = Callable[[np.random.Generator], Sequence[int]]


def _irregular(rng: np.random.Generator) -> Sequence[int]:
    days, day = [], 0
    while True:
        day += int(rng.integers(1, 11))
        if day >= ANALYSIS_DAY:
            return days
        days.append(day)


SCHEDULES: dict[str, Schedule] = {
    "daily": lambda _rng: range(1, ANALYSIS_DAY),
    "every_third_day": lambda _rng: range(3, ANALYSIS_DAY, 3),
    "weekly": lambda _rng: range(7, ANALYSIS_DAY, 7),
    "irregular": _irregular,
    "twice": lambda _rng: (ANALYSIS_DAY // 2, ANALYSIS_DAY - 1),
}


@dataclass(frozen=True)
class Stream:
    """One simulated campaign's counts by the day each outcome is in: rows and outcomes of each group so far."""

    treated_rows: np.ndarray
    treated_hits: np.ndarray
    control_rows: np.ndarray
    control_hits: np.ndarray


@dataclass(frozen=True)
class Draw:
    control: np.ndarray
    entry: np.ndarray
    converted: np.ndarray


def _draw(rng: np.random.Generator, per_arm: int, base_rate: float, effect: float) -> Draw:
    n = 2 * per_arm
    control = np.zeros(n, dtype=bool)
    control[rng.choice(n, size=per_arm, replace=False)] = True
    entry = rng.integers(0, ENROL_DAYS, size=n)
    converted = rng.random(n) < np.where(control, base_rate, base_rate + effect)
    return Draw(control, entry, converted)


def _stream(draw: Draw) -> Stream:
    ready = draw.entry + WINDOW  # the day each outcome is in
    size = ANALYSIS_DAY + 1

    def cumulative(mask: np.ndarray) -> np.ndarray:
        return np.cumsum(np.bincount(ready[mask], minlength=size))

    treated = ~draw.control
    return Stream(
        cumulative(treated),
        cumulative(treated & draw.converted),
        cumulative(draw.control),
        cumulative(draw.control & draw.converted),
    )


def _fires(
    stream: Stream, day: int, *, mde: float, good: bool = True, settings: HarmAlarmSettings = SETTINGS
) -> bool:
    """The engine's decision on the reading of `day`, from its counts (`harm_looks` without the report)."""
    nt, nc = int(stream.treated_rows[day]), int(stream.control_rows[day])
    if min(nt, nc) < settings.min_rows_per_arm:
        return False
    difference, variance = rate_difference(
        nt, nc, stream.treated_hits[day] / nt, stream.control_hits[day] / nc
    )
    if difference is None or variance is None or not variance > 0.0:
        return False
    return judge(difference, variance, mixing_sd=mde, alpha=settings.alpha, outcome_is_good=good).fired


def _naive(stream: Stream, day: int) -> bool:
    """A plain one-sided 95% test of harm at this reading: what peeking without the mixture would do."""
    nt, nc = int(stream.treated_rows[day]), int(stream.control_rows[day])
    if min(nt, nc) < SETTINGS.min_rows_per_arm:
        return False
    difference, variance = rate_difference(
        nt, nc, stream.treated_hits[day] / nt, stream.control_hits[day] / nc
    )
    if difference is None or variance is None or not variance > 0.0:
        return False
    return difference / math.sqrt(variance) < NormalDist().inv_cdf(0.05)


def _alarm_share(
    *,
    base_seed: int,
    sims: int,
    per_arm: int,
    base_rate: float,
    effect: float,
    mde: float,
    schedule: Schedule,
    good: bool = True,
) -> tuple[float, float]:
    """The share of campaigns with any alarm before the analysis date, and the share a naive daily test flags."""
    alarms = naive = 0
    for seed in seeds(base_seed, sims):
        rng = np.random.default_rng(seed)
        draw = _draw(rng, per_arm, base_rate, effect)
        stream = _stream(draw)
        days = schedule(rng)
        alarms += any(_fires(stream, day, mde=mde, good=good) for day in days)
        naive += any(_naive(stream, day) for day in range(1, ANALYSIS_DAY))
    return alarms / sims, naive / sims


GATE_ROWS = 2 * int(n_for_mde(0.10, -0.03, power=0.80).n_treat or 0)
"""Twice the customers per group a single reading needs for 80% power at 10% and 3 points (b)."""


# --- (a) no effect, many refresh schedules ------------------------------------------------------------------
@pytest.mark.parametrize("schedule", sorted(SCHEDULES))
@pytest.mark.parametrize(
    ("case", "per_arm", "base_rate", "mde", "good"),
    [
        ("common", GATE_ROWS, 0.10, 0.03, True),
        ("rare_small", 1_500, 0.02, 0.01, True),
        ("to_prevent", GATE_ROWS, 0.10, 0.03, False),
    ],
)
def test_on_no_effect_the_alarm_fires_at_most_alpha_of_the_time_however_often_it_is_read(
    schedule: str, case: str, per_arm: int, base_rate: float, mde: float, good: bool
) -> None:
    base = (
        138_000
        + 100 * sorted(SCHEDULES).index(schedule)
        + {"common": 1, "rare_small": 2, "to_prevent": 3}[case]
    )
    share, naive = _alarm_share(
        base_seed=base,
        sims=SIMS_AA,
        per_arm=per_arm,
        base_rate=base_rate,
        effect=0.0,
        mde=mde,
        schedule=SCHEDULES[schedule],
        good=good,
    )
    _, high = band(ALPHA, SIMS_AA)
    print(
        f"\n[Harm alarm] A/A {case} read {schedule}: alarms {share:.2%} of {SIMS_AA:,}; naive daily {naive:.2%}"
    )
    assert share <= high, f"{share:.4f} of campaigns that change nothing raised the alarm, above {high:.4f}"
    if case != "to_prevent":  # the naive test reads the lower side only; for (c) the harmful side is up
        assert (
            naive > high
        ), f"reading a plain test every day flagged only {naive:.4f}: the guard proves nothing"


# --- (b) planted harm of the smallest effect -----------------------------------------------------------------
def test_planted_harm_of_the_smallest_effect_is_caught_before_the_analysis_date() -> None:
    caught, _ = _alarm_share(
        base_seed=138_501,
        sims=SIMS_POWER,
        per_arm=GATE_ROWS,
        base_rate=0.10,
        effect=-0.03,
        mde=0.03,
        schedule=SCHEDULES["daily"],
    )
    planner = int(n_for_mde(0.10, -0.03, power=0.80).n_treat or 0)
    at_planner, _ = _alarm_share(
        base_seed=138_502,
        sims=SIMS_POWER,
        per_arm=planner,
        base_rate=0.10,
        effect=-0.03,
        mde=0.03,
        schedule=SCHEDULES["daily"],
    )
    print(
        f"\n[Harm alarm] harm of 3 points caught before the analysis date: {caught:.2%} at {GATE_ROWS:,} per group; "
        f"{at_planner:.2%} at the planner's {planner:,} (reported, not gated)"
    )
    assert caught >= 0.80, f"only {caught:.4f} of harmful campaigns were caught before the analysis date"


# --- (c) the production chain gives the same decisions -----------------------------------------------------------
def _frames(draw: Draw) -> tuple[pd.DataFrame, pd.DataFrame]:
    n = len(draw.control)
    keys = np.char.add("C", np.char.zfill(np.arange(n).astype(str), 7))
    scores = pd.DataFrame({"customer_id": keys, "control_group": draw.control, "suppressed_reason": ""})
    sent = np.datetime64(START.date()) + draw.entry.astype("timedelta64[D]")
    outcomes = pd.DataFrame(
        {"customer_id": keys, "converted": draw.converted.astype(int), "treatment_date": sent.astype(str)}
    )
    return scores, outcomes


@pytest.mark.parametrize(
    ("effect", "base_seed"), [(0.0, 138_601), (-0.03, 138_602)], ids=["no-effect", "harm"]
)
def test_the_measured_reports_give_the_same_decision_at_every_reading(effect: float, base_seed: int) -> None:
    disagreements = alarms = 0
    logging.disable(logging.CRITICAL)
    try:
        for seed in seeds(base_seed, SIMS_CHAIN):
            draw = _draw(np.random.default_rng(seed), GATE_ROWS, 0.10, effect)
            stream = _stream(draw)
            scores, outcomes = _frames(draw)
            for day in SCHEDULES["weekly"](np.random.default_rng(0)):
                report = measure_campaign(
                    scores,
                    outcomes,
                    run_id="simulated",
                    primary_key="customer_id",
                    outcome_column="converted",
                    treatment_time=START,
                    treatment_date_column="treatment_date",
                    outcome_window_days=WINDOW,
                    as_of=START + timedelta(days=day),
                )
                assert (report.treated_rows, report.control_rows) == (
                    stream.treated_rows[day],
                    stream.control_rows[day],
                )
                (look,) = harm_looks(report, mixing_sd=0.03, outcome_is_good=True, settings=SETTINGS)
                disagreements += look.fired != _fires(stream, day, mde=0.03)
                alarms += look.fired
    finally:
        logging.disable(logging.NOTSET)
    print(
        f"\n[Harm alarm] production chain, effect {effect:+.2f}: {alarms} alarmed readings, {disagreements} disagree"
    )
    assert disagreements == 0
    if effect < 0:
        assert alarms > 0, "harm is seen through the chain"


# --- (d) inside an open window, as the monthly loop's interim reading counts outcomes ----------------------------
OPEN_WINDOW = 30
OPEN_ANALYSIS_DAY = OPEN_WINDOW + 14
"""The window closes on day 30; the plan's analysis date is two weeks later (readings in between are early looks)."""


def _running_stream(rng: np.random.Generator, per_arm: int, base_rate: float, effect: float) -> Stream:
    """Everyone sent on day 0 and counted from the start; an outcome counts from the day it happens."""
    n = 2 * per_arm
    control = np.zeros(n, dtype=bool)
    control[rng.choice(n, size=per_arm, replace=False)] = True
    converted = rng.random(n) < np.where(control, base_rate, base_rate + effect)
    happens = rng.integers(0, OPEN_WINDOW, size=n)  # the day each outcome happens, when it does

    def hits(mask: np.ndarray) -> np.ndarray:
        return np.cumsum(np.bincount(happens[mask & converted], minlength=OPEN_ANALYSIS_DAY + 1))

    days = OPEN_ANALYSIS_DAY + 1
    return Stream(
        np.full(days, int((~control).sum())),
        hits(~control)[:days],
        np.full(days, int(control.sum())),
        hits(control)[:days],
    )


@pytest.mark.parametrize("every", [1, 3, 7], ids=["daily", "every_third_day", "weekly"])
def test_on_no_effect_reading_an_open_window_as_the_loop_does_keeps_alarms_within_alpha(every: int) -> None:
    alarms = 0
    for seed in seeds(138_700 + every, SIMS_AA):
        rng = np.random.default_rng(seed)
        stream = _running_stream(rng, GATE_ROWS, 0.10, 0.0)
        alarms += any(_fires(stream, day, mde=0.03) for day in range(every, OPEN_ANALYSIS_DAY, every))
    share = alarms / SIMS_AA
    _, high = band(ALPHA, SIMS_AA)
    print(f"\n[Harm alarm] A/A open window read every {every} day(s): alarms {share:.2%} of {SIMS_AA:,}")
    assert share <= high, f"{share:.4f} of campaigns that change nothing raised the alarm, above {high:.4f}"


def test_planted_harm_is_caught_inside_the_open_window() -> None:
    caught = 0
    for seed in seeds(138_710, SIMS_POWER):
        stream = _running_stream(np.random.default_rng(seed), GATE_ROWS, 0.10, -0.03)
        caught += any(_fires(stream, day, mde=0.03) for day in range(1, OPEN_WINDOW))
    share = caught / SIMS_POWER
    print(f"\n[Harm alarm] harm of 3 points caught before the window closes, read daily: {share:.2%}")
    assert share >= 0.80, f"only {share:.4f} of harmful campaigns were caught inside the window"
