"""Response models of the M1 API (design §7.4).

Every model is frozen and forbids extra keys because it inherits `engine.config.StrictBase`, the one
`_Base` definition in the codebase. Engine models appear in a response only where the response *is*
the engine document: the merged `UseCaseConfig` and the `AdvancedSettingsSchema` the UI renders from.
"""

from __future__ import annotations

from typing import Literal

from engine.config import (
    AdvancedSettingsSchema,
    AiType,
    OutputConfig,
    PageTitles,
    ProblemType,
    RunMode,
    StrictBase,
    UseCaseConfig,
    UseCaseStatus,
)


class ApiChoice(StrictBase):
    """One option of a Setup-screen select; `label` never comes from a config value."""

    value: str
    label: str
    enabled: bool = True
    help: str | None = None


class LegendEntry(StrictBase):
    """One row of the overview legend, from `catalog.ai_types`."""

    ai_type: AiType
    marker: str
    stars: str
    label: str


class UseCaseSummary(StrictBase):
    """A use-case card on the overview. Planned entries carry only what the industry file states."""

    id: str
    name: str
    description: str
    status: UseCaseStatus
    ai_type: AiType
    marker: str
    stars: str
    problem_type: ProblemType | None
    entity: str | None
    target_column: str | None
    trainable_in_phase_1: bool


class StageResponse(StrictBase):
    """One lifecycle stage of an industry, in file order."""

    id: str
    order: int
    name: str
    ai_type: AiType
    marker: str
    stars: str
    type_label: str
    use_cases: tuple[UseCaseSummary, ...]


class IndustryResponse(StrictBase):
    """One industry journey: its legend and its ordered stages."""

    id: str
    name: str
    journey_label: str
    legend: tuple[LegendEntry, ...]
    stages: tuple[StageResponse, ...]


class IndustriesResponse(StrictBase):
    """Body of `GET /industries`."""

    industries: tuple[IndustryResponse, ...]


class ModeOption(StrictBase):
    """One Setup mode (train / score) with all of its copy, `{entity}` already substituted."""

    value: RunMode
    label: str
    help: str
    dataset_hint: str
    columns_hint: str
    run_button: str


class SetupBlock(StrictBase):
    """Everything the Setup screen needs that is not an advanced setting."""

    modes: tuple[ModeOption, ...]
    target_label: str
    problem_type_choices: tuple[ApiChoice, ...]
    model_choices: tuple[ApiChoice, ...]
    template_url: str
    template_readme_url: str


class TargetBlock(StrictBase):
    """The target column and the Data-page copy that describes it."""

    column: str | None
    positive_label: str | int | bool | None
    definition: str
    label_source: str
    label: str


class LimitsBlock(StrictBase):
    """The upload limits the Setup screen states before a file is chosen."""

    min_rows: int
    min_positive: int
    max_file_size_mb: int


class RunningRows(StrictBase):
    """The Running-screen rows of each mode, derived from `engine.pipeline` (DEC-020)."""

    train: tuple[str, ...]
    score: tuple[str, ...]


class UseCaseResponse(StrictBase):
    """Body of `GET /use-cases/{use_case_id}`: merged config, advanced settings and Setup copy."""

    id: str
    name: str
    description: str
    lifecycle_stage: str
    ai_type: AiType
    marker: str
    stars: str
    problem_type: ProblemType
    problem_type_label: str
    entity: str
    trainable_in_phase_1: bool
    target: TargetBlock
    primary_key_hints: tuple[str, ...]
    time_column_hints: tuple[str, ...]
    time_like_pattern: str
    limits: LimitsBlock
    setup: SetupBlock
    pages: PageTitles
    running_rows: RunningRows
    output: OutputConfig
    config: UseCaseConfig
    advanced_settings: AdvancedSettingsSchema


class ErrorBody(StrictBase):
    """The machine-readable part of every error response."""

    code: str
    message: str
    path: str | None = None


class ErrorResponse(StrictBase):
    """The body of every error response: one `detail` object, never a bare string."""

    detail: ErrorBody


class HealthResponse(StrictBase):
    """Body of `GET /healthz` (DEC-024)."""

    status: Literal["ok"]
    version: str
