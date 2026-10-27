"""The repeated-campaign benchmark (docs/v4/WAVES.md): real X5 customers and buying, a hidden message effect.

    python -m scripts.v2_realbench.waves --world heterogeneous --policy agent --seeds 3 --customers 30000 \\
        --data-dir DIR --json out.json
    python -m scripts.v2_realbench.waves --fixture --world null --policy send_all_sms --customers 400 --days 28
    python -m scripts.v2_realbench.waves --summarize parts/            # one table per world from CI's JSON files
    python -m scripts.v2_realbench.waves --list

`--fixture` builds a tiny synthetic X5-shaped receipts table instead of reading `DIR` (no download); its numbers
say nothing about real shoppers. Without it, the receipts are the cached X5 table that `scripts.v2_realbench.adapters`
builds from `purchases.csv.gz` (fetch it with `scripts.v2_realbench.fetch --dataset x5`). The customers are a fixed
hash-sample of the clients who held a card at the cutoff (point in time); the cutoff is 84 days before the last
receipt in the file. `--fixture-rate 0.17 --fixture-history-days 34` makes the fixture about as dense as X5.

`--json out.json` writes one row per world, policy and seed, and `out.npz` next to it with each run's true value
per customer (the summary job pairs policies on it). Both are rewritten after every run, so a job stopped by a time
limit keeps the runs it finished.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from autonomy.gym.waves.config import DEFAULT_CUSTOMERS, HORIZON_DAYS, MARGIN, WORLDS, get_world
from autonomy.gym.waves.data import (
    WavesData,
    build_waves_data,
    default_cutoff,
    eligible_clients,
    fixture_tables,
)
from autonomy.gym.waves.policies import policy_names
from autonomy.gym.waves.report import render_markdown, summarize
from autonomy.gym.waves.runner import run_one
from scripts.v2_realbench.adapters import SAMPLE_SALT, X5_MARGIN, x5_receipts
from scripts.v2_realbench.common import Timings, pick_by_hash

FloatArray = np.ndarray[Any, np.dtype[np.float64]]
FIXTURE_CUSTOMERS = 600
FIXTURE_RATE = 0.07
FIXTURE_HISTORY_DAYS = 60
SAMPLE = SAMPLE_SALT + ":waves"
assert X5_MARGIN == MARGIN, "the waves margin must be the bench's X5 margin"


def load_x5(data_dir: Path, customers: int, timings: Timings) -> WavesData:
    """A fixed hash-sample of the X5 clients who held a card at the cutoff (`eligible_clients`: point in time,
    whatever they bought later), as the benchmark's customers."""
    rec = x5_receipts(data_dir, timings)
    with timings.step(
        f"waves: sample {customers} clients holding a card at the cutoff, point-in-time features"
    ):
        cutoff = default_cutoff(rec)
        clients = pd.read_csv(data_dir / "x5_retailhero" / "clients.csv.gz")
        clients, counts = eligible_clients(rec, clients, cutoff)
        keep = pick_by_hash(clients["client_id"].astype(str).to_numpy(), customers, salt=SAMPLE)
        clients = clients.iloc[keep].reset_index(drop=True)
        chosen = set(clients["client_id"].astype(str))
        sub = rec[rec["client_id"].astype(str).isin(chosen)].reset_index(drop=True)
        del rec
        data = build_waves_data(sub, clients, cutoff=cutoff)
        return replace(data, eligibility={**counts, "sampled": data.n})


def describe(data: WavesData, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "customers": data.n,
        "cutoff": data.start.isoformat(),
        "pre_cutoff_days": round(data.pre_days, 1),
        "receipts_before_cutoff": len(data.hist_idx),
        "receipts_replayed": len(data.post_idx),
        "receipts_per_customer_day_after_cutoff": round(
            len(data.post_idx) / max(data.n * data.horizon_days, 1), 4
        ),
        "mean_base_rate_per_day": round(float(data.base_rate.mean()), 4),
        "email_consent_share": round(float(data.email_consent.mean()), 3),
        "eligibility": dict(data.eligibility),
    }


def run_all(
    data: WavesData,
    worlds: Sequence[str],
    policies: Sequence[str],
    seeds: Sequence[int],
    days: int,
    lift: float | None = None,
    after_each: Callable[[list[dict[str, Any]], dict[str, FloatArray]], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, FloatArray]]:
    """Every world x policy x seed. `after_each(rows, arrays)` is called after every run with all results so far,
    so a job stopped by a time limit still leaves the runs it finished."""
    rows: list[dict[str, Any]] = []
    arrays: dict[str, FloatArray] = {}
    for w in worlds:
        for p in policies:
            for s in seeds:
                spec = (
                    get_world(w)
                    if lift is None or w == "null"
                    else get_world(w).with_overrides(mean_lift=lift)
                )
                result = run_one(data, spec, p, s, days=days)
                rows.append(result.row)
                arrays[f"{w}|{p}|{s}"] = result.by_customer.astype(np.float32).astype(np.float64)
                r = result.row
                print(
                    f"{w} {p} seed {s}: true value {r['true_value']:,.0f} RUB, {r['contacts_per_customer_week']:.2f} "
                    f"contacts/customer-week, unsubscribed {r['unsubscribe_rate']:.1%}, measured {r['measured']:,.0f} "
                    f"({r['seconds']}s)",
                    flush=True,
                )
                for fit in (r.get("policy_diagnostics") or {}).get("fits", []):
                    print(f"  refit {json.dumps(fit)}", flush=True)
                if after_each is not None:
                    after_each(rows, arrays)
    return rows, arrays


