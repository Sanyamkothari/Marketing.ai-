# Catalogue: refills and recommendations

## Refill cycles

When will this customer buy this product again? `autonomy/catalog/cycle.py` answers it per (customer, item), and `autonomy/catalog/enrich.py` puts the answer on `CustomerState.scores` for `decide()`. Pure maths: no LLM, no clock, no randomness.

**The model.** The gap between two purchases of the same item is log-normal. A customer's own gaps are shrunk toward the item's population cycle (empirical Bayes: the weights come from the measured within-customer spread `sigma` and between-customer spread `tau`), item cycles toward their category's, and categories toward the shop's. A merchant's stated `typical_cycle_days` stands in for the category as the item's anchor. Customer pace (how often they order anything, shrunk the same way) shifts the anchor, because a weekly shopper refills sooner than a twice-a-year one; the slope is fitted within items. Not everyone comes back: with probability `repeat_prob` (the item's on-cycle rebuy rate within 120 days, shrunk the same way and raised for customers who have bought it 2, 3, 4 or 5+ times) the customer is on the cycle, otherwise they rebuy only off-cycle at a small fitted daily rate. For a given `now` it gives the due date (last purchase + predicted median gap), `days_until_due` (negative = overdue), `p14` (the chance of a purchase in the next 14 days given none since: the hazard of that mixture) and the refill window (time since the last purchase between the 20th and 60th percentiles of the predicted gap).

**The catalogue.** `Product` (item_id, name, category, price, margin_rate, replenishable, typical_cycle_days) and `Catalogue` (from products, records or a DataFrame). What the merchant leaves blank is inferred from purchases: an item is replenishable when at least 3 customers, and at least 10% of its buyers, bought it again on a later day with a median gap of at least 2 days; its typical cycle is that median gap. Only stated cycles are used as prior knowledge in the model (an inferred one is already in the data).

**On the state.** `scores` holds numbers only, so the scores describe the customer's most pressing replenishable item (highest `p14`) and the item itself travels beside the state: `enrich_state(state, events, catalogue, now, model=...)` returns `(state, RefillPick | None)` and `enrich_all(...)` returns `(states, {customer_id: item_id})`, for `Action.item_id`. Keys: `refill_p14`, `refill_due_days`, `refill_window_open` (1.0 / 0.0), `refill_still_likely`, `refill_cycle_days`, `refill_items_due`. A customer with no replenishable purchase gets none of them (read with `scores.get`), and re-enriching drops stale `refill_` keys first.

**Validation on the UCI Online Retail log** (2010-12-01 to 2011-12-09, the raw log `fetch.py` downloads; the same cleaning as the loaders, product lines only). Item = StockCode. Two checks:

- *Timing.* Hold out each repeat (customer, item) pair's last purchase, fit on everything else, predict the held-out gap from the earlier purchases. Naive = the population's average gap for everyone, as asked; the population's median gap and the item's own cycle ("item cycle only": the fitted item cycle for a customer of typical order pace, with no customer data) are shown too.
- *`p14`, forward in time.* Fit on purchases before a cutoff, score every (customer, item) pair that had bought before it, including the many that never buy again, and check who bought in the 14 days after. Naive = the same "population average interval" with no customer or item information: the shop's rebuy rate times a constant hazard of one purchase per average gap.

Raw log: 396,337 product lines, 4,334 customers, 3,659 products.

**Timing: hold out each repeat pair's last purchase** (MAE in days of the predicted gap)

| level | pairs | model | naive: average gap | naive: median gap | item cycle only | median AE model / naive | 80% interval coverage |
|---|---:|---:|---:|---:|---:|---:|---:|
| item | 58,774 | **54.1** | 57.2 (50.6 d) | 61.3 | 55.3 | 32.4 / 35.6 | 78.0% |
| order | 2,776 | **51.8** | 54.0 (37.4 d) | 57.8 | 53.6 | 26.0 / 28.4 | 73.3% |

**Item level, by how many own gaps the customer had before the held-out one**

| own gaps | pairs | model | naive (average) | item cycle only | own mean only |
|---|---:|---:|---:|---:|---:|
| 0 | 34,931 | **64.4** | 67.5 | 63.6 | n/a |
| 1 | 11,795 | **47.9** | 49.2 | 47.5 | 68.0 |
| 2 | 5,069 | **37.3** | 38.8 | 39.6 | 49.2 |
| 3+ | 6,979 | **25.5** | 32.6 | 37.8 | 29.8 |

**`p14`, forward in time: fit before the cutoff, did the pair buy in the next 14 days?**

| level | cutoff | pairs | bought | Brier model / naive | log loss model / naive | AUC | ECE model / naive | rate, window open / closed |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| item | 2011-09-01 | 162,426 | 3.5% | 0.0320 / 0.0342 | 0.136 / 0.154 | 0.750 | 0.0073 / 0.0103 | 5.9% / 2.9% |
| item | 2011-10-01 | 188,399 | 3.1% | 0.0285 / 0.0299 | 0.125 / 0.139 | 0.741 | 0.0117 / 0.0096 | 4.2% / 2.8% |
| item | 2011-11-01 | 220,880 | 4.2% | 0.0371 / 0.0401 | 0.153 / 0.174 | 0.743 | 0.0023 / 0.0060 | 6.5% / 3.5% |
| order | 2011-09-01 | 3,314 | 15.1% | 0.1078 / 0.1287 | 0.360 / 0.426 | 0.762 | 0.0294 / 0.0186 | 23.8% / 12.3% |
| order | 2011-10-01 | 3,613 | 14.9% | 0.1107 / 0.1271 | 0.372 / 0.422 | 0.721 | 0.0202 / 0.0132 | 21.3% / 13.1% |
| order | 2011-11-01 | 3,970 | 18.6% | 0.1312 / 0.1531 | 0.421 / 0.487 | 0.747 | 0.0381 / 0.0402 | 25.6% / 15.9% |

**Reliability, item level, cutoff 2011-09-01** (ten equal-count bins of `p14`)

| mean `p14` | bought | pairs |
|---:|---:|---:|
| 1.4% | 1.3% | 16,243 |
| 1.5% | 0.9% | 16,243 |
| 1.7% | 1.3% | 16,243 |
| 1.9% | 1.5% | 16,243 |
| 2.2% | 2.0% | 16,243 |
| 2.7% | 2.2% | 16,243 |
| 3.4% | 2.5% | 16,242 |
| 4.5% | 3.3% | 16,242 |
| 7.0% | 5.8% | 16,242 |
| 16.4% | 14.5% | 16,242 |

**Committed `sample.csv`** (order level; no product codes): 481 repeat customers, MAE 26.2 days vs 33.2 for the average gap. Its in-between order dates are spaced evenly by the loader, so a customer's own gaps predict the held-out one almost exactly; only the customers with two orders have a fully real gap: 264 of them, MAE 47.1 vs 46.9.

**What the numbers say**

- The model beats the naive average gap at every level of customer history, and by most where it matters for refills: customers with 3+ earlier gaps of the item (25.5 vs 32.6 days, 22% better). With no own gap it is the item's cycle plus pace, about 3 days better than naive. Pace earns that mostly by correcting the item cycle itself (fitted within items, so frequent repeat buyers no longer drag it short): with pace switched off (`max_pace_slope=0`) the 0-gap MAE is 68.7 and the overall 57.1, no better than naive. The per-customer pace shift on top of it does not help yet at 0 or 1 own gaps (64.4 vs 63.6 and 47.9 vs 47.5 for the typical-pace item cycle alone); the model with pace still beats the model without it in every row (64.4 vs 68.7, 47.9 vs 49.2, 37.3 vs 38.0, 25.5 vs 26.0; measured in review). Absolute errors stay large (median 32 days at item level) because this is a wholesaler with irregular reorders and a one-year window; the 80% predictive interval covers 78% of held-out gaps, so it is slightly too narrow.
- `p14` ranks well (AUC 0.74 to 0.75 at item level) and beats the naive rule on Brier and log loss at every cutoff and level. It runs a little high (mean 4.3% vs 3.5% bought at the September cutoff, worst for pairs bought within the last month). The naive rule's ECE is sometimes lower only because it is one constant close to the base rate; it cannot rank anyone (AUC 0.5).
- An open refill window lifts the 14-day rebuy rate 1.5x to 2x at item level (5.9% vs 2.9% at the first cutoff) and 1.6x to 1.9x at order level. The window was set to the 20th-60th percentiles after comparing a few choices on the 2011-09-01 cutoff only (a 25th-90th window had no lift); the other two cutoffs are out of sample for that choice.
- Item cycles here have no category level (the log has none); with a real catalogue the category step sits between item and shop.

Reproduce with `python library/online-retail/fetch.py`, then `python -m autonomy.catalog.cycle` (about 20 s on one CPU). Without the raw log it prints only the sample check.

## Recommendations

Complementary products come from item-item co-purchase (`autonomy/catalog/recommend.py`): two products are related when the same customers buy them on one shopping trip (one basket, or baskets within 30 days of the trip's first one). Similarity is cosine over trip counts; each owned item votes for its neighbours, weighted by how recently it was bought, and the strongest voter is the reason ("Often bought with your Vitamin C serum"). Popular items in the customer's own categories fill any gap, always ranked below co-purchase picks, and a repeat category halves the next item's score so the list stays varied. `next_best_item` chooses a refill of the same item when its refill window is open (it reads the refill model's `CycleEstimate`: `key`, `level`, `window_open`, `days_until_due`), and a complementary item when it is not; its `item_id` is what goes in `Action.item_id`. No randomness, no clock, no LLM.

**Evaluation.** Online Retail log (UCI, 2010-12-01 to 2011-12-09): 386,326 product lines after cleaning, 4,334 customers, 3,659 products; the model before the split has 7,430 trips and 127,391 item-to-item links. Time-based split: the model learns from purchases on or before 2011-09-10 and is tested on the 90 days after. A hit is a recommended product the customer had never bought before and then bought in the test window, unprompted. Everyone's owned items are excluded, the baseline included. The baseline recommends the products with the most distinct buyers in the 90 days before the split. Reproduce with `python library/online-retail/fetch.py` (downloads the log) and then `python -m autonomy.catalog.evaluate`.

**All customers with a new product in the test window**

| Method | k | hit-rate@k | recall@k | customers |
|---|---:|---:|---:|---:|
| co-purchase (cosine) | 5 | 34.7% | 2.7% | 1,858 |
| co-purchase (cosine) | 10 | 46.2% | 4.4% | 1,858 |
| co-purchase (cosine) | 20 | 60.6% | 7.0% | 1,858 |
| co-purchase (lift) | 5 | 11.6% | 0.8% | 1,858 |
| co-purchase (lift) | 10 | 18.8% | 1.6% | 1,858 |
| co-purchase (lift) | 20 | 30.0% | 2.6% | 1,858 |
| co-purchase, same basket only | 5 | 33.5% | 2.5% | 1,858 |
| co-purchase, same basket only | 10 | 44.9% | 4.3% | 1,858 |
| co-purchase, same basket only | 20 | 60.0% | 7.0% | 1,858 |
| popularity (baseline) | 5 | 22.0% | 1.0% | 1,858 |
| popularity (baseline) | 10 | 33.6% | 1.9% | 1,858 |
| popularity (baseline) | 20 | 45.0% | 3.4% | 1,858 |

**Win-back audience (sample.csv's lapsed customers)**

| Method | k | hit-rate@k | recall@k | customers |
|---|---:|---:|---:|---:|
| co-purchase (cosine) | 5 | 35.1% | 2.6% | 595 |
| co-purchase (cosine) | 10 | 48.4% | 4.7% | 595 |
| co-purchase (cosine) | 20 | 61.8% | 7.5% | 595 |
| popularity (baseline) | 5 | 26.2% | 1.4% | 595 |
| popularity (baseline) | 10 | 39.0% | 2.4% | 595 |
| popularity (baseline) | 20 | 49.4% | 4.1% | 595 |

**Notes**

- Speed, on one CPU: fitting on 230,771 lines took 1.7 s, and scoring all 3,368 customers (10 items each) took 2.9 s, about 1,143 customers a second.
- Defaults (30-day trip window, cosine, 90-day half-life, 50 neighbours) were set before looking at this test window; of the variants compared, a 365-day half-life scored about one point higher at k=5, too small a gain on one split to change the default. Lift favours rare items and loses to plain popularity, which is why cosine is the default.
- `library/online-retail/sample.csv` has no product lines (one aggregated row per lapsed customer), so the recommender is scored on the raw log it was built from; the sample supplies the win-back audience above.
- Non-product lines (POST, DOT, M, BANK CHARGES, ...) and cancellations are excluded; the log has no categories, so the diversity rule is idle on this data (it is covered by the unit tests).
- Synthetic check (planted pairs on top of popular noise, seed 7): co-purchase (cosine) hit-rate@1 55%; co-purchase (cosine) hit-rate@5 62%; popularity (baseline) hit-rate@1 18%; popularity (baseline) hit-rate@5 43%.
