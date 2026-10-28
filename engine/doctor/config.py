"""The `data_doctor:` block of a use case (Plan O): the settings of the one Data Doctor agent, which is always on.

Plan O (`docs/plans/MARKETING_AI_PLAN_O_DATA_DOCTOR.md`) builds one agent, Data Doctor, whose tools are every data
capability the product has. There is no on/off switch: it is always on. Defaults live in `configs/engine.yaml` under
`defaults.data_doctor`; a use-case file overrides only what differs.

This module imports nothing from `engine`, for the reason `engine/agent/config.py` gives: `engine.config.UseCaseConfig`
declares a field of type :class:`DataDoctorConfig`, so `engine.config` imports this module at the top and a cycle
would break both. The settings repeat `engine.config._Base` (frozen, unknown keys refused), and a malformed value
raises `ValueError`, which the config loader reports as `CONFIG_INVALID` with the dotted path.

The block is **not overridable per run**: none of its paths is in `overridable_paths()`, so a run override that
names one is refused with `OVERRIDE_UNKNOWN_PATH`. It is outside `Recipe.recipe_hash`, and `UseCaseConfig` leaves
it out of a dump while it holds only defaults, so a run's `run_config.json` is unchanged while nothing is set
(DEC-1800 (c)).
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "AiLimitsConfig",
    "AsksLimits",
    "DataDoctorConfig",
    "DataDoctorLimits",
    "GateLimits",
    "TimeSettingsConfig",
    "TrialLimits",
    "TurnLimits",
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


class TimeSettingsConfig(BaseModel):
    """`data_doctor.time_settings` (Plan O M244, DEC-1814): how the training budget's three choices are made.

    The defaults are left out of a dump, so nothing moves while they hold."""

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
        default=2_000_000, ge=1, description="Tokens (read plus written) the AI may use on one case."
    )
    max_calls_per_case: int = Field(
        default=200, ge=1, description="Calls to the AI service the Doctor may make on one case."
    )
    max_chat_steps: int = Field(
        default=8,
        ge=1,
        le=30,
        description="Actions (looks, changes, questions) the Doctor's chat may take to answer one message.",
    )


class TurnLimits(BaseModel):
    """`data_doctor.limits.turn`: how long one turn of the Doctor's AI may run and how much of the case it reads."""

    model_config = _SETTINGS

    review_steps: int = Field(
        default=12,
        ge=1,
        le=30,
        description="Actions (looks, changes, questions) the review may take in one visit.",
    )
    strikes: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Bad replies (malformed, unknown action, refused argument) before the AI stops.",
    )
    chat_reply_chars: int = Field(
        default=2000, ge=200, le=8000, description="The longest reply the Doctor's chat accepts."
    )
    guided_reply_chars: int = Field(
        default=800, ge=200, le=8000, description="The longest reply Guided setup's helper accepts."
    )
    summary_chars: int = Field(
        default=800, ge=100, le=4000, description="The longest summary the review ends with."
    )
    effect_chars: int = Field(
        default=160,
        ge=40,
        le=1000,
        description="The longest sentence saying what one choice of a question does.",
    )
    question_chars: int = Field(
        default=400, ge=100, le=2000, description="The longest question or doubt text."
    )
    description_chars: int = Field(
        default=300, ge=50, le=2000, description="The longest description of a query."
    )
    history_turns: int = Field(default=12, ge=1, le=100, description="Earlier chat messages the model reads.")
    state_items: int = Field(
        default=40, ge=1, le=500, description="The latest changes of the case the model reads."
    )
    state_questions: int = Field(
        default=12, ge=1, le=200, description="Questions of the case the model reads."
    )
    state_doubts: int = Field(default=5, ge=1, le=100, description="Doubts of the case the model reads.")
    made_columns: int = Field(
        default=20, ge=1, le=200, description="Columns of a query's table the model is shown."
    )
    reloaded_looks: int = Field(
        default=6, ge=1, le=50, description="Looks of earlier turns the model may cite again."
    )
    short_chars: int = Field(
        default=220, ge=40, le=2000, description="The longest piece of text the case state shows."
    )
    prompt_chars: int = Field(
        default=60_000, ge=5_000, le=400_000, description="The longest prompt that is sent."
    )


class AsksLimits(BaseModel):
    """`data_doctor.limits.asks`: how much the AI may ask of the person."""

    model_config = _SETTINGS

    blocking: int = Field(
        default=3, ge=0, le=20, description="Blocking questions the AI may ask in one review."
    )
    quick: int = Field(
        default=2, ge=0, le=10, description="Quick questions (with its own options) in one message."
    )
    quick_options_min: int = Field(
        default=2, ge=2, le=6, description="The fewest options of a quick question."
    )
    quick_options_max: int = Field(default=4, ge=2, le=6, description="The most options of a quick question.")
    doubts: int = Field(default=5, ge=0, le=50, description="Doubts the AI may state in one review.")
    requests: int = Field(
        default=3, ge=0, le=20, description="Requests for more data the AI may make in one review."
    )
    request_columns: int = Field(default=8, ge=1, le=50, description="Columns one request for data may name.")


