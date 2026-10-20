"""Klaviyo as a channel: Autonomy decides, the shop's own Klaviyo sends, and we read back what happened.

Shaped like `ses.py`, so the `Dispatcher` and the `DispatchGateway` treat it as any other provider
(holdout, consent, envelope, quiet hours, exactly-once, rate limits and retries all apply before
`send` is called):

- **Send = hand off.** `KlaviyoChannelAdapter.send` creates one `Autonomy Decision` event on the
  person's Klaviyo profile carrying the rendered subject and body, the content, offer and discount
  code, the channel and the intended send time (`handoff_properties`). Autonomy's flow in Klaviyo
  (created on connect, `autonomy.connectors.klaviyo.setup`) sends it with the shop's own sender,
  footer, unsubscribes and suppressions. `unique_id` is the decision id, so Klaviyo records a
  repeated hand-off once; the base client may therefore retry the POST after a timeout.
- **Refuse rather than drop.** Until the channel's flow is live in Klaviyo the adapter refuses
  (`not_ready:`), so a decision is never "sent" into a flow that does not exist. A connection whose
  access is gone is marked `needs_reconnect` with its one alert, and the send is refused.
- **Errors the gateway understands**: a refused request (a 4xx) is `invalid:` (not retried, not failed
  over), a timeout after the request may have arrived is `ambiguous:` (retried on Klaviyo, never
  failed over, safe because of `unique_id`), a long rate limit is a plain refusal with the time.
- **Delivery read-back.** Klaviyo does not copy the decision's properties onto "Received Email", so
  the connector links engagement from Autonomy's flows to the decision through `EventStoreHandoffs`
  (the latest hand-off to that customer on that channel). `KlaviyoDeliveryWatch` then looks at every
  hand-off older than its window (6 h for email, 18 h for SMS, which waits for quiet hours): with no
  DELIVER, FAIL or BOUNCE for it, and the engagement stream synced past the window, it records a
  FAIL with `reason=not_delivered_by_tool` (Klaviyo suppressed the person, a flow filter or a person
  turned the flow off, quiet hours ran out). Learning already treats FAIL as "not delivered"; proof
  must too (see docs/v2/CONNECTORS.md, Klaviyo). It never concludes while engagement data is stale.

Klaviyo sends us no webhooks for this, so `verify_webhook` rejects everything (fail closed) and
`parse_webhook` returns nothing; the engagement stream is the read-back path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from autonomy.channels.ratelimit import Rate
from autonomy.connectors.base import (
    ConnectionStatus,
    ConnectorFailure,
    Lane,
    NeedsReconnect,
    RateLimited,
    RemoteError,
    Stream,
    TransientFailure,
    redact,
)
from autonomy.connectors.http import idempotency_key
from autonomy.connectors.klaviyo.connector import DECISION_EVENT, KlaviyoConnector
from autonomy.connectors.klaviyo.setup import EMAIL_FLOW, SMS_FLOW, SetupStatus
from autonomy.connectors.sync import enqueue_once, slot_after
from autonomy.contracts import (
    Alert,
    Channel,
    Decision,
    Event,
    EventType,
    Job,
    Message,
    OfferOption,
    SendReceipt,
    stable_id,
)
from autonomy.contracts.events import AttrValue

if TYPE_CHECKING:
    from autonomy.connectors.base import Connection
    from autonomy.connectors.sync import ConnectorEngine
    from autonomy.interfaces import ChannelAdapter, DecisionLog, EventStore, JobQueue, Workspace

__all__ = [
    "ALERT_SETUP",
    "ALERT_UNLINKED",
    "NOT_DELIVERED",
    "PROVIDER",
    "SCHEMA",
    "WATCH_JOB_KIND",
    "DecisionDetails",
    "EventStoreHandoffs",
    "KlaviyoChannelAdapter",
    "KlaviyoDeliveryWatch",
    "WatchReport",
    "details_from",
    "ensure_klaviyo_setup",
    "handoff_properties",
    "is_handoff",
    "klaviyo_adapters",
    "klaviyo_handlers",
    "profile_for",
    "provider_rates",
    "seed_klaviyo_watch",
    "watch_job",
]

PROVIDER: Final[str] = "klaviyo"
SCHEMA: Final[str] = "autonomy.klaviyo.v1"
NOT_DELIVERED: Final[str] = "not_delivered_by_tool"
"""The FAIL reason for a hand-off Klaviyo never delivered (no Received Email/SMS within the window)."""
ALERT_SETUP: Final[str] = "connector.klaviyo_setup"
ALERT_UNLINKED: Final[str] = "connector.klaviyo_unlinked"
UNLINKED_AFTER: Final[int] = 3
"""How many overdue hand-offs, with none delivered, make the watch ask a person to look (it holds
from the first: a "not delivered" is only concluded once deliveries are seen to link at all)."""
WATCH_JOB_KIND: Final[str] = "klaviyo_delivery_watch"
WATCH_EVERY: Final[timedelta] = timedelta(hours=1)
DEFAULT_WINDOWS: Final[Mapping[Channel, timedelta]] = {
    Channel.EMAIL: timedelta(hours=6),
    Channel.SMS: timedelta(hours=18),  # an SMS handed over at 20:00 waits for quiet hours to end
}
_RESOLVED: Final[frozenset[EventType]] = frozenset(
    {EventType.DELIVER, EventType.FAIL, EventType.BOUNCE, EventType.BLOCK}
)

DecisionDetails = Callable[[Message], Mapping[str, AttrValue | None]]
"""Extra event properties for a message: content id, offer, discount code, slot values, ..."""
DiscountCodes = Callable[[Decision, OfferOption | None], str | None]
"""(decision, its offer) -> the discount code to print, e.g. a per-person Shopify code, or None."""


def provider_rates(name: str = PROVIDER) -> dict[tuple[str, Channel], Rate]:
    """Gateway `RateLimiter(provider_rates=...)` entries matching Klaviyo's Create Event limits
    (burst 350/s, steady 3,500/min): 350 tokens refilling over 6 s is 3,500 a minute."""
    rate = Rate(capacity=350, per=timedelta(seconds=6))
    return {(name, Channel.EMAIL): rate, (name, Channel.SMS): rate}


def is_handoff(event: Event, connection_id: str | None = None) -> bool:
    """A SEND the dispatcher recorded for a decision handed to Klaviyo (optionally: this connection)."""
    provider_id = event.attrs.get("provider_message_id")
    if event.type is not EventType.SEND or not isinstance(provider_id, str):
        return False
    prefix = f"{PROVIDER}:" if connection_id is None else f"{PROVIDER}:{connection_id}:"
    return provider_id.startswith(prefix)


def profile_for(message: Message) -> dict[str, str]:
    """How the hand-off names the Klaviyo profile: the email or phone the address book gave, or,
    when it gave the customer's id (`Message.to` may be "the client tool's customer id"), the
    profile's `external_id` (how Klaviyo's Shopify integration keys Shopify customers). A profile
    Klaviyo cannot message is never delivered, and the delivery watch then reports it."""
    to = message.to.strip()
    if "@" in to:
        return {"email": to}
    if message.channel is Channel.SMS and to.startswith("+"):
        return {"phone_number": to}
    return {"external_id": to}


def handoff_properties(
    message: Message, handed_at: datetime, extra: Mapping[str, AttrValue | None] | None = None
) -> dict[str, AttrValue]:
    """The decision event's properties: flat, so the template reads `{{ event.subject }}` and the
    shop can segment on any of them. Empty values are left out."""
    props: dict[str, AttrValue | None] = {
        "decision_id": message.decision_id,
        "message_id": message.message_id,
        "customer_id": message.customer_id,
        "channel": message.channel.value,
        "subject": message.subject,
        "body": message.body,
        "asset_url": message.asset_url,
        "send_at": _iso(message.send_at),
        "handed_at": _iso(handed_at),
        "autonomy_schema": SCHEMA,
    }
    for name, value in (extra or {}).items():
        props.setdefault(name, value)
    return {k: v for k, v in props.items() if v is not None and v != ""}


def details_from(
    decisions: DecisionLog, workspace: Workspace, *, discount_code: DiscountCodes | None = None
) -> DecisionDetails:
    """Content, offer and discount code for a message, looked up from its decision and goal brief.

    `discount_code` is called again when a hand-off is retried (a timeout, a rate limit, a gateway
    retry), and Klaviyo keeps only the first event per decision (`unique_id`), so it must return the
    same code for the same decision (e.g. create the Shopify code with an idempotency key derived from
    the decision id), or each retry leaves an unused code behind."""

    def details(message: Message) -> Mapping[str, AttrValue | None]:
        decision = decisions.get(message.decision_id)
        if decision is None or decision.action is None:
            return {}
        action = decision.action
        offer: OfferOption | None = None
        for brief in workspace.briefs(action.goal_id):
            offer = next((o for o in brief.envelope.offers if o.offer_id == action.offer_id), offer)
        out: dict[str, AttrValue | None] = {
            "goal_id": action.goal_id,
            "content_id": action.content_id,
            "offer_id": action.offer_id,
            "follow_up_step": action.follow_up_step,
            "item_id": action.item_id,
        }
        if offer is not None and offer.offer_id != "none":
            out |= {"offer_label": offer.label, "offer_kind": offer.kind.value, "offer_amount": offer.amount}
        if discount_code is not None:
            out["discount_code"] = discount_code(decision, offer)
        return out

    return details


class EventStoreHandoffs:
    """The connector's `HandoffLookup`: the decision most recently handed to Klaviyo for a customer and
    channel, at or before an engagement event (within `lookback`, allowing `skew` between clocks)."""

    def __init__(
        self,
        events: EventStore,
        *,
        lookback: timedelta = timedelta(days=3),
        skew: timedelta = timedelta(minutes=5),
    ) -> None:
        self.events = events
        self.lookback = lookback
        self.skew = skew

    def __call__(self, customer_id: str, channel: Channel, at: datetime) -> str | None:
        found: Event | None = None
        for event in self.events.for_customer(customer_id, since=at - self.lookback, until=at + self.skew):
            ours = event.channel is channel and event.decision_id is not None and is_handoff(event)
            if ours and (found is None or (event.ts, event.event_id) > (found.ts, found.event_id)):
                found = event
        return found.decision_id if found is not None else None


class KlaviyoChannelAdapter:
    """Hands one channel's messages to Klaviyo for one connection (see the module docstring)."""

    def __init__(
        self,
        engine: ConnectorEngine,
        connection_id: str,
        *,
        channel: Channel = Channel.EMAIL,
        details: DecisionDetails | None = None,
    ) -> None:
        if channel not in DEFAULT_WINDOWS:
            raise ValueError(f"Klaviyo sends email and SMS, not {channel.value}")
        self.engine = engine
        self.connection_id = connection_id
        self.channel = channel
        self.details = details

    def _connector(self) -> KlaviyoConnector:
        connector = self.engine.registry.get(PROVIDER)
        if not isinstance(connector, KlaviyoConnector):
            raise TypeError("the registry's klaviyo connector is not a KlaviyoConnector")
        return connector

    def send(self, message: Message) -> SendReceipt:
        """Hand one message to Klaviyo. Failures come back as a refused receipt, never as an exception."""
        if message.channel is not self.channel:
            return _refused(
                f"invalid: this Klaviyo adapter sends {self.channel.value}, not {message.channel.value}"
            )
        engine = self.engine
        now = engine.clock.now()
        connection = engine.store.get_connection(self.connection_id)
        if connection is None or connection.kind != PROVIDER:
            return _refused("not_ready: no Klaviyo connection")
        if connection.status is not ConnectionStatus.ACTIVE:
            return _refused(f"not_ready: the Klaviyo connection is {connection.status.value}")
        connector = self._connector()
        profile = profile_for(message)
        ctx = engine.context(connection, now)
        try:
            setup = connector.send_setup(ctx, _channels(connection.config, self.channel))
            if not setup.ready_for(self.channel):
                found = setup.channel(self.channel)
                why = found.detail if found is not None and found.detail else setup.state
                return _refused(f"not_ready: Klaviyo's {self.channel.value} flow is not live ({why})")
            # Only now: `details` may mint a discount code, which a refused hand-off must not cost.
            try:
                extra = self.details(message) if self.details is not None else None
            except Exception as exc:  # a lookup bug must not stop the batch; send what we know
                extra = {"details_error": type(exc).__name__}
            payload = {
                "profile": profile,
                "properties": handoff_properties(message, now, extra),
                "unique_id": message.decision_id,
                "time": _iso(now),
            }
            result = connector.write(
                ctx,
                DECISION_EVENT,
                payload,
                idempotency_key=idempotency_key(message.decision_id, DECISION_EVENT),
            )
        except NeedsReconnect as failure:
            hook = engine.tokens.on_revoked
            if hook is not None:
                hook(connection, failure, now)  # marks needs_reconnect and raises the one alert
            return _refused(f"needs_reconnect: {failure.message}")
        except RateLimited as failure:
            when = failure.retry_at.isoformat() if failure.retry_at else "later"
            return _refused(f"rate_limited: Klaviyo asked us to wait until {when}")
        except TransientFailure as failure:
            # The POST may have arrived. Retrying is safe (unique_id) and must stay on Klaviyo.
            return _refused(f"ambiguous: {failure.message}")
        except RemoteError as failure:
            if failure.status is not None and 400 <= failure.status < 500 and failure.status != 429:
                return _refused(f"invalid: {failure.message}")
            return _refused(failure.message)
        except ValueError as exc:
            return _refused(f"invalid: {redact(str(exc))}")
        except ConnectorFailure as failure:
            return _refused(failure.message)
        finally:
            ctx.http.close()
        return SendReceipt(accepted=True, provider_message_id=result.provider_id)

    # --- listening: Klaviyo has no webhooks for this; the engagement stream reads it back ----------

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # noqa: ARG002
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[Event]:  # noqa: ARG002
        return []


