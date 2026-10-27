"""P6, the Causal Incrementality Ledger: the public API for "did the marketing cause this?".

Every endpoint here is stateless where it can be: the caller sends the decisions and events, and the
answer is computed from them alone, so a business can use our measurement with any sending tool.

* `POST /measurement/summary` turns decisions + events into the ledger's summary: how many customers
  were treated, held out and left to the baseline rule; the conversion rates of the treated and the
  holdout with the lift, its 95% interval and a two-sided p-value; and the money (incremental
  revenue, dispatch and offer costs, net incremental profit and ROI, each with its interval). The
  numbers come from `autonomy.proof` (last-touch rewards and the Welch comparison), so they are the
  same numbers the product shows.
* `GET /measurement/goals/{goal_id}/summary` is the same summary for one goal of the running
  programme, read from the attached `Services` (503 when the app runs without them).
* `POST /measurement/ope` estimates how "always act", "never act" or the logging policy itself would
  have done on a log of decisions, with IPS, SNIPS and (given a reward model) doubly robust
  estimates, via `autonomy.proof.ope`.
* `POST /measurement/holdout/assign` puts customer ids into agent / baseline / holdout with our
  salted hash (`autonomy.proof.split`), so a client can hold out exactly the customers we would.

**What the status means.** It reads the conversion comparison (treated vs holdout), the one the
causal metrics show: `INSUFFICIENT_DATA` while either group has fewer measured customers than
`min_group` (or a per-goal summary can't be separated from other goals), else
`STATISTICALLY_SIGNIFICANT_POSITIVE` / `SIGNIFICANT_NEGATIVE` when the two-sided p-value is below
`alpha` and the lift points that way, else `NOT_YET_SIGNIFICANT`. When the money disagrees (more
conversions, but a net profit interval wholly below zero), a note says so: the status never hides a
loss.

**The p-value** comes from the same normal approximation as the intervals: with the Welch estimate
`d` and its standard error `se = (high - low) / (2 * 1.96)`, `p = 2 * (1 - Phi(|d| / se))`.

**The lift interval** is the delta-method interval of the ratio of the two rates (both rates are
uncertain), shown as a percentage change; it is None when the holdout never converted.

Time is passed in (`now`); nothing here reads the clock.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from statistics import NormalDist
from typing import Annotated, Any, Final, Literal

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from autonomy.contracts import (
    Channel,
    Decision,
    DecisionKind,
    Estimate,
    Event,
    EventType,
    Group,
    Objective,
    ProofSummary,
    Reward,
)
from autonomy.proof import (
    MIN_GROUP,
    Z_95,
    Costs,
    assign_group,
    minimum_detectable_lift,
    proof_rewards,
    proof_summary,
)
from autonomy.proof.estimate import welch_difference
from autonomy.proof.ope import OpeResult, evaluate

__all__ = [
    "HoldoutAssignRequest",
    "HoldoutAssignResponse",
    "LoggedDecision",
    "MeasurementStatus",
    "MeasurementSummary",
    "OpeRequest",
    "OpeResponse",
    "SummaryRequest",
    "measurement_summary",
    "off_policy_estimates",
    "p_value",
    "router",
]

router = APIRouter(prefix="/measurement", tags=["engines: P6 incrementality ledger"])

MAX_ROWS: Final[int] = 500_000
"""Most decisions, events or logged rows one stateless request may carry."""
MAX_CUSTOMERS: Final[int] = 1_000_000
"""Most customer ids one holdout assignment may carry."""

_TICK: Final[timedelta] = timedelta(microseconds=1)
_NORMAL: Final[NormalDist] = NormalDist()


class _Body(BaseModel):
    """Request bodies: unknown keys are an error, so a typo fails loudly instead of being ignored."""

    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- the summary ----------------------------------------------------------------------------------


class MeasurementStatus(StrEnum):
    POSITIVE = "STATISTICALLY_SIGNIFICANT_POSITIVE"
    NOT_YET = "NOT_YET_SIGNIFICANT"
    NEGATIVE = "SIGNIFICANT_NEGATIVE"
    INSUFFICIENT = "INSUFFICIENT_DATA"


class SummaryRequest(_Body):
    decisions: Annotated[list[Decision], Field(max_length=MAX_ROWS)]
    events: Annotated[list[Event], Field(max_length=MAX_ROWS)]
    horizon_days: int = Field(default=14, ge=1, le=365, description="Outcome window after a decision")
    objective: Objective = Objective.GROSS_PROFIT
    channel_costs: dict[Channel, float] = Field(default_factory=dict, description="Cost of one message")
    offer_costs: dict[str, float] = Field(default_factory=dict, description="Cost per redemption")
    now: AwareDatetime = Field(description="Measure as of this moment; windows must close by then")
    goal_id: str | None = Field(default=None, description="None: the whole programme")
    margin_rate: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="The shop's contribution margin, used when a purchase has none; unset: full value, and the summary says so",
    )
    value_per_conversion: float | None = Field(default=None, description="For yes/no objectives")
    min_group: int = Field(default=MIN_GROUP, ge=2, description="Fewest customers per group to claim")
    alpha: float = Field(default=0.05, gt=0, lt=1, description="Significance level for the status")


class CausalMetrics(_Out):
    treated_conversion_rate: float | None
    holdout_conversion_rate: float | None
    net_incremental_lift_pct: float | None = Field(
        description="(treated rate / holdout rate - 1) x 100; None when the holdout never converted"
    )
    confidence_interval_95: tuple[float, float] | None = Field(
        description="95% interval of the lift, in percent (delta method on the ratio of rates)"
    )
    p_value: float | None = Field(description="Two-sided, for treated rate minus holdout rate")
    conversion_rate_difference: Estimate | None = Field(
        default=None, description="Treated rate minus holdout rate, with its 95% interval"
    )
    minimum_detectable_lift_pct: float | None = Field(
        default=None,
        description="Smallest lift in percent that this many customers at this holdout rate could see "
        "(95% confidence, 80% power); a lift below it reads as noise",
    )


class FinancialImpact(_Out):
    gross_incremental_revenue: Estimate | None = Field(description="Agent vs holdout, x agent customers")
    dispatch_costs: float = Field(description="Channel costs of the treated group's sent messages")
    offer_cost: float = Field(description="Cost of offers the treated group redeemed")
    net_incremental_profit: Estimate | None = Field(description="Margin - costs, agent vs holdout")
    net_incremental_profit_p_value: float | None = None
    roi_ratio: Estimate | None = Field(description="Net incremental profit / spend")
    minimum_detectable_profit: float | None = Field(
        default=None, description="Smallest net incremental profit this comparison could tell from zero"
    )
    margin_note: str | None = Field(
        default=None, description="Says the shop margin is not set, or how much profit rests on it"
    )


class MeasurementSummary(_Out):
    goal_id: str | None
    as_of: datetime
    objective: Objective
    horizon_days: int
    treated_customers: int
    holdout_customers: int
    baseline_customers: int
    causal_metrics: CausalMetrics
    financial_impact: FinancialImpact
    status: MeasurementStatus
    notes: tuple[str, ...] = ()


def p_value(estimate: Estimate | None) -> float | None:
    """Two-sided p-value of "the true difference is zero", from a 95% normal-interval estimate.

    The standard error is read back from the interval's width, so this is the same approximation
    the interval was built with. A zero-width interval gives 1.0 at zero and 0.0 anywhere else.
    """
    if estimate is None:
        return None
    se = (estimate.high - estimate.low) / (2.0 * Z_95)
    if not math.isfinite(se):
        return None
    if se <= 0.0:
        return 1.0 if estimate.point == 0.0 else 0.0
    z = abs(estimate.point) / se
    return min(1.0, max(0.0, 2.0 * (1.0 - _NORMAL.cdf(z))))


def _group_of(decisions: Iterable[Decision]) -> dict[str, Group]:
    """Each customer's group: the group of their earliest decision (intention to treat), as the proof."""
    out: dict[str, Group] = {}
    for d in sorted(decisions, key=lambda d: (d.made_at, d.decision_id)):
        out.setdefault(d.customer_id, d.group)
    return out


