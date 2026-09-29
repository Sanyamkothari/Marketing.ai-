"""What the chat model is allowed to see of a client's file: one default-deny gate (Plan G §7.6).

The chat sends tool results to an AI service. Masking by pattern can never be complete - a name in
a sentence, a street address, another country's ID number all look like ordinary words - so this
module does not rely on one detector. It makes the boundary **default-deny** and checkable:

* :func:`prepare` walks a tool result. A string is a *safe label* only when it sits under a key in
  :data:`SAFE_KEYS` (a column name, a type, a check code, a sentence our own rules wrote) or is a
  known column name or a fixed literal. **Every other string is a cell value** and goes through
  :func:`mask_value`, whatever key it sits under and whichever tool wrote it - a tool added tomorrow
  is masked by default.
* :data:`DataAccess.SUMMARIES_ONLY` (`agent.ai_data_access`) removes cell values altogether: every
  cell-derived string becomes its *shape* (`Aaaaa 9999`), and a number that is a cell (`minimum`,
  a list item) becomes a shape too. Counts and averages stay.
* A column that the profile marks as personal data, or that `agent.always_hide_columns` names, never
  yields a value in either mode: its strings become `[personal data]` / `[hidden by your settings]`
  and only its counts remain.
* A column name that is not ordinary words (an e-mail address, a 12-digit number, a URL) is shown
  to the model as a stable alias `column_<n>`; :func:`unalias` puts the real name back in what the
  person reads, when that name is itself clean.
* :func:`assert_clean` is the last line: it re-scans the rendered prompt and masks anything that
  still looks like personal data before the prompt can leave.

What no scanner can promise is in `docs/AGENTS.md` §7.7: a person's name inside a sentence, in a
column that nobody marked, gets through in `masked_data` mode. That is what `always_hide_columns`
and `summaries_only` are for.
"""

from __future__ import annotations

import ipaddress
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from engine.agent.config import DataAccess
from engine.agent.contracts import ChatMessage, SentItem
from engine.agent.untrusted import MAX_NAME_CHARS, clean_text, display_name, quoted
from engine.pii import REDACTION_MARKER_PATTERN, TEXT_SCANNERS, marker_for, redact_text

__all__ = [
    "COLUMN_KEYS",
    "EGRESS_LATE_MASK",
    "HIDDEN_BY_SETTINGS",
    "ID_KEYS",
    "MAX_CELL_CHARS",
    "MAX_SENT_ITEMS",
    "MAX_SENT_PREVIEW_CHARS",
    "MAX_SENT_SESSION_CHARS",
    "MAX_SENT_SESSION_ITEMS",
    "PERSONAL_DATA",
    "SAFE_KEYS",
    "SAFE_LITERALS",
    "STATE_TOOL",
    "TEXT_KEYS",
    "THIRD_PARTY_FREE_BACKENDS",
    "DataAccess",
    "Egress",
    "alias_table",
    "assert_clean",
    "cap_sent",
    "is_plain_label",
    "is_third_party",
    "mask_value",
    "prepare",
    "safe_column_label",
    "scrub",
    "sent_item",
    "shape_of",
    "unalias",
]

# ---------------------------------------------------------------------------
# What is a label
# ---------------------------------------------------------------------------
COLUMN_KEYS: Final[frozenset[str]] = frozenset({"column", "columns"})
"""The value is a column name (or a list of them). A string that is not one is treated as a cell."""

ID_KEYS: Final[frozenset[str]] = frozenset(
    {"type", "kind", "code", "name", "function", "dtype", "error", "severity", "override_path"}
)
"""The value is a short identifier written by our code: a type, a check code, a function name.

`name` is a column name too. A value that is not identifier-shaped (`ID_LABEL`) is a cell."""

TEXT_KEYS: Final[frozenset[str]] = frozenset({"label", "message", "suggestion"})
"""The value is a sentence our own rules wrote (a setting's label, a check's message and advice)."""

SAFE_KEYS: Final[frozenset[str]] = COLUMN_KEYS | ID_KEYS | TEXT_KEYS
"""Keys whose string value is a label, not a cell: the whole allow-list, and nothing else.

`column columns type kind code name function dtype label message` are the contract with the read
tools (a new tool puts cell-derived text under any *other* key). `error` is the loop's own error
feedback, `severity` and `suggestion` and `override_path` are what `check_data` already returns.
Everything else - `value`, `examples`, `bucket`, `positive_label`, `file_name`, a key a tool has not
been written yet - is a cell value."""

RESULT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "acknowledge",
        "acknowledgeable",
        "acknowledged",
        "allowed",
        "always_empty",
        "answered",
        "both_empty_share",
        "bucket",
        "buckets",
        "cells",
        "checked",
        "checks",
        "code",
        "column",
        "columns",
        "columns_offset",
        "columns_total",
        "columns_with_missing",
        "configured_target",
        "consent",
        "constant",
        "contains",
        "convert_share",
        "convertible",
        "count",
        "crosstab",
        "current",
        "dates",
        "dates_found",
        "day_first",
        "dayfirst",
        "decimal",
        "distinct",
        "distinct_days",
        "distinct_matching",
        "distinct_months",
        "distinct_pairs",
        "duplicate_rows",
        "earliest",
        "empty",
        "empty_key_rows",
        "equal_of_both_present_share",
        "equal_share",
        "equals",
        "error",
        "errors",
        "evidence_id",
        "evidence_ids",
        "example_cells",
        "examples",
        "failed",
        "failed_examples",
        "false_values",
        "file_name",
        "from",
        "from_text",
        "groups",
        "groups_checked",
        "groups_differing_in_other_columns",
        "hidden_columns",
        "high",
        "histogram",
        "id",
        "ids",
        "ids_on_several_rows",
        "issues",
        "kind",
        "label",
        "largest_group",
        "latest",
        "left",
        "left_distinct",
        "left_given_right_share",
        "left_value",
        "limit",
        "longest_gap_days",
        "looks_like_id",
        "looks_like_time",
        "low",
        "matches",
        "maximum",
        "mean",
        "median",
        "merge",
        "message",
        "minimum",
        "missing",
        "missing_value_rate_pct",
        "most_rows",
        "most_rows_for_one_value",
        "n",
        "name",
        "negatives",
        "infinite",
        "next_offset",
        "non_empty",
        "notes",
        "null_rate",
        "numbers",
        "numeric",
        "offset",
        "only_left_empty_share",
        "only_right_empty_share",
        "opt_out",
        "order_ambiguous",
        "other_columns",
        "other_rows",
        "outliers",
        "overall_positive_rate",
        "override_path",
        "overrides",
        "p1",
        "p25",
        "p5",
        "p50",
        "p75",
        "p95",
        "p99",
        "pairs",
        "params",
        "passed",
        "path",
        "pearson",
        "percent_to_fraction",
        "personal_data",
        "personal_data_in_text",
        "positive_label",
        "positive_rate",
        "positives",
        "primary_key",
        "primary_key_candidates",
        "proposals",
        "quantiles",
        "questions",
        "reason",
        "recently_contacted",
        "relation_to_outcome",
        "repeated_keys",
        "result",
        "returned",
        "right",
        "right_distinct",
        "right_given_left_share",
        "right_value",
        "rows",
        "rows_in_repeated_keys",
        "rows_per_id",
        "sampled",
        "severity",
        "shape",
        "shapes",
        "share",
        "share_empty",
        "share_negative",
        "share_with_time",
        "share_zero",
        "spearman",
        "state",
        "stop_reason",
        "strip",
        "suggested",
        "suggestion",
        "target",
        "target_by_synonym",
        "target_candidate",
        "target_case_insensitive",
        "target_exact",
        "text",
        "time_column_candidates",
        "title",
        "to",
        "tool",
        "top",
        "top_keys",
        "top_values",
        "total_matching",
        "total_matching_rows",
        "true_values",
        "truncated",
        "two_valued",
        "type",
        "unique",
        "unparseable",
        "value",
        "values",
        "values_seen_once",
        "warnings",
        "where_column",
        "whole_numbers",
        "whole_row",
        "withheld",
    }
)
"""The field names our own tools, checks and the chat loop write in a result: **the only dict keys that are
labels**. A dict key that is neither one of these, nor a column of the file, nor in `SAFE_KEYS`, may be a
cell value (a tool that returned `{cell: count}`): under a hidden column, and in `summaries_only`, it is
hidden or reduced to its shape. `tests/unit/agent/test_egress_canary.py` walks every tool's result and fails
when a key is not here, so a new field is added on purpose and a cell-keyed dict is caught."""

