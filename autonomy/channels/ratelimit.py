"""Deterministic token buckets: per (provider, channel), and per recipient.

Why. Providers throttle us (an SMS aggregator's transactions per second, SES's sending rate, a
WhatsApp tier), and a burst past their limit turns into errors that look like delivery problems
and trip circuit breakers for no good reason. Recipients need a limit of their own: a bug upstream
that decides "text Priya" ten times in a minute must not reach her phone ten times.

How. A bucket holds up to `capacity` tokens and refills continuously at `capacity / per`. A send
takes one token. When the bucket is empty, `wait()` says exactly how long until the next token,
so the gateway can queue the message for then instead of failing it.

Time is always passed in (`now`) and nothing here reads the clock, so a test or the Gym can replay
an hour of traffic in microseconds and get the same answer every time. A clock that steps
backwards (two servers, a test going back) refills nothing rather than granting free tokens.
"""

from __future__ import annotations

import threading
from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from autonomy.contracts import Channel

__all__ = ["Rate", "RateDecision", "RateLimiter", "TokenBucket"]


@dataclass(frozen=True)
class Rate:
    """`capacity` sends per `per`, with bursts of up to `capacity` at once."""

    capacity: float
    per: timedelta

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("a rate needs a positive capacity")
        if self.per <= timedelta(0):
            raise ValueError("a rate needs a positive period")

    @property
    def per_second(self) -> float:
        return self.capacity / self.per.total_seconds()


class TokenBucket:
    """One bucket. It starts full at the first time it sees."""

    def __init__(self, rate: Rate, *, tokens: float | None = None, at: datetime | None = None) -> None:
        self.rate = rate
        self.tokens = rate.capacity if tokens is None else min(tokens, rate.capacity)
        self.updated_at = at

    def _refill(self, now: datetime) -> None:
        if self.updated_at is None:
            self.updated_at = now
            return
        elapsed = (now - self.updated_at).total_seconds()
        if elapsed > 0:
            self.tokens = min(self.rate.capacity, self.tokens + elapsed * self.rate.per_second)
            self.updated_at = now

    def available(self, now: datetime) -> float:
        self._refill(now)
        return self.tokens

    def wait(self, now: datetime, n: float = 1.0) -> timedelta:
        """How long until `n` tokens are there; zero if they are there now."""
        self._refill(now)
        missing = n - self.tokens
        if missing <= 1e-9:
            return timedelta(0)
        # Round up to the microsecond, so waiting exactly this long always finds the token.
        micros = int(missing / self.rate.per_second * 1_000_000) + 1
        return timedelta(microseconds=micros)

    def take(self, now: datetime, n: float = 1.0) -> bool:
        """Take `n` tokens if they are there; False (and nothing taken) if not."""
        if self.wait(now, n) > timedelta(0):
            return False
        self.tokens -= n
        return True

    def full(self, now: datetime) -> bool:
        return self.available(now) >= self.rate.capacity - 1e-9


@dataclass(frozen=True)
class RateDecision:
    """The answer to "may I send now?": yes, or when to come back and which limit said no."""

    allowed: bool
    retry_at: datetime | None = None
    scope: str = ""
    """'recipient' or 'provider' when refused; empty when allowed."""


class RateLimiter:
    """Buckets per (provider, channel) and per recipient, created on first use.

    `provider_rates` gives a rate for specific (provider, channel) pairs; `default_provider_rate`
    covers any other pair (None = unlimited). `recipient_rate` limits each recipient across all
    providers (None = unlimited). Thread-safe: API requests come in on several threads.
    """

    def __init__(
        self,
        provider_rates: Mapping[tuple[str, Channel], Rate] | None = None,
        *,
        default_provider_rate: Rate | None = None,
        recipient_rate: Rate | None = None,
    ) -> None:
        self.provider_rates = dict(provider_rates or {})
        self.default_provider_rate = default_provider_rate
        self.recipient_rate = recipient_rate
        self._buckets: dict[tuple[str, Hashable], TokenBucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, key: tuple[str, Hashable], rate: Rate | None) -> TokenBucket | None:
        if rate is None:
            return None
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = TokenBucket(rate)
        return bucket

    def _provider_bucket(self, provider: str, channel: Channel) -> TokenBucket | None:
        rate = self.provider_rates.get((provider, channel), self.default_provider_rate)
        return self._bucket(("provider", (provider, channel)), rate)

    def _recipient_bucket(self, recipient: str) -> TokenBucket | None:
        return self._bucket(("recipient", recipient), self.recipient_rate)

    def recipient_wait(self, recipient: str, now: datetime) -> timedelta:
        with self._lock:
            bucket = self._recipient_bucket(recipient)
            return timedelta(0) if bucket is None else bucket.wait(now)

    def provider_wait(self, provider: str, channel: Channel, now: datetime) -> timedelta:
        with self._lock:
            bucket = self._provider_bucket(provider, channel)
            return timedelta(0) if bucket is None else bucket.wait(now)

    def take_recipient(self, recipient: str, now: datetime) -> RateDecision:
        with self._lock:
            bucket = self._recipient_bucket(recipient)
            if bucket is None or bucket.take(now):
                return RateDecision(True)
            return RateDecision(False, now + bucket.wait(now), "recipient")

    def take_provider(self, provider: str, channel: Channel, now: datetime) -> RateDecision:
        with self._lock:
            bucket = self._provider_bucket(provider, channel)
            if bucket is None or bucket.take(now):
                return RateDecision(True)
            return RateDecision(False, now + bucket.wait(now), "provider")

    def acquire(self, provider: str, channel: Channel, recipient: str, now: datetime) -> RateDecision:
        """Take one token from both buckets, or from neither: a refusal never half-spends."""
        with self._lock:
            pb = self._provider_bucket(provider, channel)
            rb = self._recipient_bucket(recipient)
            r_wait = timedelta(0) if rb is None else rb.wait(now)
            if r_wait > timedelta(0):
                return RateDecision(False, now + r_wait, "recipient")
            p_wait = timedelta(0) if pb is None else pb.wait(now)
            if p_wait > timedelta(0):
                return RateDecision(False, now + p_wait, "provider")
            if pb is not None:
                pb.take(now)
            if rb is not None:
                rb.take(now)
            return RateDecision(True)

    def prune(self, now: datetime) -> int:
        """Forget buckets that have refilled completely (they would start full anyway)."""
        with self._lock:
            stale = [k for k, b in self._buckets.items() if b.full(now)]
            for key in stale:
                del self._buckets[key]
            return len(stale)

    def __len__(self) -> int:
        return len(self._buckets)
