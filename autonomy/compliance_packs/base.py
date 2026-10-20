"""What a country's marketing rules look like to code: the `CompliancePack` protocol and its parts.

The product is global, so country rules are plug-ins rather than `if country == ...` branches spread
through the Critic and the dispatcher. A pack answers five questions about a channel and a purpose
(promotional, transactional or service):

- **When may we send?** `quiet_window` / `quiet_hours` (India: no promotional SMS 9pm-9am; the US:
  the TCPA's 8am-9pm local).
- **What must every message say?** `required_disclosures` (an opt-out line, an unsubscribe link, a
  postal address).
- **Is this content sendable here?** `content_checks` / `content_problems`, on the approved template,
  and `rendered_checks` on the words one customer receives (India: the filled SMS must still be the
  registered DLT template).
- **What consent do we need?** `consent_requirements` (explicit or opt-out, per channel or not, no
  pre-ticked boxes).
- **Must we look the number up in a do-not-disturb registry first?** `needs_dnd_check`.

Every check is plain, deterministic code: no model, no clock (a local time is passed in), no network.
The registry lookups themselves (NCPR, the US Do Not Call list) belong to the dispatch gateway; a
pack only says when one is needed.

Packs hold no customer data. What they need to know about the brand (its registered DLT templates,
its postal address) arrives as `PackSettings`, a plain contract that can live in a brand's config.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from autonomy.contracts import Channel, ContentItem, Contract

ChannelLike = Channel | str
"""A `Channel`, or a channel the contract has no member for yet (such as 'voice'), by name."""


_DISPLAY: dict[str, str] = {"sms": "SMS", "whatsapp": "WhatsApp", "ivr": "IVR"}


def channel_name(channel: ChannelLike) -> str:
    """The lower-case channel name packs compare against ('sms', 'voice', ...)."""
    return channel.value if isinstance(channel, Channel) else str(channel).strip().lower()


class Purpose(StrEnum):
    """Why a message is sent. Rules differ sharply: most only bind promotional messages.

    India's DLT has finer categories (service-implicit, service-explicit); `SERVICE` stands for both
    here, and like `TRANSACTIONAL` it is exempt from the promotional-only rules.
    """

    PROMOTIONAL = "promotional"
    TRANSACTIONAL = "transactional"
    SERVICE = "service"


TELECOM_CHANNELS: frozenset[str] = frozenset({"sms", "voice", "call", "ivr"})
"""Channels regulators treat as telephone communication (TRAI, TCPA)."""

MESSAGING_CHANNELS: frozenset[str] = frozenset({"sms", "whatsapp", "email", "voice", "call", "ivr"})
"""Channels that reach a person's own inbox or phone, where opt-out rules bind. Push and on-site
messages are switched off in the app or browser, so a pack asks no disclosure of them."""


# --- the parts of an answer ----------------------------------------------------------------------


@dataclass(frozen=True)
class QuietWindow:
    """No sends from `start_hour` (inclusive) until `end_hour` (exclusive), local time; may wrap midnight."""

    start_hour: int
    end_hour: int

    def __post_init__(self) -> None:
        if not (0 <= self.start_hour <= 23 and 0 <= self.end_hour <= 23):
            raise ValueError("quiet hours are whole hours from 0 to 23")
        if self.start_hour == self.end_hour:
            raise ValueError("a quiet window needs different start and end hours")

    def hours(self) -> frozenset[int]:
        """The local hours (0-23) inside the window."""
        if self.start_hour < self.end_hour:
            return frozenset(range(self.start_hour, self.end_hour))
        return frozenset([*range(self.start_hour, 24), *range(self.end_hour)])

    def contains(self, local: datetime | time) -> bool:
        """True when this local time falls inside the window."""
        return local.hour in self.hours()

    def describe(self) -> str:
        return f"{self.start_hour:02d}:00-{self.end_hour:02d}:00 local"


def covering_window(hours: Iterable[int]) -> QuietWindow | None:
    """The shortest single window that covers every one of these quiet hours (the strict merge).

    Two packs' windows can only be merged safely by widening: the result is quiet whenever either
    one is. Returns None when no hour is quiet; refuses when every hour is (nothing could be sent).
    """
    quiet = set(hours)
    if not quiet:
        return None
    if len(quiet) == 24:
        raise ValueError("every hour is quiet; no window can express that")
    # The longest run of open (non-quiet) hours, walking round the clock; the window is the rest.
    best_start, best_len = 0, 0
    for start in range(24):
        if start in quiet or (start - 1) % 24 not in quiet:
            continue
        length = 0
        while (start + length) % 24 not in quiet:
            length += 1
        if length > best_len:
            best_start, best_len = start, length
    open_end = (best_start + best_len) % 24
    return QuietWindow(start_hour=open_end, end_hour=best_start)


@dataclass(frozen=True)
class Disclosure:
    """A line the message must carry, and the wordings that count as carrying it."""

    code: str
    description: str
    phrases: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    needs_link: bool = False

    def present_in(self, text: str) -> bool:
        """True when any phrase (ignoring case) or pattern is found, and a link too if one is needed."""
        lowered = text.lower()
        found = any(p.lower() in lowered for p in self.phrases) or any(
            re.search(p, text, re.IGNORECASE) for p in self.patterns
        )
        if found and self.needs_link:
            return bool(LINK.search(text))
        return found


LINK = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
"""A link written into a message body."""


ConsentKind = Literal["none", "opt_out", "explicit"]


@dataclass(frozen=True)
class ConsentRequirement:
    """What consent a contact needs before it may be sent, as the consent ledger must prove it.

    - `kind`: 'none' (no consent needed, e.g. a transactional message), 'opt_out' (may send until the
      person objects, e.g. CAN-SPAM email) or 'explicit' (a clear, affirmative grant first).
    - `channel_specific`: the grant must name this channel (a WhatsApp opt-in), not 'any channel'.
    - `written`: the grant must be recorded in writing, e.g. the TCPA's prior express written consent.
    - `pre_ticked_allowed`: whether a box ticked in advance counts as consent (never, under GDPR).
    - `valid_days`: how long a grant lasts, when the law limits it (None: until withdrawn).
    """

    kind: ConsentKind
    basis: str
    channel_specific: bool = False
    written: bool = False
    pre_ticked_allowed: bool = False
    withdrawal_as_easy_as_grant: bool = True
    valid_days: int | None = None

    @property
    def required(self) -> bool:
        return self.kind == "explicit"


_STRICTNESS: dict[str, int] = {"none": 0, "opt_out": 1, "explicit": 2}


def strictest_consent(requirements: Sequence[ConsentRequirement]) -> ConsentRequirement:
    """One requirement at least as strict as each given one, on every axis."""
    if not requirements:
        return ConsentRequirement(kind="none", basis="no pack")
    kind = max((r.kind for r in requirements), key=lambda k: _STRICTNESS[k])
    days = [r.valid_days for r in requirements if r.valid_days is not None]
    return ConsentRequirement(
        kind=kind,
        basis="; ".join(dict.fromkeys(r.basis for r in requirements)),
        channel_specific=any(r.channel_specific for r in requirements),
        written=any(r.written for r in requirements),
        pre_ticked_allowed=all(r.pre_ticked_allowed for r in requirements),
        withdrawal_as_easy_as_grant=any(r.withdrawal_as_easy_as_grant for r in requirements),
        valid_days=min(days) if days else None,
    )


@dataclass(frozen=True)
class SmsTemplateRules:
    """How SMS templates must be registered and written in a country (India's TRAI DLT, mainly).

    The id patterns are what TRAI's DLT portals issue today (19-digit entity and template ids; six
    digits for a promotional header, six letters for a transactional or service one). They are
    data, so a pack can be built with an operator's own formats if those change.
    """

    requires_registered_template: bool = False
    requires_header: bool = False
    requires_entity_id: bool = False
    placeholder_tags: tuple[str, ...] = ()
    """The variable tags a registered template may use, written `{#tag#}`."""
    var_max_chars: int | None = None
    """The longest value one `{#var#}`, `{#numeric#}` or `{#alphanumeric#}` slot may take."""
    template_max_chars: int | None = None
    header_patterns: dict[str, str] = field(default_factory=dict)
    """Purpose value -> regular expression a sender header must fully match."""
    entity_id_pattern: str | None = None
    template_id_pattern: str | None = None
    opt_out_keyword: str | None = None
    """The keyword carriers act on ('STOP'), when a country names one."""


@dataclass(frozen=True)
class PackCheck:
    """One named check a pack ran, and whether the content passed it. Failures read as plain English."""

    pack: str
    code: str
    passed: bool
    detail: str


# --- what a brand tells the packs ----------------------------------------------------------------


class DltRegistration(Contract):
    """One SMS template as registered on India's DLT platform, with the ids an operator checks.

    `text` is the template exactly as registered, with variable slots tagged `{#var#}`,
    `{#numeric#}`, `{#alphanumeric#}`, `{#url#}` or `{#cbn#}` (a call-back number).
    """

    template_id: str = Field(description="The DLT content template id")
    header: str = Field(description="The registered sender id (header) the SMS goes out under")
    entity_id: str = Field(description="The principal entity (PE) id of the brand")
    text: str
    purpose: Purpose = Purpose.PROMOTIONAL


class PackSettings(Contract):
    """What the packs need to know about the brand. Empty is valid; checks that need a value then fail."""

    dlt_templates: tuple[DltRegistration, ...] = ()
    postal_address: str | None = Field(
        default=None, description="The brand's physical postal address, for CAN-SPAM email footers"
    )


# --- the protocol --------------------------------------------------------------------------------


@runtime_checkable
class CompliancePack(Protocol):
    """One country's (or region's) rules for marketing messages. See the module docstring."""

    name: str
    country_codes: frozenset[str]

    @property
    def sms_template_rules(self) -> SmsTemplateRules | None:
        """How SMS templates must be registered and written here; None when nothing special applies."""
        ...

    def quiet_window(
        self, channel: ChannelLike = Channel.SMS, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> QuietWindow | None:
        """The local hours with no sends for this channel and purpose; None when the law sets none."""
        ...

    def quiet_hours(
        self,
        local: datetime | time,
        channel: ChannelLike = Channel.SMS,
        purpose: Purpose = Purpose.PROMOTIONAL,
    ) -> bool:
        """True when `local` (the customer's local time) is inside the quiet window: do not send now."""
        ...

    def required_disclosures(
        self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> tuple[Disclosure, ...]:
        """The lines every message on this channel and purpose must carry."""
        ...

    def content_checks(
        self, item: ContentItem, channel: ChannelLike | None = None, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> list[PackCheck]:
        """Every check this pack runs on an approved template, passed or not."""
        ...

    def content_problems(
        self, item: ContentItem, channel: ChannelLike | None = None, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> list[str]:
        """What is wrong with the template here, in plain words; empty means the pack is satisfied."""
        ...

    def rendered_checks(
        self,
        body: str,
        item: ContentItem,
        channel: ChannelLike | None = None,
        purpose: Purpose = Purpose.PROMOTIONAL,
    ) -> list[PackCheck]:
        """Checks on the words one customer receives (filled slots), beyond the template's checks."""
        ...

    def consent_requirements(
        self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> ConsentRequirement:
        """The consent a contact needs before it may be sent on this channel and purpose."""
        ...

    def needs_dnd_check(self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL) -> bool:
        """True when the number must be looked up in a do-not-disturb registry before sending."""
        ...

    def dnd_registry(self) -> str | None:
        """The registry `needs_dnd_check` refers to (for the gateway's lookup and the audit)."""
        ...


class BasePack:
    """Shared plumbing: `quiet_hours` from `quiet_window`, problems from checks, disclosure checks.

    Subclasses set `name`, `country_codes` and override the rule methods. The defaults are the
    permissive ones (no quiet window, no disclosures, no consent, no registry), so a pack states
    every rule it adds.
    """

    name: str = "base"
    country_codes: frozenset[str] = frozenset()

    def __init__(self, settings: PackSettings | None = None) -> None:
        self.settings = settings or PackSettings()

    @property
    def sms_template_rules(self) -> SmsTemplateRules | None:
        return None

    def quiet_window(
        self, channel: ChannelLike = Channel.SMS, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> QuietWindow | None:
        del channel, purpose
        return None

    def quiet_hours(
        self,
        local: datetime | time,
        channel: ChannelLike = Channel.SMS,
        purpose: Purpose = Purpose.PROMOTIONAL,
    ) -> bool:
        window = self.quiet_window(channel, purpose)
        return window is not None and window.contains(local)

    def required_disclosures(
        self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> tuple[Disclosure, ...]:
        del channel, purpose
        return ()

    def content_checks(
        self, item: ContentItem, channel: ChannelLike | None = None, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> list[PackCheck]:
        return self.disclosure_checks(item, channel, purpose)

    def content_problems(
        self, item: ContentItem, channel: ChannelLike | None = None, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> list[str]:
        return [c.detail for c in self.content_checks(item, channel, purpose) if not c.passed]

    def rendered_checks(
        self,
        body: str,
        item: ContentItem,
        channel: ChannelLike | None = None,
        purpose: Purpose = Purpose.PROMOTIONAL,
    ) -> list[PackCheck]:
        del body, item, channel, purpose
        return []

    def consent_requirements(
        self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> ConsentRequirement:
        del channel, purpose
        return ConsentRequirement(kind="none", basis="no rule in this pack")

    def needs_dnd_check(self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL) -> bool:
        del channel, purpose
        return False

    def dnd_registry(self) -> str | None:
        return None

    # helpers for subclasses

    def disclosure_checks(
        self, item: ContentItem, channel: ChannelLike | None, purpose: Purpose
    ) -> list[PackCheck]:
        """One check per required disclosure: is it in the subject or body?"""
        name = channel_name(channel if channel is not None else item.channel)
        text = f"{item.subject or ''}\n{item.body}"
        return [
            self.check(
                f"disclosure:{d.code}",
                d.present_in(text),
                f"a {purpose.value} {_DISPLAY.get(name, name)} message needs {d.description}",
            )
            for d in self.required_disclosures(name, purpose)
        ]

    def check(self, code: str, passed: bool, failure: str, success: str | None = None) -> PackCheck:
        """A `PackCheck` from this pack; `failure` is the detail when it fails, `success` when it passes."""
        detail = failure if not passed else (success or f"ok: {code}")
        return PackCheck(pack=self.name, code=code, passed=passed, detail=f"{self.name}: {detail}")
