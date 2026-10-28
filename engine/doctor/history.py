"""Is the dated history long enough for the goal? (`check_history`, the plan's warnings before approval).

Two plain rules, both measured by our code on the dates of the file and never guessed by the AI:

* **Room for both windows.** A model learns from behaviour in a look-back window before the training snapshot and from
  the outcome in a full outcome window after it. The history must hold both: `lookback + outcome window` days, or,
  when the training snapshot is fixed (`decisions.training_as_of_date`), that many days before it and the outcome
  window after it. The look-back is the plan's own (`lookback_days`, at least `MIN_LOOKBACK_DAYS`) when it names
  one, else as long as the outcome window, so what is learnt from is as long as what is predicted.
  `HISTORY_TOO_SHORT`.
* **One season only.** When the history is under a year and a few months hold most of the rows (more than half in
  three months; a file of three months or less is one season by definition), the model learns one season. `HISTORY_ONE_SEASON`.

This extends the date and maturity checks that exist (`OUTCOME_WINDOW_PAST_DATA` for one query step,
`X_LABEL_MATURITY` for the newest rows); it does not repeat them. Both are warnings (flags): the person reads them
before approving, and the hard product rules (rows, positives, leakage, consent) are untouched. Counts and months only,
never a cell value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

import pandas as pd

from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.doctor.visit_contracts import PlanFinding

__all__ = [
    "HISTORY_CODES",
    "MIN_LOOKBACK_DAYS",
    "HistoryFinding",
    "HistoryReading",
    "history_plan_findings",
    "plan_findings_of",
    "read_history",
]

HISTORY_CODES: Final[tuple[str, ...]] = ("HISTORY_TOO_SHORT", "HISTORY_ONE_SEASON")
MIN_LOOKBACK_DAYS: Final[int] = 30
"""The shortest look-back the features get, whatever the outcome window."""
FULL_YEAR_DAYS: Final[int] = 365
SEASON_MONTHS: Final[int] = 3
SEASON_SHARE: Final[float] = 0.5
"""More than this share of the rows in `SEASON_MONTHS` months is a concentration ..."""
_READABLE: Final[float] = 0.5
"""Dates are read only when at least this share of the cells parse (a column that is mostly text is not a date)."""
_DAYS_PER_MONTH: Final[float] = 30.4


@dataclass(frozen=True)
class HistoryFinding:
    code: str
    text: str
    detail: str
    count: int | None = None


@dataclass(frozen=True)
class HistoryReading:
    """What the dates say, and what is wrong with them for this goal (empty `findings`: nothing to warn about)."""

    start: date
    end: date
    history_days: int
    months: int
    needed_days: int | None
    findings: tuple[HistoryFinding, ...]
    top_months_share: float | None = None


def _months(days: float) -> str:
    n = max(1, round(days / _DAYS_PER_MONTH))
    return f"{n} month{'' if n == 1 else 's'}"


def _dates(series: pd.Series) -> pd.Series:
    """The cells that read as dates. Numbers and yes/no never do (they would parse as 1970 epoch dates)."""
    column = series
    if pd.api.types.is_bool_dtype(column) or pd.api.types.is_numeric_dtype(column):
        return column.iloc[0:0]
    if not pd.api.types.is_datetime64_any_dtype(column):
        column = pd.to_datetime(column, errors="coerce", format="mixed")
    return column.dropna()


def _season(dates: pd.Series, start: date, end: date) -> HistoryFinding | None:
    """A few months holding most of the rows, in a history under a year."""
    periods = dates.dt.to_period("M")
    covered = (end.year - start.year) * 12 + end.month - start.month + 1
    counts = periods.value_counts()
    total = int(counts.sum())
    top = int(counts.nlargest(SEASON_MONTHS).sum())
    share = top / total if total else 0.0
    if share <= SEASON_SHARE:
        return None
    where = (
        f"The file covers only {covered} month{'' if covered == 1 else 's'}"
        if covered <= SEASON_MONTHS
        else f"{share:.0%} of the rows fall in {SEASON_MONTHS} of the {covered} months in the file, and the file "
        "covers less than a year"
    )
    return HistoryFinding(
        code="HISTORY_ONE_SEASON",
        text=(
            f"{where}. The model would learn one season only, so its results may look better than "
            "they will in a normal month. Send at least 12 months of history."
        ),
        detail=f"Rows per calendar month: the {SEASON_MONTHS} busiest of {covered} months hold {share:.1%}.",
        count=round(share * 100),
    )


def read_history(
    values: pd.Series,
    *,
    outcome_window_days: int | None,
    training_as_of: date | None = None,
    lookback_days: int | None = None,
) -> HistoryReading | None:
    """The history in `values` (a date column) against the goal; None when too few cells read as dates to say.

    `training_as_of` fixes the snapshot: then the history must hold the look-back before it and the outcome window
    after it. Without it, the whole span must hold both. With no `outcome_window_days` only the season rule runs.
    """
    if len(values) == 0:
        return None
    dates = _dates(values)
    if dates.empty or len(dates) < _READABLE * len(values):
        return None
    start, end = dates.min().date(), dates.max().date()
    days = (end - start).days
    found: list[HistoryFinding] = []
    needed: int | None = None
    if outcome_window_days is not None and outcome_window_days > 0:
        window = outcome_window_days
        # The plan's own look-back when it names one (never under the floor), else as long as the outcome window.
        back = max(lookback_days, MIN_LOOKBACK_DAYS) if lookback_days else max(window, MIN_LOOKBACK_DAYS)
        needed = back + window
        before = None if training_as_of is None else (training_as_of - start).days
        after = None if training_as_of is None else (end - training_as_of).days
        if training_as_of is None:
            short = needed - days
            enough = short <= 0
        else:
            assert before is not None and after is not None
            short = max(back - before, 0) + max(window - after, 0)
            enough = short == 0
        if not enough:
            found.append(
                HistoryFinding(
                    code="HISTORY_TOO_SHORT",
                    text=(
                        f"The dates cover about {_months(days)}, but a {window}-day outcome needs {back} days of "
                        f"history before the training date and {window} days after it (about {_months(needed)}). "
                        "The model would learn from too little. Send more history, or choose a shorter outcome "
                        "window."
                        if training_as_of is None
                        else f"The training date leaves {max(before or 0, 0)} days of history before it and "
                        f"{max(after or 0, 0)} days after it, but a {window}-day outcome needs {back} days before "
                        f"and {window} days after. The model would learn from too little. Move the training date, "
                        "send more history, or choose a shorter outcome window."
                    ),
                    detail=f"History {days} days; needs {back} (look-back) + {window} (outcome window) = {needed}.",
                    count=short,
                )
            )
    top: float | None = None
    if days < FULL_YEAR_DAYS:
        seasonal = _season(dates, start, end)
        if seasonal is not None:
            found.append(seasonal)
            top = (seasonal.count or 0) / 100
    covered = (end.year - start.year) * 12 + end.month - start.month + 1
    return HistoryReading(
        start=start,
        end=end,
        history_days=days,
        months=covered,
        needed_days=needed,
        findings=tuple(found),
        top_months_share=top,
    )


def history_plan_findings(
    values: pd.Series,
    *,
    outcome_window_days: int | None,
    training_as_of: date | None,
    column: str | None = None,
) -> tuple[PlanFinding, ...]:
    """The reading as the plan's warnings (flags: shown before approval, nothing to say yes to)."""
    return plan_findings_of(
        read_history(values, outcome_window_days=outcome_window_days, training_as_of=training_as_of), column
    )


def plan_findings_of(reading: HistoryReading | None, column: str | None = None) -> tuple[PlanFinding, ...]:
    """A reading's findings as the plan's warnings."""
    if reading is None:
        return ()
    return tuple(
        PlanFinding(
            key=sha256_hex(canonical_bytes([f.code, "plan"]))[:16],
            level="flag",
            code=f.code,
            text=f.text,
            detail=f.detail,
            count=f.count,
            column=column,
            scope="full",
        )
        for f in reading.findings
    )
