"""One approval: the one list of yeses a plan needs, and one call that gives them (root-cause plan, F6).

There were four kinds of yes - Approve, an APPROVE-class change's own yes (`confirm`), a stop's (`acknowledge_stop`),
and an answer that makes an accepted item - each with its own check and its own digest, and a person had to give them
one by one in the right order. Here:

* :func:`needs_yes` is the one list of what the plan still needs a yes to: every open stop that may be said yes to
  (:mod:`engine.doctor.ledger`) and every accepted APPROVE-class change without its own yes. A hard rule is on no
  list: nothing covers it.
* :class:`Approval` is what a person reads and approves: the `subjects` (what each yes is about), their `digests`
  (what each covers: the stop's key and count, the change's payload and sentence), and the `sentence`. Its `digest`
  moves when any of them does, so "I read this" can be sent back and checked.
* :func:`approve_with` gives all the yeses the person was shown, then approves, in one call. Everything is checked
  before anything is written - the role, the sentence word for word, separation of duties (who *asked* for a change is
  recorded on the item, `CaseItem.requested_by`) - and a yes for something not shown is not given.

The routes `acknowledgements` and `confirmations` stay; they run the same checks (`visit.acknowledge_stop`,
`visit.confirm`). Names, digests and sentences only.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.config import StrictBase
from engine.doctor.case import Case, CaseError, VisitState
from engine.doctor.ledger import blocking, findings_of
from engine.doctor.protocol import (
    DEFAULT_SETTINGS,
    ProtocolSettings,
    approval_digest,
    approve_class_of,
    confirmation_refusal,
    item_sentence,
    role_for,
)

if TYPE_CHECKING:
    from engine.access.roles import Principal
    from engine.doctor.visit import VisitDeps
    from engine.doctor.visit_contracts import PlanCheck

__all__ = ["Approval", "GivenYes", "Yes", "approval_of", "approve_with", "needs_yes"]


class Yes(StrictBase):
    """One yes the plan needs."""

    subject: str = Field(description="`stop:<key>` or `confirm:<item_id>`.")
    kind: Literal["acknowledge", "confirm"]
    key: str | None = Field(default=None, description="The stop's key (`acknowledge`).")
    item_id: str | None = Field(default=None, description="The change (`confirm`).")
    sentence: str = Field(description="What the person says yes to, word for word.")
    digest: str = Field(description="What the yes covers.")
    why: str = ""
    role: Literal["analyst", "approver"] | None = None
    can_agree: bool = True
    cannot_agree: Literal["role", "separation"] | None = None


class Approval(StrictBase):
    """What a person approves: every yes, with what each covers, and the sentence."""

    subjects: tuple[str, ...]
    digests: tuple[str, ...]
    sentence: str
    yeses: tuple[Yes, ...] = ()
    hard: tuple[str, ...] = Field(default=(), description="Keys of the rules no yes covers.")
    digest: str = Field(description="Over the subjects, their digests and the sentence.")


class GivenYes(StrictBase):
    """A yes the person gives with the approval."""

    kind: Literal["acknowledge", "confirm"]
    key: str | None = None
    item_id: str | None = None
    sentence: str | None = Field(
        default=None, description="`confirm`: the change's sentence as the person read it."
    )
    reason: str = Field(default="", max_length=500)

    @property
    def subject(self) -> str:
        return f"stop:{self.key}" if self.kind == "acknowledge" else f"confirm:{self.item_id}"


def needs_yes(
    case: Case,
    *,
    settings: ProtocolSettings = DEFAULT_SETTINGS,
    separate: bool = False,
    principal: Principal | None = None,
    check: PlanCheck | None = None,
) -> tuple[Yes, ...]:
    """Every yes the plan as it stands still needs: open stops that may be said yes to, then accepted APPROVE-class
    changes without their own yes."""
    from engine.agent.contracts import ProposalState
    from engine.doctor.visit import active_section, confirmed, item_proposers

    out: list[Yes] = [
        Yes(
            subject=f"stop:{f.key}",
            kind="acknowledge",
            key=f.key,
            sentence=f.text,
            digest=sha256_hex(canonical_bytes([f.key, f.count]))[:32],
            why="a safety check stops this plan",
        )
        for f in blocking(case, check)
        if f.acknowledgeable
    ]
    items, _ = active_section(case)
    for item in items:
        rule = approve_class_of(item.kind, item.payload, settings)
        if item.state is not ProposalState.ACCEPTED or rule is None or confirmed(case, item):
            continue
        sentence = item_sentence(item.title, item.payload)
        role = role_for(rule, approval_required=separate)
        why_not: Literal["role", "separation"] | None = None
        if principal is not None:
            refused = confirmation_refusal(
                rule=rule,
                approver=principal,
                sentence_read=sentence,
                sentence_now=sentence,
                proposed_by=item_proposers(case, item),
                approval_required=separate,
            )
            if refused is not None:
                why_not = "separation" if refused.code == "POLICY_ROLE_SEPARATION" else "role"
        out.append(
            Yes(
                subject=f"confirm:{item.item_id}",
                kind="confirm",
                item_id=item.item_id,
                sentence=sentence,
                digest=approval_digest([item.payload_digest], sentence),
                why=rule.why,
                role="approver" if role.value == "approver" else "analyst",
                can_agree=why_not is None,
                cannot_agree=why_not,
            )
        )
    return tuple(out)


def approval_of(
    case: Case,
    sentence: str,
    *,
    settings: ProtocolSettings = DEFAULT_SETTINGS,
    separate: bool = False,
    principal: Principal | None = None,
    check: PlanCheck | None = None,
) -> Approval:
    """The approval as the person would read it now."""
    yeses = needs_yes(case, settings=settings, separate=separate, principal=principal, check=check)
    subjects = tuple(y.subject for y in yeses)
    digests = tuple(y.digest for y in yeses)
    hard = tuple(f.key for f in findings_of(case, check) if f.level == "hard")
    return Approval(
        subjects=subjects,
        digests=digests,
        sentence=sentence,
        yeses=yeses,
        hard=hard,
        digest=sha256_hex(canonical_bytes([subjects, digests, sentence, hard]))[:32],
    )


def _sub_op(op_id: str | None, subject: str) -> str | None:
    """What a yes is about, not its place in the call, names it: a retry that has fewer yeses left keeps the ids."""
    return None if op_id is None else sha256_hex(canonical_bytes([op_id, subject]))[:40]


def _refuse_what_is_left(
    case: Case, deps: VisitDeps, wanted: dict[str, Yes], given: set[str], sentence: str | None
) -> None:
    """Before anything is written: the approval's own refusals, for what the yeses given leave undone."""
    from engine.doctor import visit

    if sentence is not None:
        shown = visit.approval_sentence(case, goal_name=visit.approval_goal_name(deps.storage, case))
        if sentence != shown:
            raise CaseError(
                "APPROVAL_SENTENCE_CHANGED",
                f"What would be approved has changed since you read it. It now reads: “{shown}”",
            )
    left = [y for s, y in wanted.items() if s not in given]
    if any(y.kind == "confirm" for y in left):
        raise CaseError(
            "APPROVAL_CONFIRMATION_MISSING", "A change still needs its own yes before the plan is approved."
        )
    hard = [f for f in blocking(case) if f.level == "hard" and f.source != "visit"]
    if hard or left:
        raise CaseError(
            "APPROVAL_STOP_OPEN",
            f"{len(hard) + len(left)} safety check(s) still stop this plan: say yes to each one that may be "
            "said yes to, or change the plan.",
        )


