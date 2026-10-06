"""Run the engine's own upload checks over the dirty files `generate_bad_test_data` wrote, and report.

    python -m scripts.audit_bad_data [--data-dir data/bad_data_tests] [--report PATH]

For every `<use case id>/*.csv` under the data directory, the file is read with pandas, profiled the
way an upload is (`engine.stages.ingest.profile_dataset`, which is where personal data is detected)
and, for a predictive use case, checked the way `POST /runs` checks a training file
(`engine.stages.validate.validate_for_training`). A generative use case's reference set is profiled
only: it is not a training file, so there is no training check to run on it.

The report (Markdown, `CAPABILITY_REPORT.md` in the data directory unless `--report` says otherwise)
contains only what this run measured: which files were read, which checks fired, how many errors and
warnings each produced, and every file on which a step raised instead of reporting - with the
exception's type and message, because that is a bug to fix. The exit status is 1 when any file
crashed a step, so the audit can gate a change.

Not collected by pytest (the name does not start with `test_`): it is a tool to run by hand, not a
test of its own.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pandas as pd

from engine.config import AiType, UseCaseConfig, load_all_use_cases
from engine.stages.ingest import profile_dataset
from engine.stages.validate import validate_for_training
from scripts.generate_bad_test_data import DEFAULT_OUT_DIR, columns_of

COMMAND: Final[str] = "python -m scripts.audit_bad_data"
REPORT_FILENAME: Final[str] = "CAPABILITY_REPORT.md"


@dataclass
class FileResult:
    """What happened to one file. A step that did not run is `None`, never a guessed outcome."""

    use_case_id: str
    file_name: str
    rows: int | None = None
    columns: int | None = None
    profiled: bool | None = None
    validated: bool | None = None
    errors: int | None = None
    warnings: int | None = None
    codes: list[str] = field(default_factory=list)
    pii_columns: list[str] = field(default_factory=list)
    crash: str | None = None
    """`Type: message` of the exception a step raised, when one did."""

    @property
    def crashed(self) -> bool:
        return self.crash is not None


def audit_file(path: Path, config: UseCaseConfig) -> FileResult:
    """Read, profile and (for a predictive use case) validate one file; a raised exception is recorded."""
    result = FileResult(use_case_id=config.id, file_name=path.name)
    step = "read"
    try:
        frame = pd.read_csv(path)
        result.rows, result.columns = len(frame), len(frame.columns)
        step = "profile"
        profile = profile_dataset(
            frame,
            config,
            upload_id=f"u_audit_{config.id}",
            file_name=path.name,
            file_format="csv",
            file_size_bytes=path.stat().st_size,
            delimiter=",",
            encoding="utf-8",
        )
        result.profiled = True
        result.pii_columns = [c.name for c in profile.columns if c.pii_kinds or c.free_text_pii_kinds]
        if config.ai_type is AiType.GENERATIVE:
            return result
        step = "validate"
        columns = columns_of(config)
        report = validate_for_training(
            frame,
            config,
            primary_key=columns.primary_key,
            target=columns.target,
            upload_id=f"u_audit_{config.id}",
        )
        result.validated = True
        result.errors, result.warnings = report.error_count, report.warning_count
        result.codes = [check.code for check in report.checks]
    except Exception as exc:  # the audit's whole point: a raised exception is a finding, not an abort
        if step == "profile":
            result.profiled = False
        elif step == "validate":
            result.validated = False
        result.crash = f"{step}: {type(exc).__name__}: {exc}"
    return result


def audit(data_dir: Path) -> list[FileResult]:
    """Every CSV under `data_dir/<use case id>/`, in folder and file order."""
    configs = load_all_use_cases()
    results: list[FileResult] = []
    for folder in sorted(path for path in data_dir.iterdir() if path.is_dir()):
        config = configs.get(folder.name)
        if config is None:
            print(f"Skipped {folder}: no use case is called {folder.name!r}.")
            continue
        for csv_file in sorted(folder.glob("*.csv")):
            result = audit_file(csv_file, config)
            results.append(result)
            outcome = (
                f"CRASH ({result.crash})"
                if result.crashed
                else f"errors {_shown(result.errors)}, warnings {_shown(result.warnings)}"
            )
            print(f"{config.id:<28} {csv_file.name:<40} {outcome}")
    return results


def _shown(value: object) -> str:
    """A figure as the report writes it; a step that did not run is a dash, not a zero."""
    return "-" if value is None else str(value)


def _cell(text: str) -> str:
    """`text` safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def render_report(results: Sequence[FileResult], *, data_dir: Path, now: datetime) -> str:
    """The Markdown report: every number in it is counted from `results`."""
    crashed = [r for r in results if r.crashed]
    validated = [r for r in results if r.validated]
    codes = Counter(code for r in results for code in set(r.codes))
    use_cases = sorted({r.use_case_id for r in results})
    lines = [
        "# Bad-data audit",
        "",
        f"Run {now.strftime('%Y-%m-%d %H:%M UTC')} over `{data_dir}` by `{COMMAND}`.",
        "",
        "## Summary",
        "",
        f"- Files: {len(results)}, across {len(use_cases)} use cases.",
        f"- Files on which a step raised instead of reporting: {len(crashed)}.",
        f"- Files read and profiled: {sum(1 for r in results if r.profiled)}.",
        f"- Files checked as training files: {len(validated)} (a generative use case's files are profiled only).",
        f"- Blocking errors reported: {sum(r.errors or 0 for r in validated)}; "
        f"warnings: {sum(r.warnings or 0 for r in validated)}.",
        f"- Distinct check codes that fired: {len(codes)}.",
        "",
        "## Check codes",
        "",
        "| Code | Files it fired on |",
        "| :--- | ---: |",
        *(f"| `{code}` | {count} |" for code, count in sorted(codes.items())),
        "",
        "## Every file",
        "",
        "| Use case | File | Rows | Columns | Errors | Warnings | Personal-data columns | Checks |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | :--- | :--- |",
    ]
    for r in results:
        checks = "CRASH" if r.crashed else ", ".join(f"`{code}`" for code in dict.fromkeys(r.codes)) or "-"
        lines.append(
            f"| `{r.use_case_id}` | `{r.file_name}` | {_shown(r.rows)} | {_shown(r.columns)} | {_shown(r.errors)} | "
            f"{_shown(r.warnings)} | {_cell(', '.join(r.pii_columns)) or '-'} | {checks} |"
        )
    if crashed:
        lines += ["", "## Crashes", "", "| Use case | File | Step and exception |", "| :--- | :--- | :--- |"]
        lines += [f"| `{r.use_case_id}` | `{r.file_name}` | {_cell(r.crash or '')} |" for r in crashed]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=COMMAND, description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_OUT_DIR, help=f"default: {DEFAULT_OUT_DIR}")
    parser.add_argument("--report", type=Path, default=None, help=f"default: <data-dir>/{REPORT_FILENAME}")
    args = parser.parse_args(argv)
    if not args.data_dir.is_dir():
        parser.error(
            f"{args.data_dir} does not exist; write the files first: python -m scripts.generate_bad_test_data"
        )
    results = audit(args.data_dir)
    report_path = args.report or args.data_dir / REPORT_FILENAME
    report_path.write_text(
        render_report(results, data_dir=args.data_dir, now=datetime.now(UTC)), encoding="utf-8"
    )
    crashes = sum(1 for r in results if r.crashed)
    print(f"{len(results)} files audited, {crashes} crashed a step. Report: {report_path}")
    return 1 if crashes else 0


if __name__ == "__main__":
    sys.exit(main())
