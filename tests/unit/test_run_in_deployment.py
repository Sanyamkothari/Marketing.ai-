"""`scripts/run_in_deployment.py`: one command, inside the deployment, as the deployment.

M50's walk of `docs/AWS_DEPLOYMENT.md` found that the migration, the permission probes and the first
Admin all have to run inside the VPC as the task role, and that nothing in the tree started such a
task. This module is that thing, so what matters about it is exactly what makes it trustworthy:

* it runs the **service's** task definition on the **service's** network - read from the running
  service, never from a note - so the one-off task has the same image, role and security groups;
* it overrides the command of the right container and **nothing else**: no environment variable,
  because an override's environment is readable by anyone allowed `DescribeTasks` and lands in
  CloudTrail;
* its exit code is the command's exit code, and a task that never produced one is distinguishable
  from a command that failed;
* it prints the task's log, and a task that died before its container started (no stream) is not a
  crash.

ECS is driven by a hand-written fake rather than moto: moto 5.2.3's `RunTask` for an `awsvpc`
Fargate task raises inside moto itself (`NetworkInterface` has no `private_dns_name`), and a fake
that records the request is the more direct assertion anyway. CloudWatch Logs is moto. Nothing here
reaches AWS.
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import WaiterError
from moto import mock_aws

from scripts import run_in_deployment as rid

TASK_ARN = "arn:aws:ecs:ap-south-1:111122223333:task/marketing-ai-dev/0123456789abcdef"
TASK_DEFINITION = "arn:aws:ecs:ap-south-1:111122223333:task-definition/marketing-ai-dev-api:7"
NETWORK = {
    "awsvpcConfiguration": {
        "subnets": ["subnet-a", "subnet-b"],
        "securityGroups": ["sg-service"],
        "assignPublicIp": "DISABLED",
    }
}


class FakeWaiter:
    def __init__(self, *, times_out: bool) -> None:
        self.times_out = times_out
        self.calls: list[dict[str, Any]] = []

    def wait(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.times_out:
            raise WaiterError(name="TasksStopped", reason="Max attempts exceeded", last_response={})


class FakeEcs:
    """Records every request; answers like ECS for one service and one task."""

    def __init__(
        self,
        *,
        services: list[dict[str, Any]] | None = None,
        run_response: dict[str, Any] | None = None,
        exit_code: int | None = 0,
        times_out: bool = False,
    ) -> None:
        self.services = (
            services
            if services is not None
            else [{"status": "ACTIVE", "taskDefinition": TASK_DEFINITION, "networkConfiguration": NETWORK}]
        )
        self.run_response = run_response if run_response is not None else {"tasks": [{"taskArn": TASK_ARN}]}
        self.exit_code = exit_code
        self.waiter = FakeWaiter(times_out=times_out)
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def describe_services(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(("describe_services", kwargs))
        return {"services": self.services}

    def run_task(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(("run_task", kwargs))
        return self.run_response

    def get_waiter(self, name: str) -> FakeWaiter:
        assert name == "tasks_stopped"
        return self.waiter

    def describe_tasks(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(("describe_tasks", kwargs))
        container: dict[str, Any] = {"name": "api"}
        if self.exit_code is not None:
            container["exitCode"] = self.exit_code
        return {
            "tasks": [
                {
                    "taskArn": TASK_ARN,
                    "stoppedReason": "Essential container in task exited",
                    "containers": [container],
                }
            ]
        }

    def request(self, name: str) -> dict[str, Any]:
        return next(kwargs for called, kwargs in self.requests if called == name)


@pytest.fixture
def logs() -> Any:
    with mock_aws():
        client = boto3.client("logs", region_name="ap-south-1")
        client.create_log_group(logGroupName="/marketing-ai/dev/api")
        yield client


def _write_log(logs: Any, *messages: str) -> None:
    stream = rid.log_stream_name(TASK_ARN)
    now = int(time.time() * 1000)  # CloudWatch (and moto) reject events older than fourteen days
    logs.create_log_stream(logGroupName="/marketing-ai/dev/api", logStreamName=stream)
    logs.put_log_events(
        logGroupName="/marketing-ai/dev/api",
        logStreamName=stream,
        logEvents=[{"timestamp": now + index, "message": text} for index, text in enumerate(messages)],
    )


def test_the_task_is_the_services_own_definition_on_the_services_own_network(logs: Any) -> None:
    ecs = FakeEcs()
    rid.run_once(ecs, logs, "dev", ["migrate"])
    assert ecs.request("describe_services") == {
        "cluster": "marketing-ai-dev",
        "services": ["marketing-ai-dev-api"],
    }
    run = ecs.request("run_task")
    assert run["cluster"] == "marketing-ai-dev"
    assert run["taskDefinition"] == TASK_DEFINITION
    assert run["networkConfiguration"] == NETWORK
    assert run["launchType"] == "FARGATE"
    assert run["count"] == 1


def test_only_the_command_is_overridden_never_the_environment(logs: Any) -> None:
    """An override's environment is readable through DescribeTasks and recorded in CloudTrail."""
    ecs = FakeEcs()
    rid.run_once(ecs, logs, "dev", ["python", "-m", "scripts.aws_bootstrap", "--no-migrate"])
    overrides = ecs.request("run_task")["overrides"]
    assert overrides == {
        "containerOverrides": [
            {"name": "api", "command": ["python", "-m", "scripts.aws_bootstrap", "--no-migrate"]}
        ]
    }


