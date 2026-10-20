"""The seams between streams. Each stream implements its own protocols and codes against the rest.

Every protocol has an in-memory version in `autonomy.fakes`, so any stream can run and test alone.
Time is always passed in (`now`) rather than read from the clock, so the Gym can run 90 days in
seconds and tests repeat exactly.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from autonomy.contracts import (
    Bid,
    Brief,
    Channel,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    Event,
    Goal,
    Job,
    Message,
    Prior,
    Reward,
    SendReceipt,
)

# --- WF1: backbone ----------------------------------------------------------------------------


@runtime_checkable
class EventStore(Protocol):
    def append(self, events: Iterable[Event]) -> int:
        """Store events; return how many were new. A repeated `event_id` is ignored."""
        ...

    def for_customer(
        self, customer_id: str, *, since: datetime | None = None, until: datetime | None = None
    ) -> list[Event]:
        """One customer's events in time order."""
        ...

    def scan(self, *, since: datetime | None = None, until: datetime | None = None) -> Iterator[Event]:
        """All events in time order, streamed."""
        ...

    def for_decision(self, decision_id: str) -> list[Event]: ...


@runtime_checkable
class StateStore(Protocol):
    def get(self, customer_id: str) -> CustomerState | None: ...

    def put(self, states: Iterable[CustomerState]) -> None: ...

    def scan(self) -> Iterator[CustomerState]: ...

    def count(self) -> int: ...


@runtime_checkable
class StateFolder(Protocol):
    """Pure: the state after one more event. `fold(None, e)` starts a new customer."""

    def fold(self, state: CustomerState | None, event: Event) -> CustomerState: ...

    def refresh(self, state: CustomerState, now: datetime) -> CustomerState:
        """Recompute time-dependent fields (contacts_7d, ...) without a new event."""
        ...


@runtime_checkable
class DecisionLog(Protocol):
    def record(self, decisions: Iterable[Decision]) -> None: ...

    def get(self, decision_id: str) -> Decision | None: ...

    def scan(self, *, since: datetime | None = None, until: datetime | None = None) -> Iterator[Decision]: ...

    def for_customer(self, customer_id: str) -> list[Decision]: ...


@runtime_checkable
class Workspace(Protocol):
    """Goals, briefs and the content library: what the agents read and write."""

    def put_goal(self, goal: Goal) -> None: ...

    def goals(self) -> list[Goal]: ...

    def put_brief(self, brief: Brief) -> None: ...

    def briefs(self, goal_id: str | None = None) -> list[Brief]:
        """Latest version per goal."""
        ...

    def put_content(self, items: Iterable[ContentItem]) -> None: ...

    def content(self, *, goal_id: str | None = None, approved_only: bool = True) -> list[ContentItem]: ...


@runtime_checkable
class JobQueue(Protocol):
    def enqueue(self, job: Job) -> None: ...

    def claim(self, kinds: Sequence[str], *, worker: str, now: datetime) -> Job | None:
        """The next due job of these kinds, marked running; None if nothing is due."""
        ...

    def complete(self, job_id: str) -> None: ...

    def fail(self, job_id: str, error: str, *, retry_at: datetime | None = None) -> None: ...


@runtime_checkable
class PriorStore(Protocol):
    def get(self, key: str) -> Prior | None: ...

    def put(self, prior: Prior) -> None: ...

    def keys(self, prefix: str = "") -> list[str]: ...


# --- WF3: the inner loop --------------------------------------------------------------------------


@runtime_checkable
class Bidder(Protocol):
    """One goal's maths: what acting is worth for this customer. No LLM in here."""

    goal_id: str

    def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]: ...


@runtime_checkable
class Policy(Protocol):
    version: str

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision: ...

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        """Update from closed outcomes (the nightly fast loop)."""
        ...


# --- WF2: the Gym, and the real world through the same door ---------------------------------------


@runtime_checkable
class Environment(Protocol):
    """Where decisions land and events come from: the Gym, or the dispatcher plus webhooks."""

    def reset(self, seed: int) -> Sequence[Event]:
        """Start over; return the history before the first decision day."""
        ...

    def now(self) -> datetime: ...

    def act(self, decisions: Sequence[Decision]) -> None:
        """Carry out ACT decisions (at their send_at). NOTHING decisions are ignored."""
        ...

    def advance(self, until: datetime) -> Sequence[Event]:
        """Move time forward; return every event that happened, in time order."""
        ...


# --- WF5: channels -------------------------------------------------------------------------------


@runtime_checkable
class ChannelAdapter(Protocol):
    channel: Channel

    def send(self, message: Message) -> SendReceipt: ...

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool: ...

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[Event]:
        """Provider callbacks as our events, each with the decision_id it belongs to."""
        ...


# --- WF4: text -> signals ------------------------------------------------------------------------


@runtime_checkable
class TextSignals(Protocol):
    """Typed answers about text: reply intent, sentiment, a message's theme, ..."""

    def classify(
        self, texts: Sequence[str], *, question: str, labels: Sequence[str]
    ) -> list[tuple[str, float]]:
        """For each text, the chosen label and its probability."""
        ...
