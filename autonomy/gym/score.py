"""The scoreboard: run a policy in a Gym world, then score it against the holdout and the planted truth.

A run steps day by day like `autonomy.loop.run_days`: events are folded into customer state, every
non-holdout customer gets one decision a day, decisions are acted on and time moves on. Customers are
split three ways by a salted hash of their id (plan-v2 section 8): the policy under test decides for
the agent group, a simple marketer's rule (`RuleBaseline`) decides for the baseline group, and the
holdout is never contacted. Shares come from the brief's envelope.

Two kinds of number come out, and the difference matters:

* **Realised** incremental profit is what a client would see: the agent group's mean net per customer
  minus the holdout's, times the agent group's size, with a 95% interval. It is noisy, like real life.
* **True** value is what only the Gym knows: the sum of `truth.expected_net` over the actions taken.
  It has no sampling noise, so policies are ranked on it, the oracle share is computed from it, and
  the realised interval is checked against it (it should cover the truth about 95% of the time).

A policy may also define `prepare_day(states, ctx)`: the scoreboard calls it with the day's states of
the group it decides for, before the per-customer `decide` calls, so a policy can work out the whole
day in one vectorised pass (the oracle does). It is optional and Gym-only.

A policy may also define `nightly(decisions, events_by_customer, now)`, the runtime's nightly hook.
When `learn` is on, the scoreboard calls it once with the history after `reset` and then every
simulated morning before the sweep, with what is new since the last call: the previous day's ACT
decisions of the agent and baseline groups, a "do nothing" for each holdout customer (the policy is
asked to decide for its holdout too, as the runtime logs one for every customer; nothing it says for
them is acted on), and the events, by customer. `Policy.learn` is still called with closed rewards.

Net per customer is margin from purchases during the run (plus the effect window after the last
decision day, and one day for sends scheduled after it) minus channel cost per send, offer cost per
redeemed offer, and the future value lost when a message makes them unsubscribe or complain. A
purchase counts as redeeming an offer when the world tags it with `attrs["offer_cost"]` (money) or
`attrs["offer_id"]` (costed from the brief's offers); margins are taken as reported. Lost value is
read from `attrs["lost_value"]` on UNSUBSCRIBE and COMPLAINT events, each event counted once as the
world reports it; a world that does not report it charges nothing, as before.

The judgement comes in two forms. `pass_criteria` is the plan's original bar (plan-v2 section 8),
kept as written. `staged_criteria` is the honest, staged bar of docs/v2/GYM_BAR.md: Stage 1 Safe,
Stage 2 Useful, Stage 3 Smart, every stage reported, with the full-oracle share as a stretch metric.
"""

from __future__ import annotations

import hashlib
import math
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol, TypeAlias, cast

import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import Field

from autonomy.contracts import (
    Action,
    Brief,
    Channel,
    ContentItem,
    Contract,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Estimate,
    Event,
    EventType,
    Group,
    Reward,
)
from autonomy.fakes.folder import SimpleStateFolder
from autonomy.gym.api import GroundTruth, GymEnvironment, ScenarioSpec
from autonomy.gym.baselines import (
    BatchTruth,
    BestFixedRule,
    ContactEveryone,
    OraclePolicy,
    RuleBaseline,
    SegmentOracle,
    weekly_cap,
)
from autonomy.gym.scenarios import PLANTED_HARM, default_brief, default_content
from autonomy.interfaces import Environment, Policy, StateFolder

ORACLE = "oracle"
EVERYONE = "contact_everyone"
RULE = "rule_baseline"
NOTHING = "nothing"
BEST_FIXED = "best_fixed_rule"
SEGMENT_ORACLE = "segment_oracle"
BASELINE_NAMES: frozenset[str] = frozenset({ORACLE, EVERYONE, RULE, NOTHING, BEST_FIXED, SEGMENT_ORACLE})

IntervalMethod = Literal["normal", "bootstrap"]
Z95 = 1.959963984540054


class SimWorld(Environment, GymEnvironment, Protocol):
    """What the scoreboard runs in: the `Environment` seam plus the Gym's truth and rule log."""


PolicyFactory: TypeAlias = Callable[[SimWorld], Policy]
"""Builds a fresh policy for one run. The world is passed so the oracle can read its truth."""
PolicyLike: TypeAlias = Policy | PolicyFactory
WorldFactory: TypeAlias = Callable[[ScenarioSpec, Sequence[Brief], Sequence[ContentItem]], SimWorld]
"""Builds the world for one run from the scenario and the brief and content the policies will use."""


def oracle_factory(world: SimWorld) -> Policy:
    return OraclePolicy(world.truth)


def everyone_factory(world: SimWorld) -> Policy:  # noqa: ARG001
    return ContactEveryone()


def rule_factory(world: SimWorld) -> Policy:  # noqa: ARG001
    return RuleBaseline()


def best_fixed_factory(world: SimWorld) -> Policy:  # noqa: ARG001
    """`BestFixedRule` at its default hour. It is never handed the world's truth."""
    return BestFixedRule()


def segment_oracle_factory(world: SimWorld) -> Policy:
    """`SegmentOracle` on the world's truth, counting purchase frequency from the start of the
    scenario's history (what the business's data covers), as the agent does."""
    return SegmentOracle(world.truth, history_days=world.spec.history_days)


def sleeping_dog_signal(spec: ScenarioSpec) -> float:
    """How visible sleeping dogs are in observable data (`ScenarioSpec.sleeping_dog_signal`): 0 when
    the scenario plants no correlate, which makes "contact fewer of them" a criterion no truth-blind
    policy can meet."""
    return float(spec.sleeping_dog_signal)


def gym_world(spec: ScenarioSpec, briefs: Sequence[Brief], content: Sequence[ContentItem]) -> SimWorld:
    """The real Gym world, told the offers, content themes, quiet hours and cap the briefs use.
    Imported only when needed, so the scoreboard loads (and its tests run) without it."""
    from autonomy.gym.world import GymWorld

    offers = {o.offer_id: o for b in briefs for o in b.envelope.offers}
    envelope = briefs[0].envelope if briefs else None
    world = GymWorld(
        spec,
        offers=tuple(offers.values()),
        content=tuple(content),
        quiet_start_hour=envelope.quiet_start_hour if envelope is not None else 21,
        quiet_end_hour=envelope.quiet_end_hour if envelope is not None else 9,
        contact_cap=weekly_cap(briefs) if briefs else 2,
    )
    return cast(SimWorld, world)


# --- the three-way split -------------------------------------------------------------------------


