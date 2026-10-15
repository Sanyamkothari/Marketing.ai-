"""The Doctor's AI limits: a spending limit per case, the client's consent, and every file masked."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.agent.tools import AgentContext
from engine.config import BudgetConfig, LlmConfig, RunMode, load_use_case
from engine.doctor import visit
from engine.doctor.ai_guard import (
    AI_LIMIT_REPLY,
    AI_NO_CONSENT_REPLY,
    AiConsentStore,
    AiGuard,
)
from engine.doctor.case import Case, CaseCounters
from engine.doctor.config import AiLimitsConfig
from engine.doctor.diagnose import LoadedFile
from engine.storage import LocalStorage
from tests.unit.agent.egress_canary import ToolingClient, guardrails
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.visit_support import ANALYST, CLIENT, GOAL_A, open_case

EMAIL = "ravi.kumar@example.com"


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _guard(client: ToolingClient, *, calls: int = 60, tokens: int = 600_000, consent: bool = True) -> AiGuard:
    return AiGuard(
        client,
        job_id="test",
        llm=LlmConfig(),
        budget=BudgetConfig(cache=False, max_calls_per_run=100_000, max_cost_usd_per_run=1_000.0),
        limits=AiLimitsConfig(max_calls_per_case=calls, max_tokens_per_case=tokens),
        consented=lambda _client: consent,
    )


def _converse(guard: AiGuard) -> visit.ConversePort:
    def context_for(case: Case, files: tuple[LoadedFile, ...]) -> AgentContext:
        return AgentContext(
            use_case_id=GOAL_A,
            config=load_use_case(GOAL_A),
            config_root=None,
            upload_id=files[0].upload_id,
            mode=RunMode.TRAIN,
            profile=files[0].profile,
            frame=files[0].frame,
        )

    return visit.plan_g_converse(context_for, meter=guard, guardrails=guardrails(), guard=guard)


def _files() -> tuple[LoadedFile, ...]:
    return tuple(vs.world().files.values())


def test_no_consent_means_no_call(storage: LocalStorage) -> None:
    client = ToolingClient([])
    case = open_case(storage, vs.world())
    result = _converse(_guard(client, consent=False))(case, "Hello", _files())
    assert not client.calls, "the AI was not asked"
    assert result.reply == AI_NO_CONSENT_REPLY and result.ai is False


def test_consent_is_kept_once_with_who_and_when(storage: LocalStorage) -> None:
    store = AiConsentStore(storage)
    assert store.get(CLIENT) is None
    first = store.give(CLIENT, actor=ANALYST, at=vs.T0)
    again = store.give(CLIENT, actor="someone-else", at=vs.T0.replace(year=2030))
    assert again == first and first.given_by == ANALYST and first.given_at == vs.T0
    assert AiConsentStore(storage).get(CLIENT) == first


@pytest.mark.parametrize("used", [{"model_calls": 3}, {"tokens": 1_000}])
def test_a_spent_case_makes_no_further_call_and_says_so(storage: LocalStorage, used: dict[str, int]) -> None:
    client = ToolingClient([])
    case = open_case(storage, vs.world()).model_copy(update={"counters": CaseCounters(**used)})
    result = _converse(_guard(client, calls=3, tokens=1_000))(case, "Hello", _files())
    assert not client.calls
    assert result.reply == AI_LIMIT_REPLY and result.ai is False


def test_the_limit_stops_a_turn_between_calls_and_the_visit_goes_on(storage: LocalStorage) -> None:
    client = ToolingClient([{"action": "describe_missing", "args": {}}])
    case = open_case(storage, vs.world())
    result = _converse(_guard(client, calls=1))(case, "What is missing?", _files())
    assert len([c for c in client.calls if c.kind == "complete"]) == 1, "the second call was refused"
    assert result.reply == AI_LIMIT_REPLY
    assert result.model_calls == 1 and result.tokens > 0, "what the turn used is counted on the case"


def test_every_file_goes_through_its_own_gate(storage: LocalStorage) -> None:
    config = load_use_case(GOAL_A)
    frame = vs.customers(rows=300, seed=3)
    frame.insert(0, f"contact {EMAIL}", [f"c{i}@example.org" for i in range(len(frame))])
    first = vs.loaded("up-1", "customers.csv", vs.customers(rows=300, seed=4), config)
    second = vs.loaded("up-2", f"{EMAIL}.csv", frame, config)
    client = ToolingClient([])
    case = open_case(storage, vs.world())
    result = _converse(_guard(client))(case, "What files do I have?", (first, second))
    sent = "\n".join(client.sent_text) + "\n".join(str(item) for item in result.sent)
    assert "other_files" in sent, "the second file was shown to the AI"
    assert EMAIL not in sent and "example.org" not in sent, "...masked"
    assert any("get_profile" in str(item) for item in result.sent), "and the screen can say what was sent"
