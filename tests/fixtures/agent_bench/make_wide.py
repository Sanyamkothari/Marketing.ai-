"""A wide client sheet for Plan L M156: one row per shopper, one column per month for each measure.

The shape of a hand-kept Retail Win-back sheet: `customer_id, country, 2024_01_spend … 2024_06_spend,
2024_01_orders … 2024_06_orders, reactivated_90d`. A shopper who joined after January has empty cells
for the months before. Shoppers who spent more in the last months come back more often, with noise, so
nothing predicts the outcome perfectly.

* `scoring=True` is next month's file: another draw of shoppers, the same columns, no outcome column;
* `extra_month=True` adds the next month's columns (`2024_07_spend`, `2024_07_orders`), as a person who
  keeps the sheet by hand would;
* `months` names the period part of each column (default `2024_01` … `2024_06`), so a test can write
  `Jan` … `Jun` (no year) instead;
* `period_first=False` writes `spend_2024_01` instead of `2024_01_spend`.

Pure: the same arguments give the same frame.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

__all__ = ["MONTHS", "wide_frame"]

MONTHS: tuple[str, ...] = ("2024_01", "2024_02", "2024_03", "2024_04", "2024_05", "2024_06")
_COUNTRIES = ("United Kingdom", "France", "Germany", "EIRE")


def wide_frame(
    shoppers: int = 1_500,
    *,
    seed: int = 7,
    scoring: bool = False,
    extra_month: bool = False,
    months: Sequence[str] = MONTHS,
    period_first: bool = True,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed + (1_000 if scoring else 0))
    periods = [*months, *(["2024_07"] if extra_month else [])]

    def name(period: str, measure: str) -> str:
        return f"{period}_{measure}" if period_first else f"{measure}_{period}"

    rows: list[dict[str, object]] = []
    for index in range(shoppers):
        engaged = float(rng.random())
        joined = int(rng.integers(0, 3))  # 0: from the first month; 1 or 2 months later
        row: dict[str, object] = {
            "customer_id": f"S{index + (50_000 if scoring else 10_000):06d}",
            "country": str(rng.choice(_COUNTRIES, p=[0.7, 0.1, 0.1, 0.1])),
        }
        recent = 0.0
        for position, period in enumerate(periods):
            if position < joined:
                row[name(period, "spend")] = None
                row[name(period, "orders")] = None
                continue
            orders = int(rng.poisson(0.5 + 2.5 * engaged))
            spend = round(float(rng.gamma(2.0, 30.0)) * orders, 2)
            row[name(period, "spend")] = spend
            row[name(period, "orders")] = orders
            if position >= len(periods) - 2:
                recent += orders
        if not scoring:
            chance = 1.0 / (1.0 + np.exp(-(-1.8 + 1.4 * engaged + 0.25 * recent)))
            row["reactivated_90d"] = int(rng.random() < chance)
        rows.append(row)
    frame = pd.DataFrame(rows)
    spend_columns = [name(p, "spend") for p in periods]
    order_columns = [name(p, "orders") for p in periods]
    outcome = [] if scoring else ["reactivated_90d"]
    return frame[["customer_id", "country", *spend_columns, *order_columns, *outcome]]
