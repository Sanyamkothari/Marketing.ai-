"""Review finding 12: a match count is an oracle on a column the model must not see.

`docs/AGENTS.md` §7.5 / §7.7: a personal-data column shows "no value at all ... never a minimum, a maximum or
a match", and a column in `always_hide_columns` "never yields a value ... only counts remain". But
`find_values` answers `total_matching_rows` for any `contains` text the model chooses - also on a personal
column - and `sample_rows` (`where_column` + `equals`) answers `total_matching` and the rows' other cells for a
column that is only *hidden by settings* (the tool checks the profile's personal-data flag, never
`always_hide_columns`). A model (or a file that talks the model into it: §7.4) can therefore read a hidden
or personal column one character at a time from the counts, in `masked_data` and in `summaries_only`,
including the e-mail column of the canary.

The tests ask for the weakest property: the gated result must not depend on what the model searched for.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.agent import egress
from engine.agent.tools import AgentContext, call_tool
from tests.unit.agent.egress_canary import canary_context


def _gate(ctx: AgentContext) -> egress.Egress:
    return egress.Egress.build(
        [str(c) for c in ctx.frame.columns],
        personal_columns=[c.name for c in ctx.profile.columns if c.pii_kinds],
        always_hide=ctx.config.agent.always_hide_columns,
        mode=ctx.config.agent.ai_data_access,
    )


def _seen(ctx: AgentContext, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return _gate(ctx).prepare(call_tool(ctx, tool, args, evidence_id="e1").result)


@pytest.mark.parametrize("mode", ["masked_data", "summaries_only"])
@pytest.mark.parametrize(
    "column", ["contact_email", "notes"]
)  # a personal-data column; a column hidden by settings
def test_find_values_result_does_not_depend_on_the_search_text(mode: str, column: str) -> None:
    ctx = canary_context(mode=mode, always_hide=["notes"], rows=300)
    hit = _seen(ctx, "find_values", {"column": column, "contains": "jane"})
    miss = _seen(ctx, "find_values", {"column": column, "contains": "zzzzqq"})
    assert hit == miss, f"the model can tell whether {column} contains 'jane': {hit!r} vs {miss!r}"


@pytest.mark.parametrize("mode", ["masked_data", "summaries_only"])
def test_sample_rows_cannot_pick_rows_by_the_value_of_a_hidden_column(mode: str) -> None:
    ctx = canary_context(mode=mode, always_hide=["notes"], rows=300)
    common = {"columns": ["converted_30d"], "where_column": "notes"}
    hit = _seen(ctx, "sample_rows", {**common, "equals": "Great service, thanks"})
    miss = _seen(ctx, "sample_rows", {**common, "equals": "no such note"})
    assert hit == miss, f"the model can test guesses against a column hidden by settings: {hit!r} vs {miss!r}"
