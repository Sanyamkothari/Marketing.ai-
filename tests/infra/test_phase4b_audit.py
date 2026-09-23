"""The audit-export bucket: locked, encrypted, private, TLS-only, append-only, and write-only to the API.

The negative assertions are the ones that matter. An audit trail is worth what it is worth *after*
somebody with access to the deployment wanted it gone, so these tests look for every way an export
could be removed or rewritten - a delete grant on any role, a missing deny on the delete marker, a
bucket that CloudFormation would try to destroy - and fail on each.
"""

from __future__ import annotations

from typing import Any

from aws_cdk.assertions import Template
from infra.naming import AUDIT_EXPORT_PREFIX

from tests.infra.conftest import as_list, rendered, resources, sole, statements


def _bucket(templates: dict[str, Template]) -> tuple[str, dict[str, Any]]:
    found = resources(templates["operations"], "AWS::S3::Bucket")
    assert len(found) == 1, "the operations stack holds exactly one bucket, the audit bucket"
    logical_id, resource = next(iter(found.items()))
    return logical_id, dict(resource)


def _bucket_policy(templates: dict[str, Template]) -> list[dict[str, Any]]:
    policy = sole(templates["operations"], "AWS::S3::BucketPolicy")
    return [dict(statement) for statement in policy["PolicyDocument"]["Statement"]]


