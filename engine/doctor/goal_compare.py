"""Does the file fit another goal better than the one the person chose? (the owner's "also fits other goals")

The goal list (`engine.doctor.diagnose.readiness`) already ranks every goal by how well the files fit it
(`fit_level`: ready, check, a build or a question about the outcome column, too little, missing; a plain fit ranks
before the same fit that still has to ask the person something). This module only reads that ranking; it is not a
second one. A goal is **clearly better** than the chosen goal when it fits (`FITTING`) and its level is strictly
better than the chosen goal's; goals that fit equally well, or less well, are never offered. The choice is the person's: the
default author asks one question with the better goals, ranked as the list ranks them, and "Keep" the chosen goal.
Choosing another goal switches the case's goal through the ordinary goal switch (`engine.doctor.visit.set_goal`), in
the same step as the answer.

The question never blocks, even when the chosen goal does not fit at all: a blocking question holds the AI's turn,
and the gap that makes the chosen goal "missing" is often what that turn closes (it proposes the outcome, the build,
the test column). What the gap costs is already held where it belongs: Approve waits while the data does not fit the
goal (`conversation.approval_holds`, "Choose another goal, or add the data this one needs"), and a question that
the author stops suggesting once the gap is closed is taken back.

Nothing here reads a cell: the verdicts are the goal list's own, with its plain reasons.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import JsonValue

from engine.doctor.diagnose import fit_level
from engine.doctor.goal_names import goal_display_name
from engine.doctor.protocol import STAY_GOAL, SWITCH_GOAL, Ask, AskOption
from engine.doctor.visit_contracts import OptionEffect

__all__ = [
    "BETTER_SHOWN",
    "COMPARE_CODE",
    "FITTING",
    "KEEP_OPTION",
    "GoalComparison",
    "compare_goals",
    "comparison_ask",
    "comparison_facts",
]

FITTING: Final[frozenset[str]] = frozenset({"ready", "check", "buildable"})
"""Verdicts that fit a goal (`buildable` fits once Data Doctor builds the outcome and the person approves it)."""
BETTER_SHOWN: Final[int] = 3
"""Better goals offered; with "Keep" the question has at most four answers."""
COMPARE_CODE: Final[str] = "GOAL_FIT_COMPARE"
KEEP_OPTION: Final[str] = "keep"


def _fits(verdict: Mapping[str, Any]) -> bool:
    return verdict.get("status") in FITTING


def _rank(verdict: Mapping[str, Any]) -> int:
    return fit_level(
        str(verdict.get("status")), verdict.get("reason_code"), int(verdict.get("questions") or 0)
    )


def _reason(verdict: Mapping[str, Any]) -> str:
    """One plain line from the verdict's own fields: the test and its control group, the outcome that is there or
    can be built; for a goal that does not fit, what the goal list says is missing."""
    if _fits(verdict):
        balance = verdict.get("balance")
        parts: list[str] = []
        if isinstance(balance, Mapping) and balance.get("control_group"):
            parts.append(f"it has a control group (“{balance['control_group']}”) to compare against")
        if verdict.get("status") == "buildable":
            parts.append("the outcome can be built from the events")
        elif verdict.get("outcome_column"):
            parts.append(f"the outcome column “{verdict['outcome_column']}” is there")
        if parts:
            sentence = ", and ".join(parts)
            return f"{sentence[0].upper()}{sentence[1:]}."
    return str(verdict.get("summary") or verdict.get("why") or "").strip()


@dataclass(frozen=True)
class GoalComparison:
    """The chosen goal's verdict and the goals that fit the file clearly better, best first."""

    chosen: Mapping[str, Any]
    better: tuple[Mapping[str, Any], ...]

    @property
    def chosen_id(self) -> str:
        return str(self.chosen["use_case_id"])

    @property
    def chosen_fits(self) -> bool:
        return _fits(self.chosen)


def compare_goals(verdicts: Sequence[Mapping[str, Any]], chosen_id: str) -> GoalComparison | None:
    """The goals that fit clearly better than `chosen_id`; None when the chosen goal is the best fit (or ties for it)
    or the goal list does not know the chosen goal. `verdicts` are the goal list's, best first."""
    chosen = next((v for v in verdicts if v.get("use_case_id") == chosen_id), None)
    if chosen is None:
        return None
    floor = _rank(chosen)
    better = tuple(
        v for v in verdicts if v.get("use_case_id") != chosen_id and _fits(v) and _rank(v) < floor
    )[:BETTER_SHOWN]
    return GoalComparison(chosen=chosen, better=better) if better else None


def _name(verdict: Mapping[str, Any]) -> str:
    return goal_display_name(str(verdict["use_case_id"]), str(verdict.get("name") or ""))


def comparison_facts(comparison: GoalComparison | None) -> dict[str, Any] | None:
    """What the AI sees of the comparison (names, verdicts and the goal list's own reasons)."""
    if comparison is None:
        return None
    return {
        "chosen": {
            "goal_id": comparison.chosen_id,
            "name": _name(comparison.chosen),
            "status": comparison.chosen.get("status"),
            "fits": comparison.chosen_fits,
            "reason": _reason(comparison.chosen),
        },
        "fits_better": [
            {
                "goal_id": str(v["use_case_id"]),
                "name": _name(v),
                "status": v.get("status"),
                "reason": _reason(v),
            }
            for v in comparison.better
        ],
    }


def comparison_ask(comparison: GoalComparison, *, upload_ids: Sequence[str]) -> Ask:
    """The one question: the better goals, ranked as the goal
    list ranks them, each with its reason, then "Keep" the chosen goal (the safe answer, recommended only while it
    still fits). The question's id holds the files and the goals compared, so it is not asked again for the same
    files and goals."""
    chosen = _name(comparison.chosen)
    fits = comparison.chosen_fits
    options = [
        AskOption(
            label=_name(v),
            effect=OptionEffect(
                option_id=f"use.{v['use_case_id']}"[:64], payload={SWITCH_GOAL: str(v["use_case_id"])}
            ),
            says=f"{_reason(v)} Switches this check to {_name(v)}.".strip(),
            sets="note",
        )
        for v in comparison.better
    ]
    needs = _reason(comparison.chosen) or "more from the file."
    options.append(
        AskOption(
            label=f"Keep {chosen}",
            effect=OptionEffect(option_id=KEEP_OPTION, payload={STAY_GOAL: comparison.chosen_id}),
            safe=True,
            says=(
                f"Stays with {chosen}; nothing changes."
                if fits
                else f"Stays with {chosen}, which needs: {needs}"
            ),
        )
    )
    subject: dict[str, JsonValue] = {
        "uploads": sorted(upload_ids),
        "chosen": comparison.chosen_id,
        "better": [str(v["use_case_id"]) for v in comparison.better],
    }
    return Ask(
        code=COMPARE_CODE,
        subject=subject,
        text=(
            "This file also fits other goals. Which one should this check be for?"
            if fits
            else f"This file does not fit {chosen} yet, but it fits other goals. Which one should this check be for?"
        ),
        why=(
            f"You chose {chosen}. These goals fit this file better; the choice is yours."
            if fits
            else f"You chose {chosen}, and the file is missing what it needs. These goals fit this file."
        ),
        options=tuple(options),
        rows=0,
        goal_id=comparison.chosen_id,
        blocking=False,  # see the module docstring: it never holds the Doctor's own work, Approve holds on the fit
        recommended=KEEP_OPTION if fits else None,
    )
