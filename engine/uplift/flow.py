"""The uplift train and score flows: Phase 1's stage rows, uplift work inside them (plan B §5).

An uplift run is still a run. It is created by the same request path, polled through the same
`status.json`, recorded in the same `run.json` and `run_manifest.json`, and fails, cancels and
reports exactly as a Phase 1 run does - because the two classes here *are* Phase 1's flows
(`engine.pipeline._TrainFlow` and `_ScoreFlow`) with only the stage bodies replaced. The Running
screen therefore shows the same eight (train) or seven (score) rows under the same group labels,
and every piece of bookkeeping - the cancel checkpoints either side of a stage, the error written
onto the stage it happened at, the manifest flushed in a `finally` - is the code Phase 1 shipped,
not a copy of it. `engine.pipeline.uplift_flow_for` is the one-line lookup that sends a run here.

**What each stage does instead.**

* *validate* runs Phase 1's checks **and** the six uplift checks; both reports are written, and
  either one blocking stops the run with Phase 1's own `RUN_BLOCKED_BY_VALIDATION`. Rows whose
  outcome window had not elapsed are dropped here, as the uplift checks decided.
* *prepare* decides the features (`engine.uplift.data.fit_feature_spec`). It writes no
  `prepare.json`: that contract's drop reasons have no word for "a date, which in an uplift table
  describes the campaign" or "a type the learners cannot take", and its transforms have no kind for
  "fixed category levels". Filling it would mislabel what happened, so the feature spec - columns,
  levels and every dropped column with its real reason - is recorded on the model card instead.
  The spec decides columns and frequent levels only; it reads neither the treatment nor the
  outcome, so deciding it before the split leaks nothing the hold-out could be scored on.
* *split* is the stratified hold-out of `split_holdout`; `split.json` is written with a zero-row
  validation part, because an uplift learner is fitted without one and saying so is the truth.
  With a two-column key (customer + snapshot date) the hold-out is drawn by customer, so every
  snapshot of a customer is on one side (M53, DEC-855).
* *train* fits the configured meta-learner into the run's `model/` directory through
  `Storage.local_path` and publishes it, exactly as `engine/stages/train.py` does for AutoGluon,
  then writes `model/uplift_model.json`: everything a scoring run needs to replay the model.
* *evaluate* measures the hold-out once (Qini, AUUC, deciles, bootstrap intervals), segments it,
  recommends a policy on it, and keeps its predictions as `uplift_holdout.parquet` - the logged
  data off-policy evaluation and every later "expected incremental conversions" are computed from.
* *explain* is TreeSHAP of the predicted uplift on the hold-out (exact for the X-learner on
  LightGBM, a surrogate otherwise, and the chart's caption says which).
* *register* writes `schema.json` over the raw training columns the model was fitted on, so a
  scoring file goes through Phase 1's own schema check unchanged; re-scores the reigning uplift
  champion on **this** hold-out; decides with the uplift champion rule; and honours
  `governance.approval_required` exactly as Phase 1 does.

**Two-column keys (M53, DEC-853).** Keys are read the way Phase 1's stages read them
(`engine.keys`, DEC-083): every key column is reserved from the features, rows are *identified* by
`StageContext.row_key` (the key itself, or the joined `_row_key` of a composite key), and treatment,
the hold-out, the control group and suppression are decided per *entity*, the key's first column.
`scores.csv` writes each key column separately, as Phase 1's does.

The score flow keeps Phase 1's ingest and schema validation untouched, replays the recorded feature
spec, predicts - and measures drift against the training data, feature PSI through Phase 1's own
`compute_drift` plus the treated-share check, into `uplift_drift.json` - explains every row, and
replaces bands with segments through
`engine.uplift.actions.apply_uplift_actions` - which in turn reuses Phase 1's suppression and control
group rules unchanged.

**Why the champion of another metric is never replaced.** The registry keeps one champion per use
case. When that champion is a propensity model, an uplift model's AUUC cannot be compared with its
ROC AUC; Phase 1 answers the same situation (`METRIC_MISMATCH`) by keeping the challenger a
candidate, and so does this flow. `POST /models/{id}/promote` is the deliberate override, and a
scoring run can always name the uplift version it wants.

**Why an empty slot is not always filled either (DEC-609).** The same single slot decides what a
Phase 1 scoring run with no model named scores (`engine.pipeline.uplift_flow_for`), and the frozen
Phase 1 rule never promotes a ROC AUC challenger over an AUUC champion (`METRIC_MISMATCH`). So if an
uplift run on a use case *configured* as classification crowned itself because nobody held the
slot, that use case's Phase 1 scoring would silently switch to uplift and its classification models
could never be promoted again. The flow therefore auto-promotes into an empty slot only when the
use case itself is configured as `uplift` (`uplift_owns_champion_slot`); a run that chose uplift
through a per-run override keeps its model a candidate, which the uplift screens and a scoring run
that names it use exactly as they would a champion. Once a person has handed the slot to an uplift
model on the Models page, later uplift runs are compared with it under the uplift champion rule.

`numpy` and `pandas` are imported inside function bodies, as everywhere in the engine.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Final

from engine import __version__, keys
from engine.config import Metric, ProblemType, SplitType, recipe_from_config
from engine.contracts import (
    MODEL_DIRECTORY,
    NO_CHAMPION_AT_DECISION,
    BandCount,
    KpiValue,
    ModelStatus,
    ModelVersion,
    Reason,
    RunRecord,
    RunState,
    SplitPart,
    SplitReport,
    StageKey,
)
from engine.errors import RUN_BLOCKED_BY_VALIDATION, EngineError, engine_error
from engine.pipeline import (
    FEATURE_IMPORTANCE_FILENAME,
    REASONS_OFF_DETAIL,
    SCORING_SUMMARY_FILENAME,
    SPLIT_FILENAME,
    VALIDATION_FILENAME,
    Pipeline,
    StageContext,
    _require,
    _ScoreFlow,
    _StageOutcome,
    _TrainFlow,
)
from engine.stages import explain, export, register, validate
from engine.stages.train import predictor_key_for
from engine.storage import StorageError, publish_local_path, run_key
from engine.uplift.config import UpliftBaseModel, UpliftLearner
from engine.uplift.contracts import (
    POLICY_FILENAME,
    QINI_CURVE_FILENAME,
    SEGMENT_ACTIONS,
    SEGMENT_LABELS,
    SEGMENTS_FILENAME,
    UPLIFT_DRIFT_FILENAME,
    UPLIFT_EVALUATION_FILENAME,
    UPLIFT_MODEL_CARD_FILENAME,
    UPLIFT_VALIDATION_FILENAME,
    Segment,
    UpliftModelCard,
)
from engine.utils.ids import seed_from
from engine.utils.logging import get_logger
from engine.utils.text import humanise_count
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    import numpy as np
    import numpy.typing as npt
    import pandas as pd

    from engine.config import PrimaryKey, ResolvedConfig, UseCaseConfig
    from engine.contracts import DriftReport, ScoringSummary, ValidationReport
    from engine.storage import Storage
    from engine.uplift.champion import UpliftChampionDecision
    from engine.uplift.contracts import (
        ConfidenceValue,
        PolicyRecommendation,
        SegmentReport,
        SegmentThresholds,
        UpliftDriftReport,
        UpliftEvaluation,
    )
    from engine.uplift.data import FeatureSpec
    from engine.uplift.learners import UpliftModel, UpliftPrediction

    IntArray = npt.NDArray[np.int_]
    FloatArray = npt.NDArray[np.float64]

__all__ = [
    "HOLDOUT_COLUMNS",
    "UPLIFT_COLUMN",
    "UPLIFT_HOLDOUT_FILENAME",
    "UPLIFT_KPI_LABEL",
    "UpliftScoreFlow",
    "UpliftTrainFlow",
    "check_seed",
    "model_card_key",
    "model_display_name",
    "read_holdout",
    "scores_columns",
    "uplift_owns_champion_slot",
]

_LOGGER = get_logger(__name__)

UPLIFT_HOLDOUT_FILENAME: Final[str] = "uplift_holdout.parquet"
"""The hold-out's treatment, outcome and predictions, kept for OPE and expected conversions."""

