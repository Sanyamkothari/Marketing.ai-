"""The proof numbers: money the agent made against the no-contact holdout, and against the simple
baseline, with 95% intervals (plan-v2 section 8).

**The unit is the customer.** Each customer's rewards are summed (so build them with last-touch
attribution, `proof_rewards()`, which counts every purchase once), and the three groups are
compared per customer. Groups are random by construction (`split.assign_group`), so a plain
difference in means is an unbiased estimate of what being in the agent group did for a customer.

* **Incremental revenue / profit** = (mean in agent - mean in holdout) x number of agent customers:
  the money the agent made over leaving those same customers alone. Profit is margin - cost per
  customer; the holdout costs nothing.
* **Lift vs baseline** = mean profit per customer in agent - in baseline: what the agent adds over
  the simple rule, per customer.
* **Spend** is the agent group's total cost (messages and redeemed offers).
* **ROI** = incremental profit / spend: the return on each unit spent *after* getting it back (0.5
  means every 1 spent came back plus 0.5). Spend is a known total, so its interval is the profit
  interval divided by spend.

**Intervals.** By default a normal approximation with Welch's variance:
`d ± z·sqrt(s1²/n1 + s0²/n0)`, `z = 1.95996`. With `method="bootstrap"`, customers are resampled
with replacement within each group (`n_boot` times, seeded) and the 2.5% and 97.5% percentiles are
taken; it is slower and better for very skewed spend.

**What the comparison can see.** Next to the incremental figures the summary carries their minimum
detectable effect (`autonomy.proof.power`): the smallest true gain a comparison of this many customers,
with this much spread, would reliably tell from nothing (95% confidence, 80% power). A small shop's
proof says "this size of shop can only see lifts above X%" instead of leaving a flat result to be read
as "no effect". Every closed window of a customer is summed into one figure, so the MDE is for the
whole period measured: waiting for more months pools more windows and lowers it.

**The margin.** Profit is margin less cost, and a purchase with no recorded margin counts at the
shop's margin rate (`Costs.margin_rate`). Given `costs`, the summary says when that rate is not set
(purchases are then counted at full value) or how much of the margin rests on it. The profit figures are
margin less cost whatever the goal's objective, so the note applies to them for a revenue or conversion
goal too. The MDE sentence and the margin note are in their own fields, not in `notes`.

**When we say nothing.** If the agent group or the holdout has fewer than `min_group` measured
customers, the incremental figures are None and a note says why; the same for the baseline and the
lift. Per-goal proof needs the goal to run alone: with one programme-wide holdout, two goals acting
on the same customers can't be told apart, so a per-goal summary refuses in that case.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from statistics import NormalDist
from typing import Final, Literal

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Decision, Detectable, Estimate, Group, ProofSummary, Reward
from autonomy.proof.power import minimum_detectable_difference
from autonomy.proof.rewards import Costs

__all__ = [
    "MIN_GROUP",
    "Z_95",
    "bootstrap_difference",
    "proof_summary",
    "welch_difference",
]

FloatArray = npt.NDArray[np.float64]

Z_95: Final[float] = NormalDist().inv_cdf(0.975)
MIN_GROUP: Final[int] = 30
"""Fewest measured customers per group before we put a number on a comparison."""

Method = Literal["normal", "bootstrap"]


def _estimate(point: float, low: float, high: float) -> Estimate:
    """An `Estimate`, with the bounds widened to the point if rounding left it just outside."""
    return Estimate(point=point, low=min(low, point), high=max(high, point))


def welch_difference(a: FloatArray, b: FloatArray, *, scale: float = 1.0) -> Estimate | None:
    """`scale x (mean(a) - mean(b))` with a 95% normal interval on Welch's variance.

    None when either side has fewer than two values (no variance can be estimated).
    """
    x, y = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if len(x) < 2 or len(y) < 2:
        return None
    diff = float(x.mean() - y.mean())
    se = math.sqrt(float(x.var(ddof=1)) / len(x) + float(y.var(ddof=1)) / len(y))
    lo, hi = diff - Z_95 * se, diff + Z_95 * se
    if scale < 0:
        lo, hi = hi, lo
    return _estimate(scale * diff, scale * lo, scale * hi)


def bootstrap_difference(
    a: FloatArray, b: FloatArray, *, scale: float = 1.0, n_boot: int = 2000, seed: int = 0
) -> Estimate | None:
    """`scale x (mean(a) - mean(b))` with a 95% percentile bootstrap interval, resampling each side."""
    x, y = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if len(x) < 2 or len(y) < 2:
        return None
    rng = np.random.default_rng(seed)
    draws = _boot_means(x, n_boot, rng) - _boot_means(y, n_boot, rng)
    lo, hi = np.percentile(scale * draws, [2.5, 97.5])
    return _estimate(scale * float(x.mean() - y.mean()), float(lo), float(hi))


def _boot_means(x: FloatArray, n_boot: int, rng: np.random.Generator) -> FloatArray:
    idx = rng.integers(0, len(x), size=(n_boot, len(x)))
    means: FloatArray = x[idx].mean(axis=1)
    return means


def proof_summary(
    rewards: Sequence[Reward],
    decisions: Iterable[Decision] | Mapping[str, Decision],
    now: datetime,
    goal_id: str | None = None,
    *,
    min_group: int = MIN_GROUP,
    method: Method = "normal",
    n_boot: int = 2000,
    seed: int = 0,
    costs: Costs | None = None,
) -> ProofSummary:
    """The proof as of `now`: incremental money vs holdout, lift vs baseline, spend and ROI.

    `costs`, when given, lets the summary report the shop's margin setting (`margin_rate`,
    `margin_note`); without it nothing is said about the margin.

    `rewards` should be last-touch rewards (`proof_rewards()`) built with this same `now`, so every
    customer is measured over the same period; rewards whose window ends after `now`, or whose
    decision is not in `decisions`, are left out. A customer's group is the group of their earliest
    decision (intention to treat).
    """
    by_id = dict(decisions) if isinstance(decisions, Mapping) else {d.decision_id: d for d in decisions}
    notes: list[str] = []

    group_of: dict[str, Group] = {}
    switched: set[str] = set()
    for d in sorted(by_id.values(), key=lambda d: (d.made_at, d.decision_id)):
        if d.customer_id not in group_of:
            group_of[d.customer_id] = d.group
        elif group_of[d.customer_id] is not d.group:
            switched.add(d.customer_id)
    if switched:
        notes.append(
            f"{len(switched):,} customers changed group part-way; each is counted in the group they "
            "started in."
        )

    usable = [r for r in rewards if r.window_end <= now and r.decision_id in by_id]
    unknown = sum(1 for r in rewards if r.decision_id not in by_id)
    if unknown:
        notes.append(f"{unknown:,} rewards had no matching decision and were left out.")
    refuse_goal = False
    if goal_id is not None:
        others = sorted({r.goal_id for r in usable if r.goal_id is not None and r.goal_id != goal_id})
        if others:
            refuse_goal = True
            notes.append(
                "Other goals acted on the same customers, so this goal's own effect can't be separated "
                "from theirs with one shared holdout; read the programme-wide proof instead."
            )
        usable = [r for r in usable if r.goal_id in (goal_id, None)]

    revenue: dict[str, float] = {}
    profit: dict[str, float] = {}
    cost: dict[str, float] = {}
    for r in usable:
        revenue[r.customer_id] = revenue.get(r.customer_id, 0.0) + r.revenue
        profit[r.customer_id] = profit.get(r.customer_id, 0.0) + r.margin - r.cost
        cost[r.customer_id] = cost.get(r.customer_id, 0.0) + r.cost

    def arm(values: Mapping[str, float], group: Group) -> FloatArray:
        return np.array([v for c, v in sorted(values.items()) if group_of.get(c) is group], dtype=np.float64)

    rev_a, rev_h = arm(revenue, Group.AGENT), arm(revenue, Group.HOLDOUT)
    prof_a, prof_h, prof_b = arm(profit, Group.AGENT), arm(profit, Group.HOLDOUT), arm(profit, Group.BASELINE)
    cost_a = arm(cost, Group.AGENT)
    agent_n, holdout_n, baseline_n = len(prof_a), len(prof_h), len(prof_b)
    spend = float(cost_a.sum())

    def compare(a: FloatArray, b: FloatArray, scale: float, salt: int) -> Estimate | None:
        if method == "bootstrap":
            return bootstrap_difference(a, b, scale=scale, n_boot=n_boot, seed=seed * 7919 + salt)
        return welch_difference(a, b, scale=scale)

    inc_rev: Estimate | None = None
    inc_profit: Estimate | None = None
    roi: Estimate | None = None
    if refuse_goal:
        pass
    elif agent_n < min_group or holdout_n < min_group:
        notes.append(
            f"Too few customers measured to compare with the holdout yet ({agent_n:,} agent, "
            f"{holdout_n:,} holdout; at least {min_group:,} each are needed), so no incremental "
            "figure is claimed."
        )
    else:
        inc_rev = compare(rev_a, rev_h, float(agent_n), 1)
        inc_profit = compare(prof_a, prof_h, float(agent_n), 2)
        if inc_profit is not None and spend > 0:
            roi = _roi(inc_profit, spend, prof_a, prof_h, cost_a, method, n_boot, seed)
        elif inc_profit is not None:
            notes.append("Nothing was spent in the agent group, so there is no return on spend to show.")

    lift: Estimate | None = None
    if refuse_goal:
        pass
    elif agent_n < min_group or baseline_n < min_group:
        notes.append(
            f"Too few customers measured to compare with the baseline yet ({agent_n:,} agent, "
            f"{baseline_n:,} baseline; at least {min_group:,} each are needed)."
        )
    else:
        lift = compare(prof_a, prof_b, 1.0, 3)

    if inc_rev is not None or lift is not None:
        how = (
            f"a bootstrap of {n_boot:,} resamples"
            if method == "bootstrap"
            else "a normal approximation with Welch's variance"
        )
        notes.append(
            f"Ranges are 95% intervals from {how}; incremental money is the per-customer difference "
            f"from the holdout times the {agent_n:,} agent customers."
        )

    detectable: Detectable | None = None
    if not refuse_goal and agent_n >= min_group and holdout_n >= min_group:
        rule = prof_b if baseline_n >= min_group else prof_b[:0]
        detectable = _detectable(usable, by_id, prof_a, prof_h, rev_a, rev_h, rule)
        if detectable is None:
            notes.append(
                "Profit per customer does not vary yet in either group (nobody bought, say), so there is "
                "nothing to say about how small a gain this comparison could see."
            )
    # The two caveats have their own fields (`detectable.sentence`, `margin_note`), shown under the
    # headline; they are not repeated in `notes`.
    margin_note = _margin_note(usable, costs)

    return ProofSummary(
        goal_id=goal_id,
        as_of=now,
        agent_n=agent_n,
        baseline_n=baseline_n,
        holdout_n=holdout_n,
        incremental_revenue=inc_rev,
        incremental_profit=inc_profit,
        lift_vs_baseline=lift,
        spend=spend,
        roi=roi,
        notes=tuple(notes),
        detectable=detectable,
        margin_rate=costs.margin_rate if costs is not None else None,
        margin_note=margin_note,
    )


def _detectable(
    usable: Sequence[Reward],
    by_id: Mapping[str, Decision],
    prof_a: FloatArray,
    prof_h: FloatArray,
    rev_a: FloatArray,
    rev_h: FloatArray,
    prof_b: FloatArray,
) -> Detectable | None:
    """The minimum detectable effect of the incremental figures, in their own units."""
    n = float(len(prof_a))
    per_customer = minimum_detectable_difference(prof_a, prof_h)
    if per_customer is None:
        return None
    revenue = minimum_detectable_difference(rev_a, rev_h)
    versus_rule = minimum_detectable_difference(prof_a, prof_b)
    holdout_mean = float(prof_h.mean())
    relative = per_customer / holdout_mean if holdout_mean > 0 else None
    starts = [by_id[r.decision_id].made_at for r in usable]
    days = max(0, (max(r.window_end for r in usable) - min(starts)).days) if usable else 0
    who = f"{len(prof_a):,} agent and {len(prof_h):,} holdout customers measured over {days:,} days"
    if relative is not None:
        what = f"profit per customer more than {relative:.0%} above the holdout's"
    else:
        what = f"a gain of more than {per_customer:,.2f} in profit per customer"
    sentence = (
        f"With {who}, this comparison can only see {what}; a real gain smaller than that would look like "
        "noise. Every closed window is already pooled, so more months lower this bar."
    )
    return Detectable(
        profit=per_customer * n,
        revenue=revenue * n if revenue is not None else None,
        lift_vs_baseline=versus_rule,
        relative=relative,
        period_days=days,
        sentence=sentence,
    )


def _margin_note(usable: Sequence[Reward], costs: Costs | None) -> str | None:
    """Says when the profit rests on a margin the shop never gave (or on the one it did)."""
    if costs is None:
        return None
    total = sum(r.margin for r in usable)
    assumed = sum(r.margin_assumed for r in usable)
    if assumed <= 0 or total <= 0:
        return None
    share = assumed / total
    if costs.margin_rate is None:
        return (
            "No shop margin is set. "
            f"{share:.0%} of the margin counted here comes from purchases that carry no margin, and those "
            "are counted at their full value, so profit is overstated. Set the shop's contribution margin "
            "(after shipping, gateway fees and returns to origin) to correct it."
        )
    return (
        f"Purchases with no recorded margin are counted at the shop's margin of {costs.margin_rate:.0%} "
        f"({share:.0%} of the margin counted here)."
    )


def _roi(
    inc_profit: Estimate,
    spend: float,
    prof_a: FloatArray,
    prof_h: FloatArray,
    cost_a: FloatArray,
    method: Method,
    n_boot: int,
    seed: int,
) -> Estimate:
    """Incremental profit per unit spent. The bootstrap resamples spend along with profit."""
    point = inc_profit.point / spend
    if method != "bootstrap":
        return _estimate(point, inc_profit.low / spend, inc_profit.high / spend)
    rng = np.random.default_rng(seed * 7919 + 4)
    idx = rng.integers(0, len(prof_a), size=(n_boot, len(prof_a)))
    gain = prof_a[idx].mean(axis=1) - _boot_means(prof_h, n_boot, rng)
    spent = cost_a[idx].mean(axis=1)
    ratios = gain[spent > 0] / spent[spent > 0]
    if len(ratios) < 2:
        return _estimate(point, inc_profit.low / spend, inc_profit.high / spend)
    lo, hi = np.percentile(ratios, [2.5, 97.5])
    return _estimate(point, float(lo), float(hi))
