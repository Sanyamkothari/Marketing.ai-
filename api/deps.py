"""FastAPI dependencies: the config root and the three process-wide services.

Every dependency is exposed twice: as a plain function (so tests and `app.dependency_overrides` can
address it) and as an `Annotated[T, Depends(fn)]` alias, because a `Depends(...)` default argument is
a function call in a default and ruff's B008 forbids it. The services are built on first use, so
importing `api.main` never touches the filesystem; M1 has no route that needs them.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from engine.config import config_root
from engine.jobs import JobRunner, ThreadJobRunner
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry, ModelRegistry, default_registry
from engine.storage import LocalStorage, Storage, default_storage

_LOCK: threading.Lock = threading.Lock()


def _state_path(request: Request, name: str) -> Path | None:
    """A `Path` attribute set on `app.state` by `create_app`, or `None` when it was not given."""
    value = getattr(request.app.state, name, None)
    return None if value is None else Path(str(value))


def get_config_root(request: Request) -> Path:
    """The configuration directory this app serves.

    `create_app(config_root=…)` wins, then `$MARKETING_AI_CONFIG_DIR`, then the checkout's `configs/`.
    """
    return config_root(_state_path(request, "config_root"))


def get_storage(request: Request) -> Storage:
    """The artefact store, rooted at `create_app(data_dir=…)` or `$MARKETING_AI_DATA_DIR`."""
    state = request.app.state
    with _LOCK:
        existing: Storage | None = getattr(state, "storage", None)
        if existing is None:
            data_dir = _state_path(request, "data_dir")
            existing = default_storage() if data_dir is None else LocalStorage(data_dir)
            state.storage = existing
    return existing


def get_registry(request: Request) -> ModelRegistry:
    """The model registry, stored beside the artefacts as `registry.db`."""
    state = request.app.state
    with _LOCK:
        existing: ModelRegistry | None = getattr(state, "registry", None)
        if existing is None:
            data_dir = _state_path(request, "data_dir")
            existing = (
                default_registry() if data_dir is None else LocalModelRegistry(data_dir / REGISTRY_FILENAME)
            )
            state.registry = existing
    return existing


def get_jobs(request: Request) -> JobRunner:
    """The job runner that will carry pipeline runs off the request thread from M2 on."""
    state = request.app.state
    with _LOCK:
        existing: JobRunner | None = getattr(state, "jobs", None)
        if existing is None:
            existing = ThreadJobRunner()
            state.jobs = existing
    return existing


ConfigRootDep = Annotated[Path, Depends(get_config_root)]
StorageDep = Annotated[Storage, Depends(get_storage)]
RegistryDep = Annotated[ModelRegistry, Depends(get_registry)]
JobsDep = Annotated[JobRunner, Depends(get_jobs)]
