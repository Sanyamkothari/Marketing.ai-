"""Review finding 6: e-mail-shaped identifiers the e-mail pattern does not read.

`engine.pii._EMAIL` needs a dot and an ASCII-letter top-level domain of two or more characters after the `@`.
That leaves out (a) UPI virtual payment addresses, which are `name@bank` with no dot (`jane.roe@okhdfcbank`,
`ravi1985@oksbi`) - a person's financial identifier, in a product built for the Indian market where PAN and
Aadhaar are already scanned - and any host without a dot (`jane.roe@intranet`); and (b) addresses on an
internationalised top-level domain (`.рф`, `.भारत`, `.テスト`), which are real and in use. The local part (the
person's name or handle) is sent as it is in `masked_data` mode, and the `assert_clean` backstop uses the
same pattern.

Documented limit? §7.7 says obfuscated addresses (`jane dot roe at example dot test`) are not recognised;
it does not say that a dotless host or a non-ASCII top-level domain is not.
"""

from __future__ import annotations

import pytest

from engine.agent import egress

ADDRESSES = [
    ("jane.roe", "jane.roe@okhdfcbank"),  # UPI
    ("ravi1985", "ravi1985@oksbi"),  # UPI
    ("jane.roe", "jane.roe@intranet"),  # host without a dot
    ("ravi.kumar", "ravi.kumar@example.भारत"),  # IDN top-level domain
    ("ivan.petrov", "ivan.petrov@example.рф"),
    ("taro.yamada", "taro.yamada@example.テスト"),
]


@pytest.mark.parametrize(("local", "address"), ADDRESSES, ids=[a for _, a in ADDRESSES])
def test_mask_value_masks_the_local_part_of_an_address(local: str, address: str) -> None:
    masked = egress.mask_value(f"Contact {address} please")
    assert local not in masked, masked


@pytest.mark.parametrize(("local", "address"), ADDRESSES, ids=[a for _, a in ADDRESSES])
def test_the_backstop_masks_the_local_part_of_an_address(local: str, address: str) -> None:
    cleaned, _ = egress.assert_clean(f'{{"value": "Contact {address} please"}}')
    assert local not in cleaned, cleaned
