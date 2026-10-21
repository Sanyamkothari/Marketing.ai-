"""From what was read to a `BrandKit`: deterministic, so the same copy always gives the same kit.

Every rule is the shop's own behaviour, counted:

| Rule (hard: the Critic enforces it)  | From                                                     | Needs at least |
|--------------------------------------|----------------------------------------------------------|----------------|
| banned words                         | `AVOID_CANDIDATES` the shop's copy never uses            | 8 messages     |
| subject length                       | 9 in 10 of their subjects, rounded up to 5 (35 to 70)    | 5 subjects     |
| SMS length                           | 9 in 10 of their SMS bodies (120 to 160)                 | 5 SMS          |
| emoji per message                    | the most any of their messages uses                      | 8 messages     |
| '!' per message                      | 9 in 10 of their messages                                | 8 messages     |
| discount forms                       | the forms their copy states (percent, money, free ship.) | 8 messages     |

With less copy than that, the rule is left out (None) and `notes` says so: nothing is invented.
The floors (35 characters for a subject, 120 for an SMS) only ever loosen a rule, so a shop with
very short subjects still gets messages our template copy can write; the finding says so.

The soft voice (tone, greeting, sign-off, words they use) is measured too. A model may refine the
tone, but only by picking from the same fixed lists (`ToneSummary`), and code accepts a pick only
when it is within one step of what was measured. Its words never become a rule.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any, Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from autonomy.brand.kit import (
    DEFAULT_GREETING,
    AssetSource,
    BrandColour,
    BrandKit,
    BrandLogo,
    CopySample,
    Energy,
    Evidence,
    Finding,
    Formality,
    IncompleteRead,
    KitAsset,
    VoiceRules,
    VoiceStyle,
    Warmth,
)
from autonomy.brand.text import (
    AVOID_CANDIDATES,
    GREETING_WORDS,
    SIGN_OFFS,
    DiscountMentions,
    colours_in,
    contractions,
    count_emoji,
    discounts_in,
    find_greeting,
    find_sign_off,
    logo_in,
    percentile,
    sentences,
    word_tokens,
    words,
)
from autonomy.contracts import Channel, Contract, OfferKind, Tone

__all__ = [
    "MIN_MESSAGES",
    "MIN_SMS",
    "MIN_SUBJECTS",
    "SMS_FLOOR",
    "SUBJECT_FLOOR",
    "BrandSources",
    "ProductImage",
    "ShopFacts",
    "ToneSummary",
    "derive_kit",
    "summarise_tone",
    "tone_for",
]

MIN_MESSAGES = 8
"""Fewer of the shop's messages than this, and "never uses" says nothing: those rules are left out."""
MIN_SUBJECTS = 5
MIN_SMS = 5
MIN_GREETINGS = 5
SUBJECT_FLOOR = 35
"""The shortest subject limit we set: our shortest template subject per theme fits in it."""
SUBJECT_CEILING = 70
"""The same as the Creative's `EMAIL_SUBJECT_MAX`: longer subjects are cut off in most inboxes."""
SMS_FLOOR = 120
SMS_CEILING = 160
MAX_ASSETS = 60
SHOPIFY_SOURCE = "Shopify"
KLAVIYO_COPY = "Klaviyo templates and campaigns"


class ShopFacts(Contract):
    name: str | None = None
    domain: str | None = Field(default=None, description="The permanent .myshopify.com domain")
    primary_host: str | None = Field(default=None, description="The shop's public host, e.g. acme.com")
    currency: str | None = None
    connection_id: str | None = None


class ProductImage(Contract):
    product_id: str
    record_id: str
    title: str
    product_type: str | None = None
    url: str
    alt_text: str = ""
    width: int | None = None
    height: int | None = None
    media_id: str | None = None


