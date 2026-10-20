"""Family C, scale 4: how far `ContactHarm.refit` (the nightly contact-harm model) scales.

`ContactHarm.refit` (`autonomy/decide/bidders.py`) fits every customer with a contact record at
once, on a 241-point ratio grid: several `n x 241` float64 arrays (about 1.9 KB per customer each)
are alive together (the log-likelihood grid, its cumulative sum and posteriors in `_neighbours`, the
likelihood and per-tau priors in `_normal_on_grid`). The sweep runner (`c_sweep.py --nightly`) times
it inside the real nightly step up to the sizes a full sweep fits in memory; this runner calls it
directly on N synthetic records to find where it breaks, in one fresh process per size.

The records are **synthetic, for timing and memory only** (nothing is concluded from the fit): per
customer a send count (Poisson 1.5), 7 exposed days per send, 30 to 700 open days, purchases at a
gamma-distributed daily rate with no effect of messages, 10% holdout, five segments, half of the
record after the agent's start.

Run:

    python3 -P scripts/v2_realdata/c_harm.py --customers 100000
    python3 -P scripts/v2_realdata/c_harm.py --curve 10000,100000,300000,1000000
    python3 -P scripts/v2_realdata/c_harm.py --customers 100000 --profile

Results: /home/user/datasets/_results/c/harm/harm_<N>.json
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
from c_common import Clock, command_line, current_rss_mb, env_info, peak_rss_mb, write_result  # noqa: E402

from autonomy.decide.bidders import ContactHarm  # noqa: E402
from autonomy.learning.observations import ContactEvidence  # noqa: E402

SPLIT = datetime(2026, 3, 2, tzinfo=UTC)


def records(n: int, seed: int) -> tuple[dict[str, ContactEvidence], set[str], dict[str, str]]:
    rng = np.random.default_rng(seed)
    sends = rng.poisson(1.5, n).astype(float)
    exposed_d = sends * 7.0
    open_d = rng.uniform(30, 700, n)
    rate = rng.gamma(0.8, 0.01 / 0.8, n)
    exposed_p = rng.poisson(rate * exposed_d).astype(float)
    open_p = rng.poisson(rate * open_d).astype(float)
    clicks = rng.binomial(sends.astype(int), 0.03).astype(float)
    visits = rng.poisson(rate * (exposed_d + open_d) * 3).astype(float)
    ev: dict[str, ContactEvidence] = {}
    holdout: set[str] = set()
    segs: dict[str, str] = {}
    names = ("lapsed", "vip", "active", "lapsing", "new")
    seg_pick = rng.integers(0, len(names), n)
    for i in range(n):
        cid = f"h{i:07d}"
        post_xd = exposed_d[i] * 0.8
        post_d = post_xd + open_d[i] * 0.1
        ev[cid] = ContactEvidence(
            exposed_days=float(exposed_d[i]),
            exposed_purchases=float(exposed_p[i]),
            open_days=float(open_d[i]),
            open_purchases=float(open_p[i]),
            sends=float(sends[i]),
            clicks=float(clicks[i]),
            visits=float(visits[i]),
            direct_visits=float(visits[i]) * 0.7,
            split_at=SPLIT,
            post_days=float(post_d),
            post_exposed_days=float(post_xd),
            post_purchases=float(rng.poisson(rate[i] * post_d)),
            post_exposed_purchases=float(min(exposed_p[i], rng.poisson(rate[i] * post_xd))),
        )
        if i % 10 == 0:
            holdout.add(cid)
        segs[cid] = names[seg_pick[i]]
    return ev, holdout, segs


def run(n: int, seed: int, profile: bool = False) -> None:
    clock = Clock()
    rss0 = current_rss_mb()
    with clock.step("records") as s:
        ev, holdout, segs = records(n, seed)
        s.extra["customers"] = len(ev)
    rss1 = current_rss_mb()
    harm = ContactHarm()
    harm.split_at = SPLIT
    harm.set_groups(holdout=holdout, segments=segs)
    harm.observe(ev)
    peak_before = peak_rss_mb()
    prof = None
    if profile:
        import cProfile

        prof = cProfile.Profile()
    with clock.step("refit") as s:
        if prof:
            prof.enable()
        harm.refit()
        if prof:
            prof.disable()
        s.extra["fitted"] = harm.fitted
    refit = clock.steps[-1]
    payload = {
        "customers": n,
        "command": command_line(),
        "env": env_info(),
        "synthetic": "records invented for timing and memory only",
        "records_mb_per_1000": round((rss1 - rss0) / n * 1000, 2),
        "refit_seconds": refit.seconds,
        "peak_rss_before_refit_mb": round(peak_before, 1),
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "refit_extra_peak_mb": round(peak_rss_mb() - peak_before, 1),
        "refit_extra_peak_kb_per_customer": round((peak_rss_mb() - rss1) * 1000 / n, 2),
        "fit": {"ratio": harm.fit.ratio, "customers": harm.fit.customers} if harm.fit else None,
        "steps": clock.as_dict(),
        "profiled": profile,
    }
    if prof:
        import pstats

        stats = pstats.Stats(prof).stats  # type: ignore[attr-defined]
        top = sorted(stats.items(), key=lambda kv: -kv[1][3])[:15]
        payload["profile_by_cumtime"] = [
            {
                "function": f"{f.split('/')[-1]}:{line}:{fn}",
                "calls": v[1],
                "tottime": round(v[2], 3),
                "cumtime": round(v[3], 3),
            }
            for (f, line, fn), v in top
        ]
    path = write_result("harm", f"harm_{n}" + ("_profile" if profile else ""), payload)
    print(f"wrote {path}: refit {refit.seconds} s, peak RSS {payload['peak_rss_mb']} MB", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--customers", type=int, default=10_000)
    parser.add_argument("--curve", default=None)
    parser.add_argument("--seed", type=int, default=5)
    parser.add_argument(
        "--profile", action="store_true", help="cProfile the refit (slower; names the hot spots)"
    )
    args = parser.parse_args(argv)
    if args.curve:
        code = 0
        for size in args.curve.split(","):
            print(f"== harm customers={size}", flush=True)
            t0 = time.perf_counter()
            rc = subprocess.call(
                [sys.executable, "-P", __file__, "--customers", size, "--seed", str(args.seed)]
            )
            if rc != 0:
                print(
                    f"   exit code {rc} after {time.perf_counter() - t0:.1f} s (a negative code is the signal)",
                    flush=True,
                )
            code |= rc
        return code
    run(args.customers, args.seed, args.profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
