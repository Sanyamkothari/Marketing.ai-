"""The goal brief (Plan O M235, DEC-1805; the design §3.2): what Data Doctor is trying to achieve, from configuration.

Data Doctor is **one agent with an optional goal** (the owner's requirement 6). Without a goal it understands the
files, fixes what does not depend on a goal and says what the data can power and what is missing. With a goal, a
**goal brief** drives the goal's own checks and questions and the readiness checklist for that goal.

A brief is **derived, never written**: :func:`load_goal_brief` reads it from the use case's configuration
(`UseCaseConfig`, the use-case YAML merged over `configs/engine.yaml`) and `configs/outcomes.yaml`. The AI never
writes or edits one. It is frozen and carries :attr:`GoalBrief.brief_hash`, the SHA-256 of its canonical JSON
without the hash itself, so an unchanged configuration always gives the same hash, in every run and every process;
the prescription (M243) and the readiness report (M234) cite it.

The fields are the design's table (§3.2):

| Field | From |
|---|---|
| `goal_id`, `goal_name`, `problem_type`, `use_case_type` | the use case's id, name, problem type; [RDY] §5's kind |
| `outcome` | `target.*`, `agent.column_hints.target_synonyms`, the success sentence in `configs/outcomes.yaml` |
| `time_rules` | `split.type`, `split.time_column`, the template's time column, the outcome window, maturity |
| `required_columns` | key hints; consent, opt-out, last contact; treatment; value; channels; complaint text; copy fields |
| `split` | `split.*` |
| `thresholds` | `validation.min_rows`, `validation.min_positive`, uplift `min_arm_rows` |
| `levels` | `agent.levels` |
| `contact` | `actions.contacts_customers`, the suppression columns, `actions.suppression.channels` |
| `models` | `model_search.metric` and `model_search.candidates` |
| `time_settings` | what the configuration says today (M244 proposes them from the data): the training budget in minutes, the as-of column, the outcome window |
| `mode` | train, score, measure outcomes or audit |

`load_goal_brief` is tool #35 of the catalogue (the design §5.5): a READ tool, rules only. It holds no client data
(configuration alone), so its result keeps the 16 KB budget, as `data_request` does. :func:`register_goal_brief_tool`
hands it to a registry; until the integrator adds it to `engine.doctor.tools.register_all`, the visit
(`engine.doctor.visit`) registers it on its own registry.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.config import ConfigError, StrictBase, UseCaseConfig, config_root, load_use_case
from engine.doctor.registry import (
    AGGREGATE_BUDGET,
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
)

__all__ = [
    "GOAL_BRIEF_TOOL",
    "GOAL_ID_PATTERN",
    "BriefColumn",
    "BriefContact",
    "BriefMode",
    "BriefModels",
    "BriefOutcome",
    "BriefSplit",
    "BriefThresholds",
    "BriefTimeRules",
    "BriefTimeSettings",
    "GoalBrief",
    "GoalBriefArgs",
    "brief_from_config",
    "brief_hash",
    "goal_brief_tool",
    "load_goal_brief",
    "register_goal_brief_tool",
]

GOAL_BRIEF_TOOL: Final[str] = "load_goal_brief"
GOAL_ID_PATTERN: Final[str] = r"^[a-z][a-z0-9-]{0,63}$"
"""A goal is a use case: its id is a use-case id (`UseCaseConfig.id`)."""

BriefMode = Literal["train", "score", "measure_outcomes", "audit"]
Need = Literal[
    "key",
    "outcome",
    "time",
    "consent",
    "recently_contacted",
    "treatment",
    "value",
    "channel",
    "complaint_text",
    "copy_field",
]


class BriefOutcome(StrictBase):
    """What the goal learns: the outcome column and what "yes" means."""

    column: str | None = Field(description="The configured outcome column, if the use case names one.")
    synonyms: tuple[str, ...] = Field(default=(), description="Other names the outcome is often found under.")
    positive_label: str | int | bool | None = Field(default=None, description="The value that means yes.")
    definition: str = Field(default="", description="What yes means, in one sentence.")
    label_source: str = Field(default="", description="Where the outcome usually comes from.")
    success_sentence: str | None = Field(
        default=None,
        description="The sentence `configs/outcomes.yaml` offers when the outcome is built from events, with its "
        "blanks ({days}, {event}, ...).",
    )
    label_type: str | None = Field(
        default=None, description="`event_absence` or `event_presence`, with a sentence."
    )
    window_days: tuple[int, ...] = Field(default=(), description="The outcome windows offered, in days.")
    default_window_days: int | None = Field(default=None, description="The window offered first.")


class BriefTimeRules(StrictBase):
    """How time is handled: the split, the as-of column, the outcome window and maturity."""

    split_type: str = Field(description="`random_stratified`, `time_based` or `group_based`.")
    time_column: str | None = Field(default=None, description="The column a time split orders by.")
    time_column_hints: tuple[str, ...] = Field(
        default=(), description="Names a date column is often found under."
    )
    snapshot_column: str | None = Field(
        default=None, description="The template's time column: the date each row describes."
    )
    outcome_window_days: int | None = Field(
        default=None, description="Days after the snapshot the outcome covers."
    )
    label_must_be_mature: bool = Field(
        description="True when the outcome is measured over a window: the newest rows' outcomes may not be final."
    )


class BriefColumn(StrictBase):
    """A column the goal needs (or uses when present), with the names it is usually found under."""

    need: Need = Field(description="What the column is for.")
    names: tuple[str, ...] = Field(description="The configured name first, then the usual other names.")
    required: bool = Field(description="True when the goal cannot be trained without it.")
    why: str = Field(description="Why the goal needs it, in plain words.")


class BriefSplit(StrictBase):
    type: str
    validation_fraction: float
    test_fraction: float
    time_column: str | None = None
    group_column: str | None = None


class BriefThresholds(StrictBase):
    min_rows: int
    min_positive: int
    min_arm_rows: int | None = Field(default=None, description="Per arm, for an uplift goal only.")


class BriefContact(StrictBase):
    """Whether the goal contacts people, and the columns that decide who may be contacted."""

    contacts_customers: bool
    suppress_opted_out: bool
    opt_out_column: str | None = None
    consent_column: str | None = None
    recently_contacted_column: str | None = None
    channels: tuple[str, ...] = Field(
        default=(), description="Channels with their own consent, in preference order."
    )


class BriefModels(StrictBase):
    metric: str
    families: tuple[str, ...]


class BriefTimeSettings(StrictBase):
    """The time settings the configuration holds today. M244 proposes them from the data and remembers them."""

    training_minutes: int = Field(description="`model_search.time_limit_minutes`.")
    as_of_column: str | None = Field(default=None, description="The date each row describes, if configured.")
    outcome_window_days: int | None = Field(default=None, description="The outcome window, if configured.")


class GoalBrief(StrictBase):
    """A goal, frozen and hashed (the design §3.2). Derived from configuration by :func:`load_goal_brief`."""

    goal_id: str = Field(description="The use case the brief comes from.")
    goal_name: str = Field(description="Its name.")
    goal_text: str | None = Field(
        default=None, description="The goal in one sentence (`agent.goal`), if any."
    )
    entity: str = Field(description="What one row is about (customer, shopper, asset).")
    problem_type: str
    use_case_type: str = Field(description="[RDY] §5's kind: propensity, time_based, regression, uplift, ...")
    mode: BriefMode = "train"
    outcome: BriefOutcome
    time_rules: BriefTimeRules
    required_columns: tuple[BriefColumn, ...]
    split: BriefSplit
    thresholds: BriefThresholds
    levels: tuple[str, ...]
    contact: BriefContact
    models: BriefModels
    time_settings: BriefTimeSettings
    brief_hash: str = Field(default="", description="SHA-256 of every field above (:func:`brief_hash`).")

    def column(self, need: Need) -> BriefColumn | None:
        """The first column the goal lists for `need`, if any."""
        return next((column for column in self.required_columns if column.need == need), None)


def brief_hash(brief: GoalBrief) -> str:
    """SHA-256 over the canonical JSON of the brief without `brief_hash`: stable across runs and processes."""
    body = brief.model_dump(mode="json", exclude={"brief_hash"})
    return sha256_hex(canonical_bytes(body))


def _names(*groups: object) -> tuple[str, ...]:
    """Names in order, each once, empty and missing ones left out."""
    found: list[str] = []
    for group in groups:
        items = group if isinstance(group, tuple | list) else (group,)
        for item in items:
            if isinstance(item, str) and item.strip() and item.strip() not in found:
                found.append(item.strip())
    return tuple(found)


def _template_time_column(config: UseCaseConfig) -> str | None:
    for column in config.template.columns:
        if str(column.role.value) == "time":
            return column.name
    return None


def _required_columns(config: UseCaseConfig, *, mode: BriefMode) -> tuple[BriefColumn, ...]:
    from engine.config import GenerativeKind, ProblemType, SplitType

    entity = config.entity
    hints = config.agent.column_hints
    suppression = config.actions.suppression
    columns: list[BriefColumn] = [
        BriefColumn(
            need="key",
            names=_names(config.primary_key_hints),
            required=True,
            why=f"One row per {entity} needs a column that names each {entity}.",
        )
    ]
    outcome = _names(config.target.column, hints.target_synonyms)
    if outcome:
        columns.append(
            BriefColumn(
                need="outcome",
                names=outcome,
                required=mode == "train",
                why="The model learns from what happened before: the outcome of each row.",
            )
        )
    time_based = config.split.type is SplitType.TIME_BASED
    time_names = _names(config.split.time_column, _template_time_column(config), config.time_column_hints)
    if time_based or time_names:
        columns.append(
            BriefColumn(
                need="time",
                names=time_names,
                required=time_based,
                why="The date each row describes: testing uses the newest dates, and nothing after it may be used.",
            )
        )
    if config.actions.contacts_customers and suppression.suppress_opted_out:
        # `hints.opt_out` names (opted_out, do_not_contact) say "do NOT contact": read as consent they would flip
        # the list, so they are left to the contact gate, which reads them as do-not-disturb.
        consent = _names(suppression.opt_out_column, config.governance.consent_column, hints.consent)
        configured = suppression.opt_out_column is not None or config.governance.consent_column is not None
        columns.append(
            BriefColumn(
                need="consent",
                names=consent,
                required=configured,
                why="This goal contacts people: only those who agreed to be contacted may be on the list.",
            )
        )
    if config.actions.contacts_customers and suppression.suppress_recently_contacted:
        columns.append(
            BriefColumn(
                need="recently_contacted",
                names=_names(suppression.recently_contacted_column, hints.recently_contacted),
                required=False,
                why="People contacted recently are left out of the next list.",
            )
        )
    for channel, spec in suppression.channels.items():
        columns.append(
            BriefColumn(
                need="channel",
                names=_names(spec.consent_column, spec.contactable_column),
                required=False,
                why=f"Who agreed to be contacted on {channel}.",
            )
        )
    if config.problem_type is ProblemType.UPLIFT:
        columns.append(
            BriefColumn(
                need="treatment",
                names=_names(config.uplift.treatment_column, config.uplift.treatment_column_hints),
                required=mode == "train",
                why="Who got the offer and who did not: the difference is what this goal measures.",
            )
        )
        if config.uplift.policy.value_column is not None:
            columns.append(
                BriefColumn(
                    need="value",
                    names=_names(config.uplift.policy.value_column),
                    required=False,
                    why="What each person is worth, to rank by money rather than by chance.",
                )
            )
    kind = config.generative.kind
    if kind is GenerativeKind.ROOT_CAUSE_SUMMARY and config.generative.root_cause.complaint_text_column:
        columns.append(
            BriefColumn(
                need="complaint_text",
                names=_names(config.generative.root_cause.complaint_text_column),
                required=True,
                why="The complaints the summary is written from.",
            )
        )
    if kind is GenerativeKind.CAMPAIGN_COPY:
        for name in config.generative.campaign_copy.allowed_fields:
            columns.append(
                BriefColumn(
                    need="copy_field",
                    names=(name,),
                    required=False,
                    why="A field the message may mention.",
                )
            )
    return tuple(columns)


def brief_from_config(
    config: UseCaseConfig, *, root: Path | None = None, mode: BriefMode = "train"
) -> GoalBrief:
    """The brief of `config` (see the module docstring), hashed."""
    from engine.config import ProblemType, SplitType
    from engine.doctor.outcomes import load_outcome_catalogue
    from engine.doctor.readiness import use_case_type

    template = load_outcome_catalogue(config_root(root)).templates.get(config.id)
    window: int | None = (
        config.uplift.outcome_window_days if config.problem_type is ProblemType.UPLIFT else None
    )
    if window is None and template is not None:
        window = template.default_days
    if window is None:
        window = _window_in(config.target.definition)
    snapshot = _template_time_column(config)
    time_based = config.split.type is SplitType.TIME_BASED
    as_of = config.split.time_column or snapshot
    draft = GoalBrief(
        goal_id=config.id,
        goal_name=config.name,
        goal_text=config.agent.goal,
        entity=config.entity,
        problem_type=config.problem_type.value,
        use_case_type=use_case_type(config),
        mode=mode,
        outcome=BriefOutcome(
            column=config.target.column,
            synonyms=_names(config.agent.column_hints.target_synonyms),
            positive_label=config.target.positive_label,
            definition=config.target.definition,
            label_source=config.target.label_source,
            success_sentence=template.sentence if template is not None else None,
            label_type=template.label_type if template is not None else None,
            window_days=template.days if template is not None else (),
            default_window_days=template.default_days if template is not None else None,
        ),
        time_rules=BriefTimeRules(
            split_type=config.split.type.value,
            time_column=config.split.time_column,
            time_column_hints=_names(config.time_column_hints),
            snapshot_column=snapshot,
            outcome_window_days=window,
            label_must_be_mature=window is not None or time_based,
        ),
        required_columns=_required_columns(config, mode=mode),
        split=BriefSplit(
            type=config.split.type.value,
            validation_fraction=config.split.validation_fraction,
            test_fraction=config.split.test_fraction,
            time_column=config.split.time_column,
            group_column=config.split.group_column,
        ),
        thresholds=BriefThresholds(
            min_rows=config.validation.min_rows,
            min_positive=config.validation.min_positive,
            min_arm_rows=config.uplift.min_arm_rows if config.problem_type is ProblemType.UPLIFT else None,
        ),
        levels=tuple(level.value for level in config.agent.levels),
        contact=BriefContact(
            contacts_customers=config.actions.contacts_customers,
            suppress_opted_out=config.actions.suppression.suppress_opted_out,
            opt_out_column=config.actions.suppression.opt_out_column,
            consent_column=config.governance.consent_column,
            recently_contacted_column=config.actions.suppression.recently_contacted_column,
            channels=tuple(config.actions.suppression.channels),
        ),
        models=BriefModels(
            metric=config.model_search.metric.value,
            families=tuple(family.value for family in config.model_search.candidates),
        ),
        time_settings=BriefTimeSettings(
            training_minutes=config.model_search.time_limit_minutes,
            as_of_column=as_of,
            outcome_window_days=window,
        ),
    )
    return draft.model_copy(update={"brief_hash": brief_hash(draft)})


_WINDOW: Final[re.Pattern[str]] = re.compile(
    r"\b(?:within|in|next|over)\s+(?:the\s+)?(\d{1,4})\s+days?\b", re.I
)


def _window_in(definition: str) -> int | None:
    """An outcome window the definition states in words ("... in the 90 days after the snapshot")."""
    found = _WINDOW.search(definition)
    return int(found.group(1)) if found else None


def load_goal_brief(goal_id: str, *, root: Path | None = None, mode: BriefMode = "train") -> GoalBrief:
    """The brief of the use case `goal_id` under the configuration `root`.

    A goal that is not a use case answers the configuration's own codes (`USE_CASE_NOT_FOUND`, `CONFIG_INVALID`),
    as a `ConfigError`; an id that is not a use-case id at all answers `USE_CASE_NOT_FOUND` without reading a file.
    """
    if not re.fullmatch(GOAL_ID_PATTERN, goal_id):
        raise ConfigError("USE_CASE_NOT_FOUND", "There is no goal with that name.")
    return brief_from_config(load_use_case(goal_id, root), root=root, mode=mode)


# ---------------------------------------------------------------------------
# The tool (#35)
# ---------------------------------------------------------------------------
class GoalBriefArgs(StrictBase):
    """`load_goal_brief`'s arguments."""

    goal_id: str = Field(pattern=GOAL_ID_PATTERN, description="The use case whose brief to load.")
    mode: BriefMode = Field(default="train", description="Train, score, measure outcomes or audit.")


