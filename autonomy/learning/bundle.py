"""The models the agent learns, kept together with what they have already learned from.

`ModelBundle` holds the three small models the bidders read (response, value, fatigue) and the
bookkeeping that makes learning idempotent: the decision ids, purchase event ids and holdout windows
already turned into observations. The nightly loop can then hand `update_models` the whole log every
night and each outcome is still counted once.

`build_bundle(priors)` makes a fresh bundle, seeded from Gym or public-data priors when given. The
priors are a flat `{key: number}` dict, the shape `Prior.values` holds (merge several with
`prior_values`):

* `treated/<segment>/<channel>|<offer_id>|<theme>` and `control/<segment>`: conversion rates for the
  response model (`*` is a wildcard, see `BetaResponseModel`). Each is worth `strength` observations.
* `value/order_value`, `value/margin_rate`, `value/future_orders`: the value model's fallbacks.
* `fatigue/base_rate`, `fatigue/growth`: the starting unsubscribe curve.

Other keys are ignored, so a prior written for something else does nothing here.

`ValueModel` is frozen and has no `update()`, so the bundle uses `LearnedValueModel`: a `ValueModel`
whose fallback order value and margin rate move with the business's own purchases. It is updated in
place, so the bidders and the fatigue model that hold it see the new numbers without being rebuilt.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from autonomy.contracts import CustomerState, Prior
from autonomy.decide.models import (
    DEFAULT_UNSUB_BASE_RATE,
    DEFAULT_UNSUB_GROWTH,
    BetaResponseModel,
    FatigueModel,
    ValueModel,
)

DEFAULT_PRIOR_STRENGTH = 50.0
"""How many observations one prior rate is worth. A client's own data outweighs it within weeks."""

DEFAULT_VALUE_PRIOR_ORDERS = 20.0
"""How many orders the value model's starting order value and margin rate are worth."""


@dataclass(frozen=True)
class ValueObservation:
    """One purchase: its gross value and, when known, its margin."""

    value: float
    margin: float | None = None


@dataclass
class ValueStats:
    """Running sums of the business's purchases, shared by reference so updates show everywhere."""

    orders: float = 0.0
    revenue: float = 0.0
    margin_revenue: float = 0.0
    """Revenue of the purchases whose margin is known: the denominator of the margin rate."""
    margin: float = 0.0


@dataclass(frozen=True)
class LearnedValueModel(ValueModel):
    """A `ValueModel` whose fallbacks learn from the business's purchases.

    A customer with their own purchase history is still valued on their own order value and margin
    (as in `ValueModel`). For everyone else the fallback order value is the business's average order
    shrunk towards `default_order_value` (worth `prior_orders` orders), and the fallback margin rate
    is the business's margin over revenue, shrunk the same way towards `default_margin_rate`. Purchases
    without a recorded margin count towards the order value only.
    """

    prior_orders: float = DEFAULT_VALUE_PRIOR_ORDERS
    stats: ValueStats = field(default_factory=ValueStats, compare=False, hash=False, repr=False)

    @property
    def learned_order_value(self) -> float:
        k = max(self.prior_orders, 0.0)
        n = self.stats.orders
        if k + n <= 0:
            return self.default_order_value
        return (self.default_order_value * k + self.stats.revenue) / (k + n)

    @property
    def learned_margin_rate(self) -> float:
        k_revenue = max(self.prior_orders, 0.0) * self.default_order_value
        denom = k_revenue + self.stats.margin_revenue
        if denom <= 0:
            return self.default_margin_rate
        rate = (self.default_margin_rate * k_revenue + self.stats.margin) / denom
        return min(max(rate, 0.0), 1.0)

    def order_value(self, state: CustomerState) -> float:
        aov = state.avg_order_value
        return aov if state.purchases > 0 and aov > 0 else self.learned_order_value

    def margin_rate(self, state: CustomerState) -> float:
        if state.revenue > 0 and state.margin > 0:
            return min(state.margin / state.revenue, 1.0)
        return self.learned_margin_rate

    def update(self, observations: Iterable[ValueObservation]) -> None:
        """Add purchases to the running sums. Non-positive values (and negative margins) are skipped:
        refunds and zero-value orders say nothing about what a normal order is worth."""
        for obs in observations:
            if not obs.value > 0:
                continue
            self.stats.orders += 1.0
            self.stats.revenue += obs.value
            if obs.margin is not None and obs.margin >= 0:
                self.stats.margin_revenue += obs.value
                self.stats.margin += min(obs.margin, obs.value)


@dataclass
class ModelBundle:
    """The agent's three learnable models, plus what they have already learned from.

    Build one with `build_bundle`. Share `response`, `value` and `fatigue` with the bidders and the
    attention price; `update_models` changes them in place. The `learned_*` sets and the holdout
    watermark are what make `update_models` idempotent; they grow with the decision log (one short
    string per decision), which is fine for a nightly job and can be pruned of windows older than
    the horizon if it ever matters.
    """

    response: BetaResponseModel
    value: ValueModel
    fatigue: FatigueModel
    learned_decisions: set[str] = field(default_factory=set)
    """Decision ids whose window closed and whose observations were made (or that were skipped)."""
    learned_purchases: set[str] = field(default_factory=set)
    """PURCHASE event ids already fed to the value model."""
    learned_holdout: set[str] = field(default_factory=set)
    """Keys `customer_id@window_start` of holdout windows already counted as control."""
    last_control_start: dict[str, datetime] = field(default_factory=dict)
    """Start of the latest control window per customer, to discount overlapping windows."""
    holdout_anchor: datetime | None = None
    """Start of the latest virtual holdout window (see `update_models(holdout_customers=...)`)."""


def prior_values(priors: Iterable[Prior]) -> dict[str, float]:
    """Merge several `Prior.values` into the flat dict `build_bundle` takes; later priors win."""
    out: dict[str, float] = {}
    for prior in priors:
        out.update(prior.values)
    return out


def build_bundle(
    priors: Mapping[str, float] | None = None,
    *,
    strength: float = DEFAULT_PRIOR_STRENGTH,
    value_prior_orders: float = DEFAULT_VALUE_PRIOR_ORDERS,
) -> ModelBundle:
    """A fresh bundle: the decide defaults, overridden by `priors` where it has a key (see module)."""
    given = dict(priors or {})
    rates = {k: v for k, v in given.items() if k.startswith(("treated/", "control/"))}
    response = BetaResponseModel(BetaResponseModel.priors_from_rates(rates, strength))

    base = ValueModel()
    value = LearnedValueModel(
        default_order_value=_positive(given, "value/order_value", base.default_order_value),
        default_margin_rate=min(_positive(given, "value/margin_rate", base.default_margin_rate), 1.0),
        future_orders=_positive(given, "value/future_orders", base.future_orders),
        prior_orders=value_prior_orders,
    )
    fatigue = FatigueModel(
        value,
        base_rate=_positive(given, "fatigue/base_rate", DEFAULT_UNSUB_BASE_RATE),
        growth=_non_negative(given, "fatigue/growth", DEFAULT_UNSUB_GROWTH),
    )
    return ModelBundle(response=response, value=value, fatigue=fatigue)


def _positive(given: Mapping[str, float], key: str, default: float) -> float:
    """The prior's number when it is a finite positive number, else the default."""
    v = given.get(key)
    if v is None or not math.isfinite(v) or v <= 0:
        return default
    return float(v)


def _non_negative(given: Mapping[str, float], key: str, default: float) -> float:
    """The prior's number when it is a finite number >= 0, else the default."""
    v = given.get(key)
    if v is None or not math.isfinite(v) or v < 0:
        return default
    return float(v)
