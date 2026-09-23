"""Measure what the deployment costs, write it down with its sources, and fill guide §6 only when whole.

Plan M50 asks for every `NOT YET MEASURED` cell in `docs/AWS_DEPLOYMENT.md` §6 to be replaced: idle
monthly cost, cost per Fast/Balanced/Exhaustive training run, per 100,000 scored rows, per 1,000
assistant questions. This command is the whole of that step. The logic, and the reasons for every
refusal, are in `engine/aws/cost_capture.py`; this file is the operator's end of it.

Three subcommands:

* ``measure`` queries Cost Explorer (us-east-1, whatever the deployment's region) and reads run
  manifests and assistant usage snapshots, then writes ``reports/costs/<date>-<label>.json``: every
  cell, whether it is **billed** (Cost Explorer), a **list-price estimate** (a rate card times a
  reported quantity) or **reported** (seconds, instance types, tokens), its full source, and - for a
  cell it could not fill - the reason. It never writes a number it did not read.
* ``render`` merges one or more measurement files and shows what the two tables would become.
  ``--write-doc`` (off by default, on ``measure`` too) replaces a table in the guide **only when
  every cell of that table has a figure**; a table with one gap is left exactly as it is and the
  gaps are printed. Partial data changes nothing.
* ``scoring-file`` makes the 100,000-row scoring file from the public reference file, by repeating
  its rows under fresh primary keys, because the per-100,000-rows figure is refused from a smaller
  file (scaling a small job up multiplies its fixed start-up cost too).

The measurement day, in order
-----------------------------
Dates are UTC days. ``D1`` … ``D5`` are placeholders; every step that reads Cost Explorer is run at
least one full day after the last day it reads, because Cost Explorer lags by up to 24 hours and a
bill read early is a partial bill (the tool refuses rather than record one).

0. **Before anything is measured** (docs/M50_CHECKLIST.md §3, §7): deploy dev, set the budget and
   the one-job cap, and fill ``configs/llm_prices.yaml`` from the provider's price list (``as_of`` and
   ``source`` set) or the assistant's list-price cell stays unmeasured. Activate the cost-allocation
   tags ``product``, ``env`` and ``client`` in Billing and Cost Management at once.
1. **One throwaway training run** of anything, so that a resource carrying the ``run_id`` tag
   exists: a tag key is offered for activation only after something carries it. Then activate
   ``run_id`` and ``use_case`` too, and wait until the console shows them Active (up to 24 hours).
   A tag counts only for usage *after* activation, so no measured run starts before this.
2. **Idle day ``D1``**: a whole UTC day with no run, no scoring and no question. On ``D1 + 2``:

      .venv/bin/python -m scripts.measure_costs measure --env dev \\
        --idle-start D1 --idle-end D1+1 --label idle-dev

   (``--idle-end`` is exclusive, as Cost Explorer's is. A longer window - a week, or a whole calendar
   month - is better; a whole month is recorded as billed, anything shorter is projected at 730
   hours/month and says so.) Credits and refunds are left out of every query, so a credited new
   account still shows what the deployment costs. A row with **no line at all** stays unmeasured,
   because an absent line is not a zero: the ECS service in ``infra/compute.py`` does not yet
   propagate its tags to its Fargate tasks, so under the tag filter the Fargate row has no line
   although it is billed. Until it does, measure the idle column with ``--account-wide`` in an
   account that holds this one deployment and nothing else. For a row the deployment genuinely has
   no resource for (``network`` when there is neither a NAT gateway nor an endpoint), add
   ``--absent-idle-row network``; Cost Explorer showing a line for it anyway refuses the row.
3. **Training, one run per strategy**, each on its own day ``D2``, ``D3``, ``D4`` (tag attribution
   does not need separate days, but ``--attribution day`` - the fallback when ``run_id`` was not
   active - does): the public Telco Churn file, all 7,043 rows
   (``python library/telco-customer-churn/fetch.py`` writes ``library/telco-customer-churn/data/
   prepared.csv``), strategy ``fast``, then ``balanced``, then ``exhaustive``, every other setting at
   its default. The ``balanced`` run is also the table's "Train, Telco Churn (7,043 rows)" row. Also
   **score the same 7,043-row file** with the champion, and train the "template-sized file" once.
   The guide does not say what that file is and this tool does not check its size (the template CSV
   itself, ``templates/telco_churn_template.csv``, has five rows - too few to train on); until the
   guide defines it, use the committed ``library/telco-customer-churn/sample.csv`` (5,000 rows) and
   record the choice with ``--note``.
4. **Scoring 100,000 rows**, on any day after a champion exists:

      .venv/bin/python -m scripts.measure_costs scoring-file \\
        --source library/telco-customer-churn/data/prepared.csv --primary-key customerID \\
        --rows 100000 --out /tmp/score-100k.csv

   then upload it and score it with the champion.
5. **Download every manifest** (guide §5.2), one file per table row:

      mkdir -p manifests
      curl -s -H "Authorization: Bearer $TOKEN" \\
        "$SERVICE_URL/runs/$RUN_ID/artefacts/run_manifest.json" > manifests/train-fast.json

6. **Assistant day ``D5``** - not the day of the Bedrock smoke test, and not the day the index was
   built: both are billed on the same account-wide Bedrock line. Build the index over the Telco
   documents on an earlier day; on ``D5`` snapshot its usage, ask **100** distinct questions (20
   is the checklist's minimum; more gives a steadier per-question figure, and a repeated question is
   a cache hit that costs nothing), and snapshot again:

      curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/indexes/$INDEX_ID" > usage-before.json
      # ... ask the questions ...
      curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/indexes/$INDEX_ID" > usage-after.json

7. **On ``D5 + 2`` or later, the one command** that measures everything else, folds in the idle
   file and, if every cell of a table is filled, writes that table into the guide:

      .venv/bin/python -m scripts.measure_costs measure --env dev --label runs-dev \\
        --manifest train-template=manifests/train-template.json \\
        --manifest train-reference=manifests/train-balanced.json \\
        --manifest score-reference=manifests/score-reference.json \\
        --manifest train-fast=manifests/train-fast.json \\
        --manifest train-balanced=manifests/train-balanced.json \\
        --manifest train-exhaustive=manifests/train-exhaustive.json \\
        --manifest score-per-100k=manifests/score-100k.json \\
        --assistant-before usage-before.json --assistant-after usage-after.json \\
        --assistant-questions 100 --assistant-day D5 \\
        --merge reports/costs/<D1+2>-idle-dev.json --write-doc

The per-run table is complete after step 7. The idle table is not, and must not be: its second
column is prod, which needs a prod deployment and its own idle day (step 2 with ``--env prod``, then
``render --write-doc`` over both files). When a table *is* written,
`test_the_cost_tables_are_entirely_unmeasured` starts failing, by design: the same change replaces it
with a check that no table is `TableState.PARTIAL`, and rewrites §6's opening sentence, which says
nothing was measured (docs/M50_CHECKLIST.md §8).

Exit codes: 0 done; 2 the inputs were unusable (nothing written); 3 ``--write-doc`` was asked for and
at least one table was withheld because it had a gap; 4 Cost Explorer refused the call (nothing
written). The caller needs ``ce:GetCostAndUsage`` - the operator's own credentials, not the task
role's, which carries no billing permission.
"""

