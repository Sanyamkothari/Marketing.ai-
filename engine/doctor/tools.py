"""The Data Doctor tools registered in M232 (Plan O, DEC-1802; the design §5.5).

**The 22 existing tools.** Plan G's 18 look tools (`engine.agent.tools.TOOLS`, in that order), the chat's two
PROPOSE actions (`propose_setting`, `propose_step`), the person's `edit_by_hand`, and the conversation loop
itself, which is not a tool: it is the caller (`engine.agent.loop.chat_turn` calls every tool through this
registry behind `data_doctor.unified`).

**The first wrappers** (#10, #29, #32-34, #36-38, #40, #71): functions Plan G and Plan L already have, made tools
with typed inputs and outputs. Each output is the wrapped function's own model, unchanged, so a wrapper can never
disagree with the screen that calls the function directly. In M232 the wrappers serve the rules and the screen;
the AI reaches them through the visit (M235), so the chat's prompt is what it was.

Roles (`RoutePolicy`, checked on every call):

* a tool that returns a cell, a row or a masked example needs an **Analyst**, as reading the rows of a file
  being set up does (`POST /uploads/{id}/agent-session/rows`) and as the Doctor's diagnosis does;
* `what_can_this_power` (aggregate verdicts) and `data_request` (no data at all) need only a **Viewer**;
* every PROPOSE tool needs an **Analyst**: a Viewer may call READ tools only.

Budgets (the design §5.2): a `session-only` result is at most 5,500 characters of JSON, an `aggregate` one 16 KB.
The wrappers that list things cut each list from its end to fit and say how many there were (`*_total`).
`data_request` holds no client data and keeps 16 KB; the three suggestions that run Plan G's handlers are bounded
by Plan G (`max_output_bytes=None`), so the registry never refuses what Plan G accepts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Final, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.contracts import Proposal, ProposalSource, RecipeStepKind
from engine.agent.tools import TOOLS, AgentToolError, NoArgs, Tool, ToolKind, tool_available
from engine.config import ConfigError, StrictBase
from engine.doctor.contracts import FileDiagnosis, FileLink, FileStack, UseCaseReadiness
from engine.doctor.diagnose import ADVISE_ROWS
from engine.doctor.first_look import FirstLook
from engine.doctor.memory import TreatmentOffer
from engine.doctor.registry import (
    AGGREGATE_BUDGET,
    BY_EXPOSURE,
    ROW_LEVEL_BUDGET,
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
    output_size,
)
from engine.doctor.sufficiency import NextSteps
from engine.onboarding.specs import KeyCandidate, RoleCandidate, TimeCandidate
from engine.pilot.data_request import DataRequest

if TYPE_CHECKING:
    from engine.doctor.diagnose import LoadedFile

__all__ = [
    "ADVICE_MAX_PROPOSALS",
    "ADVICE_MAX_QUESTIONS",
    "DATA_REQUEST_MAX_USE_CASES",
    "DIAGNOSIS_FILES",
    "DIAGNOSIS_ROWS",
    "LOOK_NUMBERS",
    "AdviceDigest",
    "LookOutput",
    "ProposeOutput",
    "register_all",
]

LOOK_NUMBERS: Final[dict[str, int]] = {
    "get_profile": 11,
    "inspect_column": 12,
    "find_format_issues": 13,
    "describe_placeholder_values": 14,
    "describe_outcome": 15,
    "describe_repeats": 16,
    "find_roles": 17,
    "draft_outcome_labels": 18,
    "check_data": 19,
    "sample_rows": 20,
    "value_counts": 21,
    "find_values": 22,
    "describe_numbers": 23,
    "describe_dates": 24,
    "compare_columns": 25,
    "describe_missing": 26,
    "rate_by": 27,
    "describe_duplicates": 28,
}
"""Plan G's look tools and their catalogue rows (the design §5.5 B)."""

ADVICE_MAX_PROPOSALS: Final[int] = 60
ADVICE_MAX_QUESTIONS: Final[int] = 20
"""`advise` lists at most this many suggestions and questions, fewer when they do not fit its budget; the totals
say how many there were."""
DIAGNOSIS_FILES: Final[int] = 20
"""`diagnose_files` reads at most this many files, as `POST /doctor/diagnosis` does (`DiagnosisRequest`)."""
DIAGNOSIS_ROWS: Final[int] = 4_000_000
"""Rows one `diagnose_files` call reads across its files, as the Doctor route does (`api.routes.doctor`
`DIAGNOSIS_ROWS`, M161 DEC-1507): an even share each, never fewer than `ADVISE_ROWS`, never more than the
use case's `profile_row_cap`. A test keeps the two equal."""
DATA_REQUEST_MAX_USE_CASES: Final[int] = 4
"""`data_request` names at most this many use cases: the four whose requests are longest fit its budget (a test
checks it). The route serves any number; the tool asks for a narrower request."""
_USE_CASE_ID: Final[str] = r"^[a-z0-9][a-z0-9-]{0,63}$"

