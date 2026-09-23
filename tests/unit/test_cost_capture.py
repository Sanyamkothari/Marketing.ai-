"""`engine/aws/cost_capture.py`: every cell of guide §6 is a sourced figure or a stated gap, never a guess.

Cost Explorer is a stub here, and on purpose: moto's `ce` support is thin, and what is under test is
not AWS but this module's reading of it - which query it makes for which cell, how each line is
attributed, and above all when it refuses. The amounts the stub returns are test inputs, not claims
about what anything costs; no assertion below states a price for a real service, and nothing here
reaches a document.

The refusals get a test each, because each is a different sentence an operator acts on: tags not
active, Cost Explorer not caught up, the idle day not idle, a manifest of the wrong strategy or the
wrong file, a scoring file too small to scale, a model with no price. The last group proves the
rule the whole module exists for: a table is written only when every one of its cells has a figure.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from engine.aws.cost_capture import (
    ASSISTANT_ROW,
    DAYS_PER_MONTH,
    IDLE_HEADING,
    IDLE_ROWS,
    IDLE_TOTAL_ROW,
    MARKER,
    PER_RUN_COLUMNS,
    PER_RUN_HEADING,
    PER_RUN_ROWS,
    Attribution,
    Cell,
    CellStatus,
    CostCaptureError,
    CostMeasurement,
    FigureKind,
    IdleWindow,
    RunRow,
    TableId,
    TableState,
    assistant_cells,
    idle_cells,
    idle_column,
    merge_cells,
    render_document,
    run_cells,
    run_days,
    table_state,
)
from engine.aws.prices import cost_estimate, load_price_table
from engine.config import Strategy, load_use_case, recipe_from_config
from engine.contracts import (
    ComputeBackend,
    ComputeInfo,
    CostEstimate,
    DatasetFingerprint,
    JobEntrypoint,
    LLMUsage,
    RunManifest,
)
from engine.generative.budget import ModelPrice, PriceTable
from engine.generative.contracts import GenerativePurpose, LlmUsageReport, ModelUsage, PurposeUsage
from tests.unit.test_docs_honesty import CITATION, CURRENCY

REGION = "ap-south-1"
TODAY = date(2026, 10, 20)
"""Far enough after every fixture day that Cost Explorer's lag never refuses unless a test wants it."""

RUN_DAY = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
TRAIN_INSTANCE = "ml.m5.xlarge"
Responder = Callable[[Mapping[str, Any]], Sequence[tuple[tuple[str, ...], float]]]


# ---------------------------------------------------------------------------
# A stubbed Cost Explorer and fixture documents
# ---------------------------------------------------------------------------
class StubCostExplorer:
    """`GetCostAndUsage` answered by `responder`: request -> [(group keys, amount)]; requests are kept."""

    def __init__(self, responder: Responder, *, unit: str = "USD", pages: int = 1, estimated: bool = False):
        self.responder = responder
        self.unit = unit
        self.pages = pages
        self.estimated = estimated
        self.requests: list[dict[str, Any]] = []

    def get_cost_and_usage(self, **kwargs: Any) -> Mapping[str, Any]:
        self.requests.append(kwargs)
        page = int(kwargs.get("NextPageToken", "0"))
        groups = [
            {"Keys": list(keys), "Metrics": {"UnblendedCost": {"Amount": str(amount), "Unit": self.unit}}}
            for keys, amount in self.responder(kwargs)
        ]
        response: dict[str, Any] = {
            "ResultsByTime": [
                {
                    "TimePeriod": dict(kwargs["TimePeriod"]),
                    "Groups": groups if page == 0 else [],
                    "Estimated": self.estimated,
                }
            ]
        }
        if page + 1 < self.pages:
            response["NextPageToken"] = str(page + 1)
        return response


