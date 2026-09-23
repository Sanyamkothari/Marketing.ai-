"""M36 items 1 and 4: one PII detector for the whole engine, and PII inside free text.

Item 1 (ruling D6, DEC-092). `engine.stages.ingest.detect_pii` and `engine.stages.prepare._detect_pii`
used to be two tables that disagreed; on the library's online-retail file prepare redacted the ISO
date column `snapshot_date` as a phone number while the validation report said nothing. Every
pattern either old table recognised has a case below, the shapes that were never personal data are
pinned as exclusions, the two call sites are shown to agree column for column, and the five library
samples are re-run through both.

Item 4 (ruling D5, DEC-095). A contact typed into a free-text column is found at profiling, reported
as the `PII_IN_FREE_TEXT` warning and masked wherever a cell of the column is shown - samples,
previews, reasons, the dataset review sample, the logs. The table that exposed the gap is Phase 2's
synthetic complaints table (`tests/fixtures/raw/make_raw.py`), whose `REMARKS` column plants a phone
number or an e-mail address in about two rows in three; it is used here as it is.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd
import pytest

from engine import pii
from engine.config import ColumnType, UseCaseConfig, load_use_case, resolve_config
from engine.contracts import (
    EXTENSION_VALIDATION_CODES,
    VALIDATION_CODES,
    Direction,
    Reason,
    RowExplanation,
    Severity,
    ValidationCheck,
    dump_artefact,
)
from engine.onboarding.datasets import _sample_rows
from engine.onboarding.specs import OnboardingCheck
from engine.stages import ingest, prepare, validate
from engine.stages.ingest import REDACTED, infer_column_type, profile_dataset
from tests.fixtures.raw.make_raw import RawTables, make_raw

REPO = Path(__file__).resolve().parents[2]
LIBRARY = REPO / "library"

#: The five library samples, with the key and target each run reserved.
LIBRARY_SAMPLES: dict[str, tuple[str, str]] = {
    "telco-customer-churn": ("customerID", "Churn"),
    "uci-bank-marketing": ("client_id", "y"),
    "online-retail": ("customer_id", "reactivated_90d"),
    "health-insurance-cross-sell": ("id", "Response"),
    "uci-credit-default": ("ID", "default_payment_next_month"),
}

#: What the two detectors flagged on each library sample before M36, measured on this tree's parent
#: commit (validate: `ingest.detect_pii`; prepare: the old `_detect_pii`, key and target reserved).
BEFORE: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    "telco-customer-churn": ({}, {}),
    "uci-bank-marketing": ({}, {}),
    "online-retail": ({}, {"snapshot_date": "phone number"}),
    "health-insurance-cross-sell": ({}, {}),
    "uci-credit-default": ({}, {}),
}


@pytest.fixture(scope="module")
def telco() -> UseCaseConfig:
    return load_use_case("telco-churn")


def profile_of(frame: pd.DataFrame, config: UseCaseConfig) -> ingest.DatasetProfile:
    return profile_dataset(
        frame,
        config,
        upload_id="upl_test",
        file_name="data.csv",
        file_format="csv",
        file_size_bytes=1024,
        delimiter=",",
        encoding="utf-8",
    )


def kinds_of(values: list[object], name: str = "value") -> tuple[str, ...]:
    series = pd.Series(values, name=name)
    return pii.detect_pii(series, name, infer_column_type(series))


# ---------------------------------------------------------------------------
# One pattern set: every shape either old table recognised
# ---------------------------------------------------------------------------
#: Each value matched the old prepare table's pattern for its kind, and must still be caught.
PREPARE_VALUE_CASES: list[tuple[str, str, str]] = [
    # prepare's `[^@\s]+@[^@\s]+\.[A-Za-z]{2,}`: any non-space local part and domain
    ("email", "user1@example.com", "email"),
    ("email", "o'brien@example.org", "email"),
    ("email", "josé@exämple.com", "email"),
    ("email", "first+tag@sub.example.co.uk", "email"),
    # prepare's `\+?\d[\d\s().-]{7,17}\d`: digits with spaces, dots, dashes and brackets
    ("phone", "+1 555 555 0001", "phone"),
    ("phone", "555.555.0001", "phone"),
    ("phone", "0400 123 456", "phone"),
    ("phone", "98 76 54 32 10", "phone"),
    ("phone", "+44 20 7946 0958", "phone"),
    ("phone", "9876543210", "phone"),
    # prepare's case-sensitive PAN
    ("pan", "ABCDE1234F", "pan"),
    # prepare's `\d{4}\s?\d{4}\s?\d{4}`
    ("aadhaar", "2345 6789 1234", "aadhaar"),
    ("aadhaar", "234567891234", "aadhaar"),
    # No Aadhaar number starts with 0 or 1; the twelve digits are still a phone-shaped identifier.
    ("aadhaar", "0123 4567 8901", "phone"),
]

#: Each value matched the old ingest table's pattern for its kind (its own positive cases).
INGEST_VALUE_CASES: list[tuple[str, str, str]] = [
    ("email", "person.1@example.invalid", "email"),
    ("phone", "+91 98765 43210", "phone"),
    ("phone", "+1-555-555-0001", "phone"),
    ("pan", "abcde1234f", "pan"),
    ("aadhaar", "2345-6789-1234", "aadhaar"),
    ("name", "Priya Sharma", "name"),
]

#: Shapes an old table accepted that were never personal data - the bug this milestone closes.
NOT_PII: list[tuple[str, str]] = [
    ("prepare's phone pattern read an ISO date as a phone number", "2011-09-10"),
    ("nor a date and time", "2011-09-10 12:30:00"),
    ("nor bracket noise", "0(((((((0"),
    ("a short number is not a phone", "123 456"),
    ("an id with a few digits is not a phone", "C-10482"),
]


@pytest.mark.parametrize(("old_kind", "value", "kind"), PREPARE_VALUE_CASES + INGEST_VALUE_CASES)
def test_every_value_shape_either_old_detector_knew_is_still_caught(
    old_kind: str, value: str, kind: str
) -> None:
    del old_kind  # the parametrisation id says which old pattern the case came from
    patterns = {detector.kind: detector.value_pattern for detector in pii.VALUE_DETECTORS}
    pattern = patterns[kind]
    assert pattern is not None
    assert pattern.fullmatch(value) is not None
    # and as a whole column, which is how both call sites ask; a name needs an open vocabulary
    if kind == "name":
        assert kind in kinds_of([value, "Arjun Mehta", "Rahul Verma", "Ananya Rao"], name="holder_name")
    else:
        assert kind in kinds_of([value] * 5, name="anything")


@pytest.mark.parametrize(("why", "value"), NOT_PII)
def test_shapes_that_were_never_personal_data_are_not_matched(why: str, value: str) -> None:
    del why
    for kind, pattern in pii.TEXT_SCANNERS:
        assert pattern.fullmatch(value) is None, kind
        assert pattern.search(value) is None, kind


@pytest.mark.parametrize(
    ("header", "kind"),
    [
        # prepare's name-only list: kinds no value pattern can recognise
        ("address", "address"),
        ("street_address", "address"),
        ("postcode", "address"),
        ("zipcode", "address"),
        ("ssn", "ssn"),
        ("passport", "passport"),
        ("passport_no", "passport"),
    ],
)
def test_a_header_that_names_an_address_ssn_or_passport_is_enough(header: str, kind: str) -> None:
    """prepare found these by their header alone; the one detector keeps that, and says so."""
    assert kind in kinds_of(["12 Main Street", "4 High Road", "9 Low Lane"], name=header)
    assert kind in kinds_of([2000, 2010, 3000], name=header)  # a postcode read as an integer


@pytest.mark.parametrize(
    ("header", "values", "kind"),
    [
        ("email", ["a@example.com", "b@example.com", "unknown"], "email"),
        ("mail", ["a@example.com", "b@example.com", "unknown"], "email"),
        ("phone", ["+1-555-555-0001", "n/a", "n/a"], "phone"),
        ("mobile", [9876543210, 9876543211, 9876543212], "phone"),
        ("msisdn", ["919876543210", "n/a", "n/a"], "phone"),
        ("telephone", ["0400 123 456", "n/a", "n/a"], "phone"),
        ("surname", ["Sharma", "Mehta", "Verma"], "name"),
        ("aadhar", ["2345 6789 1234", "n/a", "n/a"], "aadhaar"),
        ("pan", ["ABCDE1234F", "n/a", "n/a"], "pan"),
    ],
)
def test_every_header_prepare_matched_still_leads_to_its_kind(
    header: str, values: list[object], kind: str
) -> None:
    """prepare's other header tokens: a header plus a share of matching values (ingest's rule)."""
    assert kind in kinds_of(values, name=header)


def test_the_type_gate_is_the_one_both_call_sites_use() -> None:
    """FLOAT, BOOLEAN, DATE and DATETIME are never examined - the gate prepare used to lack."""
    dates = pd.to_datetime(pd.Series(["2011-09-10"] * 20))
    assert pii.detect_pii(dates, "snapshot_date", ColumnType.DATE) == ()
    assert pii.detect_pii(pd.Series([True, False] * 5), "has_address", ColumnType.BOOLEAN) == ()
    assert pii.detect_pii(pd.Series([9876543210.5] * 5), "phone", ColumnType.FLOAT) == ()


def test_ingest_and_the_generative_redaction_use_the_one_table() -> None:
    from engine.generative import redaction

    assert ingest.PII_DETECTORS is pii.VALUE_DETECTORS
    assert ingest.detect_pii(pd.Series(["a@example.com"] * 3), "x", ColumnType.STRING) == ("email",)
    text = "Call 0400 123 456 or write to jo@example.com."
    assert redaction.redact(text) == pii.redact_text(text)
    assert redaction.find(text) == pii.find_in_text(text)
    assert not hasattr(prepare, "_PII_VALUE_PATTERNS")
    assert not hasattr(prepare, "_PII_NAME")


# ---------------------------------------------------------------------------
# The two call sites agree
# ---------------------------------------------------------------------------
def _mixed_frame(rows: int = 60) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"C-{10000 + i}" for i in range(rows)],
            "snapshot_date": ["2011-09-10"] * (rows // 2) + ["2011-10-10"] * (rows - rows // 2),
            "contact_line": [f"person{i}@example.org" for i in range(rows)],
            "mobile": [9876543210 + i for i in range(rows)],
            "amount": [float(i) + 0.5 for i in range(rows)],
            "street_address": [f"{i} High Street" for i in range(rows)],
            "gender": ["Female", "Male"] * (rows // 2),
            "notes": ["call me back tomorrow please"] * rows,
            "churned": [0, 1] * (rows // 2),
        }
    )


def test_prepare_redacts_exactly_the_columns_the_validation_report_names() -> None:
    """The discrepancy the library filed, gone: same function, same type, same answer (DEC-092)."""
    frame = _mixed_frame()
    facts = validate.derive_facts(frame)
    reported = {name for name, kinds in facts.pii_kinds.items() if kinds}
    redacted = set(prepare._detect_pii(frame, ()))
    assert reported == redacted == {"contact_line", "mobile", "street_address"}
    # a reserved column is never redacted, whatever the report says about it
    assert "mobile" not in prepare._detect_pii(frame, ("mobile",))


def test_a_live_snapshot_date_is_neither_reported_nor_redacted() -> None:
    """The shape `split.type: time_based` exists for: several snapshot dates, none of them a phone."""
    frame = _mixed_frame()
    config = load_use_case("telco-churn")
    rows, plan = prepare.prepare_rows(frame, config, primary_key="customer_id", target="churned")
    assert "snapshot_date" not in plan.pii_columns
    assert validate.derive_facts(frame).pii_kinds["snapshot_date"] == ()
    # Before M36 the false PII flag was also what kept the as-of date out of every model; it stays
    # out, now because it is the use case's snapshot date, and is carried with the rows.
    assert "snapshot_date" not in plan.feature_columns
    assert "snapshot_date" in rows.columns


def test_every_shipped_use_case_trains_on_exactly_the_columns_it_did_before() -> None:
    """The synthetic telco-style file of each shipped use case: only the bogus PII flag is gone."""
    from tests.fixtures.make_data import GenerationSpec, generate

    for use_case in ("targeted-advertisement", "telco-churn", "win-back-campaign", "payment-propensity"):
        config = load_use_case(use_case)
        frame = generate(GenerationSpec(use_case, rows=300, variant="clean"))
        # the file as `read_upload` hands it over: every date is still text
        frame = frame.astype({name: str for name in frame.columns if "date" in name})
        target = config.target.column
        assert target is not None
        _, plan = prepare.prepare_rows(frame, config, primary_key="customer_id", target=target)
        assert plan.pii_columns == ()
        assert not any("date" in name for name in plan.feature_columns), use_case


# ---------------------------------------------------------------------------
# The library datasets, re-run through both call sites
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dataset", sorted(LIBRARY_SAMPLES))
def test_the_library_samples_get_identical_or_stricter_flags(dataset: str) -> None:
    """Against the validation report the flags are identical; against prepare they lose only the date.

    Before M36 no library column was reported, and prepare redacted one: online-retail's
    `snapshot_date`, as a phone number. Now both call sites give the same answer, and on every
    library file it is the answer the validation report always gave.
    """
    path = LIBRARY / dataset / "sample.csv"
    frame = pd.read_csv(path)
    key, target = LIBRARY_SAMPLES[dataset]
    reported = {
        str(name): ",".join(kinds) for name, kinds in validate.derive_facts(frame).pii_kinds.items() if kinds
    }
    redacted = prepare._detect_pii(frame, (key, target))
    validated_before, prepared_before = BEFORE[dataset]
    assert set(reported) >= set(validated_before)  # never looser than the report was
    assert set(redacted) == set(reported) - {key, target}
    assert set(redacted) == set(prepared_before) - {"snapshot_date"}


def test_online_retail_no_longer_redacts_its_snapshot_date() -> None:
    """The exact run the library reported: `prepare.json` said `pii_columns: ["snapshot_date"]`."""
    frame = pd.read_csv(LIBRARY / "online-retail" / "sample.csv")
    config = resolve_config("retail-win-back", {}, root=LIBRARY / "configs").config
    _, plan = prepare.prepare_rows(frame, config, primary_key="customer_id", target="reactivated_90d")
    assert plan.pii_columns == ()
    assert not [transform for transform in plan.transforms if transform.kind == "redact"]
    dropped = {column.name: column.reason for column in plan.dropped_columns}
    assert dropped.get("snapshot_date") == "constant"  # what CONSTANT_COLUMN told the user


# ---------------------------------------------------------------------------
# Free text (ruling D5)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def complaints(tmp_path_factory: pytest.TempPathFactory) -> pd.DataFrame:
    tables: RawTables = make_raw(tmp_path_factory.mktemp("raw"), customers=80, usage_rows=400)
    return pd.read_csv(tables.complaints, dtype=str, keep_default_na=False)


def planted(frame: pd.DataFrame) -> list[str]:
    """Every contact make_raw planted in REMARKS, found with the fixture's own shapes."""
    shapes = re.compile(r"\+1-555-555-\d{4}|subscriber\d{5}@example\.invalid")
    return [match for text in frame["REMARKS"] for match in shapes.findall(text)]


