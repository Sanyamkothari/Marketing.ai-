"""Outcome-label drafts for Guided setup (Plan K M142, DEC-1409; M93's lapse label).

A file that has no outcome column cannot train a model. When it does have a date a customer's last
activity was recorded on (`last_rchg_dt`, `last_order_date`), a person can often say what the outcome
should be: a *lapse*, "no recharge in the next 30 days, plus a few days' grace" (M93, DEC-1303 (d):
`LabelDefinition.grace_days`). This module drafts that definition for each window in the lexicon
(30, 60 and 90 days by default) and shows it with example customers, so the person can say which one
they mean. It is a question, never a change: nothing here adds a column, a recipe step or a setting.

What it measures is the past. Of the customers in the file, how many had already gone more than
`window + grace` days without the activity when the file was taken, and who they are. The outcome itself
must still be recorded *after* the date the file describes; the draft says so, and the examples show
how long before that date each customer's last activity was.

Pure functions over the frame and the profile: no model, no file is written. An example names a row
("the customer on row 4"), never an ID, because an ID can be a phone number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd

from engine.agent.formats import date_order, parse_dates
from engine.agent.headers import activity_dates, load_lexicon, rank_roles
from engine.contracts import ColumnProfile

__all__ = ["LabelDraft", "LabelDrafts", "draft_labels"]

MIN_DATED_SHARE: Final[float] = 0.5
"""A date column that fewer than this share of rows fill in cannot define lapse for the file."""


@dataclass(frozen=True)
class LabelDraft:
    """One window's draft: its counts and a few customers on each side of it."""

    window_days: int
    grace_days: int
    already_quiet: int
    """Customers whose last activity was more than `window_days + grace_days` before the date their own row
    describes (the row's snapshot date when the file has one, else the newest activity date in the file)."""
    still_active: int
    no_date: int
    after_reference: int
    """Customers whose last activity is dated after the date the file describes: not quiet, not counted active."""
    quiet_examples: tuple[str, ...]
    active_examples: tuple[str, ...]


@dataclass(frozen=True)
class LabelDrafts:
    activity_column: str
    activity: str
    """The activity in one word (`recharge`, `order`)."""
    reference_column: str | None
    reference_date: str
    """ISO date the sentence names: the newest snapshot date in the file, else its newest activity date. The
    days of each row are counted back from that row's own snapshot date when there is one."""
    customers: int
    drafts: tuple[LabelDraft, ...]


def _dates(series: pd.Series[Any]) -> pd.Series[Any] | None:
    """The column as dates, or None when it cannot be read without guessing the day/month order."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    cells = series.dropna().astype(str)
    cells = cells[cells.str.strip() != ""]
    if cells.empty:
        return None
    order = date_order(cells)
    if order is None and cells.str.match(r"^\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}").any():
        return None  # 03/04/2026: March or April; the format question settles it first
    return parse_dates(series, dayfirst=bool(order)).values


def draft_labels(
    frame: pd.DataFrame,
    columns: Sequence[ColumnProfile],
    *,
    root: Path | None = None,
) -> LabelDrafts | None:
    """The drafts for this file, or None when it has no date of a customer's activity to build one on."""
    lexicon = load_lexicon(root)
    rules = lexicon.label_drafts
    found = activity_dates(columns, root=root)
    if not found:
        return None
    for name, activity in found:
        dates = _dates(frame[name])
        if dates is None or dates.notna().mean() < MIN_DATED_SHARE:
            continue
        reference_column: str | None = None
        reference = dates.max()
        snapshots: pd.Series[Any] | None = None
        ranked = [c for c in rank_roles(columns, root=root)["date"] if c.column != name and c.score >= 0.5]
        for candidate in ranked:
            stamp = _dates(frame[candidate.column])
            if stamp is not None and stamp.notna().any():
                reference_column, reference, snapshots = candidate.column, stamp.max(), stamp
                break
        if pd.isna(reference):
            continue
        # Row by row: each customer is measured from the date their own row describes, because a file of
        # several weekly snapshots has a different "today" on each row. With no snapshot column the newest
        # activity date stands in for everyone's.
        activity_on = dates.reset_index(drop=True)
        if snapshots is not None:
            age = (snapshots.reset_index(drop=True) - activity_on).dt.days
        else:
            age = (reference - activity_on).dt.days
        drafts: list[LabelDraft] = []
        for window in rules.windows_days:
            limit = window + rules.grace_days
            quiet = age > limit
            active = age.between(0, limit)
            quiet_at = [int(i) for i in np.flatnonzero(quiet.to_numpy())[: rules.examples]]
            active_at = [int(i) for i in np.flatnonzero(active.to_numpy())[: rules.examples]]

            def line(position: int, activity_name: str = activity, ages: pd.Series[Any] = age) -> str:
                # Row numbers, never an ID: an ID can be a phone number, and the person finds the row anyway.
                days = int(ages.iloc[position])
                return (
                    f"the customer on row {position + 2} had their last {activity_name} {days} days "
                    "before the date the file describes"
                )  # row 1 is the header

            drafts.append(
                LabelDraft(
                    window_days=window,
                    grace_days=rules.grace_days,
                    already_quiet=int(quiet.sum()),
                    still_active=int(active.sum()),
                    no_date=int(age.isna().sum()),
                    after_reference=int((age < 0).sum()),
                    quiet_examples=tuple(line(i) for i in quiet_at),
                    active_examples=tuple(line(i) for i in active_at),
                )
            )
        return LabelDrafts(
            activity_column=name,
            activity=activity,
            reference_column=reference_column,
            reference_date=pd.Timestamp(reference).date().isoformat(),
            customers=len(frame),
            drafts=tuple(drafts),
        )
    return None