HOLDOUT_COLUMNS: Final[tuple[str, ...]] = ("primary_key", "t", "y", "uplift", "p_treated", "p_control")
"""Columns of `uplift_holdout.parquet`, in order."""

UPLIFT_COLUMN: Final[str] = "uplift"
P_TREATED_COLUMN: Final[str] = "p_treated"
P_CONTROL_COLUMN: Final[str] = "p_control"
PREDICTION_COLUMNS: Final[tuple[str, str, str]] = (UPLIFT_COLUMN, P_TREATED_COLUMN, P_CONTROL_COLUMN)

UPLIFT_KPI_LABEL: Final[str] = "Persuadables recommended to contact"
UPLIFT_KPI_FORMULA: Final[str] = 'count_where_action_in(["Treat"])'
"""The rule the uplift KPI tile counts by, written in the configured-formula style it stands in for."""

SLEEPING_DOG_FLOOR: Final[float] = -1.0
"""The lowest uplift a difference of two probabilities can take; the sleeping-dog band starts here."""

PROMOTED_BY: Final[str] = "engine"
NOT_AUTOGLUON: Final[str] = "not used (LightGBM base model)"
"""`ModelVersion.autogluon_version` of a LightGBM uplift model: AutoGluon trained nothing in it."""

UPLIFT_NO_FEATURES: Final[str] = "UPLIFT_NO_FEATURES"
UPLIFT_OUTCOME_UNUSABLE: Final[str] = "UPLIFT_OUTCOME_UNUSABLE"
UPLIFT_TRAIN_FAILED: Final[str] = "UPLIFT_TRAIN_FAILED"
UPLIFT_MODEL_REQUIRED: Final[str] = "UPLIFT_MODEL_REQUIRED"
UPLIFT_FEATURE_MISSING: Final[str] = "UPLIFT_FEATURE_MISSING"

LEARNER_LABELS: Final[dict[UpliftLearner, str]] = {
    UpliftLearner.S_LEARNER: "S-learner",
    UpliftLearner.T_LEARNER: "T-learner",
    UpliftLearner.X_LEARNER: "X-learner",
}
BASE_MODEL_LABELS: Final[dict[UpliftBaseModel, str]] = {
    UpliftBaseModel.LIGHTGBM: "LightGBM",
    UpliftBaseModel.AUTOGLUON_FAST: "AutoGluon",
}

_PROMOTED: Final[frozenset[ModelStatus]] = frozenset({ModelStatus.CHAMPION, ModelStatus.PENDING_APPROVAL})
_PROMOTION_WORDS: Final[dict[ModelStatus, str]] = {
    ModelStatus.PENDING_APPROVAL: "awaiting approval as uplift champion",
    ModelStatus.CHAMPION: "set as uplift champion",
    ModelStatus.CANDIDATE: "kept as candidate",
    ModelStatus.ARCHIVED: "archived",
}


# ---------------------------------------------------------------------------
# Small shared helpers (the API uses these too)
# ---------------------------------------------------------------------------
def check_seed(upload_id: str) -> int:
    """The seed of the randomness check, from the upload so `POST /uplift/runs` and the run agree.

    The check fits a small model with a random fold split; a run seeded differently from the request
    that admitted it could, on a file right at the threshold, block a run the request accepted.
    """
    return seed_from(upload_id)


def model_display_name(learner: UpliftLearner, base_model: UpliftBaseModel) -> str:
    """`X-learner (LightGBM)`: what the Results bar and the registry call an uplift model."""
    return f"{LEARNER_LABELS[learner]} ({BASE_MODEL_LABELS[base_model]})"


def model_card_key(predictor_key: str) -> str:
    """Where `uplift_model.json` lives: inside the run's model directory, beside the pickle."""
    return f"{predictor_key}/{UPLIFT_MODEL_CARD_FILENAME}"


def scores_columns(primary_key: PrimaryKey, reason_columns: tuple[str, ...]) -> tuple[str, ...]:
    """Header of an uplift run's `scores.csv` and `scores.parquet`, in order: every key column first."""
    return (
        *keys.key_columns(primary_key),
        UPLIFT_COLUMN,
        P_TREATED_COLUMN,
        P_CONTROL_COLUMN,
        "segment",
        "band",
        "action",
        *reason_columns,
        "suppressed_reason",
        "control_group",
        "intended_treatment",
    )


def read_holdout(storage: Storage, key: str) -> pd.DataFrame:
    """`uplift_holdout.parquet` as a frame with :data:`HOLDOUT_COLUMNS`; `StorageError` when absent."""
    import pandas as pd

    return pd.read_parquet(io.BytesIO(storage.read_bytes(key)))


def _parquet_bytes(frame: pd.DataFrame) -> bytes:
    import pyarrow  # noqa: F401  # imported here, not at module level, so `import engine` stays fast

    buffer = io.BytesIO()
    frame.to_parquet(buffer, engine="pyarrow", index=False)
    return buffer.getvalue()


def _observed_top_share(
    uplift: FloatArray, t: IntArray, y: IntArray, *, samples: int, seed: int
) -> Callable[[float], ConfidenceValue | None]:
    """The hold-out's observed uplift among its top `fraction`, with its bootstrap interval.

    This is what turns "the model predicts N extra conversions" into "the hold-out *measured* this
    much uplift among the same top share", which is the number `PolicyRecommendation` calls expected.
    """
    from engine.uplift.metrics import bootstrap_uplift_at

    def observed(fraction: float) -> ConfidenceValue | None:
        return bootstrap_uplift_at(uplift, t, y, fraction, samples=samples, seed=seed)

    return observed


def uplift_owns_champion_slot(resolved: ResolvedConfig) -> bool:
    """Whether an uplift model may take the use case's EMPTY champion slot by itself.

    Only when the use case (or the engine defaults) says `problem_type: uplift` - not when a run's
    overrides switched to uplift, which is how the uplift Setup screen starts a run on a use case
    configured for classification. See the module docstring (DEC-609).
    """
    return (
        resolved.config.problem_type is ProblemType.UPLIFT
        and resolved.sources.get("problem_type") != "override"
    )


