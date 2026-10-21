"""P3 Decision Hub & Clearinghouse: one customer's state in, one decision out, with its auction.

`POST /v2/engines/decisions/evaluate` runs Autonomy's own policy (`build_agent_policy`) for one
customer and answers what the inner loop would do right now: take one action (which goal, channel,
offer, content and send time, with the probability the policy had of choosing it) or do nothing.
`POST /v2/engines/decisions/evaluate-batch` does the same for up to `MAX_BATCH` customers.

Stateless on purpose. Every call builds a fresh policy from the briefs and content in the request,
so the same request always gives the same answer (the seed fixes the Thompson draw) and the engine
can be used without the rest of Autonomy. A live runtime's policy is never borrowed: deciding
writes to its memory (pending sends that count towards the weekly cap, the attention stake), so a
"what would you do" call would change what the live loop does next. Building a policy takes well
under a millisecond, so there is nothing worth caching either. When the app has `Services`
attached, briefs can be named by id (`brief_ids`) and the approved library and goal kinds are read
from its workspace; that is the only thing Services is used for.

The clearinghouse view makes the auction readable without changing it. The policy chooses by
Thompson sampling against an attention price (what one more message costs this customer); the
response adds the runner-up (the best allowed bid that did not win) and a second-price
`clearing_price`: the larger of the attention price and the runner-up's bid, which is what the
winner had to beat. `suppressed_actions` lists every option that did not go out and why: hard
rules (consent, caps, quiet hours, ...), the audience, the attention price, or losing the auction.

Rule-blocked bids are re-checked here, because the decision only logs the codes of what the rules
removed. The re-check runs the same `rules.check_bid` on the same bids, seen the way the agent
saw the customer (its segment and its chosen send hour) and timed by the policy's own code, so a
snooze or quiet-hours block is reported as that block, not as a lost auction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, Field

from api.v2.deps import SERVICES_STATE_KEY, Services
from autonomy.contracts import (
    Action,
    Bid,
    Brief,
    Channel,
    ContentItem,
    Contract,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Format,
    GoalKind,
    Group,
)
from autonomy.decide import rules
from autonomy.decide.audience import matches
from autonomy.decide.explain import money
from autonomy.runtime.agent import AgentPolicy, build_agent_policy

MAX_BATCH = 1000
"""Most customers one evaluate-batch call may carry; bigger sweeps are split by the caller."""

TAKE_ACTION: Final = "TAKE_ACTION"
DO_NOTHING: Final = "DO_NOTHING"
Outcome = Literal["TAKE_ACTION", "DO_NOTHING"]

# Reason codes this engine adds to the hard rules' vocabulary (`autonomy.decide.rules.ALL_CODES`).
NOT_IN_AUDIENCE = "not_in_audience"
BASELINE_GROUP = "baseline_group"
BELOW_ATTENTION_PRICE = "below_attention_price"
OUTBID = "outbid"
LOST_DRAW = "lost_draw"
NOT_WORTH_TESTING = "not_worth_testing"

_RULE_SENTENCE: dict[str, str] = {
    rules.HOLDOUT: "In the holdout group, which is never contacted: it is how value is proven.",
    BASELINE_GROUP: "In the baseline group, where a simple rule decides instead of the agent.",
    NOT_IN_AUDIENCE: "Not in this goal's audience.",
    rules.NO_BRIEF: "No approved brief covers this goal.",
    rules.CHANNEL_NOT_ALLOWED: "This channel is not in the goal's approved envelope.",
    rules.OFFER_NOT_ALLOWED: "This offer is not in the goal's approved envelope.",
    rules.OFFER_NEEDS_APPROVAL: "This offer waits for a person to approve it.",
    rules.NO_DISCOUNTS: "The customer asked for no discounts.",
    rules.UNSUBSCRIBED: "The customer unsubscribed from this channel.",
    rules.NO_CONSENT: "No consent to message the customer on this channel.",
    rules.NO_ADDRESS: "No address for the customer on this channel.",
    rules.OPEN_COMPLAINT: "The customer has an open complaint, so nobody markets to them.",
    rules.SNOOZED: "The customer asked us to wait, and the send would land before then.",
    rules.TOO_SOON: "Too soon after the last message to this customer.",
    rules.WEEKLY_CAP: "The customer has had the most messages a week allows.",
    rules.NO_BUDGET: "The goal's budget left does not cover this message.",
    rules.QUIET_HOURS: "The send would land in the customer's quiet hours.",
}


class EngineInputError(Exception):
    """A request the engine cannot answer; `status_code` is the HTTP status it becomes."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


