"""What makes a schedule fire: nothing, an in-process thread, or EventBridge Scheduler (M49).

`Settings.scheduler_backend` picks one of three, and all three sit behind one protocol so the API's
schedule routes never ask which is running:

* **`none`** (the default, and Phase 1's behaviour) - `NullScheduler`. Schedules are stored and can
  be fired by hand; nothing fires them on its own.
* **`local`** - `LocalScheduler`, a daemon thread in the API process that wakes every
  `scheduler_tick_seconds`, settles firings whose runs have ended, and runs whatever is due. The
  schedule table is its only state: `sync` and `remove` have nothing to push anywhere, and a restart
  loses nothing because the next tick reads the table again. The clock is injected, and `tick()` is
  public, so tests drive it one deterministic step at a time without the thread (DEC-762).
* **`eventbridge`** - `EventBridgeScheduler`. `sync` creates or updates one EventBridge Scheduler
  schedule per `Schedule` in `scheduler_group_name` (the IAM boundary infra/operations.py draws);
  `remove` deletes it. The API never polls: AWS fires, and the target does the work.

**Why the target is an ECS task running `scripts/fire_schedule.py`, not an HTTPS call to the API
(DEC-765).** EventBridge Scheduler's universal target can call AWS APIs, not arbitrary HTTPS
endpoints; reaching our API would take an EventBridge *API destination* - a connection holding
credentials for a service account, a rule, and a public (or VPC-linked) endpoint for the scheduler
to call - which is a second, long-lived credential to our own product and an unauthenticated-looking
entry point to defend. `ecs:RunTask` of the product's own image, in the product's own cluster, with
the API's own task role, needs none of that: the scheduler role can start one task family and pass
two roles (tests/infra/test_phase4b_scheduler.py pins exactly that), and the task runs the same
`engine.scheduling.firing.fire` the local scheduler and "fire now" run. A firing is the API's work
done at a time nobody clicked, so it holds the API's rights and not a copy of them.

The schedule's `Input` is the RunTask *overrides* document (EventBridge passes an ECS target's input
as `overrides`, as EventBridge rules do): it replaces the job container's command with
`python -m scripts.fire_schedule --schedule-id <id> --scheduled-time <aws.scheduler.scheduled-time>`.
The context attribute is substituted by EventBridge at invocation, so the CLI knows which slot it is
firing and a retried invocation of the same slot claims nothing twice (DEC-763). For a non-ECS target
ARN (a Lambda, a queue) the input is the plain JSON `{"schedule_id": ..., "scheduled_time": ...}`.

The ECS parameters come from settings the deployment already writes: the cluster is
`scheduler_target_arn`; the task-definition *family* is the cluster's name plus `-job`
(`infra.naming.job_task_family` - a family, never a revision, so a schedule survives deployments);
the network is the private subnets and security group SageMaker jobs already use
(`sagemaker_subnet_ids`, `sagemaker_security_group_ids`), which reach the database and S3 exactly as
a job must. Each can be overridden when the scheduler is built.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol, runtime_checkable

from engine.scheduling.schedules import Schedule, ScheduleFiring, ScheduleStore, default_grace
from engine.settings import Settings, SettingsError
from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

__all__ = [
    "FIRE_SCHEDULE_COMMAND",
    "JOB_CONTAINER_NAME",
    "SCHEDULED_TIME_PLACEHOLDER",
    "Clock",
    "EcsTarget",
    "EventBridgeScheduler",
    "Firer",
    "LocalScheduler",
    "NullScheduler",
    "Scheduler",
    "build_scheduler",
]

_LOGGER = get_logger(__name__)

Clock = Callable[[], datetime]

FIRE_SCHEDULE_COMMAND: Final[tuple[str, ...]] = ("python", "-m", "scripts.fire_schedule")
"""The job container's command. Must equal `infra.operations.FIRE_SCHEDULE_COMMAND` (a test pins it);
spelled here because the engine never imports the CDK app."""

JOB_CONTAINER_NAME: Final[str] = "job"
"""The container a RunTask override names; `infra.naming.JOB_CONTAINER_NAME`."""

SCHEDULED_TIME_PLACEHOLDER: Final[str] = "<aws.scheduler.scheduled-time>"
"""EventBridge Scheduler's context attribute for the slot being fired, substituted at invocation."""

_ECS_CLUSTER_ARN: Final[re.Pattern[str]] = re.compile(
    r"^arn:(?P<partition>[\w-]+):ecs:(?P<region>[\w-]+):(?P<account>\d+):cluster/(?P<name>[\w-]+)$"
)

