"""History through Shopify bulk operations: start an export, come back while it runs, page the JSONL.

A history window (a month of orders, a year of customers, every product) is one bulk operation.
Nothing here holds a worker or loads the file into memory:

1. **Start.** `bulkOperationRunQuery(query:, groupObjects: true)` with the window's search filter.
   The page returned has no records, `pending_until` and the operation in the cursor, so the engine
   saves it and comes back then. A shop already running its limit of exports
   (`OPERATION_IN_PROGRESS`, `LIMIT_REACHED`) is asked again a minute later.
2. **Poll.** `bulkOperation(id:)` until `COMPLETED`; the next poll is due after a quarter of the time
   the export has run so far (15 s to 2 min), so a quick export is read quickly and a slow one is not
   polled every few seconds.
3. **Page.** The file is read from the byte offset in the cursor with a `Range` request
   (`ConnectorHttp.download_lines_from`), `PAGE_RECORDS` top-level objects at a time. A nested
   connection's children (an order's line items) are separate lines with `__parentId` that follow
   their parent (`groupObjects: true`), so a page ends just before the next top-level line and the
   offset of that line goes in the cursor: memory is bounded by one page, and no byte is read twice.
   The download link is fetched again for every page (it is signed and expires); a link that has
   expired, or an operation Shopify no longer has, restarts the window (`CursorExpired`; stable ids
   make the repeat free).
4. **Fail well.** A `FAILED`, `CANCELED` or `EXPIRED` operation is started again, up to
   `MAX_FAILURES` times in a row (the count travels in the cursor); then the read raises
   `TransientFailure`, so the history lane's error streak (and its one alert) shows it, and after
   `RETRY_AFTER_GIVING_UP` the export is tried afresh. `ACCESS_DENIED` is `AccessDenied`.

Cursor grammar: `bulk:new~<failures>` (start one), `bulk:<operation gid>@<byte offset>~<failures>`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from autonomy.connectors.base import (
    AccessDenied,
    ConnectionContext,
    CursorExpired,
    RemoteError,
    StreamPage,
    SyncWindow,
    TransientFailure,
)
from autonomy.connectors.http import idempotency_key
from autonomy.connectors.shopify import gql
from autonomy.connectors.shopify.api import BULK_START, BULK_STATUS, bulk_query
from autonomy.connectors.shopify.normalize import parse_time

__all__ = [
    "BULK_PREFIX",
    "MAX_FAILURES",
    "PAGE_RECORDS",
    "BulkCursor",
    "parse_cursor",
    "read_bulk",
    "search_filter",
]

BULK_PREFIX: Final[str] = "bulk:"
PAGE_RECORDS: Final[int] = 500
MAX_FAILURES: Final[int] = 3
RETRY_AFTER_GIVING_UP: Final[timedelta] = timedelta(hours=6)
BUSY_WAIT: Final[timedelta] = timedelta(minutes=1)
MIN_POLL: Final[timedelta] = timedelta(seconds=15)
MAX_POLL: Final[timedelta] = timedelta(minutes=2)
RUNNING: Final[frozenset[str]] = frozenset({"CREATED", "RUNNING", "CANCELING"})
DEAD: Final[frozenset[str]] = frozenset({"FAILED", "CANCELED", "EXPIRED"})
BUSY_CODES: Final[frozenset[str]] = frozenset({"OPERATION_IN_PROGRESS", "LIMIT_REACHED"})
GONE_STATUS: Final[frozenset[int]] = frozenset({400, 401, 403, 404, 410})
CHILD_FIELDS: Final[Mapping[str, str]] = {"LineItem": "lineItems"}
"""gid type of a child line -> the parent's connection field it belongs to."""

Normalizer = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class BulkCursor:
    operation: str | None
    """None: no export running yet; start one."""
    offset: int = 0
    failures: int = 0

    def text(self) -> str:
        if self.operation is None:
            return f"{BULK_PREFIX}new~{self.failures}"
        return f"{BULK_PREFIX}{self.operation}@{self.offset}~{self.failures}"


def parse_cursor(cursor: str) -> BulkCursor:
    body = cursor[len(BULK_PREFIX) :] if cursor.startswith(BULK_PREFIX) else cursor
    head, _, tail = body.rpartition("~")
    if not head:
        head, tail = body, "0"
    failures = int(tail) if tail.isdigit() else 0
    if head == "new":
        return BulkCursor(None, 0, failures)
    operation, _, offset = head.rpartition("@")
    if not operation or not offset.isdigit():
        raise CursorExpired("the saved export position is unreadable")
    return BulkCursor(operation, int(offset), failures)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def search_filter(window: SyncWindow) -> str:
    """Shopify search syntax for `start <= updated_at < end` (quoted ISO times, UTC)."""
    upper = f"updated_at:<'{iso(window.end)}'"
    if window.start is None:
        return upper
    return f"updated_at:>='{iso(window.start)}' AND {upper}"


def read_bulk(
    ctx: ConnectionContext,
    stream: str,
    window: SyncWindow,
    cursor: str | None,
    normalize: Normalizer,
    *,
    page_records: int = PAGE_RECORDS,
) -> StreamPage:
    state = parse_cursor(cursor) if cursor is not None else BulkCursor(None)
    if state.operation is None:
        return _start(ctx, stream, window, state.failures)
    operation = _status(ctx, state.operation)
    if operation is None:
        raise CursorExpired("Shopify no longer has the export; starting it again")
    status = str(operation.get("status") or "").upper()
    if status in RUNNING:
        return StreamPage(next_cursor=state.text(), pending_until=ctx.now + _poll_after(operation, ctx.now))
    if status in DEAD:
        return _failed(ctx, stream, operation, state)
    if status != "COMPLETED":
        raise RemoteError(f"Shopify's {stream} export is in an unknown state")
    url = operation.get("url")
    if not isinstance(url, str) or not url:
        return StreamPage()  # completed with no results: Shopify gives no file
    return _page(ctx, url, state, normalize, page_records)


