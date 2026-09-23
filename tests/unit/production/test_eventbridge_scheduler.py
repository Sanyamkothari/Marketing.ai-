"""EventBridge Scheduler sync, against moto's `scheduler` service (DEC-765). Never real AWS.

moto (5.x) implements CreateSchedule, GetSchedule, UpdateSchedule and DeleteSchedule with their
targets, ECS parameters and state, which is the whole of what `EventBridgeScheduler` calls; it does
not *fire* schedules, so firing is tested through the CLI instead (`test_fire_schedule_cli.py`).
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from engine.scheduling.scheduler import (
    FIRE_SCHEDULE_COMMAND,
    JOB_CONTAINER_NAME,
    SCHEDULED_TIME_PLACEHOLDER,
    EcsTarget,
    EventBridgeScheduler,
    build_scheduler,
)
from engine.scheduling.schedules import Schedule, ScheduleKind, ScheduleParameters
from engine.settings import Settings, SettingsError

REGION = "ap-south-1"
ACCOUNT = "123456789012"
GROUP = "marketing-ai-dev"
CLUSTER = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/marketing-ai-dev"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/marketing-ai-dev-scheduler"
T0 = datetime(2026, 9, 1, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[3]


def settings(**changes: Any) -> Settings:
    values: dict[str, Any] = {
        "scheduler_backend": "eventbridge",
        "scheduler_group_name": GROUP,
        "scheduler_target_arn": CLUSTER,
        "scheduler_role_arn": ROLE,
        "aws_region": REGION,
        "sagemaker_subnet_ids": ("subnet-a", "subnet-b"),
        "sagemaker_security_group_ids": ("sg-jobs",),
    }
    values.update(changes)
    return Settings(**values)


def schedule(**changes: Any) -> Schedule:
    values: dict[str, Any] = {
        "schedule_id": "sch_0123abcd4567",
        "client_id": "c_demo_1",
        "use_case_id": "telco-churn",
        "kind": ScheduleKind.SCORE,
        "cron": "0 2 1 * *",
        "parameters": ScheduleParameters(onboarding_spec_id="spec_1"),
        "created_by": "u",
        "created_at": T0,
        "updated_at": T0,
    }
    values.update(changes)
    return Schedule(**values)


@pytest.fixture
def client() -> Iterator[Any]:
    with mock_aws():
        made = boto3.client("scheduler", region_name=REGION)
        made.create_schedule_group(Name=GROUP)
        yield made


def test_the_ecs_target_is_derived_from_the_cluster_and_the_job_network() -> None:
    made = EventBridgeScheduler.from_settings(settings())
    assert made.ecs == EcsTarget(
        task_definition_arn=f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/marketing-ai-dev-job",
        subnets=("subnet-a", "subnet-b"),
        security_groups=("sg-jobs",),
    )


def test_an_ecs_target_without_subnets_is_refused_at_construction() -> None:
    with pytest.raises(SettingsError) as caught:
        EventBridgeScheduler.from_settings(settings(sagemaker_subnet_ids=()))
    assert caught.value.env_var == "MARKETING_AI_SAGEMAKER_SUBNET_IDS"


def test_sync_creates_the_schedule_with_the_eventbridge_expression(client: Any) -> None:
    scheduler = build_scheduler(settings(), store=None, client=client)  # type: ignore[arg-type]
    scheduler.sync(schedule())
    stored = client.get_schedule(Name="sch_0123abcd4567", GroupName=GROUP)
    assert stored["ScheduleExpression"] == "cron(0 2 1 * ? *)"
    assert stored["ScheduleExpressionTimezone"] == "Asia/Kolkata"
    assert stored["State"] == "ENABLED"
    assert stored["FlexibleTimeWindow"] == {"Mode": "OFF"}
    target = stored["Target"]
    assert target["Arn"] == CLUSTER
    assert target["RoleArn"] == ROLE
    assert target["RetryPolicy"] == {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 3600}
    ecs = target["EcsParameters"]
    assert ecs["TaskDefinitionArn"].endswith(
        ":task-definition/marketing-ai-dev-job"
    ), "a family, not a revision"
    assert ecs["LaunchType"] == "FARGATE"
    assert ecs["NetworkConfiguration"]["awsvpcConfiguration"] == {
        "Subnets": ["subnet-a", "subnet-b"],
        "SecurityGroups": ["sg-jobs"],
        "AssignPublicIp": "DISABLED",
    }


def test_the_input_overrides_the_job_command_with_the_schedule_and_its_slot(client: Any) -> None:
    EventBridgeScheduler.from_settings(settings(), client=client).sync(schedule())
    overrides = json.loads(client.get_schedule(Name="sch_0123abcd4567", GroupName=GROUP)["Target"]["Input"])
    (container,) = overrides["containerOverrides"]
    assert container["name"] == JOB_CONTAINER_NAME
    assert container["command"] == [
        *FIRE_SCHEDULE_COMMAND,
        "--schedule-id",
        "sch_0123abcd4567",
        "--scheduled-time",
        SCHEDULED_TIME_PLACEHOLDER,
    ]


def test_a_second_sync_updates_in_place_including_the_state(client: Any) -> None:
    scheduler = EventBridgeScheduler.from_settings(settings(), client=client)
    scheduler.sync(schedule())
    scheduler.sync(schedule(cron="30 6 * * 1-5", timezone="UTC", enabled=False))
    stored = client.get_schedule(Name="sch_0123abcd4567", GroupName=GROUP)
    assert stored["ScheduleExpression"] == "cron(30 6 ? * 2-6 *)"
    assert stored["ScheduleExpressionTimezone"] == "UTC"
    assert stored["State"] == "DISABLED"


def test_remove_deletes_and_tolerates_an_already_deleted_schedule(client: Any) -> None:
    scheduler = EventBridgeScheduler.from_settings(settings(), client=client)
    scheduler.sync(schedule())
    scheduler.remove("sch_0123abcd4567")
    with pytest.raises(client.exceptions.ResourceNotFoundException):
        client.get_schedule(Name="sch_0123abcd4567", GroupName=GROUP)
    scheduler.remove("sch_0123abcd4567")


def test_a_non_ecs_target_gets_plain_json(client: Any) -> None:
    function = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:fire"
    scheduler = EventBridgeScheduler.from_settings(settings(scheduler_target_arn=function), client=client)
    assert scheduler.ecs is None
    scheduler.sync(schedule())
    target = client.get_schedule(Name="sch_0123abcd4567", GroupName=GROUP)["Target"]
    assert "EcsParameters" not in target
    assert json.loads(target["Input"]) == {
        "schedule_id": "sch_0123abcd4567",
        "scheduled_time": SCHEDULED_TIME_PLACEHOLDER,
    }


def _infra_constant(module: str, name: str) -> Any:
    """A literal constant of an `infra/` module, read from its source: importing it needs aws-cdk-lib."""
    tree = ast.parse((REPO / "infra" / f"{module}.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            assert node.value is not None
            return ast.literal_eval(node.value)
    raise AssertionError(f"infra/{module}.py defines no {name}")


def _infra_return(module: str, function: str) -> str:
    """The source of the single `return` of an `infra/` function, e.g. an f-string naming rule."""
    tree = ast.parse((REPO / "infra" / f"{module}.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function:
            (returned,) = [item for item in node.body if isinstance(item, ast.Return)]
            assert returned.value is not None
            return ast.unparse(returned.value)
    raise AssertionError(f"infra/{module}.py defines no {function}")


def test_the_command_and_names_match_the_deployed_task_definition() -> None:
    """The engine spells the job command itself (it never imports the CDK app); the two must agree."""
    assert _infra_constant("operations", "FIRE_SCHEDULE_COMMAND") == FIRE_SCHEDULE_COMMAND
    assert _infra_constant("naming", "JOB_CONTAINER_NAME") == JOB_CONTAINER_NAME
    # The task family is the cluster's name plus `-job`: that is what `EcsTarget.for_cluster` derives.
    assert _infra_return("naming", "cluster_name") == "f'{PRODUCT}-{env_name}'"
    assert _infra_return("naming", "job_task_family") == "f'{PRODUCT}-{env_name}-job'"