class TrialLimits(BaseModel):
    """`data_doctor.limits.trial`: how many rows a trial reads."""

    model_config = _SETTINGS

    sample_rows: int = Field(
        default=1_000, ge=1, le=1_000_000, description="Rows of the Sample a plan is checked on."
    )
    dry_run_rows: int = Field(
        default=100_000,
        ge=1,
        le=10_000_000,
        description="Rows of each file a query is tried on before it runs.",
    )


class GateLimits(BaseModel):
    """`data_doctor.limits.gates`: where a plan needs the person's yes. These gates may only be tightened: no value
    is looser than the one the Doctor always had. They are engine-wide (`configs/engine.yaml`); a use case's own
    `gates` and `trial` are not read (see `engine.doctor.limits.limits`)."""

    model_config = _SETTINGS

    retries: int = Field(
        default=2, ge=0, le=10, description="Tries of a step whose checks fail before a person is asked."
    )
    loss_stop_pct: float = Field(
        default=5.0,
        ge=0.0,
        le=5.0,
        description="A plan that loses a larger share of customers (%) needs a yes.",
    )
    rate_stop_points: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="A plan that moves the outcome rate by more points needs a yes.",
    )
    approve_drop_share: float = Field(
        default=0.02,
        ge=0.0,
        le=0.02,
        description="Rows a join, a stack or a row removal drops (share) before it needs a yes.",
    )
    change_warn_pct: float = Field(
        default=30.0,
        ge=0.0,
        le=100.0,
        description="A value fix changing a larger share (%) of a column is shown as a warning.",
    )
    change_refuse_pct: float = Field(
        default=60.0,
        ge=0.0,
        le=60.0,
        description="A value fix changing a larger share (%) of a column is refused.",
    )

    @model_validator(mode="after")
    def _ordered(self) -> GateLimits:
        if self.change_warn_pct > self.change_refuse_pct:
            raise ValueError("change_warn_pct must not be above change_refuse_pct")
        return self


class DataDoctorLimits(BaseModel):
    """`data_doctor.limits`: every number the Doctor's AI or a gate obeys that a prompt may state. The hard floors
    (minimum rows and positives, leakage, consent) are not here and cannot be loosened by any of these."""

    model_config = _SETTINGS

    turn: TurnLimits = Field(
        default_factory=TurnLimits, description="Length of a turn and what the model reads."
    )
    asks: AsksLimits = Field(default_factory=AsksLimits, description="What the AI may ask of the person.")
    trial: TrialLimits = Field(default_factory=TrialLimits, description="Rows a trial reads.")
    gates: GateLimits = Field(default_factory=GateLimits, description="Where a plan needs a yes.")

    @model_validator(mode="after")
    def _options(self) -> DataDoctorLimits:
        if self.asks.quick_options_min > self.asks.quick_options_max:
            raise ValueError("asks.quick_options_min must not be above asks.quick_options_max")
        return self


class DataDoctorConfig(BaseModel):
    """`data_doctor:` in a use case: Plan O's one Data Doctor agent (always on): its settings and the workspace currency (M242)."""

    model_config = _SETTINGS

    workspace_currency: str = Field(
        default="INR",
        pattern=r"^[A-Z]{3}$",
        exclude_if=lambda value: value == "INR",
        description=(
            "Plan O M242 (DEC-1812): the currency this workspace's reports use, as a code (INR, USD, EUR). A client's "
            "confirmed locale profile overrides it for that client. INR, the "
            "default, is left out of a dump so nothing moves."
        ),
    )
    time_settings: TimeSettingsConfig = Field(
        default_factory=TimeSettingsConfig,
        exclude_if=lambda value: value == TimeSettingsConfig(),
        description=(
            "Plan O M244 (DEC-1814): the training budget's diminishing-returns step and the ratios of its three "
            "choices. The defaults are left out of a dump so nothing moves."
        ),
    )
    limits: DataDoctorLimits = Field(
        default_factory=DataDoctorLimits,
        exclude_if=lambda value: value == DataDoctorLimits(),
        description=(
            "Every number the Doctor's AI or a gate obeys that a prompt may state (reply lengths, steps, strikes, "
            "questions, trial rows, where a plan needs a yes). Read through `engine.doctor.limits`; the defaults are "
            "left out of a dump so nothing moves."
        ),
    )
    ai_limits: AiLimitsConfig = Field(
        default_factory=AiLimitsConfig,
        exclude_if=lambda value: value == AiLimitsConfig(),
        description=(
            "The most tokens and calls the Doctor's AI may use on one case. "
            "the defaults are left out of a dump so nothing moves."
        ),
    )
