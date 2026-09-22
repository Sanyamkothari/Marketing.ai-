"""Response models of the M1 API (plan §8).

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
    PrimaryKey,
    ProblemType,
    RunMode,
    StrictBase,
    UseCaseConfig,
    UseCaseStatus,
)
from engine.contracts import (
    Artefact,
    DatasetProfile,
    ModelVersion,
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
# M2: uploads and runs
# ---------------------------------------------------------------------------
class UploadRecord(Artefact):
    """`upload.json` - what `POST /uploads` stored, so a later run needs no request context.

    This record belongs in `engine/uploads.py`, a module that is not part of this change; it lives
    here, beside the models of the two routers that read it, until that module lands. It
    deliberately stays out of `ARTEFACT_REGISTRY`, which documents *run directory* artefacts only
    (DEC-061).
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
    """Body of `POST /runs` (plan §8, verbatim). `extra="forbid"`, so a typo is a loud 422.

    `primary_key` accepts several column names as well as one, and `dataset_id` / `client_id` name
    an onboarded dataset instead of a raw upload. Both are the shape Phase 2 needs; neither has
    behaviour behind it yet, and `POST /runs` says so plainly rather than accepting a request it
    would then half-honour.
    """

    use_case: str
    mode: RunMode = RunMode.TRAIN
    upload_id: str
    primary_key: PrimaryKey
    dataset_id: str | None = None
    client_id: str | None = None
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


# ---------------------------------------------------------------------------
# M3: the model registry (plan §8)
# ---------------------------------------------------------------------------
class ModelVersionResponse(StrictBase):
    """One registry row with the champion flag plan §8 asks the listing to carry.

    `is_champion` is not a second source of truth: it restates `version.status is champion`, which the
    registry keeps to at most one live row per use case by demoting the incumbent inside the same
    transaction that crowns the successor. It is spelled out so the Model page never has to know the
    status vocabulary to draw a badge.
    """

    version: ModelVersion
    is_champion: bool


class ModelListResponse(StrictBase):
    """Body of `GET /models`: the versions the registry holds, newest first, the champion flagged."""

    versions: tuple[ModelVersionResponse, ...]


class ModelApproveRequest(StrictBase):
    """Body of `POST /models/{model_id}/approve`: who signed off on a version that was waiting.

    Phase 1 has no authentication (plan §1.3), so `approved_by` is whatever the caller typed. It is
    **not** verified and must not be read as proof that that person approved anything; it is a label
    the registry stores so the row is not anonymous. The alternative - the API inventing a name - would
    put a fabricated identity in the audit trail, which is worse than an unverified one.
    """

    approved_by: str = Field(
        min_length=1,
        description="Caller-supplied name of the approver. Unverified: Phase 1 has no authentication.",
    )


class ModelPromoteRequest(StrictBase):
    """Body of `POST /models/{model_id}/promote`: the manual override, with who and why.

    `reason` is required, and deliberately so: `promote` bypasses the champion rule, and an override
    nobody can account for later is worse than no override at all. `promoted_by` carries the same
    caveat as `ModelApproveRequest.approved_by` - caller-supplied, unverified, stored so the row is not
    anonymous.
    """

    promoted_by: str = Field(
        min_length=1,
        description="Caller-supplied name of the person overriding. Unverified: Phase 1 has no authentication.",
    )
    reason: str = Field(
        min_length=1,
        description="Why this version is being made champion by hand; stored as the version's promotion note.",
    )


# ===========================================================================
# Shared file (PARALLEL_WORK_PROTOCOL.md §4): three branches edit it at once.
# Add code only inside your own block, at its end. Never edit above your
# block, never reorder, never reformat the rest of the file - run `black` on
# what you paste, not on the file, if the formatter would reflow other lines.
# `tests/unit/test_shared_file_markers.py` fails if a block goes missing.
# ===========================================================================

# ---- PHASE-2 (onboarding) — append only below this line ----
# ---- END PHASE-2 ----

# ---- PHASE-3A (generative) — append only below this line ----
from engine.config import LlmBackend  # noqa: E402
from engine.generative.contracts import (  # noqa: E402
    DocIndexManifest,
    GenerativeStatus,
    GuardrailReport,
    LlmUsageReport,
    RagEval,
)


class AssistantAskRequest(StrictBase):
    """Body of `POST /indexes/{index_id}/ask`: one question for the Try-it panel.

    The whole shape is one field because everything else the answer needs - which index, which
    prompt version, which guardrails apply - is already fixed by the index and its resolved
    config; the only thing a caller supplies is the question itself.
    """

    question: str = Field(description="The question to ask the index, exactly as the caller typed it.")


class RootCauseRequest(StrictBase):
    """Body of `POST /runs/{run_id}/root-cause`: dotted overrides into `generative.root_cause.*`.

    `overrides` uses the same dotted-path mechanism as `RunRequest.overrides` rather than a typed
    field per setting, because the RCA screen's "Generate root causes" button sends `{}` today and a
    future settings panel can widen this dict without the request ever changing shape.
    """

    overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Dotted-path overrides into generative.root_cause.*, e.g. {'max_segments': 4}.",
    )


class CampaignCopyRequest(StrictBase):
    """Body of `POST /runs/{run_id}/campaign-copy`: dotted overrides into `generative.campaign_copy.*`.

    Mirrors `RootCauseRequest` in shape and in reason: both jobs run over a finished scoring run,
    and both screens' Advanced settings are a hand-picked subset the UI reads back through this one
    dict rather than through a generated form.
    """

    overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Dotted-path overrides into generative.campaign_copy.*, e.g. {'variants_per_band': 3}.",
    )


