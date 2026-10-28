"""The remaining wrapper tools and `explain` (Plan O M236, DEC-1806; the design §5.5).

Functions the product already has, made Data Doctor tools with typed inputs and outputs, and one new tool:

| # | Tool | Kind | Wraps |
|---|---|---|---|
| 3 | `propose_intake` | PROPOSE | the stored intake report (`engine.intake.run.intake_file`'s decisions), frozen as one item |
| 6 | `preview_connection` | READ | a saved connection's masked preview (`Connector.preview`, `POST /connections/{id}/preview`) |
| 7 | `propose_import` | PROPOSE | an import from a saved connection (`POST /connections/{id}/import`), proposed, never run |
| 8 | `check_refresh` | READ | which tables a recipe's next build reads, and which a refresh reads again (`latest_recipe_inputs`) |
| 9 | `preflight` | READ | the pre-flight checker on the case's files (`engine.pilot.preflight.run_preflight`) |
| 31 | `second_opinion` | PROPOSE | the AI's rerank and leak review (`engine.agent.assist.assist_session`) |
| 39 | `explain` | READ | new: the claims verifier with data claim types (`engine.doctor.explain`) |
| 41 | `propose_role` | PROPOSE | a file's role (`engine.onboarding.sources.detect_roles`, the diagnosis's rule) |
| 42 | `propose_key` | PROPOSE | the ID column the rules advisor chose (`engine.agent.advisor.advise`) |
| 43 | `propose_mapping` | PROPOSE | the suggested mapping (`engine.onboarding.mapping.suggested_mapping_spec`) |
| 65 | `find_personal_data` | READ | `engine.pii.detect_pii` and `free_text_pii` per column |
| 66 | `check_leaks` | READ | one leak verdict over the run's check, the first look, the build report and the builder |
| 68 | `check_uplift_data` | READ | `engine.uplift.checks.run_uplift_checks`'s report |
| 73 | `read_run_checks` | READ | `engine.run_checks.things_checked` (with `RESULT_SUSPICIOUSLY_GOOD`) |
| 76 | `request_approval` | ASK | the APPROVE-class rule of M233's policy and Plan K's proposal expiry: what the person must approve |

**A wrapper's output is the wrapped function's**, so it cannot disagree with the screen that calls the function
directly (`tests/unit/doctor/test_tools_wrappers.py` compares them on fixtures). Where the function's own result
carries a time of the call or a server path, the wrapper leaves that one field out or replaces it (named below).

**Every PROPOSE wrapper lands pending.** Its output carries who proposed it (`source`), whether it would be offered
ticked (`ticked`) and how sure the rules are (`confidence`); M233's policy reads them (`policy._read_pending`) and
its rule sets (`data.propose_role`, ...) refuse an item offered ticked unless the rules are sure, and anything the
AI proposed ticked. `propose_role`, `propose_mapping` and `propose_import` are always `check`; nothing is ticked.
Nothing here applies anything: an import is run by the import route, a mapping saved by the mapping route, after
the person decides.

The tools serve the rules and the screen (`callers`), like wave 1's; the AI reaches them through the visit.
Every one is `session-only` (the catalogue), so each needs an Analyst, as M232's row-level tools do; a Viewer may
call none of them.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agentic.claims import Narrative
from engine.config import StrictBase
from engine.connections.base import Preview
from engine.contracts import ValidationCheck
from engine.doctor.registry import (
    BY_EXPOSURE,
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
    Need,
)
from engine.intake.contracts import IntakeReport
from engine.onboarding.specs import MappingColumn, RoleCandidate
from engine.pilot.document import VerdictState
from engine.pilot.preflight import PreflightResult
from engine.run_checks import RunChecks
from engine.uplift.contracts import UpliftValidationReport

if TYPE_CHECKING:
    from engine.doctor.diagnose import LoadedFile

__all__ = [
    "M236_TOOLS",
    "PREFLIGHT_FOLDER",
    "ProposedRoles",
    "preflight_file_name",
    "register_wrappers",
    "whole_marker",
]

M236_TOOLS: Final[tuple[str, ...]] = (
    "propose_intake",
    "preview_connection",
    "propose_import",
    "check_refresh",
    "preflight",
    "second_opinion",
    "explain",
    "propose_role",
    "propose_key",
    "propose_mapping",
    "find_personal_data",
    "check_leaks",
    "check_uplift_data",
    "read_run_checks",
    "request_approval",
)
"""Every tool this module registers, in catalogue order."""

RULES_AND_SCREEN: Final[frozenset[str]] = frozenset({"rules", "person"})
_ID: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
_EVIDENCE: Final[str] = r"^ev_[a-p]{16}$"
PREFLIGHT_FOLDER: Final[str] = "the case's files"
"""What `preflight` names as its folder: the files are the case's, written to a temporary folder for the check."""
LEAK_SOURCES: Final[tuple[str, ...]] = ("run_checks", "first_look", "build_report", "builder", "ai_reviewer")
"""The five places a leak is found today (the design §5.5 #66), in the order `check_leaks` reports them."""
BUILD_LEAK_CODES: Final[frozenset[str]] = frozenset(
    {"FUTURE_EVENTS_LEAKED", "ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED", "LEAKAGE_SUSPECTED"}
)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _policy(purpose: str, action: str) -> RoutePolicy:
    return RoutePolicy(role=Role.ANALYST, action=f"doctor.{action}", purpose=purpose)


class Pending(StrictBase):
    """What every PROPOSE wrapper's output carries for the policy (M233): it lands pending."""

    source: Literal["rules", "ai"] = "rules"
    ticked: bool = False
    confidence: Literal["sure", "check"] = "check"
    state: Literal["pending"] = "pending"


# ---------------------------------------------------------------------------
# Shared helpers (the case's files, the use case, the roles catalogue)
# ---------------------------------------------------------------------------
def _loaded(ctx: DataToolContext, upload_id: str | None) -> LoadedFile:
    from engine.doctor.tools import _loaded as loaded

    return loaded(ctx, upload_id)


def _config(ctx: DataToolContext) -> Any:
    from engine.doctor.tools import _config as config

    return config(ctx)


def _roles(ctx: DataToolContext) -> Any:
    from engine.doctor.tools import _roles as roles

    return roles(ctx)


def _file_or_files(ctx: DataToolContext) -> bool:
    return ctx.agent is not None or bool(ctx.files)


def _source_profile(ctx: DataToolContext, loaded: LoadedFile) -> Any:
    """The onboarding profile of one case file: what `diagnose_file` and `find_file_role` read."""
    from engine.onboarding.sources import profile_source

    return profile_source(
        loaded.frame,
        _config(ctx),
        source_id=loaded.upload_id,
        client_id="data-doctor",
        file_name=loaded.file_name,
        row_count=loaded.profile.row_count,
        roles=_roles(ctx),
    )


