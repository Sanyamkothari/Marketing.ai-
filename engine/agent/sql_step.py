"""The sandboxed SQL step: one read-only query over the case's tables, run by our code on copies.

Data Doctor may write a query of its own (DuckDB) when no fixed step does the job: join two files, add up the bills
per customer, keep each customer's latest row. The query is data, not code we trust, so every rule that keeps it
safe is here, in code, and holds whoever wrote it (a rule, the AI or a person):

* **Read-only, one statement.** Only a single `SELECT` / `WITH` is run. DuckDB's own parser says what a statement
  is, so `CREATE`, `INSERT`, `COPY`, `ATTACH`, `INSTALL`, `LOAD`, `PRAGMA`, `SET`, `EXPORT` and the rest are refused
  before anything runs.
* **Only the listed tables.** Each input is a view named `t_<name>` over a copy of the frame; the query can read
  no other table, schema, file or address. A function that reads outside (`read_csv`, `read_parquet`, `glob`,
  `getenv`, ...) and every table function are refused, and the connection itself runs with external access off,
  no extensions, a small memory limit and one thread, and its settings locked.
* **Bounded.** A timer interrupts a query that runs past its time limit (`SQL_TIMEOUT`).
* **Repeatable.** The same query on the same tables gives the same rows: `random()`, `uuid()`, sampling and
  `now()` / `current_date` are refused, and a `LIMIT` needs an `ORDER BY`. A date the query needs comes in as
  the parameter `$as_of`.
* **Counted.** The input frames are never changed. The receipt says how many rows each table had and how many
  rows and columns came out; the query's own words are kept for the step's detail only.

The step's output for the tables it reads depends on nothing else, so the same step gives the same result on next
month's files (a replay).
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

import pandas as pd
from pydantic import Field, ValidationError

from engine.config import StrictBase

__all__ = [
    "SQL_CODES",
    "THIS_TABLE",
    "SqlReceipt",
    "SqlRun",
    "SqlStepError",
    "SqlStepParams",
    "check_sql",
    "run_sql",
    "sql_params",
    "table_alias",
]

THIS_TABLE: Final[str] = "this"
"""The table a step reads when it names `this`: the table being prepared when the step runs."""

MAX_SQL_CHARS: Final[int] = 2500
MAX_INPUTS: Final[int] = 10
TIMEOUT_SECONDS: Final[float] = 20.0
MEMORY_LIMIT: Final[str] = "512MB"
MAX_ROWS_OUT: Final[int] = 5_000_000

SQL_CODES: Final[frozenset[str]] = frozenset(
    {
        "SQL_NOT_VALID",
        "SQL_ONE_STATEMENT",
        "SQL_ONLY_SELECT",
        "SQL_READS_OUTSIDE",
        "SQL_TABLE_NOT_ALLOWED",
        "SQL_NOT_REPEATABLE",
        "SQL_NEEDS_AS_OF",
        "SQL_INPUT_MISSING",
        "SQL_TIMEOUT",
        "SQL_FAILED",
        "SQL_TOO_BIG",
    }
)
"""The codes this module raises."""

_READS_OUTSIDE: Final[frozenset[str]] = frozenset(
    {
        "glob",
        "getenv",
        "current_setting",
        "query",
        "query_table",
        "getvariable",
        "sniff_csv",
        "load_extension",
        "install_extension",
        "write_log",
        "nextval",
        "currval",
        "current_schema",
        "current_schemas",
        "current_database",
        "current_catalog",
        "version",
    }
)
_OUTSIDE_PREFIXES: Final[tuple[str, ...]] = (
    "read_",
    "parquet_",
    "duckdb_",
    "pragma_",
    "sqlite_",
    "postgres_",
    "mysql_",
    "iceberg_",
    "delta_",
    "http",
    "pg_",
)
_NOT_REPEATABLE: Final[frozenset[str]] = frozenset(
    {
        "random",
        "setseed",
        "uuid",
        "gen_random_uuid",
        "uuidv4",
        "uuidv7",
        "now",
        "today",
        "current_date",
        "current_time",
        "current_timestamp",
        "current_localtime",
        "current_localtimestamp",
        "localtime",
        "localtimestamp",
        "get_current_timestamp",
        "get_current_time",
        "transaction_timestamp",
        "statement_timestamp",
        "clock_timestamp",
        "txid_current",
    }
)
_CLOCK_WORDS: Final[frozenset[str]] = frozenset(
    {"current_date", "current_time", "current_timestamp", "localtime", "localtimestamp"}
)
_PARAMETER: Final[re.Pattern[str]] = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_QUOTED: Final[re.Pattern[str]] = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/", re.S)
_ALIAS_CHARS: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9_]+")
_OUTPUT_NAME: Final[str] = r"^[A-Za-z][A-Za-z0-9_ .-]{0,63}$"


class SqlStepError(Exception):
    """A query that cannot run. `code` is one of `SQL_CODES`; `message` is plain words and holds no cell value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class SqlStepParams(StrictBase):
    """A `sql_query` step's parameters, as frozen in the recipe."""

    sql: str = Field(
        min_length=1, max_length=MAX_SQL_CHARS, description="One SELECT, over `t_<input>` tables."
    )
    inputs: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_INPUTS,
        description="The tables it reads, by name (`this` = the one prepared).",
    )
    output_name: str = Field(pattern=_OUTPUT_NAME, description="What the table it makes is called.")
    description: str = Field(min_length=1, max_length=300, description="What it does, in plain words.")
    as_of: date | None = Field(
        default=None, description="The date `$as_of` stands for; a run may give its own."
    )


