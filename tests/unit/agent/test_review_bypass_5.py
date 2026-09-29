"""Review finding 5: invisible characters that are not Unicode category `C*` split a digit run.

`untrusted.clean_text` (used by `egress._normalise`) drops categories Cc, Cf, Cs, Co, Cn, Zl, Zp. Characters
that render as nothing but are not in those categories stay: U+034F combining grapheme joiner and the
variation selectors U+FE00..U+FE0F (Mn), the Hangul fillers U+115F, U+1160, U+3164, U+FFA0 (Lo) and the braille
blank U+2800 (So). A phone, card or Aadhaar number with one of them between digit groups (or a number written
in keycap emoji: digit, U+FE0F, U+20E3) looks the same to a person and is not one run to any scanner, so it is
sent unmasked; the `assert_clean` backstop does not normalise, so it misses it too. (U+200B, U+200D, U+00AD
and U+FEFF are Cf and are handled.)
"""

from __future__ import annotations

import re

import pytest

from engine.agent import egress

INVISIBLE = {
    "U+034F combining grapheme joiner": "͏",
    "U+FE0F variation selector-16": "️",
    "U+3164 hangul filler": "ㅤ",
    "U+2800 braille blank": "⠀",
}
NUMBERS = {
    "phone": "98765{c}43210",
    "card": "4111{c}1111{c}1111{c}1111",
    "aadhaar": "2345{c}6789{c}0123",
}


def _digits_left(text: str) -> int:
    return len(re.findall(r"\d", re.sub(r"\[REDACTED:[a-z]+\]", "", text)))


@pytest.mark.parametrize("kind", list(NUMBERS))
def test_an_invisible_non_format_character_between_digit_groups_does_not_hide_the_number(kind: str) -> None:
    leaked = {
        name: egress.mask_value(NUMBERS[kind].format(c=char))
        for name, char in INVISIBLE.items()
        if _digits_left(egress.mask_value(NUMBERS[kind].format(c=char))) > 3
    }
    assert not leaked, f"{kind}: {leaked}"


def test_a_phone_number_written_in_keycap_digits_is_masked() -> None:
    keycaps = "".join(f"{digit}️⃣" for digit in "9876543210")
    assert _digits_left(egress.mask_value(keycaps)) <= 3, egress.mask_value(keycaps)
