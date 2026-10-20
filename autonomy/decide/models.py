"""The small models bidders read: response (uplift), value and fatigue. Pure maths, no LLM.

* `ResponseModel` answers "how much does this action change the chance this customer buys, and how
  sure are we?". `BetaResponseModel` keeps Beta posteriors per (segment, action) for "bought after
  being contacted" and per segment for "bought without being contacted" (holdout and no-contact
  customers), so uplift = treated rate - control rate. A customer who would buy anyway gets an uplift
  near zero, which is the point.
* `ValueModel` turns a customer into money: what a conversion is worth, and what their future is
  worth (the stake that fatigue puts at risk).
* `FatigueModel` gives the attention price: the chance that one more message makes this customer
  unsubscribe, given how often we contacted them this week, times their future value. It starts
  from a documented default curve and learns from unsubscribe outcomes.
* `HazardModel` is the agent's sharper version of that chance: per channel and messages this week,
  learned from every send as its outcome arrives, each cell shrunk towards the channel, the count
  and the business (empirical Bayes), from a conservative prior.

Actions are keyed by their labels, `channel|offer_id|theme` (plus `|format` for a message that is not
plain text, e.g. `email|none|education|text_image`), so learning transfers across goals and across
content items that share a theme. Priors can be injected (from the Gym, public data or the
cross-client prior store) and the client's own data overrides them as it accumulates.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from autonomy.contracts import NO_OFFER, Channel, CustomerState, Format, Objective

ANY = "*"
"""Wildcard for a segment or one part of an action key in prior keys, and the pooled segment."""

TEXT_FORMAT = Format.TEXT.value
"""The format a three-part key stands for: plain text is the default, and keys say nothing about it."""


def action_key(channel: Channel | str, offer_id: str, theme: str, fmt: Format | str | None = None) -> str:
    """The model key of an action: its labels, e.g. `email|pct10|savings`.

    A format other than plain text adds a fourth part (`email|none|education|text_image`), so an
    image is learned as its own option. Plain text keeps the three-part key every earlier model,
    prior and log uses, so nothing learned before formats existed is lost.
    """
    base = f"{channel!s}|{offer_id}|{theme!s}"
    if fmt is None or str(fmt) == TEXT_FORMAT:
        return base
    return f"{base}|{fmt!s}"


def base_key(key: str) -> str:
    """The plain-text key under a format key (`email|none|education|text_image` -> `email|none|education`)."""
    parts = key.split("|")
    return "|".join(parts[:3]) if len(parts) > 3 else key


def key_format(key: str) -> str:
    """The format part of an action key: `text` for a three-part key."""
    parts = key.split("|")
    return parts[3] if len(parts) > 3 else TEXT_FORMAT


# --- Beta posteriors ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Beta:
    """A Beta(alpha, beta) belief about a rate."""

    alpha: float
    beta: float

    def __post_init__(self) -> None:
        if self.alpha <= 0 or self.beta <= 0:
            raise ValueError("Beta parameters must be positive")

    @classmethod
    def from_rate(cls, rate: float, strength: float) -> Beta:
        """A belief centred on `rate`, worth `strength` observations."""
        r = min(max(rate, 1e-6), 1 - 1e-6)
        s = max(strength, 1e-6)
        return cls(r * s, (1 - r) * s)

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def var(self) -> float:
        n = self.alpha + self.beta
        return self.alpha * self.beta / (n * n * (n + 1))

    def plus(self, successes: float, failures: float) -> Beta:
        return Beta(self.alpha + successes, self.beta + failures)


DEFAULT_TREATED_PRIOR = Beta.from_rate(0.05, 20.0)
"""5% buy after a contact, worth 20 observations: weak, so a client's data takes over quickly."""
DEFAULT_CONTROL_PRIOR = Beta.from_rate(0.04, 25.0)
"""4% buy anyway, worth 25 observations. So an untried action starts at about +1 point of uplift
with a wide spread: worth trying, not worth trusting."""

# Which parts of an action key a prior may generalise over, tried in this order (1 = wildcard):
# exact, then any theme, any offer, any channel, then coarser.
_WILDCARD_ORDER: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0),
    (0, 0, 1),
    (0, 1, 0),
    (1, 0, 0),
    (0, 1, 1),
    (1, 0, 1),
    (1, 1, 0),
    (1, 1, 1),
)


@dataclass(frozen=True)
class ResponseObservation:
    """One closed outcome. `action_key=None` means not contacted (holdout or a do-nothing decision)."""

    segment: str | None
    action_key: str | None
    converted: bool
    weight: float = 1.0


@runtime_checkable
class ResponseModel(Protocol):
    def predict(self, state: CustomerState, action_key: str) -> tuple[float, float]:
        """(mean uplift, std of uplift) of taking this action with this customer."""
        ...

    def treated_rate(self, state: CustomerState, action_key: str) -> float:
        """The chance they buy if we take this action (used for offer redemption cost)."""
        ...

    def update(self, observations: Iterable[ResponseObservation]) -> None: ...


