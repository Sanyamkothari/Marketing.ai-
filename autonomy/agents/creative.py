"""The Creative: writes a labelled content library for a brief (plan-v2 section 2).

The library is a grid: every channel and offer the envelope allows, crossed with a few themes. For
each cell the Creative asks the LLM for `n_per_cell` variants of copy with `{{slot}}` placeholders.
Everything else is code: ids, labels (theme, tone, offer, channel, format), the slots found, the
WhatsApp template name, the email footer, and the length limits. The LLM writes words, never
labels or numbers, so the bandits downstream can trust the labels they learn over.

Formats (decision 5, done the safe way first). Given the brand's approved assets, every text
variant is also offered as a `text_image`, `carousel` or `video` item wherever the channel can carry
that format (`FORMAT_SUPPORT` in `autonomy.channels.render`: SMS is text only, WhatsApp has no
carousel, an email carousel is stacked images). The format variant reuses the text variant's
words exactly, so a later comparison between formats measures the format and not the copy, and it
costs no extra LLM call. Assets are chosen by code from the approved set, never generated, and an
asset without alt text is never used. The text variants are always kept.

When the model cannot be reached (`LLMError`, or any other exception from the provider), each cell
gets fixed template copy instead (`TEMPLATE_COPY`): plain sentences per theme, the offer only as
`{{offer_label}}`, only allowed slots, and the same footer and length checks as model copy. A
library always comes out, and the cells that used templates are listed in `templated`.

Brand kit (`autonomy.brand`). Given the shop's `BrandKit`, the Creative writes in the voice read
from the shop's own tools: the model is told the kit's voice and hard rules (words never used,
subject length, emoji, exclamation marks), and template copy is put into the voice by code
(`autonomy.brand.rules.apply_voice`: the shop's greeting, its sign-off, a subject that fits its
length). A cell whose offer is a discount form the shop's copy never uses is skipped and noted in
`dropped` (the Critic would reject every variant). The Critic checks the same hard rules.

Items come out as DRAFT. Nothing reaches a customer until the Critic approves it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import datetime
from string import Template
from typing import Protocol

from pydantic import BaseModel, Field

from autonomy.agents.base import AgentOutputError, ask_json, load_prompt
from autonomy.brand.kit import BrandKit
from autonomy.brand.rules import apply_voice, fits_subject, offer_in_style
from autonomy.channels.render import (
    CAROUSEL_MAX,
    CAROUSEL_MIN,
    FORMAT_MEDIA,
    FORMAT_SUPPORT,
    join_asset_urls,
)
from autonomy.contracts import (
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    Format,
    OfferKind,
    OfferOption,
    Theme,
    Tone,
    stable_id,
)
from engine.llm import LLMClient, LLMError

ALLOWED_SLOTS: tuple[str, ...] = (
    "first_name",
    "top_category",
    "offer_label",
    "store_name",
    "recommended_product",
)
"""The only per-customer details the dispatcher knows how to fill at send time. `recommended_product`
is optional: the renderer fills it from the profile, or with a neutral phrase when there is none."""

SLOT_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
"""A `{{slot}}` placeholder. Spaces inside the braces are tolerated, as most template engines do."""

EMAIL_SUBJECT_MAX = 70
"""Longer subjects are cut off in most inboxes, so the end of the message is lost."""

CHANNEL_BODY_MAX: dict[Channel, int] = {
    Channel.EMAIL: 5000,
    Channel.WHATSAPP: 1024,  # Meta's limit for a template body
    Channel.SMS: 160,  # one segment; longer messages split and cost more
    Channel.PUSH: 240,  # what a lock screen shows before truncating
    Channel.WEB: 1000,
    Channel.WEBHOOK: 2000,
}
"""Body limits per channel, measured on the template text (slots unfilled)."""

EMAIL_FOOTER = "You can unsubscribe at any time."
"""Added by code when an email body has no unsubscribe hint: cheaper and surer than asking twice."""

UNSUBSCRIBE_HINTS: tuple[str, ...] = ("unsubscribe", "opt out", "opt-out", "manage your preferences")

THEME_TONE: dict[Theme, Tone] = {
    Theme.SAVINGS: Tone.FRIENDLY,
    Theme.QUALITY: Tone.PROFESSIONAL,
    Theme.URGENCY: Tone.URGENT,
    Theme.SOCIAL_PROOF: Tone.FRIENDLY,
    Theme.EDUCATION: Tone.CALM,
    Theme.CONVENIENCE: Tone.FRIENDLY,
    Theme.NOVELTY: Tone.PLAYFUL,
    Theme.LOYALTY: Tone.FRIENDLY,
    Theme.REMINDER: Tone.FRIENDLY,
    Theme.PERSONAL: Tone.FRIENDLY,
}
"""The tone label each theme is written in. Fixed, so a label always means the same thing."""

OFFER_THEMES: dict[OfferKind, tuple[Theme, ...]] = {
    OfferKind.NONE: (Theme.REMINDER, Theme.QUALITY, Theme.EDUCATION),
    OfferKind.CONTENT: (Theme.EDUCATION, Theme.PERSONAL),
    OfferKind.PERCENT_OFF: (Theme.SAVINGS, Theme.URGENCY),
    OfferKind.AMOUNT_OFF: (Theme.SAVINGS, Theme.URGENCY),
    OfferKind.FREE_SHIPPING: (Theme.CONVENIENCE, Theme.SAVINGS),
    OfferKind.GIFT: (Theme.NOVELTY, Theme.LOYALTY),
    OfferKind.LOYALTY: (Theme.LOYALTY, Theme.PERSONAL),
}
"""Default themes per offer kind. A savings message with no offer would be a false promise."""

THEME_GUIDANCE: dict[Theme, str] = {
    Theme.SOCIAL_PROOF: (
        "Social proof: say in general words that people like the reader enjoy this. Quote no "
        "ratings, review counts, customer numbers or testimonials: none were given, so any would "
        "be invented."
    ),
    Theme.QUALITY: (
        "A comparison angle is welcome: compare with the reader's past choice or between the "
        "brand's own products. Never name or run down a competitor, and claim no 'best' or "
        "'cheapest' that is not given here."
    ),
}
"""Extra direction for themes that are easy to get dishonest. Comparison has no theme of its own:
it is an angle on quality (or savings, when there is an offer) and the labels stay the Theme enum."""

TEMPLATE_COPY: dict[Theme, tuple[tuple[str, str], ...]] = {
    Theme.SAVINGS: (
        (
            "A little something for you, {{first_name}}",
            "Hi {{first_name}}, a good moment to stock up on {{top_category}}.",
        ),
        (
            "Ready when you are, {{first_name}}",
            "Hi {{first_name}}, your next order from {{store_name}} is ready when you are.",
        ),
    ),
    Theme.QUALITY: (
        (
            "Made to last, {{first_name}}",
            "Hi {{first_name}}, everything in {{top_category}} at {{store_name}} is chosen with care.",
        ),
        (
            "A closer look at {{top_category}}",
            "Hi {{first_name}}, take a closer look at {{top_category}} at {{store_name}}.",
        ),
    ),
    Theme.URGENCY: (
        (
            "Worth a look soon, {{first_name}}",
            "Hi {{first_name}}, if you have been eyeing {{top_category}}, now is a good moment.",
        ),
        (
            "Why wait, {{first_name}}?",
            "Hi {{first_name}}, {{top_category}} at {{store_name}} is ready for you today.",
        ),
    ),
    Theme.SOCIAL_PROOF: (
        (
            "A favourite with shoppers like you",
            "Hi {{first_name}}, {{top_category}} is a favourite with shoppers like you.",
        ),
        (
            "Loved by people like you",
            "Hi {{first_name}}, people who shop like you keep coming back to {{top_category}}.",
        ),
    ),
    Theme.EDUCATION: (
        (
            "A few ideas for {{top_category}}",
            "Hi {{first_name}}, a few simple ideas to get more from {{top_category}}.",
        ),
        (
            "Getting the most from {{top_category}}",
            "Hi {{first_name}}, a little care keeps what you buy in {{top_category}} at its best for longer.",
        ),
    ),
    Theme.CONVENIENCE: (
        (
            "Easy does it, {{first_name}}",
            "Hi {{first_name}}, your favourites in {{top_category}} are a few taps away.",
        ),
        (
            "Your next order, made simple",
            "Hi {{first_name}}, picking up where you left off in {{top_category}} takes a minute.",
        ),
    ),
    Theme.NOVELTY: (
        (
            "Something new in {{top_category}}",
            "Hi {{first_name}}, there is something new in {{top_category}} we think you will like.",
        ),
        (
            "The latest at {{store_name}}",
            "Hi {{first_name}}, take a look at the latest in {{top_category}} at {{store_name}}.",
        ),
    ),
    Theme.LOYALTY: (
        (
            "Thank you, {{first_name}}",
            "Hi {{first_name}}, thank you for shopping with {{store_name}}. We are glad you are here.",
        ),
        (
            "A thank-you from {{store_name}}",
            "Hi {{first_name}}, a thank-you from {{store_name}} for coming back to us.",
        ),
    ),
    Theme.REMINDER: (
        (
            "Still thinking about {{top_category}}?",
            "Hi {{first_name}}, your favourites in {{top_category}} are still here when you are ready.",
        ),
        (
            "We kept your place, {{first_name}}",
            "Hi {{first_name}}, it has been a while. {{top_category}} at {{store_name}} is ready when you are.",
        ),
    ),
    Theme.PERSONAL: (
        (
            "Picked for you, {{first_name}}",
            "Hi {{first_name}}, we picked {{recommended_product}} with you in mind.",
        ),
        (
            "Something you might like",
            "Hi {{first_name}}, based on what you like, you might enjoy {{recommended_product}}.",
        ),
    ),
}
"""Fixed copy per theme as (email subject, body), used when the model cannot be reached. Written to
pass the Critic's rules on every channel: allowed slots only, no figures, no deadlines or scarcity,
no capitals, and short enough for one SMS with the offer line added. Each line also reads well when
a slot falls back to its neutral phrase (`top_category` becomes 'our latest picks'), so no
'your {{top_category}}' or '{{top_category}} order'. The offer is never described here:
`TEMPLATE_OFFER_LINE` names it through `{{offer_label}}` when the cell has one."""

TEMPLATE_OFFER_LINE = " Your offer: {{offer_label}}."
"""Appended to template copy for a cell with an offer, so the approved offer's label is the only claim."""


