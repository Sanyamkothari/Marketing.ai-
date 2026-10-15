"""The ask / approve / doubt protocol's rules (Plan O M246, DEC-1816; the design §6.3, §6.5-§6.8).

Pure functions: the impact rule and "I don't know", the question budget, the APPROVE-class list with its roles,
the approval-time check (M233's `data.approve_class` rule), doubts whose every number is grounded, requests for
more data, and the decision record that goes to Plan K's ledger and to the timeline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine.access.roles import LOCAL_OPERATOR, Principal, Role
from engine.agent.contracts import ProposalState
from engine.agentic.ledger import BODY_KEYS, DecisionLedger, FakeSigner, verify_ledger
from engine.doctor.protocol import (
    APPROVE_CLASS_ITEMS,
    DATA_DECISION,
    DATA_PRESCRIPTION_APPROVED,
    DONT_KNOW,
    Ask,
    AskOption,
    DataRequest,
    ProtocolError,
    approve_class_of,
    confirmation_refusal,
    decision_body,
    express_doubt,
    is_material,
    item_kind,
    ledger_port,
    question_budget,
    request_data,
    role_for,
    settle,
)
from engine.doctor.visit_contracts import OptionEffect
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
ASHA = Principal(user_id="u-asha", username="asha", roles=frozenset({Role.ANALYST}), kind="user")
RAVI = Principal(user_id="u-ravi", username="ravi", roles=frozenset({Role.APPROVER}), kind="user")
MEERA = Principal(
    user_id="u-meera", username="meera", roles=frozenset({Role.ANALYST, Role.APPROVER}), kind="user"
)


def _effect(option_id: str, column: str = "joined") -> OptionEffect:
    return OptionEffect(
        option_id=option_id,
        kind="recipe_step",
        title=f"Read {column} {option_id}",
        payload={"upload_id": "up-1", "step": {"kind": "parse_date", "column": column}, "order": option_id},
    )


def _date_order(rows_disagree: int, *, rows: int = 3_000, recommended: str | None = "day") -> Ask:
    return Ask(
        code="DATE_ORDER",
        subject={"upload_id": "up-1", "column": "joined"},
        text="In 'joined', which comes first: the day or the month?",
        why=f"Both readings are valid for {rows_disagree:,} of {rows:,} values.",
        options=(
            AskOption("Day first", _effect("day"), rows_changed=rows),
            AskOption("Month first", _effect("month"), rows_changed=rows),
            AskOption("Leave it as text", OptionEffect(option_id="text"), rows_changed=0, safe=True),
        ),
        rows=rows,
        rows_disagree=rows_disagree,
        upload_id="up-1",
        column="joined",
        recommended=recommended,
    )


# ---------------------------------------------------------------------------
# ASK: the impact rule, "I don't know", AUTO
# ---------------------------------------------------------------------------
def test_an_ambiguity_below_the_impact_threshold_is_decided_with_a_stated_revocable_assumption() -> None:
    """Acceptance (M246): 12 of 3,000 rows (0.4%) differ between the readings - below 0.5% - so the rules decide,
    say so, and the person can change it (the question is kept, answered, `assumed`)."""
    ask = _date_order(12)
    assert not is_material(ask)
    settled = settle(ask, now=NOW)
    assert settled.how == "assume"
    question, item = settled.question, settled.item
    assert question is not None and item is not None
    assert question.answer == "day" and question.assumed and question.answered_by == "agent:data-doctor"
    assert item.state is ProposalState.ACCEPTED and item.item_id == f"{question.question_id}:day"
    assert (
        item.assumption is not None
        and "12 of 3,000 rows" in item.assumption
        and "Change it" in item.assumption
    )
    assert {o.option_id for o in question.options} >= {
        "day",
        "month",
        "text",
    }, "every option stays to change to"


def test_a_material_ambiguity_is_one_question_with_measured_options_and_i_dont_know() -> None:
    ask = Ask(
        code="DATE_ORDER",
        subject={"upload_id": "up-1", "column": "joined"},
        text="In 'joined', which comes first: the day or the month?",
        why="Both readings are valid for 1,204 of 3,000 values.",
        options=(
            AskOption("Day first", _effect("day"), rows_changed=3_000),
            AskOption("Month first", _effect("month"), rows_changed=3_000),
        ),
        rows=3_000,
        rows_disagree=1_204,
        recommended="day",
        blocking=True,
    )
    settled = settle(ask, now=NOW)
    assert settled.how == "ask" and settled.item is None
    question = settled.question
    assert question is not None and question.answer is None and question.blocking
    assert question.why == "Both readings are valid for 1,204 of 3,000 values."
    labels = [o.label for o in question.options]
    assert labels == ["Day first", "Month first", DONT_KNOW]
    dont_know = question.options[-1]
    assert (
        dont_know.safe and dont_know.effect_digest is None
    ), "I don't know maps to the safe choice, never a guess"
    assert question.options[0].rows_changed == 3_000


def test_a_label_rate_move_above_half_a_point_is_material_even_when_few_rows_differ() -> None:
    ask = Ask(
        code="VALUE_DRIFT",
        subject={"column": "status"},
        text="Is 'Lapsed' a churned customer?",
        why="",
        options=(
            AskOption("Yes", _effect("yes", "status"), rows_changed=10, label_rate=12.0),
            AskOption("No", OptionEffect(option_id="no"), rows_changed=0, label_rate=11.2),
        ),
        rows=3_000,
    )
    assert is_material(ask)


def test_an_unmeasured_option_is_asked_about_rather_than_assumed() -> None:
    ask = Ask(
        code="NEW_CATEGORY",
        subject={"column": "plan"},
        text="What is 'PP'?",
        why="",
        options=(
            AskOption("Prepaid", _effect("pre", "plan")),
            AskOption("Postpaid", _effect("post", "plan")),
        ),
        rows=3_000,
    )
    assert is_material(ask) and settle(ask, now=NOW).how == "ask"


def test_values_that_prove_the_answer_are_decided_automatically() -> None:
    ask = Ask(
        code="DATE_ORDER",
        subject={"column": "joined"},
        text="?",
        why="",
        options=(AskOption("Day first", _effect("day")), AskOption("Month first", _effect("month"))),
        rows=3_000,
        proven="day",
    )
    settled = settle(ask, now=NOW)
    assert settled.how == "auto" and settled.question is None
    assert settled.item is not None and settled.item.state is ProposalState.ACCEPTED


def test_a_meaning_bearing_question_recommends_nothing_and_its_assumption_is_the_safe_choice() -> None:
    ask = _date_order(1_204)
    ask = Ask(**{**ask.__dict__, "meaning_bearing": True})
    question = settle(ask, now=NOW).question
    assert question is not None and question.recommended is None
    small = Ask(**{**_date_order(3).__dict__, "meaning_bearing": True})
    assumed = settle(small, now=NOW)
    assert assumed.how == "assume" and assumed.question is not None and assumed.question.answer == "text"
    assert assumed.item is None, "the safe choice leaves the column as it is"


# ---------------------------------------------------------------------------
# The question budget
# ---------------------------------------------------------------------------
def test_the_question_budget_is_two_and_a_half_per_ambiguity_capped_at_ten() -> None:
    assert [question_budget(n) for n in (0, 1, 2, 3, 4, 50)] == [0, 3, 5, 8, 10, 10]


def test_a_question_past_the_budget_takes_the_safe_choice_and_says_why() -> None:
    settled = settle(_date_order(1_204), now=NOW, over_budget=True)
    assert settled.how == "assume" and settled.question is not None
    assert settled.question.answer == "text" and "already asked its 10 questions" in settled.reason
    blocking = Ask(**{**_date_order(1_204).__dict__, "blocking": True})
    assert settle(blocking, now=NOW, over_budget=True).how == "ask", "a blocking question is still asked"


# ---------------------------------------------------------------------------
# APPROVE: the list, the roles, the exact sentence, separation of duties
# ---------------------------------------------------------------------------
def test_the_approve_class_list_is_the_designs() -> None:
    approver = {"consent_column", "map_consent", "suppression", "pii_keep", "threshold"}
    analyst = {
        "acknowledgement",
        "define_outcome",
        "map_outcome_values",
        "outcome_definition",
        "convert_currency",
        "join_files",
        "stack_files",
        "build_dataset",
        "keep_latest_per_id",
        "split_file",
        "remove_rows",
        "sql_query",  # a query the Doctor wrote changes rows in ways no fixed step does: its own yes
    }
    assert set(APPROVE_CLASS_ITEMS) == approver | analyst
    assert {k for k, r in APPROVE_CLASS_ITEMS.items() if r.role is Role.APPROVER} == approver
    assert role_for(APPROVE_CLASS_ITEMS["acknowledgement"], approval_required=True) is Role.APPROVER
    assert role_for(APPROVE_CLASS_ITEMS["acknowledgement"], approval_required=False) is Role.ANALYST
    assert role_for(APPROVE_CLASS_ITEMS["remove_rows"], approval_required=True) is Role.ANALYST


def test_rows_left_out_need_a_yes_only_above_two_percent_and_unknown_counts_as_above() -> None:
    assert approve_class_of("join_files", {"rows_in": 1_000, "rows_out": 985}) is None
    assert approve_class_of("join_files", {"rows_in": 1_000, "rows_out": 970}) is not None
    assert approve_class_of("stack_files", {}) is not None
    assert approve_class_of("key", {"column": "id"}) is None
    step = {"step": {"kind": "remove_rows", "column": "age"}}
    assert (
        item_kind("recipe_step", step) == "remove_rows" and approve_class_of("recipe_step", step) is not None
    )


def _refusal(
    approver: Principal, *, read: str | None = "Use 'opt_in'.", by: str | None = "u-asha"
) -> str | None:
    found = confirmation_refusal(
        rule=APPROVE_CLASS_ITEMS["consent_column"],
        approver=approver,
        sentence_read=read,
        sentence_now="Use 'opt_in'.",
        proposed_by=by,
        approval_required=True,
    )
    return None if found is None else found.code


def test_an_approve_class_yes_needs_the_role_the_exact_sentence_and_another_person() -> None:
    assert _refusal(ASHA) == "TOOL_ROLE_REQUIRED"
    assert _refusal(RAVI, read="Use 'opt-in'.") == "APPROVAL_SENTENCE_CHANGED"
    assert _refusal(RAVI, read=None) == "APPROVAL_SENTENCE_CHANGED"
    assert _refusal(RAVI, by="u-ravi") == "POLICY_ROLE_SEPARATION"
    assert _refusal(RAVI) is None
    assert _refusal(LOCAL_OPERATOR, by="local-operator") is None, "sign-in off: one person holds every role"


def test_separation_applies_only_when_the_use_case_asks_for_it() -> None:
    found = confirmation_refusal(
        rule=APPROVE_CLASS_ITEMS["remove_rows"],
        approver=MEERA,
        sentence_read="Remove rows.",
        sentence_now="Remove rows.",
        proposed_by="u-meera",
        approval_required=False,
    )
    assert found is None


# ---------------------------------------------------------------------------
# Doubts: three parts, every number grounded
# ---------------------------------------------------------------------------
EVIDENCE = {
    "ev_aaaabbbbccccdddd": {
        "column": "amount",
        "rows": 312,
        "median": 2_000,
        "low": 2,
        "mean": 1_880,
        "if": 2_090,
    }
}


def test_every_number_in_a_doubt_is_grounded() -> None:
    """Acceptance (M246): a doubt states only numbers its cited results hold."""
    doubt = express_doubt(
        "UNIT_MISMATCH",
        because="'amount' has values near 2,000 for most rows and near 2 for 312 rows, which could be thousands.",
        would_do="leave these 312 values as written and flag the column.",
        if_not="if they are thousands, the average spend rises from ₹1,880 to ₹2,090.",
        evidence=EVIDENCE,
        upload_id="up-1",
    )
    assert doubt.evidence_ids == ("ev_aaaabbbbccccdddd",)
    with pytest.raises(ProtocolError) as refused:
        express_doubt(
            "UNIT_MISMATCH",
            because="'amount' has values near 2,000 for most rows and near 2 for 312 rows.",
            would_do="leave them as written.",
            if_not="the average spend rises from ₹1,880 to ₹2,450.",
            evidence=EVIDENCE,
        )
    assert refused.value.code == "DOUBT_UNGROUNDED"


def test_a_doubt_has_all_three_parts_and_column_names_are_not_numbers() -> None:
    with pytest.raises(ProtocolError):
        express_doubt("X", because="unsure", would_do="", if_not="x", evidence={})
    doubt = express_doubt(
        "VALUE_DRIFT",
        because="'visits_90d' moved this month.",
        would_do="ask before using it.",
        if_not="the model reads a different 'visits_90d'.",
        evidence={},
        names=("visits_90d",),
    )
    assert doubt.because.startswith("'visits_90d'")


# ---------------------------------------------------------------------------
# Requests for more data or a different file
# ---------------------------------------------------------------------------
def test_a_request_for_data_names_what_whom_why_and_the_file_wanted() -> None:
    question, effects = request_data(
        DataRequest(
            kind="more_data",
            what="the date of each customer's last order",
            why="Win back needs it.",
            columns=("customer_id", "last_order_date"),
            ask_whom="your billing team",
            goal_id="retail-win-back",
        )
    )
    assert question.code == "DATA_REQUESTED" and not question.blocking
    assert (
        question.text
        == "Ask your billing team for the date of each customer's last order; Win back needs it."
    )
    assert question.why == "The file needed: one row per customer, with customer_id, last_order_date."
    assert [o.label for o in question.options] == ["I will add it", "I cannot get it"]
    assert question.options[1].safe and all(e.kind is None for e in effects)
    other, _ = request_data(
        DataRequest(kind="different_file", what="one row per customer", why="This is a summary.")
    )
    assert other.code == "DIFFERENT_FILE_REQUESTED" and other.text.startswith(
        "This file cannot be used as it is."
    )
    again, _ = request_data(
        DataRequest(
            kind="more_data",
            what="the date of each customer's last order",
            why="Win back needs it.",
            columns=("customer_id", "last_order_date"),
            ask_whom="your billing team",
            goal_id="retail-win-back",
        )
    )
    assert again.question_id == question.question_id, "asked once"


# ---------------------------------------------------------------------------
# The record: ledger body keys, and entries that verify
# ---------------------------------------------------------------------------
def test_a_decision_record_holds_only_ledger_body_keys_and_binds_the_sentence() -> None:
    body = decision_body(
        case_id="case_0123456789abcdef",
        upload_id="up-1",
        item_digests=["a" * 64],
        text="Use 'opt_in'.",
        decided_by="u-ravi",
        proposed_by="u-asha",
        kind="consent_column",
        prescription_hash="b" * 64,
        recipe_hash=None,
    )
    assert set(body) <= BODY_KEYS
    other = decision_body(
        case_id="case_0123456789abcdef",
        upload_id="up-1",
        item_digests=["a" * 64],
        text="Use 'opt-in'.",
        decided_by="u-ravi",
    )
    assert body["payload_digest"] != other["payload_digest"], "the exact text is part of what is recorded"


def test_ledger_entries_of_data_decisions_verify(tmp_path: Path) -> None:
    """Acceptance (M246): `data.prescription.approved` and `data.decision` entries verify on Plan K's ledger."""
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    signer = FakeSigner()
    record = ledger_port(engine, signer, "p" * 64)
    body = decision_body(
        case_id="case_0123456789abcdef",
        upload_id=None,
        item_digests=["a" * 64, "c" * 64],
        text="Prepare 1 file with 2 changes. Nothing else changes.",
        decided_by="u-asha",
        prescription_hash="d" * 64,
    )
    first = record(DATA_DECISION, "u-ravi", {**body, "proposal_kind": "consent_column"})
    second = record(DATA_PRESCRIPTION_APPROVED, "u-asha", body)
    assert len(first) == 64 and first != second
    entries = list(DecisionLedger(engine, signer=signer, policy_version="p" * 64).entries())
    assert [e.kind for e in entries] == [DATA_DECISION, DATA_PRESCRIPTION_APPROVED]
    assert entries[1].body["prescription_hash"] == "d" * 64 and "upload_id" not in entries[1].body
    assert verify_ledger(engine, signer).ok
