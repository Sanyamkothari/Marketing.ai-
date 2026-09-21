"""Predict stage (M4): batch scoring with a stored predictor and the drift report.

Plan §6.3, `predict`, in order: load the champion (or the model version the user chose), replay
`prepare.json`, predict **calibrated** probabilities, and measure PSI per feature against the
stored drift baseline. The stage returns its artefacts rather than writing them, the way every
other stage does: `drift.json` is `PredictResult.drift`, and the pipeline writes it under the
scoring run's key together with the rest of the run's artefacts.

**The model chooses the preparation, not the run.** The transforms are replayed from the
`prepare.json` of the training run that produced the model version, read through
`ModelVersion.run_id` - never from the scoring run's own prepare stage, and never re-fitted here.
:func:`engine.stages.prepare.replay` applies the recorded parameters and measures nothing, so a
median filled at score time is the median of the *training* rows and a clip is the training
percentile. A statistic recomputed on the scored file would silently move every customer's score
relative to the run that chose the bands, which is exactly the failure this stage exists to avoid.

**The scores are the calibrated ones.** `AutoGluonScorer.score` applies the calibrator that was
fitted on the validation split at train time and persisted in `scorer.json`, so the numbers this
stage produces are on the same scale as the `actions.bands` thresholds and as every number the
evaluate stage reported. The raw model output is never exported; `raw_score` exists on the scorer
and is deliberately not used here. For a regression use case `score` is the predicted number, which
is what the bands are configured against for that problem type.

Deliberate behaviour at the edges, each one a decision rather than an accident:

- **No champion, and no version chosen** - `ScoreError("CHAMPION_NOT_FOUND")`. There is no honest
  default model, and scoring with an arbitrary candidate would put unapproved numbers in front of
  a business user.
- **A chosen version that is not in the registry** - `ScoreError("MODEL_NOT_FOUND")`.
- **A chosen version belonging to another use case** - `ScoreError("MODEL_USE_CASE_MISMATCH")`. Its
  feature list might even fit by accident; its target does not mean the same thing.
- **A version whose status is not `champion`** is still scored when it was named explicitly: the
  user asked for that version (`POST /runs` carries `model_version_id`), and refusing would make it
  impossible to re-score history with an archived model. The status is logged.
- **The champion has no stored predictor**, or has one without its `scorer.json` - `ScoreError`
  carrying `load_scorer`'s own code (`MODEL_NOT_SAVED` / `SCORER_NOT_SAVED`). A predictor without
  its threshold and calibrator cannot reproduce the numbers it was approved on, so it is refused
  rather than scored raw.
- **The training run kept no `prepare.json`** - `ScoreError("PREPARE_REPORT_NOT_SAVED")`. Scoring
  without the recorded transforms would feed the model differently prepared columns.
- **A frame missing a feature the model needs** - `ScoreError("SCORE_SCHEMA_MISMATCH")`, naming the
  columns. Reporting a schema mismatch in business language is `validate_against_schema`'s job
  (plan §4.4), and in the real score flow it blocks the run long before this stage; this check is
  the backstop that keeps a mismatched frame from being scored anyway, because AutoGluon would
  happily return numbers for a frame it understands only in part.
- **Extra columns the model does not know** are ignored, not refused: the scorer selects exactly
  its own feature columns in fit order. A scoring file legitimately carries the primary key, the
  consent and opt-out columns, a snapshot date and whatever else the source system exports, and the
  actions and export stages need them, so they are kept in `PredictResult.prepared` and simply not
  shown to the model.
- **Zero rows** - an empty `float64` series, and no call into AutoGluon (its predictors do not all
  accept an empty frame). The model is still loaded and the frame is still checked, so an empty
  file fails for the same reasons a full one would.
- **Drift with no stored baseline** (a version registered before baselines existed, or a baseline
  that has been deleted) - `PredictResult.drift` is `None` and a WARNING says so. Scoring still
  succeeds: drift is a signal the plan reports, never a gate on scoring.

Determinism: the same model and the same frame give the same scores. Nothing here samples, fits or
reads the clock on the scoring path; `replay` applies recorded numbers and `predict_proba` is
deterministic for a fitted predictor.

`pandas` and AutoGluon are imported inside function bodies, never at module level, so
`import engine` stays fast.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from engine.storage import run_key
from engine.utils.logging import get_logger, log_stage
from engine.utils.text import humanise_count

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import (
        DriftBaseline,
        DriftReport,
        DriftStatus,
        FeatureDrift,
        ModelVersion,
        PrepareReport,
    )
    from engine.registry import ModelRegistry
    from engine.stages.scorer import AutoGluonScorer
    from engine.storage import Storage

PSI_EPSILON: Final[float] = 1e-6
"""The zero-count floor of the PSI sum.

