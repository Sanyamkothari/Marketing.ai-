"""The brand kit: what we read from the shop's own tools, as typed fields code can check.

Everything here is data, never prose a model wrote. A `BrandKit` has three parts:

* `rules` (`VoiceRules`): the hard rules the Critic enforces on every message (words the shop never
  uses, its subject and SMS lengths, emoji and exclamation marks, the discount forms its copy uses).
  Each comes from the shop's own copy; a rule is left out (None) when there was too little copy to
  tell, so nothing is ever invented.
* `style` (`VoiceStyle`): the soft voice the Creative writes in (tone, greeting, sign-off, the
  words they use). Picks from fixed lists, or short phrases lifted from the shop's own copy after
  code checked their shape (a greeting word from a fixed list, a sign-off line of plain words).
* `findings`: "what we found -> what we will use", one row per rule or style choice, each with
  its `Evidence` (which source, how many messages showed it, of how many read).

Assets (`KitAsset`) are the shop's own product images, each with the record it came from. The
kit's `approval` records the one person's yes to the voice and the samples; it counts only while
the kit's `fingerprint()` is the one they approved.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field

from autonomy.contracts import Channel, Contract, OfferKind, Tone

__all__ = [
    "DISCOUNT_KINDS",
    "AssetSource",
    "BrandApproval",
    "BrandColour",
    "BrandKit",
    "BrandLogo",
    "CopySample",
    "Energy",
    "Evidence",
    "Finding",
    "Formality",
    "IncompleteRead",
    "KitAsset",
    "VoiceRules",
    "VoiceStyle",
    "Warmth",
]

IncompleteRead = Literal["klaviyo", "shopify", "shopify_products"]
"""A connected tool whose read was refused or failed part way (see `store.save_read`)."""
Formality = Literal["casual", "neutral", "formal"]
Energy = Literal["calm", "upbeat", "excited"]
Warmth = Literal["reserved", "neutral", "warm"]

DISCOUNT_KINDS: tuple[OfferKind, ...] = (OfferKind.PERCENT_OFF, OfferKind.AMOUNT_OFF, OfferKind.FREE_SHIPPING)
"""Offer kinds that are a discount a shop's copy states in a recognisable form."""

DEFAULT_GREETING = "Hi {{first_name}},"
"""The greeting our template copy opens with; kept when the shop's copy does not tell us theirs."""


class CopySample(Contract):
    """One message the shop wrote, as text. Read from Klaviyo; never stored in the kit itself."""

    source: Literal["klaviyo_template", "klaviyo_campaign", "klaviyo_sms"]
    source_id: str
    name: str = ""
    channel: Channel = Channel.EMAIL
    subject: str | None = None
    text: str = Field(description="The body as plain text, template tags replaced, footer lines dropped")
    html: str | None = Field(default=None, description="The raw HTML, for the logo and colours")
    sent_at: AwareDatetime | None = None


class Evidence(Contract):
    """Where a finding came from: the source, and how many of the messages read show it."""

    source: str = Field(description="e.g. 'Klaviyo templates and campaigns', 'Shopify products'")
    seen: int = Field(ge=0, description="How many messages (or records) show it")
    of: int = Field(ge=0, description="Out of how many read")
    examples: tuple[str, ...] = Field(default=(), description="Up to three short quotes from the shop's copy")


class Finding(Contract):
    """One row of "what we found -> what we will use"."""

    key: str
    label: str
    found: str
    use: str
    hard: bool = Field(description="True: the Critic rejects any message that breaks it")
    evidence: Evidence


class VoiceRules(Contract):
    """The kit's hard rules. None means "no rule": there was too little of the shop's copy to tell."""

    banned_words: tuple[str, ...] = Field(
        default=(),
        description="Words and phrases the shop's copy never uses, from a fixed list of hype words",
    )
    subject_max: int | None = Field(default=None, ge=1, description="Email subject characters, template text")
    sms_body_max: int | None = Field(default=None, ge=1, description="SMS body characters, template text")
    max_emoji: int | None = Field(default=None, ge=0, description="Emoji per message (subject and body)")
    max_exclamations: int | None = Field(default=None, ge=0, description="'!' per message (subject and body)")
    discount_kinds: tuple[OfferKind, ...] | None = Field(
        default=None,
        description="The discount forms the shop's copy uses. Empty: it never states a discount. None: unknown",
    )


class VoiceStyle(Contract):
    """The soft voice the Creative writes in. Picks from fixed lists, or phrases code checked."""

    tone: Tone = Tone.FRIENDLY
    formality: Formality = "neutral"
    energy: Energy = "calm"
    warmth: Warmth = "neutral"
    humour: bool = False
    greeting: str = Field(
        default=DEFAULT_GREETING,
        description="How messages open, with {{first_name}} for the name; empty: no greeting",
    )
    sign_off: str | None = Field(default=None, description="e.g. 'Cheers,' (emails only)")
    signer: str | None = Field(default=None, description="e.g. 'The Acme team'")
    words_they_use: tuple[str, ...] = ()
    discount_phrase: str | None = Field(
        default=None, description="'{n}% off', 'save {n}%' or '{n}% discount'"
    )
    usual_discount: float | None = Field(
        default=None, ge=0, description="The figure their copy states most often, in its usual form"
    )
    usual_discount_kind: OfferKind | None = None
    tone_source: Literal["measured", "measured_and_model"] = "measured"


