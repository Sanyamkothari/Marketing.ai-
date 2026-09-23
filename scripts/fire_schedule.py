"""Fire one schedule: what an EventBridge Scheduler target runs (Phase 4b M49).

    python -m scripts.fire_schedule --schedule-id sch_0123abcd4567 \\
        --scheduled-time 2026-10-01T20:30:00Z       # the slot EventBridge is firing
    python -m scripts.fire_schedule --schedule-id sch_0123abcd4567   # a manual firing
    python -m scripts.fire_schedule --sweep                           # settle and record missed slots only
    python -m scripts.fire_schedule --sweep --export-audit            # the hourly housekeeping firing

**Why a CLI in a task, not an HTTP call (DEC-765).** EventBridge Scheduler starts an ECS `RunTask` of
the product's own image, whose job container runs this module (`infra/operations.py`,
`FIRE_SCHEDULE_COMMAND`). Calling the API instead would need an EventBridge API destination - a
stored credential for a service account and an endpoint the scheduler can reach - to do what this
task does with the API's own task role and no credential at all. The work is the same
`engine.scheduling.firing.ScheduleFirer` the local scheduler and the API's "fire now" use, so a
schedule fired here and one fired on a laptop go through one code path.

What it does, in order:

1. fire the schedule for `--scheduled-time` (EventBridge substitutes `<aws.scheduler.scheduled-time>`).
   The slot is claimed first, so a retried invocation of the same slot does nothing (DEC-763);
2. with the thread job runner, **wait** for the run it started - the task *is* the job's process, and
   exiting would kill it - then settle it (`succeeded`/`failed`, alert, erasure flags). With the
   SageMaker runner the job runs elsewhere and the next sweep settles it;
3. sweep: settle every other finished firing and record missed slots of every schedule, running one
   catch-up each (DEC-764), so a deployment where one target keeps failing still hears about it;
4. with `--export-audit`, write the audit log's next window to the Object Lock bucket when the last
   scheduled export is a day old (`engine.audit.export.export_due`, DEC-726). The hourly
   *housekeeping* EventBridge schedule the API creates at startup runs `--sweep --export-audit`, so
   missed slots are found and the audit trail is copied even when no schedule of anyone's fires.

It acts as `SYSTEM_SCHEDULER` and writes one audit event per firing to the platform database.
Output is one JSON line of ids and codes - never a data value.

Exit codes: 0 fired, running, skipped or swept; 1 the firing failed; 2 settings or arguments could
not be used; 3 no such schedule.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from engine.registry import to_utc
from engine.scheduling.firing import EVENTBRIDGE_GRACE, FiringServices, ScheduleFirer
from engine.scheduling.scheduler import SCHEDULED_TIME_PLACEHOLDER
from engine.scheduling.schedules import FiringStatus, ScheduleError, ScheduleFiring, default_grace
from engine.settings import Settings, SettingsError, load_settings
from engine.utils.logging import configure_logging, get_logger

if TYPE_CHECKING:
    from engine.audit.events import AuditLog
    from engine.clients import ClientStore
    from engine.jobs import JobRunner

__all__ = ["main"]

_LOGGER = get_logger("scripts.fire_schedule")

COMMAND: Final[str] = "python -m scripts.fire_schedule"
EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1
EXIT_CONFIG: Final[int] = 2
EXIT_NOT_FOUND: Final[int] = 3

CLIENTS_DB_FILENAME: Final[str] = "clients.db"

# EVENTBRIDGE_GRACE (engine.scheduling.firing): how late a slot may be before the sweep - or a later
# slot's delivery - calls it missed. A RunTask takes a minute or two to start, and EventBridge retries
# for an hour (`scheduler.DEFAULT_MAX_EVENT_AGE_SECONDS`).

WAIT_TIMEOUT_SECONDS: Final[float] = 6 * 3600.0
"""The longest this task waits for its own run: a training run's `max_runtime` is bounded well below."""

