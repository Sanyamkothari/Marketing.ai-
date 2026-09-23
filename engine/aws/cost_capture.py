"""Turning a deployment's bill into the two cost tables, or saying exactly why a cell cannot be filled.

`docs/AWS_DEPLOYMENT.md` §6 ships two tables - idle cost per month, and cost per run - with every
cell holding the literal marker `NOT YET MEASURED`, and `tests/unit/test_docs_honesty.py` fails the
build if any one of them stops reading that way (DEC-397, DEC-399). That is the right state for a
repository with no AWS account behind it, and it makes the day the account exists expensive: two
tables, forty-two cells for one environment, three sources per cell, and a rule that each figure
has to arrive with where it came from. Done by hand that is an afternoon of copying numbers out of a
console, and an afternoon of copying is exactly how an unsourced figure gets into a document. So the
measurement is code, and a person runs one command.

**Three kinds of figure, never blended.** Each `Cell` says which kind it holds:

* `billed` - Cost Explorer's `UnblendedCost`. This is the bill: what AWS charged the account, after
  whatever free tier or credit applied, lagging by up to a day.
* `list_price_estimate` - a reported quantity multiplied by a published rate card: the run manifest's
  `CostEstimate`, which is billable seconds times `configs/aws_prices.yaml` (DEC-330), or tokens times
  `configs/llm_prices.yaml`. An order of magnitude for a rate card, not an invoice line (§6.3).
* `reported` - a quantity a platform reported and nobody priced: billable seconds, an instance type,
  token counts.

They are printed side by side in §6.2 on purpose, and they are labelled here so a reader of the
measurement file can never take one for another.

**Refusing to invent a number is the design, not a failure mode.** A cell that cannot be filled
honestly stays `NOT YET MEASURED`, and its `reason` says why in words an operator can act on: the tags
were not active, Cost Explorer has not caught up yet, a manifest was of the wrong strategy, a scoring
file was too small to scale up from, a model had no price. There is exactly one other way for a
cell to be filled without a number: `not_applicable`, for a figure that does not exist *by
construction* - a SageMaker processing job reports no billable time (DEC-332), so the scoring rows'
"billable seconds" and "estimated at list price" cells say so and cite the decision. That is a fact
about the platform, decided once, not a gap papered over.

**A table is written whole or not at all.** `render_document` replaces the cells of a table only when
every cell of that table is `measured` or `not_applicable`. A half-filled table reads as though the
whole table had been measured - precisely the mistake the honesty test exists to catch - so partial
data changes nothing in the document and the reasons are printed instead. The first complete write
*does* make `test_the_cost_tables_are_entirely_unmeasured` fail, deliberately: `docs/M50_CHECKLIST.md`
§8 says the move into the guide is one change that also replaces that test with a check that every
cell is either the marker or a sourced figure, and `table_state` below is the helper that check needs.

**Where the numbers come from, and why these queries.**

* *Idle.* `GetCostAndUsage` over a stated window, filtered to the deployment's region and its
  cost-allocation tags (`product`, `env`, and `client` when the deployment has one - `infra/app.py`
  puts them on every resource), grouped by service and usage type. Usage type is the second
  grouping because the table splits RDS into instance and storage, and "NAT gateway" lives inside
  `EC2 - Other`. Every line lands in exactly one row or in `unassigned`; the total is the total of
  *every* line, so a line no row claims still counts and the Total cell says how much of it there
  was. A row with no line at all is *not* a zero - an untagged resource is missing from a
  tag-filtered answer while it is billed - so it stays unmeasured unless the operator states the
  deployment has no such resource. Credits and refunds are left out of every query: a credited
  new account would otherwise show a deployment costing nothing. A window that is not a whole
  calendar month is projected to a month at AWS's own 730-hours-per-month convention, and the cell
  says it was projected and from how many days.
* *Per run.* The manifest's `ComputeInfo` and `CostEstimate` give the first three columns. The
  billed column is Cost Explorer over the UTC days the job ran, attributed one of two ways: by the
  job's `run_id` tag (`engine/runs.py` puts `product`, `use_case`, `run_id` and `client` on every
  SageMaker job), which is exact once `run_id` is an *active* cost-allocation tag, or by the whole
  day's SageMaker line, which is honest only when nothing else ran that day - so it is refused when
  another measured run shares the day.
* *Assistant.* Tokens are the difference between two snapshots of the index's `llm_usage.json`,
  taken before and after the questions, so the build's embeddings are not charged to the questions.
  Bedrock on-demand calls carry no resource tag, and third-party models are billed as their own
  "(Amazon Bedrock Edition)" services, so the billed figure is every service with "Bedrock" in its
  name, account-wide, for the one day the questions were asked - and the cell says account-wide.

Normalisation is linear and stated: per 100,000 scored rows is refused from a file *smaller* than
100,000 rows, because scaling a small job up multiplies its fixed start-up cost with it; per 1,000
questions multiplies by 1,000 over the count asked, and says how many were asked.

No boto3 import at module scope (DEC-306): `CostExplorer` is a protocol with the one method this
module calls, the boto3 client satisfies it structurally, and `boto3_cost_explorer` builds one
lazily. Tests hand in a stub; nothing here ever needs an account to be exercised.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol

from pydantic import AwareDatetime, Field

from engine.config import Strategy, StrictBase
from engine.contracts import ComputeBackend, JobEntrypoint, RunManifest
from engine.generative.budget import PriceTable as LlmPriceTable
from engine.generative.contracts import GenerativePurpose, LlmUsageReport

__all__ = [
    "ASSISTANT_ROW",
    "COST_EXPLORER_REGION",
    "DAYS_PER_MONTH",
    "IDLE_HEADING",
    "IDLE_ROWS",
    "IDLE_TOTAL_ROW",
    "MARKER",
    "PER_RUN_COLUMNS",
    "PER_RUN_HEADING",
    "PER_RUN_ROWS",
    "QUESTIONS_PER_UNIT",
    "Attribution",
    "Cell",
    "CellStatus",
    "CostCaptureError",
    "CostExplorer",
    "CostExplorerLine",
    "CostExplorerQuery",
    "CostMeasurement",
    "FigureKind",
    "IdleRow",
    "IdleWindow",
    "RunRow",
    "TableId",
    "TableOutcome",
    "TableState",
    "assistant_cells",
    "boto3_cost_explorer",
    "idle_cells",
    "idle_column",
    "merge_cells",
    "render_document",
    "run_cells",
    "run_days",
    "table_state",
]

MARKER: Final[str] = "NOT YET MEASURED"
"""What an unmeasured cell holds; the same literal `tests/unit/test_docs_honesty.py` compares against."""

COST_EXPLORER_REGION: Final[str] = "us-east-1"
"""Not chosen: Cost Explorer's API is served from us-east-1 whatever region the deployment is in."""

