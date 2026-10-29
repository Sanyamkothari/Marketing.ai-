"""`scripts/doctor_scenario_checks.py`: each invariant of the scenario suite catches what it names, and only that."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd

from engine.doctor.amount_outcome import ProposeAmountOutcomeArgs, compile_amount_outcome
from scripts.doctor_scenario_checks import (
    check_invariants,
    evaluate,
    plan_kind,
    same_plan_without_ai,
)

FEATURES = "f AS (SELECT k AS entity_key, COUNT(*) AS n FROM t WHERE d <= CAST($as_of AS DATE) GROUP BY k)"
OUTCOME = (
    "w AS (SELECT DISTINCT k AS entity_key FROM t WHERE d > CAST($as_of AS DATE) "
    "AND d <= CAST($as_of AS DATE) + INTERVAL 90 DAY)"
)
SQL = f"WITH {FEATURES}, {OUTCOME} SELECT f.*, w.entity_key IS NOT NULL AS yes FROM f LEFT JOIN w USING (entity_key)"


def item(
    item_id: str, state: str = "pending", *, title: str = "A change", by: str | None = None, **more: Any
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "kind": "recipe_step",
        "state": state,
        "title": title,
        "decided_by": by,
        **more,
    }


def case(*items: dict[str, Any], **more: Any) -> dict[str, Any]:
    return {"state": "approve", "items": list(items), "questions": [], "approvals": [], "chat": [], **more}


def sql_item(sql: str, window: int | None = 90, state: str = "accepted") -> dict[str, Any]:
    params = {"sql": sql, "outcome_window_days": window, "reads_future": True}
    return item("s1", state, payload={"step": {"kind": "sql_query", "column": "training", "params": params}})


def found(name: str, *args: Any, **kwargs: Any) -> list[str]:
    return check_invariants(*args, **kwargs)[name]


# -- 1: nothing is accepted without a person -------------------------------------------------------------------------


def test_an_item_the_rules_accepted_is_found_until_an_approval_records_it() -> None:
    rules = item("k1", "accepted", title="A key")
    assert found("accepted_needs_person", case(rules)) != []
    assert found("accepted_not_by_ai", case(rules)) == [], "the rules' part is the other check"
    approved = case(rules, approvals=[{"item_ids": ["k1"]}])
    assert found("accepted_needs_person", approved) == []


def test_an_item_an_ai_accepted_is_found_whatever_the_rules_do() -> None:
    by_ai = item("k1", "accepted", by="agent:data-doctor")
    assert found("accepted_not_by_ai", case(by_ai)) != []
    assert found("accepted_needs_person", case(by_ai)) == []
    assert found("accepted_not_by_ai", case(by_ai, approvals=[{"item_ids": ["k1"]}])) == []


def test_an_item_a_person_decided_or_that_waits_is_not_found() -> None:
    plan = case(item("k1", "accepted", by="local-operator"), item("k2"))
    assert found("accepted_needs_person", plan) == [] and found("accepted_not_by_ai", plan) == []


# -- 2: no leak -------------------------------------------------------------------------------------------------------


def test_a_step_may_read_past_the_snapshot_only_inside_its_outcome_window() -> None:
    assert found("no_leak", case(sql_item(SQL))) == []
    unbounded = SQL.replace(" + INTERVAL 90 DAY", "")
    assert "outside a bounded outcome window" in found("no_leak", case(sql_item(unbounded)))[0]
    assert (
        found("no_leak", case(sql_item(SQL, window=60))) != []
    ), "the window it says is not the one in the SQL"
    assert (
        found("no_leak", case(sql_item(unbounded, state="rejected"))) == []
    ), "a change left out reads nothing"


def test_a_feature_that_reads_past_the_snapshot_is_found_next_to_a_bounded_outcome() -> None:
    leaky = FEATURES.replace("d <= CAST($as_of AS DATE)", "d > CAST($as_of AS DATE)")
    sql = f"WITH {leaky}, {OUTCOME} SELECT f.*, w.entity_key IS NOT NULL AS yes FROM f LEFT JOIN w USING (entity_key)"
    assert "outside a bounded outcome window" in found("no_leak", case(sql_item(sql)))[0]
    window_in_feature = leaky.replace("GROUP BY", "AND d <= CAST($as_of AS DATE) + INTERVAL 90 DAY GROUP BY")
    sql = f"WITH {window_in_feature}, {OUTCOME} SELECT f.*, w.entity_key IS NOT NULL AS yes FROM f LEFT JOIN w USING (entity_key)"
    assert (
        "returns ['n']" in found("no_leak", case(sql_item(sql)))[0]
    ), "bounded, but not in the outcome's part"


def test_both_spellings_of_the_window_bound_and_a_look_back_are_accepted() -> None:
    amount = build_amount_sql(window_days=90)
    assert found("no_leak", case(sql_item(amount)), screen=outcome_screen("spend_next_90d")) == []
    assert found("no_leak", case(sql_item(amount, window=60)), screen=outcome_screen("spend_next_90d")) != []
    back = f"WITH r AS (SELECT k FROM t WHERE d > CAST($as_of AS DATE) - INTERVAL 90 DAY AND d <= CAST($as_of AS DATE)), {OUTCOME} SELECT * FROM r"
    assert found("no_leak", case(sql_item(back))) == [], "a look back from the snapshot reads no future"


def leaky_case(*extra: dict[str, Any]) -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 600)
    frame = pd.DataFrame({"id": range(600), "y": y, "noise": rng.normal(size=600), "gives_away": y * 3 + 1})
    outcome = item("o1", "accepted", payload={"upload_id": "u1", "column": "y"}, kind="outcome_column")
    return case(outcome, *extra, files=[{"upload_id": "u1", "file_name": "f.csv", "key_columns": ["id"]}]), {
        "f.csv": frame
    }


def test_a_column_the_leak_probe_flags_must_be_hidden_by_the_plan() -> None:
    plan, frames = leaky_case()
    assert "'gives_away' in f.csv gives away the outcome" in found("no_leak", plan, frames=frames)[0]
    hide = item("d1", "pending", payload={"step": {"kind": "drop_column", "column": "gives_away"}})
    plan, frames = leaky_case(hide)
    assert found("no_leak", plan, frames=frames) == []


def build_amount_sql(window_days: int) -> str:
    given = ProposeAmountOutcomeArgs(
        upload_id="u1",
        entity_key="id",
        time_column="d",
        amount_column="amount",
        window_days=window_days,
        returns="net",
    )
    return compile_amount_outcome(given, "t_orders")


def outcome_screen(name: str) -> dict[str, Any]:
    return {"view": {"start": {"outcome": name, "primary_key": "id"}}}


def built_case(sql: str) -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
    """An order log of 600 customers, half of whom order again after the snapshot, and a plan that builds its
    training table with `sql`."""
    ids = [f"c{i}" for i in range(600)]
    early = pd.DataFrame({"id": ids, "d": "2024-01-10"})
    later = pd.DataFrame({"id": ids[::2], "d": "2024-02-20"})
    orders = pd.concat([early, later], ignore_index=True)
    params = {
        "sql": sql,
        "inputs": ["orders"],
        "output_name": "training",
        "description": "A training table.",
        "as_of": "2024-02-01",
        "reads_future": True,
        "outcome_window_days": 90,
    }
    step = {"order": 1, "kind": "sql_query", "column": "training", "params": params}
    plan = item("s1", "accepted", payload={"step": step})
    return case(plan, files=[{"upload_id": "u1", "file_name": "orders.csv", "key_columns": ["id"]}]), {
        "orders.csv": orders
    }


BUILT = (
    "WITH f AS (SELECT id, COUNT(*) AS n FROM t_orders WHERE TRY_CAST(d AS DATE) <= CAST($as_of AS DATE) GROUP BY id), "
    "w AS (SELECT DISTINCT id AS entity_key FROM t_orders WHERE TRY_CAST(d AS DATE) > CAST($as_of AS DATE) "
    "AND TRY_CAST(d AS DATE) <= CAST($as_of AS DATE) + INTERVAL 90 DAY) "
    'SELECT f.*, CASE WHEN w.entity_key IS NULL THEN 0 ELSE 1 END AS "came_back" '
    "FROM f LEFT JOIN w ON f.id = w.entity_key ORDER BY f.id"
)


def test_an_outcome_the_plan_builds_is_probed_for_leaks_on_the_table_it_builds() -> None:
    screen = outcome_screen("came_back")
    plan, frames = built_case(BUILT)
    assert found("no_leak", plan, screen, frames=frames) == []
    # the answer handed over as a feature: one that is 1 for each customer who orders after the snapshot
    leaky = BUILT.replace(
        "COUNT(*) AS n FROM t_orders WHERE TRY_CAST(d AS DATE) <= CAST($as_of AS DATE) GROUP BY id",
        "COUNT(*) AS n, MAX(CASE WHEN TRY_CAST(d AS DATE) > CAST($as_of AS DATE) THEN 1 ELSE 0 END) AS later "
        "FROM t_orders GROUP BY id",
    )
    plan, frames = built_case(leaky)
    problems = found("no_leak", plan, screen, frames=frames)
    assert "'later' in the table 'training' the plan builds gives away the outcome" in " ".join(problems)
    hide = item("d1", "pending", payload={"step": {"kind": "drop_column", "column": "later"}})
    assert any("'n' in" in p for p in problems), "the count of all rows gives the answer away too"
    hide_n = item("d2", "pending", payload={"step": {"kind": "drop_column", "column": "n"}})
    plan = {**plan, "items": [*plan["items"], hide_n]}
    assert not any(
        "gives away" in p
        for p in found("no_leak", {**plan, "items": [*plan["items"], hide]}, screen, frames=frames)
    )


# -- 3: the screen shows what the AI saw ------------------------------------------------------------------------------


def screen(**view: Any) -> dict[str, Any]:
    return {"view": view}


def test_a_count_from_a_sample_is_marked_as_one() -> None:
    sampled = {"findings": [{"code": "PLAN_LOSES_TOO_MUCH", "scope": "sample", "text": "Leaves out 157."}]}
    assert found("screen_matches_ai", case(), screen(ledger=sampled)) != []
    assert found("screen_matches_ai", case(), screen(ledger={**sampled, "sampled_from": 5000})) == []
    row = {"review": {"plan": [{"counts": "12,008 rows"}]}}
    assert found("screen_matches_ai", case(), screen(**row)) != []
    marked = {
        "review": {"plan": [{"counts": "Whole file: 12,008 rows"}, {"counts": "Sample: the first 1,000"}]}
    }
    assert found("screen_matches_ai", case(), screen(**marked)) == []


def test_the_rows_of_a_file_are_the_rows_the_ai_was_shown() -> None:
    shown = screen(preview={"files": [{"file_name": "a.csv", "total_rows": 12008}]})
    prompt = 'THE CASE NOW: {"files": [{"file": 1, "file_name": "a.csv", "rows": 11714, "columns_total": 8}]}'
    assert (
        "11,714 rows of a.csv and the screen shows 12,008"
        in found("screen_matches_ai", case(), shown, prompts=[prompt])[0]
    )
    assert found("screen_matches_ai", case(), shown, prompts=[prompt.replace("11714", "12008")]) == []


# -- 4: a refusal says how to fix it ----------------------------------------------------------------------------------


def test_a_refusal_the_ai_received_names_how_to_fix_it() -> None:
    old = json.dumps([{"error": "AGENT_ITEM_UNKNOWN", "message": "No such change."}])
    assert (
        "AGENT_ITEM_UNKNOWN reached the AI without how_to_fix"
        in found("refusals_explain", case(), prompts=[old])[0]
    )
    new = {"code": "X", "what_failed": "a", "value": 1, "how_to_fix": ""}
    assert found("refusals_explain", case(), prompts=[json.dumps({"results": [new]})]) != []
    fixed = {**new, "how_to_fix": "Change column c."}
    assert found("refusals_explain", case(), prompts=[json.dumps({"results": [fixed]})]) == []


def test_a_finding_with_a_code_is_not_a_refusal() -> None:
    state = json.dumps({"findings": [{"code": "PLAN_LOSES_TOO_MUCH", "level": "stop"}]})
    assert found("refusals_explain", case(), prompts=[state]) == []


# -- 5: nothing a person says is ignored ------------------------------------------------------------------------------


def said(text: str, *items: dict[str, Any]) -> dict[str, Any]:
    return case(*items, chat=[{"role": "agent", "text": text}])


def test_an_instruction_ends_changed_or_answered_and_a_refusal_says_so() -> None:
    before = case()
    ask = {"message": "Use 60 days.", "expects": "change"}

    def run(after: dict[str, Any]) -> list[str]:
        return found("no_silent_ignore", after, previous=before, instruction=ask)

    assert "got no new reply" in run(case())[0]
    assert run(said("I cannot change that, because the person decided it.")) == []
    assert run(said("Looking into it.")) != [], "nothing changed and nothing was refused"
    assert run(said("Done.", item("n1"))) == []
    assert run(said("I have removed the step.")) != [], "it claims a change the plan does not show"


def test_a_question_only_needs_a_reply() -> None:
    ask = {"message": "What is this good for?", "expects": "answer"}
    assert found("no_silent_ignore", said("Win-back."), previous=case(), instruction=ask) == []
    stale = said("Win-back.")
    assert (
        found("no_silent_ignore", stale, previous=stale, instruction=ask) != []
    ), "an old reply is not a new one"


# -- 6 and 7 ----------------------------------------------------------------------------------------------------------


def test_a_size_stop_that_reaches_the_person_is_found() -> None:
    stopped = case(visit={"stopped": {"code": "TOOL_OUTPUT_TOO_LARGE", "message": "ask a narrower question"}})
    assert len(found("no_size_stop", stopped)) == 2
    assert found("no_size_stop", case()) == []


def test_without_an_ai_the_default_author_reaches_the_same_kind_of_plan() -> None:
    a, b = case(item("k", "accepted")), case(item("k", "pending"))
    assert same_plan_without_ai(plan_kind(a), plan_kind(a)) == []
    assert same_plan_without_ai(plan_kind(a), plan_kind(b)) != []
    assert same_plan_without_ai(plan_kind(a), plan_kind(case())) != []


# -- the expectations -------------------------------------------------------------------------------------------------


def test_a_change_of_the_persons_stands_until_they_say_yes_to_its_swap() -> None:
    date_fix = item("d1", "accepted", title="Use 2024-01-31 as the as-of date", by="local-operator")
    swap = item("d2", "pending", title="Take back the as-of date", replaces="d1")
    rule = {"swap_pending_for": [{"title_has": "as-of date"}]}
    assert evaluate(rule, case(date_fix, swap), None) == []
    assert evaluate(rule, case(date_fix), None) != []
    assert (
        evaluate(rule, case({**date_fix, "state": "rejected"}, swap), None) != []
    ), "the AI took back the person's own"
    taken = {"taken_back_or_swapped": [{"title_has": "as-of date"}]}
    assert evaluate(taken, case(date_fix, swap), None) == []
    assert evaluate(taken, case({**date_fix, "state": "rejected"}), None) == []
    assert evaluate(taken, case(date_fix), None) != []


def test_a_change_is_followed_by_the_title_it_had_at_an_earlier_step() -> None:
    before = case(item("d1", "accepted", title="Use 2024-01-31 as the as-of date"))
    now = case(
        item("d1", "rejected", title="Use 2024-01-31 as the as-of date"),
        item("d9", "accepted", title="as-of date"),
    )
    rule = {"item_state": [{"title_has": "as-of date", "same_as_at": "setup", "state": ["rejected"]}]}
    assert evaluate(rule, now, None, earlier={"setup": before}) == []
    assert (
        evaluate(rule, now, None, earlier={"setup": case(item("d9", "accepted", title="as-of date"))}) != []
    )


def test_the_training_table_looks_as_far_ahead_as_the_person_said() -> None:
    assert evaluate({"window_days": 90}, case(sql_item(SQL)), None) == []
    assert evaluate({"window_days": 60}, case(sql_item(SQL)), None) != []
    assert evaluate({"window_days": 90}, case(), None) != []


def test_a_file_that_is_its_own_training_table_is_counted_from_the_screen_and_the_source() -> None:
    frame = pd.DataFrame({"churn": ["Yes"] * 250 + ["No"] * 750})
    view = {"preview": {"files": [{"file_name": "f.csv", "total_rows": 1000}]}}
    rule = {
        "outcome_counts": {
            "file": "f.csv",
            "column": "churn",
            "positive": "yes",
            "rows_min": 1000,
            "positives_min": 200,
        }
    }
    assert evaluate(rule, case(), {"view": view}, frames={"f.csv": frame}) == []
    short = {"outcome_counts": {**rule["outcome_counts"], "positives_min": 300}}
    assert evaluate(short, case(), {"view": view}, frames={"f.csv": frame}) != []


def test_approvals_are_an_exact_count_and_kept_items_may_wait_or_be_accepted() -> None:
    once = case(approvals=[{"item_ids": []}])
    assert evaluate({"approvals": 1}, once, None) == []
    assert evaluate({"approvals": 1}, case(approvals=[{}, {}]), None) != []
    assert evaluate({"approvals": 1}, case(), None) != []
    spend = {"kept_items": [{"has": ["sum(", "eur"]}]}
    build: dict[str, Any] = {"payload": {"sql": "SELECT SUM(x) FROM t WHERE currency = 'EUR'"}}
    assert evaluate(spend, case(item("s1", "pending", **build)), None) == []
    assert evaluate(spend, case(item("s1", "accepted", **build)), None) == []
    assert evaluate(spend, case(item("s1", "rejected", **build)), None) != []