def test_the_exit_code_and_the_log_come_back(logs: Any) -> None:
    _write_log(logs, '{"level": "INFO", "message": "Running upgrade  -> 0001"}', "done")
    out, err = io.StringIO(), io.StringIO()
    code = rid.main(["--env", "dev", "--", "migrate"], clients=(FakeEcs(exit_code=3), logs), out=out, err=err)
    assert code == 3
    assert out.getvalue().splitlines() == ['{"level": "INFO", "message": "Running upgrade  -> 0001"}', "done"]
    assert "exit code 3" in err.getvalue()


def test_a_task_that_never_wrote_a_line_is_not_a_crash(logs: Any) -> None:
    """A container that never started has no stream; the stop reason is the report."""
    outcome = rid.run_once(FakeEcs(exit_code=None), logs, "dev", ["migrate"])
    assert outcome.log_lines == ()
    assert outcome.exit_code is None
    assert outcome.process_exit_code == rid.EXIT_TASK_DID_NOT_RUN


def test_no_service_names_the_stack_to_deploy(logs: Any) -> None:
    err = io.StringIO()
    code = rid.main(["--env", "dev", "--", "migrate"], clients=(FakeEcs(services=[]), logs), err=err)
    assert code == rid.EXIT_TASK_DID_NOT_RUN
    assert "marketing-ai-dev-compute" in err.getvalue()
    assert "SERVICE_NOT_FOUND" in err.getvalue()


def test_run_task_failures_are_reported(logs: Any) -> None:
    ecs = FakeEcs(run_response={"tasks": [], "failures": [{"reason": "RESOURCE:ENI"}]})
    with pytest.raises(rid.RunError) as caught:
        rid.run_once(ecs, logs, "dev", ["migrate"])
    assert caught.value.code == "TASK_NOT_STARTED"
    assert "RESOURCE:ENI" in caught.value.message


def test_a_task_still_running_at_the_ceiling_says_how_to_stop_it(logs: Any) -> None:
    ecs = FakeEcs(times_out=True)
    with pytest.raises(rid.RunError) as caught:
        rid.run_once(ecs, logs, "dev", ["migrate"], timeout_minutes=1)
    assert caught.value.code == "TASK_STILL_RUNNING"
    assert f"aws ecs stop-task --cluster marketing-ai-dev --task {TASK_ARN}" in caught.value.message
    assert ecs.waiter.calls[0]["WaiterConfig"] == {"Delay": rid.WAIT_DELAY_SECONDS, "MaxAttempts": 6}


def test_a_command_is_required() -> None:
    err = io.StringIO()
    assert rid.main(["--env", "dev"], clients=(FakeEcs(), None), err=err) == 2
    assert "after `--`" in err.getvalue()


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["--env", "dev", "--", "migrate"], ["migrate"]),
        (["--env", "dev", "migrate"], ["migrate"]),
        (["--env", "dev", "--", "python", "-m", "x", "--flag"], ["python", "-m", "x", "--flag"]),
    ],
)
def test_the_command_is_everything_after_the_separator(
    logs: Any, argv: list[str], command: list[str]
) -> None:
    ecs = FakeEcs()
    rid.main(argv, clients=(ecs, logs), out=io.StringIO(), err=io.StringIO())
    assert ecs.request("run_task")["overrides"]["containerOverrides"][0]["command"] == command


def test_the_names_match_the_infrastructure(repo_root: Path) -> None:
    """The constants mirror `infra/`, read as text because `infra` needs aws-cdk-lib to import."""
    naming = (repo_root / "infra" / "naming.py").read_text(encoding="utf-8")
    compute = (repo_root / "infra" / "compute.py").read_text(encoding="utf-8")
    assert f'PRODUCT: Final[str] = "{rid.PRODUCT}"' in naming
    assert 'return f"/{PRODUCT}/{env_name}/api"' in naming
    assert f'stream_prefix="{rid.LOG_STREAM_PREFIX}"' in compute
    assert f'add_container(\n            "{rid.CONTAINER_NAME}",' in compute
    assert 'service_name=f"{PRODUCT}-{context.env_name}-api"' in compute
    assert 'cluster_name=f"{PRODUCT}-{context.env_name}"' in compute