class BrandSources(Contract):
    """Everything read for one business, before any rule is derived."""

    business_id: str
    read_at: AwareDatetime
    copy_samples: tuple[CopySample, ...] = ()
    klaviyo_connection_id: str | None = None
    klaviyo_label: str | None = None
    templates_read: int = 0
    campaigns_read: int = 0
    sms_read: int = 0
    shop: ShopFacts | None = None
    shop_logo: BrandLogo | None = None
    shop_colours: tuple[BrandColour, ...] = ()
    products: tuple[ProductImage, ...] = ()
    catalogue_read: bool = Field(
        default=False, description="True: every product page asked for was read (none refused)"
    )
    incomplete: tuple[IncompleteRead, ...] = Field(
        default=(), description="Connected tools whose read was refused or failed part way"
    )
    notes: tuple[str, ...] = ()


class ToneSummary(BaseModel):
    """All a model may say about the voice: picks from fixed lists. Code checks each pick."""

    model_config = ConfigDict(extra="forbid")

    formality: Formality
    energy: Energy
    warmth: Warmth
    humour: bool


_SCALES: dict[str, tuple[str, ...]] = {
    "formality": get_args(Formality),
    "energy": get_args(Energy),
    "warmth": get_args(Warmth),
}

TONE_SYSTEM = (
    "You read a shop's own marketing emails and describe their voice by picking from fixed lists. "
    "You never write rules or new words; you only pick."
)


def tone_for(formality: str, energy: str) -> Tone:
    """The content tone label a voice reads as. Never URGENT: urgency is a theme, not a brand voice."""
    if formality == "formal":
        return Tone.PROFESSIONAL
    if formality == "casual" and energy == "excited":
        return Tone.PLAYFUL
    if energy == "calm" and formality != "casual":
        return Tone.CALM
    return Tone.FRIENDLY


def _evidence(source: str, seen: int, of: int, examples: Sequence[str] = ()) -> Evidence:
    return Evidence(source=source, seen=seen, of=of, examples=tuple(e[:80] for e in examples[:3]))