HOUSEKEEPING_SCHEDULE_NAME: Final[str] = "marketing-ai-housekeeping"
"""The one EventBridge schedule that is not a `Schedule` row: an hourly sweep and audit export."""

HOUSEKEEPING_EXPRESSION: Final[str] = "rate(1 hour)"

HOUSEKEEPING_ARGUMENTS: Final[tuple[str, ...]] = ("--sweep", "--export-audit")
"""What the housekeeping firing runs (DEC-775, DEC-726): find missed slots even when no schedule's own
target runs, and copy the audit log to Object Lock without anyone asking."""

DEFAULT_RETRY_ATTEMPTS: Final[int] = 2
DEFAULT_MAX_EVENT_AGE_SECONDS: Final[int] = 3600
"""A slot EventBridge could not deliver within an hour is left to the missed-run sweep (DEC-764),
which records it and runs one catch-up, rather than retried for EventBridge's default 24 hours."""


@runtime_checkable
class Scheduler(Protocol):
    """Keeps whatever fires schedules in line with the schedule table."""

    backend: str

    def sync(self, schedule: Schedule) -> None: ...

    def remove(self, schedule_id: str) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


@runtime_checkable
class Firer(Protocol):
    """The work a tick does: settle ended firings, run whatever is due. `firing.ScheduleFirer` is it."""

    def settle(self) -> tuple[ScheduleFiring, ...]: ...

    def run_due(
        self, schedule: Schedule, *, now: datetime, grace: timedelta
    ) -> tuple[ScheduleFiring, ...]: ...


class NullScheduler:
    """`scheduler_backend=none`: schedules are kept, nothing fires them."""

    backend = "none"

    def sync(self, schedule: Schedule) -> None:
        """Nothing fires schedules here, so there is nothing to tell."""
        del schedule

    def remove(self, schedule_id: str) -> None:
        del schedule_id

    def start(self) -> None:
        """Nothing to start."""

    def stop(self) -> None:
        """Nothing to stop."""


class LocalScheduler:
    """`scheduler_backend=local`: one daemon thread, one tick every `tick_seconds` (DEC-762)."""

    backend = "local"

    def __init__(
        self,
        store: ScheduleStore,
        firer: Firer,
        *,
        tick_seconds: int = 60,
        clock: Clock = utc_now,
        grace: timedelta | None = None,
    ) -> None:
        self._store = store
        self._firer = firer
        self._tick_seconds = tick_seconds
        self._clock = clock
        self._grace = grace if grace is not None else default_grace(tick_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()
        self.housekeeping: Callable[[datetime], object] | None = None
        """Run once per tick after the firings, e.g. the scheduled audit export (DEC-726)."""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def sync(self, schedule: Schedule) -> None:
        """Nothing to push: the next tick reads the table."""
        del schedule

    def remove(self, schedule_id: str) -> None:
        """Nothing to remove: a deleted row is simply not read by the next tick."""
        del schedule_id

    def start(self) -> None:
        """Start the thread; a second call while it runs does nothing."""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="marketing-ai-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to finish its tick and exit, and wait for it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, float(self._tick_seconds)))
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self._tick_seconds)

    def tick(self) -> tuple[ScheduleFiring, ...]:
        """One pass: settle ended firings, then run every enabled schedule's due slots.

        A failure while handling one schedule is logged by class and does not stop the others; the
        lock keeps a manual `tick()` and the thread's from running the same pass at once.
        """
        with self._tick_lock:
            now = self._clock()
            produced: list[ScheduleFiring] = []
            try:
                produced.extend(self._firer.settle())
            except Exception as exc:  # one bad run record must not stop the scheduler
                log_failure(_LOGGER, "scheduler.settle", exc)
            for schedule in self._store.list():
                if not schedule.enabled:
                    continue
                try:
                    produced.extend(self._firer.run_due(schedule, now=now, grace=self._grace))
                except Exception as exc:  # isolate one schedule's failure from the rest
                    log_failure(_LOGGER, f"scheduler.tick schedule_id={schedule.schedule_id}", exc)
            if self.housekeeping is not None:
                try:
                    self.housekeeping(now)
                except Exception as exc:  # the next tick tries again; export_due logs overdue at ERROR
                    log_failure(_LOGGER, "scheduler.housekeeping", exc)
            return tuple(produced)


