"""SMS through a generic HTTP gateway, with India's DLT rules built in.

Why it is built this way:

- **India first.** Every commercial SMS to an Indian number must name the sender's registered
  principal entity (`entity_id`, the PE ID), a registered header (the 6-character sender id) and
  the registered content template (`dlt_template_id`). The operator compares the text against the
  template and drops anything that does not match. So a message here names a template
  (`Message.template_name`), the adapter looks it up in the `DltConfig`, checks the variables
  (how many, how long, of what kind) and *builds the text from the template*, so what we send is
  the registered text by construction. A message with no registered template is refused before
  any network call, with an `invalid:` error the gateway does not retry or fail over.
- **Segments cost money.** A text that fits the GSM-7 alphabet takes 160 characters in one SMS and
  153 per part once it is split (the rest of each part is the concatenation header); a single
  character outside it (an emoji, Devanagari, a curly quote) switches the whole message to UCS-2:
  70 characters, then 67 per part. `analyze()` says which, and how many parts, before we send.
  Characters from the GSM-7 extension table (`€ [ ] { } ^ ~ | \\`) take two septets.
- **One adapter, many gateways.** Providers differ in the shape of the request, not in what it
  says. A `SmsStyle` builds the HTTP request and reads the response and the callbacks; the
  adapter does everything else. Two are included: `TwilioStyle` (form-encoded, Basic auth,
  status callbacks with `MessageSid`/`MessageStatus`) and `Msg91Style` (JSON with `authkey`, DLT
  fields as `DLT_TE_ID`/`PE_ID`, JSON delivery reports). Neither claims to be byte-for-byte the
  vendor's API: they are the two common shapes, and the vendor-specific bits are parameters.
- **No network in tests.** The httpx transport is injectable (`httpx.MockTransport`); httpx is
  imported on the first send, so parsing webhooks needs only the standard library.
- **STOP means stop.** An inbound text whose first word is a stop keyword (STOP, UNSUBSCRIBE,
  CANCEL, END, QUIT, STOPALL, OPTOUT) becomes an UNSUBSCRIBE event carrying the sender's number in
  `attrs["address"]`, which the gateway turns into an immediate suppression. Other inbound texts
  are REPLY events.

Delivery reports carry the provider's message id only. The adapter remembers the ids it sent (and
takes an optional `lookup` for ids it did not, e.g. after a restart) so each report maps back to
its decision and customer; reports for messages we cannot place are dropped, as in the WhatsApp
adapter. A report without its own timestamp takes the time the gateway received it, from the
`X-Autonomy-Received-At` header the gateway adds.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from autonomy.channels.common import header, send_event_id
from autonomy.channels.consent import normalize_msisdn
from autonomy.compliance_packs.india import DLT_TAGS, DLT_VAR_MAX_CHARS
from autonomy.contracts import Channel, Event, EventType, Message, SendReceipt, stable_id

if TYPE_CHECKING:
    import httpx

    from autonomy.compliance_packs.base import DltRegistration

__all__ = [
    "DEFAULT_STOP_KEYWORDS",
    "PERMANENT_ERROR_PREFIX",
    "RECEIVED_AT_HEADER",
    "DltConfig",
    "DltError",
    "DltTemplate",
    "HttpRequest",
    "Msg91Style",
    "OutboundSms",
    "ProviderReport",
    "Segments",
    "SmsAdapter",
    "SmsEncoding",
    "SmsStyle",
    "TwilioStyle",
    "analyze",
    "fill_dlt",
    "sign_body",
]

PERMANENT_ERROR_PREFIX = "invalid:"
"""An adapter error starting with this is about the message itself: another provider or a retry
would fail the same way, so the gateway neither fails over nor retries it."""

RECEIVED_AT_HEADER = "X-Autonomy-Received-At"
"""Added by the gateway to each webhook: when we received it (ISO 8601), for reports without a time."""

DEFAULT_SIGNATURE_HEADER = "X-Signature"

DEFAULT_STOP_KEYWORDS: frozenset[str] = frozenset(
    {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"}
)


# --- encoding and segments -----------------------------------------------------------------------

GSM7_BASIC: frozenset[str] = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
"""The GSM 03.38 default alphabet (the escape character itself excluded): one septet each."""

GSM7_EXTENDED: frozenset[str] = frozenset("\f^{}\\[~]|€")
"""The GSM 03.38 extension table: an escape plus the character, so two septets each."""


class SmsEncoding(StrEnum):
    GSM7 = "gsm7"
    UCS2 = "ucs2"


@dataclass(frozen=True)
class Segments:
    """How a text travels: its encoding, its length in that encoding's units, and its parts."""

    encoding: SmsEncoding
    units: int
    """Septets for GSM-7 (extension characters count 2), UTF-16 code units for UCS-2."""
    count: int
    """Number of SMS parts the carrier bills; 0 for an empty text."""
    single_limit: int
    part_limit: int


