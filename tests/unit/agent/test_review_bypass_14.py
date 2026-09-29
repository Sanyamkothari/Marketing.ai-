"""Review finding 14: a numeric header makes `apply_aliases` rewrite the numbers in the instructions.

A header that does not start with a letter (`2020`, `0.05`, `500.0`, a pivoted file's year or bin columns) is
not a plain label, so it gets an alias `column_<n>`, and `Egress.apply_aliases` then replaces that string
**anywhere inside any text** (a substring match for names of four or more characters, no word or number
boundary). The gate applies it to the settings block and to the advisor's sentences, so the model is told
`range column_12 to 0.4` or `range 5.0 to column_11` instead of the bounds it may suggest, and a date in a
sentence becomes `column_5-01-31`. Through the real `chat_turn`: the prompt the model receives is changed by
the *name of a column*, which is the "false positive corrupts instructions" class of the earlier
`"maximum": 100000000` bug.
"""

from __future__ import annotations

import pandas as pd

from engine.agent import egress
from engine.agent.session import start_session
from tests.unit.agent.egress_canary import ToolingClient, canary_context, run_session
from tests.unit.agent.helpers import synthetic


def test_a_numeric_header_does_not_rewrite_the_ranges_in_the_settings_block() -> None:
    frame: pd.DataFrame = synthetic("clean", rows=400)
    frame["500.0"] = 1
    frame["0.05"] = 2
    frame["240.0"] = 3
    ctx = canary_context(mode="masked_data", frame=frame)
    client = ToolingClient([{"action": "get_profile", "args": {}}], per_turn=1)
    run_session(ctx, client, started=start_session(ctx, session_id="review-14"))
    prompts = "\n".join(client.sent_text)
    assert "`model_search.tuning_trials`: 50; range 5.0 to 500.0" in prompts
    assert "`model_search.time_limit_minutes`: 30; range 1.0 to 240.0" in prompts
    assert "`split.validation_fraction`: 0.15; range 0.05 to 0.4" in prompts


def test_a_year_header_does_not_rewrite_a_date_in_a_sentence() -> None:
    gate = egress.Egress.build(["customer_id", "2020", "2021"])
    sentence = "The file runs from 2020-01-31 to 2021-03-04 and has 12020 rows."
    assert gate.text(sentence) == sentence
