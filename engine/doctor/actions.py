"""The Data Doctor AI's actions, defined once: the prompts' action and argument lists are generated from here.

Each action the review or the chat takes has an :class:`ActionSpec` with a Pydantic model of its arguments. The prompts
(`configs/prompts/data_doctor_agent.v2.md`, `data_doctor_chat.v2.md`) no longer carry hand-written argument prose:
:func:`action_lines`, :func:`step_kind_lines` and :func:`engine.doctor.limits.prompt_limits` render it into the prompt
variables, as `{tools}` always was. The contract test (`tests/unit/doctor/test_prompt_contract.py`) holds the
prompts to this registry: every action and argument a prompt names is here, every action the dispatch handles is here,
and every number a prompt states is a limit.

The look tools are not here: they come from the data-tool registry with their own input models (`agent_review._tools`).
The models describe the arguments the dispatch reads; the dispatch (`agent_review._act`, `agent_chat._act`) is
unchanged, so an action stays as strict or as lenient as it was.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field

from engine.agent.contracts import RecipeStepKind
from engine.agent.loop import CHAT_STEP_KINDS, DOCTOR_STEP_KINDS
from engine.agent.recipe import STEP_PARAMETERS
from engine.agent.value_steps import REMOVE_OPERATORS
from engine.doctor.guards import Expectations
from engine.doctor.tools_sql import ProposeSqlStepArgs

__all__ = [
    "ACTIONS",
    "ALIASES",
    "ActionSpec",
    "action_lines",
    "action_names",
    "argument_names",
    "prompt_variables",
    "step_kind_lines",
]

Surface = Literal["review", "chat"]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sql(name: str) -> str:
    return ProposeSqlStepArgs.model_fields[name].description or ""


class ProposeStepArgs(_Args):
    kind: str = Field()
    column: str = Field()
    params: dict[str, Any] = Field(default_factory=dict, description="below")
    reason: str = Field(description="cite a rule id (R4) when one asks")
    evidence_ids: list[str] = Field(default_factory=list)
    replaces: str | None = Field(
        default=None,
        description="the id of your change this swaps out",
        json_schema_extra={"surface": "chat"},
    )


class ProposeSqlArgs(_Args):
    description: str = Field()
    sql: str = Field(description="one SELECT over `t_<file name>`; `$as_of` is the date")
    output_name: str = Field()
    files: list[str] = Field(
        default_factory=list, description="the tables it reads: F1.., or a table a query built (T1..)"
    )
    expect: Expectations | None = Field(default=None)
    as_of: str | None = Field(default=None, description="YYYY-MM-DD the person said")
    as_of_rule: Literal["latest_date_in_file", "last_complete_month_end"] | None = Field(
        default=None, description="a rule over `as_of_file`'s `as_of_column`"
    )
    as_of_file: str | None = Field(default=None)
    as_of_column: str | None = Field(default=None)
    reads_future: bool = Field(
        default=False,
        description="the outcome uses rows after `$as_of`",
    )
    outcome_window_days: int | None = Field(
        default=None,
        description="days the outcome looks past `$as_of`; the data must run that far",
    )
    replaces: str | None = Field(
        default=None,
        description="the id of your change this swaps out",
        json_schema_extra={"surface": "chat"},
    )


class ProposeFeaturesArgs(_Args):
    """`propose_features` (`engine.doctor.tools_features`): the file is the action's `file`; the outcome column and
    the as-of date are the case's (`decisions`), filled in by code, so no feature can read the outcome."""

    entity_key: str = Field()
    time_column: str = Field()
    preset: Literal["rfm", "rfm_plus", "rfm_ratios"] | None = Field(default=None)
    features: list[dict[str, Any]] = Field(
        default_factory=list, description="your own: name, agg, column, window_days, where"
    )
    amount_column: str | None = Field(default=None)
    quantity_column: str | None = Field(default=None)
    price_column: str | None = Field(default=None)
    order_column: str | None = Field(default=None)
    product_column: str | None = Field(default=None)
    output_name: str | None = Field(default=None)
    as_of: str | None = Field(default=None)
    as_of_rule: Literal["latest_date_in_file", "last_complete_month_end"] | None = Field(default=None)
    replaces: str | None = Field(
        default=None,
        description="the id of your change this swaps out",
        json_schema_extra={"surface": "chat"},
    )


class ProposeUnpivotArgs(_Args):
    """`propose_unpivot` (`engine.doctor.agent_unpivot`): code finds the period columns and builds the step."""

    reason: str | None = Field(default=None)


class ReadGuideArgs(_Args):
    """`read_guide` (`engine.doctor.guides`): read-only; the guide's rules, plus one section when named."""

    name: str = Field()
    section: str | None = Field(default=None)


