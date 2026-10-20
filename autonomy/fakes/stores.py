"""In-memory stores. Good enough for tests and the Gym; WF1 provides the Postgres versions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime

from autonomy.contracts import (
    Brief,
    ContentItem,
    ContentStatus,
    CustomerState,
    Decision,
    Event,
    Goal,
    Job,
    JobStatus,
    Prior,
)


def _in_window(ts: datetime, since: datetime | None, until: datetime | None) -> bool:
    return (since is None or ts >= since) and (until is None or ts < until)


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._ids: set[str] = set()
        self._by_customer: dict[str, list[Event]] = defaultdict(list)
        self._by_decision: dict[str, list[Event]] = defaultdict(list)

    def append(self, events: Iterable[Event]) -> int:
        new = 0
        for e in events:
            if e.event_id in self._ids:
                continue
            self._ids.add(e.event_id)
            self._events.append(e)
            self._by_customer[e.customer_id].append(e)
            if e.decision_id:
                self._by_decision[e.decision_id].append(e)
            new += 1
        return new

    def for_customer(
        self, customer_id: str, *, since: datetime | None = None, until: datetime | None = None
    ) -> list[Event]:
        rows = [e for e in self._by_customer.get(customer_id, []) if _in_window(e.ts, since, until)]
        return sorted(rows, key=lambda e: e.ts)

    def scan(self, *, since: datetime | None = None, until: datetime | None = None) -> Iterator[Event]:
        yield from sorted((e for e in self._events if _in_window(e.ts, since, until)), key=lambda e: e.ts)

    def for_decision(self, decision_id: str) -> list[Event]:
        return sorted(self._by_decision.get(decision_id, []), key=lambda e: e.ts)

    def __len__(self) -> int:
        return len(self._events)


class InMemoryStateStore:
    def __init__(self) -> None:
        self._states: dict[str, CustomerState] = {}

    def get(self, customer_id: str) -> CustomerState | None:
        return self._states.get(customer_id)

    def put(self, states: Iterable[CustomerState]) -> None:
        for s in states:
            self._states[s.customer_id] = s

    def scan(self) -> Iterator[CustomerState]:
        yield from (self._states[k] for k in sorted(self._states))

    def count(self) -> int:
        return len(self._states)


class InMemoryDecisionLog:
    def __init__(self) -> None:
        self._decisions: dict[str, Decision] = {}

    def record(self, decisions: Iterable[Decision]) -> None:
        for d in decisions:
            self._decisions[d.decision_id] = d

    def get(self, decision_id: str) -> Decision | None:
        return self._decisions.get(decision_id)

    def scan(self, *, since: datetime | None = None, until: datetime | None = None) -> Iterator[Decision]:
        rows = (d for d in self._decisions.values() if _in_window(d.made_at, since, until))
        yield from sorted(rows, key=lambda d: d.made_at)

    def for_customer(self, customer_id: str) -> list[Decision]:
        return sorted(
            (d for d in self._decisions.values() if d.customer_id == customer_id), key=lambda d: d.made_at
        )


class InMemoryWorkspace:
    def __init__(self) -> None:
        self._goals: dict[str, Goal] = {}
        self._briefs: dict[str, Brief] = {}  # goal_id -> latest version
        self._content: dict[str, ContentItem] = {}

    def put_goal(self, goal: Goal) -> None:
        self._goals[goal.goal_id] = goal

    def goals(self) -> list[Goal]:
        return list(self._goals.values())

    def put_brief(self, brief: Brief) -> None:
        current = self._briefs.get(brief.goal_id)
        if current is None or brief.version >= current.version:
            self._briefs[brief.goal_id] = brief

    def briefs(self, goal_id: str | None = None) -> list[Brief]:
        return [b for g, b in self._briefs.items() if goal_id is None or g == goal_id]

    def put_content(self, items: Iterable[ContentItem]) -> None:
        for item in items:
            self._content[item.content_id] = item

    def content(self, *, goal_id: str | None = None, approved_only: bool = True) -> list[ContentItem]:
        return [
            c
            for c in self._content.values()
            if (goal_id is None or c.goal_id in (goal_id, None))
            and (not approved_only or c.status is ContentStatus.APPROVED)
        ]


class InMemoryJobQueue:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def enqueue(self, job: Job) -> None:
        self._jobs[job.job_id] = job

    def claim(self, kinds: Sequence[str], *, worker: str, now: datetime) -> Job | None:  # noqa: ARG002
        due = [
            j
            for j in self._jobs.values()
            if j.status is JobStatus.QUEUED and j.kind in kinds and j.run_at <= now
        ]
        if not due:
            return None
        job = min(due, key=lambda j: (j.run_at, j.job_id))
        claimed = job.model_copy(update={"status": JobStatus.RUNNING, "attempts": job.attempts + 1})
        self._jobs[job.job_id] = claimed
        return claimed

    def complete(self, job_id: str) -> None:
        self._jobs[job_id] = self._jobs[job_id].model_copy(update={"status": JobStatus.DONE})

    def fail(self, job_id: str, error: str, *, retry_at: datetime | None = None) -> None:
        job = self._jobs[job_id]
        if retry_at is None:
            self._jobs[job_id] = job.model_copy(update={"status": JobStatus.FAILED, "last_error": error})
        else:
            self._jobs[job_id] = job.model_copy(
                update={"status": JobStatus.QUEUED, "run_at": retry_at, "last_error": error}
            )

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)


class InMemoryPriorStore:
    def __init__(self) -> None:
        self._priors: dict[str, Prior] = {}

    def get(self, key: str) -> Prior | None:
        return self._priors.get(key)

    def put(self, prior: Prior) -> None:
        self._priors[prior.key] = prior

    def keys(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self._priors if k.startswith(prefix))
