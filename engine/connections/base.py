"""What every connector is: a form, a test, a browser, a preview and a bounded fetch (Plan H M80).

A *connector* knows one kind of service - Amazon S3, PostgreSQL, Snowflake - and nothing else: not
where connections are stored, not how secrets are encrypted, not what an upload is. It is handed a
connection's non-secret settings (`config`) and its decrypted secrets (`secrets`) for each call, and
keeps no state between calls, so a connector object is a catalogue entry that can be shared freely.

Four promises every connector keeps, because the platform's own promises depend on them (§6):

* **It only reads.** A connector never writes to, creates or deletes anything in a client's system.
  Where the service allows it the session itself is made read-only (a database's
  `default_transaction_read_only`), and the test says whether the credentials could write.
* **Every network call has a timeout.** Connecting takes at most `CONNECT_TIMEOUT_S`; a read at most
  `READ_TIMEOUT_S`. A connection that hangs is a failed step with a fix, never a stuck request.
* **Everything it returns is bounded.** A listing stops at `MAX_BROWSE_ITEMS`, a preview at
  `PREVIEW_ROWS` rows with every cell masked (`engine.pii.redact_cells`), a fetch at the use case's
  `max_file_size_mb`.
* **No secret leaves it.** A message or a fix never quotes a secret, and an exception from a driver is
  turned into plain words rather than repeated (a driver's message can carry the connection string).
"""

from __future__ import annotations

import importlib
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from types import ModuleType
from typing import Any, BinaryIO, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from engine.pii import redact_cells

__all__ = [
    "CONNECT_TIMEOUT_S",
    "MAX_BROWSE_ITEMS",
    "PREVIEW_ROWS",
    "READ_TIMEOUT_S",
    "BrowseItem",
    "BrowseResult",
    "ConfigValue",
    "Connector",
    "ConnectorError",
    "FetchResult",
    "FieldSpec",
    "FileFormat",
    "KindInfo",
    "LimitedWriter",
    "Preview",
    "Selection",
    "StepName",
    "StepStatus",
    "TestReport",
    "TestStep",
    "addon_missing",
    "frame_preview",
    "load_sdk",
    "plain_error",
    "report",
]

CONNECT_TIMEOUT_S: Final[int] = 10
"""The longest any connector waits to open a connection or sign in."""

READ_TIMEOUT_S: Final[int] = 60
"""The longest any single read (a listing, a query, a chunk of a download) may take."""

MAX_BROWSE_ITEMS: Final[int] = 500
"""A listing stops here and says it was cut short; a person picks from a list, not a catalogue."""

PREVIEW_ROWS: Final[int] = 20
"""A preview shows at most this many rows."""

PREVIEW_MAX_BYTES: Final[int] = 256 * 1024
"""How much of a CSV object a preview downloads: plenty for twenty rows."""

PREVIEW_MAX_PARQUET_BYTES: Final[int] = 64 * 1024 * 1024
"""A Parquet file has its index at the end, so a preview reads the whole file - up to this size."""

FETCH_BATCH_ROWS: Final[int] = 10_000
"""Rows a database fetch reads, converts and writes at a time."""

FileFormat = Literal["csv", "parquet"]
ConfigValue = str | int | bool | None


class StepName(StrEnum):
    """The named steps of a connection test, in the order they run."""

    REACH = "reach"
    SIGN_IN = "sign_in"
    LIST = "list"
    READ_SAMPLE = "read_sample"
    READ_ONLY_CHECK = "read_only_check"


STEP_LABELS: Final[Mapping[StepName, str]] = {
    StepName.REACH: "Reach the service",
    StepName.SIGN_IN: "Sign in",
    StepName.LIST: "List what is there",
    StepName.READ_SAMPLE: "Read a sample",
    StepName.READ_ONLY_CHECK: "Check it is read-only",
}