def template_variants(
    channel: Channel, offer: OfferOption, theme: Theme, n: int, *, kit: BrandKit | None = None
) -> list[Variant]:
    """Up to `n` fixed variants for one cell, the same every time (P4 LLM unavailable).

    With a brand kit, each is put into the shop's voice (greeting, sign-off, subject length); a
    variant whose subject cannot fit the shop's length is left out."""
    out: list[Variant] = []
    subjects = [s for s, _ in TEMPLATE_COPY[theme]]
    for subject, body in TEMPLATE_COPY[theme][:n]:
        text = body if offer.kind is OfferKind.NONE else body + TEMPLATE_OFFER_LINE
        voiced = apply_voice(
            subject if channel is Channel.EMAIL else None,
            text,
            channel=channel,
            style=kit.style if kit else None,
            rules=kit.rules if kit else None,
            subjects=subjects,
            ceiling=EMAIL_SUBJECT_MAX,
        )
        if voiced is None:
            continue
        out.append(Variant(subject=voiced[0], body=voiced[1]))
    return out


CAROUSEL_CARDS = 5
"""How many cards the Creative puts in a carousel it assembles itself. Stacked in an email, more than
a handful of images is a long scroll most readers never finish. A card set the brand built is used
whole (up to the channel limit)."""


class ApprovedAsset(Protocol):
    """An approved image or video from the brand's asset library, as far as the Creative needs it.

    Structural, so `autonomy.assets.library.Asset` fits without this module importing it. `kind` is
    "image", "video" or "carousel_card". `themes` are the themes the asset suits; an asset with none
    suits any theme. `item_id` and `category` say what product it shows, if any. Optional extras
    read when present: `status` (only "approved" is used), and `group_id` / `position` for a
    carousel card set the brand built.
    """

    @property
    def asset_id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def url(self) -> str: ...

    @property
    def alt_text(self) -> str | None: ...

    @property
    def item_id(self) -> str | None: ...

    @property
    def category(self) -> str | None: ...

    @property
    def themes(self) -> Collection[str]: ...


