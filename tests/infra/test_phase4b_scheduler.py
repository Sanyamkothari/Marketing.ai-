"""EventBridge Scheduler: one group, one role that can start one task, and the task it starts.

The shape being defended is a chain of three narrow permissions, each of which would be a
privilege escalation if it were wide:

    API task role --PassRole(scheduler role, to scheduler)--> schedule in *this* group
    scheduler role --RunTask(job family, in *this* cluster) + PassRole(task roles, to ECS)--> task
    job task --runs as the API's own task role--> nothing the API could not already do

so every link is asserted on the synthesised template, both ends of it.
"""

from __future__ import annotations

from typing import Any

import pytest
from aws_cdk.assertions import Template
from infra.app import Deployment
from infra.naming import (
    JOB_CONTAINER_NAME,
    NON_FIELD_ENV_VARS,
    SETTINGS_ENV_VARS,
    cluster_name,
    job_task_family,
    schedule_group_name,
)
from infra.operations import FIRE_SCHEDULE_COMMAND

from tests.infra.conftest import ACCOUNT, REGION, as_list, rendered, resources, sole, statements


def _role(template: Template, role_name: str) -> tuple[str, dict[str, Any]]:
    for logical_id, resource in resources(template, "AWS::IAM::Role").items():
        if resource["Properties"].get("RoleName") == role_name:
            return logical_id, dict(resource["Properties"])
    raise AssertionError(f"no role named {role_name}")


def _policy_statements(template: Template, *, attached_to: str) -> list[dict[str, Any]]:
    """Statements of the policies in this template whose `Roles` mention `attached_to`."""
    found: list[dict[str, Any]] = []
    for resource in resources(template, "AWS::IAM::Policy").values():
        if attached_to in rendered(resource["Properties"].get("Roles")):
            found.extend(
                dict(statement) for statement in resource["Properties"]["PolicyDocument"]["Statement"]
            )
    return found


def _api_statements(templates: dict[str, Template]) -> list[dict[str, Any]]:
    return _policy_statements(templates["operations"], attached_to="TaskRole")


def _scheduler_statements(templates: dict[str, Template]) -> list[dict[str, Any]]:
    logical_id, _ = _role(templates["operations"], "marketing-ai-dev-scheduler")
    return _policy_statements(templates["operations"], attached_to=logical_id)


def _job_task_definition(templates: dict[str, Template]) -> dict[str, Any]:
    return sole(templates["operations"], "AWS::ECS::TaskDefinition")


