"""Predict stage (M4): batch scoring with a stored predictor and the drift report.

Plan §6.3, `predict`, in order: load the champion (or the model version the user chose), replay
`prepare.json`, predict **calibrated** probabilities, and measure PSI per feature against the
stored drift baseline. The stage returns its artefacts rather than writing them, the way every
other stage does: `drift.json` is `PredictResult.drift`, `prepare.json` is
`PredictResult.prepare_report`, and the pipeline writes both under the scoring run's key together
with the rest of the run's artefacts.

**One resolver decides which model scores a file.** :func:`resolve_model_version` is that rule, and
`POST /runs` calls the same function before it accepts a scoring run, so a model version the
endpoint lets through is one this stage will score and a version it refuses is refused with the
code and the sentence the stage itself would have used (DEC-054). The endpoint used to re-derive
the champion with a laxer rule of its own, which let a version belonging to another use case start
a run that failed halfway through it.

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
- **A stored predictor that will not load** - `ScoreError("SCORER_UNREADABLE")`, naming the
  AutoGluon version the model was trained with and the one this engine is running, with the
  original exception chained. AutoGluon refuses a predictor written by another version
  (`require_version_match=True`), and a half-copied predictor directory raises whatever the pickle
  layer raises; neither is a `TrainError`, so catching only that let an uncoded exception reach the
  job runner as `STAGE_FAILED` instead of a business error the Running screen can explain.
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
- **Drift that was never measured** - `PredictResult.drift` is `None` and a WARNING says why.
  Scoring still succeeds: drift is a signal the plan reports, never a gate on scoring. There are
  three ways to have nothing to compare, and all three report *not measured* rather than a verdict
  (DEC-051): no stored baseline (a version registered before baselines existed, or a baseline that
  has been deleted); a frame with **no rows**; and a baseline with **no features**. The last two
  used to produce a report: every baseline share compared against an empty file scores its full
  PSI contribution, so a zero-row upload came back "drifted" against a baseline it could not
  possibly have matched, and a featureless baseline came back "PSI 0.00, stable" for a comparison
  that never happened. Both numbers were about the absence of data, not about the data.

Determinism: the same model and the same frame give the same scores. Nothing here samples, fits or
reads the clock on the scoring path; `replay` applies recorded numbers and `predict_proba` is
deterministic for a fitted predictor.

`pandas` and AutoGluon are imported inside function bodies, never at module level, so
`import engine` stays fast.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from engine.storage import run_key
from engine.utils.logging import get_logger, log_stage
from engine.utils.text import humanise_count

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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


CHAMPION_NOT_FOUND: Final[str] = "CHAMPION_NOT_FOUND"
"""The use case has no champion and the run named no version. The one code for "no champion"."""

MODEL_NOT_FOUND: Final[str] = "MODEL_NOT_FOUND"
"""The named model version is not in the registry. Shares `RegistryError`'s code deliberately."""

MODEL_USE_CASE_MISMATCH: Final[str] = "MODEL_USE_CASE_MISMATCH"
"""The named model version was trained for another use case."""

MODEL_NOT_SAVED: Final[str] = "MODEL_NOT_SAVED"
"""The version's predictor directory is not on disk. Raised first by `scorer.load_scorer`."""

SCORER_NOT_SAVED: Final[str] = "SCORER_NOT_SAVED"
"""The predictor is there but its `scorer.json` is not. Raised first by `scorer.load_scorer`."""

SCORER_UNREADABLE: Final[str] = "SCORER_UNREADABLE"
"""The stored predictor exists and will not load: a version mismatch, or a damaged directory."""

PREPARE_REPORT_NOT_SAVED: Final[str] = "PREPARE_REPORT_NOT_SAVED"
"""The run that trained the version kept no `prepare.json`, so its transforms cannot be replayed."""

SCORE_SCHEMA_MISMATCH: Final[str] = "SCORE_SCHEMA_MISMATCH"
"""The frame is missing a feature the model was fitted with."""

