"""`engine.aws.s3_registry`: publishing is the one thing it adds, and policy is the thing it does not.

Two halves. The first proves that `register` moves a version's files out of the run directory and
into `models/<use case>/<version>/`, which is the whole reason this class exists (DEC-346): a run
directory has a lifecycle and a champion's predictor must not share it. The second proves that
everything else is the store's, unchanged - the champion swap, the stale-approval refusal, the
transitions - because a second implementation of the champion rule is the failure this class was
designed to avoid.

`LocalStorage` stands in for S3 here on purpose. `S3ModelRegistry` is written against the `Storage`
protocol and calls nothing S3-specific, so the copying it does is the same copying either way, and
the tests run in milliseconds without moto.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import create_engine

from engine.aws.s3_registry import ModelMirror, S3ModelRegistry
from engine.config import Metric
from engine.contracts import MODEL_DIRECTORY, ModelStatus, ModelVersion
from engine.registry import (
    ModelRegistry,
    RegistryError,
    SqlRegistryStore,
    create_registry_tables,
)
from engine.storage import LocalStorage, published_model_key, run_key

USE_CASE: str = "a-use-case"
RUN_ID: str = "r_20260922_0001"

PREDICTOR_MEMBERS: tuple[str, ...] = ("predictor.pkl", "models/LightGBM/model.pkl", "learner.pkl")
"""Three files at two depths, so a copy that flattened the tree would be caught."""


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


@pytest.fixture
def store(tmp_path: Path) -> SqlRegistryStore:
    engine = create_engine(f"sqlite:///{tmp_path / 'registry.db'}")
    create_registry_tables(engine)
    return SqlRegistryStore(engine)


@pytest.fixture
def registry(store: SqlRegistryStore, storage: LocalStorage) -> S3ModelRegistry:
    """Built exactly the way `engine.settings.build_registry` builds it."""
    return S3ModelRegistry(store, storage)


def write_run_artefacts(storage: LocalStorage, run_id: str = RUN_ID, *, drift: bool = True) -> None:
    """Everything a finished training run leaves behind that the registry points at."""
    storage.write_text(run_key(run_id, "schema.json"), '{"schema": true}')
    storage.write_text(run_key(run_id, "run_config.json"), '{"config": true}')
    if drift:
        storage.write_text(run_key(run_id, "drift_baseline.json"), '{"baseline": true}')
    for member in PREDICTOR_MEMBERS:
        storage.write_text(run_key(run_id, f"model/{member}"), f"bytes of {member}")


def make_version(
    model_id: str = "m_1",
    version: int = 1,
    *,
    run_id: str = RUN_ID,
    status: ModelStatus = ModelStatus.CANDIDATE,
    test_score: float = 0.80,
    drift: bool = True,
) -> ModelVersion:
    """A `ModelVersion` shaped the way `engine.stages.register` shapes one: keys into the run."""
    schema_key = run_key(run_id, "schema.json")
    run_config_key = run_key(run_id, "run_config.json")
    predictor_key = run_key(run_id, "model")
    drift_key = run_key(run_id, "drift_baseline.json") if drift else None
    artefacts = {
        "schema.json": schema_key,
        "run_config.json": run_config_key,
        MODEL_DIRECTORY: predictor_key,
    }
    if drift_key is not None:
        artefacts["drift_baseline.json"] = drift_key
    return ModelVersion(
        model_id=model_id,
        use_case_id=USE_CASE,
        version=version,
        run_id=run_id,
        created_at=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
        status=status,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=test_score,
        model_display_name="WeightedEnsemble_L2",
        schema_key=schema_key,
        run_config_key=run_config_key,
        predictor_key=predictor_key,
        drift_baseline_key=drift_key,
        artefact_keys=artefacts,
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )


# ---------------------------------------------------------------------------
# Publishing (DEC-346)
# ---------------------------------------------------------------------------
def test_it_satisfies_the_registry_protocol(registry: S3ModelRegistry) -> None:
    assert isinstance(registry, ModelRegistry)


def test_register_moves_every_key_under_the_published_prefix(
    registry: S3ModelRegistry, storage: LocalStorage
) -> None:
    write_run_artefacts(storage)
    stored = registry.register(make_version())

    assert stored.schema_key == published_model_key(USE_CASE, 1, "schema.json")
    assert stored.run_config_key == published_model_key(USE_CASE, 1, "run_config.json")
    assert stored.predictor_key == published_model_key(USE_CASE, 1, "model")
    assert stored.drift_baseline_key == published_model_key(USE_CASE, 1, "drift_baseline.json")
    assert not any(key.startswith("runs/") for key in stored.artefact_keys.values())


def test_the_published_layout_is_the_one_the_storage_helper_spells(
    registry: S3ModelRegistry, storage: LocalStorage
) -> None:
    """DEC-310: one spelling of `models/<use case>/<version>/`, and this class does not own it."""
    write_run_artefacts(storage)
    registry.register(make_version(version=7))
    assert storage.exists("models/a-use-case/7/schema.json")
    assert storage.exists("models/a-use-case/7/model/predictor.pkl")


def test_the_predictor_directory_is_copied_whole_and_keeps_its_shape(
    registry: S3ModelRegistry, storage: LocalStorage
) -> None:
    write_run_artefacts(storage)
    stored = registry.register(make_version())
    published = storage.list_keys(f"{stored.predictor_key}/")
    assert published == tuple(
        sorted(published_model_key(USE_CASE, 1, "model", *member.split("/")) for member in PREDICTOR_MEMBERS)
    )
    for member in PREDICTOR_MEMBERS:
        assert storage.read_text(published_model_key(USE_CASE, 1, "model", *member.split("/"))) == (
            f"bytes of {member}"
        )


def test_the_run_directory_is_left_exactly_as_it_was(
    registry: S3ModelRegistry, storage: LocalStorage
) -> None:
    """A copy, not a move: `run.json` still points at the run's own artefacts (DEC-346)."""
    write_run_artefacts(storage)
    before = storage.list_keys(f"runs/{RUN_ID}/")
    registry.register(make_version())
    assert storage.list_keys(f"runs/{RUN_ID}/") == before