def test_free_text_pii_is_found_by_substring_in_the_complaints_table(complaints: pd.DataFrame) -> None:
    remarks = complaints["REMARKS"]
    inferred = infer_column_type(remarks)
    # the whole-cell detector still says the column is not a column of contacts ...
    assert pii.detect_pii(remarks, "REMARKS", inferred) == ()
    # ... and the free-text one finds what is inside it
    assert pii.is_free_text(remarks, inferred)
    assert pii.free_text_pii(remarks, inferred) == ("email", "phone")
    # a code column with a phone-shaped run in it is not prose, so it is not scanned
    ids = pd.Series([f"ACC-{1234567 + i}" for i in range(50)])
    assert pii.free_text_pii(ids, infer_column_type(ids)) == ()


def test_the_profile_masks_contacts_in_samples_and_previews(
    complaints: pd.DataFrame, telco: UseCaseConfig
) -> None:
    profile = profile_of(complaints, telco)
    remarks = next(column for column in profile.columns if column.name == "REMARKS")
    assert remarks.pii_kinds == ()
    assert remarks.free_text_pii_kinds == ("email", "phone")
    serialised = dump_artefact(profile)
    for contact in planted(complaints):
        assert contact not in serialised
    assert "[REDACTED:" in "".join(remarks.sample_values)
    position = remarks.position
    shown = [row[position] for row in profile.preview_rows]
    assert all(cell != REDACTED for cell in shown), "the column is masked, not hidden whole"
    assert any("[REDACTED:" in cell for cell in shown)
    # the words around the contact are kept: a reader still sees what the complaint was about
    assert all(cell.strip() for cell in shown)