def split_value(customer_id: str, salt: str) -> float:
    """A uniform number in [0, 1) fixed by the customer id and the salt."""
    digest = hashlib.sha256(f"{salt}\x1f{customer_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2.0**64


def assign_group(customer_id: str, *, holdout_share: float, baseline_share: float, salt: str) -> Group:
    """Holdout for the lowest `holdout_share` of hash values, baseline for the next slice, else agent."""
    u = split_value(customer_id, salt)
    if u < holdout_share:
        return Group.HOLDOUT
    if u < holdout_share + baseline_share:
        return Group.BASELINE
    return Group.AGENT


def split_shares(briefs: Sequence[Brief]) -> tuple[float, float]:
    """(holdout, baseline) shares from the first brief's envelope. A customer sits in one group for
    every goal, so one brief has to set the split; the plan's 10% / 10% when there is none."""
    if not briefs:
        return 0.10, 0.10
    env = briefs[0].envelope
    return env.holdout_share, env.baseline_share


# --- interval maths ------------------------------------------------------------------------------


def difference_interval(
    treated: Sequence[float] | npt.NDArray[np.float64],
    control: Sequence[float] | npt.NDArray[np.float64],
    *,
    scale: float = 1.0,
    method: IntervalMethod = "normal",
    n_boot: int = 2000,
    seed: int = 0,
) -> Estimate:
    """(mean(treated) - mean(control)) x scale, with a 95% interval.

    `normal` (the default) uses Welch's standard error, sqrt(s_t^2/n_t + s_c^2/n_c) with n-1 in the
    variances, and +/- 1.96 standard errors: groups are hundreds to thousands of customers, so the
    central limit theorem holds well even though single-customer nets are skewed.
    `bootstrap` resamples each group with replacement `n_boot` times and takes the 2.5th and 97.5th
    percentiles of the difference; slower, and a cross-check for small, very skewed groups.
    """
    t = np.asarray(treated, dtype=np.float64)
    c = np.asarray(control, dtype=np.float64)
    if t.size == 0 or c.size == 0:
        raise ValueError("both groups need at least one customer")
    diff = float(t.mean() - c.mean())
    if method == "normal":
        var_t = float(t.var(ddof=1)) if t.size > 1 else 0.0
        var_c = float(c.var(ddof=1)) if c.size > 1 else 0.0
        se = math.sqrt(var_t / t.size + var_c / c.size)
        low, high = diff - Z95 * se, diff + Z95 * se
    elif method == "bootstrap":
        rng = np.random.default_rng(seed)
        diffs = np.empty(n_boot, dtype=np.float64)
        chunk = max(1, 1_000_000 // max(t.size, c.size))
        for i in range(0, n_boot, chunk):
            k = min(chunk, n_boot - i)
            mt = t[rng.integers(0, t.size, size=(k, t.size))].mean(axis=1)
            mc = c[rng.integers(0, c.size, size=(k, c.size))].mean(axis=1)
            diffs[i : i + k] = mt - mc
        low = min(float(np.percentile(diffs, 2.5)), diff)
        high = max(float(np.percentile(diffs, 97.5)), diff)
    else:
        raise ValueError(f"unknown interval method {method!r}")
    lo, hi = sorted((low * scale, high * scale))
    return Estimate(point=diff * scale, low=min(lo, diff * scale), high=max(hi, diff * scale))


def wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """The Wilson 95% interval for a proportion; (0, 1) when there are no trials."""
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    low = 0.0 if successes <= 0 else max(0.0, centre - half)
    high = 1.0 if successes >= n else min(1.0, centre + half)
    return low, high


# --- the scorecard -------------------------------------------------------------------------------


class ScoreCard(Contract):
    """One policy, one scenario, one seed. Money is in the scenario's currency."""

    scenario: str
    policy: str
    seed: int
    days: int
    n_customers: int
    n_agent: int
    n_baseline: int
    n_holdout: int
    incremental_profit: Estimate = Field(
        description="Realised: (agent mean net - holdout mean net) x agent group size, 95% interval"
    )
    lift_vs_baseline: Estimate | None = Field(
        default=None, description="Realised: (agent mean net - baseline mean net) x agent group size"
    )
    interval_method: str = "normal"
    true_value: float = Field(description="Sum of truth.expected_net over the agent group's actions")
    baseline_true_value: float = Field(default=0.0, description="The same for the baseline group")
    interval_covers_truth: bool
    oracle_value: float | None = Field(default=None, description="The oracle's true value, same seed")
    oracle_share: float | None = Field(default=None, description="true_value / oracle_value")
    actions: int = Field(description="ACT decisions in the agent group")
    contacts: int = Field(description="Sends the world carried out for the agent group")
    contacts_per_customer_week: float
    unsubscribe_rate: float = Field(description="Share of agent-group customers who unsubscribed")
    spend: float = Field(default=0.0, description="Agent group's channel and offer costs")
    sleeping_dogs: int = Field(default=0, description="Planted sleeping dogs in the agent group")
    sleeping_dogs_contacted: int = 0
    sleeping_dog_contact_share: float | None = None
    violations: int = Field(
        description="Rule breaks: the world's (consent, quiet hours) plus caps and holdout"
    )
    violation_examples: tuple[str, ...] = ()
    planted_harm: bool = False
    breaker_batches: int | None = Field(
        default=None,
        description="Planted-harm scenarios: daily batches from the harm's start to the agent group's "
        "last send, when it then stays silent to the end; None if it never stopped (or no harm planted)",
    )
    lost_value: float = Field(
        default=0.0,
        description="Realised future value the agent group lost to unsubscribes and complaints "
        "(attrs['lost_value']); already taken off incremental_profit",
    )
    segment_oracle_value: float | None = Field(
        default=None, description="The segment oracle's true value on the same seed"
    )
    share_of_segment_oracle: float | None = Field(
        default=None, description="true_value / segment_oracle_value"
    )
    best_fixed_rule_value: float | None = Field(
        default=None, description="The best fixed rule's true value on the same seed"
    )
    value_vs_best_fixed_rule: float | None = Field(
        default=None, description="true_value - best_fixed_rule_value: positive when the policy beats it"
    )
    sleeping_dog_signal: float = Field(
        default=0.0, description="How visible sleeping dogs are in observable data (0: not at all)"
    )


# --- one run -------------------------------------------------------------------------------------


class GymFolder:
    """`SimpleStateFolder` plus the customer's time zone and country when an event carries them in
    `attrs` (the Gym puts them on signup), so send-time choices can use the customer's clock."""

    def __init__(self, inner: StateFolder | None = None) -> None:
        self.inner: StateFolder = inner if inner is not None else SimpleStateFolder()

    def fold(self, state: CustomerState | None, event: Event) -> CustomerState:
        s = self.inner.fold(state, event)
        upd: dict[str, object] = {}
        tz, country = event.attrs.get("timezone"), event.attrs.get("country")
        if isinstance(tz, str) and tz != s.timezone:
            upd["timezone"] = tz
        if isinstance(country, str) and country != s.country:
            upd["country"] = country
        return s.model_copy(update=upd) if upd else s

    def refresh(self, state: CustomerState, now: datetime) -> CustomerState:
        return self.inner.refresh(state, now)


@dataclass
class _Purchase:
    ts: datetime
    value: float
    margin: float
    offer_cost: float


def _number(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        return None
    return float(v)


@dataclass
class _Ledger:
    """Everything the scorecard needs, gathered from events as they arrive."""

    start: datetime
    channel_cost: Mapping[Channel, float]
    offer_cost: Mapping[str, float]
    horizon: Mapping[str, int]
    net: dict[str, float] = field(default_factory=dict)
    spend: dict[str, float] = field(default_factory=dict)
    lost: dict[str, float] = field(default_factory=dict)
    purchases: dict[str, list[_Purchase]] = field(default_factory=dict)
    sends: dict[str, list[datetime]] = field(default_factory=dict)
    unsubscribed: set[str] = field(default_factory=set)
    unsub_decisions: set[str] = field(default_factory=set)
    complaint_decisions: set[str] = field(default_factory=set)
    pending: list[Decision] = field(default_factory=list)

    def _add(self, book: dict[str, float], cid: str, amount: float) -> None:
        book[cid] = book.get(cid, 0.0) + amount

    def observe(self, events: Iterable[Event]) -> None:
        for e in events:
            if e.ts < self.start:
                continue
            cid = e.customer_id
            if e.type is EventType.PURCHASE:
                cost = _number(e.attrs.get("offer_cost"))
                if cost is None:
                    oid = e.attrs.get("offer_id")
                    cost = self.offer_cost.get(oid, 0.0) if isinstance(oid, str) else 0.0
                margin = e.margin or 0.0
                self._add(self.net, cid, margin - cost)
                self._add(self.spend, cid, cost)
                self.purchases.setdefault(cid, []).append(_Purchase(e.ts, e.value or 0.0, margin, cost))
            elif e.type is EventType.REFUND:
                self._add(self.net, cid, -(e.margin or 0.0))
            elif e.type is EventType.SEND and e.channel is not None:
                cost = self.channel_cost.get(e.channel, 0.0)
                self._add(self.net, cid, -cost)
                self._add(self.spend, cid, cost)
                self.sends.setdefault(cid, []).append(e.ts)
            elif e.type in (EventType.UNSUBSCRIBE, EventType.COMPLAINT):
                self.unsubscribed.add(cid)
                lost = _number(e.attrs.get("lost_value"))
                if lost is not None and lost != 0.0:
                    self._add(self.net, cid, -lost)
                    self._add(self.lost, cid, lost)
                if e.decision_id is not None:
                    book = (
                        self.unsub_decisions if e.type is EventType.UNSUBSCRIBE else self.complaint_decisions
                    )
                    book.add(e.decision_id)

    def close_windows(self, now: datetime) -> tuple[list[Reward], dict[str, Decision]]:
        """Rewards for agent decisions whose outcome window has ended by `now` (the nightly loop),
        with those decisions by id, ready for `Policy.learn`."""
        due: list[Decision] = []
        keep: list[Decision] = []
        for d in self.pending:
            assert d.action is not None
            end = d.action.send_at + timedelta(days=self.horizon.get(d.action.goal_id, 14))
            (due if end <= now else keep).append(d)
        self.pending = keep
        rewards: list[Reward] = []
        for d in due:
            assert d.action is not None
            a = d.action
            end = a.send_at + timedelta(days=self.horizon.get(a.goal_id, 14))
            window = [p for p in self.purchases.get(d.customer_id, []) if a.send_at < p.ts <= end]
            margin = sum(p.margin for p in window)
            offer = sum(p.offer_cost for p in window) if a.offer_id != "none" else 0.0
            cost = self.channel_cost.get(a.channel, 0.0) + offer
            rewards.append(
                Reward(
                    decision_id=d.decision_id,
                    customer_id=d.customer_id,
                    goal_id=a.goal_id,
                    window_end=end,
                    converted=bool(window),
                    revenue=sum(p.value for p in window),
                    margin=margin,
                    cost=cost,
                    net=margin - cost,
                    unsubscribed=d.decision_id in self.unsub_decisions,
                    complained=d.decision_id in self.complaint_decisions,
                )
            )
        return rewards, {d.decision_id: d for d in due}


@dataclass
class _Run:
    groups: dict[str, Group]
    ledger: _Ledger
    true_value: dict[Group, float]
    actions: dict[Group, int]
    daily_agent_actions: list[int]
    world_violations: list[str]


def true_values(truth: GroundTruth, decisions: Sequence[Decision], now: datetime) -> list[float]:
    """`truth.expected_net` of each ACT decision's action at `now`. With a `BatchTruth`, decisions
    sharing an action are valued in one vectorised call."""
    acts = [d.action for d in decisions if d.action is not None]
    if len(acts) != len(decisions):
        raise ValueError("only ACT decisions have a true value")
    if not isinstance(truth, BatchTruth):
        return [truth.expected_net(d.customer_id, a, now) for d, a in zip(decisions, acts, strict=True)]
    rows: dict[Action, list[int]] = {}
    for k, a in enumerate(acts):
        rows.setdefault(a, []).append(k)
    out = [0.0] * len(decisions)
    for a, ks in rows.items():
        idx = np.array([truth.index(decisions[k].customer_id) for k in ks], dtype=np.int64)
        for k, v in zip(ks, truth.net(idx, a, now).tolist(), strict=True):
            out[k] = float(v)
    return out


def _contacts_stale(state: CustomerState, now: datetime) -> bool:
    """True if a contact has aged out of the 7- or 30-day window since the state was last refreshed:
    the only time-dependent fields `SimpleStateFolder.refresh` recomputes."""
    recent = state.recent_contacts
    if not recent:
        return state.contacts_7d != 0 or state.contacts_30d != 0
    if recent[0] <= now - timedelta(days=30) or len(recent) != state.contacts_30d:
        return True
    week_ago = now - timedelta(days=7)
    return sum(1 for t in recent if t > week_ago) != state.contacts_7d


def _by_customer(events: Iterable[Event]) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = {}
    for e in events:
        out.setdefault(e.customer_id, []).append(e)
    return out


def _fold(events: Iterable[Event], states: dict[str, CustomerState], folder: StateFolder) -> None:
    for e in events:
        states[e.customer_id] = folder.fold(states.get(e.customer_id), e)


def _simulate(
    world: SimWorld,
    agent: Policy,
    baseline: Policy,
    *,
    spec: ScenarioSpec,
    briefs: Sequence[Brief],
    content: Sequence[ContentItem],
    seed: int,
    folder: StateFolder,
    decide_hour: int,
    salt: str,
    learn: bool,
    refresh_all: bool,
) -> _Run:
    history = world.reset(seed)
    start = world.now()
    holdout_share, baseline_share = split_shares(briefs)
    ledger = _Ledger(
        start=start,
        channel_cost=spec.channel_cost,
        offer_cost={o.offer_id: o.cost_per_redemption for b in briefs for o in b.envelope.offers},
        horizon={b.goal_id: b.horizon_days for b in briefs},
    )
    states: dict[str, CustomerState] = {}
    _fold(history, states, folder)
    ledger.observe(history)
    nightly = getattr(agent, "nightly", None) if learn else None
    if not callable(nightly):
        nightly = None
    logged: list[Decision] = []  # yesterday's decisions, for the agent's nightly hook
    if nightly is not None:
        nightly([], _by_customer(history), start)
    groups: dict[str, Group] = {}
    true_value = dict.fromkeys(Group, 0.0)
    actions = dict.fromkeys(Group, 0)
    daily: list[int] = []
    briefs_t, content_t = tuple(briefs), tuple(content)

    for day in range(spec.days):
        now = (start + timedelta(days=day)).replace(hour=decide_hour, minute=0, second=0, microsecond=0)
        now = max(now, world.now())
        new = world.advance(now)
        _fold(new, states, folder)
        ledger.observe(new)
        if nightly is not None:
            nightly(logged, _by_customer(new), now)
            logged = []
        if learn:
            rewards, rewarded = ledger.close_windows(now)
            if rewards:
                agent.learn(rewards, rewarded)
        ctx = DecisionContext(now=now, briefs=briefs_t, content=content_t, seed=seed * 100_003 + day)
        ready: list[tuple[str, Group, CustomerState]] = []
        for cid, state in list(states.items()):
            g = groups.get(cid)
            if g is None:
                g = assign_group(cid, holdout_share=holdout_share, baseline_share=baseline_share, salt=salt)
                groups[cid] = g
            if g is Group.HOLDOUT and nightly is None:
                continue
            s = state
            if refresh_all or _contacts_stale(s, now):
                s = folder.refresh(s, now)
            if s.group is not g:
                s = s.model_copy(update={"group": g})
            states[cid] = s
            ready.append((cid, g, s))
        for policy, group in ((agent, Group.AGENT), (baseline, Group.BASELINE)):
            prepare = getattr(policy, "prepare_day", None)
            if callable(prepare):
                prepare([s for _, g, s in ready if g is group], ctx)
        todays: list[Decision] = []
        for _cid, g, s in ready:
            if g is Group.HOLDOUT:
                # The agent logs a "do nothing" for its holdout, as the runtime does: those are the
                # control outcomes it learns uplift from. Nothing a holdout decision says is acted on.
                held = agent.decide(s, ctx)
                if held.kind is DecisionKind.NOTHING:
                    logged.append(held if held.group is g else held.model_copy(update={"group": g}))
                continue
            d = (agent if g is Group.AGENT else baseline).decide(s, ctx)
            if d.kind is not DecisionKind.ACT or d.action is None:
                continue
            if d.group is not g:
                d = d.model_copy(update={"group": g})
            todays.append(d)
        values = true_values(world.truth, todays, now)
        agent_today = 0
        for d, v in zip(todays, values, strict=True):
            actions[d.group] += 1
            true_value[d.group] += v
            if d.group is Group.AGENT:
                agent_today += 1
                if learn:
                    ledger.pending.append(d)
        daily.append(agent_today)
        world.act(todays)
        if nightly is not None:
            logged.extend(todays)

    # The last day's sends may go out up to a day after the decision, and their effect lasts
    # `effect_days` from then, so watch one day longer than days + effect_days: otherwise the tail of
    # the true value the last sends are credited with would never show up in the realised figure.
    ledger.observe(world.advance(start + timedelta(days=spec.days + spec.effect_days + 1)))
    for cid in states:
        if cid not in groups:
            groups[cid] = assign_group(
                cid, holdout_share=holdout_share, baseline_share=baseline_share, salt=salt
            )
    return _Run(groups, ledger, true_value, actions, daily, list(world.violations()))


def _own_violations(run: _Run, cap: int) -> list[str]:
    """Rule breaks the scoreboard can see for itself: a holdout customer contacted, or more sends in
    any 7-day window than the weekly cap. Consent and quiet hours are the world's to report."""
    out: list[str] = []
    week = timedelta(days=7)
    for cid, times in run.ledger.sends.items():
        if run.groups.get(cid) is Group.HOLDOUT:
            out.extend(f"Holdout customer {cid} was contacted at {t.isoformat()}." for t in times)
            continue
        ordered = sorted(times)
        for i, t in enumerate(ordered):
            in_week = i - bisect_right(ordered, t - week) + 1
            if in_week > cap:
                out.append(
                    f"Customer {cid} got {in_week} contacts in the 7 days to {t.isoformat()}; cap {cap}."
                )
    return out


def _breaker_batches(daily: Sequence[int], onset: int) -> int | None:
    """Daily batches from `onset` up to and including the last one in which the agent acted, when it
    then stays silent to the end of the run; None if it still acted on the final day.

    A single empty day is not a stop: send-to-all goes quiet for five days every week once the weekly
    cap is reached, and must not count as a breaker tripping.
    """
    start = max(0, onset)
    if start >= len(daily) or daily[-1] > 0:
        return None
    last = max((i for i in range(start, len(daily)) if daily[i] > 0), default=None)
    return 0 if last is None else last - start + 1


def _resolve(policy: PolicyLike, world: SimWorld) -> Policy:
    if isinstance(policy, Policy) and not isinstance(policy, type):
        return policy
    return cast(PolicyFactory, policy)(world)


def run_policy(
    spec: ScenarioSpec,
    policy: PolicyLike,
    seed: int = 0,
    briefs: Sequence[Brief] | None = None,
    content: Sequence[ContentItem] | None = None,
    *,
    policy_name: str | None = None,
    world_factory: WorldFactory | None = None,
    folder: StateFolder | None = None,
    oracle_value: float | None = None,
    with_oracle: bool = True,
    learn: bool = True,
    decide_hour: int = 10,
    interval_method: IntervalMethod = "normal",
    split_salt: str | None = None,
    refresh_all: bool = False,
    segment_oracle_value: float | None = None,
    best_fixed_rule_value: float | None = None,
) -> ScoreCard:
    """Run `policy` for `spec.days` decision days on one seed and score it.

    `policy` is a ready policy or a factory taking the world (a factory gives a fresh policy per run,
    which matters for policies that learn). Without `oracle_value`, and with `with_oracle`, the oracle
    is run on the same seed first so the card can say what share of the reachable value was captured.
    `learn` closes outcome windows each simulated night and hands the rewards to `policy.learn`.
    Each day a customer goes through `folder.refresh` only when a contact has aged out of the 7- or
    30-day count, since nothing else in `SimpleStateFolder`'s state depends on time; set
    `refresh_all` for a folder that keeps other time-dependent fields.
    `segment_oracle_value` and `best_fixed_rule_value` are those references' true values on the same
    seed (`compare` runs them); the card then carries the policy's share of the segment oracle and
    its margin over the best fixed rule. They are not run here when missing.
    """
    briefs = list(briefs) if briefs is not None else [default_brief()]
    content = list(content) if content is not None else default_content()
    make_world = world_factory or gym_world
    world = make_world(spec, briefs, content)
    agent = _resolve(policy, world)
    name = policy_name or agent.version
    salt = split_salt if split_salt is not None else f"gym-split:{spec.name}:{seed}"
    run = _simulate(
        world,
        agent,
        RuleBaseline(),
        spec=spec,
        briefs=briefs,
        content=content,
        seed=seed,
        folder=folder if folder is not None else GymFolder(),
        decide_hour=decide_hour,
        salt=salt,
        learn=learn,
        refresh_all=refresh_all,
    )

    members: dict[Group, list[str]] = {g: [] for g in Group}
    for cid, g in run.groups.items():
        members[g].append(cid)
    nets = {g: np.array([run.ledger.net.get(c, 0.0) for c in members[g]], dtype=np.float64) for g in Group}
    agents = members[Group.AGENT]
    n_agent = len(agents)
    zero = Estimate(point=0.0, low=0.0, high=0.0)
    incremental = (
        difference_interval(
            nets[Group.AGENT], nets[Group.HOLDOUT], scale=n_agent, method=interval_method, seed=seed
        )
        if n_agent and members[Group.HOLDOUT]
        else zero
    )
    lift = (
        difference_interval(
            nets[Group.AGENT], nets[Group.BASELINE], scale=n_agent, method=interval_method, seed=seed + 1
        )
        if n_agent and members[Group.BASELINE]
        else None
    )
    true_value = run.true_value[Group.AGENT]

    if isinstance(agent, OraclePolicy) and oracle_value is None:
        oracle_value = true_value
    elif oracle_value is None and with_oracle:
        oracle_value = run_policy(
            spec,
            oracle_factory,
            seed,
            briefs,
            content,
            policy_name=ORACLE,
            world_factory=make_world,
            with_oracle=False,
            learn=False,
            decide_hour=decide_hour,
            interval_method=interval_method,
            split_salt=salt,
            refresh_all=refresh_all,
        ).true_value
    share = true_value / oracle_value if oracle_value is not None and oracle_value > 1e-9 else None
    if isinstance(agent, SegmentOracle) and segment_oracle_value is None:
        segment_oracle_value = true_value
    if isinstance(agent, BestFixedRule) and best_fixed_rule_value is None:
        best_fixed_rule_value = true_value
    segment_share = (
        true_value / segment_oracle_value
        if segment_oracle_value is not None and segment_oracle_value > 1e-9
        else None
    )

    contacted = {c for c in agents if run.ledger.sends.get(c)}
    dogs = [c for c in agents if world.truth.is_sleeping_dog(c)]
    dogs_contacted = sum(1 for c in dogs if c in contacted)
    contacts = sum(len(run.ledger.sends.get(c, ())) for c in agents)
    weeks = spec.days / 7.0
    violations = run.world_violations + _own_violations(run, weekly_cap(briefs))
    planted_harm = spec.name in PLANTED_HARM
    onset = spec.drift_day if spec.drift_day is not None else 0
    return ScoreCard(
        scenario=spec.name,
        policy=name,
        seed=seed,
        days=spec.days,
        n_customers=len(run.groups),
        n_agent=n_agent,
        n_baseline=len(members[Group.BASELINE]),
        n_holdout=len(members[Group.HOLDOUT]),
        incremental_profit=incremental,
        lift_vs_baseline=lift,
        interval_method=interval_method,
        true_value=true_value,
        baseline_true_value=run.true_value[Group.BASELINE],
        interval_covers_truth=incremental.low <= true_value <= incremental.high,
        oracle_value=oracle_value,
        oracle_share=share,
        actions=run.actions[Group.AGENT],
        contacts=contacts,
        contacts_per_customer_week=contacts / n_agent / weeks if n_agent else 0.0,
        unsubscribe_rate=sum(1 for c in agents if c in run.ledger.unsubscribed) / n_agent if n_agent else 0.0,
        spend=sum(run.ledger.spend.get(c, 0.0) for c in agents),
        sleeping_dogs=len(dogs),
        sleeping_dogs_contacted=dogs_contacted,
        sleeping_dog_contact_share=dogs_contacted / len(dogs) if dogs else None,
        violations=len(violations),
        violation_examples=tuple(violations[:5]),
        planted_harm=planted_harm,
        breaker_batches=_breaker_batches(run.daily_agent_actions, onset) if planted_harm else None,
        lost_value=sum(run.ledger.lost.get(c, 0.0) for c in agents),
        segment_oracle_value=segment_oracle_value,
        share_of_segment_oracle=segment_share,
        best_fixed_rule_value=best_fixed_rule_value,
        value_vs_best_fixed_rule=(
            true_value - best_fixed_rule_value if best_fixed_rule_value is not None else None
        ),
        sleeping_dog_signal=sleeping_dog_signal(spec),
    )


# --- many runs -----------------------------------------------------------------------------------


TABLE_COLUMNS = (
    "scenario",
    "policy",
    "seed",
    "true_value",
    "oracle_share",
    "incremental_profit",
    "low",
    "high",
    "covered",
    "contacts_per_customer_week",
    "unsubscribe_rate",
    "sleeping_dog_contact_share",
    "violations",
    "share_of_segment_oracle",
    "value_vs_best_fixed_rule",
    "lost_value",
)


@dataclass(frozen=True)
class Comparison:
    """Every scorecard of a comparison, with table views for printing."""

    cards: tuple[ScoreCard, ...]

    def table(self) -> pd.DataFrame:
        """One row per policy and seed."""
        rows = [
            {
                "scenario": c.scenario,
                "policy": c.policy,
                "seed": c.seed,
                "true_value": c.true_value,
                "oracle_share": c.oracle_share,
                "incremental_profit": c.incremental_profit.point,
                "low": c.incremental_profit.low,
                "high": c.incremental_profit.high,
                "covered": c.interval_covers_truth,
                "contacts_per_customer_week": c.contacts_per_customer_week,
                "unsubscribe_rate": c.unsubscribe_rate,
                "sleeping_dog_contact_share": c.sleeping_dog_contact_share,
                "violations": c.violations,
                "share_of_segment_oracle": c.share_of_segment_oracle,
                "value_vs_best_fixed_rule": c.value_vs_best_fixed_rule,
                "lost_value": c.lost_value,
            }
            for c in self.cards
        ]
        return pd.DataFrame(rows, columns=list(TABLE_COLUMNS))

    def summary(self) -> pd.DataFrame:
        """One row per scenario and policy: means over seeds (violations summed)."""
        t = self.table()
        if t.empty:
            return t
        numeric = t.drop(columns=["seed"])
        agg: dict[str, str] = {c: "mean" for c in numeric.columns if c not in ("scenario", "policy")}
        for col in agg:
            numeric[col] = pd.to_numeric(numeric[col], errors="coerce").astype(float)
        agg["violations"] = "sum"
        return numeric.groupby(["scenario", "policy"], sort=False).agg(agg).reset_index()


def compare(
    spec: ScenarioSpec,
    policies: Mapping[str, PolicyLike],
    seeds: Sequence[int] | int,
    briefs: Sequence[Brief] | None = None,
    content: Sequence[ContentItem] | None = None,
    *,
    world_factory: WorldFactory | None = None,
    learn: bool = True,
    interval_method: IntervalMethod = "normal",
) -> Comparison:
    """Score every named policy on every seed. The oracle runs once per seed and its value is shared;
    its own card is included only if `policies` names it. When `policies` names the segment oracle
    (`SEGMENT_ORACLE`) or the best fixed rule (`BEST_FIXED`), each runs once per seed first and its
    true value goes on every other card of that seed, so each card says its share of the segment
    oracle and its margin over the best fixed rule. Pass factories, not instances, for policies that
    learn, so each run starts fresh."""
    seed_list = list(range(seeds)) if isinstance(seeds, int) else list(seeds)
    cards: list[ScoreCard] = []
    for seed in seed_list:
        oracle_card = run_policy(
            spec,
            policies.get(ORACLE, oracle_factory),
            seed,
            briefs,
            content,
            policy_name=ORACLE,
            world_factory=world_factory,
            with_oracle=False,
            learn=False,
            interval_method=interval_method,
        )
        done: dict[str, ScoreCard] = {ORACLE: oracle_card}
        for ref in (SEGMENT_ORACLE, BEST_FIXED):
            if ref in policies:
                done[ref] = run_policy(
                    spec,
                    policies[ref],
                    seed,
                    briefs,
                    content,
                    policy_name=ref,
                    world_factory=world_factory,
                    oracle_value=oracle_card.true_value,
                    learn=False,
                    interval_method=interval_method,
                )
        seg = done[SEGMENT_ORACLE].true_value if SEGMENT_ORACLE in done else None
        fixed = done[BEST_FIXED].true_value if BEST_FIXED in done else None
        for name, policy in policies.items():
            if name in done:
                cards.append(
                    _with_references(done[name], segment_oracle_value=seg, best_fixed_rule_value=fixed)
                )
                continue
            cards.append(
                run_policy(
                    spec,
                    policy,
                    seed,
                    briefs,
                    content,
                    policy_name=name,
                    world_factory=world_factory,
                    oracle_value=oracle_card.true_value,
                    learn=learn,
                    interval_method=interval_method,
                    segment_oracle_value=seg,
                    best_fixed_rule_value=fixed,
                )
            )
    return Comparison(tuple(cards))


def _with_references(
    card: ScoreCard, *, segment_oracle_value: float | None, best_fixed_rule_value: float | None
) -> ScoreCard:
    """A reference card that was run before the other references, with their values added."""
    upd: dict[str, object] = {}
    if card.segment_oracle_value is None and segment_oracle_value is not None:
        upd["segment_oracle_value"] = segment_oracle_value
        if segment_oracle_value > 1e-9:
            upd["share_of_segment_oracle"] = card.true_value / segment_oracle_value
    if card.best_fixed_rule_value is None and best_fixed_rule_value is not None:
        upd["best_fixed_rule_value"] = best_fixed_rule_value
        upd["value_vs_best_fixed_rule"] = card.true_value - best_fixed_rule_value
    return card.model_copy(update=upd) if upd else card


# --- the pass bar (plan-v2 section 8) ------------------------------------------------------------


class Check(Contract):
    """One line of the pass bar. `evaluated` is False when the cards cannot judge it (e.g. no
    planted-harm scenario was run); such a check neither passes nor fails the report."""

    name: str
    description: str
    evaluated: bool
    passed: bool
    value: float | None = None
    detail: str = ""


class PassReport(Contract):
    policy: str
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        """Every evaluated check passed, and at least one was evaluated."""
        judged = [c for c in self.checks if c.evaluated]
        return bool(judged) and all(c.passed for c in judged)

    @property
    def complete(self) -> bool:
        """Every check could be judged from the cards given."""
        return all(c.evaluated for c in self.checks)

    def lines(self) -> list[str]:
        out = []
        for c in self.checks:
            mark = "PASS" if c.passed else "FAIL"
            out.append(f"[{mark if c.evaluated else 'SKIP'}] {c.description}: {c.detail}")
        verdict = "passes" if self.passed else "does not pass"
        out.append(
            f"Overall: {self.policy} {verdict} the bar"
            + ("." if self.complete else " (some checks skipped).")
        )
        return out


def _pick_policy(cards: Sequence[ScoreCard], policy: str | None) -> str:
    if policy is not None:
        return policy
    names = sorted({c.policy for c in cards} - BASELINE_NAMES)
    if len(names) != 1:
        raise ValueError(
            f"name the policy to judge; candidates: {names or sorted({c.policy for c in cards})}"
        )
    return names[0]


def pass_criteria(
    scorecards: Sequence[ScoreCard],
    *,
    policy: str | None = None,
    everyone: str = EVERYONE,
    rule: str = RULE,
    capture_target: float = 0.80,
    beat_share: float = 0.95,
    sleeping_dog_max: float = 0.10,
    coverage_band: tuple[float, float] = (0.93, 0.97),
    breaker_max_batches: int = 2,
    null_tolerance_per_customer: float = 0.01,
) -> PassReport:
    """Judge one policy against plan-v2 section 8, from the cards of a comparison.

    * **Oracle capture**: per scenario, pooled over seeds, sum(true value) / sum(oracle value) >= 80%.
      Where the oracle finds (almost) nothing to gain (`null_effect`), the policy passes if it loses
      at most `null_tolerance_per_customer` per agent-group customer instead.
    * **Beats the baselines**: on at least 95% of (scenario, seed) pairs the policy's true value is
      above both send-to-all's and the rule's, on the same customers.
    * **Sleeping dogs**: under 10% of planted sleeping dogs in the agent group contacted, pooled.
    * **Rule breaks**: zero, summed over all the policy's cards.
    * **Interval coverage**: over the policy's cards, the share whose 95% interval covers its true
      value. (Cards of other policies on the same seed share its organic purchases, so they are not
      independent evidence and are left out.) It passes when the Wilson 95% interval of that share overlaps 93-97%,
      so a handful of seeds is judged fairly and hundreds of seeds are held close to the band.
    * **Breaker**: in planted-harm scenarios, the agent stops within 2 daily batches.
    """
    name = _pick_policy(scorecards, policy)
    mine = [c for c in scorecards if c.policy == name]
    if not mine:
        raise ValueError(f"no scorecards for policy {name!r}")
    checks: list[Check] = []

    # 1. oracle capture
    by_scenario: dict[str, list[ScoreCard]] = {}
    for c in mine:
        if c.oracle_value is not None:
            by_scenario.setdefault(c.scenario, []).append(c)
    shares: list[float] = []
    parts: list[str] = []
    ok = True
    for scen, cs in sorted(by_scenario.items()):
        oracle_sum = sum(c.oracle_value or 0.0 for c in cs)
        true_sum = sum(c.true_value for c in cs)
        tol = null_tolerance_per_customer * sum(c.n_agent for c in cs)
        if oracle_sum <= tol:
            passed = true_sum >= -tol
            parts.append(f"{scen}: nothing to gain, policy value {true_sum:,.2f}")
        else:
            s = true_sum / oracle_sum
            shares.append(s)
            passed = s >= capture_target
            parts.append(f"{scen}: {s:.0%}")
        ok = ok and passed
    checks.append(
        Check(
            name="oracle_capture",
            description=f"Captures >= {capture_target:.0%} of the oracle's value",
            evaluated=bool(by_scenario),
            passed=bool(by_scenario) and ok,
            value=min(shares) if shares else None,
            detail="; ".join(parts) or "no oracle values on the cards",
        )
    )

    # 2. beats send-to-all and the rule baseline
    index = {(c.policy, c.scenario, c.seed): c for c in scorecards}
    wins = pairs = 0
    for c in mine:
        ev, ru = index.get((everyone, c.scenario, c.seed)), index.get((rule, c.scenario, c.seed))
        if ev is None or ru is None:
            continue
        pairs += 1
        wins += int(c.true_value > ev.true_value and c.true_value > ru.true_value)
    win_share = wins / pairs if pairs else None
    checks.append(
        Check(
            name="beats_baselines",
            description=f"Beats send-to-all and the rule baseline in >= {beat_share:.0%} of seeds",
            evaluated=pairs > 0,
            passed=win_share is not None and win_share >= beat_share,
            value=win_share,
            detail=f"won {wins} of {pairs} seeds" if pairs else "no baseline cards on the same seeds",
        )
    )

    # 3. sleeping dogs
    dogs = sum(c.sleeping_dogs for c in mine)
    bitten = sum(c.sleeping_dogs_contacted for c in mine)
    dog_share = bitten / dogs if dogs else None
    checks.append(
        Check(
            name="sleeping_dogs",
            description=f"Contacts < {sleeping_dog_max:.0%} of planted sleeping dogs",
            evaluated=dogs > 0,
            passed=dog_share is not None and dog_share < sleeping_dog_max,
            value=dog_share,
            detail=f"contacted {bitten} of {dogs}" if dogs else "no sleeping dogs planted",
        )
    )

    # 4. rule breaks
    broken = sum(c.violations for c in mine)
    example = next((e for c in mine for e in c.violation_examples), "")
    checks.append(
        Check(
            name="violations",
            description="Zero consent, quiet-hour or cap violations",
            evaluated=True,
            passed=broken == 0,
            value=float(broken),
            detail=f"{broken} violations" + (f", e.g. {example}" if example else ""),
        )
    )

    # 5. interval coverage
    covered = sum(int(c.interval_covers_truth) for c in mine)
    n = len(mine)
    lo, hi = wilson_interval(covered, n)
    checks.append(
        Check(
            name="interval_coverage",
            description=f"95% intervals cover the truth {coverage_band[0]:.0%}-{coverage_band[1]:.0%} of the time",
            evaluated=n > 0,
            passed=n > 0 and lo <= coverage_band[1] and hi >= coverage_band[0],
            value=covered / n if n else None,
            detail=f"{covered} of {n} intervals covered the truth (Wilson range {lo:.0%}-{hi:.0%})",
        )
    )

    # 6. breaker
    harm = [c for c in mine if c.planted_harm]
    tripped = [c for c in harm if c.breaker_batches is not None and c.breaker_batches <= breaker_max_batches]
    worst = max((c.breaker_batches for c in harm if c.breaker_batches is not None), default=None)
    checks.append(
        Check(
            name="breaker",
            description=f"The breaker trips within {breaker_max_batches} batches in planted-harm scenarios",
            evaluated=bool(harm),
            passed=bool(harm) and len(tripped) == len(harm),
            value=float(worst) if worst is not None else None,
            detail=(
                f"stopped in time in {len(tripped)} of {len(harm)} runs"
                if harm
                else "no planted-harm scenario run"
            ),
        )
    )
    return PassReport(policy=name, checks=tuple(checks))


# --- the staged bar (docs/v2/GYM_BAR.md) -------------------------------------------------------

STAGED_BEAT_SHARE = 0.90
"""Stage 2: share of seeds, in scenarios where messages can make money, in which the policy must
beat every truth-blind reference. 90% rather than 95%: with the 2-3 seeds a run affords, 95% and
100% are the same demand, and a single unlucky seed should not decide whether a policy is useful."""
NULL_LOSS_PER_CUSTOMER = 0.25
"""Stage 2: what a policy may lose per agent-group customer over 90 days where nothing works
(scaled to the run's days). A learner has to try messages to find out they do nothing; one round
of the dearest offer ($6 a redemption, redeemed by the ~4% of customers who buy in a week anyway)
costs about $0.24 a customer. Send-to-all loses about $7 a customer here."""
NOTHING_TO_GAIN_PER_CUSTOMER = 0.01
"""A scenario where the oracle gains no more than this per agent customer has nothing to gain."""
SEGMENT_CAPTURE_TARGET = 0.80
"""Stage 3: share of the segment oracle's true value, per scenario where messages make money."""
SLEEPING_DOG_MARGIN = 0.10
"""Stage 3: the policy must contact a share of sleeping dogs at least this far (absolute) below
send-to-all's, where the scenario makes them observable. With ~100+ dogs per run the standard error
of a share is ~3-5 points, so 10 points is a difference, not noise."""

ScenarioKind = Literal["harm", "null", "money"]


class Stage(Contract):
    """One stage of the staged bar: a question and the checks that answer it."""

    number: int
    name: str
    question: str
    checks: tuple[Check, ...]

    @property
    def evaluated(self) -> bool:
        return any(c.evaluated for c in self.checks)

    @property
    def passed(self) -> bool:
        """Every evaluated check passed, and at least one was evaluated."""
        judged = [c for c in self.checks if c.evaluated]
        return bool(judged) and all(c.passed for c in judged)

    @property
    def status(self) -> str:
        if not self.evaluated:
            return "NOT JUDGED"
        return "PASS" if self.passed else "FAIL"


class StagedReport(Contract):
    """The staged verdict for one policy: every stage is reported, passed or not."""

    policy: str
    stages: tuple[Stage, ...]
    stretch: tuple[Check, ...] = ()

    @property
    def stage_reached(self) -> int:
        """The highest stage passed with every stage before it passed too; 0 if Stage 1 fails or
        nothing could be judged. A stage the cards cannot judge ends the climb like a failure: a
        policy whose usefulness was never measured must not be called smart."""
        reached = 0
        for st in self.stages:
            if not st.passed:
                break
            reached = st.number
        return reached

    def verdict(self) -> dict[str, object]:
        """A small, JSON-ready summary for CI artefacts."""
        names = {st.number: st.name for st in self.stages}
        return {
            "policy": self.policy,
            "stage_reached": self.stage_reached,
            "stage_name": names.get(self.stage_reached),
            "stages": {st.name: st.status for st in self.stages},
            "stretch": {
                c.name: ("PASS" if c.passed else "FAIL") if c.evaluated else "NOT JUDGED"
                for c in self.stretch
            },
        }

    def lines(self) -> list[str]:
        out: list[str] = []
        for st in self.stages:
            out.append(f"Stage {st.number} {st.name} ({st.question}): {st.status}")
            out.extend("  " + _check_line(c) for c in st.checks)
        if self.stretch:
            out.append("Stretch (reported, not a gate):")
            out.extend("  " + _check_line(c) for c in self.stretch)
        reached = self.stage_reached
        skipped = [st.name for st in self.stages if not st.evaluated]
        name = next((st.name for st in self.stages if st.number == reached), None)
        head = (
            f"Verdict: {self.policy} reaches Stage {reached} ({name}) of {len(self.stages)}"
            if name is not None
            else f"Verdict: {self.policy} does not reach Stage 1"
        )
        out.append(head + (f"; not judged on these cards: {', '.join(skipped)}." if skipped else "."))
        return out


def _check_line(c: Check) -> str:
    mark = ("PASS" if c.passed else "FAIL") if c.evaluated else "SKIP"
    return f"[{mark}] {c.description}: {c.detail}"


def scenario_kinds(
    cards: Sequence[ScoreCard], *, nothing_per_customer: float = NOTHING_TO_GAIN_PER_CUSTOMER
) -> dict[str, ScenarioKind]:
    """Each scenario's kind, from the cards: `harm` when planted harm is flagged, `null` when the
    oracle finds (almost) nothing to gain, else `money` (also when no oracle value is known)."""
    by: dict[str, list[ScoreCard]] = {}
    for c in cards:
        by.setdefault(c.scenario, []).append(c)
    out: dict[str, ScenarioKind] = {}
    for scen, cs in by.items():
        if any(c.planted_harm for c in cs):
            out[scen] = "harm"
            continue
        with_oracle = [c for c in cs if c.oracle_value is not None]
        tol = nothing_per_customer * sum(c.n_agent for c in with_oracle)
        if with_oracle and sum(c.oracle_value or 0.0 for c in with_oracle) <= tol:
            out[scen] = "null"
        else:
            out[scen] = "money"
    return out


def _ref(
    card: ScoreCard, own: float | None, ref: str, index: Mapping[tuple[str, str, int], ScoreCard]
) -> float | None:
    """A reference value for a card: the value it carries, else the reference's own card that seed."""
    if own is not None:
        return own
    other = index.get((ref, card.scenario, card.seed))
    return other.true_value if other is not None else None


def _by_scenario(cards: Iterable[ScoreCard]) -> dict[str, list[ScoreCard]]:
    out: dict[str, list[ScoreCard]] = {}
    for c in cards:
        out.setdefault(c.scenario, []).append(c)
    return dict(sorted(out.items()))


def staged_criteria(
    scorecards: Sequence[ScoreCard],
    *,
    policy: str | None = None,
    everyone: str = EVERYONE,
    rule: str = RULE,
    best_fixed: str = BEST_FIXED,
    segment_oracle: str = SEGMENT_ORACLE,
    beat_share: float = STAGED_BEAT_SHARE,
    null_loss_per_customer: float = NULL_LOSS_PER_CUSTOMER,
    segment_target: float = SEGMENT_CAPTURE_TARGET,
    sleeping_dog_margin: float = SLEEPING_DOG_MARGIN,
    oracle_target: float = 0.80,
    plan_sleeping_dog_max: float = 0.10,
    coverage_band: tuple[float, float] = (0.93, 0.97),
    breaker_max_batches: int = 2,
) -> StagedReport:
    """Judge one policy against the staged bar of docs/v2/GYM_BAR.md, from a comparison's cards.

    * **Stage 1 Safe**: zero rule breaks; the breaker stops within 2 batches in planted-harm
      scenarios; 95% intervals cover the truth (Wilson range of the share overlaps 93-97%).
    * **Stage 2 Useful**: where messages can make money, beats send-to-all, the rule baseline and the
      best fixed rule on true value in >= 90% of seeds; where nothing works, loses at most
      `null_loss_per_customer` per agent customer per 90 days; in planted-harm scenarios, ends with
      more realised profit than send-to-all once lost future value is counted.
    * **Stage 3 Smart**: >= 80% of the segment oracle's true value per scenario where messages make
      money; where sleeping dogs are observable (the scenario's signal is above zero), contacts a
      share of them at least `sleeping_dog_margin` below send-to-all's.
    * **Stretch** (reported only): >= 80% of the full oracle, and the plan's < 10% of sleeping dogs.

    Planted-harm scenarios are judged on Stage 1 and the harm check of Stage 2 only: stopping is the
    right answer there, and it cannot also capture value. A check the cards cannot judge is skipped;
    a stage with no judged check is "not judged" and neither passes nor fails.
    """
    name = _pick_policy(scorecards, policy)
    mine = [c for c in scorecards if c.policy == name]
    if not mine:
        raise ValueError(f"no scorecards for policy {name!r}")
    index = {(c.policy, c.scenario, c.seed): c for c in scorecards}
    kinds = scenario_kinds(mine)
    money = [c for c in mine if kinds[c.scenario] == "money"]
    null = [c for c in mine if kinds[c.scenario] == "null"]
    harm = [c for c in mine if kinds[c.scenario] == "harm"]

    # --- Stage 1: Safe
    broken = sum(c.violations for c in mine)
    example = next((e for c in mine for e in c.violation_examples), "")
    rules = Check(
        name="rule_breaks",
        description="Zero consent, unsubscribe, quiet-hour, cap or holdout breaks",
        evaluated=True,
        passed=broken == 0,
        value=float(broken),
        detail=f"{broken} rule breaks over {len(mine)} runs" + (f", e.g. {example}" if example else ""),
    )
    tripped = [c for c in harm if c.breaker_batches is not None and c.breaker_batches <= breaker_max_batches]
    worst = max((c.breaker_batches for c in harm if c.breaker_batches is not None), default=None)
    breaker = Check(
        name="breaker",
        description=f"Stops within {breaker_max_batches} daily batches in planted-harm scenarios",
        evaluated=bool(harm),
        passed=bool(harm) and len(tripped) == len(harm),
        value=float(worst) if worst is not None else None,
        detail=(
            f"stopped in time in {len(tripped)} of {len(harm)} runs"
            + (f" (latest after {worst} batches)" if worst is not None else "")
            if harm
            else "no planted-harm scenario run"
        ),
    )
    covered = sum(int(c.interval_covers_truth) for c in mine)
    n = len(mine)
    lo, hi = wilson_interval(covered, n)
    coverage = Check(
        name="interval_coverage",
        description=f"95% intervals cover the truth {coverage_band[0]:.0%}-{coverage_band[1]:.0%} of the time",
        evaluated=n > 0,
        passed=n > 0 and lo <= coverage_band[1] and hi >= coverage_band[0],
        value=covered / n if n else None,
        detail=f"{covered} of {n} intervals covered the truth (Wilson range {lo:.0%}-{hi:.0%})",
    )

    # --- Stage 2: Useful
    pairs = wins = 0
    beat = {everyone: 0, rule: 0, best_fixed: 0}
    for c in money:
        refs = {r: index.get((r, c.scenario, c.seed)) for r in beat}
        if any(v is None for v in refs.values()):
            continue
        pairs += 1
        for r, other in refs.items():
            beat[r] += int(other is not None and c.true_value > other.true_value)
        wins += int(all(other is not None and c.true_value > other.true_value for other in refs.values()))
    win_share = wins / pairs if pairs else None
    beats = Check(
        name="beats_references",
        description=(
            f"Beats send-to-all, the rule baseline and the best fixed rule on true value in "
            f">= {beat_share:.0%} of seeds where messages can make money"
        ),
        evaluated=pairs > 0,
        passed=win_share is not None and win_share >= beat_share,
        value=win_share,
        detail=(
            f"beat all three in {wins} of {pairs} seeds (send-to-all {beat[everyone]}, "
            f"rule {beat[rule]}, best fixed rule {beat[best_fixed]})"
            if pairs
            else "no money-making scenario with all three reference cards on the same seeds"
        ),
    )
    null_parts: list[str] = []
    null_ok = True
    worst_loss: float | None = None
    for scen, cs in _by_scenario(null).items():
        allowed = null_loss_per_customer * sum(c.n_agent * c.days / 90.0 for c in cs)
        total = sum(c.true_value for c in cs)
        per = total / max(1, sum(c.n_agent for c in cs))
        worst_loss = per if worst_loss is None else min(worst_loss, per)
        null_ok = null_ok and total >= -allowed
        null_parts.append(
            f"{scen}: {_money(total)} over {len(cs)} seeds ({_money(per, 2)} a customer; "
            f"allowed loss {_money(allowed)})"
        )
    null_check = Check(
        name="null_loss",
        description=(
            f"Where nothing works, loses at most ${null_loss_per_customer:.2f} per customer per 90 days"
        ),
        evaluated=bool(null),
        passed=bool(null) and null_ok,
        value=worst_loss,
        detail="; ".join(null_parts) or "no scenario where nothing works was run",
    )
    harm_pairs = harm_wins = 0
    lost_mine = lost_everyone = 0.0
    for c in harm:
        ev = index.get((everyone, c.scenario, c.seed))
        if ev is None:
            continue
        harm_pairs += 1
        harm_wins += int(c.incremental_profit.point > ev.incremental_profit.point)
        lost_mine += c.lost_value
        lost_everyone += ev.lost_value
    harm_share = harm_wins / harm_pairs if harm_pairs else None
    harm_detail = (
        f"more realised profit than send-to-all in {harm_wins} of {harm_pairs} seeds; lost value "
        f"{_money(lost_mine)} against send-to-all's {_money(lost_everyone)}"
        + ("" if lost_mine or lost_everyone else " (the world reported no lost value)")
        if harm_pairs
        else "no planted-harm scenario with send-to-all on the same seeds"
    )
    harm_check = Check(
        name="harm_loses_less",
        description="In planted-harm scenarios, loses less than send-to-all once lost future value is counted",
        evaluated=harm_pairs > 0,
        passed=harm_share is not None and harm_share >= beat_share,
        value=harm_share,
        detail=harm_detail,
    )

    # --- Stage 3: Smart
    seg_parts: list[str] = []
    seg_shares: list[float] = []
    seg_ok = True
    seg_judged = False
    for scen, cs in _by_scenario(money).items():
        valued = [(c, _ref(c, c.segment_oracle_value, segment_oracle, index)) for c in cs]
        judged = [(c, v) for c, v in valued if v is not None]
        if not judged:
            continue
        seg_judged = True
        seg_sum = sum(v for _, v in judged if v is not None)
        true_sum = sum(c.true_value for c, _ in judged)
        tol = NOTHING_TO_GAIN_PER_CUSTOMER * sum(c.n_agent for c, _ in judged)
        if seg_sum <= tol:
            ok = true_sum >= -tol
            seg_parts.append(f"{scen}: the segment oracle finds nothing; policy {_money(true_sum)}")
        else:
            share = true_sum / seg_sum
            seg_shares.append(share)
            ok = share >= segment_target
            # For context: how much of the segment oracle needs no learning at all.
            fixed = [_ref(c, c.best_fixed_rule_value, best_fixed, index) for c, _ in judged]
            known = [v for v in fixed if v is not None]
            context = f"; best fixed rule {sum(known) / seg_sum:.0%}" if len(known) == len(fixed) else ""
            seg_parts.append(f"{scen}: {share:.0%} ({_money(true_sum)} of {_money(seg_sum)}{context})")
        seg_ok = seg_ok and ok
    seg_check = Check(
        name="segment_oracle_capture",
        description=f"Captures >= {segment_target:.0%} of the segment oracle's value where messages make money",
        evaluated=seg_judged,
        passed=seg_judged and seg_ok,
        value=min(seg_shares) if seg_shares else None,
        detail="; ".join(seg_parts) or "no segment oracle values on the cards",
    )
    signalled = [
        c for c in mine if kinds[c.scenario] != "harm" and c.sleeping_dog_signal > 0 and c.sleeping_dogs
    ]
    dogs_mine = dogs_hit = dogs_ev = dogs_ev_hit = 0
    for c in signalled:
        ev = index.get((everyone, c.scenario, c.seed))
        if ev is None or not ev.sleeping_dogs:
            continue
        dogs_mine += c.sleeping_dogs
        dogs_hit += c.sleeping_dogs_contacted
        dogs_ev += ev.sleeping_dogs
        dogs_ev_hit += ev.sleeping_dogs_contacted
    if dogs_mine and dogs_ev:
        share_mine, share_ev = dogs_hit / dogs_mine, dogs_ev_hit / dogs_ev
        dog_check = Check(
            name="sleeping_dogs",
            description=(
                f"Contacts at least {sleeping_dog_margin:.0%} fewer of the planted sleeping dogs than "
                "send-to-all where they are observable"
            ),
            evaluated=True,
            passed=share_mine <= share_ev - sleeping_dog_margin,
            value=share_mine,
            detail=f"contacted {dogs_hit} of {dogs_mine} ({share_mine:.0%}); send-to-all {share_ev:.0%}",
        )
    else:
        any_dogs = any(c.sleeping_dogs for c in mine)
        dog_check = Check(
            name="sleeping_dogs",
            description=(
                f"Contacts at least {sleeping_dog_margin:.0%} fewer of the planted sleeping dogs than "
                "send-to-all where they are observable"
            ),
            evaluated=False,
            passed=False,
            detail=(
                "sleeping dogs are planted with no observable signal, so no truth-blind policy can "
                "avoid them before contact (GYM_RESULTS.md); not judged"
                if any_dogs and not signalled
                else ("no send-to-all cards on the same seeds" if signalled else "no sleeping dogs planted")
            ),
        )

    # --- Stretch
    full_parts: list[str] = []
    full_shares: list[float] = []
    for scen, cs in _by_scenario(money).items():
        valued = [(c, c.oracle_value) for c in cs if c.oracle_value is not None]
        o_sum = sum(v for _, v in valued if v is not None)
        if valued and o_sum > 1e-9:
            share = sum(c.true_value for c, _ in valued) / o_sum
            full_shares.append(share)
            full_parts.append(f"{scen}: {share:.0%}")
    oracle_check = Check(
        name="oracle_capture",
        description=f"Captures >= {oracle_target:.0%} of the full oracle's value (plan-v2's bar)",
        evaluated=bool(full_shares),
        passed=bool(full_shares) and min(full_shares) >= oracle_target,
        value=min(full_shares) if full_shares else None,
        detail="; ".join(full_parts) or "no oracle values on money-making scenarios",
    )
    all_dogs = sum(c.sleeping_dogs for c in mine if kinds[c.scenario] != "harm")
    all_hit = sum(c.sleeping_dogs_contacted for c in mine if kinds[c.scenario] != "harm")
    plan_dogs = Check(
        name="plan_sleeping_dogs",
        description=f"Contacts < {plan_sleeping_dog_max:.0%} of planted sleeping dogs (plan-v2's bar)",
        evaluated=all_dogs > 0,
        passed=all_dogs > 0 and all_hit / all_dogs < plan_sleeping_dog_max,
        value=all_hit / all_dogs if all_dogs else None,
        detail=f"contacted {all_hit} of {all_dogs}" if all_dogs else "no sleeping dogs planted",
    )

    return StagedReport(
        policy=name,
        stages=(
            Stage(number=1, name="Safe", question="does it do no harm", checks=(rules, breaker, coverage)),
            Stage(
                number=2,
                name="Useful",
                question="is it worth switching on",
                checks=(beats, null_check, harm_check),
            ),
            Stage(
                number=3,
                name="Smart",
                question="does it learn what a business can know",
                checks=(seg_check, dog_check),
            ),
        ),
        stretch=(oracle_check, plan_dogs),
    )


def _money(x: float, digits: int = 0) -> str:
    body = f"${abs(x):,.{digits}f}"
    return f"-{body}" if x < 0 else body
