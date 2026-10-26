"""A query is tried on the file after the plan's fixes, and a plan that moved on since says so."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.config import AgentLevel
from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind
from engine.agent.recipe import run_recipe
from engine.agent.sql_step import sql_params
from engine.doctor.case import CaseFile, CaseItem
from engine.doctor.diagnose import LoadedFile
from engine.doctor.plan_check import check_plan, steps_digest
from engine.doctor.prescription import _off_own_table
from engine.doctor.registry import DataToolContext, data_registry

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
LEVELS = (AgentLevel.CLEAN, AgentLevel.DERIVE, AgentLevel.RESHAPE)
AS_TEXT = (
    "SELECT Customer, date_trunc('month', strptime(InvoiceDate, '%m/%d/%Y %H:%M')) AS month, count(*) AS n "
    "FROM t_log GROUP BY ALL"
)
AS_TIME = "SELECT Customer, date_trunc('month', InvoiceDate) AS month, count(*) AS n FROM t_log GROUP BY ALL"


def log(rows: int = 40) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Customer": [f"C{n % 8}" for n in range(rows)],
            "InvoiceDate": [f"{1 + n % 12}/{1 + n % 27}/2025 {n % 24}:30" for n in range(rows)],
        }
    )


def loaded(frame: pd.DataFrame) -> LoadedFile:
    return LoadedFile(upload_id="up-1", file_name="log.csv", frame=frame, profile=cast(Any, None))


def parse_date(state: ProposalState) -> CaseItem:
    step = RecipeStep(
        order=1, kind=RecipeStepKind.PARSE_DATE, column="InvoiceDate", params={"dayfirst": False}, reason="d"
    )
    return item("item_parse", {"step": step.model_dump(mode="json")}, state, upload_id="up-1")


def item(item_id: str, payload: dict[str, Any], state: ProposalState, *, upload_id: str | None) -> CaseItem:
    return CaseItem.model_validate(
        {
            "item_id": item_id,
            "kind": "recipe_step",
            "source": "rules",
            "confidence": "sure",
            "state": state.value,
            "upload_id": upload_id,
            "payload": payload,
            "payload_digest": "x",
            "proposed_at": NOW,
        }
    )


def propose(sql: str, items: tuple[CaseItem, ...], frame: pd.DataFrame | None = None) -> dict[str, Any]:
    ctx = DataToolContext(
        files=(loaded(log() if frame is None else frame),),
        caller="ai",
        principal=Principal(user_id="u", username="u", roles=frozenset({Role.ANALYST}), kind="user"),
        case_items=items,
    )
    given = {"description": "Count by month.", "sql": sql, "output_name": "monthly", "upload_ids": ["up-1"]}
    return dict(data_registry().call("propose_sql_step", given, ctx).output)


def test_a_fix_still_waiting_for_its_yes_counts_in_the_trial() -> None:
    waiting = parse_date(ProposalState.PENDING)
    proposal = propose(AS_TIME, (waiting,))
    sql = RecipeStep.model_validate(proposal["step"]).model_copy(update={"order": 2})
    parse = RecipeStep.model_validate(parse_date(ProposalState.ACCEPTED).payload["step"])
    assert sql_params(sql.params).tried_after == steps_digest([parse])
    # The person then accepts it: the run feeds the query the same parsed dates it was tried on.
    out = run_recipe(
        log(),
        [parse, sql],
        upload_id="up-1",
        primary_key=None,
        target=None,
        levels=LEVELS,
        max_failure_pct=5.0,
    )
    assert list(out.frame.columns) == ["Customer", "month", "n"]


def test_a_fix_accepted_after_the_query_was_tried_is_named_not_a_binder_error() -> None:
    proposal = propose(AS_TEXT, ())  # no fix yet: the dates are text
    step = {**proposal["step"], "order": 2}
    accepted_query = item(
        "item_sql", {**proposal, "step": step, "upload_id": None}, ProposalState.ACCEPTED, upload_id=None
    )
    files = [loaded(log())]
    case_files = [CaseFile(upload_id="up-1", file_name="log.csv", added_at=NOW, added_by="u")]
    now_parsed = parse_date(ProposalState.ACCEPTED)
    found = check_plan(files, case_files, [now_parsed, accepted_query]).findings
    assert [f.code for f in found] == ["PLAN_QUERY_TRIED_BEFORE_CHANGE"]
    assert found[0].item_id == "item_sql"
    # Unchanged plan: the same query is fine.
    unchanged = {f.code for f in check_plan(files, case_files, [accepted_query]).findings}
    assert not unchanged & {"PLAN_QUERY_TRIED_BEFORE_CHANGE", "PLAN_STEP_CANNOT_RUN"}


def test_the_inputs_report_the_whole_file_not_the_tried_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("engine.doctor.tools_sql.DRY_RUN_ROWS", 10)
    proposal = propose(AS_TIME, (parse_date(ProposalState.ACCEPTED),), log(60))
    (table,) = proposal["inputs"]
    assert table["rows"] == 60
    assert "(60 rows)" in proposal["sentence"]


def test_a_query_in_the_post_build_recipe_reads_the_prepared_table_by_name() -> None:
    step = RecipeStep.model_validate(propose(AS_TEXT, ())["step"])
    assert step.params["this_table"] == "log"
    plain = _off_own_table(step)
    assert "this_table" not in plain.params and "tried_after" not in plain.params
    assert plain.params["inputs"] == ["log"]


# ---------------------------------------------------------------------------
# The trial never applies another waiting query, and a window past the data is named
# ---------------------------------------------------------------------------
PER_CUSTOMER = "SELECT Customer, count(*) AS n FROM t_log GROUP BY ALL"
ON_LINES = "SELECT Customer, max(InvoiceDate) AS last_seen FROM t_log GROUP BY ALL"


def pending_table(state: ProposalState, item_id: str = "item_c4") -> CaseItem:
    """The AI's own earlier customer table: one row per customer, no InvoiceDate."""
    step = RecipeStep.model_validate(propose(PER_CUSTOMER, ())["step"]).model_copy(update={"order": 2})
    return item(item_id, {"step": step.model_dump(mode="json")}, state, upload_id=None)


