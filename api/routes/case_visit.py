"""The Data Doctor visit over a case (Plan O M235, DEC-1805): resume it, choose a goal, answer, decide, approve, chat.

Mounted only when `data_doctor.unified` is on (the `PLAN-O (M235)` hook in `api/main.py`, beside M231's): with it
off - the default - none of these paths exists and docs/API.md is unchanged.

| Method and path | Role | Does |
|---|---|---|
| `POST /cases/{case_id}/visit` | Analyst | Continues the visit from the last state written, to its next rest. |
| `POST /cases/{case_id}/goal` | Analyst | Chooses, switches or clears (`null`) the goal; nothing decided is asked again. |
| `POST /cases/{case_id}/answers` | Analyst | Answers a question with one of its options. |
| `POST /cases/{case_id}/decisions` | Analyst | Ticks or unticks a suggestion of the active section. |
| `POST /cases/{case_id}/approval` | Analyst | Approves what the preview showed; recorded with the user id and the sentence shown. |
| `POST /cases/{case_id}/chat` | Analyst | Writes to Data Doctor; with no AI service the reply says so and nothing else changes. |
| `POST /cases/{case_id}/confirmations` | Analyst | Plan O M246: the own yes of one APPROVE-class item, with the exact sentence read (the item's role is checked). |
| `POST /cases/{case_id}/standing-answers/{answer_id}/revoke` | Analyst | Plan O M246: revokes a standing answer; its question is asked again. |
| `GET /cases/{case_id}/ai` | Viewer | Whether the client has let the Doctor's AI read masked summaries (who and when), and what the case has used of its AI limit. |
| `POST /cases/{case_id}/ai-consent` | Analyst | Records, once for the client, that the Doctor's AI may read masked summaries of its data. |
| `GET /goal-briefs/{goal_id}` | Viewer | What a goal needs (the goal brief), from configuration. |

Every change names the case `version` it was made against (`409 CASE_VERSION_CONFLICT` when it moved) and may carry
an `op_id`: a retried request whose first attempt was written changes nothing. Refusals: `404 CASE_NOT_FOUND`,
`404 USE_CASE_NOT_FOUND` (a goal that is not a use case), `409 VISIT_NOT_WAITING` (nothing waits for that now),
`422 VISIT_UNKNOWN_CHOICE` (no such question, option or suggestion in the active section).

Plan O M246 (DEC-1816): `POST .../approval` may carry the `sentence` the person read (`409
APPROVAL_SENTENCE_CHANGED` when it is no longer the one shown: nothing is recorded) and is refused while an
accepted APPROVE-class item lacks its own yes (`409 APPROVAL_CONFIRMATION_MISSING`). A confirmation is refused
with `403 TOOL_ROLE_REQUIRED` (the item's role), `403 POLICY_ROLE_SEPARATION` (separation of duties) or `409
APPROVAL_SENTENCE_CHANGED`. With `agentic.enabled` on, approvals and confirmations are also written to Plan K's
ledger (`data.prescription.approved`, `data.decision`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as Path_
from pydantic import Field

from api.access import PrincipalDep, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep, get_settings
from api.routes.uploads import http_error, load_upload, load_upload_profile
from api.schemas import ErrorResponse
from engine.access.roles import Principal, Role
from engine.config import ConfigError, StrictBase
from engine.doctor import visit as doctor_visit
from engine.doctor.case import CLIENT_ID_PATTERN, DEFAULT_CLIENT_ID, Case, CaseError
from engine.doctor.diagnose import LoadedFile
from engine.doctor.goal_brief import GOAL_ID_PATTERN, BriefMode, GoalBrief, load_goal_brief
from engine.doctor.protocol import LedgerPort
from engine.doctor.visit import ConversePort, VisitDeps
from engine.storage import Storage

__all__ = ["POLICIES", "ledger_for", "router"]

router: APIRouter = APIRouter(tags=["cases"])


def _policy(action: str, purpose: str) -> RoutePolicy:
    return RoutePolicy(
        role=Role.ANALYST, action=action, purpose=purpose, object_type="case", object_param="case_id"
    )


POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("POST", "/cases/{case_id}/visit"): _policy("cases.visit", "continue a Data Doctor visit"),
    ("POST", "/cases/{case_id}/goal"): _policy("cases.goal", "choose the goal of a Data Doctor case"),
    ("POST", "/cases/{case_id}/answers"): _policy("cases.answer", "answer Data Doctor's question"),
    ("POST", "/cases/{case_id}/decisions"): _policy(
        "cases.decide", "tick or untick a Data Doctor suggestion"
    ),
    ("POST", "/cases/{case_id}/approval"): _policy("cases.approve", "approve Data Doctor's changes"),
    ("POST", "/cases/{case_id}/chat"): _policy("cases.chat", "write to Data Doctor"),
    # Plan O M246 (DEC-1816): the protocol's actions on a case. The item's own role is checked by the protocol.
    ("POST", "/cases/{case_id}/confirmations"): _policy(
        "cases.confirm", "give the own yes to one of Data Doctor's changes"
    ),
    ("POST", "/cases/{case_id}/standing-answers/{answer_id}/revoke"): _policy(
        "cases.revoke_standing", "revoke a remembered answer"
    ),
    ("POST", "/cases/{case_id}/ai-consent"): _policy(
        "cases.ai_consent", "let the Doctor's AI read masked summaries of a client's data"
    ),
    ("GET", "/cases/{case_id}/ai"): RoutePolicy(
        role=Role.VIEWER,
        action="cases.ai_status",
        purpose="see whether the Doctor's AI may read a client's data and what it has used",
        object_type="case",
        object_param="case_id",
    ),
    ("GET", "/goal-briefs/{goal_id}"): RoutePolicy(
        role=Role.VIEWER, action="doctor.goal_brief_read", purpose="see what a goal needs"
    ),
}
register(POLICIES)

_ERRORS: Final[dict[int | str, dict[str, object]]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
DIAGNOSIS_ROWS: Final[int] = 4_000_000
"""Rows one visit reads across its files, as the Doctor's diagnosis (M161, DEC-1507)."""