def test_a_long_cell_is_masked_before_it_is_cut() -> None:
    """Cutting first would leave half a number: too short for the pattern, long enough for a person."""
    tail = " call 0400 12"
    text = "x" * (ingest.MAX_CELL_CHARS - 1 - len(tail)) + tail + "3 456 about my bill"
    cut_first = ingest._cell_str(text)
    assert cut_first.endswith("0400 12…")  # six digits: below the pattern's floor, not below a reader's
    assert pii.find_in_text(cut_first) == ()
    assert "0400" not in ingest._cell_str(text, mask_free_text=True)


def test_pii_in_free_text_is_a_warning_in_the_validation_report(
    complaints: pd.DataFrame, telco: UseCaseConfig
) -> None:
    report = validate.validate_frame(
        complaints,
        validate.params_from_config(telco, primary_key="CUST_ID"),
        mode=validate.RunMode.SCORE,
        upload_id="upl_complaints",
    )
    found = [check for check in report.checks if check.code == "PII_IN_FREE_TEXT"]
    assert len(found) == 1
    (check,) = found
    assert check.severity is Severity.WARNING
    assert check.acknowledgeable is True
    assert check.column == "REMARKS"
    assert check.details == {"pii_kinds": ["email", "phone"]}
    for contact in planted(complaints):
        assert contact not in dump_artefact(report)


