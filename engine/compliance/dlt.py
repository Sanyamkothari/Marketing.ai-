"""The DLT template matcher: is this message the registered template, with each slot filled? (Plan K M134)

A DLT template, as an Indian operator's DLT portal registers it, is literal text with **tagged variable slots**.
This module knows five tags and no others (DEC-1405 (c)); a template naming any other tag is refused when the
catalogue is loaded (`CATALOGUE_INVALID`), never guessed at:

========================  ====================================================================================
`{#var#}`                 any text: 1 to `dlt_var_max_chars` characters (30 by default; null for no limit)
`{#numeric#}`             a number: a digit, then digits, `.` or `,` (such as `1,499` or `12.50`), within the limit
`{#alphanumeric#}`        letters and digits only (such as a coupon code), within the limit
`{#url#}`                 a link: `http://`, `https://` or `www.`, then characters that are not white space
`{#cbn#}`                 a call-back number: an optional `+`, then 7 to 20 digits, spaces or hyphens, starting
                          and ending with a digit
========================  ====================================================================================

Tags are written as the portals write them, between `{#` and `#}`; the tag name is read case-insensitively.
An approved message **matches** when it equals the template with every slot replaced by a value of its tag's
type and every literal character kept exactly (case and spaces included). Whether each link is on the client's
whitelist is a separate check (`engine.compliance.urls`).

**Linear in the message length.** The template is compiled once into a small Thompson automaton and the message
is read one character at a time against the set of live states, so the work is (message length) x (template
states), with no backtracking: a template of many adjacent slots cannot make a long message slow, as a
backtracking regular expression could. A language model is never used here: one may only *suggest* which
registered template is closest, as a proposal (design §8.2).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "DEFAULT_VAR_MAX_CHARS",
    "DLT_TAGS",
    "DltTemplate",
    "DltTemplateError",
    "Slot",
    "TemplateMatcher",
    "matches",
    "parse_template",
]

DLT_TAGS: Final[tuple[str, ...]] = ("var", "numeric", "alphanumeric", "url", "cbn")
"""The only tags a template may use (see the module docstring)."""

DEFAULT_VAR_MAX_CHARS: Final[int] = 30
"""The longest value a `{#var#}`, `{#numeric#}` or `{#alphanumeric#}` slot takes by default (configurable)."""

_SLOT: Final[re.Pattern[str]] = re.compile(r"\{#([^#{}]*)#\}")
_URL_PREFIXES: Final[tuple[str, ...]] = ("http://", "https://", "www.")
_CBN_MIDDLE: Final[tuple[int, int]] = (5, 18)
"""A call-back number is a digit, 5 to 18 digits / spaces / hyphens, and a digit: 7 to 20 characters."""


class DltTemplateError(ValueError):
    """A template that cannot be registered as written (an unknown tag, an empty template)."""


@dataclass(frozen=True)
class Slot:
    """One tagged variable slot of a template."""

    tag: str


@dataclass(frozen=True)
class DltTemplate:
    """A parsed template: literal text and slots, in order."""

    text: str
    parts: tuple[str | Slot, ...]

    @property
    def slots(self) -> tuple[Slot, ...]:
        return tuple(part for part in self.parts if isinstance(part, Slot))


def parse_template(text: str) -> DltTemplate:
    """`text` as literal segments and slots; `DltTemplateError` for a tag outside `DLT_TAGS`."""
    if not text or not text.strip():
        raise DltTemplateError("A DLT template is empty.")
    parts: list[str | Slot] = []
    position = 0
    for found in _SLOT.finditer(text):
        tag = found.group(1).strip().lower()
        if tag not in DLT_TAGS:
            raise DltTemplateError(
                f"{found.group(0)!r} is not a DLT variable tag this product knows "
                f"({', '.join('{#' + name + '#}' for name in DLT_TAGS)})."
            )
        if found.start() > position:
            parts.append(text[position : found.start()])
        parts.append(Slot(tag))
        position = found.end()
    if position < len(text):
        parts.append(text[position:])
    return DltTemplate(text=text, parts=tuple(parts))


# ---------------------------------------------------------------------------
# The automaton
# ---------------------------------------------------------------------------
Test = Callable[[str], bool]


def _any(_: str) -> bool:
    return True


def _digit(char: str) -> bool:
    return "0" <= char <= "9"


def _number(char: str) -> bool:
    return _digit(char) or char in ".,"


def _alnum(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _not_space(char: str) -> bool:
    return not char.isspace()


def _cbn(char: str) -> bool:
    return _digit(char) or char in " -"


def _plus(char: str) -> bool:
    return char == "+"


def _literal(expected: str) -> Test:
    return lambda char: char == expected


def _folded(expected: str) -> Test:
    return lambda char: char.lower() == expected


@dataclass
class _State:
    test: Test | None
    """None: an epsilon state (its `out` are followed without reading a character)."""
    out: list[int]


_Fragment = tuple[int, list[tuple[int, int]]]
"""A fragment's start state and its dangling exits (state, slot in `out`), patched by the next fragment."""


