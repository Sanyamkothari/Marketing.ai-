"""`check_readiness`: one readiness report, whose `ready` means the run will not fail for a data reason
(Plan O M234, DEC-1804; the design §10; [RDY] §1.7 and §5).

The report is computed on the **prepared** copy - the upload a run would read, after Guided setup's
recipe - and composes, never re-implements:

1. **the dry run**: `engine.agent.checks.check_plan`, which calls exactly what `POST /runs` calls, with
   the same arguments; for an uplift goal also `run_uplift_checks`, exactly what `POST /uplift/runs`
   adds; for the documents assistant, the reference-set rules its evaluation enforces;
2. **the fifteen X-probes** (`engine.doctor.readiness_probes`), which simulate the steps after
   validation where a run fails today although validation passed ([RDY] §1.7);
3. what the case holds: open questions, approvals required and recorded (with the user id), the
   recipe's receipt and the settings the person chose.

**The verdict** (the design §10.3), checked in this order:

* `not_ready` - the dry run fails, or a probe fails; the reason names what to do and what to collect;
* `needs_answers` - a blocking question is open;
* `needs_approval` - something only a person may agree to is waiting (an error-severity finding
  acknowledged, a governance or threshold setting changed, personal data kept), with no recorded yes;
* `ready_with_warnings` - as `ready`, with warnings shown;
* `ready` - the dry run passes, every probe passes, no blocking question is open and every required
  approval is recorded.

**The invariant** `verdict == ready ⇒ dry run passed and no probe failed` is enforced by
`DataReadinessReport` itself, tested as a property, and checked on the Plan L benchmark.

Storage: the report is stored in the case at `cases/<client>/<case>/readiness/<goal>_<hash>.json`
(`store_readiness`) and copied into the run directory beside `data_recipe.json`
(`attach_readiness_to_run`). This module writes nothing on its own; the case (M231) and the run route
call those two helpers, behind `data_doctor.unified`.

`check_readiness` is registered as a READ tool of exposure `aggregate` through
`register_readiness_tool`, which the Plan O registry (M232) calls; the tool returns `ReadinessSummary`,
which carries no cell value.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from pydantic import Field, JsonValue

from engine import __version__ as engine_version
from engine import keys
from engine.agent.checks import check_plan
from engine.agentic.canonical import digest
from engine.config import (
    AiType,
    GenerativeKind,
    PiiHandling,
    PrimaryKey,
    ProblemType,
    RunMode,
    SplitType,
    StrictBase,
    UseCaseConfig,
)
from engine.contracts import FeatureSchema, Severity, ValidationReport
from engine.doctor.readiness_contracts import (
    PROBE_CODES,
    Acknowledgement,
    ApprovalRequired,
    DataReadinessReport,
    FixApplied,
    OpenQuestion,
    PreparedCopy,
    ProbeResult,
    ProbeSummary,
    ReadinessRoles,
    ReadinessSummary,
    ReadinessVerdict,
    Requirement,
    ScoringContract,
    SettingApplied,
    SourceFacts,
    UseCaseType,
)
from engine.doctor.readiness_probes import ProbeRun, SourceScan, label_key, run_probes, scan_source
from engine.utils.time import utc_now

if TYPE_CHECKING:
    import pandas as pd

    from engine.storage import Storage
    from engine.uplift.contracts import UpliftValidationReport

__all__ = [
    "AGENT_VERSION",
    "CHECK_READINESS_TOOL",
    "READINESS_RUN_FILENAME",
    "CheckReadinessArgs",
    "ReadinessInputs",
    "ReadinessToolContext",
    "ReadinessToolSpec",
    "RecordedApproval",
    "attach_readiness_to_run",
    "check_readiness",
    "check_readiness_tool",
    "decide_verdict",
    "inputs_for_upload",
    "readiness_key",
    "register_readiness_tool",
    "store_readiness",
    "summarise",
    "use_case_type",
]

AGENT_VERSION: Final[str] = "data-doctor/readiness-1"
READINESS_RUN_FILENAME: Final[str] = "data_readiness.json"
"""In the run directory, beside `data_recipe.json`."""
CHECK_READINESS_TOOL: Final[str] = "check_readiness"
_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_APPROVAL_PATHS: Final[dict[str, Literal["threshold_change", "governance_change", "suppression_change"]]] = {
    "validation.min_rows": "threshold_change",
    "validation.min_positive": "threshold_change",
    "validation.leakage_check": "threshold_change",
    "validation.leakage_auc_threshold": "threshold_change",
    "governance.consent_column": "governance_change",
    "prepare.pii_handling": "governance_change",
    "actions.suppression.suppress_opted_out": "suppression_change",
    "actions.suppression.opt_out_column": "suppression_change",
    "actions.suppression.suppress_recently_contacted": "suppression_change",
    "actions.suppression.recently_contacted_column": "suppression_change",
}
"""Run settings only a person may change, with a recorded yes ([RDY] §4 "Always APPROVE")."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RecordedApproval:
    """A person's recorded yes: to an acknowledgement (`CODE` / `CODE:column`) or a setting path."""

    key: str
    approved_by: str
    approved_at: datetime | None = None


