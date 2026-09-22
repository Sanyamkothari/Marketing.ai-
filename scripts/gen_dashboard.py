"""Generate `infra/observability/dashboard.json` and `infra/observability/alarms.json`.

Both files are generated from `engine.aws.metrics` - its namespace, its `MetricSpec`s and its
dimension names - and are **committed**, exactly like the upload templates (DEC-014). The reason is
the same one: a generated artefact that is not committed is an artefact nobody reviews, and one
that is committed but not checked drifts. `--check` writes nothing and exits 1 on drift, which is
what `make check-generated` and `tests/unit/test_gen_dashboard.py` run.

Generating rather than hand-writing buys one specific thing: **a metric cannot be renamed without
the dashboard and the alarms following it**, because there is no second list of metric names
anywhere. Rename one in `engine/aws/metrics.py`, run `make generate`, and the diff shows every
place it was named (DEC-391).

**Both files carry substitution placeholders**, spelled `${Name}` so that CDK's `Fn.sub` - or
Python's `string.Template` - can fill them at deploy time. Two kinds, and the difference matters:

*Deployment identity*: ``${Env}`` and ``${ClientId}`` are the two coarse dimensions every metric
carries. They are not knowable here; they are knowable in the stack that deploys the dashboard.

*Thresholds nobody has measured*: ``${StageDurationSecondsAlarmThresholdSeconds}`` and
``${JobCostUsdDailyBudgetUsd}``. How long a stage *should* take and how much a day *should* cost are
a measurement and a business decision respectively, and this repository has neither. Committing a
number for them would be inventing it, so they are left as substitutions that a deployment has to
supply deliberately (plan section 13.3, DEC-392). Every alarm says which kind its threshold is in
its own ``threshold_source`` field, and every alarm carries a ``rationale`` saying in words that its
statistic, period and evaluation count are **policy chosen before any traffic exists** (DEC-393).
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from engine.aws.metrics import COARSE_DIMENSIONS, METRICS, NAMESPACE, MetricSpec, Unit

if TYPE_CHECKING:
    from collections.abc import Sequence

COMMAND: str = "python -m scripts.gen_dashboard"
DEFAULT_OUT: Path = Path("infra/observability")

DASHBOARD_FILENAME: Final[str] = "dashboard.json"
ALARMS_FILENAME: Final[str] = "alarms.json"

ENV_PLACEHOLDER: Final[str] = "${Env}"
CLIENT_ID_PLACEHOLDER: Final[str] = "${ClientId}"
STAGE_DURATION_THRESHOLD_PLACEHOLDER: Final[str] = "${StageDurationSecondsAlarmThresholdSeconds}"
JOB_COST_BUDGET_PLACEHOLDER: Final[str] = "${JobCostUsdDailyBudgetUsd}"

PLACEHOLDERS: Final[dict[str, str]] = {
    ENV_PLACEHOLDER: "Settings.env of the deployment this dashboard watches.",
    CLIENT_ID_PLACEHOLDER: (
        "Settings.client_id, or the literal engine.aws.metrics.UNSET_CLIENT_ID when the deployment"
        " does not name a client."
    ),
    STAGE_DURATION_THRESHOLD_PLACEHOLDER: (
        "Seconds a stage may take before the alarm fires. No default is committed: nothing in this"
        " repository has measured a stage against real data, and an invented number would read like"
        " a measurement (plan section 13.3)."
    ),
    JOB_COST_BUDGET_PLACEHOLDER: (
        "Dollars of compute per day before the alarm fires. No default is committed: a budget is a"
        " business decision, and nobody here has been told what it is."
    ),
}
"""Every `${...}` token the two generated files contain, and what a deployment must put in it.

Exhaustive on purpose. `Fn.sub` fails on a token it was not given a value for, so an incomplete
list here is a stack that does not synthesise - which is the right failure, but only if the list is
the thing the stack is built from.
"""

WIDGET_WIDTH: Final[int] = 12
WIDGET_HEIGHT: Final[int] = 6
"""Half of a CloudWatch dashboard's 24-column grid, so metrics read two to a row. A layout choice."""

DASHBOARD_PERIOD_SECONDS: Final[int] = 300
"""Resolution the dashboard's widgets aggregate at. Chosen, not measured: five minutes is short
enough to see a bad afternoon and long enough that a low-traffic graph is not mostly gaps."""

_COUNT_STATISTIC: Final[str] = "Sum"
_DURATION_STATISTIC: Final[str] = "Average"
_COST_STATISTIC: Final[str] = "Sum"


