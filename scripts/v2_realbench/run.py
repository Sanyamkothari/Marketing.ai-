"""The real-data scoreboard: one command, a fixed JSON report.

    python -m scripts.v2_realbench.run --dataset all --data-dir /home/user/datasets --json out.json
    python -m scripts.v2_realbench.run --dataset hillstrom --data-dir /home/user/datasets --json h.json
    python -m scripts.v2_realbench.run --dataset hillstrom --limit 6000 --json quick.json   # smoke only
    python -m scripts.v2_realbench.run --render out.json          # the markdown tables of a report
    python -m scripts.v2_realbench.run --dataset hillstrom --policy scientist ...   # the Scientist's pick
    python -m scripts.v2_realbench.run --dataset hillstrom --policy agent_v3 ...    # the plan-v3 preset
    python -m scripts.v2_realbench.run --dataset x5 --policy v3_no_journeys ...     # an ablation variant
    python -m scripts.v2_realbench.run --dataset x5 --policy uplift_threshold ...   # docs/v4/METHOD.md
    python -m scripts.v2_realbench.run --dataset lenta --sealed-final ...   # SEALED: once, at the end

The sealed datasets (lenta, starbucks) are refused unless `--sealed-final` is passed, are never part of
`--dataset all`, and each sealed run is recorded (date, git sha) in docs/v2/REAL_BENCH.md before the
data is read. See scripts/v2_realbench/sealed.py.

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

from autonomy.agents.llm_exchange import AnswerPending, RecordingLLM
from autonomy.runtime import ablation
from scripts.v2_realbench.adapters import DATASETS, SIZES, load
from scripts.v2_realbench.ai_loop import AiSetup, CountingReplay
from scripts.v2_realbench.common import (
    BENCH_VERSION,
    LEARNED_BASELINES,
    Timings,
    add_multiplicity,
    evaluate_dataset,
    peak_rss_mb,
)
from scripts.v2_realbench.scientist import evaluate_scientist, render_scientist
from scripts.v2_realbench.sealed import DEFAULT_LOG, SEALED_DATASETS, record_sealed_run, refusal

REPO = Path(__file__).resolve().parents[2]
UPLIFT_POLICY = "uplift_threshold"


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
    # the learned rows are left out of the scale, so a report prints with the same decimals as before them
    scale = max(
        abs(float(p["dr"]["point"])) for name, p in block["policies"].items() if name not in LEARNED_BASELINES
    )
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
        learned = b.get("best_learned_baseline")
        if learned:
            lines += [
                "",
                f"Best learned baseline (DR-learner or depth-2 policy tree; chosen on TEST, so it flatters it; "
                f"not part of best simple): {learned['name']}, DR {_fmt(learned['dr'], d)}; "
                f"agent minus it {_fmt(learned['agent_minus'], d)}.",
            ]
        a = b["agent"]
        lines.append("")
        lines.append(
            f"Agent: decisions {a['kinds']}, warm start used {a['warm_start_sends_used']} logged sends "
            f"from {a['history_customers']:,} train customers ({a['history_events']:,} events), "
            f"{a['ms_per_decision']} ms per decision, {a['seeds']} seeds. Verdict: {b['verdict']}."
        )
    if report.get("multiplicity", {}).get("comparisons"):
        lines += ["", render_multiplicity(report)]
    return "\n".join(lines)


def _p(x: float) -> str:
    return f"{x:.4f}" if x >= 1e-3 else f"{x:.1e}"


def render_multiplicity(report: Mapping[str, Any]) -> str:
    """The Holm-Bonferroni table: every comparison with its raw and corrected interval and p."""
    mult = report["multiplicity"]
    lines = [
        f"### Multiple comparisons ({mult['m']} in the family: {mult['family']})",
        "",
        f"{mult['method']}, alpha {mult['alpha']}. The corrected interval is at level "
        f"1 - alpha/m = {1 - mult['alpha'] / mult['m']:.4f}.",
        "",
        "| Comparison | Raw diff [95% CI] | Raw p | Holm p | Corrected interval | Significant (raw / Holm) |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for key, c in mult["comparisons"].items():
        raw, cor = c["raw"], c["corrected"]
        lines.append(
            f"| {key} | {_fmt(raw, 4)} | {_p(raw['p'])} | {_p(c['holm_p'])} | {_fmt(cor, 4)} | "
            f"{'yes' if c['significant_raw'] else 'no'} / {'yes' if c['significant_holm'] else 'no'} |"
        )
    lines += [
        "",
        "| Dataset | Best simple policy | Corrected low vs best | Tolerance | Non-inferior | Beats it where features carry signal | Calibration within 2x |",
        "|---|---|---:|---:|---|---|---|",
    ]
    for name, b in report["datasets"].items():
        v = b.get("verdict_corrected")
        if not v:
            continue
        sig = v["beats_best_simple_where_signal"]
        lines.append(
            f"| {name} | {v['best_simple']} | {v['corrected_low_vs_best']:+.4g} | {v['tolerance']:.4g} | "
            f"{'yes' if v['non_inferior_to_best_simple'] else 'no'} | "
            f"{'n/a (no signal shown)' if sig is None else ('yes' if sig else 'no')} | {v['calibration_within_2x']} |"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Autonomy's real-data scoreboard (randomised campaigns).")
    ap.add_argument("--dataset", choices=[*DATASETS, *SEALED_DATASETS, "all"], default="all")
    ap.add_argument(
        "--sealed-final",
        action="store_true",
        help="open a SEALED dataset (lenta, starbucks): once, at the end; recorded in the Sealed runs table",
    )
    ap.add_argument(
        "--sealed-log",
        type=Path,
        default=DEFAULT_LOG,
        help="the document whose 'Sealed runs' table records it",
    )
    ap.add_argument("--data-dir", type=Path, default=Path("/home/user/datasets"))
    ap.add_argument("--json", type=Path, help="write the report here")
    ap.add_argument(
        "--policy",
        choices=(*ablation.names(), UPLIFT_POLICY, "scientist"),
        default="agent",
        metavar="POLICY",
        help="agent: score the learning agent (the scoreboard of record); agent_v3: the same agent built "
        "with the plan-v3 preset (docs/v3/PRESET.md), its who book handed every customer's pre-campaign "
        "events, scored the same way; scientist: the policy the Scientist selects on TRAIN from the fixed "
        "menu (autonomy/scientist), scored on TEST; any other name is an ablation variant of the agent "
        "(v3_no_<part>, agent_<part>, agent_no_<guard>, agent_loose_caps; docs/v3/DIAGNOSE.md), scored the "
        "same way, with a DIAG block of how it decided. One of: "
        + ", ".join(ablation.names())
        + f", {UPLIFT_POLICY} (a cross-fitted uplift model with a cost threshold, gated against the best simple "
        "rule: docs/v4/METHOD.md), scientist",
    )
    ap.add_argument(
        "--ai-answers",
        type=Path,
        help="scientist runs only: JSON {prompt id: answer} of recorded Sonnet answers; adds the AI loop "
        "(Data Doctor, Proposer, Judge) to the Scientist and scores its pick on the same TEST",
    )
    ap.add_argument(
        "--ai-record",
        type=Path,
        help="where a prompt with no recorded answer is saved (exit 3); default: next to --json",
    )
    ap.add_argument(
        "--no-scientist-agent",
        action="store_true",
        help="scientist runs only: leave the learning agent out of the candidate list (faster)",
    )
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
        saved = json.loads(args.render.read_text())
        print(
            render_scientist(saved)
            if saved.get("settings", {}).get("policy") == "scientist"
            else render(saved)
        )
        return 0
    why = refusal(args.dataset, args.sealed_final)  # before anything is read
    if why is not None:
        print(why, file=sys.stderr)
        return 2
    if args.sealed_final and (args.limit or args.seeds != 4 or args.no_features):
        print(
            "refusing: a sealed run uses the fixed sizes, 4 seeds and the features, like every "
            "scoreboard run (no --limit, --seeds or --no-features). Nothing was read.",
            file=sys.stderr,
        )
        return 2
    if args.ai_answers and args.policy != "scientist":
        print("refusing: --ai-answers belongs to --policy scientist. Nothing was read.", file=sys.stderr)
        return 2
    if args.ai_answers and args.dataset in SEALED_DATASETS:
        print(
            "refusing: the AI roles are graded on the dev datasets only; a sealed run scores the fixed "
            "candidate list. Nothing was read.",
            file=sys.stderr,
        )
        return 2
    names = list(DATASETS) if args.dataset == "all" else [args.dataset]
    answers: dict[str, Any] | None = None
    recorder = RecordingLLM()
    pending: list[str] = []
    if args.ai_answers:  # a bad answers file is refused before any data is read
        try:
            loaded = json.loads(args.ai_answers.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not isinstance(loaded, dict):
            print(
                f"error: {args.ai_answers}: expected a JSON object of {{prompt id: answer}}", file=sys.stderr
            )
            return 2
        answers = loaded
    if args.json:  # the report's folder must exist before a run of many minutes (or a sealed one) starts
        args.json.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    command = " ".join(["python -m scripts.v2_realbench.run", *(argv if argv is not None else sys.argv[1:])])
    if args.sealed_final:  # recorded BEFORE the data is read: a crashed run still opened the seal
        row = record_sealed_run(
            args.sealed_log,
            dataset=args.dataset,
            git_sha=_git("rev-parse", "HEAD"),
            dirty=bool(_git("status", "--porcelain", "--", "autonomy", "scripts")),
            command=command,
            now=datetime.now(UTC),
        )
        print(f"sealed run recorded in {args.sealed_log}: {row}", flush=True)
    report: dict[str, Any] = {
        "bench_version": BENCH_VERSION,
        "command": command,
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
            "sealed_final": args.sealed_final,
            "policy": args.policy,
        },
        "datasets": {},
    }
    for name in names:
        timings = Timings(quiet=args.quiet)
        t0 = time.perf_counter()
        print(f"\n===== {name} =====", flush=True)
        try:
            data = load(name, args.data_dir, timings, limit=args.limit or None)
            if args.policy == "scientist":
                ai = None if answers is None else AiSetup(CountingReplay(answers, fallback=recorder))
                block = evaluate_scientist(
                    data, seeds=args.seeds, timings=timings, with_agent=not args.no_scientist_agent, ai=ai
                )
            else:
                block = evaluate_dataset(
                    data,
                    seeds=args.seeds,
                    timings=timings,
                    with_decision_log=args.with_decision_log,
                    attach_features=not args.no_features,
                    v3=args.policy == "agent_v3",
                    variant=args.policy,
                )
            del data
        except MemoryError as err:  # keep the other datasets' numbers
            block = {"error": f"MemoryError: {err}"}
        except AnswerPending as exc:  # no recorded answer for this prompt: saved, never guessed
            pending.append(exc.prompt_id)
            block = {
                "error": f"waiting for an answer to prompt {exc.prompt_id}",
                "pending_prompt": exc.prompt_id,
            }
        block["seconds"] = round(time.perf_counter() - t0, 1)
        block["peak_rss_mb"] = round(peak_rss_mb(), 1)
        block["timings"] = timings.rows
        report["datasets"][name] = block
        if args.policy != "scientist":
            add_multiplicity(report)  # raw and Holm-corrected, over the datasets finished so far
        gc.collect()
        if "error" not in block and args.policy == "scientist":
            print(
                f"{name}: picked {block['picked']['run']} ({block['picked']['outcome']}); vs best simple "
                f"({block['best_simple']['name']}) {_fmt(block['best_simple']['scientist_minus_best_simple'])}; "
                f"verdict {block['verdict']}",
                flush=True,
            )
        elif "error" not in block:
            v = block["verdict"]
            print(
                f"{name}: agent DR {block['agent']['value']['dr']['point']:.4f}, contacts "
                f"{block['agent']['contact_rate']:.1%}; vs best simple ({block['best_simple']['name']}) "
                f"{_fmt(block['best_simple']['agent_minus_best_simple'])}; vs send-to-all "
                f"{_fmt(block['send_to_all']['agent_minus_send_to_all'])}; calibration ratio "
                f"{block['calibration'].get('ratio')}; verdict {v}",
                flush=True,
            )
            if block["agent"].get("diag_text"):
                print(block["agent"]["diag_text"], flush=True)
        if args.json:  # write after every dataset, so a crash keeps what finished
            args.json.write_text(json.dumps(report, indent=1, default=_default))
    report["seconds"] = round(time.perf_counter() - started, 1)
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, default=_default))
        print(f"\nreport written to {args.json}")
    print("\n" + (render_scientist(report) if args.policy == "scientist" else render(report)))
    if pending:
        record = args.ai_record or (
            args.json.with_suffix(".prompts.json") if args.json else Path("prompts.json")
        )
        recorder.save(record)
        print(
            f"{len(recorder.records)} prompt(s) saved to {record}: answer them into the --ai-answers "
            f"file ({{id: answer}}) and run again",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