def test_object_lock_is_enabled_in_compliance_mode(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    """Governance mode can be bypassed by `s3:BypassGovernanceRetention`; compliance cannot."""
    for templates in (dev_templates, prod_templates):
        properties = _bucket(templates)[1]["Properties"]
        assert properties["ObjectLockEnabled"] is True
        lock = properties["ObjectLockConfiguration"]
        assert lock["ObjectLockEnabled"] == "Enabled"
        assert lock["Rule"]["DefaultRetention"]["Mode"] == "COMPLIANCE"
        assert properties["VersioningConfiguration"] == {"Status": "Enabled"}


def test_the_bucket_is_retained_at_every_env_name(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    """A bucket holding a locked version cannot be emptied, so DESTROY would only fail the teardown."""
    for templates in (dev_templates, prod_templates):
        resource = _bucket(templates)[1]
        assert resource["DeletionPolicy"] == "Retain"
        assert resource["UpdateReplacePolicy"] == "Retain"


def test_the_bucket_is_encrypted_with_its_own_retained_key(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    """DEC-727: not the product key, which a non-prod `cdk destroy` schedules for deletion - that
    would leave every COMPLIANCE-locked export unreadable while Object Lock kept its bytes."""
    rules = _bucket(dev_templates)[1]["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert rules[0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
    assert "AuditExportKey" in rendered(rules[0]["ServerSideEncryptionByDefault"]["KMSMasterKeyID"])
    assert rules[0]["BucketKeyEnabled"] is True
    for templates in (dev_templates, prod_templates):
        keys = resources(templates["operations"], "AWS::KMS::Key")
        (key_id,) = [name for name in keys if name.startswith("AuditExportKey")]
        assert keys[key_id]["DeletionPolicy"] == "Retain"
        assert keys[key_id]["UpdateReplacePolicy"] == "Retain"
        assert keys[key_id]["Properties"]["EnableKeyRotation"] is True


def test_the_bucket_blocks_public_access(dev_templates: dict[str, Template]) -> None:
    assert _bucket(dev_templates)[1]["Properties"]["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }


def test_plain_http_and_obsolete_tls_are_refused(dev_templates: dict[str, Template]) -> None:
    body = _bucket_policy(dev_templates)
    denies = [statement for statement in body if statement["Effect"] == "Deny"]
    assert any("aws:SecureTransport" in rendered(statement.get("Condition")) for statement in denies)
    assert any("s3:TlsVersion" in rendered(statement.get("Condition")) for statement in denies)


def test_nobody_may_delete_an_export_even_with_a_delete_marker(dev_templates: dict[str, Template]) -> None:
    """Object Lock guards versions; a versionless delete still lays a marker unless this denies it."""
    denies = [
        statement
        for statement in _bucket_policy(dev_templates)
        if statement.get("Sid") == "DenyAuditExportDeletion"
    ]
    assert len(denies) == 1
    statement = denies[0]
    assert statement["Effect"] == "Deny"
    assert statement["Principal"] == {"AWS": "*"}
    assert set(as_list(statement["Action"])) == {"s3:DeleteObject", "s3:DeleteObjectVersion"}
    assert "Condition" not in statement, "a deny with a condition is a deny somebody can step around"


def test_access_to_the_bucket_is_logged_under_its_own_prefix(dev_templates: dict[str, Template]) -> None:
    logging = _bucket(dev_templates)[1]["Properties"]["LoggingConfiguration"]
    assert logging["LogFilePrefix"] == "s3-access-audit/"
    assert "AccessLogs" in rendered(logging["DestinationBucketName"])


def test_log_delivery_into_the_log_bucket_is_conditioned_on_the_audit_bucket(
    dev_templates: dict[str, Template],
) -> None:
    """CDK would have added this grant with no condition; it is written out in storage.py instead."""
    found: list[dict[str, Any]] = []
    for resource in resources(dev_templates["storage"], "AWS::S3::BucketPolicy").values():
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            if "s3-access-audit" in rendered(statement.get("Resource")):
                found.append(dict(statement))
    assert len(found) == 1, "exactly one delivery grant for the audit prefix, and it is ours"
    statement = found[0]
    assert statement["Sid"] == "AuditBucketAccessLogDelivery"
    assert statement["Principal"] == {"Service": "logging.s3.amazonaws.com"}
    assert (
        statement["Condition"]["ArnLike"]["aws:SourceArn"]
        == "arn:aws:s3:::marketing-ai-dev-123456789012-audit"
    )
    assert statement["Condition"]["StringEquals"]["aws:SourceAccount"] == "123456789012"


def test_the_bucket_name_is_the_one_the_log_grant_names(dev_templates: dict[str, Template]) -> None:
    assert _bucket(dev_templates)[1]["Properties"]["BucketName"] == "marketing-ai-dev-123456789012-audit"


def test_the_api_may_write_exports_under_the_audit_prefix_and_do_nothing_else_there(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    for templates in (dev_templates, prod_templates):
        logical_id, _ = _bucket(templates)
        touching = [
            statement
            for template in templates.values()
            for statement in statements(template)
            if logical_id in rendered(statement.get("Resource"))
        ]
        assert len(touching) == 1, "one identity statement in the whole app names the audit bucket"
        statement = touching[0]
        assert statement["Effect"] == "Allow"
        assert sorted(as_list(statement["Action"])) == ["s3:PutObject", "s3:PutObjectRetention"]
        assert f"/{AUDIT_EXPORT_PREFIX}/*" in rendered(statement["Resource"])
        assert len(as_list(statement["Resource"])) == 1


def test_no_role_anywhere_may_delete_or_bypass_a_lock(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    for templates in (dev_templates, prod_templates):
        for component, template in templates.items():
            for statement in statements(template, effect="Allow"):
                actions = as_list(statement.get("Action"))
                assert "s3:BypassGovernanceRetention" not in actions, component
                assert "s3:PutBucketObjectLockConfiguration" not in actions, component
                resource = rendered(statement.get("Resource"))
                if "AuditExports" in resource or "-audit" in resource:
                    for action in actions:
                        assert not action.startswith("s3:Delete"), (component, action)
                        assert "*" not in action, (component, action)


def test_the_write_grant_is_on_the_task_role(dev_templates: dict[str, Template]) -> None:
    for resource in resources(dev_templates["operations"], "AWS::IAM::Policy").values():
        if resource["Properties"]["PolicyName"] == "marketing-ai-dev-api-operations":
            assert "TaskRole" in rendered(resource["Properties"]["Roles"])
            return
    raise AssertionError("no api-operations policy in the operations stack")
