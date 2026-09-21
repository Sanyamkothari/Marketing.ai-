"""`Pipeline.run_score`: stage order, the status machine, the replay rules and the manifest.

The three expensive stages are replaced by fakes that record how they were called and return canned
artefacts, so every ordering, cancellation and failure path runs in milliseconds and without
AutoGluon: `ingest` (file I/O), `predict` (loads a stored predictor) and `explain.reasons_for`
(SHAP). Four things are deliberately **not** faked, because they are the wiring this milestone has
to prove rather than describe:

* `explain.with_reason_columns`, so "a reason for every scored row" is enforced by the code that
  ships - it refuses a partly explained frame, which is what a score flow that asked for the train
  flow's sample would produce;
* `actions.apply_actions`, so the bands, the suppression rules and the control group in
  `scores.csv` are the real ones;
* `export.write_scores` and `export.summarise`, so the exported header, the reason cells and the
  KPI are produced by the stage that owns them;
* `prepare`, which is not faked but **forbidden**: every entry point of that module raises here, so
  a second replay, or a call to the train-only `prepare_rows`, fails this module loudly.

The real AutoGluon round trip lives in `tests/integration/test_score_flow.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.config import ResolvedConfig, RunMode, UseCaseConfig, resolve_config
from engine.contracts import (
    SCORE_ARTEFACTS,
    ColumnProfile,
    ColumnType,
    DatasetFingerprint,
    DatasetProfile,
    Direction,
    DriftReport,
    DriftStatus,
    FeatureDrift,
    FeatureSchema,
    FeatureSchemaColumn,
    Metric,
    ModelStatus,
    ModelVersion,
    PrepareReport,
    ProblemType,
    Reason,
    RowExplanation,
    RunManifest,
    RunRecord,
    RunState,
    RunStatus,
    ScoringSummary,
    Severity,
    StageKey,
    ValidationCheck,
    ValidationReport,
    load_artefact,
)
from engine.errors import RUN_BLOCKED_BY_VALIDATION, STAGE_FAILED
from engine.jobs import CancelToken, JobCancelledError
from engine.pipeline import (
    DRIFT_FILENAME,
    MANIFEST_FILENAME,
    REASONS_OFF_DETAIL,
    RUN_FILENAME,
    SCORE_STAGES,
    SCORING_SUMMARY_FILENAME,
    STATUS_FILENAME,
    Pipeline,
    StageContext,
    running_rows,
)
from engine.registry import LocalModelRegistry
from engine.stages import actions, explain, export, ingest, prepare, score, validate
from engine.stages.score import CHAMPION_NOT_FOUND, PredictResult, ScoreError, score_error
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now

USE_CASE: str = "targeted-advertisement"
RUN_ID: str = "r_20260921_0000f00d"
TRAIN_RUN_ID: str = "r_20260801_0000beef"
UPLOAD_ID: str = "u_000000000002"
UPLOAD_KEY: str = f"uploads/{UPLOAD_ID}/source.csv"
PRIMARY_KEY: str = "customer_id"
SCORE_FIELD: str = "propensity"
CONSENT_COLUMN: str = "marketing_opt_in"
FEATURES: tuple[str, ...] = ("visits_last_7d", "plan_tier")
MODEL_ID: str = "m_targeted_advertisement_3"
MODEL_NAME: str = "LightGBM"
PREDICTOR_KEY: str = run_key(TRAIN_RUN_ID, "model")
SCHEMA_KEY: str = run_key(TRAIN_RUN_ID, "schema.json")

ROWS: int = 40
SUPPRESSED_ROWS: int = 6  # marketing_opt_in is false on every seventh row

# Every artefact the score flow writes into the run directory, in the order it is written.
WRITTEN_ARTEFACTS: tuple[str, ...] = (
    "profile.json",
    "validation.json",
    "prepare.json",
    "drift.json",
    "row_explanations.parquet",
    "scores.csv",
    "scores.parquet",
    "scoring_summary.json",
)

# What a scoring run writes, minus the two documents `POST /runs` writes before the job starts.
FLOW_ARTEFACTS: frozenset[str] = SCORE_ARTEFACTS - {"run_config.json"}


# ---------------------------------------------------------------------------
# Frames, storage and the stage context
# ---------------------------------------------------------------------------
def make_frame(rows: int = ROWS) -> pd.DataFrame:
    """A scoring file: the key, two features and the opt-out column, and no target."""
    return pd.DataFrame(
        {
            PRIMARY_KEY: [f"C-{index:05d}" for index in range(rows)],
            "visits_last_7d": [index % 11 for index in range(rows)],
            "plan_tier": ["basic" if index % 3 else "premium" for index in range(rows)],
            CONSENT_COLUMN: [index % 7 != 0 for index in range(rows)],
        }
    )


def make_scores(frame: pd.DataFrame) -> pd.Series:
    """Calibrated scores spread over all three bands, on the frame's own index."""
    return pd.Series(
        [round((position + 1) / len(frame.index), 4) for position in range(len(frame.index))],
        index=frame.index,
        name=SCORE_FIELD,
        dtype="float64",
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
    store.write_bytes(UPLOAD_KEY, b"customer_id,visits_last_7d\nC-00000,3\n")
    store.write_model(SCHEMA_KEY, make_schema())
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
    model_version_id: str | None = None,
) -> StageContext:
    return StageContext(
        run_id=RUN_ID,
        mode=RunMode.SCORE,
        config=resolved.config if config is None else config,
        resolved=resolved,
        storage=storage,
        registry=registry,
        cancel=CancelToken() if cancel is None else cancel,
        primary_key=PRIMARY_KEY,
        target=None,
        upload_key=UPLOAD_KEY,
        model_version_id=model_version_id,
    )


