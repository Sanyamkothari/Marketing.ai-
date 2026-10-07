"""The holdout's configuration (Plan J M92): scopes, the fraction rule, the explore bound, the salt setting."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from engine.config import (
    ConfigError,
    advanced_settings_schema,
    list_use_case_ids,
    load_use_case,
    resolve_config,
)
from engine.holdout.spec import (
    HoldoutConfig,
    HoldoutSpec,
    effective_holdout_fraction,
    is_persistent,
    service_on,
)
from engine.settings import ENV_VARS, SECRET_FIELDS, Settings, SettingsError, redacted, summary
from engine.uplift.measure import measure_offered
from tests.unit.holdout.support import SALT, use_case


def test_every_shipped_use_case_defaults_to_todays_per_run_draw() -> None:
    for use_case_id in list_use_case_ids():
        config = load_use_case(use_case_id)
        assert config.actions.holdout == HoldoutConfig()
        assert config.actions.holdout.scope == "run"
        assert config.actions.explore_fraction == 0.0
        assert not service_on(config.actions, config.actions.explore_fraction)
        assert effective_holdout_fraction(config.actions) == config.actions.control_group_fraction


@pytest.mark.parametrize("scope", ["use_case", "universal"])
def test_a_persistent_scope_needs_its_own_fraction(scope: str) -> None:
    with pytest.raises(ValidationError, match="fraction is required"):
        use_case(scope=scope)


def test_scope_run_refuses_a_holdout_fraction_it_would_not_read() -> None:
    with pytest.raises(ValidationError, match="applies only to a persistent scope"):
        use_case(scope="run", fraction=0.1)


@pytest.mark.parametrize("fraction", [0.0, 0.51, -0.1])
def test_a_persistent_fraction_is_above_zero_and_at_most_half(fraction: float) -> None:
    with pytest.raises(ValidationError):
        use_case(scope="universal", fraction=fraction)


@pytest.mark.parametrize("explore", [-0.01, 0.11, 0.5])
def test_the_explore_slice_is_at_most_ten_percent(explore: float) -> None:
    with pytest.raises(ValidationError):
        use_case(explore=explore)


def test_under_a_persistent_scope_the_holdout_fraction_wins() -> None:
    config = use_case(scope="universal", fraction=0.05, control_fraction=0.20)
    assert is_persistent(config.actions)
    assert effective_holdout_fraction(config.actions) == 0.05
    run = use_case(control_fraction=0.20)
    assert effective_holdout_fraction(run.actions) == 0.20


def test_measure_offered_reads_the_effective_fraction() -> None:
    """A persistent holdout holds people back even with control_group_fraction 0, so step 4 is offered."""
    persistent = use_case(scope="use_case", fraction=0.10, control_fraction=0.0)
    assert measure_offered(persistent)
    assert not measure_offered(use_case(control_fraction=0.0))


def test_the_explore_slice_alone_engages_the_service() -> None:
    config = use_case(explore=0.05)
    assert service_on(config.actions, config.actions.explore_fraction)
    assert not is_persistent(config.actions)


@pytest.mark.parametrize(
    "path, value",
    [("actions.holdout.scope", "universal"), ("actions.explore_fraction", 0.05), ("actions.holdout", {})],
)
def test_neither_setting_can_be_changed_per_run(path: str, value: object) -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_config("targeted-advertisement", {path: value})
    assert caught.value.code == "OVERRIDE_UNKNOWN_PATH"


def test_a_run_spec_carries_no_salt() -> None:
    spec = HoldoutSpec(
        scope="universal", fraction=0.1, salt_id="0123456789abcdef", epoch=2, scope_key="universal"
    )
    assert SALT not in spec.model_dump_json()


# ---------------------------------------------------------------------------
# The salt setting
# ---------------------------------------------------------------------------
def test_the_salt_is_a_secret_setting() -> None:
    assert ENV_VARS["holdout_salt"] == "MARKETING_AI_HOLDOUT_SALT"
    assert "holdout_salt" in SECRET_FIELDS
    settings = Settings.from_env({"MARKETING_AI_HOLDOUT_SALT": SALT})
    assert isinstance(settings.holdout_salt, SecretStr)
    assert settings.holdout_salt.get_secret_value() == SALT
    assert redacted(settings)["holdout_salt"] != SALT
    assert SALT not in summary(settings)
    assert SALT not in repr(settings)


def test_a_short_salt_is_refused() -> None:
    with pytest.raises(SettingsError) as caught:
        Settings.from_env({"MARKETING_AI_HOLDOUT_SALT": "too-short"})
    assert caught.value.env_var == "MARKETING_AI_HOLDOUT_SALT"


def test_the_salt_has_no_default() -> None:
    assert Settings.from_env({}).holdout_salt is None


def _actions_summary(config: object) -> str:
    stages = advanced_settings_schema(config).stages  # type: ignore[arg-type]
    return next(
        stage.summary for stage in stages if stage.summary_template and "actions." in stage.summary_template
    )


def test_the_actions_summary_reads_the_effective_fraction() -> None:
    """DEC-1302 (d): no reader shows the unused control_group_fraction under a persistent scope."""
    assert _actions_summary(use_case()).endswith(" · 10% control group"), "scope run: today's words"
    for scope in ("use_case", "universal"):
        line = _actions_summary(use_case(scope=scope, fraction=0.05))
        assert line.endswith(" · 5% held out, the same customers every run"), line
        assert "control group" not in line


def test_the_settings_card_suite_passes() -> None:
    """`settings_card.test.mjs`: the run's settings card words the effective share by scope."""
    import subprocess
    from pathlib import Path

    from tests.fixtures.node import skip_without_node

    node = skip_without_node()
    result = subprocess.run(
        [node, "--test", str(Path(__file__).resolve().parent / "settings_card.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout
