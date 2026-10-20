"""WF3 Decide: the inner loop. Fast maths per customer, no LLM.

Goal bidders price each allowed action (`bidders`), using small learnable models of response,
value and fatigue (`models`) and the brief's audience (`audience`). `AutonomousPolicy` (`policy`)
turns the bids into one `Decision`: the hard rules (`rules`) remove what may not go out, the send
time is chosen (`timing`, optionally from a learned `ReadingHourModel`), the attention price and
any pacing surcharge (`pacing`) are charged, Thompson sampling with an exploration floor picks, and
`explain` writes the one-sentence reason. `models.PersonalEffects` is the per-person click model the
agent (`autonomy.runtime.agent`) builds on.
"""

from autonomy.decide import rules
from autonomy.decide.audience import matches
from autonomy.decide.bidders import DEFAULT_CHANNEL_COSTS, GoalBidder, Urgency, days_since_cart, urgency_for
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
    cuped_adjust,
    cuped_theta,
    shrink_to_pool,
)
from autonomy.decide.pacing import Pacer
from autonomy.decide.policy import AutonomousPolicy, Pick, seeded_rng, thompson_with_floor
from autonomy.decide.rules import RuleResult, check_bid
from autonomy.decide.timing import ReadingHourModel, candidate_times, choose_send_time

__all__ = [
    "ANY",
    "DEFAULT_CHANNEL_COSTS",
    "AutonomousPolicy",
    "Beta",
    "BetaResponseModel",
    "FatigueModel",
    "FatigueObservation",
    "GoalBidder",
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
    "candidate_times",
    "check_bid",
    "choose_send_time",
    "cuped_adjust",
    "cuped_theta",
    "days_since_cart",
    "matches",
    "reason",
    "rules",
    "seeded_rng",
    "shrink_to_pool",
    "thompson_with_floor",
    "urgency_for",
]
