"""The `uplift:` block of a use case (plan B §6), and which of it a Phase 5 agent may change.

This module imports nothing from `engine`: `engine.config.UseCaseConfig` declares a field of type
:class:`UpliftConfig`, so `engine.config` imports it at the top and a cycle would break both. The
base settings repeat `engine.config._Base` for that reason - frozen, unknown keys refused - and a
cross-field failure raises `ValueError`, which the config loader reports as `CONFIG_INVALID` with
the dotted path, exactly as it reports every other malformed leaf.

Every field is read only when `problem_type` is `uplift` (DEC-601).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "LEARNER_SHORT_FORMS",
    "UPLIFT_OVERRIDABLE_PATHS",
    "UPLIFT_RUN_LOCKED_PATHS",
    "UpliftBaseModel",
    "UpliftConfig",
    "UpliftEvidenceConfig",
    "UpliftLearner",
    "UpliftPolicyConfig",
    "UpliftSegmentsConfig",
    "level_text",
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


LEARNER_SHORT_FORMS: Final[dict[str, UpliftLearner]] = {
    "s": UpliftLearner.S_LEARNER,
    "t": UpliftLearner.T_LEARNER,
    "x": UpliftLearner.X_LEARNER,
}
"""Plan B §6 spells the learner `s | t | x`; the stored value is always the full name (DEC-671)."""


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
    # Plan J M97 (DEC-1307): rank by each customer's expected net money (engine.uplift.policy).
    value_column: Annotated[str | None, Field(min_length=1)] = None
    horizon_months: Annotated[int | None, Field(ge=1, le=120)] = None
    margin_pct: Annotated[float | None, Field(ge=0.0, le=100.0)] = None
    min_roi: Annotated[float | None, Field(ge=0.0)] = None
    # Plan J M99 (DEC-1309): the catalogue action an uplift run's treat rows are sent
    # (`configs/decide/catalogue.yaml`). Left out of the serialised config while unset, so a use
    # case without one dumps exactly as before M99.
    treat_action_id: Annotated[str | None, Field(min_length=1)] = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # Plan J M100 part B (DEC-1310 (q) on): with several offers, the catalogue action each offer is sent
    # as ({offer level: action id}; its label, channels and costs), and the most the offers chosen for
    # one scoring run may cost in total, in rupees. Both are left out of the serialised config while
    # unset, so a use case without them dumps exactly as before.
    arm_action_ids: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Several offers (Plan J M100): the catalogue action each offer level is sent as, "
            "{level: action_id}. An offer not named here is priced from the value settings."
        ),
        exclude_if=lambda value: not value,
    )
    total_budget: Annotated[float | None, Field(ge=0.0)] = Field(
        default=None,
        description=(
            "Several offers (Plan J M100): the most the offers chosen in one scoring run may cost in "
            "total, contact and offer costs together, in rupees. Unset: no total budget."
        ),
        exclude_if=lambda value: value is None,
    )

    @field_validator("arm_action_ids", mode="before")
    @classmethod
    def _arm_levels_as_text(cls, value: Any) -> Any:
        """Levels as `level_text` spells them, so YAML `{1: x}` and `{"1.0": x}` name one offer."""
        if isinstance(value, dict):
            return {level_text(key): str(action).strip() for key, action in value.items()}
        return value


class UpliftEvidenceConfig(BaseModel):
    """`uplift.evidence` (Plan J M96): the costly evidence an Approver may ask for. All off by default.

    Each switch refits the meta-learner `folds` times on the training rows, LightGBM base model only
    (`engine.measurement.compare`). `fold_auuc` reports each fold's AUUC of a model fitted on all the
    other folds; `risk_comparison` writes `risk_comparison.json`, uplift top-`top_share` against risk
    top-`top_share` at equal budget, from models fitted on the next `(folds - 1) // 2` folds round a
    ring (so its interval is honest) together with a plain LightGBM risk model. `propensity_column`
    names a column of recorded per-row treatment probabilities (M92's `treatment_probability`); it is
    never a feature, rows outside (0, 1) are left out of the comparison, and without it the treated
    share is the propensity, as for a randomised file. Not agent-editable: they decide what evidence
    an approval rests on.
    """

    model_config = ConfigDict(**_SETTINGS, json_schema_extra=_editable(False))

    fold_auuc: bool = False
    risk_comparison: bool = False
    folds: Annotated[int, Field(ge=3, le=10)] = 5
    top_share: Annotated[float, Field(gt=0.0, le=1.0)] = 0.2
    propensity_column: str | None = None


_WHOLE_NUMBER_TEXT: Final = re.compile(r"[+-]?\d+\.0*")
"""Text of a whole number written with a decimal point (`1.0`, `2.`, `-3.00`): the same level as `1`."""


def level_text(value: object) -> str:
    """One comparable spelling of a treatment level (Plan J M100): `1`, `1.0`, `"1.0"` and `" 1 "` are one.

    The same normalisation the outcome labels use (`engine.uplift.data._label_key`): booleans as
    `true`/`false`, whole floats without their `.0`, text stripped and lower-cased. Text that writes a
    whole number with a decimal point (`"1.0"`, a CSV cell of a float column) loses its `.0` too, so a
    level reads the same from a number, a float cell or a text cell.
    """
    if hasattr(value, "item") and not isinstance(value, str | bytes):  # numpy scalars
        value = value.item()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    text = str(value).strip().lower()
    if _WHOLE_NUMBER_TEXT.fullmatch(text):
        return str(int(float(text)))
    return text


def _number_as_text(item: object) -> object:
    """A YAML number as level text (`1.0` -> `"1"`); anything else unchanged for the validators."""
    if isinstance(item, bool) or not isinstance(item, int | float):
        return item
    if isinstance(item, float) and item.is_integer():
        return str(int(item))
    return str(item)


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
    drift_treated_share_tolerance: Annotated[float, Field(ge=0.0, le=0.5)] = Field(
        default=0.05, json_schema_extra=_editable(False)
    )
    """How far a scoring file's treated share may move from training's before the uplift drift check
    says so: `|current - training| <= tolerance` (M53, DEC-857). An absolute difference, not a
    significance test, because on a large file a z-test flags differences too small to matter."""
    segments: UpliftSegmentsConfig = UpliftSegmentsConfig()
    policy: UpliftPolicyConfig = UpliftPolicyConfig()
    evidence: UpliftEvidenceConfig = UpliftEvidenceConfig()  # Plan J M96; off by default
    # Plan J M100 (DEC-668 (1), DEC-1310): several offers against one shared control. Empty (the default)
    # is one treatment level: the treatment column holds 0/1 and every rule is today's. Otherwise the
    # control value first, then at least two treatment values; the first of them is the treatment every
    # existing field describes. Left out of the configuration's dump when empty, so a run configured
    # without it records exactly what it recorded before.
    treatment_levels: tuple[str, ...] = Field(
        default=(),
        description=(
            "Several offers against one shared control (Plan J M100): the control value first, then each "
            "offer's value in the treatment column. Empty: one treatment, recorded as 0/1."
        ),
        json_schema_extra=_editable(False),
        exclude_if=lambda levels: not levels,
    )

    @field_validator("treatment_levels", mode="before")
    @classmethod
    def _levels_as_text(cls, value: Any) -> Any:
        """A YAML list of numbers (`[0, 1, 2]` or `[0.0, 1.0, 2.0]`) names levels as text, as the file's
        cells are compared: a whole float without its `.0`, the spelling `level_text` gives a cell."""
        if isinstance(value, list | tuple):
            return tuple(_number_as_text(item) for item in value)
        return value

    @field_validator("treatment_levels")
    @classmethod
    def _levels_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            return value
        if len(value) < 3:
            raise ValueError(
                "treatment_levels lists the control value first and then at least two treatment values; "
                "leave it empty for one treatment recorded as 0/1"
            )
        if any(not level for level in value):
            raise ValueError("treatment_levels may not contain an empty value")
        keys = [level_text(level) for level in value]
        if len(set(keys)) != len(keys):
            raise ValueError("treatment_levels names the same value twice")
        return value

    @model_validator(mode="after")
    def _arm_actions_name_offers(self) -> Self:
        """Plan J M100 part B: `policy.arm_action_ids` names offers, never the control.

        Checked against `treatment_levels` when they are set here; a scoring run whose levels come from
        the model checks the same against the model's levels and says which it ignored."""
        levels = [level_text(level) for level in self.treatment_levels]
        for key in self.policy.arm_action_ids:
            if levels and key == levels[0]:
                raise ValueError(
                    f"policy.arm_action_ids names {key!r}, the control level: the control gets no offer"
                )
            if levels and key not in levels[1:]:
                raise ValueError(
                    f"policy.arm_action_ids names {key!r}, which is not one of the offers in "
                    f"treatment_levels ({', '.join(levels[1:])})"
                )
        return self

    @property
    def multi_arm(self) -> bool:
        """True when several treatments share one control (Plan J M100); False is today's binary run."""
        return bool(self.treatment_levels)

    @field_validator("learner", mode="before")
    @classmethod
    def _short_learner(cls, value: Any) -> Any:
        """Accept plan B's `s`, `t` and `x` (any case) as the three learners' full names (DEC-671)."""
        if isinstance(value, str) and not isinstance(value, UpliftLearner):
            return LEARNER_SHORT_FORMS.get(value.strip().lower(), value)
        return value

    def reserved_columns(self) -> tuple[str, ...]:
        """The configured columns that describe the experiment and so are never features."""
        return tuple(
            name
            for name in (
                self.treatment_column,
                self.treatment_date_column,
                self.campaign_id_column,
                self.evidence.propensity_column,  # Plan J M96: a recorded P(treated) is never a feature
            )
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


UPLIFT_RUN_LOCKED_PATHS: Final[frozenset[str]] = frozenset({"uplift.randomness_auc_max"})
"""Uplift leaves a run may NOT override: they decide whether a result may be called causal (DEC-607).

`randomness_auc_max` is the TREATMENT_NOT_RANDOM threshold. Were it overridable per run, a request
could raise it to 1.0 and a targeted campaign would pass the check and be reported `causal: true` -
the one outcome plan B §4 forbids ("non-random assignment -> refuse to call it causal"). The honest
way past the check is to acknowledge it (`validation.acknowledged: ["TREATMENT_NOT_RANDOM"]`), which
runs the model and marks every artefact not causal. The threshold stays settable in the use-case
file, where changing it is a reviewed configuration change rather than a request field.

The arm-size floors (`min_arm_rows`, `min_arm_positives`) stay overridable: a small arm widens every
bootstrap interval, so the champion rule (AUUC lower bound above zero) already refuses what a small
sample cannot show, and nothing about a small arm can make non-random data look random.
"""

UPLIFT_OVERRIDABLE_PATHS: Final[frozenset[str]] = (
    frozenset(uplift_agent_editable_paths()) - UPLIFT_RUN_LOCKED_PATHS
)
"""Every uplift leaf but the locked ones may be set per run: the treatment column is chosen at Setup
like the target.

`engine.config.EXTRA_OVERRIDABLE_PATHS` includes these; being overridable by a *person* for one run
is a different permission from being editable by an *agent* (see `uplift_agent_editable_paths`).
"""
