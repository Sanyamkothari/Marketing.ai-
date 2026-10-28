"""Plan O M242 (DEC-1812): currency - money keeps its unit, foreign money is asked, mixed money stops, rates are a
person's and approved, and nothing here ever looks a rate up."""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from engine.agent.config import AgentLevel
from engine.agent.contracts import DecidedBy, RecipeStepKind
from engine.agent.recipe import RecipeError, run_recipe
from engine.doctor import currency as currency_module
from engine.doctor.config import DataDoctorConfig
from engine.doctor.currency import (
    APPROVAL_SENTENCE_CHANGED,
    CURRENCY_MIXED,
    CURRENCY_RATE_INVALID,
    CURRENCY_RATE_UNAPPROVED,
    Amount,
    CurrencyError,
    add_amounts,
    answer_mixed,
    approve_conversion,
    check_currency,
    format_money,
    load_money_basis,
    money_basis,
    money_basis_for_campaign,
    propose_conversion,
    read_currencies,
    report_in,
    report_money,
    save_money_basis,
    suggest_spellings,
    workspace_currency_for,
)
from engine.doctor.locale import confirm_profile, india_defaults
from engine.storage import LocalStorage

TODAY = date(2026, 10, 12)
AT = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)


def _run(frame: pd.DataFrame, steps: tuple[object, ...]) -> pd.DataFrame:
    return run_recipe(
        frame,
        list(steps),  # type: ignore[arg-type]
        upload_id="u",
        primary_key="id",
        target=None,
        levels=[AgentLevel.CLEAN],
        max_failure_pct=5,
    ).frame


def dollar_file() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": ["c1", "c2", "c3", "c4"],
            "bill_amount": ["$1,200", "$15.50", "$3", "$40"],
            "plan": ["Gold", "Basic", "Basic", "Gold"],
        }
    )


def mixed_file() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": ["c1", "c2", "c3", "c4"],
            "amount": ["₹1,200", "$15", "₹300", "$2"],
            "country": ["IN", "US", "IN", "US"],
        }
    )


# ---------------------------------------------------------------------------
# The workspace currency
# ---------------------------------------------------------------------------
def test_the_workspace_currency_is_rupees_unless_set() -> None:
    assert workspace_currency_for(DataDoctorConfig()).code == "INR"
    assert workspace_currency_for(None).model_dump() == {"code": "INR", "source": "default"}
    assert workspace_currency_for(DataDoctorConfig(workspace_currency="USD")).model_dump() == {
        "code": "USD",
        "source": "workspace",
    }


def test_a_clients_confirmed_currency_wins_and_a_detected_one_does_not(tmp_path: Path) -> None:
    usd = DataDoctorConfig(workspace_currency="USD")
    detected = india_defaults("acme", workspace_currency="EUR")
    assert workspace_currency_for(usd, detected).code == "USD", "a default or detected profile never wins"
    confirmed = confirm_profile(
        LocalStorage(tmp_path), detected, client_id="acme", confirmed_by="asha", clock=lambda: AT
    )
    assert workspace_currency_for(usd, confirmed).model_dump() == {"code": "EUR", "source": "client"}


def test_the_workspace_setting_is_a_code_and_rupees_leave_no_trace_in_a_dump() -> None:
    assert DataDoctorConfig().model_dump() == {}
    assert DataDoctorConfig(workspace_currency="INR").model_dump() == {}
    assert DataDoctorConfig(workspace_currency="USD").model_dump()["workspace_currency"] == "USD"
    for bad in ("usd", "dollars", "US$", ""):
        with pytest.raises(ValueError):
            DataDoctorConfig(workspace_currency=bad)


