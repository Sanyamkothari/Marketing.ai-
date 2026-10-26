"""The order-log workflow as one pure entry point (v3 phase 0): what it proposes, and everything it refuses.

`propose_order_log` and `build_from_order_log` are what the AI's order-log tool wraps (docs/plans/
DATA_DOCTOR_AI_FIRST.md, milestone A3). Here, on the small synthetic log: off means off, the customer column and the
guests answer are a person's, an approval must be for exactly the hash and sentence read now, and nothing reaches the
builder before all of that holds. The build itself, on the real file's sample, is `tests/integration/
test_order_log_workflow.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine.config import get_roles, load_use_case
from engine.doctor.build_driver import approval_for as build_approval
from engine.doctor.diagnose import LoadedFile, diagnose_file
from engine.doctor.order_log import blank_customer_question, derive_customer_list, order_log_reading
from engine.doctor.order_log_workflow import (
    GuestsAnswer,
    OrderLogApprovals,
    OrderLogProposal,
    OrderLogWorkflowError,
    build_from_order_log,
    propose_order_log,
)
from engine.doctor.order_roll_up import audience_words
from engine.doctor.outcome_definition import approval_for as outcome_approval
from tests.fixtures.order_log import order_log_config_root, order_log_frame
from tests.unit.doctor.m237_support import RecordingBuilder
from tests.unit.doctor.order_log_support import USE_CASE, loaded

NOW = datetime(2026, 10, 26, tzinfo=UTC)


@pytest.fixture(scope="module")
def on_root(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return order_log_config_root(config_root, tmp_path_factory.mktemp("on"))


@pytest.fixture(scope="module")
def world(on_root: Path):  # type: ignore[no-untyped-def]
    log = order_log_frame(customers=300)
    file = loaded(log, "online-retail.csv", on_root)
    config = load_use_case(USE_CASE, on_root)
    diagnosis = diagnose_file(file, config, get_roles(on_root))
    reading = order_log_reading(file, config)
    assert reading is not None
    derived = derive_customer_list(
        log, "CustomerID", reading.date_column, order_column="InvoiceNo", quantity_column="Quantity"
    )
    question = blank_customer_question(reading, derived)
    assert question is not None and question[2] is not None
    leave_out = GuestsAnswer(option_id="leave_out", finding_key=question[2].key)
    return file, diagnosis, leave_out


@pytest.fixture(scope="module")
def proposal(world, on_root: Path) -> OrderLogProposal:  # type: ignore[no-untyped-def]
    file, diagnosis, leave_out = world
    return propose_order_log(
        file, "CustomerID", leave_out, USE_CASE, config_root=on_root, diagnosis=diagnosis
    )


def propose(  # type: ignore[no-untyped-def]
    world,
    on_root: Path,
    *,
    column: str = "CustomerID",
    answer: GuestsAnswer | str | None = "leave_out",
    file: LoadedFile | None = None,
    use_case: str = USE_CASE,
) -> OrderLogProposal:
    """`propose_order_log` on the module's log, with one input changed at a time."""
    logged, diagnosis, leave_out = world
    return propose_order_log(
        file or logged,
        column,
        leave_out if answer == "leave_out" else answer,  # type: ignore[arg-type]
        use_case,
        config_root=on_root,
        diagnosis=diagnosis,
    )


def approvals_for(proposal: OrderLogProposal) -> OrderLogApprovals:
    assert proposal.outcome.definition is not None
    return OrderLogApprovals(
        outcome=outcome_approval(
            proposal.outcome.definition, proposal.outcome_sentence, user_id="analyst-7", at=NOW
        ),
        build=build_approval(proposal.plan, user_id="analyst-7", at=NOW),
    )


def codes(raised: pytest.ExceptionInfo[OrderLogWorkflowError]) -> str:
    return raised.value.code