class CopyTemplateApproveRequest(StrictBase):
    """Body of `POST .../campaign-copy/templates/{template_id}/approve`.

    Mirrors `ModelApproveRequest` in shape and in caveat: Phase 1 has no authentication, so
    `approved_by` is whatever the caller typed, stored so the row is not anonymous and never read
    as a verified identity - the same unverified-claim pattern DEC-055 already established, reused
    here rather than reinvented.
    """

    approved_by: str = Field(
        min_length=1,
        description="Caller-supplied name of the approver. Unverified: Phase 1 has no authentication.",
    )


class ReferenceSetResponse(StrictBase):
    """Body of `POST /use-cases/{use_case_id}/reference-sets`.

    Profiles an uploaded reference-question file the way `UploadResponse` profiles a dataset, so the
    Setup screen's "Primary key" and "Reference answer column" selects have real column names to
    offer instead of guessing at `question_id` / `reference_answer`.
    """

    reference_set_id: str = Field(description="Opaque id, passed back to the index build/evaluate routes.")
    columns: tuple[str, ...] = Field(description="Column names of the uploaded file, in file order.")
    row_count: int = Field(description="Data rows in the uploaded file.")


class IndexJobStartedResponse(StrictBase):
    """Body of the `202` from `POST .../indexes` and `POST /indexes/{index_id}/evaluate`.

    One shape for both starts, because an evaluate job does not mint a new index - it re-grades the
    one already named in the path, and the id a caller polls back is the same either way.
    `RunCreatedResponse` is the predictive analogue; this is its generative counterpart, keyed by
    `index_id` rather than `run_id`.
    """

    index_id: str = Field(description="Id of the index build or evaluate job; poll GET /indexes/{id}.")


class GenerativeLlmSummary(StrictBase):
    """The backend badge every generative screen shows, read off the config a job actually ran with.

    Not `engine.config.LlmConfig` restated in full: a screen needs to know which backend and which
    models answered, not the temperature or the retry policy it was called with. Plan section 13.3
    requires a fake backend be obvious on the screen that renders its output, not just discoverable
    in a log, and this is that visibility, carried directly on every response that names an index.
    """

    backend: LlmBackend = Field(description="Which client answered: the deterministic fake, or Bedrock.")
    generation_model_id: str = Field(description="Model id that generated answers or summaries.")
    judge_model_id: str = Field(description="Model id that judged them.")
    embedding_model_id: str = Field(description="Model id that embedded the index's chunks.")
    region: str = Field(description="Region the backend was called in.")


class IndexSummary(StrictBase):
    """One row of `GET /use-cases/{use_case_id}/indexes`: the Setup screen's Previous-runs card.

    `champion` is server-decided the same way `ModelVersionResponse.is_champion` is: the
    best-scoring build by `mean_faithfulness`, falling back to the newest build when nothing has
    been graded, so the Setup screen never has to rank indexes itself.
    """

    index_id: str = Field(description="Id of the index this row describes.")
    kind: Literal["build", "evaluate"] = Field(description="Which job produced this row.")
    champion: bool = Field(description="Whether this is the use case's current best index.")
    state: RunState = Field(description="State of the job that produced this row.")
    llm: GenerativeLlmSummary = Field(description="Backend and models the job actually ran with.")
    faithfulness: float | None = Field(
        default=None,
        description="Mean faithfulness over the index's last grading, 0 to 1; null when never graded.",
    )
    source_label: str = Field(
        description="Documents summary for a build row, or the reference file's name for an evaluate row."
    )
    created_at: AwareDatetime = Field(description="UTC time the job that produced this row finished.")


class IndexListResponse(StrictBase):
    """Body of `GET /use-cases/{use_case_id}/indexes`: every index this use case has built, newest first."""

    indexes: tuple[IndexSummary, ...] = Field(description="Index build and evaluate rows, newest first.")


class IndexDetailResponse(StrictBase):
    """Body of `GET /indexes/{index_id}`: one index in full, polled by the Setup and Results screens.

    `manifest`, `rag_eval`, `llm_usage` and `guardrails` are null exactly when the underlying file
    has not been written yet - still building, or built with no reference set for `rag_eval` - the
    same "artefact this job has not produced" meaning `GET /runs/{run_id}/artefacts/{name}` already
    carries for a run (DEC-210 makes this its own parallel artefact set, not a reuse of that file).
    """

    index_id: str = Field(description="Id of the index this response describes.")
    llm: GenerativeLlmSummary = Field(description="Backend and models the index actually ran with.")
    status: GenerativeStatus = Field(description="`index_status.json` verbatim: the job in progress.")
    manifest: DocIndexManifest | None = Field(
        default=None, description="`doc_index_manifest.json`; null until the build has produced one."
    )
    rag_eval: RagEval | None = Field(
        default=None, description="`rag_eval.json`; null when the index has never been graded."
    )
    llm_usage: LlmUsageReport | None = Field(
        default=None, description="`llm_usage.json`; null before the job has made a billable call."
    )
    guardrails: GuardrailReport | None = Field(
        default=None, description="`guardrail_report.json`; null before any check has run."
    )


class GenerativeJobStartedResponse(StrictBase):
    """Body of the `202` from `POST /runs/{run_id}/root-cause` and `POST /runs/{run_id}/campaign-copy`.

    One shape for both, because neither job has a poll route of its own: progress and the finished
    artefact are both read back through the run's own artefact route (DEC-210), so the only thing a
    caller needs from the start response is the job id to match against `*_status.json`.
    """

    run_id: str = Field(description="Run the job is running over, echoed back for convenience.")
    job_id: str = Field(description="Id of the root-cause or campaign-copy job that was started.")


# ---- END PHASE-3A ----

# ---- PHASE-4A (aws) — append only below this line ----
# ---- END PHASE-4A ----
