"""Klaviyo records as Autonomy events.

Two kinds of record come back from the connector's reads:

* **Profiles** (`GET /api/profiles?additional-fields[profile]=subscriptions`) feed the consent stream.
  Each profile gives a `SIGNUP` (its creation, carrying country and time zone for send timing) and,
  per channel, the consent state as `CONSENT_GRANTED` / `CONSENT_REVOKED`. A suppression (hard bounce,
  spam complaint, unsubscribe, manual) revokes. `NEVER_SUBSCRIBED` grants nothing unless the
  connection's `email_consent` is `can_receive` (US shops that email implied-consent customers).
  The event id includes the state and its timestamp, so a re-read is free and a change is a new event.
* **Events** (`GET /api/events?filter=equals(metric_id,...)`) feed the engagement and behaviour
  streams. Klaviyo's message metrics become our outbound types (Received → DELIVER, Opened → OPEN,
  Clicked → CLICK, Bounced → BOUNCE, Marked as Spam → COMPLAINT, Unsubscribed → UNSUBSCRIBE,
  Dropped / Failed to Deliver → FAIL); browsing metrics become VIEW / CART / VISIT. The Klaviyo event
  id is the row key.

**Linking engagement to decisions.** Klaviyo does not copy a trigger event's properties onto the
message events of the flow it starts, so a "Received Email" cannot name our decision. It does carry
`$flow`: the connector marks events from Autonomy's own send flows (`_ours`), and a `HandoffLookup`
(`autonomy.channels.klaviyo.EventStoreHandoffs`) names the decision that was handed to Klaviyo for
that customer and channel most recently before the event. Events from the shop's own campaigns and
flows are never linked (they are engagement with *their* messages, still useful for learning).

**Which customer.** Autonomy's customer id for a profile follows the connection's `identity`
setting. `auto` (the default) takes the profile's `external_id` first (Klaviyo's Shopify
integration keys synced profiles by the Shopify customer id, which is the id the Shopify connector
uses), then the lower-cased email, then the phone, then `klaviyo:<profile id>`. `email` skips the
external id; `profile_id` always uses `klaviyo:<profile id>`. Pick one per deployment and keep it:
the id is part of every event.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any, Final

from autonomy.connectors.klaviyo.api import parse_klaviyo_time
from autonomy.contracts import Channel, EventType
from autonomy.data.mapping import EventMapping

__all__ = [
    "BEHAVIOUR_METRICS",
    "ENGAGEMENT_METRICS",
    "KLAVIYO_MAPPING",
    "HandoffLookup",
    "customer_for",
    "event_rows",
    "profile_rows",
]

HandoffLookup = Callable[[str, Channel, datetime], str | None]
"""(customer id, channel, when the event happened) -> the decision handed to Klaviyo for it, or None."""

ENGAGEMENT_METRICS: Final[Mapping[str, tuple[EventType, Channel]]] = {
    # Klaviyo's built-in message metrics (names compared case-insensitively).
    "received email": (EventType.DELIVER, Channel.EMAIL),
    "opened email": (EventType.OPEN, Channel.EMAIL),
    "clicked email": (EventType.CLICK, Channel.EMAIL),
    "bounced email": (EventType.BOUNCE, Channel.EMAIL),
    "marked email as spam": (EventType.COMPLAINT, Channel.EMAIL),
    "unsubscribed": (EventType.UNSUBSCRIBE, Channel.EMAIL),
    "unsubscribed from email marketing": (EventType.UNSUBSCRIBE, Channel.EMAIL),
    "dropped email": (EventType.FAIL, Channel.EMAIL),
    "received sms": (EventType.DELIVER, Channel.SMS),
    "clicked sms": (EventType.CLICK, Channel.SMS),
    "failed to deliver sms": (EventType.FAIL, Channel.SMS),
    "unsubscribed from sms": (EventType.UNSUBSCRIBE, Channel.SMS),
    "unsubscribed from sms marketing": (EventType.UNSUBSCRIBE, Channel.SMS),
}
"""Message metrics → our outbound event types. `Placed Order` is deliberately absent: the shop's
orders come from the commerce connector, and counting Klaviyo's copy too would double revenue."""

BEHAVIOUR_METRICS: Final[Mapping[str, EventType]] = {
    "viewed product": EventType.VIEW,
    "added to cart": EventType.CART,
    "started checkout": EventType.CART,
    "active on site": EventType.VISIT,
}

