"""The column contract of a prescription, and the drift a replay checks against it (Plan O M243, DEC-1813).

A prescription (`engine.doctor.prescription`) freezes, per prepared column, what the data looked like when the person
approved it (the design §9.1 "Contract", [RES] D1): its type group, its empty share, how many distinct values it has
(a band, never a count of IDs), its 1st, 50th and 99th percentiles, its common values under the small-group rule, and
the outcome rate. :func:`learn_contract` writes it once, at PRESCRIBE. A replay never learns it again: it compares the
new prepared file with the frozen contract (:func:`check_contract`) and says what moved.

**What stops, and what is a warning** (the plan's acceptance, the design §9.3 and §7.1):

* A breach that breaks the prescription **stops** with `PRESCRIPTION_DRIFT` and one question: a column the
  contract has is gone (`MISSING_COLUMN`), a column changed its type group (`MEANING_CHANGED`: numbers became text,
  dates became numbers), or a column whose every value was listed at approval (a *closed* column: a few values,
  each held by enough rows to be listed) now holds a value it never held (`NEW_VALUE`). Nothing is re-learnt: the
  person answers, and a changed prescription is approved again.
* A breach that breaks nothing is a **warning with a question**: a new column (`NEW_COLUMN`), a new spelling in an
  open column holding more than 1% of rows (`NEW_CATEGORY`), and a value that moved outside what was approved
  (`VALUE_DRIFT`: the median outside the approved 1st-99th percentile range, the empty share moved by more than ten
  points, or the outcome rate moved by half of itself and at least five points).

**Small-group rule.** A value is listed only when at least :data:`MIN_GROUP` rows hold it, so the contract never holds
a value that names one person; the ID column's values are never listed, and neither are a column's whose values are
mostly distinct. A column with a value held by fewer rows is *open*: a new value there can only be a warning.

Rules only; no AI. Counts, shares, percentiles and the listed values only - never a row.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

import pandas as pd
from pydantic import Field

from engine.agent.contracts import Question, QuestionOption
from engine.config import StrictBase

__all__ = [
    "CLOSED_MAX",
    "EMPTY_SHARE_MOVE",
    "MIN_GROUP",
    "NEW_CATEGORY_SHARE",
    "ColumnContract",
    "DataContract",
    "Detector",
    "DriftFinding",
    "TypeGroup",
    "ValueShare",
    "check_contract",
    "drift_question",
    "learn_contract",
    "type_group",
    "value_text",
]

MIN_GROUP: Final[int] = 10
"""The small-group rule: a value is listed only when at least this many rows hold it."""
CLOSED_MAX: Final[int] = 20
"""A column whose every value is listed and that has at most this many values is closed: a new value stops."""
TOP_MAX: Final[int] = 50
"""At most this many common values are listed per column; a column with more is not checked for new spellings."""
NEW_CATEGORY_SHARE: Final[float] = 0.01
"""A new spelling in an open column is a warning above this share of rows (the design §7.1)."""
EMPTY_SHARE_MOVE: Final[float] = 0.10
"""The empty share may move by this much (ten points) before it is a warning."""
OUTCOME_RATE_MOVE: Final[float] = 0.5
OUTCOME_RATE_FLOOR: Final[float] = 0.05
"""The outcome rate may move by half of itself, and at least five points, before it is a warning."""
_PLACES: Final[int] = 6

TypeGroup = Literal["number", "date", "flag", "text"]
DistinctBand = Literal["none", "one", "few", "some", "many", "unique"]
Detector = Literal[
    "MISSING_COLUMN",
    "MEANING_CHANGED",
    "NEW_VALUE",
    "NEW_CATEGORY",
    "NEW_COLUMN",
    "VALUE_DRIFT",
    "MISSING_FILE",
    "INTAKE_CHANGED",
    "LOCALE_CHANGED",
    "CURRENCY_FOREIGN",
    "CURRENCY_MIXED",
]


class ValueShare(StrictBase):
    """One listed value and the share of non-empty rows that hold it."""

    value: str
    share: float = Field(ge=0.0, le=1.0)


class ColumnContract(StrictBase):
    """What one prepared column looked like at approval (the design §9.1 "Contract")."""

    column: str
    role: Literal["id", "outcome", "feature"] = "feature"
    type_group: TypeGroup
    empty_share: float = Field(ge=0.0, le=1.0)
    distinct: DistinctBand = Field(
        description="`none`, `one`, `few` (2-20), `some` (21-1,000), `many` (more), `unique` (every row its own)."
    )
    p1: float | None = Field(
        default=None, description="Numbers and dates (days since 1970): the 1st percentile."
    )
    p50: float | None = None
    p99: float | None = None
    top: tuple[ValueShare, ...] = Field(
        default=(), description="Text and flags: the common values, each held by at least `MIN_GROUP` rows."
    )
    complete: bool = Field(
        default=False,
        description="Every value held by at least `MIN_GROUP` rows is listed: a new common spelling can be told.",
    )
    closed: bool = Field(
        default=False, description="Every value is listed (at most `CLOSED_MAX`): a new value stops a replay."
    )
    outcome_rate: float | None = Field(default=None, description="The outcome column: the share of yes.")


class DataContract(StrictBase):
    """Every prepared column's contract, in the file's order."""

    columns: tuple[ColumnContract, ...] = ()

    def column(self, name: str) -> ColumnContract | None:
        return next((c for c in self.columns if c.column == name), None)


class DriftFinding(StrictBase):
    """One thing a replay found that differs from the prescription, with the one question it asks."""

    detector: Detector
    stops: bool = Field(description="True: the replay stops here (`PRESCRIPTION_DRIFT` or a currency code).")
    slot: str | None = Field(
        default=None, description="The file it is about, by its place in the prescription."
    )
    column: str | None = None
    summary: str = Field(description="What moved, in plain words; counts and shares only.")
    question: Question


# ---------------------------------------------------------------------------
# Learning (once, at PRESCRIBE)
# ---------------------------------------------------------------------------
def type_group(series: pd.Series[Any]) -> TypeGroup:
    """The column's type group, as `validate_against_schema` groups types: flags, numbers, dates, text."""
    if pd.api.types.is_bool_dtype(series):
        return "flag"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    return "text"


