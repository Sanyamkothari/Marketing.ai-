"""Per-session rate limits (Plan K M133, DEC-1404 (g))."""

from __future__ import annotations

import pytest

from api.mcp.limits import RateLimiter
from engine.agentic.errors import AgenticError


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_calls_per_minute_slide_and_each_session_has_its_own_budget() -> None:
    clock = Clock()
    limiter = RateLimiter(calls_per_minute=3, concurrent_calls=10, clock=clock)
    for _ in range(3):
        limiter.acquire("a").release()
    with pytest.raises(AgenticError) as caught:
        limiter.acquire("a")
    assert caught.value.code == "MCP_RATE_LIMITED" and caught.value.status == 429
    assert caught.value.detail["retry_after_seconds"] >= 1
    limiter.acquire("b").release()
    clock.now += 60.0
    limiter.acquire("a").release()


def test_calls_running_at_once_are_capped_until_their_work_ends() -> None:
    limiter = RateLimiter(calls_per_minute=100, concurrent_calls=2, clock=Clock())
    first, second = limiter.acquire("a"), limiter.acquire("a")
    with pytest.raises(AgenticError) as caught:
        limiter.acquire("a")
    assert "still running" in caught.value.message
    first.release()
    first.release()  # idempotent: a lease counts once
    third = limiter.acquire("a")
    with pytest.raises(AgenticError):
        limiter.acquire("a")
    second.release()
    third.release()
    limiter.acquire("a").release()