A bin that is empty on one side makes the log term infinite, so an empty share is read as this
epsilon instead. One part in a million is two to three orders of magnitude below the smallest
share a real file can produce (plan §4.1 asks for at least 1,000 rows, so one row is 1e-3), which
keeps the substitution invisible for bins that are merely small while an emptied bin still scores
a large, finite and reproducible contribution. The floor is applied to both sides, so PSI stays
symmetric, and the shares are not renormalised afterwards: over at most 51 slots the distortion
stays below 1e-4 of the total mass.
"""

PSI_DECIMALS: Final[int] = 4
RATE_DECIMALS: Final[int] = 6

PREPARE_REPORT_FILENAME: Final[str] = "prepare.json"
"""The artefact of the *training* run whose transforms every scoring run replays (plan §6.3)."""

_LOGGER = get_logger(__name__)


class ScoreError(Exception):
    """A scoring run cannot produce honest scores.

    `code` is machine-readable and `message` is business language, the same shape as the train
    stage's `TrainError` and the evaluate stage's `EvaluationError`; when `engine/errors.py` lands
    all three collapse into the shared `EngineError` without changing a single code string. The
    codes are `CHAMPION_NOT_FOUND`, `MODEL_NOT_FOUND`, `MODEL_USE_CASE_MISMATCH`, `MODEL_NOT_SAVED`,
    `SCORER_NOT_SAVED`, `PREPARE_REPORT_NOT_SAVED` and `SCORE_SCHEMA_MISMATCH`. A message names
    columns and model versions, never a customer value (plan §13.7).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PredictResult:
    """What the predict stage hands the pipeline. Not an artefact - the artefact is inside it."""

    model_version: ModelVersion
    """The version that scored the file: the champion, or the one the user asked for."""

    scorer: AutoGluonScorer
    """The loaded model, so a caller (explain_rows) can reuse it instead of loading it again."""

    scores: pd.Series
    """Calibrated scores, named `actions.score_field` and on the input frame's own index."""

    prepared: pd.DataFrame
    """The replayed frame: every column the file brought, with the training transforms applied."""

    drift: DriftReport | None
    """`drift.json`, or `None` when the model version stored no drift baseline."""

    detail: str
    """Pre-formatted Running-screen line for the "Scoring rows" step."""


def predict(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    storage: Storage,
    registry: ModelRegistry,
    model_version_id: str | None = None,
) -> PredictResult:
    """Score `frame` with the champion, or with `model_version_id` when the user chose one.

    The steps are the plan's, cheapest first so a broken input fails before AutoGluon is loaded:
    resolve the version, read the `prepare.json` of the run that trained it, replay it over the
    frame, load the predictor with its threshold and calibrator, refuse a frame the model cannot
    score, produce the calibrated scores, and measure drift against the stored baseline.

    The returned scores carry the input frame's index, so the caller assigns them with
    `frame[config.actions.score_field] = result.scores`; `result.prepared` is the frame the actions
    and export stages work from. Every failure mode is documented in the module docstring.
    """
    from engine.stages.prepare import replay

    started = time.perf_counter()
    version = _resolve_version(config, registry=registry, model_version_id=model_version_id)
    report = _prepare_report(version, storage=storage)
    prepared = replay(frame, report)
    scorer = _load_scorer(version, storage=storage)
    _require_features(scorer, prepared, version)
    scores = _calibrated_scores(scorer, prepared, config)
    drift = _drift(version, prepared, config, run_id=run_id, storage=storage)
    detail = _predict_detail(version, rows=len(scores.index), drift=drift)
    log_stage(_LOGGER, "predict", rows=len(scores.index), seconds=time.perf_counter() - started)
    return PredictResult(
        model_version=version,
        scorer=scorer,
        scores=scores,
        prepared=prepared,
        drift=drift,
        detail=detail,
    )