@dataclass(frozen=True)
class ReadinessInputs:
    """Everything `check_readiness` reads. Nothing here is fetched; the caller loads it.

    `frame` is the prepared copy **as the run reads it**: the stored rows capped at the profile row cap
    (`ingest.profile_row_cap`); `row_count` is the file's exact row count. `config` is the run's resolved
    configuration (overrides applied; for a built dataset with a composite key, after
    `keys.split_config_for_key`; for uplift, with the uplift route's own overrides).
    """

    frame: pd.DataFrame
    config: UseCaseConfig
    mode: RunMode
    primary_key: PrimaryKey | None
    target: str | None
    upload_id: str
    row_count: int
    schema: FeatureSchema | None = None
    source: SourceFacts | None = None
    scan: SourceScan | None = None
    prepared: PreparedCopy | None = None
    fixes_applied: tuple[FixApplied, ...] = ()
    resolved_overrides: Mapping[str, Any] = field(default_factory=dict)
    open_questions: tuple[OpenQuestion, ...] = ()
    approvals_required: tuple[ApprovalRequired, ...] = ()
    approvals: tuple[RecordedApproval, ...] = ()
    goal: str | None = None
    now: datetime | None = None


# ---------------------------------------------------------------------------
# The use-case type
# ---------------------------------------------------------------------------
def use_case_type(config: UseCaseConfig) -> UseCaseType:
    """Which of [RDY] §5's eight kinds of goal `config` is."""
    if config.ai_type is AiType.GENERATIVE or config.generative.kind is GenerativeKind.RAG_ASSISTANT:
        return "generative_rag"
    if config.problem_type is ProblemType.UPLIFT:
        return "uplift_multi_arm" if config.uplift.treatment_levels else "uplift"
    if config.generative.kind is GenerativeKind.ROOT_CAUSE_SUMMARY:
        return "hybrid_rca"
    if config.generative.kind is GenerativeKind.CAMPAIGN_COPY:
        return "hybrid_copy"
    if config.problem_type is ProblemType.REGRESSION:
        return "regression"
    if config.split.type is SplitType.TIME_BASED:
        return "time_based"
    return "propensity"


# ---------------------------------------------------------------------------
# check_readiness
# ---------------------------------------------------------------------------
def check_readiness(inputs: ReadinessInputs) -> DataReadinessReport:
    """The readiness report for the prepared copy in `inputs` (see the module docstring)."""
    config = inputs.config
    if inputs.mode is RunMode.SCORE and inputs.schema is None and use_case_type(config) != "generative_rag":
        raise ValueError("a scoring readiness report needs the model version's schema")
    kind = use_case_type(config)
    dry_report: ValidationReport | None = None
    uplift_report: UpliftValidationReport | None = None
    if kind == "generative_rag":
        requirements, dry_passed = _reference_set_requirements(inputs)
    else:
        dry_report = check_plan(
            inputs.frame,
            config,
            mode=inputs.mode,
            primary_key=inputs.primary_key,
            target=inputs.target,
            upload_id=inputs.upload_id,
            row_count=inputs.row_count,
            schema=inputs.schema,
        )
        requirements = _requirements_from(dry_report, inputs.mode, kind)
        dry_passed = dry_report.passed
        if config.problem_type is ProblemType.UPLIFT and inputs.mode is RunMode.TRAIN:
            uplift_report, uplift_requirements = _uplift_checks(inputs, kind)
            requirements = requirements + uplift_requirements
            dry_passed = dry_passed and (uplift_report is None or uplift_report.passed)
    probed: ProbeRun = run_probes(
        inputs.frame,
        config,
        mode=inputs.mode,
        primary_key=inputs.primary_key,
        target=inputs.target,
        row_count=inputs.row_count,
        scan=inputs.scan,
    )
    approvals = _approvals_required(inputs, dry_report, uplift_report)
    warnings = _warning_count(dry_report, uplift_report)
    verdict, reason = decide_verdict(
        dry_run_passed=dry_passed,
        probes=probed.probes,
        open_questions=inputs.open_questions,
        approvals_required=approvals,
        warnings=warnings,
        failing_requirements=tuple(r.id for r in requirements if r.status == "fail"),
    )
    fields: dict[str, Any] = {
        "generated_at": inputs.now or utc_now(),
        "agent_version": AGENT_VERSION,
        "engine_version": engine_version,
        "use_case_id": config.id,
        "use_case_type": kind,
        "mode": inputs.mode.value,
        "goal": inputs.goal,
        "source": inputs.source or _default_source(inputs),
        "prepared": inputs.prepared,
        "roles": _roles(inputs),
        "resolved_overrides": _jsonable_overrides(inputs.resolved_overrides),
        "requirements": requirements,
        "probes": probed.probes,
        "post_filter_counts": probed.post_filter,
        "fixes_applied": inputs.fixes_applied,
        "settings_applied": tuple(
            SettingApplied(path=path, value=value)
            for path, value in sorted(_jsonable_overrides(inputs.resolved_overrides).items())
        ),
        "acknowledgements": _acknowledgements(config, inputs.approvals),
        "open_questions": inputs.open_questions,
        "approvals_required": approvals,
        "hidden_columns": probed.hidden_columns,
        "scoring_contract": _scoring_contract(inputs),
        "measurement_contract": None,
        "dry_run_passed": dry_passed,
        "dry_run_report": dry_report,
        "uplift_report": uplift_report,
        "verdict": verdict,
        "verdict_reason": reason,
        "report_hash": "",
    }
    draft = DataReadinessReport(**fields)
    return draft.model_copy(update={"report_hash": report_hash(draft)})