# ---------------------------------------------------------------------------
# Route policies
# ---------------------------------------------------------------------------
_ROWS_POLICY: Final[RoutePolicy] = RoutePolicy(
    role=Role.ANALYST,
    action="agent.rows",
    purpose="see the rows of the file being set up",
    object_type="upload",
)
_CHAT_POLICY: Final[RoutePolicy] = RoutePolicy(
    role=Role.ANALYST,
    action="agent.chat",
    purpose="ask the helper",
    object_type="upload",
)
_EDIT_POLICY: Final[RoutePolicy] = RoutePolicy(
    role=Role.ANALYST,
    action="agent.edit",
    purpose="edit the data by hand",
    object_type="upload",
)
_DOCTOR_POLICY: Final[RoutePolicy] = RoutePolicy(
    role=Role.ANALYST, action="doctor.diagnose", purpose="ask Data Doctor about uploaded files"
)
_VERDICT_POLICY: Final[RoutePolicy] = RoutePolicy(
    role=Role.VIEWER, action="doctor.verdict_read", purpose="see what your files can power"
)
_DATA_REQUEST_POLICY: Final[RoutePolicy] = RoutePolicy(
    role=Role.VIEWER, action="pilot.data_request_read", purpose="download the data request"
)

_EVERYONE: Final[frozenset[str]] = frozenset({"rules", "ai", "person"})
_RULES_AND_SCREEN: Final[frozenset[str]] = frozenset({"rules", "person"})
_RULES_AND_AI: Final[frozenset[str]] = frozenset({"rules", "ai"})


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class LookOutput(RootModel[dict[str, JsonValue]]):
    """A Plan G look tool's JSON result, as the tool returned it (cells already masked by the tool)."""


# ---------------------------------------------------------------------------
# B. Plan G's 18 look tools, as they are
# ---------------------------------------------------------------------------
def _look_run(name: str) -> Callable[[BaseModel, DataToolContext], LookOutput]:
    def run(parsed: BaseModel, ctx: DataToolContext) -> LookOutput:
        from engine.agent.tools import call_tool

        given = parsed.model_dump(exclude_unset=True)
        return LookOutput(call_tool(ctx.require_agent(), name, given, evidence_id="registry").result)

    return run


def _look_available(name: str) -> Callable[[DataToolContext], bool]:
    def available(ctx: DataToolContext) -> bool:
        return ctx.agent is not None and tool_available(ctx.agent, name)

    return available


def _look_tool(tool: Tool) -> DataTool[Any, LookOutput]:
    return DataTool(
        number=LOOK_NUMBERS[tool.name],
        name=tool.name,
        kind=DataToolKind.READ,
        description=tool.description,
        input_model=tool.args_model,
        output_model=LookOutput,
        route_policy=_ROWS_POLICY,
        exposure=Exposure.SESSION_ONLY,
        run=_look_run(tool.name),
        callers=_EVERYONE,
        needs=("agent",),
        available=_look_available(tool.name),
        mirrors=("POST", "/uploads/{upload_id}/agent-session/rows"),
        session_tool=True,
        wraps=f"engine/agent/tools.py TOOLS[{tool.name!r}]",
    )


