"""Decisions: what `decide()` chose for one customer at one moment, and why.

Every decision, including "do nothing", is logged with the probability that the policy had of
choosing it. That probability is what lets us evaluate a new policy on old data (off-policy
evaluation) and prove value later. Never log a decision without it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from autonomy.contracts.base import Channel, Contract, Group
from autonomy.contracts.content import ContentItem, Format
from autonomy.contracts.goals import Brief


class Action(Contract):
    goal_id: str
    channel: Channel
    offer_id: str = "none"
    content_id: str | None = None
    format: Format = Format.TEXT
    send_at: AwareDatetime
    follow_up_step: int = Field(default=0, description="0 = first contact, 1 = first follow-up, ...")
    item_id: str | None = Field(
        default=None, description="The product this action is about (refill, recommendation)"
    )
    asset_id: str | None = Field(
        default=None, description="The approved image/video used, when the format needs one"
    )
    journey_id: str | None = Field(
        default=None,
        description="The channel journey this message belongs to (v3), None for a single message",
    )


class Bid(Contract):
    """One goal's offer for this customer's attention. Money is in the goal's currency."""

    goal_id: str
    action: Action
    uplift: float = Field(description="Change in the chance of the outcome caused by acting, -1..1")
    value_if_converted: float = Field(description="Expected margin (or revenue) if the outcome happens")
    offer_cost: float = 0.0
    channel_cost: float = 0.0
    urgency: float = Field(default=1.0, ge=0, description="Multiplier; decays as the moment passes")
    expected_net: float = Field(description="uplift x value - offer cost - channel cost, times urgency")
    uncertainty: float = Field(default=0.0, ge=0, description="Std. dev. of expected_net, for sampling")


class DecisionKind(StrEnum):
    ACT = "act"
    NOTHING = "nothing"


class Decision(Contract):
    decision_id: str
    customer_id: str
    made_at: AwareDatetime
    kind: DecisionKind
    action: Action | None = None
    group: Group = Group.AGENT
    probability: float = Field(gt=0, le=1, description="Chance the policy had of choosing exactly this")
    expected_net: float = 0.0
    attention_price: float = Field(default=0.0, ge=0, description="What a bid had to beat")
    reason: str = Field(description="One plain sentence a marketer understands")
    bids: tuple[Bid, ...] = Field(default=(), description="The top bids considered, best first")
    blocked_by: tuple[str, ...] = Field(default=(), description="Hard rules that removed options")
    policy_version: str
    journey_id: str | None = Field(
        default=None, description="The journey this decision starts, continues or holds (v3), else None"
    )
    journey_probability: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description="Chance the policy had of choosing this journey, summed over its offers and"
        " content (v3). A later step of a journey carries the value its first step logged.",
    )
    journey_ended: str | None = Field(
        default=None,
        description="The journey that ended while this decision was made, and why, as"
        " '<journey id>:<code>' (v3), so the runs rebuilt from the log stay ended",
    )

    @model_validator(mode="after")
    def _action_matches_kind(self) -> Decision:
        if (self.kind is DecisionKind.ACT) != (self.action is not None):
            raise ValueError("an ACT decision has an action and a NOTHING decision has none")
        return self


class DecisionContext(Contract):
    """Everything `decide()` may read besides the customer's state."""

    now: AwareDatetime
    briefs: tuple[Brief, ...] = Field(description="Approved briefs of active goals")
    content: tuple[ContentItem, ...] = Field(default=(), description="Approved content items")
    budget_left: dict[str, float] = Field(default_factory=dict, description="goal_id -> money left")
    seed: int = Field(default=0, description="Sampling seed; the Gym fixes it so runs repeat")
    policy_version: str = "v0"


class BatchStatus(StrEnum):
    PENDING = "pending"  # waiting for a person (autonomy level APPROVE)
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"


class Batch(Contract):
    """One goal's ACT decisions from one sweep, held for a person when the goal is at APPROVE level."""

    batch_id: str
    goal_id: str
    created_at: AwareDatetime
    decision_ids: tuple[str, ...]
    n_contacts: int = Field(ge=0)
    expected_net: float = Field(default=0.0, description="Sum of the decisions' expected_net")
    expected_spend: float = Field(default=0.0, ge=0)
    summary: str = Field(default="", description="One plain sentence: who, which channels, which offers")
    status: BatchStatus = BatchStatus.PENDING
    decided_by: str | None = None
    decided_at: AwareDatetime | None = None
