"""Plan A ruling D2: the six `features` settings no stage reads are kept, shown inert, and unhashed.

`features.auto_feature_engineering`, `categorical_encoding`, `numeric_scaling`, `text_columns`,
`selection` and `max_features` are agreed product intentions that nothing in the engine acts on
yet. D2 keeps them in the advanced-settings schema, rendered disabled under "Coming later", and out
of the recipe hash, because the hash answers "would this produce the same model" and a setting no
stage reads cannot change a model. DEC-074 built the mechanism (`ADVISORY_PATHS`); this module pins
what D2 asks of it, end to end:

* the schema the Setup screen is generated from marks every one of the six, for every use case
  that has the screen;
* two runs that differ only in those six share a recipe hash, both as `Recipe` objects and as the
  manifests two real `run_train` flows write;
* a setting a stage does read still changes the hash, so the exclusion is not a blanket one;
* an override of an inactive setting is still accepted and recorded, as it was before D2, so the
  run keeps what the user chose even though it is not part of the recipe's identity;
* the hash of a recipe is its canonical JSON less exactly those six fields, so single-key runs
  hashed before this milestone keep their hash.

The screen side (`ui/settings.js` renders the six disabled and never sends them) is driven through
node in `tests/integration/test_inactive_settings_ui.py`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

import pytest

from engine.config import (
    ADVISORY_NOTE,
    ADVISORY_PATHS,
    AiType,
    FeaturesConfig,
    Recipe,
    ResolvedConfig,
    advanced_settings_schema,
    list_use_case_ids,
    load_use_case,
    recipe_from_config,
    resolve_config,
)
from engine.contracts import RunManifest
from engine.pipeline import MANIFEST_FILENAME
from engine.registry import LocalModelRegistry
from engine.storage import run_key
from tests.unit.test_run_train import (
    RUN_ID,
    UPLOAD_KEY,
    RecordingStorage,
    StageStubs,
    run_flow,
)

USE_CASE: Final[str] = "targeted-advertisement"

INACTIVE: Final[tuple[str, ...]] = (
    "features.auto_feature_engineering",
    "features.categorical_encoding",
    "features.numeric_scaling",
    "features.text_columns",
    "features.selection",
    "features.max_features",
)
"""The six settings ruling D2 names, spelled out rather than read from `ADVISORY_PATHS`."""

MOVED: Final[dict[str, Any]] = {
    "features.auto_feature_engineering": False,
    "features.categorical_encoding": "one_hot",
    "features.numeric_scaling": "standard",
    "features.text_columns": "tfidf",
    "features.selection": "pca",
    "features.max_features": 300,
}
"""A valid, non-default value for each of the six."""

WIRED: Final[dict[str, Any]] = {
    "prepare.missing_values": "drop_rows",
    "prepare.outliers": "keep",
    "prepare.deduplicate": False,
    "split.validation_fraction": 0.2,
    "model_search.strategy": "fast",
    "model_search.tuning_trials": 9,
    "model_search.time_limit_minutes": 12,
    "model_search.folds": 3,
    "model_search.imbalance": "class_weights",
}
"""Recipe settings a stage does read, each moved off the demo use case's value."""


def settings_use_case_ids() -> tuple[str, ...]:
    """Every use case that has an advanced-settings screen; a generative one has no stages."""
    return tuple(
        sorted(
            use_case_id
            for use_case_id in list_use_case_ids()
            if load_use_case(use_case_id).ai_type is not AiType.GENERATIVE
        )
    )


def recipe_for(resolved: ResolvedConfig, **changes: Any) -> Recipe:
    arguments: dict[str, Any] = {
        "primary_key": "customer_id",
        "feature_columns": ("visits_last_7d", "plan_tier"),
        "seed": 7,
    }
    arguments.update(changes)
    return recipe_from_config(resolved.config, **arguments)


def _value(resolved: ResolvedConfig, path: str) -> Any:
    block, _, field = path.partition(".")
    value = getattr(getattr(resolved.config, block), field)
    return getattr(value, "value", value)


# ---------------------------------------------------------------------------
# The set and the schema the screen is generated from
# ---------------------------------------------------------------------------
def test_the_six_are_the_whole_features_block_and_all_parked() -> None:
    """If a seventh `features` field appears, D2 has to be asked about it rather than inherited."""
    assert {path.partition(".")[2] for path in INACTIVE} == set(FeaturesConfig.model_fields)
    assert set(INACTIVE) <= ADVISORY_PATHS


def test_the_moved_values_really_move_each_setting() -> None:
    """Otherwise the hash tests below would pass by overriding nothing."""
    resolved = resolve_config(USE_CASE, {})
    for path, value in (*MOVED.items(), *WIRED.items()):
        assert _value(resolved, path) != value, path


def test_a_narrowed_sweep_still_covers_something() -> None:
    assert USE_CASE in settings_use_case_ids()


@pytest.mark.parametrize("use_case_id", settings_use_case_ids())
def test_every_screen_renders_the_six_disabled_under_coming_later(use_case_id: str) -> None:
    payload = advanced_settings_schema(load_use_case(use_case_id)).model_dump(mode="json")
    fields = {field["path"]: field for stage in payload["stages"] for field in stage["fields"]}
    for path in INACTIVE:
        assert fields[path]["advisory"] is True, path
        assert fields[path]["help"] == ADVISORY_NOTE, path
        assert fields[path]["help"].startswith("Coming later"), path


@pytest.mark.parametrize("use_case_id", settings_use_case_ids())
def test_the_six_stay_in_the_schema_with_their_values(use_case_id: str) -> None:
    """Kept, not removed: the recorded value and the agreed choices are still on the screen."""
    config = load_use_case(use_case_id)
    payload = advanced_settings_schema(config).model_dump(mode="json")
    fields = {field["path"]: field for stage in payload["stages"] for field in stage["fields"]}
    for path in INACTIVE:
        block, _, name = path.partition(".")
        expected = getattr(getattr(config, block), name)
        assert fields[path]["value"] == getattr(expected, "value", expected), path


