"""Snapshot dates (Phase 2 plan section 6.4): which `(entity, date)` rows a dataset is built for.

A snapshot date is the instant the model is asked to stand at and predict from. Everything else in
this package is bounded by it - a feature may read events up to it, a label only events after it -
so the dates chosen here decide how much of the client's file each row is allowed to see, and a
careless choice is invisible in every metric.

Two of those careless choices are what this module exists to prevent.

**A tail of snapshots with no outcome yet.** The last snapshot of a periodic plan defaults to
`last_event - horizon_days`, not `last_event`. Build to the end of the file instead and every
snapshot in the final `horizon_days` has an outcome window that has not finished: for an
`event_absence` label those rows read as 100% churn, and `engine.onboarding.labels` would drop them
as censored, leaving a user staring at a report where half the dates they asked for vanished with
no way to tell an intended drop from a bug.

**A row for an entity that did not exist yet.** A customer three months before they signed up is
all-null on every event feature, which is exactly the shape of a customer about to leave; train on
both and the model learns the shape of a missing row. `build_snapshot_frame` therefore filters on
the mapped signup date whenever the client has one.

Both rules are about the calendar only. Nothing here branches on a use-case id or a client id, and
nothing here invents a date: every boundary is either the user's pick or a measured `first_event` /
`last_event`, and a pick the data cannot support comes back as a check rather than a silent clamp.
"""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING

from engine.contracts import Severity
from engine.onboarding.specs import OnboardingCheck, SnapshotFrequency, SnapshotMode, SnapshotSpec
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd

__all__ = [
    "SnapshotPlan",
    "build_snapshot_frame",
    "plan_snapshots",
    "scoring_snapshot",
]

logger = get_logger(__name__)


@dataclass(frozen=True)
class SnapshotPlan:
    """The dates to build rows for, and what to tell the user about them.

    The checks are returned, never raised: a date the user picked outside their own data is
    something they correct on the review screen, and the build stage is the one place that decides
    which severities stop a build.
    """

    dates: tuple[datetime.date, ...]
    checks: tuple[OnboardingCheck, ...]


def plan_snapshots(
    spec: SnapshotSpec,
    *,
    first_event: datetime.date,
    last_event: datetime.date,
    horizon_days: int,
) -> SnapshotPlan:
    """The training snapshot dates, given how far the client's data actually runs.

    `first_event` and `last_event` are measured over the mapped event tables; `horizon_days` is the
    label's outcome window, 0 when the dataset is unlabelled. The two defaults - the first snapshot
    a history's worth after the data starts, the last one a horizon's worth before it ends - are
    what leave room for the features to look back and the label to look forward, so a plan built
    with them cannot produce a row that is short of either.
    """
    checks: list[OnboardingCheck | None] = [
        _too_little_history(spec, first_event=first_event, last_event=last_event, horizon_days=horizon_days)
    ]
    if spec.mode is SnapshotMode.SINGLE:
        day = _requested(spec) or last_event
        checks.append(_outside_range(day, first_event=first_event, last_event=last_event))
        return SnapshotPlan(dates=(day,), checks=tuple(c for c in checks if c is not None))

    checks.extend(
        _outside_range(day, first_event=first_event, last_event=last_event)
        for day in (spec.start, spec.end)
        if day is not None
    )
    start = spec.start or first_event + datetime.timedelta(days=spec.min_history_days)
    end = spec.end or last_event - datetime.timedelta(days=horizon_days)
    dates = _month_ends(start, end) if spec.frequency is SnapshotFrequency.MONTHLY else _week_ends(start, end)
    return SnapshotPlan(dates=dates[-spec.max_snapshots :], checks=tuple(c for c in checks if c is not None))


def scoring_snapshot(spec: SnapshotSpec, *, last_event: datetime.date) -> tuple[datetime.date, ...]:
    """The one date a scoring build stands at: the end of the new data, or the user's pick.

    The spec is the scoring one, which `OnboardingSpec.scoring_snapshot_spec` derives from the
    training spec with the range cleared rather than letting a client configure a second rule that
    could drift away from the first; a date survives it only when the user picked one for this
    scoring run. Mode, frequency and the history minimum are ignored: there is no label to censor
    and no room to leave, only "score everyone as they stand at the end of the file". A tuple, so
    the build stage handles a training plan and a scoring plan with one code path.
    """
    return (_requested(spec) or last_event,)


