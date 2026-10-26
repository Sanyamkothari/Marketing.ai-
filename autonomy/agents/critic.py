"""The Critic: the gate every piece of content passes before it can reach a customer (plan-v2 section 2).

Two stages, cheapest and surest first:

1. **Rules** (code, deterministic): channel length limits, only allowed `{{slots}}`, balanced braces,
   no ALL-CAPS shouting, no banned phrases, no discount numbers other than the approved offer's, no
   links outside the allowed domains, an unsubscribe hint in every email, and for an image,
   carousel or video item: a format the channel can carry, built only from the brand's approved
   assets of the right kind, each with alt text. An item that breaks
   a rule is rejected with the reasons and never reaches the judge: there is no point paying a
   model to score copy we already know is unsendable. When the brand rules name countries, each
   country's compliance pack (`autonomy.compliance_packs`) adds its own rules here too: India's
   DLT template registration and opt-out line, the US's STOP keyword and postal address, the EU's
   unsubscribe link. With no countries named, the rules are exactly the brand's own. When the
   rules carry the shop's brand kit (`BrandRules.voice`, read from its own tools by
   `autonomy.brand`), its hard rules are checked too: words the shop never uses, its subject and
   SMS lengths, emoji, exclamation marks, and the discount forms its copy uses
   (`autonomy.brand.rules.voice_problems`, noted as `brand kit: ...`).
2. **Judge** (an LLM, ideally from a different model family than the Creative, to avoid a model
   grading its own style): scores brand voice and honesty from 1 to 5 with reasons. To reduce
   position and self bias, each item is judged alone (never side by side with others) and twice,
   with the rubric in a different order each time. The item is rejected if either run scores any
   criterion below the threshold.

When the judge cannot be reached (the provider is down, or the call raises), copy that passed the
rules is neither approved nor rejected: it is left DRAFT with a note starting `judge unavailable`,
so a later review can approve it once the judge is back. Nothing is approved without the judge
unless the brand's rules say `judge_optional`. Rule failures still reject, with no model call.

The judge never sees numbers it could change and never decides per customer; it only says yes or
no to a template, with reasons a person can read.

Out of scope for now: checking that the words do not promise something the image or video
contradicts (copy saying "in blue" beside a red shoe). The rules cannot see inside an image; the
judge is shown the asset's alt text, which catches some of it, but nothing here guarantees it.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable, Sequence
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

from autonomy.agents.base import AgentOutputError, ask_json, load_prompt
from autonomy.agents.creative import (
    ALLOWED_SLOTS,
    CHANNEL_BODY_MAX,
    EMAIL_SUBJECT_MAX,
    SLOT_PATTERN,
    ApprovedAsset,
    has_unsubscribe_hint,
    is_approved,
    media_kind,
)
from autonomy.brand.kit import BrandKit, VoiceRules
from autonomy.brand.rules import voice_problems
from autonomy.channels.render import CAROUSEL_MAX, CAROUSEL_MIN, FORMAT_MEDIA, FORMAT_SUPPORT, asset_urls
from autonomy.compliance_packs import CompliancePack, PackSettings, Purpose, packs_for
from autonomy.contracts import (
    NO_OFFER,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    Contract,
    Format,
    OfferKind,
    OfferOption,
)
from engine.llm import LLMClient, LLMError

DEFAULT_BANNED_PHRASES: tuple[str, ...] = (
    "guaranteed",
    "risk-free",
    "act now or lose",
    "once in a lifetime",
    "you have been selected",
    "100% free",
    "no strings attached",
    "last chance ever",
)
"""Phrases that promise what no business can, or that spam filters punish. Matched ignoring case,
and treating hyphens and runs of spaces alike, so 'risk free' and 'Risk-Free' both match."""

DEFAULT_ALLOWED_CAPS: tuple[str, ...] = (
    "FAQ",
    "SALE",
    "VIP",
    "NEW",
    "USA",
    "UK",
    "OK",
    "SMS",
    "PS",
    "STOP",
    "HELP",
    "START",
)
"""All-caps words that are acronyms, ordinary retail words or the SMS opt-out keywords carriers
expect in capitals ('Reply STOP to opt out'), rather than shouting."""

JUDGE_THRESHOLD = 3
"""Scores below this ('acceptable, with a weakness') reject the item."""

RUBRIC: dict[str, str] = {
    "brand_voice": (
        "Does the message sound like the brand voice described above, in wording, "
        "warmth and formality, and fit the channel?"
    ),
    "honesty": (
        "Is every claim true on its face and supportable from what is given here? No "
        "invented deadlines, scarcity, results or claims about other customers, and the offer is "
        "described only as the approved offer."
    ),
}
"""What the judge scores. Kept in code (not the prompt file) because the order is shuffled per run."""

CriterionName = Literal["brand_voice", "honesty"]

_SPACES = re.compile(r"[\s\-‐-―]+")
_SHOUT = re.compile(r"\b[A-Z]{4,}\b")
_BANGS = re.compile(r"!{2,}")
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:%|percent\b|per\s+cent\b)", re.IGNORECASE)
_MONEY_BEFORE = re.compile(r"(?:[$€£₹]|\b(?:usd|eur|gbp|inr|rs\.?)\s?)\s*(\d+(?:[.,]\d+)*)", re.IGNORECASE)
_MONEY_AFTER = re.compile(
    r"(\d+(?:[.,]\d+)*)\s*(?:usd|eur|gbp|inr|dollars?|euros?|pounds?|rupees?)\b", re.IGNORECASE
)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'()]+", re.IGNORECASE)
_BARE_DOMAIN = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com|net|org|io|co|in|uk|us|shop|store|biz|info|ly|me|app|xyz))\b",
    re.IGNORECASE,
)
_URL_TRAILING = ".,;:!?'\")]}>"


class AssetRef(Contract):
    """What the Critic needs to know about one approved asset: enough to check a format item."""

    asset_id: str
    kind: str = Field(description="'image', 'video' or 'carousel_card'")
    url: str
    alt_text: str = ""

    @classmethod
    def of(cls, asset: ApprovedAsset) -> AssetRef:
        return cls(
            asset_id=asset.asset_id,
            kind=media_kind(asset),
            url=asset.url.strip(),
            alt_text=(asset.alt_text or "").strip(),
        )


class BrandRules(Contract):
    """The client's rules for copy. Offers and assets are the approved ones; anything else is unapproved."""

    brand_voice: str = ""
    offers: tuple[OfferOption, ...] = (NO_OFFER,)
    allowed_domains: tuple[str, ...] = Field(
        default=(), description="Link hosts allowed, sub-domains included. Empty: no links at all."
    )
    allowed_slots: tuple[str, ...] = ALLOWED_SLOTS
    banned_phrases: tuple[str, ...] = DEFAULT_BANNED_PHRASES
    allowed_caps: tuple[str, ...] = DEFAULT_ALLOWED_CAPS
    approved_assets: tuple[AssetRef, ...] = Field(
        default=(), description="The brand's approved images and videos. Empty: text items only."
    )
    countries: tuple[str, ...] = Field(
        default=(),
        description=(
            "ISO country codes the content will be sent to; each adds its compliance pack's rules "
            "(a country without a pack adds the strictest of them all). Empty: no pack rules."
        ),
    )
    purpose: Purpose = Field(
        default=Purpose.PROMOTIONAL,
        description="What the packs judge the content as; marketing is promotional",
    )
    pack_settings: PackSettings = Field(
        default_factory=PackSettings,
        description="What the packs need from the brand: registered DLT templates, a postal address",
    )
    judge_optional: bool = Field(
        default=False,
        description=(
            "True lets copy that passes every rule be approved when the judge cannot be reached. "
            "False (the default) holds it as a draft until the judge is back."
        ),
    )
    voice: VoiceRules | None = Field(
        default=None,
        description="The hard rules of the shop's brand kit (autonomy.brand). None: no kit rules.",
    )

    @classmethod
    def from_brief(
        cls,
        brief: Brief,
        *,
        brand_voice: str,
        allowed_domains: Sequence[str] = (),
        assets: Iterable[ApprovedAsset] = (),
        countries: Sequence[str] = (),
        pack_settings: PackSettings | None = None,
        kit: BrandKit | None = None,
    ) -> BrandRules:
        """Rules whose approved offers are the brief's envelope offers, and assets the approved ones given.

        With the shop's brand `kit`, its hard rules are checked too, and its voice joins `brand_voice`
        for the judge."""
        voice = brand_voice
        if kit is not None:
            voice = " ".join(p for p in (brand_voice.strip(), kit.voice_text()) if p)
        return cls(
            brand_voice=voice,
            offers=brief.envelope.offers,
            allowed_domains=tuple(allowed_domains),
            approved_assets=tuple(AssetRef.of(a) for a in assets if is_approved(a)),
            countries=tuple(countries),
            pack_settings=pack_settings or PackSettings(),
            voice=kit.rules if kit is not None else None,
        )

    def compliance_packs(self) -> tuple[CompliancePack, ...]:
        """The packs for `countries`, built with `pack_settings`; empty when no country is named."""
        return packs_for(self.countries, self.pack_settings) if self.countries else ()

    def asset_by_url(self, url: str) -> AssetRef | None:
        return next((a for a in self.approved_assets if a.url == url), None)


