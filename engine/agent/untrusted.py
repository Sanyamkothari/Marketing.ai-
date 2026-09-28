"""Text from a client's file, made safe to show and to put in a prompt (Plan G M77 hardening).

Column names and cell values come from whoever wrote the file. Before any of them reaches a prompt
or a line on the screen, this module:

* removes characters that hide or reorder text - control characters, zero-width characters,
  bidirectional overrides and other Unicode *format* characters (category `C*`), and line or
  paragraph separators - so what a person reads is what the model reads;
* collapses runs of whitespace to one space;
* cuts a column name to `MAX_NAME_CHARS` and any other text to the limit its caller gives, ending in
  `…` so a cut is visible.

It never changes data. A recipe step keeps the exact column name; only what is *shown* is cleaned,
and `resolve_column` maps a cleaned name the model repeats back to the one column it stands for.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any, Final

__all__ = [
    "MAX_NAME_CHARS",
    "MAX_PROMPT_LIST",
    "MAX_TEXT_CHARS",
    "clean_text",
    "display_name",
    "for_prompt",
    "resolve_column",
]

MAX_NAME_CHARS: Final[int] = 64
"""A column name longer than this is cut when shown; real headers are far shorter."""
MAX_TEXT_CHARS: Final[int] = 500
"""The longest single string from a tool result or the session state that reaches a prompt."""
MAX_PROMPT_LIST: Final[int] = 50
"""Longer lists in a prompt keep their first items and say how many were left out."""

_DROPPED_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def clean_text(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """`text` without invisible or reordering characters, whitespace collapsed, cut to `limit`."""
    kept = "".join(
        " " if ch in "\t\n\r\f\v" else ch
        for ch in str(text)
        if ch in "\t\n\r\f\v" or unicodedata.category(ch) not in _DROPPED_CATEGORIES
    )
    single = " ".join(kept.split())
    if len(single) <= limit:
        return single
    return single[: max(1, limit - 1)].rstrip() + "…"


def display_name(name: object) -> str:
    """A column name as it is shown to a person or a model: cleaned and at most `MAX_NAME_CHARS`."""
    return clean_text(str(name), MAX_NAME_CHARS) or "(unnamed column)"


def resolve_column(name: str, columns: Iterable[object]) -> str | None:
    """The file's column `name` stands for: an exact match, else the one column shown as `name`."""
    names = [str(column) for column in columns]
    if name in names:
        return name
    shown = [column for column in names if display_name(column) == name]
    return shown[0] if len(shown) == 1 else None


def for_prompt(value: Any, names: Mapping[str, str] | None = None) -> Any:
    """A JSON-like value made safe for a prompt.

    Every string is cleaned; a string that *is* a column name becomes its display name, and long
    column names inside other strings are replaced by their display names. Lists longer than
    `MAX_PROMPT_LIST` keep their first items plus a note of how many were left out.
    """
    renames = {exact: shown for exact, shown in (names or {}).items() if exact != shown}
    # Only an over-long name is worth finding inside other text; a name that differs from its
    # display form by an invisible character is fixed by `clean_text` wherever it appears.
    long_names = sorted((n for n in renames if len(n) > MAX_NAME_CHARS), key=len, reverse=True)

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            if item in renames:
                return renames[item]
            text = item
            for exact in long_names:
                if exact in text:
                    text = text.replace(exact, renames[exact])
            return clean_text(text)
        if isinstance(item, Mapping):
            return {clean_text(str(key), MAX_NAME_CHARS): walk(inner) for key, inner in item.items()}
        if isinstance(item, (list, tuple)):
            kept = [walk(inner) for inner in list(item)[:MAX_PROMPT_LIST]]
            if len(item) > MAX_PROMPT_LIST:
                kept.append({"not_shown": len(item) - MAX_PROMPT_LIST})
            return kept
        return item

    return walk(value)
