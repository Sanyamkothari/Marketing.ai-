"""Every artefact contract: frozen, closed, described, round-trippable (spec section 5)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

import engine.contracts as contracts
from engine.config import list_use_case_ids, load_use_case, resolve_config
from engine.contracts import (
    ARTEFACT_REGISTRY,
    TABULAR_SCHEMAS,
    VALIDATION_CODES,
    Artefact,
    ConfusionMatrix,
    ModelVersion,
    RunRecord,
    RunState,
    RunStatus,
    StageKey,
    StageStatus,
    dump_artefact,
    load_artefact,
    scores_csv_columns,
)

DT = datetime(2026, 3, 1, 12, 30, tzinfo=UTC)

# Plan sections 6.1 and 6.2, transcribed literally.
PLAN_TRAIN_STAGES = ("ingest", "validate", "prepare", "split", "train", "evaluate", "explain", "register")
PLAN_SCORE_STAGES = (
    "ingest",
    "validate_against_schema",
    "prepare",
    "predict",
    "explain_rows",
    "actions",
    "export",
)

# Plan section 6.3, the validation table, transcribed literally.
PLAN_VALIDATION_CODES = frozenset(
    {
        "PK_MISSING",
        "PK_NOT_UNIQUE",
        "PK_NULLS",
        "TARGET_MISSING",
        "TARGET_NOT_BINARY",
        "TARGET_CONSTANT",
        "TARGET_TOO_FEW_POSITIVES",
        "TARGET_IMBALANCE_SEVERE",
        "ROWS_TOO_FEW",
        "LEAKAGE_SUSPECTED",
        "TIME_COLUMN_MISSING",
        "TIME_COLUMN_UNPARSEABLE",
        "HIGH_NULL_COLUMN",
        "CONSTANT_COLUMN",
        "HIGH_CARDINALITY_ID_LIKE",
        "PII_DETECTED",
        "SCHEMA_MISMATCH",
        "CONSENT_COLUMN_MISSING",
    }
)

REASON = {
    "feature": "visits_last_7d",
    "value": "12",
    "contribution": 0.12,
    "direction": "up",
    "text": "visits_last_7d up (12 visits)",
}

MINIMAL: dict[str, dict[str, Any]] = {
    "run.json": {
        "run_id": "run_0001",
        "use_case_id": "demo-use-case",
        "use_case_name": "Demo use case",
        "mode": "train",
        "state": "done",
        "created_at": DT,
        "upload_id": "upload_0001",
        "file_name": "customers.csv",
        "primary_key": "customer_id",
        "problem_type": "binary_classification",
        "model_choice": "__automl__",
        "engine_version": "0.1.0",
    },
    "status.json": {
        "run_id": "run_0001",
        "mode": "train",
        "state": "running",
        "updated_at": DT,
        "stages": [
            {
                "key": "ingest",
                "title": "Reading the file",
                "group_label": "Validating data",
                "state": "done",
            }
        ],
        "progress_pct": 12,
    },
    "profile.json": {
        "upload_id": "upload_0001",
        "file_name": "customers.csv",
        "file_size_bytes": 2048,
        "file_format": "csv",
        "delimiter": ",",
        "encoding": "utf-8",
        "row_count": 1000,
        "column_count": 1,
        "fingerprint": {
            "hash": "9f2c" + "0" * 60,
            "algorithm": "sha256",
            "n_rows": 1000,
            "columns": ["customer_id"],
        },
        "columns": [
            {
                "name": "customer_id",
                "position": 0,
                "dtype": "int64",
                "inferred_type": "integer",
                "null_count": 0,
                "null_rate": 0.0,
                "distinct_count": 1000,
                "is_unique": True,
                "is_constant": False,
                "sample_values": ["1", "2"],
                "looks_like_id": False,
                "looks_like_time": False,
            }
        ],
        "primary_key_candidates": ["customer_id"],
        "time_column_candidates": [],
        "target_candidate": None,
        "preview_rows": [["1"], ["2"]],
        "missing_value_rate_pct": 0.0,
        "profiled_at": DT,
    },
    "validation.json": {
        "upload_id": "upload_0001",
        "mode": "train",
        "checks": [
            {
                "code": "HIGH_NULL_COLUMN",
                "severity": "warning",
                "message": "Column 'last_seen' is empty in most rows.",
                "suggestion": "Leave it out, or fill it upstream.",
                "column": "last_seen",
            }
        ],
        "error_count": 0,
        "warning_count": 1,
        "passed": True,
        "validated_at": DT,
    },
    "prepare.json": {
        "run_id": "run_0001",
        "rows_in": 1000,
        "rows_out": 990,
        "columns_in": 12,
        "columns_out": 10,
        "feature_columns": ["visits_last_7d", "tenure_months"],
        "dropped_columns": [{"name": "email", "reason": "pii", "detail": "email detector matched"}],
        "row_removals": [{"reason": "duplicate", "rows": 10}],
        "transforms": [{"order": 1, "kind": "dedupe", "columns": ["customer_id"]}],
        "detail": "2 features - 2 columns excluded",
        "prepared_at": DT,
    },
    "split.json": {
        "run_id": "run_0001",
        "type": "random_stratified",
        "time_column": None,
        "group_column": None,
        "parts": [
            {"name": "train", "rows": 700, "share": 0.7},
            {"name": "validation", "rows": 150, "share": 0.15},
            {"name": "test", "rows": 150, "share": 0.15},
        ],
        "validation_fraction": 0.15,
        "test_fraction": 0.15,
        "seed": 4242,
        "detail": "700 training rows, 150 validation rows, 150 test rows",
        "split_at": DT,
    },
    "leaderboard.json": {
        "run_id": "run_0001",
        "metric": "roc_auc",
        "metric_label": "ROC-AUC",
        "greater_is_better": True,
        "entries": [
            {
                "rank": 1,
                "model_name": "LightGBM_BAG_L1",
                "family": "LightGBM",
                "family_label": "LightGBM",
                "validation_score": 0.84,
                "test_score": 0.83,
                "fit_time_seconds": 12.5,
                "predict_time_seconds": 0.4,
                "is_ensemble": False,
                "stack_level": 1,
            }
        ],
        "best_model_name": "LightGBM_BAG_L1",
        "models_trained": 1,
        "time_limit_seconds": 1800,
        "presets": "good_quality",
    },
    "best_model.json": {
        "run_id": "run_0001",
        "model_name": "LightGBM_BAG_L1",
        "family": "LightGBM",
        "is_ensemble": False,
        "display_name": "LightGBM",
        "metric": "roc_auc",
        "metric_label": "ROC-AUC",
        "validation_score": 0.84,
        "test_score": 0.83,
        "hyperparameters_summary": "depth 6 - lr 0.05 - 400 trees",
        "training_rows": 700,
        "feature_count": 2,
        "fit_time_seconds": 12.5,
        "predictor_key": "models/demo/model",
        "trained_at": DT,
    },
    "evaluation.json": {
        "run_id": "run_0001",
        "problem_type": "binary_classification",
        "rows_evaluated": 150,
        "positive_rate": 0.12,
        "primary_metric": "roc_auc",
        "primary_metric_label": "ROC-AUC",
        "headline_score": 0.83,
        "metrics": [{"id": "roc_auc", "label": "ROC-AUC", "value": 0.83, "greater_is_better": True}],
        "threshold": 0.43,
        "threshold_mode": "auto",
        "threshold_detail": "Auto (maximises F1 on validation): 0.43",
        "calibration": {"method": "isotonic", "brier_before": 0.14, "brier_after": 0.11},
        "evaluated_at": DT,
    },
    "confusion_matrix.json": {
        "run_id": "run_0001",
        "threshold": 0.43,
        "positive_label": "converted",
        "negative_label": "not converted",
        "true_positive": 12,
        "false_negative": 6,
        "false_positive": 9,
        "true_negative": 123,
        "total": 150,
        "precision": 0.5714,
        "recall": 0.6667,
        "specificity": 0.9318,
        "f1": 0.6154,
    },
    "decile_lift.json": {
        "run_id": "run_0001",
        "mode": "classification",
        "base_rate_pct": 12.0,
        "bins": [
            {
                "decile": i,
                "label": f"D{i}",
                "rows": 15,
                "score_min": round(1.0 - i / 10, 2),
                "score_max": round(1.1 - i / 10, 2),
                "mean_score": round(1.05 - i / 10, 2),
            }
            for i in range(1, 11)
        ],
        "values": tuple(float(11 - i) / 10 for i in range(1, 11)),
        "unit": "x",
        "caption": "Lift vs average. D1 is the highest score.",
        "computed_at": DT,
    },
    "baseline.json": {
        "run_id": "run_0001",
        "baseline_name": "baseline (logistic regression)",
        "baseline_family": "LogisticRegression",
        "rows": [
            {
                "id": "roc_auc",
                "label": "ROC-AUC",
                "model_value": 0.83,
                "baseline_value": 0.74,
                "delta": 0.09,
                "model_better": True,
            }
        ],
        "model_beats_baseline": True,
    },
    "fairness.json": {
        "run_id": "run_0001",
        "column": None,
        "evaluated": False,
        "reason_not_evaluated": "No sensitive column was configured.",
        "groups": [],
        "max_positive_rate_gap": None,
        "max_recall_gap": None,
        "max_precision_gap": None,
    },
    "feature_importance.json": {
        "run_id": "run_0001",
        "method": "permutation",
        "top_n": 1,
        "items": [{"rank": 1, "feature": "visits_last_7d", "importance": 0.21, "share_pct": 100.0}],
        "caption": "Permutation importance on the test split (%)",
    },
    "drift_baseline.json": {
        "run_id": "run_0001",
        "model_version_id": "model_0001",
        "rows": 700,
        "bin_count": 10,
        "features": [
            {
                "feature": "visits_last_7d",
                "kind": "numeric",
                "null_rate": 0.0,
                "bins": [{"lower": 0.0, "upper": 5.0, "count": 700, "share": 1.0}],
                "mean": 2.4,
                "std": 1.1,
            }
        ],
        "created_at": DT,
    },
    "drift.json": {
        "run_id": "run_0002",
        "baseline_run_id": "run_0001",
        "model_version_id": "model_0001",
        "threshold": 0.2,
        "features": [
            {
                "feature": "visits_last_7d",
                "psi": 0.06,
                "status": "stable",
                "null_rate_baseline": 0.0,
                "null_rate_current": 0.01,
            }
        ],
        "max_psi": 0.06,
        "drifted_features": [],
        "status": "stable",
        "summary": "PSI 0.06, stable",
        "computed_at": DT,
    },
    "scoring_summary.json": {
        "run_id": "run_0002",
        "model_version_id": "model_0001",
        "model_display_name": "LightGBM",
        "rows_scored": 1000,
        "score_field": "propensity",
        "score_mean": 0.31,
        "score_median": 0.27,
        "bands": [{"name": "High", "action": "Act now", "min_score": 0.8, "rows": 120, "share_pct": 12.0}],
        "actions": [{"action": "Act now", "rows": 120, "share_pct": 12.0}],
        "suppressed": [{"reason": "opted_out", "rows": 30}],
        "control_group_rows": 100,
        "kpi": {
            "label": "Target audience",
            "formula": 'count_where_band_in(["High"])',
            "value": 120.0,
            "display": "120",
        },
        "drift_status": "stable",
        "drift_max_psi": 0.06,
        "drift_summary": "PSI 0.06, stable",
        "files": {"scores.csv": "runs/run_0002/scores.csv"},
        "sample_rows": [
            {
                "primary_key": "1",
                "score": 0.91,
                "band": "High",
                "action": "Act now",
                "reasons": [REASON],
            }
        ],
        "scored_at": DT,
    },
    "schema.json": {
        "use_case_id": "demo-use-case",
        "model_version_id": "model_0001",
        "primary_key": "customer_id",
        "target": "converted_30d",
        "problem_type": "binary_classification",
        "columns": [{"name": "visits_last_7d", "inferred_type": "integer"}],
        "row_count_at_fit": 700,
        "created_at": DT,
    },
    "row_explanations.parquet": {"primary_key": "1", "score": 0.91, "reasons": [REASON]},
    "scores.csv": {
        "primary_key": "1",
        "score": 0.91,
        "band": "High",
        "action": "Act now",
        "reasons": [REASON],
    },
}
MINIMAL["scores.parquet"] = MINIMAL["scores.csv"]

ALL_MODELS: dict[str, type[BaseModel]] = {
    **ARTEFACT_REGISTRY,
    **TABULAR_SCHEMAS,
    "ModelVersion": ModelVersion,
}


def _contract_models() -> list[type[Artefact]]:
    """Every artefact model defined in `engine.contracts` (so not owner B's `ResolvedConfig`)."""
    return [
        obj
        for obj in vars(contracts).values()
        if inspect.isclass(obj) and issubclass(obj, Artefact) and obj is not Artefact
    ]


def _run_config_instance() -> BaseModel:
    return resolve_config(list_use_case_ids()[0])


@pytest.mark.parametrize("name", sorted(ALL_MODELS))
def test_models_are_frozen_and_forbid_extras(name: str) -> None:
    model = ALL_MODELS[name]
    assert model.model_config.get("frozen") is True, name
    assert model.model_config.get("extra") == "forbid", name


@pytest.mark.parametrize("model", _contract_models(), ids=lambda m: m.__name__)
def test_every_field_has_a_description(model: type[Artefact]) -> None:
    for field_name, field in model.model_fields.items():
        assert field.description, f"{model.__name__}.{field_name} has no description"


@pytest.mark.parametrize("filename", sorted(MINIMAL))
def test_minimal_instance_round_trips(filename: str) -> None:
    instance = load_artefact(filename, dump_artefact(_build(filename)))
    assert dump_artefact(instance) == dump_artefact(_build(filename))


def _build(filename: str) -> BaseModel:
    from engine.contracts import artefact_model

    return artefact_model(filename).model_validate(MINIMAL[filename])


def test_run_config_round_trips() -> None:
    resolved = _run_config_instance()
    payload = dump_artefact(resolved)
    assert dump_artefact(load_artefact("run_config.json", payload)) == payload


def test_dump_artefact_ends_with_newline_and_is_byte_stable() -> None:
    payload = dump_artefact(_build("run.json"))
    assert payload.endswith("\n")
    assert payload == dump_artefact(_build("run.json"))
    assert '"run_id": "run_0001"' in payload


def test_datetimes_serialise_as_iso_with_timezone() -> None:
    payload = dump_artefact(_build("run.json"))
    assert '"created_at": "2026-03-01T12:30:00Z"' in payload


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RunRecord.model_validate({**MINIMAL["run.json"], "created_at": datetime(2026, 3, 1, 12, 30)})


def test_confusion_matrix_cells_are_tp_fn_fp_tn() -> None:
    matrix = ConfusionMatrix.model_validate(MINIMAL["confusion_matrix.json"])
    assert matrix.cells == (
        matrix.true_positive,
        matrix.false_negative,
        matrix.false_positive,
        matrix.true_negative,
    )
    assert matrix.cells == (12, 6, 9, 123)
    with pytest.raises(ValidationError):
        ConfusionMatrix.model_validate({**MINIMAL["confusion_matrix.json"], "cells": (1, 2, 3, 4)})


def test_run_state_is_the_one_state_vocabulary() -> None:
    assert RunRecord.model_fields["state"].annotation is RunState
    assert RunStatus.model_fields["state"].annotation is RunState
    assert StageStatus.model_fields["state"].annotation is RunState
    jobs = pytest.importorskip("engine.jobs", reason="engine/jobs.py (owner D) is not present yet")
    assert jobs.JobInfo.model_fields["state"].annotation is RunState


def test_stage_key_covers_the_plan_stage_sequences() -> None:
    assert {member.value for member in StageKey} == set(PLAN_TRAIN_STAGES) | set(PLAN_SCORE_STAGES)
    pipeline = pytest.importorskip(
        "engine.pipeline", reason="engine/pipeline.py (owner D) is not present yet"
    )
    engine_stages = {str(stage) for stage in (*pipeline.TRAIN_STAGES, *pipeline.SCORE_STAGES)}
    assert {member.value for member in StageKey} == engine_stages


def test_validation_codes_are_the_plan_table_plus_the_suppression_warning() -> None:
    assert PLAN_VALIDATION_CODES | {"SUPPRESSION_COLUMN_MISSING"} == VALIDATION_CODES
    assert len(VALIDATION_CODES) == 19


def test_unknown_validation_code_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown validation code"):
        contracts.ValidationCheck.model_validate({"code": "NOT_A_CODE", "severity": "error", "message": "no"})


def test_scores_csv_columns_follow_the_configured_header_rule() -> None:
    config = load_use_case(list_use_case_ids()[0])
    columns = scores_csv_columns(config, "customer_id")
    reasons = tuple(f"reason_{i}" for i in range(1, config.evaluation.reasons_per_row + 1))
    assert columns == (
        "customer_id",
        config.actions.score_field,
        "band",
        "action",
        *reasons,
        "suppressed_reason",
        "control_group",
    )
