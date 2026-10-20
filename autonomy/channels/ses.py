"""Amazon SES (the v2 API) as a channel: one email per decision out, SES events back in through SNS.

Why it is built this way:

- **The SES client is injected.** Anything with a `send_email(**kwargs)` method shaped like boto3's
  `sesv2` client will do, so tests use a recording stub and never touch the network. boto3 itself is
  imported only inside `boto3_sesv2_client()`, never at module import, so the light v2 core can load
  this module without boto3 installed.
- **Every email carries message tags** `decision_id`, `customer_id` and `message_id`. SES copies an
  email's tags into every event it later publishes about it (`mail.tags`), which is how a bounce or a
  click three days later is traced back to the decision that caused it, without a lookup table.
- **SES events arrive as SNS notifications** (an SES configuration set with an SNS event destination).
  `parse_webhook` unwraps the SNS envelope and maps each SES record (Send, Delivery, Bounce, Complaint,
  Open, Click, Subscription, Reject, Rendering Failure) to our `EventType`.
- **SNS signatures are checked by an injected verifier.** `verify_webhook` checks the envelope's
  structure, that the signing certificate URL is an `sns.<region>.amazonaws.com` HTTPS URL, and
  optionally the topic ARN; the cryptographic check is delegated to `verifier(message) -> bool`.
  Production MUST pass a verifier that downloads the certificate at `SigningCertURL` (after this
  module's host check), verifies the certificate chain, and checks `Signature` over
  `sns_string_to_sign(message)` with SHA1 (SignatureVersion 1) or SHA256 (SignatureVersion 2). With no
  verifier, every webhook is rejected: we fail closed rather than trust an unsigned body.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from autonomy.channels.common import header, send_event_id
from autonomy.contracts import Channel, Event, EventType, Message, SendReceipt, stable_id

SOURCE = "ses"
TAG_DECISION = "decision_id"
TAG_CUSTOMER = "customer_id"
TAG_MESSAGE = "message_id"
B32_SUFFIX = "_b32"
"""A tag whose value is not tag-safe is sent base32-encoded under `<name>_b32` (see `encode_tag`)."""

# SES message tag values may hold only ASCII letters, digits, '_' and '-', at most 256 characters.
_TAG_VALUE = re.compile(r"[A-Za-z0-9_\-]{1,256}")
_SNS_CERT_HOST = re.compile(r"sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?")

_SNS_REQUIRED: dict[str, tuple[str, ...]] = {
    "Notification": (
        "Message",
        "MessageId",
        "Timestamp",
        "TopicArn",
        "Signature",
        "SignatureVersion",
        "SigningCertURL",
    ),
    "SubscriptionConfirmation": (
        "Message",
        "MessageId",
        "Token",
        "SubscribeURL",
        "Timestamp",
        "TopicArn",
        "Signature",
        "SignatureVersion",
        "SigningCertURL",
    ),
    "UnsubscribeConfirmation": (
        "Message",
        "MessageId",
        "Token",
        "SubscribeURL",
        "Timestamp",
        "TopicArn",
        "Signature",
        "SignatureVersion",
        "SigningCertURL",
    ),
}

# SES event type -> (our event type, the key of the record's detail object).
_SES_EVENTS: dict[str, tuple[EventType | None, str]] = {
    "Send": (EventType.SEND, "send"),
    "Delivery": (EventType.DELIVER, "delivery"),
    "Bounce": (EventType.BOUNCE, "bounce"),
    "Complaint": (EventType.COMPLAINT, "complaint"),
    "Open": (EventType.OPEN, "open"),
    "Click": (EventType.CLICK, "click"),
    "Reject": (EventType.FAIL, "reject"),
    "Rendering Failure": (EventType.FAIL, "failure"),
    "Subscription": (None, "subscription"),  # UNSUBSCRIBE or PREFERENCE, decided per record
}

SnsVerifier = Callable[[Mapping[str, Any]], bool]
"""Checks an SNS message's signature against its signing certificate; True only if it is genuine."""


class SesV2Client(Protocol):
    """The one SESv2 call this module makes. A boto3 `sesv2` client satisfies it as it is."""

    def send_email(self, **kwargs: Any) -> Mapping[str, Any]:
        """`SendEmail`, keyword arguments exactly as boto3 takes them; returns at least `MessageId`."""
        ...


