"""`SageMakerJobRunner`: the same four-method `JobRunner`, answered by a SageMaker job.

`ThreadJobRunner` runs a closure on a thread of this process. A container cannot be handed a
closure, so `engine.contracts.JobSpec` became the declarative description of the work and this
runner ships **the spec's storage key** in the container's environment and drops the callable it was
given (DEC-324). The container reads the document back with `engine.runs.read_job_spec` and calls
`engine.runs.run_job_spec`, which is the function the local closure would have called. One
description, two renderings, no second definition of the work.

Four things are genuinely different about a remote job, and each is handled here:

**A job can end without this process hearing about it.** The container can be killed, the instance
can fail to start, the job can be stopped from the console - and in every one of those cases nothing
in this process was there to write the ending, so `status.json` says "running" for ever. `reconcile`
is the additive `ReconcilingJobRunner` capability that fixes that, and `GET /runs/{id}` calls it
(DEC-325).

**Nothing here ever calls a `List*` API.** Every question is asked about a *named* job with
`Describe*`, and the name is derived from the run id, so an IAM policy can scope this role to
`…:training-job/<prefix>-*` and nothing else. A `ListTrainingJobs` call would need permission over
the whole account's job history to answer a question about one run (DEC-334).

**A queue is not a state.** `RunState` has no `queued` member and DEC-027 says there is one state
vocabulary for runs, stages and jobs. A run held back by `max_concurrent_jobs` is `pending` - which
is exactly what it is - with the detail line "Waiting for compute" on its first stage, so the
Running screen says the true thing without the enum growing a member for one backend (DEC-335).

**A billed quantity is never invented.** `volume_size_gb` and `max_runtime_seconds` are `None` by
default and are *left out of the request* when unset, so the service's own default applies rather
than a number this repository made up (plan section 13.3, DEC-336).

No boto3 import at module scope: the client is built on first use (DEC-306).
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast

from engine.aws.prices import PriceTable, cost_estimate, load_price_table
from engine.aws.secrets import quiet_aws_wire_logs
from engine.contracts import (
    ComputeBackend,
    ComputeInfo,
    JobEntrypoint,
    JobSpec,
    RunError,
    RunManifest,
    RunState,
    RunStatus,
    StageKey,
)
from engine.errors import ENGINE_ERRORS, JOB_FAILED_REMOTELY, JOB_SPEC_UNREADABLE, JOB_SUBMIT_FAILED
from engine.jobs import JobFn, JobInfo
from engine.runs import (
    RUN_MANIFEST_FILENAME,
    STATUS_FILENAME,
    cancel_run,
    fail_run,
    job_spec_key,
    read_job_spec,
    update_stage,
    write_job_spec,
)
from engine.settings import ENV_VARS, SECRET_FIELDS, SETTINGS_SOURCE_ENV_VAR, Settings
from engine.storage import Storage, StorageError, run_key
from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

__all__ = [
    "BACKEND_FOR_ENTRYPOINT",
    "CONTAINER_ENTRYPOINT",
    "JOB_SPEC_KEY_ENV_VAR",
    "JOB_STATES",
    "MAX_JOB_NAME_LENGTH",
    "PROCESSING_BACKEND",
    "RECONCILE_GRACE_SECONDS",
    "SHIPPED_SETTINGS_FIELDS",
    "STARTING_SECONDARY_STATES",
    "TERMINAL_STATES",
    "TRAINING_BACKEND",
    "WAITING_FOR_COMPUTE",
    "SageMakerError",
    "SageMakerJobConfig",
    "SageMakerJobRunner",
    "container_environment",
    "job_name_for",
    "processing_request",
    "training_request",
]

_LOGGER = get_logger(__name__)

JOB_SPEC_KEY_ENV_VAR: Final[str] = "MARKETING_AI_JOB_SPEC_KEY"
"""The one variable that is the whole contract between this runner and the container (DEC-328).

It is in `engine.settings.NON_FIELD_ENV_VARS` on purpose: it describes one job, not a deployment.
"""

TRAINING_BACKEND: Final[str] = "sagemaker-training"
PROCESSING_BACKEND: Final[str] = "sagemaker-processing"
"""`ComputeInfo.backend` and `JobSpec.backend` for the two job kinds, and the keys the price table
is read with. A training job and a processing job are billed under different components, so the
distinction has to survive into the manifest."""

BACKEND_FOR_ENTRYPOINT: Final[Mapping[JobEntrypoint, str]] = {
    JobEntrypoint.TRAIN: TRAINING_BACKEND,
    JobEntrypoint.SCORE: PROCESSING_BACKEND,
}
"""Train is a Training job; score is a Processing job (DEC-332).