METRIC: Final[str] = "UnblendedCost"
"""The metric §6.1's command reads: what was charged, before any blended or amortised view."""

EXCLUDED_RECORD_TYPES: Final[tuple[str, ...]] = ("Credit", "Refund")
"""Record types every query leaves out, and every billed cell's source says so.

A new account very often runs on promotional credits, and `UnblendedCost` nets them off: the idle
table of a credited account would read USD 0.00 per component, which is a fact about who paid, not
about what the deployment costs. A refund is the same kind of line. Free tier is *not* excluded and
cannot be - it is priced into the usage line itself - so the source says "credits and refunds
excluded", and nothing more.
"""

CURRENCY: Final[str] = "USD"
"""The only unit a figure here is written in; any other unit in a response is refused, not converted."""

_EXCLUDED: Final[str] = "credits and refunds excluded"
"""How every billed source names `EXCLUDED_RECORD_TYPES`."""

HOURS_PER_MONTH: Final[float] = 730.0
"""AWS's own convention for a month (the AWS Pricing Calculator's), used only to project a window."""

DAYS_PER_MONTH: Final[float] = HOURS_PER_MONTH / 24.0
"""30.42 days: the projection factor, printed beside every projected figure."""

QUESTIONS_PER_UNIT: Final[int] = 1_000
"""Plan M50's unit for the assistant: cost per 1,000 questions."""

IDLE_HEADING: Final[str] = "#### Idle cost, per month"
PER_RUN_HEADING: Final[str] = "#### Per-run cost"
"""The two headings in `docs/AWS_DEPLOYMENT.md` whose tables this module fills, verbatim."""


class CostCaptureError(Exception):
    """A measurement that cannot proceed at all, with a machine `code` and a sentence for a person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CostExplorer(Protocol):
    """The one Cost Explorer call this module makes. A boto3 `ce` client satisfies it as it is."""

    def get_cost_and_usage(self, **kwargs: Any) -> Mapping[str, Any]:
        """`GetCostAndUsage`, keyword arguments exactly as boto3 takes them."""
        ...


def boto3_cost_explorer() -> CostExplorer:
    """A real Cost Explorer client, in us-east-1; boto3 is imported here and nowhere else (DEC-306)."""
    import boto3

    client: CostExplorer = boto3.client("ce", region_name=COST_EXPLORER_REGION)
    return client


# ---------------------------------------------------------------------------
# What a measurement file holds
# ---------------------------------------------------------------------------
class TableId(StrEnum):
    """Which of the two §6 tables a cell belongs to."""

    IDLE = "idle"
    PER_RUN = "per_run"


class CellStatus(StrEnum):
    """Whether a cell holds a figure, a structural not-applicable, or nothing yet."""

    MEASURED = "measured"
    NOT_APPLICABLE = "not_applicable"
    NOT_MEASURED = "not_measured"


class FigureKind(StrEnum):
    """Which kind of number a measured cell is. Never blended; see the module docstring."""

    BILLED = "billed"
    LIST_PRICE_ESTIMATE = "list_price_estimate"
    REPORTED = "reported"


class Cell(StrictBase):
    """One cell of one §6 table: its figure, how it reads in the document, and where it came from."""

    table: TableId = Field(description="Which §6 table the cell belongs to.")
    row: str = Field(description="Row id, as `IDLE_ROWS` / `PER_RUN_ROWS` name it.")
    column: str = Field(description="Column id: `<region>/<env>` for idle, a `PER_RUN_COLUMNS` key per run.")
    status: CellStatus = Field(description="Measured, not applicable by construction, or not measured.")
    kind: FigureKind | None = Field(
        default=None, description="Billed, list-price estimate or reported quantity; null when not measured."
    )
    value: float | None = Field(default=None, description="The number, in `unit`; null when there is none.")
    unit: str | None = Field(default=None, description="Unit of `value`: USD, USD/month, seconds, tokens.")
    text: str = Field(description="What the document cell reads; the marker when not measured.")
    source: str = Field(description="Where the figure came from, in full: API, window, filter, file.")
    reason: str | None = Field(
        default=None, description="Why there is no figure; set whenever status is not `measured`."
    )


class CostExplorerLine(StrictBase):
    """One group of one period of a `GetCostAndUsage` response, as it was returned."""

    start: date = Field(description="First day of the period.")
    end: date = Field(description="Day after the last day of the period (Cost Explorer's convention).")
    keys: tuple[str, ...] = Field(description="The group's keys, in the order the query grouped by.")
    amount: float = Field(description="The metric's amount for this group and period.")
    unit: str = Field(description="The metric's unit as returned; only USD is accepted.")
    estimated: bool = Field(description="Whether Cost Explorer marked the period as still estimated.")


class CostExplorerQuery(StrictBase):
    """A query this tool made and every line it got back, so each billed figure can be re-derived."""

    purpose: str = Field(description="Which cell or cells the query was made for.")
    request: dict[str, Any] = Field(description="The `GetCostAndUsage` request, verbatim.")
    lines: tuple[CostExplorerLine, ...] = Field(description="Every group of every period returned.")


class CostMeasurement(StrictBase):
    """`reports/costs/<date>-<label>.json`: one measurement session, dated and sourced."""

    schema_version: int = Field(default=1, description="Version of this file's shape.")
    measured_at: AwareDatetime = Field(description="UTC time the measurement was taken.")
    command: str = Field(description="The command line that produced this file.")
    cells: tuple[Cell, ...] = Field(description="Every cell this session measured or failed to.")
    queries: tuple[CostExplorerQuery, ...] = Field(
        default=(), description="Every Cost Explorer query made, with its full response lines."
    )
    notes: tuple[str, ...] = Field(default=(), description="Anything a reader of the figures should know.")


# ---------------------------------------------------------------------------
# The two tables' layout, as docs/AWS_DEPLOYMENT.md §6 prints them
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class IdleRow:
    """One row of the idle table, and the Cost Explorer lines that belong to it.

    `rules` are `(service pattern, usage-type pattern or None)`; a line belongs to the first row,
    in `IDLE_ROWS` order, with a matching rule. The order is therefore part of the meaning: RDS
    storage is matched before the RDS instance so the instance row takes what is left.
    """

    id: str
    label: str
    rules: tuple[tuple[str, str | None], ...]

    @property
    def option(self) -> str:
        """The name an operator gives the row on the command line: the id with hyphens."""
        return self.id.replace("_", "-")

    def claims(self, service: str, usage_type: str) -> bool:
        """Whether a line with this service and usage type belongs to this row."""
        return any(
            re.search(service_rule, service) is not None
            and (usage_rule is None or re.search(usage_rule, usage_type) is not None)
            for service_rule, usage_rule in self.rules
        )


_RDS: Final[str] = r"^Amazon Relational Database Service$"

IDLE_ROWS: Final[tuple[IdleRow, ...]] = (
    IdleRow("load_balancer", "Application Load Balancer", ((r"^Amazon Elastic Load Balancing$", None),)),
    IdleRow("fargate", "Fargate task at `api_min_tasks`", ((r"^Amazon Elastic Container Service$", None),)),
    IdleRow(
        "rds_storage",
        "RDS storage and automated backups",
        ((_RDS, r"Storage|Backup|Snapshot|IOPS|Throughput"),),
    ),
    IdleRow("rds_instance", "RDS for PostgreSQL instance", ((_RDS, None),)),
    IdleRow(
        "network",
        "NAT gateway or interface endpoints",
        ((r"^EC2 - Other$", r"NatGateway"), (r"^Amazon Virtual Private Cloud$", r"VpcEndpoint")),
    ),
    IdleRow("kms", "KMS customer-managed key", ((r"^AWS Key Management Service$", None),)),
    IdleRow("cloudwatch", "CloudWatch logs, metrics and alarms", ((r"CloudWatch", None),)),
    IdleRow("s3", "S3 storage and requests", ((r"^Amazon Simple Storage Service$", None),)),
    IdleRow(
        "secrets",
        "Secrets Manager and Parameter Store",
        ((r"^AWS Secrets Manager$", None), (r"^AWS Systems Manager$", None)),
    ),
)
"""The idle table's component rows, in matching order (not document order)."""

