"""Creating a run, describing the job that produces it, and running that description.

Design section 5.3 always gave run creation its own module; `api/routes/runs.py` said in its own
docstring that `create_run`, `update_run`, `cancel_run` and the job bodies "move unchanged when it
lands". This is that module, and the move is the smaller half of what it is for.

The larger half is `engine.contracts.JobSpec`. `JobRunner.submit(job_id, fn)` takes a Python
callable, which is exactly right for a thread pool and impossible for a container: a closure cannot
cross a process boundary. Rather than change a protocol four test fakes implement, the *work* gets a
declarative description and the callable becomes something derived from it (DEC-324):

* `job_spec_for` builds that description out of the run record the request has just written;
* `write_job_spec` puts it in the artefact store beside `run.json`, where anything that can read the
  store can find it;
* `build_job_fn` turns it into the closure `ThreadJobRunner` runs *in this process*;
* `run_job_spec` is what that closure calls - and it is also what `scripts/run_job_entrypoint.py`
  calls inside a SageMaker container, having read the same document back out of the same store.

So a local run and a remote run are two renderings of one description, and there is no second
definition of the work that could drift from the first. `ENTRYPOINTS` is the table that says which
flow an entrypoint means, and it is the only place that mapping exists.

`build_train_job` is new here (DEC-326). `POST /runs` has been submitting `build_m2_job` for every
training run since M2 - the stub that runs ingest and validate and then stops at `prepare` with a
named error - while `Pipeline.run_train` has been complete and tested for just as long. The training
half of this product could not succeed through its own API. `ENTRYPOINTS` now routes a training job
to `build_train_job`, which is `build_score_job` with `run_train` in it; `build_m2_job` stays
exported because it is still the honest way to stand a job body in for a test.

Nothing here imports `api`: the container runs this module and has no FastAPI in its image. The two
pieces of `upload.json` a job body needs arrive through the `UploadInfo` protocol, which
`api.schemas.UploadRecord` satisfies on the request thread and `SpecUpload` satisfies in the
container (DEC-327).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, TypeAlias, runtime_checkable

from engine import __version__
from engine.config import Catalog, ResolvedConfig, RunMode, sole_key
from engine.contracts import (
    DatasetProfile,
    JobEntrypoint,
    JobSpec,
    RunError,
    RunRecord,
    RunState,
    RunStatus,
    StageKey,
    StageStatus,
    ValidationReport,
)
from engine.jobs import CancelToken, JobCancelledError, JobFn, JobRunner, NullJobRunner
from engine.pipeline import STATUS_FILENAME, Pipeline, StageContext
from engine.registry import ModelRegistry
from engine.stages import ingest, validate
from engine.storage import Storage, run_key
from engine.utils.ids import new_run_id
from engine.utils.logging import bind_log_context, get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from datetime import datetime

__all__ = [
    "CREATED_ARTEFACTS",
    "ENTRYPOINTS",
    "ENTRYPOINT_FOR_MODE",
    "JOB_SPEC_FILENAME",
    "LOCAL_BACKEND",
    "PREPARE_NOT_IMPLEMENTED_MESSAGE",
    "PROFILE_FILENAME",
    "RUN_CONFIG_FILENAME",
    "RUN_FILENAME",
    "RUN_MANIFEST_FILENAME",
    "STAGE_NOT_IMPLEMENTED",
    "STATUS_FILENAME",
    "VALIDATION_FILENAME",
    "DatasetLineage",
    "JobBody",
    "SpecUpload",
    "UploadInfo",
    "build_job_fn",
    "build_m2_job",
    "build_score_job",
    "build_train_job",
    "cancel_run",
    "create_run",
    "fail_run",
    "job_spec_for",
    "job_spec_key",
    "read_job_spec",
    "run_job_spec",
    "update_run",
    "update_stage",
    "write_job_spec",
]

_LOGGER = get_logger(__name__)

RUN_FILENAME: Final[str] = "run.json"
RUN_CONFIG_FILENAME: Final[str] = "run_config.json"
PROFILE_FILENAME: Final[str] = "profile.json"
VALIDATION_FILENAME: Final[str] = "validation.json"
JOB_SPEC_FILENAME: Final[str] = "job_spec.json"
RUN_MANIFEST_FILENAME: Final[str] = "run_manifest.json"
"""Written by the pipeline (DEC-067); named here so the artefact route and the runner agree on it."""

CREATED_ARTEFACTS: Final[tuple[str, ...]] = (
    RUN_FILENAME,
    STATUS_FILENAME,
    RUN_CONFIG_FILENAME,
    PROFILE_FILENAME,
    VALIDATION_FILENAME,
)
"""What `RunRecord.artefacts` maps at creation; the pipeline only ever adds to it.

