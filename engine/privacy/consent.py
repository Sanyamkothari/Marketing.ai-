"""The consent ledger (M48 item 1) and the seam that lets it gate a scoring run.

**The ledger.** One row per consent event - a principal granted or withdrew consent to one purpose
at one moment, from one source, optionally until an expiry - in the platform database's
`consent_record` table. Rows are only ever added: a change of mind is a new row, and **the latest
row as of a moment decides** (ties at the same instant go to the row written later). A principal is
validly consented at `at` when that latest row is a grant whose expiry, if any, is after `at`. No row
at all means no consent: an unrecorded consent is not a consent, the rule Phase 1's actions stage
already applies to a null in the consent column (DEC-731).

**No raw ids.** The ledger stores `principal_hash(id, salt=privacy_salt(settings))` and hashes the ids
a lookup asks about, so it never becomes a second copy of the customer list and an erasure can leave
the consent evidence behind without leaving the id behind (DEC-733).

**Bulk import** reads a CSV (`principal_id, purpose, status, recorded_at` required; `source`,
`expires_at` optional), validates every row, and reports problems by *row number and column*, never
by value - a consent file is personal data and an error message is shown, logged and screenshotted.
The import is all or nothing unless `partial=True`: half a consent file loaded silently is a ledger
nobody can reason about (DEC-734).

**The scoring seam.** Phase 1 already suppresses a row whose `governance.consent_column` is not
truthy (`consent_false`, first in `engine.stages.actions` precedence) and counts it in
`scoring_summary.json`. When a ledger exists for the run's client and the use case's purpose,
`apply_consent_gate` writes the ledger's verdict into that column - or, for a use case with no
consent column, into a synthetic one the export never writes - and hands actions a configuration
that names it. The existing rule then suppresses exactly the principals without valid consent, its
existing counts report them, and `consent_report.json` says how many were missing, withdrawn and
expired. No ledger, no purpose, or no client id: nothing here runs, and the run is Phase 1's
byte for byte (DEC-732). Training is untouched on purpose: the plan gates *scoring and actions for a
purpose*, and model fitting is not contacting anyone.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import delete, inspect
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from engine.audit.events import principal_hash
from engine.platform_db import PLATFORM_DB_FILENAME, platform_engine, sqlite_engine
from engine.privacy.config import PrivacyConfig, privacy_config_or_none, privacy_salt
from engine.privacy.contracts import (
    ConsentImportError,
    ConsentImportReport,
    ConsentRecord,
    ConsentReport,
    ConsentStatus,
)
from engine.privacy.tables import CONSENT_RECORD_TABLE, ConsentRecordRow, create_privacy_tables
from engine.registry import aware_utc, to_utc
from engine.settings import Settings, load_settings
from engine.storage import LocalStorage, Storage
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from pathlib import Path

    import pandas as pd

    from engine.config import UseCaseConfig

__all__ = [
    "CONSENT_CSV_OPTIONAL",
    "CONSENT_CSV_REQUIRED",
    "LEDGER_CONSENT_COLUMN",
    "ConsentClassification",
    "ConsentGate",
    "ConsentLedger",
    "apply_consent_gate",
    "consent_gate_for_run",
    "ledger_engine_for",
    "principal_key",
]

_LOGGER = get_logger(__name__)

LEDGER_CONSENT_COLUMN: Final[str] = "__consent_ledger__"
"""The column the ledger verdict goes into when the use case configures no consent column.

Never exported: `scores.csv` carries `scores_csv_columns(...)` and nothing else, so this name exists
only between the actions stage and the export stage of one run."""

CONSENT_CSV_REQUIRED: Final[tuple[str, ...]] = ("principal_id", "purpose", "status", "recorded_at")
CONSENT_CSV_OPTIONAL: Final[tuple[str, ...]] = ("source", "expires_at")
DEFAULT_IMPORT_SOURCE: Final[str] = "csv_import"
MAX_PRINCIPAL_ID_CHARS: Final[int] = 256
FUTURE_TOLERANCE: Final[timedelta] = timedelta(minutes=5)
"""A record may be stamped this far in the future (clock skew) and no further."""

_IN_CHUNK: Final[int] = 500
"""Hashes per `IN (...)` lookup: under SQLite's bound-parameter limit with room to spare."""


