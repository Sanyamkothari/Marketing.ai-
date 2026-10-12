"""Plan O M241: the locale profile - detected, confirmed once per client, then applied visibly (the design §8.1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from engine.doctor.locale import (
    SETTINGS,
    LocaleProfile,
    confirm_profile,
    dayfirst_for,
    detect_locale,
    forget_profile,
    india_defaults,
    locale_for,
    locale_key,
    saved_profile,
)
from engine.storage import LocalStorage

CONFIRMED_AT = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)


def _indian_file(rows: int = 24) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"C{i:04d}" for i in range(rows)],
            "bill_amount": [["₹1,20,000", "Rs 3,50,000/-", "₹2.5 L", "₹45,000"][i % 4] for i in range(rows)],
            "signup": [f"{13 + i % 15}/0{1 + i % 9}/2024" for i in range(rows)],
            "recharge_done": [["haan", "nahi"][i % 2] for i in range(rows)],
            "mobile": [f"+91 98765 {43210 + i}" for i in range(rows)],
            "pincode": [str(560001 + i) for i in range(rows)],
            "gstin": ["27AAPFU0939F1ZV"] * rows,
            "last_login": [f"2024-03-0{1 + i % 9} 10:15:00+05:30" for i in range(rows)],
        }
    )


def test_the_india_defaults_follow_the_design_table() -> None:
    profile = india_defaults("acme")
    values = profile.values()
    assert values["workspace_currency"] == "INR"
    assert values["decimal_mark"] == "." and values["digit_grouping"] == "indian"
    assert values["date_order"] == "day_first"
    assert values["number_words"] == "lakh_crore"
    assert values["boolean_words"] == ("english", "hinglish", "devanagari")
    assert values["phone_format"] == "+91" and values["postcode"] == "pin"
    assert set(values["personal_ids"]) == {"pan", "aadhaar", "gstin", "epic", "upi"}
    assert values["time_zone"] == "Asia/Kolkata" and values["consent_regime"] == "IN"
    assert values["header_words"] == "hinglish"  # O4
    assert all(getattr(profile, name).source == "default" for name in SETTINGS)
    assert tuple(values) == SETTINGS


def test_detection_proves_what_an_indian_file_shows_and_records_the_source() -> None:
    detection = detect_locale(_indian_file())
    profile = detection.profile
    assert profile.digit_grouping.value == "indian" and profile.digit_grouping.source == "detected"
    assert profile.date_order.value == "day_first" and profile.date_order.source == "detected"
    assert profile.number_words.value == "lakh_crore" and profile.number_words.source == "detected"
    assert "hinglish" in profile.boolean_words.value and profile.boolean_words.source == "detected"
    assert profile.phone_format.source == "detected" and profile.postcode.value == "pin"
    assert profile.money_units.value == {"bill_amount": "INR"}
    assert profile.time_zone.source == "detected"
    assert profile.workspace_currency.source == "default"  # from the settings, never from a file
    assert detection.conflicts == ()
    assert profile.digit_grouping.evidence  # counts, in plain words


def test_detection_returns_no_cell_value() -> None:
    frame = _indian_file()
    text = json.dumps(detect_locale(frame).model_dump(mode="json"), ensure_ascii=False)
    for secret in ("27AAPFU0939F1ZV", "98765", "560001", "3,50,000", "haan"):
        assert secret not in text


def test_a_us_file_proves_month_first_western_grouping_and_dollars() -> None:
    frame = pd.DataFrame(
        {
            "amount": ["$1,200,000.50", "$3,400,000", "$12.75", "$950,000.25"],
            "joined": ["12/25/2023", "01/31/2024", "03/15/2024", "11/28/2023"],
            "active": ["Y", "N", "Y", "N"],
        }
    )
    profile = detect_locale(frame).profile
    assert profile.date_order.value == "month_first"
    assert profile.digit_grouping.value == "western"
    assert profile.money_units.value == {"amount": "USD"}
    assert profile.number_words.source == "default"  # nothing proved; nothing converted either


def test_a_currency_word_inside_text_is_not_a_money_column() -> None:
    frame = pd.DataFrame(
        {"duration": ["3 hours", "4 hours", "12 hours"], "fee": ["Rs 100", "Rs 250", "Rs 75"]}
    )
    assert detect_locale(frame).profile.money_units.value == {"fee": "INR"}


def test_a_column_with_rupees_and_dollars_is_recorded_as_mixed_and_never_converted() -> None:
    frame = pd.DataFrame({"amount": ["₹1,200", "$15", "₹3,400", "$20"]})
    profile = detect_locale(frame).profile
    assert profile.money_units.value == {"amount": "mixed"}
    assert profile.workspace_currency.value == "INR"


def test_a_european_decimal_comma_is_proved_by_values_that_read_one_way_only() -> None:
    frame = pd.DataFrame({"price": ["1.234,50", "12,75", "3.400,00", "0,99"]})
    assert detect_locale(frame).profile.decimal_mark.value == ","


def test_an_unprovable_day_month_order_stays_unproven_and_is_asked() -> None:
    frame = pd.DataFrame({"when": ["01/02/2024", "03/04/2024", "05/06/2024"]})
    detection = detect_locale(frame)
    assert "date_order" in detection.unproven
    assert detection.profile.date_order.source == "default"
    assert dayfirst_for(detection.profile, None) is None  # default day-first is still asked when unproven


def test_files_that_disagree_are_a_conflict_and_change_nothing() -> None:
    files = {
        "a.csv": pd.DataFrame({"d": ["25/12/2023", "13/01/2024"]}),
        "b.csv": pd.DataFrame({"d": ["12/25/2023", "01/13/2024"]}),
    }
    detection = detect_locale(files)
    (conflict,) = detection.conflicts
    assert conflict.field == "date_order" and set(conflict.columns) == {"a.csv:d", "b.csv:d"}
    assert detection.profile.date_order.source == "default"


def test_gstin_and_epic_columns_add_to_the_personal_ids() -> None:
    frame = pd.DataFrame({"voter": [f"ABC{1000000 + i}" for i in range(10)]})
    base = india_defaults().model_copy(
        update={"personal_ids": india_defaults().personal_ids.model_copy(update={"value": ("pan",)})}
    )
    profile = detect_locale(frame, base=base).profile
    assert profile.personal_ids.value == ("pan", "epic") and profile.personal_ids.source == "detected"


def test_the_profile_is_asked_once_per_client_and_then_applied_visibly(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    first = locale_for(storage, "acme", _indian_file())
    assert first.question is not None and first.question.fields == SETTINGS
    assert first.question.sources["digit_grouping"] == "detected"
    assert not first.applied and first.notice is None

    confirmed = confirm_profile(
        storage, first.profile, client_id="acme", confirmed_by="u-priya", clock=lambda: CONFIRMED_AT
    )
    assert confirmed.confirmed_by == "u-priya" and confirmed.confirmed_at == CONFIRMED_AT
    assert all(getattr(confirmed, name).source == "confirmed" for name in SETTINGS)

    second = locale_for(storage, "acme", _indian_file())
    assert second.question is None  # asked once
    assert second.applied and second.notice is not None and "3 Sep 2026" in second.notice
    assert second.profile.profile_hash == confirmed.profile_hash
    assert dayfirst_for(second.profile, None) is True  # a confirmed order is applied, and said so

    other = locale_for(storage, "globex", _indian_file())
    assert other.question is not None  # per client


def test_a_file_that_contradicts_the_confirmed_profile_asks_only_about_that(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    confirm_profile(
        storage,
        detect_locale(_indian_file()).profile,
        client_id="acme",
        confirmed_by="u1",
        clock=lambda: CONFIRMED_AT,
    )
    us = pd.DataFrame({"joined": ["12/25/2023", "01/31/2024"]})
    outcome = locale_for(storage, "acme", us)
    assert outcome.applied and outcome.question is not None
    assert outcome.question.fields == ("date_order",)
    assert outcome.profile.date_order.value == "day_first"  # kept until the person answers
    assert outcome.profile.date_order.source == "confirmed"


def test_answers_override_and_are_validated(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    profile = india_defaults()
    saved = confirm_profile(
        storage, profile, client_id="acme", confirmed_by="u1", answers={"date_order": "month_first"}
    )
    assert saved.date_order.value == "month_first"
    with pytest.raises(ValueError):
        confirm_profile(
            storage, profile, client_id="acme", confirmed_by="u1", answers={"date_order": "sideways"}
        )
    with pytest.raises(ValueError):
        confirm_profile(storage, profile, client_id="acme", confirmed_by="u1", answers={"favourite": 1})
    with pytest.raises(ValueError):
        confirm_profile(storage, profile, client_id="acme", confirmed_by="")


def test_forgetting_a_profile_asks_again(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    confirm_profile(storage, india_defaults(), client_id="acme", confirmed_by="u1")
    assert saved_profile(storage, "acme") is not None
    assert forget_profile(storage, "acme") and not forget_profile(storage, "acme")
    assert locale_for(storage, "acme", _indian_file()).question is not None


def test_an_unreadable_profile_is_asked_again_not_a_crash(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    storage.write_bytes(locale_key("acme"), b"{not json")
    assert saved_profile(storage, "acme") is None


@pytest.mark.parametrize("client_id", ["", "../x", "a/b", ".hidden", "x" * 65])
def test_a_client_id_that_is_not_a_plain_name_is_refused(client_id: str) -> None:
    with pytest.raises(ValueError):
        locale_key(client_id)


def test_the_stored_profile_holds_no_cell_value(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    confirm_profile(storage, detect_locale(_indian_file()).profile, client_id="acme", confirmed_by="u1")
    text = storage.read_bytes(locale_key("acme")).decode("utf-8")
    for secret in ("27AAPFU0939F1ZV", "98765", "560001", "haan"):
        assert secret not in text
    assert LocaleProfile.model_validate_json(text).client_id == "acme"


def test_the_profile_hash_ignores_sources_and_follows_values() -> None:
    a = india_defaults("x")
    detected = a.model_copy(update={"decimal_mark": a.decimal_mark.model_copy(update={"source": "detected"})})
    assert detected.profile_hash == a.profile_hash
    changed = a.model_copy(update={"decimal_mark": a.decimal_mark.model_copy(update={"value": ","})})
    assert changed.profile_hash != a.profile_hash


def test_a_confirmed_money_unit_is_never_overwritten_by_a_later_file(tmp_path: Path) -> None:
    """Review item 1 (blocker): a later file in dollars asks; it never rewrites the confirmed rupees."""
    storage = LocalStorage(tmp_path)
    first = detect_locale(pd.DataFrame({"amount": ["₹1,200", "₹3,400", "₹560"]})).profile
    assert first.money_units.value == {"amount": "INR"}
    confirm_profile(storage, first, client_id="acme", confirmed_by="u1", clock=lambda: CONFIRMED_AT)
    later = pd.DataFrame({"amount": ["$1,200", "$15", "$20"], "spend": ["$5", "$7", "$9"]})
    outcome = locale_for(storage, "acme", later)
    assert outcome.applied
    assert outcome.question is not None and "money_units" in outcome.question.fields
    assert outcome.profile.money_units.value == {"amount": "INR"}  # kept until the person answers
    assert outcome.profile.money_units.source == "confirmed"
    (conflict,) = [c for c in outcome.conflicts if c.field == "money_units"]
    assert conflict.profile_value == {"amount": "INR"} and conflict.found == {"amount": "USD"}
    assert outcome.unconfirmed_money_units == {"spend": "USD"}  # a new column waits; never stamped confirmed
    assert outcome.question.proposed["money_units"] == {"amount": "INR", "spend": "USD"}
    assert outcome.question.sources["money_units"] == "detected"


def test_a_new_money_column_after_confirmation_is_asked_not_confirmed(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    confirm_profile(
        storage,
        detect_locale(pd.DataFrame({"amount": ["₹1,200", "₹3,400"]})).profile,
        client_id="acme",
        confirmed_by="u1",
    )
    outcome = locale_for(storage, "acme", pd.DataFrame({"amount": ["₹5", "₹6"], "fee": ["₹1", "₹2"]}))
    assert outcome.conflicts == ()
    assert outcome.question is not None and outcome.question.fields == ("money_units",)
    assert outcome.profile.money_units.value == {"amount": "INR"}
    assert outcome.unconfirmed_money_units == {"fee": "INR"}


def test_money_units_are_keyed_by_column_not_by_file() -> None:
    files = {
        "bills_aug.csv": pd.DataFrame({"amount": ["₹1,200", "₹3,400"]}),
        "bills_sep.csv": pd.DataFrame({"amount": ["₹5,600", "₹7,800"]}),
    }
    assert detect_locale(files).profile.money_units.value == {"amount": "INR"}


def test_a_bare_letter_in_a_unit_column_does_not_vote_for_number_words() -> None:
    """Review item 3: `30m` minutes and `2L` litres are not evidence of million or lakh."""
    for frame in (pd.DataFrame({"duration": ["30m", "45m", "15m"]}), pd.DataFrame({"qty": ["2L", "5L", "1L"]})):
        assert detect_locale(frame).profile.number_words.source == "default"
    money = pd.DataFrame({"ctc": ["12K", "1.5M", "800K"]})
    assert detect_locale(money).profile.number_words.value == "k_m"


def test_six_digit_amounts_do_not_prove_pin_codes() -> None:
    """Review item 7: only sure findings vote, so an amount column cannot make the postcode `detected`."""
    frame = pd.DataFrame(
        {
            "premium_paid": [100_000 + 7_919 * i for i in range(30)],
            "bill": [str(100_000 + i) for i in range(30)],
        }
    )
    assert detect_locale(frame).profile.postcode.source == "default"
