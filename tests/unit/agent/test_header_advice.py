"""Guided setup reads Hinglish and abbreviated headers (Plan K M142, DEC-1409), through the advisor and the session.

M142's setup help is always on (there is no switch), so every context here is built on the shipped configuration
(`header_help_context`).

Every test goes in through `advise`, `start_session`, `answer` or `accept_recommended` on a file built the way
an upload's is. The acceptance rules: a column read from its header is only ever a "please check" suggestion,
no suggestion is applied without the person's tick (whatever `agent.tick_uncertain` says), and a role the rules
cannot read stays a question.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.agent.advisor import ROLE_PRIMARY_KEY, ROLE_TARGET, Advice, advise
from engine.agent.contracts import AgentConfidence, ProposalKind, ProposalState, SessionStatus
from engine.agent.session import accept_recommended, answer, start_session
from engine.agent.tools import AgentContext, call_tool
from tests.fixtures.agent_bench.make_hinglish import CASES, hinglish_frame
from tests.unit.agent.helpers import header_help_context as context_for


def _case(name: str) -> pd.DataFrame:
    return hinglish_frame(next(spec for spec in CASES if spec.name == name))


def _result(ctx: AgentContext, tool: str) -> dict[str, Any]:
    """A tool's result as plain data, the way the model and the screen read it."""
    return dict(call_tool(ctx, tool, {}, evidence_id="e1").result)


def _ticking(ctx: AgentContext) -> AgentContext:
    """The same context with `agent.tick_uncertain` on: "please check" suggestions start accepted."""
    agent = ctx.config.agent.model_copy(update={"tick_uncertain": True})
    return dataclasses.replace(ctx, config=ctx.config.model_copy(update={"agent": agent}))


def _roles(advice: Advice) -> dict[str, tuple[str, str, bool]]:
    return {
        str(p.path): (str(p.value), p.confidence.value, p.from_headers)
        for p in advice.proposals
        if p.kind is ProposalKind.ROLE
    }


def test_the_id_is_the_column_whose_name_reads_as_an_id_among_those_that_are_unique() -> None:
    advice = advise(context_for(_case("telco_hinglish")))
    assert _roles(advice)[ROLE_PRIMARY_KEY] == ("grahak_id", "check", True)
    assert advice.primary_key == "grahak_id"
    key = next(p for p in advice.proposals if p.path == ROLE_PRIMARY_KEY)
    assert "reads as customer, id" in key.reason
    assert len(key.evidence_ids) == 2  # the profile that found it unique, and the headers it was read from


def test_a_single_unique_column_or_a_hinted_one_is_decided_as_before() -> None:
    advice = advise(context_for(_case("telco_prepaid_abbr")))  # cust_mob_no is the only unique column
    assert _roles(advice)[ROLE_PRIMARY_KEY] == ("cust_mob_no", "sure", False)
    advice = advise(
        context_for(_case("wide_telecom"))
    )  # cust_id is a hint of the use case; msisdn is unique too
    assert _roles(advice)[ROLE_PRIMARY_KEY] == ("cust_id", "sure", False)


def test_the_outcome_with_a_clear_lead_is_proposed_to_check_not_asked() -> None:
    advice = advise(context_for(_case("telco_prepaid_abbr")))  # churn_flg, with two other two-valued columns
    assert _roles(advice)[ROLE_TARGET] == ("churn_flg", "check", True)
    assert not [q for q in advice.questions if "Which column says" in q.text]
    assert advice.target == "churn_flg"


def test_an_outcome_the_headers_cannot_tell_stays_a_question_in_the_rules_order() -> None:
    advice = advise(context_for(_case("food_delivery_camel")))  # orderedAgain is not a word the lexicon knows
    asked = [q for q in advice.questions if "Which column says" in q.text]
    assert len(asked) == 1 and asked[0].blocking
    assert not [p for p in advice.proposals if p.path == ROLE_TARGET]
    assert {o.label for o in asked[0].options} == {"orderedAgain", "marketingOptIn", "doNotDisturb"}


def test_the_headers_only_order_the_two_valued_columns_the_rules_found_and_never_add_one() -> None:
    frame = _case("telco_prepaid_abbr").assign(
        churn_amount=range(2_000)
    )  # reads like an outcome, has 2,000 values
    advice = advise(context_for(frame))
    assert advice.target == "churn_flg"
    two_valued = {"churn_flg", "mkt_optin", "dnd_flag"}
    offered = {o.label for q in advice.questions for o in q.options}
    assert not offered & {"churn_amount"} and advice.target in two_valued


