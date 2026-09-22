"""The fitted model, everything selection-dependent already settled.

This module is the **only** place in the engine that fits a probability calibrator or chooses a
decision threshold, and it does both at **train** time on the **validation** split. The evaluate
stage was deliberately narrowed to a measurement function: it reads `threshold`, `threshold_mode`,
`threshold_detail` and `calibration` off the scorer and measures at them. Nothing downstream fits
anything, so nothing downstream can select on the hold-out.

> **INVARIANT - TEST IS FINAL-DECISION-ONLY.** No function here ever sees the test split. The two
> arguments `fit_scorer` and `fit_baseline_scorer` take are the *train* and *validation* frames,
> and the threshold and the calibrator are fitted on validation alone. Touching test here would
> invalidate every number the run reports, because the hold-out would then have chosen the
> operating point it is supposed to be judging.

A scorer is also the unit of *re-scoring*. The champion rule compares the incumbent and the
challenger on one held-out frame rather than against a stored number, which is only possible if a
stored model can be reconstructed exactly: the predictor directory plus `scorer.json` is that
reconstruction. `scorer.json` carries the identity of the model, the feature list it was fitted on,
its two class labels, the metric it was optimised for, the threshold with its mode and its
explanation, the calibration summary, the calibrator's own parameters (isotonic knots, or Platt's
coefficient and intercept) and the `recipe_hash` of the recipe that produced it. `load_scorer`
rebuilds an `AutoGluonScorer` from those two things and nothing else.

The calibrator is applied with numpy rather than by pickling an sklearn estimator: `np.interp` over
the isotonic knots is exactly what `IsotonicRegression(out_of_bounds="clip").predict` computes, and
the Platt transform is one sigmoid. That keeps the stored model readable, version-independent and
reproducible to the last bit, which a pickled estimator would not be.

What the scorer exposes, and to whom:

| Member | Who reads it |
|---|---|
| `problem_type`, `target_column`, `primary_metric`, `classes` | `engine.stages.evaluate.Scorer` |
| `threshold`, `threshold_mode`, `threshold_detail`, `calibration` | `engine.stages.evaluate.Scorer` |
| `predict_proba` (classification) / `predict` (regression) | `engine.stages.evaluate` |
| `score`, `raw_score`, `predicted_positive` | the train stage, and M4's score flow |
| `can_score` | the champion rule, before it re-scores a stored model on a new frame |
| `model_name`, `display_name`, `family`, `feature_columns`, `recipe_hash` | artefacts and log lines |

`sklearn`, `numpy` and `autogluon` are imported inside function bodies: importing the engine must
not pull a heavy library in (`tests/integration/test_engine_imports.py`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from engine.config import Calibration, Metric, ModelFamily, ProblemType, ThresholdMode
from engine.contracts import CalibrationSummary
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    import numpy.typing as npt
    import pandas as pd
    from autogluon.tabular import TabularPredictor
    from sklearn.pipeline import Pipeline

    from engine.config import EvaluationConfig, Recipe, ThresholdConfig
    from engine.storage import Storage

    FloatArray = npt.NDArray[np.float64]
    BoolArray = npt.NDArray[np.bool_]


__all__ = [
    "BASELINE_DISPLAY_NAME",
    "BASELINE_MODEL_NAME",
    "BASELINE_REGRESSION_DISPLAY_NAME",
    "MIN_CALIBRATION_ROWS",
    "SCORER_FILENAME",
    "SCORER_SCHEMA_VERSION",
    "AutoGluonScorer",
    "BaselineScorer",
    "CalibratorState",
    "ScorerState",
    "TrainError",
    "apply_calibrator",
    "brier_score",
    "choose_threshold",
    "fit_baseline_scorer",
    "fit_calibrator",
    "fit_scorer",
    "load_scorer",
    "to_numpy_dtypes",
]

_LOGGER = get_logger(__name__)

SCORER_FILENAME: Final[str] = "scorer.json"
"""File written beside the AutoGluon predictor, holding everything AutoGluon does not store."""

SCORER_SCHEMA_VERSION: Final[int] = 1
"""Bumped whenever `ScorerState` changes shape, so an old file is recognised rather than guessed at."""

MIN_CALIBRATION_ROWS: Final[int] = 50
"""Validation rows a calibrator needs. Below this a fitted curve describes noise, so none is fitted."""

FIXED_THRESHOLD: Final[float] = 0.50
"""The `fixed` decision threshold, and the fallback whenever nothing can be chosen honestly."""

THRESHOLD_DECIMALS: Final[int] = 4
"""Decimal places the chosen threshold is rounded to; `evaluate` rounds to the same place."""

BASELINE_MODEL_NAME: Final[str] = "baseline"
BASELINE_DISPLAY_NAME: Final[str] = "baseline (logistic regression)"
BASELINE_REGRESSION_DISPLAY_NAME: Final[str] = "baseline (linear regression)"

REGRESSION_THRESHOLD_DETAIL: Final[str] = "Not applicable to a regression model."

LabelValue = bool | int | float | str
"""A class label as it survives a JSON round trip. Numpy scalars are coerced before they get here."""


class TrainError(Exception):
    """Training cannot produce an honest model or artefact.

    `code` is machine-readable and `message` is business language, the same shape as the evaluate
    stage's `EvaluationError`. It lives in this module rather than in the train stage because the
    train stage imports this one; when `engine/errors.py` lands (M3 step 0, owner R) both should
    collapse into the shared `EngineError` without changing a single code string.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# What is persisted
