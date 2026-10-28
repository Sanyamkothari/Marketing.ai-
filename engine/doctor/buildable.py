"""Can a goal's outcome be built from the events in the files? (the goal fit judges what can be built)

A raw order log has no `reactivated_90d` column, so a fit that looks only for the outcome's name says "nothing fits"
for every goal, and the person is stuck: the column the goal asks for is exactly what Data Doctor can build. When a
goal allows `reshape` (`agent.levels`: many rows per customer combined into one) and its outcome is defined by a window after a snapshot
(`configs/outcomes.yaml`: `event_presence` or `event_absence`, with its windows), an event table - an ID column
and a date column, with an amount when there is one - is enough to build one row per customer with the outcome.

This module only counts. The facts are measured on the rows by code (distinct IDs, the date span, a candidate
snapshot, the customers active on or before it, and how many of them are a 'yes' in the window after it); the
build is proposed afterwards (`engine.doctor.visit`, through `propose_features` and the point-in-time query) and the
person approves it. The snapshot is the latest month-end whose window is over, a first date to discuss, not a
decision. The floors (`validation.min_rows`, `validation.min_positive`) are checked on those counts, so the verdict
is `too_small` rather than `buildable` when the table the goal would train on could not reach them.
"""

from __future__ import annotations

import calendar
import threading
import weakref
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import pandas as pd
from pydantic import Field

from engine.agent.config import AgentLevel
from engine.config import StrictBase

if TYPE_CHECKING:
    from engine.config import UseCaseConfig
    from engine.doctor.diagnose import LoadedFile

__all__ = ["BuildableOutcome", "buildable_outcome", "compile_build", "snapshot_for"]

_SAMPLE_ROWS: Final[int] = 50_000
"""Rows the columns are told apart on (an ID, a date, an amount); the counts use every row read."""
_YES: Final[tuple[object, ...]] = (1, True, "1", None)
"""The positive labels the built outcome (1 for a yes, 0 for a no) is: another word for yes is the person's to define."""
_MIN_SHARE: Final[float] = 0.8
"""Share of a column's filled cells that must read as dates (or numbers) for it to be one."""


class BuildableOutcome(StrictBase):
    """The outcome a file of events can define, with the numbers the person and the AI decide on."""

    upload_id: str
    file_name: str
    entity_key: str = Field(description="The column whose values repeat: the customer of each row.")
    time_column: str = Field(description="The column read as the date of each row.")
    amount_column: str | None = Field(
        default=None, description="A hint: what a row is worth (or a unit price)."
    )
    quantity_column: str | None = Field(default=None, description="A hint: units per row, with a unit price.")
    order_column: str | None = Field(
        default=None, description="A hint: the order number when a row is a line."
    )
    label_type: Literal["event_absence", "event_presence"]
    window_days: int
    snapshot: date | None = Field(
        description="The latest month-end whose window is over; null when the history is shorter than the window."
    )
    data_start: date
    data_end: date
    ids: int = Field(description="Distinct customers in the rows read.")
    active_before: int = Field(description="Customers with a row on or before the snapshot: the rows built.")
    yes: int = Field(description="Of them, how many are a 'yes' in the window after the snapshot.")
    rows_read: int
    file_rows: int
    enough: bool = Field(description="The rows built would reach the configured floors.")
    sentence: str = Field(description="One plain sentence: what can be built and the counts.")


def _levels_allow(config: UseCaseConfig) -> bool:
    """One row per customer from many event rows is a reshape (`agent.levels`); a goal that may only clean or derive
    columns from a row is not offered a build."""
    return AgentLevel.RESHAPE in config.agent.levels


def snapshot_for(start: date, end: date, window_days: int) -> date | None:
    """The latest month-end whose `window_days` days are over by `end` (or, when the history has none that is after
    `start`, the latest day whose window is over); None when the history is not longer than the window."""
    latest = end - timedelta(days=window_days)
    if latest <= start:
        return None
    month_end = date(latest.year, latest.month, calendar.monthrange(latest.year, latest.month)[1])
    if month_end > latest:
        month_end = date(latest.year, latest.month, 1) - timedelta(days=1)
    return month_end if month_end > start else latest


