"""Fail when README.md calls a milestone "pending" that the test suite says is built.

The README's milestone tables are the first thing a reader trusts and the last thing anybody
updates. On 23 Sep 2026 six Phase 2 milestones (M8 ... M13) still read "pending" a day after they
were merged with their tests green: the table was written when the branch opened and nothing ever
asked it again. Plan A M39 makes the question mechanical, the same way `test_docs_honesty.py` made
the deployment guide's cost tables mechanical: a claim in a document that rots silently is checked
rather than remembered.

How it decides, and why it is built this way:

* **The README is parsed, not grepped.** Every Markdown table with a `Status` column is read, and a
  row counts when its first cell is a milestone id (`M8`, `**M34**`). The word "pending" anywhere
  else - in prose, in a table about something that is not a milestone - is not a claim about a
  milestone and is left alone. A README with no milestone table at all is an error rather than a
  pass, because a parser that silently finds nothing is exactly the kind of green this check exists
  to stop.
* **The mapping from milestone to tests is explicit** (`MILESTONE_TESTS`, below). It is not
  inferred from test names or commit messages: a reviewer reads one dict to see what "M9 has passing
  tests" means, and a milestone that is not in it is never flagged, only reported. Every mapped path
  must exist, so a renamed test file fails here instead of quietly turning into "no evidence".
* **Evidence comes from a JUnit XML report, not a second test run.** CI's `make test` step already
  ran the fast suite; `ci.yml` asks it for `--junitxml` through `PYTEST_ADDOPTS` (the Makefile's
  `test` target is not ours to change) and this script reads that file, so the check costs a parse
  rather than minutes. Without `--junit` it runs pytest itself, but only over the tests of
  milestones the README calls pending - in the steady state, when the README is honest, that is
  nothing at all.
* **"Passing" means at least one mapped test passed and none failed.** A skipped test is not
  evidence either way, and neither is a test the run never selected: a milestone whose only tests
  are `@slow` is not judged by the fast suite. That makes the check one-sided on purpose - it can
  say a "pending" is false, never that a "done" is true.

Exit codes: 0 honest, 1 at least one milestone wrongly pending, 2 the check could not run (no
milestone table, a mapped path missing, an unreadable report, pytest crashed).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

COMMAND: Final[str] = "python -m scripts.check_readme"
REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_README: Final[str] = "README.md"

MILESTONE_TESTS: Final[Mapping[str, tuple[str, ...]]] = {
    # --- Phase 1 (plan.md §11) ---------------------------------------------------------------
    "M1": (
        "tests/unit/test_config_loading.py",
        "tests/unit/test_config_merge.py",
        "tests/unit/test_config_validators.py",
        "tests/unit/test_contracts.py",
        "tests/integration/test_api_config.py",
    ),
    "M2": (
        "tests/unit/test_ingest.py",
        "tests/unit/test_validate.py",
        "tests/integration/test_api_uploads.py",
        "tests/integration/test_api_runs.py",
    ),
    "M3": (
        "tests/unit/test_prepare.py",
        "tests/unit/test_split.py",
        "tests/unit/test_evaluate.py",
        "tests/unit/test_register.py",
        "tests/unit/test_run_train.py",
        "tests/integration/test_train_flow.py",
    ),
    "M4": (
        "tests/unit/test_drift.py",
        "tests/unit/test_actions.py",
        "tests/unit/test_export.py",
        "tests/unit/test_run_score.py",
        "tests/integration/test_score_flow.py",
    ),
    "M5": ("tests/integration/test_ui.py", "tests/integration/test_acceptance.py"),
    "M6": ("tests/unit/test_config_only_reuse.py",),
    "M7": ("tests/unit/test_logging_audit.py", "tests/unit/test_jobs.py"),
    # --- Phase 2 (onboarding) ----------------------------------------------------------------
    "M8": (
        "tests/unit/onboarding/test_clients.py",
        "tests/unit/onboarding/test_sources.py",
        "tests/unit/onboarding/test_roles.py",
        "tests/integration/test_api_clients.py",
    ),
    "M9": ("tests/unit/onboarding/test_mapping.py", "tests/unit/onboarding/test_transforms.py"),
    "M10": ("tests/unit/onboarding/test_features.py",),
    "M11": ("tests/unit/onboarding/test_labels.py", "tests/unit/onboarding/test_snapshots.py"),
    "M12": (
        "tests/unit/onboarding/test_datasets.py",
        "tests/unit/onboarding/test_validate.py",
        "tests/integration/test_api_datasets.py",
        "tests/integration/test_runs_from_dataset.py",
    ),
    "M13": ("tests/unit/test_onboarding_ui.py", "tests/integration/test_onboarding_flow.py"),
    "M14": ("tests/fixtures/raw/test_make_raw.py",),
    # --- Phase 3a (generative) ---------------------------------------------------------------
    "M15": (
        "tests/unit/generative/test_budget.py",
        "tests/unit/generative/test_prompts.py",
        "tests/unit/generative/test_guardrails.py",
    ),
    "M16": (
        "tests/unit/generative/test_parsing.py",
        "tests/unit/generative/test_vectorstore.py",
        "tests/unit/generative/test_retrieval.py",
        "tests/unit/generative/test_index.py",
        "tests/unit/generative/test_assistant.py",
    ),
    "M17": ("tests/unit/generative/test_evaluation.py",),
    "M18": ("tests/unit/generative/test_root_cause.py",),
    "M19": ("tests/unit/generative/test_win_back.py",),
    "M20": ("tests/integration/test_api_generative.py",),
    # --- Phase 4a (aws) ----------------------------------------------------------------------
    "M21": (
        "tests/unit/test_settings.py",
        "tests/unit/test_aws_secrets.py",
        "tests/unit/test_storage_contract.py",
        "tests/unit/test_s3_storage.py",
    ),
    "M22": ("tests/unit/test_container_files.py", "tests/unit/test_job_entrypoint.py"),
    "M23": (
        "tests/unit/test_sagemaker_jobs.py",
        "tests/unit/test_runs_module.py",
        "tests/integration/test_jobs_as_sagemaker.py",
    ),
    "M24": (
        "tests/unit/test_postgres_metadata.py",
        "tests/unit/test_alembic_migrations.py",
        "tests/unit/test_run_index.py",
        "tests/unit/test_s3_registry.py",
    ),
    "M25": ("tests/infra/",),
    "M26": (
        "tests/unit/test_metrics.py",
        "tests/unit/test_gen_dashboard.py",
        "tests/unit/test_docs_honesty.py",
    ),
    # --- Plan A (Phase 2 completion) ---------------------------------------------------------
    # M35-M38 are added by whoever merges them, naming the tests that milestone wrote: a mapping
    # that points at an older test would call the new milestone built before it was.
    "M34": ("tests/unit/test_composite_key.py", "tests/integration/test_periodic_flow.py"),
    "M39": ("tests/unit/test_check_readme.py",),
}
"""Milestone id -> the test files (or directories, with a trailing `/`) that prove it is built.

