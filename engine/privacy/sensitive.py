"""Sensitive columns and long free text: found by our code, kept away from the features and from the AI.

Two small detectors, both reading the word lists in `configs/doctor/sensitive_words.yaml`:

* :func:`find_sensitive` - a column about religion, caste, health, disability or a pharmacy-like category, found by
  a whole word in its name or by the words in its values. Such a column is left out of the features
  (`engine.stages.prepare`) and out of what the AI sees (`AgentContext`), unless the person allows it: the question
  is "Fairness report only (recommended) / remove / use". "Fairness report only" makes it the use case's
  `evaluation.fairness_column` (carried, never trained on); "use" puts it in `prepare.allow_sensitive_columns`.
* :func:`find_long_text` - a column named notes, remarks or comments, or whose median length is over 40
  characters. The AI is shown its counts and length, never the text.

Nothing here returns or logs a cell: the answers are column names, a kind and a count.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    "FAIRNESS_KIND",
    "SENSITIVE_PATH",
    "SENSITIVE_QUESTION",
    "SENSITIVE_USES",
    "LongText",
    "Sensitive",
    "Words",
    "find_long_text",
    "find_sensitive",
    "load_words",
]

SENSITIVE_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "configs" / "doctor" / "sensitive_words.yaml"
)
FAIRNESS_KIND: Final[str] = "sensitive_fairness"
"""The case item kind of the answer "fairness report only". "Use it" is a `pii_keep` item (an approver's yes)."""
SENSITIVE_QUESTION: Final[str] = "SENSITIVE_COLUMN"
"""The question's code."""
SENSITIVE_USES: Final[tuple[tuple[str, str], ...]] = (
    ("fairness_only", "Fairness report only (recommended)"),
    ("remove", "Remove it"),
    ("use", "Use it"),
)
"""The three answers as `(use, label)`; the first is the recommended one."""

_SAMPLE: Final[int] = 1_000
_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")
_CAMEL: Final[re.Pattern[str]] = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class Words:
    """The word lists, read once."""

    value_share: float
    max_distinct: int
    names: dict[str, frozenset[str]]
    values: dict[str, tuple[str, ...]]
    long_names: frozenset[str]
    median_length: int


@dataclass(frozen=True)
class Sensitive:
    """One sensitive column: what it looks like it holds and how it was recognised."""

    column: str
    kind: str
    by: str  # "name" or "values"

    def reason(self) -> str:
        what = "its name" if self.by == "name" else "its values"
        return f"'{self.column}' looks like {self.kind} data ({what} say so)."


@dataclass(frozen=True)
class LongText:
    """One long free-text column."""

    column: str
    by: str  # "name" or "length"


@lru_cache(maxsize=4)
def _load(path: str) -> Words:
    document: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    long_text = document.get("long_text") or {}
    return Words(
        value_share=float(document.get("value_share", 0.2)),
        max_distinct=int(document.get("max_distinct", 100)),
        names={
            str(kind): frozenset(str(w).lower() for w in listed)
            for kind, listed in (document.get("names") or {}).items()
        },
        values={
            str(kind): tuple(" ".join(_WORD.findall(str(w).lower())) for w in listed)
            for kind, listed in (document.get("values") or {}).items()
        },
        long_names=frozenset(str(w).lower() for w in long_text.get("names") or ()),
        median_length=int(long_text.get("median_length", 40)),
    )


def load_words(path: Path | None = None) -> Words:
    """The word lists of `sensitive_words.yaml`."""
    return _load(str(path or SENSITIVE_PATH))


def _name_words(name: str) -> set[str]:
    return set(_WORD.findall(_CAMEL.sub(" ", str(name)).lower()))


def find_sensitive(
    frame: pd.DataFrame, *, skip: Iterable[str] = (), words: Words | None = None
) -> tuple[Sensitive, ...]:
    """The sensitive columns of `frame` (not those in `skip`), in column order."""
    words = words or load_words()
    skipped = {str(s) for s in skip}
    found: list[Sensitive] = []
    for column in frame.columns:
        name = str(column)
        if name in skipped:
            continue
        tokens = _name_words(name)
        kind = next((k for k, listed in words.names.items() if tokens & listed), None)
        if kind is not None:
            found.append(Sensitive(name, kind, "name"))
            continue
        kind = _by_values(frame[column], words)
        if kind is not None:
            found.append(Sensitive(name, kind, "values"))
    return tuple(found)


def _by_values(series: pd.Series[Any], words: Words) -> str | None:
    if series.dtype != object and str(series.dtype) not in {"string", "category"}:
        return None
    cells = series.dropna().head(_SAMPLE)
    if cells.empty or cells.nunique() > words.max_distinct:
        return None
    texts = [" " + " ".join(_WORD.findall(str(v).lower())) + " " for v in cells]
    for kind, phrases in words.values.items():
        needles = [f" {p} " for p in phrases if p]
        hits = sum(1 for text in texts if any(n in text for n in needles))
        if hits / len(texts) >= words.value_share:
            return kind
    return None


def find_long_text(
    frame: pd.DataFrame, *, skip: Iterable[str] = (), words: Words | None = None
) -> tuple[LongText, ...]:
    """The long free-text columns of `frame`: named notes, remarks or comments, or a median length over 40."""
    words = words or load_words()
    skipped = {str(s) for s in skip}
    found: list[LongText] = []
    for column in frame.columns:
        name = str(column)
        if name in skipped:
            continue
        series = frame[column]
        if series.dtype != object and str(series.dtype) != "string":
            continue
        if _name_words(name) & words.long_names:
            found.append(LongText(name, "name"))
            continue
        lengths = series.dropna().head(_SAMPLE).astype(str).str.len()
        if len(lengths) and float(lengths.median()) > words.median_length:
            found.append(LongText(name, "length"))
    return tuple(found)