def test_the_proposal_is_the_outcome_and_the_plan_each_with_its_exact_sentence(proposal) -> None:  # type: ignore[no-untyped-def]
    assert proposal.outcome.definition is not None and proposal.outcome.blocking == ()
    assert proposal.plan.blocking == () and proposal.plan.order_log is not None
    # The audience is in both sentences and both hashes (the person reads it in each).
    expected = (
        audience_words("orders", 90, rows="a place in the count"),
        audience_words("orders", 90, rows="a row"),
    )
    assert expected[0] in proposal.outcome_sentence and expected[1] in proposal.plan.approval_sentence
    assert proposal.outcome.definition.audience is not None and proposal.plan.audience is not None
    assert proposal.plan.label_spec is not None and proposal.plan.label_spec.name == "reactivated_90d"
    assert proposal.outcome.definition.name == "reactivated_90d" and proposal.outcome.definition.days == 90


def test_the_same_inputs_give_the_same_hashes_and_sentences(world, on_root: Path, proposal) -> None:  # type: ignore[no-untyped-def]
    again = propose(world, on_root)
    assert again.plan.plan_hash == proposal.plan.plan_hash
    assert again.outcome_hash == proposal.outcome_hash
    assert again.outcome_sentence == proposal.outcome_sentence
    assert again.plan.approval_sentence == proposal.plan.approval_sentence


def test_a_diagnosis_given_or_made_reads_the_log_the_same_way(world, on_root: Path, proposal) -> None:  # type: ignore[no-untyped-def]
    file, _, leave_out = world
    made = propose_order_log(file, "CustomerID", leave_out, USE_CASE, config_root=on_root)
    assert made.plan.plan_hash == proposal.plan.plan_hash and made.outcome_hash == proposal.outcome_hash


def test_with_the_switch_off_it_is_refused_and_says_why(world, config_root: Path) -> None:  # type: ignore[no-untyped-def]
    file, _, leave_out = world
    with pytest.raises(OrderLogWorkflowError) as raised:
        propose_order_log(file, "CustomerID", leave_out, USE_CASE, config_root=config_root)
    assert codes(raised) == "ORDER_LOG_OFF"