class BetaResponseModel:
    """Beta posteriors per (segment, action_key) for treated and per segment for control.

    Priors are a dict of `(alpha, beta)` keyed by:

    * `treated/<segment>/<channel>|<offer_id>|<theme>`, where the segment and any part of the action
      key may be `*`. The most specific match wins: the customer's segment before `*`, and within a
      segment the exact key before wildcards (theme first, then offer, then channel). A format key
      (`...|text_image`) first looks for a prior of its own, then falls back to its plain-text key's.
    * `control/<segment>`, or `control/*`.

    Anything unmatched uses `default_treated` / `default_control`. `priors_from_rates` builds this dict
    from plain rates, which is the shape `Prior.values` holds.

    Learning pools across segments: a segment's posterior is its prior plus the other segments' data
    (shrunk to at most `pool_strength` observations) plus its own data in full. So a new segment
    borrows what the rest of the business has learned, and its own data dominates as it grows.
    """

    def __init__(
        self,
        priors: Mapping[str, tuple[float, float]] | None = None,
        *,
        default_treated: Beta = DEFAULT_TREATED_PRIOR,
        default_control: Beta = DEFAULT_CONTROL_PRIOR,
        pool_strength: float = 20.0,
    ) -> None:
        self._priors: dict[str, Beta] = {k: Beta(a, b) for k, (a, b) in (priors or {}).items()}
        self._default_treated = default_treated
        self._default_control = default_control
        self._pool_strength = max(pool_strength, 0.0)
        # counts are [successes, failures]; segment ANY holds the total over all segments
        self._treated: dict[tuple[str, str], list[float]] = {}
        self._control: dict[str, list[float]] = {}

    @staticmethod
    def priors_from_rates(rates: Mapping[str, float], strength: float) -> dict[str, tuple[float, float]]:
        """Turn `{key: rate}` (for example a `Prior.values`) into Beta priors worth `strength` each."""
        out: dict[str, tuple[float, float]] = {}
        for key, rate in rates.items():
            b = Beta.from_rate(rate, strength)
            out[key] = (b.alpha, b.beta)
        return out

    # -- reading --

    def predict(self, state: CustomerState, action_key: str) -> tuple[float, float]:
        seg = _segment(state)
        treated = self.posterior_treated(seg, action_key)
        control = self.posterior_control(seg)
        return treated.mean - control.mean, math.sqrt(treated.var + control.var)

    def treated_rate(self, state: CustomerState, action_key: str) -> float:
        return self.posterior_treated(_segment(state), action_key).mean

    def posterior_treated(self, segment: str, key: str) -> Beta:
        prior = self._treated_prior(segment, key)
        return prior.plus(
            *self._pooled(self._treated.get((ANY, key)), self._treated.get((segment, key)), segment)
        )

    def posterior_control(self, segment: str) -> Beta:
        prior = self._priors.get(f"control/{segment}") or self._priors.get(f"control/{ANY}")
        prior = prior or self._default_control
        return prior.plus(*self._pooled(self._control.get(ANY), self._control.get(segment), segment))

    # -- learning --

    def update(self, observations: Iterable[ResponseObservation]) -> None:
        for obs in observations:
            if obs.weight <= 0:
                continue
            hit, miss = (obs.weight, 0.0) if obs.converted else (0.0, obs.weight)
            segments = (ANY,) if obs.segment in (None, ANY) else (ANY, obs.segment)
            for seg in segments:
                if obs.action_key is None:
                    counts = self._control.setdefault(seg, [0.0, 0.0])
                else:
                    counts = self._treated.setdefault((seg, obs.action_key), [0.0, 0.0])
                counts[0] += hit
                counts[1] += miss

    # -- helpers --

    def _treated_prior(self, segment: str, key: str) -> Beta:
        parts = key.split("|")
        own: list[str] = []
        if len(parts) == 4:
            # An image or video option: a prior of its own if there is one, else the plain message's.
            own = [key]
            parts = parts[:3]
        if len(parts) == 3:
            patterns = own + [
                "|".join(ANY if wild else part for part, wild in zip(parts, mask, strict=True))
                for mask in _WILDCARD_ORDER
            ]
        else:
            patterns = own or [key]
        segments = (segment,) if segment == ANY else (segment, ANY)
        for seg in segments:
            for pattern in patterns:
                found = self._priors.get(f"treated/{seg}/{pattern}")
                if found is not None:
                    return found
        return self._default_treated

    def _pooled(
        self, total: list[float] | None, own: list[float] | None, segment: str
    ) -> tuple[float, float]:
        """Successes and failures to add to the prior: own data in full, others' data shrunk."""
        total_s, total_f = (total[0], total[1]) if total is not None else (0.0, 0.0)
        if segment == ANY:
            return total_s, total_f
        own_s, own_f = (own[0], own[1]) if own is not None else (0.0, 0.0)
        other_s, other_f = total_s - own_s, total_f - own_f
        n_other = other_s + other_f
        scale = min(1.0, self._pool_strength / n_other) if n_other > 0 else 0.0
        return own_s + other_s * scale, own_f + other_f * scale


