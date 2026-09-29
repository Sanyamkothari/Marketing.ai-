"""Review finding 8: a Luhn-valid card with a separator the card scanner does not read.

`egress._CARD` allows one optional ASCII space or hyphen between two digits (`\\d(?:[ \\-]?\\d){12,18}`). A card
written `4111 - 1111 - 1111 - 1111` (spaced hyphens), `4111_1111_1111_1111`, `4111/1111/1111/1111` or
`4111,1111,1111,1111` is not one run to it, and no other scanner takes a four-digit group on its own: the
whole card reaches the AI service. (Dot-separated cards are masked, but only because the phone pattern reads
them as a phone number.)
"""

from __future__ import annotations

import re

import pytest

from engine.agent import egress

CARDS = [
    "4111 - 1111 - 1111 - 1111",
    "4111_1111_1111_1111",
    "4111/1111/1111/1111",
    "4111,1111,1111,1111",
    "4111 , 1111 , 1111 , 1111",
]


@pytest.mark.parametrize("card", CARDS)
def test_a_card_with_any_common_separator_is_masked(card: str) -> None:
    masked = egress.mask_value(f"paid with {card}")
    assert re.search(r"\d{4}", re.sub(r"\[REDACTED:[a-z]+\]", "", masked)) is None, masked
