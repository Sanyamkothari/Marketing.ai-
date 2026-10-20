"""The United States, basic: the TCPA for texts and calls, CAN-SPAM for email.

- **TCPA quiet hours.** Telephone solicitations (texts included) only from 8am to 9pm at the
  recipient's local time, so 21:00-08:00 local is quiet for promotional SMS and calls.
- **STOP.** A marketing text says how to opt out with the keyword carriers act on: 'Reply STOP to
  opt out' (CTIA guidance; the FCC's 2024 revocation rule makes STOP binding).
- **Consent.** A marketing text needs prior express *written* consent; an informational one needs
  prior express consent. Email is opt-out: CAN-SPAM lets you send until the person objects.
- **CAN-SPAM email.** A clear unsubscribe and the sender's valid physical postal address in every
  commercial email. When `PackSettings.postal_address` is set the email must carry exactly that
  address; otherwise it must at least carry something address-shaped (a state and ZIP, or a PO Box).
- **Do Not Call.** A promotional text or call to a number on the National Do Not Call Registry needs
  consent, so the gateway looks the number up first.

State laws (Florida's and Oklahoma's mini-TCPAs, with 8am-8pm windows) are not modelled yet.
"""

from __future__ import annotations

from autonomy.compliance_packs.base import (
    MESSAGING_CHANNELS,
    TELECOM_CHANNELS,
    BasePack,
    ChannelLike,
    ConsentRequirement,
    Disclosure,
    PackCheck,
    Purpose,
    QuietWindow,
    SmsTemplateRules,
    channel_name,
)
from autonomy.contracts import Channel, ContentItem

STOP_KEYWORD = Disclosure(
    code="stop_keyword",
    description="the STOP keyword, for example 'Reply STOP to opt out'",
    patterns=(
        r"\b(?:reply|text|txt|send)\s+stop\b",
        r"\bstop\s+to\s+(?:opt\s*-?\s*out|unsubscribe|end|cancel|quit)\b",
    ),
)

UNSUBSCRIBE = Disclosure(
    code="unsubscribe",
    description="a clear way to unsubscribe",
    phrases=("unsubscribe", "opt out", "opt-out", "manage your preferences"),
)

US_STATE_CODES: tuple[str, ...] = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM",
    "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "PR", "GU", "VI", "AS", "MP",
)  # fmt: skip
"""USPS state and territory codes, for spotting the 'TX 78701' that ends an address."""

POSTAL_ADDRESS = Disclosure(
    code="postal_address",
    description="the sender's physical postal address",
    patterns=(
        # Case-sensitive (a `Disclosure` otherwise matches ignoring case) and a real state code, so
        # copy such as 'up to 10000 points' is not taken for an address ending 'TO 10000'.
        rf"(?-i:\b(?:{'|'.join(US_STATE_CODES)})\s+\d{{5}}(?:-\d{{4}})?\b)",
        r"\bP\.?\s?O\.?\s+Box\s+\d+",
    ),
)
"""Address-shaped text, used when the brand has not told us its exact address."""

US_SMS_RULES = SmsTemplateRules(opt_out_keyword="STOP")


def _flat(text: str) -> str:
    return " ".join(text.lower().split())


class USPack(BasePack):
    """The US: TCPA quiet hours, STOP for SMS, CAN-SPAM unsubscribe and postal address for email."""

    name = "US"
    country_codes = frozenset({"US", "USA"})

    @property
    def sms_template_rules(self) -> SmsTemplateRules:
        return US_SMS_RULES

    def quiet_window(
        self, channel: ChannelLike = Channel.SMS, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> QuietWindow | None:
        if purpose is Purpose.PROMOTIONAL and channel_name(channel) in TELECOM_CHANNELS:
            return QuietWindow(start_hour=21, end_hour=8)
        return None

    def required_disclosures(
        self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> tuple[Disclosure, ...]:
        name = channel_name(channel)
        if purpose is not Purpose.PROMOTIONAL or name not in MESSAGING_CHANNELS:
            return ()
        if name == "sms":
            return (STOP_KEYWORD,)
        if name == "email":
            return (UNSUBSCRIBE, POSTAL_ADDRESS)
        return ()

    def content_checks(
        self, item: ContentItem, channel: ChannelLike | None = None, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> list[PackCheck]:
        checks = [
            c
            for c in self.disclosure_checks(item, channel, purpose)
            if not (c.code == "disclosure:postal_address" and self.settings.postal_address)
        ]
        address = self.settings.postal_address
        name = channel_name(channel if channel is not None else item.channel)
        if address and purpose is Purpose.PROMOTIONAL and name == "email":
            checks.append(
                self.check(
                    "disclosure:postal_address",
                    _flat(address) in _flat(f"{item.subject or ''}\n{item.body}"),
                    f"a commercial email needs the sender's postal address ({address})",
                )
            )
        return checks

    def consent_requirements(
        self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> ConsentRequirement:
        name = channel_name(channel)
        if name in TELECOM_CHANNELS:
            if purpose is Purpose.PROMOTIONAL:
                return ConsentRequirement(
                    kind="explicit",
                    basis="TCPA: prior express written consent for marketing texts and calls",
                    channel_specific=True,
                    written=True,
                )
            return ConsentRequirement(
                kind="explicit",
                basis="TCPA: prior express consent for informational texts",
                channel_specific=True,
            )
        if name == "email":
            return ConsentRequirement(kind="opt_out", basis="CAN-SPAM: send until the person unsubscribes")
        if name == "whatsapp" and purpose is Purpose.PROMOTIONAL:
            return ConsentRequirement(
                kind="explicit",
                basis="WhatsApp Business policy: an opt-in to WhatsApp itself",
                channel_specific=True,
            )
        return ConsentRequirement(kind="none", basis="no US rule for this channel")

    def needs_dnd_check(self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL) -> bool:
        return purpose is Purpose.PROMOTIONAL and channel_name(channel) in TELECOM_CHANNELS

    def dnd_registry(self) -> str:
        return "National Do Not Call Registry"


__all__ = ["POSTAL_ADDRESS", "STOP_KEYWORD", "UNSUBSCRIBE", "USPack"]
