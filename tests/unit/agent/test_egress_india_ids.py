"""Plan O M241: the egress gate masks a GSTIN and a voter ID (EPIC) whole, before any prompt.

Before M241 the gate's PAN scanner took the PAN inside a GSTIN and left the state code and the check characters
(`27[REDACTED:pan]1ZV`), and the phone scanner took a voter ID's digits and left its letters (`XYZ[REDACTED:phone]`).
Now each is one marker. The canary (`egress_canary.py`) plants both in an innocent-looking column.
"""

from __future__ import annotations

import pytest

from engine.agent.config import DataAccess
from engine.agent.egress import SAFE_LITERALS, assert_clean, mask_value, scrub
from tests.unit.agent.egress_canary import EPIC, GSTIN

GSTINS = ("27AAPFU0939F1ZV", "29aagcb7383j1z4", "24AAACC1206D1ZN")  # valid, lower case, wrong check character
EPICS = ("XYZ1234567", "abc7654321")


@pytest.mark.parametrize("gstin", GSTINS)
def test_a_gstin_is_masked_whole(gstin: str) -> None:
    assert mask_value(gstin) == "[REDACTED:gstin]"
    assert scrub(f"GST no. {gstin}, billed") == "GST no. [REDACTED:gstin], billed"
    cleaned, count = assert_clean(f'{{"note": "gst {gstin}"}}')
    assert cleaned == '{"note": "gst [REDACTED:gstin]"}' and count == 1


@pytest.mark.parametrize("epic", EPICS)
def test_a_voter_id_is_masked_whole(epic: str) -> None:
    assert mask_value(epic) == "[REDACTED:epic]"
    assert scrub(f"voter card {epic} seen") == "voter card [REDACTED:epic] seen"


def test_the_canary_values_are_the_ones_masked() -> None:
    assert mask_value(f"{GSTIN} / {EPIC}") == "[REDACTED:gstin] / [REDACTED:epic]"


@pytest.mark.parametrize(
    "text",
    ["ABCDE1234F", "+91 98765 43210", "2345 6789 0123", "jane.roe@example.test"],
)
def test_the_other_scanners_still_name_their_kind(text: str) -> None:
    masked = mask_value(text)
    assert "gstin" not in masked and "epic" not in masked and masked.startswith("[REDACTED:")


@pytest.mark.parametrize(
    "text", ["XYZ12345678", "XYZ123456", "27AAPFU0939F1Z", "plan B2B", "Q3 2024", "AB1234567C"]
)
def test_near_misses_are_not_these_ids(text: str) -> None:
    masked = mask_value(text)
    assert "[REDACTED:gstin]" not in masked and "[REDACTED:epic]" not in masked


def test_summaries_only_shows_the_shape_and_nothing_of_the_value() -> None:
    shape = mask_value(GSTIN, DataAccess.SUMMARIES_ONLY)
    assert shape and set(shape) <= set("9Aa")
    assert "gstin" in SAFE_LITERALS and "epic" in SAFE_LITERALS


@pytest.mark.parametrize("gstin", ["07DELA12345B1D3", "07dela12345b1de", "27AAPFU0939F1AV"])
def test_a_gstin_with_a_tan_body_or_another_14th_character_is_masked_whole(gstin: str) -> None:
    """Review item 6: a tax deductor's GSTIN (a TAN inside) and a 14th character other than `Z` passed unmasked."""
    assert mask_value(f"TDS {gstin}") == "TDS [REDACTED:gstin]"
