"""Which changes the author may put into the plan for the person, and which wait for their tick.

:func:`waits_because` is the one rule of placement, for the AI and for the default author alike: a change it answers
None for goes into the plan ("in the plan, undecided": nobody has decided it, and the approval does), any other waits
as a proposal. What the values prove (`sure`) and the goal's own build go in as well. The author never ticks anything
itself, and `decided_by` is not written for it.

Until the visit is rebuilt around the one author (`engine.doctor.author`), the chat's AI still goes through
:func:`agent_applies`: what it proposes is pending, `check`; the **visit** then applies the ones that are safe - the
agent's own tick, `decided_by` the agent, one Undo, on the timeline - and the chat asks the same question before it
writes its reply, so what it says it did is what the visit does.

A change is applied for the person only when all of these hold:

* it is a kind in :data:`AGENT_APPLIED_STEPS` with only the parameters listed there (a typed step), or a join or stack
  that loses nobody and needs no yes of its own (`protocol.approve_class_of`). Everything that gives a value a new
  meaning (yes/no words, merged spellings, units, filled blanks, ranges), decides how dates or numbers are read, hides
  a column, or builds a table by a query waits for the tick. So do the outcome, consent, the ID and the date: the AI
  may suggest an answer, only a click records it;
* it touches no column that holds a meaning: the outcome, the ID (but for leaving out rows by it), the date a row
  describes, who agreed to be contacted or opted out (:class:`Roles`). A rename, a split or a reading of one of
  those is the person's to tick;
* it carries no warning from the change-share rule and needs no yes of its own;
* the plan as it would stand with it, run once on the sample under the guards (`plan_check.check_plan`), shows
  nothing new that **stops or flags**, on this step or any other. One flag on the step itself is expected and does
  not count: leaving rows out loses customers, which is its purpose; it is APPROVE-class, so the approval still asks
  the person for their own yes, and a loss over the guards' limit is a stop. A rule that would leave out more than
  that limit of the whole file, or that reads dates whose day/month order nobody settled, waits as well.

Nothing here reads a cell or calls the AI.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final

from engine.agent.contracts import AgentConfidence, ProposalState
from engine.doctor.decisions import role_columns
from engine.doctor.limits import limits
from engine.doctor.plan_check import check_plan
from engine.doctor.protocol import AGENT, approve_class_of
from engine.doctor.subjects import SETTING_KIND, WITHDRAW_KIND

if TYPE_CHECKING:
    from pathlib import Path

    from engine.doctor.case import Case, CaseFile, CaseItem
    from engine.doctor.diagnose import LoadedFile

__all__ = [
    "AGENT_APPLIED_STEPS",
    "NO_ROLES",
    "Applied",
    "Roles",
    "agent_applies",
    "meaning_key",
    "person_owns",
    "required_by_goal",
    "roles_of",
    "twin_of",
    "waits_because",
]

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


_CONSENT_WORDS: Final = re.compile(
    r"(consent|opt[\W_]*(in|out)|optin|optout|unsubscri|suppress|do[\W_]*not[\W_]*(call|contact|disturb)|dnd|gdpr|permission)",
    re.IGNORECASE,
)
"""Column names that read as who may be contacted, whatever the goal's settings say (no goal chosen yet)."""
_TOUCHES_MEANING: Final[str] = (
    "it touches who may be contacted or what the model learns; that is yours to tick"
)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


@dataclass(frozen=True)
class Roles:
    """The columns of a case that hold a meaning, by name (case and punctuation aside): the AI never changes them
    for the person."""

    protected: frozenset[str] = frozenset()
    """The outcome, the date a row describes, the split and fairness columns, who agreed or opted out."""
    ids: frozenset[str] = frozenset()
    """The column that names each row. Leaving rows out by it changes no ID, so that one step is allowed."""

    def touches(self, step: dict[str, Any]) -> bool:
        """Whether a recipe step (as its payload dump) reads, rewrites or creates a column that holds a meaning."""
        kind = str(step.get("kind"))
        for name in (step.get("column"), step.get("new_column")):
            if not isinstance(name, str):
                continue
            norm = _norm(name)
            if norm in self.protected or _CONSENT_WORDS.search(name):
                return True
            if norm in self.ids and kind != "remove_rows":
                return True
        return False


NO_ROLES: Final[Roles] = Roles()
_BRIEF_NEEDS: Final[frozenset[str]] = frozenset(
    {"outcome", "time", "consent", "recently_contacted", "treatment"}
)


