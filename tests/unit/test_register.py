"""`engine.stages.register`: version numbering, the champion rule, the keys and `schema.json`.

The drift baseline this module also produces is tested next to its consumer, in `test_drift.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from engine.config import (
    ColumnType,
    Metric,
    ModelFamily,
    ProblemType,
    ResolvedConfig,
    RunMode,
    resolve_config,
)
from engine.contracts import BestModel, FeatureSchema, ModelStatus, ModelVersion
from engine.jobs import CancelToken
from engine.pipeline import StageContext
from engine.registry import LocalModelRegistry, RegistryError, should_promote
from engine.stages.register import (
    MAX_CATEGORY_LEVELS,
    ChampionScore,
    build_model_version,
    feature_schema,
    next_version_id,
    store_model_version,
)
from engine.storage import LocalStorage
from engine.utils.time import utc_now

USE_CASE: str = "targeted-advertisement"
PRIMARY_KEY: str = "customer_id"
RUN_ID: str = "r_20260921_0000beef"


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------
@pytest.fixture
def resolved(config_root: Path) -> ResolvedConfig:
    """The shipped use case, resolved against the checkout's configuration root."""
    return resolve_config(USE_CASE, root=config_root)


def resolve(config_root: Path, **overrides: object) -> ResolvedConfig:
    """The use case with run overrides applied, exactly as the API would send them."""
    return resolve_config(USE_CASE, overrides, root=config_root)


def make_context(
    tmp_path: Path,
    resolved: ResolvedConfig,
    *,
    run_id: str = RUN_ID,
    model_version_id: str | None = None,
    registry: LocalModelRegistry | None = None,
) -> StageContext:
    """A `StageContext` with a real registry and store, which is all the register stage touches."""
    return StageContext(
        run_id=run_id,
        mode=RunMode.TRAIN,
        config=resolved.config,
        resolved=resolved,
        storage=LocalStorage(tmp_path / "data"),
        registry=registry if registry is not None else LocalModelRegistry(tmp_path / "registry.db"),
        cancel=CancelToken(),
        primary_key=PRIMARY_KEY,
        target=resolved.config.target.column,
        upload_key="uploads/u_0001/data.csv",
        model_version_id=model_version_id,
    )


def make_best(
    *,
    metric: Metric = Metric.ROC_AUC,
    test_score: float = 0.84,
    validation_score: float | None = 0.83,
    run_id: str = RUN_ID,
) -> BestModel:
    """The `best_model.json` the train stage hands to register."""
    return BestModel(
        run_id=run_id,
        model_name="WeightedEnsemble_L2",
        family=ModelFamily.XGBOOST,
        is_ensemble=False,
        ensemble_members=(),
        display_name="XGBoost",
        metric=metric,
        metric_label="unused: register reads the catalog label",
        validation_score=validation_score,
        test_score=test_score,
        hyperparameters={},
        hyperparameters_summary="depth 6 · lr 0.05",
        training_rows=700,
        feature_count=5,
        fit_time_seconds=12.5,
        predictor_key=f"runs/{run_id}/model",
        trained_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
    )


def make_schema(model_version_id: str) -> FeatureSchema:
    """A minimal `schema.json` for the version under test; the real one is built below."""
    return FeatureSchema(
        use_case_id=USE_CASE,
        model_version_id=model_version_id,
        primary_key=PRIMARY_KEY,
        target="converted_30d",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        columns=(),
        row_count_at_fit=700,
        created_at=utc_now(),
    )


def rescored(champion: ModelVersion, test_score: float) -> ChampionScore:
    """The champion as the caller re-scored it on the challenger's own test split."""
    return ChampionScore(model_id=champion.model_id, metric=champion.metric, test_score=test_score)


def challenge(ctx: StageContext, best: BestModel, champion_score: ChampionScore | None) -> ModelVersion:
    """Build the version this run would register, without storing it."""
    model_id, _ = next_version_id(ctx)
    return build_model_version(ctx, best, make_schema(model_id), champion_score=champion_score)


