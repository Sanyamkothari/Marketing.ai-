"""Recommended settings (Plan G §7): pure rules over facts the tools measured.

Each rule looks at the use case's current value and one fact about the file, and suggests a change
only when the change follows from that fact. A setting the rules have nothing to say about keeps
the use case's default, and the screen labels it that way; nothing is changed "just because".

Every suggestion is filtered against the advanced-settings schema - the path must be one of its
fields, the value one of its choices or inside its range, never an advisory ("Coming later") field -
and the whole set is resolved with `resolve_config` before it is returned, so a suggestion can never
come back from `POST /runs` as a 422. Suggestions that only work together - a split by date and the
column it splits by - are one group: the pair is resolved as a pair (a date column the use case
cannot split by is never offered), and a group is kept or dropped whole. Some settings are never suggested at all, whatever the data
says: anything that loosens a data limit (`validation.min_positive`, `validation.min_rows`), switches
off a check (`validation.leakage_check`) or removes human approval (`governance.approval_required`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from engine.agent.contracts import AgentConfidence
from engine.agent.untrusted import display_name
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

if TYPE_CHECKING:
    from engine.decide.reasons import BusinessReasonDictionary

__all__ = [
    "NEVER_RECOMMENDED",
    "DataFacts",
    "ReasonPhraseSuggestion",
    "SettingRecommendation",
    "recommend_settings",
    "setting_allowed",
    "settings_fields",
    "suggest_reason_phrases",
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
    columns: tuple[str, ...] = ()
    """Every column of the file, so a date column that is there but unusable is told from a missing one."""
    time_column_trouble: str | None = None
    """Why the use case's own date column, though in the file, is not in `time_columns`: `unreadable`
    (the Run button's check cannot read it as dates), `few` (it reads as dates but has fewer than
    three), `leak` (a question asks whether to hide it), `reserved` (it has another job) or
    `unusable` (it does not read as a date column)."""
    time_column_evidence_ids: tuple[str, ...] = ()
    """The tool results that measured `time_column_trouble`, when they are not in `evidence_ids`."""


@dataclass(frozen=True)
class SettingRecommendation:
    path: str
    value: Any
    title: str
    reason: str
    confidence: AgentConfidence
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReasonPhraseSuggestion:
    """A business-language phrase suggestion for a feature (Plan J M98, DEC-1308).

    Both phrases follow `configs/decide/reasons.yaml`: `{value}` is the customer's own value, and the
    phrase says which way it moved the score, never which way the value itself went.
    """

    feature: str
    up_phrase: str
    down_phrase: str
    confidence: AgentConfidence = AgentConfidence.CHECK


def _phrase_words(name: str) -> str:
    """A column name as plain words for a phrase: no underscores, and nothing `reasons.yaml` refuses.

    A word with a digit in it (`7d`, `90d`) or a brace is left out, because a phrase may hold no number
    of its own and the file is refused when it loads.
    """
    parts = re.split(r"[_\s]+", name.replace("{", " ").replace("}", " "))
    return " ".join(part for part in parts if part and not any(char.isdigit() for char in part)).lower()


def suggest_reason_phrases(
    features: Sequence[str],
    *,
    dictionary: BusinessReasonDictionary | None = None,
    config_root: Path | None = None,
) -> tuple[ReasonPhraseSuggestion, ...]:
    """Propose plain-language phrases for features `reasons.yaml` does not cover, as check suggestions.

    The phrase names the feature and the customer's `{value}` and says which way it pushed the score. It
    cannot say the value was "higher" or "lower": a reason's direction is the effect on the score
    (`engine.contracts.Reason`), not the movement of the customer's value.
    """
    from engine.decide.reasons import VALUE_PLACEHOLDER, load_reasons_dictionary

    dict_obj = dictionary or load_reasons_dictionary(root=config_root)
    suggestions: list[ReasonPhraseSuggestion] = []
    for feat in features:
        if feat in dict_obj.features:
            continue
        words = _phrase_words(display_name(feat))
        if not words:
            continue  # nothing of the name is left once digits and braces are taken out: a person writes this one
        subject = f"{words[:1].upper()}{words[1:]} of {VALUE_PLACEHOLDER}"
        suggestions.append(
            ReasonPhraseSuggestion(
                feature=feat,
                up_phrase=f"{subject} pushes the score up",
                down_phrase=f"{subject} pushes the score down",
                confidence=AgentConfidence.CHECK,
            )
        )
    return tuple(suggestions)


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


Resolves = Callable[[Mapping[str, Any]], bool]
"""Whether a set of overrides resolves for the use case (`resolve_config` raises no `ConfigError`)."""


def _split_rules(config: UseCaseConfig, facts: DataFacts, resolves: Resolves) -> list[SettingRecommendation]:
    split = config.split
    if split.type is SplitType.TIME_BASED:
        if split.time_column in facts.time_columns:
            return []
        # Only a column the use case can split by: `resolve_config` checks it against the template.
        usable = [c for c in facts.time_columns if resolves({"split.time_column": c})]
        if usable:
            column = usable[0]
            return [
                SettingRecommendation(
                    "split.time_column",
                    column,
                    f"Use '{display_name(column)}' as the date of each row",
                    "This use case tests on the newest data, and this is the file's date column.",
                    AgentConfidence.SURE,
                    facts.evidence_ids,
                )
            ]
        return _random_split(split.time_column, facts)
    time_based = SplitType.TIME_BASED.value
    usable = [c for c in facts.time_columns if resolves({"split.type": time_based, "split.time_column": c})]
    if not usable:
        return []
    column = usable[0]
    return [
        SettingRecommendation(
            "split.type",
            time_based,
            "Test on the newest rows",
            f"The file has dates in '{display_name(column)}', so the model can be tested on its newest rows, as it will be used.",
            AgentConfidence.CHECK,
            facts.evidence_ids,
        ),
        SettingRecommendation(
            "split.time_column",
            column,
            f"Use '{display_name(column)}' as the date of each row",
            "Needed to split by date.",
            AgentConfidence.CHECK,
            facts.evidence_ids,
        ),
    ]


_TROUBLE_REASONS: Final[dict[str, str]] = {
    "unreadable": "'{name}', the date this use case splits by, cannot be read as dates in this file",
    "few": "'{name}', the date this use case splits by, has fewer than three different dates in this file",
    "leak": "'{name}', the date this use case splits by, may give the answer away (see the question about it)",
    "reserved": "'{name}', the date this use case splits by, is the column that says who was contacted recently",
    "unusable": "'{name}', the date this use case splits by, is not read as a date column in this file",
}


def _random_split(configured: str | None, facts: DataFacts) -> list[SettingRecommendation]:
    """A random split for a use case that splits by date when the file gives it no usable date.

    The reason says what is actually wrong with the use case's date column: missing, unreadable,
    too few dates, possibly a leak or reserved. Only an unreadable one is also cleared, because the
    Run button checks a configured date column's values whatever the split type.
    """
    name = display_name(configured) if configured else ""
    trouble = facts.time_column_trouble if configured is not None and configured in facts.columns else None
    evidence = (*facts.evidence_ids, *facts.time_column_evidence_ids) if trouble else facts.evidence_ids
    if trouble is not None:
        reason = _TROUBLE_REASONS.get(trouble, _TROUBLE_REASONS["unusable"]).format(name=name)
    elif configured is not None:
        reason = f"The file has no '{name}' column, which this use case splits by"
        if facts.time_columns:
            reason += ", and its other dates cannot stand in for it"
    elif facts.time_columns:
        reason = "This use case cannot split by the dates in this file"
    else:
        reason = "The file has no date column"
    recs = [
        SettingRecommendation(
            "split.type",
            SplitType.RANDOM_STRATIFIED.value,
            "Split the rows at random",
            f"{reason}, so the newest rows cannot be held back for testing.",
            AgentConfidence.SURE,
            evidence,
        )
    ]
    if trouble == "unreadable":
        recs.append(
            SettingRecommendation(
                "split.time_column",
                None,
                f"Stop reading '{name}' as the date of each row",
                "The rows are split at random instead, and its values cannot be read as dates.",
                AgentConfidence.SURE,
                evidence,
            )
        )
    return recs


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
            f"Train only on customers who consented, using '{display_name(column)}'",
            f"'{display_name(column)}' looks like a consent flag. Rows without consent would be left out of training.",
            AgentConfidence.CHECK,
            facts.evidence_ids,
        )
    ]


VALUE_LIKE_CANDIDATES: Final[frozenset[str]] = frozenset(
    {"order_value", "ordervalue", "order_val", "premium", "balance", "arpu"}
)


def _value_rules(config: UseCaseConfig, facts: DataFacts) -> list[SettingRecommendation]:
    if config.uplift.policy.value_column is not None:
        return []
    for col in facts.columns:
        norm = col.lower().strip().replace("-", "_").replace(" ", "_")
        if norm in VALUE_LIKE_CANDIDATES:
            return [
                SettingRecommendation(
                    "uplift.policy.value_column",
                    col,
                    f"Use '{display_name(col)}' as customer value for targeting",
                    f"'{display_name(col)}' looks like customer value (order value, premium, balance, ARPU) that the targeting policy can rank by.",
                    AgentConfidence.CHECK,
                    facts.evidence_ids,
                )
            ]
    return []


def recommend_settings(
    config: UseCaseConfig,
    facts: DataFacts,
    schema: AdvancedSettingsSchema,
    *,
    config_root: Path | None = None,
) -> tuple[SettingRecommendation, ...]:
    """The changes these facts justify, each allowed by the schema, and resolvable together."""
    fields = settings_fields(schema)

    def resolves(overrides: Mapping[str, Any]) -> bool:
        try:
            resolve_config(config.id, dict(overrides), root=config_root)
        except ConfigError:
            return False
        return True

    groups: Sequence[Sequence[SettingRecommendation]] = [
        _split_rules(config, facts, resolves),  # one group: a split and its column go together
        *([rec] for rec in _metric_rules(config, facts)),
        *([rec] for rec in _time_rules(config, facts)),
        *([rec] for rec in _consent_rules(config, facts)),
        *([rec] for rec in _value_rules(config, facts)),
    ]
    allowed = [
        list(group)
        for group in groups
        if group and all(setting_allowed(rec.path, rec.value, fields) for rec in group)
    ]
    kept = [rec for group in allowed for rec in group]
    if kept and not resolves({rec.path: rec.value for rec in kept}):
        # A rule proposed something the config refuses: a bug in a rule, never the user's problem.
        # Keep, in order, each whole group that still resolves with the groups kept before it.
        kept = []
        for group in allowed:
            if resolves({rec.path: rec.value for rec in (*kept, *group)}):
                kept.extend(group)
    return tuple(kept)
