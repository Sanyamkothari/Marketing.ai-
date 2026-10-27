"""Turning the event log into customer states: all at once, or a few events at a time.

`rebuild_states` throws the derived state away and folds every stored event again, in time order. Use
it after loading a history (a CSV, a Shopify export) or after changing the folder. `apply_events`
folds new events into the states already stored. Folding the same events in the same order through
either path gives the same states; the tests hold both to that.

Both write in batches, so a Postgres `StateStore` sees a few large `put` calls rather than one per
event.

**Money in different currencies is never added.** A state's revenue is one number, so a rebuild
refuses (`MixedCurrencyError`, before writing anything) when stored events with an amount carry
different `attrs["currency"]` codes, for example two files mapped with different currencies. Map
them with `EventMapping.currency` and `currency_rates` into one business currency first.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from autonomy.contracts import CustomerState, Event
from autonomy.interfaces import EventStore, StateFolder, StateStore

DEFAULT_BATCH = 1000

CURRENCY_ATTR = "currency"


class MixedCurrencyError(ValueError):
    """Stored amounts are in more than one currency, so revenue cannot be one number."""

    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = dict(counts)
        listed = ", ".join(
            f"{code} ({n:,} events)" for code, n in sorted(counts.items(), key=lambda kv: -kv[1])
        )
        super().__init__(
            f"the event log holds amounts in several currencies: {listed}. Adding them would mix "
            "different money; map them into one business currency (EventMapping.currency and "
            "currency_rates) and load again"
        )


KEPT_ON_REBUILD: tuple[str, ...] = ("group", "scores")
"""Fields no event produces, so a rebuild must carry them over rather than reset them.

`group` is the agent / baseline / holdout split that every proof number rests on; losing it on a
rebuild would quietly re-randomise who is held out. `scores` come from models, not events.
"""

ACCOUNT_LEVEL_ATTR = "account_level"
"""An event carrying this attr as True is about the account, not a customer (the same key
`autonomy.channels.whatsapp_health` sets on a WhatsApp quality report)."""


def _check_batch(batch_size: int) -> None:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")


def _put_batches(states: StateStore, rows: Iterable[CustomerState], batch_size: int) -> int:
    written = 0
    batch: list[CustomerState] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= batch_size:
            states.put(batch)
            written += len(batch)
            batch = []
    if batch:
        states.put(batch)
        written += len(batch)
    return written


def rebuild_states(
    events: EventStore,
    states: StateStore,
    folder: StateFolder,
    now: datetime,
    *,
    batch_size: int = DEFAULT_BATCH,
    keep: tuple[str, ...] = KEPT_ON_REBUILD,
) -> int:
    """Rebuild every customer with events from scratch, refreshed to `now`; return how many were written.

    Raises `MixedCurrencyError`, having written nothing, when amounts carry different currency codes.

    Every stored event is folded, in the store's time order, into a fresh state per customer, then
    each state is refreshed at `now` so windows and segments are current. Fields named in `keep` are
    copied from the state already stored, if any. Customers with stored state but no events are left
    as they are: the `StateStore` seam has no delete.

    The fold runs in memory over one streamed pass of the log (one entry per customer, not per
    event); writes go out `batch_size` states at a time.
    """
    _check_batch(batch_size)
    unknown = [name for name in keep if name not in CustomerState.model_fields]
    if unknown:
        raise ValueError(f"not CustomerState fields: {unknown}")

    kept: dict[str, dict[str, object]] = {}
    if keep:
        default = CustomerState.model_fields
        for old in states.scan():
            carried = {
                name: getattr(old, name)
                for name in keep
                if getattr(old, name) != default[name].get_default(call_default_factory=True)
            }
            if carried:
                kept[old.customer_id] = carried

    built: dict[str, CustomerState] = {}
    currencies: dict[str, int] = {}
    for event in events.scan():
        if is_account_level(event):
            continue
        if event.value is not None:
            code = event.attrs.get(CURRENCY_ATTR)
            if isinstance(code, str) and code.strip():
                key = code.strip().upper()
                currencies[key] = currencies.get(key, 0) + 1
        built[event.customer_id] = folder.fold(built.get(event.customer_id), event)
    if len(currencies) > 1:
        raise MixedCurrencyError(currencies)

    def finished() -> Iterable[CustomerState]:
        for customer_id in sorted(built):
            state = built[customer_id]
            carried = kept.get(customer_id)
            if carried:
                state = state.model_copy(update=carried)
            yield folder.refresh(state, now)

    return _put_batches(states, finished(), batch_size)


def is_account_level(event: Event) -> bool:
    """True for an event about the account rather than a customer (a WhatsApp quality report):
    it stays in the log for the breakers but is never folded into a customer state."""
    return event.attrs.get(ACCOUNT_LEVEL_ATTR) is True


def apply_events(
    events: Iterable[Event],
    states: StateStore,
    folder: StateFolder,
    *,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH,
) -> int:
    """Fold `events` into the stored states, in the order given; return how many customers changed.

    Pass only events the `EventStore` accepted as new (`append` drops repeats, this does not): an
    event folded twice is counted twice. Give `now` to also refresh every touched state to that time.

    States are cached while folding and written once `batch_size` customers are pending, so a long
    stream costs one read per customer per batch and one write per batch. A customer who appears again
    after a write is read back from the store, which then holds the latest state.
    """
    _check_batch(batch_size)
    pending: dict[str, CustomerState] = {}
    touched: set[str] = set()

    def flush() -> None:
        rows: Iterable[CustomerState] = (
            pending.values() if now is None else (folder.refresh(s, now) for s in pending.values())
        )
        _put_batches(states, list(rows), batch_size)
        pending.clear()

    for event in events:
        if is_account_level(event):
            continue
        cid = event.customer_id
        current = pending[cid] if cid in pending else states.get(cid)
        pending[cid] = folder.fold(current, event)
        touched.add(cid)
        if len(pending) >= batch_size:
            flush()
    if pending:
        flush()
    return len(touched)