from __future__ import annotations

import argparse
import json
import math
import shlex
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from engine.aws.cost_capture import (
    IDLE_ROWS,
    PER_RUN_ROWS,
    Attribution,
    Cell,
    CellStatus,
    CostCaptureError,
    CostExplorer,
    CostExplorerQuery,
    CostMeasurement,
    IdleRow,
    IdleWindow,
    RunRow,
    TableOutcome,
    assistant_cells,
    boto3_cost_explorer,
    idle_cells,
    merge_cells,
    render_document,
    run_cells,
    run_days,
)
from engine.contracts import RunManifest
from engine.generative.budget import load_prices
from engine.generative.contracts import LlmUsageReport
from engine.runs import PRODUCT_TAG

__all__ = ["build_parser", "main", "make_scoring_file", "measure"]

COMMAND: str = "python -m scripts.measure_costs"
"""How this is invoked; quoted in messages so a reader can copy the fix."""

EXIT_OK: int = 0
EXIT_BAD_INPUT: int = 2
EXIT_WITHHELD: int = 3
EXIT_AWS: int = 4
"""Cost Explorer refused the call: credentials, `ce:GetCostAndUsage` permission, or throttling."""

DEFAULT_DOC: Path = Path("docs/AWS_DEPLOYMENT.md")
DEFAULT_OUT_DIR: Path = Path("reports/costs")