def sql_params(params: Mapping[str, Any]) -> SqlStepParams:
    """The parameters of a `sql_query` step, checked; `SqlStepError` (`SQL_NOT_VALID`) when they are not shaped so."""
    try:
        return SqlStepParams.model_validate(dict(params))
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "step"
        raise SqlStepError(
            "SQL_NOT_VALID", f"The query step's '{where}' is not valid: {first['msg']}."
        ) from exc


def table_alias(name: str) -> str:
    """The name a query uses for table `name`: `t_` and its letters, digits and underscores (`Customers.csv` is
    `t_customers_csv`)."""
    return "t_" + (_ALIAS_CHARS.sub("_", name.lower()).strip("_") or "table")


def _blank(sql: str) -> str:
    """`sql` without its string literals, quoted names and comments (so a word inside one is not read as code)."""
    return _QUOTED.sub(" ", sql)


def _connect() -> Any:
    import duckdb

    return duckdb.connect(
        ":memory:",
        config={
            "enable_external_access": False,
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
            "memory_limit": MEMORY_LIMIT,
            "threads": 1,
        },
    )


def _lock(con: Any) -> None:
    con.execute("SET lock_configuration = true")


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
def _walk(node: Any) -> Any:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _clock_refusal(name: str) -> SqlStepError:
    return SqlStepError(
        "SQL_NOT_REPEATABLE",
        f"'{name}' gives a different answer each time. Give the date as $as_of so the same files give the "
        "same table every month.",
    )


def _function_refusal(name: str, children: int) -> SqlStepError | None:
    low = name.lower()
    if low in _NOT_REPEATABLE or (low == "age" and children == 1):
        return _clock_refusal(name)
    if low in _READS_OUTSIDE or low.startswith(_OUTSIDE_PREFIXES) or low.endswith("_scan"):
        return SqlStepError(
            "SQL_READS_OUTSIDE", f"'{name}' reads outside the tables of this case, which a query may not do."
        )
    return None


def check_sql(sql: str, tables: Collection[str], *, as_of: date | None = None) -> None:
    """Refuse `sql` unless it is one read-only, repeatable SELECT over the tables named in `tables` (by
    :func:`table_alias`). Raises `SqlStepError`; nothing is run against any data."""
    import duckdb

    if len(sql) > MAX_SQL_CHARS:
        raise SqlStepError("SQL_NOT_VALID", f"A query is at most {MAX_SQL_CHARS:,} characters long.")
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as exc:
        raise SqlStepError("SQL_NOT_VALID", f"The query is not valid SQL: {_first_line(exc)}") from exc
    if len(statements) != 1:
        raise SqlStepError("SQL_ONE_STATEMENT", "A query step runs exactly one statement.")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise SqlStepError(
            "SQL_ONLY_SELECT",
            "A query step may only read: one SELECT (or WITH ... SELECT), nothing that changes anything.",
        )
    named = set(_PARAMETER.findall(_blank(sql)))
    if named - {"as_of"}:
        raise SqlStepError("SQL_NOT_VALID", "The only parameter a query may use is $as_of.")
    if "as_of" in named and as_of is None:
        raise SqlStepError("SQL_NEEDS_AS_OF", "The query uses $as_of, and no as-of date was given.")
    con = _connect()
    try:
        raw = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
    except duckdb.Error as exc:
        raise SqlStepError("SQL_NOT_VALID", f"The query is not valid SQL: {_first_line(exc)}") from exc
    finally:
        con.close()
    tree = json.loads(raw)
    if tree.get("error"):
        raise SqlStepError(
            "SQL_NOT_VALID", f"The query is not valid SQL: {str(tree.get('error_message'))[:200]}"
        )
    allowed = {name.lower() for name in tables}
    ctes = {
        str(entry["key"]).lower()
        for node in _walk(tree)
        for entry in (node.get("cte_map") or {}).get("map", [])
    }
    for node in _walk(tree):
        _check_node(node, allowed | ctes)


