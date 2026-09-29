"""Review finding 7: scan order - the URL scanner eats the domain of an e-mail address that is followed by `/`.

`egress._scan` runs the URL scanner before the e-mail scanner. Its second alternative
(`\\b(?:[a-z0-9\\-]+\\.)+[a-z]{2,24}/...`, a host followed by a path) matches `example.com/` inside
`jane.roe@example.com/joe.bloggs@example.com`, so what is left is `jane.roe@[REDACTED:url]`: the e-mail scanner
no longer sees an `@ ... .tld` and the local part - the person's name or handle - is sent. Two addresses
separated by `/` (a common way to list contacts) and `address/department` are the natural cases; the same
happens in `assert_clean`, which runs the same scanners in the same order.
"""

from __future__ import annotations

import pytest

from engine.agent import egress

CASES = [
    ("jane.roe", "jane.roe@example.com/joe.bloggs@example.com"),
    ("jane.roe", "jane.roe@example.com/sales"),
    ("jane.roe", "jane.roe@example.com/+91 98765 43210"),
]


@pytest.mark.parametrize(("local", "value"), CASES, ids=[v for _, v in CASES])
def test_the_local_part_of_an_address_followed_by_a_slash_is_masked(local: str, value: str) -> None:
    assert local not in egress.mask_value(value)


def test_the_backstop_has_the_same_hole() -> None:
    cleaned, _ = egress.assert_clean('{"value": "jane.roe@example.com/sales"}')
    assert "jane.roe" not in cleaned, cleaned
