"""The WhatsApp Cloud API (Meta) as a channel: approved template messages out, statuses and replies in.

Why it is built this way:

- **Templates only.** A business may start a WhatsApp conversation only with a pre-approved template,
  so `send` posts a `template` message: the template's name, a language, and the rendered slot values
  as body parameters (plus an image header when the message has an `asset_url`).
- **The decision rides along with the message.** Meta echoes the `biz_opaque_callback_data` we set at
  send time in every status webhook for that message (sent, delivered, read, failed). We put a small
  JSON object there (`{"v":1,"d":decision_id,"c":customer_id,"m":message_id}`), so a status maps to
  its decision and customer without a lookup table.
- **Inbound messages do not carry it.** A reply knows only the sender's WhatsApp id (their phone
  number) and, when they replied to a specific message, that message's wamid (`context.id`). The
  adapter takes two optional lookups for those (`customer_lookup`, `decision_lookup`); without them a
  reply's customer id is the sender's WhatsApp id, for identity resolution to link later.
- **Webhooks are signed** with the app secret: `X-Hub-Signature-256: sha256=<HMAC-SHA256 of the raw
  body>`, compared in constant time. The one-off GET subscription check is `verify_subscription`.
- **Templates are checked before they are sent.** `sync_templates` reads the business account's
  templates (`GET /{waba_id}/message_templates`) into a `TemplateCatalogue`; once synced, `send`
  refuses a template that is not APPROVED and MARKETING/UTILITY before any request is made.
- **The number's health rides the webhooks.** `phone_number_quality_update` sets the quality rating
  and tier on `health` (and emits an account-level event the circuit breaker can read);
  `message_template_status_update` updates the catalogue. `get_phone_status` reads both on demand.
  A failure status with a code Meta explains (131049, 131026) carries what it means for the
  customer and the breakers (`whatsapp_health`).
- **An opt-out reply is an UNSUBSCRIBE at once.** A reply matching `configs/v4/optout_phrases.yaml`
  ("STOP", "band karo", "बंद करो", ...; see `autonomy.channels.optout` for how each language
  matches) yields an UNSUBSCRIBE and a CONSENT_REVOKED event next to the REPLY, so the gateway
  suppresses the number and revokes its consent proofs while handling the webhook, without waiting
  for the Conversation agent. The REPLY carries `opted_out` so that agent adds only the other
  channels' UNSUBSCRIBE events, never a second one for WhatsApp.
- **No network in tests.** The httpx transport is injectable (`httpx.MockTransport` in tests). httpx
  is imported when the first message is sent, so parsing and verifying webhooks need nothing but the
  standard library.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from autonomy.channels.common import header, send_event_id, str_or_none
from autonomy.channels.optout import OPTED_OUT_ATTR, OptOutLexicon, opt_out_phrase
from autonomy.channels.whatsapp_health import (
    ACCOUNT_CUSTOMER,
    ACCOUNT_LEVEL_ATTR,
    RATINGS,
    WhatsAppHealth,
    WhatsAppLimits,
    failure_attrs,
    load_limits,
)
from autonomy.channels.whatsapp_templates import (
    TemplateCatalogue,
    TemplateError,
    WhatsAppTemplate,
    parse_templates,
)
from autonomy.contracts import Alert, Channel, Event, EventType, Message, SendReceipt, stable_id

if TYPE_CHECKING:
    import httpx

SOURCE = "whatsapp"
GRAPH_URL = "https://graph.facebook.com"
OPAQUE_LIMIT = 512
"""Meta's limit on `biz_opaque_callback_data`, in characters."""
SIGNATURE_HEADER = "X-Hub-Signature-256"

_STATUS_TYPES: dict[str, EventType] = {
    "sent": EventType.SEND,
    "delivered": EventType.DELIVER,
    "read": EventType.OPEN,
    "failed": EventType.FAIL,
}
OPTED_OUT_ERROR_CODES: frozenset[int] = frozenset({131050})
"""A failure with these codes means the person stopped marketing messages from this business; it
becomes a BLOCK, so consent is withdrawn rather than the send merely retried."""

