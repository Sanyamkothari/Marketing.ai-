"""The one author of the Data Doctor's plan.

Two authors write the plan, and they write it the same way:

* with an AI, the AI turn builds :data:`AuthorAction`s from its tool calls;
* without one, the default author builds the same actions from what the fact tools suggest.

:func:`apply_actions` is the only code that writes the case's items and questions. It takes a case and returns a case:
no storage, no tool call, no model. It runs on top of the reconcile store (`engine.doctor.reconcile`) with the single
producer :data:`PRODUCER`.

Who decides what:

* The author only *proposes*. Whether a proposal is in the plan at once or waits for the person's tick is decided by
  code (:func:`engine.doctor.agent_apply.waits_because`), never by the author.
* Nothing the person decided is ever changed here. Replacing or withdrawing it is a new proposal that waits for their
  yes (a swap); their yes then leaves the old one out (`engine.doctor.swaps`).
* `decided_by` is never set here. A change in the plan that nobody decided is "in the plan, undecided"; the approval
  stamps it.

Nothing here reads a cell.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import Field, JsonValue

from engine.agent.contracts import AgentConfidence
from engine.config import StrictBase
from engine.doctor.agent_apply import Roles
from engine.doctor.case import Case, CaseDoubt, EventDraft
from engine.doctor.protocol import Ask, DataRequest, ProtocolSettings
from engine.doctor.standing import StandingAnswerStore

__all__ = [
    "PRODUCER",
    "Applied",
    "ApplyContext",
    "AskPerson",
    "AuthorAction",
    "Create",
    "Doubt",
    "ItemDraft",
    "Refusal",
    "Request",
    "SetSetting",
    "SettingsView",
    "Withdraw",
    "apply_actions",
    "check_action",
]

PRODUCER = "author"
"""The producer every item, question and doubt of the author carries (`CaseItem.producer`)."""


class Refusal(StrictBase):
    """Why an action was not applied, in words a person (or the AI, to fix its next try) can act on."""

    code: str = Field(description="What kind of refusal, machine-readable.")
    what_failed: str = Field(description="What was refused, in one plain sentence.")
    value: str | None = Field(default=None, description="The name or count it is about; never a cell value.")
    how_to_fix: str = Field(min_length=1, description="What to change; never empty.")


class ItemDraft(StrictBase):
    """What a fact tool or an AI tool call produced for one plan item, its receipt included."""

    kind: str = Field(description="What it would change (`recipe_step`, `key`, `goal_setting`, ...).")
    payload: dict[str, JsonValue] = Field(description="What it would do, as data.")
    title: str = Field(description="What, in one plain sentence.")
    upload_id: str | None = Field(default=None, description="The file it is about; null for the whole case.")
    goal_id: str | None = Field(
        default=None, description="The goal section it belongs to; null for the base."
    )
    confidence: AgentConfidence = Field(description="`sure` only when the values prove it.")
    evidence_ids: tuple[str, ...] = Field(default=(), description="Evidence it rests on.")


class Create(StrictBase):
    """Put a change into the plan (or, when it must wait, in front of the person)."""

    op: Literal["create"] = "create"
    draft: ItemDraft
    reason: str = Field(description="Why, in the author's words.")
    replaces: str | None = Field(
        default=None,
        description="The item this takes the place of; a change the person decided waits for a yes.",
    )
    basis: str = Field(
        description='What it rests on: "s:<suggestion id>", an evidence reference ("r<n>") or "msg:<n>".'
    )


class Withdraw(StrictBase):
    """Take a change out of the plan; one the person decided, or that is already in the plan, waits for a yes."""

    op: Literal["withdraw"] = "withdraw"
    item_id: str
    reason: str
    consequence: str = Field(description="What changes in the training table without it.")


class AskPerson(StrictBase):
    """An ambiguity, settled by the protocol: proven, assumed or asked within the file's question budget."""

    op: Literal["ask"] = "ask"
    ask: Ask


class SetSetting(StrictBase):
    """A goal's setting (window, audience) to a value its config allows."""

    op: Literal["setting"] = "setting"
    goal_id: str
    setting: str
    value: str
    reason: str


class Doubt(StrictBase):
    """The author is unsure and says so in three parts."""

    op: Literal["doubt"] = "doubt"
    doubt: CaseDoubt


class Request(StrictBase):
    """No choice makes the data usable: what to collect, from whom, and why."""

    op: Literal["request"] = "request"
    request: DataRequest


AuthorAction = Annotated[
    Create | Withdraw | AskPerson | SetSetting | Doubt | Request, Field(discriminator="op")
]


class SettingsView(Protocol):
    """What the author needs to know of the chosen goal's settings (`engine.doctor.goal_settings.GoalSettings`)."""

    def allowed(self, setting: str) -> Sequence[str | int]:
        """The values the goal's config allows for `setting`; none for an unknown setting."""
        ...


@dataclass(frozen=True)
class ApplyContext:
    """What the author's actions are applied under."""

    now: datetime
    author: Literal["ai", "default"]
    roles: Roles
    settings: ProtocolSettings
    goal: SettingsView | None = None
    complete: bool = False
    """The default author says the whole set it stands for: what it made before and no longer suggests is taken
    back. The AI adds as it goes, and only withdraws or replaces by name."""
    requested_by: str | None = None
    """The user id whose message caused the turn."""
    standing: StandingAnswerStore | None = None
    """The client's remembered answers, applied to the questions asked now (`engine.doctor.standing`)."""


@dataclass
class Applied:
    """The case after the actions, and what happened to each."""

    case: Case
    events: list[EventDraft] = field(default_factory=list)
    refused: list[tuple[int, Refusal]] = field(default_factory=list)
    """The index of the action and why it was not applied."""
    made: list[str] = field(default_factory=list)
    """Ids of the items, questions and doubts created or changed."""
    after: list[Callable[[Case], None]] = field(default_factory=list)
    """Side effects once the case is written (a standing answer's use); each is idempotent."""


def check_action(case: Case, action: AuthorAction, ctx: ApplyContext) -> Refusal | None:
    """The guard layer's check of one action, before anything is written (filled in with `guards_layer`)."""
    del case, action, ctx  # the hook's signature: P4's guards read all three
    return None


def apply_actions(case: Case, actions: Sequence[AuthorAction], ctx: ApplyContext) -> Applied:
    """The case with `actions` applied, in order; a refused action is recorded and the next one still runs."""
    raise NotImplementedError("filled in by the next commit")
