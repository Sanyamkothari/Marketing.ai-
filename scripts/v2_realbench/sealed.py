"""Sealed datasets: opened once, at the very end, and never used to tune anything.

Two public randomised campaigns are kept SEALED. No policy, agent, baseline or fit may see them while
we build; they are for one final check that the numbers we got on the dev datasets were not luck.

* `lenta`: the Lenta uplift dataset (`/home/user/datasets/lenta`).
* `starbucks`: the Udacity Starbucks promotion experiment (`/home/user/datasets/starbucks_promo`).

What is allowed before the final check is a LOAD CHECK: how many rows, which columns, and what share
of rows were treated. This module's `load_check` reads one column per file (the treatment column) and
the header, and nothing else: it never reads an outcome column, so it cannot print an outcome.

What the final check does:

1. `python -m scripts.v2_realbench.run --dataset lenta --data-dir DIR --sealed-final` (the same for
   `starbucks`). Without `--sealed-final` the scoreboard refuses these names and reads nothing.
2. Before reading the data, the run appends a line to the "Sealed runs" table of
   `docs/v2/REAL_BENCH.md` (date, git sha, dataset, command). A crashed run still counts as opened:
   once seen, a sealed set is no longer sealed.
3. The sealed claim in `docs/v2/TARGETS.json` is judged on that one run. It is never re-run after a
   change to the code to get a better number.

No dataset-specific code lives in `autonomy/`; the adapters are in `adapters.py` as for the others.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

SEALED_DATASETS: tuple[str, ...] = ("lenta", "starbucks")
"""The names `run.py` accepts for sealed data (only with `--sealed-final`)."""

SEALED_HEADING = "## Sealed runs"
SEALED_PLACEHOLDER = "| none yet | | | |"
SEALED_TABLE_HEAD = "| Date (UTC) | Git sha | Dataset | Command |"
DEFAULT_LOG = Path(__file__).resolve().parents[2] / "docs" / "v2" / "REAL_BENCH.md"


@dataclass(frozen=True)
class SealedFile:
    """One file of a sealed dataset, and the only column the load check may read from it."""

    relpath: str
    treatment_column: str
    treated_value: str
    outcome_columns: tuple[str, ...]
    """Named so the load check can say it did not read them. Never read here."""


@dataclass(frozen=True)
class SealedSpec:
    name: str
    folder: str
    files: tuple[SealedFile, ...]


SEALED: dict[str, SealedSpec] = {
    "lenta": SealedSpec(
        name="lenta",
        folder="lenta",
        files=(
            SealedFile(
                relpath="lenta_dataset.csv.gz",
                treatment_column="group",
                treated_value="test",
                outcome_columns=("response_att", "response_sms", "response_viber"),
            ),
        ),
    ),
    "starbucks": SealedSpec(
        name="starbucks",
        folder="starbucks_promo",
        files=(
            SealedFile(
                relpath="training.csv",
                treatment_column="Promotion",
                treated_value="Yes",
                outcome_columns=("purchase",),
            ),
            SealedFile(
                relpath="Test.csv",
                treatment_column="Promotion",
                treated_value="Yes",
                outcome_columns=("purchase",),
            ),
        ),
    ),
}


def refusal(dataset: str, sealed_final: bool) -> str | None:
    """Why the scoreboard must not run `dataset` now, or None if it may.

    A sealed name needs `--sealed-final`; the flag with a dev dataset is a mistake (it would write a
    sealed-run record for data that is not sealed)."""
    if dataset in SEALED_DATASETS and not sealed_final:
        return (
            f"refusing --dataset {dataset}: it is a SEALED dataset (docs/v2/REAL_BENCH.md, 'Sealed runs'). "
            "It is opened once, at the end, with --sealed-final. Nothing was read."
        )
    if sealed_final and dataset not in SEALED_DATASETS:
        return (
            f"--sealed-final only applies to a sealed dataset ({', '.join(SEALED_DATASETS)}); "
            f"got --dataset {dataset}. Nothing was read."
        )
    return None


def load_check(name: str, data_dir: Path) -> dict[str, Any]:
    """Row counts, column names and treatment share of a sealed dataset. Reads only the header and the
    treatment column of each file: no outcome column is ever loaded."""
    if name not in SEALED:
        raise ValueError(f"{name!r} is not a sealed dataset; choose from {', '.join(SEALED)}")
    spec = SEALED[name]
    files: list[dict[str, Any]] = []
    for f in spec.files:
        path = data_dir / spec.folder / f.relpath
        header = pd.read_csv(path, nrows=0)
        columns = [str(c) for c in header.columns]
        if f.treatment_column not in columns:
            raise ValueError(f"{path}: no treatment column {f.treatment_column!r}; columns: {columns}")
        treat = pd.read_csv(path, usecols=[f.treatment_column], dtype=str)[f.treatment_column]
        rows = len(treat)
        treated = int((treat == f.treated_value).sum())
        files.append(
            {
                "file": f.relpath,
                "rows": rows,
                "n_columns": len(columns),
                "columns": columns,
                "treatment_column": f.treatment_column,
                "treated_value": f.treated_value,
                "treated_rows": treated,
                "treatment_share": treated / rows if rows else None,
                "outcome_columns_not_read": [c for c in f.outcome_columns if c in columns],
            }
        )
    total = sum(int(x["rows"]) for x in files)
    treated_total = sum(int(x["treated_rows"]) for x in files)
    return {
        "dataset": name,
        "files": files,
        "rows": total,
        "treatment_share": treated_total / total if total else None,
        "note": "load check only: header and treatment column read; no outcome column was loaded",
    }


def render_load_check(check: dict[str, Any]) -> str:
    share = check["treatment_share"]
    if share is None:
        return f"{check['dataset']}: no rows"
    lines = [f"{check['dataset']}: {check['rows']:,} rows, treatment share {share:.4f}"]
    for f in check["files"]:
        lines.append(
            f"  {f['file']}: {f['rows']:,} rows, {f['n_columns']} columns, treatment column "
            f"{f['treatment_column']!r} (treated = {f['treated_value']!r}) share "
            f"{f['treatment_share']:.4f}; outcome columns not read: {', '.join(f['outcome_columns_not_read'])}"
        )
        lines.append("    columns: " + ", ".join(f["columns"]))
    return "\n".join(lines)


def record_sealed_run(
    doc: Path, *, dataset: str, git_sha: str | None, dirty: bool, command: str, now: datetime
) -> str:
    """Append one row to the 'Sealed runs' table of `doc` (replacing the 'none yet' placeholder).

    Returns the row. The date is passed in so the function is deterministic. Raises if the section or
    its table is missing: a sealed run that cannot be recorded must not start."""
    text = doc.read_text()
    lines = text.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == SEALED_HEADING)
    except StopIteration:
        raise ValueError(f"{doc}: no '{SEALED_HEADING}' section; add it before a sealed run") from None
    head = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == SEALED_TABLE_HEAD), None)
    if head is None:
        raise ValueError(f"{doc}: the Sealed runs table header {SEALED_TABLE_HEAD!r} is missing")
    end = head + 2  # header and separator
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    sha = (git_sha or "unknown")[:12] + (" (dirty)" if dirty else "")
    safe_command = command.replace("|", "\\|")
    row = f"| {now.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')} | {sha} | {dataset} | `{safe_command}` |"
    body = [ln for ln in lines[head + 2 : end] if ln.strip() != SEALED_PLACEHOLDER]
    lines[head + 2 : end] = [*body, row]
    doc.write_text("\n".join(lines))
    return row


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Load check of a sealed dataset: rows, columns, treatment share. Nothing else."
    )
    ap.add_argument("--dataset", choices=[*SEALED_DATASETS, "all"], default="all")
    ap.add_argument("--data-dir", type=Path, default=Path("/home/user/datasets"))
    ap.add_argument("--json", type=Path, help="write the check here")
    args = ap.parse_args(argv)
    names = list(SEALED_DATASETS) if args.dataset == "all" else [args.dataset]
    checks = [load_check(n, args.data_dir) for n in names]
    for c in checks:
        print(render_load_check(c))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {"checked_at": datetime.now(UTC).isoformat(timespec="seconds"), "checks": checks}, indent=1
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
