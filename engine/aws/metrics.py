"""CloudWatch metrics, written as EMF log lines rather than sent with `PutMetricData`.

**Why EMF.** The Embedded Metric Format is a JSON document written to *stdout*; the log driver
that is already carrying the process's output to CloudWatch Logs carries it too, and CloudWatch
extracts the metrics from it. Three things follow, and together they are the whole argument
(DEC-385):

*It needs no new permission.* A SageMaker job role and an ECS task role already have the
``logs:PutLogEvents`` their log driver uses. `PutMetricData` would need ``cloudwatch:PutMetricData``
added to both roles - a wildcard-resource permission, because `PutMetricData` has no resource-level
control - which is a real widening of the blast radius for something as peripheral as a counter.

*It adds no synchronous HTTPS call to the run's thread.* `PutMetricData` is a signed request to a
regional endpoint: it can block, it can throttle, it can time out, and it would do all three on the
thread that is in the middle of a stage. Writing a line to stdout cannot.

*It works identically from the API and from inside a job.* Both write to stdout; neither needs a
client, a region, credentials or a network path. The same code therefore runs on a laptop, where
the line is simply printed and nothing consumes it, and in a container, where it becomes a metric.

**Nothing here imports boto3**, at module scope or anywhere else - there is nothing to call
(DEC-306 holds trivially). It also does not go through `logging`: an EMF line is a *document* whose
shape CloudWatch parses, and a formatter that wrapped it in another JSON object - which
`RedactingJsonFormatter` would - would destroy it. So `EmfMetricSink` owns its stream (DEC-386).

**A metric may never fail a run.** The guarded entry points are the ``record_*`` functions, which
swallow anything a sink raises and log the exception's class (DEC-387). `MetricSink.emit` is the
raw call and is allowed to raise; call the ``record_*`` functions from pipeline code.

**No number is invented.** `JobCostUsd` is emitted only where a real figure exists - never a zero,
because a zero is indistinguishable from "nothing was billed" and `CostEstimate` already says that
with a null (plan section 13.3, DEC-388). `LlmCostUsd` is a *hook*: the generative phase is not in
this repository, so the function is here, defaults to off, and reads nothing (DEC-389).
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

    from engine.contracts import CostEstimate
    from engine.settings import Settings

__all__ = [
    "COARSE_DIMENSIONS",
    "MAX_DIMENSIONS_PER_METRIC",
    "MAX_DIMENSION_VALUE_LENGTH",
    "MAX_METRIC_NAME_LENGTH",
    "METRICS",
    "NAMESPACE",
    "UNSET_CLIENT_ID",
    "EmfMetricSink",
    "MetricSink",
    "MetricSpec",
    "NullMetricSink",
    "Unit",
    "metric_names",
    "metric_sink_for",
    "record_job_cost",
    "record_llm_cost",
    "record_run_failed",
    "record_run_started",
    "record_stage_duration",
]

_LOGGER = get_logger(__name__)

NAMESPACE: Final[str] = "MarketingAI"
"""The CloudWatch namespace every metric here is published under.

Chosen, not measured: it is a name, and the only property that matters is that it is this product's
and nobody else's. `scripts/gen_dashboard.py` reads it from here so the dashboard and the emitter
can never disagree about it.
"""

UNSET_CLIENT_ID: Final[str] = "unset"
"""The ``ClientId`` dimension value for a deployment that has not set `Settings.client_id`.

A **policy** placeholder, not a guess at who the client is. Every metric carries the same two
coarse dimensions whether or not the deployment names a client, because a metric whose dimension
set changes shape depending on configuration is a metric no dashboard or alarm can address: the
widget addressing ``{Env, ClientId}`` would simply show nothing, with no error anywhere (DEC-395).
"""

MAX_DIMENSIONS_PER_METRIC: Final[int] = 30
"""**Service limit.** The most dimensions one metric may carry.

