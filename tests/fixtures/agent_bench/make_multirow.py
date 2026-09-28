"""A multi-row client file for level 3 (Plan G M76): a Retail Win-back order log, one row per order.

Each shopper has one to eight orders on or before the snapshot date, and every row repeats the
shopper's outcome (`reactivated_90d`), the way an export from an order system joined to a campaign
result looks. Shoppers who came back also have the orders that brought them back - dated *after*
the snapshot - which is exactly the answer: a combine that counted them would hand it to the model.

* `snapshot=True` (default) writes a `snapshot_date` column, the date each shopper is described as
  of, beside `order_date`;
* `snapshot=False` writes `order_date` only and leaves the post-snapshot orders out, so each
  shopper's latest order is their snapshot;
* `scoring=True` is next month's file: another draw, a later snapshot and no outcome column;
* `dates=False` drops every date column (the file cannot be combined).

Pure: the same arguments give the same frame.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["SNAPSHOT", "multirow_frame"]

SNAPSHOT = pd.Timestamp("2011-09-10")
_COUNTRIES = ("United Kingdom", "France", "Germany", "EIRE")


def multirow_frame(
    shoppers: int = 1_500,
    *,
    seed: int = 11,
    snapshot: bool = True,
    scoring: bool = False,
    dates: bool = True,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed + (1_000 if scoring else 0))
    as_of = SNAPSHOT + pd.Timedelta(days=30) if scoring else SNAPSHOT
    rows: list[dict[str, object]] = []
    for index in range(shoppers):
        shopper = f"S{index + (50_000 if scoring else 10_000):06d}"
        engaged = float(rng.random())
        orders = int(rng.integers(1, 9))
        country = str(rng.choice(_COUNTRIES, p=[0.7, 0.1, 0.1, 0.1]))
        chance = 1.0 / (1.0 + np.exp(-(-1.6 + 2.6 * engaged + 0.12 * orders)))
        came_back = int(rng.random() < chance)
        history = [int(rng.integers(0, 400)) for _ in range(orders)]
        future = [-int(rng.integers(1, 60))] if came_back and snapshot and not scoring else []
        for days_before in [*history, *future]:
            rows.append(
                {
                    "customer_id": shopper,
                    "snapshot_date": as_of.date().isoformat(),
                    "order_date": (as_of - pd.Timedelta(days=days_before)).date().isoformat(),
                    # a post-snapshot order is a big one: the leak must be loud if it gets in
                    "order_value": round(float(rng.gamma(2.0, 40.0)) * (0.5 + engaged), 2)
                    + (5_000.0 if days_before < 0 else 0.0),
                    "items": int(rng.integers(1, 30)),
                    "country": country,
                    "returned": int(rng.random() < 0.08),
                    "reactivated_90d": came_back,
                }
            )
    frame = pd.DataFrame(rows)
    order = np.random.default_rng(seed).permutation(len(frame))  # an export is not sorted by shopper
    frame = frame.iloc[order].reset_index(drop=True)
    if not snapshot:
        frame = frame.drop(columns=["snapshot_date"])
    if not dates:
        frame = frame.drop(columns=[c for c in ("snapshot_date", "order_date") if c in frame.columns])
    if scoring:
        frame = frame.drop(columns=["reactivated_90d"])
    return frame
