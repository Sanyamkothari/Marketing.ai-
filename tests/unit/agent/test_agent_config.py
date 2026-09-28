"""The `agent:` block (Plan G M70): loads for every use case, validates loudly, never moves a run."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.agent.config import AGENT_KNOWLEDGE_MAX_ITEMS, AgentColumnHints, AgentConfig, AgentLevel
from engine.agent.scope import agent_available
from engine.config import (
    ConfigError,
    load_all_use_cases,
    load_use_case,
    overridable_paths,
    recipe_from_config,
    resolve_config,
)


def test_every_use_case_loads_with_an_agent_block(config_root: Path) -> None:
    for use_case_id, config in load_all_use_cases(config_root).items():
        assert isinstance(config.agent, AgentConfig), use_case_id
        assert config.agent.name_for(config.name), use_case_id


def test_every_trainable_use_case_has_a_helper_and_generative_ones_do_not(config_root: Path) -> None:
    configs = load_all_use_cases(config_root)
    for use_case_id, config in configs.items():
        assert agent_available(config) is config.trainable_in_phase_1, use_case_id
    assert not agent_available(configs["ai-onboarding-assistant"])
    assert agent_available(configs["targeted-advertisement"])


def test_every_trainable_use_case_tells_its_helper_what_it_predicts(config_root: Path) -> None:
    """Plan G §8.1: each use case's YAML gives its helper a goal and at least one fact."""
    for use_case_id, config in load_all_use_cases(config_root).items():
        if not agent_available(config):
            continue
        assert config.agent.goal_for(config.target.definition), use_case_id
        assert config.agent.knowledge, f"{use_case_id} gives its helper no knowledge"


def test_the_goal_falls_back_to_the_target_definition() -> None:
    assert AgentConfig().goal_for("Bought within 30 days") == "Bought within 30 days"
    assert AgentConfig(goal="Find buyers").goal_for("Bought within 30 days") == "Find buyers"
    assert AgentConfig().goal_for(None) == ""


def test_the_display_name_defaults_to_the_use_case_name() -> None:
    assert AgentConfig().name_for("Payment Propensity") == "Payment Propensity helper"
    assert AgentConfig(display_name="Pay helper").name_for("Payment Propensity") == "Pay helper"


def test_no_agent_path_is_overridable_per_run(config_root: Path) -> None:
    config = load_use_case("targeted-advertisement", config_root)
    assert not any(path == "agent" or path.startswith("agent.") for path in overridable_paths(config))
    with pytest.raises(ConfigError) as caught:
        resolve_config("targeted-advertisement", {"agent.enabled": False}, root=config_root)
    assert caught.value.code == "OVERRIDE_UNKNOWN_PATH"


def test_the_agent_block_does_not_change_the_training_recipe_hash(config_root: Path) -> None:
    base = load_use_case("targeted-advertisement", config_root)
    changed = base.model_copy(update={"agent": AgentConfig(knowledge=("Something else entirely.",))})
    kwargs = {"primary_key": "customer_id", "feature_columns": ("visits_last_7d",), "seed": 7}
    assert recipe_from_config(base, **kwargs).recipe_hash == recipe_from_config(changed, **kwargs).recipe_hash


def test_unknown_agent_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({"enabled": True, "autonomy": "full"})


def test_levels_must_start_from_clean_and_are_kept_in_canonical_order() -> None:
    assert AgentConfig(levels=(AgentLevel.DERIVE, AgentLevel.CLEAN)).levels == (
        AgentLevel.CLEAN,
        AgentLevel.DERIVE,
    )
    for bad in ((), (AgentLevel.DERIVE,), (AgentLevel.CLEAN, AgentLevel.CLEAN)):
        with pytest.raises(ValidationError):
            AgentConfig(levels=bad)


def test_knowledge_is_short_and_bounded() -> None:
    assert AgentConfig(knowledge=("  one  ", "", "two")).knowledge == ("one", "two")
    with pytest.raises(ValidationError):
        AgentConfig(knowledge=tuple(f"fact {i}" for i in range(AGENT_KNOWLEDGE_MAX_ITEMS + 1)))
    with pytest.raises(ValidationError):
        AgentConfig(knowledge=("x" * 301,))


def test_column_hints_refuse_duplicates_case_insensitively() -> None:
    assert AgentColumnHints(target_synonyms=(" bought ", "")).target_synonyms == ("bought",)
    with pytest.raises(ValidationError):
        AgentColumnHints(consent=("opt_in", "OPT_IN"))


def test_a_malformed_agent_block_in_yaml_is_a_config_error(tmp_path: Path, config_root: Path) -> None:
    """A bad value in a use-case file fails loading with the dotted path, like any other leaf."""
    import shutil

    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    path = root / "use_cases" / "targeted_advertisement.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\n  max_tool_steps_per_turn: 99\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_use_case("targeted-advertisement", root)
    assert "agent.max_tool_steps_per_turn" in str(caught.value)
