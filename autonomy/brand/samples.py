"""Three sample messages in the shop's voice, for the goal we propose first: what one approval accepts.

The samples are written the way every real message is: the Strategist's template plan for the goal
(`template_brief`), the Creative with the brand kit (the model when one is configured, else the
fixed template copy put into the shop's voice), and the Critic's rules with the kit's hard rules.
Only samples that pass are shown. Nothing here is sent.

The offer in a sample is the shop's own usual offer, read from its copy (`VoiceStyle.usual_discount`),
and only in a form its copy uses; a shop that never states a discount gets no offer sample. Previews
fill the slots with the shop's own data (its name, its biggest product type, a product title) and
show '[first name]' where each customer's name goes, never an invented customer.

This module imports the agents, so `autonomy.brand` does not import it (the agents import the kit).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from autonomy.agents.creative import SLOT_PATTERN, Creative
from autonomy.agents.critic import BrandRules, Critic, check_rules
from autonomy.agents.strategist import BusinessFacts, template_brief
from autonomy.brand.kit import BrandKit, KitAsset
from autonomy.brand.rules import offer_in_style
from autonomy.channels.render import (
    FALLBACK_RECOMMENDED_PRODUCT,
    FALLBACK_TOP_CATEGORY,
    asset_urls,
)
from autonomy.contracts import (
    NO_OFFER,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    Contract,
    Format,
    Goal,
    GoalKind,
    OfferKind,
    OfferOption,
    Theme,
)
from engine.llm import LLMCompletion, LLMError

__all__ = [
    "LIBRARY_APPROVER",
    "SAMPLE_COUNT",
    "GoalLibrary",
    "SampleMessage",
    "SampleSet",
    "goal_library",
    "sample_messages",
    "usual_offer",
]

SAMPLE_COUNT = 3
PREVIEW_FIRST_NAME = "[first name]"
"""Shown where each customer's own first name goes (a customer with none gets 'there')."""
USUAL_OFFER_ID = "usual_offer"
_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "AUD": "A$", "CAD": "C$", "NZD": "NZ$"}


class SampleMessage(Contract):
    content_id: str
    channel: Channel
    theme: Theme
    format: Format
    offer_label: str | None = None
    subject: str | None = Field(default=None, description="The template, with {{slots}}")
    body: str
    preview_subject: str | None = Field(default=None, description="Slots filled from the shop's own data")
    preview_body: str
    image_url: str | None = None
    image_alt: str | None = None


class SampleSet(Contract):
    business_id: str
    goal_id: str
    goal_text: str
    goal_kind: GoalKind
    fingerprint: str = Field(description="The kit fingerprint the samples were written for")
    written_by: Literal["model", "template"]
    samples: tuple[SampleMessage, ...] = ()
    notes: tuple[str, ...] = ()


class _NoModel:
    """The LLM client when none is configured: every call fails, so the Creative uses template copy."""

    def complete(self, prompt: str, **_: Any) -> LLMCompletion:  # noqa: ARG002
        raise LLMError("LLM_UNAVAILABLE", "no model is configured")


@dataclass(frozen=True)
class _PreviewAsset:
    """A product image as the Creative sees it, for the samples only (the approval approves the real one)."""

    asset_id: str
    url: str
    alt_text: str
    item_id: str | None
    category: str | None
    kind: str = "image"
    themes: Collection[str] = ()


def usual_offer(kit: BrandKit) -> OfferOption | None:
    """The shop's own most-stated discount as an offer, when its copy states one in a form it uses."""
    style = kit.style
    if style.usual_discount is None or style.usual_discount_kind is None:
        return None
    figure = f"{style.usual_discount:g}"
    if style.usual_discount_kind is OfferKind.PERCENT_OFF:
        label = (style.discount_phrase or "{n}% off").format(n=figure)
    else:
        symbol = _SYMBOLS.get((kit.currency or "").upper(), "")
        label = f"{symbol}{figure} off" if symbol else f"{figure} {kit.currency or ''} off".replace("  ", " ")
    offer = OfferOption(
        offer_id=USUAL_OFFER_ID, label=label, kind=style.usual_discount_kind, amount=style.usual_discount
    )
    return offer if offer_in_style(offer, kit.rules) else None


def _fill(text: str | None, values: dict[str, str]) -> str | None:
    if text is None:
        return None
    return SLOT_PATTERN.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def _preview_values(
    kit: BrandKit, item: ContentItem, offer: OfferOption | None, by_url: dict[str, KitAsset]
) -> dict[str, str]:
    shown = [by_url[u] for u in asset_urls(item.asset_url) if u in by_url]
    product = (
        shown[0].title if shown else (kit.assets[0].title if kit.assets else FALLBACK_RECOMMENDED_PRODUCT)
    )
    return {
        "first_name": PREVIEW_FIRST_NAME,
        "store_name": kit.shop_name or "our shop",
        "top_category": kit.product_types[0].lower() if kit.product_types else FALLBACK_TOP_CATEGORY,
        "recommended_product": product,
        "offer_label": offer.label if offer is not None else "",
    }