IDLE_TOTAL_ROW: Final[tuple[str, str]] = ("total", "**Total**")
"""The idle table's last row: every line Cost Explorer returned, claimed by a row or not."""


@dataclass(frozen=True, slots=True)
class RunRow:
    """One row of the per-run table and what a manifest must be to fill it.

    `expected_rows` is checked against the manifest's `dataset_fingerprint.n_rows`, because the row's
    label names a row count and a manifest of a different file would put a figure beside a claim it
    does not support. `per_rows` makes the row a normalised one.
    """

    id: str
    label: str
    entrypoint: JobEntrypoint
    strategy: Strategy | None = None
    expected_rows: int | None = None
    per_rows: int | None = None

    @property
    def option(self) -> str:
        """The name an operator gives the row on the command line: the id with hyphens."""
        return self.id.replace("_", "-")


REFERENCE_ROWS: Final[int] = 7_043
"""The reference file's row count, as the per-run table's labels print it."""

PER_RUN_ROWS: Final[tuple[RunRow, ...]] = (
    RunRow("train_template", "Train, template-sized file", JobEntrypoint.TRAIN),
    RunRow("train_reference", "Train, Telco Churn (7,043 rows)", JobEntrypoint.TRAIN, None, REFERENCE_ROWS),
    RunRow("score_reference", "Score, same file", JobEntrypoint.SCORE, None, REFERENCE_ROWS),
    RunRow(
        "train_fast",
        "Train, Telco Churn, strategy `fast`",
        JobEntrypoint.TRAIN,
        Strategy.FAST,
        REFERENCE_ROWS,
    ),
    RunRow(
        "train_balanced",
        "Train, Telco Churn, strategy `balanced`",
        JobEntrypoint.TRAIN,
        Strategy.BALANCED,
        REFERENCE_ROWS,
    ),
    RunRow(
        "train_exhaustive",
        "Train, Telco Churn, strategy `exhaustive`",
        JobEntrypoint.TRAIN,
        Strategy.EXHAUSTIVE,
        REFERENCE_ROWS,
    ),
    RunRow("score_per_100k", "Score, per 100,000 rows", JobEntrypoint.SCORE, None, None, 100_000),
)
"""The per-run table's run rows, in document order."""

ASSISTANT_ROW: Final[tuple[str, str]] = ("assistant_per_1000", "Assistant, per 1,000 questions (Bedrock)")
"""The per-run table's last row, filled from usage snapshots rather than a run manifest."""

PER_RUN_COLUMNS: Final[Mapping[str, str]] = {
    "billable_seconds": "Billable seconds",
    "instance_type": "Instance type",
    "list_price": "Estimated at list price",
    "billed": "Cost Explorer, same day",
}
"""Column id -> the per-run table's header text, verbatim."""


def idle_column(region: str, env: str) -> str:
    """The idle table's column id for one deployment; its header reads `<region>, <env>`."""
    return f"{region}/{env}"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _usd(amount: float) -> str:
    """A dollar amount as a cell prints it: cents from a dollar up, four places below one.

    Below a hundredth of a cent the places grow until the first significant digit shows, because a
    short scoring job can genuinely cost USD 0.00003 and printing that as `USD 0.0000` would read as
    free - the one figure this module exists to never write.
    """
    if abs(amount) >= 1:
        return f"USD {amount:,.2f}"
    places = 4
    while amount and round(abs(amount), places) == 0 and places < 12:
        places += 1
    return f"USD {amount:.{places}f}"


def _missing(table: TableId, row: str, column: str, *, reason: str, source: str) -> Cell:
    """A cell that stays `NOT YET MEASURED`, and why."""
    return Cell(
        table=table,
        row=row,
        column=column,
        status=CellStatus.NOT_MEASURED,
        text=MARKER,
        source=source,
        reason=reason,
    )


def _not_applicable(table: TableId, row: str, column: str, *, text: str, reason: str, source: str) -> Cell:
    """A cell with no figure by construction, which says so in the document."""
    return Cell(
        table=table,
        row=row,
        column=column,
        status=CellStatus.NOT_APPLICABLE,
        text=text,
        source=source,
        reason=reason,
    )


def _measured(
    table: TableId,
    row: str,
    column: str,
    *,
    kind: FigureKind,
    value: float | None,
    unit: str,
    text: str,
    source: str,
) -> Cell:
    """A cell holding a figure of one named kind."""
    return Cell(
        table=table,
        row=row,
        column=column,
        status=CellStatus.MEASURED,
        kind=kind,
        value=value,
        unit=unit,
        text=text,
        source=source,
    )


def _lag_refusal(end: date, today: date) -> str | None:
    """Why a window cannot be read yet, or None when Cost Explorer has had a full day to catch up.

    Cost Explorer lags by up to 24 hours (§6.1), so a window is read only once a whole UTC day has
    passed after it ended: a figure read too early is a partial bill that looks like a whole one.
    """
    if end < today:
        return None
    return (
        f"Cost Explorer lags by up to 24 hours, so a window ending {end.isoformat()} is read on or "
        f"after {(end + timedelta(days=1)).isoformat()}; reading it earlier would record a partial bill."
    )