def _is_gsm7(text: str) -> bool:
    return all(ch in GSM7_BASIC or ch in GSM7_EXTENDED for ch in text)


def _pack(costs: Sequence[int], single: int, part: int) -> int:
    """Parts needed when no character may be split across two parts."""
    total = sum(costs)
    if total == 0:
        return 0
    if total <= single:
        return 1
    parts, used = 1, 0
    for cost in costs:
        if used + cost > part:
            parts += 1
            used = 0
        used += cost
    return parts


def analyze(text: str) -> Segments:
    """GSM-7 or UCS-2, and how many parts (160/153 or 70/67 per part)."""
    if _is_gsm7(text):
        costs = [2 if ch in GSM7_EXTENDED else 1 for ch in text]
        return Segments(SmsEncoding.GSM7, sum(costs), _pack(costs, 160, 153), 160, 153)
    # UCS-2 counts UTF-16 code units; a character outside the BMP (most emoji) takes two, unsplit.
    costs = [2 if ord(ch) > 0xFFFF else 1 for ch in text]
    return Segments(SmsEncoding.UCS2, sum(costs), _pack(costs, 70, 67), 70, 67)


# --- India DLT -----------------------------------------------------------------------------------

# The tag list and the 30-character limit are the India pack's, so the Critic (which checks the
# template before approval) and this adapter (which fills it at send time) agree.
_SLOT = re.compile(r"\{#([^#{}]*)#\}")
_NUMERIC = re.compile(r"\d[\d.,]*")
_ALNUM = re.compile(r"[A-Za-z0-9]+")
_URL = re.compile(r"(https?://|www\.)\S+")
_CBN = re.compile(r"\+?\d[\d \-]{5,18}\d")


class DltError(ValueError):
    """A message that does not fit its registered template. `code` is a stable reason code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class DltTemplate:
    """One content template as registered on the DLT portal."""

    template_id: str
    text: str
    """Literal text with tagged slots: `{#var#}`, `{#numeric#}`, `{#alphanumeric#}`, `{#url#}`, `{#cbn#}`."""
    category: str = "promotional"
    """promotional, transactional, service_implicit or service_explicit, as registered."""
    header: str | None = None
    """The header this template is registered under, when it differs from the config's default."""
    var_max_chars: int = DLT_VAR_MAX_CHARS

    @property
    def slots(self) -> tuple[str, ...]:
        return tuple(m.group(1).strip().lower() for m in _SLOT.finditer(self.text))


@dataclass(frozen=True)
class DltConfig:
    """The sender's DLT registration: entity, default header, and templates by our template name."""

    entity_id: str
    header: str
    templates: Mapping[str, DltTemplate] = field(default_factory=dict)

    @classmethod
    def from_registrations(cls, registrations: Sequence[DltRegistration]) -> DltConfig:
        """The config for the brand's registrations as the India pack holds them (`PackSettings`).

        Templates are keyed by their DLT template id, which is the `template_name` the Critic and
        Studio check an India SMS against, so the text approved there is the text filled here. The
        first registration's header is the default; each template keeps its own. One brand has one
        principal entity, so registrations that disagree on it are refused rather than mixed.
        """
        if not registrations:
            raise ValueError("a DLT config needs at least one registered template")
        entities = {r.entity_id for r in registrations}
        if len(entities) > 1:
            raise ValueError(f"DLT registrations name more than one entity id: {sorted(entities)}")
        templates = {
            r.template_id: DltTemplate(
                template_id=r.template_id, text=r.text, category=r.purpose.value, header=r.header
            )
            for r in registrations
        }
        first = registrations[0]
        return cls(entity_id=first.entity_id, header=first.header, templates=templates)


