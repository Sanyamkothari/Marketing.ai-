"""Shadow -> Autopilot in one click, and the automatic step back to Approve when proof turns.

**The bar** (`PromotionRule`; a goal in Shadow or Approve is offered Autopilot when all hold):

1. It has run at its level for at least **14 days** (`min_days`).
2. At least **100** of its contact decisions (agent group, with the agent's own forecast) have had
   their whole outcome window close (`min_forecasts`), so there is something to check them against.
3. **The forecast pays:** the sum of those decisions' forecasts (each chosen bid's `expected_net`:
   extra profit after offer and channel costs over the plan's outcome window) is above zero at the
   low end of its 95% range. Shown as "it would have made about $X a month ($low to $high)", at the
   rate of the days it ran.
4. **The forecasts matched what happened.**
   * In **Shadow** nothing was sent, so the lift itself cannot be observed yet. What can be checked
     is the other half of every forecast: how much a customer spends when they buy
     (`value_if_converted`). For every purchase inside a closed window of a contact decision, the
     forecast is compared with what the customer really spent (margin, or revenue for a revenue
     goal); the relative error must be within **25%** (`value_tolerance`) over at least **30**
     purchases (`min_value_checks`). And the comparison must be fair: in Shadow everyone is left
     alone, so the people we never contact and everyone else must buy alike (the proof's
     agent-vs-holdout difference has zero inside its 95% interval, both groups at the proof's
     minimum size). A split that is not fair means the data or the split is broken.
   * In **Approve** messages went out, so the real comparison exists: incremental profit against
     the people we never contact (`autonomy.proof.proof_summary`, closed windows) must be above
     zero over its whole 95% range (the low end above zero), and the forecast for the sent decisions must fall inside its 95% interval.
5. No safety stop for the goal in the last 14 days, the goal is active, the data is trusted (no
   watchdog hold) and, when goals are governed by mandates, its mandate is active.

When the bar is met the inbox offers **one button, "Turn on Autopilot"** (`AutopilotGuard.promote`).
If the goal's signed mandate pre-authorises it (`AUTO_PROMOTE_CLAUSE` in the mandate's signed
`note`; see `with_auto_promotion`), the guard promotes a goal in Shadow on its own and nothing
reaches the inbox. A goal a person put in Approve is only ever offered the click.

**Automatic demotion is always on** (`AutopilotGuard.run`): an Autopilot goal drops to Approve,
with one inbox item, when a safety stop trips for it (any `breaker.*` alert since it was promoted)
or its proof turns negative (the upper end of its incremental profit's 95% interval below zero,
over at least 100 closed windows, the negative-lift breaker's own guards). After an automatic
demotion it is never promoted automatically again until a person turns Autopilot on.

**Every change is logged** in the `AutonomyLedger`: append-only and hash-chained like the mandate
ledger, each entry naming the goal, the levels, who (a person, or the guard), why, the numbers it
went on, and the governing mandate (id, version, body hash and the mandate ledger's head at that
moment, so the change is tied to the signed limits it was made under). Changes made elsewhere (the
autonomy dial on the plan page) are noticed on the next run and logged as a person's.

Pure maths and time passed in: nothing here reads the clock or calls an LLM.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import AwareDatetime, Field
from sqlalchemy import JSON, Column, Index, Integer, MetaData, String, Table, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from autonomy.contracts import (
    Alert,
    AutonomyLevel,
    Brief,
    Contract,
    Decision,
    DecisionKind,
    Estimate,
    Event,
    EventType,
    Goal,
    GoalStatus,
    Group,
    Objective,
)
from autonomy.interfaces import DecisionLog, EventStore, Workspace
from autonomy.mandate.models import Mandate, MandateStatus
from autonomy.mandate.signing import GENESIS_HASH, ChainReport, body_hash, canonical_bytes
from autonomy.proof.estimate import MIN_GROUP, proof_summary
from autonomy.proof.rewards import Costs, proof_rewards, sent_decisions
from autonomy.store.schema import UtcDateTime

__all__ = [
    "AUTO_PROMOTE_CLAUSE",
    "AutonomyChange",
    "AutonomyLedger",
    "AutopilotGuard",
    "Check",
    "GuardReport",
    "InMemoryAutonomyLedger",
    "PromotionRule",
    "Readiness",
    "SqlAutonomyLedger",
    "allows_auto_promotion",
    "assess",
    "autonomy_metadata",
    "create_autonomy_tables",
    "forecast_sum",
    "value_check",
    "with_auto_promotion",
]

AUTO_PROMOTE_CLAUSE = "Pre-authorised: turn on Autopilot automatically when the Shadow bar is met."
"""Written into a mandate's signed `note`, it pre-authorises automatic promotion for its goals.