Not one job type for both: `DescribeTrainingJob` reports `BillableTimeInSeconds` and a training job
is the one SageMaker resource that carries a model artefact, while scoring is a batch that consumes
one. Using a training job to score would put "trained a model" in the account's own record of what
happened, which is not true.
"""

CONTAINER_ENTRYPOINT: Final[tuple[str, ...]] = ("python", "-m", "scripts.run_job_entrypoint")
"""What the image is asked to run. Stated in the request rather than left to the image's `ENTRYPOINT`
so that the API and the image cannot disagree about it silently."""

WAITING_FOR_COMPUTE: Final[str] = "Waiting for compute"
"""The detail line on a run held back by `max_concurrent_jobs`. The state stays `pending` (DEC-335)."""

TRAINING_INPUT_MODE: Final[str] = "File"
"""The container reads its inputs from the artefact store itself, so no channel is mounted; `File`
is the mode a job with no input channels is created with."""

RECONCILE_GRACE_SECONDS: Final[int] = 120
"""How stale `status.json` must be before reconciliation overwrites it. **Chosen, not measured.**

A job that SageMaker calls `Failed` may still have a container writing the real failure into
`status.json`: the two are different systems and neither waits for the other. Reconciliation that
wins that race would replace a stage-level failure a user can act on with a generic one. So the
document is left alone until it has been quiet for this long, which trades a slower correction for
never destroying a better answer. Nobody has measured how long that write actually takes - the
number is a margin, not a measurement (plan section 13.3, DEC-325).
"""

MAX_JOB_NAME_LENGTH: Final[int] = 63
"""SageMaker's documented limit for a training or processing job name. Not chosen: the service's."""

_JOB_NAME_ILLEGAL: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9-]+")
"""Everything SageMaker does not accept in a job name; a run id's underscores land here."""

_TAG_KEY: Final[re.Pattern[str]] = re.compile(r"^[\w\s+\-=.:/@]{1,128}$")
_TAG_VALUE: Final[re.Pattern[str]] = re.compile(r"^[\w\s+\-=.:/@]{0,256}$")
"""AWS's documented tag character set. A tag outside it is dropped rather than sent."""

SHIPPED_SETTINGS_FIELDS: Final[tuple[str, ...]] = (
    "env",
    "aws_region",
    "config_dir",
    "storage_backend",
    "data_dir",
    "s3_bucket",
    "s3_prefix",
    "s3_kms_key_id",
    "metadata_backend",
    "postgres_schema",
    "client_id",
    "log_level",
    "log_format",
    "metrics_backend",
)
"""The deployment description the container is handed, as an allow-list (DEC-337).

An allow-list, because the failure mode of forgetting a field is a container that says "this
setting is not configured" and the failure mode of *including* one by accident could be a
credential in `DescribeTrainingJob`, which anybody with read access to the account can call. Nothing
in `engine.settings.SECRET_FIELDS` may appear here and a unit test asserts that it does not: the
database URL reaches the container from Secrets Manager, read by the container's own role, never
through a job's environment.

The `sagemaker_*` fields are absent for a different reason - the container submits nothing, so a
description of how to submit is not its business - and `local_cache_dir` is absent because it names
a directory on the host that submitted the job, which the container has no reason to believe in.
"""


