"""The scoring run's own prediction as the adjustment (Plan K M135, built early as Plan J's DEC-1324).

M102 (`engine.measurement.continuous`) narrows an amount's interval with an amount each customer had
before the campaign (CUPED). A yes/no outcome - churn, conversion - had nothing of the kind: a past
column predicts it weakly. But the scoring run that chose who to contact already predicted, **before**
the campaign, each customer's chance of the outcome: the propensity run's probability (its
`actions.score_field`, `churn_prob` for telco churn) or, on an uplift run, `p_control` - the chance of
the outcome without contact. A number fixed before the treatment cannot contain the treatment's effect,
so it is a valid covariate (CUPAC, DoorDash 2020; MLRATE, Meta 2021). Nothing is trained here: the
prediction the product already computed is reused (compute once).

**Registered in advance.** A test plan opts in with `covariate_source: run_score`
(`engine.measurement.plan.TestPlanInput`); `covariate_column` is then null. Only a measurement whose
registered plan says so uses it (`engine.measurement.measure.measure_campaign`); measured with it
against a plan that did not register it is `TEST_PLAN_CHANGED`, like any other change of plan.

**Where it comes from.** :func:`load_run_prediction` reads the campaign's one scoring run: its
`run.json` (when it finished) and the prediction column of its `scores.parquet` (only the key
column(s) and that column are read). A campaign with no run (an audit, a programme readout) or with
several runs is refused with a sentence: the assignment does not record which run scored which
customer, so picking one would be a guess (DEC-1324 (d)).

**The join.** `engine.uplift.incrementality.measure_incrementality(run_prediction=...)` joins the
prediction to the measured customers on every key column (the same text keys it joins the outcomes
with, `engine.keys` for a composite key), never by position. A measured customer with no prediction
keeps their row: the prediction is set to the mean of the known ones among the measured customers
(M102's rule; it does not depend on the arm, so the estimate stays unbiased) and counted in
`rows_covariate_missing`.

**Point in time.** The prediction must have been made before the campaign went out: a run that
finished after the campaign's `treatment_start`, or after the day a customer's own treatment date names
(compared by day: a date without a time is read as any moment of that day, and the campaign record
already holds the exact send time), is refused with `COVARIATE_NOT_BEFORE_CAMPAIGN`, never quietly run.

**The estimate** is M102's `adjusted_difference`, unchanged: the pooled within-arm slope, Welch's
interval on the adjusted values. On a yes/no outcome the arrays are 0/1 and the adjusted difference is a
difference in rates (:func:`adjust_by_prediction`). `variance_reduction` is always reported, negative
too: the gain is measured, never promised.

Every sentence this module writes is plain language (`engine.pilot.plain.jargon_in` finds nothing in it).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

import numpy as np

from engine.measurement.continuous import AdjustedDifference, adjusted_difference

if TYPE_CHECKING:
    import pandas as pd
    from numpy.typing import NDArray

    from engine.measurement.campaign import Campaign
    from engine.storage import Storage

__all__ = [
    "COVARIATE_SOURCE_COLUMN",
    "COVARIATE_SOURCE_RUN_SCORE",
    "PREDICTION_COLUMN",
    "CovariateSource",
    "PredictionAdjustment",
    "RunPrediction",
    "adjust_by_prediction",
    "load_run_prediction",
    "prediction_column_for",
]

CovariateSource = Literal["column", "run_score"]
"""Where the adjustment's covariate comes from: a column of the outcomes file (M102) or the run's prediction."""
COVARIATE_SOURCE_COLUMN: Final[str] = "column"
COVARIATE_SOURCE_RUN_SCORE: Final[str] = "run_score"

PREDICTION_COLUMN: Final[str] = "prediction"
"""The column of :attr:`RunPrediction.frame` holding the prediction, whatever the run called it."""

UPLIFT_PREDICTION_COLUMN: Final[str] = "p_control"
"""An uplift run's chance of the outcome without contact (`engine.uplift.flow.P_CONTROL_COLUMN`)."""


@dataclass(frozen=True)
class RunPrediction:
    """A scoring run's prediction of each customer's outcome, and when it was made."""

    frame: pd.DataFrame
    """The key column(s) and :data:`PREDICTION_COLUMN`, one row per scored customer."""
    run_id: str
    column: str
    """The run's own name for the prediction: its `actions.score_field`, or `p_control` on an uplift run."""
    predicted_at: datetime
    """When the run finished: the prediction existed from then on."""


@dataclass(frozen=True)
class PredictionAdjustment:
    """`adjust_by_prediction`'s answer: the adjusted difference (or why not), and the customers filled in."""

    adjusted: AdjustedDifference | None
    rows_missing: int
    note: str | None