def test_registering_an_already_published_version_copies_nothing(
    registry: S3ModelRegistry, storage: LocalStorage, tmp_path: Path
) -> None:
    """Idempotence, proved by taking the source away.

    A version read back out of the registry already names published keys. Registering it again -
    into a fresh, empty store over the same bucket, which is what a restored database looks like -
    must recognise that and copy nothing. The run directory is deleted first, so any attempt to
    copy would raise `MODEL_ARTEFACT_MISSING` instead of quietly succeeding.
    """
    write_run_artefacts(storage)
    first = registry.register(make_version())
    published = {key: storage.read_text(key) for key in storage.list_keys("models/")}
    for key in storage.list_keys(f"runs/{RUN_ID}/"):
        storage.delete(key)

    engine = create_engine(f"sqlite:///{tmp_path / 'restored.db'}")
    create_registry_tables(engine)
    again = S3ModelRegistry(SqlRegistryStore(engine), storage).register(first)

    assert again == first
    assert {key: storage.read_text(key) for key in storage.list_keys("models/")} == published


def test_a_missing_required_artefact_refuses_the_registration_outright(
    registry: S3ModelRegistry, storage: LocalStorage
) -> None:
    """A row whose predictor is not there would be crowned and then fail at the first scoring run."""
    write_run_artefacts(storage)
    storage.delete(run_key(RUN_ID, "schema.json"))
    with pytest.raises(RegistryError) as excinfo:
        registry.register(make_version())
    assert excinfo.value.code == "MODEL_ARTEFACT_MISSING"
    assert excinfo.value.model_id == "m_1"
    with pytest.raises(RegistryError) as absent:
        registry.get("m_1")
    assert absent.value.code == "MODEL_NOT_FOUND"


def test_an_empty_predictor_directory_is_a_missing_artefact_too(
    registry: S3ModelRegistry, storage: LocalStorage
) -> None:
    """`exists()` is always False for a directory, so "is it there" has to be asked differently."""
    write_run_artefacts(storage)
    for member in PREDICTOR_MEMBERS:
        storage.delete(run_key(RUN_ID, f"model/{member}"))
    with pytest.raises(RegistryError) as excinfo:
        registry.register(make_version())
    assert excinfo.value.code == "MODEL_ARTEFACT_MISSING"


def test_an_absent_drift_baseline_does_not_refuse_the_model(
    registry: S3ModelRegistry, storage: LocalStorage
) -> None:
    """The register stage sets the key whether or not a baseline was written (DEC-051, DEC-346)."""
    write_run_artefacts(storage, drift=False)
    stored = registry.register(make_version())
    assert stored.drift_baseline_key == run_key(RUN_ID, "drift_baseline.json")
    assert stored.schema_key == published_model_key(USE_CASE, 1, "schema.json")
    assert registry.get("m_1").model_id == "m_1"


# ---------------------------------------------------------------------------
# It owns no policy
# ---------------------------------------------------------------------------
def test_the_champion_swap_is_the_stores_own(registry: S3ModelRegistry, storage: LocalStorage) -> None:
    write_run_artefacts(storage)
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    first = registry.approve("m_1", by="ops@telco")
    assert first.status is ModelStatus.CHAMPION

    registry.register(make_version("m_2", 2, status=ModelStatus.PENDING_APPROVAL, test_score=0.86))
    second = registry.approve("m_2", by="ops@telco")
    assert second.previous_champion_id == "m_1"
    assert registry.get("m_1").status is ModelStatus.ARCHIVED
    assert registry.get_champion(USE_CASE) == second


