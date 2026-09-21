"""Response models of the M1 API (design §7.4).

Every model is frozen and forbids extra keys because it inherits `engine.config.StrictBase`, the one
`_Base` definition in the codebase. Engine models appear in a response only where the response *is*
the engine document: the merged `UseCaseConfig` and the `AdvancedSettingsSchema` the UI renders from.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, Field

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
from engine.contracts import (
    Artefact,
    DatasetProfile,
    RunRecord,
    RunState,
    RunStatus,
    ValidationReport,
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


# ---------------------------------------------------------------------------
# M2: uploads and runs (design §4.0)
# ---------------------------------------------------------------------------
class UploadRecord(Artefact):
    """`upload.json` - what `POST /uploads` stored, so a later run needs no request context.

    Design §5.2 gives this record its own module, `engine/uploads.py`, which is not part of this
    change; it lives here, beside the models of the two routers that read it, until that module
    lands. It deliberately stays out of `ARTEFACT_REGISTRY`, which documents *run directory*
    artefacts only (DEC-061).
    """

    upload_id: str = Field(description="Id of the upload this record describes.")
    use_case_id: str = Field(description="Use case the file was uploaded for.")
    mode: RunMode = Field(description="Whether the file was uploaded to train or to score.")
    file_name: str = Field(description="The user's original file name.")
    file_format: Literal["csv", "parquet"] = Field(description="Format the file was read as.")
    file_size_bytes: int = Field(description="Bytes received and stored.")
    delimiter: str | None = Field(description="Delimiter sniffed for CSV; null for Parquet.")
    encoding: str = Field(description="Character encoding the file was decoded with.")
    row_count: int = Field(description="Number of data rows in the file.")
    column_count: int = Field(description="Number of columns in the file.")
    source_key: str = Field(description="Storage key of the bytes as received.")
    profile_key: str = Field(description="Storage key of the stored `profile.json`.")
    fingerprint_key: str = Field(description="Storage key of the stored `fingerprint.json`.")
    fingerprint_hash: str = Field(description="Dataset digest, copied out so a run needs no second read.")
    created_at: AwareDatetime = Field(description="UTC time the upload was stored.")


class UploadResponse(StrictBase):
    """Body of `POST /uploads`: the id to run against and everything the Setup screen renders."""

    upload_id: str
    profile: DatasetProfile


class RunRequest(StrictBase):
    """Body of `POST /runs` (plan §8, verbatim). `extra="forbid"`, so a typo is a loud 422."""

    use_case: str
    mode: RunMode = RunMode.TRAIN
    upload_id: str
    primary_key: str
    target: str | None = None
    model_choice: str | None = None
    model_version_id: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)


class RunCreatedResponse(StrictBase):
    """Body of the `202` from `POST /runs`: the id the Running screen polls."""

    run_id: str


class RunDetailResponse(StrictBase):
    """Body of `GET /runs/{run_id}`: plan §8's "run.json + status.json"."""

    run: RunRecord
    status: RunStatus


class RunListResponse(StrictBase):
    """Body of `GET /runs`: the Previous runs card, newest first."""

    runs: tuple[RunRecord, ...]


class RunCancelResponse(StrictBase):
    """Body of `POST /runs/{run_id}/cancel`; `cancelled` is false when the run had already finished."""

    run_id: str
    cancelled: bool
    state: RunState


class ValidationErrorResponse(StrictBase):
    """The `409` body of `POST /runs`: M1's error envelope plus the whole report (DEC-058)."""

    detail: ErrorBody
    validation: ValidationReport