def _auuc_line(evaluation: UpliftEvaluation) -> str:
    """`AUUC 0.0213 (95% CI 0.0101 to 0.0322)`, with a dash for a bound that does not exist."""

    def fmt(value: float | None) -> str:
        return "—" if value is None else f"{value:.4f}"

    auuc = evaluation.auuc
    return f"AUUC {fmt(auuc.value)} (95% CI {fmt(auuc.ci_low)} to {fmt(auuc.ci_high)})"


def _not_causal_suffix(causal: bool) -> str:
    return "" if causal else " · not causal"


def _entity(ctx: StageContext) -> str:
    """The column naming the customer: the key itself, or the first column of a composite key.

    Treatment is assigned per customer, so this is what the hold-out is grouped by and what the
    control group and suppression decide by (M53, DEC-853) - never the snapshot date.
    """
    return keys.entity_column(ctx.primary_key)


# ---------------------------------------------------------------------------
# The train flow
# ---------------------------------------------------------------------------
class UpliftTrainFlow(_TrainFlow):
    """Phase 1's train flow with uplift stage bodies; see the module docstring."""

    def __init__(self, pipeline: Pipeline, ctx: StageContext) -> None:
        super().__init__(pipeline, ctx)
        self._validation_report: ValidationReport | None = None
        self._treatment: str | None = None
        self._causal: bool = True
        self._rows_set_aside: int = 0
        self._kept: pd.DataFrame | None = None
        self._t: IntArray | None = None
        self._y: IntArray | None = None
        self._positive_label: str | None = None
        self._spec: FeatureSpec | None = None
        self._features: pd.DataFrame | None = None
        self._train_rows: IntArray | None = None
        self._test_rows: IntArray | None = None
        self._uplift_model: UpliftModel | None = None
        self._card: UpliftModelCard | None = None
        self._holdout: UpliftPrediction | None = None
        self._uplift_evaluation: UpliftEvaluation | None = None
        self._rescored: bool = False

    # -- the stages ---------------------------------------------------------
    def _validate(self) -> _StageOutcome:
        """Phase 1's checks and the six uplift checks; either one blocking stops the run."""
        from engine.uplift.checks import run_uplift_checks

        ctx = self._ctx
        profile = _require(self._profile, "the dataset profile")
        frame = _require(self._frame, "the uploaded rows")
        target = _require(ctx.target, "the target column")
        report = validate.validate_for_training(
            frame,
            ctx.config,
            primary_key=ctx.key,
            target=target,
            acknowledged=ctx.config.validation.acknowledged,
            upload_id=profile.upload_id,
            run_id=ctx.run_id,
            row_count=profile.row_count,
        )
        self._write(VALIDATION_FILENAME, report)
        checked = run_uplift_checks(
            frame,
            ctx.config,
            primary_key=ctx.key,
            target=target,
            upload_id=profile.upload_id,
            run_id=ctx.run_id,
            acknowledged=ctx.config.validation.acknowledged,
            seed=check_seed(profile.upload_id),
        )
        self._write(UPLIFT_VALIDATION_FILENAME, checked.report)
        if not (report.passed and checked.report.passed):
            # `POST /uplift/runs` refuses with 409 before a run is created; this is the defensive twin.
            raise engine_error(RUN_BLOCKED_BY_VALIDATION, stage=StageKey.VALIDATE)
        self._validation_report = report
        self._treatment = _require(checked.report.treatment_column, "the treatment column")
        self._causal = checked.report.causal
        kept = frame if checked.keep is None else frame.loc[checked.keep]
        # A row with no recorded outcome did not fail to convert; Phase 1's prepare drops it too.
        known = kept[target].notna().to_numpy(dtype=bool)
        self._rows_set_aside = checked.report.rows_immature + int((~known).sum())
        # After the checks, as in Phase 1: the joined row key of a composite key is the engine's, and
        # no check reports on it. A one-column key adds nothing (DEC-083).
        self._kept = keys.with_row_key(kept.loc[known], ctx.key_columns)
        detail = (
            f"{validate.validation_detail(report)} · treatment column {self._treatment}"
            f"{_not_causal_suffix(self._causal)}"
        )
        if self._rows_set_aside:
            detail += f" · {humanise_count(self._rows_set_aside)} rows set aside"
        return _StageOutcome(detail, len(self._kept.index))

    def _prepare(self) -> _StageOutcome:
        """The treatment and outcome arrays and the feature spec; `prepare.json` is not written.

        See the module docstring for why the Phase 1 report is skipped rather than half-filled.
        """
        from engine.uplift.data import apply_feature_spec, coerce_outcome, coerce_treatment, fit_feature_spec

        ctx = self._ctx
        kept = _require(self._kept, "the validated rows")
        treatment = _require(self._treatment, "the treatment column")
        target = _require(ctx.target, "the target column")
        t, bad = coerce_treatment(kept[treatment])
        if t is None:  # pragma: no cover - TREATMENT_NOT_BINARY blocked this file at validate
            raise EngineError(
                UPLIFT_OUTCOME_UNUSABLE,
                f"{bad} rows have a treatment value that is not 0 or 1.",
                stage=StageKey.PREPARE,
            )
        try:
            y, positive = coerce_outcome(kept[target], ctx.config.target.positive_label)
        except ValueError as exc:
            raise EngineError(
                UPLIFT_OUTCOME_UNUSABLE,
                str(exc),
                suggestion="Choose an outcome column with exactly two values, recorded for every row.",
                stage=StageKey.PREPARE,
            ) from exc
        spec = fit_feature_spec(
            kept,
            ctx.config,
            primary_key=ctx.key,
            target=target,
            treatment_column=treatment,
            validation=self._validation_report,
        )
        if not spec.feature_columns:
            raise EngineError(
                UPLIFT_NO_FEATURES,
                "No column of this file can be used to describe a customer: every one is reserved, "
                "a date, personal data, constant or an identifier.",
                suggestion="Add columns that describe the customers before the campaign.",
                stage=StageKey.PREPARE,
            )
        self._t, self._y, self._positive_label, self._spec = t, y, positive, spec
        self._features = apply_feature_spec(kept, spec)
        self._recipe = recipe_from_config(
            ctx.config,
            primary_key=ctx.key,
            feature_columns=spec.feature_columns,
            seed=self._seed,
            target=target,
        )
        self._manifest.recipe = self._recipe
        features = len(spec.feature_columns)
        segments = [
            f"{humanise_count(len(kept.index))} rows ready",
            f"{features} {'feature' if features == 1 else 'features'}",
        ]
        dropped = len(spec.dropped)
        if dropped:
            segments.append(f"{dropped} {'column' if dropped == 1 else 'columns'} dropped")
        return _StageOutcome(" · ".join(segments), len(kept.index))

    def _split_and_fit(self) -> _StageOutcome:
        """The hold-out, stratified on treatment and outcome, and an honest `split.json`.

        With a composite key the hold-out is drawn by customer (`group_column` is the entity), so
        no customer has a snapshot on each side (M53, DEC-855).
        """
        from engine.uplift.data import split_holdout

        ctx = self._ctx
        t, y = _require(self._t, "the treatment array"), _require(self._y, "the outcome array")
        fraction = ctx.config.uplift.test_fraction
        group_column = _entity(ctx) if keys.is_composite(ctx.primary_key) else None
        groups = (
            None
            if group_column is None
            else _require(self._kept, "the validated rows")[group_column].to_numpy(dtype=object)
        )
        train_rows, test_rows = split_holdout(t, y, test_fraction=fraction, seed=self._seed, groups=groups)
        self._train_rows, self._test_rows = train_rows, test_rows
        total = len(t)
        detail = (
            f"train {humanise_count(len(train_rows))} · test {humanise_count(len(test_rows))} · "
            f"stratified on treatment and outcome"
        )
        if group_column is not None:
            detail += f" · grouped by {group_column}"

        def part(name: Literal["train", "validation", "test"], rows: IntArray | None) -> SplitPart:
            if rows is None or len(rows) == 0:
                return SplitPart(name=name, rows=0, share=0.0)
            positives = int(y[rows].sum())
            return SplitPart(
                name=name,
                rows=len(rows),
                share=round(len(rows) / total, 4),
                positive_rows=positives,
                positive_rate=round(positives / len(rows), 4),
            )

        report = SplitReport(
            run_id=ctx.run_id,
            type=SplitType.RANDOM_STRATIFIED,
            time_column=None,
            group_column=group_column,
            parts=(part("train", train_rows), part("validation", None), part("test", test_rows)),
            validation_fraction=0.0,
            test_fraction=fraction,
            seed=self._seed,
            detail=detail,
            split_at=utc_now(),
        )
        self._write(SPLIT_FILENAME, report)
        return _StageOutcome(detail, total)

    def _train(self) -> _StageOutcome:
        """Fit the meta-learner into the run's model directory, publish it, write its card."""
        from engine.uplift.learners import make_learner, save_model
        from engine.uplift.segments import resolve_thresholds

        ctx = self._ctx
        uplift = ctx.config.uplift
        features = _require(self._features, "the feature matrix")
        t, y = _require(self._t, "the treatment array"), _require(self._y, "the outcome array")
        rows = _require(self._train_rows, "the training rows")
        spec = _require(self._spec, "the feature spec")
        predictor_key = predictor_key_for(ctx.run_id)
        directory = self._storage.local_path(predictor_key)
        model = make_learner(
            uplift.learner,
            uplift.base_model,
            seed=self._seed,
            time_limit_s=uplift.time_limit_minutes * 60.0,
            work_dir=directory,
        )
        try:
            model.fit(features.iloc[rows], t[rows], y[rows])
        except ValueError as exc:
            raise EngineError(
                UPLIFT_TRAIN_FAILED,
                str(exc),
                suggestion="Check that both groups have customers who converted and who did not.",
                stage=StageKey.TRAIN,
            ) from exc
        ctx.cancel.raise_if_cancelled()
        save_model(model, directory)
        # Written through `local_path`, so on a remote store it is a mirror until published - the
        # same step, for the same reason, as the AutoGluon predictor in `engine/stages/train.py`.
        publish_local_path(self._storage, predictor_key)
        base_rate = float(y[rows].mean())
        card = UpliftModelCard(
            learner=uplift.learner,
            base_model=uplift.base_model,
            feature_columns=model.feature_columns,
            categorical_levels=dict(spec.categorical_levels),
            dropped_columns=dict(spec.dropped),
            treatment_column=_require(self._treatment, "the treatment column"),
            outcome_column=_require(ctx.target, "the target column"),
            positive_label=_require(self._positive_label, "the positive label"),
            propensity=model.propensity,
            base_rate=base_rate,
            training_rows=len(rows),
            causal=self._causal,
            segment_thresholds=resolve_thresholds(uplift.segments, base_rate=base_rate),
            engine_version=__version__,
            trained_at=utc_now(),
        )
        self._storage.write_model(model_card_key(predictor_key), card)
        self._uplift_model, self._card = model, card
        self._artefacts[MODEL_DIRECTORY] = predictor_key
        _LOGGER.info(
            "uplift train: learner=%s base=%s rows=%d features=%d",
            uplift.learner.value,
            uplift.base_model.value,
            len(rows),
            len(model.feature_columns),
        )
        detail = (
            f"{model_display_name(uplift.learner, uplift.base_model)} · "
            f"{humanise_count(len(rows))} training rows · {model.propensity:.0%} treated"
        )
        return _StageOutcome(detail, len(rows))

    def _evaluate(self) -> _StageOutcome:
        """Measure the hold-out once, segment it, recommend a policy on it, keep its predictions.

        TEST IS FINAL-DECISION-ONLY: nothing here chooses a model, a feature or a cut. The segments
        and the policy are the configured ones, applied to rows the learners never saw.
        """
        from engine.uplift.metrics import evaluate_uplift
        from engine.uplift.policy import recommend_policy
        from engine.uplift.segments import assign_segments, segment_report

        ctx = self._ctx
        uplift = ctx.config.uplift
        model = _require(self._uplift_model, "the fitted uplift model")
        card = _require(self._card, "the model card")
        features = _require(self._features, "the feature matrix")
        rows = _require(self._test_rows, "the hold-out rows")
        t, y = _require(self._t, "the treatment array")[rows], _require(self._y, "the outcome array")[rows]
        prediction = model.predict(features.iloc[rows])
        self._holdout = prediction
        evaluation, curve = evaluate_uplift(
            prediction.uplift,
            t,
            y,
            run_id=ctx.run_id,
            learner=card.learner,
            base_model=card.base_model,
            bootstrap_samples=uplift.bootstrap_samples,
            seed=self._seed,
            causal=self._causal,
            holdout_keys=self._holdout_keys(rows),
        )
        self._uplift_evaluation = evaluation
        self._write(UPLIFT_EVALUATION_FILENAME, evaluation)
        self._write(QINI_CURVE_FILENAME, curve)
        segments = assign_segments(prediction.uplift, prediction.p_control, card.segment_thresholds)
        self._write(
            SEGMENTS_FILENAME,
            segment_report(
                prediction.uplift,
                prediction.p_treated,
                prediction.p_control,
                segments,
                card.segment_thresholds,
                run_id=ctx.run_id,
                computed_on="test",
                causal=self._causal,
            ),
        )
        recommendation, _ = recommend_policy(
            prediction.uplift,
            segments,
            uplift.policy,
            run_id=ctx.run_id,
            computed_on="test",
            causal=self._causal,
            observed_top_share=_observed_top_share(
                prediction.uplift, t, y, samples=uplift.bootstrap_samples, seed=self._seed
            ),
        )
        self._write(POLICY_FILENAME, recommendation)
        self._write_holdout(rows, t, y, prediction)
        metrics = {
            Metric.AUUC.value: evaluation.auuc.value,
            "qini_coefficient": evaluation.qini_coefficient.value,
            "average_treatment_effect": evaluation.average_treatment_effect.value,
        }
        for name, bound in (
            ("auuc_ci_low", evaluation.auuc.ci_low),
            ("auuc_ci_high", evaluation.auuc.ci_high),
        ):
            if bound is not None:
                metrics[name] = bound
        self._manifest.add_metrics(metrics)
        verdict = "measurable uplift" if evaluation.measurable_uplift else "no measurable uplift"
        detail = f"{_auuc_line(evaluation)} · {verdict}{_not_causal_suffix(self._causal)}"
        return _StageOutcome(detail, evaluation.rows_evaluated)

    def _holdout_keys(self, rows: IntArray) -> list[str]:
        """The hold-out rows' keys as text (the row key of a composite key): what
        `holdout_fingerprint` hashes (DEC-670)."""
        kept = _require(self._kept, "the validated rows")
        return [str(key) for key in kept[self._ctx.row_key].iloc[rows].tolist()]

    def _write_holdout(self, rows: IntArray, t: IntArray, y: IntArray, prediction: UpliftPrediction) -> None:
        """`uplift_holdout.parquet`: the logged data OPE and every expected-conversions figure use."""
        import pandas as pd

        kept = _require(self._kept, "the validated rows")
        frame = pd.DataFrame(
            {
                "primary_key": kept[self._ctx.row_key].iloc[rows].astype(str).to_numpy(),
                "t": t,
                "y": y,
                "uplift": prediction.uplift,
                "p_treated": prediction.p_treated,
                "p_control": prediction.p_control,
            },
            columns=list(HOLDOUT_COLUMNS),
        )
        key = run_key(self._ctx.run_id, UPLIFT_HOLDOUT_FILENAME)
        self._storage.write_bytes(key, _parquet_bytes(frame))
        self._artefacts[UPLIFT_HOLDOUT_FILENAME] = key

    def _explain(self) -> _StageOutcome:
        """TreeSHAP of the predicted uplift on (a sample of) the hold-out: the chart, then reasons.

        TEST IS FINAL-DECISION-ONLY: explaining the chosen model selects nothing. The sample is the
        Phase 1 train flow's budget (`EXPLAIN_MAX_ROWS`), drawn with the run's seed.
        """
        import numpy as np

        from engine.uplift.explain import uplift_importance, uplift_reasons

        ctx = self._ctx
        model = _require(self._uplift_model, "the fitted uplift model")
        features = _require(self._features, "the feature matrix")
        rows = _require(self._test_rows, "the hold-out rows")
        prediction = _require(self._holdout, "the hold-out predictions")
        positions = np.arange(len(rows))
        if len(rows) > explain.EXPLAIN_MAX_ROWS:
            rng = np.random.default_rng(self._seed)
            positions = np.sort(rng.choice(len(rows), size=explain.EXPLAIN_MAX_ROWS, replace=False))
        frame = features.iloc[rows[positions]]
        contributions, _expected = model.contributions(frame)
        importance = uplift_importance(contributions, model.feature_columns, run_id=ctx.run_id)
        method = _method_words(model)
        if model.explanation_fidelity is not None and model.explanation_method != _exact_method():
            importance = importance.model_copy(
                update={
                    "caption": f"{importance.caption}; a surrogate explains the model with "
                    f"in-sample R² {model.explanation_fidelity:.2f}"
                }
            )
        self._importance = importance
        self._write(FEATURE_IMPORTANCE_FILENAME, importance)
        if not ctx.config.evaluation.shap:
            return _StageOutcome(f"{method} · {REASONS_OFF_DETAIL}", 0)
        row_keys = _require(self._kept, "the validated rows")[ctx.row_key].iloc[rows[positions]]
        reasons = uplift_reasons(
            contributions,
            frame,
            prediction.uplift[positions],
            [str(key) for key in row_keys.tolist()],
            top_n=ctx.config.evaluation.reasons_per_row,
        )
        key = explain.write_row_explanations(reasons, run_id=ctx.run_id, storage=self._storage)
        self._artefacts[explain.ROW_EXPLANATIONS_FILENAME] = key
        detail = (
            f"{method} · top {ctx.config.evaluation.reasons_per_row} reasons for "
            f"{humanise_count(len(reasons))} hold-out rows"
        )
        return _StageOutcome(detail, len(reasons))

    def _register(self) -> _StageOutcome:
        """`schema.json`, the uplift champion rule on this hold-out, and the registry row."""
        from engine.uplift.champion import decide_uplift_champion

        ctx = self._ctx
        kept = _require(self._kept, "the validated rows")
        spec = _require(self._spec, "the feature spec")
        evaluation = _require(self._uplift_evaluation, "the uplift evaluation")
        train_rows = _require(self._train_rows, "the training rows")
        model_id, number = register.next_version_id(ctx)
        # The RAW training columns the model was fitted on, not the prepared matrix: a scoring file
        # is checked against this schema by Phase 1's `validate_against_schema`, which types the
        # file as uploaded, so a boolean column must be recorded as a boolean, not as a category.
        schema = register.feature_schema(
            kept.iloc[train_rows][list(spec.feature_columns)],
            ctx.config,
            primary_key=ctx.key,
            target=ctx.target,
            model_version_id=model_id,
        )
        self._write(register.SCHEMA_FILENAME, schema)
        # The training distribution of the same raw columns, so a scoring run can measure drift
        # with Phase 1's own PSI (M53, DEC-856): raw on both sides, because the uplift feature spec
        # is replayed on the raw scoring columns, not through Phase 1's prepare.
        baseline = register.drift_baseline(
            kept.iloc[train_rows][list(spec.feature_columns)],
            ctx.config,
            run_id=ctx.run_id,
            model_version_id=model_id,
            primary_key=ctx.key,
        )
        self._write(register.DRIFT_BASELINE_FILENAME, baseline)
        champion = ctx.registry.get_champion(ctx.config.id)
        champion_evaluation, unavailable = self._rescore_uplift_champion(champion)
        decision: UpliftChampionDecision | None = None
        if champion is None and not uplift_owns_champion_slot(ctx.resolved):
            reason = (
                "Not promoted: this use case is not configured as uplift (this run chose uplift "
                "per run), and its one champion slot stays with the problem type it is configured "
                "for; promote this version on the Models page to hand the slot over deliberately, "
                "or name it when scoring"
            )
        elif champion is None or champion_evaluation is not None:
            decision = decide_uplift_champion(
                evaluation,
                champion_evaluation,
                min_improvement_pct=ctx.config.evaluation.champion_min_improvement_pct,
            )
            reason = decision.reason
        else:
            reason = f"Not promoted: the current champion {champion.model_id} {unavailable}."
        self._rescored = champion_evaluation is not None
        version = self._model_version(
            model_id, number, champion, decision, schema_key=run_key(ctx.run_id, register.SCHEMA_FILENAME)
        )
        stored = register.store_model_version(ctx.registry, version)
        self._version = stored
        _LOGGER.info(
            "register: %s version %d status=%s (uplift champion rule)", stored.model_id, number, stored.status
        )
        _LOGGER.info("register: %s", reason)
        return _StageOutcome(f"{_PROMOTION_WORDS[stored.status]} · {reason}", None)

    def _rescore_uplift_champion(self, champion: ModelVersion | None) -> tuple[UpliftEvaluation | None, str]:
        """The reigning uplift champion measured on **this** hold-out, or why it could not be.

        THE STORED SCORE IS NEVER READ, for Phase 1's reason: the champion's `test_score` belongs to
        another hold-out. It is reloaded with its own feature spec and evaluated through the same
        `evaluate_uplift` call on the same rows, so `decide_uplift_champion` compares like with like.
        """
        from engine.uplift.data import FeatureSpec, apply_feature_spec
        from engine.uplift.learners import load_model
        from engine.uplift.metrics import evaluate_uplift

        if champion is None:
            return None, ""
        if champion.metric != Metric.AUUC:
            return None, (
                f"is a {champion.metric_label} model, and an uplift model's AUUC cannot be compared "
                f"with it; promote this version on the Models page to replace it deliberately"
            )
        ctx = self._ctx
        rows = _require(self._test_rows, "the hold-out rows")
        kept = _require(self._kept, "the validated rows")
        try:
            card = self._storage.read_model(model_card_key(champion.predictor_key), UpliftModelCard)
            model = load_model(self._storage.local_path(champion.predictor_key))
        except Exception as exc:  # any unreadable model means "not re-scored", never a failed run
            return None, f"could not be loaded ({type(exc).__name__})"
        spec = FeatureSpec(
            feature_columns=card.feature_columns, categorical_levels=dict(card.categorical_levels)
        )
        try:
            frame = apply_feature_spec(kept.iloc[rows], spec)
        except KeyError:
            return None, "was trained on a column this run's data does not have"
        prediction = model.predict(frame)
        evaluation, _ = evaluate_uplift(
            prediction.uplift,
            _require(self._t, "the treatment array")[rows],
            _require(self._y, "the outcome array")[rows],
            run_id=ctx.run_id,
            learner=card.learner,
            base_model=card.base_model,
            bootstrap_samples=ctx.config.uplift.bootstrap_samples,
            seed=self._seed,
            causal=card.causal,
            holdout_keys=self._holdout_keys(rows),
        )
        self._manifest.add_metrics({Metric.AUUC.value: evaluation.auuc.value}, prefix="champion_")
        return evaluation, ""

    def _model_version(
        self,
        model_id: str,
        number: int,
        champion: ModelVersion | None,
        decision: UpliftChampionDecision | None,
        *,
        schema_key: str,
    ) -> ModelVersion:
        """The registry row; its status follows the decision and `governance.approval_required`."""
        ctx = self._ctx
        card = _require(self._card, "the model card")
        evaluation = _require(self._uplift_evaluation, "the uplift evaluation")
        predictor_key = predictor_key_for(ctx.run_id)
        run_config_key = run_key(ctx.run_id, register.RUN_CONFIG_FILENAME)
        created_at = utc_now()
        keys = {
            register.SCHEMA_FILENAME: schema_key,
            register.RUN_CONFIG_FILENAME: run_config_key,
            FEATURE_IMPORTANCE_FILENAME: run_key(ctx.run_id, FEATURE_IMPORTANCE_FILENAME),
            UPLIFT_EVALUATION_FILENAME: run_key(ctx.run_id, UPLIFT_EVALUATION_FILENAME),
            UPLIFT_HOLDOUT_FILENAME: run_key(ctx.run_id, UPLIFT_HOLDOUT_FILENAME),
            UPLIFT_MODEL_CARD_FILENAME: model_card_key(predictor_key),
            register.DRIFT_BASELINE_FILENAME: run_key(ctx.run_id, register.DRIFT_BASELINE_FILENAME),
            MODEL_DIRECTORY: predictor_key,
        }
        candidate = ModelVersion(
            model_id=model_id,
            use_case_id=ctx.config.id,
            version=number,
            run_id=ctx.run_id,
            created_at=created_at,
            status=ModelStatus.CANDIDATE,
            metric=Metric.AUUC,
            metric_label=ctx.config.catalog.metric_label(Metric.AUUC),
            test_score=evaluation.auuc.value,
            validation_score=None,
            model_display_name=model_display_name(card.learner, card.base_model),
            schema_key=schema_key,
            run_config_key=run_config_key,
            predictor_key=predictor_key,
            drift_baseline_key=run_key(ctx.run_id, register.DRIFT_BASELINE_FILENAME),
            artefact_keys=keys,
            engine_version=__version__,
            autogluon_version=_autogluon_version(card.base_model),
        )
        if decision is None or not decision.promote:
            return candidate
        if ctx.config.governance.approval_required:
            return candidate.model_copy(
                update={
                    "status": ModelStatus.PENDING_APPROVAL,
                    "improvement_pct": decision.improvement_pct,
                    "measured_against_champion_id": (
                        NO_CHAMPION_AT_DECISION if champion is None else champion.model_id
                    ),
                }
            )
        return candidate.model_copy(
            update={
                "status": ModelStatus.CHAMPION,
                "promoted_at": created_at,
                "promoted_by": PROMOTED_BY,
                "promotion_note": decision.reason,
                "previous_champion_id": None if champion is None else champion.model_id,
                "improvement_pct": decision.improvement_pct,
            }
        )

    def _complete(self) -> RunRecord:
        """The finished run record, with AUUC as the headline metric."""
        evaluation = _require(self._uplift_evaluation, "the uplift evaluation")
        version = _require(self._version, "the registered model version")
        return self._run.update(
            state=RunState.DONE,
            finished_at=utc_now(),
            row_count=_require(self._profile, "the dataset profile").row_count,
            best_model=version.model_display_name,
            headline_metric=Metric.AUUC,
            headline_metric_label=version.metric_label,
            headline_score=round(evaluation.auuc.value, 4),
            model_version_id=version.model_id,
            champion=version.status is ModelStatus.CHAMPION,
            beat_previous_champion=version.status in _PROMOTED and self._rescored,
            artefacts=dict(self._artefacts),
            error=None,
        )


