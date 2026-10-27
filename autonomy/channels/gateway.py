"""The dispatch gateway: the last check before a message leaves, and the one place that sends.

The `Dispatcher` turns decisions into messages and guards what the policy must never get wrong
(holdout, consent flags, paused goals, approved content). The gateway wraps it and adds what only
makes sense *at the moment of sending*, for every channel and every country:

- **A compliance gate, re-run at send time.** A message queued at 20:55 for 21:05 is checked again
  at 21:05, by which time the person may have texted STOP or the quiet hours may have begun. The
  gate asks: is there a consent proof, for this channel and this purpose, that our registry knows
  and that is still live? Is the number on a do-not-disturb register the country's rules make us
  honour (India's NCPR for promotional SMS)? Is it quiet hours in the recipient's own time zone,
  by the country's rule (TCPA's 8am-9pm in the US)? Does an Indian SMS name a DLT template? Is
  there a mandate id when the deployment requires one? A hard "no" refuses the message with
  stable reason codes; a "not now" (quiet hours, a future `send_at`, a rate limit) queues it.
- **Exactly once.** Every request carries an idempotency key (or a decision id, which is one). A
  second request with the same key returns the first result and sends nothing, whatever it says.
- **Rate limits** per (provider, channel) and per recipient (`ratelimit.RateLimiter`).
- **Carrier failover without double sends.** Each channel has providers in order. A definite
  rejection (an HTTP 4xx/5xx answer, a refused connection) moves on to the next one. An
  *ambiguous* error (a read timeout: the provider may have taken the message) does not: sending
  through another carrier could put two texts on the person's phone, so the gateway retries the
  same provider later with the same message id, which providers that honour idempotency keys
  de-duplicate. A problem with the message itself (`invalid:` errors, e.g. a DLT variable that is
  too long) is neither retried nor failed over: it would fail everywhere. Every attempt is
  recorded on the message's ledger entry before the next one starts.
- **Buffered retry.** When every provider refused, the message goes back on the `JobQueue` with
  exponential backoff (deterministic jitter from the key, so runs repeat), up to `max_rounds`.
- **One telemetry stream.** Every step emits a `TelemetryEvent` (received, refused, queued,
  attempt, sent, failed, replayed, webhook), and sends and final failures are written to the event
  store as SEND / FAIL events like the dispatcher's own. A telemetry sink that raises is logged and
  counted (`telemetry_errors`); it never stops or changes a send.

Country rules come as `SendRules`, looked up per (recipient country, channel) by a `RulesFor`
callable. `default_rules` is a small built-in set (India in depth, the US and the EU/UK basic);
`pack_rules()` builds the same shape from `autonomy.compliance_packs`, which are authoritative and
are what a deployment should pass.

Time is passed in everywhere. Nothing here reads the clock; the API layer does that once per request.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator

from autonomy.channels.common import send_event_id
from autonomy.channels.consent import (
    PHONE_CHANNELS,
    ConsentProof,
    ConsentRegistry,
    DndRegistry,
    normalize_address,
    normalize_msisdn,
    verify_against,
)
from autonomy.channels.dispatcher import (
    AddressBook,
    Dispatcher,
    DispatchReport,
    DispatchResult,
    DispatchStatus,
    ProfileBook,
    message_id_for,
)
from autonomy.channels.ratelimit import RateLimiter
from autonomy.channels.sms import PERMANENT_ERROR_PREFIX, RECEIVED_AT_HEADER, SentRef, SmsAdapter
from autonomy.channels.whatsapp_guard import whatsapp_guards
from autonomy.compliance_packs import pack_for
from autonomy.compliance_packs.base import CompliancePack, PackSettings, Purpose, covering_window
from autonomy.contracts import (
    Channel,
    Contract,
    CustomerState,
    Decision,
    DecisionKind,
    Event,
    EventType,
    Job,
    Message,
    SendReceipt,
    stable_id,
)
from autonomy.contracts.events import AttrValue
from autonomy.interfaces import ChannelAdapter, DecisionLog, EventStore, JobQueue, StateStore, Workspace

__all__ = [
    "AMBIGUOUS_ERRORS",
    "GATEWAY_DECISION_JOB",
    "GATEWAY_SEND_JOB",
    "DispatchGateway",
    "FailoverAdapter",
    "GatewayError",
    "InMemoryMessageLedger",
    "MessageLedger",
    "MessageRecord",
    "Provider",
    "Recipient",
    "RefusalReason",
    "RetryPolicy",
    "RulesFor",
    "SendAttempt",
    "SendOutcome",
    "SendPayload",
    "SendRequest",
    "SendRules",
    "SendStatus",
    "TelemetryEvent",
    "WebhookOutcome",
    "default_rules",
    "pack_rules",
    "quiet_until",
]

GATEWAY_SEND_JOB = "gateway_send"
"""Queue job kind for a request waiting for its time (quiet hours, send_at, a rate limit, a retry)."""
GATEWAY_DECISION_JOB = "gateway_send_decision"
"""Queue job kind for a decision the gateway deferred or will retry."""
SOURCE = "gateway"
MAX_ERROR_LEN = 500

_log = logging.getLogger(__name__)

AMBIGUOUS_ERRORS: tuple[str, ...] = (
    "ReadTimeout",
    "WriteTimeout",
    "TimeoutException",
    "RemoteProtocolError",
    "ReadError",
    "ambiguous:",
)
"""Error prefixes after which the provider may have accepted the message: never fail over on these."""


def is_permanent(error: str | None) -> bool:
    return bool(error) and str(error).startswith(PERMANENT_ERROR_PREFIX)


def is_ambiguous(error: str | None) -> bool:
    return bool(error) and str(error).startswith(AMBIGUOUS_ERRORS)


# --- country rules -------------------------------------------------------------------------------

# The same purposes the compliance packs use (`Purpose`), as plain strings for request bodies.
PROMOTIONAL: str = Purpose.PROMOTIONAL.value
TRANSACTIONAL: str = Purpose.TRANSACTIONAL.value
SERVICE: str = Purpose.SERVICE.value


@dataclass(frozen=True)
class SendRules:
    """What one country's law asks of a message at the moment it is sent."""

    country: str
    calling_code: str | None = None
    quiet_start_hour: int | None = 21
    """Local hour quiet time starts; None for no quiet hours."""
    quiet_end_hour: int | None = 9
    """Local hour it ends (the first hour sending is allowed again)."""
    quiet_purposes: frozenset[str] = frozenset({PROMOTIONAL})
    timezones: tuple[str, ...] = ("UTC",)
    """The recipient's possible zones when theirs is unknown: quiet if it is quiet in any of them."""
    consent_exempt_purposes: frozenset[str] = frozenset({TRANSACTIONAL})
    consent_max_age: timedelta | None = None
    dnd_channels: frozenset[Channel] = frozenset()
    dnd_purposes: frozenset[str] = frozenset({PROMOTIONAL})
    dnd_registry: str | None = None
    """Which register `dnd_channels` refers to ('NCPR', 'National DNC'): picks the gateway's registry."""
    dlt_required: bool = False
    email_unsubscribe_required: bool = False

    def quiet_applies(self, purpose: str) -> bool:
        return (
            self.quiet_start_hour is not None
            and self.quiet_end_hour is not None
            and purpose in self.quiet_purposes
        )


