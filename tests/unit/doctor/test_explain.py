"""`explain`: data evidence in words, through the claims verifier (Plan O M236, DEC-1806; the design §5.5 #39).

Acceptance (the plan's §M236): `explain` refuses a claim not backed by evidence and falls back to the rules'
narrative. Also: every number in a sentence comes from the evidence, never from the model; a wrong rank is
refused; the model's own words carry no number; with no AI service the rules' narrative is the explanation; and
what the AI service is sent holds no number, no cell and no column name.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agentic.claims import CLAIM_DIRECTION, CLAIM_FREE_TEXT, CLAIM_UNREFERENCED
from engine.doctor.explain import (
    ExplainRequest,
    data_evidence_index,
    data_rules_narrative,
    meter_narrator,
    verify_data_narrative,
)
from engine.doctor.registry import (
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolResult,
    Exposure,
    data_registry,
)
from engine.generative.errors import BUDGET_EXCEEDED, generative_error
from engine.llm import FakeLLMClient, FakeLLMMode
from engine.llm_http import LLMHttpError
from tests.unit.agent.egress_canary import EMAIL, HARD_SECRETS, NAME, canary_context, leaks, meter_for
from tests.unit.agent.helpers import context_for

ANALYST = Principal(user_id="u-asha", username="asha", roles=frozenset({Role.ANALYST}), kind="user")
NOW = datetime(2026, 10, 12, tzinfo=UTC)


def _frame() -> pd.DataFrame:
    """Four regions with clearly different rates of 'yes': North 50%, South 25%, East 10%, West 0%."""
    regions = ["North"] * 100 + ["South"] * 100 + ["East"] * 100 + ["West"] * 100
    yes = [1 if i < 50 else 0 for i in range(100)]
    yes += [1 if i < 25 else 0 for i in range(100)]
    yes += [1 if i < 10 else 0 for i in range(100)]
    yes += [0] * 100
    return pd.DataFrame(
        {"customer_id": [f"C{i}" for i in range(400)], "region": regions, "converted_30d": yes}
    )


def _changed(changed: int = 12, out_of: int = 400) -> DataToolResult:
    """A measured step (an AI's merge of spellings, tried on the file), as a tool records it."""
    output = {"step": {"kind": "map_values", "column": "region"}, "changed": changed, "out_of": out_of}
    return DataToolResult(
        evidence_id="ev_" + "b" * 16,
        tool="synonym_clusters",
        kind=DataToolKind.READ,
        exposure=Exposure.SESSION_ONLY,
        input={},
        output=output,
        computed_at=NOW,
    )


@pytest.fixture
def case() -> DataToolContext:
    agent = context_for(_frame())
    ctx = DataToolContext(agent=agent, principal=ANALYST, caller="person", clock=lambda: NOW)
    rates = data_registry().call("rate_by", {"column": "region", "outcome_column": "converted_30d"}, ctx)
    outcome = data_registry().call("describe_outcome", {"column": "converted_30d"}, ctx)
    return dataclasses.replace(ctx, evidence=(rates, outcome, _changed()))


def _ids(case: DataToolContext) -> list[str]:
    return [result.evidence_id for result in case.evidence]


def _explain(case: DataToolContext, narrator: Any = None) -> dict[str, Any]:
    ctx = dataclasses.replace(case, narrator=narrator)
    output: dict[str, Any] = data_registry().call("explain", {"evidence_ids": _ids(case)}, ctx).output
    return output


def _index(case: DataToolContext) -> Any:
    return data_evidence_index(((r.evidence_id, r.output) for r in case.evidence), columns=("region",))


def _ref(case: DataToolContext, fact_type: str, about: str | None = None) -> str:
    for fact in _index(case).facts.values():
        if fact.type == fact_type and (about is None or fact.about == about):
            return str(fact.ref)
    raise AssertionError(f"no {fact_type} fact")


def _says(*sentences: dict[str, Any]) -> Any:
    reply = json.dumps({"sentences": list(sentences)})
    return lambda _request: reply


# ---------------------------------------------------------------------------
# With no AI service: the rules' narrative
# ---------------------------------------------------------------------------
def test_with_no_ai_service_the_rules_narrative_is_the_explanation(case: DataToolContext) -> None:
    out = _explain(case)
    narrative = out["narrative"]
    assert narrative["source"] == "rules" and not narrative["replaced"] and not narrative["failures"]
    texts = [s["text"] for s in narrative["sentences"]]
    assert "By region, North has the highest rate: 50.0%." in texts
    assert "This would change 12 of 400 values (3.0%)." in texts
    assert "400 rows were read." in texts
    assert out["evidence_ids"] == _ids(case) and out["facts_total"] > 3


def test_every_rules_sentence_passes_the_same_verifier(case: DataToolContext) -> None:
    index = _index(case)
    narrative = verify_data_narrative(None, index)
    assert len(narrative.sentences) == len(data_rules_narrative(index))
    for sentence in narrative.sentences:
        assert sentence.refs and all(ref.split(":", 1)[0] in _ids(case) for ref in sentence.refs)


# ---------------------------------------------------------------------------
# The AI's narrative, checked
# ---------------------------------------------------------------------------
def test_the_ai_writes_templates_and_every_number_comes_from_the_evidence(case: DataToolContext) -> None:
    out = _explain(
        case,
        _says(
            {
                "template": "Customers in {group} said yes at {rank} rate, {rate}.",
                "claim": {"type": "rate_by_group", "ref": _ref(case, "rate_by_group"), "rank": "highest"},
            },
            {
                "template": "The merge would rewrite {changed} of {out_of} values.",
                "claim": {"type": "changed", "ref": _ref(case, "changed")},
            },
            {"template": "These figures were counted by Data Doctor's rules.", "claim": None},
        ),
    )
    narrative = out["narrative"]
    assert narrative["source"] == "model" and narrative["failures"] == []
    assert [s["text"] for s in narrative["sentences"]] == [
        "Customers in North said yes at the highest rate, 50.0%.",
        "The merge would rewrite 12 of 400 values.",
        "These figures were counted by Data Doctor's rules.",
    ]


def test_a_claim_not_backed_by_evidence_is_refused_and_dropped(case: DataToolContext) -> None:
    out = _explain(
        case,
        _says(
            {"template": "The file has {count} rows.", "claim": {"type": "count", "ref": "f999"}},
            {
                "template": "The merge would rewrite {changed} of {out_of} values.",
                "claim": {"type": "changed", "ref": _ref(case, "changed")},
            },
        ),
    )
    narrative = out["narrative"]
    assert narrative["source"] == "model"
    assert [(f["sentence"], f["code"]) for f in narrative["failures"]] == [(0, CLAIM_UNREFERENCED)]
    assert [s["text"] for s in narrative["sentences"]] == ["The merge would rewrite 12 of 400 values."]


def test_a_claim_of_the_wrong_type_is_not_backed_either(case: DataToolContext) -> None:
    index = _index(case)
    narrative = verify_data_narrative(
        json.dumps(
            {
                "sentences": [
                    {"template": "{count} merged.", "claim": {"type": "count", "ref": _ref(case, "changed")}},
                    {"template": "Plain words.", "claim": None},
                ]
            }
        ),
        index,
    )
    assert [f.code for f in narrative.failures] == [CLAIM_UNREFERENCED]


def test_more_than_one_failure_falls_back_to_the_rules_narrative(case: DataToolContext) -> None:
    out = _explain(
        case,
        _says(
            {"template": "The file has {count} rows.", "claim": {"type": "count", "ref": "f999"}},
            {
                "template": "{group} has {rank} rate: {rate}.",
                "claim": {"type": "rate_by_group", "ref": _ref(case, "rate_by_group"), "rank": "lowest"},
            },
            {"template": "Roughly 40 customers churned.", "claim": None},
        ),
    )
    narrative = out["narrative"]
    assert narrative["source"] == "rules" and narrative["replaced"] is True
    codes = [f["code"] for f in narrative["failures"]]
    assert codes == [CLAIM_UNREFERENCED, CLAIM_FREE_TEXT], "the middle sentence (West is lowest) is true"
    assert narrative["sentences"] == _explain(case)["narrative"]["sentences"]


def test_a_rank_the_evidence_does_not_show_is_refused() -> None:
    frame = _frame()
    frame.loc[frame["region"] == "East", "converted_30d"] = [
        1 if i < 50 else 0 for i in range(100)
    ]  # ties North
    agent = context_for(frame)
    ctx = DataToolContext(agent=agent, principal=ANALYST, caller="person")
    rates = data_registry().call("rate_by", {"column": "region", "outcome_column": "converted_30d"}, ctx)
    index = data_evidence_index([(rates.evidence_id, rates.output)])
    ref = next(f.ref for f in index.facts.values() if f.type == "rate_by_group")
    narrative = verify_data_narrative(
        json.dumps(
            {
                "sentences": [
                    {
                        "template": "{group} has {rank} rate, {rate}.",
                        "claim": {"type": "rate_by_group", "ref": ref, "rank": "highest"},
                    },
                    {
                        "template": "{group} reads {rate}.",
                        "claim": {"type": "rate_by_group", "ref": ref, "group": 0},
                    },
                ]
            }
        ),
        index,
    )
    assert [f.code for f in narrative.failures] == [CLAIM_DIRECTION]
    assert [s.text for s in narrative.sentences] == ["North reads 50.0%."]


def test_the_models_own_words_carry_no_number_and_no_direction(case: DataToolContext) -> None:
    index = _index(case)
    for template in ("About 12 values change.", "Spend is higher in the North."):
        narrative = verify_data_narrative(
            json.dumps(
                {"sentences": [{"template": template, "claim": None}, {"template": "Fine.", "claim": None}]}
            ),
            index,
        )
        assert [f.code for f in narrative.failures] == [CLAIM_FREE_TEXT], template


def test_an_answer_that_is_not_json_is_the_rules_narrative(case: DataToolContext) -> None:
    out = _explain(case, lambda _request: "North wins, obviously.")
    assert out["narrative"]["source"] == "rules" and out["narrative"]["replaced"] is True


def test_an_ai_service_that_does_not_answer_leaves_the_rules_narrative(case: DataToolContext) -> None:
    out = _explain(case, lambda _request: None)
    assert out["narrative"]["source"] == "rules" and out["narrative"]["replaced"] is False


# ---------------------------------------------------------------------------
# The registry's side
# ---------------------------------------------------------------------------
def test_explain_reads_only_this_cases_results(case: DataToolContext) -> None:
    with pytest.raises(DataToolError) as caught:
        data_registry().call("explain", {"evidence_ids": ["ev_" + "c" * 16]}, case)
    assert caught.value.code == "TOOL_INPUT_INVALID"
    assert data_registry().find("explain", dataclasses.replace(case, evidence=())) is None


def test_the_ai_service_is_sent_refs_and_kinds_never_a_number_a_cell_or_a_column() -> None:
    """The injection canary for `explain`'s prompt: the canary file's planted values never reach it."""
    agent = canary_context(rows=600)
    ctx = DataToolContext(agent=agent, principal=ANALYST, caller="person")
    columns = [str(c) for c in agent.frame.columns]
    results = [
        data_registry().call("describe_missing", {}, ctx),
        data_registry().call("value_counts", {"column": columns[1]}, ctx),
    ]
    client = FakeLLMClient(mode=FakeLLMMode.GROUNDED)
    ctx = dataclasses.replace(ctx, evidence=tuple(results), meter=meter_for(client))
    data_registry().call("explain", {"evidence_ids": [r.evidence_id for r in results]}, ctx)
    prompts = [call.prompt for call in client.calls if call.kind == "complete"]
    assert prompts, "explain asked the AI service"
    sent = prompts[-1]
    facts = sent[sent.index("[") : sent.rindex("]") + 1]
    assert leaks(facts, HARD_SECRETS) == [] and NAME not in facts and EMAIL not in facts
    lines = json.loads(facts)
    assert lines and all(set(line) == {"ref", "type", "about"} for line in lines)
    for line in lines:
        assert re.fullmatch(r"f[0-9]+", line["ref"]), "a ref is the only place a digit appears"
        assert not any(character.isdigit() for character in line["type"] + line["about"])
        assert line["about"] not in columns


def test_meter_narrator_renders_the_data_explain_prompt() -> None:
    client = FakeLLMClient(mode=FakeLLMMode.GROUNDED)
    narrate = meter_narrator(meter_for(client))
    narrate(ExplainRequest(facts=({"ref": "f1", "type": "count", "about": "rows"},)))
    (call,) = [c for c in client.calls if c.kind == "complete"]
    assert '"ref": "f1"' in call.prompt and "rate_by_group" in call.system


class _FailingMeter:
    """A meter whose every call raises `error`."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def complete(self, rendered: Any, purpose: Any) -> Any:
        raise self.error


@pytest.mark.parametrize(
    ("error", "logged"),
    [
        (LLMHttpError("key_expired", status=401), "LLM_UNAVAILABLE, LLM_KEY_EXPIRED"),
        (LLMHttpError("out_of_room"), "LLM_REFUSED, LLM_OUT_OF_ROOM"),
        (generative_error(BUDGET_EXCEEDED, limit="calls", calls=30), "BUDGET_EXCEEDED"),
        (RuntimeError("the prompt said zq-private"), "RuntimeError"),
    ],
    ids=["expired-key", "out-of-room", "spent", "bug"],
)
def test_a_narrator_the_ai_service_failed_is_no_answer_and_the_log_says_why_without_the_words(
    caplog: pytest.LogCaptureFixture, error: Exception, logged: str
) -> None:
    """The rules' narrative is shown either way; the log is how an operator learns why it always is."""
    narrate = meter_narrator(_FailingMeter(error))
    with caplog.at_level(logging.WARNING, logger="engine.doctor.explain"):
        assert narrate(ExplainRequest(facts=({"ref": "f1", "type": "count", "about": "rows"},))) is None
    (line,) = [r.getMessage() for r in caplog.records if r.name == "engine.doctor.explain"]
    assert line.startswith("data_explain: ") and logged in line
    assert "zq-private" not in caplog.text