def sample_messages(
    kit: BrandKit,
    goal: Goal,
    *,
    now: datetime,
    llm: Any = None,
    model_id: str | None = None,
    channels: Sequence[Channel] = (Channel.EMAIL,),
    count: int = SAMPLE_COUNT,
) -> SampleSet:
    """Up to `count` passing email samples for `goal`, in the kit's voice (see the module docstring)."""
    offer = usual_offer(kit)
    facts = BusinessFacts(
        business_id=kit.business_id,
        name=kit.shop_name or "",
        channels=tuple(channels) or (Channel.EMAIL,),
        offers=(offer,) if offer is not None else (),
    )
    brief = template_brief(goal, facts, now, kind=goal.kind if goal.kind is not GoalKind.CUSTOM else None)
    pool = [
        _PreviewAsset(
            asset_id=a.asset_id, url=a.url, alt_text=a.alt_text, item_id=a.item_id, category=a.category
        )
        for a in kit.assets
    ]
    model: Any = llm if llm is not None else _NoModel()
    creative = Creative(model, model_id=model_id)
    items = creative.build_library(brief, brand_voice="", n_per_cell=1, now=now, assets=pool, kit=kit)
    rules = BrandRules.from_brief(brief, brand_voice="", assets=pool, kit=kit)
    passing = [i for i in items if i.channel is Channel.EMAIL and not check_rules(i, rules)]
    # One per (offer, theme), the picture variant when there is one: the same words either way.
    rank = {Format.TEXT_IMAGE: 0, Format.TEXT: 1}
    chosen: dict[tuple[str, Theme], ContentItem] = {}
    for item in passing:
        key = (item.offer_id, item.theme)
        if key not in chosen or rank.get(item.format, 2) < rank.get(chosen[key].format, 2):
            chosen[key] = item
    plain = [i for (offer_id, _), i in chosen.items() if offer_id == NO_OFFER.offer_id]
    offered = [i for (offer_id, _), i in chosen.items() if offer_id != NO_OFFER.offer_id]
    picks = [*plain[: count - 1], *offered[:1]]
    picks += [i for i in [*plain, *offered] if i not in picks][: count - len(picks)]
    by_url = {a.url: a for a in kit.assets}
    offers = {o.offer_id: o for o in brief.envelope.offers}
    samples: list[SampleMessage] = []
    for item in picks[:count]:
        item_offer = offers.get(item.offer_id)
        real = item_offer if item_offer is not None and item_offer.kind is not OfferKind.NONE else None
        values = _preview_values(kit, item, real, by_url)
        urls = asset_urls(item.asset_url)
        samples.append(
            SampleMessage(
                content_id=item.content_id,
                channel=item.channel,
                theme=item.theme,
                format=item.format,
                offer_label=real.label if real is not None else None,
                subject=item.subject,
                body=item.body,
                preview_subject=_fill(item.subject, values),
                preview_body=_fill(item.body, values) or "",
                image_url=urls[0] if urls else None,
                image_alt=by_url[urls[0]].alt_text if urls and urls[0] in by_url else None,
            )
        )
    notes = [*creative.dropped]
    if len(samples) < count:
        notes.append(f"Only {len(samples)} sample(s) passed the shop's rules.")
    return SampleSet(
        business_id=kit.business_id,
        goal_id=goal.goal_id,
        goal_text=goal.text,
        goal_kind=goal.kind,
        fingerprint=kit.fingerprint(),
        written_by="template" if creative.templated or llm is None else "model",
        samples=tuple(samples),
        notes=tuple(re.sub(r"\s+", " ", n) for n in notes),
    )


LIBRARY_APPROVER = "brand kit"
"""Who approved a library message: the Critic's rules under the voice a person approved once."""


class GoalLibrary(Contract):
    """The messages written for a running goal under the approved brand kit."""

    goal_id: str
    items: tuple[ContentItem, ...] = ()
    approved: int = 0
    notes: tuple[str, ...] = ()


def goal_library(
    kit: BrandKit,
    brief: Brief,
    *,
    now: datetime,
    assets: Sequence[Any] = (),
    llm: Any = None,
    model_id: str | None = None,
) -> GoalLibrary:
    """Every message a running goal may send, written in the approved kit's voice and checked once.

    The one approval in ZERO_TOUCH.md covers the voice: the samples showed what passing copy looks
    like, so every message that passes the Critic's rules plus the kit's hard rules is approved with
    no further review (`judge_optional`: with no model configured the rules are the judge; with one,
    its judge still reviews). The library is the goal's approved brief's channel x offer x theme cells
    (`Creative.build_library` with the kit), so a message never carries an offer the brief's envelope
    (the signed mandate) does not allow. `assets` are the approved product photos. Nothing is sent."""
    if kit.approval is None:
        raise ValueError("the brand kit is not approved yet")
    model: Any = llm if llm is not None else _NoModel()
    creative = Creative(model, model_id=model_id)
    drafts = creative.build_library(brief, brand_voice="", n_per_cell=1, now=now, assets=assets, kit=kit)
    rules = BrandRules.from_brief(brief, brand_voice="", assets=assets, kit=kit).model_copy(
        update={"judge_optional": True}
    )
    reviewed = Critic(model, model_id, seed=0).review(drafts, rules)
    approved = [i for i in reviewed if i.status is ContentStatus.APPROVED]
    notes = [*creative.dropped]
    if not approved:
        notes.append("No message for this goal passed the shop's rules, so it has nothing to send yet.")
    return GoalLibrary(
        goal_id=brief.goal_id,
        items=tuple(reviewed),
        approved=len(approved),
        notes=tuple(re.sub(r"\s+", " ", n) for n in notes),
    )