def report_hash(report: DataReadinessReport) -> str:
    """The content hash of a report's substance: no timestamps, no raw reports (they carry their own)."""
    body = report.model_dump(
        mode="json",
        exclude={"generated_at", "report_hash", "dry_run_report", "uplift_report", "engine_version"},
    )
    return digest(body)


def decide_verdict(
    *,
    dry_run_passed: bool,
    probes: Sequence[ProbeResult],
    open_questions: Sequence[OpenQuestion],
    approvals_required: Sequence[ApprovalRequired],
    warnings: int,
    failing_requirements: Sequence[str] = (),
) -> tuple[ReadinessVerdict, str]:
    """The verdict and its reason, in plain words (the module docstring gives the order).

    `warnings` counts the run's own unacknowledged warnings; a probe that warns is counted here.
    """
    failing = [probe for probe in probes if probe.status == "fail"]
    if not dry_run_passed or failing:
        parts: list[str] = []
        if not dry_run_passed:
            named = ", ".join(sorted(set(failing_requirements))) or "a blocking check"
            parts.append(f"the run's own checks would refuse this file ({named})")
        for probe in failing:
            parts.append(f"{probe.code}: {probe.message} {probe.fix}".strip())
        return "not_ready", "Not ready: " + "; ".join(parts)
    blocking = [q for q in open_questions if q.blocking and not q.answered]
    if blocking:
        return (
            "needs_answers",
            f"Needs answers: {len(blocking)} question(s) must be answered first ({blocking[0].text}).",
        )
    waiting = [a for a in approvals_required if not a.recorded]
    if waiting:
        named = ", ".join(a.key for a in waiting)
        return (
            "needs_approval",
            f"Needs approval: {len(waiting)} item(s) need a person's recorded yes ({named}).",
        )
    warnings += sum(1 for probe in probes if probe.status == "warn")  # a warning probe is a warning too
    if warnings:
        return (
            "ready_with_warnings",
            f"Ready, with {warnings} warning(s) to read first: the run's checks and every probe pass.",
        )
    return "ready", "Ready: the run's checks and every probe pass, nothing is waiting for an answer or a yes."


# ---------------------------------------------------------------------------
# The dry run, as requirements
# ---------------------------------------------------------------------------
def _requirements_from(report: ValidationReport, mode: RunMode, kind: UseCaseType) -> tuple[Requirement, ...]:
    from engine.stages import validate

    specs = (*validate.checks_for(mode), *validate.extension_checks_for(mode))
    by_code: dict[str, list[Any]] = {}
    for check in report.checks:
        by_code.setdefault(check.code, []).append(check)
    out: list[Requirement] = []
    seen: set[str] = set()
    for spec in specs:
        seen.add(spec.code)
        findings = by_code.get(spec.code, [])
        if not findings:
            out.append(
                Requirement(
                    id=spec.code,
                    applies_to=f"{kind}:{mode.value}",
                    engine_code=spec.code,
                    source_ref=f"engine/stages/validate.py check {spec.code}",
                    gate="validate" if mode is RunMode.TRAIN else "score_schema",
                    status="pass",
                    blocking=spec.severity is Severity.ERROR,
                )
            )
        for check in findings:
            out.append(
                _requirement_of(
                    check, kind=kind, mode=mode, gate="validate" if mode is RunMode.TRAIN else "score_schema"
                )
            )
    for code, findings in by_code.items():  # a code the registry does not list (none today)
        if code not in seen:
            out.extend(_requirement_of(check, kind=kind, mode=mode, gate="validate") for check in findings)
    return tuple(out)