`job_spec.json` is deliberately absent, exactly as it is absent from `TRAIN_ARTEFACTS` and
`SCORE_ARTEFACTS`: those sets mean "what this run produced", and the spec is what the run was
*handed*. It sits in the same directory because that is where anything about a run belongs, not
because it is an output of one (DEC-324).
"""

STAGE_NOT_IMPLEMENTED: Final[str] = "STAGE_NOT_IMPLEMENTED"
PREPARE_NOT_IMPLEMENTED_MESSAGE: Final[str] = "Preparing features is not built yet."

LOCAL_BACKEND: Final[str] = "local-thread"
"""`JobSpec.backend` before any runner has claimed the job; the value the contract defaults to."""

PRODUCT_TAG: Final[str] = "marketing-ai"
"""The `product` cost-allocation tag every job carries. The same word as the default job prefix."""

ENTRYPOINT_FOR_MODE: Final[Mapping[RunMode, JobEntrypoint]] = MappingProxyType(
    {RunMode.TRAIN: JobEntrypoint.TRAIN, RunMode.SCORE: JobEntrypoint.SCORE}
)
"""Which flow a run of each mode asks for. One mapping, so a third mode cannot be half-added."""

_FINISHED: Final[frozenset[RunState]] = frozenset({RunState.DONE, RunState.SKIPPED})
_UNFINISHED: Final[frozenset[RunState]] = frozenset({RunState.PENDING, RunState.RUNNING})
_TERMINAL: Final[frozenset[RunState]] = frozenset({RunState.DONE, RunState.FAILED, RunState.CANCELLED})


@runtime_checkable
class UploadInfo(Protocol):
    """The four things a run needs from an upload, as a shape rather than as a class.

    `api.schemas.UploadRecord` is an API model and `engine/` may not import `api/`: the container
    image that runs `run_job_spec` has no FastAPI in it at all. A structural protocol lets the
    request thread pass the real record and lets the container pass `SpecUpload`, built from the
    job spec, with neither side knowing about the other (DEC-327).
    """

    @property
    def upload_id(self) -> str: ...

    @property
    def file_name(self) -> str: ...

    @property
    def source_key(self) -> str: ...

    @property
    def file_format(self) -> Literal["csv", "parquet"]: ...


@dataclass(frozen=True, slots=True)
class SpecUpload:
    """`UploadInfo` reassembled from a `JobSpec` and the run record, for a process with no request.

    The spec carries where the bytes are and what they are; the run record carries which upload
    they came from and what the user called the file. Between them they are everything a job body
    reads from `upload.json`, which is why the container never has to read `upload.json` at all.
    """

    upload_id: str
    file_name: str
    source_key: str
    file_format: Literal["csv", "parquet"]


@dataclass(frozen=True, slots=True)
class DatasetLineage:
    """What a run read from a built dataset carries that a run read from an upload does not.

    Phase 2's dataset and Phase 4a's job path meet here. The bytes, the format and the display name
    reach `create_run` through `UploadInfo` like any upload's do - a dataset differs from an upload
    in provenance, not in what a run needs from it (DEC-107) - so the job spec, the thread runner and
    the SageMaker container all run a dataset with no code of their own. This is the provenance:
    which dataset, whose, and which exact build, so `run.json` can say so and `upload_id` can stay
    honestly null.
    """

    dataset_id: str
    client_id: str | None
    fingerprint: str


# ---------------------------------------------------------------------------
# The run directory (design section 5.3)
# ---------------------------------------------------------------------------
def create_run(
    storage: Storage,
    pipeline: Pipeline,
    *,
    resolved: ResolvedConfig,
    catalog: Catalog,
    upload: UploadInfo,
    dataset: DatasetLineage | None = None,
    profile: DatasetProfile,
    report: ValidationReport,
    mode: RunMode,
    primary_key: str,
    target: str | None,
    model_choice: str,
    model_version_id: str | None,
    now: datetime | None = None,
) -> RunRecord:
    """Write the whole run directory, in this order, then return. Nothing is submitted here.

    `status.json` is written before `run.json` and both before the caller submits, so a poller can
    never observe a run that exists but has nothing to show.
    """
    moment = now or utc_now()
    run_id = new_run_id(moment)
    config = resolved.config
    artefacts = {name: run_key(run_id, name) for name in CREATED_ARTEFACTS}
    storage.write_model(artefacts[RUN_CONFIG_FILENAME], resolved)
    storage.write_model(artefacts[PROFILE_FILENAME], profile)
    storage.write_model(artefacts[VALIDATION_FILENAME], report.model_copy(update={"run_id": run_id}))
    storage.write_model(artefacts[STATUS_FILENAME], pipeline.initial_status(run_id, mode))
    metric = config.model_search.metric
    record = RunRecord(
        run_id=run_id,
        use_case_id=config.id,
        use_case_name=config.name,
        mode=mode,
        state=RunState.PENDING,
        created_at=moment,
        started_at=None,
        finished_at=None,
        # Exactly one of upload_id and dataset_id is set (RunRecord's own contract). For a dataset
        # run, `upload` is the dataset seen through `UploadInfo`, so its id is not an upload's id.
        upload_id=upload.upload_id if dataset is None else None,
        dataset_id=dataset.dataset_id if dataset is not None else None,
        client_id=dataset.client_id if dataset is not None else None,
        dataset_fingerprint=dataset.fingerprint if dataset is not None else None,
        file_name=upload.file_name,
        row_count=profile.row_count,
        primary_key=primary_key,
        target=target,
        problem_type=config.problem_type,
        model_choice=model_choice,
        model_version_id=model_version_id,
        best_model=None,
        headline_metric=metric,
        headline_metric_label=catalog.metric_label(metric),
        headline_score=None,
        champion=False,
        beat_previous_champion=False,
        overrides=dict(resolved.overrides_applied),
        artefacts=artefacts,
        error=None,
        engine_version=__version__,
    )
    storage.write_model(artefacts[RUN_FILENAME], record)
    return record


def update_run(storage: Storage, run_id: str, **fields: Any) -> RunRecord:
    """Read, `model_copy` and write `run.json`; the only writer of that document after creation."""
    key = run_key(run_id, RUN_FILENAME)
    record = storage.read_model(key, RunRecord).model_copy(update=fields)
    storage.write_model(key, record)
    return record


def update_stage(
    storage: Storage,
    run_id: str,
    stage: StageKey,
    *,
    state: RunState,
    detail: str | None = None,
    error: RunError | None = None,
    now: datetime | None = None,
) -> RunStatus:
    """Move one stage, recompute the run-level state and progress, and rewrite `status.json` in full."""
    moment = now or utc_now()
    key = run_key(run_id, STATUS_FILENAME)
    status = storage.read_model(key, RunStatus)
    stages = tuple(
        _moved(row, moment, state=state, detail=detail, error=error) if row.key is stage else row
        for row in status.stages
    )
    updated = status.model_copy(
        update={
            "stages": stages,
            "state": _run_state(stages),
            "current_stage": stage if state is RunState.RUNNING else None,
            "progress_pct": _progress(stages),
            "updated_at": moment,
        }
    )
    storage.write_model(key, updated)
    return updated


def cancel_run(storage: Storage, run_id: str, *, now: datetime | None = None) -> RunRecord:
    """Rewrite both documents as cancelled: every unfinished stage stops, and the record is terminal."""
    moment = now or utc_now()
    key = run_key(run_id, STATUS_FILENAME)
    status = storage.read_model(key, RunStatus)
    stages = tuple(
        (
            row.model_copy(update={"state": RunState.CANCELLED, "ended_at": moment})
            if row.state in _UNFINISHED
            else row
        )
        for row in status.stages
    )
    storage.write_model(
        key,
        status.model_copy(
            update={
                "stages": stages,
                "state": RunState.CANCELLED,
                "current_stage": None,
                "progress_pct": _progress(stages),
                "updated_at": moment,
            }
        ),
    )
    return update_run(storage, run_id, state=RunState.CANCELLED, finished_at=moment)


def fail_run(
    storage: Storage, run_id: str, error: RunError, *, now: datetime | None = None
) -> RunRecord | None:
    """Record a failure that happened where no stage could record it; `None` if the run already ended.

    Every failure *inside* a flow is written by the flow, on the stage it happened at, with the
    detail line that stage had earned. This is for the failures that happen where no flow is
    running: a container that never started, an instance that was reclaimed, a job the console
    stopped. Nothing in this process was there to write the ending, so `status.json` would otherwise
    say "running" for ever (DEC-325).

    The failure is attached to the stage that was running when the lights went out, or to the first
    stage that never got to run, because "it failed at some unspecified point" is not something a
    reader can act on. A run that already reached a terminal state is left exactly as it is and
    `None` comes back: whatever ended it knew more about it than this function does.
    """
    moment = now or utc_now()
    status = storage.read_model(run_key(run_id, STATUS_FILENAME), RunStatus)
    if status.state in _TERMINAL:
        return None
    stage = _stage_to_blame(status)
    if stage is not None:
        update_stage(storage, run_id, stage, state=RunState.FAILED, error=error, now=moment)
    return update_run(storage, run_id, state=RunState.FAILED, finished_at=moment, error=error)


def _stage_to_blame(status: RunStatus) -> StageKey | None:
    """The stage a failure from outside the flow belongs on: the running one, else the next pending."""
    for row in status.stages:
        if row.state is RunState.RUNNING:
            return row.key
    for row in status.stages:
        if row.state is RunState.PENDING:
            return row.key
    return None


# ---------------------------------------------------------------------------
# job_spec.json: the declarative half of a job (DEC-324)
# ---------------------------------------------------------------------------
def job_spec_key(run_id: str) -> str:
    """Where a run's `job_spec.json` lives: beside its `run.json`, in the same directory."""
    return run_key(run_id, JOB_SPEC_FILENAME)


