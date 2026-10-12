"""`join_files` (Plan O M237, DEC-1807 (c)): per-customer files and outcome lists onto a customer list.

The acceptance of Plan O §M237, without a person: a customer list plus a churned-IDs list plus two CRM exports
join with exact counts on the receipt; `CUST-00123` and `123` join after the `normalise_key` rule and nothing
joins on a near match; a join that drops more than 2% of a file's rows (or defines the outcome) is an
APPROVE-class item.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from engine.agent.key_steps import KeyRule
from engine.doctor.join_files import (
    ColumnPick,
    JoinError,
    JoinInput,
    JoinPart,
    JoinSpec,
    outcome_sentence,
    plan_join,
    run_join,
)

CUSTOMERS = 400


def customers() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"CUST-{n:05d}" for n in range(1, CUSTOMERS + 1)],
            "city": [("Pune", "Delhi", "Agra")[n % 3] for n in range(CUSTOMERS)],
        }
    )


def churned() -> pd.DataFrame:
    """60 churned customers, written as bare numbers; 3 of them are not on the customer list."""
    ids = [*[n for n in range(1, CUSTOMERS + 1) if n % 7 == 0][:57], 9001, 9002, 9003]
    return pd.DataFrame({"customer_id": [str(n) for n in ids]})


def crm_plans() -> pd.DataFrame:
    """Every listed customer's plan, keyed as written in the list, plus 2 rows for customers not on it."""
    ids = [f"CUST-{n:05d}" for n in range(1, CUSTOMERS + 1)] + ["CUST-99998", "CUST-99999"]
    return pd.DataFrame({"customer_id": ids, "plan": ["pre" if i % 2 else "post" for i in range(len(ids))]})


def crm_usage() -> pd.DataFrame:
    """Usage for 380 customers, keyed `cust_no` with zeros; 10 rows repeat exactly (an export glitch)."""
    rows = [{"cust_no": f"{n:07d}", "data_gb": float(n % 40), "city": "x"} for n in range(1, 381)]
    rows += rows[:10]
    return pd.DataFrame(rows)


def _inputs() -> tuple[JoinInput, list[JoinInput]]:
    base = JoinInput("u_list", "customers.csv", customers())
    others = [
        JoinInput("u_churn", "churned_ids.csv", churned()),
        JoinInput("u_plans", "crm_plans.csv", crm_plans()),
        JoinInput("u_usage", "crm_usage.csv", crm_usage()),
    ]
    return base, others


def test_a_customer_list_a_churned_list_and_two_crm_exports_join_with_exact_counts() -> None:
    base, others = _inputs()
    proposal = plan_join(base, others, base_key_column="customer_id", outcome_name="churned")
    assert proposal.spec is not None and proposal.receipt is not None
    assert (proposal.state, proposal.confidence, proposal.never_ticked) == ("pending", "check", True)
    spec, receipt = proposal.spec, proposal.receipt
    kinds = {part.upload_id: part.kind for part in spec.parts}
    assert kinds == {"u_churn": "outcome_list", "u_plans": "attributes", "u_usage": "attributes"}
    run = run_join(base.frame, {item.upload_id: item.frame for item in others}, spec)
    assert run.receipt == receipt, "the proposal's receipt is the run's"
    assert receipt.base_rows == receipt.rows_out == CUSTOMERS
    parts = {part.upload_id: part for part in receipt.parts}

    churn = parts["u_churn"]
    assert (churn.rows, churn.rows_matched, churn.rows_dropped, churn.keys) == (60, 57, 3, 60)
    assert churn.base_rows_matched == 57 and churn.dropped_share == 0.05
    assert int(run.frame["churned"].sum()) == 57

    plans = parts["u_plans"]
    assert (plans.rows, plans.rows_matched, plans.rows_dropped, plans.dropped_share) == (402, 400, 2, 0.005)
    assert plans.base_rows_matched == 400 and plans.columns_added == ("plan",)

    usage = parts["u_usage"]
    assert (usage.rows, usage.exact_repeats_removed, usage.rows_matched, usage.rows_dropped) == (
        390,
        10,
        390,
        0,
    )
    assert usage.base_rows_matched == 380 and usage.keys == 380
    assert usage.columns_added == (
        "data_gb",
        "crm_usage_city",
    ), "a clashing name is prefixed, not overwritten"
    assert int(run.frame["data_gb"].notna().sum()) == 380

    assert receipt.approval_required
    assert set(receipt.approval_reasons) == {"JOIN_DEFINES_OUTCOME", "JOIN_DROPS_ROWS"}
    assert proposal.outcome_sentences == (outcome_sentence("churned_ids.csv", "churned"),)
    # The list is matched as `123`, so every file is written that way: all four carry a rule.
    assert set(proposal.normalise_key) == {
        "customers.csv",
        "churned_ids.csv",
        "crm_plans.csv",
        "crm_usage.csv",
    }


def test_the_base_keeps_every_row_and_its_ids_as_written() -> None:
    base, others = _inputs()
    proposal = plan_join(base, others, base_key_column="customer_id")
    assert proposal.spec is not None
    run = run_join(base.frame, {item.upload_id: item.frame for item in others}, proposal.spec)
    assert list(run.frame["customer_id"]) == list(customers()["customer_id"]), "IDs never rewritten"
    assert len(run.frame) == CUSTOMERS


