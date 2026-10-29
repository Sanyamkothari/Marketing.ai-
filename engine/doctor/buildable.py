"""Can a goal's outcome be built from the events in the files? (the goal fit judges what can be built)

A raw order log has no `reactivated_90d` column, so a fit that looks only for the outcome's name says "nothing fits"
for every goal, and the person is stuck: the column the goal asks for is exactly what Data Doctor can build. When a
goal allows `reshape` (`agent.levels`: many rows per customer combined into one) and its outcome is defined by a window after a snapshot
(`configs/outcomes.yaml`: `event_presence` or `event_absence`, with its windows), an event table - an ID column
and a date column, with an amount when there is one - is enough to build one row per customer with the outcome.

This module only counts. The facts are measured on every row of the file by code (distinct IDs, the date span, a
candidate snapshot, the customers active on or before it, and how many of them are a 'yes' in the window after it):
distinct customers do not grow with the rows, so a count made on a sample is never scaled up. When the diagnosis
holds a sample, the ID and date columns alone are read over the whole file (`LoadedFile.column_chunks`). When the
goal's outcome names a narrower audience (`configs/outcomes.yaml`: `audience`, such as the shoppers who have gone
quiet for 90 days), both readings are counted and the person picks one; the code never narrows it on its own. The
build is proposed afterwards (`engine.doctor.visit`, through `propose_features` and the point-in-time query) and the
person approves it. The window and the audience are parameters (`buildable_outcome(window_days=, audience_id=)`),
read from the goal's settings (`engine.doctor.goal_settings`). The snapshot is the latest month-end whose window is
over, a first date to discuss, not a decision. The floors (`validation.min_rows`, `validation.min_positive`) are checked on those counts, so the verdict
is `too_small` rather than `buildable` when the table the goal would train on could not reach them.
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import pandas as pd
from pydantic import Field

from engine.agent.config import AgentLevel
from engine.agent.formats import to_datetime_mixed
from engine.config import StrictBase
from engine.content_cache import ContentCache, content_digest
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from engine.config import UseCaseConfig
    from engine.doctor.diagnose import LoadedFile

__all__ = ["BuildAudience", "BuildableOutcome", "buildable_outcome", "compile_build", "snapshot_for"]

_LOG = get_logger(__name__)

_SAMPLE_ROWS: Final[int] = 50_000
"""Rows the columns are told apart on (an ID, a date, an amount); the counts use every row of the file."""
_YES: Final[tuple[object, ...]] = (1, True, "1", None)
"""The positive labels the built outcome (1 for a yes, 0 for a no) is: another word for yes is the person's to define."""
_MIN_SHARE: Final[float] = 0.8
"""Share of a column's filled cells that must read as dates (or numbers) for it to be one."""


class BuildAudience(StrictBase):
    """One reading of who the built table is about, with its counts: every customer active on or before the snapshot,
    or the narrower audience the goal's configuration names (customers quiet for `quiet_days` before it)."""

    audience_id: Literal["active", "quiet"]
    quiet_days: int | None = Field(
        default=None, description="For `quiet`: days before the snapshot with no row."
    )
    label: str = Field(description="Who they are, in plain words.")
    customers: int = Field(description="Customers in this reading: the rows built.")
    yes: int = Field(description="Of them, how many are a 'yes' in the window after the snapshot.")
    enough: bool = Field(description="These rows would reach the configured floors.")


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
    outcome_column: str | None = Field(
        default=None,
        description="The column the build names its outcome, the same for every window; null in a case stored "
        "before it was recorded (the use case's own target column).",
    )
    recipe: Literal["rfm", "events"] = Field(
        default="rfm",
        description="The features the build measures: `rfm` when there is an amount, else counts.",
    )
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
    exact: bool = Field(
        default=True,
        description="The counts are made on every row of the file. False only when no reader of the whole file was "
        "at hand: the counts are then a sample's, and the floors are not taken as reached unless a sample's count "
        "cannot be higher than the file's.",
    )
    audiences: tuple[BuildAudience, ...] = Field(
        default=(),
        description="When the goal names a narrower audience: the counts of both readings (all active before the "
        "snapshot, and quiet before it). The person picks one; empty when the goal names none.",
    )
    enough: bool = Field(description="The rows built would reach the configured floors (in some reading).")
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