class StepStatus(StrEnum):
    """`warning` is a step that passed with a caution: the connection works, but could be safer."""

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    WARNING = "warning"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldSpec(_Frozen):
    """One input of a connection's set-up form."""

    name: str = Field(description="The key the value is stored under.")
    label: str = Field(description="The form label, in plain words.")
    type: Literal["text", "password", "number", "select", "checkbox", "textarea"] = "text"
    required: bool = False
    secret: bool = Field(default=False, description="Stored encrypted and never sent back to a browser.")
    advanced: bool = Field(default=False, description="Drawn under “More options”, closed by default.")
    default: ConfigValue = None
    placeholder: str | None = None
    help: str | None = Field(default=None, description="One sentence under the input.")
    options: tuple[str, ...] = Field(default=(), description="The choices of a `select`.")


class KindInfo(_Frozen):
    """One card of the Connections catalogue (`GET /connections/kinds`)."""

    kind: str
    label: str
    description: str
    group: Literal["store", "database", "ai"]
    tier: Literal["built_in", "optional"]
    available: bool = Field(description="False when the service's add-on (its Python SDK) is not installed.")
    addon: str | None = Field(default=None, description="The optional extra that installs the add-on.")
    fields: tuple[FieldSpec, ...] = ()
    creatable: bool = Field(default=True, description="False for a service set up on a screen of its own.")
    screen: str | None = Field(
        default=None, description="That screen's address, e.g. `#/generative/connection`."
    )


class TestStep(_Frozen):
    """One named step of a connection test."""

    __test__ = False  # not a pytest class, whatever its name says

    name: StepName
    label: str
    status: StepStatus
    message: str = Field(description="What happened, in plain words.")
    fix: str | None = Field(default=None, description="What to do about a failure or a caution.")


class TestReport(_Frozen):
    """The result of `POST /connections/{id}/test`."""

    __test__ = False

    ok: bool = Field(description="True when no step failed.")
    steps: tuple[TestStep, ...]
    tested_at: datetime


class Selection(_Frozen):
    """What to preview or import: an object `path` in a store, or a `schema` and `table` in a database."""

    path: str | None = Field(default=None, max_length=1024, description="A store's object key.")
    schema_name: str | None = Field(
        default=None, max_length=256, description="A database's schema (or dataset)."
    )
    table: str | None = Field(default=None, max_length=256, description="A database's table or view.")


class BrowseItem(_Frozen):
    """One entry of a listing: a folder or schema to open, or a file or table to pick."""

    name: str
    kind: Literal["folder", "schema", "table", "file"]
    path: str | None = Field(default=None, description="What to pass as `path` to open it or pick it.")
    schema_name: str | None = None
    table: str | None = None
    size_bytes: int | None = None
    importable: bool = Field(description="True for a CSV or Parquet file, or a table.")


class BrowseResult(_Frozen):
    path: str = Field(description="Where this listing is: a prefix, a schema, or empty for the top.")
    parent: str | None = Field(default=None, description="The `path` one level up; null at the top.")
    items: tuple[BrowseItem, ...]
    truncated: bool = Field(description=f"True when the listing stopped at {MAX_BROWSE_ITEMS} items.")


class Preview(_Frozen):
    """The first rows of a file or table, every cell masked."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    note: str | None = None


class FetchResult(_Frozen):
    file_format: FileFormat
    file_name: str
    size_bytes: int
    rows: int | None = None


class ConnectorError(Exception):
    """A connector could not do what was asked. `message` and `fix` are plain words, never a secret."""

    def __init__(self, code: str, message: str, fix: str | None = None, *, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.status = status


@runtime_checkable
class Connector(Protocol):
    """One kind of service. Stateless: every call is handed the connection's settings and secrets."""

    kind: str
    label: str

    def info(self) -> KindInfo: ...

    def available(self) -> bool: ...

    def test(self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str]) -> TestReport: ...

    def browse(
        self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], path: str
    ) -> BrowseResult: ...

    def preview(
        self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], selection: Selection
    ) -> Preview: ...

    def file_format(self, selection: Selection) -> FileFormat: ...

    def fetch(
        self,
        config: Mapping[str, ConfigValue],
        secrets: Mapping[str, str],
        selection: Selection,
        sink: BinaryIO,
        *,
        limit_bytes: int,
        max_rows: int,
    ) -> FetchResult: ...


