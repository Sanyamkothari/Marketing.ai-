"""A real train and a real score, run as SageMaker jobs, through the product's own API.

Everything here is the shipping code except the two things there is no way to have locally: the
SageMaker control plane is `tests.fakes.fake_sagemaker.FakeSageMakerClient`, and S3 is `moto`. The
API, `SageMakerJobRunner`, `job_spec.json`, `scripts/run_job_entrypoint.py`, `S3Storage`, the
pipeline and AutoGluon are all the real ones, and the container's process is started with the
`Environment` mapping out of the request the runner actually sent - nothing else.

That is the point of the module. DEC-324 says a local run and a remote run are two renderings of
one description; the only way to find out whether that is true is to run the second rendering. Three
specific claims are settled here and nowhere else:

* the environment `SageMakerJobRunner` ships is **sufficient** - a container with only those
  variables can find the artefacts, the configuration and the registry (DEC-337);
* a training run started from `POST /runs` reaches `done`. Since M2 it could not: the route
  submitted the stub that stops at `prepare`, and `Pipeline.run_train` was reachable only from a
  test (DEC-326);
* a scoring run is a **processing** job, a training run is a **training** job, and the manifest
  ends up saying which one it was and what it cost at a published list price (DEC-330, DEC-332).

It stays in the fast suite deliberately. The training budget is one minute and the search is
narrowed to a single model family, which is not what a user's run looks like - but this module is
not about the model, and a proof of the remote path that nobody runs is not a proof.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.aws.prices import NOT_BILLED
from engine.aws.sagemaker_jobs import (
    JOB_SPEC_KEY_ENV_VAR,
    TRAINING_BACKEND,
    SageMakerJobConfig,
    SageMakerJobRunner,
)
from engine.contracts import (
    SCORE_ARTEFACTS,
    TRAIN_ARTEFACTS,
    ComputeBackend,
    JobEntrypoint,
    JobSpec,
    ModelVersion,
    RunManifest,
    RunRecord,
    RunState,
    RunStatus,
    ScoringSummary,
)
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.runs import job_spec_key
from engine.settings import Settings, build_storage
from engine.storage import Storage, run_key
from tests.fakes.fake_sagemaker import (
    CREATE_PROCESSING_JOB,
    CREATE_TRAINING_JOB,
    FakeSageMakerClient,
)
from tests.fixtures.make_data import GenerationSpec, generate

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"
PRIMARY_KEY = "customer_id"
TARGET = "converted_30d"
REGION = "ap-south-1"
"""The region the installed rate card prices, so the cost estimate here is a real lookup."""

BUCKET = "marketing-ai-jobs-test"
TRAIN_ROWS = 2_500
"""Enough rows to pass validation's `min_rows` and `min_positive` on this use case, and no more."""

SCORE_ROWS = 300
TRAIN_SEED = 20260922
SCORE_SEED = 20260923

TRAIN_INSTANCE = "ml.m5.xlarge"
PROCESSING_INSTANCE = "ml.m5.large"

OVERRIDES: dict[str, Any] = {
    "model_search.time_limit_minutes": 1,
    "model_search.strategy": "fast",
    "model_search.candidates": ["LogisticRegression"],
    "model_search.ensemble": False,
}
"""The smallest real search there is. This module tests the path, not the model."""