ClientId = Annotated[
    str, Field(pattern=CLIENT_ID_PATTERN, description="Whose case; `default` single-tenant.")
]
OpId = Annotated[
    str | None,
    Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
        description="Idempotency key: a retried request with the same key changes nothing.",
    ),
]


class ResumeRequest(StrictBase):
    client_id: ClientId = DEFAULT_CLIENT_ID


class _Change(StrictBase):
    client_id: ClientId = DEFAULT_CLIENT_ID
    version: int = Field(ge=1, description="The case version the caller read; a stale one is refused (409).")
    op_id: OpId = None


class GoalRequest(_Change):
    goal_id: str | None = Field(
        pattern=GOAL_ID_PATTERN, description="The goal (a use case id); null: no goal."
    )


class AnswerRequest(_Change):
    question_id: str = Field(min_length=1, max_length=128)
    option_id: str = Field(min_length=1, max_length=64)


class DecisionRequest(_Change):
    item_id: str = Field(min_length=1, max_length=128)
    accept: bool = Field(description="True ticks it; false unticks it.")


class ApprovalRequest(_Change):
    sentence: str | None = Field(
        default=None,
        max_length=4_000,
        description="Plan O M246: the sentence the person read, word for word; a changed one records nothing.",
    )


class ConfirmationRequest(_Change):
    """Plan O M246: the own yes of one APPROVE-class item."""

    item_id: str = Field(min_length=1, max_length=128)
    sentence: str = Field(min_length=1, max_length=4_000, description="The item's sentence, word for word.")


class ChatRequest(_Change):
    message: str = Field(min_length=1, max_length=4_000)


class AiStatus(StrictBase):
    """Answer of `GET /cases/{case_id}/ai`."""

    consent: bool = Field(description="True once the client has let the Doctor's AI read masked summaries.")
    given_by: str | None = None
    given_at: datetime | None = None
    tokens_used: int
    tokens_limit: int
    calls_used: int
    calls_limit: int
    spent: bool = Field(
        description="True when the case has used up its AI limit: the Doctor uses its rules only."
    )


def _error(exc: CaseError) -> Exception:
    status = {
        "CASE_NOT_FOUND": 404,
        "VISIT_UNKNOWN_CHOICE": 422,
        "STANDING_ANSWER_NOT_FOUND": 404,  # Plan O M246
        "TOOL_ROLE_REQUIRED": 403,
        "POLICY_ROLE_SEPARATION": 403,
    }.get(exc.code, 409)
    return http_error(status, exc.code, exc.message)


def ledger_for(request: Request, root: Path) -> LedgerPort | None:
    """Plan O M246 (DEC-1816): Plan K's ledger as the protocol's port while `agentic.enabled` is on, built on its
    first entry; None with it off (the case timeline then holds the record alone)."""
    from engine.agentic.config import agentic_enabled

    if not agentic_enabled(root):
        return None
    built: list[LedgerPort] = []

    def record(kind: str, actor: str, body: Mapping[str, Any]) -> str:
        if not built:
            from api.access import get_platform_engine, platform_data_dir
            from engine.agentic.config import policy_version
            from engine.agentic.ledger import signer_for_data_dir
            from engine.doctor.protocol import ledger_port

            data_dir = platform_data_dir(request) or get_settings(request).data_dir
            built.append(
                ledger_port(get_platform_engine(request), signer_for_data_dir(data_dir), policy_version(root))
            )
        return built[0](kind, actor, body)

    return record