def statistic_for(spec: MetricSpec) -> str:
    """How a metric is aggregated: counts and money add up, a duration averages.

    A judgement, not a measurement. Summing durations would produce a number with no meaning (the
    total time all stages spent across all runs), and averaging counts would hide volume.
    """
    if spec.unit is Unit.COUNT:
        return _COUNT_STATISTIC
    if spec.unit is Unit.SECONDS:
        return _DURATION_STATISTIC
    return _COST_STATISTIC


def _coarse_metric_reference(spec: MetricSpec) -> list[str]:
    """One metric in a dashboard widget, addressed by the coarse dimension set.

    The coarse set - not the detail one - because `engine.aws.metrics` publishes every datapoint
    under both, and the coarse set is the one that does not need every use case, stage and backend
    named here by hand.
    """
    values = {"Env": ENV_PLACEHOLDER, "ClientId": CLIENT_ID_PLACEHOLDER}
    reference = [NAMESPACE, spec.name]
    for dimension in COARSE_DIMENSIONS:
        reference.extend((dimension, values[dimension]))
    return reference


def render_dashboard() -> str:
    """The CloudWatch dashboard body, as the JSON text `PutDashboard` takes.

    The body is only ``{"widgets": [...]}``: `PutDashboard` validates the document, so the
    provenance note that every other generated file puts in a comment goes in a text widget here
    rather than in a key CloudWatch has never heard of.
    """
    widgets: list[dict[str, Any]] = [
        {
            "type": "text",
            "x": 0,
            "y": 0,
            "width": 24,
            "height": 2,
            "properties": {
                "markdown": (
                    f"## Marketing AI - namespace `{NAMESPACE}`\n"
                    f"Generated by `{COMMAND}` from `engine/aws/metrics.py`. "
                    "Edits made in the console are overwritten on the next deploy; "
                    "change the metric registry instead."
                )
            },
        }
    ]
    for index, spec in enumerate(METRICS):
        row, column = divmod(index, 2)
        title = spec.name if spec.alarmable else f"{spec.name} (reserved; no data yet)"
        widgets.append(
            {
                "type": "metric",
                "x": column * WIDGET_WIDTH,
                "y": 2 + row * WIDGET_HEIGHT,
                "width": WIDGET_WIDTH,
                "height": WIDGET_HEIGHT,
                "properties": {
                    "metrics": [_coarse_metric_reference(spec)],
                    "view": "timeSeries",
                    "stacked": False,
                    "stat": statistic_for(spec),
                    "period": DASHBOARD_PERIOD_SECONDS,
                    "title": title,
                    "yAxis": {"left": {"label": spec.unit.value, "showUnits": False}},
                },
            }
        )
    return _as_json({"widgets": widgets})


