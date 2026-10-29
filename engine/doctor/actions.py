"""The Data Doctor AI's actions, defined once: the prompt's action and argument lists are generated from here.

There is one turn (`engine.doctor.agent_turn.DoctorTurn`), one prompt (`configs/prompts/data_doctor.v3.md`) and one
action set: every action has an :class:`ActionSpec` with a Pydantic model of its arguments. The prompt carries no
hand-written argument prose: :func:`action_lines`, :func:`step_kind_lines` and
:func:`engine.doctor.limits.prompt_limits` render it into the prompt variables, as `{tools}` always was. The contract
test (`tests/unit/doctor/test_prompt_contract.py`) holds the prompt to this registry: every action and argument it
names is here, every action the dispatch handles is here, and every number it states is a limit.

The look tools are not here: they come from the data-tool registry with their own input models
(`DoctorTurn._tools`). The models describe the arguments the dispatch reads; the dispatch (`DoctorTurn._act`) is
unchanged, so an action stays as strict or as lenient as it was. Every action that changes the plan becomes an
`AuthorAction` and goes through `engine.doctor.author.apply_actions`; `replaces` names the item a new change takes
the place of, and `withdraw` takes any item out: what is in the plan or the person's waits for their yes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field

from engine.agent.contracts import RecipeStepKind
from engine.agent.loop import DOCTOR_STEP_KINDS
from engine.agent.recipe import STEP_PARAMETERS
from engine.agent.value_steps import REMOVE_OPERATORS
from engine.doctor.guards import Expectations

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


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_REPLACES: Final[str] = "id of the change this replaces"


class ProposeStepArgs(_Args):
    kind: str = Field()
    column: str = Field()
    params: dict[str, Any] = Field(default_factory=dict, description="below")
    reason: str = Field(description="cite a rule id (R4) when one asks")
    evidence_ids: list[str] = Field(default_factory=list)
    replaces: str | None = Field(default=None, description=_REPLACES)


class ProposeSqlArgs(_Args):
    description: str = Field(description="what the query does, in plain words")
    sql: str = Field(description="one SELECT over `t_<file name>`; `$as_of` is the date")
    output_name: str = Field()
    files: list[str] = Field(
        default_factory=list, description="the tables it reads: F1.., or a query's table (T1..)"
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
    replaces: str | None = Field(default=None, description=_REPLACES)


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
    replaces: str | None = Field(default=None, description=_REPLACES)


class ProposeCombineRowsArgs(_Args):
    """`propose_combine_rows` (`engine.doctor.tools_reshape`): the file is the action's `file`. Code drafts the
    per-column plan (its answer's `plan`); the model changes rows of it with a reason, and the tool checks them.
    """

    time_column: str | None = Field(default=None)
    snapshot_column: str | None = Field(default=None)
    carry: list[str] = Field(default_factory=list, description="one value per ID")
    columns: list[dict[str, Any]] = Field(
        default_factory=list, description="edits: column, functions, reason"
    )
    replaces: str | None = Field(default=None, description=_REPLACES)


class ProposeUnpivotArgs(_Args):
    """`propose_unpivot` (`engine.doctor.agent_unpivot`): code lists the period-column groups (in its answer) and
    checks the group and year the model proposes; the person answers the question it asks."""

    reason: str | None = Field(default=None)
    columns: list[str] | None = Field(default=None, description="a group from its answer")
    year: int | None = Field(default=None, description="undated months; evidence in reason")


class ProposeAmountOutcomeArgs(_Args):
    """`propose_amount_outcome` (`engine.doctor.amount_outcome`): the spend after the snapshot, as you define it from
    `describe_order_file`; the person approves the sentence, which says every choice."""

    entity_key: str = Field()
    time_column: str = Field()
    amount_column: str = Field(description="or the unit price, with quantity_column")
    quantity_column: str | None = Field(default=None)
    window_days: int = Field()
    returns: Literal["net", "leave_out"] = Field(description="a negative row: refund netted in, or left out")
    as_of_rule: Literal["latest_date_in_file", "last_complete_month_end"] | None = Field(
        default=None, description="only when the case has no as-of date and the person agreed"
    )
    reason: str = Field()
    replaces: str | None = Field(default=None, description=_REPLACES)


class ProposeTestColumnArgs(_Args):
    """`propose_test_column` (`engine.doctor.agent_amount`): the column of a campaign test and its control group, from
    `find_test_columns`; the person confirms both before the balance is checked."""

    column: str = Field()
    control: str = Field(description="the control group's level id, such as L2")
    reason: str = Field()
    replaces: str | None = Field(default=None, description=_REPLACES)


class ProposeGoalArgs(_Args):
    """`propose_goal` (`engine.doctor.agent_turn`): a goal that fits the files, offered with a reason; the person's
    click on "Choose this goal" sets it. Nothing is chosen by this action."""

    goal_id: str = Field(description="a goal that fits (ready, check or buildable), by its id")
    reason: str = Field(
        description="why this goal, one sentence of at most {effect_chars} characters; a longer one is refused"
    )


class ProposeSettingArgs(_Args):
    """`propose_setting` (`engine.doctor.author.SetSetting`): one of the chosen goal's settings, to a value its
    config allows (`allowed` in the state). The person's yes puts it in the plan; the build is made again."""

    setting: str = Field(description="a setting of the chosen goal, such as window_days")
    value: str = Field(description="one of its allowed values")
    reason: str = Field()


class AdoptSuggestionArgs(_Args):
    """`adopt_suggestion` (`engine.doctor.author`): a change a fact tool suggested, put in the plan as it stands."""

    id: str = Field(description="the id of a suggestion in the state")
    reason: str = Field()


class ReadGuideArgs(_Args):
    """`read_guide` (`engine.doctor.guides`): read-only; the guide's rules, plus one section when named."""

    name: str = Field()
    section: str | None = Field(default=None)


class ProposeLessonArgs(_Args):
    """`propose_lesson` (`engine.doctor.lessons`): the owner decides; nothing reaches a guide before they approve."""

    text: str = Field(description="one general rule, at most 18 words, no number, quote or name")


class AskArgs(_Args):
    question: str | None = Field(default=None, description="a question id below")
    column: str | None = Field(default=None)
    values: dict[str, Any] = Field(default_factory=dict)
    choices: list[str] = Field(default_factory=list)
    effects: list[str] = Field(default_factory=list, description="one short sentence per choice, no numbers")
    evidence_ids: list[str] = Field(default_factory=list)
    text: str | None = Field(
        default=None,
        description="a quick question instead of an id, at most {short_chars} characters, not about consent, the "
        "outcome, the ID or the date",
    )
    options: list[str] = Field(
        default_factory=list,
        description="the quick question's options, each at most {short_chars} characters, no numbers; "
        '"I don\'t know" is added for you',
    )
    why: str | None = Field(default=None, description="at most {short_chars} characters")


class SuggestAnswerArgs(_Args):
    question_id: str = Field(description="a question's id from the state")
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
    replaces: str | None = Field(default=None, description=_REPLACES)


class WithdrawArgs(_Args):
    item: str = Field(description="any change in the plan, by its id in the state")
    reason: str = Field()
    consequence: str = Field(description="what changes in the training table without it")


class ReplyArgs(_Args):
    text: str = Field(description="what you tell the person; at most {chat_reply_chars} characters")


@dataclass(frozen=True)
class ActionSpec:
    """One action: its name, the model of its arguments and a one-line summary."""

    name: str
    args_model: type[BaseModel]
    summary: str
    top_level: tuple[str, ...] = ()
    """Fields at the top of the reply rather than in `args`."""
    needs_file: bool = False


ACTIONS: Final[tuple[ActionSpec, ...]] = (
    ActionSpec(
        "propose_step",
        ProposeStepArgs,
        "one typed change to one column (also leaving rows out by a rule); cite your looks.",
        needs_file=True,
    ),
    ActionSpec("propose_sql", ProposeSqlArgs, "one SELECT building a new table."),
    ActionSpec(
        "propose_features",
        ProposeFeaturesArgs,
        "customer behaviour features, up to `$as_of`.",
        needs_file=True,
    ),
    ActionSpec(
        "propose_combine_rows",
        ProposeCombineRowsArgs,
        "one row per ID; edit its plan.",
        needs_file=True,
    ),
    ActionSpec(
        "propose_unpivot",
        ProposeUnpivotArgs,
        "period columns (spend_jan..) as rows.",
        needs_file=True,
    ),
    ActionSpec(
        "propose_amount_outcome",
        ProposeAmountOutcomeArgs,
        "an amount outcome (spend in N days after the snapshot) from a file of orders; say why.",
        needs_file=True,
    ),
    ActionSpec(
        "propose_test_column",
        ProposeTestColumnArgs,
        "which column holds a campaign test and which group is the control; the person confirms.",
        needs_file=True,
    ),
    ActionSpec("join_files", CombineArgs, "join the files on their key; code counts."),
    ActionSpec("stack_files", CombineArgs, "stack files of the same shape; code counts."),
    ActionSpec(
        "propose_setting",
        ProposeSettingArgs,
        "a setting of the chosen goal to an allowed value; the person says yes.",
    ),
    ActionSpec(
        "adopt_suggestion",
        AdoptSuggestionArgs,
        "put a change a fact tool suggested in the plan as it stands; only this makes you sure of it.",
    ),
    ActionSpec(
        "withdraw",
        WithdrawArgs,
        "take any change out of the plan, yours or not; one in the plan waits for the person's yes.",
    ),
    ActionSpec(
        "propose_goal",
        ProposeGoalArgs,
        "with no goal yet: one goal that fits the files (or can be built from them); the person confirms.",
    ),
    ActionSpec("ask", AskArgs, "ask the person.", needs_file=True),
    ActionSpec(
        "suggest_answer",
        SuggestAnswerArgs,
        "suggest the answer that matches the person's words; they click to confirm.",
    ),
    ActionSpec("express_doubt", ExpressDoubtArgs, "say you are unsure.", needs_file=True),
    ActionSpec("request_data", RequestDataArgs, "a missing column or file.", needs_file=True),
    ActionSpec("read_guide", ReadGuideArgs, "a guide: its rules plus the section (else all)."),
    ActionSpec(
        "propose_lesson",
        ProposeLessonArgs,
        "once the case is ready: a general rule for later cases; the owner approves it.",
    ),
    ActionSpec(
        "reply",
        ReplyArgs,
        "what you tell the person, in plain words; always end with it.",
        top_level=("text",),
    ),
)
ALIASES: Final[dict[str, str]] = {"ask_question": "ask", "finish": "reply"}
"""Names a stored turn or an older prompt used: the review's `ask_question` is `ask`, its `finish` is `reply`."""


def action_names() -> tuple[str, ...]:
    """The names offered, without aliases."""
    return tuple(a.name for a in ACTIONS)


def argument_names(name: str) -> frozenset[str]:
    """Every argument the action takes (top-level fields included)."""
    spec = next(a for a in ACTIONS if a.name == ALIASES.get(name, name))
    return frozenset(spec.args_model.model_fields) | frozenset(spec.top_level)


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


def _fields(model: type[BaseModel], skip: tuple[str, ...], numbers: Mapping[str, int | float]) -> str:
    parts = []
    for name, info in model.model_fields.items():
        if name in skip:
            continue
        mark = "" if info.is_required() else "?"
        described = info.description or ""
        for limit, value in numbers.items():  # a limit a description states is the configured one
            described = described.replace("{" + limit + "}", str(value))
        tail = f" - {described}" if described else ""
        kind = _type_of(info.annotation)
        parts.append(f"`{name}{mark}`" + ("" if kind == "text" else f" {kind}") + tail)
    return "; ".join(parts)


def action_lines(
    *,
    callable_: Callable[[ActionSpec], bool] | None = None,
    numbers: Mapping[str, int | float] | None = None,
) -> list[str]:
    """One line per action, generated from the argument models: its reply shape, its arguments (`?` marks an
    optional one) and what it does. `callable_` is the same availability predicate the dispatch uses. A limit named
    in an argument's description (`{effect_chars}`) is filled from `numbers` (`prompt_limits`).
    """
    from engine.doctor.limits import prompt_limits

    numbers = prompt_limits() if numbers is None else numbers
    lines = []
    for spec in ACTIONS:
        if callable_ is not None and not callable_(spec):
            continue
        head = f'{{"action": "{spec.name}"' + (', "file": "F1"' if spec.needs_file else "")
        if spec.top_level:
            shape = head + ", " + ", ".join(f'"{f}": "..."' for f in spec.top_level) + "}"
            body = ""
        else:
            shape = head + ', "args": {...}}'
            body = " Args: " + _fields(spec.args_model, spec.top_level, numbers) + "."
        lines.append(f"`{shape}` - {spec.summary}{body}")
    return lines


def step_kind_lines() -> list[str]:
    """The step kinds the turn may propose, each with the parameters it takes; `remove_rows` also its operators."""
    lines = []
    for kind in sorted(DOCTOR_STEP_KINDS, key=lambda k: k.value):
        names = sorted(STEP_PARAMETERS.get(kind, frozenset()))
        line = kind.value + (f" ({', '.join(names)})" if names else "")
        if kind is RecipeStepKind.REMOVE_ROWS:
            line += "; operator is one of " + ", ".join(sorted(REMOVE_OPERATORS))
        lines.append(line)
    return lines


def prompt_variables(
    config: Any = None,
    *,
    callable_: Callable[[ActionSpec], bool] | None = None,
    config_root: Any = None,
    lessons: Sequence[str] = (),
) -> dict[str, Any]:
    """The generated variables of the Doctor prompt: `actions`, `step_kinds`, `doctor_limits` and `guide_index`.

    `lessons` (the owner-approved lessons, `engine.doctor.lessons.approved_lessons`) follow the guide index, so an
    approved lesson reaches every prompt whether or not the AI reads the `lessons` guide.
    """
    from engine.doctor.guides import index_text
    from engine.doctor.lessons import lessons_text
    from engine.doctor.limits import prompt_limits

    index = index_text(config_root)
    learned = lessons_text(lessons)
    return {
        "actions": action_lines(callable_=callable_, numbers=prompt_limits(config)),
        "step_kinds": step_kind_lines(),
        "doctor_limits": prompt_limits(config),
        "guide_index": f"{index}\n\n{learned}" if learned else index,
    }