def _usable(
    rewards: Sequence[Reward], by_id: Mapping[str, Decision], now: datetime, goal_id: str | None
) -> tuple[list[Reward], bool]:
    """The rewards `proof_summary` counts, and whether a per-goal summary must refuse (other goals)."""
    usable = [r for r in rewards if r.window_end <= now and r.decision_id in by_id]
    if goal_id is None:
        return usable, False
    others = any(r.goal_id is not None and r.goal_id != goal_id for r in usable)
    return [r for r in usable if r.goal_id in (goal_id, None)], others


def _lift(
    treated: np.ndarray[Any, np.dtype[np.float64]], holdout: np.ndarray[Any, np.dtype[np.float64]]
) -> tuple[float | None, tuple[float, float] | None]:
    """Relative lift in percent and its delta-method 95% interval; None when the holdout rate is 0."""
    pt, ph = float(treated.mean()), float(holdout.mean())
    if ph <= 0.0:
        return None, None
    ratio = pt / ph
    var_t = float(treated.var(ddof=1)) / len(treated)
    var_h = float(holdout.var(ddof=1)) / len(holdout)
    # Var(pt/ph) ~ var_t/ph^2 + pt^2 var_h/ph^4: the delta method, written so pt = 0 is fine.
    se = math.sqrt(var_t / ph**2 + pt**2 * var_h / ph**4)
    lift = (ratio - 1.0) * 100.0
    return lift, ((ratio - Z_95 * se - 1.0) * 100.0, (ratio + Z_95 * se - 1.0) * 100.0)