def _start(ctx: ConnectionContext, stream: str, window: SyncWindow, failures: int) -> StreamPage:
    text = bulk_query(stream, ctx.connection.config).replace("{query}", json.dumps(search_filter(window)))
    # Retried after a timeout under a key: at worst a second export starts, which the limit allows
    # and which is never read (the cursor names one operation).
    key = idempotency_key(
        "shopify-bulk", ctx.connection.connection_id, stream, search_filter(window), failures
    )
    data = gql.mutate(ctx, BULK_START, {"query": text}, idempotency_key=key)
    payload = data.get("bulkOperationRunQuery")
    errors = gql.user_errors(payload)
    if errors:
        codes = {str(e.get("code") or "").upper() for e in errors}
        if codes & BUSY_CODES:
            waiting = BulkCursor(None, 0, failures)
            return StreamPage(next_cursor=waiting.text(), pending_until=ctx.now + BUSY_WAIT)
        raise RemoteError(
            f"Shopify refused the {stream} export ({', '.join(sorted(c or 'ERROR' for c in codes))})"
        )
    operation = payload.get("bulkOperation") if isinstance(payload, Mapping) else None
    op_id = operation.get("id") if isinstance(operation, Mapping) else None
    if not isinstance(op_id, str) or not op_id:
        raise RemoteError(f"Shopify accepted the {stream} export without an operation id")
    started = BulkCursor(op_id, 0, failures)
    return StreamPage(next_cursor=started.text(), pending_until=ctx.now + MIN_POLL)


def _status(ctx: ConnectionContext, operation: str) -> Mapping[str, Any] | None:
    found = gql.query(ctx, BULK_STATUS, {"id": operation}).get("bulkOperation")
    return found if isinstance(found, Mapping) else None


def _poll_after(operation: Mapping[str, Any], now: datetime) -> timedelta:
    created = parse_time(operation.get("createdAt"))
    elapsed = now - created if created is not None else timedelta(0)
    return min(MAX_POLL, max(MIN_POLL, elapsed / 4))


def _failed(
    ctx: ConnectionContext, stream: str, operation: Mapping[str, Any], state: BulkCursor
) -> StreamPage:
    code = str(operation.get("errorCode") or operation.get("status") or "FAILED").upper()
    if code == "ACCESS_DENIED":
        raise AccessDenied(
            f"Shopify refused the {stream} export (ACCESS_DENIED): reconnect to grant the scopes"
        )
    failures = state.failures + 1
    if failures < MAX_FAILURES:
        return StreamPage(next_cursor=BulkCursor(None, 0, failures).text())
    ended = parse_time(operation.get("completedAt")) or parse_time(operation.get("createdAt"))
    if ended is not None and ctx.now - ended >= RETRY_AFTER_GIVING_UP:
        return StreamPage(next_cursor=BulkCursor(None, 0, 0).text())
    raise TransientFailure(f"Shopify's {stream} export failed {failures} times in a row ({code})")


def _page(
    ctx: ConnectionContext, url: str, state: BulkCursor, normalize: Normalizer, limit: int
) -> StreamPage:
    records: list[Mapping[str, Any]] = []
    current: dict[str, Any] | None = None
    children: dict[str, list[Any]] = {}
    before = state.offset  # the byte offset where the line being read starts
    resume: int | None = None
    lines = ctx.http.download_lines_from(url, start=state.offset)
    try:
        for raw, after in lines:
            try:
                obj = json.loads(raw)
            except ValueError:
                before = after
                continue  # a torn or garbled line is skipped, never fatal
            if not isinstance(obj, dict):
                before = after
                continue
            parent = obj.get("__parentId")
            if parent is not None:
                if current is not None and parent == current.get("id"):
                    children.setdefault(_child_field(obj), []).append(obj)
                before = after
                continue
            if current is not None:
                records.append(normalize(_assemble(current, children)))
                if len(records) >= limit:
                    resume = before
                    break
            current, children = obj, {}
            before = after
    except RemoteError as exc:
        if exc.status in GONE_STATUS:
            raise CursorExpired("the export's download link expired; starting it again") from exc
        raise
    finally:
        close = getattr(lines, "close", None)
        if callable(close):
            close()  # stops the download: the rest of the file is read by the next page
    if resume is None and current is not None:
        records.append(normalize(_assemble(current, children)))
    if resume is None:
        return StreamPage(records=tuple(records))
    nxt = BulkCursor(state.operation, resume, state.failures)
    return StreamPage(records=tuple(records), next_cursor=nxt.text())


def _child_field(obj: Mapping[str, Any]) -> str:
    kind = str(obj.get("id") or "").removeprefix("gid://shopify/").split("/", 1)[0]
    return CHILD_FIELDS.get(kind, "children")


def _assemble(parent: dict[str, Any], children: Mapping[str, list[Any]]) -> dict[str, Any]:
    out = dict(parent)
    for field_name, nodes in children.items():
        out[field_name] = {"nodes": nodes}
    return out