def klaviyo_adapters(
    engine: ConnectorEngine, connection: Connection, *, details: DecisionDetails | None = None
) -> dict[Channel, ChannelAdapter]:
    """One `KlaviyoChannelAdapter` per channel this connection sends (email, plus SMS with `sms=on`),
    for the `Dispatcher` (`adapters=`) or as gateway providers (`autonomy.connectors.wiring.send_providers`,
    which puts our own SES behind it as the fallback)."""
    return {
        channel: KlaviyoChannelAdapter(engine, connection.connection_id, channel=channel, details=details)
        for channel in _channels(connection.config)
    }


@dataclass
class WatchReport:
    """What one delivery check found."""

    connection_id: str
    checked: int = 0
    delivered: int = 0
    not_delivered: int = 0
    waiting: int = 0
    held: str | None = None
    """Why nothing could be concluded (engagement data not fresh enough), if so."""
    events: list[Event] = field(default_factory=list)


class KlaviyoDeliveryWatch:
    """Turns hand-offs Klaviyo never delivered into FAIL `not_delivered_by_tool` events."""

    def __init__(
        self,
        engine: ConnectorEngine,
        events: EventStore,
        connection_id: str,
        *,
        windows: Mapping[Channel, timedelta] | None = None,
        lookback: timedelta = timedelta(days=2),
    ) -> None:
        self.engine = engine
        self.events = events
        self.connection_id = connection_id
        self.windows = dict(DEFAULT_WINDOWS if windows is None else windows)
        self.lookback = lookback

    def synced_through(self) -> datetime | None:
        """Up to when engagement is known complete: the live watermark minus the overlap re-read
        (an event Klaviyo processes late still lands inside the next overlap)."""
        position = self.engine.store.position(self.connection_id, Stream.ENGAGEMENT, Lane.LIVE)
        if position.high_watermark is None:
            return None
        connector = self.engine.registry.find(PROVIDER)
        overlap = timedelta(hours=2)
        if connector is not None:
            overlap = connector.capabilities.stream(Stream.ENGAGEMENT).overlap
        return position.high_watermark - overlap

    def check(self, now: datetime) -> WatchReport:
        """Record a FAIL for each overdue hand-off with no delivery outcome, unless the data cannot
        tell: engagement not read past the window, engagement not linked to hand-offs at all (the
        connector has no `handoffs` lookup), or several overdue hand-offs and not one delivered
        (customer ids that do not meet, or flows that never send). A false "not delivered" would
        take real sends out of the proof, so in those cases it holds, and the last one alerts once."""
        report = WatchReport(self.connection_id)
        connector = self.engine.registry.find(PROVIDER)
        if isinstance(connector, KlaviyoConnector) and connector.handoffs is None:
            report.held = (
                "the Klaviyo connector links no engagement to hand-offs (build it with "
                "handoffs=EventStoreHandoffs(events)), so a missing delivery proves nothing"
            )
            return report
        through = self.synced_through()
        longest = max(self.windows.values(), default=timedelta(hours=6))
        since = now - self.lookback - longest
        overdue: list[Event] = []
        for send in self.events.scan(since=since, until=now):
            if not is_handoff(send, self.connection_id) or send.decision_id is None or send.channel is None:
                continue
            report.checked += 1
            outcomes = {e.type for e in self.events.for_decision(send.decision_id)}
            if EventType.DELIVER in outcomes:
                report.delivered += 1
                continue
            if outcomes & _RESOLVED:
                continue  # Klaviyo told us itself (dropped, bounced): already counted as not delivered
            deadline = send.ts + self.windows.get(send.channel, longest)
            if now < deadline:
                report.waiting += 1
                continue
            if through is None or through < deadline:
                report.waiting += 1
                report.held = "Klaviyo engagement has not been read past the delivery window yet"
                continue
            overdue.append(self._not_delivered(send, deadline))
        if overdue and report.delivered == 0:
            report.waiting += len(overdue)
            report.held = (
                f"{len(overdue)} hand-offs to Klaviyo are overdue and none in the last "
                f"{self.lookback.days} days was delivered: either Klaviyo is not sending them or its "
                "deliveries do not reach Autonomy's customer ids"
            )
            if len(overdue) >= UNLINKED_AFTER:
                self._alert_unlinked(now, len(overdue))
            return report
        report.events = overdue
        if report.events:
            self.events.append(report.events)
            report.not_delivered = len(report.events)
        return report

    def _alert_unlinked(self, now: datetime, overdue: int) -> None:
        sink = self.engine.monitor.desk.alerts
        connection = self.engine.store.get_connection(self.connection_id)
        if sink is None or connection is None:
            return
        sink.put(
            [
                Alert(
                    alert_id=stable_id("alert", ALERT_UNLINKED, self.connection_id),
                    at=now,
                    severity="warning",
                    code=ALERT_UNLINKED,
                    message=(
                        f"Klaviyo ({connection.label}): {overdue} decisions handed to Klaviyo show no delivery. "
                        f"Check that the flows named '{EMAIL_FLOW}' / '{SMS_FLOW}' are live and send, and that "
                        "Klaviyo profiles carry the same customer id or email Autonomy uses (connection "
                        "setting 'identity'). Autonomy keeps sending but does not count these as "
                        "undelivered until deliveries are seen."
                    ),
                )
            ]
        )

    def _not_delivered(self, send: Event, deadline: datetime) -> Event:
        assert send.decision_id is not None and send.channel is not None
        window = self.windows.get(send.channel, DEFAULT_WINDOWS[Channel.EMAIL])
        attrs: dict[str, AttrValue] = {
            "reason": NOT_DELIVERED,
            "provider": PROVIDER,
            "window_hours": round(window.total_seconds() / 3600, 2),
        }
        for name in ("message_id", "provider_message_id", "goal_id", "content_id", "offer_id", "group"):
            value = send.attrs.get(name)
            if value is not None:
                attrs[name] = value
        return Event(
            event_id=stable_id("ev", PROVIDER, NOT_DELIVERED, send.decision_id),
            customer_id=send.customer_id,
            type=EventType.FAIL,
            ts=deadline,
            source=PROVIDER,
            channel=send.channel,
            decision_id=send.decision_id,
            attrs=attrs,
        )


