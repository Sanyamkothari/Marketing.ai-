"""Response models of the M1 API (plan §8).

Every model is frozen and forbids extra keys because it inherits `engine.config.StrictBase`, the one
`_Base` definition in the codebase. Engine models appear in a response only where the response *is*
the engine document: the merged `UseCaseConfig` and the `AdvancedSettingsSchema` the UI renders from.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

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
    """Body of `GET /industries`: every industry file, the one the overview opens on listed first."""

    default_industry: str | None = Field(
        description=(
            "Id of the industry the overview opens on: telecom when that file exists, else the first "
            "listed; null only when the configuration has no industry file."
        )
    )
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

    A run reads EITHER an `upload_id` - a file the user prepared themselves, exactly as in Phase 1 -
    OR a `dataset_id`, a dataset the onboarding pipeline built from their raw tables. Exactly one of
    the two, because a request naming both would have two answers to "which data produced this
    score" and the manifest can only record one.

    `primary_key` accepts several column names as well as one. It is required with an upload, where
    only the user knows what a row is, and optional with a dataset, whose manifest already says
    (plan section 6.5, change 4; DEC-107).
    """

    use_case: str
    mode: RunMode = RunMode.TRAIN
    upload_id: str | None = None
    primary_key: PrimaryKey | None = None
    dataset_id: str | None = None
    client_id: str | None = None
    target: str | None = None
    model_choice: str | None = None
    model_version_id: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_source_of_data(self) -> RunRequest:
        if (self.upload_id is None) == (self.dataset_id is None):
            given = "both" if self.upload_id is not None else "neither"
            raise ValueError(
                "A run reads one uploaded file or one built dataset; give exactly one of upload_id "
                f"and dataset_id ({given} given)."
            )
        if self.upload_id is not None and self.primary_key is None:
            raise ValueError(
                "An uploaded file needs primary_key: only you know which column identifies a row."
            )
        if self.client_id is not None and self.dataset_id is None:
            # A field that is quietly ignored is how a caller comes to believe they scoped a run to
            # a client when they scoped it to nothing. It means something only beside a dataset.
            raise ValueError("client_id names the owner of a dataset, so it needs a dataset_id.")
        return self


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
        description=(
            "Caller-supplied name of the approver. With sign-in on it is replaced by the signed-in "
            "username (Plan D, DEC-862); with sign-in off it is unverified."
        ),
    )
    # Plan D M54 (DEC-862): an added, optional field; the Approvals screen always sends one.
    reason: str | None = Field(
        default=None, max_length=2000, description="Why the version is approved; kept in the decision record."
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


from engine.aws_connection import AwsConnection, LockReason  # noqa: E402


class AwsConnectionState(StrictBase):
    """Body of `GET`, `PUT` and `DELETE /connection/aws`: the identity in force, and who may change it.

    `profiles` is empty for any caller who may not choose one. A list of the operator's profile names
    is nobody else's business, and showing a menu that the next request would refuse is worse than
    showing none.
    """

    editable: bool = Field(description="True when this caller may choose the identity.")
    locked_reason: LockReason | None = Field(
        default=None, description="`deployed` or `remote_client` when `editable` is false."
    )
    env: str = Field(description="The deployment name: `local`, `dev`, `staging` or `prod`.")
    connection: AwsConnection = Field(description="The identity choice in force. Never a credential.")
    profiles: tuple[str, ...] = Field(
        default=(), description="AWS CLI profile names on the server's machine. Empty unless editable."
    )
    aws_profile_env: str | None = Field(
        default=None,
        description="The AWS_PROFILE the server was started with, which the default chain uses. Unless editable, null.",
    )
    explanation: str = Field(description="One paragraph saying where the credentials come from.")


class ConnectionTestRequest(StrictBase):
    """Body of `POST /connection/aws/test`. Every field is optional; an empty body tests what is in force.

    `connection` lets a person try a profile before saving it, so it is honoured only for a caller
    who could have saved it. There is deliberately no field for a key: this product never takes one.
    """

    connection: AwsConnection | None = Field(
        default=None, description="A candidate identity to test instead of the saved one. Local only."
    )
    region: str | None = Field(
        default=None,
        description="Region to test against. Defaults to the use case's `generative.llm.region`.",
    )
    use_case_id: str = Field(
        default="ai-onboarding-assistant", description="Use case whose configured models are checked."
    )


# ---- END PHASE-3A ----

# ---- PHASE-4A (aws) — append only below this line ----
# ---- END PHASE-4A ----

# ---- PHASE-4B (production) — append only below this line ----
# --- M46/M47 (access control and audit): api/routes/auth.py and api/routes/audit.py -------------
from pydantic import ConfigDict  # noqa: E402

from engine.access.roles import Role  # noqa: E402
from engine.audit.events import AuditEvent  # noqa: E402
from engine.audit.export import AuditExportResult  # noqa: E402

AuditExportResponse = AuditExportResult
"""`POST /audit/exports` answers with the engine's own record of what was written."""


class _SecretBase(StrictBase):
    """A request that carries a password: whitespace is part of a password, so it is never stripped."""

    model_config = ConfigDict(str_strip_whitespace=False)


class PrincipalView(StrictBase):
    """Who the caller is, as the UI shows it. Never a credential."""

    user_id: str = Field(description="Stable id; what the audit log records.")
    username: str = Field(description="What the person signs in with.")
    display_name: str | None = Field(default=None, description="How the person is shown, when known.")
    roles: tuple[Role, ...] = Field(description="Roles held, Viewer implied, sorted.")
    kind: str = Field(description="`user`, `local_operator` (sign-in is off) or `system`.")


class PermissionView(StrictBase):
    """Whether the caller may use one route, and the sentence to show when not."""

    method: str = Field(description="HTTP method.")
    path: str = Field(description="Route template, e.g. `/models/{model_id}/approve`.")
    action: str = Field(description="Audit action name, e.g. `models.approve`.")
    role: Role | None = Field(description="Role required; null for a public route.")
    allowed: bool = Field(description="Whether the caller holds that role.")
    reason: str | None = Field(
        default=None, description='Why not, when not: "Only an Approver can approve a champion."'
    )


class MeResponse(StrictBase):
    """`GET /auth/me`: who the caller is and what they may do, so the UI can hide and explain actions."""

    principal: PrincipalView
    auth_mode: str = Field(description="`off` (everyone is the local operator) or `local`.")
    permissions: tuple[PermissionView, ...] = Field(description="One entry per declared route, sorted.")


class LoginRequest(_SecretBase):
    """Body of `POST /auth/login`."""

    username: str = Field(min_length=1, max_length=64, description="Case-insensitive.")
    password: str = Field(min_length=1, max_length=1024, description="Never logged, never audited.")


class LoginResponse(StrictBase):
    """A new sign-in. `token` is shown once; send it as `Authorization: Bearer <token>`."""

    token: str = Field(description="Bearer token. Stored by the server only as its SHA-256.")
    token_type: Literal["bearer"] = "bearer"
    expires_at: AwareDatetime = Field(description="When the sign-in lapses, UTC.")
    principal: PrincipalView


class UserView(StrictBase):
    """A user as an Admin sees one. There is no password field."""

    user_id: str
    username: str
    display_name: str
    roles: tuple[Role, ...] = Field(description="Roles granted, sorted.")
    disabled: bool
    created_at: AwareDatetime
    created_by: str
    updated_at: AwareDatetime


class UserListResponse(StrictBase):
    """`GET /users`."""

    users: tuple[UserView, ...]


class UserCreateRequest(_SecretBase):
    """Body of `POST /users`."""

    username: str = Field(min_length=3, max_length=64, description="Unique, case-insensitive.")
    password: str = Field(min_length=1, max_length=1024, description="At least 12 characters.")
    display_name: str | None = Field(default=None, max_length=120)
    roles: tuple[Role, ...] = Field(min_length=1, description="At least one role.")


class UserUpdateRequest(StrictBase):
    """Body of `PATCH /users/{user_id}`. Omitted fields are left as they are."""

    display_name: str | None = Field(default=None, max_length=120)
    roles: tuple[Role, ...] | None = Field(
        default=None, description="The complete new set; revokes the user's sign-ins."
    )
    disabled: bool | None = Field(default=None, description="True signs the user out everywhere.")


class PasswordChangeRequest(_SecretBase):
    """Body of `POST /users/{user_id}/password`."""

    password: str = Field(min_length=1, max_length=1024, description="The new password, 12+ characters.")
    current_password: str | None = Field(
        default=None,
        max_length=1024,
        description="Required when changing your own password; an Admin resetting someone else's omits it.",
    )


class AuditEventPage(StrictBase):
    """`GET /audit/events`: one page of events, newest first, and the total that match."""

    events: tuple[AuditEvent, ...]
    total: int = Field(description="Events matching the filters, across all pages.")
    limit: int
    offset: int


class AuditExportRequest(StrictBase):
    """Body of `POST /audit/exports`: the window to export. Empty exports everything up to now."""

    since: AwareDatetime | None = Field(default=None, description="Inclusive lower bound.")
    until: AwareDatetime | None = Field(default=None, description="Exclusive upper bound; defaults to now.")
    action: str | None = Field(
        default=None, max_length=120, description="Exact action or a prefix ending in `.`."
    )


# --- end M46/M47 ---------------------------------------------------------------------------------
# --- M48 (DPDP controls): api/routes/privacy.py --------------------------------------------------
# A data principal's id arrives only in a request *body* (never a path or a query string, which the
# access log and every proxy record), is hashed on arrival, and appears in no response below: every
# model here names a person by `principal_hash` or not at all (DEC-746).
from engine.privacy.contracts import (  # noqa: E402
    ConsentImportReport,
    ConsentRecord,
    ConsentReport,
    ConsentStatus,
    ErasureOutcome,
    ErasureRequestRecord,
    RetentionPlan,
    RetentionResult,
    RetrainFlag,
    StoreProgress,
)

ConsentRecordResponse = ConsentRecord
"""`POST /privacy/consent` answers with the ledger row as stored: the principal as a hash."""

ConsentImportResponse = ConsentImportReport
"""`POST /privacy/consent/imports`: rows read, rows written, and every problem by row and column."""

ErasureResponse = ErasureOutcome
"""What an erasure did, where, and which models were flagged for retraining (the job's outcome)."""


class ErasureAccepted(StrictBase):
    """`POST /privacy/erasure` (and `/retry`): the request is queued; follow its progress by id (DEC-863)."""

    request_id: str = Field(description="The erasure request's id; the audit events' object id.")
    status: str = Field(description="`queued`: a background job will carry it out.")
    principal_hash: str = Field(description="Salted hash of the principal's id; never the id.")
    client_id: str | None = Field(default=None, description="Client the request was made for.")
    progress_url: str = Field(description="Where to follow it: `GET /privacy/erasure/{id}/progress`.")


class ErasureProgressResponse(StrictBase):
    """`GET /privacy/erasure/{id}/progress`: status and per-store progress, and nothing about the person."""

    request_id: str
    status: str = Field(
        description="`queued`, `in_progress`, `completed`, `completed_with_exceptions` or `failed`."
    )
    error_code: str | None = None
    progress: tuple[StoreProgress, ...]
    completed_at: AwareDatetime | None = None


ConsentReportResponse = ConsentReport
"""`GET /privacy/runs/{run_id}/consent-report`: how the consent ledger gated one scoring run."""

_PRINCIPAL_ID_DESCRIPTION = (
    "The data principal's id exactly as the client's files spell it (e.g. a customer id). Hashed on "
    "arrival; never stored, logged, audited or echoed back."
)
_CLIENT_ID_DESCRIPTION = "Client whose data this is; defaults to the deployment's `client_id`."


class PurposeView(StrictBase):
    """One purpose a data principal may consent to (`configs/privacy.yaml`)."""

    purpose_id: str = Field(description="Id used in consent records, e.g. `marketing_communication`.")
    label: str = Field(description="How the purpose is named to a person.")
    description: str = Field(description="One sentence on what processing it covers.")


class PrivacyPolicyResponse(StrictBase):
    """`GET /privacy/purposes`: the purposes, which use case is gated by which, and the erasure policy."""

    purposes: tuple[PurposeView, ...] = Field(description="Every declared purpose, by id.")
    use_case_purposes: dict[str, str] = Field(description="Use-case id -> purpose id; unlisted = ungated.")
    erasure_mode: str = Field(description="`delete` or `tombstone`.")
    consent_history: str = Field(
        description="`keep` or `delete`: what erasure does to the hashed ledger rows."
    )


class ConsentRecordRequest(StrictBase):
    """Body of `POST /privacy/consent`: one consent given or withdrawn."""

    principal_id: str = Field(min_length=1, max_length=256, description=_PRINCIPAL_ID_DESCRIPTION)
    purpose: str = Field(min_length=1, max_length=64, description="Purpose id from `GET /privacy/purposes`.")
    status: ConsentStatus = Field(description="`granted` or `withdrawn`.")
    source: str = Field(
        default="api", min_length=1, max_length=64, description="Where the consent was captured."
    )
    recorded_at: AwareDatetime | None = Field(
        default=None, description="When the person gave or withdrew consent; defaults to now."
    )
    expires_at: AwareDatetime | None = Field(default=None, description="When a grant lapses; null = never.")
    client_id: str | None = Field(default=None, max_length=128, description=_CLIENT_ID_DESCRIPTION)


class ConsentLookupRequest(StrictBase):
    """Body of `POST /privacy/consent/lookup`."""

    principal_id: str = Field(min_length=1, max_length=256, description=_PRINCIPAL_ID_DESCRIPTION)
    client_id: str | None = Field(default=None, max_length=128, description=_CLIENT_ID_DESCRIPTION)
    as_of: AwareDatetime | None = Field(
        default=None, description="Answer as of this moment; defaults to now."
    )


class PurposeConsent(StrictBase):
    """Whether the principal may be processed for one purpose, as of the lookup's moment."""

    purpose: str = Field(description="Purpose id.")
    state: Literal["valid", "withdrawn", "expired", "none"] = Field(
        description="`valid` (an unexpired grant decides), `withdrawn`, `expired`, or `none` (no record)."
    )


class ConsentLookupResponse(StrictBase):
    """`POST /privacy/consent/lookup`: one principal's consent, per purpose, and their ledger rows."""

    lookup_id: str = Field(description="This lookup's id; the audit event's object id.")
    principal_hash: str = Field(description="Salted hash of the principal's id.")
    client_id: str = Field(description="Client whose ledger was read.")
    as_of: AwareDatetime = Field(description="The moment the states were decided as of.")
    purposes: tuple[PurposeConsent, ...] = Field(description="One state per declared purpose.")
    records: tuple[ConsentRecord, ...] = Field(description="Every ledger row of the principal, oldest first.")


class RetentionPlanResponse(StrictBase):
    """`GET /privacy/retention/plan`: the dry run. Applying it needs `plan_id`, `planned_at` and `plan_hash`."""

    plan: RetentionPlan = Field(description="Every key that would be deleted or stripped, and why.")
    plan_hash: str = Field(description="SHA-256 of `planned_at` and the items; the apply checks it.")
    counts: dict[str, int] = Field(description="Items per category.")


class RetentionApplyRequest(StrictBase):
    """Body of `POST /privacy/retention/apply`: the reviewed plan, identified by what the dry run returned."""

    plan_id: str = Field(pattern=r"^ret_[0-9a-f]{16}$", description="`plan.plan_id` of the reviewed dry run.")
    planned_at: AwareDatetime = Field(description="`plan.planned_at` of the reviewed dry run.")
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$", description="`plan_hash` of the reviewed dry run.")


class RetentionApplyResponse(StrictBase):
    """`POST /privacy/retention/apply`: what the job deleted and stripped."""

    plan_id: str = Field(description="The plan that was applied.")
    plan_hash: str = Field(description="Its hash, equal to the reviewed dry run's.")
    result: RetentionResult


class ErasureRequestBody(StrictBase):
    """Body of `POST /privacy/erasure`."""

    principal_id: str = Field(min_length=1, max_length=256, description=_PRINCIPAL_ID_DESCRIPTION)
    client_id: str | None = Field(
        default=None,
        max_length=128,
        description="Client the request came from; recorded, and scopes the consent-history deletion. "
        "Every store is searched whatever it is.",
    )


class ErasureRetryBody(StrictBase):
    """Body of `POST /privacy/erasure/{request_id}/retry`: the person's id again (it is never stored)."""

    principal_id: str = Field(min_length=1, max_length=256, description=_PRINCIPAL_ID_DESCRIPTION)


class ErasureRequestList(StrictBase):
    """`GET /privacy/erasure`: the erasure register, newest first."""

    requests: tuple[ErasureRequestRecord, ...]


class AccessRequestBody(StrictBase):
    """Body of `POST /privacy/access-requests`: whose data to export."""

    principal_id: str = Field(min_length=1, max_length=256, description=_PRINCIPAL_ID_DESCRIPTION)
    client_id: str | None = Field(
        default=None, max_length=128, description="Limits the consent history included to this client."
    )


class RetrainFlagList(StrictBase):
    """`GET /privacy/retrain-flags`: model versions due for retraining because of an erasure."""

    flags: tuple[RetrainFlag, ...]


# --- end M48 -------------------------------------------------------------------------------------
# --- M49 (scheduling, monitoring, outcomes): api/routes/schedules.py and api/routes/monitoring.py --
# A schedule, a firing, an alert, an outcome report and the incrementality input are answered as the
# engine's own documents (`engine.scheduling`): each already holds ids, codes, counts and business
# language only (DEC-769, DEC-770, DEC-771), so a second API shape would be a copy to keep in step.
from engine.scheduling.alerts import Alert  # noqa: E402
from engine.scheduling.cron import DEFAULT_TIMEZONE  # noqa: E402
from engine.scheduling.outcomes import IncrementalityInput, OutcomeReport  # noqa: E402
from engine.scheduling.schedules import (  # noqa: E402
    Schedule,
    ScheduleFiring,
    ScheduleKind,
    ScheduleParameters,
)

ScheduleResponse = Schedule
"""`POST/GET/PATCH /schedules/{schedule_id}` and the enable/disable actions answer with the stored row."""

FiringResponse = ScheduleFiring
"""`POST /schedules/{schedule_id}/fire`: the firing as recorded (usually `running`, with its run id)."""

AlertResponse = Alert
"""`POST /monitoring/alerts/{alert_id}/acknowledge`: the alert as stored after acknowledgement."""

OutcomeReportResponse = OutcomeReport
"""`POST/GET /runs/{run_id}/outcomes`: real-world performance of one scoring run (schema version 1)."""

IncrementalityInputResponse = IncrementalityInput
"""`GET /runs/{run_id}/incrementality-input`: what Plan B's incrementality report consumes (v1)."""

_CADENCE_DESCRIPTION = (
    "`daily`, `weekly` or `monthly` (02:00 in the schedule's timezone), or a five-field cron line "
    "(minute hour day-of-month month day-of-week) that EventBridge can also run."
)


class ScheduleCreateRequest(StrictBase):
    """Body of `POST /schedules`: one recurring piece of work for one client and one use case."""

    use_case_id: str = Field(min_length=1, max_length=128, description="Use case the work is for.")
    kind: ScheduleKind = Field(description="`score`, `drift_check` or `retrain`.")
    cadence: str = Field(min_length=1, max_length=120, description=_CADENCE_DESCRIPTION)
    client_id: str | None = Field(
        default=None,
        max_length=128,
        description="Client the work is for. Defaults to the client of `parameters.onboarding_spec_id`.",
    )
    timezone: str = Field(
        default=DEFAULT_TIMEZONE, min_length=1, max_length=64, description="IANA zone the cadence is read in."
    )
    parameters: ScheduleParameters = Field(
        default_factory=ScheduleParameters,
        description="Ids the work reads: a recipe to rebuild or a fixed dataset (a score needs one).",
    )
    enabled: bool = Field(default=True, description="Create it paused with `false`.")


class ScheduleUpdateRequest(StrictBase):
    """Body of `PATCH /schedules/{schedule_id}`. Omitted fields are left as they are."""

    cadence: str | None = Field(default=None, min_length=1, max_length=120, description=_CADENCE_DESCRIPTION)
    timezone: str | None = Field(default=None, min_length=1, max_length=64, description="IANA zone.")
    enabled: bool | None = Field(default=None, description="Pause (`false`) or resume (`true`).")
    parameters: ScheduleParameters | None = Field(
        default=None, description="The complete new set of ids the work reads."
    )


class ScheduleListResponse(StrictBase):
    """`GET /schedules`: matching schedules, oldest first."""

    schedules: tuple[Schedule, ...]


class FiringListResponse(StrictBase):
    """`GET /schedules/{schedule_id}/firings` and `GET /monitoring/missed-firings`: newest first."""

    firings: tuple[ScheduleFiring, ...]


class RetrainingSyncResponse(StrictBase):
    """`POST /schedules/retraining/sync`: the managed schedules `monitoring.retraining` now implies."""

    targets: int = Field(description="Client x use case pairs with a training recipe that were checked.")
    created: tuple[str, ...] = Field(description="Managed schedule ids created.")
    updated: tuple[str, ...] = Field(description="Managed schedule ids whose cadence changed.")
    removed: tuple[str, ...] = Field(description="Managed schedule ids removed (the setting became manual).")


class AlertListResponse(StrictBase):
    """`GET /monitoring/alerts`: matching alerts, newest first."""

    alerts: tuple[Alert, ...]


# --- end M49 -------------------------------------------------------------------------------------
from engine.approvals import ApprovalItem, ModelDecision  # noqa: E402


# Plan D M54 (DEC-862, DEC-864): the Approver's screen. The approvals models themselves live in
# `engine.approvals`, like the privacy contracts live in `engine.privacy.contracts`.
class ModelRejectRequest(StrictBase):
    """Body of `POST /models/{model_id}/reject`: a challenger waiting for approval is turned down."""

    rejected_by: str | None = Field(
        default=None,
        max_length=200,
        description="Ignored when sign-in is on (the signed-in person is recorded); a label otherwise.",
    )
    reason: str = Field(min_length=3, max_length=2000, description="Why it is rejected. Required.")


class ApprovalListResponse(StrictBase):
    """`GET /approvals`: every challenger waiting for an Approver, and whether the caller may decide."""

    items: tuple[ApprovalItem, ...]
    separation_enforced: bool = Field(
        description="False with sign-in off: one local operator holds every role, so the trainer cannot be told apart."
    )


class ModelDecisionResponse(StrictBase):
    """The version as it now stands and the decision just recorded."""

    version: ModelVersion
    is_champion: bool
    decision: ModelDecision


# ---- END PHASE-4B ----
# ---- PHASE-3B (uplift) — append only below this line ----
# The bodies of `api/routes/uplift.py` (plan B §8). The artefacts those routes return are the engine's
# own contracts in `engine/uplift/contracts.py`; only request shapes and the two envelopes live here.
from engine.uplift.contracts import UpliftValidationReport  # noqa: E402


class TreatmentCandidate(StrictBase):
    """One column of an upload that could record who was treated: only 0/1 values, both present."""

    column: str = Field(description="Column name, as spelled in the file.")
    treated_share: float = Field(description="Share of rows with the value 1, 0 to 1.")
    hinted: bool = Field(description="True when the column is the configured one or matches a name hint.")


class TreatmentCandidatesResponse(StrictBase):
    """Body of `GET /uploads/{upload_id}/treatment-candidates`."""

    candidates: tuple[TreatmentCandidate, ...] = Field(description="Every 0/1 column, in file order.")
    detected: str | None = Field(
        description="The column the uplift checks would use: the configured one, else the first hint present."
    )


class UpliftRunRequest(StrictBase):
    """Body of `POST /uplift/runs`: an uplift training run on one uploaded file.

    `overrides` takes the same nested or dotted run overrides as `RunRequest.overrides`; the route
    adds `problem_type: uplift` and, when given, `uplift.treatment_column` on top of them.
    """

    use_case: str = Field(description="Use case the run belongs to.")
    upload_id: str = Field(description="Upload uploaded in train mode.")
    primary_key: PrimaryKey = Field(
        description=(
            "Column that identifies a customer, or two columns - the customer and the snapshot date - "
            "for a file with one row per customer per snapshot. Treatment is assigned per customer."
        )
    )
    target: str = Field(description="Binary outcome column.")
    treatment_column: str | None = Field(
        default=None,
        description="0/1 column recording who was treated; the configured or hinted one when null.",
    )
    overrides: dict[str, Any] = Field(default_factory=dict, description="Run overrides, nested or dotted.")


class UpliftValidationErrorResponse(StrictBase):
    """The `409` of `POST /uplift/runs`: M1's envelope plus both reports, so Setup renders one response."""

    detail: ErrorBody
    validation: ValidationReport
    uplift_validation: UpliftValidationReport


class CampaignResultsRequest(StrictBase):
    """Body of `POST /runs/{run_id}/campaign-results`: an uploaded outcomes file and how to read it."""

    upload_id: str = Field(description="Upload holding the primary key and the observed outcome.")
    outcome_column: str = Field(description="Outcome column of that file.")
    positive_label: str | None = Field(default=None, description="Outcome value that counts as a conversion.")
    outcome_window_days: int | None = Field(
        default=None,
        ge=0,
        description="Days after treatment the outcome is measured over; null = all mature.",
    )
    treatment_date_column: str | None = Field(
        default=None,
        description="Per-row treatment date in the outcomes file; the run's finish time when null.",
    )
    as_of: AwareDatetime | None = Field(
        default=None, description="Reference time for maturity; now when null."
    )
    bands: tuple[str, ...] | None = Field(
        default=None,
        description="Bands to measure within, for a Phase 1 run; an uplift run uses its intended set.",
    )
    campaign_id: str | None = Field(default=None, description="Campaign the report is about, if known.")


class OpeRequest(StrictBase):
    """Body of `POST /runs/{run_id}/uplift/ope`: the targeting rule to evaluate on the hold-out."""

    top_share: float | None = Field(
        default=None, gt=0.0, le=1.0, description="Treat the top share by predicted uplift, 0 to 1."
    )
    min_uplift: float | None = Field(
        default=None, ge=-1.0, le=1.0, description="Treat every row whose predicted uplift is at least this."
    )

    @model_validator(mode="after")
    def _a_rule(self) -> OpeRequest:
        if self.top_share is None and self.min_uplift is None:
            raise ValueError("Give top_share, min_uplift or both: a policy needs a rule.")
        return self


# ---- END PHASE-3B ----
# ---- PLAN-E (pilot) — append only below this line ----
# The bodies of `api/routes/pilot.py` (Plan E, M59-M64). The reports themselves are the engine's own
# models in `engine/pilot/`; only the request shapes and the two envelopes live here.
from engine.pilot.demo import DemoManifest  # noqa: E402
from engine.pilot.feedback import FeedbackCategory  # noqa: E402


class PilotDemoResponse(StrictBase):
    """`GET /pilot/demo`: whether demo mode is on, and the seeded demo when there is one."""

    demo_mode: bool = Field(
        description="MARKETING_AI_DEMO_MODE: the screens show the demo client and the tour."
    )
    seeded: bool = Field(description="A demo has been seeded into this deployment's storage.")
    manifest: DemoManifest | None = Field(
        default=None, description="What the seed made; null when not seeded."
    )
    how_to_seed: str = Field(default="", description="The command that seeds one, when none is seeded.")


class PilotFeedbackRequest(StrictBase):
    """`POST /pilot/feedback`: what a person thought of one screen."""

    screen: str = Field(
        max_length=200, description="The page's route, for example #/pilot/value/r_20260923_ab12cd34."
    )
    category: FeedbackCategory = Field(description="confusing, wrong, idea, praise or other.")
    text: str = Field(default="", max_length=1000, description="Their words; contact details are masked.")


class PilotFeedbackResponse(StrictBase):
    """`POST /pilot/feedback`: the stored entry's id, and what was masked out of the text."""

    feedback_id: str
    redacted: tuple[str, ...] = ()


# ---- END PLAN-E ----