_DIGIT_KEY: Final[re.Pattern[str]] = re.compile(r"[0-9]+(?:\.0+)?")
"""A key made only of digits (optionally `.0`): the form ingest turns into a number (DEC-737)."""


def principal_key(value: object) -> str:
    """A primary-key cell as the ledger and erasure compare it (DEC-737).

    Stripped text; `1024.0` read as `1024`; and a key made only of digits read as the number it is,
    so `00104` is `104`. Ingest reads a CSV with inferred types, so a zero-padded key column becomes
    an integer column and every artefact downstream of it - scores, explanations, the frame the
    consent gate sees - carries `104`. The id a client knows is `00104`. Comparing both through this
    one function is what makes an erasure of `00104` reach the scores and a consent granted to
    `00104` reach the gate.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        if value.is_integer():
            return str(int(value))
    text = str(value).strip()
    if _DIGIT_KEY.fullmatch(text):
        return str(int(text.split(".", 1)[0]))
    return text


@dataclass(frozen=True)
class ConsentClassification:
    """Every looked-up principal in exactly one bucket, as of one moment."""

    valid: frozenset[str]
    withdrawn: frozenset[str]
    expired: frozenset[str]
    missing: frozenset[str]


class ConsentLedger:
    """The consent ledger of one deployment, over the platform database.

    `salt` is `privacy_salt(settings)`; every principal id is hashed with it on the way in and on
    every lookup. `create=False` is for readers that must not create a table as a side effect - the
    scoring seam, which checks for the table instead.
    """

    def __init__(self, engine: Engine, *, salt: str, create: bool = True) -> None:
        self._engine = engine
        self._salt = salt
        if create:
            create_privacy_tables(engine)

    @property
    def engine(self) -> Engine:
        """The database this ledger reads and writes."""
        return self._engine

    def hash(self, principal_id: str) -> str:
        """The ledger's key for a principal id."""
        return principal_hash(principal_key(principal_id), salt=self._salt)

    # -- writes -------------------------------------------------------------
    def record(
        self,
        *,
        client_id: str,
        principal_id: str,
        purpose: str,
        status: ConsentStatus,
        source: str,
        recorded_at: datetime,
        expires_at: datetime | None = None,
    ) -> ConsentRecord:
        """Append one record and return it. The caller has validated `purpose` against the config."""
        row = ConsentRecordRow(
            client_id=client_id,
            principal_hash=self.hash(principal_id),
            purpose=purpose,
            status=status.value,
            source=source,
            recorded_at=to_utc(recorded_at),
            expires_at=None if expires_at is None else to_utc(expires_at),
            created_at=utc_now(),
        )
        with Session(self._engine) as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return _to_contract(row)

    def import_csv(
        self,
        data: bytes | str,
        *,
        client_id: str,
        privacy: PrivacyConfig,
        partial: bool = False,
        now: datetime | None = None,
    ) -> ConsentImportReport:
        """Validate a consent CSV and, when it is clean (or `partial`), append its rows.

        Row numbers count the header as row 1, so row 2 is the first data row - the number a person
        sees in a spreadsheet. No message carries a cell value.
        """
        moment = now or utc_now()
        text = data.decode("utf-8-sig") if isinstance(data, bytes) else data.lstrip("﻿")
        reader = csv.reader(io.StringIO(text))
        header = [name.strip() for name in next(reader, [])]
        errors: list[ConsentImportError] = []
        missing = [name for name in CONSENT_CSV_REQUIRED if name not in header]
        if missing:
            errors.append(
                ConsentImportError(
                    row=1,
                    code="CONSENT_CSV_COLUMNS_MISSING",
                    message=f"The file must have the columns {', '.join(CONSENT_CSV_REQUIRED)}; "
                    f"missing: {', '.join(missing)}.",
                )
            )
            return ConsentImportReport(
                client_id=client_id, rows_read=0, rows_imported=0, errors=tuple(errors), imported=False
            )
        known = set(CONSENT_CSV_REQUIRED) | set(CONSENT_CSV_OPTIONAL)
        ignored = tuple(name for name in header if name not in known)
        position = {name: header.index(name) for name in header if name in known}
        rows: list[ConsentRecordRow] = []
        rows_read = 0
        for number, cells in enumerate(reader, start=2):
            if not any(cell.strip() for cell in cells):
                continue
            rows_read += 1
            parsed = _parse_row(cells, number, position, len(header), privacy, moment, errors)
            if parsed is None:
                continue
            principal, purpose, status, source, recorded_at, expires_at = parsed
            rows.append(
                ConsentRecordRow(
                    client_id=client_id,
                    principal_hash=self.hash(principal),
                    purpose=purpose,
                    status=status.value,
                    source=source,
                    recorded_at=recorded_at,
                    expires_at=expires_at,
                    created_at=moment,
                )
            )
        write = bool(rows) and (partial or not errors)
        if write:
            with Session(self._engine) as session:
                session.add_all(rows)
                session.commit()
        _LOGGER.info(
            "consent.import client=%s rows_read=%d rows_imported=%d errors=%d",
            client_id,
            rows_read,
            len(rows) if write else 0,
            len(errors),
        )
        return ConsentImportReport(
            client_id=client_id,
            rows_read=rows_read,
            rows_imported=len(rows) if write else 0,
            errors=tuple(errors),
            ignored_columns=ignored,
            imported=write,
        )

    def delete_history(self, principal_id: str, *, client_id: str | None = None) -> int:
        """Remove every record of one principal (erasure with `consent_history: delete`)."""
        statement = delete(ConsentRecordRow).where(
            col(ConsentRecordRow.principal_hash) == self.hash(principal_id)
        )
        if client_id is not None:
            statement = statement.where(col(ConsentRecordRow.client_id) == client_id)
        with Session(self._engine) as session:
            result = session.exec(statement)
            session.commit()
            return int(result.rowcount or 0)

    # -- reads ----------------------------------------------------------------
    def has_ledger(self, client_id: str, purpose: str) -> bool:
        """Whether this client has recorded any consent for `purpose` - the switch of DEC-732."""
        with Session(self._engine) as session:
            statement = (
                select(ConsentRecordRow.seq)
                .where(col(ConsentRecordRow.client_id) == client_id)
                .where(col(ConsentRecordRow.purpose) == purpose)
                .limit(1)
            )
            return session.exec(statement).first() is not None

    def purpose_has_ledger(self, purpose: str) -> bool:
        """Whether any client of this deployment has recorded consent for `purpose` (DEC-738)."""
        with Session(self._engine) as session:
            statement = select(ConsentRecordRow.seq).where(col(ConsentRecordRow.purpose) == purpose).limit(1)
            return session.exec(statement).first() is not None

    def valid_consent(
        self, client_id: str, purpose: str, principal_ids: Iterable[str], at: datetime
    ) -> set[str]:
        """The principal ids, of those given, validly consented to `purpose` at `at`."""
        return set(self.classify(client_id, purpose, principal_ids, at).valid)

    def classify(
        self, client_id: str, purpose: str, principal_ids: Iterable[str], at: datetime
    ) -> ConsentClassification:
        """Every given id in one of four buckets: valid, withdrawn, expired or no record."""
        moment = to_utc(at)
        ids = {principal_key(value) for value in principal_ids}
        by_hash: dict[str, list[str]] = {}
        for principal in ids:
            by_hash.setdefault(self.hash(principal), []).append(principal)
        latest = self._latest(client_id, purpose, list(by_hash), moment)
        valid: set[str] = set()
        withdrawn: set[str] = set()
        expired: set[str] = set()
        missing: set[str] = set()
        for hashed, principals in by_hash.items():
            row = latest.get(hashed)
            if row is None or principals == [""]:  # an empty key is never a principal
                missing.update(principals)
            elif row.status != ConsentStatus.GRANTED.value:
                withdrawn.update(principals)
            elif row.expires_at is not None and aware_utc(row.expires_at) <= moment:
                expired.update(principals)
            else:
                valid.update(principals)
        return ConsentClassification(
            valid=frozenset(valid),
            withdrawn=frozenset(withdrawn),
            expired=frozenset(expired),
            missing=frozenset(missing),
        )

    def history(self, principal_id: str, *, client_id: str | None = None) -> tuple[ConsentRecord, ...]:
        """Every record of one principal, oldest first (the access export includes these)."""
        statement = select(ConsentRecordRow).where(
            col(ConsentRecordRow.principal_hash) == self.hash(principal_id)
        )
        if client_id is not None:
            statement = statement.where(col(ConsentRecordRow.client_id) == client_id)
        statement = statement.order_by(col(ConsentRecordRow.recorded_at), col(ConsentRecordRow.seq))
        with Session(self._engine) as session:
            return tuple(_to_contract(row) for row in session.exec(statement).all())

    def _latest(
        self, client_id: str, purpose: str, hashes: list[str], at: datetime
    ) -> dict[str, ConsentRecordRow]:
        """The deciding row per hash: the latest `recorded_at <= at`, ties to the higher `seq`."""
        latest: dict[str, ConsentRecordRow] = {}
        with Session(self._engine) as session:
            for start in range(0, len(hashes), _IN_CHUNK):
                chunk = hashes[start : start + _IN_CHUNK]
                statement = (
                    select(ConsentRecordRow)
                    .where(col(ConsentRecordRow.client_id) == client_id)
                    .where(col(ConsentRecordRow.purpose) == purpose)
                    .where(col(ConsentRecordRow.principal_hash).in_(chunk))
                )
                for row in session.exec(statement).all():
                    if aware_utc(row.recorded_at) > at:
                        continue
                    current = latest.get(row.principal_hash)
                    if current is None or _order(row) > _order(current):
                        latest[row.principal_hash] = row
        return latest