def test_the_phase_one_code_tables_are_untouched() -> None:
    """The new code sits beside the plan's nineteen, where extensions go, not inside them."""
    assert len(VALIDATION_CODES) == 19
    assert "PII_IN_FREE_TEXT" not in VALIDATION_CODES
    assert "PII_IN_FREE_TEXT" in EXTENSION_VALIDATION_CODES
    assert len(validate.CHECK_ORDER) == 19
    assert {spec.code for spec in validate.CHECK_REGISTRY} == VALIDATION_CODES
    assert [spec.code for spec in validate.EXTENSION_REGISTRY] == list(validate.EXTENSION_CHECK_ORDER)
    check = ValidationCheck(code="PII_IN_FREE_TEXT", severity=Severity.WARNING, message="m")
    carried = OnboardingCheck.from_validation_check(check)
    assert carried.code == "PII_IN_FREE_TEXT"


def test_a_masked_sample_value_in_a_message_keeps_no_contact() -> None:
    frame = pd.DataFrame({"remarks": ["please call me on 0400 123 456 about the bill"] * 30})
    facts = validate.derive_facts(frame)
    assert facts.free_text_pii_kinds["remarks"] == ("phone",)
    assert validate.sample_values(frame, "remarks", facts, limit=1) == (
        "please call me on [REDACTED:phone] about the bill",
    )