class _Builder:
    def __init__(self) -> None:
        self.states: list[_State] = []

    def _add(self, test: Test | None, out: list[int]) -> int:
        self.states.append(_State(test, out))
        return len(self.states) - 1

    def char(self, test: Test) -> _Fragment:
        state = self._add(test, [-1])
        return state, [(state, 0)]

    def empty(self) -> _Fragment:
        state = self._add(None, [-1])
        return state, [(state, 0)]

    def _patch(self, dangling: list[tuple[int, int]], target: int) -> None:
        for state, slot in dangling:
            self.states[state].out[slot] = target

    def sequence(self, fragments: Sequence[_Fragment]) -> _Fragment:
        if not fragments:
            return self.empty()
        start, dangling = fragments[0]
        for following in fragments[1:]:
            self._patch(dangling, following[0])
            dangling = following[1]
        return start, dangling

    def either(self, fragments: Sequence[_Fragment]) -> _Fragment:
        state = self._add(None, [fragment[0] for fragment in fragments])
        return state, [exit_ for fragment in fragments for exit_ in fragment[1]]

    def optional(self, fragment: _Fragment) -> _Fragment:
        state = self._add(None, [fragment[0], -1])
        return state, [*fragment[1], (state, 1)]

    def star(self, fragment: _Fragment) -> _Fragment:
        state = self._add(None, [fragment[0], -1])
        self._patch(fragment[1], state)
        return state, [(state, 1)]

    def repeat(self, test: Test, least: int, most: int | None) -> _Fragment:
        """`least` to `most` characters passing `test` (no upper bound when `most` is None)."""
        parts = [self.char(test) for _ in range(least)]
        if most is None:
            parts.append(self.star(self.char(test)))
        else:
            parts += [self.optional(self.char(test)) for _ in range(most - least)]
        return self.sequence(parts)

    def text(self, literal: str, *, fold: bool = False) -> _Fragment:
        return self.sequence(
            [self.char(_folded(char.lower()) if fold else _literal(char)) for char in literal]
        )

    def slot(self, tag: str, var_max: int | None) -> _Fragment:
        bounded = None if var_max is None else max(var_max, 1)
        if tag == "var":
            return self.repeat(_any, 1, bounded)
        if tag == "alphanumeric":
            return self.repeat(_alnum, 1, bounded)
        if tag == "numeric":
            return self.sequence(
                [self.char(_digit), self.repeat(_number, 0, None if bounded is None else bounded - 1)]
            )
        if tag == "url":
            prefixes = self.either([self.text(prefix, fold=True) for prefix in _URL_PREFIXES])
            return self.sequence([prefixes, self.repeat(_not_space, 1, None)])
        if tag == "cbn":
            low, high = _CBN_MIDDLE
            return self.sequence(
                [
                    self.repeat(_plus, 0, 1),
                    self.char(_digit),
                    self.repeat(_cbn, low, high),
                    self.char(_digit),
                ]
            )
        raise DltTemplateError(f"{{#{tag}#}} is not a DLT variable tag this product knows.")


class TemplateMatcher:
    """A compiled template. :meth:`matches` reads a message once, left to right."""

    def __init__(self, template: DltTemplate, *, var_max_chars: int | None = DEFAULT_VAR_MAX_CHARS) -> None:
        builder = _Builder()
        fragments = [
            builder.text(part) if isinstance(part, str) else builder.slot(part.tag, var_max_chars)
            for part in template.parts
        ]
        start, dangling = builder.sequence(fragments)
        accept = len(builder.states)
        builder.states.append(_State(None, []))
        builder._patch(dangling, accept)
        self._states = builder.states
        self._start = start
        self._accept = accept
        self.template = template

    def _closure(self, seeds: set[int]) -> set[int]:
        seen: set[int] = set()
        stack = list(seeds)
        while stack:
            state = stack.pop()
            if state in seen:
                continue
            seen.add(state)
            node = self._states[state]
            if node.test is None:
                stack.extend(target for target in node.out if target >= 0)
        return seen

    def matches(self, message: str) -> bool:
        """Whether `message` is the template with every slot filled by a value of its tag's type."""
        current = self._closure({self._start})
        for char in message:
            following: set[int] = set()
            for state in current:
                node = self._states[state]
                if node.test is not None and node.test(char):
                    following.add(node.out[0])
            if not following:
                return False
            current = self._closure(following)
        return self._accept in current


def matches(template: str, message: str, *, var_max_chars: int | None = DEFAULT_VAR_MAX_CHARS) -> bool:
    """`message` against the template text `template` (parsed and compiled for this one call)."""
    return TemplateMatcher(parse_template(template), var_max_chars=var_max_chars).matches(message)
