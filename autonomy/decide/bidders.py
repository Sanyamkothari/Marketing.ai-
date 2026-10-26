"""Goal bidders: what acting on one goal is worth for one customer, option by option.

Each active goal is a `GoalBidder`. For a customer in the goal's audience it lists every action the
envelope allows and the customer can receive (an allowed channel they are reachable on x an allowed
offer x approved content for that channel and offer, one item per theme and format) and prices each:

    expected_net = urgency x (uplift x value - offer cost x p_redeem - channel cost)

* `uplift` and its spread come from the `ResponseModel`, keyed by the content's labels
  (`channel|offer_id|theme`, plus `|format` for an image, carousel or video), so learning transfers
  across goals.
* When an uplift model is attached (`uplift=`, or `autonomy.uplift.attach_uplift` on the response
  model), its per-customer score for the same key (learned from a past campaign and the customer's
  features, validated out of sample) is blended in by measured precision
  (`autonomy.uplift.score.blend`): customer features move the bid as far as the campaign showed they
  predict lift, and a response model that disagrees with what the campaign measured loses its weight.
* `value` is what one conversion is worth for this customer (`ValueModel`).
* `p_redeem` is the chance they buy when treated, organic chance of buying + uplift: an offer is
  paid for by everyone who redeems it, including people who would have bought anyway, which is why
  "sure things" get no discount (`organic_chance`).
* `urgency` fades for time-sensitive goals (cart recovery) as the triggering moment passes.

**Discount discipline.** An offer is believed only as far as the evidence goes. Its uplift is its
plain version's (the best no-offer option on the same channel and format) plus what the offer adds,
and that extra is shrunk towards nothing (`shrink_offer_extra`: a Normal prior centred on 0 with a
spread of `OFFER_PRIOR_SPREAD` x the plain uplift, against the spread of the evidence for the
difference). The evidence is the response model's: for the agent's click model, the segment's clicks
on past offers against past plain messages (including the logged campaigns of the warm start) and
the customer's own offer factor. The customer's `offer_sensitivity` (share of purchases made with an
offer) scales what is left (`offer_sensitivity_factor`). With pruning on (`prune_z`, as the agent
runs), an offer whose expected net, on that evidence, is below its plain version's is not bid at
all: the default is no offer, and an offer is tried only once the evidence says it pays.

**Sleeping dogs.** Some customers buy *less* after a message. `ContactHarm` (fitted nightly by
`autonomy.learning.update`, found with `contact_harm_of(response_model)`) keeps a posterior per
customer of how their buying moves after a message, from their own record, their neighbours along
a clue index and the business, read against the holdout once the agent runs. The share of their
own buying a message credibly costs them (`dent x organic chance`) comes off every option's uplift,
so "do nothing" moves ahead of all of them alike. With pruning on, a customer whose messages
credibly lower their buying gets no bids at all (`leave_alone`), except in about one week a year
(`LEAVE_ALONE_RETRY`), so a wrong verdict can be undone.

**Purchases weigh in on a gain read from clicks.** A put-off customer still clicks, so a response
model whose uplift is implied by clicks (`uplift_from_clicks`, the agent's) credits them with a
message's full gain. For such a model (`purchase_weighted`) the bidder weighs the click gain of the
customer's best plain option against their own purchases inside and outside message weeks
(`ContactHarm.purchase_shift`): Bayes on the ratio grid, with the click reading as the prior and
the record as the likelihood, so the record counts by how well it is measured. How far records
stray from click readings, beyond their noise, is learned from the customers themselves
(`ClickGap`, empirical Bayes), and so is the gap common to everyone, which is not charged to
anyone. Purchases only take off, never add. The purchase drop is taken off once: the larger of
that shift and the credible dent, never both (docs/v2/CLICK_HARM.md).

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

The bidder returns every option, including losing and negative ones (unless `prune_z` is set): the
attention auction, the attention price, "do nothing", the hard rules and exploration all happen
downstream. It does not choose a send time; `Action.send_at` is `ctx.now` and the timing step
replaces it. Pure maths, no LLM, no clock.
"""

from __future__ import annotations

import hashlib
import math
import re
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Protocol

import numpy as np
import numpy.typing as npt

