"""`engine/runs.py`: the run directory, the job spec, and the one place a spec becomes work.

Two things are under test here and they are not the same thing.

The **move** is the smaller one: `create_run`, `update_run`, `update_stage` and `cancel_run` came
out of `api/routes/runs.py` unchanged, so the tests that matter are the ones that would notice if
they had not - the write order a poller depends on, the run-level state a stage transition implies,
and the fact that both documents are still importable under their old names from
`api.routes.runs` (DEC-327).

The **spec** is the larger one (DEC-324). `JobSpec` exists because a closure cannot cross a process
boundary, and the property that makes it worth having is that the local closure and the container
run *the same function on the same description*. That is asserted directly: `build_job_fn`'s closure
is shown to dispatch through `ENTRYPOINTS`, which is the table `scripts/run_job_entrypoint.py` also
goes through, and every member of `JobEntrypoint` is shown to be in it - so a third flow cannot be
half-added.

`fail_run` gets its own section because it is the only writer of an ending that no flow was there
to write, and because getting it wrong in the other direction - clobbering a stage-level failure
with a generic one - is worse than not writing it at all.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from api.routes import runs as runs_route
from engine.config import Catalog, ResolvedConfig, RunMode, get_catalog, resolve_config
from engine.contracts import (
    DatasetFingerprint,
    DatasetProfile,
    JobEntrypoint,
    JobSpec,
    RunError,
    RunRecord,
    RunState,
    RunStatus,
    Severity,
    StageKey,
    ValidationCheck,
    ValidationReport,
)
from engine.jobs import CancelToken, NullJobRunner
from engine.pipeline import STATUS_FILENAME, Pipeline
from engine.registry import LocalModelRegistry
from engine.runs import (
    CREATED_ARTEFACTS,
    ENTRYPOINT_FOR_MODE,
    ENTRYPOINTS,
    JOB_SPEC_FILENAME,
    LOCAL_BACKEND,
    RUN_FILENAME,
    SpecUpload,
    UploadInfo,
    build_job_fn,
    cancel_run,
    create_run,
    fail_run,
    job_spec_for,
    job_spec_key,
    read_job_spec,
    run_job_spec,
    update_run,
    update_stage,
    write_job_spec,
)
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now

USE_CASE = "targeted-advertisement"
PRIMARY_KEY = "customer_id"
TARGET = "converted_30d"
UPLOAD_ID = "u_20260922_aaaaaaaa"


# ---------------------------------------------------------------------------
# Fixtures: the smallest run directory that is a real one
# ---------------------------------------------------------------------------
class _Upload:
    """The four things `UploadInfo` asks for; the shape `api.schemas.UploadRecord` also has."""

    upload_id = UPLOAD_ID
    file_name = "history.csv"
    source_key = f"uploads/{UPLOAD_ID}/source.csv"
    file_format = "csv"


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


@pytest.fixture
def registry(tmp_path: Path) -> LocalModelRegistry:
    return LocalModelRegistry(tmp_path / "registry.db")


@pytest.fixture
def resolved(config_root: Path) -> ResolvedConfig:
    return resolve_config(USE_CASE, {}, root=config_root)


@pytest.fixture
def catalog(config_root: Path) -> Catalog:
    return get_catalog(config_root)


def make_profile() -> DatasetProfile:
    """A profile with one column; nothing here reads it except for the row count."""
    return DatasetProfile(
        upload_id=UPLOAD_ID,
        file_name="history.csv",
        file_size_bytes=1024,
        file_format="csv",
        delimiter=",",
        encoding="utf-8",
        row_count=2_000,
        column_count=1,
        columns=(),
        primary_key_candidates=(PRIMARY_KEY,),
        time_column_candidates=(),
        target_candidate=TARGET,
        preview_rows=(),
        missing_value_rate_pct=0.0,
        fingerprint=DatasetFingerprint(
            hash="0" * 16, algorithm="sha256", n_rows=2_000, columns=(PRIMARY_KEY,)
        ),
        profiled_at=utc_now(),
    )


def make_report() -> ValidationReport:
    return ValidationReport(
        upload_id=UPLOAD_ID,
        mode=RunMode.TRAIN,
        checks=(
            ValidationCheck(
                code="PII_DETECTED",
                severity=Severity.WARNING,
                message="One column looks like an email address.",
            ),
        ),
        error_count=0,
        warning_count=1,
        passed=True,
        validated_at=utc_now(),
    )


def make_run(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    resolved: ResolvedConfig,
    catalog: Catalog,
    *,
    mode: RunMode = RunMode.TRAIN,
    model_version_id: str | None = None,
) -> RunRecord:
    return create_run(
        storage,
        Pipeline(storage, registry, NullJobRunner()),
        resolved=resolved,
        catalog=catalog,
        upload=_Upload(),
        profile=make_profile(),
        report=make_report(),
        mode=mode,
        primary_key=PRIMARY_KEY,
        target=TARGET if mode is RunMode.TRAIN else None,
        model_choice=catalog.automl_choice.value,
        model_version_id=model_version_id,
    )


@pytest.fixture
def record(
    storage: LocalStorage, registry: LocalModelRegistry, resolved: ResolvedConfig, catalog: Catalog
) -> RunRecord:
    return make_run(storage, registry, resolved, catalog)


def status_of(storage: LocalStorage, run_id: str) -> RunStatus:
    return storage.read_model(run_key(run_id, STATUS_FILENAME), RunStatus)


# ---------------------------------------------------------------------------
# The move: what `api/routes/runs.py` used to hold
# ---------------------------------------------------------------------------
def test_create_run_writes_the_whole_directory_before_it_returns(
    storage: LocalStorage, record: RunRecord
) -> None:
    """A poller can land microseconds after the 202, so every created artefact exists already."""
    assert set(record.artefacts) == set(CREATED_ARTEFACTS)
    for name in CREATED_ARTEFACTS:
        assert storage.exists(run_key(record.run_id, name)), name
    assert record.state is RunState.PENDING
    assert record.row_count == 2_000


def test_the_job_spec_is_not_one_of_the_created_artefacts(record: RunRecord) -> None:
    """`CREATED_ARTEFACTS` means "what this run produced"; the spec is what it was handed (DEC-324)."""
    assert JOB_SPEC_FILENAME not in CREATED_ARTEFACTS
    assert JOB_SPEC_FILENAME not in record.artefacts


def test_update_run_is_the_only_writer_of_run_json_after_creation(
    storage: LocalStorage, record: RunRecord
) -> None:
    updated = update_run(storage, record.run_id, state=RunState.RUNNING, best_model="LightGBM")
    assert updated.state is RunState.RUNNING
    assert updated.best_model == "LightGBM"
    assert storage.read_model(run_key(record.run_id, RUN_FILENAME), RunRecord) == updated


def test_a_stage_transition_recomputes_the_run_state_and_the_progress(
    storage: LocalStorage, record: RunRecord
) -> None:
    running = update_stage(storage, record.run_id, StageKey.INGEST, state=RunState.RUNNING)
    assert running.state is RunState.RUNNING
    assert running.current_stage is StageKey.INGEST

    done = update_stage(storage, record.run_id, StageKey.INGEST, state=RunState.DONE, detail="2,000 rows")
    assert done.current_stage is None
    assert done.progress_pct == round(100 / len(done.stages))
    assert next(row for row in done.stages if row.key is StageKey.INGEST).detail == "2,000 rows"


def test_a_failed_stage_makes_the_run_failed(storage: LocalStorage, record: RunRecord) -> None:
    error = RunError(code="STAGE_FAILED", message="Preparing features failed.", stage=StageKey.PREPARE)
    status = update_stage(storage, record.run_id, StageKey.PREPARE, state=RunState.FAILED, error=error)
    assert status.state is RunState.FAILED
    assert next(row for row in status.stages if row.key is StageKey.PREPARE).error == error


def test_cancel_run_stops_every_unfinished_stage_and_leaves_the_finished_ones(
    storage: LocalStorage, record: RunRecord
) -> None:
    update_stage(storage, record.run_id, StageKey.INGEST, state=RunState.DONE)
    update_stage(storage, record.run_id, StageKey.VALIDATE, state=RunState.RUNNING)

    cancelled = cancel_run(storage, record.run_id)
    assert cancelled.state is RunState.CANCELLED
    assert cancelled.finished_at is not None
    by_key = {row.key: row for row in status_of(storage, record.run_id).stages}
    assert by_key[StageKey.INGEST].state is RunState.DONE
    assert by_key[StageKey.VALIDATE].state is RunState.CANCELLED
    assert by_key[StageKey.PREPARE].state is RunState.CANCELLED


@pytest.mark.parametrize(
    "name",
    [
        "create_run",
        "update_run",
        "update_stage",
        "cancel_run",
        "build_m2_job",
        "build_score_job",
        "build_train_job",
        "build_job_fn",
        "job_spec_for",
        "write_job_spec",
    ],
)
def test_every_moved_name_is_still_a_module_level_name_of_the_route(name: str) -> None:
    """A monkeypatch has to address the module the route looks the name up in (DEC-327)."""
    assert hasattr(runs_route, name)
    assert name in runs_route.__all__


# ---------------------------------------------------------------------------
# job_spec.json
# ---------------------------------------------------------------------------
def test_the_spec_describes_the_run_it_was_built_from(record: RunRecord) -> None:
    spec = job_spec_for(record, upload=_Upload(), client_id="acme")

    assert spec.job_id == spec.run_id == record.run_id
    assert spec.entrypoint is JobEntrypoint.TRAIN
    assert spec.mode is RunMode.TRAIN
    assert spec.use_case_id == record.use_case_id
    assert spec.upload_key == _Upload.source_key
    assert spec.upload_format == "csv"
    assert spec.primary_key == PRIMARY_KEY
    assert spec.target == TARGET
    assert spec.run_config_key == run_key(record.run_id, "run_config.json")
    assert spec.backend == LOCAL_BACKEND


def test_a_scoring_run_asks_for_the_score_entrypoint_and_carries_its_version(
    storage: LocalStorage, registry: LocalModelRegistry, resolved: ResolvedConfig, catalog: Catalog
) -> None:
    scored = make_run(storage, registry, resolved, catalog, mode=RunMode.SCORE, model_version_id="m_x_1")
    spec = job_spec_for(scored, upload=_Upload())

    assert spec.entrypoint is JobEntrypoint.SCORE
    assert spec.model_version_id == "m_x_1"
    assert spec.target is None


def test_the_tags_are_the_four_cost_allocation_tags(record: RunRecord) -> None:
    spec = job_spec_for(record, upload=_Upload(), client_id="acme")
    assert spec.tags == {
        "product": "marketing-ai",
        "use_case": record.use_case_id,
        "run_id": record.run_id,
        "client": "acme",
    }


def test_a_deployment_that_serves_no_client_carries_no_client_tag(record: RunRecord) -> None:
    """An empty tag value looks like an answer; an absent tag does not."""
    assert "client" not in job_spec_for(record, upload=_Upload(), client_id=None).tags
    assert "client" not in job_spec_for(record, upload=_Upload(), client_id="").tags


def test_the_spec_round_trips_through_the_store(storage: LocalStorage, record: RunRecord) -> None:
    spec = job_spec_for(record, upload=_Upload())
    key = write_job_spec(storage, spec)

    assert key == job_spec_key(record.run_id) == run_key(record.run_id, JOB_SPEC_FILENAME)
    assert read_job_spec(storage, key) == spec


def test_the_spec_is_written_beside_run_json(storage: LocalStorage, record: RunRecord) -> None:
    write_job_spec(storage, job_spec_for(record, upload=_Upload()))
    assert job_spec_key(record.run_id) in storage.list_keys(f"runs/{record.run_id}/")


def test_spec_upload_satisfies_upload_info() -> None:
    """The container's stand-in for `upload.json`, structurally (DEC-327)."""
    upload = SpecUpload(
        upload_id=UPLOAD_ID, file_name="history.csv", source_key="uploads/x/source.csv", file_format="csv"
    )
    assert isinstance(upload, UploadInfo)
    assert isinstance(_Upload(), UploadInfo)