Taken from the commits that delivered each milestone. A milestone may be absent - Phase 4b's M46
... M52 are, and so is anything planned but not started - and an absent milestone is reported,
never flagged. Paths are relative to the repository root, the way pytest's node ids spell them."""

PENDING: Final[re.Pattern[str]] = re.compile(
    r"^(pending|not (yet )?(started|begun|built|done)|to ?do|tbd|planned)\b"
)
"""A status that claims the milestone is not built. Matched at the START of the normalised cell,
so "done — pending a laptop run" is a done milestone with a caveat, not a pending one, and
"partial" is neither: a partial milestone may have every test green and still owe something the
tests cannot see (M7's laptop measurement)."""

MILESTONE_ID: Final[re.Pattern[str]] = re.compile(r"^M\d+[a-z]?$")
FENCE: Final[re.Pattern[str]] = re.compile(r"^\s*(```|~~~)")
SEPARATOR_CELL: Final[re.Pattern[str]] = re.compile(r"^:?-{3,}:?$")
PAID_MARKERS: Final[str] = "not slow and not bedrock and not aws"
"""What the fallback run selects: the fast suite's filter, the same as `make test`'s."""


class CheckError(Exception):
    """The check could not reach a verdict (as opposed to reaching "dishonest")."""


@dataclass(frozen=True)
class MilestoneRow:
    """One row of a README milestone table."""

    milestone: str
    status: str
    line: int

    @property
    def pending(self) -> bool:
        return PENDING.match(self.status) is not None


