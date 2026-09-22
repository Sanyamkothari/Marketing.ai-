"""FastAPI dependencies: the config root, the deployment settings and the three process-wide services.

Every dependency is exposed twice: as a plain function (so tests and `app.dependency_overrides` can
address it) and as an `Annotated[T, Depends(fn)]` alias, because a `Depends(...)` default argument is
a function call in a default and ruff's B008 forbids it. The services are built on first use, so
importing `api.main` never touches the filesystem or any AWS client.

Phase 4a adds one dependency, `get_settings`, and changes nothing else about the shape of this file.
The three service providers keep their existing bodies - `create_app(data_dir=…)` still wins, the
same `_LOCK` still guards construction, the same `app.state` slot still caches the result - and only
fall through to `engine.settings`' factories when no explicit directory was given. That ordering is
what keeps Phase 1's behaviour intact: a test that passes `data_dir=` gets `LocalStorage` and a
laptop with no environment at all gets exactly what `default_storage()` used to give it.

The construction itself lives in `engine/settings.py` rather than here, because a SageMaker container
has no `api/deps.py` and must build the same objects from the same description. This file remains the
only place the *API* constructs a service; `build_storage`/`build_registry` are the only place
anything decides *what* to construct (DEC-308).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

from engine.config import config_root
from engine.jobs import JobRunner, ThreadJobRunner
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry, ModelRegistry
from engine.settings import Settings, build_registry, build_storage, load_settings
from engine.storage import LocalStorage, Storage

if TYPE_CHECKING:  # imported lazily below so the local path never loads a driver or a table module
    from engine.aws.run_index import RunIndex

from engine.aws.metrics import MetricSink, metric_sink_for

_LOCK: threading.Lock = threading.Lock()


def _state_path(request: Request, name: str) -> Path | None:
    """A `Path` attribute set on `app.state` by `create_app`, or `None` when it was not given."""
    value = getattr(request.app.state, name, None)
    return None if value is None else Path(str(value))


def get_settings(request: Request) -> Settings:
    """The deployment this process serves.

    `create_app(settings=…)` wins; otherwise one is loaded from the environment (and, when
    `MARKETING_AI_SETTINGS_SOURCE=aws`, from SSM Parameter Store and Secrets Manager) on first use
    and cached on `app.state`. Loading is deferred rather than done at import so that importing
    `api.main` stays free of I/O, exactly as it is for the other providers.
    """
    state = request.app.state
    with _LOCK:
        existing: Settings | None = getattr(state, "settings", None)
        if existing is None:
            existing = load_settings()
            state.settings = existing
    return existing


def get_config_root(request: Request) -> Path:
    """The configuration directory this app serves.

    `create_app(config_root=…)` wins, then `$MARKETING_AI_CONFIG_DIR`, then the checkout's `configs/`.
    """
    explicit = _state_path(request, "config_root")
    if explicit is None:
        explicit = get_settings(request).config_dir
    return config_root(explicit)


def get_storage(request: Request) -> Storage:
    """The artefact store.

    `create_app(data_dir=…)` still wins and still means the local filesystem: it is how every test in
    the suite points the app at a `tmp_path`, and a deployment setting must not be able to redirect
    it somewhere else behind the test's back. With no explicit directory, the settings decide.
    """
    state = request.app.state
    cached: Storage | None = getattr(state, "storage", None)
    if cached is not None:
        return cached
    data_dir = _state_path(request, "data_dir")
    # Built BEFORE the lock is taken: `_LOCK` is not reentrant and `get_settings` takes it itself,
    # so constructing inside the critical section would deadlock the first request.
    fresh: Storage = LocalStorage(data_dir) if data_dir is not None else build_storage(get_settings(request))
    with _LOCK:
        existing: Storage | None = getattr(state, "storage", None)
        if existing is None:
            state.storage = fresh
            return fresh
    return existing


def get_registry(request: Request) -> ModelRegistry:
    """The model registry: `registry.db` beside the artefacts locally, Postgres on a deployment."""
    state = request.app.state
    cached: ModelRegistry | None = getattr(state, "registry", None)
    if cached is not None:
        return cached
    data_dir = _state_path(request, "data_dir")
    fresh: ModelRegistry = (
        LocalModelRegistry(data_dir / REGISTRY_FILENAME)
        if data_dir is not None
        else build_registry(get_settings(request), get_storage(request))
    )
    with _LOCK:
        existing: ModelRegistry | None = getattr(state, "registry", None)
        if existing is None:
            state.registry = fresh
            return fresh
    return existing


def get_jobs(request: Request) -> JobRunner:
    """The job runner that carries pipeline runs off the request thread.

    `ThreadJobRunner` unless the deployment asks for SageMaker. The SageMaker runner needs the store
    - it writes the job spec the container reads back - so it is built after `get_storage`, and the
    import is inside the branch so a laptop never loads boto3 (DEC-306).
    """
    state = request.app.state
    cached: JobRunner | None = getattr(state, "jobs", None)
    if cached is not None:
        return cached
    settings = get_settings(request)
    remote = settings.job_backend == "sagemaker" and _state_path(request, "data_dir") is None
    fresh: JobRunner = (
        _sagemaker_runner(settings, request)
        if remote
        else ThreadJobRunner(max_workers=settings.job_max_workers)
    )
    with _LOCK:
        existing: JobRunner | None = getattr(state, "jobs", None)
        if existing is None:
            state.jobs = fresh
            return fresh
    fresh.shutdown(wait=False)  # another request won the race; do not leak this one's threads
    return existing


def _sagemaker_runner(settings: Settings, request: Request) -> JobRunner:
    """Build a `SageMakerJobRunner`; separated so `get_jobs` stays readable and the import stays local."""
    # A deliberate local import: boto3 is an optional dependency and a laptop must not pay for it (DEC-306).
    from engine.aws.sagemaker_jobs import SageMakerJobConfig, SageMakerJobRunner

    return SageMakerJobRunner(
        client=None,
        config=SageMakerJobConfig.from_settings(settings),
        storage=get_storage(request),
    )


def get_run_index(request: Request) -> RunIndex | None:
    """The run index, or `None` when this deployment does not keep one.

    `None` is the normal answer, not a degraded one: `run.json` is the record of a run and the index
    is only a faster way to list them, so `GET /runs` falls back to enumerating the store exactly as
    Phase 1 did. A deployment on SQLite therefore behaves identically to Phase 1, and a deployment on
    Postgres gets a list whose cost does not grow with the number of artefacts (DEC-342).
    """
    state = request.app.state
    cached: RunIndex | None = getattr(state, "run_index", None)
    if cached is not None:
        return cached
    if _state_path(request, "data_dir") is not None:
        return None
    settings = get_settings(request)
    if settings.metadata_backend != "postgres":
        return None
    # A deliberate local import: psycopg is an optional dependency, and this module declares tables
    # that must not reach SQLModel's metadata on a SQLite deployment (DEC-306, DEC-340).
    from engine.aws.postgres import postgres_run_index

    fresh = postgres_run_index(settings)
    with _LOCK:
        existing: RunIndex | None = getattr(state, "run_index", None)
        if existing is None:
            state.run_index = fresh
            return fresh
    return existing


def get_metrics(request: Request) -> MetricSink:
    """Where this deployment's metrics go; `NullMetricSink` unless it asked for otherwise.

    Built here for the same reason the other three are: one per process, cached on `app.state`, and
    replaceable by a test through `dependency_overrides`. `metric_sink_for` returns the null sink
    for every local deployment, so measuring costs a laptop nothing and changes nothing.
    """
    state = request.app.state
    cached: MetricSink | None = getattr(state, "metrics", None)
    if cached is not None:
        return cached
    fresh = metric_sink_for(get_settings(request))
    with _LOCK:
        existing: MetricSink | None = getattr(state, "metrics", None)
        if existing is None:
            state.metrics = fresh
            return fresh
    return existing


ConfigRootDep = Annotated[Path, Depends(get_config_root)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StorageDep = Annotated[Storage, Depends(get_storage)]
RegistryDep = Annotated[ModelRegistry, Depends(get_registry)]
JobsDep = Annotated[JobRunner, Depends(get_jobs)]
RunIndexDep = Annotated["RunIndex | None", Depends(get_run_index)]
MetricsDep = Annotated[MetricSink, Depends(get_metrics)]