# ---------------------------------------------------------------------------
class CalibratorState(BaseModel):
    """The fitted calibrator, as numbers rather than as a pickled estimator.

    `isotonic` keeps the step function's knots, which is the whole of what
    `IsotonicRegression(out_of_bounds="clip")` knows; `platt` keeps the single coefficient and
    intercept of the fitted logistic. `none` keeps nothing and is the identity.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Calibration = Calibration.NONE
    x_thresholds: tuple[float, ...] = ()
    y_thresholds: tuple[float, ...] = ()
    coef: float | None = None
    intercept: float | None = None

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.method is Calibration.ISOTONIC:
            if not self.x_thresholds or len(self.x_thresholds) != len(self.y_thresholds):
                raise ValueError("An isotonic calibrator needs matching, non-empty knot arrays.")
        elif self.method is Calibration.PLATT and (self.coef is None or self.intercept is None):
            raise ValueError("A Platt calibrator needs both a coefficient and an intercept.")
        return self


class ScorerState(BaseModel):
    """`model/scorer.json`: everything a fitted model carries that AutoGluon does not store.

    Written once by the train stage and read back by `load_scorer`, which is how the champion rule
    re-scores a stored model on a new hold-out frame. `recipe_hash` ties the file back to the
    recipe that produced it, so a stored model can always be traced to the choices that made it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCORER_SCHEMA_VERSION
    kind: Literal["autogluon", "baseline"]
    model_name: str
    display_name: str
    family: ModelFamily | None = None
    problem_type: ProblemType
    target_column: str
    feature_columns: tuple[str, ...]
    classes: tuple[LabelValue, LabelValue] | None = None
    primary_metric: Metric
    threshold: float
    threshold_mode: ThresholdMode
    threshold_detail: str
    calibrator: CalibratorState = CalibratorState()
    calibration: CalibrationSummary | None = None
    recipe_hash: str
    autogluon_version: str = ""
    trained_at: datetime


