"""Calibration: how much of what the agent expects a message to earn is real, and what to do about it.

docs/v3/DIAGNOSE_RESULTS.md H5 measured it: the agent expects 1.14x to 1.52x what a message really
earns, on every Gym scenario and both real datasets. This module is the correction and the check.

**The factor** (`shrink_factor`, `Calibration`). Per action, two numbers over the same window:

* measured = the holdout's incremental profit (`autonomy.proof`, agent group against the people we never
  contact) divided by the number of actions the agent took in that window;
* expected = the mean `expected_net` of those same chosen actions, before the factor.

The factor is `measured / expected`, shrunk toward 1 when the holdout is small and clipped to
[`FACTOR_MIN`, `FACTOR_MAX`] = [0.25, 1.0]: it only ever shrinks a bid, never inflates one, and it never
shrinks one below a quarter.

**The prior.** Before any data the factor is Normal(1, 0.25^2): the agent is taken to be right, and an
over-valuation of 2x (factor 0.5) is two standard deviations away. The measured ratio has its own standard
error (from the 95% interval of the incremental profit); the posterior mean is the precision-weighted
mean of the prior and the measurement. A holdout too small to measure (fewer than `MIN_GROUP` customers in
either group, no actions, an expected net that is not positive) gives exactly 1.0, so a new shop is never
shrunk on noise. The interval is the one `autonomy.proof` already reports; nothing is bootstrapped here.

**The gate** (`calibration_status`). A plain verdict on expected against measured per action, shown on the
proof (`ProofSummary.calibration`) and read by the Autopilot gate (`autonomy.runtime.evidence_gate`):
`ok` (the agent expects at most 1.5x what the holdout shows), `over_1_5x`, `over_2x` (more than twice), or
`not_enough_data` (no expectation to check, no interval, or an interval too wide to tell zero from
double the forecast).

Opt-in: `build_agent_policy(calibrate=True)` multiplies every bid's `expected_net` by the factor before
the attention price is taken off and the send/no-send choice is made. The bid's uncertainty is left as it
is. With it off nothing in this module is called and every number is unchanged.

Pure maths and in-memory sums: no clock, no I/O, numpy only through `autonomy.proof`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Final, Literal

import numpy as np

from autonomy.contracts import Decision, DecisionKind, Estimate, Group, ProofSummary, Reward
from autonomy.proof.estimate import MIN_GROUP, Z_95, welch_difference

__all__ = [
    "FACTOR_MAX",
    "FACTOR_MIN",
    "OVER_1_5X",
    "OVER_2X",
    "PRIOR_MEAN",
    "PRIOR_SD",
    "Calibration",
    "CalibrationStatus",
    "calibration_status",
    "measured_per_action",
    "proof_calibration",
    "shrink_factor",
]

CalibrationStatus = Literal["ok", "over_2x", "over_1_5x", "not_enough_data"]

FACTOR_MIN: Final[float] = 0.25
FACTOR_MAX: Final[float] = 1.0
"""The factor never inflates a bid (1.0) and never takes more than three quarters off it (0.25)."""
PRIOR_MEAN: Final[float] = 1.0
PRIOR_SD: Final[float] = 0.25
"""The prior on the factor: Normal(1, 0.25^2), see the module docstring."""
OVER_1_5X: Final[float] = 1.5
OVER_2X: Final[float] = 2.0
"""`calibration_status` calls an expectation above 1.5x the measured value `over_1_5x`, above 2x `over_2x`."""

_MIN_SE: Final[float] = 1e-9


def measured_per_action(profit: Estimate | None, actions: int) -> Estimate | None:
    """The holdout's incremental profit (with its 95% range) spread over the actions behind it; None if none."""
    if profit is None or actions <= 0:
        return None
    n = float(actions)
    return Estimate(point=profit.point / n, low=profit.low / n, high=profit.high / n)


def shrink_factor(
    measured: Estimate | None, expected: float | None, *, prior_sd: float = PRIOR_SD
) -> float:
    """The factor to multiply the agent's expected value by: measured / expected, shrunk toward 1, in [0.25, 1].

    `measured` is the measured incremental value per action with its 95% range, `expected` the agent's mean
    expected net per chosen action over the same window. Anything missing, or an expectation that is not
    positive, gives 1.0 (no evidence, no correction)."""
    if measured is None or expected is None or not math.isfinite(expected) or expected <= 0:
        return FACTOR_MAX
    if not all(math.isfinite(v) for v in (measured.point, measured.low, measured.high)):
        return FACTOR_MAX
    ratio = measured.point / expected
    se = max((measured.high - measured.low) / (2.0 * Z_95) / expected, _MIN_SE)
    prior_precision = 1.0 / (prior_sd * prior_sd)
    data_precision = 1.0 / (se * se)
    posterior = (PRIOR_MEAN * prior_precision + ratio * data_precision) / (prior_precision + data_precision)
    return min(FACTOR_MAX, max(FACTOR_MIN, posterior))


