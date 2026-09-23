"""The `uplift:` block of a use case (plan B §6), and which of it a Phase 5 agent may change.

This module imports nothing from `engine`: `engine.config.UseCaseConfig` declares a field of type
:class:`UpliftConfig`, so `engine.config` imports it at the top and a cycle would break both. The
base settings repeat `engine.config._Base` for that reason - frozen, unknown keys refused - and a
cross-field failure raises `ValueError`, which the config loader reports as `CONFIG_INVALID` with
the dotted path, exactly as it reports every other malformed leaf.

Every field is read only when `problem_type` is `uplift` (DEC-601).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "UPLIFT_OVERRIDABLE_PATHS",
    "UpliftBaseModel",
    "UpliftConfig",
    "UpliftLearner",
    "UpliftPolicyConfig",
    "UpliftSegmentsConfig",
    "uplift_agent_editable_paths",
]

_SETTINGS: Final[ConfigDict] = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
    use_enum_values=False,
    str_strip_whitespace=True,
    populate_by_name=True,
    protected_namespaces=(),
)


def _editable(flag: bool) -> dict[str, Any]:
    """The Phase 5 flag of plan B §12, carried in the JSON schema of each setting."""
    return {"agent_editable": flag}


class UpliftLearner(StrEnum):
    S_LEARNER = "s_learner"
    T_LEARNER = "t_learner"
    X_LEARNER = "x_learner"


class UpliftBaseModel(StrEnum):
    AUTOGLUON_FAST = "autogluon_fast"
    LIGHTGBM = "lightgbm"


class UpliftSegmentsConfig(BaseModel):
    """Where the four segments are cut. A Phase 5 agent may propose new cuts."""

    model_config = ConfigDict(**_SETTINGS, json_schema_extra=_editable(True))

    persuadable_min_uplift: Annotated[float, Field(ge=-1.0, le=1.0)] = 0.02
    sleeping_dog_max_uplift: Annotated[float, Field(ge=-1.0, le=1.0)] = -0.01
    sure_thing_min_probability: Annotated[float | None, Field(gt=0.0, lt=1.0)] = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.sleeping_dog_max_uplift >= self.persuadable_min_uplift:
            raise ValueError(
                "sleeping_dog_max_uplift must be below persuadable_min_uplift, or one customer "
                "could be both a persuadable and a sleeping dog"
            )
        return self


class UpliftPolicyConfig(BaseModel):
    """The budget the targeting recommendation works within. A Phase 5 agent may propose budgets."""

    model_config = ConfigDict(**_SETTINGS, json_schema_extra=_editable(True))

    budget_contacts: Annotated[int | None, Field(ge=1)] = None
    cost_per_contact: Annotated[float | None, Field(ge=0.0)] = None
    value_per_conversion: Annotated[float | None, Field(ge=0.0)] = None


class UpliftConfig(BaseModel):
    """`uplift:` in a use case. Inert unless `problem_type` is `uplift`.

    The data limits, the randomness check and anything about the treatment assignment are **not**
    agent-editable: an agent that could loosen them could make a targeted campaign look causal.
    """

    model_config = ConfigDict(**_SETTINGS)

    learner: UpliftLearner = Field(default=UpliftLearner.X_LEARNER, json_schema_extra=_editable(False))
    base_model: UpliftBaseModel = Field(
        default=UpliftBaseModel.AUTOGLUON_FAST, json_schema_extra=_editable(False)
    )
    treatment_column: str | None = Field(default=None, json_schema_extra=_editable(False))
    treatment_column_hints: tuple[str, ...] = Field(
        default=("treatment", "treated", "contacted", "is_treated"), json_schema_extra=_editable(False)
    )
    treatment_date_column: str | None = Field(default=None, json_schema_extra=_editable(False))
    campaign_id_column: str | None = Field(default=None, json_schema_extra=_editable(False))
    outcome_window_days: Annotated[int | None, Field(ge=1, le=3650)] = Field(
        default=None, json_schema_extra=_editable(False)
    )
    min_arm_rows: Annotated[int, Field(ge=1)] = Field(default=1000, json_schema_extra=_editable(False))
    min_arm_positives: Annotated[int, Field(ge=1)] = Field(default=50, json_schema_extra=_editable(False))
    randomness_auc_max: Annotated[float, Field(gt=0.5, le=1.0)] = Field(
        default=0.60, json_schema_extra=_editable(False)
    )
    bootstrap_samples: Annotated[int, Field(ge=10, le=5000)] = Field(
        default=200, json_schema_extra=_editable(False)
    )
    test_fraction: Annotated[float, Field(ge=0.1, le=0.5)] = Field(
        default=0.30, json_schema_extra=_editable(False)
    )
    time_limit_minutes: Annotated[int, Field(ge=1, le=240)] = Field(
        default=10, json_schema_extra=_editable(False)
    )
    segments: UpliftSegmentsConfig = UpliftSegmentsConfig()
    policy: UpliftPolicyConfig = UpliftPolicyConfig()

    def reserved_columns(self) -> tuple[str, ...]:
        """The configured columns that describe the experiment and so are never features."""
        return tuple(
            name
            for name in (self.treatment_column, self.treatment_date_column, self.campaign_id_column)
            if name is not None
        )


def uplift_agent_editable_paths(prefix: str = "uplift") -> dict[str, bool]:
    """Every leaf of :class:`UpliftConfig`, dotted, mapped to whether a Phase 5 agent may change it.

    The two nested blocks carry the flag on the block and every leaf inherits it; the rest carry it
    per field. The control-group fraction and the suppression rules live in `actions`, not here,
    and are not agent-editable either (plan B §5, §12).
    """
    paths: dict[str, bool] = {}
    for name, info in UpliftConfig.model_fields.items():
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            extra = annotation.model_config.get("json_schema_extra")
            editable = isinstance(extra, dict) and extra.get("agent_editable") is True
            for leaf in annotation.model_fields:
                paths[f"{prefix}.{name}.{leaf}"] = editable
            continue
        extra = info.json_schema_extra
        paths[f"{prefix}.{name}"] = isinstance(extra, dict) and extra.get("agent_editable") is True
    return paths


UPLIFT_OVERRIDABLE_PATHS: Final[frozenset[str]] = frozenset(uplift_agent_editable_paths())
"""Every uplift leaf may be set per run: the treatment column is chosen at Setup like the target.

`engine.config.EXTRA_OVERRIDABLE_PATHS` includes these; being overridable by a *person* for one run
is a different permission from being editable by an *agent* (see `uplift_agent_editable_paths`).
"""
