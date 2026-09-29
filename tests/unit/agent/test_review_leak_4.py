"""Review finding 4 (leak of personal data): in `summaries_only`, `describe_numbers` and
`inspect_column` send `median` as a real number.

`docs/AGENTS.md` §7.7: in `summaries_only` "no cell at all" reaches the model, and "numbers that are
cells (`minimum`, list items) are shapes too. Counts, averages, types and check messages stay."
`egress._STAT_KEY` keeps every key called `mean|median|std|var|average|avg|sum`. A mean is an
aggregate, but the median of an odd number of rows *is* one person's value - the same number the
tool also reports as `p50` (which the gate does turn into a shape) and as the middle value of
`histogram`. So the pay of one (fake) employee reaches the AI service in a mode that promises no cell
values, and the gate is inconsistent about it: `quantiles.p50` shape, `median` full value.

The same key list exempts `mean` and `median` from the nine-digit rule in `masked_data`
(see test_review_leak_5).

The frame is fake. The test runs the real `chat_turn` and searches every captured prompt.
"""

from __future__ import annotations

import pandas as pd

from tests.unit.agent.egress_canary import ToolingClient, canary_context, run_session

PAY = [41250, 52310, 61775, 73890, 88420, 95105, 120340]  # seven people; the median is 73890


def test_summaries_only_does_not_send_the_median_of_a_numeric_column() -> None:
    frame = pd.DataFrame(
        {
            "pay": PAY,
            "grade": ["gold", "silver", "bronze", "gold", "silver", "bronze", "gold"],
            "converted_30d": [1, 0, 0, 1, 0, 0, 1],
        }
    )
    ctx = canary_context(mode="summaries_only", frame=frame)
    client = ToolingClient(
        [
            {"action": "describe_numbers", "args": {"column": "pay"}},
            {"action": "inspect_column", "args": {"column": "pay"}},
        ]
    )
    session, _ = run_session(ctx, client)

    assert any(item.tool == "describe_numbers" for m in session.transcript for item in m.sent)
    prompts = "\n".join(client.sent_text)
    leaked = [str(value) for value in PAY if str(value) in prompts]
    assert leaked == [], f"summaries_only sent a value of the column 'pay' to the AI service: {leaked}"