def _fitted(model: Any, fields: tuple[str, ...]) -> Any:
    from engine.doctor.tools import _fitted as fitted

    return fitted(model, fields)


# ---------------------------------------------------------------------------
# #41 propose_role
# ---------------------------------------------------------------------------
class RoleArgs(_Args):
    upload_id: str | None = Field(
        default=None,
        max_length=128,
        description="One of the case's files; the file being set up when omitted.",
    )


class RoleProposal(Pending):
    upload_id: str
    role: str | None = Field(description="The role proposed (configs/roles.yaml); null when nothing fits.")
    reason: str = Field(description="The detector's signals for it, strongest first; empty with no role.")
    candidates: tuple[RoleCandidate, ...] = Field(
        description="Every role the file could play, most likely first (`detect_roles`); the least likely are "
        "left out when they do not fit."
    )
    candidates_total: int


def _propose_role(parsed: RoleArgs, ctx: DataToolContext) -> RoleProposal:
    """The diagnosis's rule (`diagnose_file`): the strongest role, when its signal is real (`KIND_MIN_CONFIDENCE`)."""
    from engine.doctor.diagnose import KIND_MIN_CONFIDENCE

    loaded = _loaded(ctx, parsed.upload_id)
    candidates = tuple(_source_profile(ctx, loaded).role_candidates)
    best = candidates[0] if candidates else None
    role = best.role if best is not None and best.confidence >= KIND_MIN_CONFIDENCE else None
    found = RoleProposal(
        upload_id=loaded.upload_id,
        role=role,
        reason="; ".join(best.reasons) if best is not None and role is not None else "",
        candidates=candidates,
        candidates_total=len(candidates),
    )
    fitted: RoleProposal = _fitted(found, ("candidates",))
    return fitted


class ProposedRoles:
    """M237's `RoleProposer` answered by the registry's `propose_role` (M236): what `plan_build` reads.

    Each file's role is asked through the registry, so the policy decides it like every other proposal and the
    evidence is the tool's own (the cross-branch request of the wave-1 integration).
    """

    def __init__(self, ctx: DataToolContext, registry: DataToolRegistry | None = None) -> None:
        self._ctx = ctx
        self._registry = registry

    def propose_role(self, upload_id: str) -> Any:
        from engine.doctor.build_driver import RoleSuggestion
        from engine.doctor.registry import data_registry

        registry = self._registry or data_registry()
        output = registry.call("propose_role", {"upload_id": upload_id}, self._ctx).output
        role = output.get("role")
        return None if role is None else RoleSuggestion(role=str(role), reason=str(output.get("reason", "")))


# ---------------------------------------------------------------------------
# #42 propose_key
# ---------------------------------------------------------------------------
class KeyArgs(_Args):
    target: str | None = Field(default=None, max_length=512, description="The outcome column, if decided.")


class KeyProposal(Pending):
    column: str | None = Field(description="The ID column the rules propose; null when none can be.")
    title: str = ""
    reason: str = ""
    stop_reason: str | None = Field(description="Why no column can be the ID, when none can.")
    unique_columns: tuple[str, ...] = Field(
        description="Columns the profile found unique and never empty, best first (at most 10)."
    )


def _propose_key(parsed: KeyArgs, ctx: DataToolContext) -> KeyProposal:
    """The rules advisor's ID column (`advise`), as one pending item: offered ticked only when `sure`."""
    from engine.agent.advisor import ROLE_PRIMARY_KEY, advise
    from engine.agent.contracts import AgentConfidence, ProposalKind, ProposalState

    agent = ctx.require_agent()
    advice = advise(agent, target=agent.resolve(parsed.target) if parsed.target else None)
    chosen = next(
        (p for p in advice.proposals if p.kind is ProposalKind.ROLE and p.path == ROLE_PRIMARY_KEY), None
    )
    sure = chosen is not None and chosen.confidence is AgentConfidence.SURE
    return KeyProposal(
        column=advice.primary_key,
        title="" if chosen is None else chosen.title,
        reason="" if chosen is None else chosen.reason,
        stop_reason=advice.stop_reason if advice.primary_key is None else None,
        unique_columns=tuple(agent.profile.primary_key_candidates[:10]),
        confidence="sure" if sure else "check",
        ticked=bool(sure and chosen is not None and chosen.state is ProposalState.ACCEPTED),
    )


# ---------------------------------------------------------------------------
# #43 propose_mapping
# ---------------------------------------------------------------------------
class MappingArgs(_Args):
    upload_id: str | None = Field(default=None, max_length=128, description="One of the case's files.")
    role: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="The file's role (configs/roles.yaml); `propose_role`'s when omitted.",
    )


class MappingProposal(Pending):
    upload_id: str
    role: str
    use_case: str
    columns: tuple[MappingColumn, ...] = Field(
        description="Each column the builder would read, and how (best source column each, nothing twice); the "
        "last are left out when they do not fit."
    )
    columns_total: int
    unmapped_source: tuple[str, ...] = Field(description="Columns of the file left out, sorted.")
    unmapped_total: int
    missing_required: tuple[str, ...] = Field(description="Required columns nothing fills, sorted.")


def _propose_mapping(parsed: MappingArgs, ctx: DataToolContext) -> MappingProposal:
    """`suggested_mapping_spec` for one file and role: pending, `check`, never ticked (the screen decides)."""
    from engine.onboarding.mapping import suggested_mapping_spec

    loaded = _loaded(ctx, parsed.upload_id)
    role = parsed.role
    if role is None:
        role = _propose_role(RoleArgs(upload_id=loaded.upload_id), ctx).role
    if role is None:
        raise DataToolError(
            "TOOL_INPUT_INVALID",
            "Data Doctor cannot tell what kind of file this is; name its role.",
            status=422,
        )
    if role not in _roles(ctx).roles:
        raise DataToolError("TOOL_INPUT_INVALID", f"There is no role called {role!r}.", status=422)
    config = _config(ctx)
    spec = suggested_mapping_spec(
        _source_profile(ctx, loaded),
        config,
        role=role,
        use_case=config.id,
        mapping_id="map-" + hashlib.sha256(f"{loaded.upload_id}\n{role}".encode()).hexdigest()[:12],
    )
    found = MappingProposal(
        upload_id=loaded.upload_id,
        role=role,
        use_case=spec.use_case,
        columns=spec.columns,
        columns_total=len(spec.columns),
        unmapped_source=spec.unmapped_source,
        unmapped_total=len(spec.unmapped_source),
        missing_required=spec.missing_required,
    )
    fitted: MappingProposal = _fitted(found, ("unmapped_source", "columns"))
    return fitted


# ---------------------------------------------------------------------------
# #3 propose_intake
# ---------------------------------------------------------------------------
class IntakeDecisions(StrictBase):
    """What intake decided about a file, frozen so it can be read the same way next time (DEC-1501g)."""

    source_format: str
    sheet: str | None
    delimiter: str | None
    encoding: str | None
    header_row: int
    header_rows: int
    rows_above_header: int
    footer_rows: int
    empty_rows: int
    columns_dropped: int
    turned_off: bool


