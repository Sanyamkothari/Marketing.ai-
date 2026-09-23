"""The schedule operations the API exposes: create, change, delete - each kept in step with the scheduler.

A route should translate a request into one of these calls and a `ScheduleError` into its envelope
(`ERROR_STATUS` says which HTTP status each code is), and nothing else; every rule about what a valid
schedule is lives here and in `Schedule`'s own validators, so the API, the CLI and a test cannot
disagree about it.

**The table is written first, then the scheduler is told (DEC-765).** A create whose EventBridge call
fails removes the row again and answers `SCHEDULER_SYNC_FAILED`, so the product never shows a schedule
AWS does not know about; an update whose push fails leaves the row as written and answers the same
code, because retrying the push is safe (`sync` is create-or-update) and the table is what every
firing reads. A delete removes the registration *first* and keeps the row when that fails, so
deleting again retries it instead of answering "not found" over an orphaned AWS schedule.

**Managed schedules belong to `monitoring.retraining` (DEC-767).** A person may pause and resume one
(`enabled`), which is an operational decision; changing its cadence or its data, or deleting it, is
refused with `SCHEDULE_MANAGED`, because the next sync would silently put it back - the setting is
the thing to change.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from engine.clients import ClientStore, ClientStoreError
from engine.config import ConfigError, resolve_config
from engine.scheduling.cron import DEFAULT_TIMEZONE, CronError, zone
from engine.scheduling.scheduler import Scheduler
from engine.scheduling.schedules import (
    CadencePreset,
    Schedule,
    ScheduleError,
    ScheduleKind,
    ScheduleParameters,
    ScheduleStore,
    cadence_cron,
    new_schedule_id,
)
from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

__all__ = [
    "ERROR_STATUS",
    "create_schedule",
    "delete_schedule",
    "update_schedule",
]

_LOGGER = get_logger(__name__)

ERROR_STATUS: Final[dict[str, int]] = {
    "SCHEDULE_NOT_FOUND": 404,
    "FIRING_NOT_FOUND": 404,
    "USE_CASE_NOT_FOUND": 404,
    "ONBOARDING_SPEC_NOT_FOUND": 404,
    "SCHEDULE_EXISTS": 409,
    "SCHEDULE_MANAGED": 409,
    "SCHEDULE_INVALID": 422,
    "CRON_INVALID": 422,
    "CRON_NOT_PORTABLE": 422,
    "CRON_NEVER_FIRES": 422,
    "TIMEZONE_UNKNOWN": 422,
    "ONBOARDING_SPEC_USE_CASE_MISMATCH": 422,
    "SCHEDULER_SYNC_FAILED": 502,
}
"""`ScheduleError.code` -> the HTTP status a route answers with. An unmapped code is a 500."""


def _cadence(cadence: str, timezone: str) -> tuple[str, CadencePreset | None]:
    try:
        zone(timezone)
        return cadence_cron(cadence)
    except CronError as exc:
        raise ScheduleError(exc.code, exc.message) from exc


def _validated(**fields: object) -> Schedule:
    try:
        return Schedule.model_validate(fields)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ScheduleError("SCHEDULE_INVALID", str(first.get("msg", "The schedule is not valid."))) from exc


def _check_use_case(use_case_id: str, config_root: Path) -> None:
    try:
        resolve_config(use_case_id, root=config_root)
    except ConfigError as exc:
        raise ScheduleError(
            "USE_CASE_NOT_FOUND", f"There is no use case {use_case_id!r} to schedule."
        ) from exc


def _check_spec(
    client_store: ClientStore | None,
    parameters: ScheduleParameters,
    *,
    client_id: str | None,
    use_case_id: str,
) -> None:
    """A named recipe must exist, be this client's, and build this use case. Skipped with no store."""
    if client_store is None or parameters.onboarding_spec_id is None:
        return
    spec_id = parameters.onboarding_spec_id
    try:
        spec = client_store.get_spec(spec_id)
    except ClientStoreError as exc:
        raise ScheduleError(
            "ONBOARDING_SPEC_NOT_FOUND", f"No onboarding recipe with id {spec_id!r}."
        ) from exc
    if client_id is not None and spec.client_id != client_id:
        raise ScheduleError("ONBOARDING_SPEC_NOT_FOUND", f"No onboarding recipe with id {spec_id!r}.")
    if spec.use_case != use_case_id:
        raise ScheduleError(
            "ONBOARDING_SPEC_USE_CASE_MISMATCH",
            f"Recipe {spec_id!r} builds data for {spec.use_case!r}, not {use_case_id!r}.",
        )


