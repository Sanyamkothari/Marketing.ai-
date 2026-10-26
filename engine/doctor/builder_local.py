"""`BuilderPort` in process (v3 phase 0, B6): the onboarding routes' own functions, called directly.

`engine.doctor.builder_api.ApiBuilderPort` makes the Manual setup calls over HTTP. A caller that already runs inside
the API process (a Data Doctor tool) has no use for a socket: this port calls the functions those routes are, with
the same storage, client store and configuration root the app serves, so the dataset is built by exactly the code and
the checks a person's clicks would have run. Nothing is re-implemented here; a route that refuses raises
`BuildDriveError` with the route's own code, as the HTTP port does.

| Port call | Route function |
|---|---|
| `add_source` | `api.routes.sources.create_source` |
| `suggest_mapping` | `api.routes.mappings.suggest_mapping` |
| `save_mapping` | `api.routes.mappings.save_mapping` |
| `save_spec` | `api.routes.datasets.create_onboarding_spec` |
| `build` | `api.routes.datasets.create_dataset`, its job run on the calling thread, so the dataset is built when it returns |
| `save_record` | no route: the approved plan (`BuildRecord`) is written to `storage`, beside the client's sources |

`read_dataset` is beyond the port `drive_builder` needs: the built table and its report, which a caller reads once the
build is over. The routes' modules are imported inside the methods, so importing this one loads no route.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Coroutine, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import pandas as pd

from engine.doctor.build_driver import BuildDriveError, BuildRecord, build_record_key
from engine.jobs import CancelToken, JobFn, JobInfo
from engine.onboarding.specs import BuildReport, DatasetManifest
from engine.storage import Storage

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from pydantic import BaseModel

    from engine.clients import ClientStore

__all__ = ["BuiltDataset", "InlineJobs", "LocalBuilderPort"]

T = TypeVar("T")
M = TypeVar("M", bound="BaseModel")


@dataclass(frozen=True)
class BuiltDataset:
    """A finished build, read back: its manifest, its table and its report."""

    manifest: DatasetManifest
    frame: pd.DataFrame
    report: BuildReport


class InlineJobs:
    """A `JobRunner` that runs a job on the calling thread: `submit` returns when the job has ended.

    A dataset build is the one job the port submits, and a caller that waits for it wants the same thread, the same
    exceptions' bookkeeping (`build_m12_job` settles the status itself) and no pool. A job that raises is recorded
    as failed with its class name only; the build's own status document already says why."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobInfo] = {}

    def submit(self, job_id: str, fn: JobFn) -> JobInfo:
        from engine.contracts import RunState
        from engine.utils.time import utc_now

        began = utc_now()
        state, error = RunState.DONE, None
        try:
            fn(CancelToken())
        except Exception as exc:  # the job settled its own status; this is only the runner's record of it
            state, error = RunState.FAILED, type(exc).__name__
        info = JobInfo(
            job_id=job_id, state=state, submitted_at=began, started_at=began, ended_at=utc_now(), error=error
        )
        self._jobs[job_id] = info
        return info

    def status(self, job_id: str) -> JobInfo:
        return self._jobs[job_id]

    def cancel(self, job_id: str) -> bool:  # noqa: ARG002 - a job has ended before `submit` returns
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # noqa: ARG002 - there is no pool to stop
        return None


