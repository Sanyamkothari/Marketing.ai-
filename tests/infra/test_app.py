"""The application as a whole: eight stacks, one direction, and a tag on everything."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Template
from infra.app import FEATURE_FLAGS, STACK_ORDER, Deployment
from infra.naming import stack_name


def test_eight_stacks_named_for_the_deployment(dev: Deployment) -> None:
    """Seven in Phase 4a; Phase 4b's `operations` stack is the eighth (infra/operations.py says why)."""
    assert len(STACK_ORDER) == 8
    assert [stack.stack_name for stack in dev.stacks()] == [
        stack_name("dev", component) for component in STACK_ORDER
    ]


def test_the_dependency_graph_is_a_straight_line(dev: Deployment) -> None:
    """A cycle between stacks is not a slow deployment, it is one that cannot be performed."""
    order = {stack.stack_name: index for index, stack in enumerate(dev.stacks())}
    for stack in dev.stacks():
        for dependency in stack.dependencies:
            assert (
                order[dependency.stack_name] < order[stack.stack_name]
            ), f"{stack.stack_name} depends on {dependency.stack_name}, which comes after it"


def test_observability_is_deployed_before_the_database(dev: Deployment) -> None:
    """Whoever creates `/aws/rds/instance/.../postgresql` first decides its retention."""
    assert STACK_ORDER.index("observability") < STACK_ORDER.index("database")
    names = {dependency.stack_name for dependency in dev.database.dependencies}
    assert dev.observability.stack_name in names


def test_compute_is_deployed_before_operations(dev: Deployment) -> None:
    """Phase 4b attaches its grants to the task role and runs its job in compute's cluster."""
    assert STACK_ORDER.index("compute") < STACK_ORDER.index("operations")
    names = {dependency.stack_name for dependency in dev.operations.dependencies}
    assert dev.compute.stack_name in names


def test_sagemaker_is_deployed_before_compute(dev: Deployment) -> None:
    """The task role's `iam:PassRole` names the execution role, so the role exists first."""
    assert STACK_ORDER.index("sagemaker") < STACK_ORDER.index("compute")


def test_every_resource_that_can_be_tagged_is(dev: Deployment) -> None:
    untagged: list[tuple[str, str]] = []
    for stack in dev.stacks():
        template = Template.from_stack(stack).to_json()
        for logical_id, resource in template.get("Resources", {}).items():
            properties = resource.get("Properties", {})
            if "Tags" not in properties:
                continue
            tags = properties["Tags"]
            pairs = {entry["Key"]: entry["Value"] for entry in tags} if isinstance(tags, list) else tags
            if pairs.get("product") != "marketing-ai" or pairs.get("env") != "dev":
                untagged.append((stack.stack_name, logical_id))
    assert not untagged, f"missing product/env tags on {untagged}"


def test_the_client_tag_is_applied_when_a_client_is_named(prod: Deployment) -> None:
    template = Template.from_stack(prod.storage).to_json()
    tagged = [
        resource
        for resource in template["Resources"].values()
        if isinstance(resource.get("Properties", {}).get("Tags"), list)
    ]
    assert tagged
    for resource in tagged:
        pairs = {entry["Key"]: entry["Value"] for entry in resource["Properties"]["Tags"]}
        assert pairs["client"] == "acme"


def test_a_test_and_the_cli_synthesise_under_the_same_feature_flags(dev: Deployment) -> None:
    """A flag set only in `cdk.json` would make these assertions describe a different app."""
    for flag, value in FEATURE_FLAGS.items():
        assert dev.app.node.try_get_context(flag) == value


def test_the_stacks_are_environment_aware(dev: Deployment) -> None:
    for stack in dev.stacks():
        assert stack.region == "ap-south-1"
        assert not cdk.Token.is_unresolved(stack.account)
