"""`Pipeline.run_train`: stage order, the status machine, the prepare seam, the champion rule, the manifest.

The expensive stages are replaced by fakes that record how they were called and return canned
artefacts, so every ordering, cancellation and failure path runs in milliseconds and without
AutoGluon. Three things are deliberately *not* faked, because they are the behaviour this milestone
has to prove rather than describe:

* the prepare seam - `prepare_rows`, `split_dataset` and `fit_transforms` run for real in
  `test_the_seam.py`-style tests below, so "every statistic is fitted on the training rows only"
  is measured rather than asserted about a mock;
* `register` and a real `LocalModelRegistry`, so the champion rule is decided by the code that
  ships, including its refusal to promote on a comparison that did not happen;
* `evaluate.compare_to_baseline` and `explain.write_row_explanations`, which are pure and cheap.

The real AutoGluon run lives in `tests/integration/test_train_flow.py`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.config import (
    Metric,
    MissingValues,
    ModelFamily,
    Outliers,
    ProblemType,
    ResolvedConfig,
    RunMode,
    SplitType,
    ThresholdMode,
    UseCaseConfig,
    resolve_config,
)
from engine.contracts import (
    MODEL_DIRECTORY,
    BestModel,
    CalibrationSummary,
    ColumnProfile,
    ColumnType,
    ConfusionMatrix,
    DatasetFingerprint,
    DatasetProfile,
    DecileBin,
    DecileLift,
    DriftBaseline,
    EvaluationReport,
    FairnessReport,
    FeatureImportance,
    FeatureImportanceItem,
    Leaderboard,
    LeaderboardEntry,
    MetricValue,
    ModelStatus,
    ModelVersion,
    PrepareReport,
    Reason,
    RowExplanation,
    RunManifest,
    RunRecord,
    RunState,
    RunStatus,
    Severity,
    SplitPart,
    SplitReport,
    StageKey,
    ValidationCheck,
    ValidationReport,
    load_artefact,
)
from engine.errors import STAGE_FAILED
from engine.jobs import CancelToken, JobCancelledError
from engine.pipeline import (
    MANIFEST_FILENAME,
    RUN_FILENAME,
    STATUS_FILENAME,
    TRAIN_STAGES,
    Pipeline,
    StageContext,
    running_rows,
)
from engine.registry import LocalModelRegistry
from engine.stages import evaluate, explain, ingest, prepare, register, train, validate
from engine.stages.evaluate import EvaluationError
from engine.stages.prepare import RowPlan
from engine.stages.score import compute_drift
from engine.stages.scorer import TrainError
from engine.stages.train import TrainResult
from engine.storage import LocalStorage, StorageError, run_key
from engine.utils.ids import seed_from
from engine.utils.time import utc_now

# Captured before any monkeypatching, so the seam tests can call the real functions themselves.
prepare_rows_real = prepare.prepare_rows
split_dataset_real = prepare.split_dataset
fit_transforms_real = prepare.fit_transforms
next_version_id_real = register.next_version_id

USE_CASE: str = "targeted-advertisement"
RUN_ID: str = "r_20260921_0000beef"
UPLOAD_ID: str = "u_000000000001"
UPLOAD_KEY: str = f"uploads/{UPLOAD_ID}/source.csv"
PRIMARY_KEY: str = "customer_id"
TARGET: str = "converted_30d"
FEATURES: tuple[str, ...] = ("visits_last_7d", "plan_tier")
PREDICTOR_KEY: str = run_key(RUN_ID, "model")
MODEL_SCORE: float = 0.84
BASELINE_SCORE: float = 0.70

# Every artefact the train flow writes into the run directory, by the order it is written in.
WRITTEN_ARTEFACTS: tuple[str, ...] = (
    "profile.json",
    "validation.json",
    "prepare.json",
    "split.json",
    "leaderboard.json",
    "best_model.json",
    "evaluation.json",
    "confusion_matrix.json",
    "decile_lift.json",
    "fairness.json",
    "baseline.json",
    "feature_importance.json",
    "row_explanations.parquet",
    "schema.json",
    "drift_baseline.json",
)


# ---------------------------------------------------------------------------
# Frames, storage and the stage context
# ---------------------------------------------------------------------------
def make_frame(rows: int = 40) -> pd.DataFrame:
    """A tiny frame with the shipped use case's column roles: key, two features, target."""
    return pd.DataFrame(
        {
            PRIMARY_KEY: [f"C-{index:05d}" for index in range(rows)],
            "visits_last_7d": [index % 11 for index in range(rows)],
            "plan_tier": ["basic" if index % 3 else "premium" for index in range(rows)],
            TARGET: [index % 2 for index in range(rows)],
        }
    )