def _segment(state: CustomerState) -> str:
    return state.segment or ANY


# --- value ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ValueModel:
    """What a conversion is worth for this customer, and what their future is worth.

    A conversion is worth one order: their own average order value (else `default_order_value`)
    times their own margin rate (else `default_margin_rate`). For a revenue objective the margin rate
    is not applied. Future value is the `clv_90d` score when a slow model has written one, else
    `future_orders` orders' worth of margin.
    """

    default_order_value: float = 50.0
    default_margin_rate: float = 0.4
    future_orders: float = 2.0

    def order_value(self, state: CustomerState) -> float:
        aov = state.avg_order_value
        return aov if state.purchases > 0 and aov > 0 else self.default_order_value

    def margin_rate(self, state: CustomerState) -> float:
        if state.revenue > 0 and state.margin > 0:
            return min(state.margin / state.revenue, 1.0)
        return self.default_margin_rate

    def value_if_converted(
        self, state: CustomerState, objective: Objective = Objective.GROSS_PROFIT
    ) -> float:
        if objective is Objective.REVENUE:
            return self.order_value(state)
        return self.order_value(state) * self.margin_rate(state)

    def future_value(self, state: CustomerState) -> float:
        clv = state.scores.get("clv_90d")
        if clv is not None and math.isfinite(clv) and clv >= 0:
            return clv
        return self.order_value(state) * self.margin_rate(state) * self.future_orders


# --- fatigue ----------------------------------------------------------------------------------


DEFAULT_UNSUB_BASE_RATE = 0.002
"""Chance one message causes an unsubscribe for someone not contacted this week (about 0.2%)."""
DEFAULT_UNSUB_GROWTH = 0.5
"""Each contact already received this week multiplies that chance by 1.5."""


@dataclass(frozen=True)
class FatigueObservation:
    """One message's outcome: how many contacts the customer had in the 7 days before it, and
    whether it led to an unsubscribe (or complaint)."""

    contacts_7d: int
    unsubscribed: bool
    weight: float = 1.0


class FatigueModel:
    """The attention price: p(unsubscribe | contacts this week) x future value.

    Each contacts-this-week count (capped at `max_contacts`) has a Beta belief, starting at the
    default curve `base_rate x (1 + growth) ^ contacts` and worth `prior_strength` messages, so a few
    unsubscribes do not swing it. The learned curve is kept non-decreasing: more contacts never make
    the next message safer.
    """

    def __init__(
        self,
        value_model: ValueModel | None = None,
        *,
        base_rate: float = DEFAULT_UNSUB_BASE_RATE,
        growth: float = DEFAULT_UNSUB_GROWTH,
        max_contacts: int = 6,
        prior_strength: float = 1000.0,
    ) -> None:
        if max_contacts < 0:
            raise ValueError("max_contacts must be >= 0")
        self.value_model = value_model or ValueModel()
        self._max = max_contacts
        self._beliefs = [
            Beta.from_rate(min(base_rate * (1 + growth) ** n, 0.5), prior_strength)
            for n in range(max_contacts + 1)
        ]

    def p_unsub(self, contacts_7d: int) -> float:
        n = min(max(contacts_7d, 0), self._max)
        return max(b.mean for b in self._beliefs[: n + 1])

    def price(self, state: CustomerState) -> float:
        """What the next message costs in expected lost future value. A bid must beat this."""
        return self.p_unsub(state.contacts_7d) * self.value_model.future_value(state)

    def update(self, observations: Iterable[FatigueObservation]) -> None:
        for obs in observations:
            if obs.weight <= 0:
                continue
            n = min(max(obs.contacts_7d, 0), self._max)
            hit, miss = (obs.weight, 0.0) if obs.unsubscribed else (0.0, obs.weight)
            self._beliefs[n] = self._beliefs[n].plus(hit, miss)


# --- the contact hazard, per channel and contacts this week -----------------------------------------

HAZARD_BASE_RATE = 0.005
"""Before any send: half a percent of messages lose the contact (an unsubscribe or a complaint) for
someone not contacted this week. Conservative on purpose. Published email unsubscribe rates run
0.1-0.5% a campaign and WhatsApp opt-outs and blocks higher, so a business that has not measured its
own rate should assume the dear end, not zero: a near-zero prior made the second message of every
week look free until weeks of unsubscribes had piled up."""
HAZARD_GROWTH = 0.5
"""Each message already received this week multiplies that chance by 1.5 in the prior."""
HAZARD_MAX_CONTACTS = 3
"""Contact counts above this share one cell."""
HAZARD_BUSINESS_STRENGTH = 2.0
HAZARD_COUNT_STRENGTH = 3.0
HAZARD_CHANNEL_STRENGTH = 3.0
HAZARD_CELL_STRENGTH = 3.0
"""Each level's Gamma(k, k) prior, in expected events: a level moves halfway from the one above once
its own sends predict this many events. Counted in events, not sends, so a rare event needs more
sends before one unsubscribe can move it."""


