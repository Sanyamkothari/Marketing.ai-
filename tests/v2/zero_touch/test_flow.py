"""The five-click setup in the marketer app, and Live's exceptions-only inbox and Autopilot offer (ZERO_TOUCH.md).

Three halves:

- the JavaScript (`flow.test.mjs`, run in node with a tiny DOM): the five clicks from "Connect Shopify" to a running
  Shadow plan, keeping every default; money always with its range; no money without the history to size it; Live's
  inbox items with one button each, and the Autopilot offer with its proof. It skips without node 20.10 or newer.
- the sample files the app falls back to (`ui/v2/mock/`): each one parses as the API's own answer, so the sample data
  can never show a field the server does not send.
- the real API on a Shopify-shaped shop: every route the app calls exists, and its answer carries the fields the
  screens read.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from api.v2.app import create_app
from api.v2.deps import Services, build_demo_services
from api.v2.inbox import InboxView
from api.v2.onboarding import OnboardingDesk, attach_onboarding
from api.v2.onboarding_brand import BrandPage
from autonomy.contracts import GoalStatus
from autonomy.onboarding import LearnedLimits, OpportunityScan
from autonomy.runtime.promotion import Readiness
from tests.v2.zero_touch.test_goals_limits import NOW, shopify_shop

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
UI: Final[Path] = ROOT / "ui" / "v2"
MOCK: Final[Path] = UI / "mock"
NODE_TEST: Final[Path] = Path(__file__).resolve().parent / "flow.test.mjs"


def node_command() -> list[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the JS half of this test needs it")
    out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=False)
    match = re.match(r"v(\d+)\.(\d+)", out.stdout.strip())
    version = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    if version < (20, 10):
        pytest.skip(f"node {out.stdout.strip()} is too old to load ES modules by syntax")
    return [node, "--test"] if version >= (22, 7) else [node, "--experimental-detect-module", "--test"]


def load(name: str) -> Any:
    return json.loads((MOCK / name).read_text(encoding="utf-8"))


# --- the JavaScript ------------------------------------------------------------------------------


def test_the_javascript_walks_the_five_clicks_and_live() -> None:
    done = subprocess.run(
        [*node_command(), str(NODE_TEST)], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]


def test_the_setup_is_five_clicks_or_fewer() -> None:
    js = (UI / "screens" / "onboarding.js").read_text(encoding="utf-8")
    match = re.search(r"SETUP_CLICKS = Object\.freeze\(\[(.*?)\]\)", js)
    assert match is not None
    clicks = re.findall(r'"([^"]+)"', match.group(1))
    assert clicks == [
        "Continue to Shopify",
        "Connect Klaviyo",
        "Use this goal",
        "Sign and start Shadow",
        "Approve this voice",
    ]
    assert len(clicks) <= 5


# --- the sample files are the API's own answers --------------------------------------------------


def test_the_sample_proposals_are_an_opportunity_scan_with_honest_money() -> None:
    scan = OpportunityScan.model_validate(load("onboarding_proposals.json"))
    assert scan.preselected_id == "opp_refill_due"
    sized = [p for p in scan.proposals if p.monthly_profit is not None]
    assert sized, "the sample shop has history enough to size its goals"
    for p in sized:
        money = p.monthly_profit
        assert money is not None and money.low <= money.point <= money.high, p.opportunity_id
        assert p.enough_history and p.basis, p.opportunity_id
    assert [p.opportunity_id for p in scan.proposals] == [
        p.opportunity_id for p in sorted(scan.proposals, key=lambda x: x.rank)
    ]


def test_the_sample_limits_are_learned_limits_for_each_proposal() -> None:
    scan = load("onboarding_proposals.json")
    limits = load("onboarding_limits.json")
    assert set(limits) == {p["opportunity_id"] for p in scan["proposals"]}
    for opportunity_id, body in limits.items():
        learned = LearnedLimits.model_validate(body)
        assert learned.opportunity_id == opportunity_id
        assert {lim.key for lim in learned.limits} >= {
            "cadence",
            "discount_depth",
            "margin",
            "quiet_hours",
            "budget",
        }


def test_the_sample_voice_inbox_and_autopilot_are_the_apis_answers() -> None:
    page = BrandPage.model_validate(load("onboarding_brand.json"))
    assert page.status == "waiting" and len(page.samples) == 3 and page.fingerprint
    assert all(".invalid/" in a.url for a in page.assets), "sample photos are never requested"
    box = InboxView.model_validate(load("inbox.json"))
    assert box.count == len(box.items) <= 2, "a healthy shop sees 0-2 items a week"
    for item in box.items:
        assert item.button.label and item.sentence and item.action
    alerts = {a["alert_id"] for a in load("alerts.json")}
    assert {ref for item in box.items for ref in item.refs} <= alerts, "each item traces to a sample alert"
    proof = load("proof.json")
    for goal_id, body in load("autopilot.json").items():
        ready = Readiness.model_validate(body)
        assert ready.goal_id == goal_id and ready.ready
        assert ready.observed is not None
        assert (
            ready.observed.model_dump() == proof["incremental_profit"]
        ), "the offer's proof is the Proof page's"
        assert ready.observed.low > 0, "only a range wholly above zero earns Autopilot"


def test_no_new_sample_text_says_holdout() -> None:
    for name in ("onboarding_brand.json", "inbox.json", "autopilot.json"):
        assert "holdout" not in (MOCK / name).read_text(encoding="utf-8").lower(), name


# --- the real API serves what the screens read -----------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    events, states = shopify_shop()
    base = build_demo_services(NOW, n_customers=20, days=1)
    for goal in base.workspace.goals():  # a fresh shop: nothing is running yet
        base.workspace.put_goal(goal.model_copy(update={"status": GoalStatus.DONE}))
    services: Services = dataclasses.replace(base, events=events, states=states, clock=lambda: NOW)
    app = create_app(services)
    attach_onboarding(app, OnboardingDesk(secret=b"test-secret", persistent_key=True))
    with TestClient(app) as c:
        yield c


def test_every_route_the_app_calls_for_setup_and_inbox_exists(client: TestClient) -> None:
    """A route exists when the server does not answer FastAPI's own "Not Found" (a 405 or a named 404 still match)."""
    js = (UI / "api.js").read_text(encoding="utf-8")
    called = set(re.findall(r'"(/(?:onboarding|inbox|autopilot)[^"?]*)"', js))
    for raw in re.findall(r"`(/(?:onboarding|inbox|autopilot)[^`]*)`", js):
        called.add(re.sub(r"\$\{[^}]*\}", "x", raw.split("${query")[0]))
    assert {
        "/onboarding/proposals",
        "/onboarding/accept",
        "/onboarding/brand/approve",
        "/inbox/x/act",
    } <= called
    for path in sorted(called):
        answer = client.get("/v2" + path)
        missing = answer.status_code == 404 and answer.json().get("detail") == "Not Found"
        assert not missing, path


