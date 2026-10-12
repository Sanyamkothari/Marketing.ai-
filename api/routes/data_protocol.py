"""Data Doctor's protocol outside a case (Plan O M246, DEC-1816): Guided setup's Approve, standing answers, patterns.

Mounted only when `data_doctor.unified` is on (the `PLAN-O (M246)` hook in `api/main.py`): with it off - the
default - none of these paths exists and docs/API.md is unchanged.

| Method and path | Role | Does |
|---|---|---|
| `GET /uploads/{upload_id}/agent-session/approval` | Analyst | What Approve shows now: the exact sentence, the steps that need their own yes (with the role), whether the preview was shown, decision O3's missing guards, O10's warnings. |
| `POST /uploads/{upload_id}/agent-session/approve` | Analyst | Approve with the sentence read: `POST .../apply` with M233's approval-time facts checked against it. |
| `GET /clients/{client_id}/standing-answers` | Analyst | The client's remembered answers (Settings), revoked ones too. |
| `POST /clients/{client_id}/standing-answers/{answer_id}/revoke` | Analyst | Revoke one: it is asked again next time. |
| `GET /data-doctor/candidate-rules` | Admin | AI-proposed patterns people approved often enough to become a rule (for the product team). |
| `GET /data-doctor/standing-answers` | Analyst | Plan O M245 (DEC-1815): every client's remembered answers, for Settings' one list with Revoke. |
| `GET /data-doctor/tool-requests` | Admin | Plan O M245: fixes the AI was asked for that no step kind can make yet ("I can't do this safely yet"). |

Refusals: `409 APPROVAL_SENTENCE_CHANGED` (the sentence is no longer the one shown; nothing is recorded),
`409 RESHAPE_GUARD_MISSING` (a change to the rows without its question, its preview or its future-data check),
`403 TOOL_ROLE_REQUIRED`, `404 STANDING_ANSWER_NOT_FOUND`, and every refusal of `POST .../apply`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Request
from fastapi import Path as Path_
from fastapi.responses import JSONResponse
from pydantic import Field

from api.access import PrincipalDep, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep
from api.routes.uploads import http_error
from api.schemas import ErrorResponse, ValidationErrorResponse
from engine.access.roles import Role
from engine.config import RunMode, StrictBase
from engine.doctor.case import CLIENT_ID_PATTERN
from engine.doctor.pattern_fix import ToolRequest, tool_requests
from engine.doctor.patterns import ApprovedPattern, candidate_rules
from engine.doctor.protocol import ProtocolError
from engine.doctor.standing import StandingAnswer, StandingAnswerStore
from engine.storage import Storage
from engine.utils.time import utc_now

__all__ = ["POLICIES", "router"]

router: APIRouter = APIRouter(tags=["data-doctor"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("GET", "/uploads/{upload_id}/agent-session/approval"): RoutePolicy(
        role=Role.ANALYST,
        action="agent.approval_read",
        purpose="see what Approve will record",
        object_type="upload",
        object_param="upload_id",
    ),
    ("POST", "/uploads/{upload_id}/agent-session/approve"): RoutePolicy(
        role=Role.ANALYST,
        action="agent.approve",
        purpose="approve Guided setup with the sentence read",
        object_type="upload",
        object_param="upload_id",
    ),
    ("GET", "/clients/{client_id}/standing-answers"): RoutePolicy(
        role=Role.ANALYST, action="doctor.standing_read", purpose="see the answers Data Doctor remembers"
    ),
    ("POST", "/clients/{client_id}/standing-answers/{answer_id}/revoke"): RoutePolicy(
        role=Role.ANALYST, action="doctor.standing_revoke", purpose="revoke a remembered answer"
    ),
    ("GET", "/data-doctor/candidate-rules"): RoutePolicy(
        role=Role.ADMIN, action="doctor.candidate_rules_read", purpose="see patterns to promote into rules"
    ),
    # Plan O M245 (DEC-1815): Settings' list of every remembered answer, and the product team's tool requests.
    ("GET", "/data-doctor/standing-answers"): RoutePolicy(
        role=Role.ANALYST, action="doctor.standing_read", purpose="see the answers Data Doctor remembers"
    ),
    ("GET", "/data-doctor/tool-requests"): RoutePolicy(
        role=Role.ADMIN, action="doctor.tool_requests_read", purpose="see the fixes Data Doctor cannot make yet"
    ),
}
register(POLICIES)

ClientId = Annotated[str, Path_(pattern=CLIENT_ID_PATTERN)]
AnswerId = Annotated[str, Path_(pattern=r"^sa_[a-f0-9]{24}$")]


class ApprovalStep(StrictBase):
    proposal_id: str
    kind: str = Field(description="The step kind.")
    sentence: str = Field(description="Its part of the sentence approved.")
    role: str = Field(description="The role its yes needs.")
    why: str = Field(description="Why it needs its own yes.")


class ApprovalWarning(StrictBase):
    proposal_id: str
    code: str
    message: str


class ApprovalView(StrictBase):
    """What Guided setup's Approve shows and checks with Data Doctor on."""

    sentence: str = Field(description="What the person approves, word for word; send it back to approve.")
    approve_class: tuple[ApprovalStep, ...] = ()
    preview_shown: bool = Field(description="Whether the accepted steps were previewed as they are now.")
    reshape_missing: tuple[str, ...] = Field(default=(), description="Decision O3's guards still missing.")
    warnings: tuple[ApprovalWarning, ...] = ()


