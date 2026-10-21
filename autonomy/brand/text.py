"""Reading a shop's own copy: HTML to text, and the deterministic measures the voice is derived from.

Nothing here guesses. Each function looks for one thing in a fixed way (a greeting from a fixed
list of greeting words, a sign-off from a fixed list of sign-off phrases, emoji by Unicode range,
discounts by pattern) and reports what it saw, so every rule in the kit can say "in 31 of 40".
"""

from __future__ import annotations

import html as html_lib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser

__all__ = [
    "AVOID_CANDIDATES",
    "GREETING_WORDS",
    "SIGN_OFFS",
    "DiscountMentions",
    "Greeting",
    "SignOff",
    "colours_in",
    "contractions",
    "count_emoji",
    "discounts_in",
    "find_greeting",
    "find_sign_off",
    "html_to_text",
    "logo_in",
    "sentences",
    "word_tokens",
    "words",
]

GREETING_WORDS: tuple[str, ...] = (
    "hi",
    "hey",
    "hello",
    "hiya",
    "howdy",
    "dear",
    "greetings",
    "good morning",
    "good afternoon",
    "good evening",
    "g'day",
)
"""Only these open a message as a greeting; anything else is the first line of the copy."""

GREETING_WHO: tuple[str, ...] = ("there", "friend", "friends", "everyone", "all", "folks", "you")
"""Words allowed after the greeting word instead of the customer's name."""

SIGN_OFFS: tuple[str, ...] = (
    "cheers",
    "thanks",
    "thank you",
    "thanks so much",
    "many thanks",
    "best",
    "best wishes",
    "all the best",
    "warmly",
    "warm regards",
    "warm wishes",
    "kind regards",
    "regards",
    "love",
    "with love",
    "lots of love",
    "xo",
    "xoxo",
    "talk soon",
    "speak soon",
    "see you soon",
    "until next time",
    "happy shopping",
    "sincerely",
    "yours",
)
"""Only these count as a sign-off line."""

AVOID_CANDIDATES: tuple[str, ...] = (
    "cheap",
    "bargain",
    "deal",
    "deals",
    "hurry",
    "act now",
    "act fast",
    "buy now",
    "shop now",
    "limited time",
    "don't miss",
    "last chance",
    "exclusive",
    "amazing",
    "incredible",
    "insane",
    "crazy",
    "steal",
    "lowest price",
    "best price",
    "click here",
    "urgent",
    "today only",
    "while stocks last",
    "selling fast",
    "going fast",
    "flash sale",
    "blowout",
    "clearance",
    "doorbuster",
    "must-have",
    "omg",
    "sale",
    "discount",
)
"""Hype and discount words a message could reach for. One the shop's own copy never uses (in enough
of it) becomes a banned word: the shop has shown it does not talk that way. None of these appears in
our template copy, so a ban never blocks the copy we fall back on."""

STOPWORDS: frozenset[str] = frozenset("""
    about above after again against also always another anything around back because been before being
    below between both but came can cannot come could did does doing done down during each else even ever
    every first from further have having here hers herself himself into it's its itself just keep know
    last least less like little made make many more most much must myself need never next none nothing
    only other ours ourselves over own really same should since some something still such take than that
    that's their theirs them themselves then there there's these they this those through thing things
    under until upon very want was way well were what when where which while who whom whose why will
    with within without would yet you'll you're you've your yours yourself yourselves we're we've we'll
    our out off one two three new now get got see look find view click here more email order orders
    shop store today week day days year time just let's here's what's take also every they're don't
    can't won't isn't aren't didn't doesn't hasn't haven't i'm i've i'll she he him his her it's
    """.split())  # noqa: SIM905 - a word list reads best as text
"""Words too common to say anything about a voice."""

