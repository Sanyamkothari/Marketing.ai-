"""Who is in which experiment: a hash of (experiment id, customer id), nothing stored.

For one experiment the customer's score is `split_score(customer_id, salt = experiment id)`, a number in
[0, 1) that is the same on every run and every machine. A customer is in the experiment when the score is
below the experiment's `share`, and the arm is that band cut into equal parts, so each arm gets
`share / k` of the audience and the chance of an arm, given that a customer is in the experiment, is
exactly `1 / k`.

Rules, in the order they are applied:

* Only the AGENT group is ever assigned. Holdout and baseline customers are never touched.
* The customer must match the experiment's audience at the moment of the decision.
* With several experiments running, a customer is in at most one: the earliest started that admits
  them. Whether an earlier experiment admitted a customer depends only on that experiment's own hash,
  so inside a later experiment the arms are still a fair random split of whoever is left.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from autonomy.contracts import CustomerState, Group
from autonomy.decide.audience import matches
from autonomy.experiments.model import Experiment, ExperimentSpec
from autonomy.proof.split import split_score

__all__ = ["Assignment", "arm_for_score", "assign", "experiment_score", "matching"]

SALT_PREFIX = "autonomy-experiment:"


@dataclass(frozen=True)
class Assignment:
    """A customer's place in a running experiment."""

    experiment: Experiment
    arm_id: str

    @property
    def probability(self) -> float:
        """The chance of this arm given that the customer is in the experiment: 1 / number of arms."""
        return 1.0 / len(self.experiment.spec.arms)


def experiment_score(experiment_id: str, customer_id: str) -> float:
    """The customer's stable score in [0, 1) for this experiment."""
    return split_score(customer_id, SALT_PREFIX + experiment_id)


def arm_for_score(spec: ExperimentSpec, score: float) -> str | None:
    """The arm a score falls in, or None when the score is outside the tested share."""
    if score >= spec.share:
        return None
    k = len(spec.arms)
    return spec.arms[min(int(score / spec.share * k), k - 1)].arm_id


def matching(running: Sequence[Experiment], state: CustomerState, now: datetime) -> list[Experiment]:
    """The running experiments whose audience this customer matches, in start order."""
    return [e for e in running if matches(state, e.spec.audience, now)]


def assign(running: Sequence[Experiment], state: CustomerState, now: datetime) -> Assignment | None:
    """The experiment and arm for this customer, or None. `running` is in start order."""
    if state.group is not Group.AGENT:
        return None
    for exp in matching(running, state, now):
        arm = arm_for_score(exp.spec, experiment_score(exp.experiment_id, state.customer_id))
        if arm is not None:
            return Assignment(exp, arm)
    return None