def _gamma_mean(events: float, expected: float, strength: float) -> float:
    return (strength + events) / (strength + max(expected, 0.0))


class HazardModel:
    """P(a message loses the contact | its channel, messages the customer had in the last 7 days).

    Learned from every send as soon as its outcome is in (an unsubscribe or a complaint arrives hours
    after its message, not after a week-long window), by a four-level empirical-Bayes model:

        p(channel, n) = prior(n) x r_business x r_count[n] x r_channel[channel] x r_cell[channel, n]

    `prior(n)` is `base_rate x (1 + growth) ^ n`. Each `r` is a Gamma(k, k) multiplier centred on 1,
    updated with the events its sends saw against the events the levels above predicted for them
    (Poisson counts with an offset). So the business's overall level is learned first, then how much
    worse a second or third message of a week is (pooled over channels, so email learns the shape
    from WhatsApp's sends too), then each channel's own level (WhatsApp and email apart), and last
    each channel x count cell. A cell with few sends stays where the levels above put it. The curve
    is kept non-decreasing in the count: another message this week never makes the next one safer.

    Deterministic and order-free: it keeps counts only.
    """

    def __init__(
        self,
        *,
        base_rate: float = HAZARD_BASE_RATE,
        growth: float = HAZARD_GROWTH,
        max_contacts: int = HAZARD_MAX_CONTACTS,
        business_strength: float = HAZARD_BUSINESS_STRENGTH,
        count_strength: float = HAZARD_COUNT_STRENGTH,
        channel_strength: float = HAZARD_CHANNEL_STRENGTH,
        cell_strength: float = HAZARD_CELL_STRENGTH,
    ) -> None:
        if not 0 < base_rate < 1 or growth < 0 or max_contacts < 0:
            raise ValueError("need 0 < base_rate < 1, growth >= 0 and max_contacts >= 0")
        if min(business_strength, count_strength, channel_strength, cell_strength) <= 0:
            raise ValueError("prior strengths must be positive")
        self.max_contacts = max_contacts
        self.prior = tuple(min(base_rate * (1 + growth) ** n, 0.5) for n in range(max_contacts + 1))
        self.strengths = (business_strength, count_strength, channel_strength, cell_strength)
        self._sends: dict[tuple[Channel, int], float] = {}
        self._events: dict[tuple[Channel, int], float] = {}
        self._rates: dict[tuple[Channel, int], float] | None = None

    def _cell(self, contacts_7d: int) -> int:
        return min(max(contacts_7d, 0), self.max_contacts)

    def observe_send(self, channel: Channel, contacts_7d: int, weight: float = 1.0) -> None:
        """A delivered message on `channel` to someone with `contacts_7d` messages in the week before."""
        if weight > 0:
            key = (channel, self._cell(contacts_7d))
            self._sends[key] = self._sends.get(key, 0.0) + weight
            self._rates = None

    def observe_event(self, channel: Channel, contacts_7d: int, weight: float = 1.0) -> None:
        """That message lost the contact (count each message once, even if it caused two events)."""
        if weight > 0:
            key = (channel, self._cell(contacts_7d))
            self._events[key] = self._events.get(key, 0.0) + weight
            self._rates = None

    def counts(self, channel: Channel, contacts_7d: int) -> tuple[float, float]:
        """(sends, events) seen in one cell."""
        key = (channel, self._cell(contacts_7d))
        return self._sends.get(key, 0.0), self._events.get(key, 0.0)

    def _fit(self) -> dict[tuple[Channel, int], float]:
        k_bus, k_count, k_chan, k_cell = self.strengths
        # Sorted, so the float sums below add up in the same order in every process (a set of
        # channels iterates in an order that changes with the hash seed).
        cells = sorted(set(self._sends) | set(self._events), key=lambda c: (c[0].value, c[1]))
        sends = {c: self._sends.get(c, 0.0) for c in cells}
        events = {c: self._events.get(c, 0.0) for c in cells}
        prior = {c: self.prior[c[1]] * sends[c] for c in cells}
        r_bus = _gamma_mean(sum(events.values()), sum(prior.values()), k_bus)
        r_count: dict[int, float] = {}
        for n in range(self.max_contacts + 1):
            mine = [c for c in cells if c[1] == n]
            r_count[n] = _gamma_mean(
                sum(events[c] for c in mine), sum(prior[c] for c in mine) * r_bus, k_count
            )
        r_chan: dict[Channel, float] = {}
        for ch in dict.fromkeys(c[0] for c in cells):
            mine = [c for c in cells if c[0] is ch]
            expected = sum(prior[c] * r_bus * r_count[c[1]] for c in mine)
            r_chan[ch] = _gamma_mean(sum(events[c] for c in mine), expected, k_chan)
        rates: dict[tuple[Channel, int], float] = {}
        for ch in Channel:
            running = 0.0
            for n in range(self.max_contacts + 1):
                level = self.prior[n] * r_bus * r_count[n] * r_chan.get(ch, 1.0)
                key = (ch, n)
                cell = _gamma_mean(events[key], level * sends[key], k_cell) if key in sends else 1.0
                running = max(running, min(level * cell, 0.5))
                rates[key] = running
        return rates

    def rate(self, channel: Channel, contacts_7d: int) -> float:
        """The chance the next message on `channel` loses the contact, given this week's count."""
        if self._rates is None:
            self._rates = self._fit()
        return self._rates[(channel, self._cell(contacts_7d))]


