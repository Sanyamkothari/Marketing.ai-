"""Reading an experiment: each arm's gain over the holdout, and the call to continue, stop or crown.

**What is compared.** The unit is the customer, as in `autonomy.proof.estimate`: each customer's
reward values are summed (build the rewards with last-touch attribution, `proof_rewards()`, which counts
every purchase once) and arms are compared per customer. A customer belongs to the arm of their first
decision inside the experiment (intention to treat: a customer assigned to an arm counts in it whether
or not the agent then chose to send, and whether or not the message arrived). The reference group is
the HOLDOUT customers who match the experiment's audience, which the policy tags in the decision log
(`HOLDOUT_BUCKET`); they were never contacted and are a random sample of the audience, as the arms are.

**Intervals.** For every arm the reading carries the gain over the holdout twice: the plain 95% interval
from `autonomy.proof.welch_difference` (what the single-look proof code says; for display), and an
always-valid interval (`autonomy.experiments.sequential`) that is the only one the decisions use. For a
challenger it also carries the always-valid gain over the reference arm. The experiment's `alpha` is split
by Bonferroni over the `2k - 1` comparisons a look makes (each arm against the holdout, each challenger
against the reference).

**The call** (`judge`), in this order:

1. Not enough measured customers yet (fewer than `min_per_arm` in an arm or `min_holdout` in the holdout):
   continue, or at `max_days` end with no winner.
2. A challenger meets the pick rule (above zero against the holdout, above `min_gain` against the reference):
   winner, if `stop_for_win` is on or the time is up.
3. An arm is clearly losing (upper end below zero against the holdout; for a challenger also against the
   reference), if `stop_for_loss` is on: stop.
4. Time is up: no winner, the default stays. Otherwise continue.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Decision, Estimate, Reward
from autonomy.experiments.model import HOLDOUT_BUCKET, Experiment, bucket_of
from autonomy.experiments.sequential import Interval, sequential_difference
from autonomy.proof.estimate import welch_difference

__all__ = [
    "ArmReading",
    "Judgement",
    "Outcomes",
    "Reading",
    "collect_outcomes",
    "judge",
    "read",
]

FloatArray = npt.NDArray[np.float64]
RewardValue = Callable[[Reward], float]


@dataclass(frozen=True)
class Outcomes:
    """Per-customer values by bucket: one array per arm and one for the matched holdout."""

    arms: Mapping[str, FloatArray]
    holdout: FloatArray
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArmReading:
    arm_id: str
    label: str
    n: int
    mean: float
    vs_holdout: Estimate | None
    """Gain over the holdout per customer with the plain 95% interval (single look; for display)."""
    vs_holdout_seq: Interval | None
    """The same difference with the always-valid interval the decisions use."""
    vs_reference_seq: Interval | None
    """Gain over the reference arm, always-valid; None for the reference itself."""


@dataclass(frozen=True)
class Reading:
    experiment_id: str
    as_of: datetime
    days_running: float
    holdout_n: int
    holdout_mean: float
    arms: tuple[ArmReading, ...]
    alpha_each: float
    enough: bool
    notes: tuple[str, ...] = field(default=())

    def arm(self, arm_id: str) -> ArmReading:
        return next(a for a in self.arms if a.arm_id == arm_id)


@dataclass(frozen=True)
class Judgement:
    action: Literal["continue", "winner", "stop_losing", "no_winner"]
    winner: str | None
    losing: tuple[str, ...]
    reason: str


def collect_outcomes(
    experiment: Experiment,
    decisions: Iterable[Decision] | Mapping[str, Decision],
    rewards: Sequence[Reward],
    now: datetime,
    *,
    value: RewardValue = lambda r: r.net,
) -> Outcomes:
    """Per-customer values of this experiment from the decision log and the closed rewards.

    Counts decisions made from the registration to the end of the experiment (or `now`) that carry the
    experiment's tag, and rewards whose window closed by `now`. A customer's bucket is that of their
    first tagged decision. Customers with no closed reward are left out (not yet measured).
    """
    by_id = dict(decisions) if isinstance(decisions, Mapping) else {d.decision_id: d for d in decisions}
    stop = experiment.ended_at or now
    inside: list[tuple[Decision, str]] = []
    for d in by_id.values():
        bucket = bucket_of(d.policy_version, experiment.experiment_id)
        if bucket is not None and experiment.registered_at <= d.made_at <= stop:
            inside.append((d, bucket))
    first: dict[str, tuple[tuple[datetime, str], str]] = {}
    for d, bucket in inside:
        key = (d.made_at, d.decision_id)
        if d.customer_id not in first or key < first[d.customer_id][0]:
            first[d.customer_id] = (key, bucket)
    tagged = {d.decision_id for d, bucket in inside if bucket == first[d.customer_id][1]}
    notes: list[str] = []
    if len(tagged) < len(inside):
        notes.append(
            f"{len(inside) - len(tagged):,} decisions carried another arm than their customer's first and "
            "were left out."
        )
    totals: dict[str, float] = {}
    for r in rewards:
        if r.window_end <= now and r.decision_id in tagged:
            totals[r.customer_id] = totals.get(r.customer_id, 0.0) + float(value(r))

    def values(bucket: str) -> FloatArray:
        ids = sorted(c for c in totals if first[c][1] == bucket)
        return np.array([totals[c] for c in ids], dtype=np.float64)

    arms = {a: values(a) for a in experiment.spec.arm_ids}
    return Outcomes(arms=arms, holdout=values(HOLDOUT_BUCKET), notes=tuple(notes))


def alpha_each(experiment: Experiment) -> float:
    """The experiment's alpha split over the `2k - 1` comparisons of one look."""
    return experiment.spec.pick_rule.alpha / (2 * len(experiment.spec.arms) - 1)


