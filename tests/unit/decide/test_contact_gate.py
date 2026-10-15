"""The contact-list gate (G14): who is taken off the list, counted, and said so on the list's summary.

The pure rules (`gate_customers`) first, then the treat list builder over a real written run: the customer file gets
the columns a client's file has (age, do-not-disturb, status) and the list must leave the right people out.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.decide.contact_gate import (
    ContactRules,
    GateRules,
    clean_contact_key,
    exclusion_lines,
    gate_customers,
    load_gate_rules,
    write_contact_rules,
    write_suppression_list,
)
from engine.decide.treat_list import TREAT_LIST_SUMMARY_FILENAME, TreatListSummary, build_treat_list
from engine.storage import LocalStorage, run_key, upload_key
from tests.fixtures.decide.treat_runs import write_run

DAY = date(2026, 10, 15)


@pytest.fixture(scope="module")
def rules() -> GateRules:
    return load_gate_rules()


def _reasons(
    frame: pd.DataFrame, rules: GateRules, overrides: ContactRules | None = None, **kw: object
) -> list[Any]:
    reasons, _ = gate_customers(frame, rules, overrides or ContactRules(), as_of=DAY, **kw)  # type: ignore[arg-type]
    return reasons.tolist()


def test_a_blank_do_not_disturb_answer_is_not_a_yes_to_contact(rules: GateRules) -> None:
    frame = pd.DataFrame({"dnd": ["N", "Y", None, "", "maybe", "DND-Partial", "n"]})
    assert _reasons(frame, rules) == [None, "dnd", "dnd_unknown", "dnd_unknown", "dnd_unknown", "dnd", None]


def test_a_blank_means_ok_only_when_the_person_said_so(rules: GateRules) -> None:
    frame = pd.DataFrame({"dnd": ["N", None, "DND-Partial", "maybe"]})
    said = ContactRules(blank_dnd_means_ok=True, partial_dnd_means_ok=True)
    # A blank and a partial pass; an answer nobody can read still does not.
    assert _reasons(frame, rules, said) == [None, None, None, "dnd_unknown"]


def test_consent_that_is_blank_or_refused_takes_the_customer_off(rules: GateRules) -> None:
    frame = pd.DataFrame({"marketing_consent": ["Y", "N", None, 1.0, 0.0]})
    assert _reasons(frame, rules) == [None, "no_consent", "consent_unknown", None, "no_consent"]
    ok = ContactRules(blank_consent_means_ok=True)
    assert _reasons(frame, rules, ok)[2] is None


def test_the_columns_the_scoring_run_already_applies_are_not_read_twice(rules: GateRules) -> None:
    frame = pd.DataFrame({"opt_in": ["Y", None]})
    assert _reasons(frame, rules, skip_consent=["opt_in"]) == [None, None]


def test_minors_are_found_from_an_age_or_a_date_of_birth_on_the_run_date(rules: GateRules) -> None:
    by_age = pd.DataFrame({"age": [17, 18, 40, None]})
    reasons, verdict = gate_customers(by_age, rules, ContactRules(), as_of=DAY)
    assert reasons.tolist() == ["minor", None, None, None]
    assert verdict.flagged == {"age_unknown": 1}  # flagged, not guessed

    by_birth = pd.DataFrame({"date_of_birth": ["2008-10-16", "2008-10-15", "1990-01-01", "not a date"]})
    assert _reasons(by_birth, rules) == ["minor", None, None, None]


def test_deceased_fraud_and_legal_statuses_are_taken_off_and_the_list_can_be_extended(
    rules: GateRules,
) -> None:
    frame = pd.DataFrame({"customer_status": ["Active", "Deceased", "FRAUD", "Legal", "Dormant"]})
    assert _reasons(frame, rules) == [None, "blocked_status", "blocked_status", "blocked_status", None]
    wider = ContactRules(extra_blocked_statuses=("Dormant",))
    assert _reasons(frame, rules, wider)[4] == "blocked_status"


def test_a_suppression_list_is_matched_on_cleaned_keys_and_the_match_rate_is_shown(rules: GateRules) -> None:
    frame = pd.DataFrame({"phone": ["98765 43210", "+91 91234-56789", "9000000001", None]})
    listed = ["+919876543210", "09123456789", "9999999999"]  # the third is not one of our customers
    reasons, verdict = gate_customers(
        frame, rules, ContactRules(suppression_column="phone"), as_of=DAY, suppression_keys=listed
    )
    assert reasons.tolist() == ["suppression_list", "suppression_list", None, None]
    assert (verdict.suppression_listed, verdict.suppression_matched) == (3, 2)
    assert any(
        "2 of 3 on your suppression list" in line and "67%" in line for line in exclusion_lines({}, verdict)
    )


def test_each_customer_is_counted_once_under_the_first_reason(rules: GateRules) -> None:
    frame = pd.DataFrame({"age": [16], "dnd": ["Y"], "status": ["deceased"]})
    assert _reasons(frame, rules) == ["blocked_status"]


def test_an_old_scrub_earns_a_warning(rules: GateRules) -> None:
    frame = pd.DataFrame({"dnd": ["N"]})
    _, verdict = gate_customers(frame, rules, ContactRules(dnd_scrubbed_on=date(2026, 6, 1)), as_of=DAY)
    assert any("last checked 136 days ago" in note for note in verdict.notes)


def test_phone_spellings_clean_to_one_key() -> None:
    assert clean_contact_key("98765 43210") == clean_contact_key("+91-9876543210") == "+919876543210"
    assert clean_contact_key(" A@B.com ") == "a@b.com"
    assert clean_contact_key("") is None


# --- the treat list ---------------------------------------------------------------------------------------------
def _customer_file(storage: LocalStorage, run_id: str, **columns: list[Any]) -> None:
    """Give the run's customer file the columns a client's file has (the rows are the fixture's own)."""
    key = upload_key(f"u_{run_id[-8:]}", "source.csv")
    frame = pd.read_csv(io.BytesIO(storage.read_bytes(key)))
    for name, values in columns.items():
        frame[name] = [values[i % len(values)] for i in range(len(frame))]
    storage.write_bytes(key, frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))