RulesFor = Callable[[str | None, Channel], SendRules]
"""(recipient country as ISO 3166 alpha-2 or None if unknown, channel) -> the rules to send by."""

INDIA = SendRules(
    country="IN",
    calling_code="91",
    # The band TRAI's regulations allow promotional messages in; the India pack is authoritative.
    quiet_start_hour=21,
    quiet_end_hour=9,
    quiet_purposes=frozenset({PROMOTIONAL}),
    timezones=("Asia/Kolkata",),
    consent_exempt_purposes=frozenset({TRANSACTIONAL}),
    dnd_channels=frozenset({Channel.SMS}),
    dnd_purposes=frozenset({PROMOTIONAL}),
    dnd_registry="NCPR",
    dlt_required=True,
)
UNITED_STATES = SendRules(
    country="US",
    calling_code="1",
    quiet_start_hour=21,  # TCPA: no telephone solicitation before 8am or after 9pm, recipient's time
    quiet_end_hour=8,
    quiet_purposes=frozenset({PROMOTIONAL, SERVICE}),
    # Every zone a US number may be in when theirs is unknown, Arizona (no DST), Alaska and Hawaii too.
    timezones=(
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Phoenix",
        "America/Los_Angeles",
        "America/Anchorage",
        "Pacific/Honolulu",
    ),
    consent_exempt_purposes=frozenset({TRANSACTIONAL}),
)
EUROPE = SendRules(
    country="EU",
    quiet_start_hour=None,  # no statutory band; the goal's envelope still sets quiet hours upstream
    quiet_end_hour=None,
    timezones=("Europe/London", "Europe/Paris", "Europe/Helsinki"),
    consent_exempt_purposes=frozenset({TRANSACTIONAL}),
    email_unsubscribe_required=True,
)
FALLBACK = SendRules(country="default", consent_exempt_purposes=frozenset({TRANSACTIONAL}))
"""Unknown country: quiet 21:00-09:00 in UTC (or the recipient's zone), consent needed for marketing."""

EU_UK_COUNTRIES: frozenset[str] = frozenset(
    [
        "AT",
        "BE",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DK",
        "EE",
        "FI",
        "FR",
        "DE",
        "GR",
        "HU",
        "IE",
        "IT",
        "LV",
        "LT",
        "LU",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SK",
        "SI",
        "ES",
        "SE",
        "GB",
        "IS",
        "LI",
        "NO",
    ]
)
_CALLING_CODES: tuple[tuple[str, str, int], ...] = (("91", "IN", 12), ("44", "GB", 12), ("1", "US", 11))
_NATIONAL_CODES: Mapping[str, str] = {country: code for code, country, _ in _CALLING_CODES}
"""Countries whose national number formats `normalize_msisdn` reads correctly (10 digits, or a 0
trunk prefix). Anywhere else a phone number must come in international form (+CC or 00CC)."""


def default_rules(country: str | None, channel: Channel = Channel.SMS) -> SendRules:  # noqa: ARG001
    """The built-in rules: India in depth, the US and the EU/UK basic, a careful fallback.

    The same rules serve every channel; they name the channels a rule binds (`dnd_channels`).
    """
    code = (country or "").upper()
    if code == "IN":
        return INDIA
    if code == "US":
        return UNITED_STATES
    if code in EU_UK_COUNTRIES or code == "EU":
        return EUROPE if code == "EU" else _with_country(EUROPE, code)
    return FALLBACK


def _with_country(rules: SendRules, country: str) -> SendRules:
    return replace(rules, country=country)


def pack_rules(
    settings: PackSettings | None = None, *, packs: Sequence[CompliancePack] | None = None
) -> RulesFor:
    """A `RulesFor` answered by the compliance packs (`autonomy.compliance_packs.pack_for`).

    A pack answers per (channel, purpose); `SendRules` holds one quiet window and the set of
    purposes it binds, so windows that differ by purpose are merged the strict way (quiet whenever
    any is). Consent is required (a proof must be shown) where the pack says `explicit`; `none`
    and `opt_out` (e.g. CAN-SPAM email) need no proof, and an opt-out is honoured through the
    suppression list. Time zones and calling codes, which packs do not carry, come from
    `default_rules`. Unknown countries get the packs' strictest combination.
    """
    cache: dict[tuple[str | None, Channel], SendRules] = {}

    def rules_for(country: str | None, channel: Channel) -> SendRules:
        key = ((country or "").upper() or None, channel)
        cached = cache.get(key)
        if cached is not None:
            return cached
        pack = pack_for(key[0], settings, packs=packs)
        base = default_rules(key[0], channel)
        windows = {p.value: pack.quiet_window(channel, p) for p in Purpose}
        hours: set[int] = set()
        for window in windows.values():
            if window is not None:
                hours |= window.hours()
        merged = covering_window(hours)
        consent = {p.value: pack.consent_requirements(channel, p) for p in Purpose}
        days = [c.valid_days for c in consent.values() if c.required and c.valid_days is not None]
        dnd_purposes = frozenset(p.value for p in Purpose if pack.needs_dnd_check(channel, p))
        template_rules = pack.sms_template_rules
        rules = SendRules(
            country=key[0] or FALLBACK.country,
            calling_code=base.calling_code,
            quiet_start_hour=merged.start_hour if merged is not None else None,
            quiet_end_hour=merged.end_hour if merged is not None else None,
            quiet_purposes=frozenset(p for p, w in windows.items() if w is not None),
            timezones=base.timezones,
            consent_exempt_purposes=frozenset(p for p, c in consent.items() if not c.required),
            consent_max_age=timedelta(days=min(days)) if days else None,
            dnd_channels=frozenset({channel}) if dnd_purposes else frozenset(),
            dnd_purposes=dnd_purposes,
            dnd_registry=pack.dnd_registry(),
            dlt_required=channel is Channel.SMS
            and template_rules is not None
            and template_rules.requires_registered_template,
            # Disclosures (unsubscribe links, opt-out lines) are content: the Critic checks them
            # against the same packs before an item is approved.
            email_unsubscribe_required=False,
        )
        cache[key] = rules
        return rules

    return rules_for


# --- quiet hours ---------------------------------------------------------------------------------


def _in_band(local: datetime, start: int, end: int) -> bool:
    hour = local.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _next_local_hour(t: datetime, zone: ZoneInfo, hour: int) -> datetime:
    local = t.astimezone(zone)
    day: date = local.date()
    candidate = datetime(day.year, day.month, day.day, hour, tzinfo=zone)
    if candidate <= local:
        nxt = day + timedelta(days=1)
        candidate = datetime(nxt.year, nxt.month, nxt.day, hour, tzinfo=zone)
    return candidate.astimezone(UTC)


