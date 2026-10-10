"""The read tools on a real run and real campaigns: the same numbers as the product's own routes, read once.

Each tool reads what the product already computed and stored (`compute once`); what is not computed yet is
null with a plain reason, never a 0. Every output is aggregate and fits the 16 KB budget.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.agentic.canonical import canonical_bytes
from engine.agentic.errors import AgenticError
from engine.agentic.registry import DEFAULT_OUTPUT_BUDGET
from engine.agentic.tools import default_registry
from engine.storage import run_key
from tests.agentic.scenarios.world import SCORE_ROWS, TRAIN_RUN, World
from tests.fixtures.make_uplift_data import make_winback_campaign, outcomes_for

pytestmark = pytest.mark.integration


def _call(world: World, tool: str, given: dict[str, Any]) -> Any:
    result = default_registry().call(tool, given, world.context(principal="viewer"))
    assert len(canonical_bytes(result.output)) <= DEFAULT_OUTPUT_BUDGET
    return result


def _upload(client: TestClient, frame: Any) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    response = client.post(
        "/uploads",
        files={"file": ("outcomes.csv", payload, "text/csv")},
        data={"use_case": "win-back-campaign", "mode": "score"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


@pytest.fixture(scope="module")
def measured(agentic_world: World) -> str:
    """A campaign of the world's run, every outcome in at once (a 0-day window), measured through the route."""
    world = agentic_world
    with world.client() as client:
        response = client.post("/campaigns", json={"run_id": world.run_id, "outcome_window_days": 0})
        assert response.status_code == 201, response.text
        campaign_id = str(response.json()["campaign"]["campaign_id"])
        dataset = make_winback_campaign(SCORE_ROWS, seed=11)
        outcomes = outcomes_for(dataset, treated_keys=set(dataset.frame["customer_id"].astype(str)), seed=5)
        upload_id = _upload(client, outcomes)
        added = client.post(f"/campaigns/{campaign_id}/outcomes", json={"upload_id": upload_id})
        assert added.status_code == 200, added.text
        report = client.post(f"/campaigns/{campaign_id}/measure", json={})
        assert report.status_code == 200, report.text
    return campaign_id


def test_check_contactable_reads_the_runs_own_counts(agentic_world: World) -> None:
    result = _call(agentic_world, "check_contactable", {"run_id": agentic_world.run_id})
    stored = json.loads(
        agentic_world.storage.read_bytes(run_key(agentic_world.run_id, "channel_contactability.json"))
    )
    out = result.output
    assert out["recorded"] is True and out["eligible_rows"] == stored["eligible_rows"]
    by_channel = {item["channel"]: item for item in out["channels"]}
    assert by_channel["sms"]["not_contactable_eligible_rows"] == stored["channel_counts"]["sms"] > 0
    assert by_channel["email"]["not_contactable_eligible_rows"] == 0
    assert by_channel["sms"]["reasons"] == {"not_contactable": stored["channel_counts"]["sms"]}
    assert result.label == "Descriptive" and result.artefact_refs == (agentic_world.run_id,)
    unrecorded = _call(agentic_world, "check_contactable", {"run_id": TRAIN_RUN}).output
    assert unrecorded["recorded"] is False and unrecorded["reason"] and unrecorded["channels"] == []


def test_measure_returns_the_stored_report_and_never_measures_again(
    agentic_world: World, measured: str
) -> None:
    with agentic_world.client() as client:
        view = client.get(f"/campaigns/{measured}").json()
    first = _call(agentic_world, "measure", {"campaign_id": measured})
    again = _call(agentic_world, "measure", {"campaign_id": measured})
    assert first.output["measured"] is True and first.output["report"] == view["report"]
    assert first.evidence_id == again.evidence_id, "the same stored report, the same evidence"
    assert first.output["report"]["computed_at"] == view["report"]["computed_at"], "read, not recomputed"
    assert first.label == "Causal"


def test_measure_says_not_measured_instead_of_inventing_a_number(agentic_world: World) -> None:
    out = _call(agentic_world, "measure", {"campaign_id": agentic_world.open_campaign}).output
    assert out["measured"] is False and out["report"] is None and "not been measured" in out["reason"]
    with pytest.raises(AgenticError) as caught:
        _call(agentic_world, "measure", {"campaign_id": "c_20260101_00000000"})
    assert caught.value.code == "CAMPAIGN_NOT_FOUND"


def test_the_proof_pack_tool_says_what_the_proof_route_says(agentic_world: World, measured: str) -> None:
    result = _call(agentic_world, "get_proof_pack", {"campaign_id": measured})
    with agentic_world.client() as client:
        response = client.get(f"/pilot/proof/{measured}", params={"format": "json"})
    out = result.output
    if response.status_code == 200:
        pack = response.json()
        assert (
            out["available"] is True and out["headline"] == pack["headline"] and out["claim"] == pack["claim"]
        )
        assert [section["key"] for section in out["sections"]] == [
            section["key"] for section in pack["sections"]
        ]
        assert out["artefacts"] == pack["artefacts"]
    else:
        assert out["available"] is False and out["code"] == response.json()["detail"]["code"]


def test_audit_value_and_policy_tools_say_what_is_not_there(agentic_world: World, measured: str) -> None:
    audit = _call(agentic_world, "audit_campaign", {"campaign_id": measured}).output
    assert audit["audited"] is False and audit["audit"] is None and audit["reason"]
    value = _call(agentic_world, "value_segment", {"run_id": agentic_world.run_id}).output
    assert value["valued"] is False and value["total_inr"] is None and value["reason"]
    policy = _call(agentic_world, "evaluate_policy", {"run_id": agentic_world.run_id}).output
    assert policy["ope"] is None and policy["ope_reason"] and policy["risk_comparison"] is None
    with pytest.raises(AgenticError) as caught:
        _call(agentic_world, "evaluate_policy", {"run_id": "r_20260101_00000000"})
    assert caught.value.code == "RUN_NOT_FOUND"
