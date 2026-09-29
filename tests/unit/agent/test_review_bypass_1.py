"""Review finding 1: a column named in `agent.always_hide_columns` shows its values in the advisor's reasons.

`docs/AGENTS.md` §7.7: a column in `always_hide_columns` "never yields a value in either mode ... This applies
to every tool ... an advisor sentence that names such a column in quotes shows none of its quoted examples."
The advisor writes the examples in a proposal's `reason` (`The same value is written differently ('JANE ROE' →
'Jane Roe' ...)`) and the column name only in its `title`. `Egress.text` looks for the hidden column *inside one
string*, so the `reason` (which does not name the column) goes to the model with the cells of the hidden
column, in the default `masked_data` mode, through the real `chat_turn`.
"""

from __future__ import annotations

import pandas as pd
import pytest

from engine.agent.session import start_session
from tests.unit.agent.egress_canary import ToolingClient, canary_context, run_session
from tests.unit.agent.helpers import synthetic


def _frame() -> pd.DataFrame:
    frame = synthetic("clean", rows=400)
    frame["shop_type"] = [
        "Jane Roe",
        "JANE ROE",
        "Kids",
        "kids",
    ] * 100  # spelled several ways: the advisor quotes them
    frame["invoice_note"] = [
        "1,200",
        "3,400",
        "5,600",
        "7,800",
    ] * 100  # stored as text: the advisor quotes them
    return frame


@pytest.mark.parametrize(
    ("hidden", "cells"),
    [("shop_type", ("Jane Roe", "JANE ROE")), ("invoice_note", ("1,200", "3,400", "5,600"))],
)
def test_advisor_reason_does_not_carry_values_of_an_always_hidden_column(
    hidden: str, cells: tuple[str, ...]
) -> None:
    ctx = canary_context(mode="masked_data", frame=_frame(), always_hide=[hidden])
    client = ToolingClient([{"action": "get_profile", "args": {}}], per_turn=1)
    session = start_session(ctx, session_id="review-1")
    assert any(
        hidden in p.title for p in session.proposals
    ), "the advisor is expected to propose a fix for the column"
    run_session(ctx, client, started=session)
    prompts = "\n".join(client.sent_text)
    assert [
        cell for cell in cells if cell in prompts
    ] == [], (
        f"{hidden} is in always_hide_columns but its cells reached the AI service in the advisor's reason"
    )