LOADER_CODES: Final[frozenset[str]] = frozenset({MODEL_NOT_SAVED, SCORER_NOT_SAVED})
"""`TrainError` codes this stage forwards from `scorer.load_scorer` with their own wording.

Anything else that module raises is a stored model this stage cannot use and has no words for, so
it is reported as `SCORER_UNREADABLE` rather than as a code the table has never heard of.
"""


SCORE_ERRORS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        CHAMPION_NOT_FOUND: (
            "No approved model is available for {use_case}. Train a model and approve it, or "
            "choose which model version to score with.",
            "Train a model for this use case and approve it, or name a model version on the run.",
        ),
        MODEL_NOT_FOUND: (
            "Model version {model_version_id} is not in the registry, so it cannot score this file.",
            "Pick a version from the model list, or leave it empty to score with the champion.",
        ),
        MODEL_USE_CASE_MISMATCH: (
            "Model version {model_version_id} was trained for {trained_use_case} and cannot score "
            "a {use_case_id} file.",
            "Pick a version trained for this use case, or leave it empty to score with the champion.",
        ),
        MODEL_NOT_SAVED: (
            "{reason} (model version {model_version_id})",
            "Train this use case again; the run that produced this version kept no usable model.",
        ),
        SCORER_NOT_SAVED: (
            "{reason} (model version {model_version_id})",
            "Train this use case again; the run that produced this version kept no usable model.",
        ),
        SCORER_UNREADABLE: (
            "The saved model for version {model_version_id} could not be opened. It was trained "
            "with AutoGluon {stored_version} and this engine is running AutoGluon "
            "{running_version}.",
            "Train this use case again with the installed version, then score the file with the "
            "new model.",
        ),
        PREPARE_REPORT_NOT_SAVED: (
            "The run that trained model version {model_version_id} did not keep a record of how "
            "its features were prepared, so this file cannot be prepared the same way.",
            "Train this use case again; the new model will record how its features were prepared.",
        ),
        SCORE_SCHEMA_MISMATCH: (
            "{reason} Model version {model_version_id} cannot score this file.",
            "Download the template for this use case and upload a file with those columns.",
        ),
    }
)
"""Code -> (message template, suggestion) for every failure this stage reports (plan §13.4).

The same shape as `engine.errors.ENGINE_ERRORS`, and the reason the codes exist in one place at
all: plan §13.4 asks every user-facing error to carry a code, a message and a suggestion, and
`POST /runs` maps these codes onto HTTP statuses (`api.routes.runs.SCORE_ERROR_STATUS`), which it
can only do for codes that are written down. Every raise in this module goes through
:func:`score_error`, so a code that is not in this table cannot be raised.
"""


class ScoreError(Exception):
    """A scoring run cannot produce honest scores.

    `code` is machine-readable and `message` is business language, the same shape as the train
    stage's `TrainError` and the evaluate stage's `EvaluationError`. `engine/errors.py` has landed
    and `engine.errors.run_error` already turns any of the three into the `RunError` that reaches
    `status.json`, by reading the `code` and `message` every coded engine exception carries; the
    three classes stay separate because each names the stage a reader has to go to, and collapsing
    them buys nothing that `run_error` does not already provide. `suggestion` comes from
    `SCORE_ERRORS`, the way `EngineError` takes its own from `ENGINE_ERRORS`; `RunError` has no
    place for it yet, so it reaches the log rather than the screen. A message names columns and
    model versions, never a customer value (plan §13.7).
    """

    def __init__(self, code: str, message: str, *, suggestion: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.suggestion = suggestion if suggestion else SCORE_ERRORS.get(code, ("", ""))[1]


def score_error(code: str, **values: object) -> ScoreError:
    """Build the `ScoreError` for `code` from `SCORE_ERRORS`, filling the message's placeholders."""
    message, suggestion = SCORE_ERRORS[code]
    return ScoreError(code, message.format(**values), suggestion=suggestion)


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

    prepare_report: PrepareReport
    """`prepare.json` - the training run's, replayed here and written under the scoring run's key.

    The stage has to read this record to score at all, so returning it costs nothing and is the
    only way a scoring run can produce a `prepare.json` of its own: the Data page renders from that
    artefact, and a score run that wrote none had nothing to show for its dropped columns, its
    fill values or its clip bounds (DEC-049). It is the training run's report unchanged - its
    `run_id` is the training run's - because that is the truth about how these rows were prepared.
    """

    drift: DriftReport | None
    """`drift.json`, or `None` when drift was not measured (no baseline, no rows, no features)."""

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
    from engine.column_names import load_for_model
    from engine.stages.prepare import replay

    started = time.perf_counter()
    version = resolve_model_version(config, registry=registry, model_version_id=model_version_id)
    report = _prepare_report(version, storage=storage)
    prepared = replay(frame, report)
    scorer = _load_scorer(version, storage=storage)
    # The client's headers in, the model's own names underneath: a model trained on a file whose
    # headers ingest had to rename stored the mapping beside its `scorer.json` (DEC-093).
    names = load_for_model(storage, version.predictor_key)
    if not names.is_identity:
        scorer = scorer.with_column_names(names.renamed)
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
        prepare_report=report,
        drift=drift,
        detail=detail,
    )