def job_spec_for(
    record: RunRecord,
    *,
    upload: UploadInfo,
    client_id: str | None = None,
    backend: str = LOCAL_BACKEND,
    now: datetime | None = None,
) -> JobSpec:
    """The `JobSpec` for a run that has just been created.

    Everything here is read off the run record and the upload, because those two documents are what
    a run *is*. The job id is the run id: this product has one job per run, and giving the two
    different names would only create a second thing to correlate in a log.

    The tags are the cost-allocation tags the job carries into AWS. `client` is present only when
    this deployment serves one, because an empty tag value is worse than an absent tag - it looks
    like an answer.
    """
    tags = {
        "product": PRODUCT_TAG,
        "use_case": record.use_case_id,
        "run_id": record.run_id,
        **({"client": client_id} if client_id else {}),
    }
    return JobSpec(
        job_id=record.run_id,
        run_id=record.run_id,
        entrypoint=ENTRYPOINT_FOR_MODE[record.mode],
        mode=record.mode,
        use_case_id=record.use_case_id,
        run_config_key=run_key(record.run_id, RUN_CONFIG_FILENAME),
        upload_key=upload.source_key,
        upload_format=upload.file_format,
        primary_key=sole_key(record.primary_key, what="A run"),
        target=record.target,
        model_version_id=record.model_version_id,
        engine_version=__version__,
        created_at=now or utc_now(),
        backend=backend,
        tags=tags,
    )


