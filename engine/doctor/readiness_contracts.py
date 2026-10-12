"""The readiness report's contract (Plan O M234, DEC-1804; the design §10; [RDY] §5).

`DataReadinessReport` is the one answer to "will this run go through?". It is computed by
`engine.doctor.readiness.check_readiness` on the **prepared** copy - the file a run would read - and
composes the dry run `POST /runs` itself performs (`check_plan`), the uplift checks for an uplift goal,
and the fifteen X-probes, each of which simulates a step after validation where a run fails today
although validation passed ([RDY] §1.7, the nine gaps).

The report holds **no cell value**: counts, shares, column names, codes and plain sentences only. The
dry run's own `ValidationReport` rides along (its messages may quote a label), so the whole report is a
case artefact and stays session-only; `ReadinessSummary` is the aggregate view a tool may return.

**The invariant** is enforced by the model itself: a report whose verdict is `ready` (or
`ready_with_warnings`) and whose dry run failed, or with a failing probe, an open blocking question or an
approval not yet recorded, cannot be constructed.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from engine.config import StrictBase
from engine.contracts import ValidationReport
from engine.uplift.contracts import UpliftValidationReport

__all__ = [
    "PROBE_CODES",
    "PROBE_GATES",
    "READINESS_CODES",
    "READINESS_SCHEMA_VERSION",
    "Acknowledgement",
    "ApprovalKind",
    "ApprovalRequired",
    "DataReadinessReport",
    "FixApplied",
    "Gate",
    "OpenQuestion",
    "PostFilterCounts",
    "PreparedCopy",
    "ProbeResult",
    "ProbeStatus",
    "ProbeSummary",
    "QuestionOption",
    "ReadinessMode",
    "ReadinessRoles",
    "ReadinessSummary",
    "ReadinessVerdict",
    "Requirement",
    "RequirementStatus",
    "ScoringContract",
    "SettingApplied",
    "SimulatedPart",
    "SourceFacts",
    "UseCaseType",
]

READINESS_SCHEMA_VERSION: Final = "1"

ReadinessVerdict = Literal["ready", "ready_with_warnings", "needs_answers", "needs_approval", "not_ready"]
"""One verdict replaces seven (the design §10.3)."""

UseCaseType = Literal[
    "propensity",
    "regression",
    "time_based",
    "uplift",
    "uplift_multi_arm",
    "hybrid_rca",
    "hybrid_copy",
    "generative_rag",
]
ReadinessMode = Literal["train", "score", "measure_outcomes", "audit"]
ProbeStatus = Literal["pass", "fail", "warn", "skipped"]
RequirementStatus = Literal["pass", "fail", "warn", "skipped", "acknowledged"]
Gate = Literal[
    "upload",
    "validate",
    "uplift_validate",
    "prepare",
    "split",
    "train",
    "evaluate",
    "score_schema",
    "recipe_replay",
    "actions",
    "generate",
    "measure",
]
ApprovalKind = Literal["ack_error", "governance_change", "threshold_change", "suppression_change", "pii_keep"]

PROBE_GATES: Final[dict[str, Gate]] = {
    "X_TIME_ALL_PARSE": "split",
    "X_TIME_CUTS_FEASIBLE": "split",
    "X_GROUPS_ENOUGH": "split",
    "X_CONSENT_VOCAB": "prepare",
    "X_POST_FILTER_THRESHOLDS": "prepare",
    "X_SPLIT_CLASSES": "evaluate",
    "X_LABEL_MATURITY": "evaluate",
    "X_REGRESSION_TARGET_NUMERIC": "train",
    "X_PARSER_ROW_LOSS": "upload",
    "X_PK_UNIQUE_FULL": "validate",
    "X_TREATMENT_TOKENS": "uplift_validate",
    "X_CHANNEL_COLUMNS_PRESENT": "actions",
    "X_VALUE_COLUMN_MISSING_SHARE": "actions",
    "X_ALLOWED_FIELDS_FILLED": "generate",
    "X_COMPLAINT_COLUMN_PRESENT": "generate",
}
"""The fifteen X-probes of the design §10.2, in its order, each with the step whose failure it predicts."""

PROBE_CODES: Final[tuple[str, ...]] = tuple(PROBE_GATES)

READINESS_CODES: Final[frozenset[str]] = frozenset(PROBE_CODES)
"""Every code this milestone adds (the fifteen probe codes). They belong in `PLAN_O_CODES` and
`configs/pilot/help.yaml`; M230 owns both files, so the integrator joins them (DEC-1804 (j))."""

_JsonScalar = int | float | str | bool | None


class SourceFacts(StrictBase):
    """The file the run reads, and how much of it the reader keeps."""

    upload_id: str = Field(description="The upload whose stored file the run reads.")
    file_name: str | None = Field(default=None, description="The file's name as it arrived.")
    format: Literal["csv", "parquet"] | None = Field(default=None, description="How the stored file is read.")
    encoding: str | None = Field(default=None, description="Character encoding, for a CSV.")
    delimiter: str | None = Field(default=None, description="Delimiter, for a CSV.")
    size_bytes: int | None = Field(default=None, description="Bytes stored.")
    rows_physical: int | None = Field(
        default=None, description="Data records in the stored file, counted line by line; null when not read."
    )
    rows_parsed: int = Field(description="Rows the engine's reader keeps (`profile.row_count`).")
    rows_dropped_by_parser: int | None = Field(
        default=None, description="Records the reader drops without saying so; null when not measured."
    )
    fingerprint: str | None = Field(default=None, description="The dataset fingerprint, when known.")
    profile_row_cap: int = Field(description="Rows the checks read; rows past it are counted, not checked.")
    sampled: bool = Field(description="True when the file has more rows than the checks read.")


class PreparedCopy(StrictBase):
    """The prepared copy the report is about, when a recipe made it."""

    upload_id: str = Field(description="The prepared upload.")
    source_upload_id: str | None = Field(default=None, description="The upload the recipe read.")
    data_recipe_hash: str | None = Field(default=None, description="Hash of the recipe that made it.")
    recipe_levels: tuple[str, ...] = Field(default=(), description="The recipe levels it was allowed.")
    max_failure_pct: float | None = Field(default=None, description="The replay failure limit it carries.")
    prepares_like_model_recipe: bool | None = Field(
        default=None, description="For a scoring file: whether the model's recipe prepares it the same way."
    )


class ReadinessRoles(StrictBase):
    """Which column plays which part, as the run will read them."""

    primary_key: tuple[str, ...] = Field(default=(), description="The ID column, or columns.")
    entity_column: str | None = Field(default=None, description="The entity column of a composite key.")
    target: str | None = Field(default=None, description="The outcome column.")
    positive_label_source: Literal["config", "token", "rarer", "none"] = Field(
        default="none",
        description="How the positive outcome value was chosen: configured, a known word, or the rarer value.",
    )
    time_column: str | None = None
    group_column: str | None = None
    consent_column: str | None = None
    opt_out_column: str | None = None
    opt_out_direction: Literal["true_keeps", "none"] = Field(
        default="none", description="`true_keeps`: a true value is consent; false or empty suppresses."
    )
    recently_contacted_column: str | None = None
    treatment_column: str | None = None
    treatment_levels: tuple[str, ...] = ()
    treatment_date_column: str | None = None
    value_column: str | None = None
    channel_columns: tuple[str, ...] = ()
    excluded_columns: tuple[str, ...] = ()


class Requirement(StrictBase):
    """One gate of the run, and whether this file meets it."""

    id: str = Field(description="The requirement: an engine check code, or a reference-set rule.")
    applies_to: str = Field(description="Which use-case type or mode it applies to.")
    engine_code: str | None = Field(description="The code the engine raises when it is not met.")
    source_ref: str = Field(description="Where the rule lives in the code.")
    gate: Gate = Field(description="The step that enforces it.")
    status: RequirementStatus
    skip_reason: str | None = None
    column: str | None = None
    blocking: bool = Field(description="True when an unmet requirement stops the run.")
    message: str = Field(default="", description="The engine's own sentence, when it said one.")


class ProbeResult(StrictBase):
    """One X-probe: a simulation of a step after validation, and what it found."""

    code: str = Field(description="One of the fifteen probe codes.")
    gate: Gate
    status: ProbeStatus
    column: str | None = None
    measured: float | None = Field(default=None, description="The number measured; see `details`.")
    threshold: float | None = Field(default=None, description="The number it was held against.")
    message: str = Field(default="", description="What was found, in plain words.")
    fix: str = Field(default="", description="What to do, and what to collect, when it does not pass.")
    skip_reason: str | None = None
    details: dict[str, _JsonScalar] = Field(
        default_factory=dict, description="Counts and shares behind the message; never a cell value."
    )

    @model_validator(mode="after")
    def _known(self) -> ProbeResult:
        if self.code not in PROBE_GATES:
            raise ValueError(f"unknown readiness probe {self.code!r}")
        if self.status == "skipped" and not self.skip_reason:
            raise ValueError(f"{self.code} is skipped without a reason")
        return self


class SimulatedPart(StrictBase):
    """One part of the dry-run split."""

    name: Literal["train", "validation", "test"]
    rows: int
    positives: int | None = None
    classes: int | None = Field(default=None, description="Distinct outcome values in the part.")


class PostFilterCounts(StrictBase):
    """Rows after each rule the prepare stage applies before the split, and the simulated split."""

    rows_in: int
    rows_after_consent: int
    rows_after_missing_target: int
    rows_after_dedupe: int
    rows_after_filters: int
    positives_after_filters: int | None = None
    estimated: bool = Field(
        default=False,
        description="True when the file is larger than the rows read and the counts are scaled.",
    )
    parts: tuple[SimulatedPart, ...] = ()
    test_primary_metric_defined: bool | None = Field(
        default=None, description="Whether the newest or held-out part can be scored at all."
    )


class FixApplied(StrictBase):
    """One step the prepared copy went through."""

    step_kind: str
    column: str
    params: dict[str, JsonValue] = Field(default_factory=dict)
    rows_changed: int = 0
    rows_failed: int = 0
    failure_pct: float = 0.0
    evidence_id: str | None = None
    auto_or_approved: Literal["auto", "approved"] = "approved"


class SettingApplied(StrictBase):
    """One run setting the person chose, by its dotted path."""

    path: str
    value: JsonValue = None


class Acknowledgement(StrictBase):
    """One entry of `validation.acknowledged`, and who agreed to it."""

    key: str = Field(description="CODE or CODE:column.")
    approved_by: str | None = Field(default=None, description="The user id that agreed; null if unrecorded.")
    approved_at: AwareDatetime | None = None
    effect: str = Field(default="", description="What acknowledging it does.")


class QuestionOption(StrictBase):
    id: str
    label: str
    effect: str = ""


class OpenQuestion(StrictBase):
    """A question the case holds; a blocking one stops `ready`."""

    id: str
    blocking: bool
    code: str | None = None
    column: str | None = None
    text: str
    options: tuple[QuestionOption, ...] = ()
    answered: bool = False


class ApprovalRequired(StrictBase):
    """Something only a person may agree to, and whether they have (with their user id)."""

    kind: ApprovalKind
    key: str = Field(description="The code, CODE:column or setting path the approval is for.")
    code: str | None = None
    path: str | None = None
    proposed_value: JsonValue = None
    consequence: str = ""
    recorded: bool = False
    approved_by: str | None = None
    approved_at: AwareDatetime | None = None


class ScoringContract(StrictBase):
    """For a scoring file: the model's columns against the file's."""

    model_version_id: str
    expected_columns: tuple[str, ...]
    extra: tuple[str, ...]
    missing: tuple[str, ...]
    type_changed: tuple[str, ...]


class DataReadinessReport(StrictBase):
    """`data_readiness.json`: one verdict, every requirement and every probe, on the prepared copy."""

    schema_version: Literal["1"] = READINESS_SCHEMA_VERSION
    generated_at: AwareDatetime
    agent_version: str
    engine_version: str
    use_case_id: str
    use_case_type: UseCaseType
    mode: ReadinessMode
    goal: str | None = Field(
        default=None, description="The goal brief this report answers, when there is one."
    )
    source: SourceFacts
    prepared: PreparedCopy | None = None
    roles: ReadinessRoles
    resolved_overrides: dict[str, JsonValue] = Field(default_factory=dict)
    requirements: tuple[Requirement, ...]
    probes: tuple[ProbeResult, ...]
    post_filter_counts: PostFilterCounts | None = None
    fixes_applied: tuple[FixApplied, ...] = ()
    settings_applied: tuple[SettingApplied, ...] = ()
    acknowledgements: tuple[Acknowledgement, ...] = ()
    open_questions: tuple[OpenQuestion, ...] = ()
    approvals_required: tuple[ApprovalRequired, ...] = ()
    hidden_columns: tuple[str, ...] = Field(default=(), description="Columns the run will not learn from.")
    scoring_contract: ScoringContract | None = None
    measurement_contract: dict[str, JsonValue] | None = None
    dry_run_passed: bool = Field(description="True when the checks the run itself performs pass.")
    dry_run_report: ValidationReport | None = Field(
        default=None, description="`check_plan`'s report: exactly what `POST /runs` would answer."
    )
    uplift_report: UpliftValidationReport | None = Field(
        default=None, description="The uplift checks `POST /uplift/runs` performs, for an uplift goal."
    )
    verdict: ReadinessVerdict
    verdict_reason: str
    report_hash: str = Field(description="sha256 of the report's substance (no timestamps, no raw reports).")

    @model_validator(mode="after")
    def _invariant(self) -> DataReadinessReport:
        if len(self.probes) != len(PROBE_CODES) or tuple(p.code for p in self.probes) != PROBE_CODES:
            raise ValueError("a readiness report lists every probe, once, in the design's order")
        if self.verdict in ("ready", "ready_with_warnings"):
            if not self.dry_run_passed:
                raise ValueError("verdict ready with a failing dry run")
            if any(probe.status == "fail" for probe in self.probes):
                raise ValueError("verdict ready with a failing probe")
            if any(q.blocking and not q.answered for q in self.open_questions):
                raise ValueError("verdict ready with an open blocking question")
            if any(not a.recorded for a in self.approvals_required):
                raise ValueError("verdict ready with an approval not recorded")
        return self

    def probe(self, code: str) -> ProbeResult:
        """The probe called `code`."""
        return next(probe for probe in self.probes if probe.code == code)

    @property
    def failing_probes(self) -> tuple[str, ...]:
        return tuple(probe.code for probe in self.probes if probe.status == "fail")


class ProbeSummary(StrictBase):
    code: str
    status: ProbeStatus
    gate: Gate


class ReadinessSummary(StrictBase):
    """The aggregate view of a report: what `check_readiness` returns as a tool (exposure `aggregate`)."""

    report_hash: str
    use_case_id: str
    use_case_type: UseCaseType
    mode: ReadinessMode
    verdict: ReadinessVerdict
    verdict_reason: str
    dry_run_passed: bool
    probes: tuple[ProbeSummary, ...]
    failing_requirements: tuple[str, ...]
    open_blocking_questions: int
    approvals_waiting: int
    rows_after_filters: int | None
