"""The file fits another goal better than the chosen one: the comparison and the one question it asks."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from engine.doctor.author import AskPerson
from engine.doctor.diagnose import fit_level
from engine.doctor.facts_goal import goal_compare_facts
from engine.doctor.goal_compare import (
    BETTER_SHOWN,
    COMPARE_CODE,
    KEEP_OPTION,
    compare_goals,
    comparison_ask,
    comparison_facts,
)
from engine.doctor.protocol import STAY_GOAL, SWITCH_GOAL, settle
from engine.doctor.tools_facts import Facts

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def verdict(goal: str, status: str = "ready", **more: Any) -> dict[str, Any]:
    return {"use_case_id": goal, "name": goal.title(), "status": status, "outcome_column": "bought", **more}


def test_the_levels_rank_a_plain_fit_before_one_that_still_asks() -> None:
    assert fit_level("ready", None) < fit_level("check", None) < fit_level("check", None, 1)
    assert fit_level("check", None, 1) < fit_level("buildable", None) < fit_level("too_small", None)
    assert fit_level("too_small", None) < fit_level("missing", None)
    assert fit_level("check", "GUESSED_COLUMN", 1) == fit_level("buildable", "BUILDABLE")


def test_a_goal_that_fits_clearly_better_is_offered_best_first() -> None:
    verdicts = [verdict("a", "ready"), verdict("b", "check"), verdict("c", "check", questions=1)]
    found = compare_goals(verdicts, "c")
    assert found is not None and found.chosen_fits
    assert [v["use_case_id"] for v in found.better] == ["a", "b"]


def test_the_best_fitting_goal_is_not_compared() -> None:
    verdicts = [verdict("a", "ready"), verdict("b", "ready"), verdict("c", "missing")]
    assert compare_goals(verdicts, "a") is None
    assert compare_goals(verdicts, "b") is None, "an equal fit is not a clearly better one"


def test_a_goal_that_does_not_fit_is_never_offered_and_an_unknown_goal_is_not_compared() -> None:
    verdicts = [verdict("a", "ready"), verdict("b", "missing"), verdict("c", "too_small")]
    found = compare_goals(verdicts, "c")
    assert found is not None and [v["use_case_id"] for v in found.better] == ["a"]
    assert compare_goals(verdicts, "elsewhere") is None


def test_at_most_three_better_goals_are_offered() -> None:
    verdicts = [verdict(name, "ready") for name in "abcde"] + [verdict("z", "check")]
    found = compare_goals(verdicts, "z")
    assert found is not None and len(found.better) == BETTER_SHOWN


def question(chosen: str, verdicts: list[dict[str, Any]]) -> Any:
    found = compare_goals(verdicts, chosen)
    assert found is not None
    return settle(comparison_ask(found, upload_ids=["u2", "u1"]), now=NOW).question


def test_the_question_ranks_the_better_goals_with_their_reasons_then_keeps_the_chosen_goal() -> None:
    balance = {"control_group": "No E-Mail"}
    asked = question(
        "c",
        [
            verdict("a", "ready", balance=balance),
            verdict("b", "check"),
            verdict("c", "check", questions=1, outcome_column="near"),
        ],
    )
    assert asked.code == COMPARE_CODE and asked.goal_id == "c"
    assert asked.text.startswith("This file also fits other goals")
    assert not asked.blocking and asked.recommended == KEEP_OPTION
    assert [o.label for o in asked.options] == [
        "A",
        "B",
        "Keep C",
    ], "no extra 'I don't know': Keep is the safe answer"
    first, second, keep = asked.options
    assert "control group (“No E-Mail”)" in first.effect and "outcome column “bought”" in first.effect
    assert "Switches this check to A" in first.effect and "control group" not in second.effect
    assert keep.safe and keep.option_id == KEEP_OPTION


def test_the_options_carry_the_goal_switch_and_keep_carries_none() -> None:
    found = compare_goals([verdict("a"), verdict("c", "check")], "c")
    assert found is not None
    ask = comparison_ask(found, upload_ids=["u1"])
    assert [dict(o.effect.payload) for o in ask.options] == [{SWITCH_GOAL: "a"}, {STAY_GOAL: "c"}]
    assert all(o.effect.kind is None for o in ask.options), "choosing adds no step to the plan"


def test_a_chosen_goal_that_does_not_fit_is_asked_without_a_recommendation_and_keep_says_what_is_missing() -> (
    None
):
    asked = question("c", [verdict("a"), verdict("c", "missing", summary="Needs the outcome column.")])
    assert not asked.blocking, "it never holds the Doctor's own work; Approve holds on the fit"
    assert asked.recommended is None
    assert asked.text.startswith("This file does not fit C yet")
    keep = asked.options[-1]
    assert keep.label == "Keep C" and "Needs the outcome column." in keep.effect


def test_the_question_is_the_same_for_the_same_files_and_goals() -> None:
    verdicts = [verdict("a"), verdict("c", "check")]
    found = compare_goals(verdicts, "c")
    assert found is not None
    one = settle(comparison_ask(found, upload_ids=["u1", "u2"]), now=NOW).question
    same = settle(comparison_ask(found, upload_ids=["u2", "u1"]), now=NOW).question
    other = settle(comparison_ask(found, upload_ids=["u1", "u3"]), now=NOW).question
    assert one is not None and same is not None and other is not None
    assert one.question_id == same.question_id != other.question_id


def test_the_facts_hold_the_comparison_for_the_ai_and_the_question_for_the_author() -> None:
    verdicts = [verdict("a", "ready", balance={"control_group": "None"}), verdict("c", "check")]
    fit = Facts(facts={"use_cases": verdicts})
    brief: Any = type("Brief", (), {"goal_id": "c"})()
    case: Any = type("OneFile", (), {"upload_ids": lambda self: ("u1",)})()
    facts = goal_compare_facts(case, brief, fit)
    seen: Mapping[str, Any] = facts.facts["goal_comparison"]
    assert seen["chosen"]["goal_id"] == "c" and seen["chosen"]["fits"] is True
    assert [g["goal_id"] for g in seen["fits_better"]] == ["a"]
    (suggested,) = facts.suggested
    assert isinstance(suggested.action, AskPerson) and suggested.action.ask.code == COMPARE_CODE
    best = goal_compare_facts(case, type("Brief", (), {"goal_id": "a"})(), fit)
    assert best.facts == {"goal_comparison": None} and not best.suggested
    assert comparison_facts(None) is None
