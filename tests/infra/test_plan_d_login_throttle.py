"""Plan D M54 (DEC-867): behind the load balancer the sign-in throttle counts the real client.

`client_address` uses the peer address unless `trusted_proxy_hops` says a proxy sits in front, and
behind the ALB the peer is the ALB's own private address for every request. With the setting's
default of 0, twenty wrong passwords from anybody would lock sign-in for everybody. The deployment
therefore publishes `trusted_proxy_hops=1` - and that is right only while the ALB is the one and
only way in and appends (rather than replaces or drops) `X-Forwarded-For`, both asserted here.
"""

from __future__ import annotations

from typing import Any

import pytest
from aws_cdk.assertions import Template
from infra.compute import ALB_PROXY_HOPS

from tests.infra.conftest import ACCOUNT, PROD_CONTEXT_KEYS, REGION, resources


def _parameters(template: Template) -> dict[str, Any]:
    return {
        resource["Properties"]["Name"]: resource["Properties"]["Value"]
        for resource in resources(template, "AWS::SSM::Parameter").values()
    }


def test_the_deployment_trusts_exactly_the_load_balancer(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    assert ALB_PROXY_HOPS == 1
    for env, templates in (("dev", dev_templates), ("prod", prod_templates)):
        assert _parameters(templates["compute"])[f"/marketing-ai/{env}/trusted_proxy_hops"] == "1", env


def test_no_task_variable_overrides_the_parameter(prod_templates: dict[str, Template]) -> None:
    """A task-definition variable silently wins over the parameter of the same name (DEC-377)."""
    for task in resources(prod_templates["compute"], "AWS::ECS::TaskDefinition").values():
        for container in task["Properties"]["ContainerDefinitions"]:
            names = {item["Name"] for item in container.get("Environment", [])}
            assert "MARKETING_AI_TRUSTED_PROXY_HOPS" not in names


def test_the_api_port_is_reachable_from_the_load_balancer_only(prod_templates: dict[str, Template]) -> None:
    """One trusted hop is honest only if nothing but the ALB can reach the tasks."""
    network = prod_templates["network"]
    groups = resources(network, "AWS::EC2::SecurityGroup")
    (service_id,) = [logical_id for logical_id in groups if logical_id.startswith("ServiceSecurityGroup")]
    (alb_id,) = [logical_id for logical_id in groups if logical_id.startswith("AlbSecurityGroup")]
    inline = groups[service_id]["Properties"].get("SecurityGroupIngress", [])
    assert inline == [], "no inline ingress (a CIDR rule would bypass the load balancer)"
    ingress = [
        rule["Properties"]
        for rule in resources(network, "AWS::EC2::SecurityGroupIngress").values()
        if rule["Properties"]["GroupId"] == {"Fn::GetAtt": [service_id, "GroupId"]}
    ]
    assert ingress, "the service group has no ingress at all"
    for rule in ingress:
        assert "CidrIp" not in rule and "CidrIpv6" not in rule
        assert rule["SourceSecurityGroupId"] == {"Fn::GetAtt": [alb_id, "GroupId"]}, rule


def test_the_load_balancer_appends_forwarded_for(prod_templates: dict[str, Template]) -> None:
    """`append` is the ALB default; `preserve` or `remove` would make one hop count the wrong address."""
    (alb,) = resources(prod_templates["compute"], "AWS::ElasticLoadBalancingV2::LoadBalancer").values()
    attributes = {item["Key"]: item["Value"] for item in alb["Properties"].get("LoadBalancerAttributes", [])}
    assert attributes.get("routing.http.xff_header_processing.mode", "append") == "append"


def test_the_published_setting_reaches_the_engine() -> None:
    settings_module = pytest.importorskip(
        "engine.settings", reason="the engine is not installed in this venv; `make infra-setup` installs it"
    )
    from infra.app import build_app
    from infra.context import AppContext

    deployment = build_app(AppContext.from_mapping(dict(PROD_CONTEXT_KEYS)), account=ACCOUNT)
    parameters = deployment.compute.deployment_parameters(
        s3_bucket="marketing-ai-prod-123456789012",
        s3_kms_key_id=f"arn:aws:kms:{REGION}:{ACCOUNT}:key/abc",
        sagemaker_role_arn=f"arn:aws:iam::{ACCOUNT}:role/marketing-ai-prod-sagemaker-execution",
        sagemaker_image_uri=f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/marketing-ai@sha256:" + "0" * 64,
        sagemaker_subnet_ids="subnet-aaaa,subnet-bbbb",
        sagemaker_security_group_ids="sg-aaaa",
    )
    settings = settings_module.Settings.model_validate(
        {"trusted_proxy_hops": parameters["trusted_proxy_hops"]}
    )
    assert settings.trusted_proxy_hops == 1