# ---------------------------------------------------------------------------
# Acceptance 1: a dollar file in a rupee workspace asks; "report in dollars" shows dollars, labelled
# ---------------------------------------------------------------------------
def test_a_dollar_file_in_a_rupee_workspace_asks_one_question() -> None:
    check = check_currency(dollar_file(), workspace_currency="INR", primary_key="id")
    assert not check.stopped and check.units == {}
    (finding,) = check.findings
    assert (finding.code, finding.severity, finding.column, finding.currencies) == (
        "CURRENCY_FOREIGN",
        "ask",
        "bill_amount",
        ("USD",),
    )
    assert finding.question.text == (
        "This file's 'bill_amount' is in US dollars (4 amounts), and this workspace reports in Indian rupees. "
        "Report in US dollars, or convert to Indian rupees at a rate you give?"
    )
    assert [o.key for o in finding.question.options] == ["report_in:USD", "convert:USD:INR"]
    assert not finding.question.blocking


def test_report_in_dollars_records_the_unit_and_converts_nothing() -> None:
    frame = dollar_file()
    (finding,) = check_currency(frame, workspace_currency="INR", primary_key="id").findings
    steps = report_in(finding, "USD")
    assert [(s.kind, s.params) for s in steps] == [(RecipeStepKind.PARSE_NUMBER, {"unit": "USD"})]
    out = _run(frame, steps)
    assert out["bill_amount"].tolist() == [1200.0, 15.5, 3.0, 40.0]
    basis = money_basis(steps, workspace_currency="INR")
    assert basis.report_currency == "USD"
    money = report_money(basis)
    assert money is not None and money.name == "US dollars" and money.format(1200) == "$1,200"
    assert money.notes == ("'bill_amount' is in US dollars, as the file gives it; nothing was converted.",)


def test_a_dollar_file_in_a_dollar_workspace_needs_no_question() -> None:
    check = check_currency(dollar_file(), workspace_currency="USD", primary_key="id")
    assert check.findings == () and check.units == {"bill_amount": "USD"}


def test_a_rupee_file_in_a_rupee_workspace_needs_no_question() -> None:
    frame = pd.DataFrame({"id": [1, 2], "amount": ["₹1,200", "Rs 3,50,000/-"]})
    check = check_currency(frame, workspace_currency="INR", primary_key="id")
    assert check.findings == () and check.units == {"amount": "INR"}


def test_a_header_word_names_the_currency_of_plain_numbers() -> None:
    frame = pd.DataFrame({"id": [1, 2], "spend_usd": [10.0, 20.0], "Price (INR)": ["1,200", "300"]})
    check = check_currency(frame, workspace_currency="INR", primary_key="id")
    assert check.units == {"Price (INR)": "INR"}
    assert [(f.code, f.column) for f in check.findings] == [("CURRENCY_FOREIGN", "spend_usd")]


def test_no_unit_is_invented_for_money_that_names_none() -> None:
    frame = pd.DataFrame({"id": [1, 2], "spend": [10.0, 20.0], "visits": [3, 4]})
    check = check_currency(frame, workspace_currency="INR", primary_key="id")
    (finding,) = check.findings
    assert (finding.code, finding.column, finding.severity) == ("CURRENCY_UNSTATED", "spend", "ask")
    assert "Is it in Indian rupees? We ask once" in finding.question.text
    assert check.units == {}, "visits is not money; spend waits for the person"
    confirmed = check_currency(
        frame, workspace_currency="INR", confirmed_units={"spend": "INR"}, primary_key="id"
    )
    assert confirmed.findings == () and confirmed.units == {"spend": "INR"}


def test_the_id_and_the_outcome_are_never_read() -> None:
    frame = pd.DataFrame({"id": ["$1", "$2"], "revenue": ["$5", "$6"], "amount": ["₹1", "₹2"]})
    assert [c.column for c in read_currencies(frame, primary_key="id", target="revenue")] == ["amount"]


def test_counts_only_never_a_cell_value() -> None:
    check = check_currency(mixed_file(), workspace_currency="INR", primary_key="id")
    blob = check.model_dump_json()
    for cell in ("1,200", "₹300", "$15"):
        assert cell not in blob