def _built(
    tmp_path: Path, run_id: str = "run_20260101_aaaaaaaa", **columns: list[Any]
) -> tuple[LocalStorage, pd.DataFrame]:
    storage = LocalStorage(tmp_path)
    write_run(storage, run_id, value=True)
    if columns:
        _customer_file(storage, run_id, **columns)
    build_treat_list(storage, run_id)
    listed = pd.read_csv(io.BytesIO(storage.read_bytes(run_key(run_id, "treat_list.csv"))))
    return storage, listed


def _summary(storage: LocalStorage, run_id: str = "run_20260101_aaaaaaaa") -> TreatListSummary:
    return storage.read_model(run_key(run_id, TREAT_LIST_SUMMARY_FILENAME), TreatListSummary)


def test_a_list_for_a_file_with_nothing_to_check_is_unchanged(tmp_path: Path) -> None:
    storage, _ = _built(tmp_path)
    summary = _summary(storage)
    assert summary.contact_exclusions is None and summary.contact_exclusion_lines is None
    assert (
        "contact_exclusions"
        not in storage.read_bytes(run_key("run_20260101_aaaaaaaa", TREAT_LIST_SUMMARY_FILENAME)).decode()
    )


def test_minors_and_unknown_dnd_never_reach_the_contact_list_and_are_counted(tmp_path: Path) -> None:
    ages = [34, 16, 52, 40, 45, 61]
    dnd = ["N", "N", None, "Y", "N", "N"]
    storage, listed = _built(tmp_path, age=ages, dnd=dnd)
    summary = _summary(storage)

    counts = summary.contact_exclusions or {}
    assert counts.get("minor", 0) > 0 and counts.get("dnd", 0) > 0 and counts.get("dnd_unknown", 0) > 0
    assert summary.contact_gate_columns == {"minor": "age", "dnd": "dnd"}
    assert any(
        line.endswith("excluded because they are under 18") for line in summary.contact_exclusion_lines or []
    )

    treated = listed[listed["treat"] == 1]
    source = pd.read_csv(io.BytesIO(storage.read_bytes(upload_key("u_aaaaaaaa", "source.csv"))))
    taken = source[(source["age"] < 18) | (source["dnd"] != "N")]["customer_id"]
    assert not set(treated["customer_id"]) & set(taken)
    assert set(listed.loc[listed["customer_id"].isin(taken), "suppression_reason"].dropna()) <= {
        "minor",
        "dnd",
        "dnd_unknown",
        "blocked_status",
        "consent_false",
        "opted_out",
        "recently_contacted",
    }
    assert summary.suppressed_rows >= sum(counts.values())


