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
from tests.fixtures.agent_bench.make_hinglish import CASES as HINGLISH_CASES
from tests.fixtures.agent_bench.make_hinglish import hinglish_frame
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


def _placeholder_value() -> pd.DataFrame:
    """`99` in a column whose real values stop at 16: a code for "unknown" (DEC-1221)."""
    frame = _variant("clean")()
    frame.loc[frame.index % 25 == 0, "visits_last_7d"] = 99
    return frame


def _real_ninety_nine() -> pd.DataFrame:
    """Ages 18 to 99, evenly spread: `99` is a real age, never a placeholder (the false-positive guard)."""
    frame = _variant("clean")()
    frame["age"] = [18 + (i * 7) % 82 for i in range(len(frame))]
    return frame


def _other_flags(column: str, rows: int = 2_000) -> pd.DataFrame:
    """A file whose only two-valued columns are `column` and `other_flag`: neither is the use case's outcome.

    The column's name shares one word with a long outcome name of the use case (`cash_back_flag` and
    `won_back`), which must not make it the outcome (M142 review: a word of a name is not the name).
    """
    return pd.DataFrame(
        {
            "grahak_id": [f"G{i:05d}" for i in range(rows)],
            column: [(i // 3) % 2 for i in range(rows)],
            "other_flag": [(i // 5) % 2 for i in range(rows)],
            "kul_kharch": [round(40 + (i * 37) % 3_960, 2) for i in range(rows)],
        }
    )


CASES: Final[dict[str, Callable[[], pd.DataFrame]]] = {
    "messy": messy_frame,
    "clean": _variant("clean"),
    # M109 (DEC-1319): a list big enough for the planner to size the share held back, but not big enough
    # for the default 10% to see a 2-point effect; its digest holds the new `actions.control_group_fraction`.
    "mid_size_file": _variant("clean", rows=20_000),
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
    # Placeholder values (DEC-1221): one to propose emptying, one real value to leave alone.
    "placeholder_value": _placeholder_value,
    "real_ninety_nine": _real_ninety_nine,
    # Level 3 (M76): an order log, several rows per shopper, for a use case that allows `reshape`.
    "multi_row": multirow_frame,
    "multi_row_one_date": lambda: multirow_frame(snapshot=False),
    "multi_row_no_dates": lambda: multirow_frame(dates=False),
}

# Plan K M142 (DEC-1409): synthetic Indian-style exports with Hinglish and abbreviated headers. The advice
# for each is pinned like any other case; `headers` (see `run_case`) pins what the header rules read.
HINGLISH_PREFIX: Final[str] = "hinglish_"
for _spec in HINGLISH_CASES:
    CASES[HINGLISH_PREFIX + _spec.name] = lambda spec=_spec: hinglish_frame(spec)  # type: ignore[misc]

# M142 review: words of the use case's long outcome names (`won_back`, `default_payment_next_month`,
# `churn_next_60d`) are not outcome anchors one by one; each of these files must be asked, not proposed.
CASES["other_use_case_cash_back"] = lambda: _other_flags("cash_back_flag")
CASES["other_use_case_payment_reminder"] = lambda: _other_flags("payment_reminder_sent")
CASES["other_use_case_next_bill"] = lambda: _other_flags("next_bill_flag")

CASE_USE_CASES: Final[dict[str, str]] = {
    "other_use_case_cash_back": "win-back-campaign",
    "other_use_case_payment_reminder": "card-default-propensity",
    "other_use_case_next_bill": "rca",
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


def header_reading(advice: Advice) -> dict[str, list[str]]:
    """The columns the header rules listed for each role, best first (from the advice's own evidence)."""
    roles = next(r for r in advice.tool_results if r.tool == "find_roles")
    found: dict[str, list[str]] = {}
    entries = roles.result.get("header_candidates")
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            found.setdefault(str(entry["kind"]), []).append(str(entry["column"]))
    return found


def run_case(name: str) -> dict[str, Any]:
    advice = advise(context_for(CASES[name](), CASE_USE_CASES.get(name, USE_CASE)))
    result = digest(advice)
    if name.startswith(HINGLISH_PREFIX):
        result["headers"] = header_reading(advice)
    return result


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