# ---------------------------------------------------------------------------
# Acceptance 2: an approved conversion records rate, date and source
# ---------------------------------------------------------------------------
def _proposal(**change: object) -> object:
    kwargs: dict[str, object] = {
        "source_code": "USD",
        "target": "INR",
        "rate": 83.1,
        "rate_date": "2026-10-10",
        "source": "RBI reference rate, 10 Oct 2026",
        "today": TODAY,
    }
    kwargs.update(change)
    return propose_conversion("bill_amount", **kwargs)  # type: ignore[arg-type]


def test_a_conversion_is_proposed_with_the_exact_sentence_and_pending_steps() -> None:
    proposal = propose_conversion(
        "bill_amount",
        source_code="USD",
        target="INR",
        rate=83.1,
        rate_date="2026-10-10",
        source="RBI reference rate, 10 Oct 2026",
        today=TODAY,
    )
    assert proposal.sentence == (
        "In 'bill_amount', convert every amount from US dollars to Indian rupees at 1 US dollar = 83.1 Indian "
        "rupees, the rate of 10 Oct 2026, from: RBI reference rate, 10 Oct 2026. Every converted amount changes, "
        "and every report that uses them shows this rate."
    )
    convert, parse = proposal.steps
    assert convert.kind is RecipeStepKind.CONVERT_CURRENCY and convert.decided_by is DecidedBy.USER
    assert convert.params == {
        "from": "USD",
        "to": "INR",
        "rate": 83.1,
        "rate_date": "2026-10-10",
        "source": "RBI reference rate, 10 Oct 2026",
    }
    assert parse.kind is RecipeStepKind.PARSE_NUMBER and parse.params == {"unit": "INR"} and parse.order == 2
    out = _run(dollar_file(), proposal.steps)
    assert out["bill_amount"].tolist() == [99720.0, 1288.05, 249.3, 3324.0]


def test_an_approved_conversion_records_rate_date_source_and_who() -> None:
    proposal = propose_conversion(
        "bill_amount",
        source_code="USD",
        target="INR",
        rate=83.1,
        rate_date=date(2026, 10, 10),
        source="RBI reference rate",
        today=TODAY,
    )
    approved = approve_conversion(proposal, sentence=proposal.sentence, approved_by="asha", clock=lambda: AT)
    assert (approved.from_currency, approved.to_currency, approved.rate) == ("USD", "INR", 83.1)
    assert approved.rate_date == date(2026, 10, 10) and approved.source == "RBI reference rate"
    assert (approved.approved_by, approved.approved_at, approved.sentence) == ("asha", AT, proposal.sentence)
    basis = money_basis(proposal.steps, workspace_currency="INR", rates=[approved])
    (column,) = basis.columns
    assert (column.written_in, column.reported_in) == (("USD",), "INR")
    assert basis.notes() == (
        "'bill_amount': every amount converted from US dollars to Indian rupees at 1 US dollar = 83.1 Indian "
        "rupees, the rate of 10 Oct 2026 (source: RBI reference rate), approved by asha on 12 Oct 2026.",
    )
    money = report_money(basis)
    assert money is not None and money.currency == "INR" and money.notes == basis.notes()


def test_a_changed_sentence_records_nothing() -> None:
    proposal = propose_conversion(
        "bill_amount",
        source_code="USD",
        target="INR",
        rate=83.1,
        rate_date="2026-10-10",
        source="RBI",
        today=TODAY,
    )
    with pytest.raises(CurrencyError) as refused:
        approve_conversion(proposal, sentence=proposal.sentence.replace("83.1", "8.31"), approved_by="asha")
    assert refused.value.code == APPROVAL_SENTENCE_CHANGED
    with pytest.raises(CurrencyError) as nobody:
        approve_conversion(proposal, sentence=proposal.sentence, approved_by="  ")
    assert nobody.value.code == CURRENCY_RATE_UNAPPROVED


