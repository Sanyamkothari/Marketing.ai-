"""The weekly monitor: compare the forecast with what happened, and say when to run the loop again.

When a policy is selected, the evaluation gives a FORECAST: the gain per customer over sending
nothing, measured on customers it was not fitted on (`Selection.forecast`). Every week the live
system measures the same quantity against the people it never contacts (the holdout group,
`autonomy.proof`): the ACTUAL. `check_week` compares them with two rules, written down beforehand:

1. **Proof negative.** The actual's 95% interval lies wholly below zero: the policy is losing money
   against not contacting. Re-run, whatever the sample size (an interval wholly below zero is
   already evidence).
2. **Drift above 2x.** The forecast was a positive gain and the actual is more than twice it or less
   than half of it (a zero or negative actual counts as less than half). Judged only when the week
   has at least `MIN_ACTUAL_N` customers and the actual's interval is narrower than the forecast
   itself (otherwise the week cannot tell 2x from 1x and the answer is `wait`, never a false alarm).

A forecast of zero or less (the safe default was 'send nothing') has no ratio to drift from: only
rule 1 applies. Pure: nothing here reads a clock or a store; the caller passes each week in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from autonomy.contracts import Estimate
from autonomy.scientist.select import Selection

__all__ = [
    "MIN_ACTUAL_N",
    "RATIO_BAND",
    "Forecast",
    "Monitor",
    "MonitorSignal",
    "WeeklyActual",
    "check_week",
    "forecast_of",
]

RATIO_BAND = 2.0
MIN_ACTUAL_N = 200


@dataclass(frozen=True)
class Forecast:
    """What the evaluation said the running policy would earn per customer over sending nothing."""

    label: str
    gain: Estimate
    n_held_out: int


@dataclass(frozen=True)
class WeeklyActual:
    """One week of the live comparison against the never-contacted group."""

    week: str
    gain: Estimate
    """Measured gain per customer over the never-contacted group, with its 95% interval."""
    n: int
    """Customers in the week's comparison (the smaller of the two groups is what limits it)."""


@dataclass(frozen=True)
class MonitorSignal:
    week: str
    status: Literal["ok", "rerun", "wait"]
    reasons: tuple[str, ...]
    ratio: float | None
    forecast: float
    actual: float

    @property
    def rerun(self) -> bool:
        return self.status == "rerun"

    @property
    def explanation(self) -> str:
        head = {
            "ok": f"{self.week}: on track",
            "rerun": f"{self.week}: run the Scientist again",
            "wait": f"{self.week}: too early to tell",
        }[self.status]
        return head + (": " + "; ".join(self.reasons) if self.reasons else "") + "."


def forecast_of(selection: Selection) -> Forecast | None:
    """The forecast of what a selection will run (None when the run could not be evaluated)."""
    ev = selection.evaluations
    if selection.forecast is None or ev is None:
        return None
    return Forecast(selection.run.label(ev.arm_names), selection.forecast, ev.n_held)


def check_week(
    forecast: Forecast,
    actual: WeeklyActual,
    *,
    band: float = RATIO_BAND,
    min_n: int = MIN_ACTUAL_N,
) -> MonitorSignal:
    """Judge one week against the forecast (rules in the module docstring)."""
    if band <= 1.0:
        raise ValueError("band must be above 1")
    f, a = forecast.gain.point, actual.gain.point
    reasons: list[str] = []
    if actual.gain.high < 0.0:
        reasons.append(
            f"the proof is negative: gain {a:+.4g} per customer, 95% interval {actual.gain.low:+.4g} to "
            f"{actual.gain.high:+.4g}, wholly below zero"
        )
        return MonitorSignal(actual.week, "rerun", tuple(reasons), None if f <= 0 else a / f, f, a)
    if f <= 0.0:
        return MonitorSignal(actual.week, "ok", (), None, f, a)
    if actual.n < min_n:
        return MonitorSignal(
            actual.week,
            "wait",
            (f"only {actual.n} customers this week (at least {min_n} are needed)",),
            None,
            f,
            a,
        )
    half = (actual.gain.high - actual.gain.low) / 2.0
    if half > f:
        return MonitorSignal(
            actual.week,
            "wait",
            (f"this week's gain is known only to within {half:.4g}, wider than the forecast {f:.4g}",),
            a / f,
            f,
            a,
        )
    ratio = a / f
    if ratio > band or ratio < 1.0 / band:
        way = "more than" if ratio > band else "less than"
        limit = band if ratio > band else 1.0 / band
        reasons.append(
            f"the actual gain {a:+.4g} per customer is {way} {limit:g}x the forecast {f:.4g} (ratio {ratio:.2f})"
        )
        return MonitorSignal(actual.week, "rerun", tuple(reasons), ratio, f, a)
    return MonitorSignal(actual.week, "ok", (), ratio, f, a)


@dataclass
class Monitor:
    """Weekly bookkeeping around `check_week`: one forecast, the signals so far."""

    forecast: Forecast
    band: float = RATIO_BAND
    min_n: int = MIN_ACTUAL_N
    history: list[MonitorSignal] = field(default_factory=list)

    def observe(self, actual: WeeklyActual) -> MonitorSignal:
        """Judge a week and remember it. A week already seen is replaced, not counted twice."""
        signal = check_week(self.forecast, actual, band=self.band, min_n=self.min_n)
        self.history = [s for s in self.history if s.week != actual.week] + [signal]
        return signal

    @property
    def should_rerun(self) -> bool:
        """True when the latest week asks for a re-run."""
        return bool(self.history) and self.history[-1].rerun