Read from the CloudWatch API model shipped in this checkout rather than quoted from memory or from
a documentation page nobody can reach from here - it is the one source of these numbers that is
version-pinned, offline and checkable by whoever reads this line (DEC-394): botocore 1.43.99,
``botocore/data/cloudwatch/2010-08-01/service-2.json.gz``, shape ``Dimensions`` (``"max": 30``),
API version 2010-08-01. A dimension set longer than this is refused here rather than accepted and
silently dropped by CloudWatch.
"""

MAX_METRIC_NAME_LENGTH: Final[int] = 255
"""**Service limit.** Longest metric name CloudWatch accepts.

Same source: shape ``MetricName`` (``"max": 255``). Every name in :data:`METRICS` is a constant far
below it; the check exists so that a name added later fails here rather than in the account.
"""

MAX_DIMENSION_VALUE_LENGTH: Final[int] = 1024
"""**Service limit.** Longest dimension value CloudWatch accepts.

Same source: shape ``DimensionValue`` (``"max": 1024``). What this module does when a value is
longer is a **truncation policy**, not a limit: the value is cut to this length and the metric is
still emitted, because a use case id or a stage name that someone made absurdly long should cost a
truncated label rather than a missing datapoint.
"""

COARSE_DIMENSIONS: Final[tuple[str, ...]] = ("Env", "ClientId")
"""The dimension set every metric is published under, and the one dashboards and alarms address.

Every metric is emitted under **two** dimension sets: this one, and this one plus the metric's own
detail dimension. CloudWatch does not aggregate across dimensions - a datapoint stored under
``{Env, ClientId, Stage}`` is not readable as ``{Env, ClientId}`` - so a metric that was only ever
emitted with its detail could not be put on a dashboard without naming every stage by hand. EMF
publishes one document under each set it lists, which is what makes both views free (DEC-390).
"""


class Unit(StrEnum):
    """The CloudWatch units this product uses.

    A subset of the ``StandardUnit`` enumeration in the same botocore model cited above. It is a
    closed subset on purpose: a unit CloudWatch does not know is silently ignored, which turns a
    typo into a metric with no unit rather than into an error.
    """

    COUNT = "Count"
    SECONDS = "Seconds"
    NONE = "None"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One metric: its name, its unit, the dimension that distinguishes its detail view.

    This is the single description `EmfMetricSink` emits from and `scripts/gen_dashboard.py`
    generates from, so a metric cannot be renamed in one place and left alone in the other.
    """

    name: str
    unit: Unit
    detail_dimension: str | None
    description: str
    alarmable: bool = True


RUNS_STARTED: Final[MetricSpec] = MetricSpec(
    name="RunsStarted",
    unit=Unit.COUNT,
    detail_dimension="UseCaseId",
    description="A run was accepted and began executing.",
)

RUNS_FAILED: Final[MetricSpec] = MetricSpec(
    name="RunsFailed",
    unit=Unit.COUNT,
    detail_dimension="UseCaseId",
    description="A run ended in the failed state. Cancellation is not a failure and is not counted.",
)

STAGE_DURATION_SECONDS: Final[MetricSpec] = MetricSpec(
    name="StageDurationSeconds",
    unit=Unit.SECONDS,
    detail_dimension="Stage",
    description="Wall-clock seconds one pipeline stage took, as `log_stage` already reports it.",
)

JOB_COST_USD: Final[MetricSpec] = MetricSpec(
    name="JobCostUsd",
    unit=Unit.NONE,
    detail_dimension="Backend",
    description=(
        "Billable compute for one run at the published AWS list price named in `CostEstimate.basis`."
        " Emitted only where that figure exists; never a zero standing in for an unknown."
    ),
)

LLM_COST_USD: Final[MetricSpec] = MetricSpec(
    name="LlmCostUsd",
    unit=Unit.NONE,
    detail_dimension="ModelId",
    description=(
        "Reserved for the generative phase, which is not in this repository. The hook is off by"
        " default and nothing emits it, so no alarm is generated for it."
    ),
    alarmable=False,
)

METRICS: Final[tuple[MetricSpec, ...]] = (
    RUNS_STARTED,
    RUNS_FAILED,
    STAGE_DURATION_SECONDS,
    JOB_COST_USD,
    LLM_COST_USD,
)
"""Every metric this product publishes. `scripts/gen_dashboard.py` generates from exactly this."""


