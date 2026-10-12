"""Reshape level, one row per ID (Plan O M238, DEC-1808): plan and run a `keep_latest_per_id` recipe step.

A customer list exported every month, or a CRM table that keeps every edit of a customer, has several rows per
ID where the model needs one. `combine_rows` (M76) adds the rows up; this step keeps one of them, the latest,
and leaves the rest out. It is the only code that does.

**Entity-wise and stateless.** Which row an ID keeps depends only on that ID's own rows (in the order the file
lists them) and the step's frozen parameters - never on another ID or a statistic of the whole file (DEC-1004,
DEC-1023). The day/month order of each date column is frozen at planning (`dayfirst`, as `combine_rows`), so the
preview, the Approve on the full file and next month's scoring file read every date the same way.

**Point in time.** Each ID is described as of one date, its *snapshot*: the latest value of `snapshot_column`
among its rows, or - when the step names no separate snapshot column - its latest `time_column`. A row dated
after its ID's snapshot is never kept (the rule of `engine.agent.reshape`: only rows dated on or before the
snapshot count), so a row written after the moment the ID is described as of can never be the one the model
learns from. With a separate snapshot column, a training Approve re-checks that by adding, for every ID (the
full check) or the first 1,000 (the narrow check, on a scoring file), a row dated a day after its snapshot and
proving no kept row moves - the point-in-time probe of decision O3.

**Ties and conflicting duplicates.** Among an ID's rows of its latest date (then its latest snapshot), rows that
are identical in every column are one row: the copies are left out and counted. Rows that differ are a
*conflict*: the file does not say which is right. `ties` is frozen when the person answers the one question the
planner asks: `refuse` (the default: a conflict stops the step with `RECIPE_VALUES_UNCONVERTED`, nothing is
guessed), `last_in_file` or `first_in_file` (the later or earlier of the ID's own rows as the file lists them).

**Counted.** A row with no ID, no readable date, or whose ID has no readable snapshot on any row belongs to no
ID at no time; it is left out and counted as a failure, and more failures than the use case's
`max_conversion_failure_pct` stop the step. The result lists the rows kept, the identical copies and the rows
dated after the snapshot that were left out.

The step never reads the outcome to choose a row: it is refused when its date, snapshot or ID column is the
outcome (`engine.agent.recipe.check_recipe`). The outcome is carried on the kept row like every other column.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import pandas as pd

from engine.agent.reshape import CombineError, LeakCheck, _dates, _keys, _order

__all__ = [
    "KEEP_LATEST_TIES",
    "NARROW_PROBE_IDS",
    "KeepLatestError",
    "KeepLatestResult",
    "KeepLatestSpec",
    "LatestConflicts",
    "find_conflicts",
    "keep_latest_per_id",
    "plan_keep_latest",
]

KEEP_LATEST_TIES: Final[frozenset[str]] = frozenset({"refuse", "last_in_file", "first_in_file"})
"""How rows of an ID's latest date that differ are settled; `refuse` stops the step instead of guessing."""

NARROW_PROBE_IDS: Final[int] = 1_000
"""The narrow point-in-time check (a scoring file) re-checks this many IDs, the first by their ID as text."""

_PARAMS: Final[frozenset[str]] = frozenset({"time_column", "snapshot_column", "dayfirst", "ties"})
_DAY_NS: Final[int] = 86_400 * 10**9
_NAT: Final[int] = np.iinfo(np.int64).min


