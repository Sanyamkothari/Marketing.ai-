"""Reading from a saved connection, read-only: a table, a file, or the newest file under a folder (Plan J M107).

The monthly loop reads the client's systems instead of waiting for someone to download and upload files:
a recipe's tables before each scheduled build, a campaign's outcomes once its window has closed, a consent
table. Every read goes through a saved connection (Plan H M80) and its connector, which only ever reads,
on a read-only session where the service allows one (DEC-1105). Nothing here writes to a client's system.

**What may be read** (`PullSelection`): exactly one of a file (`path`), the newest CSV or Parquet file
anywhere under a folder (`prefix`, a store only), or a table (`schema_name` and `table`, a database only).

**The query rule** (DEC-1317, amending DEC-1105). From a SQL database the only statement that reads rows is
`SELECT * FROM <schema>.<table> [WHERE <column> >= '<from>' AND <column> < '<to + 1 day>'] LIMIT n`: one
date window (`DateWindow`) on a column the request declared, which must be one of the table's own columns,
quoted by the dialect's rule, with the two dates written from `datetime.date` values
(`engine.connections.sql.fetch_window`). There is no free SQL anywhere. A file in a store, or a BigQuery
table (read through its table API, which takes no condition), is read whole within the size limit and the
same window is applied here, after reading; `PullRecord.filtered_by` says which happened.

**Days, both ends included.** `date_from` and `date_to` are whole days, both inside the window. A row
whose date cannot be read is left out and counted (`unreadable_dates`), never guessed at.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, Field, model_validator

from engine.config import StrictBase
from engine.connections.base import ConfigValue, ConnectorError, Selection, addon_missing

if TYPE_CHECKING:
    import pandas as pd

    from engine.connections.store import ConnectionStore

__all__ = [
    "PULL_CODES",
    "PULL_INVALID",
    "PULL_MAX_ROWS",
    "PULL_NOTHING_FOUND",
    "PULL_SOURCE_FILENAME",
    "DateWindow",
    "OutcomePullSpec",
    "PullRecord",
    "PullSelection",
    "pull_frame",
]

PULL_INVALID: Final[str] = "PULL_INVALID"
"""422: the selection or the date window cannot be read as asked (a folder of a database, a column the
table does not have, a window that ends before it starts). The message names the problem, never a value."""
PULL_NOTHING_FOUND: Final[str] = "PULL_NOTHING_FOUND"
"""404: the folder holds no CSV or Parquet file to read."""
PULL_CODES: Final[frozenset[str]] = frozenset({PULL_INVALID, PULL_NOTHING_FOUND})
"""Joined into `engine.decide.codes.PLAN_J_CODES` at integration, each with a `configs/pilot/help.yaml` entry."""

PULL_MAX_ROWS: Final[int] = 50_000_000
"""A backstop on a table's rows, as for an import (`api.routes.connections.IMPORT_MAX_ROWS`); the use
case's file-size limit is the limit that normally applies."""

PULL_SOURCE_FILENAME: Final[str] = "pull_source.json"
"""Beside an upload made from a pull: which connection, which table or file, which window. Never a secret."""

_MAX_NAME: Final[int] = 1024


class PullSelection(StrictBase):
    """What to read from a connection: one file, the newest file under a folder, or one table."""

    path: str | None = Field(default=None, max_length=_MAX_NAME, description="A store's file.")
    prefix: str | None = Field(
        default=None,
        max_length=_MAX_NAME,
        description="A store's folder: the newest CSV or Parquet file anywhere under it is read.",
    )
    schema_name: str | None = Field(
        default=None, max_length=256, description="A database's schema or dataset."
    )
    table: str | None = Field(default=None, max_length=256, description="A database's table or view.")

    @model_validator(mode="after")
    def _one_thing(self) -> PullSelection:
        named = [
            bool(self.path),
            bool(self.prefix),
            bool(self.schema_name or self.table),
        ]
        if sum(named) != 1:
            raise ValueError("name exactly one of: a file (path), a folder (prefix), or a schema and a table")
        return self

    @property
    def is_table(self) -> bool:
        return bool(self.schema_name or self.table)