def value_text(value: Any) -> str:
    """A cell as the contract lists it: text trimmed, a whole number without `.0`, a flag as `true`/`false`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _round(value: Any) -> float:
    return round(float(value), _PLACES)


def _band(distinct: int, rows: int) -> DistinctBand:
    if distinct == 0:
        return "none"
    if distinct == 1:
        return "one"
    if distinct == rows and rows > CLOSED_MAX:
        return "unique"
    if distinct <= CLOSED_MAX:
        return "few"
    return "some" if distinct <= 1_000 else "many"


def _numbers(series: pd.Series[Any], group: TypeGroup) -> pd.Series[Any]:
    present = series.dropna()
    if group == "date":
        stamps = pd.to_datetime(present, errors="coerce", utc=True).dropna()
        epoch = pd.Timestamp("1970-01-01", tz="UTC")
        return ((stamps - epoch) / pd.Timedelta(days=1)).astype("float64")
    numbers = pd.to_numeric(present, errors="coerce").astype("float64")
    return numbers[numbers.abs() < float("inf")]  # NaN and infinity are not values a percentile reads


def _counts(series: pd.Series[Any]) -> pd.Series[Any]:
    present = series.dropna()
    texts = present.map(value_text)
    return texts[texts != ""].value_counts()


def _is_yes_no(series: pd.Series[Any]) -> bool:
    values = set(pd.to_numeric(series.dropna(), errors="coerce").dropna().unique().tolist())
    return bool(values) and values <= {0.0, 1.0}


def learn_contract(
    frame: pd.DataFrame, *, primary_key: str | None = None, target: str | None = None
) -> DataContract:
    """The contract of a prepared file: one entry per column. Counts, shares and listed values only."""
    columns: list[ColumnContract] = []
    rows = len(frame)
    for name in frame.columns:
        column = str(name)
        series = frame[name]
        group = type_group(series)
        role: Literal["id", "outcome", "feature"] = (
            "id" if column == primary_key else "outcome" if column == target else "feature"
        )
        empty = float(series.isna().mean()) if rows else 0.0
        counts = _counts(series)
        band = _band(int(counts.size), int(counts.sum()))
        entry: dict[str, Any] = {
            "column": column,
            "role": role,
            "type_group": group,
            "empty_share": _round(empty),
            "distinct": band,
        }
        if role != "id" and group in ("number", "date"):
            numbers = _numbers(series, group)
            if not numbers.empty:
                entry["p1"] = _round(numbers.quantile(0.01))
                entry["p50"] = _round(numbers.quantile(0.5))
                entry["p99"] = _round(numbers.quantile(0.99))
        if role != "id" and group in ("text", "flag") and band not in ("unique", "many"):
            total = int(counts.sum())
            listed = [(str(value), int(n)) for value, n in counts.items() if int(n) >= MIN_GROUP]
            listed.sort(key=lambda pair: (-pair[1], pair[0]))
            entry["top"] = tuple(
                ValueShare(value=value, share=_round(n / total)) for value, n in listed[:TOP_MAX]
            )
            entry["complete"] = bool(total) and len(listed) <= TOP_MAX
            entry["closed"] = bool(total) and len(listed) == int(counts.size) <= CLOSED_MAX
        if role == "outcome" and _is_yes_no(series):
            entry["outcome_rate"] = _round(pd.to_numeric(series.dropna(), errors="coerce").mean())
        columns.append(ColumnContract(**entry))
    return DataContract(columns=tuple(columns))


# ---------------------------------------------------------------------------
# Checking (every replay; never learning)
# ---------------------------------------------------------------------------
_QUESTION_ID: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9_-]+")


def _question_id(detector: str, slot: str | None, column: str | None) -> str:
    raw = "-".join(part for part in ("drift", detector, slot or "", column or "") if part).lower()
    return (_QUESTION_ID.sub("-", raw).strip("-") or "drift")[:64]


def drift_question(
    detector: Detector,
    text: str,
    *,
    stops: bool,
    slot: str | None = None,
    column: str | None = None,
    examples: Sequence[str] = (),
) -> Question:
    """The one question a finding asks. A stop offers no way on but a person's review: nothing is re-learnt."""
    if stops:
        options = (
            QuestionOption(
                option_id="review",
                label="Review the change and approve the preparation again",
                effect="Opens the file in Data Doctor; nothing is trained or scored until it is approved.",
            ),
            QuestionOption(
                option_id="stop",
                label="Stop here",
                effect="Nothing is trained or scored with this file.",
            ),
        )
    else:
        options = (
            QuestionOption(
                option_id="expected",
                label="The change is expected; go on",
                effect="The approved preparation is used as it is.",
            ),
            QuestionOption(
                option_id="review",
                label="Review it first",
                effect="Opens the file in Data Doctor before it is used.",
            ),
        )
    return Question(
        question_id=_question_id(detector, slot, column),
        text=text,
        options=options,
        blocking=stops,
        examples=tuple(examples[:3]),
    )