class KeepLatestError(Exception):
    """A step that cannot run on this file; `code` is a RECIPE_* code the recipe engine passes on."""

    def __init__(self, code: str, message: str, *, column: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.column = column


@dataclass(frozen=True)
class KeepLatestSpec:
    """A `keep_latest_per_id` step's parameters, checked."""

    key: str
    time_column: str
    snapshot_column: str | None
    dayfirst: bool | Mapping[str, bool | None] | None
    ties: str

    def order(self, column: str) -> bool | None:
        """The frozen day/month order of `column` (True: day first); None when it was not decided."""
        if isinstance(self.dayfirst, Mapping):
            return self.dayfirst.get(column)
        return self.dayfirst

    @property
    def reads(self) -> tuple[str, ...]:
        """Every column the step needs in the file."""
        needed = [self.key, self.time_column]
        if self.snapshot_column is not None:
            needed.append(self.snapshot_column)
        return tuple(dict.fromkeys(needed))

    @classmethod
    def from_params(cls, key: str, params: Mapping[str, Any]) -> KeepLatestSpec:
        """Check `params`; raises `KeepLatestError("RECIPE_STEP_INVALID", …)` naming what is wrong."""

        def refuse(what: str) -> KeepLatestError:
            return KeepLatestError("RECIPE_STEP_INVALID", f"Keeping the latest row per ID: {what}")

        unknown = set(params) - _PARAMS
        if unknown:
            raise refuse(f"unknown parameter {sorted(unknown)[0]!r}.")
        time_column = params.get("time_column")
        if not isinstance(time_column, str) or not time_column:
            raise refuse("the date column that orders the rows must be named.")
        snapshot = params.get("snapshot_column")
        if snapshot is not None and (not isinstance(snapshot, str) or not snapshot):
            raise refuse("the snapshot column must be a column name or null.")
        if snapshot == time_column:
            snapshot = None
        dayfirst = params.get("dayfirst", False)
        if isinstance(dayfirst, Mapping):
            if not all(
                isinstance(column, str) and (order is None or isinstance(order, bool))
                for column, order in dayfirst.items()
            ):
                raise refuse("dayfirst maps each date column to true, false or null.")
            dayfirst = dict(dayfirst)
        elif dayfirst is not None and not isinstance(dayfirst, bool):
            raise refuse("dayfirst must be true, false, null or a map of date columns to one of those.")
        ties = params.get("ties", "refuse")
        if ties not in KEEP_LATEST_TIES:
            raise refuse(f"ties must be one of {sorted(KEEP_LATEST_TIES)}.")
        if key in {time_column, snapshot}:
            raise refuse("the ID column cannot also be the date.")
        spec = cls(
            key=key, time_column=time_column, snapshot_column=snapshot, dayfirst=dayfirst, ties=str(ties)
        )
        if isinstance(dayfirst, dict) and not set(dayfirst) <= set(spec.reads):
            raise refuse(
                f"dayfirst names {sorted(set(dayfirst) - set(spec.reads))[0]!r}, which the step does not read."
            )
        return spec


@dataclass(frozen=True)
class KeepLatestResult:
    """What the step did: the kept rows, in the file's order, and the counts its receipt shows."""

    frame: pd.DataFrame
    rows_in: int
    rows_out: int
    failed: int
    """Rows with no ID, no readable date, or whose ID has no readable snapshot: left out."""
    identical_copies: int
    """Rows left out because another row of the same ID and latest date is identical in every column."""
    after_snapshot: int
    """Rows dated after their ID's snapshot: never kept."""
    conflicts: int
    """IDs whose latest rows differ, settled by `ties` (0 under `refuse`, which stops instead)."""
    leak_check: str | None


@dataclass(frozen=True)
class LatestConflicts:
    """IDs whose rows of the latest date differ: what the planner's one question is about (counts only)."""

    ids: int
    rows: int
    ids_with_identical_copies: int
    after_snapshot: int
    failed: int


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def plan_keep_latest(
    frame: pd.DataFrame, *, time_column: str, snapshot_column: str | None, ties: str = "refuse"
) -> dict[str, Any]:
    """The step's parameters for this file: each date column's day/month order frozen as its values prove it.

    A column's own values decide its order; when they cannot, the order the other date column proves is used;
    else null, and a value that reads differently either way then stops the step rather than being guessed.
    """
    columns = [time_column, *([snapshot_column] if snapshot_column not in {None, time_column} else [])]
    found = {name: _order(frame[name])[0] for name in columns}
    proven = {order for order in found.values() if order is not None}
    settled = next(iter(proven)) if len(proven) == 1 else None
    return {
        "time_column": time_column,
        "snapshot_column": None if snapshot_column == time_column else snapshot_column,
        "dayfirst": {name: own if own is not None else settled for name, own in found.items()},
        "ties": ties,
    }


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
def _row_hashes(frame: pd.DataFrame) -> np.ndarray[Any, Any]:
    """One number per row that is equal exactly when the rows are equal in every column (empty equals empty)."""
    try:
        hashed = pd.util.hash_pandas_object(frame, index=False)
    except TypeError:  # an unhashable cell (a list from a JSON file): compare the rows as text
        hashed = pd.util.hash_pandas_object(frame.astype(str), index=False)
    return hashed.to_numpy(dtype=np.uint64)


def _ns(values: pd.Series[Any]) -> np.ndarray[Any, Any]:
    """Naive datetimes as int64 nanoseconds; NaT is the smallest int64, so it sorts before every date."""
    return values.astype("datetime64[ns]").to_numpy().view("int64")


@dataclass(frozen=True)
class _Choice:
    kept: np.ndarray[Any, Any]
    """Positions of the kept rows, ascending."""
    conflict_ids: int
    conflict_rows: int
    identical_copies: int
    copy_ids: int


def _choose(
    keys: np.ndarray[Any, Any],
    event: np.ndarray[Any, Any],
    snap: np.ndarray[Any, Any],
    row_hash: np.ndarray[Any, Any],
    eligible: np.ndarray[Any, Any],
    ties: str,
) -> _Choice:
    """The row each ID keeps among its `eligible` rows: latest date, then latest snapshot, then by `ties`.

    Every input is per row and every decision is per ID, so an ID's choice reads only its own rows.
    """
    rows = pd.DataFrame(
        {"k": keys, "t": event, "s": snap, "h": row_hash, "pos": np.arange(len(keys), dtype=np.int64)}
    )[eligible]
    if rows.empty:
        return _Choice(np.empty(0, dtype=np.int64), 0, 0, 0, 0)
    by_key = rows.groupby("k", sort=False)
    rows = rows[rows["t"] == by_key["t"].transform("max")]
    rows = rows[rows["s"] == rows.groupby("k", sort=False)["s"].transform("max")]
    distinct = rows.groupby("k", sort=False)["h"].transform("nunique")
    size = rows.groupby("k", sort=False)["h"].transform("size")
    conflicting = rows[distinct > 1]
    clean = rows[distinct == 1]
    kept_clean = clean.groupby("k", sort=False)["pos"].min()
    copies = clean[size[distinct == 1] > 1]
    if ties == "last_in_file":
        kept_conflict = conflicting.groupby("k", sort=False)["pos"].max()
    else:  # first_in_file; under `refuse` the caller stops before using these
        kept_conflict = conflicting.groupby("k", sort=False)["pos"].min()
    kept = np.sort(np.concatenate([kept_clean.to_numpy(), kept_conflict.to_numpy()]).astype(np.int64))
    return _Choice(
        kept=kept,
        conflict_ids=int(conflicting["k"].nunique()),
        conflict_rows=len(conflicting),
        identical_copies=len(copies) - int(copies["k"].nunique()),
        copy_ids=int(copies["k"].nunique()),
    )


@dataclass(frozen=True)
class _Prepared:
    spec: KeepLatestSpec
    keys: np.ndarray[Any, Any]
    has_key: np.ndarray[Any, Any]
    event: np.ndarray[Any, Any]
    snap: np.ndarray[Any, Any]
    known: np.ndarray[Any, Any]
    usable: np.ndarray[Any, Any]
    eligible: np.ndarray[Any, Any]
    row_hash: np.ndarray[Any, Any]


def _prepare(frame: pd.DataFrame, key: str, params: Mapping[str, Any]) -> _Prepared:
    spec = KeepLatestSpec.from_params(key, params)
    for column in spec.reads:
        if column not in frame.columns:
            raise KeepLatestError(
                "RECIPE_COLUMN_MISSING", f"The file has no column '{column}'.", column=column
            )
    orders = {name: spec.order(name) for name in spec.reads}
    try:
        event_time = _dates(frame[spec.time_column], orders.get(spec.time_column), spec.time_column)
        snapshot_at = (
            event_time
            if spec.snapshot_column is None
            else _dates(frame[spec.snapshot_column], orders.get(spec.snapshot_column), spec.snapshot_column)
        )
    except CombineError as exc:  # the shared date reader: same code, same message
        raise KeepLatestError(exc.code, exc.message, column=exc.column) from exc
    keys = _keys(frame[key]).astype(object).to_numpy()
    event = _ns(event_time)
    snap = _ns(snapshot_at)
    has_key = pd.notna(keys)
    known, usable, eligible = _eligibility(keys, has_key, event, snap)
    return _Prepared(spec, keys, has_key, event, snap, known, usable, eligible, _row_hashes(frame))


def _eligibility(
    keys: np.ndarray[Any, Any],
    has_key: np.ndarray[Any, Any],
    event: np.ndarray[Any, Any],
    snap: np.ndarray[Any, Any],
) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any], np.ndarray[Any, Any]]:
    """(each row's ID snapshot, usable rows, rows a kept row may come from), from per-row values only.

    An ID's snapshot is the latest readable snapshot over its own rows. A row is usable with an ID, a readable
    date and an ID that has a snapshot; it may be kept only when it is dated on or before that snapshot.
    """
    dated = has_key & (snap != _NAT)
    # int64 throughout (a float would round nanoseconds); an undated row is the smallest int64, never a max.
    per_row = pd.Series(np.where(dated, snap, _NAT).astype(np.int64))
    latest = per_row.groupby(pd.Series(keys, dtype=object), sort=False, dropna=False).transform("max")
    known = np.where(has_key, latest.to_numpy(dtype=np.int64), _NAT).astype(np.int64)
    usable = has_key & (event != _NAT) & (known != _NAT)
    return known, usable, usable & (event <= known)


