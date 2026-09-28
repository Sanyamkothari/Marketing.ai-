"""Run the data checks for a plan without starting a run (Plan G §9, `POST /uploads/{id}/checks`).

`check_plan` calls exactly what `POST /runs` calls - `validate_for_training` for a training file,
`validate_against_schema` for a scoring file - with the same arguments, so a dry run and the real
run cannot disagree about a file. The helper's `check_data` tool and the dry-run endpoint both use
it; neither writes anything.
"""

from __future__ import annotations

import pandas as pd

from engine.config import PrimaryKey, RunMode, UseCaseConfig
from engine.contracts import FeatureSchema, ValidationReport
from engine.stages import validate

__all__ = ["check_plan"]


def check_plan(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    mode: RunMode,
    primary_key: PrimaryKey | None,
    target: str | None,
    upload_id: str,
    row_count: int,
    schema: FeatureSchema | None = None,
) -> ValidationReport:
    """The validation report `POST /runs` would produce for this file, config and roles.

    `config` is the resolved config, overrides applied; its `validation.acknowledged` is honoured
    exactly as the run honours it. A scoring check needs the model version's `schema`.
    """
    if mode is RunMode.TRAIN:
        return validate.validate_for_training(
            frame,
            config,
            primary_key=primary_key,
            target=target or "",
            acknowledged=config.validation.acknowledged,
            upload_id=upload_id,
            row_count=row_count,
        )
    if schema is None:
        raise ValueError("a scoring check needs the model version's schema")
    return validate.validate_against_schema(
        frame,
        schema,
        primary_key=primary_key,
        config=config,
        acknowledged=config.validation.acknowledged,
        upload_id=upload_id,
        row_count=row_count,
    )