# --- setup check, alert and jobs ------------------------------------------------------------------


def ensure_klaviyo_setup(
    engine: ConnectorEngine, connection_id: str, now: datetime, *, channels: Sequence[Channel] = ()
) -> SetupStatus | None:
    """Re-check (and finish) the send setup; raise one alert when a person must do the guided step.

    The alert id is stable for the same problem (`SetupStatus.fingerprint`), and alert stores ignore
    a repeated id, so an hourly check never repeats it. Nothing is raised while the connection is
    not active (its own reconnect or uninstall alert already told a person)."""
    connection = engine.store.get_connection(connection_id)
    if connection is None or connection.kind != PROVIDER or connection.status is not ConnectionStatus.ACTIVE:
        return None
    if connection.config.get("send") == "off":
        return None
    connector = engine.registry.get(PROVIDER)
    if not isinstance(connector, KlaviyoConnector):
        return None
    ctx = engine.context(connection, now)
    try:
        status = connector.send_setup(ctx, tuple(channels) or _channels(connection.config), refresh=True)
    except NeedsReconnect as failure:
        hook = engine.tokens.on_revoked
        if hook is not None:
            hook(connection, failure, now)
        return None
    except ConnectorFailure:
        return None  # Klaviyo unreachable right now; the next hourly check tries again
    finally:
        ctx.http.close()
    sink = engine.monitor.desk.alerts
    if status.state == "guided" and sink is not None:
        lines = [f"{i}. {step}" for i, step in enumerate(status.steps(), start=1)]
        problems = "; ".join(f"{c.channel.value}: {c.detail}" for c in status.channels if not c.ready)
        sink.put(
            [
                Alert(
                    alert_id=stable_id("alert", ALERT_SETUP, connection_id, status.fingerprint()),
                    at=now,
                    severity="warning",
                    code=ALERT_SETUP,
                    message=(
                        f"Klaviyo ({connection.label}): Autonomy could not finish its send setup ({problems}). "
                        "Autonomy holds those decisions until it is done. One-time steps in Klaviyo: "
                        + " ".join(lines)
                    ),
                )
            ]
        )
    return status


