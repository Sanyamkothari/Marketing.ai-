"""The artefact registry against the plan section 7 file list, transcribed literally."""

from __future__ import annotations

import pytest

from engine.config import ResolvedConfig
from engine.contracts import (
    ARTEFACT_REGISTRY,
    MODEL_DIRECTORY,
    SCORE_ARTEFACTS,
    TABULAR_SCHEMAS,
    TRAIN_ARTEFACTS,
    ModelVersion,
    artefact_model,
)

# plan.md section 7, "Every run directory data/runs/<run_id>/ contains, at most:" - transcribed by
# hand (never parsed), with "scores.csv / scores.parquet" split into two names and "model/" kept.
PLAN_SECTION_7 = frozenset(
    {
        "run.json",
        "status.json",
        "profile.json",
        "validation.json",
        "prepare.json",
        "split.json",
        "leaderboard.json",
        "best_model.json",
        "evaluation.json",
        "confusion_matrix.json",
        "decile_lift.json",
        "baseline.json",
        "fairness.json",
        "feature_importance.json",
        "row_explanations.parquet",
        "drift_baseline.json",
        "drift.json",
        "scoring_summary.json",
        "scores.csv",
        "scores.parquet",
        "model/",
    }
)

KNOWN = set(ARTEFACT_REGISTRY) | set(TABULAR_SCHEMAS) | {MODEL_DIRECTORY}


def test_plan_section_7_is_twenty_one_names() -> None:
    assert len(PLAN_SECTION_7) == 21


def test_registry_covers_exactly_the_plan_artefacts() -> None:
    # schema.json (plan section 4.4) and run_config.json (plan section 5) are saved with every run too.
    assert PLAN_SECTION_7 | {"schema.json", "run_config.json"} == KNOWN


def test_run_config_is_the_config_module_s_resolved_config() -> None:
    assert ARTEFACT_REGISTRY["run_config.json"] is ResolvedConfig


def test_model_version_is_not_a_run_artefact() -> None:
    assert ModelVersion not in set(ARTEFACT_REGISTRY.values())
    assert ModelVersion not in set(TABULAR_SCHEMAS.values())


def test_mode_artefact_sets_are_known_names() -> None:
    assert TRAIN_ARTEFACTS <= KNOWN
    assert SCORE_ARTEFACTS <= KNOWN
    assert MODEL_DIRECTORY in TRAIN_ARTEFACTS
    assert MODEL_DIRECTORY not in SCORE_ARTEFACTS
    assert {
        "run.json",
        "status.json",
        "run_config.json",
        "profile.json",
        "validation.json",
        "prepare.json",
        "row_explanations.parquet",
    } == TRAIN_ARTEFACTS & SCORE_ARTEFACTS


@pytest.mark.parametrize("filename", sorted(set(ARTEFACT_REGISTRY) | set(TABULAR_SCHEMAS)))
def test_artefact_model_resolves_every_known_file(filename: str) -> None:
    assert artefact_model(filename) is {**ARTEFACT_REGISTRY, **TABULAR_SCHEMAS}[filename]


def test_artefact_model_lists_the_known_files_when_it_fails() -> None:
    with pytest.raises(KeyError) as excinfo:
        artefact_model("nope.json")
    message = str(excinfo.value)
    assert "nope.json" in message
    for filename in KNOWN:
        assert filename in message