def test_nothing_read_from_a_header_is_ticked_even_when_uncertain_suggestions_are() -> None:
    ctx = _ticking(context_for(_case("telco_hinglish")))
    session = accept_recommended(start_session(ctx, session_id="s-1"), ctx)
    by_path = {p.path: p for p in session.proposals if p.kind is ProposalKind.ROLE}
    assert by_path[ROLE_PRIMARY_KEY].from_headers and by_path[ROLE_PRIMARY_KEY].state is ProposalState.PENDING
    assert by_path[ROLE_TARGET].from_headers and by_path[ROLE_TARGET].state is ProposalState.PENDING
    assert session.status is SessionStatus.NEEDS_REVIEW


def test_without_the_headers_the_same_switch_still_ticks_what_it_always_did() -> None:
    """`tick_uncertain` keeps its meaning for every suggestion the headers did not make."""
    frame = pd.DataFrame(
        {"customer_id": [f"C{i}" for i in range(1000)], "y": [0, 1] * 500, "spend": range(1000)}
    )
    ctx = _ticking(context_for(frame))
    session = accept_recommended(start_session(ctx, session_id="s-1"), ctx)
    target = next(p for p in session.proposals if p.path == ROLE_TARGET)
    assert not target.from_headers and target.state is ProposalState.ACCEPTED


def test_the_opt_out_the_consent_and_the_last_contact_are_questions_with_no_change_behind_them() -> None:
    ctx = context_for(_case("telco_prepaid_abbr"))
    advice = advise(ctx)
    notes = [q for q in advice.questions if "may say" in q.text]
    assert [q.text.split("'")[1] for q in notes] == ["mkt_optin", "dnd_flag", "last_cntct_dt"]
    for note in notes:
        assert not note.blocking and note.answer is None
        assert all(option.proposal is None for option in note.options)
        assert "Nothing is changed here" in note.options[0].effect
    # The column the use case holds back opted-out customers by is named, and the polarity is spelled out.
    opt_out = next(q for q in notes if "dnd_flag" in q.text)
    assert (
        "'marketing_opt_in'" in opt_out.text
        and "1 for customers who may be contacted" in opt_out.options[0].effect
    )
    session = start_session(ctx, session_id="s-1")
    asked = [q for q in session.questions if "may say" in q.text]
    assert [q.text for q in asked] == [q.text for q in notes]
    for note in asked:
        before = session.proposals
        session = answer(session, ctx, note.question_id, "yes")
        assert session.proposals == before  # answering changes nothing in the setup
    assert session.status is SessionStatus.NEEDS_REVIEW  # the role suggestions are still the person's to tick


def test_no_note_when_the_file_already_has_the_column_the_use_case_reads() -> None:
    frame = _case("telco_prepaid_abbr").rename(
        columns={"dnd_flag": "marketing_opt_in", "last_cntct_dt": "last_contacted_at"}
    )
    texts = " ".join(q.text for q in advise(context_for(frame)).questions)
    assert "may say which customers asked not to be contacted" not in texts
    assert "may say when each customer was last contacted" not in texts


def test_a_date_column_read_as_the_last_contact_is_not_the_date_the_file_describes() -> None:
    frame = _case("wide_telecom")
    advice = advise(context_for(frame, "targeted-advertisement"))
    time = {p.path: p.value for p in advice.proposals if p.kind is ProposalKind.SETTING}
    assert time.get("split.time_column") == "snapshot_date"  # not last_contact_date, not signup_dt


def test_the_old_find_roles_keys_are_unchanged_and_the_new_one_is_additive() -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(300)],
            "Purchased": [i % 2 for i in range(300)],
            "marketing_opt_in": [1] * 300,
            "unsubscribed_flag": [0] * 300,
            "last_contacted_at": ["2026-08-01"] * 300,
        }
    )
    result = _result(context_for(frame), "find_roles")
    assert [c["column"] for c in result["target_by_synonym"]] == ["Purchased"]
    assert result["consent"] == ["marketing_opt_in"]
    assert result["opt_out"] == ["unsubscribed_flag"]
    assert result["recently_contacted"] == ["last_contacted_at"]
    kinds = {c["kind"] for c in result["header_candidates"]}
    assert {"consent", "opt_out", "last_contact", "id"} <= kinds


def test_a_header_read_column_is_added_to_the_role_lists_after_the_use_cases_own_hints() -> None:
    frame = _case("telco_prepaid_abbr").assign(marketing_opt_in=1)
    result = _result(context_for(frame), "find_roles")
    assert result["consent"] == ["marketing_opt_in", "mkt_optin"]