# --- per-person response: empirical Bayes ------------------------------------------------------
#
# People in one segment want different things: one clicks on everything, one only on money off, one
# never. A model per (segment, action) gets the average right and every individual wrong. The pieces
# below learn each customer's own deviation from their segment, shrunk towards it by exactly as much
# as the business's data says people really differ (empirical Bayes), so a customer with two messages
# behind them is still mostly their segment and one with thirty is mostly themselves.

GAMMA_STRENGTH_GRID: tuple[float, ...] = (
    0.5,
    1.0,
    1.5,
    2.0,
    3.0,
    4.0,
    6.0,
    8.0,
    12.0,
    16.0,
    24.0,
    32.0,
    64.0,
)
"""Candidate prior strengths (1 / variance of the multiplier) tried when fitting one from data."""


def gamma_poisson_loglik(clicks: float, expected: float, strength: float) -> float:
    """Log marginal likelihood of `clicks` when the rate is `expected` x a Gamma(k, k) multiplier.

    This is the negative binomial with mean `expected` and shape `strength`; the term that does not
    depend on the strength (log clicks!) is left out. Non-integer (weighted) clicks are fine.
    """
    k = strength
    m = max(expected, 1e-12)
    return (
        math.lgamma(k + clicks) - math.lgamma(k) + k * math.log(k / (k + m)) + clicks * math.log(m / (k + m))
    )


def fit_gamma_strength(
    pairs: Iterable[tuple[float, float]],
    *,
    grid: Iterable[float] = GAMMA_STRENGTH_GRID,
    default: float,
    min_units: int = 30,
) -> float:
    """The prior strength under which the observed (clicks, expected clicks) pairs are most likely.

    Each pair is one unit (a customer, or a customer x offer): its clicks, and the clicks its segment
    would predict. If units really differ, their click counts are more spread out than Poisson noise
    alone allows and the fitted strength is low (trust each unit's own record sooner); if they do
    not, it is high (keep everyone at their segment). Type-II maximum likelihood over `grid`. With
    fewer than `min_units` units that carry any expectation the data cannot say, so `default`.
    """
    data = [(c, e) for c, e in pairs if e > 0 and math.isfinite(e) and math.isfinite(c) and c >= 0]
    if len(data) < min_units:
        return default
    best, best_ll = default, -math.inf
    for k in grid:
        ll = sum(gamma_poisson_loglik(c, e, k) for c, e in data)
        if ll > best_ll + 1e-9:
            best, best_ll = k, ll
    return best


def moment_strength(
    units: Iterable[tuple[float, ...]],
    *,
    default: float,
    min_units: int = 30,
    floor: float = GAMMA_STRENGTH_GRID[0],
    ceiling: float = GAMMA_STRENGTH_GRID[-1],
) -> float:
    """The prior strength (1 / variance of the multiplier) from the spread of yes/no click records.

    Each unit is (clicks, expected clicks, sum of squared per-message click chances[, extra]). A
    unit's clicks vary by the messages' own yes/no noise (expected - squares: at most one click per
    message, so less than Poisson), by `extra` (the variance the expectation itself carries, when it
    rests on an estimate), plus the multiplier's variance times expected squared. The method-of-moments
    estimate of that variance pools every unit (weighted by expected squared, so a unit with more
    messages counts for more). This, not a Poisson likelihood, is what one click per message needs:
    the yes/no noise is smaller than Poisson noise, and a Poisson model would read the difference as
    "people do not differ". Clipped to [floor, ceiling]; `default` with fewer than `min_units` units.
    """
    data = [u for u in units if len(u) >= 3 and u[1] > 0 and all(math.isfinite(v) for v in u)]
    if len(data) < min_units:
        return default
    num = sum((u[0] - u[1]) ** 2 - max(u[1] - u[2], 0.0) - (u[3] if len(u) > 3 else 0.0) for u in data)
    den = sum(u[1] * u[1] for u in data)
    if den <= 0:
        return default
    var = num / den
    if var <= 1.0 / ceiling:
        return ceiling
    return min(max(1.0 / var, floor), ceiling)


