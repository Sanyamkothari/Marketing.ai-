"""The brand kit's hard rules as a check, and its voice applied to fixed template copy.

`voice_problems(item, rules, offer)` is what the Critic adds to its own rules: every way a message
breaks the shop's own habits, in plain words. `apply_voice(subject, body, ...)` turns the Creative's
fixed template copy (used when no model is configured) into the shop's voice: its greeting, its
sign-off, a subject that fits its length. Both are pure code.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from autonomy.brand.kit import DEFAULT_GREETING, DISCOUNT_KINDS, VoiceRules, VoiceStyle
from autonomy.brand.text import count_emoji
from autonomy.contracts import Channel, ContentItem, OfferKind, OfferOption

__all__ = ["apply_voice", "fits_subject", "offer_in_style", "voice_problems"]

_SPACES = re.compile(r"[\s\-‐-―]+")
_TEMPLATE_GREETING = "Hi {{first_name}}, "
_FORM_WORDS: dict[OfferKind, str] = {
    OfferKind.PERCENT_OFF: "a percentage off",
    OfferKind.AMOUNT_OFF: "money off",
    OfferKind.FREE_SHIPPING: "free shipping",
}


def _normalise(text: str) -> str:
    return _SPACES.sub(" ", text.lower().replace("’", "'")).strip()


def offer_in_style(offer: OfferOption | None, rules: VoiceRules | None) -> bool:
    """False when the offer is a discount form the shop's own copy never uses."""
    if offer is None or rules is None or rules.discount_kinds is None:
        return True
    return offer.kind not in DISCOUNT_KINDS or offer.kind in rules.discount_kinds


def voice_problems(item: ContentItem, rules: VoiceRules | None, offer: OfferOption | None) -> list[str]:
    """Every hard rule of the brand kit the item breaks, in plain words. Empty: it fits the shop's voice."""
    if rules is None:
        return []
    problems: list[str] = []
    subject = item.subject or ""
    text = f"{subject}\n{item.body}"
    flat = _normalise(text)
    for phrase in rules.banned_words:
        if re.search(rf"(?<![\w']){re.escape(_normalise(phrase))}(?![\w'])", flat):
            problems.append(f"the shop's own copy never says {phrase!r}")
    if item.channel is Channel.EMAIL and rules.subject_max is not None and len(subject) > rules.subject_max:
        problems.append(
            f"the subject is {len(subject)} characters; the shop's own subjects stay within {rules.subject_max}"
        )
    if item.channel is Channel.SMS and rules.sms_body_max is not None and len(item.body) > rules.sms_body_max:
        problems.append(
            f"the text is {len(item.body)} characters; the shop's own texts stay within {rules.sms_body_max}"
        )
    if rules.max_emoji is not None:
        used = count_emoji(text)
        if used > rules.max_emoji:
            problems.append(
                f"uses {used} emoji; the shop's own messages use "
                + ("none" if rules.max_emoji == 0 else f"at most {rules.max_emoji}")
            )
    if rules.max_exclamations is not None:
        bangs = text.count("!")
        if bangs > rules.max_exclamations:
            problems.append(
                f"uses {bangs} exclamation marks; the shop's own messages use "
                + ("none" if rules.max_exclamations == 0 else f"at most {rules.max_exclamations}")
            )
    if not offer_in_style(offer, rules) and offer is not None:
        forms = rules.discount_kinds or ()
        problems.append(
            f"the offer is {_FORM_WORDS.get(offer.kind, offer.kind.value)}; "
            + (
                "the shop's own copy never states a discount"
                if not forms
                else f"the shop's own copy only states {' or '.join(_FORM_WORDS[k] for k in forms)}"
            )
        )
    return problems


def fits_subject(subject: str | None, rules: VoiceRules | None, ceiling: int) -> bool:
    if subject is None:
        return True
    limit = (
        min(ceiling, rules.subject_max) if rules is not None and rules.subject_max is not None else ceiling
    )
    return len(subject) <= limit


def apply_voice(
    subject: str | None,
    body: str,
    *,
    channel: Channel,
    style: VoiceStyle | None,
    rules: VoiceRules | None,
    subjects: Sequence[str] = (),
    ceiling: int = 70,
) -> tuple[str | None, str] | None:
    """Template copy in the shop's voice, or None when no subject fits its length.

    The template bodies open with 'Hi {{first_name}}, '; that opening becomes the shop's greeting
    (or goes, when the shop does not greet). An email gets the shop's sign-off. A subject longer
    than the shop's limit is swapped for the shortest of `subjects` (the theme's other template
    subjects) that fits; if none fits, the variant is dropped (None). A text message longer than
    the shop's own texts starts straight in, without the greeting; if it is still too long, the
    variant is dropped (None)."""
    if style is not None and body.startswith(_TEMPLATE_GREETING) and style.greeting != DEFAULT_GREETING:
        rest = body[len(_TEMPLATE_GREETING) :]
        body = f"{style.greeting} {rest}" if style.greeting else rest[:1].upper() + rest[1:]
    if channel is Channel.SMS and rules is not None and rules.sms_body_max is not None:
        if len(body) > rules.sms_body_max and style is not None and style.greeting:
            opening = style.greeting if style.greeting != DEFAULT_GREETING else _TEMPLATE_GREETING.strip()
            if body.startswith(opening):
                rest = body[len(opening) :].lstrip()
                body = rest[:1].upper() + rest[1:]
        if len(body) > rules.sms_body_max:
            return None
    if style is not None and channel is Channel.EMAIL and style.sign_off:
        body = f"{body}\n\n{style.sign_off}" + (f"\n{style.signer}" if style.signer else "")
    if channel is Channel.EMAIL and not fits_subject(subject, rules, ceiling):
        fitting = sorted((s for s in subjects if fits_subject(s, rules, ceiling)), key=len)
        if not fitting:
            return None
        subject = fitting[0]
    return subject, body
