"""Editing by hand (Plan L M154, DEC-1540): an edit is an accepted step of the person's, undone by unticking."""

from __future__ import annotations

from typing import Any

import pytest

from engine.agent.advisor import recipe_steps
from engine.agent.contracts import ProposalSource, ProposalState, RecipeStepKind
from engine.agent.edits import add_edit, table_rows
from engine.agent.recipe import run_recipe
from engine.agent.session import SessionError, accepted, decide, start_session
from tests.unit.agent.helpers import context_for, synthetic


@pytest.fixture
def started() -> tuple[Any, Any]:
    frame = synthetic(rows=1500)
    frame["city"] = ["Bombay" if i % 4 == 0 else "Pune" for i in range(len(frame))]
    frame["note"] = ["TEST" if i % 100 == 0 else "live" for i in range(len(frame))]
    ctx = context_for(frame)
    return ctx, start_session(ctx, session_id="s1")


def mine(session: Any) -> list[Any]:
    return [p for p in session.proposals if p.source is ProposalSource.PERSON]


def test_an_edit_is_an_accepted_step_of_the_persons(started: tuple[Any, Any]) -> None:
    ctx, session = started
    after = add_edit(
        session, ctx, kind=RecipeStepKind.MAP_VALUES, column="city", params={"mapping": {"Bombay": "Mumbai"}}
    )
    (edit,) = mine(after)
    assert edit.state is ProposalState.ACCEPTED and edit.step is not None
    assert edit.title == "Your spellings in 'city'"
    assert "changes 250 values" in edit.reason
    rejected = decide(after, ctx, [(edit.proposal_id, ProposalState.REJECTED, None)])
    assert edit.step not in [p.step for p in accepted(rejected.proposals)]


def test_corrections_on_one_column_stay_one_step(started: tuple[Any, Any]) -> None:
    ctx, session = started
    ids = list(ctx.frame["customer_id"].astype(str)[:3])
    for key in ids:
        session = add_edit(
            session,
            ctx,
            kind=RecipeStepKind.CORRECT_CELLS,
            column="city",
            params={
                "key_column": "customer_id",
                "cells": [
                    {
                        "key": key,
                        "old": str(
                            ctx.frame.loc[ctx.frame["customer_id"].astype(str) == key, "city"].iloc[0]
                        ),
                        "new": "Agra",
                    }
                ],
            },
        )
    (edit,) = mine(session)
    assert edit.step is not None and len(edit.step.params["cells"]) == 3
    steps = recipe_steps(accepted(session.proposals))
    done = run_recipe(
        ctx.frame,
        steps,
        upload_id="u",
        primary_key="customer_id",
        target="converted_30d",
        levels=ctx.config.agent.levels,
        max_failure_pct=50,
    )
    assert list(done.frame["city"][:3]) == ["Agra", "Agra", "Agra"]


def test_leaving_rows_out_needs_the_derive_level_and_counts_them(started: tuple[Any, Any]) -> None:
    ctx, session = started
    after = add_edit(
        session,
        ctx,
        kind=RecipeStepKind.REMOVE_ROWS,
        column="note",
        params={"operator": "equals", "values": ["TEST"]},
    )
    (edit,) = mine(after)
    assert "leave these rows out: 10 of the first 1000" in edit.reason


@pytest.mark.parametrize(
    ("kind", "column", "params", "code"),
    [
        (
            RecipeStepKind.REMOVE_ROWS,
            "converted_30d",
            {"operator": "equals", "values": ["1"]},
            "RECIPE_STEP_INVALID",
        ),
        (RecipeStepKind.FILL_MISSING, "city", {"value": "mean"}, "RECIPE_STEP_INVALID"),
        (RecipeStepKind.PARSE_NUMBER, "city", {}, "AGENT_EDIT_NOT_ALLOWED"),
        (RecipeStepKind.MAP_VALUES, "nope", {"mapping": {"a": "b"}}, "AGENT_COLUMN_UNKNOWN"),
    ],
)
def test_edits_the_recipe_engine_refuses_are_refused(
    started: tuple[Any, Any], kind: RecipeStepKind, column: str, params: dict[str, Any], code: str
) -> None:
    ctx, session = started
    with pytest.raises(SessionError) as caught:
        add_edit(session, ctx, kind=kind, column=column, params=params)
    assert caught.value.code == code


def test_the_table_view_shows_rows_with_personal_data_hidden(started: tuple[Any, Any]) -> None:
    ctx, _ = started
    page = table_rows(ctx, offset=0, limit=5)
    assert page["total"] == 1500 and len(page["rows"]) == 5
    assert page["row_numbers"] == [2, 3, 4, 5, 6]
    found = table_rows(ctx, where_column="note", equals="TEST", limit=100)
    assert found["total"] == 15