def approve_with(
    client_id: str,
    case_id: str,
    deps: VisitDeps,
    *,
    expected_version: int,
    actor: str,
    yeses: Sequence[GivenYes],
    principal: Principal | None,
    op_id: str | None = None,
    sentence: str | None = None,
    digest: str | None = None,
) -> Case:
    """Every yes the person was shown, then the approval, in one call.

    Checked first, nothing written until all pass: the case is waiting for approval; `digest`, when sent, is the
    approval's digest the person read; each yes given is one the plan needs, and each own-yes change has the role, its
    exact sentence and - with separation on - someone who did not ask for it. A yes the plan does not need (a stop
    that is gone, a change already confirmed) is left out. What is still needed after them is `approve`'s own refusal
    (`APPROVAL_STOP_OPEN`, `APPROVAL_CONFIRMATION_MISSING`)."""
    from engine.doctor import visit

    case = deps.case_store().get(client_id, case_id)
    if op_id is not None and visit.was_applied(case, op_id):  # a retry: the first attempt is written
        return visit.approve(
            client_id,
            case_id,
            deps,
            expected_version=expected_version,
            actor=actor,
            op_id=op_id,
            sentence=sentence,
        )
    if case.version != expected_version:
        raise CaseError(
            "CASE_VERSION_CONFLICT",
            f"The case changed since you loaded it (version {case.version}, you sent {expected_version}). "
            "Reload it and try again.",
        )
    if case.state is not VisitState.APPROVE:
        raise CaseError("VISIT_NOT_WAITING", "There is nothing waiting for approval now.")
    separate = visit.approval_is_required(deps, case)
    wanted = {
        y.subject: y for y in needs_yes(case, settings=deps.protocol, separate=separate, principal=principal)
    }
    if digest is not None:
        shown = approval_of(
            case,
            visit.approval_sentence(case, goal_name=visit.approval_goal_name(deps.storage, case)),
            settings=deps.protocol,
            separate=separate,
            principal=principal,
        )
        if shown.digest != digest:
            raise CaseError(
                "APPROVAL_SENTENCE_CHANGED",
                f"What would be approved has changed since you read it. It now reads: “{shown.sentence}”",
            )
    given = [g for g in yeses if g.subject in wanted]
    for each in given:
        if each.kind == "acknowledge" and (each.key is None):
            raise CaseError("VISIT_UNKNOWN_CHOICE", "A yes to a stop names the stop.")
        if each.kind != "confirm":
            continue
        need = wanted[each.subject]
        if principal is None:
            raise CaseError(
                "APPROVAL_CONFIRMATION_MISSING", "A change's own yes needs the person who gives it."
            )
        if each.sentence != need.sentence:
            raise CaseError(
                "APPROVAL_SENTENCE_CHANGED",
                f"What would be approved has changed since you read it. It now reads: “{need.sentence}”",
            )
        if not need.can_agree:
            raise CaseError(
                "POLICY_ROLE_SEPARATION" if need.cannot_agree == "separation" else "TOOL_ROLE_REQUIRED",
                "Someone else must give this one: the person who asked for it, or lacks the role, cannot.",
            )
    _refuse_what_is_left(case, deps, wanted, {g.subject for g in given}, sentence)
    version = expected_version
    for each in sorted(
        given, key=lambda g: g.kind != "acknowledge"
    ):  # a stop's yes does not move the plan: first
        if each.kind == "acknowledge":
            assert each.key is not None
            case = visit.acknowledge_stop(
                client_id,
                case_id,
                each.key,
                deps,
                expected_version=version,
                actor=actor,
                reason=each.reason,
                op_id=_sub_op(op_id, each.subject),
            )
        else:
            assert principal is not None and each.item_id is not None
            case = visit.confirm(
                client_id,
                case_id,
                each.item_id,
                deps,
                principal=principal,
                sentence=each.sentence,
                expected_version=version,
                op_id=_sub_op(op_id, each.subject),
            )
        version = case.version
    return visit.approve(
        client_id,
        case_id,
        deps,
        expected_version=version,
        actor=actor,
        op_id=op_id,
        sentence=sentence,
    )
