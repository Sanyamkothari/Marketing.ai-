"""CloudWatch metrics: the EMF document's shape, and the three rules that keep it honest.

The rules, each of which has a test below rather than a comment somewhere:

* **A metric may never fail a run.** Every `record_*` function swallows whatever the sink raises,
  so a broken stream, a full disk or a sink somebody wrote badly costs a datapoint and not a run.
* **No number is invented.** `JobCostUsd` is emitted only where a real figure exists. A run with no
  estimate, an estimate that could not be stated, and a zero all emit *nothing* - a zero on a cost
  graph is indistinguishable from "it was free", which is not what "we do not know" means.
* **The generative hook reads nothing.** `LlmCostUsd` exists so the call site can be written once;
  the generative phase is not in this repository, so it is off by default and there is no file, no
  price table and no model list for it to consult.

The shape assertions are offline and deliberately literal: there is no AWS account here, so what
can be checked is that the document says what the EMF specification's own field names say, and that
every dimension set it declares has a matching key in the body - which is the mistake that produces
a metric CloudWatch silently drops.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import threading
from pathlib import Path

import pytest

from engine.aws import metrics as metrics_module
from engine.aws.metrics import (
    COARSE_DIMENSIONS,
    JOB_COST_USD,
    LLM_COST_USD,
    MAX_DIMENSION_VALUE_LENGTH,
    MAX_METRIC_NAME_LENGTH,
    METRICS,
    NAMESPACE,
    RUNS_FAILED,
    RUNS_STARTED,
    STAGE_DURATION_SECONDS,
    UNSET_CLIENT_ID,
    EmfMetricSink,
    MetricSink,
    MetricSpec,
    NullMetricSink,
    Unit,
    metric_names,
    metric_sink_for,
    record_job_cost,
    record_llm_cost,
    record_run_failed,
    record_run_started,
    record_stage_duration,
)
from engine.contracts import CostEstimate
from engine.settings import Settings

USE_CASE = "targeted-advertisement"


class _Recorder:
    """A sink that keeps what it was handed. Three lines, because `MetricSink` has one member."""

    def __init__(self) -> None:
        self.calls: list[tuple[MetricSpec, float, str | None]] = []

    def emit(self, spec: MetricSpec, value: float, *, detail: str | None = None) -> None:
        self.calls.append((spec, value, detail))


class _Exploding:
    """A sink that fails the way a real one fails: at the moment of writing."""

    def emit(self, spec: MetricSpec, value: float, *, detail: str | None = None) -> None:
        raise OSError("no space left on device")


def sink_and_lines() -> tuple[EmfMetricSink, io.StringIO]:
    stream = io.StringIO()
    return EmfMetricSink(env="prod", client_id="acme", stream=stream), stream


def emitted(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


# ---------------------------------------------------------------------------
# The registry, which the dashboard generator also reads
# ---------------------------------------------------------------------------
def test_the_five_metric_names_are_the_frozen_ones() -> None:
    assert metric_names() == (
        "RunsStarted",
        "RunsFailed",
        "StageDurationSeconds",
        "JobCostUsd",
        "LlmCostUsd",
    )


def test_every_metric_is_describable_and_within_the_service_limit() -> None:
    for spec in METRICS:
        assert 0 < len(spec.name) <= MAX_METRIC_NAME_LENGTH
        assert spec.description.strip(), f"{spec.name} must say what it counts"
        assert isinstance(spec.unit, Unit)


def test_the_reserved_generative_metric_is_the_only_one_that_is_not_alarmable() -> None:
    assert [spec.name for spec in METRICS if not spec.alarmable] == ["LlmCostUsd"]


# ---------------------------------------------------------------------------
# The default sink
# ---------------------------------------------------------------------------
def test_the_null_sink_satisfies_the_protocol_and_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    sink = NullMetricSink()
    assert isinstance(sink, MetricSink)

    record_run_started(sink, use_case_id=USE_CASE)
    record_stage_duration(sink, stage="train", seconds=12.5)

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("backend", ["none", "emf"])
def test_the_backend_setting_chooses_the_sink(backend: str) -> None:
    settings = Settings(metrics_backend=backend, env="dev", client_id="acme")
    sink = metric_sink_for(settings)
    assert isinstance(sink, EmfMetricSink if backend == "emf" else NullMetricSink)


def test_the_default_settings_choose_the_sink_that_does_nothing() -> None:
    assert isinstance(metric_sink_for(Settings()), NullMetricSink)


# ---------------------------------------------------------------------------
# The EMF document
# ---------------------------------------------------------------------------
def test_one_datapoint_is_one_json_line_with_the_emf_node() -> None:
    sink, stream = sink_and_lines()

    record_run_started(sink, use_case_id=USE_CASE)

    (document,) = emitted(stream)
    aws = document["_aws"]
    (directive,) = aws["CloudWatchMetrics"]
    assert directive["Namespace"] == NAMESPACE
    assert directive["Metrics"] == [{"Name": "RunsStarted", "Unit": "Count"}]
    assert isinstance(aws["Timestamp"], int), "EMF wants milliseconds since the epoch, as an integer"
    assert document["RunsStarted"] == 1.0


def test_every_datapoint_carries_the_coarse_set_and_the_detail_set() -> None:
    sink, stream = sink_and_lines()

    record_stage_duration(sink, stage="train", seconds=12.5)

    (document,) = emitted(stream)
    (directive,) = document["_aws"]["CloudWatchMetrics"]
    assert directive["Dimensions"] == [["Env", "ClientId"], ["Env", "ClientId", "Stage"]]
    assert document["Env"] == "prod"
    assert document["ClientId"] == "acme"
    assert document["Stage"] == "train"
    assert document["StageDurationSeconds"] == 12.5


def test_every_declared_dimension_has_a_value_in_the_body() -> None:
    """The mistake that produces a metric CloudWatch accepts and then silently drops."""
    sink, stream = sink_and_lines()

    record_run_started(sink, use_case_id=USE_CASE)
    record_run_failed(sink, use_case_id=USE_CASE)
    record_stage_duration(sink, stage="prepare", seconds=1.0)

    for document in emitted(stream):
        (directive,) = document["_aws"]["CloudWatchMetrics"]
        for dimension_set in directive["Dimensions"]:
            for name in dimension_set:
                assert document.get(name), f"{name} is declared but has no value"


def test_a_metric_with_no_detail_still_publishes_the_coarse_set() -> None:
    sink, stream = sink_and_lines()

    sink.emit(RUNS_STARTED, 1.0, detail=None)

    (document,) = emitted(stream)
    (directive,) = document["_aws"]["CloudWatchMetrics"]
    assert directive["Dimensions"] == [list(COARSE_DIMENSIONS)]
    assert "UseCaseId" not in document


def test_a_deployment_that_names_no_client_still_carries_the_client_dimension() -> None:
    stream = io.StringIO()
    sink = EmfMetricSink(env="local", client_id=None, stream=stream)

    record_run_started(sink, use_case_id=USE_CASE)

    (document,) = emitted(stream)
    assert document["ClientId"] == UNSET_CLIENT_ID
    assert UNSET_CLIENT_ID == "unset", "a placeholder, never a guess at who the client is"


def test_an_over_long_dimension_value_is_truncated_rather_than_dropped() -> None:
    sink, stream = sink_and_lines()
    absurd = "x" * (MAX_DIMENSION_VALUE_LENGTH + 500)

    record_run_started(sink, use_case_id=absurd)

    (document,) = emitted(stream)
    assert len(document["UseCaseId"]) == MAX_DIMENSION_VALUE_LENGTH


def test_a_blank_detail_drops_its_dimension_instead_of_sending_an_empty_one() -> None:
    sink, stream = sink_and_lines()

    record_stage_duration(sink, stage="   ", seconds=1.0)

    (document,) = emitted(stream)
    (directive,) = document["_aws"]["CloudWatchMetrics"]
    assert directive["Dimensions"] == [list(COARSE_DIMENSIONS)]
    assert "Stage" not in document


def test_a_metric_name_longer_than_the_service_limit_is_refused() -> None:
    sink, _stream = sink_and_lines()
    too_long = MetricSpec(
        name="x" * (MAX_METRIC_NAME_LENGTH + 1),
        unit=Unit.COUNT,
        detail_dimension=None,
        description="not a real metric",
    )

    with pytest.raises(ValueError, match=str(MAX_METRIC_NAME_LENGTH)):
        sink.emit(too_long, 1.0)


def test_the_sink_follows_a_stream_that_was_replaced_after_it_was_built(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default is resolved on every write, which is what makes stdout capture work."""
    sink = EmfMetricSink(env="prod", client_id="acme")

    record_run_started(sink, use_case_id=USE_CASE)

    document = json.loads(capsys.readouterr().out.strip())
    assert document["RunsStarted"] == 1.0