def _valid(tag: str, value: str, max_chars: int) -> bool:
    if tag == "url":
        return _URL.fullmatch(value) is not None
    if tag == "cbn":
        return _CBN.fullmatch(value) is not None and len(value) <= 20
    if not value or len(value) > max_chars:
        return False
    if tag == "numeric":
        return _NUMERIC.fullmatch(value) is not None
    if tag == "alphanumeric":
        return _ALNUM.fullmatch(value) is not None
    return "\n" not in value and "\r" not in value


def fill_dlt(template: DltTemplate, params: Sequence[str]) -> str:
    """The registered text with each slot filled, or `DltError` naming what does not fit."""
    slots = template.slots
    unknown = sorted({s for s in slots if s not in DLT_TAGS})
    if unknown:
        raise DltError("dlt_template_invalid", f"unknown slot tags {unknown}")
    if len(params) != len(slots):
        raise DltError(
            "dlt_var_count", f"template {template.template_id} has {len(slots)} slots, got {len(params)}"
        )
    for i, (tag, value) in enumerate(zip(slots, params, strict=True), start=1):
        if tag in ("var", "numeric", "alphanumeric") and len(value) > template.var_max_chars:
            raise DltError(
                "dlt_var_too_long", f"slot {i} ({tag}) is {len(value)} chars, max {template.var_max_chars}"
            )
        if not _valid(tag, value, template.var_max_chars):
            raise DltError("dlt_var_type", f"slot {i} is not a valid {tag}")
    values = iter(params)
    return _SLOT.sub(lambda _m: next(values), template.text)


# --- what a style builds and reads ---------------------------------------------------------------


@dataclass(frozen=True)
class OutboundSms:
    """One SMS ready to go: the text is final and the DLT fields (if any) are filled in."""

    message_id: str
    decision_id: str
    customer_id: str
    to: str
    """Digits with country code, no '+'."""
    sender: str
    text: str
    segments: Segments
    entity_id: str | None = None
    dlt_template_id: str | None = None
    dlt_category: str | None = None


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    form: Mapping[str, str] | None = None
    json: Mapping[str, Any] | None = None


class ReportKind(StrEnum):
    STATUS = "status"
    INBOUND = "inbound"


@dataclass(frozen=True)
class ProviderReport:
    """One item of a provider callback, in provider-neutral words."""

    kind: ReportKind
    provider_message_id: str
    status: str = ""
    """For STATUS: sent, delivered, failed, blocked, or '' for states we do not track."""
    number: str = ""
    """The recipient (STATUS) or the sender (INBOUND), as digits."""
    text: str = ""
    error: str = ""
    ts: datetime | None = None


class SmsStyle(Protocol):
    """How one provider's HTTP API is shaped."""

    name: str
    signature_header: str

    def build(self, sms: OutboundSms) -> HttpRequest: ...

    def parse_response(self, status_code: int, body: bytes) -> SendReceipt: ...

    def parse_callback(self, headers: Mapping[str, str], body: bytes) -> list[ProviderReport]: ...


def _json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


def _parse_ts(value: Any, tz: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=ZoneInfo(tz))


