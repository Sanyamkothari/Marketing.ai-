"""`engine.registry`: the champion policy, the SQL store and its one-transaction champion swap.

Every store test runs **twice**: once against a SQLite file and once against a real PostgreSQL
schema migrated to head. That is the point of the Phase 4a split - `LocalModelRegistry` and the
deployed registry are the same `SqlRegistryStore` over different engines (DEC-338) - and a suite
that only ever ran on SQLite could not tell the difference between "the same" and "close enough".
The timezone defect at the bottom of this module is precisely such a difference: it is invisible on
SQLite and wrong by the server's UTC offset on Postgres.

The Postgres half skips, loudly and with a reason, when there is no server; see
`tests/fixtures/postgres.py`.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from engine.config import Metric
from engine.contracts import NO_CHAMPION_AT_DECISION, ModelStatus, ModelVersion
from engine.registry import (
    LocalModelRegistry,
    ModelRegistry,
    ModelVersionRow,
    RegistryError,
    SqlRegistryStore,
    should_promote,
)
from engine.utils.time import utc_now
from tests.fixtures.postgres import (  # noqa: F401 - imported so pytest can resolve them by name
    postgres_engine_fixture,
    postgres_registry,
    postgres_schema,
    postgres_url_value,
)

USE_CASE: str = "a-use-case"

UNIQUE_VIOLATION: dict[str, str] = {
    # SQLite names the columns, Postgres names the constraint. Both mean the same refusal, and
    # asserting on the real text of each is what keeps this test from passing on a different error.
    "sqlite": "UNIQUE constraint failed",
    "postgres": "uq_use_case_version",
}


def make_version(
    model_id: str,
    version: int,
    *,
    use_case_id: str = USE_CASE,
    status: ModelStatus = ModelStatus.CANDIDATE,
    test_score: float = 0.80,
    metric: Metric = Metric.ROC_AUC,
    minutes: int = 0,
    measured_against_champion_id: str | None = None,
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
        measured_against_champion_id=measured_against_champion_id,
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ]
)
def backend(request: pytest.FixtureRequest) -> str:
    """Which store the test in hand is running against; also the id in the test name."""
    return str(request.param)


@pytest.fixture
def registry(backend: str, request: pytest.FixtureRequest, tmp_path: Path) -> SqlRegistryStore:
    """The store under test: a SQLite file, or a migrated Postgres schema of this test's own.

    `getfixturevalue` rather than a parameter so the Postgres fixtures - and the skip they raise
    when there is no server - are only touched on the run that actually needs them.
    """
    if backend == "sqlite":
        return LocalModelRegistry(tmp_path / "registry.db")
    store: SqlRegistryStore = request.getfixturevalue("postgres_registry")
    return store


def test_local_registry_satisfies_the_protocol(tmp_path: Path) -> None:
    assert isinstance(LocalModelRegistry(tmp_path / "r.db"), ModelRegistry)


def test_the_database_file_and_its_parent_are_created(tmp_path: Path) -> None:
    registry = LocalModelRegistry(tmp_path / "nested" / "registry.db")
    assert registry.db_path.parent.is_dir()
    assert registry.db_path.is_file()


def test_register_then_get_round_trips_every_field(registry: SqlRegistryStore) -> None:
    version = make_version("m_1", 1)
    assert registry.register(version) == version
    assert registry.get("m_1") == version


def test_a_duplicate_model_id_is_rejected(registry: SqlRegistryStore) -> None:
    registry.register(make_version("m_1", 1))
    with pytest.raises(RegistryError) as excinfo:
        registry.register(make_version("m_1", 2))
    assert excinfo.value.code == "DUPLICATE_MODEL_ID"
    assert excinfo.value.model_id == "m_1"


def test_an_unknown_model_id_is_reported(registry: SqlRegistryStore) -> None:
    with pytest.raises(RegistryError) as excinfo:
        registry.get("m_absent")
    assert excinfo.value.code == "MODEL_NOT_FOUND"


def test_next_version_increments_per_use_case(registry: SqlRegistryStore) -> None:
    assert registry.next_version(USE_CASE) == 1
    registry.register(make_version("m_1", 1))
    assert registry.next_version(USE_CASE) == 2
    assert registry.next_version("another-use-case") == 1
    registry.register(make_version("m_other", 1, use_case_id="another-use-case"))
    assert registry.next_version("another-use-case") == 2
    registry.register(make_version("m_2", 2))
    assert registry.next_version(USE_CASE) == 3


def test_list_versions_filters_and_sorts_newest_first(registry: SqlRegistryStore) -> None:
    registry.register(make_version("m_1", 1, minutes=0))
    registry.register(make_version("m_2", 2, minutes=5))
    registry.register(make_version("m_other", 1, use_case_id="another-use-case", minutes=10))
    assert [v.model_id for v in registry.list_versions()] == ["m_other", "m_2", "m_1"]
    assert [v.model_id for v in registry.list_versions(USE_CASE)] == ["m_2", "m_1"]
    assert [v.model_id for v in registry.list_versions("another-use-case")] == ["m_other"]
    assert registry.list_versions("no-such-use-case") == ()


def test_there_is_no_champion_until_one_is_approved(registry: SqlRegistryStore) -> None:
    assert registry.get_champion(USE_CASE) is None
    registry.register(make_version("m_1", 1))
    assert registry.get_champion(USE_CASE) is None


def test_approve_promotes_and_archives_the_previous_champion(registry: SqlRegistryStore) -> None:
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


def test_approving_a_version_whose_champion_has_been_replaced_is_refused(
    registry: SqlRegistryStore,
) -> None:
    """DEC-047, the whole sequence: A and B both beat C, B is approved, A must not be approved over B."""
    registry.register(
        make_version(
            "m_c",
            1,
            status=ModelStatus.PENDING_APPROVAL,
            measured_against_champion_id=NO_CHAMPION_AT_DECISION,
        )
    )
    registry.approve("m_c", by="ops@telco")
    registry.register(
        make_version("m_a", 2, status=ModelStatus.PENDING_APPROVAL, measured_against_champion_id="m_c")
    )
    registry.register(
        make_version("m_b", 3, status=ModelStatus.PENDING_APPROVAL, measured_against_champion_id="m_c")
    )

    approved = registry.approve("m_b", by="ops@telco")
    assert approved.status is ModelStatus.CHAMPION
    assert approved.previous_champion_id == "m_c"

    with pytest.raises(RegistryError) as excinfo:
        registry.approve("m_a", by="ops@telco")
    assert excinfo.value.code == "CHAMPION_CHANGED"
    assert excinfo.value.model_id == "m_a"
    message = excinfo.value.message
    assert "m_c" in message and "m_b" in message
    assert "/models/m_a/promote" in message

    assert registry.get("m_a").status is ModelStatus.PENDING_APPROVAL
    assert registry.get("m_a").approved_by is None
    champion = registry.get_champion(USE_CASE)
    assert champion is not None
    assert champion.model_id == "m_b"


def test_approval_goes_through_while_the_compared_champion_still_holds(
    registry: SqlRegistryStore,
) -> None:
    registry.register(make_version("m_c", 1, status=ModelStatus.CHAMPION))
    registry.register(
        make_version("m_a", 2, status=ModelStatus.PENDING_APPROVAL, measured_against_champion_id="m_c")
    )
    approved = registry.approve("m_a", by="ops@telco")
    assert approved.status is ModelStatus.CHAMPION
    assert approved.previous_champion_id == "m_c"
    assert approved.measured_against_champion_id == "m_c"
    assert registry.get("m_c").status is ModelStatus.ARCHIVED


def test_a_version_decided_against_no_champion_is_approvable_only_while_there_is_none(
    registry: SqlRegistryStore,
) -> None:
    """A first model waiting for approval is just as stale once somebody else has been crowned."""
    registry.register(
        make_version(
            "m_a",
            1,
            status=ModelStatus.PENDING_APPROVAL,
            measured_against_champion_id=NO_CHAMPION_AT_DECISION,
        )
    )
    registry.register(
        make_version(
            "m_b",
            2,
            status=ModelStatus.PENDING_APPROVAL,
            measured_against_champion_id=NO_CHAMPION_AT_DECISION,
        )
    )
    assert registry.approve("m_b", by="ops@telco").status is ModelStatus.CHAMPION

    with pytest.raises(RegistryError) as excinfo:
        registry.approve("m_a", by="ops@telco")
    assert excinfo.value.code == "CHAMPION_CHANGED"
    assert "no champion at all" in excinfo.value.message
    assert registry.get("m_a").status is ModelStatus.PENDING_APPROVAL


def test_a_version_registered_before_the_compared_champion_was_recorded_is_still_approvable(
    registry: SqlRegistryStore,
) -> None:
    """A null records nothing about the comparison, so approval behaves as it did before DEC-047."""
    registry.register(make_version("m_c", 1, status=ModelStatus.CHAMPION))
    registry.register(make_version("m_a", 2, status=ModelStatus.PENDING_APPROVAL))
    assert registry.get("m_a").measured_against_champion_id is None
    approved = registry.approve("m_a", by="ops@telco")
    assert approved.status is ModelStatus.CHAMPION
    assert approved.previous_champion_id == "m_c"


def test_promote_still_overrides_the_rule_after_the_champion_changed(
    registry: SqlRegistryStore,
) -> None:
    """`CHAMPION_CHANGED` refuses the rubber stamp, not the informed manual override of plan §8."""
    registry.register(make_version("m_c", 1, status=ModelStatus.CHAMPION))
    registry.register(
        make_version("m_a", 2, status=ModelStatus.PENDING_APPROVAL, measured_against_champion_id="m_gone")
    )
    with pytest.raises(RegistryError) as excinfo:
        registry.approve("m_a", by="ops@telco")
    assert excinfo.value.code == "CHAMPION_CHANGED"
    promoted = registry.promote("m_a", by="ops@telco", note="Compared both by hand on the newer data.")
    assert promoted.status is ModelStatus.CHAMPION
    assert promoted.promotion_note == "Compared both by hand on the newer data."


def test_approving_a_candidate_is_an_invalid_transition(registry: SqlRegistryStore) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.CANDIDATE))
    with pytest.raises(RegistryError) as excinfo:
        registry.approve("m_1", by="ops@telco")
    assert excinfo.value.code == "INVALID_TRANSITION"
    assert registry.get("m_1").status is ModelStatus.CANDIDATE


def test_promote_is_the_manual_override_and_records_the_note(registry: SqlRegistryStore) -> None:
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


def test_archive_retires_a_version(registry: SqlRegistryStore) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    registry.approve("m_1", by="ops@telco")
    assert registry.archive("m_1").status is ModelStatus.ARCHIVED
    assert registry.get_champion(USE_CASE) is None


def test_a_champion_is_not_archived_by_an_archive_that_expected_it_to_be_waiting(
    registry: SqlRegistryStore,
) -> None:
    """A reject that read `pending_approval` loses to an approval that landed since (DEC-869)."""
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    registry.approve("m_1", by="ops@telco")
    with pytest.raises(RegistryError) as excinfo:
        registry.archive("m_1", expected_status=ModelStatus.PENDING_APPROVAL)
    assert excinfo.value.code == "INVALID_TRANSITION"
    assert excinfo.value.model_id == "m_1"
    assert registry.get("m_1").status is ModelStatus.CHAMPION, "nothing was written"
    champion = registry.get_champion(USE_CASE)
    assert champion is not None and champion.model_id == "m_1"


def test_an_archive_whose_expected_status_holds_retires_the_version(registry: SqlRegistryStore) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    archived = registry.archive("m_1", expected_status=ModelStatus.PENDING_APPROVAL)
    assert archived.status is ModelStatus.ARCHIVED
    with pytest.raises(RegistryError) as excinfo:
        registry.archive("m_1", expected_status=ModelStatus.PENDING_APPROVAL)
    assert excinfo.value.code == "INVALID_TRANSITION", "an archived version is not waiting either"


def test_the_row_projection_round_trips(registry: SqlRegistryStore) -> None:
    version = make_version("m_1", 1)
    assert ModelVersionRow.from_contract(version).to_contract() == version
    assert ModelVersionRow.__tablename__ == "model_version"


@pytest.mark.parametrize("measured_against", [None, NO_CHAMPION_AT_DECISION, "m_champ"])
def test_the_row_projection_carries_the_compared_champion(measured_against: str | None) -> None:
    version = make_version(
        "m_1", 1, status=ModelStatus.PENDING_APPROVAL, measured_against_champion_id=measured_against
    )
    assert ModelVersionRow.from_contract(version).to_contract() == version


def test_the_use_case_and_version_pair_is_unique(registry: SqlRegistryStore, backend: str) -> None:
    registry.register(make_version("m_1", 1))
    with pytest.raises(IntegrityError) as excinfo:
        registry.register(make_version("m_2", 1))
    assert UNIQUE_VIOLATION[backend] in str(excinfo.value)


def test_two_threads_writing_concurrently_do_not_raise(registry: SqlRegistryStore) -> None:
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


@pytest.mark.parametrize("greater_is_better", [True, False])
@pytest.mark.parametrize(
    ("gain", "rule", "expected"),
    [
        (0.1, 0.0, True),
        (0.1, 1.0, True),
        (0.0, 0.0, True),
        (0.0, 1.0, False),
        (-0.1, 0.0, False),
        (-0.1, 1.0, False),
    ],
)
def test_should_promote_over_a_zero_champion_needs_a_strict_gain_unless_the_rule_is_zero(
    gain: float, rule: float, expected: bool, greater_is_better: bool
) -> None:
    """A 0.0 champion (DEC-035): a better candidate wins, an equal one only at 0 %, a worse one never."""
    champion = make_version("m_champ", 1, status=ModelStatus.CHAMPION, test_score=0.0)
    candidate = make_version("m_cand", 2, test_score=gain if greater_is_better else -gain)
    assert should_promote(candidate, champion, rule, greater_is_better=greater_is_better) is expected


# ---------------------------------------------------------------------------
# DEC-339: timestamps survive a backend whose clock is not UTC
# ---------------------------------------------------------------------------
KOLKATA: timezone = timezone(timedelta(hours=5, minutes=30))
"""A non-UTC offset to write with. +05:30 is not a whole number of hours, so a test that passed by
rounding, by truncating or by a lucky DST boundary would still fail here."""


def test_every_timestamp_column_declares_a_time_zone() -> None:
    """The model-level half of DEC-339, which holds whatever backend is in front of it.

    Without `sa_column=Column(..., DateTime(timezone=True))` SQLModel compiles `datetime` to
    `TIMESTAMP WITHOUT TIME ZONE` on Postgres, and the instant is silently replaced by the server's
    wall clock. This asserts the declaration itself, so removing it fails here even on a machine
    with no PostgreSQL at all.
    """
    columns = ModelVersionRow.__table__.columns
    stamps = ("created_at", "approved_at", "promoted_at")
    assert [columns[name].type.timezone for name in stamps] == [True, True, True]


def test_a_timestamp_round_trips_as_the_same_instant_in_utc(registry: SqlRegistryStore) -> None:
    """DEC-339, proved on whichever backend is running: same moment out, and wearing UTC.

    The version is registered with `created_at` at +05:30 and every read of it must come back as
    the same instant with a zero offset. On a `TIMESTAMP WITHOUT TIME ZONE` column this fails by
    the server's own UTC offset, which is why `tests/fixtures/postgres.py` documents pointing the
    suite at a server that is not set to UTC.
    """
    moment = datetime(2026, 9, 22, 17, 30, 0, tzinfo=KOLKATA)
    registry.register(make_version("m_tz", 1).model_copy(update={"created_at": moment}))

    for read in (registry.get("m_tz"), registry.list_versions(USE_CASE)[0]):
        assert read.created_at == moment
        assert read.created_at == datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
        assert read.created_at.utcoffset() == timedelta(0)
        assert read.created_at.hour == 12


def test_the_approval_timestamps_are_also_utc_aware(registry: SqlRegistryStore) -> None:
    """The nullable columns take the same route through `_utc`/`aware_utc` as `created_at` does."""
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    approved = registry.approve("m_1", by="ops@telco")
    for stamp in (approved.approved_at, approved.promoted_at):
        assert stamp is not None
        assert stamp.utcoffset() == timedelta(0)
    assert registry.get("m_1").approved_at == approved.approved_at


def test_an_already_utc_timestamp_is_unchanged(registry: SqlRegistryStore) -> None:
    """The guard against a vacuous pass: widening `aware_utc` must not move a value that was UTC."""
    moment = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    registry.register(make_version("m_utc", 1).model_copy(update={"created_at": moment}))
    assert registry.get("m_utc").created_at == moment
    assert registry.get("m_utc").created_at.isoformat() == "2026-09-22T12:00:00+00:00"