def _detectable_lift_pct(
    treated: np.ndarray[Any, np.dtype[np.float64]], holdout: np.ndarray[Any, np.dtype[np.float64]]
) -> float | None:
    """`minimum_detectable_lift` in percent, at the holdout's own conversion rate."""
    rate = float(holdout.mean())
    lift = minimum_detectable_lift(len(treated), len(holdout), rate)
    return lift * 100.0 if lift is not None else None


def measurement_summary(
    decisions: Sequence[Decision],
    events: Iterable[Event],
    *,
    horizon_days: int,
    objective: Objective,
    costs: Costs,
    now: datetime,
    goal_id: str | None = None,
    min_group: int = MIN_GROUP,
    alpha: float = 0.05,
) -> MeasurementSummary:
    """The ledger's summary as of `now`, from decisions and events alone (see the module docstring)."""
    by_customer: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_customer[e.customer_id].append(e)
    for evs in by_customer.values():
        evs.sort(key=lambda e: (e.ts, e.event_id))

    by_id = {d.decision_id: d for d in decisions}
    rewards = proof_rewards(by_id.values(), by_customer, horizon_days, objective, costs, now=now)
    proof: ProofSummary = proof_summary(rewards, by_id, now, goal_id, min_group=min_group, costs=costs)
    notes = list(proof.notes)

    group_of = _group_of(by_id.values())
    usable, refused = _usable(rewards, by_id, now, goal_id)
    converted: dict[str, bool] = {}
    for r in usable:
        converted[r.customer_id] = converted.get(r.customer_id, False) or r.converted

    def rates(group: Group) -> np.ndarray[Any, np.dtype[np.float64]]:
        return np.array(
            [1.0 if v else 0.0 for c, v in sorted(converted.items()) if group_of.get(c) is group],
            dtype=np.float64,
        )

    treated, holdout = rates(Group.AGENT), rates(Group.HOLDOUT)
    enough = not refused and len(treated) >= min_group and len(holdout) >= min_group
    diff = welch_difference(treated, holdout) if enough else None
    lift, lift_ci = _lift(treated, holdout) if enough else (None, None)
    p = p_value(diff)
    causal = CausalMetrics(
        treated_conversion_rate=float(treated.mean()) if len(treated) else None,
        holdout_conversion_rate=float(holdout.mean()) if len(holdout) else None,
        net_incremental_lift_pct=lift,
        confidence_interval_95=lift_ci,
        p_value=p,
        conversion_rate_difference=diff,
        minimum_detectable_lift_pct=_detectable_lift_pct(treated, holdout) if enough else None,
    )
    if enough and lift is None:
        notes.append("No holdout customer converted, so the lift has no percentage to show.")

    dispatch, offers = _cost_split(usable, by_id, group_of, by_customer, costs)
    financial = FinancialImpact(
        gross_incremental_revenue=proof.incremental_revenue,
        dispatch_costs=dispatch,
        offer_cost=offers,
        net_incremental_profit=proof.incremental_profit,
        net_incremental_profit_p_value=p_value(proof.incremental_profit),
        roi_ratio=proof.roi,
        minimum_detectable_profit=proof.detectable.profit if proof.detectable is not None else None,
        margin_note=proof.margin_note,
    )

    status = _status(diff, p, alpha)
    loss = proof.incremental_profit is not None and proof.incremental_profit.high < 0
    if status is MeasurementStatus.POSITIVE and loss:
        notes.append(
            "More treated customers converted, but net incremental profit is below zero across its "
            "whole interval: the extra sales cost more than they made."
        )
    if status is not MeasurementStatus.INSUFFICIENT:
        notes.append(
            f"The status reads the conversion comparison: a two-sided p-value below {alpha:g} counts "
            "as significant."
        )

    return MeasurementSummary(
        goal_id=goal_id,
        as_of=now,
        objective=objective,
        horizon_days=horizon_days,
        treated_customers=proof.agent_n,
        holdout_customers=proof.holdout_n,
        baseline_customers=proof.baseline_n,
        causal_metrics=causal,
        financial_impact=financial,
        status=status,
        notes=tuple(notes),
    )