# ---------------------------------------------------------------------------
# D/E. The chat's PROPOSE actions and the person's edit
# ---------------------------------------------------------------------------
class _ChatArgs(BaseModel):
    """The chat's suggestion arguments are the model's own: nothing is stripped or changed (Plan G decides)."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class ProposeSettingArgs(_ChatArgs):
    """`propose_setting`'s arguments, read leniently as Plan G's chat reads them (its own checks decide)."""

    path: JsonValue = Field(default=None, description="The setting's dotted path, from the listed settings.")
    value: JsonValue = Field(default=None, description="The suggested value.")
    reason: JsonValue = Field(default=None, description="Why, in one plain sentence.")


class ProposeStepArgs(_ChatArgs):
    """`propose_step`'s arguments, read leniently as Plan G's chat reads them (its own checks decide)."""

    kind: JsonValue = Field(default=None, description="One step kind the chat may suggest.")
    column: JsonValue = Field(default=None, description="The column it changes.")
    params: JsonValue = Field(default=None, description="The step's parameters.")
    new_column: JsonValue = Field(default=None, description="For a split or a rename.")
    reason: JsonValue = Field(default=None, description="Why, in one plain sentence.")
    evidence_ids: JsonValue = Field(
        default=None, description="The look-tool results about the column this fix rests on."
    )


PROPOSE_ARG_FIELDS: Final[dict[str, frozenset[str]]] = {
    "propose_setting": frozenset(ProposeSettingArgs.model_fields),
    "propose_step": frozenset(ProposeStepArgs.model_fields),
}
"""The arguments each chat PROPOSE tool reads; the chat passes these and leaves the rest, as Plan G ignores them."""


class ProposeOutput(StrictBase):
    """A PROPOSE tool's answer: the pending (or, for a person's edit, accepted) item, and what it measured.

    Its strings are recorded as the handler made them (nothing stripped), so the evidence is the suggestion's.
    """

    model_config = ConfigDict(**{**StrictBase.model_config, "str_strip_whitespace": False})

    args: dict[str, JsonValue] = Field(default_factory=dict)
    result: dict[str, JsonValue] = Field(default_factory=dict)
    proposal: Proposal


def _chat_propose(name: str) -> Callable[[BaseModel, DataToolContext], ProposeOutput]:
    def run(parsed: BaseModel, ctx: DataToolContext) -> ProposeOutput:
        if ctx.turn is None:  # unreachable through the registry: `needs` makes the tool unavailable
            raise DataToolError("TOOL_UNKNOWN", f"{name} is made inside a chat turn.", status=404)
        result, proposal = ctx.turn.propose(name, parsed.model_dump(exclude_unset=True))
        return ProposeOutput(args=dict(result.args), result=dict(result.result), proposal=proposal)

    return run


class EditArgs(_Args):
    """`edit_by_hand`'s arguments: the body of `POST /uploads/{id}/agent-session/edits`."""

    kind: RecipeStepKind = Field(description="What the edit does: one of the step kinds a person may use.")
    column: str = Field(min_length=1, max_length=512, description="The column it changes.")
    params: dict[str, JsonValue] = Field(default_factory=dict, description="The step's parameters.")
    new_column: str | None = Field(default=None, max_length=128, description="For a split or a rename.")


def _edit_by_hand(parsed: EditArgs, ctx: DataToolContext) -> ProposeOutput:
    """The person's edit as an accepted step (`engine.agent.edits.add_edit`).

    The edited session goes to `ctx.save_session`, which the caller stores under its lock and version check (as
    the edits route does); the tool is not available without one, so an edit is never made and dropped.
    """
    from engine.agent.edits import add_edit
    from engine.agent.session import SessionError

    session, save = ctx.session, ctx.save_session
    if session is None or save is None:  # unreachable through the registry: `needs` makes it unavailable
        raise DataToolError(
            "TOOL_UNKNOWN", "edit_by_hand needs a session and somewhere to save it.", status=404
        )
    try:
        edited = add_edit(
            session,
            ctx.require_agent(),
            kind=parsed.kind,
            column=parsed.column,
            params=dict(parsed.params),
            new_column=parsed.new_column,
            clock=ctx.clock,
        )
    except SessionError as exc:
        raise DataToolError(exc.code, exc.message, status=409) from exc
    column = ctx.require_agent().resolve(parsed.column)
    made = [
        p
        for p in edited.proposals
        if p.source is ProposalSource.PERSON
        and p.step is not None
        and p.step.kind is parsed.kind
        and p.step.column == column
    ]
    if not made:  # add_edit always adds or merges one; anything else is a bug, not the person's problem
        raise AgentToolError("AGENT_TOOL_ARGS_INVALID", "The edit made no step.")
    proposal = made[-1]
    save(edited)
    return ProposeOutput(args={"kind": parsed.kind.value, "column": column}, result={}, proposal=proposal)


# ---------------------------------------------------------------------------
# Wrappers (#10, #29, #32-34, #36-38, #40, #71)
# ---------------------------------------------------------------------------
class FileArgs(_Args):
    upload_id: str | None = Field(
        default=None,
        max_length=128,
        description="One of the case's files; the file being set up when omitted.",
    )


class DiagnoseArgs(_Args):
    upload_ids: tuple[str, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=DIAGNOSIS_FILES,
        description="Up to 20 of the case's files; every file of the case (at most 20) when omitted.",
    )


class AdviseArgs(_Args):
    primary_key: str | None = Field(default=None, max_length=512, description="The ID column, if decided.")
    target: str | None = Field(default=None, max_length=512, description="The outcome column, if decided.")


class OutcomeArgs(_Args):
    outcome: str = Field(min_length=1, max_length=512, description="The outcome column.")
    primary_key: str | None = Field(default=None, max_length=512, description="The ID column, if decided.")


