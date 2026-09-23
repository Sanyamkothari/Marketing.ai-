"""Every route declares its role (plan M46, section 4: "a test fails if a route has none").

The route list is read from a live `create_app()`, so a route that any branch adds tomorrow - a
Phase 4b router's included - is checked by this file without anyone editing it. A route with no
policy is also refused at runtime (`ROUTE_HAS_NO_POLICY`), so this test is the early warning and the
enforcement is the guarantee.
"""

from __future__ import annotations

import pytest

from api.access_policy import LEGACY_POLICIES, MUTATING_METHODS, RoutePolicy, all_policies, refusal_message
from engine.access.roles import Role
from tests.integration.production.access_support import live_routes

ROUTES = live_routes()

PUBLIC: frozenset[tuple[str, str]] = frozenset({("GET", "/healthz"), ("POST", "/auth/login")})
"""The only routes that answer without a sign-in. Adding one is a reviewed change to this set."""


def test_the_app_has_routes_to_check() -> None:
    assert len(ROUTES) > 50, "the walk found too few routes; FastAPI's route nesting may have changed"


@pytest.mark.parametrize("route", ROUTES, ids=lambda route: route.id)
def test_every_route_has_a_policy(route: object) -> None:
    from tests.integration.production.access_support import LiveRoute

    assert isinstance(route, LiveRoute)
    assert route.policy is not None, (
        f"{route.id} has no access policy. Add it to api/access_policy.py LEGACY_POLICIES, or call "
        "api.access_policy.register(...) from the router that declares it."
    )


def test_only_the_reviewed_routes_are_public() -> None:
    public = {(route.method, route.path) for route in ROUTES if route.policy and route.policy.role is None}
    assert public == PUBLIC


def test_every_legacy_policy_names_a_route_that_exists() -> None:
    """A stale row is a policy for a route nobody serves, which hides a rename that dropped one."""
    live = {(route.method, route.path) for route in ROUTES}
    assert sorted(set(LEGACY_POLICIES) - live) == []


def test_every_policy_is_well_formed() -> None:
    for (method, path), policy in all_policies().items():
        assert isinstance(policy, RoutePolicy)
        assert method == method.upper() and path.startswith("/")
        assert policy.action and "." in policy.action and policy.action == policy.action.lower(), policy
        if policy.object_param is not None:
            assert "{" + policy.object_param + "}" in path, (method, path, policy.object_param)
        if policy.audit_reads:
            assert method == "GET", (method, path)


def test_no_legacy_route_lets_a_viewer_change_anything() -> None:
    """A Viewer sees results (plan M46). Every pre-4b write needs a role beyond Viewer."""
    loose = [
        key
        for key, policy in LEGACY_POLICIES.items()
        if key[0] in MUTATING_METHODS and policy.role in (None, Role.VIEWER)
    ]
    assert loose == []


def test_every_legacy_policy_has_a_purpose_to_explain_a_refusal() -> None:
    assert [key for key, policy in LEGACY_POLICIES.items() if not policy.purpose] == []


@pytest.mark.parametrize(
    ("key", "role"),
    [
        (("POST", "/models/{model_id}/approve"), Role.APPROVER),
        (("POST", "/models/{model_id}/promote"), Role.APPROVER),
        (("POST", "/runs/{run_id}/campaign-copy/templates/{template_id}/approve"), Role.APPROVER),
        (("POST", "/runs"), Role.ANALYST),
        (("POST", "/uploads"), Role.ANALYST),
        (("PUT", "/connection/aws"), Role.ADMIN),
        (("DELETE", "/connection/aws"), Role.ADMIN),
        (("POST", "/connection/aws/test"), Role.ADMIN),
        (("GET", "/runs"), Role.VIEWER),
    ],
)
def test_the_separation_of_duties_is_what_the_plan_says(key: tuple[str, str], role: Role) -> None:
    assert all_policies()[key].role is role


@pytest.mark.parametrize(
    "key",
    [
        ("GET", "/runs/{run_id}/scores.csv"),
        ("GET", "/runs/{run_id}/copy_messages.csv"),
        ("GET", "/runs/{run_id}/artefacts/{name}"),
    ],
)
def test_row_level_downloads_are_audited(key: tuple[str, str]) -> None:
    assert all_policies()[key].audit_reads


def test_the_refusal_reads_as_a_sentence() -> None:
    policy = all_policies()[("POST", "/models/{model_id}/approve")]
    assert refusal_message(policy) == "Only an Approver can approve a champion."
    assert refusal_message(all_policies()[("POST", "/runs")]) == "Only an Analyst can start a run."
    assert refusal_message(RoutePolicy(role=Role.VIEWER, action="x.y")) == "Only a Viewer can do this."