def _status(diff: Estimate | None, p: float | None, alpha: float) -> MeasurementStatus:
    if diff is None or p is None:
        return MeasurementStatus.INSUFFICIENT
    if p < alpha and diff.point > 0:
        return MeasurementStatus.POSITIVE
    if p < alpha and diff.point < 0:
        return MeasurementStatus.NEGATIVE
    return MeasurementStatus.NOT_YET


def _cost_split(
    usable: Sequence[Reward],
    by_id: Mapping[str, Decision],
    group_of: Mapping[str, Group],
    by_customer: Mapping[str, Sequence[Event]],
    costs: Costs,
) -> tuple[float, float]:
    """The treated group's spend, split into dispatch (channel) cost and offer cost.

    A reward's cost is the channel cost when the message went out (a SEND or DELIVER event names
    the decision) plus any offer redeemed, as `autonomy.proof.rewards` charges it; the offer part is
    what is left once the channel part is taken out.
    """
    sent = {
        e.decision_id
        for evs in by_customer.values()
        for e in evs
        if e.decision_id is not None and e.type in (EventType.SEND, EventType.DELIVER)
    }
    dispatch = offers = 0.0
    for r in usable:
        if group_of.get(r.customer_id) is not Group.AGENT:
            continue
        d = by_id[r.decision_id]
        channel = 0.0
        if d.action is not None and d.decision_id in sent:
            channel = float(costs.channel.get(d.action.channel, 0.0))
        dispatch += channel
        offers += max(0.0, r.cost - channel)
    return dispatch, offers


@router.post("/summary")
def post_summary(body: SummaryRequest) -> MeasurementSummary:
    """The ledger's summary for the decisions and events in the request; nothing is stored."""
    costs = Costs(
        channel=dict(body.channel_costs),
        offer=dict(body.offer_costs),
        margin_rate=body.margin_rate,
        value_per_conversion=body.value_per_conversion,
    )
    return measurement_summary(
        body.decisions,
        body.events,
        horizon_days=body.horizon_days,
        objective=body.objective,
        costs=costs,
        now=body.now,
        goal_id=body.goal_id,
        min_group=body.min_group,
        alpha=body.alpha,
    )