def boto3_sesv2_client(region_name: str) -> SesV2Client:
    """A real SESv2 client. boto3 is imported here and nowhere else, so the core never needs it."""
    import boto3

    client: SesV2Client = boto3.client("sesv2", region_name=region_name)
    return client


def encode_tag(name: str, value: str) -> tuple[str, str]:
    """The (name, value) of an SES message tag carrying `value` exactly.

    SES rejects tag values with characters other than letters, digits, '_' and '-'. Our ids are
    tag-safe, but a client's customer id may be an email address; that is sent as lowercase base32
    without padding under `<name>_b32`, and `decode_tags` reverses it.
    """
    if _TAG_VALUE.fullmatch(value):
        return name, value
    encoded = base64.b32encode(value.encode("utf-8")).decode("ascii").rstrip("=").lower()
    if len(encoded) > 256:
        raise ValueError(f"the {name} is too long to carry as an SES message tag")
    return name + B32_SUFFIX, encoded


def decode_tag(tags: Mapping[str, Any], name: str) -> str | None:
    """A tag's value from an SES event's `mail.tags` (name -> list of values), plain or base32."""
    plain = _first(tags.get(name))
    if plain is not None:
        return plain
    encoded = _first(tags.get(name + B32_SUFFIX))
    if encoded is None:
        return None
    padded = encoded.upper() + "=" * (-len(encoded) % 8)
    try:
        return base64.b32decode(padded).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def sns_string_to_sign(message: Mapping[str, Any]) -> str:
    """The canonical text SNS signs, per the AWS docs on verifying SNS message signatures.

    A production `SnsVerifier` checks `Signature` (base64) over this text with the certificate's
    public key: SHA1 for SignatureVersion "1", SHA256 for "2".
    """
    kind = message.get("Type")
    if kind == "Notification":
        keys: Sequence[str] = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
    else:
        keys = ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type")
    return "".join(f"{key}\n{message[key]}\n" for key in keys if message.get(key) is not None)