class AskReviewArgs(_Args):
    question: str = Field(description="a question id below")
    column: str | None = Field(default=None)
    values: dict[str, Any] = Field(default_factory=dict)
    choices: list[str] = Field(default_factory=list)
    effects: list[str] = Field(default_factory=list, description="one short sentence per choice, no numbers")
    evidence_ids: list[str] = Field(default_factory=list)


class AskChatArgs(_Args):
    question: str | None = Field(default=None, description="a question id below")
    values: dict[str, Any] = Field(default_factory=dict)
    choices: list[str] = Field(default_factory=list)
    text: str | None = Field(default=None, description="a quick question instead of an id")
    options: list[str] = Field(default_factory=list, description="the quick question's options")
    why: str | None = Field(default=None)


class SuggestAnswerArgs(_Args):
    question_id: str = Field()
    option_id: str = Field(description="such as c0")


class ExpressDoubtArgs(_Args):
    because: str = Field()
    would_do: str = Field()
    if_not: str = Field()
    evidence_ids: list[str] = Field(default_factory=list)
    meaning: bool = Field(default=False, description="true if about a column's meaning: approval waits")


class RequestDataArgs(_Args):
    kind: Literal["more_data", "different_file"] = Field(default="more_data")
    what: str = Field()
    why: str = Field()
    columns: list[str] = Field(default_factory=list)
    per: str | None = Field(default=None)
    ask_whom: str | None = Field(default=None)


class CombineArgs(_Args):
    files: list[str] = Field(description="two or more, such as F1, F2")
    replaces: str | None = Field(
        default=None,
        description="the id of your change this swaps out",
        json_schema_extra={"surface": "chat"},
    )


class WithdrawArgs(_Args):
    item_id: str = Field(description="one of your own changes")


class ReplyArgs(_Args):
    text: str = Field(description="what you tell the person")


class FinishArgs(_Args):
    text: str = Field(description="what you found, suggest, want to know")


@dataclass(frozen=True)
class ActionSpec:
    """One action: its name, the model of its arguments, a one-line summary, and where it is offered."""

    name: str
    args_model: type[BaseModel] | None
    summary: str
    surfaces: frozenset[str]
    surface_models: tuple[tuple[str, type[BaseModel]], ...] = ()
    """Per-surface argument models where the two surfaces take different arguments."""
    top_level: tuple[str, ...] = ()
    """Fields at the top of the reply rather than in `args`."""
    needs_file: bool = False

    def model_for(self, surface: str) -> type[BaseModel] | None:
        return dict(self.surface_models).get(surface, self.args_model)


_BOTH: Final = frozenset({"review", "chat"})
ACTIONS: Final[tuple[ActionSpec, ...]] = (
    ActionSpec(
        "propose_step",
        ProposeStepArgs,
        "one typed change to one column; cite your looks.",
        _BOTH,
        needs_file=True,
    ),
    ActionSpec(
        "propose_sql",
        ProposeSqlArgs,
        "one SELECT building a new table.",
        _BOTH,
    ),
    ActionSpec(
        "propose_features",
        ProposeFeaturesArgs,
        "customer behaviour features, up to `$as_of`.",
        _BOTH,
        needs_file=True,
    ),
    ActionSpec(
        "propose_unpivot",
        ProposeUnpivotArgs,
        "a wide sheet (spend_jan..spend_dec) turned into one row per customer per period: the person is asked, and "
        "says the year when the names have none.",
        frozenset({"review"}),
        needs_file=True,
    ),
    ActionSpec(
        "read_guide",
        ReadGuideArgs,
        "a guide: its rules plus the section (else all).",
        _BOTH,
    ),
    ActionSpec("join_files", CombineArgs, "join the files on their key; code counts.", frozenset({"chat"})),
    ActionSpec(
        "stack_files", CombineArgs, "stack files of the same shape; code counts.", frozenset({"chat"})
    ),
    ActionSpec(
        "ask",
        AskReviewArgs,
        "ask the person.",
        _BOTH,
        surface_models=(("review", AskReviewArgs), ("chat", AskChatArgs)),
        needs_file=True,
    ),
    ActionSpec(
        "suggest_answer",
        SuggestAnswerArgs,
        "suggest the answer that matches the person's words; they click to confirm.",
        frozenset({"chat"}),
    ),
    ActionSpec(
        "express_doubt",
        ExpressDoubtArgs,
        "say you are unsure.",
        _BOTH,
        needs_file=True,
    ),
    ActionSpec(
        "request_data",
        RequestDataArgs,
        "a missing column or file.",
        _BOTH,
        needs_file=True,
    ),
    ActionSpec("withdraw", WithdrawArgs, "take back one of your own changes.", frozenset({"chat"})),
    ActionSpec(
        "reply",
        ReplyArgs,
        "what you tell the person; always end with it.",
        frozenset({"chat"}),
        top_level=("text",),
    ),
    ActionSpec(
        "finish",
        FinishArgs,
        "3 to 5 plain sentences.",
        frozenset({"review"}),
        top_level=("text",),
    ),
)
ALIASES: Final[dict[str, str]] = {"ask_question": "ask"}
"""Names a stored turn or an older prompt used: the review's `ask_question` is `ask`."""