# ---------------------------------------------------------------------------
# The deployment
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def restore_root_logging() -> Iterator[None]:
    """Put this process's logging back exactly as it was found, once this module is done.

    Module-scoped and autouse so that the snapshot is taken *before* `deployed` builds the app: an
    autouse fixture is set up first within its scope, and a function-scoped one would run after the
    module-scoped fixtures it is meant to undo.

    `configure_logging` is called for real here - by `create_app` when it is given settings, and by
    `main()` inside the container - because configuring the process is part of what is under test.
    A test is not a process, though: the handler, its formatter and its `ContextFilter` would
    otherwise outlive this module and change how every later test in the session logs, which
    `tests/unit/test_logging_audit.py` makes assertions about (DEC-381, DEC-383).
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    state = [(handler, list(handler.filters), handler.formatter) for handler in handlers]
    level = root.level
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(level)
        for handler, filters, formatter in state:
            handler.filters = filters
            handler.setFormatter(formatter)


@pytest.fixture(scope="module")
def aws_credentials() -> Any:
    """Credentials moto accepts and a real account would refuse.

    Module-scoped, with its own `MonkeyPatch`, because everything below is module-scoped: one real
    training run and one real scoring run are shared by every test here, since running AutoGluon
    once per assertion would put this module out of the fast suite for no extra coverage.
    """
    with pytest.MonkeyPatch.context() as patch:
        for name, value in (
            ("AWS_ACCESS_KEY_ID", "testing"),
            ("AWS_SECRET_ACCESS_KEY", "testing"),
            ("AWS_SECURITY_TOKEN", "testing"),
            ("AWS_SESSION_TOKEN", "testing"),
            ("AWS_DEFAULT_REGION", REGION),
        ):
            patch.setenv(name, value)
        yield None


@pytest.fixture(scope="module")
def bucket(aws_credentials: None) -> Any:
    del aws_credentials
    moto = pytest.importorskip("moto", reason="the remote path is exercised against moto's S3")
    import boto3

    with moto.mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        yield client


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """A deployment that keeps artefacts in a bucket and runs its jobs on SageMaker.

    `data_dir` still means something here: the metadata backend is SQLite, so it is where
    `registry.db` lives - and it is shipped to the container, which is how the job registers the
    model version this process then reads back.
    """
    root = tmp_path_factory.mktemp("jobs-as-sagemaker")
    return Settings(
        env="dev",
        aws_region=REGION,
        data_dir=root / "meta",
        storage_backend="s3",
        s3_bucket=BUCKET,
        s3_prefix="artefacts",
        local_cache_dir=root / "mirror",
        job_backend="sagemaker",
        sagemaker_role_arn="arn:aws:iam::000000000000:role/marketing-ai-job",
        sagemaker_image_uri="000000000000.dkr.ecr.ap-south-1.amazonaws.com/marketing-ai:1",
        sagemaker_instance_type=TRAIN_INSTANCE,
        sagemaker_processing_instance_type=PROCESSING_INSTANCE,
        sagemaker_volume_size_gb=30,
        client_id="acme",
        log_level="WARNING",
    )


@dataclass
class Deployed:
    """The app, the fake control plane and the two services the test reads artefacts through."""

    client: TestClient
    sagemaker: FakeSageMakerClient
    storage: Storage
    registry: LocalModelRegistry
    settings: Settings

    def artefact(self, run_id: str, name: str, model: Any) -> Any:
        return self.storage.read_model(run_key(run_id, name), model)


@pytest.fixture(scope="module")
def deployed(bucket: Any, settings: Settings, config_root: Path) -> Any:
    """The API, wired to the bucket and to a SageMaker that runs the container in this process."""
    del bucket
    storage = build_storage(settings)
    sagemaker = FakeSageMakerClient(region=REGION)
    runner = SageMakerJobRunner(
        config=SageMakerJobConfig.from_settings(settings),
        storage=storage,
        client=sagemaker,
    )
    app = create_app(config_root=config_root, settings=settings)
    app.state.storage = storage
    app.state.jobs = runner
    with TestClient(app) as client:
        yield Deployed(
            client=client,
            sagemaker=sagemaker,
            storage=storage,
            registry=LocalModelRegistry(settings.data_dir / REGISTRY_FILENAME),
            settings=settings,
        )


# ---------------------------------------------------------------------------
# Driving the API
# ---------------------------------------------------------------------------
def upload(deployed: Deployed, *, variant: str, rows: int, seed: int, mode: str) -> str:
    frame = generate(GenerationSpec(USE_CASE, rows=rows, variant=variant, seed=seed))
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    response = deployed.client.post(
        "/uploads",
        files={"file": (f"{mode}.csv", payload, "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def start(deployed: Deployed, body: dict[str, Any]) -> str:
    response = deployed.client.post("/runs", json=body)
    assert response.status_code == 202, response.text
    return str(response.json()["run_id"])


def finish(deployed: Deployed, run_id: str) -> RunRecord:
    """Wait for the container to exit, then read the run back through the API."""
    deployed.sagemaker.wait_all(timeout=300)
    response = deployed.client.get(f"/runs/{run_id}")
    assert response.status_code == 200, response.text
    return RunRecord.model_validate(response.json()["run"])


@pytest.fixture(scope="module")
def trained(deployed: Deployed) -> Any:
    """One real training run, as a SageMaker training job, through `POST /runs`."""
    upload_id = upload(deployed, variant="clean", rows=TRAIN_ROWS, seed=TRAIN_SEED, mode="train")
    run_id = start(
        deployed,
        {
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": upload_id,
            "primary_key": PRIMARY_KEY,
            "target": TARGET,
            "overrides": OVERRIDES,
        },
    )
    return run_id, finish(deployed, run_id)


@pytest.fixture(scope="module")
def scored(deployed: Deployed, trained: Any) -> Any:
    """That model's own scoring run, as a SageMaker processing job, through `POST /runs`."""
    _, record = trained
    assert record.model_version_id is not None
    upload_id = upload(deployed, variant="scoring", rows=SCORE_ROWS, seed=SCORE_SEED, mode="score")
    run_id = start(
        deployed,
        {
            "use_case": USE_CASE,
            "mode": "score",
            "upload_id": upload_id,
            "primary_key": PRIMARY_KEY,
            "model_version_id": record.model_version_id,
            "overrides": OVERRIDES,
        },
    )
    return run_id, finish(deployed, run_id)


