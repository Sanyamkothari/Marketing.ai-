"""Phase 4b's context keys: sign-in on by default, a lock period per `env_name`, a sized job task.

Two of these are `env_name` rows in `AppContext`'s table, and a row in a docstring is worth what
the assertion beside it is worth - so the audit lock period is checked on the synthesised bucket,
not only on the context object that produced it, in the way `test_env_name.py` checks the others.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from aws_cdk.assertions import Template
from infra.context import AUTH_MODES, PROD_AUDIT_RETENTION_DAYS, AppContext, ContextError

from tests.infra.conftest import PROD_CONTEXT_KEYS, sole


def _prod(**extra: str) -> AppContext:
    return AppContext.from_mapping({**PROD_CONTEXT_KEYS, **extra})


def test_sign_in_is_on_by_default_at_both_names() -> None:
    """Every deployment of this app is behind a public load balancer; `off` is a laptop setting."""
    assert AppContext.from_mapping({"env_name": "dev"}).auth_mode == "local"
    assert _prod().auth_mode == "local"


def test_a_dev_deployment_may_still_ask_for_sign_in_off() -> None:
    assert AppContext.from_mapping({"env_name": "dev", "auth_mode": "OFF"}).auth_mode == "off"


def test_a_prod_deployment_with_sign_in_off_is_refused_at_synth_time() -> None:
    """At runtime it would answer 503 on every route (DEC-702); here the fix is one flag."""
    with pytest.raises(ContextError, match="sign-in off"):
        _prod(auth_mode="off")


def test_an_unknown_auth_mode_is_refused() -> None:
    with pytest.raises(ContextError, match="auth_mode: must be one of"):
        AppContext.from_mapping({"auth_mode": "cognito"})


def test_the_auth_modes_are_the_engine_s() -> None:
    settings_module = pytest.importorskip(
        "engine.settings", reason="the engine is not installed in this venv; `make infra-setup` installs it"
    )
    assert set(AUTH_MODES) == set(get_args(settings_module.AuthMode))


def test_the_audit_lock_period_follows_env_name() -> None:
    assert AppContext.from_mapping({"env_name": "dev"}).audit_retention_days == 1
    assert _prod().audit_retention_days == PROD_AUDIT_RETENTION_DAYS == 2555


def test_the_prod_lock_period_is_the_setting_s_own_default() -> None:
    """The bucket's default and the date the export sets must be the same number of days."""
    settings_module = pytest.importorskip(
        "engine.settings", reason="the engine is not installed in this venv"
    )
    assert settings_module.Settings().audit_retention_days == PROD_AUDIT_RETENTION_DAYS


def test_the_lock_period_can_be_overridden_at_either_name() -> None:
    assert AppContext.from_mapping({"audit_retention_days": "30"}).audit_retention_days == 30
    assert _prod(audit_retention_days="3650").audit_retention_days == 3650


@pytest.mark.parametrize("days", ["0", "3651", "-5"])
def test_a_lock_period_outside_the_setting_s_range_is_refused(days: str) -> None:
    with pytest.raises(ContextError, match="audit_retention_days"):
        AppContext.from_mapping({"audit_retention_days": days})


@pytest.mark.parametrize(
    ("values", "fragment"),
    [
        ({"job_cpu": "1500"}, "job_cpu"),
        ({"job_cpu": "1024", "job_memory": "1024"}, "job_memory"),
        ({"job_cpu": "256", "job_memory": "4096"}, "job_memory"),
    ],
)
def test_the_job_task_size_is_a_fargate_size(values: dict[str, str], fragment: str) -> None:
    with pytest.raises(ContextError, match=fragment):
        AppContext.from_mapping(values)


def test_the_job_task_size_defaults() -> None:
    context = AppContext.from_mapping({})
    assert (context.job_cpu, context.job_memory) == (1024, 4096)


def _audit_bucket(templates: dict[str, Template]) -> dict[str, Any]:
    return sole(templates["operations"], "AWS::S3::Bucket")


def test_the_synthesised_lock_period_follows_env_name(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    for templates, days in ((dev_templates, 1), (prod_templates, 2555)):
        rule = _audit_bucket(templates)["ObjectLockConfiguration"]["Rule"]
        assert rule == {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": days}}