def write_job_spec(storage: Storage, spec: JobSpec) -> str:
    """Store `spec` beside the run's other documents and return the key it was written under."""
    key = job_spec_key(spec.run_id)
    storage.write_model(key, spec)
    return key


def read_job_spec(storage: Storage, key: str) -> JobSpec:
    """The `JobSpec` stored at `key`.

    Takes a key rather than a run id because the container is *handed* one, in an environment
    variable, and must not have to derive it: the runner that launched the job is the only thing
    that knows where it put the document, and one string in the environment is the whole contract
    between them (DEC-328).
    """
    return storage.read_model(key, JobSpec)


# ---------------------------------------------------------------------------
# The imperative half: one description, two renderings
# ---------------------------------------------------------------------------
JobBody: TypeAlias = Callable[[JobSpec, CancelToken, Storage, ModelRegistry], None]
"""What an entrypoint does: run the spec's flow, here, now, on this process's services."""


def _train_body(spec: JobSpec, cancel: CancelToken, storage: Storage, registry: ModelRegistry) -> None:
    """The train entrypoint: `build_train_job`, with its arguments read back out of the store."""
    record, resolved, upload = _inputs(spec, storage)
    build_train_job(storage, registry, NullJobRunner(), resolved=resolved, record=record, upload=upload)(
        cancel
    )


def _score_body(spec: JobSpec, cancel: CancelToken, storage: Storage, registry: ModelRegistry) -> None:
    """The score entrypoint: `build_score_job`, with its arguments read back out of the store."""
    record, resolved, upload = _inputs(spec, storage)
    build_score_job(storage, registry, NullJobRunner(), resolved=resolved, record=record, upload=upload)(
        cancel
    )


