"""Plan O M241: GSTIN with its checksum, voter ID, PIN as text, phone to +91 (the design §5.5 G #63)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from engine.doctor.india_ids import (
    detect_india_ids,
    gstin_check_char,
    is_epic,
    is_pin,
    is_valid_gstin,
    looks_like_gstin,
    to_e164_india,
)

VALID_GSTINS = ("27AAPFU0939F1ZV", "29AAGCB7383J1Z4", "24AAACC1206D1ZM")
"""Published examples whose check character is right (Maharashtra, Karnataka, Gujarat)."""
INVALID_GSTINS = ("27AAPFU0939F1ZA", "29AAGCB7383J1Z5", "24AAACC1206D1ZN")
"""The same, one check character changed: the shape fits, the checksum does not."""


@pytest.mark.parametrize("gstin", VALID_GSTINS)
def test_a_valid_gstin_passes_its_checksum(gstin: str) -> None:
    assert gstin_check_char(gstin[:14]) == gstin[14]
    assert is_valid_gstin(gstin) and is_valid_gstin(gstin.lower()) and looks_like_gstin(gstin)


@pytest.mark.parametrize("gstin", INVALID_GSTINS)
def test_an_invalid_checksum_is_told_apart_from_a_valid_one(gstin: str) -> None:
    assert looks_like_gstin(gstin)
    assert not is_valid_gstin(gstin)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "27AAPFU0939F1Z",
        "27AAPFU0939F1ZVX",
        "00AAPFU0939F1ZV",
        "45AAPFU0939F1ZV",
        "27AAPFU0939F0ZV",
        "ABCDE1234F",
    ],
)
def test_a_wrong_shape_or_state_code_is_not_a_gstin(text: str) -> None:
    assert not looks_like_gstin(text) and not is_valid_gstin(text)


def test_the_check_character_needs_fourteen_alphanumerics() -> None:
    with pytest.raises(ValueError):
        gstin_check_char("27AAPFU0939F1")
    with pytest.raises(ValueError):
        gstin_check_char("27AAPFU0939F1-")


def test_epic_and_pin_shapes() -> None:
    assert is_epic("ABC1234567") and is_epic(" abc1234567 ")
    assert not is_epic("AB1234567") and not is_epic("ABC123456") and not is_epic("ABCD1234567")
    assert is_pin("560001") and is_pin("110011")
    assert not is_pin("056001") and not is_pin("56001") and not is_pin("5600011") and not is_pin("56000A")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("+91-98765-43210", "+919876543210"),
        ("0091 98765 43210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("(98765) 43210", "+919876543210"),
        ("98765.43210", "+919876543210"),
        (9876543210, "+919876543210"),
        (9876543210.0, "+919876543210"),
        ("6123456789", "+916123456789"),
        ("5876543210", None),  # Indian mobiles start 6-9
        ("98765 4321", None),  # nine digits
        ("+1 415 555 0100", None),  # another country
        ("+44 98765 43210", None),
        ("98765432100", None),  # eleven digits not starting with a trunk 0
        ("", None),
        (None, None),
        (float("nan"), None),
        (98765.5, None),
        (True, None),
    ],
)
def test_phone_to_plus91(value: object, expected: str | None) -> None:
    assert to_e164_india(value) == expected


def test_two_spellings_of_one_number_join_exactly_after_normalising() -> None:
    left = pd.DataFrame({"mobile": ["+91 98765 43210", "09123456789"], "a": [1, 2]})
    right = pd.DataFrame({"phone": ["9876543210", "91 91234 56789"], "b": [3, 4]})
    left["key"] = left["mobile"].map(to_e164_india)
    right["key"] = right["phone"].map(to_e164_india)
    joined = left.merge(right, on="key")
    assert len(joined) == 2  # exact keys after normalising; nothing fuzzy


def _frame() -> pd.DataFrame:
    rows = 40
    return pd.DataFrame(
        {
            "customer_id": [f"C{i:05d}" for i in range(rows)],
            "gstin": [VALID_GSTINS[i % 3] if i % 10 else INVALID_GSTINS[0] for i in range(rows)],
            "voter_id": [f"XYZ{1000000 + i}" for i in range(rows)],
            "pincode": [str(560001 + i) for i in range(rows)],
            "mobile": [f"+91 98765 {43210 + i}" for i in range(rows)],
            "mobile_num": [9876543210.0 + i for i in range(rows)],
            "amount": [str(100000 + i * 37) for i in range(rows)],
            "plan": ["Basic", "Premium"] * (rows // 2),
        }
    )


def test_detect_india_ids_reads_each_kind_with_counts_only() -> None:
    frame = _frame()
    found = {f.column: f for f in detect_india_ids(frame)}
    assert set(found) == {"gstin", "voter_id", "pincode", "mobile", "mobile_num", "amount"}
    gstin = found["gstin"]
    assert (gstin.kind, gstin.matching, gstin.invalid_checksum) == ("gstin", 40, 4)
    assert gstin.confidence == "check"  # four checksums fail: the column is not sure
    assert gstin.personal and gstin.keep_as_text
    assert found["voter_id"].kind == "epic" and found["voter_id"].confidence == "sure"
    pin = found["pincode"]
    assert (pin.kind, pin.confidence, pin.keep_as_text, pin.personal) == ("pin", "sure", True, False)
    assert (
        found["mobile"].kind == "phone_in"
        and found["mobile"].join_key
        and found["mobile"].confidence == "sure"
    )
    assert found["mobile_num"].kind == "phone_in"  # a phone read as floats is read by its value
    # Six-digit amounts look like PIN codes; without a header that says postcode it is a question, not a fact.
    assert found["amount"].kind == "pin" and found["amount"].confidence == "check"
    text = json.dumps([f.as_json() for f in found.values()])
    for secret in (*VALID_GSTINS, "XYZ1000000", "560001", "98765"):
        assert secret not in text


def test_an_all_valid_gstin_column_with_its_header_is_sure() -> None:
    frame = pd.DataFrame({"gst_no": list(VALID_GSTINS) * 10})
    (finding,) = detect_india_ids(frame)
    assert (finding.kind, finding.invalid_checksum, finding.confidence) == ("gstin", 0, "sure")


def test_dates_booleans_and_fractional_numbers_are_never_read_as_ids() -> None:
    frame = pd.DataFrame(
        {
            "when": pd.to_datetime(["2024-01-01"] * 5),
            "flag": [True, False, True, False, True],
            "spend": [987654.5, 123456.25, 654321.75, 111111.5, 222222.5],
        }
    )
    assert detect_india_ids(frame) == ()
