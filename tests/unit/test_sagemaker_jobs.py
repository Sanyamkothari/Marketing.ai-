"""`SageMakerJobRunner`: the requests it builds, the statuses it reads, and what it refuses to invent.

The request builders are pure functions of a spec and a configuration, so most of this file needs
no client at all - which is the point of their being pure. What needs one gets
`tests.fakes.fake_sagemaker.FakeSageMakerClient`, whose `__getattr__` raises: a `List*` call is a
named failure rather than a silently tolerated one, which is what turns DEC-334 from a comment into
a check.

Four properties get a section of their own because each of them is a rule that is easy to break by
accident and expensive to have broken:

* **No billed quantity is invented.** A volume size and a runtime limit are money, so they are
  absent from the request unless the deployment set them - and where the *service* has no default
  either (`CreateProcessingJob.ClusterConfig.VolumeSizeInGB`), the job is refused rather than sized
  by this repository (DEC-336).
* **No secret is shipped.** `SHIPPED_SETTINGS_FIELDS` is an allow-list, and anybody with read access
  to the account can call `DescribeTrainingJob` (DEC-337).
* **A queue is not a state.** `RunState` has no `queued` member and DEC-027 says there is one
  vocabulary, so a held-back run is `pending` with a detail line (DEC-335).
* **Reconciliation never wins a race it should lose.** The container may be writing the better
  answer at this moment, so a status document that is not yet stale is left alone (DEC-325).

The failure mapping is exercised with **constructed** `botocore` errors rather than errors provoked
out of a fake, for the reason `tests/unit/test_s3_storage.py` gives at more length: a constructed
error is the wire shape botocore itself produces, which is the thing being handled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from engine.aws.sagemaker_jobs import (
    BACKEND_FOR_ENTRYPOINT,
    CONTAINER_ENTRYPOINT,
    JOB_SPEC_KEY_ENV_VAR,
    JOB_STATES,
    MAX_JOB_NAME_LENGTH,
    PROCESSING_BACKEND,
    RECONCILE_GRACE_SECONDS,
    SHIPPED_SETTINGS_FIELDS,
    STARTING_SECONDARY_STATES,
    TRAINING_BACKEND,
    WAITING_FOR_COMPUTE,
    SageMakerError,
    SageMakerJobConfig,
    SageMakerJobRunner,
    container_environment,
    job_name_for,
    processing_request,
    training_request,
)
from engine.contracts import (
    ComputeBackend,
    CostEstimate,
    DatasetFingerprint,
    JobEntrypoint,
    RunManifest,
    RunRecord,
    RunState,
    RunStatus,
    StageKey,
)
from engine.errors import JOB_FAILED_REMOTELY, JOB_SPEC_UNREADABLE, JOB_SUBMIT_FAILED
from engine.jobs import CancelToken, JobRunner, ReconcilingJobRunner
from engine.pipeline import STATUS_FILENAME
from engine.registry import LocalModelRegistry
from engine.runs import RUN_MANIFEST_FILENAME, job_spec_key, update_stage, write_job_spec
from engine.settings import ENV_VARS, SECRET_FIELDS, Settings
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.fakes.fake_sagemaker import (
    CREATE_PROCESSING_JOB,
    CREATE_TRAINING_JOB,
    EXIT_OK,
    FakeSageMakerClient,
)
from tests.unit.test_runs_module import (
    _Upload,
    make_run,
)

REGION = "ap-south-1"
BUCKET = "marketing-ai-jobs"
ROLE = "arn:aws:iam::000000000000:role/marketing-ai-job"
IMAGE = "000000000000.dkr.ecr.ap-south-1.amazonaws.com/marketing-ai:1"
TRAIN_INSTANCE = "ml.m5.xlarge"
PROCESSING_INSTANCE = "ml.m5.large"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def make_settings(**overrides: Any) -> Settings:
    """A deployment that runs its jobs on SageMaker; `Settings` refuses an incomplete one itself."""
    values: dict[str, Any] = {
        "env": "dev",
        "aws_region": REGION,
        "storage_backend": "s3",
        "s3_bucket": BUCKET,
        "s3_prefix": "artefacts",
        "job_backend": "sagemaker",
        "sagemaker_role_arn": ROLE,
        "sagemaker_image_uri": IMAGE,
        "sagemaker_instance_type": TRAIN_INSTANCE,
        "sagemaker_processing_instance_type": PROCESSING_INSTANCE,
        "client_id": "acme",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def config(settings: Settings) -> SageMakerJobConfig:
    return SageMakerJobConfig.from_settings(settings)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    """A local store, because what the runner does with the store is read and write documents."""
    return LocalStorage(tmp_path / "data")


@pytest.fixture
def registry(tmp_path: Path) -> LocalModelRegistry:
    return LocalModelRegistry(tmp_path / "registry.db")


@pytest.fixture
def spec_for(storage: LocalStorage, registry: LocalModelRegistry, config_root: Path) -> Any:
    """A factory for a real run directory plus its `job_spec.json`, in the store."""
    from engine.config import get_catalog, resolve_config
    from engine.runs import job_spec_for

    def build(mode: str = "train", *, client_id: str | None = "acme") -> Any:
        from engine.config import RunMode

        run_mode = RunMode.TRAIN if mode == "train" else RunMode.SCORE
        resolved = resolve_config("targeted-advertisement", {}, root=config_root)
        record = make_run(
            storage,
            registry,
            resolved,
            get_catalog(config_root),
            mode=run_mode,
            model_version_id=None if run_mode is RunMode.TRAIN else "m_x_1",
        )
        spec = job_spec_for(record, upload=_Upload(), client_id=client_id)
        write_job_spec(storage, spec)
        return spec

    return build


# ---------------------------------------------------------------------------
# SageMakerJobConfig
# ---------------------------------------------------------------------------
def test_from_settings_reads_the_deployment(config: SageMakerJobConfig) -> None:
    assert config.role_arn == ROLE
    assert config.image_uri == IMAGE
    assert config.train_instance_type == TRAIN_INSTANCE
    assert config.processing_instance_type == PROCESSING_INSTANCE
    assert config.region == REGION
    assert config.output_s3_uri == f"s3://{BUCKET}/artefacts/sagemaker/"
    assert config.instance_count == 1


def test_the_two_instance_types_are_two_costs(config: SageMakerJobConfig) -> None:
    assert config.instance_type_for(JobEntrypoint.TRAIN) == TRAIN_INSTANCE
    assert config.instance_type_for(JobEntrypoint.SCORE) == PROCESSING_INSTANCE


def test_the_billed_quantities_are_none_until_a_deployment_says_otherwise(
    config: SageMakerJobConfig,
) -> None:
    """A volume size and a runtime limit are money; nobody here has measured either (DEC-336)."""
    assert config.volume_size_gb is None
    assert config.max_runtime_seconds is None


def test_a_configuration_built_from_the_wrong_settings_names_the_missing_setting() -> None:
    local = Settings()
    with pytest.raises(SageMakerError) as caught:
        SageMakerJobConfig.from_settings(local)
    assert caught.value.code == JOB_SUBMIT_FAILED
    assert "s3_bucket" in caught.value.message
    assert ENV_VARS["s3_bucket"] in caught.value.message


# ---------------------------------------------------------------------------
# The container's environment
# ---------------------------------------------------------------------------
def test_every_shipped_field_is_a_real_field_with_a_real_variable() -> None:
    """An allow-list entry that names nothing is an AttributeError at job submission.

    `container_environment` reads each name off `Settings` with `getattr` and renders it through
    `ENV_VARS`, so a name that is no longer a field - a field renamed in a later phase, say - does
    not fail here or at import: it fails on the first `POST /runs` of a SageMaker deployment, which
    is the worst place to find out. This test moves that to collection time.
    """
    fields = set(Settings.model_fields)
    for name in SHIPPED_SETTINGS_FIELDS:
        assert name in fields, f"SHIPPED_SETTINGS_FIELDS names {name!r}, which is not a Settings field"
        assert name in ENV_VARS, f"{name!r} is a field but has no environment variable to ship it in"


def test_no_secret_is_ever_shipped_to_a_container(settings: Settings) -> None:
    """`DescribeTrainingJob` is readable by anybody with read access to the account (DEC-337)."""
    assert set(SHIPPED_SETTINGS_FIELDS) & SECRET_FIELDS == set()
    environment = container_environment(settings, {})
    for field_name in SECRET_FIELDS:
        assert ENV_VARS[field_name] not in environment


def test_the_container_is_told_where_the_artefacts_are(settings: Settings) -> None:
    environment = container_environment(settings, {})
    assert environment[ENV_VARS["storage_backend"]] == "s3"
    assert environment[ENV_VARS["s3_bucket"]] == BUCKET
    assert environment[ENV_VARS["aws_region"]] == REGION
    assert environment[ENV_VARS["client_id"]] == "acme"


def test_the_container_is_not_told_how_to_submit_jobs(settings: Settings) -> None:
    """It submits nothing, so a description of how to submit is not its business."""
    environment = container_environment(settings, {})
    assert not [name for name in environment if "SAGEMAKER" in name]


def test_an_absent_setting_ships_no_variable_at_all(settings: Settings) -> None:
    """A container that reads an empty string would believe it; an absent name it cannot."""
    without = make_settings(client_id=None)
    assert ENV_VARS["client_id"] not in container_environment(without, {})


def test_the_settings_source_is_copied_from_this_process(settings: Settings) -> None:
    """A task reading its configuration from Parameter Store launches jobs that do the same."""
    assert (
        container_environment(settings, {"MARKETING_AI_SETTINGS_SOURCE": "aws"})[
            "MARKETING_AI_SETTINGS_SOURCE"
        ]
        == "aws"
    )
    assert "MARKETING_AI_SETTINGS_SOURCE" not in container_environment(settings, {})


# ---------------------------------------------------------------------------
# The request builders
# ---------------------------------------------------------------------------
def test_a_training_run_becomes_a_training_job(config: SageMakerJobConfig, spec_for: Any) -> None:
    spec = spec_for("train")
    request = training_request(spec, config, spec_key="runs/x/job_spec.json")

    assert request["TrainingJobName"] == job_name_for(spec, prefix=config.job_name_prefix)
    assert request["RoleArn"] == ROLE
    assert request["AlgorithmSpecification"]["TrainingImage"] == IMAGE
    assert request["AlgorithmSpecification"]["ContainerEntrypoint"] == list(CONTAINER_ENTRYPOINT)
    assert request["ResourceConfig"]["InstanceType"] == TRAIN_INSTANCE
    assert request["ResourceConfig"]["InstanceCount"] == 1
    assert request["Environment"][JOB_SPEC_KEY_ENV_VAR] == "runs/x/job_spec.json"


def test_a_scoring_run_becomes_a_processing_job(spec_for: Any) -> None:
    """Not a training job: a training job is the one SageMaker resource that carries a model."""
    config = SageMakerJobConfig.from_settings(make_settings(sagemaker_volume_size_gb=30))
    spec = spec_for("score")
    request = processing_request(spec, config, spec_key="runs/x/job_spec.json")

    assert request["ProcessingJobName"] == job_name_for(spec, prefix=config.job_name_prefix)
    assert request["AppSpecification"]["ImageUri"] == IMAGE
    assert request["AppSpecification"]["ContainerEntrypoint"] == list(CONTAINER_ENTRYPOINT)
    cluster = request["ProcessingResources"]["ClusterConfig"]
    assert cluster["InstanceType"] == PROCESSING_INSTANCE
    assert cluster["VolumeSizeInGB"] == 30


def test_the_entrypoint_decides_the_job_kind() -> None:
    assert BACKEND_FOR_ENTRYPOINT[JobEntrypoint.TRAIN] == TRAINING_BACKEND
    assert BACKEND_FOR_ENTRYPOINT[JobEntrypoint.SCORE] == PROCESSING_BACKEND
    assert set(BACKEND_FOR_ENTRYPOINT) == set(JobEntrypoint)


def test_every_job_carries_the_four_cost_allocation_tags(config: SageMakerJobConfig, spec_for: Any) -> None:
    spec = spec_for("train")
    tags = {item["Key"]: item["Value"] for item in training_request(spec, config, spec_key="k")["Tags"]}
    assert tags == {
        "product": "marketing-ai",
        "client": "acme",
        "use_case": spec.use_case_id,
        "run_id": spec.run_id,
    }


def test_a_tag_aws_would_refuse_is_dropped_rather_than_sent(
    config: SageMakerJobConfig, spec_for: Any
) -> None:
    spec = spec_for("train").model_copy(update={"tags": {"ok": "yes", "not ok!": "*"}})
    tags = {item["Key"]: item["Value"] for item in training_request(spec, config, spec_key="k")["Tags"]}
    assert tags == {"ok": "yes"}


def test_the_job_name_is_derived_from_the_run_id(config: SageMakerJobConfig, spec_for: Any) -> None:
    """Derived, so every question can be asked by name and no `List*` is ever needed (DEC-334)."""
    spec = spec_for("train")
    name = job_name_for(spec, prefix="marketing-ai")

    assert name.startswith("marketing-ai-train-")
    assert set(name) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
    assert len(name) <= MAX_JOB_NAME_LENGTH
    assert job_name_for(spec, prefix="marketing-ai") == name, "the same spec always names the same job"


def test_a_long_prefix_keeps_the_tail_that_carries_the_run_id(spec_for: Any) -> None:
    spec = spec_for("train")
    name = job_name_for(spec, prefix="x" * 80)

    assert len(name) == MAX_JOB_NAME_LENGTH
    assert name.endswith(spec.run_id.replace("_", "-"))


# ---------------------------------------------------------------------------
# What is never invented (DEC-336)
# ---------------------------------------------------------------------------
def test_a_training_request_omits_the_volume_size_until_it_is_set(
    config: SageMakerJobConfig, spec_for: Any
) -> None:
    """`CreateTrainingJob` publishes a default; a published default beats an invented number."""
    request = training_request(spec_for("train"), config, spec_key="k")
    assert "VolumeSizeInGB" not in request["ResourceConfig"]
    assert request["StoppingCondition"] == {}


def test_a_training_request_sends_what_the_deployment_did_set(spec_for: Any) -> None:
    config = SageMakerJobConfig.from_settings(
        make_settings(sagemaker_volume_size_gb=50, sagemaker_max_runtime_seconds=900)
    )
    request = training_request(spec_for("train"), config, spec_key="k")
    assert request["ResourceConfig"]["VolumeSizeInGB"] == 50
    assert request["StoppingCondition"] == {"MaxRuntimeInSeconds": 900}


def test_a_scoring_job_is_refused_rather_than_sized_by_this_repository(
    config: SageMakerJobConfig, spec_for: Any
) -> None:
    """`ClusterConfig.VolumeSizeInGB` is required and has no service default, so there is no fallback."""
    with pytest.raises(SageMakerError) as caught:
        processing_request(spec_for("score"), config, spec_key="k")

    assert caught.value.code == JOB_SUBMIT_FAILED
    assert ENV_VARS["sagemaker_volume_size_gb"] in caught.value.message


def test_a_vpc_is_configured_only_when_the_deployment_asked_for_one(spec_for: Any) -> None:
    plain = SageMakerJobConfig.from_settings(make_settings())
    assert "VpcConfig" not in training_request(spec_for("train"), plain, spec_key="k")

    fenced = SageMakerJobConfig.from_settings(
        make_settings(sagemaker_subnet_ids=("subnet-a", "subnet-b"), sagemaker_security_group_ids=("sg-a",))
    )
    request = training_request(spec_for("train"), fenced, spec_key="k")
    assert request["VpcConfig"] == {"Subnets": ["subnet-a", "subnet-b"], "SecurityGroupIds": ["sg-a"]}


def test_a_customer_managed_key_reaches_both_the_volume_and_the_output(spec_for: Any) -> None:
    config = SageMakerJobConfig.from_settings(make_settings(s3_kms_key_id="alias/marketing-ai"))
    request = training_request(spec_for("train"), config, spec_key="k")
    assert request["ResourceConfig"]["VolumeKmsKeyId"] == "alias/marketing-ai"
    assert request["OutputDataConfig"]["KmsKeyId"] == "alias/marketing-ai"


# ---------------------------------------------------------------------------
# The status vocabulary
# ---------------------------------------------------------------------------
def test_the_table_covers_the_whole_sagemaker_vocabulary() -> None:
    """Both APIs share this vocabulary exactly, which is why one table serves both."""
    assert set(JOB_STATES) == {"InProgress", "Stopping", "Completed", "Failed", "Stopped"}
    assert JOB_STATES["Completed"] is RunState.DONE
    assert JOB_STATES["Failed"] is RunState.FAILED
    assert JOB_STATES["Stopped"] is RunState.CANCELLED


def test_stopping_is_running_not_cancelled() -> None:
    """A stop has been asked for and the container is still executing; it can still come back."""
    assert JOB_STATES["Stopping"] is RunState.RUNNING


@pytest.mark.parametrize("secondary", sorted(STARTING_SECONDARY_STATES))
def test_a_job_whose_instance_is_still_launching_is_pending(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any, secondary: str
) -> None:
    spec = spec_for("train")
    client = _StubClient({"TrainingJobStatus": "InProgress", "SecondaryStatus": secondary})
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)

    assert runner.status(spec.job_id).state is RunState.PENDING


def test_a_status_this_table_does_not_know_is_running_never_terminal(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    """A run wrongly called failed misinforms a user; one wrongly called running self-corrects."""
    spec = spec_for("train")
    client = _StubClient({"TrainingJobStatus": "SomethingAwsAddedLater", "SecondaryStatus": ""})
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)

    assert runner.status(spec.job_id).state is RunState.RUNNING


def test_a_failed_job_reports_this_products_words_and_never_the_failure_reason(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    """`FailureReason` is the container's last output, which can carry a customer value (13.7)."""
    spec = spec_for("train")
    client = _StubClient(
        {
            "TrainingJobStatus": "Failed",
            "SecondaryStatus": "Failed",
            "FailureReason": "ValueError: could not convert 'ACME-042'",
        }
    )
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)

    info = runner.status(spec.job_id)
    assert info.state is RunState.FAILED
    assert info.error is not None
    assert "ACME-042" not in info.error