class IntakeProposal(Pending):
    upload_id: str
    rewritten: bool = Field(
        description="True when intake changed how the file is read; false: read as it is."
    )
    decisions: IntakeDecisions | None = Field(description="Intake's decisions; null when it changed nothing.")
    notes: tuple[str, ...] = Field(description="What intake did, in plain sentences (`notes_for`).")


def _propose_intake(parsed: RoleArgs, ctx: DataToolContext) -> IntakeProposal:
    """The upload's `intake.json` (written only when intake changed something) as one item to keep."""
    from engine.intake.run import INTAKE_REPORT_FILENAME, notes_for
    from engine.storage import StorageError, upload_key

    upload_id = parsed.upload_id or ctx.require_agent().upload_id
    storage = ctx.storage
    if storage is None:  # unreachable through the registry: `needs` makes the tool unavailable
        raise DataToolError("TOOL_UNKNOWN", "propose_intake needs storage.", status=404)
    if (
        ctx.files
        and upload_id not in {f.upload_id for f in ctx.files}
        and (ctx.agent is None or ctx.agent.upload_id != upload_id)
    ):
        raise DataToolError("TOOL_INPUT_INVALID", "That file is not in this case.", status=422)
    try:
        report: IntakeReport | None = storage.read_model(
            upload_key(upload_id, INTAKE_REPORT_FILENAME), IntakeReport
        )
    except (StorageError, FileNotFoundError, ValueError):
        report = None
    if report is None:
        return IntakeProposal(
            upload_id=upload_id, rewritten=False, decisions=None, notes=(), confidence="sure"
        )
    decisions = IntakeDecisions(
        source_format=report.source_format,
        sheet=report.sheet,
        delimiter=report.delimiter,
        encoding=report.encoding,
        header_row=report.header_row,
        header_rows=report.header_rows,
        rows_above_header=report.rows_above_header,
        footer_rows=report.footer_rows,
        empty_rows=report.empty_rows,
        columns_dropped=report.columns_dropped,
        turned_off=report.turned_off,
    )
    return IntakeProposal(
        upload_id=upload_id,
        rewritten=report.applied,
        decisions=decisions,
        notes=report.notes or notes_for(report),
        confidence="sure",
    )


# ---------------------------------------------------------------------------
# #6 preview_connection, #7 propose_import
# ---------------------------------------------------------------------------
class SelectionArgs(_Args):
    connection_id: str = Field(pattern=r"^c_[0-9a-f]{12}$", description="A saved connection.")
    path: str | None = Field(default=None, max_length=1024, description="A store's object key.")
    schema_name: str | None = Field(default=None, max_length=256, description="A database's schema.")
    table: str | None = Field(default=None, max_length=256, description="A database's table or view.")


class ConnectionPreview(StrictBase):
    connection_id: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...] = Field(
        description="The first rows, every cell masked (the route's own); the last rows are left out when they "
        "do not fit."
    )
    rows_total: int = Field(
        description="Rows the preview read; more than `rows` lists when some did not fit."
    )
    note: str | None = None


def _connector_call(ctx: DataToolContext, parsed: SelectionArgs) -> tuple[Any, Any, Any, Any]:
    """`(record, connector, secrets, selection)`, refused as the connection routes refuse."""
    from engine.connections import registry as connectors
    from engine.connections.base import ConnectorError, Selection

    store = ctx.connections
    if store is None:  # unreachable through the registry: `needs` makes the tool unavailable
        raise DataToolError("TOOL_UNKNOWN", "This tool needs the saved connections.", status=404)
    try:
        record = store.get(parsed.connection_id)
        conn = connectors.connector(record.kind)
        if not conn.available():
            raise DataToolError(
                "CONNECTION_NEEDS_ADDON",
                f"{conn.info().label} needs an add-on that is not installed on this server.",
                status=409,
            )
        secrets = store.secrets(record)
    except ConnectorError as exc:
        raise DataToolError(exc.code, f"{exc.message} {exc.fix or ''}".strip(), status=exc.status) from exc
    selection = Selection(path=parsed.path, schema_name=parsed.schema_name, table=parsed.table)
    return record, conn, secrets, selection


def _bounded_preview(ctx: DataToolContext, parsed: SelectionArgs) -> tuple[Any, Any, Any, Preview]:
    """`(record, connector, selection, preview)`: the preview runs under the connection routes' own gate
    (`engine.connections.limits.CALLS`, at most four calls to clients' systems at once), and a full gate is
    `429 CONNECTION_BUSY` straight away, as `POST /connections/{id}/preview` answers."""
    from engine.connections import limits
    from engine.connections.base import ConnectorError

    record, conn, secrets, selection = _connector_call(ctx, parsed)
    try:
        preview: Preview = limits.bounded_call(
            limits.CALLS, lambda: conn.preview(record.config, secrets, selection)
        )
    except limits.ConnectionBusyError as exc:
        raise DataToolError("CONNECTION_BUSY", limits.BUSY_MESSAGE, status=429) from exc
    except ConnectorError as exc:
        raise DataToolError(exc.code, f"{exc.message} {exc.fix or ''}".strip(), status=exc.status) from exc
    return record, conn, selection, preview


def _preview(parsed: SelectionArgs, ctx: DataToolContext) -> Preview:
    return _bounded_preview(ctx, parsed)[3]


_MARKER: Final[re.Pattern[str]] = re.compile(r"\[REDACTED:[a-z_]+\]")


def whole_marker(cell: str) -> str:
    """A preview cell in which the connector masked personal data, shown as its markers alone: the connector
    masks what `engine.pii` finds, which can leave part of an ID around a marker (`27[REDACTED:pan]1ZV` of a
    GSTIN), and a part of an ID is still the ID. A cell with no marker is unchanged."""
    found = _MARKER.findall(cell)
    return " ".join(found) if found else cell


def _preview_connection(parsed: SelectionArgs, ctx: DataToolContext) -> ConnectionPreview:
    preview = _preview(parsed, ctx)
    found = ConnectionPreview(
        connection_id=parsed.connection_id,
        columns=preview.columns,
        rows=tuple(tuple(whole_marker(cell) for cell in row) for row in preview.rows),
        rows_total=len(preview.rows),
        note=preview.note,
    )
    fitted: ConnectionPreview = _fitted(found, ("rows",))
    return fitted


class ImportArgs(SelectionArgs):
    role: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$", description="The table's role, when known."
    )


class ImportProposal(Pending):
    connection_id: str
    kind: str = Field(description="The connection's kind, e.g. `s3` or `postgres`.")
    path: str | None
    schema_name: str | None
    table: str | None
    file_format: str = Field(description="How the table will be read: csv, parquet or xlsx.")
    columns: tuple[str, ...] = Field(description="The table's columns, as its preview shows them.")
    role: str | None
    summary: str = Field(description="What approving does, in one plain sentence.")


