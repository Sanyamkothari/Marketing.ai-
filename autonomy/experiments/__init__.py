"""Live experiments: the agent tests one question at a time on a small random group (plan-v3 foundation C).

* `model`: the experiment record, with its pick rule and stopping rule written down before the first send.
* `assign`: who is in which arm, by a hash of (experiment id, customer id); only the agent group.
* `policy`: `ExperimentPolicy(inner, book)`, the wrapper that restricts options by arm and logs the probability.
* `sequential`: always-valid intervals, so the test can be read as often as you like.
* `reading`: each arm's gain over the holdout, and the call to continue, stop or crown a winner.
* `book`: the register, the limits (10% of the agent group, 2 per goal), the log, promotion and undo.
* `summary`: the one-line result for the Analyst's briefing.

See docs/v3/EXPERIMENTS.md.
"""

from autonomy.experiments.assign import Assignment, assign, experiment_score
from autonomy.experiments.book import ExperimentBook, ExperimentRefused, PromotionRefused, Review
from autonomy.experiments.model import (
    HOLDOUT_BUCKET,
    MAX_PER_GOAL,
    MAX_TOTAL_SHARE,
    Arm,
    EndKind,
    Experiment,
    ExperimentEvent,
    ExperimentSpec,
    PickRule,
    Promotion,
    Status,
    StoppingRule,
    bucket_of,
    tags_of,
)
from autonomy.experiments.policy import EXPERIMENT_ARM, ExperimentPolicy
from autonomy.experiments.reading import Judgement, Outcomes, Reading, collect_outcomes, judge, read
from autonomy.experiments.sequential import Interval, sequential_difference
from autonomy.experiments.summary import briefing_section, summary_line

__all__ = [
    "EXPERIMENT_ARM",
    "HOLDOUT_BUCKET",
    "MAX_PER_GOAL",
    "MAX_TOTAL_SHARE",
    "Arm",
    "Assignment",
    "EndKind",
    "Experiment",
    "ExperimentBook",
    "ExperimentEvent",
    "ExperimentPolicy",
    "ExperimentRefused",
    "ExperimentSpec",
    "Interval",
    "Judgement",
    "Outcomes",
    "PickRule",
    "Promotion",
    "PromotionRefused",
    "Reading",
    "Review",
    "Status",
    "StoppingRule",
    "assign",
    "briefing_section",
    "bucket_of",
    "collect_outcomes",
    "experiment_score",
    "judge",
    "read",
    "sequential_difference",
    "summary_line",
    "tags_of",
]
