"""Refill scores on customer state: the numbers `decide()` reads to consider a refill reminder.

`decide()` reads only `CustomerState` (plus its context), so the cycle model's answer has to land
there. `CustomerState.scores` holds numbers only, so the scores describe the customer's *most
pressing* refill (the replenishable item they have bought with the highest `p14`) and the item
itself travels beside the state: `enrich_state` returns it as a `RefillPick`, and `enrich_all` as a
mapping customer_id -> item_id, for a bidder to put in `Action.item_id`.

The score keys (all namespaced `refill_`):

| key | meaning |
|---|---|
| `refill_p14` | chance they buy that item in the next 14 days, given they have not since |
| `refill_due_days` | days until the typical refill date; negative = overdue |
| `refill_window_open` | 1.0 when the refill window is open (see `autonomy.catalog.cycle`), else 0.0 |
| `refill_still_likely` | chance they are still on that item's cycle (not lapsed), given nothing since |
| `refill_cycle_days` | their predicted typical (median) gap for that item |
| `refill_items_due` | how many of their replenishable items have the window open now |

A customer with no replenishable purchase gets none of these keys (read them with
`scores.get(key, default)`), and enriching again first removes the old `refill_` keys, so the result
never mixes two moments. Nothing else on the state changes.

Purchases after `now` are ignored, so a replay never peeks at the future. Pure and deterministic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from autonomy.catalog.cycle import CycleEstimate, CycleModel
from autonomy.catalog.products import Catalogue
from autonomy.contracts import CustomerState, Event, EventType

__all__ = [
    "REFILL_CYCLE_DAYS",
    "REFILL_DUE_DAYS",
    "REFILL_ITEMS_DUE",
    "REFILL_KEYS",
    "REFILL_P14",
    "REFILL_STILL_LIKELY",
    "REFILL_WINDOW_OPEN",
    "RefillPick",
    "enrich_all",
    "enrich_state",
    "refill_candidates",
]

REFILL_P14: Final[str] = "refill_p14"
REFILL_DUE_DAYS: Final[str] = "refill_due_days"
REFILL_WINDOW_OPEN: Final[str] = "refill_window_open"
REFILL_STILL_LIKELY: Final[str] = "refill_still_likely"
REFILL_CYCLE_DAYS: Final[str] = "refill_cycle_days"
REFILL_ITEMS_DUE: Final[str] = "refill_items_due"
REFILL_KEYS: Final[tuple[str, ...]] = (
    REFILL_P14,
    REFILL_DUE_DAYS,
    REFILL_WINDOW_OPEN,
    REFILL_STILL_LIKELY,
    REFILL_CYCLE_DAYS,
    REFILL_ITEMS_DUE,
)


@dataclass(frozen=True)
class RefillPick:
    """The item a refill action would be about, with every replenishable item's estimate, best first."""

    customer_id: str
    item_id: str
    category: str | None
    estimate: CycleEstimate
    candidates: tuple[CycleEstimate, ...]


def refill_candidates(
    customer_id: str,
    events: Iterable[Event],
    catalogue: Catalogue,
    now: datetime,
    model: CycleModel,
) -> tuple[CycleEstimate, ...]:
    """One estimate per replenishable item the customer bought, most pressing first.

    Most pressing = highest `p14`, then the most overdue, then the item code (so ties always break
    the same way). Only PURCHASE events of `customer_id` at or before `now` count; the customer's
    purchases of any item set their order pace.
    """
    if model.level != "item":
        raise ValueError("refill scores need an item-level CycleModel")
    item_times: dict[str, list[datetime]] = {}
    all_times: list[datetime] = []
    for e in events:
        if e.type is not EventType.PURCHASE or e.customer_id != customer_id or e.ts > now:
            continue
        all_times.append(e.ts)
        if e.item_id is not None and catalogue.is_replenishable(e.item_id):
            item_times.setdefault(e.item_id, []).append(e.ts)
    if not item_times:
        return ()
    pace = model.pace_of(all_times, now)
    found: list[CycleEstimate] = []
    for item in sorted(item_times):
        est = model.estimate(item, item_times[item], now, group=catalogue.category_of(item), pace=pace)
        if est is not None:
            found.append(est)
    found.sort(key=lambda c: (-c.p14, c.days_until_due, c.key))
    return tuple(found)


def _scores(base: Mapping[str, float], candidates: Sequence[CycleEstimate]) -> dict[str, float]:
    scores = {k: v for k, v in base.items() if k not in REFILL_KEYS}
    if not candidates:
        return scores
    best = candidates[0]
    scores[REFILL_P14] = best.p14
    scores[REFILL_DUE_DAYS] = best.days_until_due
    scores[REFILL_WINDOW_OPEN] = 1.0 if best.window_open else 0.0
    scores[REFILL_STILL_LIKELY] = best.still_likely
    scores[REFILL_CYCLE_DAYS] = best.median_interval_days
    scores[REFILL_ITEMS_DUE] = float(sum(1 for c in candidates if c.window_open))
    return scores


def enrich_state(
    state: CustomerState,
    events: Iterable[Event],
    catalogue: Catalogue,
    now: datetime,
    *,
    model: CycleModel | None = None,
) -> tuple[CustomerState, RefillPick | None]:
    """The state with refill scores added, and the item they are about (`None` if there is none).

    `events` is the customer's history (other customers' events are ignored). `model` is the
    nightly-fitted item-level `CycleModel`; without one, a data-free model is built from the
    catalogue's typical cycles, which is right only for a shop with no history yet.
    """
    cycle = model if model is not None else CycleModel.from_catalogue(catalogue)
    candidates = refill_candidates(state.customer_id, events, catalogue, now, cycle)
    enriched = state.model_copy(update={"scores": _scores(state.scores, candidates)})
    if not candidates:
        return enriched, None
    best = candidates[0]
    pick = RefillPick(
        customer_id=state.customer_id,
        item_id=best.key,
        category=catalogue.category_of(best.key),
        estimate=best,
        candidates=candidates,
    )
    return enriched, pick


def enrich_all(
    states: Iterable[CustomerState],
    events_by_customer: Mapping[str, Sequence[Event]],
    catalogue: Catalogue,
    now: datetime,
    *,
    model: CycleModel | None = None,
) -> tuple[list[CustomerState], dict[str, str]]:
    """`enrich_state` for many customers: the states in the same order, and customer_id -> item_id.

    Only customers with a refill candidate appear in the mapping. A customer missing from
    `events_by_customer` has no purchases, so only their stale `refill_` keys are removed.
    """
    cycle = model if model is not None else CycleModel.from_catalogue(catalogue)
    out: list[CustomerState] = []
    items: dict[str, str] = {}
    for state in states:
        enriched, pick = enrich_state(
            state, events_by_customer.get(state.customer_id, ()), catalogue, now, model=cycle
        )
        out.append(enriched)
        if pick is not None:
            items[state.customer_id] = pick.item_id
    return out, items