def _ids_days(frame: pd.DataFrame, key: str, when: str) -> tuple[pd.Series[str], pd.Series[pd.Timestamp]]:
    """The customer (text, trimmed) and the day of every row of `frame` that has both."""
    ids = frame[key].astype("string").str.strip()
    ids = ids.mask(ids == "")
    if frame[when].dtype == object:
        days = to_datetime_mixed(frame[when])
    else:
        days = pd.to_datetime(frame[when], errors="coerce")
    keep = (ids.notna() & days.notna()).to_numpy()
    return ids[keep], days[keep].dt.normalize()


def _events_cost(events: _Events | None) -> int:
    """What an `_Events` weighs in bytes. A customer ID is a Python string per row, so the text is counted too."""
    return 0 if events is None else _series_bytes(events.ids) + _series_bytes(events.days)


def _series_bytes(series: pd.Series[Any]) -> int:
    return int(series.memory_usage(index=False, deep=True))


_CACHE_BUDGET: Final[int] = 128 * 2**20
_EVENTS: Final[ContentCache[_Events | None]] = ContentCache(4, budget=_CACHE_BUDGET, cost=_events_cost)
_WHOLE: Final[ContentCache[pd.DataFrame | None]] = ContentCache(
    4,
    budget=_CACHE_BUDGET,
    cost=lambda pairs: 0 if pairs is None else int(pairs.memory_usage(index=False, deep=True).sum()),
)
"""Read once per content, not per frame object: every step of a visit loads its files again, and reading the dates
is the slow part. Each cache has a byte budget: a table too big for it is read again by the next step, never kept
(the customer IDs of an upload must not outlive the request in a long-lived process). (A cache keyed by `id(frame)`
never hit across steps, and one keyed by a freed frame's id could hand back another table's answer.)"""


def _events(frame: pd.DataFrame) -> _Events | None:
    """`frame` as events, or None."""
    found = _columns(frame)
    if found is None:
        return None
    key, when, amount, quantity, order = found
    digests = (content_digest(frame[key]), content_digest(frame[when]))
    cache_key = None if None in digests else f"{key}|{when}|{amount}|{quantity}|{order}|{digests}"

    def make() -> _Events | None:
        ids, days = _ids_days(frame, key, when)
        return _Events(key, when, amount, quantity, order, ids, days) if len(ids) else None

    return _EVENTS.get_or_compute(cache_key, make)


def _every_row(loaded: LoadedFile, events: _Events) -> pd.DataFrame | None:
    """The distinct (customer, day) pairs of every row of the file: from the frame when it is the whole file, else
    from the ID and date columns read over the whole stored file (`LoadedFile.column_chunks`). None when the frame is
    a sample and the whole file cannot be read: a count of distinct customers is never scaled up from a sample.
    """
    reader = loaded.column_chunks
    if loaded.file_rows is None or loaded.file_rows <= len(loaded.frame):
        reader = None
    elif reader is None:
        return None

    def make() -> pd.DataFrame | None:
        if reader is None:  # the frame is the whole file
            return pd.DataFrame({"id": events.ids, "day": events.days}).drop_duplicates()
        parts: list[pd.DataFrame] = []
        try:
            for chunk in reader([events.key, events.when]):
                ids, days = _ids_days(chunk, events.key, events.when)
                parts.append(pd.DataFrame({"id": ids, "day": days}).drop_duplicates())
        except (
            Exception
        ) as exc:  # a file that cannot be read again (renamed columns, gone) is counted as unknown
            _LOG.info("doctor.build_count_unread upload=%s error=%s", loaded.upload_id, type(exc).__name__)
            return None
        if not parts:
            return None
        return pd.concat(parts, ignore_index=True).drop_duplicates()

    # The pairs depend on the whole stored file (its fingerprint covers every row) and on the customers and days read.
    digests = (content_digest(events.ids), content_digest(events.days))
    cache_key = None if None in digests else f"{loaded.profile.fingerprint.hash}|{reader is None}|{digests}"
    return _WHOLE.get_or_compute(cache_key, make)