def _propose_import(parsed: ImportArgs, ctx: DataToolContext) -> ImportProposal:
    """Read nothing but the preview: the import itself is the import route's, after the person says yes."""
    from engine.connections.base import ConnectorError

    record, conn, selection, preview = _bounded_preview(ctx, parsed)
    try:
        file_format = str(conn.file_format(selection))
    except ConnectorError as exc:
        raise DataToolError(exc.code, f"{exc.message} {exc.fix or ''}".strip(), status=exc.status) from exc
    what = parsed.path or ".".join(p for p in (parsed.schema_name, parsed.table) if p) or "the table"
    return ImportProposal(
        connection_id=record.connection_id,
        kind=record.kind,
        path=parsed.path,
        schema_name=parsed.schema_name,
        table=parsed.table,
        file_format=file_format,
        columns=preview.columns,
        role=parsed.role,
        summary=f"Import {what} from the connection '{record.name}' as a new file of this case.",
    )


# ---------------------------------------------------------------------------
# #8 check_refresh
# ---------------------------------------------------------------------------
class RefreshArgs(_Args):
    spec_id: str = Field(pattern=_ID, description="A saved recipe (onboarding spec) of this client.")


class RefreshTable(StrictBase):
    source_id: str
    role: str | None
    file_name: str
    rows: int
    created_at: AwareDatetime
    newer: bool = Field(
        description="True when it replaces the recipe's own table (a newer one of that role)."
    )
    bound: bool = Field(description="True when it came from a saved connection, so a refresh reads it again.")
    connection_id: str | None = None
    connection_kind: str | None = None
    pick: Literal["newest", "fixed"] | None = None


class RefreshCheck(StrictBase):
    spec_id: str
    tables: tuple[RefreshTable, ...] = Field(
        description="The tables the next build reads, entity table first."
    )
    bound_total: int = Field(description="Tables a refresh would read again from their connection.")
    newer_total: int = Field(description="Tables replaced by a newer table of the same role.")


def _check_refresh(parsed: RefreshArgs, ctx: DataToolContext) -> RefreshCheck:
    """`latest_recipe_inputs`: read only, nothing fetched (`refresh_bound_sources` fetches at the build)."""
    from engine.clients import ClientStoreError
    from engine.scheduling.firing import FiringError, latest_recipe_inputs

    store = ctx.client_store
    if store is None:  # unreachable through the registry: `needs` makes the tool unavailable
        raise DataToolError("TOOL_UNKNOWN", "check_refresh needs the onboarding registry.", status=404)
    try:
        spec = store.get_spec(parsed.spec_id)
    except ClientStoreError as exc:
        raise DataToolError("TOOL_INPUT_INVALID", "There is no such recipe.", status=404) from exc
    if ctx.case is not None and spec.client_id != ctx.case.client_id:
        raise DataToolError("TOOL_INPUT_INVALID", "There is no such recipe.", status=404)
    try:
        inputs = latest_recipe_inputs(store, spec)
    except FiringError as exc:
        raise DataToolError(exc.code, exc.message, status=422) from exc
    replaced = {source.source_id for source in inputs.sources} - {
        spec.entity_source_id,
        *spec.event_source_ids,
    }
    tables = tuple(
        RefreshTable(
            source_id=source.source_id,
            role=source.role,
            file_name=source.file_name,
            rows=source.rows,
            created_at=source.created_at,
            newer=source.source_id in replaced,
            bound=source.binding is not None,
            connection_id=None if source.binding is None else source.binding.connection_id,
            connection_kind=None if source.binding is None else source.binding.kind,
            pick=None if source.binding is None else source.binding.pick,
        )
        for source in inputs.sources
    )
    return RefreshCheck(
        spec_id=spec.spec_id,
        tables=tables,
        bound_total=sum(table.bound for table in tables),
        newer_total=sum(table.newer for table in tables),
    )


# ---------------------------------------------------------------------------
# #9 preflight
# ---------------------------------------------------------------------------
class PreflightArgs(_Args):
    use_cases: tuple[str, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=4,
        description="Up to 4 configured use cases; the pilot's default ones when omitted.",
    )


class PreflightOutput(StrictBase):
    result: PreflightResult = Field(
        description="The pre-flight check (`run_preflight`); its folder is the case's files, and the last "
        "findings, then the last tables, are left out when they do not fit."
    )
    verdict: VerdictState = Field(description="ready, warnings or not_ready, as the pre-flight page says.")
    findings_total: int
    tables_total: int


_NAME_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z]+")


def preflight_file_name(file_name: str, vocabulary: frozenset[str], taken: set[str]) -> str:
    """The name a case file is checked under: only the words of its own name that name a table (the role
    catalogue's `name_tokens`: `customers`, `bills`, ...), so the pre-flight reads the table's kind from it as
    it would on the client's laptop, and no other word of a file name - a person's name, an e-mail - can reach
    a sentence it writes. `file.csv` when no word names a table; unique in the folder."""
    words = [word for word in _NAME_WORD.findall(file_name.rsplit("/", 1)[-1].lower()) if word in vocabulary]
    stem = "_".join(dict.fromkeys(words)) or "file"
    name, n = f"{stem}.csv", 1
    while name in taken:
        n += 1
        name = f"{stem}-{n}.csv"
    taken.add(name)
    return name


def _preflight(parsed: PreflightArgs, ctx: DataToolContext) -> PreflightOutput:
    """The pre-flight checker on the case's files, as a client's analyst would run it on the same files.

    The frames are written as CSV to a temporary folder (nothing is stored) and read back by `run_preflight`
    itself, each named by the words of its name that name a table (:func:`preflight_file_name`); `folder` is
    reported as :data:`PREFLIGHT_FOLDER`, never a server path.
    """
    import tempfile
    from pathlib import Path

    from engine.config import ConfigError
    from engine.pilot.preflight import run_preflight

    files = list(ctx.files) or [_loaded(ctx, None)]
    with tempfile.TemporaryDirectory(prefix="doctor-preflight-") as folder:
        taken: set[str] = set()
        vocabulary = frozenset(token for spec in _roles(ctx).roles.values() for token in spec.name_tokens)
        for loaded in files:
            name = preflight_file_name(loaded.file_name, vocabulary, taken)
            loaded.frame.to_csv(Path(folder) / name, index=False)
        try:
            result = run_preflight([Path(folder)], parsed.use_cases, root=ctx.root(), now=ctx.clock())
        except ConfigError as exc:
            raise DataToolError(
                "TOOL_INPUT_INVALID", "A use case named here could not be read.", status=422
            ) from exc
    result = result.model_copy(update={"folder": PREFLIGHT_FOLDER})
    return _fit_preflight(
        PreflightOutput(
            result=result,
            verdict=result.verdict,
            findings_total=len(result.findings),
            tables_total=len(result.tables),
        )
    )


