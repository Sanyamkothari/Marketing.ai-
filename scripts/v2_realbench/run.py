"""The real-data scoreboard: one command, a fixed JSON report.

    python -m scripts.v2_realbench.run --dataset all --data-dir /home/user/datasets --json out.json
    python -m scripts.v2_realbench.run --dataset hillstrom --data-dir /home/user/datasets --json h.json
    python -m scripts.v2_realbench.run --dataset hillstrom --limit 6000 --json quick.json   # smoke only
    python -m scripts.v2_realbench.run --render out.json          # the markdown tables of a report

Run from the repo root. For each dataset: the bench adapter (scripts/v2_realbench/adapters.py) turns
the file into Autonomy's inputs, the split is by customer id hash (fixed salt), the agent is built
and warm-started through the product path on TRAIN and decides for TEST, the baselines are fitted
on TRAIN, and everything is scored on TEST with autonomy.proof (see common.py and
docs/v2/REAL_BENCH.md). `--limit` changes the fixed sizes and marks the report as not comparable.
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from scripts.v2_realbench.adapters import DATASETS, SIZES, load
from scripts.v2_realbench.common import BENCH_VERSION, Timings, evaluate_dataset, peak_rss_mb

REPO = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _autonomy_location() -> str:
    import autonomy

    return str(Path(autonomy.__file__).resolve().parent)


def _default(o: object) -> object:
    if isinstance(o, np.integer | np.floating):
        return o.item()
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def _fmt(e: Mapping[str, float] | None, digits: int = 4) -> str:
    if e is None:
        return "n/a"
    return f"{e['point']:+.{digits}f} [{e['low']:+.{digits}f}, {e['high']:+.{digits}f}]"


def _digits(block: Mapping[str, Any]) -> int:
    scale = max(abs(float(p["dr"]["point"])) for p in block["policies"].values())
    return 4 if scale < 10 else 2


def render(report: Mapping[str, Any]) -> str:
    """The scoreboard as markdown: one summary table, then each dataset's policy table."""
    lines = [
        "| Dataset | Agent value (DR) | Agent contact rate | Best simple policy (chosen on train) | "
        "Agent minus best simple [95% CI] | Agent minus send-to-all [95% CI] | Expected vs measured per contact |",
        "|---|---:|---:|---|---:|---:|---:|",
    ]
    for name, b in report["datasets"].items():
        if "error" in b:
            lines.append(f"| {name} | error: {b['error']} | | | | | |")
            continue
        d = _digits(b)
        cal = b["calibration"]
        meas = cal.get("measured_per_contact")
        exp = cal.get("expected_per_contact")
        cal_txt = (
            "contacts nobody"
            if meas is None
            else f"{exp:.4g} vs {meas['point']:.4g} [{meas['low']:.4g}, {meas['high']:.4g}]"
            + (f" (x{cal['ratio']:.1f})" if cal.get("ratio") is not None else "")
        )
        lines.append(
            f"| {name} | {b['agent']['value']['dr']['point']:.{d}f} | {b['agent']['contact_rate']:.1%} | "
            f"{b['best_simple']['name']} | {_fmt(b['best_simple']['agent_minus_best_simple'], d)} | "
            f"{_fmt(b['send_to_all']['agent_minus_send_to_all'], d)} | {cal_txt} |"
        )
    for name, b in report["datasets"].items():
        if "error" in b:
            continue
        d = _digits(b)
        lines += [
            "",
            f"### {name}: profit per customer on TEST ({b['test']:,} of {b['rows']:,}; {b['economics']})",
            "",
            "| Policy | Contacted | IPS | SNIPS | DR [95% CI] | DR gain vs send-to-none | Agent minus this [95% CI] |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for pname, p in b["policies"].items():
            sn = p["snips"]["point"] if p["snips"] else float("nan")
            lines.append(
                f"| {pname} | {p['contact_rate']:.1%} | {p['ips']['point']:.{d}f} | {sn:.{d}f} | "
                f"{_fmt(p['dr'], d)} | {_fmt(p['dr_vs_none'], d)} | {_fmt(p['agent_minus_this'], d)} |"
            )
        a = b["agent"]
        lines.append("")
        lines.append(
            f"Agent: decisions {a['kinds']}, warm start used {a['warm_start_sends_used']} logged sends "
            f"from {a['history_customers']:,} train customers ({a['history_events']:,} events), "
            f"{a['ms_per_decision']} ms per decision, {a['seeds']} seeds. Verdict: {b['verdict']}."
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Autonomy's real-data scoreboard (randomised campaigns).")
    ap.add_argument("--dataset", choices=[*DATASETS, "all"], default="all")
    ap.add_argument("--data-dir", type=Path, default=Path("/home/user/datasets"))
    ap.add_argument("--json", type=Path, help="write the report here")
    ap.add_argument("--seeds", type=int, default=4, help="agent decisions per test customer (fixed: 4)")
    ap.add_argument("--limit", type=int, default=0, help="smoke runs only: fewer customers (not comparable)")
    ap.add_argument(
        "--with-decision-log",
        action="store_true",
        help="also hand the agent the past campaign as decisions (ACT / holdout NOTHING) in nightly()",
    )
    ap.add_argument("--no-features", action="store_true", help="do not put customer features in states")
    ap.add_argument("--render", type=Path, help="print the markdown tables of an existing report and exit")
    ap.add_argument("--note", default="", help="free text kept in the report (e.g. which code was run)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.render:
        print(render(json.loads(args.render.read_text())))
        return 0
    names = list(DATASETS) if args.dataset == "all" else [args.dataset]
    started = time.perf_counter()
    report: dict[str, Any] = {
        "bench_version": BENCH_VERSION,
        "command": " ".join(
            ["python -m scripts.v2_realbench.run", *(argv if argv is not None else sys.argv[1:])]
        ),
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git": {
            "commit": _git("rev-parse", "HEAD"),
            "dirty": bool(_git("status", "--porcelain", "--", "autonomy")),
        },
        "autonomy_package": _autonomy_location(),
        "python": platform.python_version(),
        "note": args.note,
        "settings": {
            "seeds": args.seeds,
            "limit": args.limit or None,
            "fixed_sizes": not args.limit and args.seeds == 4,
            "sizes": SIZES,
            "with_decision_log": args.with_decision_log,
            "features_attached": not args.no_features,
        },
        "datasets": {},
    }
    for name in names:
        timings = Timings(quiet=args.quiet)
        t0 = time.perf_counter()
        print(f"\n===== {name} =====", flush=True)
        try:
            data = load(name, args.data_dir, timings, limit=args.limit or None)
            block = evaluate_dataset(
                data,
                seeds=args.seeds,
                timings=timings,
                with_decision_log=args.with_decision_log,
                attach_features=not args.no_features,
            )
            del data
        except MemoryError as err:  # keep the other datasets' numbers
            block = {"error": f"MemoryError: {err}"}
        block["seconds"] = round(time.perf_counter() - t0, 1)
        block["peak_rss_mb"] = round(peak_rss_mb(), 1)
        block["timings"] = timings.rows
        report["datasets"][name] = block
        gc.collect()
        if "error" not in block:
            v = block["verdict"]
            print(
                f"{name}: agent DR {block['agent']['value']['dr']['point']:.4f}, contacts "
                f"{block['agent']['contact_rate']:.1%}; vs best simple ({block['best_simple']['name']}) "
                f"{_fmt(block['best_simple']['agent_minus_best_simple'])}; vs send-to-all "
                f"{_fmt(block['send_to_all']['agent_minus_send_to_all'])}; calibration ratio "
                f"{block['calibration'].get('ratio')}; verdict {v}",
                flush=True,
            )
        if args.json:  # write after every dataset, so a crash keeps what finished
            args.json.write_text(json.dumps(report, indent=1, default=_default))
    report["seconds"] = round(time.perf_counter() - started, 1)
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, default=_default))
        print(f"\nreport written to {args.json}")
    print("\n" + render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
