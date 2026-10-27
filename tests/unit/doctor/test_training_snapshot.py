"""Two dates: the training snapshot (where the outcome is cut) and the scoring date (the case's as-of)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from engine.access.roles import Principal, Role
from engine.agent.config import AgentLevel
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


# ---------------------------------------------------------------------------
# One training date, visible differences, rule steps, twins, every caller
# ---------------------------------------------------------------------------
FEATURE_SQL = (
    "SELECT Customer, count(*) FILTER (WHERE InvoiceDate <= $as_of) AS before FROM t_feat GROUP BY ALL"
)


def feature(**params: Any) -> RecipeStep:
    return query(
        sql=FEATURE_SQL,
        inputs=["feat"],
        output_name="features",
        reads_future=False,
        outcome_window_days=None,
        **params,
    ).model_copy(update={"order": 1, "column": "features"})


def plan_of(*steps: RecipeStep) -> list[Any]:
    from engine.agent.contracts import ProposalState

    return [
        item(
            f"item_{s.order}",
            {"step": s.model_dump(mode="json")},
            ProposalState.ACCEPTED,
            upload_id="up-2" if s.column == "features" else "up-1",
        )
        for s in steps
    ]


def both_files() -> tuple[Any, ...]:

    other = loaded(log())
    return (loaded(log()), other.__class__(upload_id="up-2", file_name="feat.csv", frame=log(), profile=None))  # type: ignore[arg-type]


CASE_FILES = [
    CaseFile(upload_id=u, file_name=n, added_at=datetime(2026, 1, 1, tzinfo=UTC), added_by="u")
    for u, n in (("up-1", "log.csv"), ("up-2", "feat.csv"))
]


def test_a_feature_step_without_a_date_reads_the_outcomes_date_not_the_scoring_date() -> None:
    from engine.doctor.materialize import Full

    outcome = query(as_of=TRAINING.isoformat())
    run = materialize_plan(CASE_FILES, plan_of(outcome, feature()), both_files(), Full(), as_of=SCORING)
    assert not run.failures
    # measured on rows up to 31 Aug, never on the Sep-Nov rows inside the outcome's window
    assert int(run.tables["features"]["before"].sum()) == (TRAINING - date(2011, 1, 1)).days + 1
    codes = {f.code: f for f in run.findings}
    assert "STEP_DATE_DIFFERS" in codes  # the person is told the plan reads 31 Aug, not 30 Nov
    assert codes["STEP_DATE_DIFFERS"].level == "stop" and not codes["STEP_DATE_DIFFERS"].hard


def test_no_difference_is_reported_when_the_step_reads_the_decided_date() -> None:
    from engine.doctor.materialize import Full

    run = materialize_plan(
        CASE_FILES, plan_of(query(as_of=SCORING.isoformat())), (loaded(log()),), Full(), as_of=SCORING
    )
    assert "STEP_DATE_DIFFERS" not in {f.code for f in run.findings}


def test_a_rule_step_is_tried_and_run_at_the_decided_date() -> None:
    from engine.doctor.materialize import tried_step

    ruled = query(as_of="2011-06-30", as_of_rule={"rule": "latest_date_in_file", "table": "t", "column": "c"})
    tried = tried_step(ruled, SCORING, None)
    assert tried.params["as_of"] == SCORING.isoformat() and "as_of_rule" not in tried.params
    assert _bound_as_of(ruled, SCORING.isoformat()).params["as_of"] == tried.params["as_of"]


def test_the_same_query_at_another_date_is_not_a_twin() -> None:
    from engine.agent.contracts import ProposalState
    from engine.doctor.agent_apply import meaning_key

    def keyed(**params: Any) -> str:
        step = query(**params)
        return meaning_key(
            item("i", {"step": step.model_dump(mode="json")}, ProposalState.ACCEPTED, upload_id="up-1")
        )

    assert keyed(as_of="2011-08-31") != keyed(as_of="2011-11-30")
    assert keyed(as_of="2011-08-31") != keyed()
    rule = {"rule": "latest_date_in_file", "table": "t", "column": "c"}
    assert keyed(as_of="2011-08-31", as_of_rule=rule) == keyed(as_of="2011-09-30", as_of_rule=rule)


def test_the_outcome_is_not_all_zero_in_the_trial_the_prescription_and_the_replay() -> None:
    from engine.doctor.materialize import Full, snapshot_of
    from engine.doctor.prescription_replay import run_part_recipe
    from tests.unit.doctor import prescription_support as ps

    outcome = query(as_of=TRAINING.isoformat())
    # (a) the trial: the query tried as one more step on the plan
    trial = materialize_plan(
        CASE_FILES, [], (loaded(log()),), Full(), as_of=SCORING, candidate=("up-1", outcome)
    )
    assert int(trial.tables["outcome"]["again"].sum()) > 0
    # (b) the prescription binds the very steps the plan ran: one date for all of them
    steps = [outcome, feature()]
    snapshot = snapshot_of(steps)
    assert snapshot == TRAINING
    bound = [_bound_as_of(s, SCORING.isoformat(), snapshot.isoformat()) for s in steps]
    assert [s.params["as_of"] for s in bound] == [TRAINING.isoformat()] * 2
    # (c) the recipe replay (what VERIFY and a training run execute)
    recipe = ps.recipe((bound[0],), primary_key="Customer", target=None, levels=tuple(AgentLevel))
    ran = run_part_recipe(
        recipe, log(), config=None, upload_id="up-1", leak_check=None, tables={"log": log()}
    )
    assert int(ran.frame["again"].sum()) > 0
