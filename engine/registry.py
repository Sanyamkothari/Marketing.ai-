"""The model registry: the `ModelRegistry` protocol, the champion policy and the SQL store.

`should_promote` is a pure function rather than a protocol method (DEC-028): a Phase 4 registry
stores facts, it does not own the policy.

Phase 4a splits this module in two along a seam that was already there. `SqlRegistryStore` is
every statement this registry makes, against whatever SQLAlchemy `Engine` it is handed;
`LocalModelRegistry` is that store plus the one decision Phase 1 made about *which* engine - a
SQLite file at a path, opened so the job threads can write to it. `engine/aws/postgres.py` makes
the other choice and hands over a Postgres engine, and `engine/aws/s3_registry.py` wraps the store
with the file publishing an S3 deployment needs. The method bodies did not change when they moved:
a champion swap has to be the same transaction on both backends, and the surest way to keep it so
is for there to be exactly one copy of it (DEC-338).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol, runtime_checkable

from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlalchemy.engine import Engine
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, create_engine, select

# `Metric` is `engine.config`'s enum, re-exported by `engine.contracts`; imported from its home
# module because mypy --strict does not follow implicit re-exports.
from engine.config import Metric
from engine.contracts import NO_CHAMPION_AT_DECISION, ModelStatus, ModelVersion
from engine.settings import DEFAULT_DATA_DIR as _DEFAULT_DATA_DIR
from engine.settings import ENV_VARS, settings
from engine.settings import REGISTRY_FILENAME as _REGISTRY_FILENAME
from engine.utils.time import utc_now

DATA_DIR_ENV_VAR: Final[str] = ENV_VARS["data_dir"]
# All three are defined in `engine.settings` and re-exported here, where `api.deps` and the
# registry's own tests already import them from.
DEFAULT_DATA_DIR: Final[str] = _DEFAULT_DATA_DIR
REGISTRY_FILENAME: Final[str] = _REGISTRY_FILENAME
MODEL_VERSION_TABLE: Final[str] = "model_version"
"""The one table `LocalModelRegistry` creates. `create_all` is scoped to it (DEC-340)."""

_PROMOTABLE: Final[frozenset[ModelStatus]] = frozenset({ModelStatus.CANDIDATE, ModelStatus.PENDING_APPROVAL})


class RegistryError(Exception):
    """A registry operation failed.

    `code` is one of MODEL_NOT_FOUND | DUPLICATE_MODEL_ID | INVALID_TRANSITION | METRIC_MISMATCH |
    CHAMPION_CHANGED. The last one is `approve` refusing to crown a version whose promotion decision
    was measured against a champion that no longer holds the title (DEC-047).
    """

    def __init__(self, code: str, message: str, *, model_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.model_id = model_id


@runtime_checkable
class ModelRegistry(Protocol):
    """Where model versions live; SQLite now, SageMaker Model Registry in Phase 4."""

    def register(self, version: ModelVersion) -> ModelVersion: ...

    def get(self, model_id: str) -> ModelVersion: ...

    def list_versions(self, use_case_id: str | None = None) -> tuple[ModelVersion, ...]: ...

    def get_champion(self, use_case_id: str) -> ModelVersion | None: ...

    def next_version(self, use_case_id: str) -> int: ...

    def approve(self, model_id: str, *, by: str) -> ModelVersion: ...

    def promote(self, model_id: str, *, by: str, note: str) -> ModelVersion: ...

    def archive(self, model_id: str, *, expected_status: ModelStatus | None = None) -> ModelVersion: ...


def should_promote(
    candidate: ModelVersion,
    champion: ModelVersion | None,
    min_improvement_pct: float,
    *,
    greater_is_better: bool,
) -> bool:
    """The champion rule of plan §6.3, as a pure function.

    PRECONDITION, and the whole point of DEC-044: `candidate.test_score` and `champion.test_score`
    must BOTH have been measured on the SAME held-out frame, on the same metric. The caller is
    responsible for re-scoring the incumbent on the challenger's test split and passing that
    re-measured number in; `register.build_model_version` does this through `ChampionScore`. Passing
    the champion's *stored* score, from whatever test split existed when it was trained, compares
    two numbers that were never comparable: different rows, possibly different preprocessing, and
    possibly a different population, so a challenger could win or lose for reasons unrelated to
    model quality. This function cannot detect that misuse, which is why the caller must not commit
    it.

    True when there is no champion yet. Otherwise the candidate must have been scored on the same
    metric (`RegistryError('METRIC_MISMATCH')` when it was not) and must beat the champion's score
    by at least `min_improvement_pct` percent of it; for metrics where lower is better (rmse, mae)
    the sign is flipped. Equality is enough when the rule is 0 %.
    """
    if champion is None:
        return True
    if candidate.metric != champion.metric:
        raise RegistryError(
            "METRIC_MISMATCH",
            f"Model {candidate.model_id} was scored on {candidate.metric} but the current champion "
            f"{champion.model_id} was scored on {champion.metric}; the two cannot be compared.",
            model_id=candidate.model_id,
        )
    delta = candidate.test_score - champion.test_score
    signed_delta = delta if greater_is_better else -delta
    if champion.test_score == 0.0:
        # A relative improvement over zero is undefined (DEC-035): any strict gain beats it, an equal
        # score does only when the rule is 0 %, and a worse candidate never does.
        return signed_delta > 0.0 or (signed_delta == 0.0 and min_improvement_pct <= 0.0)
    improvement_pct = signed_delta / abs(champion.test_score) * 100.0
    return improvement_pct >= min_improvement_pct


def aware_utc(moment: datetime) -> datetime:
    """The stored instant as UTC, whatever the driver handed back.

    Two backends answer this differently and both answers have to become the same value. SQLite has
    no timestamp type at all, so a read comes back naive and the offset has to be put back; it is
    UTC because `_utc` is the only way a value gets in. Postgres returns a `timestamptz` **in the
    session's time zone**, so on a server running `Asia/Kolkata` the same instant comes back at
    +05:30 - the right moment, wearing the server's clock. Attaching UTC to that would be wrong and
    leaving it alone would publish the server's time zone into `ModelVersion.created_at`, which the
    contract says is UTC. Converting covers both: naive means UTC, aware is moved to UTC (DEC-339).
    """
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _aware_or_none(moment: datetime | None) -> datetime | None:
    """`aware_utc` for the optional timestamp columns."""
    return None if moment is None else aware_utc(moment)


def to_utc(moment: datetime) -> datetime:
    """Normalise to UTC before storing, so a naive read is still the right instant.

    The write-side counterpart of `aware_utc`, and the reason SQLite is safe: SQLite has no
    timestamp type and stores whatever it is handed, so a value that arrived at +05:30 would be
    read back as 17:30 UTC - the wrong instant - unless it was converted here first. Public because
    `engine/aws/postgres.py`'s run index has to store its timestamps the same way (DEC-339).
    """
    return moment.astimezone(UTC) if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def to_utc_or_none(moment: datetime | None) -> datetime | None:
    """`to_utc` for the optional timestamp columns."""
    return None if moment is None else to_utc(moment)


class ModelVersionRow(SQLModel, table=True):
    """One row per model version; the SQL projection of `engine.contracts.ModelVersion`.

    Every timestamp column is declared `DateTime(timezone=True)` through an explicit `sa_column`.
    Without it SQLModel compiles `datetime` to `TIMESTAMP WITHOUT TIME ZONE` on Postgres, and a
    driver handing an aware UTC value to such a column stores the *session's* wall clock instead of
    the instant: on a server set to `Asia/Kolkata` a run registered at 12:00Z read back as 17:30Z,
    five and a half hours into the future, silently and only on that server (DEC-339).

    `sa_column` is mutually exclusive with SQLField's `index=` and `primary_key=`, and a `Column`
    object belongs to exactly one table - neither is a constraint this row runs into, because none
    of its timestamps is indexed and each one gets its own `Column`.
    """

    __tablename__ = MODEL_VERSION_TABLE
    __table_args__: ClassVar[Any] = (UniqueConstraint("use_case_id", "version", name="uq_use_case_version"),)

    model_id: str = SQLField(primary_key=True)
    use_case_id: str = SQLField(index=True)
    version: int
    run_id: str = SQLField(index=True)
    created_at: datetime = SQLField(sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    status: str = SQLField(index=True)
    metric: str
    metric_label: str
    test_score: float
    validation_score: float | None = None
    model_display_name: str
    schema_key: str
    run_config_key: str
    predictor_key: str
    drift_baseline_key: str | None = None
    artefact_keys_json: str = "{}"
    approved_by: str | None = None
    approved_at: datetime | None = SQLField(
        default=None, sa_column=Column("approved_at", DateTime(timezone=True), nullable=True)
    )
    promoted_at: datetime | None = SQLField(
        default=None, sa_column=Column("promoted_at", DateTime(timezone=True), nullable=True)
    )
    promoted_by: str | None = None
    promotion_note: str | None = None
    previous_champion_id: str | None = None
    improvement_pct: float | None = None
    measured_against_champion_id: str | None = None
    engine_version: str
    autogluon_version: str

    def to_contract(self) -> ModelVersion:
        """The contract model this row stores."""
        artefact_keys: dict[str, str] = json.loads(self.artefact_keys_json)
        return ModelVersion(
            model_id=self.model_id,
            use_case_id=self.use_case_id,
            version=self.version,
            run_id=self.run_id,
            created_at=aware_utc(self.created_at),
            status=ModelStatus(self.status),
            metric=Metric(self.metric),
            metric_label=self.metric_label,
            test_score=self.test_score,
            validation_score=self.validation_score,
            model_display_name=self.model_display_name,
            schema_key=self.schema_key,
            run_config_key=self.run_config_key,
            predictor_key=self.predictor_key,
            drift_baseline_key=self.drift_baseline_key,
            artefact_keys=artefact_keys,
            approved_by=self.approved_by,
            approved_at=_aware_or_none(self.approved_at),
            promoted_at=_aware_or_none(self.promoted_at),
            promoted_by=self.promoted_by,
            promotion_note=self.promotion_note,
            previous_champion_id=self.previous_champion_id,
            improvement_pct=self.improvement_pct,
            measured_against_champion_id=self.measured_against_champion_id,
            engine_version=self.engine_version,
            autogluon_version=self.autogluon_version,
        )

    @classmethod
    def from_contract(cls, v: ModelVersion) -> ModelVersionRow:
        """The row that stores `v`."""
        return cls(
            model_id=v.model_id,
            use_case_id=v.use_case_id,
            version=v.version,
            run_id=v.run_id,
            created_at=to_utc(v.created_at),
            status=str(v.status),
            metric=str(v.metric),
            metric_label=v.metric_label,
            test_score=v.test_score,
            validation_score=v.validation_score,
            model_display_name=v.model_display_name,
            schema_key=v.schema_key,
            run_config_key=v.run_config_key,
            predictor_key=v.predictor_key,
            drift_baseline_key=v.drift_baseline_key,
            artefact_keys_json=json.dumps(dict(v.artefact_keys), sort_keys=True),
            approved_by=v.approved_by,
            approved_at=to_utc_or_none(v.approved_at),
            promoted_at=to_utc_or_none(v.promoted_at),
            promoted_by=v.promoted_by,
            promotion_note=v.promotion_note,
            previous_champion_id=v.previous_champion_id,
            improvement_pct=v.improvement_pct,
            measured_against_champion_id=v.measured_against_champion_id,
            engine_version=v.engine_version,
            autogluon_version=v.autogluon_version,
        )


def create_registry_tables(engine: Engine) -> None:
    """Create `model_version` if it is missing, and nothing else.

    `SQLModel.metadata` is one global namespace shared by every `table=True` class the process has
    imported, so an unscoped `create_all` creates whatever happens to be in it. Phase 4a puts a
    second table in that namespace - `engine/aws/run_index.py`'s run index - and an unscoped call
    here would conjure it into every `registry.db` the moment anything imported that module, on a
    deployment that had deliberately not asked for it. The table list is therefore explicit
    (DEC-340). Postgres does not go through here at all: Alembic owns that schema (DEC-341).
    """
    SQLModel.metadata.create_all(engine, tables=[SQLModel.metadata.tables[MODEL_VERSION_TABLE]])


class SqlRegistryStore:
    """Every statement the registry makes, against whatever `Engine` it is given.

    One session per call and one transaction per champion swap: `_make_champion` demotes the
    incumbent and crowns the challenger inside a single `commit`, so no reader can ever see a use
    case with two champions or none. A `threading.Lock` serialises the writes, which is what lets
    `ThreadJobRunner` workers register from several threads at once.

    This class does not create tables and does not know what an engine is connected to. That is the
    whole point of the split: `LocalModelRegistry` below chooses SQLite and calls
    `create_registry_tables`, `engine/aws/postgres.py` chooses Postgres and leaves the schema to
    Alembic, and both get the same transactions (DEC-338).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._lock = threading.Lock()

    @property
    def engine(self) -> Engine:
        """The engine every statement runs on; `engine/aws/s3_registry.py` needs nothing else."""
        return self._engine

    def register(self, version: ModelVersion) -> ModelVersion:
        """Store a new model version; raises `DUPLICATE_MODEL_ID` when the id is taken."""
        with self._lock, Session(self._engine) as session:
            if session.get(ModelVersionRow, version.model_id) is not None:
                raise RegistryError(
                    "DUPLICATE_MODEL_ID",
                    f"Model {version.model_id} is already registered.",
                    model_id=version.model_id,
                )
            row = ModelVersionRow.from_contract(version)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.to_contract()

    def get(self, model_id: str) -> ModelVersion:
        """One model version; raises `MODEL_NOT_FOUND` when the id is unknown."""
        with Session(self._engine) as session:
            return self._require(session, model_id).to_contract()

    def list_versions(self, use_case_id: str | None = None) -> tuple[ModelVersion, ...]:
        """Every version, newest first, optionally restricted to one use case."""
        with Session(self._engine) as session:
            statement = select(ModelVersionRow)
            if use_case_id is not None:
                statement = statement.where(col(ModelVersionRow.use_case_id) == use_case_id)
            statement = statement.order_by(
                col(ModelVersionRow.created_at).desc(),
                col(ModelVersionRow.version).desc(),
                col(ModelVersionRow.model_id).desc(),
            )
            rows: Sequence[ModelVersionRow] = session.exec(statement).all()
            return tuple(row.to_contract() for row in rows)

    def get_champion(self, use_case_id: str) -> ModelVersion | None:
        """The current champion of a use case, or None while there is none."""
        with Session(self._engine) as session:
            row = self._champion_row(session, use_case_id)
            return None if row is None else row.to_contract()

    def next_version(self, use_case_id: str) -> int:
        """The version number the next model of this use case gets (1 when there is none)."""
        with Session(self._engine) as session:
            statement = (
                select(ModelVersionRow)
                .where(col(ModelVersionRow.use_case_id) == use_case_id)
                .order_by(col(ModelVersionRow.version).desc())
                .limit(1)
            )
            row = session.exec(statement).first()
            return 1 if row is None else row.version + 1

    def approve(self, model_id: str, *, by: str) -> ModelVersion:
        """Promote a `pending_approval` version to champion (plan §8); other states are rejected.

        Approval signs off a decision the champion rule already took, so it may only be signed off
        against the champion that decision was taken against (DEC-047). A version records that
        champion in `measured_against_champion_id` when the register stage marks it
        `pending_approval`; if the use case has a different champion by the time a human gets to
        it - another version was approved or promoted meanwhile, or the incumbent was archived -
        this version's win was never measured against the model it would now replace, and
        approving it would decide the championship on a stale comparison. That is what plan §6.3's
        "beats the champion" forbids, so approval is refused with `CHAMPION_CHANGED` and the
        message says what to do instead. `promote` remains the deliberate, recorded override.

        A version whose `measured_against_champion_id` is null was registered before the field
        existed and says nothing about what it was compared with; it is approved without the check,
        exactly as it would have been then.
        """
        now = utc_now()
        with self._lock, Session(self._engine) as session:
            row = self._require(session, model_id)
            if row.status != ModelStatus.PENDING_APPROVAL:
                raise RegistryError(
                    "INVALID_TRANSITION",
                    f"Model {model_id} is {row.status}; only a model waiting for approval can be approved.",
                    model_id=model_id,
                )
            self._require_unchanged_champion(session, row)
            row.approved_by = by
            row.approved_at = now
            self._make_champion(session, row, by=by, note=None, when=now)
            session.commit()
            session.refresh(row)
            return row.to_contract()

    def promote(self, model_id: str, *, by: str, note: str) -> ModelVersion:
        """Manual override (plan §8): make a candidate or pending version the champion, recording why."""
        now = utc_now()
        with self._lock, Session(self._engine) as session:
            row = self._require(session, model_id)
            if row.status not in {str(status) for status in _PROMOTABLE}:
                raise RegistryError(
                    "INVALID_TRANSITION",
                    f"Model {model_id} is {row.status}; only a candidate or a model waiting for "
                    "approval can be promoted.",
                    model_id=model_id,
                )
            self._make_champion(session, row, by=by, note=note, when=now)
            session.commit()
            session.refresh(row)
            return row.to_contract()

    def archive(self, model_id: str, *, expected_status: ModelStatus | None = None) -> ModelVersion:
        """Retire a version; archiving an archived version is a no-op.

        `expected_status`, when given, is checked inside the same lock and session as the write:
        a version whose status is anything else is refused with `INVALID_TRANSITION` and left as it
        is. Rejecting a challenger passes `PENDING_APPROVAL`, so an approval that crowned it a
        moment earlier cannot be undone by the reject that read it as still waiting - which would
        archive the champion and leave the use case with none (DEC-873).
        """
        with self._lock, Session(self._engine) as session:
            row = self._require(session, model_id)
            if expected_status is not None and row.status != expected_status:
                raise RegistryError(
                    "INVALID_TRANSITION",
                    f"Model {model_id} is {row.status}, not {expected_status}; it was not archived.",
                    model_id=model_id,
                )
            row.status = str(ModelStatus.ARCHIVED)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.to_contract()

    def _require(self, session: Session, model_id: str) -> ModelVersionRow:
        row = session.get(ModelVersionRow, model_id)
        if row is None:
            raise RegistryError("MODEL_NOT_FOUND", f"No model version {model_id!r}.", model_id=model_id)
        return row

    def _require_unchanged_champion(self, session: Session, row: ModelVersionRow) -> None:
        """Raise `CHAMPION_CHANGED` when `row`'s decision was measured against another champion."""
        measured_against = row.measured_against_champion_id
        if measured_against is None:
            return
        expected = None if measured_against == NO_CHAMPION_AT_DECISION else measured_against
        current = self._champion_row(session, row.use_case_id)
        current_id = None if current is None else current.model_id
        if current_id == expected:
            return
        raise RegistryError(
            "CHAMPION_CHANGED",
            _stale_approval_message(row, expected=expected, current_id=current_id),
            model_id=row.model_id,
        )

    def _champion_row(self, session: Session, use_case_id: str) -> ModelVersionRow | None:
        statement = (
            select(ModelVersionRow)
            .where(col(ModelVersionRow.use_case_id) == use_case_id)
            .where(col(ModelVersionRow.status) == str(ModelStatus.CHAMPION))
            .order_by(col(ModelVersionRow.version).desc())
        )
        return session.exec(statement).first()

    def _make_champion(
        self,
        session: Session,
        row: ModelVersionRow,
        *,
        by: str,
        note: str | None,
        when: datetime,
    ) -> None:
        """Demote the incumbent and crown `row` inside the caller's single transaction."""
        previous = self._champion_row(session, row.use_case_id)
        if previous is not None and previous.model_id != row.model_id:
            previous.status = str(ModelStatus.ARCHIVED)
            session.add(previous)
            row.previous_champion_id = previous.model_id
        row.status = str(ModelStatus.CHAMPION)
        row.promoted_at = when
        row.promoted_by = by
        if note is not None:
            row.promotion_note = note
        session.add(row)


