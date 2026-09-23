"""`env_name` changes real things, and here is each one, read off the synthesised template.

The table in `AppContext`'s docstring claims six differences between `dev` and `prod`. A claim in a
docstring is worth what the test beside it is worth, so every row has an assertion here, made
against the CloudFormation that would actually be deployed rather than against the context object
that produced it.
"""

from __future__ import annotations

from typing import Any

from aws_cdk.assertions import Template
from infra.context import AppContext

from tests.infra.conftest import rendered, resources, sole


def _instance(templates: dict[str, Template]) -> dict[str, Any]:
    return sole(templates["database"], "AWS::RDS::DBInstance")


def test_multi_az_is_off_in_dev_and_on_in_prod(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    assert _instance(dev_templates)["MultiAZ"] is False
    assert _instance(prod_templates)["MultiAZ"] is True


def test_multi_az_can_still_be_switched_on_in_dev() -> None:
    """The only one of the six a dev deployment may override, so a failover can be rehearsed."""
    assert AppContext.from_mapping({"env_name": "dev", "db_multi_az": "true"}).db_multi_az is True


def test_deletion_protection_is_off_in_dev_and_on_in_prod(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    assert _instance(dev_templates)["DeletionProtection"] is False
    assert _instance(prod_templates)["DeletionProtection"] is True


def test_the_removal_policy_destroys_in_dev_and_retains_in_prod(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    for resource_type, component in (
        ("AWS::S3::Bucket", "storage"),
        ("AWS::KMS::Key", "storage"),
        ("AWS::RDS::DBInstance", "database"),
    ):
        dev_found = resources(dev_templates[component], resource_type)
        prod_found = resources(prod_templates[component], resource_type)
        assert dev_found, f"no {resource_type} in the dev {component} stack"
        for resource in dev_found.values():
            assert resource["DeletionPolicy"] == "Delete", resource_type
        for resource in prod_found.values():
            assert resource["DeletionPolicy"] == "Retain", resource_type


def test_dev_serves_plain_http_and_prod_serves_https(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    dev_listener = sole(dev_templates["compute"], "AWS::ElasticLoadBalancingV2::Listener")
    prod_listener = sole(prod_templates["compute"], "AWS::ElasticLoadBalancingV2::Listener")
    assert (dev_listener["Protocol"], dev_listener["Port"]) == ("HTTP", 80)
    assert "Certificates" not in dev_listener
    assert (prod_listener["Protocol"], prod_listener["Port"]) == ("HTTPS", 443)
    assert prod_listener["Certificates"]
    assert prod_listener["SslPolicy"].startswith("ELBSecurityPolicy-TLS13")


def test_the_load_balancer_only_opens_the_port_it_listens_on(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    dev_ports = _ingress_ports(dev_templates["network"])
    prod_ports = _ingress_ports(prod_templates["network"])
    assert 80 in dev_ports and 443 not in dev_ports
    assert 443 in prod_ports and 80 not in prod_ports


def _ingress_ports(template: Template) -> set[int]:
    ports: set[int] = set()
    for group in resources(template, "AWS::EC2::SecurityGroup").values():
        for rule in group["Properties"].get("SecurityGroupIngress", []) or []:
            if rule.get("CidrIp") == "0.0.0.0/0":
                ports.add(int(rule["FromPort"]))
    return ports


def test_log_retention_is_a_month_in_dev_and_a_year_in_prod(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    for group in resources(dev_templates["observability"], "AWS::Logs::LogGroup").values():
        assert group["Properties"]["RetentionInDays"] == 30
    for group in resources(prod_templates["observability"], "AWS::Logs::LogGroup").values():
        assert group["Properties"]["RetentionInDays"] == 365


def test_cors_is_open_in_dev_and_named_in_prod(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    assert _parameter(dev_templates["compute"], "cors_origins") == "*"
    assert _parameter(prod_templates["compute"], "cors_origins") == "https://marketing.example.com"


def _parameter(template: Template, field_name: str) -> str:
    for resource in resources(template, "AWS::SSM::Parameter").values():
        if resource["Properties"]["Name"].endswith(f"/{field_name}"):
            value = resource["Properties"]["Value"]
            assert isinstance(value, str), f"{field_name} is a token, not a literal"
            return value
    raise AssertionError(f"no SSM parameter for {field_name}")


def test_nothing_else_differs_between_the_two_deployments() -> None:
    """The table claims six rows. A seventh conditional would be an undocumented surprise."""
    dev = AppContext.from_mapping({"env_name": "dev"})
    prod = AppContext.from_mapping(
        {
            "env_name": "prod",
            "certificate_arn": "arn:aws:acm:ap-south-1:123456789012:certificate/a",
            "domain_name": "marketing.example.com",
        }
    )
    differing = {
        name for name in vars(AppContext).get("__slots__", ()) if getattr(dev, name) != getattr(prod, name)
    }
    assert differing == {
        "env_name",
        "db_multi_az",
        "db_deletion_protection",
        "removal_policy_destroy",
        "https_only",
        "log_retention_days",
        "kms_key_alias",
        "certificate_arn",
        "domain_name",
        # Phase 4b: the Object Lock period of an audit export. The row is in `AppContext`'s table and
        # its template assertion is in tests/infra/test_phase4b_context.py.
        "audit_retention_days",
    }


def test_the_budget_records_its_answer_either_way(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    assert not resources(dev_templates["budgets"], "AWS::Budgets::Budget")
    assert "none" in rendered(sole(dev_templates["budgets"], "AWS::SSM::Parameter"))
    budget = sole(prod_templates["budgets"], "AWS::Budgets::Budget")
    assert budget["Budget"]["BudgetLimit"] == {"Amount": 750, "Unit": "USD"}


def test_the_database_does_not_listen_on_the_postgres_default_port(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    """Worth little on its own and free, which is why it is done; AwsSolutions-RDS11 asks for it."""
    from infra.network import POSTGRES_PORT

    assert POSTGRES_PORT != 5432
    for templates in (dev_templates, prod_templates):
        # CloudFormation renders DBInstance.Port as a string, whatever CDK was handed.
        assert _instance(templates)["Port"] == str(POSTGRES_PORT)


def test_the_client_ingress_rule_names_the_same_port(dev_templates: dict[str, Template]) -> None:
    """One constant read twice: a database on a port nothing may reach is a silent outage."""
    from infra.network import POSTGRES_PORT

    ingress = resources(dev_templates["database"], "AWS::EC2::SecurityGroupIngress")
    client_rules = [
        rule["Properties"]
        for rule in ingress.values()
        if rule["Properties"].get("Description") == "From an application principal"
    ]
    assert len(client_rules) == 2, "the service and the jobs security groups both reach the database"
    for rule in client_rules:
        assert rule["FromPort"] == POSTGRES_PORT
        assert rule["ToPort"] == POSTGRES_PORT