def _requirement_of(check: Any, *, kind: UseCaseType, mode: RunMode, gate: Any) -> Requirement:
    severity: Severity = check.severity
    if check.acknowledged:
        status: Any = "acknowledged"
    elif severity is Severity.ERROR:
        status = "fail"
    elif severity is Severity.WARNING:
        status = "warn"
    else:
        status = "pass"
    return Requirement(
        id=check.code,
        applies_to=f"{kind}:{mode.value}",
        engine_code=check.code,
        source_ref=f"{'engine/uplift/checks.py' if gate == 'uplift_validate' else 'engine/stages/validate.py'} {check.code}",
        gate=gate,
        status=status,
        column=check.column,
        blocking=severity is Severity.ERROR,
        message=check.message,
    )


def _uplift_checks(
    inputs: ReadinessInputs, kind: UseCaseType
) -> tuple[UpliftValidationReport | None, tuple[Requirement, ...]]:
    from engine.uplift.checks import run_uplift_checks
    from engine.uplift.contracts import UPLIFT_VALIDATION_CODES
    from engine.uplift.flow import check_seed

    if inputs.primary_key is None or not inputs.target:
        return None, (
            Requirement(
                id="UPLIFT_CHECKS",
                applies_to=f"{kind}:train",
                engine_code=None,
                source_ref="engine/uplift/checks.py run_uplift_checks",
                gate="uplift_validate",
                status="skipped",
                skip_reason="the ID column and the outcome must be chosen first",
                blocking=True,
            ),
        )
    checked = run_uplift_checks(
        inputs.frame,
        inputs.config,
        primary_key=inputs.primary_key,
        target=inputs.target,
        upload_id=inputs.upload_id,
        acknowledged=inputs.config.validation.acknowledged,
        seed=check_seed(inputs.upload_id),
    )
    report = checked.report
    found = {check.code for check in report.checks}
    out = [
        _requirement_of(check, kind=kind, mode=RunMode.TRAIN, gate="uplift_validate")
        for check in report.checks
    ]
    out.extend(
        Requirement(
            id=code,
            applies_to=f"{kind}:train",
            engine_code=code,
            source_ref=f"engine/uplift/checks.py {code}",
            gate="uplift_validate",
            status="pass",
            blocking=True,
        )
        for code in sorted(UPLIFT_VALIDATION_CODES - found)
    )
    return report, tuple(out)


def _reference_set_requirements(inputs: ReadinessInputs) -> tuple[tuple[Requirement, ...], bool]:
    """The documents assistant trains nothing; its file is the reference set its evaluation reads."""
    from engine.generative.evaluation import SOURCE_DOC_COLUMN

    reference = inputs.config.generative.reference_set
    wanted = (
        reference.question_column,
        reference.refusal_column,
        SOURCE_DOC_COLUMN,
        reference.reference_column,
    )
    missing = [name for name in wanted if name not in inputs.frame.columns]
    columns_ok = not missing
    rows_ok = inputs.row_count >= inputs.config.validation.min_rows
    requirements = (
        Requirement(
            id="REFERENCE_SET_COLUMNS",
            applies_to="generative_rag",
            engine_code="REFERENCE_SET_INVALID",
            source_ref="engine/generative/evaluation.py _read_reference_set",
            gate="evaluate",
            status="pass" if columns_ok else "fail",
            column=None if columns_ok else missing[0],
            blocking=True,
            message="" if columns_ok else f"The reference set has no '{missing[0]}' column.",
        ),
        Requirement(
            id="REFERENCE_SET_ROWS",
            applies_to="generative_rag",
            engine_code="ROWS_TOO_FEW",
            source_ref="the use case's validation.min_rows",
            gate="evaluate",
            status="pass" if rows_ok else "fail",
            blocking=True,
            message=(
                ""
                if rows_ok
                else f"{inputs.row_count:,} questions; at least {inputs.config.validation.min_rows:,} are needed."
            ),
        ),
    )
    return requirements, columns_ok and rows_ok