class BrandColour(Contract):
    hex: str = Field(pattern=r"^#[0-9a-f]{6}$")
    role: Literal["primary", "secondary", "accent"]
    source: str
    seen: int = Field(default=1, ge=1, description="How many records or templates use it")


class BrandLogo(Contract):
    url: str
    alt_text: str = ""
    source: str
    width: int | None = None
    height: int | None = None


class AssetSource(Contract):
    """The record an asset was read from, so a person can find it in their own tool."""

    platform: Literal["shopify", "klaviyo"]
    connection_id: str | None = None
    record_id: str = Field(description="e.g. gid://shopify/Product/123")
    media_id: str | None = None
    read_at: AwareDatetime


class KitAsset(Contract):
    """One of the shop's own product images. It becomes an approved `Asset` with the kit's approval."""

    asset_id: str
    url: str
    alt_text: str
    title: str = ""
    width: int | None = None
    height: int | None = None
    item_id: str | None = None
    category: str | None = None
    source: AssetSource


class BrandApproval(Contract):
    approved_by: str
    approved_at: AwareDatetime
    fingerprint: str
    sample_ids: tuple[str, ...] = ()
    goal_id: str | None = None


class BrandKit(Contract):
    business_id: str
    read_at: AwareDatetime
    shop_name: str | None = None
    shop_domain: str | None = Field(default=None, description="The shop's public host, for links")
    currency: str | None = None
    sources: tuple[str, ...] = Field(default=(), description="What was read, one line per source")
    messages_read: int = 0
    rules: VoiceRules = Field(default_factory=VoiceRules)
    style: VoiceStyle = Field(default_factory=VoiceStyle)
    findings: tuple[Finding, ...] = ()
    colours: tuple[BrandColour, ...] = ()
    logo: BrandLogo | None = None
    assets: tuple[KitAsset, ...] = ()
    product_types: tuple[str, ...] = Field(
        default=(), description="The shop's product types, most products first"
    )
    notes: tuple[str, ...] = Field(default=(), description="What could not be read, and why")
    catalogue_read: bool = Field(
        default=False,
        description="True: the Shopify products were read in full, so a photo missing from `assets` is "
        "of a product no longer active (its library asset goes back to DRAFT)",
    )
    incomplete: tuple[IncompleteRead, ...] = Field(
        default=(), description="Connected tools whose read was refused or failed part way"
    )
    approval: BrandApproval | None = None

    def fingerprint(self) -> str:
        """A digest of everything the approval covers (not when it was read, not the notes).

        Left out on purpose, so ordinary shop activity does not ask the owner to approve again:
        the product photos themselves (the approval is to use the shop's own active product photos,
        and a product added, updated or archived in Shopify changes which ones those are; the page
        never showed them all), the counted vocabulary (`words_they_use`) and the usual discount
        figure, which shift with every campaign the shop sends. Which shop the photos come from is
        covered (`shop_domain`)."""
        style = self.style.model_dump(mode="json", exclude={"words_they_use", "usual_discount"})
        body = {
            "shop_name": self.shop_name,
            "shop_domain": self.shop_domain,
            "rules": self.rules.model_dump(mode="json"),
            "style": style,
            "colours": [c.hex for c in self.colours],
            "logo": self.logo.url if self.logo else None,
        }
        text = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()[:24]

    @property
    def approved(self) -> bool:
        return self.approval is not None and self.approval.fingerprint == self.fingerprint()

    def approved_at(self) -> datetime | None:
        return self.approval.approved_at if self.approved and self.approval else None

    def voice_text(self) -> str:
        """The voice in plain sentences, built by code from the kit's fields (never model prose)."""
        s, r = self.style, self.rules
        parts = [f"{s.tone.value.capitalize()}: {s.formality}, {s.energy}, {s.warmth} towards the reader."]
        if s.greeting:
            parts.append(f"Open with '{s.greeting}'.")
        else:
            parts.append("Start straight in, with no greeting.")
        if s.sign_off:
            parts.append(f"Sign emails off '{s.sign_off}{' ' + s.signer if s.signer else ''}'.")
        if s.words_they_use:
            parts.append(f"Words this shop uses: {', '.join(s.words_they_use[:10])}.")
        if r.banned_words:
            parts.append(f"Never use: {', '.join(r.banned_words)}.")
        if r.subject_max is not None:
            parts.append(f"Subjects at most {r.subject_max} characters.")
        if r.max_emoji == 0:
            parts.append("No emoji.")
        elif r.max_emoji is not None:
            parts.append(f"At most {r.max_emoji} emoji per message.")
        if r.max_exclamations == 0:
            parts.append("No exclamation marks.")
        if r.discount_kinds == ():
            parts.append("The shop does not talk about discounts.")
        return " ".join(parts)
