"""Review finding 6 (leak of personal data, partial secret): a look tool cuts a cell to 60 characters
*before* the gate's own scanners see it, so the head of an API key, a card or an IP address survives.

`docs/AGENTS.md` §7.7 says "A cell is masked whole and then cut". That is the gate's rule (80
characters). The look tools cut first (`tools._masked`: `redact_cells` - e-mail, phone, PAN, Aadhaar
only - then `[:60]`; `describe_duplicates` cuts at 40, `inspect_column` examples at 80). The gate
then scans the *fragment*, and its card / IBAN / IP / token scanners need the whole secret: a token is
only recognised from 20 characters, a card from nine digits. A note that is 43 characters long and then
holds an API key sends the first 16 characters of that key (`zk_live_51HxAbCd`), with a longer filler
up to 19 characters. The canary never sees this because its notes are short enough for the key to
stay whole.

The frame is fake. The test runs the real `chat_turn` and searches every captured prompt.
"""

from __future__ import annotations

import pandas as pd

from tests.unit.agent.egress_canary import ToolingClient, canary_context, every_call, run_session

ROWS = 120
TOKEN = (
    "zk_live_51HxAbCdEf123456789012345"  # secret-scan: allow (a fake key the masking tests plant on purpose)
)
FILLER = "Customer wrote a long note about the order:"  # 43 characters
NOTES = [f"{FILLER} {TOKEN} thanks", f"{FILLER} 4111 1111 1111 1111", "short note", "another short note"]


def test_a_cell_is_masked_whole_before_a_tool_cuts_it() -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i:05d}" for i in range(ROWS)],
            "note": [NOTES[i % len(NOTES)] for i in range(ROWS)],
            "converted_30d": [1 if (i * 7) % 10 < 3 else 0 for i in range(ROWS)],
        }
    )
    ctx = canary_context(mode="masked_data", frame=frame)
    client = ToolingClient(every_call(list(frame.columns)))
    run_session(ctx, client)

    prompts = "\n".join(client.sent_text)
    assert FILLER in prompts, "the notes were not looked at"
    fragments = [part for part in ("51HxAbCd", "zk_live_51") if part in prompts]
    assert fragments == [], f"the head of an API key reached the AI service: {fragments}"