def _columns(frame: pd.DataFrame) -> tuple[str, str, str | None, str | None, str | None] | None:
    """(customer, date, amount, quantity, order) columns read on the first rows, or None when the frame is not an
    event table: the customer is read by name first, then by repeating values (`order_file_facts`)."""
    from engine.doctor.tools_facts import order_file_facts

    sample = frame.head(_SAMPLE_ROWS)
    facts = order_file_facts("sample", sample)
    # A column already read as dates (the plan parsed it) is a date, not a number: its dtype says so.
    typed = {str(c) for c in sample.columns if pd.api.types.is_datetime64_any_dtype(sample[c])}
    # The customer is the ID-like or text column whose values repeat most (an order number repeats less than the
    # customer who placed the orders); a number column is one only when its name says it is an ID.
    keys = sorted(
        (
            c
            for c in facts.columns
            if c.rows_per_value > 1
            and c.column not in typed
            and c.dates_share < _MIN_SHARE
            and (c.hint == "id" or c.numbers_share < _MIN_SHARE)
        ),
        key=lambda c: (c.hint != "id", -c.rows_per_value),
    )
    dates = [c for c in facts.columns if c.column in typed or c.dates_share >= _MIN_SHARE]
    if not keys or not dates:
        return None
    key = keys[0].column
    when = next((c.column for c in dates if c.hint == "date"), dates[0].column)
    numbers = [
        c
        for c in facts.columns
        if c.numbers_share >= _MIN_SHARE and c.column not in {key, when, *typed} and c.dates_share < 1
    ]
    quantity = next((c.column for c in numbers if c.hint == "quantity"), None)
    amount = next((c.column for c in numbers if c.hint == "amount"), None)
    order = next(
        (c.column for c in facts.columns if c.hint == "id" and c.column != key and c.rows_per_value > 1), None
    )  # another ID that repeats: the order of a line
    return key, when, amount, quantity, order


@dataclass(frozen=True)
class _Events:
    """A frame read as events: its columns, and the customer and the day of every row that has both."""

    key: str
    when: str
    amount: str | None
    quantity: str | None
    order: str | None
    ids: pd.Series[str]
    days: pd.Series[pd.Timestamp]


_CACHE: OrderedDict[int, tuple[weakref.ref[pd.DataFrame], _Events | None]] = OrderedDict()
_CACHE_SIZE: Final[int] = 4
_LOCK = threading.Lock()


def _events(frame: pd.DataFrame) -> _Events | None:
    """`frame` as events, or None. Read once per frame: every goal asks, and reading the dates is the slow part.
    An entry keeps a weak reference to its frame (a negative one too) and is used only while that reference still
    reaches this very frame: an id that a freed frame left behind and a new frame took is never mistaken for it, and
    the cache never keeps a frame alive."""
    with _LOCK:
        entry = _CACHE.get(id(frame))
        if entry is not None:
            if entry[0]() is frame:
                _CACHE.move_to_end(id(frame))
                return entry[1]
            del _CACHE[id(frame)]
    found = _columns(frame)
    events: _Events | None = None
    if found is not None:
        key, when, amount, quantity, order = found
        ids = frame[key].astype("string").str.strip()
        ids = ids.mask(ids == "")
        how = "mixed" if frame[when].dtype == object else None
        days = pd.to_datetime(frame[when], errors="coerce", format=how)
        keep = (ids.notna() & days.notna()).to_numpy()
        if keep.any():
            events = _Events(key, when, amount, quantity, order, ids[keep], days[keep].dt.normalize())
    with _LOCK:
        for stale in [k for k, (ref, _e) in _CACHE.items() if ref() is None]:
            del _CACHE[stale]
        _CACHE[id(frame)] = (weakref.ref(frame), events)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return events


def _measure(
    events: _Events, window_days: int, label_type: str
) -> tuple[date, date, int, date | None, int, int]:
    """(first date, last date, distinct customers, snapshot, customers active before it, 'yes' among them)."""
    ids, days = events.ids, events.days
    start, end = days.min().date(), days.max().date()
    snapshot = snapshot_for(start, end, window_days)
    distinct = int(ids.nunique())
    if snapshot is None:
        return start, end, distinct, None, 0, 0
    cut = pd.Timestamp(snapshot)
    known = set(ids[days <= cut].unique())
    later = set(ids[(days > cut) & (days <= cut + pd.Timedelta(days=window_days))].unique())
    came = len(known & later)
    yes = came if label_type == "event_presence" else len(known) - came
    return start, end, distinct, snapshot, len(known), yes


