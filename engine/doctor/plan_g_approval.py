"""Guided setup's Approve with Data Doctor on (Plan O M246, DEC-1816): the approval-time facts M233 left open.

M233 (DEC-1803) wrote the approval-time half of the data policy - the exact sentence, the role, separation of
duties and decision O3's reshape guards (`engine.doctor.policy.ApprovalFacts`) - but Plan G's Approve route did
not call it: the session did not record whether a preview was shown or which sentence was read. Behind
`data_doctor.unified` it now does:

* **Preview shown.** `POST /uploads/{id}/agent-session/preview` records what it showed (:func:`record_preview`:
  the accepted steps' `recipe_hash` and the preview's receipt, counts only) beside the session. A preview of
  other steps than the ones approved does not count.
* **Reshape items.** Each accepted step that changes the rows (`combine_rows`, `unpivot`, `keep_latest_per_id`)
  becomes M238's `ReshapeItem`: offered unticked, through its answered question, with the full future-data check
  a training Approve runs, and its preview receipt shown - M233's `data.reshape_guard` decides
  (`RESHAPE_GUARD_MISSING`).
* **The sentence read.** The person approves the sentence the server shows (:func:`approval_of`), word for word.
  When an accepted step is APPROVE-class (`remove_rows`; `keep_latest_per_id` leaving out more than 2% of the
  preview's rows), the sentence names it and the approval needs it read back: M233's `data.approve_class` decides
  (`APPROVAL_SENTENCE_CHANGED`, `TOOL_ROLE_REQUIRED`).
* **Separation of duties** (O17). With the switch on, the decide, answer and edit routes record who ticked,
  answered or added each suggestion (:func:`record_proposers`, beside the session); with
  `governance.approval_required` on, an APPROVE-class step is approved by someone else (`POLICY_ROLE_SEPARATION`).
  A step a person ticked before anyone was recorded (the switch turned on mid-session) cannot be checked, so it
  is refused the same way until it is ticked again.
* **O10's warning.** An AI step that changes more than 30% of its column carries `AI_STEP_CHANGES_MANY`
  (`engine.agent.loop`); :func:`approval_of` lists those accepted, so the screen shows them at Approve.

With the switch off none of this is read, and Approve is exactly as before.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from pydantic import AwareDatetime, Field

from engine.access.roles import Principal, Role
from engine.agent.contracts import (
    ROW_RESHAPE_STEP_KINDS,
    AgentSession,
    DecidedBy,
    Proposal,
    ProposalKind,
    ProposalSource,
    ProposalState,
    RecipeReceipt,
    RecipeStepKind,
    StepReceipt,
)
from engine.config import StrictBase
from engine.doctor.protocol import (
    DEFAULT_SETTINGS,
    ProtocolSettings,
    approve_class_of,
    confirmation_refusal,
    role_for,
)
from engine.doctor.reshape_guard import ReshapeItem, reshape_guard_failures
from engine.storage import Storage, StorageError, upload_key

if TYPE_CHECKING:
    from engine.agentic.policy import Decision

__all__ = [
    "PREVIEW_SHOWN_FILENAME",
    "PROPOSERS_FILENAME",
    "WARNING_MESSAGES",
    "ApproveClassStep",
    "PlanGApproval",
    "PreviewShown",
    "StepWarning",
    "approval_of",
    "approval_refusal",
    "read_preview",
    "read_proposers",
    "record_preview",
    "record_proposers",
]

PREVIEW_SHOWN_FILENAME: Final[str] = "agent/preview_shown.json"
"""Beside `agent/agent_session.json`: deleted with the upload (retention, erasure)."""

PROPOSERS_FILENAME: Final[str] = "agent/proposed_by.json"
"""Beside `agent/agent_session.json`: who ticked, answered or added each suggestion (user ids only)."""

_UNRECORDED: Final[str] = (
    "Who chose this change was not recorded, so another Approver cannot be checked. Untick it and tick it again, "
    "then someone else approves it."
)

_KEEP_LATEST: Final[str] = "Keep only the latest row of each “{column}”"

WARNING_MESSAGES: Final[dict[str, str]] = {
    "AI_STEP_CHANGES_MANY": (
        "This suggestion from the AI changes more than 30% of the values in its column. Check the before and "
        "after carefully before you approve it."
    ),
}
"""What Approve says for each warning an accepted AI step carries (decision O10)."""


class PreviewShown(StrictBase):
    """What the last preview showed: the accepted steps (by `recipe_hash`) and its receipt. Counts, never rows."""

    upload_id: str
    recipe_hash: str | None = Field(default=None, description="The accepted steps previewed; null for none.")
    receipt: RecipeReceipt | None = None
    shown_at: AwareDatetime
    shown_by: str | None = None


def record_preview(
    storage: Storage,
    upload_id: str,
    *,
    recipe_hash: str | None,
    receipt: RecipeReceipt | None,
    actor: str | None,
    at: datetime,
) -> None:
    storage.write_model(
        upload_key(upload_id, PREVIEW_SHOWN_FILENAME),
        PreviewShown(
            upload_id=upload_id, recipe_hash=recipe_hash, receipt=receipt, shown_at=at, shown_by=actor
        ),
    )


def read_preview(storage: Storage, upload_id: str) -> PreviewShown | None:
    key = upload_key(upload_id, PREVIEW_SHOWN_FILENAME)
    try:
        return storage.read_model(key, PreviewShown) if storage.exists(key) else None
    except (StorageError, ValueError):
        return None


class Proposers(StrictBase):
    """`agent/proposed_by.json`: per suggestion, the people who ticked, answered or added it."""

    upload_id: str
    by_proposal: dict[str, tuple[str, ...]] = Field(default_factory=dict)


def read_proposers(storage: Storage, upload_id: str) -> dict[str, tuple[str, ...]]:
    key = upload_key(upload_id, PROPOSERS_FILENAME)
    try:
        return dict(storage.read_model(key, Proposers).by_proposal) if storage.exists(key) else {}
    except (StorageError, ValueError):
        return {}


def record_proposers(
    storage: Storage, upload_id: str, before: AgentSession, after: AgentSession, actor: str | None
) -> None:
    """Record `actor` as a proposer of every suggestion their action ticked, changed or added (Plan O M246, O17).

    Called by the decide, answer and edit routes with the session before and after the action, under the
    session's lock. A suggestion is the person's when it is accepted after the action and was not accepted, or
    not the same, before: accepting one, answering the question that adds it, or adding an edit by hand. Without
    a signed-in person (`actor` None: the local operator, who is everyone at once) nothing is recorded.
    """
    if actor is None:
        return
    earlier = {p.proposal_id: p for p in before.proposals}
    touched = [
        p.proposal_id
        for p in after.proposals
        if p.state is ProposalState.ACCEPTED
        and p.decided_by is DecidedBy.USER
        and (
            (old := earlier.get(p.proposal_id)) is None
            or old.state is not ProposalState.ACCEPTED
            or old.model_dump(exclude={"decided_by", "state"})
            != p.model_dump(exclude={"decided_by", "state"})
        )
    ]
    if not touched:
        return
    found = read_proposers(storage, upload_id)
    for proposal_id in touched:
        people = found.get(proposal_id, ())
        if actor not in people:
            found[proposal_id] = (*people, actor)
    storage.write_model(
        upload_key(upload_id, PROPOSERS_FILENAME), Proposers(upload_id=upload_id, by_proposal=found)
    )


@dataclass(frozen=True)
class ApproveClassStep:
    """An accepted step that needs its own yes: what it is, its sentence and the role the yes needs."""

    proposal_id: str
    kind: str
    sentence: str
    role: Role
    why: str
    proposed_by: tuple[str, ...] = ()
    """Who ticked, answered or added it (:func:`record_proposers`)."""
    proposer_known: bool = True
    """False for a step a person ticked that no record names (separation of duties cannot be checked)."""


@dataclass(frozen=True)
class StepWarning:
    proposal_id: str
    code: str
    message: str


@dataclass(frozen=True)
class PlanGApproval:
    """What Approve shows and checks: the exact sentence, the APPROVE-class steps, the reshape guards, warnings."""

    sentence: str
    approve_class: tuple[ApproveClassStep, ...]
    reshape: tuple[ReshapeItem, ...]
    preview_shown: bool
    warnings: tuple[StepWarning, ...]

    @property
    def reshape_missing(self) -> tuple[str, ...]:
        return tuple(reason for item in self.reshape for reason in reshape_guard_failures(item))


def _question_answered(session: AgentSession, proposal: Proposal) -> bool:
    """Whether `proposal` was added by answering its question with the option that adds it (guard 1)."""
    from engine.agent.proposals import identity

    wanted = identity(proposal)
    return any(
        option.proposal is not None
        and identity(option.proposal) == wanted
        and question.answer == option.option_id
        for question in session.questions
        for option in question.options
    )


def _receipt_for(
    preview: PreviewShown | None, recipe_hash: str | None, kind: RecipeStepKind
) -> StepReceipt | None:
    if preview is None or preview.receipt is None or preview.recipe_hash != recipe_hash:
        return None
    return next((step for step in preview.receipt.steps if step.kind is kind), None)


def _warnings(session: AgentSession, accepted: Sequence[Proposal]) -> tuple[StepWarning, ...]:
    results = {result.evidence_id: result for result in session.tool_results}
    found: list[StepWarning] = []
    for proposal in accepted:
        if proposal.source is not ProposalSource.AI:
            continue
        for evidence_id in proposal.evidence_ids[:1]:  # the chat's own step result is the first it cites
            result = results.get(evidence_id)
            codes = result.result.get("warnings") if result is not None else None
            if isinstance(codes, list):
                found.extend(
                    StepWarning(proposal.proposal_id, str(code), WARNING_MESSAGES.get(str(code), str(code)))
                    for code in codes
                    if isinstance(code, str)
                )
    return tuple(found)


def approval_of(
    session: AgentSession,
    *,
    file_name: str,
    preview: PreviewShown | None,
    recipe_hash: str | None,
    training: bool,
    approval_required: bool,
    settings: ProtocolSettings = DEFAULT_SETTINGS,
    proposers: Mapping[str, Sequence[str]] | None = None,
) -> PlanGApproval:
    """What Approve shows now: the sentence, word for word, and what it checks. `proposers`: who ticked,
    answered or added each suggestion (:func:`read_proposers`); None reads as nothing recorded."""
    recorded = proposers or {}
    accepted = [p for p in session.proposals if p.state is ProposalState.ACCEPTED]
    steps = [p for p in accepted if p.kind is ProposalKind.RECIPE_STEP and p.step is not None]
    reshape: list[ReshapeItem] = []
    special: list[ApproveClassStep] = []
    rows: Mapping[str, int] = {}
    if preview is not None and preview.receipt is not None and preview.recipe_hash == recipe_hash:
        rows = {"rows_in": preview.receipt.rows_in, "rows_out": preview.receipt.rows_out}
    for proposal in steps:
        step = proposal.step
        assert step is not None
        if step.kind in ROW_RESHAPE_STEP_KINDS:
            receipt = _receipt_for(preview, recipe_hash, step.kind)
            reshape.append(
                ReshapeItem(
                    step=step,
                    ticked_when_offered=proposal.decided_by is DecidedBy.AGENT,
                    question_answered=_question_answered(session, proposal),
                    leak_check="full" if training else "narrow",
                    receipt=receipt,
                    training=training,
                )
            )
        rule = approve_class_of(step.kind.value, dict(rows), settings)
        if rule is None:
            continue
        if step.kind is RecipeStepKind.KEEP_LATEST_PER_ID:
            left = (rows["rows_in"] - rows["rows_out"]) if rows else None
            sentence = _KEEP_LATEST.format(column=step.column) + (
                f": {left:,} of the preview's {rows['rows_in']:,} rows are left out."
                if left is not None
                else "."
            )
        else:
            sentence = proposal.title.rstrip(".") + "."
        special.append(
            ApproveClassStep(
                proposal_id=proposal.proposal_id,
                kind=step.kind.value,
                sentence=sentence,
                role=role_for(rule, approval_required=approval_required),
                why=rule.why,
                proposed_by=tuple(recorded.get(proposal.proposal_id, ())),
                proposer_known=proposal.proposal_id in recorded or proposal.decided_by is not DecidedBy.USER,
            )
        )
    count = len(accepted)
    base = f"Prepare {file_name} with {count} change{'s' if count != 1 else ''}."
    extra = "".join(f" {item.sentence}" for item in special)
    return PlanGApproval(
        sentence=f"{base}{extra} Nothing else changes.",
        approve_class=tuple(special),
        reshape=tuple(reshape),
        preview_shown=preview is not None and preview.recipe_hash == recipe_hash,
        warnings=_warnings(session, accepted),
    )


def approval_refusal(
    approval: PlanGApproval,
    *,
    approver: Principal,
    sentence_read: str | None,
    approval_required: bool,
) -> Decision | None:
    """The first refusal of M233's approval-time rules on this Approve, or None.

    Reshape guards (`RESHAPE_GUARD_MISSING`) first; then, when a step is APPROVE-class, the role, the exact
    sentence and - with `approval_required` - separation of duties (`TOOL_ROLE_REQUIRED`,
    `APPROVAL_SENTENCE_CHANGED`, `POLICY_ROLE_SEPARATION`, also when who chose the step was not recorded);
    otherwise a sentence sent must still be exact.
    """
    from engine.doctor import policy

    if approval.reshape:
        ctx = policy.DataPolicyContext(
            policy=policy.DataPolicyConfig(),
            approval=policy.ApprovalFacts(approver=approver, reshape=approval.reshape),
        )
        rules = policy.DataRuleSet(rules=(policy.RESHAPE_GUARD,), proposes="steps")
        for item in approval.reshape:
            subject = policy.DataProposal(
                kind="data.recipe_step", source="person", steps=(policy.StepFacts.of(item.step),), asked=True
            )
            found = policy.reshape_guard(subject, ctx, rules)
            if found is not None and not found.allowed:
                return found
    for special in approval.approve_class:
        rule = approve_class_of(special.kind, {})
        assert rule is not None
        found = confirmation_refusal(
            rule=rule,
            approver=approver,
            sentence_read=sentence_read,
            sentence_now=approval.sentence,
            proposed_by=special.proposed_by,
            approval_required=approval_required,
        )
        if found is not None:
            return found
        if approval_required and approver.kind == "user" and not special.proposer_known:
            from engine.agentic.policy import Decision

            return Decision(
                allowed=False,
                rule="data.approve_class",
                code="POLICY_ROLE_SEPARATION",
                detail={"reason": _UNRECORDED},
            )
    if sentence_read is not None and sentence_read != approval.sentence:
        from engine.agentic.policy import Decision

        return Decision(
            allowed=False,
            rule="data.approve_class",
            code="APPROVAL_SENTENCE_CHANGED",
            detail={"reason": "What would be approved has changed since you read it; read the new sentence."},
        )
    return None