def test_concurrent_writers_produce_whole_lines() -> None:
    """A partial line is not a metric; CloudWatch drops the whole document."""
    stream = io.StringIO()
    sink = EmfMetricSink(env="prod", client_id="acme", stream=stream)
    start = threading.Barrier(4)

    def write() -> None:
        start.wait(timeout=10)
        for _ in range(25):
            record_run_started(sink, use_case_id=USE_CASE)

    threads = [threading.Thread(target=write) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    documents = emitted(stream)
    assert len(documents) == 100, "every line parsed, so no two writes interleaved"
    assert all(document["RunsStarted"] == 1.0 for document in documents)


# ---------------------------------------------------------------------------
# A metric may never fail a run
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "call",
    [
        lambda sink: record_run_started(sink, use_case_id=USE_CASE),
        lambda sink: record_run_failed(sink, use_case_id=USE_CASE),
        lambda sink: record_stage_duration(sink, stage="train", seconds=1.0),
        lambda sink: record_job_cost(
            sink, CostEstimate(compute_seconds=60.0, estimated_usd=0.5, basis="list price"), backend="thread"
        ),
        lambda sink: record_llm_cost(sink, model_id="m", usd=0.5, enabled=True),
    ],
    ids=["started", "failed", "stage", "job_cost", "llm_cost"],
)
def test_no_recording_function_lets_a_sink_failure_escape(call, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="engine.aws.metrics"):
        call(_Exploding())

    assert "metrics.emit_failed" in caplog.text
    assert "error=OSError" in caplog.text
    assert "no space left" not in caplog.text, "the exception's class, never its message (plan 13.7)"


