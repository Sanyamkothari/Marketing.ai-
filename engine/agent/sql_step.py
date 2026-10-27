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
  the parameter `$as_of`: a date typed into the query (`DATE '2026-01-01'`, `'2026-01-01'`) is refused unless it
  is the as-of date itself, so next month's files are never filtered by last month's date. The as-of date may be
  kept as a rule ("the latest date in the file", "the last complete month-end") and worked out again on each file.
* **Counted.** The input frames are never changed. The receipt says how many rows each table had and how many
  rows and columns came out; the query's own words are kept for the step's detail only.

The step's output for the tables it reads depends on nothing else, so the same step gives the same result on next
month's files (a replay).
"""

from __future__ import annotations

import json
import re
import threading
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal

import pandas as pd
from pydantic import Field, ValidationError

from engine.config import StrictBase

__all__ = [
    "AS_OF_RULES",
    "SQL_CODES",
    "THIS_TABLE",
    "AsOfRule",
    "SqlReceipt",
    "SqlRun",
    "SqlStepError",
    "SqlStepParams",
    "check_sql",
    "future_rows_moved",
    "group_by_column",
    "resolve_as_of_rule",
    "rows_aggregated",
    "run_sql",
    "sql_params",
    "table_alias",
]

THIS_TABLE: Final[str] = "this"
"""The table a step reads when it names `this`: the table being prepared when the step runs."""

MAX_SQL_CHARS: Final[int] = 6000
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
        "SQL_FIXED_DATE",
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
_DATE_TEXT: Final[re.Pattern[str]] = re.compile(
    r"^\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})([ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?)?\s*$"
)
"""A string literal that is nothing but a date (a time of day may follow): `'2026-01-01'`, `'01/02/2026 09:30'`."""
_MAKE_DATE: Final[frozenset[str]] = frozenset(
    {
        "make_date",
        "make_timestamp",
        "make_timestamptz",
        "strptime",
        "try_strptime",
        "to_timestamp",
        "to_date",
        "epoch_ms",
    }
)
"""Functions that make a date: refused when fed only fixed values (`make_date(2026, 1, 1)`)."""
_DATE_TYPES: Final[frozenset[str]] = frozenset(
    {"DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS"}
)
_NOT_FIXED: Final[frozenset[str]] = frozenset({"COLUMN_REF", "PARAMETER", "SUBQUERY", "STAR"})
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


AS_OF_RULES: Final[tuple[str, ...]] = ("latest_date_in_file", "last_complete_month_end")


class AsOfRule(StrictBase):
    """The as-of date as a rule, not a date: next month's file gives next month's date."""

    rule: Literal["latest_date_in_file", "last_complete_month_end"]
    table: str = Field(description="The input table whose dates the rule reads (`this` = the one prepared).")
    column: str = Field(min_length=1, max_length=128)


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
    as_of_rule: AsOfRule | None = Field(
        default=None,
        description="How `$as_of` is worked out on each file, when no run gives one; `as_of` is what it was when "
        "the step was written.",
    )
    this_table: str | None = Field(
        default=None,
        max_length=128,
        description="The name the query gives the file the recipe prepares (a one-file case): that name is the "
        "table as it stands when the step runs, after the steps before it, exactly what the step was tried on.",
    )
    tried_after: str | None = Field(
        default=None,
        max_length=64,
        description="A digest of the fixes (accepted or waiting) the query was tried after. A plan whose earlier "
        "fixes differ from it names that, when the query then cannot run, instead of a bare engine error.",
    )
    reads_future: bool = Field(
        default=False,
        description="The query reads rows dated after `$as_of` on purpose (it builds the outcome); the "
        "future-rows check then leaves the outcome column out, and every other column must still not move.",
    )
    outcome_window_days: int | None = Field(
        default=None,
        ge=1,
        le=3650,
        description="How many days after `$as_of` the outcome looks (a `reads_future` query states it): the "
        "trial checks that the data reaches that far.",
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
            "python_enable_replacements": False,
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
        built_in = {
            str(row[0]).lower()
            for row in con.execute(
                "SELECT view_name FROM duckdb_views() UNION SELECT table_name FROM duckdb_tables()"
            ).fetchall()
        }
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
    if ctes & built_in:
        # A WITH name is seen only inside its own query; outside it the same name would read a built-in table.
        raise SqlStepError(
            "SQL_TABLE_NOT_ALLOWED",
            f"A WITH table may not be called '{sorted(ctes & built_in)[0]}', the name of a built-in table.",
        )
    for node in _walk(tree):
        _check_node(node, allowed | ctes, as_of)


def group_by_column(sql: str) -> str | None:
    """The one column a query groups by, as DuckDB's own parser reads it, or none (no GROUP BY, more than one
    group, or a group that is not a plain column). Returned under the name it has in the result: a column
    renamed in the SELECT is given by its new name only when it is kept under its own name."""
    import duckdb

    con = _connect()
    try:
        tree = json.loads(con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    except (duckdb.Error, ValueError):
        return None
    finally:
        con.close()
    if tree.get("error"):
        return None
    try:
        node = tree["statements"][0]["node"]
        groups = node.get("group_expressions") or []
        if node.get("type") != "SELECT_NODE" or len(groups) != 1:
            return None
        group = groups[0]
        selected = node.get("select_list") or []
        if group.get("type") == "VALUE_CONSTANT":  # GROUP BY 1: the first column of the SELECT
            position = int(group["value"]["value"])
            group = selected[position - 1]
        if group.get("type") != "COLUMN_REF":
            return None
        column = str(group["column_names"][-1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    for item in selected:
        if (
            item.get("type") == "COLUMN_REF"
            and item["column_names"][-1] == column
            and item.get("alias") not in ("", column)
        ):
            return None  # kept under another name: the input and the result do not share it
    return column


def rows_aggregated(sql: str, tables: Mapping[str, pd.DataFrame], *, as_of: date | None = None) -> int | None:
    """How many rows a grouping query actually combines: the rows its FROM, JOIN and WHERE leave for the GROUP BY
    to fold (rows a WHERE drops are not among them). None when the query does not group, or cannot be counted.
    """
    import duckdb

    aliases = {name: table_alias(name) for name in tables}
    con = _connect()
    try:
        tree = json.loads(con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
        node = tree["statements"][0]["node"]
        if tree.get("error") or node.get("type") != "SELECT_NODE" or not node.get("group_expressions"):
            return None
        star = json.loads(con.execute("SELECT json_serialize_sql('SELECT count(*) FROM x')").fetchone()[0])
        node.update(
            select_list=star["statements"][0]["node"]["select_list"],
            group_expressions=[],
            group_sets=[],
            having=None,
            qualify=None,
            modifiers=[],
            aggregate_handling="STANDARD_HANDLING",
        )
        counting = con.execute("SELECT json_deserialize_sql(?)", [json.dumps(tree)]).fetchone()[0]
        for name, frame in tables.items():
            con.register(aliases[name], frame)
        _lock(con)
        uses_as_of = "as_of" in set(_PARAMETER.findall(_blank(counting)))
        if uses_as_of and as_of is None:
            return None
        return int(con.execute(counting, {"as_of": as_of} if uses_as_of else None).fetchone()[0])
    except (duckdb.Error, ValueError, KeyError, IndexError, TypeError):
        return None
    finally:
        con.close()


def _fixed_date_refusal(shown: str) -> SqlStepError:
    return SqlStepError(
        "SQL_FIXED_DATE",
        f"The query has a date typed into it ({shown}). Use $as_of instead, so next month's files are not "
        "cut at this month's date.",
    )


def _literal_date(text: str) -> date | None:
    """The day a date-only string literal names (`2026-01-31`, `2026-01-31 00:00:00`), else None."""
    day = text.strip()[:10]
    try:
        return date.fromisoformat(day)
    except ValueError:
        return None


def _check_dates(node: dict[str, Any], as_of: date | None) -> None:
    """A date typed into the query is refused unless it is the as-of date itself (G10)."""
    if node.get("class") == "CONSTANT":
        value = node.get("value") or {}
        kind = (value.get("type") or {}).get("id")
        text = value.get("value")
        typed = kind == "VARCHAR" and isinstance(text, str) and _DATE_TEXT.match(text)
        if typed and (as_of is None or _literal_date(str(text)) != as_of):
            raise _fixed_date_refusal(str(text).strip()[:19])
    elif node.get("class") == "FUNCTION" and str(node.get("function_name", "")).lower() in _MAKE_DATE:
        if node.get("children") and _fixed(node):
            raise _fixed_date_refusal(f"{node.get('function_name')} of fixed values")
    elif (
        node.get("class") == "CAST"
        and str((node.get("cast_type") or {}).get("id", "")).upper() in _DATE_TYPES
    ):
        child = node.get("child")
        # A single literal is the CONSTANT case above; a fixed expression (`'2026-' || '01-01'`) is refused here.
        if isinstance(child, dict) and child.get("class") != "CONSTANT" and _fixed(child):
            raise _fixed_date_refusal("a date built from fixed values")


def _fixed(node: dict[str, Any]) -> bool:
    """True when nothing under `node` reads a column, a parameter or a table: its value is typed into the query."""
    return not any(isinstance(n, dict) and n.get("class") in _NOT_FIXED for n in _walk(node))


def _check_node(node: dict[str, Any], allowed: set[str], as_of: date | None = None) -> None:
    _check_dates(node, as_of)
    if node.get("class") == "FUNCTION":
        refusal = _function_refusal(str(node.get("function_name", "")), len(node.get("children") or []))
        if refusal is not None:
            raise refusal
    kind = node.get("type")
    if kind == "COLUMN_REF":
        names = node.get("column_names") or []
        if len(names) == 1 and str(names[0]).lower() in _CLOCK_WORDS:
            raise _clock_refusal(str(names[0]))
    elif kind == "SHOW_REF" and not node.get("query"):
        raise SqlStepError(
            "SQL_TABLE_NOT_ALLOWED", "A query may read only the tables of this case, not the list of tables."
        )
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


_CONVERSION_HINTS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(r"invalid (date|timestamp) field format"),
        "a text value is not written as {0} YYYY-MM-DD; read it with strptime(column, '<its format>') first",
    ),
    (re.compile(r"Could not convert string .* to ([A-Z0-9]{2,12})\b"), "a text value cannot be read as {0}"),
)
"""What a conversion error was about, in our words: the type it wanted, never the cell DuckDB quotes."""


def _error_kind(exc: Exception) -> str:
    """Only the kind of a run error ("Conversion Error"): its text may repeat a cell, quoted any way or not at all.

    A conversion error also says what it could not read as (a date, INT32), from a closed list of patterns, so the
    model can fix its query; the value itself never leaves.
    """
    text = str(exc)
    kind = text.split(":", 1)[0].strip()
    if not re.fullmatch(r"[A-Za-z ]{1,40} Error", kind):
        return "an error"
    for pattern, hint in _CONVERSION_HINTS:
        found = pattern.search(text.splitlines()[0] if text else "")
        if found is not None:
            word = found.group(1)
            return f"{kind}: " + hint.format(word if word.isupper() else f"a {word}")
    return kind


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
                "SQL_FAILED", f"The query could not run on these tables ({_error_kind(exc)})."
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


# ---------------------------------------------------------------------------
# The as-of date as a rule, and the future-rows check
# ---------------------------------------------------------------------------
def _day_values(series: pd.Series[Any]) -> pd.Series[Any]:
    """A column as days, read only the one unambiguous way (year first); anything else is not a date here."""
    if pd.api.types.is_datetime64_any_dtype(series):
        stamps = pd.to_datetime(series, errors="coerce")
    elif pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
        return pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    else:
        text = series.dropna().astype(str).str.strip()
        stamps = pd.to_datetime(text.where(text.str.match(r"^\d{4}-\d{2}-\d{2}")), errors="coerce")
        stamps = stamps.reindex(series.index)
    if getattr(stamps.dt, "tz", None) is not None:
        stamps = stamps.dt.tz_localize(None)
    return stamps


def resolve_as_of_rule(rule: AsOfRule, series: pd.Series[Any]) -> date:
    """The date `rule` gives on this file's `series`: its latest date, or the last month-end the file covers in
    full (the latest date's own month-end when the latest date is one, else the month-end before it)."""
    days = _day_values(series).dropna()
    if days.empty:
        raise SqlStepError(
            "SQL_NEEDS_AS_OF",
            f"The as-of date is 'the {rule.rule.replace('_', ' ')}' of '{rule.column}', and that column has no "
            "dates written year first (2026-01-31).",
        )
    latest = days.max().normalize()
    if rule.rule == "latest_date_in_file":
        found: date = latest.date()
        return found
    month_end = latest + pd.offsets.MonthEnd(0)
    last_full = month_end if latest == month_end else latest - pd.offsets.MonthBegin(1) - pd.Timedelta(days=1)
    result: date = last_full.date()
    return result


def _event_column(frame: pd.DataFrame) -> str | None:
    """The column that dates each row of an event table: the first column that reads as dates on almost every row."""
    for name in frame.columns:
        if not re.search(r"date|time|_at$|_ts$|day|month", str(name), re.I):
            continue
        days = _day_values(frame[name])
        if len(frame) and days.notna().sum() / len(frame) >= 0.95:
            return str(name)
    return None


def future_rows_moved(
    sql: str,
    tables: Mapping[str, pd.DataFrame],
    *,
    output_name: str,
    as_of: date,
    ignore: Collection[str] = (),
    one_outcome: bool = False,
) -> int | None:
    """How many rows of the query's table change when every row dated after `as_of` is taken out of its inputs.

    A table made of what was known on the as-of date does not move: the rows that came later were never in it. Each
    input that has a date column (`_event_column`) is cut at `as_of`; the query runs again; the two tables are
    compared row by row, without the `ignore` columns (the outcome of a query that builds it from later rows). None
    when no input holds a row dated after `as_of` (nothing to test). Our code runs both queries, so the answer is
    not the writer's word.

    `one_outcome`: the outcome column is not decided yet, and a query that builds it from later rows has exactly one
    such column, so one column may move (compared by its values) and two or more are a leak.
    """
    cut: dict[str, pd.DataFrame] = {}
    later = False
    for name, frame in tables.items():
        column = _event_column(frame)
        if column is None:
            cut[name] = frame
            continue
        days = _day_values(frame[column])
        after = days.dt.normalize() > pd.Timestamp(as_of)
        later = later or bool(after.any())
        cut[name] = frame[~after.to_numpy()]
    if not later:
        return None
    whole = run_sql(sql, tables, output_name=output_name, as_of=as_of).frame
    part = run_sql(sql, cut, output_name=output_name, as_of=as_of).frame
    whole = whole.drop(columns=[c for c in whole.columns if c in ignore])
    part = part.drop(columns=[c for c in part.columns if c in ignore])
    if list(whole.columns) != list(part.columns):
        return len(whole)
    if one_outcome:
        moved = [c for c in whole.columns if Counter(whole[c].astype(str)) != Counter(part[c].astype(str))]
        return len(whole) if len(moved) > 1 else 0
    left = Counter(whole.astype(str).itertuples(index=False, name=None))
    right = Counter(part.astype(str).itertuples(index=False, name=None))
    return max(sum((left - right).values()), sum((right - left).values()))