def _exact_method() -> str:
    from engine.uplift.learners import EXPLANATION_EXACT

    return EXPLANATION_EXACT


def _method_words(model: UpliftModel) -> str:
    """How the uplift was explained, in the words the Running screen shows."""
    if model.explanation_method == _exact_method():
        return "exact TreeSHAP of the predicted uplift"
    if model.explanation_fidelity is None:
        return "TreeSHAP of a surrogate of the predicted uplift"
    return f"TreeSHAP of a surrogate of the predicted uplift (in-sample R² {model.explanation_fidelity:.2f})"


def _autogluon_version(base_model: UpliftBaseModel) -> str:
    """The AutoGluon version that trained the model, or a plain statement that none did."""
    if base_model is UpliftBaseModel.LIGHTGBM:
        return NOT_AUTOGLUON
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("autogluon.tabular")
    except PackageNotFoundError:
        return "unknown"


# ---------------------------------------------------------------------------
# The score flow
# ---------------------------------------------------------------------------
class UpliftScoreFlow(_ScoreFlow):
    """Phase 1's score flow with uplift stage bodies; see the module docstring.

    Ingest and `validate_against_schema` are Phase 1's, unchanged: the version is resolved once
    there and the file is checked against the schema the uplift training run wrote.
    """

    def __init__(self, pipeline: Pipeline, ctx: StageContext) -> None:
        super().__init__(pipeline, ctx)
        self._card: UpliftModelCard | None = None
        self._features: pd.DataFrame | None = None
        self._model: UpliftModel | None = None
        self._prediction: UpliftPrediction | None = None
        self._recommendation: PolicyRecommendation | None = None
        self._drift: UpliftDriftReport | None = None

    def _validate_against_schema(self) -> _StageOutcome:
        """Phase 1's stage, unchanged; then `run.json` says what kind of run this turned out to be.

        A scoring run of an uplift model is created by Phase 1's `POST /runs`, which records the use
        case's own problem type. Once the version is resolved the run is known to score an uplift
        model, so the record says `uplift` and AUUC - which is what lists and filters read.
        """
        outcome = super()._validate_against_schema()
        version = _require(self._version, "the model version")
        if version.metric == Metric.AUUC:
            self._run.update(
                problem_type=ProblemType.UPLIFT,
                headline_metric=Metric.AUUC,
                headline_metric_label=version.metric_label,
            )
        return outcome

    def _prepare(self) -> _StageOutcome:
        """Replay the training run's feature spec - columns and fixed category levels - once."""
        from engine.uplift.data import FeatureSpec, apply_feature_spec

        version = _require(self._version, "the model version")
        if version.metric != Metric.AUUC:
            raise EngineError(
                UPLIFT_MODEL_REQUIRED,
                f"Model {version.model_id} is a {version.metric_label} model, not an uplift model.",
                suggestion="Score with a model version trained as an uplift model.",
                stage=StageKey.PREPARE,
            )
        card = self._storage.read_model(model_card_key(version.predictor_key), UpliftModelCard)
        spec = FeatureSpec(
            feature_columns=card.feature_columns, categorical_levels=dict(card.categorical_levels)
        )
        try:
            self._features = apply_feature_spec(_require(self._frame, "the uploaded rows"), spec)
        except KeyError as exc:
            raise EngineError(
                UPLIFT_FEATURE_MISSING,
                str(exc.args[0]) if exc.args else "A column the model was trained on is missing.",
                suggestion="Upload a file with the same columns as the training data.",
                stage=StageKey.PREPARE,
            ) from exc
        self._card = card
        return _StageOutcome(
            f"{version.model_display_name} (v{version.version}) · prepared with the feature spec its "
            f"training run recorded"
        )

    def _predict(self) -> _StageOutcome:
        """Load the model once, predict both counterfactual probabilities and the uplift, and
        measure drift against the training data (`uplift_drift.json`, M53)."""
        from engine.uplift.drift import measure_uplift_drift
        from engine.uplift.learners import load_model

        version = _require(self._version, "the model version")
        features = _require(self._features, "the prepared features")
        model = load_model(self._storage.local_path(version.predictor_key))
        prediction = model.predict(features)
        scored = _require(self._frame, "the uploaded rows").copy()
        scored[UPLIFT_COLUMN] = prediction.uplift
        scored[P_TREATED_COLUMN] = prediction.p_treated
        scored[P_CONTROL_COLUMN] = prediction.p_control
        self._model, self._prediction, self._scored = model, prediction, scored
        rows = len(scored.index)
        self._manifest.add_metrics({"mean_predicted_uplift": float(prediction.uplift.mean())} if rows else {})
        card = _require(self._card, "the model card")
        drift = measure_uplift_drift(
            _require(self._frame, "the uploaded rows"),
            self._ctx.config,
            version=version,
            card=card,
            run_id=self._ctx.run_id,
            storage=self._storage,
        )
        self._drift = drift
        self._write(UPLIFT_DRIFT_FILENAME, drift)
        if drift.features is not None:
            self._manifest.add_metrics({"max_psi": drift.features.max_psi}, prefix="drift_")
        _LOGGER.info(
            "predict: %d rows scored by uplift model %s (v%d) drift=%s treated_share=%s",
            rows,
            version.model_id,
            version.version,
            "not measured" if drift.features is None else drift.features.status.value,
            drift.treatment.status,
        )
        return _StageOutcome(
            f"{humanise_count(rows)} rows scored · {version.model_display_name} (v{version.version})"
            f" · {drift.summary}{_not_causal_suffix(card.causal)}",
            rows,
        )

    def _explain_rows(self) -> _StageOutcome:
        """Why each customer's uplift is what it is - every row, as Phase 1's score flow does."""
        from engine.uplift.explain import uplift_reasons

        ctx = self._ctx
        if not ctx.config.evaluation.shap:
            _LOGGER.info("explain_rows: per-row reasons are switched off for this use case")
            return _StageOutcome(REASONS_OFF_DETAIL, 0)
        model = _require(self._model, "the loaded uplift model")
        features = _require(self._features, "the prepared features")
        prediction = _require(self._prediction, "the predictions")
        scored = _require(self._scored, "the scored rows")
        contributions, _expected = model.contributions(features)
        reasons = uplift_reasons(
            contributions,
            features,
            prediction.uplift,
            [str(key) for key in scored[ctx.row_key].tolist()],
            top_n=ctx.config.evaluation.reasons_per_row,
        )
        key = explain.write_row_explanations(reasons, run_id=ctx.run_id, storage=self._storage)
        self._artefacts[explain.ROW_EXPLANATIONS_FILENAME] = key
        self._scored = explain.with_reason_columns(reasons, scored, ctx.config, primary_key=ctx.row_key)
        return _StageOutcome(
            f"{_method_words(model)} · reasons for {humanise_count(len(reasons))} rows", len(reasons)
        )

    def _actions(self) -> _StageOutcome:
        """Segments instead of bands; Phase 1's suppression and control group, unchanged."""
        from engine.uplift.actions import apply_uplift_actions

        ctx = self._ctx
        card = _require(self._card, "the model card")
        scored, recommendation = apply_uplift_actions(
            _require(self._scored, "the scored rows"),
            ctx.config,
            run_id=ctx.run_id,
            primary_key=ctx.row_key,
            entity_key=ctx.entity_key,
            causal=card.causal,
            prediction_columns=PREDICTION_COLUMNS,
            thresholds=card.segment_thresholds,
            observed_top_share=self._training_holdout_share(),
        )
        self._scored = scored
        self._recommendation = recommendation
        self._write(SEGMENTS_FILENAME, _scored_segments(scored, card, run_id=ctx.run_id))
        self._write(POLICY_FILENAME, recommendation)
        suppressed = int(scored["suppressed_reason"].notna().sum())
        control = int(scored["control_group"].astype(bool).sum())
        return _StageOutcome(
            f"{humanise_count(len(scored.index))} rows segmented · "
            f"{humanise_count(recommendation.contacts_recommended)} to treat · "
            f"{humanise_count(suppressed)} suppressed · {humanise_count(control)} held out as control",
            len(scored.index),
        )

    def _training_holdout_share(self) -> Callable[[float], ConfidenceValue | None] | None:
        """The training run's measured hold-out uplift by top share, or `None` when unreadable.

        Seeded with the TRAINING run's seed, so a scoring run quotes the same interval the training
        run's own policy recommendation was built from. Without the file the expected conversions
        are null, never estimated some other way.
        """
        import numpy as np

        version = _require(self._version, "the model version")
        key = version.artefact_keys.get(
            UPLIFT_HOLDOUT_FILENAME, run_key(version.run_id, UPLIFT_HOLDOUT_FILENAME)
        )
        try:
            holdout = read_holdout(self._storage, key)
        except (StorageError, OSError, ValueError):
            _LOGGER.warning("actions: the training run's %s could not be read", UPLIFT_HOLDOUT_FILENAME)
            return None
        return _observed_top_share(
            np.asarray(holdout["uplift"], dtype=np.float64),
            np.asarray(holdout["t"], dtype=np.int_),
            np.asarray(holdout["y"], dtype=np.int_),
            samples=self._ctx.config.uplift.bootstrap_samples,
            seed=seed_from(version.run_id),
        )

    def _export(self) -> _StageOutcome:
        """`scores.csv`, `scores.parquet` and a `scoring_summary.json` whose bands are the segments."""
        ctx = self._ctx
        version = _require(self._version, "the model version")
        card = _require(self._card, "the model card")
        frame = _require(self._scored, "the scored rows")
        files = _write_scores(
            frame,
            ctx.config,
            run_id=ctx.run_id,
            primary_key=ctx.key,
            storage=self._storage,
        )
        self._artefacts.update(files)
        summary = _scoring_summary(
            frame,
            ctx.config,
            run_id=ctx.run_id,
            version=version,
            primary_key=ctx.row_key,
            files=files,
            drift=None if self._drift is None else self._drift.features,
            thresholds=card.segment_thresholds,
            treat=_require(self._recommendation, "the policy recommendation").contacts_recommended,
        )
        self._summary = summary
        self._write(SCORING_SUMMARY_FILENAME, summary)
        self._manifest.add_metrics({"rows_scored": float(summary.rows_scored)})
        return _StageOutcome(
            f"{humanise_count(summary.rows_scored)} rows written to scores.csv · "
            f"{summary.kpi.label} {summary.kpi.display}",
            summary.rows_scored,
        )


