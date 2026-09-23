"""S3 lifecycle rules as a retention backstop (DEC-740), against moto - never real AWS."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from engine.privacy.config import load_privacy_config
from engine.privacy.errors import PrivacyError
from engine.privacy.lifecycle import apply_lifecycle, build_lifecycle_rules
from engine.privacy.retention import retention_days_by_use_case

BUCKET = "test-artefacts"
REGION = "ap-south-1"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        yield client


def test_rules_cover_each_input_prefix_with_the_longest_setting_plus_grace(config_root: Path) -> None:
    policy = load_privacy_config(config_root).retention.lifecycle
    days, default = retention_days_by_use_case(config_root)
    rules = build_lifecycle_rules(config_root, s3_prefix="client-a", client_ids=["cl_1", "cl_2"])
    assert {rule.prefix for rule in rules} == {
        "client-a/uploads/",
        "client-a/datasets/",
        "client-a/clients/cl_1/sources/",
        "client-a/clients/cl_2/sources/",
    }
    assert {rule.expiration_days for rule in rules} == {max([default, *days.values()]) + policy.grace_days}
    assert all(rule.rule_id.startswith(policy.rule_id_prefix) for rule in rules)
    assert all(rule.noncurrent_days == policy.grace_days for rule in rules)
    assert not [
        rule for rule in rules if rule.prefix.startswith("client-a/runs/")
    ], "run artefacts are the job's"


def test_without_named_clients_one_rule_covers_every_source(config_root: Path) -> None:
    prefixes = {rule.prefix for rule in build_lifecycle_rules(config_root)}
    assert prefixes == {"uploads/", "datasets/", "clients/"}


def test_apply_preserves_rules_it_does_not_own(s3: Any, config_root: Path) -> None:
    prefix = load_privacy_config(config_root).retention.lifecycle.rule_id_prefix
    foreign = {
        "ID": "access-logs-expiry",
        "Status": "Enabled",
        "Filter": {"Prefix": "logs/"},
        "Expiration": {"Days": 30},
    }
    stale_ours = {
        "ID": f"{prefix}old-rule",
        "Status": "Enabled",
        "Filter": {"Prefix": "old/"},
        "Expiration": {"Days": 1},
    }
    s3.put_bucket_lifecycle_configuration(
        Bucket=BUCKET, LifecycleConfiguration={"Rules": [foreign, stale_ours]}
    )
    rules = build_lifecycle_rules(config_root, s3_prefix="client-a")
    result = apply_lifecycle(s3, BUCKET, rules, rule_id_prefix=prefix)
    assert result.preserved == ("access-logs-expiry",)
    assert result.replaced == (f"{prefix}old-rule",)
    current = {rule["ID"]: rule for rule in s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]}
    assert set(current) == {"access-logs-expiry", *(rule.rule_id for rule in rules)}
    assert current["access-logs-expiry"]["Expiration"] == {"Days": 30}
    uploads = current[f"{prefix}client-a-uploads"]
    assert uploads["Filter"] == {"Prefix": "client-a/uploads/"}
    assert uploads["NoncurrentVersionExpiration"]["NoncurrentDays"] >= 1


def test_apply_on_a_bucket_with_no_configuration(s3: Any, config_root: Path) -> None:
    prefix = load_privacy_config(config_root).retention.lifecycle.rule_id_prefix
    rules = build_lifecycle_rules(config_root)
    result = apply_lifecycle(s3, BUCKET, rules, rule_id_prefix=prefix)
    assert result.preserved == ()
    assert len(s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]) == len(rules)


def test_applying_twice_is_idempotent(s3: Any, config_root: Path) -> None:
    prefix = load_privacy_config(config_root).retention.lifecycle.rule_id_prefix
    rules = build_lifecycle_rules(config_root)
    apply_lifecycle(s3, BUCKET, rules, rule_id_prefix=prefix)
    first = s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]
    apply_lifecycle(s3, BUCKET, rules, rule_id_prefix=prefix)
    assert s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"] == first


def test_an_unreadable_configuration_changes_nothing(config_root: Path) -> None:
    class Refusing:
        put_calls = 0

        def get_bucket_lifecycle_configuration(self, **kwargs: Any) -> dict[str, Any]:
            error = RuntimeError("denied")
            error.response = {"Error": {"Code": "AccessDenied"}}  # type: ignore[attr-defined]
            raise error

        def put_bucket_lifecycle_configuration(self, **kwargs: Any) -> dict[str, Any]:
            Refusing.put_calls += 1
            return {}

    with pytest.raises(PrivacyError) as excinfo:
        apply_lifecycle(Refusing(), BUCKET, build_lifecycle_rules(config_root), rule_id_prefix="x-")
    assert excinfo.value.code == "LIFECYCLE_READ_FAILED"
    assert Refusing.put_calls == 0