_ROWS_BY_OPTION: dict[str, RunRow] = {row.option: row for row in PER_RUN_ROWS}
_IDLE_ROWS_BY_OPTION: dict[str, IdleRow] = {row.option: row for row in IDLE_ROWS}


class InputError(Exception):
    """An input the operator gave that cannot be used; nothing is written."""


# ---------------------------------------------------------------------------
# Reading inputs
# ---------------------------------------------------------------------------
def _pair(raw: str) -> tuple[RunRow, Path]:
    """`train-fast=manifests/train-fast.json` -> (row, path)."""
    name, sep, path = raw.partition("=")
    if not sep or not path:
        raise InputError(f"--manifest takes ROW=PATH, got {raw!r}.")
    row = _ROWS_BY_OPTION.get(name.strip())
    if row is None:
        raise InputError(f"--manifest row {name!r} is not one of: {', '.join(sorted(_ROWS_BY_OPTION))}.")
    return row, Path(path)


def _read_json(path: Path) -> Any:
    """The JSON document at `path`, or an `InputError` naming the file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"{path} could not be read as JSON: {exc.__class__.__name__}.") from exc


def read_manifest(path: Path) -> RunManifest:
    """A `run_manifest.json` as downloaded with the guide §5.2 command."""
    try:
        return RunManifest.model_validate(_read_json(path))
    except ValueError as exc:
        raise InputError(f"{path} is not a run manifest: {exc.__class__.__name__}.") from exc


def read_usage(path: Path) -> LlmUsageReport:
    """An index's `llm_usage.json`: bare, or the `llm_usage` member of `GET /indexes/{id}`."""
    document = _read_json(path)
    if isinstance(document, dict) and "llm_usage" in document and "totals" not in document:
        document = document["llm_usage"]
    if document is None:
        raise InputError(f"{path} carries no llm_usage: the index has made no metered call yet.")
    try:
        return LlmUsageReport.model_validate(document)
    except ValueError as exc:
        raise InputError(f"{path} is not an llm_usage report: {exc.__class__.__name__}.") from exc


def read_measurement(path: Path) -> CostMeasurement:
    """A measurement file this command wrote earlier."""
    try:
        return CostMeasurement.model_validate(_read_json(path))
    except ValueError as exc:
        raise InputError(f"{path} is not a cost measurement: {exc.__class__.__name__}.") from exc


def _idle_row_id(raw: str) -> str:
    """`--absent-idle-row nat-gateway`-style names -> the idle row's id, or an `InputError`."""
    row = _IDLE_ROWS_BY_OPTION.get(raw.strip())
    if row is None:
        raise InputError(
            f"--absent-idle-row {raw!r} is not one of: {', '.join(sorted(_IDLE_ROWS_BY_OPTION))}."
        )
    return row.id


