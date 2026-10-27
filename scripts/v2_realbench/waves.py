"""The repeated-campaign benchmark (docs/v4/WAVES.md): real X5 customers and buying, a hidden message effect.

    python -m scripts.v2_realbench.waves --world heterogeneous --policy agent --seeds 3 --customers 30000 \\
        --data-dir DIR --json out.json
    python -m scripts.v2_realbench.waves --fixture --world null --policy send_all_sms --customers 400 --days 28
    python -m scripts.v2_realbench.waves --summarize parts/            # one table per world from CI's JSON files
    python -m scripts.v2_realbench.waves --list

`--fixture` builds a tiny synthetic X5-shaped receipts table instead of reading `DIR` (no download); its numbers
say nothing about real shoppers. Without it, the receipts are the cached X5 table that `scripts.v2_realbench.adapters`
builds from `purchases.csv.gz` (fetch it with `scripts.v2_realbench.fetch --dataset x5`). The customers are a fixed
hash-sample of the clients that have a receipt; the cutoff is 84 days before the last receipt in the file.

`--json out.json` writes one row per world, policy and seed, and `out.npz` next to it with each run's true value
per customer (the summary job pairs policies on it).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from autonomy.gym.waves.config import DEFAULT_CUSTOMERS, HORIZON_DAYS, MARGIN, WORLDS, get_world
from autonomy.gym.waves.data import WavesData, build_waves_data, default_cutoff, fixture_tables
from autonomy.gym.waves.policies import policy_names
from autonomy.gym.waves.report import render_markdown, summarize
from autonomy.gym.waves.runner import run_one
from scripts.v2_realbench.adapters import SAMPLE_SALT, X5_MARGIN, x5_receipts
from scripts.v2_realbench.common import Timings, pick_by_hash

FIXTURE_CUSTOMERS = 600
SAMPLE = SAMPLE_SALT + ":waves"
assert X5_MARGIN == MARGIN, "the waves margin must be the bench's X5 margin"


def load_x5(data_dir: Path, customers: int, timings: Timings) -> WavesData:
    """A fixed hash-sample of X5 clients with at least one receipt, as the benchmark's customers."""
    rec = x5_receipts(data_dir, timings)
    with timings.step(f"waves: sample {customers} clients with a receipt, point-in-time features"):
        cutoff = default_cutoff(rec)
        clients = pd.read_csv(data_dir / "x5_retailhero" / "clients.csv.gz")
        have = set(rec["client_id"].astype(str).unique())
        clients = clients[clients["client_id"].astype(str).isin(have)].reset_index(drop=True)
        keep = pick_by_hash(clients["client_id"].astype(str).to_numpy(), customers, salt=SAMPLE)
        clients = clients.iloc[keep].reset_index(drop=True)
        chosen = set(clients["client_id"].astype(str))
        sub = rec[rec["client_id"].astype(str).isin(chosen)].reset_index(drop=True)
        del rec
        return build_waves_data(sub, clients, cutoff=cutoff)


def describe(data: WavesData, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "customers": data.n,
        "cutoff": data.start.isoformat(),
        "pre_cutoff_days": round(data.pre_days, 1),
        "receipts_before_cutoff": len(data.hist_idx),
        "receipts_replayed": len(data.post_idx),
        "mean_base_rate_per_day": round(float(data.base_rate.mean()), 4),
        "email_consent_share": round(float(data.email_consent.mean()), 3),
    }


def run_all(
    data: WavesData, worlds: Sequence[str], policies: Sequence[str], seeds: Sequence[int], days: int
) -> tuple[list[dict[str, Any]], dict[str, np.ndarray[Any, np.dtype[np.float64]]]]:
    rows: list[dict[str, Any]] = []
    arrays: dict[str, np.ndarray[Any, np.dtype[np.float64]]] = {}
    for w in worlds:
        for p in policies:
            for s in seeds:
                result = run_one(data, get_world(w), p, s, days=days)
                rows.append(result.row)
                arrays[f"{w}|{p}|{s}"] = result.by_customer.astype(np.float32).astype(np.float64)
                r = result.row
                print(
                    f"{w} {p} seed {s}: true value {r['true_value']:,.0f} RUB, {r['contacts_per_customer_week']:.2f} "
                    f"contacts/customer-week, unsubscribed {r['unsubscribe_rate']:.1%}, measured {r['measured']:,.0f} "
                    f"({r['seconds']}s)",
                    flush=True,
                )
    return rows, arrays


def summarize_dir(directory: Path) -> str:
    """The markdown tables for every `*.json` waves report in `directory` (with its `.npz` where present)."""
    rows: list[dict[str, Any]] = []
    per_customer: dict[tuple[str, str, int], np.ndarray[Any, np.dtype[np.float64]]] = {}
    for path in sorted(directory.rglob("*.json")):
        report = json.loads(path.read_text())
        if report.get("benchmark") != "waves":
            continue
        rows.extend(report["runs"])
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
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--fixture", action="store_true", help="a tiny synthetic table; no download")
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
        receipts, clients, cutoff = fixture_tables(n)
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
    rows, arrays = run_all(data, worlds, policies, seeds, args.days)
    report: dict[str, Any] = {
        "benchmark": "waves",
        "command": "python -m scripts.v2_realbench.waves " + " ".join(sys.argv[1:] if argv is None else argv),
        "data": info,
        "seeds": seeds,
        "runs": rows,
        "wall_seconds": round(time.perf_counter() - started, 1),
    }
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        np.savez_compressed(args.json.with_suffix(".npz"), **cast(dict[str, Any], arrays))
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