def quiet_until(now: datetime, zones: Sequence[ZoneInfo], start: int, end: int) -> datetime | None:
    """None if `now` is outside quiet hours in every zone; else the first moment it is outside in all.

    Zones whose allowed windows never overlap would loop; after a few rounds the latest candidate is
    returned, and the send is checked again then (so a violation is impossible, only a delay).
    """
    t = now
    for _ in range(8):
        quiet = [z for z in zones if _in_band(t.astimezone(z), start, end)]
        if not quiet:
            return None if t == now else t
        t = max(_next_local_hour(t, z, end) for z in quiet)
    return t


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


# --- requests, records and results ---------------------------------------------------------------


class Recipient(Contract):
    address: str = Field(description="Phone number, email address, or the client tool's id")
    customer_id: str | None = None
    timezone: str | None = Field(default=None, description="IANA zone, e.g. 'Asia/Kolkata'")
    country: str | None = Field(default=None, description="ISO 3166 alpha-2; inferred from +CC if absent")


class SendPayload(Contract):
    body: str = ""
    subject: str | None = None
    template_name: str | None = Field(default=None, description="DLT or WhatsApp template")
    template_params: tuple[str, ...] = ()
    asset_url: str | None = None
    purpose: str = Field(default=PROMOTIONAL, description="promotional, transactional or service")
    dnd_category: str | None = Field(default=None, description="India NCPR preference category, 1-7")


class SendRequest(Contract):
    idempotency_key: str | None = None
    decision_id: str | None = None
    mandate_id: str | None = None
    channel: Channel
    recipient: Recipient
    payload: SendPayload
    consent_proof: ConsentProof | None = None
    send_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _has_key(self) -> SendRequest:
        if not (self.idempotency_key or self.decision_id):
            raise ValueError("a send needs an idempotency_key or a decision_id")
        return self

    @property
    def key(self) -> str:
        return self.idempotency_key or str(self.decision_id)


class SendStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    REFUSED = "refused"
    FAILED = "failed"
    """Every provider refused, through every retry round. Never the answer to the first POST
    when a queue is configured: that answer is `queued` with the retry time."""


class RefusalReason(StrEnum):
    """Stable reason codes. Consent problems use `consent.ConsentProblem`'s codes."""

    NO_PROVIDER = "no_provider"
    NO_ADDRESS = "no_address"
    MANDATE_MISSING = "mandate_missing"
    MANDATE_INVALID = "mandate_invalid"
    OPTED_OUT = "opted_out"
    DND_REGISTERED = "dnd_registered"
    DND_UNAVAILABLE = "dnd_registry_unavailable"
    DLT_TEMPLATE_MISSING = "dlt_template_missing"
    UNSUBSCRIBE_LINK_MISSING = "unsubscribe_link_missing"
    INVALID_MESSAGE = "invalid_message"
    ALREADY_SENT = "already_sent"
    NO_QUEUE = "no_queue"
    # deferrals (status queued)
    SCHEDULED = "scheduled"
    QUIET_HOURS = "quiet_hours"
    RATE_LIMITED_RECIPIENT = "rate_limited_recipient"
    RATE_LIMITED_PROVIDER = "rate_limited_provider"
    PROVIDER_ERROR = "provider_error"
    AMBIGUOUS_RETRY = "ambiguous_retry"


class SendAttempt(Contract):
    provider: str
    at: AwareDatetime
    accepted: bool
    provider_message_id: str | None = None
    error: str | None = None
    round: int = 0


class MessageRecord(Contract):
    """Everything the gateway knows about one idempotency key. Updated as a new frozen copy."""

    idempotency_key: str
    message_id: str
    decision_id: str
    customer_id: str
    channel: Channel
    address: str
    country: str = Field(description="The recipient's country as resolved (ISO alpha-2), '' if unknown")
    status: SendStatus
    reasons: tuple[str, ...] = ()
    request: SendRequest
    attempts: tuple[SendAttempt, ...] = ()
    rounds: int = Field(default=0, description="Retry rounds in which every provider refused")
    schedules: int = Field(default=0, description="How many times it was put on the queue")
    provider: str | None = None
    provider_message_id: str | None = None
    pinned_provider: str | None = Field(default=None, description="Set after an ambiguous error")
    next_attempt_at: AwareDatetime | None = None
    job_id: str | None = None
    delivery: str | None = Field(default=None, description="The latest delivery report, e.g. 'deliver'")
    created_at: AwareDatetime
    updated_at: AwareDatetime


@dataclass(frozen=True)
class SendOutcome:
    record: MessageRecord
    replayed: bool = False
    """True when the key was seen before: nothing was sent, this is the first result."""

    @property
    def status(self) -> SendStatus:
        return self.record.status

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.record.reasons


