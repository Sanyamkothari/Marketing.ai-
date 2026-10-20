"""Handing decisions to the client's own tool: a signed webhook, or a CSV file.

Many clients already send through Klaviyo, Braze or HubSpot and want to keep doing so. For them we
decide and their tool sends. Both adapters here are `Channel.WEBHOOK` adapters:

- `WebhookToolAdapter` POSTs one JSON payload per message to a URL the client gives us (typically a
  flow trigger in their tool). The body is signed with a shared secret,
  `X-Autonomy-Signature: sha256=<HMAC-SHA256 of the raw body>`, so their side can check it came from
  us; `Idempotency-Key` is the message id, so a retried POST is not sent twice. The client's tool may
  call back with what happened (delivered, clicked, ...) in the small JSON format `parse_webhook`
  documents, signed the same way.
- `CsvExportAdapter` appends one row per message to a CSV file, for clients who import a file. What
  happened comes back later through their own data (the Data Doctor's import), not through webhooks.

The payload is the rendered `Message` (`model_dump(mode="json")`) plus a schema tag, serialised with
sorted keys so the same message always produces the same bytes and the same signature.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autonomy.channels.common import header, send_event_id, str_or_none
from autonomy.contracts import (
    CHANNEL_REQUIRED,
    Channel,
    Event,
    EventType,
    Message,
    SendReceipt,
    stable_id,
)

if TYPE_CHECKING:
    import httpx

SCHEMA = "autonomy.decision.v1"
SIGNATURE_HEADER = "X-Autonomy-Signature"
CALLBACK_TYPES: frozenset[EventType] = CHANNEL_REQUIRED
"""What a client's tool may report back: the lifecycle of a message we handed over, and consent."""

CSV_COLUMNS: tuple[str, ...] = (
    "message_id",
    "decision_id",
    "customer_id",
    "channel",
    "to",
    "subject",
    "body",
    "asset_url",
    "template_name",
    "template_params",
    "send_at",
)
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
_PHONE = re.compile(r"\+[0-9][0-9 ]*")
"""An international phone number such as +44 20 7946 0958: harmless, and an apostrophe would break it."""