def register_version(
    ctx: StageContext, best: BestModel, champion_score: ChampionScore | None = None
) -> ModelVersion:
    """Build and store one model version, the way the train flow does."""
    return store_model_version(ctx.registry, challenge(ctx, best, champion_score))


def train_frame() -> pd.DataFrame:
    """A fitted frame: the five template features plus the reserved columns around them."""
    return pd.DataFrame(
        {
            "customer_id": ["C-1", "C-2", "C-3", "C-4"],
            "visits_last_7d": [12, 3, 0, 7],
            "ad_ctr_90d": [0.041, 0.012, 0.0, None],
            "plan_tier": ["premium", "basic", "basic", "standard"],
            "tenure_months": [36, 4, 11, 62],
            "region": ["south", "north", "west", "south"],
            "marketing_opt_in": [True, True, False, True],
            "last_contacted_at": pd.to_datetime(["2026-07-20", "2026-06-02", None, "2026-07-29"]),
            "converted_30d": [1, 0, 0, 1],
        }
    )


# ---------------------------------------------------------------------------
# Version numbering
# ---------------------------------------------------------------------------
def test_the_first_model_of_a_use_case_is_version_one(tmp_path: Path, resolved: ResolvedConfig) -> None:
    ctx = make_context(tmp_path, resolved)
    model_id, version = next_version_id(ctx)
    assert version == 1
    assert model_id == "m_targeted-advertisement_1"
    assert build_model_version(ctx, make_best(), make_schema(model_id)).version == 1


def test_version_numbers_count_on_across_runs_of_the_same_use_case(tmp_path: Path, config_root: Path) -> None:
    resolved = resolve(config_root, **{"governance.approval_required": False})
    registry = LocalModelRegistry(tmp_path / "registry.db")
    versions: list[ModelVersion] = []
    for index in range(3):
        run_id = f"r_2026092{index}_0000beef"
        ctx = make_context(tmp_path, resolved, run_id=run_id, registry=registry)
        champion = registry.get_champion(USE_CASE)
        versions.append(
            register_version(
                ctx,
                make_best(test_score=0.80 + index / 100, run_id=run_id),
                None if champion is None else rescored(champion, 0.80),
            )
        )
    assert [version.version for version in versions] == [1, 2, 3]
    assert [version.model_id for version in versions] == [
        "m_targeted-advertisement_1",
        "m_targeted-advertisement_2",
        "m_targeted-advertisement_3",
    ]
    assert [version.run_id for version in versions] == [
        "r_20260920_0000beef",
        "r_20260921_0000beef",
        "r_20260922_0000beef",
    ]


def test_each_use_case_counts_its_own_versions(tmp_path: Path, config_root: Path) -> None:
    registry = LocalModelRegistry(tmp_path / "registry.db")
    first = resolve_config(USE_CASE, root=config_root)
    other = resolve_config("payment-propensity", root=config_root)
    register_version(make_context(tmp_path, first, registry=registry), make_best())
    register_version(make_context(tmp_path, first, registry=registry), make_best(test_score=0.90))
    other_version = register_version(make_context(tmp_path, other, registry=registry), make_best())
    assert other_version.version == 1
    assert other_version.model_id == "m_payment-propensity_1"
    assert other_version.use_case_id == "payment-propensity"
    assert registry.next_version(USE_CASE) == 3
    assert registry.next_version("payment-propensity") == 2


def test_a_model_id_fixed_by_the_run_is_kept(tmp_path: Path, resolved: ResolvedConfig) -> None:
    ctx = make_context(tmp_path, resolved, model_version_id="m_fixed_by_the_caller")
    model_id, version = next_version_id(ctx)
    assert (model_id, version) == ("m_fixed_by_the_caller", 1)
    assert build_model_version(ctx, make_best(), make_schema(model_id)).model_id == "m_fixed_by_the_caller"