SETUP_PROFILE: Final[str] = "autonomy-setup"
"""`setup.SETUP_PROFILE_EXTERNAL_ID` (not imported: setup depends on the client, mapping on nothing)."""

_ATTR_COLUMNS: Final[tuple[str, ...]] = (
    "provider_event_id",
    "provider",
    "metric",
    "profile_id",
    "flow_id",
    "flow_message_id",
    "via_autonomy",
    "url",
    "bounce_type",
    "reason",
    "consent_status",
    "consent_method",
    "country",
    "timezone",
)

KLAVIYO_MAPPING: Final[EventMapping] = EventMapping(
    customer_id="customer",
    timestamp="ts",
    event_type_column="type",
    channel_column="channel",
    decision_id_column="decision_id",
    category_column="category",
    item_column="item",
    attrs={name: name for name in _ATTR_COLUMNS},
    fixed_attrs={"provider": "klaviyo"},
    row_key=("row_id",),
)
"""One mapping for every row the flatteners below produce (they already speak our type names)."""

Row = dict[str, Any]


def customer_for(profile_id: str | None, attributes: Mapping[str, Any], identity: str = "auto") -> str | None:
    """Autonomy's customer id for a Klaviyo profile, by the connection's `identity` setting."""
    email = _text(attributes.get("email"))
    phone = _text(attributes.get("phone_number"))
    external = _text(attributes.get("external_id"))
    fallback = f"klaviyo:{profile_id}" if profile_id else None
    if identity == "profile_id":
        return fallback
    if identity in ("auto", "external_id") and external:
        return external
    if email:
        return email.lower()
    return phone or fallback


# --- profiles -----------------------------------------------------------------------------------


def profile_rows(
    record: Mapping[str, Any], *, identity: str = "auto", email_consent: str = "subscribed"
) -> list[Row]:
    """A profile as a SIGNUP row plus one consent row per channel whose state is known."""
    profile_id = _text(record.get("id"))
    attributes = _mapping(record.get("attributes"))
    customer = customer_for(profile_id, attributes, identity)
    if profile_id is None or customer is None or _text(attributes.get("external_id")) == SETUP_PROFILE:
        return []  # nobody, or the id-only profile Autonomy's send setup created (not a customer)
    created = parse_klaviyo_time(attributes.get("created"))
    updated = parse_klaviyo_time(attributes.get("updated")) or created
    location = _mapping(attributes.get("location"))
    country, timezone = _text(location.get("country")), _text(location.get("timezone"))
    rows: list[Row] = []
    if created is not None:
        rows.append(
            _row(
                f"{profile_id}:signup:{timezone or ''}:{country or ''}",
                customer,
                created,
                EventType.SIGNUP,
                profile_id=profile_id,
                country=country,
                timezone=timezone,
            )
        )
    subscriptions = _mapping(attributes.get("subscriptions"))
    email = _mapping(_mapping(subscriptions.get("email")).get("marketing"))
    if email:
        rows.extend(
            _consent_rows(
                profile_id,
                customer,
                Channel.EMAIL,
                email,
                can_receive=email.get("can_receive_email_marketing"),
                implied_ok=email_consent == "can_receive",
                fallback_ts=updated,
            )
        )
    sms = _mapping(_mapping(subscriptions.get("sms")).get("marketing"))
    if sms:
        rows.extend(
            _consent_rows(
                profile_id,
                customer,
                Channel.SMS,
                sms,
                can_receive=sms.get("can_receive_sms_marketing"),
                implied_ok=False,  # SMS marketing always needs express consent (TCPA, PECR)
                fallback_ts=updated,
            )
        )
    return rows