def watch_job(connection_id: str, run_at: datetime) -> Job:
    return Job(
        job_id=stable_id("job", WATCH_JOB_KIND, connection_id, run_at.astimezone(UTC).isoformat()),
        kind=WATCH_JOB_KIND,
        payload={"connection_id": connection_id},
        run_at=run_at,
    )


def seed_klaviyo_watch(queue: JobQueue, connection_id: str, now: datetime) -> Job:
    """Start the hourly setup-and-delivery check for a Klaviyo connection (call once after connect)."""
    job = watch_job(connection_id, now)
    enqueue_once(queue, job)
    return job


def klaviyo_handlers(
    engine: ConnectorEngine,
    events: EventStore,
    queue: JobQueue,
    *,
    windows: Mapping[Channel, timedelta] | None = None,
) -> dict[str, Callable[[Job, datetime], None]]:
    """Worker handlers for `klaviyo_delivery_watch`: finish/re-check the send setup, then record
    silent drops; the job re-queues itself on the next hourly slot while the connection exists."""

    def run(job: Job, now: datetime) -> None:
        connection_id = str(job.payload.get("connection_id") or "")
        connection = engine.store.get_connection(connection_id)
        if connection is None or connection.status is ConnectionStatus.DISCONNECTED:
            return
        enqueue_once(queue, watch_job(connection_id, slot_after(now, WATCH_EVERY)))
        if connection.status is not ConnectionStatus.ACTIVE:
            return
        ensure_klaviyo_setup(engine, connection_id, now)
        KlaviyoDeliveryWatch(engine, events, connection_id, windows=windows).check(now)

    return {WATCH_JOB_KIND: run}


def _channels(config: Mapping[str, str], wanted: Channel | None = None) -> tuple[Channel, ...]:
    from autonomy.connectors.klaviyo.connector import channels_for

    channels = channels_for(config)
    if wanted is not None and wanted not in channels:
        channels = (*channels, wanted)
    return channels


def _refused(error: str) -> SendReceipt:
    return SendReceipt(accepted=False, error=redact(error, limit=480))


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")
