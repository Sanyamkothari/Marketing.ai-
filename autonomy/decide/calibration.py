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
`not_enough_data` (no expectation to check, no interval, or an interval that cannot rule 2x over in or out).

Opt-in: `build_agent_policy(calibrate=True)` shrinks every bid's value by the factor before the attention
price is taken off and the send/no-send choice is made (`scaled_net`). Only the value part, urgency x uplift
x value if converted, is shrunk; the channel and offer costs stay whole, and a bid whose value part is not
positive is left as it is, so a bid's net only ever goes down. The bid's uncertainty is left as it is. With
it off nothing in this module is called and every number is unchanged.

Pure maths and in-memory sums: no clock, no I/O, numpy only through `autonomy.proof`.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Final, Literal

import numpy as np

from autonomy.contracts import Bid, Decision, DecisionKind, Estimate, Group, ProofSummary, Reward
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
    "scaled_net",
    "shrink_factor",
    "unscaled_net",
    "value_part",
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


def value_part(bid: Bid) -> float:
    """What a bid expects the message to earn before any cost: urgency x uplift x value if converted.

    0.0 when it is not a positive, finite number: there is then nothing for the factor to shrink."""
    v = bid.urgency * bid.uplift * bid.value_if_converted
    return v if math.isfinite(v) and v > 0 else 0.0


def scaled_net(bid: Bid, factor: float) -> float:
    """The bid's expected net with its value part shrunk by `factor` and its costs left whole.

    expected_net - (1 - factor) x value part: with a factor in [0.25, 1] the net never goes up, a loss-making
    bid never looks less bad, and a factor of 1 gives the bid's own expected net exactly."""
    if factor >= 1.0 or not math.isfinite(bid.expected_net):
        return bid.expected_net
    return bid.expected_net - (1.0 - factor) * value_part(bid)


def unscaled_net(decision: Decision, factor: float) -> float:
    """A contact decision's expected net as it was before `factor` shrank its chosen bid (`scaled_net` undone)."""
    if factor >= 1.0:
        return decision.expected_net
    chosen = next((b for b in decision.bids if b.action == decision.action), None)
    if chosen is None:
        return decision.expected_net
    return decision.expected_net + (1.0 - factor) * value_part(chosen)


def measured_per_action(profit: Estimate | None, actions: int) -> Estimate | None:
    """The holdout's incremental profit (with its 95% range) spread over the actions behind it; None if none."""
    if profit is None or actions <= 0:
        return None
    n = float(actions)
    return Estimate(point=profit.point / n, low=profit.low / n, high=profit.high / n)


def shrink_factor(measured: Estimate | None, expected: float | None, *, prior_sd: float = PRIOR_SD) -> float:
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

    `expected` and `measured` are per action, `ci` is the 95% range of `measured`. With t = expected / 2:

    * the whole range is at or above t: it cannot be more than 2x over, so `over_1_5x` if expected / measured
      is above 1.5, else `ok`;
    * otherwise 2x over is possible: `over_2x` when the point itself is below t (a measured value of zero or
      less is) and the range is not wider than twice the expectation, or when the whole range is below t;
    * anything else, a range that cannot rule 2x over in or out, and any missing or unusable input, is
      `not_enough_data`."""
    if expected is None or measured is None or ci is None:
        return "not_enough_data"
    low, high = ci
    if not all(math.isfinite(v) for v in (expected, measured, low, high)) or expected <= 0 or low > high:
        return "not_enough_data"
    half = expected / OVER_2X
    if low >= half:
        return "over_1_5x" if expected / measured > OVER_1_5X else "ok"
    if high < half or (measured < half and high - low <= 2.0 * expected):
        return "over_2x"
    return "not_enough_data"


def proof_calibration(
    summary: ProofSummary, decisions: Iterable[Decision], sent: Collection[str] | None = None
) -> CalibrationStatus:
    """The status for a proof: `decisions` are the agent-group contact decisions whose windows the proof counted.

    `sent` is the ids of the decisions whose message actually went out (`autonomy.proof.sent_decisions`);
    when given, only those count as actions, so a Shadow decision, a rejected or expired batch or a failed
    send does not dilute the measured value per action. Expected is their mean `expected_net`, measured is
    the summary's incremental profit over their number."""
    acts = [
        d
        for d in decisions
        if d.group is Group.AGENT and d.kind is DecisionKind.ACT and (sent is None or d.decision_id in sent)
    ]
    per = measured_per_action(summary.incremental_profit, len(acts))
    if per is None:
        return "not_enough_data"
    expected = sum(d.expected_net for d in acts) / len(acts)
    return calibration_status(expected, per.point, (per.low, per.high))


