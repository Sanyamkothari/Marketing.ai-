"""`AppContext`: the defaults, the two spellings, and every combination it refuses.

These are the cheapest tests in the suite and they cover the failure this whole module exists for:
a misspelt `-c` key that synthesises happily and does nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from infra.context import (
    FARGATE_CPU_MEMORY,
    KNOWN_KEYS,
    NAMESPACE,
    REQUIRED_PRIVATE_ENDPOINTS,
    AppContext,
    ContextError,
)


def test_defaults_match_the_plan() -> None:
    context = AppContext.from_mapping({})
    assert context.env_name == "dev"
    assert context.region == "ap-south-1"
    assert context.db_instance_class == "db.t4g.small"
    assert context.db_storage_gb == 50
    assert context.db_multi_az is False
    assert (context.api_min_tasks, context.api_max_tasks) == (1, 3)
    assert context.sagemaker_instance_train == "ml.m5.2xlarge"
    assert context.sagemaker_instance_process == "ml.m5.xlarge"
    assert context.max_concurrent_jobs == 3
    assert context.bedrock_enabled is False
    assert context.bedrock_model_ids == ()
    assert context.kms_key_alias == "alias/marketing-ai-dev"


def test_monthly_budget_has_no_default() -> None:
    """A guessed budget would look like somebody's decision (plan section 13.3)."""
    assert AppContext.from_mapping({}).monthly_budget_usd is None


def test_an_unknown_key_is_refused_not_ignored() -> None:
    with pytest.raises(ContextError) as caught:
        AppContext.from_mapping({"db_storage_bg": "50"})
    assert "db_storage_bg" in str(caught.value)


def test_an_unknown_namespaced_key_is_refused() -> None:
    with pytest.raises(ContextError) as caught:
        AppContext.from_mapping({f"{NAMESPACE}bucket_prefix": "x"})
    assert "bucket_prefix" in str(caught.value)


def test_a_near_miss_gets_a_suggestion() -> None:
    with pytest.raises(ContextError) as caught:
        AppContext.from_mapping({"alert_emial": "ops@example.com"})
    assert "Did you mean" in str(caught.value)


@pytest.mark.parametrize(
    "key",
    [
        "@aws-cdk/aws-s3:serverAccessLogsUseBucketPolicy",
        "aws:cdk:enable-path-metadata",
        "availability-zones:account=123456789012:region=ap-south-1",
        "hosted-zone:account=1:domainName=example.com:region=ap-south-1",
    ],
)
def test_cdk_own_keys_are_left_alone(key: str) -> None:
    """The toolkit shares the bag; its keys carry `@` or `:` and are not ours to refuse."""
    assert AppContext.from_mapping({key: True, "env_name": "dev"}).env_name == "dev"


def test_both_spellings_mean_the_same_thing() -> None:
    bare = AppContext.from_mapping({"db_storage_gb": "120"})
    namespaced = AppContext.from_mapping({f"{NAMESPACE}db_storage_gb": "120"})
    assert bare.db_storage_gb == namespaced.db_storage_gb == 120


def test_the_same_key_twice_with_different_values_is_refused() -> None:
    with pytest.raises(ContextError) as caught:
        AppContext.from_mapping({"db_storage_gb": "50", f"{NAMESPACE}db_storage_gb": "120"})
    assert "twice" in str(caught.value)


def test_every_known_key_round_trips() -> None:
    """No key is accepted by `KNOWN_KEYS` and then dropped on the floor by `from_mapping`."""
    for key in KNOWN_KEYS:
        assert hasattr(AppContext.from_mapping({}), key), key


@pytest.mark.parametrize(
    ("values", "fragment"),
    [
        ({"env_name": "staging"}, "must be one of"),
        ({"api_cpu": "1500"}, "not a Fargate CPU size"),
        ({"api_cpu": "2048", "api_memory": "1024"}, "not valid for api_cpu"),
        ({"api_min_tasks": "0"}, "at least 1"),
        ({"api_min_tasks": "3", "api_max_tasks": "2"}, "at least api_min_tasks"),
        ({"db_storage_gb": "10"}, "20 GiB"),
        ({"db_backup_retention_days": "0"}, "automated backups"),
        ({"monthly_budget_usd": "100"}, "alert_email"),
        ({"monthly_budget_usd": "-1", "alert_email": "a@b.c"}, "greater than zero"),
        ({"domain_name": "example.com"}, "without certificate_arn"),
        ({"image_digest": "marketing-ai:latest"}, "@sha256:"),
        ({"alarm_thresholds": "NoEquals"}, "Name=value"),
        ({"bedrock_enabled": "maybe"}, "boolean"),
        ({"db_storage_gb": "lots"}, "whole number"),
    ],
)
def test_refused_combinations(values: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ContextError) as caught:
        AppContext.from_mapping(values)
    assert fragment in str(caught.value)


def test_prod_refuses_an_http_only_load_balancer() -> None:
    with pytest.raises(ContextError) as caught:
        AppContext.from_mapping({"env_name": "prod", "domain_name": "x.example.com"})
    assert "clear text" in str(caught.value)


def test_prod_refuses_a_deployment_with_no_origin() -> None:
    with pytest.raises(ContextError) as caught:
        AppContext.from_mapping(
            {"env_name": "prod", "certificate_arn": "arn:aws:acm:ap-south-1:1:certificate/a"}
        )
    assert "domain_name" in str(caught.value)


def test_no_nat_without_the_endpoints_a_task_cannot_start_without() -> None:
    with pytest.raises(ContextError) as caught:
        AppContext.from_mapping({"nat_gateways": "0", "vpc_endpoints": "ecr.api,ecr.dkr"})
    message = str(caught.value)
    for name in ("logs", "secretsmanager", "ssm"):
        assert name in message
    assert "cannot pull its image" in message


def test_no_nat_with_every_required_endpoint_is_allowed() -> None:
    context = AppContext.from_mapping(
        {"nat_gateways": "0", "vpc_endpoints": ",".join(REQUIRED_PRIVATE_ENDPOINTS)}
    )
    assert context.private_subnets_have_egress is False


def test_every_fargate_cpu_size_has_a_workable_default_memory() -> None:
    for cpu, (low, _high, _step) in FARGATE_CPU_MEMORY.items():
        context = AppContext.from_mapping({"api_cpu": str(cpu), "api_memory": str(low)})
        assert context.api_memory == low


def test_alarm_thresholds_parse_into_a_mapping() -> None:
    context = AppContext.from_mapping({"alarm_thresholds": "StageSeconds=900,DailyUsd=25"})
    assert context.alarm_threshold_values == {"StageSeconds": "900", "DailyUsd": "25"}


def test_tags_name_the_product_and_the_deployment() -> None:
    assert AppContext.from_mapping({"client_id": "acme"}).tags() == {
        "product": "marketing-ai",
        "env": "dev",
        "client": "acme",
    }


def test_the_client_tag_is_absent_when_no_client_is_named() -> None:
    assert "client" not in AppContext.from_mapping({}).tags()