def _scored_segments(frame: pd.DataFrame, card: UpliftModelCard, *, run_id: str) -> SegmentReport:
    """`segments.json` over every scored row (`computed_on: scored`)."""
    import numpy as np

    from engine.uplift.segments import segment_report

    return segment_report(
        np.asarray(frame[UPLIFT_COLUMN], dtype=np.float64),
        np.asarray(frame[P_TREATED_COLUMN], dtype=np.float64),
        np.asarray(frame[P_CONTROL_COLUMN], dtype=np.float64),
        np.asarray(frame["segment"], dtype=object),
        card.segment_thresholds,
        run_id=run_id,
        computed_on="scored",
        causal=card.causal,
    )


def _reason_text(cell: object) -> str | None:
    """A reason cell as the sentence the files store; a row with fewer reasons has an empty cell."""
    if isinstance(cell, Reason):
        return cell.text
    if isinstance(cell, dict):
        return Reason.model_validate(cell).text
    return None


def _write_scores(
    frame: pd.DataFrame, config: UseCaseConfig, *, run_id: str, primary_key: PrimaryKey, storage: Storage
) -> dict[str, str]:
    """The uplift `scores.csv` and `scores.parquet`: the same table, in :func:`scores_columns` order.

    A one-column key is written as the frame holds it, as it always was. Each column of a composite
    key is written separately as text (`engine.keys.key_text`), as Phase 1's `scores.csv` does, so an
    id keeps its leading zeros and a snapshot date reads as the date (DEC-083).
    """
    import pandas as pd

    reason_names = explain.reason_column_names(config)
    columns = keys.key_columns(primary_key)
    data: dict[str, pd.Series] = (
        {columns[0]: frame[columns[0]]}
        if len(columns) == 1
        else {name: keys.key_text(frame[name]).astype("object") for name in columns}
    )
    data |= {
        UPLIFT_COLUMN: frame[UPLIFT_COLUMN].astype("float64"),
        P_TREATED_COLUMN: frame[P_TREATED_COLUMN].astype("float64"),
        P_CONTROL_COLUMN: frame[P_CONTROL_COLUMN].astype("float64"),
        "segment": frame["segment"].astype("object"),
        "band": frame["band"].astype("object"),
        "action": frame["action"].astype("object"),
    }
    for name in reason_names:
        cells = frame[name].tolist() if name in frame.columns else [None] * len(frame.index)
        data[name] = pd.Series([_reason_text(cell) for cell in cells], index=frame.index, dtype="object")
    data["suppressed_reason"] = (
        frame["suppressed_reason"].astype("object").where(frame["suppressed_reason"].notna(), other=None)
    )
    data["control_group"] = frame["control_group"].astype(bool)
    data["intended_treatment"] = frame["intended_treatment"].astype(bool)
    table = pd.DataFrame(data, columns=list(scores_columns(primary_key, reason_names)))
    csv_key = run_key(run_id, export.SCORES_CSV)
    storage.write_bytes(csv_key, table.to_csv(index=False, lineterminator="\n").encode("utf-8"))
    parquet_key = run_key(run_id, export.SCORES_PARQUET)
    storage.write_bytes(parquet_key, _parquet_bytes(table))
    return {export.SCORES_CSV: csv_key, export.SCORES_PARQUET: parquet_key}


