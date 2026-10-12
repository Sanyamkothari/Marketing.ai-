"""The goal brief (Plan O M235, DEC-1805; the design §3.2).

Acceptance (Plan O §M235): `brief_hash` is stable for an unchanged configuration - in one process, across
processes - and moves when the configuration moves. Also: the brief carries the design's fields from the use
case's configuration; it is tool #35 of the catalogue, a READ tool a Viewer may call, holding no client data.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from engine.access.roles import Principal, Role
from engine.agent.egress import assert_clean
from engine.config import ConfigError, list_use_case_ids
from engine.doctor.catalogue import row
from engine.doctor.goal_brief import (
    GOAL_BRIEF_TOOL,
    GoalBrief,
    brief_hash,
    goal_brief_tool,
    load_goal_brief,
    register_goal_brief_tool,
)
from engine.doctor.registry import DataToolContext, DataToolError, DataToolKind, DataToolRegistry, Exposure
from engine.doctor.visit import visit_registry

VIEWER = Principal(user_id="u-viewer", username="v", roles=frozenset({Role.VIEWER}), kind="user")


def _ids(root: Path | None = None) -> list[str]:
    return sorted(list_use_case_ids(root))


def test_the_brief_carries_the_designs_fields_from_configuration() -> None:
    brief = load_goal_brief("targeted-advertisement")
    assert brief.goal_id == "targeted-advertisement" and brief.goal_name
    assert brief.problem_type == "binary_classification" and brief.use_case_type == "propensity"
    assert brief.outcome.column == "converted_30d" and "converted" in brief.outcome.synonyms
    assert brief.outcome.success_sentence and brief.outcome.default_window_days in brief.outcome.window_days
    assert brief.split.type == "random_stratified" and 0 < brief.split.test_fraction < 1
    assert brief.thresholds.min_rows >= 1 and brief.thresholds.min_positive >= 1
    assert brief.thresholds.min_arm_rows is None, "only an uplift goal has arms"
    assert brief.levels == ("clean", "derive")
    assert brief.contact.contacts_customers and brief.contact.opt_out_column == "marketing_opt_in"
    assert brief.models.families and brief.models.metric
    assert brief.time_settings.training_minutes == 30
    needs = {column.need: column for column in brief.required_columns}
    assert needs["key"].required and "customer_id" in needs["key"].names
    assert needs["outcome"].required and needs["outcome"].names[0] == "converted_30d"
    assert needs["consent"].required, "a contacting goal needs its consent column"
    assert brief.mode == "train" and len(brief.brief_hash) == 64


def test_a_reshape_goal_and_a_time_split_goal_say_so() -> None:
    win_back = load_goal_brief("retail-win-back")
    assert "reshape" in win_back.levels and win_back.time_rules.outcome_window_days == 90
    assert win_back.time_rules.label_must_be_mature and win_back.time_rules.snapshot_column == "snapshot_date"
    faults = load_goal_brief("fault-prediction")
    assert faults.time_rules.split_type == "time_based" and faults.time_rules.label_must_be_mature
    time = faults.column("time")
    assert time is not None and time.required, "a time split cannot be made without the date column"
    assert faults.column("consent") is None, "an operational goal contacts nobody"


@pytest.mark.parametrize("goal_id", _ids())
def test_every_use_case_has_a_brief_and_its_hash_is_its_content(goal_id: str) -> None:
    brief = load_goal_brief(goal_id)
    assert brief.brief_hash == brief_hash(brief)
    assert load_goal_brief(goal_id).brief_hash == brief.brief_hash, "stable for an unchanged configuration"
    assert (
        load_goal_brief(goal_id, mode="score").brief_hash != brief.brief_hash
    ), "the mode is part of the brief"
    GoalBrief.model_validate_json(brief.model_dump_json())  # it round-trips


def test_brief_hash_is_stable_across_processes() -> None:
    script = (
        "from engine.doctor.goal_brief import load_goal_brief\n"
        "print(load_goal_brief('telco-churn').brief_hash)\n"
    )
    found = {
        subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=Path.cwd()
        ).stdout.strip()
        for _ in range(2)
    }
    assert found == {load_goal_brief("telco-churn").brief_hash}


def _copy(config_root: Path, tmp_path: Path) -> Path:
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    return root


def test_brief_hash_moves_when_the_configuration_moves(config_root: Path, tmp_path: Path) -> None:
    root = _copy(config_root, tmp_path)
    before = load_goal_brief("telco-churn", root=root)
    assert before.brief_hash == load_goal_brief("telco-churn").brief_hash, "a copy is the same configuration"
    path = root / "use_cases" / "telco_churn.yaml"
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.setdefault("validation", {})["min_rows"] = 1234
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    after = load_goal_brief("telco-churn", root=root)
    assert after.thresholds.min_rows == 1234 and after.brief_hash != before.brief_hash


def test_every_field_is_in_the_hash() -> None:
    brief = load_goal_brief("targeted-advertisement")
    for name in GoalBrief.model_fields:
        if name == "brief_hash":
            continue
        value = getattr(brief, name)
        changed = brief.model_copy(update={name: _other(value)})
        assert brief_hash(changed) != brief.brief_hash, name


def _other(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value + 1
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, tuple):
        return value[1:] if value else ("x",)
    if value is None:
        return "x"
    if hasattr(value, "model_copy"):
        first = next(iter(type(value).model_fields))
        return value.model_copy(update={first: _other(getattr(value, first))})
    return value


def test_a_goal_that_is_not_a_use_case_is_refused() -> None:
    for goal_id in ("no-such-goal", "../etc", "Telco"):
        with pytest.raises(ConfigError) as caught:
            load_goal_brief(goal_id)
        assert caught.value.code == "USE_CASE_NOT_FOUND"


# ---------------------------------------------------------------------------
# The tool (#35)
# ---------------------------------------------------------------------------
def test_the_tool_matches_its_catalogue_row_and_a_viewer_may_call_it() -> None:
    tool = goal_brief_tool()
    entry = row(GOAL_BRIEF_TOOL)
    assert entry is not None and entry.number == tool.number == 35
    assert entry.kind == tool.kind.value == DataToolKind.READ.value
    assert entry.exposure == tool.exposure.value == Exposure.SESSION_ONLY.value
    registry = DataToolRegistry()
    register_goal_brief_tool(registry)
    register_goal_brief_tool(registry)  # twice is once
    assert registry.names() == (GOAL_BRIEF_TOOL,)
    ctx = DataToolContext(principal=VIEWER, caller="ai")
    first = registry.call(GOAL_BRIEF_TOOL, {"goal_id": "telco-churn"}, ctx)
    second = registry.call(GOAL_BRIEF_TOOL, {"goal_id": "telco-churn"}, ctx)
    assert first.evidence_id == second.evidence_id, "the same question gives the same evidence id"
    assert GoalBrief.model_validate(first.output).brief_hash == load_goal_brief("telco-churn").brief_hash
    with pytest.raises(DataToolError) as caught:
        registry.call(GOAL_BRIEF_TOOL, {"goal_id": "no-such-goal"}, ctx)
    assert caught.value.code == "USE_CASE_NOT_FOUND" and caught.value.status == 404
    assert GOAL_BRIEF_TOOL in visit_registry().names()
    assert visit_registry().mcp_surface() == (), "Plan O lists no Data Doctor tool on MCP (O7)"


@pytest.mark.parametrize("goal_id", _ids())
def test_the_brief_holds_no_client_data_and_passes_the_egress_gate_untouched(goal_id: str) -> None:
    """Nothing of it looks like personal data. (The hash itself reads as a token to the gate and would be masked in a
    prompt, which costs nothing: it means nothing to the AI.)"""
    text = json.dumps(
        load_goal_brief(goal_id).model_dump(mode="json", exclude={"brief_hash"}), ensure_ascii=False
    )
    cleaned, masked = assert_clean(text)
    assert masked == 0 and cleaned == text
