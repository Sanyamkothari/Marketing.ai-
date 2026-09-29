"""Review finding 3 (leak of personal data): `find_format_issues` returns a dict keyed by cell values
(`params.merge`), and the gate lets those keys through in `summaries_only` and for
`always_hide_columns`.

`docs/AGENTS.md` §11 documents that a dict keyed by cell values is masked "only when the key is not
identifier-shaped; a lower-case single word key ... is read as a label", and adds: "Tools return
lists of `{value, rows}` instead; the canary walks every tool in the registry to keep that true."
That is not true of `find_format_issues`: for a column with spelling variants the issue carries
`params: {"strip": true, "merge": {"<spelling as written>": "<canonical>"}}`. The values of `merge`
are masked (shape / hidden marker) but its keys are not: a lower-case single word passes as a
"label" in any mode, and a lower-case multi-word key is only run through the pattern scanners, which
is no protection in `masked_data` for a column in `always_hide_columns`.

The frame is fake. The test runs the real `chat_turn` and searches every captured prompt.
"""

from __future__ import annotations

import pandas as pd
import pytest

from tests.unit.agent.egress_canary import ToolingClient, canary_context, run_session

ROWS = 300
SPELLINGS = ["zelda", "Zelda", "Zelda", "quill roe", "Quill Roe", "Quill Roe"]


@pytest.mark.parametrize(
    ("mode", "always_hide"),
    [("summaries_only", ()), ("masked_data", ("account_holder",)), ("summaries_only", ("account_holder",))],
    ids=["summaries_only", "always_hide_masked_data", "always_hide_summaries_only"],
)
def test_format_issue_merge_keys_do_not_carry_cells(mode: str, always_hide: tuple[str, ...]) -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i:05d}" for i in range(ROWS)],
            "account_holder": [SPELLINGS[i % len(SPELLINGS)] for i in range(ROWS)],
            "converted_30d": [1 if (i * 7) % 10 < 3 else 0 for i in range(ROWS)],
        }
    )
    ctx = canary_context(mode=mode, always_hide=always_hide, frame=frame)
    client = ToolingClient([{"action": "find_format_issues", "args": {}}])
    session, _ = run_session(ctx, client)

    sent = [item.preview for message in session.transcript for item in message.sent]
    assert any("category_variants" in preview for preview in sent), "the tool was not called"
    prompts = "\n".join(client.sent_text)
    leaked = [word for word in ("zelda", "quill roe") if word in prompts]
    assert (
        leaked == []
    ), f"{mode}, always_hide={always_hide}: cells in a dict key reached the prompt: {leaked}"