def _consent_rows(
    profile_id: str,
    customer: str,
    channel: Channel,
    block: Mapping[str, Any],
    *,
    can_receive: object,
    implied_ok: bool,
    fallback_ts: datetime | None,
) -> list[Row]:
    status = (_text(block.get("consent")) or "").upper()
    suppressions = [s for s in _list(block.get("suppression")) if isinstance(s, Mapping)]
    # The state is as of its last change: `last_updated` ("when a field on the subscription object
    # was last modified") first. `consent_timestamp` can stay at the original opt-in when a person
    # unsubscribes or a suppression is lifted, which would stamp the new state with the old one's
    # time (same event id, dropped as a repeat; or a tie that a time-ordered rebuild may resolve to
    # the older state).
    when = (
        parse_klaviyo_time(block.get("last_updated"))
        or parse_klaviyo_time(block.get("consent_timestamp"))
        or fallback_ts
    )
    receivable = can_receive is True
    reason: str | None = None
    if suppressions:
        latest = max(suppressions, key=lambda s: parse_klaviyo_time(s.get("timestamp")) or _EPOCH)
        kind = EventType.CONSENT_REVOKED
        reason = _text(latest.get("reason")) or "suppressed"
        suppressed_at = parse_klaviyo_time(latest.get("timestamp"))
        if suppressed_at is not None and (when is None or suppressed_at > when):
            when = suppressed_at  # a revocation is never stamped earlier than the state it replaces
    elif status == "UNSUBSCRIBED" or (status == "SUBSCRIBED" and can_receive is False):
        kind = EventType.CONSENT_REVOKED
        reason = "unsubscribed" if status == "UNSUBSCRIBED" else "cannot_receive"
    elif receivable and (status == "SUBSCRIBED" or (implied_ok and status == "NEVER_SUBSCRIBED")):
        kind = EventType.CONSENT_GRANTED
    else:
        return []  # never subscribed (and implied consent not accepted): no consent to record
    if when is None:
        return []
    return [
        _row(
            f"{profile_id}:{channel.value}:{kind.value}:{when.isoformat()}",
            customer,
            when,
            kind,
            channel=channel,
            profile_id=profile_id,
            consent_status=status or None,
            consent_method=_text(block.get("method")),
            reason=reason,
        )
    ]


# --- events -------------------------------------------------------------------------------------


def event_rows(
    records: Iterable[Mapping[str, Any]],
    *,
    identity: str = "auto",
    handoffs: HandoffLookup | None = None,
) -> list[Row]:
    """Klaviyo events (with `_metric`, `_profile` and `_ours` added by the reader) as rows."""
    rows: list[Row] = []
    for record in records:
        row = _event_row(record, identity, handoffs)
        if row is not None:
            rows.append(row)
    return rows


def _event_row(record: Mapping[str, Any], identity: str, handoffs: HandoffLookup | None) -> Row | None:
    event_id = _text(record.get("id"))
    metric = (_text(record.get("_metric")) or "").strip()
    attributes = _mapping(record.get("attributes"))
    profile = _mapping(record.get("_profile"))
    when = parse_klaviyo_time(attributes.get("datetime"))
    if event_id is None or when is None:
        return None
    customer = customer_for(_text(profile.get("id")), profile, identity)
    if customer is None:
        return None
    props = _mapping(attributes.get("event_properties"))
    key = metric.casefold()
    common: dict[str, Any] = {
        "provider_event_id": event_id,
        "metric": metric,
        "profile_id": _text(profile.get("id")),
    }
    if key in ENGAGEMENT_METRICS:
        kind, channel = ENGAGEMENT_METRICS[key]
        ours = record.get("_ours") is True
        decision = handoffs(customer, channel, when) if (ours and handoffs is not None) else None
        return _row(
            event_id,
            customer,
            when,
            kind,
            channel=channel,
            decision_id=decision,
            flow_id=_text(props.get("$flow")),
            flow_message_id=_text(props.get("$message")),
            via_autonomy=True if ours else None,
            url=_text(props.get("URL")) or _text(props.get("url")),
            bounce_type=_text(props.get("Bounce Type")) or _text(props.get("bounce_type")),
            reason=_text(props.get("Reason")) or _text(props.get("Drop Reason")),
            **common,
        )
    if key in BEHAVIOUR_METRICS:
        return _row(
            event_id,
            customer,
            when,
            BEHAVIOUR_METRICS[key],
            item=_text(props.get("ProductID")) or _text(props.get("ProductId")) or _text(props.get("SKU")),
            category=_first_text(props.get("Categories")),
            url=_text(props.get("URL")),
            **common,
        )
    return None


# --- helpers ------------------------------------------------------------------------------------

_EPOCH: Final[datetime] = datetime.fromisoformat("1970-01-01T00:00:00+00:00")


def _row(row_id: str, customer: str, ts: datetime, kind: EventType, **values: Any) -> Row:
    row: Row = {
        "row_id": row_id,
        "customer": customer,
        "ts": ts.isoformat(),
        "type": kind.value,
        "channel": None,
        "decision_id": None,
        "category": None,
        "item": None,
    }
    for name in _ATTR_COLUMNS:
        row.setdefault(name, None)
    for name, value in values.items():
        row[name] = value.value if isinstance(value, Channel) else value
    return row


def _text(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    return None


def _first_text(value: object) -> str | None:
    if isinstance(value, list):
        return next((t for t in (_text(v) for v in value) if t), None)
    return _text(value)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []
