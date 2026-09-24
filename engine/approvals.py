"""Approving, rejecting and promoting challengers (Plan D M54): the head-to-head and who may decide.

Phase 1 left approval as two API calls with a name the caller typed. This module is what the
Approver's screen (`#/approvals`) and the three decision routes stand on:

**The head-to-head is on the same held-out data** (DEC-044, DEC-864). A challenger's metrics are its
`evaluation.json`, measured on its own test split. The champion it was measured against was re-scored
on *that same split* by the train flow (`engine.pipeline._rescore_champion`), and since Plan D every
catalog metric of that re-score is kept in the run's `run_manifest.json` as `champion_<metric>` -
before, only the primary metric was, so an older run shows the primary metric alone and says so.
Nothing here re-scores anything: a decision screen that trained or scored on request would put
minutes of compute behind a click and could disagree with the number the champion rule used.
Nothing is estimated either: a metric without a champion value is shown without one.

**Separation of duties** (DEC-862). Whoever started the training run (`RunRecord.requested_by`)
cannot approve or promote its model; another Approver has to. It is enforced by the routes and shown
on the screen before anybody clicks. With sign-in off there is one operator holding every role, so it
cannot be enforced, and the screen says that rather than implying otherwise. A run recorded before
`requested_by` existed has no known trainer: the rule cannot be checked and the screen says so.

**Every decision is recorded with its reason** in `model_decision` (the platform database, migration
`0005_plan_d`): approved, rejected or promoted, by whom (a user id), against which champion, when.
The registry row keeps Phase 1's `approved_by` / `promoted_by`, now the signed-in username when
sign-in is on. Rejecting archives a `pending_approval` version; nothing else can be rejected.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Final, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, DateTime
from sqlalchemy.engine import Engine
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, select

from engine.access.roles import Principal
from engine.contracts import (
    NO_CHAMPION_AT_DECISION,
    EvaluationReport,
    ModelStatus,
    ModelVersion,
    RunManifest,
    RunRecord,
)
from engine.platform_db import create_tables
from engine.registry import ModelRegistry, aware_utc
from engine.storage import Storage, StorageError, run_key
from engine.utils.time import utc_now

__all__ = [
    "MODEL_DECISION_TABLE",
    "ApprovalItem",
    "DecisionKind",
    "HeadToHead",
    "HeadToHeadMetric",
    "ModelDecision",
    "ModelDecisionRow",
    "decisions_for",
    "head_to_head",
    "pending_approvals",
    "record_decision",
    "separation_refusal",
    "trainer_of",
]

MODEL_DECISION_TABLE: Final[str] = "model_decision"

DecisionKind = Literal["approved", "rejected", "promoted"]

SEPARATION_MESSAGE: Final[str] = (
    "You started the training run that produced this model, so another Approver has to decide on it."
)
"""What the route answers and the screen shows when the trainer tries to approve (DEC-862)."""

_EQUAL_TOLERANCE: Final[float] = 1e-12
_M = TypeVar("_M", bound=BaseModel)


class ModelDecisionRow(SQLModel, table=True):
    """One human decision on a model version, with its reason. Append-only in practice."""

    __tablename__ = MODEL_DECISION_TABLE

    decision_id: str = SQLField(primary_key=True)
    model_id: str = SQLField(index=True)
    use_case_id: str
    decision: str
    decided_by: str
    reason: str | None = None
    champion_id: str | None = None
    decided_at: datetime = SQLField(sa_column=Column("decided_at", DateTime(timezone=True), nullable=False))


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelDecision(_Model):
    """A decision as the screen and the API show it."""

    decision_id: str
    model_id: str
    use_case_id: str
    decision: DecisionKind
    decided_by: str = Field(description="`Principal.user_id` of whoever decided.")
    reason: str | None = Field(default=None, description="Why, in the decider's words.")
    champion_id: str | None = Field(default=None, description="The champion at the moment of the decision.")
    decided_at: datetime


class HeadToHeadMetric(_Model):
    """One metric for both models on the challenger's test split."""

    metric: str = Field(description="Metric id from the catalog.")
    label: str = Field(description="Catalog label of the metric.")
    greater_is_better: bool
    primary: bool = Field(description="Whether this is the metric the champion rule compares.")
    challenger: float | None = Field(description="The challenger on its test split.")
    champion: float | None = Field(
        description="The champion re-scored on the same split; null when that was not recorded."
    )
    difference: float | None = Field(description="Challenger minus champion, when both are known.")
    better: Literal["challenger", "champion", "equal"] | None = Field(
        description="Which model this metric favours, honouring `greater_is_better`; null when unknown."
    )