def test_a_conversion_nobody_approved_never_reaches_a_report() -> None:
    proposal = propose_conversion(
        "bill_amount",
        source_code="USD",
        target="INR",
        rate=83.1,
        rate_date="2026-10-10",
        source="RBI",
        today=TODAY,
    )
    with pytest.raises(CurrencyError) as refused:
        money_basis(proposal.steps, workspace_currency="INR")
    assert refused.value.code == CURRENCY_RATE_UNAPPROVED
    other = propose_conversion(
        "bill_amount",
        source_code="USD",
        target="INR",
        rate=84.0,
        rate_date="2026-10-10",
        source="RBI",
        today=TODAY,
    )
    approved_other = approve_conversion(other, sentence=other.sentence, approved_by="asha")
    with pytest.raises(CurrencyError):
        money_basis(proposal.steps, workspace_currency="INR", rates=[approved_other])


@pytest.mark.parametrize(
    ("change", "words"),
    [
        ({"rate": 0}, "above 0"),
        ({"rate": -1.0}, "above 0"),
        ({"rate": float("nan")}, "is a number"),
        ({"rate": True}, "is a number"),
        ({"rate_date": "2026-10-13"}, "in the future"),
        ({"rate_date": "yesterday"}, "not a date"),
        ({"source": " "}, "where the rate comes from"),
        ({"target": "USD"}, "the same"),
        ({"source_code": "dollar"}, "by their code"),
        ({"rows": "some"}, "rows is all"),
    ],
)
def test_a_rate_that_cannot_be_used_is_refused_with_the_reason(change: dict[str, object], words: str) -> None:
    with pytest.raises(CurrencyError) as refused:
        _proposal(**change)
    assert refused.value.code == CURRENCY_RATE_INVALID
    assert words in refused.value.message


def test_nothing_here_fetches_a_rate() -> None:
    """[REQ] 7: never fetch an exchange rate from the internet. The modules import no network code at all."""
    network = {"urllib", "http", "requests", "httpx", "socket", "aiohttp", "boto3", "botocore", "engine.llm"}
    for module in (
        currency_module.__file__,
        currency_module.__file__.replace("doctor/currency.py", "agent/currency_steps.py"),
    ):
        tree = ast.parse(Path(module).read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0] if not alias.name.startswith("engine.") else alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (
                (node.module or "")
                if (node.module or "").startswith("engine.")
                else (node.module or "").split(".")[0]
            )
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not imported & network, (module, imported & network)


# ---------------------------------------------------------------------------
# Acceptance 3: a column holding ₹ and $ stops and is never summed
# ---------------------------------------------------------------------------
def test_a_column_holding_rupees_and_dollars_stops_with_one_question() -> None:
    check = check_currency(mixed_file(), workspace_currency="INR", primary_key="id")
    assert check.stopped
    (finding,) = check.findings
    assert (finding.code, finding.severity, finding.currencies) == ("CURRENCY_MIXED", "stop", ("INR", "USD"))
    assert finding.rows == {"INR": 2, "USD": 2}
    assert finding.candidates == ("country",)
    assert finding.question.blocking
    assert finding.question.text.startswith(
        "'amount' holds more than one currency (2 rows in Indian rupees; 2 rows in US dollars). Amounts in "
        "different currencies are never added together"
    )
    keys = [o.key for o in finding.question.options]
    assert keys == ["rows_by_column:country", "rows_by_sign", "new_file"]
    assert not any(
        "add" in o.label.casefold() or "sum" in o.label.casefold() for o in finding.question.options
    )
    with pytest.raises(CurrencyError) as refused:
        report_in(finding, "INR")
    assert refused.value.code == CURRENCY_MIXED


def test_the_mixed_column_is_never_summed() -> None:
    frame = mixed_file()
    (finding,) = check_currency(frame, workspace_currency="INR", primary_key="id").findings
    with pytest.raises(RecipeError) as stopped:  # reading it in rupees stops: the dollars are not rupees
        _run(frame, (report_in(finding.model_copy(update={"code": "CURRENCY_FOREIGN"}), "INR")[0],))
    assert stopped.value.code == CURRENCY_MIXED
    with pytest.raises(CurrencyError) as refused:
        add_amounts([Amount(value=1200, currency="INR"), Amount(value=15, currency="USD")])
    assert refused.value.code == CURRENCY_MIXED
    assert "never added together" in refused.value.message
    assert add_amounts([Amount(value=1, currency="USD"), Amount(value=2.5, currency="USD")]) == Amount(
        value=3.5, currency="USD"
    )