def test_a_schema_written_for_another_model_is_refused(tmp_path: Path, resolved: ResolvedConfig) -> None:
    ctx = make_context(tmp_path, resolved)
    with pytest.raises(ValueError, match="m_somebody_else"):
        build_model_version(ctx, make_best(), make_schema("m_somebody_else"))


# ---------------------------------------------------------------------------
# The champion rule
# ---------------------------------------------------------------------------
def test_the_first_model_is_the_champion_when_no_approval_is_required(
    tmp_path: Path, config_root: Path
) -> None:
    resolved = resolve(config_root, **{"governance.approval_required": False})
    ctx = make_context(tmp_path, resolved)
    version = build_model_version(ctx, make_best(), make_schema(next_version_id(ctx)[0]))
    assert version.status is ModelStatus.CHAMPION
    assert version.previous_champion_id is None
    assert version.improvement_pct is None
    assert version.promoted_by == "engine"
    assert version.promoted_at == version.created_at
    assert version.promotion_note == "First model registered for targeted-advertisement."


def test_the_first_model_waits_for_approval_when_the_use_case_asks_for_it(
    tmp_path: Path, resolved: ResolvedConfig
) -> None:
    assert resolved.config.governance.approval_required is True
    ctx = make_context(tmp_path, resolved)
    version = build_model_version(ctx, make_best(), make_schema(next_version_id(ctx)[0]))
    assert version.status is ModelStatus.PENDING_APPROVAL
    assert version.promoted_at is None
    assert version.previous_champion_id is None


def test_a_clear_winner_takes_the_championship_and_records_the_improvement(
    tmp_path: Path, config_root: Path
) -> None:
    resolved = resolve(config_root, **{"governance.approval_required": False})
    registry = LocalModelRegistry(tmp_path / "registry.db")
    champion = register_version(
        make_context(tmp_path, resolved, registry=registry), make_best(test_score=0.80)
    )
    assert champion.status is ModelStatus.CHAMPION
    ctx = make_context(tmp_path, resolved, run_id="r_20260922_0000beef", registry=registry)
    challenger = challenge(ctx, make_best(test_score=0.84, run_id=ctx.run_id), rescored(champion, 0.80))
    assert challenger.status is ModelStatus.CHAMPION
    assert challenger.previous_champion_id == champion.model_id
    assert challenger.improvement_pct == pytest.approx((0.84 - 0.80) / 0.80 * 100.0)
    assert challenger.improvement_pct == 5.0
    assert challenger.promotion_note == (
        "Beats m_targeted-advertisement_1 by +5.00% on ROC-AUC, both re-scored on this run's test split."
    )


def test_the_champions_stored_score_is_never_the_one_compared(tmp_path: Path, config_root: Path) -> None:
    """A stored score belongs to an older test split; only the re-measured one may decide."""
    resolved = resolve(config_root, **{"governance.approval_required": False})
    registry = LocalModelRegistry(tmp_path / "registry.db")
    champion = register_version(
        make_context(tmp_path, resolved, registry=registry), make_best(test_score=0.99)
    )
    ctx = make_context(tmp_path, resolved, run_id="r_20260922_0000beef", registry=registry)
    best = make_best(test_score=0.84, run_id=ctx.run_id)
    # against the stored 0.99 the challenger loses; re-scored on this run's frame it wins
    assert challenge(ctx, best, rescored(champion, 0.99)).status is ModelStatus.CANDIDATE
    challenger = challenge(ctx, best, rescored(champion, 0.80))
    assert challenger.status is ModelStatus.CHAMPION
    assert challenger.improvement_pct == 5.0
    assert registry.get(champion.model_id).test_score == 0.99  # the stored number is left alone