PERSONAL_DATA: Final[str] = "[personal data]"
"""What a value of a column the profile marks as personal data becomes."""
HIDDEN_BY_SETTINGS: Final[str] = "[hidden by your settings]"
"""What a value of a column named in `agent.always_hide_columns` becomes."""
EGRESS_LATE_MASK: Final[str] = "EGRESS_LATE_MASK"
"""The turn-log code for a prompt that `assert_clean` had to mask before sending."""

_PII_KINDS: Final[frozenset[str]] = frozenset(
    {"email", "phone", "pan", "aadhaar", "name", "address", "ssn", "passport"}
)
SAFE_LITERALS: Final[frozenset[str]] = _PII_KINDS | {
    PERSONAL_DATA,
    HIDDEN_BY_SETTINGS,
    "[REDACTED]",
    "(empty)",
    "(other)",
    "(unnamed column)",
}
"""Strings that carry no information about a person wherever they are: detector names, our markers."""

ID_LABEL: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9_.:/\-\[\]]{0,47}")
_KEY_LABEL: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,39}")
_COUNT_KEY: Final[re.Pattern[str]] = re.compile(
    r"(?:rows?|empty|distinct|unique|missing\w*|nulls?\w*|count\w*|total\w*|non_empty|convertible|"
    r"failed|ids|most_rows|positives|negatives|offset|limit|top|n|errors|warnings|"
    r"share|shown|returned|next_offset|infinite|unparseable|numbers|checked|distinct\w*|"
    r"repeated_keys|rows_in_repeated_keys|largest_group|groups_\w+|values_seen_once|"
    r"most_rows_for_one_value|"
    r"\w*_(?:rate|count|counts|share|pct|rows|total|distinct))"
)
"""Keys whose number is a count or a share the engine measured: never a cell, kept for every column."""
_STAT_KEY: Final[re.Pattern[str]] = re.compile(r"mean|std|var|average|avg|sum")
"""Aggregates: kept in `summaries_only` for a column that is not hidden, dropped for one that is. Not
`median`: with an odd number of rows it is one person's value (the same number as `p50`), so it is a
cell and becomes a shape in `summaries_only` like `p50`. A number with nine or more digits is masked
whatever its key unless it is a count (`_number`), aggregates included: the mean of ten-digit phone numbers
stored as floats is not an aggregate anybody may read."""

MAX_CELL_CHARS: Final[int] = 80
"""A cell longer than this is cut after it is masked (a whole cell is masked before the cut)."""
MAX_DEPTH: Final[int] = 12
MAX_ITEMS: Final[int] = 200
MAX_SENT_ITEMS: Final[int] = 25
STATE_TOOL: Final[str] = "advisor_state"
"""What `SentItem.tool` says for the helper's own suggestions and questions, which go into every prompt."""
MAX_SENT_PREVIEW_CHARS: Final[int] = 1_500
MAX_SENT_SESSION_CHARS: Final[int] = 20_000
MAX_SENT_SESSION_ITEMS: Final[int] = 300
"""The most items a session file keeps in all, newest first; older messages keep none. Without it a session
of hundreds of turns would keep a placeholder for every old item and grow past its preview budget."""
SENT_DROPPED: Final[str] = "[not kept: the session keeps at most 20 KB of previews]"
THIRD_PARTY_FREE_BACKENDS: Final[frozenset[str]] = frozenset({"fake", "bedrock"})
"""Backends that run inside the platform's own account. Any other backend is a third party."""

# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------
SHAPE_CAP: Final[int] = 32
SHAPE_RUN: Final[int] = 4
SHAPE_ALPHABET: Final[re.Pattern[str]] = re.compile(r"[Aa9 ?!-/:-@\[-`{-~…]*")
"""Every character a shape can contain: `A`, `a`, `9`, a space, `?`, ASCII punctuation and `…`."""


def shape_of(text: str, cap: int = SHAPE_CAP) -> str:
    """`text` reduced to its shape: digits 9, letters A or a by case, runs of at most four, capped.

    `Jane Roe, 12 Baker St` -> `Aaaa Aaa, 99 Aaaa Aa`. Punctuation is kept (it shows the format);
    any other character is `?`. Nothing of the value survives: not one letter and not one digit.
    """
    out: list[str] = []
    previous = ""
    run = 0
    for ch in text:
        if ch.isdigit():
            symbol = "9"
        elif ch.isalpha():
            symbol = "A" if ch.isupper() else "a"
        elif ch.isspace():
            symbol = " "
        elif ch.isascii() and ch.isprintable():
            symbol = ch
        else:
            symbol = "?"
        if symbol == previous:
            run += 1
            if run > SHAPE_RUN or symbol == " ":
                continue
        else:
            previous, run = symbol, 1
        out.append(symbol)
    shape = "".join(out).strip()
    return shape if len(shape) <= cap else shape[:cap] + "…"


