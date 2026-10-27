"""The policies of the waves benchmark, as factories the Gym's `run_policy` accepts.

* the Gym's own: `agent` and every ablation variant (`autonomy.runtime.ablation`, e.g. `agent_greedy`,
  `agent_uplift_only`) built on this benchmark's brief and library with SMS and e-mail costs and nothing else
  changed; `best_fixed` (the Gym's `BestFixedRule`, SMS before e-mail); `oracle` (the Gym's `OraclePolicy` on the
  world's truth: a ceiling, never a target);
* the simple rules: `send_none`, `send_all_sms`, `send_all_email` (every day the cap allows) and `weekly_sms` (one
  SMS a week);
* `uplift_waves` (`autonomy.gym.waves.uplift`): the uplift-threshold method refitted every week on a randomised slice.

A policy sees only the customer's state and the decision context, as in the Gym; only the oracle is handed the truth.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta

from autonomy.contracts import (
    NO_OFFER,
    Action,
    Brief,
    Channel,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Format,
    Group,
    Reward,
    stable_id,
)
from autonomy.fakes.policies import NothingPolicy
from autonomy.gym.baselines import (
    BestFixedRule,
    next_local_time,
    send_hour,
    usable_content,
    weekly_cap,
)
from autonomy.gym.score import PolicyFactory, SimWorld, oracle_factory
from autonomy.gym.waves.config import CHANNEL_COST
from autonomy.interfaces import Policy

SEND_NONE = "send_none"
SEND_ALL_SMS = "send_all_sms"
SEND_ALL_EMAIL = "send_all_email"
WEEKLY_SMS = "weekly_sms"
BEST_FIXED = "best_fixed"
ORACLE = "oracle"
UPLIFT_WAVES = "uplift_waves"
AGENT = "agent"

SIMPLE_POLICIES: tuple[str, ...] = (SEND_NONE, SEND_ALL_SMS, SEND_ALL_EMAIL, WEEKLY_SMS, BEST_FIXED)
"""What needs no learning: the candidates for 'the best simple policy'."""

PolicyBuilder = Callable[[], PolicyFactory]


class FixedChannelRule:
    """Send on one channel to everyone who can receive it, whenever the weekly cap and `min_gap_days` allow,
    at a fixed shop-local hour, with no offer. The holdout is never contacted."""

    def __init__(self, channel: Channel, *, hour: int = 14, min_gap_days: float = 0.0, name: str) -> None:
        self.channel = channel
        self.hour = hour
        self.min_gap = timedelta(days=min_gap_days)
        self.version = f"{name}-v1"

    def _nothing(self, state: CustomerState, ctx: DecisionContext, reason: str) -> Decision:
        return Decision(
            decision_id=stable_id("dec", state.customer_id, ctx.now.isoformat(), self.version),
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.NOTHING,
            group=state.group,
            probability=1.0,
            reason=reason,
            policy_version=self.version,
        )

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        now = ctx.now
        if state.group is Group.HOLDOUT:
            return self._nothing(state, ctx, "Holdout: never contacted.")
        if state.open_complaint:
            return self._nothing(state, ctx, "They have an open complaint.")
        if state.contacts_7d >= weekly_cap(ctx.briefs):
            return self._nothing(state, ctx, "The weekly contact cap is reached.")
        if state.last_contact_at is not None and now - state.last_contact_at < self.min_gap:
            return self._nothing(state, ctx, "Contacted too recently.")
        if self.channel not in state.contactable_channels():
            return self._nothing(state, ctx, f"{self.channel.value} cannot reach this customer.")
        for brief in ctx.briefs:
            if self.channel not in brief.envelope.channels:
                continue
            hour = send_hour(self.hour, brief.envelope)
            if hour is None:
                continue
            item = next(
                (
                    c
                    for c in usable_content(ctx.content, brief, self.channel)
                    if c.offer_id == NO_OFFER.offer_id
                ),
                None,
            )
            action = Action(
                goal_id=brief.goal_id,
                channel=self.channel,
                offer_id=NO_OFFER.offer_id,
                content_id=item.content_id if item is not None else None,
                format=item.format if item is not None else Format.TEXT,
                send_at=next_local_time(now, state.timezone, hour),
            )
            return Decision(
                decision_id=stable_id("dec", state.customer_id, now.isoformat(), self.version),
                customer_id=state.customer_id,
                made_at=now,
                kind=DecisionKind.ACT,
                action=action,
                group=state.group,
                probability=1.0,
                reason=f"Fixed rule: {self.channel.value} at {hour}:00 local.",
                policy_version=self.version,
            )
        return self._nothing(state, ctx, "No brief allows this channel.")

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:  # noqa: ARG002
        return None


def simple_factories() -> dict[str, PolicyFactory]:
    """The policies that need no learning and no truth."""

    def none(world: SimWorld) -> Policy:  # noqa: ARG001
        return NothingPolicy()

    def sms(world: SimWorld) -> Policy:  # noqa: ARG001
        return FixedChannelRule(Channel.SMS, name=SEND_ALL_SMS)

    def email(world: SimWorld) -> Policy:  # noqa: ARG001
        return FixedChannelRule(Channel.EMAIL, name=SEND_ALL_EMAIL)

    def weekly(world: SimWorld) -> Policy:  # noqa: ARG001
        return FixedChannelRule(Channel.SMS, min_gap_days=7.0, name=WEEKLY_SMS)

    def fixed(world: SimWorld) -> Policy:  # noqa: ARG001
        return BestFixedRule(channels=(Channel.SMS, Channel.EMAIL))

    return {SEND_NONE: none, SEND_ALL_SMS: sms, SEND_ALL_EMAIL: email, WEEKLY_SMS: weekly, BEST_FIXED: fixed}


def agent_factory(name: str, brief: Brief, content: Sequence[ContentItem]) -> PolicyFactory:
    """The Gym's agent or one of its ablation variants (`autonomy.runtime.ablation`), built exactly as the real
    bench builds it: this brief and library, and the channels' costs in roubles, nothing else changed."""

    def factory(world: SimWorld) -> Policy:  # noqa: ARG001
        from autonomy.runtime import ablation

        briefs = ablation.variant_briefs(name, [brief])
        return ablation.build_variant(name, briefs, list(content), costs=dict(CHANNEL_COST))

    return factory


def is_agent_variant(name: str) -> bool:
    from autonomy.runtime import ablation

    return name in ablation.VARIANTS


def policy_names() -> tuple[str, ...]:
    from autonomy.runtime import ablation

    return (*SIMPLE_POLICIES, ORACLE, UPLIFT_WAVES, *ablation.names())


def make_factory(name: str, brief: Brief, content: Sequence[ContentItem]) -> PolicyFactory:
    """The factory for a policy by name."""
    simple = simple_factories()
    if name in simple:
        return simple[name]
    if name == ORACLE:
        return oracle_factory
    if name == UPLIFT_WAVES:
        from autonomy.gym.waves.uplift import uplift_waves_factory

        return uplift_waves_factory(brief, content)
    if is_agent_variant(name):
        return agent_factory(name, brief, content)
    raise KeyError(f"unknown policy {name!r}; known: {', '.join(policy_names())}")


def run_briefs(name: str, brief: Brief) -> list[Brief]:
    """The briefs `run_policy` is given for this policy: the plain one, with the weekly cap raised for
    `agent_loose_caps` (as the Gym's own runner does)."""
    from autonomy.runtime import ablation

    if name in ablation.VARIANTS:
        return ablation.variant_briefs(name, [brief])
    return [brief]
