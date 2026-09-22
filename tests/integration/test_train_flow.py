"""The real train flow: plan §10's run, on the synthetic generator, with the installed AutoGluon.

One module-scoped fixture runs `Pipeline.run_train` end to end on 10,000 synthetic rows with plan
§10's settings verbatim (`time_limit_minutes: 1`, `strategy: fast`); every test below reads that
one run's artefacts. A second fixture approves the resulting model and runs the flow again on a
differently-seeded file, which is what makes the champion rule observable: the incumbent is
reloaded and re-scored on run 2's own test split, and the promotion follows *that* pair of numbers.

Plan §10's golden checks live here too, in the plan's own words: if the model does not beat the
logistic baseline and 0.7 on this data, the pipeline is broken, not the data.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from engine.config import Metric, Recipe, ResolvedConfig, RunMode, recipe_from_config, resolve_config
from engine.contracts import (
    MODEL_DIRECTORY,
    TRAIN_ARTEFACTS,
    BestModel,
    DatasetProfile,
    DecileLift,
    EvaluationReport,
    Leaderboard,
    ModelStatus,
    PrepareReport,
    RunManifest,
    RunRecord,
    RunState,
    RunStatus,
    SplitPart,
    SplitReport,
    StageKey,
    load_artefact,
)
from engine.jobs import CancelToken
from engine.pipeline import (
    MANIFEST_FILENAME,
    RUN_FILENAME,
    STATUS_FILENAME,
    TRAIN_STAGES,
    Pipeline,
    StageContext,
    running_rows,
)
from engine.registry import LocalModelRegistry, should_promote
from engine.stages import explain
from engine.stages.scorer import load_scorer
from engine.storage import LocalStorage, run_key, upload_key
from engine.utils.ids import new_run_id, seed_from
from tests.fixtures.make_data import GenerationSpec, generate

pytestmark = [pytest.mark.slow, pytest.mark.integration]

USE_CASE: str = "targeted-advertisement"
PRIMARY_KEY: str = "customer_id"
TARGET: str = "converted_30d"
ROWS: int = 10_000
# plan §10, verbatim: "full train flow on the synthetic Targeted Advertisement data with
# `time_limit_minutes: 1` and `strategy: fast`".
# `tuning_trials: 5` keeps the one-minute budget honest: since DEC-073 each trial is a real
# AutoGluon HPO fit, so the shipped default of 50 would divide this minute 50 ways per family.
OVERRIDES: dict[str, object] = {
    "model_search.time_limit_minutes": 1,
    "model_search.strategy": "fast",
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

    def key(self, name: str) -> str:
        return run_key(self.run_id, name)

    def artefact(self, name: str) -> object:
        """One artefact of this run, validated through the contract registered for its name."""
        return load_artefact(name, self.storage.read_text(self.key(name)))


def run_once(storage: LocalStorage, registry: LocalModelRegistry, config_root: Path, *, seed: int) -> Flow:
    """Generate a synthetic upload, then run the whole train flow over it."""
    upload = upload_key(f"u_{seed}", "source.csv")
    frame = generate(GenerationSpec(USE_CASE, rows=ROWS, variant="clean", seed=seed))
    storage.write_text(upload, frame.to_csv(index=False, lineterminator="\n"))
    resolved = resolve_config(USE_CASE, OVERRIDES, root=config_root)
    run_id = new_run_id()
    # `POST /runs` writes run_config.json before it submits the job; the rest of the run directory
    # is the pipeline's to write.
    storage.write_model(run_key(run_id, "run_config.json"), resolved)
    ctx = StageContext(
        run_id=run_id,
        mode=RunMode.TRAIN,
        config=resolved.config,
        resolved=resolved,
        storage=storage,
        registry=registry,
        cancel=CancelToken(),
        primary_key=PRIMARY_KEY,
        target=TARGET,
        upload_key=upload,
        model_version_id=None,
    )
    record = Pipeline(storage, registry, _NoJobs()).run_train(ctx)
    return Flow(run_id=run_id, record=record, storage=storage, registry=registry, resolved=resolved)


class _NoJobs:
    """`run_train` runs on this thread; the runner is only there to satisfy the constructor."""

    def submit(self, job_id: str, fn: object) -> object:  # pragma: no cover - never called
        raise AssertionError("run_train must not submit jobs")

    def status(self, job_id: str) -> object:  # pragma: no cover - never called
        raise AssertionError("run_train must not read job status")

    def cancel(self, job_id: str) -> bool:  # pragma: no cover - never called
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # pragma: no cover - never called
        return None


@pytest.fixture(scope="module")
def flow(tmp_path_factory: pytest.TempPathFactory, config_root: Path) -> Flow:
    """One real training run, shared by every test in this module."""
    root = tmp_path_factory.mktemp("train-flow")
    storage = LocalStorage(root / "data")
    registry = LocalModelRegistry(root / "registry.db")
    return run_once(storage, registry, config_root, seed=20260921)


@pytest.fixture(scope="module")
def head_to_head(flow: Flow, config_root: Path) -> Flow:
    """Run 1's model approved into the champion, then a second run on a different file."""
    flow.registry.approve(flow.record.model_version_id or "", by="tests")
    return run_once(flow.storage, flow.registry, config_root, seed=20270214)