ENTRYPOINTS: Final[Mapping[JobEntrypoint, JobBody]] = MappingProxyType(
    {JobEntrypoint.TRAIN: _train_body, JobEntrypoint.SCORE: _score_body}
)
"""Entrypoint -> the flow it names. The one place a `JobSpec` becomes work.

Both bodies build a `NullJobRunner`: a job body is already inside the job, and a runner it could
submit to would be a recursion rather than a feature (see `engine.jobs.NullJobRunner`).
"""


def _inputs(spec: JobSpec, storage: Storage) -> tuple[RunRecord, ResolvedConfig, SpecUpload]:
    """The three documents a job body takes, read back from the store the spec points into."""
    record = storage.read_model(run_key(spec.run_id, RUN_FILENAME), RunRecord)
    resolved = storage.read_model(spec.run_config_key, ResolvedConfig)
    upload = SpecUpload(
        # A dataset run records `upload_id=None` and its `dataset_id` instead; the same fallback
        # `engine.pipeline` uses when it names the source a profile was taken from.
        upload_id=record.upload_id or record.dataset_id or record.run_id,
        file_name=record.file_name,
        source_key=spec.upload_key,
        file_format=spec.upload_format,
    )
    return record, resolved, upload


def run_job_spec(spec: JobSpec, cancel: CancelToken, *, storage: Storage, registry: ModelRegistry) -> None:
    """Run the work `spec` describes on this process's services, and let every failure propagate.

    The one function both renderings end at: `build_job_fn`'s closure calls it on a thread of this
    process, and `scripts/run_job_entrypoint.py` calls it inside a SageMaker container. Whoever is
    watching - a `ThreadJobRunner` or the SageMaker control plane - learns the outcome the same way,
    from an exception or its absence.

    The log context is bound *here*, inside the body, because a `ContextVar` set around a `submit`
    call is gone by the time the worker thread runs it (DEC-384).
    """
    with bind_log_context(run_id=spec.run_id):
        ENTRYPOINTS[spec.entrypoint](spec, cancel, storage, registry)


def build_job_fn(spec: JobSpec, *, storage: Storage, registry: ModelRegistry) -> JobFn:
    """The `JobFn` a `JobRunner` takes, derived from `spec`.

    This is the bridge DEC-324 is about. `ThreadJobRunner` gets a closure it can call on one of its
    threads; `SageMakerJobRunner` is handed the same closure, ignores it, and ships the spec's
    storage key to a container instead - and the two do the same thing, because the closure has
    nothing in it that the spec does not say.
    """

    def job(cancel: CancelToken) -> None:
        run_job_spec(spec, cancel, storage=storage, registry=registry)

    return job


def build_train_job(
    storage: Storage,
    registry: ModelRegistry,
    jobs: JobRunner,
    *,
    resolved: ResolvedConfig,
    record: RunRecord,
    upload: UploadInfo,
) -> JobFn:
    """The train flow of plan section 6.1, off the request thread: `Pipeline.run_train` and nothing else.

    `build_score_job`'s twin, and new in Phase 4a (DEC-326). Since M2 the API has submitted
    `build_m2_job` for a training run - two real stages and then an honest stop at `prepare` -
    while `Pipeline.run_train` sat complete and tested behind it, reachable only from a test. A
    training run started from the product's own API could not finish. It can now.

    Everything `build_score_job` says applies unchanged: the pipeline owns both documents from here
    on, records a failure or a cancellation on the stage it happened at, and writes
    `run_manifest.json` whatever the outcome - so this body adds nothing to either path, and a
    `JobCancelledError` is left to propagate rather than being flattened into a blanket cancellation.
    """
    pipeline = Pipeline(storage, registry, jobs)

    def job(cancel: CancelToken) -> None:
        pipeline.run_train(
            StageContext(
                run_id=record.run_id,
                mode=RunMode.TRAIN,
                config=resolved.config,
                resolved=resolved,
                storage=storage,
                registry=registry,
                cancel=cancel,
                primary_key=sole_key(record.primary_key, what="A run"),
                target=record.target,
                upload_key=upload.source_key,
                model_version_id=record.model_version_id,
            )
        )

    return job