def pipeline_for(storage: LocalStorage, registry: LocalModelRegistry) -> Pipeline:
    return Pipeline(storage, registry, _NoJobs())


class _NoJobs:
    """The pipeline only ever asks the runner to cancel, which these tests drive directly."""

    def submit(self, job_id: str, fn: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("run_score must not submit jobs")

    def status(self, job_id: str) -> Any:  # pragma: no cover - never called
        raise AssertionError("run_score must not read job status")

    def cancel(self, job_id: str) -> bool:  # pragma: no cover - never called
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # pragma: no cover - never called
        return None


# ---------------------------------------------------------------------------
# Canned artefacts
# ---------------------------------------------------------------------------
def make_fingerprint(rows: int = ROWS) -> DatasetFingerprint:
    return DatasetFingerprint(
        hash="sha256:v1:" + "b" * 8, algorithm="sha256:v1", n_rows=rows, columns=(PRIMARY_KEY, *FEATURES)
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
        file_name="customers_to_score.csv",
        file_size_bytes=len(frame.index) * 32,
        file_format="csv",
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame.index),
        column_count=len(frame.columns),
        columns=columns,
        primary_key_candidates=(PRIMARY_KEY,),
        time_column_candidates=(),
        target_candidate=None,
        preview_rows=(),
        missing_value_rate_pct=0.0,
        fingerprint=make_fingerprint(len(frame.index)),
        profiled_at=utc_now(),
    )


def make_schema() -> FeatureSchema:
    return FeatureSchema(
        use_case_id=USE_CASE,
        model_version_id=MODEL_ID,
        primary_key=PRIMARY_KEY,
        target=None,
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        columns=tuple(
            FeatureSchemaColumn(name=name, inferred_type=ColumnType.STRING, required=True)
            for name in FEATURES
        ),
        row_count_at_fit=2_800,
        created_at=utc_now(),
    )


def make_version(*, status: ModelStatus = ModelStatus.CHAMPION) -> ModelVersion:
    return ModelVersion(
        model_id=MODEL_ID,
        use_case_id=USE_CASE,
        version=3,
        run_id=TRAIN_RUN_ID,
        created_at=utc_now(),
        status=status,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=0.84,
        model_display_name=MODEL_NAME,
        schema_key=SCHEMA_KEY,
        run_config_key=run_key(TRAIN_RUN_ID, "run_config.json"),
        predictor_key=PREDICTOR_KEY,
        drift_baseline_key=run_key(TRAIN_RUN_ID, "drift_baseline.json"),
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )


def make_validation(*, passed: bool = True) -> ValidationReport:
    checks = (
        ()
        if passed
        else (
            ValidationCheck(
                code="SCHEMA_MISMATCH",
                severity=Severity.ERROR,
                message="This file is missing 1 column the model needs: visits_last_7d.",
            ),
        )
    )
    return ValidationReport(
        upload_id=UPLOAD_ID,
        run_id=RUN_ID,
        mode=RunMode.SCORE,
        checks=checks,
        error_count=0 if passed else 1,
        warning_count=0,
        passed=passed,
        validated_at=utc_now(),
    )


def make_prepare_report() -> PrepareReport:
    """The **training** run's report: the one predict reads and replays."""
    return PrepareReport(
        run_id=TRAIN_RUN_ID,
        rows_in=10_000,
        rows_out=9_800,
        columns_in=10,
        columns_out=8,
        feature_columns=FEATURES,
        dropped_columns=(),
        row_removals=(),
        transforms=(),
        detail="9,800 rows ready · 2 features",
        prepared_at=utc_now(),
    )