def filters_of(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The operands of a request's filter, whether it is an `And` or a single expression."""
    expression = request.get("Filter")
    if expression is None:
        return []
    return list(expression["And"]) if "And" in expression else [expression]


def tag_value(request: Mapping[str, Any], key: str) -> str | None:
    """The value a request filters tag `key` to, or None."""
    for operand in filters_of(request):
        if "Tags" in operand and operand["Tags"]["Key"] == key:
            return str(operand["Tags"]["Values"][0])
    return None


def recipe(strategy: Strategy) -> Any:
    """A real recipe for the reference use case, with its search strategy set."""
    base = recipe_from_config(
        load_use_case("telco_churn"), primary_key="customerID", feature_columns=("tenure",), seed=1
    )
    return base.model_copy(
        update={"model_search": base.model_search.model_copy(update={"strategy": strategy})}
    )


def manifest(
    run_id: str,
    *,
    entrypoint: JobEntrypoint = JobEntrypoint.TRAIN,
    strategy: Strategy | None = Strategy.BALANCED,
    rows: int = 7_043,
    billable: float | None = 600.0,
    finished: datetime = RUN_DAY,
    duration: float = 900.0,
    backend: ComputeBackend = ComputeBackend.SAGEMAKER,
    instance_type: str = TRAIN_INSTANCE,
) -> RunManifest:
    """A finished run's manifest, its estimate computed by the real `prices.cost_estimate`."""
    compute = ComputeInfo(
        backend=backend,
        entrypoint=entrypoint if backend == ComputeBackend.SAGEMAKER else None,
        job_name=f"marketing-ai-{entrypoint.value}-{run_id}",
        instance_type=instance_type,
        instance_count=1,
        region=REGION,
        duration_s=duration,
        billable_seconds=billable,
        billable_seconds_source="DescribeTrainingJob.BillableTimeInSeconds" if billable is not None else None,
    )
    table = load_price_table(Path(__file__).resolve().parents[2] / "configs" / "aws_prices.yaml")
    estimate: CostEstimate = cost_estimate(compute, table=table)
    return RunManifest(
        run_id=run_id,
        primary_key="customerID",
        recipe=recipe(strategy) if (strategy is not None and entrypoint == JobEntrypoint.TRAIN) else None,
        dataset_fingerprint=DatasetFingerprint(hash="0" * 16, algorithm="sha256", n_rows=rows, columns=()),
        seed=1,
        duration_s=duration,
        cost_estimate=estimate,
        compute=compute,
        created_at=finished,
    )


def usage(
    *,
    answer_calls: int,
    tokens_in: int,
    tokens_out: int,
    at: datetime,
    model: str = "some.generation-model-v1:0",
    embed_calls: int = 0,
    embed_tokens: int = 0,
    job_id: str = "idx-1",
) -> LlmUsageReport:
    """An index's `llm_usage.json` with one generation model and, optionally, one embedding model."""
    by_model = [
        ModelUsage(model_id=model, calls=answer_calls, input_tokens=tokens_in, output_tokens=tokens_out)
    ]
    if embed_calls:
        by_model.append(
            ModelUsage(
                model_id="some.embedding-model-v2:0",
                calls=embed_calls,
                input_tokens=embed_tokens,
                output_tokens=0,
            )
        )
    return LlmUsageReport(
        job_id=job_id,
        totals=LLMUsage(
            calls=answer_calls + embed_calls,
            input_tokens=tokens_in + embed_tokens,
            output_tokens=tokens_out,
            model_ids=tuple(sorted(item.model_id for item in by_model)),
        ),
        cache_hits=0,
        budget_calls=10_000,
        by_model=tuple(by_model),
        by_purpose=(
            PurposeUsage(
                purpose=GenerativePurpose.ASSISTANT_ANSWER,
                calls=answer_calls,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
            ),
        ),
        created_at=at,
    )


IDLE_LINES: tuple[tuple[tuple[str, ...], float], ...] = (
    (("Amazon Elastic Load Balancing", "APS3-LoadBalancerUsage"), 0.5),
    (("Amazon Elastic Container Service", "APS3-Fargate-vCPU-Hours:perCPU"), 0.25),
    (("Amazon Relational Database Service", "APS3-InstanceUsage:db.t4g.micro"), 0.4),
    (("Amazon Relational Database Service", "APS3-RDS:GP3-Storage"), 0.1),
    (("EC2 - Other", "APS3-NatGateway-Hours"), 1.0),
    (("AWS Key Management Service", "ap-south-1-KMS-Keys"), 0.03),
    (("AmazonCloudWatch", "APS3-TimedStorage-ByteHrs"), 0.02),
    (("Amazon Simple Storage Service", "APS3-TimedStorage-ByteHrs"), 0.01),
    (("AWS Secrets Manager", "APS3-AWSSecretsManager-Secrets"), 0.013),
    (("Amazon EC2 Container Registry (ECR)", "APS3-TimedStorage-ByteHrs"), 0.2),
)
"""One idle day's lines. ECR is claimed by no row, so it must reach the total and be named there."""


def idle_stub() -> StubCostExplorer:
    return StubCostExplorer(lambda request: IDLE_LINES)


# ---------------------------------------------------------------------------
# Idle cost
# ---------------------------------------------------------------------------
def test_idle_cost_filters_on_the_deployment_tags_and_groups_by_service_and_usage_type() -> None:
    client = idle_stub()
    idle_cells(
        client,
        env="dev",
        region=REGION,
        window=IdleWindow(date(2026, 10, 5), date(2026, 10, 6)),
        tags={"product": "marketing-ai", "env": "dev", "client": "acme"},
        today=TODAY,
    )
    (request,) = client.requests
    assert request["Metrics"] == ["UnblendedCost"]
    assert request["TimePeriod"] == {"Start": "2026-10-05", "End": "2026-10-06"}
    assert [group["Key"] for group in request["GroupBy"]] == ["SERVICE", "USAGE_TYPE"]
    assert {"Dimensions": {"Key": "REGION", "Values": [REGION]}} in filters_of(request)
    assert (tag_value(request, "product"), tag_value(request, "env"), tag_value(request, "client")) == (
        "marketing-ai",
        "dev",
        "acme",
    )


def test_every_idle_line_lands_in_one_row_and_the_total_counts_the_unclaimed_ones() -> None:
    cells, queries, notes = idle_cells(
        idle_stub(),
        env="dev",
        region=REGION,
        window=IdleWindow(date(2026, 10, 5), date(2026, 10, 6)),
        tags={"product": "marketing-ai", "env": "dev"},
        today=TODAY,
    )
    by_row = {cell.row: cell for cell in cells}
    assert set(by_row) == {row.id for row in IDLE_ROWS} | {IDLE_TOTAL_ROW[0]}
    assert all(cell.status == CellStatus.MEASURED and cell.kind == FigureKind.BILLED for cell in cells)
    assert all(cell.column == idle_column(REGION, "dev") for cell in cells)
    # One day, projected at the stated factor; the RDS lines split into instance and storage.
    assert by_row["rds_instance"].value == pytest.approx(0.4 * DAYS_PER_MONTH, abs=1e-3)
    assert by_row["rds_storage"].value == pytest.approx(0.1 * DAYS_PER_MONTH, abs=1e-3)
    assert by_row["network"].value == pytest.approx(1.0 * DAYS_PER_MONTH, abs=1e-3)
    total = sum(amount for _, amount in IDLE_LINES)
    assert by_row["total"].value == pytest.approx(total * DAYS_PER_MONTH, abs=1e-3)
    assert "in no row above" in by_row["total"].text
    assert any("Amazon EC2 Container Registry (ECR)" in note for note in notes)
    assert "x 30.42 days/month" in by_row["load_balancer"].text
    assert len(queries) == 1 and len(queries[0].lines) == len(IDLE_LINES)


def test_a_whole_calendar_month_is_recorded_as_billed_not_projected() -> None:
    cells, _, notes = idle_cells(
        idle_stub(),
        env="dev",
        region=REGION,
        window=IdleWindow(date(2026, 9, 1), date(2026, 10, 1)),
        tags={"product": "marketing-ai", "env": "dev"},
        today=TODAY,
    )
    alb = next(cell for cell in cells if cell.row == "load_balancer")
    assert alb.value == pytest.approx(0.5)
    assert alb.text.startswith("USD 0.5000: billed 2026-09-01 to 2026-09-30")
    assert not any("projected" in note for note in notes)


def test_a_row_with_no_line_is_not_a_zero_and_stays_unmeasured() -> None:
    """An untagged resource (Fargate tasks whose service propagates no tags) is billed but absent."""
    lines = [line for line in IDLE_LINES if line[0][0] != "Amazon Elastic Container Service"]
    cells, _, _ = idle_cells(
        StubCostExplorer(lambda request: lines),
        env="dev",
        region=REGION,
        window=IdleWindow(date(2026, 10, 5), date(2026, 10, 6)),
        tags={"product": "marketing-ai", "env": "dev"},
        today=TODAY,
    )
    fargate = next(cell for cell in cells if cell.row == "fargate")
    assert fargate.status == CellStatus.NOT_MEASURED and fargate.text == MARKER and fargate.value is None
    assert "not a zero" in (fargate.reason or "") and "--absent-idle-row fargate" in (fargate.reason or "")


def test_a_line_counts_only_for_the_row_that_claims_it() -> None:
    """RDS storage also matches the instance row's rule; it must not make the instance row present."""
    lines = [line for line in IDLE_LINES if "InstanceUsage" not in line[0][1]]
    cells, _, _ = idle_cells(
        StubCostExplorer(lambda request: lines),
        env="dev",
        region=REGION,
        window=IdleWindow(date(2026, 10, 5), date(2026, 10, 6)),
        tags={"product": "marketing-ai", "env": "dev"},
        today=TODAY,
    )
    by_row = {cell.row: cell for cell in cells}
    assert by_row["rds_instance"].status == CellStatus.NOT_MEASURED
    assert by_row["rds_storage"].status == CellStatus.MEASURED


def test_a_row_the_operator_states_absent_reads_none_unless_cost_explorer_disagrees() -> None:
    lines = [line for line in IDLE_LINES if line[0][0] != "EC2 - Other"]
    window = IdleWindow(date(2026, 10, 5), date(2026, 10, 6))
    tags = {"product": "marketing-ai", "env": "dev"}
    cells, _, _ = idle_cells(
        StubCostExplorer(lambda request: lines),
        env="dev",
        region=REGION,
        window=window,
        tags=tags,
        today=TODAY,
        absent=("network",),
    )
    network = next(cell for cell in cells if cell.row == "network")
    assert network.status == CellStatus.NOT_APPLICABLE and "none in this deployment" in network.text
    cells, _, _ = idle_cells(
        idle_stub(), env="dev", region=REGION, window=window, tags=tags, today=TODAY, absent=("network",)
    )
    network = next(cell for cell in cells if cell.row == "network")
    assert network.status == CellStatus.NOT_MEASURED and "one of the two is wrong" in (network.reason or "")
    with pytest.raises(CostCaptureError) as caught:
        idle_cells(
            idle_stub(), env="dev", region=REGION, window=window, tags=tags, today=TODAY, absent=("x",)
        )
    assert caught.value.code == "UNKNOWN_IDLE_ROW"


def test_a_public_ipv4_line_is_not_counted_as_the_nat_gateway_or_endpoints() -> None:
    lines = [*IDLE_LINES, (("Amazon Virtual Private Cloud", "APS3-PublicIPv4:InUseAddress"), 0.12)]
    cells, _, notes = idle_cells(
        StubCostExplorer(lambda request: lines),
        env="dev",
        region=REGION,
        window=IdleWindow(date(2026, 10, 5), date(2026, 10, 6)),
        tags={"product": "marketing-ai", "env": "dev"},
        today=TODAY,
    )
    network = next(cell for cell in cells if cell.row == "network")
    assert network.value == pytest.approx(1.0 * DAYS_PER_MONTH, abs=1e-3)
    assert any("Amazon Virtual Private Cloud" in note for note in notes), "named in the total instead"


def test_every_query_leaves_out_credits_and_refunds_and_says_so() -> None:
    """A credited new account nets its usage to zero; that is who paid, not what it costs."""
    client = idle_stub()
    cells, _, _ = idle_cells(
        client,
        env="dev",
        region=REGION,
        window=IdleWindow(date(2026, 10, 5), date(2026, 10, 6)),
        tags={"product": "marketing-ai", "env": "dev"},
        today=TODAY,
    )
    exclusion = {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}}
    assert exclusion in filters_of(client.requests[0])
    assert all("credits and refunds excluded" in cell.source for cell in cells)