@pytest.mark.parametrize(
    "champion_score",
    [
        None,
        ChampionScore(model_id="m_targeted-advertisement_1", unavailable_reason="its predictor is missing"),
        ChampionScore(
            model_id="m_targeted-advertisement_1",
            unavailable_reason="it was fitted on another feature schema",
        ),
        ChampionScore(model_id="m_somebody_else", metric=Metric.ROC_AUC, test_score=0.10),
    ],
    ids=["not-re-scored", "predictor-missing", "schema-incompatible", "stale-champion"],
)
def test_a_champion_that_could_not_be_re_scored_blocks_the_promotion(
    tmp_path: Path,
    config_root: Path,
    caplog: pytest.LogCaptureFixture,
    champion_score: ChampionScore | None,
) -> None:
    resolved = resolve(config_root, **{"governance.approval_required": False})
    registry = LocalModelRegistry(tmp_path / "registry.db")
    champion = register_version(
        make_context(tmp_path, resolved, registry=registry), make_best(test_score=0.80)
    )
    ctx = make_context(tmp_path, resolved, run_id="r_20260922_0000beef", registry=registry)
    with caplog.at_level("WARNING", logger="engine.stages.register"):
        challenger = challenge(ctx, make_best(test_score=0.99, run_id=ctx.run_id), champion_score)
    assert challenger.status is ModelStatus.CANDIDATE
    assert challenger.improvement_pct is None
    assert challenger.previous_champion_id is None
    assert "promotion=skipped" in caplog.text
    assert registry.get_champion(USE_CASE) == champion  # the incumbent keeps the crown


def test_a_champion_score_is_either_a_score_or_a_reason() -> None:
    with pytest.raises(ValueError, match="never both and never neither"):
        ChampionScore(model_id="m_1")
    with pytest.raises(ValueError, match="never both and never neither"):
        ChampionScore(model_id="m_1", metric=Metric.ROC_AUC, test_score=0.8, unavailable_reason="missing")


def test_a_winner_that_needs_approval_is_pending_not_champion(tmp_path: Path, config_root: Path) -> None:
    approving = resolve(config_root, **{"governance.approval_required": False})
    registry = LocalModelRegistry(tmp_path / "registry.db")
    champion = register_version(
        make_context(tmp_path, approving, registry=registry), make_best(test_score=0.80)
    )
    resolved = resolve(config_root, **{"governance.approval_required": True})
    ctx = make_context(tmp_path, resolved, run_id="r_20260922_0000beef", registry=registry)
    challenger = challenge(ctx, make_best(test_score=0.84, run_id=ctx.run_id), rescored(champion, 0.80))
    assert challenger.status is ModelStatus.PENDING_APPROVAL
    assert challenger.improvement_pct == 5.0
    assert challenger.previous_champion_id is None  # the registry records it when the approval lands
    assert challenger.promoted_at is None
    approved = store_model_version(registry, challenger)
    assert approved.status is ModelStatus.PENDING_APPROVAL
    crowned = registry.approve(approved.model_id, by="a.analyst")
    assert crowned.status is ModelStatus.CHAMPION
    assert crowned.previous_champion_id == "m_targeted-advertisement_1"


@pytest.mark.parametrize(
    ("test_score", "minimum_pct", "expected"),
    [
        (0.808, 1.0, ModelStatus.CHAMPION),  # exactly +1.00 %, the configured minimum
        (0.8079, 1.0, ModelStatus.CANDIDATE),  # +0.9875 %, just under it
        (0.80, 0.0, ModelStatus.CHAMPION),  # a tie is enough when the rule asks for nothing
        (0.79, 0.0, ModelStatus.CANDIDATE),  # a strictly worse model never promotes
        (0.90, 5.0, ModelStatus.CHAMPION),
        (0.83, 5.0, ModelStatus.CANDIDATE),
    ],
)
def test_the_improvement_percentage_decides_the_status(
    tmp_path: Path,
    config_root: Path,
    test_score: float,
    minimum_pct: float,
    expected: ModelStatus,
) -> None:
    resolved = resolve(
        config_root,
        **{
            "governance.approval_required": False,
            "evaluation.champion_min_improvement_pct": minimum_pct,
        },
    )
    registry = LocalModelRegistry(tmp_path / "registry.db")
    champion = register_version(
        make_context(tmp_path, resolved, registry=registry), make_best(test_score=0.80)
    )
    ctx = make_context(tmp_path, resolved, run_id="r_20260922_0000beef", registry=registry)
    challenger = challenge(ctx, make_best(test_score=test_score, run_id=ctx.run_id), rescored(champion, 0.80))
    assert challenger.status is expected
    if expected is ModelStatus.CHAMPION:
        assert challenger.improvement_pct == pytest.approx((test_score - 0.80) / 0.80 * 100.0, abs=5e-3)
    else:
        assert challenger.improvement_pct is None


