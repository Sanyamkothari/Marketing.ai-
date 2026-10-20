"""Goal bidders: what acting on one goal is worth for one customer, option by option.

Each active goal is a `GoalBidder`. For a customer in the goal's audience it lists every action the
envelope allows and the customer can receive (an allowed channel they are reachable on x an allowed
offer x approved content for that channel and offer) and prices each one:

    expected_net = urgency x (uplift x value - offer cost x p_redeem - channel cost)

* `uplift` and its spread come from the `ResponseModel`, keyed by the content's labels
  (`channel|offer_id|theme`), so learning transfers across goals.
* `value` is what one conversion is worth for this customer (`ValueModel`).
* `p_redeem` is the chance they buy when treated: an offer is paid for by everyone who redeems it,
  including people who would have bought anyway, which is why "sure things" get no discount.
* `urgency` fades for time-sensitive goals (cart recovery) as the triggering moment passes.

The bidder returns every option, including losing and negative ones: the attention auction, the
attention price, "do nothing", the hard rules and exploration all happen downstream. It does not
choose a send time; `Action.send_at` is `ctx.now` and the timing step replaces it. Pure maths, no
LLM, no clock.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from autonomy.contracts import (
    Action,
    Bid,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    GoalKind,
    Group,
    Reward,
)
from autonomy.decide.audience import matches
from autonomy.decide.models import (
    ANY,
    BetaResponseModel,
    ResponseModel,
    ResponseObservation,
    ValueModel,
    action_key,
)

DEFAULT_CHANNEL_COSTS: Mapping[Channel, float] = MappingProxyType(
    {
        Channel.EMAIL: 0.001,
        Channel.WHATSAPP: 0.03,
        Channel.SMS: 0.02,
        Channel.PUSH: 0.0,
        Channel.WEB: 0.0,
        Channel.WEBHOOK: 0.0,
    }
)
"""Rough cost of one message per channel, in the goal's currency. Override per business."""

DEFAULT_CART_HALF_LIFE_DAYS = 1.0
"""A cart is worth half as much a day later and an eighth after three days."""

_SECONDS_PER_DAY = 86_400.0


def days_since_cart(state: CustomerState, now: datetime) -> float | None:
    """Days since the customer's last cart, or None if unknown.

    Uses the `days_since_cart` score when a builder wrote one (measured at `state.as_of`, so the time
    since then is added); otherwise `last_seen_at`, which the state folder stamps on cart events. For
    a cart-recovery audience (people with an open cart) the latest activity is the cart or later.
    """
    score = state.scores.get("days_since_cart")
    if score is not None and math.isfinite(score):
        return max(score, 0.0) + max((now - state.as_of).total_seconds(), 0.0) / _SECONDS_PER_DAY
    if state.last_seen_at is None:
        return None
    return max((now - state.last_seen_at).total_seconds(), 0.0) / _SECONDS_PER_DAY


@dataclass(frozen=True)
class Urgency:
    """How a time-sensitive goal's value fades: halves every `half_life_days` after the trigger.

    `trigger` gives days since the triggering moment; when it is unknown the urgency is 1 (the
    audience, not the urgency, decides who is eligible). `floor` keeps a little value for late moments.
    """

    half_life_days: float
    trigger: Callable[[CustomerState, datetime], float | None] = days_since_cart
    floor: float = 0.0

    def __post_init__(self) -> None:
        if self.half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        if not 0 <= self.floor <= 1:
            raise ValueError("floor must be between 0 and 1")

    def at(self, state: CustomerState, now: datetime) -> float:
        days = self.trigger(state, now)
        if days is None:
            return 1.0
        # float(): typeshed types float ** float as Any (a negative base can give a complex).
        return max(self.floor, float(0.5 ** (max(days, 0.0) / self.half_life_days)))


def urgency_for(kind: GoalKind) -> Urgency | None:
    """The default fade for a kind of goal; None for goals whose value does not fade."""
    if kind is GoalKind.CART_RECOVERY:
        return Urgency(half_life_days=DEFAULT_CART_HALF_LIFE_DAYS)
    return None


