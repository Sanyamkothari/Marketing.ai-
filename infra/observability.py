"""Log groups, one alarm topic, the metric filters - and nothing this stack decided by itself.

This stack deliberately contains **no widget list and no alarm threshold**. Both are somebody
else's output: `infra/observability/dashboard.json` and `infra/observability/alarms.json` are
generated beside this module, and this stack renders them if they are there. A threshold typed into
a CDK file is a number that looks measured because it is in code; leaving the numbers in a generated
file keeps the question "where did 0.95 come from" answerable (DEC-374).

What is genuinely this stack's:

* **the log groups**, because they must exist with a retention *before* anything writes to them.
  A group created implicitly by ECS or by RDS is created with "Never expire", and nobody notices
  until the log bill does. This is also why the RDS group is created here under the name RDS will
  export to, and why this stack is ordered ahead of the database stack: whoever creates the group
  first decides its retention.
* **the topic**, because an alarm with nowhere to go is a colour in a console.
* **the metric filters**, because a filter's pattern is a statement about the log *schema* -
  `log_format=json` means `engine/utils/logging.py` writes one JSON object per line - not a
  statement about how often something should happen. The pattern is a fact; the threshold is a
  judgement.

If neither JSON file exists the stack still deploys, and an operator gets log groups, a topic and
two custom metrics with no alarms on them. That is a smaller thing than was intended but it is not
a broken thing, and it is what makes the two sides independently deployable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

from aws_cdk import Annotations, CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from infra.context import AppContext
from infra.naming import (
    METRIC_NAMESPACE,
    PRODUCT,
    api_log_group_name,
    jobs_log_group_name,
    log_retention,
    rds_log_group_name,
)

__all__ = [
    "ALARMS_FILE",
    "DASHBOARD_FILE",
    "GENERATED_DIR",
    "METRIC_FILTERS",
    "STANDARD_PLACEHOLDERS",
    "UNSET_CLIENT_ID",
    "ObservabilityStack",
]

GENERATED_DIR: Final[Path] = Path(__file__).resolve().parent / "observability"
"""Where the generated dashboard and alarm definitions are written by their owner."""

DASHBOARD_FILE: Final[Path] = GENERATED_DIR / "dashboard.json"
ALARMS_FILE: Final[Path] = GENERATED_DIR / "alarms.json"

METRIC_FILTERS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "Errors",
        '{ $.level = "ERROR" }',
        "One data point per ERROR line the API or a job wrote.",
    ),
    (
        "RunFailures",
        '{ $.event = "run_failed" }',
        "One data point per run that ended in a coded failure.",
    ),
)
"""(metric name, filter pattern, why) for each filter applied to the API and jobs log groups.

Each pattern names a field `engine/utils/logging.py` writes when `log_format=json`. None of them
carries a threshold: a filter counts, an alarm judges, and the judging is done elsewhere.
"""

UNSET_CLIENT_ID: Final[str] = "unset"
"""Mirrors `engine.aws.metrics.UNSET_CLIENT_ID`: what fills the ClientId dimension when there is none.

Duplicated rather than imported, because `infra/` must stay importable from a checkout that has only
the deploy extra. `tests/infra/test_settings_contract.py` asserts the two are equal.
"""

STANDARD_PLACEHOLDERS: Final[tuple[str, ...]] = ("${Env}", "${ClientId}")
"""Substitutions this stack can always fill, whatever the deployment.