def read(experiment: Experiment, outcomes: Outcomes, now: datetime) -> Reading:
    """Every arm's gain over the holdout and over the reference, as of `now`."""
    spec = experiment.spec
    rule, stop = spec.pick_rule, spec.stopping_rule
    alpha = alpha_each(experiment)
    ref = outcomes.arms[spec.reference_arm]
    arms: list[ArmReading] = []
    for arm in spec.arms:
        x = outcomes.arms[arm.arm_id]
        vs_ref = (
            None
            if arm.arm_id == spec.reference_arm
            else sequential_difference(x, ref, prior_sd=stop.prior_sd, alpha=alpha)
        )
        arms.append(
            ArmReading(
                arm_id=arm.arm_id,
                label=arm.label,
                n=len(x),
                mean=float(x.mean()) if len(x) else 0.0,
                vs_holdout=welch_difference(x, outcomes.holdout),
                vs_holdout_seq=sequential_difference(
                    x, outcomes.holdout, prior_sd=stop.prior_sd, alpha=alpha
                ),
                vs_reference_seq=vs_ref,
            )
        )
    enough = len(outcomes.holdout) >= rule.min_holdout and all(a.n >= rule.min_per_arm for a in arms)
    return Reading(
        experiment_id=experiment.experiment_id,
        as_of=now,
        days_running=(now - experiment.registered_at) / timedelta(days=1),
        holdout_n=len(outcomes.holdout),
        holdout_mean=float(outcomes.holdout.mean()) if len(outcomes.holdout) else 0.0,
        arms=tuple(arms),
        alpha_each=alpha,
        enough=enough,
        notes=outcomes.notes,
    )


def qualifies(experiment: Experiment, arm: ArmReading) -> bool:
    """Whether this arm meets the pick rule at this look."""
    spec = experiment.spec
    if arm.arm_id == spec.reference_arm or arm.vs_holdout_seq is None or arm.vs_reference_seq is None:
        return False
    return arm.vs_holdout_seq.low > 0.0 and arm.vs_reference_seq.low > spec.pick_rule.min_gain


def clearly_losing(arm: ArmReading) -> bool:
    """Whether this arm is clearly worse than doing nothing, or (a challenger) than the reference."""
    if arm.vs_holdout_seq is not None and arm.vs_holdout_seq.high < 0.0:
        return True
    return arm.vs_reference_seq is not None and arm.vs_reference_seq.high < 0.0


def judge(experiment: Experiment, reading: Reading) -> Judgement:
    """Continue, crown a winner, stop for a losing arm, or end with no winner: see the module docstring."""
    spec = experiment.spec
    stop = spec.stopping_rule
    time_up = reading.days_running >= stop.max_days
    if not reading.enough:
        if time_up:
            return Judgement(
                "no_winner",
                None,
                (),
                f"Time was up after {stop.max_days} days before enough customers were measured "
                f"(at least {spec.pick_rule.min_per_arm:,} per arm and {spec.pick_rule.min_holdout:,} held out).",
            )
        return Judgement("continue", None, (), "Not enough customers are measured yet to call anything.")
    winners = [a for a in reading.arms if qualifies(experiment, a)]
    if winners and (stop.stop_for_win or time_up):
        best = max(winners, key=lambda a: (a.vs_reference_seq.low if a.vs_reference_seq else 0.0))
        return Judgement(
            "winner",
            best.arm_id,
            (),
            f"{best.label} beat {spec.label_of(spec.reference_arm)} and beat doing nothing, "
            f"with intervals that hold however often the test was read.",
        )
    losing = tuple(a.arm_id for a in reading.arms if clearly_losing(a))
    if losing and stop.stop_for_loss:
        names = ", ".join(spec.label_of(a) for a in losing)
        return Judgement("stop_losing", None, losing, f"{names} was clearly losing, so the test stopped.")
    if time_up:
        return Judgement(
            "no_winner",
            None,
            losing,
            f"No arm proved better than {spec.label_of(spec.reference_arm)} within {stop.max_days} days.",
        )
    return Judgement("continue", None, losing, "No arm has proved itself or clearly lost yet.")
