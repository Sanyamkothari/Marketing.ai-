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


# Artefacts the engine writes that plan section 7 does not list, each with the reason it exists.
BEYOND_PLAN_SECTION_7 = {
    "schema.json",  # plan section 4.4: the feature schema saved with every model
    "run_config.json",  # plan section 5: the merged configuration a run resolved to
    "run_manifest.json",  # DEC-042: one flat, queryable record per run
    "job_spec.json",  # DEC-324: the declarative description a remote job is handed
    "shap_beeswarm.json",  # DEC-802: the Model page's Details beeswarm, laid out at explain time
}


def test_registry_covers_exactly_the_plan_artefacts_plus_the_recorded_additions() -> None:
    assert PLAN_SECTION_7 | BEYOND_PLAN_SECTION_7 == KNOWN


def test_every_addition_beyond_the_plan_is_deliberate() -> None:
    # A new artefact must be added here consciously, not absorbed silently.
    assert KNOWN - PLAN_SECTION_7 == BEYOND_PLAN_SECTION_7


def test_run_config_is_the_config_module_s_resolved_config() -> None:
    assert ARTEFACT_REGISTRY["run_config.json"] is ResolvedConfig


def test_model_version_is_not_a_run_artefact() -> None:
    assert ModelVersion not in set(ARTEFACT_REGISTRY.values())
    assert ModelVersion not in set(TABULAR_SCHEMAS.values())


def test_the_job_spec_is_registered_but_is_not_something_a_flow_writes() -> None:
    """DEC-324: the route hands the spec to the run; `run_train`/`run_score` never write it.

    `TRAIN_ARTEFACTS` and `SCORE_ARTEFACTS` mean "everything the flow writes", and the integration
    tests assert real run directories against them. Adding `job_spec.json` to either would make the
    pipeline responsible for a document it is given, which is the opposite of what it is.
    """
    assert "job_spec.json" in ARTEFACT_REGISTRY
    assert "job_spec.json" not in TRAIN_ARTEFACTS
    assert "job_spec.json" not in SCORE_ARTEFACTS


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
        "run_manifest.json",  # DEC-042: every run writes one, train and score alike
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
