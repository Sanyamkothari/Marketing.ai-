"""The guard layer of the author: what an action may never do, whoever proposes it and whatever the AI says.

:func:`check_action` runs on every action before :func:`engine.doctor.author.apply_actions` writes anything. A refusal
is a :class:`Refusal` (code, what failed, a name or count, how to fix it: never empty, never a cell), and the AI reads
it in its next turn like any other. The checks are code, so the safety does not depend on the AI's own words:

* ``ITEM_NOT_FOUND``: the change an action names (to replace or take out) is not in the plan's active section. (A
  suggestion the AI adopts is checked where it is adopted; one that is made up only leaves the change unsure.)
* ``APPROVAL_IS_PERSONS``: an action carries a decision (``decided_by``, an approval); only the person's click records one.
* ``OUTCOME_IS_PERSONS``: the AI sets the outcome. It is the person's answer to its question (or a rule's suggestion
  they approve); a column that is not a yes/no never gets as far as a question (`engine.doctor.facts_goal`).
* ``FEATURE_IS_ID``: a feature or setting that would make the customer ID a column the model learns from.
* ``STEP_ON_ROLE_COLUMN``: a step on a column that holds a meaning would be in the plan without the person's tick.
  Placement is :func:`engine.doctor.agent_apply.waits_because`'s; this refuses only what it would let through.
* ``SETTING_VALUE_NOT_ALLOWED``: a goal setting outside the values its config allows.
* ``LEFT_OUT_BY_PERSON``: the same change the person left out, proposed again.

Nothing here reads a cell or calls the AI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from engine.agent.contracts import ProposalState, Refusal
from engine.doctor.agent_apply import twin_of, waits_because
from engine.doctor.author import (
    ApplyContext,
    AskPerson,
    AuthorAction,
    Create,
    SetSetting,
    Withdraw,
    find_item,
    in_section,
    proposal_of,
    setting_create,
    withdrawal_of,
)

if TYPE_CHECKING:
    from engine.doctor.case import Case, CaseItem

__all__ = ["HOW_TO_FIX", "check_action"]

HOW_TO_FIX: Final[dict[str, str]] = {
    "ITEM_NOT_FOUND": "Use the id of a change in the plan, as the state shows it.",
    "APPROVAL_IS_PERSONS": "Propose the change and leave the decision out; the person's click is what approves.",
    "OUTCOME_IS_PERSONS": "Ask the person which column says whether, or point at an answer of the question that "
    "asks it; only their answer sets the outcome, and only a yes/no column can be one.",
    "FEATURE_IS_ID": "Leave the customer ID out of the features; it names the rows and the model must not learn "
    "from it.",
    "STEP_ON_ROLE_COLUMN": "Propose the step so it waits for the person's tick, or use another column.",
    "SETTING_VALUE_NOT_ALLOWED": "Use one of the values the goal allows for that setting.",
    "LEFT_OUT_BY_PERSON": "Ask the person to tick it again, or propose a different change.",
}
"""A fix sentence for every code of this layer, used when the check has nothing more specific to say."""
_DECISION_KEYS: Final[frozenset[str]] = frozenset(
    {"decided_by", "decided_at", "approved", "approved_by", "approval"}
)
"""Payload keys that say a decision was made: a change the author proposes carries none."""
_OUTCOME_KIND: Final[str] = "outcome_column"
_HANDLES_SHOWN: Final[int] = 8


def _refuse(code: str, what_failed: str, value: str, how_to_fix: str = "") -> Refusal:
    return Refusal(code=code, what_failed=what_failed, value=value, how_to_fix=how_to_fix or HOW_TO_FIX[code])


def check_action(case: Case, action: AuthorAction, ctx: ApplyContext) -> Refusal | None:
    """Why `action` may not be applied (None: it may). Pure: it reads the case and the context, nothing else."""
    match action:
        case Create():
            return (
                _approval(action)
                or target_refusal(case, action.replaces)
                or _outcome(action, ctx)
                or _feature_is_id(action, ctx)
                or _proposal(case, action, ctx)
            )
        case Withdraw():
            old = find_item(case, action.item_id)
            if old is None:
                return target_refusal(case, action.item_id)
            return (
                None
                if old.state is ProposalState.REJECTED
                else _left_out(case, withdrawal_of(ctx, old, action))
            )
        case SetSetting():
            return _setting(case, action, ctx)
        case AskPerson():
            return _outcome_asked(action, ctx)
    return None


def _live_handles(case: Case) -> str:
    live = [
        i.item_id for i in case.items if i.state is not ProposalState.REJECTED and in_section(i.goal_id, case)
    ]
    return ", ".join(live[:_HANDLES_SHOWN])


def target_refusal(case: Case, item_id: str | None) -> Refusal | None:
    """The change an action names must be in the plan's active section."""
    if item_id is None or find_item(case, item_id) is not None:
        return None
    live = _live_handles(case)
    return _refuse(
        "ITEM_NOT_FOUND",
        f"There is no item “{item_id}” in the plan.",
        item_id,
        f"{HOW_TO_FIX['ITEM_NOT_FOUND']} In the plan: {live}." if live else "The plan is empty.",
    )


