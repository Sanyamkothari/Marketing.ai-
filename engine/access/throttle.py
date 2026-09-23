"""Sign-in rate limiting (Plan D M54): a username or a client address that keeps failing is locked out.

`POST /auth/login` answers every wrong password with the same 401 (DEC-720), which stops the form
from confirming accounts but not from being asked a million times. `LoginThrottle` counts failed
sign-ins in a sliding window, separately **per account** (the username as typed, case-folded - so an
unknown username locks exactly like a known one and a lock-out confirms nothing) and **per client
address**, and locks whichever reaches its limit for `lockout_seconds` (DEC-861):

* per account stops a guess-the-password attack on one person from many addresses;
* per address stops one client spraying one password across many usernames.

While either is locked, a sign-in is refused with **429 `LOGIN_LOCKED`** *before* the password is
checked - checking it would keep answering the attacker's question - and with `Retry-After`. A
successful sign-in clears its account's failures; the address's are kept, so a sprayer that guesses
one account right does not reset its count against the others.

State is in memory, per process, like `AnonymousEventLimiter` (DEC-724). One API process per
deployment is what `scripts/entrypoint.sh` runs (`--workers 1`); with several, each counts on its
own and the effective limit is multiplied by their number - a weaker limit, never a lock-out of the
wrong person. A restart forgets every lock. Both are stated in `docs/PRODUCTION.md`.

The clock is injected (`clock`), so every rule is tested without sleeping. Nothing here logs a
username or an address.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal

from engine.utils.time import utc_now

__all__ = ["LockScope", "LoginLock", "LoginThrottle", "account_key"]

LockScope = Literal["account", "address"]

_MAX_TRACKED: Final[int] = 50_000
"""Keys kept per scope before the least recently failed are forgotten; bounds memory under a flood."""


def account_key(username: str) -> str:
    """The key a username is counted under: stripped and case-folded, as the user store compares it."""
    return username.strip().casefold()


def _scopes(account: str, address: str) -> tuple[tuple[LockScope, str], ...]:
    return (("account", account), ("address", address))


@dataclass(frozen=True)
class LoginLock:
    """A refusal: which scope is locked and for how many more whole seconds."""

    scope: LockScope
    retry_after_seconds: int


@dataclass
class _Entry:
    failures: deque[datetime] = field(default_factory=deque)
    locked_until: datetime | None = None


class LoginThrottle:
    """Failed sign-ins per account and per address in a sliding window, with a fixed lock-out."""

    def __init__(
        self,
        *,
        max_failures_per_account: int,
        max_failures_per_address: int,
        window_seconds: int,
        lockout_seconds: int,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if min(max_failures_per_account, max_failures_per_address, window_seconds, lockout_seconds) < 1:
            raise ValueError("every login limit must be at least 1")
        self._limits: dict[LockScope, int] = {
            "account": max_failures_per_account,
            "address": max_failures_per_address,
        }
        self._window = timedelta(seconds=window_seconds)
        self._lockout = timedelta(seconds=lockout_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[LockScope, dict[str, _Entry]] = {"account": {}, "address": {}}

    def locked(self, account: str, address: str) -> LoginLock | None:
        """The lock that refuses a sign-in for `account` from `address` now, or None.

        The account is reported first when both are locked: it is the one a person can do something
        about (wait, or ask an Admin), and the answer is the same 429 either way.
        """
        now = self._clock()
        with self._lock:
            for scope, key in _scopes(account, address):
                entry = self._entries[scope].get(key)
                if entry is not None and entry.locked_until is not None:
                    if entry.locked_until > now:
                        remaining = (entry.locked_until - now).total_seconds()
                        return LoginLock(scope=scope, retry_after_seconds=max(1, int(-(-remaining // 1))))
                    entry.locked_until = None
                    entry.failures.clear()
        return None

    def record_failure(self, account: str, address: str) -> tuple[LockScope, ...]:
        """Count one failed sign-in; the scopes this failure has just locked (usually none)."""
        now = self._clock()
        started: list[LockScope] = []
        with self._lock:
            for scope, key in _scopes(account, address):
                table = self._entries[scope]
                entry = table.pop(key, None) or _Entry()
                table[key] = entry  # re-inserted: the dict's order is least recently failed first
                if entry.locked_until is not None and entry.locked_until <= now:
                    entry.locked_until = None  # a lock that has run out starts a fresh count
                    entry.failures.clear()
                while entry.failures and entry.failures[0] <= now - self._window:
                    entry.failures.popleft()
                entry.failures.append(now)
                if entry.locked_until is None and len(entry.failures) >= self._limits[scope]:
                    entry.locked_until = now + self._lockout
                    started.append(scope)
                while len(table) > _MAX_TRACKED:
                    table.pop(next(iter(table)))
        return tuple(started)

    def record_success(self, account: str) -> None:
        """A correct sign-in clears the account's failures. The address's count is kept on purpose."""
        with self._lock:
            self._entries["account"].pop(account, None)

    def failures(self, scope: LockScope, key: str) -> int:
        """Failures counted for `key` within the window now (for tests and diagnostics)."""
        now = self._clock()
        with self._lock:
            entry = self._entries[scope].get(key)
            if entry is None:
                return 0
            return sum(1 for moment in entry.failures if moment > now - self._window)