@router.get("/goals/{goal_id}/summary")
def get_goal_summary(goal_id: str, request: Request) -> MeasurementSummary:
    """The summary for one goal of the running programme, from the attached `Services`.

    Reads every decision up to now from the decision log and each decided customer's events. The
    horizon is the goal's brief's; channel costs are the API's `SEND_COST` and offer costs the
    brief's. 503 when the app runs without services, 404 for an unknown goal.
    """
    from api.v2.deps import DEFAULT_HORIZON_DAYS, SEND_COST, SERVICES_STATE_KEY, Services

    services = getattr(request.app.state, SERVICES_STATE_KEY, None)
    if not isinstance(services, Services):
        raise HTTPException(503, "measurement for running goals needs the v2 services attached")
    goal = next((g for g in services.workspace.goals() if g.goal_id == goal_id), None)
    if goal is None:
        raise HTTPException(404, f"no goal {goal_id!r}")
    now = services.clock()
    briefs = services.workspace.briefs(goal_id)
    horizon = max((b.horizon_days for b in briefs), default=DEFAULT_HORIZON_DAYS)
    decisions = list(services.decisions.scan(until=now + _TICK))
    customers = sorted({d.customer_id for d in decisions})
    events = [e for c in customers for e in services.events.for_customer(c, until=now + _TICK)]
    costs = Costs(
        channel=SEND_COST,
        offer={o.offer_id: o.cost_per_redemption for b in briefs for o in b.envelope.offers},
        margin_rate=services.margin_rate,
    )
    return measurement_summary(
        decisions,
        events,
        horizon_days=horizon,
        objective=goal.objective,
        costs=costs,
        now=now,
        goal_id=goal_id,
    )


# --- off-policy evaluation ------------------------------------------------------------------------

TargetPolicy = Literal["always_act", "never_act", "logged"]


class LoggedDecision(_Body):
    """One logged decision with its outcome. The options are "act" and "do nothing".

    `act_probability` is the logging policy's chance of acting at all, for an ACT row whose policy
    chose among several actions; without it an ACT row's own probability is taken as that chance
    (one action to choose from). For a NOTHING row the chance of acting is `1 - probability`.
    `q_act` / `q_nothing` are an optional reward model's predictions for this row, for DR.
    """

    decision_id: str | None = None
    kind: DecisionKind
    probability: float = Field(gt=0, le=1, description="Chance the logging policy chose this option")
    reward: float
    act_probability: float | None = Field(default=None, gt=0, le=1)
    q_act: float | None = None
    q_nothing: float | None = None


class OpeRequest(_Body):
    logged: Annotated[list[LoggedDecision], Field(min_length=2, max_length=MAX_ROWS)]
    target_policy: TargetPolicy
    clip: float | None = Field(default=None, gt=0, description="Cap on each importance weight")


class OpeEstimate(_Out):
    value: Estimate
    n: int
    effective_n: float = Field(description="Kish's effective sample size of the weights")
    max_weight: float


class OpeResponse(_Out):
    target_policy: TargetPolicy
    n: int
    logged_value: float = Field(description="Mean reward under the logging policy")
    acted_share: float
    ips: OpeEstimate
    snips: OpeEstimate | None
    dr: OpeEstimate | None = Field(description="Needs q_act and q_nothing on every row")
    notes: tuple[str, ...] = ()


def _p_act(row: LoggedDecision) -> float:
    if row.kind is DecisionKind.NOTHING:
        return 1.0 - row.probability
    return row.act_probability if row.act_probability is not None else row.probability


def _target_prob(row: LoggedDecision, target: TargetPolicy) -> float:
    """The target policy's chance of choosing the option this row logged."""
    if target == "logged":
        return row.probability
    acted = row.kind is DecisionKind.ACT
    if target == "never_act":
        return 0.0 if acted else 1.0
    if not acted:
        return 0.0
    # Always act, choosing among actions as the logging policy did when it acted.
    return min(1.0, row.probability / max(_p_act(row), row.probability))


def _q_target(row: LoggedDecision, target: TargetPolicy, q_act: float, q_nothing: float) -> float:
    if target == "always_act":
        return q_act
    if target == "never_act":
        return q_nothing
    p = _p_act(row)
    return p * q_act + (1.0 - p) * q_nothing


