"""Review finding 13: an identifier-shaped dict key is never scanned.

`Egress._key` returns any key that matches `[a-z][a-z0-9_]{0,39}` as it is, without a scanner. §7.7 lists
"a lower-case single word key (`{"jane": 3}`)" as the known limit of a dict keyed by cell values; the pattern
is wider than a word, though: it takes letters, digits and underscores, so a key that carries a phone number,
a card or an API key (`user_9876543210`, `card_4111111111111111`, `zk_live_51hxabcdef1234567890`) passes  (secret-scan: allow (a fake key the masking tests plant on purpose))
too, in `masked_data` and even though the same string as a *value* is masked. Reachable when a tool returns
`{cell value: count}` (the canary asserts none does today, so this is a latent hole in the walk, not a
current leak). Low severity.
"""

from __future__ import annotations

import json

import pytest

from engine.agent import egress
from engine.agent.config import DataAccess

FAKE_KEY = (
    "zk_live_51hxabcdef1234567890"  # secret-scan: allow (a fake key the masking tests plant on purpose)
)


@pytest.mark.parametrize("key", ["user_9876543210", "card_4111111111111111", FAKE_KEY])
def test_a_dict_key_that_holds_a_number_or_a_key_is_masked_like_a_value(key: str) -> None:
    gate = egress.Egress.build(["customer_id"], mode=DataAccess.MASKED_DATA)
    assert egress.mask_value(key) != key, "control: the same text as a value is masked"
    shown = json.dumps(gate.prepare({"counts": {key: 3}}))
    assert key not in shown, shown
