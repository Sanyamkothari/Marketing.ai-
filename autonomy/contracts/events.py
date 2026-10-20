"""Events: everything that happens to a customer, including what we sent and how they reacted.

The event store is append-only. Customer state is a fold over events. Every outbound lifecycle event
(send, deliver, open, click, reply, bounce, unsubscribe, complaint, block, fail) carries the
`decision_id` that caused it, so every result can be traced back to a decision.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from autonomy.contracts.base import Channel, Contract

AttrValue = str | int | float | bool


class EventType(StrEnum):
    # what the customer did on their own
    SIGNUP = "signup"
    VISIT = "visit"
    VIEW = "view"
    CART = "cart"
    PURCHASE = "purchase"
    REFUND = "refund"
    # what happened to a message we sent
    SEND = "send"
    DELIVER = "deliver"
    OPEN = "open"
    CLICK = "click"
    REPLY = "reply"
    BOUNCE = "bounce"
    UNSUBSCRIBE = "unsubscribe"
    COMPLAINT = "complaint"
    BLOCK = "block"
    FAIL = "fail"
    # consent and preferences
    CONSENT_GRANTED = "consent_granted"
    CONSENT_REVOKED = "consent_revoked"
    PREFERENCE = "preference"  # attrs carry {"key": ..., "value": ...}, e.g. from a reply


OUTBOUND_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.SEND,
        EventType.DELIVER,
        EventType.OPEN,
        EventType.CLICK,
        EventType.REPLY,
        EventType.BOUNCE,
        EventType.UNSUBSCRIBE,
        EventType.COMPLAINT,
        EventType.BLOCK,
        EventType.FAIL,
    }
)
CHANNEL_REQUIRED: frozenset[EventType] = OUTBOUND_TYPES | {
    EventType.CONSENT_GRANTED,
    EventType.CONSENT_REVOKED,
}


class Event(Contract):
    event_id: str = Field(description="Unique; the store drops a second event with the same id")
    customer_id: str
    type: EventType
    ts: AwareDatetime
    source: str = Field(description="Where it came from: 'gym', 'ses', 'whatsapp', 'csv', 'shopify', ...")
    channel: Channel | None = None
    decision_id: str | None = Field(default=None, description="The decision that caused it, if any")
    value: float | None = Field(
        default=None, description="Purchase or refund amount, gross, in the business currency"
    )
    margin: float | None = Field(default=None, description="Gross margin of a purchase, if known")
    category: str | None = None
    item_id: str | None = None
    text: str | None = Field(default=None, description="Reply text; never logged")
    attrs: dict[str, AttrValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _channel_where_needed(self) -> Event:
        if self.type in CHANNEL_REQUIRED and self.channel is None:
            raise ValueError(f"a {self.type.value} event needs a channel")
        if self.type in (EventType.PURCHASE, EventType.REFUND) and self.value is None:
            raise ValueError(f"a {self.type.value} event needs a value")
        return self
