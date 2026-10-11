"""The tool registry (design §1; invariant I1): no write kind, every call checked, every result a content hash."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from api.access_policy import RoutePolicy, all_policies
from engine.access.roles import Principal, Role
from engine.agentic.errors import AgenticError
from engine.agentic.ledger import DecisionLedger, FakeSigner, verify_ledger
from engine.agentic.registry import (
    DEFAULT_OUTPUT_BUDGET,
    AgentTool,
    ToolContext,
    ToolKind,
    ToolRegistry,
    evidence_id_for,
)
from engine.agentic.tools import ALL_TOOLS, PLAN_TEST, default_registry
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.storage import LocalStorage

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
VIEWER = Principal(user_id="u_vera", username="vera", roles=frozenset({Role.VIEWER}))
ANALYST = Principal(user_id="u_asha", username="asha", roles=frozenset({Role.ANALYST}))
PREVIEW = {"eligible": 20_000, "base_rate": 0.05, "holdout_shares": [0.05, 0.1], "contact_cost": 0.2}


def ctx(tmp_path: Path, config_root: Path, principal: Principal = VIEWER, **extra: Any) -> ToolContext:
    return ToolContext(
        storage=LocalStorage(tmp_path),
        principal=principal,
        config_root=config_root,
        clock=lambda: NOW,
        **extra,
    )


def test_there_is_no_write_kind() -> None:
    assert {member.value for member in ToolKind} == {"read", "propose"}
    assert not hasattr(ToolKind, "WRITE")


def test_every_registered_tool_reads_or_proposes() -> None:
    registry = default_registry()
    tools = registry.list()
    assert [tool.name for tool in tools] == sorted(tool.name for tool in ALL_TOOLS)
    for tool in tools:
        assert tool.kind in (ToolKind.READ, ToolKind.PROPOSE), tool.name
    assert {tool.name for tool in tools if tool.kind is ToolKind.PROPOSE} == {"draft_test_plan"}
    names = {tool.name for tool in tools}
    assert names >= {
        "plan_test",
        "check_contactable",
        "value_segment",
        "measure",
        "audit_campaign",
        "get_proof_pack",
        "evaluate_policy",
        "draft_test_plan",
    }


def test_the_registry_refuses_a_write_tool_and_other_lax_tools() -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="only read and propose"):
        registry.register(dataclasses.replace(PLAN_TEST, kind="write"))  # type: ignore[arg-type]

    class Lax(BaseModel):
        model_config = ConfigDict(extra="allow")

    with pytest.raises(ValueError, match="unknown fields"):
        registry.register(dataclasses.replace(PLAN_TEST, input_model=Lax))  # type: ignore[arg-type]
    public = RoutePolicy(role=None, action="x.y")
    with pytest.raises(ValueError, match="no tool is public"):
        registry.register(dataclasses.replace(PLAN_TEST, route_policy=public))
    registry.register(PLAN_TEST)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(PLAN_TEST)


def test_each_tool_has_the_policy_of_the_route_it_mirrors() -> None:
    from tests.integration.production.access_support import live_routes

    routes = {(route.method, route.path) for route in live_routes()}
    policies = all_policies()
    for tool in ALL_TOOLS:
        assert tool.mirrors in routes, f"{tool.name} mirrors {tool.mirrors}, which is not a route"
        assert policies[tool.mirrors] == tool.route_policy, tool.name


def test_a_call_is_checked_before_it_runs(tmp_path: Path, config_root: Path) -> None:
    registry = default_registry()
    with pytest.raises(AgenticError) as unknown:
        registry.call("send_email", {}, ctx(tmp_path, config_root))
    assert unknown.value.code == "TOOL_UNKNOWN"
    with pytest.raises(AgenticError) as invalid:
        registry.call("plan_test", {**PREVIEW, "rows": [{"customer_id": "C-1"}]}, ctx(tmp_path, config_root))
    assert invalid.value.code == "TOOL_INPUT_INVALID" and invalid.value.status == 422
    with pytest.raises(AgenticError) as role:
        registry.call("draft_test_plan", {"campaign_id": "c_1", "plan": {}}, ctx(tmp_path, config_root))
    assert role.value.code == "TOOL_ROLE_REQUIRED" and role.value.status == 403
    with pytest.raises(AgenticError) as path:
        registry.call("measure", {"campaign_id": "../../etc"}, ctx(tmp_path, config_root))
    assert path.value.code == "TOOL_INPUT_INVALID"


def test_the_evidence_id_is_a_content_hash(tmp_path: Path, config_root: Path) -> None:
    registry = default_registry()
    first = registry.call("plan_test", PREVIEW, ctx(tmp_path, config_root))
    again = registry.call("plan_test", dict(reversed(list(PREVIEW.items()))), ctx(tmp_path, config_root))
    other = registry.call("plan_test", {**PREVIEW, "base_rate": 0.06}, ctx(tmp_path, config_root))
    assert first.evidence_id == again.evidence_id == evidence_id_for("plan_test", first.input, first.output)
    assert first.evidence_id != other.evidence_id
    assert first.evidence_id.startswith("ev_") and len(first.evidence_id) == 19
    assert first.label == "Estimated" and first.kind is ToolKind.READ


def test_plan_test_answers_what_the_route_answers(tmp_path: Path, config_root: Path) -> None:
    from api.main import create_app

    result = default_registry().call("plan_test", PREVIEW, ctx(tmp_path, config_root))
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path)) as client:
        response = client.post("/measurement/power-preview", json=PREVIEW)
    assert response.status_code == 200
    assert result.output == response.json()


def test_an_output_over_budget_is_refused(tmp_path: Path, config_root: Path) -> None:
    class Big(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str

    class Empty(BaseModel):
        model_config = ConfigDict(extra="forbid")

    tool: AgentTool[Empty, Big] = AgentTool(
        name="too_big",
        kind=ToolKind.READ,
        description="test",
        input_model=Empty,
        output_model=Big,
        route_policy=PLAN_TEST.route_policy,
        mirrors=PLAN_TEST.mirrors,
        evidence_label=lambda _out: "Descriptive",
        run=lambda _in, _ctx: Big(text="x" * DEFAULT_OUTPUT_BUDGET),
    )
    registry = ToolRegistry()
    registry.register(tool)
    with pytest.raises(AgenticError) as caught:
        registry.call("too_big", {}, ctx(tmp_path, config_root))
    assert caught.value.code == "TOOL_OUTPUT_TOO_LARGE" and caught.value.detail["budget"] == 16 * 1024


def test_every_evidence_label_goes_on_the_ledger_signed(tmp_path: Path, config_root: Path) -> None:
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    ledger = DecisionLedger(engine, signer=FakeSigner(), policy_version="p" * 64)
    result = default_registry().call("plan_test", PREVIEW, ctx(tmp_path, config_root, ledger=ledger))
    (entry,) = list(ledger.entries())
    assert entry.kind == "evidence.label" and entry.signature is not None
    assert entry.body["evidence_id"] == result.evidence_id and entry.body["label"] == "Estimated"
    assert "input" not in entry.body and "output" not in entry.body, "digests only"
    assert ledger.has_evidence(result.evidence_id)
    assert verify_ledger(engine, FakeSigner()).ok


def _a_run(tmp_path: Path, run_id: str) -> None:
    from engine.runs import RUN_FILENAME
    from engine.storage import run_key

    LocalStorage(tmp_path).write_bytes(run_key(run_id, RUN_FILENAME), b"{}")


def test_a_call_its_route_would_audit_writes_one_audit_event(tmp_path: Path, config_root: Path) -> None:
    """`check_contactable` mirrors an `audit_reads` route and `draft_test_plan` a POST: each call writes one
    audit event, as the middleware writes one per request. `plan_test` mirrors an unaudited read: none."""
    from engine.agentic.registry import audited
    from engine.audit.events import AuditQuery
    from engine.audit.store import SqlAuditLog

    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    _a_run(tmp_path, "r_audited")
    registry = default_registry()
    out = registry.call(
        "check_contactable", {"run_id": "r_audited"}, ctx(tmp_path, config_root, platform_engine=engine)
    )
    assert out.output["recorded"] is False
    registry.call("plan_test", PREVIEW, ctx(tmp_path, config_root, platform_engine=engine))
    with pytest.raises(AgenticError):
        registry.call(
            "draft_test_plan",
            {"campaign_id": "c_1", "plan": {}},
            ctx(tmp_path, config_root, platform_engine=engine, actor="agent:planner"),
        )
    log = SqlAuditLog(engine)
    events = log.query(AuditQuery())
    assert len(events) == 2, [event.action for event in events]
    read = next(event for event in events if event.action == "runs.artefact_download")
    assert (read.object_type, read.object_id, read.outcome, read.actor_id) == (
        "run",
        "r_audited",
        "success",
        "u_vera",
    )
    assert read.details["route"] == "tool:check_contactable" and read.details["trigger"] == "agent:core"
    denied = next(event for event in events if event.action == "campaigns.plan")
    assert denied.outcome == "denied" and denied.details["reason_code"] == "TOOL_ROLE_REQUIRED"
    assert denied.object_id == "c_1" and denied.details["trigger"] == "agent:planner"
    assert {tool.name for tool in ALL_TOOLS if audited(tool)} == {
        "check_contactable",
        "value_segment",
        "draft_test_plan",
    }


def test_a_stored_artefact_that_does_not_read_is_not_called_missing(
    tmp_path: Path, config_root: Path
) -> None:
    """A partial write is not "this run did not record it": the tool refuses with the real reason."""
    from engine.storage import run_key

    _a_run(tmp_path, "r_corrupt")
    LocalStorage(tmp_path).write_bytes(run_key("r_corrupt", "channel_contactability.json"), b'{"channels": [')
    with pytest.raises(AgenticError) as caught:
        default_registry().call("check_contactable", {"run_id": "r_corrupt"}, ctx(tmp_path, config_root))
    assert caught.value.code == "TOOL_ARTEFACT_UNREADABLE"
    assert "channel_contactability.json" in caught.value.message and "r_corrupt" in caught.value.message
    LocalStorage(tmp_path).write_bytes(run_key("r_corrupt", "expected_gross_value.json"), b'{"run_id": 1}')
    with pytest.raises(AgenticError) as invalid:
        default_registry().call("value_segment", {"run_id": "r_corrupt"}, ctx(tmp_path, config_root))
    assert invalid.value.code == "TOOL_ARTEFACT_UNREADABLE", "stored but not valid: not 'not valued'"
