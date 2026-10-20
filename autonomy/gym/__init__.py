"""The Gym: simulated customers with planted truth, used to prove and regression-test the agent.

`GymWorld` simulates a shop's customers and plants a `PlantedTruth` the policy cannot see; the
scenario library names the businesses to test on; `run_policy`, `compare` and `pass_criteria` score
a policy against the holdout, the baselines and the oracle. The command line lives in
`autonomy.gym.run` and is not imported here, so `python -m autonomy.gym.run` stays clean.
"""

from autonomy.gym.api import PERSONAS, GroundTruth, GymEnvironment, ScenarioSpec
from autonomy.gym.baselines import ContactEveryone, OraclePolicy, RuleBaseline
from autonomy.gym.scenarios import (
    GYM_OFFERS,
    PLANTED_HARM,
    SCENARIOS,
    default_brief,
    default_content,
    get_scenario,
)
from autonomy.gym.score import (
    Check,
    Comparison,
    PassReport,
    PolicyFactory,
    PolicyLike,
    ScoreCard,
    SimWorld,
    WorldFactory,
    compare,
    gym_world,
    pass_criteria,
    run_policy,
)
from autonomy.gym.truth import DEFAULT_START, PlantedTruth
from autonomy.gym.world import ContactRecord, GymWorld

__all__ = [
    "DEFAULT_START",
    "GYM_OFFERS",
    "PERSONAS",
    "PLANTED_HARM",
    "SCENARIOS",
    "Check",
    "Comparison",
    "ContactEveryone",
    "ContactRecord",
    "GroundTruth",
    "GymEnvironment",
    "GymWorld",
    "OraclePolicy",
    "PassReport",
    "PlantedTruth",
    "PolicyFactory",
    "PolicyLike",
    "RuleBaseline",
    "ScenarioSpec",
    "ScoreCard",
    "SimWorld",
    "WorldFactory",
    "compare",
    "default_brief",
    "default_content",
    "get_scenario",
    "gym_world",
    "pass_criteria",
    "run_policy",
]
