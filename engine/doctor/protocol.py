r"""The ask / approve / doubt protocol (Plan O M246, DEC-1816; the design §6.1, §6.3, §6.5-§6.8, §7.2).

Data Doctor may ask, ask for a yes, say it is unsure and ask for more data ([REQ] 3, 8). This module is the one
place that says **how**, as pure functions the visit (`engine.doctor.visit`) and Guided setup's Approve
(`engine.doctor.plan_g_approval`) call. Nothing here reads a file or calls an AI service.

**Questions (§6.5).** A question is asked only when the values cannot prove the answer, the options give
materially different data (more than `min_rows_changed_pct`, 0.5%, of the rows differ between them, or the share
of yes outcomes moves by more than `min_label_rate_change_pts`, 0.5 points) and one question settles it
(:func:`settle`):

* proven by the values -> **AUTO**: the proven option's item, ticked, shown, and the person may untick it;
* below the impact threshold -> **an assumption**: the recommended (or safe) option is ticked and the question is
  recorded as answered by the rules, `assumed`, with the assumption in plain words; answering it changes it;
* otherwise -> **one question**, with what each option was measured to do and an **"I don't know"** option that
  maps to the safe choice (hide, leave as text, stop), never to a guess. A meaning-bearing question recommends
  nothing.

The **budget** (§6.3): at most `ceil(2.5 x ambiguities)` questions a file, capped at 10. A question past the
budget that has a safe choice and does not block becomes a stated assumption of that safe choice.

**APPROVE-class items (§6.6)** need their own yes, the exact sentence shown and the role named in
:data:`APPROVE_CLASS_ITEMS`; with the use case's `governance.approval_required` on, someone other than the person
who proposed or answered it (separation of duties, O17). The check is M233's `data.approve_class` rule itself
(:func:`confirmation_refusal`), so the codes are its: `TOOL_ROLE_REQUIRED`, `APPROVAL_SENTENCE_CHANGED`,
`POLICY_ROLE_SEPARATION`. A sentence that changed records nothing; the answer carries the new one.

**Doubts (§6.7)** always have three parts - *I am not sure because*, *here is what I would do*, *here is what
changes if not* - and every number in them is grounded in a tool result (`numbers_grounded`), or the doubt is
refused (`DOUBT_UNGROUNDED`). **Requests for more data (§6.8)** name what to ask for, whom, why, and the shape of
the file wanted (:func:`request_data`).

**The record (§6.6).** Every decision is recorded with the user id, the time, the digest of what was decided
and the exact text. With `agentic.enabled` on it is also an entry of Plan K's ledger
(`data.prescription.approved`, `data.decision`; body keys `case_id`, `upload_id`, `prescription_hash`,
`recipe_hash`, `payload_digest`); with it off the case timeline holds the same record (:func:`decision_body`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import JsonValue

from engine.access.roles import Principal, Role
from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.doctor.case import CaseDoubt, CaseItem, CaseOption, CaseQuestion, payload_digest
from engine.doctor.visit_contracts import OptionEffect

if TYPE_CHECKING:
    from engine.agentic.policy import Decision

__all__ = [
    "AGENT",
    "APPROVE_CLASS_ITEMS",
    "DATA_DECISION",
    "DATA_PRESCRIPTION_APPROVED",
    "DONT_KNOW",
    "PROTOCOL_CODES",
    "ApproveClassRule",
    "Ask",
    "AskOption",
    "DataRequest",
    "LedgerPort",
    "ProtocolError",
    "ProtocolSettings",
    "Settled",
    "approval_digest",
    "approve_class_of",
    "confirmation_refusal",
    "decision_body",
    "doubt_ask",
    "express_doubt",
    "is_material",
    "item_kind",
    "item_sentence",
    "ledger_port",
    "question_budget",
    "request_data",
    "role_for",
    "settle",
    "ungrounded_in",
]

AGENT: Final[str] = "agent:data-doctor"
"""Who decided what the rules decided (an AUTO item, an assumption)."""

DONT_KNOW: Final[str] = "I don't know"
DATA_PRESCRIPTION_APPROVED: Final[str] = "data.prescription.approved"
DATA_DECISION: Final[str] = "data.decision"

PROTOCOL_CODES: Final[frozenset[str]] = frozenset(
    {"APPROVAL_CONFIRMATION_MISSING", "DOUBT_UNGROUNDED", "STANDING_ANSWER_NOT_FOUND"}
)
"""The codes M246 adds (for `PLAN_O_CODES` and `configs/pilot/help.yaml` at integration). Every other refusal reuses
the code the same refusal has: M233's `APPROVAL_SENTENCE_CHANGED`, Plan K's `TOOL_ROLE_REQUIRED` and
`POLICY_ROLE_SEPARATION`, M238's `RESHAPE_GUARD_MISSING`, `AI_STEP_CHANGES_MANY` and `AI_STEP_CHANGES_TOO_MANY`."""


