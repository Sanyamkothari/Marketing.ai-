"""`check_plan` (Plan G M71) is the run's validation, for scoring files too."""

from __future__ import annotations

import pytest

from engine.agent.checks import check_plan
from engine.config import ColumnType, ProblemType, RunMode, resolve_config
from engine.contracts import FeatureSchema, FeatureSchemaColumn
from engine.stages import validate
from engine.utils.time import utc_now
from tests.unit.agent.helpers import synthetic


def _schema(columns: dict[str, ColumnType]) -> FeatureSchema:
    return FeatureSchema(
        use_case_id="targeted-advertisement",
        model_version_id="m1",
        primary_key="customer_id",
        target="converted_30d",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        columns=tuple(FeatureSchemaColumn(name=n, inferred_type=t) for n, t in columns.items()),
        row_count_at_fit=1_000,
        created_at=utc_now(),
    )


def test_a_scoring_check_is_validate_against_schema() -> None:
    frame = synthetic("scoring", rows=400).drop(columns=["tenure_months"])
    schema = _schema({"visits_last_7d": ColumnType.INTEGER, "tenure_months": ColumnType.INTEGER})
    config = resolve_config("targeted-advertisement", {}).config
    ours = check_plan(
        frame,
        config,
        mode=RunMode.SCORE,
        primary_key="customer_id",
        target=None,
        upload_id="u",
        row_count=400,
        schema=schema,
    )
    theirs = validate.validate_against_schema(
        frame, schema, primary_key="customer_id", config=config, upload_id="u", row_count=400
    )
    assert [(c.code, c.severity) for c in ours.checks] == [(c.code, c.severity) for c in theirs.checks]
    assert any(c.code == "SCHEMA_MISMATCH" for c in ours.checks)


def test_a_scoring_check_needs_the_schema() -> None:
    config = resolve_config("targeted-advertisement", {}).config
    with pytest.raises(ValueError):
        check_plan(
            synthetic("scoring", rows=50),
            config,
            mode=RunMode.SCORE,
            primary_key="customer_id",
            target=None,
            upload_id="u",
            row_count=50,
        )
