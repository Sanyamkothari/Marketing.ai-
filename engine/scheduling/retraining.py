"""`monitoring.retraining` made live, and the erasure flags the next retraining cycle answers (M49).

Phase 1 shipped `monitoring.retraining` (`manual | on_drift | weekly | monthly`) as a setting that
nothing read. This module is what reads it. For every client x use case that has an onboarding
recipe, `sync_retraining_schedules` keeps the *managed* schedules - `managed_by =
"monitoring.retraining"` - in line with the setting (DEC-767):

=============  =================================================================================
`manual`       no managed schedule at all: nothing retrains unless a person starts it
`weekly`       one managed `retrain` schedule, preset `weekly`
`monthly`      one managed `retrain` schedule, preset `monthly`
`on_drift`     one managed `drift_check` schedule, preset `weekly`; the drift check retrains when
               the latest scored data has drifted beyond `monitoring.drift_psi_threshold`
=============  =================================================================================

A person's own schedules (`managed_by` null) are never touched here: changing the setting to
`manual` removes what the setting created and nothing else. The managed ids are derived from the
client, the use case and the kind, so a second sync - or two replicas syncing at once - finds the
same schedule instead of creating a twin.

**Erasure flags (DEC-743 / DEC-768).** M48 flags every model version whose training data included an
erased data principal (`model_retrain_flag`), and says the model is retrained at the *next scheduled
cycle*, not immediately. The cycle is this module's: a `retrain` firing, or a `drift_check` firing
under `on_drift` (which retrains when a model of its use case is flagged even if nothing drifted),
records which flagged versions of its use case it answers for, and the flags are cleared only when
that training run finishes and registers a new version - never when it merely starts, and never when
it fails. The new version is a challenger like any other: the champion rule and
`governance.approval_required` decide what happens to it, and the flagged champion stays champion
until an Approver says otherwise.

The flags are read through `RetrainFlags`, a two-method protocol, rather than by importing M48's
module at the top of this one: M48 and M49 were built side by side, and the scheduler must work -
with no flags - on a deployment where the privacy tables have not been created.
`privacy_retrain_flags(engine)` is the adapter to M48's `engine.privacy.erasure` functions.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from sqlalchemy.engine import Engine

from engine.access.roles import SYSTEM_SCHEDULER
from engine.config import ConfigError, Retraining, resolve_config
from engine.registry import ModelRegistry, RegistryError
from engine.scheduling.scheduler import Scheduler
from engine.scheduling.schedules import (
    PRESET_CRON,
    CadencePreset,
    Schedule,
    ScheduleKind,
    ScheduleStore,
)
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "MANAGED_BY_RETRAINING",
    "NO_RETRAIN_FLAGS",
    "CallableRetrainFlags",
    "NoRetrainFlags",
    "RetrainFlags",
    "RetrainingTarget",
    "SyncResult",
    "flagged_for_use_case",
    "managed_schedule_id",
    "privacy_retrain_flags",
    "retraining_targets",
    "sync_retraining_schedules",
    "wanted_schedules",
]

_LOGGER = get_logger(__name__)

MANAGED_BY_RETRAINING: Final[str] = "monitoring.retraining"
"""`Schedule.managed_by` of every schedule this module creates, and the only ones it may change."""


# ---------------------------------------------------------------------------
# Erasure flags
# ---------------------------------------------------------------------------
@runtime_checkable
class RetrainFlags(Protocol):
    """The open erasure retraining flags, and the way to close one."""

    def flagged(self) -> tuple[str, ...]: ...

    def clear(self, model_id: str) -> int: ...


class NoRetrainFlags:
    """No flags: a deployment without M48's tables, and every test that is not about flags."""

    def flagged(self) -> tuple[str, ...]:
        return ()

    def clear(self, model_id: str) -> int:
        del model_id
        return 0


NO_RETRAIN_FLAGS: Final[RetrainFlags] = NoRetrainFlags()


@dataclass(frozen=True, slots=True)
class CallableRetrainFlags:
    """`RetrainFlags` from two callables - how M48's functions are wired in without importing them here."""

    list_flagged: Callable[[], tuple[str, ...]]
    clear_one: Callable[[str], int]

    def flagged(self) -> tuple[str, ...]:
        return tuple(self.list_flagged())

    def clear(self, model_id: str) -> int:
        return int(self.clear_one(model_id))


def privacy_retrain_flags(engine: Engine) -> RetrainFlags:
    """M48's flags on the platform database: `models_flagged_for_retraining` and `clear_flag`.

    Imported when called rather than at module scope, so the scheduling package imports cleanly on
    a tree - or at a moment - where the privacy package is absent.
    """

    def list_flagged() -> tuple[str, ...]:
        from engine.privacy.erasure import models_flagged_for_retraining

        return models_flagged_for_retraining(engine)

    def clear_one(model_id: str) -> int:
        from engine.privacy.erasure import clear_flag

        return clear_flag(engine, model_id)

    return CallableRetrainFlags(list_flagged=list_flagged, clear_one=clear_one)


def flagged_for_use_case(flags: RetrainFlags, registry: ModelRegistry, use_case_id: str) -> tuple[str, ...]:
    """The flagged model versions that belong to `use_case_id`, sorted.

    Asked of the registry; a version the registry no longer knows is judged by its id, which is
    `m_<use case>_<n>` by construction (`engine.utils.ids.new_model_id`).
    """
    found: list[str] = []
    for model_id in flags.flagged():
        try:
            owner = registry.get(model_id).use_case_id
        except RegistryError:
            owner = model_id.removeprefix("m_").rsplit("_", 1)[0]
        if owner == use_case_id:
            found.append(model_id)
    return tuple(sorted(found))


