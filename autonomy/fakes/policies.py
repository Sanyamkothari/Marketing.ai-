"""Two trivial policies: never act, and contact everyone. Useful as floors in tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from autonomy.contracts import (
    Action,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Reward,
    stable_id,
)


class NothingPolicy:
    version = "nothing-v0"

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        return Decision(
            decision_id=stable_id("dec", state.customer_id, ctx.now.isoformat(), self.version),
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.NOTHING,
            group=state.group,
            probability=1.0,
            reason="This policy never contacts anyone.",
            policy_version=self.version,
        )

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:  # noqa: ARG002
        return None


class ContactEveryonePolicy:
    """Contact through the first allowed, contactable channel of the first brief, every time asked."""

    version = "everyone-v0"

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        did = stable_id("dec", state.customer_id, ctx.now.isoformat(), self.version)
        for brief in ctx.briefs:
            for ch in brief.envelope.channels:
                if ch in state.contactable_channels():
                    content = next((c.content_id for c in ctx.content if c.channel == ch), None)
                    action = Action(goal_id=brief.goal_id, channel=ch, content_id=content, send_at=ctx.now)
                    return Decision(
                        decision_id=did,
                        customer_id=state.customer_id,
                        made_at=ctx.now,
                        kind=DecisionKind.ACT,
                        action=action,
                        group=state.group,
                        probability=1.0,
                        reason=f"This policy contacts everyone; {ch.value} was the first allowed channel.",
                        policy_version=self.version,
                    )
        return (
            NothingPolicy()
            .decide(state, ctx)
            .model_copy(
                update={
                    "decision_id": did,
                    "reason": "No allowed channel can reach this customer.",
                    "policy_version": self.version,
                }
            )
        )

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:  # noqa: ARG002
        return None
