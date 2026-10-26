"""The agent: Autonomy's own policy, put together the way the runtime and the Gym run it.

`build_agent_policy(briefs, content)` returns an `AgentPolicy`: one `GoalBidder` per approved brief,
all sharing one `ModelBundle` (response, value and fatigue models, from `autonomy.learning`), inside
`AutonomousPolicy` (bids, hard rules, send time, attention price, Thompson sampling with a floor).
Around `decide()` it adds what a policy that runs for months needs. Pure maths, no LLM, no clock.

How a message is valued (see `ClickResponseModel`):

    uplift = lift_per_click(segment) x click_rate(segment, action) x personal(customer, action) x fatigue(week)
    net    = uplift x value - offer cost x (organic chance of buying + uplift) - channel cost

On a channel that logs no clicks (SMS, direct mail, coupons: `NO_CLICK_MIN_SENDS` past messages and
not one click) the unit is the message itself: `uplift = lift_per_send(segment) x personal x
fatigue`, read from purchases.

* **Click rates** per behaviour segment (new, lapsed, light / medium / heavy buyer, see `Segmenter`)
  and action (`channel|offer|theme`) arrive within hours and tell actions apart far sooner than
  week-long purchase windows can. Each send counts with the customer's own factor as its exposure,
  so an action sent mostly to keen clickers is not credited with their keenness.
* **Personal** factors (`PersonalEffects` in `autonomy.decide.models`) say how much more or less
  this customer clicks than their segment: overall, and for the action's channel, offer and theme,
  so learning on one discount informs the next discount on any channel. Each is shrunk towards 1 by
  as much as the business's customers really differ, re-fitted weekly from every click and non-click
  (empirical Bayes), so a customer with two messages is mostly their segment and one with thirty
  mostly themselves. Someone who never clicks after a dozen messages counts for much less.
* **Lift per click** (`LiftPerClick`) puts clicks on the scale of money: the agent group's extra
  purchases over the holdout, per click, with each customer's pre-period purchase rate as a CUPED
  covariate, per segment and shrunk towards the business-wide figure (`shrink_to_pool`). Before the
  agent has a holdout of its own, a past randomised campaign in the history sets it
  (`_calibrate`, `autonomy.learning.calibration`): what its recipients bought beyond the customers
  it left out at random, per click it drew. On real e-mail data that is a few hundredths of a
  purchase per click, not the prior's quarter (Hillstrom: 0.014), and valuing messages at the
  prior made the agent expect 10x what an e-mail made. Without such a campaign, history's clicks set
  the scale only part way (`HISTORY_CLICK_TRUST`).
* **Organic chance** (`OrganicRate`): the customer's own purchase rate, shrunk to their segment's.
  It is what makes a discount expensive for someone who would have bought anyway.
* **Value** (`ShrunkValueModel`): one order's margin, the customer's own average shrunk towards
  the business's, so a single $6 order does not make someone worthless.
* **Attention price** (`ContactPrice`), per channel: the chance this message loses the contact
  (`HazardModel`, learned from every send, per channel and messages this week, from a conservative
  prior) times the value of the messages we could no longer send on that channel (half a year of
  the customer's best weekly rhythm there), plus the chance of a complaint times its cost.

What it does around the auction:

* **History.** Sends, deliveries and clicks handed over before the first decision are the client's
  past campaigns (`autonomy.learning.observations.logged_sends`). They warm-start the click rates
  (weighted by the inverse of each send's `logged_probability`), each customer's own record and
  their reading hours, so day 1 is not blind (`_warm_start`), and a randomised one calibrates what
  a message is worth (above). The history may come in slices of customers (the runtime streams it,
  `needs_history`): messages are kept whole, purchases and consent in a compact `HistoryDigest`.
* **Send hour.** A click that comes hours after the message shows when the person reads; their
  own reading hour is used once known. A customer with quick clicks only gets the best allowed
  hour of `ReadingHourModel`'s posterior mean (their clicks credit the hours we sent at, shrunk to
  their segment's).
  Everyone else gets the allowed hour closest, on average, to when people in their segment read
  (`ReadingHours.closest_hour`), with a small share sent at random hours so every hour keeps being
  tried.
* **Pacing.** Pending sends count towards the weekly cap, and the cap is judged on the week before
  the planned send, not before the morning decision (`_contacts_at_send`). Nothing new goes out
  within `MIN_GAP` of the last message (so each one is decided knowing whether the last caused an
  unsubscribe). Options that could not beat doing nothing even at the optimistic end are not
  explored (`PLAUSIBLE_Z`), nor are those that could not be the customer's best (`PRUNE_Z`).
  With `pacing=True`, `Pacer` slows a channel, then holds it, before the brief's complaint or
  unsubscribe rule trips (off by default, see `build_agent_policy`).
* **Contacting less.** Each customer has a weekly budget (`ContactPrice.rhythm`): one message a
  week, a burst of two (then a rest until the week is clear) or two spread out, whichever is worth
  most after each message's fatigue and price; the policy holds them when one more does not fit,
  and the auction gives each slot to the best option. Where the holdout comparison has not shown
  that messages create lift in a segment, an option is sampled only if it pays at the pessimistic
  end of what is known
  (`_margin`, a lower-confidence-bound rule); the exploration floor keeps trying the rest. See
  docs/v2/CONTACT_LESS.md.
* **Briefs move.** A newer brief version rebuilds that goal's bidder (the models are shared).
* **Nightly.** `nightly(decisions, events_by_customer, now)` feeds the clicks and purchases above,
  then hands the log to `autonomy.learning.update_models` (window conversions, fatigue, value), with
  the holdout and the agent's start for the contact-harm model: people we never contact are the
  organic baseline it reads each customer's buying against (`autonomy.decide.bidders.ContactHarm`).
  Decisions do not carry the customer's segment, so the agent remembers the segment each decision
  was made in: labelling an old window with today's segment would leak its outcome.
* **The breaker.** The brief's stop rules, judged nightly on the agent's own sends of the last week
  (`Breaker`). A complaint or unsubscribe rule trips only on a count that would happen less than
  once in 10,000 weeks at the limit; a paused goal stays paused until a person approves a new brief
  version.

Products and formats (see `autonomy.decide.bidders`):

* **Products.** `item_lookup` / `recommender` (customer -> item id or (item id, reason)) feed every
  goal's bidder; without them the agent reads its own `refill_items` and `complements`, which the
  runtime fills with `set_items(...)` after enriching states (`autonomy.catalog.enrich_all` returns
  the refill mapping). A refill goal's uplift rises while the customer's refill window is open, and
  its actions carry the item (`Action.item_id`); content that names a product is offered only when
  there is one.
* **Formats.** An approved image, carousel or video variant is its own option (`channel|offer|theme|
  format`), offered only on channels that can carry it. Its segment click rate is shrunk towards the
  same words in plain text (`FORMAT_POOL`), and each person gets a format factor beside their
  channel, offer and theme factors (`PersonalEffects`, plain text as the reference), so the agent
  learns who answers images.
* **Explaining.** `explain_bids(state, ctx)` returns every bid the auction would see for the
  customer, ranked, each with the hard rule that blocks it (if any), its net after the attention
  price and whether it would be sampled. It changes nothing: deciding afterwards gives the same
  decision, so an API can show "why" without calling private methods.

Nothing here reads the Gym's truth: everything learned comes from events a real deployment sees.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from autonomy.contracts import (
    NO_OFFER,
    Bid,
    Brief,
    Channel,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    Format,
    GoalKind,
    Group,
    Objective,
    Reward,
    StopMetric,
)
from autonomy.decide import rules
from autonomy.decide.bidders import AssetOf, GoalBidder, ItemLookup, contact_harm_of
from autonomy.decide.frequency import FrequencyConfig, FrequencyPlanner, LookalikeHazard, lerp
from autonomy.decide.journeys import JourneyConfig, JourneyRunner
from autonomy.decide.lookalike import (
    LookalikeConfig,
    LookalikeResponseModel,
    Outcome,
    OutcomeBook,
    Profile,
    history_profile,
    profile_of,
    stamp,
)
from autonomy.decide.models import (
    ANY,
    Beta,
    BetaResponseModel,
    HazardModel,
    PersonalEffects,
    ResponseObservation,
    ValueModel,
    base_key,
    cuped_adjust,
    product_moments,
    shrink_to_pool,
)
from autonomy.decide.models import action_key as key_of
from autonomy.decide.pacing import BudgetPrice, OfferPricing, Pacer
from autonomy.decide.planner import basket_cost_fn
from autonomy.decide.policy import SWEEP, AutonomousPolicy, seeded_rng
from autonomy.decide.timing import LATE_CLICK, ReadingHourModel, zone_named
from autonomy.decide.who import WhoBook, WhoConfig
from autonomy.interfaces import Bidder, Policy
from autonomy.learning import ModelBundle, build_bundle, learn_from_rewards, update_models
from autonomy.learning.calibration import (
    MIN_ARM,
    HistoryDigest,
    combine,
    difference,
    randomised_arms,
    with_prior,
)
from autonomy.learning.observations import (
    LoggedSend,
    Timeline,
    ipw_weights,
    logged_sends,
    timelines,
    weighted_rate,
)
from autonomy.runtime.v3 import V3Preset, resolve_v3
from autonomy.uplift import UpliftWarmStart, warm_start_uplift

if TYPE_CHECKING:
    from autonomy.decide.moments import MomentBook
    from autonomy.experiments.book import ExperimentBook
    from autonomy.experiments.policy import ExperimentPolicy

AGENT_VERSION = "autonomy-agent-v1"

PolicyChooser = Callable[[CustomerState, DecisionContext, Decision], Decision]
"""The hook `AgentPolicy.use_chooser` takes: `(state, ctx, the agent's own decision) -> the decision to
use`. Called for agent-group customers only, after the agent has decided. `autonomy.scientist.chooser`
is the one implementation (Shadow first: it logs what the selected policy would do and returns the
agent's decision unchanged)."""

DEFAULT_LEARN_HORIZON_DAYS = 7
"""Days after a send in which a purchase counts as its outcome, for learning. A week, not the
brief's two-week proof window: the agent contacts a customer up to twice a week, and a two-week
window would credit each message with the next three messages' effect too."""

NEW_CUSTOMER_DAYS = 120
"""A customer first seen this recently is "new" (their first four months): no buying rhythm to
read yet, and they answer messages differently from long-standing customers."""
LAPSED_DAYS = 90
"""No purchase for this long (or none ever, after the new period) makes a customer "lapsed"."""
MIN_OBSERVED_DAYS = 30.0
"""Purchase frequency is never computed over less than a month of history."""
DEFAULT_FREQUENCY_CUTS = (0.15, 0.35)
"""Purchases per 30 days that split light / medium / heavy buyers before the business's own
terciles are known."""

MIN_HOUR_SIGNAL = 1.0
"""Late clicks a customer needs before their own reading hour is used."""

BREAKER_WINDOW = timedelta(days=7)
BREAKER_Z = 3.09
"""One-sided 99.9%, for the delivery-rate rule (a Wilson bound on deliveries, which are many)."""
BREAKER_ALPHA = 1e-4
"""The complaint and unsubscribe rules trip only when the week's count would happen less than once
in 10,000 weeks at the limit (an exact Poisson tail). They are checked every night on a rolling
week, about 90 overlapping looks a quarter, and on a handful of events a normal-style bound is far
too eager: at 99.9% (Wilson) a goal whose true complaint rate was 0.06% against a 0.08% limit was
paused on 5 complaints in 1,146 messages, a once-in-400-weeks count at the limit. A real storm
(complaints at ten times the limit) still trips on its first batch."""
BREAKER_MIN_EVENTS = 5
"""At least this many complaints (or unsubscribes, or failures) before a rule can trip."""
BASELINE_SENDS = 200
BASELINE_DAYS = 14
"""The goal's own unsubscribe baseline is measured on its first two weeks (and at least 200
deliveries) before the "unsubscribes N times the usual" rule can trip: first messages unsubscribe
people less than the second message of a week does, so a shorter baseline would call the agent's
normal rhythm a storm."""

MIN_GAP = timedelta(hours=6)
"""No new message within six hours of the last one (or while one is scheduled): an unsubscribe
arrives a few hours after the message that caused it, so each message is decided knowing how the
previous one landed."""
PLAUSIBLE_Z = 2.0
"""Options that could not beat doing nothing even two standard deviations up are not explored."""
PRUNE_Z = 2.0
"""Options that could not be this customer's best even two standard deviations up (against the best
option two down) are not offered at all, so the exploration floor is spent only on contenders."""

RESTRAINT_Z = 1.64
"""One-sided 95%. The evidence that messages create lift in a segment is weak when the lift per
click's posterior reaches zero this many standard deviations below its mean; there, an option is
sampled only if its net beats doing nothing by this many of its own standard deviations."""
LIFT_EVIDENCE_CLICKS = 20.0
"""Matured clicks the holdout comparison needs before its verdict can trigger restraint (the lift
prior is worth as many)."""
LIFT_JUDGE_WINDOWS = 2
"""The comparison is judged only after this many learning windows (two weeks) since the agent
started: a launch test runs for a set period before anyone reads it, because a comparison read
every night from day one will look "unproven" in its first noisy week in every shop, and holding
back then would starve the very test that settles it."""

HISTORY_LOSS_WINDOW = timedelta(days=2)
"""In the history, an unsubscribe or complaint without a decision id is put down to the latest
delivered send on its channel at most this long before it (they come within hours of a message)."""

_EVENT_MEMORY_EXTRA = timedelta(days=9)
"""Events are kept for the learning window plus this: the week of sends before a send (fatigue)
plus a day for sends timed after their decision."""


_HISTORY_KEEP = frozenset(
    {
        EventType.SEND,
        EventType.DELIVER,
        EventType.CLICK,
        EventType.FAIL,
        EventType.BOUNCE,
        EventType.BLOCK,
        EventType.UNSUBSCRIBE,
        EventType.COMPLAINT,
    }
)
"""History events kept whole until the warm start (past campaigns' messages and what followed them);
purchases, consent and first sightings go to the compact `HistoryDigest` instead."""
MIN_SEGMENT_ARM = 50
"""Customers a segment needs on each side of a past campaign before its own difference is measured."""


def _poisson_tail(hits: float, expected: float) -> float:
    """P(X >= hits) for X ~ Poisson(expected): how surprising `hits` events are at the limit.

    Each term of P(X < hits) is computed in log space: a running product from exp(-expected)
    underflows to zero once the expected count passes about 745 (a big sender's weekly
    complaints), which would make every count look unsurprising and the breaker never trip."""
    k = math.ceil(hits)
    if k <= 0:
        return 1.0
    if expected <= 0:
        return 0.0
    log_mu = math.log(expected)
    below = math.fsum(math.exp(i * log_mu - expected - math.lgamma(i + 1)) for i in range(k))
    return min(max(0.0, 1.0 - below), 1.0)


def _wilson_high(hits: float, n: float, z: float = BREAKER_Z) -> float:
    if n <= 0:
        return 1.0
    p = min(max(hits / n, 0.0), 1.0)
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + half) / denom)


# --- segments -----------------------------------------------------------------------------------