def _order(row: ConsentRecordRow) -> tuple[datetime, int]:
    return aware_utc(row.recorded_at), row.seq or 0


def _to_contract(row: ConsentRecordRow) -> ConsentRecord:
    return ConsentRecord(
        seq=row.seq or 0,
        client_id=row.client_id,
        principal_hash=row.principal_hash,
        purpose=row.purpose,
        status=ConsentStatus(row.status),
        source=row.source,
        recorded_at=aware_utc(row.recorded_at),
        expires_at=None if row.expires_at is None else aware_utc(row.expires_at),
        created_at=aware_utc(row.created_at),
    )


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------
def _parse_row(
    cells: list[str],
    number: int,
    position: Mapping[str, int],
    width: int,
    privacy: PrivacyConfig,
    now: datetime,
    errors: list[ConsentImportError],
) -> tuple[str, str, ConsentStatus, str, datetime, datetime | None] | None:
    """One data row, or None after appending its errors. Messages never quote a cell."""
    before = len(errors)
    if len(cells) != width:
        errors.append(
            ConsentImportError(
                row=number,
                code="CONSENT_ROW_SHAPE",
                message=f"Row {number} has {len(cells)} cells; the header has {width}.",
            )
        )
        return None

    def cell(name: str) -> str:
        index = position.get(name)
        return "" if index is None else cells[index].strip()

    def fail(column: str, code: str, message: str) -> None:
        errors.append(ConsentImportError(row=number, column=column, code=code, message=message))

    principal = cell("principal_id")
    if not principal:
        fail("principal_id", "CONSENT_PRINCIPAL_MISSING", f"Row {number} has no principal_id.")
    elif len(principal) > MAX_PRINCIPAL_ID_CHARS:
        fail(
            "principal_id",
            "CONSENT_PRINCIPAL_TOO_LONG",
            f"Row {number}: principal_id is longer than {MAX_PRINCIPAL_ID_CHARS} characters.",
        )
    purpose = cell("purpose")
    if not privacy.is_purpose(purpose):
        fail(
            "purpose",
            "CONSENT_PURPOSE_UNKNOWN",
            f"Row {number}: purpose is not one of {', '.join(sorted(privacy.purposes))}.",
        )
    raw_status = cell("status").lower()
    status = ConsentStatus(raw_status) if raw_status in {s.value for s in ConsentStatus} else None
    if status is None:
        fail("status", "CONSENT_STATUS_INVALID", f"Row {number}: status must be granted or withdrawn.")
    recorded_at = _parse_time(cell("recorded_at"))
    if recorded_at is None:
        fail(
            "recorded_at",
            "CONSENT_TIME_INVALID",
            f"Row {number}: recorded_at is not an ISO-8601 date or date-time.",
        )
    elif recorded_at > now + FUTURE_TOLERANCE:
        fail("recorded_at", "CONSENT_TIME_IN_FUTURE", f"Row {number}: recorded_at is in the future.")
    raw_expiry = cell("expires_at")
    expires_at = _parse_time(raw_expiry) if raw_expiry else None
    if raw_expiry and expires_at is None:
        fail(
            "expires_at",
            "CONSENT_EXPIRY_INVALID",
            f"Row {number}: expires_at is not an ISO-8601 date or date-time.",
        )
    elif expires_at is not None and recorded_at is not None and expires_at <= recorded_at:
        fail(
            "expires_at",
            "CONSENT_EXPIRY_BEFORE_RECORDED",
            f"Row {number}: expires_at is not after recorded_at.",
        )
    source = cell("source") or DEFAULT_IMPORT_SOURCE
    if len(errors) > before or status is None or recorded_at is None:
        return None
    return principal, purpose, status, source, recorded_at, expires_at