class HeadToHead(_Model):
    """The challenger against the champion it was measured against, on one held-out frame."""

    challenger_model_id: str
    measured_against_champion_id: str | None = Field(
        description="The champion the decision was measured against; null when there was none."
    )
    current_champion_id: str | None = Field(description="The champion now.")
    stale: bool = Field(
        description="The champion has changed since the comparison; approval will be refused (CHAMPION_CHANGED)."
    )
    rows_evaluated: int | None = Field(
        description="Rows in the challenger's test split, both models scored on them."
    )
    improvement_pct: float | None = Field(
        description="The champion rule's improvement on the primary metric."
    )
    metrics: tuple[HeadToHeadMetric, ...]
    note: str | None = Field(default=None, description="What the comparison could not include, and why.")


class ApprovalItem(_Model):
    """One challenger waiting for an Approver, with everything the decision needs."""

    version: ModelVersion
    head_to_head: HeadToHead
    trained_by: str | None = Field(description="`requested_by` of the training run; null when not recorded.")
    can_decide: bool = Field(description="Whether the caller may approve or reject it now.")
    blocked_reason: str | None = Field(description="Why not, in the sentence the screen shows.")
    decisions: tuple[ModelDecision, ...] = Field(
        description="Earlier decisions on this version, oldest first."
    )


# ---------------------------------------------------------------------------
# Who trained it, and whether the caller may decide
# ---------------------------------------------------------------------------
def trainer_of(storage: Storage, version: ModelVersion) -> str | None:
    """`requested_by` of the run that trained `version`, or None when unrecorded or unreadable."""
    try:
        record = storage.read_model(run_key(version.run_id, "run.json"), RunRecord)
    except (StorageError, ValueError, OSError):
        return None
    return record.requested_by


def separation_refusal(principal: Principal, trainer: str | None) -> str | None:
    """The sentence refusing `principal` a decision on a model `trainer` trained, or None (DEC-862).

    Enforced only between signed-in people: the local operator (sign-in off) is everyone at once.
    """
    if principal.kind != "user" or trainer is None:
        return None
    return SEPARATION_MESSAGE if trainer == principal.user_id else None


# ---------------------------------------------------------------------------
# The head-to-head
# ---------------------------------------------------------------------------
def head_to_head(storage: Storage, registry: ModelRegistry, version: ModelVersion) -> HeadToHead:
    """Both models' metrics on the challenger's test split, from what the train flow recorded."""
    measured = version.measured_against_champion_id
    against = None if measured in (None, NO_CHAMPION_AT_DECISION) else measured
    current = registry.get_champion(version.use_case_id)
    current_id = None if current is None else current.model_id
    stale = measured is not None and against != current_id
    evaluation = _read(storage, version.run_id, "evaluation.json", EvaluationReport)
    manifest = _read(storage, version.run_id, "run_manifest.json", RunManifest)
    champion_values = {} if manifest is None or against is None else _champion_metrics(manifest.metrics)
    rows: list[HeadToHeadMetric] = []
    notes: list[str] = []
    if evaluation is not None:
        for item in evaluation.metrics:
            rows.append(
                _row(
                    item.id.value,
                    item.label,
                    item.greater_is_better,
                    primary=item.id is version.metric,
                    challenger=item.value,
                    champion=champion_values.get(item.id.value),
                )
            )
    if not any(row.primary for row in rows):
        # No evaluation.json (an uplift model, whose report is uplift_evaluation.json) or one that
        # does not carry the primary metric: the registry's own number is still the one compared.
        rows.insert(
            0,
            _row(
                version.metric.value,
                version.metric_label,
                _greater_is_better(version, evaluation),
                primary=True,
                challenger=version.test_score,
                champion=champion_values.get(version.metric.value),
            ),
        )
    if against is None:
        notes.append(
            "There was no champion when this model was trained, so there is nothing to compare against."
        )
    elif not champion_values:
        notes.append("The champion's re-score on this model's test split was not recorded for this run.")
    elif any(row.champion is None for row in rows):
        notes.append(
            "Only the metrics the training run recorded for the champion are shown for both models; "
            "runs trained before Plan D recorded the headline metric alone."
        )
    if stale:
        notes.append(
            "The champion has changed since this comparison was made, so approving it will be refused; "
            "retrain, or promote deliberately with a reason."
        )
    return HeadToHead(
        challenger_model_id=version.model_id,
        measured_against_champion_id=against,
        current_champion_id=current_id,
        stale=stale,
        rows_evaluated=None if evaluation is None else evaluation.rows_evaluated,
        improvement_pct=version.improvement_pct,
        metrics=tuple(rows),
        note=" ".join(notes) or None,
    )


