"""WF3 Decide: the inner loop. Fast maths per customer, no LLM.

Goal bidders price each allowed action (`bidders`), using small learnable models of response,
value and fatigue (`models`) and the brief's audience (`audience`). `AutonomousPolicy` (`policy`)
turns the bids into one `Decision`: the hard rules (`rules`) remove what may not go out, the send
time is chosen (`timing`, optionally from a learned `ReadingHourModel`), the attention price and
any pacing surcharge (`pacing`) are charged, Thompson sampling with an exploration floor picks, and
`explain` writes the one-sentence reason. `models.PersonalEffects` is the per-person click model the
agent (`autonomy.runtime.agent`) builds on. Bids can name a product (`ItemChoice`, from an
`ItemLookup`; refills get `refill_boost` while `refill_window_open`) and a format's asset
(`asset_lookup`); `action_key` adds the format as a fourth part (`base_key`, `key_format`).

Contacting less: `models.HazardModel` learns P(unsubscribe or complaint | channel, messages this
week), which the agent turns into a per-channel attention price. `bidders` prices offers against
the plain message (`shrink_offer_extra`, `offer_sensitivity_factor`, `organic_chance`) and charges
the dent contact makes in some customers' own buying (`ContactHarm`, found by `contact_harm_of`).
`ContactHarm` is a per-person posterior read against the holdout's organic trend (at least
`HARM_MIN_HOLDOUT` people); `leave_alone` says when messages credibly lower someone's buying, and
`leave_alone_retry` lets them back in now and then so a wrong verdict can be undone.
`policy.Market` is one customer's priced options before the draw.
"""

from autonomy.decide import rules
from autonomy.decide.audience import matches
from autonomy.decide.bidders import (
    DEFAULT_CHANNEL_COSTS,
    EVIDENCE_HALF_LIFE_DAYS,
    HARM_MIN_HOLDOUT,
    LEAVE_ALONE_CREDIBLE,
    LEAVE_ALONE_RETRY,
    AssetOf,
    ContactHarm,
    GoalBidder,
    HarmEvidence,
    HarmFit,
    ItemChoice,
    ItemLookup,
    Urgency,
    asset_lookup,
    contact_harm_of,
    days_since_cart,
    leave_alone,
    leave_alone_retry,
    needs_item,
    offer_sensitivity_factor,
    organic_chance,
    refill_boost,
    refill_window_open,
    shrink_offer_extra,
    urgency_for,
    uses_refill_scores,
)
from autonomy.decide.explain import reason
from autonomy.decide.models import (
    ANY,
    Beta,
    BetaResponseModel,
    FatigueModel,
    FatigueObservation,
    HazardModel,
    PersonalEffects,
    ResponseModel,
    ResponseObservation,
    ValueModel,
    action_key,
    base_key,
    cuped_adjust,
    cuped_theta,
    key_format,
    shrink_to_pool,
)
from autonomy.decide.pacing import Pacer
from autonomy.decide.policy import AutonomousPolicy, Market, Pick, seeded_rng, thompson_with_floor
from autonomy.decide.rules import RuleResult, check_bid
from autonomy.decide.timing import ReadingHourModel, candidate_times, choose_send_time

__all__ = [
    "ANY",
    "DEFAULT_CHANNEL_COSTS",
    "EVIDENCE_HALF_LIFE_DAYS",
    "HARM_MIN_HOLDOUT",
    "LEAVE_ALONE_CREDIBLE",
    "LEAVE_ALONE_RETRY",
    "AssetOf",
    "AutonomousPolicy",
    "Beta",
    "BetaResponseModel",
    "ContactHarm",
    "FatigueModel",
    "FatigueObservation",
    "GoalBidder",
    "HarmEvidence",
    "HarmFit",
    "HazardModel",
    "ItemChoice",
    "ItemLookup",
    "Market",
    "Pacer",
    "PersonalEffects",
    "Pick",
    "ReadingHourModel",
    "ResponseModel",
    "ResponseObservation",
    "RuleResult",
    "Urgency",
    "ValueModel",
    "action_key",
    "asset_lookup",
    "base_key",
    "candidate_times",
    "check_bid",
    "choose_send_time",
    "contact_harm_of",
    "cuped_adjust",
    "cuped_theta",
    "days_since_cart",
    "key_format",
    "leave_alone",
    "leave_alone_retry",
    "matches",
    "needs_item",
    "offer_sensitivity_factor",
    "organic_chance",
    "reason",
    "refill_boost",
    "refill_window_open",
    "rules",
    "seeded_rng",
    "shrink_offer_extra",
    "shrink_to_pool",
    "thompson_with_floor",
    "urgency_for",
    "uses_refill_scores",
]
