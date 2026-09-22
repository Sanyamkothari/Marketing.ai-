"""The bucket, the key, and the encryption-header policy that is easy to get subtly wrong.

The interesting assertion is the last pair: the deny must fire on a PUT that names the *wrong*
encryption and must not fire on a PUT that names none, because `S3Storage` only sets the header
when `s3_kms_key_id` is configured and a header-less PUT falls through to the bucket's default.
"""

from __future__ import annotations

from typing import Any

from aws_cdk.assertions import Template

from tests.infra.conftest import rendered, resources


def _artefact_bucket(template: Template) -> dict[str, Any]:
    for resource in resources(template, "AWS::S3::Bucket").values():
        properties = resource["Properties"]
        if properties.get("VersioningConfiguration"):
            return dict(properties)
    raise AssertionError("no versioned bucket found")


def test_every_bucket_blocks_public_access(dev_templates: dict[str, Template]) -> None:
    buckets = resources(dev_templates["storage"], "AWS::S3::Bucket")
    assert len(buckets) == 2
    for resource in buckets.values():
        assert resource["Properties"]["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }


def test_the_artefact_bucket_uses_the_customer_key(dev_templates: dict[str, Template]) -> None:
    bucket = _artefact_bucket(dev_templates["storage"])
    rules = bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert rules[0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
    assert rules[0]["ServerSideEncryptionByDefault"]["KMSMasterKeyID"]
    assert rules[0]["BucketKeyEnabled"] is True


def test_the_artefact_bucket_is_versioned(dev_templates: dict[str, Template]) -> None:
    assert _artefact_bucket(dev_templates["storage"])["VersioningConfiguration"] == {"Status": "Enabled"}


def test_the_key_rotates(dev_templates: dict[str, Template]) -> None:
    keys = resources(dev_templates["storage"], "AWS::KMS::Key")
    assert len(keys) == 1
    assert next(iter(keys.values()))["Properties"]["EnableKeyRotation"] is True


def test_incomplete_multipart_uploads_are_abandoned(dev_templates: dict[str, Template]) -> None:
    rules = _artefact_bucket(dev_templates["storage"])["LifecycleConfiguration"]["Rules"]
    aborts = [rule for rule in rules if "AbortIncompleteMultipartUpload" in rule]
    assert aborts, "a half-finished upload is stored and billed and invisible in the object list"
    assert aborts[0]["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 7


def _bucket_policy_statements(template: Template, *, sid_prefix: str = "") -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for resource in resources(template, "AWS::S3::BucketPolicy").values():
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            if not sid_prefix or str(statement.get("Sid", "")).startswith(sid_prefix):
                collected.append(dict(statement))
    return collected


def test_plain_http_is_denied(dev_templates: dict[str, Template]) -> None:
    denies = [
        statement
        for statement in _bucket_policy_statements(dev_templates["storage"])
        if statement["Effect"] == "Deny" and "aws:SecureTransport" in rendered(statement.get("Condition"))
    ]
    assert len(denies) == 2, "both buckets refuse plaintext HTTP"


def test_obsolete_tls_is_denied(dev_templates: dict[str, Template]) -> None:
    body = rendered(_bucket_policy_statements(dev_templates["storage"]))
    assert "s3:TlsVersion" in body


def test_a_put_naming_the_wrong_encryption_is_denied(dev_templates: dict[str, Template]) -> None:
    denies = _bucket_policy_statements(dev_templates["storage"], sid_prefix="DenyIncorrect")
    assert len(denies) == 2, "one for the algorithm, one for the key id"
    for statement in denies:
        assert statement["Effect"] == "Deny"
        assert statement["Action"] == "s3:PutObject"
        condition = statement["Condition"]
        assert "StringNotEquals" in condition


def test_a_put_naming_no_encryption_is_not_denied(dev_templates: dict[str, Template]) -> None:
    """`Null: false` is what makes the statement match only a request that set the header.

    Without it the deny would refuse every header-less PUT - which is exactly what `S3Storage`
    sends when `s3_kms_key_id` is unset - and the refusal would arrive as `AccessDenied` on a
    write, reading like a broken role rather than a policy choice.
    """
    for statement in _bucket_policy_statements(dev_templates["storage"], sid_prefix="DenyIncorrect"):
        null_condition = statement["Condition"]["Null"]
        assert set(null_condition.values()) == {"false"}
        header = next(iter(null_condition))
        assert header in statement["Condition"]["StringNotEquals"], (
            "the Null guard and the StringNotEquals must name the same header, or the guard "
            "protects a condition that is not there"
        )


def test_the_access_log_bucket_does_not_use_the_product_key(
    dev_templates: dict[str, Template],
) -> None:
    for resource in resources(dev_templates["storage"], "AWS::S3::Bucket").values():
        properties = resource["Properties"]
        if properties.get("VersioningConfiguration"):
            continue
        rules = properties["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        assert rules[0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"


def test_the_only_lambda_in_the_whole_app_is_the_one_that_closes_the_default_security_group(
    dev_templates: dict[str, Template],
) -> None:
    """Every CDK convenience that brings a Lambda also brings a standing grant.

    `auto_delete_objects` leaves a function that may empty a customer's bucket for the lifetime of
    the deployment; `cloudwatch_logs_retention` leaves one holding `logs:PutRetentionPolicy`. This
    app uses neither, and this test is what keeps it that way - it names the single exception
    rather than counting, so a *second* one cannot slip in beside it.
    """
    found = {
        component: sorted(template.find_resources("AWS::Lambda::Function"))
        for component, template in dev_templates.items()
        if template.find_resources("AWS::Lambda::Function")
    }
    assert list(found) == ["network"], found
    assert len(found["network"]) == 1
    assert (
        "VpcRestrictDefaultSG" in found["network"][0]
    ), "a new Lambda appeared; whatever construct added it also added a role - read the policy"


def test_the_image_repository_scans_and_is_immutable(dev_templates: dict[str, Template]) -> None:
    repositories = resources(dev_templates["storage"], "AWS::ECR::Repository")
    assert len(repositories) == 1
    properties = next(iter(repositories.values()))["Properties"]
    assert properties["ImageScanningConfiguration"] == {"ScanOnPush": True}
    assert properties["ImageTagMutability"] == "IMMUTABLE"


def test_the_image_repository_is_retained_even_in_dev(dev_templates: dict[str, Template]) -> None:
    """CI pushes into it before `cdk deploy` runs; destroying it breaks the next deployment."""
    for resource in resources(dev_templates["storage"], "AWS::ECR::Repository").values():
        assert resource["DeletionPolicy"] == "Retain"