def find_conflicts(frame: pd.DataFrame, *, key: str, params: Mapping[str, Any]) -> LatestConflicts:
    """What the step would meet on `frame`, as counts: the planner asks one question when `ids` is not 0."""
    prepared = _prepare(frame, key, params)
    choice = _choose(
        prepared.keys, prepared.event, prepared.snap, prepared.row_hash, prepared.eligible, "first_in_file"
    )
    return LatestConflicts(
        ids=choice.conflict_ids,
        rows=choice.conflict_rows,
        ids_with_identical_copies=choice.copy_ids,
        after_snapshot=int((prepared.usable & ~prepared.eligible).sum()),
        failed=int((~prepared.usable).sum()),
    )


def _probe(prepared: _Prepared, choice: _Choice, leak_check: LeakCheck) -> str:
    """Add a row dated a day after each probed ID's snapshot and prove no kept row moves (decision O3).

    The added rows go through the same rule as real ones - each ID's snapshot is computed again over its rows,
    the added row among them - so the check fails if the rule ever lets a row after the snapshot win.
    """
    spec = prepared.spec
    if spec.snapshot_column is None:
        return (
            "No separate snapshot column: each ID's latest date is the date it is described as of, so the "
            "file must hold only what was known then."
        )
    kept_keys = prepared.keys[choice.kept]
    ids = sorted({str(k) for k in kept_keys})
    probed = set(ids if leak_check == "full" else ids[:NARROW_PROBE_IDS])
    positions = choice.kept[np.array([str(k) in probed for k in kept_keys], dtype=bool)]
    extra = len(positions)
    keys = np.concatenate([prepared.keys, prepared.keys[positions]])
    has_key = np.concatenate([prepared.has_key, np.ones(extra, dtype=bool)])
    snapshot_of_id = prepared.known[positions]
    event = np.concatenate([prepared.event, snapshot_of_id + _DAY_NS])
    snap = np.concatenate([prepared.snap, snapshot_of_id])
    # Each added row differs from every real row, so it can never pass for an identical copy.
    salt = np.arange(1, extra + 1, dtype=np.uint64) * np.uint64(0x9E3779B97F4A7C15)
    row_hash = np.concatenate([prepared.row_hash, prepared.row_hash[positions] ^ salt])
    _, _, eligible = _eligibility(keys, has_key, event, snap)
    ties = "first_in_file" if spec.ties == "refuse" else spec.ties
    again = _choose(keys, event, snap, row_hash, eligible, ties)
    if not np.array_equal(again.kept, choice.kept):
        raise KeepLatestError(
            "RECIPE_STEP_INVALID",
            "FUTURE_EVENTS_LEAKED: a kept row changed when rows dated after the snapshot were added, so the "
            "prepared file could contain the answer. This is an engine fault, not a problem with the file.",
            column=spec.key,
        )
    scope = "Full" if leak_check == "full" else "Narrow"
    return (
        f"{scope} future-data check: {len(probed):,} of {len(ids):,} IDs were checked again with a row dated "
        "after their snapshot added, and no kept row changed."
    )


