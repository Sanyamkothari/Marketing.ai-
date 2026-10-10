"""A scripted MCP client plans a test and audits Hillstrom's library campaign over streamable HTTP, and gets the
numbers the product's own API gives for the same artefacts (Plan K M133 acceptance; design §7.3, §12).

The client is the official SDK's, speaking streamable HTTP to the real app's `/mcp` with a bearer session, through
an in-process ASGI transport (`api.mcp.transport.AsgiTransport`): no socket, no network.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient

from api.mcp.transport import AsgiTransport
from engine.agentic.ledger import EVIDENCE_LABEL, DecisionLedger
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from tests.integration.mcp.conftest import POWER_BODY, Demo

pytestmark = pytest.mark.integration


async def _session(demo: Demo, who: str, calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    async with AsgiTransport(demo.app(), demo.token(who), client_name="claude-demo").connect() as client:
        listed = await client.list_tools()
        results = [await client.call_tool(name, arguments) for name, arguments in calls]
        protocol = client.protocol_version
    return {"tools": listed.tools, "results": results, "protocol": protocol}


def _product(demo: Demo) -> dict[str, Any]:
    headers = demo.headers("analyst")
    with TestClient(demo.app()) as client:
        power = client.post("/measurement/power-preview", json=POWER_BODY, headers=headers)
        campaign = client.get(f"/campaigns/{demo.campaign_id}", headers=headers)
        proof = client.get(f"/pilot/proof/{demo.campaign_id}", params={"format": "json"}, headers=headers)
    assert power.status_code == 200 and campaign.status_code == 200 and proof.status_code == 200, proof.text
    return {"power": power.json(), "campaign": campaign.json(), "proof": proof.json()}


def test_a_scripted_client_gets_the_products_own_numbers_for_hillstrom(demo: Demo) -> None:
    calls = [
        ("plan_test", POWER_BODY),
        ("audit_campaign", {"campaign_id": demo.campaign_id}),
        ("measure", {"campaign_id": demo.campaign_id}),
        ("get_proof_pack", {"campaign_id": demo.campaign_id}),
    ]
    session = anyio.run(_session, demo, "analyst", calls)
    product = _product(demo)
    assert session["protocol"] == "2026-07-28", "the client spoke the stateless revision"
    plan, audit, measure, proof = session["results"]
    for result in session["results"]:
        assert result.is_error is False, result.content
        assert "withheld" not in "".join(
            result.meta or {}
        ), "nothing in Hillstrom is a small group or outside text"

    # plan_test: the power preview, figure for figure.
    assert plan.structured_content == product["power"]
    # audit_campaign: the stored audit and report, exactly as GET /campaigns/{id} serves them.
    view = product["campaign"]
    assert audit.structured_content["audit"] == view["audit"]
    assert audit.structured_content["report"] == view["report"]
    assert measure.structured_content["report"] == view["report"]
    assert audit.meta["marketing-ai/label"] == "Causal", "the engine verified that the e-mails were random"
    report = view["report"]
    assert (
        report["treated_rows"] > 2_000 and report["control_rows"] > 2_000
    ), "groups of thousands: nothing to suppress"
    # get_proof_pack: the pack the proof route draws.
    pack = product["proof"]
    out = proof.structured_content
    assert out["available"] is True
    assert (out["claim"], out["headline"], out["artefacts"]) == (
        pack["claim"],
        pack["headline"],
        pack["artefacts"],
    )
    assert [section["key"] for section in out["sections"]] == [section["key"] for section in pack["sections"]]

    # Every answer is evidence on the ledger, under the id the result carries, recorded as the MCP client.
    ledger = DecisionLedger(
        sqlite_engine(demo.data_dir / PLATFORM_DB_FILENAME), signer=None, policy_version=""
    )
    labelled = {entry.body["evidence_id"]: entry.actor for entry in ledger.entries(kind=EVIDENCE_LABEL)}
    for result in session["results"]:
        assert labelled[result.meta["marketing-ai/evidence_id"]] == "agent:mcp:claude-demo"


def test_the_text_beside_a_result_is_ours_and_carries_no_string_from_the_data(demo: Demo) -> None:
    session = anyio.run(_session, demo, "viewer", [("audit_campaign", {"campaign_id": demo.campaign_id})])
    (audit,) = session["results"]
    text = "".join(block.text for block in audit.content if block.type == "text")
    assert audit.meta["marketing-ai/evidence_id"] in text and "Causal" in text
    for third_party in ("Mens E-Mail", "Womens E-Mail", "No E-Mail", "assignment.csv", "Hillstrom"):
        assert third_party not in text