def _fit_preflight(output: PreflightOutput) -> PreflightOutput:
    from engine.doctor.registry import ROW_LEVEL_BUDGET, output_size

    result = output.result
    for name in ("findings", "tables"):
        while (
            getattr(result, name)
            and output_size(
                Exposure.SESSION_ONLY, output.model_copy(update={"result": result}).model_dump(mode="json")
            )
            > ROW_LEVEL_BUDGET
        ):
            result = result.model_copy(update={name: getattr(result, name)[:-1]})
    return output.model_copy(update={"result": result})


# ---------------------------------------------------------------------------
# #31 second_opinion
# ---------------------------------------------------------------------------
class SecondOpinionQuestion(StrictBase):
    question_id: str
    text: str
    options: tuple[str, ...]
    blocking: bool


class SecondOpinion(Pending):
    llm_calls: int = Field(description="Calls made to the AI service.")
    reordered: tuple[str, ...] = Field(description="Roles whose candidates the AI put in another order.")
    leak_questions: int = Field(
        description="Columns the AI asked about as possibly recorded after the outcome."
    )
    questions: tuple[SecondOpinionQuestion, ...] = Field(description="The questions it added, pending.")
    blocked_by: str | None = Field(description="Why it stopped early: budget, unavailable, prompt_too_large.")


def _agentic_on(ctx: DataToolContext) -> bool:
    """`agentic.enabled` under the context's configuration root; an `agentic.yaml` that does not read leaves it
    off, as `POST /uploads/{id}/agent-session/assist` does (DEC-1409 (g))."""
    from engine.agentic.config import agentic_enabled
    from engine.agentic.errors import AgenticError

    try:
        return agentic_enabled(ctx.root())
    except AgenticError:
        return False


def _second_opinion(_parsed: _Args, ctx: DataToolContext) -> SecondOpinion:
    """`assist_session`: the AI reranks candidates the rules listed and asks about possible leaks. Every
    question it adds is pending; it decides, ticks and applies nothing.

    It works from the case: the rules read the case's file once (`rules_session` on the file's context with the ID
    and outcome the case decided, a working record that is never stored) and the AI's questions are returned for the visit to put to the person, so no
    Guided-setup session is read or written. It refuses what `POST /uploads/{id}/agent-session/assist` refused:
    with `agentic.enabled` off the tool is unavailable and a direct call is `404 AGENTIC_DISABLED`, before any AI
    call. The transcript the AI call leaves is not kept. The AI's usage stays on `ctx.meter`: the caller that
    supplied the meter records `meter.usage()`."""
    from engine.agent.assist import assist_session
    from engine.agent.proposals import rules_session
    from engine.agentic.claims import guardrails_for
    from engine.doctor.case import CaseError, CaseStore
    from engine.doctor.decisions import decisions

    if ctx.case is None:  # unreachable through the registry: `needs` makes it unavailable
        raise DataToolError("TOOL_UNKNOWN", "second_opinion needs a case.", status=404)
    if not _agentic_on(ctx):  # unreachable through the registry: `available` makes it unavailable
        raise DataToolError(
            "AGENTIC_DISABLED",
            "Second opinions on the columns are switched off for this deployment (agentic.enabled is false).",
            status=404,
        )
    agent = ctx.require_agent()
    key, outcome = ctx.primary_key, None
    if ctx.storage is not None:
        try:
            decided = decisions(CaseStore(ctx.storage).get(ctx.case.client_id, ctx.case.case_id))
        except CaseError:  # a case that does not read leaves the rules to their own choice
            decided = None
        if decided is not None:
            key = decided.column_of_upload("key", agent.upload_id) or key
            outcome = decided.outcome_of_upload(agent.upload_id)
    session = rules_session(
        agent, session_id="case-second-opinion", clock=ctx.clock, primary_key=key, target=outcome
    )
    before = {question.question_id for question in session.questions}
    result = assist_session(
        agent,
        session,
        meter=ctx.meter,
        guardrails=ctx.guardrails or guardrails_for(ctx.root()),
        config_root=ctx.root(),
        clock=ctx.clock,
    )
    added = [q for q in result.session.questions if q.question_id not in before]
    return SecondOpinion(
        source="ai",
        llm_calls=result.llm_calls,
        reordered=result.reordered,
        leak_questions=result.leak_questions,
        questions=tuple(
            SecondOpinionQuestion(
                question_id=q.question_id,
                text=q.text,
                options=tuple(o.label for o in q.options),
                blocking=q.blocking,
            )
            for q in added
        ),
        blocked_by=result.blocked_by,
    )


# ---------------------------------------------------------------------------
# #39 explain
# ---------------------------------------------------------------------------
class ExplainArgs(_Args):
    evidence_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=20,
        description="Results of this case's earlier tool calls to explain (their content-hash evidence ids).",
    )


class Explanation(StrictBase):
    narrative: Narrative = Field(description="The checked sentences: the AI's, or the rules' when it failed.")
    evidence_ids: tuple[str, ...]
    facts_total: int = Field(description="Measured facts the sentences could state.")


def _explain(parsed: ExplainArgs, ctx: DataToolContext) -> Explanation:
    """The case's evidence in words: the AI's claim-checked narrative, else the rules' (`engine.doctor.explain`)."""
    from engine.doctor.explain import (
        ExplainRequest,
        data_evidence_index,
        meter_narrator,
        verify_data_narrative,
    )

    known = {result.evidence_id: result for result in ctx.evidence}
    wanted = list(dict.fromkeys(parsed.evidence_ids))
    unknown = [evidence_id for evidence_id in wanted if evidence_id not in known]
    if unknown:
        raise DataToolError(
            "TOOL_INPUT_INVALID",
            f"{len(unknown)} of the evidence ids are not results of this case.",
            status=422,
        )
    columns = {str(c) for f in ctx.files for c in f.frame.columns}
    if ctx.agent is not None:
        columns |= {str(c) for c in ctx.agent.frame.columns}
    index = data_evidence_index(((e, known[e].output) for e in wanted), columns=columns)
    narrator = ctx.narrator
    if narrator is None and ctx.meter is not None:
        narrator = meter_narrator(ctx.meter, ctx.root())
    raw = None if narrator is None else narrator(ExplainRequest(facts=tuple(index.lines())))
    narrative = verify_data_narrative(raw, index, guardrails=ctx.guardrails)
    return Explanation(narrative=narrative, evidence_ids=tuple(wanted), facts_total=len(index.facts))


# ---------------------------------------------------------------------------
# #65 find_personal_data
# ---------------------------------------------------------------------------
class ColumnsArgs(_Args):
    columns: tuple[str, ...] | None = Field(
        default=None, min_length=1, max_length=200, description="Columns to check; every column when omitted."
    )


class PersonalColumn(StrictBase):
    column: str
    kinds: tuple[str, ...] = Field(description="Personal data the column holds (`detect_pii`).")
    free_text_kinds: tuple[str, ...] = Field(description="Personal data mentioned inside free text.")
    hidden: bool = Field(description="True when the use case always hides it (`agent.always_hide_columns`).")


