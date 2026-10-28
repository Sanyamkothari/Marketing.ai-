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
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.access_policy import all_policies
from api.deps import ConfigRootDep, SettingsDep, StorageDep
from api.main import create_app
from api.routes.agentic import get_planner_deps
from engine.access.roles import Role
from engine.agentic.ledger import DecisionLedger, signer_for_data_dir, verify_ledger
from engine.agentic.planner import PlannerDeps, PlannerState, load_plan
from engine.agentic.policy import CampaignPlanPayload
from engine.agentic.tools import default_registry
from engine.config import BudgetConfig, LlmConfig
from engine.decide.treat_list import TREAT_LIST_SUMMARY_FILENAME
from engine.generative.budget import Meter
from engine.measurement.plan import TestPlan
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.settings import Settings
from engine.storage import run_key
from engine.utils.time import utc_now
from tests.agentic.scenarios.planner_scenarios import expected_digest
from tests.agentic.scenarios.planner_world import HonestModel, PlannerWorld
from tests.ai_consent_support import consented

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
    assert {
        ("POST", "/agentic/plans"),
        ("GET", "/agentic/plans/{plan_id}"),
        ("POST", "/agentic/plans/{plan_id}/resume"),
    } <= routes
    policies = all_policies()
    assert policies[("POST", "/agentic/plans")].role is Role.ANALYST
    assert policies[("GET", "/agentic/plans/{plan_id}")].role is Role.VIEWER
    assert policies[("POST", "/agentic/plans/{plan_id}/resume")].role is Role.ANALYST
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
        assert proposal["payload_digest"] == expected_digest(pw, objective="larger_test", now=utc_now())
        # The test is sized on everyone the campaign compares (the eligible rows and the held-back group),
        # as the product's own preview sizes it - not on the eligible rows, which would take the holdout twice.
        contact = plan["evidence"]["check_contactable"]["output"]
        (point,) = plan["evidence"]["plan_test"]["output"]["points"]
        assert contact["held_back_rows"] > 0
        assert point["n_treat"] + point["n_control"] == contact["eligible_rows"] + contact["held_back_rows"]
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


def _with_model(model: Any) -> Any:
    """`get_planner_deps` with `model` as the Product AI (FastAPI's dependency override: no hook in the API)."""

    def deps() -> PlannerDeps:
        meter = Meter(model, job_id="t", llm=LlmConfig(), budget=BudgetConfig(cache=False))
        return PlannerDeps(registry=default_registry(), meter=meter)

    return deps


def test_words_read_by_the_model_give_the_forms_plan(planner_world: PlannerWorld) -> None:
    pw = planner_world
    app = create_app(config_root=pw.world.config_root, data_dir=pw.world.data_dir)
    app.dependency_overrides[get_planner_deps] = _with_model(HonestModel())
    with TestClient(app) as client:
        words = client.post(
            "/agentic/plans", json={"goal_text": "Win back the customers who lapsed, by sms."}
        )
        assert words.status_code == 201, words.text
        assert words.json()["proposal"]["payload_digest"] == expected_digest(
            pw, objective="next_cycle", now=utc_now()
        )
        assert words.json()["plan"]["frame"]["source"] == "words"


def test_a_narrative_edited_after_the_draft_is_refused_as_tampered(planner_world: PlannerWorld) -> None:
    """The wording shown under a proposal is checked like its payload: its digest is on the ledger."""
    from engine.agentic.proposals import ProposalStore

    pw = planner_world
    store = ProposalStore(pw.world.storage)
    with pw.world.client() as client:
        drafted = client.post("/agentic/plans", json=_card(pw))
        assert drafted.status_code == 201, drafted.text
        proposal = store.get(drafted.json()["proposal"]["proposal_id"])
        assert proposal.narrative is not None
        forged = {
            **proposal.narrative,
            "sentences": [{"text": "Sales rose 40%.", "claim_type": None, "refs": []}],
        }
        store.save(proposal.model_copy(update={"narrative": forged}))
        response = client.post(f"/agentic/proposals/{proposal.proposal_id}/approve", json={})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "PROPOSAL_TAMPERED"
        assert client.get("/campaigns", params={"run_id": pw.run_id}).json()["campaigns"] == []


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


def _plan_ids(pw: PlannerWorld) -> set[str]:
    from engine.agentic.planner import PLANS_PREFIX

    return {key.split("/")[1] for key in pw.world.storage.list_keys(PLANS_PREFIX)}


