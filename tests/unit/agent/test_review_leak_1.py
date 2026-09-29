"""Review finding 1 (leak of personal data): `agent.always_hide_columns` values reach the AI service
through the advisor's suggestions in the `state` block.

`docs/AGENTS.md` §7.7 says a column named in `agent.always_hide_columns` "never yields a value in
either mode" and that "an advisor sentence that names such a column in quotes shows none of its
quoted examples". The advisor's `reason` for a format fix does not name the column (only the
proposal's `title` does), and the gate hides examples one string at a time, so the reason - which
quotes real cells - goes through in `masked_data`. Nothing in the advisor reads `always_hide_columns`.

The frame is fake (invented names, amounts and dates of birth). The test runs the real `chat_turn`
with a scripted client that captures every prompt.
"""

from __future__ import annotations

import pandas as pd
import pytest

from tests.unit.agent.egress_canary import ToolingClient, canary_context, run_session

ROWS = 300


def _column(values: list[str]) -> list[str]:
    return [values[i % len(values)] for i in range(ROWS)]


CASES: dict[str, tuple[list[str], tuple[str, ...]]] = {
    # column values, the spellings the advisor quotes in its reason
    "category_variants_of_names": (
        ["Jane Roe", "JANE ROE", "Jane Roe", "Kim Doe", "kim doe", "Kim Doe"],
        ("JANE ROE", "kim doe"),
    ),
    "amounts_stored_as_text": (["48,250", "51,300", "62,975", "70,110"], ("48,250", "51,300", "62,975")),
    "dates_of_birth_written_two_ways": (
        ["03/04/1985", "12/05/1990", "25/12/1979", "01/01/1980"],
        ("03/04/1985", "12/05/1990", "25/12/1979"),
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_always_hide_column_values_stay_out_of_the_advisor_sentences(case: str) -> None:
    values, quoted_by_the_advisor = CASES[case]
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i:05d}" for i in range(ROWS)],
            "secret_col": _column(values),
            "converted_30d": [1 if (i * 7) % 10 < 3 else 0 for i in range(ROWS)],
        }
    )
    ctx = canary_context(mode="masked_data", always_hide=("secret_col",), frame=frame)
    client = ToolingClient([{"action": "get_profile", "args": {}}])
    session, _ = run_session(ctx, client)

    # The advisor did make a suggestion about the hidden column ...
    assert any(
        "secret_col" in proposal.title or "secret_col" in proposal.reason for proposal in session.proposals
    )
    prompts = "\n".join(client.sent_text)
    # ... and none of the cells it quoted may be in a prompt.
    leaked = [value for value in quoted_by_the_advisor if value in prompts]
    assert leaked == [], f"always_hide column secret_col: values sent to the AI service: {leaked}"
