"""The time-settings tools (Plan O M244, DEC-1814; the design §3.5, §5.5 L).

| # | Tool | Kind | What it does |
|---|---|---|---|
| 79 | `estimate_training_time` | READ | minutes a training run is expected to take under a budget, per stage, from rows x columns x model families x this deployment's past runs (the stored stage durations M189's progress bar reads) |
| 80 | `propose_training_budget` | PROPOSE | Quick, Balanced (recommended, at the point of diminishing returns) and Thorough, each "about N minutes" and what it buys; replaces `_time_rules` behind the switch |
| 81 | `propose_time_settings` | PROPOSE | the date each row describes, feasible time-split cut points, whether the newest slice is mature, the outcome window and lapse (settled through `define_outcome`), the snapshot cadence |

Both PROPOSE tools return a pending proposal the policy reads (M233's `data.propose_training_budget` and
`data.propose_time_settings` rule sets: never ticked unless the rules are `sure`). Nothing here changes the data;
`propose_training_budget` keeps the three choices it offered beside the data (`engine.doctor.time_settings.save_offer`)
so the run that takes one of them is promised exactly what was shown. Like M236's and M239's tools they serve the
rules and the screen; each is `session-only` (the catalogue) and needs an Analyst.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.config import StrictBase
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
from engine.doctor.time_settings import (
    TimeOffer,
    TimeSettings,
    TimeSettingsError,
    TrainingBudget,
    TrainingTimeEstimate,
    estimate,
    propose_budget,
    propose_time_settings,
    save_offer,
    source_kind_of,
    time_inputs,
)

__all__ = ["M244_TOOLS", "register_time_tools"]

M244_TOOLS: Final[tuple[str, ...]] = (
    "estimate_training_time",
    "propose_training_budget",
    "propose_time_settings",
)
"""Every tool this module registers, in catalogue order."""

RULES_AND_SCREEN: Final[frozenset[str]] = frozenset({"rules", "person"})


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _refused(exc: TimeSettingsError) -> DataToolError:
    return DataToolError(exc.code, exc.message, status=422)


def _inputs(ctx: DataToolContext) -> Any:
    agent = ctx.require_agent()
    try:
        return time_inputs(
            ctx.storage, agent.config, rows=agent.profile.row_count, columns=agent.profile.column_count
        )
    except TimeSettingsError as exc:
        raise _refused(exc) from exc


# ---------------------------------------------------------------------------
# #79 estimate_training_time
# ---------------------------------------------------------------------------
class EstimateArgs(_Args):
    minutes: int | None = Field(
        default=None,
        ge=1,
        le=240,
        description="The search's time limit to estimate for; the use case's own when omitted.",
    )


def _estimate(args: EstimateArgs, ctx: DataToolContext) -> TrainingTimeEstimate:
    inputs = _inputs(ctx)
    return estimate(inputs, args.minutes if args.minutes is not None else inputs.current_minutes)


# ---------------------------------------------------------------------------
# #80 propose_training_budget
# ---------------------------------------------------------------------------
class BudgetArgs(_Args):
    pass


class BudgetOutput(StrictBase):
    """A pending choice of three; Balanced offered ticked only when the rules are `sure`."""

    source: Literal["rules"] = "rules"
    ticked: bool
    confidence: Literal["sure", "check"]
    state: Literal["pending"] = "pending"
    budget: TrainingBudget
    offered: bool = Field(description="Whether the choices were kept beside the data for the run to promise.")


def _propose_budget(_args: BudgetArgs, ctx: DataToolContext) -> BudgetOutput:
    agent = ctx.require_agent()
    budget = propose_budget(_inputs(ctx))
    offered = False
    if ctx.storage is not None and agent.upload_id:
        save_offer(
            ctx.storage,
            TimeOffer(
                use_case_id=agent.config.id,
                source_kind=source_kind_of(ctx.storage, agent.upload_id),
                source_id=agent.upload_id,
                offered_at=ctx.clock(),
                rows=agent.profile.row_count,
                columns=agent.profile.column_count,
                budget=budget,
            ),
        )
        offered = True
    sure = budget.confidence == "sure"
    return BudgetOutput(ticked=sure, confidence=budget.confidence, budget=budget, offered=offered)


# ---------------------------------------------------------------------------
# #81 propose_time_settings
# ---------------------------------------------------------------------------
class TimeSettingsArgs(_Args):
    as_of_column: str | None = Field(
        default=None, min_length=1, max_length=512, description="The date each row describes, if decided."
    )
    outcome_column: str | None = Field(
        default=None, min_length=1, max_length=512, description="The outcome column, if decided."
    )
    window_days: int | None = Field(
        default=None, ge=1, le=3650, description="The outcome window in days; the goal's when omitted."
    )


class TimeSettingsOutput(StrictBase):
    source: Literal["rules"] = "rules"
    ticked: Literal[False] = False
    confidence: Literal["sure", "check"]
    state: Literal["pending"] = "pending"
    settings: TimeSettings


def _decided(ctx: DataToolContext) -> tuple[str | None, str | None]:
    """The file's ID and outcome columns, as decided in the session or the context (never guessed)."""
    agent = ctx.require_agent()
    key, target = ctx.primary_key, agent.target
    if ctx.session is not None:
        from engine.agent.proposals import roles_of

        session_key, session_target = roles_of(ctx.session)
        key, target = key or session_key, target or session_target
    return key, target


