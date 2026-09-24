"""The service: the health check, the start period, the autoscaling bounds and the database."""

from __future__ import annotations

import pytest
from aws_cdk.assertions import Template
from infra.app import build_app
from infra.compute import HEALTH_CHECK_PATH, START_PERIOD_SECONDS, TARGET_CPU_PERCENT
from infra.context import AppContext
from infra.database import DATABASE_NAME, ROTATION_DAYS

from tests.infra.conftest import ACCOUNT, rendered, resources, sole, templates_of


def test_the_load_balancer_probes_healthz(dev_templates: dict[str, Template]) -> None:
    target_group = sole(dev_templates["compute"], "AWS::ElasticLoadBalancingV2::TargetGroup")
    assert target_group["HealthCheckPath"] == HEALTH_CHECK_PATH
    assert target_group["Matcher"] == {"HttpCode": "200"}
    assert target_group["TargetType"] == "ip"


def test_the_start_period_is_the_one_the_dockerfile_uses(dev_templates: dict[str, Template]) -> None:
    """120 seconds, chosen because AutoGluon's import is heavy - not measured on Fargate."""
    assert START_PERIOD_SECONDS == 120
    definition = sole(dev_templates["compute"], "AWS::ECS::TaskDefinition")
    health_check = definition["ContainerDefinitions"][0]["HealthCheck"]
    assert health_check["StartPeriod"] == START_PERIOD_SECONDS
    service = sole(dev_templates["compute"], "AWS::ECS::Service")
    assert service["HealthCheckGracePeriodSeconds"] == START_PERIOD_SECONDS


def test_the_container_health_check_calls_the_same_endpoint(
    dev_templates: dict[str, Template],
) -> None:
    definition = sole(dev_templates["compute"], "AWS::ECS::TaskDefinition")
    command = definition["ContainerDefinitions"][0]["HealthCheck"]["Command"]
    assert HEALTH_CHECK_PATH in " ".join(command)


def test_the_task_runs_in_private_subnets_with_no_public_address(
    dev_templates: dict[str, Template],
) -> None:
    service = sole(dev_templates["compute"], "AWS::ECS::Service")
    configuration = service["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert configuration["AssignPublicIp"] == "DISABLED"


def test_autoscaling_is_bounded_by_the_context(dev_templates: dict[str, Template]) -> None:
    target = sole(dev_templates["compute"], "AWS::ApplicationAutoScaling::ScalableTarget")
    assert (target["MinCapacity"], target["MaxCapacity"]) == (1, 3)
    policy = sole(dev_templates["compute"], "AWS::ApplicationAutoScaling::ScalingPolicy")
    tracking = policy["TargetTrackingScalingPolicyConfiguration"]
    assert tracking["TargetValue"] == TARGET_CPU_PERCENT
    assert (
        tracking["PredefinedMetricSpecification"]["PredefinedMetricType"] == "ECSServiceAverageCPUUtilization"
    )


def test_a_failed_deployment_rolls_itself_back(dev_templates: dict[str, Template]) -> None:
    service = sole(dev_templates["compute"], "AWS::ECS::Service")
    assert service["DeploymentConfiguration"]["DeploymentCircuitBreaker"] == {
        "Enable": True,
        "Rollback": True,
    }


def test_the_load_balancer_writes_access_logs(dev_templates: dict[str, Template]) -> None:
    balancer = sole(dev_templates["compute"], "AWS::ElasticLoadBalancingV2::LoadBalancer")
    attributes = {entry["Key"]: entry["Value"] for entry in balancer["LoadBalancerAttributes"]}
    assert attributes["access_logs.s3.enabled"] == "true"
    assert attributes["routing.http.drop_invalid_header_fields.enabled"] == "true"


def test_zero_healthy_targets_raises_an_alarm(dev_templates: dict[str, Template]) -> None:
    """Not a tuning threshold: no healthy target is the service being down."""
    alarm = sole(dev_templates["compute"], "AWS::CloudWatch::Alarm")
    assert alarm["MetricName"] == "HealthyHostCount"
    assert alarm["ComparisonOperator"] == "LessThanThreshold"
    assert alarm["Threshold"] == 1
    assert alarm["AlarmActions"]


def test_the_database_requires_tls(dev_templates: dict[str, Template]) -> None:
    group = sole(dev_templates["database"], "AWS::RDS::DBParameterGroup")
    assert group["Parameters"]["rds.force_ssl"] == "1"


def test_the_database_is_encrypted_and_private(dev_templates: dict[str, Template]) -> None:
    instance = sole(dev_templates["database"], "AWS::RDS::DBInstance")
    assert instance["StorageEncrypted"] is True
    assert instance["KmsKeyId"]
    assert instance["PubliclyAccessible"] is False
    assert instance["DBName"] == DATABASE_NAME
    assert instance["BackupRetentionPeriod"] == 7
    assert instance["EnableCloudwatchLogsExports"] == ["postgresql"]


def test_the_credential_rotates(dev_templates: dict[str, Template]) -> None:
    schedules = resources(dev_templates["database"], "AWS::SecretsManager::RotationSchedule")
    assert len(schedules) == 1
    properties = next(iter(schedules.values()))["Properties"]
    assert properties["RotationRules"]["ScheduleExpression"] == f"rate({ROTATION_DAYS} days)"


def test_the_application_secret_is_a_separate_document(dev_templates: dict[str, Template]) -> None:
    secrets = resources(dev_templates["database"], "AWS::SecretsManager::Secret")
    names = {resource["Properties"]["Name"] for resource in secrets.values()}
    # Plan D (DEC-860) adds the generated privacy salt, which the application secret references.
    assert names == {"marketing-ai/dev/db", "marketing-ai/dev/app", "marketing-ai/dev/privacy-salt"}


def test_the_composed_url_is_a_deploy_time_reference_not_a_literal(
    dev_templates: dict[str, Template],
) -> None:
    """The password must never be rendered into a template; a dynamic reference is the only way."""
    body = rendered(resources(dev_templates["database"], "AWS::SecretsManager::Secret"))
    assert "{{resolve:secretsmanager:" in body
    assert "postgresql://" in body
    assert "sslmode=require" in body


def test_container_insights_is_off_unless_it_is_asked_for(dev_templates: dict[str, Template]) -> None:
    """A recurring per-task charge is opt-in; AwsSolutions-ECS4 is suppressed only while it is off."""
    cluster = sole(dev_templates["compute"], "AWS::ECS::Cluster")
    settings = {entry["Name"]: entry["Value"] for entry in cluster.get("ClusterSettings", [])}
    assert settings.get("containerInsights") == "disabled"


def test_container_insights_can_be_switched_on(tmp_path_factory: pytest.TempPathFactory) -> None:
    context = AppContext.from_mapping({"env_name": "dev", "container_insights": "true"})
    deployment = build_app(context, account=ACCOUNT, outdir=str(tmp_path_factory.mktemp("insights")))
    cluster = sole(templates_of(deployment)["compute"], "AWS::ECS::Cluster")
    settings = {entry["Name"]: entry["Value"] for entry in cluster["ClusterSettings"]}
    assert settings["containerInsights"] == "enabled"