# --- request and response bodies ------------------------------------------------------------------


class EvaluateInputs(Contract):
    """What every evaluation needs besides the customer: the plan, the library and the moment."""

    briefs: tuple[Brief, ...] = Field(default=(), description="Approved briefs of the active goals")
    brief_ids: tuple[str, ...] = Field(
        default=(), description="Briefs to read from the attached workspace, by brief_id"
    )
    content: tuple[ContentItem, ...] | None = Field(
        default=None,
        description="Approved content to choose from; None reads the workspace's library if one is attached",
    )
    now: AwareDatetime = Field(description="The moment to decide for; the engine never reads a clock")
    seed: int = Field(default=0, description="Fixes the Thompson draw, so a call repeats exactly")
    budget_left: dict[str, float] = Field(
        default_factory=dict, description="goal_id -> money left; a goal left out is not budget-checked"
    )
    goal_kinds: dict[str, GoalKind] = Field(
        default_factory=dict, description="goal_id -> kind (cart recovery fades fast); default custom"
    )
    exploration_floor: float | None = Field(
        default=None, ge=0, le=1, description="Overrides the envelopes' exploration floor"
    )


class EvaluateRequest(EvaluateInputs):
    customer_state: CustomerState


class EvaluateBatchRequest(EvaluateInputs):
    customer_states: tuple[CustomerState, ...] = Field(min_length=1, max_length=MAX_BATCH)


class WinningAction(Contract):
    """The action the policy chose, ready for a dispatcher."""

    goal_id: str
    offer_id: str
    channel: Channel
    content_id: str | None
    format: Format
    item_id: str | None
    asset_id: str | None = None
    follow_up_step: int = 0
    scheduled_time: AwareDatetime
    expected_net_value: float = Field(description="Uplift x value - offer cost - channel cost, x urgency")
    assigned_probability: float = Field(description="The chance the policy had of choosing exactly this")
    reason: str


class SuppressedAction(Contract):
    """An option that did not go out, and why. `channel`/`offer_id` are None when a whole goal was out."""

    goal_id: str
    channel: Channel | None = None
    offer_id: str | None = None
    content_id: str | None = None
    reason_code: str
    plain_reason: str
    bid: float | None = Field(default=None, description="The option's expected net value, when it bid")


class RunnerUp(Contract):
    goal_id: str
    channel: Channel
    offer_id: str
    content_id: str | None
    bid: float


class Clearing(Contract):
    """The second-price view of the auction. Informative only: it never changes what was chosen."""

    attention_price: float = Field(description="What a bid had to beat: the cost of one more message")
    winning_bid: float | None = Field(description="The chosen action's expected net value; None if none")
    runner_up: RunnerUp | None = Field(description="The best allowed bid that did not win")
    clearing_price: float = Field(description="max(attention price, runner-up bid)")
    winner_surplus: float | None = Field(description="winning_bid - clearing_price; None if nothing won")


class EvaluateResponse(Contract):
    customer_id: str
    decision_id: str
    decision: Outcome
    group: Group
    probability: float = Field(description="The chance the policy had of this outcome, do-nothing included")
    reason: str
    winning_action: WinningAction | None
    suppressed_actions: tuple[SuppressedAction, ...]
    blocked_by: tuple[str, ...]
    clearing: Clearing
    policy_version: str


class EvaluateBatchResponse(Contract):
    results: tuple[EvaluateResponse, ...]
    n_customers: int
    n_take_action: int
    n_do_nothing: int
    policy_version: str


# --- resolving the inputs -------------------------------------------------------------------------


def _services(request: Request) -> Services | None:
    services = getattr(request.app.state, SERVICES_STATE_KEY, None)
    return services if isinstance(services, Services) else None