# ---------------------------------------------------------------------------
# Managed schedules (DEC-767)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RetrainingTarget:
    """One client x use case whose `monitoring.retraining` the managed schedules should follow."""

    client_id: str | None
    use_case_id: str
    retraining: Retraining


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What one sync changed, by schedule id."""

    created: tuple[str, ...]
    updated: tuple[str, ...]
    removed: tuple[str, ...]


def managed_schedule_id(kind: ScheduleKind, client_id: str | None, use_case_id: str) -> str:
    """`sch_auto_<kind>_<12 hex>`, stable for one client x use case x kind, and a valid EventBridge name."""
    digest = hashlib.sha256(f"{client_id or ''}\x00{use_case_id}\x00{kind.value}".encode()).hexdigest()[:12]
    short = {"score": "sc", "drift_check": "dc", "retrain": "rt"}[kind.value]
    return f"sch_auto_{short}_{digest}"


def wanted_schedules(retraining: Retraining) -> dict[ScheduleKind, CadencePreset]:
    """The managed schedules one value of `monitoring.retraining` asks for (the table above)."""
    if retraining is Retraining.WEEKLY:
        return {ScheduleKind.RETRAIN: CadencePreset.WEEKLY}
    if retraining is Retraining.MONTHLY:
        return {ScheduleKind.RETRAIN: CadencePreset.MONTHLY}
    if retraining is Retraining.ON_DRIFT:
        return {ScheduleKind.DRIFT_CHECK: CadencePreset.WEEKLY}
    return {}


def retraining_targets(client_store: object | None, config_root: Path) -> tuple[RetrainingTarget, ...]:
    """Every client x use case with a training recipe, and that use case's `monitoring.retraining`.

    `client_store` is an `engine.clients.ClientStore`; typed loosely so this module does not import
    the onboarding package for a signature. A use case whose configuration no longer loads is skipped
    with a WARNING rather than failing the whole sync.
    """
    if client_store is None:
        return ()
    from engine.clients import ClientStore

    if not isinstance(client_store, ClientStore):
        raise TypeError("client_store must be an engine.clients.ClientStore")
    targets: dict[tuple[str, str], RetrainingTarget] = {}
    for client in client_store.list_clients():
        for spec in client_store.list_specs(client.client_id):
            if spec.label_spec is None or (client.client_id, spec.use_case) in targets:
                continue
            try:
                setting = resolve_config(spec.use_case, root=config_root).config.monitoring.retraining
            except ConfigError:
                _LOGGER.warning(
                    "retraining.sync skipped use_case=%s reason=config_unavailable", spec.use_case
                )
                continue
            targets[(client.client_id, spec.use_case)] = RetrainingTarget(
                client.client_id, spec.use_case, setting
            )
    return tuple(targets[key] for key in sorted(targets))


def sync_retraining_schedules(
    store: ScheduleStore,
    scheduler: Scheduler,
    targets: Iterable[RetrainingTarget],
    *,
    now: datetime | None = None,
) -> SyncResult:
    """Create, update and remove managed schedules until they match every target's setting.

    Idempotent: a second call with the same targets changes nothing. Each change is also pushed to
    `scheduler` (a no-op locally, the EventBridge schedule otherwise), after the row is written, so
    the table stays the source of truth if the push fails - and every wanted schedule is pushed on
    every sync, so the next sync repairs a push that failed (DEC-776).
    """
    moment = now or utc_now()
    created: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    for target in targets:
        wanted = wanted_schedules(target.retraining)
        existing = {
            schedule.schedule_id: schedule
            for schedule in store.list(use_case_id=target.use_case_id, managed_by=MANAGED_BY_RETRAINING)
            if schedule.client_id == target.client_id
        }
        wanted_ids = {
            managed_schedule_id(kind, target.client_id, target.use_case_id): kind for kind in wanted
        }
        for schedule_id in existing:
            if schedule_id not in wanted_ids:
                store.delete(schedule_id)
                scheduler.remove(schedule_id)
                removed.append(schedule_id)
        for schedule_id, kind in wanted_ids.items():
            preset = wanted[kind]
            cron = PRESET_CRON[preset]
            current = existing.get(schedule_id)
            if current is None:
                schedule = Schedule(
                    schedule_id=schedule_id,
                    client_id=target.client_id,
                    use_case_id=target.use_case_id,
                    kind=kind,
                    cron=cron,
                    preset=preset,
                    managed_by=MANAGED_BY_RETRAINING,
                    created_by=SYSTEM_SCHEDULER.user_id,
                    created_at=moment,
                    updated_at=moment,
                )
                schedule = schedule.model_copy(update={"next_due_at": schedule.next_slot_after(moment)})
                store.create(schedule)
                scheduler.sync(schedule)
                created.append(schedule_id)
            elif current.cron != cron or current.preset is not preset:
                changed = current.model_copy(update={"cron": cron, "preset": preset, "updated_at": moment})
                changed = changed.model_copy(update={"next_due_at": changed.next_slot_after(moment)})
                store.save(changed)
                scheduler.sync(changed)
                updated.append(schedule_id)
            else:
                # Unchanged, but pushed again: the row is written before its push, so a push that
                # failed last time left a row EventBridge never heard of, and only a re-push on a
                # later sync repairs it. `Scheduler.sync` is create-or-update (DEC-776).
                scheduler.sync(current)
    return SyncResult(created=tuple(created), updated=tuple(updated), removed=tuple(removed))