@dataclass(frozen=True, slots=True)
class EcsTarget:
    """The ECS half of an EventBridge schedule's target: which task, in which network."""

    task_definition_arn: str
    subnets: tuple[str, ...]
    security_groups: tuple[str, ...]
    container_name: str = JOB_CONTAINER_NAME
    launch_type: str = "FARGATE"

    def parameters(self) -> dict[str, Any]:
        return {
            "TaskDefinitionArn": self.task_definition_arn,
            "LaunchType": self.launch_type,
            "TaskCount": 1,
            "NetworkConfiguration": {
                "awsvpcConfiguration": {
                    "Subnets": list(self.subnets),
                    "SecurityGroups": list(self.security_groups),
                    "AssignPublicIp": "DISABLED",
                }
            },
        }

    @classmethod
    def for_cluster(
        cls, cluster_arn: str, *, subnets: tuple[str, ...], security_groups: tuple[str, ...]
    ) -> EcsTarget | None:
        """The job task of the cluster `cluster_arn` names, or `None` when it names no ECS cluster."""
        match = _ECS_CLUSTER_ARN.fullmatch(cluster_arn)
        if match is None:
            return None
        family = f"{match['name']}-job"
        return cls(
            task_definition_arn=(
                f"arn:{match['partition']}:ecs:{match['region']}:{match['account']}:task-definition/{family}"
            ),
            subnets=subnets,
            security_groups=security_groups,
        )