@dataclass(frozen=True)
class TelemetryEvent:
    """One step of one message, for logs, metrics and the UI's delivery view."""

    name: str
    at: datetime
    key: str
    channel: Channel
    status: str = ""
    provider: str | None = None
    reasons: tuple[str, ...] = ()
    attrs: Mapping[str, str | int | float | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class WebhookOutcome:
    provider: str
    events: list[Event]
    stored: int = 0
    suppressed: int = 0


class GatewayError(Exception):
    """A webhook the gateway will not take. `status_code` is the HTTP answer for it."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Provider:
    """One carrier for one channel. Providers are tried in the order given."""

    name: str
    adapter: ChannelAdapter


@dataclass(frozen=True)
class RetryPolicy:
    base: timedelta = timedelta(seconds=30)
    factor: float = 2.0
    cap: timedelta = timedelta(hours=1)
    max_rounds: int = 5
    jitter: float = 0.1

    def delay(self, rounds: int, key: str) -> timedelta:
        """The wait before retry round `rounds + 1`: exponential, capped, with jitter fixed by the key."""
        raw = min(self.cap.total_seconds(), self.base.total_seconds() * self.factor ** max(rounds - 1, 0))
        digest = hashlib.sha256(f"{key}\x1f{rounds}".encode()).digest()
        share = int.from_bytes(digest[:4], "big") / 2**32
        return timedelta(seconds=raw * (1 + self.jitter * share))


MandateCheck = Callable[[str, SendRequest, datetime], str | None]
"""(mandate_id, request, now) -> a refusal reason, or None if the mandate covers this send."""


# --- the ledger ----------------------------------------------------------------------------------


class MessageLedger:
    """Where message records live. The in-memory one below is the reference; a SQL one can follow."""

    def get(self, key: str) -> MessageRecord | None:
        raise NotImplementedError

    def put(self, record: MessageRecord) -> None:
        raise NotImplementedError

    def by_provider_message_id(self, provider_message_id: str) -> MessageRecord | None:
        raise NotImplementedError


class InMemoryMessageLedger(MessageLedger):
    def __init__(self) -> None:
        self._records: dict[str, MessageRecord] = {}
        self._by_pid: dict[str, str] = {}

    def get(self, key: str) -> MessageRecord | None:
        return self._records.get(key)

    def put(self, record: MessageRecord) -> None:
        self._records[record.idempotency_key] = record
        for attempt in record.attempts:
            if attempt.provider_message_id:
                self._by_pid[attempt.provider_message_id] = record.idempotency_key

    def by_provider_message_id(self, provider_message_id: str) -> MessageRecord | None:
        key = self._by_pid.get(provider_message_id)
        return None if key is None else self._records.get(key)

    def __len__(self) -> int:
        return len(self._records)


# --- one round of provider attempts --------------------------------------------------------------


class _RoundKind(StrEnum):
    SENT = "sent"
    PERMANENT = "permanent"
    AMBIGUOUS = "ambiguous"
    EXHAUSTED = "exhausted"
    RATE_LIMITED = "rate_limited"


@dataclass
class _Round:
    kind: _RoundKind
    attempts: list[SendAttempt] = field(default_factory=list)
    provider: str | None = None
    provider_message_id: str | None = None
    errors: list[str] = field(default_factory=list)
    retry_at: datetime | None = None
    limit: RefusalReason = RefusalReason.RATE_LIMITED_PROVIDER
    """For RATE_LIMITED: which limit said no."""


# --- the gateway ---------------------------------------------------------------------------------


class DispatchGateway:
    """See the module docstring. Thread-safe: one lock serialises sends, jobs and webhooks."""

    def __init__(
        self,
        providers: Mapping[Channel, Sequence[Provider]],
        *,
        consent: ConsentRegistry | None = None,
        dnd: DndRegistry | Mapping[str, DndRegistry] | None = None,
        rules: RulesFor = default_rules,
        limiter: RateLimiter | None = None,
        queue: JobQueue | None = None,
        ledger: MessageLedger | None = None,
        events: EventStore | None = None,
        require_mandate: bool = False,
        mandate_check: MandateCheck | None = None,
        retry: RetryPolicy | None = None,
        telemetry: Callable[[TelemetryEvent], None] | None = None,
        default_country: str = "IN",
        decision_purpose: str = PROMOTIONAL,
    ) -> None:
        self.providers: dict[Channel, tuple[Provider, ...]] = {c: tuple(p) for c, p in providers.items()}
        self.consent = consent
        self.dnd = dnd
        self.rules = rules
        # `is None`, not `or`: an empty limiter or ledger has len() 0 and would be replaced.
        self.limiter = limiter if limiter is not None else RateLimiter()
        self.queue = queue
        self.ledger = ledger if ledger is not None else InMemoryMessageLedger()
        self.events = events
        self.require_mandate = require_mandate
        self.mandate_check = mandate_check
        self.retry = retry or RetryPolicy()
        self._sink = telemetry
        self.telemetry_log: deque[TelemetryEvent] = deque(maxlen=10_000)
        self.telemetry_errors = 0
        """How many times the telemetry sink raised. Those events are still in `telemetry_log`."""
        self.default_country = default_country
        self.decision_purpose = decision_purpose
        self.suppressed: set[tuple[Channel, str]] = set()
        self.dispatcher: Dispatcher | None = None
        self._lock = threading.RLock()
        self._now: datetime | None = None
        self._decision_attempts: dict[str, list[SendAttempt]] = {}
        self._decision_addresses: dict[str, str] = {}
        """message id -> the address the gate normalised (with the recipient's country) for it."""
        self._decision_pins: dict[str, str] = {}
        for channel_providers in self.providers.values():
            for provider in channel_providers:
                if isinstance(provider.adapter, SmsAdapter) and provider.adapter._lookup is None:
                    provider.adapter._lookup = self.receipt_lookup

    # --- telemetry ---------------------------------------------------------------------------------

    def _emit(self, event: TelemetryEvent) -> None:
        """Record a telemetry event and hand it to the sink. A failing sink never touches the send.

        `_emit` runs in the middle of a send (after the carrier accepted, before the SEND event is
        stored), so a sink error that escaped would turn a delivered message into a FAIL and a retry."""
        self.telemetry_log.append(event)
        if self._sink is not None:
            try:
                self._sink(event)
            except Exception:  # P6 telemetry: a raising sink is logged and counted, never propagated
                self.telemetry_errors += 1
                _log.warning("telemetry sink failed for %s (%s)", event.name, event.key, exc_info=True)

    def _emit_record(self, name: str, record: MessageRecord, at: datetime, **attrs: AttrValue) -> None:
        self._emit(
            TelemetryEvent(
                name=name,
                at=at,
                key=record.idempotency_key,
                channel=record.channel,
                status=record.status.value,
                provider=record.provider,
                reasons=record.reasons,
                attrs=attrs,
            )
        )

    # --- addresses and rules -------------------------------------------------------------------------

    def _country(self, channel: Channel, address: str, explicit: str | None) -> str | None:
        if explicit:
            return explicit.upper()
        if channel in PHONE_CHANNELS and address.strip().startswith(("+", "00")):
            digits = normalize_msisdn(address)
            for code, country, length in _CALLING_CODES:
                if digits.startswith(code) and len(digits) == length:
                    return country
            return None
        return self.default_country if channel in PHONE_CHANNELS else None

    def _normalize(self, channel: Channel, address: str, rules: SendRules) -> str:
        """The comparable address, or '' when a phone number cannot be placed with certainty.

        A national-format number is read with the recipient country's calling code. For a country
        whose code (or national format) we do not know, guessing would text a stranger in another
        country, so the number must be international (+CC or 00CC) or the send is refused.
        """
        if channel in PHONE_CHANNELS and not address.strip().startswith(("+", "00")):
            code = rules.calling_code or _NATIONAL_CODES.get(rules.country.upper())
            if code is None:
                return ""
            return normalize_address(channel, address, code)
        return normalize_address(channel, address)

    def _zones(self, timezone: str | None, rules: SendRules) -> list[ZoneInfo]:
        own = _zone(timezone)
        if own is not None:
            return [own]
        zones = [z for z in (_zone(n) for n in rules.timezones) if z is not None]
        return zones or [ZoneInfo("UTC")]

    def _dnd_for(self, rules: SendRules) -> DndRegistry | None:
        """The DND registry for these rules: by register name, then by country, or the only one."""
        if self.dnd is None or isinstance(self.dnd, DndRegistry):
            return self.dnd
        for key in (rules.dnd_registry, rules.country):
            if key and key in self.dnd:
                return self.dnd[key]
        return None

    # --- the gate ----------------------------------------------------------------------------------

    def _hard_reasons(
        self,
        *,
        channel: Channel,
        address: str,
        rules: SendRules,
        purpose: str,
        payload: SendPayload | None,
        proof: ConsentProof | None,
        request: SendRequest | None,
        check_consent: bool,
        now: datetime,
    ) -> list[str]:
        """Every reason this message must not be sent at all (empty: it may be, maybe later).

        `request` is None on the decision path, where the goal's envelope stands in for a mandate;
        `payload` is None there too, as the content was approved and is rendered by the Dispatcher.
        """
        reasons: list[str] = []
        if not self.providers.get(channel):
            reasons.append(RefusalReason.NO_PROVIDER.value)
        if not address:
            reasons.append(RefusalReason.NO_ADDRESS.value)
            return reasons
        if request is not None:
            mandate_id = request.mandate_id
            if self.require_mandate and not mandate_id:
                reasons.append(RefusalReason.MANDATE_MISSING.value)
            if mandate_id and self.mandate_check is not None:
                problem = self.mandate_check(mandate_id, request, now)
                if problem:
                    reasons.append(problem)
        if (channel, address) in self.suppressed:
            reasons.append(RefusalReason.OPTED_OUT.value)
        if check_consent and purpose not in rules.consent_exempt_purposes:
            check = verify_against(
                self.consent,
                proof,
                channel=channel,
                purpose=purpose,
                recipient=address,
                now=now,
                max_age=rules.consent_max_age,
            )
            if not check.ok:
                reasons.append(check.reason)
        if channel in rules.dnd_channels and purpose in rules.dnd_purposes:
            registry = self._dnd_for(rules)
            if registry is None:
                reasons.append(RefusalReason.DND_UNAVAILABLE.value)
            elif registry.blocks(address, payload.dnd_category if payload is not None else None):
                reasons.append(RefusalReason.DND_REGISTERED.value)
        if payload is None:
            return reasons
        if channel is Channel.SMS and rules.dlt_required and not payload.template_name:
            reasons.append(RefusalReason.DLT_TEMPLATE_MISSING.value)
        if (
            channel is Channel.EMAIL
            and rules.email_unsubscribe_required
            and purpose == PROMOTIONAL
            and "unsubscribe" not in payload.body.lower()
        ):
            reasons.append(RefusalReason.UNSUBSCRIBE_LINK_MISSING.value)
        return reasons

    def _not_before(
        self,
        *,
        rules: SendRules,
        purpose: str,
        timezone: str | None,
        send_at: datetime | None,
        address: str,
        channel: Channel,
        now: datetime,
    ) -> tuple[datetime, RefusalReason] | None:
        """When a sendable message may go, if not now: its own time, quiet hours, a recipient limit."""
        if send_at is not None and send_at > now:
            return send_at, RefusalReason.SCHEDULED
        if rules.quiet_applies(purpose):
            assert rules.quiet_start_hour is not None and rules.quiet_end_hour is not None
            until = quiet_until(
                now, self._zones(timezone, rules), rules.quiet_start_hour, rules.quiet_end_hour
            )
            if until is not None:
                return until, RefusalReason.QUIET_HOURS
        wait = self.limiter.recipient_wait(_recipient_key(channel, address), now)
        if wait > timedelta(0):
            return now + wait, RefusalReason.RATE_LIMITED_RECIPIENT
        return None

    # --- provider attempts ---------------------------------------------------------------------------

    def _try_providers(
        self,
        channel: Channel,
        message: Message,
        address: str,
        now: datetime,
        *,
        pinned: str | None,
        round_no: int,
    ) -> _Round:
        """Try providers in order until one accepts; see the module docstring for when to stop."""
        providers = [p for p in self.providers.get(channel, ()) if pinned is None or p.name == pinned]
        if not providers:
            return _Round(_RoundKind.EXHAUSTED, errors=[f"no provider named {pinned!r}"])
        ready = [p for p in providers if self.limiter.provider_wait(p.name, channel, now) == timedelta(0)]
        if not ready:
            wait = min(self.limiter.provider_wait(p.name, channel, now) for p in providers)
            return _Round(_RoundKind.RATE_LIMITED, retry_at=now + wait)
        taken = self.limiter.take_recipient(_recipient_key(channel, address), now)
        if not taken.allowed:
            return _Round(
                _RoundKind.RATE_LIMITED,
                retry_at=taken.retry_at,
                limit=RefusalReason.RATE_LIMITED_RECIPIENT,
            )
        result = _Round(_RoundKind.EXHAUSTED)
        for provider in ready:
            if not self.limiter.take_provider(provider.name, channel, now).allowed:
                continue
            try:
                receipt = provider.adapter.send(message)
            except Exception as exc:  # we cannot tell whether it went: treat as ambiguous
                receipt = SendReceipt(accepted=False, error=f"ambiguous: {type(exc).__name__}: {exc}")
            error = None if receipt.accepted else (receipt.error or "not accepted")[:MAX_ERROR_LEN]
            result.attempts.append(
                SendAttempt(
                    provider=provider.name,
                    at=now,
                    accepted=receipt.accepted,
                    provider_message_id=receipt.provider_message_id,
                    error=error,
                    round=round_no,
                )
            )
            if receipt.accepted:
                result.kind = _RoundKind.SENT
                result.provider = provider.name
                result.provider_message_id = receipt.provider_message_id
                return result
            result.errors.append(f"{provider.name}: {error}")
            if is_permanent(error):
                result.kind = _RoundKind.PERMANENT
                result.provider = provider.name
                return result
            if is_ambiguous(error):
                result.kind = _RoundKind.AMBIGUOUS
                result.provider = provider.name
                return result
        return result

    # --- the request path ----------------------------------------------------------------------------

    def send(self, request: SendRequest, now: datetime) -> SendOutcome:
        """Gate, then send, queue or refuse one message. A repeated key returns the first result."""
        with self._lock:
            key = request.key
            existing = self.ledger.get(key)
            if existing is not None:
                self._emit_record("dispatch.replayed", existing, now)
                return SendOutcome(existing, replayed=True)
            channel = request.channel
            country = self._country(channel, request.recipient.address, request.recipient.country)
            rules = self.rules(country, channel)
            address = self._normalize(channel, request.recipient.address, rules)
            decision_id = request.decision_id or stable_id("dec", "gateway", key)
            record = MessageRecord(
                idempotency_key=key,
                message_id=(
                    message_id_for(request.decision_id)
                    if request.decision_id
                    else stable_id("msg", "gateway", key)
                ),
                decision_id=decision_id,
                customer_id=request.recipient.customer_id or address or key,
                channel=channel,
                address=address,
                country=country or "",
                status=SendStatus.QUEUED,
                request=request,
                created_at=now,
                updated_at=now,
            )
            self._emit_record("dispatch.received", record, now)
            record = self._process(record, rules, now)
            self.ledger.put(record)
            return SendOutcome(record)

    def get(self, key: str) -> MessageRecord | None:
        with self._lock:
            return self.ledger.get(key)

    def _process(self, record: MessageRecord, rules: SendRules, now: datetime) -> MessageRecord:
        req = record.request
        reasons = self._hard_reasons(
            channel=record.channel,
            address=record.address,
            rules=rules,
            purpose=req.payload.purpose,
            payload=req.payload,
            proof=req.consent_proof,
            request=req,
            check_consent=True,
            now=now,
        )
        if not reasons and self._decision_already_sent(record):
            reasons.append(RefusalReason.ALREADY_SENT.value)
        if reasons:
            return self._finish(record, SendStatus.REFUSED, tuple(reasons), now, "dispatch.refused")
        later = self._not_before(
            rules=rules,
            purpose=req.payload.purpose,
            timezone=req.recipient.timezone,
            send_at=req.send_at,
            address=record.address,
            channel=record.channel,
            now=now,
        )
        if later is not None:
            return self._defer(record, later[0], (later[1].value,), now)
        message = Message(
            message_id=record.message_id,
            decision_id=record.decision_id,
            customer_id=record.customer_id,
            channel=record.channel,
            # "+" makes the normalised number unambiguous: an adapter must not read "6591234567"
            # (Singapore) as a 10-digit national number and prefix its own default code.
            to=f"+{record.address}" if record.channel in PHONE_CHANNELS else req.recipient.address,
            subject=req.payload.subject,
            body=req.payload.body,
            asset_url=req.payload.asset_url,
            template_name=req.payload.template_name,
            template_params=req.payload.template_params,
            send_at=now,
        )
        result = self._try_providers(
            record.channel,
            message,
            record.address,
            now,
            pinned=record.pinned_provider,
            round_no=record.rounds,
        )
        if result.attempts:
            record = record.model_copy(
                update={"attempts": record.attempts + tuple(result.attempts), "updated_at": now}
            )
            self.ledger.put(record)  # every attempt is on the record before anything else happens
            for attempt in result.attempts:
                self._emit_record(
                    "dispatch.attempt",
                    record,
                    now,
                    provider_name=attempt.provider,
                    accepted=attempt.accepted,
                    error=attempt.error or "",
                )
        if result.kind is _RoundKind.SENT:
            record = record.model_copy(
                update={
                    "provider": result.provider,
                    "provider_message_id": result.provider_message_id,
                    "pinned_provider": None,
                }
            )
            record = self._finish(record, SendStatus.SENT, (), now, "dispatch.sent")
            self._store_event(record, EventType.SEND, now)
            return record
        if result.kind is _RoundKind.RATE_LIMITED:
            assert result.retry_at is not None
            return self._defer(record, result.retry_at, (result.limit.value,), now)
        if result.kind is _RoundKind.PERMANENT:
            return self._finish(
                record,
                SendStatus.REFUSED,
                (RefusalReason.INVALID_MESSAGE.value, *result.errors),
                now,
                "dispatch.refused",
            )
        if result.kind is _RoundKind.AMBIGUOUS:
            record = record.model_copy(update={"pinned_provider": result.provider})
        rounds = record.rounds + 1
        record = record.model_copy(update={"rounds": rounds})
        reason = (
            RefusalReason.AMBIGUOUS_RETRY
            if result.kind is _RoundKind.AMBIGUOUS
            else RefusalReason.PROVIDER_ERROR
        )
        if self.queue is not None and rounds < self.retry.max_rounds:
            at = now + self.retry.delay(rounds, record.idempotency_key)
            return self._defer(record, at, (reason.value, *result.errors), now)
        record = self._finish(
            record, SendStatus.FAILED, (reason.value, *result.errors), now, "dispatch.failed"
        )
        self._store_event(record, EventType.FAIL, now)
        return record

    def _decision_already_sent(self, record: MessageRecord) -> bool:
        """A decision id sent before, under another idempotency key or by the decision path."""
        if record.request.decision_id is None or self.events is None:
            return False
        if any(a.accepted for a in record.attempts):
            return False
        return any(e.type is EventType.SEND for e in self.events.for_decision(record.request.decision_id))

    def _finish(
        self, record: MessageRecord, status: SendStatus, reasons: tuple[str, ...], now: datetime, name: str
    ) -> MessageRecord:
        record = record.model_copy(
            update={
                "status": status,
                "reasons": reasons,
                "next_attempt_at": None,
                "job_id": None,
                "updated_at": now,
            }
        )
        self._emit_record(name, record, now)
        return record

    def _defer(
        self, record: MessageRecord, at: datetime, reasons: tuple[str, ...], now: datetime
    ) -> MessageRecord:
        if self.queue is None:
            # Nowhere to wait: a scheduled or quiet-hour message cannot be held, so it is refused.
            status = SendStatus.FAILED if record.attempts else SendStatus.REFUSED
            return self._finish(
                record, status, (*reasons, RefusalReason.NO_QUEUE.value), now, f"dispatch.{status.value}"
            )
        job_id = stable_id("job", GATEWAY_SEND_JOB, record.idempotency_key, record.schedules)
        self.queue.enqueue(
            Job(
                job_id=job_id,
                kind=GATEWAY_SEND_JOB,
                payload={"idempotency_key": record.idempotency_key, "channel": record.channel.value},
                run_at=at,
            )
        )
        record = record.model_copy(
            update={
                "status": SendStatus.QUEUED,
                "reasons": reasons,
                "next_attempt_at": at,
                "job_id": job_id,
                "schedules": record.schedules + 1,
                "updated_at": now,
            }
        )
        self._emit_record("dispatch.queued", record, now, until=at.isoformat())
        return record

    def _store_event(self, record: MessageRecord, etype: EventType, now: datetime) -> None:
        if self.events is None:
            return
        attrs: dict[str, AttrValue] = {
            "message_id": record.message_id,
            "idempotency_key": record.idempotency_key,
            "country": record.country,
        }
        if record.provider:
            attrs["provider"] = record.provider
        if record.provider_message_id:
            attrs["provider_message_id"] = record.provider_message_id
        if record.request.mandate_id:
            attrs["mandate_id"] = record.request.mandate_id
        if etype is EventType.SEND:
            event_id = send_event_id(record.message_id)
        else:
            attrs["error"] = "; ".join(record.reasons)[:MAX_ERROR_LEN]
            event_id = stable_id("ev", record.message_id, etype.value, now.isoformat())
        self.events.append(
            [
                Event(
                    event_id=event_id,
                    customer_id=record.customer_id,
                    type=etype,
                    ts=now,
                    source=SOURCE,
                    channel=record.channel,
                    decision_id=record.decision_id,
                    attrs=attrs,
                )
            ]
        )

    # --- queued work ---------------------------------------------------------------------------------

    def run_job(self, job: Job, now: datetime) -> MessageRecord | None:
        """Finish a `gateway_send` job: the whole gate runs again, then the send.

        A job that is no longer the record's current one (the record moved on, or was sent) does
        nothing: that is what keeps a duplicated or late job from sending twice.
        """
        with self._lock:
            key = str(job.payload.get("idempotency_key") or "")
            record = self.ledger.get(key)
            if record is None or record.status is not SendStatus.QUEUED or record.job_id != job.job_id:
                return record
            rules = self.rules(record.country or None, record.channel)
            record = self._process(record, rules, now)
            self.ledger.put(record)
            return record

    def run_due(
        self,
        now: datetime,
        *,
        worker: str = "gateway",
        limit: int = 1000,
        decisions: DecisionLog | None = None,
        states: StateStore | None = None,
    ) -> int:
        """Claim and run every due gateway job (both kinds); return how many ran."""
        if self.queue is None:
            return 0
        kinds = [GATEWAY_SEND_JOB] + ([GATEWAY_DECISION_JOB] if decisions and states else [])
        ran = 0
        while ran < limit:
            job = self.queue.claim(kinds, worker=worker, now=now)
            if job is None:
                break
            try:
                if job.kind == GATEWAY_SEND_JOB:
                    self.run_job(job, now)
                else:
                    assert decisions is not None and states is not None
                    self.run_decision_job(job, decisions, states, now)
            except Exception as exc:  # one bad job must not stop the rest; the queue keeps the error
                self.queue.fail(job.job_id, f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LEN])
            else:
                self.queue.complete(job.job_id)
            ran += 1
        return ran

    # --- webhooks ------------------------------------------------------------------------------------

    def provider(self, name: str) -> Provider | None:
        for channel_providers in self.providers.values():
            for provider in channel_providers:
                if provider.name == name:
                    return provider
        return None

    def receipt_lookup(self, provider_message_id: str) -> SentRef | None:
        """For adapters: which message a provider id belongs to, from the ledger."""
        record = self.ledger.by_provider_message_id(provider_message_id)
        if record is None:
            return None
        return SentRef(record.decision_id, record.customer_id, record.message_id)

    def suppress(self, channel: Channel, address: str, now: datetime) -> bool:
        """Never send to this address on this channel again (a STOP, a block, a complaint)."""
        with self._lock:
            new = (channel, address) not in self.suppressed
            self.suppressed.add((channel, address))
            if self.consent is not None:
                self.consent.revoke_recipient(channel, address, now)
            return new

    def handle_webhook(
        self, provider_name: str, headers: Mapping[str, str], body: bytes, now: datetime
    ) -> WebhookOutcome:
        """Verify, parse, store and act on one provider callback (STOP suppresses at once)."""
        provider = self.provider(provider_name)
        if provider is None:
            raise GatewayError(404, f"no provider named {provider_name!r}")
        if not provider.adapter.verify_webhook(headers, body):
            raise GatewayError(401, "the webhook signature does not match")
        stamped = {**dict(headers), RECEIVED_AT_HEADER: now.isoformat()}
        try:
            events = provider.adapter.parse_webhook(stamped, body)
        except ValueError as exc:
            raise GatewayError(400, str(exc)) from exc
        suppressed = 0
        with self._lock:
            for event in events:
                pid = event.attrs.get("provider_message_id")
                record = self.ledger.by_provider_message_id(pid) if isinstance(pid, str) else None
                if event.type in (EventType.UNSUBSCRIBE, EventType.BLOCK, EventType.COMPLAINT):
                    channel = event.channel or provider.adapter.channel
                    raw = event.attrs.get("address")
                    address = (
                        normalize_address(channel, raw)
                        if isinstance(raw, str) and raw
                        else (record.address if record is not None else "")
                    )
                    if address and self.suppress(channel, address, now):
                        suppressed += 1
                if record is not None and event.type is not EventType.SEND:
                    self.ledger.put(
                        record.model_copy(update={"delivery": event.type.value, "updated_at": now})
                    )
            stored = self.events.append(events) if self.events is not None else 0
        self._emit(
            TelemetryEvent(
                name="dispatch.webhook",
                at=now,
                key="",
                channel=provider.adapter.channel,
                provider=provider.name,
                attrs={"events": len(events), "suppressed": suppressed},
            )
        )
        return WebhookOutcome(provider.name, events, stored, suppressed)

    # --- the decision path: wrapping the Dispatcher ---------------------------------------------------

    def channel_adapters(self) -> dict[Channel, ChannelAdapter]:
        """One `FailoverAdapter` per channel, for a `Dispatcher` to send through."""
        return {channel: FailoverAdapter(self, channel) for channel in self.providers}

    def attach_dispatcher(
        self,
        address_book: AddressBook,
        workspace: Workspace,
        events: EventStore,
        *,
        profiles: ProfileBook | None = None,
    ) -> Dispatcher:
        """Build the Dispatcher the decision path wraps. It gets no queue: the gateway does the waiting.

        A WhatsApp Cloud provider adds its guard (approved templates, quality, Meta's holds)."""
        # v4 WhatsApp readiness (autonomy/channels/whatsapp_guard.py)
        guards = whatsapp_guards(p.adapter for p in self.providers.get(Channel.WHATSAPP, ()))
        self.dispatcher = Dispatcher(
            self.channel_adapters(), address_book, workspace, events, profiles=profiles, guards=guards
        )
        return self.dispatcher

    @contextmanager
    def _at(self, now: datetime) -> Iterator[None]:
        previous, self._now = self._now, now
        try:
            yield
        finally:
            self._now = previous

    def _send_decision_message(self, channel: Channel, message: Message) -> SendReceipt:
        """What a `FailoverAdapter` does: one round across providers, never twice for one message."""
        now = self._now
        if now is None:
            return SendReceipt(accepted=False, error="the gateway sends only inside dispatch()")
        with self._lock:
            done = next((a for a in self._decision_attempts.get(message.message_id, []) if a.accepted), None)
            if done is not None:
                return SendReceipt(accepted=True, provider_message_id=done.provider_message_id)
            address = self._decision_addresses.get(message.message_id) or normalize_address(
                channel, message.to
            )
            if channel in PHONE_CHANNELS:
                # The gate read the number with the recipient's country; the adapter must not re-read it.
                message = message.model_copy(update={"to": f"+{address}"})
            result = self._try_providers(
                channel,
                message,
                address,
                now,
                pinned=self._decision_pins.get(message.message_id),
                round_no=0,
            )
            self._decision_attempts.setdefault(message.message_id, []).extend(result.attempts)
            for attempt in result.attempts:
                self._emit(
                    TelemetryEvent(
                        name="dispatch.attempt",
                        at=now,
                        key=message.decision_id,
                        channel=channel,
                        provider=attempt.provider,
                        attrs={"accepted": attempt.accepted, "error": attempt.error or ""},
                    )
                )
            if result.kind is _RoundKind.SENT:
                self._decision_pins.pop(message.message_id, None)
                return SendReceipt(accepted=True, provider_message_id=result.provider_message_id)
            if result.kind is _RoundKind.AMBIGUOUS and result.provider:
                self._decision_pins[message.message_id] = result.provider
                return SendReceipt(accepted=False, error="ambiguous: " + "; ".join(result.errors))
            if result.kind is _RoundKind.RATE_LIMITED:
                return SendReceipt(accepted=False, error=result.limit.value)
            if result.kind is _RoundKind.PERMANENT:
                # Keep the prefix first, so `dispatch()` sees a permanent error and does not retry it.
                return SendReceipt(
                    accepted=False, error=f"{PERMANENT_ERROR_PREFIX} " + "; ".join(result.errors)
                )
            return SendReceipt(accepted=False, error="; ".join(result.errors) or "no provider accepted")

    def dispatch(
        self,
        decisions: Sequence[Decision],
        states: Mapping[str, CustomerState],
        now: datetime,
        *,
        _rounds: Mapping[str, int] | None = None,
    ) -> DispatchReport:
        """The gate for each ACT decision, then the Dispatcher for the ones that may go now.

        Deferred decisions (future `send_at`, quiet hours, a recipient limit) become
        `gateway_send_decision` jobs; failed sends are retried with backoff the same way.
        """
        if self.dispatcher is None:
            raise RuntimeError("call attach_dispatcher() before dispatching decisions")
        dispatcher = self.dispatcher
        report = DispatchReport()
        allowed: list[Decision] = []
        rounds = dict(_rounds or {})
        for decision in decisions:
            if decision.kind is not DecisionKind.ACT or decision.action is None:
                report.ignored += 1
                continue
            verdict = self._gate_decision(decision, states.get(decision.customer_id), dispatcher, now)
            if verdict is None:
                allowed.append(decision)
            elif isinstance(verdict, tuple):
                report.results.append(self._defer_decision(decision, verdict[0], verdict[1].value, 0, now))
            else:
                report.results.append(
                    DispatchResult(
                        decision_id=decision.decision_id,
                        customer_id=decision.customer_id,
                        status=DispatchStatus.SKIPPED,
                        reason=verdict[0],
                        detail=", ".join(verdict),
                    )
                )
        with self._at(now):
            inner = dispatcher.dispatch(allowed, states, now)
        for result in inner.results:
            if result.status is DispatchStatus.FAILED and not is_permanent(result.reason):
                done = rounds.get(result.decision_id, 0) + 1
                if self.queue is not None and done < self.retry.max_rounds:
                    at = now + self.retry.delay(done, result.decision_id)
                    decision = next(d for d in allowed if d.decision_id == result.decision_id)
                    report.results.append(
                        self._defer_decision(decision, at, RefusalReason.PROVIDER_ERROR.value, done, now)
                    )
                    continue
            report.results.append(result)
        report.events.extend(inner.events)
        report.ignored += inner.ignored
        for result in report.results:
            self._emit(
                TelemetryEvent(
                    name=f"dispatch.decision.{result.status.value}",
                    at=now,
                    key=result.decision_id,
                    channel=next(
                        (
                            d.action.channel
                            for d in decisions
                            if d.decision_id == result.decision_id and d.action is not None
                        ),
                        Channel.WEBHOOK,
                    ),
                    status=result.status.value,
                    reasons=(result.reason,) if result.reason else (),
                )
            )
        return report

    def _gate_decision(
        self,
        decision: Decision,
        state: CustomerState | None,
        dispatcher: Dispatcher,
        now: datetime,
    ) -> list[str] | tuple[datetime, RefusalReason] | None:
        """Refusal reasons, or (when, why) to defer, or None to hand it to the Dispatcher."""
        action = decision.action
        assert action is not None
        if state is None:
            return None  # the Dispatcher reports NO_STATE in its own words
        channel = action.channel
        raw = dispatcher.address_book(decision.customer_id, channel) or ""
        country = state.country or self._country(channel, raw, None)
        rules = self.rules(country, channel)
        if not raw:
            return None  # the Dispatcher reports NO_ADDRESS
        address = self._normalize(channel, raw, rules)
        if not address:
            return [RefusalReason.NO_ADDRESS.value]  # a number we cannot place: never guess
        with self._lock:
            self._decision_addresses[message_id_for(decision.decision_id)] = address
        reasons = self._hard_reasons(
            channel=channel,
            address=address,
            rules=rules,
            purpose=self.decision_purpose,
            payload=None,
            proof=None,
            request=None,
            # Without a registry, the consent flag in state (checked by the Dispatcher) is the check.
            check_consent=self.consent is not None,
            now=now,
        )
        if reasons:
            return reasons
        return self._not_before(
            rules=rules,
            purpose=self.decision_purpose,
            timezone=state.timezone,
            send_at=action.send_at,
            address=address,
            channel=channel,
            now=now,
        )

    def _defer_decision(
        self, decision: Decision, at: datetime, reason: str, rounds: int, now: datetime
    ) -> DispatchResult:
        if self.queue is None:
            return DispatchResult(
                decision_id=decision.decision_id,
                customer_id=decision.customer_id,
                status=DispatchStatus.SKIPPED,
                reason=reason,
                detail="no queue to wait in",
            )
        job_id = stable_id("job", GATEWAY_DECISION_JOB, decision.decision_id, rounds, at.isoformat())
        self.queue.enqueue(
            Job(
                job_id=job_id,
                kind=GATEWAY_DECISION_JOB,
                payload={
                    "decision_id": decision.decision_id,
                    "customer_id": decision.customer_id,
                    "round": rounds,
                },
                run_at=at,
            )
        )
        return DispatchResult(
            decision_id=decision.decision_id,
            customer_id=decision.customer_id,
            status=DispatchStatus.QUEUED,
            reason=reason,
            detail=f"until {at.isoformat()} (now {now.isoformat()})",
            message_id=message_id_for(decision.decision_id),
            job_id=job_id,
        )

    def run_decision_job(
        self, job: Job, decisions: DecisionLog, states: StateStore, now: datetime
    ) -> DispatchReport:
        """Finish a deferred decision: the gate and the Dispatcher's checks run again."""
        decision_id = str(job.payload.get("decision_id") or "")
        decision = decisions.get(decision_id)
        if decision is None:
            return DispatchReport(
                results=[
                    DispatchResult(
                        decision_id=decision_id,
                        customer_id=str(job.payload.get("customer_id") or ""),
                        status=DispatchStatus.SKIPPED,
                        reason="decision_not_found",
                    )
                ]
            )
        state = states.get(decision.customer_id)
        snapshot = {decision.customer_id: state} if state is not None else {}
        # The decision's own send_at has passed by now; a deferral must not re-defer on it.
        if decision.action is not None and decision.action.send_at > now:
            return DispatchReport(
                results=[self._defer_decision(decision, decision.action.send_at, "scheduled", 0, now)]
            )
        raw_round = job.payload.get("round")
        rounds = {decision_id: int(raw_round) if isinstance(raw_round, int | float) else 0}
        return self.dispatch([decision], snapshot, now, _rounds=rounds)


def _recipient_key(channel: Channel, address: str) -> str:
    return f"{channel.value}:{address}"


class FailoverAdapter:
    """A channel's providers, in order, as one `ChannelAdapter` for the Dispatcher.

    Sending goes through the gateway: rate limits, failover, and the never-twice rule apply. It
    only sends inside `DispatchGateway.dispatch()`, which supplies the time.
    """

    def __init__(self, gateway: DispatchGateway, channel: Channel) -> None:
        self.gateway = gateway
        self.channel = channel

    def send(self, message: Message) -> SendReceipt:
        return self.gateway._send_decision_message(self.channel, message)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        return any(
            p.adapter.verify_webhook(headers, body) for p in self.gateway.providers.get(self.channel, ())
        )

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[Event]:
        for provider in self.gateway.providers.get(self.channel, ()):
            if provider.adapter.verify_webhook(headers, body):
                return provider.adapter.parse_webhook(headers, body)
        return []
