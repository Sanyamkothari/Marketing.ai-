"""Safe mapping: nothing ambiguous in a client's file is read one way without a word.

Each test is one messy-file case from docs/v2/real_data/D-domains-messy.md, on a few inline rows: the
mapping either settles it from evidence in the same file, from an explicit option, or reports the
rows (library path) / asks a plain question (`ask=True`, the Data Doctor's route).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from autonomy.contracts import EventType
from autonomy.data.folder import StateFolder
from autonomy.data.mapping import (
    EventMapping,
    MappingQuestionsError,
    ProblemCode,
    QuestionCode,
    customer_key,
    inspect_frame,
    map_frame,
    problem_counts,
)
from autonomy.data.rebuild import MixedCurrencyError, rebuild_states
from autonomy.fakes import InMemoryEventStore, InMemoryStateStore


def _orders(**extra: object) -> EventMapping:
    base: dict[str, object] = {
        "customer_id": "cid",
        "timestamp": "at",
        "timezone": "Europe/London",
        "event_type": EventType.PURCHASE,
        "value_column": "amount",
    }
    return EventMapping.model_validate(base | extra)


def _codes(frame: pd.DataFrame, mapping: EventMapping) -> list[str]:
    return [q.code for q in inspect_frame(frame, mapping).questions]


# --- ids ----------------------------------------------------------------------------------------------
def test_placeholder_and_totals_ids_are_left_out_not_merged() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "0", "unknown", "Guest", "-", "TOTAL", "000", "b"],
            "at": ["2024-01-01T10:00:00"] * 8,
            "amount": ["1", "2", "3", "4", "5", "15", "6", "7"],
        }
    )
    events, problems = map_frame(frame, _orders(), "csv")
    assert [e.customer_id for e in events] == ["a", "b"]
    assert problem_counts(problems) == {ProblemCode.PLACEHOLDER_CUSTOMER: 6}
    assert all("unknown" not in p.message.split("(")[0] for p in problems)
    # A business whose real ids include "0" says so: an empty list turns the check off.
    kept, _ = map_frame(frame.iloc[:2], _orders(id_placeholders=()), "csv")
    assert [e.customer_id for e in kept] == ["a", "0"]


def test_scientific_notation_ids_are_refused_and_emails_fold_case() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["1.23457E+11", "1.23457E+11", "Ann@Example.com", "ann@example.com", "12345e6789"],
            "at": ["2024-01-01T10:00:00"] * 5,
            "amount": ["1"] * 5,
        }
    )
    events, problems = map_frame(frame, _orders(), "csv")
    assert problem_counts(problems) == {ProblemCode.BAD_CUSTOMER_ID: 2}
    # The two spellings of one e-mail are one customer; a hex id that looks like 1e6 is left alone.
    assert [e.customer_id for e in events] == ["ann@example.com", "ann@example.com", "12345e6789"]
    assert customer_key("1.23457E+11") is None and customer_key("Guest") is None
    assert customer_key(" 007 ") == "007"


def test_leading_zeros_are_asked_and_strip_matches_a_file_that_lost_them() -> None:
    orders = pd.DataFrame(
        {"cid": ["0012431", "0012431"], "at": ["2024-01-01T10:00:00"] * 2, "amount": [1, 2]}
    )
    with pytest.raises(MappingQuestionsError) as asked:
        map_frame(orders, _orders(), "csv", ask=True)
    assert [q.code for q in asked.value.questions] == [QuestionCode.LEADING_ZEROS]
    strip = _orders(id_leading_zeros="strip")
    events, _ = map_frame(orders, strip, "csv", ask=True)
    consent = pd.DataFrame({"cid": [12431], "at": ["2024-01-01T09:00:00"], "amount": [0]})  # pandas' int
    other, _ = map_frame(consent, strip, "consent.csv")
    assert {e.customer_id for e in events} == {e.customer_id for e in other} == {"12431"}


def test_two_spellings_of_one_number_are_reported_without_an_answer() -> None:
    frame = pd.DataFrame({"cid": ["007", "7", "8"], "at": ["2024-01-01T10:00:00"] * 3, "amount": [1, 1, 1]})
    events, problems = map_frame(frame, _orders(), "csv")
    assert [e.customer_id for e in events] == ["8"]
    assert problem_counts(problems) == {ProblemCode.BAD_CUSTOMER_ID: 2}


def test_one_id_on_most_rows_is_questioned() -> None:
    cids = ["walkin"] * 60 + [f"c{i}" for i in range(20)]
    frame = pd.DataFrame({"cid": cids, "at": [f"2024-01-{i % 28 + 1:02d}T10:00:00" for i in range(80)]})
    frame["amount"] = range(1, 81)
    assert _codes(frame, _orders()) == [QuestionCode.HEAVY_ID]
    assert _codes(frame, _orders(allow_heavy_ids=("walkin",))) == []


# --- event ids and repeated lines ------------------------------------------------------------------
def test_reexport_with_new_rows_first_neither_drops_nor_doubles() -> None:
    m = _orders()
    old = pd.DataFrame({"cid": ["a", "b", "c"], "at": ["2024-01-01T10:00:00"] * 3, "amount": ["1", "2", "3"]})
    new = pd.DataFrame({"cid": ["d"], "at": ["2024-01-08T10:00:00"], "amount": ["40"]})
    store = InMemoryEventStore()
    store.append(map_frame(old, m, "orders.csv")[0])
    store.append(map_frame(pd.concat([new, old], ignore_index=True), m, "orders.csv")[0])
    assert sorted((e.customer_id, e.value) for e in store.scan()) == [
        ("a", 1.0),
        ("b", 2.0),
        ("c", 3.0),
        ("d", 40.0),
    ]


def test_exact_repeated_lines_are_asked_kept_or_dropped() -> None:
    frame = pd.DataFrame({"cid": ["a", "a", "b"], "at": ["2024-01-01T10:00:00"] * 3, "amount": [5, 5, 6]})
    with pytest.raises(MappingQuestionsError, match="exact copies"):
        map_frame(frame, _orders(), "csv", ask=True)
    kept, _ = map_frame(frame, _orders(duplicates="keep"), "csv", ask=True)
    assert len(kept) == 3 and len({e.event_id for e in kept}) == 3  # two real lines, two ids
    dropped, problems = map_frame(frame, _orders(duplicates="drop"), "csv", ask=True)
    assert len(dropped) == 2 and problem_counts(problems) == {ProblemCode.DUPLICATE_ROW: 1}
    # The library path keeps them (the old default), each with its own content id.
    library, _ = map_frame(frame, _orders(), "csv")
    assert [e.event_id for e in library] == [e.event_id for e in kept]


# --- dates ----------------------------------------------------------------------------------------------
_US = "%m/%d/%Y %H:%M"


def test_day_first_file_under_a_month_first_format_is_never_read_silently() -> None:
    at = ["13/01/2024 10:00", "01/02/2024 10:00", "05/02/2024 11:00"]  # the first can only be day first
    frame = pd.DataFrame({"cid": ["a", "b", "c"], "at": at, "amount": [1, 2, 3]})
    m = _orders(timestamp_format=_US)
    events, problems = map_frame(frame, m, "csv")
    assert events == []
    assert problem_counts(problems) == {ProblemCode.BAD_TIMESTAMP: 1, ProblemCode.AMBIGUOUS_DATE: 2}
    with pytest.raises(MappingQuestionsError) as asked:
        map_frame(frame, m, "csv", ask=True)
    (question,) = asked.value.questions
    assert (question.code, question.option, question.confidence) == ("date_order", "date_order", 1.0)
    fixed, problems = map_frame(
        frame, _orders(timestamp_format="%d/%m/%Y %H:%M", date_order="day_first"), "csv"
    )
    assert not problems and fixed[1].ts.month == 2 and fixed[1].ts.day == 1


def test_a_single_ambiguous_date_is_asked_and_a_dense_one_is_settled() -> None:
    one_day = pd.DataFrame({"cid": ["a", "b"], "at": ["01/12/2010 08:26"] * 2, "amount": [1, 2]})
    assert _codes(one_day, _orders(timestamp_format=_US)) == [QuestionCode.DATE_ORDER]
    assert _codes(one_day, _orders(timestamp_format=_US, date_order="month_first")) == []
    # Nine consecutive December days written day first: read month first they scatter over the year.
    nine = pd.DataFrame(
        {"cid": list("abcdefghi"), "at": [f"0{d}/12/2010 09:00" for d in range(1, 10)], "amount": [1] * 9}
    )
    q = inspect_frame(nine, _orders(timestamp_format=_US)).questions
    assert [x.code for x in q] == [QuestionCode.DATE_ORDER] and (q[0].confidence or 0) > 0.9
    assert _codes(nine, _orders(timestamp_format="%d/%m/%Y %H:%M")) == []


def test_date_order_and_format_must_agree_and_date_order_alone_reads_common_shapes() -> None:
    with pytest.raises(ValueError, match="date_order"):
        _orders(timestamp_format=_US, date_order="day_first")
    frame = pd.DataFrame(
        {"cid": ["a", "b"], "at": ["03/04/2024 10:00", "2024-04-05T10:00:00+00:00"], "amount": [1, 2]}
    )
    events, problems = map_frame(frame, _orders(date_order="day_first"), "csv")
    assert not problems and events[0].ts.month == 4 and events[1].ts.day == 5


def test_placeholder_dates_and_dates_after_the_export_are_reported() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "b", "c"],
            "at": ["1900-01-01T00:00:00", "1970-01-01T00:00:00", "1970-01-01T09:30:00"],
            "amount": [1, 2, 3],
        }
    )
    events, problems = map_frame(frame, _orders(), "csv")
    assert [e.customer_id for e in events] == ["c"]  # a real time on that day is kept
    assert problem_counts(problems) == {ProblemCode.BAD_TIMESTAMP: 2}
    assert all("placeholder date" in p.message for p in problems)

    short = pd.DataFrame({"cid": ["a", "b"], "at": ["12/01/68 08:00", "06/01/24 12:00"], "amount": [1, 2]})
    m = _orders(timestamp_format="%m/%d/%y %H:%M", not_after=datetime(2026, 10, 1, tzinfo=UTC))
    events, problems = map_frame(short, m, "csv")
    assert [e.customer_id for e in events] == ["b"]  # 68 is read as 2068, after the export: reported
    assert [p.code for p in problems] == [ProblemCode.BAD_TIMESTAMP] and "two-digit year" in problems[
        0
    ].message
    early = _orders(not_before=datetime(2000, 1, 1, tzinfo=UTC))
    assert problem_counts(map_frame(frame.iloc[2:], early, "csv")[1]) == {ProblemCode.BAD_TIMESTAMP: 1}


def test_naive_times_without_a_zone_are_asked_never_read_as_utc() -> None:
    frame = pd.DataFrame({"cid": ["a"], "at": ["2024-01-01T10:00:00"], "amount": [1]})
    unset = EventMapping(
        customer_id="cid", timestamp="at", event_type=EventType.PURCHASE, value_column="amount"
    )
    with pytest.raises(MappingQuestionsError) as asked:
        map_frame(frame, unset, "csv", ask=True)
    assert [q.code for q in asked.value.questions] == [QuestionCode.TIMEZONE]
    events, _ = map_frame(frame, _orders(timezone="America/New_York"), "csv", ask=True)
    assert events[0].ts == datetime(2024, 1, 1, 15, tzinfo=UTC)
    offset = pd.DataFrame({"cid": ["a"], "at": ["2024-01-01T10:00:00+02:00"], "amount": [1]})
    assert (
        map_frame(offset, unset, "csv", ask=True)[0][0].ts.astimezone(UTC).hour == 8
    )  # an offset needs no zone


# --- numbers and money -------------------------------------------------------------------------------
def test_decimal_comma_is_settled_by_the_file_and_three_digit_groups_are_not_guessed() -> None:
    comma = pd.DataFrame(
        {"cid": ["a", "b", "c"], "at": ["2024-01-01T10:00:00"] * 3, "amount": ["2,55", "1.234,50", "7"]}
    )
    events, problems = map_frame(comma, _orders(), "csv")
    assert not problems and [e.value for e in events] == [2.55, 1234.5, 7.0]
    assert "decimal comma" in inspect_frame(comma, _orders()).inferred[0]

    dots = pd.DataFrame({"cid": ["a", "b"], "at": ["2024-01-01T10:00:00"] * 2, "amount": ["2.550", "350"]})
    events, problems = map_frame(dots, _orders(), "csv")
    assert [e.value for e in events] == [350.0]
    assert problem_counts(problems) == {ProblemCode.AMBIGUOUS_NUMBER: 1}
    assert _codes(dots, _orders()) == [QuestionCode.SEPARATORS]
    thousands, _ = map_frame(dots, _orders(decimal=","), "csv", ask=True)
    assert [e.value for e in thousands] == [2550.0, 350.0]
    point, _ = map_frame(dots, _orders(decimal="."), "csv", ask=True)
    assert [e.value for e in point] == [2.55, 350.0]
    # One cell that only reads with a decimal point settles the whole file.
    settled = pd.concat([dots, dots.iloc[:1].assign(amount="4.25")], ignore_index=True)
    assert [e.value for e in map_frame(settled, _orders(), "csv")[0]] == [2.55, 350.0, 4.25]


def test_a_negative_purchase_needs_negative_as() -> None:
    frame = pd.DataFrame({"cid": ["a", "b"], "at": ["2024-01-01T10:00:00"] * 2, "amount": ["-3.5", "4"]})
    events, problems = map_frame(frame, _orders(), "csv")
    assert [e.value for e in events] == [4.0] and problem_counts(problems) == {ProblemCode.BAD_NUMBER: 1}
    refunds, _ = map_frame(frame, _orders(negative_as=EventType.REFUND), "csv")
    assert [(e.type, e.value) for e in refunds] == [(EventType.REFUND, 3.5), (EventType.PURCHASE, 4.0)]


def test_two_currencies_are_never_added() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "b", "c"],
            "at": ["2024-01-01T10:00:00"] * 3,
            "amount": ["10", "100", "20"],
            "cur": ["GBP", "brl", "GBP"],
        }
    )
    m = _orders(attrs={"currency": "cur"})
    assert _codes(frame, m) == [QuestionCode.CURRENCY]
    events, problems = map_frame(frame, m, "csv")
    assert [e.value for e in events] == [10.0, 20.0]
    assert problem_counts(problems) == {ProblemCode.MIXED_CURRENCY: 1}
    rated = _orders(attrs={"currency": "cur"}, currency="GBP", currency_rates={"BRL": 0.2})
    converted, problems = map_frame(frame, rated, "csv", ask=True)
    assert not problems and [e.value for e in converted] == [10.0, 20.0, 20.0]
    assert converted[1].attrs == {
        "timezone": "Europe/London",
        "currency": "GBP",
        "original_currency": "BRL",
        "original_value": 100.0,
    }
    with pytest.raises(ValueError, match="currency_rates needs currency"):
        _orders(currency_rates={"BRL": 0.2})


def test_rebuild_refuses_to_add_two_files_in_different_currencies() -> None:
    m = _orders(attrs={"currency": "cur"})
    uk = pd.DataFrame({"cid": ["a"], "at": ["2024-01-01T10:00:00"], "amount": ["10"], "cur": ["GBP"]})
    br = pd.DataFrame({"cid": ["b"], "at": ["2024-01-01T10:00:00"], "amount": ["100"], "cur": ["BRL"]})
    store = InMemoryEventStore()
    store.append(map_frame(uk, m, "uk.csv")[0])
    states = InMemoryStateStore()
    now = datetime(2024, 2, 1, tzinfo=UTC)
    assert rebuild_states(store, states, StateFolder(), now) == 1
    store.append(map_frame(br, m, "br.csv")[0])
    with pytest.raises(MixedCurrencyError, match="BRL"):
        rebuild_states(store, states, StateFolder(), now)
    assert [s.customer_id for s in states.scan()] == ["a"]  # nothing written by the refused rebuild


# --- big files: the inspection samples, the rows are still checked one by one ----------------------
def test_ambiguous_dates_beyond_the_inspection_sample_are_still_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autonomy.data.mapping as mapping_module

    monkeypatch.setattr(mapping_module, "INSPECT_SAMPLE", 2)
    # Day-first text under a month-first format: the first row proves it, later ones read both ways.
    at = ["13/01/2024 10:00", "14/01/2024 10:00"] + [f"0{d}/02/2024 10:00" for d in range(3, 10)]
    frame = pd.DataFrame({"cid": [f"c{i}" for i in range(len(at))], "at": at, "amount": [1] * len(at)})
    events, problems = map_frame(frame, _orders(timestamp_format=_US), "csv")
    assert events == []  # none read month first without a word
    assert problem_counts(problems)[ProblemCode.AMBIGUOUS_DATE] == 7


def test_ambiguous_amounts_beyond_the_inspection_sample_are_still_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autonomy.data.mapping as mapping_module

    monkeypatch.setattr(mapping_module, "INSPECT_SAMPLE", 2)
    amounts = ["10", "20", "30", "40", "2.550", "1,234"]  # nothing settles the convention
    frame = pd.DataFrame({"cid": list("abcdef"), "at": ["2024-01-01T10:00:00"] * 6, "amount": amounts})
    events, problems = map_frame(frame, _orders(), "csv")
    assert [e.value for e in events] == [10.0, 20.0, 30.0, 40.0]
    assert problem_counts(problems) == {ProblemCode.AMBIGUOUS_NUMBER: 2}


def test_a_file_in_another_currency_than_the_mapping_names_is_asked() -> None:
    frame = pd.DataFrame({"cid": ["a", "b"], "at": ["2024-01-01T10:00:00"] * 2, "amount": ["1", "2"]})
    euros = frame.assign(cur=["EUR", "EUR"])
    assert _codes(euros, _orders(attrs={"currency": "cur"})) == []  # its one currency is the business's
    assert _codes(euros, _orders(attrs={"currency": "cur"}, currency="GBP")) == [QuestionCode.CURRENCY]
    rated = _orders(attrs={"currency": "cur"}, currency="GBP", currency_rates={"EUR": 0.85})
    assert [e.value for e in map_frame(euros, rated, "csv", ask=True)[0]] == [0.85, 1.7]