def _check_node(node: dict[str, Any], allowed: set[str]) -> None:
    if node.get("class") == "FUNCTION":
        refusal = _function_refusal(str(node.get("function_name", "")), len(node.get("children") or []))
        if refusal is not None:
            raise refusal
    kind = node.get("type")
    if kind == "COLUMN_REF":
        names = node.get("column_names") or []
        if len(names) == 1 and str(names[0]).lower() in _CLOCK_WORDS:
            raise _clock_refusal(str(names[0]))
    elif kind == "TABLE_FUNCTION":
        raise SqlStepError(
            "SQL_READS_OUTSIDE",
            "A query may read only the tables of this case, not a function that makes rows.",
        )
    elif kind == "BASE_TABLE":
        name = str(node.get("table_name", ""))
        if node.get("schema_name") or node.get("catalog_name") or name.lower() not in allowed:
            raise SqlStepError(
                "SQL_TABLE_NOT_ALLOWED",
                f"The query reads '{name}', which is not one of the tables it was given "
                f"({', '.join(sorted(a for a in allowed if a.startswith('t_'))) or 'none'}).",
            )
    if node.get("sample"):
        raise SqlStepError(
            "SQL_NOT_REPEATABLE", "Sampling rows picks different rows each time; use ORDER BY and LIMIT."
        )
    modifiers = node.get("modifiers")
    if isinstance(modifiers, list):
        types = {str(m.get("type")) for m in modifiers if isinstance(m, dict)}
        if any("LIMIT" in t for t in types) and "ORDER_MODIFIER" not in types:
            raise SqlStepError(
                "SQL_NOT_REPEATABLE",
                "A query with LIMIT needs an ORDER BY, or it keeps different rows each time.",
            )


def _first_line(exc: Exception) -> str:
    """The first line of an error with any quoted value taken out: an error may quote a cell."""
    line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return re.sub(r"'[^']*'", "'…'", line)[:240]


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
class SqlReceipt(StrictBase):
    """What a query did: rows read from each table, rows and columns written. Counts and names, never a cell."""

    rows_in: dict[str, int] = Field(description="Rows of each input table, by table name.")
    rows_out: int
    columns_out: int
    columns: tuple[str, ...]
    output_name: str
    as_of: date | None = None

    @property
    def sentence(self) -> str:
        read = ", ".join(f"{name} ({rows:,} rows)" for name, rows in self.rows_in.items())
        return (
            f"Read {read}; made '{self.output_name}' with {self.rows_out:,} rows and "
            f"{self.columns_out:,} columns."
        )


@dataclass(frozen=True)
class SqlRun:
    frame: pd.DataFrame
    receipt: SqlReceipt


def run_sql(
    sql: str,
    tables: Mapping[str, pd.DataFrame],
    *,
    output_name: str,
    as_of: date | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> SqlRun:
    """Run `sql` over `tables` (name -> frame, each read as `t_<name>`) and return the new table.

    The frames are registered, never written. Raises `SqlStepError`: a refused query, a missing `$as_of`,
    `SQL_TIMEOUT` past `timeout` seconds, `SQL_FAILED` when DuckDB cannot run it on these values.
    """
    import duckdb

    aliases: dict[str, str] = {}
    for name in tables:
        alias = table_alias(name)
        if alias in aliases.values():
            raise SqlStepError("SQL_NOT_VALID", f"Two tables are both called '{alias}' in a query.")
        aliases[name] = alias
    check_sql(sql, aliases.values(), as_of=as_of)
    uses_as_of = "as_of" in set(_PARAMETER.findall(_blank(sql)))
    con = _connect()
    timed_out = threading.Event()

    def stop() -> None:
        timed_out.set()
        con.interrupt()

    timer = threading.Timer(timeout, stop)
    try:
        for name, frame in tables.items():
            con.register(aliases[name], frame)
        _lock(con)
        timer.start()
        try:
            result = con.execute(sql, {"as_of": as_of} if uses_as_of else None).df()
        except duckdb.Error as exc:
            if timed_out.is_set():
                raise SqlStepError(
                    "SQL_TIMEOUT", f"The query took longer than {timeout:g} seconds and was stopped."
                ) from exc
            raise SqlStepError(
                "SQL_FAILED", f"The query could not run on these tables: {_first_line(exc)}"
            ) from exc
    finally:
        timer.cancel()
        con.close()
    if len(result) > MAX_ROWS_OUT:
        raise SqlStepError(
            "SQL_TOO_BIG", f"The query made {len(result):,} rows, more than the {MAX_ROWS_OUT:,} allowed."
        )
    receipt = SqlReceipt(
        rows_in={name: len(frame) for name, frame in tables.items()},
        rows_out=len(result),
        columns_out=len(result.columns),
        columns=tuple(str(c) for c in result.columns),
        output_name=output_name,
        as_of=as_of if uses_as_of else None,
    )
    return SqlRun(frame=result, receipt=receipt)


def aliases_of(inputs: Sequence[str]) -> list[str]:
    """The `t_<name>` of each input, in order."""
    return [table_alias(name) for name in inputs]
