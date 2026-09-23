"""The alert topic, and the DPDP retention job's rights on the artefact bucket.

Two small grants that are easy to get subtly wrong in the generous direction: publishing to the
*alarm* topic instead of the alert topic (which would let the application forge CloudWatch's
messages), and deleting object versions under every prefix instead of the two that hold customer
rows (which would let erasure delete the models it is meant to keep).
"""

from __future__ import annotations

from typing import Any

from aws_cdk.assertions import Template
from infra.naming import MODELS_PREFIX, ROW_LEVEL_PREFIXES

from tests.infra.conftest import as_list, rendered, resources, sole, statements


def _api_statements(templates: dict[str, Template]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for resource in resources(templates["operations"], "AWS::IAM::Policy").values():
        if "TaskRole" in rendered(resource["Properties"].get("Roles")):
            found.extend(
                dict(statement) for statement in resource["Properties"]["PolicyDocument"]["Statement"]
            )
    return found


def _with_action(found: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    return [statement for statement in found if action in as_list(statement.get("Action"))]


def test_the_alert_topic_is_encrypted_with_the_product_key(dev_templates: dict[str, Template]) -> None:
    topic = sole(dev_templates["operations"], "AWS::SNS::Topic")
    assert topic["TopicName"] == "marketing-ai-dev-alerts"
    assert "ArtefactKey" in rendered(topic["KmsMasterKeyId"])


def test_the_alert_topic_refuses_plain_http(dev_templates: dict[str, Template]) -> None:
    policy = sole(dev_templates["operations"], "AWS::SNS::TopicPolicy")
    denies = [s for s in policy["PolicyDocument"]["Statement"] if s["Effect"] == "Deny"]
    assert any("aws:SecureTransport" in rendered(s.get("Condition")) for s in denies)


def test_the_alert_email_is_subscribed_when_one_is_given(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    assert not resources(dev_templates["operations"], "AWS::SNS::Subscription")
    subscription = sole(prod_templates["operations"], "AWS::SNS::Subscription")
    assert subscription["Protocol"] == "email"
    assert subscription["Endpoint"] == "ops@example.com"
    assert "Alerts" in rendered(subscription["TopicArn"])


def test_the_api_publishes_to_the_alert_topic_and_to_no_other(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    for templates in (dev_templates, prod_templates):
        publishing = [
            statement
            for template in templates.values()
            for statement in _with_action(statements(template, effect="Allow"), "sns:Publish")
        ]
        assert len(publishing) == 1
        (resource,) = as_list(publishing[0]["Resource"])
        assert "Alerts" in rendered(resource)
        assert "Alarms" not in rendered(resource), "the alarm topic speaks for CloudWatch only"


def test_the_alarm_topic_is_still_the_only_topic_in_observability(dev_templates: dict[str, Template]) -> None:
    assert len(resources(dev_templates["observability"], "AWS::SNS::Topic")) == 1


def test_the_retention_job_may_read_and_replace_the_lifecycle_of_the_artefact_bucket_only(
    dev_templates: dict[str, Template],
) -> None:
    found = _with_action(_api_statements(dev_templates), "s3:PutLifecycleConfiguration")
    assert len(found) == 1
    statement = found[0]
    assert sorted(as_list(statement["Action"])) == [
        "s3:GetLifecycleConfiguration",
        "s3:ListBucketVersions",
        "s3:PutLifecycleConfiguration",
    ]
    (resource,) = as_list(statement["Resource"])
    assert "Artefacts" in rendered(resource)
    assert "/*" not in rendered(resource), "bucket-level, not objects"
    assert "Condition" not in statement


def test_object_versions_may_be_deleted_under_the_row_level_prefixes_only(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    for templates in (dev_templates, prod_templates):
        found = [
            statement
            for template in templates.values()
            for statement in _with_action(statements(template, effect="Allow"), "s3:DeleteObjectVersion")
        ]
        assert len(found) == 1, "one statement in the whole app may delete a version"
        statement = found[0]
        assert as_list(statement["Action"]) == ["s3:DeleteObjectVersion"]
        body = rendered(statement["Resource"])
        for prefix in ROW_LEVEL_PREFIXES:
            assert f"/{prefix}*" in body
        assert f"/{MODELS_PREFIX}" not in body, "models are kept by retention and erasure"
        assert "_bootstrap" not in body
        assert "Artefacts" in body
        assert len(as_list(statement["Resource"])) == len(ROW_LEVEL_PREFIXES)


def test_the_row_level_prefixes_are_a_subset_of_the_artefact_prefixes() -> None:
    from infra.naming import OBJECT_PREFIXES

    assert set(ROW_LEVEL_PREFIXES) < set(OBJECT_PREFIXES)


def test_the_operations_grants_are_one_policy_on_the_task_role(dev_templates: dict[str, Template]) -> None:
    policies = [
        resource
        for resource in resources(dev_templates["operations"], "AWS::IAM::Policy").values()
        if "TaskRole" in rendered(resource["Properties"].get("Roles"))
    ]
    assert len(policies) == 1
    sids = sorted(statement["Sid"] for statement in policies[0]["Properties"]["PolicyDocument"]["Statement"])
    assert sids == [
        "EraseObjectVersions",
        "ManageSchedules",
        "PassSchedulerRole",
        "PublishAlerts",
        "RetentionLifecycle",
        "WriteAuditExports",
    ]
