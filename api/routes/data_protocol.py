"""Data Doctor's protocol outside a case (Plan O M246, DEC-1816): standing answers, client facts, patterns.

Mounted by the `PLAN-O (M246)` hook in `api/main.py`.

| Method and path | Role | Does |
|---|---|---|
| `GET /clients/{client_id}/standing-answers` | Analyst | The client's remembered answers (Settings), revoked ones too. |
| `POST /clients/{client_id}/standing-answers/{answer_id}/revoke` | Analyst | Revoke one: it is asked again next time. |
| `GET /clients/{client_id}/facts` | Viewer | What the client told Data Doctor about their business (customers, outcome rate, ID length, financial year, time zone, currency). |
| `PUT /clients/{client_id}/facts` | Analyst | State or take back facts; each records who and when, and a changed fact keeps the old one in its history. |
| `GET /data-doctor/candidate-rules` | Admin | AI-proposed patterns people approved often enough to become a rule (for the product team). |

Refusals: `404 STANDING_ANSWER_NOT_FOUND`, `422 FACT_INVALID` (a fact that makes no sense, with a sentence).
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Request
from fastapi import Path as Path_
from pydantic import Field

from api.access import PrincipalDep, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import StorageDep
from api.routes.uploads import http_error
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.config import StrictBase
from engine.doctor.case import CLIENT_ID_PATTERN
from engine.doctor.facts import ClientFact, FactError, FactInput, FactStore
from engine.doctor.patterns import ApprovedPattern, candidate_rules
from engine.doctor.protocol import ProtocolError
from engine.doctor.standing import StandingAnswer, StandingAnswerStore
from engine.utils.time import utc_now

__all__ = ["POLICIES", "router"]

router: APIRouter = APIRouter(tags=["data-doctor"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("GET", "/clients/{client_id}/standing-answers"): RoutePolicy(
        role=Role.ANALYST, action="doctor.standing_read", purpose="see the answers Data Doctor remembers"
    ),
    ("POST", "/clients/{client_id}/standing-answers/{answer_id}/revoke"): RoutePolicy(
        role=Role.ANALYST, action="doctor.standing_revoke", purpose="revoke a remembered answer"
    ),
    ("GET", "/clients/{client_id}/facts"): RoutePolicy(
        role=Role.VIEWER, action="doctor.facts_read", purpose="see what Data Doctor was told about a client"
    ),
    ("PUT", "/clients/{client_id}/facts"): RoutePolicy(
        role=Role.ANALYST, action="doctor.facts_write", purpose="tell Data Doctor facts about a client"
    ),
    ("GET", "/data-doctor/candidate-rules"): RoutePolicy(
        role=Role.ADMIN, action="doctor.candidate_rules_read", purpose="see patterns to promote into rules"
    ),
}
register(POLICIES)

ClientId = Annotated[str, Path_(pattern=CLIENT_ID_PATTERN)]
AnswerId = Annotated[str, Path_(pattern=r"^sa_[a-f0-9]{24}$")]


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


class FactsRequest(StrictBase):
    facts: tuple[FactInput, ...] = Field(
        min_length=1, max_length=20, description="The facts to state or revoke."
    )


@router.get(
    "/clients/{client_id}/facts",
    response_model=tuple[ClientFact, ...],
    summary="What Data Doctor was told about a client's business",
)
def read_facts(
    client_id: ClientId, storage: StorageDep, include_revoked: bool = False
) -> tuple[ClientFact, ...]:
    return FactStore(storage).list(client_id, include_revoked=include_revoked)


@router.put(
    "/clients/{client_id}/facts",
    response_model=tuple[ClientFact, ...],
    responses={422: {"model": ErrorResponse}},
    summary="Tell Data Doctor facts about a client's business, or take them back",
)
def put_facts(
    client_id: ClientId, body: FactsRequest, storage: StorageDep, principal: PrincipalDep, request: Request
) -> tuple[ClientFact, ...]:
    try:
        facts = FactStore(storage).put(client_id, body.facts, actor=principal.user_id)
    except FactError as exc:
        raise http_error(422, "FACT_INVALID", str(exc)) from exc
    set_audit_context(request, object_id=client_id, object_type="client")
    return facts


@router.get(
    "/data-doctor/candidate-rules",
    response_model=tuple[ApprovedPattern, ...],
    summary="AI-proposed patterns approved often enough to become a rule",
)
def read_candidate_rules(storage: StorageDep) -> tuple[ApprovedPattern, ...]:
    return candidate_rules(storage)