def test_a_lower_is_better_metric_flips_the_sign_of_the_improvement(
    tmp_path: Path, config_root: Path
) -> None:
    resolved = resolve(config_root, **{"problem_type": "regression", "governance.approval_required": False})
    assert resolved.config.model_search.metric is Metric.RMSE
    assert resolved.config.catalog.metrics[Metric.RMSE].greater_is_better is False
    registry = LocalModelRegistry(tmp_path / "registry.db")
    champion = register_version(
        make_context(tmp_path, resolved, registry=registry),
        make_best(metric=Metric.RMSE, test_score=1.00, validation_score=1.10),
    )
    ctx = make_context(tmp_path, resolved, run_id="r_20260922_0000beef", registry=registry)
    better = challenge(
        ctx, make_best(metric=Metric.RMSE, test_score=0.90, run_id=ctx.run_id), rescored(champion, 1.00)
    )
    assert better.status is ModelStatus.CHAMPION
    assert better.improvement_pct == pytest.approx(10.0)
    assert better.metric_label == "RMSE"
    worse_ctx = make_context(tmp_path, resolved, run_id="r_20260923_0000beef", registry=registry)
    worse = challenge(
        worse_ctx,
        make_best(metric=Metric.RMSE, test_score=1.50, run_id=worse_ctx.run_id),
        rescored(champion, 1.00),
    )
    assert worse.status is ModelStatus.CANDIDATE


def test_a_candidate_scored_on_another_metric_stays_a_candidate(tmp_path: Path, config_root: Path) -> None:
    resolved = resolve(config_root, **{"governance.approval_required": False})
    registry = LocalModelRegistry(tmp_path / "registry.db")
    champion = register_version(
        make_context(tmp_path, resolved, registry=registry), make_best(metric=Metric.ROC_AUC, test_score=0.80)
    )
    ctx = make_context(tmp_path, resolved, run_id="r_20260922_0000beef", registry=registry)
    # the champion was even re-scored on the challenger's metric: the guard is about what the two
    # models were optimised for, which no re-scoring changes
    candidate = challenge(
        ctx,
        make_best(metric=Metric.PR_AUC, test_score=0.99, run_id=ctx.run_id),
        ChampionScore(model_id=champion.model_id, metric=Metric.PR_AUC, test_score=0.70),
    )
    assert candidate.status is ModelStatus.CANDIDATE
    assert candidate.improvement_pct is None
    assert candidate.previous_champion_id is None
    assert candidate.metric is Metric.PR_AUC
    assert candidate.metric_label == "PR-AUC"
    # the guard itself is the registry's, and it is an error there
    with pytest.raises(RegistryError) as excinfo:
        should_promote(candidate, champion, 1.0, greater_is_better=True)
    assert excinfo.value.code == "METRIC_MISMATCH"


def test_storing_a_champion_demotes_the_incumbent(tmp_path: Path, config_root: Path) -> None:
    resolved = resolve(config_root, **{"governance.approval_required": False})
    registry = LocalModelRegistry(tmp_path / "registry.db")
    first = register_version(make_context(tmp_path, resolved, registry=registry), make_best(test_score=0.80))
    second = register_version(
        make_context(tmp_path, resolved, run_id="r_20260922_0000beef", registry=registry),
        make_best(test_score=0.90, run_id="r_20260922_0000beef"),
        rescored(first, 0.80),
    )
    assert second.status is ModelStatus.CHAMPION
    assert second.previous_champion_id == first.model_id
    assert registry.get(first.model_id).status is ModelStatus.ARCHIVED
    champion = registry.get_champion(USE_CASE)
    assert champion is not None
    assert champion.model_id == second.model_id
    assert [version.status for version in registry.list_versions(USE_CASE)] == [
        ModelStatus.CHAMPION,
        ModelStatus.ARCHIVED,
    ]


