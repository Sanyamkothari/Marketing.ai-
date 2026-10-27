"""The outcome tools (Plan O M239, DEC-1809; the design §5.5 D, §6.6, §9.2).

| # | Tool | Kind | What it does |
|---|---|---|---|
| 45 | `define_outcome` | PROPOSE (APPROVE) | the outcome from an event file: presence, absence or a value threshold, with grace, files that leave a customer out, and censoring; measured by the builder's `build_labels` |
| 46 | `map_outcome_values` | PROPOSE (APPROVE) | an outcome column's values mapped to yes, no or leave out (`column_values`) |
| 47 | `check_target_type` | READ | whether the outcome column can be learnt as the use case asks: a regression outcome is numbers (`X_REGRESSION_TARGET_NUMERIC`), a yes/no outcome has two values |

Both PROPOSE tools return a pending, never-ticked, `check` proposal of an `OutcomeDefinition` with the exact sentence
the person approves (M233's `data.define_outcome` and `data.map_outcome_values` rule sets: APPROVE-class, always).
Whatever decides the meaning and is not given - a value the rules cannot place, a second event file or date column,
the date a file that leaves customers out is read on, the window - is a blocking question and no definition is
proposed (`engine.doctor.outcome_definition`); every row that counts for nothing is counted in the sentence. Nothing
here applies anything: `apply_outcome_values` and the dataset builder do, after the person approves.

The tools serve the rules and the screen, like M236's; the AI reaches them through the visit (M235). Each is
`session-only` (the catalogue) and needs an Analyst.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.contracts import Question
from engine.config import StrictBase
from engine.doctor.outcome_definition import (
    MAX_OUTCOME_VALUES,
    OutcomeCounts,
    OutcomeDefinition,
    OutcomeError,
    ValueCount,
    WindowChoice,
)
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

if TYPE_CHECKING:
    from engine.doctor.outcomes import OutcomeFile

__all__ = ["M239_TOOLS", "register_outcome_tools"]

M239_TOOLS: Final[tuple[str, ...]] = ("define_outcome", "map_outcome_values", "check_target_type")
"""Every tool this module registers, in catalogue order."""

RULES_AND_SCREEN: Final[frozenset[str]] = frozenset({"rules", "person"})
_MAX_EXCLUDE_FILES: Final[int] = 5


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class _Pending(StrictBase):
    """What an outcome proposal carries for the policy (M233): who proposed it; never ticked; never `sure`."""

    source: Literal["rules", "person"] = "rules"
    ticked: Literal[False] = False
    confidence: Literal["check"] = "check"
    state: Literal["pending"] = "pending"


def _refused(exc: OutcomeError) -> DataToolError:
    return DataToolError(exc.code, exc.message, status=422)


def _decided_target(ctx: DataToolContext) -> str | None:
    """The outcome column decided in the session or the file's context, else the use case's when the file has it.
    Never guessed from the values."""
    from engine.agent.proposals import roles_of

    agent = ctx.require_agent()
    if agent.target:
        return agent.target
    if ctx.session is not None:
        _key, target = roles_of(ctx.session)
        if target:
            return target
    named = agent.config.target.column
    return named if named and named in agent.frame.columns else None


def _column(ctx: DataToolContext, given: str | None) -> str:
    agent = ctx.require_agent()
    name = given or _decided_target(ctx)
    if name is None:
        raise DataToolError(
            "TOOL_INPUT_INVALID", "Name the outcome column; none is decided for this file yet.", status=422
        )
    return agent.resolve(name)


def _personal(ctx: DataToolContext, column: str) -> bool:
    agent = ctx.require_agent()
    marked = {c.name for c in agent.profile.columns if c.pii_kinds}
    return column in marked or column in set(agent.config.agent.always_hide_columns)


# ---------------------------------------------------------------------------
# #46 map_outcome_values
# ---------------------------------------------------------------------------
class MapValuesArgs(_Args):
    column: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="The outcome column; the one decided for the file when omitted.",
    )
    map: dict[str, Literal["yes", "no", "exclude"]] | None = Field(
        default=None,
        max_length=MAX_OUTCOME_VALUES,
        description="The person's answers: a value of the column, as shown, and what it means. Values left out "
        "keep the rules' suggestion, or are asked.",
    )


class MapValuesOutput(_Pending):
    column: str
    rows: int = Field(description="Rows of the file read.")
    empty: int = Field(description="Rows with no value: left out of an outcome.")
    values: tuple[ValueCount, ...] = Field(description="Every value of the column, its rows and its meaning.")
    definition: OutcomeDefinition | None = Field(
        description="The `column_values` definition proposed; null while a value has no meaning."
    )
    definition_hash: str | None
    counts: OutcomeCounts | None
    sentence: str | None = Field(
        description="The exact sentence the person approves; null with no definition."
    )
    questions: tuple[Question, ...] = Field(description="One blocking question per value with no meaning.")
    questions_total: int
    blocking: tuple[str, ...]
    summary: str


def _map_outcome_values(parsed: MapValuesArgs, ctx: DataToolContext) -> MapValuesOutput:
    from engine.doctor.outcome_definition import propose_value_map
    from engine.doctor.tools import _fitted

    agent = ctx.require_agent()
    column = _column(ctx, parsed.column)
    if column == ctx.primary_key:
        raise DataToolError("TOOL_INPUT_INVALID", "The ID column is never the outcome.", status=422)
    if _personal(ctx, column):
        raise DataToolError(
            "TOOL_INPUT_INVALID", "A column holding personal data is never the outcome.", status=422
        )
    try:
        found = propose_value_map(
            agent.frame, column, positive_label=agent.config.target.positive_label, given=parsed.map
        )
    except OutcomeError as exc:
        raise _refused(exc) from exc
    by_person = bool(parsed.map) and ctx.caller == "person"
    output = MapValuesOutput(
        source="person" if by_person else "rules",
        column=column,
        rows=found.rows,
        empty=found.empty,
        values=found.values,
        definition=found.definition,
        definition_hash=None if found.definition is None else found.definition.definition_hash,
        counts=found.counts,
        sentence=found.sentence,
        questions=found.questions,
        questions_total=len(found.questions),
        blocking=found.blocking,
        summary=found.summary,
    )
    return _fitted(output, ("questions",))


# ---------------------------------------------------------------------------
# #45 define_outcome
# ---------------------------------------------------------------------------
class DefineOutcomeArgs(_Args):
    kind: Literal["event_presence", "event_absence", "value_threshold"] = Field(
        description="Yes when an event happens in the window, when none does, or when an event's value passes a "
        "threshold."
    )
    event_upload_id: str | None = Field(
        default=None, max_length=128, description="The event file; asked when the case has several."
    )
    date_column: str | None = Field(
        default=None, max_length=512, description="Its date column; asked when it has several."
    )
    days: int | None = Field(
        default=None, ge=1, le=3650, description="The window after the snapshot; asked when omitted."
    )
    grace_days: int | None = Field(default=None, ge=1, le=365, description="Days' grace added to the window.")
    exclude_upload_ids: tuple[str, ...] = Field(
        default=(),
        max_length=_MAX_EXCLUDE_FILES,
        description="Event files whose events in the window leave a customer out (a port-out, a move).",
    )
    exclude_date_columns: dict[str, str] = Field(
        default_factory=dict,
        max_length=_MAX_EXCLUDE_FILES,
        description="The date column each of those files is read on, by upload id; asked when a file has several.",
    )
    column: str | None = Field(
        default=None, max_length=512, description="value_threshold: the column tested."
    )
    operator: Literal[">", ">=", "<", "<="] | None = Field(default=None, description="value_threshold.")
    threshold: float | None = Field(default=None, description="value_threshold: the number.")

    @model_validator(mode="after")
    def _threshold(self) -> DefineOutcomeArgs:
        named = (self.column, self.operator, self.threshold)
        if self.kind == "value_threshold" and any(value is None for value in named):
            raise ValueError("a value_threshold outcome names column, operator and threshold")
        if self.kind != "value_threshold" and any(value is not None for value in named):
            raise ValueError("only a value_threshold outcome names column, operator and threshold")
        if any(len(key) > 128 or len(value) > 512 for key, value in self.exclude_date_columns.items()):
            raise ValueError("an upload id or a date column name is too long")
        return self


class DefineOutcomeOutput(_Pending):
    kind: str
    entity_file_name: str | None
    event_file_name: str | None
    data_end: date | None
    definition: OutcomeDefinition | None = Field(description="Null while something that decides it is asked.")
    definition_hash: str | None
    counts: OutcomeCounts | None
    sentence: str | None = Field(
        description="The exact sentence the person approves; null with no definition."
    )
    choices: tuple[WindowChoice, ...] = Field(description="Each window offered, measured.")
    questions: tuple[Question, ...]
    blocking: tuple[str, ...]
    notes: tuple[str, ...]


def _outcome_files(ctx: DataToolContext) -> list[OutcomeFile]:
    """The case's files, diagnosed as the Doctor route diagnoses them (its row caps), for M157's measuring."""
    from dataclasses import replace

    from engine.doctor.diagnose import ADVISE_ROWS, diagnose_file
    from engine.doctor.outcomes import OutcomeFile
    from engine.doctor.tools import DIAGNOSIS_FILES, DIAGNOSIS_ROWS, _config, _roles
    from engine.stages.ingest import profile_row_cap

    files = list(ctx.files)[:DIAGNOSIS_FILES]
    config, roles = _config(ctx), _roles(ctx)
    cap = min(profile_row_cap(config), max(ADVISE_ROWS, DIAGNOSIS_ROWS // max(len(files), 1)))
    out: list[OutcomeFile] = []
    for loaded in files:
        capped = loaded if len(loaded.frame) <= cap else replace(loaded, frame=loaded.frame.head(cap))
        out.append(OutcomeFile(diagnosis=diagnose_file(capped, config, roles), frame=capped.frame))
    return out


def _define_outcome(parsed: DefineOutcomeArgs, ctx: DataToolContext) -> DefineOutcomeOutput:
    from engine.doctor.outcome_definition import propose_event_outcome
    from engine.doctor.outcomes import load_outcome_catalogue
    from engine.doctor.tools import _config, _roles, _root

    try:
        found = propose_event_outcome(
            _outcome_files(ctx),
            _config(ctx),
            _roles(ctx),
            load_outcome_catalogue(_root(ctx)),
            kind=parsed.kind,
            event_upload_id=parsed.event_upload_id,
            date_column=parsed.date_column,
            days=parsed.days,
            grace_days=parsed.grace_days,
            exclude_upload_ids=parsed.exclude_upload_ids,
            exclude_date_columns=parsed.exclude_date_columns,
            column=parsed.column,
            operator=parsed.operator,
            threshold=parsed.threshold,
        )
    except OutcomeError as exc:
        raise _refused(exc) from exc
    return DefineOutcomeOutput(
        kind=parsed.kind,
        entity_file_name=found.entity_file_name,
        event_file_name=found.event_file_name,
        data_end=found.data_end,
        definition=found.definition,
        definition_hash=None if found.definition is None else found.definition.definition_hash,
        counts=found.counts,
        sentence=found.sentence,
        choices=found.choices,
        questions=found.questions,
        blocking=found.blocking,
        notes=found.notes,
    )


# ---------------------------------------------------------------------------
# #47 check_target_type
# ---------------------------------------------------------------------------
class TargetTypeArgs(_Args):
    column: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="The outcome column; the one decided for the file when omitted.",
    )


class TargetType(StrictBase):
    column: str
    problem_type: str = Field(description="What the use case learns: binary_classification, regression, ...")
    values: int = Field(description="Rows with a value.")
    distinct: int = Field(description="Different values.")
    not_numbers: int = Field(description="Values that are not numbers.")
    verdict: Literal["ok", "not_ready", "needs_outcome_definition"] = Field(
        description="`not_ready`: the run would stop for this column's type; `needs_outcome_definition`: the "
        "values must first be mapped to yes and no (map_outcome_values) or read as an amount."
    )
    code: str | None = Field(
        description="X_REGRESSION_TARGET_NUMERIC, TARGET_NOT_BINARY or TARGET_CONSTANT; null when ok."
    )
    switch_to: str | None = Field(
        description="`regression` when a yes/no use case's outcome is many numbers (the run's own hint)."
    )
    next_tool: str | None = Field(description="The tool that settles it, when one does.")
    summary: str = Field(description="What was found, in plain words.")
    next_step: str = Field(description="What to do, when it is not ok.")


def _check_target_type(parsed: TargetTypeArgs, ctx: DataToolContext) -> TargetType:
    """The run's own rules for the outcome column's type: the readiness probe for a regression outcome
    (`X_REGRESSION_TARGET_NUMERIC`, so the tool and the report cannot disagree) and `TARGET_NOT_BINARY`'s count of
    values for a yes/no one. It reads; it proposes nothing."""
    import pandas as pd

    from engine.config import ProblemType, RunMode
    from engine.doctor.outcome_definition import value_text
    from engine.doctor.readiness_probes import _regression_target_numeric, _State

    agent = ctx.require_agent()
    column = _column(ctx, parsed.column)
    series = agent.frame[column]
    filled = series.dropna()
    numbers = pd.to_numeric(filled, errors="coerce") if not pd.api.types.is_bool_dtype(filled.dtype) else None
    not_numbers = len(filled) if numbers is None else int(numbers.isna().sum())
    distinct = int(filled.astype(object).map(value_text).dropna().nunique())
    problem = agent.config.problem_type
    base: dict[str, Any] = {
        "column": column,
        "problem_type": problem.value,
        "values": len(filled),
        "distinct": distinct,
        "not_numbers": not_numbers,
    }
    if problem is ProblemType.REGRESSION:
        state = _State(
            config=agent.config,
            mode=RunMode.TRAIN,
            frame=agent.frame,
            key_columns=(),
            target=column,
            row_count=len(agent.frame),
            scan=None,
        )
        _regression_target_numeric(state)
        probe = state.results["X_REGRESSION_TARGET_NUMERIC"]
        failed = probe.status == "fail"
        return TargetType(
            **base,
            verdict="not_ready" if failed else "ok",
            code="X_REGRESSION_TARGET_NUMERIC" if failed else None,
            switch_to=None,
            next_tool=None,
            summary=probe.message or "The outcome is not checked here.",
            next_step=probe.fix,
        )
    if problem is ProblemType.BINARY_CLASSIFICATION:
        if distinct == 2:
            return TargetType(
                **base,
                verdict="ok",
                code=None,
                switch_to=None,
                next_tool=None,
                summary=f"'{column}' has two values: one is yes, the other no.",
                next_step="",
            )
        if distinct < 2:
            return TargetType(
                **base,
                verdict="not_ready",
                code="TARGET_CONSTANT",
                switch_to=None,
                next_tool=None,
                summary=f"Every row of '{column}' has the same outcome; there is nothing to tell apart.",
                next_step="Choose a column with both outcomes, or define the outcome from events (define_outcome).",
            )
        numeric = not_numbers == 0
        if numeric and distinct > MAX_OUTCOME_VALUES:
            return TargetType(
                **base,
                verdict="needs_outcome_definition",
                code="TARGET_NOT_BINARY",
                switch_to="regression",
                next_tool=None,
                summary=f"'{column}' holds {distinct:,} different numbers, not yes and no.",
                next_step="Predict it as an amount (regression), or say which numbers mean yes.",
            )
        return TargetType(
            **base,
            verdict="needs_outcome_definition",
            code="TARGET_NOT_BINARY",
            switch_to=None,
            next_tool="map_outcome_values" if distinct <= MAX_OUTCOME_VALUES else None,
            summary=f"'{column}' has {distinct:,} different values; a yes/no model needs exactly two.",
            next_step=(
                "Say which values mean yes, which mean no and which are left out (map_outcome_values)."
                if distinct <= MAX_OUTCOME_VALUES
                else "Choose the column that records the outcome, or define it from events (define_outcome)."
            ),
        )
    return TargetType(
        **base,
        verdict="ok",
        code=None,
        switch_to=None,
        next_tool=None,
        summary=f"This use case learns a {problem.value.replace('_', ' ')} outcome; its type is checked by its own run.",
        next_step="",
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
_Entry = tuple[
    int, str, DataToolKind, str, type[BaseModel], type[BaseModel], Callable[..., Any], tuple[Need, ...]
]


def _entries() -> tuple[_Entry, ...]:
    read, propose = DataToolKind.READ, DataToolKind.PROPOSE
    return (
        (
            45,
            "define_outcome",
            propose,
            "Propose the outcome from an event file: an event in the window, none, or an event's value past a "
            "threshold; with grace, files that leave a customer out, and month-ends not over left out. Measured "
            "by the dataset builder; approved with its exact sentence.",
            DefineOutcomeArgs,
            DefineOutcomeOutput,
            _define_outcome,
            ("files",),
        ),
        (
            46,
            "map_outcome_values",
            propose,
            "Propose what each value of the outcome column means: yes, no or leave out. A value the rules cannot "
            "place is asked; approved with its exact sentence. No recipe step is written.",
            MapValuesArgs,
            MapValuesOutput,
            _map_outcome_values,
            ("agent",),
        ),
        (
            47,
            "check_target_type",
            read,
            "Whether the outcome column can be learnt as the use case asks: numbers for an amount, two values "
            "for yes/no.",
            TargetTypeArgs,
            TargetType,
            _check_target_type,
            ("agent",),
        ),
    )


_WRAPS: Final[Mapping[str, str]] = {
    "define_outcome": "engine/onboarding/labels.py:464 build_labels; engine/doctor/outcomes.py:586 offer_outcome",
    "map_outcome_values": "engine/stages/validate.py:712 resolve_positive_label; TARGET_NOT_BINARY :1009",
    "check_target_type": "engine/doctor/readiness_probes.py X_REGRESSION_TARGET_NUMERIC; validate.py:1009",
}


def register_outcome_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register M239's tools in `registry`; their names, in catalogue order."""
    from engine.doctor.policy import policy_kind_for

    names: list[str] = []
    for number, name, kind, description, args, output, run, needs in _entries():
        proposes = kind is DataToolKind.PROPOSE
        registry.register(
            DataTool(
                number=number,
                name=name,
                kind=kind,
                description=description,
                input_model=args,
                output_model=output,
                route_policy=RoutePolicy(
                    role=Role.ANALYST,
                    action=f"doctor.{name}",
                    purpose=(
                        "ask Data Doctor for a suggestion" if proposes else "ask Data Doctor about the data"
                    ),
                ),
                exposure=Exposure.SESSION_ONLY,
                run=run,
                callers=RULES_AND_SCREEN,
                needs=needs,
                max_output_bytes=BY_EXPOSURE,
                policy_kind=policy_kind_for(name) if proposes else None,
                wraps=_WRAPS[name],
            )
        )
        names.append(name)
    return tuple(names)
