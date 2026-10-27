"""Opt-out phrases in the languages customers write: "STOP", "band karo", "बंद करो", ...

The phrases live in `configs/v4/optout_phrases.yaml`, one list per language, so a language is added
by editing the file. A reply opts out when, after lower-casing, Unicode normalisation and dropping
punctuation, it is one of the phrases or contains one as whole words. Words are cut on anything that
is not a letter, a combining mark or a digit, so Devanagari vowel signs stay inside their word (a
plain `\\b` regular expression would split "बंद" in two). A whole message is also compared with its
spaces removed, so "bandkaro" and "s t o p" match too.

A missed opt-out is a legal problem and a wrong one costs one customer's messages, so a phrase inside
a longer sentence counts ("mujhe nahi chahiye" opts out), the same call `autonomy.agents.conversation`
makes for English.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml

DEFAULT_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "configs" / "v4" / "optout_phrases.yaml"


def normalize_words(text: str) -> str:
    """Lower-cased words of `text` joined by single spaces; punctuation and symbols become spaces."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(ch if unicodedata.category(ch)[0] in "LMN" else " " for ch in folded)
    return " ".join(kept.split())


@dataclass(frozen=True)
class OptOutLexicon:
    """The opt-out phrases, normalised, with the language each came from."""

    phrases: tuple[tuple[str, str], ...]
    """(normalised phrase, language), longest phrase first."""

    @classmethod
    def from_languages(cls, languages: Mapping[str, Sequence[str]]) -> OptOutLexicon:
        seen: dict[str, str] = {}
        for language, entries in languages.items():
            for entry in entries:
                phrase = normalize_words(str(entry))
                if phrase:
                    seen.setdefault(phrase, str(language))
        ordered = sorted(seen.items(), key=lambda item: (-len(item[0]), item[0]))
        return cls(tuple(ordered))

    def match(self, text: str) -> str | None:
        """The configured phrase that `text` is or contains, else None (the longest one wins)."""
        words = normalize_words(text)
        if not words:
            return None
        padded = f" {words} "
        for phrase, _language in self.phrases:
            if f" {phrase} " in padded:
                return phrase
        squeezed = words.replace(" ", "")
        for phrase, _language in self.phrases:
            if squeezed == phrase.replace(" ", ""):
                return phrase
        return None

    def language_of(self, phrase: str) -> str | None:
        return next((lang for p, lang in self.phrases if p == phrase), None)


def load_lexicon(path: Path | None = None) -> OptOutLexicon:
    """The lexicon in `path` (default: the shipped `configs/v4/optout_phrases.yaml`)."""
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text(encoding="utf-8"))
    languages = raw.get("languages") if isinstance(raw, Mapping) else None
    if not isinstance(languages, Mapping):
        raise ValueError("optout_phrases.yaml needs a `languages` mapping")
    parsed: dict[str, list[str]] = {}
    for language, entries in languages.items():
        if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
            raise ValueError(f"optout_phrases.yaml: {language!r} must be a list of phrases")
        parsed[str(language)] = list(entries)
    return OptOutLexicon.from_languages(parsed)


@lru_cache(maxsize=1)
def default_lexicon() -> OptOutLexicon:
    """The shipped lexicon, read once."""
    return load_lexicon()


def opt_out_phrase(text: str, lexicon: OptOutLexicon | None = None) -> str | None:
    """The opt-out phrase in a reply, if any (see the module docstring)."""
    return (lexicon or default_lexicon()).match(text)
