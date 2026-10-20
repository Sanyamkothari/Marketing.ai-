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

Actions are keyed by their labels, `channel|offer_id|theme`, so learning transfers across goals and
across content items that share a theme. Priors can be injected (from the Gym, public data or the
cross-client prior store) and the client's own data overrides them as it accumulates.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from autonomy.contracts import Channel, CustomerState, Objective

ANY = "*"
"""Wildcard for a segment or one part of an action key in prior keys, and the pooled segment."""


def action_key(channel: Channel | str, offer_id: str, theme: str) -> str:
    """The model key of an action: its labels, e.g. `email|pct10|savings`."""
    return f"{channel!s}|{offer_id}|{theme!s}"


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
      segment the exact key before wildcards (theme first, then offer, then channel).
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
        if len(parts) == 3:
            patterns = [
                "|".join(ANY if wild else part for part, wild in zip(parts, mask, strict=True))
                for mask in _WILDCARD_ORDER
            ]
        else:
            patterns = [key]
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
