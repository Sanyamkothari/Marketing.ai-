"""The holdout salt, its fingerprint, and the epoch ledger (DEC-1302 (b)).

**The salt** is `MARKETING_AI_HOLDOUT_SALT` (`Settings.holdout_salt`, a secret: hidden by
`redacted()` and never named by `summary()`). It has no default and no generated fallback: a
persistent holdout is a promise to the same customers for months, and a salt that a laptop made up,
or that changed with a container, would quietly break it. Without it a persistent scope refuses to
score (`HOLDOUT_SALT_MISSING`); `scope: run` never reads it.

**The fingerprint** - a domain-separated SHA-256 of the salt, never the salt - is kept in the
platform database's `platform_setting` table under `holdout_salt_fingerprint` the first time a
persistent holdout is used, as `engine.privacy.config` keeps the privacy salt's. A different salt
afterwards refuses scoring (`HOLDOUT_SALT_CHANGED`): every customer would silently move in or out of
the holdout. Only an Admin rotating the salt on purpose (`PUT /holdout` with `rotate_salt`) records
the new fingerprint, and that starts a new epoch of every persistent holdout.

**Epochs.** Each persistent holdout (`universal`, or `use_case` per use case) has a ledger entry
(`holdout_epoch:<scope>:<key>`, a JSON `HoldoutLedgerEntry`) with its epoch and the largest
fraction used in it:

* the first scoring run records epoch 1 at the configured fraction;
* **raising** a `use_case` holdout's fraction keeps every member (the rule nests), so a run with a
  higher fraction simply records it - same epoch;
* **lowering** it would turn held-out customers into contacted ones, so results before and after
  cannot be added up: scoring is refused (`HOLDOUT_FRACTION_LOWERED`) until an Admin starts a new
  epoch at the lower fraction (`PUT /holdout`, audited);
* the **universal** holdout has one share for every use case on it, the ledger's: a use case asking
  for less is refused as above, and one asking for more is refused too (`HOLDOUT_FRACTION_MISMATCH`)
  - it would hold out customers that the others, within the same epoch, contact. Changing the
  universal share is an Admin's new epoch, after every universal use case has been set to it;
* **rotating the salt** reshuffles everyone: a new epoch of every entry. Rotating to the salt
  already recorded is refused (`HOLDOUT_SALT_UNCHANGED`): it would record a reshuffle that did not
  happen.

The ledger is facts about the platform, never a secret and never a person's data, as the table's
contract requires.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from engine.holdout.spec import (
    HOLDOUT_FRACTION_LOWERED,
    HOLDOUT_FRACTION_MISMATCH,
    HOLDOUT_SALT_CHANGED,
    HOLDOUT_SALT_MISSING,
    HOLDOUT_SALT_UNCHANGED,
    UNIVERSAL_SCOPE_KEY,
    HoldoutError,
    HoldoutLedgerEntry,
    HoldoutSpec,
    PersistentScope,
    effective_holdout_fraction,
    ledger_key,
    reserved_scope_error,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from engine.config import UseCaseConfig
    from engine.holdout.assign import ActiveHoldout
    from engine.settings import Settings
    from engine.storage import Storage

__all__ = [
    "FINGERPRINT_KEY",
    "HoldoutLedger",
    "ResolvedHoldout",
    "configured_salt",
    "holdout_engine_for",
    "resolve_holdout",
    "salt_fingerprint",
    "salt_id",
    "start_epoch",
]

FINGERPRINT_KEY: Final[str] = "holdout_salt_fingerprint"
"""The `platform_setting` key the holdout salt's fingerprint is kept under."""

_FINGERPRINT_DOMAIN: Final[bytes] = b"marketing-ai/holdout-salt-fingerprint\x00"
_LEDGER_PREFIX: Final[str] = "holdout_epoch:"
_SALT_ID_LENGTH: Final[int] = 16