class PersonalData(StrictBase):
    columns: tuple[PersonalColumn, ...] = Field(
        description="Each column holding or mentioning personal data; the last are left out when they do not fit."
    )
    columns_total: int
    checked: int = Field(description="Columns checked.")


def _find_personal_data(parsed: ColumnsArgs, ctx: DataToolContext) -> PersonalData:
    from engine.config import ColumnType
    from engine.pii import detect_pii, free_text_pii

    agent = ctx.require_agent()
    names = (
        [agent.resolve(c) for c in parsed.columns]
        if parsed.columns
        else [str(c) for c in agent.frame.columns]
    )
    names = list(dict.fromkeys(names))
    types = {c.name: c.inferred_type for c in agent.profile.columns}
    hide = {entry.casefold() for entry in agent.config.agent.always_hide_columns}
    found: list[PersonalColumn] = []
    for name in names:
        series = agent.frame[name]
        inferred = types.get(name, ColumnType.STRING)
        kinds = tuple(detect_pii(series, name, inferred))
        free = tuple(free_text_pii(series, inferred))
        hidden = name.casefold() in hide
        if kinds or free or hidden:
            found.append(PersonalColumn(column=name, kinds=kinds, free_text_kinds=free, hidden=hidden))
    shown: PersonalData = _fitted(
        PersonalData(columns=tuple(found), columns_total=len(found), checked=len(names)), ("columns",)
    )
    return shown


# ---------------------------------------------------------------------------
# #66 check_leaks
# ---------------------------------------------------------------------------
class LeakArgs(_Args):
    outcome: str = Field(min_length=1, max_length=512, description="The outcome column.")
    primary_key: str | None = Field(default=None, max_length=512, description="The ID column, if decided.")
    dataset_id: str | None = Field(
        default=None, pattern=_ID, description="A built dataset whose build report to read too."
    )


class LeakSource(StrictBase):
    source: str = Field(description="run_checks, first_look, build_report, builder or ai_reviewer.")
    ran: bool
    found: int = Field(description="Columns it flags.")
    note: str = Field(description="What was checked, or why it was not.")


class LeakColumn(StrictBase):
    column: str
    sources: tuple[str, ...]
    code: str | None = Field(
        description="The check's code (LEAKAGE_SUSPECTED, FUTURE_EVENTS_LEAKED, ...); null for the first look, which has none."
    )
    message: str


class LeakVerdict(StrictBase):
    verdict: Literal["leak_suspected", "none_found"] = Field(
        description="`leak_suspected` when any source flags a column; `none_found` otherwise (never a promise)."
    )
    columns: tuple[LeakColumn, ...]
    columns_total: int
    sources: tuple[LeakSource, ...]
    run_check: tuple[ValidationCheck, ...] = Field(
        description="`LEAKAGE_SUSPECTED`'s own findings (`check_leakage_suspected`), as `POST /runs` reports them."
    )
    run_check_skipped: str | None = Field(description="Why the run's check did not run, when it did not.")


def _check_leaks(parsed: LeakArgs, ctx: DataToolContext) -> LeakVerdict:
    """One leak verdict: the run's own check, the first look's exclusions, a built dataset's report and the
    builder's leak check. The AI's leak reviewer is `second_opinion`'s and is never run here."""
    from engine.doctor.first_look import outcome_look
    from engine.stages.validate import check_leakage_suspected, params_from_config
    from engine.utils.ids import seed_from

    agent = ctx.require_agent()
    outcome = agent.resolve(parsed.outcome)
    key = agent.resolve(parsed.primary_key) if parsed.primary_key else None
    params = params_from_config(
        agent.config,
        primary_key=key,
        target=outcome,
        seed=seed_from(agent.upload_id),
        row_count=agent.profile.row_count,
    )
    checked = check_leakage_suspected(agent.frame, params)
    flagged: dict[str, LeakColumn] = {}

    def flag(column: str, source: str, code: str | None, message: str) -> None:
        current = flagged.get(column)
        if current is None:
            flagged[column] = LeakColumn(column=column, sources=(source,), code=code, message=message)
        elif source not in current.sources:
            flagged[column] = current.model_copy(update={"sources": (*current.sources, source)})

    for finding in checked.findings:
        if finding.column:
            flag(finding.column, "run_checks", finding.code, finding.message)
    sources = [
        LeakSource(
            source="run_checks",
            ran=not checked.skipped,
            found=len({f.column for f in checked.findings if f.column}),
            note=checked.skip_reason or "The check every training run makes (LEAKAGE_SUSPECTED).",
        )
    ]
    look = outcome_look(
        agent,
        outcome=outcome,
        file_name=agent.profile.file_name,
        file_rows=agent.profile.row_count,
        primary_key=key,
    )
    answers = [] if look is None else [c.column for c in look.left_out if c.reason == "may_hold_answer"]
    for column in answers:
        flag(
            column,
            "first_look",
            None,
            "The first look left it out: it may hold the answer, recorded after the outcome.",
        )
    sources.append(
        LeakSource(
            source="first_look",
            ran=look is not None,
            found=len(answers),
            note=(
                "Columns the first look leaves out as possibly holding the answer."
                if look is not None
                else "The outcome is not a yes/no column, so there is no first look."
            ),
        )
    )
    report = _build_report(ctx, parsed.dataset_id)
    if report is None:
        for source in ("build_report", "builder"):
            sources.append(
                LeakSource(
                    source=source,
                    ran=False,
                    found=0,
                    note="Only for a dataset built from several tables (name its dataset_id).",
                )
            )
    else:
        hits = [c for c in report.checks if c.code in BUILD_LEAK_CODES and c.column]
        for check in hits:
            flag(str(check.column), "build_report", check.code, check.message)
        sources.append(
            LeakSource(
                source="build_report",
                ran=True,
                found=len({c.column for c in hits}),
                note="The build report's future-data and time-versioning checks.",
            )
        )
        leak_check = report.leak_check
        sources.append(
            LeakSource(
                source="builder",
                ran=leak_check is not None,
                found=0,
                note=(
                    "The builder's future-data check ran."
                    if leak_check is not None
                    else "The build stopped before its future-data check."
                ),
            )
        )
    sources.append(
        LeakSource(
            source="ai_reviewer",
            ran=False,
            found=0,
            note="The AI's leak review is the second opinion (second_opinion); it only ever asks.",
        )
    )
    columns = tuple(flagged.values())
    shown: LeakVerdict = _fitted(
        LeakVerdict(
            verdict="leak_suspected" if columns else "none_found",
            columns=columns,
            columns_total=len(columns),
            sources=tuple(sources),
            run_check=checked.findings,
            run_check_skipped=checked.skip_reason or None,
        ),
        ("run_check", "columns"),
    )
    return shown


