"""The agent: Autonomy's own policy, put together the way the runtime and the Gym run it.

`build_agent_policy(briefs, content)` returns an `AgentPolicy`: one `GoalBidder` per approved brief,
all sharing one `ModelBundle` (response, value and fatigue models, from `autonomy.learning`), inside
`AutonomousPolicy` (bids, hard rules, send time, attention price, Thompson sampling with a floor).
Around `decide()` it adds what a policy that runs for months needs. Pure maths, no LLM, no clock.

How a message is valued (see `ClickResponseModel`):

    uplift = lift_per_click(segment) x click_rate(segment, action) x personal(customer, action) x fatigue(week)
    net    = uplift x value - offer cost x (organic chance of buying + uplift) - channel cost

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
  covariate, per segment and shrunk towards the business-wide figure (`shrink_to_pool`).
* **Organic chance** (`OrganicRate`): the customer's own purchase rate, shrunk to their segment's.
  It is what makes a discount expensive for someone who would have bought anyway.
* **Value** (`ShrunkValueModel`): one order's margin, the customer's own average shrunk towards
  the business's, so a single $6 order does not make someone worthless.
* **Attention price** (`AttentionPrice`): the learned chance of an unsubscribe times the value of
  the messages we could no longer send them.

What it does around the auction:

* **History.** Sends, deliveries and clicks handed over before the first decision are the client's
  past campaigns (`autonomy.learning.observations.logged_sends`). They warm-start the click rates
  (weighted by the inverse of each send's `logged_probability`), each customer's own record and
  their reading hours, so day 1 is not blind (`_warm_start`).
* **Send hour.** A click that comes hours after the message shows when the person reads; their
  own reading hour is used once known. Otherwise the allowed hour closest, on average, to when
  people in their segment read (`ReadingHours.closest_hour`), with a small share sent at random
  hours so every hour keeps being tried.
* **Pacing.** Pending sends count towards the weekly cap, and the cap is judged on the week before
  the planned send, not before the morning decision (`_contacts_at_send`). Nothing new goes out
  within `MIN_GAP` of the last message (so each one is decided knowing whether the last caused an
  unsubscribe). Options that could not beat doing nothing even at the optimistic end are not
  explored (`PLAUSIBLE_Z`), nor are those that could not be the customer's best (`PRUNE_Z`).
* **Briefs move.** A newer brief version rebuilds that goal's bidder (the models are shared).
* **Nightly.** `nightly(decisions, events_by_customer, now)` feeds the clicks and purchases above,
  then hands the log to `autonomy.learning.update_models` (window conversions, fatigue, value).
  Decisions do not carry the customer's segment, so the agent remembers the segment each decision
  was made in: labelling an old window with today's segment would leak its outcome.
* **The breaker.** The brief's stop rules, judged nightly on the agent's own sends of the last week
  (`Breaker`). A complaint or unsubscribe rule trips only on a count that would happen less than
  once in 10,000 weeks at the limit; a paused goal stays paused until a person approves a new brief
  version.

Nothing here reads the Gym's truth: everything learned comes from events a real deployment sees.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from autonomy.contracts import (
    Brief,
    Channel,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    GoalKind,
    Group,
    Reward,
    StopMetric,
)
from autonomy.decide import rules
from autonomy.decide.bidders import GoalBidder
from autonomy.decide.models import (
    ANY,
    Beta,
    BetaResponseModel,
    FatigueModel,
    PersonalEffects,
    ValueModel,
    cuped_adjust,
    product_moments,
    shrink_to_pool,
)
from autonomy.decide.models import action_key as key_of
from autonomy.decide.policy import SWEEP, AutonomousPolicy, seeded_rng
from autonomy.decide.timing import LATE_CLICK, zone_named
from autonomy.interfaces import Policy
from autonomy.learning import ModelBundle, build_bundle, learn_from_rewards, update_models
from autonomy.learning.observations import ipw_weights, logged_sends, weighted_rate

AGENT_VERSION = "autonomy-agent-v1"

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

_EVENT_MEMORY_EXTRA = timedelta(days=9)
"""Events are kept for the learning window plus this: the week of sends before a send (fatigue)
plus a day for sends timed after their decision."""


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
LIFT_PER_CLICK_PRIOR = (0.25, 0.25)
"""Before purchases say otherwise: a click goes with a quarter of an extra purchase, give or take a
quarter. Weak on purpose: the agent-vs-holdout comparison takes over within weeks."""
LIFT_PER_CLICK_STRENGTH = 20.0
"""The prior above is worth this many clicks."""
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
      that action (worth `CLICK_POOL` sends), itself shrunk towards `CLICK_PRIOR`.
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
    """

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
        segment rate as the expected clicks, as for the agent's own sends.
        """
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
        pooled = self.click_prior.plus(total[0], max(total[1] - total[0], 0.0))
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

    def _parts(self, state: CustomerState, action_key: str) -> tuple[Beta, float, float, float]:
        """(click rate, personal x fatigue mean, its variance, the variance of the taste part alone)."""
        rate = self.click_rate(state.segment or ANY, action_key)
        (rho, rho_var), (taste, taste_var) = self.people.parts(state.customer_id, action_key)
        f, f_var = product_moments(
            ((rho, rho_var), (taste, taste_var), self.fatigue_factor(state.contacts_7d))
        )
        return rate, f, f_var, rho * rho * taste_var

    def predict(self, state: CustomerState, action_key: str) -> tuple[float, float]:
        rate, f, f_var, _taste_var = self._parts(state, action_key)
        k, k_sd = self.lift_for(state.segment)
        mean = k * rate.mean * f
        var = (k_sd * rate.mean * f) ** 2 + k * k * (rate.var * f * f + rate.mean**2 * f_var)
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
        rate, f, _f_var, _taste_var = self._parts(state, action_key)
        return self.lift_for(state.segment)[0] * math.sqrt(rate.var) * f

    def treated_rate(self, state: CustomerState, action_key: str) -> float:
        return min(1.0, max(self.organic(state), 0.0) + max(self.predict(state, action_key)[0], 0.0))


@dataclass
class LiftPerClick:
    """Extra purchases per click: the agent group's purchases minus the holdout's, per click.

    Both groups are random slices of the same customers, so the difference in purchases per customer
    since the agent started is what the agent's messages caused, net of everything they cost in
    purchases (a put-off customer buys less). Dividing by the agent group's clicks turns it into the
    scale of `ClickResponseModel`. A click only counts in full once a week has passed since its
    message (before that, part of the purchases it leads to have not happened yet). The estimate is
    shrunk towards `LIFT_PER_CLICK_PRIOR`.

    Two things make it much less noisy on a small holdout:

    * **CUPED.** Each customer's purchase rate before the agent started (`member(pre_rate=...)`) is
      used as a covariate: purchases since the start minus theta x (pre-period rate - its mean), with
      theta fitted on both groups together, so the difference keeps its mean and loses the part of
      its variance that is just "some people buy more than others".
    * **Per segment, shrunk.** `by_segment` measures the same ratio inside each segment (the one a
      customer had when they joined) and pulls each towards the business-wide figure by as much as
      the segments' spread beyond their own noise allows (`shrink_to_pool`).
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

    def click(self, sent_at: datetime, customer_id: str | None = None) -> None:
        self.click_times.append(sent_at)
        if customer_id is not None:
            seg = self.segment.get(customer_id)
            if seg is not None:
                self.clicks_by.setdefault(seg, []).append(sent_at)

    def _matured(self, times: Iterable[datetime], now: datetime) -> float:
        return sum(min(1.0, max((now - t).total_seconds(), 0.0) / 86_400.0 / self.effect_days) for t in times)

    def _extra(self, members: Sequence[str]) -> tuple[float, float] | None:
        """(extra purchases in the agent group, its standard error), CUPED-adjusted; None if a group
        has fewer than two customers."""
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
        return (ma - mh) * n_a, math.sqrt(va / n_a + vh / n_h) * n_a

    def _ratio(self, extra: tuple[float, float], clicks: float) -> tuple[float, float]:
        k0, sd0 = self.prior
        den = self.strength + clicks
        k = max((k0 * self.strength + extra[0]) / den, 0.0)
        return k, math.sqrt((sd0 * self.strength) ** 2 + extra[1] ** 2) / den

    def estimate(self, now: datetime) -> tuple[float, float]:
        clicks = self._matured(self.click_times, now)
        extra = self._extra(sorted(self.group))
        if extra is None or clicks <= 0:
            return self.prior
        return self._ratio(extra, clicks)

    def posterior(self, now: datetime) -> tuple[float, float]:
        """Extra purchases per click, combining the prior and the measurement by their precision.

        `estimate` weighs the measurement by its clicks, however noisy the purchase comparison
        behind them still is; on a holdout of a hundred customers that lets one lucky week swing
        every bid. Here the measured ratio (CUPED-adjusted extra purchases / matured clicks) counts
        by 1 / its variance against the prior's 1 / sd^2, so it takes over as the comparison
        tightens, not merely as clicks pile up."""
        k0, sd0 = self.prior
        clicks = self._matured(self.click_times, now)
        extra = self._extra(sorted(self.group))
        if extra is None or clicks <= 0:
            return self.prior
        k_hat, se = extra[0] / clicks, extra[1] / clicks
        if se <= 0:
            return max(k_hat, 0.0), 0.0
        w0, w = 1.0 / (sd0 * sd0), 1.0 / (se * se)
        return max((k0 * w0 + k_hat * w) / (w0 + w), 0.0), math.sqrt(1.0 / (w0 + w))

    def by_segment(self, now: datetime) -> dict[str, tuple[float, float]]:
        """Extra purchases per click per segment, each shrunk towards the business-wide posterior."""
        pooled = self.posterior(now)
        raw: dict[str, tuple[float, float]] = {}
        members: dict[str, list[str]] = {}
        for cid in sorted(self.group):
            seg = self.segment.get(cid)
            if seg is not None:
                members.setdefault(seg, []).append(cid)
        for seg, cids in members.items():
            clicks = self._matured(self.clicks_by.get(seg, ()), now)
            extra = self._extra(cids)
            if extra is None or clicks < 1.0:
                continue
            # no prior here: the pool is the prior, and shrink_to_pool decides how far to trust this
            raw[seg] = (extra[0] / clicks, extra[1] / clicks)
        if not raw:
            return {}
        shrunk = shrink_to_pool(raw, pooled[0])
        return {seg: (max(k, 0.0), math.sqrt(sd * sd + pooled[1] ** 2)) for seg, (k, sd) in shrunk.items()}


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


