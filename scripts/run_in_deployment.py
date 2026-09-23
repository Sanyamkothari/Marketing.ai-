"""Run one command inside a deployment, as the deployment: a one-off ECS task, then its exit code.

The first walk of `docs/AWS_DEPLOYMENT.md` (M50) found that three of its commands could not do what
the guide said from where the guide ran them:

* `make aws-bootstrap` probes permissions **as whoever runs it**. From a laptop or a CI runner that
  is the operator's own - usually administrator - credentials, so a storage probe that passes says
  nothing about the task role, which is the only identity that matters.
* The database is in **isolated subnets** with a security group that admits the service and the
  jobs and nobody else. `make migrate`, the bootstrap's database probe and
  `scripts/create_user.py` cannot reach it from outside the VPC at all.
* The image's own `migrate` entrypoint is the right way to apply the schema, and the guide said so,
  but gave no command that starts it.

All three have the same answer: start the **service's own task definition** - the same image
digest, the same task role, the same settings read from the same Parameter Store path - on the
**service's own subnets and security groups**, with the container's command overridden, wait for
it to stop, print what it logged, and exit with its exit code. That is what this module does, and
it is the whole of what it does. It creates nothing that outlives the task, and it reads the
network configuration from the running service rather than from anybody's notes, so it cannot
drift from what `cdk deploy` built.

    python -m scripts.run_in_deployment --env dev -- migrate
    python -m scripts.run_in_deployment --env dev -- python -m scripts.aws_bootstrap --no-migrate

What it deliberately does not do:

* **It passes no environment variables.** A container override's environment is visible to anyone
  who may `DescribeTasks`, and it is recorded in CloudTrail with the `RunTask` request. Anything a
  command needs comes from the deployment's settings, like everything else the image does. That is
  also why the first Admin's password is the one case the guide handles separately.
* **It does not retry.** A one-off task that failed has failed; its log is printed either way.
* **It never prints a log line's content selectively.** The application's logs carry no data values
  on any path (plan section 13.7), so the task's stream is echoed as it is.

`boto3` is imported lazily and every AWS call goes through one client object passed in, so the
tests drive the whole sequence with a fake and never reach AWS.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, TextIO

__all__ = [
    "COMMAND",
    "CONTAINER_NAME",
    "DEFAULT_REGION",
    "RunError",
    "TaskOutcome",
    "api_log_group",
    "cluster_name",
    "log_stream_name",
    "main",
    "run_once",
    "service_name",
]

COMMAND: Final[str] = "python -m scripts.run_in_deployment"

PRODUCT: Final[str] = "marketing-ai"
"""Mirrors `infra.naming.PRODUCT`. Not imported: `infra/` needs aws-cdk-lib, which `.venv` lacks (DEC-364)."""

CONTAINER_NAME: Final[str] = "api"
"""The container in the API task definition (`infra/compute.py` adds it as `"api"`)."""

LOG_STREAM_PREFIX: Final[str] = "api"
"""The `awslogs` stream prefix `infra/compute.py` gives that container."""

DEFAULT_REGION: Final[str] = "ap-south-1"

STARTED_BY: Final[str] = "run_in_deployment"
"""What `DescribeTasks` shows as `startedBy`, so a one-off task is recognisable in the console."""

WAIT_DELAY_SECONDS: Final[int] = 10
DEFAULT_TIMEOUT_MINUTES: Final[int] = 30
"""How long to wait for the task to stop. A migration or a bootstrap takes minutes; this is a ceiling."""

EXIT_TASK_DID_NOT_RUN: Final[int] = 125
"""The task never produced an exit code: it could not start, or it was stopped from outside.

Chosen from the range `docker run` uses for "the container runtime failed" so it cannot be
mistaken for an exit code of the command itself."""


class RunError(Exception):
    """The task could not be started, or could not be found afterwards. `message` names no value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """What happened to the one-off task."""

    task_arn: str
    exit_code: int | None
    stopped_reason: str
    log_lines: tuple[str, ...]

    @property
    def process_exit_code(self) -> int:
        """The code this process exits with: the command's own, or `EXIT_TASK_DID_NOT_RUN`."""
        return EXIT_TASK_DID_NOT_RUN if self.exit_code is None else self.exit_code