def metric_names() -> tuple[str, ...]:
    """The names in :data:`METRICS`, in declaration order."""
    return tuple(spec.name for spec in METRICS)


@runtime_checkable
class MetricSink(Protocol):
    """Somewhere a metric datapoint can go.

    One member, because a sink has one job and because a protocol with one member is one a test
    fake can satisfy in three lines. ``detail`` is the value of ``spec.detail_dimension``; passing
    ``None`` publishes only the coarse dimension set.

    This is the *raw* call and is allowed to raise. Pipeline code calls the ``record_*`` functions,
    which are the guarded ones (DEC-387).
    """

    def emit(self, spec: MetricSpec, value: float, *, detail: str | None = None) -> None: ...


class NullMetricSink:
    """The default sink: it does nothing, and does it without allocating anything.

    Selected whenever `Settings.metrics_backend` is ``none``, which is every local run. Metrics are
    therefore opt-in, and a laptop's stdout stays exactly as noisy as it was in Phase 1.
    """

    def emit(self, spec: MetricSpec, value: float, *, detail: str | None = None) -> None:
        """Discard the datapoint."""


class EmfMetricSink:
    """Writes one CloudWatch EMF document per datapoint, as a single line on its own stream.

    The stream defaults to ``sys.stdout`` and is resolved on **every** write rather than captured in
    the constructor, so a sink built at startup still follows a stream that was replaced afterwards -
    which is what `contextlib.redirect_stdout` and pytest's capture both do.

    Writes are serialised with a lock because a metric line is only a metric if it arrives whole: two
    threads writing partial lines into the same stream would produce JSON that CloudWatch drops. The
    lock is held for one ``write`` plus one ``flush`` and never across anything that can block for
    long.
    """

    def __init__(
        self,
        *,
        env: str,
        client_id: str | None = None,
        namespace: str = NAMESPACE,
        stream: TextIO | None = None,
    ) -> None:
        """``env`` and ``client_id`` fill :data:`COARSE_DIMENSIONS` for every datapoint."""
        self._namespace = namespace
        self._dimensions: dict[str, str] = {
            "Env": _dimension_value(env) or "unknown",
            "ClientId": _dimension_value(client_id) or UNSET_CLIENT_ID,
        }
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, spec: MetricSpec, value: float, *, detail: str | None = None) -> None:
        """Write the datapoint. Raises on a bad spec or a broken stream; `record_*` catches."""
        line = json.dumps(self.document(spec, value, detail=detail), separators=(",", ":"))
        stream = sys.stdout if self._stream is None else self._stream
        with self._lock:
            stream.write(line + "\n")
            stream.flush()

    def document(self, spec: MetricSpec, value: float, *, detail: str | None = None) -> dict[str, Any]:
        """The EMF document for one datapoint, as a plain dictionary.

        Split out from :meth:`emit` so a test can assert the shape without a stream, and so a caller
        that already has somewhere to put a JSON object does not have to parse a line back.
        """
        if not spec.name or len(spec.name) > MAX_METRIC_NAME_LENGTH:
            raise ValueError(f"metric name must be 1..{MAX_METRIC_NAME_LENGTH} characters")
        body: dict[str, Any] = dict(self._dimensions)
        sets: list[Sequence[str]] = [list(COARSE_DIMENSIONS)]
        detail_value = _dimension_value(detail)
        if spec.detail_dimension is not None and detail_value:
            body[spec.detail_dimension] = detail_value
            sets.append([*COARSE_DIMENSIONS, spec.detail_dimension])
        for dimension_set in sets:
            if len(dimension_set) > MAX_DIMENSIONS_PER_METRIC:
                raise ValueError(f"a metric may carry at most {MAX_DIMENSIONS_PER_METRIC} dimensions")
        body[spec.name] = float(value)
        body["_aws"] = {
            "Timestamp": int(utc_now().timestamp() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": self._namespace,
                    "Dimensions": [list(dimension_set) for dimension_set in sets],
                    "Metrics": [{"Name": spec.name, "Unit": spec.unit.value}],
                }
            ],
        }
        return body