def _resolve_version(
    config: UseCaseConfig, *, registry: ModelRegistry, model_version_id: str | None
) -> ModelVersion:
    """The model version this run scores with: the named one, else the use case's champion."""
    from engine.contracts import ModelStatus
    from engine.registry import RegistryError

    if model_version_id is None:
        champion = registry.get_champion(config.id)
        if champion is None:
            raise ScoreError(
                "CHAMPION_NOT_FOUND",
                f"No approved model is available for {config.name}. Train a model and approve it, "
                f"or choose which model version to score with.",
            )
        return champion
    try:
        version = registry.get(model_version_id)
    except RegistryError as error:
        raise ScoreError(
            error.code,
            f"Model version {model_version_id} is not in the registry, so it cannot score this file.",
        ) from error
    if version.use_case_id != config.id:
        raise ScoreError(
            "MODEL_USE_CASE_MISMATCH",
            f"Model version {version.model_id} was trained for {version.use_case_id} and cannot "
            f"score a {config.id} file.",
        )
    if version.status is not ModelStatus.CHAMPION:
        _LOGGER.info(
            "stage=predict model_version=%s status=%s chosen=explicitly",
            version.model_id,
            version.status.value,
        )
    return version


def _prepare_report(version: ModelVersion, *, storage: Storage) -> PrepareReport:
    """The `prepare.json` of the run that trained `version`: the only transforms that may be replayed."""
    from engine.contracts import PrepareReport

    key = run_key(version.run_id, PREPARE_REPORT_FILENAME)
    if not storage.exists(key):
        raise ScoreError(
            "PREPARE_REPORT_NOT_SAVED",
            f"The run that trained model version {version.model_id} did not keep a record of how "
            f"its features were prepared, so this file cannot be prepared the same way.",
        )
    return storage.read_model(key, PrepareReport)


def _load_scorer(version: ModelVersion, *, storage: Storage) -> AutoGluonScorer:
    """The stored predictor rebuilt with its threshold and calibrator, or a `ScoreError`."""
    from engine.stages.scorer import TrainError, load_scorer

    try:
        return load_scorer(version.predictor_key, storage)
    except TrainError as error:
        raise ScoreError(error.code, f"{error.message} (model version {version.model_id})") from error


def _require_features(scorer: AutoGluonScorer, prepared: pd.DataFrame, version: ModelVersion) -> None:
    """Refuse a frame the model cannot score, naming the columns it is missing."""
    usable, reason = scorer.can_score(prepared)
    if not usable:
        raise ScoreError(
            "SCORE_SCHEMA_MISMATCH",
            f"{reason} Model version {version.model_id} cannot score this file.",
        )


def _calibrated_scores(scorer: AutoGluonScorer, prepared: pd.DataFrame, config: UseCaseConfig) -> pd.Series:
    """The calibrated score of every row, named `actions.score_field`; empty in, empty out."""
    import pandas as pd

    name = config.actions.score_field
    if not len(prepared.index):
        return pd.Series([], index=prepared.index, dtype="float64", name=name)
    scores: pd.Series = scorer.score(prepared)
    return scores.rename(name)


def _drift(
    version: ModelVersion,
    prepared: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    storage: Storage,
) -> DriftReport | None:
    """PSI against the baseline stored with `version`, or `None` when it stored none.

    The baseline was summarised from the *prepared* training rows, so the comparison is made on the
    replayed frame; comparing the raw upload against it would measure the transforms, not the data.
    """
    from engine.contracts import DriftBaseline

    key = version.drift_baseline_key
    if key is None or not storage.exists(key):
        _LOGGER.warning(
            "stage=predict drift=unavailable model_version=%s reason=no_stored_baseline",
            version.model_id,
        )
        return None
    baseline = storage.read_model(key, DriftBaseline)
    return compute_drift(baseline, prepared, config, run_id=run_id)


def _predict_detail(version: ModelVersion, *, rows: int, drift: DriftReport | None) -> str:
    """Running-screen row 3 of the score flow. Every number in it was measured."""
    measured = "drift not measured" if drift is None else drift.summary
    return (
        f"{humanise_count(rows)} rows scored · "
        f"{version.model_display_name} (v{version.version}) · {measured}"
    )


