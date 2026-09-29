"""Review finding 10: `_hold` protects a date or a decimal *inside* a longer number, and the rest is not scanned.

`egress._scan` sets aside dates, UUIDs and "decimals with a short whole part" before the scanners run and puts
them back at the end (§7.7: `12345.678` is kept on purpose). The hold is applied to any match, also when the
match is a piece of a longer digit run:

* `9876.543210` (a 10-digit mobile number written with a dot after four digits) is a "decimal" with a
  four-digit whole part, so it is held and nothing is scanned - the number is sent. Only the 5.5 split
  (`98765.43210`) is special-cased in `_keep_decimal`.
* `98765 43210.0` (or `+91 98765 43210.0`, a phone column that went through a float cast) holds `43210.0`
  as a decimal, and the phone scanner is left with `98765 ` and no longer sees a number.
"""

from __future__ import annotations

import re

import pytest

from engine.agent import egress


@pytest.mark.parametrize("value", ["9876.543210", "98765 43210.0", "+91 98765 43210.0"])
def test_a_phone_number_is_not_kept_because_a_part_of_it_looks_like_a_decimal(value: str) -> None:
    masked = egress.mask_value(value)
    assert re.search(r"\d{4}", re.sub(r"\[REDACTED:[a-z]+\]", "", masked)) is None, masked