class CriterionScore(BaseModel):
    criterion: CriterionName
    score: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1)


class JudgeVerdict(BaseModel):
    scores: list[CriterionScore]

    @model_validator(mode="after")
    def _every_criterion_once(self) -> JudgeVerdict:
        names = sorted(s.criterion for s in self.scores)
        if names != sorted(RUBRIC):
            raise ValueError(f"score each of {sorted(RUBRIC)} exactly once; got {names}")
        return self


def _normalise(text: str) -> str:
    return _SPACES.sub(" ", text.lower()).strip()


def _number(raw: str) -> float:
    """'1,000' is a thousand; '9,99' is nine ninety-nine (comma decimals)."""
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
        return float(raw.replace(",", ""))
    return float(raw.replace(",", "."))


def _hosts(text: str) -> list[str]:
    hosts: list[str] = []
    for token in _URL.findall(text):
        cleaned = token.rstrip(_URL_TRAILING)
        host = urlsplit(cleaned if "://" in cleaned else f"http://{cleaned}").hostname
        hosts.append((host or cleaned).lower())
    hosts.extend(m.lower() for m in _BARE_DOMAIN.findall(_URL.sub(" ", text)))
    return hosts


def _host_allowed(host: str, allowed: Sequence[str]) -> bool:
    return any(host == d or host.endswith(f".{d}") for d in (a.lower().strip() for a in allowed) if d)