def test_the_schedule_group_is_named_for_the_deployment(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    assert sole(dev_templates["operations"], "AWS::Scheduler::ScheduleGroup")["Name"] == "marketing-ai-dev"
    assert sole(prod_templates["operations"], "AWS::Scheduler::ScheduleGroup")["Name"] == "marketing-ai-prod"
    assert schedule_group_name("dev") == "marketing-ai-dev"


def test_no_schedule_is_created_by_the_deployment(dev_templates: dict[str, Template]) -> None:
    """Schedules are the application's data (one per client x use case), not infrastructure."""
    for template in dev_templates.values():
        assert not resources(template, "AWS::Scheduler::Schedule")


def test_only_the_scheduler_in_this_account_and_group_may_assume_the_role(
    dev_templates: dict[str, Template],
) -> None:
    _, role = _role(dev_templates["operations"], "marketing-ai-dev-scheduler")
    trust = role["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust) == 1
    assert trust[0]["Principal"] == {"Service": "scheduler.amazonaws.com"}
    assert trust[0]["Condition"] == {
        "StringEquals": {
            "aws:SourceAccount": ACCOUNT,
            "aws:SourceArn": f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule-group/marketing-ai-dev",
        }
    }


def test_the_scheduler_role_can_do_three_things_and_no_fourth(dev_templates: dict[str, Template]) -> None:
    found = _scheduler_statements(dev_templates)
    actions = sorted(action for statement in found for action in as_list(statement["Action"]))
    assert actions == ["ecs:RunTask", "ecs:TagResource", "iam:PassRole"]
    for statement in found:
        assert statement["Effect"] == "Allow"


def test_run_task_is_one_family_in_one_cluster(dev_templates: dict[str, Template]) -> None:
    (statement,) = [s for s in _scheduler_statements(dev_templates) if "ecs:RunTask" in as_list(s["Action"])]
    family = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{job_task_family('dev')}"
    assert as_list(statement["Resource"]) == [family, f"{family}:*"]
    condition = statement["Condition"]["ArnEquals"]["ecs:cluster"]
    assert "Cluster" in rendered(condition), "RunTask must be confined to this deployment's cluster"


def test_tagging_is_only_part_of_run_task(dev_templates: dict[str, Template]) -> None:
    (statement,) = [
        s for s in _scheduler_statements(dev_templates) if "ecs:TagResource" in as_list(s["Action"])
    ]
    assert as_list(statement["Resource"]) == [f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/{cluster_name('dev')}/*"]
    assert statement["Condition"] == {"StringEquals": {"ecs:CreateAction": "RunTask"}}


def test_the_scheduler_passes_the_task_s_two_roles_to_ecs_only(dev_templates: dict[str, Template]) -> None:
    (statement,) = [s for s in _scheduler_statements(dev_templates) if "iam:PassRole" in as_list(s["Action"])]
    assert statement["Condition"] == {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}}
    body = rendered(statement["Resource"])
    assert len(as_list(statement["Resource"])) == 2
    assert "TaskRole" in body and "ExecutionRole" in body


def test_the_api_manages_schedules_in_its_own_group_only(dev_templates: dict[str, Template]) -> None:
    found = [
        statement
        for statement in _api_statements(dev_templates)
        if any(action.startswith("scheduler:") for action in as_list(statement["Action"]))
    ]
    assert len(found) == 1
    statement = found[0]
    assert sorted(as_list(statement["Action"])) == [
        "scheduler:CreateSchedule",
        "scheduler:DeleteSchedule",
        "scheduler:GetSchedule",
        "scheduler:UpdateSchedule",
    ]
    assert as_list(statement["Resource"]) == [
        f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule/marketing-ai-dev/*"
    ]


def test_no_role_may_list_schedules_or_touch_groups(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    for templates in (dev_templates, prod_templates):
        for template in templates.values():
            for statement in statements(template, effect="Allow"):
                for action in as_list(statement.get("Action")):
                    assert not action.startswith("scheduler:List"), action
                    assert "ScheduleGroup" not in action, action


def test_the_api_may_pass_the_scheduler_role_and_only_to_the_scheduler(
    dev_templates: dict[str, Template],
) -> None:
    found = [s for s in _api_statements(dev_templates) if "iam:PassRole" in as_list(s["Action"])]
    assert len(found) == 1
    statement = found[0]
    assert statement["Condition"] == {"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}}
    assert len(as_list(statement["Resource"])) == 1
    assert "SchedulerRole" in rendered(statement["Resource"])


def test_every_pass_role_in_the_app_is_conditioned_on_a_service(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    """`iam:PassRole` without `iam:PassedToService` lets the holder hand the role to anything."""
    for templates in (dev_templates, prod_templates):
        for component, template in templates.items():
            for statement in statements(template, effect="Allow"):
                if "iam:PassRole" in as_list(statement.get("Action")):
                    assert "iam:PassedToService" in rendered(statement.get("Condition")), component
                    assert "*" not in as_list(statement.get("Resource")), component


def test_the_compute_stack_s_own_pass_role_is_untouched(dev_templates: dict[str, Template]) -> None:
    """Phase 4b's grant lives in its own policy; compute still passes one role to one service."""
    found = [
        s
        for s in statements(dev_templates["compute"], effect="Allow")
        if "iam:PassRole" in as_list(s["Action"])
    ]
    assert len(found) == 1
    assert found[0]["Condition"] == {"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}}


def test_the_job_task_runs_the_api_s_image_as_the_api_s_roles(dev: Deployment) -> None:
    operations = Template.from_stack(dev.operations)
    definition = sole(operations, "AWS::ECS::TaskDefinition")
    api = sole(Template.from_stack(dev.compute), "AWS::ECS::TaskDefinition")
    assert definition["Family"] == job_task_family("dev")
    assert rendered(definition["ContainerDefinitions"][0]["Image"]) == rendered(
        api["ContainerDefinitions"][0]["Image"]
    )
    assert "TaskRole" in rendered(definition["TaskRoleArn"])
    assert "ExecutionRole" in rendered(definition["ExecutionRoleArn"])
    assert definition["RequiresCompatibilities"] == ["FARGATE"]
    assert definition["NetworkMode"] == "awsvpc"
    assert (definition["Cpu"], definition["Memory"]) == ("1024", "4096")


def test_the_job_container_fires_a_schedule(dev_templates: dict[str, Template]) -> None:
    containers = _job_task_definition(dev_templates)["ContainerDefinitions"]
    assert len(containers) == 1
    container = containers[0]
    assert container["Name"] == JOB_CONTAINER_NAME, "a schedule's containerOverrides names this"
    assert container["Command"] == list(FIRE_SCHEDULE_COMMAND)
    assert container["Essential"] is True


def test_the_job_container_carries_the_same_three_variables_as_the_api(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    """Anything more would override Parameter Store for ever (DEC-377); anything unknown won't boot."""
    allowed = set(SETTINGS_ENV_VARS.values()) | NON_FIELD_ENV_VARS
    for templates in (dev_templates, prod_templates):
        job = _job_task_definition(templates)["ContainerDefinitions"][0]
        api = sole(templates["compute"], "AWS::ECS::TaskDefinition")["ContainerDefinitions"][0]
        assert job["Environment"] == api["Environment"]
        assert {entry["Name"] for entry in job["Environment"]} <= allowed
        assert not job.get("Secrets")


def test_the_job_container_logs_into_the_api_group_under_its_own_prefix(
    dev_templates: dict[str, Template],
) -> None:
    options = _job_task_definition(dev_templates)["ContainerDefinitions"][0]["LogConfiguration"]
    assert options["LogDriver"] == "awslogs"
    assert options["Options"]["awslogs-stream-prefix"] == "job"
    assert "ApiLogs" in rendered(options["Options"]["awslogs-group"])


def test_the_job_task_did_not_widen_the_compute_stack_s_roles(dev: Deployment) -> None:
    """The roles are imported immutably, so no grant CDK makes for the job task lands on them.

    Searching the compute template for "operations" or "JobTaskDefinition" would not see such a
    grant: the `awslogs` driver's statement names only a log group. What does give one away is a
    missing `Sid` - every statement `infra/policies.py` writes carries one, and a statement CDK adds
    by itself never does. The compute stack has exactly one of those, and it is its own: the API
    container's log driver on the API log group. (A job-task grant on that same group would be
    merged into it by CDK, which is why the job logs there - and why this asserts on the *set* of
    Sid-less statements rather than on the absence of any.)
    """
    compute = Template.from_stack(dev.compute)
    role_policies = [
        resource
        for resource in resources(compute, "AWS::IAM::Policy").values()
        if any(
            name in rendered(resource["Properties"].get("Roles")) for name in ("TaskRole", "ExecutionRole")
        )
    ]
    assert len(role_policies) == 2, "one DefaultPolicy per API role"
    unnamed = [
        statement
        for resource in role_policies
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]
        if not statement.get("Sid")
    ]
    assert len(unnamed) == 1, f"a grant nobody wrote landed on a compute role: {unnamed}"
    assert sorted(as_list(unnamed[0]["Action"])) == ["logs:CreateLogStream", "logs:PutLogEvents"]
    assert "ApiLogs" in rendered(unnamed[0]["Resource"])
    assert "JobTaskDefinition" not in rendered(compute.to_json())


def test_the_engine_derives_the_same_job_family_from_the_cluster(dev: Deployment) -> None:
    """The engine never reads `JobTaskDefinitionArn`: it derives the family from `scheduler_target_arn`.

    `engine.scheduling.scheduler.EcsTarget.for_cluster` takes the cluster ARN this stack writes to
    Parameter Store and appends `-job`; a schedule created from anything else would name a family
    the scheduler role may not run. Its network is the SageMaker jobs' subnets and security group,
    which the compute stack writes and the database already admits.
    """
    scheduler_module = pytest.importorskip(
        "engine.scheduling.scheduler", reason="the engine is not installed in this venv"
    )
    cluster_arn = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{cluster_name('dev')}"
    target = scheduler_module.EcsTarget.for_cluster(
        cluster_arn, subnets=("subnet-a",), security_groups=("sg-a",)
    )
    assert target is not None
    assert target.task_definition_arn == dev.operations.job_task_definition_family_arn
    assert target.container_name == JOB_CONTAINER_NAME
    assert scheduler_module.FIRE_SCHEDULE_COMMAND == FIRE_SCHEDULE_COMMAND
    leaves = {
        resource["Properties"]["Name"].rsplit("/", 1)[-1]
        for resource in resources(Template.from_stack(dev.compute), "AWS::SSM::Parameter").values()
    }
    assert {"sagemaker_subnet_ids", "sagemaker_security_group_ids"} <= leaves


def test_the_family_arn_a_schedule_should_name_has_no_revision(dev: Deployment) -> None:
    arn = dev.operations.job_task_definition_family_arn
    assert arn == f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{job_task_family('dev')}"


def test_the_cluster_name_the_scheduler_is_scoped_to_is_compute_s(dev_templates: dict[str, Template]) -> None:
    assert sole(dev_templates["compute"], "AWS::ECS::Cluster")["ClusterName"] == cluster_name("dev")