def _approval(action: Create) -> Refusal | None:
    """A proposal carries no decision: not in its payload, and not in the step it holds."""
    payload = action.draft.payload
    step = payload.get("step")
    named = sorted(_DECISION_KEYS & set(payload))
    if isinstance(step, dict) and step.get("decided_by") not in (None, "agent"):
        named.append("step.decided_by")
    if not named:
        return None
    return _refuse(
        "APPROVAL_IS_PERSONS",
        f"“{action.draft.title[:60]}” says it was decided; the author only proposes.",
        ", ".join(named),
    )


def _outcome(action: Create, ctx: ApplyContext) -> Refusal | None:
    """The AI does not set the outcome: it is the person's answer, or a rule's suggestion that they approve."""
    adopted = action.basis.startswith("s:") and action.basis[2:] in ctx.suggested
    if ctx.author != "ai" or action.draft.kind != _OUTCOME_KIND or adopted:
        return None
    column = action.draft.payload.get("column")
    return _refuse(
        "OUTCOME_IS_PERSONS",
        "The outcome is the person's to choose, by answering the question that asks it.",
        column if isinstance(column, str) else "",
    )


def _outcome_asked(action: AskPerson, ctx: ApplyContext) -> Refusal | None:
    """A question the AI wrote does not answer itself with the outcome."""
    ask = action.ask
    if ctx.author != "ai" or ask.proven is None:
        return None
    if not any(o.effect.kind == _OUTCOME_KIND for o in ask.options):
        return None
    return _refuse(
        "OUTCOME_IS_PERSONS",
        "The outcome is the person's to choose, not the question's own answer.",
        ask.code,
    )


def _feature_is_id(action: Create, ctx: ApplyContext) -> Refusal | None:
    """No feature of the plan is the column that names each row."""
    features = action.draft.payload.get("features")
    if not isinstance(features, list):
        return None
    for feature in features:
        name = feature.get("name") if isinstance(feature, dict) else None
        if isinstance(name, str) and ctx.roles.names_id(name):
            return _refuse(
                "FEATURE_IS_ID",
                f"“{name[:60]}” is the customer ID, which names the rows; it cannot be a feature.",
                name[:60],
            )
    return None


def _proposal(case: Case, action: Create, ctx: ApplyContext) -> Refusal | None:
    """What the change would be once written: a step on a column that holds a meaning waits for the tick
    (`waits_because`), so refuse the one that would not; and never the same change the person left out."""
    item = proposal_of(case, ctx, action)
    raw = action.draft.payload.get("step")
    if isinstance(raw, dict) and ctx.roles.touches(raw) and waits_because(item, ctx.roles) is None:
        column = raw.get("column") or raw.get("new_column")
        return _refuse(
            "STEP_ON_ROLE_COLUMN",
            "This step changes a column that holds a meaning, and it would be in the plan without a tick.",
            column if isinstance(column, str) else "",
        )
    return _left_out(case, item)


def _setting(case: Case, action: SetSetting, ctx: ApplyContext) -> Refusal | None:
    allowed = [str(v) for v in ctx.goal.allowed(action.setting)] if ctx.goal is not None else []
    if ctx.roles.names_id(action.value):
        return _refuse("FEATURE_IS_ID", "A setting cannot make the customer ID a feature.", action.setting)
    if action.value not in allowed:
        return _refuse(
            "SETTING_VALUE_NOT_ALLOWED",
            f"{action.value} is not an allowed value of {action.setting}.",
            action.setting,
            (
                f"Use one of: {', '.join(allowed)}."
                if allowed
                else f"The chosen goal has no setting named {action.setting}."
            ),
        )
    return _left_out(case, proposal_of(case, ctx, setting_create(ctx, case.items, action)))


def _left_out(case: Case, candidate: CaseItem) -> Refusal | None:
    """The same change the person already left out is not put back unless they tick it again."""
    twin = twin_of(case.items, candidate)
    if twin is None or twin.state is not ProposalState.REJECTED or twin.withdrawn_because is not None:
        return None
    return _refuse(
        "LEFT_OUT_BY_PERSON",
        f"The person said no to “{twin.title}”, so it is not proposed again.",
        twin.item_id,
    )