def build_snapshot_frame(
    entities: pd.DataFrame,
    dates: Sequence[datetime.date],
    *,
    signup_column: str | None,
) -> pd.DataFrame:
    """The spine of the build: one row per entity per snapshot date, `(entity_key, snapshot_date)`.

    Every feature and label query is a left join onto this frame, so which rows exist here is which
    rows exist in the dataset. `signup_column` is the mapped signup date when the client has one;
    a row is kept only where the entity had already signed up, and a signup date that is null or
    unparseable is left out rather than assumed, because an unknown date cannot show the entity
    existed and the whole point of the filter is not to invent history.

    `snapshot_date` is a timestamp at midnight, so DuckDB compares it against `event_time` without
    an implicit cast: the snapshot is the instant the day begins, which is what
    `SnapshotDefinition.inclusive_snapshot_time` then puts on one side of the boundary or the other.
    """
    import pandas as pd

    if signup_column is None:
        logger.warning("no signup column mapped; snapshot rows may pre-date the entity's signup")
        keep = ["entity_key"]
    else:
        keep = ["entity_key", signup_column]
    spine = pd.DataFrame({"snapshot_date": pd.to_datetime(list(dates))})
    frame = entities.loc[:, keep].merge(spine, how="cross")
    if signup_column is not None:
        signup = pd.to_datetime(frame[signup_column], errors="coerce")
        frame = frame.loc[signup <= frame["snapshot_date"], ["entity_key", "snapshot_date"]]
    return frame.reset_index(drop=True)


def _requested(spec: SnapshotSpec) -> datetime.date | None:
    """The date the user picked for a single snapshot; `end` wins, a single snapshot being the one
    the dataset ends at."""
    return spec.end or spec.start


def _month_ends(start: datetime.date, end: datetime.date) -> tuple[datetime.date, ...]:
    """Calendar month-ends within `[start, end]`, earliest first.

    The last day of each month is looked up rather than reached by adding days, so February is 28 or
    29 as the year requires and a 30-day month never yields a 31st. Walking back from `end` means a
    snapshot is never placed past the end of the usable window.
    """
    dates: list[datetime.date] = []
    year, month = end.year, end.month
    while True:
        day = datetime.date(year, month, calendar.monthrange(year, month)[1])
        if day < start:
            break
        if day <= end:
            dates.append(day)
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return tuple(reversed(dates))


def _week_ends(start: datetime.date, end: datetime.date) -> tuple[datetime.date, ...]:
    """Every seventh day back from `end`, earliest first.

    Anchoring on the end rather than the start keeps the most recent snapshot on the date the caller
    asked for when the span is not a whole number of weeks, and keeps every snapshot on the same
    weekday - a week's activity is not comparable between a Monday cut and a Saturday one.
    """
    dates: list[datetime.date] = []
    day = end
    while day >= start:
        dates.append(day)
        day -= datetime.timedelta(days=7)
    return tuple(reversed(dates))


def _outside_range(
    day: datetime.date, *, first_event: datetime.date, last_event: datetime.date
) -> OnboardingCheck | None:
    if first_event <= day <= last_event:
        return None
    return OnboardingCheck(
        code="SNAPSHOT_OUTSIDE_DATA_RANGE",
        severity=Severity.ERROR,
        message=(
            f"The snapshot date {day} is outside the data, which runs from {first_event} to "
            f"{last_event}, so no row can be built for it."
        ),
        suggestion=(f"Pick a date between {first_event} and {last_event}, or upload data that covers {day}."),
        details={
            "snapshot_date": str(day),
            "first_event": str(first_event),
            "last_event": str(last_event),
        },
    )


def _too_little_history(
    spec: SnapshotSpec,
    *,
    first_event: datetime.date,
    last_event: datetime.date,
    horizon_days: int,
) -> OnboardingCheck | None:
    required = spec.min_history_days + horizon_days
    span = (last_event - first_event).days
    if span >= required:
        return None
    shorten = f", or shorten the outcome window of {horizon_days} days" if horizon_days else ""
    return OnboardingCheck(
        code="TOO_LITTLE_HISTORY",
        severity=Severity.ERROR,
        message=(
            f"The data covers {span:,} days, from {first_event} to {last_event}. "
            f"This needs at least {required} days of history."
        ),
        suggestion=(
            f"Upload data going back to {last_event - datetime.timedelta(days=required)} - "
            f"{required - span:,} days more than you have{shorten}."
        ),
        details={
            "span_days": span,
            "required_days": required,
            "min_history_days": spec.min_history_days,
            "horizon_days": horizon_days,
        },
    )
