"""Review finding 4: phone numbers with a bracketed area code the phone pattern does not read.

`engine.pii._PHONE` accepts a bracketed area code of two to four digits only, and `(495) 123-45-67` (groups
of two after the area code) matches none of its three branches. `+91 (98765) 43210` - the Indian mobile
number with its five-digit block in brackets - and `+7 (495) 123-45-67` therefore reach the AI service as
written, although `+91 98765 43210` is masked. No other scanner (card, 9-digit run) sees them either, so the
`assert_clean` backstop lets them through too.
"""

from __future__ import annotations

import re

from engine.agent import egress

PHONES = [
    "+91 (98765) 43210",
    "+91 [98765] 43210",
    "+7 (495) 123-45-67",
    "8 (495) 123-45-67",
]


def _digits_left(text: str) -> bool:
    return re.search(r"\d{3}", re.sub(r"\[REDACTED:[a-z]+\]", "", text)) is not None


def test_mask_value_masks_a_phone_with_a_bracketed_area_code() -> None:
    leaked = {phone: egress.mask_value(phone) for phone in PHONES if _digits_left(egress.mask_value(phone))}
    assert not leaked, leaked


def test_backstop_masks_a_phone_with_a_bracketed_area_code() -> None:
    leaked = {}
    for phone in PHONES:
        cleaned, _ = egress.assert_clean(f'{{"value": "{phone}"}}')
        if _digits_left(cleaned):
            leaked[phone] = cleaned
    assert not leaked, leaked