@dataclass(frozen=True)
class _Counts:
    start: date
    end: date
    distinct: int
    snapshot: date | None
    active: int = 0
    yes: int = 0
    quiet: int = 0
    quiet_yes: int = 0


def _measure(pairs: pd.DataFrame, window_days: int, label_type: str, quiet_days: int | None) -> _Counts:
    """The span, the distinct customers, the snapshot, the customers active on or before it and the 'yes' among
    them; with `quiet_days`, also the customers among them with no row in the `quiet_days` days up to it."""
    ids, days = pairs["id"], pairs["day"]
    start, end = days.min().date(), days.max().date()
    snapshot = snapshot_for(start, end, window_days)
    distinct = int(ids.nunique())
    if snapshot is None:
        return _Counts(start, end, distinct, None)
    cut = pd.Timestamp(snapshot)
    known = set(ids[days <= cut].unique())
    later = set(ids[(days > cut) & (days <= cut + pd.Timedelta(days=window_days))].unique())

    def yes_of(group: set[Any]) -> int:
        came = len(group & later)
        return came if label_type == "event_presence" else len(group) - came

    quiet: set[Any] = set()
    if quiet_days is not None:
        recent = set(ids[(days > cut - pd.Timedelta(days=quiet_days)) & (days <= cut)].unique())
        quiet = known - recent
    return _Counts(start, end, distinct, snapshot, len(known), yes_of(known), len(quiet), yes_of(quiet))