def roles_of(case: Case, config_root: Path | None = None) -> Roles:
    """The case's columns that hold a meaning: what the person and the rules decided (key, outcome, consent, date),
    what each goal's settings reserve (the outcome, the time and split columns, the consent and opt-out columns of
    the goal and of each channel), and the names a goal's brief says those are usually found under - so the outcome
    is covered before anyone has answered which column it is. Nothing here reads a cell."""
    from engine.config import ConfigError, load_use_case
    from engine.doctor.goal_brief import load_goal_brief

    named_ids, protected = role_columns(
        case
    )  # named in any state: a column once proposed is not the AI's to touch
    ids: set[str] = {c for f in case.files for c in f.key_columns} | named_ids
    for goal in case.goals:
        try:
            config = load_use_case(goal.goal_id, config_root)
            brief = load_goal_brief(goal.goal_id, root=config_root)
        except (ConfigError, OSError, ValueError):
            continue
        protected.update(config._reserved_columns())
        for channel in config.actions.suppression.channels.values():
            protected.update(c for c in (channel.consent_column, channel.contactable_column) if c)
        for needed in brief.required_columns:
            if needed.need in _BRIEF_NEEDS:
                protected.update(needed.names)
            elif needed.need == "key":
                ids.update(needed.names)
    return Roles(protected=frozenset(_norm(c) for c in protected), ids=frozenset(_norm(c) for c in ids))


def person_owns(case: Case, item: CaseItem) -> bool:
    """Whether the person has taken a change as their own: they ticked or unticked it, or gave it their own yes
    (an agent-applied change they confirmed keeps its place in the plan whatever the agent says later)."""
    return (item.decided_by not in (None, AGENT)) or any(item.item_id in a.item_ids for a in case.approvals)


def required_by_goal(item: CaseItem) -> bool:
    """Whether the item is the goal's build as the author put it in the plan: ticked because the goal needs it, with
    no person's decision on it yet. Their own yes at Approve is what makes it theirs (`visit.confirm`)."""
    return (
        item.state is ProposalState.ACCEPTED
        and item.decided_by is None
        and bool(item.payload.get("needs_yes"))
        and isinstance(item.payload.get("buildable"), dict)
    )


_WORDING: Final[frozenset[str]] = frozenset({"reason", "order", "proposal_id", "decided_by"})
_VOLATILE_PARAMS: Final[frozenset[str]] = frozenset({"tried_after"})
"""Step parameters that record when or on what it was tried (the rows a trial counted after, the date it read), not
what the step does: the same query re-proposed after a tick has another `tried_after` and is the same change."""


def meaning_key(item: CaseItem) -> str:
    """What a change does, without the words around it: a typed or SQL step with its reason, order and proposal left
    out. The id of a change hashes the model's wording, so the same filter re-proposed with another reason has
    another id; this is what a person's earlier "no" is compared by. Taking an item out of the plan is the item it
    takes out, and a goal's setting is its value."""
    payload = item.payload
    if item.kind == WITHDRAW_KIND:
        return f"{item.kind}|{payload.get('item_id')}"
    if item.kind == SETTING_KIND:
        return f"{item.kind}|{payload.get('goal_id')}|{payload.get('setting')}|{payload.get('value')}"
    step = payload.get("step")
    if not isinstance(step, dict):
        return item.item_id
    body = {k: v for k, v in step.items() if k not in _WORDING}
    params = body.get("params")
    if isinstance(params, dict):  # what a trial found when it was made is not what the step does
        # the date a step reads is part of what it does, unless a rule derives it (then it is only what the rule gave)
        gone = _VOLATILE_PARAMS | ({"as_of"} if params.get("as_of_rule") else set())
        body["params"] = {k: v for k, v in params.items() if k not in gone}
    return f"{item.kind}|{item.upload_id}|{json.dumps(body, sort_keys=True, default=str)}"


def twin_of(items: Sequence[CaseItem], item: CaseItem) -> CaseItem | None:
    """The change already in the case that is this same change (same id, or the same step in other words); a
    waiting or accepted one before a left-out one."""
    key = meaning_key(item)
    same = [
        i
        for i in items
        if (i.item_id == item.item_id or meaning_key(i) == key)
        and (i.withdrawn_because is None or i.item_id == item.item_id)  # taken back by the Doctor: not a "no"
    ]
    same.sort(key=lambda i: i.state is ProposalState.REJECTED)
    return same[0] if same else None