# ---------------------------------------------------------------------------
# Every artefact of plan §7
# ---------------------------------------------------------------------------
def test_the_run_finished(flow: Flow) -> None:
    assert flow.record.state is RunState.DONE
    assert flow.record.error is None
    assert flow.record.finished_at is not None


@pytest.mark.parametrize("name", sorted(TRAIN_ARTEFACTS - {MODEL_DIRECTORY, "row_explanations.parquet"}))
def test_every_json_artefact_exists_and_validates_against_its_contract(flow: Flow, name: str) -> None:
    assert flow.storage.exists(flow.key(name)), f"{name} was not written"
    assert flow.artefact(name) is not None
    assert flow.record.artefacts[name] == flow.key(name)


def test_the_predictor_directory_holds_a_reloadable_model(flow: Flow) -> None:
    predictor_key = flow.record.artefacts[MODEL_DIRECTORY]
    directory = flow.storage.local_path(predictor_key)
    assert (directory / "predictor.pkl").is_file()
    assert flow.storage.exists(f"{predictor_key}/scorer.json")
    scorer = load_scorer(predictor_key, flow.storage)
    assert scorer.problem_type.value == "binary_classification"
    assert 0.0 < scorer.threshold < 1.0
    assert scorer.feature_columns


def test_row_explanations_carry_real_reasons(flow: Flow) -> None:
    explanations = explain.read_row_explanations(flow.key("row_explanations.parquet"), storage=flow.storage)
    limit = flow.resolved.config.evaluation.reasons_per_row
    features = set(_prepare(flow).feature_columns)
    assert explanations
    assert len(explanations) == _part(flow, "test").rows, "one explanation per hold-out row"
    assert len({item.primary_key for item in explanations}) == len(explanations), "one row per entity"
    for item in explanations:
        assert 0.0 <= item.score <= 1.0
        assert len(item.reasons) <= limit
        for reason in item.reasons:
            assert reason.text.strip()
            assert reason.feature in features
            assert reason.direction.value in {"up", "down"}
    explained = sum(1 for item in explanations if item.reasons)
    assert explained > len(explanations) // 2, "most rows must have something to say"


def test_the_leaderboard_is_a_real_search(flow: Flow) -> None:
    leaderboard = _leaderboard(flow)
    assert leaderboard.entries, "the search finished without a single model"
    assert leaderboard.models_trained == len(leaderboard.entries)
    assert [entry.rank for entry in leaderboard.entries] == list(range(1, len(leaderboard.entries) + 1))
    scores = [entry.validation_score for entry in leaderboard.entries]
    assert scores == sorted(scores, reverse=True)
    assert all(entry.fit_time_seconds > 0.0 for entry in leaderboard.entries)
    assert leaderboard.time_limit_seconds == 60
    best = _best(flow)
    assert best.model_name == leaderboard.best_model_name
    assert best.display_name


def test_the_evaluation_measured_the_whole_test_split(flow: Flow) -> None:
    evaluation = _evaluation(flow)
    test_rows = _part(flow, "test").rows
    confusion = flow.artefact("confusion_matrix.json")
    assert evaluation.rows_evaluated == test_rows
    assert confusion.total == test_rows
    assert evaluation.primary_metric is Metric.ROC_AUC
    assert evaluation.calibration is not None
    assert evaluation.calibration.fitted_on == "validation"


def test_the_decile_table_has_ten_bins(flow: Flow) -> None:
    lift = _lift(flow)
    assert len(lift.bins) == 10
    assert sum(item.rows for item in lift.bins) == _part(flow, "test").rows
    assert max(item.rows for item in lift.bins) - min(item.rows for item in lift.bins) <= 1


def test_the_importance_chart_is_real_and_sums_to_one_hundred(flow: Flow) -> None:
    importance = flow.artefact("feature_importance.json")
    assert importance.items, "no feature importance was measured"
    features = set(_prepare(flow).feature_columns)
    assert {item.feature for item in importance.items} <= features
    total = sum(Decimal(str(item.share_pct)) for item in importance.items)
    assert total == Decimal("100.0")