def product_moments(factors: Iterable[tuple[float, float]]) -> tuple[float, float]:
    """(mean, variance) of a product of independent factors, each given as (mean, variance)."""
    m, second = 1.0, 1.0
    for fm, fv in factors:
        m *= fm
        second *= fm * fm + fv
    return m, max(second - m * m, 0.0)


PERSON_FEATURES: tuple[str, ...] = ("channel", "offer", "theme", "format")
"""The parts of an action a person's own taste is learned for. Learning on one discount informs the
next discount on any channel; a liking for WhatsApp informs every WhatsApp message; someone who
clicks on product images gets more of them, on any channel that can carry one."""
DEFAULT_OVERALL_STRENGTH = 3.0
"""Before the data can say: a customer's overall responsiveness varies by about +-60% around their
segment's (variance 1/3)."""
DEFAULT_FEATURE_STRENGTH = 8.0
"""Before the data can say: a customer's taste for one channel, offer, theme or format varies by
about +-35% around what their overall level predicts (variance 1/8)."""
REFERENCE_LEVELS: Mapping[str, str] = MappingProxyType({"offer": NO_OFFER.offer_id, "format": TEXT_FORMAT})
"""Feature levels whose factor is fixed at 1. With "no offer" as the reference, a customer's overall
level is how they answer a plain message and their offer factor is how much more (or less) an offer
moves them than that: the taste that differs most between people, measured against a clean baseline.
Plain text is the format's reference for the same reason: the format factor is how much more (or
less) an image, carousel or video moves this person than the same words alone."""
_BACKFIT_ROUNDS = 2

_Fit = tuple[tuple[float, float], dict[tuple[int, str], tuple[float, float]]]
_Cell = tuple[str, str, str, str]


def action_features(action_key: str) -> _Cell:
    """(channel, offer, theme, format) of an action key.

    A three-part key is plain text (see `action_key`); parts missing from a shorter key are ANY.
    """
    parts = action_key.split("|")
    fmt = parts[3] if len(parts) > 3 else TEXT_FORMAT
    parts = parts[:3]
    while len(parts) < 3:
        parts.append(ANY)
    return parts[0], parts[1], parts[2], fmt


@dataclass
class _Person:
    """One customer's evidence and its fitted factors (recomputed only when the evidence changes)."""

    cells: dict[_Cell, list[float]]
    """(channel, offer, theme, format) -> [clicks, expected clicks from their segment, sum of each message's
    expected clicks squared (for the yes/no noise in `refit`)]."""
    fit: _Fit | None = None
    """((overall shape, rate), {(feature index, level): (shape, rate)}), or None when stale."""


