"""The Gym: simulated customers with planted truth, used to prove and regression-test the agent.

`GymWorld` simulates a shop's customers and plants a `PlantedTruth` the policy cannot see; the
scenario library names the businesses to test on; `run_policy`, `compare` and `pass_criteria` score
a policy against the holdout, the baselines and the oracle. `staged_criteria` is the honest, staged
bar (Safe, Useful, Smart) judged against `BestFixedRule` and the `SegmentOracle` ceiling; see
docs/v2/GYM_BAR.md. The command line lives in
`autonomy.gym.run` and is not imported here, so `python -m autonomy.gym.run` stays clean.
"""

from autonomy.gym.api import PERSONAS, GroundTruth, GymEnvironment, ScenarioSpec
from autonomy.gym.baselines import (
    BestFixedRule,
    ContactEveryone,
    OraclePolicy,
    RuleBaseline,
    SegmentOracle,
    observable_segment,
)
from autonomy.gym.scenarios import (
    GYM_OFFERS,
    PLANTED_HARM,
    SCENARIOS,
    default_brief,
    default_content,
    get_scenario,
)
from autonomy.gym.score import (
    BEST_FIXED,
    SEGMENT_ORACLE,
    Check,
    Comparison,
    PassReport,
    PolicyFactory,
    PolicyLike,
    ScoreCard,
    SimWorld,
    Stage,
    StagedReport,
    WorldFactory,
    compare,
    gym_world,
    pass_criteria,
    run_policy,
    staged_criteria,
)
from autonomy.gym.truth import DEFAULT_START, Evaluation, PlantedTruth
from autonomy.gym.world import ContactRecord, GymWorld

__all__ = [
    "BEST_FIXED",
    "DEFAULT_START",
    "GYM_OFFERS",
    "PERSONAS",
    "PLANTED_HARM",
    "SCENARIOS",
    "SEGMENT_ORACLE",
    "BestFixedRule",
    "Check",
    "Comparison",
    "ContactEveryone",
    "ContactRecord",
    "Evaluation",
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
    "SegmentOracle",
    "SimWorld",
    "Stage",
    "StagedReport",
    "WorldFactory",
    "compare",
    "default_brief",
    "default_content",
    "get_scenario",
    "gym_world",
    "observable_segment",
    "pass_criteria",
    "run_policy",
    "staged_criteria",
]