# ---------------------------------------------------------------------------
# Helpers every connector shares
# ---------------------------------------------------------------------------
def report(steps: Sequence[TestStep], now: datetime) -> TestReport:
    return TestReport(
        ok=not any(s.status is StepStatus.FAILED for s in steps), steps=tuple(steps), tested_at=now
    )


def step(name: StepName, status: StepStatus, message: str, fix: str | None = None) -> TestStep:
    return TestStep(name=name, label=STEP_LABELS[name], status=status, message=message, fix=fix)


def skipped_after(names: Iterable[StepName], why: str = "Skipped: an earlier step failed.") -> list[TestStep]:
    return [step(name, StepStatus.SKIPPED, why) for name in names]


def remaining(after: StepName) -> list[StepName]:
    order = list(StepName)
    return order[order.index(after) + 1 :]


def load_sdk(module: str) -> ModuleType | None:
    """The optional SDK `module`, or None when it is not installed (a lazy import, never at start-up)."""
    try:
        return importlib.import_module(module)
    except ImportError:
        return None


def addon_missing(label: str, extra: str) -> ConnectorError:
    return ConnectorError(
        "CONNECTION_NEEDS_ADDON",
        f"{label} needs an add-on that is not installed on this server.",
        f"Ask whoever runs Marketing AI to install it: pip install 'marketing-ai[{extra}]'.",
        status=409,
    )


_SECRETISH: Final[re.Pattern[str]] = re.compile(
    r"(password|pwd|secret|token|key)\s*[=:]\s*\S+", re.IGNORECASE
)


def plain_error(exc: BaseException, limit: int = 200) -> str:
    """A driver's error as one short line, with anything shaped like `password=...` removed.

    Used only as the *detail* after a plain sentence; drivers put hosts and database names in their
    messages, which help, and occasionally a connection string, which must not be repeated.
    """
    text = " ".join(str(exc).split())
    text = _SECRETISH.sub(r"\1=***", text)
    return text[:limit] + ("…" if len(text) > limit else "")


def frame_preview(frame: Any, note: str | None = None) -> Preview:
    """At most `PREVIEW_ROWS` rows of a pandas frame, each cell as text with personal data masked."""
    head = frame.head(PREVIEW_ROWS)
    columns = tuple(str(c) for c in head.columns)
    rows = tuple(
        redact_cells("" if _missing(value) else str(value) for value in record)
        for record in head.itertuples(index=False, name=None)
    )
    return Preview(columns=columns, rows=rows, note=note)


def _missing(value: Any) -> bool:
    try:
        return bool(value is None or value != value)  # NaN is the only value unequal to itself
    except (TypeError, ValueError):
        return False


class LimitedWriter(io.RawIOBase):
    """A write-only wrapper that counts bytes and refuses to go past `limit_bytes`."""

    def __init__(self, sink: BinaryIO, limit_bytes: int, what: str) -> None:
        super().__init__()
        self._sink = sink
        self._limit = limit_bytes
        self._what = what
        self.written = 0

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        size = len(data)
        if self.written + size > self._limit:
            raise too_large(self._what, self._limit)
        self._sink.write(bytes(data))
        self.written += size
        return size


def too_large(what: str, limit_bytes: int) -> ConnectorError:
    megabytes = max(1, limit_bytes // (1024 * 1024))
    return ConnectorError(
        "CONNECTION_TOO_LARGE",
        f"{what} is larger than the {megabytes} MB this use case accepts.",
        "Pick a smaller file or table, or ask for an extract with only the rows and columns you need.",
        status=413,
    )


def text_value(config: Mapping[str, ConfigValue], name: str) -> str:
    value = config.get(name)
    return "" if value is None else str(value).strip()


def int_value(config: Mapping[str, ConfigValue], name: str, default: int) -> int:
    value = config.get(name)
    if value is None or value == "":
        return default
    return int(value)


def bool_value(config: Mapping[str, ConfigValue], name: str, default: bool = False) -> bool:
    value = config.get(name)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
