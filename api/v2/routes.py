"""The `/v2` endpoints the marketer app calls. The UI stream codes against exactly these.

Every JSON body is a contract model (or a small wrapper of them) dumped with
`model_dump(mode="json")`, which is what FastAPI's response models produce. The routes only parse,
call one `Services` method and wrap the answer; the rules live in `api.v2.deps`.

`ServiceError`s become their HTTP status (400, 401, 403, 404, 409) with a plain-English `detail`.
"""

from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, ParamSpec, TypeVar

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import AwareDatetime, Field
from starlette.concurrency import run_in_threadpool

from api.v2.deps import ServiceError, Services, get_services
from api.v2.insights import (
    ActivityRow,
    BatchBreakdown,
    ChannelStatus,
    CustomerInsight,
    DataStatus,
    Expected,
    GoalReading,
    HealthLimits,
    PlanFacts,
    SizedOpportunity,
    Today,
)
from autonomy.contracts import (
    Alert,
    AutonomyLevel,
    Batch,
    BatchStatus,
    Brief,
    Briefing,
    Channel,
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
    budget: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        description="Optional: without it the amount (and the period) are read from the text",
    )
    ends_at: AwareDatetime | None = Field(default=None, description="Optional end of the goal")
    currency: str | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="Optional: the currency in the words, else the shop's, else USD",
    )
    kind: GoalKind | None = Field(default=None, description="Optional; inferred from the text when absent")
    objective: Objective | None = Field(
        default=None, description="Optional; the Strategist proposes one when absent"
    )


class GoalText(Contract):
    text: str = Field(min_length=1, max_length=2000)


class BudgetUpdate(Contract):
    budget: float = Field(ge=0, allow_inf_nan=False)


class GoalBudget(Contract):
    goal: Goal
    brief: Brief | None = Field(description="The latest plan; a draft plan takes the new budget too")


class PauseAllResult(Contract):
    paused: list[Goal] = Field(description="The goals this paused (those that were active)")


class ApproveRequest(Contract):
    autonomy: AutonomyLevel


class ContentStatusRequest(Contract):
    status: ContentStatus


class BatchDecisionRequest(Contract):
    """Approving or rejecting a batch needs nothing more than its id: send `{}` or no body at all."""


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


class TickRequest(Contract):
    now: AwareDatetime | None = Field(
        default=None, description="When the tick runs; the API's clock if absent"
    )


class TickResult(Contract):
    """What one runtime tick did: counts, plus the batches it created and the alerts it raised in full."""

    now: AwareDatetime
    decisions: int = Field(ge=0, description="Decisions made (contact or leave alone)")
    acts: int = Field(default=0, ge=0, description="Of those, decisions to contact someone")
    sent: int = Field(ge=0, description="Messages handed to a channel")
    queued: int = Field(default=0, ge=0, description="Messages queued for a later send time")
    failed: int = Field(default=0, ge=0)
    events_folded: int = Field(default=0, ge=0)
    batches: list[Batch] = Field(description="Batches created by this tick, waiting for a person")
    batches_sent: list[str] = Field(default_factory=list, description="Ids of approved batches sent")
    batches_expired: list[str] = Field(default_factory=list)
    alerts: list[Alert]
    paused_goals: list[str] = Field(default_factory=list, description="Goals a circuit breaker paused")
    paused_channels: list[Channel] = Field(default_factory=list)
    swept_goals: list[str] = Field(default_factory=list, description="Goals whose daily sweep ran")
    nightly_ran: bool = False

    @classmethod
    def from_summary(cls, summary: dict[str, Any]) -> "TickResult":
        """Keep the fields this response promises; a runtime may report more."""
        return cls.model_validate({k: v for k, v in summary.items() if k in cls.model_fields})


# --- goals ----------------------------------------------------------------------------------------


@router.get("/goals")
def list_goals(services: ServicesDep) -> list[Goal]:
    """Every goal, newest first."""
    return services.list_goals()


@router.post("/goals")
def create_goal(body: GoalCreate, services: ServicesDep) -> GoalWithBrief:
    """Store a new goal (a draft) with the Strategist's plan for it, waiting for approval."""
    goal, brief = _run(
        services.create_goal, body.text, body.budget, body.currency, body.kind, body.objective, body.ends_at
    )
    return GoalWithBrief(goal=goal, brief=brief)


@router.post("/goals/read")
def read_goal(body: GoalText, services: ServicesDep) -> GoalReading:
    """What the server reads from a goal's words: an amount and a period ("$5,000 this month")."""
    return services.read_goal(body.text)


@router.get("/goals/{goal_id}")
def get_goal(goal_id: str, services: ServicesDep) -> GoalDetail:
    """The goal, its latest plan, its content (drafts included) and the proof so far."""
    goal, brief, content, proof = _run(services.goal_detail, goal_id)
    return GoalDetail(goal=goal, brief=brief, content=content, proof=proof)


@router.patch("/goals/{goal_id}")
def update_goal(goal_id: str, body: BudgetUpdate, services: ServicesDep) -> GoalBudget:
    """Change the goal's budget. Raising it above an approved plan's budget is a 409 (a new plan)."""
    goal, brief = _run(services.set_budget, goal_id, body.budget)
    return GoalBudget(goal=goal, brief=brief)


@router.get("/goals/{goal_id}/plan")
def goal_plan(goal_id: str, services: ServicesDep) -> PlanFacts:
    """Audience size, the hard rules in words, and the AI editor's checks on each message."""
    return _run(services.plan_facts, goal_id)


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


