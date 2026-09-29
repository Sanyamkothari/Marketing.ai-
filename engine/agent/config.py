"""The `agent:` block of a use case (Plan G §8.1): how that use case's Guided-setup helper behaves.

There is one agent engine and one configuration per use case (DEC-1003): the engine never branches
on a use-case id, and a new use case gets its helper by adding YAML. Defaults live in
`configs/engine.yaml` under `defaults.agent`; a use-case file overrides only what differs.

This module imports nothing from `engine`, for the reason `engine/uplift/config.py` gives:
`engine.config.UseCaseConfig` declares a field of type :class:`AgentConfig`, so `engine.config`
imports this module at the top and a cycle would break both. The base settings repeat
`engine.config._Base` - frozen, unknown keys refused - and a malformed value raises `ValueError`,
which the config loader reports as `CONFIG_INVALID` with the dotted path.

The block is **not overridable per run**: none of its paths is in `overridable_paths()`, so a run
override that names one is refused with `OVERRIDE_UNKNOWN_PATH`. It changes how suggestions are
made, never what a run trains on, so it is also outside `Recipe.recipe_hash`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "AGENT_KNOWLEDGE_MAX_CHARS",
    "AGENT_KNOWLEDGE_MAX_ITEMS",
    "AgentColumnHints",
    "AgentConfig",
    "AgentLevel",
    "DataAccess",
]

_SETTINGS: Final[ConfigDict] = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
    use_enum_values=False,
    str_strip_whitespace=True,
    populate_by_name=True,
    protected_namespaces=(),
)

AGENT_KNOWLEDGE_MAX_ITEMS: Final[int] = 20
AGENT_KNOWLEDGE_MAX_CHARS: Final[int] = 300
"""One knowledge line is a sentence, not a document: the whole pack goes into every prompt."""


class AgentLevel(StrEnum):
    """How far a recipe may change the data (Plan G §4, G8).

    `clean` fixes values in place, `derive` adds columns computed from one row, `reshape` turns many
    rows per entity into one (M76). Combining several files and creating the target are later
    levels and deliberately have no member yet; agent-written code is never a level.
    """

    CLEAN = "clean"
    DERIVE = "derive"
    RESHAPE = "reshape"


class DataAccess(StrEnum):
    """What the chat model may see of the file's cells (`agent.ai_data_access`).

    `masked_data` shows the values a tool asks for after masking (`engine.agent.egress.mask_value`):
    the model can look at real cells so it does not have to guess. `summaries_only` shows no cell at
    all: every string that comes from a cell is replaced by its *shape* (`Aaaaa 9999`), so the model
    still sees formats and counts and never a value. Personal-data columns never yield a value in
    either mode.
    """

    MASKED_DATA = "masked_data"
    SUMMARIES_ONLY = "summaries_only"


def _clean_names(values: tuple[str, ...]) -> tuple[str, ...]:
    """Strip, drop blanks and refuse duplicates (case-insensitive), keeping the author's order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        name = raw.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            raise ValueError(f"{name!r} is listed twice")
        seen.add(key)
        out.append(name)
    return tuple(out)


class AgentColumnHints(BaseModel):
    """Extra column-name hints the helper uses on top of the use case's own detection hints.

    `primary_key_hints` and `time_column_hints` stay where they are; these cover roles the profile
    does not detect today. Matching is case-insensitive on the whole name or a `_`-separated part.
    """

    model_config = _SETTINGS

    target_synonyms: tuple[str, ...] = ()
    consent: tuple[str, ...] = ("marketing_opt_in", "opt_in", "consent", "consented")
    opt_out: tuple[str, ...] = ("opt_out", "opted_out", "unsubscribed", "do_not_contact")
    recently_contacted: tuple[str, ...] = ("last_contacted_at", "last_contacted", "last_contact_date")

    @field_validator("target_synonyms", "consent", "opt_out", "recently_contacted")
    @classmethod
    def _names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_names(value)


class AgentConfig(BaseModel):
    """`agent:` in a use case: the Guided-setup helper for that use case."""

    model_config = _SETTINGS

    enabled: bool = True
    display_name: Annotated[str | None, Field(max_length=60)] = None
    """Shown on the tab and in the chat. Null means "<use case name> helper"."""
    goal: Annotated[str | None, Field(max_length=300)] = None
    """One sentence on what the use case predicts, in plain words. Null means the target definition."""
    knowledge: tuple[str, ...] = ()
    """Short facts in plain words that the rules and the prompt may rely on."""
    column_hints: AgentColumnHints = AgentColumnHints()
    levels: tuple[AgentLevel, ...] = (AgentLevel.CLEAN, AgentLevel.DERIVE)
    max_conversion_failure_pct: Annotated[float, Field(ge=0.0, le=50.0)] = 5.0
    """A format fix that cannot convert more than this share of a column's values becomes a question."""
    tick_uncertain: bool = False
    """Whether suggestions the helper is not sure about start ticked. Sure ones always do."""
    max_llm_calls_per_session: Annotated[int, Field(ge=1, le=500)] = 40
    max_tool_steps_per_turn: Annotated[int, Field(ge=1, le=20)] = 12
    ai_data_access: DataAccess = DataAccess.MASKED_DATA
    """`masked_data` (cells visible after masking) or `summaries_only` (cells replaced by their shape)."""
    always_hide_columns: tuple[str, ...] = ()
    """Columns whose values the chat model never sees (counts only), whatever the profile says."""

    @field_validator("knowledge")
    @classmethod
    def _knowledge(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        lines = tuple(line.strip() for line in value if line.strip())
        if len(lines) > AGENT_KNOWLEDGE_MAX_ITEMS:
            raise ValueError(f"at most {AGENT_KNOWLEDGE_MAX_ITEMS} knowledge lines, got {len(lines)}")
        long = [line[:40] for line in lines if len(line) > AGENT_KNOWLEDGE_MAX_CHARS]
        if long:
            raise ValueError(
                f"a knowledge line is at most {AGENT_KNOWLEDGE_MAX_CHARS} characters; "
                f"too long: {', '.join(repr(text + '...') for text in long)}"
            )
        return lines

    @field_validator("always_hide_columns")
    @classmethod
    def _always_hide(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_names(value)

    @field_validator("levels")
    @classmethod
    def _levels(cls, value: tuple[AgentLevel, ...]) -> tuple[AgentLevel, ...]:
        if not value:
            raise ValueError("levels must name at least one level (clean)")
        if len(set(value)) != len(value):
            raise ValueError("levels lists a level twice")
        if AgentLevel.CLEAN not in value:
            raise ValueError("levels must include clean: every other level builds on cleaned values")
        return tuple(member for member in AgentLevel if member in value)

    def name_for(self, use_case_name: str) -> str:
        """The helper's display name."""
        return self.display_name or f"{use_case_name} helper"

    def goal_for(self, target_definition: str | None) -> str:
        """The helper's goal sentence; falls back to the target definition, then to an empty string."""
        return self.goal or (target_definition or "")