def waits_because(item: CaseItem, roles: Roles = NO_ROLES) -> str | None:
    """Why this change cannot go into the plan before the person's tick, whoever proposes it, whatever the guards say
    (None: it may). The author never decides this; the plan's placement is this answer."""
    from engine.doctor.combine_run import JOIN_KIND, STACK_KIND

    payload = item.payload
    if item.state is not ProposalState.PENDING:
        return "it is not a new change"
    if payload.get("warnings"):
        return "it changes many of the values in its column"
    if payload.get("needs_yes"):
        # The goal's own build is in the plan and has its yes at Approve; any other change that needs one waits.
        return None if isinstance(payload.get("buildable"), dict) else "it needs your own yes"
    # What the values prove is in the plan, shown, and the person may untick it; it still keeps to the rules below.
    sure = item.confidence is AgentConfidence.SURE
    if item.kind == "recipe_step":
        raw = payload.get("step")
        if sure:  # proof does not lift the protection of a column that holds a meaning
            return _TOUCHES_MEANING if isinstance(raw, dict) and roles.touches(raw) else None
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
        if isinstance(raw, dict) and roles.touches(raw):
            return _TOUCHES_MEANING
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
    return None if sure else "this kind of change is yours to tick"


def _filter_waits(files: Sequence[LoadedFile], item: CaseItem) -> str | None:
    """Leaving rows out by a rule, judged on the whole file (the guard run sees only its first rows): a rule that
    would leave out more than the guards' limit waits, and so does a date rule over day/month dates nobody has
    settled the order of (the rows left out would depend on a guess)."""
    from engine.agent.formats import order_unsettled
    from engine.agent.value_steps import remove_rows

    raw = item.payload.get("step")
    if not isinstance(raw, dict) or raw.get("kind") != "remove_rows":
        return None
    loaded = next((f for f in files if f.upload_id == item.upload_id), None)
    column, params = raw.get("column"), raw.get("params")
    if loaded is None or not isinstance(column, str) or column not in loaded.frame.columns:
        return None
    if not isinstance(params, dict) or not len(loaded.frame):
        return None
    operator = str(params.get("operator") or "")
    cells = loaded.frame[column]
    if operator in {"before", "after"} and order_unsettled(cells):
        return "the dates in that column could be day first or month first; which one is yours to say"
    values = params.get("values")
    try:
        kept = remove_rows(cells, operator=operator, values=values if isinstance(values, list) else [])
    except (ValueError, TypeError, KeyError):
        return None  # the guard run says what is wrong with it
    share = 100.0 * float((~kept).mean())
    stop = limits().gates.loss_stop_pct
    if share > stop:
        return f"it would leave out {share:.0f}% of the rows of the whole file, more than {stop:g}%"
    return None


def agent_applies(
    files: Sequence[LoadedFile],
    case_files: Sequence[CaseFile],
    plan: Sequence[CaseItem],
    fresh: Sequence[CaseItem],
    now: datetime,
    roles: Roles = NO_ROLES,
    as_of: date | None = None,
    training_as_of: date | None = None,
) -> list[Applied]:
    """`fresh`, each with the agent's tick when it may apply and `None` for why it waits when it may not.

    `plan` is the active items as they stand (with the agent's withdrawals); it grows with each change applied, so
    the second change is judged with the first in place. `as_of` is the case's decided as-of date, so the check binds
    `$as_of` as the materialiser does."""
    active = list(plan)
    out: list[Applied] = []
    counted = None
    for item in fresh:
        reason = waits_because(item, roles) or _filter_waits(files, item)
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
            counted = counted or check_plan(
                files, case_files, active, as_of=as_of, training_as_of=training_as_of
            )
            after = check_plan(
                files, case_files, [*active, trial], as_of=as_of, training_as_of=training_as_of
            )
            seen_keys = {f.key for f in counted.findings}
            seen_codes = {(f.code, f.item_id) for f in counted.findings}
            found = [
                f
                for f in after.findings
                if f.level in {"stop", "flag"}
                and not (f.code in expected and f.item_id == item.item_id)  # what the step is for
                and (
                    f.key not in seen_keys
                    if f.item_id in (item.item_id, None)  # its own, or the whole plan's
                    else (f.code, f.item_id) not in seen_codes  # another step's: only what is new there
                )
            ]
            if found:
                out.append((item, found[0].text))
                continue
            counted = after
        active.append(trial)
        out.append((trial, None))
    return out