class _StubClient:
    """A `Describe*` that always answers the same thing; the smallest control plane there is."""

    def __init__(self, description: dict[str, Any]) -> None:
        self.description = description
        self.stopped: list[str] = []

    def describe_training_job(self, **kwargs: Any) -> dict[str, Any]:
        return {"TrainingJobName": kwargs["TrainingJobName"], **self.description}

    def describe_processing_job(self, **kwargs: Any) -> dict[str, Any]:
        return {"ProcessingJobName": kwargs["ProcessingJobName"], **self.description}

    def stop_training_job(self, **kwargs: Any) -> dict[str, Any]:
        self.stopped.append(str(kwargs["TrainingJobName"]))
        return {}

    def stop_processing_job(self, **kwargs: Any) -> dict[str, Any]:
        self.stopped.append(str(kwargs["ProcessingJobName"]))
        return {}

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"this test's runner may not call {name!r}")


# ---------------------------------------------------------------------------
# The runner: submit, status, cancel
# ---------------------------------------------------------------------------
def test_the_runner_is_a_job_runner_and_a_reconciling_one(
    storage: LocalStorage, config: SageMakerJobConfig
) -> None:
    runner = SageMakerJobRunner(config=config, storage=storage, client=_StubClient({}))
    assert isinstance(runner, JobRunner)
    assert isinstance(runner, ReconcilingJobRunner)


