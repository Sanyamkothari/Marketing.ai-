"""Product catalogue intelligence: refill cycles and complementary-product recommendations.

The product vision's hero example ("bought a serum, viewed moisturisers twice, nears the likely refill
period") needs to know, per customer, which product they will run out of and when, and which product
goes with what they have. These modules answer that from purchase history alone, with plain maths
(no LLM, no clock reads), for the agent to read through `CustomerState.scores` and `Action.item_id`:

- `products`: `Product` and `Catalogue` (replenishable items and typical cycles, given or inferred),
  plus the purchase-table helpers and the one cleaning of the raw online-retail log
  (`clean_retail_lines`) that both validations start from.
- `cycle`: the per-customer purchase-cycle model (`CycleModel`): when is the next purchase due, the
  chance of one in the next 14 days, and whether the refill window is open; plus its backtests
  (`python -m autonomy.catalog.cycle` prints the online-retail validation in docs/v2/CATALOG.md).
- `enrich`: puts the refill scores on `CustomerState` (`enrich_state`, `enrich_all`).
- `recommend`: complementary products from co-purchase history (`CoPurchaseModel`), and
  `next_best_item`, which picks a refill when its window is open and a complement otherwise.
- `evaluate`: the recommender's offline evaluation (`python -m autonomy.catalog.evaluate`). It is
  not imported here, since it is a report, not something the agent calls.

Two purchase tables, on purpose: `products.purchases_frame` (days since the epoch, with value and
quantity) feeds the catalogue and the cycle model, and `recommend.basket_frame` (Unix seconds, with
the basket) feeds the recommender. Both are built from the same PURCHASE events.

The everyday names are re-exported here (`from autonomy.catalog import CycleModel`). Everything is
light (numpy and pandas), so importing the package costs little. The `recommend()` function is not
re-exported: its name would hide the `recommend` module on the package (use
`CoPurchaseModel.recommend`, or import it from `autonomy.catalog.recommend`).
"""

from autonomy.catalog.cycle import CycleConfig, CycleEstimate, CycleModel
from autonomy.catalog.enrich import REFILL_KEYS, RefillPick, enrich_all, enrich_state
from autonomy.catalog.products import Catalogue, InferenceRules, Product, purchases_frame
from autonomy.catalog.recommend import (
    CoPurchaseConfig,
    CoPurchaseModel,
    NextItem,
    Recommendation,
    RefillWindow,
    RefillWindowInfo,
    basket_frame,
    next_best_item,
)

__all__ = [
    "REFILL_KEYS",
    "Catalogue",
    "CoPurchaseConfig",
    "CoPurchaseModel",
    "CycleConfig",
    "CycleEstimate",
    "CycleModel",
    "InferenceRules",
    "NextItem",
    "Product",
    "Recommendation",
    "RefillPick",
    "RefillWindow",
    "RefillWindowInfo",
    "basket_frame",
    "enrich_all",
    "enrich_state",
    "next_best_item",
    "purchases_frame",
]
