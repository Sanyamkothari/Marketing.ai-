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
| `GET /goal-briefs/{goal_id}` | Viewer | What a goal needs (the goal brief), from configuration. |

Every change names the case `version` it was made against (`409 CASE_VERSION_CONFLICT` when it moved) and may carry
an `op_id`: a retried request whose first attempt was written changes nothing. Refusals: `404 CASE_NOT_FOUND`,
`404 USE_CASE_NOT_FOUND` (a goal that is not a use case), `409 VISIT_NOT_WAITING` (nothing waits for that now),
`422 VISIT_UNKNOWN_CHOICE` (no such question, option or suggestion in the active section).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, Request
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
from engine.doctor.visit import ConversePort, VisitDeps
from engine.storage import Storage

__all__ = ["POLICIES", "router"]

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
    pass


class ChatRequest(_Change):
    message: str = Field(min_length=1, max_length=4_000)


def _error(exc: CaseError) -> Exception:
    status = {"CASE_NOT_FOUND": 404, "VISIT_UNKNOWN_CHOICE": 422}.get(exc.code, 409)
    return http_error(status, exc.code, exc.message)


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
    from engine.generative.budget import Meter
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
    meter = Meter(ai.client, job_id="data-doctor", llm=ai.llm, budget=default.generative.budget)
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
        context_for, meter=meter, guardrails=guardrails, principal=principal, config_root=root
    )


def _deps(
    storage: Storage, root: Path, request: Request, principal: Principal | None, *, chat: bool = False
) -> VisitDeps:
    from api.routes.doctor import offers as doctor_offers
    from engine.stages.ingest import profile_row_cap

    offers = doctor_offers(root)
    cap = min((profile_row_cap(offer.config) for offer in offers), default=DIAGNOSIS_ROWS)
    return VisitDeps(
        storage=storage,
        load_files=lambda case: _load_files(storage, case.upload_ids(), cap),
        offers=offers,
        config_root=root,
        converse=(
            _converse(storage, root, request, principal, offers) if chat and principal is not None else None
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
            _deps(storage, root, request, principal, chat=True),
            expected_version=body.version,
            actor=principal.user_id,
            op_id=body.op_id,
        )
    except CaseError as exc:
        raise _error(exc) from exc
    return _done(request, case)


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
