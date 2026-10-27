"""The dispatcher's last checks for a WhatsApp message (a `ChannelGuard`), and its upkeep.

In order: the template must be one Meta approved for a usable category; the number must not be
paused for a RED quality rating; the customer must be under the weekly WhatsApp budget (halved on a
YELLOW rating); and the customer must not be on hold after a Meta error that said "not this person"
(131049's 24 hours, 131026's longer one). A refusal is a skip with a reason code, not a failure: it
sends nothing and counts against no breaker.

Wiring: `whatsapp_guards` builds the guard from a `WhatsAppCloudAdapter` (sharing its template
catalogue and number health); `DispatchGateway.attach_dispatcher` and `build_runtime` use it.
`WhatsAppUpkeep` is the runtime's start-of-tick step that re-reads the templates and the number's
rating from Meta and hands RED alerts to the alert store.

Across processes: the API process takes the webhooks and the runtime process dispatches, so the guard
also reads the latest quality report from the event log (the adapter stores one account-level event
per report) before each check. A RED report there pauses this process's sender too, and survives a
restart. Template status webhooks reach only the process that took them; the other one sees the
change at its next `WhatsAppUpkeep` sync.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Protocol, cast

from autonomy.channels.dispatcher import ChannelGuard, SkipReason
from autonomy.channels.whatsapp_health import (
    ACCOUNT_CUSTOMER,
    ACCOUNT_LEVEL_ATTR,
    HOLD_UNTIL_ATTR,
    WhatsAppHealth,
    WhatsAppLimits,
    error_code_in,
    failure_attrs,
)
from autonomy.channels.whatsapp_templates import TemplateCatalogue
from autonomy.contracts import Alert, Channel, Event, EventType, Message
from autonomy.contracts.events import AttrValue
from autonomy.interfaces import EventStore

WEEK = timedelta(days=7)
UPKEEP_EVERY = timedelta(hours=6)
"""How often `WhatsAppUpkeep` re-reads the templates and the number's rating from Meta."""


def _hold_until(event: Event) -> datetime | None:
    raw = event.attrs.get(HOLD_UNTIL_ATTR)
    if not isinstance(raw, str):
        return None
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return None  # a malformed hold is no hold: it must not stop the batch
    return until if until.tzinfo is not None else None


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
        longest = max((rule.hold for rule in self.limits.errors), default=timedelta(0))
        self.lookback = max(WEEK, longest)
        """How far back holds are read: a week, or the longest hold in the config if that is longer."""

    def sync_health(self, events: EventStore, now: datetime) -> None:
        """Apply quality reports stored by another process (or before a restart) to `health`.

        Reports older than the one `health` holds are ignored by `WhatsAppHealth.update`."""
        reports = [
            e
            for e in events.for_customer(ACCOUNT_CUSTOMER, until=now)
            if e.channel is Channel.WHATSAPP and e.attrs.get(ACCOUNT_LEVEL_ATTR) is True
        ]
        for report in sorted(reports, key=lambda e: e.ts):
            if self.health.updated_at is not None and report.ts <= self.health.updated_at:
                continue
            rating = report.attrs.get("quality_rating")
            tier = report.attrs.get("messaging_limit_tier")
            self.health.update(
                rating if isinstance(rating, str) else None,
                tier if isinstance(tier, str) else None,
                report.ts,
            )

    def check(
        self, message: Message, weekly_cap: int, now: datetime, events: EventStore
    ) -> tuple[SkipReason, str] | None:
        why = self.catalogue.refusal(message.template_name, self.language)
        if why:
            return SkipReason.TEMPLATE_NOT_APPROVED, why
        self.sync_health(events, now)
        if self.health.paused:
            return SkipReason.QUALITY_PAUSED, "WhatsApp is paused: the number's quality rating went RED"
        history = events.for_customer(message.customer_id, since=now - self.lookback, until=now)
        for event in history:
            if event.channel is Channel.WHATSAPP and event.type is EventType.FAIL:
                until = _hold_until(event)
                if until is not None and until > now:
                    label = event.attrs.get("not_delivered", "held by Meta")
                    return (
                        SkipReason.PROVIDER_HOLD,
                        f"{label}; not retried on WhatsApp before {until.isoformat()}",
                    )
        if self.health.rating != "GREEN":
            allowed = self.health.weekly_cap(weekly_cap)
            sent = sum(
                1
                for e in history
                if e.channel is Channel.WHATSAPP and e.type is EventType.SEND and e.ts > now - WEEK
            )
            if sent >= allowed:
                return (
                    SkipReason.QUALITY_BUDGET,
                    f"quality {self.health.rating}: {sent} of {allowed} WhatsApp messages used this week",
                )
        return None

    def failure_attrs(self, error: str, now: datetime) -> Mapping[str, AttrValue]:
        return failure_attrs(self.limits, error_code_in(error), now)