def test_a_tiny_amount_is_never_printed_as_zero() -> None:
    cells, _ = run_cells(
        row("score_reference"),
        manifest("run-tiny", entrypoint=JobEntrypoint.SCORE, billable=None),
        client=run_stub(0.00003),
        attribution=Attribution.TAG,
        today=TODAY,
    )
    billed = next(cell for cell in cells if cell.column == "billed")
    assert billed.text.startswith("USD 0.00003 billed"), billed.text


def test_an_empty_answer_is_not_recorded_as_a_free_deployment() -> None:
    cells, queries, _ = idle_cells(
        StubCostExplorer(lambda request: ()),
        env="dev",
        region=REGION,
        window=IdleWindow(date(2026, 10, 5), date(2026, 10, 6)),
        tags={"product": "marketing-ai", "env": "dev"},
        today=TODAY,
    )
    assert all(cell.status == CellStatus.NOT_MEASURED and cell.text == MARKER for cell in cells)
    assert all("not active" in (cell.reason or "") for cell in cells)
    assert len(queries) == 1, "the empty query is still recorded, as evidence of what was asked"


def test_a_window_cost_explorer_has_not_caught_up_with_is_refused_without_a_query() -> None:
    client = idle_stub()
    cells, queries, _ = idle_cells(
        client,
        env="dev",
        region=REGION,
        window=IdleWindow(date(2026, 10, 5), date(2026, 10, 6)),
        tags={"product": "marketing-ai", "env": "dev"},
        today=date(2026, 10, 6),
    )
    assert not client.requests and not queries
    assert all("lags" in (cell.reason or "") and "2026-10-07" in (cell.reason or "") for cell in cells)


