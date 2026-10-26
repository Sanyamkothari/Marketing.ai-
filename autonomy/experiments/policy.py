"""`ExperimentPolicy(inner, book)`: run the agent as usual, except that a small random group is tested.

For an assigned customer the arm RESTRICTS THE OPTIONS and nothing else: in the library the inner policy
sees (`DecisionContext.content`), the experiment's goal keeps only the content of the arm's offer or
channel. Every other goal of the same customer sees the library it always sees: content shared by all
goals (`goal_id` None) that falls outside the arm is handed to the other goals as their own copies, so
only the tested goal loses it. The inner policy then does everything it always does with what is left,
including every hard rule (consent, unsubscribes, quiet hours, caps, the holdout, the envelope), its own
pricing and its own sampling. The wrapper never sends anything and never lets anything through that the
inner policy blocked.

An offer arm forces its offer: the tested goal's "no offer" content is cut too (unless "none" is itself
the arm), because the agent's own evidence gate defaults to no offer until an offer has proved itself, and
an arm that could fall back to a plain message would measure that gate, not the offer.

**The logged probability** is the inner policy's own, unchanged: its probability of this choice among the
options left in the arm, which already counts its exploration floor. The arm is part of the decision's
context, not of its randomness: it is a fixed function of (experiment id, customer id), so for this
customer, at this moment, the only random step is the inner policy's draw. Whether a customer is in an
experiment and which arm they are on are thus treated like the holdout split, a known context written in
the decision's `policy_version`. That keeps every reader of the probability right: `autonomy.proof.ope`
(weight = target / logged), and the agent's own learner, which weights "do nothing" rows by
1 / probability. The consequence for off-policy reads: on an experiment row an option outside the
customer's arm had probability 0, so a target policy that would pick it is not supported there (the
positivity assumption of `autonomy.proof.ope`). Arms are compared by `autonomy.experiments.reading`, the
randomised comparison, not by off-policy evaluation.

**The log is self-describing.** The decision's `policy_version` gains `|exp:<id>:<arm>`; matched holdout
customers (same audience, never contacted) gain `|exp:<id>:holdout` (`holdout_experiments` says which
experiments a holdout customer is read in). Their decisions are otherwise untouched. Baseline customers are
not tagged.

**Guard.** If the inner policy ignores the restricted library and returns an action of the tested goal
outside the arm, the decision is turned into "do nothing" (code `experiment_arm` in `blocked_by`) and counted
in `stats["outside_arm"]`; it never goes out, and the send the inner policy scheduled is withdrawn
(`forget_pending`) so its caps do not count it. Such a row keeps the probability of the discarded draw, a
lower bound on the chance of "do nothing", and should be left out of off-policy reads. Policies built on the
library, as the agent is, never trigger it.

**Defaults.** A promoted winner (`ExperimentBook.promote`) is applied, when `apply_defaults` is on, to
customers not in an experiment who match the audience it was tested on. At every decision it is checked
again against the goal's brief in the context: it applies only while it is still inside that envelope and,
for a channel, while the goal may still use that channel (not paused by the inner policy's breaker) and the
customer can be reached on it. Otherwise it is skipped for that decision and counted in
`stats["default_skipped"]`. An offer default keeps the default offer and "no offer" in the goal's library;
the agent's evidence gate then still decides between them, so the default never forces an offer on a
customer for whom a plain message is expected to do better. A channel default keeps the goal's content on
that channel. With no promotion, and no running experiment, `decide` is exactly the inner policy's.

Everything else (`learn`, `nightly`, `prepare_day`, `explain_bids`...) is the inner policy's.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from autonomy.contracts import (
    NO_OFFER,
    Brief,
    Channel,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Group,
    Reward,
)
from autonomy.decide.audience import matches
from autonomy.experiments.assign import Assignment, assign, holdout_experiments
from autonomy.experiments.book import ExperimentBook
from autonomy.experiments.model import HOLDOUT_BUCKET, Experiment, Promotion, tag_decision_version
from autonomy.interfaces import Policy
from autonomy.scientist.questions import QuestionError, QuestionKind, check_arm

__all__ = ["EXPERIMENT_ARM", "ExperimentPolicy", "restrict_content"]

EXPERIMENT_ARM = "experiment_arm"
"""`blocked_by` code of a decision the wrapper turned into "do nothing" because it fell outside its arm."""


def _value(item: ContentItem, kind: QuestionKind) -> str:
    return item.offer_id if kind is QuestionKind.OFFER else item.channel.value


def restrict_content(
    content: Sequence[ContentItem],
    goal_id: str,
    kind: QuestionKind,
    allowed: Sequence[str],
    other_goals: Sequence[str] = (),
) -> tuple[ContentItem, ...]:
    """The library with `goal_id`'s view cut to the allowed offers (or channels); other goals' views kept.

    Items of other goals stay as they are. Items of `goal_id` outside `allowed` are cut. An item shared by
    all goals (`goal_id` None) outside `allowed` is cut for `goal_id` only: it is handed to each of
    `other_goals` as that goal's own copy, so they can still use it.
    """
    out: list[ContentItem] = []
    for c in content:
        if (c.goal_id is not None and c.goal_id != goal_id) or _value(c, kind) in allowed:
            out.append(c)
        elif c.goal_id is None:
            out.extend(c.model_copy(update={"goal_id": g}) for g in other_goals if g != goal_id)
    return tuple(out)


def _brief(ctx: DecisionContext, goal_id: str) -> Brief | None:
    return next((b for b in ctx.briefs if b.goal_id == goal_id), None)


def _other_goals(ctx: DecisionContext, goal_id: str) -> tuple[str, ...]:
    return tuple(b.goal_id for b in ctx.briefs if b.goal_id != goal_id)


class ExperimentPolicy:
    """`Policy` that wraps another and runs the book's running experiments on a random few."""

    def __init__(self, inner: Policy, book: ExperimentBook, *, apply_defaults: bool = True) -> None:
        self._inner = inner
        self.book = book
        self.apply_defaults = apply_defaults
        self.version = inner.version
        self.stats: Counter[str] = Counter()
        """Counts for checking a run: `assigned:<experiment>:<arm>`, `acted:<experiment>:<arm>`,
        `outside_arm`, `defaulted`, `default_skipped`."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    # --- decide -----------------------------------------------------------------------------------

    def decide(self, state: CustomerState, ctx: DecisionContext, *, trigger: str | None = None) -> Decision:
        running = self.book.running()
        defaults = self.book.active_promotions() if self.apply_defaults else []
        if not running and not defaults:
            return self._call(state, ctx, trigger)
        if state.group is not Group.AGENT:
            decision = self._call(state, ctx, trigger)
            for exp in holdout_experiments(running, state, ctx.now, ctx.briefs):
                decision = decision.model_copy(
                    update={
                        "policy_version": tag_decision_version(
                            decision.policy_version, exp.experiment_id, HOLDOUT_BUCKET
                        )
                    }
                )
            return decision
        found = assign(running, state, ctx.now, ctx.briefs)
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
        allowed = (found.arm_id,)
        restricted = ctx.model_copy(
            update={
                "content": restrict_content(
                    ctx.content, spec.goal_id, spec.kind, allowed, _other_goals(ctx, spec.goal_id)
                )
            }
        )
        decision = self._call(state, restricted, trigger)
        update: dict[str, Any] = {}
        action = decision.action
        if (
            action is not None
            and action.goal_id == spec.goal_id
            and (action.offer_id if spec.kind is QuestionKind.OFFER else action.channel.value) not in allowed
        ):
            self.stats["outside_arm"] += 1
            self._withdraw(decision)
            update.update(
                kind=DecisionKind.NOTHING,
                action=None,
                expected_net=0.0,
                blocked_by=(*decision.blocked_by, EXPERIMENT_ARM),
                reason=f"Left alone: the choice fell outside the test arm {spec.label_of(found.arm_id)}.",
            )
        elif action is not None:
            self.stats[f"acted:{key}"] += 1
        update["policy_version"] = tag_decision_version(
            decision.policy_version, exp.experiment_id, found.arm_id
        )
        update["reason"] = (
            f"{update.get('reason', decision.reason)} (Part of a test of {spec.kind.value}s: this customer "
            f"is on {spec.label_of(found.arm_id)}.)"
        )
        return decision.model_copy(update=update)

    def _withdraw(self, decision: Decision) -> None:
        """Tell the inner policy a send it scheduled is not going out, so its caps stop counting it."""
        for owner in (self._inner, getattr(self._inner, "policy", None)):
            forget = getattr(owner, "forget_pending", None)
            if callable(forget):
                forget([decision.decision_id])
                return

    # --- promoted defaults ------------------------------------------------------------------------

    def _paused(self, goal_id: str, channel: Channel) -> bool:
        """Whether the inner policy's breaker (if it has one) has paused this goal or this channel of it."""
        breaker = getattr(self._inner, "breaker", None)
        if breaker is None:
            return False
        if goal_id in getattr(breaker, "paused_goals", ()):
            return True
        return (goal_id, channel) in getattr(breaker, "paused_channels", ())

    def _default_applies(self, promo: Promotion, state: CustomerState, ctx: DecisionContext) -> bool:
        """Whether the default is still inside the goal's live envelope and usable for this customer."""
        brief = _brief(ctx, promo.goal_id)
        if brief is None:
            return False
        try:
            check_arm(promo.kind, promo.value, brief.envelope)
        except QuestionError:
            return False
        if promo.kind is QuestionKind.CHANNEL:
            channel = Channel(promo.value)
            stats = state.channels.get(channel)
            if stats is None or not stats.contactable or self._paused(promo.goal_id, channel):
                return False
        return True

    def _with_defaults(
        self, state: CustomerState, ctx: DecisionContext, defaults: Sequence[Promotion]
    ) -> DecisionContext:
        content = ctx.content
        for promo in defaults:
            if promo.audience is not None and not matches(state, promo.audience, ctx.now):
                continue
            if not self._default_applies(promo, state, ctx):
                self.stats["default_skipped"] += 1
                continue
            allowed = (promo.value, NO_OFFER.offer_id) if promo.kind is QuestionKind.OFFER else (promo.value,)
            content = restrict_content(
                content, promo.goal_id, promo.kind, allowed, _other_goals(ctx, promo.goal_id)
            )
        if content is ctx.content:
            return ctx
        self.stats["defaulted"] += 1
        return ctx.model_copy(update={"content": content})

    # --- learn ------------------------------------------------------------------------------------

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        """The inner policy learns from what the restricted decisions earned, as from any other."""
        self._inner.learn(rewards, decisions)