# ---------------------------------------------------------------------------
# The recipe hash
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", INACTIVE)
def test_moving_one_inactive_setting_keeps_the_recipe_hash(path: str) -> None:
    base = recipe_for(resolve_config(USE_CASE, {}))
    moved = recipe_for(resolve_config(USE_CASE, {path: MOVED[path]}))
    assert moved.recipe_hash == base.recipe_hash
    assert moved != base, "the recipe still records the choice; only its identity ignores it"


def test_moving_all_six_together_keeps_the_recipe_hash() -> None:
    base = recipe_for(resolve_config(USE_CASE, {}))
    moved = recipe_for(resolve_config(USE_CASE, MOVED))
    assert moved.recipe_hash == base.recipe_hash
    assert moved.features.max_features == 300
    assert moved.features.auto_feature_engineering is False


@pytest.mark.parametrize("path", sorted(WIRED))
def test_a_setting_a_stage_reads_still_changes_the_recipe_hash(path: str) -> None:
    base = recipe_for(resolve_config(USE_CASE, {}))
    moved = recipe_for(resolve_config(USE_CASE, {path: WIRED[path]}))
    assert moved.recipe_hash != base.recipe_hash


def test_a_wired_setting_changes_the_hash_even_beside_the_six() -> None:
    """The six are dropped, not the block around them: one real change among them still counts."""
    quiet = recipe_for(resolve_config(USE_CASE, MOVED))
    loud = recipe_for(resolve_config(USE_CASE, {**MOVED, "model_search.tuning_trials": 9}))
    assert loud.recipe_hash != quiet.recipe_hash


@pytest.mark.parametrize(
    "changes",
    [
        {"seed": 8},
        {"feature_columns": ("visits_last_7d",)},
        {"primary_key": "account_id"},
        {"target": "churned"},
    ],
    ids=["seed", "feature_columns", "primary_key", "target"],
)
def test_the_rest_of_the_recipe_still_changes_the_hash(changes: dict[str, Any]) -> None:
    resolved = resolve_config(USE_CASE, {})
    assert recipe_for(resolved, **changes).recipe_hash != recipe_for(resolved).recipe_hash


def test_the_hash_is_the_canonical_recipe_less_exactly_the_six() -> None:
    """Recomputed independently: nothing else is dropped, so earlier single-key hashes still hold.

    DEC-074 already hashed the recipe this way before D2 was written, so no stored `recipe_hash`
    changes with this milestone; this is the pin that keeps it so.
    """
    recipe = recipe_for(resolve_config(USE_CASE, MOVED))
    payload = recipe.model_dump(mode="json")
    for path in INACTIVE:
        block, _, field = path.partition(".")
        del payload[block][field]
    assert payload["features"] == {}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert recipe.recipe_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_a_single_key_recipe_hashes_its_key_as_a_plain_string() -> None:
    """D1 normalises keys to a list internally; a one-column key still hashes as it always did."""
    recipe = recipe_for(resolve_config(USE_CASE, {}))
    assert recipe.model_dump(mode="json")["primary_key"] == "customer_id"


# ---------------------------------------------------------------------------
# Overrides: accepted and recorded, as before D2
# ---------------------------------------------------------------------------
def test_an_override_of_an_inactive_setting_is_accepted_and_recorded() -> None:
    """Existing API behaviour, kept: the run records what was asked, the hash ignores it.

    Refusing it would turn a request an older client or a saved scenario could still send into a
    422 for a value that does nothing either way, and the on-screen note already says these are
    "recorded with the run".
    """
    resolved = resolve_config(USE_CASE, MOVED)
    for path, value in MOVED.items():
        block, _, field = path.partition(".")
        assert resolved.overrides_applied[block][field] == value
        assert resolved.sources[path] == "override"
        assert _value(resolved, path) == value


# ---------------------------------------------------------------------------
# Two runs through the real pipeline, with the expensive stages stubbed
# ---------------------------------------------------------------------------
def _run_once(root: Path, resolved: ResolvedConfig, monkeypatch: pytest.MonkeyPatch) -> RunManifest:
    StageStubs().install(monkeypatch)
    storage = RecordingStorage(root / "data")
    storage.write_bytes(UPLOAD_KEY, b"customer_id,converted_30d\nC-1,1\n")
    registry = LocalModelRegistry(root / "registry.db")
    run_flow(resolved, storage, registry)
    return storage.read_model(run_key(RUN_ID, MANIFEST_FILENAME), RunManifest)


def test_two_runs_differing_only_in_the_six_share_a_recipe_hash(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _run_once(tmp_path / "first", resolve_config(USE_CASE, {}, root=config_root), monkeypatch)
    second = _run_once(tmp_path / "second", resolve_config(USE_CASE, MOVED, root=config_root), monkeypatch)
    assert first.recipe is not None and second.recipe is not None
    assert second.recipe.recipe_hash == first.recipe.recipe_hash
    # ... and each manifest still says what its run was asked for.
    assert second.recipe.features != first.recipe.features
    assert second.recipe.features.max_features == 300


def test_two_runs_differing_in_a_wired_setting_do_not(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _run_once(tmp_path / "first", resolve_config(USE_CASE, {}, root=config_root), monkeypatch)
    moved = resolve_config(USE_CASE, {"model_search.tuning_trials": 9}, root=config_root)
    second = _run_once(tmp_path / "second", moved, monkeypatch)
    assert first.recipe is not None and second.recipe is not None
    assert second.recipe.recipe_hash != first.recipe.recipe_hash