def media_kind(asset: ApprovedAsset) -> str:
    """The asset's kind as a lowercase word, whether the library stores a string or a string enum."""
    return str(getattr(asset.kind, "value", asset.kind)).strip().lower()


def is_approved(asset: ApprovedAsset) -> bool:
    """False only when the asset says it is not approved (a library that hands over drafts by mistake)."""
    status = getattr(asset, "status", "approved")
    return str(getattr(status, "value", status)).strip().lower() == "approved"


def usable_asset(asset: ApprovedAsset) -> bool:
    """Approved, with a URL a message can carry and alt text to describe it."""
    if not is_approved(asset):
        return False
    url = asset.url.strip()
    return bool(url) and not any(ch.isspace() for ch in url) and bool((asset.alt_text or "").strip())


def _fits(asset: ApprovedAsset, fmt: Format, theme: Theme) -> bool:
    themes = {str(t) for t in asset.themes}
    return (
        usable_asset(asset)
        and media_kind(asset) in FORMAT_MEDIA.get(fmt, frozenset())
        and (not themes or theme.value in themes)
    )


def _rank(theme: Theme) -> Callable[[ApprovedAsset], tuple[bool, str]]:
    # Assets made for the theme first, then the ones that suit any theme; ties by id, so it repeats.
    return lambda a: (theme.value not in {str(t) for t in a.themes}, a.asset_id)


