"""Shapes shared by M238's row and value tools (Plan O, DEC-1808): a pending step proposal and a doubt.

Every tool in `engine.doctor.tools_rows` answers with counts and masked examples only, and proposes changes as
`StepProposal`s: a fixed step kind with frozen parameters (never code), always `pending` and never ticked, with
what it would change measured on the file. A proposal becomes a `RecipeStep` only when the planner adds it to the
case (`as_step`); only the person ticks it and only Approve runs it.

**No dict is keyed by a cell.** The egress gate reads a dict key as a label (`tests/unit/agent/test_egress_canary.py`,
"no dict key in any tool result is a cell"), so a parameter that maps spellings or units (`map_values.mapping`,
`convert_units.units`, `normalise_text.merge`) is carried as a list of `[from, to]` pairs and turned back into the
step's map by `as_step` / `step_params`.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import Field, JsonValue, field_validator

from engine.agent.contracts import ProposalSource, RecipeStep, RecipeStepKind
from engine.config import StrictBase

__all__ = ["PAIRED_PARAMS", "Doubt", "StepProposal"]

PAIRED_PARAMS: Final[frozenset[str]] = frozenset({"mapping", "units", "merge"})
"""Step parameters that map cell spellings to values: carried as `[from, to]` pairs, never as a dict."""


def _as_map(pairs: list[Any]) -> dict[str, Any]:
    return {str(pair[0]): pair[1] for pair in pairs}


class StepProposal(StrictBase):
    """One suggested step: pending, `check`, never ticked (design §6.2; decision O10 measured)."""

    kind: RecipeStepKind = Field(description="The fixed step kind it would add.")
    column: str = Field(description="The column the step reads.")
    params: dict[str, JsonValue] = Field(default_factory=dict, description="The step's frozen parameters.")
    source: ProposalSource = Field(description="Who proposed it: the rules, the AI or the person.")
    confidence: Literal["check"] = Field(default="check", description="Always `check`: a person decides.")
    ticked: Literal[False] = Field(default=False, description="Never ticked when offered.")
    state: Literal["pending"] = Field(
        default="pending", description="Always pending until the person decides."
    )
    title: str = Field(description="What it does, in one plain sentence.")
    reason: str = Field(description="Why, in one plain sentence built from the counts.")
    changed: int = Field(
        ge=0, description="Values (or rows) it changes on this file, measured by running it."
    )
    out_of: int = Field(ge=0, description="Non-empty values of the column (or rows) it was measured on.")
    share_pct: float = Field(ge=0.0, le=100.0, description="`changed` as a percentage of `out_of`.")
    guard: Literal["ok", "warn"] = Field(
        default="ok",
        description="Decision O10 for an AI proposal: `warn` above 30%; above 60% it is not made.",
    )
    guard_code: str | None = Field(default=None, description="`AI_STEP_CHANGES_MANY` on a warning.")

    @field_validator("params", mode="before")
    @classmethod
    def _pairs(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {
            key: (
                [[k, v] for k, v in item.items()] if key in PAIRED_PARAMS and isinstance(item, dict) else item
            )
            for key, item in value.items()
        }

    @property
    def step_params(self) -> dict[str, Any]:
        """The step's own parameters: each paired parameter back as the map the step reads."""
        return {
            key: _as_map(item) if key in PAIRED_PARAMS and isinstance(item, list) else item
            for key, item in self.params.items()
        }

    def as_step(self, order: int) -> RecipeStep:
        """The recipe step it would add, at `order`."""
        return RecipeStep(
            order=order, kind=self.kind, column=self.column, params=self.step_params, reason=self.reason
        )


class Doubt(StrictBase):
    """Design §6.7: I am not sure because …; here is what I would do …; here is what changes if not …"""

    code: str = Field(description="The detector that raised it.")
    column: str = Field(description="The column it is about.")
    because: str = Field(description="Why the agent is unsure, built from counts.")
    would_do: str = Field(description="What it would do.")
    if_not: str = Field(description="What changes if the other reading is right.")
    blocking: bool = Field(default=False, description="Doubts are non-blocking unless APPROVE-class.")