@dataclass
class Segmenter:
    """Plain behaviour segments from the customer's state: new, lapsed, light, medium, heavy.

    Frequency is purchases per 30 days over the history we can see: from the customer's first event
    or, when it is later, the start of the business's data (`data_start`; an import usually covers
    a fixed period, and counting a year-old signup's purchases over a year when only six months of
    orders were imported would make everyone look light). `fit(states, now)` sets the light/medium
    and medium/heavy cuts at the business's own terciles of frequency among active buyers.
    """

    data_start: datetime | None = None
    cuts: tuple[float, float] = DEFAULT_FREQUENCY_CUTS
    fitted: bool = False

    def frequency(self, state: CustomerState, now: datetime) -> float:
        start = state.first_seen_at
        if self.data_start is not None and (start is None or start < self.data_start):
            start = self.data_start
        days = (now - start).total_seconds() / 86_400.0 if start is not None else MIN_OBSERVED_DAYS
        return state.purchases * 30.0 / max(days, MIN_OBSERVED_DAYS)

    def label(self, state: CustomerState, now: datetime) -> str:
        if state.first_seen_at is not None and now - state.first_seen_at <= timedelta(days=NEW_CUSTOMER_DAYS):
            return "new"
        last = state.last_purchase_at
        if state.purchases <= 0 or last is None or now - last > timedelta(days=LAPSED_DAYS):
            return "lapsed"
        f = self.frequency(state, now)
        if f >= self.cuts[1]:
            return "heavy"
        if f >= self.cuts[0]:
            return "medium"
        return "light"

    def fit(self, states: Sequence[CustomerState], now: datetime) -> None:
        """Set the frequency cuts at the terciles of the active buyers; keep the defaults if too few."""
        active = sorted(
            self.frequency(s, now)
            for s in states
            if s.purchases > 0
            and s.last_purchase_at is not None
            and now - s.last_purchase_at <= timedelta(days=LAPSED_DAYS)
            and not (
                s.first_seen_at is not None and now - s.first_seen_at <= timedelta(days=NEW_CUSTOMER_DAYS)
            )
        )
        self.fitted = True
        if len(active) < 30:
            return
        low, high = active[len(active) // 3], active[(2 * len(active)) // 3]
        if high > low:
            self.cuts = (low, high)


# --- reading hours ------------------------------------------------------------------------------


_HOUR_KERNEL = (0.25, 0.5, 1.0, 0.5, 0.25)
_CIRCULAR = np.array(
    [[min(abs(a - b), 24 - abs(a - b)) for b in range(24)] for a in range(24)], dtype=np.float64
)
"""Hours apart on the clock, the short way round (0 to 12)."""

READING_PRIOR_STRENGTH = 20.0
"""How many customers' worth of reading hours the business's profile is worth for one segment."""


def _smooth(counts: Sequence[float]) -> list[float]:
    return [
        sum(w * counts[(h + d) % 24] for d, w in zip(range(-2, 3), _HOUR_KERNEL, strict=True))
        for h in range(24)
    ]


@dataclass
class ReadingHours:
    """When people read their messages, learned from clicks that come well after the send.

    A click hours after the message shows when that person reads. Once a customer has one, their own
    reading hour is used. Everyone else is sent at the hour that is closest, on average, to when
    people like them read: the circular median of their segment's reading hours (shrunk towards the
    business's). If closeness to the reading hour is what makes a message land, as any falling
    curve says, that hour loses the least, whatever the curve's steepness; so it needs no assumption
    about how much the hour matters. Each customer counts once in their segment's profile (their
    own share of late clicks), so a keen clicker does not set everyone's hour.
    """

    per_customer: dict[str, list[float]] = field(default_factory=dict)
    segment_of: dict[str, str] = field(default_factory=dict)
    _profiles: dict[str, list[float]] = field(default_factory=dict)
    _best: dict[tuple[str, tuple[int, ...]], int] = field(default_factory=dict)

    def observe(self, customer_id: str, local_hour: int, segment: str | None = None) -> None:
        self.per_customer.setdefault(customer_id, [0.0] * 24)[local_hour] += 1.0
        if segment is not None:
            self.segment_of[customer_id] = segment
        self._profiles.clear()
        self._best.clear()

    def profile(self, customer_id: str) -> tuple[float, ...] | None:
        """The customer's reading hours, spread a little to the neighbouring hours (someone who
        reads at 22:00 is better caught at 20:00 than at 10:00 when 21-9 is quiet)."""
        mine = self.per_customer.get(customer_id)
        if mine is None or sum(mine) < MIN_HOUR_SIGNAL:
            return None
        smooth = _smooth(mine)
        total = sum(smooth)
        return tuple(x / total for x in smooth)

    def known(self) -> int:
        """Customers whose own reading hour is known."""
        return sum(1 for v in self.per_customer.values() if sum(v) >= MIN_HOUR_SIGNAL)

    def segment_profile(self, segment: str | None) -> list[float] | None:
        """Where a segment's customers read (each known customer counts once), shrunk towards the
        business's profile; None before anyone's hour is known."""
        key = segment or ANY
        found = self._profiles.get(key)
        if found is not None:
            return found
        business = [0.0] * 24
        own = [0.0] * 24
        for cid, counts in self.per_customer.items():
            total = sum(counts)
            if total < MIN_HOUR_SIGNAL:
                continue
            share = [c / total for c in counts]
            business = [b + s for b, s in zip(business, share, strict=True)]
            if segment is not None and self.segment_of.get(cid) == segment:
                own = [o + s for o, s in zip(own, share, strict=True)]
        n_business = sum(business)
        if n_business <= 0:
            return None
        base = [b / n_business for b in business]
        mixed = [READING_PRIOR_STRENGTH * b + o for b, o in zip(base, own, strict=True)]
        total = sum(mixed)
        profile = [m / total for m in _smooth(mixed)]
        self._profiles[key] = profile
        return profile

    def closest_hour(self, segment: str | None, allowed: Sequence[int]) -> int | None:
        """The allowed hour with the least expected distance to the segment's reading hours."""
        key = (segment or ANY, tuple(allowed))
        if key in self._best:
            return self._best[key]
        profile = self.segment_profile(segment)
        if profile is None or not allowed:
            return None
        cost = _CIRCULAR[list(allowed)] @ np.asarray(profile, dtype=np.float64)
        best = int(allowed[int(np.argmin(cost))])
        self._best[key] = best
        return best


# --- uplift from purchases, told apart by clicks -------------------------------------------------

CLICK_PRIOR = Beta.from_rate(0.05, 20.0)
"""Before any send: about 5% of messages get a click, worth 20 sends."""
CLICK_POOL = 20.0
"""How many sends' worth the business-wide click rate of an action is worth for one segment."""
FORMAT_POOL = 20.0
"""How many sends' worth the plain-text click rate is for the same message with an image, carousel
or video: a new format starts where its words are and moves as its own clicks come in."""
LIFT_PER_CLICK_PRIOR = (0.25, 0.25)
"""Before purchases say otherwise: a click goes with a quarter of an extra purchase, give or take a
quarter. Weak on purpose: the agent-vs-holdout comparison takes over within weeks."""
LIFT_PER_CLICK_STRENGTH = 20.0
"""The prior above is worth this many clicks."""
LIFT_PER_SEND_PRIOR = (
    LIFT_PER_CLICK_PRIOR[0] * CLICK_PRIOR.mean,
    LIFT_PER_CLICK_PRIOR[1] * CLICK_PRIOR.mean,
)
"""On a channel that logs no clicks (SMS, direct mail, coupons), extra purchases per delivered
message before purchases say otherwise: what the two priors above imply for a message whose clicks
we cannot see (a quarter purchase per click x 5% clicks = 1.25%, give or take as much)."""
NO_CLICK_MIN_SENDS = 200
"""Delivered past messages a channel needs, all without a click, before it is taken to log no clicks:
its value is then read from purchases (lift per message), not from clicks it can never show."""
HISTORY_CLICK_TRUST = 0.5
"""Without a randomised control in the history, how far the history's click rate is trusted to set
the scale of uplift: the lift per click is pulled towards what keeps the prior's uplift per message,
by `(prior click rate / history click rate) ** (1 - trust)` when clicks ran above the prior (a click
is often a visit by someone who would have bought anyway; only a control shows how often). 0.5 meets
halfway on the log scale; the agent's own holdout then takes over."""
ORGANIC_PRIOR_DAYS = 90.0
"""A customer's own purchase rate is shrunk towards their segment's, worth this many days."""
PERSONAL_REFIT_DAYS = 7
"""How often the prior strengths of the personal factors are re-fitted to the business's clicks."""


HOUR_STRENGTH = 10.0
"""Expected clicks an hour's own record needs before its factor moves halfway from the pool's."""
HOUR_EXPLORE = 0.05
"""Share of messages to customers without a known reading hour sent at a random allowed hour, so
every hour keeps being tried and late clicks keep showing people's own hours."""
FATIGUE_STRENGTH = 5.0
"""How many expected clicks the "messages this week" factor needs before it moves halfway."""


def _with_pool(segment: str | None) -> tuple[str, ...]:
    """The keys an observation counts under: the business-wide pool, and the segment if known."""
    return (ANY,) if segment is None or segment == ANY else (ANY, segment)


def _channel_value(action_key: str) -> str:
    """The channel part of an action key (`channel|offer|theme[|format]`)."""
    return action_key.split("|", 1)[0]


def _bucket(contacts_7d: int) -> int:
    return min(max(contacts_7d, 0), 2)


def _gamma_ratio(hits: float, expected: float, strength: float) -> tuple[float, float]:
    """(mean, variance) of a Poisson rate ratio with a Gamma(strength, strength) prior centred on 1."""
    a, b = strength + hits, strength + expected
    return a / b, a / (b * b)


class ClickResponseModel(BetaResponseModel):
    """Uplift per customer and action, learned from clicks and calibrated on purchases.

    A purchase says whether a message worked, but it is a noisy yes/no that shows up days later, and
    split over every segment x action it leaves a few hundred outcomes per cell: far too few to tell
    a 3-point uplift from a 4-point one. Clicks arrive within hours and follow the same thing: the
    people a message moves are the people who click. So:

        uplift(customer, action) = lift_per_click(segment) x click_rate(segment, action) x personal(customer, action)

    * `click_rate` is a Beta posterior per (segment, action), shrunk towards the business-wide rate of
      that action (worth `CLICK_POOL` sends), itself shrunk towards `CLICK_PRIOR`. For an image,
      carousel or video option the business-wide rate is shrunk towards the same words' plain-text
      rate instead (worth `FORMAT_POOL` sends): format, segment, then person, each shrunk to the
      level above.
    * `personal` is how much more (or less) this customer clicks than their segment predicts, for
      this action's channel, offer and theme (`PersonalEffects`): their overall responsiveness times
      their taste for each feature, every factor shrunk towards 1 by as much as the business's
      customers really differ (fitted from their clicks). Someone who never clicks after a dozen
      messages counts for much less (sleeping dogs, the uninterested); someone who clicks on every
      discount is offered one, on any channel.
    * `lift_per_click` (set by the agent from `LiftPerClick`) is extra purchases per click, measured by
      comparing the agent group's purchases with the holdout's, per segment and shrunk towards the
      business-wide figure. It keeps the scale honest: if messages draw clicks but no extra purchases
      (the null-effect shop) it goes to zero, and so does every bid.

    `treated_rate` (what pays for an offer: everyone who buys while it is open redeems it) is the
    customer's own organic chance of buying in the window (from `organic`, set by the agent) plus the
    uplift. Window conversions still come in through `update()` as for `BetaResponseModel`, so the
    nightly learner keeps its control posteriors, but the bids do not read them.

    **Purchases weigh in per person** (`uplift_from_clicks`). A put-off customer still clicks, so
    uplift read from clicks credits them with a message's full gain while their buying drops.
    Bidders that see `uplift_from_clicks` weigh each customer's click gain against their own
    purchases inside and outside message weeks (`autonomy.decide.bidders.ContactHarm.purchase_shift`),
    by how well each is measured: see docs/v2/CLICK_HARM.md. Off by default: the full CI Gym
    (docs/v2/GYM_CI_RESULTS.md) showed no gain beyond seed noise and no sleeping dog spared.
    """

    uplift_from_clicks = False
    """Read by `GoalBidder`: this model's uplift is implied by clicks, so its bids are weighed
    against each customer's purchases."""

    def __init__(
        self,
        priors: Mapping[str, tuple[float, float]] | None = None,
        *,
        click_prior: Beta = CLICK_PRIOR,
        click_pool: float = CLICK_POOL,
        lift_per_click: tuple[float, float] = LIFT_PER_CLICK_PRIOR,
        people: PersonalEffects | None = None,
    ) -> None:
        super().__init__(priors)
        self.click_prior = click_prior
        self.click_pool = click_pool
        self.lift: tuple[float, float] = lift_per_click
        self.lift_by_segment: dict[str, tuple[float, float]] = {}
        """Extra purchases per click per segment (set by the agent); `lift` for a segment not in it."""
        self.organic: Callable[[CustomerState], float] = lambda _s: 0.0
        self.people = people if people is not None else PersonalEffects()
        self._clicks: dict[tuple[str, str], list[float]] = {}  # (segment or ANY, key) -> [clicks, sends]
        self._rates: dict[tuple[str, str], Beta] = {}
        self._fatigue: dict[int, list[float]] = {}  # contacts this week (capped) -> [clicks, expected]
        self._hours: dict[tuple[str, int], list[float]] = {}  # (segment or ANY, local send hour) -> same
        self.no_click: set[str] = set()
        """Channels (their values) that log no clicks: their uplift is lift per message, not per click."""
        self.send_lift: tuple[float, float] = LIFT_PER_SEND_PRIOR
        """Extra purchases per delivered message on a channel that logs no clicks (set by the agent)."""
        self.send_lift_by_segment: dict[str, tuple[float, float]] = {}
        self.action_scale: dict[str, tuple[float, float]] = {}
        """Action key -> (mean, sd) of how much more or less it lifts per unit than the pool, as a
        past randomised campaign showed (1 for an action never measured)."""

    # -- learning from clicks --

    def expected_click(self, segment: str | None, action_key: str) -> float:
        return self.click_rate(segment or ANY, action_key).mean

    def observe_send(
        self,
        customer_id: str,
        segment: str | None,
        action_key: str,
        offer_id: str = ANY,
        contacts_7d: int = 0,
        local_hour: int | None = None,
    ) -> None:
        """A delivered message: one more send for the action, and its expected clicks for the person.

        `contacts_7d` is how many messages the customer had in the week before this one and
        `local_hour` the hour on their clock it was sent at. `offer_id` is kept for callers of the
        earlier signature; the offer is read from the action key."""
        del offer_id
        expected = self.expected_click(segment, action_key)
        # The send's exposure is the customer's own factor for it, not 1: the agent sends an action
        # most to the people it expects to answer it, and counting their clicks against plain sends
        # would credit the action with their keenness (a Poisson offset, as in a GLM with person
        # effects). The segment rate is then the rate of an average customer of the segment.
        exposure = min(max(self.people.factor(customer_id, action_key)[0], 0.1), 10.0)
        self._fatigue.setdefault(_bucket(contacts_7d), [0.0, 0.0])[1] += expected * exposure
        expected *= self.fatigue_factor(contacts_7d)[0]
        if local_hour is not None:
            for seg in _with_pool(segment):
                self._hours.setdefault((seg, local_hour), [0.0, 0.0])[1] += expected * exposure
        for seg in _with_pool(segment):
            self._clicks.setdefault((seg, action_key), [0.0, 0.0])[1] += exposure
        self.people.observe(customer_id, action_key, expected=expected)
        self._rates.clear()

    def observe_click(
        self,
        customer_id: str,
        segment: str | None,
        action_key: str,
        offer_id: str = ANY,
        contacts_7d: int = 0,
        local_hour: int | None = None,
    ) -> None:
        del offer_id
        self.no_click.discard(_channel_value(action_key))
        self._fatigue.setdefault(_bucket(contacts_7d), [0.0, 0.0])[0] += 1.0
        if local_hour is not None:
            for seg in _with_pool(segment):
                self._hours.setdefault((seg, local_hour), [0.0, 0.0])[0] += 1.0
        for seg in _with_pool(segment):
            self._clicks.setdefault((seg, action_key), [0.0, 0.0])[0] += 1.0
        self.people.observe(customer_id, action_key, clicks=1.0)
        self._rates.clear()

    def warm_start(self, sends: Sequence[tuple[str, str | None, str, bool, float]]) -> int:
        """Learn from logged history before the first decision: (customer, segment, action key,
        clicked, weight) per delivered past send. Returns how many sends were used.

        The segment click rates take each action's inverse-propensity-weighted click rate, worth its
        effective sample size: the past campaigns targeted some people more than others, and the
        weights undo that so the rate is the segment's, not the targeted few's. Each customer's own
        record takes their sends unweighted (their response to what they got is what it is), with the
        segment rate as the expected clicks, as for the agent's own sends. Sends on a channel that
        logs no clicks (`no_click`) are left out: they say nothing about clicks.
        """
        sends = [r for r in sends if self.counts_clicks(r[2])]
        cells: dict[tuple[str, str], tuple[list[bool], list[float]]] = {}
        for _cid, segment, key, clicked, weight in sends:
            for seg in _with_pool(segment):
                ys, ws = cells.setdefault((seg, key), ([], []))
                ys.append(clicked)
                ws.append(weight)
        for cell, (ys, ws) in sorted(cells.items()):
            rate, n_eff = weighted_rate(ys, ws)
            counts = self._clicks.setdefault(cell, [0.0, 0.0])
            counts[0] += rate * n_eff
            counts[1] += n_eff
        self._rates.clear()
        for cid, segment, key, clicked, _weight in sends:
            self.people.observe(
                cid, key, expected=self.expected_click(segment, key), clicks=1.0 if clicked else 0.0
            )
        return len(sends)

    def hour_factor(self, segment: str | None, local_hour: int) -> float:
        """How messages sent at this hour of the customer's clock draw clicks compared with the
        average message: the segment's record shrunk towards the business's, which is shrunk
        towards 1 (`HOUR_STRENGTH` expected clicks each)."""
        total = self._hours.get((ANY, local_hour), [0.0, 0.0])
        pooled, _ = _gamma_ratio(total[0], total[1], HOUR_STRENGTH)
        if segment is None or segment == ANY:
            return pooled
        own = self._hours.get((segment, local_hour), [0.0, 0.0])
        return (HOUR_STRENGTH * pooled + own[0]) / (HOUR_STRENGTH + own[1])

    def fatigue_factor(self, contacts_7d: int) -> tuple[float, float]:
        """(mean, variance) of how a message's click rate compares with the average message's, given
        how many messages the customer already had this week (a second message in a week tends to
        do less than the first)."""
        found = self._fatigue.get(_bucket(contacts_7d), [0.0, 0.0])
        return _gamma_ratio(found[0], found[1], FATIGUE_STRENGTH)

    def forget(self, customer_id: str) -> None:
        self.people.forget(customer_id)

    # -- reading --

    def click_rate(self, segment: str, action_key: str) -> Beta:
        found = self._rates.get((segment, action_key))
        if found is not None:
            return found
        total = self._clicks.get((ANY, action_key), [0.0, 0.0])
        plain = base_key(action_key)
        prior = (
            self.click_prior
            if plain == action_key
            else Beta.from_rate(self.click_rate(ANY, plain).mean, FORMAT_POOL)
        )
        pooled = prior.plus(total[0], max(total[1] - total[0], 0.0))
        if segment == ANY:
            rate = pooled
        else:
            own = self._clicks.get((segment, action_key), [0.0, 0.0])
            rate = Beta.from_rate(pooled.mean, self.click_pool).plus(own[0], max(own[1] - own[0], 0.0))
        self._rates[(segment, action_key)] = rate
        return rate

    def personal(self, customer_id: str, action_key: str) -> tuple[float, float]:
        """(mean, variance) of how this customer's clicks on this action compare with their segment's."""
        return self.people.factor(customer_id, action_key)

    def lift_for(self, segment: str | None) -> tuple[float, float]:
        return self.lift_by_segment.get(segment or ANY, self.lift)

    def counts_clicks(self, action_key: str) -> bool:
        """Whether this action's channel logs clicks (else its uplift is per message)."""
        return _channel_value(action_key) not in self.no_click

    def _unit(self, segment: str | None, action_key: str) -> tuple[float, float, float, float]:
        """(lift per unit, its sd, units per message, their variance) for this action: per click on a
        channel with clicks, per message (one unit, known exactly) on one without."""
        r, r_sd = self.action_scale.get(action_key, (1.0, 0.0))
        if self.counts_clicks(action_key):
            k, k_sd = self.lift_for(segment)
            rate = self.click_rate(segment or ANY, action_key)
            base, base_var = rate.mean, rate.var
        else:
            k, k_sd = self.send_lift_by_segment.get(segment or ANY, self.send_lift)
            base, base_var = 1.0, 0.0
        return k * r, math.hypot(k_sd * r, k * r_sd), base, base_var

    def _parts(self, state: CustomerState, action_key: str) -> tuple[Beta, float, float, float]:
        """(click rate, personal x fatigue mean, its variance, the variance of the taste part alone)."""
        rate = self.click_rate(state.segment or ANY, action_key)
        (rho, rho_var), (taste, taste_var) = self.people.parts(state.customer_id, action_key)
        f, f_var = product_moments(
            ((rho, rho_var), (taste, taste_var), self.fatigue_factor(state.contacts_7d))
        )
        return rate, f, f_var, rho * rho * taste_var

    def predict(self, state: CustomerState, action_key: str) -> tuple[float, float]:
        _rate, f, f_var, _taste_var = self._parts(state, action_key)
        k, k_sd, base, base_var = self._unit(state.segment, action_key)
        mean = k * base * f
        var = (k_sd * base * f) ** 2 + k * k * (base_var * f * f + base**2 * f_var)
        return mean, math.sqrt(var)

    def option_std(self, state: CustomerState, action_key: str) -> float:
        """The spread used to sample between this customer's options: the segment click rate's only.

        `lift_per_click` and the customer's overall responsiveness are shared by every option they
        have; drawing either independently per option would shuffle the options without teaching
        anything. Their taste for one channel, offer or theme does tell options apart, but a person
        gets a few dozen messages and clicks on a handful, so trying an option on them to learn their
        own taste almost never pays for itself: their taste moves the means, and is learned from
        whatever they are sent. Exploration happens where the data is shared: the segment's click
        rate per action.
        """
        _rate, f, _f_var, _taste_var = self._parts(state, action_key)
        k, k_sd, _base, base_var = self._unit(state.segment, action_key)
        if not self.counts_clicks(action_key):
            # No click rate to explore: what is being learned per action is its lift per message.
            return k_sd * f
        return k * math.sqrt(base_var) * f

    def treated_rate(self, state: CustomerState, action_key: str) -> float:
        return min(1.0, max(self.organic(state), 0.0) + max(self.predict(state, action_key)[0], 0.0))


class _ClickCrowd:
    """A `ClickResponseModel` as the look-alike model's prior: its own estimate, with nothing blended in."""

    def __init__(self, owner: ClickResponseModel) -> None:
        self.owner = owner

    def predict(self, state: CustomerState, action_key: str) -> tuple[float, float]:
        return ClickResponseModel.predict(self.owner, state, action_key)

    def treated_rate(self, state: CustomerState, action_key: str) -> float:
        mean = ClickResponseModel.predict(self.owner, state, action_key)[0]
        return min(1.0, max(self.owner.organic(state), 0.0) + max(mean, 0.0))

    def update(self, observations: Iterable[ResponseObservation]) -> None:
        ClickResponseModel.update(self.owner, observations)

    def option_std(self, state: CustomerState, action_key: str) -> float:
        return ClickResponseModel.option_std(self.owner, state, action_key)


class LookalikeClickModel(ClickResponseModel):
    """The click model with the look-alike model on top (opt-in: `build_agent_policy(lookalike=True)`).

    Everything the click model learns (click rates, personal factors, lift per click, fatigue) is
    unchanged and is the prior. `lookalike` learns purchase given profile and action from the
    agent's own closed windows and the holdout's (the agent feeds it, see `AgentPolicy.nightly`), and
    where it has enough data its uplift is combined with the click model's by precision. With too
    little data the answer is the click model's, exactly. See docs/v3/LOOKALIKE.md.
    """

    def __init__(
        self,
        priors: Mapping[str, tuple[float, float]] | None = None,
        *,
        click_prior: Beta = CLICK_PRIOR,
        click_pool: float = CLICK_POOL,
        lift_per_click: tuple[float, float] = LIFT_PER_CLICK_PRIOR,
        people: PersonalEffects | None = None,
        config: LookalikeConfig | None = None,
    ) -> None:
        super().__init__(
            priors,
            click_prior=click_prior,
            click_pool=click_pool,
            lift_per_click=lift_per_click,
            people=people,
        )
        self.lookalike = LookalikeResponseModel(_ClickCrowd(self), config=config)

    def predict(self, state: CustomerState, action_key: str) -> tuple[float, float]:
        return self.lookalike.predict(state, action_key)

    def option_std(self, state: CustomerState, action_key: str) -> float:
        return self.lookalike.option_std(state, action_key)


CLICK = "click"
SEND = "send"
"""The two response units the lift is measured per: a click, or (on a channel that logs no clicks,
such as SMS) a delivered message."""


@dataclass
class LiftPerClick:
    """Extra purchases per click: the agent group's purchases minus the holdout's, per click.

    Both groups are random slices of the same customers, so the difference in purchases per customer
    since the agent started is what the agent's messages caused, net of everything they cost in
    purchases (a put-off customer buys less). Dividing by the agent group's clicks turns it into the
    scale of `ClickResponseModel`. A click only counts in full once a week has passed since its
    message (before that, part of the purchases it leads to have not happened yet). The estimate is
    shrunk towards `prior`: `LIFT_PER_CLICK_PRIOR`, or what a past randomised campaign measured
    (`AgentPolicy._calibrate`).

    Two things make it much less noisy on a small holdout:

    * **CUPED.** Each customer's purchase rate before the agent started (`member(pre_rate=...)`) is
      used as a covariate: purchases since the start minus theta x (pre-period rate - its mean), with
      theta fitted on both groups together, so the difference keeps its mean and loses the part of
      its variance that is just "some people buy more than others".
    * **Per segment, shrunk.** `by_segment` measures the same ratio inside each segment (the one a
      customer had when they joined) and pulls each towards the business-wide figure by as much as
      the segments' spread beyond their own noise allows (`shrink_to_pool`). A segment the agent
      has not measured yet keeps the ratio to the business figure a past campaign showed
      (`history_ratio`).

    **Two units.** On a channel that logs no clicks the unit is a delivered message (`kind=SEND`,
    prior `send_prior`): "extra purchases per message". When the agent uses both kinds of channel,
    each kind is credited with its `share` of the extra purchases (the agent passes each kind's share
    of the uplift its sends were expected to bring), so one difference is not counted twice.
    """

    prior: tuple[float, float] = LIFT_PER_CLICK_PRIOR
    strength: float = LIFT_PER_CLICK_STRENGTH
    effect_days: float = float(DEFAULT_LEARN_HORIZON_DAYS)
    start: datetime | None = None
    group: dict[str, Group] = field(default_factory=dict)
    purchases: dict[str, int] = field(default_factory=dict)
    click_times: list[datetime] = field(default_factory=list)
    pre_rate: dict[str, float] = field(default_factory=dict)
    """Customer -> purchases per day before the agent started (the CUPED covariate)."""
    segment: dict[str, str] = field(default_factory=dict)
    """Customer -> their segment when they joined."""
    clicks_by: dict[str, list[datetime]] = field(default_factory=dict)
    """Segment -> send times of the agent group's clicked messages."""
    send_prior: tuple[float, float] = (0.0, 0.0)
    """Prior of extra purchases per delivered message on channels without clicks (set by the agent)."""
    send_times: list[datetime] = field(default_factory=list)
    sends_by: dict[str, list[datetime]] = field(default_factory=dict)
    history_ratio: dict[str, dict[str, float]] = field(default_factory=dict)
    """Kind -> segment -> that segment's lift over the business-wide lift, as a past campaign showed."""

    def member(
        self,
        customer_id: str,
        group: Group,
        at: datetime,
        *,
        pre_rate: float | None = None,
        segment: str | None = None,
    ) -> None:
        if group is Group.BASELINE or customer_id in self.group:
            return
        self.group[customer_id] = group
        if pre_rate is not None and math.isfinite(pre_rate):
            self.pre_rate[customer_id] = pre_rate
        if segment is not None:
            self.segment[customer_id] = segment
        if self.start is None or at < self.start:
            self.start = at

    def purchase(self, customer_id: str, at: datetime) -> None:
        if self.start is not None and at >= self.start and customer_id in self.group:
            self.purchases[customer_id] = self.purchases.get(customer_id, 0) + 1

    def click(self, sent_at: datetime, customer_id: str | None = None, *, kind: str = CLICK) -> None:
        """One response unit: a clicked message (`CLICK`), or a delivered one without clicks (`SEND`)."""
        times, by = self._units(kind)
        times.append(sent_at)
        if customer_id is not None:
            seg = self.segment.get(customer_id)
            if seg is not None:
                by.setdefault(seg, []).append(sent_at)

    def _units(self, kind: str) -> tuple[list[datetime], dict[str, list[datetime]]]:
        return (self.send_times, self.sends_by) if kind == SEND else (self.click_times, self.clicks_by)

    def _prior(self, kind: str) -> tuple[float, float]:
        return self.send_prior if kind == SEND else self.prior

    def _matured(self, times: Iterable[datetime], now: datetime) -> float:
        return sum(min(1.0, max((now - t).total_seconds(), 0.0) / 86_400.0 / self.effect_days) for t in times)

    def _extra(self, members: Sequence[str], share: float = 1.0) -> tuple[float, float] | None:
        """(extra purchases in the agent group credited to this unit, its standard error),
        CUPED-adjusted; None if a group has fewer than two customers."""
        groups = [self.group[c] for c in members]
        n_a = sum(1 for g in groups if g is Group.AGENT)
        n_h = sum(1 for g in groups if g is Group.HOLDOUT)
        if n_a < 2 or n_h < 2:
            return None
        y = [float(self.purchases.get(c, 0)) for c in members]
        x = [self.pre_rate.get(c, 0.0) for c in members]
        adjusted = cuped_adjust(x, y)
        a = [v for v, g in zip(adjusted, groups, strict=True) if g is Group.AGENT]
        h = [v for v, g in zip(adjusted, groups, strict=True) if g is Group.HOLDOUT]
        ma, mh = sum(a) / n_a, sum(h) / n_h
        va = sum((v - ma) ** 2 for v in a) / (n_a - 1)
        vh = sum((v - mh) ** 2 for v in h) / (n_h - 1)
        share = min(max(share, 0.0), 1.0)
        return (ma - mh) * n_a * share, math.sqrt(va / n_a + vh / n_h) * n_a * share

    def _ratio(self, extra: tuple[float, float], clicks: float) -> tuple[float, float]:
        k0, sd0 = self.prior
        den = self.strength + clicks
        k = max((k0 * self.strength + extra[0]) / den, 0.0)
        return k, math.sqrt((sd0 * self.strength) ** 2 + extra[1] ** 2) / den

    def evidence(self, now: datetime, *, kind: str = CLICK) -> float:
        """Matured units behind the comparison so far (how much it has had to go on)."""
        return self._matured(self._units(kind)[0], now)

    def estimate(self, now: datetime) -> tuple[float, float]:
        clicks = self._matured(self.click_times, now)
        extra = self._extra(sorted(self.group))
        if extra is None or clicks <= 0:
            return self.prior
        return self._ratio(extra, clicks)

    def posterior(self, now: datetime, *, kind: str = CLICK, share: float = 1.0) -> tuple[float, float]:
        """Extra purchases per unit, combining the prior and the measurement by their precision.

        `estimate` weighs the measurement by its clicks, however noisy the purchase comparison
        behind them still is; on a holdout of a hundred customers that lets one lucky week swing
        every bid. Here the measured ratio (CUPED-adjusted extra purchases / matured units) counts
        by 1 / its variance against the prior's 1 / sd^2, so it takes over as the comparison
        tightens, not merely as units pile up."""
        k0, sd0 = self._prior(kind)
        units = self._matured(self._units(kind)[0], now)
        extra = self._extra(sorted(self.group), share) if units > 0 else None
        if extra is None:
            return k0, sd0
        k_hat, se = extra[0] / units, extra[1] / units
        if se <= 0:
            return max(k_hat, 0.0), 0.0
        if sd0 <= 0:
            return k0, sd0
        w0, w = 1.0 / (sd0 * sd0), 1.0 / (se * se)
        return max((k0 * w0 + k_hat * w) / (w0 + w), 0.0), math.sqrt(1.0 / (w0 + w))

    def by_segment(
        self, now: datetime, *, kind: str = CLICK, share: float = 1.0
    ) -> dict[str, tuple[float, float]]:
        """Extra purchases per unit per segment, each shrunk towards the business-wide posterior;
        segments the agent has not measured keep a past campaign's ratio to the business figure."""
        pooled = self.posterior(now, kind=kind, share=share)
        out = {seg: (pooled[0] * r, pooled[1] * r) for seg, r in self.history_ratio.get(kind, {}).items()}
        raw: dict[str, tuple[float, float]] = {}
        members: dict[str, list[str]] = {}
        for cid in sorted(self.group):
            seg = self.segment.get(cid)
            if seg is not None:
                members.setdefault(seg, []).append(cid)
        by = self._units(kind)[1]
        for seg, cids in members.items():
            units = self._matured(by.get(seg, ()), now)
            extra = self._extra(cids, share) if units >= 1.0 else None
            if extra is None:
                continue
            # no prior here: the pool is the prior, and shrink_to_pool decides how far to trust this
            raw[seg] = (extra[0] / units, extra[1] / units)
        if raw:
            shrunk = shrink_to_pool(raw, pooled[0])
            out.update(
                {seg: (max(k, 0.0), math.sqrt(sd * sd + pooled[1] ** 2)) for seg, (k, sd) in shrunk.items()}
            )
        return out


@dataclass
class OrganicRate:
    """Each customer's own chance of buying in the next `days` without a message.

    Their purchases per day over the history we can see, shrunk towards their segment's average
    (worth `ORGANIC_PRIOR_DAYS` days). What a discount costs depends on it: everyone who buys while
    an offer is open redeems it, including people who would have bought anyway.
    """

    segmenter: Segmenter
    days: float = float(DEFAULT_LEARN_HORIZON_DAYS)
    segment_rate: dict[str, float] = field(default_factory=dict)
    overall: float = 0.005

    def _observed_days(self, state: CustomerState, now: datetime) -> float:
        start = state.first_seen_at
        data_start = self.segmenter.data_start
        if data_start is not None and (start is None or start < data_start):
            start = data_start
        return max((now - start).total_seconds() / 86_400.0, 1.0) if start is not None else 1.0

    def fit(self, states: Sequence[CustomerState], now: datetime) -> None:
        by: dict[str, list[float]] = {}
        for s in states:
            by.setdefault(self.segmenter.label(s, now), []).append(s.purchases / self._observed_days(s, now))
        rates = [r for rs in by.values() for r in rs]
        self.overall = sum(rates) / len(rates) if rates else self.overall
        self.segment_rate = {seg: sum(rs) / len(rs) for seg, rs in by.items() if rs}

    def __call__(self, state: CustomerState) -> float:
        now = state.as_of
        prior = self.segment_rate.get(state.segment or "", self.overall)
        days = self._observed_days(state, now)
        rate = (state.purchases + prior * ORGANIC_PRIOR_DAYS) / (days + ORGANIC_PRIOR_DAYS)
        return 1.0 - math.exp(-rate * self.days)


# --- what a purchase is worth ------------------------------------------------------------------------

VALUE_PRIOR_ORDERS = 3.0
"""A customer's own average order is shrunk towards the business's, worth this many orders."""


@dataclass(frozen=True)
class ShrunkValueModel(ValueModel):
    """A `ValueModel` that trusts a customer's own average order only as far as their orders go.

    Order values vary a lot (one $6 order and one $180 order are both normal), so a customer with one
    or two orders says little about their next one. Their average is shrunk towards the business's
    (`base`'s fallback, which learns from every purchase) with `prior_orders` orders' weight; the
    margin rate likewise. Future value and the objective handling are `ValueModel`'s.
    """

    base: ValueModel = field(default_factory=ValueModel)
    prior_orders: float = VALUE_PRIOR_ORDERS

    def _fallback_order(self) -> float:
        learned = getattr(self.base, "learned_order_value", None)
        return float(learned) if isinstance(learned, int | float) else self.base.default_order_value

    def _fallback_margin(self) -> float:
        learned = getattr(self.base, "learned_margin_rate", None)
        return float(learned) if isinstance(learned, int | float) else self.base.default_margin_rate

    def order_value(self, state: CustomerState) -> float:
        n = max(state.purchases, 0)
        own = max(state.revenue, 0.0) if n else 0.0
        return (own + self.prior_orders * self._fallback_order()) / (n + self.prior_orders)

    def margin_rate(self, state: CustomerState) -> float:
        fallback = self._fallback_margin()
        if state.revenue <= 0 or state.margin <= 0:
            return fallback
        weight = self.prior_orders * self._fallback_order()
        return min((state.margin + fallback * weight) / (state.revenue + weight), 1.0)

    def future_value(self, state: CustomerState) -> float:
        clv = state.scores.get("clv_90d")
        if clv is not None and math.isfinite(clv) and clv >= 0:
            return clv
        return self.order_value(state) * self.margin_rate(state) * self.base.future_orders


# --- the attention price ---------------------------------------------------------------------------

LOST_VALUE_WEEKS = 26.0
"""How far ahead an unsubscribe costs us: half a year of the messages we would have sent on that
channel. A contact permission is permanent, but a plan that far out is uncertain and the customer's
interest drifts, so the far future is not counted (CRM teams value a subscriber over 6-12 months)."""
COMPLAINT_BASE_RATE = 0.0005
"""Before any send: one message in 2,000 draws a spam complaint (inbox providers start filtering a
sender at about 0.1%)."""
COMPLAINT_COST = 2.0
"""What one complaint costs beyond the lost contact: deliverability for every other customer. A
rough figure in the goal's currency; a business that knows its own should set it."""


@dataclass(frozen=True)
class _Option:
    channel: Channel
    gross: float
    """Uplift x value of one message at the customer's current count, before any urgency."""
    fixed: float
    """Offer and channel cost of one message."""
    urgency: float


BURST_GAP_DAYS = 1.0
"""In a burst, each message after the first goes out a day after the one before (the agent decides
daily, and nothing goes within `MIN_GAP` of the last message)."""
BURST_WITHIN = timedelta(days=2)
"""A customer is in the middle of a burst while their last message is at most this old."""


@dataclass(frozen=True)
class Rhythm:
    """How a customer's weekly budget is spent: `size` messages in any 7 days, either `steady`
    (spread out, so each message has `size - 1` others in its week) or in a burst (back to back,
    then a rest until the week is clear, so the j-th message of a burst has j - 1 before it)."""

    size: int
    burst: bool
    weekly_value: float

    def allows(self, contacts_7d: int, last_contact_age: timedelta | None) -> bool:
        """Whether one more message fits: under the size, and for a burst only at its start (a clear
        week) or in its middle (the last message went out within `BURST_WITHIN`)."""
        if contacts_7d >= self.size:
            return False
        if not self.burst or contacts_7d == 0:
            return True
        return last_contact_age is not None and last_contact_age <= BURST_WITHIN


def rhythms(values: Sequence[float]) -> list[Rhythm]:
    """Every way to spend a weekly budget the cap allows, with its value per week.

    `values[j]` is what a message with j others in its week is worth after its price (j = 0 .. cap
    - 1). Steady b: b messages a week, each with b - 1 others, `b x values[b - 1]`. A burst of b:
    `values[0] + ... + values[b - 1]` every `7 + (b - 1) x BURST_GAP_DAYS` days, since the next burst
    can only start once its first message has a clear week. The list is in order of preference on a
    tie: fewer messages first, a burst before the steady rhythm of the same size."""
    out: list[Rhythm] = []
    for b in range(1, len(values) + 1):
        if b >= 2:
            cycle = 7.0 + (b - 1) * BURST_GAP_DAYS
            out.append(Rhythm(b, True, sum(values[:b]) * 7.0 / cycle))
        out.append(Rhythm(b, False, b * values[b - 1]))
    return out


def best_rhythm(values: Sequence[float]) -> Rhythm:
    """The rhythm worth most a week; the first in `rhythms`' order on a tie."""
    best: Rhythm | None = None
    for r in rhythms(values):
        if best is None or r.weekly_value > best.weekly_value + 1e-12:
            best = r
    assert best is not None
    return best


class ContactPrice:
    """What one more message costs a customer, per channel, and how their weekly budget is spent.

    **Attention price** of a message on a channel, for a customer with n messages in the last week:

        P(unsubscribe or complaint | channel, n) x lost future value(customer, channel)
        + P(complaint | channel, n) x the complaint cost

    * The chances come from `HazardModel`s learned from every send (WhatsApp and email apart, each
      contact count apart, shrunk towards each other), starting from a conservative prior.
    * The **lost future value** is what the messages we would still send on that channel are worth:
      `LOST_VALUE_WEEKS` x the weekly value of that channel's best rhythm for this customer (see
      below), from the customer's own options now. Those options carry the per-person factors,
      which are shrunk to the segment, and the measured lift per click, so a customer messages do not
      move (or a shop where nothing works) has little to lose, and a keen WhatsApp reader a lot.

    **Weekly budget.** A message with j others in its week is worth `fatigue(j)` of a fresh one and
    costs `price(j)`. The customer's budget is the rhythm (`rhythms`) worth most a week: one
    message a week; a burst of two (a message, another the next day, then a rest until the week is
    clear: one fresh and one tired message every eight days); or two spread out (both tired). The
    policy holds the customer whenever one more message does not fit that rhythm, so a second
    message goes out only when it pays for itself and for tiring the next one. The auction spends
    each slot on the best option there is, so a cheap email does not take the slot (and tire the
    reader) that a WhatsApp message would have used better.

    `fatigue(n)` is the click model's "messages this week" factor; `cap()` the briefs' weekly cap.
    """

    def __init__(
        self,
        fatigue: Callable[[int], float],
        cap: Callable[[], int],
        *,
        hazard: HazardModel | None = None,
        complaints: HazardModel | None = None,
        weeks: float = LOST_VALUE_WEEKS,
        complaint_cost: float = COMPLAINT_COST,
    ) -> None:
        self.fatigue = fatigue
        self.cap = cap
        self.hazard = hazard if hazard is not None else HazardModel()
        self.complaints = complaints if complaints is not None else HazardModel(base_rate=COMPLAINT_BASE_RATE)
        self.weeks = weeks
        self.complaint_cost = complaint_cost

    @staticmethod
    def _options(bids: Sequence[Bid]) -> list[_Option]:
        return [
            _Option(
                b.action.channel,
                max(b.uplift, 0.0) * max(b.value_if_converted, 0.0),
                b.offer_cost + b.channel_cost,
                b.urgency,
            )
            for b in bids
        ]

    def _relative(self, contacts_7d: int, top: int) -> list[float]:
        """How a message at each count 0..top compares with one at the customer's count now."""
        now = max(self.fatigue(contacts_7d), 1e-9)
        return [max(self.fatigue(j), 0.0) / now for j in range(top + 1)]

    def lost_value(self, state: CustomerState, bids: Sequence[Bid]) -> dict[Channel, float]:
        """Per channel: what losing the customer on it costs (its best rhythm's weekly value x the
        horizon, before any attention price)."""
        cap = max(self.cap(), 1)
        rel = self._relative(state.contacts_7d, cap - 1)
        values: dict[Channel, list[float]] = {}
        for o in self._options(bids):
            mine = values.setdefault(o.channel, [0.0] * cap)
            for j in range(cap):
                mine[j] = max(mine[j], o.gross * rel[j] - o.fixed)
        return {ch: self.weeks * max(best_rhythm(v).weekly_value, 0.0) for ch, v in values.items()}

    def _price(
        self, channel: Channel, contacts_7d: int, lost: float, state: CustomerState | None = None
    ) -> float:
        """With a `LookalikeHazard` (v3 frequency, opt-in) and the customer's `state`, the rates are the
        customer's own; otherwise the plain curves'."""
        return (
            self._rate(self.hazard, channel, contacts_7d, state) * lost
            + self._rate(self.complaints, channel, contacts_7d, state) * self.complaint_cost
        )

    @staticmethod
    def _rate(model: HazardModel, channel: Channel, contacts_7d: int, state: CustomerState | None) -> float:
        if state is not None and isinstance(model, LookalikeHazard):
            return model.rate_for(state, channel, contacts_7d)
        return model.rate(channel, contacts_7d)

    def price_at(self, state: CustomerState, channel: Channel, contacts: float, lost: float) -> float:
        """The attention price of a message at a fractional week count (the planner's: earlier planned
        messages count part-way). At a whole count it is the price the auction charges."""
        return lerp(lambda n: self._price(channel, n, lost, state), contacts)

    def freshness(self, contacts: float) -> float:
        """The click model's "messages this week" factor at a fractional count."""
        return lerp(self.fatigue, contacts)

    def prices(self, state: CustomerState, bids: Sequence[Bid]) -> dict[Channel, float]:
        """The attention price of the next message on each channel the bids use."""
        lost = self.lost_value(state, bids)
        return {ch: self._price(ch, state.contacts_7d, v, state) for ch, v in lost.items()}

    def floor(self, state: CustomerState) -> float:
        """The cheapest a message can be for this customer (no future value at stake): the
        complaint cost alone, on their cheapest reachable channel."""
        channels = state.contactable_channels() or (Channel.EMAIL,)
        return min(self._price(ch, state.contacts_7d, 0.0, state) for ch in channels)

    def rhythm(self, state: CustomerState, bids: Sequence[Bid]) -> Rhythm:
        """The customer's best rhythm, from their options now, each count priced on its own."""
        cap = max(self.cap(), 1)
        if not bids:
            return Rhythm(1, False, 0.0)
        lost = self.lost_value(state, bids)
        rel = self._relative(state.contacts_7d, cap - 1)
        options = self._options(bids)
        values = [
            max(
                o.urgency * (o.gross * rel[j] - o.fixed)
                - self._price(o.channel, j, lost.get(o.channel, 0.0), state)
                for o in options
            )
            for j in range(cap)
        ]
        return best_rhythm(values)

    def budget(
        self, state: CustomerState, bids: Sequence[Bid], cost: Callable[[Bid], float] | None = None
    ) -> int:
        """The policy's hook: how many messages the customer's last 7 days may hold for one more to
        go now. Their rhythm's size while one more fits it, else their count (which holds them);
        never below 1, so the first message of a week is the auction's call. `cost` is accepted for
        the hook; each count is priced here."""
        del cost
        rhythm = self.rhythm(state, bids)
        last = state.last_contact_at
        send_at = min((b.action.send_at for b in bids), default=state.as_of)
        age = send_at - last if last is not None else None
        if rhythm.allows(state.contacts_7d, age):
            return max(rhythm.size, 1)
        return max(state.contacts_7d, 1)


# --- the breaker --------------------------------------------------------------------------------


@dataclass
class _Outcome:
    ts: datetime
    goal_id: str
    channel: Channel
    kind: EventType


@dataclass
class Breaker:
    """The brief's stop rules, judged on the agent's own sends of the last week."""

    outcomes: list[_Outcome] = field(default_factory=list)
    paused_goals: dict[str, str] = field(default_factory=dict)
    """goal_id -> the plain sentence that paused it."""
    paused_channels: dict[tuple[str, Channel], str] = field(default_factory=dict)
    unsub_baseline: dict[str, float] = field(default_factory=dict)
    """goal_id -> the goal's own unsubscribe rate over its first `BASELINE_DAYS` days of sends."""
    _first: dict[str, list[float]] = field(default_factory=dict)  # goal -> [delivers, unsubs, t0]

    def record(self, event: Event, goal_id: str, channel: Channel) -> None:
        if event.type not in (
            EventType.DELIVER,
            EventType.FAIL,
            EventType.BOUNCE,
            EventType.UNSUBSCRIBE,
            EventType.COMPLAINT,
        ):
            return
        self.outcomes.append(_Outcome(event.ts, goal_id, channel, event.type))
        if goal_id not in self.unsub_baseline and event.type in (EventType.DELIVER, EventType.UNSUBSCRIBE):
            first = self._first.setdefault(goal_id, [0.0, 0.0, event.ts.timestamp()])
            first[0 if event.type is EventType.DELIVER else 1] += 1.0

    def _settle_baseline(self, goal_id: str, now: datetime) -> None:
        """Fix the goal's unsubscribe baseline once it has `BASELINE_DAYS` days and `BASELINE_SENDS`
        deliveries behind it (unsubscribes arrive hours after the message, so the first deliveries
        alone would make the baseline look like zero)."""
        first = self._first.get(goal_id)
        if goal_id in self.unsub_baseline or first is None:
            return
        if first[0] >= BASELINE_SENDS and now.timestamp() - first[2] >= BASELINE_DAYS * 86_400.0:
            self.unsub_baseline[goal_id] = first[1] / first[0]

    def check_history(self, briefs: Iterable[Brief], counts: Mapping[Channel, tuple[int, int]]) -> list[str]:
        """Before the first decision: judge each brief's complaint-rate rule on the client's past
        campaigns, channel by channel, with the weekly check's own test (at least
        `BREAKER_MIN_EVENTS` complaints, a count that would happen less than once in 10,000 at the
        limit). `counts` maps a channel to (delivered past messages, those that drew a complaint).

        A channel whose history already breaks the limit is paused for the goal before anything is
        sent on it, until a person approves a new brief version: a business whose past messages on a
        channel drew complaints at many times the brief's limit should not have the agent find that
        out again on its customers. Returns sentences for the new pauses."""
        news: list[str] = []
        for brief in briefs:
            for rule in brief.envelope.stop_rules:
                if rule.metric is not StopMetric.COMPLAINT_RATE:
                    continue
                for channel in brief.envelope.channels:
                    sent, complaints = counts.get(channel, (0, 0))
                    key = (brief.goal_id, channel)
                    if key in self.paused_channels or complaints < BREAKER_MIN_EVENTS:
                        continue
                    if _poisson_tail(complaints, rule.threshold * sent) >= BREAKER_ALPHA:
                        continue
                    sentence = (
                        f"Paused {channel.value} before the first message: past campaigns drew "
                        f"{complaints} complaints on {sent:,} messages ({complaints / max(sent, 1):.2%}), "
                        f"above the {rule.threshold:.2%} limit."
                    )
                    self.paused_channels[key] = sentence
                    news.append(sentence)
        return news

    def resume(self, goal_id: str) -> None:
        """A new brief version: a person has looked, so the goal starts afresh."""
        self.paused_goals.pop(goal_id, None)
        for key in [k for k in self.paused_channels if k[0] == goal_id]:
            del self.paused_channels[key]
        self.unsub_baseline.pop(goal_id, None)
        self._first.pop(goal_id, None)
        self.outcomes = [o for o in self.outcomes if o.goal_id != goal_id]

    def check(self, briefs: Iterable[Brief], now: datetime) -> list[str]:
        """Judge every brief's stop rules on the last week; returns sentences for new pauses."""
        since = now - BREAKER_WINDOW
        self.outcomes = [o for o in self.outcomes if o.ts >= since and o.ts <= now]
        news: list[str] = []
        for brief in briefs:
            goal = brief.goal_id
            if goal in self.paused_goals:
                continue
            self._settle_baseline(goal, now)
            mine = [o for o in self.outcomes if o.goal_id == goal]
            if not mine:
                continue
            delivered = sum(1 for o in mine if o.kind is EventType.DELIVER)
            complaints = sum(1 for o in mine if o.kind is EventType.COMPLAINT)
            unsubs = sum(1 for o in mine if o.kind is EventType.UNSUBSCRIBE)
            for rule in brief.envelope.stop_rules:
                why: str | None = None
                if rule.metric is StopMetric.COMPLAINT_RATE and complaints >= BREAKER_MIN_EVENTS:
                    if _poisson_tail(complaints, rule.threshold * delivered) < BREAKER_ALPHA:
                        why = (
                            f"Paused: {complaints} complaints on {delivered} messages this week "
                            f"({complaints / max(delivered, 1):.2%}), above the {rule.threshold:.2%} limit."
                        )
                elif rule.metric is StopMetric.UNSUBSCRIBE_MULTIPLE and unsubs >= BREAKER_MIN_EVENTS:
                    base = self.unsub_baseline.get(goal)
                    if (
                        base is not None
                        and base > 0
                        and _poisson_tail(unsubs, rule.threshold * base * delivered) < BREAKER_ALPHA
                    ):
                        why = (
                            f"Paused: unsubscribes ran at {unsubs / max(delivered, 1):.2%} this week, "
                            f"more than {rule.threshold:g} times the usual {base:.2%}."
                        )
                elif rule.metric is StopMetric.DELIVERY_RATE_BELOW:
                    for channel in sorted({o.channel for o in mine}, key=lambda c: c.value):
                        key = (goal, channel)
                        if key in self.paused_channels:
                            continue
                        ok = sum(1 for o in mine if o.channel is channel and o.kind is EventType.DELIVER)
                        bad = sum(
                            1
                            for o in mine
                            if o.channel is channel and o.kind in (EventType.FAIL, EventType.BOUNCE)
                        )
                        if bad >= BREAKER_MIN_EVENTS and _wilson_high(ok, ok + bad) < rule.threshold:
                            sentence = (
                                f"Paused {channel.value}: only {ok / (ok + bad):.0%} of messages were "
                                f"delivered this week, below {rule.threshold:.0%}."
                            )
                            self.paused_channels[key] = sentence
                            news.append(sentence)
                    continue
                if why is None:
                    continue
                if rule.action == "pause_channel":
                    for channel in sorted({o.channel for o in mine}, key=lambda c: c.value):
                        self.paused_channels[(goal, channel)] = why
                else:
                    self.paused_goals[goal] = why
                news.append(why)
                if goal in self.paused_goals:
                    break
        return news


# --- the agent ----------------------------------------------------------------------------------


def _kind_of(brief: Brief, kinds: Mapping[str, GoalKind]) -> GoalKind:
    return kinds.get(brief.goal_id, GoalKind.CUSTOM)


BASELINE_GROUP = "baseline_group"
"""The block code `explain_bids` gives every bid of a baseline-group customer: the baseline rule, not
the auction, decides for them."""

PRUNED = "pruned"
"""The block code `explain_bids` gives an option the bidder's own pruning left out (an offer not
expected to beat its plain version, an option that cannot plausibly be the best, or every option
of someone contact clearly harms) when no hard rule blocks it first: `decide` never sees it."""


@dataclass(frozen=True)
class BidExplanation:
    """One option the auction would see for a customer, as `AgentPolicy.explain_bids` reports it.

    `bid` is timed as the auction times it. `net` is its expected net minus the price it must beat
    (the attention price), so a positive `net` beats doing nothing on average. `blocked_by` is the
    hard rule that removes it (a code from `autonomy.decide.rules`, or `BASELINE_GROUP`), None when
    it may go out. `considered` is True for the options Thompson sampling and the exploration floor
    choose among (the near-best few that could beat doing nothing). `item_reason` says why the
    action is about its product (`bid.action.item_id`), when it is about one. `moment_reasons` names
    the live moments that raise the bid's goal for this customer (plan-v3 decision 2; empty when
    moments are off or none is live), strongest first.
    """

    bid: Bid
    net: float
    blocked_by: str | None = None
    considered: bool = False
    item_reason: str | None = None
    moment_reasons: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.blocked_by is None


def _quote(bidder: Bidder, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
    """A bidder's bids without its learning memo (`GoalBidder.quote`); `bids` for other bidders."""
    quote = getattr(bidder, "quote", None)
    if callable(quote):
        found = quote(state, ctx)
        if isinstance(found, list):
            return [b for b in found if isinstance(b, Bid)]
    return bidder.bids(state, ctx)


def _quote_all(bidder: Bidder, state: CustomerState, ctx: DecisionContext) -> list[tuple[Bid, bool]]:
    """Every option a bidder prices, each with whether its pruning keeps it (what `decide` sees).
    A bidder without `GoalBidder.quote(prune=False)` keeps all it bids."""
    kept = _quote(bidder, state, ctx)
    if isinstance(bidder, GoalBidder):
        return [(b, b in kept) for b in bidder.quote(state, ctx, prune=False)]
    return [(b, True) for b in kept]


def _item_reason(bid: Bid, bidders: Mapping[str, Bidder], state: CustomerState) -> str | None:
    if bid.action.item_id is None:
        return None
    bidder = bidders.get(bid.goal_id)
    choice = bidder.item_for(state) if isinstance(bidder, GoalBidder) else None
    return choice.reason if choice is not None and choice.item_id == bid.action.item_id else None


def _moment_reasons(
    bid: Bid, bidders: Mapping[str, Bidder], state: CustomerState, now: datetime
) -> tuple[str, ...]:
    """The live moments raising the bid's goal (`GoalBidder.moment_reasons`); () for other bidders."""
    bidder = bidders.get(bid.goal_id)
    return bidder.moment_reasons(state, now) if isinstance(bidder, GoalBidder) else ()


def _later_step(decision: Decision | None) -> bool:
    """Whether the decision sends a journey's later step (not its first message)."""
    return (
        decision is not None
        and decision.kind is DecisionKind.ACT
        and decision.action is not None
        and decision.action.follow_up_step > 0
    )


class _Undecorated:
    """The agent as the experiments' wrapper sees it: `decide` without the wrapper, everything else as the agent's."""

    def __init__(self, agent: AgentPolicy) -> None:
        self._agent = agent
        self.version = agent.version

    def decide(self, state: CustomerState, ctx: DecisionContext, *, trigger: str | None = None) -> Decision:
        return self._agent._decide(state, ctx, trigger=trigger)

    def retract_decision(self, decision_id: str) -> None:
        """The wrapper is about to decide this moment again (same decision id): drop what the agent kept of
        the first attempt that the second does not overwrite. The journey runner undoes it by itself
        (`JourneyRunner.begin`), and the scheduled send is replaced under the same id."""
        if self._agent.outcomes is not None:
            self._agent.outcomes.forget(decision_id)
        self._agent._tired_profiles.pop(decision_id, None)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._agent, name)


class AgentPolicy:
    """Autonomy's policy for the agent group (implements `Policy`). Build it with `build_agent_policy`."""

    def __init__(
        self,
        briefs: Sequence[Brief],
        content: Sequence[ContentItem],
        bundle: ModelBundle,
        *,
        costs: Mapping[Channel, float] | None = None,
        exploration_floor: float | None = None,
        baseline_policy: Policy | None = None,
        kinds: Mapping[str, GoalKind] | None = None,
        learn_horizon_days: int = DEFAULT_LEARN_HORIZON_DAYS,
        mc_draws: int = 400,
        version: str = AGENT_VERSION,
        item_lookup: ItemLookup | None = None,
        recommender: ItemLookup | None = None,
        asset_of: AssetOf | None = None,
        carousel_template: bool = False,
        format_costs: Mapping[Format, float] | None = None,
        pacing: bool = False,
        reading_hour_model: bool = True,
        restraint: bool = True,
        weekly_budget: bool = True,
        currency: str = "$",
        offer_pricing: OfferPricing | None = None,
        moments: MomentBook | Literal[False] | None = None,
        who: WhoBook | None = None,
        frequency: FrequencyConfig | None = None,
        journeys: JourneyRunner | None = None,
        experiments: ExperimentBook | None = None,
    ) -> None:
        if learn_horizon_days < 1:
            raise ValueError("learn_horizon_days must be at least 1")
        # v3 offers (docs/v3/OFFERS.md), off by default: a budget price per goal, charged on every
        # offer's cost by its bidder and moved once a day by the offer money actually spent.
        self.offer_pricing = offer_pricing
        self.offer_prices: dict[str, BudgetPrice] = {}
        self._offer_spend: dict[str, float] = {}
        self._channel_spend: dict[str, float] = {}
        self._offer_goal: dict[tuple[str, str], str] = {}
        self._priced_sends: set[str] = set()
        self._priced_day: date | None = None
        self.version = version
        self.refill_items: dict[str, str | tuple[str, str]] = {}
        """customer -> refill pick (item id, or (item id, reason)), set by `set_items`; read when no
        `item_lookup` is given."""
        self.complements: dict[str, str | tuple[str, str]] = {}
        """customer -> complementary product, set by `set_items`; read when no `recommender` is given."""
        self.item_lookup: ItemLookup = item_lookup if item_lookup is not None else self.refill_items.get
        self.recommender: ItemLookup = recommender if recommender is not None else self.complements.get
        self.asset_of = asset_of
        self.carousel_template = carousel_template
        self.format_costs = dict(format_costs) if format_costs is not None else None
        self.moments: MomentBook | Literal[False] | None = moments
        """The bidders' moments (plan-v3 decision 2): a book, False for off, None for `use_moments`'."""
        self.who = who
        """Plan-v3 decision 1 (None: off): the slow models' scores, written to the state each decision
        is made on, and the trait-pooled leave-alone (docs/v3/WHO.md)."""
        self.bundle = bundle
        self.costs = dict(costs) if costs is not None else None
        self.kinds: dict[str, GoalKind] = dict(kinds or {})
        self.learn_horizon_days = learn_horizon_days
        self.content_by_id: dict[str, ContentItem] = {c.content_id: c for c in content}
        self.segmenter = Segmenter()
        self.reading = ReadingHours()
        self.breaker = Breaker()
        self.alerts: list[str] = []
        self.chooser: PolicyChooser | None = None
        """Set by `use_chooser`; None (the default) leaves every decision exactly as the agent made it."""
        self.value = ShrunkValueModel(base=bundle.value)
        # v3 frequency (docs/v3/FREQUENCY.md), off by default: tolerance learned from look-alikes and
        # 3-week plans. With it off the hazard models are the plain ones and no gate is installed.
        self.frequency = frequency
        tolerance = frequency.tolerance if frequency is not None else None
        self.contact_price = (
            ContactPrice(
                self._click_fatigue,
                self._weekly_cap,
                hazard=LookalikeHazard(tolerance),
                complaints=LookalikeHazard(tolerance, base_rate=COMPLAINT_BASE_RATE),
            )
            if tolerance is not None
            else ContactPrice(self._click_fatigue, self._weekly_cap)
        )
        self.planner: FrequencyPlanner | None = (
            FrequencyPlanner(self.contact_price, frequency.plans, fade=self._urgency_fade)
            if frequency is not None and frequency.plans is not None
            else None
        )
        self._tired_profiles: dict[str, tuple[str, Profile]] = {}
        """With tolerance on: decision_id -> (customer, the profile the send was decided on), until its
        delivery is seen and the hazard models take it. Filled only by `decide` in this process."""
        self.pacer: Pacer | None = Pacer(step_aside=True) if pacing else None
        self.hour_model: ReadingHourModel | None = ReadingHourModel() if reading_hour_model else None
        self.restraint = restraint
        self._weak_lift: dict[str, bool] = {}
        """Segment (or ANY) -> whether the evidence that messages create lift there is weak tonight."""
        self.lift = LiftPerClick(effect_days=float(learn_horizon_days), send_prior=LIFT_PER_SEND_PRIOR)
        self.organic = OrganicRate(self.segmenter, days=float(learn_horizon_days))
        if isinstance(bundle.response, ClickResponseModel):
            bundle.response.organic = self.organic
        self.outcomes: OutcomeBook | None = (
            OutcomeBook(timedelta(days=learn_horizon_days))
            if isinstance(bundle.response, LookalikeClickModel)
            else None
        )
        """With the look-alike model on: the profile each holdout or sent decision was made on, kept until its
        window closes and handed to the model as an outcome (`nightly`). None otherwise. It is filled only by
        `decide` in this process: decisions replayed through `nightly` after a rebuild do not reach it."""
        self.briefs: dict[str, Brief] = {}
        self.bidders: dict[str, GoalBidder] = {}
        self.policy = AutonomousPolicy(
            [],
            self.contact_price.floor,
            exploration_floor,
            baseline_policy,
            mc_draws=mc_draws,
            currency=currency,
            version=version,
            plausible_z=PLAUSIBLE_Z,
            min_gap=MIN_GAP,
            pacer=self.pacer,
            channel_price_fn=self.contact_price.prices,
            budget_fn=self.contact_price.budget if weekly_budget else None,
            margin_fn=self._margin if restraint else None,
            plan_fn=self.planner.gate if self.planner is not None else None,
            journeys=journeys,
        )
        if journeys is not None and journeys.fade is None:
            journeys.fade = self._urgency_fade  # a later step is priced at the urgency left on its day
        if journeys is not None:
            journeys.follow_rhythm = False  # the journey is the plan; the weekly rhythm would veto its steps
        self._journeys_replayed = False
        self.experiment_policy: ExperimentPolicy | None = None
        """With a book (the v3 preset), the wrapper `decide` goes through: it runs the book's running
        experiments on a random few and changes nothing while none runs (docs/v3/EXPERIMENTS.md)."""
        if experiments is not None:
            from autonomy.experiments.policy import ExperimentPolicy

            busy = None if journeys is None else (lambda cid: journeys.active(cid) is not None)
            self.experiment_policy = ExperimentPolicy(_Undecorated(self), experiments, busy=busy)
        for brief in briefs:
            self._install(brief)
        # what the nightly learner needs and decisions do not carry
        self._segments: dict[str, str] = {}
        self._timezones: dict[str, str] = {}
        self._decision_segment: dict[str, str] = {}
        """decision_id -> the customer's segment when it was made. Learning must use this, not the
        segment today: a customer who bought since is no longer "lapsed", and labelling their old
        window with today's segment would teach that lapsed customers never buy."""
        self._log: dict[str, Decision] = {}
        self._events: dict[str, list[Event]] = {}
        self._seen_events: set[str] = set()
        self._sends: dict[str, tuple[str, Channel, datetime]] = {}
        """decision_id -> (goal, channel, send time) of the agent group's sends, for the breaker."""
        self._send_keys: dict[str, tuple[str | None, str, str]] = {}
        """decision_id -> (segment, action key, offer) of the agent group's sends, for the click model."""
        self._clicked: set[str] = set()
        self._lost: set[str] = set()
        """Sends whose lost contact the hazard model has counted (once per message)."""
        self._complained: set[str] = set()
        self._send_contacts: dict[str, tuple[int, int | None]] = {}
        """decision_id -> (messages the customer had in the week before that send, its local hour)."""
        self._forgotten_before: datetime | None = None
        """Events before this were pruned from memory; they are never observed again."""
        self._ctx_key: tuple[object, ...] | None = None
        self._decided = False
        """Set by the first decision: until then, events handed to `nightly` are history."""
        self._history: dict[str, dict[str, Event]] = {}
        """Customer -> event id -> event handed over before the first decision (the history). Kept
        by id and read back as past campaigns' sends only at the first decision, so a caller that
        hands the whole log twice does not count a send twice, and a click handed over in a later
        call than its send is still matched to it."""
        self.history_used = 0
        """How many logged history sends the warm start learned from."""
        self.digest = HistoryDigest()
        """Purchases, consent and first sightings from the history, kept compactly until the warm
        start (and dropped after it)."""
        self._history_holdout: dict[str, datetime] = {}
        """Customer -> when, for a past campaign's holdout handed over as decisions before the first
        decision (an explicit control group)."""
        self.calibration: dict[str, object] = {}
        self.uplift: UpliftWarmStart | None = None
        """What the warm start learned about who a message moves (`autonomy.uplift`): the fitted
        per-customer model, attached to the response model so every bidder blends it into its bids,
        or None and its `notes` saying why not. None until the warm start."""
        """What the warm start measured on past randomised campaigns (for explanations and tests)."""
        self._expected_by_kind: dict[str, float] = {CLICK: 0.0, SEND: 0.0}
        """Uplift the agent's own sends were expected to bring, per response unit: each kind's share
        of the holdout difference."""
        self._refit_at: datetime | None = None
        self._live: tuple[Brief, ...] | None = None
        self._content_ref: object = None

    # --- briefs and bidders ---------------------------------------------------------------------

    def set_items(
        self,
        refill: Mapping[str, str | tuple[str, str]] | None = None,
        complements: Mapping[str, str | tuple[str, str]] | None = None,
    ) -> None:
        """Replace the products the bidders read (unless `item_lookup` / `recommender` were given).

        `refill` maps a customer to their refill pick (`autonomy.catalog.enrich_all`'s mapping) and
        `complements` to a complementary product (from `autonomy.catalog.next_best_item`), each an
        item id or (item id, plain reason). A mapping left as None keeps what it held. Call it with
        the enriched states, before the sweep: a customer missing from the mapping has no product.
        """
        if refill is not None:
            self.refill_items.clear()
            self.refill_items.update(refill)
        if complements is not None:
            self.complements.clear()
            self.complements.update(complements)

    def _new_bidder(self, brief: Brief) -> GoalBidder:
        return GoalBidder(
            brief,
            None,  # read the live library from ctx.content
            self.bundle.response,
            self.value,
            self.costs,
            kind=_kind_of(brief, self.kinds),
            own_spread=True,
            prune_z=PRUNE_Z,
            item_lookup=self.item_lookup,
            recommender=self.recommender,
            asset_of=self.asset_of,
            carousel_template=self.carousel_template,
            format_costs=self.format_costs,
            **self._offer_pricing_hooks(brief),
            moments=self.moments,
            who=self.who,
        )

    def _install(self, brief: Brief) -> None:
        self.briefs[brief.goal_id] = brief
        self.bidders[brief.goal_id] = self._new_bidder(brief)
        self.policy.bidders = list(self.bidders.values())

    def _is_newer(self, brief: Brief) -> bool:
        """Whether the context's brief replaces the installed one (or there is none yet)."""
        known = self.briefs.get(brief.goal_id)
        return (
            known is None
            or brief.version > known.version
            or (brief.version == known.version and brief.brief_id != known.brief_id)
        )

    def _refresh(self, briefs: Sequence[Brief]) -> None:
        """Rebuild a goal's bidder when the context carries a newer version of its brief."""
        for brief in briefs:
            if self._is_newer(brief):
                if brief.goal_id in self.briefs:
                    self.breaker.resume(brief.goal_id)
                    if self.pacer is not None:
                        self.pacer.reset(brief.goal_id)
                self._install(brief)

    def _effective(self, ctx: DecisionContext) -> DecisionContext:
        """The context with paused goals removed and paused channels taken out of their envelopes.

        Only the live briefs are cached (they change when a brief or a pause does); the context
        itself is always the caller's, so a budget or library that moves during the day is seen
        by the very next decision.
        """
        if ctx.content is not self._content_ref:
            self._content_ref = ctx.content
            for item in ctx.content:
                self.content_by_id[item.content_id] = item
        key: tuple[object, ...] = (
            tuple((b.brief_id, b.version) for b in ctx.briefs),
            tuple(sorted(self.breaker.paused_goals)),
            tuple(sorted((g, ch.value) for g, ch in self.breaker.paused_channels)),
        )
        live = self._live
        if key != self._ctx_key or live is None:
            self._refresh(ctx.briefs)
            live = self._live_briefs(ctx.briefs)
            self._live, self._ctx_key = live, key
        if len(live) == len(ctx.briefs) and all(a is b for a, b in zip(live, ctx.briefs, strict=True)):
            return ctx
        return ctx.model_copy(update={"briefs": live})

    def _live_briefs(
        self, briefs: Sequence[Brief], resumed: frozenset[str] = frozenset()
    ) -> tuple[Brief, ...]:
        """The briefs with paused goals and channels taken out; goals in `resumed` count as unpaused
        (a new brief version that `_refresh` has not installed yet resumes its goal)."""
        live: list[Brief] = []
        for brief in briefs:
            if brief.goal_id in self.breaker.paused_goals and brief.goal_id not in resumed:
                continue
            closed = {
                ch
                for (g, ch) in self.breaker.paused_channels
                if g == brief.goal_id and brief.goal_id not in resumed
            }
            if closed:
                channels = tuple(ch for ch in brief.envelope.channels if ch not in closed)
                if not channels:
                    continue
                brief = brief.model_copy(
                    update={"envelope": brief.envelope.model_copy(update={"channels": channels})}
                )
            live.append(brief)
        return tuple(live)

    # --- deciding -------------------------------------------------------------------------------

    def use_chooser(self, chooser: PolicyChooser | None) -> None:
        """Let `chooser` see each agent-group decision after the agent made it, and replace it (None
        removes the hook). The holdout and baseline groups never reach it. A chooser that raises is
        ignored for that customer: the agent's decision stands and `alerts` says so."""
        self.chooser = chooser

    def _chosen(
        self, state: CustomerState, ctx: DecisionContext, decision: Decision, segment: str
    ) -> Decision:
        chooser = self.chooser
        if chooser is None:
            return decision
        try:
            # The chooser sees the sends already scheduled for this customer (all but the agent's own
            # decision, which it is about to replace), so the weekly cap holds across repeated decisions.
            seen = self.policy.with_pending(state, ctx.now, ignore=decision.decision_id)
            chosen = chooser(seen, ctx, decision)
        except Exception as err:  # the agent's decision stands; a person is told
            self.alerts.append(
                f"policy chooser failed for {state.customer_id}: {err}; the agent's decision stands"
            )
            return decision
        if chosen is not decision:
            # The agent's own send is not going out: it must not count as pending towards the cap.
            if decision.action is not None:
                withdrawn = decision.model_copy(update={"kind": DecisionKind.NOTHING, "action": None})
                self.policy._remember(withdrawn)
            self.policy._remember(chosen)
            self._decision_segment[chosen.decision_id] = segment
        return chosen

    def segment(self, state: CustomerState, now: datetime) -> str:
        return self.segmenter.label(state, now)

    def use_baseline(self, policy: Policy | None) -> None:
        """The rule that decides for the baseline group (None leaves that group alone)."""
        self.policy.baseline_policy = policy

    @property
    def needs_history(self) -> bool:
        """True until the first decision: what `nightly` gets until then is the client's history,
        which the warm start learns from (the runtime streams it all in, `_feed_history`)."""
        return not self._decided

    def prepare_day(self, states: Sequence[CustomerState], ctx: DecisionContext) -> None:
        """Once a day before the sweep (optional): fit the frequency segments and the segments'
        organic purchase rates to the business's own customers (first day), and learn from the
        history before the first decision. Without it the segments keep their default cuts."""
        if not self.segmenter.fitted:
            self.segmenter.fit(states, ctx.now)
            self.organic.fit(states, ctx.now)
        if not self._decided:
            self._warm_start({s.customer_id: s for s in states}, ctx.now)

    # --- what contacting costs ------------------------------------------------------------------

    def _click_fatigue(self, contacts_7d: int) -> float:
        """How a message's click rate compares with the average message's at this weekly count."""
        model = self.bundle.response
        if isinstance(model, ClickResponseModel):
            return model.fatigue_factor(contacts_7d)[0]
        return 1.0

    def _weekly_cap(self) -> int:
        """The weekly contact cap the briefs allow (the largest, when goals differ)."""
        caps = [b.envelope.max_contacts_per_week for b in self.briefs.values()]
        return max(caps) if caps else 1

    def _margin(self, state: CustomerState, bid: Bid) -> float:
        """How far this option's net must beat doing nothing before it may be sampled.

        Zero (plain Thompson sampling) while the evidence that messages create lift in the customer's
        segment is good. Where it is weak (`_weak_lift`), `RESTRAINT_Z` of the option's own standard
        deviations, the spread Thompson sampling would have drawn it with: a lower-confidence-bound
        rule, so an option goes out only if it pays at the pessimistic end of what we know about it,
        not when one lucky draw says so. The doubt about the lift itself is already in the mean (the
        lift per click is the holdout comparison's posterior, shrunk to the prior); counting it a
        second time in the margin holds back every option at once, and is a trap (see
        docs/v2/CONTACT_LESS.md): a segment that stops being sent to stops being measured.
        """
        if not self._weak_lift.get(state.segment or ANY, self._weak_lift.get(ANY, False)):
            return 0.0
        return RESTRAINT_Z * bid.uncertainty

    def _judge_lift(self, now: datetime) -> None:
        """Nightly: in which segments is the evidence that messages create lift weak?

        Weak means the holdout comparison has had its say (`LIFT_JUDGE_WINDOWS` learning windows
        since the agent started, and at least `LIFT_EVIDENCE_CLICKS` matured clicks behind it) and
        the lift per click's posterior still includes zero at `RESTRAINT_Z` (its lower bound is at
        or below 0), whether because the effect is small or because the comparison is too noisy to
        show it. Before that the prior rules: the agent starts as a business launching a programme
        with a holdout would, by running it.

        A segment is judged on its own posterior (shrunk towards the business's) only when the
        business-wide one is weak too. A segment has a tenth of a holdout of a hundred or so, so
        its own comparison includes zero most weeks even where messages plainly work; letting that
        alone hold it back stopped sending (and so measuring) in shops that make money. Where the
        business's lift is unproven, a segment whose own lift is clear of zero is still sent to.
        """
        model = self.bundle.response
        if not self.restraint or not isinstance(model, ClickResponseModel):
            self._weak_lift = {}
            return
        start = self.lift.start
        judged_from = timedelta(days=LIFT_JUDGE_WINDOWS * self.learn_horizon_days)
        if start is None or now - start < judged_from or self.lift.evidence(now) < LIFT_EVIDENCE_CLICKS:
            self._weak_lift = {}
            return
        pooled = model.lift[0] - RESTRAINT_Z * model.lift[1] <= 0
        weak = {ANY: pooled}
        for seg, (k, sd) in model.lift_by_segment.items():
            weak[seg] = pooled and k - RESTRAINT_Z * sd <= 0
        self._weak_lift = weak

    def decide(self, state: CustomerState, ctx: DecisionContext, *, trigger: str | None = None) -> Decision:
        if self.experiment_policy is not None:  # the v3 preset: the experiments' wrapper, then `_decide`
            return self.experiment_policy.decide(state, ctx, trigger=trigger)
        return self._decide(state, ctx, trigger=trigger)

    def _decide(self, state: CustomerState, ctx: DecisionContext, *, trigger: str | None = None) -> Decision:
        if not self._decided:
            self._warm_start({}, ctx.now)
        eff = self._effective(ctx)
        seg = self.segment(state, ctx.now)
        self._segments[state.customer_id] = seg
        self._timezones[state.customer_id] = state.timezone
        if state.customer_id not in self.lift.group:
            # Purchases so far are all from before the agent started: the CUPED covariate.
            pre = self.segmenter.frequency(state, ctx.now)
            self.lift.member(state.customer_id, state.group, ctx.now, pre_rate=pre, segment=seg)
        update: dict[str, object] = {"segment": seg}
        if state.group is not Group.AGENT:
            # The holdout is left alone and the baseline rule times its own sends: no hour to plan.
            seen = state.model_copy(update=update)
            decision = self.policy.decide(seen, eff, trigger=trigger)
            self._decision_segment[decision.decision_id] = seg
            self._remember(seen, decision)
            return decision
        if self.who is not None:  # plan-v3 decision 1: the slow models' scores for this customer
            self.who.refresh(ctx.now, contact_harm_of(self.bundle.response))
            update.update(self.who.prepare(state, ctx.now))
        hour = self._send_hour(state.customer_id, seg, eff, trigger)
        if hour is not None:
            update["active_hours"] = tuple(1.0 if h == hour else 0.0 for h in range(24))
            update["contacts_7d"] = self._contacts_at_send(state, eff, hour)
        elif state.active_hours:
            # The folder's profile counts every click, most of them at the hour we sent: it would
            # only teach the agent its own habits.
            update["active_hours"] = ()
        seen = state.model_copy(update=update)
        decision = self.policy.decide(seen, eff, trigger=trigger)
        self._decision_segment[decision.decision_id] = seg
        decision = self._chosen(state, eff, decision, seg)
        self._note_profile(seen, decision)
        self._remember(seen, decision)
        return decision

    def _note_profile(self, seen: CustomerState, decision: Decision) -> None:
        """With tolerance on, keep the profile a send was decided on until its delivery is seen."""
        if isinstance(self.contact_price.hazard, LookalikeHazard) and decision.kind is DecisionKind.ACT:
            self._tired_profiles[decision.decision_id] = (decision.customer_id, profile_of(seen))

    def _remember(self, seen: CustomerState, decision: Decision) -> None:
        """With the look-alike model on, keep the profile this decision was made on until its window
        closes. Only sends and the holdout's "do nothing" are kept: the agent's own "do nothing" goes to
        the customers it judged least likely to respond, so it is not a control."""
        book = self.outcomes
        if book is None:
            return
        key: str | None = None
        action = decision.action
        if action is not None:
            if action.follow_up_step > 0:  # a journey's later step: see `_keep`
                return
            item = self.content_by_id.get(action.content_id or "")
            if item is None:
                return
            key = key_of(action.channel, action.offer_id, item.theme.value, item.format)
        book.remember(decision, profile_of(seen), key)

    # --- explaining ------------------------------------------------------------------------------

    def explain_bids(
        self, state: CustomerState, ctx: DecisionContext, *, trigger: str | None = None
    ) -> list[BidExplanation]:
        """Every bid the auction would see for this customer now, ranked, with what blocks each.

        The public hook for "why this, and why not that" (the engine API's decision view): the bids
        are priced and timed exactly as `decide` would (the same segment, send hour, weekly count
        with pending sends, gap rule and hard rules), and ranked the way the auction ranks them:
        the bids that may go out first, by `net` (expected net minus the attention price), then the
        blocked ones by expected net. `considered` marks the near-best few the auction samples from.
        Options the bidder's own pruning leaves out are listed too: blocked by the hard rule that
        would stop them, else by `PRUNED`.

        Each bid's `net` is after its own channel's attention price and any pacing surcharge. Bids
        held by pacing (`pacing`) or by the customer's spent weekly budget (`weekly_cap`) come back
        blocked with that code. `considered` includes options restraint keeps out of the Thompson
        draw: the exploration floor can still pick them.

        It changes nothing: no decision is logged, no send is scheduled, no bidder remembers the
        customer, no new brief version is installed and the pacer's running reference is left
        alone, so deciding afterwards gives the same decision. The warm start from history (before
        the first decision) is not done here.
        A holdout or baseline customer's bids all come back blocked (`holdout`, `BASELINE_GROUP`).
        """
        eff, bidders = self._view(ctx)
        seg = self.segment(state, ctx.now)
        update: dict[str, object] = {"segment": seg}
        by_goal = {b.goal_id: b for b in bidders}
        if state.group is not Group.AGENT:
            code = rules.HOLDOUT if state.group is Group.HOLDOUT else BASELINE_GROUP
            view = state.model_copy(update=update)
            held = [b for bidder in bidders for b in _quote(bidder, view, eff)]
            held.sort(key=lambda b: -b.expected_net)
            return [
                BidExplanation(
                    b,
                    b.expected_net,
                    code,
                    False,
                    _item_reason(b, by_goal, view),
                    _moment_reasons(b, by_goal, view, ctx.now),
                )
                for b in held
            ]
        hour = self._send_hour(state.customer_id, seg, eff, trigger)
        if hour is not None:
            update["active_hours"] = tuple(1.0 if h == hour else 0.0 for h in range(24))
            update["contacts_7d"] = self._contacts_at_send(state, eff, hour)
        elif state.active_hours:
            update["active_hours"] = ()
        if self.who is not None:  # read-only: explaining stores nothing (`WhoBook.prepare`)
            update.update(self.who.prepare(state, ctx.now, remember=False))
        view = state.model_copy(update=update)

        policy = self.policy
        decision_id = policy.decision_id(state.customer_id, ctx.now, trigger)
        pending, upcoming = self._pending_view(view, ctx.now, decision_id)
        gap = (
            rules.too_soon(view, ctx.now, policy.min_gap, upcoming)
            if policy.min_gap is not None
            else rules.OK
        )
        if pending:
            view = view.model_copy(
                update={
                    "contacts_7d": view.contacts_7d + pending,
                    "contacts_30d": view.contacts_30d + pending,
                }
            )
        hour_scores: tuple[float, ...] | None = None
        if policy.reading_hours is not None:
            rng_key = state.customer_id if trigger in (None, SWEEP) else f"{state.customer_id}\x1f{trigger}"
            hour_scores = policy.reading_hours.sample_profile(
                state.customer_id, view.segment, seeded_rng(ctx.seed, f"{rng_key}\x1freading")
            )
        times: dict[tuple[str, datetime], datetime | None] = {}
        offered = [
            (policy._timed(b, view, eff, times, hour_scores), kept)
            for bidder in bidders
            for b, kept in _quote_all(bidder, view, eff)
            if math.isfinite(b.expected_net) and math.isfinite(b.uncertainty)
        ]
        if policy.journeys is not None:  # v3 journeys: the options decide() weighs, not the raw bids
            quoted = [b for b, kept in offered if kept]
            offered = [(b, True) for b in policy.journey_options(quoted, view, eff)] + [
                (b, False) for b, kept in offered if not kept
            ]
        allowed: list[Bid] = []
        blocked: list[tuple[Bid, str]] = []
        for bid, kept in offered:
            check = rules.check_bid(bid, view, eff)
            if check.allowed and not gap.allowed:
                check = gap
            if check.allowed and not kept:
                blocked.append((bid, PRUNED))
            elif check.allowed:
                allowed.append(bid)
            else:
                blocked.append((bid, check.code))
        market = policy.auction(view, eff, allowed, [], observe=False)
        blocked.extend(market.held)
        held_ids = {id(b) for b, _code in market.held}
        considered = {id(b) for b in market.options}
        # Allowed bids past the auction's best `max_options` are listed too, as not considered.
        ranked = sorted(
            (b for b in allowed if id(b) not in held_ids),
            key=lambda b: b.expected_net - market.cost(b),
            reverse=True,
        )
        out = [
            BidExplanation(
                b,
                b.expected_net - market.cost(b),
                None,
                id(b) in considered,
                _item_reason(b, by_goal, view),
                _moment_reasons(b, by_goal, view, ctx.now),
            )
            for b in ranked
        ]
        blocked.sort(key=lambda x: -x[0].expected_net)
        out.extend(
            BidExplanation(
                b,
                b.expected_net - market.cost(b),
                code,
                False,
                _item_reason(b, by_goal, view),
                _moment_reasons(b, by_goal, view, ctx.now),
            )
            for b, code in blocked
        )
        return out

    def _view(self, ctx: DecisionContext) -> tuple[DecisionContext, list[Bidder]]:
        """The context and bidders `decide` would use, without installing anything (`_effective`
        installs new brief versions and caches the live briefs; this leaves both alone)."""
        bidders: dict[str, Bidder] = dict(self.bidders)
        resumed: set[str] = set()
        for brief in ctx.briefs:
            if self._is_newer(brief):
                if brief.goal_id in self.briefs:
                    resumed.add(brief.goal_id)
                bidders[brief.goal_id] = self._new_bidder(brief)
        live = self._live_briefs(ctx.briefs, frozenset(resumed))
        if len(live) == len(ctx.briefs) and all(a is b for a, b in zip(live, ctx.briefs, strict=True)):
            return ctx, list(bidders.values())
        return ctx.model_copy(update={"briefs": live}), list(bidders.values())

    def _pending_view(self, state: CustomerState, now: datetime, decision_id: str) -> tuple[int, int]:
        """(sends the policy scheduled that the state does not show yet, those of them still to come
        or due within the gap), leaving out `decision_id`: what `AutonomousPolicy.decide` counts,
        read without dropping anything from its schedule."""
        scheduled = self.policy._scheduled.get(state.customer_id, {})
        folded = state.last_contact_at
        week_ago = now - timedelta(days=7)
        keep = {
            did: at
            for did, at in scheduled.items()
            if did != decision_id and at > week_ago and (folded is None or at > folded)
        }
        gap = self.policy.min_gap
        upcoming = sum(1 for at in keep.values() if gap is not None and at > now - gap)
        return len(keep), upcoming

    def _warm_start(self, states: Mapping[str, CustomerState], now: datetime) -> None:
        """Learn from the client's past campaigns before the first decision, so day 1 is not blind.

        Every delivered logged send feeds the click model: the segment click rates take it weighted
        by the inverse of its logged propensity (`logged_probability`, clipped; weight 1 when the
        campaign did not log one), and the customer's own record takes it as for the agent's sends.
        A click that came hours after its message shows when that customer reads. The same sends
        and the unsubscribes and complaints that followed them warm-start the contact hazard
        (`_warm_hazard`). Segments are today's (as of the first decision): decisions in the history
        were not ours, so the segment each was made in is unknown, and a customer without a state
        here is pooled.

        A channel with `NO_CLICK_MIN_SENDS` delivered past messages and not one click logs no clicks
        (SMS, direct mail): its value is read per message from purchases, not from clicks. Then
        `_calibrate` measures what past randomised campaigns were really worth and sets the lift per
        click (and per message) from that. The contact-harm model, which gathered evidence while the
        history streamed in, is fitted once here.
        """
        history = {cid: list(evs.values()) for cid, evs in self._history.items()}
        self._history = {}
        self._decided = True
        harm = contact_harm_of(self.bundle.response)
        if harm is not None and harm.evidence:
            harm.refit()
        every = logged_sends(history, self.content_by_id, exclude=self._sends)
        sends = [s for s in every if s.delivered]
        model = self.bundle.response
        if sends:
            weights = ipw_weights([s.propensity for s in sends])
            self._warm_hazard(history, sends, weights)
            if isinstance(model, ClickResponseModel):
                segment = {cid: self.segment(st, now) for cid, st in states.items()}
                delivered: dict[str, int] = {}
                clicked: set[str] = set()
                for s in sends:
                    delivered[s.channel.value] = delivered.get(s.channel.value, 0) + 1
                    if s.clicked:
                        clicked.add(s.channel.value)
                model.no_click.update(
                    ch for ch, n in delivered.items() if n >= NO_CLICK_MIN_SENDS and ch not in clicked
                )
                records = [
                    (s.customer_id, segment.get(s.customer_id), s.action_key(), s.clicked, w)
                    for s, w in zip(sends, weights, strict=True)
                ]
                self.history_used += model.warm_start(records)
                for s in sends:
                    if s.click_at is None or s.click_at - s.sent_at < LATE_CLICK:
                        continue
                    st = states.get(s.customer_id)
                    tz = st.timezone if st is not None else self._timezones.get(s.customer_id, "UTC")
                    self.reading.observe(
                        s.customer_id, s.click_at.astimezone(zone_named(tz)).hour, segment.get(s.customer_id)
                    )
                self._calibrate(states, now, every, segment, records)
                self._warm_lookalike(now, every)
                if SEND in self.calibration:
                    # Messages on channels without clicks taught the lift per message instead.
                    self.history_used += sum(1 for s in sends if not model.counts_clicks(s.action_key()))
        if every:
            # Who a message moves: fitted on the latest closed past campaign with the customers'
            # features (`CustomerState.preferences`) and attached to the response model, so every
            # bidder sharing it blends the per-customer score into its bids. No usable campaign, no
            # model: bids are unchanged (the notes say why).
            self.uplift = warm_start_uplift(
                model,
                states,
                self.digest.as_events(history),
                self.content_by_id,
                now,
                horizon_days=float(self.learn_horizon_days),
                decisions=list(self._log.values()),
                exclude=self._sends,
            )
        self.digest = HistoryDigest()
        self._history_holdout = {}

    def _warm_lookalike(self, now: datetime, sends: Sequence[LoggedSend]) -> None:
        """With the look-alike model on, learn from past randomised campaigns too.

        Each arm of a campaign with a randomised control (`randomised_arms`, the same test the
        calibration uses) gives its recipients and its left-out customers as outcomes: did they
        buy in the `learn_horizon_days` after the campaign started? Their profiles are rebuilt from
        what the history shows before that moment (`history_profile`), never from today's states,
        which hold the purchases being predicted. Campaigns without a usable control are skipped.
        """
        model = self.bundle.response
        if not isinstance(model, LookalikeClickModel):
            return
        horizon = timedelta(days=self.learn_horizon_days)
        arms, _notes = randomised_arms(
            sends, self.digest, window=horizon, now=now, holdout=self._history_holdout or None
        )
        digest = self.digest
        rows: list[Outcome] = []
        controlled: set[tuple[str, datetime]] = set()

        def outcome(customer: str, arm_start: datetime, key: str | None) -> Outcome:
            row = digest.purchases.get(customer)
            bought = [(stamp(row[i]), row[i + 1]) for i in range(0, len(row), 3)] if row is not None else []
            first = digest.first_seen.get(customer)
            profile = history_profile(
                customer,
                arm_start,
                first_seen=stamp(first) if first is not None else None,
                purchases=bought,
                sends=[stamp(t) for t in digest.send_times.get(customer, ())],
                segment=lambda st: self.segmenter.label(st, arm_start),
            )
            end = arm_start + horizon
            t0, t1 = arm_start.timestamp(), end.timestamp()
            # Any purchase in the window, whatever its value, as `Timeline.bought` counts it on the
            # live path; refunds are kept with a negative value (a zero one as -0.0) and are not one.
            converted = row is not None and any(
                t0 <= row[i] < t1 and math.copysign(1.0, row[i + 1]) > 0 for i in range(0, len(row), 3)
            )
            return Outcome(customer, profile, key, converted, arm_start, end)

        for arm in arms:
            rows.extend(outcome(c, arm.start, arm.action_key) for c in arm.treated)
            # the control is shared by a campaign's arms: one row per customer
            rows.extend(outcome(c, arm.start, None) for c in arm.control if (c, arm.start) not in controlled)
            controlled.update((c, arm.start) for c in arm.control)
        if rows:
            model.lookalike.observe(rows)
            model.lookalike.fit(now)

    def _objective(self, channel: Channel) -> Objective:
        for brief in self.briefs.values():
            if channel in brief.envelope.channels:
                return brief.objective
        return Objective.GROSS_PROFIT

    def _proof_window(self) -> timedelta:
        days = [b.horizon_days for b in self.briefs.values()]
        return timedelta(days=max(days) if days else 14)

    def _calibrate(
        self,
        states: Mapping[str, CustomerState],
        now: datetime,
        sends: Sequence[LoggedSend],
        segment: Mapping[str, str],
        records: Sequence[tuple[str, str | None, str, bool, float]],
    ) -> None:
        """Set the lift per click (and per message, on channels without clicks) from what past
        randomised campaigns were really worth (`autonomy.learning.calibration`).

        For each arm of a campaign with a randomised control: the extra money per recipient over the
        control (CUPED-adjusted, over the brief's proof window), divided by what the model, at one
        purchase per unit, predicts per recipient (their segment's click rate x their value; their
        value alone on a channel without clicks). That is the lift per unit the campaign showed, with
        its standard error. The arms are pooled by their precision and combined with the prior
        (`LIFT_PER_CLICK_PRIOR`, `LIFT_PER_SEND_PRIOR`) by precision too, so a big campaign sets the
        scale and a small one only nudges it. Arms and segments that differ beyond their noise keep
        part of their own figure (`shrink_to_pool`): an e-mail that draws visits but few extra
        purchases is valued below one that turns its visits into sales.

        Without a randomised control, nothing in the history shows what a click is worth; the
        history's click rate is then trusted to set the scale only part way (`HISTORY_CLICK_TRUST`).
        """
        model = self.bundle.response
        if not isinstance(model, ClickResponseModel):
            return
        arms, notes = randomised_arms(
            sends,
            self.digest,
            window=self._proof_window(),
            now=now,
            holdout=self._history_holdout or None,
        )
        measured: dict[str, list[tuple[str, float, float]]] = {CLICK: [], SEND: []}
        by_segment: dict[str, dict[str, list[tuple[float, float]]]] = {CLICK: {}, SEND: {}}
        report: list[dict[str, object]] = []
        for arm in arms:
            kind = CLICK if model.counts_clicks(arm.action_key) else SEND
            objective = self._objective(arm.channel)
            margin_rate = None if objective is Objective.REVENUE else self.value._fallback_margin()
            t0, t1 = arm.start.timestamp(), arm.end.timestamp()
            treated = [c for c in arm.treated if c in states]
            if len(treated) < MIN_ARM:
                continue
            pred: dict[str, float] = {}
            for c in treated:
                seg = segment.get(c)
                base = 1.0 if kind == SEND else model.click_rate(seg or ANY, arm.action_key).mean
                pred[c] = base * self.value.value_if_converted(states[c], objective)
            y = {c: self.digest.money(c, t0, t1, margin_rate) for c in (*treated, *arm.control)}
            x = {c: self.digest.rate_before(c, t0) for c in (*treated, *arm.control)}
            p_bar = sum(pred.values()) / len(pred)
            diff = difference(
                [y[c] for c in treated],
                [y[c] for c in arm.control],
                [x[c] for c in treated],
                [x[c] for c in arm.control],
            )
            if diff is None or p_bar <= 0:
                continue
            k, se = diff.effect / p_bar, diff.se / p_bar
            measured[kind].append((arm.action_key, k, se))
            report.append(
                {
                    "campaign": arm.campaign,
                    "action": arm.action_key,
                    "unit": kind,
                    "treated": len(treated),
                    "control": len(arm.control),
                    "extra_money_per_recipient": diff.effect,
                    "se": diff.se,
                    "predicted_per_unit": p_bar,
                    "lift_per_unit": k,
                    "lift_se": se,
                }
            )
            control_by: dict[str, list[str]] = {}
            for c in arm.control:
                seg = segment.get(c)
                if seg is not None:
                    control_by.setdefault(seg, []).append(c)
            treated_by: dict[str, list[str]] = {}
            for c in treated:
                seg = segment.get(c)
                if seg is not None:
                    treated_by.setdefault(seg, []).append(c)
            for seg, ts in sorted(treated_by.items()):
                cs = control_by.get(seg, [])
                if len(ts) < MIN_SEGMENT_ARM or len(cs) < MIN_SEGMENT_ARM:
                    continue
                d = difference([y[c] for c in ts], [y[c] for c in cs], [x[c] for c in ts], [x[c] for c in cs])
                p_seg = sum(pred[c] for c in ts) / len(ts)
                if d is not None and p_seg > 0:
                    by_segment[kind].setdefault(seg, []).append((d.effect / p_seg, d.se / p_seg))
        result: dict[str, object] = {"arms": report, "not_used": notes}
        for kind, rows in measured.items():
            pooled = combine((k, se) for _key, k, se in rows)
            if pooled is None:
                continue
            prior = LIFT_PER_CLICK_PRIOR if kind == CLICK else LIFT_PER_SEND_PRIOR
            post = with_prior(prior, pooled)
            ratios: dict[str, float] = {}
            if pooled[0] > 0:
                per_key: dict[str, tuple[float, float]] = {}
                for key in sorted({key for key, _k, _se in rows}):
                    found = combine((k, se) for kk, k, se in rows if kk == key)
                    if found is not None:
                        per_key[key] = found
                for key, (m, sd) in shrink_to_pool(per_key, pooled[0]).items():
                    model.action_scale[key] = (max(m, 0.0) / pooled[0], sd / pooled[0])
                segs = {
                    seg: found
                    for seg, vals in by_segment[kind].items()
                    if (found := combine(vals)) is not None
                }
                if segs:
                    ratios = {
                        seg: max(m, 0.0) / pooled[0]
                        for seg, (m, _sd) in shrink_to_pool(segs, pooled[0]).items()
                    }
            self.lift.history_ratio[kind] = ratios
            by_seg = {seg: (post[0] * r, post[1] * r) for seg, r in ratios.items()}
            if kind == CLICK:
                self.lift.prior = post
                model.lift, model.lift_by_segment = post, by_seg
            else:
                self.lift.send_prior = post
                model.send_lift, model.send_lift_by_segment = post, by_seg
            result[kind] = {"measured": pooled, "posterior": post, "segments": ratios}
        if not measured[CLICK]:
            # No control: the history's clicks set the scale of uplift only part way.
            clicks = [(r[3], r[4]) for r in records if model.counts_clicks(r[2])]
            if clicks:
                rate, _n = weighted_rate([c for c, _w in clicks], [w for _c, w in clicks])
                prior_rate = model.click_prior.mean
                if rate > prior_rate > 0:
                    factor = (prior_rate / rate) ** (1.0 - HISTORY_CLICK_TRUST)
                    k0, sd0 = self.lift.prior
                    self.lift.prior = (k0 * factor, sd0 * factor)
                    model.lift = self.lift.prior
                    result["no_control"] = {"history_click_rate": rate, "factor": factor}
        self.calibration = result

    def _warm_hazard(
        self, history: Mapping[str, Sequence[Event]], sends: Sequence[LoggedSend], weights: Sequence[float]
    ) -> None:
        """Teach the contact hazard from past campaigns: every delivered past send, by its channel and
        the messages the customer had in the week before it, and every unsubscribe or complaint that
        followed one. An event is tied to its send by decision id when the history has one, else to
        the latest delivered send on the same channel within `HISTORY_LOSS_WINDOW` before it. Each
        send counts with its inverse-propensity weight, as for the click rates, so a campaign aimed
        at the people least likely to leave does not make leaving look rare (the weights are scaled to
        average 1, so the history counts for as many sends as it holds). The plain counts of messages
        and complaints per channel then go to the breaker's `check_history`."""
        price = self.contact_price
        sent_times: dict[str, list[datetime]] = {}
        for cid, events in history.items():
            sent_times[cid] = sorted(e.ts for e in events if e.type is EventType.SEND)
        rows: list[tuple[LoggedSend, float, int]] = []
        # Weights rescaled to average 1: the evidence counts as many sends as there were (the prior
        # strengths are in events), while the campaigns' targeting is still undone.
        mean_w = sum(weights) / len(weights) if weights else 1.0
        for s, w in zip(sends, (w / mean_w for w in weights), strict=True):
            week_ago = s.sent_at - timedelta(days=7)
            before = sum(1 for t in sent_times.get(s.customer_id, ()) if week_ago < t < s.sent_at)
            rows.append((s, w, before))
            price.hazard.observe_send(s.channel, before, w)
            price.complaints.observe_send(s.channel, before, w)
        by_decision = {s.decision_id: i for i, (s, _w, _b) in enumerate(rows) if s.decision_id}
        by_customer: dict[str, list[int]] = {}
        for i, (s, _w, _b) in enumerate(rows):
            by_customer.setdefault(s.customer_id, []).append(i)
        lost: set[int] = set()
        complained: set[int] = set()
        delivered: dict[Channel, int] = {}
        for s in sends:
            delivered[s.channel] = delivered.get(s.channel, 0) + 1
        for cid, events in sorted(history.items()):
            for e in sorted(events, key=lambda e: (e.ts, e.event_id)):
                if e.type not in (EventType.UNSUBSCRIBE, EventType.COMPLAINT):
                    continue
                found = by_decision.get(e.decision_id) if e.decision_id else None
                if found is None:
                    candidates = [
                        i
                        for i in by_customer.get(cid, ())
                        if rows[i][0].channel is e.channel
                        and rows[i][0].sent_at <= e.ts <= rows[i][0].sent_at + HISTORY_LOSS_WINDOW
                    ]
                    found = max(candidates, key=lambda i: rows[i][0].sent_at) if candidates else None
                if found is None:
                    continue
                s, w, before = rows[found]
                if found not in lost:
                    lost.add(found)
                    price.hazard.observe_event(s.channel, before, w)
                if e.type is EventType.COMPLAINT and found not in complained:
                    complained.add(found)
                    price.complaints.observe_event(s.channel, before, w)
        counts: dict[Channel, tuple[int, int]] = {ch: (n, 0) for ch, n in delivered.items()}
        for i in complained:
            ch = rows[i][0].channel
            counts[ch] = (counts[ch][0], counts[ch][1] + 1)
        self.alerts.extend(self.breaker.check_history(self.briefs.values(), counts))

    def _contacts_at_send(self, state: CustomerState, ctx: DecisionContext, hour: int) -> int:
        """Messages the customer will have had in the 7 days before the planned send.

        The weekly cap is about any 7 days, and the state counts the 7 days before the decision. A
        message decided at 10:00 for 20:00 is judged on the week before 20:00: a send from eight
        days ago at 15:00 has dropped out by then. Counting from the decision instead holds every
        customer whose last send was later in the day than the morning sweep for an extra day, every
        week (about 7% of the messages the cap allows). Sends scheduled but not yet folded into
        the state are added by the policy (`AutonomousPolicy.with_pending`), so this never lets a
        third message through.
        """
        if not state.recent_contacts:
            return state.contacts_7d
        # The policy sends at the first moment in the next day when the customer's clock shows
        # `hour` (now, if it already does); `hour` is outside quiet hours by construction. Stepped
        # in absolute hours, as `autonomy.decide.timing.candidate_times` does, so a day when the
        # clocks change lands on the same moment the policy picks.
        zone = zone_named(state.timezone)
        top = ctx.now.astimezone(zone).replace(minute=0, second=0, microsecond=0).astimezone(UTC)
        send_at = ctx.now
        for step in range(25):
            at = max(ctx.now, top + timedelta(hours=step))
            if at.astimezone(zone).hour == hour:
                send_at = at
                break
        week_before = send_at - timedelta(days=7)
        return sum(1 for t in state.recent_contacts if week_before < t <= send_at)

    def _send_hour(
        self, customer_id: str, segment: str, ctx: DecisionContext, trigger: str | None
    ) -> int | None:
        """The local hour to aim for.

        1. The customer's own reading hour, when late clicks show it.
        2. Else, for a customer with quick clicks only (and `hour_model` on), the best allowed hour
           of `ReadingHourModel`'s posterior mean.
        3. Else the allowed hour closest, on average, to when people in their segment read
           (`ReadingHours.closest_hour`; each known customer counts once, the segment shrunk towards
           the business). Before anyone's hour is known, the business default.
        A small share (`HOUR_EXPLORE`) of messages to customers without a known hour goes to a random
        allowed hour, so every hour keeps being tried and late clicks keep arriving.
        """
        allowed = [h for h in range(24) if not any(rules.is_quiet_hour(h, b.envelope) for b in ctx.briefs)]
        if not allowed:
            return None
        own = self.reading.profile(customer_id)
        if own is not None:
            best = max(allowed, key=lambda h: own[h])
            if own[best] > 0:
                return best
        default = min(allowed, key=lambda h: ((h - self.policy.default_hour) % 24, h))
        key = f"{customer_id}\x1fhour" if trigger in (None, SWEEP) else f"{customer_id}\x1fhour\x1f{trigger}"
        rng = seeded_rng(ctx.seed, key)
        if rng.random() < HOUR_EXPLORE:
            return allowed[int(rng.integers(len(allowed)))]
        if self.hour_model is not None and self.hour_model.clicks(customer_id) > 0:
            # Some quick clicks but no late one yet: the best allowed hour of their reading-hour
            # posterior (their quick clicks credit the hours we sent at, shrunk to the segment's).
            return self.hour_model.best_hour(customer_id, segment, allowed)
        closest = self.reading.closest_hour(segment, allowed)
        return default if closest is None else closest

    # --- learning -------------------------------------------------------------------------------

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        """Closed rewards (the `Policy` hook) into the shared models, skipping what is learned.

        A journey's later step is left out, as in `_keep`: it went to someone who did not buy after the step
        before it, so it is not a fresh draw for the response models."""
        rewards = [r for r in rewards if not _later_step(decisions.get(r.decision_id))]
        if not rewards:
            return
        learn_from_rewards(
            self.bundle,
            rewards,
            decisions,
            self.content_by_id,
            horizon_days=self.learn_horizon_days,
            segment_of=self._segment_at(decisions.values(), None),
        )

    def nightly(
        self,
        decisions: Iterable[Decision],
        events_by_customer: Mapping[str, Sequence[Event]],
        now: datetime,
    ) -> list[str]:
        """The nightly loop: remember new decisions and events, learn, and check the stop rules.

        `decisions` and `events_by_customer` may be just what is new since the last call or the
        whole log: decisions and events are kept by id, so nothing is counted twice. Returns plain
        sentences for anything the breaker paused tonight (also kept in `alerts`).
        """
        decisions = list(decisions)
        for d in decisions:
            self._keep(d)
        if self.who is not None:
            self.who.ingest(events_by_customer, now)
        journeys = self.policy.journeys
        if journeys is not None and not self._decided and not self._journeys_replayed:
            # A policy built after a restart or a brief change has no runs; the log has them (JOURNEYS.md).
            running = [d for d in decisions if d.journey_id is not None or d.journey_ended is not None]
            if running:
                journeys.rebuild(running)
                self._journeys_replayed = True
        if not self._decided:
            # Before the first decision everything handed over is history: past campaigns' sends
            # and clicks are learned from at the first decision (they need segments, see _warm_start).
            # Only their messages are kept whole; purchases and consent go to the compact digest, so
            # the history can be streamed in, a slice of customers per call.
            for d in decisions:
                if d.kind is DecisionKind.NOTHING and d.group is Group.HOLDOUT:
                    self._history_holdout.setdefault(d.customer_id, d.made_at)
            for cid, events in events_by_customer.items():
                kept: dict[str, Event] | None = None
                for e in events:
                    if e.ts > now:
                        continue
                    self.digest.add(e)
                    if e.type in _HISTORY_KEEP:
                        if kept is None:
                            kept = self._history.setdefault(cid, {})
                        kept.setdefault(e.event_id, e)
        forgotten = self._forgotten_before
        for cid, events in events_by_customer.items():
            for e in events:
                if e.event_id in self._seen_events or e.ts > now:
                    continue
                if forgotten is not None and e.ts < forgotten:
                    continue  # seen once already, then pruned: a whole-log caller must not recount it
                self._seen_events.add(e.event_id)
                self._events.setdefault(cid, []).append(e)
                self._observe(e)
        if not self._decided and self.digest.data_start is not None:
            # The business's records begin at its first purchase on file across the whole history,
            # however many calls it came in.
            first = datetime.fromtimestamp(self.digest.data_start, UTC)
            if self.segmenter.data_start is None or first < self.segmenter.data_start:
                self.segmenter.data_start = first
        elif self.segmenter.data_start is None:
            purchases = [
                e.ts for evs in events_by_customer.values() for e in evs if e.type is EventType.PURCHASE
            ]
            if purchases:
                self.segmenter.data_start = min(purchases)

        log = list(self._log.values())
        # Sorted so the learner sees customers in the same order in every process (set order of
        # strings changes with the hash seed, and float sums with the order they are added in).
        mapping: dict[str, Sequence[Event]] = {
            cid: self._events.get(cid, []) for cid in sorted({d.customer_id for d in log})
        }
        for cid in sorted(events_by_customer):
            mapping.setdefault(cid, self._events.get(cid, []))
        if log or mapping:
            update_models(
                self.bundle,
                log,
                mapping,
                self.content_by_id,
                now,
                self.learn_horizon_days,
                segment_of=self._segment_at(log, now),
                # Who contact puts off is read against the people we never contact: the holdout
                # since the agent's first decision is the honest organic baseline.
                harm_holdout=sorted(c for c, g in self.lift.group.items() if g is Group.HOLDOUT),
                harm_split_at=self.lift.start,
                harm_data_start=self.segmenter.data_start,
                # While the history streams in, the harm model gathers evidence; it is fitted once at
                # the warm start, not once per slice.
                refit_harm=self._decided,
            )
        model = self.bundle.response
        if isinstance(model, ClickResponseModel):
            expected = self._expected_by_kind
            total = expected[CLICK] + expected[SEND]
            click_share = expected[CLICK] / total if total > 0 else 1.0
            send_share = expected[SEND] / total if total > 0 else 1.0
            model.lift = self.lift.posterior(now, share=click_share)
            model.lift_by_segment = self.lift.by_segment(now, share=click_share)
            model.send_lift = self.lift.posterior(now, kind=SEND, share=send_share)
            model.send_lift_by_segment = self.lift.by_segment(now, kind=SEND, share=send_share)
            if self._refit_at is None or now - self._refit_at >= timedelta(days=PERSONAL_REFIT_DAYS):
                model.people.refit()
                self._refit_at = now
        if isinstance(model, LookalikeClickModel) and self.outcomes is not None and self._decided:
            # Windows that closed by tonight, on the profiles they were decided on: the crowd's model
            # learns from them, and refits when the data has grown.
            seen_by = timelines(self._events)
            none = Timeline(())  # a customer with no events in memory bought nothing and was sent nothing
            model.lookalike.observe(self.outcomes.close(now, lambda cid: seen_by(cid) or none))
            model.lookalike.fit(now)
        if self._decided:
            for tired in (self.contact_price.hazard, self.contact_price.complaints):
                if isinstance(tired, LookalikeHazard):
                    tired.fit()
        self._judge_lift(now)
        learned = self.bundle.learned_decisions
        self._log = {k: d for k, d in self._log.items() if k not in learned}
        self._decision_segment = {k: v for k, v in self._decision_segment.items() if k not in learned}
        self._prune(now)
        news = self.breaker.check(self.briefs.values(), now)
        if self.pacer is not None:
            for goal in self.briefs:
                self.pacer.set_unsubscribe_baseline(goal, self.breaker.unsub_baseline.get(goal))
        if self.offer_pricing is not None and self._decided:
            self._roll_offer_prices(now)
        self.alerts.extend(news)
        return news

    # --- the budget price for offers (v3, opt-in) -----------------------------------------------

    def _offer_pricing_hooks(self, brief: Brief) -> dict[str, Any]:
        """The bidder's v3 offer hooks for this brief: none unless `offer_pricing` is on."""
        pricing = self.offer_pricing
        if pricing is None:
            return {}
        price = self.offer_prices.get(brief.goal_id)
        if price is None:
            price = self.offer_prices[brief.goal_id] = pricing.price_for(brief.envelope.budget)
        else:
            price.budget = brief.envelope.budget  # a new brief version may move the budget
        hooks: dict[str, Any] = {"budget_price": price}
        if pricing.default_basket is not None:
            hooks["offer_cost_fn"] = basket_cost_fn(pricing.default_basket)
        return hooks

    def _price_send(self, d: Decision) -> None:
        """Count an agent send's channel cost against its goal, and remember which goal its offer is
        on, so a later redemption is charged to that goal."""
        if d.kind is not DecisionKind.ACT or d.action is None or d.group is not Group.AGENT:
            return
        if d.decision_id in self._priced_sends:
            return
        self._priced_sends.add(d.decision_id)
        goal = d.action.goal_id
        bidder = self.bidders.get(goal)
        if bidder is not None:
            cost = bidder.costs.get(d.action.channel, 0.0) + bidder.format_costs.get(d.action.format, 0.0)
            self._channel_spend[goal] = self._channel_spend.get(goal, 0.0) + cost
        if d.action.offer_id != NO_OFFER.offer_id:
            self._offer_goal[(d.customer_id, d.action.offer_id)] = goal

    def _price_redemption(self, e: Event) -> None:
        """Charge a purchase that used one of our offers to the goal that sent it: the cost the shop
        reported (`attrs["offer_cost"]`), else the offer's own `cost_per_redemption`."""
        offer_id = e.attrs.get("offer_id")
        if not isinstance(offer_id, str) or offer_id == NO_OFFER.offer_id:
            return
        goal = self._offer_goal.get((e.customer_id, offer_id))
        if goal is None:
            return
        reported = e.attrs.get("offer_cost")
        if isinstance(reported, int | float) and not isinstance(reported, bool):
            cost = max(float(reported), 0.0)
        else:
            brief = self.briefs.get(goal)
            found = (
                next((o for o in brief.envelope.offers if o.offer_id == offer_id), None) if brief else None
            )
            cost = found.cost_per_redemption if found is not None else 0.0
        self._offer_spend[goal] = self._offer_spend.get(goal, 0.0) + cost

    def _roll_offer_prices(self, now: datetime) -> None:
        """Close the day for every goal's budget price, once per calendar day (UTC) of `now`."""
        day = now.astimezone(UTC).date()
        if self._priced_day == day:
            return
        self._priced_day = day
        renew = self.offer_pricing is not None and self.offer_pricing.renew
        for goal, price in self.offer_prices.items():
            price.update(self._offer_spend.pop(goal, 0.0), other_spend=self._channel_spend.pop(goal, 0.0))
            if renew and price.days_left == 0 and price.remaining > 0:
                price.extend(self.offer_pricing.days if self.offer_pricing is not None else 1)

    def _segment_at(self, decisions: Iterable[Decision], now: datetime | None) -> Callable[[str], str | None]:
        """A customer -> segment lookup that gives, for the decisions closing now, the segment the
        customer had when the decision was made (the learner asks by customer, not by decision)."""
        horizon = timedelta(days=self.learn_horizon_days)
        at: dict[str, str] = {}
        for d in sorted(decisions, key=lambda d: d.made_at):
            seg = self._decision_segment.get(d.decision_id)
            if seg is None:
                continue
            start = d.action.send_at if d.action is not None else d.made_at
            if now is None or start + horizon <= now:
                at[d.customer_id] = seg
        current = self._segments

        def lookup(customer_id: str) -> str | None:
            return at.get(customer_id, current.get(customer_id))

        return lookup

    def _keep(self, d: Decision) -> None:
        """Keep the decisions the learner can use: sends, and the holdout's "do nothing"."""
        if d.decision_id in self.bundle.learned_decisions:
            return
        if d.kind is DecisionKind.ACT and d.action is not None:
            # A journey's later step is sent to someone who did not buy after the step before it, so it is
            # not a fresh draw: the breaker, the tiredness curves and the pacer count it (it is a message),
            # the response models and the look-alike outcomes do not learn from it as a treatment.
            follow_up = d.action.follow_up_step > 0
            if not follow_up:
                self._log[d.decision_id] = d
            if d.group is Group.AGENT:
                new = d.decision_id not in self._sends
                self._sends[d.decision_id] = (d.action.goal_id, d.action.channel, d.action.send_at)
                item = None if follow_up else self.content_by_id.get(d.action.content_id or "")
                if item is not None:
                    seg = self._decision_segment.get(d.decision_id)
                    key = key_of(d.action.channel, d.action.offer_id, item.theme.value, item.format)
                    self._send_keys[d.decision_id] = (seg, key, d.action.offer_id)
                    model = self.bundle.response
                    if new and isinstance(model, ClickResponseModel):
                        chosen = next((b for b in d.bids if b.action == d.action), None)
                        if chosen is not None:
                            kind = CLICK if model.counts_clicks(key) else SEND
                            self._expected_by_kind[kind] += max(chosen.uplift, 0.0)
        elif d.group is Group.HOLDOUT:
            self._log[d.decision_id] = d
        if self.offer_pricing is not None:
            self._price_send(d)

    def _observe(self, e: Event) -> None:
        """Feed one event to the lift estimate, the breaker, the click model and the reading hours."""
        if e.type is EventType.PURCHASE:
            self.lift.purchase(e.customer_id, e.ts)
            if self.offer_pricing is not None:
                self._price_redemption(e)
        if e.decision_id is None:
            return
        sent = self._sends.get(e.decision_id)
        if sent is None:
            return
        goal, channel, send_at = sent
        self.breaker.record(e, goal, channel)
        if self.pacer is not None:
            self.pacer.record(goal, channel, e.type, e.ts)
        model = self.bundle.response
        keyed = self._send_keys.get(e.decision_id)
        seg = keyed[0] if keyed is not None else self._decision_segment.get(e.decision_id)
        first_click = e.type is EventType.CLICK and e.decision_id not in self._clicked
        if first_click:
            self._clicked.add(e.decision_id)
            self.lift.click(send_at, e.customer_id)
        if e.type is EventType.DELIVER and e.decision_id not in self._send_contacts:
            before = self._sends_before(e.customer_id, send_at)
            local = self._local_hour(e.customer_id, send_at)
            self._send_contacts[e.decision_id] = (before, local)
            price = self.contact_price
            price.hazard.observe_send(channel, before)
            price.complaints.observe_send(channel, before)
            self._tolerance_send(e.decision_id, channel, before)
            if self.hour_model is not None:
                self.hour_model.observe_send(
                    e.customer_id, send_at, self._timezones.get(e.customer_id, "UTC"), seg
                )
            if keyed is not None and isinstance(model, ClickResponseModel):
                if model.counts_clicks(keyed[1]):
                    model.observe_send(e.customer_id, keyed[0], keyed[1], keyed[2], before, local)
                else:
                    # No clicks on this channel: the delivered message is the unit its lift is per.
                    self.lift.click(send_at, e.customer_id, kind=SEND)
        elif e.type in (EventType.UNSUBSCRIBE, EventType.COMPLAINT):
            # One lost contact per message, however many events it caused; complaints apart too.
            contacts = self._send_contacts.get(
                e.decision_id, (self._sends_before(e.customer_id, send_at), None)
            )[0]
            if e.decision_id not in self._lost:
                self._lost.add(e.decision_id)
                self.contact_price.hazard.observe_event(channel, contacts)
                self._tolerance_loss(e.decision_id, complaint=False)
            if e.type is EventType.COMPLAINT and e.decision_id not in self._complained:
                self._complained.add(e.decision_id)
                self.contact_price.complaints.observe_event(channel, contacts)
                self._tolerance_loss(e.decision_id, complaint=True)
        elif first_click:
            if keyed is not None and isinstance(model, ClickResponseModel):
                prior_sends, send_hour = self._send_contacts.get(e.decision_id, (0, None))
                model.observe_click(e.customer_id, keyed[0], keyed[1], keyed[2], prior_sends, send_hour)
            if self.hour_model is not None:
                self.hour_model.observe_click(
                    e.customer_id, send_at, e.ts, self._timezones.get(e.customer_id, "UTC"), seg
                )
        if e.type is EventType.CLICK and e.ts - send_at >= LATE_CLICK:
            hour = self._local_hour(e.customer_id, e.ts)
            self.reading.observe(e.customer_id, hour, seg)

    def _urgency_fade(self, state: CustomerState, goal_id: str, now: datetime, days: float) -> float | None:
        """v3 frequency plans: the share of a goal's urgency now still there `days` later, from its
        bidder (a cart reminder halves every day, a moment closes); None for an unknown goal."""
        bidder = self.bidders.get(goal_id)
        if bidder is None:
            return None
        today = bidder.urgency_on(state, now)
        if today <= 0:
            return 0.0
        return bidder.urgency_on(state, now + timedelta(days=days)) / today

    def _tolerance_send(self, decision_id: str, channel: Channel, before: int) -> None:
        """A delivered send goes to the look-alike hazard models with the profile it was decided on
        (a send decided before this process started has none and only counts in the plain curve)."""
        noted = self._tired_profiles.pop(decision_id, None)
        if noted is None:
            return
        customer_id, profile = noted
        for model in (self.contact_price.hazard, self.contact_price.complaints):
            if isinstance(model, LookalikeHazard):
                model.record_send(decision_id, customer_id, profile, channel, before)

    def _tolerance_loss(self, decision_id: str, *, complaint: bool) -> None:
        price = self.contact_price
        model = price.complaints if complaint else price.hazard
        if isinstance(model, LookalikeHazard):
            model.record_loss(decision_id)

    def _local_hour(self, customer_id: str, at: datetime) -> int:
        return at.astimezone(zone_named(self._timezones.get(customer_id, "UTC"))).hour

    def _sends_before(self, customer_id: str, at: datetime) -> int:
        """SEND events to the customer in the week before `at` (not counting one at `at` itself).

        The window is the weekly cap's (`_contacts_at_send`): a send exactly seven days earlier has
        dropped out. Counted the same way as the count the price is read at, so the hazard and the
        fatigue factor learned for "one message this week" are the ones charged for it."""
        week_ago = at - timedelta(days=7)
        return sum(
            1 for e in self._events.get(customer_id, ()) if e.type is EventType.SEND and week_ago < e.ts < at
        )

    def _prune(self, now: datetime) -> None:
        keep_from = now - timedelta(days=self.learn_horizon_days) - _EVENT_MEMORY_EXTRA
        if self._forgotten_before is None or keep_from > self._forgotten_before:
            self._forgotten_before = keep_from
        for cid in list(self._events):
            kept = [e for e in self._events[cid] if e.ts >= keep_from]
            if kept:
                self._events[cid] = kept
            else:
                del self._events[cid]
        self._seen_events = {e.event_id for evs in self._events.values() for e in evs}
        self._sends = {k: v for k, v in self._sends.items() if v[2] >= keep_from}
        self._send_keys = {k: v for k, v in self._send_keys.items() if k in self._sends}
        self._tired_profiles = {k: v for k, v in self._tired_profiles.items() if k in self._sends}
        self._clicked = {k for k in self._clicked if k in self._sends}
        self._send_contacts = {k: v for k, v in self._send_contacts.items() if k in self._sends}
        self._lost = {k for k in self._lost if k in self._sends}
        self._complained = {k for k in self._complained if k in self._sends}


def build_agent_policy(
    briefs: Sequence[Brief],
    content: Sequence[ContentItem],
    *,
    bundle: ModelBundle | None = None,
    costs: Mapping[Channel, float] | None = None,
    exploration_floor: float | None = None,
    baseline_policy: Policy | None = None,
    kinds: Mapping[str, GoalKind] | None = None,
    learn_horizon_days: int = DEFAULT_LEARN_HORIZON_DAYS,
    item_lookup: ItemLookup | None = None,
    recommender: ItemLookup | None = None,
    asset_of: AssetOf | None = None,
    carousel_template: bool = False,
    format_costs: Mapping[Format, float] | None = None,
    pacing: bool = False,
    reading_hour_model: bool = True,
    restraint: bool = True,
    weekly_budget: bool = True,
    currency: str = "$",
    offer_pricing: OfferPricing | None = None,
    moments: MomentBook | Literal[False] | None = None,
    lookalike: bool | LookalikeConfig | None = None,
    who: bool | WhoConfig | WhoBook | None = None,
    frequency: bool | FrequencyConfig | None = None,
    journeys: bool | JourneyConfig | JourneyRunner | None = None,
    v3: bool | V3Preset = False,
) -> AgentPolicy:
    """The agent for these briefs and this content library, ready to decide and to learn.

    `bundle` shares models already learned (or seeded from priors); None builds a fresh one with
    `autonomy.learning.build_bundle()` whose response model is a `ClickResponseModel`. `costs`
    overrides the per-message channel costs, `kinds` gives each goal's kind (cart recovery fades
    fast), `exploration_floor` overrides the envelopes' floor and `baseline_policy` decides for the
    baseline group. `item_lookup` (refill picks) and `recommender` (complements) name the products
    actions are about (None: the agent's own `set_items` mappings); `asset_of` names the approved
    asset behind an image variant (`autonomy.decide.bidders.asset_lookup`), `carousel_template` says
    the brand has WhatsApp carousel templates and `format_costs` adds a per-message cost by format.

    Three parts that make contacting rarer and better timed are on by default and can be switched
    off one at a time (to measure each): `reading_hour_model` (the reading-hour posterior for
    customers with clicks but no known hour), `restraint` (the lower-confidence-bound rule where
    lift is unproven) and `weekly_budget` (each customer's own weekly rhythm). `pacing` (slow a
    channel before a stop rule trips, `autonomy.decide.pacing`) is off by default: on the Gym it
    changed nothing where no stop rule is near and kept the breaker from stopping a complaint storm.
    See docs/v2/CONTACT_LESS.md. `currency` is how money is written in each decision's reason (the
    shop's own: "₽", "EUR "...). `offer_pricing` (v3, off by default) charges each goal's offers a
    budget price that the nightly loop moves day by day (docs/v3/OFFERS.md).
    `moments` (plan-v3 decision 2, opt-in) is the shop's `autonomy.decide.moments.MomentBook`: the
    bidders raise a message's worth while a customer's moments are live. None leaves it to
    `use_moments` (off unless switched on), False keeps it off.

    `lookalike` (off by default, so every number above is unchanged) puts the look-alike model on top
    of the click model (`LookalikeClickModel`, docs/v3/LOOKALIKE.md); pass a `LookalikeConfig` to
    change its numbers. With a `bundle` given, its response model must already be a
    `LookalikeClickModel` (built with its own config; a `LookalikeConfig` here is then refused).

    `who` (plan-v3 decision 1, off by default) switches on the leaving-risk and 90-day-value models
    (their scores go to each decision's state once a held-out check passes), the bidders' use of them,
    and the trait-pooled leave-alone (`autonomy.decide.who`, docs/v3/WHO.md). Pass a `WhoConfig` to
    change its numbers or a `WhoBook` to share one.

    `frequency` (plan-v3 decision 7, off by default, so every number above is unchanged) plans weeks
    instead of messages (docs/v3/FREQUENCY.md): the tiredness models learn from look-alikes, and each
    customer's best 3-week contact plan decides whether today is a day to wait. Pass a
    `FrequencyConfig` to change its numbers or to switch one of its two parts off.

    `journeys` (plan-v3 decision 4, off by default) lets the auction choose a short plan of messages
    over several channels (docs/v3/JOURNEYS.md): True is the shipped `configs/v3/journeys.yaml`, or pass
    a `JourneyConfig` or a `JourneyRunner`. Later steps are priced at the urgency left on their day.

    `v3` (off by default, so every number above is unchanged) turns on every v3 piece together
    (`autonomy.runtime.v3.V3Preset`, docs/v3/PRESET.md): look-alike, who, frequency, the shipped
    moments, the shipped journeys, the budget price for offers and the experiments' wrapper with no
    experiment running (`agent.experiment_policy.book`). Pass a `V3Preset` to switch a part off. A piece
    also passed by name here wins over the preset's, False included (`journeys=False`, `moments=False`, a
    `journeys=` runner...); `lookalike`, `who`, `frequency` and `journeys` default to None, "not given",
    which is off without the preset. Offer pricing is switched off only with `V3Preset(offers=False)`.
    """
    parts = resolve_v3(
        v3,
        lookalike=lookalike,
        who=who,
        frequency=frequency,
        moments=moments,
        journeys=journeys,
        offer_pricing=offer_pricing,
    )
    lookalike, who, frequency = parts.lookalike, parts.who, parts.frequency
    moments, offer_pricing = parts.moments, parts.offer_pricing
    book = (
        who
        if isinstance(who, WhoBook)
        else WhoBook(who if isinstance(who, WhoConfig) else None) if who else None
    )
    if bundle is None:
        fresh = build_bundle()
        if not lookalike:
            response: ClickResponseModel = ClickResponseModel()
        else:
            response = LookalikeClickModel(
                config=lookalike if isinstance(lookalike, LookalikeConfig) else None
            )
        bundle = ModelBundle(response=response, value=fresh.value, fatigue=fresh.fatigue)
    elif lookalike and not isinstance(bundle.response, LookalikeClickModel):
        raise ValueError("lookalike needs a bundle whose response model is a LookalikeClickModel")
    elif isinstance(lookalike, LookalikeConfig):
        raise ValueError("with a bundle given, pass the LookalikeConfig to its LookalikeClickModel instead")
    return AgentPolicy(
        briefs,
        content,
        bundle,
        costs=costs,
        exploration_floor=exploration_floor,
        baseline_policy=baseline_policy,
        kinds=kinds,
        learn_horizon_days=learn_horizon_days,
        item_lookup=item_lookup,
        recommender=recommender,
        asset_of=asset_of,
        carousel_template=carousel_template,
        format_costs=format_costs,
        pacing=pacing,
        reading_hour_model=reading_hour_model,
        restraint=restraint,
        weekly_budget=weekly_budget,
        currency=currency,
        offer_pricing=offer_pricing,
        moments=moments,
        who=book,
        frequency=(
            (frequency if isinstance(frequency, FrequencyConfig) else FrequencyConfig())
            if frequency
            else None
        ),
        journeys=parts.journeys,
        experiments=parts.experiments,
    )