def test_an_idle_window_that_saw_a_run_is_refused() -> None:
    client = idle_stub()
    cells, _, _ = idle_cells(
        client,
        env="dev",
        region=REGION,
        window=IdleWindow(date(2026, 10, 7), date(2026, 10, 8)),
        tags={"product": "marketing-ai", "env": "dev"},
        today=TODAY,
        run_days={"run-a": run_days(manifest("run-a"))},
    )
    assert not client.requests
    assert all("was not idle" in (cell.reason or "") and "run-a" in (cell.reason or "") for cell in cells)


def test_a_currency_other_than_usd_is_refused_not_converted() -> None:
    with pytest.raises(CostCaptureError) as caught:
        idle_cells(
            StubCostExplorer(lambda request: IDLE_LINES, unit="INR"),
            env="dev",
            region=REGION,
            window=IdleWindow(date(2026, 10, 5), date(2026, 10, 6)),
            tags=None,
            today=TODAY,
        )
    assert caught.value.code == "UNEXPECTED_CURRENCY"


def test_every_page_is_read_and_account_wide_drops_the_tags() -> None:
    client = StubCostExplorer(lambda request: IDLE_LINES, pages=3, estimated=True)
    cells, _, _ = idle_cells(
        client,
        env="dev",
        region=REGION,
        window=IdleWindow(date(2026, 10, 5), date(2026, 10, 6)),
        tags=None,
        today=TODAY,
    )
    assert [request.get("NextPageToken") for request in client.requests] == [None, "1", "2"]
    assert tag_value(client.requests[0], "product") is None
    assert all("account-wide" in cell.source and "provisional" in cell.source for cell in cells)