# ---------------------------------------------------------------------------
# Storage keys and provenance
# ---------------------------------------------------------------------------
def test_the_storage_keys_point_at_this_runs_artefacts(tmp_path: Path, resolved: ResolvedConfig) -> None:
    import engine

    ctx = make_context(tmp_path, resolved)
    best = make_best()
    version = build_model_version(ctx, best, make_schema(next_version_id(ctx)[0]))
    assert version.schema_key == f"runs/{RUN_ID}/schema.json"
    assert version.run_config_key == f"runs/{RUN_ID}/run_config.json"
    assert version.drift_baseline_key == f"runs/{RUN_ID}/drift_baseline.json"
    assert version.predictor_key == best.predictor_key == f"runs/{RUN_ID}/model"
    assert version.artefact_keys == {
        "schema.json": f"runs/{RUN_ID}/schema.json",
        "run_config.json": f"runs/{RUN_ID}/run_config.json",
        "drift_baseline.json": f"runs/{RUN_ID}/drift_baseline.json",
        "model/": f"runs/{RUN_ID}/model",
    }
    assert version.engine_version == engine.__version__
    assert version.autogluon_version and version.autogluon_version != "unknown"


def test_the_record_carries_the_catalog_label_and_both_scores(
    tmp_path: Path, resolved: ResolvedConfig
) -> None:
    ctx = make_context(tmp_path, resolved)
    version = build_model_version(
        ctx, make_best(test_score=0.84, validation_score=0.83), make_schema(next_version_id(ctx)[0])
    )
    assert version.metric is Metric.ROC_AUC
    assert version.metric_label == resolved.config.catalog.metric_label(Metric.ROC_AUC) == "ROC-AUC"
    assert (version.test_score, version.validation_score) == (0.84, 0.83)
    assert version.model_display_name == "XGBoost"
    assert version.use_case_id == USE_CASE
    assert version.created_at.tzinfo is not None


def test_a_stored_version_round_trips_through_the_registry(tmp_path: Path, resolved: ResolvedConfig) -> None:
    ctx = make_context(tmp_path, resolved)
    version = build_model_version(ctx, make_best(), make_schema(next_version_id(ctx)[0]))
    stored = store_model_version(ctx.registry, version)
    assert stored == version
    assert ctx.registry.get(version.model_id) == version


def test_building_the_same_version_twice_is_deterministic(tmp_path: Path, resolved: ResolvedConfig) -> None:
    ctx = make_context(tmp_path, resolved)
    model_id, _ = next_version_id(ctx)
    first = build_model_version(ctx, make_best(), make_schema(model_id))
    second = build_model_version(ctx, make_best(), make_schema(model_id))
    assert first.model_dump(exclude={"created_at"}) == second.model_dump(exclude={"created_at"})


# ---------------------------------------------------------------------------
# schema.json (plan section 4.4)
# ---------------------------------------------------------------------------
def build_schema(frame: pd.DataFrame, resolved: ResolvedConfig) -> FeatureSchema:
    """The schema of `frame` for the shipped use case."""
    return feature_schema(
        frame,
        resolved.config,
        primary_key=PRIMARY_KEY,
        target=resolved.config.target.column,
        model_version_id="m_targeted-advertisement_1",
    )


