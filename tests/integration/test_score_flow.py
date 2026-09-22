"""The real score flow: a model trained by the engine, then a fresh file scored end to end.

Two module-scoped fixtures do all the work. The first runs `Pipeline.run_train` on the synthetic
generator with plan §10's settings (`time_limit_minutes: 1`, `strategy: fast`) and approves the
resulting version into the champion; the second runs `Pipeline.run_score` over a *scoring* file -
the same columns without the target - through the installed AutoGluon, the stored predictor, the
recorded `prepare.json` and the stored drift baseline. Every test below reads that one run.

What this module is for is the wiring no unit test can prove: that the artefacts of plan §7 are
really written and really validate, that **every scored row** comes back with a band, an action and
a reason, that `scores.csv` has exactly one row per uploaded row, and that a file whose column was
renamed is refused **by column name** before anything is scored.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from engine.config import ResolvedConfig, RunMode, resolve_config
from engine.contracts import (
    SCORE_ARTEFACTS,
    Direction,
    DriftReport,
    ModelStatus,
    ModelVersion,
    PrepareReport,
    ReasonMethod,
    RunManifest,
    RunRecord,
    RunState,
    RunStatus,
    ScoringSummary,
    StageKey,
    ValidationReport,
    load_artefact,
    scores_csv_columns,
)
from engine.jobs import CancelToken
from engine.pipeline import (
    MANIFEST_FILENAME,
    SCORE_STAGES,
    SCORING_SUMMARY_FILENAME,
    STATUS_FILENAME,
    Pipeline,
    StageContext,
    running_rows,
)
from engine.registry import LocalModelRegistry
from engine.stages import explain
from engine.stages.export import SCORES_CSV, SCORES_PARQUET
from engine.storage import LocalStorage, run_key, upload_key
from engine.utils.ids import new_run_id
from tests.fixtures.make_data import GenerationSpec, generate

pytestmark = [pytest.mark.slow, pytest.mark.integration]

USE_CASE: str = "targeted-advertisement"
PRIMARY_KEY: str = "customer_id"
TARGET: str = "converted_30d"
TRAIN_ROWS: int = 4_000
SCORE_ROWS: int = 400
TRAIN_SEED: int = 20260921
SCORE_SEED: int = 20270405

# plan §10's settings, plus `ensemble: false` so the run stays inside a slow test's budget.
# `tuning_trials: 5` belongs to that same budget, not to the flow under test: since DEC-057 the
# trials are real AutoGluon HPO fits, and the shipped default of 50 across four families is 200
# fits, which one minute cannot give a fair share of. Five is the schema's floor for the setting and still exercises the HPO path.
OVERRIDES: dict[str, object] = {
    "model_search.time_limit_minutes": 1,
    "model_search.strategy": "fast",
    "model_search.ensemble": False,
    "model_search.folds": 3,
    "model_search.tuning_trials": 5,
}


@dataclass(frozen=True)
class Flow:
    """One completed run and everything needed to read it back."""

    run_id: str
    record: RunRecord
    storage: LocalStorage
    registry: LocalModelRegistry
    resolved: ResolvedConfig
    uploaded: pd.DataFrame

    def key(self, name: str) -> str:
        return run_key(self.run_id, name)

    def artefact(self, name: str) -> object:
        """One artefact of this run, validated through the contract registered for its name."""
        return load_artefact(name, self.storage.read_text(self.key(name)))

    def scores(self) -> pd.DataFrame:
        return pd.read_csv(io.BytesIO(self.storage.read_bytes(self.key(SCORES_CSV))))

    def summary(self) -> ScoringSummary:
        return self.storage.read_model(self.key(SCORING_SUMMARY_FILENAME), ScoringSummary)

    def status(self) -> RunStatus:
        return self.storage.read_model(self.key(STATUS_FILENAME), RunStatus)


class _NoJobs:
    """The flows run on this thread; the runner is only there to satisfy the constructor."""

    def submit(self, job_id: str, fn: object) -> object:  # pragma: no cover - never called
        raise AssertionError("the pipeline must not submit jobs")

    def status(self, job_id: str) -> object:  # pragma: no cover - never called
        raise AssertionError("the pipeline must not read job status")

    def cancel(self, job_id: str) -> bool:  # pragma: no cover - never called
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # pragma: no cover - never called
        return None


def stage_context(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    resolved: ResolvedConfig,
    *,
    run_id: str,
    mode: RunMode,
    source: str,
    target: str | None,
    model_version_id: str | None = None,
) -> StageContext:
    return StageContext(
        run_id=run_id,
        mode=mode,
        config=resolved.config,
        resolved=resolved,
        storage=storage,
        registry=registry,
        cancel=CancelToken(),
        primary_key=PRIMARY_KEY,
        target=target,
        upload_key=source,
        model_version_id=model_version_id,
    )


def upload(storage: LocalStorage, frame: pd.DataFrame, *, name: str) -> str:
    """Write a generated frame where an upload would be, and return its storage key."""
    key = upload_key(f"u_{name}", "source.csv")
    storage.write_text(key, frame.to_csv(index=False, lineterminator="\n"))
    return key


def train_champion(
    storage: LocalStorage, registry: LocalModelRegistry, resolved: ResolvedConfig
) -> ModelVersion:
    """Run the whole train flow, then approve the version it produced into the champion."""
    frame = generate(GenerationSpec(USE_CASE, rows=TRAIN_ROWS, variant="clean", seed=TRAIN_SEED))
    run_id = new_run_id()
    storage.write_model(run_key(run_id, "run_config.json"), resolved)
    record = Pipeline(storage, registry, _NoJobs()).run_train(
        stage_context(
            storage,
            registry,
            resolved,
            run_id=run_id,
            mode=RunMode.TRAIN,
            source=upload(storage, frame, name="train"),
            target=TARGET,
        )
    )
    assert record.state is RunState.DONE, record.error
    model_id = record.model_version_id or ""
    version = registry.get(model_id)
    if version.status is ModelStatus.PENDING_APPROVAL:
        # `approval_required` is on by default, so the first model of a use case waits for a human.
        version = registry.approve(model_id, by="tests")
    assert version.status is ModelStatus.CHAMPION
    return version


def score_file(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    resolved: ResolvedConfig,
    frame: pd.DataFrame,
    *,
    name: str,
) -> Flow:
    """Run the whole score flow over `frame`, as `POST /runs` would."""
    run_id = new_run_id()
    storage.write_model(run_key(run_id, "run_config.json"), resolved)
    record = Pipeline(storage, registry, _NoJobs()).run_score(
        stage_context(
            storage,
            registry,
            resolved,
            run_id=run_id,
            mode=RunMode.SCORE,
            source=upload(storage, frame, name=name),
            target=None,
        )
    )
    return Flow(run_id, record, storage, registry, resolved, frame)


@dataclass(frozen=True)
class Trained:
    storage: LocalStorage
    registry: LocalModelRegistry
    resolved: ResolvedConfig
    champion: ModelVersion


@pytest.fixture(scope="module")
def trained(tmp_path_factory: pytest.TempPathFactory, config_root: Path) -> Trained:
    """One real training run, approved into the champion; shared by every test in this module."""
    root = tmp_path_factory.mktemp("score-flow")
    storage = LocalStorage(root / "data")
    registry = LocalModelRegistry(root / "registry.db")
    resolved = resolve_config(USE_CASE, OVERRIDES, root=config_root)
    return Trained(storage, registry, resolved, train_champion(storage, registry, resolved))


@pytest.fixture(scope="module")
def scored(trained: Trained) -> Flow:
    """The champion scoring a fresh file it has never seen."""
    frame = generate(GenerationSpec(USE_CASE, rows=SCORE_ROWS, variant="scoring", seed=SCORE_SEED))
    assert TARGET not in frame.columns, "a scoring file carries no answers"
    return score_file(trained.storage, trained.registry, trained.resolved, frame, name="score")


# ---------------------------------------------------------------------------
# The run, and every artefact of plan §7
# ---------------------------------------------------------------------------
def test_the_champion_is_a_really_trained_model(trained: Trained) -> None:
    """The fixture above is not a stub: AutoGluon really fitted this, and it really reloads."""
    from engine.stages.scorer import load_scorer

    directory = trained.storage.local_path(trained.champion.predictor_key)
    assert (directory / "predictor.pkl").is_file()
    scorer = load_scorer(trained.champion.predictor_key, trained.storage)
    assert scorer.feature_columns, "a model that saw no features could not score anything"
    assert 0.0 < scorer.threshold < 1.0
    leaderboard = trained.storage.read_text(run_key(trained.champion.run_id, "leaderboard.json"))
    assert '"models_trained"' in leaderboard


def test_the_scoring_run_finished(scored: Flow) -> None:
    assert scored.record.state is RunState.DONE, scored.record.error
    assert scored.record.error is None
    assert scored.record.finished_at is not None
    assert scored.record.mode is RunMode.SCORE


def test_every_stage_of_plan_6_2_ran_in_order(scored: Flow) -> None:
    status = scored.status()
    assert [row.key for row in status.stages] == list(SCORE_STAGES)
    assert [row.state for row in status.stages] == [RunState.DONE] * len(SCORE_STAGES)
    assert status.progress_pct == 100
    assert tuple(dict.fromkeys(row.group_label for row in status.stages)) == running_rows(RunMode.SCORE)
    for row in status.stages:
        assert row.detail.strip(), f"{row.key.value} finished without a detail line"


@pytest.mark.parametrize(
    "name", sorted(SCORE_ARTEFACTS - {"row_explanations.parquet", SCORES_CSV, SCORES_PARQUET})
)
def test_every_json_artefact_exists_and_validates_against_its_contract(scored: Flow, name: str) -> None:
    assert scored.storage.exists(scored.key(name)), f"{name} was not written"
    assert scored.artefact(name) is not None
    assert scored.record.artefacts[name] == scored.key(name)


def test_the_two_score_files_are_written_and_named(scored: Flow) -> None:
    for name in (SCORES_CSV, SCORES_PARQUET):
        assert scored.storage.exists(scored.key(name))
        assert scored.record.artefacts[name] == scored.key(name)
    parquet = pd.read_parquet(io.BytesIO(scored.storage.read_bytes(scored.key(SCORES_PARQUET))))
    assert list(parquet.columns) == list(scores_csv_columns(scored.resolved.config, PRIMARY_KEY))
    assert len(parquet.index) == len(scored.scores().index), "both files hold the same table"


def test_the_manifest_of_a_scoring_run_records_no_recipe(scored: Flow) -> None:
    manifest = scored.storage.read_model(scored.key(MANIFEST_FILENAME), RunManifest)
    assert manifest.recipe is None, "a scoring run fitted nothing"
    assert manifest.leaderboard_path is None
    assert manifest.dataset_fingerprint.n_rows == SCORE_ROWS
    assert manifest.metrics["rows_scored"] == float(SCORE_ROWS)
    assert manifest.duration_s > 0.0


# ---------------------------------------------------------------------------
# One row in, one row out - with a band, an action and a reason
# ---------------------------------------------------------------------------
def test_scores_csv_has_one_row_per_uploaded_row(scored: Flow) -> None:
    scores = scored.scores()
    assert len(scores.index) == SCORE_ROWS == len(scored.uploaded.index)
    assert set(scores[PRIMARY_KEY].astype(str)) == set(scored.uploaded[PRIMARY_KEY].astype(str))
    assert scores[PRIMARY_KEY].is_unique


def test_every_scored_row_has_a_band_an_action_and_a_reason(scored: Flow) -> None:
    scores = scored.scores()
    bands = {band.name for band in scored.resolved.config.actions.bands}
    assert scores["band"].notna().all()
    assert set(scores["band"]) <= bands
    assert scores["action"].notna().all()
    assert scores["action"].astype(str).str.strip().ne("").all()
    assert scores["reason_1"].notna().all(), "plan §6.3: a reason for every scored row"
    assert scores["reason_1"].astype(str).str.strip().ne("").all()


def test_the_scores_are_calibrated_probabilities_in_the_band_range(scored: Flow) -> None:
    scores = scored.scores()[scored.resolved.config.actions.score_field]
    assert scores.notna().all()
    assert scores.between(0.0, 1.0).all()
    assert scores.nunique() > 1, "a constant score would mean the model never saw the features"


def test_row_explanations_parquet_covers_every_scored_row(scored: Flow) -> None:
    explanations = explain.read_row_explanations(
        scored.record.artefacts[explain.ROW_EXPLANATIONS_FILENAME], storage=scored.storage
    )
    limit = scored.resolved.config.evaluation.reasons_per_row
    assert len(explanations) == SCORE_ROWS, "the score flow explains every row, never a sample"
    assert len({item.primary_key for item in explanations}) == SCORE_ROWS
    assert {item.primary_key for item in explanations} == set(scored.uploaded[PRIMARY_KEY].astype(str))
    for item in explanations:
        assert item.reasons, "a row with no reason would leave a blank cell in scores.csv"
        assert len(item.reasons) <= limit
        for reason in item.reasons:
            assert reason.text.strip()
            # "none" is a general reason (DEC-056): the row moved neither way, and says so.
            assert reason.direction.value in {"up", "down", "none"}
            if reason.direction is Direction.NONE:
                assert item.method is ReasonMethod.GENERAL
                assert reason.contribution == 0.0
                assert reason.text.endswith(explain.GENERAL_SUFFIX)


def test_the_summary_counts_the_rows_that_needed_a_fallback_reason(scored: Flow) -> None:
    """DEC-056: whatever the model saturated on, the Output page is told how much of it there was."""
    explanations = explain.read_row_explanations(
        scored.record.artefacts[explain.ROW_EXPLANATIONS_FILENAME], storage=scored.storage
    )
    methods = {item.method for item in explanations}
    primary = max(methods, key=lambda name: sum(1 for item in explanations if item.method is name))
    counted = sum(1 for item in explanations if item.method is not primary)
    assert scored.summary().rows_with_fallback_reasons == counted
    assert 0 <= counted <= SCORE_ROWS


def test_the_reasons_in_the_file_belong_to_the_row_they_are_on(scored: Flow) -> None:
    """The adapter joins by primary key; this is that join, checked against the parquet."""
    scores = scored.scores().set_index(scored.scores()[PRIMARY_KEY].astype(str))
    explanations = {
        item.primary_key: item
        for item in explain.read_row_explanations(
            scored.record.artefacts[explain.ROW_EXPLANATIONS_FILENAME], storage=scored.storage
        )
    }
    for key, row in scores.iterrows():
        assert str(row["reason_1"]) == explanations[str(key)].reasons[0].text


# ---------------------------------------------------------------------------
# What the Output page reads
# ---------------------------------------------------------------------------
def test_the_summary_counts_every_scored_row_exactly_once(scored: Flow) -> None:
    summary = scored.summary()
    scores = scored.scores()
    assert summary.rows_scored == SCORE_ROWS
    assert sum(item.rows for item in summary.bands) == SCORE_ROWS
    assert sum(item.rows for item in summary.actions) == SCORE_ROWS
    assert summary.control_group_rows == int(scores["control_group"].astype(bool).sum())
    assert sum(item.rows for item in summary.suppressed) == int(scores["suppressed_reason"].notna().sum())
    assert summary.model_version_id == scored.record.model_version_id
    assert summary.files[SCORES_CSV] == scored.key(SCORES_CSV)
    assert summary.sample_rows, "the Output page renders its table without reading the CSV"


def test_the_kpi_is_a_real_count_of_the_scored_rows(scored: Flow) -> None:
    summary = scored.summary()
    scores = scored.scores()
    assert summary.kpi.formula == scored.resolved.config.output.kpi.formula
    expected = int(scores["band"].isin(["High", "Medium"]).sum())
    assert summary.kpi.value == float(expected)
    assert summary.kpi.display.strip()


def test_drift_was_measured_against_the_training_baseline(scored: Flow) -> None:
    drift = scored.storage.read_model(scored.key("drift.json"), DriftReport)
    summary = scored.summary()
    assert drift.model_version_id == scored.record.model_version_id
    assert drift.features, "a baseline with features produces a per-feature comparison"
    assert drift.max_psi >= 0.0
    assert summary.drift_status is drift.status
    assert summary.drift_max_psi == drift.max_psi


# ---------------------------------------------------------------------------
# The replay: the training run's transforms, applied once
# ---------------------------------------------------------------------------
def test_prepare_json_is_the_training_runs_record(scored: Flow, trained: Trained) -> None:
    report = scored.storage.read_model(scored.key("prepare.json"), PrepareReport)
    training = scored.storage.read_model(run_key(trained.champion.run_id, "prepare.json"), PrepareReport)
    assert report == training, "the scoring run replays that record; it does not write a new one"
    assert report.run_id == trained.champion.run_id
    assert report.rows_in == TRAIN_ROWS


def test_the_run_record_names_the_champion_that_scored_the_file(scored: Flow, trained: Trained) -> None:
    assert scored.record.model_version_id == trained.champion.model_id
    assert scored.record.best_model == trained.champion.model_display_name
    assert scored.record.champion is True
    assert scored.record.row_count == SCORE_ROWS
    assert scored.record.headline_score is None, "nothing about model quality was measured here"


# ---------------------------------------------------------------------------
# A renamed column is refused, by name, before anything is scored
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def renamed(trained: Trained) -> tuple[Flow | None, str, Exception]:
    """The score flow over a file whose first feature column was renamed."""
    frame = generate(GenerationSpec(USE_CASE, rows=SCORE_ROWS, variant="renamed_column", seed=SCORE_SEED))
    clean = generate(GenerationSpec(USE_CASE, rows=SCORE_ROWS, variant="scoring", seed=SCORE_SEED))
    missing = next(name for name in clean.columns if name not in frame.columns)
    run_id = new_run_id()
    storage, registry, resolved = trained.storage, trained.registry, trained.resolved
    storage.write_model(run_key(run_id, "run_config.json"), resolved)
    context = stage_context(
        storage,
        registry,
        resolved,
        run_id=run_id,
        mode=RunMode.SCORE,
        source=upload(storage, frame, name="renamed"),
        target=None,
    )
    with pytest.raises(Exception) as caught:  # the code is asserted in the tests below
        Pipeline(storage, registry, _NoJobs()).run_score(context)
    flow = Flow(
        run_id, storage.read_model(run_key(run_id, "run.json"), RunRecord), storage, registry, resolved, frame
    )
    return flow, missing, caught.value


def test_a_renamed_column_is_reported_by_name(renamed: tuple[Flow, str, Exception]) -> None:
    flow, missing, _ = renamed
    report = flow.storage.read_model(flow.key("validation.json"), ValidationReport)
    mismatches = [check for check in report.checks if check.code == "SCHEMA_MISMATCH"]
    assert mismatches, "the scoring file was checked against the model's saved schema"
    assert report.passed is False
    assert any(
        missing in check.message for check in mismatches
    ), f"the refusal must name {missing!r}: " + " | ".join(check.message for check in mismatches)


def test_a_renamed_column_stops_the_run_before_anything_is_scored(
    renamed: tuple[Flow, str, Exception],
) -> None:
    flow, _, error = renamed
    assert getattr(error, "code", None) == "RUN_BLOCKED_BY_VALIDATION"
    assert flow.record.state is RunState.FAILED
    assert flow.record.error is not None
    assert flow.record.error.stage is StageKey.VALIDATE_AGAINST_SCHEMA
    status = flow.status()
    assert status.state is RunState.FAILED
    failed = next(row for row in status.stages if row.key is StageKey.VALIDATE_AGAINST_SCHEMA)
    assert failed.state is RunState.FAILED
    for name in (SCORES_CSV, SCORES_PARQUET, SCORING_SUMMARY_FILENAME, "drift.json"):
        assert not flow.storage.exists(flow.key(name)), f"{name} was written for a refused file"


def test_a_refused_run_still_gets_its_manifest(renamed: tuple[Flow, str, Exception]) -> None:
    manifest = renamed[0].storage.read_model(renamed[0].key(MANIFEST_FILENAME), RunManifest)
    assert manifest.run_id == renamed[0].run_id
    assert manifest.recipe is None
    assert manifest.duration_s >= 0.0