# ---------------------------------------------------------------------------
# One description, two renderings
# ---------------------------------------------------------------------------
def test_every_entrypoint_has_a_body() -> None:
    """The table is the only place the mapping exists, so a third flow cannot be half-added."""
    assert set(ENTRYPOINTS) == set(JobEntrypoint)
    assert set(ENTRYPOINT_FOR_MODE) == set(RunMode)
    assert set(ENTRYPOINT_FOR_MODE.values()) == set(JobEntrypoint)


def test_the_closure_dispatches_through_the_entrypoint_table(
    storage: LocalStorage, registry: LocalModelRegistry, record: RunRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `build_job_fn` runs is what `scripts/run_job_entrypoint.py` runs: the same table entry."""
    seen: list[tuple[JobSpec, CancelToken]] = []

    def body(spec: JobSpec, cancel: CancelToken, _storage: Any, _registry: Any) -> None:
        seen.append((spec, cancel))

    monkeypatch.setattr("engine.runs.ENTRYPOINTS", {**ENTRYPOINTS, JobEntrypoint.TRAIN: body})

    spec = job_spec_for(record, upload=_Upload())
    token = CancelToken()
    build_job_fn(spec, storage=storage, registry=registry)(token)

    assert seen == [(spec, token)]


def test_run_job_spec_and_the_closure_are_the_same_call(
    storage: LocalStorage, registry: LocalModelRegistry, record: RunRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def body(spec: JobSpec, cancel: CancelToken, _storage: Any, _registry: Any) -> None:
        del cancel
        calls.append(spec.run_id)

    monkeypatch.setattr("engine.runs.ENTRYPOINTS", {**ENTRYPOINTS, JobEntrypoint.TRAIN: body})
    spec = job_spec_for(record, upload=_Upload())

    build_job_fn(spec, storage=storage, registry=registry)(CancelToken())
    run_job_spec(spec, CancelToken(), storage=storage, registry=registry)

    assert calls == [record.run_id, record.run_id]


def test_a_train_spec_runs_the_train_flow(
    storage: LocalStorage, registry: LocalModelRegistry, record: RunRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not `build_m2_job` any more: the API's training runs go to `Pipeline.run_train` (DEC-326)."""
    seen: list[RunMode] = []

    def run_train(self: Pipeline, ctx: Any) -> RunRecord:
        del self
        seen.append(ctx.mode)
        return record

    monkeypatch.setattr(Pipeline, "run_train", run_train)
    write_job_spec(storage, job_spec_for(record, upload=_Upload()))
    spec = read_job_spec(storage, job_spec_key(record.run_id))

    run_job_spec(spec, CancelToken(), storage=storage, registry=registry)
    assert seen == [RunMode.TRAIN]


def test_a_score_spec_runs_the_score_flow(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    resolved: ResolvedConfig,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scored = make_run(storage, registry, resolved, catalog, mode=RunMode.SCORE, model_version_id="m_x_1")
    seen: list[str | None] = []

    def run_score(self: Pipeline, ctx: Any) -> RunRecord:
        del self
        seen.append(ctx.model_version_id)
        return scored

    monkeypatch.setattr(Pipeline, "run_score", run_score)
    write_job_spec(storage, job_spec_for(scored, upload=_Upload()))
    spec = read_job_spec(storage, job_spec_key(scored.run_id))

    run_job_spec(spec, CancelToken(), storage=storage, registry=registry)
    assert seen == ["m_x_1"], "the version the request pinned is the version the job scores with"


def test_the_body_reads_its_inputs_back_out_of_the_store(
    storage: LocalStorage, registry: LocalModelRegistry, record: RunRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container has no request: the upload it needs is rebuilt from the spec and the record."""
    seen: list[Any] = []

    def run_train(self: Pipeline, ctx: Any) -> RunRecord:
        del self
        seen.append(ctx)
        return record

    monkeypatch.setattr(Pipeline, "run_train", run_train)
    spec = job_spec_for(record, upload=_Upload())
    run_job_spec(spec, CancelToken(), storage=storage, registry=registry)

    (ctx,) = seen
    assert ctx.run_id == record.run_id
    assert ctx.upload_key == _Upload.source_key
    # DEC-083 (ruling D1): StageContext carries the key as a list of its columns; what the
    # artefacts store is still the bare column name.
    assert ctx.primary_key == [PRIMARY_KEY]
    assert ctx.key == PRIMARY_KEY
    assert ctx.target == TARGET


# ---------------------------------------------------------------------------
# fail_run: the ending nothing else was there to write
# ---------------------------------------------------------------------------
def remote_failure() -> RunError:
    return RunError(code="JOB_FAILED_REMOTELY", message="The compute ended early.", stage=None)


def test_a_failure_from_outside_lands_on_the_running_stage(storage: LocalStorage, record: RunRecord) -> None:
    update_stage(storage, record.run_id, StageKey.INGEST, state=RunState.DONE)
    update_stage(storage, record.run_id, StageKey.VALIDATE, state=RunState.RUNNING)

    failed = fail_run(storage, record.run_id, remote_failure())

    assert failed is not None
    assert failed.state is RunState.FAILED
    assert failed.error == remote_failure()
    by_key = {row.key: row for row in status_of(storage, record.run_id).stages}
    assert by_key[StageKey.VALIDATE].state is RunState.FAILED
    assert by_key[StageKey.INGEST].state is RunState.DONE


def test_a_failure_before_anything_ran_lands_on_the_first_stage(
    storage: LocalStorage, record: RunRecord
) -> None:
    """ "It failed at some unspecified point" is not something a reader can act on."""
    failed = fail_run(storage, record.run_id, remote_failure())

    assert failed is not None
    status = status_of(storage, record.run_id)
    assert status.stages[0].state is RunState.FAILED
    assert status.stages[0].error is not None


def test_a_run_that_already_ended_is_left_exactly_as_it_is(storage: LocalStorage, record: RunRecord) -> None:
    """Whatever ended it knew more about it than this function does."""
    stage_error = RunError(code="PREPARE_FAILED", message="A column was empty.", stage=StageKey.PREPARE)
    update_stage(storage, record.run_id, StageKey.PREPARE, state=RunState.FAILED, error=stage_error)
    update_run(storage, record.run_id, state=RunState.FAILED, error=stage_error)
    before = status_of(storage, record.run_id)

    assert fail_run(storage, record.run_id, remote_failure()) is None
    assert status_of(storage, record.run_id) == before
    assert storage.read_model(run_key(record.run_id, RUN_FILENAME), RunRecord).error == stage_error


def test_a_cancelled_run_is_not_reopened_as_a_failure(storage: LocalStorage, record: RunRecord) -> None:
    cancel_run(storage, record.run_id)
    assert fail_run(storage, record.run_id, remote_failure()) is None
    assert status_of(storage, record.run_id).state is RunState.CANCELLED


def test_fail_run_stamps_the_moment_it_was_given(storage: LocalStorage, record: RunRecord) -> None:
    moment = utc_now() + timedelta(minutes=5)
    failed = fail_run(storage, record.run_id, remote_failure(), now=moment)

    assert failed is not None
    assert failed.finished_at == moment
