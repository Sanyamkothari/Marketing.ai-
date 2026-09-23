"""The M49 routes' rows of the policy table (DEC-780), and the outcome file's format rule.

`test_route_policies.py` proves every route has *a* policy; this pins *which*: reading is Viewer,
changing or firing a schedule, adding outcomes and acknowledging an alert are Analyst, and nothing
here needs Approver or Admin - scheduled work acts as `SYSTEM_SCHEDULER` and can never approve.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from api.access_policy import MUTATING_METHODS, all_policies, refusal_message
from api.routes.monitoring import POLICIES as MONITORING_POLICIES
from api.routes.monitoring import outcome_file_format
from api.routes.schedules import POLICIES as SCHEDULE_POLICIES
from engine.access.roles import Role

M49 = {**SCHEDULE_POLICIES, **MONITORING_POLICIES}


def test_every_m49_policy_is_registered_as_declared() -> None:
    registered = all_policies()
    for key, policy in M49.items():
        assert registered[key] == policy, key


@pytest.mark.parametrize("key", sorted(M49), ids=lambda key: f"{key[0]} {key[1]}")
def test_reads_are_viewer_and_every_write_is_analyst(key: tuple[str, str]) -> None:
    policy = M49[key]
    expected = Role.ANALYST if key[0] in MUTATING_METHODS else Role.VIEWER
    assert policy.role is expected
    assert policy.purpose, "a refusal needs words"
    assert not policy.audit_reads, "no M49 read hands out a row"


def test_each_route_names_its_object_for_the_audit_trail() -> None:
    assert M49[("POST", "/schedules/{schedule_id}/fire")].object_param == "schedule_id"
    assert M49[("POST", "/runs/{run_id}/outcomes")].object_type == "run"
    assert M49[("POST", "/monitoring/alerts/{alert_id}/acknowledge")].object_type == "alert"


def test_the_refusals_read_as_sentences() -> None:
    assert refusal_message(M49[("POST", "/schedules")]) == "Only an Analyst can create a schedule."
    assert (
        refusal_message(M49[("POST", "/monitoring/alerts/{alert_id}/acknowledge")])
        == "Only an Analyst can acknowledge an alert."
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [("outcomes.csv", "csv"), ("OUT.CSV", "csv"), ("x.parquet", "parquet"), ("x.pq", "parquet")],
)
def test_the_outcome_format_comes_from_the_suffix(name: str, expected: str) -> None:
    assert outcome_file_format(name) == expected


@pytest.mark.parametrize("name", ["outcomes.xlsx", "outcomes", None, "csv"])
def test_any_other_file_is_415(name: str | None) -> None:
    with pytest.raises(HTTPException) as caught:
        outcome_file_format(name)
    assert caught.value.status_code == 415
    detail: Any = caught.value.detail
    assert detail["code"] == "OUTCOME_FILE_FORMAT_UNSUPPORTED"
