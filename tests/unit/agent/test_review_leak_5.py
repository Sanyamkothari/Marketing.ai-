"""Review finding 5 (leak of personal data, stored and returned by the API): the last check
(`assert_clean`) is the only thing that masks a phone-like `mean` / `median`, and what it masks is
still recorded unmasked in `ChatMessage.sent[].preview`.

`docs/AGENTS.md` §7.7: `sent` holds "the exact masked payload" of each tool result, and the canary
says `EGRESS_LATE_MASK` "never fires ... a non-zero count means a bug". Here it fires:

* `egress._number` keeps a number whose key is `mean` or `median` whatever its size, so a column
  whose values are ten-digit phone numbers stored as floats (the profile does not look at those, and
  a header like `callback_ref` names nothing) puts its exact mean and its median - with an odd number
  of rows, one person's phone number - into the payload;
* the prompt is saved by `assert_clean`, which masks it late (`EGRESS_LATE_MASK` in the turn log);
* `SentItem.preview` is taken from the payload *before* that last check (`loop._record`), so the
  session file and every `GET/POST .../agent-session*` answer carry the unmasked number to a Viewer.

The first assertion (the prompt) holds today; the second (what is stored and returned) does not.
The frame is fake (555 numbers). The test runs the real `chat_turn`.
"""

from __future__ import annotations

import pandas as pd

from tests.unit.agent.egress_canary import ToolingClient, canary_context, run_session, stored_text

PHONES = [5551234567.0, 5559876543.0, 5550001111.0, 5552223333.0, 5554445555.0]  # median: 5552223333


def test_a_phone_like_median_is_not_stored_in_the_sent_preview() -> None:
    frame = pd.DataFrame(
        {
            "callback_ref": PHONES,
            "grade": ["gold", "silver", "bronze", "gold", "silver"],
            "converted_30d": [1, 0, 0, 1, 0],
        }
    )
    ctx = canary_context(mode="masked_data", frame=frame)
    assert not ctx.profile.columns[0].pii_kinds  # the profile does not flag it (float, neutral header)
    client = ToolingClient([{"action": "describe_numbers", "args": {"column": "callback_ref"}}])
    session, _ = run_session(ctx, client)

    planted = "5552223333"
    assert planted not in "\n".join(client.sent_text), "the last check no longer saves the prompt"
    stored = stored_text(session)
    assert planted not in stored, "a phone number is stored in ChatMessage.sent and returned by the API"
