"""`engine.aws.sagemaker_registry`: off by default, one way, and never fatal.

These are the three promises the module makes, and each gets its own test. The AWS calls run
against `moto`, which is where the honesty limit of this module sits: moto accepts a versioned
model package carrying only metadata, and this repository has no AWS account to confirm that real
SageMaker does too. That is exactly why a failure here is caught and logged rather than raised -
if the real service refuses the call, a deployment loses a console page and not a model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from engine.aws.s3_registry import ModelMirror
from engine.aws.sagemaker_registry import (
    APPROVAL_STATUS,
    SageMakerModelRegistryMirror,
    SageMakerRegistryConfig,
)
from engine.config import Metric
from engine.contracts import ModelStatus, ModelVersion
from engine.settings import Deployment, Settings

REGION: str = "eu-west-1"
USE_CASE: str = "a-use-case"


def make_version(
    model_id: str = "m_1", *, status: ModelStatus = ModelStatus.CANDIDATE, use_case_id: str = USE_CASE
) -> ModelVersion:
    return ModelVersion(
        model_id=model_id,
        use_case_id=use_case_id,
        version=1,
        run_id="r_1",
        created_at=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
        status=status,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=0.8123,
        model_display_name="WeightedEnsemble_L2",
        schema_key="models/a-use-case/1/schema.json",
        run_config_key="models/a-use-case/1/run_config.json",
        predictor_key="models/a-use-case/1/model",
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )


class CountingClient:
    """A client that records calls and, optionally, fails them. No AWS, no moto, no guesswork."""

    def __init__(self, *, group_error: Exception | None = None, package_error: Exception | None = None):
        self.groups: list[dict[str, Any]] = []
        self.packages: list[dict[str, Any]] = []
        self.group_error = group_error
        self.package_error = package_error

    def create_model_package_group(self, **kwargs: Any) -> dict[str, Any]:
        self.groups.append(kwargs)
        if self.group_error is not None:
            raise self.group_error
        return {"ModelPackageGroupArn": "arn:group"}

    def create_model_package(self, **kwargs: Any) -> dict[str, Any]:
        self.packages.append(kwargs)
        if self.package_error is not None:
            raise self.package_error
        return {"ModelPackageArn": "arn:package/1"}


def client_error(code: str) -> ClientError:
    """A constructed botocore error, which is the only honest way to make one of a given code."""
    return ClientError({"Error": {"Code": code, "Message": "..."}}, "CreateModelPackageGroup")


# ---------------------------------------------------------------------------
# Off by default (DEC-348)
# ---------------------------------------------------------------------------
def test_the_mirror_is_off_unless_somebody_turns_it_on() -> None:
    settings = Settings.build(region=REGION, client_id="telco", env=Deployment.DEV)
    assert SageMakerRegistryConfig.from_settings(settings).enabled is False
    assert SageMakerRegistryConfig().enabled is False


def test_a_disabled_mirror_makes_no_call_at_all() -> None:
    """Not "makes a call and ignores it": a product must not write into an account unasked."""
    client = CountingClient()
    mirror = SageMakerModelRegistryMirror(SageMakerRegistryConfig(enabled=False), client=client)
    assert mirror.mirror(make_version()) is None
    assert mirror.enabled is False
    assert (client.groups, client.packages) == ([], [])


def test_from_settings_carries_the_prefix_the_region_and_the_tags() -> None:
    settings = Settings.build(region=REGION, client_id="telco", env=Deployment.DEV)
    config = SageMakerRegistryConfig.from_settings(settings, enabled=True)
    assert config.enabled is True
    assert config.region == REGION
    assert config.group_prefix == "marketing-ai"
    assert config.group_name(USE_CASE) == "marketing-ai-a-use-case"
    assert config.tags == {"product": "marketing-ai", "env": "dev", "client": "telco"}


def test_it_satisfies_the_mirror_protocol() -> None:
    mirror = SageMakerModelRegistryMirror(SageMakerRegistryConfig())
    assert isinstance(mirror, ModelMirror)


# ---------------------------------------------------------------------------
# What it writes
# ---------------------------------------------------------------------------
def test_an_enabled_mirror_creates_the_group_and_the_package() -> None:
    client = CountingClient()
    mirror = SageMakerModelRegistryMirror(SageMakerRegistryConfig(enabled=True), client=client)
    assert mirror.mirror(make_version()) == "arn:package/1"
    assert client.groups[0]["ModelPackageGroupName"] == "marketing-ai-a-use-case"
    request = client.packages[0]
    assert request["ModelPackageGroupName"] == "marketing-ai-a-use-case"
    assert request["ModelApprovalStatus"] == "PendingManualApproval"
    assert request["CustomerMetadataProperties"]["model_id"] == "m_1"
    assert request["CustomerMetadataProperties"]["predictor_key"] == "models/a-use-case/1/model"


def test_no_inference_specification_is_invented() -> None:
    """An AutoGluon predictor is a directory; `ModelDataUrl` wants a tarball (plan section 13.3)."""
    client = CountingClient()
    mirror = SageMakerModelRegistryMirror(SageMakerRegistryConfig(enabled=True), client=client)
    mirror.mirror(make_version())
    assert "InferenceSpecification" not in client.packages[0]


def test_an_inference_specification_is_sent_when_a_deployment_supplies_one() -> None:
    client = CountingClient()
    config = SageMakerRegistryConfig(
        enabled=True,
        image_uri="123.dkr.ecr.eu-west-1.amazonaws.com/marketing-ai:1",
        model_data_url="s3://bucket/models/a-use-case/1/model.tar.gz",
    )
    SageMakerModelRegistryMirror(config, client=client).mirror(make_version())
    containers = client.packages[0]["InferenceSpecification"]["Containers"]
    assert containers[0]["ModelDataUrl"] == "s3://bucket/models/a-use-case/1/model.tar.gz"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ModelStatus.CANDIDATE, "PendingManualApproval"),
        (ModelStatus.PENDING_APPROVAL, "PendingManualApproval"),
        (ModelStatus.CHAMPION, "Approved"),
        (ModelStatus.ARCHIVED, "Rejected"),
    ],
)
def test_every_status_has_a_word_in_sagemakers_vocabulary(status: ModelStatus, expected: str) -> None:
    """Four states into three words; the map is lossy in one place and complete in every other."""
    client = CountingClient()
    mirror = SageMakerModelRegistryMirror(SageMakerRegistryConfig(enabled=True), client=client)
    mirror.mirror(make_version(status=status))
    assert client.packages[0]["ModelApprovalStatus"] == expected
    assert set(APPROVAL_STATUS) == set(ModelStatus)


def test_the_group_is_created_once_per_use_case() -> None:
    client = CountingClient()
    mirror = SageMakerModelRegistryMirror(SageMakerRegistryConfig(enabled=True), client=client)
    mirror.mirror(make_version("m_1"))
    mirror.mirror(make_version("m_2"))
    mirror.mirror(make_version("m_3", use_case_id="another"))
    assert [call["ModelPackageGroupName"] for call in client.groups] == [
        "marketing-ai-a-use-case",
        "marketing-ai-another",
    ]


def test_a_group_that_already_exists_is_the_state_this_wanted() -> None:
    """Every version after the first meets this, so it is not a failure and must not be logged as one."""
    client = CountingClient(group_error=client_error("ValidationException"))
    mirror = SageMakerModelRegistryMirror(SageMakerRegistryConfig(enabled=True), client=client)
    assert mirror.mirror(make_version()) == "arn:package/1"
    assert len(client.packages) == 1


# ---------------------------------------------------------------------------
# Never fatal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", ["AccessDeniedException", "ThrottlingException", "InternalFailure"])
def test_any_aws_failure_is_swallowed_and_reported_as_nothing_written(code: str) -> None:
    client = CountingClient(package_error=client_error(code))
    mirror = SageMakerModelRegistryMirror(SageMakerRegistryConfig(enabled=True), client=client)
    assert mirror.mirror(make_version()) is None


def test_a_group_failure_that_is_not_already_exists_is_also_swallowed() -> None:
    client = CountingClient(group_error=client_error("AccessDeniedException"))
    mirror = SageMakerModelRegistryMirror(SageMakerRegistryConfig(enabled=True), client=client)
    assert mirror.mirror(make_version()) is None
    assert client.packages == []


def test_a_response_without_an_arn_is_none_rather_than_a_key_error() -> None:
    class Silent(CountingClient):
        def create_model_package(self, **kwargs: Any) -> dict[str, Any]:
            super().create_model_package(**kwargs)
            return {}

    mirror = SageMakerModelRegistryMirror(SageMakerRegistryConfig(enabled=True), client=Silent())
    assert mirror.mirror(make_version()) is None


def test_the_failure_log_line_carries_no_exception_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plan section 13.7: the class name, never the message - an AWS message quotes the request."""
    client = CountingClient(package_error=RuntimeError("bucket telco-prod-2026 is not yours"))
    mirror = SageMakerModelRegistryMirror(SageMakerRegistryConfig(enabled=True), client=client)
    with caplog.at_level("WARNING"):
        assert mirror.mirror(make_version()) is None
    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in rendered
    assert "telco-prod-2026" not in rendered


# ---------------------------------------------------------------------------
# Against moto
# ---------------------------------------------------------------------------
def test_the_two_calls_are_accepted_by_a_sagemaker_implementation() -> None:
    """What this proves and what it does not.

    It proves the request this module builds is well-formed enough for a SageMaker implementation
    to accept and to read back: the group exists, the package is in it, and the approval status
    arrived. It does **not** prove real SageMaker accepts a versioned model package with no
    `InferenceSpecification` - there is no AWS account here to ask, which is why this call is
    guarded rather than trusted.
    """
    with mock_aws():
        client = boto3.client("sagemaker", region_name=REGION)
        config = SageMakerRegistryConfig(enabled=True, region=REGION, tags={"product": "marketing-ai"})
        mirror = SageMakerModelRegistryMirror(config, client=client)
        arn = mirror.mirror(make_version(status=ModelStatus.CHAMPION))
        assert arn is not None and arn.startswith("arn:aws:sagemaker:")
        summaries = client.list_model_packages(ModelPackageGroupName="marketing-ai-a-use-case")[
            "ModelPackageSummaryList"
        ]
        assert [summary["ModelApprovalStatus"] for summary in summaries] == ["Approved"]
        assert "m_1" in summaries[0]["ModelPackageDescription"]
