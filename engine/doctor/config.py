"""The `data_doctor:` block of a use case (Plan O): the switch that turns the one Data Doctor agent on.

Plan O (`docs/plans/MARKETING_AI_PLAN_O_DATA_DOCTOR.md`) builds one agent, Data Doctor, whose tools are every data
capability the product has. It is built behind one switch, `data_doctor.unified`, which stays off until the plan's
parity checklist (§3.2) is met at M249 (decision O14). While it is off, nothing the product does changes: every
existing test and golden digest stays byte-identical. Defaults live in `configs/engine.yaml` under
`defaults.data_doctor`; a use-case file overrides only what differs.

This module imports nothing from `engine`, for the reason `engine/agent/config.py` gives: `engine.config.UseCaseConfig`
declares a field of type :class:`DataDoctorConfig`, so `engine.config` imports this module at the top and a cycle
would break both. The settings repeat `engine.config._Base` (frozen, unknown keys refused), and a malformed value
raises `ValueError`, which the config loader reports as `CONFIG_INVALID` with the dotted path.

The block is **not overridable per run**: none of its paths is in `overridable_paths()`, so a run override that
names one is refused with `OVERRIDE_UNKNOWN_PATH`. It is outside `Recipe.recipe_hash`, and `UseCaseConfig` leaves
it out of a dump while it holds only defaults, so a run's `run_config.json` is unchanged while the switch is off
(DEC-1800 (c)).
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field, StrictBool

__all__ = ["AiLimitsConfig", "DataDoctorConfig", "TimeSettingsConfig"]

_SETTINGS: Final[ConfigDict] = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
    use_enum_values=False,
    str_strip_whitespace=True,
    populate_by_name=True,
    protected_namespaces=(),
)


class TimeSettingsConfig(BaseModel):
    """`data_doctor.time_settings` (Plan O M244, DEC-1814): how the training budget's three choices are made.

    Read only while `unified` is on. The defaults are left out of a dump, so nothing moves while they hold."""

    model_config = _SETTINGS

    score_step_per_minute: float = Field(
        default=0.0005,
        gt=0.0,
        le=0.1,
        description=(
            "The diminishing-returns step: a minute more is worth it only while similar past runs gained at "
            "least this much validation score per extra minute. Balanced is the first budget after which they "
            "gained less."
        ),
    )
    quick_ratio: float = Field(
        default=0.34, gt=0.0, lt=1.0, description="Quick's time limit as a share of Balanced's."
    )
    thorough_ratio: float = Field(
        default=2.5, gt=1.0, le=10.0, description="Thorough's time limit as a multiple of Balanced's."
    )
    sure_min_runs: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Balanced is pre-selected (`sure`) only when at least this many past runs were close in size.",
    )


class AiLimitsConfig(BaseModel):
    """`data_doctor.ai_limits`: what the Doctor's AI may spend on one case. When either is used up the Doctor
    says so and carries on with its rules alone."""

    model_config = _SETTINGS

    max_tokens_per_case: int = Field(
        default=600_000, ge=1, description="Tokens (read plus written) the AI may use on one case."
    )
    max_calls_per_case: int = Field(
        default=60, ge=1, description="Calls to the AI service the Doctor may make on one case."
    )


class DataDoctorConfig(BaseModel):
    """`data_doctor:` in a use case: Plan O's one Data Doctor agent: the switch (M230) and the workspace currency (M242)."""

    model_config = _SETTINGS

    unified: StrictBool = Field(
        default=False,
        description=(
            "Plan O (DEC-1800): when true, Guided setup and the Doctor page are the one Data Doctor agent. Off "
            "until the parity checklist is met (M249); while off, nothing changes."
        ),
    )
    workspace_currency: str = Field(
        default="INR",
        pattern=r"^[A-Z]{3}$",
        exclude_if=lambda value: value == "INR",
        description=(
            "Plan O M242 (DEC-1812): the currency this workspace's reports use, as a code (INR, USD, EUR). A client's "
            "confirmed locale profile overrides it for that client. Read only while `unified` is on; INR, the "
            "default, is left out of a dump so nothing moves."
        ),
    )
    time_settings: TimeSettingsConfig = Field(
        default_factory=TimeSettingsConfig,
        exclude_if=lambda value: value == TimeSettingsConfig(),
        description=(
            "Plan O M244 (DEC-1814): the training budget's diminishing-returns step and the ratios of its three "
            "choices. Read only while `unified` is on; the defaults are left out of a dump so nothing moves."
        ),
    )
    ai_limits: AiLimitsConfig = Field(
        default_factory=AiLimitsConfig,
        exclude_if=lambda value: value == AiLimitsConfig(),
        description=(
            "The most tokens and calls the Doctor's AI may use on one case. Read only while `unified` is on; "
            "the defaults are left out of a dump so nothing moves."
        ),
    )
