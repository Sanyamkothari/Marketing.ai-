"""Goal bidders: what acting on one goal is worth for one customer, option by option.

Each active goal is a `GoalBidder`. For a customer in the goal's audience it lists every action the
envelope allows and the customer can receive (an allowed channel they are reachable on x an allowed
offer x approved content for that channel and offer, one item per theme and format) and prices each:

    expected_net = urgency x (uplift x value - offer cost x p_redeem - channel cost)

* `uplift` and its spread come from the `ResponseModel`, keyed by the content's labels
  (`channel|offer_id|theme`, plus `|format` for an image, carousel or video), so learning transfers
  across goals.
* `value` is what one conversion is worth for this customer (`ValueModel`).
* `p_redeem` is the chance they buy when treated: an offer is paid for by everyone who redeems it,
  including people who would have bought anyway, which is why "sure things" get no discount.
* `urgency` fades for time-sensitive goals (cart recovery) as the triggering moment passes.

**Products.** An action can be about one product (`Action.item_id`):

* A *refill* goal (`GoalKind.REPEAT_PURCHASE`, or a brief whose audience reads `scores.refill_*`)
  is about the customer's refill pick (`item_lookup(customer_id)`, e.g. the mapping that
  `autonomy.catalog.enrich_all` returns). When the refill window is open its uplift is raised by
  `refill_boost`, which grows with `refill_p14` and is shrunk towards no boost (see the constants).
  Its offers are paid for by everyone who refills while they are open, so `p_redeem` is at least
  `refill_p14`: free shipping for someone who refills anyway is a cost, not an incentive.
* A *cross-sell* goal is about the recommended complement (`recommender(customer_id)`, e.g. from
  `autonomy.catalog.next_best_item`).
* Any other goal names a product only on content that needs one (`{{recommended_product}}`).
* Content that needs `{{recommended_product}}` is eligible only when there is a product to name.

**Formats.** Approved content in `text_image`, `carousel` or `video` is a separate option from the
same words in plain text, so the models learn format like channel, offer and theme. An option is
offered only when its channel can carry its format (`autonomy.assets.support.supports`) and it has
media (`asset_url`); `asset_of(item)` names the approved asset behind it for `Action.asset_id`, and
an action about one product never shows an asset of a different product.

The bidder returns every option, including losing and negative ones: the attention auction, the
attention price, "do nothing", the hard rules and exploration all happen downstream. It does not
choose a send time; `Action.send_at` is `ctx.now` and the timing step replaces it. Pure maths, no
LLM, no clock.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from autonomy.assets.library import Asset
from autonomy.assets.support import supports
from autonomy.contracts import (
    Action,
    Audience,
    Bid,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Envelope,
    Format,
    GoalKind,
    Group,
    Reward,
)
from autonomy.decide import rules
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

# --- refill scores (written by autonomy.catalog.enrich; the names are repeated here so the inner
# loop does not import the catalogue's pandas code) -----------------------------------------------

REFILL_P14 = "refill_p14"
REFILL_DUE_DAYS = "refill_due_days"
REFILL_WINDOW_OPEN = "refill_window_open"
_REFILL_FIELD_PREFIX = "scores.refill_"

REFILL_LIFT = 1.0
"""How much an open refill window can raise a refill message's uplift at most (+100%)."""
REFILL_P14_HALF = 0.10
"""The `refill_p14` at which half of `REFILL_LIFT` is given. The boost is

    1 + REFILL_LIFT x p14 / (p14 + REFILL_P14_HALF)

so it is shrunk towards no boost when the cycle model gives a low chance of a refill (a p14 of 0.02
gives +17%), reaches +50% at 0.10 and +75% at 0.30, and never doubles. The scale is the catalogue
backtest's (docs/v2/CATALOG.md): an open window lifts the 14-day rebuy rate 1.5x to 2x. It is a
prior, not something learned per customer: the response model learns the refill message's own
rate from its sends, most of which go out while windows are open."""
REFILL_OPEN_LEAD_DAYS = 7.0
REFILL_OPEN_LATE_DAYS = 14.0
"""When `refill_window_open` is not on the state, the window counts as open from a week before the
refill is due until two weeks after (from `refill_due_days`)."""

