"""The observability stack contributes structure and reads the numbers from somewhere else.

The strongest assertion here is the negative one: no threshold that the generated `alarms.json`
left as a `${...}` substitution is invented by this stack. An alarm with no supplied threshold is
not created at all, and the synthesis says so.
"""

from __future__ import annotations

import json
import re

from aws_cdk.assertions import Template
from infra.app import build_app
from infra.context import AppContext
from infra.naming import api_log_group_name, jobs_log_group_name, rds_log_group_name
from infra.observability import ALARMS_FILE, DASHBOARD_FILE, METRIC_FILTERS, UNSET_CLIENT_ID

from tests.infra.conftest import ACCOUNT, rendered, resources, templates_of


def test_the_three_log_groups_exist_with_a_retention(dev_templates: dict[str, Template]) -> None:
    names = {
        resource["Properties"]["LogGroupName"]
        for resource in resources(dev_templates["observability"], "AWS::Logs::LogGroup").values()
    }
    assert names == {
        api_log_group_name("dev"),
        jobs_log_group_name("dev"),
        rds_log_group_name("dev"),
    }


def test_the_rds_group_is_created_before_rds_can_create_it_without_one(
    dev_templates: dict[str, Template],
) -> None:
    """RDS creates that group itself, with no retention, the first time it exports a log."""
    groups = resources(dev_templates["observability"], "AWS::Logs::LogGroup")
    rds_groups = [
        resource
        for resource in groups.values()
        if resource["Properties"]["LogGroupName"] == rds_log_group_name("dev")
    ]
    assert rds_groups
    assert rds_groups[0]["Properties"]["RetentionInDays"] == 30


def test_the_alarm_topic_is_encrypted(dev_templates: dict[str, Template]) -> None:
    topics = resources(dev_templates["observability"], "AWS::SNS::Topic")
    assert len(topics) == 1
    assert next(iter(topics.values()))["Properties"]["KmsMasterKeyId"]


def test_an_alert_email_is_subscribed_when_one_is_given(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    assert not resources(dev_templates["observability"], "AWS::SNS::Subscription")
    subscriptions = resources(prod_templates["observability"], "AWS::SNS::Subscription")
    assert len(subscriptions) == 1
    properties = next(iter(subscriptions.values()))["Properties"]
    assert properties["Protocol"] == "email"
    assert properties["Endpoint"] == "ops@example.com"


def test_the_metric_filters_match_the_json_log_schema(dev_templates: dict[str, Template]) -> None:
    filters = resources(dev_templates["observability"], "AWS::Logs::MetricFilter")
    assert len(filters) == 2 * len(METRIC_FILTERS)
    patterns = {resource["Properties"]["FilterPattern"] for resource in filters.values()}
    assert patterns == {pattern for _name, pattern, _why in METRIC_FILTERS}
    for resource in filters.values():
        for transformation in resource["Properties"]["MetricTransformations"]:
            assert transformation["MetricNamespace"] == "MarketingAI"
            assert "Threshold" not in rendered(transformation)


def test_no_alarm_is_created_for_a_threshold_nobody_supplied(
    dev_templates: dict[str, Template],
) -> None:
    document = json.loads(ALARMS_FILE.read_text(encoding="utf-8"))
    substituted = [entry["name"] for entry in document["alarms"] if str(entry["threshold"]).startswith("${")]
    assert substituted, "the generated file no longer leaves a threshold to the deployment"
    alarms = resources(dev_templates["observability"], "AWS::CloudWatch::Alarm")
    created = {resource["Properties"]["AlarmName"] for resource in alarms.values()}
    for name in substituted:
        assert name.replace("${Env}", "dev") not in created


def test_supplying_a_threshold_creates_the_alarm() -> None:
    document = json.loads(ALARMS_FILE.read_text(encoding="utf-8"))
    placeholders = {
        str(entry["threshold"])[2:-1]
        for entry in document["alarms"]
        if str(entry["threshold"]).startswith("${")
    }
    supplied = ",".join(f"{name}=1" for name in sorted(placeholders))
    deployment = build_app(
        AppContext.from_mapping({"env_name": "dev", "alarm_thresholds": supplied}),
        account=ACCOUNT,
    )
    template = templates_of(deployment)["observability"]
    alarms = resources(template, "AWS::CloudWatch::Alarm")
    assert len(alarms) == len(document["alarms"])
    for resource in alarms.values():
        assert resource["Properties"]["AlarmActions"], "an alarm with nowhere to go"
        assert resource["Properties"]["AlarmDescription"], "the generator's rationale is carried over"


def test_every_alarm_threshold_came_from_the_generated_file_or_the_operator(
    dev_templates: dict[str, Template],
) -> None:
    """The stack contributes no number of its own; every one is traceable (DEC-374).

    Each synthesised alarm's threshold has to be findable in `alarms.json` as a committed literal.
    A threshold that appears in the template and nowhere in the generated file would be one this
    stack invented, and a number in a CDK file reads as measured because it is in code.
    """
    document = json.loads(ALARMS_FILE.read_text(encoding="utf-8"))
    committed = {
        float(entry["threshold"])
        for entry in document["alarms"]
        if not str(entry["threshold"]).startswith("${")
    }
    alarms = resources(dev_templates["observability"], "AWS::CloudWatch::Alarm")
    assert alarms, "no alarm at all - has the generated file stopped being read?"
    for resource in alarms.values():
        assert float(resource["Properties"]["Threshold"]) in committed


def test_the_stack_reports_which_alarms_it_refused_to_invent(
    dev_templates: dict[str, Template],
) -> None:
    """Silence would be indistinguishable from the alarm not being wanted."""
    outputs = dev_templates["observability"].to_json()["Outputs"]
    value = rendered(outputs["GeneratedArtefacts"]["Value"])
    assert "alarms without a threshold" in value
    assert "none" not in value.split("alarms without a threshold")[-1]


def test_the_dashboard_is_rendered_with_every_placeholder_filled(
    dev_templates: dict[str, Template],
) -> None:
    dashboards = resources(dev_templates["observability"], "AWS::CloudWatch::Dashboard")
    assert len(dashboards) == 1
    body = next(iter(dashboards.values()))["Properties"]["DashboardBody"]
    text = body if isinstance(body, str) else rendered(body)
    assert not re.search(r"\$\{[A-Za-z0-9_]+\}", text), "an unfilled substitution renders as text"
    assert '\\"dev\\"' in text or '"dev"' in text or "dev" in text
    assert UNSET_CLIENT_ID in text


def test_the_dashboard_body_is_the_generated_widgets(dev_templates: dict[str, Template]) -> None:
    generated = json.loads(DASHBOARD_FILE.read_text(encoding="utf-8"))
    dashboards = resources(dev_templates["observability"], "AWS::CloudWatch::Dashboard")
    body = next(iter(dashboards.values()))["Properties"]["DashboardBody"]
    text = body if isinstance(body, str) else rendered(body)
    assert text.count('"type"') == json.dumps(generated).count('"type"')
