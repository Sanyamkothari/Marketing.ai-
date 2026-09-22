"""A SageMaker control plane that actually runs the job, in this process.

`moto` can pretend a training job exists; it cannot run this product's container, and a fake that
only returned canned statuses would leave the one question that matters unasked - *is what the
runner puts in the request enough for the container to do the work?* The answer has to come from
the container reading the real request, so this fake runs `scripts.run_job_entrypoint.main` on a
thread with the `Environment` mapping out of the `CreateTrainingJob` request and nothing else
(DEC-337). Anything the runner forgot to ship is then a failure here rather than a surprise in an
account.

Three things it is careful about:

* **Every request is recorded, verbatim.** `calls` is the full history, so a test can assert on the
  tags, the entrypoint or the environment of the exact request that was sent.
* **Nothing outside the six-call protocol answers.** `__getattr__` raises, so a `ListTrainingJobs`
  is a named test failure rather than a silently-tolerated call - which is what makes DEC-334
  checkable rather than merely documented.
* **Nothing is invented.** `BillableTimeInSeconds` is the wall clock this fake actually spent on
  the thread, rounded up to a whole second the way the service reports one. It is a measurement of
  the fake, which is the only billable thing there is here; a made-up number would put a fiction
  into `CostEstimate.basis` (plan section 13.3).

The statuses are the real SageMaker vocabulary - `InProgress`/`Completed`/`Failed`/`Stopped`, with
`Starting` and `Training` as secondary statuses - because that vocabulary is what
`engine.aws.sagemaker_jobs.JOB_STATES` claims to map, and a fake with a tidier one of its own would
prove nothing about the mapping.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from engine.jobs import CancelToken
from engine.utils.time import utc_now

__all__ = [
    "CREATE_PROCESSING_JOB",
    "CREATE_TRAINING_JOB",
    "EXIT_CANCELLED",
    "EXIT_FAILED",
    "EXIT_OK",
    "FakeSageMakerClient",
    "RecordedCall",
    "run_entrypoint",
]

CREATE_TRAINING_JOB: Final[str] = "CreateTrainingJob"
CREATE_PROCESSING_JOB: Final[str] = "CreateProcessingJob"
DESCRIBE_TRAINING_JOB: Final[str] = "DescribeTrainingJob"
DESCRIBE_PROCESSING_JOB: Final[str] = "DescribeProcessingJob"
STOP_TRAINING_JOB: Final[str] = "StopTrainingJob"
STOP_PROCESSING_JOB: Final[str] = "StopProcessingJob"

EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1
EXIT_CANCELLED: Final[int] = 2
"""The entrypoint's exit codes, as the service would read them off the container."""

_EXIT_STATUS: Final[Mapping[int, str]] = {
    EXIT_OK: "Completed",
    EXIT_FAILED: "Failed",
    EXIT_CANCELLED: "Stopped",
}
"""Container exit code -> the status SageMaker reports. Any other code is a failure."""

JobBody = Callable[[Mapping[str, str], CancelToken], int]
"""What the fake runs for a job: the request's environment and a token, returning an exit code."""