RECOMMENDED_PRODUCT_SLOT = "recommended_product"
_PRODUCT_SLOT = re.compile(r"\{\{\s*recommended_product\s*\}\}")
_FORMAT_ORDER: Mapping[str, int] = MappingProxyType({f.value: i for i, f in enumerate(Format)})

ItemLookup = Callable[[str], str | tuple[str, str] | None]
"""customer_id -> the product an action is about: an item id, or (item id, plain reason); None for
nothing."""
AssetOf = Callable[[ContentItem], str | Asset | None]
"""A content item -> the approved asset it shows (its id, or the asset itself), None when unknown."""


@dataclass(frozen=True)
class ItemChoice:
    """The product one goal's actions are about for one customer, and why."""

    item_id: str
    kind: Literal["refill", "complement"]
    reason: str


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


# --- products and formats -----------------------------------------------------------------------


def _score(state: CustomerState, key: str) -> float | None:
    value = state.scores.get(key)
    return value if value is not None and math.isfinite(value) else None


def refill_window_open(state: CustomerState) -> bool:
    """Whether now is the time to remind this customer to refill (False without refill scores)."""
    flag = _score(state, REFILL_WINDOW_OPEN)
    if flag is not None:
        return flag >= 0.5
    due = _score(state, REFILL_DUE_DAYS)
    return due is not None and -REFILL_OPEN_LATE_DAYS <= due <= REFILL_OPEN_LEAD_DAYS


def refill_boost(state: CustomerState) -> float:
    """The factor an open refill window puts on a refill message's uplift (1 when it is closed)."""
    if not refill_window_open(state):
        return 1.0
    p = _score(state, REFILL_P14)
    if p is None:
        return 1.0
    p = min(max(p, 0.0), 1.0)
    return 1.0 + REFILL_LIFT * p / (p + REFILL_P14_HALF)


def uses_refill_scores(audience: Audience) -> bool:
    """Whether an audience is written on the refill scores (`scores.refill_*`)."""
    return any(f.field.startswith(_REFILL_FIELD_PREFIX) for f in audience.filters)


def needs_item(item: ContentItem) -> bool:
    """Whether the content names a product (`{{recommended_product}}`), so it needs one to name."""
    if RECOMMENDED_PRODUCT_SLOT in item.slots:
        return True
    return bool(_PRODUCT_SLOT.search(item.body) or (item.subject and _PRODUCT_SLOT.search(item.subject)))


def asset_lookup(assets: Iterable[Asset]) -> Callable[[ContentItem], Asset | None]:
    """An `asset_of` for `GoalBidder`: the approved asset behind a content item's `asset_url`.

    The Creative writes an image variant's URL (space-joined for a carousel) into `asset_url`; the
    first URL that belongs to an approved asset names it. Draft and retired assets never match.
    """
    by_url: dict[str, Asset] = {}
    for asset in sorted(assets, key=lambda a: a.asset_id):
        if asset.is_approved:
            by_url.setdefault(asset.url, asset)

    def lookup(item: ContentItem) -> Asset | None:
        for url in (item.asset_url or "").split():
            found = by_url.get(url)
            if found is not None:
                return found
        return None

    return lookup


def _as_choice(
    value: str | tuple[str, str] | None, kind: Literal["refill", "complement"], default: str
) -> ItemChoice | None:
    if value is None:
        return None
    item, reason = (value, default) if isinstance(value, str) else (value[0], value[1] or default)
    item = item.strip()
    return ItemChoice(item_id=item, kind=kind, reason=reason) if item else None


