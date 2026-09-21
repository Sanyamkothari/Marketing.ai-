"""Validation stages: the training checks (M2) and the scoring schema check (M4).

Every check of plan §6.3 returns a `ValidationReport`; nothing here ever raises on bad data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engine.config import UseCaseConfig
    from engine.contracts import DatasetProfile, FeatureSchema, ValidationReport


def validate_for_training(
    profile: DatasetProfile,
    config: UseCaseConfig,
    *,
    primary_key: str,
    target: str,
    acknowledged: Sequence[str],
) -> ValidationReport:
    """Run every train-mode check of plan §6.3 against the profile of an upload."""
    raise NotImplementedError("M2")


def validate_against_schema(
    profile: DatasetProfile,
    schema: FeatureSchema,
    *,
    primary_key: str,
) -> ValidationReport:
    """Check a scoring file against the schema the champion model was fitted with."""
    raise NotImplementedError("M4")