# ---------------------------------------------------------------------------
# A training run reaches `done` through the API (DEC-326)
# ---------------------------------------------------------------------------
def test_a_training_run_started_from_the_api_finishes(deployed: Deployed, trained: Any) -> None:
    """Since M2 this could not happen: the route submitted the stub that stops at `prepare`."""
    run_id, record = trained

    assert record.state is RunState.DONE, record.error
    assert record.model_version_id is not None
    assert record.headline_score is not None
    status = deployed.artefact(run_id, "status.json", RunStatus)
    assert status.state is RunState.DONE
    assert status.progress_pct == 100
    assert {row.state for row in status.stages} == {RunState.DONE}


def test_the_training_run_wrote_every_artefact_into_the_bucket(deployed: Deployed, trained: Any) -> None:
    run_id, _ = trained
    keys = set(deployed.storage.list_keys(f"runs/{run_id}/"))

    for name in TRAIN_ARTEFACTS - {"model/"}:
        assert run_key(run_id, name) in keys, name
    assert any(key.startswith(run_key(run_id, "model/")) for key in keys), "the predictor directory"


def test_the_model_the_job_registered_is_readable_from_this_process(deployed: Deployed, trained: Any) -> None:
    """The container and the API share one registry because they share one description (DEC-308)."""
    _, record = trained
    version = deployed.registry.get(record.model_version_id or "")

    assert isinstance(version, ModelVersion)
    assert version.use_case_id == USE_CASE
    assert deployed.storage.exists(version.schema_key)


# ---------------------------------------------------------------------------
# A scoring run is a processing job (DEC-332)
# ---------------------------------------------------------------------------
def test_a_scoring_run_started_from_the_api_finishes(deployed: Deployed, scored: Any) -> None:
    run_id, record = scored

    assert record.state is RunState.DONE, record.error
    summary = deployed.artefact(run_id, "scoring_summary.json", ScoringSummary)
    assert summary.rows_scored == SCORE_ROWS
    for name in SCORE_ARTEFACTS - {"drift.json"}:
        assert deployed.storage.exists(run_key(run_id, name)), name


def test_training_created_a_training_job_and_scoring_a_processing_one(
    deployed: Deployed, scored: Any
) -> None:
    del scored
    assert len(deployed.sagemaker.requests(CREATE_TRAINING_JOB)) == 1
    assert len(deployed.sagemaker.requests(CREATE_PROCESSING_JOB)) == 1


def test_nothing_ever_asked_the_account_to_list_its_jobs(deployed: Deployed, scored: Any) -> None:
    """The fake raises on any other call, so reaching this line is the assertion (DEC-334)."""
    del scored
    assert {call.api for call in deployed.sagemaker.calls} <= {
        CREATE_TRAINING_JOB,
        CREATE_PROCESSING_JOB,
        "DescribeTrainingJob",
        "DescribeProcessingJob",
        "StopTrainingJob",
        "StopProcessingJob",
    }