def make_drift() -> DriftReport:
    return DriftReport(
        run_id=RUN_ID,
        baseline_run_id=TRAIN_RUN_ID,
        model_version_id=MODEL_ID,
        threshold=0.2,
        features=(
            FeatureDrift(
                feature="visits_last_7d",
                psi=0.06,
                status=DriftStatus.STABLE,
                null_rate_baseline=0.0,
                null_rate_current=0.0,
            ),
        ),
        max_psi=0.06,
        drifted_features=(),
        status=DriftStatus.STABLE,
        summary="No feature has drifted (largest PSI 0.06).",
        computed_at=utc_now(),
    )


def make_explanation(key: str, score: float) -> RowExplanation:
    return RowExplanation(
        primary_key=key,
        score=score,
        reasons=(
            Reason(
                feature="visits_last_7d",
                value="12",
                contribution=0.2,
                direction=Direction.UP,
                text="visits_last_7d ↑ (12)",
            ),
        ),
    )


@dataclass
class FakeScorer:
    """Stands in for the fitted scorer `predict` loaded; only its identity matters here."""

    feature_columns: tuple[str, ...] = FEATURES
    loaded_from: str = PREDICTOR_KEY


# ---------------------------------------------------------------------------
# The stage stubs
# ---------------------------------------------------------------------------
@dataclass
class StageStubs:
    """Fakes for the three expensive stages, recording how the pipeline called them.

    `fail` injects an exception into the first function of a stage, after the call is recorded, so a
    failure test can see that the stage was entered and then failed. `actions` and `export` are
    recorded and then delegated to the real implementations.
    """

    fail: dict[StageKey, BaseException] = field(default_factory=dict)
    passes_validation: bool = True
    drift: DriftReport | None = field(default_factory=make_drift)
    prepare_report: PrepareReport = field(default_factory=make_prepare_report)
    version: ModelVersion = field(default_factory=make_version)
    resolve_error: BaseException | None = None
    truncate_first_read: bool = False
    calls: list[StageKey] = field(default_factory=list)
    frame: pd.DataFrame = field(default_factory=make_frame)
    scorer: FakeScorer = field(default_factory=FakeScorer)
    resolved_ids: list[str | None] = field(default_factory=list)
    predict_kwargs: list[dict[str, Any]] = field(default_factory=list)
    explain_kwargs: list[dict[str, Any]] = field(default_factory=list)
    explain_scorers: list[object] = field(default_factory=list)
    explain_frames: list[pd.DataFrame] = field(default_factory=list)
    summarise_kwargs: list[dict[str, Any]] = field(default_factory=list)
    row_caps: list[int] = field(default_factory=list)
    schemas: list[FeatureSchema] = field(default_factory=list)

    # -- installation ----------------------------------------------------
    def install(self, monkeypatch: pytest.MonkeyPatch) -> StageStubs:
        monkeypatch.setattr(ingest, "read_upload", self.read_upload)
        monkeypatch.setattr(ingest, "profile_dataset", self.profile_dataset)
        monkeypatch.setattr(validate, "validate_against_schema", self.validate_against_schema)
        monkeypatch.setattr(score, "resolve_model_version", self.resolve_model_version)
        monkeypatch.setattr(score, "predict", self.predict)
        monkeypatch.setattr(explain, "reasons_for", self.reasons_for)
        monkeypatch.setattr(actions, "apply_actions", self.apply_actions)
        monkeypatch.setattr(export, "write_scores", self.write_scores)
        monkeypatch.setattr(export, "summarise", self.summarise)
        for name in ("prepare", "prepare_rows", "fit_transforms", "split_dataset", "replay"):
            monkeypatch.setattr(prepare, name, _forbidden(name))
        return self

    def _enter(self, stage: StageKey) -> None:
        self.calls.append(stage)
        failure = self.fail.get(stage)
        if failure is not None:
            raise failure

    # -- ingest ----------------------------------------------------------
    def read_upload(self, storage: Any, key: str, **kwargs: Any) -> ingest.ReadResult:
        if not self.row_caps:
            self._enter(StageKey.INGEST)
        self.row_caps.append(int(kwargs["row_cap"]))
        truncated = self.truncate_first_read and len(self.row_caps) == 1
        frame = self.frame.iloc[:3] if truncated else self.frame
        return ingest.ReadResult(
            frame=frame,
            file_format="csv",
            delimiter=",",
            encoding="utf-8",
            row_count=len(self.frame.index),
            row_count_estimated=False,
            truncated=truncated,
            fingerprint=make_fingerprint(len(self.frame.index)),
        )

    def profile_dataset(self, frame: pd.DataFrame, config: Any, **kwargs: Any) -> DatasetProfile:
        return make_profile(frame)

    # -- validate_against_schema -----------------------------------------
    def resolve_model_version(self, config: Any, **kwargs: Any) -> ModelVersion:
        self.resolved_ids.append(kwargs.get("model_version_id"))
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.version

    def validate_against_schema(
        self, frame: pd.DataFrame, schema: FeatureSchema, **kwargs: Any
    ) -> ValidationReport:
        self._enter(StageKey.VALIDATE_AGAINST_SCHEMA)
        self.schemas.append(schema)
        return make_validation(passed=self.passes_validation)

    # -- predict ----------------------------------------------------------
    def predict(self, frame: pd.DataFrame, config: Any, **kwargs: Any) -> PredictResult:
        self._enter(StageKey.PREDICT)
        self.predict_kwargs.append(dict(kwargs))
        prepared = frame.copy()
        return PredictResult(
            model_version=self.version,
            scorer=self.scorer,  # type: ignore[arg-type]  # a stand-in for the loaded scorer
            scores=make_scores(prepared),
            prepared=prepared,
            prepare_report=self.prepare_report,
            drift=self.drift,
            detail=f"{len(prepared.index)} rows scored · {MODEL_NAME} (v3)",
        )

    # -- explain_rows -----------------------------------------------------
    def reasons_for(self, scorer: Any, frame: pd.DataFrame, config: Any, **kwargs: Any) -> explain.RowReasons:
        self._enter(StageKey.EXPLAIN_ROWS)
        self.explain_kwargs.append(dict(kwargs))
        self.explain_scorers.append(scorer)
        self.explain_frames.append(frame)
        # The cap is honoured, so a score flow that asked for a sample would explain a prefix - and
        # the real `with_reason_columns` below would then refuse the partly explained frame.
        limit = kwargs.get("max_rows")
        explained = frame if limit is None else frame.iloc[:limit]
        scores = make_scores(frame)
        return explain.RowReasons(
            explanations=tuple(
                make_explanation(str(key), float(scores.loc[index]))
                for index, key in explained[PRIMARY_KEY].items()
            ),
            method="TreeSHAP",
        )

    # -- actions and export: recorded, then the real thing -----------------
    def apply_actions(self, frame: pd.DataFrame, config: Any, **kwargs: Any) -> pd.DataFrame:
        self._enter(StageKey.ACTIONS)
        return apply_actions_real(frame, config, **kwargs)

    def write_scores(self, frame: pd.DataFrame, config: Any, **kwargs: Any) -> dict[str, str]:
        self._enter(StageKey.EXPORT)
        return write_scores_real(frame, config, **kwargs)

    def summarise(self, frame: pd.DataFrame, config: Any, **kwargs: Any) -> ScoringSummary:
        self.summarise_kwargs.append(dict(kwargs))
        return summarise_real(frame, config, **kwargs)