class ProtocolError(Exception):
    """A refusal of the protocol, with its code (the visit turns it into its own error)."""

    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ProtocolSettings:
    """The protocol's bounds (the design §6.3, §6.5, §6.6). Defaults are the design's."""

    min_rows_changed_pct: float = 0.5
    """`ask.min_rows_changed_pct`: below this share of rows differing between the options, the rules decide."""
    min_label_rate_change_pts: float = 0.5
    """`ask.min_label_rate_change_pts`: below this move of the yes share (points), the rules decide."""
    questions_per_ambiguity: float = 2.5
    max_questions_per_file: int = 10
    approve_drop_share_pct: float = 2.0
    """A join, stack, de-duplication or keep-latest leaving out more than this share of rows needs a yes."""


DEFAULT_SETTINGS: Final[ProtocolSettings] = ProtocolSettings()


# ---------------------------------------------------------------------------
# ASK: questions with the impact rule and "I don't know" (§6.5)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AskOption:
    """One answer, with what it was measured to do (rows it changes; the yes share with it, in percent)."""

    label: str
    effect: OptionEffect
    rows_changed: int | None = None
    label_rate: float | None = None
    safe: bool = False
    """The safe choice (hide, leave as text, stop): what "I don't know" and a question past the budget map to."""


@dataclass(frozen=True)
class Ask:
    """An ambiguity a rule or a detector found: what it would ask, and what settles it without asking."""

    code: str
    subject: Mapping[str, JsonValue]
    """What it is about (a file, a column): with `code` and the goal, the question's stable id."""
    text: str
    why: str
    options: tuple[AskOption, ...]
    rows: int
    """The rows the options were measured on."""
    rows_disagree: int | None = None
    """Rows on which the options give different data (for example the values both date orders read and
    disagree on); when None, the spread of the options' `rows_changed`."""
    upload_id: str | None = None
    column: str | None = None
    goal_id: str | None = None
    blocking: bool = False
    proven: str | None = None
    """The option the values prove (AUTO), if they prove one."""
    recommended: str | None = None
    meaning_bearing: bool = False
    """A question about meaning (the outcome, consent, which column is which): nothing is recommended."""
    value_signature: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settled:
    """What the protocol made of an :class:`Ask`: an AUTO item, a stated assumption, or one question."""

    how: Literal["auto", "assume", "ask"]
    question: CaseQuestion | None
    effects: tuple[OptionEffect, ...]
    item: CaseItem | None
    reason: str = ""


def _short(value: Any) -> str:
    return payload_digest({"v": value})[:12]


def question_id_of(code: str, subject: Mapping[str, JsonValue], goal_id: str | None) -> str:
    """The id the visit gives a question: its code, its goal (none for the base) and its subject."""
    return f"q.{code.lower()}.{goal_id or 'base'}.{_short(dict(subject))}"[:128]