def test_the_schema_and_the_drift_baseline_describe_the_training_split(flow: Flow) -> None:
    schema = flow.artefact("schema.json")
    baseline = flow.artefact("drift_baseline.json")
    train_rows = _part(flow, "train").rows
    assert schema.row_count_at_fit == train_rows
    assert baseline.rows == train_rows
    assert [column.name for column in schema.columns] == [item.feature for item in baseline.features]
    assert {column.name for column in schema.columns} <= set(_prepare(flow).feature_columns)
    assert PRIMARY_KEY not in {column.name for column in schema.columns}
    assert schema.model_version_id == flow.record.model_version_id


# ---------------------------------------------------------------------------
# status.json, run.json and the manifest
# ---------------------------------------------------------------------------
def test_the_status_document_shows_eight_done_stages_in_five_rows(flow: Flow) -> None:
    status = flow.storage.read_model(flow.key(STATUS_FILENAME), RunStatus)
    assert status.state is RunState.DONE
    assert status.progress_pct == 100
    assert tuple(row.key for row in status.stages) == TRAIN_STAGES
    assert all(row.state is RunState.DONE for row in status.stages)
    assert all(row.detail for row in status.stages), "every stage writes its own detail line"
    assert all((row.duration_seconds or 0.0) >= 0.0 for row in status.stages)
    assert tuple(dict.fromkeys(row.group_label for row in status.stages)) == running_rows(RunMode.TRAIN)


def test_the_run_record_reports_what_was_measured(flow: Flow) -> None:
    record = flow.storage.read_model(flow.key(RUN_FILENAME), RunRecord)
    evaluation = _evaluation(flow)
    assert record == flow.record
    assert record.headline_metric is Metric.ROC_AUC
    assert record.headline_score == evaluation.headline_score
    assert record.best_model == _best(flow).display_name
    assert record.row_count == _profile(flow).row_count == ROWS
    assert record.model_version_id is not None


def test_the_manifest_describes_the_recipe_the_data_and_the_cost(flow: Flow) -> None:
    manifest = flow.storage.read_model(flow.key(MANIFEST_FILENAME), RunManifest)
    profile = _profile(flow)
    prepare_report = _prepare(flow)
    rebuilt: Recipe = recipe_from_config(
        flow.resolved.config,
        primary_key=PRIMARY_KEY,
        feature_columns=prepare_report.feature_columns,
        seed=seed_from(flow.run_id),
        target=TARGET,
    )
    assert manifest.recipe is not None
    assert manifest.recipe.recipe_hash == rebuilt.recipe_hash
    assert manifest.dataset_fingerprint == profile.fingerprint
    assert manifest.seed == seed_from(flow.run_id)
    assert manifest.leaderboard_path == flow.key("leaderboard.json")
    assert manifest.metrics["roc_auc"] == _evaluation(flow).headline_score
    assert manifest.metrics["baseline_roc_auc"] > 0.0
    assert manifest.duration_s > 0.0
    assert manifest.cost_estimate.compute_seconds > 0.0
    assert manifest.cost_estimate.estimated_usd is None, "a local run bills nothing; zero would be a lie"


def test_the_transforms_were_fitted_on_the_training_rows_only(flow: Flow) -> None:
    # The artefact-level proof of the seam: every fitted transform recorded how many rows it saw,
    # and that count is the training partition's, not the whole prepared frame's.
    prepare_report = _prepare(flow)
    split = _split(flow)
    fitted = [item for item in prepare_report.transforms if "fit_rows" in item.parameters]
    assert fitted, "the shipped config fits at least the clip bounds"
    assert {item.parameters["fit_rows"] for item in fitted} == {float(_part(flow, "train").rows)}
    assert sum(part.rows for part in split.parts) == prepare_report.rows_out


# ---------------------------------------------------------------------------
# Plan §10's golden checks
# ---------------------------------------------------------------------------
def test_the_model_beats_the_logistic_baseline_and_zero_point_seven(flow: Flow) -> None:
    evaluation = _evaluation(flow)
    comparison = flow.artefact("baseline.json")
    roc = next(row for row in comparison.rows if row.id is Metric.ROC_AUC)
    broken = "If this fails, the pipeline is broken, not the data."
    assert evaluation.headline_score > 0.7, f"test ROC-AUC {evaluation.headline_score}. {broken}"
    assert comparison.model_beats_baseline is True, broken
    assert roc.baseline_value is not None
    assert (
        roc.model_value > roc.baseline_value
    ), f"model {roc.model_value} vs baseline {roc.baseline_value}. {broken}"


def test_the_decile_chart_separates_the_top_from_the_bottom(flow: Flow) -> None:
    lift = _lift(flow)
    first, last = lift.bins[0], lift.bins[-1]
    assert first.lift is not None and last.lift is not None
    assert first.lift > last.lift
    assert lift.bins[-1].cumulative_capture_pct == pytest.approx(100.0, abs=0.5)