def buildable_outcome(
    files: Sequence[LoadedFile],
    config: UseCaseConfig,
    root: Path | None,
    *,
    window_days: int | None = None,
    audience_id: str | None = None,
) -> BuildableOutcome | None:
    """The outcome of `config` that an event file among `files` can define, counted on every row; None when the goal
    does not allow building it, has no event outcome in `configs/outcomes.yaml`, or no file is an event table (an ID
    that repeats and a date).

    `window_days` is the days after the snapshot the outcome looks at (the template's default when None) and must be
    one the template allows. Both audiences are always counted; with `audience_id` the table is judged on that one
    (`enough` is its floors), which the template must offer. A value the template does not allow is a ValueError: the
    caller checks it first (`GoalSettings.allowed`)."""
    from engine.config import config_root
    from engine.doctor.outcomes import load_outcome_catalogue

    if config.target.column is None or not _levels_allow(config) or config.target.positive_label not in _YES:
        return None
    template = load_outcome_catalogue(config_root(root)).templates.get(config.id)
    if template is None:
        return None
    noun = config.entity or "customer"
    days = template.default_days if window_days is None else window_days
    if days not in template.days:
        raise ValueError(f"a window of {days} days is not one of {list(template.days)} for {config.id}")
    narrower = template.audience
    if audience_id is not None and (narrower is None or narrower.option(audience_id) is None):
        raise ValueError(f"{config.id} does not offer the audience {audience_id!r}")
    quiet_option = narrower.option("quiet") if narrower is not None else None
    floors = (config.validation.min_rows, config.validation.min_positive)
    for loaded in files:
        frame = loaded.frame
        if frame.empty:
            continue
        events = _events(frame)
        if events is None:
            continue
        rows = loaded.file_rows or len(frame)
        pairs = _every_row(loaded, events)
        exact = pairs is not None
        if pairs is None:  # a sample only: its counts are a floor of the file's at best
            pairs = pd.DataFrame({"id": events.ids, "day": events.days}).drop_duplicates()
        counted = _measure(
            pairs, days, template.label_type, quiet_option.quiet_days if quiet_option is not None else None
        )
        start, end, snapshot = counted.start, counted.end, counted.snapshot

        presence = template.label_type == "event_presence"
        # A sample's count is used only when it cannot be higher than the file's (customers seen, and a 'yes' that is
        # a row seen in the window); a floor is a hard rule, never met by scaling a count up.
        trusted = snapshot is not None and exact
        floor_ok = snapshot is not None and (exact or presence)
        audiences: tuple[BuildAudience, ...] = ()
        active_option = narrower.option("active") if narrower is not None else None
        if active_option is not None and quiet_option is not None and snapshot is not None:
            audiences = (
                BuildAudience(
                    audience_id="active",
                    label=active_option.phrase(_plural(noun)),
                    customers=counted.active,
                    yes=counted.yes,
                    enough=floor_ok and _reaches(counted.active, counted.yes, floors),
                ),
                BuildAudience(
                    audience_id="quiet",
                    quiet_days=quiet_option.quiet_days,
                    label=quiet_option.phrase(_plural(noun)),
                    customers=counted.quiet,
                    yes=counted.quiet_yes,
                    # quiet is "no row seen lately": a sample can miss the recent row, so only every row says it
                    enough=trusted and _reaches(counted.quiet, counted.quiet_yes, floors),
                ),
            )
        picked = [a for a in audiences if audience_id in {None, a.audience_id}]
        enough = (
            any(a.enough for a in picked)
            if audiences
            else floor_ok and _reaches(counted.active, counted.yes, floors)
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
                f"{counted.active:,} {noun}s, {counted.yes:,} of them a yes."
            )
            if len(audiences) > 1:
                quiet = audiences[1]
                sentence += (
                    f" Counting only the {quiet.label}: {quiet.customers:,}, {quiet.yes:,} of them a yes. "
                    "Which of the two the goal means is yours to say."
                )
            if not exact:
                sentence += f" Counted on {len(frame):,} of {rows:,} rows only."
        return BuildableOutcome(
            upload_id=loaded.upload_id,
            file_name=loaded.file_name,
            entity_key=events.key,
            time_column=events.when,
            amount_column=events.amount,
            quantity_column=events.quantity,
            order_column=events.order,
            label_type=template.label_type,
            outcome_column=template.build.outcome_column or config.target.column,
            recipe=template.build.recipe,
            window_days=days,
            snapshot=snapshot,
            data_start=start,
            data_end=end,
            ids=counted.distinct,
            active_before=counted.active,
            yes=counted.yes,
            rows_read=rows if exact else len(frame),
            file_rows=rows,
            exact=exact,
            audiences=audiences,
            enough=enough,
            sentence=sentence,
        )
    return None


def _reaches(customers: int, yes: int, floors: tuple[int, int]) -> bool:
    return customers >= floors[0] and yes >= floors[1]


def _plural(noun: str) -> str:
    return noun if noun.endswith("s") else f"{noun}s"


_DESCRIPTION_ROOM = 300
"""The most characters a query step's description holds (`SqlStepProposal`, `SqlParams`)."""