def _option_std(model: ResponseModel, state: CustomerState, key: str, std: float) -> float:
    """The spread of this option's uplift that is its own, for sampling between options.

    Uplift is treated rate minus control rate, and the control rate is the same for every option a
    customer has: drawing its spread independently per option (as Thompson sampling does) would
    shuffle the options' order without anything to learn about them. So a model that exposes its
    treated posterior (`BetaResponseModel`) is sampled on that part only; the control part still
    moves every option's mean together as the holdout's data comes in. A model with its own
    `option_std(state, key)` answers for itself.
    """
    own = getattr(model, "option_std", None)
    if callable(own):
        value = own(state, key)
        return float(value) if isinstance(value, int | float) and value >= 0 else std
    posterior = getattr(model, "posterior_treated", None)
    if not callable(posterior):
        return std
    belief = posterior(state.segment or ANY, key)
    var = getattr(belief, "var", None)
    return math.sqrt(var) if isinstance(var, float) and var >= 0 else std


def _rank(bid: Bid) -> tuple[float, float, str]:
    """Order for picking one theme: higher expected net, then less uncertain, then content id."""
    return (bid.expected_net, -bid.uncertainty, bid.action.content_id or "")


class GoalBidder:
    """One goal's bidder (implements `autonomy.interfaces.Bidder`).

    `content` is the approved library to choose from; None means read `ctx.content` at bid time.
    Items for another goal, and items that are not approved, are ignored. `kind` picks the default
    `urgency` (cart recovery fades; pass `urgency` to override). With `per_theme=True` (the default)
    each channel x offer gets one bid per theme, so the bandit downstream can learn over themes; with
    False only the theme with the highest expected net is bid.

    `learn(rewards, decisions)` is the nightly learner's entry point (`AutonomousPolicy.learn` calls
    it): treated outcomes of this goal's sends and untreated outcomes of the `control_groups`
    feed the response model. `segment_of(customer_id)` gives a customer's segment; without it the
    bidder uses the segment it saw when it last bid for that customer, else none (pooled).

    `own_spread=True` sets each bid's `uncertainty` from the option's own (treated) spread only, not
    the control rate's spread that all of a customer's options share (see `_option_std`); the default
    uses the full spread of the uplift.
    """

    def __init__(
        self,
        brief: Brief,
        content: Sequence[ContentItem] | None = None,
        response_model: ResponseModel | None = None,
        value_model: ValueModel | None = None,
        costs: Mapping[Channel, float] | None = None,
        *,
        kind: GoalKind = GoalKind.CUSTOM,
        urgency: Urgency | None = None,
        per_theme: bool = True,
        segment_of: Callable[[str], str | None] | None = None,
        control_groups: frozenset[Group] = frozenset({Group.HOLDOUT}),
        own_spread: bool = False,
    ) -> None:
        self.goal_id = brief.goal_id
        self.own_spread = own_spread
        self.brief = brief
        self.response_model: ResponseModel = response_model or BetaResponseModel()
        self.value_model = value_model or ValueModel()
        # A partial override keeps the defaults for the channels it does not name, so a channel is
        # never priced as free just because the business only told us about email.
        self.costs: Mapping[Channel, float] = {**DEFAULT_CHANNEL_COSTS, **(costs or {})}
        self.urgency = urgency if urgency is not None else urgency_for(kind)
        self.per_theme = per_theme
        self._content = tuple(content) if content is not None else None
        self.segment_of = segment_of
        self.control_groups = control_groups
        # What learn() needs but rewards do not carry: the theme behind each content id, and the
        # segment of each customer we bid for. Both are filled as a side effect of bids().
        self._themes: dict[str, str] = {c.content_id: c.theme.value for c in self._content or ()}
        self._segments: dict[str, str | None] = {}

    def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
        """Every allowed option for this customer, best expected net first; [] if not in the audience."""
        if not matches(state, self.brief.audience, ctx.now):
            return []
        envelope = self.brief.envelope
        reachable = set(state.contactable_channels())
        channels = [ch for ch in envelope.channels if ch in reachable]
        if not channels:
            return []
        library = self._usable(self._content if self._content is not None else ctx.content)
        self._segments[state.customer_id] = state.segment
        self._themes.update((c.content_id, c.theme.value) for c in library)
        urgency = self.urgency.at(state, ctx.now) if self.urgency is not None else 1.0
        value = self.value_model.value_if_converted(state, self.brief.objective)

        out: list[Bid] = []
        for channel in channels:
            channel_cost = self.costs.get(channel, 0.0)
            for offer in envelope.offers:
                options = self._by_theme(library, channel, offer.offer_id)
                group: list[Bid] = []
                for item in options:
                    key = action_key(channel, offer.offer_id, item.theme)
                    mean, std = self.response_model.predict(state, key)
                    if self.own_spread:
                        std = _option_std(self.response_model, state, key, std)
                    p_redeem = (
                        self.response_model.treated_rate(state, key) if offer.cost_per_redemption else 0.0
                    )
                    offer_cost = offer.cost_per_redemption * p_redeem
                    uplift = min(max(mean, -1.0), 1.0)
                    net = urgency * (uplift * value - offer_cost - channel_cost)
                    action = Action(
                        goal_id=self.goal_id,
                        channel=channel,
                        offer_id=offer.offer_id,
                        content_id=item.content_id,
                        format=item.format,
                        send_at=ctx.now,
                    )
                    group.append(
                        Bid(
                            goal_id=self.goal_id,
                            action=action,
                            uplift=uplift,
                            value_if_converted=value,
                            offer_cost=offer_cost,
                            channel_cost=channel_cost,
                            urgency=urgency,
                            expected_net=net,
                            uncertainty=urgency * value * std,
                        )
                    )
                if not self.per_theme and group:
                    # Best by expected net, not uplift: the offer is paid by everyone who buys, so a
                    # theme that lifts more can still lose money when the offer costs more than the sale.
                    group = [max(group, key=_rank)]
                out.extend(group)
        out.sort(
            key=lambda b: (
                -b.expected_net,
                b.action.channel.value,
                b.action.offer_id,
                b.action.content_id or "",
            )
        )
        return out

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        """Turn closed outcomes into response observations and update the response model.

        A treated observation is an ACT decision of this goal whose content we know (its action key
        needs the theme). A control observation is a "do nothing" decision in `control_groups`. The
        default is the holdout only: the agent's own do-nothing decisions go to the customers it
        judged least likely to respond, so counting them as control would bias uplift upward.
        Rewards whose decision is unknown are skipped.
        """
        observations: list[ResponseObservation] = []
        for reward in rewards:
            decision = decisions.get(reward.decision_id)
            if decision is None:
                continue
            if decision.kind is DecisionKind.NOTHING:
                if decision.group not in self.control_groups:
                    continue
                key: str | None = None
            else:
                action = decision.action
                if action is None or action.goal_id != self.goal_id or action.content_id is None:
                    continue
                theme = self._themes.get(action.content_id)
                if theme is None:
                    continue
                key = action_key(action.channel, action.offer_id, theme)
            observations.append(
                ResponseObservation(
                    segment=self._segment(decision.customer_id),
                    action_key=key,
                    converted=reward.converted,
                )
            )
        if observations:
            self.response_model.update(observations)

    def _segment(self, customer_id: str) -> str | None:
        if self.segment_of is not None:
            return self.segment_of(customer_id)
        return self._segments.get(customer_id)

    def _usable(self, items: Sequence[ContentItem]) -> list[ContentItem]:
        return [
            c
            for c in items
            if c.status is ContentStatus.APPROVED and (c.goal_id is None or c.goal_id == self.goal_id)
        ]

    @staticmethod
    def _by_theme(items: Sequence[ContentItem], channel: Channel, offer_id: str) -> list[ContentItem]:
        """One item per theme for this channel and offer: the lowest content_id, so runs repeat."""
        chosen: dict[str, ContentItem] = {}
        for item in sorted(items, key=lambda c: c.content_id):
            if item.channel == channel and item.offer_id == offer_id:
                chosen.setdefault(item.theme.value, item)
        return [chosen[t] for t in sorted(chosen)]