def test_the_schema_keeps_the_feature_columns_in_fit_order(resolved: ResolvedConfig) -> None:
    schema = build_schema(train_frame(), resolved)
    assert [column.name for column in schema.columns] == [
        "visits_last_7d",
        "ad_ctr_90d",
        "plan_tier",
        "tenure_months",
        "region",
    ]
    assert schema.primary_key == PRIMARY_KEY
    assert schema.target == "converted_30d"
    assert schema.use_case_id == USE_CASE
    assert schema.problem_type is ProblemType.BINARY_CLASSIFICATION
    assert schema.row_count_at_fit == 4
    assert schema.created_at.tzinfo is not None


def test_the_schema_leaves_out_every_reserved_column(resolved: ResolvedConfig) -> None:
    names = {column.name for column in build_schema(train_frame(), resolved).columns}
    assert PRIMARY_KEY not in names  # plan section 4.1: never a feature
    assert "converted_30d" not in names  # the target
    assert "marketing_opt_in" not in names  # the opt-out column
    assert "last_contacted_at" not in names  # the recent-contact column


def test_the_schema_leaves_out_excluded_columns(config_root: Path) -> None:
    resolved = resolve(config_root, **{"prepare.exclude_columns": ["region"]})
    names = {column.name for column in build_schema(train_frame(), resolved).columns}
    assert "region" not in names
    assert "plan_tier" in names


def test_the_schema_records_types_categories_and_ranges(resolved: ResolvedConfig) -> None:
    columns = {column.name: column for column in build_schema(train_frame(), resolved).columns}
    assert columns["visits_last_7d"].inferred_type is ColumnType.INTEGER
    assert (columns["visits_last_7d"].minimum, columns["visits_last_7d"].maximum) == (0.0, 12.0)
    assert columns["visits_last_7d"].categories == ()
    assert columns["ad_ctr_90d"].inferred_type is ColumnType.FLOAT
    assert (columns["ad_ctr_90d"].minimum, columns["ad_ctr_90d"].maximum) == (0.0, 0.041)
    assert columns["plan_tier"].inferred_type is ColumnType.STRING
    assert columns["plan_tier"].categories == ("basic", "premium", "standard")
    assert columns["plan_tier"].minimum is None and columns["plan_tier"].maximum is None
    assert all(column.required for column in columns.values())


def test_the_schema_records_where_nulls_were_seen(resolved: ResolvedConfig) -> None:
    columns = {column.name: column for column in build_schema(train_frame(), resolved).columns}
    assert columns["ad_ctr_90d"].nullable is True
    assert columns["visits_last_7d"].nullable is False


def test_the_schema_types_booleans_and_datetimes(resolved: ResolvedConfig) -> None:
    frame = train_frame()
    frame["weather_alert"] = [True, False, True, True]
    frame["seen_at"] = pd.to_datetime(["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"])
    columns = {column.name: column for column in build_schema(frame, resolved).columns}
    assert columns["weather_alert"].inferred_type is ColumnType.BOOLEAN
    assert columns["weather_alert"].categories == ("False", "True")
    assert columns["seen_at"].inferred_type is ColumnType.DATETIME
    assert columns["seen_at"].minimum is None  # a datetime range is not a float range


def test_a_high_cardinality_column_keeps_no_category_list(resolved: ResolvedConfig) -> None:
    rows = MAX_CATEGORY_LEVELS + 1
    frame = pd.DataFrame(
        {
            "customer_id": [f"C-{index}" for index in range(rows)],
            "plan_tier": [f"tier-{index}" for index in range(rows)],
            "region": ["south"] * rows,
            "converted_30d": [index % 2 for index in range(rows)],
        }
    )
    columns = {column.name: column for column in build_schema(frame, resolved).columns}
    assert columns["plan_tier"].categories == ()
    assert len(columns["region"].categories) == 1


def test_building_the_same_schema_twice_is_deterministic(resolved: ResolvedConfig) -> None:
    frame = train_frame()
    first = build_schema(frame, resolved)
    second = build_schema(frame, resolved)
    assert first.model_dump(exclude={"created_at"}) == second.model_dump(exclude={"created_at"})
