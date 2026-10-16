"""Client facts and data fingerprints: what the person said and what the data looked like last time are compared
with today's file, and a drastic change is said out loud with both numbers (stop / flag / note)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from engine.config import load_use_case
from engine.doctor.facts import FactError, FactInput, FactKind, FactStore
from engine.doctor.fingerprint import (
    Fingerprint,
    FingerprintStore,
    compare,
    fingerprint_detector,
    fingerprint_file,
)
from engine.storage import LocalStorage
from engine.utils.time import utc_now
from tests.unit.doctor import visit_support as vs


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _file(frame: pd.DataFrame, upload_id: str = vs.FIRST):
    return vs.loaded(upload_id, "customers.csv", frame, load_use_case(vs.GOAL_A))


def _print(frame: pd.DataFrame, **kw: object) -> Fingerprint:
    return fingerprint_file(
        _file(frame),
        goal_id=None,
        key="customer_id",
        outcome=kw.pop("outcome", "churned"),  # type: ignore[arg-type]
        brief=None,
        at=utc_now(),
        **kw,  # type: ignore[arg-type]
    )


def _detect(storage: LocalStorage, frame: pd.DataFrame):
    today = _file(frame, vs.SECOND)  # a later upload than the one `_print` fingerprints
    w = vs.World(files={vs.FIRST: today, vs.SECOND: today}, offers=())
    case = vs.open_case(storage, w)
    return fingerprint_detector(storage)(case, (today,), None), case


def test_facts_round_trip_with_who_and_when_and_can_be_taken_back(storage: LocalStorage) -> None:
    store = FactStore(storage)
    store.put(
        "default",
        [
            FactInput(kind=FactKind.CUSTOMER_COUNT, value="50,000"),
            FactInput(kind=FactKind.OUTCOME_RATE, value="2%", goal_id="telco-churn"),
            FactInput(kind=FactKind.FISCAL_YEAR_START, value="April"),
            FactInput(kind=FactKind.ID_LENGTH, value=8, approximate=False),
            FactInput(kind=FactKind.TIMEZONE, value="Asia/Kolkata"),
            FactInput(kind=FactKind.CURRENCY, value="inr"),
        ],
        actor="u-asha",
    )
    facts = {f.fact_id: f for f in FactStore(storage).list("default")}
    assert facts["customer_count"].value == 50_000 and facts["customer_count"].set_by == "u-asha"
    assert facts["outcome_rate:telco-churn"].value == pytest.approx(0.02)
    assert facts["fiscal_year_start"].value == 4 and facts["currency"].value == "INR"
    assert store.get("default", FactKind.OUTCOME_RATE, "telco-churn") is not None
    assert store.get("default", FactKind.OUTCOME_RATE, "other-goal") is None  # a rate is about its goal

    store.put("default", [FactInput(kind=FactKind.CUSTOMER_COUNT, value=60_000)], actor="u-ravi")
    assert store.get("default", FactKind.CUSTOMER_COUNT).value == 60_000  # type: ignore[union-attr]
    history = [f for f in store.list("default", include_revoked=True) if f.kind is FactKind.CUSTOMER_COUNT]
    assert [f.value for f in history] == [50_000, 60_000] and history[0].revoked_by == "u-ravi"

    store.put("default", [FactInput(kind=FactKind.CUSTOMER_COUNT, revoke=True)], actor="u-ravi")
    assert store.get("default", FactKind.CUSTOMER_COUNT) is None


def test_a_fact_that_makes_no_sense_is_refused_and_changes_nothing(storage: LocalStorage) -> None:
    store = FactStore(storage)
    with pytest.raises(FactError, match="share between 0 and 1"):
        store.put(
            "default",
            [
                FactInput(kind=FactKind.CUSTOMER_COUNT, value=100),
                FactInput(kind=FactKind.OUTCOME_RATE, value=5),
            ],
            actor="u",
        )
    assert store.list("default") == ()
    with pytest.raises(FactError):
        store.put("default", [FactInput(kind=FactKind.TIMEZONE, value="Mars/Base")], actor="u")


def test_first_visit_has_nothing_to_compare_with(storage: LocalStorage) -> None:
    found, _case = _detect(storage, vs.customers(300))
    assert not found.asks and not found.doubts and not found.evidence
    assert compare(_print(vs.customers(300)), None, ()) == []


def test_a_seventy_percent_drop_against_the_fact_is_a_stop_with_both_numbers(storage: LocalStorage) -> None:
    FactStore(storage).put("default", [FactInput(kind=FactKind.CUSTOMER_COUNT, value=1_000)], actor="u-asha")
    found, _case = _detect(storage, vs.customers(300))
    (stop,) = found.asks
    assert stop.code == "FACTS_CUSTOMER_COUNT" and stop.blocking
    assert "You told me about 1,000 customers" in stop.text and "Now: 300 customers" in stop.text
    assert any(option.safe for option in stop.options)
    assert found.evidence and not found.doubts

    # Near the fact: nothing to say.
    quiet, _ = _detect(storage, vs.customers(950))
    assert not quiet.asks and not quiet.doubts


def test_the_example_from_the_owner_reads_with_last_times_and_this_times_numbers() -> None:
    last = _print(vs.customers(100)).model_copy(
        update={"customers": 48_210, "outcome_rate": 0.021, "date_last": None}
    )
    now = last.model_copy(update={"customers": 8_214, "outcome_rate": 0.18})
    found = {f.code: f for f in compare(now, last, ())}
    assert found["FACTS_CUSTOMER_COUNT"].level == "stop" and found["FACTS_OUTCOME_RATE"].level == "stop"
    assert "Last time: 48,210 customers. Now: 8,214 customers." in found["FACTS_CUSTOMER_COUNT"].text
    assert "Last time: 2.1% yes. Now: 18% yes." in found["FACTS_OUTCOME_RATE"].text


def test_smaller_moves_are_flags_and_notes_not_stops() -> None:
    last = _print(vs.customers(100)).model_copy(
        update={"customers": 1_000, "outcome_rate": 0.10, "date_last": None}
    )
    flag = compare(last.model_copy(update={"customers": 650, "outcome_rate": 0.25}), last, ())
    assert {f.code: f.level for f in flag} == {"FACTS_CUSTOMER_COUNT": "flag", "FACTS_OUTCOME_RATE": "flag"}
    note = compare(last.model_copy(update={"customers": 850, "outcome_rate": 0.18}), last, ())
    assert {f.code: f.level for f in note} == {"FACTS_CUSTOMER_COUNT": "note", "FACTS_OUTCOME_RATE": "note"}
    assert compare(last.model_copy(update={"customers": 1_050, "outcome_rate": 0.11}), last, ()) == []


def test_a_renamed_column_is_a_flag_and_a_lost_outcome_column_is_a_stop(storage: LocalStorage) -> None:
    frame = vs.customers(300)
    FingerprintStore(storage).save("default", _print(frame))
    renamed = frame.rename(columns={"monthly_spend": "monthly_spend_inr"})
    found, _case = _detect(storage, renamed)
    (doubt,) = [d for d in found.doubts if "renamed" in d.because]
    assert "‘monthly_spend’" in doubt.because and "‘monthly_spend_inr’" in doubt.because
    assert not found.asks

    # The outcome column the person approved on last time is gone: the visit stops.
    lost, _ = _detect(storage, frame.drop(columns=["churned"]))
    assert [a.code for a in lost.asks] == ["FACTS_COLUMN_MISSING"] and lost.asks[0].blocking


def test_a_category_move_a_new_id_format_and_an_old_file_are_flagged() -> None:
    frame = vs.customers(300).assign(plan=["gold"] * 60 + ["basic"] * 240)
    last = _print(frame)
    shifted = frame.assign(plan=["gold"] * 180 + ["basic"] * 120)
    shifted["customer_id"] = [str(10_000_000 + i) for i in range(300)]
    codes = {f.code: f for f in compare(_print(shifted), last, ())}
    assert "80%" in codes["FACTS_CATEGORY_SHARE"].text and "40%" in codes["FACTS_CATEGORY_SHARE"].text
    assert codes["FACTS_KEY_FORMAT"].level == "flag" and "8 digits" in codes["FACTS_KEY_FORMAT"].text
    # The date range: the same newest date as last time may be an old file; an older one is a stop.
    dated = _print(frame).model_copy(update={"date_column": "snapshot_date", "date_last": "2026-09-30"})
    assert [f.level for f in compare(dated, dated, ()) if f.code == "FACTS_DATES_NOT_LATER"] == ["flag"]
    older = dated.model_copy(update={"date_last": "2026-08-31"})
    assert [f.level for f in compare(older, dated, ()) if f.code == "FACTS_DATES_NOT_LATER"] == ["stop"]


def test_the_id_length_fact_flags_ids_of_another_length() -> None:
    from engine.doctor.facts import ClientFact

    now = _print(vs.customers(300))  # C100000: 7 characters
    fact = ClientFact(fact_id="id_length", kind=FactKind.ID_LENGTH, value=8, set_by="u", set_at=utc_now())
    (finding,) = compare(now, None, [fact])
    assert finding.level == "stop" and "8 characters" in finding.text and "0%" in finding.text


def test_approval_saves_a_fingerprint_that_next_months_file_is_compared_with(storage: LocalStorage) -> None:
    w = vs.world()
    case = vs.open_case(storage, w)
    for _name, _op, act in vs.SCRIPT[:6]:  # open, key, goal, outcome, chat, approve
        case = act(vs.deps_for(storage, w), case)
    last = FingerprintStore(storage).last(vs.CLIENT, vs.GOAL_A)
    assert last is not None and last.upload_id == vs.FIRST
    assert last.customers == 1_200 and last.key_column == "customer_id" and last.outcome_column == "converted"
    assert last.outcome_rate is not None and 0.4 < last.outcome_rate < 0.6 and last.date_last == "2026-09-30"
    assert not any("C1" in str(v) for v in last.model_dump().values())  # no customer value is kept


def test_next_months_file_is_compared_with_last_months_never_with_itself(storage: LocalStorage) -> None:
    oct_ = vs.customers(300)
    nov = oct_.assign(snapshot_date="2026-10-31")
    nov["customer_id"] = nov["customer_id"].str.replace("C", "C0", regex=False)
    FingerprintStore(storage).save("default", _print(oct_))  # last month's file, approved
    w = vs.World(files={vs.FIRST: _file(oct_), vs.SECOND: _file(nov, vs.SECOND)}, offers=())
    case = vs.open_case(storage, w)
    # Only last month's file: it is not compared with its own fingerprint ("the same newest date").
    alone = fingerprint_detector(storage)(case, (w.files[vs.FIRST],), None)
    assert not alone.asks and not alone.doubts
    # Next month's file beside it: the new one is the one compared.
    both = fingerprint_detector(storage)(case, (w.files[vs.FIRST], w.files[vs.SECOND]), None)
    assert [d.because.split(" ")[0] for d in both.doubts] == ["IDs"]  # the ID format moved; nothing else
    assert both.evidence[0].input["upload_id"] == vs.SECOND


def test_a_customer_table_is_not_compared_with_last_times_orders_log(storage: LocalStorage) -> None:
    frame = vs.customers(300)
    orders = pd.DataFrame(
        {
            "customer_id": frame["customer_id"].repeat(2).to_numpy(),
            "order_id": [f"A-{n:06d}" for n in range(600)],
            "order_date": ["2026-07-01"] * 600,
            "order_value": [10] * 600,
        }
    )
    FingerprintStore(storage).save(
        "default", _print(orders, outcome=None)
    )  # approved last time: the orders file
    today = _file(frame, vs.SECOND)
    beside = _file(orders, vs.FIRST)  # the orders file is part of this visit too
    w = vs.World(files={vs.FIRST: beside, vs.SECOND: today}, offers=())
    case = vs.open_case(storage, w)
    found = fingerprint_detector(storage)(case, (beside, today), None)
    assert (
        not found.asks and not found.doubts
    ), "that print is the orders file beside it, not last time's customer file"


def test_a_lone_wrong_file_sharing_only_the_id_with_last_times_print_still_stops(
    storage: LocalStorage,
) -> None:
    frame = vs.customers(300)
    FingerprintStore(storage).save(
        "default", _print(frame)
    )  # last time: the customer table, with its outcome
    wrong = pd.DataFrame({"customer_id": frame["customer_id"], "a": 1, "b": 2})
    found, _case = _detect(storage, wrong)
    assert found.asks, "no other file in the visit explains it: it may be the wrong file"


def test_whole_number_ids_read_as_decimals_keep_their_length() -> None:
    frame = vs.customers(10).assign(customer_id=[12345678.0] * 9 + [None])
    print_ = _print(frame)
    assert print_.key_lengths == {"8": 1.0} and print_.key_pattern == "9{8}"


def test_a_new_category_value_is_said_with_its_share_and_a_tiny_one_is_not() -> None:
    frame = vs.customers(300).assign(plan=["gold"] * 100 + ["basic"] * 200)
    last = _print(frame)
    new_tier = frame.assign(plan=["gold"] * 100 + ["basic"] * 176 + ["enterprise"] * 24)
    (found,) = [f for f in compare(_print(new_tier), last, ()) if f.code == "FACTS_NEW_VALUE"]
    assert found.level == "flag" and "“enterprise” (8% of the rows)" in found.text and "‘plan’" in found.text
    small = frame.assign(plan=["gold"] * 100 + ["basic"] * 196 + ["enterprise"] * 4)
    (note,) = [f for f in compare(_print(small), last, ()) if f.code == "FACTS_NEW_VALUE"]
    assert note.level == "note"
    tiny = frame.assign(plan=["gold"] * 100 + ["basic"] * 199 + ["enterprise"] * 1)
    assert not [f for f in compare(_print(tiny), last, ()) if f.code == "FACTS_NEW_VALUE"]
    assert not [f for f in compare(_print(frame), last, ()) if f.code == "FACTS_NEW_VALUE"]


def test_a_new_category_value_reaches_the_person_as_a_doubt_with_its_numbers(storage: LocalStorage) -> None:
    frame = vs.customers(300).assign(plan=["gold"] * 100 + ["basic"] * 200)
    FingerprintStore(storage).save("default", _print(frame))
    found, _case = _detect(storage, frame.assign(plan=["gold"] * 100 + ["basic"] * 176 + ["enterprise"] * 24))
    (doubt,) = [d for d in found.doubts if "enterprise" in d.because]
    assert "‘plan’" in doubt.because and "8%" in doubt.because and not found.asks


def test_a_value_that_only_entered_the_top_ten_of_a_wider_column_is_not_called_new() -> None:
    # 11 categories: last time's shares keep only the 10 most common, so the rarest ("c10") is not in them
    before = [f"c{i}" for i in range(10) for _ in range(28)] + ["c10"] * 20
    after = [f"c{i}" for i in range(9) for _ in range(28)] + ["c9"] * 8 + ["c10"] * 40
    last = _print(vs.customers(300).assign(plan=before))
    now = _print(vs.customers(300).assign(plan=after))
    assert not [f for f in compare(now, last, ()) if f.code == "FACTS_NEW_VALUE"]


def test_a_doubled_or_halved_outcome_rate_and_a_fifth_more_customers_are_flags_not_notes() -> None:
    last = _print(vs.customers(100)).model_copy(
        update={"customers": 4_000, "outcome_rate": 0.116, "date_last": None}
    )
    big = compare(last.model_copy(update={"customers": 5_000, "outcome_rate": 0.233}), last, ())
    assert {f.code: f.level for f in big} == {"FACTS_CUSTOMER_COUNT": "flag", "FACTS_OUTCOME_RATE": "flag"}
    halved = compare(last.model_copy(update={"customers": 4_000, "outcome_rate": 0.06}), last, ())
    assert [(f.code, f.level) for f in halved] == [("FACTS_OUTCOME_RATE", "flag")]
    small = compare(last.model_copy(update={"customers": 4_500, "outcome_rate": 0.2}), last, ())
    assert {f.code: f.level for f in small} == {"FACTS_CUSTOMER_COUNT": "note", "FACTS_OUTCOME_RATE": "note"}


def test_what_is_about_the_same_column_is_one_card_with_the_facts_listed(storage: LocalStorage) -> None:
    from engine.doctor.fingerprint import Finding, _one_card_per_column

    def finding(level: str, code: str, text: str, column: str | None) -> Finding:
        return Finding(level, code, code, text, "do", "if", column, (1,))  # type: ignore[arg-type]

    found = [
        finding("flag", "FACTS_OUTCOME_RATE", "Last time: 12% yes. Now: 23% yes.", "bought"),
        finding("note", "FACTS_NEW_VALUE", "‘bought’ has a value not seen last time.", "bought"),
        finding("flag", "FACTS_CUSTOMER_COUNT", "Now: 5,000 customers.", None),
        finding("stop", "FACTS_COLUMN_MISSING", "No ‘bought’.", "bought"),
    ]
    merged = _one_card_per_column(found)
    assert [(f.code, f.level) for f in merged] == [
        ("FACTS_OUTCOME_RATE", "flag"),
        ("FACTS_CUSTOMER_COUNT", "flag"),
        ("FACTS_COLUMN_MISSING", "stop"),  # a stop stays its own question
    ]
    assert merged[0].text == "Last time: 12% yes. Now: 23% yes. ‘bought’ has a value not seen last time."