def _parse_time(text: str) -> datetime | None:
    """An ISO date (midnight UTC) or date-time; a naive time is read as UTC. None when unparseable."""
    if not text:
        return None
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return datetime(day.year, day.month, day.day, tzinfo=UTC)
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return to_utc(moment)


# ---------------------------------------------------------------------------
# The scoring seam (DEC-732)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConsentGate:
    """A ledger that applies to one scoring run: its client and its use case's purpose."""

    ledger: ConsentLedger
    client_id: str
    purpose: str


def ledger_engine_for(storage: Storage, config: Settings | None = None) -> Engine | None:
    """The platform database a run's ledger lives in, or None when there is none to consult.

    A local store's `platform.db` sits in its own root (DEC-706), and a file that does not exist is
    *not* created here: looking for a ledger must leave a Phase 1 data directory exactly as it was.
    A non-local store follows the settings: SQLite beside `data_dir`, or the registry's Postgres.
    A database without the `consent_record` table has no ledger either.

    With no `config`, the settings come from `load_settings()`, the loader the API uses, not the
    environment-only `settings()`: a deployment sets only `MARKETING_AI_SETTINGS_SOURCE=aws` and keeps
    `metadata_backend`, the DSN and `client_id` in Parameter Store, so reading the environment alone
    would find no ledger - and gate nothing - on exactly the deployments that need it (DEC-796).
    """
    current = config or load_settings()
    if current.metadata_backend == "sqlite":
        # The API's `platform_engine` puts SQLite beside the artefacts; a local store's own root is
        # that directory for the job. With Postgres metadata the ledger is in Postgres even when the
        # artefacts are on a local volume (docker-compose), exactly where the API wrote it (DEC-739).
        root = storage.root if isinstance(storage, LocalStorage) else current.data_dir
        path = root / PLATFORM_DB_FILENAME
        if not path.is_file():
            return None
        engine = sqlite_engine(path)
    else:
        engine = platform_engine(current)
    if not inspect(engine).has_table(CONSENT_RECORD_TABLE):
        return None
    return engine


