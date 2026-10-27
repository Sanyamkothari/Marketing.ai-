"""The dispatcher's last checks for a WhatsApp message (a `ChannelGuard`).

In order: the template must be one Meta approved for a usable category; the number must not be
paused for a RED quality rating; the customer must be under the weekly WhatsApp budget (halved on a
YELLOW rating); and the customer must not be on hold after a Meta error that said "not this person"
(131049's 24 hours, 131026's longer one). A refusal is a skip with a reason code, not a failure: it
sends nothing and counts against no breaker.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from autonomy.channels.dispatcher import SkipReason
from autonomy.channels.whatsapp_health import (
    HOLD_UNTIL_ATTR,
    WhatsAppHealth,
    WhatsAppLimits,
    error_code_in,
    failure_attrs,
)
from autonomy.channels.whatsapp_templates import TemplateCatalogue
from autonomy.contracts import Channel, EventType, Message
from autonomy.contracts.events import AttrValue
from autonomy.interfaces import EventStore

WEEK = timedelta(days=7)


class WhatsAppGuard:
    """Template, quality and Meta-limit checks for the Dispatcher (`Dispatcher(guards={WHATSAPP: ...})`)."""

    def __init__(
        self,
        catalogue: TemplateCatalogue,
        health: WhatsAppHealth,
        *,
        language: str = "en",
        limits: WhatsAppLimits | None = None,
    ) -> None:
        """`language` is the template language the adapter sends in (`WhatsAppCloudAdapter.language`)."""
        self.catalogue = catalogue
        self.health = health
        self.language = language
        self.limits = limits if limits is not None else health.limits

    def check(
        self, message: Message, weekly_cap: int, now: datetime, events: EventStore
    ) -> tuple[SkipReason, str] | None:
        why = self.catalogue.refusal(message.template_name, self.language)
        if why:
            return SkipReason.TEMPLATE_NOT_APPROVED, why
        if self.health.paused:
            return SkipReason.QUALITY_PAUSED, "WhatsApp is paused: the number's quality rating is RED"
        history = events.for_customer(message.customer_id, since=now - WEEK, until=now)
        for event in history:
            if event.channel is Channel.WHATSAPP and event.type is EventType.FAIL:
                until = event.attrs.get(HOLD_UNTIL_ATTR)
                if isinstance(until, str) and datetime.fromisoformat(until) > now:
                    label = event.attrs.get("not_delivered", "held by Meta")
                    return SkipReason.PROVIDER_HOLD, f"{label}; not retried on WhatsApp before {until}"
        if self.health.rating != "GREEN":
            allowed = self.health.weekly_cap(weekly_cap)
            sent = sum(1 for e in history if e.channel is Channel.WHATSAPP and e.type is EventType.SEND)
            if sent >= allowed:
                return (
                    SkipReason.QUALITY_BUDGET,
                    f"quality {self.health.rating}: {sent} of {allowed} WhatsApp messages used this week",
                )
        return None

    def failure_attrs(self, error: str, now: datetime) -> Mapping[str, AttrValue]:
        return failure_attrs(self.limits, error_code_in(error), now)
