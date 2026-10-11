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

**Advisory checks (Plan J M96).** An uplift challenger also carries `checks` from
`engine.model_gates`: whether it beats plain risk ranking, whether it is stable across refitted folds
and whether its predicted uplift is calibrated, each read from what its training run measured. They
inform the Approver; the champion rule and the decision routes ignore them.

**How the model that chose the last list did on that campaign (Plan J M106).** A challenger learned
from a cycle (step 4's "Learn who to contact next time" on a run that engaged the holdout service)
also carries `live_calibration`: the change the model that chose that cycle's list predicted, against
the change the campaign measured, per tenth of its ranking, read from the training run's
`learned_from.json` (`engine.measurement.learn`). That model may since have been replaced, so the block
never calls it the model in use. It is advice too, and absent for every other model.

**Every decision is recorded with its reason** in `model_decision` (the platform database, migration
`0005_plan_d`): approved, rejected or promoted, by whom (a user id), against which champion, when.
The registry row keeps Phase 1's `approved_by` / `promoted_by`, now the signed-in username when
sign-in is on. Rejecting archives a `pending_approval` version; nothing else can be rejected.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any, Final, Literal, TypeVar

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
from engine.measurement.learn import LiveCalibration, live_calibration_for
from engine.model_gates import ApprovalCheck, approval_checks
from engine.platform_db import create_tables
from engine.registry import ModelRegistry, aware_utc
from engine.storage import Storage, StorageError, run_key
from engine.utils.time import utc_now

__all__ = [
    "DECISION_SUBJECTS",  # Plan K M132 (DEC-1401): a proposal is the second kind of decision
    "MODEL_DECISION_TABLE",
    "ApprovalItem",
    "DecisionKind",
    "HeadToHead",
    "HeadToHeadMetric",
    "ModelDecision",
    "ModelDecisionRow",
    "ProposalDecisionKind",
    "ProposalReceipt",
    "decide_proposal",
    "decisions_for",
    "head_to_head",
    "identity_part",
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


def _absent(value: object) -> bool:
    """`exclude_if` of M106's field: an item without a live block carries no `live_calibration` key."""
    return value is None


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
    # Plan J M96 (additive): advisory checks beside the champion decision, from `engine.model_gates`.
    # Empty for a model that is not an uplift model; they never approve, reject or block anything.
    # Plan L follow-up (DEC-1599): such a model now carries RESULT_SUSPICIOUSLY_GOOD when it was evaluated.
    checks: tuple[ApprovalCheck, ...] = Field(
        default=(),
        description="Advisory checks ({code, passed, message}); passed is null when not measured.",
    )
    # Plan J M106 (additive): for a challenger learned from a cycle, how the model that chose that
    # cycle's list did, predicted against measured by tenth (`engine.measurement.learn`). Absent, not
    # null, for every other model, so their items are unchanged.
    live_calibration: LiveCalibration | None = Field(
        default=None,
        exclude_if=_absent,
        description="Predicted against measured change by tenth on the last campaign; absent when not learned from one.",
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


def identity_part(principal: Principal) -> dict[str, bool]:
    """Plan M M184 (DEC-1614 (e)): `{"identity_verified": False}` for a decision by anyone but a signed-in person
    (the local operator with sign-in off), to add to its ledger or audit record; `{}` for a signed-in person, whose
    record is unchanged."""
    return {} if principal.kind == "user" else {"identity_verified": False}


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
                checks=approval_checks(storage, version),
                live_calibration=live_calibration_for(storage, version),
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


# ---------------------------------------------------------------------------
# Plan K M132 (DEC-1401): agent proposals, a second kind of decision beside model decisions
# ---------------------------------------------------------------------------
# A proposal (`engine.agentic.proposals`) is decided here as a model is: by a person holding the Approver
# role, never by the person it was proposed for (`separation_refusal`, the same rule), and with the policy
# checked again against the facts as they are now. The stored proposal file must be the one the ledger
# recorded at draft time (`engine.agentic.proposals.ledger_mismatch`), or it is refused as
# `PROPOSAL_TAMPERED`; a test plan the route freezes must hash to the plan proposed, or it is `PROPOSAL_STALE`. Approving it calls the function the HTTP route calls (the
# caller passes it in as `execute`: `api.routes.agentic` passes `api.routes.campaigns.register_plan` for a
# test plan), so an agent never gets a second path to anything. The record is the decision ledger
# (`engine.agentic.ledger`): `proposal.approved` (signed), `proposal.rejected`, `proposal.refused`,
# `approval.refused`, `proposal.execution_failed` and, for a test plan, `test_plan.frozen` (signed, with the
# holdout's commitment). Nothing above this block changes, and nothing here runs unless `agentic.enabled` is
# on; `engine.agentic` is imported inside the functions, so importing this module does not load it.
DECISION_SUBJECTS: Final[tuple[str, ...]] = ("model", "proposal")
"""What a person decides on: a challenger model (Plan D) or an agent's proposal (Plan K)."""

ProposalDecisionKind = Literal["approved", "rejected"]


class ProposalReceipt(_Model):
    """What an Approver gets back, and keeps: the decision and the ledger head after it (the checkpoint)."""

    proposal_id: str
    decision: ProposalDecisionKind
    status: str = Field(description="The proposal's status after the decision.")
    decided_by: str = Field(description="`Principal.user_id` of whoever decided.")
    payload_digest: str
    result_ref: str | None = Field(
        default=None, description="What approving made, e.g. the frozen plan_hash."
    )
    ledger_checkpoint: str = Field(
        description="entry_hash of the ledger head after the decision. Keep it: a rewritten ledger no longer matches."
    )
    # Plan K M141 (DEC-1408 (d)): the rest of a client-held checkpoint (`engine.agentic.receipts`). Added, defaulted
    # fields: `python -m engine.agentic.ledger verify --against` reads a kept receipt back.
    ledger_seq: int | None = Field(default=None, description="The decision's seq on its chain.")
    ledger_chain: str | None = Field(
        default=None,
        description="The client workspace whose chain holds the decision; absent on the deployment's chain.",
        exclude_if=lambda value: value is None,
    )
    holdout_salt_fingerprint: str | None = Field(
        default=None,
        description="The holdout salt's fingerprint the platform records: who is held out, committed.",
    )
    holdout_salt_note: str | None = Field(default=None, description="Why there is no fingerprint, when null.")


def decide_proposal(
    proposal_id: str,
    decision: ProposalDecisionKind,
    *,
    principal: Principal,
    reason: str | None,
    tool_ctx: Any,
    execute: Callable[[Any], str | None],
) -> ProposalReceipt:
    """Approve or reject a pending proposal (see the block comment above). Raises `AgenticError`."""
    from engine.agentic.canonical import digest
    from engine.agentic.config import load_policy, require_enabled
    from engine.agentic.errors import AgenticError
    from engine.agentic.ledger import (
        APPROVAL_REFUSED,
        EXECUTION_FAILED,
        PROPOSAL_APPROVED,
        PROPOSAL_EXPIRED,
        PROPOSAL_REFUSED,
        PROPOSAL_REJECTED,
    )
    from engine.agentic.policy import RULES, ProposalSubject, build_context, evaluate
    from engine.agentic.proposals import ProposalStore, ledger_mismatch

    require_enabled(tool_ctx.config_root)
    ledger = tool_ctx.ledger
    if ledger is None:
        raise AgenticError(
            "LEDGER_CHAIN_BROKEN", "A proposal is decided only onto the decision ledger.", status=500
        )
    store = ProposalStore(tool_ctx.storage)
    proposal = store.get(proposal_id)
    now = tool_ctx.clock()
    refs: dict[str, Any] = {
        "proposal_id": proposal.proposal_id,
        "proposal_kind": proposal.kind,
        "payload_digest": proposal.payload_digest,
    }
    reason_text = (reason or "").strip()
    reason_part: dict[str, Any] = {"reason_digest": digest(reason_text)} if reason_text else {}
    if proposal.status == "pending":
        # The stored file is ordinary artefact storage: act on it only when it is the proposal the ledger
        # recorded at draft time (payload digest, kind, proposer, evidence, expiry) and still undecided there.
        entries = ledger.proposal_entries(proposal.proposal_id)
        mismatch, ledger_status = ledger_mismatch(proposal, entries)
        if mismatch is not None:
            drafted = [e.body for e in entries if e.kind == "proposal.drafted" and "payload_digest" in e.body]
            recorded = {"payload_digest": drafted[0]["payload_digest"]} if drafted else {}
            status = ledger_status or "refused"
            body = {**refs, **recorded, "status": status, "codes": ["PROPOSAL_TAMPERED"]}
            if ledger_status is None:
                ledger.append(PROPOSAL_REFUSED, principal.user_id, body, at=now)
            else:
                ledger.append(APPROVAL_REFUSED, principal.user_id, body, at=now)
            store.save(
                proposal.model_copy(update={"status": status, "decision_codes": ("PROPOSAL_TAMPERED",)})
            )
            raise AgenticError(
                "PROPOSAL_TAMPERED",
                f"This proposal was changed after it was drafted ({mismatch}), so it cannot be decided; "
                "have it drafted again.",
                status=409,
            )
    if proposal.status == "pending" and proposal.status_at(now) == "expired":
        ledger.append(PROPOSAL_EXPIRED, principal.user_id, {**refs, "status": "expired"}, at=now)
        store.save(proposal.model_copy(update={"status": "expired"}))
        raise AgenticError(
            "PROPOSAL_EXPIRED", "This proposal expired before anyone decided on it.", status=409
        )
    if proposal.status != "pending":
        raise AgenticError(
            "PROPOSAL_NOT_PENDING",
            f"This proposal is {proposal.status}; only a pending one can be decided.",
            status=409,
        )

    def finish(
        status: str, kind: str, extra: dict[str, Any], result_ref: str | None = None
    ) -> ProposalReceipt:
        body = {
            **refs,
            "status": status,
            "decided_by": principal.user_id,
            "proposed_by": proposal.proposed_by,
            # Plan M M184 (DEC-1614 (e)): with sign-in off the decider is the local operator, holding every role,
            # so who decided is not known; the entry says so. A signed-in decision's body is unchanged.
            **identity_part(principal),
        }
        head = ledger.append(kind, principal.user_id, {**body, **extra}, at=now)
        store.save(
            proposal.model_copy(
                update={
                    "status": status,
                    "decided_by": principal.user_id,
                    "decided_at": now,
                    "result_ref": result_ref,
                    "ledger_checkpoint": head.entry_hash,
                }
            )
        )
        from engine.agentic.receipts import receipt_for

        kept = receipt_for(head, ledger.engine)
        return ProposalReceipt(
            proposal_id=proposal.proposal_id,
            decision=decision,
            status=status,
            decided_by=principal.user_id,
            payload_digest=proposal.payload_digest,
            result_ref=result_ref,
            ledger_checkpoint=head.entry_hash,
            ledger_seq=kept.ledger_seq,
            ledger_chain=kept.ledger_chain,
            holdout_salt_fingerprint=kept.holdout_salt_fingerprint,
            holdout_salt_note=kept.holdout_salt_note,
        )

    if decision == "rejected":
        return finish("rejected", PROPOSAL_REJECTED, reason_part)

    subject = ProposalSubject(kind=proposal.kind, payload=proposal.payload, proposed_by=proposal.proposed_by)
    context = build_context(subject, tool_ctx, load_policy(tool_ctx.config_root), approver=principal)
    rules = tool_ctx.policy_rules if tool_ctx.policy_rules is not None else RULES
    refused = [d for d in evaluate(subject, context, rules=rules) if not d.allowed]
    if any(d.rule == "role.separation" for d in refused):
        body = {
            **refs,
            "status": "pending",
            "codes": ["POLICY_ROLE_SEPARATION"],
            "decided_by": principal.user_id,
        }
        ledger.append(APPROVAL_REFUSED, principal.user_id, body, at=now)
        raise AgenticError(
            "POLICY_ROLE_SEPARATION",
            "You proposed this, so another Approver has to decide on it.",
            status=403,
        )
    codes = sorted({d.code for d in refused if d.code is not None})
    if (
        not codes
        and proposal.kind == "test_plan"
        and context.dry_plan_hash != proposal.payload.get("plan_hash")
    ):
        codes = ["PROPOSAL_STALE"]
    if codes:
        ledger.append(
            PROPOSAL_REFUSED, principal.user_id, {**refs, "status": "refused", "codes": codes}, at=now
        )
        store.save(proposal.model_copy(update={"status": "refused", "decision_codes": tuple(codes)}))
        message = (
            "The campaign has changed since this was proposed, so the plan would not be the one proposed."
            if codes == ["PROPOSAL_STALE"]
            else "The policy refuses this proposal now: " + ", ".join(codes) + "."
        )
        raise AgenticError(codes[0], message, status=409, detail={"codes": codes})
    try:
        result_ref = execute(proposal)
    except AgenticError as exc:
        ledger.append(
            EXECUTION_FAILED, principal.user_id, {**refs, "status": "pending", "codes": [exc.code]}, at=now
        )
        raise
    if proposal.kind == "test_plan" and result_ref is not None:
        _record_frozen_plan(ledger, tool_ctx, principal, proposal, result_ref, now)
        if result_ref != proposal.payload.get("plan_hash"):
            # The route froze (or kept) another plan than the one proposed: never record that as approving it.
            body = {**refs, "status": "refused", "codes": ["PROPOSAL_STALE"], "result_ref": result_ref}
            ledger.append(PROPOSAL_REFUSED, principal.user_id, body, at=now)
            store.save(
                proposal.model_copy(
                    update={
                        "status": "refused",
                        "decision_codes": ("PROPOSAL_STALE",),
                        "result_ref": result_ref,
                    }
                )
            )
            raise AgenticError(
                "PROPOSAL_STALE",
                "The campaign's plan is not the one proposed, so this proposal was not approved.",
                status=409,
                detail={"codes": ["PROPOSAL_STALE"], "result_ref": result_ref},
            )
    return finish("approved", PROPOSAL_APPROVED, {**reason_part, "result_ref": result_ref}, result_ref)


def _record_frozen_plan(
    ledger: Any, tool_ctx: Any, principal: Principal, proposal: Any, plan_hash: str, now: datetime
) -> None:
    """`test_plan.frozen` (signed): the plan's hash and the holdout's commitment (scope, epoch, salt id)."""
    from engine.agentic.ledger import TEST_PLAN_FROZEN, holdout_commitment

    campaign_id = str(proposal.payload.get("campaign_id"))
    body: dict[str, Any] = {
        "proposal_id": proposal.proposal_id,
        "campaign_id": campaign_id,
        "plan_hash": plan_hash,
        **holdout_commitment(tool_ctx, campaign_id),
    }
    ledger.append(TEST_PLAN_FROZEN, principal.user_id, body, at=now)