class RecordingStorage(LocalStorage):
    """A real local store that also remembers every model it was asked to write, in order."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.writes: list[tuple[str, Any]] = []

    def write_model(self, key: str, model: Any) -> None:
        self.writes.append((key, model))
        super().write_model(key, model)

    def statuses(self) -> list[RunStatus]:
        """Every `status.json` written, in order."""
        return [model for key, model in self.writes if key.endswith(STATUS_FILENAME)]

    def keys_written(self) -> list[str]:
        return [key for key, _ in self.writes]


class CountingCancel(CancelToken):
    """Cancels at the n-th checkpoint, so every boundary in the flow can be tested by number."""

    def __init__(self, at: int) -> None:
        super().__init__()
        self.at = at
        self.checks = 0

    def raise_if_cancelled(self) -> None:
        self.checks += 1
        if self.checks == self.at:
            self.cancel()
        super().raise_if_cancelled()


@pytest.fixture
def resolved(config_root: Path) -> ResolvedConfig:
    return resolve_config(USE_CASE, root=config_root)


@pytest.fixture
def storage(tmp_path: Path) -> RecordingStorage:
    store = RecordingStorage(tmp_path / "data")
    store.write_bytes(UPLOAD_KEY, b"customer_id,converted_30d\nC-1,1\n")
    return store


@pytest.fixture
def registry(tmp_path: Path) -> LocalModelRegistry:
    return LocalModelRegistry(tmp_path / "registry.db")


def make_context(
    resolved: ResolvedConfig,
    storage: LocalStorage,
    registry: LocalModelRegistry,
    *,
    cancel: CancelToken | None = None,
    config: UseCaseConfig | None = None,
) -> StageContext:
    return StageContext(
        run_id=RUN_ID,
        mode=RunMode.TRAIN,
        config=resolved.config if config is None else config,
        resolved=resolved,
        storage=storage,
        registry=registry,
        cancel=CancelToken() if cancel is None else cancel,
        primary_key=PRIMARY_KEY,
        target=TARGET,
        upload_key=UPLOAD_KEY,
        model_version_id=None,
    )


def pipeline_for(storage: LocalStorage, registry: LocalModelRegistry) -> Pipeline:
    return Pipeline(storage, registry, _NoJobs())


class _NoJobs:
    """The pipeline only ever asks the runner to cancel, which these tests drive directly."""

    def submit(self, job_id: str, fn: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("run_train must not submit jobs")

    def status(self, job_id: str) -> Any:  # pragma: no cover - never called
        raise AssertionError("run_train must not read job status")

    def cancel(self, job_id: str) -> bool:  # pragma: no cover - never called
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # pragma: no cover - never called
        return None


# ---------------------------------------------------------------------------
# Canned artefacts
# ---------------------------------------------------------------------------
def make_fingerprint() -> DatasetFingerprint:
    return DatasetFingerprint(
        hash="sha256:v1:" + "a" * 8, algorithm="sha256:v1", n_rows=40, columns=(PRIMARY_KEY, TARGET)
    )


def make_profile(frame: pd.DataFrame) -> DatasetProfile:
    columns = tuple(
        ColumnProfile(
            name=str(name),
            position=position,
            dtype=str(frame[name].dtype),
            inferred_type=ColumnType.STRING,
            null_count=0,
            null_rate=0.0,
            distinct_count=int(frame[name].nunique()),
            is_unique=bool(frame[name].is_unique),
            is_constant=False,
            sample_values=(),
            looks_like_id=name == PRIMARY_KEY,
            looks_like_time=False,
        )
        for position, name in enumerate(frame.columns)
    )
    return DatasetProfile(
        upload_id=UPLOAD_ID,
        file_name="customers.csv",
        file_size_bytes=len(frame.index) * 32,
        file_format="csv",
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame.index),
        column_count=len(frame.columns),
        columns=columns,
        primary_key_candidates=(PRIMARY_KEY,),
        time_column_candidates=(),
        target_candidate=TARGET,
        preview_rows=(),
        missing_value_rate_pct=0.0,
        fingerprint=make_fingerprint(),
        profiled_at=utc_now(),
    )


def make_validation(*, passed: bool = True) -> ValidationReport:
    checks = (
        ()
        if passed
        else (
            ValidationCheck(
                code="TARGET_TOO_FEW_POSITIVES",
                severity=Severity.ERROR,
                message="Only 4 positive examples. At least 50 are needed for a reliable model.",
            ),
        )
    )
    return ValidationReport(
        upload_id=UPLOAD_ID,
        run_id=RUN_ID,
        mode=RunMode.TRAIN,
        checks=checks,
        error_count=0 if passed else 1,
        warning_count=0,
        passed=passed,
        validated_at=utc_now(),
    )


def make_row_plan(frame: pd.DataFrame) -> RowPlan:
    return RowPlan(
        rows_in=len(frame.index),
        columns_in=len(frame.columns),
        feature_columns=FEATURES,
        dropped_columns=(),
        row_removals=(),
        transforms=(),
        pii_columns=(),
        consent_column=None,
        consent_rows_removed=0,
        missing_rows_dropped=0,
    )


def make_prepare_report(frame: pd.DataFrame) -> PrepareReport:
    return PrepareReport(
        run_id=RUN_ID,
        rows_in=len(frame.index),
        rows_out=len(frame.index),
        columns_in=len(frame.columns),
        columns_out=len(frame.columns),
        feature_columns=FEATURES,
        dropped_columns=(),
        row_removals=(),
        transforms=(),
        detail=f"{len(frame.index)} rows ready · 2 features",
        prepared_at=utc_now(),
    )


def make_split_report(parts: dict[str, pd.DataFrame]) -> SplitReport:
    total = sum(len(part.index) for part in parts.values())
    return SplitReport(
        run_id=RUN_ID,
        type=SplitType.RANDOM_STRATIFIED,
        time_column=None,
        group_column=None,
        parts=tuple(
            SplitPart(name=name, rows=len(parts[name].index), share=len(parts[name].index) / total)
            for name in ("train", "validation", "test")
        ),
        validation_fraction=0.15,
        test_fraction=0.15,
        seed=seed_from(RUN_ID),
        detail="2 features · stratified split · 15/15% val/test",
        split_at=utc_now(),
    )


def make_leaderboard() -> Leaderboard:
    return Leaderboard(
        run_id=RUN_ID,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        greater_is_better=True,
        entries=(
            LeaderboardEntry(
                rank=1,
                model_name="LightGBM",
                family=ModelFamily.LIGHTGBM,
                family_label="LightGBM",
                validation_score=0.83,
                test_score=0.82,
                fit_time_seconds=1.5,
                predict_time_seconds=0.1,
                is_ensemble=False,
                stack_level=1,
            ),
        ),
        best_model_name="LightGBM",
        models_trained=1,
        time_limit_seconds=60,
        presets="medium_quality",
    )


def make_best_model(*, test_score: float = 0.82, metric: Metric = Metric.ROC_AUC) -> BestModel:
    return BestModel(
        run_id=RUN_ID,
        model_name="LightGBM",
        family=ModelFamily.LIGHTGBM,
        is_ensemble=False,
        display_name="LightGBM",
        metric=metric,
        metric_label="ROC-AUC" if metric is Metric.ROC_AUC else "F1",
        validation_score=0.83,
        test_score=test_score,
        hyperparameters_summary="learning_rate 0.05 · num_leaves 31",
        training_rows=28,
        feature_count=len(FEATURES),
        fit_time_seconds=1.5,
        predictor_key=PREDICTOR_KEY,
        trained_at=utc_now(),
    )


def make_evaluation(*, headline: float, rows: int = 6) -> EvaluationReport:
    return EvaluationReport(
        run_id=RUN_ID,
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        rows_evaluated=rows,
        positive_rate=0.5,
        primary_metric=Metric.ROC_AUC,
        primary_metric_label="ROC-AUC",
        headline_score=headline,
        metrics=(
            MetricValue(id=Metric.ROC_AUC, label="ROC-AUC", value=headline, greater_is_better=True),
            MetricValue(id=Metric.F1, label="F1", value=round(headline - 0.2, 4), greater_is_better=True),
        ),
        extra_metrics={"accuracy": 0.8, "brier_score": 0.12},
        threshold=0.5,
        threshold_mode=ThresholdMode.AUTO,
        threshold_detail="Chosen on the validation split.",
        calibration=CalibrationSummary(method="isotonic", brier_before=0.2, brier_after=0.12),
        evaluated_at=utc_now(),
    )


def make_confusion() -> ConfusionMatrix:
    return ConfusionMatrix(
        run_id=RUN_ID,
        threshold=0.5,
        positive_label="1",
        negative_label="0",
        true_positive=2,
        false_negative=1,
        false_positive=1,
        true_negative=2,
        total=6,
        cells=(2, 1, 1, 2),
        precision=0.6667,
        recall=0.6667,
        specificity=0.6667,
        f1=0.6667,
    )


def make_decile_lift() -> DecileLift:
    return DecileLift(
        run_id=RUN_ID,
        mode="classification",
        base_rate_pct=50.0,
        bins=(
            DecileBin(decile=1, label="1", rows=6, score_min=0.1, score_max=0.9, mean_score=0.5, lift=1.2),
        ),
        values=(1.2,),
        unit="x",
        caption="Lift over the base rate, by decile",
        computed_at=utc_now(),
    )


def make_fairness(*, evaluated: bool = False) -> FairnessReport:
    return FairnessReport(
        run_id=RUN_ID,
        column="region" if evaluated else None,
        evaluated=evaluated,
        reason_not_evaluated=None if evaluated else "No sensitive column is configured.",
        groups=(),
        max_positive_rate_gap=None,
        max_recall_gap=None,
        max_precision_gap=None,
    )


def make_importance() -> FeatureImportance:
    return FeatureImportance(
        run_id=RUN_ID,
        method="permutation",
        top_n=2,
        items=(
            FeatureImportanceItem(rank=1, feature="visits_last_7d", importance=0.08, share_pct=70.0),
            FeatureImportanceItem(rank=2, feature="plan_tier", importance=0.03, share_pct=30.0),
        ),
        caption="Permutation importance on the test split (%)",
    )


def make_reasons() -> explain.RowReasons:
    explanations = tuple(
        RowExplanation(
            primary_key=f"C-{index:05d}",
            score=0.5 + index / 100,
            reasons=(
                Reason(
                    feature="visits_last_7d",
                    value="12",
                    contribution=0.2,
                    direction="up",
                    text="visits_last_7d ↑ (12)",
                ),
            ),
        )
        for index in range(3)
    )
    return explain.RowReasons(explanations=explanations, method="TreeSHAP")


@dataclass
class FakeScorer:
    """Stands in for a fitted `Scorer`; only the champion path reads anything off it."""

    role: str = "model"
    display_name: str = "LightGBM"
    family: ModelFamily | None = ModelFamily.LIGHTGBM
    feature_columns: tuple[str, ...] = FEATURES
    target_column: str = TARGET
    missing_column: str | None = None

    def can_score(self, frame: pd.DataFrame) -> tuple[bool, str]:
        if self.missing_column is None:
            return True, ""
        return False, f"The data is missing 1 column(s) the model needs: {self.missing_column}."


# ---------------------------------------------------------------------------
# The stage stubs
# ---------------------------------------------------------------------------
@dataclass
class StageStubs:
    """Fakes for every stage, recording how the pipeline called them.

    `fail` injects an exception into the first function of a stage, after the call is recorded, so a
    failure test can see that the stage was entered and then failed.
    """

    fail: dict[StageKey, BaseException] = field(default_factory=dict)
    with_baseline: bool = True
    champion_score: float = 0.60
    champion_error: BaseException | None = None
    calls: list[StageKey] = field(default_factory=list)
    frame: pd.DataFrame = field(default_factory=make_frame)
    parts: dict[str, pd.DataFrame] = field(default_factory=dict)
    trained_parts: dict[str, pd.DataFrame] | None = None
    trained_recipe: Any = None
    scored: list[str] = field(default_factory=list)
    fit_index: pd.Index | None = None
    reason_seed: int | None = None
    reason_importance: FeatureImportance | None = None
    champion_scorer: FakeScorer = field(default_factory=lambda: FakeScorer(role="champion"))

    def __post_init__(self) -> None:
        if not self.parts:
            self.parts = {
                "train": self.frame.iloc[:28],
                "validation": self.frame.iloc[28:34],
                "test": self.frame.iloc[34:],
            }

    # -- installation ----------------------------------------------------
    def install(self, monkeypatch: pytest.MonkeyPatch) -> StageStubs:
        monkeypatch.setattr(ingest, "read_upload", self.read_upload)
        monkeypatch.setattr(ingest, "profile_dataset", self.profile_dataset)
        monkeypatch.setattr(validate, "validate_for_training", self.validate_for_training)
        monkeypatch.setattr(prepare, "prepare_rows", self.prepare_rows)
        monkeypatch.setattr(prepare, "split_dataset", self.split_dataset)
        monkeypatch.setattr(prepare, "fit_transforms", self.fit_transforms)
        monkeypatch.setattr(prepare, "prepare", _forbidden_prepare)
        monkeypatch.setattr(train, "train", self.train)
        monkeypatch.setattr(evaluate, "evaluate", self.evaluate)
        monkeypatch.setattr(explain, "global_importance", self.global_importance)
        monkeypatch.setattr(explain, "reasons_for", self.reasons_for)
        monkeypatch.setattr(register, "next_version_id", self.next_version_id)
        monkeypatch.setattr("engine.pipeline.load_scorer", self.load_scorer)
        return self

    def _enter(self, stage: StageKey) -> None:
        self.calls.append(stage)
        failure = self.fail.get(stage)
        if failure is not None:
            raise failure

    # -- ingest ----------------------------------------------------------
    def read_upload(self, storage: Any, key: str, **kwargs: Any) -> ingest.ReadResult:
        self._enter(StageKey.INGEST)
        return ingest.ReadResult(
            frame=self.frame,
            file_format="csv",
            delimiter=",",
            encoding="utf-8",
            row_count=len(self.frame.index),
            row_count_estimated=False,
            truncated=False,
            fingerprint=make_fingerprint(),
        )

    def profile_dataset(self, frame: pd.DataFrame, config: Any, **kwargs: Any) -> DatasetProfile:
        return make_profile(frame)

    # -- validate --------------------------------------------------------
    def validate_for_training(self, frame: pd.DataFrame, config: Any, **kwargs: Any) -> ValidationReport:
        self._enter(StageKey.VALIDATE)
        return make_validation()

    # -- prepare and split ------------------------------------------------
    def prepare_rows(self, frame: pd.DataFrame, config: Any, **kwargs: Any) -> tuple[pd.DataFrame, RowPlan]:
        self._enter(StageKey.PREPARE)
        return self.frame, make_row_plan(self.frame)

    def split_dataset(
        self, frame: pd.DataFrame, config: Any, **kwargs: Any
    ) -> tuple[dict[str, pd.DataFrame], SplitReport]:
        self._enter(StageKey.SPLIT)
        return dict(self.parts), make_split_report(self.parts)

    def fit_transforms(
        self, frame: pd.DataFrame, config: Any, plan: RowPlan, **kwargs: Any
    ) -> tuple[pd.DataFrame, PrepareReport]:
        self.fit_index = kwargs.get("fit_index")
        return self.frame, make_prepare_report(self.frame)

    # -- train ------------------------------------------------------------
    def train(self, recipe: Any, parts: Any, evaluation: Any, **kwargs: Any) -> TrainResult:
        self._enter(StageKey.TRAIN)
        self.trained_recipe = recipe
        self.trained_parts = dict(parts)
        return TrainResult(
            leaderboard=make_leaderboard(),
            best=make_best_model(),
            model=FakeScorer(),
            baseline=(
                FakeScorer(role="baseline", display_name="baseline (logistic regression)")
                if self.with_baseline
                else None
            ),
            predictor_key=PREDICTOR_KEY,
            detail="1 models trained · held-out validation · best: LightGBM",
        )

    # -- evaluate ---------------------------------------------------------
    def evaluate(
        self, scorer: Any, frame: pd.DataFrame, evaluation: Any, **kwargs: Any
    ) -> tuple[EvaluationReport, ConfusionMatrix, DecileLift, FairnessReport]:
        role = getattr(scorer, "role", "model")
        self.scored.append(role)
        if role == "model":
            self._enter(StageKey.EVALUATE)
        if role == "champion":
            if self.champion_error is not None:
                raise self.champion_error
            return (
                make_evaluation(headline=self.champion_score),
                make_confusion(),
                make_decile_lift(),
                make_fairness(),
            )
        headline = MODEL_SCORE if role == "model" else BASELINE_SCORE
        return make_evaluation(headline=headline), make_confusion(), make_decile_lift(), make_fairness()

    # -- explain ----------------------------------------------------------
    def global_importance(self, predictor_key: str, frame: pd.DataFrame, config: Any, **kwargs: Any):
        self._enter(StageKey.EXPLAIN)
        return make_importance()

    def reasons_for(self, scorer: Any, frame: pd.DataFrame, config: Any, **kwargs: Any):
        self.reason_seed = kwargs.get("seed")
        self.reason_importance = kwargs.get("importance")
        return make_reasons()

    # -- register ---------------------------------------------------------
    def next_version_id(self, ctx: Any) -> tuple[str, int]:
        """The one register function that is spied rather than faked: the rest of the stage is real."""
        if StageKey.REGISTER not in self.calls:
            self._enter(StageKey.REGISTER)
        return next_version_id_real(ctx)

    def load_scorer(self, predictor_key: str, storage: Any) -> FakeScorer:
        return self.champion_scorer


def _forbidden_prepare(*args: Any, **kwargs: Any) -> None:
    raise AssertionError(
        "run_train must call prepare_rows/split_dataset/fit_transforms, never the single-frame "
        "prepare(): fitting before the split is the leak DEC-046 removed."
    )


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> StageStubs:
    return StageStubs().install(monkeypatch)


def run_flow(resolved: ResolvedConfig, storage: LocalStorage, registry: LocalModelRegistry, **kwargs):
    """Run the whole train flow against the stubs and return the run record."""
    ctx = make_context(resolved, storage, registry, **kwargs)
    return pipeline_for(storage, registry).run_train(ctx)


def read_manifest(storage: LocalStorage) -> RunManifest:
    return storage.read_model(run_key(RUN_ID, MANIFEST_FILENAME), RunManifest)


def read_status(storage: LocalStorage) -> RunStatus:
    return storage.read_model(run_key(RUN_ID, STATUS_FILENAME), RunStatus)


def read_run(storage: LocalStorage) -> RunRecord:
    return storage.read_model(run_key(RUN_ID, RUN_FILENAME), RunRecord)


def stage(status: RunStatus, key: StageKey):
    return next(row for row in status.stages if row.key is key)


# ---------------------------------------------------------------------------
# Order, status and the finished record
# ---------------------------------------------------------------------------
def test_the_stages_run_in_the_plan_order(stubs, resolved, storage, registry) -> None:
    run_flow(resolved, storage, registry)
    assert tuple(stubs.calls) == TRAIN_STAGES


def test_every_stage_ends_done_with_its_own_detail_line(stubs, resolved, storage, registry) -> None:
    run_flow(resolved, storage, registry)
    status = read_status(storage)
    assert status.state is RunState.DONE
    assert status.current_stage is None
    assert status.progress_pct == 100
    assert [row.state for row in status.stages] == [RunState.DONE] * len(TRAIN_STAGES)
    details = {row.key: row.detail for row in status.stages}
    assert details[StageKey.INGEST] == "40 rows · 4 columns · CSV"
    assert details[StageKey.VALIDATE] == "No problems found"
    assert details[StageKey.PREPARE] == "40 rows ready · 2 features"
    assert details[StageKey.SPLIT] == "2 features · stratified split · 15/15% val/test"
    assert details[StageKey.TRAIN] == "1 models trained · held-out validation · best: LightGBM"
    assert details[StageKey.EVALUATE] == ("ROC-AUC 0.84 · optimised for ROC-AUC · isotonic calibration")
    assert details[StageKey.EXPLAIN] == "top 2 features · TreeSHAP reasons for 3 rows"
    assert details[StageKey.REGISTER] == (
        "top 3 SHAP reasons · awaiting approval as champion · drift baseline stored"
    )
    assert all(row.duration_seconds is not None for row in status.stages)
    assert all(row.started_at is not None and row.ended_at is not None for row in status.stages)


def test_the_five_running_rows_are_the_prototype_lines(stubs, resolved, storage, registry) -> None:
    run_flow(resolved, storage, registry)
    status = read_status(storage)
    labels = tuple(dict.fromkeys(row.group_label for row in status.stages))
    assert labels == running_rows(RunMode.TRAIN)
    assert len(labels) == 5


def test_status_is_rewritten_at_every_transition(stubs, resolved, storage, registry) -> None:
    run_flow(resolved, storage, registry)
    statuses = storage.statuses()
    # one write when each stage starts and one when it finishes
    assert len(statuses) == 2 * len(TRAIN_STAGES)
    running = [status for status in statuses if status.current_stage is not None]
    assert [status.current_stage for status in running] == list(TRAIN_STAGES)
    assert [status.progress_pct for status in statuses[1::2]] == [12, 25, 38, 50, 62, 75, 88, 100]


def test_progress_counts_done_stages_only(stubs, resolved, storage, registry) -> None:
    # Python's round() is half-even, so 12.5 and 62.5 round down: the M3 design's "13 … 63"
    # assumed half-up. The sequence below is what the engine actually writes.
    run_flow(resolved, storage, registry)
    assert [status.progress_pct for status in storage.statuses()] == [
        0,
        12,
        12,
        25,
        25,
        38,
        38,
        50,
        50,
        62,
        62,
        75,
        75,
        88,
        88,
        100,
    ]


def test_the_finished_run_record_carries_the_headline_numbers(stubs, resolved, storage, registry) -> None:
    record = run_flow(resolved, storage, registry)
    assert record.state is RunState.DONE
    assert record.finished_at is not None and record.started_at is not None
    assert record.row_count == 40
    assert record.best_model == "LightGBM"
    assert record.headline_metric is Metric.ROC_AUC
    assert record.headline_metric_label == "ROC-AUC"
    assert record.headline_score == MODEL_SCORE
    assert record.model_version_id == "m_targeted-advertisement_1"
    assert record.champion is False  # approval_required is on, so it waits
    assert record.beat_previous_champion is False
    assert record.error is None
    assert read_run(storage) == record


def test_every_artefact_is_written_and_recorded_in_the_run_record(stubs, resolved, storage, registry) -> None:
    record = run_flow(resolved, storage, registry)
    for name in WRITTEN_ARTEFACTS:
        key = run_key(RUN_ID, name)
        assert storage.exists(key), f"{name} was not written"
        assert record.artefacts[name] == key
    assert record.artefacts[MODEL_DIRECTORY] == PREDICTOR_KEY
    for name in (RUN_FILENAME, STATUS_FILENAME, MANIFEST_FILENAME):
        assert record.artefacts[name] == run_key(RUN_ID, name)


def test_a_run_record_written_by_the_api_is_updated_not_replaced(stubs, resolved, storage, registry) -> None:
    created = RunRecord(
        run_id=RUN_ID,
        use_case_id=USE_CASE,
        use_case_name="Targeted Advertisement",
        mode=RunMode.TRAIN,
        state=RunState.PENDING,
        created_at=utc_now(),
        upload_id=UPLOAD_ID,
        file_name="customers.csv",
        primary_key=PRIMARY_KEY,
        target=TARGET,
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        model_choice="automl",
        overrides={"model_search.time_limit_minutes": 1},
        artefacts={"run_config.json": run_key(RUN_ID, "run_config.json")},
        engine_version="0.1.0",
    )
    storage.write_model(run_key(RUN_ID, RUN_FILENAME), created)
    record = run_flow(resolved, storage, registry)
    assert record.created_at == created.created_at
    assert record.file_name == "customers.csv"
    assert record.overrides == {"model_search.time_limit_minutes": 1}
    assert record.artefacts["run_config.json"] == run_key(RUN_ID, "run_config.json")


def test_the_run_is_marked_running_before_the_first_stage(stubs, resolved, storage, registry) -> None:
    run_flow(resolved, storage, registry)
    first_run_write = next(model for key, model in storage.writes if key.endswith(RUN_FILENAME))
    assert first_run_write.state is RunState.RUNNING
    assert first_run_write.started_at is not None


# ---------------------------------------------------------------------------
# What each stage hands the next one
# ---------------------------------------------------------------------------
def test_train_receives_the_recipe_built_from_the_prepared_feature_columns(
    stubs, resolved, storage, registry
) -> None:
    run_flow(resolved, storage, registry)
    recipe = stubs.trained_recipe
    assert recipe.feature_columns == FEATURES
    assert recipe.seed == seed_from(RUN_ID)
    assert recipe.target == TARGET
    assert recipe.primary_key == PRIMARY_KEY
    assert recipe.use_case_id == USE_CASE


def test_a_datasets_own_label_is_in_neither_the_baseline_nor_the_scored_drift(
    monkeypatch, resolved, storage, registry
) -> None:
    """A run on a built dataset trains on the dataset's label, not the template's target (DEC-957).

    The template names `converted_30d`; the dataset names its label `converted_next_30d`. The
    baseline used to reserve only the template's, so it summarised the label, and every scoring
    file - where the label is empty - came back drifted on it at the largest PSI there is.
    """
    label = "converted_next_30d"
    frame = make_frame().rename(columns={TARGET: label})
    StageStubs(frame=frame).install(monkeypatch)
    ctx = replace(make_context(resolved, storage, registry), target=label)

    pipeline_for(storage, registry).run_train(ctx)

    baseline = storage.read_model(run_key(RUN_ID, register.DRIFT_BASELINE_FILENAME), DriftBaseline)
    assert [feature.feature for feature in baseline.features] == list(FEATURES)
    scoring = frame.assign(**{label: None})
    drift = compute_drift(baseline, scoring, resolved.config, run_id="r_20260922_0000cafe")
    assert drift is not None and label not in {feature.feature for feature in drift.features}


def test_evaluate_and_explain_both_read_the_test_split(stubs, resolved, storage, registry) -> None:
    run_flow(resolved, storage, registry)
    assert stubs.scored == ["model", "baseline"]
    assert stubs.reason_seed == seed_from(RUN_ID)
    assert stubs.reason_importance == make_importance()


def test_the_baseline_table_compares_two_reports_from_the_same_frame(
    stubs, resolved, storage, registry
) -> None:
    run_flow(resolved, storage, registry)
    comparison = load_artefact("baseline.json", storage.read_text(run_key(RUN_ID, "baseline.json")))
    assert comparison.model_beats_baseline is True
    assert comparison.baseline_name == "baseline (logistic regression)"
    roc = next(row for row in comparison.rows if row.id is Metric.ROC_AUC)
    assert (roc.model_value, roc.baseline_value) == (MODEL_SCORE, BASELINE_SCORE)


def test_without_a_baseline_no_comparison_is_written(monkeypatch, resolved, storage, registry) -> None:
    StageStubs(with_baseline=False).install(monkeypatch)
    run_flow(resolved, storage, registry)
    assert not storage.exists(run_key(RUN_ID, "baseline.json"))


def test_row_explanations_are_skipped_when_shap_is_off(monkeypatch, resolved, storage, registry) -> None:
    stubs = StageStubs().install(monkeypatch)
    config = resolved.config.model_copy(
        update={"evaluation": resolved.config.evaluation.model_copy(update={"shap": False})}
    )
    run_flow(resolved, storage, registry, config=config)
    assert not storage.exists(run_key(RUN_ID, "row_explanations.parquet"))
    assert stubs.reason_seed is None
    status = read_status(storage)
    assert stage(status, StageKey.EXPLAIN).detail == "top 2 features · per-row reasons turned off"
    assert stage(status, StageKey.REGISTER).detail == (
        "awaiting approval as champion · drift baseline stored"
    )


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------
def test_the_manifest_of_a_finished_run(stubs, resolved, storage, registry) -> None:
    run_flow(resolved, storage, registry)
    manifest = read_manifest(storage)
    assert manifest.run_id == RUN_ID
    assert manifest.seed == seed_from(RUN_ID)
    assert manifest.recipe is not None
    assert manifest.recipe.recipe_hash == stubs.trained_recipe.recipe_hash
    assert manifest.dataset_fingerprint == make_fingerprint()
    assert manifest.leaderboard_path == run_key(RUN_ID, "leaderboard.json")
    assert manifest.metrics["roc_auc"] == MODEL_SCORE
    assert manifest.metrics["baseline_roc_auc"] == BASELINE_SCORE
    assert manifest.metrics["accuracy"] == 0.8
    assert manifest.metrics["brier_score"] == 0.12
    assert manifest.duration_s > 0.0
    assert manifest.cost_estimate.estimated_usd is None
    assert manifest.cost_estimate.compute_seconds > 0.0
    assert "Nothing was billed" in manifest.cost_estimate.basis
    assert manifest.created_at is not None


def test_the_manifest_is_written_last(stubs, resolved, storage, registry) -> None:
    run_flow(resolved, storage, registry)
    assert storage.keys_written()[-1] == run_key(RUN_ID, MANIFEST_FILENAME)


def test_compute_seconds_is_the_sum_of_the_recorded_stage_durations(
    stubs, resolved, storage, registry
) -> None:
    run_flow(resolved, storage, registry)
    manifest = read_manifest(storage)
    stages = read_status(storage).stages
    total = sum(round(row.duration_seconds or 0.0, 3) for row in stages)
    assert manifest.cost_estimate.compute_seconds == pytest.approx(total, abs=0.02)


def test_a_manifest_write_that_fails_does_not_fail_the_run(
    stubs, monkeypatch, resolved, storage, registry
) -> None:
    original = storage.write_model

    def refuse(key: str, model: Any) -> None:
        if key.endswith(MANIFEST_FILENAME):
            raise StorageError("WRITE_FAILED", "the disk is full", key=key)
        original(key, model)

    monkeypatch.setattr(storage, "write_model", refuse)
    record = run_flow(resolved, storage, registry)
    assert record.state is RunState.DONE
    assert not storage.exists(run_key(RUN_ID, MANIFEST_FILENAME))


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------
FAILURES: tuple[tuple[StageKey, BaseException, str], ...] = (
    (StageKey.INGEST, ingest.IngestError("UPLOAD_EMPTY", "The file is empty."), "UPLOAD_EMPTY"),
    (
        StageKey.VALIDATE,
        StorageError("KEY_NOT_FOUND", "The file could not be found."),
        "KEY_NOT_FOUND",
    ),
    (StageKey.PREPARE, ValueError("a column vanished"), STAGE_FAILED),
    (StageKey.SPLIT, ValueError("A time-based split needs split.time_column."), STAGE_FAILED),
    (
        StageKey.TRAIN,
        TrainError("TRAIN_NO_MODEL_FITTED", "No model could be trained within the time limit."),
        "TRAIN_NO_MODEL_FITTED",
    ),
    (
        StageKey.EVALUATE,
        EvaluationError("EVAL_EMPTY_TEST_SPLIT", "The test split has no rows to evaluate."),
        "EVAL_EMPTY_TEST_SPLIT",
    ),
    (StageKey.EXPLAIN, RuntimeError("shap blew up"), STAGE_FAILED),
    (
        StageKey.REGISTER,
        TrainError("MODEL_NOT_SAVED", "The saved model could not be found."),
        "MODEL_NOT_SAVED",
    ),
)


@pytest.mark.parametrize(("key", "error", "code"), FAILURES, ids=[case[0].value for case in FAILURES])
def test_a_failing_stage_fails_the_run_and_skips_the_rest(
    monkeypatch, resolved, storage, registry, key, error, code
) -> None:
    StageStubs(fail={key: error}).install(monkeypatch)
    with pytest.raises(type(error)):
        run_flow(resolved, storage, registry)
    status = read_status(storage)
    index = TRAIN_STAGES.index(key)
    assert status.state is RunState.FAILED
    assert [row.state for row in status.stages] == (
        [RunState.DONE] * index + [RunState.FAILED] + [RunState.SKIPPED] * (7 - index)
    )
    failed = stage(status, key)
    assert failed.error is not None
    assert failed.error.code == code
    assert failed.error.stage is key
    record = read_run(storage)
    assert record.state is RunState.FAILED
    assert record.finished_at is not None
    assert record.error is not None and record.error.code == code
    assert storage.exists(run_key(RUN_ID, MANIFEST_FILENAME))


def test_an_unexpected_failure_never_leaks_its_own_words(monkeypatch, resolved, storage, registry) -> None:
    StageStubs(fail={StageKey.EXPLAIN: RuntimeError("connection to 10.0.0.4 refused")}).install(monkeypatch)
    with pytest.raises(RuntimeError):
        run_flow(resolved, storage, registry)
    record = read_run(storage)
    assert record.error is not None
    assert record.error.code == STAGE_FAILED
    assert record.error.message == "Generating explanations failed unexpectedly."
    assert "10.0.0.4" not in record.error.message


def test_a_run_blocked_by_validation_fails_with_its_own_code(
    monkeypatch, resolved, storage, registry
) -> None:
    stubs = StageStubs().install(monkeypatch)
    monkeypatch.setattr(validate, "validate_for_training", lambda *a, **k: make_validation(passed=False))
    with pytest.raises(Exception) as caught:
        run_flow(resolved, storage, registry)
    assert getattr(caught.value, "code", "") == "RUN_BLOCKED_BY_VALIDATION"
    status = read_status(storage)
    assert stage(status, StageKey.VALIDATE).state is RunState.FAILED
    # the report is written before the run is stopped, so the Data page can show why
    assert storage.exists(run_key(RUN_ID, "validation.json"))
    assert stubs.calls == [StageKey.INGEST]


def test_the_manifest_of_a_run_that_failed_in_ingest(monkeypatch, resolved, storage, registry) -> None:
    StageStubs(fail={StageKey.INGEST: ingest.IngestError("UPLOAD_EMPTY", "The file is empty.")}).install(
        monkeypatch
    )
    with pytest.raises(ingest.IngestError):
        run_flow(resolved, storage, registry)
    manifest = read_manifest(storage)
    assert manifest.recipe is None
    assert manifest.metrics == {}
    assert manifest.leaderboard_path is None
    assert manifest.cost_estimate.estimated_usd is None
    assert manifest.dataset_fingerprint.algorithm == "sha256-file"
    assert manifest.dataset_fingerprint.n_rows == 0
    assert manifest.dataset_fingerprint.columns == ()
    assert manifest.dataset_fingerprint.hash == hashlib.sha256(storage.read_bytes(UPLOAD_KEY)).hexdigest()


def test_the_manifest_of_a_run_whose_upload_cannot_even_be_read(
    monkeypatch, resolved, storage, registry
) -> None:
    storage.delete(UPLOAD_KEY)
    StageStubs(fail={StageKey.INGEST: ingest.IngestError("UPLOAD_EMPTY", "The file is empty.")}).install(
        monkeypatch
    )
    with pytest.raises(ingest.IngestError):
        run_flow(resolved, storage, registry)
    fingerprint = read_manifest(storage).dataset_fingerprint
    assert fingerprint.algorithm == "unavailable"
    assert fingerprint.hash == ""


def test_the_manifest_of_a_run_that_failed_in_train(monkeypatch, resolved, storage, registry) -> None:
    StageStubs(fail={StageKey.TRAIN: TrainError("TRAIN_NO_MODEL_FITTED", "No model.")}).install(monkeypatch)
    with pytest.raises(TrainError):
        run_flow(resolved, storage, registry)
    manifest = read_manifest(storage)
    assert manifest.recipe is not None, "prepare had already settled the recipe"
    assert manifest.metrics == {}
    assert manifest.leaderboard_path is None
    assert manifest.dataset_fingerprint == make_fingerprint()
    assert manifest.cost_estimate.compute_seconds > 0.0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
CHECKPOINTS: tuple[tuple[int, StageKey, str], ...] = tuple(
    (index * 2 + offset + 1, key, "before" if offset == 0 else "after")
    for index, key in enumerate(TRAIN_STAGES)
    for offset in (0, 1)
)


@pytest.mark.parametrize(
    ("checkpoint", "key", "when"), CHECKPOINTS, ids=[f"{case[2]}-{case[1].value}" for case in CHECKPOINTS]
)
def test_cancelling_at_any_checkpoint_stops_at_that_stage(
    stubs, resolved, storage, registry, checkpoint, key, when
) -> None:
    cancel = CountingCancel(at=checkpoint)
    with pytest.raises(JobCancelledError):
        run_flow(resolved, storage, registry, cancel=cancel)
    status = read_status(storage)
    index = TRAIN_STAGES.index(key)
    assert status.state is RunState.CANCELLED
    assert [row.state for row in status.stages] == (
        [RunState.DONE] * index + [RunState.CANCELLED] + [RunState.SKIPPED] * (7 - index)
    )
    cancelled = stage(status, key)
    assert cancelled.error is None
    assert (cancelled.started_at is None) == (when == "before")
    record = read_run(storage)
    assert record.state is RunState.CANCELLED
    assert record.finished_at is not None
    assert record.error is None
    manifest = read_manifest(storage)
    assert manifest.duration_s > 0.0
    assert manifest.cost_estimate.estimated_usd is None


def test_a_run_cancelled_before_it_starts_stops_at_ingest(stubs, resolved, storage, registry) -> None:
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(JobCancelledError):
        run_flow(resolved, storage, registry, cancel=cancel)
    status = read_status(storage)
    assert stage(status, StageKey.INGEST).state is RunState.CANCELLED
    assert status.progress_pct == 0
    assert storage.exists(run_key(RUN_ID, MANIFEST_FILENAME))


def test_a_cancelled_run_records_what_it_measured_before_stopping(stubs, resolved, storage, registry) -> None:
    # cancel after evaluate (checkpoint 12), so the metrics are known but the model is not registered
    with pytest.raises(JobCancelledError):
        run_flow(resolved, storage, registry, cancel=CountingCancel(at=12))
    manifest = read_manifest(storage)
    assert manifest.metrics["roc_auc"] == MODEL_SCORE
    assert manifest.recipe is not None
    assert manifest.leaderboard_path == run_key(RUN_ID, "leaderboard.json")
    assert registry.list_versions(USE_CASE) == ()


# ---------------------------------------------------------------------------
# The champion rule: both models re-scored on this run's test split
# ---------------------------------------------------------------------------
def make_champion(
    registry: LocalModelRegistry,
    *,
    stored_score: float,
    metric: Metric = Metric.ROC_AUC,
    predictor_key: str = "runs/r_older/model",
) -> ModelVersion:
    """A champion whose STORED score is deliberately absurd, so using it would be visible."""
    version = ModelVersion(
        model_id="m_targeted-advertisement_1",
        use_case_id=USE_CASE,
        version=1,
        run_id="r_20260101_00000001",
        created_at=utc_now(),
        status=ModelStatus.CANDIDATE,
        metric=metric,
        metric_label="ROC-AUC" if metric is Metric.ROC_AUC else "F1",
        test_score=stored_score,
        validation_score=stored_score,
        model_display_name="XGBoost",
        schema_key="runs/r_older/schema.json",
        run_config_key="runs/r_older/run_config.json",
        predictor_key=predictor_key,
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )
    registry.register(version)
    return registry.promote(version.model_id, by="test", note="first champion")


def test_with_no_champion_the_first_model_is_promoted(stubs, resolved, storage, registry) -> None:
    record = run_flow(resolved, storage, registry)
    stored = registry.get(record.model_version_id or "")
    assert stored.status is ModelStatus.PENDING_APPROVAL
    assert stored.test_score == MODEL_SCORE
    assert record.beat_previous_champion is False
    assert "champion_roc_auc" not in read_manifest(storage).metrics


def test_the_champion_is_re_scored_and_its_stored_score_is_never_used(
    monkeypatch, resolved, storage, registry
) -> None:
    StageStubs(champion_score=0.60).install(monkeypatch)
    champion = make_champion(registry, stored_score=0.99)  # absurd, and never read
    record = run_flow(resolved, storage, registry)
    manifest = read_manifest(storage)
    assert manifest.metrics["champion_roc_auc"] == 0.60
    assert manifest.metrics["roc_auc"] == MODEL_SCORE
    stored = registry.get(record.model_version_id or "")
    assert stored.status is ModelStatus.PENDING_APPROVAL
    assert stored.improvement_pct == pytest.approx((MODEL_SCORE - 0.60) / 0.60 * 100, abs=0.01)
    assert record.beat_previous_champion is True
    assert registry.get(champion.model_id).test_score == 0.99, "the champion's own row is untouched"


def test_a_champion_that_wins_the_fresh_comparison_keeps_the_crown(
    monkeypatch, resolved, storage, registry
) -> None:
    StageStubs(champion_score=0.90).install(monkeypatch)
    make_champion(registry, stored_score=0.10)  # absurdly low, and never read
    record = run_flow(resolved, storage, registry)
    stored = registry.get(record.model_version_id or "")
    assert stored.status is ModelStatus.CANDIDATE
    assert record.champion is False
    assert record.beat_previous_champion is False
    assert read_manifest(storage).metrics["champion_roc_auc"] == 0.90
    assert read_status(storage).stages[-1].detail == (
        "top 3 SHAP reasons · kept as candidate · drift baseline stored"
    )


def test_a_champion_that_cannot_be_loaded_is_not_compared_against(
    monkeypatch, resolved, storage, registry
) -> None:
    stubs = StageStubs().install(monkeypatch)

    def refuse(key: str, store: Any) -> None:
        raise TrainError("MODEL_NOT_SAVED", "The saved model could not be found on disk.")

    monkeypatch.setattr("engine.pipeline.load_scorer", refuse)
    make_champion(registry, stored_score=0.99)
    record = run_flow(resolved, storage, registry)
    stored = registry.get(record.model_version_id or "")
    assert stored.status is ModelStatus.CANDIDATE, "a comparison that did not happen promotes nothing"
    assert record.beat_previous_champion is False
    assert "champion_roc_auc" not in read_manifest(storage).metrics
    assert stubs.scored == ["model", "baseline"], "the champion was never scored"


def test_a_champion_whose_schema_no_longer_fits_is_not_compared_against(
    monkeypatch, resolved, storage, registry
) -> None:
    stubs = StageStubs().install(monkeypatch)
    stubs.champion_scorer = FakeScorer(role="champion", missing_column="ad_ctr_90d")
    make_champion(registry, stored_score=0.99)
    record = run_flow(resolved, storage, registry)
    assert registry.get(record.model_version_id or "").status is ModelStatus.CANDIDATE
    assert stubs.scored == ["model", "baseline"]


def test_a_champion_that_cannot_be_evaluated_is_not_compared_against(
    monkeypatch, resolved, storage, registry
) -> None:
    StageStubs(champion_error=EvaluationError("EVAL_TARGET_COLUMN_MISSING", "no target column")).install(
        monkeypatch
    )
    make_champion(registry, stored_score=0.99)
    record = run_flow(resolved, storage, registry)
    assert registry.get(record.model_version_id or "").status is ModelStatus.CANDIDATE
    assert "champion_roc_auc" not in read_manifest(storage).metrics


def test_a_champion_optimised_for_another_metric_is_re_scored_on_the_challengers(
    monkeypatch, resolved, storage, registry
) -> None:
    """The champion is re-scored on the challenger's metric, and the fresh number is recorded.

    What is then *decided* is the register stage's call, and the committed `register` keeps the
    registry's `METRIC_MISMATCH` guard (its own docstring says so): the re-scored copy carries the
    fresh `test_score` but the champion's original `metric`, so the comparison is refused and the
    version stays a candidate. The M3 design (§7.2, fourth case) wanted the copy to carry the
    challenger's metric as well, so the guard could not fire. Either way no stored score decides
    anything; this test pins the behaviour that ships and names the difference.
    """
    StageStubs(champion_score=0.60).install(monkeypatch)
    champion = make_champion(registry, stored_score=0.99, metric=Metric.F1)
    record = run_flow(resolved, storage, registry)
    assert read_manifest(storage).metrics["champion_roc_auc"] == 0.60
    stored = registry.get(record.model_version_id or "")
    assert stored.status is ModelStatus.CANDIDATE
    assert record.beat_previous_champion is False
    assert registry.get(champion.model_id).metric is Metric.F1, "its own row keeps its own metric"


def test_without_approval_the_promoted_model_becomes_the_champion_at_once(
    monkeypatch, resolved, storage, registry
) -> None:
    StageStubs(champion_score=0.60).install(monkeypatch)
    previous = make_champion(registry, stored_score=0.99)
    config = resolved.config.model_copy(
        update={"governance": resolved.config.governance.model_copy(update={"approval_required": False})}
    )
    record = run_flow(resolved, storage, registry, config=config)
    stored = registry.get(record.model_version_id or "")
    assert stored.status is ModelStatus.CHAMPION
    assert stored.previous_champion_id == previous.model_id
    assert record.champion is True
    assert record.beat_previous_champion is True
    assert registry.get(previous.model_id).status is ModelStatus.ARCHIVED
    assert read_status(storage).stages[-1].detail.endswith("set as champion · drift baseline stored")


def test_the_registered_score_is_the_one_evaluate_measured(stubs, resolved, storage, registry) -> None:
    # best_model.json keeps AutoGluon's own number (0.82); the registry row carries the number the
    # champion rule compares, which must come from the same measurement path as a re-scored
    # champion - evaluate's (0.84).
    record = run_flow(resolved, storage, registry)
    best = load_artefact("best_model.json", storage.read_text(run_key(RUN_ID, "best_model.json")))
    assert best.test_score == 0.82
    assert registry.get(record.model_version_id or "").test_score == MODEL_SCORE


# ---------------------------------------------------------------------------
# The prepare seam: fitted on the training rows only (DEC-046)
# ---------------------------------------------------------------------------
def seam_frame(rows: int = 600) -> pd.DataFrame:
    """A frame whose feature values differ sharply between the training and hold-out rows.

    The row order is what the split assigns from, so planting extreme values in the *last* rows
    gives the validation and test partitions a different distribution from the training rows: a
    statistic fitted over the whole frame cannot equal one fitted over the training rows.
    """
    visits = [index % 20 for index in range(rows)]
    spend = [float(index % 50) for index in range(rows)]
    for index in range(rows - 120, rows):
        visits[index] = 10_000 + index
        spend[index] = 90_000.0 + index
    frame = pd.DataFrame(
        {
            PRIMARY_KEY: [f"C-{index:05d}" for index in range(rows)],
            "visits_last_7d": visits,
            "ad_ctr_90d": spend,
            "plan_tier": ["basic" if index % 4 else "premium" for index in range(rows)],
            TARGET: [index % 2 for index in range(rows)],
        }
    )
    frame.loc[frame.index[:10], "visits_last_7d"] = None
    return frame


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> StageStubs:
    """Stubs for everything except the three real prepare functions, which are spied on."""
    stubs = StageStubs(frame=seam_frame())
    stubs.install(monkeypatch)
    monkeypatch.setattr(prepare, "prepare_rows", _spy(stubs, prepare_rows_real, StageKey.PREPARE))
    monkeypatch.setattr(prepare, "split_dataset", _spy(stubs, split_dataset_real, StageKey.SPLIT))
    monkeypatch.setattr(prepare, "fit_transforms", _fit_spy(stubs))
    return stubs


def _spy(stubs: StageStubs, function: Any, key: StageKey) -> Any:
    def spied(*args: Any, **kwargs: Any) -> Any:
        stubs._enter(key)
        return function(*args, **kwargs)

    return spied


def _fit_spy(stubs: StageStubs) -> Any:
    def spied(frame: pd.DataFrame, config: Any, plan: RowPlan, **kwargs: Any) -> Any:
        stubs.fit_index = kwargs.get("fit_index")
        return fit_transforms_real(frame, config, plan, **kwargs)

    return spied


def transform(report: PrepareReport, kind: str, column: str) -> Any:
    """The one recorded transform of `kind` that was fitted for `column`."""
    return next(item for item in report.transforms if item.kind == kind and item.columns == (column,))


def fill_config(resolved: ResolvedConfig) -> UseCaseConfig:
    """The shipped use case, with the two fitted transforms both switched on."""
    return resolved.config.model_copy(
        update={
            "prepare": resolved.config.prepare.model_copy(
                update={"missing_values": MissingValues.FILL, "outliers": Outliers.CLIP}
            )
        }
    )


def test_the_seam_fits_every_transform_on_the_training_partition_only(
    seam, resolved, storage, registry
) -> None:
    config = fill_config(resolved)
    run_flow(resolved, storage, registry, config=config)

    rows, _plan = prepare_rows_real(seam_frame(), config, primary_key=PRIMARY_KEY, target=TARGET)
    parts, _ = split_dataset_real(rows, config, run_id=RUN_ID, target=TARGET)
    train_rows = parts["train"]

    report = load_artefact("prepare.json", storage.read_text(run_key(RUN_ID, "prepare.json")))
    clip = transform(report, "clip_percentile", "visits_last_7d")
    fill = transform(report, "fill_median", "visits_last_7d")

    observed = train_rows["visits_last_7d"].dropna()
    assert clip.parameters["fit_rows"] == float(len(train_rows.index))
    assert clip.parameters["lower"] == float(observed.quantile(0.01))
    assert clip.parameters["upper"] == float(observed.quantile(0.99))
    # the median is fitted after the clip, on the same training rows
    clipped = observed.clip(lower=clip.parameters["lower"], upper=clip.parameters["upper"])
    assert fill.parameters["value"] == float(clipped.median())
    assert fill.parameters["fit_rows"] == float(len(train_rows.index))

    # ... and fitting the same transforms over the whole frame gives different numbers, so the
    # assertions above cannot pass by coincidence.
    _, whole_frame = fit_transforms_real(rows, config, _plan, run_id=RUN_ID, fit_index=None)
    recorded = {(item.kind, item.columns): item.parameters for item in report.transforms}
    everything = {(item.kind, item.columns): item.parameters for item in whole_frame.transforms}
    assert recorded.keys() == everything.keys()
    assert (
        recorded[("clip_percentile", ("visits_last_7d",))]
        != everything[("clip_percentile", ("visits_last_7d",))]
    )
    assert float(rows["visits_last_7d"].dropna().quantile(0.99)) != clip.parameters["upper"]


def test_the_seam_hands_fit_transforms_exactly_the_training_index(seam, resolved, storage, registry) -> None:
    config = fill_config(resolved)
    run_flow(resolved, storage, registry, config=config)
    rows, _plan = prepare_rows_real(seam_frame(), config, primary_key=PRIMARY_KEY, target=TARGET)
    parts, _ = split_dataset_real(rows, config, run_id=RUN_ID, target=TARGET)
    assert seam.fit_index is not None
    assert list(seam.fit_index) == list(parts["train"].index)
    assert not set(seam.fit_index) & set(parts["validation"].index)
    assert not set(seam.fit_index) & set(parts["test"].index)


def test_the_parts_handed_to_train_come_from_the_fitted_frame(seam, resolved, storage, registry) -> None:
    config = fill_config(resolved)
    run_flow(resolved, storage, registry, config=config)
    handed = seam.trained_parts
    assert handed is not None
    assert set(handed) == {"train", "validation", "test"}
    indexes = [set(part.index) for part in handed.values()]
    assert not indexes[0] & indexes[1] and not indexes[0] & indexes[2] and not indexes[1] & indexes[2]
    # every part carries the FITTED values, so the clip bound applies to the hold-out too
    report = load_artefact("prepare.json", storage.read_text(run_key(RUN_ID, "prepare.json")))
    upper = transform(report, "clip_percentile", "visits_last_7d").parameters["upper"]
    assert handed["test"]["visits_last_7d"].max() <= upper
    assert handed["train"]["visits_last_7d"].isna().sum() == 0
    assert sum(len(part.index) for part in handed.values()) == report.rows_out


def test_the_single_frame_prepare_convenience_is_never_called(seam, resolved, storage, registry) -> None:
    # `prepare.prepare` is monkeypatched to raise; reaching it would mean the pipeline fitted its
    # statistics over the validation and test rows as well (the leak DEC-046 removed).
    run_flow(resolved, storage, registry, config=fill_config(resolved))
    assert seam.calls == list(TRAIN_STAGES)