def prediction_column_for(storage: Storage, run_id: str) -> str:
    """The column of `run_id`'s `scores.parquet` holding its prediction; `ValueError` when it cannot be said.

    An uplift run's `p_control` (its chance of the outcome without contact); a propensity run's
    `actions.score_field`, read from the run's own `run_config.json` - never assumed.
    """
    from engine.config import ProblemType, ResolvedConfig
    from engine.contracts import RunRecord
    from engine.runs import RUN_CONFIG_FILENAME, RUN_FILENAME
    from engine.storage import StorageError, run_key

    try:
        record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
    except StorageError as exc:
        raise ValueError(
            "The campaign's scoring run is no longer stored, so its prediction cannot be read."
        ) from exc
    if record.problem_type is ProblemType.UPLIFT:
        return UPLIFT_PREDICTION_COLUMN
    try:
        resolved = storage.read_model(run_key(run_id, RUN_CONFIG_FILENAME), ResolvedConfig)
    except StorageError as exc:
        raise ValueError(
            "The campaign's scoring run kept no record of its settings, so which column holds its prediction "
            "cannot be said."
        ) from exc
    return resolved.config.actions.score_field


def load_run_prediction(storage: Storage, campaign: Campaign) -> RunPrediction:
    """The prediction of `campaign`'s scoring run, read from its `scores.parquet`.

    Raises `ValueError` with a plain sentence when the campaign has no scoring run or several, when the
    run's scores or its prediction column are missing; `CovariateNotBeforeCampaignError` when the run
    never finished (its prediction cannot be shown to come from before the campaign).
    """
    import pandas as pd

    from engine.config import key_columns
    from engine.contracts import RunRecord
    from engine.measurement.continuous import CovariateNotBeforeCampaignError
    from engine.runs import RUN_FILENAME
    from engine.stages.export import SCORES_PARQUET
    from engine.storage import StorageError, run_key

    if not campaign.run_ids:
        raise ValueError(
            "This campaign was not made from one of our scoring runs, so there is no prediction from before "
            "the campaign to adjust with."
        )
    if len(campaign.run_ids) > 1:
        raise ValueError(
            "This campaign was made from several scoring runs and does not record which run scored which "
            "customer, so whose prediction to use cannot be said."
        )
    run_id = campaign.run_ids[0]
    column = prediction_column_for(storage, run_id)
    record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
    if record.finished_at is None:
        raise CovariateNotBeforeCampaignError(
            "The campaign's scoring run has not finished, so its prediction cannot be shown to come from "
            "before the campaign."
        )
    keys = list(key_columns(campaign.primary_key))
    try:
        payload = storage.read_bytes(run_key(run_id, SCORES_PARQUET))
    except StorageError as exc:
        raise ValueError(
            "The campaign's scoring run has no scores file, so its prediction cannot be read."
        ) from exc
    try:
        frame = pd.read_parquet(io.BytesIO(payload), columns=[*keys, column])
    except (KeyError, ValueError) as exc:  # pyarrow names a missing column one way or the other
        raise ValueError(
            f"The campaign's scoring run has no {column!r} column in its scores, so its prediction cannot be read."
        ) from exc
    frame = frame.rename(columns={column: PREDICTION_COLUMN})
    return RunPrediction(frame=frame, run_id=run_id, column=column, predicted_at=record.finished_at)


def adjust_by_prediction(
    outcome: NDArray[np.float64],
    prediction: NDArray[np.float64],
    *,
    usable: NDArray[np.bool_],
    treated: NDArray[np.bool_],
) -> PredictionAdjustment:
    """M102's adjusted difference of `outcome` (0/1 or an amount) by `prediction`, over the `usable` customers.

    All arrays are aligned on the measured customers (already joined on the key). A usable customer whose
    prediction is unknown (NaN) is given the mean of the known ones and counted in `rows_missing`. None
    with a plain `note` when no usable customer has a prediction or it is the same for all of them;
    None without a note when an arm has fewer than two customers (nothing to compare, said elsewhere).
    """
    counted = np.asarray(usable, dtype=bool)
    arm = np.asarray(treated, dtype=bool)
    values = np.asarray(prediction, dtype=np.float64)
    y = np.asarray(outcome, dtype=np.float64)
    known = counted & np.isfinite(values)
    missing = int((counted & ~known).sum())
    if not known.any():
        return PredictionAdjustment(
            None,
            missing,
            "No measured customer has a prediction from the scoring run, so nothing could be adjusted.",
        )
    if int((counted & arm).sum()) < 2 or int((counted & ~arm).sum()) < 2:
        return PredictionAdjustment(None, missing, None)
    same = (
        "The scoring run predicted the same for every measured customer, so its prediction cannot narrow "
        "the range and the estimate is not adjusted."
    )
    if float(np.ptp(values[known])) == 0.0:  # exactly constant: no slope, whatever rounding the mean leaves
        return PredictionAdjustment(None, missing, same)
    filled = np.where(known, values, float(values[known].mean()))
    adjusted = adjusted_difference(
        y[counted & arm], filled[counted & arm], y[counted & ~arm], filled[counted & ~arm]
    )
    note = same if adjusted is None else None
    return PredictionAdjustment(adjusted, missing, note)