class LocalModelRegistry(SqlRegistryStore):
    """`SqlRegistryStore` over a SQLite file: what Phase 1 called the registry, unchanged.

    The engine is created with `check_same_thread=False` so `ThreadJobRunner` workers can write, and
    `create_registry_tables` is idempotent, so pointing two instances at the same file is safe.

    Creating a table is not migrating one: it adds a missing table, never a missing column, and the
    local path has no migration tool. A `registry.db` written before a column was added to
    `ModelVersionRow` still has to be recreated. That is a local development file, not client data
    (plan §1.3), so the cost is one deleted file - and it is exactly the reason Postgres does not
    work this way (DEC-341).
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(
            create_engine(f"sqlite:///{self._db_path}", connect_args={"check_same_thread": False})
        )
        create_registry_tables(self.engine)

    @property
    def db_path(self) -> Path:
        """The SQLite file this registry writes to."""
        return self._db_path


def _stale_approval_message(row: ModelVersionRow, *, expected: str | None, current_id: str | None) -> str:
    """Why this approval was refused and what the user can do instead, in business language."""
    measured = "no champion at all" if expected is None else f"champion {expected}"
    standing = "there is no champion now" if current_id is None else f"{current_id} is the champion now"
    return (
        f"Model {row.model_id} was put forward for approval against {measured}, but {standing}. "
        f"Approving it would make it the champion of {row.use_case_id} on the strength of a "
        "comparison it never had with the model it would replace. Train a new version, so it is "
        "measured against the champion that actually holds the title, and approve that one; or, if "
        f"you have looked at both models yourself, POST /models/{row.model_id}/promote overrides "
        "the champion rule and records who decided and why."
    )


def default_registry() -> LocalModelRegistry:
    """The process-wide default registry: `$MARKETING_AI_DATA_DIR/registry.db` (or `data/registry.db`).

    Read through `engine.settings`, which also carries `metadata_backend`; it is `sqlite` until
    Phase 4a implements `PostgresMetadata`, so this factory does not yet branch on it.
    """
    return LocalModelRegistry(settings().registry_path)