def test_submit_ships_the_spec_key_and_drops_the_closure(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    """The callable and the spec are two renderings of one description (DEC-324)."""
    spec = spec_for("train")
    called: list[str] = []
    client = FakeSageMakerClient(body=lambda env, cancel: _record(called, env, cancel))
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)

    def fn(cancel: CancelToken) -> None:  # pragma: no cover - the whole point is that it is not run
        raise AssertionError("a closure cannot cross a process boundary")

    info = runner.submit(spec.job_id, fn)
    client.wait_all()

    assert info.state is RunState.PENDING
    request = client.last(CREATE_TRAINING_JOB)
    assert request.environment[JOB_SPEC_KEY_ENV_VAR] == job_spec_key(spec.run_id)
    assert called == [job_spec_key(spec.run_id)]


def _record(sink: list[str], environment: Any, cancel: CancelToken) -> int:
    del cancel
    sink.append(str(environment[JOB_SPEC_KEY_ENV_VAR]))
    return EXIT_OK


def test_the_launched_spec_records_which_backend_claimed_it(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    spec = spec_for("train")
    client = FakeSageMakerClient(body=lambda env, cancel: EXIT_OK)
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)
    runner.submit(spec.job_id, _unused)
    client.wait_all()

    from engine.runs import read_job_spec

    assert read_job_spec(storage, job_spec_key(spec.run_id)).backend == TRAINING_BACKEND