class DateWindow(StrictBase):
    """Whole days, both included, on one declared column."""

    column: str = Field(min_length=1, max_length=256, description="The column that dates each row.")
    date_from: date = Field(description="The first day read.")
    date_to: date = Field(description="The last day read.")

    @model_validator(mode="after")
    def _ordered(self) -> DateWindow:
        if "\x00" in self.column:
            raise ValueError("a column name cannot hold a NUL character")
        if self.date_to < self.date_from:
            raise ValueError("date_to is before date_from")
        return self

    @property
    def end_exclusive(self) -> date:
        """The day after `date_to`: the window is `>= date_from` and `< end_exclusive`."""
        return self.date_to + timedelta(days=1)


class OutcomePullSpec(StrictBase):
    """Where a `measure` schedule reads a campaign's outcomes (`ScheduleParameters.outcomes`). Ids and names only.

    The window is the campaign's own - its treatment start to the end of its outcome window - so only the
    column that dates each row is named here.
    """

    connection_id: str = Field(min_length=1, max_length=64, description="The saved connection to read.")
    selection: PullSelection = Field(description="The table, file or folder holding the outcomes.")
    date_column: str = Field(min_length=1, max_length=256, description="The column that dates each row.")
    outcome_column: str | None = Field(
        default=None, max_length=256, description="The outcome column; found in the rows when null."
    )
    positive_label: str | None = Field(
        default=None, max_length=256, description="The value that counts as a conversion."
    )


class PullRecord(StrictBase):
    """`pull_source.json`: where a pull's rows came from. No secret and no data value."""

    connection_id: str
    kind: str = Field(description="The connection's kind, e.g. `postgres` or `s3`.")
    path: str | None = Field(default=None, description="The file read.")
    prefix: str | None = Field(default=None, description="The folder whose newest file was read.")
    schema_name: str | None = None
    table: str | None = None
    window: DateWindow | None = Field(default=None, description="The date window read; null for every row.")
    filtered_by: Literal["database", "marketing_ai"] | None = Field(
        default=None,
        description=(
            "`database` when the database read only the window (the one WHERE); `marketing_ai` when the file "
            "or table was read whole and the window applied after reading; null with no window."
        ),
    )
    rows: int = Field(description="Rows kept.")
    unreadable_dates: int = Field(
        default=0, description="Rows left out because their date could not be read."
    )
    pulled_at: AwareDatetime


@runtime_checkable
class NewestFile(Protocol):
    """A store connector that can name the newest file under a folder (S3, Azure Blob Storage)."""

    def newest(
        self, config: dict[str, ConfigValue], secrets: dict[str, str], prefix: str
    ) -> str | None: ...  # pragma: no cover - a protocol


@runtime_checkable
class WindowedTables(Protocol):
    """A database connector that reads a date window itself (the SQL databases)."""

    def fetch_window(
        self,
        config: dict[str, ConfigValue],
        secrets: dict[str, str],
        selection: Selection,
        sink: Any,
        *,
        column: str,
        start: date,
        end: date,
        limit_bytes: int,
        max_rows: int,
    ) -> Any: ...  # pragma: no cover - a protocol


def invalid(message: str, fix: str) -> ConnectorError:
    return ConnectorError(PULL_INVALID, message, fix, status=422)


