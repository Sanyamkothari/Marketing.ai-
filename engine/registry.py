"""The model registry: the `ModelRegistry` protocol, the champion policy and a SQLite implementation.

`should_promote` is a pure function rather than a protocol method (DEC-028): a Phase 4 registry
stores facts, it does not own the policy.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol, runtime_checkable

from sqlalchemy import UniqueConstraint
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, create_engine, select

# `Metric` is `engine.config`'s enum, re-exported by `engine.contracts`; imported from its home
# module because mypy --strict does not follow implicit re-exports.
from engine.config import Metric
from engine.contracts import ModelStatus, ModelVersion
from engine.utils.time import utc_now

DATA_DIR_ENV_VAR: Final[str] = "MARKETING_AI_DATA_DIR"
DEFAULT_DATA_DIR: Final[str] = "data"
REGISTRY_FILENAME: Final[str] = "registry.db"

_PROMOTABLE: Final[frozenset[ModelStatus]] = frozenset({ModelStatus.CANDIDATE, ModelStatus.PENDING_APPROVAL})


class RegistryError(Exception):
    """A registry operation failed.

    `code` is one of MODEL_NOT_FOUND | DUPLICATE_MODEL_ID | INVALID_TRANSITION | METRIC_MISMATCH.
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

    def archive(self, model_id: str) -> ModelVersion: ...


def should_promote(
    candidate: ModelVersion,
    champion: ModelVersion | None,
    min_improvement_pct: float,
    *,
    greater_is_better: bool,
) -> bool:
    """The champion rule of plan §6.3, as a pure function.

    True when there is no champion yet. Otherwise the candidate must have been scored on the same
    metric (`RegistryError('METRIC_MISMATCH')` when it was not) and must beat the champion's test
    score by at least `min_improvement_pct` percent of the champion's score; for metrics where lower
    is better (rmse, mae) the sign is flipped. Equality is enough when the rule is 0 %.
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


def _aware(moment: datetime) -> datetime:
    """SQLite forgets the offset; every stored timestamp is UTC, so put the offset back."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _aware_or_none(moment: datetime | None) -> datetime | None:
    """`_aware` for the optional timestamp columns."""
    return None if moment is None else _aware(moment)


def _utc(moment: datetime) -> datetime:
    """Normalise to UTC before storing, so a naive read is still the right instant."""
    return moment.astimezone(UTC) if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _utc_or_none(moment: datetime | None) -> datetime | None:
    """`_utc` for the optional timestamp columns."""
    return None if moment is None else _utc(moment)


class ModelVersionRow(SQLModel, table=True):
    """One row per model version; the SQLite projection of `engine.contracts.ModelVersion`."""

    __tablename__ = "model_version"
    __table_args__: ClassVar[Any] = (UniqueConstraint("use_case_id", "version", name="uq_use_case_version"),)

    model_id: str = SQLField(primary_key=True)
    use_case_id: str = SQLField(index=True)
    version: int
    run_id: str = SQLField(index=True)
    created_at: datetime
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
    approved_at: datetime | None = None
    promoted_at: datetime | None = None
    promoted_by: str | None = None
    promotion_note: str | None = None
    previous_champion_id: str | None = None
    improvement_pct: float | None = None
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
            created_at=_aware(self.created_at),
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
            created_at=_utc(v.created_at),
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
            approved_at=_utc_or_none(v.approved_at),
            promoted_at=_utc_or_none(v.promoted_at),
            promoted_by=v.promoted_by,
            promotion_note=v.promotion_note,
            previous_champion_id=v.previous_champion_id,
            improvement_pct=v.improvement_pct,
            engine_version=v.engine_version,
            autogluon_version=v.autogluon_version,
        )


class LocalModelRegistry:
    """SQLModel/SQLite registry: one session per call, one transaction per champion swap.

    The engine is created with `check_same_thread=False` so `ThreadJobRunner` workers can write, and
    `create_all` is idempotent, so pointing two instances at the same file is safe.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._engine = create_engine(
            f"sqlite:///{self._db_path}",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self._engine)

    @property
    def db_path(self) -> Path:
        """The SQLite file this registry writes to."""
        return self._db_path

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
        """Promote a `pending_approval` version to champion (plan §8); other states are rejected."""
        now = utc_now()
        with self._lock, Session(self._engine) as session:
            row = self._require(session, model_id)
            if row.status != ModelStatus.PENDING_APPROVAL:
                raise RegistryError(
                    "INVALID_TRANSITION",
                    f"Model {model_id} is {row.status}; only a model waiting for approval can be approved.",
                    model_id=model_id,
                )
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

    def archive(self, model_id: str) -> ModelVersion:
        """Retire a version; archiving an archived version is a no-op."""
        with self._lock, Session(self._engine) as session:
            row = self._require(session, model_id)
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


def default_registry() -> LocalModelRegistry:
    """The process-wide default registry: `$MARKETING_AI_DATA_DIR/registry.db` (or `data/registry.db`)."""
    return LocalModelRegistry(Path(os.environ.get(DATA_DIR_ENV_VAR, DEFAULT_DATA_DIR)) / REGISTRY_FILENAME)
