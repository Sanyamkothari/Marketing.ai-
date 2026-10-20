"""WF3 Decide: the inner loop. Fast maths per customer, no LLM.

Goal bidders price each allowed action (`bidders`), using small learnable models of response,
value and fatigue (`models`) and the brief's audience (`audience`). `AutonomousPolicy` (`policy`)
turns the bids into one `Decision`: the hard rules (`rules`) remove what may not go out, the send
time is chosen (`timing`), the attention price is charged, Thompson sampling with an exploration
floor picks, and `explain` writes the one-sentence reason.
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
    ResponseModel,
    ResponseObservation,
    ValueModel,
    action_key,
)
from autonomy.decide.policy import AutonomousPolicy, Pick, seeded_rng, thompson_with_floor
from autonomy.decide.rules import RuleResult, check_bid
from autonomy.decide.timing import candidate_times, choose_send_time

__all__ = [
    "ANY",
    "DEFAULT_CHANNEL_COSTS",
    "AutonomousPolicy",
    "Beta",
    "BetaResponseModel",
    "FatigueModel",
    "FatigueObservation",
    "GoalBidder",
    "Pick",
    "ResponseModel",
    "ResponseObservation",
    "RuleResult",
    "Urgency",
    "ValueModel",
    "action_key",
    "candidate_times",
    "check_bid",
    "choose_send_time",
    "days_since_cart",
    "matches",
    "reason",
    "rules",
    "seeded_rng",
    "thompson_with_floor",
    "urgency_for",
]