def _whatsapp_parts(adapter: object) -> tuple[TemplateCatalogue, WhatsAppHealth] | None:
    templates = getattr(adapter, "templates", None)
    health = getattr(adapter, "health", None)
    if isinstance(templates, TemplateCatalogue) and isinstance(health, WhatsAppHealth):
        return templates, health
    return None


def whatsapp_guards(adapters: Iterable[object]) -> dict[Channel, ChannelGuard]:
    """The WhatsApp guard for the first adapter among `adapters` that carries a template catalogue and
    number health (a `WhatsAppCloudAdapter`); empty when there is none, so other channels are untouched."""
    for adapter in adapters:
        parts = _whatsapp_parts(adapter)
        if parts is not None:
            templates, health = parts
            language = getattr(adapter, "language", "en")
            guard = WhatsAppGuard(templates, health, language=language if isinstance(language, str) else "en")
            return {Channel.WHATSAPP: guard}
    return {}


class UpkeepTarget(Protocol):
    """What `WhatsAppUpkeep` needs of an adapter (`WhatsAppCloudAdapter` has it)."""

    waba_id: str | None

    def sync_templates(self, now: datetime) -> object: ...

    def get_phone_status(self, now: datetime) -> object: ...

    def drain_alerts(self) -> list[Alert]: ...


class WhatsAppUpkeep:
    """A start-of-tick step (`before_tick` hook): every `every`, re-read the approved templates and the
    number's quality rating from Meta; every tick, hand the alerts the number's health raised to `put`.

    A call that fails raises (the runtime turns it into one alert a day) and is retried after `every`,
    not on every tick."""

    def __init__(
        self,
        adapter: UpkeepTarget,
        put: Callable[[Iterable[Alert]], object],
        *,
        every: timedelta = UPKEEP_EVERY,
    ) -> None:
        self.adapter = adapter
        self.put = put
        self.every = every
        self._last_try: datetime | None = None

    def __call__(self, now: datetime) -> None:
        due = self._last_try is None or now - self._last_try >= self.every
        problems: list[str] = []
        if due:
            self._last_try = now
            if self.adapter.waba_id:
                try:
                    self.adapter.sync_templates(now)
                except Exception as err:  # the provider boundary: report it, keep going
                    problems.append(f"template sync: {type(err).__name__}: {err}")
            try:
                self.adapter.get_phone_status(now)
            except Exception as err:
                problems.append(f"quality rating: {type(err).__name__}: {err}")
        alerts = self.adapter.drain_alerts()
        if alerts:
            self.put(alerts)
        if problems:
            raise RuntimeError("; ".join(problems))


def whatsapp_upkeep(
    adapters: Iterable[object], put: Callable[[Iterable[Alert]], object]
) -> WhatsAppUpkeep | None:
    """The upkeep step for the first WhatsApp Cloud adapter among `adapters`, or None."""
    for adapter in adapters:
        if _whatsapp_parts(adapter) is not None and all(
            callable(getattr(adapter, name, None))
            for name in ("sync_templates", "get_phone_status", "drain_alerts")
        ):
            return WhatsAppUpkeep(cast(UpkeepTarget, adapter), put)
    return None