_BRIEF_POLICY: Final[RoutePolicy] = RoutePolicy(
    role=Role.VIEWER, action="doctor.goal_brief_read", purpose="see what a goal needs"
)


def _run(args: GoalBriefArgs, ctx: DataToolContext) -> GoalBrief:
    try:
        return load_goal_brief(args.goal_id, root=ctx.root(), mode=args.mode)
    except ConfigError as exc:
        raise DataToolError(exc.code, "There is no goal with that name here.", status=404) from exc


def goal_brief_tool() -> DataTool[GoalBriefArgs, GoalBrief]:
    """`load_goal_brief` as a registry entry: READ, rules, no client data (configuration only)."""
    return DataTool(
        number=35,
        name=GOAL_BRIEF_TOOL,
        kind=DataToolKind.READ,
        description="What a goal needs: its outcome, time rules, required columns, split, thresholds and levels.",
        input_model=GoalBriefArgs,
        output_model=GoalBrief,
        route_policy=_BRIEF_POLICY,
        exposure=Exposure.SESSION_ONLY,
        run=_run,
        # Built from configuration alone, it holds no client data: the 16 KB budget, as `data_request`.
        max_output_bytes=AGGREGATE_BUDGET,
        wraps="engine/config.py load_use_case; engine/doctor/outcomes.py load_outcome_catalogue",
    )


def register_goal_brief_tool(registry: DataToolRegistry) -> None:
    """Add `load_goal_brief` to `registry` (the integrator calls this from `register_all`)."""
    if GOAL_BRIEF_TOOL not in registry.names():
        registry.register(goal_brief_tool())
