"""Which changes the Data Doctor's AI may put into the plan for the person, and which wait for their tick.

The chat's AI never ticks anything itself. What it proposes is pending, `check`, never ticked; the **visit** then
applies the ones that are safe - the agent's own tick, `decided_by` the agent, one Undo, on the timeline - and the
chat asks the same question before it writes its reply, so what it says it did is what the visit does.

A change is applied for the person only when all of these hold:

* it is a kind in :data:`AGENT_APPLIED_STEPS` with only the parameters listed there (a typed step), or a join or stack
  that loses nobody and needs no yes of its own (`protocol.approve_class_of`). Everything that gives a value a new
  meaning (yes/no words, merged spellings, units, filled blanks, ranges), decides how dates or numbers are read, hides
  a column, or builds a table by a query waits for the tick. So do the outcome, consent, the ID and the date: the AI
  may suggest an answer, only a click records it;
* it carries no warning from the change-share rule and needs no yes of its own;
* the plan as it would stand with it, run once on the sample under the guards (`plan_check.check_plan`), shows nothing
  new about it that **stops or flags**, and nothing new about the whole plan that stops it. One flag is expected and does not count: leaving rows out loses customers, which is its
  purpose; it is APPROVE-class, so the approval still asks the person for their own yes, and a loss over the guards'
  limit is a stop.

Nothing here reads a cell or calls the AI.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Final

from engine.agent.contracts import ProposalSource, ProposalState
from engine.doctor.plan_check import check_plan
from engine.doctor.protocol import AGENT, approve_class_of

if TYPE_CHECKING:
    from engine.doctor.case import CaseFile, CaseItem
    from engine.doctor.diagnose import LoadedFile

__all__ = ["AGENT_APPLIED_STEPS", "Applied", "agent_applies", "waits_because"]

AGENT_APPLIED_STEPS: Final[dict[str, frozenset[str]]] = {
    "parse_number": frozenset(),
    "normalise_text": frozenset({"strip"}),
    "split_column": frozenset({"separator", "part"}),
    "rename_column": frozenset(),
    "remove_rows": frozenset({"operator", "values"}),
}
"""The recipe steps the chat's AI may apply for the person (they undo it with one click) once its guard run shows
no stop and no flag, and the parameters each may carry. A step that decides how to read something - the order of day
and month, a decimal comma, lakh words, which spellings are one - or that gives a value a new meaning (yes/no words,
units, filled blanks, ranges, "no value" words) or hides a column waits for the person's tick. Leaving rows out is
APPROVE-class: the plan still carries it as an own-yes at the approval."""
_DEFAULT_PARAMS: Final[dict[str, str]] = {"decimal": "."}
EXPECTED_FLAGS: Final[dict[str, frozenset[str]]] = {"remove_rows": frozenset({"STEP_LOSES_CUSTOMERS"})}
"""A flag a step is expected to raise by its nature, by step kind: it is shown on the change, not a reason to wait."""

Applied = tuple["CaseItem", str | None]
"""A change as the visit will write it, and why it waits for the person's tick (None: applied for them)."""


def waits_because(item: CaseItem) -> str | None:
    """Why this change cannot be applied for the person whatever the guards say (None: it may be)."""
    from engine.doctor.combine_run import JOIN_KIND, STACK_KIND

    payload = item.payload
    if item.source is not ProposalSource.AI or item.state is not ProposalState.PENDING:
        return "it is not a new change of the AI's"
    if payload.get("warnings"):
        return "it changes many of the values in its column"
    if payload.get("needs_yes"):
        return "it needs your own yes"
    if item.kind == "recipe_step":
        raw = payload.get("step")
        allowed = AGENT_APPLIED_STEPS.get(str(raw.get("kind"))) if isinstance(raw, dict) else None
        if allowed is None:
            return "this kind of change is yours to tick"
        params = raw.get("params") if isinstance(raw, dict) else None
        used = (
            {k for k, v in params.items() if v and v != _DEFAULT_PARAMS.get(k)}
            if isinstance(params, dict)
            else set()
        )  # a parameter left empty or at its default decides nothing
        if used - allowed:
            return "how it reads the values is yours to confirm"
        return None
    if item.kind in (JOIN_KIND, STACK_KIND):
        found = payload.get("findings")
        if payload.get("receipt") is None or not isinstance(found, list):
            return "its effect could not be counted"
        if any(isinstance(f, dict) and f.get("level") in {"stop", "flag"} for f in found):
            return "the checks found something to look at"
        if approve_class_of(item.kind, payload) is not None:
            return "it needs your own yes"
        return None
    return "this kind of change is yours to tick"


def agent_applies(
    files: Sequence[LoadedFile],
    case_files: Sequence[CaseFile],
    plan: Sequence[CaseItem],
    fresh: Sequence[CaseItem],
    now: datetime,
) -> list[Applied]:
    """`fresh`, each with the agent's tick when it may apply and `None` for why it waits when it may not.

    `plan` is the active items as they stand (with the agent's withdrawals); it grows with each change applied, so
    the second change is judged with the first in place."""
    active = list(plan)
    out: list[Applied] = []
    counted = None
    for item in fresh:
        reason = waits_because(item)
        if reason is not None:
            out.append((item, reason))
            continue
        trial = item.model_copy(
            update={"state": ProposalState.ACCEPTED, "decided_by": AGENT, "decided_at": now}
        )
        if item.kind == "recipe_step":
            step = item.payload.get("step")
            expected = EXPECTED_FLAGS.get(
                str(step.get("kind")) if isinstance(step, dict) else "", frozenset()
            )
            counted = counted or check_plan(files, case_files, active)
            after = check_plan(files, case_files, [*active, trial])
            seen = {f.key for f in counted.findings}
            found = [
                f
                for f in after.findings
                if f.key not in seen
                and f.level in {"stop", "flag"}
                and f.code not in expected
                and f.item_id in (item.item_id, None)  # its own, or the whole plan's; not another step's
            ]
            if found:
                out.append((item, found[0].text))
                continue
            counted = after
        active.append(trial)
        out.append((trial, None))
    return out
