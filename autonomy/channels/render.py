"""Turn an approved content item into the words one customer receives.

The Creative writes bodies with `{{slot}}` placeholders and the Critic approves them as written. At
send time we only fill those slots, from customer state and a small profile (name and the like),
so nothing the Critic has not seen can slip in. Three rules keep that true:

- Only known slots are filled. A placeholder we do not know is an error, never left in the text
  for a customer to see.
- Every slot has a safe fallback ("Hi there" rather than "Hi ,"), except `offer_label`: a message
  that promises an offer the decision did not grant is refused instead.
- Filled values are plain text: control characters are stripped and whitespace collapsed to one
  line. Nothing is HTML-escaped here; an email adapter that builds an HTML part escapes the whole
  body itself, and WhatsApp template parameters are sent as given.

Rendering is pure: `now` is passed in (for the `weekday` slot) and nothing reads the clock.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from autonomy.contracts import ContentItem, CustomerState, OfferKind, OfferOption

SLOT_PATTERN = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
"""A `{{slot}}` placeholder, spaces inside the braces allowed."""

MAX_VALUE_LEN = 120
"""Longest filled value. A runaway profile field must not turn a short message into a wall of text."""

FALLBACK_FIRST_NAME = "there"
FALLBACK_TOP_CATEGORY = "our latest picks"


class RenderError(ValueError):
    """The item cannot be rendered safely for this customer; the dispatcher skips the decision."""


@dataclass(frozen=True)
class Rendered:
    """A filled-in item: what goes into a `Message`.

    `template_params` holds the slot values in the order their placeholders first appear in the
    body, which is the order of a WhatsApp template's `{{1}}`, `{{2}}`, ... It is empty unless the
    item names a pre-approved template.
    """

    subject: str | None
    body: str
    template_params: tuple[str, ...]


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


def render(
    item: ContentItem,
    state: CustomerState,
    profile: Mapping[str, str],
    offer: OfferOption | None,
    now: datetime,
) -> Rendered:
    """Fill the item's slots for one customer.

    Raises `RenderError` for an unknown slot or an `offer_label` slot without an offer, so a broken
    message is skipped and reported rather than sent.
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
    return Rendered(subject=subject, body=body, template_params=params)