def test_the_answer_which_column_says_which_rows_converts_only_those_rows() -> None:
    frame = mixed_file()
    (finding,) = check_currency(frame, workspace_currency="INR", primary_key="id").findings
    spellings = suggest_spellings(frame, "country")
    assert spellings == {"INR": ["IN"], "USD": ["US"]}
    answer = answer_mixed(finding, workspace_currency="INR", rows_column="country", spellings=spellings)
    assert answer.to_convert == ("USD",) and answer.keep == "INR"
    proposal = propose_conversion(
        "amount",
        source_code="USD",
        target="INR",
        rate=83.1,
        rate_date="2026-10-10",
        source="RBI",
        rows=answer.rows["USD"],
        today=TODAY,
    )
    assert proposal.sentence.startswith("In 'amount', convert the rows whose 'country' is US from US dollars")
    out = _run(frame, proposal.steps)
    assert out["amount"].tolist() == [1200.0, 1246.5, 300.0, 166.2]
    approved = approve_conversion(proposal, sentence=proposal.sentence, approved_by="asha", clock=lambda: AT)
    basis = money_basis(proposal.steps, workspace_currency="INR", rates=[approved])
    assert basis.columns[0].written_in == ("INR", "USD") and basis.columns[0].reported_in == "INR"
    assert "the rows whose 'country' is US converted from US dollars" in basis.notes()[0]


def test_the_answer_the_signs_say_it_converts_the_marked_amounts() -> None:
    frame = mixed_file().drop(columns=["country"])
    (finding,) = check_currency(frame, workspace_currency="INR", primary_key="id").findings
    answer = answer_mixed(finding, workspace_currency="INR", by_sign=True)
    assert answer.rows == {"INR": "marked", "USD": "marked"}
    proposal = propose_conversion(
        "amount",
        source_code="USD",
        target="INR",
        rate=83.1,
        rate_date="2026-10-10",
        source="RBI",
        rows=answer.rows["USD"],
        today=TODAY,
    )
    assert _run(frame, proposal.steps)["amount"].tolist() == [1200.0, 1246.5, 300.0, 166.2]


def test_signs_cannot_answer_when_some_amounts_carry_none() -> None:
    frame = pd.DataFrame({"id": [1, 2, 3], "amount": ["₹1,200", "$15", "300"]})
    (finding,) = check_currency(frame, workspace_currency="INR", primary_key="id").findings
    assert "rows_by_sign" not in [o.key for o in finding.question.options]
    with pytest.raises(CurrencyError, match="carry no single sign"):
        answer_mixed(finding, workspace_currency="INR", by_sign=True)
    with pytest.raises(CurrencyError, match="is not a column"):
        answer_mixed(finding, workspace_currency="INR", rows_column="id", spellings={"USD": ["x"]})


def test_a_spelling_cannot_mean_two_currencies() -> None:
    (finding,) = check_currency(mixed_file(), workspace_currency="INR", primary_key="id").findings
    with pytest.raises(CurrencyError, match="cannot mean two"):
        answer_mixed(
            finding,
            workspace_currency="INR",
            rows_column="country",
            spellings={"USD": ["US"], "INR": ["IN", "us"]},
        )
    with pytest.raises(CurrencyError, match="Say which values"):
        answer_mixed(finding, workspace_currency="INR", rows_column="country", spellings={"USD": ["US"]})


def test_values_that_contradict_a_confirmed_unit_are_mixed_not_overwritten() -> None:
    check = check_currency(dollar_file(), workspace_currency="INR", confirmed_units={"bill_amount": "INR"})
    (finding,) = check.findings
    assert finding.code == "CURRENCY_MIXED" and finding.currencies == ("INR", "USD")