# ---------------------------------------------------------------------------
# Calibration: fitted on validation, applied with numpy
# ---------------------------------------------------------------------------
def fit_calibrator(
    raw: FloatArray, actual: BoolArray, method: Calibration
) -> tuple[CalibratorState, str | None]:
    """Fit `method` on the **validation** split and return it with the reason it could not be fitted.

    The second element is `None` on success and a plain-language reason otherwise. A calibrator that
    could not be fitted degrades to the identity and the summary says `none`: the configured method
    is never claimed for work that did not happen.
    """
    import numpy as np

    if method is Calibration.NONE:
        return CalibratorState(), None
    if raw.size < MIN_CALIBRATION_ROWS:
        return CalibratorState(), (
            f"the validation split has {raw.size} rows, fewer than the {MIN_CALIBRATION_ROWS} a "
            "calibration curve needs"
        )
    if len(np.unique(actual)) < 2:
        return CalibratorState(), "the validation split holds one outcome only"
    targets = actual.astype(np.float64)
    if method is Calibration.ISOTONIC:
        from sklearn.isotonic import IsotonicRegression

        fitted = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(raw, targets)
        knots_x = np.asarray(fitted.X_thresholds_, dtype=np.float64)
        knots_y = np.asarray(fitted.y_thresholds_, dtype=np.float64)
        return (
            CalibratorState(
                method=Calibration.ISOTONIC,
                x_thresholds=tuple(float(value) for value in knots_x),
                y_thresholds=tuple(float(value) for value in knots_y),
            ),
            None,
        )
    from sklearn.linear_model import LogisticRegression

    platt = LogisticRegression(max_iter=1000).fit(raw.reshape(-1, 1), targets)
    return (
        CalibratorState(
            method=Calibration.PLATT,
            coef=float(np.asarray(platt.coef_, dtype=np.float64).ravel()[0]),
            intercept=float(np.asarray(platt.intercept_, dtype=np.float64).ravel()[0]),
        ),
        None,
    )


def apply_calibrator(state: CalibratorState, raw: FloatArray) -> FloatArray:
    """Map raw model probabilities onto calibrated ones, using only the stored numbers.

    `np.interp` clamps outside the knot range, which is exactly `out_of_bounds="clip"`, so a scorer
    reconstructed from `scorer.json` reproduces the fitted curve rather than approximating it.
    """
    import numpy as np

    values = np.asarray(raw, dtype=np.float64)
    if state.method is Calibration.ISOTONIC:
        interpolated = np.interp(
            values,
            np.asarray(state.x_thresholds, dtype=np.float64),
            np.asarray(state.y_thresholds, dtype=np.float64),
        )
        return np.asarray(interpolated, dtype=np.float64)
    if state.method is Calibration.PLATT:
        coef = 0.0 if state.coef is None else state.coef
        intercept = 0.0 if state.intercept is None else state.intercept
        logits = np.asarray(values * coef + intercept, dtype=np.float64)
        return np.asarray(1.0 / (1.0 + np.exp(-logits)), dtype=np.float64)
    return values


