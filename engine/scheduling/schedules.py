"""Schedules, their firings, the two tables that hold them, and the rule that says which slot is due.

A **schedule** is one piece of recurring work for one client and one use case: `score` (build a
fresh dataset from the client's latest tables through a saved onboarding recipe and score it with the
champion), `drift_check` (compare the latest scored data with the champion's training baseline) or
`retrain` (a normal training run on the latest data, which the champion rule and the approval
setting then judge). Its cadence is a five-field cron line in a named timezone - `Asia/Kolkata`
unless it says otherwise - or one of three presets in words (`daily`, `weekly`, `monthly`), which
are stored as the cron line they mean so there is one representation to fire from (DEC-761).

A **firing** is one attempt at that work: `queued`, `running` (a run was started and has not ended),
`succeeded`, `failed` or `missed` (a due slot that passed while nothing was running to fire it). It
records what started it (`manual`, `scheduled` or `catch_up`), which run and dataset it produced, and
a short result or error code - never a data value.

**One due slot fires at most once (DEC-763).** `schedule_firing` has a unique index on
`(schedule_id, scheduled_for)`, and a firing claims its slot by inserting that row *before* doing any
work. A second process that reaches the same slot - an EventBridge retry, a second API replica's
local scheduler, the CLI and the API sweeping at the same moment - loses the insert and does
nothing. A manual firing has no slot (`scheduled_for` null, which a unique index never collides on),
so "fire now" is always allowed.

**Missed runs and catch-up (DEC-764).** `due_slots` is the whole policy, as a pure function:

* a slot is fired *on time* when it is the only due slot and it is no older than the grace period
  (two scheduler ticks plus a minute by default - a scheduler that is merely busy is not "missing");
* otherwise every due slot is recorded as a `missed` firing (at most `MAX_MISSED_RECORDED` of them,
  so a per-minute schedule after a week offline cannot write ten thousand rows), one
  `schedule_missed` alert is raised, and **one** `catch_up` firing runs. Not one per missed slot:
  every kind of work here reads the *latest* data, so three catch-up scorings of the same tables
  produce the same scores three times, and three catch-up retrains waste three trainings on one
  dataset. The missed slots stay visible in the firing history, which is what an operator needs.

A disabled schedule is never due and records nothing; enabling it again recomputes `next_due_at`
from that moment, so switching a schedule off for a month does not produce a month of `missed` rows.

The tables live in the platform database (`engine/platform_db.py`, DEC-706);
`alembic/versions/0004_scheduling.py` creates them on Postgres and declares exactly these columns.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Column, DateTime, Index, Text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, select

from engine.platform_db import create_tables
from engine.registry import to_utc
from engine.scheduling.cron import DEFAULT_TIMEZONE, CronExpression, zone
from engine.utils.time import utc_now

__all__ = [
    "FIRING_STATUSES",
    "MAX_MISSED_RECORDED",
    "PRESET_CRON",
    "SCHEDULE_FIRING_TABLE",
    "SCHEDULE_TABLE",
    "SCHEDULING_TABLES",
    "CadencePreset",
    "DueSlots",
    "FiringStatus",
    "FiringTrigger",
    "Schedule",
    "ScheduleError",
    "ScheduleFiring",
    "ScheduleFiringRow",
    "ScheduleKind",
    "ScheduleParameters",
    "ScheduleRow",
    "ScheduleStore",
    "SqlScheduleStore",
    "cadence_cron",
    "create_scheduling_tables",
    "default_grace",
    "due_slots",
    "new_firing_id",
    "new_schedule_id",
]

SCHEDULE_TABLE: Final[str] = "schedule"
SCHEDULE_FIRING_TABLE: Final[str] = "schedule_firing"
SCHEDULING_TABLES: Final[tuple[str, ...]] = (SCHEDULE_TABLE, SCHEDULE_FIRING_TABLE)
"""The tables this module owns, in creation order (`alert` is `engine.scheduling.alerts`')."""

MAX_MISSED_RECORDED: Final[int] = 100
"""At most this many `missed` rows per sweep of one schedule; the alert says "or more" beyond it."""


class ScheduleKind(StrEnum):
    """What a schedule does when it fires."""

    SCORE = "score"
    DRIFT_CHECK = "drift_check"
    RETRAIN = "retrain"


class CadencePreset(StrEnum):
    """Cadences in words. Each is stored as the cron line `PRESET_CRON` gives it."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


PRESET_CRON: Final[Mapping[CadencePreset, str]] = {
    CadencePreset.DAILY: "0 2 * * *",
    CadencePreset.WEEKLY: "0 2 * * 1",
    CadencePreset.MONTHLY: "0 2 1 * *",
}
"""02:00 in the schedule's timezone - after the business day's extracts have landed, before anyone
reads a dashboard - every day, every Monday, or on the 1st of the month."""


class FiringStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MISSED = "missed"


FIRING_STATUSES: Final[tuple[str, ...]] = tuple(status.value for status in FiringStatus)


class FiringTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    CATCH_UP = "catch_up"


class ScheduleError(Exception):
    """A schedule operation was refused. `code` is SCHEDULE_NOT_FOUND | SCHEDULE_EXISTS |
    FIRING_NOT_FOUND | SCHEDULE_INVALID or one of `CronError`'s codes; `message` is business language."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def new_schedule_id() -> str:
    """`sch_<12 hex>`: short enough to be an EventBridge schedule name (64 characters, `[\\w.-]`)."""
    return f"sch_{secrets.token_hex(6)}"


def new_firing_id(now: datetime | None = None) -> str:
    """`fir_<yyyymmdd>_<12 hex>`: sortable by day, unique within it."""
    moment = now or utc_now()
    return f"fir_{moment.strftime('%Y%m%d')}_{secrets.token_hex(6)}"


def cadence_cron(cadence: str) -> tuple[str, CadencePreset | None]:
    """`(cron line, preset)` for a preset name or a cron line; raises `CronError` for neither."""
    try:
        preset = CadencePreset(cadence.strip().lower())
    except ValueError:
        return CronExpression.parse(cadence).text, None
    return PRESET_CRON[preset], preset


# ---------------------------------------------------------------------------
# The contracts
# ---------------------------------------------------------------------------
class ScheduleParameters(BaseModel):
    """What a schedule's work reads. Ids only: a parameter is never a data value.

    A `score` schedule needs a recipe (rebuilt against the client's latest tables at every firing) or
    a fixed dataset; a `retrain` schedule may name either or neither, in which case the firing finds
    the recipe the champion was trained from (`engine.scheduling.firing`); a `drift_check` needs none.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    onboarding_spec_id: str | None = Field(
        default=None, description="Saved onboarding recipe to rebuild a fresh dataset from at each firing."
    )
    dataset_id: str | None = Field(
        default=None, description="A fixed, already-built dataset to read instead of rebuilding one."
    )
    model_version_id: str | None = Field(
        default=None, description="Score with this model version instead of the champion (score only)."
    )


class Schedule(BaseModel):
    """One recurring piece of work for one client and one use case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schedule_id: str = Field(description="Unique id; also the EventBridge schedule name.")
    client_id: str | None = Field(description="Client the work is for; null on a deployment with no clients.")
    use_case_id: str = Field(description="Use case the work is for.")
    kind: ScheduleKind = Field(description="`score`, `drift_check` or `retrain`.")
    cron: str = Field(description="Five-field cron line: minute hour day-of-month month day-of-week.")
    timezone: str = Field(default=DEFAULT_TIMEZONE, description="IANA zone the cron line is read in.")
    preset: CadencePreset | None = Field(
        default=None, description="The preset the cadence was chosen as; null for a custom cron line."
    )
    enabled: bool = Field(default=True, description="A disabled schedule is kept but never fires.")
    parameters: ScheduleParameters = Field(
        default_factory=ScheduleParameters, description="Ids the work reads."
    )
    managed_by: str | None = Field(
        default=None,
        description="`monitoring.retraining` for a schedule the engine keeps in sync with that setting.",
    )
    created_by: str = Field(description="`Principal.user_id` of whoever created it.")
    created_at: datetime = Field(description="UTC time it was created.")
    updated_at: datetime = Field(description="UTC time it last changed.")
    next_due_at: datetime | None = Field(default=None, description="UTC time of the next due slot.")
    last_fired_at: datetime | None = Field(default=None, description="UTC time it last fired.")

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        return CronExpression.parse(value).text

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        zone(value)
        return value

    @model_validator(mode="after")
    def _parameters_fit_kind(self) -> Schedule:
        chosen = self.parameters
        if self.kind is ScheduleKind.SCORE and not (chosen.onboarding_spec_id or chosen.dataset_id):
            raise ValueError("a score schedule needs an onboarding_spec_id or a dataset_id to score")
        if self.kind is not ScheduleKind.SCORE and chosen.model_version_id is not None:
            raise ValueError("only a score schedule may pin a model_version_id")
        if chosen.onboarding_spec_id and chosen.dataset_id:
            raise ValueError("name a recipe to rebuild or a fixed dataset, not both")
        return self

    @property
    def expression(self) -> CronExpression:
        return CronExpression.parse(self.cron)

    def next_slot_after(self, moment: datetime) -> datetime | None:
        """The first due slot strictly after `moment`, in UTC."""
        return self.expression.next_after(moment, timezone=self.timezone)


class ScheduleFiring(BaseModel):
    """One attempt at a schedule's work, or one slot that passed with nobody there to fire it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    firing_id: str = Field(description="Unique id of this firing.")
    schedule_id: str = Field(description="Schedule it belongs to.")
    client_id: str | None = Field(description="The schedule's client, repeated for listings.")
    use_case_id: str = Field(description="The schedule's use case, repeated for listings.")
    kind: ScheduleKind = Field(description="The schedule's kind at the time it fired.")
    trigger: FiringTrigger = Field(description="`manual`, `scheduled` or `catch_up`.")
    status: FiringStatus = Field(description="queued, running, succeeded, failed or missed.")
    scheduled_for: datetime | None = Field(
        default=None, description="The due slot this firing is for; null for a manual or catch-up firing."
    )
    run_id: str | None = Field(default=None, description="Run the firing started, if any.")
    dataset_id: str | None = Field(default=None, description="Dataset the firing built or read, if any.")
    result_code: str | None = Field(default=None, description="Short outcome token, e.g. DRIFT_OK.")
    error_code: str | None = Field(default=None, description="Why it failed; null unless it failed.")
    flagged_models: tuple[str, ...] = Field(
        default=(), description="Erasure-flagged model versions this firing's retraining answers for."
    )
    fired_at: datetime = Field(description="UTC time the firing was recorded.")
    finished_at: datetime | None = Field(default=None, description="UTC time it reached a final status.")

    @property
    def finished(self) -> bool:
        return self.status in (FiringStatus.SUCCEEDED, FiringStatus.FAILED, FiringStatus.MISSED)


# ---------------------------------------------------------------------------
# The due-slot rule (DEC-764)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DueSlots:
    """What a scheduler should do with one schedule at one instant."""

    on_time: datetime | None
    """Fire this slot now, as `scheduled`."""
    missed: tuple[datetime, ...]
    """Record these slots as `missed` (oldest first, at most `MAX_MISSED_RECORDED`)."""
    missed_truncated: bool
    """True when more slots were missed than `missed` holds."""
    catch_up: bool
    """Run one `catch_up` firing (only ever alongside a non-empty `missed`)."""
    next_due_at: datetime | None
    """The schedule's new `next_due_at`; the old one when nothing was due."""

    @property
    def anything(self) -> bool:
        return self.on_time is not None or bool(self.missed)


def default_grace(tick_seconds: int) -> timedelta:
    """How late a slot may be fired and still count as on time: two ticks and a minute."""
    return timedelta(seconds=2 * tick_seconds + 60)


def due_slots(schedule: Schedule, now: datetime, *, grace: timedelta) -> DueSlots:
    """The policy of DEC-764 as a pure function of the schedule and the clock."""
    nothing = DueSlots(None, (), False, False, schedule.next_due_at)
    if not schedule.enabled or schedule.next_due_at is None or to_utc(schedule.next_due_at) > now:
        return nothing
    expression = schedule.expression
    first = to_utc(schedule.next_due_at)
    slots = expression.slots_between(first, now, timezone=schedule.timezone, limit=MAX_MISSED_RECORDED + 1)
    upcoming = expression.next_after(now, timezone=schedule.timezone)
    if not slots:  # next_due_at was off the grid (the cadence changed): just move it on
        return DueSlots(None, (), False, False, upcoming)
    if len(slots) == 1 and now - slots[0] <= grace:
        return DueSlots(slots[0], (), False, False, upcoming)
    truncated = len(slots) > MAX_MISSED_RECORDED
    return DueSlots(None, slots[:MAX_MISSED_RECORDED], truncated, True, upcoming)


# ---------------------------------------------------------------------------
# The tables
# ---------------------------------------------------------------------------
class ScheduleRow(SQLModel, table=True):
    """`schedule`: one row per schedule; `parameters_json` holds `ScheduleParameters`."""

    __tablename__ = SCHEDULE_TABLE
    __table_args__: ClassVar[Any] = (Index("ix_schedule_scope", "client_id", "use_case_id"),)

    schedule_id: str = SQLField(primary_key=True)
    client_id: str | None = None
    use_case_id: str
    kind: str
    cron: str
    timezone: str
    preset: str | None = None
    enabled: bool
    parameters_json: str = SQLField(sa_column=Column("parameters_json", Text(), nullable=False))
    managed_by: str | None = None
    created_by: str
    created_at: datetime = SQLField(sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    updated_at: datetime = SQLField(sa_column=Column("updated_at", DateTime(timezone=True), nullable=False))
    next_due_at: datetime | None = SQLField(
        default=None, sa_column=Column("next_due_at", DateTime(timezone=True), nullable=True)
    )
    last_fired_at: datetime | None = SQLField(
        default=None, sa_column=Column("last_fired_at", DateTime(timezone=True), nullable=True)
    )


class ScheduleFiringRow(SQLModel, table=True):
    """`schedule_firing`: one row per firing; the unique slot index is DEC-763."""

    __tablename__ = SCHEDULE_FIRING_TABLE
    __table_args__: ClassVar[Any] = (
        Index("ux_schedule_firing_slot", "schedule_id", "scheduled_for", unique=True),
        Index("ix_schedule_firing_status", "status"),
    )

    firing_id: str = SQLField(primary_key=True)
    schedule_id: str = SQLField(index=True)
    client_id: str | None = None
    use_case_id: str
    kind: str
    trigger: str
    status: str
    scheduled_for: datetime | None = SQLField(
        default=None, sa_column=Column("scheduled_for", DateTime(timezone=True), nullable=True)
    )
    run_id: str | None = None
    dataset_id: str | None = None
    result_code: str | None = None
    error_code: str | None = None
    flagged_models_json: str = SQLField(sa_column=Column("flagged_models_json", Text(), nullable=False))
    fired_at: datetime = SQLField(sa_column=Column("fired_at", DateTime(timezone=True), nullable=False))
    finished_at: datetime | None = SQLField(
        default=None, sa_column=Column("finished_at", DateTime(timezone=True), nullable=True)
    )


def create_scheduling_tables(engine: Engine) -> None:
    """Create `schedule` and `schedule_firing` if missing (SQLite only; Alembic owns Postgres)."""
    create_tables(engine, SCHEDULING_TABLES)


def _utc_or_none(moment: datetime | None) -> datetime | None:
    return None if moment is None else to_utc(moment)


def _schedule_row(schedule: Schedule) -> ScheduleRow:
    return ScheduleRow(
        schedule_id=schedule.schedule_id,
        client_id=schedule.client_id,
        use_case_id=schedule.use_case_id,
        kind=schedule.kind.value,
        cron=schedule.cron,
        timezone=schedule.timezone,
        preset=None if schedule.preset is None else schedule.preset.value,
        enabled=schedule.enabled,
        parameters_json=schedule.parameters.model_dump_json(),
        managed_by=schedule.managed_by,
        created_by=schedule.created_by,
        created_at=to_utc(schedule.created_at),
        updated_at=to_utc(schedule.updated_at),
        next_due_at=_utc_or_none(schedule.next_due_at),
        last_fired_at=_utc_or_none(schedule.last_fired_at),
    )


def _schedule(row: ScheduleRow) -> Schedule:
    return Schedule(
        schedule_id=row.schedule_id,
        client_id=row.client_id,
        use_case_id=row.use_case_id,
        kind=ScheduleKind(row.kind),
        cron=row.cron,
        timezone=row.timezone,
        preset=None if row.preset is None else CadencePreset(row.preset),
        enabled=row.enabled,
        parameters=ScheduleParameters.model_validate_json(row.parameters_json),
        managed_by=row.managed_by,
        created_by=row.created_by,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
        next_due_at=_utc_or_none(row.next_due_at),
        last_fired_at=_utc_or_none(row.last_fired_at),
    )


def _firing_row(firing: ScheduleFiring) -> ScheduleFiringRow:
    return ScheduleFiringRow(
        firing_id=firing.firing_id,
        schedule_id=firing.schedule_id,
        client_id=firing.client_id,
        use_case_id=firing.use_case_id,
        kind=firing.kind.value,
        trigger=firing.trigger.value,
        status=firing.status.value,
        scheduled_for=_utc_or_none(firing.scheduled_for),
        run_id=firing.run_id,
        dataset_id=firing.dataset_id,
        result_code=firing.result_code,
        error_code=firing.error_code,
        flagged_models_json=json.dumps(list(firing.flagged_models)),
        fired_at=to_utc(firing.fired_at),
        finished_at=_utc_or_none(firing.finished_at),
    )


def _firing(row: ScheduleFiringRow) -> ScheduleFiring:
    return ScheduleFiring(
        firing_id=row.firing_id,
        schedule_id=row.schedule_id,
        client_id=row.client_id,
        use_case_id=row.use_case_id,
        kind=ScheduleKind(row.kind),
        trigger=FiringTrigger(row.trigger),
        status=FiringStatus(row.status),
        scheduled_for=_utc_or_none(row.scheduled_for),
        run_id=row.run_id,
        dataset_id=row.dataset_id,
        result_code=row.result_code,
        error_code=row.error_code,
        flagged_models=tuple(json.loads(row.flagged_models_json)),
        fired_at=to_utc(row.fired_at),
        finished_at=_utc_or_none(row.finished_at),
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
@runtime_checkable
class ScheduleStore(Protocol):
    """Where schedules and their firings live. SQL on every deployment (the platform database)."""

    def create(self, schedule: Schedule) -> Schedule: ...

    def get(self, schedule_id: str) -> Schedule: ...

    def list(
        self,
        *,
        client_id: str | None = None,
        use_case_id: str | None = None,
        kind: ScheduleKind | None = None,
        managed_by: str | None = None,
    ) -> tuple[Schedule, ...]: ...

    def save(self, schedule: Schedule) -> Schedule: ...

    def delete(self, schedule_id: str) -> None: ...

    def claim_firing(self, firing: ScheduleFiring) -> ScheduleFiring | None: ...

    def save_firing(self, firing: ScheduleFiring) -> ScheduleFiring: ...

    def settle_firing(self, firing: ScheduleFiring) -> bool: ...

    def get_firing(self, firing_id: str) -> ScheduleFiring: ...

    def list_firings(
        self,
        *,
        schedule_id: str | None = None,
        status: FiringStatus | None = None,
        limit: int = 100,
    ) -> tuple[ScheduleFiring, ...]: ...


class SqlScheduleStore:
    """`ScheduleStore` on a SQLAlchemy engine (SQLite `platform.db` locally, Postgres deployed)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        create_scheduling_tables(engine)

    @property
    def engine(self) -> Engine:
        return self._engine

    # --- schedules --------------------------------------------------------------------------------
    def create(self, schedule: Schedule) -> Schedule:
        """Insert a new schedule; `SCHEDULE_EXISTS` when its id is taken."""
        with Session(self._engine) as session:
            if session.get(ScheduleRow, schedule.schedule_id) is not None:
                raise ScheduleError("SCHEDULE_EXISTS", f"A schedule with id {schedule.schedule_id!r} exists.")
            session.add(_schedule_row(schedule))
            session.commit()
        return schedule

    def get(self, schedule_id: str) -> Schedule:
        with Session(self._engine) as session:
            row = session.get(ScheduleRow, schedule_id)
            if row is None:
                raise ScheduleError("SCHEDULE_NOT_FOUND", f"No schedule with id {schedule_id!r}.")
            return _schedule(row)

    def list(
        self,
        *,
        client_id: str | None = None,
        use_case_id: str | None = None,
        kind: ScheduleKind | None = None,
        managed_by: str | None = None,
    ) -> tuple[Schedule, ...]:
        """Matching schedules, oldest first. Every filter given must match."""
        statement = select(ScheduleRow)
        if client_id is not None:
            statement = statement.where(col(ScheduleRow.client_id) == client_id)
        if use_case_id is not None:
            statement = statement.where(col(ScheduleRow.use_case_id) == use_case_id)
        if kind is not None:
            statement = statement.where(col(ScheduleRow.kind) == kind.value)
        if managed_by is not None:
            statement = statement.where(col(ScheduleRow.managed_by) == managed_by)
        statement = statement.order_by(col(ScheduleRow.created_at), col(ScheduleRow.schedule_id))
        with Session(self._engine) as session:
            return tuple(_schedule(row) for row in session.exec(statement).all())

    def save(self, schedule: Schedule) -> Schedule:
        """Replace an existing schedule's row; `SCHEDULE_NOT_FOUND` when there is none."""
        with Session(self._engine) as session:
            existing = session.get(ScheduleRow, schedule.schedule_id)
            if existing is None:
                raise ScheduleError("SCHEDULE_NOT_FOUND", f"No schedule with id {schedule.schedule_id!r}.")
            for name, value in _schedule_row(schedule).model_dump().items():
                setattr(existing, name, value)
            session.add(existing)
            session.commit()
        return schedule

    def delete(self, schedule_id: str) -> None:
        """Remove a schedule. Its firings stay: they are the history of what it did."""
        with Session(self._engine) as session:
            row = session.get(ScheduleRow, schedule_id)
            if row is None:
                raise ScheduleError("SCHEDULE_NOT_FOUND", f"No schedule with id {schedule_id!r}.")
            session.delete(row)
            session.commit()

    # --- firings -----------------------------------------------------------------------------------
    def claim_firing(self, firing: ScheduleFiring) -> ScheduleFiring | None:
        """Insert a firing; `None` when its slot was already claimed by someone else (DEC-763)."""
        try:
            with Session(self._engine) as session:
                session.add(_firing_row(firing))
                session.commit()
        except IntegrityError:
            return None
        return firing

    def save_firing(self, firing: ScheduleFiring) -> ScheduleFiring:
        """Replace an existing firing's row (status, run, codes, finish time)."""
        with Session(self._engine) as session:
            existing = session.get(ScheduleFiringRow, firing.firing_id)
            if existing is None:
                raise ScheduleError("FIRING_NOT_FOUND", f"No firing with id {firing.firing_id!r}.")
            for name, value in _firing_row(firing).model_dump().items():
                setattr(existing, name, value)
            session.add(existing)
            session.commit()
        return firing

    def settle_firing(self, firing: ScheduleFiring) -> bool:
        """Write a firing's outcome only if it is still `running`; False when another settler won.

        One conditional UPDATE, so two processes settling the same firing cannot both finish it -
        and so cannot both raise its alert (DEC-779).
        """
        values = _firing_row(firing).model_dump()
        del values["firing_id"]
        statement = (
            update(ScheduleFiringRow)
            .where(col(ScheduleFiringRow.firing_id) == firing.firing_id)
            .where(col(ScheduleFiringRow.status) == FiringStatus.RUNNING.value)
            .values(**values)
        )
        with Session(self._engine) as session:
            result = session.exec(statement)
            session.commit()
            return int(result.rowcount or 0) == 1

    def get_firing(self, firing_id: str) -> ScheduleFiring:
        with Session(self._engine) as session:
            row = session.get(ScheduleFiringRow, firing_id)
            if row is None:
                raise ScheduleError("FIRING_NOT_FOUND", f"No firing with id {firing_id!r}.")
            return _firing(row)

    def list_firings(
        self,
        *,
        schedule_id: str | None = None,
        status: FiringStatus | None = None,
        limit: int = 100,
    ) -> tuple[ScheduleFiring, ...]:
        """Matching firings, newest first."""
        statement = select(ScheduleFiringRow)
        if schedule_id is not None:
            statement = statement.where(col(ScheduleFiringRow.schedule_id) == schedule_id)
        if status is not None:
            statement = statement.where(col(ScheduleFiringRow.status) == status.value)
        statement = statement.order_by(
            col(ScheduleFiringRow.fired_at).desc(), col(ScheduleFiringRow.firing_id).desc()
        ).limit(limit)
        with Session(self._engine) as session:
            return tuple(_firing(row) for row in session.exec(statement).all())