def format_problems(item: ContentItem, rules: BrandRules) -> list[str]:
    """What is wrong with the item's format and media; empty for a sound text or media item.

    A media item must be one the channel can carry and must use only approved assets of the
    right kind, each with alt text (for screen readers, and for inboxes that block images). A text
    item must carry no media, so its label means what it says.
    """
    urls = asset_urls(item.asset_url)
    if item.format is Format.TEXT:
        return ["a text item carries an asset; label it with its image or video format"] if urls else []
    problems: list[str] = []
    if item.format not in FORMAT_SUPPORT.get(item.channel, (Format.TEXT,)):
        problems.append(f"{item.channel.value} cannot carry the {item.format.value} format")
    if not urls:
        return [*problems, f"a {item.format.value} item needs an approved asset"]
    if item.format is Format.CAROUSEL:
        if not CAROUSEL_MIN <= len(urls) <= CAROUSEL_MAX:
            problems.append(f"a carousel needs {CAROUSEL_MIN} to {CAROUSEL_MAX} images; it has {len(urls)}")
        if len(set(urls)) != len(urls):
            problems.append("a carousel repeats an image")
    elif len(urls) != 1:
        problems.append(f"a {item.format.value} item uses exactly one asset; it has {len(urls)}")
    kinds = FORMAT_MEDIA[item.format]
    for url in dict.fromkeys(urls):
        asset = rules.asset_by_url(url)
        if asset is None:
            problems.append(f"asset is not an approved asset: {url}")
            continue
        if not asset.alt_text.strip():
            problems.append(f"asset {asset.asset_id} has no alt text")
        if asset.kind not in kinds:
            problems.append(
                f"a {item.format.value} item needs {' or '.join(sorted(kinds))} assets; "
                f"{asset.asset_id} is {asset.kind}"
            )
    return problems


def pack_problems(
    item: ContentItem, rules: BrandRules, packs: Sequence[CompliancePack] | None = None
) -> list[str]:
    """What the country packs say is wrong with the item, each prefixed with the pack's name.

    `packs` overrides the packs `rules.countries` names (a brand with its own pack, or a test).
    """
    chosen = rules.compliance_packs() if packs is None else tuple(packs)
    found = [p for pack in chosen for p in pack.content_problems(item, item.channel, rules.purpose)]
    return list(dict.fromkeys(found))