def calibration_status(
    expected: float | None, measured: float | None, ci: tuple[float, float] | None
) -> CalibrationStatus:
    """Whether the agent's expected value per action holds up against the measured value per action.

    `expected` and `measured` are per action, `ci` is the 95% range of `measured`. `not_enough_data` when there
    is no positive expectation to check, no measurement, or a range wider than twice the expectation (it could
    not tell zero from double the forecast). Otherwise by expected / measured: above 2 is `over_2x`, above 1.5
    `over_1_5x`, else `ok`; a measured value of zero or less is `over_2x`."""
    if expected is None or measured is None or ci is None:
        return "not_enough_data"
    low, high = ci
    if not all(math.isfinite(v) for v in (expected, measured, low, high)) or expected <= 0 or low > high:
        return "not_enough_data"
    if high - low > 2.0 * expected:
        return "not_enough_data"
    if measured <= 0 or expected / measured > OVER_2X:
        return "over_2x"
    return "over_1_5x" if expected / measured > OVER_1_5X else "ok"


def proof_calibration(summary: ProofSummary, decisions: Iterable[Decision]) -> CalibrationStatus:
    """The status for a proof: `decisions` are the agent-group contact decisions whose windows the proof counted.

    Expected is their mean `expected_net`, measured is the summary's incremental profit over their number."""
    acts = [d for d in decisions if d.group is Group.AGENT and d.kind is DecisionKind.ACT]
    per = measured_per_action(summary.incremental_profit, len(acts))
    if per is None:
        return "not_enough_data"
    expected = sum(d.expected_net for d in acts) / len(acts)
    return calibration_status(expected, per.point, (per.low, per.high))


class Calibration:
    """The agent's running factor, refitted from the closed outcomes it is handed (`observe`).

    It keeps, per customer, the sum of margin less cost over closed windows and the group of their earliest
    decision (as `autonomy.proof` does), and over the agent group's contact decisions their count and the sum
    of their expected nets before the factor. `observe` refits `factor` from the whole history so far. The
    logged `expected_net` already carries the factor in force when a decision was made (`note`), so it is
    divided back out; a decision this process did not make is taken to have been made at the current factor.
    """

    def __init__(self, *, prior_sd: float = PRIOR_SD, min_group: int = MIN_GROUP) -> None:
        if prior_sd <= 0:
            raise ValueError("prior_sd must be positive")
        self.prior_sd = prior_sd
        self.min_group = min_group
        self.factor = FACTOR_MAX
        """The factor in force: 1.0 until the holdout can be measured."""
        self.measured: Estimate | None = None
        """Measured incremental value per action at the last refit, or None."""
        self.expected: float | None = None
        """The agent's mean expected net per chosen action (before the factor) at the last refit, or None."""
        self.actions = 0
        self._applied: dict[str, float] = {}
        self._seen: set[str] = set()
        self._first: dict[str, tuple[datetime, Group]] = {}
        self._profit: dict[str, float] = {}
        self._expected_sum = 0.0

    def scale(self) -> float:
        """The factor to multiply every bid's expected value by (the policy's hook)."""
        return self.factor

    def note(self, decision: Decision) -> None:
        """Remember the factor a contact decision was made at, so its logged expected net can be undone."""
        if decision.group is Group.AGENT and decision.kind is DecisionKind.ACT:
            self._applied[decision.decision_id] = self.factor

    def observe(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> float:
        """Take newly closed rewards (each counted once), refit and return the factor."""
        changed = False
        for r in rewards:
            d = decisions.get(r.decision_id)
            if d is None or r.decision_id in self._seen:
                continue
            self._seen.add(r.decision_id)
            changed = True
            first = self._first.get(r.customer_id)
            if first is None or d.made_at < first[0]:
                self._first[r.customer_id] = (d.made_at, d.group)
            self._profit[r.customer_id] = self._profit.get(r.customer_id, 0.0) + r.margin - r.cost
            if d.group is Group.AGENT and d.kind is DecisionKind.ACT:
                applied = self._applied.pop(d.decision_id, self.factor)
                self.actions += 1
                self._expected_sum += d.expected_net / applied if applied > 0 else d.expected_net
        if changed:
            self._refit()
        return self.factor

    def _refit(self) -> None:
        by_group: dict[Group, list[float]] = {Group.AGENT: [], Group.HOLDOUT: []}
        for customer, (_, group) in self._first.items():
            if group in by_group:
                by_group[group].append(self._profit.get(customer, 0.0))
        agent, holdout = by_group[Group.AGENT], by_group[Group.HOLDOUT]
        self.measured, self.expected = None, None
        if len(agent) < self.min_group or len(holdout) < self.min_group or self.actions == 0:
            self.factor = FACTOR_MAX
            return
        total = welch_difference(
            np.asarray(agent, dtype=np.float64), np.asarray(holdout, dtype=np.float64), scale=float(len(agent))
        )
        self.measured = measured_per_action(total, self.actions)
        self.expected = self._expected_sum / self.actions
        self.factor = shrink_factor(self.measured, self.expected, prior_sd=self.prior_sd)
