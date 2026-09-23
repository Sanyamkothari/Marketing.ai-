"""The M48 routes' contract, without a request: policies, the plan hash, and where a principal id may go.

DEC-746: a data principal's id is accepted only in a JSON body. A path parameter or a query
parameter would be written to the access log of every server and proxy on the way, so no privacy
route may declare one that could carry it - checked here against the live route objects, so a route
added later is held to the same rule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.routing import APIRoute

from api.access_policy import all_policies
from api.routes.privacy import POLICIES, plan_hash, router
from engine.access.roles import Role
from engine.privacy.contracts import RetentionAction, RetentionCategory, RetentionItem, RetentionPlan

MOMENT = datetime(2026, 9, 1, tzinfo=UTC)


def _item(key: str) -> RetentionItem:
    return RetentionItem(
        key=key,
        category=RetentionCategory.UPLOAD,
        action=RetentionAction.DELETE,
        owner_id="u_1",
        created_at=MOMENT - timedelta(days=400),
        retention_days=90,
    )


def _plan(plan_id: str, *keys: str, at: datetime = MOMENT) -> RetentionPlan:
    return RetentionPlan(plan_id=plan_id, planned_at=at, items=tuple(_item(key) for key in keys))


def test_every_privacy_route_is_registered_and_audited_by_the_rules() -> None:
    registered = all_policies()
    for key, policy in POLICIES.items():
        assert registered[key] == policy
        assert policy.purpose, key
        assert policy.action.startswith("privacy."), key
    routes = {
        (method, route.path)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }
    assert routes == set(POLICIES)


def test_only_the_consent_report_is_open_beyond_admin() -> None:
    assert {key for key, policy in POLICIES.items() if policy.role is not Role.ADMIN} == {
        ("GET", "/privacy/runs/{run_id}/consent-report")
    }


def test_no_privacy_route_takes_a_principal_id_outside_the_body() -> None:
    for route in router.routes:
        assert isinstance(route, APIRoute)
        outside = [param.name for param in (*route.dependant.path_params, *route.dependant.query_params)]
        assert not any("principal" in name for name in outside), (route.path, outside)
    takes_principal = {
        route.path
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.body_field is not None
        and "principal_id" in getattr(route.body_field.field_info.annotation, "model_fields", {})
    }
    assert takes_principal == {
        "/privacy/consent",
        "/privacy/consent/lookup",
        "/privacy/erasure",
        "/privacy/access-requests",
    }


def test_the_plan_hash_is_what_the_plan_would_do_not_its_random_id() -> None:
    assert plan_hash(_plan("ret_a", "uploads/u_1/a.csv")) == plan_hash(_plan("ret_b", "uploads/u_1/a.csv"))
    assert plan_hash(_plan("ret_a", "uploads/u_1/a.csv")) != plan_hash(
        _plan("ret_a", "uploads/u_1/a.csv", "uploads/u_1/b.csv")
    )
    assert plan_hash(_plan("ret_a", "uploads/u_1/a.csv")) != plan_hash(
        _plan("ret_a", "uploads/u_1/a.csv", at=MOMENT + timedelta(seconds=1))
    )
    assert len(plan_hash(_plan("ret_a"))) == 64


def test_the_same_instant_in_another_offset_is_the_same_plan() -> None:
    from datetime import timezone

    india = MOMENT.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert plan_hash(_plan("ret_a", "uploads/u_1/a.csv", at=india)) == plan_hash(
        _plan("ret_a", "uploads/u_1/a.csv")
    )