class EventBridgeScheduler:
    """`scheduler_backend=eventbridge`: one EventBridge Scheduler schedule per `Schedule` (DEC-765)."""

    backend = "eventbridge"

    def __init__(
        self,
        *,
        group_name: str,
        target_arn: str,
        role_arn: str,
        region_name: str | None,
        ecs: EcsTarget | None = None,
        client: Any = None,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        max_event_age_seconds: int = DEFAULT_MAX_EVENT_AGE_SECONDS,
    ) -> None:
        self.group_name = group_name
        self.target_arn = target_arn
        self.role_arn = role_arn
        self.ecs = ecs
        self._region_name = region_name
        self._client = client
        self._retry = {
            "MaximumRetryAttempts": retry_attempts,
            "MaximumEventAgeInSeconds": max_event_age_seconds,
        }

    @classmethod
    def from_settings(cls, settings: Settings, *, client: Any = None) -> EventBridgeScheduler:
        """Built from the Phase 4b settings; an ECS cluster target also needs the job network."""
        if not settings.scheduler_target_arn or not settings.scheduler_role_arn:
            raise SettingsError(
                "SETTING_REQUIRED",
                "scheduler_backend=eventbridge needs scheduler_target_arn and scheduler_role_arn.",
                env_var="MARKETING_AI_SCHEDULER_TARGET_ARN",
            )
        ecs = EcsTarget.for_cluster(
            settings.scheduler_target_arn,
            subnets=tuple(settings.sagemaker_subnet_ids),
            security_groups=tuple(settings.sagemaker_security_group_ids),
        )
        if ecs is not None and not ecs.subnets:
            raise SettingsError(
                "SETTING_REQUIRED",
                "An ECS cluster as the schedule target needs the private subnets its task runs in.",
                env_var="MARKETING_AI_SAGEMAKER_SUBNET_IDS",
            )
        return cls(
            group_name=settings.scheduler_group_name,
            target_arn=settings.scheduler_target_arn,
            role_arn=settings.scheduler_role_arn,
            region_name=settings.aws_region,
            ecs=ecs,
            client=client,
        )

    def _scheduler(self) -> Any:
        if self._client is None:
            import boto3  # a deliberate local import: a laptop never loads boto3 (DEC-306)

            self._client = boto3.client("scheduler", region_name=self._region_name)
        return self._client

    def target_input(self, schedule_id: str) -> str:
        """The target's `Input`: RunTask overrides for an ECS target, plain JSON otherwise."""
        arguments = ["--schedule-id", schedule_id, "--scheduled-time", SCHEDULED_TIME_PLACEHOLDER]
        if self.ecs is not None:
            command = [*FIRE_SCHEDULE_COMMAND, *arguments]
            return json.dumps({"containerOverrides": [{"name": self.ecs.container_name, "command": command}]})
        return json.dumps({"schedule_id": schedule_id, "scheduled_time": SCHEDULED_TIME_PLACEHOLDER})

    def request(self, schedule: Schedule) -> dict[str, Any]:
        """The `CreateSchedule`/`UpdateSchedule` arguments for `schedule` - identical for both calls."""
        target: dict[str, Any] = {
            "Arn": self.target_arn,
            "RoleArn": self.role_arn,
            "Input": self.target_input(schedule.schedule_id),
            "RetryPolicy": dict(self._retry),
        }
        if self.ecs is not None:
            target["EcsParameters"] = self.ecs.parameters()
        return {
            "Name": schedule.schedule_id,
            "GroupName": self.group_name,
            "ScheduleExpression": schedule.expression.to_eventbridge(),
            "ScheduleExpressionTimezone": schedule.timezone,
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "State": "ENABLED" if schedule.enabled else "DISABLED",
            "Description": f"marketing-ai {schedule.kind.value} {schedule.use_case_id}"[:512],
            "Target": target,
        }

    def sync(self, schedule: Schedule) -> None:
        """Create the EventBridge schedule, or update it in place when it exists.

        `GetSchedule` first rather than create-and-catch-conflict because the API's role is granted
        Create, Get, Update and Delete in its group and nothing else - no List (infra/policies.py).
        """
        client = self._scheduler()
        request = self.request(schedule)
        try:
            client.get_schedule(Name=schedule.schedule_id, GroupName=self.group_name)
        except client.exceptions.ResourceNotFoundException:
            client.create_schedule(**request)
            _LOGGER.info("scheduler.eventbridge created schedule_id=%s", schedule.schedule_id)
            return
        client.update_schedule(**request)
        _LOGGER.info("scheduler.eventbridge updated schedule_id=%s", schedule.schedule_id)

    def housekeeping_request(self) -> dict[str, Any]:
        """The `CreateSchedule`/`UpdateSchedule` arguments of the hourly housekeeping schedule."""
        if self.ecs is not None:
            command = [*FIRE_SCHEDULE_COMMAND, *HOUSEKEEPING_ARGUMENTS]
            payload = json.dumps(
                {"containerOverrides": [{"name": self.ecs.container_name, "command": command}]}
            )
        else:
            payload = json.dumps({"housekeeping": True})
        target: dict[str, Any] = {
            "Arn": self.target_arn,
            "RoleArn": self.role_arn,
            "Input": payload,
            "RetryPolicy": dict(self._retry),
        }
        if self.ecs is not None:
            target["EcsParameters"] = self.ecs.parameters()
        return {
            "Name": HOUSEKEEPING_SCHEDULE_NAME,
            "GroupName": self.group_name,
            "ScheduleExpression": HOUSEKEEPING_EXPRESSION,
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "State": "ENABLED",
            "Description": "marketing-ai housekeeping: missed-slot sweep and scheduled audit export",
            "Target": target,
        }

    def ensure_housekeeping(self) -> None:
        """Create or update the housekeeping schedule; the API calls this at every startup."""
        client = self._scheduler()
        request = self.housekeeping_request()
        try:
            client.get_schedule(Name=HOUSEKEEPING_SCHEDULE_NAME, GroupName=self.group_name)
        except client.exceptions.ResourceNotFoundException:
            client.create_schedule(**request)
            _LOGGER.info("scheduler.eventbridge created housekeeping schedule")
            return
        client.update_schedule(**request)

    def remove(self, schedule_id: str) -> None:
        """Delete the EventBridge schedule; one that is already gone is not an error."""
        client = self._scheduler()
        try:
            client.delete_schedule(Name=schedule_id, GroupName=self.group_name)
        except client.exceptions.ResourceNotFoundException:
            return
        _LOGGER.info("scheduler.eventbridge deleted schedule_id=%s", schedule_id)

    def start(self) -> None:
        """Nothing to start: AWS fires."""

    def stop(self) -> None:
        """Nothing to stop."""


def build_scheduler(
    settings: Settings,
    *,
    store: ScheduleStore,
    firer: Firer | None = None,
    clock: Clock = utc_now,
    client: Any = None,
) -> Scheduler:
    """The scheduler `settings.scheduler_backend` names. `local` needs the `firer` its ticks call."""
    if settings.scheduler_backend == "eventbridge":
        return EventBridgeScheduler.from_settings(settings, client=client)
    if settings.scheduler_backend == "local":
        if firer is None:
            raise ValueError("scheduler_backend=local needs a firer: build one with firing.ScheduleFirer")
        return LocalScheduler(store, firer, tick_seconds=settings.scheduler_tick_seconds, clock=clock)
    return NullScheduler()


_NULL: type[Scheduler] = NullScheduler
_LOCAL: type[Scheduler] = LocalScheduler
_EVENTBRIDGE: type[Scheduler] = EventBridgeScheduler
"""The three must satisfy the protocol; mypy checks these lines so no test has to."""