def pending_approvals(
    storage: Storage,
    registry: ModelRegistry,
    engine: Engine,
    principal: Principal,
    *,
    use_case_id: str | None = None,
    may_approve: bool,
    role_reason: str | None,
) -> tuple[ApprovalItem, ...]:
    """Every `pending_approval` version (of one use case, or all), newest first, with what the caller may do."""
    pending = [v for v in registry.list_versions(use_case_id) if v.status is ModelStatus.PENDING_APPROVAL]
    history = decisions_for(engine, (v.model_id for v in pending))
    items: list[ApprovalItem] = []
    for version in sorted(pending, key=lambda v: aware_utc(v.created_at), reverse=True):
        trainer = trainer_of(storage, version)
        separation = separation_refusal(principal, trainer)
        blocked = role_reason if not may_approve else separation
        items.append(
            ApprovalItem(
                version=version,
                head_to_head=head_to_head(storage, registry, version),
                trained_by=trainer,
                can_decide=blocked is None,
                blocked_reason=blocked,
                decisions=history.get(version.model_id, ()),
            )
        )
    return tuple(items)


# ---------------------------------------------------------------------------
# The decision record
# ---------------------------------------------------------------------------
def create_decision_table(engine: Engine) -> None:
    """Create `model_decision` if missing (SQLite only; Alembic's 0005 owns Postgres)."""
    create_tables(engine, (MODEL_DECISION_TABLE,))


def record_decision(
    engine: Engine,
    version: ModelVersion,
    decision: DecisionKind,
    *,
    principal: Principal,
    reason: str | None,
    champion_id: str | None,
    now: datetime | None = None,
) -> ModelDecision:
    """Append one decision and return it."""
    create_decision_table(engine)
    row = ModelDecisionRow(
        decision_id=f"md_{uuid.uuid4().hex[:20]}",
        model_id=version.model_id,
        use_case_id=version.use_case_id,
        decision=decision,
        decided_by=principal.user_id,
        reason=(reason or "").strip() or None,
        champion_id=champion_id,
        decided_at=now or utc_now(),
    )
    with Session(engine) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _decision(row)


def decisions_for(engine: Engine, model_ids: Iterable[str]) -> dict[str, tuple[ModelDecision, ...]]:
    """Every recorded decision on these versions, oldest first per version."""
    wanted = list(dict.fromkeys(model_ids))
    if not wanted:
        return {}
    create_decision_table(engine)
    with Session(engine) as session:
        rows = session.exec(
            select(ModelDecisionRow)
            .where(col(ModelDecisionRow.model_id).in_(wanted))
            .order_by(col(ModelDecisionRow.decided_at), col(ModelDecisionRow.decision_id))
        ).all()
    out: dict[str, list[ModelDecision]] = {}
    for row in rows:
        out.setdefault(row.model_id, []).append(_decision(row))
    return {model_id: tuple(items) for model_id, items in out.items()}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _decision(row: ModelDecisionRow) -> ModelDecision:
    return ModelDecision(
        decision_id=row.decision_id,
        model_id=row.model_id,
        use_case_id=row.use_case_id,
        decision=row.decision,  # type: ignore[arg-type]
        decided_by=row.decided_by,
        reason=row.reason,
        champion_id=row.champion_id,
        decided_at=aware_utc(row.decided_at),
    )


def _read(storage: Storage, run_id: str, name: str, model: type[_M]) -> _M | None:
    key = run_key(run_id, name)
    try:
        return storage.read_model(key, model) if storage.exists(key) else None
    except (StorageError, ValueError, OSError):
        return None


def _champion_metrics(metrics: dict[str, float]) -> dict[str, float]:
    prefix = "champion_"
    return {name[len(prefix) :]: value for name, value in metrics.items() if name.startswith(prefix)}


def _greater_is_better(version: ModelVersion, evaluation: EvaluationReport | None) -> bool:
    if evaluation is not None:
        for item in evaluation.metrics:
            if item.id is version.metric:
                return item.greater_is_better
    from engine.config import get_catalog

    try:
        return bool(get_catalog().metrics[version.metric].greater_is_better)
    except Exception:  # an unreadable catalog must not hide the screen; say "unknown" instead
        return True


def _row(
    metric: str,
    label: str,
    greater_is_better: bool,
    *,
    primary: bool,
    challenger: float | None,
    champion: float | None,
) -> HeadToHeadMetric:
    difference = None if challenger is None or champion is None else challenger - champion
    better: Literal["challenger", "champion", "equal"] | None = None
    if difference is not None:
        if abs(difference) <= _EQUAL_TOLERANCE:
            better = "equal"
        else:
            better = "challenger" if (difference > 0) == greater_is_better else "champion"
    return HeadToHeadMetric(
        metric=metric,
        label=label,
        greater_is_better=greater_is_better,
        primary=primary,
        challenger=challenger,
        champion=champion,
        difference=difference,
        better=better,
    )
