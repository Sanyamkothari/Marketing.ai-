"""Benchmark cases and the digest of an `Advice` that the golden file pins (Plan G §10).

`python -m tests.fixtures.agent_bench.cases --update` rewrites `expected.json` after a deliberate
rule change; the diff is then the review.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pandas as pd

from engine.agent.advisor import Advice, advise
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.fixtures.agent_bench.make_multirow import multirow_frame
from tests.fixtures.make_data import GenerationSpec, generate
from tests.unit.agent.helpers import context_for

EXPECTED: Final[Path] = Path(__file__).with_name("expected.json")
USE_CASE: Final[str] = "targeted-advertisement"


def _variant(name: str, rows: int = 3_000) -> Callable[[], pd.DataFrame]:
    return lambda: generate(GenerationSpec(use_case_id=USE_CASE, rows=rows, variant=name))


def _renamed_target() -> pd.DataFrame:
    return _variant("clean")().rename(columns={"converted_30d": "Purchased"})


def _ambiguous_dates() -> pd.DataFrame:
    frame = _variant("clean")()
    frame["joined"] = [f"{(i % 12) + 1:02d}/{(i % 11) + 1:02d}/2024" for i in range(len(frame))]
    return frame


def _unreadable_numbers() -> pd.DataFrame:
    frame = _variant("clean")()
    frame["balance"] = [f"₹{i:,}" if i % 4 else "ask branch" for i in range(len(frame))]
    return frame


CASES: Final[dict[str, Callable[[], pd.DataFrame]]] = {
    "messy": messy_frame,
    "clean": _variant("clean"),
    "renamed_target": _renamed_target,
    "ambiguous_dates": _ambiguous_dates,
    "unreadable_numbers": _unreadable_numbers,
    "leaky_column": _variant("leaky_column"),
    "pii_column": _variant("pii_column"),
    "constant_column": _variant("constant_column"),
    "high_null_column": _variant("high_null_column"),
    "id_like_column": _variant("id_like_column"),
    "duplicate_keys": _variant("duplicate_keys"),
    "null_keys": _variant("null_keys"),
    "too_few_positives": _variant("too_few_positives"),
    "too_few_rows": _variant("too_few_rows"),
    "constant_target": _variant("constant_target"),
    "non_binary_target": _variant("non_binary_target"),
    # Level 3 (M76): an order log, several rows per shopper, for a use case that allows `reshape`.
    "multi_row": multirow_frame,
    "multi_row_one_date": lambda: multirow_frame(snapshot=False),
    "multi_row_no_dates": lambda: multirow_frame(dates=False),
}

CASE_USE_CASES: Final[dict[str, str]] = {
    "multi_row": "retail-win-back",
    "multi_row_one_date": "retail-win-back",
    "multi_row_no_dates": "retail-win-back",
}
"""The use case a case runs under, when it is not Targeted Advertisement."""


def digest(advice: Advice) -> dict[str, Any]:
    """What must not change silently: status, roles, each proposal's subject and confidence, questions."""

    def subject(proposal: Any) -> str:
        if proposal.step is not None:
            return f"{proposal.step.kind.value}:{proposal.step.column}"
        return f"{proposal.path}={proposal.value}"

    return {
        "status": advice.status.value,
        "stopped": advice.stop_reason is not None,
        "primary_key": advice.primary_key,
        "target": advice.target,
        "proposals": [f"{p.kind.value} {subject(p)} ({p.confidence.value})" for p in advice.proposals],
        "questions": [
            {
                "asks": q.text.split(",")[0][:60],
                "options": [o.label for o in q.options],
                "blocking": q.blocking,
            }
            for q in advice.questions
        ],
        "hidden": list(advice.engine_hidden),
    }


def run_case(name: str) -> dict[str, Any]:
    return digest(advise(context_for(CASES[name](), CASE_USE_CASES.get(name, USE_CASE))))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="rewrite expected.json")
    args = parser.parse_args()
    results = {name: run_case(name) for name in CASES}
    if args.update:
        EXPECTED.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