def keep_latest_per_id(
    frame: pd.DataFrame,
    *,
    key: str,
    params: Mapping[str, Any],
    max_failure_pct: float,
    leak_check: LeakCheck | None = None,
) -> KeepLatestResult:
    """One row per ID of `frame`, by the step's frozen parameters. `frame` is not modified."""
    prepared = _prepare(frame, key, params)
    failed = int((~prepared.usable).sum())
    if len(frame) and failed / len(frame) * 100.0 > max_failure_pct:
        raise KeepLatestError(
            "RECIPE_VALUES_UNCONVERTED",
            f"{failed} of {len(frame)} rows have no '{key}' or no readable '{prepared.spec.time_column}'"
            + (
                f", or no readable '{prepared.spec.snapshot_column}' on any row of their '{key}'"
                if prepared.spec.snapshot_column
                else ""
            )
            + f" ({failed / len(frame):.1%}), more than the {max_failure_pct:g}% limit.",
            column=prepared.spec.time_column,
        )
    ties = prepared.spec.ties
    choice = _choose(
        prepared.keys,
        prepared.event,
        prepared.snap,
        prepared.row_hash,
        prepared.eligible,
        "first_in_file" if ties == "refuse" else ties,
    )
    if ties == "refuse" and choice.conflict_ids:
        raise KeepLatestError(
            "RECIPE_VALUES_UNCONVERTED",
            f"{choice.conflict_ids} IDs in '{key}' have two or more different rows on their latest date, and "
            "nothing in the file says which is right, so none is guessed. Answer the question about these "
            "rows (keep the later or the earlier row as the file lists them), or fix them at source.",
            column=key,
        )
    summary = None if leak_check is None else _probe(prepared, choice, leak_check)
    out = frame.iloc[choice.kept].reset_index(drop=True)
    return KeepLatestResult(
        frame=out,
        rows_in=len(frame),
        rows_out=len(out),
        failed=failed,
        identical_copies=choice.identical_copies,
        after_snapshot=int((prepared.usable & ~prepared.eligible).sum()),
        conflicts=choice.conflict_ids,
        leak_check=summary,
    )