def resolve_model_version(
    config: UseCaseConfig, *, registry: ModelRegistry, model_version_id: str | None
) -> ModelVersion:
    """The model version this run scores with: the named one, else the use case's champion.

    The single source of truth for that question (DEC-054). `predict` calls it, and so does
    `POST /runs` before it starts a scoring run, so the endpoint and the stage can no longer
    disagree about which versions are scorable; the endpoint maps the codes below onto HTTP
    statuses and reports this function's own message.

    A version the caller named is honoured whatever its status - candidate, pending approval,
    champion or archived (DEC-050). Only the *implicit* choice, the champion, has to be approved:
    an explicitly named version is a user asking for that model, and refusing an archived one
    would make it impossible to re-score history with the model that produced it. A status other
    than champion is logged, so a run that used an unapproved model says so in the log as well as
    in `run.json`.
    """
    from engine.contracts import ModelStatus

    if model_version_id is None:
        champion = registry.get_champion(config.id)
        if champion is None:
            raise score_error(CHAMPION_NOT_FOUND, use_case=config.name)
        return champion
    version = _registered_version(model_version_id, registry=registry)
    if version.use_case_id != config.id:
        raise score_error(
            MODEL_USE_CASE_MISMATCH,
            model_version_id=version.model_id,
            trained_use_case=version.use_case_id,
            use_case_id=config.id,
        )
    if version.status is not ModelStatus.CHAMPION:
        _LOGGER.info(
            "stage=predict model_version=%s status=%s chosen=explicitly",
            version.model_id,
            version.status.value,
        )
    return version


def _registered_version(model_version_id: str, *, registry: ModelRegistry) -> ModelVersion:
    """The registry row for `model_version_id`, as a `ScoreError` rather than a `RegistryError`."""
    from engine.registry import RegistryError

    try:
        return registry.get(model_version_id)
    except RegistryError as error:
        raise score_error(MODEL_NOT_FOUND, model_version_id=model_version_id) from error


def _prepare_report(version: ModelVersion, *, storage: Storage) -> PrepareReport:
    """The `prepare.json` of the run that trained `version`: the only transforms that may be replayed."""
    from engine.contracts import PrepareReport

    key = run_key(version.run_id, PREPARE_REPORT_FILENAME)
    if not storage.exists(key):
        raise score_error(PREPARE_REPORT_NOT_SAVED, model_version_id=version.model_id)
    return storage.read_model(key, PrepareReport)