def metric_sink_for(settings: Settings) -> MetricSink:
    """The sink `Settings.metrics_backend` selects: `NullMetricSink` unless it says ``emf``.

    The default is the one that does nothing, which is the safe direction for the mistake to fall:
    a deployment that forgets to configure metrics loses a dashboard, whereas one that emits by
    default would put lines nobody asked for into every laptop's terminal.
    """
    if settings.metrics_backend != "emf":
        return NullMetricSink()
    return EmfMetricSink(env=settings.env, client_id=settings.client_id)


def record_run_started(sink: MetricSink, *, use_case_id: str) -> None:
    """One `RunsStarted`, dimensioned by use case."""
    _emit(sink, RUNS_STARTED, 1.0, detail=use_case_id)


def record_run_failed(sink: MetricSink, *, use_case_id: str) -> None:
    """One `RunsFailed`, dimensioned by use case.

    A cancelled run is not a failed one and must not be recorded here: an operator cancelling a run
    is the system working, and counting it would make the alarm on this metric untrustworthy.
    """
    _emit(sink, RUNS_FAILED, 1.0, detail=use_case_id)


def record_stage_duration(sink: MetricSink, *, stage: str, seconds: float) -> None:
    """One `StageDurationSeconds`, dimensioned by stage - the number `log_stage` already has."""
    _emit(sink, STAGE_DURATION_SECONDS, float(seconds), detail=stage)


def record_job_cost(sink: MetricSink, estimate: CostEstimate | None, *, backend: str) -> None:
    """One `JobCostUsd`, **only** when a real figure exists.

    `CostEstimate.estimated_usd` is null whenever the number cannot be stated honestly: a local run,
    no billable time reported, no published rate for that instance in that region. Emitting a zero
    in any of those cases would put a point on a cost graph that means "we do not know" and looks
    exactly like "it was free", so nothing is emitted at all (plan section 13.3, DEC-388).

    A zero that did arrive from a real computation is dropped too. It can only come from zero
    billable seconds, which is not a cost worth a datapoint, and the rule "this graph never shows a
    zero" is worth more than that datapoint.
    """
    if estimate is None or estimate.estimated_usd is None or estimate.estimated_usd <= 0.0:
        return
    _emit(sink, JOB_COST_USD, float(estimate.estimated_usd), detail=backend)


def record_llm_cost(sink: MetricSink, *, model_id: str, usd: float | None, enabled: bool = False) -> None:
    """One `LlmCostUsd` - the hook for a generative phase that is **not in this repository**.

    It is off by default and reads nothing: there is no price table for a model here, no client that
    calls one and no file to consult, so this function will not invent any of them. When the
    generative phase arrives it passes ``enabled=settings.bedrock_enabled`` and a figure it computed
    itself; until then every call is a no-op and the metric has no data, which is the truth
    (DEC-389).
    """
    if not enabled or usd is None or usd <= 0.0:
        return
    _emit(sink, LLM_COST_USD, float(usd), detail=model_id)


def _emit(sink: MetricSink, spec: MetricSpec, value: float, *, detail: str | None = None) -> None:
    """Emit, and swallow whatever the sink raises: a metric may never fail a run (DEC-387).

    One guard, in one place, covering every sink and every metric - rather than a `try` at each of
    the five call sites, where the fifth would eventually be written without one. The failure is
    logged by the exception's class name only, like every other failure here (plan section 13.7).
    """
    try:
        sink.emit(spec, value, detail=detail)
    except Exception as exc:
        log_failure(_LOGGER, f"metrics.emit_failed metric={spec.name}", exc)


def _dimension_value(value: str | None) -> str:
    """A dimension value CloudWatch will accept, or ``""`` for one it would reject.

    Truncation at :data:`MAX_DIMENSION_VALUE_LENGTH` is this module's policy; the length itself is
    the service limit. A blank value is returned as ``""`` so the caller drops the dimension:
    CloudWatch requires at least one non-whitespace character, and a dimension with an empty value
    would take the whole datapoint down with it.
    """
    if value is None:
        return ""
    return value.strip()[:MAX_DIMENSION_VALUE_LENGTH]