class Calibration:
    """The agent's running factor, refitted from the holdout it can see.

    Two feeds reach the same fit, so a caller uses whichever it has:

    * **the agent's own events** (`record` for every decision it makes, `purchase` for every purchase it sees,
      `refit` once a week): per customer, the margin of purchases since the agent started less the cost of the
      messages sent in closed windows, and the group of their first decision. This is how the Gym and the
      runtime's nightly loop feed it, because the agent is handed events and decisions but not the holdout's
      rewards. The period is the one `autonomy.proof`'s last touch measures: the cutoff is `horizon_days`
      before now, and only purchases before the cutoff count, for every customer alike, with the cost, the
      number and the expected nets of the contact decisions made at or before it. Purchases after the cutoff,
      which the later (still open) sends helped cause, wait for the next refit.
    * **closed rewards** (`observe`), for a caller that already has the proof's rewards for every group.

    The logged `expected_net` of a decision already carries the factor in force when it was made, so it is
    undone (`unscaled_net`) (`record` knows the factor; `note` records it for `observe`; a decision this process
    did not make is taken to have been made at the current factor).
    """

    REFIT_EVERY = timedelta(days=7)

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
        self.refit_at: datetime | None = None
        self._applied: dict[str, float] = {}
        # the closed-rewards feed
        self._seen: set[str] = set()
        self._first: dict[str, tuple[datetime, Group]] = {}
        self._profit: dict[str, float] = {}
        self._expected_sum = 0.0
        # the event feed
        self._start: datetime | None = None
        self._group: dict[str, tuple[datetime, Group]] = {}
        self._purchases: list[tuple[datetime, str, float]] = []
        """(when, customer, margin less offer cost) of each purchase after the start."""
        self._sent: list[tuple[datetime, str, float, float]] = []
        """(made_at, customer, message cost, expected net before the factor) of each contact decision."""

    def scale(self) -> float:
        """The factor to multiply every bid's expected value by (the policy's hook)."""
        return self.factor

    # --- the event feed ---------------------------------------------------------------------------

    def record(self, decision: Decision, cost: float = 0.0) -> None:
        """A decision the agent has just made at the current factor, with the cost of the message it sends."""
        c = decision.customer_id
        if self._start is None or decision.made_at < self._start:
            self._start = decision.made_at
        first = self._group.get(c)
        if first is None or decision.made_at < first[0]:
            self._group[c] = (decision.made_at, decision.group)
        if decision.group is Group.AGENT and decision.kind is DecisionKind.ACT:
            self._sent.append((decision.made_at, c, max(cost, 0.0), unscaled_net(decision, self.factor)))

    def purchase(self, customer_id: str, ts: datetime, margin: float, offer_cost: float = 0.0) -> None:
        """A purchase seen (its margin, less the cost of the offer it redeemed); only those after the start count."""
        if self._start is not None and ts > self._start:
            self._purchases.append((ts, customer_id, margin - offer_cost))

    def refit(self, now: datetime, horizon_days: float, *, force: bool = False) -> float:
        """Refit from the events so far (at most once a week unless `force`) and return the factor."""
        if not force and self.refit_at is not None and now - self.refit_at < self.REFIT_EVERY:
            return self.factor
        self.refit_at = now
        cutoff = now - timedelta(days=horizon_days)
        cost: dict[str, float] = {}
        actions, expected_sum = 0, 0.0
        for made_at, c, spent, raw in self._sent:
            if made_at <= cutoff:
                cost[c] = cost.get(c, 0.0) + spent
                actions += 1
                expected_sum += raw
        margin: dict[str, float] = {}
        for ts, c, m in self._purchases:
            if ts < cutoff:  # as autonomy.proof's last touch: the same cutoff for every customer
                margin[c] = margin.get(c, 0.0) + m
        by_group: dict[Group, list[float]] = {Group.AGENT: [], Group.HOLDOUT: []}
        for c, (first_at, group) in self._group.items():
            if first_at <= cutoff and group in by_group:
                by_group[group].append(margin.get(c, 0.0) - cost.get(c, 0.0))
        return self._fit(by_group[Group.AGENT], by_group[Group.HOLDOUT], actions, expected_sum)

    # --- the closed-rewards feed ------------------------------------------------------------------

    def note(self, decision: Decision) -> None:
        """Remember the factor a contact decision was made at, so its logged expected net can be undone."""
        if decision.group is Group.AGENT and decision.kind is DecisionKind.ACT:
            self._applied[decision.decision_id] = self.factor

    def observe(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> float:
        """Take newly closed rewards of every group (each counted once), refit and return the factor."""
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
                self._expected_sum += unscaled_net(d, applied)
        if changed:
            by_group: dict[Group, list[float]] = {Group.AGENT: [], Group.HOLDOUT: []}
            for customer, (_, group) in self._first.items():
                if group in by_group:
                    by_group[group].append(self._profit.get(customer, 0.0))
            self._fit(by_group[Group.AGENT], by_group[Group.HOLDOUT], self.actions, self._expected_sum)
        return self.factor

    # --- the fit ----------------------------------------------------------------------------------

    def _fit(self, agent: list[float], holdout: list[float], actions: int, expected_sum: float) -> float:
        self.measured, self.expected, self.actions = None, None, actions
        if len(agent) < self.min_group or len(holdout) < self.min_group or actions == 0:
            self.factor = FACTOR_MAX
            return self.factor
        total = welch_difference(
            np.asarray(agent, dtype=np.float64),
            np.asarray(holdout, dtype=np.float64),
            scale=float(len(agent)),
        )
        self.measured = measured_per_action(total, actions)
        self.expected = expected_sum / actions
        self.factor = shrink_factor(self.measured, self.expected, prior_sd=self.prior_sd)
        return self.factor