def test_a_scoring_run_is_submitted_as_a_processing_job(storage: LocalStorage, spec_for: Any) -> None:
    config = SageMakerJobConfig.from_settings(make_settings(sagemaker_volume_size_gb=30))
    spec = spec_for("score")
    client = FakeSageMakerClient(body=lambda env, cancel: EXIT_OK)
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)

    runner.submit(spec.job_id, _unused)
    client.wait_all()

    assert CREATE_PROCESSING_JOB in client.apis()
    assert CREATE_TRAINING_JOB not in client.apis()


def test_a_submit_the_service_refuses_fails_the_run_rather_than_leaving_it_pending(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    """A scoring job with no volume size is refused before the wire; the run has to hear about it."""
    spec = spec_for("score")
    runner = SageMakerJobRunner(config=config, storage=storage, client=FakeSageMakerClient())

    info = runner.submit(spec.job_id, _unused)

    assert info.state is RunState.FAILED
    status = storage.read_model(run_key(spec.run_id, STATUS_FILENAME), RunStatus)
    assert status.state is RunState.FAILED


def test_a_submit_the_control_plane_rejects_becomes_a_coded_failure(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    """A constructed `ClientError`: the wire shape botocore itself produces (see the module docstring)."""
    from botocore.exceptions import ClientError

    spec = spec_for("train")
    error = ClientError(
        {"Error": {"Code": "ResourceLimitExceeded", "Message": "account limit"}}, "CreateTrainingJob"
    )

    class _Refusing(_StubClient):
        def create_training_job(self, **kwargs: Any) -> dict[str, Any]:
            raise error

    runner = SageMakerJobRunner(config=config, storage=storage, client=_Refusing({}))
    info = runner.submit(spec.job_id, _unused)

    assert info.state is RunState.FAILED
    record = storage.read_model(run_key(spec.run_id, "run.json"), RunRecord)
    assert record.error is not None
    assert record.error.code == JOB_SUBMIT_FAILED
    assert "account limit" not in record.error.message


def test_a_job_whose_spec_cannot_be_read_is_failed_with_its_own_code(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    spec = spec_for("train")
    storage.delete(job_spec_key(spec.run_id))
    runner = SageMakerJobRunner(config=config, storage=storage, client=FakeSageMakerClient())

    info = runner.submit(spec.job_id, _unused)

    assert info.state is RunState.FAILED
    record = storage.read_model(run_key(spec.run_id, "run.json"), RunRecord)
    assert record.error is not None
    assert record.error.code == JOB_SPEC_UNREADABLE


def test_status_of_a_job_this_process_never_launched_reads_the_store(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    """A task that restarts mid-run still has to answer for the jobs it launched (DEC-328)."""
    spec = spec_for("train")
    fresh = SageMakerJobRunner(
        config=config, storage=storage, client=_StubClient({"TrainingJobStatus": "Completed"})
    )

    assert fresh.status(spec.job_id).state is RunState.DONE


def test_status_of_an_unknown_job_is_a_key_error(storage: LocalStorage, config: SageMakerJobConfig) -> None:
    runner = SageMakerJobRunner(config=config, storage=storage, client=_StubClient({}))
    with pytest.raises(KeyError):
        runner.status("r_nothing")


def test_cancel_asks_the_service_to_stop_the_named_job(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    spec = spec_for("train")
    client = _StubClient({"TrainingJobStatus": "InProgress", "SecondaryStatus": "Training"})
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)

    assert runner.cancel(spec.job_id) is True
    assert client.stopped == [job_name_for(spec, prefix=config.job_name_prefix)]


def test_cancelling_a_finished_job_is_false_and_stops_nothing(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    spec = spec_for("train")
    client = _StubClient({"TrainingJobStatus": "Completed"})
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)

    assert runner.cancel(spec.job_id) is False
    assert client.stopped == []


def test_cancelling_an_unknown_job_is_false_and_never_raises(
    storage: LocalStorage, config: SageMakerJobConfig
) -> None:
    runner = SageMakerJobRunner(config=config, storage=storage, client=_StubClient({}))
    assert runner.cancel("r_nothing") is False


def test_shutdown_does_not_wait_for_jobs_that_run_in_aws(
    storage: LocalStorage, config: SageMakerJobConfig
) -> None:
    """A deployment that waited for every training job could not be restarted."""
    runner = SageMakerJobRunner(config=config, storage=storage, client=_StubClient({}))
    runner.shutdown(wait=True)


def _unused(cancel: CancelToken) -> None:  # pragma: no cover - never called
    raise AssertionError("the spec is the description; the closure is dropped")


# ---------------------------------------------------------------------------
# The queue: a queue is not a state (DEC-335)
# ---------------------------------------------------------------------------
def test_a_run_held_back_by_the_limit_is_pending_with_a_detail_line(
    storage: LocalStorage, spec_for: Any
) -> None:
    """`RunState` grows no `queued` member for one backend; DEC-027 says there is one vocabulary."""
    assert "queued" not in {state.value for state in RunState}

    config = SageMakerJobConfig.from_settings(make_settings(sagemaker_max_concurrent_jobs=1))
    client = FakeSageMakerClient(autostart=False)
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)
    first, second = spec_for("train"), spec_for("train")

    runner.submit(first.job_id, _unused)
    info = runner.submit(second.job_id, _unused)

    assert info.state is RunState.PENDING
    assert len(client.requests(CREATE_TRAINING_JOB)) == 1, "the second job was not created"
    status = storage.read_model(run_key(second.run_id, STATUS_FILENAME), RunStatus)
    assert status.stages[0].detail == WAITING_FOR_COMPUTE
    assert status.state is RunState.PENDING


def test_a_waiting_run_is_launched_once_a_slot_frees(storage: LocalStorage, spec_for: Any) -> None:
    """There is no background thread here: the poll the Running screen makes is what moves the queue."""
    config = SageMakerJobConfig.from_settings(make_settings(sagemaker_max_concurrent_jobs=1))
    client = FakeSageMakerClient(body=lambda env, cancel: EXIT_OK)
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)
    first, second = spec_for("train"), spec_for("train")

    runner.submit(first.job_id, _unused)
    runner.submit(second.job_id, _unused)
    client.wait_all()

    runner.status(second.job_id)
    client.wait_all()
    assert len(client.requests(CREATE_TRAINING_JOB)) == 2


def test_cancelling_a_waiting_run_never_reaches_the_service(storage: LocalStorage, spec_for: Any) -> None:
    config = SageMakerJobConfig.from_settings(make_settings(sagemaker_max_concurrent_jobs=1))
    client = FakeSageMakerClient(autostart=False)
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)
    first, second = spec_for("train"), spec_for("train")

    runner.submit(first.job_id, _unused)
    runner.submit(second.job_id, _unused)

    assert runner.cancel(second.job_id) is True
    assert "StopTrainingJob" not in client.apis()


# ---------------------------------------------------------------------------
# Reconciliation (DEC-325)
# ---------------------------------------------------------------------------
def stale(storage: LocalStorage, run_id: str, *, seconds: int) -> None:
    """Backdate `status.json` so the grace period has or has not elapsed."""
    key = run_key(run_id, STATUS_FILENAME)
    status = storage.read_model(key, RunStatus)
    storage.write_model(key, status.model_copy(update={"updated_at": utc_now() - timedelta(seconds=seconds)}))


def test_reconciliation_writes_the_failure_a_dead_container_never_wrote(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    spec = spec_for("train")
    update_stage(storage, spec.run_id, StageKey.INGEST, state=RunState.RUNNING)
    stale(storage, spec.run_id, seconds=RECONCILE_GRACE_SECONDS + 60)
    runner = SageMakerJobRunner(
        config=config, storage=storage, client=_StubClient({"TrainingJobStatus": "Failed"})
    )

    info = runner.reconcile(spec.job_id)

    assert info is not None and info.state is RunState.FAILED
    record = storage.read_model(run_key(spec.run_id, "run.json"), RunRecord)
    assert record.state is RunState.FAILED
    assert record.error is not None and record.error.code == JOB_FAILED_REMOTELY


def test_reconciliation_leaves_a_status_document_that_is_still_being_written(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    """The container may be writing the better answer right now: the stage, with its detail line."""
    spec = spec_for("train")
    update_stage(storage, spec.run_id, StageKey.INGEST, state=RunState.RUNNING)
    runner = SageMakerJobRunner(
        config=config, storage=storage, client=_StubClient({"TrainingJobStatus": "Failed"})
    )

    assert runner.reconcile(spec.job_id) is None
    assert storage.read_model(run_key(spec.run_id, STATUS_FILENAME), RunStatus).state is RunState.RUNNING


def test_the_grace_period_says_it_is_chosen_rather_than_measured() -> None:
    """Plan section 13.3: a constant nobody measured has to say so where it is defined."""
    import engine.aws.sagemaker_jobs as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    block = source.split("RECONCILE_GRACE_SECONDS: Final[int]")[1]
    assert "chosen, not measured" in block.lower().replace("**", "")


def test_a_run_that_wrote_its_own_ending_is_never_overwritten(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    spec = spec_for("train")
    update_stage(storage, spec.run_id, StageKey.PREPARE, state=RunState.FAILED)
    stale(storage, spec.run_id, seconds=RECONCILE_GRACE_SECONDS + 60)
    runner = SageMakerJobRunner(
        config=config, storage=storage, client=_StubClient({"TrainingJobStatus": "Failed"})
    )

    assert runner.reconcile(spec.job_id) is None


def test_a_job_the_console_stopped_is_reconciled_as_cancelled(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    spec = spec_for("train")
    update_stage(storage, spec.run_id, StageKey.INGEST, state=RunState.RUNNING)
    stale(storage, spec.run_id, seconds=RECONCILE_GRACE_SECONDS + 60)
    runner = SageMakerJobRunner(
        config=config, storage=storage, client=_StubClient({"TrainingJobStatus": "Stopped"})
    )

    info = runner.reconcile(spec.job_id)

    assert info is not None and info.state is RunState.CANCELLED
    assert storage.read_model(run_key(spec.run_id, STATUS_FILENAME), RunStatus).state is RunState.CANCELLED


def test_a_running_job_is_never_reconciled(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    spec = spec_for("train")
    stale(storage, spec.run_id, seconds=RECONCILE_GRACE_SECONDS + 60)
    runner = SageMakerJobRunner(
        config=config,
        storage=storage,
        client=_StubClient({"TrainingJobStatus": "InProgress", "SecondaryStatus": "Training"}),
    )

    assert runner.reconcile(spec.job_id) is None


def test_a_control_plane_this_process_cannot_reach_changes_nothing(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    spec = spec_for("train")
    stale(storage, spec.run_id, seconds=RECONCILE_GRACE_SECONDS + 60)

    class _Unreachable(_StubClient):
        def describe_training_job(self, **kwargs: Any) -> dict[str, Any]:
            raise OSError("no route to host")

    runner = SageMakerJobRunner(config=config, storage=storage, client=_Unreachable({}))
    assert runner.reconcile(spec.job_id) is None


def test_reconciling_a_job_nobody_has_ever_heard_of_is_none(
    storage: LocalStorage, config: SageMakerJobConfig
) -> None:
    runner = SageMakerJobRunner(config=config, storage=storage, client=_StubClient({}))
    assert runner.reconcile("r_nothing") is None


# ---------------------------------------------------------------------------
# What the manifest learns when a job ends
# ---------------------------------------------------------------------------
def seed_manifest(storage: LocalStorage, run_id: str, *, seconds: float) -> None:
    storage.write_model(
        run_key(run_id, RUN_MANIFEST_FILENAME),
        RunManifest(
            run_id=run_id,
            primary_key="customer_id",
            dataset_fingerprint=DatasetFingerprint(hash="0" * 16, algorithm="sha256", n_rows=1, columns=()),
            seed=1,
            duration_s=seconds,
            cost_estimate=CostEstimate(compute_seconds=seconds, estimated_usd=None, basis="local"),
            created_at=utc_now(),
        ),
    )


def test_a_finished_training_job_writes_where_it_ran_and_what_aws_billed(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    spec = spec_for("train")
    seed_manifest(storage, spec.run_id, seconds=12.5)
    started = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
    client = _StubClient(
        {
            "TrainingJobStatus": "Completed",
            "TrainingJobArn": "arn:aws:sagemaker:ap-south-1:0:training-job/x",
            "TrainingStartTime": started,
            "TrainingEndTime": started + timedelta(seconds=300),
            "BillableTimeInSeconds": 240,
            "ResourceConfig": {"InstanceType": TRAIN_INSTANCE, "InstanceCount": 2},
        }
    )
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)

    runner.status(spec.job_id)

    manifest = storage.read_model(run_key(spec.run_id, RUN_MANIFEST_FILENAME), RunManifest)
    assert manifest.compute is not None
    assert manifest.compute.backend == ComputeBackend.SAGEMAKER
    assert manifest.compute.entrypoint is JobEntrypoint.TRAIN
    assert manifest.compute.billable_seconds == 240
    assert manifest.compute.billable_seconds_source == "DescribeTrainingJob.BillableTimeInSeconds"
    assert manifest.compute.duration_s == 300
    assert manifest.compute.instance_count == 2
    assert manifest.cost_estimate.compute_seconds == 12.5, "what the pipeline measured is kept"
    assert manifest.cost_estimate.estimated_usd is not None


def test_a_finished_processing_job_reports_wall_clock_and_no_billable_time(
    storage: LocalStorage, spec_for: Any
) -> None:
    """A processing job's start and end times are wall clock; SageMaker bills neither (DEC-332)."""
    config = SageMakerJobConfig.from_settings(make_settings(sagemaker_volume_size_gb=30))
    spec = spec_for("score")
    seed_manifest(storage, spec.run_id, seconds=4.0)
    started = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
    client = _StubClient(
        {
            "ProcessingJobStatus": "Completed",
            "ProcessingStartTime": started,
            "ProcessingEndTime": started + timedelta(seconds=60),
            "ProcessingResources": {
                "ClusterConfig": {"InstanceType": PROCESSING_INSTANCE, "InstanceCount": 1}
            },
        }
    )
    runner = SageMakerJobRunner(config=config, storage=storage, client=client)

    runner.status(spec.job_id)

    manifest = storage.read_model(run_key(spec.run_id, RUN_MANIFEST_FILENAME), RunManifest)
    assert manifest.compute is not None
    assert manifest.compute.backend == ComputeBackend.SAGEMAKER
    assert manifest.compute.entrypoint is JobEntrypoint.SCORE
    assert manifest.compute.billable_seconds is None
    assert manifest.compute.billable_seconds_source is None
    assert manifest.compute.duration_s == 60
    assert manifest.cost_estimate.estimated_usd is None
    assert "billable" in manifest.cost_estimate.basis.lower()


def test_a_job_that_never_reached_the_container_gets_no_invented_manifest(
    storage: LocalStorage, config: SageMakerJobConfig, spec_for: Any
) -> None:
    """Writing one would mean inventing a fingerprint and a seed for a run that read no data."""
    spec = spec_for("train")
    runner = SageMakerJobRunner(
        config=config, storage=storage, client=_StubClient({"TrainingJobStatus": "Failed"})
    )

    runner.status(spec.job_id)

    assert not storage.exists(run_key(spec.run_id, RUN_MANIFEST_FILENAME))