# --- the attention price -------------------------------------------------------------------------

STAKE_WEEKS = 13.0
"""How far ahead an unsubscribe costs us: a quarter of the messages we would have sent."""
STAKE_PRIOR_VALUE = 0.25
"""Before the agent has sent anything: a message is assumed worth 25 cents, worth 100 messages."""
STAKE_PRIOR_SENDS = 100.0


@dataclass
class AttentionPrice:
    """What one more message costs this customer: p(unsubscribe | contacts this week) x the stake.

    An unsubscribe does not stop someone buying; it stops us messaging them on that channel. So the
    stake is the value of the messages we would have sent them over the next `STAKE_WEEKS` weeks:
    the agent's own recent expected net per message, times how many messages a customer gets a week.
    The unsubscribe curve is the bundle's learned `FatigueModel`.
    """

    fatigue: FatigueModel
    sends: list[tuple[datetime, float]] = field(default_factory=list)
    customers: set[str] = field(default_factory=set)

    def record(self, decision: Decision) -> None:
        self.customers.add(decision.customer_id)
        if decision.kind is DecisionKind.ACT:
            self.sends.append((decision.made_at, decision.expected_net))

    def roll(self, now: datetime) -> None:
        week_ago = now - timedelta(days=7)
        self.sends = [x for x in self.sends if x[0] > week_ago]

    def stake(self) -> float:
        n = len(self.sends)
        value = (STAKE_PRIOR_VALUE * STAKE_PRIOR_SENDS + sum(v for _, v in self.sends)) / (
            STAKE_PRIOR_SENDS + n
        )
        per_week = n / len(self.customers) if self.customers and n else 1.0
        return max(value, 0.0) * per_week * STAKE_WEEKS

    def refresh(self, now: datetime) -> None:
        """Once a day: forget sends older than a week and recompute the stake."""
        self.roll(now)
        self.current = self.stake()

    def __call__(self, state: CustomerState) -> float:
        return self.fatigue.p_unsub(state.contacts_7d) * self.current

    current: float = STAKE_PRIOR_VALUE * STAKE_WEEKS
    """The stake used by `__call__`, refreshed once a day."""


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
    ) -> None:
        if learn_horizon_days < 1:
            raise ValueError("learn_horizon_days must be at least 1")
        self.version = version
        self.bundle = bundle
        self.costs = dict(costs) if costs is not None else None
        self.kinds: dict[str, GoalKind] = dict(kinds or {})
        self.learn_horizon_days = learn_horizon_days
        self.content_by_id: dict[str, ContentItem] = {c.content_id: c for c in content}
        self.segmenter = Segmenter()
        self.reading = ReadingHours()
        self.breaker = Breaker()
        self.alerts: list[str] = []
        self.value = ShrunkValueModel(base=bundle.value)
        self.attention = AttentionPrice(bundle.fatigue)
        self.lift = LiftPerClick(effect_days=float(learn_horizon_days))
        self.organic = OrganicRate(self.segmenter, days=float(learn_horizon_days))
        if isinstance(bundle.response, ClickResponseModel):
            bundle.response.organic = self.organic
        self.briefs: dict[str, Brief] = {}
        self.bidders: dict[str, GoalBidder] = {}
        self.policy = AutonomousPolicy(
            [],
            self.attention,
            exploration_floor,
            baseline_policy,
            mc_draws=mc_draws,
            version=version,
            plausible_z=PLAUSIBLE_Z,
            min_gap=MIN_GAP,
        )
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
        self._send_contacts: dict[str, tuple[int, int | None]] = {}
        """decision_id -> (messages the customer had in the week before that send, its local hour)."""
        self._refreshed_at: datetime | None = None
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
        self._refit_at: datetime | None = None
        self._live: tuple[Brief, ...] | None = None
        self._content_ref: object = None

    # --- briefs and bidders ---------------------------------------------------------------------

    def _install(self, brief: Brief) -> None:
        self.briefs[brief.goal_id] = brief
        self.bidders[brief.goal_id] = GoalBidder(
            brief,
            None,  # read the live library from ctx.content
            self.bundle.response,
            self.value,
            self.costs,
            kind=_kind_of(brief, self.kinds),
            own_spread=True,
            prune_z=PRUNE_Z,
        )
        self.policy.bidders = list(self.bidders.values())

    def _refresh(self, briefs: Sequence[Brief]) -> None:
        """Rebuild a goal's bidder when the context carries a newer version of its brief."""
        for brief in briefs:
            known = self.briefs.get(brief.goal_id)
            if (
                known is None
                or brief.version > known.version
                or (brief.version == known.version and brief.brief_id != known.brief_id)
            ):
                if known is not None:
                    self.breaker.resume(brief.goal_id)
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

    def _live_briefs(self, briefs: Sequence[Brief]) -> tuple[Brief, ...]:
        live: list[Brief] = []
        for brief in briefs:
            if brief.goal_id in self.breaker.paused_goals:
                continue
            closed = {ch for (g, ch) in self.breaker.paused_channels if g == brief.goal_id}
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

    def segment(self, state: CustomerState, now: datetime) -> str:
        return self.segmenter.label(state, now)

    def prepare_day(self, states: Sequence[CustomerState], ctx: DecisionContext) -> None:
        """Once a day before the sweep (optional): fit the frequency segments and the segments'
        organic purchase rates to the business's own customers (first day), and refresh the
        attention stake. Without it the segments keep their default cuts."""
        if not self.segmenter.fitted:
            self.segmenter.fit(states, ctx.now)
            self.organic.fit(states, ctx.now)
        if not self._decided:
            self._warm_start({s.customer_id: s for s in states}, ctx.now)
        self._daily(ctx.now)

    def _daily(self, now: datetime) -> None:
        if self._refreshed_at is None or now - self._refreshed_at >= timedelta(days=1):
            self.attention.refresh(now)
            self._refreshed_at = now

    def decide(self, state: CustomerState, ctx: DecisionContext, *, trigger: str | None = None) -> Decision:
        if not self._decided:
            self._warm_start({}, ctx.now)
        self._daily(ctx.now)
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
            decision = self.policy.decide(state.model_copy(update=update), eff, trigger=trigger)
            self._decision_segment[decision.decision_id] = seg
            return decision
        hour = self._send_hour(state.customer_id, seg, eff, trigger)
        if hour is not None:
            update["active_hours"] = tuple(1.0 if h == hour else 0.0 for h in range(24))
            update["contacts_7d"] = self._contacts_at_send(state, eff, hour)
        elif state.active_hours:
            # The folder's profile counts every click, most of them at the hour we sent: it would
            # only teach the agent its own habits.
            update["active_hours"] = ()
        decision = self.policy.decide(state.model_copy(update=update), eff, trigger=trigger)
        self._decision_segment[decision.decision_id] = seg
        if decision.group is Group.AGENT:
            self.attention.record(decision)
        return decision

    def _warm_start(self, states: Mapping[str, CustomerState], now: datetime) -> None:
        """Learn from the client's past campaigns before the first decision, so day 1 is not blind.

        Every delivered logged send feeds the click model: the segment click rates take it weighted
        by the inverse of its logged propensity (`logged_probability`, clipped; weight 1 when the
        campaign did not log one), and the customer's own record takes it as for the agent's sends.
        A click that came hours after its message shows when that customer reads. Segments are
        today's (as of the first decision): decisions in the history were not ours, so the segment
        each was made in is unknown, and a customer without a state here is pooled.
        """
        history = {cid: list(evs.values()) for cid, evs in self._history.items()}
        self._history = {}
        self._decided = True
        sends = [s for s in logged_sends(history, self.content_by_id, exclude=self._sends) if s.delivered]
        model = self.bundle.response
        if not sends or not isinstance(model, ClickResponseModel):
            return
        segment = {cid: self.segment(st, now) for cid, st in states.items()}
        weights = ipw_weights([s.propensity for s in sends])
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
        2. Else the allowed hour closest, on average, to when people in their segment read
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
        closest = self.reading.closest_hour(segment, allowed)
        return default if closest is None else closest

    # --- learning -------------------------------------------------------------------------------

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        """Closed rewards (the `Policy` hook) into the shared models, skipping what is learned."""
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
        for d in decisions:
            self._keep(d)
        if not self._decided and events_by_customer:
            # Before the first decision everything handed over is history: past campaigns' sends
            # and clicks are learned from at the first decision (they need segments, see _warm_start).
            for cid, events in events_by_customer.items():
                kept = self._history.setdefault(cid, {})
                for e in events:
                    if e.ts <= now:
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
        if self.segmenter.data_start is None:
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
            )
        model = self.bundle.response
        if isinstance(model, ClickResponseModel):
            model.lift = self.lift.posterior(now)
            model.lift_by_segment = self.lift.by_segment(now)
            if self._refit_at is None or now - self._refit_at >= timedelta(days=PERSONAL_REFIT_DAYS):
                model.people.refit()
                self._refit_at = now
        learned = self.bundle.learned_decisions
        self._log = {k: d for k, d in self._log.items() if k not in learned}
        self._decision_segment = {k: v for k, v in self._decision_segment.items() if k not in learned}
        self._prune(now)
        news = self.breaker.check(self.briefs.values(), now)
        self.alerts.extend(news)
        return news

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
            self._log[d.decision_id] = d
            if d.group is Group.AGENT:
                self._sends[d.decision_id] = (d.action.goal_id, d.action.channel, d.action.send_at)
                item = self.content_by_id.get(d.action.content_id or "")
                if item is not None:
                    seg = self._decision_segment.get(d.decision_id)
                    self._send_keys[d.decision_id] = (
                        seg,
                        key_of(d.action.channel, d.action.offer_id, item.theme.value),
                        d.action.offer_id,
                    )
        elif d.group is Group.HOLDOUT:
            self._log[d.decision_id] = d

    def _observe(self, e: Event) -> None:
        """Feed one event to the lift estimate, the breaker, the click model and the reading hours."""
        if e.type is EventType.PURCHASE:
            self.lift.purchase(e.customer_id, e.ts)
        if e.decision_id is None:
            return
        sent = self._sends.get(e.decision_id)
        if sent is None:
            return
        goal, channel, send_at = sent
        self.breaker.record(e, goal, channel)
        model = self.bundle.response
        keyed = self._send_keys.get(e.decision_id)
        first_click = e.type is EventType.CLICK and e.decision_id not in self._clicked
        if first_click:
            self._clicked.add(e.decision_id)
            self.lift.click(send_at, e.customer_id)
        if keyed is not None and isinstance(model, ClickResponseModel):
            seg, key, offer = keyed
            if e.type is EventType.DELIVER:
                before = self._sends_before(e.customer_id, send_at)
                local = self._local_hour(e.customer_id, send_at)
                self._send_contacts[e.decision_id] = (before, local)
                model.observe_send(e.customer_id, seg, key, offer, before, local)
            elif first_click:
                prior_sends, send_hour = self._send_contacts.get(e.decision_id, (0, None))
                model.observe_click(e.customer_id, seg, key, offer, prior_sends, send_hour)
        if e.type is EventType.CLICK and e.ts - send_at >= LATE_CLICK:
            hour = self._local_hour(e.customer_id, e.ts)
            self.reading.observe(e.customer_id, hour, keyed[0] if keyed is not None else None)

    def _local_hour(self, customer_id: str, at: datetime) -> int:
        return at.astimezone(zone_named(self._timezones.get(customer_id, "UTC"))).hour

    def _sends_before(self, customer_id: str, at: datetime) -> int:
        """SEND events to the customer in the week before `at` (not counting one at `at` itself)."""
        week_ago = at - timedelta(days=7)
        return sum(
            1 for e in self._events.get(customer_id, ()) if e.type is EventType.SEND and week_ago <= e.ts < at
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
        self._clicked = {k for k in self._clicked if k in self._sends}
        self._send_contacts = {k: v for k, v in self._send_contacts.items() if k in self._sends}


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
) -> AgentPolicy:
    """The agent for these briefs and this content library, ready to decide and to learn.

    `bundle` shares models already learned (or seeded from priors); None builds a fresh one with
    `autonomy.learning.build_bundle()` whose response model is a `ClickResponseModel`. `costs`
    overrides the per-message channel costs, `kinds` gives each goal's kind (cart recovery fades
    fast), `exploration_floor` overrides the envelopes' floor and `baseline_policy` decides for the
    baseline group.
    """
    if bundle is None:
        fresh = build_bundle()
        bundle = ModelBundle(response=ClickResponseModel(), value=fresh.value, fatigue=fresh.fatigue)
    return AgentPolicy(
        briefs,
        content,
        bundle,
        costs=costs,
        exploration_floor=exploration_floor,
        baseline_policy=baseline_policy,
        kinds=kinds,
        learn_horizon_days=learn_horizon_days,
    )
