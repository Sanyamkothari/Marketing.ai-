"""How many calls to a client's systems run at once (Plan H M80; shared with Data Doctor by Plan O M236).

A test, a listing, a preview or an import talks to someone else's server with timeouts of up to a minute. At most
`MAX_CONCURRENT_CALLS` of them run at once (imports: `MAX_CONCURRENT_IMPORTS`), whoever asks: the connection
routes (`api/routes/connections.py`) and Data Doctor's `preview_connection` and `propose_import`
(`engine/doctor/tools_wrappers.py`) take a slot from the same gate, so the registry cannot run more live queries
against a client's database than the routes can. A call that finds no free slot is refused straight away, never
queued: the caller turns `ConnectionBusyError` into its own `429 CONNECTION_BUSY`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Final, TypeVar

__all__ = [
    "BUSY_MESSAGE",
    "CALLS",
    "IMPORTS",
    "MAX_CONCURRENT_CALLS",
    "MAX_CONCURRENT_IMPORTS",
    "RETRY_AFTER_SECONDS",
    "ConnectionBusyError",
    "bounded_call",
]

_T = TypeVar("_T")

MAX_CONCURRENT_CALLS: Final[int] = 4
MAX_CONCURRENT_IMPORTS: Final[int] = 2
CALLS: Final[threading.BoundedSemaphore] = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
"""Tests, listings and previews of saved connections, from any caller."""
IMPORTS: Final[threading.BoundedSemaphore] = threading.BoundedSemaphore(MAX_CONCURRENT_IMPORTS)
"""Imports from saved connections."""
BUSY_MESSAGE: Final[str] = "Other connection calls are running. Try again in a few seconds."
RETRY_AFTER_SECONDS: Final[int] = 5


class ConnectionBusyError(Exception):
    """Every slot of the gate is taken; the call was not made."""


def bounded_call(gate: threading.BoundedSemaphore, action: Callable[[], _T]) -> _T:
    """`action()` while holding one slot of `gate`; `ConnectionBusyError` at once when none is free."""
    if not gate.acquire(blocking=False):
        raise ConnectionBusyError(BUSY_MESSAGE)
    try:
        return action()
    finally:
        gate.release()