def _goal_error(exc: ConfigError) -> Exception:
    del exc  # the configuration's own message names files; the answer does not
    return http_error(404, "USE_CASE_NOT_FOUND", "There is no goal with that name here.")


def _load_files(storage: Storage, upload_ids: Sequence[str], cap_of: int) -> tuple[LoadedFile, ...]:
    """The case's files as the Doctor's diagnosis reads them: rows capped, the profile, the intake notes."""
    from engine.doctor.diagnose import ADVISE_ROWS
    from engine.intake.contracts import IntakeReport
    from engine.stages import ingest

    if not upload_ids:
        return ()
    cap = min(cap_of, max(ADVISE_ROWS, DIAGNOSIS_ROWS // len(upload_ids)))
    loaded: list[LoadedFile] = []
    for upload_id in upload_ids:
        record = load_upload(storage, upload_id)
        profile = load_upload_profile(storage, upload_id)
        frame = ingest.read_upload(
            storage, record.source_key, file_format=record.file_format, max_rows=cap, row_cap=cap
        ).frame
        notes: tuple[str, ...] = ()
        if record.intake_key is not None and storage.exists(record.intake_key):
            notes = storage.read_model(record.intake_key, IntakeReport).notes
        loaded.append(
            LoadedFile(
                upload_id=upload_id, file_name=record.file_name, frame=frame, profile=profile, notes=notes
            )
        )
    return tuple(loaded)


def _converse(
    storage: Storage, root: Path, request: Request, principal: Principal, offers: Sequence[Any]
) -> ConversePort | None:
    """Plan G's chat turn when an AI service is connected; None (the chat says it needs one) otherwise."""
    from api.routes.ai_service import resolve_slot
    from engine.agent.tools import AgentContext
    from engine.config import RunMode, load_use_case
    from engine.doctor.ai_guard import AiConsentStore, AiGuard
    from engine.generative.guardrails import Guardrails, load_policy

    if not offers:
        return None
    default = offers[0].config
    try:
        ai = resolve_slot(
            default.generative.llm, slot="product", storage=storage, settings=get_settings(request)
        )
    except HTTPException:
        return None
    # Every Doctor AI call goes through this one guard: the case's spending limit and the client's consent.
    meter = AiGuard(
        ai.client,
        job_id="data-doctor",
        llm=ai.llm,
        budget=default.generative.budget,
        limits=default.data_doctor.ai_limits,
        consented=lambda client_id: AiConsentStore(storage).get(client_id) is not None,
    )
    guardrails = Guardrails(load_policy(root), meter=meter, prompts_root=root)

    def context_for(case: Case, files: tuple[LoadedFile, ...]) -> AgentContext:
        goal = doctor_visit.active_goal(case)
        config = load_use_case(goal.goal_id, root) if goal is not None else default
        chosen = files[0]
        return AgentContext(
            use_case_id=config.id,
            config=config,
            config_root=root,
            upload_id=chosen.upload_id,
            mode=RunMode.TRAIN,
            profile=chosen.profile,
            frame=chosen.frame,
        )

    return doctor_visit.plan_g_converse(
        context_for, meter=meter, guardrails=guardrails, principal=principal, config_root=root, guard=meter
    )


def _deps(storage: Storage, root: Path, request: Request, principal: Principal | None) -> VisitDeps:
    """Every visit route carries the CONVERSE port, connected only when a chat turn runs: a visit resumed in
    CONVERSE (the process died during the AI call) is answered by the AI service, not told there is none."""
    from api.routes.case_combine import case_combiner
    from api.routes.doctor import offers as doctor_offers
    from engine.doctor.cleaning import cleaning_detector
    from engine.doctor.combining import combining_detector
    from engine.doctor.prescription import CasePrescriber
    from engine.doctor.sensitive_ask import sensitive_detector
    from engine.doctor.time_settings import time_settings_detector
    from engine.stages.ingest import profile_row_cap

    offers = doctor_offers(root)
    cap = min((profile_row_cap(offer.config) for offer in offers), default=DIAGNOSIS_ROWS)

    def load_files(case: Case) -> tuple[LoadedFile, ...]:
        return _load_files(storage, case.upload_ids(), cap)

    return VisitDeps(
        storage=storage,
        load_files=load_files,
        # Plan O M243 (DEC-1813): PRESCRIBE writes `prescriptions/<hash>.json` and binds the case's files to it.
        prescribe=CasePrescriber(storage, load_files=load_files, config_root=root),
        # Several files: an approved join or stack is built from the whole files as a new upload of the case.
        combine=case_combiner(storage, root),
        offers=offers,
        config_root=root,
        converse=(
            None
            if principal is None
            else doctor_visit.lazy_converse(lambda: _converse(storage, root, request, principal, offers))
        ),
        ledger=ledger_for(request, root),
        # Plan O M244 (DEC-1814): the time settings - the training budget's three choices and the data time
        # settings - asked through M246's protocol, for a goal.
        # The cleaning (Guided setup's format fixes, the Indian-format checks, the mixed-currency stop), no AI.
        detectors=(
            cleaning_detector(offers, config_root=root),
            combining_detector(),
            sensitive_detector(),
            time_settings_detector(storage, config_root=root),
        ),
    )


def _done(request: Request, case: Case) -> Case:
    set_audit_context(request, object_id=case.case_id, object_type="case")
    return case


@router.post("/cases/{case_id}/visit", response_model=Case, responses=_ERRORS, summary="Continue a visit")
def resume_visit(
    case_id: str,
    body: ResumeRequest,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    principal: PrincipalDep,
) -> Case:
    try:
        case = doctor_visit.resume(
            body.client_id, case_id, _deps(storage, root, request, principal), actor=principal.user_id
        )
    except CaseError as exc:
        raise _error(exc) from exc
    return _done(request, case)


@router.post("/cases/{case_id}/goal", response_model=Case, responses=_ERRORS, summary="Choose a goal")
def choose_goal(
    case_id: str,
    body: GoalRequest,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    principal: PrincipalDep,
) -> Case:
    try:
        case = doctor_visit.set_goal(
            body.client_id,
            case_id,
            body.goal_id,
            _deps(storage, root, request, principal),
            expected_version=body.version,
            actor=principal.user_id,
            op_id=body.op_id,
        )
    except CaseError as exc:
        raise _error(exc) from exc
    except ConfigError as exc:
        raise _goal_error(exc) from exc
    return _done(request, case)


@router.post("/cases/{case_id}/answers", response_model=Case, responses=_ERRORS, summary="Answer a question")
def answer_question(
    case_id: str,
    body: AnswerRequest,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    principal: PrincipalDep,
) -> Case:
    try:
        case = doctor_visit.answer(
            body.client_id,
            case_id,
            body.question_id,
            body.option_id,
            _deps(storage, root, request, principal),
            expected_version=body.version,
            actor=principal.user_id,
            op_id=body.op_id,
        )
    except CaseError as exc:
        raise _error(exc) from exc
    return _done(request, case)


@router.post(
    "/cases/{case_id}/decisions",
    response_model=Case,
    responses=_ERRORS,
    summary="Tick or untick a suggestion",
)
def decide_item(
    case_id: str,
    body: DecisionRequest,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    principal: PrincipalDep,
) -> Case:
    try:
        case = doctor_visit.decide(
            body.client_id,
            case_id,
            body.item_id,
            body.accept,
            _deps(storage, root, request, principal),
            expected_version=body.version,
            actor=principal.user_id,
            op_id=body.op_id,
        )
    except CaseError as exc:
        raise _error(exc) from exc
    return _done(request, case)


@router.post(
    "/cases/{case_id}/approval", response_model=Case, responses=_ERRORS, summary="Approve the changes"
)
def approve_changes(
    case_id: str,
    body: ApprovalRequest,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    principal: PrincipalDep,
) -> Case:
    try:
        case = doctor_visit.approve(
            body.client_id,
            case_id,
            _deps(storage, root, request, principal),
            expected_version=body.version,
            actor=principal.user_id,
            op_id=body.op_id,
            sentence=body.sentence,
        )
    except CaseError as exc:
        raise _error(exc) from exc
    return _done(request, case)


@router.post(
    "/cases/{case_id}/confirmations",
    response_model=Case,
    responses=_ERRORS,
    summary="Give the own yes to one change that needs it",
)
def confirm_item(
    case_id: str,
    body: ConfirmationRequest,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    principal: PrincipalDep,
) -> Case:
    """Plan O M246 (DEC-1816): an APPROVE-class item's own yes, by someone with its role, with its exact sentence."""
    try:
        case = doctor_visit.confirm(
            body.client_id,
            case_id,
            body.item_id,
            _deps(storage, root, request, principal),
            principal=principal,
            sentence=body.sentence,
            expected_version=body.version,
            op_id=body.op_id,
        )
    except CaseError as exc:
        raise _error(exc) from exc
    return _done(request, case)


@router.post(
    "/cases/{case_id}/standing-answers/{answer_id}/revoke",
    response_model=Case,
    responses=_ERRORS,
    summary="Revoke a remembered answer",
)
def revoke_standing_answer(
    case_id: str,
    answer_id: Annotated[str, Path_(pattern=r"^sa_[a-f0-9]{24}$")],
    body: _Change,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    principal: PrincipalDep,
) -> Case:
    """Plan O M246 (DEC-1816): the answer stops standing for the client and its question is asked again here."""
    try:
        case = doctor_visit.revoke_standing(
            body.client_id,
            case_id,
            answer_id,
            _deps(storage, root, request, principal),
            expected_version=body.version,
            actor=principal.user_id,
            op_id=body.op_id,
        )
    except CaseError as exc:
        raise _error(exc) from exc
    return _done(request, case)


@router.post("/cases/{case_id}/chat", response_model=Case, responses=_ERRORS, summary="Write to Data Doctor")
def chat_message(
    case_id: str,
    body: ChatRequest,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    principal: PrincipalDep,
) -> Case:
    try:
        case = doctor_visit.chat(
            body.client_id,
            case_id,
            body.message,
            _deps(storage, root, request, principal),
            expected_version=body.version,
            actor=principal.user_id,
            op_id=body.op_id,
        )
    except CaseError as exc:
        raise _error(exc) from exc
    return _done(request, case)


def _ai_status(storage: Storage, root: Path, client_id: str, case_id: str) -> AiStatus:
    from api.routes.doctor import offers as doctor_offers
    from engine.doctor.ai_guard import AiConsentStore
    from engine.doctor.case import CaseStore
    from engine.doctor.config import AiLimitsConfig

    case = CaseStore(storage).get(client_id, case_id)
    offers = doctor_offers(root)
    limits = offers[0].config.data_doctor.ai_limits if offers else AiLimitsConfig()
    consent = AiConsentStore(storage).get(client_id)
    return AiStatus(
        consent=consent is not None,
        given_by=None if consent is None else consent.given_by,
        given_at=None if consent is None else consent.given_at,
        tokens_used=case.counters.tokens,
        tokens_limit=limits.max_tokens_per_case,
        calls_used=case.counters.model_calls,
        calls_limit=limits.max_calls_per_case,
        spent=case.counters.tokens >= limits.max_tokens_per_case
        or case.counters.model_calls >= limits.max_calls_per_case,
    )


@router.get(
    "/cases/{case_id}/ai", response_model=AiStatus, responses=_ERRORS, summary="The Doctor's AI on a case"
)
def ai_status(
    case_id: str,
    storage: StorageDep,
    root: ConfigRootDep,
    client_id: Annotated[str, Query(pattern=CLIENT_ID_PATTERN)] = DEFAULT_CLIENT_ID,
) -> AiStatus:
    try:
        return _ai_status(storage, root, client_id, case_id)
    except CaseError as exc:
        raise _error(exc) from exc


@router.post(
    "/cases/{case_id}/ai-consent",
    response_model=AiStatus,
    responses=_ERRORS,
    summary="Let the Doctor's AI read masked summaries of this client's data",
)
def give_ai_consent(
    case_id: str,
    body: ResumeRequest,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    principal: PrincipalDep,
) -> AiStatus:
    from engine.doctor.ai_guard import AiConsentStore
    from engine.utils.time import utc_now

    try:
        status = _ai_status(storage, root, body.client_id, case_id)  # also refuses a case that is not there
    except CaseError as exc:
        raise _error(exc) from exc
    if not status.consent:
        AiConsentStore(storage).give(body.client_id, actor=principal.user_id, at=utc_now())
        status = _ai_status(storage, root, body.client_id, case_id)
    set_audit_context(request, object_id=case_id, object_type="case")
    return status


@router.get(
    "/goal-briefs/{goal_id}", response_model=GoalBrief, responses=_ERRORS, summary="What a goal needs"
)
def goal_brief(
    goal_id: str,
    root: ConfigRootDep,
    mode: Annotated[BriefMode, Query(description="Train, score, measure outcomes or audit.")] = "train",
) -> GoalBrief:
    try:
        return load_goal_brief(goal_id, root=root, mode=mode)
    except ConfigError as exc:
        raise _goal_error(exc) from exc