# Captured before any monkeypatching, so the stubs can delegate to the real implementations.
apply_actions_real = actions.apply_actions
write_scores_real = export.write_scores
summarise_real = export.summarise


def _forbidden(name: str) -> Any:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            f"run_score called prepare.{name}(): the score flow replays nothing itself. predict "
            f"loads the training run's prepare.json and replays it once; a second replay would "
            f"clip and fill twice, and prepare_rows would re-derive column drops from the scoring "
            f"batch and lose genuine features."
        )

    return refuse


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> StageStubs:
    return StageStubs().install(monkeypatch)


def run_flow(
    resolved: ResolvedConfig, storage: LocalStorage, registry: LocalModelRegistry, **kwargs: Any
) -> RunRecord:
    """Run the whole score flow against the stubs and return the run record."""
    return pipeline_for(storage, registry).run_score(make_context(resolved, storage, registry, **kwargs))


def read_manifest(storage: LocalStorage) -> RunManifest:
    return storage.read_model(run_key(RUN_ID, MANIFEST_FILENAME), RunManifest)


def read_status(storage: LocalStorage) -> RunStatus:
    return storage.read_model(run_key(RUN_ID, STATUS_FILENAME), RunStatus)


def read_run(storage: LocalStorage) -> RunRecord:
    return storage.read_model(run_key(RUN_ID, RUN_FILENAME), RunRecord)


def read_scores(storage: LocalStorage) -> pd.DataFrame:
    import io

    return pd.read_csv(io.BytesIO(storage.read_bytes(run_key(RUN_ID, export.SCORES_CSV))))


def stage(status: RunStatus, key: StageKey) -> Any:
    return next(row for row in status.stages if row.key is key)


# ---------------------------------------------------------------------------
# Order, status and the finished record
# ---------------------------------------------------------------------------
STUBBED_STAGES: tuple[StageKey, ...] = tuple(key for key in SCORE_STAGES if key is not StageKey.PREPARE)
"""The stages with a stage function to record; prepare has none, by design (see `_prepare`)."""