def test_profiling_and_validation_never_log_a_contact(
    complaints: pd.DataFrame, telco: UseCaseConfig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    profile_of(complaints, telco)
    validate.validate_frame(
        complaints,
        validate.params_from_config(telco, primary_key="CUST_ID"),
        mode=validate.RunMode.SCORE,
        upload_id="upl_complaints",
    )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for contact in planted(complaints):
        assert contact not in logged


def test_the_dataset_review_sample_masks_free_text(complaints: pd.DataFrame) -> None:
    rows = _sample_rows(complaints, pii_columns=frozenset())
    shown = " ".join(row["REMARKS"] for row in rows)
    assert "[REDACTED:" in shown
    for contact in planted(complaints):
        assert contact not in shown
    assert all(row["CUST_ID"] == value for row, value in zip(rows, complaints["CUST_ID"], strict=False))


def test_a_reason_quoting_free_text_is_masked_and_renamed() -> None:
    """Reasons are samples of a row's own cells, so they are masked like every other sample."""
    from engine.column_names import ColumnNames, present_explanations

    reason = Reason(
        feature="remarks",
        value="call me on 0400 123 456",
        contribution=0.2,
        direction=Direction.UP,
        text="remarks = call me on 0400 123 456",
    )
    explained = RowExplanation(primary_key="C-1", score=0.8, reasons=(reason,))
    (shown,) = present_explanations((explained,), ColumnNames(), masked_features=("remarks",))
    assert shown.reasons[0].text == "remarks = call me on [REDACTED:phone]"
    assert shown.reasons[0].value == "call me on [REDACTED:phone]"
    # a column the profile did not flag is shown as it is
    (same,) = present_explanations((explained,), ColumnNames(), masked_features=())
    assert same == explained


def test_llm_inputs_go_through_the_one_redaction() -> None:
    """The root-cause evidence pack redacts complaints with `redaction.redact`, i.e. `engine.pii`."""
    from engine.generative import root_cause

    assert root_cause.redact("ring 0400 123 456")[0] == "ring [REDACTED:phone]"
