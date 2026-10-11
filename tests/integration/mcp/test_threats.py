"""The MCP surface, its authorisation and its threat model, over streamable HTTP to the real app (Plan K M133;
design §7.2, §7.3).

* the exposed tools are exactly the agent-safe core's read and propose tools; none writes or approves;
* sign-in is required (401 without a session, 503 with sign-in off), and a Viewer cannot propose (confused
  deputy);
* a malicious proposal (no holdout) is refused by the server's policy with a code;
* only demo data is served (`MCP_DEMO_ONLY`, the same answer for "not demo" and "not there");
* per-session rate limits;
* every call is audited with the principal and the client id.
"""

from __future__ import annotations

from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient

from api.mcp.transport import AsgiTransport
from engine.agentic.ledger import EVIDENCE_LABEL, PROPOSAL_DRAFTED, PROPOSAL_REFUSED, DecisionLedger
from engine.agentic.registry import ToolKind
from engine.agentic.tools import default_registry
from engine.audit.events import AuditQuery
from engine.audit.store import SqlAuditLog
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from tests.integration.mcp.conftest import POWER_BODY, Demo, mcp_config_root, signed_in_app

pytestmark = pytest.mark.integration

INITIALIZE: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


async def _calls(
    app: Any, token: str, calls: list[tuple[str, dict[str, Any]]], *, meta: dict[str, Any] | None = None
) -> tuple[list[Any], list[Any]]:
    async with AsgiTransport(app, token, client_name="claude-demo").connect() as client:
        tools = (await client.list_tools()).tools
        results = [await client.call_tool(name, arguments, meta=meta) for name, arguments in calls]  # type: ignore[arg-type]
    return tools, results


def _code(result: Any) -> str | None:
    return None if not result.is_error else (result.meta or {}).get("marketing-ai/code")


def _ledger(demo: Demo) -> DecisionLedger:
    return DecisionLedger(sqlite_engine(demo.data_dir / PLATFORM_DB_FILENAME), signer=None, policy_version="")


def test_the_exposed_tools_are_exactly_the_cores_read_and_propose_tools(demo: Demo) -> None:
    tools, _ = anyio.run(_calls, demo.app(), demo.token("analyst"), [])
    registry = {tool.name: tool for tool in default_registry().list()}
    assert sorted(tool.name for tool in tools) == sorted(registry)
    for listed in tools:
        tool = registry[listed.name]
        assert tool.kind in (ToolKind.READ, ToolKind.PROPOSE), "there is no write kind"
        assert (listed.meta or {})["marketing-ai/kind"] == tool.kind.value
        assert listed.annotations is not None and listed.annotations.destructive_hint is False
        assert listed.annotations.read_only_hint is (tool.kind is ToolKind.READ)
        assert listed.input_schema == tool.input_model.model_json_schema(mode="validation")
        assert listed.output_schema == tool.output_model.model_json_schema(mode="serialization")
        for verb in ("approve", "reject", "send", "write", "delete", "export", "freeze", "execute"):
            assert verb not in listed.name, f"{listed.name} must not {verb}"
    assert [tool.name for tool in tools if (tool.meta or {})["marketing-ai/kind"] == "propose"] == [
        "draft_test_plan"
    ]


def test_sign_in_is_required(demo: Demo, tmp_path: Path) -> None:
    with TestClient(demo.app()) as client:
        missing = client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
        forged = client.post("/mcp", json=INITIALIZE, headers={"Authorization": "Bearer not-a-session"})
    assert missing.status_code == 401 and missing.json()["detail"]["code"] == "AUTH_REQUIRED"
    assert missing.headers["www-authenticate"] == "Bearer"
    assert forged.status_code == 401
    with TestClient(signed_in_app(demo.config_root, tmp_path, auth_mode="off")) as client:
        off = client.post("/mcp", json=INITIALIZE)
    assert off.status_code == 503 and off.json()["detail"]["code"] == "AUTH_NOT_CONFIGURED"


