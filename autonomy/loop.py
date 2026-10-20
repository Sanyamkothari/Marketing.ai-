"""The reference loop: run a policy against an environment day by day.

This is deliberately small. It is the contract for how the pieces meet: events are folded into
state, the policy decides once a day for every customer in the agent group, decisions are logged
and acted on, and time moves on. WF2 (the Gym) and WF3 (decide) both use it; Wave 2 grows it into
the real runtime (event-triggered decisions, outcome windows, nightly learning).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from autonomy.contracts import ContentItem, CustomerState, Decision, DecisionContext, Event
from autonomy.contracts.goals import Brief
from autonomy.interfaces import DecisionLog, Environment, EventStore, Policy, StateFolder, StateStore


@dataclass
class LoopResult:
    days: int
    decisions: list[Decision] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)


def fold_into(events: Iterable[Event], states: StateStore, folder: StateFolder) -> None:
    cache: dict[str, CustomerState | None] = {}
    for e in events:
        if e.customer_id not in cache:
            cache[e.customer_id] = states.get(e.customer_id)
        cache[e.customer_id] = folder.fold(cache[e.customer_id], e)
    states.put(s for s in cache.values() if s is not None)


def run_days(
    *,
    env: Environment,
    policy: Policy,
    events: EventStore,
    states: StateStore,
    decisions: DecisionLog,
    folder: StateFolder,
    briefs: Sequence[Brief],
    content: Sequence[ContentItem],
    days: int,
    seed: int = 0,
    decide_hour: int = 10,
    assign_group: Callable[[CustomerState], CustomerState] | None = None,
) -> LoopResult:
    """Reset the environment, then decide once a day at `decide_hour` (UTC) for `days` days."""
    history = env.reset(seed)
    events.append(history)
    fold_into(history, states, folder)
    result = LoopResult(days=days, events=list(history))
    start = env.now()
    for day in range(days):
        now = (start + timedelta(days=day)).replace(hour=decide_hour, minute=0, second=0, microsecond=0)
        if now < env.now():
            now = env.now()
        new = env.advance(now)
        events.append(new)
        fold_into(new, states, folder)
        result.events.extend(new)
        ctx = DecisionContext(
            now=now, briefs=tuple(briefs), content=tuple(content), seed=seed * 100_003 + day
        )
        todays: list[Decision] = []
        for s in list(states.scan()):
            s = folder.refresh(s, now)
            if assign_group is not None:
                s = assign_group(s)
            states.put([s])
            todays.append(policy.decide(s, ctx))
        decisions.record(todays)
        env.act(todays)
        result.decisions.extend(todays)
    tail = env.advance(start + timedelta(days=days))
    events.append(tail)
    fold_into(tail, states, folder)
    result.events.extend(tail)
    return result