def assets_for(assets: Iterable[ApprovedAsset], fmt: Format, theme: Theme) -> list[ApprovedAsset]:
    """The usable assets that fit a format and theme, best first, in a fixed order.

    An asset tagged for other themes only is left out: a celebratory picture under an education
    message says something the words do not. Two records with the same URL count once, so a
    carousel never shows one picture twice (the Critic rejects that).
    """
    ranked = sorted((a for a in assets if _fits(a, fmt, theme)), key=_rank(theme))
    by_url: dict[str, ApprovedAsset] = {}
    for asset in ranked:
        by_url.setdefault(asset.url.strip(), asset)
    return list(by_url.values())


def carousel_sets(assets: Sequence[ApprovedAsset], theme: Theme) -> list[list[ApprovedAsset]]:
    """The brand's own card sets (same `group_id`) that can be used whole for this theme, by group.

    A set with any card unusable or off-theme, or with two cards of the same URL, is skipped rather
    than shown with a gap or a repeat.
    """
    groups: dict[str, list[ApprovedAsset]] = {}
    for asset in assets:
        group = getattr(asset, "group_id", None)
        if group and media_kind(asset) in FORMAT_MEDIA[Format.CAROUSEL]:
            groups.setdefault(str(group), []).append(asset)
    out: list[list[ApprovedAsset]] = []
    for group in sorted(groups):
        cards = sorted(groups[group], key=lambda a: (int(getattr(a, "position", 0) or 0), a.asset_id))
        kept = cards[:CAROUSEL_MAX]
        distinct = len({c.url.strip() for c in kept}) == len(kept)
        if len(cards) >= CAROUSEL_MIN and distinct and all(_fits(c, Format.CAROUSEL, theme) for c in cards):
            out.append(kept)
    return out


def _media_choices(
    pool: Sequence[ApprovedAsset], fmt: Format, theme: Theme, n: int
) -> list[list[ApprovedAsset]]:
    """The assets for each of `n` variants of one format, or an empty list when there are too few."""
    if fmt is Format.CAROUSEL:
        sets = carousel_sets(pool, theme)
        if sets:
            return [sets[k % len(sets)] for k in range(n)]
        loose = [a for a in assets_for(pool, fmt, theme) if not getattr(a, "group_id", None)]
        if len(loose) < CAROUSEL_MIN:
            return []
        size = min(CAROUSEL_CARDS, len(loose))
        return [[loose[(k + j) % len(loose)] for j in range(size)] for k in range(n)]
    fitting = assets_for(pool, fmt, theme)
    return [[fitting[k % len(fitting)]] for k in range(n)] if fitting else []