def test_cust_00123_and_123_join_only_after_the_approved_rule() -> None:
    base = customers()
    churn = pd.DataFrame({"customer_id": ["123", "7"]})
    as_written = JoinSpec(
        base_upload_id="u_list",
        base_file_name="customers.csv",
        base_key_column="customer_id",
        parts=(
            JoinPart(
                upload_id="u_churn",
                file_name="churned_ids.csv",
                kind="outcome_list",
                key_column="customer_id",
                outcome_column="churned",
            ),
        ),
    )
    nothing = run_join(base, {"u_churn": churn}, as_written)
    assert int(nothing.frame["churned"].sum()) == 0 and nothing.receipt.parts[0].rows_dropped == 2
    ruled = as_written.model_copy(update={"base_key_rule": KeyRule(prefix="CUST-", zeros=True)})
    joined = run_join(base, {"u_churn": churn}, ruled)
    assert int(joined.frame["churned"].sum()) == 2
    marked = joined.frame.loc[joined.frame["churned"] == 1, "customer_id"]
    assert list(marked) == ["CUST-00007", "CUST-00123"]


def test_nothing_joins_on_a_near_match() -> None:
    base = JoinInput("u_list", "customers.csv", customers())
    near = JoinInput(
        "u_near",
        "lookalikes.csv",
        pd.DataFrame({"customer_id": [f"CUST-0O{n:03d}" for n in range(1, 301)], "plan": ["a"] * 300}),
    )
    proposal = plan_join(base, [near], base_key_column="customer_id")
    assert proposal.spec is None and proposal.receipt is None
    (part,) = proposal.parts
    assert not part.joined and part.fixed_coverage < 0.8
    assert "near match" in part.summary


def test_a_join_dropping_at_most_two_percent_is_not_approve_class() -> None:
    base = JoinInput("u_list", "customers.csv", customers())
    plans = JoinInput("u_plans", "crm_plans.csv", crm_plans())  # 2 of 402 rows dropped: 0.5%
    proposal = plan_join(base, [plans], base_key_column="customer_id")
    assert proposal.receipt is not None
    assert not proposal.receipt.approval_required and proposal.receipt.approval_reasons == ()


def test_a_join_dropping_more_than_two_percent_is_approve_class() -> None:
    base = JoinInput("u_list", "customers.csv", customers())
    extra = [f"CUST-{n:05d}" for n in range(5000, 5020)]  # 20 of 420 rows have no customer: 4.8%
    frame = pd.DataFrame({"customer_id": list(customers()["customer_id"]) + extra, "plan": ["p"] * 420})
    proposal = plan_join(base, [JoinInput("u_p", "plans.csv", frame)], base_key_column="customer_id")
    assert proposal.receipt is not None
    assert proposal.receipt.approval_reasons == ("JOIN_DROPS_ROWS",)
    assert proposal.receipt.parts[0].rows_dropped == 20


def test_a_per_customer_file_with_conflicting_rows_is_refused() -> None:
    frame = pd.DataFrame({"customer_id": ["CUST-00001", "CUST-00001"], "plan": ["pre", "post"]})
    spec = JoinSpec(
        base_upload_id="u_list",
        base_file_name="customers.csv",
        base_key_column="customer_id",
        parts=(
            JoinPart(
                upload_id="u_p",
                file_name="plans.csv",
                kind="attributes",
                key_column="customer_id",
                columns=(ColumnPick(source="plan", output="plan"),),
            ),
        ),
    )
    with pytest.raises(JoinError) as raised:
        run_join(customers(), {"u_p": frame}, spec)
    assert raised.value.code == "JOIN_KEY_REPEATS"


def test_next_months_files_replay_the_frozen_join() -> None:
    base, others = _inputs()
    proposal = plan_join(base, others, base_key_column="customer_id", outcome_name="churned")
    assert proposal.spec is not None
    frames = {item.upload_id: item.frame for item in others}
    frames["u_plans"] = crm_plans().assign(new_field=1)
    replayed = run_join(base.frame, frames, proposal.spec)
    plans = next(part for part in replayed.receipt.parts if part.upload_id == "u_plans")
    assert plans.columns_left_out == ("new_field",), "a new column is left out and counted, not joined"
    assert "new_field" not in replayed.frame.columns
    frames["u_plans"] = crm_plans().drop(columns=["plan"])
    with pytest.raises(JoinError) as raised:
        run_join(base.frame, frames, proposal.spec)
    assert raised.value.code == "JOIN_COLUMN_MISSING"


def test_the_spec_hash_is_stable_and_ignores_file_names() -> None:
    base, others = _inputs()
    first = plan_join(base, others, base_key_column="customer_id").spec
    again = plan_join(base, others, base_key_column="customer_id").spec
    assert first is not None and again is not None
    assert first.spec_hash == again.spec_hash
    renamed = first.model_copy(update={"base_file_name": "customers_may.csv"})
    assert renamed.spec_hash == first.spec_hash


def test_the_proposal_holds_no_customer_id() -> None:
    """Join tools return counts, never keys (Plan O §8): no ID of any file appears in the proposal."""
    base, others = _inputs()
    text = json.dumps(plan_join(base, others, base_key_column="customer_id").model_dump(mode="json"))
    for frame, column in ((customers(), "customer_id"), (churned(), "customer_id"), (crm_usage(), "cust_no")):
        for value in frame[column]:
            assert f'"{value}"' not in text