def _warning_count(dry: ValidationReport | None, uplift: UpliftValidationReport | None) -> int:
    count = 0
    for report in (dry, uplift):
        if report is None:
            continue
        count += sum(
            1 for check in report.checks if check.severity is Severity.WARNING and not check.acknowledged
        )
    return count


# ---------------------------------------------------------------------------
# Approvals, acknowledgements, roles, contracts
# ---------------------------------------------------------------------------
def _approved(approvals: Sequence[RecordedApproval], *candidates: str) -> RecordedApproval | None:
    wanted = set(candidates)
    return next((a for a in approvals if a.key in wanted and a.approved_by), None)


def _approvals_required(
    inputs: ReadinessInputs,
    dry: ValidationReport | None,
    uplift: UpliftValidationReport | None,
) -> tuple[ApprovalRequired, ...]:
    """What only a person may agree to ([RDY] §4 "Always APPROVE"), and whether they did, by user id."""
    out: list[ApprovalRequired] = list(inputs.approvals_required)
    taken = {item.key for item in out}

    def add(kind: Any, key: str, *, code: str | None, path: str | None, value: Any, consequence: str) -> None:
        if key in taken:
            return
        taken.add(key)
        candidates = [key] + ([code] if code else [])
        found = _approved(inputs.approvals, *candidates)
        out.append(
            ApprovalRequired(
                kind=kind,
                key=key,
                code=code,
                path=path,
                proposed_value=value,
                consequence=consequence,
                recorded=found is not None,
                approved_by=None if found is None else found.approved_by,
                approved_at=None if found is None else found.approved_at,
            )
        )

    for report in (dry, uplift):
        if report is None:
            continue
        for check in report.checks:
            if check.severity is Severity.ERROR and check.acknowledged:
                key = f"{check.code}:{check.column}" if check.column else check.code
                add(
                    "ack_error",
                    key,
                    code=check.code,
                    path="validation.acknowledged",
                    value=key,
                    consequence="A finding that would stop the run is waved through.",
                )
    config = inputs.config
    if (
        config.prepare.pii_handling is PiiHandling.KEEP
        and dry is not None
        and any(check.code == "PII_DETECTED" for check in dry.checks)
    ):
        add(
            "pii_keep",
            "prepare.pii_handling",
            code="PII_DETECTED",
            path="prepare.pii_handling",
            value="keep",
            consequence="Columns holding personal data are kept and trained on.",
        )
    for path, value in _jsonable_overrides(inputs.resolved_overrides).items():
        approval_kind = _APPROVAL_PATHS.get(path)
        if approval_kind is not None:
            add(
                approval_kind,
                path,
                code=None,
                path=path,
                value=value,
                consequence=f"The run's setting {path} is changed from the use case's own.",
            )
    return tuple(out)


def _acknowledgements(
    config: UseCaseConfig, approvals: Sequence[RecordedApproval]
) -> tuple[Acknowledgement, ...]:
    out: list[Acknowledgement] = []
    for key in config.validation.acknowledged:
        found = _approved(approvals, key, key.split(":", 1)[0])
        out.append(
            Acknowledgement(
                key=key,
                approved_by=None if found is None else found.approved_by,
                approved_at=None if found is None else found.approved_at,
                effect="The finding is shown but does not stop the run.",
            )
        )
    return tuple(out)


def _roles(inputs: ReadinessInputs) -> ReadinessRoles:
    from engine.stages import validate
    from engine.uplift.data import detect_treatment_column

    config = inputs.config
    key_columns: tuple[str, ...] = ()
    if inputs.primary_key is not None:
        key_columns = (
            (inputs.primary_key,) if isinstance(inputs.primary_key, str) else tuple(inputs.primary_key)
        )
    source: Literal["config", "token", "rarer", "none"] = "none"
    if (
        inputs.target
        and inputs.target in inputs.frame.columns
        and config.problem_type is ProblemType.BINARY_CLASSIFICATION
    ):
        params = validate.params_from_config(config, target=inputs.target)
        label, _p, _n = validate.resolve_positive_label(inputs.frame, params)
        if label is not None:
            if config.target.positive_label is not None and label_key(label) == label_key(
                config.target.positive_label
            ):
                source = "config"
            elif label.strip().lower() in validate.POSITIVE_TOKENS:
                source = "token"
            else:
                source = "rarer"
    suppression = config.actions.suppression
    channel_columns: list[str] = []
    for channel in suppression.channels.values():
        channel_columns.extend(name for name in (channel.consent_column, channel.contactable_column) if name)
    treatment = None
    if config.problem_type is ProblemType.UPLIFT:
        treatment = detect_treatment_column([str(c) for c in inputs.frame.columns], config.uplift)
    return ReadinessRoles(
        primary_key=key_columns,
        entity_column=keys.entity_column(list(key_columns)) if len(key_columns) > 1 else None,
        target=inputs.target,
        positive_label_source=source,
        time_column=config.split.time_column if config.split.type is SplitType.TIME_BASED else None,
        group_column=config.split.group_column,
        consent_column=config.governance.consent_column,
        opt_out_column=suppression.opt_out_column if suppression.suppress_opted_out else None,
        opt_out_direction=(
            "true_keeps" if suppression.suppress_opted_out and suppression.opt_out_column else "none"
        ),
        recently_contacted_column=(
            suppression.recently_contacted_column if suppression.suppress_recently_contacted else None
        ),
        treatment_column=treatment,
        treatment_levels=tuple(config.uplift.treatment_levels),
        treatment_date_column=config.uplift.treatment_date_column,
        value_column=config.uplift.policy.value_column,
        channel_columns=tuple(channel_columns),
        excluded_columns=tuple(config.prepare.exclude_columns),
    )


