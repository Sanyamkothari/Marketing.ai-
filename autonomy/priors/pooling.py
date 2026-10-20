"""Cross-client learning, done safely (plan-v2 section 7): pool per-client pattern summaries into priors.

A prior is a starting point: "win-back emails labelled *education* average X% relative lift, across
N clients". A new client starts from it, and its own data overrides it as soon as it has enough
(`blend`). This module is where the safety rules are enforced in code, not left to good intentions:

1. **Only pattern statistics go in.** The input is a `ClientStat`: a pattern key, a hashed client
   tag, a count and a few named numbers. It has no room for customer rows, customer ids, content,
   offers or prices, and unknown fields are an error, so nothing else can be smuggled in.
2. **Clients are hashed, never named.** `client_hash` must look like a hex digest; a raw client name
   is refused. Use `hash_client(client_id, salt)` to make one, with a salt kept on our side.
3. **At least `MIN_CLIENTS_FOR_SHARED_PRIOR` (5) distinct clients, or no prior.** `pool` raises
   `NotEnoughClientsError` below that. The same rule applies to each named value on its own: a value that
   fewer than 5 clients reported is dropped, because it would be a number from fewer than 5 clients.
4. **No client dominates.** Each client is weighted by sqrt(its observations), so bigger clients count
   for more but not in proportion, and no single client's weight can exceed `MAX_CLIENT_WEIGHT` (30%).
   Without the cap one large client would effectively *be* the prior, and anyone who knew the other
   clients' numbers could read that client's strategy straight off it.
5. **Identities are dropped.** The resulting `Prior` carries the key, the client count, the total
   observation count and the pooled values. No client hashes, no per-client numbers, no weights.
6. **Opting out is by omission.** A client that turns sharing off simply has no `ClientStat` passed
   in; the agent still works for it, starting from Gym and public priors instead.

`seed_from` makes priors from the Gym and public datasets, which involve no client data and so need
none of the above. That is all the POC uses; real client pooling arrives later on the same path.

Everything here is pure and deterministic: time is passed in, and inputs are processed in a sorted
order so the same stats always give the same prior regardless of the order they arrive in.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from pydantic import Field, field_validator

from autonomy.contracts import MIN_CLIENTS_FOR_SHARED_PRIOR, Prior, PriorSource
from autonomy.contracts.base import Contract

__all__ = [
    "MAX_CLIENT_WEIGHT",
    "ClientStat",
    "NotEnoughClientsError",
    "blend",
    "capped_weights",
    "hash_client",
    "pool",
    "seed_from",
]

MAX_CLIENT_WEIGHT = 0.30
"""The most any one client can contribute to a pooled value (rule 4)."""

_HEX_DIGITS = frozenset("0123456789abcdef")


class NotEnoughClientsError(ValueError):
    """Raised instead of producing a prior from fewer than `MIN_CLIENTS_FOR_SHARED_PRIOR` clients."""


def hash_client(client_id: str, salt: str) -> str:
    """A stable, non-reversible tag for a client. The salt stays with us so tags cannot be guessed."""
    if not salt:
        raise ValueError("hash_client needs a non-empty salt; an unsalted hash of a name can be guessed")
    return hashlib.sha256(f"{salt}\x1f{client_id}".encode()).hexdigest()


def _finite(values: Mapping[str, float], what: str) -> None:
    bad = sorted(name for name, value in values.items() if not math.isfinite(value))
    if bad:
        raise ValueError(f"{what} has non-finite values for {bad}")


class ClientStat(Contract):
    """One client's summary of one pattern. Pattern statistics only; never customer-level data."""

    key: str = Field(description="The pattern, e.g. 'theme_lift/win_back/education'")
    client_hash: str = Field(description="hash_client(client_id, salt): a hex digest, never a name")
    n_observations: int = Field(ge=0, description="How many decisions or customers the numbers summarise")
    values: dict[str, float] = Field(description="Named pattern numbers, e.g. {'relative_lift': 0.12}")

    @field_validator("client_hash")
    @classmethod
    def _looks_hashed(cls, value: str) -> str:
        if not 16 <= len(value) <= 128 or not set(value) <= _HEX_DIGITS:
            raise ValueError("client_hash must be a lowercase hex digest (use hash_client), not a raw name")
        return value

    @field_validator("values")
    @classmethod
    def _finite_values(cls, value: dict[str, float]) -> dict[str, float]:
        _finite(value, "a client stat")
        return value


def capped_weights(sizes: Sequence[int], cap: float = MAX_CLIENT_WEIGHT) -> list[float]:
    """Weights proportional to sqrt(size) that sum to 1, with none above `cap`.

    Water-filling: any weight over the cap is pinned at the cap, and the remaining mass is shared
    among the others in proportion to sqrt(size) again, until nobody is over. It needs at least
    ceil(1 / cap) clients to be possible, which the 5-client minimum guarantees for a 30% cap.
    """
    if not sizes:
        raise ValueError("capped_weights needs at least one size")
    if any(n <= 0 for n in sizes):
        raise ValueError("every size must be positive")
    if not 0 < cap <= 1:
        raise ValueError("cap must be in (0, 1]")
    if len(sizes) * cap < 1 - 1e-12:
        raise ValueError(f"{len(sizes)} clients cannot each stay under a {cap:.0%} cap")
    raw = [math.sqrt(n) for n in sizes]
    weights = [0.0] * len(raw)
    pinned: set[int] = set()
    while True:
        free = [i for i in range(len(raw)) if i not in pinned]
        mass = 1.0 - cap * len(pinned)
        total = sum(raw[i] for i in free)
        for i in free:
            weights[i] = mass * raw[i] / total
        over = [i for i in free if weights[i] > cap + 1e-12]
        if not over:
            break
        for i in over:
            pinned.add(i)
            weights[i] = cap
    return weights


