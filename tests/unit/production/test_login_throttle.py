"""Sign-in rate limiting (Plan D M54, DEC-861): the counting rules, on a controlled clock."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from engine.access import throttle as throttle_module
from engine.access.throttle import LoginLock, LoginThrottle, account_key


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def throttle(
    clock: Clock, *, account: int = 3, address: int = 5, window: int = 60, lockout: int = 300
) -> LoginThrottle:
    return LoginThrottle(
        max_failures_per_account=account,
        max_failures_per_address=address,
        window_seconds=window,
        lockout_seconds=lockout,
        clock=clock,
    )


def test_the_account_key_is_the_case_folded_username() -> None:
    assert account_key("  Asha ") == account_key("ASHA") == "asha"


def test_an_account_locks_at_its_limit_and_says_for_how_long(clock: Clock) -> None:
    limiter = throttle(clock)
    assert limiter.record_failure("asha", "10.0.0.1") == ()
    assert limiter.record_failure("asha", "10.0.0.2") == ()
    assert limiter.locked("asha", "10.0.0.3") is None
    assert limiter.record_failure("asha", "10.0.0.3") == ("account",)
    assert limiter.locked("asha", "10.0.0.9") == LoginLock(scope="account", retry_after_seconds=300)
    clock.advance(299.5)
    assert limiter.locked("asha", "10.0.0.9") == LoginLock(scope="account", retry_after_seconds=1)
    assert limiter.locked("bob", "10.0.0.9") is None, "another account from the same address is not locked"


def test_a_lock_ends_after_the_lockout_and_the_count_starts_again(clock: Clock) -> None:
    limiter = throttle(clock)
    for _ in range(3):
        limiter.record_failure("asha", "a")
    clock.advance(300)
    assert limiter.locked("asha", "a") is None
    assert limiter.failures("account", "asha") == 0
    assert (
        limiter.record_failure("asha", "a") == ()
    ), "one failure after a lock is one failure, not a new lock"


def test_a_lock_that_ran_out_unobserved_still_locks_again(clock: Clock) -> None:
    limiter = throttle(clock, address=100, window=10_000)
    for _ in range(3):
        limiter.record_failure("asha", "a")
    clock.advance(301)  # nobody called locked() in between
    assert [limiter.record_failure("asha", "a") for _ in range(3)] == [(), (), ("account",)]


def test_failures_outside_the_window_do_not_count(clock: Clock) -> None:
    limiter = throttle(clock, window=60)
    limiter.record_failure("asha", "a")
    limiter.record_failure("asha", "a")
    clock.advance(61)
    assert limiter.failures("account", "asha") == 0
    assert limiter.record_failure("asha", "a") == ()
    assert limiter.locked("asha", "a") is None


def test_an_address_spraying_many_accounts_locks(clock: Clock) -> None:
    limiter = throttle(clock, account=100, address=5)
    started = [limiter.record_failure(f"user{i}", "203.0.113.7") for i in range(5)]
    assert started[-1] == ("address",) and all(item == () for item in started[:-1])
    assert limiter.locked("somebody-else", "203.0.113.7") == LoginLock(
        scope="address", retry_after_seconds=300
    )
    assert limiter.locked("somebody-else", "203.0.113.8") is None


def test_one_failure_can_lock_both_scopes(clock: Clock) -> None:
    limiter = throttle(clock, account=2, address=2)
    limiter.record_failure("asha", "a")
    assert limiter.record_failure("asha", "a") == ("account", "address")
    lock = limiter.locked("asha", "a")
    assert lock is not None and lock.scope == "account", "the account is reported first"


def test_a_success_clears_the_account_but_not_the_address(clock: Clock) -> None:
    limiter = throttle(clock, account=3, address=3)
    limiter.record_failure("asha", "a")
    limiter.record_failure("asha", "a")
    limiter.record_success("asha")
    assert limiter.failures("account", "asha") == 0
    assert limiter.failures("address", "a") == 2
    assert limiter.record_failure("bob", "a") == ("address",)


@pytest.mark.parametrize("field", ["account", "address", "window", "lockout"])
def test_a_limit_below_one_is_refused(clock: Clock, field: str) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        throttle(clock, **{field: 0})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DEC-867: the memory bound never lifts a lock; clear(); one attempt per account at a time
# ---------------------------------------------------------------------------
def test_a_flood_of_new_keys_does_not_push_out_a_locked_account(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(throttle_module, "_MAX_TRACKED", 3)
    limiter = throttle(clock, account=2, address=10_000)
    limiter.record_failure("asha", "a")
    assert limiter.record_failure("asha", "a") == ("account",)
    for index in range(20):  # asha is now the least recently failed key of all
        limiter.record_failure(f"flood{index}", "a")
    lock = limiter.locked("asha", "b")
    assert lock is not None and lock.scope == "account", "the flood lifted the lock"
    assert limiter.failures("account", "flood0") == 0, "unlocked keys were the ones forgotten"


def test_when_every_tracked_key_is_locked_the_oldest_goes(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(throttle_module, "_MAX_TRACKED", 2)
    limiter = throttle(clock, account=1, address=10_000)
    for name in ("first", "second", "third"):
        assert limiter.record_failure(name, "a") == ("account",)
    assert limiter.locked("first", "b") is None
    assert limiter.locked("second", "b") is not None and limiter.locked("third", "b") is not None


def test_an_expired_lock_does_not_protect_its_entry(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(throttle_module, "_MAX_TRACKED", 2)
    limiter = throttle(clock, account=1, address=10_000, lockout=10)
    limiter.record_failure("old", "a")
    clock.advance(11)
    limiter.record_failure("locked", "a")
    limiter.record_failure("new", "a")
    assert limiter.failures("account", "old") == 0
    assert limiter.locked("locked", "b") is not None


def test_clear_lifts_the_account_lock_but_not_the_address(clock: Clock) -> None:
    limiter = throttle(clock, account=2, address=2)
    limiter.record_failure("asha", "a")
    assert limiter.record_failure("asha", "a") == ("account", "address")
    limiter.clear("asha")
    assert limiter.failures("account", "asha") == 0
    assert limiter.locked("asha", "b") is None
    lock = limiter.locked("asha", "a")
    assert lock is not None and lock.scope == "address"


def test_parallel_attempts_on_one_account_never_exceed_its_limit(clock: Clock) -> None:
    """Without the per-account mutex every thread passes `locked` before any failure is counted."""
    limiter = throttle(clock, account=3, address=10_000)
    threads_count = 12
    barrier = threading.Barrier(threads_count)
    outcomes: list[str] = []
    record = threading.Lock()

    def one_attempt(index: int) -> None:
        barrier.wait()
        with limiter.attempt("asha"):
            if limiter.locked("asha", f"10.0.0.{index}") is not None:
                result = "429"
            else:
                time.sleep(0.02)  # the password check: the window the race needs
                limiter.record_failure("asha", f"10.0.0.{index}")
                result = "401"
        with record:
            outcomes.append(result)

    workers = [threading.Thread(target=one_attempt, args=(i,)) for i in range(threads_count)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sorted(outcomes) == ["401"] * 3 + ["429"] * (threads_count - 3)
    assert limiter._account_mutexes == {}, "a mutex outlived its last holder"


def test_attempts_on_different_accounts_do_not_wait_for_each_other(clock: Clock) -> None:
    limiter = throttle(clock)
    inside = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with limiter.attempt("asha"):
            inside.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert inside.wait(5)
    entered = threading.Event()

    def other() -> None:
        with limiter.attempt("bob"):
            entered.set()

    thread = threading.Thread(target=other)
    thread.start()
    assert entered.wait(5), "bob waited on asha's mutex"
    release.set()
    holder.join()
    thread.join()
