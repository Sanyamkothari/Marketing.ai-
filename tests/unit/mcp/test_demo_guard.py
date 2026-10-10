"""The demo-only guard (Plan K M133, DEC-1404 (d)): which runs and uploads are demo data, and a walk that fails
when an exposed tool's input names an artefact the guard does not check."""

from __future__ import annotations

import json
import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from api.mcp.config import McpSettings
from api.mcp.demo import GUARDED_FIELDS, NOT_ARTEFACT_FIELDS, check_workspace, is_demo_run, is_demo_upload
from api.mcp.scopes import scope_for
from engine.agentic.errors import AgenticError
from engine.agentic.tools import default_registry
from engine.storage import LocalStorage

SETTINGS = McpSettings(enabled=True, demo_use_cases=("hillstrom-email",))


def _write(storage: LocalStorage, key: str, body: dict[str, Any]) -> None:
    storage.write_bytes(key, json.dumps(body).encode("utf-8"))


def test_a_run_is_demo_data_by_use_case_or_generated_data_and_never_a_clients(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    _write(storage, "runs/r_1/run.json", {"use_case_id": "hillstrom-email", "client_id": None})
    _write(storage, "runs/r_2/run.json", {"use_case_id": "win-back-campaign", "synthetic": True})
    _write(storage, "runs/r_3/run.json", {"use_case_id": "win-back-campaign"})
    _write(storage, "runs/r_4/run.json", {"use_case_id": "hillstrom-email", "client_id": "cl_acme"})
    storage.write_bytes("runs/r_5/run.json", b"{not json")
    verdicts = {
        run: is_demo_run(storage, run, SETTINGS) for run in ("r_1", "r_2", "r_3", "r_4", "r_5", "r_6")
    }
    assert verdicts == {"r_1": True, "r_2": True, "r_3": False, "r_4": False, "r_5": False, "r_6": False}


def test_an_upload_is_demo_data_by_use_case_or_marked_synthetic(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    _write(storage, "uploads/u_1/upload.json", {"use_case_id": "hillstrom-email"})
    _write(storage, "uploads/u_2/upload.json", {"use_case_id": "telco-churn", "synthetic": True})
    _write(storage, "uploads/u_3/upload.json", {"use_case_id": "telco-churn"})
    assert [is_demo_upload(storage, u, SETTINGS) for u in ("u_1", "u_2", "u_3", "u_4")] == [
        True,
        True,
        False,
        False,
    ]


def test_only_the_demo_workspace_is_served() -> None:
    check_workspace(SETTINGS, None)
    check_workspace(SETTINGS, {"marketing-ai/workspace": "demo"})
    for settings, meta in (
        (SETTINGS, {"marketing-ai/workspace": "acme"}),
        (McpSettings(enabled=True, workspace="acme"), None),
    ):
        with pytest.raises(AgenticError) as caught:
            check_workspace(settings, meta)
        assert caught.value.code == "MCP_DEMO_ONLY" and caught.value.status == 403


def _id_fields(model: type[BaseModel], path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for name, info in model.model_fields.items():
        if name.endswith(("_id", "_ids")):
            found.append((*path, name))
        for arg in (info.annotation, *typing.get_args(info.annotation)):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                found.extend(_id_fields(arg, (*path, name)))
    return found


def test_every_id_an_exposed_tool_takes_is_guarded_at_the_top_of_its_input() -> None:
    seen: set[str] = set()
    for tool in default_registry().list():
        if scope_for(tool) is None:
            continue
        for path in _id_fields(tool.input_model):
            name = path[-1]
            seen.add(name)
            if name in NOT_ARTEFACT_FIELDS:
                continue
            assert (
                path == (name,) and name in GUARDED_FIELDS
            ), f"{tool.name}: {'.'.join(path)} reaches it unguarded"
    assert {"run_id", "campaign_id"} <= seen, "the walk found the ids it guards"