RECEIVED_AT_HEADER = "X-Autonomy-Received-At"
"""The gateway stamps the time it received a webhook here (see `gateway.Gateway.handle_webhook`)."""
OPT_OUT_REASON = "reply_optout_instant"
"""The reason on the UNSUBSCRIBE this adapter writes for an opt-out reply."""
MAX_TEMPLATE_PAGES = 50
"""Pages of 100 templates read before giving up (an account holds at most a few thousand)."""
_TEMPLATE_STATUS_EVENTS: dict[str, str] = {
    "APPROVED": "APPROVED",
    "REINSTATED": "APPROVED",
    "REJECTED": "REJECTED",
    "PAUSED": "PAUSED",
    "DISABLED": "DISABLED",
    "PENDING": "PENDING",
}
"""`message_template_status_update` events that change whether a template may be sent. FLAGGED and
IN_APPEAL leave it as it is (Meta still lets a flagged template go)."""
_QUALITY_EVENT_RATINGS: dict[str, str] = {"FLAGGED": "RED", "UNFLAGGED": "YELLOW"}
"""`phone_number_quality_update` events that imply a rating when the payload carries none. FLAGGED is
Meta's low-quality state; after UNFLAGGED we assume YELLOW until `get_phone_status` reads the real one."""

CustomerLookup = Callable[[str], str | None]
"""A WhatsApp id (the sender's phone number, digits only) -> our customer id, if we know them."""
DecisionLookup = Callable[[str], str | None]
"""A message's wamid -> the decision that sent it, if we kept the send receipt."""


def encode_opaque(decision_id: str, customer_id: str, message_id: str) -> str:
    """The `biz_opaque_callback_data` for one message: compact JSON, at most 512 characters.

    The message id is dropped first if it does not fit; the decision and customer ids must.
    """
    data = {"v": 1, "d": decision_id, "c": customer_id, "m": message_id}
    text = json.dumps(data, separators=(",", ":"))
    if len(text) > OPAQUE_LIMIT:
        del data["m"]
        text = json.dumps(data, separators=(",", ":"))
    if len(text) > OPAQUE_LIMIT:
        raise ValueError("decision and customer ids are too long for WhatsApp's callback data")
    return text


def decode_opaque(value: Any) -> tuple[str | None, str | None, str | None]:
    """(decision_id, customer_id, message_id) from callback data we set; Nones for anything else."""
    if not isinstance(value, str):
        return None, None, None
    try:
        data = json.loads(value)
    except ValueError:
        return None, None, None
    if not isinstance(data, dict) or data.get("v") != 1:
        return None, None, None
    return str_or_none(data.get("d")), str_or_none(data.get("c")), str_or_none(data.get("m"))