def _scoring_contract(inputs: ReadinessInputs) -> ScoringContract | None:
    from engine.stages import ingest

    schema = inputs.schema
    if inputs.mode is not RunMode.SCORE or schema is None:
        return None
    present = {str(column) for column in inputs.frame.columns}
    expected = tuple(column.name for column in schema.columns)
    key_names = (schema.primary_key,) if isinstance(schema.primary_key, str) else tuple(schema.primary_key)
    known = set(expected) | set(key_names) | ({schema.target} if schema.target else set())
    changed = []
    for column in schema.columns:
        if column.name in present:
            actual = ingest.infer_column_type(inputs.frame[column.name])
            from engine.stages.validate import types_compatible

            if not types_compatible(column.inferred_type, actual):
                changed.append(column.name)
    return ScoringContract(
        model_version_id=schema.model_version_id,
        expected_columns=expected,
        extra=tuple(sorted(present - known)),
        missing=tuple(name for name in expected if name not in present),
        type_changed=tuple(changed),
    )


def _default_source(inputs: ReadinessInputs) -> SourceFacts:
    from engine.stages import ingest

    scan = inputs.scan
    return SourceFacts(
        upload_id=inputs.upload_id,
        format=None if scan is None else scan.file_format,
        rows_physical=None if scan is None else scan.rows_physical,
        rows_parsed=inputs.row_count,
        rows_dropped_by_parser=None if scan is None else scan.rows_dropped,
        profile_row_cap=ingest.profile_row_cap(inputs.config),
        sampled=inputs.row_count > len(inputs.frame),
    )


def _jsonable_overrides(overrides: Mapping[str, Any]) -> dict[str, JsonValue]:
    out: dict[str, JsonValue] = {}
    for path, value in overrides.items():
        if isinstance(value, tuple):
            value = list(value)
        out[str(path)] = value
    return out


# ---------------------------------------------------------------------------
# Loading an upload from storage
# ---------------------------------------------------------------------------
_PROFILE_FILENAME: Final[str] = "profile.json"
"""`api.routes.uploads.UPLOAD_PROFILE_FILENAME`, spelled here because `engine/` never imports `api/`."""


def _source_key(upload_id: str, file_format: str) -> str:
    """`api.routes.uploads.source_filename`: the stored file a run reads."""
    from engine.storage import upload_key

    return upload_key(upload_id, f"source.{file_format}")


