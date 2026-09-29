"""Review finding 9: a PAN written with spaces or hyphens.

`engine.pii` scans PAN as `(?i)[A-Z]{5}\\d{4}[A-Z]`, one run. `ABCDE 1234 F` and `ABCDE-1234-F` (how the number
is often typed from a card or a form) match neither that nor any other scanner (the four digits are too
short for the phone and the 9-digit-run patterns), so a PAN - one of the identifiers the platform names as
personal data - is sent as written. Lower-case `abcde1234f` is masked; the separated forms are not.
"""

from __future__ import annotations

import pytest

from engine.agent import egress


@pytest.mark.parametrize("pan", ["ABCDE 1234 F", "ABCDE-1234-F", "ABCDE 1234F", "ABCDE1234 F"])
def test_a_pan_with_separators_is_masked(pan: str) -> None:
    masked = egress.mask_value(f"PAN {pan}")
    assert "1234" not in masked and "ABCDE" not in masked, masked