def is_material(ask: Ask, settings: ProtocolSettings = DEFAULT_SETTINGS) -> bool:
    """Whether the options give materially different data (§6.5 rule 2). Unmeasured counts as material."""
    options = list(ask.options)
    if ask.rows <= 0 or len(options) < 2:
        return True
    if ask.rows_disagree is not None:
        differ = ask.rows_disagree
    else:
        changed = [option.rows_changed for option in options]
        if any(value is None for value in changed):
            return True
        counted = [int(value) for value in changed if value is not None]
        differ = max(counted) - min(counted)
    if differ / ask.rows * 100.0 > settings.min_rows_changed_pct:
        return True
    rates = [option.label_rate for option in options if option.label_rate is not None]
    return len(rates) >= 2 and max(rates) - min(rates) > settings.min_label_rate_change_pts


def question_budget(ambiguities: int, settings: ProtocolSettings = DEFAULT_SETTINGS) -> int:
    """At most `ceil(2.5 x ambiguities)` questions about one file, capped at 10 (§6.3)."""
    if ambiguities <= 0:
        return 0
    return min(settings.max_questions_per_file, math.ceil(settings.questions_per_ambiguity * ambiguities))


def _with_dont_know(ask: Ask) -> tuple[AskOption, ...]:
    if any(option.safe for option in ask.options):
        return ask.options
    used = {option.effect.option_id for option in ask.options}
    option_id = "dont_know" if "dont_know" not in used else "dont_know_2"
    return (*ask.options, AskOption(label=DONT_KNOW, effect=OptionEffect(option_id=option_id), safe=True))


def _item_for(
    question_id: str,
    effect: OptionEffect,
    ask: Ask,
    *,
    now: datetime,
    confidence: AgentConfidence,
    assumption: str | None = None,
) -> CaseItem | None:
    if effect.kind is None:
        return None
    upload = ask.upload_id
    if upload is None:
        given = effect.payload.get("upload_id")
        upload = given if isinstance(given, str) else None
    return CaseItem(
        item_id=f"{question_id}:{effect.option_id}"[:128],
        kind=effect.kind,
        title=effect.title,
        source=ProposalSource.RULES,
        confidence=confidence,
        state=ProposalState.ACCEPTED,
        upload_id=upload,
        evidence_ids=ask.evidence_ids,
        payload=dict(effect.payload),
        payload_digest=payload_digest(effect.payload),
        proposed_at=now,
        decided_by=AGENT,
        decided_at=now,
        goal_id=ask.goal_id,
        assumption=assumption,
    )


def _case_question(ask: Ask, question_id: str, options: tuple[AskOption, ...]) -> CaseQuestion:
    recommended = None if ask.meaning_bearing else ask.recommended
    return CaseQuestion(
        question_id=question_id,
        code=ask.code,
        upload_id=ask.upload_id,
        column=ask.column,
        text=ask.text,
        why=ask.why,
        options=tuple(
            CaseOption(
                option_id=option.effect.option_id,
                label=option.label,
                effect_digest=(
                    payload_digest(option.effect.payload) if option.effect.kind is not None else None
                ),
                rows_changed=option.rows_changed,
                label_rate=option.label_rate,
                safe=option.safe,
            )
            for option in options
        ),
        blocking=ask.blocking,
        recommended=recommended,
        goal_id=ask.goal_id,
        value_signature=ask.value_signature,
    )


def settle(
    ask: Ask,
    *,
    now: datetime,
    settings: ProtocolSettings = DEFAULT_SETTINGS,
    over_budget: bool = False,
) -> Settled:
    """AUTO, a stated assumption, or one question (see the module docstring)."""
    question_id = question_id_of(ask.code, ask.subject, ask.goal_id)
    effects = {option.effect.option_id: option for option in ask.options}
    if ask.proven is not None and ask.proven in effects:
        item = _item_for(
            question_id, effects[ask.proven].effect, ask, now=now, confidence=AgentConfidence.SURE
        )
        return Settled(how="auto", question=None, effects=(), item=item, reason="the values prove it")
    options = _with_dont_know(ask)
    material = is_material(ask, settings)
    budget_hit = over_budget and not ask.blocking
    if material and not budget_hit:
        question = _case_question(ask, question_id, options)
        return Settled(how="ask", question=question, effects=tuple(o.effect for o in options), item=None)
    safe = next(option for option in options if option.safe)
    if not material and not ask.meaning_bearing and ask.recommended in effects:
        chosen = effects[str(ask.recommended)]
    else:
        chosen = safe
    if material:
        why = (
            f"Data Doctor has already asked its {settings.max_questions_per_file} questions about this file, "
            f"so it took the safe choice: {chosen.label}."
        )
    else:
        differ = ask.rows_disagree
        if differ is None:
            known = [o.rows_changed for o in ask.options if o.rows_changed is not None]
            differ = (max(known) - min(known)) if known else 0
        why = (
            f"I took “{chosen.label}”: the choices differ on only {differ:,} of {ask.rows:,} rows, too few to "
            "ask about. Change it if it is wrong."
        )
    question = _case_question(ask, question_id, options).model_copy(
        update={"answer": chosen.effect.option_id, "answered_by": AGENT, "answered_at": now, "assumed": True}
    )
    item = _item_for(
        question_id, chosen.effect, ask, now=now, confidence=AgentConfidence.CHECK, assumption=why
    )
    return Settled(
        how="assume", question=question, effects=tuple(o.effect for o in options), item=item, reason=why
    )


