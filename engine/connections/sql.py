"""What every SQL database connector shares: the test, the browser, the preview and the fetch.

A dialect (`postgres.py`, `mysql.py`, `snowflake.py`) supplies four things - how to connect (read-only
where the database allows it), how to list schemas and tables, how to write the one query this
module ever runs, and whether the signed-in user could write - and this module does the rest.

**The one query.** Every row that leaves a database leaves through
`SELECT * FROM <schema>.<table> LIMIT n`, where `<schema>` and `<table>` are quoted as identifiers by
the dialect's own quoting (psycopg's `sql.Identifier` for PostgreSQL, a doubled back-tick for MySQL,
a doubled double quote for Snowflake) and `n` is an integer. No identifier is ever pasted into SQL
unquoted, and a schema and table must first appear, exactly, in the database's own listing - so a
name cannot even reach the quoting unless the database said it exists.

**Imports are CSV.** A table is streamed out in batches of `FETCH_BATCH_ROWS` rows and written as
CSV, counted against the use case's size limit as it goes. CSV and not Parquet because a batch at a
time cannot know a column's final type (a column empty for ten thousand rows), and CSV is exactly
what a person exporting the table by hand would upload: ingest reads it the same way.
"""

from __future__ import annotations

import io
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, BinaryIO, Final

import pandas as pd

from engine.connections.base import (
    FETCH_BATCH_ROWS,
    MAX_BROWSE_ITEMS,
    PREVIEW_ROWS,
    BrowseItem,
    BrowseResult,
    ConfigValue,
    ConnectorError,
    FetchResult,
    FieldSpec,
    FileFormat,
    KindInfo,
    LimitedWriter,
    Preview,
    Selection,
    StepName,
    StepStatus,
    TestReport,
    TestStep,
    frame_preview,
    int_value,
    remaining,
    report,
    skipped_after,
    step,
    text_value,
)
from engine.connections.net import reach
from engine.utils.time import utc_now

__all__ = ["SqlConnector", "database_fields"]

MAX_IDENTIFIER_CHARS: Final[int] = 256


def database_fields(*, port: int, schema_label: str = "Only this schema") -> list[FieldSpec]:
    """The form most databases need: address, database, user and password; port and schema tucked away."""
    return [
        FieldSpec(name="host", label="Server address", required=True, placeholder="db.example.com"),
        FieldSpec(name="database", label="Database name", required=True),
        FieldSpec(
            name="user", label="User name", required=True, help="Ask for a user that can only read data."
        ),
        FieldSpec(name="password", label="Password", type="password", secret=True, required=True),
        FieldSpec(name="port", label="Port", type="number", advanced=True, default=port),
        FieldSpec(
            name="schema",
            label=schema_label,
            advanced=True,
            help="Leave blank to see every schema the user can read.",
        ),
    ]


