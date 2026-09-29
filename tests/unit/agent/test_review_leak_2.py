"""Review finding 2 (leak of personal data): in `summaries_only`, a value with an apostrophe in it
goes to the AI service as written, through the advisor's suggestions in the `state` block.

`docs/AGENTS.md` §7.7: in `summaries_only` "no cell at all" reaches the model; "in the advisor's
sentences a quoted piece that is not a column name is a cell example and becomes its shape".
The gate finds a quoted piece with `_QUOTED = (?<!\\w)'([^'\\n]{1,120}?)'(?!\\w)`, and the advisor quotes
examples with `quoted()`, which does not escape an apostrophe. `'o'brien jane'` therefore matches
nothing and stays as written, while `'doe kim'` next to it becomes `'aaa aaa'`. Names such as
O'Brien and D'Souza are exactly the values a person would want kept out.

The frame is fake. The test runs the real `chat_turn` and searches every captured prompt.
"""

from __future__ import annotations

import pandas as pd

from tests.unit.agent.egress_canary import ToolingClient, canary_context, run_session

ROWS = 300
SPELLINGS = ["O'Brien Jane", "o'brien jane", "D'Souza Kim", "d'souza kim"]


def test_summaries_only_hides_a_value_with_an_apostrophe_in_the_advisor_sentences() -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i:05d}" for i in range(ROWS)],
            "account_holder": [SPELLINGS[i % len(SPELLINGS)] for i in range(ROWS)],
            "converted_30d": [1 if (i * 7) % 10 < 3 else 0 for i in range(ROWS)],
        }
    )
    ctx = canary_context(mode="summaries_only", frame=frame)
    client = ToolingClient([{"action": "get_profile", "args": {}}])
    session, _ = run_session(ctx, client)

    assert any("account_holder" in proposal.title for proposal in session.proposals)
    prompts = "\n".join(client.sent_text)
    leaked = [word for word in ("Brien", "brien", "Souza", "souza") if word in prompts]
    assert leaked == [], f"summaries_only sent parts of cells to the AI service: {leaked}"