# ---------------------------------------------------------------------------
# APPROVE: what needs the person's explicit yes (§6.6)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ApproveClassRule:
    """One APPROVE-class item kind: the role its yes needs, and why it needs one."""

    role: Role
    why: str
    drop_share: bool = False
    """Only when it leaves out more than `approve_drop_share_pct` of the rows (unknown counts as more)."""
    approver_when_separated: bool = False
    """Analyst, or Approver when the use case sets `governance.approval_required`."""


APPROVE_CLASS_ITEMS: Final[Mapping[str, ApproveClassRule]] = {
    "acknowledgement": ApproveClassRule(Role.ANALYST, "it keeps a known risk", approver_when_separated=True),
    "consent_column": ApproveClassRule(Role.APPROVER, "it decides who may be contacted"),
    "map_consent": ApproveClassRule(Role.APPROVER, "it decides who may be contacted"),
    "suppression": ApproveClassRule(Role.APPROVER, "it decides who may be contacted"),
    "pii_keep": ApproveClassRule(Role.APPROVER, "it puts personal data into a model"),
    "threshold": ApproveClassRule(Role.APPROVER, "it loosens a safeguard"),
    "define_outcome": ApproveClassRule(Role.ANALYST, "it decides what the model learns"),
    "map_outcome_values": ApproveClassRule(Role.ANALYST, "it decides what the model learns"),
    "outcome_definition": ApproveClassRule(Role.ANALYST, "it decides what the model learns"),
    "convert_currency": ApproveClassRule(Role.ANALYST, "it changes every amount"),
    "join_files": ApproveClassRule(Role.ANALYST, "it leaves rows out", drop_share=True),
    "stack_files": ApproveClassRule(Role.ANALYST, "it leaves rows out", drop_share=True),
    "build_dataset": ApproveClassRule(Role.ANALYST, "it leaves rows out", drop_share=True),
    "keep_latest_per_id": ApproveClassRule(Role.ANALYST, "it leaves rows out", drop_share=True),
    "sql_query": ApproveClassRule(Role.ANALYST, "it makes a new table with a query", drop_share=True),
    "split_file": ApproveClassRule(Role.ANALYST, "it chooses which customers are learnt from"),
    "remove_rows": ApproveClassRule(Role.ANALYST, "it chooses which customers are learnt from"),
}
"""The design §6.6's list, by item kind (a recipe step is read by its step kind, :func:`item_kind`)."""


def item_kind(kind: str, payload: Mapping[str, Any]) -> str:
    """An item's kind as the APPROVE-class list reads it: a recipe step by its step kind."""
    if kind in {"recipe_step", "step"}:
        step = payload.get("step")
        if isinstance(step, Mapping) and isinstance(step.get("kind"), str):
            return str(step["kind"])
        if isinstance(payload.get("kind"), str):
            return str(payload["kind"])
    return kind


def _drop_share(payload: Mapping[str, Any]) -> float | None:
    rows_in, rows_out = payload.get("rows_in"), payload.get("rows_out")
    if not isinstance(rows_in, int) or not isinstance(rows_out, int):
        return None
    if rows_in <= 0:
        return 0.0
    return max(0, rows_in - rows_out) / rows_in * 100.0