from autonomy.assets.library import Asset
from autonomy.assets.support import supports
from autonomy.contracts import (
    NO_OFFER,
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
    OfferOption,
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
from autonomy.uplift.score import UpliftScorer, blend, uplift_of

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


# --- contact harm: customers a message makes buy less ------------------------------------------


class HarmEvidence(Protocol):
    """One customer's record around messages, as `ContactHarm` reads it.

    `autonomy.learning.observations.ContactEvidence` is the one the learner builds from events; the
    protocol keeps the inner loop free of the learning package. A record may also carry
    `message_purchases`, `sends_since_click` and the `post_*` counts of `ContactEvidence` (the time
    since the agent started); one without them is read as having none."""

    @property
    def exposed_days(self) -> float: ...
    @property
    def exposed_purchases(self) -> float: ...
    @property
    def open_days(self) -> float: ...
    @property
    def open_purchases(self) -> float: ...
    @property
    def sends(self) -> float: ...
    @property
    def clicks(self) -> float: ...
    @property
    def stops(self) -> float: ...
    @property
    def direct_visits(self) -> float: ...


HARM_MIN_CUSTOMERS = 50
"""Below this many customers with a record the model is not fitted and charges nothing."""
HARM_RIDGE = 1.0
"""Prior precision of each clue's (standardised) coefficient in the index: a clue says nothing until
purchases show it does."""
HARM_BANDS = 10
"""Customers are cut into this many equal bands along the clue index; each band's buying after a
message is read on its own."""
HARM_RATIO_SD = 0.5
"""Prior spread (log scale) of a band's or a segment's ratio around the business's: before its
purchases say otherwise, a band buys after a message like everyone else."""
HARM_CREDIBLE = 0.9
"""A band is charged only the dent its purchases make credible: 1 minus the upper 90% bound of its
ratio. A band whose buying merely might fall after a message is charged nothing. The same level
decides when a person is left alone (`LEAVE_ALONE_CREDIBLE`)."""
LEAVE_ALONE_CREDIBLE = HARM_CREDIBLE
"""A person is left alone when it is this credible, on their own posterior, that messages lower
their buying: a lower-confidence-bound rule, like the restraint rule, but on the harm. Below it the
harm is only charged (`ContactHarm.dent`)."""
HARM_ROUNDS = 25
EVIDENCE_HALF_LIFE_DAYS = 90.0
"""Recency weighting of the evidence the learner keeps (`ContactEvidence`): a record a quarter old
counts half. Annoyance begins at some point; the recent stretch says more about who someone is now."""
HARM_CLUES: tuple[str, ...] = ("clicks", "direct_visits", "stopped")
"""The clues, in the order of `HarmFit.coefficients`: click rate, direct-visit rate, ever
unsubscribed or complained. Three more were tried and left out (docs/v2/DOGS_STORM.md):

* message-attributed purchases: each is itself a purchase in the week after a message, so "never
  bought through a message" puts everyone who has not bought after one at the harmed end by
  construction;
* the run of messages since the last click (`ContactEvidence.sends_since_click`): it is the click
  rate again, and it ranked the put-off worse;
* the segment: it sorts heavy buyers, whom messages move least, in with the put-off."""
HARM_TAUS: tuple[float, ...] = (0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0)
"""Candidate spreads (log scale) of one person's ratio around their band's: the one the records
make most likely is used (empirical Bayes). Narrow when people in a band behave alike, so a thin
record is mostly its band; wide when they differ, so a person's own record counts."""
TREND_PSEUDO_PURCHASES = 5.0
"""The holdout's buying since the start, against what its history predicted, is shrunk per segment
towards the whole holdout's (and that towards no change) by this many expected purchases."""
HARM_MIN_HOLDOUT = 20
"""Fewer holdout customers than this with time since the start give no honest baseline: the organic
level since the start is then taken as unchanged."""
_CLICK_PSEUDO_SENDS = 5.0
"""A customer's click rate is shrunk towards the business's by this many messages."""
_VISIT_PSEUDO_DAYS = 30.0
"""A customer's direct-visit rate is shrunk towards the business's by this many days."""
HARM_BLOCK = 2048
"""Customers per block when the refit reads the customers x ratio-grid likelihood: a block's rows are
2048 x 241 floats (4 MB), and no pass holds more than a few blocks at once."""
_RATIO_GRID = np.exp(np.linspace(math.log(0.05), math.log(5.0), 241))
_LOG_GRID = np.log(_RATIO_GRID)
_TINY = 1e-12
CLICK_GAP_SPREADS: tuple[float, ...] = (0.0, *HARM_TAUS)
"""Candidate spreads (log scale) of how far one person's ratio, as their purchases read it, strays
from what their clicks predict: the one the records make most likely is used (`ClickGap`). 0 means
the records differ from the click readings by noise alone, and then no record moves a gain."""
CLICK_GAP_MIN = HARM_MIN_CUSTOMERS
"""Customers with both a record and a click reading needed before the gap is read at all."""
CLICK_GAP_ROUNDS = 8
"""Newton steps for the shared gap at each candidate spread (warm-started from the last fit)."""
CLICK_GAP_SAMPLE = 5000
"""The most customers the gap is read from: two numbers need no more, and it keeps the nightly cost
flat however large the business."""
_MIN_PRIOR_SD = 0.02
"""The narrowest a click reading's prior is drawn on the ratio grid (about one grid step)."""
_CLICK_SD_CAP = 3.0
"""The most a click reading's own spread (log scale) is taken to be: wider is as good as none."""


@dataclass(frozen=True)
class ClickGap:
    """How the customers' purchase records relate to what the click model predicts for them.

    For each customer with both, u = log(ratio their record reads) - log(ratio their click gain
    implies). Across customers u is taken as Normal(`offset`, `spread`^2 + the click reading's own
    variance), both chosen by the marginal likelihood of the records (empirical Bayes, as for
    `HarmFit.tau`).

    * `offset` is the gap everyone shares. The click model's scale is set against the holdout
      (extra purchases per click), so a shared gap is the record's own bias: messages go out when
      people are likelier to buy anyway (a refill is due, they just visited), which flatters the
      ratio a purchase record reads for everyone alike. It is taken off before a record is read
      against a click reading, so no one's gain moves for it.
    * `spread` is how far people really differ from their click reading beyond that. In a shop
      where clicks tell the whole story it is 0 and no record moves a gain; where some customers
      click but buy less after messages it is wide, and a clear record counts."""

    offset: float = 0.0
    spread: float = 0.0
    customers: int = 0
    """Customers it was read from (0: not read yet, and then it moves nothing)."""


def _click_reading(organic: float, gain: float, gain_sd: float) -> tuple[float, float]:
    """(log ratio, its spread) that a click model's gain implies: with `organic` the chance of a
    purchase in the window without a message (> 0), a message's week buys 1 + gain / organic times as
    much. The spread is the gain's sd carried to the log scale (delta method)."""
    r = min(max(1.0 + gain / organic, float(_RATIO_GRID[0])), float(_RATIO_GRID[-1]))
    return math.log(r), min(max(gain_sd, 0.0) / (organic * r), _CLICK_SD_CAP)


@dataclass(frozen=True)
class _OwnRecords:
    """Per customer, what their own record says about their ratio: of `trials` purchases, `hits`
    were made in the week after a message, where a message's weeks are `share` of their (weighted)
    time; `ok` marks customers with a usable record."""

    hits: npt.NDArray[np.float64]
    trials: npt.NDArray[np.float64]
    share: npt.NDArray[np.float64]
    ok: npt.NDArray[np.bool_]


def _own_loglik(
    hits: npt.NDArray[np.float64],
    trials: npt.NDArray[np.float64],
    share: npt.NDArray[np.float64],
    ok: npt.NDArray[np.bool_],
) -> npt.NDArray[np.float64]:
    """Binomial log-likelihood (up to a constant) of each record on the ratio grid, one row each: with
    ratio r, a purchase falls in a message's week with chance r x share / (r x share + 1 - share).
    Rows without a usable record are 0 (flat)."""
    x = np.where(ok, share, 0.5)
    ll = hits[:, None] * _LOG_GRID[None, :] - trials[:, None] * np.log(
        _RATIO_GRID[None, :] * x[:, None] + (1.0 - x[:, None])
    )
    return np.where(ok[:, None], ll, 0.0)


@dataclass(frozen=True)
class HarmFit:
    """What `ContactHarm` fitted.

    The ratio is buying in the week after a message as a multiple of the customer's own buying
    without one: above 1 a message adds to what they buy, below 1 it takes away."""

    coefficients: tuple[float, ...]
    """Per standardised clue (`HARM_CLUES`): its weight in the index (positive: more of it, more
    buying after a message), fitted on all customers; the index each customer gets comes from the
    other half of the customers (cross-fitting). The segment is not a clue (see `HARM_CLUES`)."""
    ratio: float
    """The business's ratio."""
    band_ratios: tuple[float, ...]
    """Posterior mean ratio per band, from the band least likely to buy after a message up."""
    band_dents: tuple[float, ...]
    """The credible dent charged per band (`HARM_CREDIBLE`)."""
    customers: int
    rows: int
    """Customers whose history could be read (some buying, some exposed and some open days)."""
    tau: float = 0.0
    """The spread of one person's (log) ratio around their neighbours', chosen by the records."""
    spread: float = HARM_RATIO_SD
    """How far a band's (log) ratio was let stray from the business's: as far as the bands really
    differ beyond their own noise."""
    segment_ratios: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    """Posterior mean ratio per segment, shrunk towards the business's."""
    holdout: int = 0
    """Holdout customers whose buying since the start set the organic baseline (0: not read)."""
    trend: float = 1.0
    """The holdout's buying since the start as a multiple of what its own history predicted."""
    post_rows: int = 0
    """Contacted customers whose record since the start was read (at the holdout's level)."""

    @property
    def share(self) -> float:
        """Share of customers in bands that are charged a dent."""
        return sum(1 for d in self.band_dents if d > 0) / len(self.band_dents) if self.band_dents else 0.0

    @property
    def effect(self) -> float:
        """The average dent charged in those bands (0 when there are none)."""
        charged = [d for d in self.band_dents if d > 0]
        return sum(charged) / len(charged) if charged else 0.0


class ContactHarm:
    """How much one message makes this customer buy less, learned from what happened after past sends.

    Some customers are put off by marketing: after a message they buy *less* than on their own
    (sleeping dogs). No single clue gives them away, but the clues a CRM holds tilt the odds: many
    messages and few clicks, coming in by direct visits rather than links, an unsubscribe or a
    complaint on any channel. Which clues matter, and whether people who show them really buy less,
    is learned from purchases. The result is a posterior per person, shrunk to the people most like
    them and those to the business.

    **The outcome** is each customer's own buying around messages. Given their N purchases, the
    share made in the week after a message is binomial, with odds = ratio x exposed / open days, so
    how much someone buys in general cancels out. The ratio is read on all of their buying, so it
    nets off whatever messages add: below 1, a message costs them more than it brings.

    **The holdout sets the organic level since the start.** Once the agent runs, most of its
    customers' exposed days fall after its start and most of their open days before it, so a season
    that lowers everyone's buying would read as harm. People we never contact show how buying moved
    on its own: the holdout's purchases since the start, against what each holdout customer's own
    earlier rate predicted (shrunk to their segment's by as much as people really differ), give the
    organic trend per segment, shrunk to the whole holdout's and that to no change. Each day since
    the start then counts at that level. Segments are the ones customers started in: today's segment
    depends on what they bought since, which is the outcome. Without a holdout (fewer than
    `min_holdout` customers with time since the start) the level is taken as unchanged.

    **Who is put off**, in layers:

    * **An index** orders customers from least to most likely to buy after a message: a logistic
      regression of that outcome on the clues (each the customer's own, shrunk towards the
      business's), with a ridge prior on each. It is cross-fitted: each customer's index comes from
      the coefficients fitted on the *other* half of the customers, so no one is placed low because
      they happened to buy less.
    * **Bands** (`HARM_BANDS` equal slices of the index) each read their own ratio from their
      members' purchases, shrunk towards the business's by as much as the bands really differ beyond
      their own noise (`spread`, at most `HARM_RATIO_SD`). The index's shape is not trusted: in a
      shop where keen clickers buy more after messages, a straight line would call the quiet loyal
      buyers harmed; the bands read what the quiet buyers actually do. And many groups are read
      every night: where they differ by noise alone, the shrinkage keeps one from looking harmed by
      chance.
    * **Each person** gets a posterior over their own ratio: their neighbours' (the band's worth of
      customers nearest them on the index, themselves left out, shrunk the same way) as the prior,
      spread by `tau` (the person-to-person spread the records make most likely, from `HARM_TAUS`),
      times the likelihood of their own purchases. A thin record stays near its neighbours; a long
      one is mostly the person.

    **What it charges.** `dent(customer)` is the dent their purchases make credible: 1 minus the
    upper `HARM_CREDIBLE` bound of their ratio (or their neighbours', if larger), 0 if that bound is
    above 1. A shop where messages harm no one has no band credibly below 1 and is charged nothing.
    `expected_loss(customer, organic)` = dent x their organic chance of buying in the window: what
    bidders take off every option's uplift (a purchase-weighted bidder takes the larger of it and
    `purchase_shift`, never both). `p_below(customer, ratio)` is the chance their own ratio
    is below `ratio` (a Normal summary); `p_harm` is the exact grid chance that it is below 1, which
    the bidders use to leave someone alone when it is credible that messages lower their buying
    (`leave_alone`) and which `likely` and `describe` count, so the two never disagree.
    `purchase_shift(customer, organic, gain, gain_sd)` weighs a click model's gain against their own
    record (see `GoalBidder`'s purchase weighting); `note_click` hands it the click readings the
    bidders made, from which each refit reads `click_gap`.

    **Memory.** `refit` reads the customers x 241 grid `block` customers at a time, so its peak
    memory does not grow with the number of customers beyond a few per-customer vectors.
    """

    def __init__(
        self,
        *,
        min_customers: int = HARM_MIN_CUSTOMERS,
        ridge: float = HARM_RIDGE,
        bands: int = HARM_BANDS,
        ratio_sd: float = HARM_RATIO_SD,
        credible: float = HARM_CREDIBLE,
        rounds: int = HARM_ROUNDS,
        taus: Sequence[float] = HARM_TAUS,
        min_holdout: int = HARM_MIN_HOLDOUT,
        block: int = HARM_BLOCK,
    ) -> None:
        if ridge <= 0 or bands < 1 or ratio_sd <= 0 or not 0.5 <= credible < 1 or rounds < 1:
            raise ValueError("ridge, bands, ratio_sd and rounds must be positive; credible in [0.5, 1)")
        if block < 1:
            raise ValueError("block must be at least 1")
        if not taus or min(taus) <= 0:
            raise ValueError("taus must be positive")
        self.min_customers = min_customers
        self.ridge = ridge
        self.bands = bands
        self.ratio_sd = ratio_sd
        self.credible = credible
        self.rounds = rounds
        self.taus = tuple(float(t) for t in taus)
        self.min_holdout = min_holdout
        self.block = block
        self.evidence: dict[str, HarmEvidence] = {}
        self.data_start: datetime | None = None
        """Where the business's records begin (set by the learner): evidence is counted from there."""
        self.split_at: datetime | None = None
        """Where the agent started (set by the learner): the records split into history and since."""
        self.holdout: set[str] = set()
        """Customers we never contact (set by the learner): the honest organic baseline."""
        self.segments: dict[str, str] = {}
        """Customer -> segment (set by the learner); a customer without one is pooled."""
        self.fit: HarmFit | None = None
        self._dent: dict[str, float] = {}
        self._p: dict[str, float] = {}
        self._post: dict[str, tuple[float, float]] = {}
        self._near_dent: dict[str, float] = {}
        self._own: dict[str, tuple[float, float, float]] = {}
        """Customer -> (purchases in a message's week, purchases, that week's share of their time):
        their own record, which `purchase_shift` weighs against the click model's gain."""
        self._click: dict[str, tuple[float, float]] = {}
        """Customer -> the latest click reading a bidder made for them (`note_click`)."""
        self.click_gap = ClickGap()
        """How purchase records relate to click readings, read at each refit (`ClickGap`)."""
        self._last_holdout: tuple[int, float] = (0, 1.0)
        self._dirty = False

    # -- evidence --

    def observe(self, updates: Mapping[str, HarmEvidence]) -> None:
        """Replace these customers' records (the learner's accumulators carry everything so far)."""
        if updates:
            self.evidence.update(updates)
            self._dirty = True

    def set_groups(
        self, *, holdout: Iterable[str] = (), segments: Mapping[str, str | None] | None = None
    ) -> None:
        """Add holdout customers and each customer's segment (the learner's view).

        A customer keeps the first segment they are given: today's segment depends on what they
        bought since the start (a lapsed customer who buys is lapsed no more), and that buying is
        the outcome being read, so a later segment would sort people by their own result."""
        new = set(holdout) - self.holdout
        if new:
            self.holdout |= new
            self._dirty = True
        for cid, seg in (segments or {}).items():
            if seg is not None and cid not in self.segments:
                self.segments[cid] = seg
                self._dirty = True

    def forget(self, customer_id: str) -> None:
        self.evidence.pop(customer_id, None)
        self._p.pop(customer_id, None)
        self._dent.pop(customer_id, None)
        self._post.pop(customer_id, None)
        self._near_dent.pop(customer_id, None)
        self._own.pop(customer_id, None)
        self._click.pop(customer_id, None)

    # -- reading --

    @property
    def fitted(self) -> bool:
        return self.fit is not None

    @property
    def share(self) -> float:
        return self.fit.share if self.fit is not None else 0.0

    @property
    def effect(self) -> float:
        return self.fit.effect if self.fit is not None else 0.0

    def p_harm(self, customer_id: str) -> float:
        """Chance that a message lowers this customer's buying (0 without a record).

        Read on the exact grid posterior the refit keeps. `leave_alone`, `likely` and `describe` all
        read this one number, so the count of customers who "look clearly put off" is the count of
        customers left alone. A posterior set without its grid (a hand-made model) falls back to the
        Normal reading of `p_below`."""
        found = self._p.get(customer_id)
        return found if found is not None else self.p_below(customer_id, 1.0)

    def p_below(self, customer_id: str, ratio: float) -> float:
        """Chance this customer's own ratio is below `ratio` (0 without a record: a stranger is the
        average customer, and nobody is left alone for being unknown)."""
        found = self._post.get(customer_id)
        if found is None or ratio <= 0:
            return 0.0
        mean, sd = found
        if sd <= 0:
            return 1.0 if mean < math.log(ratio) else 0.0
        return 0.5 * math.erfc(-(math.log(ratio) - mean) / (sd * math.sqrt(2.0)))

    def posterior(self, customer_id: str) -> tuple[float, float] | None:
        """(mean, sd) of the log of this customer's ratio, None without a record."""
        return self._post.get(customer_id)

    def dent(self, customer_id: str) -> float:
        """Share of their own buying one message credibly costs this customer (0 without a record)."""
        return self._dent.get(customer_id, 0.0)

    def expected_loss(self, customer_id: str, organic: float) -> float:
        """Chance of a purchase one message costs this customer: their dent x their organic chance."""
        return self.dent(customer_id) * min(max(organic, 0.0), 1.0)

    def likely(self, threshold: float = 0.5) -> int:
        """Customers whose chance of being harmed (`p_harm`) is at least `threshold`: at
        `LEAVE_ALONE_CREDIBLE`, exactly the customers `leave_alone` holds back."""
        known = set(self._p) | set(self._post)
        return sum(1 for cid in known if self.p_harm(cid) >= threshold)

    def near_dent(self, customer_id: str) -> float:
        """The dent the customer's neighbours along the clue index make credible, without their own
        record (0 when none is credible, or without a record)."""
        return self._near_dent.get(customer_id, 0.0)

    def note_click(self, customer_id: str, organic: float, gain: float, gain_sd: float) -> None:
        """Remember what a click model credits a message with for this customer (`gain` +-
        `gain_sd` on a chance `organic` of buying without one). The next refit reads the gap between
        these readings and the customers' records (`click_gap`); a later reading replaces an
        earlier one."""
        o = min(max(organic, 0.0), 1.0)
        if o > 0.0 and math.isfinite(gain) and math.isfinite(gain_sd):
            self._click[customer_id] = _click_reading(o, gain, gain_sd)

    def purchase_shift(self, customer_id: str, organic: float, gain: float, gain_sd: float) -> float:
        """How much the customer's own purchases take off the click model's gain (negative: add).

        The click model credits a message with `gain` (+- `gain_sd`): extra chance of a purchase in
        its week. With `organic` their chance without one, that is a ratio r_c = 1 + gain / organic
        of buying in a message's week to buying without. Their own record reads the same ratio from
        purchases: of their N purchases, how many fell in a message's week (`_own`, against the
        holdout's organic level since the start, as for the harm posterior). The two are combined by
        Bayes on the ratio grid. The click reading is the prior: a log-Normal centred on r_c (moved
        by the gap every record shares, `click_gap.offset`) whose spread is the click model's own
        (`gain_sd` on the log scale) together with how far people's records really stray from their
        click readings (`click_gap.spread`). The record is the likelihood. So the record's weight
        grows with how well it pins the ratio down against how sure the click reading is (an
        inverse-variance blend, done exactly on the grid): a customer with no record keeps the click
        gain (shift 0); one with a long record of buying less after messages ends with a gain near
        or below zero; one who buys more reads a larger ratio (a negative shift, which the bidder
        does not add: `GoalBidder._purchase_weighted_loss`). Before the gap is read
        (`click_gap.customers` 0) or where it is nil, only the click model's own spread lets the
        record in.

        The shift is organic x (prior mean of r - posterior mean of r), carried back to the click
        model's scale (times e^-offset), so it is exactly 0 without a record, whatever the grid's
        edges do to either mean. 0 when nothing is fitted."""
        own = self._own.get(customer_id)
        o = min(max(organic, 0.0), 1.0)
        if (
            own is None
            or self.fit is None
            or o <= 0.0
            or not (math.isfinite(gain) and math.isfinite(gain_sd))
        ):
            return 0.0
        centre, s_click = _click_reading(o, gain, gain_sd)
        gap = self.click_gap
        spread = max(math.sqrt(s_click * s_click + gap.spread * gap.spread), _MIN_PRIOR_SD)
        mid = min(max(centre + gap.offset, float(_LOG_GRID[0])), float(_LOG_GRID[-1]))
        prior = _normal_on_grid(np.array([mid]), np.array([spread]))[0]
        hits, trials, share = own
        ll = _own_loglik(
            np.array([hits]), np.array([trials]), np.array([share]), np.array([True], dtype=np.bool_)
        )[0]
        post = prior * np.exp(ll - ll.max())
        post /= post.sum()
        return o * math.exp(-gap.offset) * float(((prior - post) * _RATIO_GRID).sum())

    def describe(self) -> str:
        """One plain sentence for logs and the decision view."""
        fit = self.fit
        if fit is None:
            return "Not enough history yet to tell who is put off by messages."
        clearly = self.likely(LEAVE_ALONE_CREDIBLE)
        who = f"; {clearly} look clearly put off on their own record" if clearly else ""
        if fit.share <= 0:
            return (
                f"No sign that messages put anyone off as a group: across {fit.customers} customers, "
                f"buying after a message runs at {fit.ratio:.2f} times their own{who}."
            )
        return (
            f"About {fit.share:.0%} of customers look put off by messages (few clicks, direct "
            f"visits, past unsubscribes): they buy at least {fit.effect:.0%} less of their own in "
            f"the week after one{who}."
        )

    # -- fitting --

    def refit(self) -> None:
        """Fit the model to every record (a no-op when nothing changed since the last fit).

        Every step that reads the customers x ratio-grid likelihood goes through it `block`
        customers at a time (`_rows` recomputes a block's rows from the per-customer counts), so
        peak memory stays at a few blocks of the grid whatever the number of customers: the whole
        grid is about 190 MB at 100,000 customers. Sums over customers (bands, segments, the
        business) are added up block by block; the neighbours' sliding sums use running totals kept
        at every block boundary (`_cum_rows`)."""
        if not self._dirty and self.fit is not None:
            return
        self._dirty = False
        ids = sorted(self.evidence)
        if len(ids) < self.min_customers:
            self.fit = None
            self._p, self._dent, self._post = {}, {}, {}
            self._own, self._near_dent = {}, {}
            self.click_gap = ClickGap()
            return
        rows = [self.evidence[c] for c in ids]
        n = len(ids)

        def col(name: str) -> npt.NDArray[np.float64]:
            return np.array([max(float(getattr(r, name, 0.0) or 0.0), 0.0) for r in rows], dtype=np.float64)

        exposed_p, open_p = col("exposed_purchases"), col("open_purchases")
        exposed_d, open_d = col("exposed_days"), col("open_days")
        post_d, post_xd = col("post_days"), col("post_exposed_days")
        post_p, post_xp = col("post_purchases"), col("post_exposed_purchases")
        sends = col("sends")
        clicks = np.minimum(col("clicks"), sends)
        visits = col("direct_visits")
        stopped = np.minimum(col("stops"), 1.0)
        days = exposed_d + open_d

        # The history, the record before the agent started: each customer's earlier buying rate,
        # which the holdout's trend is measured against.
        hist_xp = np.maximum(exposed_p - post_xp, 0.0)
        hist_xd = np.maximum(exposed_d - post_xd, 0.0)
        hist_op = np.maximum(open_p - (post_p - post_xp), 0.0)
        hist_od = np.maximum(open_d - (post_d - post_xd), 0.0)
        hist_n = hist_xp + hist_op
        hist_days = hist_xd + hist_od
        ok = (hist_xd > 0) & (hist_od > 0) & (hist_n > 0)

        # Since the start, the holdout's organic level (1 without a holdout).
        seg_of = [self.segments.get(c) for c in ids]
        trend_i = self._holdout_trend(ids, seg_of, hist_n, hist_days, post_d, post_p)
        holdout_n, trend = self._last_holdout

        # Each person's own record, history and since the start together: given their N purchases,
        # the share made in the week after a message is binomial with odds = ratio x exposed / open
        # days, each day since the start counted at the holdout's organic level then, so a season
        # everyone shares is not read as harm.
        post_od = np.maximum(post_d - post_xd, 0.0)
        exp_w = hist_xd + trend_i * post_xd
        open_w = hist_od + trend_i * post_od
        n_buys = exposed_p + open_p
        own_ok = (exp_w > 0) & (open_w > 0) & (n_buys > 0)

        # The clues, each shrunk towards the business's.
        c_pool = max(float(clicks.sum()), 0.5) / max(float(sends.sum()), 1.0)
        v_pool = max(float(visits.sum()), 0.5) / max(float(days.sum()), 1.0)
        click_rate = (clicks + c_pool * _CLICK_PSEUDO_SENDS) / (sends + _CLICK_PSEUDO_SENDS)
        visit_rate = (visits + v_pool * _VISIT_PSEUDO_DAYS) / (days + _VISIT_PSEUDO_DAYS)
        raw = np.column_stack([np.log(click_rate / c_pool), np.log(visit_rate / v_pool), stopped])
        scale = raw.std(axis=0)
        feats = (raw - raw.mean(axis=0)) / np.where(scale > 1e-9, scale, 1.0)

        offset = np.log(np.maximum(exp_w, _TINY) / np.maximum(open_w, _TINY))
        design = np.column_stack([np.ones(n), feats])

        def fit_on(mask: npt.NDArray[np.bool_]) -> npt.NDArray[np.float64]:
            m = mask & own_ok
            return _logit_fit(design[m], exposed_p[m], n_buys[m], offset[m], self.ridge, self.rounds)

        # cross-fitting: each half's index comes from the other half's coefficients
        half = np.array([_stable_half(c) for c in ids], dtype=np.bool_)
        index = np.where(half, feats @ fit_on(~half)[1:], feats @ fit_on(half)[1:])
        everyone = fit_on(np.ones(n, dtype=np.bool_))

        grid, log_grid = _RATIO_GRID, _LOG_GRID
        x = np.where(own_ok, exp_w / np.maximum(exp_w + open_w, _TINY), 0.5)
        lik_in = _OwnRecords(hits=exposed_p, trials=n_buys, share=x, ok=own_ok)
        in_holdout = np.array([c in self.holdout for c in ids], dtype=np.bool_)
        post_ok = own_ok & (post_xd > 0) & ~in_holdout
        order = np.argsort(index, kind="stable")
        sizes = [len(g) for g in np.array_split(order, self.bands)]
        block = self.block

        # Pass 1, along the index: the sums every group needs, and running totals at each block
        # boundary for the neighbours' sliding sums.
        seg_names = sorted({s for s in seg_of if s is not None})
        seg_code = np.array([seg_names.index(s) if s is not None else -1 for s in seg_of], dtype=np.int64)[
            order
        ]
        bounds = np.concatenate([[0], np.cumsum(sizes)]).astype(np.int64)
        total = np.zeros(len(grid))
        band_sums = np.zeros((self.bands, len(grid)))
        seg_sums = np.zeros((len(seg_names), len(grid)))
        checkpoints = np.zeros(((n + block - 1) // block + 1, len(grid)))
        for i, a in enumerate(range(0, n, block)):
            b = min(a + block, n)
            ll = self._rows(lik_in, order[a:b])
            total += ll.sum(axis=0)
            for j in range(self.bands):
                lo, hi = max(int(bounds[j]), a), min(int(bounds[j + 1]), b)
                if lo < hi:
                    band_sums[j] += ll[lo - a : hi - a].sum(axis=0)
            codes = seg_code[a:b]
            for k in np.unique(codes[codes >= 0]).tolist():
                seg_sums[k] += ll[codes == k].sum(axis=0)
            checkpoints[i + 1] = np.cumsum(np.vstack([checkpoints[i], ll]), axis=0)[-1]

        business = log_grid[int(np.argmax(total))]
        # How far a band's ratio is let stray from the business's: as far as the bands really differ
        # beyond their own noise (empirical Bayes), at most `ratio_sd`. Many bands and windows are
        # read every night; in a shop where they differ by noise alone, this keeps one of them from
        # reading as credibly harmed by chance.
        band_spread = _spread_between(list(band_sums), self.ratio_sd)
        prior = -0.5 * ((log_grid - business) / band_spread) ** 2

        seg_scores = {seg: seg_sums[k] for k, seg in enumerate(seg_names)}
        seg_prior = (
            -0.5 * ((log_grid - business) / _spread_between(list(seg_scores.values()), self.ratio_sd)) ** 2
        )
        seg_ratios = {
            seg: float(math.exp(_grid_moments(sc + seg_prior)[0])) for seg, sc in seg_scores.items()
        }
        band_ratios: list[float] = []
        band_dents: list[float] = []
        for j in range(self.bands):
            score = band_sums[j] + prior
            post = np.exp(score - score.max())
            post /= post.sum()
            upper = float(grid[min(int(np.searchsorted(np.cumsum(post), self.credible)), len(grid) - 1)])
            band_dents.append(max(0.0, 1.0 - upper))
            band_ratios.append(float((post * grid).sum()))

        # Pass 2, along the index: each person's neighbours (a band's worth of the customers nearest
        # them, themselves left out, shrunk to the business) as the prior.
        near_mean, near_sd, near_upper = self._neighbours(
            lik_in, order, max(n // (2 * self.bands), 1), prior, checkpoints
        )

        # Pass 3: tau, the person-to-person spread the records make most likely.
        marginal = np.zeros(len(self.taus))
        for a in range(0, n, block):
            idx = np.arange(a, min(a + block, n))
            ll = self._rows(lik_in, idx)
            lik = np.exp(ll - ll.max(axis=1, keepdims=True))
            mine = own_ok[idx]
            for t, tau in enumerate(self.taus):
                pri = _normal_on_grid(near_mean[idx], np.sqrt(near_sd[idx] ** 2 + tau * tau))
                marginal[t] += float(np.log(np.maximum((pri * lik)[mine].sum(axis=1), _TINY)).sum())
        best_tau, best_ll = self.taus[0], -math.inf
        for tau, value in zip(self.taus, marginal.tolist(), strict=True):
            if value > best_ll + 1e-9:
                best_tau, best_ll = tau, value

        # Pass 4: each person's posterior, their neighbours spread by tau times their own record.
        mean = np.empty(n)
        sd = np.empty(n)
        own_dent = np.empty(n)
        p_harm = np.empty(n)
        below = grid < 1.0
        for a in range(0, n, block):
            idx = np.arange(a, min(a + block, n))
            ll = self._rows(lik_in, idx)
            lik = np.exp(ll - ll.max(axis=1, keepdims=True))
            own_post = _normal_on_grid(near_mean[idx], np.sqrt(near_sd[idx] ** 2 + best_tau * best_tau)) * lik
            own_post /= np.maximum(own_post.sum(axis=1, keepdims=True), _TINY)
            m = (own_post * log_grid[None, :]).sum(axis=1)
            mean[idx] = m
            sd[idx] = np.sqrt(np.maximum((own_post * (log_grid[None, :] - m[:, None]) ** 2).sum(axis=1), 0.0))
            cut = np.minimum((np.cumsum(own_post, axis=1) < self.credible).sum(axis=1), len(grid) - 1)
            own_dent[idx] = np.maximum(1.0 - grid[cut], 0.0)
            p_harm[idx] = own_post[:, below].sum(axis=1)
        # Their charge is the larger of the two credible dents: their own, and their neighbours'
        # (someone who looks like the put-off but has bought too little to show it is charged what
        # the people most like them make credible).
        near_dent = np.maximum(1.0 - near_upper, 0.0)
        dent = np.maximum(own_dent, near_dent)

        self.fit = HarmFit(
            coefficients=tuple(float(b) for b in everyone[1:]),
            ratio=float(math.exp(business)),
            band_ratios=tuple(band_ratios),
            band_dents=tuple(band_dents),
            customers=n,
            rows=int(ok.sum()),
            tau=best_tau,
            spread=band_spread,
            segment_ratios=MappingProxyType(seg_ratios),
            holdout=holdout_n,
            trend=trend,
            post_rows=int(post_ok.sum()),
        )
        self._dent = {cid: float(d) for cid, d in zip(ids, dent.tolist(), strict=True) if d > 0}
        self._near_dent = {cid: float(d) for cid, d in zip(ids, near_dent.tolist(), strict=True) if d > 0}
        self._p = {cid: float(p) for cid, p in zip(ids, p_harm.tolist(), strict=True)}
        self._post = {
            cid: (float(m), float(s)) for cid, m, s in zip(ids, mean.tolist(), sd.tolist(), strict=True)
        }
        self._own = {
            cid: (float(k), float(t), float(s))
            for cid, k, t, s, good in zip(
                ids, exposed_p.tolist(), n_buys.tolist(), x.tolist(), own_ok.tolist(), strict=True
            )
            if good
        }
        self.click_gap = self._fit_click_gap(lik_in, ids)

    def _fit_click_gap(self, records: _OwnRecords, ids: Sequence[str]) -> ClickGap:
        """Read `ClickGap` from the customers with a usable record and a click reading.

        For each candidate spread (`CLICK_GAP_SPREADS`) the shared offset that makes the records
        most likely is found by safeguarded Newton steps (warm-started from the last fit), and the
        spread whose marginal likelihood is highest is kept. At most `CLICK_GAP_SAMPLE` customers
        are read, picked by a fixed hash of their id (two numbers need no more), a block at a time
        like the refit. Fewer than `CLICK_GAP_MIN`: nothing is read, and no record moves a gain
        beyond what the click model's own spread lets in."""
        pick = [i for i, cid in enumerate(ids) if records.ok[i] and cid in self._click]
        if len(pick) > CLICK_GAP_SAMPLE:
            pick = sorted(sorted(pick, key=lambda i: _stable_key(ids[i]))[:CLICK_GAP_SAMPLE])
        if len(pick) < CLICK_GAP_MIN:
            return ClickGap(customers=len(pick))
        idx_all = np.array(pick, dtype=np.int64)
        centre_all = np.array([self._click[ids[i]][0] for i in pick], dtype=np.float64)
        sd_all = np.array([self._click[ids[i]][1] for i in pick], dtype=np.float64)
        k = len(pick)

        def sweep(offset: float, spread: float) -> tuple[float, float, float]:
            """(marginal log-likelihood, its slope in the offset, the curvature's estimate)."""
            marginal, score, info = 0.0, 0.0, 0.0
            for a in range(0, k, self.block):
                b = min(a + self.block, k)
                ll = self._rows(records, idx_all[a:b])
                top = ll.max(axis=1, keepdims=True)
                centre = np.clip(centre_all[a:b] + offset, _LOG_GRID[0], _LOG_GRID[-1])
                var = np.maximum(sd_all[a:b] ** 2 + spread * spread, _MIN_PRIOR_SD**2)
                pri = _normal_on_grid(centre, np.sqrt(var))
                joint = pri * np.exp(ll - top)
                mass = np.maximum(joint.sum(axis=1), _TINY)
                marginal += float((np.log(mass) + top[:, 0]).sum())
                post = joint / mass[:, None]
                mean = (post * _LOG_GRID[None, :]).sum(axis=1)
                post_var = (post * (_LOG_GRID[None, :] - mean[:, None]) ** 2).sum(axis=1)
                # d/d offset of log m_i = (posterior mean - prior mean) / prior var; its expected
                # curvature is (1 - posterior var / prior var) / prior var.
                score += float(((mean - centre) / var).sum())
                info += float((np.maximum(1.0 - post_var / var, 0.0) / var).sum())
            return marginal, score, info

        best = ClickGap(customers=k)
        best_ll = -math.inf
        for spread in CLICK_GAP_SPREADS:
            offset = self.click_gap.offset
            value, score, info = sweep(offset, spread)
            for _ in range(CLICK_GAP_ROUNDS):
                if info <= _TINY or abs(score) < 1e-6:
                    break
                step = min(max(score / info, -0.5), 0.5)
                while abs(step) > 1e-4:
                    trial = sweep(offset + step, spread)
                    if trial[0] >= value:
                        offset += step
                        value, score, info = trial
                        break
                    step /= 2.0
                else:
                    break
            if value > best_ll + 1e-9:
                best, best_ll = ClickGap(offset=offset, spread=spread, customers=k), value
        return best

    def _rows(self, records: _OwnRecords, idx: npt.NDArray[np.int64]) -> npt.NDArray[np.float64]:
        """The log-likelihood of these customers' own records on the ratio grid, one row each (0 for
        a customer with no usable record). Every pass of `refit` reads the grid through here, a
        block at a time."""
        return _own_loglik(records.hits[idx], records.trials[idx], records.share[idx], records.ok[idx])

    def _cum_rows(
        self,
        records: _OwnRecords,
        order: npt.NDArray[np.int64],
        checkpoints: npt.NDArray[np.float64],
        start: int,
        stop: int,
    ) -> npt.NDArray[np.float64]:
        """Running totals of the rows along `order`, at positions `start..stop` inclusive (position p
        holds the sum of the first p rows). Rebuilt from the nearest block boundary at or before
        `start`, so at most two blocks of rows are read."""
        base = (start // self.block) * self.block
        ll = self._rows(records, order[base:stop])
        cum = np.cumsum(np.vstack([checkpoints[base // self.block], ll]), axis=0)
        return cum[start - base : stop - base + 1]

    def _neighbours(
        self,
        records: _OwnRecords,
        order: npt.NDArray[np.int64],
        half_width: int,
        prior: npt.NDArray[np.float64],
        checkpoints: npt.NDArray[np.float64],
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """Per customer, the mean and sd of the log ratio read from the `half_width` customers on each
        side of them along the index (themselves left out), with `prior` (a log-density on the grid),
        and the upper `credible` bound of that ratio.

        A sliding band: everyone is read against the people most like them, so someone at the edge of a
        band is not moved by which side of an arbitrary cut they fall, and the reading changes smoothly
        as their clues do. Worked a block of positions at a time: the window sum of position p is the
        running total at p + half_width + 1 minus the one at p - half_width (`_cum_rows`)."""
        n = len(order)
        mean = np.empty(n)
        sd = np.empty(n)
        upper = np.empty(n)
        for a in range(0, n, self.block):
            b = min(a + self.block, n)
            pos = np.arange(a, b)
            lo = np.clip(pos - half_width, 0, n)
            hi = np.clip(pos + half_width + 1, 0, n)
            cum_lo = self._cum_rows(records, order, checkpoints, int(lo[0]), int(lo[-1]))
            cum_hi = self._cum_rows(records, order, checkpoints, int(hi[0]), int(hi[-1]))
            own = self._rows(records, order[a:b])
            score = cum_hi[hi - hi[0]] - cum_lo[lo - lo[0]] - own + prior[None, :]
            post = np.exp(score - score.max(axis=1, keepdims=True))
            post /= post.sum(axis=1, keepdims=True)
            m = (post * _LOG_GRID[None, :]).sum(axis=1)
            s = np.sqrt(np.maximum((post * (_LOG_GRID[None, :] - m[:, None]) ** 2).sum(axis=1), 0.0))
            cut = np.minimum((np.cumsum(post, axis=1) < self.credible).sum(axis=1), len(_RATIO_GRID) - 1)
            where = order[a:b]
            mean[where] = m
            sd[where] = s
            upper[where] = _RATIO_GRID[cut]
        return mean, sd, upper

    def _holdout_trend(
        self,
        ids: Sequence[str],
        seg_of: Sequence[str | None],
        hist_n: npt.NDArray[np.float64],
        hist_days: npt.NDArray[np.float64],
        post_d: npt.NDArray[np.float64],
        post_p: npt.NDArray[np.float64],
    ) -> npt.NDArray[np.float64]:
        """Per customer, the organic level of buying since the start as a multiple of before it: the
        holdout's purchases since the start against what each holdout customer's own earlier rate
        predicted, in the customer's segment, shrunk towards the whole holdout's (and that towards
        no change). 1 where there is no honest baseline. Also sets `_last_holdout` (holdout customers
        read, the overall trend)."""
        n = len(ids)
        ones = np.ones(n)
        self._last_holdout = (0, 1.0)
        if not (post_d > 0).any():
            return ones
        # their own rate before the start, shrunk towards their segment's (pooled without one) by as
        # much as people really differ: a light buyer's lucky month is mostly luck
        keys = [s if s is not None else "" for s in seg_of]
        k_days = _pseudo_days(hist_n, hist_days)
        pooled = float(hist_n.sum()) / max(float(hist_days.sum()), _TINY)
        seg_rate: dict[str, float] = {}
        for k in sorted(set(keys)):
            m = np.array([x == k for x in keys], dtype=np.bool_)
            d = float(hist_days[m].sum())
            seg_rate[k] = (float(hist_n[m].sum()) + pooled * k_days) / (d + k_days)
        prior_rate = np.array([seg_rate[k] for k in keys])
        own_rate = (hist_n + prior_rate * k_days) / (hist_days + k_days)
        predicted = own_rate * post_d
        held = np.array([c in self.holdout for c in ids], dtype=np.bool_) & (post_d > 0)
        if int(held.sum()) < self.min_holdout:
            return ones
        k0 = TREND_PSEUDO_PURCHASES
        overall = (float(post_p[held].sum()) + k0) / (float(predicted[held].sum()) + k0)
        trend_of: dict[str, float] = {}
        for k in sorted(set(keys)):
            m = held & np.array([x == k for x in keys], dtype=np.bool_)
            trend_of[k] = (float(post_p[m].sum()) + overall * k0) / (float(predicted[m].sum()) + k0)
        self._last_holdout = (int(held.sum()), overall)
        return np.array([trend_of[k] for k in keys])


def _pseudo_days(n: npt.NDArray[np.float64], d: npt.NDArray[np.float64]) -> float:
    """How many days of the average customer's buying one person's own record is shrunk by
    (empirical Bayes for Poisson buying with Gamma-distributed rates): the mean rate over the
    spread of true rates, read from how much more people's counts vary than chance alone makes
    them. With no spread beyond chance everyone is the average (a very large number)."""
    total = float(d.sum())
    if total <= 0:
        return _NO_SPREAD_DAYS
    m = float(n.sum()) / total
    spread = (float(((n - m * d) ** 2).sum()) - m * total) / max(float((d * d).sum()), _TINY)
    if spread <= 0 or m <= 0:
        return _NO_SPREAD_DAYS
    return float(min(max(m / spread, 1.0), _NO_SPREAD_DAYS))


_NO_SPREAD_DAYS = 1e6


HARM_SPREAD_FLOOR = 0.05
"""The least a band's (or segment's) ratio may stray from the business's, on the log scale."""


def _spread_between(scores: Sequence[npt.NDArray[np.float64]], ceiling: float) -> float:
    """How much groups' log ratios really differ: the DerSimonian-Laird moment estimate from each
    group's own reading (its log-likelihood on the grid, read with a flat prior), between
    `HARM_SPREAD_FLOOR` and `ceiling`. As `autonomy.decide.models.shrink_to_pool` does for lift."""
    read = [_grid_moments(sc) for sc in scores if np.ptp(sc) > 1e-9]
    usable = [(m, sd) for m, sd in read if sd > 1e-6]
    if len(usable) < 2:
        return HARM_SPREAD_FLOOR
    w = np.array([1.0 / (sd * sd) for _, sd in usable])
    m = np.array([mean for mean, _ in usable])
    centre = float((w * m).sum() / w.sum())
    q = float((w * (m - centre) ** 2).sum())
    denom = float(w.sum() - (w * w).sum() / w.sum())
    tau2 = max(0.0, (q - (len(usable) - 1)) / denom) if denom > 0 else 0.0
    return float(min(max(math.sqrt(tau2), HARM_SPREAD_FLOOR), ceiling))


def _grid_moments(score: npt.NDArray[np.float64]) -> tuple[float, float]:
    """Mean and sd of the log ratio under a log-posterior `score` on the ratio grid."""
    post = np.exp(score - score.max())
    post /= post.sum()
    mean = float((post * _LOG_GRID).sum())
    return mean, math.sqrt(max(float((post * (_LOG_GRID - mean) ** 2).sum()), 0.0))


def _normal_on_grid(
    centre: npt.NDArray[np.float64], spread: npt.NDArray[np.float64]
) -> npt.NDArray[np.float64]:
    """Per row, a Normal(centre, spread) on the log-ratio grid, normalised to sum to 1."""
    z = (_LOG_GRID[None, :] - centre[:, None]) / np.maximum(spread[:, None], 1e-6)
    out = np.exp(-0.5 * z * z)
    normalised: npt.NDArray[np.float64] = out / np.maximum(out.sum(axis=1, keepdims=True), _TINY)
    return normalised


def _stable_key(customer_id: str) -> int:
    """A fixed number per customer (not Python's salted hash), for picking a sample."""
    return int.from_bytes(hashlib.blake2b(customer_id.encode(), digest_size=8).digest(), "big")


def _stable_half(customer_id: str) -> bool:
    """A fixed coin flip per customer (not Python's salted hash), for cross-fitting."""
    return int.from_bytes(hashlib.blake2b(customer_id.encode(), digest_size=2).digest(), "big") % 2 == 1


def _logit_fit(
    design: npt.NDArray[np.float64],
    hits: npt.NDArray[np.float64],
    trials: npt.NDArray[np.float64],
    offset: npt.NDArray[np.float64],
    ridge: float,
    rounds: int,
) -> npt.NDArray[np.float64]:
    """Binomial logistic regression with an offset, by Newton steps. `ridge` is the prior precision of
    every coefficient but the first (the intercept)."""
    k = design.shape[1]
    beta = np.zeros(k)
    if design.shape[0] == 0:
        return beta
    penalty = np.full(k, ridge)
    penalty[0] = 0.0
    for _ in range(rounds):
        eta = offset + design @ beta
        mu = 1.0 / (1.0 + np.exp(-np.clip(eta, -30.0, 30.0)))
        grad = design.T @ (hits - trials * mu) - penalty * beta
        hess = design.T @ (design * (trials * mu * (1.0 - mu))[:, None]) + np.diag(penalty) + 1e-6 * np.eye(k)
        step = np.linalg.solve(hess, grad)
        beta = beta + step
        if float(np.abs(step).max()) < 1e-8:
            break
    return beta


LEAVE_ALONE_RETRY = 0.02
"""The exploration floor of leaving alone: the chance, per customer and calendar week, that someone
left alone is bid for as usual that week, so a wrongly flagged customer can come back (a click or a
purchase after the message moves their posterior). About one week a year."""
_WEEK_SECONDS = 7 * 86_400


def leave_alone(harm: ContactHarm, customer_id: str, at_stake: float) -> bool:
    """Whether a message clearly harms this customer more than it could gain.

    Their ratio is read on all of their buying in the week after a message, so it already nets off
    whatever messages like those add: a ratio below 1 is a message whose harm is larger than its
    gain. They are left alone when their own posterior puts at least `LEAVE_ALONE_CREDIBLE` below 1,
    read on the exact grid (`ContactHarm.p_harm`, the number `likely` and `describe` count). The
    click model's gain for the best option is not added on top: it predicts the same lift from
    clicks, and counting it twice would never leave anyone alone (measured: on day 0 it credits the
    put-off customers of `sleeping_dogs_15` with the same gain, about 65% of their buying in the
    window, as everyone else). Below the bar the bidder weighs that gain against the same record
    instead (`ContactHarm.purchase_shift`). `at_stake` is what their buying in a message's window
    is worth (urgency x organic chance x value): nobody is left alone with nothing at stake, or
    without a record."""
    return at_stake > 0 and harm.p_harm(customer_id) >= LEAVE_ALONE_CREDIBLE


def leave_alone_retry(customer_id: str, now: datetime, rate: float = LEAVE_ALONE_RETRY) -> bool:
    """A fixed coin per customer and calendar week (time is passed in): True in about `rate` of
    weeks, when someone left alone is bid for as usual."""
    week = int(now.timestamp() // _WEEK_SECONDS)
    digest = hashlib.blake2b(f"{customer_id}|{week}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2.0**64 < rate


_HARM_MODELS: weakref.WeakKeyDictionary[object, ContactHarm] = weakref.WeakKeyDictionary()


def contact_harm_of(model: object, *, create: bool = False) -> ContactHarm | None:
    """The `ContactHarm` that travels with a response model (the learner fills it; bidders read it).

    Keyed by the response model, so every bidder sharing a model shares what was learned about who
    is put off by messages, and a fresh model starts without. None when there is none yet (and
    `create` is False) or the model cannot be keyed."""
    try:
        found = _HARM_MODELS.get(model)
        if found is None and create:
            found = _HARM_MODELS[model] = ContactHarm()
    except TypeError:  # not weak-referenceable or not hashable
        return None
    return found


# --- offers: paid by everyone who buys while they are open ----------------------------------------

OFFER_PRIOR_SPREAD = 2.0
"""Before the evidence: what an offer adds to a message's uplift is centred on nothing, give or take
twice what the message does without it. Wide on purpose: for someone who wants a deal a discount can
triple what a message does, for someone who pays full price it adds nothing, and only the evidence
can tell them apart. The default to no offer comes from the centre and from the gate (an offer must
be expected to beat its plain version), not from a narrow prior that would also hide offers that work."""
OFFER_PRIOR_FLOOR = 0.005
"""The plain uplift the prior's spread is never taken below (half a point of purchase chance). Where
the plain message does nothing (or harms), a spread of 2 x nothing would rule out any offer whatever
its evidence; this keeps a well-measured discount believable there, while a thinly measured one is
still shrunk to almost nothing."""
OFFER_SENSITIVITY_PRIOR = 0.3
"""Where `CustomerState.offer_sensitivity` starts (`autonomy.data.folder.OFFER_PRIOR`): a customer
there is neither keen on offers nor indifferent to them."""
OFFER_SENSITIVITY_MAX = 2.0
"""A customer who buys only with offers can at most double what the evidence says an offer adds."""


def offer_sensitivity_factor(state: CustomerState) -> float:
    """How much more (or less) an offer moves this customer than their segment, from their own buying.

    `offer_sensitivity` is the share of their purchases made with an offer, shrunk towards
    `OFFER_SENSITIVITY_PRIOR` by the state folder. Someone who has bought ten times at full price
    (about 0.07) would have bought anyway: an offer adds a quarter of what it adds for others. Someone
    who buys only on offer (near 1) gets up to `OFFER_SENSITIVITY_MAX`. Unknown: 1."""
    s = state.offer_sensitivity
    if s is None or not math.isfinite(s):
        return 1.0
    return min(max(s, 0.0) / OFFER_SENSITIVITY_PRIOR, OFFER_SENSITIVITY_MAX)


def shrink_offer_extra(extra: float, extra_sd: float, reference: float) -> tuple[float, float]:
    """(posterior mean, posterior sd) of what an offer adds to uplift, shrunk towards nothing.

    `extra` is the offer option's uplift minus its no-offer version's, `extra_sd` the spread of the
    evidence behind that difference, `reference` the no-offer uplift. The prior is Normal(0, tau^2)
    with tau = `OFFER_PRIOR_SPREAD` x the no-offer uplift (at least `OFFER_PRIOR_FLOOR`): an offer
    must show it adds something before it is believed to. A difference measured on thousands of
    sends passes almost unchanged; one measured on a handful is mostly ignored."""
    tau = OFFER_PRIOR_SPREAD * max(reference, OFFER_PRIOR_FLOOR)
    var = max(extra_sd, 0.0) ** 2
    weight = tau * tau / (tau * tau + var)
    return extra * weight, math.sqrt(weight * var)


def organic_chance(model: ResponseModel, state: CustomerState, key: str) -> float:
    """The customer's chance of buying in the window without the message.

    A model with its own `organic(state)` answers for itself (the agent's click model); otherwise it
    is the treated rate minus the uplift (the control rate, for `BetaResponseModel`)."""
    own = getattr(model, "organic", None)
    if callable(own):
        value = own(state)
        if isinstance(value, int | float) and math.isfinite(value):
            return min(max(float(value), 0.0), 1.0)
    mean, _ = model.predict(state, key)
    return min(max(model.treated_rate(state, key) - max(mean, 0.0), 0.0), 1.0)


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


@dataclass(frozen=True)
class _Raw:
    """One option before pricing: its uplift as the response model predicts it (not yet boosted by
    a refill window, shrunk for an offer or charged for harm) and the spread used for sampling."""

    channel: Channel
    offer_id: str
    item: ContentItem
    key: str
    plain: float
    std: float


def _no_offer_references(raw: Sequence[_Raw]) -> dict[tuple[Channel, Format], _Raw]:
    """The plain version an offer has to beat: per channel and format, the no-offer option with the
    highest predicted uplift (lowest content id on a tie)."""
    out: dict[tuple[Channel, Format], _Raw] = {}
    for opt in raw:
        if opt.offer_id != NO_OFFER.offer_id:
            continue
        where = (opt.channel, opt.item.format)
        best = out.get(where)
        if best is None or (opt.plain, best.item.content_id) > (best.plain, opt.item.content_id):
            out[where] = opt
    return out


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

    `purchase_weighted` (None: on when the response model says `uplift_from_clicks`, as the agent's
    click model does) weighs the click gain against each customer's own purchases and takes the
    purchase drop off once, never adding to the gain (`_purchase_weighted_loss`; see the module
    docstring). Off, every option pays the credible dent only, as for a model whose uplift is read
    from purchases.

    `uplift` (None: whatever `autonomy.uplift.uplift_of(response_model)` finds, usually nothing) is a
    per-customer, per-action uplift scorer (`autonomy.uplift.UpliftScorer`). For an option whose key
    it scores, the response model's uplift and spread are replaced by their blend with the score
    (`autonomy.uplift.score.blend`). With `own_spread` the sampling spread keeps only the share of it
    that is the option's own (`UpliftScore.own_sd`: its treated customers' noise, not the control
    group's that all the customer's options share). An option it does not score is priced by the
    response model alone, exactly as without it.

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
        harm: ContactHarm | None = None,
        purchase_weighted: bool | None = None,
        uplift: UpliftScorer | None = None,
        offer_cost_fn: Callable[[OfferOption, CustomerState], float] | None = None,
        budget_price: Callable[[], float] | None = None,
    ) -> None:
        if prune_z is not None and prune_z < 0:
            raise ValueError("prune_z must be >= 0")
        # v3 offers (docs/v3/OFFERS.md), both off by default: `offer_cost_fn(offer, state)` is the
        # cost of one redemption (None: the offer's `cost_per_redemption`, as in v2), and
        # `budget_price()` the price of one unit of offer cost (None or 0: offers cost what they cost).
        self.offer_cost_fn = offer_cost_fn
        self.budget_price = budget_price
        self.harm = harm
        self.uplift = uplift
        self.response_model: ResponseModel = response_model or BetaResponseModel()
        self.purchase_weighted = (
            purchase_weighted
            if purchase_weighted is not None
            else bool(getattr(self.response_model, "uplift_from_clicks", False))
        )
        self.goal_id = brief.goal_id
        self.kind = kind
        self.own_spread = own_spread
        self.prune_z = prune_z
        self.brief = brief
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
        out = self._quote(state, ctx, prune=True, remember=True)
        if out:
            library = self._usable(self._content if self._content is not None else ctx.content)
            self._segments[state.customer_id] = state.segment
            self._labels.update((c.content_id, (c.theme.value, c.format)) for c in library)
        return out

    def quote(self, state: CustomerState, ctx: DecisionContext, *, prune: bool = True) -> list[Bid]:
        """What `bids()` returns, without remembering anything for learning: safe to call to explain.

        `prune=False` skips every `prune_z` step (the offer gate, the harm hold and the spread floor),
        so an explanation can list what pruning left out and say so."""
        return self._quote(state, ctx, prune=prune, remember=False)

    def _quote(self, state: CustomerState, ctx: DecisionContext, *, prune: bool, remember: bool) -> list[Bid]:
        """`quote`; with `remember`, the click reading of a purchase-weighted bid is handed to the
        harm model (`ContactHarm.note_click`) for its next refit."""
        prune_z = self.prune_z if prune else None
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

        scorer = self.uplift if self.uplift is not None else uplift_of(self.response_model)
        raw = self._raw_options(state, library, channels, choice, scorer, ctx.now)
        if not raw:
            return []
        organic = organic_chance(self.response_model, state, raw[0].key)
        harm = self.harm if self.harm is not None else contact_harm_of(self.response_model)
        # Chance of a purchase one message costs them, if they are someone contact puts off: every
        # option pays it, so it moves "do nothing" ahead of all of them alike.
        loss = harm.expected_loss(state.customer_id, organic) if harm is not None else 0.0
        references = _no_offer_references(raw)
        if harm is not None and self.purchase_weighted:
            loss = self._purchase_weighted_loss(state, harm, raw, references, organic, loss, remember)
        sensitivity = offer_sensitivity_factor(state)
        shadow = max(self.budget_price(), 0.0) if self.budget_price is not None else 0.0

        out: list[Bid] = []
        reference_net: dict[tuple[Channel, Format], float] = {}
        gates: list[tuple[Bid, tuple[Channel, Format]]] = []
        for channel in channels:
            channel_cost = self.costs.get(channel, 0.0)
            for offer in envelope.offers:
                group: list[Bid] = []
                for opt in (o for o in raw if o.channel is channel and o.offer_id == offer.offer_id):
                    item = opt.item
                    plain, std = opt.plain, opt.std
                    extra_sd = 0.0
                    # An offer in a format with no plain version is held to the plain text one.
                    ref = references.get((channel, item.format)) or references.get((channel, Format.TEXT))
                    if offer.offer_id != NO_OFFER.offer_id and ref is not None:
                        # Believe only what the evidence shows an offer adds to its plain version.
                        extra, extra_sd = shrink_offer_extra(
                            plain - ref.plain, math.hypot(std, ref.std), ref.plain
                        )
                        if extra > 0:
                            extra *= sensitivity
                        plain = ref.plain + extra
                        # Sampled against its plain version, an offer's own uncertainty is what it
                        # adds: drawing the plain part's spread again for it would let the offer win
                        # draws on noise the two share.
                        std = extra_sd
                    # The window raises what a message can do, not what a harmful one costs.
                    uplift = min(plain * boost, 1.0) if plain > 0 else plain
                    std *= boost
                    p_redeem = 0.0
                    unit_cost = (
                        offer.cost_per_redemption
                        if self.offer_cost_fn is None
                        else self.offer_cost_fn(offer, state)
                    )
                    if unit_cost:
                        # Everyone who buys while the offer is open redeems it, including those who
                        # would have bought anyway: organic chance plus what the message adds.
                        p_redeem = min(max(organic + max(plain, 0.0) - loss, 0.0), 1.0)
                        if boost > 1.0:
                            # Everyone who refills while the offer is open redeems it.
                            p_redeem = min(1.0, max(p_redeem, p14) + (uplift - plain))
                    uplift -= loss
                    offer_cost = unit_cost * p_redeem
                    cost = channel_cost + self.format_costs.get(item.format, 0.0)
                    # The budget price is charged on top of the cost, so an offer wins only when its
                    # extra profit per unit of cost is above the price (autonomy.decide.planner).
                    net = urgency * (uplift * value - offer_cost * (1.0 + shadow) - cost)
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
                    bid = Bid(
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
                    if ref is not None and opt is ref:
                        reference_net[(channel, item.format)] = net
                    elif ref is not None and offer.offer_id != NO_OFFER.offer_id:
                        gates.append((bid, (ref.channel, ref.item.format)))
                    group.append(bid)
                if not self.per_theme and group:
                    # Best by expected net, not uplift: the offer is paid by everyone who buys, so a
                    # theme that lifts more can still lose money when the offer costs more than the sale.
                    group = [max(group, key=_rank)]
                out.extend(group)
        if prune_z is not None and gates:
            # Default to no offer: an offer is bid only when, on the evidence, it is expected to beat
            # its own plain version. Its prior spread is no reason to try it: an offer that has not
            # shown what it adds is paid for by every customer who would have bought anyway.
            dropped = {
                id(bid)
                for bid, where in gates
                if where in reference_net and bid.expected_net < reference_net[where]
                # One the offer rules block cannot go out anyway: keep it, so the rule that blocks
                # it (waiting for approval, say) is what the decision reports.
                and _offer_allowed(bid, state, envelope, ctx)
            }
            out = [b for b in out if id(b) not in dropped]
        if prune_z is not None and harm is not None and out:
            # Leave alone someone a message clearly harms: their credible harm is larger than every
            # option's gain even prune_z standard deviations up, or their own posterior makes it
            # credible that messages lower their buying (`leave_alone`). A week or so a year they are
            # let back in (`LEAVE_ALONE_RETRY`), so a wrong verdict can be undone.
            harm_cost = urgency * loss * value
            z = prune_z
            clear = loss > 0 and harm_cost > max(b.expected_net + harm_cost + z * b.uncertainty for b in out)
            if (clear or leave_alone(harm, state.customer_id, urgency * organic * value)) and not (
                leave_alone_retry(state.customer_id, ctx.now)
            ):
                return []
        if prune_z is not None and len(out) > 1:
            # The floor is set by options the offer rules let through for this customer: a discount
            # they asked not to get, one waiting for approval or one the budget cannot pay must not
            # crowd out the plain message the policy could still send.
            z = prune_z
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

    def _purchase_weighted_loss(
        self,
        state: CustomerState,
        harm: ContactHarm,
        raw: Sequence[_Raw],
        references: Mapping[tuple[Channel, Format], _Raw],
        organic: float,
        dent_loss: float,
        remember: bool = False,
    ) -> float:
        """What comes off every option's uplift when the gain is read from clicks: the purchase drop
        counted once.

        The click model's gain for the message they would get (their best plain option) is weighed
        against their own purchases (`ContactHarm.purchase_shift`). The credible dent
        (`dent_loss`, from the same record and their neighbours') is a second reading of the same
        drop, so the larger of the two is taken, never their sum.

        Purchases only ever take off: a record that reads more buying than the clicks predict leaves
        the click gain as it is. The click model's scale is set against the holdout, while a record's
        ratio is flattered by when messages go out (when a refill is due, after a visit), so a high
        record is as likely that timing as a keener customer; a low one survives the flattery. And
        a mean-zero spread of small raises and cuts is not neutral: it lifts the customers just
        below the attention price over it (measured: `whatsapp_fatigue` sent 1.11 messages a week
        instead of 1.03 and lost $546 on average; docs/v2/CLICK_HARM.md). Every option moves by the
        same amount, so their order (the customer's taste, from clicks) is kept and only the level
        changes. With `remember` the click reading is noted for the harm
        model's next refit, which reads from all of them how records and click readings relate
        (`ContactHarm.click_gap`)."""
        plain = list(references.values()) or list(raw)
        best = max(plain, key=lambda o: (o.plain, o.item.content_id))
        gain_sd = self.response_model.predict(state, best.key)[1]
        if remember:
            harm.note_click(state.customer_id, organic, best.plain, gain_sd)
        shift = harm.purchase_shift(state.customer_id, organic, best.plain, gain_sd)
        return max(shift, dent_loss, 0.0)

    def _raw_options(
        self,
        state: CustomerState,
        library: Sequence[ContentItem],
        channels: Sequence[Channel],
        choice: ItemChoice | None,
        scorer: UpliftScorer | None = None,
        now: datetime | None = None,
    ) -> list[_Raw]:
        """Every allowed option with the response model's uplift and spread, in bidding order; blended
        with the uplift score where `scorer` scores the option's key."""
        out: list[_Raw] = []
        for channel in channels:
            for offer in self.brief.envelope.offers:
                for item in self._options(library, channel, offer.offer_id, choice):
                    key = action_key(channel, offer.offer_id, item.theme, item.format)
                    mean, std = self.response_model.predict(state, key)
                    found = scorer.score(state, key, now) if scorer is not None and now is not None else None
                    if found is not None:
                        mean, std = blend(mean, std, found)
                        if self.own_spread and found.own_sd is not None and found.sd > 0:
                            # Sample on the option's own noise, not the control group's that all of
                            # the customer's options share (as `_option_std` does for the model).
                            std *= min(found.own_sd / found.sd, 1.0)
                    elif self.own_spread:
                        std = _option_std(self.response_model, state, key, std)
                    out.append(_Raw(channel, offer.offer_id, item, key, min(max(mean, -1.0), 1.0), std))
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