def _push(scheduler: Scheduler, schedule: Schedule) -> None:
    try:
        scheduler.sync(schedule)
    except Exception as exc:  # the scheduler's own failure, whatever AWS called it
        log_failure(_LOGGER, f"schedule.sync schedule_id={schedule.schedule_id}", exc)
        raise ScheduleError(
            "SCHEDULER_SYNC_FAILED",
            "The schedule could not be registered with the scheduler. Try again; if it keeps failing, "
            "check the scheduler settings of this deployment.",
        ) from exc


def create_schedule(
    store: ScheduleStore,
    scheduler: Scheduler,
    *,
    config_root: Path,
    client_id: str | None,
    use_case_id: str,
    kind: ScheduleKind,
    cadence: str,
    created_by: str,
    timezone: str = DEFAULT_TIMEZONE,
    parameters: ScheduleParameters | None = None,
    enabled: bool = True,
    client_store: ClientStore | None = None,
    now: datetime | None = None,
) -> Schedule:
    """Validate, store and register a new schedule. `cadence` is a preset name or a cron line."""
    moment = now or utc_now()
    chosen = parameters or ScheduleParameters()
    _check_use_case(use_case_id, config_root)
    cron, preset = _cadence(cadence, timezone)
    _check_spec(client_store, chosen, client_id=client_id, use_case_id=use_case_id)
    schedule = _validated(
        schedule_id=new_schedule_id(),
        client_id=client_id,
        use_case_id=use_case_id,
        kind=kind,
        cron=cron,
        timezone=timezone,
        preset=preset,
        enabled=enabled,
        parameters=chosen,
        created_by=created_by,
        created_at=moment,
        updated_at=moment,
    )
    schedule = schedule.model_copy(
        update={"next_due_at": schedule.next_slot_after(moment) if enabled else None}
    )
    store.create(schedule)
    try:
        _push(scheduler, schedule)
    except ScheduleError:
        store.delete(schedule.schedule_id)
        raise
    return schedule


def update_schedule(
    store: ScheduleStore,
    scheduler: Scheduler,
    schedule_id: str,
    *,
    cadence: str | None = None,
    timezone: str | None = None,
    enabled: bool | None = None,
    parameters: ScheduleParameters | None = None,
    client_store: ClientStore | None = None,
    now: datetime | None = None,
) -> Schedule:
    """Change a schedule. `next_due_at` is recomputed from now when the cadence changes or it is enabled."""
    moment = now or utc_now()
    current = store.get(schedule_id)
    if current.managed_by is not None and (cadence, timezone, parameters) != (None, None, None):
        raise ScheduleError(
            "SCHEDULE_MANAGED",
            f"This schedule follows the use case's {current.managed_by} setting; change that setting "
            "instead. It can still be paused and resumed.",
        )
    new_timezone = timezone or current.timezone
    if cadence is not None or timezone is not None:
        cron, preset = _cadence(cadence or current.cron, new_timezone)
        if cadence is None:
            preset = current.preset
    else:
        cron, preset = current.cron, current.preset
    chosen = parameters or current.parameters
    if parameters is not None:
        _check_spec(client_store, chosen, client_id=current.client_id, use_case_id=current.use_case_id)
    now_enabled = current.enabled if enabled is None else enabled
    fields = current.model_dump()
    fields.update(
        cron=cron,
        preset=preset,
        timezone=new_timezone,
        parameters=chosen,
        enabled=now_enabled,
        updated_at=moment,
    )
    changed = _validated(**fields)
    retimed = (
        cron != current.cron or new_timezone != current.timezone or (now_enabled and not current.enabled)
    )
    if not now_enabled:
        changed = changed.model_copy(update={"next_due_at": None})
    elif retimed or changed.next_due_at is None:
        changed = changed.model_copy(update={"next_due_at": changed.next_slot_after(moment)})
    store.save(changed)
    _push(scheduler, changed)
    return changed


def delete_schedule(store: ScheduleStore, scheduler: Scheduler, schedule_id: str) -> None:
    """Remove a schedule's registration, then the schedule. Its firing history is kept."""
    current = store.get(schedule_id)
    if current.managed_by is not None:
        raise ScheduleError(
            "SCHEDULE_MANAGED",
            f"This schedule follows the use case's {current.managed_by} setting; set it to 'manual' to "
            "remove the schedule.",
        )
    try:
        scheduler.remove(schedule_id)
    except Exception as exc:  # keep the row, so deleting again retries the removal
        log_failure(_LOGGER, f"schedule.remove schedule_id={schedule_id}", exc)
        raise ScheduleError(
            "SCHEDULER_SYNC_FAILED",
            "The schedule could not be removed from the scheduler, so it was kept. Delete it again to retry.",
        ) from exc
    store.delete(schedule_id)