def _day(raw: str | None, option: str) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise InputError(f"{option} takes a date as YYYY-MM-DD, got {raw!r}.") from exc


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------
def measure(
    args: argparse.Namespace,
    *,
    client: CostExplorer | None,
    now: datetime,
    command: str,
) -> CostMeasurement:
    """Every cell the arguments ask for, with Cost Explorer through `client` (None: not queried)."""
    today = now.astimezone(UTC).date()
    cells: list[Cell] = []
    queries: list[CostExplorerQuery] = []
    notes: list[str] = list(args.note or [])

    manifests = [(row, read_manifest(path)) for row, path in (_pair(raw) for raw in args.manifest or [])]
    seen_rows = [row.id for row, _ in manifests]
    duplicates = sorted({row for row in seen_rows if seen_rows.count(row) > 1})
    if duplicates:
        raise InputError(f"Each row takes one manifest; given twice: {', '.join(duplicates)}.")
    days_by_run = {manifest.run_id: run_days(manifest) for _, manifest in manifests}
    attribution = Attribution(args.attribution)
    for row, manifest in manifests:
        row_cells, row_queries = run_cells(
            row, manifest, client=client, attribution=attribution, today=today, other_runs=days_by_run
        )
        cells.extend(row_cells)
        queries.extend(row_queries)

    start, end = _day(args.idle_start, "--idle-start"), _day(args.idle_end, "--idle-end")
    if (start is None) != (end is None):
        raise InputError("--idle-start and --idle-end are given together or not at all.")
    if start is not None and end is not None:
        tags = None if args.account_wide else {"product": PRODUCT_TAG, "env": args.env}
        if tags is not None and args.client:
            tags["client"] = args.client
        idle, idle_queries, idle_notes = idle_cells(
            client,
            env=args.env,
            region=args.region,
            window=IdleWindow(start, end),
            tags=tags,
            today=today,
            run_days=days_by_run,
            absent=[_idle_row_id(raw) for raw in args.absent_idle_row or []],
        )
        cells.extend(idle)
        queries.extend(idle_queries)
        notes.extend(idle_notes)

    assistant = (args.assistant_before, args.assistant_after, args.assistant_questions, args.assistant_day)
    if any(value is not None for value in assistant):
        if any(value is None for value in assistant):
            raise InputError(
                "--assistant-before, --assistant-after, --assistant-questions and --assistant-day are "
                "given together or not at all."
            )
        day = _day(args.assistant_day, "--assistant-day")
        assert day is not None
        prices = load_prices(args.configs)
        row_cells, row_queries = assistant_cells(
            read_usage(Path(args.assistant_before)),
            read_usage(Path(args.assistant_after)),
            questions=int(args.assistant_questions),
            day=day,
            prices=prices,
            client=client,
            today=today,
        )
        cells.extend(row_cells)
        queries.extend(row_queries)

    if not cells:
        raise InputError("Nothing to measure: give an idle window, a manifest or the assistant snapshots.")
    return CostMeasurement(
        measured_at=now, command=command, cells=tuple(cells), queries=tuple(queries), notes=tuple(notes)
    )


