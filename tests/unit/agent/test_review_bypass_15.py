"""Review finding 15: the backstop masks a 7- or 8-digit count in a tool result, as a phone number.

`Egress._number` lets an integer through when its key is a count (`rows`, `total_matching_rows`, `empty`,
...) whatever its size, and masks a non-count number only from nine digits. `assert_clean` then re-scans the
rendered JSON prompt with the phone pattern, which reads any run of seven or more digits: a file of a million
rows (`"rows": 1000000`, seven digits) or a match count of 1,500,000 is sent as `"rows": [REDACTED:phone]`,
the model cannot say how big the file is, the JSON is invalid, and every turn is logged `EGRESS_LATE_MASK`,
the alarm that is supposed to mean "something leaked upstream". Same class as the earlier
`"maximum": 100000000` schema bug, this time for data (`test_prompt_static_clean.py` only covers the static
prompt).

Through the real `chat_turn` with `get_profile` on a profile that says two million rows.
"""

from __future__ import annotations

import re

import pytest

from engine.agent import egress
from engine.agent.egress import EGRESS_LATE_MASK
from engine.agent.session import start_session
from tests.unit.agent.egress_canary import ToolingClient, canary_context, run_session
from tests.unit.agent.helpers import synthetic


@pytest.mark.parametrize("rows", [1_000_000, 2_000_000, 12_345_678])
def test_a_large_row_count_reaches_the_model_intact_and_raises_no_alarm(rows: int) -> None:
    ctx = canary_context(mode="masked_data", frame=synthetic("clean", rows=300))
    ctx = ctx.__class__(**{**ctx.__dict__, "profile": ctx.profile.model_copy(update={"row_count": rows})})
    client = ToolingClient([{"action": "get_profile", "args": {}}], per_turn=1)
    _, turns = run_session(ctx, client, started=start_session(ctx, session_id="review-15"))
    prompt = client.sent_text[-1]
    assert re.search(rf'"rows": {rows}\b', prompt), "the row count the tool returned is not in the prompt"
    assert EGRESS_LATE_MASK not in turns[0].reply.turn.error_codes


def test_the_backstop_leaves_a_counted_number_alone() -> None:
    prompt = '{"rows": 2000000, "total_matching_rows": 1500000}'
    cleaned, matches = egress.assert_clean(prompt)
    assert (cleaned, matches) == (prompt, 0)