class DataRequestArgs(_Args):
    use_case_ids: tuple[Annotated[str, Field(pattern=_USE_CASE_ID)], ...] | None = Field(
        default=None,
        min_length=1,
        max_length=DATA_REQUEST_MAX_USE_CASES,
        description="Up to 4 configured use cases to ask data for; the file's own use case when omitted.",
    )


def _loaded(ctx: DataToolContext, upload_id: str | None) -> LoadedFile:
    """The case file `upload_id`, or the file being set up (`ctx.agent`) as a `LoadedFile`."""
    from engine.doctor.diagnose import LoadedFile

    if upload_id is not None:
        for loaded in ctx.files:
            if loaded.upload_id == upload_id:
                return loaded
    agent = ctx.agent
    if agent is not None and (upload_id is None or upload_id == agent.upload_id):
        return LoadedFile(
            upload_id=agent.upload_id,
            file_name=agent.profile.file_name,
            frame=agent.frame,
            profile=agent.profile,
        )
    if upload_id is None and len(ctx.files) == 1:
        return ctx.files[0]
    raise DataToolError(
        "TOOL_INPUT_INVALID",
        "Name one of the case's files." if upload_id is None else "That file is not in this case.",
        status=422,
    )


def _config(ctx: DataToolContext) -> Any:
    """The file's use case, else the first offer's, else the pilot's first configured use case
    (`configs/pilot/data_request.yaml` `default_use_cases`): never a use case named in code (DEC-1802 (t))."""
    if ctx.agent is not None:
        return ctx.agent.config
    if ctx.offers:
        return ctx.offers[0].config
    from engine.config import load_use_case
    from engine.pilot.data_request import load_wording

    return load_use_case(load_wording(ctx.root()).default_use_cases[0], ctx.root())


def _roles(ctx: DataToolContext) -> Any:
    from engine.config import get_roles

    return ctx.roles if ctx.roles is not None else get_roles(ctx.root())


def _root(ctx: DataToolContext) -> Any:
    from engine.config import config_root

    return config_root(ctx.root())


def _file_or_files(ctx: DataToolContext) -> bool:
    return ctx.agent is not None or bool(ctx.files)


M = TypeVar("M", bound=BaseModel)


def _fitted(
    model: M,
    fields: tuple[str, ...],
    whole: Callable[[M], BaseModel] | None = None,
    budget: int = ROW_LEVEL_BUDGET,
) -> M:
    """`model` with items dropped from the end of each of `fields`, in order, until the result fits `budget`.

    As Plan G's look tools do (DEC-1054): a list that does not fit loses items from its end, and the output's
    `*_total` fields say how many there were. `whole` builds what is measured when `model` sits inside the output
    (`first_look`). Each list is cut by halving, so a long list costs a few measurements, not one per item. When
    nothing is left to cut and it still does not fit, the registry refuses it (`TOOL_OUTPUT_TOO_LARGE`).
    """

    def size(candidate: M) -> int:
        shown = whole(candidate) if whole is not None else candidate
        return output_size(Exposure.SESSION_ONLY, shown.model_dump(mode="json"))

    for field in fields:
        if size(model) <= budget:
            return model
        items = tuple(getattr(model, field))
        low, high = 0, len(items)
        while low < high:  # the most items that fit, between `low` (fits or none) and `high`
            middle = (low + high + 1) // 2
            if size(model.model_copy(update={field: items[:middle]})) <= budget:
                low = middle
            else:
                high = middle - 1
        model = model.model_copy(update={field: items[:low]})
    return model


class DiagnosedFiles(StrictBase):
    files: tuple[FileDiagnosis, ...] = Field(
        description="One diagnosis per file, in the order asked; the last ones are left out when they do not fit."
    )
    files_total: int = Field(description="Files diagnosed; more than `files` lists when some did not fit.")