def test_the_stale_approval_refusal_still_holds(registry: S3ModelRegistry, storage: LocalStorage) -> None:
    """DEC-047 arrives here unchanged, because it was never reimplemented (DEC-338)."""
    write_run_artefacts(storage)
    registry.register(
        make_version("m_a", 1, status=ModelStatus.PENDING_APPROVAL).model_copy(
            update={"measured_against_champion_id": "m_gone"}
        )
    )
    registry.register(make_version("m_c", 2, status=ModelStatus.CHAMPION))
    with pytest.raises(RegistryError) as excinfo:
        registry.approve("m_a", by="ops@telco")
    assert excinfo.value.code == "CHAMPION_CHANGED"


def test_the_reads_and_the_remaining_transitions_are_delegated(
    registry: S3ModelRegistry, store: SqlRegistryStore, storage: LocalStorage
) -> None:
    write_run_artefacts(storage)
    registry.register(make_version("m_1", 1))
    assert registry.next_version(USE_CASE) == 2
    assert [v.model_id for v in registry.list_versions(USE_CASE)] == ["m_1"]
    assert registry.promote("m_1", by="ops@telco", note="by hand").status is ModelStatus.CHAMPION
    assert registry.archive("m_1").status is ModelStatus.ARCHIVED
    assert store.get("m_1").status is ModelStatus.ARCHIVED


def test_archiving_leaves_the_published_files_where_they_are(
    registry: S3ModelRegistry, storage: LocalStorage
) -> None:
    """Deleting a model's files is a lifecycle rule's job, and it is not reversible by a mistake."""
    write_run_artefacts(storage)
    registry.register(make_version())
    registry.archive("m_1")
    assert storage.exists(published_model_key(USE_CASE, 1, "schema.json"))


# ---------------------------------------------------------------------------
# The optional mirror (DEC-348)
# ---------------------------------------------------------------------------
class RecordingMirror:
    """Counts what it was offered; `raising=True` makes every offer fail."""

    def __init__(self, *, raising: bool = False) -> None:
        self.seen: list[str] = []
        self.raising = raising

    def mirror(self, version: ModelVersion) -> str | None:
        self.seen.append(f"{version.model_id}:{version.status}")
        if self.raising:
            raise RuntimeError("SageMaker said no")
        return f"arn:{version.model_id}"


def test_a_mirror_is_offered_every_write(store: SqlRegistryStore, storage: LocalStorage) -> None:
    write_run_artefacts(storage)
    mirror = RecordingMirror()
    registry = S3ModelRegistry(store, storage, mirror=mirror)
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    registry.approve("m_1", by="ops@telco")
    registry.archive("m_1")
    assert mirror.seen == ["m_1:pending_approval", "m_1:champion", "m_1:archived"]
    assert isinstance(mirror, ModelMirror)


def test_an_archive_expecting_another_status_is_the_stores_refusal_and_mirrors_nothing(
    store: SqlRegistryStore, storage: LocalStorage
) -> None:
    """`expected_status` reaches the store, whose lock decides it (DEC-873)."""
    write_run_artefacts(storage)
    mirror = RecordingMirror()
    registry = S3ModelRegistry(store, storage, mirror=mirror)
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    registry.approve("m_1", by="ops@telco")
    with pytest.raises(RegistryError) as excinfo:
        registry.archive("m_1", expected_status=ModelStatus.PENDING_APPROVAL)
    assert excinfo.value.code == "INVALID_TRANSITION"
    assert store.get("m_1").status is ModelStatus.CHAMPION
    assert mirror.seen == ["m_1:pending_approval", "m_1:champion"], "a refused archive is not mirrored"


def test_a_failing_mirror_never_fails_a_registry_write(
    store: SqlRegistryStore, storage: LocalStorage
) -> None:
    """The registry is the record; the mirror is a copy. Losing the copy costs a console page."""
    write_run_artefacts(storage)
    registry = S3ModelRegistry(store, storage, mirror=RecordingMirror(raising=True))
    stored = registry.register(make_version())
    assert stored.model_id == "m_1"
    assert registry.get("m_1").status is ModelStatus.CANDIDATE


def test_without_a_mirror_nothing_is_offered_anywhere(
    registry: S3ModelRegistry, storage: LocalStorage
) -> None:
    """The default. `build_registry` passes no mirror, so a deployment gets none by construction."""
    write_run_artefacts(storage)
    assert registry.register(make_version()).model_id == "m_1"
