# Stream: Catalog (refills and recommendations)

Which product a customer will run out of and when, and which product goes with what they own.
Plain maths over purchase history: no LLM, no clock reads (time is always passed in), no randomness.
Validation on the real online-retail log is in [docs/v2/CATALOG.md](../CATALOG.md).

## What was built (`autonomy/catalog/`)

| Module | What it does |
|---|---|
| `products.py` | `Product` and `Catalogue`. Merchant fields left blank are inferred from repeat purchases (replenishable, typical cycle = median repeat gap). Also the cycle model's purchase table (`purchases_frame`, `trips`, `repeat_intervals`) and `clean_retail_lines`, the one cleaning of the raw online-retail log both validations use. |
| `cycle.py` | `CycleModel`: log-normal gaps, shrunk customer -> item -> category -> shop (empirical Bayes), shifted by the customer's order pace, with a cure-rate mixture for people who do not come back. Gives due date, `days_until_due`, `p14` and the refill window. Also the backtests and `python -m autonomy.catalog.cycle`. |
| `enrich.py` | Puts refill scores on `CustomerState.scores` (`enrich_state`, `enrich_all`) and returns the item they are about, for `Action.item_id`. |
| `recommend.py` | `CoPurchaseModel`: item-item co-purchase over shopping trips (cosine or lift), recency-weighted votes, a plain reason ("Often bought with your ..."), popularity fallback, category diversity. `next_best_item` picks a refill when its window is open, else a complement. |
| `evaluate.py` | Offline evaluation of the recommender against popularity (`python -m autonomy.catalog.evaluate`); writes the Recommendations section of CATALOG.md. |

On the real log the cycle model beats the naive average gap (item MAE 54.1 vs 57.2 days; 25.5 vs
32.6 for customers with 3+ gaps; `p14` AUC 0.74-0.75) and co-purchase beats popularity (hit@5
34.7% vs 22.0%).

## How other streams use it

```python
from autonomy.catalog import (
    Catalogue, Product, CycleModel, CycleEstimate, enrich_state, enrich_all, REFILL_KEYS,
    CoPurchaseModel, next_best_item, NextItem, Recommendation,
)

# Nightly fits (both ignore purchases after as_of)
catalogue = Catalogue.from_records(records, purchases=events)      # or Catalogue.infer(events)
cycles = CycleModel.fit(events, as_of, level="item", catalogue=catalogue)
co = CoPurchaseModel.fit(events, as_of=as_of, names={p.item_id: p.name for p in catalogue})

# Per customer, before decide()
state, pick = enrich_state(state, customer_events, catalogue, now, model=cycles)
#   -> (CustomerState, RefillPick | None); pick.item_id goes in Action.item_id
states, item_by_customer = enrich_all(states, events_by_customer, catalogue, now, model=cycles)
#   score keys: refill_p14, refill_due_days, refill_window_open (1.0/0.0), refill_still_likely,
#   refill_cycle_days, refill_items_due. Absent when there is no replenishable purchase.

co.recommend(customer_events, k=5, now=now) -> list[Recommendation]   # (item_id, score, reason)
next_best_item(customer_events, co, pick.candidates if pick else (), now=now) -> NextItem | None
#   NextItem(item_id, kind="refill" | "complement", reason)
cycles.estimate_customer(customer_events, now) -> dict[str, CycleEstimate]
```

`CycleEstimate` fits `next_best_item`'s `RefillWindow` protocol as it is (`key`, `level`,
`window_open`, `days_until_due`). There are two purchase tables on purpose: `purchases_frame`
(days since the epoch, value, quantity) for the catalogue and cycles, and `basket_frame` (Unix
seconds, basket id) for the recommender; both fits also accept the events directly.

## Contract additions requested

None. `Action.item_id` already exists.

## Dependencies requested

None (numpy, pandas, pydantic, stdlib).

## Known gaps

- mypy --strict was not run with Python 3.11 and pandas-stubs (mypy is not installed here). The
  pandas calls were read against the stubs; CI is the real gate.
- The real-data numbers need the git-ignored raw UCI log (`library/online-retail/fetch.py`). CI and
  the tests use only synthetic data and the committed `sample.csv`, which has no product lines.
- The online-retail log has no categories, so the category level of the cycle hierarchy and the
  recommender's category diversity are tested only on synthetic data. Refund events are ignored by
  both models.
- `p14` runs about one point high; the 80% gap interval covers 78%; no seasonality (one year of
  data); item cycles under-represent long gaps (right-censoring). The 20th-60th percentile refill
  window was tuned on one cutoff. Timing errors stay large (median 32 days) on this wholesaler log.
- The scores are not yet read by any bidder: wiring `refill_*` scores and `NextItem` into
  `decide()` belongs to the decide and runtime streams.
- Scoring recommendations runs at about 1,000 customers a second (not vectorised across customers).
- A merchant-stated `replenishable=False` item still gets the inferred `typical_cycle_days`
  (harmless: only replenishable items are scored).
- `black --check docs/v2/CATALOG.md` cannot work (black does not parse Markdown); the Python files
  are black- and ruff-clean.