def test_the_setup_answers_carry_what_the_screens_read(client: TestClient) -> None:
    scan = client.get("/v2/onboarding/proposals").json()
    for key in ("proposals", "preselected_id", "not_enough", "notes", "skipped", "currency"):
        assert key in scan, key
    for p in scan["proposals"]:
        for key in (
            "opportunity_id",
            "title",
            "why",
            "basis",
            "audience_now",
            "monthly_profit",
            "enough_history",
            "not_enough",
            "already_running",
            "currency",
            "goal_text",
            "kind",
            "rank",
        ):
            assert key in p, key
    limits = client.get("/v2/onboarding/limits", params={"opportunity_id": scan["preselected_id"]}).json()
    for lim in limits["limits"]:
        assert {"key", "label", "found", "use", "description", "source", "from_data", "question"} <= set(lim)
    terms = limits["terms"]
    used = {
        "max_touches",
        "window_days",
        "max_offer_pct",
        "offers_on",
        "margin_rate",
        "margin_floor_pct",
        "quiet_start",
        "quiet_end",
        "budget_ceiling",
        "currency",
        "valid_days",
    }
    assert used <= set(terms), used - set(terms)
    out = client.post(
        "/v2/onboarding/accept", json={"opportunity_id": scan["preselected_id"], "offers": False}
    )
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["goal"]["autonomy"] == "shadow" and body["goal"]["status"] == "active"
    assert {"unmet", "single_owner", "goal_id"} <= set(body["mandate"]) and body["mandate"]["mandate"][
        "valid_until"
    ]
    assert "sends nothing" in body["words"]
    records = client.get("/v2/onboarding/mandates").json()
    assert (
        records[0]["goal_id"] == body["goal"]["goal_id"]
    ), "the setup finds its own goal again after a reload"
    brand = client.get("/v2/onboarding/brand").json()
    assert {"status", "words", "found", "samples", "fingerprint", "messages_read", "colours"} <= set(brand)