def compile_build(
    built: BuildableOutcome, config: UseCaseConfig, table: str, audience: BuildAudience | None = None
) -> tuple[str, str]:
    """(the query, its description) that builds one row per customer known at `$as_of` over the table whose SQL name is
    `table`: the behaviour features measured at that date (`propose_features`'s own compiler, so none reads a row
    after it) and the outcome, read from the `window_days` days after it (the only part that reads the future). The
    outcome is 1 for a yes and 0 for a no, named `built.outcome_column`: the same name for every window (the window is in
    the query and in the setting), so a decision about the outcome never points at a column that no longer exists. With the `quiet` audience the person
    picked, only the customers with no row in its `quiet_days` days up to `$as_of` are kept (read on or before it).
    """
    from engine.doctor.tools_features import FeatureArg, ProposeFeaturesArgs, _col, compile_features

    outcome = built.outcome_column or config.target.column
    assert outcome is not None  # buildable_outcome returns nothing for a goal without a target column
    key, when = built.entity_key, built.time_column
    counts = (
        FeatureArg(name="days_since_last_event", agg="days_since_last"),
        FeatureArg(name="events_90d", agg="count", window_days=90),
        FeatureArg(name="events_365d", agg="count", window_days=365),
        FeatureArg(name="events_total", agg="count"),
    )
    pricey = built.amount_column is not None and any(
        w in built.amount_column.casefold() for w in ("price", "rate", "cost")
    )
    if built.recipe == "events":
        given = ProposeFeaturesArgs(
            upload_id=built.upload_id,
            entity_key=key,
            time_column=when,
            output_name="training_table",
            features=counts,
        )
    elif built.quantity_column and built.amount_column and pricey:
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
            features=counts,
        )
    features = compile_features(given, table, outcome=outcome)
    days = built.window_days
    id_key, id_when, label = _col(key), _col(when), _col(outcome)
    yes, no = (1, 0) if built.label_type == "event_presence" else (0, 1)
    quiet = audience.quiet_days if audience is not None and audience.audience_id == "quiet" else None
    recent, only_quiet = "", ""
    if quiet is not None:
        recent = (
            f"r AS (SELECT DISTINCT CAST({id_key} AS VARCHAR) AS entity_key FROM {table} "
            f"WHERE NULLIF(TRIM(CAST({id_key} AS VARCHAR)), '') IS NOT NULL "
            f"AND TRY_CAST({id_when} AS DATE) > CAST($as_of AS DATE) - INTERVAL {int(quiet)} DAY "
            f"AND TRY_CAST({id_when} AS DATE) <= CAST($as_of AS DATE)), "
        )
        only_quiet = f'WHERE CAST(f.{id_key} AS VARCHAR) NOT IN (SELECT entity_key FROM "r") '
    sql = (
        f"WITH f AS ({features.sql}), "
        f"{recent}"
        f"w AS (SELECT DISTINCT CAST({id_key} AS VARCHAR) AS entity_key FROM {table} "
        f"WHERE NULLIF(TRIM(CAST({id_key} AS VARCHAR)), '') IS NOT NULL "
        f"AND TRY_CAST({id_when} AS DATE) > CAST($as_of AS DATE) "
        f"AND TRY_CAST({id_when} AS DATE) <= CAST($as_of AS DATE) + INTERVAL {days} DAY) "
        f"SELECT f.*, CASE WHEN w.entity_key IS NULL THEN {no} ELSE {yes} END AS {label} "
        f'FROM "f" LEFT JOIN "w" ON CAST(f.{id_key} AS VARCHAR) = w.entity_key {only_quiet}ORDER BY f.{id_key}'
    )
    noun = config.entity or "customer"
    word = "came back" if built.label_type == "event_presence" else "had no row"
    who = (
        f"known at the snapshot with no row in the {quiet} days up to it"
        if quiet is not None
        else "known at the snapshot"
    )
    names = [f.name for f in features.features]
    ending = f", and {outcome}: 1 when the {noun} {word} in the {days} days after the snapshot, else 0."
    head = f"Training table: one row per {noun} {who}, with behaviour measured on or before it"
    # The description has a fixed room (`_DESCRIPTION_ROOM`): when the feature names do not fit, fewer of them are named
    # ("such as ...") so the sentence is always told to its end, never cut in the middle of the outcome.
    description = f"{head} ({', '.join(names)}){ending}"
    shown = len(names)
    while len(description) > _DESCRIPTION_ROOM and shown > 0:
        shown -= 1
        listed = f" ({len(names)} measures, such as {', '.join(names[:shown])})" if shown else ""
        description = f"{head}{listed}{ending}"
    return sql, description[:_DESCRIPTION_ROOM]