def _query(
    client: CostExplorer,
    *,
    purpose: str,
    start: date,
    end: date,
    group_by: Sequence[str],
    expression: Mapping[str, Any] | None,
) -> CostExplorerQuery:
    """One `GetCostAndUsage` at DAILY granularity, every page, as lines. Only USD is accepted.

    Every request also leaves out `EXCLUDED_RECORD_TYPES`; `_and` flattens a nested `And`, since
    Cost Explorer takes one level of it happily and an `And` inside an `And` is noise in the record.
    """
    request: dict[str, Any] = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": "DAILY",
        "Metrics": [METRIC],
        "GroupBy": [{"Type": "DIMENSION", "Key": key} for key in group_by],
    }
    no_credits: Mapping[str, Any] = {
        "Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": list(EXCLUDED_RECORD_TYPES)}}
    }
    request["Filter"] = dict(_and([no_credits] if expression is None else [expression, no_credits]))
    lines: list[CostExplorerLine] = []
    token: str | None = None
    while True:
        page = client.get_cost_and_usage(**request, **({"NextPageToken": token} if token else {}))
        for period in page.get("ResultsByTime", []):
            window = period.get("TimePeriod", {})
            estimated = bool(period.get("Estimated", False))
            for group in period.get("Groups", []):
                metric = group.get("Metrics", {}).get(METRIC, {})
                unit = str(metric.get("Unit", ""))
                if unit != CURRENCY:
                    raise CostCaptureError(
                        "UNEXPECTED_CURRENCY",
                        f"Cost Explorer answered in {unit!r}, not {CURRENCY}; figures are never converted.",
                    )
                lines.append(
                    CostExplorerLine(
                        start=date.fromisoformat(str(window["Start"])),
                        end=date.fromisoformat(str(window["End"])),
                        keys=tuple(str(key) for key in group.get("Keys", [])),
                        amount=float(metric.get("Amount", "0")),
                        unit=unit,
                        estimated=estimated,
                    )
                )
        next_token = page.get("NextPageToken")
        token = str(next_token) if next_token else None
        if token is None:
            break
    return CostExplorerQuery(purpose=purpose, request=request, lines=tuple(lines))