def build_score_job(
    storage: Storage,
    registry: ModelRegistry,
    jobs: JobRunner,
    *,
    resolved: ResolvedConfig,
    record: RunRecord,
    upload: UploadInfo,
) -> JobFn:
    """The score flow of plan section 6.2, off the request thread: `Pipeline.run_score` and nothing else.

    The pipeline owns both documents from here on. It rewrites `status.json` at every stage
    transition, records a failure or a cancellation *on the stage it happened at* with the detail
    line that stage had earned, and writes `run_manifest.json` whatever the outcome - so this body
    adds nothing to either path. In particular a `JobCancelledError` is left to propagate: the
    runner reads it as "cancelled", and calling `cancel_run` here as the M2 body does would
    overwrite the stage the pipeline stopped at with a blanket cancellation.

    The model version is the one the request resolved and pinned on `run.json`
    (`record.model_version_id`), never "the champion" again. The upload was validated against that
    version's schema, so that version is the one that must score it.
    """
    pipeline = Pipeline(storage, registry, jobs)

    def job(cancel: CancelToken) -> None:
        pipeline.run_score(
            StageContext(
                run_id=record.run_id,
                mode=RunMode.SCORE,
                config=resolved.config,
                resolved=resolved,
                storage=storage,
                registry=registry,
                cancel=cancel,
                primary_key=sole_key(record.primary_key, what="A run"),
                target=record.target,
                upload_key=upload.source_key,
                model_version_id=record.model_version_id,
            )
        )

    return job


def build_m2_job(
    storage: Storage,
    *,
    run_id: str,
    profile: DatasetProfile,
    report: ValidationReport,
    mode: RunMode,
) -> JobFn:
    """The M2 job body (DEC-060): ingest and validate for real, then an honest stop at `prepare`.

    Both stages have already done their work on the request thread - the job replays them onto the
    status document so the Running screen shows real detail lines - and `prepare` fails with a named
    error instead of an unhandled `NotImplementedError`.

    No route submits this any more (DEC-326). It stays because it is still the only honest way to
    stand a job body in for a test of the *API*: it moves a run through the real status machine to a
    real terminal state in milliseconds, without AutoGluon and without a real frame.
    """
    checked = StageKey.VALIDATE if mode is RunMode.TRAIN else StageKey.VALIDATE_AGAINST_SCHEMA

    def job(cancel: CancelToken) -> None:
        try:
            update_run(storage, run_id, state=RunState.RUNNING, started_at=utc_now())
            update_stage(storage, run_id, StageKey.INGEST, state=RunState.RUNNING)
            cancel.raise_if_cancelled()
            update_stage(
                storage, run_id, StageKey.INGEST, state=RunState.DONE, detail=ingest.ingest_detail(profile)
            )
            update_stage(storage, run_id, checked, state=RunState.RUNNING)
            cancel.raise_if_cancelled()
            update_stage(
                storage, run_id, checked, state=RunState.DONE, detail=validate.validation_detail(report)
            )
            cancel.raise_if_cancelled()
            failure = RunError(
                code=STAGE_NOT_IMPLEMENTED,
                message=PREPARE_NOT_IMPLEMENTED_MESSAGE,
                stage=StageKey.PREPARE,
            )
            update_stage(storage, run_id, StageKey.PREPARE, state=RunState.FAILED, error=failure)
            update_run(storage, run_id, state=RunState.FAILED, finished_at=utc_now(), error=failure)
        except JobCancelledError:
            cancel_run(storage, run_id)
            raise

    return job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _moved(
    row: StageStatus,
    moment: datetime,
    *,
    state: RunState,
    detail: str | None,
    error: RunError | None,
) -> StageStatus:
    """One stage row after a transition; `started_at` is kept once set so durations stay honest."""
    started = moment if state is RunState.RUNNING else row.started_at
    ended = None if state in _UNFINISHED else moment
    changes: dict[str, Any] = {
        "state": state,
        "started_at": started,
        "ended_at": ended,
        "duration_seconds": None if started is None or ended is None else (ended - started).total_seconds(),
    }
    if detail is not None:
        changes["detail"] = detail
    if error is not None:
        changes["error"] = error
    return row.model_copy(update=changes)


def _run_state(stages: tuple[StageStatus, ...]) -> RunState:
    """The run-level state implied by its stages: failure wins, then completion, then progress."""
    states = {row.state for row in stages}
    if RunState.FAILED in states:
        return RunState.FAILED
    if RunState.CANCELLED in states:
        return RunState.CANCELLED
    if states <= _FINISHED:
        return RunState.DONE
    if states & {RunState.RUNNING, RunState.DONE}:
        return RunState.RUNNING
    return RunState.PENDING


def _progress(stages: tuple[StageStatus, ...]) -> int:
    """Done stages over total, as whole percent (design section 5.4)."""
    if not stages:
        return 0
    return round(100 * sum(1 for row in stages if row.state in _FINISHED) / len(stages))
