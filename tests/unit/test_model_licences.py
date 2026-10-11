"""Plan K M143 (DEC-1410): the model licence allow-list and the opt-in `foundation` strategy.

What these pin:

* a strategy or preset that would load a model whose licence bars commercial use (TabPFN 2.5 and
  later, AutoGluon's `noncommercial` preset) is refused when the configuration is resolved, with
  `MODEL_LICENCE_BLOCKED`, and so is anything the list has not reviewed (default deny);
* every preset on the allow-list, read through the installed AutoGluon, loads only models on the
  allow-list, so a library upgrade that slips a blocked model into a preset fails here;
* `foundation` resolves to `extreme_quality`, and nothing else about configuration resolution
  changes: the four shipped-by-default values for every use case hash to what they were before M143;
* a small real fit with the `foundation` strategy runs on CPU (`slow`).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.config import (
    DEFAULT_CONFIG_ROOT,
    ConfigError,
    EvaluationConfig,
    FeaturesConfig,
    Metric,
    ModelSearchConfig,
    PrepareConfig,
    ProblemType,
    Recipe,
    SplitConfig,
    Strategy,
    advanced_settings_schema,
    get_catalog,
    list_use_case_ids,
    load_use_case,
    resolve_config,
)
from engine.jobs import CancelToken
from engine.model_licences import (
    LICENCES_RELATIVE_PATH,
    load_model_licences,
    portfolio_model_keys,
)
from engine.stages.train import autogluon_fit_kwargs, train
from engine.storage import LocalStorage

BLOCKED = "MODEL_LICENCE_BLOCKED"


def _root_mapping(tmp_path: Path, strategy: str, preset: str) -> Path:
    """A copy of `configs/` whose catalog maps `strategy` to `preset`."""
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    engine_yaml = root / "engine.yaml"
    lines = engine_yaml.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith(f"{strategy}:") and "# str" in line:
            line = f"    {strategy}: {preset}          # str\n"
            replaced = True
        out.append(line)
    assert replaced, f"engine.yaml has no strategy_presets line for {strategy!r}"
    engine_yaml.write_text("".join(out), encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# The allow-list refuses a model that bars commercial use
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("preset", ["noncommercial", "noncommercial_v160"])
def test_a_strategy_mapped_to_the_noncommercial_preset_is_refused(tmp_path: Path, preset: str) -> None:
    root = _root_mapping(tmp_path, "balanced", preset)
    with pytest.raises(ConfigError) as refused:
        resolve_config("telco-churn", root=root)
    assert refused.value.code == BLOCKED
    assert refused.value.path == "model_search.strategy"
    assert preset in refused.value.message
    assert "TabPFN" in refused.value.message  # the plain reason names the licence problem


def test_the_refusal_also_holds_when_the_use_case_is_only_loaded(tmp_path: Path) -> None:
    root = _root_mapping(tmp_path, "balanced", "noncommercial")
    with pytest.raises(ConfigError) as refused:
        load_use_case("telco-churn", root)
    assert refused.value.code == BLOCKED


def test_the_refusal_follows_the_strategy_a_run_picks_not_every_catalog_line(tmp_path: Path) -> None:
    """A catalog that maps an unused strategy to a blocked preset only matters if a run picks it."""
    root = _root_mapping(tmp_path, "exhaustive", "noncommercial")
    resolve_config("telco-churn", root=root)  # balanced, unaffected
    with pytest.raises(ConfigError) as refused:
        resolve_config("telco-churn", {"model_search.strategy": "exhaustive"}, root=root)
    assert refused.value.code == BLOCKED


def test_a_preset_the_list_has_not_reviewed_is_refused_by_default(tmp_path: Path) -> None:
    root = _root_mapping(tmp_path, "balanced", "tabarena")  # loads TabPFN through the `tabpfn` package
    with pytest.raises(ConfigError) as refused:
        resolve_config("telco-churn", root=root)
    assert refused.value.code == BLOCKED
    assert "not on the allow-list" in refused.value.message


@pytest.mark.parametrize("alias", ["extreme", "extreme_v160", "eq", "extreme_quality"])
def test_the_commercial_preset_and_its_aliases_are_allowed(tmp_path: Path, alias: str) -> None:
    root = _root_mapping(tmp_path, "balanced", alias)
    resolved = resolve_config("telco-churn", root=root)
    assert resolved.config.catalog.strategy_presets[Strategy.BALANCED] == alias


@pytest.mark.parametrize(
    "key",
    ["REALTABPFN-V2.5", "TABPFN-2.6", "TABPFN-3", "TABPFN-3.5", "TABPFN-4", "REALTABPFN-V3"],
)
def test_a_noncommercial_model_key_is_refused(key: str) -> None:
    licences = load_model_licences()
    with pytest.raises(ConfigError) as refused:
        licences.check_model_key(key)
    assert refused.value.code == BLOCKED
    assert key in refused.value.message


@pytest.mark.parametrize(
    "key", ["NORI", "TABICL", "TABDPT-TURBO", "XGB", "GBM", "RF", "LR", "CAT", "NN_TORCH"]
)
def test_a_commercial_safe_model_key_is_allowed(key: str) -> None:
    load_model_licences().check_model_key(key)


def test_a_model_key_nobody_reviewed_is_refused() -> None:
    with pytest.raises(ConfigError) as refused:
        load_model_licences().check_model_key("SOME-NEW-FOUNDATION-MODEL")
    assert refused.value.code == BLOCKED
    assert "not on the allow-list" in refused.value.message


def test_the_shipped_list_names_what_the_proposal_bars() -> None:
    """K8: TabPFN-2.5, 2.6, 3 and 3.5 and AutoGluon's `noncommercial` preset are blocked by name."""
    licences = load_model_licences()
    assert {"noncommercial", "noncommercial_v160"} <= set(licences.blocked_presets)
    keys = " ".join(licences.blocked_model_keys)
    for fragment in ("REALTABPFN-V2.[5-9]", "TABPFN-*"):
        assert fragment in keys
    for plain_name in ("TabPFN-2.5", "TabPFN-2.6", "TabPFN-3", "TabPFN-3.5"):
        assert plain_name in " ".join(licences.blocked_model_keys.values()), plain_name