# ---------------------------------------------------------------------------
# The registry and the champion rule
# ---------------------------------------------------------------------------
def test_the_first_model_waits_for_approval_before_it_is_champion(flow: Flow) -> None:
    first = flow.registry.get(flow.record.model_version_id or "")
    assert flow.record.champion is False, "approval_required is on, so the run crowns nothing"
    assert first.run_id == flow.run_id
    assert first.test_score == _evaluation(flow).headline_score
    assert first.metric is Metric.ROC_AUC
    assert first.predictor_key == flow.record.artefacts[MODEL_DIRECTORY]
    assert first.schema_key == flow.key("schema.json")
    assert first.drift_baseline_key == flow.key("drift_baseline.json")
    if first.approved_at is None:  # nothing has approved it yet in this module
        assert first.status is ModelStatus.PENDING_APPROVAL
        assert flow.registry.get_champion(USE_CASE) is None


def test_the_champion_is_re_scored_on_the_second_runs_own_test_split(flow: Flow, head_to_head: Flow) -> None:
    incumbent = flow.registry.get(flow.record.model_version_id or "")
    manifest = head_to_head.storage.read_model(head_to_head.key(MANIFEST_FILENAME), RunManifest)
    fresh = manifest.metrics.get("champion_roc_auc")
    assert fresh is not None, "the champion was not re-scored on this run's test split"
    assert 0.0 < fresh <= 1.0
    # The stored score was measured on run 1's hold-out; the fresh one on run 2's. They are two
    # different samples, so they must not be assumed equal - and the stored one decides nothing.
    assert incumbent.test_score != manifest.metrics["roc_auc"]


def test_the_promotion_follows_the_fresh_pair_of_numbers(flow: Flow, head_to_head: Flow) -> None:
    manifest = head_to_head.storage.read_model(head_to_head.key(MANIFEST_FILENAME), RunManifest)
    incumbent = flow.registry.get(flow.record.model_version_id or "")
    challenger = head_to_head.registry.get(head_to_head.record.model_version_id or "")
    rescored = incumbent.model_copy(update={"test_score": manifest.metrics["champion_roc_auc"]})
    expected = should_promote(
        challenger,
        rescored,
        flow.resolved.config.evaluation.champion_min_improvement_pct,
        greater_is_better=True,
    )
    assert challenger.test_score == manifest.metrics["roc_auc"]
    if expected:
        assert challenger.status is ModelStatus.PENDING_APPROVAL
        assert head_to_head.record.beat_previous_champion is True
    else:
        assert challenger.status is ModelStatus.CANDIDATE
        assert head_to_head.record.beat_previous_champion is False
        assert head_to_head.registry.get_champion(USE_CASE).model_id == incumbent.model_id
    status = head_to_head.storage.read_model(head_to_head.key(STATUS_FILENAME), RunStatus)
    register_row = next(row for row in status.stages if row.key is StageKey.REGISTER)
    assert register_row.detail.endswith("drift baseline stored")


def test_the_second_run_is_a_complete_run_of_its_own(flow: Flow, head_to_head: Flow) -> None:
    assert head_to_head.record.state is RunState.DONE
    assert head_to_head.run_id != flow.run_id
    assert head_to_head.record.model_version_id != flow.record.model_version_id
    for name in sorted(TRAIN_ARTEFACTS - {MODEL_DIRECTORY}):
        assert head_to_head.storage.exists(head_to_head.key(name)), f"{name} was not written"


# ---------------------------------------------------------------------------
# Small typed readers
# ---------------------------------------------------------------------------
def _profile(flow: Flow) -> DatasetProfile:
    return flow.storage.read_model(flow.key("profile.json"), DatasetProfile)


def _prepare(flow: Flow) -> PrepareReport:
    return flow.storage.read_model(flow.key("prepare.json"), PrepareReport)


def _split(flow: Flow) -> SplitReport:
    return flow.storage.read_model(flow.key("split.json"), SplitReport)


def _leaderboard(flow: Flow) -> Leaderboard:
    return flow.storage.read_model(flow.key("leaderboard.json"), Leaderboard)


def _best(flow: Flow) -> BestModel:
    return flow.storage.read_model(flow.key("best_model.json"), BestModel)


def _evaluation(flow: Flow) -> EvaluationReport:
    return flow.storage.read_model(flow.key("evaluation.json"), EvaluationReport)


def _lift(flow: Flow) -> DecileLift:
    return flow.storage.read_model(flow.key("decile_lift.json"), DecileLift)


def _part(flow: Flow, name: str) -> SplitPart:
    """One partition of `split.json`, by name rather than by position."""
    return next(part for part in _split(flow).parts if part.name == name)
