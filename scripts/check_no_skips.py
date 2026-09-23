"""Fail when a test that CI is supposed to run was skipped, or never ran at all (M56, DEC-878).

`REQUIRE_JSDOM=1` and `MARKETING_AI_REQUIRE_POSTGRES=1` already turn the known skips into failures
inside the suite. This is the check from outside it: it reads the JUnit XML report `make test`
wrote and, for every `--id` pattern, demands that

* no test case whose id matches the pattern was skipped, whatever the reason - a new skip path
  somebody adds next month is caught here even though no fixture knows about it; and
* at least one test case whose id matches it actually ran - so a suite that was renamed, moved or
  deselected cannot pass by matching nothing.

A test case's id is `classname::name` as pytest writes them into the report, for example
`tests.integration.production.test_production_ui_js::test_the_production_ui_modules_in_jsdom`;
a pattern is a regular expression searched for anywhere in it.

Exit codes: 0 every pattern ran and none skipped, 1 a violation, 2 the report could not be read.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

COMMAND = "python -m scripts.check_no_skips"


@dataclass(frozen=True)
class Case:
    """One `<testcase>` of the report: its id and whether it was skipped."""

    id: str
    skipped: bool
    reason: str = ""


def read_cases(report: Path) -> list[Case]:
    """Every test case in a JUnit XML report. Raises `ValueError` when it cannot be read."""
    try:
        # Plain ElementTree: the report is one our own pytest run wrote a step earlier.
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"could not read the test report {report}: {type(exc).__name__}") from exc
    cases: list[Case] = []
    for element in root.iter("testcase"):
        skip = element.find("skipped")
        cases.append(
            Case(
                id=f"{element.get('classname', '')}::{element.get('name', '')}",
                skipped=skip is not None,
                reason="" if skip is None else skip.get("message", ""),
            )
        )
    return cases


def violations(cases: Sequence[Case], patterns: Sequence[str]) -> list[str]:
    """One sentence per skipped matching test, and per pattern that matched no test that ran."""
    problems: list[str] = []
    for pattern in patterns:
        regex = re.compile(pattern)
        matching = [case for case in cases if regex.search(case.id)]
        problems += [
            f"skipped: {case.id} ({case.reason or 'no reason given'})" for case in matching if case.skipped
        ]
        if not any(not case.skipped for case in matching):
            problems.append(f"never ran: no test matching {pattern!r} ran in this report")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Print the verdict; the exit code is what CI gates on."""
    parser = argparse.ArgumentParser(
        prog=COMMAND, description="Fail when a test CI must run was skipped or never ran."
    )
    parser.add_argument("--junit", type=Path, required=True, help="the JUnit XML report to read")
    parser.add_argument(
        "--id",
        dest="patterns",
        action="append",
        required=True,
        help="a regular expression over `classname::name`; repeat for several",
    )
    args = parser.parse_args(argv)
    try:
        cases = read_cases(args.junit)
    except ValueError as exc:
        print(f"check_no_skips: {exc}", file=sys.stderr)
        return 2
    problems = violations(cases, args.patterns)
    for problem in problems:
        print(f"check_no_skips: {problem}", file=sys.stderr)
    if problems:
        return 1
    ran = sum(1 for case in cases if any(re.search(p, case.id) for p in args.patterns))
    print(f"check_no_skips: {ran} matching tests ran, none skipped ({len(args.patterns)} patterns)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
