"""Decisions O10 (an AI step's change share) and O3 (the reshape level with guards), M238, DEC-1808 (f)(g)."""

from __future__ import annotations

import pandas as pd
import pytest

from engine.agent.config import AgentConfig, AgentLevel
from engine.agent.contracts import ProposalSource, RecipeStep, RecipeStepKind, StepReceipt
from engine.doctor.change_share import ChangeShareLimits, change_share_verdict, measure_step
from engine.doctor.reshape_guard import (
    ReshapeGuardError,
    ReshapeItem,
    check_reshape_guards,
    doctor_levels,
    reshape_guard_failures,
    reshape_question,
)


@pytest.mark.parametrize(
    ("changed", "level", "code"),
    [(30, "ok", None), (31, "warn", "AI_STEP_CHANGES_MANY"), (60, "warn", "AI_STEP_CHANGES_MANY"),
     (61, "refuse", "AI_STEP_CHANGES_TOO_MANY")],
)  # fmt: skip
def test_an_ai_step_warns_above_30_and_is_refused_above_60_percent(
    changed: int, level: str, code: str | None
) -> None:
    verdict = change_share_verdict(changed=changed, out_of=100, source=ProposalSource.AI)
    assert (verdict.level, verdict.code) == (level, code)


@pytest.mark.parametrize("source", [ProposalSource.RULES, ProposalSource.PERSON])
def test_the_rules_and_the_persons_own_edit_are_not_held_to_it(source: ProposalSource) -> None:
    assert change_share_verdict(changed=99, out_of=100, source=source).level == "ok"


def test_limits_are_passed_in_and_checked() -> None:
    assert (
        change_share_verdict(
            changed=20, out_of=100, source=ProposalSource.AI, limits=ChangeShareLimits(10, 50)
        ).level
        == "warn"
    )
    with pytest.raises(ValueError):
        ChangeShareLimits(70, 60)


def test_measure_step_runs_the_step_on_a_copy() -> None:
    frame = pd.DataFrame(
        {"id": [1, 2, 3, 4], "city": ["Bombay", "Mumbai", None, "Bombay"], "y": [0, 1, 0, 1]}
    )
    mapping = RecipeStep(
        order=3, kind=RecipeStepKind.MAP_VALUES, column="city", params={"mapping": {"Bombay": "Mumbai"}}
    )
    assert measure_step(frame, mapping, primary_key="id", target="y") == (2, 3)
    rows = RecipeStep(
        order=1, kind=RecipeStepKind.REMOVE_ROWS, column="city", params={"operator": "is_empty"}
    )
    assert measure_step(frame, rows, primary_key="id", target="y") == (1, 4)
    assert frame["city"].tolist() == ["Bombay", "Mumbai", None, "Bombay"]


# ---------------------------------------------------------------------------
# O3
# ---------------------------------------------------------------------------
def test_the_reshape_level_is_on_for_every_goal_only_with_data_doctor_on() -> None:
    default = AgentConfig().levels
    assert default == (AgentLevel.CLEAN, AgentLevel.DERIVE)  # the switch off changes nothing
    assert doctor_levels(default) == (AgentLevel.CLEAN, AgentLevel.DERIVE, AgentLevel.RESHAPE)
    assert doctor_levels((AgentLevel.CLEAN,)) == (AgentLevel.CLEAN, AgentLevel.RESHAPE)


def _item(**changes: object) -> ReshapeItem:
    step = RecipeStep(
        order=1, kind=RecipeStepKind.KEEP_LATEST_PER_ID, column="id", params={"time_column": "t"}
    )
    receipt = StepReceipt(
        order=1, kind=RecipeStepKind.KEEP_LATEST_PER_ID, column="id", rows=10, changed=4, failed=0
    )
    base: dict[str, object] = {
        "step": step, "ticked_when_offered": False, "question_answered": True, "leak_check": "full", "receipt": receipt,
    }  # fmt: skip
    return ReshapeItem(**{**base, **changes})  # type: ignore[arg-type]


def test_a_guarded_reshape_step_passes() -> None:
    assert reshape_guard_failures(_item()) == ()
    check_reshape_guards(_item())
    assert reshape_guard_failures(_item(training=False, leak_check="narrow")) == ()


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"question_answered": False}, "question"),
        ({"ticked_when_offered": True}, "ticked"),
        ({"leak_check": None}, "future-data check"),
        ({"receipt": None}, "preview"),
    ],
)
def test_each_missing_guard_refuses_approve(changes: dict[str, object], words: str) -> None:
    with pytest.raises(ReshapeGuardError) as refused:
        check_reshape_guards(_item(**changes))
    assert refused.value.code == "RESHAPE_GUARD_MISSING" and words in refused.value.message


def test_a_step_that_is_not_a_reshape_needs_no_guard() -> None:
    step = RecipeStep(order=1, kind=RecipeStepKind.MAP_VALUES, column="c", params={"mapping": {"a": "b"}})
    assert reshape_guard_failures(_item(step=step, question_answered=False, receipt=None)) == ()


def test_the_question_is_blocking_and_shows_the_preview() -> None:
    item = _item()
    assert item.receipt is not None
    question = reshape_question(item.step, receipt=item.receipt, rows_out=6)
    assert question.blocking and "10 rows; this leaves 6" in question.text
    assert [o.option_id for o in question.options] == ["yes", "no", "dont-know"]
    with pytest.raises(ValueError):
        reshape_question(
            RecipeStep(order=1, kind=RecipeStepKind.MAP_VALUES, column="c", params={"mapping": {"a": "b"}}),
            receipt=item.receipt,
            rows_out=6,
        )


def test_a_step_that_fills_empty_cells_is_measured_against_every_row_of_the_column() -> None:
    # Review fix: 4 of 10 cells are empty and the fill writes into all 4. That is 40% of the column (a warning),
    # not 4 of the 6 non-empty values (67%, which would refuse it); a fill can never score above 100%.
    frame = pd.DataFrame(
        {
            "id": range(10),
            "region": ["North", None, "South", None, "East", None, "West", None, "North", "South"],
            "y": [0, 1] * 5,
        }
    )
    fill = RecipeStep(order=1, kind=RecipeStepKind.FILL_MISSING, column="region", params={"value": "Unknown"})
    changed, out_of = measure_step(frame, fill, primary_key="id", target="y")
    assert (changed, out_of) == (4, 10)
    verdict = change_share_verdict(changed=changed, out_of=out_of, source=ProposalSource.AI)
    assert (verdict.level, verdict.code) == ("warn", "AI_STEP_CHANGES_MANY")  # 40%: shown, not refused