def resolve_inputs(
    inputs: EvaluateInputs, services: Services | None
) -> tuple[tuple[Brief, ...], tuple[ContentItem, ...], dict[str, GoalKind]]:
    """The briefs, content and goal kinds to decide with: the request's own, plus the workspace's.

    Briefs named in `brief_ids` must exist in the attached workspace (its latest version per goal).
    Every brief must be approved, since only an approved envelope may reach a customer.
    """
    briefs = list(inputs.briefs)
    if inputs.brief_ids:
        if services is None:
            raise EngineInputError(
                400, "brief_ids need a workspace, and none is attached: pass the briefs in the request"
            )
        known = {b.brief_id: b for b in services.workspace.briefs()}
        missing = [bid for bid in inputs.brief_ids if bid not in known]
        if missing:
            raise EngineInputError(404, f"No brief with id {', '.join(missing)} in the workspace")
        briefs.extend(known[bid] for bid in inputs.brief_ids)
    if not briefs:
        raise EngineInputError(400, "Pass at least one brief (inline in briefs, or by id in brief_ids)")
    goals = [b.goal_id for b in briefs]
    if len(set(goals)) != len(goals):
        raise EngineInputError(400, "One brief per goal: two briefs name the same goal")
    unapproved = [b.brief_id for b in briefs if not b.approved]
    if unapproved:
        raise EngineInputError(400, f"Brief {', '.join(unapproved)} is not approved")

    if inputs.content is not None:
        content = inputs.content
    elif services is not None:
        seen: dict[str, ContentItem] = {}
        for goal_id in goals:
            for item in services.workspace.content(goal_id=goal_id, approved_only=True):
                seen.setdefault(item.content_id, item)
        content = tuple(seen.values())
    else:
        content = ()

    kinds: dict[str, GoalKind] = {}
    if services is not None:
        kinds.update({g.goal_id: g.kind for g in services.workspace.goals() if g.goal_id in goals})
    kinds.update(inputs.goal_kinds)
    return tuple(briefs), tuple(content), kinds


# --- evaluating -----------------------------------------------------------------------------------


def _key(action: Action) -> tuple[object, ...]:
    """What makes two actions the same option, whatever send time each was given."""
    return (
        action.goal_id,
        action.channel,
        action.offer_id,
        action.content_id,
        action.format,
        action.item_id,
        action.follow_up_step,
    )


def _reach_code(state: CustomerState, channel: Channel) -> str:
    for check in (rules.unsubscribed, rules.consent, rules.contactable):
        result = check(state, channel)
        if not result.allowed:
            return result.code
    return rules.NO_ADDRESS


def _rule_entry(
    goal_id: str, code: str, bid: Bid | None = None, channel: Channel | None = None
) -> SuppressedAction:
    action = bid.action if bid is not None else None
    return SuppressedAction(
        goal_id=goal_id,
        channel=action.channel if action is not None else channel,
        offer_id=action.offer_id if action is not None else None,
        content_id=action.content_id if action is not None else None,
        reason_code=code,
        plain_reason=_RULE_SENTENCE.get(code, code.replace("_", " ").capitalize() + "."),
        bid=bid.expected_net if bid is not None else None,
    )


def _policy_view(agent: AgentPolicy, state: CustomerState, ctx: DecisionContext) -> CustomerState:
    """The customer as `AgentPolicy.decide` hands them to the auction, for an agent-group customer.

    The agent labels the segment and replaces `active_hours` with the one hour it aims for (or
    clears them), and every bid is timed from that view. Re-checking a blocked bid against the
    customer's raw `active_hours` would time it differently, and a snooze or quiet-hours block could
    then read as a lost auction. `_send_hour` is seeded by (seed, customer), so asking again gives
    the hour the decision used. Keep this in step with `AgentPolicy.decide`.
    """
    seg = agent.segment(state, ctx.now)
    update: dict[str, object] = {"segment": seg}
    hour = agent._send_hour(state.customer_id, seg, ctx, None)
    if hour is not None:
        update["active_hours"] = tuple(1.0 if h == hour else 0.0 for h in range(24))
    elif state.active_hours:
        update["active_hours"] = ()
    return state.model_copy(update=update)


