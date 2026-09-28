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

STEPS = ("profile", "formats", "advise", "recipe")


def _rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _context(frame: pd.DataFrame) -> Any:
    from tests.unit.agent.helpers import context_for

    return context_for(frame)


def _run_step(step: str, path: Path) -> dict[str, Any]:
    frame = pd.read_parquet(path)
    ctx = _context(frame) if step in {"advise", "recipe"} else None
    base = _rss_mib()
    started = time.perf_counter()
    detail: dict[str, Any] = {}
    if step == "profile":
        _context(frame)
    elif step == "formats":
        from engine.agent.formats import find_format_issues

        detail["issues"] = len(find_format_issues(frame))
    elif step == "advise":
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--step", choices=STEPS)
    parser.add_argument("--data", type=Path)
    args = parser.parse_args(argv)
    if args.step:
        print(json.dumps(_run_step(args.step, args.data)))
        return 0
    from tests.fixtures.agent_bench.make_messy import messy_frame

    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "messy.parquet"
        started = time.perf_counter()
        frame = messy_frame(rows=args.rows)
        frame.to_parquet(data, index=False)
        _write_recipe(frame.head(20_000), data.with_suffix(".recipe.json"))
        del frame
        print(
            json.dumps(
                {"step": "generate", "rows": args.rows, "seconds": round(time.perf_counter() - started, 2)}
            )
        )
        for step in STEPS:
            out = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.fixtures.agent_bench.perf",
                    "--step",
                    step,
                    "--data",
                    str(data),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            print(out.stdout.strip().splitlines()[-1], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