ServicesFactory = Callable[[Settings], tuple[FiringServices, "JobRunner"]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=COMMAND, description="Fire one schedule, then settle and sweep.")
    parser.add_argument("--schedule-id", default=None, help="The schedule to fire.")
    parser.add_argument(
        "--scheduled-time",
        default=None,
        help="ISO time of the slot being fired (EventBridge's scheduled time). Omit for a manual firing.",
    )
    parser.add_argument("--sweep", action="store_true", help="Only settle firings and record missed slots.")
    parser.add_argument("--no-wait", action="store_true", help="Do not wait for a thread-run job to finish.")
    parser.add_argument(
        "--export-audit",
        action="store_true",
        help="Also export the audit log when a scheduled export is due.",
    )
    parser.add_argument("--config-dir", default=None, help="Configuration root (default: the deployment's).")
    return parser


def parse_slot(text: str | None) -> datetime | None:
    """The slot time, or `None` for a manual firing. An unsubstituted placeholder is treated as none."""
    if text is None or not text.strip():
        return None
    if text.strip() == SCHEDULED_TIME_PLACEHOLDER:
        _LOGGER.warning("fire_schedule.slot reason=placeholder_not_substituted")
        return None
    return to_utc(datetime.fromisoformat(text.strip().replace("Z", "+00:00")))


def main(argv: Sequence[str] | None = None, *, services_factory: ServicesFactory | None = None) -> int:
    """Fire, wait, settle, sweep. `services_factory` lets a test hand in its own services."""
    args = build_parser().parse_args(argv)
    configure_logging()
    if not args.sweep and not args.schedule_id:
        print("--schedule-id is required unless --sweep is given", file=sys.stderr)
        return EXIT_CONFIG
    try:
        settings = load_settings()
        slot = parse_slot(args.scheduled_time)
    except SettingsError as exc:
        print(exc.message, file=sys.stderr)
        return EXIT_CONFIG
    except ValueError:
        print("--scheduled-time is not an ISO date-time", file=sys.stderr)
        return EXIT_CONFIG
    if args.config_dir:
        settings = settings.model_copy(update={"config_dir": Path(args.config_dir)})
    services, jobs = (services_factory or build_services)(settings)
    firer = ScheduleFirer(services)
    firing: ScheduleFiring | None = None
    grace = max(EVENTBRIDGE_GRACE, default_grace(settings.scheduler_tick_seconds))
    try:
        if not args.sweep:
            try:
                firing = firer.fire_scheduled(str(args.schedule_id), scheduled_for=slot, grace=grace)
            except ScheduleError as exc:
                print(exc.message, file=sys.stderr)
                return EXIT_NOT_FOUND
            if firing is not None and firing.status is FiringStatus.RUNNING and not args.no_wait:
                firing = _wait_and_settle(firer, jobs, firing)
        firer.settle()
        swept = firer.sweep_missed(now=services.clock(), grace=grace)
        exported, export_failed = _export_audit(settings, services) if args.export_audit else (None, False)
    finally:
        jobs.shutdown(wait=True)
    print(json.dumps({**_summary(firing, swept), "audit_export": exported}, sort_keys=True))
    failed = firing is not None and firing.status is FiringStatus.FAILED
    return EXIT_FAILED if failed or export_failed else EXIT_OK


def _export_audit(settings: Settings, services: FiringServices) -> tuple[str | None, bool]:
    """`(key written or None, whether it failed)` for the scheduled audit export (DEC-726)."""
    from engine.audit.export import export_due, export_sink_for

    if services.audit_log is None:
        return None, False
    data_dir = settings.data_dir if settings.storage_backend == "local" else None
    try:
        result = export_due(services.audit_log, export_sink_for(settings, data_dir), now=services.clock())
    except Exception as exc:  # recorded as a failed audit event by export_due; the exit status says so
        _LOGGER.error("fire_schedule.audit_export failed error=%s", type(exc).__name__)
        return None, True
    return (None if result is None else result.key), False