def _propose_time_settings(args: TimeSettingsArgs, ctx: DataToolContext) -> TimeSettingsOutput:
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import brief_from_config

    agent = ctx.require_agent()
    key, target = _decided(ctx)
    for name in (args.as_of_column, args.outcome_column):
        if name is not None:
            agent.resolve(name)
    file = LoadedFile(
        upload_id=agent.upload_id, file_name=agent.profile.file_name, frame=agent.frame, profile=agent.profile
    )
    try:
        settings = propose_time_settings(
            file,
            agent.config,
            brief=brief_from_config(agent.config, root=ctx.root()),
            as_of_column=agent.resolve(args.as_of_column) if args.as_of_column else None,
            outcome_column=agent.resolve(args.outcome_column) if args.outcome_column else target,
            primary_key=key,
            window_days=args.window_days,
            config_root=ctx.root(),
        )
    except TimeSettingsError as exc:
        raise _refused(exc) from exc
    sure = settings.as_of.sure and not settings.questions
    return TimeSettingsOutput(confidence="sure" if sure else "check", settings=settings)


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
            79,
            "estimate_training_time",
            read,
            "How long training is expected to take under a time limit, stage by stage, from the file's size, "
            "the model families allowed and this deployment's past runs (or similar public datasets).",
            EstimateArgs,
            TrainingTimeEstimate,
            _estimate,
            ("agent",),
        ),
        (
            80,
            "propose_training_budget",
            propose,
            "Propose how long training may take as three plain choices - Quick, Balanced (recommended, where "
            "similar past runs stopped finding better models) and Thorough - each about N minutes.",
            BudgetArgs,
            BudgetOutput,
            _propose_budget,
            ("agent",),
        ),
        (
            81,
            "propose_time_settings",
            propose,
            "Propose the time settings: the date each row describes, where a time split cuts, whether the newest "
            "rows' outcomes are final, the outcome window, lapse and the snapshot cadence, each with its reason.",
            TimeSettingsArgs,
            TimeSettingsOutput,
            _propose_time_settings,
            ("agent",),
        ),
    )


_WRAPS: Final[Mapping[str, str]] = {
    "estimate_training_time": (
        "engine/run_progress.py past_durations (M189's stored stage durations, engine/pipeline.py "
        "_StatusWriter.finish); configs/engine.yaml model_search.time_limit_minutes"
    ),
    "propose_training_budget": (
        "engine/agent/recommend.py _time_rules (replaced by the budget); " "model_search.time_limit_minutes"
    ),
    "propose_time_settings": (
        "engine/stages/prepare.py _part_sizes/_tie_boundary; engine/agent/label_drafts.py draft_labels; "
        "engine/onboarding/snapshots.py plan_snapshots; configs/outcomes.yaml"
    ),
}


def register_time_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register M244's tools in `registry`; their names, in catalogue order."""
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