def approve_class_of(
    kind: str, payload: Mapping[str, Any], settings: ProtocolSettings = DEFAULT_SETTINGS
) -> ApproveClassRule | None:
    """The APPROVE-class rule an item falls under, or None for an item the overall Approve covers."""
    rule = APPROVE_CLASS_ITEMS.get(item_kind(kind, payload))
    if rule is None:
        return None
    if rule.drop_share:
        share = _drop_share(payload)
        if share is not None and share <= settings.approve_drop_share_pct:
            return None
    return rule


def role_for(rule: ApproveClassRule, *, approval_required: bool) -> Role:
    """The role a yes needs: the rule's, raised to Approver when separation is on and the rule says so."""
    if rule.approver_when_separated and approval_required:
        return Role.APPROVER
    return rule.role


def item_sentence(title: str, payload: Mapping[str, Any]) -> str:
    """The exact sentence an APPROVE-class item is approved by: its own `sentence`, else its title."""
    sentence = payload.get("sentence")
    return sentence if isinstance(sentence, str) and sentence.strip() else title


def confirmation_refusal(
    *,
    rule: ApproveClassRule,
    approver: Principal,
    sentence_read: str | None,
    sentence_now: str,
    proposed_by: str | Sequence[str] | None,
    approval_required: bool,
) -> Decision | None:
    """M233's `data.approve_class` rule at approval time, for one APPROVE-class item; None when it may be approved.

    The approver holds the role (`TOOL_ROLE_REQUIRED`), approves the sentence the server shows now word for word
    (`APPROVAL_SENTENCE_CHANGED`), and - with separation on - did not propose or answer it themselves
    (`POLICY_ROLE_SEPARATION`). `proposed_by` names everyone who proposed or answered it (the person who
    answered its question, the person who ticked it); the approver may be none of them."""
    proposers: tuple[str | None, ...] = (
        (proposed_by,)
        if isinstance(proposed_by, str) or proposed_by is None
        else tuple(proposed_by) or (None,)
    )
    for proposer in proposers:
        found = _approve_class_refusal(
            rule, approver, sentence_read, sentence_now, proposer, approval_required
        )
        if found is not None:
            return found
    return None


def _approve_class_refusal(
    rule: ApproveClassRule,
    approver: Principal,
    sentence_read: str | None,
    sentence_now: str,
    proposed_by: str | None,
    approval_required: bool,
) -> Decision | None:
    from engine.doctor import policy

    rules = policy.DataRuleSet(
        rules=(policy.APPROVE_CLASS,),
        proposes="plan",
        approve="always",
        approve_role=role_for(rule, approval_required=approval_required),
    )
    ctx = policy.DataPolicyContext(
        policy=policy.DataPolicyConfig(),
        approval=policy.ApprovalFacts(
            approver=approver,
            approved_sentence=sentence_read,
            current_sentence=sentence_now,
            approval_required=approval_required,
        ),
    )
    subject = policy.DataProposal(
        kind="data.protocol", source="person", proposed_by=proposed_by, sentence=sentence_now
    )
    decision = policy.approve_class(subject, ctx, rules)
    return None if decision is None or decision.allowed else decision


