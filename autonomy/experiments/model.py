"""The record of one live experiment: its question, arms, audience and the rules written down first.

An experiment is a `Spec` (fixed before the first send) plus what happens to it. The spec holds the
question from the Scientist's menu, the arms, who is eligible, the share of them tested, the PICK RULE
(what has to be true before an arm is called the winner) and the STOPPING RULE (when the test stops
early, and how long it may run). `ExperimentBook.start` stamps the spec with the time it was registered
and a SHA-256 digest of the spec and the rule text, and that stamp is the first thing in the log, so
nobody can change the rules after the data is in: `Experiment.digest_matches()` says whether they
still are the ones that were registered.

Every decision made inside an experiment carries a tag in its `policy_version`
(`<version>|exp:<experiment id>:<arm id>`), and the matched holdout customers carry `holdout` in place
of an arm. The decision log alone is therefore enough to read an experiment later.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Final, Literal

from pydantic import AwareDatetime, Field, model_validator

from autonomy.contracts import Audience, Contract
from autonomy.proof.estimate import MIN_GROUP
from autonomy.scientist.questions import (
    ID_PATTERN,
    MAX_ARMS,
    MIN_ARMS,
    RESERVED_ARM_IDS,
    QuestionKind,
)

__all__ = [
    "HOLDOUT_BUCKET",
    "MAX_PER_GOAL",
    "MAX_TOTAL_SHARE",
    "Arm",
    "EndKind",
    "Experiment",
    "ExperimentEvent",
    "ExperimentSpec",
    "PickRule",
    "Promotion",
    "Status",
    "StoppingRule",
    "bucket_of",
    "rule_text",
    "tag_decision_version",
    "tags_of",
]

MAX_TOTAL_SHARE: Final[float] = 0.10
"""Most of the agent group that may be in experiments at once (decision V-3 in docs/v3/DECISIONS.md).
Each experiment's `share` is a share of its own audience, which is at most the whole agent group, so
capping the sum of the shares caps the share of the agent group."""

MAX_PER_GOAL: Final[int] = 2
"""Most experiments running at once for one goal (decision V-3)."""

HOLDOUT_BUCKET: Final[str] = "holdout"
"""The tag of the holdout customers who match an experiment's audience: the group arms are read against."""

_TAG: Final[str] = "|exp:"


class Arm(Contract):
    """One thing being tested: an offer id (offer question) or a channel name (channel question)."""

    arm_id: str = Field(pattern=ID_PATTERN)
    label: str = Field(description="What the marketer calls it, e.g. 'Free shipping'")

    @model_validator(mode="after")
    def _not_reserved(self) -> Arm:
        if self.arm_id in RESERVED_ARM_IDS:
            raise ValueError(f"{self.arm_id!r} is reserved")
        return self


class PickRule(Contract):
    """When an arm may be called the winner. Written before the first send and never changed.

    A challenger (an arm other than the reference, the current default) wins when, at a look, BOTH
    hold with at least `min_per_arm` measured customers in every arm and `min_holdout` in the holdout:

    1. its profit per customer beats the HOLDOUT's: the lower end of its sequential interval for the
       difference is above zero (it makes money against leaving customers alone), and
    2. it beats the REFERENCE arm by more than `min_gain` per customer: the lower end of its
       sequential interval for the difference is above `min_gain`.

    The intervals are always-valid (`autonomy.experiments.sequential`), at level `alpha` shared across
    every comparison of the look, so looking at the data as often as you like does not change the
    chance of crowning an arm that is not better. If two challengers qualify, the one with the higher
    lower end against the reference wins. If none qualifies the default stays.
    """

    metric: Literal["net_per_customer"] = "net_per_customer"
    alpha: float = Field(default=0.05, gt=0, lt=0.5)
    min_per_arm: int = Field(default=200, ge=MIN_GROUP)
    min_holdout: int = Field(default=200, ge=MIN_GROUP)
    min_gain: float = Field(default=0.0, ge=0, description="Smallest gain over the reference worth a change")


class StoppingRule(Contract):
    """When the experiment ends. Written before the first send and never changed.

    The experiment ends when (a) an arm qualifies as the winner and `stop_for_win` is on, (b) an arm
    is CLEARLY LOSING and `stop_for_loss` is on, or (c) it has run `max_days`. An arm is clearly
    losing when the upper end of its sequential interval is below zero against the holdout (it loses
    money compared with doing nothing) or, for a challenger, against the reference (it is worse than
    what we do today). Both calls use the same always-valid intervals as the pick rule, at the same
    `alpha`, so the error rate holds however often the test is looked at.

    `prior_sd` is the scale of the mixture the intervals are built on: the size, in money per
    customer, of a difference that would matter. A value near the true effect gives the tightest
    interval; a poor guess only widens it, it never makes the error rate wrong.
    """

    method: Literal["mixture_sprt"] = "mixture_sprt"
    prior_sd: float = Field(gt=0)
    max_days: int = Field(default=28, ge=7, le=90)
    stop_for_win: bool = True
    stop_for_loss: bool = True