class PersonalEffects:
    """Each customer's own click response, relative to their segment, per action feature.

    For customer c and an action with features (channel, offer, theme):

        clicks ~ Poisson(segment rate(action) x rho_c x psi_c[channel] x psi_c[offer] x psi_c[theme]
                         x psi_c[format])

    Plain text is the format's reference (its factor is 1), so `psi_c[text_image]` is how much more
    this person answers a message with a product image than the same words alone: the agent learns
    who responds to images, shrunk towards their segment like every other taste.

    Every factor has a Gamma(k, k) prior centred on 1, so a customer with no record is exactly their
    segment. Each delivered message adds its segment-predicted clicks to the customer's evidence and
    each first click adds one click. Posteriors come from two rounds of backfitting (each factor's
    Gamma posterior given the others' means): exact for the overall factor, close for the rest at
    the few dozen messages a customer gets.

    The prior strengths are not guesses: `refit()` estimates them by the method of moments from the
    spread of every customer's clicks beyond their yes/no noise (`moment_strength`), so they say how
    much this business's customers really differ. A shop of near-identical customers keeps everyone at their segment; one with
    sleeping dogs and bargain hunters lets each person's record speak after a few messages.

    Observations can be weighted (an inverse-propensity weight for logged history sends, say).
    """

    def __init__(
        self,
        *,
        overall_strength: float = DEFAULT_OVERALL_STRENGTH,
        feature_strength: float = DEFAULT_FEATURE_STRENGTH,
        reference: Mapping[str, str] = REFERENCE_LEVELS,
    ) -> None:
        if overall_strength <= 0 or feature_strength <= 0:
            raise ValueError("prior strengths must be positive")
        unknown = set(reference) - set(PERSON_FEATURES)
        if unknown:
            raise ValueError(f"unknown features in reference: {sorted(unknown)}")
        self._reference: dict[int, str] = {PERSON_FEATURES.index(f): level for f, level in reference.items()}
        self.overall_strength = overall_strength
        self.feature_strengths: list[float] = [feature_strength] * len(PERSON_FEATURES)
        self._default_overall = overall_strength
        self._default_feature = feature_strength
        self._people: dict[str, _Person] = {}

    # -- evidence --

    def observe(
        self,
        customer_id: str,
        action_key: str,
        *,
        expected: float = 0.0,
        clicks: float = 0.0,
        weight: float = 1.0,
    ) -> None:
        """Add a message's segment-predicted clicks (`expected`) and/or its clicks to the evidence."""
        if weight <= 0 or (expected <= 0 and clicks <= 0):
            return
        person = self._people.get(customer_id)
        if person is None:
            person = self._people[customer_id] = _Person(cells={})
        cell = person.cells.setdefault(action_features(action_key), [0.0, 0.0, 0.0])
        e = min(max(expected, 0.0), 1.0)
        cell[0] += max(clicks, 0.0) * weight
        cell[1] += e * weight
        cell[2] += e * e * weight
        person.fit = None

    def forget(self, customer_id: str) -> None:
        self._people.pop(customer_id, None)

    def evidence(self, customer_id: str) -> tuple[float, float]:
        """(clicks, expected clicks) over all of a customer's messages."""
        person = self._people.get(customer_id)
        if person is None:
            return 0.0, 0.0
        return sum(v[0] for v in person.cells.values()), sum(v[1] for v in person.cells.values())

    def __len__(self) -> int:
        return len(self._people)

    # -- fitting --

    def _fit(self, person: _Person) -> _Fit:
        if person.fit is not None:
            return person.fit
        k0 = self.overall_strength
        ks = self.feature_strengths
        cells = [(key, (v[0], v[1])) for key, v in person.cells.items()]
        means: dict[tuple[int, str], float] = {}
        overall = (k0, k0)
        params: dict[tuple[int, str], tuple[float, float]] = {}
        for _ in range(_BACKFIT_ROUNDS):
            rho_shape = k0 + sum(c for _key, (c, _e) in cells)
            rho_rate = k0 + sum(e * _others(means, key, -1) for key, (_c, e) in cells)
            overall = (rho_shape, rho_rate)
            rho = rho_shape / rho_rate
            for f in range(len(PERSON_FEATURES)):
                sums: dict[str, list[float]] = {}
                for key, (c, e) in cells:
                    acc = sums.setdefault(key[f], [0.0, 0.0])
                    acc[0] += c
                    acc[1] += e * rho * _others(means, key, f)
                for level, (c, e) in sums.items():
                    if self._reference.get(f) == level:
                        continue  # fixed at 1
                    shape, rate = ks[f] + c, ks[f] + e
                    params[(f, level)] = (shape, rate)
                    means[(f, level)] = shape / rate
        person.fit = (overall, params)
        return person.fit

    def factor(self, customer_id: str, action_key: str) -> tuple[float, float]:
        """(mean, variance) of how this customer's click rate for this action compares with their
        segment's: 1 and the prior variance for someone we know nothing about."""
        overall, taste = self.parts(customer_id, action_key)
        return product_moments((overall, taste))

    def parts(self, customer_id: str, action_key: str) -> tuple[tuple[float, float], tuple[float, float]]:
        """The factor split in two, each as (mean, variance): the customer's overall responsiveness,
        which every one of their options shares, and their taste for this action's channel, offer and
        theme, which tells their options apart."""
        person = self._people.get(customer_id)
        fit = self._fit(person) if person is not None else None
        taste: list[tuple[float, float]] = []
        for f, level in enumerate(action_features(action_key)):
            found = fit[1].get((f, level)) if fit is not None else None
            if self._reference.get(f) == level:
                taste.append((1.0, 0.0))
            elif found is None:
                taste.append((1.0, 1.0 / self.feature_strengths[f]))
            else:
                taste.append((found[0] / found[1], found[0] / found[1] ** 2))
        if fit is None:
            return (1.0, 1.0 / self.overall_strength), product_moments(taste)
        shape, rate = fit[0]
        return (shape / rate, shape / rate**2), product_moments(taste)

    def overall(self, customer_id: str) -> tuple[float, float]:
        """(mean, variance) of the customer's overall responsiveness alone."""
        person = self._people.get(customer_id)
        if person is None:
            return 1.0, 1.0 / self.overall_strength
        (shape, rate), _ = self._fit(person)
        return shape / rate, shape / rate**2

    def refit(self, *, min_units: int = 30) -> None:
        """Choose the prior strengths that make the business's click records most likely.

        The overall strength is fitted on each customer's total clicks against their segment's
        prediction (`moment_strength`). A feature's strength is fitted on (customer, level) records
        against what the segment predicts times the customer's own level on their *other* messages
        (on their no-offer messages, for the offer, which has "no offer" as its reference), for
        customers with more than one level of the feature. Measuring their level on the same
        messages would let it soak up the very taste being measured (someone who clicks on every
        discount looks generally keen, and their discount clicks then look ordinary); the
        leave-one-level-out level is noisier instead, and that noise is subtracted.
        """
        totals = [
            (
                sum(v[0] for v in p.cells.values()),
                sum(v[1] for v in p.cells.values()),
                sum(v[2] for v in p.cells.values()),
            )
            for p in self._people.values()
        ]
        self.overall_strength = moment_strength(totals, default=self._default_overall, min_units=min_units)
        self._stale()
        k0 = self.overall_strength
        for f in range(len(PERSON_FEATURES)):
            units: list[tuple[float, ...]] = []
            ref = self._reference.get(f)
            for person in self._people.values():
                sums: dict[str, list[float]] = {}
                for key, (c, e, sq) in person.cells.items():
                    acc = sums.setdefault(key[f], [0.0, 0.0, 0.0])
                    acc[0] += c
                    acc[1] += e
                    acc[2] += sq
                if len(sums) < 2 or (ref is not None and ref not in sums):
                    continue
                total_c = sum(v[0] for v in sums.values())
                total_e = sum(v[1] for v in sums.values())
                for level, (c, e, sq) in sums.items():
                    if level == ref:
                        continue
                    # their level on the reference messages if there is a reference, else on the rest
                    base_c, base_e = (
                        (sums[ref][0], sums[ref][1]) if ref is not None else (total_c - c, total_e - e)
                    )
                    shape, rate = k0 + base_c, k0 + base_e
                    rho, rho_var = shape / rate, shape / rate**2
                    units.append((c, e * rho, sq * rho * rho, e * e * rho_var))
            self.feature_strengths[f] = moment_strength(
                units, default=self._default_feature, min_units=min_units
            )
        self._stale()

    def _stale(self) -> None:
        for person in self._people.values():
            person.fit = None