def slots_in(*texts: str | None) -> tuple[str, ...]:
    """Every slot name used in the texts, in order of first use, without repeats."""
    seen: dict[str, None] = {}
    for text in texts:
        for name in SLOT_PATTERN.findall(text or ""):
            seen.setdefault(name, None)
    return tuple(seen)


def has_unsubscribe_hint(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in UNSUBSCRIBE_HINTS)


def template_name(
    goal_id: str,
    version: int,
    channel: Channel,
    offer_id: str,
    theme: Theme,
    index: int,
    scope: str | None = None,
) -> str:
    """A WhatsApp template name: lowercase letters, digits and underscores only, as Meta requires.

    The brief version is part of the name because Meta approves a template by name: a new library
    for a revised brief must not reuse an old name for different wording. `scope` (a customer type, in
    `autonomy.content`) keeps the names of per-type copy apart; without it the name is as before.
    """
    raw = f"{goal_id}_v{version}_{channel.value}_{offer_id}_{theme.value}_{index}".lower()
    if scope:
        raw = f"{raw}_{scope}".lower()
    return re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")[:512]


class Variant(BaseModel):
    """One piece of copy as the LLM writes it. Labels are not asked for; code assigns them."""

    subject: str | None = Field(default=None, description="Email only; leave null for other channels")
    body: str = Field(min_length=1)


class VariantSet(BaseModel):
    variants: list[Variant] = Field(min_length=1)


def _why(exc: Exception) -> str:
    """A short reason for a failed model call: the `LLMError` code, or the exception's type."""
    return exc.code if isinstance(exc, LLMError) else type(exc).__name__


