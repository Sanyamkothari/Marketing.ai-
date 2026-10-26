"""Build profiles: for one customer, for a whole store in batches, and as training rows.

Three entry points, one computation (`autonomy.profile.features.compute_all`):

- `profile_for(events, as_of)`: one customer's features, reading no event after `as_of`;
- `build_profiles(events, states, as_of)`: fills `CustomerState.features` and `features_as_of` for
  every stored state, `batch_size` customers at a time;
- `training_rows(events, as_ofs)` and `training_rows_from_store(...)`: the same features at several
  snapshot dates (month ends, say), so a slow model trains on exactly what the agent will be fed.

**Memory.** `build_profiles` holds one batch of states and their events at a time, plus the shop's
price histogram (a few hundred counters). It pages the state store with `scan()` and writes each batch
back before reading the next, as the stores' keyset paging allows (REAL_DATA_RESULTS failure 11: the
sweep ran out of memory at 1M customers because it held everything at once). Events are read per
customer with `EventStore.for_customer`, the way `AutonomyRuntime._feed_history` reads them.

**Price level.** Needs the shop's order values up to `as_of`, which is one extra streamed pass over the
log (`EventStore.scan(until=...)`) when `build_profiles` is not handed a `ShopPrices`. Pass
`price_context=False` to skip the pass; `price_level` then stays at its default and `price_level_known`
at 0.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from autonomy.contracts import CustomerState, Event, EventType
from autonomy.interfaces import EventStore, StateStore
from autonomy.profile.facts import gather
from autonomy.profile.features import compute_all
from autonomy.profile.shop import ShopPrices

__all__ = [
    "DEFAULT_BATCH",
    "ProfileRow",
    "build_profiles",
    "profile_for",
    "training_rows",
    "training_rows_from_store",
    "with_profile",
]

DEFAULT_BATCH = 2_000
"""Customers per batch, the same as `RuntimeConfig.history_batch`."""

_INCLUSIVE = timedelta(microseconds=1)
"""Stores read `since <= ts < until`; adding this to `as_of` makes an event at `as_of` included."""


@dataclass(frozen=True, slots=True)
class ProfileRow:
    """One customer's features at one snapshot date."""

    customer_id: str
    as_of: datetime
    features: dict[str, float]


def profile_for(
    events: Iterable[Event], as_of: datetime, *, shop: ShopPrices | None = None
) -> dict[str, float]:
    """One customer's features as of `as_of`; an event after `as_of` is never read.

    `events` are that customer's events in any order (more than one customer raises `ValueError`).
    `shop` is the shop's order values up to `as_of`; without it the price level takes its default.
    Every value is a finite float; a feature with nothing to measure has its documented default.
    """
    return compute_all(gather(events, as_of, shop))


def with_profile(state: CustomerState, features: dict[str, float], as_of: datetime) -> CustomerState:
    """The state with its profile filled in. Nothing else about it changes."""
    return state.model_copy(update={"features": features, "features_as_of": as_of})


def build_profiles(
    events: EventStore,
    states: StateStore,
    as_of: datetime,
    *,
    batch_size: int = DEFAULT_BATCH,
    shop: ShopPrices | None = None,
    price_context: bool = True,
) -> int:
    """Fill `features` and `features_as_of` on every stored state; return how many were written.

    Customers are read and written `batch_size` at a time. A customer with no event at or before
    `as_of` gets the all-defaults profile. Without `shop`, the shop's order values are read from the
    log in one streamed pass unless `price_context` is False.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if shop is None and price_context:
        shop = ShopPrices.until(events.scan(until=as_of + _INCLUSIVE), as_of)
    written = 0
    batch: list[CustomerState] = []
    for state in states.scan():
        batch.append(state)
        if len(batch) >= batch_size:
            written += _profile_batch(events, states, batch, as_of, shop)
            batch = []
    if batch:
        written += _profile_batch(events, states, batch, as_of, shop)
    return written


def _profile_batch(
    events: EventStore,
    states: StateStore,
    batch: Sequence[CustomerState],
    as_of: datetime,
    shop: ShopPrices | None,
) -> int:
    rows = [
        with_profile(
            s,
            profile_for(events.for_customer(s.customer_id, until=as_of + _INCLUSIVE), as_of, shop=shop),
            as_of,
        )
        for s in batch
    ]
    states.put(rows)
    return len(rows)


def training_rows(
    events: Iterable[Event], as_ofs: Sequence[datetime], *, price_context: bool = True
) -> Iterator[ProfileRow]:
    """The profile of every customer at each date in `as_ofs`, customers in id order, dates ascending.

    A customer appears at a date only if they have an event at or before it: someone who has not
    shown up yet is not in the population then. Each row equals `profile_for(that customer's events,
    that date)`; the shop's price histogram is the one `ShopPrices.until` gives for that date.
    `events` are held in memory, grouped by customer; for a store too big for that, use
    `training_rows_from_store`. A repeated `event_id` is read once (the first copy), as the stores
    keep it, so a row equals what `build_profiles` writes from a store holding the same events.
    """
    dates = _dates(as_ofs)
    by_customer: dict[str, list[Event]] = {}
    seen: set[str] = set()
    for e in events:
        if e.event_id not in seen:
            seen.add(e.event_id)
            by_customer.setdefault(e.customer_id, []).append(e)
    del seen
    if price_context:
        purchases = sorted(
            (e for evs in by_customer.values() for e in evs if e.type is EventType.PURCHASE),
            key=lambda e: (e.ts, e.event_id),
        )
        shops = ShopPrices.at(purchases, dates)  # the roll-up into orders needs time order
    else:
        shops = {}
    for cid in sorted(by_customer):
        yield from _rows(cid, by_customer[cid], dates, shops)


def training_rows_from_store(
    events: EventStore,
    customer_ids: Iterable[str],
    as_ofs: Sequence[datetime],
    *,
    price_context: bool = True,
) -> Iterator[ProfileRow]:
    """`training_rows` for an event store: one customer's events in memory at a time.

    The shop's price histograms for all dates come from one streamed pass over the log.
    """
    dates = _dates(as_ofs)
    shops = ShopPrices.at(events.scan(until=dates[-1] + _INCLUSIVE), dates) if price_context else {}
    for cid in customer_ids:
        yield from _rows(cid, events.for_customer(cid, until=dates[-1] + _INCLUSIVE), dates, shops)


def _dates(as_ofs: Sequence[datetime]) -> list[datetime]:
    if not as_ofs:
        raise ValueError("training rows need at least one as_of date")
    for d in as_ofs:
        if d.tzinfo is None or d.utcoffset() is None:
            raise ValueError("every as_of must be timezone-aware")
    return sorted(set(as_ofs))


def _rows(
    customer_id: str, events: Sequence[Event], dates: Sequence[datetime], shops: dict[datetime, ShopPrices]
) -> Iterator[ProfileRow]:
    first = min((e.ts for e in events), default=None)
    for d in dates:
        if first is None or first > d:
            continue
        yield ProfileRow(customer_id, d, profile_for(events, d, shop=shops.get(d)))