def salt_fingerprint(salt: str) -> str:
    """What `platform_setting` keeps to recognise the salt: a domain-separated SHA-256, never the salt."""
    return hashlib.sha256(_FINGERPRINT_DOMAIN + salt.encode("utf-8")).hexdigest()


def salt_id(fingerprint: str) -> str:
    """The short form shown on screens and recorded on a run: the fingerprint's first 16 hex characters."""
    return fingerprint[:_SALT_ID_LENGTH]


def configured_salt(settings: Settings) -> str | None:
    """`MARKETING_AI_HOLDOUT_SALT`, stripped, or None when it is not set."""
    if settings.holdout_salt is None:
        return None
    return settings.holdout_salt.get_secret_value().strip() or None


def _missing_salt() -> HoldoutError:
    from engine.settings import ENV_VARS

    return HoldoutError(
        HOLDOUT_SALT_MISSING,
        f"This use case holds the same customers out every run, which needs the secret "
        f"{ENV_VARS['holdout_salt']}. Ask the administrator to set it (at least 16 characters, kept "
        "for as long as the holdout runs), or set actions.holdout.scope back to run.",
    )


class HoldoutLedger:
    """The salt fingerprint and the epoch entries, in one database's `platform_setting` table."""

    def __init__(self, engine: Engine) -> None:
        from engine.platform_db import PLATFORM_SETTING_TABLE, create_tables

        self._engine = engine
        create_tables(engine, (PLATFORM_SETTING_TABLE,))

    # -- reads ---------------------------------------------------------------------------------
    def fingerprint(self) -> str | None:
        return self._get(FINGERPRINT_KEY)

    def entry(self, scope: str, key: str) -> HoldoutLedgerEntry | None:
        raw = self._get(ledger_key(scope, key))
        return None if raw is None else HoldoutLedgerEntry.model_validate_json(raw)

    def entries(self) -> tuple[HoldoutLedgerEntry, ...]:
        from sqlmodel import Session, col, select

        from engine.platform_db import PlatformSettingRow

        with Session(self._engine) as session:
            rows = session.exec(
                select(PlatformSettingRow)
                .where(col(PlatformSettingRow.key).startswith(_LEDGER_PREFIX))
                .order_by(col(PlatformSettingRow.key))
            ).all()
        return tuple(HoldoutLedgerEntry.model_validate_json(row.value) for row in rows)

    # -- writes --------------------------------------------------------------------------------
    def put_fingerprint(self, fingerprint: str, *, at: datetime) -> None:
        self._put(FINGERPRINT_KEY, fingerprint, at=at)

    def put_entry(self, entry: HoldoutLedgerEntry) -> None:
        self._put(ledger_key(entry.scope, entry.scope_key), entry.model_dump_json(), at=entry.updated_at)

    def snapshot(self) -> dict[str, object]:
        """Everything the ledger holds, for an audit event's before/after hash."""
        return {
            "fingerprint": self.fingerprint(),
            "entries": [entry.model_dump(mode="json") for entry in self.entries()],
        }

    def _get(self, key: str) -> str | None:
        from sqlmodel import Session

        from engine.platform_db import PlatformSettingRow

        with Session(self._engine) as session:
            row = session.get(PlatformSettingRow, key)
            return None if row is None else row.value

    def _put(self, key: str, value: str, *, at: datetime) -> None:
        from sqlalchemy.exc import IntegrityError
        from sqlmodel import Session

        from engine.platform_db import PlatformSettingRow

        with Session(self._engine) as session:
            row = session.get(PlatformSettingRow, key)
            if row is None:
                session.add(PlatformSettingRow(key=key, value=value, updated_at=at))
            else:
                row.value = value
                row.updated_at = at
                session.add(row)
            try:
                session.commit()
            except IntegrityError:  # another process inserted it first: overwrite theirs with ours
                session.rollback()
                existing = session.get(PlatformSettingRow, key)
                if existing is None:
                    raise
                existing.value = value
                existing.updated_at = at
                session.add(existing)
                session.commit()


