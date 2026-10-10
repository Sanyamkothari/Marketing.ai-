"""Approving an agent's proposal through the public route freezes a real test plan (Plan K M132 acceptance).

An agent drafts a test plan through the tool registry (`draft_test_plan`, freeze_plan in dry mode: nothing is
registered); a person approves it at `POST /agentic/proposals/{id}/approve`; the plan the campaign then holds
is the one proposed, frozen through the same function `POST /campaigns/{id}/plan` runs - so registering the
same body again answers 200 with that very plan. The ledger records the draft, the frozen plan and the
approval (both signed) and verifies, and the approval receipt carries the ledger head (the client's
checkpoint). The other paths: reject, a plan registered meanwhile, a proposal edited after it was drafted
(whatever the edit), a plan the route would not freeze as proposed, expiry, an unknown id.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from typing import Any

import pytest

from api.access_policy import all_policies
from engine.access.roles import Role
from engine.agentic.errors import AgenticError
from engine.agentic.ledger import signer_for_data_dir, verify_ledger
from engine.agentic.proposals import Proposal, ProposalStore, proposal_key
from engine.agentic.tools import default_registry
from engine.approvals import decide_proposal
from engine.audit.events import AuditQuery, content_hash
from engine.audit.store import SqlAuditLog
from engine.measurement.plan import TestPlan
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from tests.agentic.scenarios.world import NOW, PRINCIPALS, World, scoring_frame

pytestmark = pytest.mark.integration


def _draft(world: World, campaign_id: str, **extra: Any) -> dict[str, Any]:
    result = default_registry().call(
        "draft_test_plan", {"campaign_id": campaign_id, "plan": world.plan_body(), **extra}, world.context()
    )
    return result.output


def _ledger_entries(world: World, proposal_id: str) -> list[Any]:
    from engine.agentic.ledger import DecisionLedger

    ledger = DecisionLedger(
        sqlite_engine(world.data_dir / PLATFORM_DB_FILENAME), signer=None, policy_version="unused"
    )
    return [entry for entry in ledger.entries() if entry.body.get("proposal_id") == proposal_id]


def test_the_routes_exist_with_their_policies(agentic_world: World) -> None:
    from tests.integration.production.access_support import live_routes

    with agentic_world.client() as client:
        routes = {(route.method, route.path) for route in live_routes(client.app)}  # type: ignore[arg-type]
    ours = {key for key in routes if key[1].startswith("/agentic")}
    assert ours == {
        ("GET", "/agentic/proposals"),
        ("GET", "/agentic/proposals/{proposal_id}"),
        ("POST", "/agentic/proposals/{proposal_id}/approve"),
        ("POST", "/agentic/proposals/{proposal_id}/reject"),
    }
    policies = all_policies()
    assert policies[("POST", "/agentic/proposals/{proposal_id}/approve")].role is Role.APPROVER
    assert policies[("POST", "/agentic/proposals/{proposal_id}/reject")].role is Role.APPROVER
    assert policies[("GET", "/agentic/proposals")].role is Role.VIEWER
    assert not any(path.startswith("/agentic/tools") for _, path in routes), "no route drafts for an agent"


def test_approving_a_draft_freezes_the_proposed_plan_through_the_route(agentic_world: World) -> None:
    world = agentic_world
    with world.client() as client:
        campaign_id = world.new_campaign(client)
        draft = _draft(world, campaign_id)
        assert draft["status"] == "pending" and draft["codes"] == []
        proposal_id = draft["proposal_id"]
        assert (
            client.get(f"/campaigns/{campaign_id}/plan").json()["plan"] is None
        ), "nothing frozen by a draft"

        listed = client.get("/agentic/proposals", params={"status": "pending"}).json()["proposals"]
        assert proposal_id in {item["proposal_id"] for item in listed}
        shown = Proposal.model_validate(client.get(f"/agentic/proposals/{proposal_id}").json())
        assert shown.payload["plan_hash"] == draft["plan_hash"] and shown.status == "pending"

        response = client.post(f"/agentic/proposals/{proposal_id}/approve", json={"reason": "Looks right."})
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["status"] == "approved" and receipt["result_ref"] == draft["plan_hash"]

        plan = TestPlan.model_validate(client.get(f"/campaigns/{campaign_id}/plan").json()["plan"])
        assert plan.plan_hash == draft["plan_hash"] and plan.version == 1
        again = client.post(f"/campaigns/{campaign_id}/plan", json=world.plan_body())
        assert (
            again.status_code == 200 and TestPlan.model_validate(again.json()) == plan
        ), "the route's own path"
        campaign = client.get(f"/campaigns/{campaign_id}").json()["campaign"]
        assert campaign["test_plan_hash"] == plan.plan_hash

        second = client.post(f"/agentic/proposals/{proposal_id}/approve", json={})
        assert second.status_code == 409 and second.json()["detail"]["code"] == "PROPOSAL_NOT_PENDING"

    kinds = [entry.kind for entry in _ledger_entries(world, proposal_id)]
    assert kinds == ["proposal.drafted", "test_plan.frozen", "proposal.approved"]
    frozen, approved = _ledger_entries(world, proposal_id)[1:]
    assert frozen.signature and approved.signature
    assert frozen.body["plan_hash"] == plan.plan_hash and frozen.body["holdout_scope"] == "run"
    assert approved.entry_hash == receipt["ledger_checkpoint"]
    assert "reason_digest" in approved.body and "Looks right." not in json.dumps(approved.body)
    engine = sqlite_engine(world.data_dir / PLATFORM_DB_FILENAME)
    assert verify_ledger(engine, signer_for_data_dir(world.data_dir, create=False)).ok

    events = list(SqlAuditLog(engine).query(AuditQuery(object_id=proposal_id)))
    approve = [event for event in events if event.action == "agentic.proposal_approve"]
    assert len(approve) == 2, "both approval attempts are audited"
    assert any(event.after_hash == content_hash(plan) for event in approve)


def test_rejecting_freezes_nothing(agentic_world: World) -> None:
    world = agentic_world
    with world.client() as client:
        campaign_id = world.new_campaign(client)
        proposal_id = _draft(world, campaign_id)["proposal_id"]
        response = client.post(f"/agentic/proposals/{proposal_id}/reject", json={"reason": "Not this month."})
        assert response.status_code == 200 and response.json()["status"] == "rejected"
        assert client.get(f"/campaigns/{campaign_id}/plan").json()["plan"] is None
        approve = client.post(f"/agentic/proposals/{proposal_id}/approve", json={})
        assert approve.status_code == 409 and approve.json()["detail"]["code"] == "PROPOSAL_NOT_PENDING"
    assert [entry.kind for entry in _ledger_entries(world, proposal_id)] == [
        "proposal.drafted",
        "proposal.rejected",
    ]


def test_an_approval_never_overwrites_a_plan_registered_meanwhile(agentic_world: World) -> None:
    """The policy is checked again at approval: another plan frozen meanwhile refuses it, nothing is replaced."""
    world = agentic_world
    with world.client() as client:
        campaign_id = world.new_campaign(client)
        proposal_id = _draft(world, campaign_id)["proposal_id"]
        other = client.post(f"/campaigns/{campaign_id}/plan", json=world.plan_body(mde_pp=3.0))
        assert other.status_code == 201
        response = client.post(f"/agentic/proposals/{proposal_id}/approve", json={})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "POLICY_TEST_PLAN_FIXED"
        assert (
            client.get(f"/campaigns/{campaign_id}/plan").json()["plan"]["plan_hash"]
            == other.json()["plan_hash"]
        )
        assert client.get(f"/agentic/proposals/{proposal_id}").json()["status"] == "refused"
    entries = _ledger_entries(world, proposal_id)
    assert [entry.kind for entry in entries] == ["proposal.drafted", "proposal.refused"]
    assert entries[-1].body["codes"] == ["POLICY_TEST_PLAN_FIXED"]


def test_a_plan_for_a_campaign_with_another_plan_or_already_measured_is_refused_at_draft(
    agentic_world: World, measured: str
) -> None:
    """Nobody is asked to approve what could not be registered (TEST_PLAN_EXISTS / TEST_PLAN_INVALID later)."""
    world = agentic_world
    other = _draft(world, world.planned_campaign, plan=world.plan_body(mde_pp=3.0))
    assert other["status"] == "refused" and other["codes"] == ["POLICY_TEST_PLAN_FIXED"]
    late = _draft(world, measured)
    assert late["status"] == "refused" and late["codes"] == ["POLICY_TEST_PLAN_FIXED"]
    same = _draft(world, world.planned_campaign)
    assert same["status"] == "pending" and same["plan_hash"] == world.plan_hash, "the frozen plan, again"


def test_a_proposal_edited_after_it_was_drafted_is_refused_as_tampered(agentic_world: World) -> None:
    world = agentic_world
    with world.client() as client:
        campaign_id = world.new_campaign(client)
        proposal_id = _draft(world, campaign_id)["proposal_id"]
        store = ProposalStore(world.storage)
        tampered = store.get(proposal_id)
        store.save(tampered.model_copy(update={"payload": {**tampered.payload, "plan_hash": "0" * 64}}))
        response = client.post(f"/agentic/proposals/{proposal_id}/approve", json={})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "PROPOSAL_TAMPERED"
        assert client.get(f"/campaigns/{campaign_id}/plan").json()["plan"] is None
        assert client.get(f"/agentic/proposals/{proposal_id}").json()["status"] == "refused"


def _consistently_edited(world: World, proposal: Proposal, *, digest_too: bool) -> Proposal:
    """The proposal with a bigger smallest-effect-worth-detecting and its plan_hash recomputed by the product's
    own dry freeze - an edit that keeps the file consistent with itself (and, with `digest_too`, with its own
    payload_digest)."""
    from engine.agentic.canonical import digest
    from engine.agentic.proposals import dry_test_plan
    from engine.measurement.plan import TestPlanInput

    ctx = world.context()
    assert ctx.campaigns is not None
    campaign = ctx.campaigns.get(proposal.payload["campaign_id"])
    assert campaign is not None
    plan = TestPlanInput.model_validate({**proposal.payload["plan"], "mde_pp": 12.0})
    frozen, _realised = dry_test_plan(ctx, campaign, plan)
    payload = {**proposal.payload, "plan": plan.model_dump(mode="json"), "plan_hash": frozen.plan_hash}
    update: dict[str, Any] = {"payload": payload}
    if digest_too:
        update["payload_digest"] = digest(payload)
    return proposal.model_copy(update=update)


@pytest.mark.parametrize("digest_too", [False, True], ids=["payload", "payload-and-digest"])
def test_a_consistently_edited_proposal_is_not_what_gets_frozen(
    agentic_world: World, digest_too: bool
) -> None:
    """Review finding (M132): the approval ran whatever the stored file said, so an edit that recomputed
    plan_hash froze mde_pp=12 while the receipt kept the drafted payload_digest. The file must match the
    proposal.drafted entry on the ledger."""
    world = agentic_world
    with world.client() as client:
        campaign_id = world.new_campaign(client)
        draft = _draft(world, campaign_id)
        store = ProposalStore(world.storage)
        edited = _consistently_edited(world, store.get(draft["proposal_id"]), digest_too=digest_too)
        assert edited.payload["plan_hash"] != draft["plan_hash"]
        store.save(edited)
        response = client.post(f"/agentic/proposals/{draft['proposal_id']}/approve", json={})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "PROPOSAL_TAMPERED"
        assert client.get(f"/campaigns/{campaign_id}/plan").json()["plan"] is None, "nothing frozen"
    entries = _ledger_entries(world, draft["proposal_id"])
    assert [entry.kind for entry in entries] == ["proposal.drafted", "proposal.refused"]
    assert entries[-1].body["codes"] == ["PROPOSAL_TAMPERED"]
    assert entries[-1].body["payload_digest"] == draft["payload_digest"], "the digest the ledger recorded"


def test_a_proposal_reassigned_to_another_proposer_is_refused(agentic_world: World) -> None:
    """Editing who it was proposed for would let its proposer approve it: the ledger says who it was."""
    world = agentic_world
    with world.client() as client:
        campaign_id = world.new_campaign(client)
    draft = _draft(world, campaign_id)
    store = ProposalStore(world.storage)
    store.save(store.get(draft["proposal_id"]).model_copy(update={"proposed_by": "u_somebody_else"}))
    with pytest.raises(AgenticError) as caught:
        decide_proposal(
            draft["proposal_id"],
            "approved",
            principal=PRINCIPALS["analyst"],
            reason=None,
            tool_ctx=world.context(),
            execute=lambda _proposal: pytest.fail("a tampered proposal is never executed"),
        )
    assert caught.value.code == "PROPOSAL_TAMPERED" and "proposed_by" in caught.value.message


def test_an_execution_that_freezes_another_plan_is_not_an_approval(agentic_world: World) -> None:
    """If the route freezes (or keeps) another plan than the one proposed, the proposal is refused as stale."""
    world = agentic_world
    with world.client() as client:
        campaign_id = world.new_campaign(client)
    draft = _draft(world, campaign_id)
    with pytest.raises(AgenticError) as caught:
        decide_proposal(
            draft["proposal_id"],
            "approved",
            principal=PRINCIPALS["approver"],
            reason=None,
            tool_ctx=world.context(principal="approver"),
            execute=lambda _proposal: "9" * 64,
        )
    assert caught.value.code == "PROPOSAL_STALE"
    entries = _ledger_entries(world, draft["proposal_id"])
    assert [entry.kind for entry in entries] == ["proposal.drafted", "test_plan.frozen", "proposal.refused"]
    assert entries[-1].body["result_ref"] == "9" * 64 and entries[-1].body["codes"] == ["PROPOSAL_STALE"]
    assert ProposalStore(world.storage).get(draft["proposal_id"]).status == "refused"


def test_a_refused_draft_and_an_unknown_id_cannot_be_approved(agentic_world: World) -> None:
    world = agentic_world
    with world.client() as client:
        refused = _draft(world, world.open_campaign, requested_holdout_fraction=0.01)
        assert refused["status"] == "refused" and refused["codes"] == ["POLICY_HOLDOUT_IMMUTABLE"]
        response = client.post(f"/agentic/proposals/{refused['proposal_id']}/approve", json={})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "PROPOSAL_NOT_PENDING"
        missing = client.get("/agentic/proposals/prop_doesnotexist")
        assert missing.status_code == 404 and missing.json()["detail"]["code"] == "PROPOSAL_NOT_FOUND"
        path = client.get("/agentic/proposals/..%2F..%2Fruns")
        assert path.status_code == 404


def test_a_proposal_nobody_decided_expires(agentic_world: World) -> None:
    world = agentic_world
    with world.client() as client:
        campaign_id = world.new_campaign(client)
    proposal_id = _draft(world, campaign_id)["proposal_id"]
    later = dataclasses.replace(world.context(principal="approver"), clock=lambda: NOW + timedelta(days=15))
    with pytest.raises(AgenticError) as caught:
        decide_proposal(
            proposal_id,
            "approved",
            principal=PRINCIPALS["approver"],
            reason=None,
            tool_ctx=later,
            execute=lambda _proposal: None,
        )
    assert caught.value.code == "PROPOSAL_EXPIRED"
    assert ProposalStore(world.storage).get(proposal_id).status == "expired"


def test_no_proposal_or_ledger_entry_holds_a_customer(agentic_world: World) -> None:
    world = agentic_world
    keys = {str(key) for key in scoring_frame()["customer_id"]}
    texts = [world.storage.read_text(key) for key in world.storage.list_keys("agentic/proposals/")]
    engine = sqlite_engine(world.data_dir / PLATFORM_DB_FILENAME)
    with engine.connect() as connection:
        from sqlalchemy import text

        texts += [str(row[0]) for row in connection.execute(text("SELECT body FROM agentic_decision_log"))]
    assert texts, "the suite drafted proposals"
    tokens = {token for item in texts for token in json.dumps(json.loads(item)).replace('"', " ").split()}
    assert not keys & tokens
    assert all("customer_id" not in item for item in texts)
    assert proposal_key("prop_x").startswith("agentic/proposals/")


def test_an_approved_campaign_plan_waits_for_a_path_to_carry_it_out(agentic_world: World) -> None:
    """M132 checks campaign plans; nothing executes one yet (M139), so approving leaves it pending, on record."""
    from engine.agentic.proposals import submit_proposal
    from tests.agentic.scenarios.scenarios import campaign_payload

    world = agentic_world
    proposal = submit_proposal("campaign_plan", campaign_payload(world), tool_ctx=world.context())
    assert proposal.status == "pending"
    with world.client() as client:
        response = client.post(f"/agentic/proposals/{proposal.proposal_id}/approve", json={})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "PROPOSAL_NOT_EXECUTABLE"
        assert client.get(f"/agentic/proposals/{proposal.proposal_id}").json()["status"] == "pending"
    kinds = [entry.kind for entry in _ledger_entries(world, proposal.proposal_id)]
    assert kinds == ["proposal.drafted", "proposal.execution_failed"]