def consent_gate_for_run(
    storage: Storage,
    *,
    use_case_id: str,
    client_id: str | None,
    config_root: Path | None = None,
    config: Settings | None = None,
) -> ConsentGate | None:
    """The ledger that gates this run, or None - in which case the run is Phase 1's exactly.

    None when: the config root has no `privacy.yaml`; the use case maps to no purpose; neither the
    run nor the deployment names a client; there is no platform database or no consent table; or
    **no client of the deployment** has recorded anything for the purpose (DEC-738).

    Which client's ledger: the run's own `client_id` (a dataset run, which includes every scheduled
    firing) when it has one for the purpose, else the deployment's `Settings.client_id` - the client
    the consent routes record under when the Admin leaves the field empty - so a ledger recorded
    under the deployment's id still gates a run of an onboarding client `c_<slug>_<n>`.

    **Fail closed.** When the deployment keeps a ledger for the purpose but neither of those clients
    has one, the gate still applies, to the run's client, whose empty ledger gives nobody valid
    consent: every row is suppressed and the consent report says why. Otherwise an Analyst could
    switch the Admin's control off by onboarding the same customers under a fresh client id.
    """
    privacy = privacy_config_or_none(config_root)
    if privacy is None:
        return None
    purpose = privacy.purpose_for(use_case_id)
    if purpose is None:
        return None
    current = config or load_settings()  # the API's loader, so both hash with one salt (DEC-796)
    clients = [client for client in dict.fromkeys((client_id, current.client_id)) if client]
    if not clients:
        return None
    engine = ledger_engine_for(storage, current)
    if engine is None:
        return None
    ledger = ConsentLedger(engine, salt=privacy_salt(current, engine=engine), create=False)
    for client in clients:
        if ledger.has_ledger(client, purpose):
            return ConsentGate(ledger=ledger, client_id=client, purpose=purpose)
    if ledger.purpose_has_ledger(purpose):
        _LOGGER.warning("consent.gate client has no ledger for purpose=%s; no row is contactable", purpose)
        return ConsentGate(ledger=ledger, client_id=clients[0], purpose=purpose)
    return None


