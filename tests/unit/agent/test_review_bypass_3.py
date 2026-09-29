"""Review finding 3: a typographic dash instead of `-` defeats every digit scanner.

`egress._normalise` folds full-width characters (NFKC) and drops invisible ones, but U+2010..U+2015 and
U+2212 (hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar, minus) survive NFKC as
themselves (U+2011 only becomes U+2010), and the phone, card, Aadhaar and 9-digit-run scanners accept only
ASCII `-`, ` ` and `.` between digit groups. A card, a phone number or an Aadhaar number typed or pasted
with an en dash (word processors and spreadsheets produce these) goes to the AI service unmasked, and the
`assert_clean` backstop does not catch it either.
"""

from __future__ import annotations

import re

import pytest

from engine.agent import egress

DASHES = ["‐", "‑", "‒", "–", "—", "―", "−"]
VALUES = {
    "card": "4111{d}1111{d}1111{d}1111",  # a test card (Luhn-valid)
    "indian mobile": "98765{d}43210",
    "indian mobile with country code": "+91{d}98765{d}43210",
    "us phone": "555{d}123{d}4567",
    "aadhaar": "2345{d}6789{d}0123",
}


def _digits_left(text: str) -> bool:
    return re.search(r"\d{4}", re.sub(r"\[REDACTED:[a-z]+\]", "", text)) is not None


@pytest.mark.parametrize("kind", list(VALUES))
def test_mask_value_masks_digit_groups_joined_by_any_dash(kind: str) -> None:
    leaked = {
        f"U+{ord(dash):04X}": masked
        for dash in DASHES
        if _digits_left(masked := egress.mask_value(VALUES[kind].format(d=dash)))
    }
    assert not leaked, f"{kind} went through unmasked with these dashes: {leaked}"


def test_the_backstop_catches_a_card_written_with_any_dash() -> None:
    leaked = {}
    for dash in DASHES:
        cleaned, _ = egress.assert_clean(f'{{"value": "4111{dash}1111{dash}1111{dash}1111"}}')
        if _digits_left(cleaned):
            leaked[f"U+{ord(dash):04X}"] = cleaned
    assert not leaked, leaked