def alarm_definitions() -> list[dict[str, Any]]:
    """Every alarm, each one naming a metric in `engine.aws.metrics` and its own rationale.

    Not generated from the registry alone, because an alarm is a *policy* and a policy has to be
    written by someone: the registry supplies the names, the units and which metrics are alarmable,
    and each entry below supplies the judgement. `LlmCostUsd` has ``alarmable=False`` and therefore
    gets no alarm - alarming on a metric that nothing emits would produce either permanent
    INSUFFICIENT_DATA or, with the wrong missing-data treatment, a permanent false alarm.
    """
    policy = "POLICY, chosen before this product has served any traffic."
    return [
        {
            "name": "marketing-ai-${Env}-runs-failed",
            "metric_name": "RunsFailed",
            "namespace": NAMESPACE,
            "dimensions": {"Env": ENV_PLACEHOLDER, "ClientId": CLIENT_ID_PLACEHOLDER},
            "statistic": "Sum",
            "period_seconds": 300,
            "evaluation_periods": 1,
            "datapoints_to_alarm": 1,
            "comparison_operator": "GreaterThanOrEqualToThreshold",
            "threshold": 1,
            "threshold_source": "literal",
            "treat_missing_data": "notBreaching",
            "rationale": (
                f"{policy} The threshold is 1 because a failed run is an event, not a rate: with no"
                " history there is no rate to compare it against, and the only statement anyone can"
                " defend today is 'say something the first time this happens'. One period of five"
                " minutes and one datapoint follow from that - waiting for a trend would be waiting"
                " for a trend nobody has measured. Missing data is not breaching: a deployment with"
                " no runs is idle, not broken."
            ),
        },
        {
            "name": "marketing-ai-${Env}-stage-duration",
            "metric_name": "StageDurationSeconds",
            "namespace": NAMESPACE,
            "dimensions": {"Env": ENV_PLACEHOLDER, "ClientId": CLIENT_ID_PLACEHOLDER},
            "statistic": "Average",
            "period_seconds": 300,
            "evaluation_periods": 3,
            "datapoints_to_alarm": 3,
            "comparison_operator": "GreaterThanThreshold",
            "threshold": STAGE_DURATION_THRESHOLD_PLACEHOLDER,
            "threshold_source": "substitution",
            "treat_missing_data": "notBreaching",
            "rationale": (
                "The threshold is deliberately NOT committed: how long a stage should take is a"
                " measurement, this repository has never run one against real customer data, and a"
                " number written here would be read as though it had been (plan section 13.3). The"
                f" deployment supplies it. Everything else is {policy} Three of three periods,"
                " because one slow stage is a big dataset and three in a row is a problem."
            ),
        },
        {
            "name": "marketing-ai-${Env}-job-cost",
            "metric_name": "JobCostUsd",
            "namespace": NAMESPACE,
            "dimensions": {"Env": ENV_PLACEHOLDER, "ClientId": CLIENT_ID_PLACEHOLDER},
            "statistic": "Sum",
            "period_seconds": 86400,
            "evaluation_periods": 1,
            "datapoints_to_alarm": 1,
            "comparison_operator": "GreaterThanThreshold",
            "threshold": JOB_COST_BUDGET_PLACEHOLDER,
            "threshold_source": "substitution",
            "treat_missing_data": "notBreaching",
            "rationale": (
                "The threshold is a budget, which is a business decision nobody has stated to this"
                " repository, so it is left as a substitution rather than invented. The one-day"
                f" period is {policy} a budget is stated per day, so the alarm evaluates per day."
                " Note that the metric is derived from AWS *list* prices, never from a bill, so"
                " this alarm is a tripwire on estimated spend and not on invoiced spend."
            ),
        },
    ]


def render_alarms() -> str:
    """The alarm definitions, as JSON text for whatever stack turns them into CloudWatch alarms.

    This is not an AWS document format - there is no "alarms.json" the API takes - so it carries its
    own provenance, the namespace, and the placeholder table, and its ``alarms`` entries name the
    fields `PutMetricAlarm` and CDK's `Alarm` both take.
    """
    alarms = alarm_definitions()
    known = {spec.name for spec in METRICS}
    unknown = sorted({alarm["metric_name"] for alarm in alarms} - known)
    if unknown:
        raise ValueError(f"alarm names a metric that is not in engine.aws.metrics: {unknown}")
    return _as_json(
        {
            "generated_by": COMMAND,
            "namespace": NAMESPACE,
            "placeholders": PLACEHOLDERS,
            "alarms": alarms,
        }
    )


def _as_json(document: dict[str, Any]) -> str:
    """Two-space JSON with a trailing newline, so a diff of a generated file reads like a diff."""
    return json.dumps(document, indent=2) + "\n"


def write_observability(out_dir: Path, *, check: bool = False) -> int:
    """Render both files into `out_dir`; return the process exit code.

    `check=False` writes them (creating `out_dir`) and returns 0. `check=True` writes nothing, prints
    a unified diff per differing or missing file and returns 1 when anything differs, else 0. The
    shape is `scripts/gen_templates.py`'s, deliberately: two generators that behave differently under
    `--check` would need two explanations in the Makefile.
    """
    rendered: list[tuple[Path, str]] = [
        (out_dir / DASHBOARD_FILENAME, render_dashboard()),
        (out_dir / ALARMS_FILENAME, render_alarms()),
    ]
    if not check:
        out_dir.mkdir(parents=True, exist_ok=True)
        for path, body in rendered:
            path.write_text(body, encoding="utf-8")
        return 0
    drifted = 0
    for path, body in rendered:
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if current == body:
            continue
        drifted = 1
        sys.stdout.writelines(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                body.splitlines(keepends=True),
                fromfile=str(path),
                tofile=f"{path} (generated by {COMMAND})",
            )
        )
    return drifted


def main(argv: Sequence[str] | None = None) -> int:
    """`--out infra/observability/` (default) and `--check`; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description="Generate the CloudWatch dashboard and alarm definitions from engine.aws.metrics.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="directory to write (default: infra/observability/)",
    )
    parser.add_argument("--check", action="store_true", help="write nothing; diff and exit 1 on drift")
    args = parser.parse_args(argv)
    out_dir: Path = args.out
    return write_observability(out_dir, check=args.check)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
