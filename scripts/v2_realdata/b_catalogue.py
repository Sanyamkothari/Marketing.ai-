"""Family B, stage 2: the catalogue engine on real transaction logs.

For each dataset with item-level lines (built by `b_adapters` through the product's own
`purchases_frame` / `basket_frame`), this runs what docs/v2/CATALOG.md ran on Online Retail I:

1. **Catalogue inference** (`Catalogue.infer`): how many items, how many look replenishable, their
   typical cycles.
2. **Refill timing** (`autonomy.catalog.cycle.backtest_last_interval`): hold out each repeat
   (customer, key) pair's last purchase, predict its gap; model vs the naive average gap. At item,
   category (when the data has categories) and order level.
3. **`p14` forward in time** (`calibrate_at_cutoff`): fit before a cutoff, did each pair buy in the
   next 14 days? Brier, log loss, AUC against the naive constant.
4. **Refill picks at the decision moment**: how many customers would get a refill score (and an
   open refill window) on the day the Shadow run decides (`CycleModel.score_trips`, the vectorised
   form of what `enrich_state` calls).
5. **Co-purchase recommender** (`autonomy.catalog.evaluate.evaluate`): time split, hit-rate@k and
   recall@k for new items vs the best-sellers baseline, plus fit and scoring speed.

On Online Retail I it also re-runs the CATALOG.md numbers from the repo's own cleaning
(`clean_retail_lines`) to check they reproduce, next to the numbers from the event path (the
loader's line events, which keep postage and fee lines).

Run:

    python3 -P scripts/v2_realdata/b_catalogue.py online_retail_i
    python3 -P scripts/v2_realdata/b_catalogue.py --all

Results: /home/user/datasets/_results/b/catalogue/<dataset>.json
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from b_adapters import DATASETS, FAMILY_B, adapt  # noqa: E402
from b_common import Clock, command_line, env_info, peak_rss_mb, write_result  # noqa: E402

from autonomy.catalog.cycle import CycleModel, backtest_last_interval, calibrate_at_cutoff  # noqa: E402
from autonomy.catalog.evaluate import _last_bought, evaluate  # noqa: E402
from autonomy.catalog.products import Catalogue, from_days, to_days, trips  # noqa: E402
from autonomy.catalog.recommend import CoPurchaseConfig, CoPurchaseModel  # noqa: E402

CONFIGS = {
    "co-purchase (cosine)": CoPurchaseConfig(),
    "co-purchase, same basket only": CoPurchaseConfig(window=timedelta(0)),
}


def _day_floor(days: float) -> datetime:
    d = from_days(math.floor(days))
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def catalogue_facts(purchases: pd.DataFrame) -> dict[str, Any]:
    cat = Catalogue.infer(purchases)
    rep = [p for p in cat if p.replenishable]
    cycles = [p.typical_cycle_days for p in rep if p.typical_cycle_days is not None]
    return {
        "items": len(cat),
        "replenishable_items": len(rep),
        "replenishable_share": round(len(rep) / max(len(cat), 1), 4),
        "typical_cycle_days_median": float(np.median(cycles)) if cycles else None,
        "categories": len(cat.categories()),
        "_catalogue": cat,
    }


def holdout(purchases: pd.DataFrame, level: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    r = backtest_last_interval(purchases, level=level)  # type: ignore[arg-type]
    return {
        "level": level,
        "pairs": r.n_pairs,
        "naive_interval_days": r.naive_interval_days,
        "mae_model": r.mae_model,
        "mae_naive_mean": r.mae_naive,
        "mae_naive_median": r.mae_naive_median,
        "mae_item_cycle_only": r.mae_prior_only,
        "median_ae_model": r.median_ae_model,
        "median_ae_naive": r.median_ae_naive,
        "coverage_80": r.coverage_80,
        "slices": [s.__dict__ for s in r.slices],
        "seconds": round(time.perf_counter() - t0, 2),
    }


def calibration(purchases: pd.DataFrame, cutoff: datetime, level: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    r = calibrate_at_cutoff(purchases, cutoff, level=level)  # type: ignore[arg-type]
    return {
        "level": level,
        "cutoff": cutoff,
        "pairs": r.n_pairs,
        "base_rate": r.base_rate,
        "brier_model": r.model.brier,
        "brier_naive": r.naive.brier,
        "logloss_model": r.model.log_loss,
        "logloss_naive": r.naive.log_loss,
        "auc": r.model.auc,
        "ece_model": r.model.ece,
        "ece_naive": r.naive.ece,
        "mean_p": r.model.mean_p,
        "window_open_pairs": r.window_open_n,
        "rate_window_open": r.window_open_rate,
        "rate_window_closed": r.window_closed_rate,
        "seconds": round(time.perf_counter() - t0, 2),
    }


def refill_picks(purchases: pd.DataFrame, catalogue: Catalogue, now: datetime) -> dict[str, Any]:
    t0 = time.perf_counter()
    model = CycleModel.fit(purchases, now, level="item", catalogue=catalogue)
    fit_s = time.perf_counter() - t0
    rep = set(catalogue.replenishable_items())
    rows = purchases[purchases["item_id"].isin(rep) & (purchases["day"] <= to_days(now))]
    customers = purchases["customer_id"].nunique()
    if rows.empty:
        return {"customers": customers, "with_refill_item": 0, "fit_seconds": round(fit_s, 2)}
    pace = model.customer_paces(purchases, now)
    scored = model.score_trips(trips(rows, "item_id"), now, pace=pace)
    best = scored.sort_values(["customer_id", "p"], ascending=[True, False]).drop_duplicates("customer_id")
    open_any = scored.groupby("customer_id")["window_open"].any()
    return {
        "customers": customers,
        "with_refill_item": int(best.shape[0]),
        "with_window_open": int(open_any.sum()),
        "best_p14": {
            "p50": round(float(best["p"].median()), 4),
            "p90": round(float(best["p"].quantile(0.9)), 4),
            "mean": round(float(best["p"].mean()), 4),
        },
        "best_days_until_due_median": round(float(best["days_until_due"].median()), 1),
        "fit_seconds": round(fit_s, 2),
        "score_seconds": round(time.perf_counter() - t0 - fit_s, 2),
    }


def recommender(baskets: pd.DataFrame, cutoff: datetime, horizon: timedelta) -> dict[str, Any]:
    t0 = time.perf_counter()
    scores = evaluate(baskets, cutoff=cutoff, horizon=horizon, configs=CONFIGS)
    eval_s = time.perf_counter() - t0
    train = baskets[baskets["t"].to_numpy(dtype=np.int64) <= int(cutoff.timestamp())]
    t1 = time.perf_counter()
    model = CoPurchaseModel.fit(train, as_of=cutoff)
    fit_s = time.perf_counter() - t1
    owned = _last_bought(train)
    sample = sorted(owned)[:5000]
    t2 = time.perf_counter()
    for cid in sample:
        model.recommend_items(owned[cid], k=10, now=cutoff)
    score_s = time.perf_counter() - t2
    return {
        "cutoff": cutoff,
        "horizon_days": horizon.days,
        "train_lines": len(train),
        "trips": model.n_trips,
        "item_links": len(model.neighbours.item),
        "scores": [s.__dict__ for s in scores],
        "fit_seconds": round(fit_s, 2),
        "customers_scored_for_speed": len(sample),
        "score_seconds": round(score_s, 2),
        "customers_per_second": round(len(sample) / max(score_s, 1e-9), 1),
        "evaluate_seconds": round(eval_s, 2),
    }


def split_for(span_days: float, last_day: float) -> tuple[datetime, timedelta]:
    """90-day test window on a year or more of data; 30 days on a short log (Ta-Feng: 4 months)."""
    horizon = timedelta(days=90) if span_days >= 270 else timedelta(days=30)
    end = _day_floor(last_day) + timedelta(days=1)
    return end - horizon, horizon


def run(name: str) -> dict[str, Any]:
    clock = Clock()
    t0 = time.perf_counter()
    a = adapt(name, clock)
    payload: dict[str, Any] = {
        "dataset": name,
        "command": command_line(),
        "env": env_info(),
        "invented_time": a.invented_time,
        "adapter_steps": clock.as_list(),
    }
    if a.purchases is not None and not a.purchases.empty:
        p = a.purchases
        has_items = bool(p["item_id"].notna().any())
        has_cats = bool(p["category"].notna().any())
        first, last = float(p["day"].min()), float(p["day"].max())
        span = last - first
        payload["purchase_lines"] = len(p)
        payload["span_days"] = round(span, 1)
        if has_items:
            facts = catalogue_facts(p)
            cat = facts.pop("_catalogue")
            payload["catalogue"] = facts
        levels = (["item"] if has_items else []) + (["category"] if has_cats else []) + ["order"]
        payload["holdout"] = [holdout(p, lvl) for lvl in levels]
        # Two forward cutoffs, 45 and 90 days before the end (one, 30 days before, on a short log).
        end = _day_floor(last) + timedelta(days=1)
        offsets = (90, 45) if span >= 270 else (30,)
        payload["p14"] = [calibration(p, end - timedelta(days=o), lvl) for o in offsets for lvl in levels]
        if has_items:
            payload["refill_at_decision"] = refill_picks(p, cat, end + timedelta(hours=10))
    if a.baskets is not None and not a.baskets.empty:
        b = a.baskets
        if a.invented_time:
            from b_adapters import NBR_CUTOFF

            cutoff, horizon = NBR_CUTOFF, timedelta(days=90)
        else:
            tmin, tmax = int(b["t"].min()), int(b["t"].max())
            cutoff, horizon = split_for((tmax - tmin) / 86400, tmax / 86400)
        payload["recommender"] = recommender(b, cutoff, horizon)
    if name == "online_retail_i":
        payload["catalog_md_reproduction"] = reproduce_catalog_md()
    payload["total_seconds"] = round(time.perf_counter() - t0, 2)
    payload["peak_rss_mb"] = round(peak_rss_mb(), 1)
    return payload


def reproduce_catalog_md() -> dict[str, Any]:
    """The CATALOG.md numbers from the repo's own cleaning, on the same file (2011-09-10 split)."""
    from autonomy.catalog.cycle import retail_log_purchases
    from autonomy.catalog.evaluate import DEFAULT_CUTOFF, DEFAULT_HORIZON, retail_purchases
    from autonomy.data.loaders import read_online_retail_log

    log = read_online_retail_log(DATASETS / "online_retail_i" / "online-retail-dataset.csv")
    baskets = retail_purchases(log)
    purchases = retail_log_purchases(log)
    rec = evaluate(baskets, cutoff=DEFAULT_CUTOFF, horizon=DEFAULT_HORIZON, configs=CONFIGS)
    return {
        "recommender_2011_09_10": [s.__dict__ for s in rec],
        "holdout_item": holdout(purchases, "item"),
        "p14_item_2011_09_01": calibration(purchases, datetime(2011, 9, 1, tzinfo=UTC), "item"),
        "lines": len(baskets),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("datasets", nargs="*")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    if args.all:
        code = 0
        for name in FAMILY_B:
            print(f"== {name}", flush=True)
            code |= subprocess.call([sys.executable, "-P", __file__, name])
        return code
    for name in args.datasets:
        payload = run(name)
        path = write_result("catalogue", name, payload)
        print(f"{name}: wrote {path} ({payload['total_seconds']} s, peak RSS {payload['peak_rss_mb']} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
