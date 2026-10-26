"""Fingerprint the scale scenario for every combination of its options, to compare two checkouts.

    python -m tests.v3.scale.fingerprint_matrix OUT.json [--customers 300] [--batches unset,none,1,7,64,250]
    python -m tests.v3.scale.fingerprint_matrix --compare OLD.json NEW.json

The first form runs `one_contact_per_tick` on/off x mandate none/paced/violation x state enricher
off/on x send-time gateway off/on, plus the group assigner with mandate none/paced, at each batch
size, and writes a sha256 of each fingerprint (and one with the dispatch results sorted). `unset`
builds the config without `sweep_batch`, so it also runs on a checkout from before the setting
existed (copy `tests/v3/scale/` there). `--compare` takes OLD's `unset` run as the reference for
every batch size in NEW and prints which differ, and whether they differ only in the order of the
dispatch results. `docs/v3/SCALE.md` says what was compared.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import time
from pathlib import Path
from typing import Any

from tests.v3.scale import scenario as sc


def combos() -> list[dict[str, Any]]:
    out = [
        {"one_contact": one, "mandate_kind": m, "enrich": e, "gateway": g, "groups": False}
        for one, m, e, g in itertools.product(
            (True, False), ("none", "paced", "violation"), (False, True), (False, True)
        )
    ]
    out += [
        {"one_contact": True, "mandate_kind": m, "enrich": False, "gateway": False, "groups": True}
        for m in ("none", "paced")
    ]
    return out


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def run(n: int, batches: list[str]) -> dict[str, dict[str, dict[str, str]]]:
    results: dict[str, dict[str, dict[str, str]]] = {}
    for batch in batches:
        size = sc.UNSET if batch == "unset" else (None if batch == "none" else int(batch))
        for kw in combos():
            s = sc.build(n, sweep_batch=size, **kw)
            fp = sc.fingerprint(s, sc.play(s))
            unordered = {**fp, "report_dispatch": [sorted(map(tuple, r)) for r in fp["report_dispatch"]]}
            key = " ".join(f"{k}={v}" for k, v in kw.items())
            results.setdefault(key, {})[batch] = {"full": _sha(fp), "unordered": _sha(unordered)}
    return results


def compare(old: dict[str, Any], new: dict[str, Any]) -> None:
    runs = same = order_only = 0
    for key, by_batch in sorted(new.items()):
        ref = old[key]["unset"]
        for batch, got in sorted(by_batch.items()):
            runs += 1
            if got["full"] == ref["full"]:
                same += 1
            elif got["unordered"] == ref["unordered"]:
                order_only += 1
                print(f"dispatch order only: batch={batch} {key}")
            else:
                print(f"DIFFERENT: batch={batch} {key}")
    print(f"{runs} runs: {same} identical, {order_only} differ only in the order of the dispatch results")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", nargs="?")
    parser.add_argument("--customers", type=int, default=300)
    parser.add_argument("--batches", default="unset,none,1,7,64,250")
    parser.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"))
    args = parser.parse_args()
    if args.compare:
        old, new = (json.loads(Path(p).read_text()) for p in args.compare)
        compare(old, new)
        return
    t0 = time.perf_counter()
    results = run(args.customers, args.batches.split(","))
    Path(args.out).write_text(json.dumps(results, indent=1, sort_keys=True))
    print(f"{sum(len(v) for v in results.values())} runs in {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