def test_a_value_naming_two_currencies_is_mixed() -> None:
    frame = pd.DataFrame({"id": [1, 2], "amount": ["$12 (₹1,000)", "$5"]})
    (finding,) = check_currency(frame, workspace_currency="USD", primary_key="id").findings
    assert finding.code == "CURRENCY_MIXED" and finding.several == 1


# ---------------------------------------------------------------------------
# Acceptance 4: an INR workspace prints exactly as before
# ---------------------------------------------------------------------------
def test_an_inr_basis_with_nothing_converted_changes_no_report() -> None:
    frame = pd.DataFrame({"id": [1, 2], "amount": ["₹1,200", "₹5"]})
    check = check_currency(frame, workspace_currency="INR", primary_key="id")
    basis = money_basis((), workspace_currency="INR", units=check.units)
    assert basis.report_currency == "INR"
    assert report_money(basis) is None
    assert report_money(None) is None


def test_format_money_is_format_inr_for_rupees() -> None:
    from engine.pilot.roi import format_inr

    for amount in (0, 45.5, 1234567, -250000, 99_999_999):
        assert format_money(amount, "INR") == format_inr(amount)
        assert format_money(amount, "INR", decimals=2) == format_inr(amount, decimals=2)
    assert format_money(1234567.4, "USD") == "$1,234,567"
    assert format_money(-12.5, "EUR", decimals=2) == "-€12.50"
    assert format_money(1200, "AED") == "AED 1,200"
    assert format_money(999, "GBP") == "£999"


# ---------------------------------------------------------------------------
# The basis beside the run
# ---------------------------------------------------------------------------
def test_the_basis_is_stored_beside_the_run_and_read_back(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    proposal = propose_conversion(
        "bill_amount",
        source_code="USD",
        target="INR",
        rate=83.1,
        rate_date="2026-10-10",
        source="RBI",
        today=TODAY,
    )
    approved = approve_conversion(proposal, sentence=proposal.sentence, approved_by="asha", clock=lambda: AT)
    basis = money_basis(proposal.steps, workspace_currency="INR", rates=[approved])
    assert load_money_basis(storage, "r_1") is None
    save_money_basis(storage, "r_1", basis)
    assert storage.exists("runs/r_1/money_basis.json")
    assert load_money_basis(storage, "r_1") == basis
    assert load_money_basis(storage, "../etc") is None
    storage.write_text("campaigns/cmp_1/campaign.json", '{"run_ids": ["r_1"]}')
    assert money_basis_for_campaign(storage, "cmp_1") == basis
    assert money_basis_for_campaign(storage, "cmp_2") is None


def test_the_basis_hash_follows_units_and_rates_not_the_approver() -> None:
    proposal = propose_conversion(
        "bill_amount",
        source_code="USD",
        target="INR",
        rate=83.1,
        rate_date="2026-10-10",
        source="RBI",
        today=TODAY,
    )
    by_asha = approve_conversion(proposal, sentence=proposal.sentence, approved_by="asha")
    by_ravi = approve_conversion(proposal, sentence=proposal.sentence, approved_by="ravi")
    first = money_basis(proposal.steps, workspace_currency="INR", rates=[by_asha])
    second = money_basis(proposal.steps, workspace_currency="INR", rates=[by_ravi])
    assert first.basis_hash == second.basis_hash
    reported = money_basis(
        report_in(check_currency(dollar_file()).findings[0], "USD"), workspace_currency="INR"
    )
    assert reported.basis_hash != first.basis_hash


def test_two_columns_in_two_currencies_are_shown_apart() -> None:
    from engine.agent.contracts import RecipeStep

    steps = (
        RecipeStep(order=1, kind=RecipeStepKind.PARSE_NUMBER, column="a", params={"unit": "USD"}),
        RecipeStep(order=2, kind=RecipeStepKind.PARSE_NUMBER, column="b", params={"unit": "INR"}),
    )
    basis = money_basis(steps, workspace_currency="INR")
    assert basis.report_currency == "INR"
    assert (
        basis.notes()[-1]
        == "Amounts in Indian rupees and US dollars are shown apart and never added together."
    )
    assert report_money(basis) is not None
