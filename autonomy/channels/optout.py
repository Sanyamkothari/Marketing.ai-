"""Opt-out phrases in the languages customers write: "STOP", "band karo", "बंद करो", ...

The phrases live in `configs/v4/optout_phrases.yaml`, one entry per language, so a language is added
by editing the file. Text is compared after lower-casing, Unicode normalisation and dropping
punctuation. Words are cut on anything that is not a letter, a combining mark or a digit, so
Devanagari vowel signs stay inside their word (a plain `\\b` regular expression would split "बंद" in
two).

Each language says how its phrases match:

- `contains`: the reply contains the phrase as whole words ("please stop texting me" opts out). This
  is the call `autonomy.agents.conversation` already makes for English, where the phrases are
  unambiguous commands.
- `whole_message` (the default): the reply is the phrase, once the filler words of `filler_words`
  ("please", "mujhe", "ye", "messages", "अब", ...) are dropped. Hinglish and Hindi phrases are
  ordinary words that also describe products ("Red nahi chahiye, blue bhejo" = "not the red one,
  send the blue"; "abhi nahi chahiye, next month" = "not now"), so a phrase inside a longer sentence
  does not opt out: those replies go to the classifier as before.

In both modes the whole message with its spaces removed is also compared, so "bandkaro" and
"s t o p" match.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml

DEFAULT_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "configs" / "v4" / "optout_phrases.yaml"

CONTAINS = "contains"
WHOLE_MESSAGE = "whole_message"
MATCH_MODES: tuple[str, ...] = (CONTAINS, WHOLE_MESSAGE)
OPTED_OUT_ATTR = "opted_out"
"""Set on a REPLY event (to the matched phrase) by an adapter that already wrote the UNSUBSCRIBE for
the reply's channel, so the Conversation agent does not write a second one."""


def normalize_words(text: str) -> str:
    """Lower-cased words of `text` joined by single spaces; punctuation and symbols become spaces."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(ch if unicodedata.category(ch)[0] in "LMN" else " " for ch in folded)
    return " ".join(kept.split())


@dataclass(frozen=True)
class OptOutLexicon:
    """The opt-out phrases, normalised, with the language and match mode each came from."""

    phrases: tuple[tuple[str, str], ...]
    """(normalised phrase, language), longest phrase first."""
    modes: Mapping[str, str] = field(default_factory=dict)
    """language -> `CONTAINS` or `WHOLE_MESSAGE`; a language not listed is `WHOLE_MESSAGE`."""
    filler: frozenset[str] = frozenset()
    """Normalised words dropped before a `WHOLE_MESSAGE` comparison."""

    @classmethod
    def from_languages(
        cls,
        languages: Mapping[str, Sequence[str]],
        *,
        modes: Mapping[str, str] | None = None,
        filler: Sequence[str] = (),
    ) -> OptOutLexicon:
        seen: dict[str, str] = {}
        for language, entries in languages.items():
            for entry in entries:
                phrase = normalize_words(str(entry))
                if phrase:
                    seen.setdefault(phrase, str(language))
        ordered = sorted(seen.items(), key=lambda item: (-len(item[0]), item[0]))
        for language, mode in (modes or {}).items():
            if mode not in MATCH_MODES:
                raise ValueError(f"opt-out language {language!r}: match must be one of {MATCH_MODES}")
        words = frozenset(w for entry in filler for w in normalize_words(str(entry)).split())
        return cls(tuple(ordered), dict(modes or {}), words)

    def mode_of(self, language: str) -> str:
        return self.modes.get(language, WHOLE_MESSAGE)

    def match(self, text: str) -> str | None:
        """The configured phrase that `text` is (or, for a `contains` language, contains), else None.

        The longest phrase wins."""
        words = normalize_words(text)
        if not words:
            return None
        padded = f" {words} "
        bare = " ".join(w for w in words.split() if w not in self.filler)
        for phrase, language in self.phrases:
            if self.mode_of(language) == CONTAINS:
                if f" {phrase} " in padded:
                    return phrase
            elif phrase in (words, bare):
                return phrase
        squeezed = {words.replace(" ", ""), bare.replace(" ", "")}
        for phrase, _language in self.phrases:
            if phrase.replace(" ", "") in squeezed:
                return phrase
        return None

    def language_of(self, phrase: str) -> str | None:
        return next((lang for p, lang in self.phrases if p == phrase), None)


def _strings(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(e, str) for e in value):
        raise ValueError(f"optout_phrases.yaml: {where} must be a list of strings")
    return list(value)


def load_lexicon(path: Path | None = None) -> OptOutLexicon:
    """The lexicon in `path` (default: the shipped `configs/v4/optout_phrases.yaml`).

    A language is either a list of phrases (matched as the whole message) or a mapping with
    `phrases` and `match` (`contains` or `whole_message`)."""
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text(encoding="utf-8"))
    languages = raw.get("languages") if isinstance(raw, Mapping) else None
    if not isinstance(raw, Mapping) or not isinstance(languages, Mapping):
        raise ValueError("optout_phrases.yaml needs a `languages` mapping")
    parsed: dict[str, list[str]] = {}
    modes: dict[str, str] = {}
    for language, entry in languages.items():
        name = str(language)
        if isinstance(entry, Mapping):
            parsed[name] = _strings(entry.get("phrases"), f"{name}.phrases")
            modes[name] = str(entry.get("match", WHOLE_MESSAGE))
        else:
            parsed[name] = _strings(entry, name)
    filler = _strings(raw.get("filler_words", []), "filler_words")
    return OptOutLexicon.from_languages(parsed, modes=modes, filler=filler)


@lru_cache(maxsize=1)
def default_lexicon() -> OptOutLexicon:
    """The shipped lexicon, read once."""
    return load_lexicon()


def opt_out_phrase(text: str, lexicon: OptOutLexicon | None = None) -> str | None:
    """The opt-out phrase in a reply, if any (see the module docstring)."""
    return (lexicon or default_lexicon()).match(text)