def _build_report(ctx: DataToolContext, dataset_id: str | None) -> Any:
    from engine.onboarding.datasets import DATASET_REPORT_FILENAME, dataset_key
    from engine.onboarding.specs import BuildReport
    from engine.storage import StorageError

    if dataset_id is None:
        return None
    if ctx.storage is None:
        raise DataToolError("TOOL_INPUT_INVALID", "A built dataset needs storage to be read.", status=422)
    try:
        return ctx.storage.read_model(dataset_key(dataset_id, DATASET_REPORT_FILENAME), BuildReport)
    except (StorageError, FileNotFoundError) as exc:
        raise DataToolError("TOOL_INPUT_INVALID", "There is no such built dataset.", status=404) from exc


# ---------------------------------------------------------------------------
# #68 check_uplift_data
# ---------------------------------------------------------------------------
class UpliftArgs(_Args):
    primary_key: str = Field(min_length=1, max_length=512, description="The ID column.")
    target: str = Field(min_length=1, max_length=512, description="The outcome column.")


class UpliftData(StrictBase):
    report: UpliftValidationReport = Field(
        description="The campaign-effect checks (`run_uplift_checks`), as a campaign-effect run writes them; the "
        "last findings are left out when they do not fit."
    )
    checks_total: int


def _check_uplift_data(parsed: UpliftArgs, ctx: DataToolContext) -> UpliftData:
    from engine.uplift.checks import run_uplift_checks

    agent = ctx.require_agent()
    result = run_uplift_checks(
        agent.frame,
        agent.config,
        primary_key=agent.resolve(parsed.primary_key),
        target=agent.resolve(parsed.target),
        upload_id=agent.upload_id,
        now=ctx.clock(),
    )
    output = UpliftData(report=result.report, checks_total=len(result.report.checks))
    from engine.doctor.registry import ROW_LEVEL_BUDGET, output_size

    report = output.report
    while (
        report.checks
        and output_size(
            Exposure.SESSION_ONLY, output.model_copy(update={"report": report}).model_dump(mode="json")
        )
        > ROW_LEVEL_BUDGET
    ):
        report = report.model_copy(update={"checks": report.checks[:-1]})
    return output.model_copy(update={"report": report})


# ---------------------------------------------------------------------------
# #73 read_run_checks
# ---------------------------------------------------------------------------
class RunArgs(_Args):
    run_id: str = Field(pattern=_ID, description="A training run.")


def _read_run_checks(parsed: RunArgs, ctx: DataToolContext) -> RunChecks:
    """`things_checked`: the lines of "Things we checked", the too-good alarm (`RESULT_SUSPICIOUSLY_GOOD`) first."""
    from engine.contracts import RunRecord
    from engine.run_checks import things_checked
    from engine.runs import RUN_FILENAME
    from engine.storage import StorageError, run_key

    storage = ctx.storage
    if storage is None:  # unreachable through the registry: `needs` makes the tool unavailable
        raise DataToolError("TOOL_UNKNOWN", "read_run_checks needs storage.", status=404)
    try:
        record = storage.read_model(run_key(parsed.run_id, RUN_FILENAME), RunRecord)
    except (StorageError, FileNotFoundError) as exc:
        raise DataToolError("RUN_NOT_FOUND", f"No run with id {parsed.run_id!r}.", status=404) from exc
    return things_checked(storage, record, ctx.root())


# ---------------------------------------------------------------------------
# #76 request_approval
# ---------------------------------------------------------------------------
class ApprovalArgs(_Args):
    evidence_id: str = Field(pattern=_EVIDENCE, description="The proposal's result in this case.")


class ApprovalRequest(StrictBase):
    evidence_id: str
    tool: str
    kind: str = Field(description="The proposal's policy kind, e.g. data.join_files.")
    approve_class: bool = Field(
        description="True when it needs the person's own yes with the exact sentence (the design §6.6)."
    )
    sentence: str | None = Field(description="The exact sentence the person approves; null when not needed.")
    sentence_digest: str | None = Field(
        description="SHA-256 of the sentence: what the approval must send back."
    )
    role: str | None = Field(description="The role the approver needs.")
    another_person: bool = Field(
        description="True when the use case sets governance.approval_required: someone other than the proposer."
    )
    requested_by: str | None
    expires_at: datetime | None = Field(
        description="When the request lapses unanswered (Plan K's proposals)."
    )
    note: str


