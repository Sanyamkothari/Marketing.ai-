"""The primary key, which is one column or several (Plan A M34, DEC-083).

Phase 1 had one row per entity, so a primary key was a column name. A periodic dataset has one row
per entity *per snapshot date*, so its key is a pair: the entity key first, the snapshot date second
(`DatasetManifest.primary_key`). Rather than teach every stage to branch on that, a run normalises
its key once, into the tuple of its columns, and carries it in two shapes:

* **the key columns**, which every stage that *excludes* columns reads - validation, prepare's
  reserved set, the schema and the recipe - so that neither the entity key nor the snapshot date is
  ever learned from;
* **the row key**, one column naming one row, which every stage that *identifies* rows reads -
  explain's `row_explanations.parquet`, the join of reasons onto scores, and the scoring summary.
  For a one-column key the row key *is* that column, so a Phase 1 run is byte-for-byte what it was.
  For a composite key it is :data:`ROW_KEY_COLUMN`, the parts as text joined by :data:`KEY_SEPARATOR`,
  added to the frame after validation and never a feature.

`scores.csv` keeps the original columns rather than the joined string, because the point of the file
is that a client can join it back to their own systems, and `"C-1|2026-01-31"` is not a key in
anybody's warehouse.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from engine.config import PrimaryKey, ResolvedConfig, SplitType, UseCaseConfig, key_columns

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    "KEY_SEPARATOR",
    "ROW_KEY_COLUMN",
    "PrimaryKey",
    "entity_column",
    "is_composite",
    "key_columns",
    "key_label",
    "key_series",
    "key_text",
    "normalise_key",
    "row_key_column",
    "split_config_for_key",
    "with_row_key",
]

KEY_SEPARATOR: Final[str] = "|"
"""Joins the parts of a composite key into the one string a row index needs."""

ROW_KEY_COLUMN: Final[str] = "_row_key"
"""The internal column naming one row of a composite-key frame; never written to `scores.csv`."""


def _columns(primary_key: PrimaryKey | Sequence[str]) -> tuple[str, ...]:
    columns = key_columns(primary_key if isinstance(primary_key, str) else list(primary_key))
    if not columns:
        raise ValueError("A primary key needs at least one column.")
    if any(not isinstance(name, str) or not name for name in columns):
        raise ValueError(f"Every part of a primary key must be a column name: {list(columns)}.")
    duplicates = sorted({name for name in columns if columns.count(name) > 1})
    if duplicates:
        raise ValueError(f"A primary key names {', '.join(duplicates)} more than once.")
    return columns


def normalise_key(primary_key: PrimaryKey | Sequence[str]) -> PrimaryKey:
    """The key in its narrowest faithful shape: a `str` for one column, a list for several.

    Artefacts store this, so a Phase 1 run reads back byte-for-byte as it always did - its recipe
    hash included - and a reader can tell a one-column key from a composite one.
    """
    columns = _columns(primary_key)
    return columns[0] if len(columns) == 1 else list(columns)


def is_composite(primary_key: PrimaryKey | Sequence[str]) -> bool:
    """True when the key needs more than one column to identify a row."""
    return len(_columns(primary_key)) > 1


def key_label(primary_key: PrimaryKey | Sequence[str]) -> str:
    """The key as a user-facing phrase: `customer_id`, or `entity_key + snapshot_date`."""
    return " + ".join(_columns(primary_key))


def entity_column(primary_key: PrimaryKey | Sequence[str]) -> str:
    """The column naming the entity: the key itself, or the first column of a composite key."""
    return _columns(primary_key)[0]


def row_key_column(primary_key: PrimaryKey | Sequence[str]) -> str:
    """The one column that names a row: the key itself, or :data:`ROW_KEY_COLUMN`."""
    columns = _columns(primary_key)
    return columns[0] if len(columns) == 1 else ROW_KEY_COLUMN


def key_text(series: pd.Series[Any]) -> pd.Series[Any]:
    """One key part as text, with a null as the empty string.

    A whole-number id column that has any null in it arrives from pandas as `float64`, so the
    obvious `astype("string")` would turn account `1` into `"1.0"` - a key that joins back to
    nothing in the client's system. Whole floats are narrowed to integers first; a genuinely
    fractional column is left alone rather than rounded. A date with no time of day prints as the
    date alone, which is how a snapshot date reads in the client's own tables.
    """
    import pandas as pd

    if pd.api.types.is_float_dtype(series):
        present = series.dropna()
        if present.empty or bool((present == present.round()).all()):
            series = series.astype("Int64")
    elif pd.api.types.is_datetime64_any_dtype(series):
        present = series.dropna()
        if present.empty or bool((present == present.dt.normalize()).all()):
            dates: pd.Series[Any] = series.dt.strftime("%Y-%m-%d").astype("string").fillna("")
            return dates
    return series.astype("string").fillna("")


def key_series(frame: pd.DataFrame, primary_key: PrimaryKey | Sequence[str]) -> pd.Series[Any]:
    """One string per row identifying it: the key columns as text, joined by :data:`KEY_SEPARATOR`."""
    import pandas as pd

    columns = _columns(primary_key)
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise KeyError(f"The frame has no {', '.join(missing)} column, so its rows cannot be identified.")
    parts = [key_text(frame[name]).to_numpy(dtype=object) for name in columns]
    joined = [KEY_SEPARATOR.join(str(value) for value in row) for row in zip(*parts, strict=True)]
    return pd.Series(joined, index=frame.index, dtype=object)


def with_row_key(frame: pd.DataFrame, primary_key: PrimaryKey | Sequence[str]) -> pd.DataFrame:
    """`frame` with :data:`ROW_KEY_COLUMN` added for a composite key; `frame` itself for one column.

    Idempotent: a frame that already carries the row key has it recomputed from the key columns,
    so a stale value can never survive.
    """
    if not is_composite(primary_key):
        return frame
    result = frame.copy()
    result[ROW_KEY_COLUMN] = key_series(frame, primary_key)
    return result


def split_config_for_key(resolved: ResolvedConfig, primary_key: PrimaryKey | Sequence[str]) -> ResolvedConfig:
    """The run's configuration with the split a composite key needs; unchanged for one column.

    A periodic dataset holds the same customer at several snapshot dates, so a row-level random
    split would put a customer's March row in training and their April row in test - the model
    would be graded on customers it has already seen. The split is therefore kept whole **by
    entity** (`split.group_column = <entity key>`), or, when the use case asks for a time-based
    split, made on **the snapshot date**, which is the time a periodic row describes. The leaves
    this sets are recorded as `derived` in `sources`, so `run_config.json` says the engine chose
    them. Idempotent.
    """
    columns = _columns(primary_key)
    if len(columns) == 1:
        return resolved
    config = resolved.config
    split = config.split
    entity, snapshot = columns[0], columns[-1]
    if split.type is SplitType.TIME_BASED:
        update: dict[str, Any] = {"time_column": snapshot}
        if split.group_column == snapshot:
            update["group_column"] = None
    else:
        update = {"group_column": entity}
    if all(getattr(split, name) == value for name, value in update.items()):
        return resolved
    new_split = type(split).model_validate({**split.model_dump(), **update})
    new_config: UseCaseConfig = config.model_copy(update={"split": new_split})
    sources = dict(resolved.sources)
    for name in update:
        sources[f"split.{name}"] = "derived"
    return resolved.model_copy(update={"config": new_config, "sources": sources})