@dataclass(frozen=True)
class Evidence:
    """What one test report says about one milestone's tests."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def proves_built(self) -> bool:
        return self.passed > 0 and self.failed == 0


def _normalise(cell: str) -> str:
    """`**done** — see below` -> `done — see below`: emphasis and code marks carry no meaning here."""
    return re.sub(r"[*_`]", "", cell).strip().lower()


def _cells(line: str) -> list[str]:
    """Split one table row. `\\|` is a literal pipe inside a cell, not a column boundary."""
    body = line.strip()
    body = body.removeprefix("|").removesuffix("|")
    return [cell.replace("\\|", "|").strip() for cell in re.split(r"(?<!\\)\|", body)]


def _tables(lines: Sequence[str]) -> Iterable[tuple[int, list[str]]]:
    """Yield `(first line number, rows)` for each Markdown table outside a fenced code block."""
    in_fence = False
    block: list[str] = []
    start = 0
    for number, line in enumerate(lines, start=1):
        if FENCE.match(line):
            in_fence = not in_fence
        is_row = not in_fence and line.lstrip().startswith("|")
        if is_row:
            if not block:
                start = number
            block.append(line)
            continue
        if block:
            yield start, block
            block = []
    if block:
        yield start, block


def milestone_rows(readme_text: str) -> list[MilestoneRow]:
    """Every milestone row of every table that has a `Status` column."""
    rows: list[MilestoneRow] = []
    for start, block in _tables(readme_text.splitlines()):
        if len(block) < 2 or not all(SEPARATOR_CELL.match(c.replace(" ", "")) for c in _cells(block[1])):
            continue
        header = [_normalise(cell) for cell in _cells(block[0])]
        if "status" not in header:
            continue
        status_at = header.index("status")
        for offset, line in enumerate(block[2:], start=2):
            cells = _cells(line)
            if len(cells) <= status_at:
                continue
            milestone = re.sub(r"[*_`]", "", cells[0]).strip()
            if MILESTONE_ID.match(milestone):
                rows.append(MilestoneRow(milestone, _normalise(cells[status_at]), start + offset))
    return rows


def _module_prefix(path: str) -> str:
    """`tests/unit/test_x.py` -> `tests.unit.test_x`; `tests/infra/` -> `tests.infra.`.

    That is how pytest's JUnit writer spells a test's `classname` (its node id with `/` turned into
    `.` and `.py` dropped), so the comparison needs no second source of truth about file layout.
    """
    if path.endswith("/"):
        return path.rstrip("/").replace("/", ".") + "."
    return path.removesuffix(".py").replace("/", ".")


def _belongs(classname: str, path: str) -> bool:
    prefix = _module_prefix(path)
    if prefix.endswith("."):
        return classname.startswith(prefix)
    return classname == prefix or classname.startswith(prefix + ".")


def read_junit(report: Path) -> list[tuple[str, str]]:
    """`(classname, outcome)` for every test case in a JUnit XML report.

    The outcome is `failed` for a `<failure>` or `<error>` child, `skipped` for `<skipped>` (which is
    also how pytest writes an xfail), and `passed` otherwise.
    """
    try:
        # Plain ElementTree rather than a hardened parser: the report is one our own pytest run
        # wrote, on the same runner, a step earlier.
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError) as exc:
        raise CheckError(f"Could not read the test report {report}: {type(exc).__name__}.") from exc
    cases: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        outcome = "failed" if tags & {"failure", "error"} else "skipped" if "skipped" in tags else "passed"
        cases.append((case.get("classname", ""), outcome))
    return cases


def evidence_for(paths: Sequence[str], cases: Sequence[tuple[str, str]]) -> Evidence:
    """Count the report's outcomes for the test cases that live under `paths`."""
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for classname, outcome in cases:
        if any(_belongs(classname, path) for path in paths):
            counts[outcome] += 1
    return Evidence(**counts)


def missing_paths(mapping: Mapping[str, Sequence[str]], repo_root: Path) -> list[str]:
    """Mapped paths that are not in the checkout: a rename the mapping has not caught up with."""
    return sorted(
        f"{milestone}: {path}"
        for milestone, paths in mapping.items()
        for path in paths
        if not (repo_root / path).exists()
    )