@pytest.mark.parametrize("state", [ProposalState.PENDING, ProposalState.ACCEPTED])
def test_a_replacement_query_is_tried_on_the_lines_not_on_the_table_it_replaces(state: ProposalState) -> None:
    earlier = pending_table(state)
    parse = parse_date(ProposalState.PENDING)
    ctx = DataToolContext(
        files=(loaded(log()),),
        caller="ai",
        principal=Principal(user_id="u", username="u", roles=frozenset({Role.ANALYST}), kind="user"),
        case_items=(parse, earlier),
        replacing=("item_c4",),
    )
    given = {"description": "Last visit.", "sql": ON_LINES, "output_name": "last", "upload_ids": ["up-1"]}
    proposal = dict(data_registry().call("propose_sql_step", given, ctx).output)
    assert proposal["rows_out"] == 8
    # The pending parse_date was applied (InvoiceDate is a time, as the query needs), the table was not.
    step = RecipeStep.model_validate(proposal["step"])
    parsed = RecipeStep.model_validate(parse_date(ProposalState.ACCEPTED).payload["step"])
    assert sql_params(step.params).tried_after == steps_digest([parsed])


def test_another_waiting_query_is_not_applied_even_when_nothing_is_replaced() -> None:
    proposal = propose(ON_LINES, (parse_date(ProposalState.PENDING), pending_table(ProposalState.PENDING)))
    assert proposal["rows_out"] == 8


def ending(days_before_end: int, window: int = 90) -> dict[str, Any]:
    days = pd.date_range("2025-01-01", "2025-12-09", freq="D")
    frame = pd.DataFrame({"Customer": [f"C{n % 5}" for n in range(len(days))], "InvoiceDate": days})
    sql = "SELECT Customer, count(*) FILTER (WHERE InvoiceDate > $as_of) AS again FROM t_log GROUP BY ALL"
    as_of = (pd.Timestamp("2025-12-09") - pd.Timedelta(days=days_before_end)).date()
    ctx = DataToolContext(
        files=(loaded(frame),),
        caller="ai",
        principal=Principal(user_id="u", username="u", roles=frozenset({Role.ANALYST}), kind="user"),
    )
    given = {
        "description": "Ordered again.",
        "sql": sql,
        "output_name": "outcome",
        "upload_ids": ["up-1"],
        "as_of": as_of.isoformat(),
        "reads_future": True,
        "outcome_window_days": window,
    }
    return dict(data_registry().call("propose_sql_step", given, ctx).output)


def test_an_outcome_window_past_the_end_of_the_data_is_named() -> None:
    late = ending(9)
    (found,) = [f for f in late["findings"] if f["code"] == "OUTCOME_WINDOW_PAST_DATA"]
    assert found["level"] == "stop" and late["needs_yes"]
    assert "90 days past 2025-11-30" in found["message"] and "ends on 2025-12-09" in found["message"]
    assert "for example 2025-09-10" in found["message"]
    assert sql_params(late["step"]["params"]).outcome_window_days == 90


def test_an_outcome_window_inside_the_data_is_not() -> None:
    assert not [f for f in ending(100)["findings"] if f["code"] == "OUTCOME_WINDOW_PAST_DATA"]


def test_a_query_reading_the_future_states_its_window() -> None:
    from engine.doctor.registry import DataToolError

    with pytest.raises(DataToolError) as caught:
        data_registry().call(
            "propose_sql_step",
            {
                "description": "d",
                "sql": PER_CUSTOMER,
                "output_name": "o",
                "upload_ids": ["up-1"],
                "reads_future": True,
            },
            DataToolContext(
                files=(loaded(log()),),
                caller="ai",
                principal=Principal(user_id="u", username="u", roles=frozenset({Role.ANALYST}), kind="user"),
            ),
        )
    assert "outcome_window_days" in caught.value.message