def _request_approval(parsed: ApprovalArgs, ctx: DataToolContext) -> ApprovalRequest:
    """What one earlier proposal needs from the person, by M233's APPROVE-class rule: its own yes, the exact
    sentence (and its digest, which the approval sends back word for word), the role, separation of duties
    and Plan K's expiry. It asks; it records and decides nothing (the approval is M246's)."""
    from engine.agentic.config import load_policy
    from engine.doctor import policy as pol
    from engine.doctor.registry import data_registry

    result = next((r for r in ctx.evidence if r.evidence_id == parsed.evidence_id), None)
    if result is None:
        raise DataToolError(
            "TOOL_INPUT_INVALID", "That evidence id is not a result of this case.", status=422
        )
    tool = data_registry().get(result.tool)
    kind = tool.proposal_kind
    if kind is None:
        raise DataToolError("TOOL_INPUT_INVALID", f"{result.tool} proposes nothing to approve.", status=422)
    data_policy = pol.policy_or_deny(ctx.root())
    rules = data_policy.rule_sets.get(kind)
    if rules is None:
        raise DataToolError("POLICY_NO_RULES", f"No policy rules are set for a {kind} proposal.", status=403)
    try:
        proposals = pol.output_proposals(kind, result.output)
    except ValueError as exc:
        raise DataToolError("POLICY_PAYLOAD_INVALID", str(exc)[:300], status=403) from exc
    pctx = pol.tool_policy_context(ctx, kind)
    needing = [p for p in proposals if pol._approve_class(p, pctx, rules)]
    separation = bool(ctx.agent is not None and ctx.agent.config.governance.approval_required)
    requested_by = ctx.principal.user_id if ctx.principal is not None else None
    if not needing:
        return ApprovalRequest(
            evidence_id=result.evidence_id,
            tool=result.tool,
            kind=kind,
            approve_class=False,
            sentence=None,
            sentence_digest=None,
            role=None,
            another_person=False,
            requested_by=requested_by,
            expires_at=None,
            note="This needs no approval of its own: ticking it in the list is the person's yes.",
        )
    sentence = needing[0].sentence
    if not sentence:
        raise DataToolError(
            "POLICY_PAYLOAD_INVALID",
            "An item that needs your explicit yes carries the exact sentence you approve.",
            status=403,
        )
    days = load_policy(ctx.root()).proposals.expire_after_days
    return ApprovalRequest(
        evidence_id=result.evidence_id,
        tool=result.tool,
        kind=kind,
        approve_class=True,
        sentence=sentence,
        sentence_digest=hashlib.sha256(sentence.encode("utf-8")).hexdigest(),
        role=rules.approve_role.value,
        another_person=separation,
        requested_by=requested_by,
        expires_at=ctx.clock() + timedelta(days=days),
        note="Approve the exact sentence; if it changes before you approve, the new one is shown instead.",
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
_Entry = tuple[
    int,
    str,
    DataToolKind,
    str,
    type[BaseModel],
    type[BaseModel],
    Callable[..., Any],
    tuple[Need, ...],
    Mapping[str, Any],
]


def _entries() -> tuple[_Entry, ...]:
    read, propose, ask = DataToolKind.READ, DataToolKind.PROPOSE, DataToolKind.ASK
    return (
        (
            3,
            "propose_intake",
            propose,
            "How the file was read (header row, sheet, rows left out), kept as one "
            "decision so next month's file is read the same way.",
            RoleArgs,
            IntakeProposal,
            _propose_intake,
            ("storage",),
            {"available": _file_or_files, "wraps": "engine/intake/run.py:162 intake_file (intake.json)"},
        ),
        (
            6,
            "preview_connection",
            read,
            "The first rows of a table or file in a saved connection, every cell " "masked.",
            SelectionArgs,
            ConnectionPreview,
            _preview_connection,
            ("connections",),
            {"wraps": "api/routes/connections.py:398 preview_connection (Connector.preview)"},
        ),
        (
            7,
            "propose_import",
            propose,
            "Propose importing a table or file from a saved connection as a file of "
            "the case; nothing is read but its preview until the person says yes.",
            ImportArgs,
            ImportProposal,
            _propose_import,
            ("connections",),
            {"wraps": "api/routes/connections.py import_connection; engine/onboarding/sources.py:634"},
        ),
        (
            8,
            "check_refresh",
            read,
            "Which tables a saved recipe's next build reads, which are newer, and which "
            "a refresh reads again from their connection.",
            RefreshArgs,
            RefreshCheck,
            _check_refresh,
            ("client_store",),
            {"wraps": "engine/scheduling/firing.py:536 latest_recipe_inputs; sources.py:776"},
        ),
        (
            9,
            "preflight",
            read,
            "The pre-flight check on the case's files: which file is which table, rows, "
            "dates, how the IDs link up, personal details and format problems.",
            PreflightArgs,
            PreflightOutput,
            _preflight,
            (),
            {"available": _file_or_files, "wraps": "engine/pilot/preflight.py:303 run_preflight"},
        ),
        (
            31,
            "second_opinion",
            propose,
            "The AI reorders candidates the rules found and asks about columns that "
            "may be recorded after the outcome; pending questions only.",
            _Args,
            SecondOpinion,
            _second_opinion,
            ("agent", "case", "meter"),
            {"available": _agentic_on, "wraps": "engine/agent/assist.py:365 assist_session"},
        ),
        (
            39,
            "explain",
            read,
            "Explain earlier results in short sentences whose every number is checked "
            "against the evidence; the rules' sentences when the AI's fail.",
            ExplainArgs,
            Explanation,
            _explain,
            ("evidence",),
            {"wraps": "engine/agentic/claims.py:756/:805 with data claims (engine/doctor/explain.py)"},
        ),
        (
            41,
            "propose_role",
            propose,
            "Propose what one file is (customer list, bills, tickets, ...), with the "
            "reasons; pending, to check.",
            RoleArgs,
            RoleProposal,
            _propose_role,
            (),
            {"available": _file_or_files, "wraps": "engine/onboarding/sources.py:305 detect_roles"},
        ),
        (
            42,
            "propose_key",
            propose,
            "Propose the file's ID column, the rules' choice; when none can be, why.",
            KeyArgs,
            KeyProposal,
            _propose_key,
            ("agent",),
            {"wraps": "engine/agent/advisor.py:342 _choose_primary_key"},
        ),
        (
            43,
            "propose_mapping",
            propose,
            "Propose how a file's columns map onto the use case's columns; pending, " "to check.",
            MappingArgs,
            MappingProposal,
            _propose_mapping,
            (),
            {"available": _file_or_files, "wraps": "engine/onboarding/mapping.py:691 suggested_mapping_spec"},
        ),
        (
            65,
            "find_personal_data",
            read,
            "Columns that hold or mention personal data, and which kinds.",
            ColumnsArgs,
            PersonalData,
            _find_personal_data,
            ("agent",),
            {"wraps": "engine/pii.py:265/:327 detect_pii, free_text_pii"},
        ),
        (
            66,
            "check_leaks",
            read,
            "One verdict on columns that may hold the answer: the run's leak check, the "
            "first look, a built dataset's report and the builder's check.",
            LeakArgs,
            LeakVerdict,
            _check_leaks,
            ("agent",),
            {"wraps": "engine/stages/validate.py:1840 check_leakage_suspected; first_look; build report"},
        ),
        (
            68,
            "check_uplift_data",
            read,
            "The campaign-effect checks: treatment column, arms, randomness, " "maturity.",
            UpliftArgs,
            UpliftData,
            _check_uplift_data,
            ("agent",),
            {"wraps": "engine/uplift/checks.py:814 run_uplift_checks"},
        ),
        (
            73,
            "read_run_checks",
            read,
            "What a training run checked, and whether its result is too good to be " "true.",
            RunArgs,
            RunChecks,
            _read_run_checks,
            ("storage",),
            {"wraps": "engine/run_checks.py:231 things_checked"},
        ),
        (
            76,
            "request_approval",
            ask,
            "Ask the person to approve a proposal that needs their own yes: the exact "
            "sentence, the role, and whether another person must approve.",
            ApprovalArgs,
            ApprovalRequest,
            _request_approval,
            ("evidence",),
            {"wraps": "engine/doctor/policy.py approve_class; engine/approvals.py:481/:510"},
        ),
    )


def register_wrappers(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register M236's tools in `registry`; their names, in catalogue order."""
    from engine.doctor.policy import policy_kind_for

    names: list[str] = []
    for number, name, kind, description, args, output, run, needs, extra in _entries():
        registry.register(
            DataTool(
                number=number,
                name=name,
                kind=kind,
                description=description,
                input_model=args,
                output_model=output,
                route_policy=_policy(
                    (
                        "ask Data Doctor for a suggestion"
                        if kind is DataToolKind.PROPOSE
                        else (
                            "ask for an approval"
                            if kind is DataToolKind.ASK
                            else "ask Data Doctor about the data"
                        )
                    ),
                    name,
                ),
                exposure=Exposure.SESSION_ONLY,
                run=run,
                callers=RULES_AND_SCREEN,
                needs=needs,
                available=extra.get("available", lambda _ctx: True),
                max_output_bytes=BY_EXPOSURE,
                policy_kind=policy_kind_for(name) if kind is DataToolKind.PROPOSE else None,
                wraps=extra["wraps"],
            )
        )
        names.append(name)
    return tuple(names)