def _segment_bands(frame: pd.DataFrame, thresholds: SegmentThresholds) -> tuple[BandCount, ...]:
    """The four segments as `ScoringSummary.bands`, in segment order, empty ones included.

    `min_score` is the lowest uplift that lands in the segment: the persuadable cut; the
    sleeping-dog cut for sure things and lost causes (which start just above it); and -1, the lowest
    uplift there is, for sleeping dogs.
    """
    from engine.uplift.segments import SEGMENT_ORDER

    lowest = {
        Segment.PERSUADABLE: thresholds.persuadable_min_uplift,
        Segment.SURE_THING: thresholds.sleeping_dog_max_uplift,
        Segment.LOST_CAUSE: thresholds.sleeping_dog_max_uplift,
        Segment.SLEEPING_DOG: SLEEPING_DOG_FLOOR,
    }
    counts = frame["segment"].value_counts()
    total = len(frame.index)
    return tuple(
        BandCount(
            name=SEGMENT_LABELS[segment],
            action=SEGMENT_ACTIONS[segment],
            min_score=lowest[segment],
            rows=int(counts.get(segment.value, 0)),
            share_pct=0.0 if total == 0 else round(100.0 * int(counts.get(segment.value, 0)) / total, 1),
        )
        for segment in SEGMENT_ORDER
    )