def test_a_viewer_reads_but_cannot_propose(demo: Demo) -> None:
    before = len(list(_ledger(demo).entries(kind=PROPOSAL_DRAFTED)))
    draft = {"campaign_id": demo.campaign_id, "plan": _plan()}
    tools, results = anyio.run(
        _calls,
        demo.app(),
        demo.token("viewer"),
        [("draft_test_plan", draft), ("audit_campaign", {"campaign_id": demo.campaign_id})],
    )
    assert "draft_test_plan" not in {tool.name for tool in tools}, "a Viewer is not shown a propose tool"
    denied, read = results
    assert _code(denied) == "MCP_SCOPE_DENIED" and denied.structured_content is None
    assert read.is_error is False
    assert len(list(_ledger(demo).entries(kind=PROPOSAL_DRAFTED))) == before, "nothing was drafted"
    # An Approver holds no propose scope either: approving is not something MCP maps to.
    _, (approver,) = anyio.run(_calls, demo.app(), demo.token("approver"), [("draft_test_plan", draft)])
    assert _code(approver) == "MCP_SCOPE_DENIED"


def _plan() -> dict[str, Any]:
    return {
        "metric": "bought within two weeks",
        "outcome_column": "conversion",
        "analysis_date": date(2030, 1, 1).isoformat(),
        "mde_pp": 1.0,
        "base_rate": 0.006,
    }


def test_a_malicious_proposal_is_refused_by_the_server_with_a_code(demo: Demo) -> None:
    """Tool poisoning: another server's description told the agent to propose no holdout. The policy decides."""
    draft = {"campaign_id": demo.campaign_id, "plan": _plan(), "requested_holdout_fraction": 0.0}
    _, (result,) = anyio.run(_calls, demo.app(), demo.token("analyst"), [("draft_test_plan", draft)])
    assert result.is_error is False, result.content
    out = result.structured_content
    assert out["status"] == "refused" and "POLICY_HOLDOUT_IMMUTABLE" in out["codes"]
    refused = list(_ledger(demo).entries(kind=PROPOSAL_REFUSED))
    assert out["proposal_id"] in {
        entry.body.get("proposal_id") for entry in refused
    }, "the refusal is on the ledger"


def test_only_demo_data_is_served(demo: Demo, tmp_path: Path) -> None:
    calls = [
        ("audit_campaign", {"campaign_id": "c_20990101_0000beef"}),
        ("check_contactable", {"run_id": "r_20990101_0000beef"}),
    ]
    _, (no_campaign, no_run) = anyio.run(_calls, demo.app(), demo.token("analyst"), calls)
    assert _code(no_campaign) == _code(no_run) == "MCP_DEMO_ONLY"
    # The same Hillstrom campaign, on a deployment where Hillstrom is not demo data: refused, with the same
    # answer as an id that names nothing.
    root = mcp_config_root(demo.config_root, tmp_path / "configs", demo_use_cases=[])
    app = signed_in_app(root, demo.data_dir)
    _, (client_data,) = anyio.run(
        _calls, app, demo.token("analyst"), [("audit_campaign", {"campaign_id": demo.campaign_id})]
    )
    assert _code(client_data) == "MCP_DEMO_ONLY"
    assert client_data.content[0].text == no_campaign.content[0].text
    # A client's campaign, measured by the product on this very deployment (its files filed under a use case
    # that is not demo data): refused by every tool that reads a campaign, with the same answer again.
    client_calls = [
        (name, {"campaign_id": demo.client_campaign_id})
        for name in ("audit_campaign", "measure", "get_proof_pack")
    ]
    _, refused = anyio.run(_calls, demo.app(), demo.token("analyst"), client_calls)
    assert [_code(result) for result in refused] == ["MCP_DEMO_ONLY"] * 3
    assert {result.content[0].text for result in refused} == {no_campaign.content[0].text}
    with TestClient(demo.app()) as client:
        own = client.get(f"/campaigns/{demo.client_campaign_id}", headers=demo.headers("analyst"))
    assert own.status_code == 200 and own.json()["report"] is not None, "the product itself serves it"
    # A request that names another workspace is refused too.
    elsewhere = partial(_calls, meta={"marketing-ai/workspace": "client-acme"})
    _, (other,) = anyio.run(elsewhere, demo.app(), demo.token("analyst"), [("plan_test", POWER_BODY)])
    assert _code(other) == "MCP_DEMO_ONLY"


