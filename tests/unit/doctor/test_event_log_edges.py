"""Counts on an event log say only what belongs to them: a sample's count is called one and replaced by the whole
file's, a repeated customer of a log is not attributed to a step, and an audience's left-out customers are agreed.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from pydantic import JsonValue

from engine.agent.contracts import ProposalState
from engine.doctor.case import Case, CaseItem
from engine.doctor.guards import make_receipt
from engine.doctor.plan_check import _step_row, item_sentence_now
from engine.doctor.visit import _agreed_update, _audience_left_out
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
    assert _audience_left_out(build_item(ids=4_372, kept=3_360, audience=True).payload) == 1_012
    assert _audience_left_out(build_item(ids=4_372, kept=3_360, audience=False).payload) == 1_012
    assert _audience_left_out({"sentence": "a plain step"}) == 0
    assert _audience_left_out({"buildable": {"ids": 10, "active_before": 12}}) == 0
    case = Case.model_construct(visit=CaseVisit())
    # no count made yet (or a sample's smaller one): the yes still agrees to what the audience showed, no more
    assert _agreed_update(case, build_item(ids=4_372, kept=3_360, audience=True), True) == {
        "agreed_rows": 1_012
    }
    assert _agreed_update(case, build_item(ids=4_372, kept=3_360, audience=True), False) == {}
    plain = item("item_x", {"sentence": "s"}, ProposalState.ACCEPTED, upload_id="up-1")
    assert _agreed_update(case, plain, True) == {}
