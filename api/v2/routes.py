"""The `/v2` endpoints the marketer app calls. The UI stream codes against exactly these.

Every JSON body is a contract model (or a small wrapper of them) dumped with
`model_dump(mode="json")`, which is what FastAPI's response models produce. The routes only parse,
call one `Services` method and wrap the answer; the rules live in `api.v2.deps`.

`ServiceError`s become their HTTP status (400, 401, 404, 409) with a plain-English `detail`.
"""

from collections.abc import Callable
from typing import Annotated, ParamSpec, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from api.v2.deps import ServiceError, Services, get_services
from autonomy.contracts import (
    Alert,
    AutonomyLevel,
    Brief,
    Briefing,
    ContentItem,
    ContentStatus,
    Contract,
    CustomerState,
    Decision,
    Event,
    Goal,
    GoalKind,
    HealthStatus,
    Objective,
    ProofSummary,
)

router = APIRouter(prefix="/v2", tags=["v2"])

ServicesDep = Annotated[Services, Depends(get_services)]


P = ParamSpec("P")
T = TypeVar("T")


def _http_error(exc: ServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _run(fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Call one `Services` method, turning a refusal into its HTTP status."""
    try:
        return fn(*args, **kwargs)
    except ServiceError as exc:
        raise _http_error(exc) from exc


# --- request and response bodies ------------------------------------------------------------------


class GoalCreate(Contract):
    """The marketer's goal in their own words, and what they will spend on it."""

    text: str = Field(min_length=1, max_length=2000, description="e.g. 'win back lapsed customers'")
    budget: float = Field(ge=0, allow_inf_nan=False)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    kind: GoalKind | None = Field(default=None, description="Optional; inferred from the text when absent")
    objective: Objective | None = Field(
        default=None, description="Optional; the Strategist proposes one when absent"
    )


class ApproveRequest(Contract):
    autonomy: AutonomyLevel


class ContentStatusRequest(Contract):
    status: ContentStatus


class GoalWithBrief(Contract):
    goal: Goal
    brief: Brief


class GoalDetail(Contract):
    goal: Goal
    brief: Brief | None
    content: list[ContentItem]
    proof: ProofSummary | None


class CustomerDetail(Contract):
    state: CustomerState
    decisions: list[Decision] = Field(description="Newest first, at most 200")
    events: list[Event] = Field(description="Newest first, at most 200")


class WebhookResult(Contract):
    accepted: int = Field(description="How many of the callback's events were new")


# --- goals ----------------------------------------------------------------------------------------


@router.get("/goals")
def list_goals(services: ServicesDep) -> list[Goal]:
    """Every goal, newest first."""
    return services.list_goals()


@router.post("/goals")
def create_goal(body: GoalCreate, services: ServicesDep) -> GoalWithBrief:
    """Store a new goal (a draft) with the Strategist's plan for it, waiting for approval."""
    goal, brief = _run(services.create_goal, body.text, body.budget, body.currency, body.kind, body.objective)
    return GoalWithBrief(goal=goal, brief=brief)


@router.get("/goals/{goal_id}")
def get_goal(goal_id: str, services: ServicesDep) -> GoalDetail:
    """The goal, its latest plan, its content (drafts included) and the proof so far."""
    goal, brief, content, proof = _run(services.goal_detail, goal_id)
    return GoalDetail(goal=goal, brief=brief, content=content, proof=proof)


@router.post("/goals/{goal_id}/approve")
def approve_goal(goal_id: str, body: ApproveRequest, services: ServicesDep) -> GoalWithBrief:
    """Approve the plan and set the autonomy level; the goal becomes active."""
    goal, brief = _run(services.approve, goal_id, body.autonomy)
    return GoalWithBrief(goal=goal, brief=brief)


@router.post("/goals/{goal_id}/pause")
def pause_goal(goal_id: str, services: ServicesDep) -> Goal:
    """Stop the agent acting for this goal."""
    return _run(services.pause, goal_id)


@router.post("/goals/{goal_id}/resume")
def resume_goal(goal_id: str, services: ServicesDep) -> Goal:
    """Let the agent act again (the plan must be approved)."""
    return _run(services.resume, goal_id)


@router.get("/goals/{goal_id}/content")
def goal_content(goal_id: str, services: ServicesDep) -> list[ContentItem]:
    """The goal's content library, drafts included."""
    return _run(services.goal_content, goal_id)


# --- content --------------------------------------------------------------------------------------


@router.post("/content/{content_id}/status")
def set_content_status(content_id: str, body: ContentStatusRequest, services: ServicesDep) -> ContentItem:
    """Approve, reject or retire one content item."""
    return _run(services.set_content_status, content_id, body.status)


# --- decisions and customers ----------------------------------------------------------------------


@router.get("/decisions")
def list_decisions(
    services: ServicesDep,
    goal_id: str | None = None,
    customer_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> list[Decision]:
    """The latest decisions, newest first, each with its plain-English reason."""
    return services.recent_decisions(goal_id=goal_id, customer_id=customer_id, limit=limit)


@router.get("/customers/{customer_id}")
def get_customer(customer_id: str, services: ServicesDep) -> CustomerDetail:
    """One customer: their state, and their latest decisions and events."""
    state, decisions, events = _run(services.customer, customer_id)
    return CustomerDetail(state=state, decisions=decisions, events=events)


# --- what the system tells people -----------------------------------------------------------------


@router.get("/briefing")
def get_briefing(services: ServicesDep, goal_id: str | None = None) -> Briefing | None:
    """The latest briefing for a goal (or the whole programme); null when there is none yet."""
    return services.briefing(goal_id)


@router.get("/proof")
def get_proof(services: ServicesDep, goal_id: str | None = None) -> ProofSummary | None:
    """Agent vs holdout and vs baseline; null when there is nothing to show yet."""
    return services.proof(goal_id)


@router.get("/health")
def get_health(services: ServicesDep) -> HealthStatus:
    """Delivery, complaint and unsubscribe rates over the last week, with open alerts."""
    return services.health()


@router.get("/alerts")
def list_alerts(services: ServicesDep) -> list[Alert]:
    return services.alerts()


@router.get("/opportunities")
def list_opportunities(services: ServicesDep) -> list[Goal]:
    """Goals the agent suggests (drafts); POST one's text to /v2/goals to take it up."""
    return services.opportunities()


# --- inbound provider callbacks -------------------------------------------------------------------


@router.post("/webhooks/{channel}")
async def receive_webhook(channel: str, request: Request, services: ServicesDep) -> WebhookResult:
    """A provider callback (delivery, click, reply, ...): 401 on a bad signature, 404 on an unknown channel.

    The raw body is passed through untouched, because signatures are computed over the exact bytes.
    """
    body = await request.body()
    try:
        accepted = await run_in_threadpool(services.receive_webhook, channel, request.headers, body)
    except ServiceError as exc:
        raise _http_error(exc) from exc
    return WebhookResult(accepted=accepted)