def sign_body(app_secret: str, body: bytes) -> str:
    """The `X-Hub-Signature-256` value Meta sends for this body."""
    return "sha256=" + hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class WhatsAppCloudAdapter:
    """Sends template messages through the Cloud API and reads its webhooks back as our events."""

    channel: Channel = Channel.WHATSAPP

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        app_secret: str,
        transport: httpx.BaseTransport | None = None,
        api_version: str = "v21.0",
        *,
        verify_token: str | None = None,
        language: str = "en",
        customer_lookup: CustomerLookup | None = None,
        decision_lookup: DecisionLookup | None = None,
        timeout: float = 10.0,
        waba_id: str | None = None,
        templates: TemplateCatalogue | None = None,
        health: WhatsAppHealth | None = None,
        limits: WhatsAppLimits | None = None,
        optout: OptOutLexicon | None = None,
        display_phone_number: str | None = None,
    ) -> None:
        """
        `language` is the template language code used for every message (the `Message` contract has
        no language of its own). `verify_token` is the token chosen when registering the webhook in
        the Meta app dashboard; without it the GET subscription check always fails.

        `waba_id` is the WhatsApp Business Account id, needed to list templates. `templates` and
        `health` are shared with the dispatcher's `WhatsAppGuard`; `limits` defaults to
        `configs/v4/whatsapp.yaml` and `optout` to `configs/v4/optout_phrases.yaml`.

        `display_phone_number` is this number as Meta shows it: quality webhooks name the number only
        that way, so with it a report about another number of the same business account is ignored.
        `get_phone_status` fills it in when it is not given.
        """
        self.phone_number_id = phone_number_id
        self._access_token = access_token
        self._app_secret = app_secret
        self._transport = transport
        self.api_version = api_version
        self._verify_token = verify_token
        self.language = language
        self._customer_lookup = customer_lookup
        self._decision_lookup = decision_lookup
        self._timeout = timeout
        self._client: httpx.Client | None = None
        self.waba_id = waba_id
        self.limits = limits if limits is not None else load_limits()
        self.templates = (
            templates
            if templates is not None
            else TemplateCatalogue(categories=self.limits.usable_categories, status=self.limits.usable_status)
        )
        self.health = health if health is not None else WhatsAppHealth(self.limits)
        self._optout = optout
        self.display_phone_number = _digits(display_phone_number)

    @property
    def messages_url(self) -> str:
        return f"{GRAPH_URL}/{self.api_version}/{self.phone_number_id}/messages"

    # --- sending -----------------------------------------------------------------------------------

    def build_payload(self, message: Message) -> dict[str, Any]:
        """The JSON body of one template message."""
        if not message.template_name:
            raise ValueError("a WhatsApp message needs an approved template_name")
        to = re.sub(r"\D", "", message.to)
        if not to:
            raise ValueError("a WhatsApp message needs a phone number")
        components: list[dict[str, Any]] = []
        if message.asset_url:
            components.append(
                {"type": "header", "parameters": [{"type": "image", "image": {"link": message.asset_url}}]}
            )
        if message.template_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": param} for param in message.template_params],
                }
            )
        template: dict[str, Any] = {"name": message.template_name, "language": {"code": self.language}}
        if components:
            template["components"] = components
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": template,
            "biz_opaque_callback_data": encode_opaque(
                message.decision_id, message.customer_id, message.message_id
            ),
        }

    def send(self, message: Message) -> SendReceipt:
        """Send one template message; the receipt's provider id is the wamid. Never raises."""
        if message.channel is not Channel.WHATSAPP:
            return SendReceipt(accepted=False, error=f"WhatsApp sends whatsapp, not {message.channel.value}")
        try:
            payload = self.build_payload(message)
        except ValueError as exc:
            return SendReceipt(accepted=False, error=str(exc))
        refusal = self.templates.refusal(message.template_name, self.language)
        if refusal:
            return SendReceipt(accepted=False, error=f"template_not_approved: {refusal}")
        try:
            response = self._http().post(
                self.messages_url,
                json=payload,
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
        except Exception as exc:  # the provider boundary (network, a bad URL, ...): report, never raise
            return SendReceipt(accepted=False, error=f"{type(exc).__name__}: {exc}")
        try:
            data: Any = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return SendReceipt(accepted=False, error=f"HTTP {response.status_code}: not a JSON object")
        error = data.get("error")
        if response.status_code >= 400 or error is not None:
            return SendReceipt(accepted=False, error=_graph_error(error, response.status_code))
        messages = data.get("messages")
        first = messages[0] if isinstance(messages, list) and messages else None
        wamid = first.get("id") if isinstance(first, dict) else None
        if not isinstance(wamid, str) or not wamid:
            return SendReceipt(accepted=False, error="the Cloud API returned no message id")
        return SendReceipt(accepted=True, provider_message_id=wamid)

    # --- templates and number health ---------------------------------------------------------------

    def list_templates(self) -> list[WhatsAppTemplate]:
        """Every message template of the business account, following the API's paging.

        Raises `TemplateError` for a response that is not the documented shape and `RuntimeError`
        for an HTTP or Graph error; it needs `waba_id`."""
        if not self.waba_id:
            raise RuntimeError("listing templates needs the WhatsApp Business Account id (waba_id)")
        url = f"{GRAPH_URL}/{self.api_version}/{self.waba_id}/message_templates"
        params: dict[str, str] | None = {
            "fields": "name,language,category,status,components",
            "limit": "100",
        }
        found: list[WhatsAppTemplate] = []
        for _page in range(MAX_TEMPLATE_PAGES):
            doc = self._graph_get(url, params)
            found.extend(parse_templates(doc))
            paging = doc.get("paging")
            following = str_or_none(paging.get("next")) if isinstance(paging, dict) else None
            if following is None:
                return found
            if not following.startswith(GRAPH_URL + "/"):
                raise TemplateError("the template list's next page is not on the Graph API")
            url, params = following, None  # the `next` link carries the parameters
        raise TemplateError(f"the template list has more than {MAX_TEMPLATE_PAGES} pages")

    def sync_templates(self, now: datetime) -> list[WhatsAppTemplate]:
        """Replace the catalogue with the account's templates as they are now; returns the usable ones."""
        self.templates.replace(self.list_templates(), at=now)
        return self.templates.usable()

    def get_phone_status(self, now: datetime) -> tuple[str | None, str | None]:
        """(quality rating, messaging-limit tier) of the number from the API, applied to `health`.

        Raises `RuntimeError` for an HTTP or Graph error."""
        doc = self._graph_get(
            f"{GRAPH_URL}/{self.api_version}/{self.phone_number_id}",
            {"fields": "quality_rating,messaging_limit_tier,display_phone_number"},
        )
        if self.display_phone_number is None:
            self.display_phone_number = _digits(str_or_none(doc.get("display_phone_number")))
        rating = str_or_none(doc.get("quality_rating"))
        tier = str_or_none(doc.get("messaging_limit_tier"))
        self.health.update(rating, tier, now)
        return rating, tier

    def drain_alerts(self) -> list[Alert]:
        """Alerts raised by quality reports since the last call (the number turned RED)."""
        return self.health.drain_alerts()

    def _graph_get(self, url: str, params: Mapping[str, str] | None) -> dict[str, Any]:
        try:
            response = self._http().get(
                url, params=params, headers={"Authorization": f"Bearer {self._access_token}"}
            )
        except Exception as exc:  # the provider boundary
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
        try:
            data: Any = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise RuntimeError(f"HTTP {response.status_code}: not a JSON object")
        if response.status_code >= 400 or data.get("error") is not None:
            raise RuntimeError(_graph_error(data.get("error"), response.status_code))
        return data

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

    # --- listening ---------------------------------------------------------------------------------

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        """True only if `X-Hub-Signature-256` is the app secret's HMAC-SHA256 of the raw body."""
        given = header(headers, SIGNATURE_HEADER)
        if given is None or not self._app_secret:
            return False
        return hmac.compare_digest(given.strip().encode("utf-8"), sign_body(self._app_secret, body).encode())

    def verify_subscription(self, params: Mapping[str, str]) -> str | None:
        """The answer to Meta's GET webhook check: the `hub.challenge` to echo, or None to refuse (403)."""
        if self._verify_token is None or params.get("hub.mode") != "subscribe":
            return None
        token = params.get("hub.verify_token", "")
        if not hmac.compare_digest(token.encode("utf-8"), self._verify_token.encode("utf-8")):
            return None
        return params.get("hub.challenge")

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[Event]:
        """Our events for one webhook delivery: a status per message status, a REPLY per inbound message.

        Changes for another phone number on the same business account are ignored, as are statuses we
        do not track (e.g. `deleted`) and statuses of messages no decision of ours sent (no callback
        data and no `decision_lookup` hit). A body that is not a JSON object raises ValueError.
        """
        try:
            doc = json.loads(body)
        except ValueError as exc:
            raise ValueError("a WhatsApp webhook body is a JSON object") from exc
        if not isinstance(doc, dict):
            raise ValueError("a WhatsApp webhook body is a JSON object")
        if doc.get("object") != "whatsapp_business_account":
            return []
        events: list[Event] = []
        for entry in _dicts(doc.get("entry")):
            for change in _dicts(entry.get("changes")):
                value = change.get("value")
                if not isinstance(value, dict):
                    continue
                field = change.get("field")
                if field == "phone_number_quality_update":
                    events.extend(self._quality_event(entry, value, headers))
                    continue
                if field == "message_template_status_update":
                    self._template_status(value)
                    continue
                if field != "messages":
                    continue
                metadata = value.get("metadata")
                number = metadata.get("phone_number_id") if isinstance(metadata, dict) else None
                if number is not None and str(number) != self.phone_number_id:
                    continue
                for status in _dicts(value.get("statuses")):
                    events.extend(self._status_event(status))
                for inbound in _dicts(value.get("messages")):
                    events.extend(self._reply_event(inbound))
        return events

    def _status_event(self, status: Mapping[str, Any]) -> list[Event]:
        state = status.get("status")
        event_type = _STATUS_TYPES.get(state) if isinstance(state, str) else None
        wamid = status.get("id")
        if event_type is None or not isinstance(wamid, str):
            return []
        decision_id, customer_id, message_id = decode_opaque(status.get("biz_opaque_callback_data"))
        recipient = str_or_none(status.get("recipient_id"))
        if decision_id is None and self._decision_lookup is not None:
            decision_id = self._decision_lookup(wamid)
        if decision_id is None:
            # Not a message this system sent (e.g. the client's own agents on the same number):
            # recording it would add contacts no decision made.
            return []
        if customer_id is None and recipient is not None:
            customer_id = self._customer(recipient)
        if customer_id is None:
            return []
        attrs: dict[str, str | int | float | bool] = {"provider_message_id": wamid}
        if message_id is not None:
            attrs["message_id"] = message_id
        pricing = status.get("pricing")
        if isinstance(pricing, dict) and isinstance(pricing.get("category"), str):
            attrs["pricing_category"] = pricing["category"]
        if event_type is EventType.FAIL:
            errors = _dicts(status.get("errors"))
            first = errors[0] if errors else {}
            code = first.get("code")
            if isinstance(code, int):
                attrs["error_code"] = code
                if code in OPTED_OUT_ERROR_CODES:
                    event_type = EventType.BLOCK
                attrs.update(failure_attrs(self.limits, code, _unix_ts(status.get("timestamp"))))
            attrs["error_title"] = str(first.get("title") or first.get("message") or "failed")
        if event_type is EventType.SEND and message_id is not None:
            # The dispatcher's id for the SEND it records on an accepted send: one SEND per message.
            event_id = send_event_id(message_id)
        else:
            event_id = stable_id("ev", SOURCE, wamid, state)
        return [
            Event(
                event_id=event_id,
                customer_id=customer_id,
                type=event_type,
                ts=_unix_ts(status.get("timestamp")),
                source=SOURCE,
                channel=Channel.WHATSAPP,
                decision_id=decision_id,
                attrs=attrs,
            )
        ]

    def _reply_event(self, inbound: Mapping[str, Any]) -> list[Event]:
        sender = str_or_none(inbound.get("from"))
        message_wamid = str_or_none(inbound.get("id"))
        if sender is None or message_wamid is None:
            return []
        kind = str(inbound.get("type") or "unknown")
        text, payload = _reply_text(inbound, kind)
        decision_id, customer_id, _ = decode_opaque(payload)
        context = inbound.get("context")
        replied_to = str_or_none(context.get("id")) if isinstance(context, dict) else None
        if decision_id is None and replied_to is not None and self._decision_lookup is not None:
            decision_id = self._decision_lookup(replied_to)
        attrs: dict[str, str | int | float | bool] = {
            "provider_message_id": message_wamid,
            "wa_id": sender,
            "message_type": kind,
        }
        if replied_to is not None:
            attrs["context_message_id"] = replied_to
        who = customer_id or self._customer(sender)
        ts = _unix_ts(inbound.get("timestamp"))
        phrase = opt_out_phrase(text, self._optout) if text else None
        if phrase is not None:
            attrs[OPTED_OUT_ATTR] = phrase  # the Conversation agent then skips this channel's UNSUBSCRIBE
        events = [
            Event(
                event_id=stable_id("ev", SOURCE, message_wamid, "reply"),
                customer_id=who,
                type=EventType.REPLY,
                ts=ts,
                source=SOURCE,
                channel=Channel.WHATSAPP,
                decision_id=decision_id,
                text=text,
                attrs=attrs,
            )
        ]
        if phrase is not None:
            events.extend(self._opt_out_events(who, sender, message_wamid, decision_id, ts, phrase))
        return events

    def _opt_out_events(
        self, customer_id: str, wa_id: str, wamid: str, decision_id: str | None, ts: datetime, phrase: str
    ) -> list[Event]:
        """The UNSUBSCRIBE and CONSENT_REVOKED for an opt-out reply. `address` is what the gateway
        suppresses and revokes consent proofs for."""
        attrs: dict[str, str | int | float | bool] = {
            "reason": OPT_OUT_REASON,
            "matched": phrase,
            "address": wa_id,
            "provider_message_id": wamid,
        }
        return [
            Event(
                event_id=stable_id("ev", SOURCE, wamid, kind.value),
                customer_id=customer_id,
                type=kind,
                ts=ts,
                source=SOURCE,
                channel=Channel.WHATSAPP,
                decision_id=decision_id,
                attrs=dict(attrs),
            )
            for kind in (EventType.UNSUBSCRIBE, EventType.CONSENT_REVOKED)
        ]

    def _quality_event(
        self, entry: Mapping[str, Any], value: Mapping[str, Any], headers: Mapping[str, str]
    ) -> list[Event]:
        """Apply a `phone_number_quality_update` to `health` and report it as an account-level event.

        The rating is the payload's own (`quality_rating` or `current_quality_rating`) when it has
        one, else implied by a FLAGGED or UNFLAGGED event; the tier is `current_limit`. A report
        naming another number than `display_phone_number` (when we know ours) is ignored. The event
        is marked `account_level`, so it is not folded into a customer state."""
        at = _entry_time(entry, headers)
        if at is None:
            return []
        number = _digits(str_or_none(value.get("display_phone_number")))
        if (
            self.display_phone_number is not None
            and number is not None
            and number != self.display_phone_number
        ):
            return []
        event = str(value.get("event") or "").upper()
        given = str_or_none(value.get("quality_rating") or value.get("current_quality_rating"))
        rating = given.upper() if given and given.upper() in RATINGS else _QUALITY_EVENT_RATINGS.get(event)
        tier = str_or_none(value.get("current_limit"))
        self.health.update(rating, tier, at)
        attrs: dict[str, str | int | float | bool] = {
            "key": "whatsapp_quality",
            "value": rating or "",
            ACCOUNT_LEVEL_ATTR: True,
        }
        if rating is not None:
            attrs["quality_rating"] = rating
        if tier is not None:
            attrs["messaging_limit_tier"] = tier
        if event:
            attrs["quality_event"] = event
        return [
            Event(
                event_id=stable_id("ev", SOURCE, "quality", at.isoformat(), event, rating or "", tier or ""),
                customer_id=ACCOUNT_CUSTOMER,
                type=EventType.PREFERENCE,
                ts=at,
                source=SOURCE,
                channel=Channel.WHATSAPP,
                attrs=attrs,
            )
        ]

    def _template_status(self, value: Mapping[str, Any]) -> None:
        """Apply a `message_template_status_update` to the catalogue."""
        name = str_or_none(value.get("message_template_name"))
        status = _TEMPLATE_STATUS_EVENTS.get(str(value.get("event") or "").upper())
        if name is not None and status is not None:
            self.templates.set_status(name, str_or_none(value.get("message_template_language")), status)

    def _customer(self, wa_id: str) -> str:
        if self._customer_lookup is not None:
            found = self._customer_lookup(wa_id)
            if found is not None:
                return found
        return wa_id


def _reply_text(inbound: Mapping[str, Any], kind: str) -> tuple[str | None, str | None]:
    """(the words the person sent, a quick-reply button's payload) for the message types with text."""
    if kind == "text":
        text = inbound.get("text")
        return (str_or_none(text.get("body")) if isinstance(text, dict) else None), None
    if kind == "button":
        button = inbound.get("button")
        if isinstance(button, dict):
            return str_or_none(button.get("text")), str_or_none(button.get("payload"))
        return None, None
    if kind == "interactive":
        interactive = inbound.get("interactive")
        if isinstance(interactive, dict):
            for key in ("button_reply", "list_reply"):
                reply = interactive.get(key)
                if isinstance(reply, dict):
                    return str_or_none(reply.get("title")), str_or_none(reply.get("id"))
        return None, None
    return None, None  # image, audio, location, ...: a reply without words


def _graph_error(error: Any, status_code: int) -> str:
    if isinstance(error, dict):
        code = error.get("code")
        text = str(error.get("message") or error.get("error_user_msg") or "error")
        if code is None or text.startswith(f"(#{code})"):
            return text
        return f"(#{code}) {text}"
    return f"HTTP {status_code}"


def _entry_time(entry: Mapping[str, Any], headers: Mapping[str, str]) -> datetime | None:
    """When a webhook entry happened: its own Unix `time`, else the gateway's received-at stamp."""
    raw = entry.get("time")
    if raw is not None:
        try:
            return datetime.fromtimestamp(int(raw), tz=UTC)
        except (TypeError, ValueError):
            return None
    stamped = header(headers, RECEIVED_AT_HEADER)
    if stamped:
        try:
            parsed = datetime.fromisoformat(stamped)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _digits(number: str | None) -> str | None:
    """A phone number's digits only ("+1 555-000 1111" -> "15550001111"); None if it has none."""
    digits = "".join(ch for ch in number or "" if ch.isdigit())
    return digits or None


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _unix_ts(value: Any) -> datetime:
    """Meta sends Unix seconds as a string."""
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"a WhatsApp webhook timestamp is Unix seconds, got {value!r}") from exc