class SageMakerError(Exception):
    """A SageMaker operation failed, or was asked for something the service has no default for.

    `code` is one of the `engine.errors` job codes; `job_name` is the job it was about. The
    exception's `message` is business language and never quotes an AWS message: a `FailureReason`
    is the container's last words and can contain a value out of the customer's file (DEC-331).
    """

    def __init__(self, code: str, message: str, *, job_name: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.job_name = job_name


class _SageMakerClient(Protocol):
    """The six calls this module makes. Six `Describe`/`Create`/`Stop`, and no `List` (DEC-334)."""

    def create_training_job(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def create_processing_job(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def describe_training_job(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def describe_processing_job(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def stop_training_job(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def stop_processing_job(self, **kwargs: Any) -> Mapping[str, Any]: ...


# ---------------------------------------------------------------------------
# The SageMaker status vocabulary, in full
# ---------------------------------------------------------------------------
JOB_STATES: Final[Mapping[str, RunState]] = {
    "InProgress": RunState.RUNNING,
    "Stopping": RunState.RUNNING,
    "Completed": RunState.DONE,
    "Failed": RunState.FAILED,
    "Stopped": RunState.CANCELLED,
}
"""Every value `TrainingJobStatus` and `ProcessingJobStatus` can take, mapped to DEC-027's vocabulary.

The two APIs share this vocabulary exactly, which is why one table serves both. `Stopping` is
`running`, not `cancelled`: a stop has been *asked for* and the container is still executing, and
calling it cancelled would let a run report a terminal state it can still come back from.
"""

STARTING_SECONDARY_STATES: Final[frozenset[str]] = frozenset(
    {
        "Starting",
        "Pending",
        "LaunchingMLInstances",
        "PreparingTrainingStack",
        "Downloading",
        "DownloadingTrainingImage",
        "Restarting",
    }
)
"""`SecondaryStatus` values that mean the container has not begun the work yet.

While the job is in one of these, SageMaker's own status is already `InProgress` but nothing of the
run has happened: the instance is being launched and the image is being pulled. Reporting `running`
then would start the run's clock before its first stage. `pending` is the honest answer and it is
the same answer the queue gives, for the same reason (DEC-335).
"""

TERMINAL_STATES: Final[frozenset[RunState]] = frozenset({RunState.DONE, RunState.FAILED, RunState.CANCELLED})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SageMakerJobConfig:
    """Everything a job request needs, resolved once, so the request builders stay pure functions.

    The billed quantities - `volume_size_gb`, `max_runtime_seconds` - are `None` by default and are
    omitted from the request when they are, so the service default applies rather than a number
    nobody measured. `CreateProcessingJob` is the one exception, and it is the service's: its
    `ClusterConfig.VolumeSizeInGB` is a *required* member, so a scoring job cannot be created
    without one and this module refuses rather than inventing a size (DEC-336).
    """

    role_arn: str
    image_uri: str
    train_instance_type: str
    processing_instance_type: str
    region: str
    output_s3_uri: str
    instance_count: int = 1
    volume_size_gb: int | None = None
    max_runtime_seconds: int | None = None
    max_concurrent_jobs: int = 2
    subnet_ids: tuple[str, ...] = ()
    security_group_ids: tuple[str, ...] = ()
    job_name_prefix: str = "marketing-ai"
    environment: Mapping[str, str] = field(default_factory=dict)
    kms_key_id: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> SageMakerJobConfig:
        """The configuration this deployment describes.

        `Settings` already refuses `job_backend=sagemaker` without a role, an image, both instance
        types, a region and S3 storage, so the four `_required` calls here can only fire for a
        caller that built this from settings describing a different backend - which is a wiring
        mistake, and is reported as one rather than as a missing key on the wire.
        """
        bucket = _required(settings.s3_bucket, "s3_bucket")
        prefix = f"{settings.s3_prefix}/" if settings.s3_prefix else ""
        return cls(
            role_arn=_required(settings.sagemaker_role_arn, "sagemaker_role_arn"),
            image_uri=_required(settings.sagemaker_image_uri, "sagemaker_image_uri"),
            train_instance_type=_required(settings.sagemaker_instance_type, "sagemaker_instance_type"),
            processing_instance_type=_required(
                settings.sagemaker_processing_instance_type, "sagemaker_processing_instance_type"
            ),
            region=_required(settings.aws_region, "region"),
            output_s3_uri=f"s3://{bucket}/{prefix}sagemaker/",
            instance_count=settings.sagemaker_instance_count,
            volume_size_gb=settings.sagemaker_volume_size_gb,
            max_runtime_seconds=settings.sagemaker_max_runtime_seconds,
            max_concurrent_jobs=settings.sagemaker_max_concurrent_jobs,
            subnet_ids=settings.sagemaker_subnet_ids,
            security_group_ids=settings.sagemaker_security_group_ids,
            job_name_prefix=settings.sagemaker_job_name_prefix,
            environment=container_environment(settings),
            kms_key_id=settings.s3_kms_key_id,
        )

    def instance_type_for(self, entrypoint: JobEntrypoint) -> str:
        """Which instance type this entrypoint runs on; two settings, because they are two costs."""
        if entrypoint is JobEntrypoint.TRAIN:
            return self.train_instance_type
        return self.processing_instance_type


def _required(value: str | None, field_name: str) -> str:
    """`value`, or a `SageMakerError` naming the setting that is missing - never its value."""
    if not value:
        raise SageMakerError(
            JOB_SUBMIT_FAILED,
            f"This deployment runs jobs on SageMaker but {ENV_VARS[field_name]} "
            f"({field_name}) is not configured.",
        )
    return value


def container_environment(settings: Settings, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The deployment description a job's container is handed, as environment variables.

    `SHIPPED_SETTINGS_FIELDS` is the allow-list and `engine.settings.ENV_VARS` gives every
    name, so the container is configured through exactly the mechanism a laptop is and there is no
    second spelling of any variable to keep in step (DEC-337).

    `MARKETING_AI_SETTINGS_SOURCE` is copied from *this* process rather than assumed: a task reading
    its configuration from Parameter Store should launch jobs that do the same, and a test process
    that describes itself entirely in its environment should launch jobs that do the same. The job
    spec key is added per job by the runner, not here.
    """
    source = (environ if environ is not None else os.environ).get(SETTINGS_SOURCE_ENV_VAR)
    out: dict[str, str] = {}
    for name in SHIPPED_SETTINGS_FIELDS:
        rendered = _as_text(getattr(settings, name))
        if rendered:
            out[ENV_VARS[name]] = rendered
    if source:
        out[SETTINGS_SOURCE_ENV_VAR] = source
    return out


def _as_text(value: object) -> str:
    """One setting as the container will read it back; `""` for anything absent."""
    if value is None:
        return ""
    if isinstance(value, tuple):
        return ",".join(str(item) for item in value)
    return str(value)


# ---------------------------------------------------------------------------
# The request builders: pure functions of a spec and a configuration
# ---------------------------------------------------------------------------
def job_name_for(spec: JobSpec, *, prefix: str) -> str:
    """`<prefix>-<entrypoint>-<run id>`, in SageMaker's alphabet.

    Derived rather than random, and derived from the run id, so that every question about a job can
    be asked by name: `Describe*` needs a name, and a name nobody remembered after a restart would
    force a `List*` call over the whole account to find it again (DEC-334). The prefix is also the
    IAM resource boundary, which is why it is a setting rather than a constant.
    """
    raw = f"{prefix}-{spec.entrypoint.value}-{spec.run_id}"
    cleaned = _JOB_NAME_ILLEGAL.sub("-", raw).strip("-")
    if len(cleaned) > MAX_JOB_NAME_LENGTH:
        # Keep the tail: it carries the run id, which is what a human correlates with.
        cleaned = cleaned[-MAX_JOB_NAME_LENGTH:].lstrip("-")
    return cleaned


def _tags(spec: JobSpec) -> list[dict[str, str]]:
    """The spec's cost-allocation tags, as AWS wants them, with anything unsendable dropped."""
    return [
        {"Key": key, "Value": value}
        for key, value in sorted(spec.tags.items())
        if _TAG_KEY.match(key) and _TAG_VALUE.match(value)
    ]


def _environment(config: SageMakerJobConfig, *, spec_key: str) -> dict[str, str]:
    """The container's environment: the deployment, plus the one variable that names this job."""
    return {**dict(config.environment), JOB_SPEC_KEY_ENV_VAR: spec_key}


def _vpc_config(config: SageMakerJobConfig) -> dict[str, list[str]] | None:
    """`VpcConfig`, or `None` when this deployment does not put its jobs in a VPC."""
    if not config.subnet_ids:
        return None
    return {
        "Subnets": list(config.subnet_ids),
        "SecurityGroupIds": list(config.security_group_ids),
    }


def training_request(
    spec: JobSpec, config: SageMakerJobConfig, *, spec_key: str, job_name: str | None = None
) -> dict[str, Any]:
    """The `CreateTrainingJob` request for a training run. A pure function; no client is involved.

    `VolumeSizeInGB` and `MaxRuntimeInSeconds` appear only when this deployment set them, because
    `CreateTrainingJob` has documented defaults for both and a default the service publishes is a
    better number than one this repository would have to invent (DEC-336). `StoppingCondition` is a
    required member, so it is sent with whatever is set and otherwise as an empty object - which is
    what asks for the service default.
    """
    name = job_name or job_name_for(spec, prefix=config.job_name_prefix)
    resources: dict[str, Any] = {
        "InstanceType": config.instance_type_for(spec.entrypoint),
        "InstanceCount": config.instance_count,
    }
    if config.volume_size_gb is not None:
        resources["VolumeSizeInGB"] = config.volume_size_gb
    if config.kms_key_id:
        resources["VolumeKmsKeyId"] = config.kms_key_id
    stopping: dict[str, Any] = {}
    if config.max_runtime_seconds is not None:
        stopping["MaxRuntimeInSeconds"] = config.max_runtime_seconds
    request: dict[str, Any] = {
        "TrainingJobName": name,
        "RoleArn": config.role_arn,
        "AlgorithmSpecification": {
            "TrainingImage": config.image_uri,
            "TrainingInputMode": TRAINING_INPUT_MODE,
            "ContainerEntrypoint": list(CONTAINER_ENTRYPOINT),
        },
        "OutputDataConfig": _output_config(config, spec),
        "ResourceConfig": resources,
        "StoppingCondition": stopping,
        "Environment": _environment(config, spec_key=spec_key),
        "Tags": _tags(spec),
        "EnableNetworkIsolation": False,
    }
    vpc = _vpc_config(config)
    if vpc is not None:
        request["VpcConfig"] = vpc
    return request


def processing_request(
    spec: JobSpec, config: SageMakerJobConfig, *, spec_key: str, job_name: str | None = None
) -> dict[str, Any]:
    """The `CreateProcessingJob` request for a scoring run. A pure function; no client is involved.

    Unlike `CreateTrainingJob`, this API has **no default volume size**: `ClusterConfig` requires
    `VolumeSizeInGB`. There is therefore nothing to fall back to, and inventing a size would be
    writing a number nobody measured into a billed quantity. So the deployment has to say
    (`MARKETING_AI_SAGEMAKER_VOLUME_SIZE_GB`), and until it does a scoring run is refused with a
    message that says which setting is missing (DEC-336).
    """
    name = job_name or job_name_for(spec, prefix=config.job_name_prefix)
    if config.volume_size_gb is None:
        raise SageMakerError(
            JOB_SUBMIT_FAILED,
            "A scoring job needs a disk size, because SageMaker's processing API has no default "
            f"for one. Set {ENV_VARS['sagemaker_volume_size_gb']} for this deployment.",
            job_name=name,
        )
    cluster: dict[str, Any] = {
        "InstanceType": config.instance_type_for(spec.entrypoint),
        "InstanceCount": config.instance_count,
        "VolumeSizeInGB": config.volume_size_gb,
    }
    if config.kms_key_id:
        cluster["VolumeKmsKeyId"] = config.kms_key_id
    request: dict[str, Any] = {
        "ProcessingJobName": name,
        "RoleArn": config.role_arn,
        "AppSpecification": {
            "ImageUri": config.image_uri,
            "ContainerEntrypoint": list(CONTAINER_ENTRYPOINT),
        },
        "ProcessingResources": {"ClusterConfig": cluster},
        "Environment": _environment(config, spec_key=spec_key),
        "Tags": _tags(spec),
    }
    if config.max_runtime_seconds is not None:
        request["StoppingCondition"] = {"MaxRuntimeInSeconds": config.max_runtime_seconds}
    vpc = _vpc_config(config)
    if vpc is not None:
        request["NetworkConfig"] = {"VpcConfig": vpc}
    return request


def _output_config(config: SageMakerJobConfig, spec: JobSpec) -> dict[str, str]:
    """`OutputDataConfig`, which `CreateTrainingJob` requires even when nothing is written to it.

    The container writes every artefact through `Storage`, so this path receives only SageMaker's
    own `output.tar.gz`. It is still per-run, because a shared path would let two runs' outputs
    land on top of each other.
    """
    out = {"S3OutputPath": f"{config.output_s3_uri.rstrip('/')}/{spec.run_id}/"}
    if config.kms_key_id:
        out["KmsKeyId"] = config.kms_key_id
    return out


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Launched:
    """What this process remembers about a job it started: enough to ask about it by name."""

    spec: JobSpec
    job_name: str
    submitted_at: datetime


class SageMakerJobRunner:
    """`JobRunner` over SageMaker Training and Processing jobs.

    `client` is for tests and for a caller holding a configured client; left `None`, one is built on
    first use, which is also when boto3 is imported (DEC-306). `storage` is not decoration: the job
    spec lives in the artefact store, which is how this runner answers a question about a job it has
    never heard of - after a restart, a deployment, or on another task behind the same load balancer.
    """

    def __init__(
        self,
        *,
        config: SageMakerJobConfig,
        storage: Storage,
        client: _SageMakerClient | None = None,
        prices: PriceTable | None = None,
    ) -> None:
        self._config = config
        self._storage = storage
        self._client = client
        self._prices = prices
        self._prices_loaded = prices is not None
        self._lock = threading.Lock()
        self._running: dict[str, _Launched] = {}
        self._waiting: list[str] = []
        self._finished: set[str] = set()

    # -- the client ----------------------------------------------------------
    def _sagemaker(self) -> _SageMakerClient:
        """The boto3 client, built and cached on first use, with the wire loggers capped first."""
        client = self._client
        if client is None:
            import boto3  # a deliberate local import: boto3 is optional (DEC-306)

            quiet_aws_wire_logs()
            # `cast`, because boto3's generated client is a structural match for the six calls above
            # but not a nominal one; the Protocol is what this module is written against.
            client = cast("_SageMakerClient", boto3.client("sagemaker", region_name=self._config.region))
            self._client = client
        return client

    # -- JobRunner -----------------------------------------------------------
    def submit(self, job_id: str, fn: JobFn) -> JobInfo:
        """Launch the job `job_spec.json` describes, or hold it until a slot is free.

        `fn` is dropped, and that is the whole point of DEC-324 rather than an oversight: the
        callable and the spec are two renderings of one description, and only one of them can cross
        a process boundary. `del` says so at the top of the function, where a reader looks.
        """
        del fn  # the spec is the description; a closure cannot be shipped to a container (DEC-324)
        submitted = utc_now()
        try:
            spec = read_job_spec(self._storage, job_spec_key(job_id))
        except (StorageError, ValueError) as exc:
            log_failure(_LOGGER, "sagemaker.spec", exc)
            return self._record_failure(job_id, JOB_SPEC_UNREADABLE, submitted)
        with self._lock:
            held = len(self._running) >= self._config.max_concurrent_jobs
            if held:
                self._waiting.append(job_id)
        if held:
            self._mark_waiting(spec)
            return JobInfo(job_id=job_id, state=RunState.PENDING, submitted_at=submitted)
        return self._launch(spec, submitted)

    def status(self, job_id: str) -> JobInfo:
        """The job's state, from `Describe*` on a job named after the run; `KeyError` if unknown.

        A job still waiting for a slot is `pending`, and asking about it is also when this runner
        notices a slot has freed: there is no background thread here, so the poll the Running screen
        already makes is what moves the queue (DEC-335).
        """
        with self._lock:
            waiting = job_id in self._waiting
        if waiting:
            self._drain()
            with self._lock:
                if job_id in self._waiting:
                    return JobInfo(job_id=job_id, state=RunState.PENDING, submitted_at=utc_now())
        spec, job_name = self._identify(job_id)
        return self._describe(spec, job_name)

    def cancel(self, job_id: str) -> bool:
        """Ask SageMaker to stop the job; True iff it was still pending or running. Never raises."""
        with self._lock:
            if job_id in self._waiting:
                self._waiting.remove(job_id)
                self._finished.add(job_id)
                return True
        try:
            spec, job_name = self._identify(job_id)
        except KeyError:
            return False
        try:
            info = self._describe(spec, job_name)
            if info.state in TERMINAL_STATES:
                return False
            if spec.entrypoint is JobEntrypoint.TRAIN:
                self._sagemaker().stop_training_job(TrainingJobName=job_name)
            else:
                self._sagemaker().stop_processing_job(ProcessingJobName=job_name)
        except Exception as exc:  # a cancel that could not be delivered is "no", not a 500
            log_failure(_LOGGER, "sagemaker.cancel", exc)
            return False
        return True

    def shutdown(self, *, wait: bool = True) -> None:
        """Nothing to shut down: the jobs run in AWS and outlive this process on purpose.

        `wait=True` is *not* honoured by blocking, and must not be: a deployment that waited for
        every training job before it could restart would be a deployment that cannot be restarted.
        """
        del wait

    # -- ReconcilingJobRunner ------------------------------------------------
    def reconcile(self, job_id: str) -> JobInfo | None:
        """Write the ending a job that died never got to write; `None` when there is nothing to do.

        Three things have to be true before this touches anything: SageMaker says the job reached a
        terminal state, `status.json` still says it is running or pending, and that document has not
        been written for `RECONCILE_GRACE_SECONDS`. The third is what stops this from winning a race
        against a container that is at this moment writing the *better* answer - the stage the
        failure happened at, with the detail line that stage had earned (DEC-325).
        """
        try:
            spec, job_name = self._identify(job_id)
        except KeyError:
            return None
        try:
            info = self._describe(spec, job_name)
        except Exception as exc:  # a control plane this process cannot reach changes nothing
            log_failure(_LOGGER, "sagemaker.reconcile", exc)
            return None
        if info.state not in {RunState.FAILED, RunState.CANCELLED}:
            return None
        try:
            status = self._storage.read_model(run_key(spec.run_id, STATUS_FILENAME), RunStatus)
        except (StorageError, ValueError) as exc:
            log_failure(_LOGGER, "sagemaker.reconcile", exc)
            return None
        if status.state in TERMINAL_STATES:
            return None
        if utc_now() - status.updated_at < timedelta(seconds=RECONCILE_GRACE_SECONDS):
            return None
        if info.state is RunState.CANCELLED:
            cancel_run(self._storage, spec.run_id)
        else:
            fail_run(self._storage, spec.run_id, _remote_failure())
        _LOGGER.warning("sagemaker.reconciled run=%s state=%s", spec.run_id, info.state.value)
        return info

    # -- internals -----------------------------------------------------------
    def _identify(self, job_id: str) -> tuple[JobSpec, str]:
        """The spec and job name for `job_id`, from memory or from the store; `KeyError` if neither.

        The store is the fallback rather than the exception, because this process is not the only
        thing that has ever existed: a task that restarts mid-run still has to answer for the jobs
        it launched, and `job_spec.json` is where the answer was left (DEC-328).
        """
        with self._lock:
            launched = self._running.get(job_id)
        if launched is not None:
            return launched.spec, launched.job_name
        try:
            spec = read_job_spec(self._storage, job_spec_key(job_id))
        except (StorageError, ValueError) as exc:
            raise KeyError(job_id) from exc
        return spec, job_name_for(spec, prefix=self._config.job_name_prefix)

    def _launch(self, spec: JobSpec, submitted: datetime) -> JobInfo:
        """Create the job, remember it, and record where the compute is meant to be running."""
        backend = BACKEND_FOR_ENTRYPOINT[spec.entrypoint]
        job_name = job_name_for(spec, prefix=self._config.job_name_prefix)
        claimed = spec.model_copy(update={"backend": backend})
        spec_key = write_job_spec(self._storage, claimed)
        try:
            if spec.entrypoint is JobEntrypoint.TRAIN:
                request = training_request(claimed, self._config, spec_key=spec_key, job_name=job_name)
                self._sagemaker().create_training_job(**request)
            else:
                request = processing_request(claimed, self._config, spec_key=spec_key, job_name=job_name)
                self._sagemaker().create_processing_job(**request)
        except SageMakerError as exc:
            _LOGGER.warning("sagemaker.submit refused code=%s job=%s", exc.code, job_name)
            return self._record_failure(spec.job_id, JOB_SUBMIT_FAILED, submitted, message=exc.message)
        except Exception as exc:
            log_failure(_LOGGER, "sagemaker.submit", exc)
            return self._record_failure(spec.job_id, JOB_SUBMIT_FAILED, submitted)
        with self._lock:
            self._running[spec.job_id] = _Launched(spec=claimed, job_name=job_name, submitted_at=submitted)
        _LOGGER.info("sagemaker.submitted job=%s kind=%s", job_name, backend)
        return JobInfo(job_id=spec.job_id, state=RunState.PENDING, submitted_at=submitted)

    def _mark_waiting(self, spec: JobSpec) -> None:
        """Say on the run itself that it is queued: `pending`, with a detail line (DEC-335)."""
        try:
            update_stage(
                self._storage,
                spec.run_id,
                StageKey.INGEST,
                state=RunState.PENDING,
                detail=WAITING_FOR_COMPUTE,
            )
        except (StorageError, ValueError) as exc:  # the queue is real whether or not it can be said
            log_failure(_LOGGER, "sagemaker.queue", exc)

    def _drain(self) -> None:
        """Release finished jobs and launch as many waiting ones as there are free slots."""
        with self._lock:
            running = dict(self._running)
        for job_id, launched in running.items():
            try:
                info = self._describe(launched.spec, launched.job_name)
            except Exception as exc:  # an unanswerable job holds its slot rather than losing it
                log_failure(_LOGGER, "sagemaker.poll", exc)
                continue
            if info.state in TERMINAL_STATES:
                with self._lock:
                    self._running.pop(job_id, None)
                    self._finished.add(job_id)
        while True:
            with self._lock:
                free = self._config.max_concurrent_jobs - len(self._running)
                if free <= 0 or not self._waiting:
                    return
                job_id = self._waiting.pop(0)
            try:
                spec = read_job_spec(self._storage, job_spec_key(job_id))
            except (StorageError, ValueError) as exc:
                log_failure(_LOGGER, "sagemaker.spec", exc)
                self._record_failure(job_id, JOB_SPEC_UNREADABLE, utc_now())
                continue
            self._launch(spec, utc_now())

    def _describe(self, spec: JobSpec, job_name: str) -> JobInfo:
        """One `Describe*` call, as a `JobInfo`, recording the compute when the job has ended."""
        if spec.entrypoint is JobEntrypoint.TRAIN:
            description = self._sagemaker().describe_training_job(TrainingJobName=job_name)
            raw = str(description.get("TrainingJobStatus", ""))
            secondary = str(description.get("SecondaryStatus", ""))
        else:
            description = self._sagemaker().describe_processing_job(ProcessingJobName=job_name)
            raw = str(description.get("ProcessingJobStatus", ""))
            secondary = ""
        state = _state_for(raw, secondary)
        info = JobInfo(
            job_id=spec.job_id,
            state=state,
            submitted_at=_moment(description.get("CreationTime")) or utc_now(),
            started_at=_moment(_started(description)),
            ended_at=_moment(_ended(description)),
            error=(ENGINE_ERRORS[JOB_FAILED_REMOTELY][0] if state is RunState.FAILED else None),
        )
        if state in TERMINAL_STATES:
            self._record_compute(spec, job_name, description)
        return info

    def _record_failure(
        self, job_id: str, code: str, submitted: datetime, *, message: str | None = None
    ) -> JobInfo:
        """Write a failure that happened before any stage could, and report it as the job's state."""
        error = RunError(code=code, message=message or ENGINE_ERRORS[code][0], stage=None)
        try:
            fail_run(self._storage, job_id, error)
        except (StorageError, ValueError) as exc:
            log_failure(_LOGGER, "sagemaker.fail", exc)
        return JobInfo(
            job_id=job_id,
            state=RunState.FAILED,
            submitted_at=submitted,
            ended_at=utc_now(),
            error=error.message,
        )

    def _record_compute(self, spec: JobSpec, job_name: str, description: Mapping[str, Any]) -> None:
        """Put where the compute ran, and what it cost at list price, onto `run_manifest.json`.

        Only onto an existing manifest: the pipeline writes one for every run it *starts*, and a job
        that never reached the container has none - inventing one here would mean inventing a
        dataset fingerprint and a seed for a run that never read any data (DEC-329).

        It runs at most once per job: the manifest is rewritten only while this process still
        remembers launching the job, so a poll after a restart reads rather than rewrites.
        """
        with self._lock:
            if spec.job_id in self._finished:
                return
            self._finished.add(spec.job_id)
        compute = _compute_info(spec, job_name, description, self._config)
        key = run_key(spec.run_id, RUN_MANIFEST_FILENAME)
        try:
            manifest = self._storage.read_model(key, RunManifest)
        except (StorageError, ValueError):
            return  # no manifest: the run never got far enough to have one
        estimate = cost_estimate(
            compute, table=self._price_table(), compute_seconds=manifest.cost_estimate.compute_seconds
        )
        self._storage.write_model(
            key, manifest.model_copy(update={"compute": compute, "cost_estimate": estimate})
        )

    def _price_table(self) -> PriceTable | None:
        """The rate card, read once per runner. `None` is a normal answer (see `engine.aws.prices`)."""
        if not self._prices_loaded:
            self._prices = load_price_table()
            self._prices_loaded = True
        return self._prices


# ---------------------------------------------------------------------------
# Reading a description
# ---------------------------------------------------------------------------
def _state_for(raw: str, secondary: str) -> RunState:
    """One SageMaker status as a `RunState`; an unknown status is `running`, never terminal.

    An unrecognised status can only be one AWS has added since this table was written, and the safe
    reading of "I do not know what this means" is "it has not finished": a run wrongly called failed
    is a run a user is told the wrong thing about, while a run wrongly called running is corrected by
    the next poll.
    """
    state = JOB_STATES.get(raw, RunState.RUNNING)
    if state is RunState.RUNNING and secondary in STARTING_SECONDARY_STATES:
        return RunState.PENDING
    return state


def _started(description: Mapping[str, Any]) -> Any:
    """`TrainingStartTime` or `ProcessingStartTime`, whichever this description carries."""
    return description.get("TrainingStartTime") or description.get("ProcessingStartTime")


def _ended(description: Mapping[str, Any]) -> Any:
    """`TrainingEndTime` or `ProcessingEndTime`, whichever this description carries."""
    return description.get("TrainingEndTime") or description.get("ProcessingEndTime")


def _moment(value: Any) -> datetime | None:
    """A boto3 timestamp as an aware UTC datetime, or `None` when the field was absent."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _compute_info(
    spec: JobSpec, job_name: str, description: Mapping[str, Any], config: SageMakerJobConfig
) -> ComputeInfo:
    """Where this job ran, from its own description.

    `billable_seconds` is filled from `DescribeTrainingJob.BillableTimeInSeconds` and from nowhere
    else. A processing job's start and end times are wall clock - the service does not report a
    billable time for one - so they go to `duration_s` and the billable field stays null rather
    than being filled with a number that means something different (DEC-332).

    `backend` is `sagemaker` for both job kinds, because that is what `ComputeBackend` says and a
    reader of the manifest is asking what carried the run. Which of the two rate cards it is billed
    under is `entrypoint`: train is a Training job, score is a Processing job, and the price table
    is read with that rather than with a second spelling of the backend.
    """
    started, ended = _moment(_started(description)), _moment(_ended(description))
    wall_clock = (ended - started).total_seconds() if started is not None and ended is not None else None
    billable = description.get("BillableTimeInSeconds")
    resources = description.get("ResourceConfig") or (
        (description.get("ProcessingResources") or {}).get("ClusterConfig") or {}
    )
    return ComputeInfo(
        backend=ComputeBackend.SAGEMAKER,
        entrypoint=spec.entrypoint,
        job_name=job_name,
        job_arn=_arn(description),
        instance_type=str(resources.get("InstanceType") or config.instance_type_for(spec.entrypoint)),
        instance_count=int(resources.get("InstanceCount") or config.instance_count),
        region=config.region,
        image_uri=config.image_uri,
        # `duration_s` is required and means "seconds the compute was occupied". A description with
        # no end time yet has not occupied it for a knowable number of seconds, and 0.0 is the
        # honest reading of "nothing has been measured" - the same answer `CostEstimate` gives.
        duration_s=wall_clock if wall_clock is not None else 0.0,
        billable_seconds=float(billable) if isinstance(billable, (int, float)) else None,
        billable_seconds_source=(
            "DescribeTrainingJob.BillableTimeInSeconds" if isinstance(billable, (int, float)) else None
        ),
    )


def _arn(description: Mapping[str, Any]) -> str | None:
    """The job ARN under whichever of the two names this description uses."""
    value = description.get("TrainingJobArn") or description.get("ProcessingJobArn")
    return str(value) if value else None


def _remote_failure() -> RunError:
    """The failure reconciliation writes: coded, and in this product's own words.

    Never `FailureReason`. AWS fills that field with the container's last output, which for this
    container can be a library's exception message - the most likely place a value out of a
    customer's file appears in text (plan section 13.7, DEC-331).
    """
    return RunError(code=JOB_FAILED_REMOTELY, message=ENGINE_ERRORS[JOB_FAILED_REMOTELY][0], stage=None)


def _check_no_secret_is_shipped() -> None:
    """`SHIPPED_SETTINGS_FIELDS` may never name a secret; asserted by a test, stated here."""
    leaked = sorted(set(SHIPPED_SETTINGS_FIELDS) & SECRET_FIELDS)
    if leaked:  # pragma: no cover - the unit test is the real guard
        raise RuntimeError(f"secret settings must not be shipped to a container: {leaked}")


_check_no_secret_is_shipped()