def suppressed_actions(
    agent: AgentPolicy, state: CustomerState, ctx: DecisionContext, decision: Decision
) -> list[SuppressedAction]:
    """Every option that did not go out for this customer, and why, best bid first after the rules.

    Goals the customer is out of reach for come first (holdout, audience, channels with no consent
    or address), then each bid the goals offered that did not win.
    """
    if decision.group is not Group.AGENT:
        code = rules.HOLDOUT if decision.group is Group.HOLDOUT else BASELINE_GROUP
        return [_rule_entry(b.goal_id, code) for b in ctx.briefs]

    out: list[SuppressedAction] = []
    for brief in ctx.briefs:
        if not matches(state, brief.audience, ctx.now):
            out.append(_rule_entry(brief.goal_id, NOT_IN_AUDIENCE))
            continue
        reachable = set(state.contactable_channels())
        for channel in brief.envelope.channels:
            if channel not in reachable:
                out.append(_rule_entry(brief.goal_id, _reach_code(state, channel), channel=channel))

    seg_state = _policy_view(agent, state, ctx)
    logged = {_key(b.action): b for b in decision.bids}
    winner = _key(decision.action) if decision.action is not None else None
    price = decision.attention_price
    gap = rules.too_soon(seg_state, ctx.now, agent.policy.min_gap)
    offered = [b for bidder in agent.policy.bidders for b in bidder.bids(seg_state, ctx)]
    lost: list[SuppressedAction] = []
    for raw in offered:
        k = _key(raw.action)
        if k == winner:
            continue
        logged_bid = logged.get(k)
        # A logged bid is exactly what the auction saw; any other is timed by the policy's own code.
        bid = logged_bid if logged_bid is not None else agent.policy._timed(raw, seg_state, ctx)
        check = rules.check_bid(bid, seg_state, ctx)
        if check.allowed and not gap.allowed:
            check = gap
        if not check.allowed:
            out.append(_rule_entry(bid.goal_id, check.code, bid))
            continue
        net = bid.expected_net
        if net - price <= 0:
            code = BELOW_ATTENTION_PRICE
            sentence = (
                f"Expected to add {money(net)}, not more than the {money(price)} cost of another message."
            )
        elif decision.kind is DecisionKind.ACT:
            code = OUTBID
            sentence = f"Expected to add {money(net)}, but lost the auction to the chosen action."
        else:
            code = LOST_DRAW
            sentence = (
                f"Expected to add {money(net)}, but doing nothing won this draw: the agent is still "
                "unsure enough of this option to keep testing it."
            )
        lost.append(
            SuppressedAction(
                goal_id=bid.goal_id,
                channel=bid.action.channel,
                offer_id=bid.action.offer_id,
                content_id=bid.action.content_id,
                reason_code=code,
                plain_reason=sentence,
                bid=net,
            )
        )
    # Options the bidders' pruning left out (an offer without evidence that this customer needs it,
    # a person messages seem to put off, an option that cannot plausibly be the best): listed too,
    # so every option in the envelope is accounted for.
    seen = {_key(b.action) for b in offered}
    for bidder in agent.policy.bidders:
        quote = getattr(bidder, "quote", None)
        if quote is None:
            continue
        for bid in quote(seg_state, ctx, prune=False):
            k = _key(bid.action)
            if k == winner or k in seen:
                continue
            seen.add(k)
            timed = agent.policy._timed(bid, seg_state, ctx)
            check = rules.check_bid(timed, seg_state, ctx)
            if check.allowed and not gap.allowed:
                check = gap
            if not check.allowed:
                out.append(_rule_entry(timed.goal_id, check.code, timed))
                continue
            lost.append(
                SuppressedAction(
                    goal_id=bid.goal_id,
                    channel=bid.action.channel,
                    offer_id=bid.action.offer_id,
                    content_id=bid.action.content_id,
                    reason_code=NOT_WORTH_TESTING,
                    plain_reason=(
                        f"Expected to add {money(bid.expected_net)}, but the evidence says it cannot "
                        "beat the best option for this customer, so it was not put to the auction."
                    ),
                    bid=bid.expected_net,
                )
            )
    lost.sort(key=lambda s: -(s.bid or 0.0))
    return out + lost


def clearing(decision: Decision) -> Clearing:
    """The second-price view: the runner-up is the best logged bid that is not the winner."""
    winner = _key(decision.action) if decision.action is not None else None
    others = [b for b in decision.bids if _key(b.action) != winner]
    best = max(others, key=lambda b: b.expected_net) if others else None
    price = decision.attention_price
    runner_up = (
        RunnerUp(
            goal_id=best.goal_id,
            channel=best.action.channel,
            offer_id=best.action.offer_id,
            content_id=best.action.content_id,
            bid=best.expected_net,
        )
        if best is not None
        else None
    )
    clearing_price = max(price, best.expected_net) if best is not None else price
    acted = decision.kind is DecisionKind.ACT
    return Clearing(
        attention_price=price,
        winning_bid=decision.expected_net if acted else None,
        runner_up=runner_up,
        clearing_price=clearing_price,
        winner_surplus=decision.expected_net - clearing_price if acted else None,
    )