def sign(secret: str, body: bytes) -> str:
    """The signature header value for a body: `sha256=<hex HMAC-SHA256>`."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signature_ok(secret: str, body: bytes, given: str | None) -> bool:
    """Constant-time check of a signature header against the body."""
    if given is None or not secret:
        return False
    return hmac.compare_digest(given.strip().encode("utf-8"), sign(secret, body).encode("utf-8"))


def decision_payload(message: Message) -> bytes:
    """The bytes we POST for one message: deterministic JSON, the same every time for the same message."""
    data: dict[str, Any] = {"schema": SCHEMA, **message.model_dump(mode="json")}
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class WebhookToolAdapter:
    """POSTs each message as signed JSON to the client's own tool, and reads its signed callbacks."""

    def __init__(
        self,
        url: str,
        secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        headers: Mapping[str, str] | None = None,
        source: str = "webhook",
    ) -> None:
        """
        `headers` are extra request headers the client's endpoint needs (e.g. their own API key).
        `source` names the tool on the events it reports, e.g. "klaviyo".
        """
        if not secret:
            raise ValueError("a webhook tool needs a signing secret")
        self.channel: Channel = Channel.WEBHOOK
        self.url = url
        self._secret = secret
        self._transport = transport
        self._timeout = timeout
        self._extra_headers = dict(headers or {})
        self.source = source
        self._client: httpx.Client | None = None

    def build_request(self, message: Message) -> tuple[bytes, dict[str, str]]:
        """The body and headers of the POST for one message."""
        body = decision_payload(message)
        headers = {
            **self._extra_headers,
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign(self._secret, body),
            "Idempotency-Key": message.message_id,
        }
        return body, headers

    def send(self, message: Message) -> SendReceipt:
        """Hand one message to the client's tool. Any 2xx is accepted; nothing raises."""
        body, headers = self.build_request(message)
        try:
            response = self._http().post(self.url, content=body, headers=headers)
        except Exception as exc:  # the provider boundary (network, a bad URL, ...): report, never raise
            return SendReceipt(accepted=False, error=f"{type(exc).__name__}: {exc}")
        if not 200 <= response.status_code < 300:
            return SendReceipt(
                accepted=False, error=f"HTTP {response.status_code}: {response.text[:200]}".strip()
            )
        provider_id = message.message_id
        try:
            data: Any = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"]:
            provider_id = data["id"]
        return SendReceipt(accepted=True, provider_message_id=provider_id)

    def _http(self) -> httpx.Client:
        if self._client is None:
            import httpx

            self._client = httpx.Client(transport=self._transport, timeout=self._timeout)
        return self._client

    def close(self) -> None:
        """Close the HTTP connection pool, if one was opened."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        """True only if the callback is signed with our shared secret, exactly as we sign sends."""
        return signature_ok(self._secret, body, header(headers, SIGNATURE_HEADER))

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[Event]:  # noqa: ARG002
        """The client's tool reporting what happened to messages we handed it.

        Body: `{"events": [{"type": "deliver", "customer_id": "...", "ts": "<ISO 8601>",
        "decision_id": "...", "message_id": "...", "event_id": "...", "text": "..."}]}` (a bare list
        or a single object also works). `type`, `customer_id` and `ts` are required; `type` is one of
        our event types for a message's lifecycle or consent. Anything else raises ValueError, so the
        sender gets a 400 and can fix its mapping rather than lose events silently.
        """
        try:
            doc = json.loads(body)
        except ValueError as exc:
            raise ValueError("a webhook tool callback is JSON") from exc
        rows = doc.get("events") if isinstance(doc, dict) and "events" in doc else doc
        rows = rows if isinstance(rows, list) else [rows]
        return [self._event(row) for row in rows]

    def _event(self, row: Any) -> Event:
        if not isinstance(row, dict):
            raise ValueError("each callback event is a JSON object")
        try:
            event_type = EventType(str(row.get("type")))
        except ValueError as exc:
            raise ValueError(f"unknown event type {row.get('type')!r}") from exc
        if event_type not in CALLBACK_TYPES:
            raise ValueError(f"a webhook tool cannot report {event_type.value} events")
        customer_id = row.get("customer_id")
        if not isinstance(customer_id, str) or not customer_id:
            raise ValueError("each callback event needs a customer_id")
        ts = _parse_iso(row.get("ts"))
        decision_id = str_or_none(row.get("decision_id"))
        message_id = str_or_none(row.get("message_id"))
        their_id = str_or_none(row.get("event_id"))
        key = their_id or message_id or decision_id or customer_id
        if event_type is EventType.SEND and message_id is not None:
            # The dispatcher already recorded this SEND when the tool accepted the message; the same
            # id makes the store keep one, so contact counts are not doubled.
            event_id = send_event_id(message_id)
        else:
            event_id = stable_id("ev", self.source, key, event_type.value, ts.isoformat())
        attrs: dict[str, str | int | float | bool] = {}
        if message_id is not None:
            attrs["message_id"] = message_id
        if their_id is not None:
            attrs["provider_event_id"] = their_id
        return Event(
            event_id=event_id,
            customer_id=customer_id,
            type=event_type,
            ts=ts,
            source=self.source,
            channel=self.channel,
            decision_id=decision_id,
            text=str_or_none(row.get("text")),
            attrs=attrs,
        )


class CsvExportAdapter:
    """Appends each message as a row of a CSV file the client imports into their own tool.

    The header row is written when the file is new or empty. Cells that a spreadsheet would run as a
    formula (starting with =, +, -, @, tab or carriage return) get a leading apostrophe unless
    `escape_formulas=False`: a customer's name filled into a slot must not become a formula on the
    marketer's laptop (OWASP "CSV injection"). A bare international phone number (`+44 20 7946 0958`)
    is left alone: it cannot run anything, and an apostrophe would break the client's import.
    """

    def __init__(self, path: Path | str, *, escape_formulas: bool = True) -> None:
        self.channel: Channel = Channel.WEBHOOK
        self.path = Path(path)
        self.escape_formulas = escape_formulas

    def row(self, message: Message) -> list[str]:
        """One message as CSV cells, in `CSV_COLUMNS` order."""
        cells = [
            message.message_id,
            message.decision_id,
            message.customer_id,
            message.channel.value,
            message.to,
            message.subject or "",
            message.body,
            message.asset_url or "",
            message.template_name or "",
            json.dumps(list(message.template_params), ensure_ascii=False) if message.template_params else "",
            message.send_at.isoformat(),
        ]
        return [self._escape(cell) for cell in cells]

    def _escape(self, cell: str) -> str:
        if self.escape_formulas and cell.startswith(_FORMULA_STARTS) and not _PHONE.fullmatch(cell):
            return "'" + cell
        return cell

    def send(self, message: Message) -> SendReceipt:
        """Append one row; the receipt's provider id is our message id. A file error is a refusal."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if new_file:
                    writer.writerow(CSV_COLUMNS)
                writer.writerow(self.row(message))
        except OSError as exc:
            return SendReceipt(accepted=False, error=f"{type(exc).__name__}: {exc}")
        return SendReceipt(accepted=True, provider_message_id=message.message_id)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # noqa: ARG002
        """A CSV export has no webhooks: results come back through the client's own data import."""
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[Event]:  # noqa: ARG002
        """Nothing: see `verify_webhook`."""
        return []


def _parse_iso(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("each callback event needs an ISO 8601 ts")
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