The note is part of the signed body, so the clause cannot be added or removed without a new version
and new signatures. (A typed field on `Mandate` would be cleaner; the mandate model is not this
module's to change.)"""

SAFETY_PREFIX = "breaker."
NEGATIVE_LIFT_CODE = "proof.negative_lift"
"""`autonomy.channels.breakers.NEGATIVE_LIFT_CODE` (not imported: breakers pull in the dispatcher)."""

GUARD_ACTOR = "autopilot"
REASON_PROMOTED = "turned_on"
REASON_AUTO_PROMOTED = "auto_promoted"
REASON_LOSING = "losing"
REASON_SEEN = "changed_on_plan"
REASON_RESUMED = "resumed_in_approve"
REASON_KEPT = "kept_in_approve"
PAUSED_EVIDENCE = "goal_paused"
"""Evidence on an automatic demotion: 1.0 when the goal was paused (by its safety stop) at the time."""


def allows_auto_promotion(mandate: Mandate | None, now: datetime) -> bool:
    """True when an active mandate, valid now, carries the pre-authorisation clause."""
    return (
        mandate is not None
        and mandate.effective_status(now) is MandateStatus.ACTIVE
        and mandate.is_valid_at(now)
        and AUTO_PROMOTE_CLAUSE in mandate.note
    )


def with_auto_promotion(mandate: Mandate) -> Mandate:
    """The next version of `mandate` with the clause in its note: a draft that must be signed again."""
    note = (
        mandate.note
        if AUTO_PROMOTE_CLAUSE in mandate.note
        else f"{mandate.note} {AUTO_PROMOTE_CLAUSE}".strip()
    )
    return mandate.model_copy(
        update={"note": note, "version": mandate.version + 1, "signers": (), "status": MandateStatus.DRAFT}
    )


@dataclass(frozen=True)
class PromotionRule:
    """The bar for Autopilot (see the module docstring). Every field is immutable."""

    min_days: float = 14.0
    min_forecasts: int = 100
    min_value_checks: int = 30
    value_tolerance: float = 0.25
    min_group: int = MIN_GROUP
    quiet_days: float = 14.0
    """No safety stop for the goal in this many days."""
    min_closed_for_demotion: int = 100
    """Closed outcome windows before proof may demote (the negative-lift breaker's `min_closed`)."""
    min_proof_horizons: float = 2.0
    """Proof judges a level only after it has run this many outcome windows. Last-touch proof counts
    purchases up to `now - horizon` but the cost of every closed window, so in the first window after
    a start the agent's side always reads as cost with no sales: on the demo shop that "proved" the
    win-back goal losing $1 on day 14, when it went on to make about $1,600."""
    proof_every: timedelta = timedelta(hours=24)
    """How often the guard recomputes a goal's proof for demotion and readiness (it reads the logs)."""


# --- the evidence -----------------------------------------------------------------------------------

CheckName = Literal[
    "level", "days", "forecasts", "pays", "value", "fair", "proof", "safety", "data", "mandate"
]


class Check(Contract):
    name: CheckName
    ok: bool
    words: str


class Readiness(Contract):
    """Whether a goal has earned Autopilot, with the numbers behind it."""

    goal_id: str
    as_of: AwareDatetime
    level: AutonomyLevel
    ready: bool
    since: AwareDatetime | None = Field(description="When the goal started at its current level")
    days: float = Field(ge=0)
    contacts: int = Field(ge=0, description="Agent-group contact decisions with a forecast, in the period")
    closed: int = Field(ge=0, description="Of them, decisions whose outcome window has closed")
    forecast: Estimate | None = Field(
        default=None, description="What it would have made a month, at the period's rate, 95% range"
    )
    forecast_total: Estimate | None = Field(default=None, description="The same over the whole period")
    value_error: Estimate | None = Field(
        default=None, description="Shadow: (spent - forecast) / forecast per purchase, 95% range"
    )
    value_checks: int = 0
    observed: Estimate | None = Field(
        default=None, description="Approve: incremental profit vs the people we never contact"
    )
    observed_forecast: float | None = Field(
        default=None, description="Approve: the forecast for the same sends"
    )
    checks: tuple[Check, ...] = ()
    sentence: str
    detail: str = ""
    currency: str = "USD"
    auto: bool = Field(default=False, description="The mandate pre-authorises automatic promotion")


@dataclass(frozen=True)
class ForecastSum:
    n: int
    total: float
    var: float

    def estimate(self, scale: float = 1.0) -> Estimate | None:
        if self.n == 0:
            return None
        half = 1.96 * math.sqrt(self.var)
        return Estimate(
            point=round(self.total * scale, 2),
            low=round((self.total - half) * scale, 2),
            high=round((self.total + half) * scale, 2),
        )


def _chosen(d: Decision) -> Any:
    if d.action is None:
        return None
    return next((b for b in d.bids if b.action == d.action), None)


def forecast_sum(
    decisions: Iterable[Decision], goal_id: str, *, only: Iterable[str] | None = None
) -> ForecastSum:
    """The agent's own forecast summed over its contact decisions for the goal (agent group only).

    Each forecast is the chosen bid's `expected_net` with its `uncertainty` as standard deviation;
    the sum's range treats them as independent (as `/v2/expected` does). `only` keeps those ids."""
    keep = None if only is None else set(only)
    n, total, var = 0, 0.0, 0.0
    for d in decisions:
        if d.kind is not DecisionKind.ACT or d.action is None or d.action.goal_id != goal_id:
            continue
        if d.group is not Group.AGENT or (keep is not None and d.decision_id not in keep):
            continue
        bid = _chosen(d)
        if bid is None:
            continue
        n += 1
        total += bid.expected_net
        var += bid.uncertainty**2
    return ForecastSum(n, total, var)


@dataclass(frozen=True)
class ValueCheck:
    n: int
    error: Estimate | None
    """(mean spent - mean forecast) / mean forecast, with a 95% range from the paired differences."""


def value_check(
    decisions: Sequence[Decision],
    events_by_customer: Mapping[str, Sequence[Event]],
    goal_id: str,
    horizon_days: int,
    objective: Objective,
    now: datetime,
    *,
    margin_rate: float = 1.0,
) -> ValueCheck:
    """Shadow's forecast check: what customers spent when they bought, against the forecast.

    For each purchase, the latest closed contact decision (agent group, with a forecast) whose
    outcome window holds it gives the pair (forecast `value_if_converted`, what was spent). Spent is
    the purchase's margin (its own, else `value x margin_rate`) for a profit goal and its value for a
    revenue goal; other objectives value a conversion, not a purchase, and are not checked here."""
    if objective not in (Objective.GROSS_PROFIT, Objective.REVENUE):
        return ValueCheck(0, None)
    horizon = timedelta(days=horizon_days)
    by_customer: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for d in decisions:
        if d.kind is not DecisionKind.ACT or d.action is None or d.action.goal_id != goal_id:
            continue
        if d.group is not Group.AGENT or d.made_at + horizon > now:
            continue
        bid = _chosen(d)
        if bid is None or bid.value_if_converted <= 0:
            continue
        by_customer[d.customer_id].append((d.made_at, bid.value_if_converted))
    pairs: list[tuple[float, float]] = []
    for cid, made in by_customer.items():
        made.sort()
        for e in events_by_customer.get(cid, ()):
            if e.type is not EventType.PURCHASE or e.value is None:
                continue
            covering = [v for t, v in made if t <= e.ts < t + horizon]
            if not covering:
                continue
            if objective is Objective.REVENUE:
                spent = e.value
            else:
                spent = e.margin if e.margin is not None else e.value * margin_rate
            pairs.append((covering[-1], spent))
    n = len(pairs)
    if n < 2:
        return ValueCheck(n, None)
    mean_fc = sum(f for f, _ in pairs) / n
    diffs = [s - f for f, s in pairs]
    mean_d = sum(diffs) / n
    sd = math.sqrt(sum((x - mean_d) ** 2 for x in diffs) / (n - 1))
    half = 1.96 * sd / math.sqrt(n)
    if mean_fc <= 0:
        return ValueCheck(n, None)
    return ValueCheck(
        n,
        Estimate(
            point=round(mean_d / mean_fc, 4),
            low=round((mean_d - half) / mean_fc, 4),
            high=round((mean_d + half) / mean_fc, 4),
        ),
    )


def _money(x: float, currency: str) -> str:
    symbol = {"USD": "$", "AUD": "A$", "GBP": "£", "EUR": "€", "CAD": "C$", "NZD": "NZ$"}.get(currency)
    amount = f"{abs(x):,.0f}"
    sign = "-" if x < 0 else ""
    return f"{sign}{symbol}{amount}" if symbol else f"{sign}{amount} {currency}"


def _range(e: Estimate, currency: str) -> str:
    return f"{_money(e.low, currency)} to {_money(e.high, currency)}"


def assess(
    goal: Goal,
    brief: Brief,
    decisions: Sequence[Decision],
    events_by_customer: Mapping[str, Sequence[Event]],
    now: datetime,
    *,
    since: datetime,
    rule: PromotionRule | None = None,
    costs: Costs | None = None,
    margin_rate: float = 1.0,
    safety_stops: Sequence[Alert] = (),
    data_trusted: bool = True,
    mandate_ok: bool | None = None,
    others_acting: bool = False,
    auto: bool = False,
) -> Readiness:
    """Has `goal` earned Autopilot? `decisions` are the goal's own since `since` (holdout "do nothing"
    ones included, for the fairness check), `events_by_customer` those customers' events.

    `mandate_ok` is None when goals are not governed by mandates. `others_acting` says another goal
    sends to the same customers: the Shadow fairness check is then skipped (that goal's messages
    make the groups differ; its own proof covers them)."""
    rule = rule or PromotionRule()
    level = goal.autonomy
    days = max(0.0, (now - since).total_seconds() / 86400.0)
    horizon = timedelta(days=brief.horizon_days)
    closed_ids = {d.decision_id for d in decisions if d.made_at + horizon <= now}
    period = forecast_sum(decisions, goal.goal_id)
    closed = forecast_sum(decisions, goal.goal_id, only=closed_ids)
    scale = 30.0 / max(days, 1.0)
    monthly = period.estimate(scale)
    checks: list[Check] = []
    add = checks.append
    cur = goal.currency
    level_ok = goal.status is GoalStatus.ACTIVE and level in (AutonomyLevel.SHADOW, AutonomyLevel.APPROVE)
    need_days = rule.min_days
    if level is AutonomyLevel.APPROVE:
        need_days = max(need_days, rule.min_proof_horizons * brief.horizon_days)
    add(Check(name="level", ok=level_ok, words=f"The goal is {goal.status.value} in {level.value}."))
    add(
        Check(
            name="days",
            ok=days >= need_days,
            words=f"{days:.0f} of {need_days:.0f} days at {level.value}.",
        )
    )
    add(
        Check(
            name="forecasts",
            ok=closed.n >= rule.min_forecasts,
            words=f"{closed.n} of {rule.min_forecasts} contact decisions have a closed outcome window.",
        )
    )
    closed_est = closed.estimate()
    pays = closed_est is not None and closed_est.low > 0
    add(
        Check(
            name="pays",
            ok=pays,
            words=(
                "No forecast to add up yet."
                if closed_est is None
                else f"Forecast over closed windows {_money(closed_est.point, cur)} ({_range(closed_est, cur)})."
            ),
        )
    )
    value = ValueCheck(0, None)
    observed: Estimate | None = None
    observed_fc: float | None = None
    costs = costs or Costs.from_brief(brief, margin_rate=margin_rate)
    if level is AutonomyLevel.APPROVE:
        sent = sent_decisions(e for evs in events_by_customer.values() for e in evs)
        rewards = proof_rewards(
            decisions, events_by_customer, brief.horizon_days, brief.objective, costs, now=now
        )
        summary = proof_summary(rewards, decisions, now, goal.goal_id, min_group=rule.min_group)
        observed = summary.incremental_profit
        fc_sent = forecast_sum(decisions, goal.goal_id, only=closed_ids & set(sent))
        observed_fc = round(fc_sent.total, 2) if fc_sent.n else None
        # "Confirmed" means the whole 95% range is above zero, not just its middle: a point above
        # zero with a range reaching below it is not yet proof.
        matched = (
            observed is not None
            and observed.low > 0
            and observed_fc is not None
            and observed.low <= observed_fc <= observed.high
        )
        add(
            Check(
                name="proof",
                ok=matched,
                words=(
                    "Not enough people measured to compare with the people we never contact yet."
                    if observed is None
                    else f"Against the people we never contact it made {_money(observed.point, cur)} "
                    f"({_range(observed, cur)}); its forecast for the same messages was "
                    f"{_money(observed_fc or 0.0, cur)}."
                ),
            )
        )
    else:
        value = value_check(
            decisions,
            events_by_customer,
            goal.goal_id,
            brief.horizon_days,
            brief.objective,
            now,
            margin_rate=margin_rate,
        )
        err = value.error
        # One-sided: a forecast that expects more than customers spend would overstate $X and fails;
        # one that expects less makes $X cautious, which is said, not held against it.
        value_ok = err is not None and value.n >= rule.min_value_checks and err.point >= -rule.value_tolerance
        if err is None or value.n < rule.min_value_checks:
            value_words = f"{value.n} of {rule.min_value_checks} purchases to check its forecasts against."
        elif err.point < -rule.value_tolerance:
            value_words = (
                f"Customers spent {-err.point:.0%} less than it forecast over {value.n} purchases "
                f"(allowed {rule.value_tolerance:.0%})."
            )
        elif err.point > rule.value_tolerance:
            value_words = (
                f"Customers spent {err.point:.0%} more than it forecast over {value.n} purchases, so its "
                "figure is cautious."
            )
        else:
            value_words = f"Spend forecasts matched within {abs(err.point):.0%} over {value.n} purchases."
        add(Check(name="value", ok=value_ok, words=value_words))
        if others_acting:
            add(
                Check(
                    name="fair",
                    ok=True,
                    words="Another goal messages the same customers, so its own proof covers the comparison.",
                )
            )
        else:
            rewards = proof_rewards(
                decisions, events_by_customer, brief.horizon_days, brief.objective, costs, now=now
            )
            summary = proof_summary(rewards, decisions, now, goal.goal_id, min_group=rule.min_group)
            diff = summary.incremental_revenue
            fair = diff is not None and diff.low <= 0 <= diff.high
            add(
                Check(
                    name="fair",
                    ok=fair,
                    words=(
                        f"Too few people we never contact measured yet ({summary.holdout_n} of {rule.min_group})."
                        if diff is None
                        else (
                            "The people we never contact bought like everyone else, so the comparison is fair."
                            if fair
                            else "The people we never contact bought differently from everyone else while nothing "
                            "was sent: the split or the data needs a look before anything is proven."
                        )
                    ),
                )
            )
    recent = [a for a in safety_stops if a.at >= now - timedelta(days=rule.quiet_days)]
    add(
        Check(
            name="safety",
            ok=not recent,
            words=(
                "No safety stop in the last 14 days."
                if not recent
                else f"{len(recent)} safety stop(s) recently."
            ),
        )
    )
    add(
        Check(
            name="data",
            ok=data_trusted,
            words="The data is trusted." if data_trusted else "The data is on hold.",
        )
    )
    if mandate_ok is not None:
        add(
            Check(
                name="mandate",
                ok=mandate_ok,
                words="Its signed limits are active." if mandate_ok else "Its signed limits are not active.",
            )
        )
    ready = all(c.ok for c in checks)
    sentence, detail = _words(
        level, ready, need_days, days, monthly, value, observed, checks, cur, rule.value_tolerance
    )
    return Readiness(
        goal_id=goal.goal_id,
        as_of=now,
        level=level,
        ready=ready,
        since=since,
        days=round(days, 2),
        contacts=period.n,
        closed=closed.n,
        forecast=monthly,
        forecast_total=period.estimate(),
        value_error=value.error,
        value_checks=value.n,
        observed=observed,
        observed_forecast=observed_fc,
        checks=tuple(checks),
        sentence=sentence,
        detail=detail,
        currency=cur,
        auto=auto,
    )


def _words(
    level: AutonomyLevel,
    ready: bool,
    need_days: float,
    days: float,
    monthly: Estimate | None,
    value: ValueCheck,
    observed: Estimate | None,
    checks: Sequence[Check],
    cur: str,
    tolerance: float,
) -> tuple[str, str]:
    name = "Shadow" if level is AutonomyLevel.SHADOW else level.value.title()
    detail_lines = [c.words for c in checks]
    detail_lines.append(
        "A forecast is the agent's own estimate of extra profit after offer and message costs, summed over "
        "the contacts it chose; the monthly figure is that sum at the rate of the days it ran. "
        + (
            "Shadow sends nothing, so the extra purchases themselves are proven only once it sends: "
            "Autopilot keeps the people we never contact and drops back to Approve on its own if the "
            "proof turns negative."
            if level is AutonomyLevel.SHADOW
            else "Proof compares everyone it could contact with the people we never contact, over closed "
            "outcome windows."
        )
    )
    detail = " ".join(detail_lines)
    if not ready:
        first = next(c for c in checks if not c.ok)
        if first.name == "days":
            left = max(1, math.ceil(need_days - days))
            return (
                f"{name} needs {left} more day{'s' if left != 1 else ''} before Autopilot can be judged.",
                detail,
            )
        return f"Not ready for Autopilot yet: {first.words[0].lower()}{first.words[1:]}", detail
    assert monthly is not None
    made = f"{name} ran {days:.0f} days: it would have made about {_money(monthly.point, cur)} a month ({_range(monthly, cur)})"
    if level is AutonomyLevel.APPROVE and observed is not None:
        return f"{made}, and the people we never contact confirm it ({_range(observed, cur)}).", detail
    err = value.error
    if err is not None and err.point > tolerance:
        return f"{made}; customers spent {err.point:.0%} more than it forecast, so that is cautious.", detail
    within = f"{abs(err.point):.0%}" if err is not None else "the allowed margin"
    return f"{made}, and its forecasts of what customers spend matched within {within}.", detail


# --- the ledger -------------------------------------------------------------------------------------


class AutonomyChange(Contract):
    """One change of a goal's autonomy level, chained to the one before (append-only)."""

    seq: int = Field(ge=1)
    at: AwareDatetime
    business_id: str
    goal_id: str
    from_level: AutonomyLevel | None
    to_level: AutonomyLevel
    actor: str = Field(description="'person:<id>', or 'autopilot' for the guard")
    reason: str = Field(description="turned_on, auto_promoted, safety_stop:<code>, losing, changed_on_plan")
    words: str
    evidence: dict[str, float] = Field(default_factory=dict)
    mandate_id: str | None = None
    mandate_version: int | None = None
    mandate_body_hash: str | None = None
    mandate_ledger_head: str | None = Field(
        default=None, description="The mandate ledger's head when this was logged: the change's receipt"
    )
    prev_hash: str
    entry_hash: str


def _entry_hash(prev: str, content: Mapping[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(prev.encode("ascii") + canonical_bytes(dict(content))).hexdigest()


def _content(change: AutonomyChange) -> dict[str, Any]:
    return change.model_dump(mode="json", exclude={"prev_hash", "entry_hash"})


class AutonomyLedger(Protocol):
    def append(
        self,
        *,
        at: datetime,
        business_id: str,
        goal_id: str,
        from_level: AutonomyLevel | None,
        to_level: AutonomyLevel,
        actor: str,
        reason: str,
        words: str,
        evidence: Mapping[str, float] | None = None,
        mandate: Mandate | None = None,
        mandate_ledger_head: str | None = None,
    ) -> AutonomyChange: ...

    def entries(self, goal_id: str | None = None) -> list[AutonomyChange]: ...

    def verify_chain(self) -> ChainReport: ...


def _build(
    seq: int,
    prev: str,
    *,
    at: datetime,
    business_id: str,
    goal_id: str,
    from_level: AutonomyLevel | None,
    to_level: AutonomyLevel,
    actor: str,
    reason: str,
    words: str,
    evidence: Mapping[str, float] | None,
    mandate: Mandate | None,
    mandate_ledger_head: str | None,
) -> AutonomyChange:
    draft = AutonomyChange(
        seq=seq,
        at=at.astimezone(UTC),
        business_id=business_id,
        goal_id=goal_id,
        from_level=from_level,
        to_level=to_level,
        actor=actor,
        reason=reason,
        words=words,
        evidence={k: float(v) for k, v in (evidence or {}).items() if math.isfinite(float(v))},
        mandate_id=None if mandate is None else mandate.mandate_id,
        mandate_version=None if mandate is None else mandate.version,
        mandate_body_hash=None if mandate is None else body_hash(mandate),
        mandate_ledger_head=mandate_ledger_head,
        prev_hash=prev,
        entry_hash="",
    )
    return draft.model_copy(update={"entry_hash": _entry_hash(prev, _content(draft))})


def verify_changes(entries: Sequence[AutonomyChange]) -> ChainReport:
    """Walk the chain: sequence, links and hashes. Stops at the first bad entry."""
    prev = GENESIS_HASH
    for i, e in enumerate(entries, start=1):
        if e.seq != i:
            return ChainReport(ok=False, entries=len(entries), first_bad_seq=e.seq, problem="seq_gap")
        if e.prev_hash != prev:
            return ChainReport(ok=False, entries=len(entries), first_bad_seq=e.seq, problem="chain_broken")
        if _entry_hash(e.prev_hash, _content(e)) != e.entry_hash:
            return ChainReport(ok=False, entries=len(entries), first_bad_seq=e.seq, problem="hash_mismatch")
        prev = e.entry_hash
    return ChainReport(ok=True, entries=len(entries))


class InMemoryAutonomyLedger:
    def __init__(self, entries: Iterable[AutonomyChange] = ()) -> None:
        self._entries: list[AutonomyChange] = list(entries)
        self._lock = threading.Lock()

    def append(
        self,
        *,
        at: datetime,
        business_id: str,
        goal_id: str,
        from_level: AutonomyLevel | None,
        to_level: AutonomyLevel,
        actor: str,
        reason: str,
        words: str,
        evidence: Mapping[str, float] | None = None,
        mandate: Mandate | None = None,
        mandate_ledger_head: str | None = None,
    ) -> AutonomyChange:
        with self._lock:
            prev = self._entries[-1].entry_hash if self._entries else GENESIS_HASH
            entry = _build(
                len(self._entries) + 1,
                prev,
                at=at,
                business_id=business_id,
                goal_id=goal_id,
                from_level=from_level,
                to_level=to_level,
                actor=actor,
                reason=reason,
                words=words,
                evidence=evidence,
                mandate=mandate,
                mandate_ledger_head=mandate_ledger_head,
            )
            self._entries.append(entry)
            return entry

    def entries(self, goal_id: str | None = None) -> list[AutonomyChange]:
        return [e for e in self._entries if goal_id is None or e.goal_id == goal_id]

    def verify_chain(self) -> ChainReport:
        return verify_changes(self._entries)


autonomy_metadata = MetaData()

change_table = Table(
    "autonomy_change",
    autonomy_metadata,
    Column("seq", Integer, primary_key=True, autoincrement=False),
    Column("at", UtcDateTime(), nullable=False),
    Column("business_id", String(200), nullable=False),
    Column("goal_id", String(200), nullable=False),
    Column("entry_hash", String(64), nullable=False),
    Column("body", JSON, nullable=False),
    Index("ix_autonomy_change_goal", "goal_id", "seq"),
)


def create_autonomy_tables(engine: Engine) -> None:
    autonomy_metadata.create_all(engine)


class SqlAutonomyLedger:
    """The ledger on Postgres or SQLite. Two writers (the API's click, the worker's guard) are fine:
    `seq` is the primary key, so a writer that loses the race re-reads the head and tries again."""

    def __init__(self, engine: Engine, *, create: bool = True) -> None:
        self._engine = engine
        if create:
            create_autonomy_tables(engine)

    def append(
        self,
        *,
        at: datetime,
        business_id: str,
        goal_id: str,
        from_level: AutonomyLevel | None,
        to_level: AutonomyLevel,
        actor: str,
        reason: str,
        words: str,
        evidence: Mapping[str, float] | None = None,
        mandate: Mandate | None = None,
        mandate_ledger_head: str | None = None,
    ) -> AutonomyChange:
        t = change_table.c
        for _ in range(8):
            with self._engine.connect() as conn:
                head = conn.execute(select(t.seq, t.entry_hash).order_by(t.seq.desc()).limit(1)).first()
            seq, prev = (1, GENESIS_HASH) if head is None else (int(head.seq) + 1, str(head.entry_hash))
            entry = _build(
                seq,
                prev,
                at=at,
                business_id=business_id,
                goal_id=goal_id,
                from_level=from_level,
                to_level=to_level,
                actor=actor,
                reason=reason,
                words=words,
                evidence=evidence,
                mandate=mandate,
                mandate_ledger_head=mandate_ledger_head,
            )
            try:
                with self._engine.begin() as conn:
                    conn.execute(
                        change_table.insert().values(
                            seq=entry.seq,
                            at=entry.at,
                            business_id=business_id,
                            goal_id=goal_id,
                            entry_hash=entry.entry_hash,
                            body=entry.model_dump(mode="json"),
                        )
                    )
            except IntegrityError:
                continue  # another writer took this seq: chain onto theirs
            return entry
        raise RuntimeError("could not append to the autonomy ledger: too many concurrent writers")

    def entries(self, goal_id: str | None = None) -> list[AutonomyChange]:
        t = change_table.c
        query = select(t.body).order_by(t.seq)
        if goal_id is not None:
            query = query.where(t.goal_id == goal_id)
        with self._engine.connect() as conn:
            return [AutonomyChange.model_validate(b) for b in conn.execute(query).scalars()]

    def verify_chain(self) -> ChainReport:
        return verify_changes(self.entries())


# --- the guard --------------------------------------------------------------------------------------


class AlertReader(Protocol):
    def list_alerts(
        self, *, goal_id: str | None = None, since: datetime | None = None, limit: int | None = None
    ) -> list[Alert]: ...


@dataclass
class GuardReport:
    promoted: list[str] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)
    logged: list[str] = field(default_factory=list)
    """Goals whose level changed elsewhere and was logged now."""
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Proof:
    at: datetime
    since: datetime
    readiness: Readiness | None
    losing: Estimate | None
    closed: int


class AutopilotGuard:
    """Offers Autopilot when a goal earns it, promotes when pre-authorised, demotes when proof turns.

    `run(now)` is a runtime tick hook (cheap: the logs are read at most once per `rule.proof_every`
    per goal, and safety stops are read from the alert store). `promote(goal_id, now, by=...)` is
    the one click. `readiness(goal_id, now)` is what the inbox and the API show."""

    def __init__(
        self,
        *,
        business_id: str,
        workspace: Workspace,
        decisions: DecisionLog,
        events: EventStore,
        ledger: AutonomyLedger,
        alerts: AlertReader | None = None,
        rule: PromotionRule | None = None,
        mandate_for: Callable[[str], Mandate | None] | None = None,
        mandate_verified: Callable[[Mandate, datetime], bool] | None = None,
        mandate_ledger_head: Callable[[], str] | None = None,
        governed: bool = False,
        data_trusted: Callable[[datetime], bool] | None = None,
        channel_costs: Mapping[Any, float] | None = None,
        margin_rate: float = 1.0,
        timezone: str = "UTC",
    ) -> None:
        self.business_id = business_id
        self.workspace = workspace
        self.decisions = decisions
        self.events = events
        self.ledger = ledger
        self.alerts = alerts
        self.rule = rule or PromotionRule()
        self.mandate_for = mandate_for
        self.mandate_verified = mandate_verified
        """Whether the mandate's signatures authorise it (the runtime's `MandateSource.problems`)."""
        self.mandate_ledger_head = mandate_ledger_head
        self.governed = governed or mandate_for is not None
        self.data_trusted = data_trusted
        self.channel_costs = dict(channel_costs or {})
        self.margin_rate = margin_rate
        self.timezone = timezone
        self._proof: dict[tuple[str, AutonomyLevel], _Proof] = {}
        self._lock = threading.RLock()

    # --- reading ---------------------------------------------------------------------------------

    def goal(self, goal_id: str) -> Goal | None:
        return next((g for g in self.workspace.goals() if g.goal_id == goal_id), None)

    def brief(self, goal_id: str) -> Brief | None:
        briefs = self.workspace.briefs(goal_id)
        return max(briefs, key=lambda b: b.version) if briefs else None

    def since(self, goal: Goal) -> datetime:
        """When the goal started at its current level: its last logged change to it, else its plan's
        approval, else its start."""
        entries = self.ledger.entries(goal.goal_id)
        if entries and entries[-1].to_level is goal.autonomy:
            return entries[-1].at
        brief = self.brief(goal.goal_id)
        if brief is not None and brief.approved_at is not None:
            return max(brief.approved_at, goal.starts_at)
        return goal.starts_at

    def readiness(self, goal_id: str, now: datetime) -> Readiness | None:
        """The goal's latest readiness (recomputed at most once per `rule.proof_every`), or None."""
        goal = self.goal(goal_id)
        if goal is None or goal.autonomy is AutonomyLevel.AUTOPILOT or goal.status is not GoalStatus.ACTIVE:
            return None
        proof = self._evaluate(goal, now)
        return None if proof is None else proof.readiness

    def offers(self, now: datetime) -> list[Readiness]:
        """Goals that have earned Autopilot and wait for one click (not pre-authorised)."""
        out: list[Readiness] = []
        for goal in self.workspace.goals():
            r = self.readiness(goal.goal_id, now)
            if r is not None and r.ready and not self._auto_allowed(goal, now):
                out.append(r)
        return out

    def demotion(self, goal_id: str, now: datetime | None = None) -> AutonomyChange | None:
        """The automatic step back to Approve that still needs a person, if any.

        It stops needing one when a person sets the level, when a person resumes a goal its safety
        stop had paused (they acted on it: the pause was the item), when the goal earns Autopilot
        again (the offer takes its place), or, after a demotion for losing money, once proof since
        the demotion is no longer negative."""
        goal = self.goal(goal_id)
        entries = self.ledger.entries(goal_id)
        if goal is None or not entries:
            return None
        last = entries[-1]
        if not (
            last.actor == GUARD_ACTOR
            and last.to_level is AutonomyLevel.APPROVE
            and goal.autonomy is AutonomyLevel.APPROVE
        ):
            return None
        if last.evidence.get(PAUSED_EVIDENCE, 0.0) > 0 and goal.status is GoalStatus.ACTIVE:
            return None  # paused when demoted, running now: only a person resumes a paused goal
        if now is not None and goal.status is GoalStatus.ACTIVE:
            r = self.readiness(goal_id, now)
            if r is not None and r.ready:
                return None
            if (
                last.reason == REASON_LOSING
                and r is not None
                and r.observed is not None
                and r.observed.high >= 0
            ):
                return None
        return last

    # --- acting ----------------------------------------------------------------------------------

    def run(self, now: datetime) -> GuardReport:
        report = GuardReport()
        with self._lock:
            for goal in list(self.workspace.goals()):
                try:
                    self._log_outside_change(goal, now, report)
                    if goal.autonomy is AutonomyLevel.AUTOPILOT:
                        self._maybe_demote(goal, now, report)
                    elif goal.status is GoalStatus.ACTIVE and self._auto_allowed(goal, now):
                        r = self.readiness(goal.goal_id, now)
                        if r is not None and r.ready:
                            self._set_level(goal, r, now, actor=GUARD_ACTOR, reason=REASON_AUTO_PROMOTED)
                            report.promoted.append(goal.goal_id)
                except Exception as err:  # one goal's failure never stops the others
                    report.errors.append(f"{goal.goal_id}: {type(err).__name__}: {err}")
        return report

    def __call__(self, now: datetime) -> GuardReport:
        """A runtime `before_tick` hook."""
        return self.run(now)

    def promote(self, goal_id: str, now: datetime, *, by: str) -> AutonomyChange:
        """The one click: turn on Autopilot for a goal that has earned it. Raises ValueError otherwise."""
        with self._lock:
            goal = self.goal(goal_id)
            if goal is None:
                raise KeyError(goal_id)
            if goal.autonomy is AutonomyLevel.AUTOPILOT:
                raise ValueError("Autopilot is already on for this goal.")
            r = self.readiness(goal_id, now)
            if r is None or not r.ready:
                raise ValueError(r.sentence if r is not None else "This goal is not running.")
            return self._set_level(goal, r, now, actor=f"person:{by}", reason=REASON_PROMOTED)

    def record(
        self,
        goal_id: str,
        now: datetime,
        *,
        from_level: AutonomyLevel | None,
        to_level: AutonomyLevel,
        actor: str,
        reason: str,
        words: str,
    ) -> AutonomyChange:
        """Log a change made elsewhere (a person's click in the inbox) against the goal's mandate."""
        return self.ledger.append(
            at=now,
            business_id=self.business_id,
            goal_id=goal_id,
            from_level=from_level,
            to_level=to_level,
            actor=actor,
            reason=reason,
            words=words,
            mandate=self._mandate(goal_id),
            mandate_ledger_head=self._head(),
        )

    # --- internals -------------------------------------------------------------------------------

    def _auto_allowed(self, goal: Goal, now: datetime) -> bool:
        """Automatic promotion: only from Shadow (a person who chose Approve keeps it until they change
        it), only under a verified mandate carrying the clause, and never again after an automatic
        demotion until a person turns Autopilot on."""
        if goal.autonomy is not AutonomyLevel.SHADOW:
            return False
        mandate = None if self.mandate_for is None else self.mandate_for(goal.goal_id)
        if mandate is None or not allows_auto_promotion(mandate, now) or not self._verified(mandate, now):
            return False
        entries = self.ledger.entries(goal.goal_id)
        demoted = [e.at for e in entries if e.actor == GUARD_ACTOR and e.to_level is AutonomyLevel.APPROVE]
        turned_on = [
            e.at for e in entries if e.actor.startswith("person:") and e.to_level is AutonomyLevel.AUTOPILOT
        ]
        return not demoted or (bool(turned_on) and max(turned_on) > max(demoted))

    def _verified(self, mandate: Mandate, now: datetime) -> bool:
        if self.mandate_verified is None:
            return True
        try:
            return self.mandate_verified(mandate, now)
        except Exception:  # cannot tell: never pre-authorised on signatures nobody checked
            return False

    def _mandate(self, goal_id: str) -> Mandate | None:
        return None if self.mandate_for is None else self.mandate_for(goal_id)

    def _head(self) -> str | None:
        return None if self.mandate_ledger_head is None else self.mandate_ledger_head()

    def _log_outside_change(self, goal: Goal, now: datetime, report: GuardReport) -> None:
        entries = self.ledger.entries(goal.goal_id)
        if not entries:
            return  # never logged: the plan's approval is its start (see `since`)
        last = entries[-1]
        if last.to_level is goal.autonomy:
            return
        brief = self.brief(goal.goal_id)
        approved = brief.approved_at if brief is not None else None
        at = approved if approved is not None and approved > last.at and approved <= now else now
        self.ledger.append(
            at=at,
            business_id=self.business_id,
            goal_id=goal.goal_id,
            from_level=last.to_level,
            to_level=goal.autonomy,
            actor="person:plan",
            reason=REASON_SEEN,
            words=f"Set to {goal.autonomy.value} on the plan page.",
            mandate=self._mandate(goal.goal_id),
            mandate_ledger_head=self._head(),
        )
        report.logged.append(goal.goal_id)

    def _set_level(
        self, goal: Goal, r: Readiness, now: datetime, *, actor: str, reason: str
    ) -> AutonomyChange:
        evidence: dict[str, float] = {
            "days": r.days,
            "contacts": float(r.contacts),
            "closed": float(r.closed),
        }
        if r.forecast is not None:
            evidence.update(
                forecast=r.forecast.point, forecast_low=r.forecast.low, forecast_high=r.forecast.high
            )
        if r.value_error is not None:
            evidence.update(value_error=r.value_error.point, value_checks=float(r.value_checks))
        if r.observed is not None:
            evidence.update(
                observed=r.observed.point, observed_low=r.observed.low, observed_high=r.observed.high
            )
        self.workspace.put_goal(goal.model_copy(update={"autonomy": AutonomyLevel.AUTOPILOT}))
        return self.ledger.append(
            at=now,
            business_id=self.business_id,
            goal_id=goal.goal_id,
            from_level=goal.autonomy,
            to_level=AutonomyLevel.AUTOPILOT,
            actor=actor,
            reason=reason,
            words=r.sentence,
            evidence=evidence,
            mandate=self._mandate(goal.goal_id),
            mandate_ledger_head=self._head(),
        )

    def _maybe_demote(self, goal: Goal, now: datetime, report: GuardReport) -> None:
        since = self.since(goal)
        stops = self._safety_stops(goal.goal_id, since)
        reason = words = ""
        evidence: dict[str, float] = {}
        if stops:
            first = stops[0]
            reason = f"safety_stop:{first.code}" if first.code != NEGATIVE_LIFT_CODE else REASON_LOSING
            words = (
                "Dropped to Approve: proof shows it losing money against the people we never contact."
                if reason == REASON_LOSING
                else f"Dropped to Approve: a safety stop tripped ({first.code.removeprefix(SAFETY_PREFIX).replace('_', ' ')})."
            )
        else:
            proof = self._evaluate(goal, now)
            if proof is not None and proof.losing is not None:
                reason = REASON_LOSING
                evidence = {
                    "incremental_profit": proof.losing.point,
                    "incremental_profit_low": proof.losing.low,
                    "incremental_profit_high": proof.losing.high,
                    "closed": float(proof.closed),
                }
                words = (
                    "Dropped to Approve: proof shows it losing "
                    f"{_money(-proof.losing.point, goal.currency)} ({_range(proof.losing, goal.currency)}) "
                    "against the people we never contact."
                )
        if not reason:
            return
        if goal.status is GoalStatus.PAUSED:
            evidence[PAUSED_EVIDENCE] = 1.0  # the breaker paused it too: resuming it is the person's act
        self.workspace.put_goal(goal.model_copy(update={"autonomy": AutonomyLevel.APPROVE}))
        self.ledger.append(
            at=now,
            business_id=self.business_id,
            goal_id=goal.goal_id,
            from_level=AutonomyLevel.AUTOPILOT,
            to_level=AutonomyLevel.APPROVE,
            actor=GUARD_ACTOR,
            reason=reason,
            words=words,
            evidence=evidence,
            mandate=self._mandate(goal.goal_id),
            mandate_ledger_head=self._head(),
        )
        report.demoted.append(goal.goal_id)

    def _safety_stops(self, goal_id: str, since: datetime) -> list[Alert]:
        if self.alerts is None:
            return []
        rows = self.alerts.list_alerts(goal_id=goal_id, since=since)
        stops = [
            a
            for a in rows
            if a.goal_id == goal_id
            and a.severity == "critical"
            and (a.code.startswith(SAFETY_PREFIX) or a.code == NEGATIVE_LIFT_CODE)
        ]
        return sorted(stops, key=lambda a: (a.at, a.alert_id))

    def _evaluate(self, goal: Goal, now: datetime) -> _Proof | None:
        """Read the goal's logs and judge it, at most once per `rule.proof_every` per level."""
        brief = self.brief(goal.goal_id)
        if brief is None:
            return None
        since = self.since(goal)
        key = (goal.goal_id, goal.autonomy)
        cached = self._proof.get(key)
        if (
            cached is not None
            and cached.since == since
            and timedelta(0) <= now - cached.at < self.rule.proof_every
        ):
            return cached
        decisions, by_customer = self._logs(goal, since, now)
        costs = Costs.from_brief(brief, channel=self.channel_costs, margin_rate=self.margin_rate)
        readiness: Readiness | None = None
        losing: Estimate | None = None
        closed = 0
        long_enough = now - since >= timedelta(days=self.rule.min_proof_horizons * brief.horizon_days)
        if goal.autonomy is AutonomyLevel.AUTOPILOT and long_enough:
            rewards = proof_rewards(
                decisions, by_customer, brief.horizon_days, brief.objective, costs, now=now
            )
            closed = len(rewards)
            if closed >= self.rule.min_closed_for_demotion:
                summary = proof_summary(rewards, decisions, now, goal.goal_id, min_group=self.rule.min_group)
                profit = summary.incremental_profit
                if profit is not None and profit.high < 0:
                    losing = profit
        elif goal.autonomy is not AutonomyLevel.AUTOPILOT:
            mandate = self._mandate(goal.goal_id)
            mandate_ok: bool | None = None
            if self.governed:
                mandate_ok = (
                    mandate is not None
                    and mandate.effective_status(now) is MandateStatus.ACTIVE
                    and mandate.is_valid_at(now)
                    and self._verified(mandate, now)
                )
            trusted = True
            if self.data_trusted is not None:
                try:
                    trusted = self.data_trusted(now)
                except Exception:
                    trusted = False
            readiness = assess(
                goal,
                brief,
                decisions,
                by_customer,
                now,
                since=since,
                rule=self.rule,
                costs=costs,
                margin_rate=self.margin_rate,
                safety_stops=self._safety_stops(goal.goal_id, now - timedelta(days=self.rule.quiet_days)),
                data_trusted=trusted,
                mandate_ok=mandate_ok,
                others_acting=self._others_acting(goal),
                auto=allows_auto_promotion(mandate, now),
            )
        proof = _Proof(at=now, since=since, readiness=readiness, losing=losing, closed=closed)
        self._proof[key] = proof
        return proof

    def _others_acting(self, goal: Goal) -> bool:
        return any(
            g.goal_id != goal.goal_id
            and g.status is GoalStatus.ACTIVE
            and g.autonomy in (AutonomyLevel.APPROVE, AutonomyLevel.AUTOPILOT)
            for g in self.workspace.goals()
        )

    def _logs(
        self, goal: Goal, since: datetime, now: datetime
    ) -> tuple[list[Decision], dict[str, list[Event]]]:
        resolve = self._resolver()
        tick = timedelta(microseconds=1)
        decisions = [
            d for d in self.decisions.scan(since=since, until=now + tick) if resolve(d) == goal.goal_id
        ]
        customers = {d.customer_id for d in decisions}
        by_customer: dict[str, list[Event]] = defaultdict(list)
        if customers:
            for e in self.events.scan(since=since - timedelta(days=1), until=now + tick):
                if e.customer_id in customers:
                    by_customer[e.customer_id].append(e)
        return decisions, dict(by_customer)

    def _resolver(self) -> Callable[[Decision], str | None]:
        """decision -> its goal: the action's, the best bid's, else the runtime's derived id (every
        holdout customer's "do nothing" carries no bid). With one running goal, that goal."""
        goals = self.workspace.goals()
        ids = [g.goal_id for g in goals]
        live = [g.goal_id for g in goals if g.status is not GoalStatus.DRAFT]
        derive: Callable[[Decision], str | None] | None
        try:  # the runtime's own rule; imported late so this module stays light
            from zoneinfo import ZoneInfo

            from autonomy.runtime.service import _derived_goal

            tz = ZoneInfo(self.timezone)

            def derive(d: Decision) -> str | None:
                return _derived_goal(d, ids, tz, ())

        except ImportError:  # pragma: no cover - the light environment without the runtime's extras
            derive = None

        def resolve(d: Decision) -> str | None:
            if d.action is not None:
                return d.action.goal_id
            if d.bids:
                return d.bids[0].goal_id
            if derive is not None and len(live) != 1:
                return derive(d)
            return live[0] if len(live) == 1 else None

        return resolve