def cluster_name(env: str) -> str:
    """Mirrors `infra.naming.cluster_name`."""
    return f"{PRODUCT}-{env}"


def service_name(env: str) -> str:
    """The API service `infra/compute.py` creates."""
    return f"{PRODUCT}-{env}-api"


def api_log_group(env: str) -> str:
    """Mirrors `infra.naming.api_log_group_name`: where the API container - and so this task - logs."""
    return f"/{PRODUCT}/{env}/api"


def log_stream_name(task_arn: str) -> str:
    """`<prefix>/<container>/<task id>`, the stream name the `awslogs` driver gives a Fargate task."""
    return f"{LOG_STREAM_PREFIX}/{CONTAINER_NAME}/{task_arn.rsplit('/', 1)[-1]}"


def _service(ecs: Any, env: str) -> dict[str, Any]:
    """The running API service, or `RunError` naming the stack that should have created it."""
    response = ecs.describe_services(cluster=cluster_name(env), services=[service_name(env)])
    services = [item for item in response.get("services", []) if item.get("status") == "ACTIVE"]
    if not services:
        raise RunError(
            "SERVICE_NOT_FOUND",
            f"no active service {service_name(env)} in cluster {cluster_name(env)}; deploy the "
            f"marketing-ai-{env}-compute stack first",
        )
    service: dict[str, Any] = services[0]
    return service


def start_task(ecs: Any, env: str, command: Sequence[str]) -> str:
    """Start the service's task definition with `command`, on the service's network. Returns the ARN."""
    service = _service(ecs, env)
    network = service.get("networkConfiguration")
    if not network:
        raise RunError("SERVICE_HAS_NO_NETWORK", f"{service_name(env)} reports no network configuration")
    response = ecs.run_task(
        cluster=cluster_name(env),
        taskDefinition=service["taskDefinition"],
        launchType="FARGATE",
        count=1,
        startedBy=STARTED_BY,
        networkConfiguration=network,
        overrides={"containerOverrides": [{"name": CONTAINER_NAME, "command": list(command)}]},
    )
    failures = response.get("failures") or []
    tasks = response.get("tasks") or []
    if failures or not tasks:
        reasons = ", ".join(sorted({str(item.get("reason", "unknown")) for item in failures})) or "no task"
        raise RunError("TASK_NOT_STARTED", f"RunTask started nothing: {reasons}")
    arn: str = tasks[0]["taskArn"]
    return arn