class Creative:
    """Builds a DRAFT content library for a brief. One LLM call per (channel, offer, theme) cell."""

    def __init__(self, llm: LLMClient, *, model_id: str | None = None) -> None:
        self._llm = llm
        self._model_id = model_id
        self.dropped: list[str] = []
        """Why variants from the last build were left out (too long, missing subject, duplicate)."""
        self.failures: list[str] = []
        """Cells from the last build whose LLM answer never parsed."""
        self.templated: list[str] = []
        """Cells from the last build written from `TEMPLATE_COPY` because the model could not be reached."""
        self._down: str | None = None
        self._kit: BrandKit | None = None

    def build_library(
        self,
        brief: Brief,
        *,
        brand_voice: str,
        n_per_cell: int = 2,
        now: datetime,
        themes: Sequence[Theme] | None = None,
        assets: Iterable[ApprovedAsset] = (),
        formats: Mapping[Channel, Sequence[Format]] | None = None,
        kit: BrandKit | None = None,
    ) -> list[ContentItem]:
        """Write `n_per_cell` variants for every channel x offer x theme cell of the brief's envelope.

        `themes`, if given, is used for every offer; otherwise each offer gets the themes that fit
        its kind (`OFFER_THEMES`). A cell whose answer never parses is skipped and noted in
        `failures`, so one bad answer does not cost the whole library. A cell whose model call
        raises (the provider is down) gets template copy instead and is noted in `templated`; once
        the model has failed in a build, the remaining cells use templates without asking again.

        `assets` are the brand's approved images and videos. With none, the library is text only,
        as before. `formats` overrides which formats each channel may use (default
        `FORMAT_SUPPORT`); it can narrow the defaults, and anything the renderer cannot carry is
        still refused at send time.

        `kit` is the shop's brand kit: its voice is added to `brand_voice`, its hard rules go to the
        model, template copy is put into its voice, and offers in a discount form the shop never
        uses are skipped.
        """
        if n_per_cell < 1:
            raise ValueError("n_per_cell must be at least 1")
        self.dropped = []
        self.failures = []
        self.templated = []
        self._down = None
        self._kit = kit
        if kit is not None:
            brand_voice = " ".join(p for p in (brand_voice.strip(), kit.voice_text()) if p)
        pool = list(assets)
        support = formats if formats is not None else FORMAT_SUPPORT
        items: list[ContentItem] = []
        for channel in brief.envelope.channels:
            for offer in brief.envelope.offers:
                if kit is not None and not offer_in_style(offer, kit.rules):
                    self.dropped.append(
                        f"{channel.value}/{offer.offer_id}: the shop's own copy never states this kind of discount"
                    )
                    continue
                for theme in tuple(themes) if themes else OFFER_THEMES[offer.kind]:
                    texts = self._cell(
                        brief, channel, offer, theme, brand_voice=brand_voice, n=n_per_cell, now=now
                    )
                    items.extend(texts)
                    if pool:
                        items.extend(
                            self._format_variants(
                                brief, texts, pool, tuple(support.get(channel, (Format.TEXT,)))
                            )
                        )
        return items

    def reset(self) -> None:
        """Forget the notes of earlier work (`dropped`, `failures`, `templated`) and any outage.

        `build_library` does this itself. `write_for_type` does not, so one weekly run over many
        customer types keeps one set of notes, and a model that went down stays skipped for it."""
        self.dropped = []
        self.failures = []
        self.templated = []
        self._down = None

    def write_for_type(
        self,
        brief: Brief,
        *,
        type_id: str,
        type_note: str,
        channel: Channel,
        offer: OfferOption,
        theme: Theme,
        n: int,
        brand_voice: str,
        now: datetime,
        kit: BrandKit | None = None,
    ) -> list[ContentItem]:
        """Up to `n` DRAFT variants of one cell, written for one customer type (plan-v3 decision 6).

        `type_note` is the type's plain label (no customer data); the model is told it as the
        customer type. Labels, slots, footers and length limits are assigned exactly as in
        `build_library`; the ids and WhatsApp template names also carry `type_id` (the caller's key
        for the type, which may hold more than a type id) and a hash of the final subject and body,
        so two types never share an id and a template name is never reused for different wording.
        Nothing is reset here (see `reset`)."""
        if n < 1:
            raise ValueError("n must be at least 1")
        self._kit = kit
        if kit is not None:
            brand_voice = " ".join(p for p in (brand_voice.strip(), kit.voice_text()) if p)
        if kit is not None and not offer_in_style(offer, kit.rules):
            self.dropped.append(
                f"{channel.value}/{offer.offer_id}: the shop's own copy never states this kind of discount"
            )
            return []
        return self._cell(
            brief,
            channel,
            offer,
            theme,
            brand_voice=brand_voice,
            n=n,
            now=now,
            audience_note=type_note,
            scope=type_id,
        )

    def _format_variants(
        self,
        brief: Brief,
        texts: Sequence[ContentItem],
        pool: Sequence[ApprovedAsset],
        formats: Sequence[Format],
    ) -> list[ContentItem]:
        """The text variants of one cell again, each with approved media, per supported format.

        Variant k takes the k-th fitting asset (wrapping round), so a cell's image variants do not
        all show the same picture. A carousel uses one of the brand's own card sets when one fits
        the theme, and otherwise `CAROUSEL_CARDS` loose images starting at the k-th.
        """
        if not texts:
            return []
        first = texts[0]
        cell = f"{first.channel.value}/{first.offer_id}/{first.theme.value}"
        out: list[ContentItem] = []
        for fmt in dict.fromkeys(formats):
            if fmt is Format.TEXT or fmt not in FORMAT_SUPPORT.get(first.channel, ()):
                continue
            choices = _media_choices(pool, fmt, first.theme, len(texts))
            if not choices:
                self.dropped.append(f"{cell}: no {fmt.value} variant, too few approved assets with alt text")
                continue
            for index, (text, chosen) in enumerate(zip(texts, choices, strict=True)):
                out.append(
                    text.model_copy(
                        update={
                            "content_id": stable_id(
                                "ci",
                                brief.brief_id,
                                brief.version,
                                text.channel.value,
                                text.offer_id,
                                text.theme.value,
                                index,
                                fmt.value,
                                *(a.asset_id for a in chosen),
                            ),
                            "format": fmt,
                            "asset_url": join_asset_urls(a.url for a in chosen),
                            "template_name": (
                                f"{text.template_name}_{fmt.value}"[:512] if text.template_name else None
                            ),
                        }
                    )
                )
        return out

    def _cell(
        self,
        brief: Brief,
        channel: Channel,
        offer: OfferOption,
        theme: Theme,
        *,
        brand_voice: str,
        n: int,
        now: datetime,
        audience_note: str | None = None,
        scope: str | None = None,
    ) -> list[ContentItem]:
        tone = THEME_TONE[theme]
        cell = f"{channel.value}/{offer.offer_id}/{theme.value}"
        variants: Sequence[Variant]
        if self._down is not None:
            variants = template_variants(channel, offer, theme, n, kit=self._kit)
            self.templated.append(f"{cell}: {self._down}")
        else:
            try:
                variants = ask_json(
                    self._llm,
                    VariantSet,
                    system=self._system(),
                    prompt=self._cell_prompt(
                        brief,
                        channel,
                        offer,
                        theme,
                        tone,
                        brand_voice,
                        n,
                        kit=self._kit,
                        audience_note=audience_note,
                    ),
                    model_id=self._model_id,
                ).variants
            except AgentOutputError as exc:
                self.failures.append(f"{cell}: {exc}")
                return []
            except Exception as exc:  # P4 LLM unavailable: fixed template copy, still DRAFT for the Critic
                self._down = f"the model could not be reached ({_why(exc)})"
                variants = template_variants(channel, offer, theme, n, kit=self._kit)
                self.templated.append(f"{cell}: {self._down}")
        return self._items(brief, channel, offer, theme, variants, n=n, now=now, scope=scope)

    def _items(
        self,
        brief: Brief,
        channel: Channel,
        offer: OfferOption,
        theme: Theme,
        variants: Sequence[Variant],
        *,
        n: int,
        now: datetime,
        scope: str | None = None,
    ) -> list[ContentItem]:
        """DRAFT items for one cell's variants, with code-assigned labels; misfits go to `dropped`."""
        tone = THEME_TONE[theme]
        cell = f"{channel.value}/{offer.offer_id}/{theme.value}"
        out: list[ContentItem] = []
        seen_bodies: set[str] = set()
        for variant in variants[:n]:
            body = variant.body.strip()
            subject = ((variant.subject or "").strip() or None) if channel is Channel.EMAIL else None
            if channel is Channel.EMAIL:
                if subject is None:
                    self.dropped.append(f"{cell}: email without a subject")
                    continue
                if len(subject) > EMAIL_SUBJECT_MAX:
                    self.dropped.append(f"{cell}: subject over {EMAIL_SUBJECT_MAX} characters")
                    continue
                kit_rules = self._kit.rules if self._kit is not None else None
                if not fits_subject(subject, kit_rules, EMAIL_SUBJECT_MAX):
                    self.dropped.append(f"{cell}: subject longer than the shop's own subjects")
                    continue
                if not has_unsubscribe_hint(body):
                    body = f"{body}\n\n{EMAIL_FOOTER}"
            # After the footer, so two emails that differ only by a missing footer still count as one.
            if body in seen_bodies:
                self.dropped.append(f"{cell}: duplicate body")
                continue
            limit = CHANNEL_BODY_MAX[channel]
            if len(body) > limit:
                self.dropped.append(f"{cell}: body over {limit} characters")
                continue
            kit_sms = self._kit.rules.sms_body_max if self._kit is not None else None
            if channel is Channel.SMS and kit_sms is not None and len(body) > kit_sms:
                self.dropped.append(f"{cell}: text longer than the shop's own texts ({kit_sms} characters)")
                continue
            seen_bodies.add(body)
            index = len(out)
            # Per-type copy: the scope plus a hash of the final wording, so neither an id nor a WhatsApp
            # template name is ever reused for different words. Without a scope, ids are as before.
            scoped = f"{scope}_{stable_id('w', subject or '', body)[2:10]}" if scope else None
            out.append(
                ContentItem(
                    content_id=stable_id(
                        "ci",
                        brief.brief_id,
                        brief.version,
                        channel.value,
                        offer.offer_id,
                        theme.value,
                        index,
                        *((scoped,) if scoped else ()),
                    ),
                    goal_id=brief.goal_id,
                    channel=channel,
                    format=Format.TEXT,
                    theme=theme,
                    tone=tone,
                    offer_id=offer.offer_id,
                    subject=subject,
                    body=body,
                    slots=slots_in(subject, body),
                    template_name=(
                        template_name(
                            brief.goal_id, brief.version, channel, offer.offer_id, theme, index, scoped
                        )
                        if channel is Channel.WHATSAPP
                        else None
                    ),
                    status=ContentStatus.DRAFT,
                    created_at=now,
                )
            )
        return out

    @staticmethod
    def _system() -> str:
        return Template(load_prompt("creative")).safe_substitute(
            allowed_slots=", ".join(f"{{{{{s}}}}}" for s in ALLOWED_SLOTS),
            email_subject_max=EMAIL_SUBJECT_MAX,
            whatsapp_body_max=CHANNEL_BODY_MAX[Channel.WHATSAPP],
            carousel_max=CAROUSEL_MAX,
        )

    @staticmethod
    def _cell_prompt(
        brief: Brief,
        channel: Channel,
        offer: OfferOption,
        theme: Theme,
        tone: Tone,
        brand_voice: str,
        n: int,
        *,
        kit: BrandKit | None = None,
        audience_note: str | None = None,
    ) -> str:
        if offer.kind is OfferKind.NONE:
            offer_line = "No offer. Do not mention any discount, price, saving or free item."
        else:
            offer_line = (
                f"Offer: {offer.label}. Refer to it with {{{{offer_label}}}}; "
                "do not write any percentage or amount yourself."
            )
        rules = kit.rules if kit is not None else None
        subject_max = (
            min(EMAIL_SUBJECT_MAX, rules.subject_max)
            if rules is not None and rules.subject_max is not None
            else EMAIL_SUBJECT_MAX
        )
        subject_line = (
            f"Each variant needs a subject of at most {subject_max} characters."
            if channel is Channel.EMAIL
            else "Leave subject null: this channel has no subject."
        )
        body_max = CHANNEL_BODY_MAX[channel]
        if channel is Channel.SMS and rules is not None and rules.sms_body_max is not None:
            body_max = min(body_max, rules.sms_body_max)
        kit_lines: list[str] = []
        if rules is not None:
            if rules.banned_words:
                kit_lines.append(
                    f"Never use these words (the shop never does): {', '.join(rules.banned_words)}."
                )
            if rules.max_emoji == 0:
                kit_lines.append("No emoji.")
            elif rules.max_emoji is not None:
                kit_lines.append(f"At most {rules.max_emoji} emoji.")
            if rules.max_exclamations == 0:
                kit_lines.append("No exclamation marks.")
            elif rules.max_exclamations is not None:
                kit_lines.append(f"At most {rules.max_exclamations} exclamation marks.")
        return "\n".join(
            [
                f"Write {n} distinct variants.",
                f"Channel: {channel.value}. Body at most {body_max} characters.",
                subject_line,
                f"Theme: {theme.value}. Tone: {tone.value}.",
                *([f"Angle: {THEME_GUIDANCE[theme]}"] if theme in THEME_GUIDANCE else []),
                offer_line,
                f"Audience: {brief.audience.description}",
                *([f"Customer type: {audience_note}"] if audience_note else []),
                f"Objective: {brief.objective.value}",
                f"Why this plan: {brief.rationale or 'not given'}",
                f"Brand voice: {brand_voice}",
                *kit_lines,
                f"Allowed slots: {', '.join(ALLOWED_SLOTS)}",
            ]
        )
