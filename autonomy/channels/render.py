"""Turn an approved content item into the words one customer receives.

The Creative writes bodies with `{{slot}}` placeholders and the Critic approves them as written. At
send time we only fill those slots, from customer state and a small profile (name and the like),
so nothing the Critic has not seen can slip in. Three rules keep that true:

- Only known slots are filled. A placeholder we do not know is an error, never left in the text
  for a customer to see.
- Every slot has a safe fallback ("Hi there" rather than "Hi ,"; "our pick for you" when no product
  was recommended), except `offer_label`: a message that promises an offer the decision did not
  grant is refused instead.
- Filled values are plain text: control characters are stripped and whitespace collapsed to one
  line. Nothing is HTML-escaped here; an email adapter that builds an HTML part escapes the whole
  body itself, and WhatsApp template parameters are sent as given.

Formats. An item labelled `text_image`, `carousel` or `video` carries the approved asset it was
approved with in `asset_url`, and the rendered result carries it on unchanged: the renderer never
picks, swaps or generates media. A carousel's card URLs share the one `asset_url` field, joined by a
single space (a URL cannot contain a raw space, so the split is unambiguous) until the contract
grows a list field. Which formats each channel can carry is `FORMAT_SUPPORT`; the Creative and the
Critic read the same table, so a format the renderer would refuse is never written or approved.
For WhatsApp the media becomes the template's header parameter (`HeaderMedia`).

Rendering is pure: `now` is passed in (for the `weekday` slot) and nothing reads the clock.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from autonomy.assets.support import formats_for
from autonomy.contracts import Channel, ContentItem, CustomerState, Format, Message, OfferKind, OfferOption

SLOT_PATTERN = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
"""A `{{slot}}` placeholder, spaces inside the braces allowed."""

MAX_VALUE_LEN = 120
"""Longest filled value. A runaway profile field must not turn a short message into a wall of text."""

FALLBACK_FIRST_NAME = "there"
FALLBACK_TOP_CATEGORY = "our latest picks"
FALLBACK_RECOMMENDED_PRODUCT = "our pick for you"
"""Reads well wherever a product name would ("Have you seen our pick for you?")."""

MediaKind = Literal["image", "video"]

FORMAT_SUPPORT: dict[Channel, tuple[Format, ...]] = {channel: formats_for(channel) for channel in Channel}
"""The formats each channel can carry by default, read from the cells of
`autonomy.assets.support.SUPPORT` that need no brand flag, so the renderer, the Creative and the
Critic share one matrix with the asset checks. Email has no video because most inboxes do not play
it; WhatsApp has no carousel because a template header holds one medium (a carousel template is a
separate, opt-in Meta product); SMS is words only."""

FORMAT_MEDIA: dict[Format, frozenset[str]] = {
    Format.TEXT_IMAGE: frozenset({"image"}),
    Format.CAROUSEL: frozenset({"carousel_card", "image"}),
    Format.VIDEO: frozenset({"video"}),
}
"""The kinds of approved asset each non-text format may be built from."""

CAROUSEL_MIN = 2
"""A carousel of one card is just an image, and should be labelled as one."""

CAROUSEL_MAX = 10
"""The most cards any channel takes (WhatsApp's limit; email guidance is the same)."""


def asset_urls(asset_url: str | None) -> tuple[str, ...]:
    """The media URLs an item's `asset_url` holds: one for an image or video, several for a carousel."""
    return tuple((asset_url or "").split())


def join_asset_urls(urls: Iterable[str]) -> str | None:
    """The `asset_url` value for these URLs (space-joined), or None when there are none."""
    kept = [u.strip() for u in urls if u.strip()]
    return " ".join(kept) or None


class RenderError(ValueError):
    """The item cannot be rendered safely for this customer; the dispatcher skips the decision."""


@dataclass(frozen=True)
class HeaderMedia:
    """A WhatsApp template's header parameter: the one image or video shown above the body."""

    kind: MediaKind
    link: str

    def component(self) -> dict[str, Any]:
        """The `header` component of a Cloud API template message."""
        return {"type": "header", "parameters": [{"type": self.kind, self.kind: {"link": self.link}}]}


@dataclass(frozen=True)
class Rendered:
    """A filled-in item: what goes into a `Message`.

    `template_params` holds the slot values in the order their placeholders first appear in the
    body, which is the order of a WhatsApp template's `{{1}}`, `{{2}}`, ... It is empty unless the
    item names a pre-approved template.

    `asset_url` is the item's media exactly as approved (space-joined card URLs for a carousel) and
    `asset_urls` the same split into a list. `header_media` is set for a WhatsApp template with media.
    """

    subject: str | None
    body: str
    template_params: tuple[str, ...]
    format: Format = Format.TEXT
    asset_url: str | None = None
    asset_urls: tuple[str, ...] = ()
    header_media: HeaderMedia | None = None


def clean_value(value: str) -> str:
    """One line of plain text: control characters dropped, line breaks and tabs made spaces, capped length.

    WhatsApp rejects template parameters with newlines, tabs or more than four spaces in a row, and
    an email subject must be a single line, so the same cleaning suits every channel.
    """
    kept = "".join(
        " " if ch in _SPACE_LIKE else ch for ch in value if ch in _SPACE_LIKE or not _is_control(ch)
    )
    return " ".join(kept.split())[:MAX_VALUE_LEN].strip()


_SPACE_LIKE = frozenset("\t\n\r\v\f\x1c\x1d\x1e\x1f\x85")


def _is_control(ch: str) -> bool:
    # Only C0/C1 control characters (category Cc). Format characters such as the zero-width joiner
    # stay, because emoji sequences and some scripts need them.
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F


def _first_name(state: CustomerState, profile: Mapping[str, str], offer: OfferOption | None) -> str:
    del state, offer
    name = clean_value(profile.get("first_name", ""))
    if not name:
        full = clean_value(profile.get("name", ""))
        name = full.split(" ")[0] if full else ""
    return name or FALLBACK_FIRST_NAME


def _top_category(state: CustomerState, profile: Mapping[str, str], offer: OfferOption | None) -> str:
    del offer
    for candidate in (*state.top_categories, profile.get("top_category", "")):
        value = clean_value(candidate)
        if value:
            return value
    return FALLBACK_TOP_CATEGORY


def _recommended_product(state: CustomerState, profile: Mapping[str, str], offer: OfferOption | None) -> str:
    # Chosen upstream (a recommender or the decision's item) and handed over in the profile; the
    # renderer only fills it, never chooses it.
    del state, offer
    return clean_value(profile.get("recommended_product", "")) or FALLBACK_RECOMMENDED_PRODUCT


def _offer_label(state: CustomerState, profile: Mapping[str, str], offer: OfferOption | None) -> str:
    del state, profile
    if offer is None or offer.kind is OfferKind.NONE:
        raise RenderError("the content mentions an offer ({{offer_label}}) but the decision has none")
    label = clean_value(offer.label)
    if not label:
        raise RenderError(f"offer {offer.offer_id!r} has an empty label")
    return label


SlotFiller = Callable[[CustomerState, Mapping[str, str], OfferOption | None], str]

SLOT_FILLERS: dict[str, SlotFiller] = {
    "first_name": _first_name,
    "top_category": _top_category,
    "offer_label": _offer_label,
    "recommended_product": _recommended_product,
}
"""Every slot the renderer knows. `weekday` is handled apart because it needs `now`."""

KNOWN_SLOTS: frozenset[str] = frozenset({*SLOT_FILLERS, "weekday"})


def slots_in(text: str | None) -> list[str]:
    """Slot names in order of first appearance, without repeats."""
    if not text:
        return []
    seen: dict[str, None] = {}
    for match in SLOT_PATTERN.finditer(text):
        seen.setdefault(match.group(1), None)
    return list(seen)


def _weekday(state: CustomerState, now: datetime) -> str:
    try:
        local = now.astimezone(ZoneInfo(state.timezone))
    except (KeyError, ValueError):
        local = now
    return local.strftime("%A")


def _media(item: ContentItem) -> tuple[tuple[str, ...], HeaderMedia | None]:
    """The item's media URLs and, for a WhatsApp template, its header; refuse what cannot be sent.

    A text item that still carries an `asset_url` (content written before formats were labelled) is
    passed through as an image, as before, so old content keeps rendering the way it did.
    """
    urls = asset_urls(item.asset_url)
    if item.format is not Format.TEXT:
        if item.format not in FORMAT_SUPPORT.get(item.channel, (Format.TEXT,)):
            raise RenderError(
                f"content {item.content_id} is {item.format.value}, which {item.channel.value} cannot carry"
            )
        if not urls:
            raise RenderError(f"content {item.content_id} is {item.format.value} but has no asset")
        if item.format is not Format.CAROUSEL and len(urls) != 1:
            raise RenderError(f"content {item.content_id} is {item.format.value} but has {len(urls)} assets")
    header: HeaderMedia | None = None
    if item.channel is Channel.WHATSAPP and item.template_name and urls:
        if len(urls) != 1:
            raise RenderError(f"content {item.content_id}: a WhatsApp template header holds one medium")
        kind: MediaKind = "video" if item.format is Format.VIDEO else "image"
        header = HeaderMedia(kind=kind, link=urls[0])
    return urls, header


def render(
    item: ContentItem,
    state: CustomerState,
    profile: Mapping[str, str],
    offer: OfferOption | None,
    now: datetime,
) -> Rendered:
    """Fill the item's slots for one customer and carry its approved media along.

    Raises `RenderError` for an unknown slot, an `offer_label` slot without an offer, or a format
    the channel cannot carry (or that lacks its asset), so a broken message is skipped and reported
    rather than sent.
    """
    for text in (item.subject or "", item.body):
        if "{{" in SLOT_PATTERN.sub("", text) or "}}" in SLOT_PATTERN.sub("", text):
            raise RenderError(f"content {item.content_id} has a malformed {{{{slot}}}} placeholder")
    used = slots_in(f"{item.subject or ''}\n{item.body}")
    unknown = [s for s in used if s not in KNOWN_SLOTS]
    if unknown:
        raise RenderError(f"content {item.content_id} uses unknown slot(s): {', '.join(sorted(unknown))}")

    values: dict[str, str] = {}
    for slot in used:
        values[slot] = (
            _weekday(state, now) if slot == "weekday" else SLOT_FILLERS[slot](state, profile, offer)
        )

    def fill(text: str) -> str:
        return SLOT_PATTERN.sub(lambda m: values[m.group(1)], text)

    subject = fill(item.subject) if item.subject is not None else None
    body = fill(item.body)
    params = tuple(values[s] for s in slots_in(item.body)) if item.template_name else ()
    urls, header = _media(item)
    return Rendered(
        subject=subject,
        body=body,
        template_params=params,
        format=item.format,
        asset_url=join_asset_urls(urls),
        asset_urls=urls,
        header_media=header,
    )


def to_message(
    rendered: Rendered,
    item: ContentItem,
    *,
    message_id: str,
    decision_id: str,
    customer_id: str,
    to: str,
    send_at: datetime,
) -> Message:
    """The `Message` for a rendered item, media included, so every caller builds it the same way."""
    return Message(
        message_id=message_id,
        decision_id=decision_id,
        customer_id=customer_id,
        channel=item.channel,
        to=to,
        subject=rendered.subject,
        body=rendered.body,
        asset_url=rendered.asset_url,
        template_name=item.template_name,
        template_params=rendered.template_params,
        send_at=send_at,
    )