def holdout_engine_for(storage: Storage, settings: Settings) -> Engine:
    """The platform database a scoring run's holdout ledger lives in (the one `GET /holdout` reads).

    A local store's `platform.db` sits in its own root, as the consent ledger's does; it is created
    when missing, which happens only for a run that uses a persistent holdout. A non-local store
    follows the settings: SQLite beside `data_dir`, or the registry's Postgres.
    """
    from engine.platform_db import PLATFORM_DB_FILENAME, platform_engine, sqlite_engine
    from engine.storage import LocalStorage

    if settings.metadata_backend == "sqlite":
        root: Path = storage.root if isinstance(storage, LocalStorage) else settings.data_dir
        return sqlite_engine(root / PLATFORM_DB_FILENAME)
    return platform_engine(settings)


@dataclass(frozen=True)
class ResolvedHoldout:
    """What a scoring run uses: the run's `HoldoutSpec`, and the active holdout (None under `run`)."""

    spec: HoldoutSpec
    active: ActiveHoldout | None


def resolve_holdout(
    config: UseCaseConfig,
    settings: Settings | None,
    engine: Engine | None,
    *,
    at: datetime,
    record: bool,
) -> ResolvedHoldout:
    """The holdout a scoring run of `config` must use; raises `HoldoutError` when it may not score.

    Under `scope: run` nothing is read: the spec says run, the active holdout is None. Under a
    persistent scope `settings` and `engine` are required, the salt must be set and match the
    recorded fingerprint, and the configured fraction may not be below the current epoch's (the
    epoch's largest so far). With `record`, a first use records the
    fingerprint and epoch 1, and a raised fraction is recorded in the same epoch; without it nothing
    is written (the check before a run starts).
    """
    from engine.holdout.assign import ActiveHoldout

    actions = config.actions
    explore = float(actions.explore_fraction)
    fraction = effective_holdout_fraction(actions)
    scope = actions.holdout.scope
    if scope == "run":
        return ResolvedHoldout(HoldoutSpec(scope="run", fraction=fraction, explore_fraction=explore), None)
    reserved = reserved_scope_error(scope, config.id)
    if reserved is not None:
        raise reserved
    key = config.id if scope == "use_case" else UNIVERSAL_SCOPE_KEY
    if settings is None or engine is None:
        raise ValueError(
            "a persistent holdout is resolved with the deployment's settings and platform database"
        )
    salt = configured_salt(settings)
    if salt is None:
        raise _missing_salt()
    fingerprint = salt_fingerprint(salt)
    ledger = HoldoutLedger(engine)
    stored = ledger.fingerprint()
    if stored is not None and stored != fingerprint:
        from engine.settings import ENV_VARS

        raise HoldoutError(
            HOLDOUT_SALT_CHANGED,
            f"{ENV_VARS['holdout_salt']} is not the salt this deployment's holdout was drawn with, so "
            "every customer would move in or out of the holdout. Set the original salt again, or ask an "
            "Admin to start a new holdout epoch with the new salt (PUT /holdout with rotate_salt).",
        )
    entry = ledger.entry(scope, key)
    if entry is not None and fraction < entry.fraction:
        everyone = (
            " Every use case on the universal holdout holds out the same share."
            if scope == "universal"
            else ""
        )
        raise HoldoutError(
            HOLDOUT_FRACTION_LOWERED,
            f"The holdout for {key} is {entry.fraction:.0%} in its current epoch ({entry.epoch}) and this "
            f"use case now asks for {fraction:.0%}. Lowering it would contact customers who were held out, "
            f"so results before and after could not be added up.{everyone} Ask an Admin to start a new "
            "holdout epoch at the lower share (PUT /holdout), or set the share back.",
        )
    if scope == "universal" and entry is not None and fraction > entry.fraction:
        raise HoldoutError(
            HOLDOUT_FRACTION_MISMATCH,
            f"The universal holdout is {entry.fraction:.0%} in its current epoch ({entry.epoch}) and this "
            f"use case asks for {fraction:.0%}. Every use case on the universal holdout holds out the same "
            "share, or one would contact customers another holds out. Set actions.holdout.fraction to "
            f"{entry.fraction:.0%}, or set every universal use case to the new share and ask an Admin to "
            "start a new epoch at it (PUT /holdout).",
        )
    if record:
        if stored is None:
            ledger.put_fingerprint(fingerprint, at=at)
        if entry is None:
            entry = HoldoutLedgerEntry(
                scope=scope,
                scope_key=key,
                epoch=1,
                fraction=fraction,
                salt_id=salt_id(fingerprint),
                started_at=at,
                updated_at=at,
            )
            ledger.put_entry(entry)
        elif fraction > entry.fraction:
            entry = entry.model_copy(update={"fraction": fraction, "updated_at": at})
            ledger.put_entry(entry)
    epoch = 1 if entry is None else entry.epoch
    spec = HoldoutSpec(
        scope=scope,
        fraction=fraction,
        salt_id=salt_id(fingerprint),
        epoch=epoch,
        scope_key=key,
        explore_fraction=explore,
    )
    return ResolvedHoldout(spec, ActiveHoldout(salt=salt, scope_key=key, fraction=fraction))