class ApproveRequest(StrictBase):
    sentence: str = Field(min_length=1, max_length=4_000, description="The sentence read, word for word.")


def _approval(storage: Storage, root: Path, request: Request, upload_id: str) -> ApprovalView:
    from api.routes import agent as agent_routes
    from engine.doctor.plan_g_approval import approval_of, read_preview

    session = agent_routes._load_session(storage, upload_id)
    loaded = agent_routes._load_context(
        storage, root, request, upload_id, session.use_case_id, session.model_version_id
    )
    recipe = agent_routes._accepted_recipe(session, loaded.upload, None, loaded.ctx.config)
    approval = approval_of(
        session,
        file_name=loaded.upload.file_name,
        preview=read_preview(storage, upload_id),
        recipe_hash=recipe.recipe_hash if recipe is not None else None,
        training=loaded.ctx.mode is RunMode.TRAIN,
        approval_required=bool(loaded.ctx.config.governance.approval_required),
    )
    return ApprovalView(
        sentence=approval.sentence,
        approve_class=tuple(
            ApprovalStep(
                proposal_id=s.proposal_id, kind=s.kind, sentence=s.sentence, role=s.role.value, why=s.why
            )
            for s in approval.approve_class
        ),
        preview_shown=approval.preview_shown,
        reshape_missing=approval.reshape_missing,
        warnings=tuple(
            ApprovalWarning(proposal_id=w.proposal_id, code=w.code, message=w.message)
            for w in approval.warnings
        ),
    )


@router.get(
    "/uploads/{upload_id}/agent-session/approval",
    response_model=ApprovalView,
    responses={404: {"model": ErrorResponse}},
    summary="What Approve will record: the exact sentence and what needs its own yes",
)
def read_approval(upload_id: str, root: ConfigRootDep, storage: StorageDep, request: Request) -> ApprovalView:
    return _approval(storage, root, request, upload_id)


@router.post(
    "/uploads/{upload_id}/agent-session/approve",
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ValidationErrorResponse},
    },
    summary="Approve with the sentence read: the accepted steps prepare the data",
)
def approve_with_sentence(
    upload_id: str, body: ApproveRequest, root: ConfigRootDep, storage: StorageDep, request: Request
) -> JSONResponse:
    """`POST .../apply` with the sentence the person read checked against the one shown now (M233's facts)."""
    from api.routes import agent as agent_routes

    with agent_routes._session_lock(upload_id):
        result = agent_routes._apply_agent_session(
            upload_id, root, storage, request, approved_sentence=body.sentence
        )
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse(result.model_dump(mode="json"))


@router.get(
    "/clients/{client_id}/standing-answers",
    response_model=tuple[StandingAnswer, ...],
    summary="The answers Data Doctor remembers for a client",
)
def list_standing_answers(client_id: ClientId, storage: StorageDep) -> tuple[StandingAnswer, ...]:
    return StandingAnswerStore(storage).list(client_id)


@router.post(
    "/clients/{client_id}/standing-answers/{answer_id}/revoke",
    response_model=StandingAnswer,
    responses={404: {"model": ErrorResponse}},
    summary="Revoke a remembered answer: it is asked again next time",
)
def revoke_standing_answer(
    client_id: ClientId, answer_id: AnswerId, storage: StorageDep, principal: PrincipalDep, request: Request
) -> StandingAnswer:
    try:
        answer = StandingAnswerStore(storage).revoke(
            client_id, answer_id, actor=principal.user_id, at=utc_now()
        )
    except ProtocolError as exc:
        raise http_error(exc.status, exc.code, exc.message) from exc
    set_audit_context(request, object_id=answer_id, object_type="standing_answer")
    return answer


@router.get(
    "/data-doctor/candidate-rules",
    response_model=tuple[ApprovedPattern, ...],
    summary="AI-proposed patterns approved often enough to become a rule",
)
def read_candidate_rules(storage: StorageDep) -> tuple[ApprovedPattern, ...]:
    return candidate_rules(storage)


@router.get(
    "/data-doctor/standing-answers",
    response_model=tuple[StandingAnswer, ...],
    summary="Every client's remembered answers (Settings)",
)
def list_every_standing_answer(storage: StorageDep) -> tuple[StandingAnswer, ...]:
    return StandingAnswerStore(storage).every_client()


@router.get(
    "/data-doctor/tool-requests",
    response_model=tuple[ToolRequest, ...],
    summary="Fixes Data Doctor was asked for that no step kind can make yet",
)
def read_tool_requests(storage: StorageDep) -> tuple[ToolRequest, ...]:
    return tool_requests(storage)
