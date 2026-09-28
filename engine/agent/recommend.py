"""Recommended settings (Plan G §7): pure rules over facts the tools measured.

Each rule looks at the use case's current value and one fact about the file, and suggests a change
only when the change follows from that fact. A setting the rules have nothing to say about keeps
the use case's default, and the screen labels it that way; nothing is changed "just because".

Every suggestion is filtered against the advanced-settings schema - the path must be one of its
fields, the value one of its choices or inside its range, never an advisory ("Coming later") field -
and the whole set is resolved with `resolve_config` before it is returned, so a suggestion can never
come back from `POST /runs` as a 422. Some settings are never suggested at all, whatever the data
says: anything that loosens a data limit (`validation.min_positive`, `validation.min_rows`), switches
off a check (`validation.leakage_check`) or removes human approval (`governance.approval_required`).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from engine.agent.contracts import AgentConfidence
from engine.config import (
    ADVISORY_PATHS,
    EXTRA_OVERRIDABLE_PATHS,
    AdvancedSettingsSchema,
    ConfigError,
    FieldSpec,
    Metric,
    SplitType,
    UseCaseConfig,
    resolve_config,
)

__all__ = [
    "NEVER_RECOMMENDED",
    "DataFacts",
    "SettingRecommendation",
    "recommend_settings",
    "setting_allowed",
    "settings_fields",
]

NEVER_RECOMMENDED: Final[frozenset[str]] = frozenset(
    {
        "validation.min_positive",
        "validation.min_rows",
        "validation.leakage_check",
        "governance.approval_required",
        "validation.acknowledged",
    }
)
"""Paths the helper never suggests: each would loosen a safeguard rather than fit the data."""

SMALL_FILE_ROWS: Final[int] = 5_000
LARGE_FILE_ROWS: Final[int] = 500_000
SMALL_FILE_MINUTES: Final[int] = 10
LARGE_FILE_MINUTES: Final[int] = 60
RARE_OUTCOME_RATE: Final[float] = 0.05


@dataclass(frozen=True)
class DataFacts:
    """What the recommender may know about the file, all measured by tools."""

    rows: int
    positive_rate: float | None
    time_columns: tuple[str, ...]
    """Date columns with at least three distinct values, best first."""
    consent_candidates: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class SettingRecommendation:
    path: str
    value: Any
    title: str
    reason: str
    confidence: AgentConfidence
    evidence_ids: tuple[str, ...]


def settings_fields(schema: AdvancedSettingsSchema) -> dict[str, FieldSpec]:
    return {field.path: field for stage in schema.stages for field in stage.fields}


def setting_allowed(path: str, value: Any, fields: dict[str, FieldSpec]) -> bool:
    """Whether the helper may suggest `value` for `path`: a live, non-advisory field, inside its choices or range."""
    if path in NEVER_RECOMMENDED or path in ADVISORY_PATHS:
        return False
    field = fields.get(path)
    if field is None:
        return path in EXTRA_OVERRIDABLE_PATHS
    if field.advisory:
        return False
    if field.choices and value is not None:
        allowed = {json.dumps(choice.value) for choice in field.choices}
        values = value if isinstance(value, (list, tuple)) else [value]  # a multi-select holds a list
        if not values or any(json.dumps(item) not in allowed for item in values):
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if field.min is not None and value < field.min:
            return False
        if field.max is not None and value > field.max:
            return False
    return True


def _split_rules(config: UseCaseConfig, facts: DataFacts) -> list[SettingRecommendation]:
    split = config.split
    if split.type is SplitType.TIME_BASED:
        if split.time_column in facts.time_columns:
            return []
        if facts.time_columns:
            column = facts.time_columns[0]
            return [
                SettingRecommendation(
                    "split.time_column",
                    column,
                    f"Use '{column}' as the date of each row",
                    "This use case tests on the newest data, and this is the file's date column.",
                    AgentConfidence.SURE,
                    facts.evidence_ids,
                )
            ]
        return [
            SettingRecommendation(
                "split.type",
                SplitType.RANDOM_STRATIFIED.value,
                "Split the rows at random",
                "The file has no date column, so the newest rows cannot be held back for testing.",
                AgentConfidence.SURE,
                facts.evidence_ids,
            )
        ]
    if not facts.time_columns:
        return []
    column = facts.time_columns[0]
    return [
        SettingRecommendation(
            "split.type",
            SplitType.TIME_BASED.value,
            "Test on the newest rows",
            f"The file has dates in '{column}', so the model can be tested on its newest rows, as it will be used.",
            AgentConfidence.CHECK,
            facts.evidence_ids,
        ),
        SettingRecommendation(
            "split.time_column",
            column,
            f"Use '{column}' as the date of each row",
            "Needed to split by date.",
            AgentConfidence.CHECK,
            facts.evidence_ids,
        ),
    ]


def _metric_rules(config: UseCaseConfig, facts: DataFacts) -> list[SettingRecommendation]:
    rate = facts.positive_rate
    if rate is None or rate >= RARE_OUTCOME_RATE or config.model_search.metric is not Metric.ROC_AUC:
        return []
    return [
        SettingRecommendation(
            "model_search.metric",
            Metric.PR_AUC.value,
            "Judge models on how well they find the rare 'yes' cases",
            f"Only {rate:.1%} of rows are 'yes'; this measure rewards finding them rather than ranking the many 'no' rows.",
            AgentConfidence.SURE,
            facts.evidence_ids,
        )
    ]


def _time_rules(config: UseCaseConfig, facts: DataFacts) -> list[SettingRecommendation]:
    minutes = config.model_search.time_limit_minutes
    if facts.rows < SMALL_FILE_ROWS and minutes > SMALL_FILE_MINUTES:
        return [
            SettingRecommendation(
                "model_search.time_limit_minutes",
                SMALL_FILE_MINUTES,
                f"Limit the model search to {SMALL_FILE_MINUTES} minutes",
                f"The file has {facts.rows:,} rows; a longer search would not find a better model.",
                AgentConfidence.SURE,
                facts.evidence_ids,
            )
        ]
    if facts.rows > LARGE_FILE_ROWS and minutes < LARGE_FILE_MINUTES:
        return [
            SettingRecommendation(
                "model_search.time_limit_minutes",
                LARGE_FILE_MINUTES,
                f"Give the model search {LARGE_FILE_MINUTES} minutes",
                f"The file has {facts.rows:,} rows; each model takes longer to train.",
                AgentConfidence.SURE,
                facts.evidence_ids,
            )
        ]
    return []


def _consent_rules(config: UseCaseConfig, facts: DataFacts) -> list[SettingRecommendation]:
    if config.governance.consent_column is not None or not facts.consent_candidates:
        return []
    column = facts.consent_candidates[0]
    return [
        SettingRecommendation(
            "governance.consent_column",
            column,
            f"Train only on customers who consented, using '{column}'",
            f"'{column}' looks like a consent flag. Rows without consent would be left out of training.",
            AgentConfidence.CHECK,
            facts.evidence_ids,
        )
    ]


def recommend_settings(
    config: UseCaseConfig,
    facts: DataFacts,
    schema: AdvancedSettingsSchema,
    *,
    config_root: Path | None = None,
) -> tuple[SettingRecommendation, ...]:
    """The changes these facts justify, each allowed by the schema, and resolvable together."""
    fields = settings_fields(schema)
    candidates: Sequence[SettingRecommendation] = [
        *_split_rules(config, facts),
        *_metric_rules(config, facts),
        *_time_rules(config, facts),
        *_consent_rules(config, facts),
    ]
    kept = [rec for rec in candidates if setting_allowed(rec.path, rec.value, fields)]
    if kept:
        try:
            resolve_config(config.id, {rec.path: rec.value for rec in kept}, root=config_root)
        except ConfigError:
            # A rule proposed something the config refuses: a bug in a rule, never the user's
            # problem. Keep only what resolves on its own rather than showing nothing.
            kept = [rec for rec in kept if _resolves(config.id, rec, config_root)]
    return tuple(kept)


def _resolves(use_case_id: str, rec: SettingRecommendation, config_root: Path | None) -> bool:
    try:
        resolve_config(use_case_id, {rec.path: rec.value}, root=config_root)
    except ConfigError:
        return False
    return True
