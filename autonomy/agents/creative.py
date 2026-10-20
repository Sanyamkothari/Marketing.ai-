"""The Creative: writes a labelled content library for a brief (plan-v2 section 2).

The library is a grid: every channel and offer the envelope allows, crossed with a few themes. For
each cell the Creative asks the LLM for `n_per_cell` variants of copy with `{{slot}}` placeholders.
Everything else is code: ids, labels (theme, tone, offer, channel, format), the slots found, the
WhatsApp template name, the email footer, and the length limits. The LLM writes words, never
labels or numbers, so the bandits downstream can trust the labels they learn over.

Items come out as DRAFT. Nothing reaches a customer until the Critic approves it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from string import Template

from pydantic import BaseModel, Field

from autonomy.agents.base import AgentOutputError, ask_json, load_prompt
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
from engine.llm import LLMClient

ALLOWED_SLOTS: tuple[str, ...] = ("first_name", "top_category", "offer_label", "store_name")
"""The only per-customer details the dispatcher knows how to fill at send time."""

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
    goal_id: str, version: int, channel: Channel, offer_id: str, theme: Theme, index: int
) -> str:
    """A WhatsApp template name: lowercase letters, digits and underscores only, as Meta requires.

    The brief version is part of the name because Meta approves a template by name: a new library
    for a revised brief must not reuse an old name for different wording.
    """
    raw = f"{goal_id}_v{version}_{channel.value}_{offer_id}_{theme.value}_{index}".lower()
    return re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")[:512]


class Variant(BaseModel):
    """One piece of copy as the LLM writes it. Labels are not asked for; code assigns them."""

    subject: str | None = Field(default=None, description="Email only; leave null for other channels")
    body: str = Field(min_length=1)


class VariantSet(BaseModel):
    variants: list[Variant] = Field(min_length=1)


class Creative:
    """Builds a DRAFT content library for a brief. One LLM call per (channel, offer, theme) cell."""

    def __init__(self, llm: LLMClient, *, model_id: str | None = None) -> None:
        self._llm = llm
        self._model_id = model_id
        self.dropped: list[str] = []
        """Why variants from the last build were left out (too long, missing subject, duplicate)."""
        self.failures: list[str] = []
        """Cells from the last build whose LLM answer never parsed."""

    def build_library(
        self,
        brief: Brief,
        *,
        brand_voice: str,
        n_per_cell: int = 2,
        now: datetime,
        themes: Sequence[Theme] | None = None,
    ) -> list[ContentItem]:
        """Write `n_per_cell` variants for every channel x offer x theme cell of the brief's envelope.

        `themes`, if given, is used for every offer; otherwise each offer gets the themes that fit
        its kind (`OFFER_THEMES`). A cell whose answer never parses is skipped and noted in
        `failures`, so one bad answer does not cost the whole library.
        """
        if n_per_cell < 1:
            raise ValueError("n_per_cell must be at least 1")
        self.dropped = []
        self.failures = []
        items: list[ContentItem] = []
        for channel in brief.envelope.channels:
            for offer in brief.envelope.offers:
                for theme in tuple(themes) if themes else OFFER_THEMES[offer.kind]:
                    items.extend(
                        self._cell(
                            brief, channel, offer, theme, brand_voice=brand_voice, n=n_per_cell, now=now
                        )
                    )
        return items

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
    ) -> list[ContentItem]:
        tone = THEME_TONE[theme]
        cell = f"{channel.value}/{offer.offer_id}/{theme.value}"
        try:
            answer = ask_json(
                self._llm,
                VariantSet,
                system=self._system(),
                prompt=self._cell_prompt(brief, channel, offer, theme, tone, brand_voice, n),
                model_id=self._model_id,
            )
        except AgentOutputError as exc:
            self.failures.append(f"{cell}: {exc}")
            return []

        out: list[ContentItem] = []
        seen_bodies: set[str] = set()
        for variant in answer.variants[:n]:
            body = variant.body.strip()
            subject = ((variant.subject or "").strip() or None) if channel is Channel.EMAIL else None
            if channel is Channel.EMAIL:
                if subject is None:
                    self.dropped.append(f"{cell}: email without a subject")
                    continue
                if len(subject) > EMAIL_SUBJECT_MAX:
                    self.dropped.append(f"{cell}: subject over {EMAIL_SUBJECT_MAX} characters")
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
            seen_bodies.add(body)
            index = len(out)
            out.append(
                ContentItem(
                    content_id=stable_id(
                        "ci", brief.brief_id, brief.version, channel.value, offer.offer_id, theme.value, index
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
                        template_name(brief.goal_id, brief.version, channel, offer.offer_id, theme, index)
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
    ) -> str:
        if offer.kind is OfferKind.NONE:
            offer_line = "No offer. Do not mention any discount, price, saving or free item."
        else:
            offer_line = (
                f"Offer: {offer.label}. Refer to it with {{{{offer_label}}}}; "
                "do not write any percentage or amount yourself."
            )
        subject_line = (
            f"Each variant needs a subject of at most {EMAIL_SUBJECT_MAX} characters."
            if channel is Channel.EMAIL
            else "Leave subject null: this channel has no subject."
        )
        return "\n".join(
            [
                f"Write {n} distinct variants.",
                f"Channel: {channel.value}. Body at most {CHANNEL_BODY_MAX[channel]} characters.",
                subject_line,
                f"Theme: {theme.value}. Tone: {tone.value}.",
                offer_line,
                f"Audience: {brief.audience.description}",
                f"Objective: {brief.objective.value}",
                f"Why this plan: {brief.rationale or 'not given'}",
                f"Brand voice: {brand_voice}",
                f"Allowed slots: {', '.join(ALLOWED_SLOTS)}",
            ]
        )