def _finding(
    detector: Detector,
    summary: str,
    question: str,
    *,
    stops: bool,
    slot: str | None,
    column: str | None,
    examples: Sequence[str] = (),
) -> DriftFinding:
    return DriftFinding(
        detector=detector,
        stops=stops,
        slot=slot,
        column=column,
        summary=summary,
        question=drift_question(detector, question, stops=stops, slot=slot, column=column, examples=examples),
    )


def _pct(share: float) -> str:
    return f"{share * 100:.1f}%"


_GROUP_WORDS: Final[Mapping[str, str]] = {
    "number": "numbers",
    "date": "dates",
    "flag": "yes/no values",
    "text": "text",
}


def _check_column(
    frozen: ColumnContract, series: pd.Series[Any], *, mode: Literal["train", "score"], slot: str | None
) -> list[DriftFinding]:
    name = frozen.column
    found: list[DriftFinding] = []
    group = type_group(series)
    if group != frozen.type_group:
        found.append(
            _finding(
                "MEANING_CHANGED",
                f"'{name}' held {_GROUP_WORDS[frozen.type_group]} when the preparation was approved and now holds "
                f"{_GROUP_WORDS[group]}.",
                f"'{name}' now holds {_GROUP_WORDS[group]}, not {_GROUP_WORDS[frozen.type_group]} as when the "
                "preparation was approved. Is it still the same column?",
                stops=True,
                slot=slot,
                column=name,
            )
        )
        return found
    if frozen.role == "id":
        return found
    counts = _counts(series)
    total = int(counts.sum())
    listed = {item.value for item in frozen.top}
    unseen = {str(value): int(n) for value, n in counts.items() if str(value) not in listed}
    if frozen.closed and unseen:
        rows = sum(unseen.values())
        names = sorted(unseen, key=lambda v: (-unseen[v], v))
        found.append(
            _finding(
                "NEW_VALUE",
                f"'{name}' holds {len(unseen)} value{'s' if len(unseen) != 1 else ''} the approved preparation never "
                f"saw, in {rows:,} row{'s' if rows != 1 else ''}.",
                f"'{name}' has {len(unseen)} new value{'s' if len(unseen) != 1 else ''} ({rows:,} "
                f"row{'s' if rows != 1 else ''}). What do they mean?",
                stops=True,
                slot=slot,
                column=name,
                examples=names,
            )
        )
    elif not frozen.closed and frozen.complete and total:
        common = {v: n for v, n in unseen.items() if n / total > NEW_CATEGORY_SHARE and n >= MIN_GROUP}
        if common:
            share = sum(common.values()) / total
            names = sorted(common, key=lambda v: (-common[v], v))
            found.append(
                _finding(
                    "NEW_CATEGORY",
                    f"'{name}' has {len(common)} common spelling{'s' if len(common) != 1 else ''} not seen at "
                    f"approval, in {_pct(share)} of its rows.",
                    f"'{name}' has {len(common)} spelling{'s' if len(common) != 1 else ''} not seen when the "
                    "preparation was approved. Is that expected?",
                    stops=False,
                    slot=slot,
                    column=name,
                    examples=names,
                )
            )
    empty = float(series.isna().mean()) if len(series) else 0.0
    if abs(empty - frozen.empty_share) > EMPTY_SHARE_MOVE:
        found.append(
            _finding(
                "VALUE_DRIFT",
                f"'{name}' is empty in {_pct(empty)} of rows; it was {_pct(frozen.empty_share)} at approval.",
                f"'{name}' is empty much more or less often than when the preparation was approved "
                f"({_pct(empty)} against {_pct(frozen.empty_share)}). Is that expected?",
                stops=False,
                slot=slot,
                column=name,
            )
        )
    if frozen.p50 is not None and frozen.p1 is not None and frozen.p99 is not None:
        numbers = _numbers(series, frozen.type_group)
        if not numbers.empty:
            median = float(numbers.quantile(0.5))
            if not frozen.p1 <= median <= frozen.p99:
                found.append(
                    _finding(
                        "VALUE_DRIFT",
                        f"The middle value of '{name}' is outside the range 98% of its values had at approval.",
                        f"The values of '{name}' moved a long way from what was approved. Is the column measured "
                        "the same way?",
                        stops=False,
                        slot=slot,
                        column=name,
                    )
                )
    if mode == "train" and frozen.outcome_rate is not None and _is_yes_no(series):
        rate = float(pd.to_numeric(series.dropna(), errors="coerce").mean())
        moved = abs(rate - frozen.outcome_rate)
        if moved > max(OUTCOME_RATE_FLOOR, OUTCOME_RATE_MOVE * frozen.outcome_rate):
            found.append(
                _finding(
                    "VALUE_DRIFT",
                    f"The share of yes in '{name}' is {_pct(rate)}; it was {_pct(frozen.outcome_rate)} at approval.",
                    f"The share of yes in the outcome '{name}' moved from {_pct(frozen.outcome_rate)} to "
                    f"{_pct(rate)}. Is that expected?",
                    stops=False,
                    slot=slot,
                    column=name,
                )
            )
    return found