def _refill_reason(state: CustomerState) -> str:
    due = _score(state, REFILL_DUE_DAYS)
    if due is None:
        return "Bought it before and buys it again"
    if due < -0.5:
        return f"Refill about {-due:.0f} days overdue"
    if due <= 0.5:
        return "Refill due about now"
    return f"Refill due in about {due:.0f} days"


# --- pricing helpers ----------------------------------------------------------------------------


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
    """Order for picking one option: higher expected net, then less uncertain, then content id."""
    return (bid.expected_net, -bid.uncertainty, bid.action.content_id or "")


class GoalBidder:
    """One goal's bidder (implements `autonomy.interfaces.Bidder`).

    `content` is the approved library to choose from; None means read `ctx.content` at bid time.
    Items for another goal, and items that are not approved, are ignored. `kind` picks the default
    `urgency` (cart recovery fades; pass `urgency` to override). With `per_theme=True` (the default)
    each channel x offer gets one bid per theme and format, so the bandit downstream can learn over
    them; with False only the option with the highest expected net is bid.

    Products (see the module docstring): `item_lookup(customer_id)` gives the customer's refill pick
    and `recommender(customer_id)` a complementary product, each an item id or (item id, reason).
    `refill` says whether this is a refill goal; None decides from `kind` (repeat purchase) and the
    audience (it reads `scores.refill_*`).

    Formats: `asset_of(item)` names the approved asset behind a content item (`asset_lookup` builds
    one from the library). `carousel_template` says the brand has WhatsApp carousel templates.
    `format_costs` adds a per-message cost for a rich format (a media template, say), so an image
    wins only when its extra uplift pays for it.

    `learn(rewards, decisions)` is the nightly learner's entry point (`AutonomousPolicy.learn` calls
    it): treated outcomes of this goal's sends and untreated outcomes of the `control_groups`
    feed the response model. `segment_of(customer_id)` gives a customer's segment; without it the
    bidder uses the segment it saw when it last bid for that customer, else none (pooled).

    `own_spread=True` sets each bid's `uncertainty` from the option's own (treated) spread only, not
    the control rate's spread that all of a customer's options share (see `_option_std`); the default
    uses the full spread of the uplift.

    `prune_z` (None: off) drops options that cannot plausibly be this customer's best: those whose
    optimistic value (`expected_net + prune_z x uncertainty`) is below the pessimistic value of the
    best option (`expected_net - prune_z x uncertainty`). Thompson sampling would rarely pick them,
    but the exploration floor picks uniformly among whatever is offered, so without pruning it keeps
    handing a discount to someone who plainly buys at full price.

    `bids()` remembers each customer's segment and each content item's labels for `learn()`;
    `quote()` prices the same options and remembers nothing (for explaining a decision).
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
        prune_z: float | None = None,
        item_lookup: ItemLookup | None = None,
        recommender: ItemLookup | None = None,
        refill: bool | None = None,
        asset_of: AssetOf | None = None,
        carousel_template: bool = False,
        format_costs: Mapping[Format, float] | None = None,
    ) -> None:
        if prune_z is not None and prune_z < 0:
            raise ValueError("prune_z must be >= 0")
        self.goal_id = brief.goal_id
        self.kind = kind
        self.own_spread = own_spread
        self.prune_z = prune_z
        self.brief = brief
        self.response_model: ResponseModel = response_model or BetaResponseModel()
        self.value_model = value_model or ValueModel()
        # A partial override keeps the defaults for the channels it does not name, so a channel is
        # never priced as free just because the business only told us about email.
        self.costs: Mapping[Channel, float] = {**DEFAULT_CHANNEL_COSTS, **(costs or {})}
        self.format_costs: Mapping[Format, float] = dict(format_costs or {})
        self.urgency = urgency if urgency is not None else urgency_for(kind)
        self.per_theme = per_theme
        self._content = tuple(content) if content is not None else None
        self.segment_of = segment_of
        self.control_groups = control_groups
        self.item_lookup = item_lookup
        self.recommender = recommender
        self.refill = (
            refill
            if refill is not None
            else kind is GoalKind.REPEAT_PURCHASE or uses_refill_scores(brief.audience)
        )
        self.asset_of = asset_of
        self.carousel_template = carousel_template
        # What learn() needs but rewards do not carry: the theme and format behind each content id,
        # and the segment of each customer we bid for. Both are filled as a side effect of bids().
        self._labels: dict[str, tuple[str, Format]] = {
            c.content_id: (c.theme.value, c.format) for c in self._content or ()
        }
        self._segments: dict[str, str | None] = {}

    # -- products --

    @property
    def names_product(self) -> bool:
        """Whether every action of this goal is about a product (refill and cross-sell goals)."""
        return self.refill or self.kind is GoalKind.CROSS_SELL

    def item_for(self, state: CustomerState) -> ItemChoice | None:
        """The product this goal's actions are about for this customer, or None. Pure.

        A refill goal uses the refill pick only and a cross-sell goal the complement only; any
        other goal (for content that names a product) the complement, else the refill pick.
        """
        cid = state.customer_id
        refill = self.item_lookup(cid) if self.item_lookup is not None else None
        if self.refill:
            return _as_choice(refill, "refill", _refill_reason(state))
        complement = self.recommender(cid) if self.recommender is not None else None
        found = _as_choice(complement, "complement", "Goes well with what they bought")
        if found is not None or self.kind is GoalKind.CROSS_SELL:
            return found
        return _as_choice(refill, "refill", _refill_reason(state))

    # -- bidding --

    def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
        """Every allowed option for this customer, best expected net first; [] if not in the audience."""
        out = self.quote(state, ctx)
        if out:
            library = self._usable(self._content if self._content is not None else ctx.content)
            self._segments[state.customer_id] = state.segment
            self._labels.update((c.content_id, (c.theme.value, c.format)) for c in library)
        return out

    def quote(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
        """What `bids()` returns, without remembering anything for learning: safe to call to explain."""
        if not matches(state, self.brief.audience, ctx.now):
            return []
        envelope = self.brief.envelope
        reachable = set(state.contactable_channels())
        channels = [ch for ch in envelope.channels if ch in reachable]
        if not channels:
            return []
        library = self._usable(self._content if self._content is not None else ctx.content)
        urgency = self.urgency.at(state, ctx.now) if self.urgency is not None else 1.0
        value = self.value_model.value_if_converted(state, self.brief.objective)
        choice = self.item_for(state)
        boost = refill_boost(state) if self.refill and choice is not None else 1.0
        p14 = min(max(_score(state, REFILL_P14) or 0.0, 0.0), 1.0) if boost > 1.0 else 0.0

        out: list[Bid] = []
        for channel in channels:
            channel_cost = self.costs.get(channel, 0.0)
            for offer in envelope.offers:
                group: list[Bid] = []
                for item in self._options(library, channel, offer.offer_id, choice):
                    key = action_key(channel, offer.offer_id, item.theme, item.format)
                    mean, std = self.response_model.predict(state, key)
                    if self.own_spread:
                        std = _option_std(self.response_model, state, key, std)
                    plain = min(max(mean, -1.0), 1.0)
                    # The window raises what a message can do, not what a harmful one costs.
                    uplift = min(plain * boost, 1.0) if plain > 0 else plain
                    std *= boost
                    p_redeem = 0.0
                    if offer.cost_per_redemption:
                        p_redeem = self.response_model.treated_rate(state, key)
                        if boost > 1.0:
                            # Everyone who refills while the offer is open redeems it.
                            p_redeem = min(1.0, max(p_redeem, p14) + (uplift - plain))
                    offer_cost = offer.cost_per_redemption * p_redeem
                    cost = channel_cost + self.format_costs.get(item.format, 0.0)
                    net = urgency * (uplift * value - offer_cost - cost)
                    names = choice is not None and (self.names_product or needs_item(item))
                    action = Action(
                        goal_id=self.goal_id,
                        channel=channel,
                        offer_id=offer.offer_id,
                        content_id=item.content_id,
                        format=item.format,
                        send_at=ctx.now,
                        item_id=choice.item_id if names and choice is not None else None,
                        asset_id=self._asset(item)[0],
                    )
                    group.append(
                        Bid(
                            goal_id=self.goal_id,
                            action=action,
                            uplift=uplift,
                            value_if_converted=value,
                            offer_cost=offer_cost,
                            channel_cost=cost,
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
        if self.prune_z is not None and len(out) > 1:
            # The floor is set by options the offer rules let through for this customer: a discount
            # they asked not to get, one waiting for approval or one the budget cannot pay must not
            # crowd out the plain message the policy could still send.
            z = self.prune_z
            sendable = [b for b in out if _offer_allowed(b, state, envelope, ctx)]
            if sendable:
                floor = max(b.expected_net - z * b.uncertainty for b in sendable)
                out = [b for b in out if b.expected_net + z * b.uncertainty >= floor]
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
        needs the theme and format). A control observation is a "do nothing" decision in
        `control_groups`. The default is the holdout only: the agent's own do-nothing decisions go
        to the customers it judged least likely to respond, so counting them as control would bias
        uplift upward. Rewards whose decision is unknown are skipped.
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
                labels = self._labels.get(action.content_id)
                if labels is None:
                    continue
                key = action_key(action.channel, action.offer_id, labels[0], labels[1])
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

    def _asset(self, item: ContentItem) -> tuple[str | None, str | None]:
        """(asset id, the product the asset shows) for a content item, as far as `asset_of` knows."""
        if self.asset_of is None or item.format is Format.TEXT:
            return None, None
        found = self.asset_of(item)
        if isinstance(found, Asset):
            return found.asset_id, found.item_id
        return found, None

    def _format_ok(self, item: ContentItem) -> bool:
        """The channel can carry the item's format, and a rich format has media to show."""
        if item.format is Format.TEXT:
            return True
        if not supports(item.channel, item.format, carousel_template=self.carousel_template):
            return False
        return bool((item.asset_url or "").strip())

    def _options(
        self,
        items: Sequence[ContentItem],
        channel: Channel,
        offer_id: str,
        choice: ItemChoice | None,
    ) -> list[ContentItem]:
        """One item per theme and format for this channel and offer: the lowest content_id, so runs
        repeat. Content that names a product needs one, and an action about a product never shows
        media of another (an asset with no `item_id`, or one `asset_of` cannot name, is fine)."""
        chosen: dict[tuple[str, str], ContentItem] = {}
        for item in sorted(items, key=lambda c: c.content_id):
            if item.channel != channel or item.offer_id != offer_id or not self._format_ok(item):
                continue
            if choice is None and needs_item(item):
                continue
            shows = self._asset(item)[1]
            names = choice is not None and (self.names_product or needs_item(item))
            if names and shows is not None and choice is not None and shows != choice.item_id:
                continue  # a message about one product never shows another
            chosen.setdefault((item.theme.value, item.format.value), item)
        return [chosen[k] for k in sorted(chosen, key=lambda k: (k[0], _FORMAT_ORDER.get(k[1], 99)))]


def _offer_allowed(bid: Bid, state: CustomerState, envelope: Envelope, ctx: DecisionContext) -> bool:
    """Whether the hard rules that depend on the offer (not on the customer's week or the hour)
    would let this bid through: the ones that can block a pruned option's winner alone."""
    return all(
        r.allowed
        for r in (
            rules.offer_in_envelope(bid, envelope),
            rules.offer_approved(bid, envelope),
            rules.no_discounts(bid, state, envelope),
            rules.budget_left(bid, ctx.budget_left),
        )
    )