def _wait_and_settle(firer: ScheduleFirer, jobs: JobRunner, firing: ScheduleFiring) -> ScheduleFiring:
    """Wait for this process's own job (thread runner only), then settle the firing from its run."""
    from engine.jobs import ThreadJobRunner

    if not isinstance(jobs, ThreadJobRunner) or firing.run_id is None:
        return firing
    try:
        jobs.wait(firing.run_id, timeout=WAIT_TIMEOUT_SECONDS)
    except TimeoutError:
        _LOGGER.warning("fire_schedule.wait timed_out firing_id=%s", firing.firing_id)
        return firing
    for settled in firer.settle():
        if settled.firing_id == firing.firing_id:
            return settled
    return firer.services.store.get_firing(firing.firing_id)


def _summary(firing: ScheduleFiring | None, swept: Sequence[ScheduleFiring]) -> dict[str, object]:
    fired = None
    if firing is not None:
        fired = {
            "firing_id": firing.firing_id,
            "schedule_id": firing.schedule_id,
            "status": firing.status.value,
            "trigger": firing.trigger.value,
            "run_id": firing.run_id,
            "dataset_id": firing.dataset_id,
            "result_code": firing.result_code,
            "error_code": firing.error_code,
        }
    return {
        "fired": fired,
        "swept": [{"firing_id": item.firing_id, "status": item.status.value} for item in swept],
    }


# ---------------------------------------------------------------------------
# Building the deployment's services
# ---------------------------------------------------------------------------
def build_services(settings: Settings) -> tuple[FiringServices, JobRunner]:
    """The services a firing needs, built exactly as the API builds its own (api/deps.py)."""
    from engine.config import config_root
    from engine.platform_db import platform_engine
    from engine.scheduling.alerts import build_alert_sink
    from engine.scheduling.retraining import privacy_retrain_flags
    from engine.scheduling.schedules import SqlScheduleStore
    from engine.settings import build_services as build_storage_and_registry

    storage, registry = build_storage_and_registry(settings)
    engine = platform_engine(settings)
    jobs = _jobs(settings, storage)
    services = FiringServices(
        store=SqlScheduleStore(engine),
        storage=storage,
        registry=registry,
        jobs=jobs,
        config_root=config_root(settings.config_dir),
        alerts=build_alert_sink(settings, engine=engine),
        audit_log=_audit_log(engine),
        client_store=_client_store(settings.data_dir),
        retrain_flags=privacy_retrain_flags(engine),
        job_client_tag=settings.client_id,
    )
    return services, jobs


def _jobs(settings: Settings, storage: object) -> JobRunner:
    """`ThreadJobRunner`, or the SageMaker runner on a deployment that trains there (DEC-306 imports)."""
    if settings.job_backend == "sagemaker":
        from engine.aws.sagemaker_jobs import SageMakerJobConfig, SageMakerJobRunner
        from engine.storage import Storage

        if not isinstance(storage, Storage):
            raise TypeError("the SageMaker runner needs the deployment's Storage")
        return SageMakerJobRunner(
            client=None, config=SageMakerJobConfig.from_settings(settings), storage=storage
        )
    from engine.jobs import ThreadJobRunner

    return ThreadJobRunner(max_workers=settings.job_max_workers)


def _audit_log(engine: object) -> AuditLog:
    """The platform database's audit log (M47), imported only when a real firing needs it."""
    from sqlalchemy.engine import Engine

    from engine.audit.store import SqlAuditLog

    if not isinstance(engine, Engine):
        raise TypeError("the audit log needs a SQLAlchemy engine")
    return SqlAuditLog(engine)


def _client_store(data_dir: Path) -> ClientStore | None:
    """The onboarding client store beside the artefacts, when this deployment has one."""
    path = data_dir / CLIENTS_DB_FILENAME
    if not path.is_file():
        return None
    from engine.clients import LocalClientStore

    return LocalClientStore(path)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