def run_entrypoint(environment: Mapping[str, str], cancel: CancelToken) -> int:
    """Run the real container entrypoint against `environment`; its exit code is the job's outcome.

    Imported inside the call because `scripts.run_job_entrypoint` imports the whole engine, and a
    test module that only wants the request builders should not pay for that.
    """
    from scripts.run_job_entrypoint import main

    return main([], environment, cancel=cancel)


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One API call this fake answered, kept whole so a test can assert on the real request."""

    api: str
    request: Mapping[str, Any]

    def tag(self, key: str) -> str | None:
        """The value of one cost-allocation tag on this request, or `None` when it carries none."""
        for item in self.request.get("Tags", ()):
            if item.get("Key") == key:
                return str(item.get("Value"))
        return None

    @property
    def environment(self) -> Mapping[str, str]:
        """The container environment this request carried."""
        return dict(self.request.get("Environment", {}))


@dataclass
class _Job:
    """One job the fake is carrying: its request, its thread and everything `Describe*` reports."""

    name: str
    training: bool
    request: Mapping[str, Any]
    created_at: datetime
    cancel: CancelToken
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: str = "InProgress"
    secondary: str = "Starting"
    exit_code: int | None = None
    thread: threading.Thread | None = field(default=None, repr=False)


class FakeSageMakerClient:
    """The six calls `engine.aws.sagemaker_jobs` makes, answered by running the job here.

    `body` is what a job *is*: `run_entrypoint` by default, so the container's own code runs, and a
    plain function in a test that only cares about the control plane. `account` and `region` only
    shape the ARNs, which nothing in the engine parses - they are there so a recorded description
    looks like one.
    """

    def __init__(
        self,
        *,
        body: JobBody = run_entrypoint,
        region: str = "ap-south-1",
        account: str = "000000000000",
        autostart: bool = True,
    ) -> None:
        self._body = body
        self._region = region
        self._account = account
        self._autostart = autostart
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self.calls: list[RecordedCall] = []

    # -- the six calls -------------------------------------------------------
    def create_training_job(self, **request: Any) -> dict[str, str]:
        """`CreateTrainingJob`: record it, start the container, answer with the ARN."""
        return {"TrainingJobArn": self._create(CREATE_TRAINING_JOB, request, training=True)}

    def create_processing_job(self, **request: Any) -> dict[str, str]:
        """`CreateProcessingJob`: the same, for the job kind a scoring run is (DEC-332)."""
        return {"ProcessingJobArn": self._create(CREATE_PROCESSING_JOB, request, training=False)}

    def describe_training_job(self, **request: Any) -> dict[str, Any]:
        """`DescribeTrainingJob`, including the `BillableTimeInSeconds` only this API reports."""
        job = self._job(DESCRIBE_TRAINING_JOB, request, "TrainingJobName")
        description: dict[str, Any] = {
            "TrainingJobName": job.name,
            "TrainingJobArn": self._arn("training-job", job.name),
            "TrainingJobStatus": job.status,
            "SecondaryStatus": job.secondary,
            "CreationTime": job.created_at,
            "ResourceConfig": dict(job.request.get("ResourceConfig", {})),
        }
        if job.started_at is not None:
            description["TrainingStartTime"] = job.started_at
        if job.ended_at is not None:
            description["TrainingEndTime"] = job.ended_at
            description["BillableTimeInSeconds"] = _billable(job)
        return description

    def describe_processing_job(self, **request: Any) -> dict[str, Any]:
        """`DescribeProcessingJob`: start and end times, which are wall clock, and no billable time."""
        job = self._job(DESCRIBE_PROCESSING_JOB, request, "ProcessingJobName")
        description: dict[str, Any] = {
            "ProcessingJobName": job.name,
            "ProcessingJobArn": self._arn("processing-job", job.name),
            "ProcessingJobStatus": job.status,
            "CreationTime": job.created_at,
            "ProcessingResources": dict(job.request.get("ProcessingResources", {})),
        }
        if job.started_at is not None:
            description["ProcessingStartTime"] = job.started_at
        if job.ended_at is not None:
            description["ProcessingEndTime"] = job.ended_at
        return description

    def stop_training_job(self, **request: Any) -> dict[str, Any]:
        """`StopTrainingJob`: set the token, which is what SIGTERM does to a real container."""
        self._stop(STOP_TRAINING_JOB, request, "TrainingJobName")
        return {}

    def stop_processing_job(self, **request: Any) -> dict[str, Any]:
        """`StopProcessingJob`: the same."""
        self._stop(STOP_PROCESSING_JOB, request, "ProcessingJobName")
        return {}

    def __getattr__(self, name: str) -> Any:
        """Anything outside the six calls is a test failure that names itself.

        `ListTrainingJobs` is the one this is really for: DEC-334 says nothing in the runner may
        ask a question that needs permission over the whole account, and a failure that says which
        call was attempted is how that stays true as the runner changes.

        A dunder is answered with `AttributeError` as any object would, because the interpreter,
        `copy` and pytest all probe for dunders on anything they are handed, and turning one of
        those probes into a test failure would say nothing about the runner.
        """
        if name.startswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"the SageMaker runner may not call {name!r}; it asks about named jobs only")

    # -- the test-facing surface --------------------------------------------
    def wait(self, job_name: str, timeout: float = 120.0) -> str:
        """Block until the job's container has exited and return its final status."""
        with self._lock:
            thread = self._jobs[job_name].thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError(f"job {job_name!r} did not finish within {timeout} seconds")
        with self._lock:
            return self._jobs[job_name].status

    def wait_all(self, timeout: float = 120.0) -> None:
        """Block until every job this fake has started has finished."""
        with self._lock:
            names = list(self._jobs)
        for name in names:
            self.wait(name, timeout)

    def start(self, job_name: str) -> None:
        """Start a job held back by `autostart=False`; how a test keeps a job pending on purpose."""
        with self._lock:
            job = self._jobs[job_name]
        self._start(job)

    def apis(self) -> tuple[str, ...]:
        """Every API this fake was asked for, in order."""
        return tuple(call.api for call in self.calls)

    def requests(self, api: str) -> tuple[Mapping[str, Any], ...]:
        """Every request sent to one API, in order."""
        return tuple(call.request for call in self.calls if call.api == api)

    def last(self, api: str) -> RecordedCall:
        """The most recent call to `api`; `LookupError` when there was none."""
        for call in reversed(self.calls):
            if call.api == api:
                return call
        raise LookupError(api)

    def statuses(self) -> Mapping[str, str]:
        """Job name -> the status this fake would report for it right now."""
        with self._lock:
            return {name: job.status for name, job in self._jobs.items()}

    # -- internals -----------------------------------------------------------
    def _record(self, api: str, request: Mapping[str, Any]) -> None:
        with self._lock:
            self.calls.append(RecordedCall(api=api, request=dict(request)))

    def _create(self, api: str, request: Mapping[str, Any], *, training: bool) -> str:
        self._record(api, request)
        name = str(request["TrainingJobName" if training else "ProcessingJobName"])
        job = _Job(
            name=name,
            training=training,
            request=dict(request),
            created_at=utc_now(),
            cancel=CancelToken(),
        )
        with self._lock:
            if name in self._jobs:
                raise AssertionError(f"job {name!r} was created twice; SageMaker names are unique")
            self._jobs[name] = job
        if self._autostart:
            self._start(job)
        return self._arn("training-job" if training else "processing-job", name)

    def _start(self, job: _Job) -> None:
        """Run the container on its own thread, then record how it ended."""

        def run() -> None:
            with self._lock:
                job.started_at = utc_now()
                job.secondary = "Training"
            code = EXIT_FAILED
            try:
                code = self._body(dict(job.request.get("Environment", {})), job.cancel)
            finally:
                with self._lock:
                    job.exit_code = code
                    job.ended_at = utc_now()
                    job.status = _EXIT_STATUS.get(code, "Failed")
                    job.secondary = job.status

        thread = threading.Thread(target=run, name=f"fake-sagemaker-{job.name}", daemon=True)
        with self._lock:
            job.thread = thread
        thread.start()

    def _job(self, api: str, request: Mapping[str, Any], key: str) -> _Job:
        self._record(api, request)
        name = str(request[key])
        with self._lock:
            job = self._jobs.get(name)
        if job is None:
            raise _validation_error(api, name)
        return job

    def _stop(self, api: str, request: Mapping[str, Any], key: str) -> None:
        job = self._job(api, request, key)
        job.cancel.cancel()
        with self._lock:
            if job.ended_at is None:
                job.status = "Stopping"
                job.secondary = "Stopping"

    def _arn(self, resource: str, name: str) -> str:
        return f"arn:aws:sagemaker:{self._region}:{self._account}:{resource}/{name}"


def _billable(job: _Job) -> int:
    """The seconds this fake actually spent running the job, as whole seconds like the service.

    Rounded *up*, because SageMaker bills a started second and a sub-second job that reported zero
    billable seconds would send `engine.aws.prices` down the "nothing was billed" path and make the
    cost estimate untestable for a reason that has nothing to do with the code under test.
    """
    if job.started_at is None or job.ended_at is None:  # pragma: no cover - only read once ended
        return 0
    return max(1, math.ceil((job.ended_at - job.started_at).total_seconds()))


def _validation_error(api: str, name: str) -> Exception:
    """What botocore raises for a `Describe*` of a job that does not exist.

    A real `ClientError`, built the way `botocore` builds one, so a caller's `except ClientError`
    means here what it will mean in an account. botocore is imported inside the function because a
    test that never asks about a missing job should not need it installed.
    """
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": f"Could not find {name}."}},
        api,
    )