# ---------------------------------------------------------------------------
# Doubts (§6.7) and requests for more data (§6.8)
# ---------------------------------------------------------------------------
def express_doubt(
    code: str,
    *,
    because: str,
    would_do: str,
    if_not: str,
    evidence: Mapping[str, Any],
    names: Iterable[str] = (),
    upload_id: str | None = None,
    item_id: str | None = None,
    goal_id: str | None = None,
) -> CaseDoubt:
    """A doubt in three parts; every number in it must be in the cited tool results (`evidence`, by id).

    Raises `ProtocolError("DOUBT_UNGROUNDED")` when a part is empty or a number is in no result. Column names
    (`names`) are not numbers, so `visits_90d` grounds nothing and needs no grounding.
    """
    parts = [because.strip(), would_do.strip(), if_not.strip()]
    if not all(parts):
        raise ProtocolError("DOUBT_UNGROUNDED", "A doubt says why, what I would do, and what changes if not.")
    subject = {"code": code, "upload": upload_id, "item": item_id, "goal": goal_id, "parts": parts}
    doubt = CaseDoubt(
        doubt_id=f"d.{code.lower()}.{goal_id or 'base'}.{_short(subject)}"[:128],
        because=parts[0],
        would_do=parts[1],
        if_not=parts[2],
        upload_id=upload_id,
        item_id=item_id,
        evidence_ids=tuple(sorted(evidence)),
    )
    loose = ungrounded_in(doubt, evidence, names)
    if loose:
        raise ProtocolError(
            "DOUBT_UNGROUNDED",
            f"A number in this doubt ({loose[0][:20]}) is in none of the results it cites.",
        )
    return doubt


def ungrounded_in(doubt: CaseDoubt, evidence: Mapping[str, Any], names: Iterable[str] = ()) -> list[str]:
    """The numbers of a doubt's three parts that none of `evidence` (results by id) holds; empty when grounded."""
    from engine.agent.grounding import grounded_numbers, ungrounded_numbers

    grounded = grounded_numbers(list(evidence.values()))
    named = tuple(names)
    return [
        token
        for part in (doubt.because, doubt.would_do, doubt.if_not)
        for token in ungrounded_numbers(part, grounded, named)
    ]


def doubt_ask(doubt: CaseDoubt) -> Ask:
    """A doubt about meaning also asks, so approval waits for the person (§6.7: a doubt blocks only then).

    The answers change no data (they carry no effect): the person says whether the reading is right, and the
    answer is recorded with who and when. Nothing is recommended and "I don't know" is added by :func:`settle`.
    """
    return Ask(
        code="DOUBT_MEANING",
        subject={"doubt": doubt.doubt_id},
        text=(
            f"Data Doctor is not sure: {doubt.because.rstrip('.')}. What it would do: "
            f"{doubt.would_do.rstrip('.')}. If not: {doubt.if_not.rstrip('.')}. Is this right?"
        ),
        why="It is about what a column means, which only you can say.",
        options=(
            AskOption(label="Yes, that is right", effect=OptionEffect(option_id="right")),
            AskOption(label="No, it is not right", effect=OptionEffect(option_id="wrong")),
        ),
        rows=0,
        upload_id=doubt.upload_id,
        blocking=True,
        meaning_bearing=True,
        evidence_ids=doubt.evidence_ids,
    )


@dataclass(frozen=True)
class DataRequest:
    """When no choice makes the data usable: what to collect, from whom, why, and the file shape wanted."""

    kind: Literal["more_data", "different_file"]
    what: str
    """"the date of each customer's last order" - what is missing, in plain words."""
    why: str
    """"Win back needs it." """
    columns: tuple[str, ...] = ()
    per: str = "one row per customer"
    ask_whom: str | None = None
    upload_id: str | None = None
    goal_id: str | None = None
    evidence_ids: tuple[str, ...] = field(default=())


REQUEST_CODES: Final[Mapping[str, str]] = {
    "more_data": "DATA_REQUESTED",
    "different_file": "DIFFERENT_FILE_REQUESTED",
}


def request_data(request: DataRequest) -> tuple[CaseQuestion, tuple[OptionEffect, ...]]:
    """The request as one non-blocking question: "I will add it" (the visit waits for the file) or "I cannot get
    it" (the safe choice: the goal stays not ready, with the reason). Its id is its subject, so it is asked once.
    """
    code = REQUEST_CODES[request.kind]
    whom = f"Ask {request.ask_whom} for" if request.ask_whom else "Please add"
    if request.kind == "different_file":
        text = (
            f"This file cannot be used as it is. {whom} a different file with {request.what}; {request.why}"
        )
    else:
        text = f"{whom} {request.what}; {request.why}"
    shape = ", ".join(request.columns)
    why = f"The file needed: {request.per}" + (f", with {shape}." if shape else ".")
    subject: dict[str, JsonValue] = {
        "kind": request.kind,
        "what": request.what,
        "upload": request.upload_id,
        "columns": list(request.columns),
    }
    question_id = question_id_of(code, subject, request.goal_id)
    effects = (OptionEffect(option_id="will_add"), OptionEffect(option_id="cannot"))
    question = CaseQuestion(
        question_id=question_id,
        code=code,
        upload_id=request.upload_id,
        text=text.strip(),
        why=why,
        options=(
            CaseOption(option_id="will_add", label="I will add it"),
            CaseOption(option_id="cannot", label="I cannot get it", safe=True),
        ),
        blocking=False,
        goal_id=request.goal_id,
    )
    return question, effects