def _out(result: OpeResult) -> OpeEstimate:
    return OpeEstimate(
        value=result.value, n=result.n, effective_n=result.effective_n, max_weight=result.max_weight
    )


def off_policy_estimates(
    rows: Sequence[LoggedDecision], target: TargetPolicy, *, clip: float | None = None
) -> OpeResponse:
    """IPS, SNIPS and (with a reward model on every row) DR estimates of the target policy's value.

    Raises ValueError for a log the estimators cannot use (fewer than two rows, bad numbers).
    """
    rewards = np.array([r.reward for r in rows], dtype=np.float64)
    logged = np.array([r.probability for r in rows], dtype=np.float64)
    pi = np.array([_target_prob(r, target) for r in rows], dtype=np.float64)
    have_q = all(r.q_act is not None and r.q_nothing is not None for r in rows)
    q_logged = q_target = None
    if have_q:
        q_logged = np.array(
            [(r.q_act if r.kind is DecisionKind.ACT else r.q_nothing) or 0.0 for r in rows], dtype=np.float64
        )
        q_target = np.array(
            [_q_target(r, target, r.q_act or 0.0, r.q_nothing or 0.0) for r in rows], dtype=np.float64
        )
    report = evaluate(rewards, logged, pi, q_logged=q_logged, q_target=q_target, clip=clip)

    notes: list[str] = []
    if report.snips is None:
        notes.append("No logged option is one the target policy would choose, so SNIPS is undefined.")
    if report.ips.effective_n < 0.1 * report.n:
        notes.append(
            f"Only about {report.ips.effective_n:,.0f} of {report.n:,} rows carry the estimate (effective "
            "sample size), so the intervals are optimistic."
        )
    if not have_q:
        notes.append("No reward model on every row (q_act and q_nothing), so no doubly robust estimate.")
    notes.append(
        "Estimates assume the logged probabilities are right and every option the target policy "
        "chooses had a chance under the logging policy; intervals are 95% normal approximations."
    )
    acted = sum(1 for r in rows if r.kind is DecisionKind.ACT)
    return OpeResponse(
        target_policy=target,
        n=report.n,
        logged_value=report.logged_value,
        acted_share=acted / len(rows) if rows else 0.0,
        ips=_out(report.ips),
        snips=_out(report.snips) if report.snips is not None else None,
        dr=_out(report.dr) if report.dr is not None else None,
        notes=tuple(notes),
    )


@router.post("/ope")
def post_ope(body: OpeRequest) -> OpeResponse:
    """How the target policy would have done on this log; 422 when the log can't be evaluated."""
    try:
        return off_policy_estimates(body.logged, body.target_policy, clip=body.clip)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# --- the holdout split ----------------------------------------------------------------------------


class HoldoutAssignRequest(_Body):
    customer_ids: Annotated[list[str], Field(min_length=1, max_length=MAX_CUSTOMERS)]
    salt: str = Field(min_length=1, description="Fixed per business; a new salt reshuffles everyone")
    holdout_share: float = Field(default=0.10, ge=0, le=1)
    baseline_share: float = Field(default=0.10, ge=0, le=1)


class HoldoutAssignResponse(_Out):
    groups: dict[str, Group]
    counts: dict[Group, int]
    shares: dict[Group, float] = Field(description="The share of the distinct ids that fell in each group")
    salt: str


@router.post("/holdout/assign")
def post_holdout_assign(body: HoldoutAssignRequest) -> HoldoutAssignResponse:
    """Each customer's group under our salted hash: the same answer on every call, for every client.

    Repeated ids are answered once. 422 when the shares leave no room for the agent.
    """
    try:
        groups = {
            c: assign_group(c, body.salt, body.holdout_share, body.baseline_share) for c in body.customer_ids
        }
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    counts = dict.fromkeys(Group, 0)
    for g in groups.values():
        counts[g] += 1
    total = len(groups)
    return HoldoutAssignResponse(
        groups=groups,
        counts=counts,
        shares={g: n / total for g, n in counts.items()},
        salt=body.salt,
    )