def check_rules(
    item: ContentItem, rules: BrandRules, *, packs: Sequence[CompliancePack] | None = None
) -> list[str]:
    """Every deterministic rule the item breaks, in plain words. Empty means it may go to the judge.

    The brand's rules first, then the country packs' (none unless `rules.countries` or `packs` names some).
    """
    problems: list[str] = []
    subject = item.subject or ""
    text = f"{subject}\n{item.body}"

    # Length and channel shape.
    if not item.body.strip():
        problems.append("the body is empty")
    limit = CHANNEL_BODY_MAX[item.channel]
    if len(item.body) > limit:
        problems.append(f"the body is {len(item.body)} characters; {item.channel.value} allows {limit}")
    if item.channel is Channel.EMAIL:
        if not subject.strip():
            problems.append("an email needs a subject")
        elif len(subject) > EMAIL_SUBJECT_MAX:
            problems.append(f"the subject is {len(subject)} characters; at most {EMAIL_SUBJECT_MAX}")
        if not has_unsubscribe_hint(item.body):
            problems.append("an email needs an unsubscribe hint")
    if item.channel is Channel.WHATSAPP and not item.template_name:
        problems.append("a WhatsApp message needs a template name")

    # Slots and braces.
    unknown = sorted(set(SLOT_PATTERN.findall(text)) - set(rules.allowed_slots))
    if unknown:
        problems.append(f"unknown slots: {', '.join(unknown)}")
    offer = next((o for o in rules.offers if o.offer_id == item.offer_id), None)
    no_offer = item.offer_id == NO_OFFER.offer_id or (offer is not None and offer.kind is OfferKind.NONE)
    if "offer_label" in SLOT_PATTERN.findall(text) and no_offer:
        problems.append("uses {{offer_label}} but the item has no offer")
    if re.search(r"[{}]", SLOT_PATTERN.sub("", text)):
        problems.append("unbalanced or malformed braces")

    # Shouting.
    allowed_caps = {w.upper() for w in rules.allowed_caps}
    shouted = sorted({w for w in _SHOUT.findall(text) if w not in allowed_caps})
    if shouted:
        problems.append(f"ALL-CAPS shouting: {', '.join(shouted)}")
    if _BANGS.search(text):
        problems.append("repeated exclamation marks")

    # Banned phrases.
    flat = _normalise(text)
    for phrase in rules.banned_phrases:
        if re.search(rf"(?<!\w){re.escape(_normalise(phrase))}(?!\w)", flat):
            problems.append(f"banned phrase: {phrase!r}")

    # Offers and the numbers that describe them: the copy may only state the approved offer's figure.
    if offer is None:
        problems.append(f"offer {item.offer_id!r} is not an approved offer")
    else:
        # A bundle's `amount` is its percent off the bundle (v3), so the same figure check applies.
        allowed_percent = offer.amount if offer.kind in (OfferKind.PERCENT_OFF, OfferKind.BUNDLE) else None
        allowed_money = offer.amount if offer.kind is OfferKind.AMOUNT_OFF else None
        for raw in _PERCENT.findall(text):
            if allowed_percent is None or abs(_number(raw) - allowed_percent) > 1e-9:
                problems.append(f"unapproved discount figure: {raw}%")
        for raw in [*_MONEY_BEFORE.findall(text), *_MONEY_AFTER.findall(text)]:
            if allowed_money is None or abs(_number(raw) - allowed_money) > 1e-9:
                problems.append(f"unapproved money figure: {raw}")

    # The shop's brand kit: its own habits, read from its own tools.
    problems.extend(f"brand kit: {p}" for p in voice_problems(item, rules.voice, offer))

    # Links.
    for host in _hosts(text):
        if not _host_allowed(host, rules.allowed_domains):
            problems.append(f"link to a domain that is not allowed: {host}")

    # Format and media.
    problems.extend(format_problems(item, rules))

    # Country rules.
    problems.extend(pack_problems(item, rules, packs))
    return problems


