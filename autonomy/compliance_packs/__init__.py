"""Country compliance packs: one country's marketing rules as a plug-in (see `base.CompliancePack`).

`pack_for(country)` returns the pack for an ISO country code. A country with no pack of its own, or
none given at all, gets the **strictest** combination of every loaded pack (`CombinedPack`): quiet
whenever any pack is quiet, every pack's disclosures, the strictest consent, a registry lookup when
any pack wants one. Sending to an unknown country is therefore never looser than sending to a known
one; a brand that sells there adds a pack.

Loaded today: `IndiaPack` (TRAI DLT, NCPR, DPDP; in depth), `USPack` (TCPA, CAN-SPAM; basic) and
`EUPack` (GDPR and ePrivacy, the UK included; basic).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import datetime, time

from autonomy.compliance_packs.base import (
    BasePack,
    ChannelLike,
    CompliancePack,
    ConsentRequirement,
    Disclosure,
    DltRegistration,
    PackCheck,
    PackSettings,
    Purpose,
    QuietWindow,
    SmsTemplateRules,
    channel_name,
    covering_window,
    strictest_consent,
)
from autonomy.compliance_packs.eu import EUPack
from autonomy.compliance_packs.india import IndiaPack
from autonomy.compliance_packs.us import USPack
from autonomy.contracts import Channel, ContentItem

PACK_TYPES: tuple[type[BasePack], ...] = (IndiaPack, USPack, EUPack)
"""Every pack the product ships, strictest first (India's SMS rules are the tightest)."""


class CombinedPack:
    """Several packs as one, each rule taken at its strictest. Used for unknown and multi-country sends."""

    def __init__(self, packs: Sequence[CompliancePack], *, name: str | None = None) -> None:
        if not packs:
            raise ValueError("a combined pack needs at least one pack")
        self.packs: tuple[CompliancePack, ...] = tuple(packs)
        self.name = name or "+".join(p.name for p in packs)
        self.country_codes: frozenset[str] = frozenset[str]().union(*(p.country_codes for p in packs))

    @property
    def sms_template_rules(self) -> SmsTemplateRules | None:
        rules = [r for p in self.packs if (r := p.sms_template_rules) is not None]
        if not rules:
            return None
        # The registration-heavy rules are the strict ones; carry another pack's keyword if it lacks one.
        strictest = max(
            rules,
            key=lambda r: (r.requires_registered_template, r.requires_header, r.requires_entity_id),
        )
        keyword = strictest.opt_out_keyword or next(
            (r.opt_out_keyword for r in rules if r.opt_out_keyword), None
        )
        return replace(strictest, opt_out_keyword=keyword)

    def quiet_window(
        self, channel: ChannelLike = Channel.SMS, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> QuietWindow | None:
        hours: set[int] = set()
        for pack in self.packs:
            window = pack.quiet_window(channel, purpose)
            if window is not None:
                hours |= window.hours()
        return covering_window(hours)

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
        seen: dict[tuple[str, str], Disclosure] = {}
        for pack in self.packs:
            for disclosure in pack.required_disclosures(channel, purpose):
                seen.setdefault((disclosure.code, disclosure.description), disclosure)
        return tuple(seen.values())

    def content_checks(
        self, item: ContentItem, channel: ChannelLike | None = None, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> list[PackCheck]:
        return [c for pack in self.packs for c in pack.content_checks(item, channel, purpose)]

    def content_problems(
        self, item: ContentItem, channel: ChannelLike | None = None, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> list[str]:
        problems = [c.detail for c in self.content_checks(item, channel, purpose) if not c.passed]
        return list(dict.fromkeys(problems))

    def rendered_checks(
        self,
        body: str,
        item: ContentItem,
        channel: ChannelLike | None = None,
        purpose: Purpose = Purpose.PROMOTIONAL,
    ) -> list[PackCheck]:
        return [c for pack in self.packs for c in pack.rendered_checks(body, item, channel, purpose)]

    def consent_requirements(
        self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> ConsentRequirement:
        return strictest_consent([p.consent_requirements(channel, purpose) for p in self.packs])

    def needs_dnd_check(self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL) -> bool:
        return any(p.needs_dnd_check(channel, purpose) for p in self.packs)

    def dnd_registry(self) -> str | None:
        names = [r for p in self.packs if (r := p.dnd_registry()) is not None]
        return "; ".join(dict.fromkeys(names)) or None


def loaded_packs(settings: PackSettings | None = None) -> tuple[BasePack, ...]:
    """One instance of every shipped pack, built with the brand's settings."""
    return tuple(pack_type(settings) for pack_type in PACK_TYPES)


def normalise_country(country: str | None) -> str | None:
    """'in', ' IN ' -> 'IN'; empty -> None."""
    code = (country or "").strip().upper()
    return code or None


def pack_for(
    country: str | None,
    settings: PackSettings | None = None,
    *,
    packs: Sequence[CompliancePack] | None = None,
) -> CompliancePack:
    """The pack for this country, or the strictest combination of all loaded packs when there is none.

    `packs` replaces the shipped packs (a brand with its own pack, or a test); `settings` builds the
    shipped ones with the brand's DLT templates and postal address.
    """
    available: Sequence[CompliancePack] = packs if packs is not None else loaded_packs(settings)
    code = normalise_country(country)
    if code is not None:
        for pack in available:
            if code in pack.country_codes:
                return pack
    return CombinedPack(available, name=f"strictest({'+'.join(p.name for p in available)})")


def packs_for(
    countries: Iterable[str | None],
    settings: PackSettings | None = None,
    *,
    packs: Sequence[CompliancePack] | None = None,
) -> tuple[CompliancePack, ...]:
    """One pack per distinct country, in order (unknown countries share one strictest pack)."""
    out: dict[str, CompliancePack] = {}
    for country in countries:
        pack = pack_for(country, settings, packs=packs)
        out.setdefault(pack.name, pack)
    return tuple(out.values())


__all__ = [
    "PACK_TYPES",
    "BasePack",
    "ChannelLike",
    "CombinedPack",
    "CompliancePack",
    "ConsentRequirement",
    "Disclosure",
    "DltRegistration",
    "EUPack",
    "IndiaPack",
    "PackCheck",
    "PackSettings",
    "Purpose",
    "QuietWindow",
    "SmsTemplateRules",
    "USPack",
    "channel_name",
    "loaded_packs",
    "normalise_country",
    "pack_for",
    "packs_for",
]