def check_contract(
    frame: pd.DataFrame,
    contract: DataContract,
    *,
    mode: Literal["train", "score"],
    slot: str | None = None,
) -> tuple[DriftFinding, ...]:
    """What moved in `frame` against `contract`: stops first, then warnings. At scoring the outcome is not read."""
    names = {str(c): c for c in frame.columns}
    found: list[DriftFinding] = []
    for frozen in contract.columns:
        if mode == "score" and frozen.role == "outcome":
            continue
        if frozen.column not in names:
            found.append(
                _finding(
                    "MISSING_COLUMN",
                    f"'{frozen.column}' is not in this file; the approved preparation reads it.",
                    f"This file has no column '{frozen.column}'. Is it called something else now?",
                    stops=True,
                    slot=slot,
                    column=frozen.column,
                )
            )
            continue
        found.extend(_check_column(frozen, frame[names[frozen.column]], mode=mode, slot=slot))
    known = {c.column for c in contract.columns}
    for name in names:
        if name not in known:
            found.append(
                _finding(
                    "NEW_COLUMN",
                    f"'{name}' is new since the preparation was approved; it is not used.",
                    f"This file has a new column '{name}'. It is left as it is; should Data Doctor look at it?",
                    stops=False,
                    slot=slot,
                    column=name,
                )
            )
    return tuple(sorted(found, key=lambda f: not f.stops))
