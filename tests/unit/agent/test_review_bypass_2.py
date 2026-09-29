"""Review finding 2: `summaries_only` sends a cell with an apostrophe in it, verbatim.

`docs/AGENTS.md` §7.7: in `summaries_only` "no cell at all: every cell-derived string is its shape ... In the
advisor's sentences a quoted piece that is not a column name is a cell example and becomes its shape."
`egress._QUOTED` is `(?<!\\w)'([^'\\n]{1,120}?)'(?!\\w)`: a cell that contains an apostrophe (`Men's Wear`,
`O'Brien`) is quoted by `untrusted.quoted` as `'Men's Wear'`, which that pattern cannot read as one piece, so
the advisor's reason keeps it as written. Through the real `chat_turn`, in `summaries_only`.
"""

from __future__ import annotations

import pandas as pd

from engine.agent import egress
from engine.agent.config import DataAccess
from engine.agent.session import start_session
from engine.agent.untrusted import quoted
from tests.unit.agent.egress_canary import ToolingClient, canary_context, run_session
from tests.unit.agent.helpers import synthetic


def test_a_quoted_cell_with_an_apostrophe_becomes_its_shape_in_summaries_only() -> None:
    gate = egress.Egress.build(["shop_type"], mode=DataAccess.SUMMARIES_ONLY)
    cell = "Jane O'Roe"
    sentence = f"The same value is written differently ({quoted(cell)})."
    shown = gate.text(sentence, examples=True)
    assert "Jane" not in shown and "Roe" not in shown, shown


def test_summaries_only_prompt_has_no_cell_of_a_column_the_advisor_quotes() -> None:
    frame: pd.DataFrame = synthetic("clean", rows=400)
    frame["shop_type"] = ["Men's Wear", "MEN'S WEAR", "Kids", "kids"] * 100
    ctx = canary_context(mode="summaries_only", frame=frame)
    client = ToolingClient([{"action": "get_profile", "args": {}}], per_turn=1)
    session = start_session(ctx, session_id="review-2")
    assert any(
        "Men's Wear" in p.reason for p in session.proposals
    ), "the advisor is expected to quote the cells"
    run_session(ctx, client, started=session)
    prompts = "\n".join(client.sent_text)
    assert "Men's Wear" not in prompts and "MEN'S WEAR" not in prompts
