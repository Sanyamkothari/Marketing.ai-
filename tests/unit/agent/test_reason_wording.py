"""Plan J M98 (DEC-1308): Guided setup notes the columns whose reasons have no plain wording yet.

The wording is a **check** suggestion (`engine.agent.recommend.suggest_reason_phrases`). No run setting
can hold it, so the advisor records it as an assumption, never as a setting that would be applied: the
benchmark's golden digest (`tests/fixtures/agent_bench/expected.json`) lists proposals only and is
unchanged by it.
"""

from __future__ import annotations

from engine.agent.advisor import advise
from engine.agent.contracts import ProposalKind
from tests.fixtures.agent_bench.cases import CASES
from tests.unit.agent.helpers import context_for


def test_the_clean_file_gets_a_wording_note_for_columns_reasons_yaml_lacks() -> None:
    advice = advise(context_for(CASES["clean"]()))
    notes = [text for text in advice.assumptions if "no plain wording yet" in text]
    assert len(notes) == 1, advice.assumptions
    note = notes[0]
    assert "pushes the score up" in note and "pushes the score down" in note
    # Plain words only: no template placeholder and no repository path reach a business user.
    assert "{" not in note and "}" not in note and "configs/" not in note and ".yaml" not in note
    assert "administrator" in note
    # The key, the outcome and the opt-in flag are not model inputs, so they are not worded.
    assert "customer_id" not in note and "converted_30d" not in note
    # It is a note, not a setting: nothing the run would apply.
    assert all("reason" not in str(p.path or "") for p in advice.proposals if p.kind is ProposalKind.SETTING)


def test_a_column_reasons_yaml_covers_is_not_listed() -> None:
    advice = advise(context_for(CASES["clean"]()))
    note = next(text for text in advice.assumptions if "no plain wording yet" in text)
    assert "'monthly_spend'" not in note.split(". To check")[0]