def _and(expressions: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Cost Explorer's `And`, which refuses fewer than two operands, so one operand stands alone."""
    flat: list[Mapping[str, Any]] = []
    for expression in expressions:
        flat.extend(expression.get("And", [expression]))
    if len(flat) == 1:
        return flat[0]
    return {"And": flat}


def _estimated_note(query: CostExplorerQuery) -> str:
    """`; Cost Explorer marked it provisional` when any period was still an estimate, else nothing."""
    return (
        "; Cost Explorer still marked it provisional" if any(line.estimated for line in query.lines) else ""
    )


# ---------------------------------------------------------------------------
# Idle cost
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class IdleWindow:
    """The days an idle deployment was measured over; `end` is exclusive, as Cost Explorer's is."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise CostCaptureError(
                "EMPTY_WINDOW",
                f"The idle window {self.start}..{self.end} holds no day; its end is exclusive.",
            )

    @property
    def days(self) -> int:
        """Whole days in the window."""
        return (self.end - self.start).days

    @property
    def last_day(self) -> date:
        """The last day inside the window."""
        return self.end - timedelta(days=1)

    @property
    def is_calendar_month(self) -> bool:
        """Whether the window is exactly one calendar month, so its sum *is* a month's bill."""
        following = (self.start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return self.start.day == 1 and self.end == following

    def describe(self) -> str:
        """`2026-10-05 to 2026-10-11` - the window as a person reads it, both ends inclusive."""
        if self.days == 1:
            return self.start.isoformat()
        return f"{self.start.isoformat()} to {self.last_day.isoformat()}"


def idle_cells(
    client: CostExplorer | None,
    *,
    env: str,
    region: str,
    window: IdleWindow,
    tags: Mapping[str, str] | None,
    today: date,
    run_days: Mapping[str, frozenset[date]] | None = None,
    absent: Collection[str] = (),
) -> tuple[list[Cell], list[CostExplorerQuery], list[str]]:
    """Every cell of one column of the idle table, from Cost Explorer over `window`.

    `tags` is the cost-allocation filter (`product`, `env`, `client`); None means account-wide in the
    region, which §6.1 allows when the tags are not active and the account holds nothing else - the
    source line then says account-wide. `run_days` are the days of any run this session measured: a
    run inside the idle window means the window was not idle, and the whole column is refused.

    A row with no Cost Explorer line at all stays unmeasured: an absent line is not a zero, because
    an untagged resource (Fargate tasks whose service does not propagate its tags, say) is absent
    from a tag-filtered answer while it is being billed. `absent` names the rows the operator states
    this deployment does not have - a NAT gateway where interface endpoints are used, for instance;
    such a row reads "none in this deployment", and is refused if Cost Explorer contradicts it.
    """
    known = {row.id for row in IDLE_ROWS}
    stated_absent = frozenset(absent)
    if stated_absent - known:
        raise CostCaptureError(
            "UNKNOWN_IDLE_ROW",
            f"No idle row is called {', '.join(sorted(stated_absent - known))}; "
            f"rows: {', '.join(sorted(known))}.",
        )
    column = idle_column(region, env)
    scope = (
        "account-wide in " + region
        if tags is None
        else "tags " + ", ".join(f"{key}={tag}" for key, tag in sorted(tags.items())) + f", {region}"
    )
    source = (
        f"Cost Explorer GetCostAndUsage {METRIC}, {window.describe()} ({window.days} day(s)), "
        f"{scope}, grouped by SERVICE and USAGE_TYPE, {_EXCLUDED}"
    )
    row_ids = [row.id for row in IDLE_ROWS] + [IDLE_TOTAL_ROW[0]]

    def refuse_all(reason: str) -> list[Cell]:
        return [_missing(TableId.IDLE, row_id, column, reason=reason, source=source) for row_id in row_ids]

    busy = sorted(
        run_id
        for run_id, days in (run_days or {}).items()
        if any(window.start <= day < window.end for day in days)
    )
    if busy:
        return refuse_all(f"The idle window was not idle: run(s) {', '.join(busy)} ran inside it."), [], []
    if client is None:
        return refuse_all("Cost Explorer was not queried (--no-cost-explorer)."), [], []
    lag = _lag_refusal(window.end, today)
    if lag is not None:
        return refuse_all(lag), [], []

    expressions: list[Mapping[str, Any]] = [{"Dimensions": {"Key": "REGION", "Values": [region]}}]
    for key, tag_value in sorted((tags or {}).items()):
        expressions.append({"Tags": {"Key": key, "Values": [tag_value]}})
    query = _query(
        client,
        purpose=f"idle {column}",
        start=window.start,
        end=window.end,
        group_by=("SERVICE", "USAGE_TYPE"),
        expression=_and(expressions),
    )
    if not query.lines:
        reason = (
            "Cost Explorer returned no line at all for this window and filter. With the tag filter "
            "that almost always means the cost-allocation tags product, env and client are not "
            "active yet (§6.1) - an empty answer reads exactly like a free deployment, so it is "
            "not recorded as one."
        )
        return refuse_all(reason), [query], []

    by_row: dict[str, float] = {row.id: 0.0 for row in IDLE_ROWS}
    present: set[str] = set()
    unassigned: dict[str, float] = {}
    total = 0.0
    for line in query.lines:
        service = line.keys[0] if line.keys else ""
        usage_type = line.keys[1] if len(line.keys) > 1 else ""
        total += line.amount
        owner = next((row.id for row in IDLE_ROWS if row.claims(service, usage_type)), None)
        if owner is None:
            unassigned[service] = unassigned.get(service, 0.0) + line.amount
        else:
            by_row[owner] += line.amount
            present.add(owner)

    provisional = _estimated_note(query)
    full_source = source + provisional

    def monthly(amount: float) -> tuple[float, str]:
        if window.is_calendar_month:
            return amount, f"billed {window.describe()}"
        per_day = amount / window.days
        return (
            per_day * DAYS_PER_MONTH,
            f"{_usd(per_day)}/day billed {window.describe()}, x {DAYS_PER_MONTH:.2f} days/month",
        )

    cells: list[Cell] = []
    for row in IDLE_ROWS:
        if row.id in stated_absent:
            if row.id in present:
                reason = (
                    f"The operator stated the deployment has no {row.label}, but Cost Explorer has "
                    f"{_usd(by_row[row.id])} of it over {window.describe()}; one of the two is wrong."
                )
                cells.append(_missing(TableId.IDLE, row.id, column, reason=reason, source=full_source))
            else:
                cells.append(
                    _not_applicable(
                        TableId.IDLE,
                        row.id,
                        column,
                        text=f"none in this deployment (stated at measurement; no line {window.describe()})",
                        reason="The operator stated the deployment has none, and Cost Explorer has no line.",
                        source=full_source,
                    )
                )
            continue
        if row.id not in present:
            cells.append(
                _missing(
                    TableId.IDLE,
                    row.id,
                    column,
                    reason=(
                        f"Cost Explorer has no line for {row.label} under this filter. A missing line is "
                        "not a zero: the resource may carry no cost-allocation tag (an ECS service's "
                        "Fargate tasks carry the tags only when the service propagates them), the tag "
                        "may have been activated after the window, or the deployment has none - in "
                        f"that last case say so with --absent-idle-row {row.option}."
                    ),
                    source=full_source,
                )
            )
            continue
        value, how = monthly(by_row[row.id])
        text = f"{_usd(value)}: {how}"
        cells.append(
            _measured(
                TableId.IDLE,
                row.id,
                column,
                kind=FigureKind.BILLED,
                value=round(value, 4),
                unit="USD/month",
                text=text,
                source=full_source,
            )
        )
    total_value, total_how = monthly(total)
    total_text = f"{_usd(total_value)}: {total_how}"
    notes: list[str] = []
    if unassigned:
        unassigned_value, _ = monthly(sum(unassigned.values()))
        total_text += f"; includes {_usd(unassigned_value)} in no row above"
        notes.append(
            f"Idle {column}: lines no row claims, counted in the total only: "
            + ", ".join(
                f"{service} {_usd(amount)} over the window" for service, amount in sorted(unassigned.items())
            )
        )
    if not window.is_calendar_month:
        notes.append(
            f"Idle {column}: projected to a month from {window.days} billed day(s) at "
            f"{HOURS_PER_MONTH:.0f} hours per month, AWS's own convention."
        )
    cells.append(
        _measured(
            TableId.IDLE,
            IDLE_TOTAL_ROW[0],
            column,
            kind=FigureKind.BILLED,
            value=round(total_value, 4),
            unit="USD/month",
            text=total_text,
            source=full_source,
        )
    )
    return cells, [query], notes


# ---------------------------------------------------------------------------
# Per run
# ---------------------------------------------------------------------------
class Attribution(StrEnum):
    """How a run's billed cost is told apart from everything else billed the same day."""

    TAG = "tag"
    DAY = "day"


def run_days(manifest: RunManifest) -> frozenset[date]:
    """The UTC days a run occupied: from `created_at - duration_s` to `created_at`, both inclusive."""
    finished = manifest.created_at.astimezone(UTC)
    started = finished - timedelta(seconds=manifest.duration_s)
    days: set[date] = set()
    day = started.date()
    while day <= finished.date():
        days.add(day)
        day += timedelta(days=1)
    return frozenset(days)


def _manifest_problem(row: RunRow, manifest: RunManifest) -> str | None:
    """Why this manifest cannot fill this row, or None when it can."""
    compute = manifest.compute
    if compute is None or compute.backend != ComputeBackend.SAGEMAKER:
        return (
            f"Run {manifest.run_id} did not run as a SageMaker job, so AWS billed nothing for it; "
            "this table is about the deployment."
        )
    if compute.entrypoint != row.entrypoint:
        return (
            f"Run {manifest.run_id} is a {compute.entrypoint or 'unknown'} job; the row "
            f"{row.label!r} needs a {row.entrypoint.value} job."
        )
    if row.strategy is not None:
        strategy = manifest.recipe.model_search.strategy if manifest.recipe is not None else None
        if strategy != row.strategy:
            return (
                f"Run {manifest.run_id} trained with strategy {strategy or 'none recorded'}; the row "
                f"{row.label!r} needs {row.strategy.value}."
            )
    rows = manifest.dataset_fingerprint.n_rows
    if row.expected_rows is not None and rows != row.expected_rows:
        return f"Run {manifest.run_id} consumed {rows:,} rows; the row {row.label!r} names {row.expected_rows:,}."
    if row.per_rows is not None and rows < row.per_rows:
        return (
            f"Run {manifest.run_id} scored {rows:,} rows. Scaling it up to {row.per_rows:,} would "
            "multiply the job's fixed start-up cost along with it; score a file of at least "
            f"{row.per_rows:,} rows (`python -m scripts.measure_costs scoring-file`)."
        )
    return None


_OFFER: Final[re.Pattern[str]] = re.compile(r"offer version ([^,)\s]+)")


def run_cells(
    row: RunRow,
    manifest: RunManifest,
    *,
    client: CostExplorer | None,
    attribution: Attribution,
    today: date,
    other_runs: Mapping[str, frozenset[date]] | None = None,
) -> tuple[list[Cell], list[CostExplorerQuery]]:
    """The four cells of one per-run row, from the run's manifest and, for the bill, Cost Explorer.

    `other_runs` are the days of every *other* run measured in this session, keyed by run id: day
    attribution is refused when one of them shares a day with this run, because the day's SageMaker
    line would then be two runs' cost printed as one.
    """
    table = TableId.PER_RUN
    source = f"run_manifest.json of run {manifest.run_id}"
    problem = _manifest_problem(row, manifest)
    if problem is not None:
        return [
            _missing(table, row.id, column, reason=problem, source=source) for column in PER_RUN_COLUMNS
        ], []
    compute = manifest.compute
    assert compute is not None  # _manifest_problem refused a manifest without one
    rows = manifest.dataset_fingerprint.n_rows
    scale = (row.per_rows / rows) if row.per_rows is not None else 1.0
    scaled = f", x {scale:.4g} from {rows:,} rows" if row.per_rows is not None else ""
    cells: list[Cell] = []

    # Billable seconds.
    if compute.billable_seconds is not None:
        seconds = compute.billable_seconds * scale
        cells.append(
            _measured(
                table,
                row.id,
                "billable_seconds",
                kind=FigureKind.REPORTED,
                value=round(seconds, 1),
                unit="seconds",
                text=f"{seconds:,.0f} ({compute.billable_seconds_source or 'reported'}{scaled})",
                source=f"{source}: compute.billable_seconds, from {compute.billable_seconds_source}",
            )
        )
    elif compute.entrypoint == JobEntrypoint.SCORE:
        cells.append(
            _not_applicable(
                table,
                row.id,
                "billable_seconds",
                text="n/a: a processing job reports no billable time (DEC-332)",
                reason="SageMaker processing jobs report start and end times, which are wall clock, not billed time.",
                source=source,
            )
        )
    else:
        cells.append(
            _missing(
                table,
                row.id,
                "billable_seconds",
                reason=f"SageMaker reported no billable time for training job {compute.job_name}.",
                source=source,
            )
        )

    # Instance type.
    if compute.instance_type:
        cells.append(
            _measured(
                table,
                row.id,
                "instance_type",
                kind=FigureKind.REPORTED,
                value=None,
                unit="instance type",
                text=f"{compute.instance_type} x {compute.instance_count or 1}",
                source=f"{source}: compute.instance_type, compute.instance_count",
            )
        )
    else:
        cells.append(
            _missing(
                table, row.id, "instance_type", reason="The manifest names no instance type.", source=source
            )
        )

    # Estimated at list price.
    estimate = manifest.cost_estimate
    if estimate.estimated_usd is not None:
        value = estimate.estimated_usd * scale
        offer = _OFFER.search(estimate.basis)
        cells.append(
            _measured(
                table,
                row.id,
                "list_price",
                kind=FigureKind.LIST_PRICE_ESTIMATE,
                value=round(value, 6),
                unit="USD",
                text=f"{_usd(value)} at list price, offer version {offer.group(1) if offer else 'unrecorded'}{scaled}",
                source=f"{source}: cost_estimate.estimated_usd; basis: {estimate.basis}",
            )
        )
    elif compute.entrypoint == JobEntrypoint.SCORE and compute.billable_seconds is None:
        cells.append(
            _not_applicable(
                table,
                row.id,
                "list_price",
                text="n/a: no billable time to price (DEC-332)",
                reason=estimate.basis,
                source=source,
            )
        )
    else:
        cells.append(_missing(table, row.id, "list_price", reason=estimate.basis, source=source))

    # Cost Explorer.
    billed, queries = _billed_run_cell(
        row,
        manifest,
        client=client,
        attribution=attribution,
        today=today,
        other_runs=other_runs or {},
        scale=scale,
        scaled=scaled,
    )
    cells.append(billed)
    return cells, queries


def _billed_run_cell(
    row: RunRow,
    manifest: RunManifest,
    *,
    client: CostExplorer | None,
    attribution: Attribution,
    today: date,
    other_runs: Mapping[str, frozenset[date]],
    scale: float,
    scaled: str,
) -> tuple[Cell, list[CostExplorerQuery]]:
    """The per-run row's billed cell: the run's own tagged cost, or its day's whole SageMaker line."""
    table = TableId.PER_RUN
    compute = manifest.compute
    assert compute is not None
    days = run_days(manifest)
    start, end = min(days), max(days) + timedelta(days=1)
    region = compute.region or ""
    span = start.isoformat() if len(days) == 1 else f"{start.isoformat()} to {max(days).isoformat()}"
    words = (
        f"tag run_id={manifest.run_id}"
        if attribution == Attribution.TAG
        else "SageMaker line for the whole day"
    )
    source = f"Cost Explorer GetCostAndUsage {METRIC}, {span}, {region}, {words}, {_EXCLUDED}"
    if client is None:
        return _missing(table, row.id, "billed", reason="Cost Explorer was not queried.", source=source), []
    if not region:
        return _missing(table, row.id, "billed", reason="The manifest names no region.", source=source), []
    lag = _lag_refusal(end, today)
    if lag is not None:
        return _missing(table, row.id, "billed", reason=lag, source=source), []
    region_filter: Mapping[str, Any] = {"Dimensions": {"Key": "REGION", "Values": [region]}}
    if attribution == Attribution.TAG:
        expression = _and([region_filter, {"Tags": {"Key": "run_id", "Values": [manifest.run_id]}}])
        group_by: tuple[str, ...] = ("SERVICE",)
    else:
        sharing = sorted(
            run_id for run_id, other in other_runs.items() if run_id != manifest.run_id and other & days
        )
        if sharing:
            reason = (
                f"Run(s) {', '.join(sharing)} ran on the same day, so the day's SageMaker line is not "
                f"this run's alone. Use --attribution tag, or run one job per day."
            )
            return _missing(table, row.id, "billed", reason=reason, source=source), []
        expression = _and([region_filter, {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon SageMaker"]}}])
        group_by = ("USAGE_TYPE",)
    query = _query(
        client,
        purpose=f"per-run {row.id} ({manifest.run_id})",
        start=start,
        end=end,
        group_by=group_by,
        expression=expression,
    )
    if not query.lines:
        if attribution == Attribution.TAG:
            reason = (
                f"Cost Explorer has no line tagged run_id={manifest.run_id}. A tag counts only for usage "
                "after it was activated as a cost-allocation tag: activate run_id (Billing and Cost "
                "Management) before the measured runs, or measure with --attribution day."
            )
        else:
            reason = f"Cost Explorer has no Amazon SageMaker line in {region} for {span}."
        return _missing(table, row.id, "billed", reason=reason, source=source), [query]
    value = sum(line.amount for line in query.lines) * scale
    return (
        _measured(
            table,
            row.id,
            "billed",
            kind=FigureKind.BILLED,
            value=round(value, 6),
            unit="USD",
            text=f"{_usd(value)} billed {span}, {words}{scaled}",
            source=source + _estimated_note(query),
        ),
        [query],
    )


# ---------------------------------------------------------------------------
# The assistant
# ---------------------------------------------------------------------------
def _delta(after: LlmUsageReport, before: LlmUsageReport) -> dict[str, tuple[int, int, int]]:
    """Model id -> (calls, input tokens, output tokens) spent between the two snapshots."""
    earlier = {usage.model_id: usage for usage in before.by_model}
    out: dict[str, tuple[int, int, int]] = {}
    for usage in after.by_model:
        prior = earlier.get(usage.model_id)
        calls = usage.calls - (prior.calls if prior else 0)
        tokens_in = usage.input_tokens - (prior.input_tokens if prior else 0)
        tokens_out = usage.output_tokens - (prior.output_tokens if prior else 0)
        if calls or tokens_in or tokens_out:
            out[usage.model_id] = (calls, tokens_in, tokens_out)
    return out


def _answer_calls(report: LlmUsageReport) -> int:
    """Answering calls a usage report counts, cache hits excluded."""
    return sum(
        usage.calls for usage in report.by_purpose if usage.purpose == GenerativePurpose.ASSISTANT_ANSWER
    )


def _assistant_problem(
    before: LlmUsageReport, after: LlmUsageReport, questions: int, day: date
) -> str | None:
    """Why these two snapshots cannot be read as `questions` questions asked on `day`, or None."""
    if questions <= 0:
        return "The number of questions asked must be positive."
    if before.job_id != after.job_id:
        return f"The two usage snapshots are of different indexes ({before.job_id}, {after.job_id})."
    if after.created_at <= before.created_at:
        return "The 'after' usage snapshot is not later than the 'before' one."
    if after.created_at.astimezone(UTC).date() != day:
        return (
            f"The 'after' snapshot was last written on {after.created_at.astimezone(UTC).date()}, not on "
            f"{day}, the day whose Bedrock bill is read."
        )
    delta = _delta(after, before)
    if any(calls < 0 or tokens_in < 0 or tokens_out < 0 for calls, tokens_in, tokens_out in delta.values()):
        return "A counter went down between the snapshots, so they are not before and after of one index."
    answers = _answer_calls(after) - _answer_calls(before)
    if answers <= 0:
        return "No answering call was made between the two snapshots."
    if answers > questions:
        return (
            f"{answers} answering calls were made between the snapshots but {questions} questions are "
            "claimed; something else asked this index questions in between."
        )
    return None


def assistant_cells(
    before: LlmUsageReport,
    after: LlmUsageReport,
    *,
    questions: int,
    day: date,
    prices: LlmPriceTable | None,
    client: CostExplorer | None,
    today: date,
) -> tuple[list[Cell], list[CostExplorerQuery]]:
    """The assistant row: tokens and models from two usage snapshots; list price; the day's Bedrock bill.

    The row's "billable seconds" and "instance type" cells hold the token counts and the model ids,
    as §6.2 says they do for this row. Everything is normalised to `QUESTIONS_PER_UNIT` questions.
    """
    table = TableId.PER_RUN
    row = ASSISTANT_ROW[0]
    source = (
        f"llm_usage.json of index {after.job_id}, before and after {questions} questions on {day.isoformat()}"
    )
    problem = _assistant_problem(before, after, questions, day)
    if problem is not None:
        return [_missing(table, row, column, reason=problem, source=source) for column in PER_RUN_COLUMNS], []
    scale = QUESTIONS_PER_UNIT / questions
    per = f"x {scale:.4g} from {questions:,} questions"
    delta = _delta(after, before)
    tokens_in = sum(entry[1] for entry in delta.values())
    tokens_out = sum(entry[2] for entry in delta.values())
    cells = [
        _measured(
            table,
            row,
            "billable_seconds",
            kind=FigureKind.REPORTED,
            value=round((tokens_in + tokens_out) * scale, 1),
            unit="tokens",
            text=f"{tokens_in * scale:,.0f} input + {tokens_out * scale:,.0f} output tokens ({per})",
            source=f"{source}: by_model tokens, after minus before",
        ),
        _measured(
            table,
            row,
            "instance_type",
            kind=FigureKind.REPORTED,
            value=None,
            unit="model id",
            text=", ".join(sorted(delta)),
            source=f"{source}: by_model model ids with calls in between",
        ),
    ]

    unpriced = sorted(model for model in delta if prices is None or prices.get(model) is None)
    if prices is None or unpriced or not prices.as_of:
        reason = (
            "configs/llm_prices.yaml has no dated price for "
            + (", ".join(unpriced) if unpriced else "these models")
            + "; fill it from the provider's price list (with as_of and source) and measure again."
        )
        cells.append(_missing(table, row, "list_price", reason=reason, source=source))
    else:
        cost = 0.0
        for model, (_, model_in, model_out) in delta.items():
            price = prices.get(model)
            assert price is not None
            cost += price.cost(model_in, model_out)
        value = cost * scale
        cells.append(
            _measured(
                table,
                row,
                "list_price",
                kind=FigureKind.LIST_PRICE_ESTIMATE,
                value=round(value, 6),
                unit="USD",
                text=f"{_usd(value)} at list price, configs/llm_prices.yaml as of {prices.as_of} ({per})",
                source=f"{source}; tokens x configs/llm_prices.yaml (as_of {prices.as_of}, source {prices.source})",
            )
        )

    billed_source = (
        f"Cost Explorer GetCostAndUsage {METRIC}, {day.isoformat()}, account-wide, every Bedrock service, "
        f"{_EXCLUDED}"
    )
    if client is None:
        cells.append(
            _missing(table, row, "billed", reason="Cost Explorer was not queried.", source=billed_source)
        )
        return cells, []
    end = day + timedelta(days=1)
    lag = _lag_refusal(end, today)
    if lag is not None:
        cells.append(_missing(table, row, "billed", reason=lag, source=billed_source))
        return cells, []
    query = _query(client, purpose="assistant", start=day, end=end, group_by=("SERVICE",), expression=None)
    bedrock = [line for line in query.lines if "Bedrock" in (line.keys[0] if line.keys else "")]
    if not bedrock:
        reason = f"Cost Explorer has no Bedrock line for {day.isoformat()}."
        cells.append(_missing(table, row, "billed", reason=reason, source=billed_source))
        return cells, [query]
    value = sum(line.amount for line in bedrock) * scale
    cells.append(
        _measured(
            table,
            row,
            "billed",
            kind=FigureKind.BILLED,
            value=round(value, 6),
            unit="USD",
            text=f"{_usd(value)} billed {day.isoformat()}, every Bedrock line that day, account-wide ({per})",
            source=billed_source + _estimated_note(query),
        )
    )
    return cells, [query]


# ---------------------------------------------------------------------------
# Merging sessions and writing the document
# ---------------------------------------------------------------------------
def _unit(cell: Cell) -> tuple[TableId, str]:
    """What is merged as one piece: an idle *column*, or a per-run *row*."""
    return (cell.table, cell.column) if cell.table == TableId.IDLE else (cell.table, cell.row)


def merge_cells(measurements: Sequence[CostMeasurement]) -> dict[tuple[TableId, str, str], Cell]:
    """Every cell across several sessions, merged a whole idle column or a whole per-run row at a time.

    Sessions are merged because the tables are measured over days - an idle day, a run per strategy
    on its own day, the assistant's day, and a prod column that needs a prod deployment - and the
    document is written once, from all of them.

    The unit is the column or the row, never the single cell, because cells of one unit only mean
    something together: an idle column's Total is the sum of *its* window and filter, and a per-run
    row describes *one* run. Merged cell by cell, a Fargate figure read account-wide could sit above
    a Total read under a tag filter that missed Fargate, or one run's billable seconds beside another
    run's bill - every cell sourced, and the table wrong. So a unit is taken whole from one session:
    the latest session in which every cell of it has a figure, or, when no session has it complete,
    the latest session that has it at all (its gaps then keep the table from being written).
    """
    chosen: dict[tuple[TableId, str], tuple[bool, datetime, list[Cell]]] = {}
    for measurement in measurements:
        units: dict[tuple[TableId, str], list[Cell]] = {}
        for cell in measurement.cells:
            units.setdefault(_unit(cell), []).append(cell)
        for unit, cells in units.items():
            complete = all(cell.status != CellStatus.NOT_MEASURED for cell in cells)
            held = chosen.get(unit)
            if held is None or (complete, measurement.measured_at) >= (held[0], held[1]):
                chosen[unit] = (complete, measurement.measured_at, cells)
    return {(cell.table, cell.row, cell.column): cell for _, _, cells in chosen.values() for cell in cells}


class TableState(StrEnum):
    """What a §6 table holds: all markers, no marker at all, or a mixture - which is never allowed."""

    UNMEASURED = "unmeasured"
    MEASURED = "measured"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class TableOutcome:
    """What `render_document` did with one table, and, when it did nothing, every reason why."""

    table: TableId
    heading: str
    complete: bool
    written: bool
    missing: tuple[str, ...] = field(default=())


_ROW: Final[re.Pattern[str]] = re.compile(r"^\s*\|(.+)\|\s*$")


def _cells_of(line: str) -> list[str]:
    """The trimmed cells of a markdown table row; empty when the line is not one."""
    match = _ROW.match(line)
    return [] if match is None else [cell.strip() for cell in match.group(1).split("|")]


def _table_span(lines: Sequence[str], heading: str) -> tuple[int, int, int] | None:
    """(header line, first data line, end) of the first table after `heading`; None when absent."""
    start = next((index for index, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        return None
    header = next((index for index in range(start + 1, len(lines)) if _cells_of(lines[index])), None)
    if header is None or header + 1 >= len(lines):
        return None
    end = header + 2
    while end < len(lines) and _cells_of(lines[end]):
        end += 1
    return header, header + 2, end


def table_state(text: str, heading: str) -> TableState:
    """Whether the table under `heading` is all markers, has no marker, or mixes the two.

    The check that replaces `test_the_cost_tables_are_entirely_unmeasured` on the day the tables are
    filled asserts this is never `PARTIAL` (docs/M50_CHECKLIST.md §8).
    """
    lines = text.splitlines()
    span = _table_span(lines, heading)
    if span is None:
        raise CostCaptureError("TABLE_NOT_FOUND", f"{heading!r} has no table under it.")
    _, first, end = span
    values = [cell for line in lines[first:end] for cell in _cells_of(line)[1:]]
    markers = sum(1 for cell in values if cell == MARKER)
    if markers == len(values):
        return TableState.UNMEASURED
    return TableState.MEASURED if markers == 0 else TableState.PARTIAL


def _idle_column_from_header(header: str) -> str | None:
    """`ap-south-1, dev` -> `ap-south-1/dev`; None for a header of any other shape."""
    parts = [part.strip() for part in header.split(",")]
    return idle_column(parts[0], parts[1]) if len(parts) == 2 and all(parts) else None


def _layout(table: TableId) -> tuple[Mapping[str, str], Any]:
    """(row label -> row id, header -> column id function) for one table."""
    if table == TableId.IDLE:
        rows = {row.label: row.id for row in IDLE_ROWS} | {IDLE_TOTAL_ROW[1]: IDLE_TOTAL_ROW[0]}
        return rows, _idle_column_from_header
    by_header = {header: column for column, header in PER_RUN_COLUMNS.items()}
    rows = {row.label: row.id for row in PER_RUN_ROWS} | {ASSISTANT_ROW[1]: ASSISTANT_ROW[0]}
    return rows, by_header.get


def _cell_text(text: str) -> str:
    """A cell's text made safe for a markdown table: a pipe would split the cell in two."""
    return text.replace("|", "/").replace("\n", " ")


def render_document(
    text: str, cells: Mapping[tuple[TableId, str, str], Cell]
) -> tuple[str, tuple[TableOutcome, ...]]:
    """`text` with each §6 table replaced *only* when every one of its cells has a figure.

    A table with even one cell still `NOT YET MEASURED` - including a cell this tool does not know,
    because somebody added a row or a column - is left exactly as it was, and its outcome lists every
    missing cell and why. The document is never partly filled.
    """
    lines = text.splitlines(keepends=True)
    outcomes: list[TableOutcome] = []
    for table, heading in ((TableId.IDLE, IDLE_HEADING), (TableId.PER_RUN, PER_RUN_HEADING)):
        span = _table_span([line.rstrip("\n") for line in lines], heading)
        if span is None:
            outcomes.append(
                TableOutcome(table, heading, False, False, (f"{heading!r} has no table under it.",))
            )
            continue
        header_at, first, end = span
        rows, column_of = _layout(table)
        headers = _cells_of(lines[header_at])[1:]
        columns = [column_of(header) for header in headers]
        missing: list[str] = []
        replacement: list[str] = []
        for index in range(first, end):
            data = _cells_of(lines[index])
            label = data[0] if data else ""
            row_id = rows.get(label)
            texts: list[str] = []
            for header, column in zip(headers, columns, strict=True):
                cell = cells.get((table, row_id, column)) if row_id and column else None
                if cell is None or cell.status == CellStatus.NOT_MEASURED:
                    why = (
                        cell.reason
                        if cell is not None
                        else (
                            "this tool does not measure that cell"
                            if not (row_id and column)
                            else "not measured"
                        )
                    )
                    missing.append(f"{label} / {header}: {why}")
                    continue
                texts.append(_cell_text(cell.text))
            if not missing:
                newline = "\n" if lines[index].endswith("\n") else ""
                replacement.append("| " + " | ".join([label, *texts]) + " |" + newline)
        complete = not missing
        if complete:
            lines[first:end] = replacement
        outcomes.append(TableOutcome(table, heading, complete, complete, tuple(missing)))
    return "".join(lines), tuple(outcomes)