_BLOCK_TAGS = frozenset(
    [
        "p",
        "div",
        "br",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ul",
        "ol",
        "table",
        "tbody",
        "thead",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
        "center",
        "hr",
    ]
)
_SKIP_TAGS = frozenset({"style", "script", "head", "title", "noscript"})
_NAME_TAG = re.compile(r"\{\{\s*(?:person\.)?first_name[^}]*\}\}", re.IGNORECASE)
_ORG_TAG = re.compile(r"\{\{\s*organization\.name[^}]*\}\}", re.IGNORECASE)
_UNSUB_TAG = re.compile(r"\{%\s*(?:unsubscribe|manage_preferences|web_view)[^%]*%\}", re.IGNORECASE)
_OTHER_TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
_FOOTER = re.compile(
    r"unsubscribe|manage (?:your )?(?:email )?preferences|view (?:this email |it )?in (?:your |a )?browser|"
    r"no longer (?:want|wish)|©|\(c\)|copyright|all rights reserved|update your (?:profile|preferences)|"
    r"you(?:'re| are) receiving|why did i get this|opt[ -]out|privacy policy",
    re.IGNORECASE,
)
_EMOJI = re.compile(
    "[\U0001f300-\U0001f5ff\U0001f600-\U0001f64f\U0001f680-\U0001f6ff\U0001f900-\U0001f9ff"
    "\U0001fa70-\U0001faff\U0001f1e6-\U0001f1ff☀-⛿✀-➿⭐⭕]"
)
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*[A-Za-z]|[A-Za-z]")
_TOKEN = re.compile(r"[a-z][a-z'-]{3,}")
_CONTRACTION = re.compile(
    r"\b[A-Za-z]+(?:n't|'re|'ll|'ve|'m|'d)\b|\b(?:it|that|what|let|here|there|who|she|he)'s\b", re.IGNORECASE
)
_SENTENCE_END = re.compile(r"[.!?]+(?:\s|$)")
_PCT = r"\s?(?:%|percent\b|per\s+cent\b)"
_PERCENT = re.compile(
    rf"(?:save\s+(?:up\s+to\s+)?(\d{{1,2}}){_PCT})|(?:(\d{{1,2}}){_PCT}[\s-]*(off|discount|savings?))",
    re.IGNORECASE,
)
_AMOUNT = re.compile(
    r"(?:save\s+[$£€]\s?(\d{1,4}(?:\.\d{2})?))|(?:[$£€]\s?(\d{1,4}(?:\.\d{2})?)\s*off)", re.IGNORECASE
)
_FREE_SHIP = re.compile(
    r"\bfree\s+(?:standard\s+|uk\s+|us\s+)?(?:shipping|delivery|postage)\b", re.IGNORECASE
)
_CODE = re.compile(r"\b(?:use|with|enter|apply)\s+(?:the\s+)?code\s*[:\-]?\s*([A-Z0-9]{3,20})\b")
_SIGNER = re.compile(r"^[A-Za-z][A-Za-z&'.\- ]{1,38}[A-Za-z.]$")
_HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_COLOUR_DECL = re.compile(
    r"(?:background-color|background|bgcolor|color)\s*[:=]\s*[\"']?\s*(#[0-9a-fA-F]{3,6})\b"
)
_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""(\w[\w-]*)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: ARG002
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: ARG002
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def replace_tags(text: str) -> str:
    """Klaviyo template tags as plain tokens: the first name as `{first_name}`, the shop as `{store_name}`."""
    text = _NAME_TAG.sub("{first_name}", text)
    text = _ORG_TAG.sub("{store_name}", text)
    text = _UNSUB_TAG.sub(" unsubscribe ", text)
    return _OTHER_TAG.sub(" ", text)


def html_to_text(html: str | None, plain: str | None = None, *, keep_footer: bool = False) -> str:
    """A template's words, one line per block, template tags replaced, footer lines dropped.

    `plain` (the template's own text version) is used when there is no HTML."""
    if html and html.strip():
        parser = _Text()
        try:
            parser.feed(replace_tags(html))
            parser.close()
        except Exception:  # a broken template still gives what was read so far
            pass
        raw = "".join(parser.parts)
    else:
        raw = replace_tags(html_lib.unescape(plain or ""))
    lines = []
    for line in raw.replace("\r", "\n").split("\n"):
        clean = re.sub(r"[ \t ​‌͏]+", " ", line).strip()
        if not clean:
            continue
        if not keep_footer and _FOOTER.search(clean):
            continue
        lines.append(clean)
    return "\n".join(lines)


def words(text: str) -> list[str]:
    return _WORD.findall(text.replace("{first_name}", " ").replace("{store_name}", " "))


def word_tokens(text: str) -> set[str]:
    """Distinct lower-case words of four letters or more, without the common ones."""
    lowered = text.lower().replace("’", "'").replace("{first_name}", " ").replace("{store_name}", " ")
    return {t.strip("'-") for t in _TOKEN.findall(lowered)} - STOPWORDS


def contractions(text: str) -> int:
    return len(_CONTRACTION.findall(text.replace("’", "'")))


def sentences(text: str) -> int:
    return max(1, len(_SENTENCE_END.findall(text)) or (1 if text.strip() else 0))


def count_emoji(text: str) -> int:
    return len(_EMOJI.findall(text))


def strip_emoji(text: str) -> str:
    return _EMOJI.sub("", text).replace("️", "")


@dataclass(frozen=True)
class Greeting:
    word: str
    """As the shop writes it, e.g. 'Hey'."""
    who: str | None
    """'{{first_name}}', a word from `GREETING_WHO`, or None."""
    punct: str

    @property
    def line(self) -> str:
        return f"{self.word}{' ' + self.who if self.who else ''}{self.punct}"


def find_greeting(text: str) -> Greeting | None:
    """The greeting in the first three lines of a message, if one opens it."""
    words_alt = "|".join(re.escape(w) for w in sorted(GREETING_WORDS, key=len, reverse=True))
    who_alt = "|".join(GREETING_WHO)
    pattern = re.compile(
        rf"^(?P<word>{words_alt})(?:\s+(?P<who>\{{first_name\}}|{who_alt}))?\s*(?P<p>[,!.:]?)\s*$",
        re.IGNORECASE,
    )
    for line in text.split("\n")[:3]:
        candidate = strip_emoji(line).strip().replace("’", "'")
        # "Hi {first_name}, here is ..." on one line: the greeting is the part before the comma.
        head = re.split(r"(?<=[,!])\s", candidate, maxsplit=1)[0]
        match = pattern.match(head) or pattern.match(candidate)
        if match is None:
            continue
        raw_word = match.group("word")
        word = raw_word[0].upper() + raw_word[1:].lower()
        who_raw = match.group("who")
        who = (
            "{{first_name}}"
            if who_raw and who_raw.lower() == "{first_name}"
            else (who_raw or "").lower() or None
        )
        return Greeting(word=word, who=who, punct=match.group("p") or ",")
    return None


