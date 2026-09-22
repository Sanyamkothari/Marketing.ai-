"""The VPC's shape, and the two configurations of how a private subnet reaches AWS."""

from __future__ import annotations

from aws_cdk.assertions import Template
from infra.app import build_app
from infra.context import REQUIRED_PRIVATE_ENDPOINTS, AppContext
from infra.network import POSTGRES_PORT

from tests.infra.conftest import ACCOUNT, resources, templates_of


def test_two_availability_zones_three_subnet_groups(dev_templates: dict[str, Template]) -> None:
    subnets = resources(dev_templates["network"], "AWS::EC2::Subnet")
    assert len(subnets) == 6, "public, app and db, in each of two AZs"
    zones = {
        resource["Properties"]["AvailabilityZone"]
        for resource in subnets.values()
        if isinstance(resource["Properties"].get("AvailabilityZone"), str)
    }
    assert len(zones) == 2


def test_the_database_subnets_have_no_route_to_the_internet(
    dev_templates: dict[str, Template],
) -> None:
    template = dev_templates["network"]
    isolated = [
        logical_id
        for logical_id, resource in resources(template, "AWS::EC2::Subnet").items()
        if "db" in logical_id.lower()
    ]
    assert len(isolated) == 2
    for route in resources(template, "AWS::EC2::Route").values():
        if "NatGatewayId" in route["Properties"] or "GatewayId" in route["Properties"]:
            table = route["Properties"]["RouteTableId"]
            assert "db" not in str(table).lower()


def test_the_s3_gateway_endpoint_is_always_created(dev_templates: dict[str, Template]) -> None:
    endpoints = resources(dev_templates["network"], "AWS::EC2::VPCEndpoint")
    gateways = [
        resource
        for resource in endpoints.values()
        if resource["Properties"].get("VpcEndpointType", "Gateway") == "Gateway"
    ]
    assert len(gateways) == 1


def test_by_default_there_is_one_nat_gateway_and_no_interface_endpoint(
    dev_templates: dict[str, Template],
) -> None:
    template = dev_templates["network"]
    assert len(resources(template, "AWS::EC2::NatGateway")) == 1
    interfaces = [
        resource
        for resource in resources(template, "AWS::EC2::VPCEndpoint").values()
        if resource["Properties"].get("VpcEndpointType") == "Interface"
    ]
    assert not interfaces


def test_without_a_nat_gateway_every_required_endpoint_is_created() -> None:
    deployment = build_app(
        AppContext.from_mapping(
            {
                "env_name": "dev",
                "nat_gateways": "0",
                "vpc_endpoints": ",".join(REQUIRED_PRIVATE_ENDPOINTS),
            }
        ),
        account=ACCOUNT,
    )
    template = templates_of(deployment)["network"]
    assert not resources(template, "AWS::EC2::NatGateway")
    interfaces = [
        resource["Properties"]["ServiceName"]
        for resource in resources(template, "AWS::EC2::VPCEndpoint").values()
        if resource["Properties"].get("VpcEndpointType") == "Interface"
    ]
    assert len(interfaces) == len(REQUIRED_PRIVATE_ENDPOINTS)


def test_flow_logs_are_captured(dev_templates: dict[str, Template]) -> None:
    flow_logs = resources(dev_templates["network"], "AWS::EC2::FlowLog")
    assert len(flow_logs) == 1
    assert next(iter(flow_logs.values()))["Properties"]["TrafficType"] == "ALL"


def test_the_tasks_are_reachable_only_from_the_load_balancer(
    dev_templates: dict[str, Template],
) -> None:
    ingress = resources(dev_templates["network"], "AWS::EC2::SecurityGroupIngress")
    to_tasks = [
        resource["Properties"]
        for resource in ingress.values()
        if resource["Properties"].get("FromPort") == 8000
    ]
    assert to_tasks, "nothing can reach the API port"
    for rule in to_tasks:
        assert "CidrIp" not in rule, "the task port is open to the internet"
        assert "SourceSecurityGroupId" in rule


def test_the_database_is_reachable_only_from_named_groups(
    dev_templates: dict[str, Template],
) -> None:
    ingress = resources(dev_templates["database"], "AWS::EC2::SecurityGroupIngress")
    to_database = [
        resource["Properties"]
        for resource in ingress.values()
        if resource["Properties"].get("FromPort") == POSTGRES_PORT
    ]
    assert len(to_database) >= 2, "the API tasks and the jobs"
    for rule in to_database:
        assert "CidrIp" not in rule
