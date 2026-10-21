"""What a bidder reads from an uplift model: a per-customer, per-action score, and how it is blended.

This module is the light half of `autonomy.uplift`: the bidder imports it, so it needs nothing but
the standard library. The fitting half (`autonomy.uplift.model`, `autonomy.uplift.campaign`) is only
imported by whoever fits a model.

**The score.** `UpliftScore(mean, sd, n)`: how much a message with this action key raises this
customer's chance of buying in the learning window (a probability, like `Bid.uplift`), the spread of
that estimate, and how many customers of the campaign it rests on. The bidder multiplies it by what a
conversion is worth to this customer (`ValueModel`), as it does the response model's uplift, so it is
in money as soon as it is in a bid.

**The blend** (`blend`). The response model's uplift is a model's reading (for the agent: clicks
times a lift per click); the score is a measurement (a randomised or propensity-logged campaign,
validated out of sample). They are combined by their precision, but the response model's precision
is what it *measured* to be against the score: its variance is its own spread plus however far it
sits from the score beyond the score's noise. Where the two agree, both count; where the response
model sits far from what the campaign measured, its weight falls away, and it can never pull the
blend more than one score sd off the score. So customer features (through the score) move bids
as far as the campaign showed they predict lift, and a click model that is ten times too keen is
corrected by the campaign rather than averaged with it.

**The registry.** `attach_uplift(response_model, scorer)` makes a scorer travel with a response model
(as `contact_harm_of` does for the harm model): every `GoalBidder` sharing that model reads it, with
no change to how bidders are built. `uplift_of(model)` finds it.
"""

from __future__ import annotations

import math
import weakref
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from autonomy.contracts import CustomerState

__all__ = [
    "UpliftScore",
    "UpliftScorer",
    "attach_uplift",
    "blend",
    "detach_uplift",
    "uplift_of",
]


@dataclass(frozen=True)
class UpliftScore:
    """One customer's uplift for one action key, from an uplift model."""

    mean: float
    """Rise in the chance of buying in the learning window (a probability; may be negative)."""
    sd: float
    """Spread of `mean` (its standard error), >= 0."""
    n: int = 0
    """How many customers of the campaign it rests on (treated plus control)."""
    own_sd: float | None = None
    """The part of `sd` that is this action's own (its treated customers' noise), leaving out what
    every action of the customer shares (the control group's): the spread to sample between a
    customer's options with. None: all of `sd`."""


@runtime_checkable
class UpliftScorer(Protocol):
    """A fitted uplift model, as a bidder reads it."""

    def score(self, state: CustomerState, action_key: str, now: datetime) -> UpliftScore | None:
        """This customer's uplift for this action key at `now`, or None when the model has no arm
        for the key (the bidder then keeps the response model's reading as it is)."""
        ...


_SCORERS: weakref.WeakKeyDictionary[object, UpliftScorer] = weakref.WeakKeyDictionary()


def attach_uplift(model: object, scorer: UpliftScorer) -> bool:
    """Make `scorer` travel with the response model `model`; False if the model cannot be keyed."""
    try:
        _SCORERS[model] = scorer
    except TypeError:  # not weak-referenceable or not hashable
        return False
    return True


def detach_uplift(model: object) -> None:
    """Forget the scorer of `model`, if any."""
    try:
        _SCORERS.pop(model, None)
    except TypeError:
        return


def uplift_of(model: object) -> UpliftScorer | None:
    """The scorer attached to the response model `model`, or None."""
    try:
        return _SCORERS.get(model)
    except TypeError:
        return None


_TINY_VAR = 1e-18


def blend(model_mean: float, model_sd: float, score: UpliftScore) -> tuple[float, float]:
    """(mean, sd) of the response model's uplift and an uplift score combined by measured precision.

    The score's variance is `score.sd ** 2`. The response model's is its own spread plus its excess
    disagreement with the score, `max((model_mean - score.mean) ** 2 - score.sd ** 2, 0)`: a reading
    that disagrees with the measurement by more than the measurement's noise has shown that its own
    spread understates its error. The result is the precision-weighted mean of the two. Its distance
    from the score is at most `score.sd`, however far off the response model is.
    """
    if not (math.isfinite(model_mean) and math.isfinite(score.mean)):
        return (score.mean, score.sd) if math.isfinite(score.mean) else (model_mean, model_sd)
    v_s = max(score.sd, 0.0) ** 2
    gap = (model_mean - score.mean) ** 2
    v_m = max(model_sd, 0.0) ** 2 + max(gap - v_s, 0.0)
    if v_s <= _TINY_VAR and v_m <= _TINY_VAR:
        return score.mean, 0.0
    if v_s <= _TINY_VAR:
        return score.mean, 0.0
    if v_m <= _TINY_VAR:
        return model_mean, max(model_sd, 0.0)
    w_model = v_s / (v_s + v_m)
    mean = score.mean + w_model * (model_mean - score.mean)
    return mean, math.sqrt(v_s * v_m / (v_s + v_m))