def test_every_strategy_the_catalog_ships_is_on_the_list() -> None:
    licences = load_model_licences()
    for preset in get_catalog().strategy_presets.values():
        licences.check_preset(preset)


def test_a_broken_list_is_a_config_error_not_a_pass(tmp_path: Path) -> None:
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    (root / LICENCES_RELATIVE_PATH).write_text(
        "schema_version: 1\nallowed_presets: not-a-list\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as refused:
        resolve_config("telco-churn", root=root)
    assert refused.value.code == "CONFIG_INVALID"


def test_a_root_without_its_own_list_is_read_against_the_shipped_one(tmp_path: Path) -> None:
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    (root / LICENCES_RELATIVE_PATH).unlink()
    resolve_config("telco-churn", root=root)  # still validated, against the shipped list
    root2 = _root_mapping(tmp_path / "other", "balanced", "noncommercial")
    (root2 / LICENCES_RELATIVE_PATH).unlink()
    with pytest.raises(ConfigError) as refused:
        resolve_config("telco-churn", root=root2)
    assert refused.value.code == BLOCKED


# ---------------------------------------------------------------------------
# The installed AutoGluon: what each allowed preset really loads
# ---------------------------------------------------------------------------
def test_every_allowed_preset_loads_only_allowed_models_in_the_installed_autogluon() -> None:
    licences = load_model_licences()
    seen = 0
    for preset in licences.allowed_presets:
        keys = portfolio_model_keys(preset)
        for key in keys:
            licences.check_model_key(key)
        seen += 1
    assert seen == len(licences.allowed_presets)
    assert {"NORI", "TABICL", "TABDPT-TURBO"} <= set(portfolio_model_keys("extreme_quality"))


def test_the_check_is_not_vacuous_the_noncommercial_preset_really_loads_tabpfn() -> None:
    licences = load_model_licences()
    keys = portfolio_model_keys("noncommercial")
    assert "TABPFN-3" in keys
    with pytest.raises(ConfigError) as refused:
        for key in keys:
            licences.check_model_key(key)
    assert refused.value.code == BLOCKED


def test_every_preset_the_list_names_exists_in_the_installed_autogluon() -> None:
    """A typo, or a preset a library upgrade renamed, would otherwise sit in the list doing nothing."""
    from autogluon.tabular.configs.presets_configs import tabular_presets_alias, tabular_presets_dict

    installed = set(tabular_presets_dict) | set(tabular_presets_alias)
    licences = load_model_licences()
    assert not set(licences.allowed_presets) & set(licences.blocked_presets)
    assert sorted((set(licences.allowed_presets) | set(licences.blocked_presets)) - installed) == []


# ---------------------------------------------------------------------------
# The opt-in strategy; the defaults do not move
# ---------------------------------------------------------------------------
def test_foundation_maps_to_the_commercial_preset() -> None:
    assert Strategy.FOUNDATION.value == "foundation"
    assert get_catalog().strategy_presets[Strategy.FOUNDATION] == "extreme_quality"


def test_a_run_can_opt_in_with_an_override_and_the_resolution_says_what_it_does() -> None:
    resolved = resolve_config("telco-churn", {"model_search.strategy": "foundation"})
    assert resolved.config.model_search.strategy is Strategy.FOUNDATION
    assert resolved.sources["model_search.strategy"] == "override"
    assert len(resolved.warnings) == 1
    assert "foundation" in resolved.warnings[0]
    assert "model_search.candidates" in resolved.warnings[0]


def test_no_shipped_use_case_selects_foundation() -> None:
    for use_case in list_use_case_ids():
        assert resolve_config(use_case).config.model_search.strategy is not Strategy.FOUNDATION


def test_the_form_does_not_offer_the_unproven_strategy() -> None:
    config = load_use_case("telco-churn")
    schema = advanced_settings_schema(config)
    fields = {field.path: field for stage in schema.stages for field in stage.fields}
    offered = [choice.value for choice in fields["model_search.strategy"].choices or ()]
    assert offered == ["fast", "balanced", "exhaustive"]


# The sha256 of each shipped use case's resolved model_search section, its warnings, its sources under
# model_search and the three original strategy presets, computed on the code before M143 existed
# (`resolve_config(use_case)`, canonical JSON with sorted keys). A change to a use case file's
# model_search, or to what the defaults resolve to, fails here on purpose.
RESOLVED_BEFORE_M143 = {
    "ai-onboarding-assistant": "bbbd16c2a10eedcf53e4c0d64f52dca16c43eae65df5abde60abcf1186011e1c",
    "bank-term-deposit": "bbbd16c2a10eedcf53e4c0d64f52dca16c43eae65df5abde60abcf1186011e1c",
    "card-default-propensity": "bbbd16c2a10eedcf53e4c0d64f52dca16c43eae65df5abde60abcf1186011e1c",
    "fault-prediction": "cc907b072bf9260702c7a91e9ab5000dea3ea570d9e2165b55203471daf4cc2e",
    "hillstrom-email": "bbbd16c2a10eedcf53e4c0d64f52dca16c43eae65df5abde60abcf1186011e1c",
    "insurance-cross-sell": "bbbd16c2a10eedcf53e4c0d64f52dca16c43eae65df5abde60abcf1186011e1c",
    "order-fulfillment": "dead280b1b26c2162585bc7b799d56e421ede7d77d09dd232e55898168892f61",
    "payment-propensity": "bbbd16c2a10eedcf53e4c0d64f52dca16c43eae65df5abde60abcf1186011e1c",
    "rca": "d37d2308d14c9ce1216d587250f12af9ca68d575025a71226f2fde10f7459c77",
    "retail-win-back": "613b08e9af6e86eb4d294cd7c7525be0d0f2cf724701b35690a3e96c5b67c905",
    "targeted-advertisement": "bbbd16c2a10eedcf53e4c0d64f52dca16c43eae65df5abde60abcf1186011e1c",
    "telco-churn": "bbbd16c2a10eedcf53e4c0d64f52dca16c43eae65df5abde60abcf1186011e1c",
    "win-back-campaign": "8048eff23ac2e48fac2f0093a605a64073e7d7dd35b9b52cd89b22e29de51869",
}


def test_default_config_resolution_is_what_it_was_before_m143() -> None:
    assert sorted(list_use_case_ids()) == sorted(RESOLVED_BEFORE_M143)
    for use_case, expected in RESOLVED_BEFORE_M143.items():
        resolved = resolve_config(use_case)
        document = {
            "model_search": json.loads(resolved.config.model_search.model_dump_json()),
            "warnings": list(resolved.warnings),
            "sources": {k: v for k, v in resolved.sources.items() if k.startswith("model_search")},
            "presets": {
                k.value: v
                for k, v in resolved.config.catalog.strategy_presets.items()
                if k.value in ("fast", "balanced", "exhaustive")
            },
        }
        digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        assert digest == expected, f"{use_case}: default resolution moved"


def test_the_three_original_strategies_keep_their_presets() -> None:
    presets = get_catalog().strategy_presets
    assert presets[Strategy.FAST] == "medium_quality"
    assert presets[Strategy.BALANCED] == "good_quality"
    assert presets[Strategy.EXHAUSTIVE] == "best_quality"


# ---------------------------------------------------------------------------
# A real (small) fit with the foundation strategy, on CPU
# ---------------------------------------------------------------------------
def _frame(rows: int, start: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    signal = rng.normal(size=rows)
    outcome = (signal + rng.normal(scale=0.8, size=rows) > 0).astype(int)
    return pd.DataFrame(
        {
            "row_id": [f"r{i}" for i in range(start, start + rows)],
            "signal": signal,
            "noise": rng.normal(size=rows),
            "converted": outcome,
        },
        index=pd.RangeIndex(start, start + rows),
    )


@pytest.mark.slow
def test_a_small_fit_with_the_foundation_strategy_runs_on_cpu(tmp_path: Path) -> None:
    recipe = Recipe(
        use_case_id="unit-test",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        target="converted",
        primary_key="row_id",
        feature_columns=("signal", "noise"),
        prepare=PrepareConfig(),
        split=SplitConfig(),
        features=FeaturesConfig(),
        model_search=ModelSearchConfig.model_validate(
            {
                "metric": Metric.ROC_AUC,
                "strategy": Strategy.FOUNDATION,
                "ensemble": False,
                "tuning_trials": 5,
                "time_limit_minutes": 1,
            }
        ),
        seed=4242,
    )
    assert autogluon_fit_kwargs(recipe)["presets"] == "extreme_quality"
    parts = {"train": _frame(300, 0, 1), "validation": _frame(120, 1000, 2), "test": _frame(120, 2000, 3)}
    result = train(
        recipe,
        parts,
        EvaluationConfig(),
        run_id="r_20261011_0000f00d",
        storage=LocalStorage(tmp_path / "data"),
        cancel=CancelToken(),
    )
    leaderboard = result.leaderboard
    assert leaderboard.presets == "extreme_quality"
    assert leaderboard.entries, "the search finished without a single model"
    assert 0.5 < result.best.validation_score <= 1.0