def test_the_stages_run_in_the_order_plan_6_2_names(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    assert stubs.calls == list(STUBBED_STAGES)


def test_the_score_flow_is_the_seven_stages_of_plan_6_2(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    status = read_status(storage)
    assert [row.key for row in status.stages] == list(SCORE_STAGES)
    assert all(row.state is RunState.DONE for row in status.stages)


def test_the_running_screen_shows_the_prototypes_four_score_rows(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    labels = tuple(dict.fromkeys(row.group_label for row in read_status(storage).stages))
    assert labels == running_rows(RunMode.SCORE)
    assert len(labels) == 4


def test_status_is_rewritten_at_every_transition(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    progress = [status.progress_pct for status in storage.statuses()]
    assert progress[0] == 0, "the first write is the stage starting, before anything is done"
    assert progress[-1] == 100
    assert progress == sorted(progress), "progress never goes backwards"
    assert list(dict.fromkeys(progress)) == [0, 14, 29, 43, 57, 71, 86, 100]


def test_every_stage_carries_a_detail_line_and_a_duration(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    for row in read_status(storage).stages:
        assert row.detail.strip(), f"{row.key.value} finished without a detail line"
        assert row.duration_seconds is not None
        assert row.started_at is not None and row.ended_at is not None


def test_the_finished_record_names_the_model_that_scored_the_file(resolved, storage, registry, stubs) -> None:
    record = run_flow(resolved, storage, registry)
    assert record.state is RunState.DONE
    assert record.error is None
    assert record.finished_at is not None
    assert record.model_version_id == MODEL_ID
    assert record.best_model == MODEL_NAME
    assert record.champion is True, "the file was scored by the champion"
    assert record.row_count == ROWS


def test_a_scoring_run_invents_no_model_quality_number(resolved, storage, registry, stubs) -> None:
    # A scoring file carries no target, so nothing about the model was measured on it.
    record = run_flow(resolved, storage, registry)
    assert record.headline_score is None
    assert record.beat_previous_champion is False


def test_a_run_scored_by_a_version_that_is_not_champion_says_so(resolved, storage, registry, stubs) -> None:
    stubs.version = make_version(status=ModelStatus.ARCHIVED)
    record = run_flow(resolved, storage, registry, model_version_id=MODEL_ID)
    assert record.champion is False
    assert record.model_version_id == MODEL_ID


# ---------------------------------------------------------------------------
# The artefacts of plan §7
# ---------------------------------------------------------------------------
def test_every_score_artefact_is_written_and_named_in_the_run_record(
    resolved, storage, registry, stubs
) -> None:
    record = run_flow(resolved, storage, registry)
    assert set(record.artefacts) >= FLOW_ARTEFACTS, sorted(FLOW_ARTEFACTS - set(record.artefacts))
    for name, key in record.artefacts.items():
        assert storage.exists(key), f"{name} is named in run.json but was never written"


def test_the_flow_writes_exactly_the_artefacts_of_a_scoring_run(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    for name in WRITTEN_ARTEFACTS:
        assert storage.exists(run_key(RUN_ID, name)), f"{name} was not written"
    prefix = run_key(RUN_ID, "")
    unexpected = {
        key.removeprefix(prefix)
        for key in storage.list_keys(prefix)
        if key.removeprefix(prefix) not in {*WRITTEN_ARTEFACTS, *FLOW_ARTEFACTS}
    }
    assert not unexpected, f"a scoring run wrote something plan §7 does not list: {sorted(unexpected)}"


def test_the_json_artefacts_are_written_in_stage_order(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    by_name = {run_key(RUN_ID, name): name for name in WRITTEN_ARTEFACTS}
    order = [by_name[key] for key in storage.keys_written() if key in by_name]
    assert order == [name for name in WRITTEN_ARTEFACTS if name in set(order)]


def test_every_json_artefact_validates_against_its_contract(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    for name in sorted(FLOW_ARTEFACTS):
        if not name.endswith(".json"):
            continue
        assert load_artefact(name, storage.read_text(run_key(RUN_ID, name))) is not None


# ---------------------------------------------------------------------------
# The replay rules: once, by predict, and never with the train-only entry point
# ---------------------------------------------------------------------------
def test_the_prepare_stage_replays_nothing_itself(resolved, storage, registry, stubs) -> None:
    # Every entry point of `engine.stages.prepare` raises in this module, so reaching the end of
    # the flow is the assertion: the pipeline called none of them.
    record = run_flow(resolved, storage, registry)
    assert record.state is RunState.DONE
    assert StageKey.PREPARE not in stubs.calls, "the prepare stage has no stage function to call"


def test_the_prepare_row_reports_which_model_prepared_the_rows(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    detail = stage(read_status(storage), StageKey.PREPARE).detail
    assert MODEL_NAME in detail
    assert "recorded" in detail, "the row says where the transforms came from"


def test_prepare_json_is_the_training_runs_report_not_a_new_one(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    report = storage.read_model(run_key(RUN_ID, "prepare.json"), PrepareReport)
    assert report.run_id == TRAIN_RUN_ID, "the truth about how these rows were prepared"
    assert report.rows_in == 10_000, "the training run's numbers, unchanged"
    assert report == stubs.prepare_report


def test_prepare_json_is_written_by_the_stage_that_replays_it(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    written = [key for key in storage.keys_written() if key.endswith("prepare.json")]
    assert len(written) == 1, "written once, from what predict returned"


# ---------------------------------------------------------------------------
# One resolution, one load
# ---------------------------------------------------------------------------
def test_the_model_version_is_resolved_once(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    assert stubs.resolved_ids == [None], "the champion, resolved once, at the stage that needs it"


def test_predict_is_asked_for_that_version_by_id(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    assert stubs.predict_kwargs[0]["model_version_id"] == MODEL_ID
    assert stubs.predict_kwargs[0]["run_id"] == RUN_ID


def test_a_named_version_is_the_one_resolved(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry, model_version_id=MODEL_ID)
    assert stubs.resolved_ids == [MODEL_ID]


def test_the_file_is_checked_against_that_versions_saved_schema(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    assert [schema.model_version_id for schema in stubs.schemas] == [MODEL_ID]


def test_a_use_case_without_a_champion_fails_at_the_validate_stage(
    resolved, storage, registry, stubs
) -> None:
    stubs.resolve_error = score_error(CHAMPION_NOT_FOUND, use_case="Targeted Advertisement")
    with pytest.raises(ScoreError):
        run_flow(resolved, storage, registry)
    failed = stage(read_status(storage), StageKey.VALIDATE_AGAINST_SCHEMA)
    assert failed.state is RunState.FAILED
    assert failed.error is not None and failed.error.code == CHAMPION_NOT_FOUND
    assert StageKey.PREDICT not in stubs.calls, "no model is loaded when none was chosen"


# ---------------------------------------------------------------------------
# A reason for every scored row
# ---------------------------------------------------------------------------
def test_explain_is_asked_for_every_row(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    assert stubs.explain_kwargs[0]["max_rows"] is None, "plan §6.3: a reason per scored row"
    assert stubs.explain_kwargs[0]["primary_key"] == PRIMARY_KEY


def test_explain_reuses_the_scorer_predict_loaded(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    assert stubs.explain_scorers == [stubs.scorer], "the model is loaded once per scoring run"


def test_a_use_case_with_reasons_switched_off_gets_none_and_invents_none(
    resolved, storage, registry, stubs
) -> None:
    """`evaluation.shap: false` is a user's choice, honoured here as it is in the train flow."""
    config = resolved.config.model_copy(
        update={"evaluation": resolved.config.evaluation.model_copy(update={"shap": False})}
    )
    record = run_flow(resolved, storage, registry, config=config)

    assert record.state is RunState.DONE
    assert stubs.explain_kwargs == [], "nothing was explained"
    assert explain.ROW_EXPLANATIONS_FILENAME not in record.artefacts
    assert stage(read_status(storage), StageKey.EXPLAIN_ROWS).detail == REASONS_OFF_DETAIL
    scores = read_scores(storage)
    assert len(scores.index) == ROWS, "the rows are still scored, banded and exported"
    assert scores["reason_1"].isna().all(), "empty cells, never an invented reason"


def test_row_explanations_parquet_holds_one_row_per_scored_row(resolved, storage, registry, stubs) -> None:
    record = run_flow(resolved, storage, registry)
    explanations = explain.read_row_explanations(
        record.artefacts[explain.ROW_EXPLANATIONS_FILENAME], storage=storage
    )
    assert len(explanations) == ROWS
    assert {item.primary_key for item in explanations} == set(make_frame()[PRIMARY_KEY])
    assert all(item.reasons for item in explanations)


def test_every_exported_row_has_a_band_an_action_and_a_reason(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    scores = read_scores(storage)
    assert len(scores.index) == ROWS
    assert scores["band"].notna().all()
    assert scores["action"].notna().all()
    assert scores["reason_1"].notna().all(), "every scored row carries at least one reason"


def test_the_exported_header_is_the_contract(resolved, storage, registry, stubs) -> None:
    from engine.contracts import scores_csv_columns

    run_flow(resolved, storage, registry)
    assert list(read_scores(storage).columns) == list(scores_csv_columns(resolved.config, PRIMARY_KEY))


# ---------------------------------------------------------------------------
# Drift: written when it was measured, absent when it was not
# ---------------------------------------------------------------------------
def test_drift_json_is_written_from_what_predict_returned(resolved, storage, registry, stubs) -> None:
    record = run_flow(resolved, storage, registry)
    assert storage.read_model(record.artefacts[DRIFT_FILENAME], DriftReport) == stubs.drift
    summary = storage.read_model(run_key(RUN_ID, SCORING_SUMMARY_FILENAME), ScoringSummary)
    assert summary.drift_status is DriftStatus.STABLE
    assert stubs.drift is not None and summary.drift_max_psi == stubs.drift.max_psi


def test_a_run_whose_drift_was_not_measured_writes_no_drift_file(resolved, storage, registry, stubs) -> None:
    stubs.drift = None
    record = run_flow(resolved, storage, registry)
    assert record.state is RunState.DONE, "drift is a signal, never a gate on scoring"
    assert DRIFT_FILENAME not in record.artefacts
    assert not storage.exists(run_key(RUN_ID, DRIFT_FILENAME)), "an empty verdict is not invented"
    summary = storage.read_model(run_key(RUN_ID, SCORING_SUMMARY_FILENAME), ScoringSummary)
    assert summary.drift_status is None and summary.drift_max_psi is None


def test_the_manifest_records_the_measured_psi(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    manifest = read_manifest(storage)
    assert stubs.drift is not None
    assert manifest.metrics["drift_max_psi"] == stubs.drift.max_psi
    assert manifest.metrics["rows_scored"] == float(ROWS)


def test_the_manifest_of_a_scoring_run_records_no_recipe(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    manifest = read_manifest(storage)
    assert manifest.recipe is None, "a scoring run fitted nothing"
    assert manifest.leaderboard_path is None
    assert manifest.dataset_fingerprint == make_fingerprint()
    assert manifest.cost_estimate.compute_seconds >= 0.0


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def test_scores_csv_has_one_row_per_uploaded_row(resolved, storage, registry, stubs) -> None:
    record = run_flow(resolved, storage, registry)
    summary = storage.read_model(record.artefacts[SCORING_SUMMARY_FILENAME], ScoringSummary)
    assert summary.rows_scored == ROWS == len(read_scores(storage).index)
    assert set(read_scores(storage)[PRIMARY_KEY]) == set(make_frame()[PRIMARY_KEY])


def test_the_kpi_is_totalled_from_the_rows_as_uploaded(resolved, storage, registry, stubs) -> None:
    run_flow(resolved, storage, registry)
    source = stubs.summarise_kwargs[0]["kpi_source"]
    assert source is not None
    assert list(source.columns) == list(make_frame().columns), "the uploaded rows, before the replay"
    assert CONSENT_COLUMN in source.columns


def test_the_summary_counts_the_suppressed_and_control_rows(resolved, storage, registry, stubs) -> None:
    record = run_flow(resolved, storage, registry)
    summary = storage.read_model(record.artefacts[SCORING_SUMMARY_FILENAME], ScoringSummary)
    assert sum(item.rows for item in summary.bands) == ROWS
    assert sum(item.rows for item in summary.actions) == ROWS
    assert sum(item.rows for item in summary.suppressed) == SUPPRESSED_ROWS
    assert summary.model_version_id == MODEL_ID
    assert summary.model_display_name == MODEL_NAME


# ---------------------------------------------------------------------------
# Ingest reads the whole scoring file
# ---------------------------------------------------------------------------
def test_a_truncated_scoring_file_is_read_again_in_full(resolved, storage, registry, stubs) -> None:
    stubs.truncate_first_read = True
    record = run_flow(resolved, storage, registry)
    assert stubs.row_caps[1] == ROWS, "the second read asks for the row count the first one measured"
    assert record.row_count == ROWS
    assert len(read_scores(storage).index) == ROWS, "no customer is dropped by a profiling cap"


# ---------------------------------------------------------------------------
# Validation blocks the run
# ---------------------------------------------------------------------------
def test_a_blocking_validation_stops_the_run_before_the_model_is_loaded(
    resolved, storage, registry, stubs
) -> None:
    stubs.passes_validation = False
    with pytest.raises(Exception) as caught:
        run_flow(resolved, storage, registry)
    assert getattr(caught.value, "code", None) == RUN_BLOCKED_BY_VALIDATION
    assert StageKey.PREDICT not in stubs.calls
    report = storage.read_model(run_key(RUN_ID, "validation.json"), ValidationReport)
    assert report.passed is False, "the report is written before the run is refused"
    assert read_run(storage).state is RunState.FAILED


# ---------------------------------------------------------------------------
# Cancellation at every boundary
# ---------------------------------------------------------------------------
CHECKPOINTS: int = 2 * len(SCORE_STAGES)


@pytest.mark.parametrize("checkpoint", range(1, CHECKPOINTS + 1))
def test_cancelling_at_any_boundary_stops_the_run_and_writes_both_documents(
    resolved, storage, registry, stubs, checkpoint: int
) -> None:
    cancel = CountingCancel(at=checkpoint)
    with pytest.raises(JobCancelledError):
        run_flow(resolved, storage, registry, cancel=cancel)

    status = read_status(storage)
    record = read_run(storage)
    assert status.state is RunState.CANCELLED
    assert record.state is RunState.CANCELLED
    assert record.finished_at is not None
    assert record.error is None, "a cancellation is not a failure"
    stopped = [row for row in status.stages if row.state is RunState.CANCELLED]
    assert len(stopped) == 1, "exactly one stage records the stop"
    assert not [row for row in status.stages if row.state is RunState.RUNNING]
    assert status.current_stage is None


@pytest.mark.parametrize("checkpoint", range(1, CHECKPOINTS + 1))
def test_a_cancelled_run_still_gets_its_manifest(resolved, storage, registry, stubs, checkpoint: int) -> None:
    with pytest.raises(JobCancelledError):
        run_flow(resolved, storage, registry, cancel=CountingCancel(at=checkpoint))
    manifest = read_manifest(storage)
    assert manifest.run_id == RUN_ID
    assert manifest.duration_s >= 0.0


def test_a_run_cancelled_before_its_first_stage_stops_at_ingest(resolved, storage, registry, stubs) -> None:
    with pytest.raises(JobCancelledError):
        run_flow(resolved, storage, registry, cancel=CountingCancel(at=1))
    status = read_status(storage)
    assert stage(status, StageKey.INGEST).state is RunState.CANCELLED
    assert stage(status, StageKey.INGEST).started_at is None
    assert [row.state for row in status.stages[1:]] == [RunState.SKIPPED] * (len(SCORE_STAGES) - 1)
    assert stubs.calls == [], "nothing ran"


# ---------------------------------------------------------------------------
# Failure at every stage
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", SCORE_STAGES)
def test_a_failing_stage_stops_the_run_and_records_why(
    resolved, storage, registry, stubs, monkeypatch, key: StageKey
) -> None:
    _fail_at(stubs, monkeypatch, key)
    with pytest.raises(RuntimeError):
        run_flow(resolved, storage, registry)

    status = read_status(storage)
    record = read_run(storage)
    assert status.state is RunState.FAILED
    assert record.state is RunState.FAILED
    assert record.finished_at is not None
    failed = stage(status, key)
    assert failed.state is RunState.FAILED
    assert failed.error is not None
    assert failed.error.code == STAGE_FAILED, "an uncoded exception is a bug, reported as one"
    assert failed.error.stage is key
    assert record.error is not None and record.error.stage is key
    after = SCORE_STAGES[SCORE_STAGES.index(key) + 1 :]
    assert [stage(status, later).state for later in after] == [RunState.SKIPPED] * len(after)


@pytest.mark.parametrize("key", SCORE_STAGES)
def test_a_failed_run_still_gets_its_manifest(
    resolved, storage, registry, stubs, monkeypatch, key: StageKey
) -> None:
    _fail_at(stubs, monkeypatch, key)
    with pytest.raises(RuntimeError):
        run_flow(resolved, storage, registry)
    manifest = read_manifest(storage)
    assert manifest.run_id == RUN_ID
    assert manifest.recipe is None
    assert manifest.cost_estimate.compute_seconds >= 0.0
    assert manifest.duration_s >= 0.0


def test_a_coded_stage_failure_keeps_its_own_code(resolved, storage, registry, stubs) -> None:
    stubs.fail[StageKey.PREDICT] = ScoreError(
        "SCORER_UNREADABLE", "The saved model for version m_3 could not be opened."
    )
    with pytest.raises(ScoreError):
        run_flow(resolved, storage, registry)
    failed = stage(read_status(storage), StageKey.PREDICT)
    assert failed.error is not None
    assert failed.error.code == "SCORER_UNREADABLE"
    assert "could not be opened" in failed.error.message


def test_a_failed_run_names_the_artefacts_it_did_produce(resolved, storage, registry, stubs) -> None:
    stubs.fail[StageKey.EXPLAIN_ROWS] = RuntimeError("shap exploded")
    with pytest.raises(RuntimeError):
        run_flow(resolved, storage, registry)
    record = read_run(storage)
    assert {"profile.json", "validation.json", "prepare.json", DRIFT_FILENAME} <= set(record.artefacts)
    assert SCORING_SUMMARY_FILENAME not in record.artefacts, "nothing is claimed that was not written"


def _fail_at(stubs: StageStubs, monkeypatch: pytest.MonkeyPatch, key: StageKey) -> None:
    """Inject a plain exception into `key`.

    The prepare stage of the score flow has no stage function to fail - it replays nothing - so its
    one call, the detail line it reports, is what fails instead.
    """
    if key is StageKey.PREPARE:
        monkeypatch.setattr("engine.pipeline._replay_detail", _raise_runtime)
    else:
        stubs.fail[key] = RuntimeError("the stage exploded")


def _raise_runtime(*args: Any, **kwargs: Any) -> str:
    raise RuntimeError("the stage exploded")