def _round_up(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


def _contains(text: str, phrase: str) -> bool:
    flat = re.sub(r"[\s\-‐-―]+", " ", text.lower().replace("’", "'"))
    target = re.sub(r"[\s\-]+", " ", phrase.lower())
    return re.search(rf"(?<![a-z']){re.escape(target)}(?![a-z'])", flat) is not None


def _full(sample: CopySample) -> str:
    return f"{sample.subject or ''}\n{sample.text}".strip()


def derive_kit(sources: BrandSources, *, llm: Any = None, model_id: str | None = None) -> BrandKit:
    """The kit for what was read. `llm` (optional) may refine the tone within one step of what was measured."""
    copy = list(sources.copy_samples)
    # Greeting, sign-off and the "never uses" rules look at what a message says, so a campaign whose
    # body could not be read (only its subject came back) counts for none of them: an empty body
    # is not evidence that the shop never greets, or never says a word.
    emails = [c for c in copy if c.channel is Channel.EMAIL and c.text.strip()]
    sms = [c for c in copy if c.channel is Channel.SMS]
    n = len(copy)
    with_body = sum(1 for c in copy if c.text.strip())
    findings: list[Finding] = []
    notes = list(sources.notes)
    enough = with_body >= MIN_MESSAGES
    if copy and not enough:
        notes.append(
            f"Only {with_body} of your messages could be read in full; the rules that need at least "
            f"{MIN_MESSAGES} (words you never use, emoji, '!', discount forms) are left out until there are more."
        )
    elif not copy:
        notes.append("None of your messages could be read, so the voice uses our plain defaults.")
    if with_body < n:
        notes.append(
            f"For {n - with_body} of your campaigns only the subject could be read; they count for the "
            "subject length only."
        )

    rules: dict[str, Any] = {}
    style: dict[str, Any] = {}

    # Greeting (emails).
    greetings = [g for g in (find_greeting(c.text) for c in emails) if g is not None]
    if len(emails) >= MIN_GREETINGS:
        if len(greetings) * 3 >= len(emails):
            line, count = Counter(g.line for g in greetings).most_common(1)[0]
            style["greeting"] = line
            findings.append(
                Finding(
                    key="greeting",
                    label="Greeting",
                    found=f"'{line.replace('{{first_name}}', '{first name}')}' opens {count} of {len(emails)} emails",
                    use=f"Messages open '{line.replace('{{first_name}}', '{first name}')}'",
                    hard=False,
                    evidence=_evidence(KLAVIYO_COPY, count, len(emails), [line]),
                )
            )
        else:
            style["greeting"] = ""
            findings.append(
                Finding(
                    key="greeting",
                    label="Greeting",
                    found=f"Only {len(greetings)} of {len(emails)} emails open with a greeting",
                    use="Messages start straight in, with no greeting",
                    hard=False,
                    evidence=_evidence(KLAVIYO_COPY, len(greetings), len(emails)),
                )
            )
    else:
        findings.append(
            Finding(
                key="greeting",
                label="Greeting",
                found=f"{len(emails)} emails read, too few to tell how you open",
                use=f"Messages open '{DEFAULT_GREETING.replace('{{first_name}}', '{first name}')}'",
                hard=False,
                evidence=_evidence(KLAVIYO_COPY, len(greetings), len(emails)),
            )
        )

    # Sign-off (emails).
    offs = [s for s in (find_sign_off(c.text) for c in emails) if s is not None]
    if offs and len(emails) >= MIN_GREETINGS:
        pairs = Counter((s.phrase, s.signer) for s in offs)
        (phrase, signer), count = pairs.most_common(1)[0]
        if count < 2 or count * 3 < len(emails):
            phrases = Counter(s.phrase for s in offs)
            phrase, count = phrases.most_common(1)[0]
            signer = None
        if count >= 2 and count * 3 >= len(emails):
            style["sign_off"] = phrase
            style["signer"] = signer
            shown = f"{phrase} {signer}" if signer else phrase
            findings.append(
                Finding(
                    key="sign_off",
                    label="Sign-off",
                    found=f"'{shown}' closes {count} of {len(emails)} emails",
                    use=f"Emails close '{shown}'",
                    hard=False,
                    evidence=_evidence(KLAVIYO_COPY, count, len(emails), [shown]),
                )
            )

    # Subject length (email campaigns).
    subjects = [c.subject.strip() for c in copy if c.subject and c.subject.strip()]
    if len(subjects) >= MIN_SUBJECTS:
        lengths = [len(s) for s in subjects]
        p90 = int(percentile(lengths, 0.9))
        limit = min(SUBJECT_CEILING, max(SUBJECT_FLOOR, _round_up(p90, 5)))
        rules["subject_max"] = limit
        within = sum(1 for x in lengths if x <= limit)
        floor_note = f" (our floor: {SUBJECT_FLOOR})" if _round_up(p90, 5) < SUBJECT_FLOOR else ""
        findings.append(
            Finding(
                key="subject_max",
                label="Subject length",
                found=f"9 in 10 of your {len(subjects)} subjects are {p90} characters or fewer",
                use=f"Subjects at most {limit} characters{floor_note}",
                hard=True,
                evidence=_evidence(
                    "Klaviyo campaigns", within, len(subjects), sorted(subjects, key=len)[-2:]
                ),
            )
        )

    # SMS length.
    if len(sms) >= MIN_SMS:
        p90 = int(percentile([len(c.text) for c in sms], 0.9))
        limit = min(SMS_CEILING, max(SMS_FLOOR, _round_up(p90, 10)))
        rules["sms_body_max"] = limit
        sms_within = sum(1 for c in sms if len(c.text) <= limit)
        findings.append(
            Finding(
                key="sms_body_max",
                label="Text message length",
                found=f"9 in 10 of your {len(sms)} text messages are {p90} characters or fewer",
                use=f"Text messages at most {limit} characters",
                hard=True,
                evidence=_evidence("Klaviyo SMS campaigns", sms_within, len(sms)),
            )
        )

    # From here on, "your messages" are the ones read in full (subject and body).
    texts = [_full(c) for c in copy if c.text.strip()]
    n = len(texts)
    emoji = [count_emoji(t) for t in texts]
    bangs = [t.count("!") for t in texts]
    mentions: list[DiscountMentions] = [discounts_in(t) for t in texts]

    if enough:
        # Emoji.
        most = max(emoji)
        rules["max_emoji"] = most
        with_emoji = sum(1 for e in emoji if e)
        findings.append(
            Finding(
                key="max_emoji",
                label="Emoji",
                found=(
                    f"None of your {n} messages uses an emoji"
                    if most == 0
                    else f"{with_emoji} of your {n} messages use emoji, at most {most} in one"
                ),
                use="No emoji" if most == 0 else f"At most {most} emoji per message",
                hard=True,
                evidence=_evidence(KLAVIYO_COPY, with_emoji, n),
            )
        )
        # Exclamation marks.
        p90 = int(percentile(bangs, 0.9))
        rules["max_exclamations"] = p90
        findings.append(
            Finding(
                key="max_exclamations",
                label="Exclamation marks",
                found=(
                    f"9 in 10 of your {n} messages use none"
                    if p90 == 0
                    else f"9 in 10 of your {n} messages use {p90} or fewer"
                ),
                use="No exclamation marks" if p90 == 0 else f"At most {p90} per message",
                hard=True,
                evidence=_evidence(KLAVIYO_COPY, sum(1 for b in bangs if b <= p90), n),
            )
        )
        # Words they never use.
        banned = tuple(w for w in AVOID_CANDIDATES if not any(_contains(t, w) for t in texts))
        rules["banned_words"] = banned
        if banned:
            findings.append(
                Finding(
                    key="banned_words",
                    label="Words you never use",
                    found=f"{len(banned)} common hype words appear in none of your {n} messages",
                    use=f"Never: {', '.join(banned[:8])}{' and more' if len(banned) > 8 else ''}",
                    hard=True,
                    evidence=_evidence(KLAVIYO_COPY, 0, n),
                )
            )
        # Discount forms.
        has_pct = [m for m in mentions if m.percents]
        has_amt = [m for m in mentions if m.amounts]
        has_ship = [m for m in mentions if m.free_shipping]
        kinds = tuple(
            k
            for k, rows in (
                (OfferKind.PERCENT_OFF, has_pct),
                (OfferKind.AMOUNT_OFF, has_amt),
                (OfferKind.FREE_SHIPPING, has_ship),
            )
            if rows
        )
        rules["discount_kinds"] = kinds
        discounting = sum(1 for m in mentions if m.any)
        if not kinds:
            findings.append(
                Finding(
                    key="discount_kinds",
                    label="Discount style",
                    found=f"None of your {n} messages states a discount",
                    use="No discount offers in messages",
                    hard=True,
                    evidence=_evidence(KLAVIYO_COPY, 0, n),
                )
            )
        else:
            percents = [p for m in mentions for p in m.percents]
            amounts = [a for m in mentions for a in m.amounts]
            if percents and len(percents) >= len(amounts):
                figure, times = Counter(percents).most_common(1)[0]
                phrase = Counter(p for m in mentions for p in m.phrases).most_common(1)[0][0]
                style["usual_discount"] = figure
                style["usual_discount_kind"] = OfferKind.PERCENT_OFF
                style["discount_phrase"] = phrase
                usual = f"; most often '{phrase.format(n=_figure(figure))}' ({times} times)"
            elif amounts:
                figure, times = Counter(amounts).most_common(1)[0]
                style["usual_discount"] = figure
                style["usual_discount_kind"] = OfferKind.AMOUNT_OFF
                usual = f"; most often {_figure(figure)} off ({times} times)"
            else:
                usual = ""
            forms = [_FORM[k] for k in kinds]
            left = [
                _FORM[k]
                for k in (OfferKind.PERCENT_OFF, OfferKind.AMOUNT_OFF, OfferKind.FREE_SHIPPING)
                if k not in kinds
            ]
            findings.append(
                Finding(
                    key="discount_kinds",
                    label="Discount style",
                    found=f"{discounting} of your {n} messages state a discount, as {_join(forms)}{usual}",
                    use=f"Offers only as {_join(forms)}" + (f"; no {_join(left)}" if left else ""),
                    hard=True,
                    evidence=_evidence(KLAVIYO_COPY, discounting, n),
                )
            )

    # A greeting or sign-off may not break the '!' rule it sits next to: when most of the shop's
    # messages use no '!' but its most common greeting has one, the greeting keeps its words and
    # takes a comma, so the copy written in the voice can pass the Critic.
    bang_budget = rules.get("max_exclamations")
    if bang_budget is not None:
        for key in ("greeting", "sign_off"):
            said = style.get(key)
            if not isinstance(said, str) or "!" not in said:
                continue
            if said.count("!") <= bang_budget:
                bang_budget -= said.count("!")
                continue
            fixed = re.sub(r"!+", ",", said).replace(",,", ",")
            style[key] = fixed
            if key == "greeting":
                use = f"Messages open '{fixed.replace('{{first_name}}', '{first name}')}'"
            else:
                use = f"Emails close '{fixed}{' ' + style['signer'] if style.get('signer') else ''}'"
            use += f" (with a comma: 9 in 10 of your messages use at most {rules['max_exclamations']} '!')"
            findings = [f.model_copy(update={"use": use}) if f.key == key else f for f in findings]

    # Words they use.
    df: Counter[str] = Counter()
    for t in texts:
        df.update(word_tokens(t))
    threshold = max(3, math.ceil(0.2 * n))
    avoid = {w for w in AVOID_CANDIDATES if " " not in w}
    # Their name, greeting and sign-off are shown on their own rows; they say nothing about wording.
    own = " ".join(
        str(x or "")
        for x in (
            sources.shop.name if sources.shop else None,
            sources.klaviyo_label,
            style.get("greeting"),
            style.get("sign_off"),
            style.get("signer"),
        )
    )
    avoid |= word_tokens(own) | {w.replace("'", "") for w in GREETING_WORDS} | set(SIGN_OFFS)
    used = [
        w for w, c in sorted(df.items(), key=lambda kv: (-kv[1], kv[0])) if c >= threshold and w not in avoid
    ]
    if used:
        style["words_they_use"] = tuple(used[:10])
        findings.append(
            Finding(
                key="words_they_use",
                label="Words you use",
                found=f"{', '.join(used[:6])} appear across your messages",
                use="The Creative uses your words where they fit",
                hard=False,
                evidence=_evidence(KLAVIYO_COPY, df[used[0]], n, used[:3]),
            )
        )

    # Tone, measured.
    chosen = str(style.get("greeting", DEFAULT_GREETING))
    measured = _measured_tone(texts, chosen.split()[0].rstrip(",!.:") if chosen else None, bool(chosen))
    style.update(measured)
    tone_style = VoiceStyle(**style)
    if llm is not None and copy:
        tone_style, note = summarise_tone(llm, copy, tone_style, model_id=model_id)
        if note:
            notes.append(note)
    if texts:
        w = sum(len(words(t)) for t in texts) or 1
        findings.append(
            Finding(
                key="tone",
                label="Tone",
                found=(
                    f"{contractions(' '.join(texts)) * 100 / w:.1f} contractions per 100 words, "
                    f"{sum(bangs) / n:.1f} '!' and {sum(emoji) / n:.1f} emoji per message"
                ),
                use=f"{tone_style.tone.value.capitalize()}: {tone_style.formality}, {tone_style.energy}, {tone_style.warmth}",
                hard=False,
                evidence=_evidence(KLAVIYO_COPY, n, n),
            )
        )

    # Shop, logo, colours, products.
    shop = sources.shop
    shop_name = (shop.name if shop else None) or sources.klaviyo_label
    logo = sources.shop_logo or _logo_from_templates(copy)
    colours = sources.shop_colours or _colours_from_templates(copy)
    if logo is not None:
        findings.append(
            Finding(
                key="logo",
                label="Logo",
                found=f"Your logo, from {logo.source}",
                use="Shown on emails we send",
                hard=False,
                evidence=_evidence(logo.source, 1, 1),
            )
        )
    if colours:
        findings.append(
            Finding(
                key="colours",
                label="Colours",
                found=f"{_join([c.hex for c in colours])}, from {colours[0].source}",
                use="Used for buttons and links in our emails",
                hard=False,
                evidence=(
                    _evidence(colours[0].source, 1, 1)
                    if sources.shop_colours
                    else _evidence(
                        colours[0].source,
                        colours[0].seen,
                        max(colours[0].seen, sum(1 for c in copy if c.html)),
                    )
                ),
            )
        )
    assets = _assets(sources)
    if sources.shop is not None:
        findings.append(
            Finding(
                key="assets",
                label="Product photos",
                found=(
                    f"{len(assets)} product photos from the {len(sources.products)} active products read "
                    "(the most recently updated first)"
                ),
                use="Messages may show them once you approve this voice",
                hard=False,
                evidence=_evidence("Shopify products", len(assets), len(sources.products)),
            )
        )
    types = Counter(p.product_type for p in sources.products if p.product_type)
    product_types = tuple(t for t, _ in sorted(types.items(), key=lambda kv: (-kv[1], kv[0])))

    lines: list[str] = []
    if sources.klaviyo_connection_id:
        lines.append(
            f"Klaviyo: {sources.templates_read} templates, {sources.campaigns_read} email campaigns, "
            f"{sources.sms_read} text campaigns"
        )
    if shop is not None:
        lines.append(f"Shopify: {shop.name or shop.domain}, {len(sources.products)} products")
    return BrandKit(
        business_id=sources.business_id,
        read_at=sources.read_at,
        shop_name=shop_name,
        shop_domain=(shop.primary_host or shop.domain) if shop else None,
        currency=shop.currency if shop else None,
        sources=tuple(lines),
        messages_read=len(copy),
        rules=VoiceRules(**rules),
        style=tone_style,
        findings=tuple(findings),
        colours=tuple(colours),
        logo=logo,
        assets=assets,
        product_types=product_types,
        notes=tuple(dict.fromkeys(notes)),
        catalogue_read=sources.catalogue_read,
        incomplete=sources.incomplete,
    )


_FORM: dict[OfferKind, str] = {
    OfferKind.PERCENT_OFF: "percentages",
    OfferKind.AMOUNT_OFF: "money off",
    OfferKind.FREE_SHIPPING: "free shipping",
}


def _figure(value: float) -> str:
    return f"{value:g}"


def _join(items: Sequence[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _measured_tone(texts: Sequence[str], greeting_word: str | None, greets: bool) -> dict[str, Any]:
    """Formality, energy and warmth from counts. The cut-offs are code's, and the finding shows the counts."""
    if not texts:
        return {}
    joined = "\n".join(texts)
    w = len(words(joined)) or 1
    per100 = contractions(joined) * 100 / w
    n = len(texts)
    bangs = joined.count("!") / n
    emoji = count_emoji(joined) / n
    you = sum(1 for t in words(joined) if t.lower() in ("you", "your", "you're", "you'll", "yours")) * 100 / w
    avg_sentence = w / sum(sentences(t) for t in texts)
    casual_word = (greeting_word or "").lower() in ("hey", "hiya", "howdy", "g'day")
    if per100 >= 1.5 or casual_word:
        formality = "casual"
    elif per100 < 0.3 and ((greeting_word or "").lower() == "dear" or avg_sentence >= 20):
        formality = "formal"
    else:
        formality = "neutral"
    if bangs >= 1.5 or emoji >= 1:
        energy = "excited"
    elif bangs < 0.3 and emoji == 0:
        energy = "calm"
    else:
        energy = "upbeat"
    if you >= 3.5 and greets:
        warmth = "warm"
    elif you < 1.5 and not greets:
        warmth = "reserved"
    else:
        warmth = "neutral"
    return {
        "formality": formality,
        "energy": energy,
        "warmth": warmth,
        "tone": tone_for(formality, energy),
    }


def summarise_tone(
    llm: Any, copy: Sequence[CopySample], measured: VoiceStyle, *, model_id: str | None = None
) -> tuple[VoiceStyle, str | None]:
    """The measured voice, with a model's picks accepted where they are within one step of it.

    Returns the style and a note when the model was not used (unreachable, unreadable, or every pick
    too far from the counts). The model's words are never kept: only its picks from fixed lists."""
    from autonomy.agents.base import AgentOutputError, ask_json

    excerpts = "\n\n".join(
        f"Message {i}:\nSubject: {c.subject or '(none)'}\n{c.text[:400]}" for i, c in enumerate(copy[:12], 1)
    )
    prompt = (
        "These are a shop's own marketing messages. Pick how their voice reads:\n"
        "- formality: casual, neutral or formal\n- energy: calm, upbeat or excited\n"
        "- warmth: reserved, neutral or warm\n- humour: true if they joke, else false\n\n" + excerpts
    )
    try:
        summary = ask_json(
            llm, ToneSummary, system=TONE_SYSTEM, prompt=prompt, model_id=model_id, max_tokens=200
        )
    except AgentOutputError:
        return measured, "The model's tone summary could not be read; the tone is measured from your copy."
    except Exception:  # the model is unreachable: the measured tone stands
        return measured, "The model could not be reached; the tone is measured from your copy."
    picks: dict[str, Any] = {}
    refused: list[str] = []
    for name, scale in _SCALES.items():
        mine, theirs = getattr(measured, name), getattr(summary, name)
        if abs(scale.index(mine) - scale.index(theirs)) <= 1:
            picks[name] = theirs
        else:
            refused.append(f"{name} '{theirs}' (measured '{mine}')")
    picks["humour"] = summary.humour
    style = measured.model_copy(
        update={
            **picks,
            "tone": tone_for(
                picks.get("formality", measured.formality), picks.get("energy", measured.energy)
            ),
            "tone_source": "measured_and_model",
        }
    )
    note = (
        f"The model's tone picks too far from your copy were not used: {', '.join(refused)}."
        if refused
        else None
    )
    return style, note


def _logo_from_templates(copy: Sequence[CopySample]) -> BrandLogo | None:
    found = Counter(hit for c in copy if c.html for hit in [logo_in(c.html)] if hit is not None)
    if not found:
        return None
    (url, alt), _count = found.most_common(1)[0]
    return BrandLogo(url=url, alt_text=alt, source="your Klaviyo email templates")


def _colours_from_templates(copy: Sequence[CopySample]) -> list[BrandColour]:
    templates = [c for c in copy if c.html]
    per: Counter[str] = Counter()
    for c in templates:
        per.update(set(colours_in(c.html or "")))
    need = 2 if len(templates) >= 2 else 1
    ranked = [(hex6, k) for hex6, k in sorted(per.items(), key=lambda kv: (-kv[1], kv[0])) if k >= need]
    roles: tuple[Literal["primary", "secondary", "accent"], ...] = ("primary", "secondary", "accent")
    return [
        BrandColour(hex=hex6, role=roles[i], source="your Klaviyo email templates", seen=k)
        for i, (hex6, k) in enumerate(ranked[:3])
    ]


def asset_id_for(shop: str | None, product_id: str, media_id: str | None) -> str:
    slug = (
        re.sub(r"[^a-z0-9-]+", "-", (shop or "shop").lower().removesuffix(".myshopify.com")).strip("-")
        or "shop"
    )
    media = re.sub(r"[^A-Za-z0-9]+", "", (media_id or "").rsplit("/", 1)[-1]) or "main"
    return f"shopify-{slug}-{product_id}-{media}"[:200]


def _assets(sources: BrandSources) -> tuple[KitAsset, ...]:
    shop = sources.shop
    out: dict[str, KitAsset] = {}
    for p in sources.products:
        if not p.url.startswith("https://") or any(ch.isspace() for ch in p.url):
            continue
        asset_id = asset_id_for(shop.domain if shop else None, p.product_id, p.media_id)
        out.setdefault(
            asset_id,
            KitAsset(
                asset_id=asset_id,
                url=p.url,
                alt_text=(p.alt_text or p.title).strip()[:200],
                title=p.title,
                width=p.width,
                height=p.height,
                item_id=p.product_id,
                category=p.product_type or None,
                source=AssetSource(
                    platform="shopify",
                    connection_id=shop.connection_id if shop else None,
                    record_id=p.record_id,
                    media_id=p.media_id,
                    read_at=sources.read_at,
                ),
            ),
        )
        if len(out) >= MAX_ASSETS:
            break
    return tuple(out.values())