@router.post("/pause")
def pause_all(services: ServicesDep) -> PauseAllResult:
    """The one big Pause button: pause every active goal."""
    return PauseAllResult(paused=services.pause_all())


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


@router.get("/activity")
def list_activity(
    services: ServicesDep,
    goal_id: str | None = None,
    customer_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> list[ActivityRow]:
    """The latest decisions, newest first, each with its goal, what happened to it and who it was for."""
    return _run(services.activity, goal_id=goal_id, customer_id=customer_id, limit=limit)


@router.get("/customers/{customer_id}/insight")
def get_customer_insight(customer_id: str, services: ServicesDep) -> CustomerInsight:
    """One customer in words: reading hours, offer habit, refill, items, and each decision's outcome."""
    return _run(services.customer_insight, customer_id)


@router.get("/today")
def get_today(services: ServicesDep, goal_id: str | None = None, day: date | None = None) -> Today:
    """One day's work for a goal (default: the latest day it decided), counted over every decision."""
    return _run(services.today, goal_id, day)


@router.get("/expected")
def get_expected(goal_id: str, services: ServicesDep) -> Expected:
    """What the agent expects its decisions to earn: its own forecast, labelled as such, not proof."""
    return _run(services.expected, goal_id)


# --- what the system tells people -----------------------------------------------------------------


@router.get("/briefing")
def get_briefing(services: ServicesDep, goal_id: str | None = None) -> Briefing | None:
    """The latest briefing for a goal (or the whole programme); null when there is none yet.

    Its measured sections carry the same proof and health as `/v2/proof` and `/v2/health` serve
    now, for this goal, so the briefing and Proof never show two profits for one goal.
    """
    return services.current_briefing(goal_id)


@router.get("/proof")
def get_proof(services: ServicesDep, goal_id: str | None = None) -> ProofSummary | None:
    """Agent vs holdout and vs baseline; null when there is nothing to show yet."""
    return services.proof(goal_id)


@router.get("/health")
def get_health(services: ServicesDep) -> HealthStatus:
    """Delivery, complaint and unsubscribe rates over the last week, with open alerts."""
    return services.health()


@router.get("/health/limits")
def get_health_limits(services: ServicesDep, goal_id: str | None = None) -> HealthLimits:
    """The rules that pause sending, in words, with today's rate and the usual rate they compare with."""
    return _run(services.health_limits, goal_id)


@router.get("/data/status")
def get_data_status(services: ServicesDep) -> DataStatus:
    """What customer data is connected, by source, and the Data Doctor's fixes and questions."""
    return services.data_status()


@router.get("/channels")
def list_channels(services: ServicesDep) -> list[ChannelStatus]:
    """How messages go out: email and WhatsApp directly; SMS and your own tools by hand-off."""
    return services.channels()


@router.get("/alerts")
def list_alerts(services: ServicesDep) -> list[Alert]:
    return services.alerts()


@router.get("/opportunities")
def list_opportunities(services: ServicesDep) -> list[Goal]:
    """Goals the agent suggests (drafts); POST one's text to /v2/goals to take it up."""
    return services.opportunities()


@router.get("/opportunities/sized")
def list_sized_opportunities(services: ServicesDep) -> list[SizedOpportunity]:
    """The same suggestions, each with how many customers it is about and why."""
    return services.sized_opportunities()


# --- batches (goals at the APPROVE autonomy level) ----------------------------------------------------


@router.get("/batches")
def list_batches(
    services: ServicesDep, goal_id: str | None = None, status: BatchStatus | None = None
) -> list[Batch]:
    """Contacts held for a person to sign off, newest first; filter by goal and status."""
    return _run(services.list_batches, goal_id, status)


@router.get("/batches/{batch_id}/breakdown")
def get_batch_breakdown(batch_id: str, services: ServicesDep) -> BatchBreakdown:
    """A batch's contacts counted by channel and by offer, and in words."""
    return _run(services.batch_breakdown, batch_id)


BatchBody = Annotated[BatchDecisionRequest | None, Body()]

_BATCH_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "No batch with this id; the detail names it"},
    409: {"description": "The batch is no longer pending (already approved, rejected, sent or expired)"},
}


@router.post("/batches/{batch_id}/approve", responses=_BATCH_ERRORS)
def approve_batch(batch_id: str, services: ServicesDep, body: BatchBody = None) -> Batch:  # noqa: ARG001
    """Approve a pending batch; the runtime sends it on its next tick. 409 if it is no longer pending."""
    return _run(services.approve_batch, batch_id)


@router.post("/batches/{batch_id}/reject", responses=_BATCH_ERRORS)
def reject_batch(batch_id: str, services: ServicesDep, body: BatchBody = None) -> Batch:  # noqa: ARG001
    """Reject a pending batch; nothing in it is sent. 409 if it is no longer pending."""
    return _run(services.reject_batch, batch_id)


# --- the runtime (operator and demo only) ---------------------------------------------------------


@router.post("/runtime/tick")
def run_tick(services: ServicesDep, body: Annotated[TickRequest | None, Body()] = None) -> TickResult:
    """Run one tick of the runtime now (or at `now`). 404 without a runtime, 403 where it is turned off."""
    summary = _run(services.tick, body.now if body is not None else None)
    return TickResult.from_summary(summary)


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