def _scoring_summary(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    version: ModelVersion,
    primary_key: str,
    files: dict[str, str],
    thresholds: SegmentThresholds,
    treat: int,
    drift: DriftReport | None = None,
) -> ScoringSummary:
    """Phase 1's `ScoringSummary`, filled truthfully for segments.

    Phase 1's `export.summarise` counts the actions, the suppression rules that ran, the control
    group and the sample rows, and all of that is the same for an uplift run, so it is called - on a
    copy of the configuration whose score field is `uplift` and whose KPI simply counts rows. Its
    two answers that do not fit are then replaced: the bands become the four segments, and the KPI
    becomes the persuadables the policy recommends contacting.
    """
    from engine.config import KpiConfig, OutputConfig

    summary_config = config.model_copy(
        update={
            "actions": config.actions.model_copy(update={"score_field": UPLIFT_COLUMN}),
            "output": OutputConfig(kpi=KpiConfig(label=UPLIFT_KPI_LABEL, formula="count_rows()")),
        }
    )
    summary = export.summarise(
        frame,
        summary_config,
        run_id=run_id,
        model_version_id=version.model_id,
        model_display_name=version.model_display_name,
        primary_key=primary_key,
        drift=drift,
        files=files,
    )
    return summary.model_copy(
        update={
            "bands": _segment_bands(frame, thresholds),
            "kpi": KpiValue(
                label=UPLIFT_KPI_LABEL,
                formula=UPLIFT_KPI_FORMULA,
                value=float(treat),
                display=humanise_count(treat),
            ),
        }
    )
