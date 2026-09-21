"""`engine.registry`: the champion policy, the SQLite store and its one-transaction champion swap."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from engine.config import Metric
from engine.contracts import ModelStatus, ModelVersion
from engine.registry import (
    LocalModelRegistry,
    ModelRegistry,
    ModelVersionRow,
    RegistryError,
    should_promote,
)
from engine.utils.time import utc_now

USE_CASE: str = "a-use-case"


def make_version(
    model_id: str,
    version: int,
    *,
    use_case_id: str = USE_CASE,
    status: ModelStatus = ModelStatus.CANDIDATE,
    test_score: float = 0.80,
    metric: Metric = Metric.ROC_AUC,
    minutes: int = 0,
) -> ModelVersion:
    """A complete `ModelVersion` with no fabricated numbers beyond the ones a test compares."""
    return ModelVersion(
        model_id=model_id,
        use_case_id=use_case_id,
        version=version,
        run_id=f"r_{model_id}",
        created_at=utc_now() + timedelta(minutes=minutes),
        status=status,
        metric=metric,
        metric_label="ROC-AUC",
        test_score=test_score,
        validation_score=None,
        model_display_name="WeightedEnsemble_L2",
        schema_key=f"models/{model_id}/schema.json",
        run_config_key=f"models/{model_id}/run_config.json",
        predictor_key=f"models/{model_id}/model",
        artefact_keys={"run.json": f"runs/r_{model_id}/run.json"},
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )


@pytest.fixture
def registry(tmp_path: Path) -> LocalModelRegistry:
    return LocalModelRegistry(tmp_path / "registry.db")


def test_local_registry_satisfies_the_protocol(tmp_path: Path) -> None:
    assert isinstance(LocalModelRegistry(tmp_path / "r.db"), ModelRegistry)


def test_the_database_file_and_its_parent_are_created(tmp_path: Path) -> None:
    registry = LocalModelRegistry(tmp_path / "nested" / "registry.db")
    assert registry.db_path.parent.is_dir()
    assert registry.db_path.is_file()


def test_register_then_get_round_trips_every_field(registry: LocalModelRegistry) -> None:
    version = make_version("m_1", 1)
    assert registry.register(version) == version
    assert registry.get("m_1") == version


def test_a_duplicate_model_id_is_rejected(registry: LocalModelRegistry) -> None:
    registry.register(make_version("m_1", 1))
    with pytest.raises(RegistryError) as excinfo:
        registry.register(make_version("m_1", 2))
    assert excinfo.value.code == "DUPLICATE_MODEL_ID"
    assert excinfo.value.model_id == "m_1"


def test_an_unknown_model_id_is_reported(registry: LocalModelRegistry) -> None:
    with pytest.raises(RegistryError) as excinfo:
        registry.get("m_absent")
    assert excinfo.value.code == "MODEL_NOT_FOUND"


def test_next_version_increments_per_use_case(registry: LocalModelRegistry) -> None:
    assert registry.next_version(USE_CASE) == 1
    registry.register(make_version("m_1", 1))
    assert registry.next_version(USE_CASE) == 2
    assert registry.next_version("another-use-case") == 1
    registry.register(make_version("m_other", 1, use_case_id="another-use-case"))
    assert registry.next_version("another-use-case") == 2
    registry.register(make_version("m_2", 2))
    assert registry.next_version(USE_CASE) == 3


def test_list_versions_filters_and_sorts_newest_first(registry: LocalModelRegistry) -> None:
    registry.register(make_version("m_1", 1, minutes=0))
    registry.register(make_version("m_2", 2, minutes=5))
    registry.register(make_version("m_other", 1, use_case_id="another-use-case", minutes=10))
    assert [v.model_id for v in registry.list_versions()] == ["m_other", "m_2", "m_1"]
    assert [v.model_id for v in registry.list_versions(USE_CASE)] == ["m_2", "m_1"]
    assert [v.model_id for v in registry.list_versions("another-use-case")] == ["m_other"]
    assert registry.list_versions("no-such-use-case") == ()


def test_there_is_no_champion_until_one_is_approved(registry: LocalModelRegistry) -> None:
    assert registry.get_champion(USE_CASE) is None
    registry.register(make_version("m_1", 1))
    assert registry.get_champion(USE_CASE) is None


def test_approve_promotes_and_archives_the_previous_champion(registry: LocalModelRegistry) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    first = registry.approve("m_1", by="ops@telco")
    assert first.status is ModelStatus.CHAMPION
    assert first.approved_by == "ops@telco"
    assert first.approved_at is not None
    assert first.promoted_at is not None
    assert first.previous_champion_id is None
    assert registry.get_champion(USE_CASE) == first

    registry.register(make_version("m_2", 2, status=ModelStatus.PENDING_APPROVAL, test_score=0.86))
    second = registry.approve("m_2", by="ops@telco")
    assert second.status is ModelStatus.CHAMPION
    assert second.previous_champion_id == "m_1"
    assert registry.get("m_1").status is ModelStatus.ARCHIVED
    assert registry.get_champion(USE_CASE) == second


def test_approving_a_candidate_is_an_invalid_transition(registry: LocalModelRegistry) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.CANDIDATE))
    with pytest.raises(RegistryError) as excinfo:
        registry.approve("m_1", by="ops@telco")
    assert excinfo.value.code == "INVALID_TRANSITION"
    assert registry.get("m_1").status is ModelStatus.CANDIDATE


def test_promote_is_the_manual_override_and_records_the_note(registry: LocalModelRegistry) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    registry.approve("m_1", by="ops@telco")
    registry.register(make_version("m_2", 2, status=ModelStatus.CANDIDATE, test_score=0.81))
    promoted = registry.promote("m_2", by="ops@telco", note="Campaign needs the newer features.")
    assert promoted.status is ModelStatus.CHAMPION
    assert promoted.promotion_note == "Campaign needs the newer features."
    assert promoted.promoted_by == "ops@telco"
    assert promoted.previous_champion_id == "m_1"
    assert registry.get("m_1").status is ModelStatus.ARCHIVED

    with pytest.raises(RegistryError) as excinfo:
        registry.promote("m_1", by="ops@telco", note="again")
    assert excinfo.value.code == "INVALID_TRANSITION"


def test_archive_retires_a_version(registry: LocalModelRegistry) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    registry.approve("m_1", by="ops@telco")
    assert registry.archive("m_1").status is ModelStatus.ARCHIVED
    assert registry.get_champion(USE_CASE) is None


def test_the_row_projection_round_trips(registry: LocalModelRegistry) -> None:
    version = make_version("m_1", 1)
    assert ModelVersionRow.from_contract(version).to_contract() == version
    assert ModelVersionRow.__tablename__ == "model_version"


def test_the_use_case_and_version_pair_is_unique(registry: LocalModelRegistry) -> None:
    registry.register(make_version("m_1", 1))
    with pytest.raises(Exception, match="UNIQUE constraint failed"):
        registry.register(make_version("m_2", 1))


def test_two_threads_writing_concurrently_do_not_raise(registry: LocalModelRegistry) -> None:
    start = threading.Barrier(2)
    failures: list[BaseException] = []

    def write(offset: int) -> None:
        try:
            start.wait(timeout=10.0)
            for index in range(5):
                number = offset + index
                registry.register(make_version(f"m_{number}", number))
        except BaseException as exc:  # the test asserts there is nothing to catch
            failures.append(exc)

    threads = [threading.Thread(target=write, args=(offset,)) for offset in (1, 100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)
    assert failures == []
    assert len(registry.list_versions(USE_CASE)) == 10


def test_should_promote_with_no_champion_is_true() -> None:
    assert should_promote(make_version("m_1", 1), None, 1.0, greater_is_better=True) is True


@pytest.mark.parametrize(
    ("candidate_score", "expected"),
    [(0.804, False), (0.808, True), (0.816, True), (0.80, False), (0.79, False)],
)
def test_should_promote_needs_the_configured_improvement(candidate_score: float, expected: bool) -> None:
    champion = make_version("m_champ", 1, status=ModelStatus.CHAMPION, test_score=0.80)
    candidate = make_version("m_cand", 2, test_score=candidate_score)
    assert should_promote(candidate, champion, 1.0, greater_is_better=True) is expected


def test_should_promote_accepts_an_equal_score_only_at_a_zero_rule() -> None:
    champion = make_version("m_champ", 1, status=ModelStatus.CHAMPION, test_score=0.80)
    candidate = make_version("m_cand", 2, test_score=0.80)
    assert should_promote(candidate, champion, 0.0, greater_is_better=True) is True
    assert should_promote(candidate, champion, 1.0, greater_is_better=True) is False


def test_should_promote_is_inverted_when_lower_is_better() -> None:
    champion = make_version("m_champ", 1, status=ModelStatus.CHAMPION, test_score=10.0, metric=Metric.RMSE)
    better = make_version("m_better", 2, test_score=9.8, metric=Metric.RMSE)
    worse = make_version("m_worse", 3, test_score=10.2, metric=Metric.RMSE)
    assert should_promote(better, champion, 1.0, greater_is_better=False) is True
    assert should_promote(worse, champion, 1.0, greater_is_better=False) is False
    assert should_promote(better, champion, 1.0, greater_is_better=True) is False


def test_should_promote_refuses_to_compare_two_metrics() -> None:
    champion = make_version("m_champ", 1, status=ModelStatus.CHAMPION, test_score=0.80)
    candidate = make_version("m_cand", 2, test_score=0.90, metric=Metric.PR_AUC)
    with pytest.raises(RegistryError) as excinfo:
        should_promote(candidate, champion, 1.0, greater_is_better=True)
    assert excinfo.value.code == "METRIC_MISMATCH"
