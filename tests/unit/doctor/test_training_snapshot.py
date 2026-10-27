"""Two dates: the training snapshot (where the outcome is cut) and the scoring date (the case's as-of)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from engine.access.roles import Principal, Role
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.doctor.case import CaseFile
from engine.doctor.materialize import materialize_plan
from engine.doctor.prescription import _bound_as_of
from engine.doctor.registry import DataToolContext, data_registry
from tests.unit.doctor.test_sql_trial_state import item, loaded

SQL = (
    "SELECT Customer, count(*) FILTER (WHERE InvoiceDate > $as_of "
    "AND InvoiceDate <= $as_of + INTERVAL 90 DAY) AS again FROM t_log GROUP BY ALL"
)
SCORING = date(2011, 11, 30)  # the case's as-of; the data ends the day before
TRAINING = date(2011, 8, 31)


def log() -> pd.DataFrame:
    days = pd.date_range("2011-01-01", "2011-11-29", freq="D")
    return pd.DataFrame({"Customer": [f"C{n % 5}" for n in range(len(days))], "InvoiceDate": days})


def query(**params: Any) -> RecipeStep:
    return RecipeStep(
        order=1,
        kind=RecipeStepKind.SQL_QUERY,
        column="outcome",
        params={
            "sql": SQL,
            "inputs": ["log"],
            "output_name": "outcome",
            "description": "d",
            "reads_future": True,
            "outcome_window_days": 90,
            **params,
        },
        reason="r",
    )


def again(step: RecipeStep, *, training: date | None = None) -> int:
    files = (loaded(log()),)
    from engine.agent.contracts import ProposalState

    plan = item("item_q", {"step": step.model_dump(mode="json")}, ProposalState.ACCEPTED, upload_id="up-1")
    case_files = [
        CaseFile(
            upload_id="up-1", file_name="log.csv", added_at=datetime(2026, 1, 1, tzinfo=UTC), added_by="u"
        )
    ]
    from engine.doctor.materialize import Full

    run = materialize_plan(case_files, [plan], files, Full(), as_of=SCORING, training_as_of=training)
    assert not run.failures
    return int(run.tables["outcome"]["again"].sum())


def test_a_step_with_its_own_date_keeps_it_when_the_case_date_is_later() -> None:
    assert again(query(as_of=TRAINING.isoformat())) > 0


def test_the_prescription_does_not_overwrite_a_steps_own_date_but_binds_one_without() -> None:
    own = query(as_of=TRAINING.isoformat())
    assert _bound_as_of(own, SCORING.isoformat()) is own
    bound = _bound_as_of(query(), SCORING.isoformat())
    assert bound.params["as_of"] == SCORING.isoformat()
    cut = _bound_as_of(query(), SCORING.isoformat(), TRAINING.isoformat())
    assert cut.params["as_of"] == TRAINING.isoformat()  # an outcome query is cut at the training snapshot


def test_a_step_without_a_date_reads_the_case_date_or_the_training_snapshot() -> None:
    assert again(query()) == 0  # scoring date: no later rows
    assert again(query(), training=TRAINING) > 0


def test_the_trial_passes_the_window_check_for_the_august_step_and_names_the_date() -> None:
    ctx = DataToolContext(
        files=(loaded(log()),),
        caller="ai",
        principal=Principal(user_id="u", username="u", roles=frozenset({Role.ANALYST}), kind="user"),
        as_of=SCORING,
    )
    given = {
        "description": "Ordered again.",
        "sql": SQL,
        "output_name": "outcome",
        "upload_ids": ["up-1"],
        "as_of": TRAINING.isoformat(),
        "reads_future": True,
        "outcome_window_days": 90,
    }
    out = dict(data_registry().call("propose_sql_step", given, ctx).output)
    assert "OUTCOME_WINDOW_PAST_DATA" not in {f["code"] for f in out["findings"]}
    assert "2011-08-31, not 2011-11-30" in out["sentence"]
    given.pop("as_of")
    out = dict(data_registry().call("propose_sql_step", given, ctx).output)
    assert "OUTCOME_WINDOW_PAST_DATA" in {f["code"] for f in out["findings"]}  # the case date does not fit
    ctx2 = ctx.__class__(**{**ctx.__dict__, "training_as_of": TRAINING})
    out = dict(data_registry().call("propose_sql_step", given, ctx2).output)
    assert "OUTCOME_WINDOW_PAST_DATA" not in {f["code"] for f in out["findings"]}