# ---------------------------------------------------------------------------
# Scanners (run after `engine.pii`'s own, see `_scan`)
# ---------------------------------------------------------------------------
_HOLD_OPEN: Final[str] = ""
_HOLD_CLOSE: Final[str] = ""
_HELD: Final[re.Pattern[str]] = re.compile(f"{_HOLD_OPEN}(\\d+){_HOLD_CLOSE}")

_SHAPE_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"(?<![^\s\"'(\[{,;])[Aa9]{1,40}(?:[ !-/:-@\[-`{-~]{1,3}[Aa9]{1,40}){0,24}(?![^\s\"')\]},;.])"
)
"""A stretch made only of `A`, `a`, `9`, spaces and ASCII punctuation, between spaces or quotes: a shape.

Such a text carries no value (not one real letter or digit), so it is never masked - `aaaa.aaa@aaaa.aaaa`
must not become `[REDACTED:email]` and `9999 9999 9999` must not become a phone number, or a
`summaries_only` prompt would be masked away by its own last check (`_is_shape` decides which chains
count). It must start and end at a word edge, so a token cannot be cut in two by an `a` inside it.
At most 25 groups: a shape is at most `SHAPE_CAP` characters, and an unbounded chain is quadratic on `"a\\"a\\...`."""
_UUID: Final[re.Pattern[str]] = re.compile(
    r"(?<![0-9A-Za-z])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Za-z])"
)
_MONTH: Final[str] = r"(?:0?[1-9]|1[0-2])"
_DAY: Final[str] = r"(?:0?[1-9]|[12]\d|3[01])"
_ISO_DATE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\d.\-/])(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"(?:[T ](?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)?(?!\d)"
)
_OTHER_DATE: Final[re.Pattern[str]] = re.compile(
    rf"(?<![\d./\-])(?:(?:19|20)\d{{2}}[/.]{_MONTH}[/.]{_DAY}|{_DAY}[/.\-]{_MONTH}[/.\-](?:(?:19|20)\d{{2}}|\d{{2}}))"
    r"(?![\d/\-]|\.\d)"
)
_DECIMAL: Final[re.Pattern[str]] = re.compile(r"(?<![\d.\w])(\d{1,9})\.(\d{1,8})(?![\d]|\.\d)")

_URL: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:\b[a-z][a-z0-9+.\-]{1,15}://|\bwww\.)[^\s<>\"'`]+"
    # A bare host and a path. It starts only where a run of host characters starts, and never right
    # after an `@`: `jane.roe@example.com/sales` is an address followed by a path, not a URL, and the
    # e-mail scanner must see the whole address. The run is read once (possessive), not from every
    # position, so `a.a.a...` and `1-1-1-...` are linear.
    r"|(?<![a-z0-9\-.@])(?:[a-z0-9\-]++\.)++[a-z]{2,24}/[^\s<>\"'`]*"
)
_CARD: Final[re.Pattern[str]] = re.compile(r"(?<!\d)\d(?:(?: ?[\-_/,] ?| )?\d){12,18}(?!\d)")
"""13 to 19 digits, in one run or joined by a space, `-`, `_`, `/` or `,` (`4111 - 1111 - 1111 - 1111`)."""
_IBAN: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{2}(?: ?[A-Za-z0-9]{4}){2,7}(?: ?[A-Za-z0-9]{1,3})?(?![A-Za-z0-9])"
)
_IPV4: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
_IPV6: Final[re.Pattern[str]] = re.compile(r"(?<![\w:.])(?=[0-9A-Fa-f:.]*:)[0-9A-Fa-f:.]{3,45}(?![\w:])")
_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9+/=_\-])[A-Za-z0-9+/=_\-]{20,}(?![A-Za-z0-9+/=_\-])"
)
_TOKEN_SEGMENT: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]+|\d{1,4}[A-Za-z]{0,2}|[A-Za-z]{1,10}\d{1,2}")
"""A readable piece of a name: a word, a number, a word with a short number (`q3`), a short number with a
window unit (`90d`, `12m`, `4wk`: `total_spend_last_90d` is a feature name, not a key)."""
_TOKEN_SPLIT: Final[re.Pattern[str]] = re.compile(r"[_\-/+=]+")
_NUMBER: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(?<!\d\.)\d(?:[ \-]?\d){8,}(?!\d)")
_SPACED_NUMBER: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(?<!\d\.)\d{3,}(?: - ?| ?- )\d{3,}(?:(?: - ?| ?- )\d{3,})*(?!\d)"
)
"""Groups of three or more digits joined by a hyphen with a space on at least one side (`98765 - 43210`,
`2345 - 6789 - 0123`): `_nine_digits` wants nine. Groups of one or two digits (`12 - 34 - 56`) are a list."""
_DELIMITERS: Final[str] = r"\s@<>()\[\],;:\""
_EMAIL_LOOSE: Final[re.Pattern[str]] = re.compile(
    rf"(?<![^{_DELIMITERS}])[^{_DELIMITERS}]++@[^\W\d_][^{_DELIMITERS}'/\\!?]{{2,}}(?<![.\-])"
)
"""An identifier shaped like an address without the dot and ASCII top-level domain `engine.pii` needs:
a UPI id (`jane.roe@okhdfcbank`), a host on an intranet (`x@intranet`), an internationalised domain
(`ravi@example.भारत`, `.рф`, `.テスト`). The host starts with a letter and has at least three characters,
so an `@` in prose (`@jane`, `10 @ 5`, `meet@5pm`) is not one. Linear like `engine.pii`'s pattern: the
local part is possessive and starts only at the start of a run."""
_PAN_SEPARATED: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{5}(?:[ \-]\d{4}[ \-]?|\d{4}[ \-])[A-Za-z](?![A-Za-z0-9])"
)
"""A PAN as it is typed from a card (`ABCDE 1234 F`, `ABCDE-1234-F`); `engine.pii` reads the run `ABCDE1234F`."""
_PHONE_BRACKETED: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w+])(?:(?:\+|00)?\d{1,3}[ \-.]?)?[(\[]\d{2,5}[)\]](?:[ \-.]?\d{2,5}){1,4}(?!\d)"
)
"""A phone number with a bracketed block `engine.pii`'s three shapes do not read: `+91 (98765) 43210`,
`+91 [98765] 43210`, `+7 (495) 123-45-67`. It needs nine digits (`_nine_digits`), so `(2019) 2020` is not one."""
_MARKER: Final[str] = r"\[REDACTED:[a-z]+\]"
_WORD_CHARS: Final[str] = r"[A-Za-z0-9_\-+/=]"
_GLUED: Final[re.Pattern[str]] = re.compile(
    rf"(?<!{_WORD_CHARS})(?:{_WORD_CHARS}*+{_MARKER})++{_WORD_CHARS}*+"
)
_GLUED_KEEP: Final[int] = 5
"""A word with a marker inside it and more than this many characters around it is the rest of a value
whose middle something else already masked (`zk_live_51HxAbCdEf[REDACTED:phone]45`): all of it goes.
Linear: a run of word characters is read once, from its start."""
_JSON_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"(?<!\\)\"(?:[^\"\\\n]|\\.)*+\"|(?<=[:\[,])(?P<number> ?-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?=[,\]}])"
)
"""A JSON string (on one line), or a bare JSON number that follows `:` `[` or `,` and ends at `,` `]` `}`."""
_EMAIL: Final[re.Pattern[str]] = dict(TEXT_SCANNERS)["email"]