`scripts/gen_dashboard` writes `${Env}` and `${ClientId}` into both generated files because the
dimension values are a property of the deployment, not of the metric registry. Anything else the
generator leaves as a `${...}` is a number it refused to invent, and is filled from
`-c alarm_thresholds=Name=value` or not at all.
"""


class ObservabilityStack(Stack):
    """Log groups with a retention, an alarm topic, metric filters, and whatever was generated."""

    def __init__(
        self, scope: Construct, construct_id: str, *, context: AppContext, key: kms.IKey, **kwargs: object
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        self.context = context
        retention = log_retention(context.log_retention_days)
        removal = RemovalPolicy.DESTROY if context.removal_policy_destroy else RemovalPolicy.RETAIN

        self.api_log_group = logs.LogGroup(
            self,
            "ApiLogs",
            log_group_name=api_log_group_name(context.env_name),
            retention=retention,
            removal_policy=removal,
        )
        self.jobs_log_group = logs.LogGroup(
            self,
            "JobLogs",
            log_group_name=jobs_log_group_name(context.env_name),
            retention=retention,
            removal_policy=removal,
        )
        # RDS will create this group itself, without a retention, the first time it exports a log.
        # Creating it here - with the exact name RDS derives from the instance identifier, and
        # before the database stack is deployed - is the only way to decide the retention.
        self.database_log_group = logs.LogGroup(
            self,
            "DatabaseLogs",
            log_group_name=rds_log_group_name(context.env_name),
            retention=retention,
            removal_policy=removal,
        )

        self.alarm_topic = sns.Topic(
            self,
            "Alarms",
            topic_name=f"{PRODUCT}-{context.env_name}-alarms",
            display_name=f"{PRODUCT} {context.env_name} alarms",
            master_key=key,
            enforce_ssl=True,
        )
        if context.alert_email:
            self.alarm_topic.add_subscription(subscriptions.EmailSubscription(context.alert_email))
        else:
            Annotations.of(self).add_warning(
                "No alert_email: this deployment raises alarms that nobody is told about. "
                "Pass -c alert_email=<address> and confirm the subscription."
            )

        for group, label in ((self.api_log_group, "Api"), (self.jobs_log_group, "Jobs")):
            for metric_name, pattern, _why in METRIC_FILTERS:
                logs.MetricFilter(
                    self,
                    f"{label}{metric_name}Filter",
                    log_group=group,
                    filter_pattern=logs.FilterPattern.literal(pattern),
                    metric_namespace=METRIC_NAMESPACE,
                    metric_name=f"{label}{metric_name}",
                    metric_value="1",
                    default_value=0,
                )

        self.missing_thresholds: list[str] = []
        self.alarms = self._alarms_from_file()
        self._dashboard_from_file()

        CfnOutput(self, "AlarmTopicArn", value=self.alarm_topic.topic_arn, description="Alarm topic")
        CfnOutput(
            self,
            "GeneratedArtefacts",
            value=(
                f"dashboard={'present' if DASHBOARD_FILE.exists() else 'absent'}; "
                f"alarms={len(self.alarms)}; "
                f"alarms without a threshold={','.join(self.missing_thresholds) or 'none'}"
            ),
            description="What infra/observability/*.json contributed to this stack",
        )

    # ------------------------------------------------------------------
    def _alarms_from_file(self) -> list[cloudwatch.CfnAlarm]:
        """Every alarm in `alarms.json` that this deployment can actually give a threshold.

        `CfnAlarm` rather than the L2 `Alarm` on purpose: the file describes a metric by namespace,
        name and dimensions, which is exactly the L1 shape. Going through the L2 would mean this
        module reconstructing `Metric` objects, and reconstructing is where a default creeps in.
        """
        document = _read_json(ALARMS_FILE)
        if document is None:
            Annotations.of(self).add_warning(
                f"{ALARMS_FILE.name} is absent: this deployment has an alarm topic and no alarms "
                "on it. Generate it with `python -m scripts.gen_dashboard`."
            )
            return []
        raw = document.get("alarms")
        if not isinstance(raw, list):
            raise ValueError(f"{ALARMS_FILE} must be a JSON object with an 'alarms' array.")
        built: list[cloudwatch.CfnAlarm] = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ValueError(f"{ALARMS_FILE}: alarms[{index}] must be an object.")
            alarm = self._alarm(index, entry)
            if alarm is not None:
                built.append(alarm)
        return built

    def _alarm(self, index: int, entry: dict[str, Any]) -> cloudwatch.CfnAlarm | None:
        name = str(_required(entry, "name", ALARMS_FILE, index))
        raw_threshold = _required(entry, "threshold", ALARMS_FILE, index)
        threshold = self._threshold(name, raw_threshold)
        if threshold is None:
            return None
        dimensions = entry.get("dimensions") or {}
        if not isinstance(dimensions, dict):
            raise ValueError(f"{ALARMS_FILE}: alarms[{index}].dimensions must be an object.")
        return cloudwatch.CfnAlarm(
            self,
            f"Alarm{_construct_id(name)}",
            alarm_name=self._fill(name),
            # The generator's `rationale` is the reason the alarm exists and the reason its
            # thresholds are what they are. It is carried into the alarm's own description so the
            # person woken by it reads the argument, not just the number.
            alarm_description=str(entry.get("rationale", "")) or None,
            namespace=str(_required(entry, "namespace", ALARMS_FILE, index)),
            metric_name=str(_required(entry, "metric_name", ALARMS_FILE, index)),
            dimensions=[
                cloudwatch.CfnAlarm.DimensionProperty(name=str(key), value=self._fill(str(value)))
                for key, value in sorted(dimensions.items())
            ]
            or None,
            statistic=str(entry.get("statistic", "Sum")),
            period=int(entry.get("period_seconds", 300)),
            evaluation_periods=int(_required(entry, "evaluation_periods", ALARMS_FILE, index)),
            datapoints_to_alarm=(
                int(entry["datapoints_to_alarm"]) if entry.get("datapoints_to_alarm") else None
            ),
            threshold=threshold,
            comparison_operator=str(entry.get("comparison_operator", "GreaterThanThreshold")),
            treat_missing_data=str(entry.get("treat_missing_data", "notBreaching")),
            actions_enabled=True,
            alarm_actions=[self.alarm_topic.topic_arn],
            ok_actions=[self.alarm_topic.topic_arn],
        )

    def _threshold(self, alarm_name: str, raw: object) -> float | None:
        """A number, or `None` when the deployment has not supplied the substitution.

        This is the whole point of the arrangement. `alarms.json` writes
        `"threshold": "${StageDurationSecondsAlarmThresholdSeconds}"` and says in its `rationale`
        that nobody has measured it. The honest answers are "the operator tells us" and "there is
        no alarm" - and picking a plausible number would be neither.
        """
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        text = str(raw)
        if not text.startswith("${"):
            return float(text)
        key = text[2:-1]
        supplied = self.context.alarm_threshold_values.get(key)
        if supplied is None:
            self.missing_thresholds.append(key)
            Annotations.of(self).add_warning(
                f"Alarm {alarm_name!r} is NOT created: its threshold is the substitution "
                f"${{{key}}}, which nothing has measured and this stack will not invent. "
                f"Create it with -c alarm_thresholds={key}=<value>."
            )
            return None
        try:
            return float(supplied)
        except ValueError:
            raise ValueError(f"alarm_thresholds: {key}={supplied!r} is not a number.") from None

    def _dashboard_from_file(self) -> cloudwatch.CfnDashboard | None:
        """The generated dashboard body, verbatim apart from the `${...}` substitutions."""
        document = _read_json(DASHBOARD_FILE)
        if document is None:
            return None
        if "widgets" not in document:
            raise ValueError(f"{DASHBOARD_FILE} must be a CloudWatch dashboard body with a 'widgets' array.")
        body = self._fill(json.dumps({"widgets": document["widgets"]}))
        leftover = sorted(set(_PLACEHOLDER_PATTERN.findall(body)))
        if leftover:
            raise ValueError(
                f"{DASHBOARD_FILE} still contains {', '.join(leftover)} after substitution. "
                f"This stack fills {', '.join(STANDARD_PLACEHOLDERS)} and whatever "
                "-c alarm_thresholds supplies; a dashboard cannot render an unfilled one."
            )
        return cloudwatch.CfnDashboard(
            self,
            "Dashboard",
            dashboard_name=f"{PRODUCT}-{self.context.env_name}",
            dashboard_body=body,
        )

    def _fill(self, text: str) -> str:
        """`${Env}`, `${ClientId}` and any supplied alarm threshold, substituted into `text`."""
        filled = text.replace("${Env}", self.context.env_name).replace(
            "${ClientId}", self.context.client_id or UNSET_CLIENT_ID
        )
        for key, value in self.context.alarm_threshold_values.items():
            filled = filled.replace("${" + key + "}", value)
        return filled


def _read_json(path: Path) -> dict[str, Any] | None:
    """The file as a JSON object, or `None` when it is not there. Malformed is an error, not absent."""
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc.msg} at line {exc.lineno}.") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return parsed


def _required(entry: dict[str, Any], key: str, path: Path, index: int) -> Any:
    if key not in entry or entry[key] in (None, ""):
        raise ValueError(f"{path}: alarms[{index}] is missing {key!r}.")
    return entry[key]


_PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\$\{[A-Za-z0-9_]+\}")
"""What an unfilled substitution looks like once the known ones have been replaced."""


def _construct_id(alarm_name: str) -> str:
    """`marketing-ai-${Env}-runs-failed` -> `RunsFailed`; a construct id is alphanumeric.

    The `${...}` parts are dropped rather than substituted, so the logical id of an alarm does not
    change when the deployment is renamed - a changed logical id would replace the alarm.
    """
    cleaned = _PLACEHOLDER_PATTERN.sub("", alarm_name).replace("_", "-")
    parts = [part for part in cleaned.split("-") if part and part != PRODUCT.split("-")[0]]
    return "".join(part.capitalize() for part in parts) or "Alarm"
