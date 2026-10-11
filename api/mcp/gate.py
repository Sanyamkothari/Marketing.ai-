"""No third-party text in a result (Plan K M133, DEC-1404 (f); design §7.3, "prompt injection through our data").

A result reaches another company's model, so a string in it is a prompt. Results are aggregates in typed fields,
but some strings are not ours: a column name (an audit's randomness signals), an arm's name, a file name, and
whatever a caller sent as an argument. Every string of a result - values and keys, at any depth - passes this
gate before it leaves:

1. **Invisible characters** (control, format, private-use, unassigned; Plan G's `untrusted` set) are removed.
2. **Arguments are never echoed.** A string that contains a sentence-like argument the caller sent (four words
   or more, or 32 characters or more; any argument in an error message) is withheld. Short labels a caller
   names (a band) come back only as the stored data spells them.
3. **Instruction-shaped text is withheld**: "ignore previous instructions", "system prompt", "you are now",
   "call the tool", "approve all", chat-template markers, a line starting `system:` - after folding
   (NFKC, lower case), so look-alike letters do not hide it.
4. **Personal data is masked** (the product's own ids and digests, `PRODUCT_ID`, are left as they are) with Plan G's egress scanners (`engine.agent.egress.assert_clean`: e-mail,
   phone, PAN, Aadhaar, card, IBAN, IP, URL, tokens); a string they find nothing in is returned byte for byte,
   so the product's own sentences reach the client exactly as its screens show them.

A withheld string becomes `WITHHELD`; a withheld key is dropped. The count of what was withheld goes into the
result's `_meta`. Error messages pass the same gate (`message`), so a refusal cannot quote an argument either.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "INSTRUCTION",
    "PRODUCT_ID",
    "WITHHELD",
    "Gate",
    "argument_texts",
    "is_instruction",
]

WITHHELD: Final[str] = "[withheld: text from outside the product]"

_DROPPED: Final[frozenset[str]] = frozenset({"Cf", "Cs", "Co", "Cn"})

INSTRUCTION: Final[re.Pattern[str]] = re.compile(
    r"""
    \b(?:ignore|disregard|forget|override|bypass)\b[^.;\n]{0,60}?
        \b(?:instructions?|previous|prior|above|earlier|rules?|prompts?|guidelines|polic(?:y|ies))\b
    | \b(?:system|developer|assistant)[\s_-]*(?:prompts?|messages?|instructions?)\b
    | \bnew\s+instructions?\b
    | \byou\s+(?:are|must|should|will)\s+now\b
    | \b(?:call|invoke|use|run|execute|trigger)\s+(?:the\s+|a\s+|this\s+)?(?:tools?|functions?)\b
    | \b(?:call|invoke)\s+[a-z]+_[a-z_]+\b
    | \bapprove\s+(?:all|every|everything|this|these|the)\b
    | \bdo\s+not\s+(?:tell|inform|show)\b
    | <\|[^|>]{0,40}\|> | \[/?(?:inst|sys)\] | <</?sys>>
    | (?:^|\n)\s*(?:system|assistant|user|human)\s*:
    """,
    re.IGNORECASE | re.VERBOSE,
)
"""What instruction-shaped text looks like. Wide on purpose: a false positive withholds a label, a false negative
hands another company's model an instruction."""

_IDS: Final[str] = (
    r"[rcx]_\d{8}_[0-9a-f]{8}|u_[0-9a-f]{12}|m_[a-z0-9-]{1,64}_\d{1,6}|ev_[0-9a-f]{16}|prop_[0-9a-f]{16}|[0-9a-f]{64}"
)
PRODUCT_ID: Final[re.Pattern[str]] = re.compile(
    rf"(?:{_IDS})|(?:runs|campaigns|uploads|indexes)/(?:{_IDS})(?:/[a-z0-9_.-]{{1,96}}){{1,3}}"
)
"""The product's own ids (runs, campaigns, indexes, uploads, models, evidence, proposals), sha256 digests and
the storage keys of its artefacts (`campaigns/<id>/audit.json`): returned as they are. The token scanner would
read one as an API key; an id carries no instruction and no person."""

_ARGUMENT_WORDS: Final[int] = 4
_ARGUMENT_CHARS: Final[int] = 32
_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def _visible(text: str) -> str:
    if all(unicodedata.category(ch) not in _DROPPED for ch in text):
        return text
    return "".join(ch for ch in text if unicodedata.category(ch) not in _DROPPED)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[_\-.]+")


def _instruction(folded: str) -> bool:
    """The screen on folded text, and again with `_`, `-` and `.` read as spaces (`ignore_previous_rules`)."""
    return (
        INSTRUCTION.search(folded) is not None or INSTRUCTION.search(_SEPARATORS.sub(" ", folded)) is not None
    )


def is_instruction(text: str) -> bool:
    """Whether `text` reads as an instruction to a model (after folding)."""
    return _instruction(_fold(_visible(text)))


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def argument_texts(arguments: Any) -> tuple[str, ...]:
    """Every string a caller sent - keys and values, at any depth - except ids, folded, longest first."""
    seen = {_fold(_visible(text)).strip() for text in _strings(arguments)}
    return tuple(
        sorted((text for text in seen if text and not _ID.fullmatch(text)), key=lambda t: (-len(t), t))
    )


def _sentence_like(text: str) -> bool:
    return len(text) >= _ARGUMENT_CHARS or len(text.split()) >= _ARGUMENT_WORDS


@dataclass
class Gate:
    """One result's gate: the caller's arguments, and what was withheld or masked so far."""

    arguments: tuple[str, ...] = ()
    withheld: int = 0
    masked: int = 0
    _echo: tuple[str, ...] = field(default=(), init=False, repr=False)

    def __post_init__(self) -> None:
        self._echo = tuple(text for text in self.arguments if _sentence_like(text))

    @classmethod
    def for_arguments(cls, arguments: Any) -> Gate:
        return cls(arguments=argument_texts(arguments))

    def _echoes(self, folded: str, candidates: Iterable[str]) -> bool:
        return any(text in folded for text in candidates)

    def text(self, value: str) -> str:
        """A result string as it may leave: `WITHHELD`, masked, or exactly as it was."""
        if PRODUCT_ID.fullmatch(value):
            return value
        visible = _visible(value)
        folded = _fold(visible)
        if _instruction(folded) or self._echoes(folded, self._echo):
            self.withheld += 1
            return WITHHELD
        from engine.agent.egress import assert_clean

        cleaned, count = assert_clean(visible)
        self.masked += count
        return cleaned

    def message(self, value: str) -> str:
        """An error message: withheld if it quotes any argument at all (ids aside), else gated as text."""
        folded = _fold(_visible(value))
        if self._echoes(folded, (text for text in self.arguments if len(text) >= 3)):
            self.withheld += 1
            return "The request was refused. The reason quoted what was sent, so it is not repeated here."
        return self.text(value)

    def value(self, value: Any) -> Any:
        """A JSON value with every string gated; a key that would be withheld is dropped with its value."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            kept: dict[str, Any] = {}
            for key, item in value.items():
                gated = self.text(str(key))
                if gated == WITHHELD:
                    continue
                kept[gated] = self.value(item)
            return kept
        if isinstance(value, list | tuple):
            return [self.value(item) for item in value]
        return value
