"""Family B, review add-on: paired bootstrap intervals for co-purchase vs best-sellers.

`b_catalogue.py` reports hit-rate@k from `autonomy.catalog.evaluate.evaluate`, which returns only the
aggregate rates, so "beats", "ties" and "loses" in the notes had no uncertainty behind them. This
re-runs the same split per dataset (same cutoff and horizon as `b_catalogue.py`; the same population:
customers who bought before the cutoff and bought at least one new item in the test window), scores
the default co-purchase model (cosine) and the popularity baseline per customer exactly as
`evaluate` does, checks that the aggregate rates match `evaluate`'s, and reports the paired
difference (co-purchase minus best-sellers, in percentage points) with a 95% percentile bootstrap
interval over customers (2,000 resamples, seed 7).

The customers are not independent draws from a fixed population (one shop, one test window), so
the interval measures sampling noise among this window's customers only, not how a different
period or shop would come out.

Run:

    python3 -P scripts/v2_realdata/b_rec_ci.py ta_feng dunnhumby_nbr superstore

Results: /home/user/datasets/_results/b/rec_ci/<dataset>.json
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
from b_adapters import NBR_CUTOFF, adapt  # noqa: E402
from b_catalogue import split_for  # noqa: E402
from b_common import Clock, command_line, env_info, peak_rss_mb, write_result  # noqa: E402

from autonomy.catalog.evaluate import POPULARITY, _columns, _last_bought, evaluate  # noqa: E402
from autonomy.catalog.recommend import CoPurchaseConfig, CoPurchaseModel  # noqa: E402

KS = (5, 10, 20)


def paired(name: str, *, resamples: int = 2000, seed: int = 7) -> dict[str, Any]:
    t0 = time.perf_counter()
    a = adapt(name, Clock())
    b = a.baskets
    if b is None or b.empty:
        return {"dataset": name, "skipped": "no basket table"}
    if a.invented_time:
        cutoff, horizon = NBR_CUTOFF, timedelta(days=90)
    else:
        tmin, tmax = int(b["t"].min()), int(b["t"].max())
        cutoff, horizon = split_for((tmax - tmin) / 86400, tmax / 86400)
    t = b["t"].to_numpy(dtype=np.int64)
    lo, hi = int(cutoff.timestamp()), int((cutoff + horizon).timestamp())
    train, test = b[t <= lo], b[(t > lo) & (t <= hi)]
    owned = _last_bought(train)
    after: dict[str, set[str]] = {}
    for cid, item, _ in _columns(test):
        after.setdefault(cid, set()).add(item)
    targets = {c: items - set(owned[c]) for c, items in after.items() if c in owned and items - set(owned[c])}
    order = sorted(targets)
    model = CoPurchaseModel.fit(train, as_of=cutoff, config=CoPurchaseConfig())
    rec = [[r.item_id for r in model.recommend_items(owned[c], k=max(KS), now=cutoff)] for c in order]
    pop = [model.popular(max(KS), exclude=owned[c]) for c in order]
    reference = {
        (s.method, s.k): s.hit_rate
        for s in evaluate(
            b, cutoff=cutoff, horizon=horizon, configs={"co-purchase (cosine)": CoPurchaseConfig()}
        )
    }
    rng = np.random.default_rng(seed)
    n = len(order)
    idx = rng.integers(0, n, size=(resamples, n)) if n else np.zeros((0, 0), dtype=int)
    rows = []
    for k in KS:
        h_rec = np.array(
            [bool(set(r[:k]) & targets[c]) for r, c in zip(rec, order, strict=True)], dtype=float
        )
        h_pop = np.array(
            [bool(set(p[:k]) & targets[c]) for p, c in zip(pop, order, strict=True)], dtype=float
        )
        diff = h_rec - h_pop
        boot = diff[idx].mean(axis=1) if n else np.array([np.nan])
        rows.append(
            {
                "k": k,
                "customers": n,
                "cosine_hit_rate": round(float(h_rec.mean()), 4),
                "best_sellers_hit_rate": round(float(h_pop.mean()), 4),
                "matches_evaluate": bool(
                    abs(h_rec.mean() - reference[("co-purchase (cosine)", k)]) < 1e-9
                    and abs(h_pop.mean() - reference[(POPULARITY, k)]) < 1e-9
                ),
                "diff_points": round(100 * float(diff.mean()), 2),
                "diff_ci95_points": [
                    round(100 * float(np.quantile(boot, 0.025)), 2),
                    round(100 * float(np.quantile(boot, 0.975)), 2),
                ],
                "customers_only_cosine_hit": int(((h_rec == 1) & (h_pop == 0)).sum()),
                "customers_only_best_sellers_hit": int(((h_rec == 0) & (h_pop == 1)).sum()),
            }
        )
    return {
        "dataset": name,
        "command": command_line(),
        "env": env_info(),
        "invented_time": a.invented_time,
        "cutoff": cutoff,
        "horizon_days": horizon.days,
        "resamples": resamples,
        "seed": seed,
        "rows": rows,
        "seconds": round(time.perf_counter() - t0, 1),
        "peak_rss_mb": round(peak_rss_mb(), 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("datasets", nargs="+")
    args = parser.parse_args(argv)
    for name in args.datasets:
        payload = paired(name)
        path = write_result("rec_ci", name, payload)
        for r in payload.get("rows", []):
            print(
                f"{name} @{r['k']}: cosine {r['cosine_hit_rate']:.3f} vs best-sellers "
                f"{r['best_sellers_hit_rate']:.3f}, diff {r['diff_points']:+.2f} pts "
                f"[{r['diff_ci95_points'][0]:+.2f}, {r['diff_ci95_points'][1]:+.2f}] "
                f"(n={r['customers']}, matches evaluate: {r['matches_evaluate']})",
                flush=True,
            )
        print(f"{name}: wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
