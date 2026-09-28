"""Guided setup on a large file: time and peak memory of each step (Plan G M77, `reports/plan_g_performance.md`).

    python -m tests.fixtures.agent_bench.perf --rows 1000000

Writes the messy Targeted Advertisement file once as Parquet, then runs each step in a fresh
process (so one step's memory is not another's), and prints one JSON line per step: wall time in
seconds, the frame's own size, and the process's peak resident memory before and after the step, in
MiB (the peak includes reading the Parquet file, so a step that stays under it shows no rise).

Steps: `profile` (the upload's profile, which the API computes at upload time), `formats`
(`find_format_issues` on the frame the helper sees - the profile-capped rows, which for any file
under the 2,000,000-row cap is every row), `advise` (the whole advisor, rules only), and `recipe`
(`run_recipe` on every row with the fixes the advisor is sure of, as Approve runs it).

Level 3 (M76) runs on a second file, a Retail Win-back order log of about the same number of rows
(`make_multirow`, about 5 orders per shopper): `multirow_advise` (the advisor, which asks to combine) and
`combine` (`run_recipe` with the proposed `combine_rows` step and the full future-data check, as
Approve runs it on a training file). `--only` runs a subset.
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pandas as pd

from engine.agent.contracts import RecipeStep

STEPS = ("profile", "formats", "advise", "recipe", "multirow_advise", "combine")
MULTIROW = ("multirow_advise", "combine")
MULTIROW_USE_CASE = "retail-win-back"
ORDERS_PER_SHOPPER = 5.06


def _rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _context(frame: pd.DataFrame, use_case_id: str = "targeted-advertisement") -> Any:
    from tests.unit.agent.helpers import context_for

    return context_for(frame, use_case_id)


def _run_step(step: str, path: Path) -> dict[str, Any]:
    frame = pd.read_parquet(path)
    use_case = MULTIROW_USE_CASE if step in MULTIROW else "targeted-advertisement"
    ctx = _context(frame, use_case) if step in {"advise", "recipe", *MULTIROW} else None
    base = _rss_mib()
    started = time.perf_counter()
    detail: dict[str, Any] = {}
    if step == "profile":
        _context(frame)
    elif step == "formats":
        from engine.agent.formats import find_format_issues

        detail["issues"] = len(find_format_issues(frame))
    elif step in {"advise", "multirow_advise"}:
        from engine.agent.advisor import advise

        advice = advise(ctx)
        detail["proposals"] = len(advice.proposals)
        detail["questions"] = len(advice.questions)
    elif step == "recipe":
        from engine.agent.recipe import run_recipe

        plan = json.loads(path.with_suffix(".recipe.json").read_text())
        steps = [RecipeStep.model_validate(item) for item in plan["steps"]]
        run = run_recipe(
            frame,
            steps,
            upload_id="perf",
            primary_key=plan["primary_key"],
            target=plan["target"],
            levels=ctx.config.agent.levels,
            max_failure_pct=ctx.config.agent.max_conversion_failure_pct,
        )
        detail["steps"] = [s.kind.value for s in steps]
        detail["rows_out"] = run.receipt.rows_out
    elif step == "combine":
        from engine.agent.recipe import run_recipe

        plan = json.loads(path.with_suffix(".recipe.json").read_text())
        steps = [RecipeStep.model_validate(item) for item in plan["steps"]]
        run = run_recipe(
            frame,
            steps,
            upload_id="perf",
            primary_key=plan["primary_key"],
            target=plan["target"],
            levels=ctx.config.agent.levels,
            max_failure_pct=ctx.config.agent.max_conversion_failure_pct,
            leak_check="full",
        )
        detail["steps"] = [s.kind.value for s in steps]
        detail["rows_out"] = run.receipt.rows_out
        detail["features"] = len(run.frame.columns)
    seconds = time.perf_counter() - started
    return {
        "step": step,
        "rows": len(frame),
        "seconds": round(seconds, 2),
        "frame_mib": round(frame.memory_usage(deep=True).sum() / 2**20, 1),
        "rss_before_mib": round(base, 1),
        "peak_rss_mib": round(_rss_mib(), 1),
        **detail,
    }


def _write_recipe(sample: pd.DataFrame, path: Path) -> None:
    """The fixes the advisor is sure of, found on a sample of the same file (the recipe step's input)."""
    from engine.agent.advisor import advise, recipe_steps
    from engine.agent.contracts import AgentConfidence

    advice = advise(_context(sample))
    steps = recipe_steps(p for p in advice.proposals if p.confidence is AgentConfidence.SURE)
    path.write_text(
        json.dumps(
            {
                "primary_key": advice.primary_key,
                "target": advice.target,
                "steps": [step.model_dump(mode="json") for step in steps],
            }
        )
    )


def combine_step(sample: pd.DataFrame) -> RecipeStep:
    """The `combine_rows` step the advisor asks about, planned on a sample of an order log."""
    from engine.agent.advisor import advise

    advice = advise(_context(sample, MULTIROW_USE_CASE))
    step = next(
        option.proposal.step
        for question in advice.questions
        for option in question.options
        if option.option_id == "combine" and option.proposal is not None and option.proposal.step is not None
    )
    return step.model_copy(update={"order": 1})


def _write_combine(sample: pd.DataFrame, path: Path) -> None:
    step = combine_step(sample)
    path.write_text(
        json.dumps(
            {
                "primary_key": step.column,
                "target": step.params.get("outcome"),
                "steps": [step.model_dump(mode="json")],
            }
        )
    )


def _run_all(steps: tuple[str, ...], data: Path) -> None:
    for step in steps:
        out = subprocess.run(
            [sys.executable, "-m", "tests.fixtures.agent_bench.perf", "--step", step, "--data", str(data)],
            capture_output=True,
            text=True,
            check=True,
        )
        print(out.stdout.strip().splitlines()[-1], flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--step", choices=STEPS)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--only", nargs="+", choices=STEPS, help="run these steps only")
    args = parser.parse_args(argv)
    if args.step:
        print(json.dumps(_run_step(args.step, args.data)))
        return 0
    from tests.fixtures.agent_bench.make_messy import messy_frame
    from tests.fixtures.agent_bench.make_multirow import multirow_frame

    wanted = tuple(args.only or STEPS)
    with tempfile.TemporaryDirectory() as tmp:
        single = tuple(step for step in wanted if step not in MULTIROW)
        if single:
            data = Path(tmp) / "messy.parquet"
            started = time.perf_counter()
            frame = messy_frame(rows=args.rows)
            frame.to_parquet(data, index=False)
            _write_recipe(frame.head(20_000), data.with_suffix(".recipe.json"))
            del frame
            seconds = round(time.perf_counter() - started, 2)
            print(json.dumps({"step": "generate", "rows": args.rows, "seconds": seconds}), flush=True)
            _run_all(single, data)
        multi = tuple(step for step in wanted if step in MULTIROW)
        if multi:
            data = Path(tmp) / "orders.parquet"
            started = time.perf_counter()
            frame = multirow_frame(shoppers=int(args.rows / ORDERS_PER_SHOPPER))
            frame.to_parquet(data, index=False)
            _write_combine(frame.head(20_000), data.with_suffix(".recipe.json"))
            rows = len(frame)
            del frame
            seconds = round(time.perf_counter() - started, 2)
            print(json.dumps({"step": "generate_multirow", "rows": rows, "seconds": seconds}), flush=True)
            _run_all(multi, data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