def write_measurement(measurement: CostMeasurement, out_dir: Path, label: str) -> Path:
    """`<out_dir>/<date>-<label>.json`, never overwriting an earlier file of the same day."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{measurement.measured_at.astimezone(UTC).date().isoformat()}-{label}"
    path = out_dir / f"{stem}.json"
    counter = 2
    while path.exists():
        path = out_dir / f"{stem}-{counter}.json"
        counter += 1
    path.write_text(measurement.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Reporting and the document
# ---------------------------------------------------------------------------
def describe_cells(cells: Sequence[Cell]) -> list[str]:
    """One line per cell: what it will read, or why it stays unmeasured."""
    lines: list[str] = []
    for cell in cells:
        where = f"{cell.table.value}/{cell.row}/{cell.column}"
        if cell.status == CellStatus.NOT_MEASURED:
            lines.append(f"  {where}: NOT YET MEASURED - {cell.reason}")
        else:
            kind = cell.kind.value if cell.kind else cell.status.value
            lines.append(f"  {where} [{kind}]: {cell.text}")
    return lines


def describe_outcomes(outcomes: Sequence[TableOutcome], *, write: bool) -> list[str]:
    """What happened, or would happen, to each table of the guide."""
    lines: list[str] = []
    for outcome in outcomes:
        if outcome.complete:
            verb = "written" if write else "complete; --write-doc would write it"
            lines.append(f"{outcome.heading}: {verb}.")
        else:
            lines.append(
                f"{outcome.heading}: left unchanged, {len(outcome.missing)} cell(s) without a figure:"
            )
            lines.extend(f"  {reason}" for reason in outcome.missing)
    return lines


def apply_to_document(
    measurements: Sequence[CostMeasurement], doc: Path, *, write: bool
) -> tuple[list[str], bool]:
    """Render the guide from `measurements`; write it only with `write` and only whole tables.

    Returns the report lines and whether any table was withheld.
    """
    text = doc.read_text(encoding="utf-8")
    rendered, outcomes = render_document(text, merge_cells(measurements))
    lines = describe_outcomes(outcomes, write=write)
    if write and rendered != text:
        doc.write_text(rendered, encoding="utf-8")
        lines.append(
            f"{doc} changed. tests/unit/test_docs_honesty.py::test_the_cost_tables_are_entirely_"
            "unmeasured now fails by design: in the same change, replace it with a check that no "
            "table is partial (engine.aws.cost_capture.table_state) and rewrite §6's opening "
            "sentence (docs/M50_CHECKLIST.md §8)."
        )
    withheld = any(not outcome.complete for outcome in outcomes)
    return lines, withheld


# ---------------------------------------------------------------------------
# The 100,000-row scoring file
# ---------------------------------------------------------------------------
def make_scoring_file(
    source: Path, *, primary_key: str, rows: int, out: Path, drop: Sequence[str] = ()
) -> int:
    """Repeat `source`'s rows until there are `rows`, giving every copy after the first a fresh key.

    The values are the reference file's, repeated; only the key changes, so every row is unique to
    the scorer. What a scoring job costs depends on how many rows it scores, not on which values they
    hold, which is why a repeated public file is a fair stand-in and a customer's file is never
    needed. Returns the number of rows written.
    """
    import pandas as pd

    if rows <= 0:
        raise InputError("--rows must be positive.")
    frame = pd.read_csv(source, dtype=str, keep_default_na=False)
    if primary_key not in frame.columns:
        raise InputError(f"{source} has no column {primary_key!r}.")
    unknown = sorted(set(drop) - set(frame.columns))
    if unknown:
        raise InputError(f"{source} has no column(s) {', '.join(unknown)} to drop.")
    if frame.empty:
        raise InputError(f"{source} has no rows to repeat.")
    copies = math.ceil(rows / len(frame))
    parts = []
    for copy in range(copies):
        part = frame.copy()
        if copy:
            part[primary_key] = part[primary_key] + f"-x{copy}"
        parts.append(part)
    result = pd.concat(parts, ignore_index=True).head(rows).drop(columns=list(drop))
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    return len(result)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """The three subcommands; the module docstring is the operator's guide to them."""
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description="Measure deployment costs from Cost Explorer and run manifests, and fill guide §6.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("measure", help="query Cost Explorer and manifests; write reports/costs/*.json")
    run.add_argument("--env", default="dev", help="deployment env tag and idle column (default: dev)")
    run.add_argument("--region", default="ap-south-1", help="deployment region (default: ap-south-1)")
    run.add_argument("--client", default=None, help="the deployment's client tag, when it has one")
    run.add_argument("--idle-start", default=None, help="first day of the idle window, YYYY-MM-DD")
    run.add_argument("--idle-end", default=None, help="day after the idle window's last, YYYY-MM-DD")
    run.add_argument(
        "--account-wide",
        action="store_true",
        help="no tag filter for idle cost: only for an account holding this deployment alone",
    )
    run.add_argument(
        "--absent-idle-row",
        action="append",
        metavar="ROW",
        help="an idle row this deployment has no resource for (e.g. network when it has neither a NAT "
        f"gateway nor endpoints); rows: {', '.join(_IDLE_ROWS_BY_OPTION)}",
    )
    run.add_argument(
        "--manifest",
        action="append",
        metavar="ROW=PATH",
        help=f"a run manifest for one per-run row; rows: {', '.join(_ROWS_BY_OPTION)}",
    )
    run.add_argument(
        "--attribution",
        choices=[item.value for item in Attribution],
        default=Attribution.TAG.value,
        help="billed per run by the run_id tag (default) or by the whole day's SageMaker line",
    )
    run.add_argument("--assistant-before", default=None, help="index usage snapshot before the questions")
    run.add_argument("--assistant-after", default=None, help="index usage snapshot after the questions")
    run.add_argument("--assistant-questions", type=int, default=None, help="questions asked in between")
    run.add_argument("--assistant-day", default=None, help="UTC day the questions were asked, YYYY-MM-DD")
    run.add_argument("--configs", type=Path, default=None, help="configuration root (default: configs/)")
    run.add_argument("--no-cost-explorer", action="store_true", help="do not query Cost Explorer")
    run.add_argument("--note", action="append", help="a sentence to record in the measurement file")
    run.add_argument("--label", default=None, help="file name label (default: the env)")
    run.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="default: reports/costs")
    run.add_argument("--merge", action="append", type=Path, help="earlier measurement files to fold in")
    run.add_argument("--doc", type=Path, default=DEFAULT_DOC, help="default: docs/AWS_DEPLOYMENT.md")
    run.add_argument("--write-doc", action="store_true", help="write each table that has no gap")

    render = commands.add_parser("render", help="show, and optionally write, the guide's tables")
    render.add_argument("measurements", nargs="+", type=Path, help="measurement files to merge")
    render.add_argument("--doc", type=Path, default=DEFAULT_DOC, help="default: docs/AWS_DEPLOYMENT.md")
    render.add_argument("--write-doc", action="store_true", help="write each table that has no gap")

    scoring = commands.add_parser(
        "scoring-file", help="repeat a public file to N rows for the scoring figure"
    )
    scoring.add_argument("--source", type=Path, required=True, help="the reference CSV")
    scoring.add_argument("--primary-key", required=True, help="its primary-key column")
    scoring.add_argument("--rows", type=int, default=100_000, help="rows to write (default: 100000)")
    scoring.add_argument("--out", type=Path, required=True, help="where to write the CSV")
    scoring.add_argument("--drop", action="append", default=[], help="a column to leave out")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client: CostExplorer | None = None,
    now: datetime | None = None,
) -> int:
    """Run one subcommand. `client` and `now` exist for tests; the defaults are the real ones."""
    args = build_parser().parse_args(argv)
    moment = now or datetime.now(UTC)
    try:
        if args.command == "scoring-file":
            written = make_scoring_file(
                args.source, primary_key=args.primary_key, rows=args.rows, out=args.out, drop=args.drop
            )
            print(f"{args.out}: {written:,} rows from {args.source}")
            return EXIT_OK
        if not args.doc.is_file():
            raise InputError(f"{args.doc} does not exist; run from the repository root or pass --doc.")
        if args.command == "render":
            measurements = [read_measurement(path) for path in args.measurements]
            lines, withheld = apply_to_document(measurements, args.doc, write=args.write_doc)
            print("\n".join(lines))
            return EXIT_WITHHELD if (args.write_doc and withheld) else EXIT_OK
        earlier = [read_measurement(path) for path in args.merge or []]
    except (InputError, CostCaptureError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_INPUT

    aws_errors: tuple[type[Exception], ...] = ()
    explorer = client
    if explorer is None and not args.no_cost_explorer:
        from botocore.exceptions import BotoCoreError, ClientError

        aws_errors = (BotoCoreError, ClientError)
        explorer = boto3_cost_explorer()
    command = " ".join([COMMAND, *(shlex.quote(part) for part in (argv or sys.argv[1:]))])
    try:
        measurement = measure(
            args, client=None if args.no_cost_explorer else explorer, now=moment, command=command
        )
    except (InputError, CostCaptureError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_INPUT
    except aws_errors as exc:
        print(f"Cost Explorer refused the call ({exc.__class__.__name__}): {exc}", file=sys.stderr)
        return EXIT_AWS
    path = write_measurement(measurement, args.out_dir, args.label or args.env)
    print(f"Wrote {path}")
    print("\n".join(describe_cells(measurement.cells)))
    for note in measurement.notes:
        print(f"note: {note}")
    lines, withheld = apply_to_document([*earlier, measurement], args.doc, write=args.write_doc)
    print("\n".join(lines))
    return EXIT_WITHHELD if (args.write_doc and withheld) else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
