"""Small builders shared by the experiment tests: a brief, a content library, a bidder and a spec."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from autonomy.contracts import (
    NO_OFFER,
    Action,
    Audience,
    Bid,
    Brief,
    Channel,
    ChannelStats,
    ContentItem,
    ContentStatus,
    CustomerState,
    DecisionContext,
    Envelope,
    Group,
    Objective,
    OfferKind,
    OfferOption,
    Theme,
    Tone,
)
from autonomy.experiments import Arm, ExperimentSpec, PickRule, StoppingRule, experiment_score
from autonomy.experiments.assign import arm_for_score
from autonomy.scientist.questions import QuestionKind

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
GOAL = "goal_exp"
FREE_SHIP = OfferOption(
    offer_id="freeship", label="Free shipping", kind=OfferKind.FREE_SHIPPING, cost_per_redemption=4.0
)
TEN_OFF = OfferOption(
    offer_id="pct10", label="10% off", kind=OfferKind.PERCENT_OFF, amount=10, cost_per_redemption=6.0
)
NET = {"freeship": 3.0, "pct10": 2.0, "none": 1.0}
"""What the test bidder thinks each offer is worth, so the inner policy has a clear favourite."""
REACHABLE = ChannelStats(address_known=True, consent=True)


def brief(goal_id: str = GOAL, *, offers: tuple[OfferOption, ...] = (NO_OFFER, FREE_SHIP, TEN_OFF)) -> Brief:
    return Brief(
        brief_id=f"brief_{goal_id}_1",
        goal_id=goal_id,
        objective=Objective.GROSS_PROFIT,
        audience=Audience(description="lapsed customers"),
        envelope=Envelope(channels=(Channel.EMAIL, Channel.WHATSAPP), offers=offers, budget=5000),
        created_at=NOW,
        approved_by="founder",
        approved_at=NOW,
    )


def library(goal_id: str = GOAL) -> tuple[ContentItem, ...]:
    """Every channel x offer pair, once."""
    items: list[ContentItem] = []
    for channel in (Channel.EMAIL, Channel.WHATSAPP):
        for offer in ("none", "freeship", "pct10"):
            items.append(
                ContentItem(
                    content_id=f"{channel.value}_{offer}",
                    goal_id=goal_id,
                    channel=channel,
                    theme=Theme.REMINDER,
                    tone=Tone.FRIENDLY,
                    offer_id=offer,
                    subject="Hello" if channel is Channel.EMAIL else None,
                    body="Hi {{first_name}}",
                    slots=("first_name",),
                    template_name=f"t_{offer}" if channel is Channel.WHATSAPP else None,
                    status=ContentStatus.APPROVED,
                    created_at=NOW,
                )
            )
    return tuple(items)


class LibraryBidder:
    """Bids once for every library item of its goal (or shared by all goals) on a channel the customer can be reached on, so
    what it offers follows `ctx.content`, as the real bidders do."""

    def __init__(self, goal_id: str = GOAL) -> None:
        self.goal_id = goal_id

    def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
        reachable = set(state.contactable_channels())
        out: list[Bid] = []
        for item in ctx.content:
            if item.goal_id not in (None, self.goal_id) or item.channel not in reachable:
                continue
            net = NET[item.offer_id]
            out.append(
                Bid(
                    goal_id=self.goal_id,
                    action=Action(
                        goal_id=self.goal_id,
                        channel=item.channel,
                        offer_id=item.offer_id,
                        content_id=item.content_id,
                        send_at=ctx.now,
                    ),
                    uplift=0.05,
                    value_if_converted=100.0,
                    expected_net=net,
                    uncertainty=0.0,
                )
            )
        return out


def state(customer_id: str, group: Group = Group.AGENT, **kw: object) -> CustomerState:
    base: dict[str, object] = {
        "customer_id": customer_id,
        "as_of": NOW,
        "group": group,
        "channels": {Channel.EMAIL: REACHABLE, Channel.WHATSAPP: REACHABLE},
    }
    base.update(kw)
    return CustomerState.model_validate(base)


def ctx(now: datetime = NOW, seed: int = 0, goal_id: str = GOAL) -> DecisionContext:
    return DecisionContext(now=now, briefs=(brief(goal_id),), content=library(goal_id), seed=seed)


def spec(
    experiment_id: str = "exp1",
    *,
    goal_id: str = GOAL,
    arms: tuple[str, ...] = ("pct10", "freeship"),
    share: float = 0.05,
    min_per_arm: int = 100,
    prior_sd: float = 1.0,
    max_days: int = 28,
    kind: QuestionKind = QuestionKind.OFFER,
    stop_for_win: bool = True,
) -> ExperimentSpec:
    labels = {"pct10": "10% off", "freeship": "Free shipping", "none": "No offer"}
    return ExperimentSpec(
        experiment_id=experiment_id,
        goal_id=goal_id,
        kind=kind,
        question="Which offer earns more profit per customer for lapsed customers?",
        arms=tuple(Arm(arm_id=a, label=labels.get(a, a)) for a in arms),
        reference_arm=arms[0],
        audience=Audience(description="lapsed customers"),
        share=share,
        pick_rule=PickRule(min_per_arm=min_per_arm, min_holdout=min_per_arm),
        stopping_rule=StoppingRule(prior_sd=prior_sd, max_days=max_days, stop_for_win=stop_for_win),
    )


def customers_in(experiment: ExperimentSpec, arm_id: str | None, n: int, prefix: str = "c") -> Iterator[str]:
    """The first `n` customer ids (c0, c1, ...) that the hash puts in this arm, or (arm None) outside the test."""
    found = 0
    i = 0
    while found < n:
        cid = f"{prefix}{i}"
        i += 1
        arm = arm_for_score(experiment, experiment_score(experiment.experiment_id, cid))
        if arm == arm_id:
            found += 1
            yield cid