def inputs_for_upload(
    storage: Storage,
    upload_id: str,
    *,
    config: UseCaseConfig,
    mode: RunMode,
    primary_key: PrimaryKey | None,
    target: str | None,
    schema: FeatureSchema | None = None,
    **case: Any,
) -> ReadinessInputs:
    """The inputs for one stored upload, read the way `POST /runs` reads it.

    The frame is the stored file capped at the profile row cap (`api.routes.runs.read_frame`); the
    stored file is scanned for lines the reader drops and, past the cap, for every key. For a prepared
    upload (one with `data_recipe.json`), the recipe and its receipt fill `prepared` and
    `fixes_applied`, and the file the recipe read is scanned for dropped lines as well, since a line
    lost there is lost in the copy too. `case` carries the case's own fields (questions, approvals,
    overrides, goal, now).
    """
    from engine.agent.contracts import (
        DATA_RECIPE_FILENAME,
        RECIPE_RECEIPT_FILENAME,
        DataRecipe,
        RecipeReceipt,
    )
    from engine.contracts import DatasetProfile
    from engine.stages import ingest
    from engine.storage import upload_key

    profile = storage.read_model(upload_key(upload_id, _PROFILE_FILENAME), DatasetProfile)
    key = _source_key(upload_id, profile.file_format)
    cap = ingest.profile_row_cap(config)
    frame = ingest.read_upload(storage, key, file_format=profile.file_format, row_cap=cap).frame
    key_columns: tuple[str, ...] = ()
    if primary_key is not None:
        key_columns = (primary_key,) if isinstance(primary_key, str) else tuple(primary_key)
    scan = scan_source(
        storage,
        key,
        file_format=profile.file_format,
        rows_parsed=profile.row_count,
        encoding=profile.encoding,
        delimiter=profile.delimiter,
        key_columns=key_columns,
        full_keys=profile.row_count > len(frame),
    )
    prepared: PreparedCopy | None = None
    fixes: tuple[FixApplied, ...] = ()
    recipe_key = upload_key(upload_id, DATA_RECIPE_FILENAME)
    if storage.exists(recipe_key):
        recipe = storage.read_model(recipe_key, DataRecipe)
        receipt_key = upload_key(upload_id, RECIPE_RECEIPT_FILENAME)
        receipt = storage.read_model(receipt_key, RecipeReceipt) if storage.exists(receipt_key) else None
        prepared = PreparedCopy(
            upload_id=upload_id,
            source_upload_id=None if receipt is None else receipt.upload_id,
            data_recipe_hash=recipe.recipe_hash,
            recipe_levels=tuple(level.value for level in recipe.levels or ()),
            max_failure_pct=recipe.max_failure_pct,
        )
        if receipt is not None:
            approved_by = {step.order: step.decided_by.value for step in recipe.steps}
            fixes = tuple(
                FixApplied(
                    step_kind=step.kind.value,
                    column=step.column,
                    params=dict(next((s.params for s in recipe.steps if s.order == step.order), {})),
                    rows_changed=step.changed,
                    rows_failed=step.failed,
                    failure_pct=round(100.0 * step.failed / step.rows, 3) if step.rows else 0.0,
                    auto_or_approved="approved" if approved_by.get(step.order) == "user" else "auto",
                )
                for step in receipt.steps
            )
            scan = _with_original_loss(storage, scan, receipt.upload_id)
    source = SourceFacts(
        upload_id=upload_id,
        file_name=profile.file_name,
        format=profile.file_format,
        encoding=profile.encoding,
        delimiter=profile.delimiter,
        size_bytes=profile.file_size_bytes,
        rows_physical=scan.rows_physical,
        rows_parsed=profile.row_count,
        rows_dropped_by_parser=scan.rows_dropped,
        fingerprint=profile.fingerprint.hash,
        profile_row_cap=cap,
        sampled=profile.row_count > len(frame),
    )
    return ReadinessInputs(
        frame=frame,
        config=config,
        mode=mode,
        primary_key=primary_key,
        target=target,
        upload_id=upload_id,
        row_count=profile.row_count,
        schema=schema,
        source=source,
        scan=scan,
        prepared=prepared,
        fixes_applied=fixes,
        **case,
    )


def _with_original_loss(storage: Storage, scan: SourceScan, original_id: str) -> SourceScan:
    """The prepared copy's scan, with the lines the reader dropped from the file the recipe read."""
    from dataclasses import replace

    from engine.contracts import DatasetProfile
    from engine.storage import upload_key

    profile_key = upload_key(original_id, _PROFILE_FILENAME)
    if not storage.exists(profile_key):
        return scan
    profile = storage.read_model(profile_key, DatasetProfile)
    if profile.file_format != "csv":
        return scan
    original = scan_source(
        storage,
        _source_key(original_id, profile.file_format),
        file_format="csv",
        rows_parsed=profile.row_count,
        encoding=profile.encoding,
        delimiter=profile.delimiter,
        full_keys=False,
    )
    return replace(
        scan,
        file_format="csv",
        rows_physical=None if original.rows_physical is None else original.rows_physical,
        rows_parsed=original.rows_parsed,
        lost_lines=original.lost_lines,
        parse_error=original.parse_error,
    )


# ---------------------------------------------------------------------------
# Storage: in the case, and beside the run's data_recipe.json
# ---------------------------------------------------------------------------
def _safe(part: str, what: str) -> str:
    if not _ID.fullmatch(part):
        raise ValueError(f"{what} {part!r} is not a valid id")
    return part


def readiness_key(*, client_id: str, case_id: str, goal: str | None, report_hash: str) -> str:
    """`cases/<client>/<case>/readiness/<goal>_<hash>.json` (the design §10.3)."""
    name = _safe(goal, "goal") if goal else "no_goal"
    return (
        f"cases/{_safe(client_id, 'client id')}/{_safe(case_id, 'case id')}/readiness/"
        f"{name}_{_safe(report_hash[:16], 'report hash')}.json"
    )


