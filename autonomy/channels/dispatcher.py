"""The dispatcher: turns ACT decisions into messages on real channels, exactly once each.

`decide()` says who to contact, on which channel, with which content and offer. The dispatcher does
the rest: it looks up the content and offer, fills the slots, finds the customer's address, and
hands a `Message` to the channel adapter. Every attempt lands in the event store (SEND, or FAIL
with the error) carrying the `decision_id`, so every result traces back to a decision.

It is also the last line of defence, so it re-checks what must never go wrong even if the policy
got it right already:

- **Exactly once per decision.** A decision that already has a SEND event is never sent again,
  even if `dispatch()` is called twice or a queued job runs late. The message id is derived from
  the decision id, so an adapter can pass it to the provider as an idempotency key too.
- **Holdout is never contacted**, whatever the decision says.
- **Consent.** A customer who has unsubscribed or withdrawn consent on the channel is skipped.
- **Journey steps (v3).** A later step of a channel journey is skipped once the customer bought
  after it was decided.
- **Goal status.** Only active goals out of shadow mode, inside their start and end dates, send.
  Paused goals and channels (by a person or a circuit breaker) send nothing.
- **Envelope.** The channel and offer must be ones the goal's brief allows.
- **Content.** Only approved content for the decision's channel and offer is sent, and a message
  that cannot be rendered safely is skipped.
- **Channel guards (v4).** A channel may add checks of its own (`ChannelGuard`): for WhatsApp, an
  approved template, a quality rating that is not RED, the weekly budget (halved on YELLOW) and
  Meta's per-user holds (`whatsapp_guard`).

Anything skipped is reported with a short reason code; nothing is skipped silently. Time is passed
in: a decision whose `send_at` is still in the future is queued as a `send_message` job when a
queue is given, and `run_send_job()` finishes it when it comes due.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from autonomy.channels.common import send_event_id
from autonomy.channels.render import RenderError, render
from autonomy.contracts import (
    NO_OFFER,
    AutonomyLevel,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    CustomerState,
    Decision,
    DecisionKind,
    Event,
    EventType,
    Goal,
    GoalStatus,
    Group,
    Job,
    Message,
    OfferOption,
    stable_id,
)
from autonomy.contracts.events import AttrValue
from autonomy.interfaces import ChannelAdapter, DecisionLog, EventStore, JobQueue, StateStore, Workspace

SEND_JOB_KIND = "send_message"
SOURCE = "dispatcher"
MAX_ERROR_LEN = 500

AddressBook = Callable[[str, Channel], str | None]
"""(customer_id, channel) -> the address to send to (email, phone, the client tool's id), or None."""

ProfileBook = Callable[[str], Mapping[str, str]]
"""customer_id -> profile fields used to fill slots, e.g. {"first_name": "Priya"}."""


class ChannelGuard(Protocol):
    """A channel's own last checks before dispatch, beyond the ones every channel gets (WhatsApp:
    approved templates, quality rating, Meta's per-user limits; see `whatsapp_guard`)."""

    def check(
        self, message: Message, weekly_cap: int, now: datetime, events: EventStore
    ) -> tuple[SkipReason, str] | None:
        """Why this message must not be sent, or None. `weekly_cap` is the strictest weekly contact
        cap of the goal's briefs."""
        ...

    def failure_attrs(self, error: str, now: datetime) -> Mapping[str, AttrValue]:
        """Extra attrs for the FAIL event of a send the provider refused with this error."""
        ...


class DispatchStatus(StrEnum):
    SENT = "sent"
    QUEUED = "queued"
    FAILED = "failed"
    SKIPPED = "skipped"


class SkipReason(StrEnum):
    """Why a decision was not sent. Stable codes: the UI and the Analyst count them."""

    ALREADY_SENT = "already_sent"
    DUPLICATE_IN_BATCH = "duplicate_in_batch"
    HOLDOUT = "holdout"
    UNKNOWN_GOAL = "unknown_goal"
    GOAL_NOT_ACTIVE = "goal_not_active"
    SHADOW_MODE = "shadow_mode"
    PAUSED_GOAL = "paused_goal"
    PAUSED_CHANNEL = "paused_channel"
    NO_ADAPTER = "no_adapter"
    NO_BRIEF = "no_brief"
    CHANNEL_NOT_ALLOWED = "channel_not_allowed"
    NO_STATE = "no_state"
    NOT_CONTACTABLE = "not_contactable"
    NO_CONTENT = "no_content"
    CONTENT_NOT_FOUND = "content_not_found"
    CONTENT_NOT_APPROVED = "content_not_approved"
    CONTENT_MISMATCH = "content_mismatch"
    OFFER_NOT_ALLOWED = "offer_not_allowed"
    RENDER_FAILED = "render_failed"
    NO_ADDRESS = "no_address"
    DECISION_NOT_FOUND = "decision_not_found"
    # v3 channel journeys
    BOUGHT_SINCE = "bought_since_decision"
    # v4 WhatsApp readiness (`ChannelGuard`)
    TEMPLATE_NOT_APPROVED = "template_not_approved"
    QUALITY_PAUSED = "quality_paused"
    QUALITY_BUDGET = "quality_budget"
    PROVIDER_HOLD = "provider_hold"


@dataclass(frozen=True)
class DispatchResult:
    decision_id: str
    customer_id: str
    status: DispatchStatus
    reason: str = ""
    """A `SkipReason` for skips, the provider's error for failures, empty otherwise."""
    detail: str = ""
    message_id: str | None = None
    provider_message_id: str | None = None
    job_id: str | None = None


@dataclass
class DispatchReport:
    """What happened to every ACT decision in one `dispatch()` call, plus the events it wrote."""

    results: list[DispatchResult] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    ignored: int = 0
    """NOTHING decisions: nothing to send, so not reported one by one."""

    def with_status(self, status: DispatchStatus) -> list[DispatchResult]:
        return [r for r in self.results if r.status is status]

    @property
    def sent(self) -> list[DispatchResult]:
        return self.with_status(DispatchStatus.SENT)

    @property
    def queued(self) -> list[DispatchResult]:
        return self.with_status(DispatchStatus.QUEUED)

    @property
    def failed(self) -> list[DispatchResult]:
        return self.with_status(DispatchStatus.FAILED)

    @property
    def skipped(self) -> list[DispatchResult]:
        return self.with_status(DispatchStatus.SKIPPED)

    def skip_reasons(self) -> dict[str, int]:
        return dict(Counter(r.reason for r in self.skipped))

    def merge(self, other: DispatchReport) -> None:
        self.results.extend(other.results)
        self.events.extend(other.events)
        self.ignored += other.ignored


def message_id_for(decision_id: str) -> str:
    """The message id of a decision's message: stable, so a retry is recognisable to the provider."""
    return stable_id("msg", decision_id)


def send_job_id(decision_id: str) -> str:
    return stable_id("job", SEND_JOB_KIND, decision_id)


class _SkipError(Exception):
    def __init__(self, reason: SkipReason, detail: str = "") -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class _Prepared:
    """Everything needed to send one decision, checked."""

    message: Message
    attrs: dict[str, AttrValue]


def _check_journey_step(decision: Decision, state: CustomerState) -> None:
    """v3 channel journeys: a journey's later step is not sent to a customer who bought after it was
    decided. The policy checks purchases at each sweep, but a step can be decided up to a day before
    it is due and timed up to a day after, so this is the last check at send time."""
    action = decision.action
    if action is None or action.journey_id is None or action.follow_up_step == 0:
        return
    bought = state.last_purchase_at
    if bought is not None and bought >= decision.made_at:
        raise _SkipError(SkipReason.BOUGHT_SINCE, f"journey {action.journey_id} stops once the customer buys")


class Dispatcher:
    """Sends ACT decisions through channel adapters; see the module docstring for the rules."""

    def __init__(
        self,
        adapters: Mapping[Channel, ChannelAdapter],
        address_book: AddressBook,
        workspace: Workspace,
        events: EventStore,
        queue: JobQueue | None = None,
        *,
        profiles: ProfileBook | None = None,
        paused_goals: Iterable[str] = (),
        paused_channels: Iterable[Channel] = (),
        guards: Mapping[Channel, ChannelGuard] | None = None,
    ) -> None:
        self.adapters = dict(adapters)
        self.guards = dict(guards or {})
        self.address_book = address_book
        self.workspace = workspace
        self.events = events
        self.queue = queue
        self.profiles = profiles
        self.paused_goals: set[str] = set(paused_goals)
        self.paused_channels: set[Channel] = set(paused_channels)

    # --- pausing ---------------------------------------------------------------------------------

    def pause(self, *, goals: Iterable[str] = (), channels: Iterable[Channel] = ()) -> None:
        """Stop sending for these goals and channels, e.g. from a circuit breaker's verdict."""
        self.paused_goals.update(goals)
        self.paused_channels.update(channels)

    def resume(self, *, goals: Iterable[str] = (), channels: Iterable[Channel] = ()) -> None:
        self.paused_goals.difference_update(goals)
        self.paused_channels.difference_update(channels)

    # --- sending ---------------------------------------------------------------------------------

    def dispatch(
        self, decisions: Sequence[Decision], states: Mapping[str, CustomerState], now: datetime
    ) -> DispatchReport:
        """Send (or queue) every ACT decision for the agent and baseline groups.

        `states` maps customer_id to the current state; a decision whose customer is missing is
        skipped. Calling this twice with the same decisions sends nothing the second time.
        """
        return self._dispatch(decisions, states, now, allow_queue=True)

    def run_send_job(
        self, job: Job, decisions: DecisionLog, states: StateStore, now: datetime
    ) -> DispatchReport:
        """Finish a queued `send_message` job: re-check everything and send now.

        The job carries only the decision id; content, consent and pauses are looked up afresh, so
        a customer who unsubscribed while the message waited is not contacted. Marking the job done
        or failed is the worker's business.
        """
        decision_id = str(job.payload.get("decision_id") or "")
        decision = decisions.get(decision_id) if decision_id else None
        if decision is None:
            customer_id = str(job.payload.get("customer_id") or "")
            return DispatchReport(
                results=[
                    DispatchResult(
                        decision_id=decision_id,
                        customer_id=customer_id,
                        status=DispatchStatus.SKIPPED,
                        reason=SkipReason.DECISION_NOT_FOUND.value,
                        detail=f"job {job.job_id} names a decision the log does not have",
                    )
                ]
            )
        state = states.get(decision.customer_id)
        snapshot = {decision.customer_id: state} if state is not None else {}
        return self._dispatch([decision], snapshot, now, allow_queue=False)

    def _dispatch(
        self,
        decisions: Sequence[Decision],
        states: Mapping[str, CustomerState],
        now: datetime,
        *,
        allow_queue: bool,
    ) -> DispatchReport:
        report = DispatchReport()
        goals = {g.goal_id: g for g in self.workspace.goals()}
        seen: set[str] = set()
        for decision in decisions:
            if decision.kind is not DecisionKind.ACT or decision.action is None:
                report.ignored += 1
                continue
            try:
                if decision.decision_id in seen:
                    raise _SkipError(SkipReason.DUPLICATE_IN_BATCH)
                seen.add(decision.decision_id)
                prepared = self._prepare(decision, states, goals, now)
            except _SkipError as skip:
                report.results.append(
                    DispatchResult(
                        decision_id=decision.decision_id,
                        customer_id=decision.customer_id,
                        status=DispatchStatus.SKIPPED,
                        reason=skip.reason.value,
                        detail=skip.detail,
                    )
                )
                continue
            if allow_queue and self.queue is not None and decision.action.send_at > now:
                report.results.append(self._enqueue(decision, prepared))
            else:
                report.results.append(self._send(decision, prepared, now, report))
        return report

    def _prepare(
        self,
        decision: Decision,
        states: Mapping[str, CustomerState],
        goals: Mapping[str, Goal],
        now: datetime,
    ) -> _Prepared:
        """Run every check and render the message; raise `_SkipError` at the first problem."""
        action = decision.action
        assert action is not None  # the caller only passes ACT decisions
        if any(e.type is EventType.SEND for e in self.events.for_decision(decision.decision_id)):
            raise _SkipError(SkipReason.ALREADY_SENT)
        state = states.get(decision.customer_id)
        if decision.group is Group.HOLDOUT or (state is not None and state.group is Group.HOLDOUT):
            raise _SkipError(SkipReason.HOLDOUT, "holdout customers are never contacted")

        goal = goals.get(action.goal_id)
        if goal is None:
            raise _SkipError(SkipReason.UNKNOWN_GOAL, action.goal_id)
        if goal.status is not GoalStatus.ACTIVE:
            raise _SkipError(SkipReason.GOAL_NOT_ACTIVE, f"goal is {goal.status.value}")
        if now < goal.starts_at or (goal.ends_at is not None and now >= goal.ends_at):
            raise _SkipError(SkipReason.GOAL_NOT_ACTIVE, "outside the goal's start and end dates")
        if goal.autonomy is AutonomyLevel.SHADOW:
            raise _SkipError(SkipReason.SHADOW_MODE, "shadow mode decides and logs but never sends")
        if action.goal_id in self.paused_goals:
            raise _SkipError(SkipReason.PAUSED_GOAL)
        channel = action.channel
        if channel in self.paused_channels:
            raise _SkipError(SkipReason.PAUSED_CHANNEL, channel.value)
        if channel not in self.adapters:
            raise _SkipError(SkipReason.NO_ADAPTER, channel.value)
        briefs = self.workspace.briefs(action.goal_id)
        if not briefs:
            raise _SkipError(SkipReason.NO_BRIEF, "a goal without a brief has no envelope to act in")
        if not any(channel in b.envelope.channels for b in briefs):
            raise _SkipError(SkipReason.CHANNEL_NOT_ALLOWED, f"{channel.value} is not in the envelope")

        if state is None:
            raise _SkipError(SkipReason.NO_STATE)
        stats = state.channels.get(channel)
        if channel is Channel.WEBHOOK:
            # The client's own tool holds consent for webhook sends; we still honour an opt-out we saw.
            reachable = stats is None or not stats.unsubscribed
        else:
            reachable = stats is not None and stats.consent and not stats.unsubscribed
        if not reachable:
            raise _SkipError(SkipReason.NOT_CONTACTABLE, f"no consent on {channel.value}")
        _check_journey_step(decision, state)

        item = self._content(action.goal_id, action.content_id)
        if item.channel is not channel:
            raise _SkipError(SkipReason.CONTENT_MISMATCH, f"content is for {item.channel.value}")
        if item.offer_id != action.offer_id:
            raise _SkipError(
                SkipReason.CONTENT_MISMATCH,
                f"content carries offer {item.offer_id!r}, decision chose {action.offer_id!r}",
            )
        offer = self._offer(briefs, action.offer_id)

        profile = self.profiles(decision.customer_id) if self.profiles is not None else {}
        try:
            rendered = render(item, state, profile, offer, now)
        except RenderError as err:
            raise _SkipError(SkipReason.RENDER_FAILED, str(err)) from err

        address = self.address_book(decision.customer_id, channel)
        if not address:
            raise _SkipError(SkipReason.NO_ADDRESS, channel.value)

        message = Message(
            message_id=message_id_for(decision.decision_id),
            decision_id=decision.decision_id,
            customer_id=decision.customer_id,
            channel=channel,
            to=address,
            subject=rendered.subject,
            body=rendered.body,
            asset_url=item.asset_url,
            template_name=item.template_name,
            template_params=rendered.template_params,
            send_at=action.send_at,
        )
        guard = self.guards.get(channel)
        if guard is not None:
            cap = min(b.envelope.max_contacts_per_week for b in briefs)
            refused = guard.check(message, cap, now, self.events)
            if refused is not None:
                raise _SkipError(refused[0], refused[1])
        attrs: dict[str, AttrValue] = {
            "message_id": message.message_id,
            "goal_id": action.goal_id,
            "content_id": item.content_id,
            "offer_id": action.offer_id,
            "group": decision.group.value,
            "follow_up_step": action.follow_up_step,
        }
        return _Prepared(message=message, attrs=attrs)

    def _content(self, goal_id: str, content_id: str | None) -> ContentItem:
        if content_id is None:
            raise _SkipError(SkipReason.NO_CONTENT, "the decision names no content")
        item = next(
            (
                c
                for c in self.workspace.content(goal_id=goal_id, approved_only=False)
                if c.content_id == content_id
            ),
            None,
        )
        if item is None:
            raise _SkipError(SkipReason.CONTENT_NOT_FOUND, content_id)
        if item.status is not ContentStatus.APPROVED:
            raise _SkipError(SkipReason.CONTENT_NOT_APPROVED, f"{content_id} is {item.status.value}")
        return item

    def _offer(self, briefs: Sequence[Brief], offer_id: str) -> OfferOption | None:
        """The decision's offer, which must be one the goal's brief allows. "none" means no offer."""
        if offer_id == NO_OFFER.offer_id:
            return None
        for brief in briefs:
            for offer in brief.envelope.offers:
                if offer.offer_id == offer_id:
                    return offer
        raise _SkipError(SkipReason.OFFER_NOT_ALLOWED, f"{offer_id!r} is not in the goal's envelope")

    def _enqueue(self, decision: Decision, prepared: _Prepared) -> DispatchResult:
        assert self.queue is not None and decision.action is not None
        job_id = send_job_id(decision.decision_id)
        self.queue.enqueue(
            Job(
                job_id=job_id,
                kind=SEND_JOB_KIND,
                payload={
                    "decision_id": decision.decision_id,
                    "customer_id": decision.customer_id,
                    "goal_id": decision.action.goal_id,
                    "channel": decision.action.channel.value,
                    "message_id": prepared.message.message_id,
                },
                run_at=decision.action.send_at,
            )
        )
        return DispatchResult(
            decision_id=decision.decision_id,
            customer_id=decision.customer_id,
            status=DispatchStatus.QUEUED,
            message_id=prepared.message.message_id,
            job_id=job_id,
        )

    def _send(
        self, decision: Decision, prepared: _Prepared, now: datetime, report: DispatchReport
    ) -> DispatchResult:
        message = prepared.message
        adapter = self.adapters[message.channel]
        try:
            receipt = adapter.send(message)
            accepted, error, provider_id = receipt.accepted, receipt.error, receipt.provider_message_id
        except Exception as exc:  # an adapter bug or a network error must not stop the batch
            accepted, error, provider_id = False, f"{type(exc).__name__}: {exc}", None

        attrs = dict(prepared.attrs)
        if provider_id:
            attrs["provider_message_id"] = provider_id
        if accepted:
            event = Event(
                event_id=send_event_id(message.message_id),
                customer_id=message.customer_id,
                type=EventType.SEND,
                ts=now,
                source=SOURCE,
                channel=message.channel,
                decision_id=decision.decision_id,
                attrs=attrs,
            )
        else:
            error = (error or "the provider did not accept the message")[:MAX_ERROR_LEN]
            attrs["error"] = error
            guard = self.guards.get(message.channel)
            if guard is not None:
                attrs.update(guard.failure_attrs(error, now))
            event = Event(
                event_id=stable_id("ev", message.message_id, EventType.FAIL.value, now.isoformat()),
                customer_id=message.customer_id,
                type=EventType.FAIL,
                ts=now,
                source=SOURCE,
                channel=message.channel,
                decision_id=decision.decision_id,
                attrs=attrs,
            )
        self.events.append([event])
        report.events.append(event)
        return DispatchResult(
            decision_id=decision.decision_id,
            customer_id=decision.customer_id,
            status=DispatchStatus.SENT if accepted else DispatchStatus.FAILED,
            reason="" if accepted else error or "",
            message_id=message.message_id,
            provider_message_id=provider_id,
        )