def run_pytest(paths: Sequence[str], repo_root: Path, report: Path) -> None:
    """Run the fast-suite selection of `paths` and leave its JUnit report at `report`.

    A red test is not an error here - it is evidence, and `evidence_for` reads it - but a run that
    wrote no report at all (a collection crash, a usage error) is.
    """
    command = [
        sys.executable,
        "-m",
        "pytest",
        *paths,
        "-m",
        PAID_MARKERS,
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
    ]
    completed = subprocess.run(command, cwd=repo_root, check=False, capture_output=True, text=True)
    if not report.is_file():
        tail = "\n".join(completed.stdout.splitlines()[-20:])
        raise CheckError(f"pytest exited {completed.returncode} without a report:\n{tail}")


def verdict(
    rows: Sequence[MilestoneRow],
    cases: Sequence[tuple[str, str]],
    mapping: Mapping[str, Sequence[str]],
    *,
    readme: str,
) -> tuple[list[str], int]:
    """The lines to print and the exit code, as a pure function of the parsed inputs."""
    lines: list[str] = []
    wrong = 0
    for row in rows:
        if not row.pending:
            continue
        paths = mapping.get(row.milestone)
        if paths is None:
            lines.append(f"{readme}:{row.line}: {row.milestone} is pending; no tests are mapped to it (ok).")
            continue
        found = evidence_for(paths, cases)
        if found.proves_built:
            wrong += 1
            lines.append(
                f"{readme}:{row.line}: {row.milestone} is marked '{row.status}' but {found.passed} of its "
                f"tests passed and none failed ({', '.join(paths)}). Mark it done, or say what is missing."
            )
        else:
            lines.append(
                f"{readme}:{row.line}: {row.milestone} is pending; the report holds no evidence it is "
                f"built (passed {found.passed}, failed {found.failed}, skipped {found.skipped}) (ok)."
            )
    pending = sum(row.pending for row in rows)
    lines.append(
        f"{len(rows)} milestone rows, {pending} pending, {wrong} wrongly pending."
        if wrong
        else f"README honest: {len(rows)} milestone rows, {pending} pending, none with passing tests."
    )
    return lines, 1 if wrong else 0


def check(readme_path: Path, junit: Path | None, repo_root: Path) -> tuple[list[str], int]:
    """Parse, gather evidence (from `junit`, or by running pytest), and decide."""
    try:
        text = readme_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckError(f"Could not read {readme_path}: {type(exc).__name__}.") from exc
    rows = milestone_rows(text)
    if not rows:
        raise CheckError(
            f"{readme_path} has no milestone table (a table with a 'Status' column whose rows start "
            "with M<number>). Either the tables moved or the parser needs to follow them."
        )
    missing = missing_paths(MILESTONE_TESTS, repo_root)
    if missing:
        raise CheckError(
            "MILESTONE_TESTS names paths that do not exist:\n  " + "\n  ".join(missing) + "\n"
            "Update the mapping in scripts/check_readme.py to where those tests went."
        )
    if junit is not None:
        cases = read_junit(junit)
    else:
        targets = sorted(
            {path for row in rows if row.pending for path in MILESTONE_TESTS.get(row.milestone, ())}
        )
        cases = []
        if targets:
            with tempfile.TemporaryDirectory() as scratch:
                report = Path(scratch) / "junit.xml"
                run_pytest(targets, repo_root, report)
                cases = read_junit(report)
    try:
        shown = readme_path.resolve().relative_to(repo_root)
    except ValueError:
        shown = readme_path
    return verdict(rows, cases, MILESTONE_TESTS, readme=str(shown))


def main(argv: Sequence[str] | None = None) -> int:
    """Print the verdict; the exit code is what CI gates on."""
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description="Fail when README.md marks a milestone pending whose mapped tests pass.",
    )
    parser.add_argument("--readme", type=Path, default=REPO_ROOT / DEFAULT_README)
    parser.add_argument(
        "--junit",
        type=Path,
        default=None,
        help="a JUnit XML report of a run that already happened (CI passes the fast suite's); "
        "without it, the tests of pending milestones are run here",
    )
    args = parser.parse_args(argv)
    try:
        lines, code = check(args.readme, args.junit, REPO_ROOT)
    except CheckError as exc:
        print(f"check_readme: {exc}", file=sys.stderr)
        return 2
    stream = sys.stderr if code else sys.stdout
    for line in lines:
        print(line, file=stream)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
