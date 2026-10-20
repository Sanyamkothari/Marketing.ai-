"""The EU, the EEA and the UK, basic: GDPR consent and the ePrivacy Directive (PECR in the UK).

- **Consent.** Electronic marketing (email, SMS, WhatsApp) needs consent that is freely given,
  specific, informed and unambiguous: a clear affirmative act, so a pre-ticked box is not consent
  (GDPR Art. 4(11) and recital 32; CJEU *Planet49*). Withdrawing must be as easy as giving
  (Art. 7(3)). The 'soft opt-in' for existing customers (ePrivacy Art. 13(2)) is not assumed: a
  brand that relies on it records it as consent in the ledger.
- **Unsubscribe.** Every marketing email carries an unsubscribe link, and every marketing text or
  WhatsApp message a simple way to object ('Reply STOP').
- **No pre-ticked consent in content.** A web or email item must not show a consent box already
  ticked (a ☑, '[x]' or an HTML `checked` box).
- **Quiet hours.** None at EU level (some member states set their own; not modelled yet).
"""

from __future__ import annotations

import re

from autonomy.compliance_packs.base import (
    MESSAGING_CHANNELS,
    BasePack,
    ChannelLike,
    ConsentRequirement,
    Disclosure,
    PackCheck,
    Purpose,
    channel_name,
)
from autonomy.contracts import ContentItem

EU_COUNTRIES: frozenset[str] = frozenset(
    {
        # EU member states
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV",
        "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
        # the EEA beyond the EU
        "IS", "LI", "NO",
        # the UK (UK GDPR and PECR), under its ISO code and its common alias; 'EL' is Greece's EU code
        "GB", "UK", "EL",
        # the region itself, for brands that only know 'EU'
        "EU", "EEA",
    }
)  # fmt: skip

UNSUBSCRIBE_LINK = Disclosure(
    code="unsubscribe_link",
    description="an unsubscribe link",
    phrases=("unsubscribe", "opt out", "opt-out", "manage your preferences"),
    needs_link=True,
)

OPT_OUT = Disclosure(
    code="opt_out",
    description="a simple way to object, for example 'Reply STOP to opt out'",
    phrases=("opt out", "opt-out", "unsubscribe"),
    patterns=(r"\b(?:reply|text|txt|send)\s+stop\b",),
)

_PRE_TICKED = re.compile(r"☑|\[\s*[xX✓✔]\s*\]|\bchecked\b\s*/?>|\bchecked=|\bpre-?ticked\b")
"""A consent box shown ticked. Not the ✅ emoji: marketing copy uses it as a bullet ('✅ Free delivery')."""


class EUPack(BasePack):
    """The EU, EEA and UK: explicit GDPR consent, an unsubscribe link, no pre-ticked consent."""

    name = "EU/UK"
    country_codes = EU_COUNTRIES

    def required_disclosures(
        self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> tuple[Disclosure, ...]:
        name = channel_name(channel)
        if purpose is not Purpose.PROMOTIONAL or name not in MESSAGING_CHANNELS:
            return ()
        return (UNSUBSCRIBE_LINK,) if name == "email" else (OPT_OUT,)

    def content_checks(
        self, item: ContentItem, channel: ChannelLike | None = None, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> list[PackCheck]:
        checks = self.disclosure_checks(item, channel, purpose)
        name = channel_name(channel if channel is not None else item.channel)
        if name in ("web", "email"):
            checks.append(
                self.check(
                    "consent:no_pre_ticked",
                    not _PRE_TICKED.search(f"{item.subject or ''}\n{item.body}"),
                    "the content shows a consent box already ticked; GDPR consent must be an active choice",
                )
            )
        return checks

    def consent_requirements(
        self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> ConsentRequirement:
        name = channel_name(channel)
        if purpose is not Purpose.PROMOTIONAL:
            return ConsentRequirement(
                kind="none", basis="GDPR Art. 6(1)(b): needed to perform the contract (transactional)"
            )
        if name in MESSAGING_CHANNELS or name == "push":
            return ConsentRequirement(
                kind="explicit",
                basis="GDPR Art. 6(1)(a) and ePrivacy Art. 13: prior consent to electronic marketing",
                channel_specific=name == "whatsapp",
                pre_ticked_allowed=False,
                withdrawal_as_easy_as_grant=True,
            )
        return ConsentRequirement(kind="none", basis="no EU rule for on-site content")
