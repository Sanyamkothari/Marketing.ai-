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
"""

from autonomy.decide import rules
from autonomy.decide.audience import matches
from autonomy.decide.bidders import (
    DEFAULT_CHANNEL_COSTS,
    AssetOf,
    GoalBidder,
    ItemChoice,
    ItemLookup,
    Urgency,
    asset_lookup,
    days_since_cart,
    needs_item,
    refill_boost,
    refill_window_open,
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
from autonomy.decide.policy import AutonomousPolicy, Pick, seeded_rng, thompson_with_floor
from autonomy.decide.rules import RuleResult, check_bid
from autonomy.decide.timing import ReadingHourModel, candidate_times, choose_send_time

__all__ = [
    "ANY",
    "DEFAULT_CHANNEL_COSTS",
    "AssetOf",
    "AutonomousPolicy",
    "Beta",
    "BetaResponseModel",
    "FatigueModel",
    "FatigueObservation",
    "GoalBidder",
    "ItemChoice",
    "ItemLookup",
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
    "cuped_adjust",
    "cuped_theta",
    "days_since_cart",
    "key_format",
    "matches",
    "needs_item",
    "reason",
    "refill_boost",
    "refill_window_open",
    "rules",
    "seeded_rng",
    "shrink_to_pool",
    "thompson_with_floor",
    "urgency_for",
    "uses_refill_scores",
]