def test_the_raw_protocol_call_is_still_allowed_to_raise() -> None:
    """`emit` is the raw one; the guard lives in the `record_*` functions, in one place."""
    with pytest.raises(OSError, match="no space left"):
        _Exploding().emit(RUNS_STARTED, 1.0)


# ---------------------------------------------------------------------------
# No number is invented
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "estimate",
    [
        None,
        CostEstimate(compute_seconds=60.0, estimated_usd=None, basis="local run: nothing was billed"),
        CostEstimate(compute_seconds=0.0, estimated_usd=0.0, basis="no billable time"),
    ],
    ids=["no_estimate", "unstateable", "zero"],
)
def test_job_cost_is_not_emitted_unless_a_real_figure_exists(estimate: CostEstimate | None) -> None:
    recorder = _Recorder()

    record_job_cost(recorder, estimate, backend="sagemaker-training")

    assert recorder.calls == [], "a zero on a cost graph reads as 'it was free', not as 'unknown'"


def test_job_cost_is_emitted_when_there_is_a_figure_to_emit() -> None:
    recorder = _Recorder()
    estimate = CostEstimate(
        compute_seconds=120.0, estimated_usd=0.0408, basis="published list price for ml.c5.xlarge"
    )

    record_job_cost(recorder, estimate, backend="sagemaker-training")

    assert recorder.calls == [(JOB_COST_USD, 0.0408, "sagemaker-training")]


# ---------------------------------------------------------------------------
# The generative hook, which reads nothing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("usd", "enabled"),
    [(0.5, False), (None, True), (0.0, True), (None, False)],
    ids=["disabled", "no_figure", "zero", "neither"],
)
def test_the_llm_cost_hook_is_off_by_default_and_emits_nothing(usd: float | None, enabled: bool) -> None:
    recorder = _Recorder()

    record_llm_cost(recorder, model_id="anthropic.claude-v1", usd=usd, enabled=enabled)

    assert recorder.calls == []


def test_the_llm_cost_hook_emits_only_a_figure_its_caller_computed() -> None:
    recorder = _Recorder()

    record_llm_cost(recorder, model_id="anthropic.claude-v1", usd=1.25, enabled=True)

    assert recorder.calls == [(LLM_COST_USD, 1.25, "anthropic.claude-v1")]


def test_this_module_reads_no_price_table_and_imports_no_client() -> None:
    """The generative phase is absent: there is no file to read and nothing to call.

    Asserted on the parsed source rather than trusted, because the tempting fix when the hook is
    finally wired is to "just look up the model's price" from a table this repository does not
    have - and because the same parse proves DEC-306's no-boto3-at-module-scope rule holds here in
    the strongest possible form: there is no boto3 import anywhere in the file, at any depth.

    A parse, not a substring search: the module's own docstring argues at length about boto3 and
    about reading files, and a test that could not tell prose from code would either fail on the
    explanation or have to be written so loosely that it proved nothing.
    """
    assert metrics_module.__file__ is not None
    tree = ast.parse(Path(metrics_module.__file__).read_text(encoding="utf-8"))

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & {"boto3", "botocore"}, f"an AWS client crept in: {sorted(imported)}"

    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    }
    assert not called & {"open", "read_text", "read_bytes", "load", "safe_load"}, (
        "nothing here may read a file: there is no generative price table in this repository, and "
        "a call that looked for one would have to invent what it found"
    )


# ---------------------------------------------------------------------------
# The two counters
# ---------------------------------------------------------------------------
def test_the_run_counters_count_one_each_dimensioned_by_use_case() -> None:
    recorder = _Recorder()

    record_run_started(recorder, use_case_id=USE_CASE)
    record_run_failed(recorder, use_case_id=USE_CASE)

    assert recorder.calls == [
        (RUNS_STARTED, 1.0, USE_CASE),
        (RUNS_FAILED, 1.0, USE_CASE),
    ]


def test_a_stage_duration_is_recorded_as_the_seconds_it_was_given() -> None:
    recorder = _Recorder()

    record_stage_duration(recorder, stage="prepare", seconds=3)

    assert recorder.calls == [(STAGE_DURATION_SECONDS, 3.0, "prepare")]
