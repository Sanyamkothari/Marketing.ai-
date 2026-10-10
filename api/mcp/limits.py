"""Per-session rate limits (Plan K M133, DEC-1404 (g); design §7.3, "exfiltration by aggregation", "denial of
service").

Counted per signed-in session - the bearer token, kept as its sha256 only (`api.mcp.scopes.token_key`):

* at most `calls_per_minute` tool calls in any 60 seconds (a sliding window of call times);
* at most `concurrent_calls` calls running at once. A call stays counted until its work has finished, even
  when the caller was already answered `MCP_TIMEOUT` (the synchronous fallback cannot stop a thread), so a
  caller cannot pile up abandoned work.

Over either, the call is refused with `MCP_RATE_LIMITED` (429) before anything runs. The counters live in the
API process (one process, as Plan G's session lock; a deployment with several API processes divides the
budget among them - M141 moves it to the platform database if a client needs one budget).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from engine.agentic.errors import AgenticError

__all__ = ["WINDOW_SECONDS", "Lease", "RateLimiter"]

WINDOW_SECONDS: Final[float] = 60.0
_MAX_KEYS: Final[int] = 10_000


@dataclass
class Lease:
    """One admitted call; `release` when its work has finished (idempotent)."""

    _release: Callable[[], None]
    _done: bool = False

    def release(self) -> None:
        if not self._done:
            self._done = True
            self._release()


@dataclass
class RateLimiter:
    calls_per_minute: int
    concurrent_calls: int
    clock: Callable[[], float] = time.monotonic
    _calls: dict[str, deque[float]] = field(default_factory=dict, init=False)
    _running: dict[str, int] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def acquire(self, key: str) -> Lease:
        """Admit one call for `key`, or refuse it with `MCP_RATE_LIMITED`."""
        now = self.clock()
        with self._lock:
            times = self._calls.setdefault(key, deque())
            while times and now - times[0] >= WINDOW_SECONDS:
                times.popleft()
            if len(times) >= self.calls_per_minute:
                raise AgenticError(
                    "MCP_RATE_LIMITED",
                    f"More than {self.calls_per_minute} calls in a minute from this sign-in; wait and call again.",
                    status=429,
                    detail={"retry_after_seconds": max(1, int(WINDOW_SECONDS - (now - times[0])) + 1)},
                )
            if self._running.get(key, 0) >= self.concurrent_calls:
                raise AgenticError(
                    "MCP_RATE_LIMITED",
                    f"{self.concurrent_calls} calls from this sign-in are still running; wait for one to finish.",
                    status=429,
                )
            times.append(now)
            self._running[key] = self._running.get(key, 0) + 1
            if len(self._calls) > _MAX_KEYS:  # forget the stalest idle sessions; never one still running
                for stale in [k for k, v in self._calls.items() if not self._running.get(k) and k != key][
                    : len(self._calls) - _MAX_KEYS
                ]:
                    del self._calls[stale]
        return Lease(lambda: self._finish(key))

    def _finish(self, key: str) -> None:
        with self._lock:
            left = self._running.get(key, 0) - 1
            if left > 0:
                self._running[key] = left
            else:
                self._running.pop(key, None)
