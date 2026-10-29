"""What the AI is told about the chosen goal and the build it asks for, so it does not guess.

The chosen goal is named with its outcome definition (`goal_line`), and while the goal's outcome has to be built from
events (`engine.doctor.buildable`) the AI reads the build's parameters: the outcome column the goal expects, the
window, the customer and date columns, the snapshot rule and the snapshot it gives now, and the audience the person
chose. `build_is_owed` says when the next AI turn has to propose that build: a buildable goal is chosen, nothing
decides its outcome yet, and no query that makes the table is waiting or in the plan.

Everything here is a count, a column name (through the file's egress gate) or a sentence of ours; no cell text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from engine.agent.contracts import ProposalState
from engine.doctor.decisions import decisions

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from engine.doctor.buildable import BuildableOutcome
    from engine.doctor.case import Case
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief

__all__ = ["build_facts", "build_is_owed", "decided_numbers", "goal_line", "to_do_now"]

SNAPSHOT_RULE: Final[str] = (
    "the snapshot is the latest month-end whose outcome window is over by the last day of data (when the data has "
    "no such month-end after its first day, the latest day whose window is over); every feature is read on or "
    "before it and only the outcome reads the window after it"
)
TO_DO_BUILD: Final[str] = (
    "The goal's outcome is not in the files, so it has to be built, and no build is waiting or in the plan yet. "
    "Propose it now with `propose_sql` (one row per customer, features read on or before the snapshot, the outcome "
    "column named as `chosen_goal.outcome_column` says, the customer column kept under its own name), then tell the "
    "person it waits for their tick. Do not wait for them to ask."
)
TO_DO_AUDIENCE: Final[str] = (
    "The goal's outcome has to be built, and the person has not said which customers it is about. Point at the "
    "answer the goal's own words give with `suggest_answer`, say why in a sentence with both counts, and propose "
    "the build once they have answered. Do not wait for them to ask."
)


def to_do_now(case: Case) -> str | None:
    """The next thing the AI must do whatever it is asked: propose the missing build (or, while the person has not
    said which customers it is about, point at that answer), or None."""
    if not build_is_owed(case):
        return None
    from engine.doctor.visit import active_section

    open_audience = any(q.code == "BUILD_AUDIENCE" and q.answer is None for q in active_section(case)[1])
    return TO_DO_AUDIENCE if open_audience else TO_DO_BUILD


def goal_line(brief: GoalBrief | None, default: str) -> str:
    """The goal as the prompts name it: its name and what a yes means (`default` while no goal is chosen)."""
    if brief is None:
        return default
    means = brief.outcome.definition.strip().rstrip(".")
    return f"{brief.goal_name}" + (f". A yes means: {means}." if means else ".")


def _live_query(case: Case) -> bool:
    """Whether a query that makes a table waits for the person or is in the plan (or the person declined the build)."""
    from engine.doctor.visit import active_section

    for item in active_section(case)[0]:
        step = item.payload.get("step")
        if item.kind == "recipe_step" and isinstance(step, dict) and step.get("kind") == "sql_query":
            if item.state in (ProposalState.PENDING, ProposalState.ACCEPTED):
                return True
            if item.state is ProposalState.REJECTED and "buildable" in item.payload:
                return True  # the person said no to the build: it is not proposed again
    return False


def build_is_owed(case: Case) -> bool:
    """A buildable goal is chosen, nothing decides its outcome and no build is pending or accepted: the next AI turn
    proposes the build."""
    from engine.doctor.visit import active_goal, fitting_goals

    goal = active_goal(case)
    if goal is None:
        return False
    buildable = any(
        str(u.get("use_case_id")) == goal.goal_id and u.get("status") == "buildable"
        for u in fitting_goals(case)
    )
    return buildable and decisions(case).pair("outcome", goal.goal_id) is None and not _live_query(case)


def build_facts(
    case: Case,
    brief: GoalBrief | None,
    files: Sequence[LoadedFile],
    config_root: Path | None,
    label: Callable[[str, str], str],
) -> dict[str, Any]:
    """The chosen goal and, when its outcome is built from events, the build's parameters. `label(upload_id, name)`
    gives a column name as the AI may read it. Empty while no goal is chosen."""
    from engine.config import load_use_case
    from engine.doctor.buildable import buildable_outcome
    from engine.doctor.visit import active_goal

    goal = active_goal(case)
    if goal is None or brief is None:
        return {}
    chosen: dict[str, Any] = {
        "goal": brief.goal_name,
        "a_yes_means": brief.outcome.definition,
        "outcome_column": brief.outcome.column,
    }
    try:
        config = load_use_case(goal.goal_id, config_root)
        built = buildable_outcome(files, config, config_root)
    except Exception:  # a goal whose configuration cannot be read has no build to describe
        built = None
    if built is None or _outcome_in_files(case, goal.goal_id):
        return {"chosen_goal": chosen}
    chosen["outcome_column"] = config.target.column or chosen["outcome_column"]
    return {"chosen_goal": chosen, "goal_build": _parameters(case, goal.goal_id, built, config.entity, label)}


def _outcome_in_files(case: Case, goal_id: str) -> bool:
    """The goal's outcome is decided by a column and no query builds it: there is no build to describe."""
    return decisions(case).pair("outcome", goal_id) is not None and not _live_query(case)


def _parameters(
    case: Case, goal_id: str, built: BuildableOutcome, entity: str, label: Callable[[str, str], str]
) -> dict[str, Any]:
    chosen = next(
        (
            q
            for q in case.questions
            if q.code == "BUILD_AUDIENCE" and q.goal_id == goal_id and q.answer is not None
        ),
        None,
    )
    audience = (
        next((a.label for a in built.audiences if a.audience_id == chosen.answer), "the person's choice")
        if chosen is not None
        else (
            "not chosen yet: the person picks between the readings below"
            if built.audiences
            else f"every {entity or 'customer'} active on or before the snapshot"
        )
    )
    facts: dict[str, Any] = {
        "outcome_window_days": built.window_days,
        "outcome_is": (
            "1 when the customer has a row in the window after the snapshot, else 0"
            if built.label_type == "event_presence"
            else "1 when the customer has no row in the window after the snapshot, else 0"
        ),
        "customer_column": label(built.upload_id, built.entity_key),
        "date_column": label(built.upload_id, built.time_column),
        "data_runs": f"{built.data_start.isoformat()} to {built.data_end.isoformat()}",
        "snapshot_rule": SNAPSHOT_RULE,
        "audience": audience,
    }
    if built.snapshot is not None:
        facts["snapshot_now"] = built.snapshot.isoformat()
    if built.audiences and chosen is None:
        facts["readings"] = [
            {"audience": a.audience_id, "customers": a.customers, "yes_count": a.yes} for a in built.audiences
        ]
    if build_is_owed(case):
        facts["build_wanted"] = True
    return facts


def decided_numbers(case: Case) -> list[str]:
    """The as-of and snapshot dates and the outcome window the case decided, as text: numbers the AI may repeat."""
    return [
        str(record.value)
        for record in decisions(case).records
        if record.kind in {"as_of", "training_as_of", "window"}
    ]