def trusted_sns_url(url: str) -> bool:
    """True for an HTTPS URL on an SNS endpoint (`sns.<region>.amazonaws.com`).

    Checked before anyone fetches `SigningCertURL` or `SubscribeURL`, so a forged body cannot make us
    download a certificate from, or confirm a subscription at, a host of the sender's choosing.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and _SNS_CERT_HOST.fullmatch(parts.hostname or "") is not None


class SesEmailAdapter:
    """Sends email through SESv2 and turns SES event notifications (via SNS) into our events."""

    channel: Channel = Channel.EMAIL

    def __init__(
        self,
        client: SesV2Client,
        from_address: str,
        configuration_set: str,
        *,
        verifier: SnsVerifier | None = None,
        topic_arns: Sequence[str] = (),
        unsubscribe_url: str | None = None,
        unsubscribe_mailto: str | None = None,
    ) -> None:
        """
        `unsubscribe_url` may hold `{decision_id}`, `{customer_id}` and `{message_id}`; an HTTPS URL
        also gets `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058, which Gmail and
        Yahoo require of bulk senders). The mailto defaults to the sender's own address. `topic_arns`,
        when given, is the allow-list of SNS topics we accept notifications from.
        """
        self._client = client
        self.from_address = from_address
        self.configuration_set = configuration_set
        self._verifier = verifier
        self._topic_arns = frozenset(topic_arns)
        self._unsubscribe_url = unsubscribe_url
        self._unsubscribe_mailto = unsubscribe_mailto or parseaddr(from_address)[1] or None

    @classmethod
    def from_boto3(
        cls,
        from_address: str,
        configuration_set: str,
        *,
        region_name: str,
        verifier: SnsVerifier | None = None,
        topic_arns: Sequence[str] = (),
        unsubscribe_url: str | None = None,
        unsubscribe_mailto: str | None = None,
    ) -> SesEmailAdapter:
        """An adapter on a real boto3 client (imports boto3)."""
        return cls(
            boto3_sesv2_client(region_name),
            from_address,
            configuration_set,
            verifier=verifier,
            topic_arns=topic_arns,
            unsubscribe_url=unsubscribe_url,
            unsubscribe_mailto=unsubscribe_mailto,
        )

    # --- sending -----------------------------------------------------------------------------------

    def build_request(self, message: Message) -> dict[str, Any]:
        """The SESv2 `SendEmail` keyword arguments for one message (a plain-text Simple email)."""
        if message.subject is None or not message.subject.strip():
            raise ValueError("an email needs a subject")
        tags = [
            encode_tag(TAG_DECISION, message.decision_id),
            encode_tag(TAG_CUSTOMER, message.customer_id),
            encode_tag(TAG_MESSAGE, message.message_id),
        ]
        simple: dict[str, Any] = {
            "Subject": {"Data": message.subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": message.body, "Charset": "UTF-8"}},
        }
        headers = self._unsubscribe_headers(message)
        if headers:  # sent only when there is something to send, never as an empty list
            simple["Headers"] = headers
        return {
            "FromEmailAddress": self.from_address,
            "Destination": {"ToAddresses": [message.to]},
            "Content": {"Simple": simple},
            "EmailTags": [{"Name": name, "Value": value} for name, value in tags],
            "ConfigurationSetName": self.configuration_set,
        }

    def _unsubscribe_headers(self, message: Message) -> list[dict[str, str]]:
        targets: list[str] = []
        if self._unsubscribe_mailto:
            subject = quote(f"unsubscribe {message.message_id}")
            targets.append(f"<mailto:{self._unsubscribe_mailto}?subject={subject}>")
        one_click = False
        if self._unsubscribe_url:
            url = (
                self._unsubscribe_url.replace("{decision_id}", quote(message.decision_id, safe=""))
                .replace("{customer_id}", quote(message.customer_id, safe=""))
                .replace("{message_id}", quote(message.message_id, safe=""))
            )
            targets.append(f"<{url}>")
            one_click = url.startswith("https://")
        if not targets:
            return []
        headers = [{"Name": "List-Unsubscribe", "Value": ", ".join(targets)}]
        if one_click:
            headers.append({"Name": "List-Unsubscribe-Post", "Value": "List-Unsubscribe=One-Click"})
        return headers

    def send(self, message: Message) -> SendReceipt:
        """Send one email. Failures come back as a refused receipt, never as an exception.

        The adapter is the boundary to a provider: whatever SES (or the network) raises is reported in
        `error` so the dispatcher can decide whether to retry, and nothing escapes into the loop.
        """
        if message.channel is not Channel.EMAIL:
            return SendReceipt(accepted=False, error=f"SES sends email, not {message.channel.value}")
        try:
            request = self.build_request(message)
        except ValueError as exc:
            return SendReceipt(accepted=False, error=str(exc))
        try:
            response = self._client.send_email(**request)
        except Exception as exc:  # the provider boundary: report it, let the dispatcher decide
            return SendReceipt(accepted=False, error=f"{type(exc).__name__}: {exc}")
        message_id = response.get("MessageId")
        if not isinstance(message_id, str) or not message_id:
            return SendReceipt(accepted=False, error="SES returned no MessageId")
        return SendReceipt(accepted=True, provider_message_id=message_id)

    # --- listening ---------------------------------------------------------------------------------

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        """True only for a well-formed SNS message from an allowed topic that the verifier vouches for."""
        doc = _json_object(body)
        if doc is None:
            return False
        kind = doc.get("Type")
        required = _SNS_REQUIRED.get(kind) if isinstance(kind, str) else None
        if required is None or any(not isinstance(doc.get(key), str) for key in required):
            return False
        header_kind = header(headers, "x-amz-sns-message-type")
        if header_kind is not None and header_kind != kind:
            return False
        if doc["SignatureVersion"] not in ("1", "2") or not trusted_sns_url(doc["SigningCertURL"]):
            return False
        if self._topic_arns and doc["TopicArn"] not in self._topic_arns:
            return False
        if self._verifier is None:
            return False
        try:
            return bool(self._verifier(doc))
        except Exception:  # a verifier that cannot decide (e.g. cannot fetch the cert) rejects
            return False

    def subscribe_url(self, body: bytes) -> str | None:
        """The URL to visit to confirm an SNS subscription, if this body asks for one.

        Only a `SubscriptionConfirmation` on a genuine SNS host is answered; the route that receives
        it (after `verify_webhook`) fetches the URL once to start receiving SES events.
        """
        doc = _json_object(body)
        if doc is None or doc.get("Type") != "SubscriptionConfirmation":
            return None
        url = doc.get("SubscribeURL")
        return url if isinstance(url, str) and trusted_sns_url(url) else None

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[Event]:  # noqa: ARG002
        """Our events for one SNS notification. Subscription confirmations carry none.

        Records without our `customer_id` tag were not sent by this system and are skipped. A body that
        is not an SNS JSON object raises ValueError.
        """
        doc = _json_object(body)
        if doc is None:
            raise ValueError("an SNS webhook body is a JSON object")
        if doc.get("Type") != "Notification":
            return []
        raw = doc.get("Message")
        record = _json_object(raw.encode("utf-8")) if isinstance(raw, str) else None
        if record is None:
            raise ValueError("an SNS notification's Message is a JSON object")
        return self._record_events(record, sns_timestamp=doc.get("Timestamp"))

    def _record_events(self, record: Mapping[str, Any], *, sns_timestamp: Any) -> list[Event]:
        kind = record.get("eventType") or record.get("notificationType")
        if not isinstance(kind, str) or kind not in _SES_EVENTS:
            return []  # DeliveryDelay and anything newer: nothing for the customer's history
        event_type, detail_key = _SES_EVENTS[kind]
        mail = _mapping(record.get("mail"))
        tags = _mapping(mail.get("tags"))
        found_customer = decode_tag(tags, TAG_CUSTOMER)
        if found_customer is None:
            return []
        customer_id: str = found_customer
        decision_id = decode_tag(tags, TAG_DECISION)
        detail = _mapping(record.get(detail_key))
        ts = _parse_iso(detail.get("timestamp") or mail.get("timestamp") or sns_timestamp)
        ses_id = str(mail.get("messageId") or "")
        attrs: dict[str, str | int | float | bool] = {"provider_message_id": ses_id}
        message_id = decode_tag(tags, TAG_MESSAGE)
        if message_id is not None:
            attrs["message_id"] = message_id

        def event(etype: EventType, *id_parts: str, **extra: str | int | float | bool) -> Event:
            if etype is EventType.SEND and message_id is not None:
                # The same id the dispatcher gives the SEND it records when SES accepts the message,
                # so the store keeps one SEND per message and contact counts are not doubled.
                event_id = send_event_id(message_id)
            else:
                event_id = stable_id("ev", SOURCE, ses_id, kind, ts.isoformat(), *id_parts)
            return Event(
                event_id=event_id,
                customer_id=customer_id,
                type=etype,
                ts=ts,
                source=SOURCE,
                channel=Channel.EMAIL,
                decision_id=decision_id,
                attrs={**attrs, **extra},
            )

        if kind == "Bounce":
            bounce_type = str(detail.get("bounceType") or "Undetermined")
            return [
                event(
                    EventType.BOUNCE,
                    bounce_type=bounce_type,
                    bounce_sub_type=str(detail.get("bounceSubType") or ""),
                    permanent=bounce_type == "Permanent",
                )
            ]
        if kind == "Complaint":
            return [event(EventType.COMPLAINT, complaint_type=str(detail.get("complaintFeedbackType") or ""))]
        if kind == "Click":
            link = str(detail.get("link") or "")
            return [event(EventType.CLICK, link, link=link)]
        if kind == "Reject":
            return [event(EventType.FAIL, reason=str(detail.get("reason") or "rejected"))]
        if kind == "Rendering Failure":
            return [event(EventType.FAIL, reason=str(detail.get("errorMessage") or "rendering failure"))]
        if kind == "Subscription":
            return _subscription_events(detail, event)
        return [event(event_type)] if event_type is not None else []


def _subscription_events(detail: Mapping[str, Any], event: Callable[..., Event]) -> list[Event]:
    """An unsubscribe-all is an UNSUBSCRIBE; a per-topic change is one PREFERENCE per topic."""
    new = _mapping(detail.get("newTopicPreferences"))
    source = str(detail.get("source") or "")
    if new.get("unsubscribeAll") is True:
        return [event(EventType.UNSUBSCRIBE, subscription_source=source)]
    out: list[Event] = []
    for topic in new.get("topicSubscriptionStatus") or []:
        if not isinstance(topic, Mapping):
            continue
        name = str(topic.get("topicName") or "")
        status = str(topic.get("subscriptionStatus") or "")
        out.append(
            event(
                EventType.PREFERENCE,
                name,
                key=f"email_topic:{name}",
                value=status,
                subscription_source=source,
            )
        )
    return out


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        doc = json.loads(body)
    except ValueError:  # includes JSONDecodeError and UnicodeDecodeError
        return None
    return doc if isinstance(doc, dict) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first(value: Any) -> str | None:
    """SES puts each tag's values in a list; a bare string is accepted too."""
    if isinstance(value, list) and value:
        value = value[0]
    return value if isinstance(value, str) and value else None


def _parse_iso(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("an SES event needs a timestamp")
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