class Critic:
    """Approves or rejects content: rules first, then a judge run twice with the rubric reordered."""

    def __init__(
        self,
        judge_llm: LLMClient,
        judge_model_id: str | None = None,
        *,
        threshold: int = JUDGE_THRESHOLD,
        seed: int = 0,
    ) -> None:
        if not 1 <= threshold <= 5:
            raise ValueError("threshold must be between 1 and 5")
        self._llm = judge_llm
        self._model_id = judge_model_id
        self._threshold = threshold
        self._seed = seed

    def review(self, items: Sequence[ContentItem], brand_rules: BrandRules) -> list[ContentItem]:
        """The same items, in order, each APPROVED or REJECTED with `critic_notes` saying why.

        While the judge is unreachable, an item that passes the rules stays DRAFT (or is APPROVED,
        if `brand_rules.judge_optional`), with a `judge unavailable` note. One item's outage never
        takes the rest of the review with it. Once the judge has failed in a review, the remaining
        items are held without asking it again: retrying a provider that is down, item after item,
        only makes the review slow.
        """
        down: list[str] = []  # the judge's failure, once it has failed in this review
        return [self._review_one(item, brand_rules, down) for item in items]

    def rubric_orders(self, item: ContentItem) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The criterion order for each of the two runs: a seeded shuffle, then its reverse.

        Reversing guarantees every criterion appears in a different position in the second run,
        which a second independent shuffle would not.
        """
        order = list(RUBRIC)
        random.Random(f"{self._seed}|{item.content_id}").shuffle(order)
        return tuple(order), tuple(reversed(order))

    def _review_one(self, item: ContentItem, rules: BrandRules, down: list[str]) -> ContentItem:
        problems = check_rules(item, rules)
        if problems:
            return _verdict(item, ContentStatus.REJECTED, [f"rule: {p}" for p in problems])
        if down:
            return _judge_unavailable(item, rules, [], [], f"judge unavailable (not asked: {down[0]})")

        notes: list[str] = []
        failed: list[str] = []
        for run, order in enumerate(self.rubric_orders(item), start=1):
            try:
                verdict = ask_json(
                    self._llm,
                    JudgeVerdict,
                    system=load_prompt("critic_judge"),
                    prompt=_judge_prompt(item, rules, order),
                    model_id=self._model_id,
                    max_tokens=600,
                )
            except AgentOutputError as exc:
                notes.append(f"judge run {run}: no valid verdict ({exc})")
                failed.append(f"run {run} gave no valid verdict")
                continue
            except Exception as exc:  # P4 LLM unavailable: hold the item, never crash the review
                why = exc.code if isinstance(exc, LLMError) else type(exc).__name__
                down.append(f"it failed earlier in this review with {why}")
                return _judge_unavailable(item, rules, notes, failed, f"judge unavailable (run {run}: {why})")
            by_name: dict[str, CriterionScore] = {s.criterion: s for s in verdict.scores}
            notes.append(
                f"judge run {run} [{', '.join(order)}]: "
                + "; ".join(f"{c} {by_name[c].score} ({by_name[c].reason})" for c in order)
            )
            failed.extend(
                f"run {run} scored {c} {by_name[c].score} < {self._threshold}"
                for c in order
                if by_name[c].score < self._threshold
            )
        if failed:
            return _verdict(item, ContentStatus.REJECTED, [*notes, f"rejected: {'; '.join(failed)}"])
        return _verdict(item, ContentStatus.APPROVED, [*notes, "approved"])


def _judge_unavailable(
    item: ContentItem, rules: BrandRules, notes: list[str], failed: list[str], note: str
) -> ContentItem:
    """The verdict for an item that passed the rules but whose judge could not be reached.

    `note` (starting `judge unavailable`) says why. A run that already scored it below the
    threshold still rejects it: that is a sure no. Otherwise it stays DRAFT, so it is never sent,
    unless the brand chose `judge_optional`.
    """
    notes = [*notes, note]
    if failed:
        return _verdict(item, ContentStatus.REJECTED, [*notes, f"rejected: {'; '.join(failed)}"])
    if rules.judge_optional:
        return _verdict(
            item, ContentStatus.APPROVED, [*notes, "approved on the rules alone (judge optional)"]
        )
    return _verdict(item, ContentStatus.DRAFT, [*notes, "held as a draft until the judge is back"])


def _verdict(item: ContentItem, status: ContentStatus, notes: list[str]) -> ContentItem:
    return item.model_copy(update={"status": status, "critic_notes": tuple(notes)})


def _judge_prompt(item: ContentItem, rules: BrandRules, order: Sequence[str]) -> str:
    """One item, alone, with the criteria in the given order. No other items, no author name."""
    offer = next((o for o in rules.offers if o.offer_id == item.offer_id), NO_OFFER)
    lines = [
        f"Brand voice: {rules.brand_voice or 'not described; judge for plain, respectful copy'}",
        "",
        "The message:",
        f"- channel: {item.channel.value}",
        f"- theme: {item.theme.value}; tone: {item.tone.value}",
        f"- approved offer: {offer.label if offer.kind is not OfferKind.NONE else 'none'}",
    ]
    if item.format is not Format.TEXT:
        described = [
            ref.alt_text if (ref := rules.asset_by_url(url)) is not None else "an approved asset"
            for url in asset_urls(item.asset_url)
        ]
        lines.append(f"- format: {item.format.value}, shown with: {' | '.join(described)}")
    if item.subject:
        lines.append(f"- subject: {item.subject}")
    lines += [f"- body:\n{item.body}", "", "Score these criteria, in this order:"]
    lines += [f"{i}. {name}: {RUBRIC[name]}" for i, name in enumerate(order, start=1)]
    return "\n".join(lines)