def pull_frame(
    connections: ConnectionStore,
    connection_id: str,
    selection: PullSelection,
    *,
    window: DateWindow | None,
    limit_bytes: int,
    now: datetime,
    max_rows: int = PULL_MAX_ROWS,
) -> tuple[pd.DataFrame, PullRecord]:
    """The rows `selection` names (inside `window` when given), read once, and the record of where from.

    Raises `ConnectorError`: the connector's own codes for a connection that cannot be reached or read,
    `PULL_INVALID` for a selection or window that does not fit the connection, `PULL_NOTHING_FOUND` for an
    empty folder.
    """
    from engine.connections.registry import connector as connector_for

    record = connections.get(connection_id)
    connector = connector_for(record.kind)
    if not connector.available():
        info = connector.info()
        raise addon_missing(info.label, info.addon or record.kind)
    group = connector.info().group
    config = dict(record.config)
    secrets = connections.secrets(record)
    path: str | None = None
    filtered_by: Literal["database", "marketing_ai"] | None = None
    sink = io.BytesIO()
    if group == "database":
        if not selection.is_table:
            raise invalid(
                "A database holds tables, not files or folders.", "Pick a schema and a table from the list."
            )
        if not (selection.schema_name and selection.table):
            raise invalid("A table is named by its schema and its own name.", "Pick both from the list.")
        chosen = Selection(schema_name=selection.schema_name, table=selection.table)
        if window is not None and isinstance(connector, WindowedTables):
            fetched = connector.fetch_window(
                config,
                secrets,
                chosen,
                sink,
                column=window.column,
                start=window.date_from,
                end=window.end_exclusive,
                limit_bytes=limit_bytes,
                max_rows=max_rows,
            )
            filtered_by = "database"
        else:
            fetched = connector.fetch(
                config, secrets, chosen, sink, limit_bytes=limit_bytes, max_rows=max_rows
            )
    elif group == "store":
        if selection.is_table:
            raise invalid(
                "A file store holds files, not tables.",
                "Pick a file, or a folder to read the newest file of.",
            )
        if selection.prefix:
            if not isinstance(connector, NewestFile):
                raise invalid(
                    "This kind of connection cannot pick the newest file in a folder.",
                    "Pick one file instead.",
                )
            path = connector.newest(config, secrets, selection.prefix)
            if path is None:
                raise ConnectorError(
                    PULL_NOTHING_FOUND,
                    "There is no CSV or Parquet file in that folder.",
                    "Check the folder, or wait until the next file has landed there.",
                    status=404,
                )
        else:
            path = selection.path
        chosen = Selection(path=path)
        fetched = connector.fetch(config, secrets, chosen, sink, limit_bytes=limit_bytes, max_rows=max_rows)
    else:
        raise invalid("This connection does not hold data to read.", "Pick a store or a database connection.")
    frame = _read(sink.getvalue(), fetched.file_format)
    unreadable = 0
    if window is not None and filtered_by is None:
        frame, unreadable = _within(frame, window)
        filtered_by = "marketing_ai"
    elif window is not None and window.column not in frame.columns:
        raise invalid(
            "The rows read have no column with the name given for the date.",
            "Pick the column that holds the date of each row.",
        )
    pulled = PullRecord(
        connection_id=record.connection_id,
        kind=record.kind,
        path=path,
        prefix=selection.prefix,
        schema_name=selection.schema_name,
        table=selection.table,
        window=window,
        filtered_by=filtered_by,
        rows=len(frame.index),
        unreadable_dates=unreadable,
        pulled_at=now,
    )
    return frame, pulled


def _read(data: bytes, file_format: str) -> pd.DataFrame:
    """The fetched bytes as a frame, read the way an uploaded file is (types inferred from the values)."""
    import pandas as pd

    if file_format == "parquet":
        return pd.read_parquet(io.BytesIO(data))
    if not data.strip():
        return pd.DataFrame()
    return pd.read_csv(io.BytesIO(data), low_memory=False)


def _within(frame: pd.DataFrame, window: DateWindow) -> tuple[pd.DataFrame, int]:
    """The rows whose date falls inside `window`, and how many rows had no readable date (vectorised)."""
    import pandas as pd

    if window.column not in frame.columns:
        raise invalid(
            f"The file has no column called {window.column}.",
            "Pick the column that holds the date of each row, exactly as the file spells it.",
        )
    moments = pd.to_datetime(frame[window.column], errors="coerce", utc=True, format="mixed")
    start = pd.Timestamp(window.date_from, tz="UTC")
    end = pd.Timestamp(window.end_exclusive, tz="UTC")
    unreadable = int(moments.isna().sum())
    keep = (moments >= start) & (moments < end)
    return frame.loc[keep.to_numpy()].reset_index(drop=True), unreadable