def to_numpy_dtypes(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    """`columns` of `frame` with pandas' nullable extension dtypes replaced by numpy ones.

    **AutoGluon 1.6.3 silently drops a pandas nullable column.** It logs `dtype Int64 is not
    recognized as a valid dtype by numpy! AutoGluon may incorrectly handle this feature` and then
    leaves the column out of its feature metadata altogether - the model is fitted without it and
    nothing fails. Measured on the synthetic training file: two of five features disappeared and the
    test ROC-AUC fell from 0.851 to 0.763, below the separately trained logistic baseline. The
    prepare stage legitimately produces `Int64` (a nullable integer is the honest dtype for an
    integer column with missing values), so the conversion belongs here, at the library boundary,
    and is applied on every path into AutoGluon: the fit, and every later score.

    Integers and floats become `float64` (a missing value becomes `NaN`), booleans become `bool` or
    `object` when some are missing, anything else becomes `object`. Categoricals and plain numpy
    dtypes are left alone. The label column is never converted: its values are the class labels.
    """
    import pandas as pd
    from pandas.api.types import is_bool_dtype, is_numeric_dtype

    converted: dict[str, pd.Series] = {}
    for column in columns:
        series = frame[column]
        dtype = series.dtype
        if isinstance(dtype, pd.CategoricalDtype) or not isinstance(dtype, pd.api.extensions.ExtensionDtype):
            continue
        if is_bool_dtype(dtype):
            converted[column] = series.astype("object") if bool(series.isna().any()) else series.astype(bool)
        elif is_numeric_dtype(dtype):
            converted[column] = series.astype("float64")
        else:
            converted[column] = series.astype("object")
    if not converted:
        return frame
    result = frame.copy()
    for column, series in converted.items():
        result[column] = series
    return result


def brier_score(actual: BoolArray, scores: FloatArray) -> float | None:
    """Mean squared error of a probability against a 0/1 outcome; null when there is nothing to score."""
    import numpy as np

    if scores.size == 0:
        return None
    return float(np.mean((scores - actual.astype(np.float64)) ** 2))


# ---------------------------------------------------------------------------
# The decision threshold: chosen on validation, never on test
# ---------------------------------------------------------------------------
def _format_threshold(value: float) -> str:
    """Two decimals when that is the whole number, four when the extra digits are real."""
    return f"{value:.2f}" if round(value, 2) == round(value, THRESHOLD_DECIMALS) else f"{value:.4f}"


def _best_f1_threshold(scores: FloatArray, actual: BoolArray) -> float | None:
    """The smallest threshold maximising F1 for the rule `positive iff score >= threshold`.

    One descending sweep: with `TP + FP` the number of rows above the cut and `TP + FN` the number
    of positives, `F1 = 2 TP / ((TP + FP) + (TP + FN))`, so no confusion matrix has to be rebuilt
    per candidate. Only the last row of a run of equal scores is a candidate, because a cut inside
    a tie is not a rule the `>=` comparison can express. Among equal maxima the smallest threshold
    wins, which is the higher-recall end: one deterministic rule, and reach is the marketing default.
    """
    import numpy as np

    positives = int(np.count_nonzero(actual))
    if positives == 0 or positives == actual.size:
        return None
    order = np.argsort(-scores, kind="stable")
    sorted_scores = scores[order]
    hits = np.cumsum(actual[order].astype(np.int64))
    above = np.arange(1, scores.size + 1, dtype=np.int64)
    f1 = 2.0 * hits / (above + positives)
    last_of_run = np.ones(scores.size, dtype=np.bool_)
    last_of_run[:-1] = sorted_scores[:-1] != sorted_scores[1:]
    candidates = np.flatnonzero(last_of_run)
    best = float(np.max(f1[candidates]))
    # The last candidate at the maximum is the lowest threshold, because the sweep descends.
    chosen = int(candidates[np.flatnonzero(f1[candidates] == best)][-1])
    return round(float(sorted_scores[chosen]), THRESHOLD_DECIMALS)


def choose_threshold(
    scores: FloatArray, actual: BoolArray, threshold: ThresholdConfig
) -> tuple[float, ThresholdMode, str]:
    """`(threshold, mode, detail)` for a classification model, decided on the validation split.

    `scores` are the **calibrated** validation scores, so the threshold and the probabilities that
    will be compared against it mean the same thing. `fixed` is 0.5 by config validation and
    `manual` is whatever the user set; only `auto` looks at the data, and only ever at this data.
    """
    if threshold.mode is ThresholdMode.FIXED:
        return FIXED_THRESHOLD, ThresholdMode.FIXED, f"Fixed: {FIXED_THRESHOLD:.2f}"
    if threshold.mode is ThresholdMode.MANUAL:
        value = round(float(threshold.value), THRESHOLD_DECIMALS)
        return value, ThresholdMode.MANUAL, f"Manual: {_format_threshold(value)}"
    chosen = _best_f1_threshold(scores, actual)
    if chosen is None:
        return (
            FIXED_THRESHOLD,
            ThresholdMode.AUTO,
            f"Auto (validation had one class only): {FIXED_THRESHOLD:.2f}",
        )
    return chosen, ThresholdMode.AUTO, f"Auto (maximises F1 on validation): {_format_threshold(chosen)}"


# ---------------------------------------------------------------------------
# The scorers
# ---------------------------------------------------------------------------
class _FittedScorer(ABC):
    """Everything both scorers share: the metadata evaluate reads, and the scoring calls.

    The attributes the evaluate stage's `Scorer` protocol names are properties over `state`, so the
    object that answers `isinstance(x, Scorer)` and the object that is persisted are one thing and
    cannot drift apart.
    """

    def __init__(self, state: ScorerState) -> None:
        self.state = state

    # -- identity -----------------------------------------------------------
    @property
    def model_name(self) -> str:
        return self.state.model_name

    @property
    def display_name(self) -> str:
        return self.state.display_name

    @property
    def family(self) -> ModelFamily | None:
        return self.state.family

    @property
    def feature_columns(self) -> tuple[str, ...]:
        return self.state.feature_columns

    @property
    def recipe_hash(self) -> str:
        return self.state.recipe_hash

    # -- the evaluate protocol ---------------------------------------------
    @property
    def problem_type(self) -> ProblemType:
        return self.state.problem_type

    @property
    def target_column(self) -> str:
        return self.state.target_column

    @property
    def primary_metric(self) -> Metric:
        return self.state.primary_metric

    @property
    def classes(self) -> tuple[object, object] | None:
        return self.state.classes

    @property
    def threshold(self) -> float:
        return self.state.threshold

    @property
    def threshold_mode(self) -> ThresholdMode:
        return self.state.threshold_mode

    @property
    def threshold_detail(self) -> str:
        return self.state.threshold_detail

    @property
    def calibration(self) -> CalibrationSummary | None:
        return self.state.calibration

    # -- scoring ------------------------------------------------------------
    @abstractmethod
    def _raw_values(self, features: pd.DataFrame) -> FloatArray:
        """The model's own output: positive-class probability, or the predicted number."""

    def _features(self, frame: pd.DataFrame) -> pd.DataFrame:
        usable, reason = self.can_score(frame)
        if not usable:
            raise TrainError("SCORER_CANNOT_SCORE", reason)
        columns = list(self.state.feature_columns)
        return to_numpy_dtypes(frame[columns], columns)

    def can_score(self, frame: pd.DataFrame) -> tuple[bool, str]:
        """`(True, "")` when `frame` carries every feature column, else `(False, why)`.

        This is what lets the champion rule decide whether a stored model can still be measured on
        a new hold-out frame. The reason names columns, never values.
        """
        missing = [column for column in self.state.feature_columns if column not in frame.columns]
        if not missing:
            return True, ""
        shown = ", ".join(missing[:5])
        more = "" if len(missing) <= 5 else f" and {len(missing) - 5} more"
        return False, f"The data is missing {len(missing)} column(s) the model needs: {shown}{more}."

    def raw_score(self, frame: pd.DataFrame) -> pd.Series:
        """The **uncalibrated** model output, one value per row, on `frame`'s own index."""
        import pandas as pd

        features = self._features(frame)
        return pd.Series(self._raw_values(features), index=frame.index, dtype="float64")

    def score(self, frame: pd.DataFrame) -> pd.Series:
        """The calibrated positive-class probability, or the prediction for a regression model."""
        import pandas as pd

        features = self._features(frame)
        values = self._raw_values(features)
        if self.state.problem_type is not ProblemType.REGRESSION:
            values = apply_calibrator(self.state.calibrator, values)
        return pd.Series(values, index=frame.index, dtype="float64")

    def predict_proba(self, frame: pd.DataFrame) -> FloatArray:
        """Calibrated positive-class probabilities - the evaluate stage's `ClassificationScorer`."""
        import numpy as np

        if self.state.problem_type is ProblemType.REGRESSION:
            raise TrainError(
                "SCORER_NOT_A_CLASSIFIER",
                "This model predicts a number, so it has no class probabilities.",
            )
        return np.asarray(self.score(frame).to_numpy(), dtype=np.float64)

    def predicted_positive(self, frame: pd.DataFrame) -> pd.Series:
        """`score >= threshold`, the one decision rule the whole engine uses."""
        return self.score(frame) >= self.state.threshold

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        """The model's prediction: the predicted number, or the class label chosen at the threshold.

        For a regression model this is the evaluate stage's `RegressionScorer.predict`. For a
        classification model it is the decision at the threshold rendered as the label the data
        actually carries, the way `sklearn`'s `predict` does; `predicted_positive` gives the same
        decision as a boolean mask.
        """
        import pandas as pd

        if self.state.problem_type is ProblemType.REGRESSION:
            return self.score(frame)
        classes = self.state.classes
        if classes is None:
            raise TrainError(
                "SCORER_CLASSES_UNKNOWN",
                "This model does not name its two classes, so it cannot label a prediction.",
            )
        negative, positive = classes
        decided = self.predicted_positive(frame)
        return pd.Series([positive if flag else negative for flag in decided], index=frame.index)

    def to_json(self) -> str:
        """`scorer.json`'s text: everything needed to rebuild this object beside its predictor."""
        return self.state.model_dump_json(indent=2)


class AutoGluonScorer(_FittedScorer):
    """A fitted `TabularPredictor` plus the threshold and calibrator chosen on validation."""

    def __init__(self, predictor: TabularPredictor, state: ScorerState) -> None:
        super().__init__(state)
        self.predictor = predictor

    def _raw_values(self, features: pd.DataFrame) -> FloatArray:
        import numpy as np

        predictor = self.predictor
        if self.state.problem_type is ProblemType.REGRESSION:
            predicted = predictor.predict(features)
            return np.asarray(np.asarray(predicted), dtype=np.float64)
        proba = predictor.predict_proba(features, as_multiclass=False)
        return np.asarray(np.asarray(proba), dtype=np.float64)

    @classmethod
    def from_json(cls, predictor: TabularPredictor, text: str) -> AutoGluonScorer:
        """Rebuild the scorer that wrote `text` around an already-loaded predictor."""
        return cls(predictor, ScorerState.model_validate_json(text))

    def save(self, storage: Storage, predictor_key: str) -> str:
        """Write `scorer.json` into the predictor's own directory and return its key."""
        key = f"{predictor_key}/{SCORER_FILENAME}"
        storage.write_text(key, self.to_json())
        return key


class BaselineScorer(_FittedScorer):
    """The separately trained logistic (or ridge) baseline, with its own threshold and calibrator.

    Its operating point is fitted on the same validation split by the same rule as the model's, so
    the model-versus-baseline table compares each at the point it would actually be deployed at.
    It is not persisted: it exists to be measured once, in the run that trained it.
    """

    def __init__(self, pipeline: Pipeline, state: ScorerState) -> None:
        super().__init__(state)
        self.pipeline = pipeline

    def _raw_values(self, features: pd.DataFrame) -> FloatArray:
        import numpy as np

        if self.state.problem_type is ProblemType.REGRESSION:
            return np.asarray(np.asarray(self.pipeline.predict(features)), dtype=np.float64)
        proba = np.asarray(self.pipeline.predict_proba(features), dtype=np.float64)
        return np.asarray(proba[:, 1], dtype=np.float64)


# ---------------------------------------------------------------------------
# Fitting the operating point - validation only
# ---------------------------------------------------------------------------
def positive_mask(labels: pd.Series, positive: LabelValue) -> BoolArray:
    """Which rows carry the positive class, comparing as-is first and then as text.

    The text fallback is what makes a label that survived a CSV round trip as `"1"` match the
    integer `1` the model was fitted with.
    """
    import numpy as np

    matched = np.asarray(labels.to_numpy() == positive, dtype=np.bool_)
    if not bool(matched.any()):
        as_text = np.asarray(labels.astype(str).to_numpy() == str(positive), dtype=np.bool_)
        if bool(as_text.any()):
            return as_text
    return matched


def _fit_operating_point(
    raw: FloatArray,
    actual: BoolArray,
    evaluation: EvaluationConfig,
    *,
    problem_type: ProblemType,
    what: str,
) -> tuple[CalibratorState, CalibrationSummary | None, float, ThresholdMode, str]:
    """Calibrator, summary, threshold, mode and detail - all from the validation split.

    THE TEST SPLIT IS FINAL-DECISION-ONLY: `raw` and `actual` come from validation, and this is the
    only function in the engine that turns scores into an operating point. Fitting here on anything
    else would let the hold-out choose the point it is meant to be judging.
    """
    if problem_type is ProblemType.REGRESSION:
        return CalibratorState(), None, 0.0, ThresholdMode.FIXED, REGRESSION_THRESHOLD_DETAIL
    calibrator, refused = fit_calibrator(raw, actual, evaluation.calibration)
    if refused is not None:
        _LOGGER.warning("%s: %s calibration was not fitted because %s", what, evaluation.calibration, refused)
    calibrated = apply_calibrator(calibrator, raw)
    if evaluation.calibration is Calibration.NONE:
        summary: CalibrationSummary | None = None
    else:
        summary = CalibrationSummary(
            method=calibrator.method,
            brier_before=_rounded(brier_score(actual, raw)),
            brier_after=_rounded(brier_score(actual, calibrated)),
        )
    threshold, mode, detail = choose_threshold(calibrated, actual, evaluation.threshold)
    return calibrator, summary, threshold, mode, detail


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def fit_scorer(
    predictor: TabularPredictor,
    recipe: Recipe,
    evaluation: EvaluationConfig,
    *,
    validation: pd.DataFrame,
    display_name: str,
    model_name: str,
    family: ModelFamily | None,
    classes: tuple[LabelValue, LabelValue] | None,
    autogluon_version: str = "",
    trained_at: datetime | None = None,
) -> AutoGluonScorer:
    """Fit the calibrator and choose the threshold for `predictor`, on the **validation** split.

    THE TEST SPLIT IS FINAL-DECISION-ONLY (DEC-045): the only frame this function reads is
    `validation`. The threshold and the calibration curve are the two numbers that decide what
    every downstream metric means, so fitting either on the hold-out would make the hold-out score
    a self-assessment. `tests/unit/test_scorer.py::test_fit_scorer_reads_validation_only` proves
    the property by perturbing a test frame and watching nothing move.
    """
    import numpy as np

    columns = list(recipe.feature_columns)
    features = to_numpy_dtypes(validation[columns], columns)
    if recipe.problem_type is ProblemType.REGRESSION:
        raw = np.asarray(np.asarray(predictor.predict(features)), dtype=np.float64)
        actual = np.zeros(raw.size, dtype=np.bool_)
    else:
        proba = predictor.predict_proba(features, as_multiclass=False)
        raw = np.asarray(np.asarray(proba), dtype=np.float64)
        positive = None if classes is None else classes[1]
        actual = (
            np.zeros(raw.size, dtype=np.bool_)
            if positive is None
            else positive_mask(validation[recipe.target], positive)
        )
    calibrator, summary, threshold, mode, detail = _fit_operating_point(
        raw, actual, evaluation, problem_type=recipe.problem_type, what=model_name
    )
    state = ScorerState(
        kind="autogluon",
        model_name=model_name,
        display_name=display_name,
        family=family,
        problem_type=recipe.problem_type,
        target_column=recipe.target,
        feature_columns=tuple(recipe.feature_columns),
        classes=classes,
        primary_metric=recipe.model_search.metric,
        threshold=threshold,
        threshold_mode=mode,
        threshold_detail=detail,
        calibrator=calibrator,
        calibration=summary,
        recipe_hash=recipe.recipe_hash,
        autogluon_version=autogluon_version,
        trained_at=utc_now() if trained_at is None else trained_at,
    )
    return AutoGluonScorer(predictor, state)


def fit_baseline_scorer(
    recipe: Recipe,
    evaluation: EvaluationConfig,
    *,
    train: pd.DataFrame,
    validation: pd.DataFrame,
    classes: tuple[LabelValue, LabelValue] | None,
    class_weights: bool = False,
    trained_at: datetime | None = None,
) -> BaselineScorer | None:
    """Plan section 6.3's separately trained baseline: logistic regression on the same train split.

    Fitted on the **train** split only - not train plus validation - so the comparison is like for
    like with the model, and given the same class-imbalance handling the model got. Its own
    threshold and calibrator come from the same validation split through the same code path above.

    Any failure returns `None` with one WARNING: a baseline that will not fit is a missing
    comparison, not a failed run, and `compare_to_baseline` is simply not called.
    """
    try:
        return _fit_baseline_scorer(
            recipe,
            evaluation,
            train=train,
            validation=validation,
            classes=classes,
            class_weights=class_weights,
            trained_at=trained_at,
        )
    except Exception:
        _LOGGER.warning("the baseline could not be trained; the model-versus-baseline table will be empty")
        _LOGGER.debug("baseline failure detail", exc_info=True)
        return None


def _fit_baseline_scorer(
    recipe: Recipe,
    evaluation: EvaluationConfig,
    *,
    train: pd.DataFrame,
    validation: pd.DataFrame,
    classes: tuple[LabelValue, LabelValue] | None,
    class_weights: bool,
    trained_at: datetime | None,
) -> BaselineScorer:
    """The body of `fit_baseline_scorer`, so the caller above is nothing but the failure policy."""
    import numpy as np
    from pandas.api.types import is_numeric_dtype
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.pipeline import Pipeline as SkPipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    features = list(recipe.feature_columns)
    train = to_numpy_dtypes(train, features)
    numeric = [column for column in features if is_numeric_dtype(train[column])]
    other = [column for column in features if column not in numeric]
    classification = recipe.problem_type is not ProblemType.REGRESSION
    pre = ColumnTransformer(
        [
            (
                "num",
                SkPipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]),
                numeric,
            ),
            (
                "cat",
                SkPipeline(
                    [
                        ("impute", SimpleImputer(strategy="most_frequent")),
                        ("encode", OneHotEncoder(handle_unknown="ignore", min_frequency=0.01)),
                    ]
                ),
                other,
            ),
        ]
    )
    head = (
        LogisticRegression(
            max_iter=1000,
            random_state=recipe.seed,
            class_weight="balanced" if class_weights else None,
        )
        if classification
        else Ridge(alpha=1.0, random_state=recipe.seed)
    )
    pipeline = SkPipeline([("pre", pre), ("head", head)])
    labels: BoolArray | FloatArray
    if classification:
        if classes is None:
            raise TrainError(
                "BASELINE_CLASSES_UNKNOWN", "The baseline needs the two class labels to be known."
            )
        labels = positive_mask(train[recipe.target], classes[1])
    else:
        labels = np.asarray(train[recipe.target].to_numpy(), dtype=np.float64)
    pipeline.fit(to_numpy_dtypes(train[features], features), labels)

    validation_features = to_numpy_dtypes(validation[features], features)
    if classification and classes is not None:
        proba = np.asarray(pipeline.predict_proba(validation_features), dtype=np.float64)
        raw = np.asarray(proba[:, 1], dtype=np.float64)
        actual = positive_mask(validation[recipe.target], classes[1])
    else:
        raw = np.asarray(np.asarray(pipeline.predict(validation_features)), dtype=np.float64)
        actual = np.zeros(raw.size, dtype=np.bool_)
    calibrator, summary, threshold, mode, detail = _fit_operating_point(
        raw, actual, evaluation, problem_type=recipe.problem_type, what=BASELINE_MODEL_NAME
    )
    state = ScorerState(
        kind="baseline",
        model_name=BASELINE_MODEL_NAME,
        display_name=BASELINE_DISPLAY_NAME if classification else BASELINE_REGRESSION_DISPLAY_NAME,
        family=ModelFamily.LOGISTIC_REGRESSION,
        problem_type=recipe.problem_type,
        target_column=recipe.target,
        feature_columns=tuple(features),
        classes=classes,
        primary_metric=recipe.model_search.metric,
        threshold=threshold,
        threshold_mode=mode,
        threshold_detail=detail,
        calibrator=calibrator,
        calibration=summary,
        recipe_hash=recipe.recipe_hash,
        trained_at=utc_now() if trained_at is None else trained_at,
    )
    return BaselineScorer(pipeline, state)


def load_scorer(predictor_key: str, storage: Storage) -> AutoGluonScorer:
    """Rebuild a stored model: the AutoGluon predictor plus its `scorer.json`.

    This is what makes the champion rule honest. The incumbent is reloaded here and re-scored on the
    challenger's test split, with **its own** threshold and calibrator, rather than compared against
    a number measured on a hold-out that no longer exists.
    """
    path = storage.local_path(predictor_key)
    if not (path / "predictor.pkl").is_file():
        raise TrainError("MODEL_NOT_SAVED", "The saved model could not be found on disk.")
    scorer_key = f"{predictor_key}/{SCORER_FILENAME}"
    if not storage.exists(scorer_key):
        raise TrainError(
            "SCORER_NOT_SAVED",
            "The saved model does not carry its decision threshold and calibration, so it cannot be "
            "scored the way it was when it was trained.",
        )
    from autogluon.tabular import TabularPredictor  # imported only once there is something to load

    predictor = TabularPredictor.load(str(path), require_version_match=True)
    return AutoGluonScorer.from_json(predictor, storage.read_text(scorer_key))