def _merge_per_client(
    stats: Iterable[ClientStat],
) -> dict[str, tuple[int, dict[str, list[tuple[int, float]]]]]:
    """Group a key's stats by client: total observations, and each value's (n, value) pairs."""
    merged: dict[str, tuple[int, dict[str, list[tuple[int, float]]]]] = {}
    for stat in stats:
        n_total, parts = merged.get(stat.client_hash, (0, {}))
        for name, value in stat.values.items():
            parts.setdefault(name, []).append((stat.n_observations, value))
        merged[stat.client_hash] = (n_total + stat.n_observations, parts)
    return merged


def _client_value(parts: list[tuple[int, float]]) -> float:
    """One client's value from several stats for the same key: observation-weighted mean."""
    total = sum(n for n, _ in parts)
    return sum(n * v for n, v in parts) / total


def pool(stats: Iterable[ClientStat], key: str, now: datetime) -> Prior:
    """Pool the clients' stats for `key` into one client-sourced prior, enforcing the safety rules.

    Stats for other keys are ignored, so a caller can pass everything it has. Several stats from one
    client count as one client (merged by observation count), and stats with no observations are
    ignored. Raises `NotEnoughClientsError` when fewer than `MIN_CLIENTS_FOR_SHARED_PRIOR` distinct clients
    remain, or when no named value was reported by that many clients.
    """
    relevant = sorted(
        (s for s in stats if s.key == key and s.n_observations > 0),
        key=lambda s: (s.client_hash, s.n_observations, sorted(s.values.items())),
    )
    per_client = _merge_per_client(relevant)
    if len(per_client) < MIN_CLIENTS_FOR_SHARED_PRIOR:
        raise NotEnoughClientsError(
            f"{key!r} has data from {len(per_client)} client(s); a shared prior needs at least "
            f"{MIN_CLIENTS_FOR_SHARED_PRIOR}"
        )

    names = sorted({name for _, parts in per_client.values() for name in parts})
    pooled: dict[str, float] = {}
    for name in names:
        reporting = sorted(
            (client, sum(n for n, _ in parts[name]), _client_value(parts[name]))
            for client, (_, parts) in per_client.items()
            if name in parts
        )
        if len(reporting) < MIN_CLIENTS_FOR_SHARED_PRIOR:
            continue  # rule 3: a number from fewer than 5 clients is never shared
        weights = capped_weights([n for _, n, _ in reporting])
        pooled[name] = math.fsum(w * v for w, (_, _, v) in zip(weights, reporting, strict=True))
    if not pooled:
        raise NotEnoughClientsError(
            f"no value of {key!r} was reported by at least {MIN_CLIENTS_FOR_SHARED_PRIOR} clients"
        )

    return Prior(
        key=key,
        source=PriorSource.CLIENTS,
        n_clients=len(per_client),
        n_observations=sum(n for n, _ in per_client.values()),
        values=pooled,
        updated_at=now,
    )


def blend(
    prior_values: Mapping[str, float],
    local_values: Mapping[str, float],
    local_n: float,
    prior_strength: float,
) -> dict[str, float]:
    """Shrink a client's own estimates towards the prior, less and less as its own data grows.

    Empirical-Bayes style: the prior counts as `prior_strength` pseudo-observations, so each value is

        local_n / (local_n + prior_strength) * local + prior_strength / (local_n + prior_strength) * prior

    With no local data the prior is returned; with `local_n` much larger than `prior_strength` the
    local value wins. A value known on only one side passes through unchanged.
    """
    if local_n < 0 or not math.isfinite(local_n):
        raise ValueError("local_n must be a finite number >= 0")
    if prior_strength <= 0 or not math.isfinite(prior_strength):
        raise ValueError("prior_strength must be a finite number > 0")
    _finite(prior_values, "the prior")
    _finite(local_values, "the local estimate")
    w_local = local_n / (local_n + prior_strength)
    out: dict[str, float] = {}
    for name in sorted(set(prior_values) | set(local_values)):
        if name not in local_values:
            out[name] = prior_values[name]
        elif name not in prior_values:
            out[name] = local_values[name]
        else:
            out[name] = w_local * local_values[name] + (1.0 - w_local) * prior_values[name]
    return out


def seed_from(
    source: PriorSource,
    values: Mapping[str, float],
    key: str,
    now: datetime,
    *,
    n_observations: int = 0,
) -> Prior:
    """A starting prior from the Gym or a public dataset. No client data, so no pooling rules apply.

    Client-sourced priors must come from `pool`, which enforces the safety rules; asking for one
    here is an error.
    """
    if source is PriorSource.CLIENTS:
        raise ValueError("client-sourced priors must be built with pool(), which enforces the safety rules")
    if not values:
        raise ValueError(f"a seeded prior for {key!r} needs at least one value")
    _finite(values, f"the seed for {key!r}")
    return Prior(
        key=key,
        source=source,
        n_clients=0,
        n_observations=n_observations,
        values=dict(sorted(values.items())),
        updated_at=now,
    )
