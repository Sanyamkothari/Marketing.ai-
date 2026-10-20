"""The fast learning loop: what happened becomes model updates, each outcome counted once.

`build_bundle(priors)` makes the agent's three models (response, value, fatigue) from the decide
defaults and any Gym or public priors. `update_models(bundle, decisions, events_by_customer,
content_by_id, now, horizon_days)` closes outcome windows and updates the models in place;
`learn_from_rewards` does the same from `Policy.learn`'s rewards. Control outcomes come only from the
randomised holdout unless inverse-propensity weighting of the agent's own do-nothing decisions is
asked for (see `observations`).
"""

from autonomy.learning.bundle import (
    DEFAULT_PRIOR_STRENGTH,
    LearnedValueModel,
    ModelBundle,
    ValueObservation,
    ValueStats,
    build_bundle,
    prior_values,
)
from autonomy.learning.observations import (
    ObservationSet,
    SegmentOf,
    Timeline,
    decision_observations,
    holdout_anchors,
    holdout_observations,
    value_observations,
)
from autonomy.learning.update import LearnReport, learn_from_rewards, update_models

__all__ = [
    "DEFAULT_PRIOR_STRENGTH",
    "LearnReport",
    "LearnedValueModel",
    "ModelBundle",
    "ObservationSet",
    "SegmentOf",
    "Timeline",
    "ValueObservation",
    "ValueStats",
    "build_bundle",
    "decision_observations",
    "holdout_anchors",
    "holdout_observations",
    "learn_from_rewards",
    "prior_values",
    "update_models",
    "value_observations",
]