def compute_drift(
    baseline: DriftBaseline,
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
) -> DriftReport:
    """Population stability index per feature against the training baseline (plan §6.3, predict).

    Every feature is compared **in the baseline's own bins and levels**; the scored file is never
    re-binned, because two files binned independently are not comparable. The projection lives in
    `engine.stages.register`, next to the code that wrote the baseline.

    PSI is `sum((actual - expected) * ln(actual / expected))` over those bins, with `PSI_EPSILON`
    standing in for an empty share on either side. The edge cases, all of them deliberate:

    - **a category unseen at training time** falls into the baseline's `__other__` bucket when the
      column had one, and otherwise into a trailing slot whose expected share is zero - so unseen
      mass scores as drift instead of being dropped;
    - **a numeric value outside every baseline bin** is clamped into the outermost bin, whose tails
      are read as open-ended; a *constant* baseline column has a zero-width bin instead, and values
      that differ from the constant land in the trailing slot;
    - **an empty bin on either side** is floored at `PSI_EPSILON`;
    - **a feature missing from the scored frame** is read as a feature that is entirely null: no
      mass in any bin, a current null rate of one, and therefore a large PSI. Reporting which
      columns are missing is the schema check's job (plan §4.4), not this one's;
    - **a feature present but all-null** behaves identically, and so does an empty frame;
    - **a baseline feature with no distribution at all** (a column that was already entirely null
      at training time) has nothing to compare, so its PSI is zero and it is stable.

    A feature is `drifted` at or above `monitoring.drift_psi_threshold`, `watch` from half that
    threshold, and `stable` below it; the run takes the verdict of its worst feature, and the
    summary is the Output page's line, for example `PSI 0.11, watch`. A run with any drifted
    feature is logged at WARNING.
    """
    from engine.contracts import DriftReport, FeatureDrift
    from engine.stages.register import _feature_shares
    from engine.utils.logging import get_logger
    from engine.utils.time import utc_now

    threshold = config.monitoring.drift_psi_threshold
    drifts: list[FeatureDrift] = []
    for feature in baseline.features:
        expected, actual, null_rate_current = _feature_shares(feature, frame)
        psi = round(_psi(expected, actual), PSI_DECIMALS)
        drifts.append(
            FeatureDrift(
                feature=feature.feature,
                psi=psi,
                status=_drift_status(psi, threshold),
                null_rate_baseline=round(feature.null_rate, RATE_DECIMALS),
                null_rate_current=round(null_rate_current, RATE_DECIMALS),
            )
        )
    drifts.sort(key=lambda drift: (-drift.psi, drift.feature))
    max_psi = max((drift.psi for drift in drifts), default=0.0)
    status = _drift_status(max_psi, threshold)
    drifted = tuple(drift.feature for drift in drifts if drift.psi >= threshold)
    if drifted:
        get_logger(__name__).warning(
            "stage=predict drift=exceeded features=%d max_psi=%.4f threshold=%.2f",
            len(drifted),
            max_psi,
            threshold,
        )
    return DriftReport(
        run_id=run_id,
        baseline_run_id=baseline.run_id,
        model_version_id=baseline.model_version_id,
        threshold=threshold,
        features=tuple(drifts),
        max_psi=max_psi,
        drifted_features=drifted,
        status=status,
        summary=f"PSI {max_psi:.2f}, {status.value}",
        computed_at=utc_now(),
    )


def _psi(expected: Sequence[float], actual: Sequence[float]) -> float:
    """The population stability index between two share vectors binned the same way.

    `sum((a - e) * ln(a / e))` with every share floored at `PSI_EPSILON`. Every term is
    non-negative, so the sum is too, and swapping the two vectors leaves it unchanged.
    """
    import math

    total = 0.0
    for expected_share, actual_share in zip(expected, actual, strict=True):
        floored_expected = max(expected_share, PSI_EPSILON)
        floored_actual = max(actual_share, PSI_EPSILON)
        total += (floored_actual - floored_expected) * math.log(floored_actual / floored_expected)
    return max(total, 0.0)


def _drift_status(psi: float, threshold: float) -> DriftStatus:
    """`drifted` at or above the threshold, `watch` from half of it, `stable` below."""
    from engine.contracts import DriftStatus

    if psi >= threshold:
        return DriftStatus.DRIFTED
    if psi >= threshold / 2.0:
        return DriftStatus.WATCH
    return DriftStatus.STABLE