def summarize_dir(directory: Path) -> str:
    """The markdown tables for every `*.json` waves report in `directory` (with its `.npz` where present)."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    per_customer: dict[tuple[str, str, int], FloatArray] = {}
    for path in sorted(directory.rglob("*.json")):
        report = json.loads(path.read_text())
        if report.get("benchmark") != "waves":
            continue
        for run in report["runs"]:
            key = (str(run["world"]), str(run["policy"]), int(run["seed"]))
            if key not in seen:  # the same run copied twice (a re-run's artifacts) counts once
                seen.add(key)
                rows.append(run)
        npz = path.with_suffix(".npz")
        if npz.exists():
            with np.load(npz) as z:
                for key in z.files:
                    w, p, s = key.split("|")
                    per_customer[(w, p, int(s))] = np.asarray(z[key], dtype=np.float64)
    if not rows:
        return "No waves report found.\n"
    return render_markdown(summarize(rows, per_customer))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The repeated-campaign benchmark on real X5 customers.")
    ap.add_argument("--world", default="heterogeneous", help=f"one of {', '.join(WORLDS)}, or a comma list")
    ap.add_argument("--policy", default="send_all_sms", help="a policy name, or a comma list (see --list)")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--first-seed", type=int, default=0)
    ap.add_argument(
        "--customers",
        type=int,
        default=None,
        help=f"default {DEFAULT_CUSTOMERS} ({FIXTURE_CUSTOMERS} with --fixture)",
    )
    ap.add_argument("--days", type=int, default=HORIZON_DAYS)
    ap.add_argument(
        "--lift", type=float, default=None, help="override the worlds' average relative lift (a power check)"
    )
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--fixture", action="store_true", help="a tiny synthetic table; no download")
    ap.add_argument(
        "--fixture-rate",
        type=float,
        default=FIXTURE_RATE,
        help="mean receipts per customer-day of the fixture (X5 has about 0.17)",
    )
    ap.add_argument("--fixture-history-days", type=int, default=FIXTURE_HISTORY_DAYS)
    ap.add_argument("--json", type=Path, default=None, metavar="PATH")
    ap.add_argument(
        "--summarize", type=Path, default=None, metavar="DIR", help="print the tables of a folder of reports"
    )
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    if args.list:
        print("Worlds:")
        for name, w in WORLDS.items():
            print(f"  {name}: {w.description}")
        print("Policies: " + ", ".join(policy_names()))
        return 0
    if args.summarize is not None:
        print(summarize_dir(args.summarize))
        return 0

    worlds = [w for w in args.world.split(",") if w]
    policies = [p for p in args.policy.split(",") if p]
    for w in worlds:
        get_world(w)
    unknown = [p for p in policies if p not in policy_names()]
    if unknown:
        ap.error(f"unknown policy {', '.join(unknown)}; known: {', '.join(policy_names())}")
    if args.days < 7 or args.days > HORIZON_DAYS:
        ap.error(f"--days must be 7..{HORIZON_DAYS}")
    started = time.perf_counter()
    timings = Timings()
    if args.fixture:
        n = args.customers or FIXTURE_CUSTOMERS
        receipts, clients, cutoff = fixture_tables(
            n, pre_days=args.fixture_history_days, rate_mean=args.fixture_rate
        )
        data = build_waves_data(receipts, clients, cutoff=cutoff)
        source = "fixture"
    else:
        if args.data_dir is None:
            ap.error("--data-dir is required without --fixture")
        data = load_x5(args.data_dir, args.customers or DEFAULT_CUSTOMERS, timings)
        source = "x5_retailhero"
    info = describe(data, source)
    print("data:", json.dumps(info), flush=True)
    seeds = list(range(args.first_seed, args.first_seed + args.seeds))
    command = "python -m scripts.v2_realbench.waves " + " ".join(sys.argv[1:] if argv is None else argv)

    def write(rows: list[dict[str, Any]], arrays: dict[str, FloatArray]) -> None:
        if args.json is None:
            return
        report: dict[str, Any] = {
            "benchmark": "waves",
            "command": command,
            "data": info,
            "seeds": seeds,
            "runs": rows,
            "wall_seconds": round(time.perf_counter() - started, 1),
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        np.savez_compressed(args.json.with_suffix(".npz"), **cast(dict[str, Any], arrays))

    rows, arrays = run_all(data, worlds, policies, seeds, args.days, args.lift, after_each=write)
    if args.json is not None:
        print(f"wrote {args.json} and {args.json.with_suffix('.npz')}")
    print()
    print(
        render_markdown(
            summarize(
                rows, {(k.split("|")[0], k.split("|")[1], int(k.split("|")[2])): v for k, v in arrays.items()}
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
