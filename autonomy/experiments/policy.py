"""`ExperimentPolicy(inner, book)`: run the agent as usual, except that a small random group is tested.

For an assigned customer the arm RESTRICTS THE OPTIONS and nothing else: the library the inner policy
sees (`DecisionContext.content`) keeps only the content that belongs to the arm's offer or channel. The
inner policy then does everything it always does with what is left, including every hard rule (consent,
unsubscribes, quiet hours, caps, the holdout, the envelope), its own pricing and its own sampling. The
wrapper never sends anything and never lets anything through that the inner policy blocked.

**The logged probability.** For an assigned customer the decision is logged with

    probability = (1 / number of arms)  x  (the inner policy's probability of this choice among the
                                           options left in the arm)

The first factor is the randomisation by the hash (`autonomy.experiments.assign`): given that a customer
is in the experiment, each arm is equally likely. The second is the inner policy's own, which already
counts its exploration floor over the restricted options. Their product is the probability that this
randomisation and this policy together chose exactly this decision, so `autonomy.proof.ope` weights it
correctly. Whether a customer is inside the experiment at all (the hash band, fixed by the experiment's
`share` and audience) is treated like the holdout split: a known context, not part of the policy's
randomness; the share is in the experiment's record when it matters.

**The log is self-describing.** The decision's `policy_version` gains `|exp:<id>:<arm>`; matched holdout
customers (same audience, never contacted) gain `|exp:<id>:holdout`. Their decisions are otherwise
untouched. Baseline customers are not tagged.

**Guard.** If the inner policy ignores the restricted library and returns an action outside the arm, the
decision is turned into "do nothing" (code `experiment_arm` in `blocked_by`) and counted in
`stats["outside_arm"]`; it never goes out. Policies built on the library, as the agent is, never trigger it.

**Defaults.** A promoted winner (`ExperimentBook.promote`) is applied for customers not in an experiment, when
`apply_defaults` is on: an offer default keeps the default offer and "no offer" in the library of that goal's
customers, a channel default keeps content on the default channel for customers who can be reached on it.
With no promotion, and no running experiment, `decide` is exactly the inner policy's.

Everything else (`learn`, `nightly`, `prepare_day`, `explain_bids`...) is the inner policy's.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from autonomy.contracts import (
    Channel,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Group,
    Reward,
)
from autonomy.experiments.assign import Assignment, assign, matching
from autonomy.experiments.book import ExperimentBook
from autonomy.experiments.model import HOLDOUT_BUCKET, Experiment, tag_decision_version
from autonomy.interfaces import Policy
from autonomy.scientist.questions import QuestionKind

__all__ = ["EXPERIMENT_ARM", "ExperimentPolicy", "restrict_content"]

EXPERIMENT_ARM = "experiment_arm"
"""`blocked_by` code of a decision the wrapper turned into "do nothing" because it fell outside its arm."""


def _keeps(item: ContentItem, kind: QuestionKind, arm_id: str) -> bool:
    return item.offer_id == arm_id if kind is QuestionKind.OFFER else item.channel.value == arm_id


def restrict_content(
    content: Sequence[ContentItem], goal_id: str, kind: QuestionKind, allowed: Sequence[str]
) -> tuple[ContentItem, ...]:
    """The library with this goal's items cut to those of the allowed arms (other goals' items stay)."""
    return tuple(
        c
        for c in content
        if (c.goal_id is not None and c.goal_id != goal_id) or any(_keeps(c, kind, arm) for arm in allowed)
    )


class ExperimentPolicy:
    """`Policy` that wraps another and runs the book's running experiments on a random few."""

    def __init__(self, inner: Policy, book: ExperimentBook, *, apply_defaults: bool = True) -> None:
        self._inner = inner
        self.book = book
        self.apply_defaults = apply_defaults
        self.version = inner.version
        self.stats: Counter[str] = Counter()
        """Counts for checking a run: `assigned:<experiment>:<arm>`, `acted:<experiment>:<arm>`,
        `outside_arm`, `defaulted`."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    # --- decide -----------------------------------------------------------------------------------

    def decide(self, state: CustomerState, ctx: DecisionContext, *, trigger: str | None = None) -> Decision:
        running = self.book.running()
        defaults = self.book.defaults() if self.apply_defaults else []
        if not running and not defaults:
            return self._call(state, ctx, trigger)
        if state.group is not Group.AGENT:
            decision = self._call(state, ctx, trigger)
            if state.group is Group.HOLDOUT:
                for exp in matching(running, state, ctx.now):
                    decision = decision.model_copy(
                        update={
                            "policy_version": tag_decision_version(
                                decision.policy_version, exp.experiment_id, HOLDOUT_BUCKET
                            )
                        }
                    )
            return decision
        found = assign(running, state, ctx.now)
        if found is None:
            return self._call(state, self._with_defaults(state, ctx, defaults), trigger)
        return self._in_experiment(found, state, ctx, trigger)

    def _call(self, state: CustomerState, ctx: DecisionContext, trigger: str | None) -> Decision:
        if trigger is None:
            return self._inner.decide(state, ctx)
        decide: Any = self._inner.decide
        result: Decision = decide(state, ctx, trigger=trigger)
        return result

    def _in_experiment(
        self, found: Assignment, state: CustomerState, ctx: DecisionContext, trigger: str | None
    ) -> Decision:
        exp: Experiment = found.experiment
        spec = exp.spec
        self.book.note_assignment(exp.experiment_id, ctx.now)
        key = f"{exp.experiment_id}:{found.arm_id}"
        self.stats[f"assigned:{key}"] += 1
        restricted = ctx.model_copy(
            update={"content": restrict_content(ctx.content, spec.goal_id, spec.kind, (found.arm_id,))}
        )
        decision = self._call(state, restricted, trigger)
        update: dict[str, Any] = {}
        action = decision.action
        if (
            action is not None
            and action.goal_id == spec.goal_id
            and (action.offer_id if spec.kind is QuestionKind.OFFER else action.channel.value) != found.arm_id
        ):
            self.stats["outside_arm"] += 1
            update.update(
                kind=DecisionKind.NOTHING,
                action=None,
                expected_net=0.0,
                blocked_by=(*decision.blocked_by, EXPERIMENT_ARM),
                reason=f"Left alone: the choice fell outside the test arm {spec.label_of(found.arm_id)}.",
            )
        elif action is not None:
            self.stats[f"acted:{key}"] += 1
        update["probability"] = min(1.0, decision.probability * found.probability)
        update["policy_version"] = tag_decision_version(
            decision.policy_version, exp.experiment_id, found.arm_id
        )
        update["reason"] = (
            f"{update.get('reason', decision.reason)} (Part of a test of {spec.kind.value}s: this customer "
            f"is on {spec.label_of(found.arm_id)}.)"
        )
        return decision.model_copy(update=update)

    def _with_defaults(
        self, state: CustomerState, ctx: DecisionContext, defaults: Sequence[tuple[str, QuestionKind, str]]
    ) -> DecisionContext:
        content = ctx.content
        for goal_id, kind, value in defaults:
            if kind is QuestionKind.OFFER:
                content = restrict_content(content, goal_id, kind, (value, "none"))
            else:
                stats = state.channels.get(Channel(value))
                if stats is not None and stats.contactable:
                    content = restrict_content(content, goal_id, kind, (value,))
        if content is ctx.content:
            return ctx
        self.stats["defaulted"] += 1
        return ctx.model_copy(update={"content": content})

    # --- learn ------------------------------------------------------------------------------------

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        """The inner policy learns from what the restricted decisions earned, as from any other."""
        self._inner.learn(rewards, decisions)