# ---------------------------------------------------------------------------
# What crossed the process boundary
# ---------------------------------------------------------------------------
def test_the_container_was_handed_the_spec_key_and_nothing_else_about_the_job(
    deployed: Deployed, trained: Any
) -> None:
    run_id, _ = trained
    request = deployed.sagemaker.last(CREATE_TRAINING_JOB)

    assert request.environment[JOB_SPEC_KEY_ENV_VAR] == job_spec_key(run_id)
    assert not [name for name in request.environment if "SAGEMAKER" in name]
    assert not [name for name in request.environment if "DATABASE" in name]


def test_the_spec_the_container_read_is_the_spec_the_request_pointed_at(
    deployed: Deployed, trained: Any
) -> None:
    run_id, record = trained
    spec = deployed.storage.read_model(job_spec_key(run_id), JobSpec)

    assert spec.run_id == run_id
    assert spec.entrypoint is JobEntrypoint.TRAIN
    assert spec.backend == TRAINING_BACKEND
    assert spec.use_case_id == record.use_case_id
    assert spec.primary_key == PRIMARY_KEY
    assert spec.target == TARGET


def test_every_job_carried_the_cost_allocation_tags(deployed: Deployed, scored: Any) -> None:
    del scored
    for api in (CREATE_TRAINING_JOB, CREATE_PROCESSING_JOB):
        call = deployed.sagemaker.last(api)
        assert call.tag("product") == "marketing-ai"
        assert call.tag("client") == "acme"
        assert call.tag("use_case") == USE_CASE
        assert call.tag("run_id")


def test_the_job_spec_is_not_counted_as_something_the_run_produced(deployed: Deployed, trained: Any) -> None:
    run_id, record = trained
    assert deployed.storage.exists(job_spec_key(run_id))
    assert "job_spec.json" not in record.artefacts
    assert "job_spec.json" not in TRAIN_ARTEFACTS


# ---------------------------------------------------------------------------
# What the manifest learned (DEC-329, DEC-330)
# ---------------------------------------------------------------------------
def test_the_training_manifest_says_where_it_ran_and_what_aws_billed(
    deployed: Deployed, trained: Any
) -> None:
    run_id, _ = trained
    manifest = deployed.artefact(run_id, "run_manifest.json", RunManifest)

    assert manifest.compute is not None
    assert manifest.compute.backend == ComputeBackend.SAGEMAKER
    assert manifest.compute.entrypoint is JobEntrypoint.TRAIN
    assert manifest.compute.instance_type == TRAIN_INSTANCE
    assert manifest.compute.region == REGION
    assert manifest.compute.job_name is not None
    assert manifest.compute.billable_seconds is not None
    assert manifest.compute.billable_seconds_source == "DescribeTrainingJob.BillableTimeInSeconds"


def test_the_training_cost_is_a_published_list_price_and_says_so(deployed: Deployed, trained: Any) -> None:
    run_id, _ = trained
    estimate = deployed.artefact(run_id, "run_manifest.json", RunManifest).cost_estimate

    assert estimate.estimated_usd is not None
    assert estimate.estimated_usd > 0
    assert "list price" in estimate.basis
    assert TRAIN_INSTANCE in estimate.basis
    assert REGION in estimate.basis
    assert "is not a bill" in estimate.basis


def test_the_scoring_manifest_reports_wall_clock_and_no_price(deployed: Deployed, scored: Any) -> None:
    """SageMaker reports no billable time for a processing job, so there is nothing to price."""
    run_id, _ = scored
    manifest = deployed.artefact(run_id, "run_manifest.json", RunManifest)

    assert manifest.compute is not None
    assert manifest.compute.backend == ComputeBackend.SAGEMAKER
    assert manifest.compute.entrypoint is JobEntrypoint.SCORE
    assert manifest.compute.duration_s is not None
    assert manifest.compute.billable_seconds is None
    assert manifest.cost_estimate.estimated_usd is None
    assert manifest.cost_estimate.basis != NOT_BILLED, "it *was* carried by a service that bills"
