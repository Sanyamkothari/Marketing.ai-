"""Zero-touch setup: from a connected shop's history to a sized goal and a signed mandate.

* `opportunities`: goal proposals sized in money from the shop's own history (refill due, first to
  second purchase, win-back, VIP at risk, cart and browse), each with a range and one sentence of
  why, ranked, the best preselected (`scan_opportunities`).
* `limits`: the hard limits read from the same history, shown as "what we found -> what we will
  use" (`learn_limits`), and the one call that turns them into a signed mandate
  (`mandate_from_limits`, `sign_as_owner`, with the documented single-owner path).
* `history`: the single pass over events and states both read (`ShopHistory`).
* `priors`: the only outside numbers allowed, each with where it is written down.

The HTTP routes are in `api/v2/onboarding.py`. See docs/v2/ZERO_TOUCH.md, items 1 and 2.
"""

from autonomy.onboarding.history import ShopHistory
from autonomy.onboarding.limits import (
    SINGLE_OWNER_NOTE,
    SINGLE_OWNER_POLICY,
    LearnedLimit,
    LearnedLimits,
    MandateTerms,
    learn_limits,
    mandate_from_limits,
    offer_for,
    sign_as_owner,
)
from autonomy.onboarding.opportunities import (
    Basis,
    MarginReading,
    OpportunityScan,
    Proposal,
    margin_reading,
    proposal_goal,
    scan_opportunities,
)
from autonomy.onboarding.priors import MESSAGE_LIFT, CitedNumber

__all__ = [
    "MESSAGE_LIFT",
    "SINGLE_OWNER_NOTE",
    "SINGLE_OWNER_POLICY",
    "Basis",
    "CitedNumber",
    "LearnedLimit",
    "LearnedLimits",
    "MandateTerms",
    "MarginReading",
    "OpportunityScan",
    "Proposal",
    "ShopHistory",
    "learn_limits",
    "mandate_from_limits",
    "margin_reading",
    "offer_for",
    "proposal_goal",
    "scan_opportunities",
    "sign_as_owner",
]