@pytest.mark.parametrize("name", ["lapse_no_outcome_telco", "lapse_no_outcome_retail"])
def test_a_file_with_no_outcome_gets_a_lapse_draft_as_a_question_and_in_the_stop_reason(name: str) -> None:
    ctx = context_for(_case(name))
    advice = advise(ctx)
    assert advice.stop_reason is not None and advice.stop_reason.startswith("No column says whether")
    assert "A draft outcome to start from" in advice.stop_reason
    assert "30 days after the date the file describes, plus 7 days' grace" in advice.stop_reason
    draft = next(q for q in advice.questions if "no column that says whether a customer lapsed" in q.text)
    assert not draft.blocking
    assert [o.option_id for o in draft.options] == ["window-30", "window-60", "window-90", "none"]
    assert all(o.proposal is None for o in draft.options)  # a question, never a change
    assert "the customer on row" in draft.text  # example customers, by row
    assert advice.proposals == () or all(p.kind is not ProposalKind.RECIPE_STEP for p in advice.proposals)


def test_a_lapse_draft_never_shows_an_id_and_counts_what_the_file_holds() -> None:
    ctx = context_for(_case("lapse_no_outcome_telco"))
    result = _result(ctx, "draft_outcome_labels")
    assert result["column"] == "last_rchg_dt" and result["activity"] == "recharge"
    assert result["reference_column"] == "tareekh" and result["reference_date"] == "2026-09-30"
    drafts = result["drafts"]
    assert [d["window_days"] for d in drafts] == [30, 60, 90]
    quiet = [d["already_quiet"] for d in drafts]
    assert quiet == sorted(quiet, reverse=True)  # a longer window leaves fewer customers already quiet
    text = str(result)
    assert "9000" not in text and "G1" not in text  # row numbers, never an ID or a phone number
    frame = ctx.frame
    # Each customer is measured from the date their own row describes, not from the newest date in the file.
    age = (pd.to_datetime(frame["tareekh"]) - pd.to_datetime(frame["last_rchg_dt"])).dt.days
    for draft in drafts:
        limit = draft["window_days"] + draft["grace_days"]
        assert draft["already_quiet"] == int((age > limit).sum())
        assert draft["still_active"] == int(age.between(0, limit).sum())
        assert draft["after_reference"] == int((age < 0).sum())
        assert draft["no_date"] == int(age.isna().sum())
    assert drafts[0]["already_quiet"] == int(
        ((pd.to_datetime(frame.tareekh) - pd.to_datetime(frame.last_rchg_dt)).dt.days > 37).sum()
    )
    newest_only = (pd.Timestamp("2026-09-30") - pd.to_datetime(frame["last_rchg_dt"])).dt.days
    assert drafts[0]["already_quiet"] < int(
        (newest_only > 37).sum()
    )  # the single-newest-date count overstated


def test_a_recharge_dated_after_its_own_snapshot_is_counted_apart_not_dropped() -> None:
    frame = _case("lapse_no_outcome_telco").copy()
    frame["tareekh"] = "2026-06-30"
    frame["last_rchg_dt"] = "2026-06-01"
    frame.loc[:99, "last_rchg_dt"] = "2026-07-15"  # after the date the row describes
    result = _result(context_for(frame), "draft_outcome_labels")
    first = result["drafts"][0]
    assert first["after_reference"] == 100
    assert first["still_active"] == len(frame) - 100 and first["already_quiet"] == 0


def test_a_lapse_draft_with_no_snapshot_column_counts_back_from_the_newest_activity() -> None:
    frame = _case("lapse_no_outcome_telco").drop(columns=["tareekh"])
    result = _result(context_for(frame), "draft_outcome_labels")
    assert result["reference_column"] is None
    newest = pd.to_datetime(frame["last_rchg_dt"]).max()
    assert result["reference_date"] == newest.date().isoformat()
    age = (newest - pd.to_datetime(frame["last_rchg_dt"])).dt.days
    assert result["drafts"][0]["already_quiet"] == int((age > 37).sum())


def test_an_opt_out_flag_is_not_the_outcome_of_a_file_that_has_none_and_the_draft_is_shown() -> None:
    frame = _case("lapse_no_outcome_telco").assign(dnd_flag=[i % 2 for i in range(2_000)])
    advice = advise(context_for(frame))
    assert advice.target is None
    assert not [p for p in advice.proposals if p.path == ROLE_TARGET]  # dnd_flag is not proposed
    assert advice.stop_reason is not None and "A draft outcome to start from" in advice.stop_reason
    draft = next(q for q in advice.questions if "no column that says whether a customer lapsed" in q.text)
    assert not draft.blocking and "the customer on row" in draft.text
    assert "30 days:" in draft.text and "90 days:" in draft.text  # every window's count is in the question