def test_each_session_is_rate_limited(demo: Demo, tmp_path: Path) -> None:
    root = mcp_config_root(
        demo.config_root,
        tmp_path / "configs",
        rate_limit={"calls_per_minute": 3, "concurrent_calls": 2},
    )
    app = signed_in_app(root, demo.data_dir)
    calls = [("plan_test", POWER_BODY)] * 4
    _, results = anyio.run(_calls, app, demo.token("analyst"), calls)
    assert [_code(result) for result in results] == [None, None, None, "MCP_RATE_LIMITED"]
    assert results[-1].meta["marketing-ai/retry_after_seconds"] >= 1
    _, (fresh,) = anyio.run(_calls, app, demo.token("analyst"), [("plan_test", POWER_BODY)])
    assert fresh.is_error is False, "another sign-in session has its own budget"


def test_every_call_is_audited_with_the_principal_and_the_client(demo: Demo) -> None:
    calls = [("audit_campaign", {"campaign_id": demo.campaign_id}), ("draft_test_plan", {"campaign_id": "x"})]
    anyio.run(_calls, demo.app(), demo.token("viewer"), calls)
    log = SqlAuditLog(sqlite_engine(demo.data_dir / PLATFORM_DB_FILENAME))
    events = log.query(AuditQuery(action="mcp.tool_call", limit=5000))
    mine = [event for event in events if event.actor_id == demo.users["viewer"]]
    by_tool = {(event.object_id, event.outcome, event.details.get("reason_code")) for event in mine}
    assert ("audit_campaign", "success", None) in by_tool
    assert ("draft_test_plan", "denied", "MCP_SCOPE_DENIED") in by_tool
    assert {event.details["client_id"] for event in mine} == {"mcp:claude-demo"}
    requests = log.query(AuditQuery(action="mcp.request", limit=5000))
    assert any(
        event.actor_id == demo.users["viewer"] for event in requests
    ), "the HTTP request names who sent it"


def test_a_client_of_the_2025_revision_is_served_statelessly_too(demo: Demo) -> None:
    async def legacy() -> tuple[str, Any]:
        transport = AsgiTransport(demo.app(), demo.token("viewer"), client_name="old-client", mode="legacy")
        async with transport.connect() as client:
            result = await client.call_tool("audit_campaign", {"campaign_id": demo.campaign_id})
            return client.protocol_version, result

    log = SqlAuditLog(sqlite_engine(demo.data_dir / PLATFORM_DB_FILENAME))

    def calls() -> dict[str, Any]:
        events = log.query(AuditQuery(action="mcp.tool_call", limit=5000))
        return {event.event_id: event for event in events if event.actor_id == demo.users["viewer"]}

    before = calls()
    protocol, result = anyio.run(legacy)
    assert protocol != "2026-07-28" and result.is_error is False
    assert result.meta["marketing-ai/label"] == "Causal"
    # Its clientInfo went with an initialize POST the stateless server did not keep: the call is still named,
    # from the request's User-Agent, in the audit event and on the ledger.
    (event,) = [event for key, event in calls().items() if key not in before]
    client_id = event.details["client_id"]
    assert client_id != "mcp:unnamed" and str(client_id).startswith("mcp:ua-"), client_id
    labelled = {
        entry.body["evidence_id"]: entry.actor for entry in _ledger(demo).entries(kind=EVIDENCE_LABEL)
    }
    assert labelled[result.meta["marketing-ai/evidence_id"]] == f"agent:{client_id}"


def test_the_endpoint_takes_post_only(demo: Demo) -> None:
    """A GET would open the transport's event stream and hold the connection outside every per-session limit;
    the stateless server has no stream and no session to delete. Both are answered 405 by the router."""

    async def probe() -> list[Any]:
        import httpx2

        app = demo.app()
        headers = {"Authorization": f"Bearer {demo.token('viewer')}", "Accept": "text/event-stream"}
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
                with anyio.fail_after(10):  # on a server that serves GET, the stream never answers
                    return [
                        await http.request(method, "/mcp", headers=headers) for method in ("GET", "DELETE")
                    ]

    for response in anyio.run(probe):
        assert response.status_code == 405, response.text
        assert response.headers["allow"] == "POST", "the router refused it, before sign-in or the transport"
