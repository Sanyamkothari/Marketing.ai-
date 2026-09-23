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
one account right does not reset its count against the others. An Admin resetting a user's password
or re-enabling the user clears that account's count and lock (`clear`, DEC-867): the new password
makes every earlier guess moot, and the 429 tells the person to ask for exactly that.

**One attempt per account at a time** (DEC-867). `attempt(account)` holds a mutex for that account
key while the route checks the lock, checks the password and records the outcome, so N parallel
wrong passwords for one account cannot all pass the lock check before any of them is counted: at
most `max_failures_per_account` of them are ever answered 401, the rest 429. The mutexes live in a
map that holds only keys somebody is currently inside, so it is bounded by the requests in flight,
not by the usernames ever typed. Addresses are not serialised: a client spraying many usernames in
parallel can overshoot its address limit by at most the number of requests it has in flight.

**The memory bound never lifts a lock** (DEC-867). Past `_MAX_TRACKED` keys per scope the entry
forgotten is the least recently failed one *that is not locked*; only when every tracked key is
locked is the oldest locked one dropped. A flood of distinct usernames therefore cannot push a
locked account out of the table and so unlock it.

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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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

    def is_locked(self, now: datetime) -> bool:
        return self.locked_until is not None and self.locked_until > now


@dataclass
class _KeyMutex:
    """The mutex of one account key and how many requests hold or wait for it."""

    mutex: threading.Lock = field(default_factory=threading.Lock)
    holders: int = 0


def _evict_one(table: dict[str, _Entry], now: datetime) -> None:
    """Forget the least recently failed key that is not locked; the oldest of all if every one is."""
    victim = next((key for key, entry in table.items() if not entry.is_locked(now)), None)
    table.pop(next(iter(table)) if victim is None else victim)


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
        self._account_mutexes: dict[str, _KeyMutex] = {}

    @contextmanager
    def attempt(self, account: str) -> Iterator[None]:
        """Serialise sign-in attempts on one account key: check, verify and record inside this block.

        Other accounts are not held up. The mutex is dropped from the map when its last holder
        leaves, so the map never holds more keys than there are attempts in flight.
        """
        with self._lock:
            slot = self._account_mutexes.get(account)
            if slot is None:
                slot = self._account_mutexes[account] = _KeyMutex()
            slot.holders += 1
        try:
            with slot.mutex:
                yield
        finally:
            with self._lock:
                slot.holders -= 1
                if slot.holders == 0:
                    del self._account_mutexes[account]

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
                    _evict_one(table, now)
        return tuple(started)

    def record_success(self, account: str) -> None:
        """A correct sign-in clears the account's failures. The address's count is kept on purpose."""
        self.clear(account)

    def clear(self, account: str) -> None:
        """Forget the account's failures and lift its lock (an Admin reset its password or re-enabled it).

        Addresses are untouched: an address lock is about the client, not about this account.
        """
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