def _winning(decision: Decision) -> WinningAction | None:
    a = decision.action
    if a is None:
        return None
    return WinningAction(
        goal_id=a.goal_id,
        offer_id=a.offer_id,
        channel=a.channel,
        content_id=a.content_id,
        format=a.format,
        item_id=a.item_id,
        asset_id=a.asset_id,
        follow_up_step=a.follow_up_step,
        scheduled_time=a.send_at,
        expected_net_value=decision.expected_net,
        assigned_probability=decision.probability,
        reason=decision.reason,
    )


def evaluate(
    state: CustomerState,
    briefs: Sequence[Brief],
    content: Sequence[ContentItem],
    *,
    now: datetime,
    seed: int = 0,
    budget_left: Mapping[str, float] | None = None,
    kinds: Mapping[str, GoalKind] | None = None,
    exploration_floor: float | None = None,
) -> EvaluateResponse:
    """One customer's decision from a freshly built agent policy (no HTTP, no stores)."""
    agent = build_agent_policy(briefs, content, kinds=kinds, exploration_floor=exploration_floor)
    ctx = DecisionContext(
        now=now,
        briefs=tuple(briefs),
        content=tuple(content),
        budget_left=dict(budget_left or {}),
        seed=seed,
        policy_version=agent.version,
    )
    decision = agent.decide(state, ctx)
    return EvaluateResponse(
        customer_id=decision.customer_id,
        decision_id=decision.decision_id,
        decision=TAKE_ACTION if decision.kind is DecisionKind.ACT else DO_NOTHING,
        group=decision.group,
        probability=decision.probability,
        reason=decision.reason,
        winning_action=_winning(decision),
        suppressed_actions=tuple(suppressed_actions(agent, state, ctx, decision)),
        blocked_by=decision.blocked_by,
        clearing=clearing(decision),
        policy_version=decision.policy_version,
    )


def evaluate_many(
    states: Sequence[CustomerState],
    briefs: Sequence[Brief],
    content: Sequence[ContentItem],
    *,
    now: datetime,
    seed: int = 0,
    budget_left: Mapping[str, float] | None = None,
    kinds: Mapping[str, GoalKind] | None = None,
    exploration_floor: float | None = None,
) -> EvaluateBatchResponse:
    """Each customer evaluated exactly as `evaluate` would, on its own fresh policy.

    One policy per customer (not one per batch) keeps a batch's answers identical to single calls:
    a shared policy would let one customer's scheduled send count towards another row with the same
    customer id. Building one costs far less than deciding.
    """
    if len(states) > MAX_BATCH:
        raise EngineInputError(422, f"At most {MAX_BATCH} customers per call; got {len(states)}")
    results = tuple(
        evaluate(
            s,
            briefs,
            content,
            now=now,
            seed=seed,
            budget_left=budget_left,
            kinds=kinds,
            exploration_floor=exploration_floor,
        )
        for s in states
    )
    acted = sum(1 for r in results if r.decision == TAKE_ACTION)
    return EvaluateBatchResponse(
        results=results,
        n_customers=len(results),
        n_take_action=acted,
        n_do_nothing=len(results) - acted,
        policy_version=results[0].policy_version if results else "",
    )


# --- the routes -----------------------------------------------------------------------------------

router = APIRouter(prefix="/decisions", tags=["engines: P3 decision hub"])


def _http(exc: EngineInputError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/evaluate", response_model=EvaluateResponse)
def evaluate_route(body: EvaluateRequest, request: Request) -> EvaluateResponse:
    """What the agent would do for this customer now: one action, or nothing, with its auction."""
    try:
        briefs, content, kinds = resolve_inputs(body, _services(request))
    except EngineInputError as exc:
        raise _http(exc) from exc
    return evaluate(
        body.customer_state,
        briefs,
        content,
        now=body.now,
        seed=body.seed,
        budget_left=body.budget_left,
        kinds=kinds,
        exploration_floor=body.exploration_floor,
    )


@router.post("/evaluate-batch", response_model=EvaluateBatchResponse)
def evaluate_batch_route(body: EvaluateBatchRequest, request: Request) -> EvaluateBatchResponse:
    """`evaluate` for up to 1,000 customers sharing one plan, library and moment."""
    try:
        briefs, content, kinds = resolve_inputs(body, _services(request))
        return evaluate_many(
            body.customer_states,
            briefs,
            content,
            now=body.now,
            seed=body.seed,
            budget_left=body.budget_left,
            kinds=kinds,
            exploration_floor=body.exploration_floor,
        )
    except EngineInputError as exc:
        raise _http(exc) from exc