class SqlConnector(ABC):
    """A database reached over the network, browsed as schemas and tables."""

    kind: str
    label: str
    description: str
    default_port: int
    tier: str = "built_in"
    extra: str | None = None

    # --- what a dialect supplies -------------------------------------------------------------------
    @abstractmethod
    def fields(self) -> list[FieldSpec]: ...

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def connect(self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str]) -> Any:
        """A DB-API connection, read-only where the database allows. Raises `ConnectorError` for sign-in."""

    @abstractmethod
    def list_schemas(self, connection: Any) -> list[str]: ...

    @abstractmethod
    def list_tables(self, connection: Any, schema: str) -> list[str]: ...

    @abstractmethod
    def select_sql(self, schema: str, table: str, limit: int) -> Any:
        """`SELECT * FROM <schema>.<table> LIMIT <limit>`, identifiers quoted by the dialect."""

    @abstractmethod
    def write_privileges(self, connection: Any) -> int | None:
        """How many write privileges the user holds (0 means read-only), or None when it cannot tell."""

    # --- defaults a dialect may override -----------------------------------------------------------
    def needs_reach(self, config: Mapping[str, ConfigValue]) -> tuple[str, int] | None:
        """The host and port to try before signing in; None for a service reached through its SDK."""
        return text_value(config, "host"), int_value(config, "port", self.default_port)

    def open_cursor(self, connection: Any) -> Any:
        """The cursor a whole-table fetch streams through (a server-side one where the driver has it)."""
        return connection.cursor()

    def close(self, connection: Any) -> None:
        try:
            connection.close()
        except Exception:
            return

    def read_only_note(self) -> str:
        return "The session is read-only: Marketing AI cannot change anything, whatever the user may do."

    # --- the catalogue ------------------------------------------------------------------------------
    def info(self) -> KindInfo:
        return KindInfo(
            kind=self.kind,
            label=self.label,
            description=self.description,
            group="database",
            tier="optional" if self.tier == "optional" else "built_in",
            available=self.available(),
            addon=self.extra,
            fields=tuple(self.fields()),
        )

    # --- test ---------------------------------------------------------------------------------------
    def test(self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str]) -> TestReport:
        steps: list[TestStep] = []
        target = self.needs_reach(config)
        try:
            if target is not None:
                reach(*target)
                steps.append(
                    step(StepName.REACH, StepStatus.OK, f"{target[0]} answered on port {target[1]}.")
                )
        except ConnectorError as exc:
            steps.append(step(StepName.REACH, StepStatus.FAILED, exc.message, exc.fix))
            return report([*steps, *skipped_after(remaining(StepName.REACH))], utc_now())
        try:
            connection = self.connect(config, secrets)
        except ConnectorError as exc:
            if target is None and exc.code == "CONNECTION_UNREACHABLE":
                steps.append(step(StepName.REACH, StepStatus.FAILED, exc.message, exc.fix))
                return report([*steps, *skipped_after(remaining(StepName.REACH))], utc_now())
            if target is None:
                steps.append(step(StepName.REACH, StepStatus.OK, f"{self.label} answered."))
            steps.append(step(StepName.SIGN_IN, StepStatus.FAILED, exc.message, exc.fix))
            return report([*steps, *skipped_after(remaining(StepName.SIGN_IN))], utc_now())
        if target is None:
            steps.append(step(StepName.REACH, StepStatus.OK, f"{self.label} answered."))
        steps.append(
            step(
                StepName.SIGN_IN,
                StepStatus.OK,
                f"Signed in as {text_value(config, 'user') or 'the given user'}.",
            )
        )
        try:
            steps.extend(self._test_signed_in(connection, config))
        finally:
            self.close(connection)
        return report(steps, utc_now())

    def _test_signed_in(self, connection: Any, config: Mapping[str, ConfigValue]) -> list[TestStep]:
        steps: list[TestStep] = []
        try:
            schemas = self._schemas(connection, config)
            first: tuple[str, str] | None = None
            count = 0
            for schema in schemas:
                tables = self.list_tables(connection, schema)
                count += len(tables)
                if first is None and tables:
                    first = (schema, tables[0])
                if count >= MAX_BROWSE_ITEMS:
                    break
        except Exception:
            steps.append(
                step(
                    StepName.LIST,
                    StepStatus.FAILED,
                    "Signed in, but the tables could not be listed.",
                    "Ask for this user to be allowed to read the tables you need (SELECT, and USAGE on the schema).",
                )
            )
            return [*steps, *skipped_after(remaining(StepName.LIST))]
        if first is None:
            steps.append(
                step(
                    StepName.LIST,
                    StepStatus.OK,
                    "Signed in, but this user cannot see any tables.",
                    "Ask for read access to the tables you need, or check “Only this schema” under More options.",
                )
            )
            steps.append(step(StepName.READ_SAMPLE, StepStatus.SKIPPED, "Nothing to read yet."))
        else:
            more = " or more" if count >= MAX_BROWSE_ITEMS else ""
            steps.append(step(StepName.LIST, StepStatus.OK, f"Found {count}{more} tables."))
            try:
                self._rows(connection, first[0], first[1], 1)
                steps.append(
                    step(StepName.READ_SAMPLE, StepStatus.OK, f"Read a row of {first[0]}.{first[1]}.")
                )
            except Exception:
                steps.append(
                    step(
                        StepName.READ_SAMPLE,
                        StepStatus.FAILED,
                        f"The table {first[0]}.{first[1]} is listed but could not be read.",
                        "Ask for SELECT permission on the tables you need.",
                    )
                )
        steps.append(self._read_only_step(connection))
        return steps

    def _read_only_step(self, connection: Any) -> TestStep:
        try:
            writes = self.write_privileges(connection)
        except Exception:
            writes = None
        if writes is None:
            return step(
                StepName.READ_ONLY_CHECK,
                StepStatus.SKIPPED,
                "The database could not say whether this user can change data. " + self.read_only_note(),
                "For safety, use a user that can only read.",
            )
        if writes == 0:
            return step(StepName.READ_ONLY_CHECK, StepStatus.OK, "This user can only read.")
        return step(
            StepName.READ_ONLY_CHECK,
            StepStatus.WARNING,
            "This user can also change data. " + self.read_only_note(),
            "For safety, ask for a user that can only read (SELECT only) and use that one.",
        )

    # --- browse, preview, fetch --------------------------------------------------------------------
    def browse(
        self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], path: str
    ) -> BrowseResult:
        connection = self.connect(config, secrets)
        try:
            schemas = self._schemas(connection, config)
            if not path:
                if len(schemas) == 1:  # one schema: open it straight away
                    return self._table_listing(connection, schemas[0], parent=None)
                items = [BrowseItem(name=s, kind="schema", path=s, importable=False) for s in schemas]
                return BrowseResult(
                    path="",
                    parent=None,
                    items=tuple(items[:MAX_BROWSE_ITEMS]),
                    truncated=len(items) > MAX_BROWSE_ITEMS,
                )
            if path not in schemas:
                raise _not_found("schema", path)
            return self._table_listing(connection, path, parent="" if len(schemas) > 1 else None)
        except ConnectorError:
            raise
        except Exception as exc:
            raise _read_failed() from exc
        finally:
            self.close(connection)

    def _table_listing(self, connection: Any, schema: str, parent: str | None) -> BrowseResult:
        tables = self.list_tables(connection, schema)
        items = [
            BrowseItem(name=t, kind="table", path=schema, schema_name=schema, table=t, importable=True)
            for t in tables[:MAX_BROWSE_ITEMS]
        ]
        return BrowseResult(
            path=schema, parent=parent, items=tuple(items), truncated=len(tables) > MAX_BROWSE_ITEMS
        )

    def file_format(self, selection: Selection) -> FileFormat:  # noqa: ARG002
        return "csv"

    def preview(
        self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], selection: Selection
    ) -> Preview:
        connection = self.connect(config, secrets)
        try:
            schema, table = self._checked(connection, config, selection)
            columns, rows = self._rows(connection, schema, table, PREVIEW_ROWS)
        except ConnectorError:
            raise
        except Exception as exc:
            raise _read_failed() from exc
        finally:
            self.close(connection)
        return frame_preview(pd.DataFrame(list(rows), columns=list(columns)))

    def fetch(
        self,
        config: Mapping[str, ConfigValue],
        secrets: Mapping[str, str],
        selection: Selection,
        sink: BinaryIO,
        *,
        limit_bytes: int,
        max_rows: int,
    ) -> FetchResult:
        connection = self.connect(config, secrets)
        name = ""
        try:
            schema, table = self._checked(connection, config, selection)
            name = f"{schema}.{table}.csv"
            writer = LimitedWriter(sink, limit_bytes, f"The table {schema}.{table}")
            cursor = self.open_cursor(connection)
            try:
                cursor.execute(self.select_sql(schema, table, max_rows))
                columns = [str(d[0]) for d in cursor.description]
                rows = 0
                header = True
                while True:
                    batch = cursor.fetchmany(FETCH_BATCH_ROWS)
                    if not batch and not header:
                        break
                    buffer = io.StringIO()
                    pd.DataFrame(list(batch), columns=columns).to_csv(buffer, index=False, header=header)
                    writer.write(buffer.getvalue().encode("utf-8"))
                    rows += len(batch)
                    header = False
                    if not batch:
                        break
            finally:
                _close_cursor(cursor)
        except ConnectorError:
            raise
        except Exception as exc:
            raise _read_failed() from exc
        finally:
            self.close(connection)
        return FetchResult(file_format="csv", file_name=name, size_bytes=writer.written, rows=rows)

    # --- helpers ------------------------------------------------------------------------------------
    def _schemas(self, connection: Any, config: Mapping[str, ConfigValue]) -> list[str]:
        only = text_value(config, "schema")
        schemas = self.list_schemas(connection)
        if only:
            if only not in schemas:
                raise _not_found("schema", only)
            return [only]
        return schemas

    def _checked(
        self, connection: Any, config: Mapping[str, ConfigValue], selection: Selection
    ) -> tuple[str, str]:
        """The selection's schema and table, exactly as the database lists them, or a `ConnectorError`."""
        schema = selection.schema_name or ""
        table = selection.table or ""
        if not schema or not table:
            raise ConnectorError(
                "CONNECTION_PICK_A_TABLE", "Pick a table first.", "Choose a table from the list."
            )
        for name in (schema, table):
            if len(name) > MAX_IDENTIFIER_CHARS or "\x00" in name:
                raise _not_found("table", f"{schema}.{table}")
        if schema not in self._schemas(connection, config):
            raise _not_found("schema", schema)
        if table not in self.list_tables(connection, schema):
            raise _not_found("table", f"{schema}.{table}")
        return schema, table

    def _rows(self, connection: Any, schema: str, table: str, limit: int) -> tuple[list[str], Sequence[Any]]:
        cursor = connection.cursor()
        try:
            cursor.execute(self.select_sql(schema, table, limit))
            columns = [str(d[0]) for d in cursor.description]
            return columns, cursor.fetchall()
        finally:
            _close_cursor(cursor)


def _close_cursor(cursor: Any) -> None:
    try:
        cursor.close()
    except Exception:
        return


def _not_found(what: str, name: str) -> ConnectorError:
    return ConnectorError(
        "CONNECTION_OBJECT_NOT_FOUND",
        f"There is no {what} called {name} that this user can read.",
        "Open the list again and pick one that is there.",
        status=404,
    )


def _read_failed() -> ConnectorError:
    return ConnectorError(
        "CONNECTION_FAILED",
        "The database could not be read.",
        "Test the connection to see what is wrong.",
        status=502,
    )