def wait_for_stop(ecs: Any, env: str, task_arn: str, *, timeout_minutes: int) -> None:
    """Block until the task has stopped, or raise `RunError` when the ceiling is reached."""
    from botocore.exceptions import WaiterError

    attempts = max(1, (timeout_minutes * 60) // WAIT_DELAY_SECONDS)
    try:
        ecs.get_waiter("tasks_stopped").wait(
            cluster=cluster_name(env),
            tasks=[task_arn],
            WaiterConfig={"Delay": WAIT_DELAY_SECONDS, "MaxAttempts": attempts},
        )
    except WaiterError as exc:
        raise RunError(
            "TASK_STILL_RUNNING",
            f"the task had not stopped after {timeout_minutes} minutes; it is still running - "
            f"stop it with `aws ecs stop-task --cluster {cluster_name(env)} --task {task_arn}`",
        ) from exc


def describe_outcome(ecs: Any, logs: Any, env: str, task_arn: str) -> TaskOutcome:
    """The container's exit code, the task's stop reason and the task's log lines."""
    response = ecs.describe_tasks(cluster=cluster_name(env), tasks=[task_arn])
    tasks = response.get("tasks") or []
    if not tasks:
        raise RunError("TASK_NOT_FOUND", f"DescribeTasks no longer knows {task_arn}")
    task = tasks[0]
    container: dict[str, Any] = next(
        (item for item in task.get("containers", []) if item.get("name") == CONTAINER_NAME), {}
    )
    exit_code = container.get("exitCode")
    return TaskOutcome(
        task_arn=task_arn,
        exit_code=int(exit_code) if exit_code is not None else None,
        stopped_reason=str(task.get("stoppedReason", "")),
        log_lines=read_log_lines(logs, env, task_arn),
    )


def read_log_lines(logs: Any, env: str, task_arn: str) -> tuple[str, ...]:
    """Every line the task wrote, oldest first; empty when the stream was never created.

    A task that failed before its container started - an image it could not pull, a subnet with no
    route to ECR - has no stream at all. That is reported by the stop reason, not as an error here.
    """
    from botocore.exceptions import ClientError

    lines: list[str] = []
    token: str | None = None
    while True:
        request: dict[str, Any] = {
            "logGroupName": api_log_group(env),
            "logStreamName": log_stream_name(task_arn),
            "startFromHead": True,
        }
        if token is not None:
            request["nextToken"] = token
        try:
            response = logs.get_log_events(**request)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return tuple(lines)
            raise
        events = response.get("events") or []
        lines.extend(str(event.get("message", "")) for event in events)
        next_token = response.get("nextForwardToken")
        if not events or next_token == token:
            return tuple(lines)
        token = next_token


def run_once(
    ecs: Any,
    logs: Any,
    env: str,
    command: Sequence[str],
    *,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
    announce: Callable[[str], None] | None = None,
) -> TaskOutcome:
    """Start, wait, describe: the whole sequence against the clients given."""
    say = announce or (lambda _line: None)
    task_arn = start_task(ecs, env, command)
    say(f"started {task_arn} in {cluster_name(env)}; waiting for it to stop")
    wait_for_stop(ecs, env, task_arn, timeout_minutes=timeout_minutes)
    return describe_outcome(ecs, logs, env, task_arn)


def build_parser() -> argparse.ArgumentParser:
    """`--env`, `--region`, `--timeout-minutes`, then the command after `--`."""
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description=(
            "Run one command as a one-off ECS task with the deployment's own task definition, role "
            "and network, print its log, and exit with its exit code."
        ),
    )
    parser.add_argument("--env", required=True, help="deployment name, e.g. dev")
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"AWS region (default {DEFAULT_REGION})")
    parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=DEFAULT_TIMEOUT_MINUTES,
        help=f"how long to wait for the task to stop (default {DEFAULT_TIMEOUT_MINUTES})",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="what to run, after `--`: `migrate`, or e.g. `python -m scripts.aws_bootstrap`",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    clients: tuple[Any, Any] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Parse, run, report; returns the process exit code."""
    stdout = out or sys.stdout
    stderr = err or sys.stderr
    args = build_parser().parse_args(argv)
    command: list[str] = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print(f"{COMMAND}: give the command to run after `--`, e.g. `-- migrate`", file=stderr)
        return 2
    if clients is None:
        import boto3

        clients = (
            boto3.client("ecs", region_name=args.region),
            boto3.client("logs", region_name=args.region),
        )
    ecs, logs = clients
    try:
        outcome = run_once(
            ecs,
            logs,
            args.env,
            command,
            timeout_minutes=args.timeout_minutes,
            announce=lambda line: print(f"{COMMAND}: {line}", file=stderr),
        )
    except RunError as exc:
        print(f"{COMMAND}: {exc.message} ({exc.code})", file=stderr)
        return EXIT_TASK_DID_NOT_RUN
    for line in outcome.log_lines:
        print(line, file=stdout)
    code = outcome.process_exit_code
    detail = f"exit code {outcome.exit_code}" if outcome.exit_code is not None else "no exit code"
    print(f"{COMMAND}: task stopped ({outcome.stopped_reason or 'no reason given'}), {detail}", file=stderr)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
