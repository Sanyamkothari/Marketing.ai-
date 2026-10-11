"""Client workspaces (Plan K M141, DEC-1408 (a), (b)): the configuration that binds every person to one
workspace, and the guard that keeps a token inside it.

On the unchanged code (M133) `mcp.workspaces` is refused as an unknown key and every non-demo workspace is
`MCP_DEMO_ONLY`: these tests fail there.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from api.mcp.config import ClientWorkspace, McpSettings, load_mcp_config
from api.mcp.demo import GUARDED_FIELDS
from api.mcp.workspace import WORKSPACE_GUARDED_FIELDS, check_named, guard, run_belongs, workspace_for
from engine.access.roles import Principal, Role
from engine.agentic.errors import AgenticError
from engine.agentic.registry import ToolContext
from engine.agentic.tools import CampaignInput, RunInput
from engine.storage import LocalStorage

ACME = ClientWorkspace(id="acme", client_id="c_acme_1", members=("u-asha", "u-vera"))
HOUSE = ClientWorkspace(id="house", unattributed=True, members=("u-ravi",))
SETTINGS = McpSettings(enabled=True, workspace="clients", workspaces=(ACME, HOUSE))


def _write(storage: LocalStorage, key: str, body: dict[str, Any]) -> None:
    storage.write_bytes(key, json.dumps(body).encode("utf-8"))


def test_the_default_serves_the_demo_library_and_binds_nobody() -> None:
    settings = load_mcp_config(Path(__file__).resolve().parents[3] / "configs").mcp
    assert settings.workspace == "demo" and settings.workspaces == () and not settings.serves_clients
    assert settings.workspace_of("u-anyone") is None


@pytest.mark.parametrize(
    ("workspaces", "message"),
    [
        ([ACME, ACME.model_copy(update={"members": ("u-other",)})], "share an id"),
        ([ACME, HOUSE.model_copy(update={"client_id": "c_acme_1"})], "named by two"),
        ([HOUSE, HOUSE.model_copy(update={"id": "house-2", "members": ("u-x",)})], "more than one workspace"),
        ([ClientWorkspace(id="empty", members=("u-x",))], "holds no data"),
        ([ACME, HOUSE.model_copy(update={"members": ("u-asha",)})], "member of two"),
        ([], "needs at least one"),
    ],
)
def test_a_configuration_that_would_not_bind_each_token_to_one_workspace_is_refused(
    workspaces: list[ClientWorkspace], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        McpSettings(enabled=True, workspace="clients", workspaces=tuple(workspaces))


def test_a_bad_file_is_config_invalid(tmp_path: Path) -> None:
    (tmp_path / "agentic").mkdir()
    (tmp_path / "agentic" / "mcp.yaml").write_text(
        "mcp:\n  workspace: clients\n  workspaces:\n    - {id: Acme Corp, client_id: c_1, members: [u-1]}\n",
        encoding="utf-8",
    )
    with pytest.raises(AgenticError) as caught:
        load_mcp_config(tmp_path)
    assert caught.value.code == "CONFIG_INVALID"


def test_a_person_is_bound_to_their_workspace_or_refused() -> None:
    assert workspace_for(SETTINGS, "u-vera").id == "acme" and workspace_for(SETTINGS, "u-ravi").id == "house"
    with pytest.raises(AgenticError) as caught:
        workspace_for(SETTINGS, "u-stranger")
    assert caught.value.code == "MCP_WORKSPACE_DENIED" and caught.value.status == 403
    check_named(ACME, None)
    check_named(ACME, {"marketing-ai/workspace": "acme"})
    with pytest.raises(AgenticError) as other:
        check_named(ACME, {"marketing-ai/workspace": "house"})
    assert other.value.code == "MCP_WORKSPACE_DENIED"


def test_a_run_belongs_by_the_client_it_records(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    _write(storage, "runs/r_1/run.json", {"use_case_id": "telco-churn", "client_id": "c_acme_1"})
    _write(storage, "runs/r_2/run.json", {"use_case_id": "telco-churn", "client_id": "c_beta_1"})
    _write(storage, "runs/r_3/run.json", {"use_case_id": "telco-churn", "client_id": None})
    storage.write_bytes("runs/r_4/run.json", b"{not json")
    runs = ("r_1", "r_2", "r_3", "r_4", "r_5")
    assert [run_belongs(storage, run, ACME) for run in runs] == [True, False, False, False, False]
    assert [run_belongs(storage, run, HOUSE) for run in runs] == [False, False, True, False, False]


class _Campaigns:
    def __init__(self, **campaigns: Any) -> None:
        self._campaigns = campaigns

    def get(self, campaign_id: str) -> Any:
        return self._campaigns.get(campaign_id)


def _ctx(storage: LocalStorage, campaigns: _Campaigns) -> ToolContext:
    principal = Principal(user_id="u-asha", username="asha", roles=frozenset({Role.ANALYST}), kind="user")
    return ToolContext(
        storage=storage,
        principal=principal,
        config_root=storage.root,
        clock=lambda: datetime(2026, 10, 11, tzinfo=UTC),
        campaigns=campaigns,  # type: ignore[arg-type]
    )


def _campaign(campaign_id: str, run_ids: tuple[str, ...], rows: int = 500) -> Any:
    from types import SimpleNamespace

    counts = SimpleNamespace(treated=rows, holdout=rows, intended_treated=rows, intended_holdout=rows)
    return SimpleNamespace(campaign_id=campaign_id, run_ids=run_ids, counts=counts)


def test_a_campaign_belongs_where_its_scoring_runs_belong(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    _write(storage, "runs/r_1/run.json", {"client_id": "c_acme_1"})
    _write(storage, "runs/r_2/run.json", {"client_id": "c_beta_1"})
    campaigns = _Campaigns(
        c_acme=_campaign("c_acme", ("r_1",)),
        c_mixed=_campaign("c_mixed", ("r_1", "r_2")),
        c_audit=_campaign("c_audit", ()),
        c_small=_campaign("c_small", ("r_1",), rows=7),
    )
    ctx = _ctx(storage, campaigns)

    def verdict(workspace: ClientWorkspace, campaign_id: str) -> str | None:
        try:
            guard(CampaignInput(campaign_id=campaign_id), ctx, workspace, SETTINGS, reads=True)
        except AgenticError as exc:
            return exc.code
        return None

    assert verdict(ACME, "c_acme") is None
    assert verdict(ACME, "c_mixed") == "MCP_WORKSPACE_DENIED", "one run of another client's is enough"
    assert verdict(ACME, "c_audit") == "MCP_WORKSPACE_DENIED", "an audited campaign records no client"
    assert verdict(HOUSE, "c_audit") is None, "the workspace that claims unattributed data holds it"
    assert verdict(HOUSE, "c_acme") == "MCP_WORKSPACE_DENIED"
    assert verdict(ACME, "c_nowhere") == "MCP_WORKSPACE_DENIED"
    assert verdict(ACME, "c_small") == "MCP_GROUP_TOO_SMALL", "small groups are refused on client data too"
    refused = [AgenticError("x", "y")]
    for campaign_id in ("c_mixed", "c_audit", "c_nowhere"):
        with pytest.raises(AgenticError) as caught:
            guard(CampaignInput(campaign_id=campaign_id), ctx, ACME, SETTINGS, reads=True)
        refused.append(caught.value)
    assert len({error.message for error in refused[1:]}) == 1, "not yours and not there read alike"
    assert all(
        campaign_id not in error.message for error in refused for campaign_id in ("c_mixed", "c_audit")
    )
    with pytest.raises(AgenticError) as run_refused:
        guard(RunInput(run_id="r_2"), ctx, ACME, SETTINGS, reads=True)
    assert run_refused.value.code == "MCP_WORKSPACE_DENIED"


def test_the_workspace_guard_checks_every_field_the_demo_guard_checks() -> None:
    """`tests/unit/mcp/test_demo_guard.py` walks every exposed tool's input against the demo guard's fields; the
    workspace guard must check the same ones, so no id field reaches a tool unguarded on client data either.
    """
    assert set(WORKSPACE_GUARDED_FIELDS) == set(GUARDED_FIELDS)
