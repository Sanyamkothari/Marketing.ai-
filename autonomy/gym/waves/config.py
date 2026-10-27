"""Every number of the repeated-campaign benchmark that is a choice, in one place.

Nothing here is fitted to data. Money is in roubles (the X5 RetailHero currency), time in days.
The worlds (`WORLDS`) differ only in the hidden message effect; the customers and their buying are real.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from autonomy.contracts import Channel

# --- the protocol ----------------------------------------------------------------------------------

HORIZON_DAYS = 84
"""Decision days: 12 weeks. The policy decides every day, within the weekly cap."""
EFFECT_DAYS = 14
"""A message changes buying for 14 days, starting the day after it is sent."""
PULL_FORWARD_DAYS = 30
"""Pulled-forward purchases are missing from the 30 days after the effect window."""
TAIL_DAYS = EFFECT_DAYS + PULL_FORWARD_DAYS
"""How long after a send the truth still moves: the true value counts all of it, even past day 84."""
HOLDOUT_SHARE = 0.10
"""Never contacted by any policy; the only thing a business could measure against."""
WEEKLY_CAP = 2
"""The Gym's envelope cap: at most this many contacts per customer in 7 days."""
DEFAULT_CUSTOMERS = 30_000

# --- money (RUB) -----------------------------------------------------------------------------------

MARGIN = 0.25
"""Margin on a purchase. The CLI passes `scripts.v2_realbench.adapters.X5_MARGIN`, which is the same 0.25."""
CHANNEL_COST: dict[Channel, float] = {Channel.SMS: 2.0, Channel.EMAIL: 0.3}
"""Cost of one message. SMS is `adapters.X5_SMS_COST`; e-mail is a typical bulk price."""
UNSUBSCRIBE_COST = 50.0
"""What a customer who opts out of a channel costs the shop beyond the messages inside the run: the margin
its later messages would have made. About one extra purchase of a light buyer."""
COMPLAINT_COST = 150.0
"""A spam complaint, on top of the unsubscribe it comes with (deliverability damage)."""
COMPLAINT_SHARE = 0.10
"""Share of unsubscribes that are also complaints (the Gym's value)."""
EMAIL_CONSENT_SHARE = 0.60
"""Share of customers who have an e-mail address on file and agreed to e-mail (synthetic: X5 has no
e-mail data). SMS consent is granted to every customer, as in the one-shot bench."""

# --- shape of the hidden effect, shared by every world ----------------------------------------------

_WEIGHT_TOTAL = EFFECT_DAYS * (EFFECT_DAYS + 1) / 2
EFFECT_WEIGHTS: tuple[float, ...] = tuple(
    (EFFECT_DAYS + 1 - j) / _WEIGHT_TOTAL for j in range(1, EFFECT_DAYS + 1)
)
"""Share of a message's effect that lands on day j after the send (j = 1..14): front-loaded, so 73% of it
is in the first 7 days. Sums to 1."""
BASE_PRIOR_DAYS = 14.0
"""Pseudo-days of the population's average rate that a customer's own pre-cutoff rate is shrunk toward."""
MAX_DAILY_PROB = 0.9


@dataclass(frozen=True)
class WorldSpec:
    """One hidden truth: how much a message moves buying, for whom, and what repeating it costs."""

    name: str
    description: str
    mean_lift: float
    """Average relative lift of the 14-day purchase rate for one SMS to a rested customer (0.07 = +7%)."""
    depth: int = 3
    """Depth of the hidden tree that spreads the lift over customers (0: everyone gets the same)."""
    negative_share: float = 0.0
    """Share of customers whose lift is negative (sleeping dogs): a message lowers their buying."""
    idiosyncratic_sd: float = 0.0
    """Log-sd of a per-customer factor no feature explains (so the best feature rule is not the oracle)."""
    fatigue: float = 0.8
    """A positive effect is multiplied by this once for each message the customer got in the last 14 days."""
    unsubscribe_base: float = 0.002
    """Chance a message ends in an opt-out from that channel, for a customer with no other recent contact."""
    unsubscribe_per_contact: float = 0.5
    """The chance is multiplied by (1 + this x contacts in the 7 days before the message)."""
    dog_unsubscribe_multiplier: float = 1.0
    """Customers with a negative lift opt out this many times as often."""
    pull_forward: float = 0.4
    """Share of the incremental purchases that are taken out of the 30 days after the window."""
    email_effect: float = 0.7
    """An e-mail moves buying this share as much as an SMS."""

    def with_overrides(self, **kwargs: Any) -> WorldSpec:
        return replace(self, **kwargs)


WORLDS: dict[str, WorldSpec] = {
    w.name: w
    for w in (
        WorldSpec("null", "Messages change nothing at all.", mean_lift=0.0, depth=0),
        WorldSpec(
            "homogeneous_small",
            "Every customer gets the same small lift (+4%): no one is worth targeting for their own sake.",
            mean_lift=0.04,
            depth=0,
            fatigue=0.85,
        ),
        WorldSpec(
            "heterogeneous",
            "The lift (average +7%) is a depth-3 tree of the customer's real features, plus a personal factor.",
            mean_lift=0.07,
            idiosyncratic_sd=0.25,
        ),
        WorldSpec(
            "sleeping_dogs",
            "As heterogeneous, but 15% of customers are put off by messages and opt out twice as often.",
            mean_lift=0.07,
            negative_share=0.15,
            idiosyncratic_sd=0.25,
            dog_unsubscribe_multiplier=2.0,
        ),
        WorldSpec(
            "fatigue_heavy",
            "As heterogeneous, but each recent message halves the next one and opt-outs climb fast with volume.",
            mean_lift=0.07,
            idiosyncratic_sd=0.25,
            fatigue=0.5,
            unsubscribe_base=0.006,
            unsubscribe_per_contact=1.5,
        ),
    )
}


def get_world(name: str) -> WorldSpec:
    try:
        return WORLDS[name]
    except KeyError as exc:
        raise KeyError(f"unknown world {name!r}; known: {', '.join(WORLDS)}") from exc
