"""`resolve_config` end to end: merge, override, provenance and a JSON round trip (design §4.9)."""

from __future__ import annotations

from typing import Any

import pytest

from engine.config import (
    AiType,
    ConfigError,
    ResolvedConfig,
    list_use_case_ids,
    load_use_case,
    resolve_config,
)

pytestmark = pytest.mark.integration

USE_CASE_IDS: tuple[str, ...] = tuple(
    # The override document below touches `split`, `actions.bands` and `prepare` - three of the
    # eight advanced-settings stages. A generative use case emits no such stages, so none of those
    # paths is overridable on one and every case here would be an `OVERRIDE_UNKNOWN_PATH`. The
    # sweep is read off the configs rather than listed, so a use case added by YAML alone still
    # needs no edit here (plan section 2.1).
    use_case_id
    for use_case_id in list_use_case_ids()
    if load_use_case(use_case_id).ai_type is not AiType.GENERATIVE
)

OVERRIDES: dict[str, Any] = {
    "split": {"test_fraction": 0.20},
    "actions.bands[0].min_score": 0.85,
    "prepare.exclude_columns": ["region"],
}
TOUCHED_LEAVES: frozenset[str] = frozenset(
    {"split.test_fraction", "actions.bands[0].min_score", "prepare.exclude_columns"}
)


@pytest.fixture(params=USE_CASE_IDS)
def resolved(request: pytest.FixtureRequest) -> ResolvedConfig:
    """The §4.9 override document applied to each shipped use case in turn."""
    return resolve_config(str(request.param), OVERRIDES)


def test_the_overrides_land_in_the_merged_config(resolved: ResolvedConfig) -> None:
    config = resolved.config
    assert config.actions.bands[0].min_score == 0.85
    assert config.split.test_fraction == 0.20
    assert config.prepare.exclude_columns == ("region",)


def test_the_untouched_band_fields_survive_the_index_patch(resolved: ResolvedConfig) -> None:
    """An index patch is the one exception to "lists replace" (DEC-029)."""
    before = load_use_case(resolved.use_case_id).actions.bands
    after = resolved.config.actions.bands
    assert [band.name for band in after] == [band.name for band in before]
    assert [band.action for band in after] == [band.action for band in before]
    assert [band.min_score for band in after[1:]] == [band.min_score for band in before[1:]]


def test_sources_mark_exactly_the_touched_leaves_as_override(resolved: ResolvedConfig) -> None:
    overridden = {leaf for leaf, source in resolved.sources.items() if source == "override"}
    assert overridden == TOUCHED_LEAVES


def test_identity_leaves_come_from_the_use_case_file(resolved: ResolvedConfig) -> None:
    assert resolved.sources["id"] == "use_case"
    assert resolved.sources["model_search.strategy"] == "engine"


def test_the_document_round_trips_through_json(resolved: ResolvedConfig) -> None:
    """`run_config.json` re-validates as the same document (plan §5, DEC-015)."""
    again = ResolvedConfig.model_validate_json(resolved.model_dump_json())
    assert again.schema_version == resolved.schema_version
    assert again.use_case_id == resolved.use_case_id
    assert again.resolved_at == resolved.resolved_at
    assert again.config == resolved.config
    assert again.sources == resolved.sources
    assert again.warnings == resolved.warnings
    # An index patch keys its mapping by `int`; JSON has string keys only, so the reloaded
    # `overrides_applied` carries "0" where the in-memory document carries 0.
    assert again.overrides_applied["split"] == resolved.overrides_applied["split"]


def test_the_narrowed_sweep_still_covers_something() -> None:
    """A sweep that shrank to nothing would look exactly like a passing suite."""
    assert len(USE_CASE_IDS) >= 5
    assert set(USE_CASE_IDS) < set(list_use_case_ids())


def test_a_generative_use_case_refuses_a_predictive_override() -> None:
    """The other half of the narrowing: refused by name, not silently ignored."""
    generative = [i for i in list_use_case_ids() if load_use_case(i).ai_type is AiType.GENERATIVE]
    assert generative, "no generative use case to check the refusal against"
    with pytest.raises(ConfigError) as error:
        resolve_config(generative[0], {"split.test_fraction": 0.20})
    assert error.value.code == "OVERRIDE_UNKNOWN_PATH"


def test_without_overrides_nothing_is_marked_override() -> None:
    resolved = resolve_config(USE_CASE_IDS[0])
    assert resolved.overrides_applied == {}
    assert "override" not in set(resolved.sources.values())