def test_a_file_without_a_date_of_activity_gets_no_draft() -> None:
    frame = _case("lapse_no_outcome_telco").drop(columns=["last_rchg_dt", "pehla_order"])
    advice = advise(context_for(frame))
    assert advice.stop_reason is not None and "A draft outcome" not in advice.stop_reason
    assert not [q for q in advice.questions if "lapsed" in q.text]
    result = _result(context_for(frame), "draft_outcome_labels")
    assert result["drafts"] == [] and "no date" in str(result["message"])


def test_a_day_month_date_nobody_has_settled_gives_no_draft() -> None:
    frame = _case("lapse_no_outcome_telco")
    frame["last_rchg_dt"] = [f"{1 + i % 12:02d}/{1 + i % 11:02d}/2026" for i in range(len(frame))]
    result = _result(context_for(frame), "draft_outcome_labels")
    assert result["drafts"] == []  # 03/04/2026: March or April; the format question comes first


@pytest.mark.parametrize(
    ("use_case", "column"),
    [
        ("win-back-campaign", "cash_back_flag"),  # outcome name `won_back`: `back` alone is not it
        (
            "card-default-propensity",
            "payment_reminder_sent",
        ),  # `missed_payment` / `default_payment_next_month`
        ("rca", "next_bill_flag"),  # `churn_next_60d`: `next` alone is not it
    ],
)
def test_one_word_of_a_long_outcome_name_does_not_make_a_column_the_outcome(
    use_case: str, column: str
) -> None:
    frame = pd.DataFrame(
        {
            "grahak_id": [f"G{i:05d}" for i in range(2_000)],
            column: [(i // 3) % 2 for i in range(2_000)],
            "other_flag": [(i // 5) % 2 for i in range(2_000)],
            "kul_kharch": range(2_000),
        }
    )
    advice = advise(context_for(frame, use_case))
    assert advice.target is None
    assert not [p for p in advice.proposals if p.path == ROLE_TARGET and p.from_headers]
    asked = [q for q in advice.questions if "Which column says" in q.text]
    assert len(asked) == 1 and asked[0].blocking
    assert {o.label for o in asked[0].options} == {column, "other_flag"}


def test_a_whole_multi_word_outcome_name_still_anchors_the_outcome_in_order() -> None:
    frame = pd.DataFrame(
        {
            "grahak_id": [f"G{i:05d}" for i in range(2_000)],
            "WonBack_flag": [(i // 3) % 2 for i in range(2_000)],
            "other_flag": [(i // 5) % 2 for i in range(2_000)],
        }
    )
    advice = advise(context_for(frame, "win-back-campaign"))
    # `won_back` is a synonym: both words, in order, make the one column the use case's own hint (no question).
    assert advice.target == "WonBack_flag"


def test_confidence_is_never_sure_for_a_header_read_pick_that_replaced_the_rules_choice() -> None:
    advice = advise(context_for(_case("telco_hinglish")))
    assert all(p.confidence is AgentConfidence.CHECK for p in advice.proposals if p.from_headers)


def test_drafting_a_lapse_outcome_takes_time_in_proportion_to_the_rows() -> None:
    """Best of three at 25,000 and at 200,000 rows (eight times as many): the ratio stays near eight."""
    from time import perf_counter

    from engine.agent.label_drafts import draft_labels

    small = _case("lapse_no_outcome_telco")
    columns = context_for(small).profile.columns

    def best(rows: int) -> float:
        frame = pd.concat([small] * (rows // len(small) + 1), ignore_index=True).head(rows)
        times = []
        for _ in range(3):
            start = perf_counter()
            assert draft_labels(frame, columns) is not None
            times.append(perf_counter() - start)
        return min(times)

    ratio = best(200_000) / best(25_000)
    assert ratio < 20, f"8x the rows took {ratio:.1f}x as long"


def test_a_broken_lexicon_leaves_guided_setup_working_on_the_use_cases_own_hints(tmp_path: Path) -> None:
    """An operator's mistake in the YAML is logged, not a 500: no header reading, everything else as before."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "header_lexicon.yaml").write_text(
        "schema_version: 1\nmeanings: {}\n", encoding="utf-8"
    )
    (tmp_path / "agentic").mkdir()  # Plan K on, so the broken lexicon is what leaves the headers unread
    (tmp_path / "agentic" / "agentic.yaml").write_text("agentic:\n  enabled: true\n", encoding="utf-8")
    ctx = dataclasses.replace(context_for(_case("telco_hinglish")), config_root=tmp_path)
    found = _result(ctx, "find_roles")
    assert found["header_candidates"] == []
    assert _result(ctx, "draft_outcome_labels")["drafts"] == []
    advice = advise(
        ctx
    )  # the file's own unique column and two-valued columns are asked about as they always were
    assert advice.primary_key is not None
    assert not [p for p in advice.proposals if p.from_headers]