def buildable_outcome(
    files: Sequence[LoadedFile], config: UseCaseConfig, root: Path | None
) -> BuildableOutcome | None:
    """The outcome of `config` that an event file among `files` can define, counted; None when the goal does not allow
    building it, has no event outcome in `configs/outcomes.yaml`, or no file is an event table (an ID that repeats and
    a date)."""
    from engine.config import config_root
    from engine.doctor.outcomes import load_outcome_catalogue

    if config.target.column is None or not _levels_allow(config) or config.target.positive_label not in _YES:
        return None
    template = load_outcome_catalogue(config_root(root)).templates.get(config.id)
    if template is None:
        return None
    noun = config.entity or "customer"
    days = template.default_days
    for loaded in files:
        frame = loaded.frame
        if frame.empty:
            continue
        events = _events(frame)
        if events is None:
            continue
        key, when, amount, quantity, order = (
            events.key,
            events.when,
            events.amount,
            events.quantity,
            events.order,
        )
        start, end, ids, snapshot, active, yes = _measure(events, days, template.label_type)
        rows = loaded.file_rows or len(frame)
        scale = rows / max(len(frame), 1)
        # A sample undercounts the whole file: a shortfall counts only when it holds scaled up as well.
        enough = (
            snapshot is not None
            and round(active * scale) >= config.validation.min_rows
            and round(yes * scale) >= config.validation.min_positive
        )
        span = f"{start:%d %b %Y} to {end:%d %b %Y}"
        if snapshot is None:
            sentence = (
                f"{loaded.file_name} has a row per event ({span}), but that is not longer than the {days} days "
                f"the outcome looks ahead, so no {noun} could be followed."
            )
        else:
            sentence = (
                f"Fits once I build one row per {noun} with the outcome: a snapshot (for example "
                f"{snapshot:%d %b %Y}, the latest month-end whose {days} days are over), the {days}-day window "
                f"after it, and the {noun}s active before it. {loaded.file_name} ({span}) would give "
                f"{active:,} {noun}s, {yes:,} of them a yes."
            )
            if scale > 1:
                sentence += f" Counted on {len(frame):,} of {rows:,} rows."
        return BuildableOutcome(
            upload_id=loaded.upload_id,
            file_name=loaded.file_name,
            entity_key=key,
            time_column=when,
            amount_column=amount,
            quantity_column=quantity,
            order_column=order,
            label_type=template.label_type,
            window_days=days,
            snapshot=snapshot,
            data_start=start,
            data_end=end,
            ids=ids,
            active_before=active,
            yes=yes,
            rows_read=len(frame),
            file_rows=rows,
            enough=enough,
            sentence=sentence,
        )
    return None


def compile_build(built: BuildableOutcome, config: UseCaseConfig, table: str) -> tuple[str, str]:
    """(the query, its description) that builds one row per customer known at `$as_of` over the table whose SQL name is
    `table`: the behaviour features measured at that date (`propose_features`'s own compiler, so none reads a row
    after it) and the outcome, read from the `window_days` days after it (the only part that reads the future). The
    outcome is 1 for a yes and 0 for a no, named as the goal's target column."""
    from engine.doctor.tools_features import FeatureArg, ProposeFeaturesArgs, _col, compile_features

    outcome = config.target.column
    assert outcome is not None  # buildable_outcome returns nothing for a goal without a target column
    key, when = built.entity_key, built.time_column
    pricey = built.amount_column is not None and any(
        w in built.amount_column.casefold() for w in ("price", "rate", "cost")
    )
    if built.quantity_column and built.amount_column and pricey:
        quantity, price = built.quantity_column, built.amount_column
        given = ProposeFeaturesArgs(
            upload_id=built.upload_id,
            entity_key=key,
            time_column=when,
            preset="rfm",
            quantity_column=quantity,
            price_column=price,
            order_column=built.order_column,
            output_name="training_table",
        )
    elif built.amount_column:
        given = ProposeFeaturesArgs(
            upload_id=built.upload_id,
            entity_key=key,
            time_column=when,
            preset="rfm",
            amount_column=built.amount_column,
            order_column=built.order_column,
            output_name="training_table",
        )
    else:
        given = ProposeFeaturesArgs(
            upload_id=built.upload_id,
            entity_key=key,
            time_column=when,
            output_name="training_table",
            features=(
                FeatureArg(name="days_since_last_event", agg="days_since_last"),
                FeatureArg(name="events_90d", agg="count", window_days=90),
                FeatureArg(name="events_365d", agg="count", window_days=365),
                FeatureArg(name="events_total", agg="count"),
            ),
        )
    features = compile_features(given, table, outcome=outcome)
    days = built.window_days
    id_key, id_when, label = _col(key), _col(when), _col(outcome)
    yes, no = (1, 0) if built.label_type == "event_presence" else (0, 1)
    sql = (
        f"WITH f AS ({features.sql}), "
        f"w AS (SELECT DISTINCT CAST({id_key} AS VARCHAR) AS entity_key FROM {table} "
        f"WHERE NULLIF(TRIM(CAST({id_key} AS VARCHAR)), '') IS NOT NULL "
        f"AND TRY_CAST({id_when} AS DATE) > CAST($as_of AS DATE) "
        f"AND TRY_CAST({id_when} AS DATE) <= CAST($as_of AS DATE) + INTERVAL {days} DAY) "
        f"SELECT f.*, CASE WHEN w.entity_key IS NULL THEN {no} ELSE {yes} END AS {label} "
        f'FROM "f" LEFT JOIN "w" ON CAST(f.{id_key} AS VARCHAR) = w.entity_key ORDER BY f.{id_key}'
    )
    noun = config.entity or "customer"
    word = "came back" if built.label_type == "event_presence" else "had no row"
    description = (
        f"Training table: one row per {noun} known at the snapshot, with behaviour measured on or before it "
        f"({', '.join(f.name for f in features.features)}), and {outcome}: 1 when the {noun} {word} in the "
        f"{days} days after the snapshot, else 0."
    )
    return sql, description[:300]