def test_an_empty_window_is_refused() -> None:
    with pytest.raises(CostCaptureError):
        IdleWindow(date(2026, 10, 5), date(2026, 10, 5))


# ---------------------------------------------------------------------------
# Per run
# ---------------------------------------------------------------------------
def row(row_id: str) -> RunRow:
    return next(item for item in PER_RUN_ROWS if item.id == row_id)


def run_stub(amount: float = 0.2) -> StubCostExplorer:
    return StubCostExplorer(lambda request: ((("Amazon SageMaker",), amount),))


def test_a_training_run_fills_its_row_with_three_kinds_of_figure() -> None:
    client = run_stub()
    cells, queries = run_cells(
        row("train_fast"),
        manifest("run-fast", strategy=Strategy.FAST),
        client=client,
        attribution=Attribution.TAG,
        today=TODAY,
    )
    by_column = {cell.column: cell for cell in cells}
    assert set(by_column) == set(PER_RUN_COLUMNS)
    assert by_column["billable_seconds"].kind == FigureKind.REPORTED
    assert by_column["billable_seconds"].value == 600.0
    assert by_column["instance_type"].text == f"{TRAIN_INSTANCE} x 1"
    list_price = by_column["list_price"]
    assert list_price.kind == FigureKind.LIST_PRICE_ESTIMATE
    assert "at list price, offer version" in list_price.text
    assert "published list price" in list_price.source
    billed = by_column["billed"]
    assert billed.kind == FigureKind.BILLED and billed.value == pytest.approx(0.2)
    assert "billed 2026-10-07" in billed.text
    (request,) = client.requests
    assert tag_value(request, "run_id") == "run-fast"
    assert request["TimePeriod"] == {"Start": "2026-10-07", "End": "2026-10-08"}
    assert len(queries) == 1


@pytest.mark.parametrize(
    ("row_id", "built", "fragment"),
    [
        ("train_fast", {"strategy": Strategy.BALANCED}, "needs fast"),
        ("train_reference", {"rows": 5_000}, "names 7,043"),
        ("score_reference", {}, "needs a score job"),
        ("train_balanced", {"backend": ComputeBackend.LOCAL}, "did not run as a SageMaker job"),
        (
            "score_per_100k",
            {"entrypoint": JobEntrypoint.SCORE, "billable": None, "rows": 7_043},
            "at least 100,000 rows",
        ),
    ],
)
def test_a_manifest_that_does_not_match_its_row_fills_nothing(
    row_id: str, built: dict[str, Any], fragment: str
) -> None:
    client = run_stub()
    cells, queries = run_cells(
        row(row_id), manifest("run-x", **built), client=client, attribution=Attribution.TAG, today=TODAY
    )
    assert not client.requests and not queries
    assert all(cell.status == CellStatus.NOT_MEASURED and cell.text == MARKER for cell in cells)
    assert all(fragment in (cell.reason or "") for cell in cells)


def test_a_scoring_run_says_it_has_no_billable_time_and_is_billed_from_cost_explorer() -> None:
    cells, _ = run_cells(
        row("score_reference"),
        manifest("run-score", entrypoint=JobEntrypoint.SCORE, billable=None),
        client=run_stub(0.05),
        attribution=Attribution.TAG,
        today=TODAY,
    )
    by_column = {cell.column: cell for cell in cells}
    assert by_column["billable_seconds"].status == CellStatus.NOT_APPLICABLE
    assert "DEC-332" in by_column["billable_seconds"].text
    assert by_column["list_price"].status == CellStatus.NOT_APPLICABLE
    assert by_column["billed"].status == CellStatus.MEASURED


def test_per_100k_rows_scales_down_from_a_larger_file_and_says_by_how_much() -> None:
    cells, _ = run_cells(
        row("score_per_100k"),
        manifest("run-big", entrypoint=JobEntrypoint.SCORE, billable=None, rows=400_000),
        client=run_stub(0.8),
        attribution=Attribution.TAG,
        today=TODAY,
    )
    billed = next(cell for cell in cells if cell.column == "billed")
    assert billed.value == pytest.approx(0.2)
    assert "x 0.25 from 400,000 rows" in billed.text


def test_a_run_id_tag_with_no_line_asks_for_the_tag_to_be_activated() -> None:
    cells, queries = run_cells(
        row("train_balanced"),
        manifest("run-b"),
        client=StubCostExplorer(lambda request: ()),
        attribution=Attribution.TAG,
        today=TODAY,
    )
    billed = next(cell for cell in cells if cell.column == "billed")
    assert billed.status == CellStatus.NOT_MEASURED
    assert "activate run_id" in (billed.reason or "")
    assert len(queries) == 1