class TwilioStyle:
    """Form-encoded `Messages.json`, Basic auth, `MessageSid`/`MessageStatus` callbacks.

    DLT fields go in as `EntityId` and `TemplateId` (rename with `dlt_fields` for a gateway that
    copies Twilio's shape but names them differently). Callbacks are verified with the adapter's
    shared-secret HMAC of the body; Twilio's own `X-Twilio-Signature` signs the callback URL plus
    the sorted form fields, so a real Twilio account needs its webhook routed through a signer
    that knows the public URL (not built here).
    """

    signature_header = DEFAULT_SIGNATURE_HEADER
    _STATUSES: Mapping[str, str] = {
        "sent": "sent",
        "delivered": "delivered",
        "undelivered": "failed",
        "failed": "failed",
    }
    BLOCKED_ERROR_CODES: frozenset[str] = frozenset({"21610"})
    """'Attempt to send to an unsubscribed recipient': the person said STOP at the carrier."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        *,
        base_url: str = "https://api.twilio.com",
        status_callback: str | None = None,
        dlt_fields: tuple[str, str] = ("EntityId", "TemplateId"),
        name: str = "twilio",
    ) -> None:
        self.account_sid = account_sid
        self._auth_token = auth_token
        self.base_url = base_url.rstrip("/")
        self.status_callback = status_callback
        self.dlt_fields = dlt_fields
        self.name = name

    def build(self, sms: OutboundSms) -> HttpRequest:
        form = {"To": f"+{sms.to}", "From": sms.sender, "Body": sms.text}
        if sms.entity_id:
            form[self.dlt_fields[0]] = sms.entity_id
        if sms.dlt_template_id:
            form[self.dlt_fields[1]] = sms.dlt_template_id
        if self.status_callback:
            form["StatusCallback"] = self.status_callback
        token = base64.b64encode(f"{self.account_sid}:{self._auth_token}".encode()).decode("ascii")
        return HttpRequest(
            method="POST",
            url=f"{self.base_url}/2010-04-01/Accounts/{self.account_sid}/Messages.json",
            headers={"Authorization": f"Basic {token}", "Idempotency-Key": sms.message_id},
            form=form,
        )

    def parse_response(self, status_code: int, body: bytes) -> SendReceipt:
        data = _json(body)
        if not isinstance(data, dict):
            return SendReceipt(accepted=False, error=f"HTTP {status_code}: not a JSON object")
        if status_code >= 400:
            return SendReceipt(
                accepted=False,
                error=f"HTTP {status_code}: {data.get('message', '')} (code {data.get('code')})",
            )
        sid = data.get("sid")
        if not isinstance(sid, str) or not sid:
            return SendReceipt(accepted=False, error="the gateway returned no message sid")
        return SendReceipt(accepted=True, provider_message_id=sid)

    def parse_callback(self, headers: Mapping[str, str], body: bytes) -> list[ProviderReport]:  # noqa: ARG002
        fields = {k: v[0] for k, v in parse_qs(body.decode("utf-8"), keep_blank_values=True).items()}
        sid = fields.get("MessageSid") or fields.get("SmsSid") or ""
        if not sid:
            return []
        status = fields.get("MessageStatus") or fields.get("SmsStatus") or ""
        if status and status != "received":
            code = fields.get("ErrorCode", "")
            canonical = "blocked" if code in self.BLOCKED_ERROR_CODES else self._STATUSES.get(status, "")
            return [
                ProviderReport(
                    kind=ReportKind.STATUS,
                    provider_message_id=sid,
                    status=canonical,
                    number=re.sub(r"\D", "", fields.get("To", "")),
                    error=f"error {code}" if code else "",
                )
            ]
        if "Body" in fields and fields.get("From"):
            return [
                ProviderReport(
                    kind=ReportKind.INBOUND,
                    provider_message_id=sid,
                    number=re.sub(r"\D", "", fields["From"]),
                    text=fields["Body"],
                )
            ]
        return []


class Msg91Style:
    """JSON with an `authkey` header and DLT fields `DLT_TE_ID`/`PE_ID`; JSON delivery reports.

    A delivery report body is a JSON list (or `{"data": [...]}`) of
    `{"requestId", "number", "status", "desc", "date"}`; an inbound text is
    `{"type": "inbound", "id", "mobile", "message"}`. Numeric statuses follow MSG91's codes for the
    ones we track (1 delivered, 2 failed, 8 sent, 16 rejected, 9/25/26 NDNC/DND).
    """

    signature_header = DEFAULT_SIGNATURE_HEADER
    _STATUSES: Mapping[str, str] = {
        "1": "delivered",
        "delivered": "delivered",
        "2": "failed",
        "failed": "failed",
        "16": "failed",
        "rejected": "failed",
        "8": "sent",
        "sent": "sent",
        "submitted": "sent",
        "9": "blocked",
        "25": "blocked",
        "26": "blocked",
        "ndnc": "blocked",
        "dnd": "blocked",
    }

    def __init__(
        self,
        auth_key: str,
        *,
        base_url: str = "https://control.msg91.com",
        route: str = "4",
        report_timezone: str = "Asia/Kolkata",
        name: str = "msg91",
    ) -> None:
        self._auth_key = auth_key
        self.base_url = base_url.rstrip("/")
        self.route = route
        self.report_timezone = report_timezone
        self.name = name

    def build(self, sms: OutboundSms) -> HttpRequest:
        payload: dict[str, Any] = {
            "sender": sms.sender,
            "route": self.route,
            "unicode": 1 if sms.segments.encoding is SmsEncoding.UCS2 else 0,
            "sms": [{"message": sms.text, "to": [sms.to]}],
            "CRQID": sms.message_id,
        }
        if sms.entity_id:
            payload["PE_ID"] = sms.entity_id
        if sms.dlt_template_id:
            payload["DLT_TE_ID"] = sms.dlt_template_id
        return HttpRequest(
            method="POST",
            url=f"{self.base_url}/api/v5/sms",
            headers={"authkey": self._auth_key},
            json=payload,
        )

    def parse_response(self, status_code: int, body: bytes) -> SendReceipt:
        data = _json(body)
        if not isinstance(data, dict):
            return SendReceipt(accepted=False, error=f"HTTP {status_code}: not a JSON object")
        message = data.get("message")
        if status_code >= 400 or data.get("type") != "success":
            return SendReceipt(accepted=False, error=f"HTTP {status_code}: {message}")
        if not isinstance(message, str) or not message:
            return SendReceipt(accepted=False, error="the gateway returned no request id")
        return SendReceipt(accepted=True, provider_message_id=message)

    def parse_callback(self, headers: Mapping[str, str], body: bytes) -> list[ProviderReport]:  # noqa: ARG002
        data = _json(body)
        if isinstance(data, dict) and data.get("type") == "inbound":
            pid, number, text = data.get("id"), data.get("mobile"), data.get("message")
            if not isinstance(pid, str) or not isinstance(number, str) or not isinstance(text, str):
                return []
            return [
                ProviderReport(
                    kind=ReportKind.INBOUND,
                    provider_message_id=pid,
                    number=re.sub(r"\D", "", number),
                    text=text,
                    ts=_parse_ts(data.get("date"), self.report_timezone),
                )
            ]
        rows = data.get("data") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return []
        out: list[ProviderReport] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("requestId"), str):
                continue
            raw = str(row.get("status", "")).strip().lower()
            out.append(
                ProviderReport(
                    kind=ReportKind.STATUS,
                    provider_message_id=row["requestId"],
                    status=self._STATUSES.get(raw, ""),
                    number=re.sub(r"\D", "", str(row.get("number", ""))),
                    error=(
                        str(row.get("desc", "")) if self._STATUSES.get(raw) in ("failed", "blocked") else ""
                    ),
                    ts=_parse_ts(row.get("date"), self.report_timezone),
                )
            )
        return out


# --- the adapter ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SentRef:
    """What we remember about a message the gateway accepted: enough to place its reports."""

    decision_id: str
    customer_id: str
    message_id: str


ReceiptLookup = Callable[[str], SentRef | None]
"""A provider message id -> the message it belongs to, for ids this process did not send."""
CustomerLookup = Callable[[str], str | None]
"""A phone number (digits with country code) -> our customer id, if we know them."""

_EVENT_TYPES: Mapping[str, EventType] = {
    "sent": EventType.SEND,
    "delivered": EventType.DELIVER,
    "failed": EventType.FAIL,
    "blocked": EventType.BLOCK,
}


def sign_body(secret: str, body: bytes) -> str:
    """The signature header value for a callback body: hex HMAC-SHA256 with the shared secret."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class SmsAdapter:
    """A `ChannelAdapter` for SMS through one HTTP gateway (see the module docstring)."""

    channel: Channel = Channel.SMS

    def __init__(
        self,
        style: SmsStyle,
        *,
        sender: str | None = None,
        dlt: DltConfig | None = None,
        transport: httpx.BaseTransport | None = None,
        webhook_secret: str | None = None,
        default_country_code: str = "91",
        max_segments: int = 6,
        stop_keywords: frozenset[str] = DEFAULT_STOP_KEYWORDS,
        lookup: ReceiptLookup | None = None,
        customer_lookup: CustomerLookup | None = None,
        timeout: float = 10.0,
    ) -> None:
        """
        `sender` is the sender id (a number, or an alphanumeric header); with `dlt` the header comes
        from the DLT registration instead. `max_segments` refuses texts that would be split into
        more parts than that (cost and readability both suffer beyond a few).
        """
        if sender is None and dlt is None:
            raise ValueError("an SMS adapter needs a sender id or a DLT registration")
        self.style = style
        self.name = style.name
        self.sender = sender
        self.dlt = dlt
        self._transport = transport
        self._webhook_secret = webhook_secret
        self.default_country_code = default_country_code
        self.max_segments = max_segments
        self.stop_keywords = stop_keywords
        self._lookup = lookup
        self._customer_lookup = customer_lookup
        self._timeout = timeout
        self._client: httpx.Client | None = None
        self._sent: dict[str, SentRef] = {}
        self._last_to: dict[str, SentRef] = {}
        self._lock = threading.Lock()

    # --- sending -----------------------------------------------------------------------------------

    def prepare(self, message: Message) -> OutboundSms:
        """The final SMS for a message, or `DltError` / `ValueError` saying why it cannot be sent."""
        to = normalize_msisdn(message.to, self.default_country_code)
        if not 8 <= len(to) <= 15:
            raise DltError("bad_number", f"{message.to!r} is not a phone number")
        entity_id = template_id = category = None
        sender = self.sender
        if self.dlt is not None:
            name = message.template_name
            template = self.dlt.templates.get(name) if name else None
            if template is None:
                raise DltError("dlt_template_unknown", f"no DLT template registered as {name!r}")
            text = fill_dlt(template, message.template_params)
            entity_id, template_id, category = self.dlt.entity_id, template.template_id, template.category
            sender = template.header or self.dlt.header
        else:
            text = message.body
        if not text:
            raise DltError("empty_text", "an SMS needs some text")
        segments = analyze(text)
        if segments.count > self.max_segments:
            raise DltError("too_long", f"{segments.count} parts, max {self.max_segments}")
        assert sender is not None  # the constructor requires a sender or a DLT header
        return OutboundSms(
            message_id=message.message_id,
            decision_id=message.decision_id,
            customer_id=message.customer_id,
            to=to,
            sender=sender,
            text=text,
            segments=segments,
            entity_id=entity_id,
            dlt_template_id=template_id,
            dlt_category=category,
        )

    def send(self, message: Message) -> SendReceipt:
        """Send one SMS; never raises. Problems with the message itself start with `invalid:`."""
        if message.channel is not Channel.SMS:
            return SendReceipt(
                accepted=False, error=f"{PERMANENT_ERROR_PREFIX} SMS sends sms, not {message.channel.value}"
            )
        try:
            sms = self.prepare(message)
        except DltError as exc:
            return SendReceipt(accepted=False, error=f"{PERMANENT_ERROR_PREFIX} {exc}")
        request = self.style.build(sms)
        try:
            response = self._http().request(
                request.method,
                request.url,
                headers=dict(request.headers),
                data=dict(request.form) if request.form is not None else None,
                json=dict(request.json) if request.json is not None else None,
            )
        except Exception as exc:  # the provider boundary: report, never raise
            return SendReceipt(accepted=False, error=f"{type(exc).__name__}: {exc}")
        receipt = self.style.parse_response(response.status_code, response.content)
        if receipt.accepted and receipt.provider_message_id:
            ref = SentRef(message.decision_id, message.customer_id, message.message_id)
            with self._lock:
                self._sent[receipt.provider_message_id] = ref
                self._last_to[sms.to] = ref
        return receipt

    def segments_for(self, message: Message) -> Segments:
        """How the message would travel, without sending it (raises like `prepare`)."""
        return self.prepare(message).segments

    def _http(self) -> httpx.Client:
        if self._client is None:
            import httpx

            self._client = httpx.Client(transport=self._transport, timeout=self._timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # --- listening ---------------------------------------------------------------------------------

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        """True only if the signature header is the shared secret's HMAC-SHA256 of the raw body."""
        given = header(headers, self.style.signature_header)
        if given is None or not self._webhook_secret:
            return False
        expected = sign_body(self._webhook_secret, body)
        return hmac.compare_digest(given.strip().lower().encode(), expected.encode())

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[Event]:
        """Delivery reports and inbound texts as our events (see the module docstring)."""
        received = _parse_ts(header(headers, RECEIVED_AT_HEADER), "UTC")
        events: list[Event] = []
        for report in self.style.parse_callback(headers, body):
            ts = report.ts or received
            if ts is None:
                raise ValueError(f"an SMS report without a time needs the {RECEIVED_AT_HEADER} header")
            if report.kind is ReportKind.STATUS:
                event = self._status_event(report, ts)
            else:
                event = self._inbound_event(report, ts)
            if event is not None:
                events.append(event)
        return events

    def _ref(self, provider_message_id: str) -> SentRef | None:
        with self._lock:
            ref = self._sent.get(provider_message_id)
        if ref is None and self._lookup is not None:
            ref = self._lookup(provider_message_id)
        return ref

    def _status_event(self, report: ProviderReport, ts: datetime) -> Event | None:
        etype = _EVENT_TYPES.get(report.status)
        ref = self._ref(report.provider_message_id)
        if etype is None or ref is None:
            return None
        attrs: dict[str, str | int | float | bool] = {
            "provider": self.name,
            "provider_message_id": report.provider_message_id,
            "message_id": ref.message_id,
        }
        if report.number:
            attrs["address"] = report.number
        if report.error:
            attrs["error"] = report.error[:500]
        event_id = (
            send_event_id(ref.message_id)
            if etype is EventType.SEND
            else stable_id("ev", "sms", report.provider_message_id, etype.value)
        )
        return Event(
            event_id=event_id,
            customer_id=ref.customer_id,
            type=etype,
            ts=ts,
            source=f"sms:{self.name}",
            channel=Channel.SMS,
            decision_id=ref.decision_id,
            attrs=attrs,
        )

    def is_stop(self, text: str) -> bool:
        words = re.findall(r"[A-Za-z]+", text)
        return bool(words) and words[0].upper() in self.stop_keywords

    def _inbound_event(self, report: ProviderReport, ts: datetime) -> Event:
        number = normalize_msisdn(report.number, self.default_country_code)
        customer_id = (self._customer_lookup(number) if self._customer_lookup else None) or number
        with self._lock:
            last = self._last_to.get(number)
        stop = self.is_stop(report.text)
        etype = EventType.UNSUBSCRIBE if stop else EventType.REPLY
        attrs: dict[str, str | int | float | bool] = {
            "provider": self.name,
            "provider_message_id": report.provider_message_id,
            "address": number,
        }
        if stop:
            attrs["keyword"] = re.findall(r"[A-Za-z]+", report.text)[0].upper()
        return Event(
            event_id=stable_id("ev", "sms", report.provider_message_id, etype.value),
            customer_id=customer_id,
            type=etype,
            ts=ts,
            source=f"sms:{self.name}",
            channel=Channel.SMS,
            decision_id=last.decision_id if last is not None else None,
            text=None if stop else report.text,
            attrs=attrs,
        )