def apply_consent_gate(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    gate: ConsentGate,
    *,
    primary_key: str,
    run_id: str,
    at: datetime,
) -> tuple[pd.DataFrame, UseCaseConfig, ConsentReport]:
    """The frame with the ledger's verdict in the consent column, the config naming it, and the report.

    The verdict is a real boolean per row, so `actions._truthy` reads it the way it reads any
    consent column. The configuration is copied, never mutated; when the use case already has a
    consent column the copy is the same object and the column becomes the ledger lookup - the
    plan's "the Phase 1 consent column becomes a lookup against this ledger" - **combined with the
    file's own value by AND** (DEC-738): a customer the client's file marks as not consenting today
    stays uncontactable even when the ledger holds an older grant, because the more restrictive of
    two consent records is the one that can be relied on.
    """
    keys = [principal_key(value) for value in frame[primary_key].tolist()]
    verdict = gate.ledger.classify(gate.client_id, gate.purpose, keys, at)
    column = config.governance.consent_column
    replaced = column is not None and column in frame.columns
    gated_config = config
    if column is None:
        column = LEDGER_CONSENT_COLUMN
        governance = config.governance.model_copy(update={"consent_column": column})
        gated_config = config.model_copy(update={"governance": governance})
    ledger_valid = [key in verdict.valid for key in keys]
    if replaced:
        from engine.stages.actions import _truthy  # the rule actions itself applies to the column

        in_file = [bool(value) for value in _truthy(frame[column]).tolist()]
    else:
        in_file = [True] * len(keys)
    final = [ledger and file for ledger, file in zip(ledger_valid, in_file, strict=True)]
    gated = frame.copy()
    gated[column] = final
    gated.attrs = dict(frame.attrs)
    rows = len(keys)
    valid_rows = sum(final)
    report = ConsentReport(
        run_id=run_id,
        use_case_id=config.id,
        client_id=gate.client_id,
        purpose=gate.purpose,
        ledger_as_of=to_utc(at),
        consent_column=column,
        replaced_file_column=replaced,
        principals_checked=rows,
        principals_with_valid_consent=sum(ledger_valid),
        excluded_no_consent=sum(1 for key in keys if key in verdict.missing),
        excluded_withdrawn=sum(1 for key in keys if key in verdict.withdrawn),
        excluded_expired=sum(1 for key in keys if key in verdict.expired),
        excluded_file_opt_out=sum(
            1 for ledger, file in zip(ledger_valid, in_file, strict=True) if ledger and not file
        ),
        excluded_total=rows - valid_rows,
        created_at=utc_now(),
    )
    _LOGGER.info(
        "consent.gate run=%s purpose=%s checked=%d valid=%d no_record=%d withdrawn=%d expired=%d",
        run_id,
        gate.purpose,
        rows,
        valid_rows,
        report.excluded_no_consent,
        report.excluded_withdrawn,
        report.excluded_expired,
    )
    return gated, gated_config, report