@dataclass(frozen=True)
class SignOff:
    phrase: str
    """As the shop writes it, capitalised, with its punctuation: 'Cheers,'."""
    signer: str | None


def find_sign_off(text: str) -> SignOff | None:
    """A sign-off from `SIGN_OFFS` in the last six lines, with the line that names who signs."""
    lines = text.split("\n")[-6:]
    alt = "|".join(re.escape(s) for s in sorted(SIGN_OFFS, key=len, reverse=True))
    pattern = re.compile(rf"^(?P<phrase>{alt})(?P<p>[,!.]?)(?:\s+(?P<signer>.+))?$", re.IGNORECASE)
    for index, line in enumerate(lines):
        candidate = strip_emoji(line).strip().replace("’", "'")
        match = pattern.match(candidate)
        if match is None:
            continue
        phrase = match.group("phrase")
        shown = phrase[0].upper() + phrase[1:].lower() + (match.group("p") or ",")
        signer = match.group("signer")
        if signer is None and index + 1 < len(lines):
            signer = strip_emoji(lines[index + 1]).strip()
        return SignOff(phrase=shown, signer=clean_signer(signer))
    return None


def clean_signer(value: str | None) -> str | None:
    """A signer line only when it is a few plain words (a name or 'The Acme team'), no shouting."""
    if not value:
        return None
    text = re.sub(r"\s+", " ", value).strip().rstrip(",")
    if not _SIGNER.match(text) or len(text.split()) > 5 or _FOOTER.search(text):
        return None
    if any(len(w) >= 4 and w.isupper() for w in text.split()):
        return None
    return text


@dataclass(frozen=True)
class DiscountMentions:
    percents: tuple[float, ...]
    amounts: tuple[float, ...]
    free_shipping: int
    codes: int
    phrases: tuple[str, ...]
    """Each percent mention's form: '{n}% off', 'save {n}%' or '{n}% discount'."""

    @property
    def any(self) -> bool:
        return bool(self.percents or self.amounts or self.free_shipping)


def discounts_in(text: str) -> DiscountMentions:
    percents: list[float] = []
    phrases: list[str] = []
    for m in _PERCENT.finditer(text):
        if m.group(1):
            percents.append(float(m.group(1)))
            phrases.append("save {n}%")
        else:
            percents.append(float(m.group(2)))
            phrases.append("{n}% off" if m.group(3).lower() == "off" else "{n}% discount")
    amounts = [float(m.group(1) or m.group(2)) for m in _AMOUNT.finditer(text)]
    return DiscountMentions(
        percents=tuple(percents),
        amounts=tuple(amounts),
        free_shipping=len(_FREE_SHIP.findall(text)),
        codes=len(_CODE.findall(text)),
        phrases=tuple(phrases),
    )


def _hex6(value: str) -> str:
    v = value.lstrip("#").lower()
    return "#" + ("".join(c * 2 for c in v) if len(v) == 3 else v)


def is_neutral(hex6: str) -> bool:
    """White, black and greys (low saturation) say nothing about a brand's colours."""
    r, g, b = (int(hex6[i : i + 2], 16) for i in (1, 3, 5))
    hi, lo = max(r, g, b), min(r, g, b)
    return hi - lo < 24 or hi < 40 or lo > 235


def colours_in(html: str) -> list[str]:
    """The brand-looking colours an email template declares (greys, black and white left out)."""
    found: list[str] = []
    for raw in _COLOUR_DECL.findall(html):
        if len(raw) not in (4, 7):
            continue
        value = _hex6(raw)
        if not is_neutral(value):
            found.append(value)
    return found


def logo_in(html: str) -> tuple[str, str] | None:
    """The first image whose alt, class, id or file name says 'logo': (url, alt)."""
    for tag in _IMG.findall(html):
        attrs = {k.lower(): v.strip("\"'") for k, v in _ATTR.findall(tag)}
        src = html_lib.unescape(attrs.get("src", ""))
        if not src.startswith("https://"):
            continue
        said = (
            " ".join(attrs.get(k, "") for k in ("alt", "class", "id", "title")) + " " + src.rsplit("/", 1)[-1]
        )
        if "logo" in said.lower():
            return src, html_lib.unescape(attrs.get("alt", ""))
    return None


def percentile(values: Iterable[float], share: float) -> float:
    """The value at `share` (0..1) of the sorted values, nearest rank; 0 for none."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(1, min(len(ordered), math.ceil(share * len(ordered))))
    return ordered[rank - 1]