def _run(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run `coroutine` to its end from a plain function: on this thread when no loop runs here, else on a fresh one."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _refused(exc: Exception, what: str) -> BuildDriveError:
    """A route's `HTTPException` (or a body the route's model refuses) as the `BuildDriveError` the HTTP port raises."""
    detail: Any = getattr(exc, "detail", None)
    code = detail.get("code") if isinstance(detail, dict) else None
    message = detail.get("message") if isinstance(detail, dict) else None
    status = getattr(exc, "status_code", 422)
    return BuildDriveError(
        str(code or "BUILD_STEP_FAILED"),
        f"{what} was refused ({status}): {message or code or str(exc.__class__.__name__)}",
    )


@dataclass(frozen=True)
class LocalBuilderPort:
    """The builder, through its route functions, for one client, in this process."""

    client_id: str
    storage: Storage
    store: ClientStore
    config_root: Path
    request: Request
    """A request over the app (`api.routes.schedules.app_request`): the spec route reads the app's settings from it."""
    jobs: Any = None
    """The runner a build is submitted to; `InlineJobs` when unset, so `build` returns with the dataset built."""

    @classmethod
    def for_app(cls, app: FastAPI, client_id: str, *, jobs: Any = None) -> LocalBuilderPort:
        """The port over the services `app` serves: the same storage, client store and configuration root as its routes."""
        from api.deps import get_config_root, get_storage
        from api.routes.clients import get_client_store
        from api.routes.schedules import app_request

        request = app_request(app)
        return cls(
            client_id=client_id,
            storage=get_storage(request),
            store=get_client_store(request),
            config_root=get_config_root(request),
            request=request,
            jobs=jobs,
        )

    def add_source(self, *, file_name: str, data: bytes, role: str) -> str:
        from fastapi import HTTPException, Response, UploadFile

        from api.routes.sources import create_source

        upload = UploadFile(file=io.BytesIO(data), filename=file_name)
        try:
            created = _run(
                create_source(
                    self.client_id,
                    self.storage,
                    self.config_root,
                    self.store,
                    Response(),
                    upload,
                    role=role,
                    sheet=None,
                    intake=True,
                )
            )
        except HTTPException as exc:
            raise _refused(exc, f"Adding {file_name}") from exc
        return str(created.source_id)

    def suggest_mapping(self, *, source_id: str, use_case: str) -> Mapping[str, Any]:
        from fastapi import HTTPException

        from api.routes.mappings import MappingSuggestRequest, suggest_mapping

        try:
            suggested = suggest_mapping(
                self.client_id,
                MappingSuggestRequest(source_id=source_id, use_case=use_case),
                self.config_root,
                self.storage,
                self.store,
            )
        except HTTPException as exc:
            raise _refused(exc, "Suggesting a mapping") from exc
        return suggested.model_dump(mode="json")

    def save_mapping(
        self,
        *,
        mapping_id: str,
        source_id: str,
        use_case: str,
        role: str,
        columns: Sequence[Mapping[str, Any]],
    ) -> str:
        from fastapi import HTTPException
        from pydantic import ValidationError

        from api.routes.mappings import MappingSaveRequest, save_mapping

        try:
            body = MappingSaveRequest.model_validate(
                {
                    "client_id": self.client_id,
                    "source_id": source_id,
                    "use_case": use_case,
                    "role": role,
                    "columns": [dict(column) for column in columns],
                }
            )
            saved = save_mapping(self.client_id, mapping_id, body, self.config_root, self.storage, self.store)
        except (HTTPException, ValidationError) as exc:
            raise _refused(exc, "Saving a mapping") from exc
        return str(saved.mapping_id)

    def save_spec(self, body: Mapping[str, Any]) -> str:
        from fastapi import HTTPException
        from pydantic import ValidationError

        from api.routes.datasets import OnboardingSpecCreateRequest, create_onboarding_spec

        try:
            request_body = OnboardingSpecCreateRequest.model_validate(dict(body))
            created = create_onboarding_spec(
                self.client_id, request_body, self.request, self.config_root, self.store
            )
        except (HTTPException, ValidationError) as exc:
            raise _refused(exc, "Saving the recipe") from exc
        return str(created.spec_id)

    def build(self, *, spec_id: str, mode: str) -> str:
        from fastapi import HTTPException, Response
        from fastapi.responses import JSONResponse
        from pydantic import ValidationError

        from api.routes.datasets import DatasetBuildRequest, create_dataset

        try:
            answered = create_dataset(
                DatasetBuildRequest.model_validate(
                    {"client_id": self.client_id, "spec_id": spec_id, "mode": mode}
                ),
                self.config_root,
                self.storage,
                self.store,
                self.jobs if self.jobs is not None else InlineJobs(),
                Response(),
            )
        except (HTTPException, ValidationError) as exc:
            raise _refused(exc, "Building the dataset") from exc
        if isinstance(answered, JSONResponse):  # the 409 with the check list: a blocking error remains
            import json

            payload = json.loads(bytes(answered.body))
            errors = [
                c for c in payload.get("checks", ()) if isinstance(c, dict) and c.get("severity") == "error"
            ]
            code = str(errors[0].get("code")) if errors else "VALIDATION_FAILED"
            said = str(errors[0].get("message")) if errors else "a blocking check remains"
            raise BuildDriveError(code, f"Building the dataset was refused (409): {said}")
        return str(answered.dataset_id)

    def save_record(self, record: BuildRecord) -> str | None:
        key = build_record_key(self.client_id, record.spec_id)
        self.storage.write_model(key, record)
        return key

    def load_record(self, spec_id: str) -> BuildRecord:
        """The approved plan kept for the recipe `spec_id`; `BUILD_PLAN_NOT_APPROVED` when none was kept."""
        key = build_record_key(self.client_id, spec_id)
        if not self.storage.exists(key):
            raise BuildDriveError(
                "BUILD_PLAN_NOT_APPROVED", f"No approved Data Doctor plan is kept for the recipe {spec_id}."
            )
        record: BuildRecord = self.storage.read_model(key, BuildRecord)
        return record

    def read_dataset(self, dataset_id: str) -> BuiltDataset:
        """The finished build `dataset_id`: manifest, table and report. A build that did not pass leaves a report and no
        table; that is `BuildDriveError` with the code of its first blocking check and the words of all of them.
        """
        from engine.onboarding.datasets import (
            DATASET_FRAME_FILENAME,
            DATASET_MANIFEST_FILENAME,
            DATASET_REPORT_FILENAME,
            LocalDatasetRegistry,
            dataset_key,
        )

        report_key = dataset_key(dataset_id, DATASET_REPORT_FILENAME)
        if not self.storage.exists(report_key):
            raise BuildDriveError("BUILD_STEP_FAILED", f"The build {dataset_id} left no report.")
        report: BuildReport = self._read(report_key, BuildReport, dataset_id, "report")
        frame_key = dataset_key(dataset_id, DATASET_FRAME_FILENAME)
        manifest_key = dataset_key(dataset_id, DATASET_MANIFEST_FILENAME)
        if not report.passed or not self.storage.exists(frame_key) or not self.storage.exists(manifest_key):
            errors = [check for check in report.checks if check.severity.value == "error"]
            said = " ".join(f"{check.code}: {check.message}" for check in errors) or "it did not finish."
            raise BuildDriveError(
                errors[0].code if errors else "BUILD_STEP_FAILED",
                f"The build {dataset_id} wrote no dataset. {said}",
            )
        manifest: DatasetManifest = self._read(manifest_key, DatasetManifest, dataset_id, "manifest")
        return BuiltDataset(
            manifest=manifest, frame=LocalDatasetRegistry(self.storage).read_frame(dataset_id), report=report
        )

    def _read(self, key: str, model: type[M], dataset_id: str, what: str) -> M:
        """`storage.read_model`, with a document that does not read as `model` (pydantic's `ValidationError` is a
        `ValueError`) as `BUILD_STEP_FAILED`."""
        try:
            read: M = self.storage.read_model(key, model)
        except ValueError as exc:
            raise BuildDriveError(
                "BUILD_STEP_FAILED", f"The {what} of the build {dataset_id} cannot be read back."
            ) from exc
        return read