# ---------------------------------------------------------------------------
# The record: the ledger when `agentic.enabled`, the timeline always (§6.6)
# ---------------------------------------------------------------------------
LedgerPort = Callable[[str, str, Mapping[str, Any]], str]
"""`(kind, actor, body) -> entry_hash`: appends one entry to Plan K's ledger and returns its hash (the receipt)."""


def decision_body(
    *,
    case_id: str | None,
    upload_id: str | None,
    item_digests: Sequence[str],
    text: str,
    decided_by: str,
    proposed_by: str | Sequence[str] | None = None,
    kind: str | None = None,
    prescription_hash: str | None = None,
    recipe_hash: str | None = None,
    identity_verified: bool | None = None,
    approval_id: str | None = None,
    case_version: int | None = None,
) -> dict[str, Any]:
    """One decision as ledger body keys (ids and digests, never a value), and as the timeline event's details.

    `payload_digest` is the sha256 of the items' payload digests and the sentence read, so the exact text is
    bound into the record without the record holding it. `approval_id` and `case_version` name the case's approval
    and the version it was decided on, so a retried write finds the entry its first attempt made
    (:func:`ledger_port`). `proposed_by` names everyone who proposed or answered it, comma-separated when there are
    several (a timeline detail is one scalar).
    """
    proposers = [proposed_by] if isinstance(proposed_by, str) else sorted(set(proposed_by or ()))
    body: dict[str, Any] = {
        "case_id": case_id,
        "upload_id": upload_id,
        "payload_digest": approval_digest(item_digests, text),
        "prescription_hash": prescription_hash,
        "recipe_hash": recipe_hash,
        "status": "approved",
        "decided_by": decided_by,
        "proposed_by": ",".join(proposers) or None,
        "proposal_kind": kind,
    }
    if identity_verified is not None:
        body["identity_verified"] = identity_verified
    if approval_id is not None:
        body["approval_id"] = approval_id
    if case_version is not None:
        body["case_version"] = case_version
    return body


def approval_digest(item_digests: Sequence[str], text: str) -> str:
    """sha256 of what a yes covers: the items' payload digests and the sentence read, word for word."""
    return payload_digest({"items": sorted(item_digests), "text": text})


def ledger_port(engine: Any, signer: Any, policy_version: str) -> LedgerPort:
    """A :data:`LedgerPort` writing to the deployment's chain of Plan K's ledger.

    **Idempotent.** An entry of the same kind, by the same actor, with the same body is written once: a retried
    write (its first attempt appended, then the case write failed or the process died before it) gets the first
    entry's hash back instead of a second entry. A case's decision body names the approval and the case version
    it was decided on (:func:`decision_body`), so two different decisions never share a body. Data Doctor's kinds
    are few (one per person's approval), so the look-up reads that kind's entries only, newest last.
    """
    from engine.agentic.canonical import jsonable
    from engine.agentic.ledger import DecisionLedger

    ledger = DecisionLedger(engine, signer=signer, policy_version=policy_version)

    def record(kind: str, actor: str, body: Mapping[str, Any]) -> str:
        kept = {k: v for k, v in body.items() if v is not None}
        wanted = jsonable({**kept, "policy_version": ledger.policy_version})
        for entry in ledger.entries(kind=kind):
            if entry.actor == actor and entry.body == wanted:
                return entry.entry_hash
        return ledger.append(kind, actor, kept).entry_hash

    return record