def start_epoch(
    ledger: HoldoutLedger,
    *,
    scope: PersistentScope,
    key: str,
    fraction: float,
    settings: Settings,
    rotate_salt: bool,
    at: datetime,
) -> HoldoutLedgerEntry:
    """An Admin's new epoch: for one holdout at `fraction`, and with `rotate_salt` for every holdout.

    Rotating records the current salt's fingerprint and moves every other entry to its next epoch at
    its own fraction; it needs a recorded salt that differs from the configured one
    (`HOLDOUT_SALT_UNCHANGED` otherwise: nothing would be reshuffled). Without `rotate_salt` the salt
    must be the recorded one (`HOLDOUT_SALT_CHANGED`).
    """
    salt = configured_salt(settings)
    if salt is None:
        raise _missing_salt()
    fingerprint = salt_fingerprint(salt)
    stored = ledger.fingerprint()
    if rotate_salt and (stored is None or stored == fingerprint):
        from engine.settings import ENV_VARS

        detail = "the one already recorded" if stored is not None else "the first one any holdout uses"
        raise HoldoutError(
            HOLDOUT_SALT_UNCHANGED,
            f"The configured {ENV_VARS['holdout_salt']} is {detail}, so rotating would reshuffle nobody. "
            "Set the new salt first, or start the epoch without rotate_salt.",
        )
    if not rotate_salt and stored is not None and stored != fingerprint:
        raise HoldoutError(
            HOLDOUT_SALT_CHANGED,
            "The configured holdout salt is not the recorded one. Start the new epoch with rotate_salt "
            "to adopt it, or set the original salt again.",
        )
    if stored != fingerprint:
        ledger.put_fingerprint(fingerprint, at=at)
    short = salt_id(fingerprint)
    if rotate_salt:  # a recorded, different salt (checked above): every holdout starts again
        for other in ledger.entries():
            if (other.scope, other.scope_key) == (scope, key):
                continue
            ledger.put_entry(
                other.model_copy(
                    update={"epoch": other.epoch + 1, "salt_id": short, "started_at": at, "updated_at": at}
                )
            )
    current = ledger.entry(scope, key)
    entry = HoldoutLedgerEntry(
        scope=scope,
        scope_key=key,
        epoch=1 if current is None else current.epoch + 1,
        fraction=fraction,
        salt_id=short,
        started_at=at,
        updated_at=at,
    )
    ledger.put_entry(entry)
    return entry