def action_names(surface: str | None = None) -> tuple[str, ...]:
    """The names offered on `surface` (all surfaces when None), without aliases."""
    return tuple(a.name for a in ACTIONS if surface is None or surface in a.surfaces)


def argument_names(name: str) -> frozenset[str]:
    """Every argument any surface of the action takes (top-level fields included)."""
    spec = next(a for a in ACTIONS if a.name == ALIASES.get(name, name))
    models = {spec.args_model, *(m for _s, m in spec.surface_models)} - {None}
    return frozenset(f for m in models if m is not None for f in m.model_fields) | frozenset(spec.top_level)


def _type_of(annotation: Any) -> str:
    origin = get_origin(annotation)
    if annotation is bool:
        return "true|false"
    if annotation is int:
        return "int"
    if origin is Literal:
        return "|".join(str(v) for v in get_args(annotation))
    args = [a for a in get_args(annotation) if a is not type(None)]
    if origin is not None and len(args) == 1 and origin is not list and origin is not dict:
        return _type_of(args[0])  # Optional[X]
    if origin is list or origin is tuple:
        return "[" + _type_of(args[0]) + "]"
    if origin is dict or annotation is dict:
        return "{...}"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return (
            "{" + ", ".join(annotation.model_fields) + "}"
        )  # names only: the types are in the model's checks
    if len(args) > 1:
        return "|".join(_type_of(a) for a in args)
    return "text"


def _surface_of(info: Any, default: str) -> Any:
    """The one surface a field is offered on (`json_schema_extra={"surface": ...}`), else `default`."""
    extra = info.json_schema_extra
    return extra.get("surface", default) if isinstance(extra, dict) else default


def _fields(model: type[BaseModel], skip: tuple[str, ...], surface: str) -> str:
    parts = []
    for name, info in model.model_fields.items():
        if name in skip or _surface_of(info, surface) != surface:
            continue
        mark = "" if info.is_required() else "?"
        tail = f" - {info.description}" if info.description else ""
        kind = _type_of(info.annotation)
        parts.append(f"`{name}{mark}`" + ("" if kind == "text" else f" {kind}") + tail)
    return "; ".join(parts)


def action_lines(surface: str, *, callable_: Callable[[ActionSpec], bool] | None = None) -> list[str]:
    """One line per action offered on `surface`, generated from the argument models: its reply shape, its arguments
    (`?` marks an optional one) and what it does. `callable_` is the same availability predicate the dispatch uses.
    """
    lines = []
    for spec in ACTIONS:
        if surface not in spec.surfaces or (callable_ is not None and not callable_(spec)):
            continue
        model = spec.model_for(surface)
        assert model is not None
        name = "ask_question" if (surface == "review" and spec.name == "ask") else spec.name
        head = f'{{"action": "{name}"' + (', "file": "F1"' if spec.needs_file else "")
        if spec.top_level:
            shape = head + ", " + ", ".join(f'"{f}": "..."' for f in spec.top_level) + "}"
            body = ""
        else:
            shape = head + ', "args": {...}}'
            body = " Args: " + _fields(model, spec.top_level, surface) + "."
        lines.append(f"`{shape}` - {spec.summary}{body}")
    return lines


def step_kind_lines(surface: str) -> list[str]:
    """The step kinds the surface may propose, each with the parameters it takes; `remove_rows` also its operators."""
    kinds: frozenset[RecipeStepKind] = DOCTOR_STEP_KINDS if surface == "chat" else CHAT_STEP_KINDS
    lines = []
    for kind in sorted(kinds, key=lambda k: k.value):
        names = sorted(STEP_PARAMETERS.get(kind, frozenset()))
        line = kind.value + (f" ({', '.join(names)})" if names else "")
        if kind is RecipeStepKind.REMOVE_ROWS:
            line += "; operator is one of " + ", ".join(sorted(REMOVE_OPERATORS))
        lines.append(line)
    return lines


def prompt_variables(
    surface: str,
    config: Any = None,
    *,
    callable_: Callable[[ActionSpec], bool] | None = None,
    config_root: Any = None,
) -> dict[str, Any]:
    """The generated variables of a Doctor prompt: `actions`, `step_kinds`, `doctor_limits` and `guide_index`."""
    from engine.doctor.guides import index_text
    from engine.doctor.limits import prompt_limits

    return {
        "actions": action_lines(surface, callable_=callable_),
        "step_kinds": step_kind_lines(surface),
        "doctor_limits": prompt_limits(config),
        "guide_index": index_text(config_root),
    }