def _others(means: Mapping[tuple[int, str], float], key: _Cell, skip: int) -> float:
    """Product of the current feature means for this cell, leaving out feature `skip`."""
    out = 1.0
    for f, level in enumerate(key):
        if f != skip:
            out *= means.get((f, level), 1.0)
    return out


# --- calibration helpers -------------------------------------------------------------------------


def cuped_theta(x: Sequence[float], y: Sequence[float]) -> float:
    """The CUPED coefficient cov(x, y) / var(x): how much of y the pre-period covariate x predicts.

    Computed on everyone in the comparison (treated and control together), so it is the same for
    both groups and the adjusted difference stays unbiased under random assignment. 0 when x does not
    vary.
    """
    n = len(x)
    if n != len(y):
        raise ValueError("x and y must have the same length")
    if n < 2:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    if sxx <= 0:
        return 0.0
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / sxx


def cuped_adjust(x: Sequence[float], y: Sequence[float], theta: float | None = None) -> list[float]:
    """y - theta (x - mean x): the same mean as y, and less variance when x predicts y.

    Controlled-experiment Using Pre-Experiment Data (Deng et al., 2013). A customer's purchases before
    the agent started say a lot about their purchases since; taking that predictable part out leaves
    the part a message can change, so the agent-versus-holdout difference needs far fewer customers
    for the same precision.
    """
    if len(x) != len(y):
        raise ValueError("x and y must have the same length")
    t = cuped_theta(x, y) if theta is None else theta
    mx = sum(x) / len(x) if x else 0.0
    return [b - t * (a - mx) for a, b in zip(x, y, strict=True)]


def shrink_to_pool(
    estimates: Mapping[str, tuple[float, float]], pooled: float
) -> dict[str, tuple[float, float]]:
    """Empirical-Bayes shrinkage of per-group estimates towards a pooled value.

    `estimates` maps a group to (estimate, standard error). How far the groups spread beyond their
    standard errors (tau^2, the DerSimonian-Laird moment estimate) sets how hard each is pulled to
    `pooled`: a group measured loosely ends near the pool, one measured precisely keeps its own value.
    Returns (shrunk estimate, its standard error) per group. Groups without a usable standard error,
    and every group when the spread is no more than noise, come back at the pool.
    """
    usable = {g: (m, s) for g, (m, s) in estimates.items() if s > 0 and math.isfinite(m) and math.isfinite(s)}
    tau2 = 0.0
    if len(usable) >= 2:
        w = {g: 1.0 / (s * s) for g, (_m, s) in usable.items()}
        sw = sum(w.values())
        mean = sum(w[g] * m for g, (m, _s) in usable.items()) / sw
        q = sum(w[g] * (m - mean) ** 2 for g, (m, _s) in usable.items())
        denom = sw - sum(v * v for v in w.values()) / sw
        tau2 = max(0.0, (q - (len(usable) - 1)) / denom) if denom > 0 else 0.0
    out: dict[str, tuple[float, float]] = {}
    for g in estimates:
        if g not in usable or tau2 <= 0:
            out[g] = (pooled, math.sqrt(tau2))
            continue
        m, s = usable[g]
        b = s * s / (s * s + tau2)
        out[g] = (b * pooled + (1 - b) * m, math.sqrt((1 - b) * s * s))
    return out