def test_a_use_case_that_does_not_offer_the_path_is_refused(world, on_root: Path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(OrderLogWorkflowError) as raised:
        propose(world, on_root, use_case="telco-churn")
    assert codes(raised) == "ORDER_LOG_USE_CASE"


@pytest.mark.parametrize("column", ["InvoiceNo", "Country", "NoSuchColumn"])
def test_only_a_column_the_reading_offered_can_name_the_shopper(world, on_root: Path, column: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(OrderLogWorkflowError) as raised:
        propose(world, on_root, column=column)
    assert codes(raised) == "ORDER_LOG_CUSTOMER_UNKNOWN"


def test_a_sample_of_the_log_is_refused_the_path_rolls_up_the_whole_log(world, on_root: Path) -> None:  # type: ignore[no-untyped-def]
    file, _, _ = world
    from dataclasses import replace

    sampled = replace(file, file_rows=len(file.frame) + 1)
    with pytest.raises(OrderLogWorkflowError) as raised:
        propose(world, on_root, file=sampled)
    assert codes(raised) == "ORDER_LOG_SAMPLED"


def test_the_lines_with_no_shopper_are_a_persons_answer(world, on_root: Path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(OrderLogWorkflowError) as unanswered:
        propose(world, on_root, answer=None)
    assert (
        codes(unanswered) == "ORDER_LOG_GUESTS_UNANSWERED" and "no 'CustomerID'" in unanswered.value.message
    )
    with pytest.raises(OrderLogWorkflowError) as stopped:
        propose(world, on_root, answer=GuestsAnswer(option_id="stop"))
    assert codes(stopped) == "ORDER_LOG_STOPPED"
    # A yes tied to another count (here: none) is stale; the guard's own finding key is what a yes carries.
    with pytest.raises(OrderLogWorkflowError) as stale:
        propose(
            world, on_root, answer=GuestsAnswer(option_id="leave_out", finding_key="PLAN_LOSES_TOO_MUCH:old")
        )
    assert codes(stale) == "ORDER_LOG_GUESTS_STALE"
    with pytest.raises(OrderLogWorkflowError) as missing:
        propose(world, on_root, answer=GuestsAnswer(option_id="leave_out"))
    assert codes(missing) == "ORDER_LOG_GUESTS_STALE"


def test_an_order_with_and_without_a_shopper_is_asked_never_assumed(on_root: Path) -> None:
    log = order_log_frame(customers=300, mixed_orders=4)
    file = loaded(log, "online-retail.csv", on_root)
    config = load_use_case(USE_CASE, on_root)
    reading = order_log_reading(file, config)
    assert reading is not None
    derived = derive_customer_list(
        log, "CustomerID", reading.date_column, order_column="InvoiceNo", quantity_column="Quantity"
    )
    question = blank_customer_question(reading, derived)
    assert question is not None and question[2] is not None
    answer = GuestsAnswer(option_id="leave_out", finding_key=question[2].key)
    with pytest.raises(OrderLogWorkflowError) as raised:
        propose_order_log(file, "CustomerID", answer, USE_CASE, config_root=on_root)
    assert codes(raised) == "ORDER_LOG_MIXED_ORDERS"
    kept = propose_order_log(
        file, "CustomerID", answer, USE_CASE, config_root=on_root, mixed_answer="keep_named"
    )
    assert kept.plan.blocking == ()


def test_nothing_reaches_the_builder_without_both_approvals_for_what_is_read_now(  # type: ignore[no-untyped-def]
    world, on_root: Path, proposal
) -> None:
    file, diagnosis, leave_out = world
    port = RecordingBuilder()
    good = approvals_for(proposal)
    wrong_outcome = good.model_copy(
        update={"outcome": good.outcome.model_copy(update={"definition_hash": "0" * 64})}
    )
    reworded = good.model_copy(
        update={"outcome": good.outcome.model_copy(update={"sentence": good.outcome.sentence + " (changed)"})}
    )
    wrong_plan = good.model_copy(update={"build": good.build.model_copy(update={"plan_hash": "0" * 64})})
    plan_reworded = good.model_copy(
        update={"build": good.build.model_copy(update={"sentence": good.build.sentence + " (changed)"})}
    )
    for approvals, code in (
        (wrong_outcome, "OUTCOME_NOT_APPROVED"),
        (reworded, "APPROVAL_SENTENCE_CHANGED"),
        (wrong_plan, "BUILD_PLAN_NOT_APPROVED"),
        (plan_reworded, "APPROVAL_SENTENCE_CHANGED"),
    ):
        with pytest.raises(OrderLogWorkflowError) as raised:
            build_from_order_log(
                file,
                "CustomerID",
                leave_out,
                USE_CASE,
                approvals,
                port,  # type: ignore[arg-type]
                config_root=on_root,
                diagnosis=diagnosis,
            )
        assert codes(raised) == code, code
    assert port.calls == [], "no source, mapping, recipe or build was made"


def test_a_changed_log_is_not_the_one_that_was_approved(world, on_root: Path, proposal) -> None:  # type: ignore[no-untyped-def]
    """The approvals name the hash of the log's roll-up: one more shopper is another plan, refused before any call."""
    _, diagnosis, _ = world
    bigger = order_log_frame(customers=301)
    file = loaded(bigger, "online-retail.csv", on_root)
    config = load_use_case(USE_CASE, on_root)
    reading = order_log_reading(file, config)
    assert reading is not None
    derived = derive_customer_list(
        bigger, "CustomerID", reading.date_column, order_column="InvoiceNo", quantity_column="Quantity"
    )
    question = blank_customer_question(reading, derived)
    assert question is not None and question[2] is not None
    answer = GuestsAnswer(option_id="leave_out", finding_key=question[2].key)
    port = RecordingBuilder()
    with pytest.raises(OrderLogWorkflowError) as raised:
        build_from_order_log(
            file,
            "CustomerID",
            answer,
            USE_CASE,
            approvals_for(proposal),
            port,  # type: ignore[arg-type]
            config_root=on_root,
            diagnosis=diagnose_file(file, config, get_roles(on_root)),
        )
    assert codes(raised) in {"OUTCOME_NOT_APPROVED", "APPROVAL_SENTENCE_CHANGED", "BUILD_PLAN_NOT_APPROVED"}
    assert port.calls == [] and diagnosis is not None