def test_a_plan_a_crash_left_part_way_resumes_through_its_route(
    fresh_planner_world: PlannerWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each step is written as it completes; a worker stopped mid-plan leaves the plan at its last step, and
    `POST /agentic/plans/{id}/resume` carries it on to the interrupt (design §6: a crash resumes)."""
    import engine.agentic.planner as planner

    pw = fresh_planner_world
    real = planner._candidates

    def crash(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the worker was stopped")

    before = _plan_ids(pw)
    monkeypatch.setattr(planner, "_candidates", crash)
    with TestClient(
        create_app(config_root=pw.world.config_root, data_dir=pw.world.data_dir),
        raise_server_exceptions=False,
    ) as client:
        assert client.post("/agentic/plans", json=_card(pw)).status_code == 500
        (plan_id,) = _plan_ids(pw) - before
        assert client.get(f"/agentic/plans/{plan_id}").json()["plan"]["step"] == "SIZE"
        monkeypatch.setattr(planner, "_candidates", real)
        resumed = client.post(f"/agentic/plans/{plan_id}/resume")
        assert resumed.status_code == 200, resumed.text
        body = resumed.json()
        assert body["plan"]["step"] == "INTERRUPT" and body["proposal"]["status"] == "pending"
        assert body["proposal"]["payload_digest"] == expected_digest(
            pw, objective="larger_test", now=utc_now()
        )
        again = client.post(f"/agentic/plans/{plan_id}/resume").json()
        assert again["plan"] == body["plan"], "a plan at its interrupt is answered as it is"
        assert client.post("/agentic/plans/plan_0000000000000000/resume").status_code == 404


def test_a_crash_inside_draft_resumes_to_the_same_proposal(
    fresh_planner_world: PlannerWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the proposal was drafted but before the plan recorded it: the resumed DRAFT reuses that
    proposal (its id was written first), so there is one pending proposal and approving it carries it out."""
    import engine.agentic.proposals as proposals
    from engine.agentic.proposals import ProposalStore

    pw = fresh_planner_world
    real = proposals.submit_proposal

    def drafted_then_crash(*args: Any, **kwargs: Any) -> Any:
        real(*args, **kwargs)
        raise RuntimeError("the worker was stopped after drafting")

    before = _plan_ids(pw)
    monkeypatch.setattr(proposals, "submit_proposal", drafted_then_crash)
    with TestClient(
        create_app(config_root=pw.world.config_root, data_dir=pw.world.data_dir),
        raise_server_exceptions=False,
    ) as client:
        form = {"goal": {"use_case_id": "win-back-campaign", "channel": "sms"}}
        assert client.post("/agentic/plans", json=form).status_code == 500
        (plan_id,) = _plan_ids(pw) - before
        halted = client.get(f"/agentic/plans/{plan_id}").json()["plan"]
        assert halted["step"] == "CHECK" and halted["proposal_id"] is None and halted["reserved_proposal_id"]
        monkeypatch.setattr(proposals, "submit_proposal", real)
        body = client.post(f"/agentic/plans/{plan_id}/resume").json()
        proposal_id = body["proposal"]["proposal_id"]
        assert proposal_id == halted["reserved_proposal_id"] and body["plan"]["step"] == "INTERRUPT"
        mine = [
            p for p in ProposalStore(pw.world.storage).list(status="pending") if p.proposal_id == proposal_id
        ]
        drafts = [e for e in _entries(pw, proposal_id) if e.kind == "proposal.drafted"]
        assert len(mine) == 1 and len(drafts) == 1, "one proposal, drafted once"
        receipt = client.post(f"/agentic/proposals/{proposal_id}/approve", json={})
        assert receipt.status_code == 200, receipt.text
        assert len(client.get("/campaigns", params={"run_id": pw.run_id}).json()["campaigns"]) == 1


def test_the_planner_meters_under_the_use_cases_own_ai_and_budget(
    planner_world: PlannerWorld, tmp_path: Any
) -> None:
    """The narrative is metered as every metered route meters (the use case's `generative.llm` and `budget`):
    a use case allowing one call per run gets a Meter that refuses the second, and a deployment whose AI comes
    from its use-case configuration (here the test model, `allow_fake_ai`) is not refused as not connected."""
    import shutil

    import yaml

    from engine.generative.contracts import GenerativePurpose
    from engine.generative.errors import GenerativeError
    from engine.generative.prompts import load_prompt, render

    pw = planner_world
    root = tmp_path / "configs"
    shutil.copytree(pw.world.config_root, root)
    path = root / "use_cases" / "win_back_campaign.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["generative"]["budget"] = {"max_calls_per_run": 1, "cache": False}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    settings = Settings(connections_key=SecretStr("F" * 40), allow_fake_ai=True)
    app = create_app(config_root=root, data_dir=consented(pw.world.data_dir), settings=settings)
    seen: list[PlannerDeps] = []

    def capture(
        request: Request, storage: StorageDep, root: ConfigRootDep, settings: SettingsDep
    ) -> PlannerDeps:
        deps = get_planner_deps(request, storage, root, settings)
        seen.append(deps)
        return deps

    app.dependency_overrides[get_planner_deps] = capture
    with TestClient(app) as client:
        card = client.post("/agentic/plans", json=_card(pw))
        assert card.status_code == 201, card.text
        plan = card.json()["plan"]
        assert plan["llm_calls"] == 1 and plan["narrative_error"] is None, "the use case's model wrote"
        words = client.post("/agentic/plans", json={"goal_text": "Win back lapsed customers by sms."})
        assert words.json()["detail"]["code"] != "AI_NOT_CONNECTED", words.text
    meter = seen[0].meter_for("win-back-campaign") if seen[0].meter_for else None
    assert meter is not None and meter.remaining_calls() == 1
    prompt = render(load_prompt("agent_narrative", root), {"evidence": "{}", "state": "{}"})
    meter.complete(prompt, GenerativePurpose.AGENT_NARRATIVE)
    with pytest.raises(GenerativeError) as refused:
        meter.complete(prompt, GenerativePurpose.AGENT_NARRATIVE)
    assert refused.value.code == "BUDGET_EXCEEDED"