def _diagnose_files(parsed: DiagnoseArgs, ctx: DataToolContext) -> DiagnosedFiles:
    """The Doctor route's diagnosis of each file, with its caps: at most 20 files, and an even share of
    `DIAGNOSIS_ROWS` rows each (never fewer than `ADVISE_ROWS`, never more than the use case's row cap)."""
    from dataclasses import replace

    from engine.doctor.diagnose import diagnose_file
    from engine.stages.ingest import profile_row_cap

    if parsed.upload_ids is None:
        files = list(ctx.files) or [_loaded(ctx, None)]
    else:
        files = [_loaded(ctx, upload_id) for upload_id in dict.fromkeys(parsed.upload_ids)]
    if len(files) > DIAGNOSIS_FILES:
        raise DataToolError(
            "TOOL_INPUT_INVALID",
            f"The case has {len(files)} files; name at most {DIAGNOSIS_FILES} of them in upload_ids.",
            status=422,
        )
    config, roles = _config(ctx), _roles(ctx)
    cap = min(profile_row_cap(config), max(ADVISE_ROWS, DIAGNOSIS_ROWS // len(files)))
    capped = [
        loaded if len(loaded.frame) <= cap else replace(loaded, frame=loaded.frame.head(cap))
        for loaded in files
    ]
    diagnosed = tuple(diagnose_file(loaded, config, roles) for loaded in capped)
    return _fitted(DiagnosedFiles(files=diagnosed, files_total=len(diagnosed)), ("files",))


class FileRoles(StrictBase):
    upload_id: str
    roles: tuple[RoleCandidate, ...] = Field(
        description="Each role the file could play, most likely first, with reasons; the least likely are left "
        "out when they do not fit."
    )
    roles_total: int = Field(description="Roles found; more than `roles` lists when some did not fit.")
    keys: tuple[KeyCandidate, ...] = Field(
        description="Columns that could be its ID, best first (at most 5)."
    )


def _find_file_role(parsed: FileArgs, ctx: DataToolContext) -> FileRoles:
    from engine.onboarding.sources import profile_source

    loaded = _loaded(ctx, parsed.upload_id)
    source = profile_source(
        loaded.frame,
        _config(ctx),
        source_id=loaded.upload_id,
        client_id="data-doctor",
        file_name=loaded.file_name,
        row_count=loaded.profile.row_count,
        roles=_roles(ctx),
    )
    found = FileRoles(
        upload_id=loaded.upload_id,
        roles=tuple(source.role_candidates),
        roles_total=len(source.role_candidates),
        keys=tuple(source.key_candidates[:5]),
    )
    return _fitted(found, ("roles", "keys"))


class TimeColumns(StrictBase):
    upload_id: str
    columns: tuple[TimeCandidate, ...] = Field(
        description="Columns that could be the date of each row: parse rate, range, whether day and month are ambiguous."
    )
    columns_total: int = Field(description="Columns found; more than `columns` lists when some did not fit.")


def _find_time_columns(parsed: FileArgs, ctx: DataToolContext) -> TimeColumns:
    from engine.onboarding.sources import profile_source

    loaded = _loaded(ctx, parsed.upload_id)
    source = profile_source(
        loaded.frame,
        _config(ctx),
        source_id=loaded.upload_id,
        client_id="data-doctor",
        file_name=loaded.file_name,
        row_count=loaded.profile.row_count,
        roles=_roles(ctx),
    )
    found = TimeColumns(
        upload_id=loaded.upload_id,
        columns=tuple(source.time_candidates),
        columns_total=len(source.time_candidates),
    )
    return _fitted(found, ("columns",))


class ProposalLine(StrictBase):
    proposal_id: str
    kind: str
    title: str
    confidence: str
    state: str
    evidence_ids: tuple[str, ...]


class QuestionLine(StrictBase):
    question_id: str
    text: str
    options: tuple[str, ...]
    blocking: bool


class AdviceDigest(StrictBase):
    """The rules advisor's verdict on one file (Plan G `advise`, rules only, no AI)."""

    status: str
    stop_reason: str | None
    primary_key: str | None
    target: str | None
    proposals: tuple[ProposalLine, ...]
    proposals_total: int
    questions: tuple[QuestionLine, ...]
    questions_total: int
    hidden: tuple[str, ...]


def _advise(parsed: AdviseArgs, ctx: DataToolContext) -> AdviceDigest:
    from engine.agent.advisor import advise

    advice = advise(ctx.require_agent(), primary_key=parsed.primary_key, target=parsed.target)
    digest = AdviceDigest(
        status=advice.status.value,
        stop_reason=advice.stop_reason,
        primary_key=advice.primary_key,
        target=advice.target,
        proposals=tuple(
            ProposalLine(
                proposal_id=p.proposal_id,
                kind=p.kind.value,
                title=p.title,
                confidence=p.confidence.value,
                state=p.state.value,
                evidence_ids=p.evidence_ids,
            )
            for p in advice.proposals[:ADVICE_MAX_PROPOSALS]
        ),
        proposals_total=len(advice.proposals),
        questions=tuple(
            QuestionLine(
                question_id=q.question_id,
                text=q.text,
                options=tuple(o.label for o in q.options),
                blocking=q.blocking,
            )
            for q in advice.questions[:ADVICE_MAX_QUESTIONS]
        ),
        questions_total=len(advice.questions),
        hidden=advice.engine_hidden,
    )
    return _fitted(digest, ("proposals", "questions"))


class FirstLookOutput(StrictBase):
    look: FirstLook | None = Field(
        description="The rate of 'yes' and the rate by the most informative columns; null when not yes/no. "
        "The columns left out, then the last columns, are cut when they do not fit."
    )
    columns_total: int = Field(default=0, description="Columns measured; more than `look.columns` when cut.")
    left_out_total: int = Field(
        default=0, description="Columns left out; more than `look.left_out` when cut."
    )


def _first_look(parsed: OutcomeArgs, ctx: DataToolContext) -> FirstLookOutput:
    from engine.doctor.first_look import outcome_look

    agent = ctx.require_agent()
    look = outcome_look(
        agent,
        outcome=agent.resolve(parsed.outcome),
        file_name=agent.profile.file_name,
        file_rows=agent.profile.row_count,
        primary_key=agent.resolve(parsed.primary_key) if parsed.primary_key else None,
    )
    if look is None:
        return FirstLookOutput(look=None)
    totals = {"columns_total": len(look.columns), "left_out_total": len(look.left_out)}
    shown = _fitted(look, ("left_out", "columns"), whole=lambda cut: FirstLookOutput(look=cut, **totals))
    return FirstLookOutput(look=shown, **totals)


class FilesFit(StrictBase):
    links: tuple[FileLink, ...] = Field(
        description="How each event file's customer IDs meet the customer list's."
    )
    links_total: int = Field(description="Links found; more than `links` lists when some did not fit.")
    stacks: tuple[FileStack, ...] = Field(description="Files that are the same report for different periods.")
    stacks_total: int = Field(description="Stacks found; more than `stacks` lists when some did not fit.")


def _how_files_fit(_parsed: NoArgs, ctx: DataToolContext) -> FilesFit:
    from engine.doctor.contracts import Diagnosis
    from engine.doctor.diagnose import diagnose_file
    from engine.doctor.joins import how_files_fit

    files = list(ctx.files)
    config, roles = _config(ctx), _roles(ctx)
    diagnosed = tuple(diagnose_file(loaded, config, roles) for loaded in files)
    fitted = how_files_fit(Diagnosis(files=diagnosed, use_cases=()), files, roles)
    found = FilesFit(
        links=fitted.links,
        links_total=len(fitted.links),
        stacks=fitted.stacks,
        stacks_total=len(fitted.stacks),
    )
    return _fitted(found, ("stacks", "links"))


class PowerVerdicts(StrictBase):
    use_cases: tuple[UseCaseReadiness, ...] = Field(
        description="Each use case's verdict over the files, ready first."
    )


def _what_can_this_power(_parsed: NoArgs, ctx: DataToolContext) -> PowerVerdicts:
    from engine.doctor.diagnose import Offer, readiness

    files = list(ctx.files) or [_loaded(ctx, None)]
    offers = ctx.offers or (Offer(config=_config(ctx), stage=""),)
    return PowerVerdicts(use_cases=readiness(files, offers, _root(ctx)))


def _enough_data(parsed: OutcomeArgs, ctx: DataToolContext) -> NextSteps:
    from engine.doctor.sufficiency import enough_data

    agent = ctx.require_agent()
    return enough_data(
        agent.frame,
        outcome=agent.resolve(parsed.outcome),
        config=agent.config,
        root=_root(ctx),
        file_name=agent.profile.file_name,
        file_rows=agent.profile.row_count,
    )


def _data_request(parsed: DataRequestArgs, ctx: DataToolContext) -> DataRequest:
    """The data request kit, for configured use cases only: as `GET /pilot/data-request` checks them
    (`api.routes.pilot._use_cases`), an id that is not configured is refused, here with no server path."""
    from engine.config import list_use_case_ids
    from engine.pilot.data_request import build_data_request

    ids = parsed.use_case_ids or ((ctx.agent.use_case_id,) if ctx.agent is not None else None)
    if ids is not None:
        known = set(list_use_case_ids(ctx.root()))
        unknown = [use_case for use_case in dict.fromkeys(ids) if use_case not in known]
        if unknown:
            raise DataToolError(
                "TOOL_INPUT_INVALID", f"Unknown use case: {', '.join(unknown)[:200]}.", status=422
            )
    try:
        return build_data_request(ids, ctx.root())
    except ConfigError as exc:  # a use case that does not load: say which, never where it lives
        raise DataToolError(
            "TOOL_INPUT_INVALID", "A use case named here could not be read.", status=422
        ) from exc


class RecalledTreatment(StrictBase):
    offer: TreatmentOffer | None = Field(
        description="The treatment of a file of the same shape, approved before; null when none."
    )


def _recall_treatment(_parsed: NoArgs, ctx: DataToolContext) -> RecalledTreatment:
    from engine.doctor.memory import offer_for

    agent = ctx.require_agent()
    if ctx.storage is None:  # unreachable through the registry: `needs` makes the tool unavailable
        raise DataToolError("TOOL_UNKNOWN", "recall_treatment needs storage.", status=404)
    offer = offer_for(
        ctx.storage,
        use_case_id=agent.use_case_id,
        upload_id=agent.upload_id,
        columns=[str(column) for column in agent.frame.columns],
        intake=None,
        mode=agent.mode.value,
    )
    return RecalledTreatment(offer=offer)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
_Wrapper = tuple[
    int, str, str, type[BaseModel], type[BaseModel], Callable[..., Any], tuple[Any, ...], dict[str, Any]
]


def register_all(registry: DataToolRegistry) -> None:
    """Register every tool built so far: M232's 22 existing (21 tools and the loop) and first wrappers, the
    tools of Plan O wave 1 (`engine.doctor.tools_wave_one`), then M236's wrappers and `explain`
    (`engine.doctor.tools_wrappers`), M242's `convert_currency` (`engine.doctor.tools_wave_two`), M239's
    outcome tools (`engine.doctor.tools_outcomes`) and M244's time-settings tools (`engine.doctor.tools_time`).
    """
    for tool in TOOLS.values():
        if tool.kind is ToolKind.READ:
            registry.register(_look_tool(tool))
    registry.register(
        DataTool(
            number=44,
            name="propose_setting",
            kind=DataToolKind.PROPOSE,
            description="Suggest one setting from the listed settings; pending until the person decides.",
            input_model=ProposeSettingArgs,
            output_model=ProposeOutput,
            route_policy=_CHAT_POLICY,
            exposure=Exposure.SESSION_ONLY,
            run=_chat_propose("propose_setting"),
            callers=_RULES_AND_AI,
            needs=("agent", "turn"),
            max_output_bytes=None,  # Plan G's handler bounds the suggestion (the listed settings)
            mirrors=("POST", "/uploads/{upload_id}/agent-session/messages"),
            policy_kind="data.setting",
            wraps="engine/agent/loop.py _propose",
        )
    )
    registry.register(
        DataTool(
            number=48,
            name="propose_step",
            kind=DataToolKind.PROPOSE,
            description="Suggest one data fix from the fixed list, grounded in a look at the column, tried on "
            "1,000 rows; pending until the person decides.",
            input_model=ProposeStepArgs,
            output_model=ProposeOutput,
            route_policy=_CHAT_POLICY,
            exposure=Exposure.SESSION_ONLY,
            run=_chat_propose("propose_step"),
            callers=_RULES_AND_AI,
            needs=("agent", "turn"),
            max_output_bytes=None,  # Plan G's `check_recipe` bounds it (MAX_MAPPINGS = 500 spellings, ...)
            mirrors=("POST", "/uploads/{upload_id}/agent-session/messages"),
            policy_kind="data.recipe_step",
            wraps="engine/agent/loop.py _propose_step",
        )
    )
    registry.register(
        DataTool(
            number=49,
            name="edit_by_hand",
            kind=DataToolKind.PROPOSE,
            description="The person's own edit, as a step like every other: ticked, undone by unticking.",
            input_model=EditArgs,
            output_model=ProposeOutput,
            route_policy=_EDIT_POLICY,
            exposure=Exposure.SESSION_ONLY,
            run=_edit_by_hand,
            callers=frozenset({"person"}),
            needs=("agent", "session", "save_session"),
            max_output_bytes=None,  # `add_edit`'s own checks bound it, as the edits route has no other cap
            mirrors=("POST", "/uploads/{upload_id}/agent-session/edits"),
            policy_kind="data.person_edit",
            wraps="engine/agent/edits.py add_edit",
        )
    )
    wrappers: tuple[_Wrapper, ...] = (
        (
            10,
            "diagnose_files",
            "What each file is (customer list, bills, ...), its ID and date columns, rows and fixable columns.",
            DiagnoseArgs,
            DiagnosedFiles,
            _diagnose_files,
            (),
            {"available": _file_or_files, "wraps": "engine/doctor/diagnose.py:82 diagnose_file"},
        ),
        (
            29,
            "find_file_role",
            "Which role one file could play (customer list, bills, tickets, ...), with the reasons, and its ID columns.",
            FileArgs,
            FileRoles,
            _find_file_role,
            (),
            {"available": _file_or_files, "wraps": "engine/onboarding/sources.py:305 detect_roles"},
        ),
        (
            32,
            "advise",
            "The rules' suggestions and questions for the file being set up (no AI).",
            AdviseArgs,
            AdviceDigest,
            _advise,
            ("agent",),
            {"wraps": "engine/agent/advisor.py:1422 advise"},
        ),
        (
            33,
            "first_look",
            "The rate of 'yes' and the rate by the most informative columns, with small groups left out.",
            OutcomeArgs,
            FirstLookOutput,
            _first_look,
            ("agent",),
            {"wraps": "engine/doctor/first_look.py:308 outcome_look"},
        ),
        (
            34,
            "how_files_fit",
            "How the case's files meet: the share of each event file's IDs found in the customer list, and "
            "which files are the same report for different periods.",
            NoArgs,
            FilesFit,
            _how_files_fit,
            ("files",),
            {"wraps": "engine/doctor/joins.py:85 how_files_fit"},
        ),
        (
            36,
            "what_can_this_power",
            "Which use cases the files can power today: ready, check, missing or too small, with the reason.",
            NoArgs,
            PowerVerdicts,
            _what_can_this_power,
            (),
            {
                "available": _file_or_files,
                "wraps": "engine/doctor/diagnose.py:153 readiness",
                "route_policy": _VERDICT_POLICY,
                "exposure": Exposure.AGGREGATE,
            },
        ),
        (
            37,
            "enough_data",
            "What is short for the use case, by how much, what can be done now and what to collect.",
            OutcomeArgs,
            NextSteps,
            _enough_data,
            ("agent",),
            {"wraps": "engine/doctor/sufficiency.py:125 enough_data"},
        ),
        (
            38,
            "data_request",
            "The tables and columns to ask the data team for, per use case.",
            DataRequestArgs,
            DataRequest,
            _data_request,
            (),
            {
                "wraps": "engine/pilot/data_request.py:344 build_data_request",
                "route_policy": _DATA_REQUEST_POLICY,
                # Built from configuration alone, it holds no client data: not a row-level result.
                "max_output_bytes": AGGREGATE_BUDGET,
            },
        ),
        (
            40,
            "find_time_columns",
            "Columns that could be the date of each row: how many values read as dates, the range, and "
            "whether day and month are ambiguous.",
            FileArgs,
            TimeColumns,
            _find_time_columns,
            (),
            {
                "available": _file_or_files,
                "wraps": "engine/onboarding/sources.py:360 profile_source time_candidates",
            },
        ),
        (
            71,
            "recall_treatment",
            "The treatment of a file of the same shape approved before, if any (names only, never values).",
            NoArgs,
            RecalledTreatment,
            _recall_treatment,
            ("agent", "storage"),
            {"wraps": "engine/doctor/memory.py:231 offer_for"},
        ),
    )
    for number, name, description, args, output, run, needs, extra in wrappers:
        registry.register(
            DataTool(
                number=number,
                name=name,
                kind=DataToolKind.READ,
                description=description,
                input_model=args,
                output_model=output,
                route_policy=extra.get("route_policy", _DOCTOR_POLICY),
                exposure=extra.get("exposure", Exposure.SESSION_ONLY),
                run=run,
                callers=_RULES_AND_SCREEN,
                needs=needs,
                available=extra.get("available", lambda _ctx: True),
                max_output_bytes=extra.get("max_output_bytes", BY_EXPOSURE),
                wraps=extra["wraps"],
            )
        )
    # Plan O wave 1 integration (DEC-1802 (t)): the tools of M240, M238, M237, M241 and M234, rules and screen only.
    from engine.doctor.tools_wave_one import register_wave_one
    from engine.doctor.tools_wrappers import register_wrappers

    register_wave_one(registry)
    # Plan O M236 (DEC-1806): the remaining wrappers and `explain`, rules and screen only.
    register_wrappers(registry)
    # Plan O wave 2 integration: M242's `convert_currency` (#64, DEC-1812 (k); the person's only). M235's
    # `load_goal_brief` (#35) stays on `engine.doctor.visit.visit_registry`: it lists the AI as a caller, so here
    # it would change the chat's prompt with the switch on (docs/CROSS_BRANCH_REQUESTS.md).
    from engine.doctor.tools_wave_two import register_wave_two

    register_wave_two(registry)
    # Plan O M239 (DEC-1809): the outcome tools, rules and screen only.
    from engine.doctor.tools_outcomes import register_outcome_tools

    register_outcome_tools(registry)
    # Plan O M244 (DEC-1814): the time-settings tools, rules and screen only.
    from engine.doctor.tools_time import register_time_tools

    register_time_tools(registry)