def test_a_suppression_list_and_the_persons_rules_reach_the_list(tmp_path: Path) -> None:
    run_id = "run_20260101_bbbbbbbb"
    storage = LocalStorage(tmp_path)
    write_run(storage, run_id, value=True)
    _customer_file(storage, run_id, phone=[f"98765{n:05d}" for n in range(24)], dnd=[None])
    write_suppression_list(storage, run_id, ["+919876500003", "9876500004", "9111111111"])
    write_contact_rules(
        storage,
        run_id,
        ContactRules(blank_dnd_means_ok=True, suppression_column="phone", dnd_scrubbed_on=date(2020, 1, 1)),
    )

    build_treat_list(storage, run_id)
    summary = _summary(storage, run_id)
    counts = summary.contact_exclusions or {}
    assert "dnd_unknown" not in counts  # the person said a blank means OK
    assert counts["suppression_list"] >= 1
    lines = " ".join(summary.contact_exclusion_lines or [])
    assert "of 3 on your suppression list were found" in lines and "last checked" in lines


def test_a_customer_file_that_is_gone_leaves_the_list_as_scored_unless_rules_were_given(
    tmp_path: Path,
) -> None:
    run_id = "run_20260101_cccccccc"
    storage = LocalStorage(tmp_path)
    write_run(storage, run_id)  # no value: the fixture keeps no customer file
    write_contact_rules(storage, run_id, ContactRules())
    build_treat_list(storage, run_id)
    lines = _summary(storage, run_id).contact_exclusion_lines or []
    assert lines and "no longer stored" in lines[0]


def test_every_status_and_do_not_disturb_column_is_read_not_only_the_first(rules: GateRules) -> None:
    frame = pd.DataFrame(
        {
            "kyc_status": ["Verified", "Verified", "Verified"],
            "account_status": ["Active", "Deceased", "Active"],
            "dnd_flag": ["N", "N", "Y"],  # found by the word in its name, read because its answers are known
            "dnd_checked_on": ["2026-09-01"] * 3,  # dates: not an answer column, and no note
        }
    )
    reasons, verdict = gate_customers(frame, rules, ContactRules(), as_of=DAY)
    assert reasons.tolist() == [None, "blocked_status", "dnd"]
    assert verdict.columns["dnd"] == "dnd_flag" and verdict.notes == []


def test_a_do_not_disturb_column_whose_answers_are_not_known_is_named_not_guessed(rules: GateRules) -> None:
    frame = pd.DataFrame({"dnd_status": ["Active", "Inactive", "Active"]})
    reasons, verdict = gate_customers(frame, rules, ContactRules(), as_of=DAY)
    assert reasons.tolist() == [None, None, None]
    assert any("'dnd_status' looks like a do-not-disturb column" in note for note in verdict.notes)


def test_a_suppression_list_of_phone_numbers_meets_a_hashed_working_key_through_the_vault(
    tmp_path: Path,
) -> None:
    from engine.privacy.working_key import KEY_VAULT_FILENAME

    run_id = "run_20260101_dddddddd"
    storage = LocalStorage(tmp_path)
    write_run(storage, run_id, value=True)
    source = pd.read_csv(io.BytesIO(storage.read_bytes(upload_key("u_dddddddd", "source.csv"))))
    ids = source["customer_id"].astype(str).tolist()  # stands in for the hashes the table holds
    vault = pd.DataFrame({"working_key": ids, "real_key": [f"98765{n:05d}" for n in range(len(ids))]})
    buffer = io.BytesIO()
    vault.to_parquet(buffer, index=False)
    storage.write_bytes(upload_key("u_dddddddd", KEY_VAULT_FILENAME), buffer.getvalue())
    write_suppression_list(storage, run_id, ["+91 98765 00001"])

    build_treat_list(storage, run_id)
    summary = _summary(storage, run_id)
    assert (summary.contact_exclusions or {}).get("suppression_list", 0) >= 1
    assert "1 of 1 on your suppression list were found" in " ".join(summary.contact_exclusion_lines or [])