def store_readiness(storage: Storage, report: DataReadinessReport, *, client_id: str, case_id: str) -> str:
    """Store the report in its case; returns the key. The same report always lands on the same key."""
    key = readiness_key(
        client_id=client_id, case_id=case_id, goal=report.goal, report_hash=report.report_hash
    )
    storage.write_model(key, report)
    return key


def attach_readiness_to_run(storage: Storage, report: DataReadinessReport, run_id: str) -> str:
    """Copy the report into the run directory, beside `data_recipe.json`; returns the key."""
    from engine.storage import run_key

    key = run_key(run_id, READINESS_RUN_FILENAME)
    storage.write_model(key, report)
    return key


# ---------------------------------------------------------------------------
# The tool: a READ tool of exposure `aggregate`, for the Plan O registry (M232)
# ---------------------------------------------------------------------------
class CheckReadinessArgs(StrictBase):
    """`check_readiness`'s arguments: the case supplies the file, the roles and the settings."""

    goal: str | None = Field(default=None, description="The goal brief to check against, when there is one.")
    mode: Literal["train", "score"] = Field(default="train", description="Training or scoring.")


class ReadinessToolContext(Protocol):
    """What the registry's context must offer this tool: the inputs for the case it serves."""

    def readiness_inputs(self, args: CheckReadinessArgs) -> ReadinessInputs: ...


def summarise(report: DataReadinessReport) -> ReadinessSummary:
    """The aggregate view: verdict, probes, failing requirement codes and counts - no cell value."""
    return ReadinessSummary(
        report_hash=report.report_hash,
        use_case_id=report.use_case_id,
        use_case_type=report.use_case_type,
        mode=report.mode,
        verdict=report.verdict,
        verdict_reason=_aggregate_reason(report),
        dry_run_passed=report.dry_run_passed,
        probes=tuple(ProbeSummary(code=p.code, status=p.status, gate=p.gate) for p in report.probes),
        failing_requirements=tuple(sorted({r.id for r in report.requirements if r.status == "fail"})),
        open_blocking_questions=sum(1 for q in report.open_questions if q.blocking and not q.answered),
        approvals_waiting=sum(1 for a in report.approvals_required if not a.recorded),
        rows_after_filters=(
            None if report.post_filter_counts is None else report.post_filter_counts.rows_after_filters
        ),
    )


def _aggregate_reason(report: DataReadinessReport) -> str:
    """The verdict in codes only: probe messages name columns, which an aggregate view does not repeat."""
    if report.verdict == "not_ready":
        named = sorted({r.id for r in report.requirements if r.status == "fail"}) + list(
            report.failing_probes
        )
        return "Not ready: " + ", ".join(named)
    return report.verdict_reason.split(" (", 1)[0]


@dataclass(frozen=True)
class ReadinessToolSpec:
    """The registry entry for `check_readiness` (the design §5.5 #69), in the shape the registry needs."""

    name: str
    kind: Literal["read"]
    exposure: Literal["aggregate"]
    description: str
    input_model: type[CheckReadinessArgs]
    output_model: type[ReadinessSummary]
    run: Callable[[CheckReadinessArgs, ReadinessToolContext], ReadinessSummary]
    mirrors: tuple[str, ...] = ("engine.agent.checks.check_plan", "engine.uplift.checks.run_uplift_checks")
    probe_codes: tuple[str, ...] = PROBE_CODES


def _run_tool(args: CheckReadinessArgs, ctx: ReadinessToolContext) -> ReadinessSummary:
    return summarise(check_readiness(ctx.readiness_inputs(args)))


def check_readiness_tool() -> ReadinessToolSpec:
    """`check_readiness` as a READ tool: rules only, no AI, aggregate output."""
    return ReadinessToolSpec(
        name=CHECK_READINESS_TOOL,
        kind="read",
        exposure="aggregate",
        description=(
            "Says whether the prepared file will train (or score) to the end: the run's own checks plus "
            "fifteen probes of the steps after them. Returns the verdict and the codes, never a value."
        ),
        input_model=CheckReadinessArgs,
        output_model=ReadinessSummary,
        run=_run_tool,
    )


def register_readiness_tool(register: Callable[[ReadinessToolSpec], object]) -> ReadinessToolSpec:
    """Hand `check_readiness` to a registry's `register`; returns the spec it registered."""
    spec = check_readiness_tool()
    register(spec)
    return spec