def test_day_attribution_is_refused_when_another_measured_run_shares_the_day() -> None:
    this = manifest("run-a")
    other = manifest("run-b", finished=RUN_DAY + timedelta(hours=3))
    client = run_stub()
    cells, _ = run_cells(
        row("train_balanced"),
        this,
        client=client,
        attribution=Attribution.DAY,
        today=TODAY,
        other_runs={"run-a": run_days(this), "run-b": run_days(other)},
    )
    billed = next(cell for cell in cells if cell.column == "billed")
    assert billed.status == CellStatus.NOT_MEASURED and "run-b" in (billed.reason or "")
    assert not client.requests


def test_day_attribution_reads_the_whole_days_sagemaker_line_when_the_day_is_the_runs_alone() -> None:
    this = manifest("run-a")
    client = run_stub()
    cells, _ = run_cells(
        row("train_balanced"),
        this,
        client=client,
        attribution=Attribution.DAY,
        today=TODAY,
        other_runs={
            "run-a": run_days(this),
            "run-c": run_days(manifest("run-c", finished=RUN_DAY + timedelta(days=2))),
        },
    )
    (request,) = client.requests
    assert {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon SageMaker"]}} in filters_of(request)
    assert tag_value(request, "run_id") is None
    assert "SageMaker line for the whole day" in next(cell for cell in cells if cell.column == "billed").text


def test_a_run_crossing_midnight_is_billed_over_both_days() -> None:
    late = manifest("run-late", finished=datetime(2026, 10, 8, 0, 10, tzinfo=UTC), duration=1_800)
    assert run_days(late) == frozenset({date(2026, 10, 7), date(2026, 10, 8)})
    client = run_stub()
    run_cells(row("train_balanced"), late, client=client, attribution=Attribution.TAG, today=TODAY)
    assert client.requests[0]["TimePeriod"] == {"Start": "2026-10-07", "End": "2026-10-09"}


# ---------------------------------------------------------------------------
# The assistant
# ---------------------------------------------------------------------------
ASK_DAY = date(2026, 10, 9)
BEFORE = usage(
    answer_calls=0,
    tokens_in=0,
    tokens_out=0,
    embed_calls=10,
    embed_tokens=5_000,
    at=datetime(2026, 10, 8, 9, tzinfo=UTC),
)
AFTER = usage(
    answer_calls=20,
    tokens_in=40_000,
    tokens_out=6_000,
    embed_calls=30,
    embed_tokens=5_400,
    at=datetime(2026, 10, 9, 15, tzinfo=UTC),
)


def bedrock_stub() -> StubCostExplorer:
    return StubCostExplorer(
        lambda request: (
            (("Amazon Bedrock",), 0.01),
            (("Claude Sonnet (Amazon Bedrock Edition)",), 0.09),
            (("Amazon Simple Storage Service",), 5.0),
        )
    )


def test_the_assistant_row_is_the_difference_between_two_snapshots_per_1000_questions() -> None:
    prices = PriceTable(
        prices={
            "some.generation-model-v1:0": ModelPrice(input_per_1m=1.0, output_per_1m=2.0),
            "some.embedding-model-v2:0": ModelPrice(input_per_1m=0.5, output_per_1m=0.0),
        },
        as_of="2026-10-01",
        source="test fixture",
    )
    client = bedrock_stub()
    cells, _ = assistant_cells(
        BEFORE, AFTER, questions=20, day=ASK_DAY, prices=prices, client=client, today=TODAY
    )
    by_column = {cell.column: cell for cell in cells}
    assert all(cell.row == ASSISTANT_ROW[0] for cell in cells)
    tokens = by_column["billable_seconds"]
    # 40,000 + 400 input and 6,000 output tokens for 20 questions, times 50.
    assert tokens.text.startswith("2,020,000 input + 300,000 output tokens (x 50 from 20 questions)")
    assert by_column["instance_type"].text == "some.embedding-model-v2:0, some.generation-model-v1:0"
    expected = (40_000 * 1.0 + 6_000 * 2.0 + 400 * 0.5) / 1_000_000 * 50
    assert by_column["list_price"].value == pytest.approx(expected)
    assert "as of 2026-10-01" in by_column["list_price"].text
    billed = by_column["billed"]
    assert billed.value == pytest.approx((0.01 + 0.09) * 50), "every Bedrock service, and nothing else"
    assert "account-wide" in billed.text
    # Account-wide: no region and no tag, only the credits-and-refunds exclusion every query carries.
    assert list(client.requests[0]["Filter"]) == ["Not"]


def test_an_unpriced_model_leaves_the_list_price_cell_unmeasured() -> None:
    cells, _ = assistant_cells(
        BEFORE, AFTER, questions=20, day=ASK_DAY, prices=PriceTable(), client=bedrock_stub(), today=TODAY
    )
    list_price = next(cell for cell in cells if cell.column == "list_price")
    assert list_price.status == CellStatus.NOT_MEASURED
    assert "some.generation-model-v1:0" in (list_price.reason or "")


@pytest.mark.parametrize(
    ("questions", "day", "after", "fragment"),
    [
        (10, ASK_DAY, AFTER, "20 answering calls"),
        (20, date(2026, 10, 10), AFTER, "not on 2026-10-10"),
        (
            20,
            ASK_DAY,
            usage(answer_calls=20, tokens_in=1, tokens_out=1, at=AFTER.created_at, job_id="idx-2"),
            "different indexes",
        ),
        (0, ASK_DAY, AFTER, "must be positive"),
    ],
)
def test_snapshots_that_do_not_describe_the_questions_fill_nothing(
    questions: int, day: date, after: LlmUsageReport, fragment: str
) -> None:
    client = bedrock_stub()
    cells, _ = assistant_cells(
        BEFORE, after, questions=questions, day=day, prices=None, client=client, today=TODAY
    )
    assert all(cell.status == CellStatus.NOT_MEASURED and fragment in (cell.reason or "") for cell in cells)
    assert not client.requests


# ---------------------------------------------------------------------------
# Merging and the document
# ---------------------------------------------------------------------------
def measurement(cells: Sequence[Cell], at: datetime) -> CostMeasurement:
    return CostMeasurement(measured_at=at, command="test", cells=tuple(cells))


def full_per_run_cells() -> list[Cell]:
    """Every per-run cell, measured through the real functions with stubbed inputs."""
    cells: list[Cell] = []
    specs: dict[str, dict[str, Any]] = {
        "train_template": {"rows": 50},
        "train_reference": {},
        "score_reference": {"entrypoint": JobEntrypoint.SCORE, "billable": None},
        "train_fast": {"strategy": Strategy.FAST},
        "train_balanced": {},
        "train_exhaustive": {"strategy": Strategy.EXHAUSTIVE},
        "score_per_100k": {"entrypoint": JobEntrypoint.SCORE, "billable": None, "rows": 100_000},
    }
    for row_id, built in specs.items():
        row_cells, _ = run_cells(
            row(row_id),
            manifest(f"run-{row_id}", **built),
            client=run_stub(),
            attribution=Attribution.TAG,
            today=TODAY,
        )
        cells.extend(row_cells)
    prices = PriceTable(
        prices={
            "some.generation-model-v1:0": ModelPrice(input_per_1m=1.0, output_per_1m=2.0),
            "some.embedding-model-v2:0": ModelPrice(input_per_1m=0.5, output_per_1m=0.0),
        },
        as_of="2026-10-01",
        source="test fixture",
    )
    assistant, _ = assistant_cells(
        BEFORE, AFTER, questions=20, day=ASK_DAY, prices=prices, client=bedrock_stub(), today=TODAY
    )
    cells.extend(assistant)
    return cells


def full_idle_cells(env: str) -> list[Cell]:
    cells, _, _ = idle_cells(
        idle_stub(),
        env=env,
        region=REGION,
        window=IdleWindow(date(2026, 10, 5), date(2026, 10, 6)),
        tags={"product": "marketing-ai", "env": env},
        today=TODAY,
    )
    return cells


@pytest.fixture
def guide(repo_root: Path) -> str:
    return (repo_root / "docs" / "AWS_DEPLOYMENT.md").read_text(encoding="utf-8")


def test_the_shipped_guide_is_entirely_unmeasured_by_this_modules_reading(guide: str) -> None:
    assert table_state(guide, IDLE_HEADING) == TableState.UNMEASURED
    assert table_state(guide, PER_RUN_HEADING) == TableState.UNMEASURED


def test_a_complete_table_is_written_whole_and_every_line_of_it_is_sourced(guide: str) -> None:
    cells = merge_cells([measurement(full_per_run_cells(), datetime(2026, 10, 20, tzinfo=UTC))])
    rendered, outcomes = render_document(guide, cells)
    per_run = next(outcome for outcome in outcomes if outcome.table == TableId.PER_RUN)
    idle = next(outcome for outcome in outcomes if outcome.table == TableId.IDLE)
    assert per_run.complete and per_run.written and not per_run.missing
    assert not idle.written
    assert table_state(rendered, PER_RUN_HEADING) == TableState.MEASURED
    assert table_state(rendered, IDLE_HEADING) == TableState.UNMEASURED
    changed = [line for line in rendered.splitlines() if line not in guide.splitlines()]
    assert len(changed) == len(PER_RUN_ROWS) + 1, "exactly the per-run table's data rows changed"
    for line in changed:
        assert CURRENCY.search(line) is None or CITATION.search(line), f"unsourced figure: {line}"
        assert MARKER not in line
    # Nothing outside the table moved.
    assert len(rendered.splitlines()) == len(guide.splitlines())


def test_one_missing_cell_leaves_the_whole_table_untouched(guide: str) -> None:
    cells = full_per_run_cells()
    gap = next(
        index
        for index, cell in enumerate(cells)
        if cell.column == "billed" and cell.row == "train_exhaustive"
    )
    cells[gap] = cells[gap].model_copy(
        update={"status": CellStatus.NOT_MEASURED, "text": MARKER, "reason": "stub gap"}
    )
    rendered, outcomes = render_document(
        guide, merge_cells([measurement(cells, datetime(2026, 10, 20, tzinfo=UTC))])
    )
    assert rendered == guide
    per_run = next(outcome for outcome in outcomes if outcome.table == TableId.PER_RUN)
    assert not per_run.written
    assert per_run.missing == (
        "Train, Telco Churn, strategy `exhaustive` / Cost Explorer, same day: stub gap",
    )


def test_the_idle_table_waits_for_its_prod_column_and_is_written_once_it_has_one(guide: str) -> None:
    dev_only = merge_cells([measurement(full_idle_cells("dev"), datetime(2026, 10, 7, tzinfo=UTC))])
    rendered, outcomes = render_document(guide, dev_only)
    assert rendered == guide
    idle = next(outcome for outcome in outcomes if outcome.table == TableId.IDLE)
    assert len(idle.missing) == len(IDLE_ROWS) + 1
    both = merge_cells(
        [
            measurement(full_idle_cells("dev"), datetime(2026, 10, 7, tzinfo=UTC)),
            measurement(full_idle_cells("prod"), datetime(2026, 10, 8, tzinfo=UTC)),
        ]
    )
    rendered, _ = render_document(guide, both)
    assert table_state(rendered, IDLE_HEADING) == TableState.MEASURED
    assert table_state(rendered, PER_RUN_HEADING) == TableState.UNMEASURED


def test_a_figure_beats_a_gap_and_a_later_figure_beats_an_earlier_one() -> None:
    measured = full_idle_cells("dev")[0]
    gap = measured.model_copy(update={"status": CellStatus.NOT_MEASURED, "text": MARKER, "value": None})
    later = measured.model_copy(update={"text": "later"})
    merged = merge_cells(
        [
            measurement([measured], datetime(2026, 10, 7, tzinfo=UTC)),
            measurement([gap], datetime(2026, 10, 9, tzinfo=UTC)),
        ]
    )
    assert merged[(measured.table, measured.row, measured.column)].status == CellStatus.MEASURED
    merged = merge_cells(
        [
            measurement([later], datetime(2026, 10, 9, tzinfo=UTC)),
            measurement([measured], datetime(2026, 10, 7, tzinfo=UTC)),
        ]
    )
    assert merged[(measured.table, measured.row, measured.column)].text == "later"


def test_an_idle_column_is_merged_whole_so_its_total_matches_its_rows() -> None:
    """A later session missing Fargate (untagged) must not lend its Total to an earlier Fargate figure."""
    complete = full_idle_cells("dev")
    no_fargate_lines = [line for line in IDLE_LINES if line[0][0] != "Amazon Elastic Container Service"]
    later, _, _ = idle_cells(
        StubCostExplorer(lambda request: no_fargate_lines),
        env="dev",
        region=REGION,
        window=IdleWindow(date(2026, 10, 12), date(2026, 10, 13)),
        tags={"product": "marketing-ai", "env": "dev"},
        today=TODAY,
    )
    merged = merge_cells(
        [
            measurement(complete, datetime(2026, 10, 7, tzinfo=UTC)),
            measurement(later, datetime(2026, 10, 14, tzinfo=UTC)),
        ]
    )
    column = [cell for key, cell in merged.items() if key[0] == TableId.IDLE]
    assert {cell.source for cell in column} == {complete[0].source}, "one session's column, whole"


def test_a_per_run_row_is_merged_whole_so_it_describes_one_run() -> None:
    first, _ = run_cells(
        row("train_fast"),
        manifest("run-1", strategy=Strategy.FAST),
        client=run_stub(),
        attribution=Attribution.TAG,
        today=TODAY,
    )
    second, _ = run_cells(
        row("train_fast"),
        manifest("run-2", strategy=Strategy.FAST),
        client=None,
        attribution=Attribution.TAG,
        today=TODAY,
    )
    merged = merge_cells(
        [
            measurement(first, datetime(2026, 10, 8, tzinfo=UTC)),
            measurement(second, datetime(2026, 10, 9, tzinfo=UTC)),
        ]
    )
    assert all("run-1" in cell.source for key, cell in merged.items() if key[1] == "train_fast")


def test_a_partly_filled_table_reads_as_partial() -> None:
    text = f"{PER_RUN_HEADING}\n\n| Run | A | B |\n|---|---|---|\n| x | {MARKER} | USD 1 on 2026-10-07 |\n"
    assert table_state(text, PER_RUN_HEADING) == TableState.PARTIAL