def _load_scorer(version: ModelVersion, *, storage: Storage) -> AutoGluonScorer:
    """The stored predictor rebuilt with its threshold and calibrator, or a `ScoreError`.

    Two kinds of failure reach this. `load_scorer` raises `TrainError` for the ones it checks for
    itself - no predictor directory, no `scorer.json` - and those keep their own code and wording.
    Everything else comes out of AutoGluon or the pickle layer while the predictor is being opened:
    `require_version_match=True` refuses a predictor written by a different AutoGluon, and a
    truncated or half-copied predictor directory fails in whatever way its own loader fails. Those
    are still business conditions - the stored model cannot be used - so they become
    `SCORER_UNREADABLE` with the stored and running versions named, rather than escaping uncoded
    and being reported as `STAGE_FAILED`, "train failed unexpectedly", by `engine.errors.run_error`.
    The original exception is chained, so the traceback survives in the log where it belongs.
    """
    from engine.stages.scorer import TrainError, load_scorer

    try:
        return load_scorer(version.predictor_key, storage)
    except TrainError as error:
        if error.code in LOADER_CODES:
            raise score_error(error.code, reason=error.message, model_version_id=version.model_id) from error
        raise _unreadable_scorer(version) from error
    except Exception as error:  # AutoGluon's own version check, or a damaged predictor directory
        raise _unreadable_scorer(version) from error


def _unreadable_scorer(version: ModelVersion) -> ScoreError:
    """`SCORER_UNREADABLE`, naming the AutoGluon the model was stored with and the one running."""
    return score_error(
        SCORER_UNREADABLE,
        model_version_id=version.model_id,
        stored_version=version.autogluon_version or "an unrecorded version",
        running_version=_running_autogluon_version(),
    )


def _running_autogluon_version() -> str:
    """The installed AutoGluon version, from the distribution metadata rather than an import.

    The counterpart of `engine.stages.register`'s version stamp, read the same way: this module
    must stay importable in milliseconds, and the version is wanted precisely when importing
    AutoGluon has just failed.
    """
    from importlib.metadata import PackageNotFoundError, version

    for distribution in ("autogluon.tabular", "autogluon"):
        try:
            return version(distribution)
        except PackageNotFoundError:
            continue
    return "unknown"


def _require_features(scorer: AutoGluonScorer, prepared: pd.DataFrame, version: ModelVersion) -> None:
    """Refuse a frame the model cannot score, naming the columns it is missing."""
    usable, reason = scorer.can_score(prepared)
    if not usable:
        raise score_error(SCORE_SCHEMA_MISMATCH, reason=reason, model_version_id=version.model_id)


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


def _unmeasurable_drift(baseline: DriftBaseline, frame: pd.DataFrame) -> str | None:
    """Why this comparison cannot be made, or `None` when it can (DEC-051).

    PSI compares two distributions, and a distribution needs rows on both sides and features to
    compare. Without either there is nothing to measure, and a number computed anyway would be a
    statement about the missing data rather than about drift: an empty file scores every baseline
    share as a fully emptied bin, which is the largest PSI the metric can produce, and a baseline
    with no features scores the empty sum, zero, which reads as a clean bill of health.
    """
    if not baseline.features:
        return "baseline_has_no_features"
    if not len(frame.index):
        return "no_rows_to_compare"
    return None


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
) -> DriftReport | None:
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
    - **a feature present but all-null** behaves identically; a frame with no rows at all is not a
      comparison and is reported as not measured instead (see below);
    - **a baseline feature with no distribution at all** (a column that was already entirely null
      at training time) has nothing to compare, so its PSI is zero and it is stable.

    A feature is `drifted` at or above `monitoring.drift_psi_threshold`, `watch` from half that
    threshold, and `stable` below it; the run takes the verdict of its worst feature, and the
    summary is the Output page's line, for example `PSI 0.11, watch`. A run with any drifted
    feature is logged at WARNING.

    `None` is the answer when there is nothing to compare - a frame with no rows, or a baseline
    with no features - because either way no comparison happened and a verdict would be about the
    absence of data (DEC-051). The caller reports "drift not measured", which is exactly what it
    already does for a model version that stored no baseline at all; `_unmeasurable_drift` says
    which of the two it was, and the reason is logged at WARNING.
    """
    from engine.contracts import DriftReport, FeatureDrift
    from engine.stages.register import _feature_shares
    from engine.utils.logging import get_logger
    from engine.utils.time import utc_now

    unmeasurable = _unmeasurable_drift(baseline, frame)
    if unmeasurable is not None:
        get_logger(__name__).warning(
            "stage=predict drift=unavailable model_version=%s reason=%s",
            baseline.model_version_id,
            unmeasurable,
        )
        return None
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
