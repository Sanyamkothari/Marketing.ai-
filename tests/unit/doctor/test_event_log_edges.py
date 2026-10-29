"""Counts on an event log say only what belongs to them: a sample's count is called one and replaced by the whole
file's, a repeated customer of a log is not attributed to a step, and an audience's left-out customers are agreed.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from pydantic import JsonValue

from engine.agent.contracts import ProposalState
from engine.doctor.buildable import audience_left_out
from engine.doctor.case import Case, CaseItem
from engine.doctor.guards import make_receipt
from engine.doctor.plan_check import _step_row, item_sentence_now
from engine.doctor.visit import _agreed_update
from engine.doctor.visit_contracts import CaseVisit, PlanCheck
from tests.unit.doctor.test_sql_trial_state import (
    _dry_run_rows,
    item,
    log,
    parse_date,
    propose,
)

PER_CUSTOMER = "SELECT Customer, count(*) AS n FROM t_log GROUP BY ALL"


def sampled_item(monkeypatch: pytest.MonkeyPatch) -> CaseItem:
    _dry_run_rows(monkeypatch, 10)
    proposal = propose(PER_CUSTOMER, (parse_date(ProposalState.ACCEPTED),), log(60))
    clause = "Customers: 3 before, 3 after (0 lost, 0 repeated)."  # said when the plan knows the ID
    payload = {
        **proposal,
        "sentence": f"{proposal['sentence']} {clause}",
        "sentence_estimate": {**proposal["estimate"], "customers": clause},
    }
    return item("item_q", payload, ProposalState.ACCEPTED, upload_id="up-1")


def test_a_sampled_count_says_it_is_an_estimate(monkeypatch: pytest.MonkeyPatch) -> None:
    _dry_run_rows(monkeypatch, 10)
    proposal = propose(PER_CUSTOMER, (parse_date(ProposalState.ACCEPTED),), log(60))
    assert proposal["estimate"]["rows"].startswith("about ")
    assert (
        "would have about" in proposal["sentence"]
        and "an estimate from 10 sampled rows" in proposal["sentence"]
    )
    assert "tried on" not in proposal["sentence"]
    whole = propose(PER_CUSTOMER, (parse_date(ProposalState.ACCEPTED),), log(8))
    assert whole["estimate"] == {} and "estimate" not in whole["sentence"]


def test_the_count_of_the_whole_file_replaces_the_estimate_once_it_is_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    it = sampled_item(monkeypatch)
    row: dict[str, JsonValue] = {
        "item_id": "item_q",
        "rows_out": 8,
        "customers_in": 8,
        "customers_out": 8,
        "customers_lost": 0,
    }
    full = PlanCheck(digest="d", basis="b", ledger={"steps": [row]})
    sample = PlanCheck(digest="d", ledger={"steps": [row]})
    assert "about" in item_sentence_now(it, sample) and "estimate" in item_sentence_now(it, sample)
    now = item_sentence_now(it, full)
    assert "would have 8 rows" in now and "estimate" not in now and "about" not in now
    assert "Customers: 8 before, 8 after (0 lost, 0 repeated)." in now


def test_a_customer_repeated_on_the_rows_of_a_log_is_not_counted_against_a_date_fix() -> None:
    frame = log(40)
    receipt = make_receipt(1, "parse_date", {"log": frame}, frame.copy(), key="Customer")
    assert (
        receipt.keys_repeated > 0 and receipt.one_row_per_key is False
    )  # the log's own customers, on many rows
    row = _step_row(receipt, "item_parse")
    assert row["customers_duplicated"] is None
    per_customer = pd.DataFrame({"Customer": ["a", "b"], "n": [1, 2]})
    twice = pd.concat([per_customer, per_customer])
    joined = _step_row(make_receipt(1, "sql_query", {"t": per_customer}, twice, key="Customer"), "item_join")
    assert joined["customers_duplicated"] == 2  # each was on one row before: the step repeated them


def build_item(*, ids: int, kept: int, audience: bool) -> CaseItem:
    built: dict[str, Any] = {"ids": ids, "active_before": kept}
    payload: dict[str, Any] = {"buildable": built}
    if audience:
        payload["audience"] = {"audience_id": "active", "customers": kept}
    return item("item_build", payload, ProposalState.ACCEPTED, upload_id="up-1")


def test_the_customers_an_audience_leaves_out_are_agreed_up_to_the_count_it_showed() -> None:
    assert audience_left_out(build_item(ids=4_372, kept=3_360, audience=True).payload) == 1_012
    assert audience_left_out(build_item(ids=4_372, kept=3_360, audience=False).payload) == 1_012
    assert audience_left_out({"sentence": "a plain step"}) == 0
    assert audience_left_out({"buildable": {"ids": 10, "active_before": 12}}) == 0
    case = Case.model_construct(visit=CaseVisit())
    # no count made yet (or a sample's smaller one): the yes still agrees to what the audience showed, no more
    assert _agreed_update(case, build_item(ids=4_372, kept=3_360, audience=True), True) == {
        "agreed_rows": 1_012
    }
    assert _agreed_update(case, build_item(ids=4_372, kept=3_360, audience=True), False) == {}
    plain = item("item_x", {"sentence": "s"}, ProposalState.ACCEPTED, upload_id="up-1")
    assert _agreed_update(case, plain, True) == {}


def test_a_small_agreed_loss_does_not_turn_off_the_plan_loss_stop_for_a_replay() -> None:
    from engine.doctor.plan_check import acknowledged_codes

    def visit_of(customers: int) -> CaseVisit:
        return CaseVisit(plan_check=PlanCheck(digest="d", ledger={"customers_in": customers}))

    def agreed(rows: int) -> CaseItem:
        return item("item_a", {"sentence": "s"}, ProposalState.ACCEPTED, upload_id="up-1").model_copy(
            update={"agreed_rows": rows, "decided_by": "person"}
        )

    assert acknowledged_codes(visit_of(10_000), [agreed(25)]) == ()  # 0.25% of the customers: bounded
    assert acknowledged_codes(visit_of(10_000), [agreed(2_000)]) == ("PLAN_LOSES_TOO_MUCH",)


def test_a_key_that_repeats_by_design_is_not_expected_to_be_unique() -> None:
    from engine.doctor.checks_time_keys import key_quality

    log_ids = pd.Series([f"C{n % 50}" for n in range(1_000)])  # the customer on each row of a log
    assert "KEY_NOT_UNIQUE" not in {c.code for c in key_quality(log_ids, "customer", expect_unique=True)}
    list_ids = pd.Series([f"C{n}" for n in range(1_000)] + ["C1", "C2", "C3"])  # a list with a few repeats
    assert "KEY_NOT_UNIQUE" in {c.code for c in key_quality(list_ids, "customer", expect_unique=True)}


def test_a_build_says_its_two_dates_and_who_its_audience_leaves_out() -> None:
    from datetime import date

    from engine.doctor.buildable import BuildableOutcome
    from engine.doctor.facts_goal import _say_build_counts

    built = BuildableOutcome(
        upload_id="u",
        file_name="f.csv",
        entity_key="CustomerID",
        time_column="InvoiceDate",
        label_type="event_presence",
        window_days=90,
        snapshot=date(2011, 8, 31),
        data_start=date(2010, 12, 1),
        data_end=date(2011, 12, 9),
        ids=4_372,
        active_before=3_360,
        yes=1_893,
        rows_read=541_909,
        file_rows=541_909,
        enough=True,
        sentence="s",
    )
    payload: dict[str, Any] = {
        "sentence": "Build. It would have about 3,102 rows and 8 columns (an estimate).",
        "sentence_estimate": {"rows": "about 3,102 rows", "note": "(an estimate)"},
        "buildable": built.model_dump(mode="json"),
    }
    _say_build_counts(payload, built, "shopper", None, date(2011, 11, 30))
    said = payload["sentence"]
    assert "would have 3,360 rows" in said and "estimate" not in said and "sentence_estimate" not in payload
    assert (
        "Left out, of the file's 4,372 shoppers: 1,012 have no activity on or before 31 Aug 2011, so they were "
        "not known by then. The table is about those active by then."
    ) in said
    assert "the 90 days after it give the outcome, and 30 Nov 2011 is the date your case is read at" in said
    other: dict[str, Any] = {"sentence": "Build.", "buildable": built.model_dump(mode="json")}
    _say_build_counts(other, built, "shopper", None, date(2011, 10, 15))  # the window runs past the case date
    assert "is the date your case is read at" not in other["sentence"]


def test_the_gone_quiet_audience_says_each_group_it_leaves_out_and_why() -> None:
    from datetime import date

    from engine.doctor.buildable import BuildableOutcome, BuildAudience
    from engine.doctor.facts_goal import _say_build_counts

    quiet = BuildAudience(
        audience_id="quiet",
        quiet_days=90,
        label="shoppers who have gone quiet",
        customers=1_402,
        yes=552,
        enough=True,
    )
    built = BuildableOutcome(
        upload_id="u",
        file_name="f.csv",
        entity_key="CustomerID",
        time_column="InvoiceDate",
        label_type="event_presence",
        window_days=90,
        snapshot=date(2011, 8, 31),
        data_start=date(2010, 12, 1),
        data_end=date(2011, 12, 9),
        ids=4_331,
        active_before=3_360,
        yes=1_893,
        rows_read=541_909,
        file_rows=541_909,
        enough=True,
        sentence="s",
        audiences=(quiet,),
    )
    payload: dict[str, Any] = {"sentence": "Build.", "buildable": built.model_dump(mode="json")}
    _say_build_counts(payload, built, "shopper", quiet, None)
    said = payload["sentence"]
    assert "971 have no activity on or before 31 Aug 2011, so they were not known by then" in said
    assert "1,958 were active in the 90 days before 31 Aug 2011, so they have not gone quiet" in said
    assert "Who is kept is the audience you chose." in said