def _luhn(digits: str) -> bool:
    total = 0
    for index, ch in enumerate(reversed(digits)):
        value = int(ch)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str | None:
    """The match with its card replaced by a marker, or None when no run of digit groups in it is one.

    The pattern is greedy, so a card followed by another number (`4111 1111 1111 1111, 203.0.113.42`)
    matches more than the card and the whole match fails the Luhn check. The card is then looked for
    among the groups of digits: the longest run of whole groups, 13 to 19 digits, that passes it."""
    raw = match.group(0)
    groups = [(m.start(), m.end()) for m in re.finditer(r"\d+", raw)]
    for first in range(len(groups)):
        for last in range(len(groups) - 1, first - 1, -1):
            digits = "".join(raw[a:b] for a, b in groups[first : last + 1])
            if 13 <= len(digits) <= 19 and _luhn(digits):
                return raw[: groups[first][0]] + marker_for("card") + raw[groups[last][1] :]
    return None


def _sub_cards(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        masked = _mask_card(match)
        if masked is None:
            return match.group(0)
        count += 1
        return masked

    return _CARD.sub(replace, text), count


def _is_iban(match: re.Match[str]) -> bool:
    compact = re.sub(r" ", "", match.group(0))
    return 12 <= len(compact) <= 34 and sum(ch.isdigit() for ch in compact) >= 6


def _is_ipv4(match: re.Match[str]) -> bool:
    return all(int(part) <= 255 for part in match.group(0).split("."))


def _is_ipv6(match: re.Match[str]) -> bool:
    candidate = match.group(0).rstrip(".")
    if candidate.count(":") < 2:
        return False
    try:
        ipaddress.IPv6Address(candidate)
    except ValueError:
        return False
    return True


def _is_token(match: re.Match[str]) -> bool:
    text = match.group(0)
    if not (any(ch.isdigit() for ch in text) and any(ch.isalpha() for ch in text)):
        return False
    segments = [part for part in _TOKEN_SPLIT.split(text) if part]
    readable = len(segments) >= 3 and all(_TOKEN_SEGMENT.fullmatch(part) for part in segments)
    return not readable  # `campaign_summer_promo_2024_q3` is a name, `zk_live_51Hx...` is a key


def _has_residue(match: re.Match[str]) -> bool:
    return len(re.sub(_MARKER, "", match.group(0))) > _GLUED_KEEP


def _is_shape(match: re.Match[str]) -> bool:
    """A chain with a letter is a shape; one of digits only needs two or more groups of 2-4 nines
    (`9999 9999`), so a lone `9` next to real digits is not cut out of a number."""
    chain = match.group(0)
    if any(ch in "Aa" for ch in chain):
        return True
    groups = re.findall(r"9+", chain)
    return len(groups) >= 2 and all(2 <= len(group) <= SHAPE_RUN for group in groups)


def _nine_digits(match: re.Match[str]) -> bool:
    return sum(ch.isdigit() for ch in match.group(0)) >= 9


def _keep_decimal(match: re.Match[str]) -> bool:
    """A decimal is kept only when it is a number and not a piece of a phone number.

    `98765.43210` and `9876.543210` (ten digits with four or more on each side of the dot) are a mobile
    number written with a dot; `+91 98765 43210.0` (a phone column that went through a float cast) is a
    number whose last group looks like a decimal: it follows a group of three or more digits and a
    separator. Neither is kept, so the phone scanner sees the whole number."""
    whole, fraction = match.group(1), match.group(2)
    if len(whole) > 8:
        return False
    if len(whole) >= 4 and len(fraction) >= 4 and len(whole) + len(fraction) >= 10:
        return False
    return not (
        len(whole) >= 3 and re.search(r"\d{3}[ \-]$", match.string[max(0, match.start() - 4) : match.start()])
    )


def _sub(
    pattern: re.Pattern[str], kind: str, text: str, accept: Callable[[re.Match[str]], bool] | None = None
) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        if accept is not None and not accept(match):
            return match.group(0)
        count += 1
        return marker_for(kind)

    return pattern.sub(replace, text), count


def _hold(text: str, *, json_numbers: bool = False) -> tuple[str, list[str]]:
    """Set aside what must survive the scanners: shapes, UUIDs, dates and short decimals (`2024-01-31`, `12345.678`).

    With `json_numbers` (the backstop over a rendered prompt) a bare JSON number - `"rows": 1000000` - is
    set aside too, up to eight digits: the phone pattern must not read a seven-digit count as a phone number.
    A bare number of nine digits or more is scanned like text (the gate masks such a cell number, so one
    reaching the backstop is a leak the gate missed). A number inside a
    JSON *string* is never held (the tokenizer reads strings first, one line at a time)."""
    held: list[str] = []

    def keep(match: re.Match[str]) -> str:
        held.append(match.group(0))
        return f"{_HOLD_OPEN}{len(held) - 1}{_HOLD_CLOSE}"

    def keep_decimal(match: re.Match[str]) -> str:
        return keep(match) if _keep_decimal(match) else match.group(0)

    def keep_number(match: re.Match[str]) -> str:
        number = match.group("number")
        if number is None:
            return match.group(0)
        # A count or a bound is held; a bare number of nine digits or more is not: no file has that many
        # rows, so it is a cell the gate should have masked (a phone-like float statistic), and the
        # backstop is exactly for what the gate missed.
        whole = re.sub(r"\D", "", number.split(".", 1)[0].split("e", 1)[0].split("E", 1)[0])
        return match.group(0) if len(whole) >= 9 else keep(match)

    if json_numbers:
        text = _JSON_TOKEN.sub(keep_number, text)
    text = _SHAPE_TOKEN.sub(lambda match: keep(match) if _is_shape(match) else match.group(0), text)
    text = _UUID.sub(keep, text)
    text = _ISO_DATE.sub(keep, text)
    text = _OTHER_DATE.sub(keep, text)
    return _DECIMAL.sub(keep_decimal, text), held


def _release(text: str, held: Sequence[str]) -> str:
    return _HELD.sub(lambda match: held[int(match.group(1))], text)


def _scan(text: str, *, json_numbers: bool = False) -> tuple[str, int]:
    """`text` with every value that looks like personal data replaced by a marker, and how many.

    Structured scanners run first so a whole card, key or address is one marker and no half of it is
    left behind for the phone shape to take; then the platform's own detector (`redact_text`: e-mail,
    phone, PAN, Aadhaar); then a bracketed phone number and any other run of nine digits. UUIDs, dates
    and short decimals are set aside first and put back last. Every pattern is linear in the length of
    the text (`tests/unit/agent/test_review_bypass_11.py`).
    """
    text = text.replace(_HOLD_OPEN, "").replace(_HOLD_CLOSE, "")
    text, held = _hold(text, json_numbers=json_numbers)
    total = 0
    for pattern, kind, accept in (
        (_URL, "url", None),
        (_EMAIL, "email", None),
        (_EMAIL_LOOSE, "email", None),
    ):
        text, count = _sub(pattern, kind, text, accept)
        total += count
    text, count = _sub_cards(text)
    total += count
    for pattern, kind, accept in (
        (_IBAN, "iban", _is_iban),
        (_IPV4, "ip", _is_ipv4),
        (_IPV6, "ip", _is_ipv6),
        (_TOKEN, "token", _is_token),
    ):
        text, count = _sub(pattern, kind, text, accept)
        total += count
    text, kinds = redact_text(text)
    total += len(kinds)
    for pattern, kind, accept in (
        (_PAN_SEPARATED, "pan", None),
        (_PHONE_BRACKETED, "phone", _nine_digits),
        (_NUMBER, "number", None),
        (_SPACED_NUMBER, "number", _nine_digits),
    ):
        text, count = _sub(pattern, kind, text, accept)
        total += count
    text, count = _sub(_GLUED, "token", text, _has_residue)
    return _release(text, held), total + count


_INVISIBLE: Final[frozenset[str]] = frozenset(
    "\u034f\u115f\u1160\u17b4\u17b5\u180b\u180c\u180d\u3164\u2800\uffa0"
)
"""Characters that draw nothing but are not in Unicode's `C*` categories: the combining grapheme joiner,
the Hangul and Khmer fillers, the Mongolian variation selectors, the braille blank."""
_DROPPED: Final[frozenset[str]] = frozenset({"Cf", "Cs", "Co", "Cn"})
_WHITESPACE_CONTROLS: Final[str] = "\t\n\r\f\v"


def _is_invisible(ch: str, category: str, previous: str) -> bool:
    """A character that draws nothing, or a combining mark on an ASCII character (`previous`)."""
    if ch in _INVISIBLE or category in _DROPPED:
        return True
    if category == "Cc":
        return ch not in _WHITESPACE_CONTROLS
    return category in {"Mn", "Me"} and (not previous or previous.isascii())


def _fold(text: str) -> str:
    """`text` as a scanner should read it, layout kept: compatibility-folded (full-width digits, `＠`),
    every dash written as `-`, no character that draws nothing.

    What a person reads as `98765 - 43210` (typed with an en dash) or as a number written in keycap emoji
    must be what the scanners read. Besides NFKC this maps every Unicode dash (category `Pd`, and the minus
    sign U+2212) to `-`, drops the `C*` categories except white space and the fillers in `_INVISIBLE`, and
    drops a combining mark (`Mn`, `Me`: variation selectors, the keycap `U+20E3`) that sits on an ASCII
    character. A mark on a letter of another script (a Devanagari vowel sign) is kept.
    """
    text = unicodedata.normalize("NFKC", str(text))
    if text.isascii():
        return text
    out: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if category == "Pd" or ch == "\u2212":
            out.append("-")
        elif not _is_invisible(ch, category, out[-1] if out else ""):
            out.append(ch)
    return "".join(out)


def _normalise(text: str) -> str:
    """Fold (`_fold`), drop invisible characters and collapse whitespace before scanning."""
    return clean_text(_fold(text), limit=1_000_000)


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    open_at = head.rfind("[REDACTED:")
    if open_at != -1 and "]" not in head[open_at:]:
        head = head[:open_at]  # never end inside a marker
    return head.rstrip() + "…"


def mask_value(
    text: str, mode: DataAccess = DataAccess.MASKED_DATA, *, limit: int | None = MAX_CELL_CHARS
) -> str:
    """One cell value as the model may see it.

    `masked_data`: the text folded and stripped (`_normalise`: full-width look-alikes, dashes, invisible
    characters), then every scanner (`_scan`: URL, e-mail and address-shaped identifiers, card, IBAN, IP,
    token, `engine.pii`'s e-mail, phone, PAN and Aadhaar, a separated PAN, a bracketed phone and any run of
    nine digits), then a cut to `limit` characters. `summaries_only`: the value's shape, and nothing of the
    value.
    A whole cell is masked before it is cut.
    """
    cleaned = _normalise(text if limit is None else str(text)[: max(limit, 1) * 25])
    if mode is DataAccess.SUMMARIES_ONLY:
        return shape_of(cleaned)
    scanned = _scan(cleaned)[0]
    return scanned if limit is None else _cut(scanned, limit)


def scrub(text: str) -> str:
    """Text written by a person or by our own rules with every scanner applied, uncut."""
    return _scan(_normalise(text))[0]


def assert_clean(prompt: str) -> tuple[str, int]:
    """The rendered prompt, re-scanned: what still matches a scanner is masked, and how many were.

    Nothing should match - every value went through `prepare` - so a non-zero count is a bug or an
    injection somewhere upstream, and the caller records it (`EGRESS_LATE_MASK`) instead of sending
    the original. The prompt is scanned as a scanner reads it (`_fold`: dashes, invisible characters,
    full-width look-alikes), but a prompt that matches nothing is returned byte for byte, and the
    layout (lines, spacing) is kept either way. Markers, UUIDs, dates, short decimals and the bare
    JSON numbers of a tool result (`"rows": 1000000`) are left alone.
    """
    cleaned, count = _scan(_fold(prompt), json_numbers=True)
    return (cleaned, count) if count else (prompt, 0)


# ---------------------------------------------------------------------------
# Column labels
# ---------------------------------------------------------------------------
_PLAIN_NAME: Final[re.Pattern[str]] = re.compile(r"[^\W\d_][\w \-]*")


def is_plain_label(name: str) -> bool:
    """A name that reads as ordinary words: letters, digits, spaces, `_` and `-`; at most 64 characters.

    No `@`, no run of six digits, nothing that looks like a URL or a path, and nothing a scanner
    would change (a name shaped like a phone number is not a label).
    """
    return (
        0 < len(name) <= MAX_NAME_CHARS
        and _PLAIN_NAME.fullmatch(name) is not None
        and re.search(r"\d{6,}", name) is None
        and scrub(name) == name
    )


def alias_table(columns: Iterable[object]) -> dict[str, str]:
    """`{real name: column_<n>}` for each column that is not a plain label (n is its 1-based position).

    A pure function of the column list, so it is the same on every request for one file and needs no
    storage. An alias never equals a real column name.
    """
    names = [str(column) for column in columns]
    taken = set(names)
    table: dict[str, str] = {}
    for position, name in enumerate(names, start=1):
        if is_plain_label(name):
            continue
        alias = f"column_{position}"
        while alias in taken:
            alias += "x"
        taken.add(alias)
        table[name] = alias
    return table


def safe_column_label(name: str, table: Mapping[str, str] | None = None) -> str:
    """The name the model is shown: its alias from `table` when it has one, itself when plain."""
    if table is not None and name in table:
        return table[name]
    return name if is_plain_label(name) else "(unnamed column)"


def _shown_in_full(name: str) -> str:
    """A name as text shows it: invisible characters dropped, whitespace collapsed, not cut."""
    return clean_text(unicodedata.normalize("NFKC", name), limit=1_000_000)


def _alias_pattern(table: Mapping[str, str]) -> tuple[re.Pattern[str] | None, dict[str, str]]:
    needles: dict[str, str] = {}
    for real, alias in table.items():
        for form in (real, _shown_in_full(real), display_name(real)):
            needles.setdefault(form, alias)
    # A header that is only a number (`2020`, `0.05`, `500.0`) is aliased where it is a label or a whole
    # cell (`_known`) but never inside a sentence: there it cannot be told from the number itself.
    findable = [n for n in needles if _NUMERIC_NAME.fullmatch(n) is None]
    long_names = sorted((n for n in findable if len(n) >= 4), key=len, reverse=True)
    short_names = sorted((n for n in findable if len(n) < 4), key=len, reverse=True)
    parts = [_bounded(n) for n in long_names]
    parts += [rf"(?<=['\"`]){re.escape(n)}(?=['\"`])" for n in short_names]
    return (re.compile("|".join(parts)) if parts else None), needles


_NUMERIC_NAME: Final[re.Pattern[str]] = re.compile(r"[+\-]?[\d.,:/\- ]+")


def _bounded(name: str) -> str:
    """`name` as a pattern that matches it only as a whole word or number, never inside a longer one."""
    left = (
        r"(?<![\w.\-])" if name[0].isdigit() else (r"(?<!\w)" if name[0].isalnum() or name[0] == "_" else "")
    )
    right = (
        r"(?![\w]|[.\-]\d)"
        if name[-1].isdigit()
        else (r"(?!\w)" if name[-1].isalnum() or name[-1] == "_" else "")
    )
    return f"{left}{re.escape(name)}{right}"


_ALIAS_TOKEN: Final[re.Pattern[str]] = re.compile(r"\bcolumn_\d+x*\b")


def unalias(text: str, table: Mapping[str, str]) -> str:
    """`text` with each alias in `table` replaced by the real column name, when that name is clean.

    A real name that a scanner would change (an e-mail address, a long number) stays an alias in
    the stored reply: it would otherwise be personal data in a transcript a Viewer can read.
    """
    shown = {real: _shown_in_full(real) for real in table}
    # A name longer than `MAX_NAME_CHARS` is never given back (and never scanned: the reply must not cost
    # more the longer a client's header is); a shorter one only when no scanner would change it.
    back = {
        alias: shown[real]
        for real, alias in table.items()
        if len(shown[real]) <= MAX_NAME_CHARS and scrub(shown[real]) == shown[real]
    }
    return _ALIAS_TOKEN.sub(lambda match: back.get(match.group(0), match.group(0)), text)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
_QUOTED: Final[re.Pattern[str]] = re.compile(r"(?<!\w)'((?:[^'\n]|(?<=\w)'(?=\w)){1,120}?)'(?!\w)")
"""A quoted piece of a sentence. An apostrophe between two word characters (`'Men's Wear'`, `'O'Brien'`) is
part of the piece; `untrusted.quoted` does not escape it. This is the fallback for a sentence that has no
structured `examples` (`Egress.sentences` replaces those piece by piece, without reading any quote)."""


@dataclass(frozen=True)
class Egress:
    """One turn's gate: which columns exist, which are hidden, which mode, which aliases."""

    columns: tuple[str, ...]
    mode: DataAccess = DataAccess.MASKED_DATA
    personal: frozenset[str] = frozenset()
    always_hide: frozenset[str] = frozenset()
    known_text: frozenset[str] = frozenset()
    aliases: Mapping[str, str] = field(default_factory=dict)
    _exact: dict[str, str] = field(default_factory=dict, init=False, repr=False, compare=False)
    _folded: dict[str, str | None] = field(default_factory=dict, init=False, repr=False, compare=False)
    _hidden_labels: dict[str, str] = field(default_factory=dict, init=False, repr=False, compare=False)
    _alias_re: re.Pattern[str] | None = field(default=None, init=False, repr=False, compare=False)
    _needles: dict[str, str] = field(default_factory=dict, init=False, repr=False, compare=False)

    @classmethod
    def build(
        cls,
        columns: Iterable[object],
        *,
        personal_columns: Iterable[str] = (),
        always_hide: Iterable[str] = (),
        mode: DataAccess = DataAccess.MASKED_DATA,
        known_text: Iterable[str] = (),
    ) -> Egress:
        names = tuple(str(column) for column in columns)
        return cls(
            columns=names,
            mode=DataAccess(mode),
            personal=frozenset(str(name).casefold() for name in personal_columns),
            always_hide=frozenset(str(name).casefold() for name in always_hide),
            known_text=frozenset(known_text),
            aliases=alias_table(names),
        )

    # -- names ---------------------------------------------------------------
    def label(self, column: str) -> str:
        """A real column name as the model sees it: its alias, or its cleaned display name."""
        return self.aliases.get(column) or display_name(column)

    def __post_init__(self) -> None:
        exact: dict[str, str] = {}
        for column in self.columns:
            shown = self.label(column)
            for form in (column, _shown_in_full(column), display_name(column)):
                exact.setdefault(form, shown)
        for alias in self.aliases.values():
            exact.setdefault(alias, alias)
        folded: dict[str, str | None] = {}
        for name, shown in exact.items():
            key = name.casefold()
            folded[key] = (
                shown if folded.get(key, shown) == shown else None
            )  # two columns differ only by case
        marked: dict[str, str] = {}
        for column in self.columns:
            marker = self._hidden_marker(column)
            if marker is not None:
                for form in (column, _shown_in_full(column), display_name(column), self.label(column)):
                    marked.setdefault(form, marker)
        object.__setattr__(self, "_exact", exact)
        object.__setattr__(self, "_folded", folded)
        object.__setattr__(self, "_hidden_labels", marked)
        pattern, needles = _alias_pattern(self.aliases)
        object.__setattr__(self, "_alias_re", pattern)
        object.__setattr__(self, "_needles", needles)

    def _known(self, value: str) -> str | None:
        """The label of the column `value` names (exactly, or by case when only one column fits), else None."""
        if value in self._exact:
            return self._exact[value]
        return self._folded.get(value.casefold())

    def _hidden_marker(self, name: str) -> str | None:
        folded = name.casefold()
        shown = display_name(name).casefold()
        if folded in self.always_hide or shown in self.always_hide:
            return HIDDEN_BY_SETTINGS
        if folded in self.personal or shown in self.personal:
            return PERSONAL_DATA
        return None

    def apply_aliases(self, text: str) -> str:
        """`text` with every real name that is not a plain label replaced by its alias."""
        if self._alias_re is None:
            return text
        return self._alias_re.sub(lambda match: self._needles[match.group(0)], text)

    def unalias(self, text: str) -> str:
        return unalias(text, self.aliases)

    def unalias_args(self, value: Any) -> Any:
        """Arguments the model wrote with aliases, back to the file's names (exact matches only)."""
        back = {alias: real for real, alias in self.aliases.items()}
        if isinstance(value, str):
            return back.get(value, value)
        if isinstance(value, Mapping):
            return {key: self.unalias_args(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.unalias_args(item) for item in value]
        return value

    def person_text(self, text: str) -> str:
        """Text for the person: aliases, then the real names again where those are clean."""
        return self.unalias(self.apply_aliases(text))

    def scrub(self, text: str) -> str:
        """A person's message: every scanner, never a shape (it is theirs, not the file's)."""
        return scrub(text)

    # -- text our own rules wrote -------------------------------------------
    def text(self, value: str, *, examples: bool = False, hide: str | None = None) -> str:
        """A sentence from the advisor or a check: real names aliased, every scanner applied.

        With `examples` (the advisor's sentences, which quote cell values) and in `summaries_only`, a
        quoted piece that is not a column name or a known setting label is a cell example
        (`'12/03/2024'`) and becomes its shape. A check's message quotes column names only. `hide` is
        the marker for a sentence known to be about a hidden column (its siblings named it); it acts as
        if the sentence named that column itself.
        """
        aliased = self.apply_aliases(clean_text(value, 1_000_000))
        if examples:
            hide = hide or self._hidden_mention(aliased)
            if hide is not None or self.mode is DataAccess.SUMMARIES_ONLY:
                aliased = _QUOTED.sub(lambda match: self._quoted(match, hide), aliased)
        return scrub(aliased)

    def sentences(
        self, texts: Sequence[str], examples: Sequence[str], *, columns: Iterable[str] = ()
    ) -> list[str]:
        """The sentences of ONE suggestion or question (title, reason, question text), as the model may read them.

        The advisor knows which cells it quoted (`examples`), so nothing has to be read back out of the
        prose: each `quoted(cell)` in any of `texts` is replaced, in one pass, by the cell's shape
        (`summaries_only`), the hidden marker, or the masked cell. A suggestion is hidden **as a whole**
        when it is about a hidden column - `columns` (the step's column) or a column any of its sentences
        names in quotes - so a title that names the column also hides the examples in the reason that
        does not. What is left is still read for quoted pieces (`text`), for a session saved before
        `examples` existed.
        """
        hide = self._hidden_of([*columns, *[m.group(1) for t in texts for m in _QUOTED.finditer(t)]])
        table = {quoted(cell): f"'{self._example(cell, hide)}'" for cell in examples if cell}
        if table:
            pattern = re.compile("|".join(re.escape(k) for k in sorted(table, key=len, reverse=True)))
            texts = [pattern.sub(lambda match: table[match.group(0)], t) for t in texts]
        return [self.text(t, examples=True, hide=hide) for t in texts]

    def _hidden_of(self, names: Iterable[str]) -> str | None:
        for name in names:
            marker = self._hidden_labels.get(name) or self._hidden_marker(name)
            if marker is not None:
                return marker
        return None

    def _example(self, cell: str, hide: str | None) -> str:
        """One quoted cell as the model may read it: the hidden marker, its shape, or the cell masked."""
        if hide is not None:
            return hide
        if self.mode is DataAccess.SUMMARIES_ONLY:
            return shape_of(_normalise(cell))
        return mask_value(cell)

    def _hidden_mention(self, text: str) -> str | None:
        """The marker for the first hidden column a sentence names in quotes, if any."""
        for match in _QUOTED.finditer(text):
            marker = self._hidden_labels.get(match.group(1))
            if marker is not None:
                return marker
        return None

    def _quoted(self, match: re.Match[str], hide: str | None) -> str:
        """A quoted piece of an advisor sentence: kept when it is a label, else a cell example.

        A sentence that names a hidden column shows none of its examples; in `summaries_only` any other
        example is reduced to its shape."""
        inner = match.group(1)
        if self._known(inner) is not None or inner in self.known_text or inner in SAFE_LITERALS:
            return match.group(0)
        if hide is not None:
            return f"'{hide}'"
        return f"'{shape_of(inner)}'"

    # -- the walk -------------------------------------------------------------
    def prepare(
        self,
        result: Mapping[str, Any],
        *,
        extra_text_keys: Iterable[str] = (),
    ) -> dict[str, Any]:
        """A tool result (or any JSON-like mapping) made safe to put in a prompt. See the module docstring.

        `extra_text_keys` names keys that hold our own sentences for this call only (the loop's state
        block and settings list); it never adds a key that could hold a cell.
        """
        walked = self._walk(result, "", None, frozenset(extra_text_keys), 0)
        return walked if isinstance(walked, dict) else {}

    def _walk(self, value: Any, key: str, hidden: str | None, extra: frozenset[str], depth: int) -> Any:
        if depth > MAX_DEPTH:
            return None
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return self._number(value, key, hidden)
        if isinstance(value, str):
            return self._string(value, key, hidden, extra)
        if isinstance(value, Mapping):
            return self._mapping(value, hidden, extra, depth)
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._walk(item, key, hidden, extra, depth + 1) for item in list(value)[:MAX_ITEMS]]
        return self._string(str(value), "", hidden, extra)

    def _mapping(
        self, value: Mapping[Any, Any], hidden: str | None, extra: frozenset[str], depth: int
    ) -> dict[str, Any]:
        names = [value.get(k) for k in ("column", "name")]
        columns = value.get("columns")
        rows = value.get("rows")
        # `columns` + `rows` (rows of cells, one per column): a hidden column hides ITS cells, not the whole
        # result, so the other columns and the counts stay readable (`sample_rows`).
        positional: list[str | None] | None = None
        if (
            isinstance(columns, (list, tuple))
            and isinstance(rows, (list, tuple))
            and all(isinstance(r, (list, tuple)) for r in rows)
        ):
            positional = [self._hidden_marker(c) if isinstance(c, str) else None for c in columns]
        elif isinstance(columns, (list, tuple)):
            names.extend(columns)
        for name in names:
            if hidden is None and isinstance(name, str):
                hidden = self._hidden_marker(name)
        row = self._is_row(value)
        out: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            child_hidden = hidden or self._hidden_marker(key)
            shown = self._key(key, hidden)
            while shown in out:
                shown += "_"
            if positional is not None and key == "rows":
                out[shown] = [
                    [
                        (
                            positional[i]
                            if i < len(positional) and positional[i] is not None
                            else self._walk(cell, "", hidden, extra, depth + 2)
                        )
                        for i, cell in enumerate(cells)
                    ]
                    for cells in list(item)[:MAX_ITEMS]
                ]
                continue
            # A row keyed by column names holds cells even under a key like `name` or `type`.
            out[shown] = self._walk(
                item, "" if row and key not in extra else key, child_hidden, extra, depth + 1
            )
        return out

    def _is_row(self, value: Mapping[Any, Any]) -> bool:
        """A dict keyed by column names is a row of cells: its keys must not be read as labels.

        It is one when a key names a column that is not itself a label key, or when *every* key names a
        column (a file whose columns are all called `name` or `type`). A profile entry has keys like
        `null_rate` that are not columns, so it is not mistaken for a row.
        """
        named = [self._known(str(k)) is not None for k in value]
        return any(hit and str(k) not in SAFE_KEYS for hit, k in zip(named, value, strict=True)) or (
            bool(named) and all(named)
        )

    def _key(self, key: str, hidden: str | None = None) -> str:
        """A dict key as the model sees it.

        A column name is its label; a fixed field name (`SAFE_KEYS`, `RESULT_KEYS`) stays. Any other key may
        be a cell value: under a hidden column it becomes the hidden marker and in `summaries_only` its shape.
        In `masked_data` a label-shaped key passes and the rest is masked like a value."""
        known = self._known(key)
        if known is not None:
            return known
        if key in SAFE_KEYS or key in RESULT_KEYS:
            return key
        if hidden is not None:
            return hidden
        if self.mode is DataAccess.SUMMARIES_ONLY:
            return shape_of(_normalise(key)) or "key"
        if _KEY_LABEL.fullmatch(key) and scrub(key) == key:
            return key
        return mask_value(key, self.mode, limit=MAX_NAME_CHARS) or "key"

    def _number(self, value: float, key: str, hidden: str | None) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        counted = _COUNT_KEY.fullmatch(key) is not None
        if hidden is not None:
            return value if counted else None
        stat = _STAT_KEY.fullmatch(key) is not None
        if self.mode is DataAccess.SUMMARIES_ONLY and not (counted or stat):
            return shape_of(repr(value))
        if not counted and len(str(abs(int(value)))) >= 9:
            return marker_for("number")
        return value

    def _string(self, value: str, key: str, hidden: str | None, extra: frozenset[str]) -> str:
        stripped = value.strip()
        if stripped in SAFE_LITERALS or REDACTION_MARKER_PATTERN.fullmatch(stripped):
            return value
        known = self._known(value)
        if known is not None:
            return known
        if key in TEXT_KEYS or key in extra:
            return self.text(value, examples=key in extra)
        if key in ID_KEYS and key != "name" and ID_LABEL.fullmatch(value) and scrub(value) == value:
            return value
        if hidden is not None:
            return hidden
        return mask_value(self.apply_aliases(value), self.mode)


def prepare(
    result: Mapping[str, Any],
    *,
    ctx_columns: Iterable[object],
    personal_columns: Iterable[str],
    mode: DataAccess = DataAccess.MASKED_DATA,
    always_hide: Iterable[str] = (),
) -> dict[str, Any]:
    """A tool result made safe for a prompt: the function form of :meth:`Egress.prepare`."""
    gate = Egress.build(ctx_columns, personal_columns=personal_columns, always_hide=always_hide, mode=mode)
    return gate.prepare(result)


# ---------------------------------------------------------------------------
# What was sent
# ---------------------------------------------------------------------------
def sent_item(
    gate: Egress, tool: str, args: Mapping[str, Any], payload: str, *, extra_text_keys: Iterable[str] = ()
) -> SentItem:
    """What the transcript records for one tool result that went into a prompt.

    The preview is the payload **after the last check** (`assert_clean`), never before: whatever the
    backstop would mask in the prompt is masked here too, so the session file and every API answer
    hold what left, and never a value the last check had to catch."""
    checked, _ = assert_clean(payload)
    return SentItem(
        tool=tool,
        args=gate.prepare(args, extra_text_keys=extra_text_keys),
        preview=checked[:MAX_SENT_PREVIEW_CHARS],
        chars=len(checked),
        mode=gate.mode,
    )


def cap_sent(transcript: Sequence[ChatMessage]) -> tuple[ChatMessage, ...]:
    """The transcript with at most 25 items per message, 300 items and 20 KB of previews in all, newest first."""
    budget = MAX_SENT_SESSION_CHARS
    room = MAX_SENT_SESSION_ITEMS
    kept: list[ChatMessage] = []
    for message in reversed(transcript):
        items: list[SentItem] = []
        for item in message.sent[: min(MAX_SENT_ITEMS, room)]:
            room -= 1
            if len(item.preview) <= budget:
                budget -= len(item.preview)
                items.append(item)
            else:  # the note keeps the tool and the length; the arguments go too, so it stays small
                items.append(item.model_copy(update={"preview": SENT_DROPPED, "args": {}}))
        kept.append(message.model_copy(update={"sent": tuple(items)}) if message.sent else message)
    return tuple(reversed(kept))


def is_third_party(llm: Any) -> bool:
    """True when the chat model runs outside the platform's own account (any backend but the two below)."""
    backend = getattr(llm, "backend", llm)
    return str(getattr(backend, "value", backend)).lower() not in THIRD_PARTY_FREE_BACKENDS
