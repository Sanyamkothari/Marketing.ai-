"""The Phase 4b settings reach the engine through Parameter Store, and they make a `Settings`.

Everything the operations stack knows goes into `/marketing-ai/<env>/<field>`, flat, exactly as
`infra/compute.py` does for Phase 4a - never onto a task definition, where a variable silently
overrides the parameter of the same name (DEC-377). The last test here is the one that matters:
it builds the object the container builds at start, from both stacks' parameters together, so a
combination `Settings` refuses (an `eventbridge` scheduler with no role, an `sns` alert backend with
no topic) fails here and not in a deployed task's first log line.
"""

from __future__ import annotations

from typing import Any

import pytest
from aws_cdk.assertions import Template
from infra.app import build_app
from infra.context import AppContext
from infra.naming import AUDIT_EXPORT_PREFIX, SETTINGS_FIELDS

from tests.infra.conftest import ACCOUNT, PROD_CONTEXT_KEYS, REGION


def _parameters(template: Template) -> dict[str, Any]:
    """`{parameter name: value}` for every SSM parameter in the stack."""
    return {
        resource["Properties"]["Name"]: resource["Properties"]["Value"]
        for resource in template.find_resources("AWS::SSM::Parameter").values()
    }


def test_every_operations_parameter_is_a_flat_settings_field(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    for env_name, templates in (("dev", dev_templates), ("prod", prod_templates)):
        names = _parameters(templates["operations"])
        assert names
        for name in names:
            assert name.startswith(f"/marketing-ai/{env_name}/"), name
            assert name.count("/") == 3, f"{name} is nested; the application reads one level only"
            assert name.rsplit("/", 1)[-1] in SETTINGS_FIELDS, name


def test_no_parameter_is_written_by_two_stacks(dev_templates: dict[str, Template]) -> None:
    """The same name from two stacks is a deployment that fails with `AlreadyExists`."""
    compute = set(_parameters(dev_templates["compute"]))
    operations = set(_parameters(dev_templates["operations"]))
    assert compute and operations
    assert not compute & operations


def test_the_operations_parameters_are_the_phase_4b_switches(dev_templates: dict[str, Template]) -> None:
    leaves = {name.rsplit("/", 1)[-1] for name in _parameters(dev_templates["operations"])}
    assert leaves == {
        "auth_mode",
        "audit_export_bucket",
        "audit_export_prefix",
        "audit_retention_days",
        "scheduler_backend",
        "scheduler_group_name",
        "scheduler_target_arn",
        "scheduler_role_arn",
        "alert_backend",
        "alert_sns_topic_arn",
    }


@pytest.mark.parametrize(
    ("field_name", "dev_value", "prod_value"),
    [
        ("auth_mode", "local", "local"),
        ("scheduler_backend", "eventbridge", "eventbridge"),
        ("alert_backend", "sns", "sns"),
        ("audit_export_prefix", AUDIT_EXPORT_PREFIX, AUDIT_EXPORT_PREFIX),
        ("audit_retention_days", "1", "2555"),
        ("scheduler_group_name", "marketing-ai-dev", "marketing-ai-prod"),
    ],
)
def test_the_literal_values(
    dev_templates: dict[str, Template],
    prod_templates: dict[str, Template],
    field_name: str,
    dev_value: str,
    prod_value: str,
) -> None:
    for env_name, templates, expected in (
        ("dev", dev_templates, dev_value),
        ("prod", prod_templates, prod_value),
    ):
        assert _parameters(templates["operations"])[f"/marketing-ai/{env_name}/{field_name}"] == expected


def test_the_scheduler_target_is_the_cluster(dev_templates: dict[str, Template]) -> None:
    """EventBridge Scheduler's ECS target is addressed by the cluster ARN; the rest is EcsParameters."""
    value = _parameters(dev_templates["operations"])["/marketing-ai/dev/scheduler_target_arn"]
    assert "Cluster" in str(value)


def test_the_referenced_values_name_the_resources_this_stack_made(dev_templates: dict[str, Template]) -> None:
    parameters = _parameters(dev_templates["operations"])
    assert "AuditExports" in str(parameters["/marketing-ai/dev/audit_export_bucket"])
    assert "SchedulerRole" in str(parameters["/marketing-ai/dev/scheduler_role_arn"])
    assert "Alerts" in str(parameters["/marketing-ai/dev/alert_sns_topic_arn"])


def test_a_dev_deployment_that_asked_for_sign_in_off_writes_off() -> None:
    deployment = build_app(AppContext.from_mapping({"env_name": "dev", "auth_mode": "off"}), account=ACCOUNT)
    assert _parameters(Template.from_stack(deployment.operations))["/marketing-ai/dev/auth_mode"] == "off"


def test_the_audit_prefix_is_the_setting_s_default() -> None:
    """It is also the IAM boundary of the write grant, so the two must be the same string."""
    settings_module = pytest.importorskip(
        "engine.settings", reason="the engine is not installed in this venv; `make infra-setup` installs it"
    )
    assert settings_module.Settings().audit_export_prefix == AUDIT_EXPORT_PREFIX


def test_both_stacks_parameters_together_make_a_settings_the_engine_accepts() -> None:
    """What the container does at start, with the ARNs CloudFormation would fill in."""
    settings_module = pytest.importorskip(
        "engine.settings", reason="the engine is not installed in this venv; `make infra-setup` installs it"
    )
    deployment = build_app(AppContext.from_mapping(dict(PROD_CONTEXT_KEYS)), account=ACCOUNT)
    parameters = {
        **deployment.compute.deployment_parameters(
            s3_bucket="marketing-ai-prod-123456789012",
            s3_kms_key_id=f"arn:aws:kms:{REGION}:{ACCOUNT}:key/abc",
            sagemaker_role_arn=f"arn:aws:iam::{ACCOUNT}:role/marketing-ai-prod-sagemaker-execution",
            sagemaker_image_uri=f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/marketing-ai@sha256:" + "0" * 64,
            sagemaker_subnet_ids="subnet-aaaa,subnet-bbbb",
            sagemaker_security_group_ids="sg-aaaa",
        ),
        **deployment.operations.operations_parameters(
            audit_export_bucket="marketing-ai-prod-123456789012-audit",
            scheduler_target_arn=f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/marketing-ai-prod",
            scheduler_role_arn=f"arn:aws:iam::{ACCOUNT}:role/marketing-ai-prod-scheduler",
            alert_sns_topic_arn=f"arn:aws:sns:{REGION}:{ACCOUNT}:marketing-ai-prod-alerts",
        ),
    }
    settings = settings_module.settings_from_aws(
        source=_StaticSource(
            parameters, secret={"postgres_dsn": "postgresql+psycopg://u:p@host:5432/marketing_ai"}
        ),
        env="prod",
        environ={"MARKETING_AI_AWS_REGION": REGION},
    )
    assert settings.auth_mode == "local"
    assert settings.scheduler_backend == "eventbridge"
    assert settings.scheduler_group_name == "marketing-ai-prod"
    assert settings.alert_backend == "sns"
    assert settings.audit_export_bucket == "marketing-ai-prod-123456789012-audit"
    assert settings.audit_retention_days == 2555


class _StaticSource:
    """A `ParameterSource` backed by two dictionaries; no AWS, no boto3."""

    def __init__(self, parameters: dict[str, str], secret: dict[str, str] | None = None) -> None:
        self._parameters = parameters
        self._secret = dict(secret or {})

    def parameters(self, _prefix: str) -> dict[str, str]:
        return dict(self._parameters)

    def secret(self, _name: str) -> dict[str, str]:
        return dict(self._secret)
