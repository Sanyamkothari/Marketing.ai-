"""The campaign planner through its public routes (Plan K M139 acceptance).

A warning card on the Results page becomes a drafted plan at `POST /agentic/plans`; a person approves it at
`POST /agentic/proposals/{id}/approve`; the run's treat list is then built, its campaign opened and the test
plan the proposal carried frozen - through the product's own functions - and the ledger records the draft, the
frozen plan, the execution and the approval, and verifies. The other paths: words without an AI service, words
with one, a card that is not there, a retried approval after a failure part-way (no second campaign), the
routes' policies, the switch off.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.access_policy import all_policies
from api.main import create_app
from engine.access.roles import Role
from engine.agentic.ledger import DecisionLedger, signer_for_data_dir, verify_ledger
from engine.agentic.planner import PlannerState, load_plan
from engine.agentic.policy import CampaignPlanPayload
from engine.decide.treat_list import TREAT_LIST_SUMMARY_FILENAME
from engine.measurement.plan import TestPlan
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.settings import Settings
from engine.storage import run_key
from tests.agentic.scenarios.planner_scenarios import expected_digest
from tests.agentic.scenarios.planner_world import HonestModel, PlannerWorld

pytestmark = pytest.mark.integration


def _entries(pw: PlannerWorld, proposal_id: str) -> list[Any]:
    ledger = DecisionLedger(
        sqlite_engine(pw.world.data_dir / PLATFORM_DB_FILENAME), signer=None, policy_version="unused"
    )
    return [entry for entry in ledger.entries() if entry.body.get("proposal_id") == proposal_id]


def _card(pw: PlannerWorld) -> dict[str, Any]:
    return {"card": {"code": "PLAN_UNDERPOWERED", "campaign_id": pw.world.planned_campaign}}


def test_the_planner_routes_exist_with_their_policies(planner_world: PlannerWorld) -> None:
    from tests.integration.production.access_support import live_routes

    with planner_world.world.client() as client:
        routes = {(route.method, route.path) for route in live_routes(client.app)}  # type: ignore[arg-type]
    assert {("POST", "/agentic/plans"), ("GET", "/agentic/plans/{plan_id}")} <= routes
    policies = all_policies()
    assert policies[("POST", "/agentic/plans")].role is Role.ANALYST
    assert policies[("GET", "/agentic/plans/{plan_id}")].role is Role.VIEWER
    assert not any("approve" in path and "plans" in path for _, path in routes), "no route approves a plan"


def test_a_card_becomes_a_plan_that_an_approval_carries_out(fresh_planner_world: PlannerWorld) -> None:
    pw = fresh_planner_world
    storage = pw.world.storage
    with pw.world.client() as client:
        assert any(
            card["code"] == "PLAN_UNDERPOWERED" for card in client.get("/campaigns/summary").json()["cards"]
        )
        response = client.post("/agentic/plans", json=_card(pw))
        assert response.status_code == 201, response.text
        body = response.json()
        plan, proposal = body["plan"], body["proposal"]
        assert plan["step"] == "INTERRUPT" and proposal["status"] == "pending"
        assert proposal["payload_digest"] == expected_digest(pw, objective="larger_test")
        assert set(proposal["evidence_ids"]) == {item["evidence_id"] for item in plan["evidence"].values()}
        assert proposal["narrative"]["sentences"], "a narrative is shown under the plan"
        # Nothing is made by a draft: no treat list, no campaign for the run.
        assert not storage.exists(run_key(pw.run_id, TREAT_LIST_SUMMARY_FILENAME))
        assert client.get("/campaigns", params={"run_id": pw.run_id}).json()["campaigns"] == []
        assert client.get(f"/agentic/plans/{plan['plan_id']}").json()["plan"]["step"] == "INTERRUPT"

        receipt = client.post(f"/agentic/proposals/{proposal['proposal_id']}/approve", json={"reason": "Go."})
        assert receipt.status_code == 200, receipt.text
        result = receipt.json()
        assert result["status"] == "approved"

        assert storage.exists(run_key(pw.run_id, TREAT_LIST_SUMMARY_FILENAME)), "the treat list was built"
        campaigns = client.get("/campaigns", params={"run_id": pw.run_id}).json()["campaigns"]
        assert len(campaigns) == 1
        campaign_id = campaigns[0]["campaign_id"]
        frozen = TestPlan.model_validate(client.get(f"/campaigns/{campaign_id}/plan").json()["plan"])
        assert frozen.plan_hash == result["result_ref"] == campaigns[0]["test_plan_hash"]
        proposed = CampaignPlanPayload.model_validate(proposal["payload"]).test_plan
        assert proposed is not None
        assert frozen.analysis_date == proposed.analysis_date and frozen.mde_pp == proposed.mde_pp
        assert frozen.holdout_fraction > 0.0, "the holdout in force was kept"
        done = client.get(f"/agentic/plans/{plan['plan_id']}").json()["plan"]
        assert done["step"] == "DONE" and done["execution"]["campaign_id"] == campaign_id
        assert done["execution"]["measure_on"] == proposed.analysis_date.isoformat()

        again = client.post(f"/agentic/proposals/{proposal['proposal_id']}/approve", json={})
        assert again.status_code == 409 and again.json()["detail"]["code"] == "PROPOSAL_NOT_PENDING"
        assert len(client.get("/campaigns", params={"run_id": pw.run_id}).json()["campaigns"]) == 1

    kinds = [entry.kind for entry in _entries(pw, proposal["proposal_id"])]
    assert kinds == ["proposal.drafted", "test_plan.frozen", "campaign_plan.executed", "proposal.approved"]
    drafted, frozen_entry, executed = _entries(pw, proposal["proposal_id"])[:3]
    assert drafted.body["plan_id"] == plan["plan_id"] and "narrative_digest" in drafted.body
    assert frozen_entry.signature and frozen_entry.body["holdout_scope"] == "run"
    assert executed.body["campaign_id"] == campaign_id and executed.body["run_id"] == pw.run_id
    engine = sqlite_engine(pw.world.data_dir / PLATFORM_DB_FILENAME)
    assert verify_ledger(engine, signer_for_data_dir(pw.world.data_dir, create=False)).ok


def test_an_approval_retried_after_a_failure_does_not_open_a_second_campaign(
    fresh_planner_world: PlannerWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    import api.routes.campaigns as campaigns_routes
    from api.routes.uploads import http_error

    pw = fresh_planner_world
    original = campaigns_routes.register_plan
    calls: list[str] = []

    def failing_once(campaign_id: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(campaign_id)
        if len(calls) == 1:
            raise http_error(503, "STORAGE_UNAVAILABLE", "The store did not answer.")
        return original(campaign_id, *args, **kwargs)

    monkeypatch.setattr(campaigns_routes, "register_plan", failing_once)
    with pw.world.client() as client:
        drafted = client.post(
            "/agentic/plans", json={"goal": {"use_case_id": "win-back-campaign", "channel": "sms"}}
        )
        assert drafted.status_code == 201, drafted.text
        proposal_id = drafted.json()["proposal"]["proposal_id"]
        first = client.post(f"/agentic/proposals/{proposal_id}/approve", json={})
        assert first.status_code == 503 and first.json()["detail"]["code"] == "STORAGE_UNAVAILABLE"
        assert client.get(f"/agentic/proposals/{proposal_id}").json()["status"] == "pending"
        second = client.post(f"/agentic/proposals/{proposal_id}/approve", json={})
        assert second.status_code == 200, second.text
        campaigns = client.get("/campaigns", params={"run_id": pw.run_id}).json()["campaigns"]
    assert len(campaigns) == 1 and calls == [campaigns[0]["campaign_id"]] * 2


def test_words_need_an_ai_service_and_a_card_does_not(planner_world: PlannerWorld) -> None:
    pw = planner_world
    settings = Settings(connections_key=SecretStr("F" * 40), allow_fake_ai=False)
    app = create_app(config_root=pw.world.config_root, data_dir=pw.world.data_dir, settings=settings)
    with TestClient(app) as client:
        words = client.post("/agentic/plans", json={"goal_text": "Win back lapsed customers by sms."})
        assert words.status_code == 409 and words.json()["detail"]["code"] == "AI_NOT_CONNECTED"
        card = client.post("/agentic/plans", json=_card(pw))
        assert card.status_code == 201, card.text
        narrative = card.json()["proposal"]["narrative"]
        assert narrative["source"] == "rules" and narrative["failures"] == []


def test_words_read_by_the_model_give_the_forms_plan(planner_world: PlannerWorld) -> None:
    pw = planner_world
    app = create_app(config_root=pw.world.config_root, data_dir=pw.world.data_dir)
    app.state.agentic_llm_client = HonestModel()
    with TestClient(app) as client:
        words = client.post(
            "/agentic/plans", json={"goal_text": "Win back the customers who lapsed, by sms."}
        )
        assert words.status_code == 201, words.text
        assert words.json()["proposal"]["payload_digest"] == expected_digest(pw, objective="next_cycle")
        assert words.json()["plan"]["frame"]["source"] == "words"


def test_a_card_that_is_not_there_or_asks_for_waiting_plans_nothing(planner_world: PlannerWorld) -> None:
    pw = planner_world
    with pw.world.client() as client:
        gone = client.post(
            "/agentic/plans", json={"card": {"code": "DRIFT_DRIFTED", "use_case_id": "win-back-campaign"}}
        )
        assert gone.status_code == 404 and gone.json()["detail"]["code"] == "PLANNER_CARD_NOT_FOUND"
        both = client.post("/agentic/plans", json={**_card(pw), "goal": {"use_case_id": "win-back-campaign"}})
        assert both.status_code == 422
        unknown = client.get("/agentic/plans/plan_0000000000000000")
        assert unknown.status_code == 404 and unknown.json()["detail"]["code"] == "PLANNER_NOT_FOUND"


def test_a_plan_is_kept_after_every_step(planner_world: PlannerWorld) -> None:
    from engine.agentic.planner import PlannerDeps, PlanRequest, start_plan
    from engine.agentic.tools import default_registry

    state = start_plan(
        PlanRequest.model_validate({"goal": {"use_case_id": "win-back-campaign"}}),
        planner_world.world.context(),
        PlannerDeps(registry=default_registry()),
    )
    stored = load_plan(planner_world.world.context(), state.plan_id)
    assert isinstance(stored, PlannerState) and stored == state
    assert [t.step for t in state.history] == [
        "FRAME",
        "SIZE",
        "CANDIDATES",
        "SELECT",
        "CHECK",
        "DRAFT",
        "INTERRUPT",
    ]
    assert [(t.channel, t.codes) for t in state.tried] == [("email", ("POLICY_FREQUENCY_CAP",)), ("sms", ())]
    assert state.selected is not None and state.selected.channel == "sms"


def test_with_the_switch_off_there_is_no_planner_route(planner_world: PlannerWorld, tmp_path: Any) -> None:
    import shutil

    root = tmp_path / "configs"
    shutil.copytree(planner_world.world.config_root, root)
    (root / "agentic" / "agentic.yaml").write_text("agentic:\n  enabled: false\n", encoding="utf-8")
    with TestClient(create_app(config_root=root, data_dir=planner_world.world.data_dir)) as client:
        assert client.post("/agentic/plans", json=_card(planner_world)).status_code == 404