class ExperimentSpec(Contract):
    """Everything about an experiment that is decided before its first send."""

    experiment_id: str = Field(pattern=ID_PATTERN)
    goal_id: str
    kind: QuestionKind
    question: str = Field(description="From the Scientist's menu, in plain words")
    arms: tuple[Arm, ...]
    reference_arm: str = Field(description="The arm that is today's default; the others are challengers")
    audience: Audience = Field(description="Who may be assigned; the matched holdout is read the same way")
    share: float = Field(gt=0, le=MAX_TOTAL_SHARE, description="Share of the audience (agent group) tested")
    pick_rule: PickRule = PickRule()
    stopping_rule: StoppingRule
    created_by: str = "scientist"

    @model_validator(mode="after")
    def _arms(self) -> ExperimentSpec:
        ids = [a.arm_id for a in self.arms]
        if not MIN_ARMS <= len(ids) <= MAX_ARMS or len(set(ids)) != len(ids):
            raise ValueError(f"an experiment needs {MIN_ARMS} to {MAX_ARMS} different arms")
        if self.reference_arm not in ids:
            raise ValueError("the reference arm must be one of the arms")
        return self

    @property
    def arm_ids(self) -> tuple[str, ...]:
        return tuple(a.arm_id for a in self.arms)

    def label_of(self, arm_id: str) -> str:
        return next((a.label for a in self.arms if a.arm_id == arm_id), arm_id)


def rule_text(spec: ExperimentSpec) -> str:
    """The pick and stopping rules of this spec in plain words, as registered."""
    p, s = spec.pick_rule, spec.stopping_rule
    ref = spec.label_of(spec.reference_arm)
    comparisons = 2 * len(spec.arms) - 1
    return (
        f"Pick rule: a challenger wins when, with at least {p.min_per_arm:,} measured customers in every arm "
        f"and {p.min_holdout:,} in the holdout, it earns more {p.metric.replace('_', ' ')} than the holdout "
        f"and more than the reference ({ref}) by over {p.min_gain:g}, each shown by an always-valid interval "
        f"at overall error {p.alpha:g} (split over {comparisons} comparisons). If none does, {ref} stays the "
        f"default. Stopping rule: the test ends when a challenger wins"
        f"{'' if s.stop_for_win else ' (switched off: only at the end)'}, when an arm is clearly losing "
        f"against the holdout or the reference{'' if s.stop_for_loss else ' (switched off)'}, or after "
        f"{s.max_days} days. Intervals use a mixture of scale {s.prior_sd:g} per customer ({s.method}); "
        f"looking at the data more often does not change the error rate."
    )


def digest_of(spec: ExperimentSpec) -> str:
    """SHA-256 of the spec and its rule text: the fingerprint of what was registered."""
    return hashlib.sha256((spec.model_dump_json() + "\n" + rule_text(spec)).encode()).hexdigest()


class Status(StrEnum):
    RUNNING = "running"
    ENDED = "ended"


class EndKind(StrEnum):
    WINNER = "winner"  # a challenger proved itself
    NO_WINNER = "no_winner"  # ran to the end; the default stays
    STOPPED_LOSING = "stopped_losing"  # an arm was clearly losing
    STOPPED_MANUALLY = "stopped_manually"  # a person pressed stop


class Experiment(Contract):
    """A registered experiment and what has happened to it. Only `ExperimentBook` creates and changes it."""

    spec: ExperimentSpec
    status: Status = Status.RUNNING
    registered_at: AwareDatetime
    digest: str
    rules: str = Field(description="`rule_text(spec)` as registered")
    first_assignment_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    end_kind: EndKind | None = None
    end_reason: str | None = None
    winner: str | None = None
    losing: tuple[str, ...] = ()
    promoted: bool = False

    @property
    def experiment_id(self) -> str:
        return self.spec.experiment_id

    @property
    def goal_id(self) -> str:
        return self.spec.goal_id

    @property
    def running(self) -> bool:
        return self.status is Status.RUNNING

    def digest_matches(self) -> bool:
        """True while the spec and its rule text are exactly what was registered."""
        return self.digest == digest_of(self.spec) and self.rules == rule_text(self.spec)


class ExperimentEvent(Contract):
    """One line of the append-only log: what happened to which experiment, when, and why."""

    seq: int
    at: AwareDatetime
    experiment_id: str
    kind: Literal["registered", "started", "ended", "promoted", "reverted", "refused"]
    detail: str


class Promotion(Contract):
    """A winner made the default of a goal, inside the approved envelope. Reversible."""

    promotion_id: str
    experiment_id: str
    goal_id: str
    kind: QuestionKind
    value: str = Field(description="The arm id: an offer id or a channel name")
    previous: str | None = Field(description="The default before this promotion, if there was one")
    promoted_at: AwareDatetime
    reason: str
    reverted_at: AwareDatetime | None = None
    reverted_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.reverted_at is None


# --- tags in the decision log ---------------------------------------------------------------------------


def tag_decision_version(policy_version: str, experiment_id: str, bucket: str) -> str:
    """The policy version with this experiment's tag added (`bucket` is an arm id or `holdout`)."""
    return f"{policy_version}{_TAG}{experiment_id}:{bucket}"


def tags_of(policy_version: str) -> dict[str, str]:
    """experiment id -> arm id (or `holdout`) for every experiment tag in a policy version."""
    out: dict[str, str] = {}
    for part in policy_version.split("|")[1:]:
        match = re.fullmatch(r"exp:([A-Za-z0-9_-]+):([A-Za-z0-9_-]+)", part)
        if match:
            out[match.group(1)] = match.group(2)
    return out


def bucket_of(policy_version: str, experiment_id: str) -> str | None:
    """The arm id (or `holdout`) this decision was made under in the experiment, else None."""
    return tags_of(policy_version).get(experiment_id)
