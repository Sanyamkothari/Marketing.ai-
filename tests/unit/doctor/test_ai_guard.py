"""The Doctor's AI limits: a spending limit per case, the client's consent, and every file masked."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from engine.access.roles import Principal, Role
from engine.agent.loop import UNAVAILABLE
from engine.agent.tools import AgentContext
from engine.config import BudgetConfig, LlmConfig, RunMode, load_use_case
from engine.doctor import visit
from engine.doctor.agent_chat import doctor_converse
from engine.doctor.ai_guard import (
    AI_LIMIT_REPLY,
    AI_NO_CONSENT_REPLY,
    MAX_OVERVIEW_CHARS,
    AiConsentStore,
    AiGuard,
    file_overviews,
)
from engine.doctor.case import Case, CaseCounters
from engine.doctor.config import AiLimitsConfig
from engine.doctor.diagnose import LoadedFile
from engine.generative.contracts import GenerativePurpose
from engine.generative.prompts import load_prompt, render
from engine.llm import LLMCompletion
from engine.llm_http import PROBLEMS, LLMHttpError
from engine.storage import LocalStorage
from tests.fixtures.doctor_eval.chat_model import ChatScript
from tests.unit.agent.egress_canary import guardrails
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.chat_support import Down
from tests.unit.doctor.visit_support import ANALYST, CLIENT, GOAL_A, open_case

EMAIL = "ravi.kumar@example.com"
ASHA = Principal(user_id=ANALYST, username="asha", roles=frozenset({Role.ANALYST}), kind="user")


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _guard(
    client: ChatScript,
    *,
    calls: int = 60,
    tokens: int = 600_000,
    consent: bool = True,
    llm: LlmConfig | None = None,
) -> AiGuard:
    return AiGuard(
        client,
        job_id="test",
        llm=llm if llm is not None else LlmConfig(),
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

    return doctor_converse(context_for, meter=guard, guardrails=guardrails(), principal=ASHA)


def _files() -> tuple[LoadedFile, ...]:
    return tuple(vs.world().files.values())


def test_no_consent_means_no_call(storage: LocalStorage) -> None:
    client = ChatScript({})
    case = open_case(storage, vs.world())
    result = _converse(_guard(client, consent=False))(case, "Hello", _files())
    assert not client.calls, "the AI was not asked"
    assert result.reply == AI_NO_CONSENT_REPLY and result.ai is False
    assert (result.blocked_by, result.error_codes) == ("no_consent", ())


def test_consent_is_kept_once_with_who_and_when(storage: LocalStorage) -> None:
    store = AiConsentStore(storage)
    assert store.get(CLIENT) is None
    first = store.give(CLIENT, actor=ANALYST, at=vs.T0)
    again = store.give(CLIENT, actor="someone-else", at=vs.T0.replace(year=2030))
    assert again == first and first.given_by == ANALYST and first.given_at == vs.T0
    assert AiConsentStore(storage).get(CLIENT) == first


@pytest.mark.parametrize("used", [{"model_calls": 3}, {"tokens": 1_000}])
def test_a_spent_case_makes_no_further_call_and_says_so(storage: LocalStorage, used: dict[str, int]) -> None:
    client = ChatScript({})
    case = open_case(storage, vs.world()).model_copy(update={"counters": CaseCounters.model_validate(used)})
    result = _converse(_guard(client, calls=3, tokens=1_000))(case, "Hello", _files())
    assert not client.calls
    assert result.reply == AI_LIMIT_REPLY and result.ai is False
    assert (result.blocked_by, result.error_codes) == ("budget", ())


def test_the_limit_stops_a_turn_between_calls_and_the_visit_goes_on(storage: LocalStorage) -> None:
    client = ChatScript({"missing": [{"action": "describe_missing", "file": "F1", "args": {}}]})
    case = open_case(storage, vs.world())
    result = _converse(_guard(client, calls=1))(case, "What is missing?", _files())
    assert len([c for c in client.calls if c.kind == "complete"]) == 1, "the second call was refused"
    assert result.reply == AI_LIMIT_REPLY
    assert result.model_calls == 1 and result.tokens > 0, "what the turn used is counted on the case"
    assert (result.blocked_by, result.error_codes) == ("budget", ("BUDGET_EXCEEDED",))


def test_an_ai_service_that_fails_says_why_in_the_result(storage: LocalStorage) -> None:
    sentence, fix = PROBLEMS["out_of_room"]
    case = open_case(storage, vs.world())
    result = _converse(_guard(Down(LLMHttpError("out_of_room"))))(case, "Hello", _files())
    assert result.ai is True
    counted = (result.model_calls, result.tokens)
    assert counted == (
        1,
        0,
    ), "billed whatever the provider reported: one call, and no tokens it did not report"
    assert result.reply == f"{UNAVAILABLE} {sentence} {fix}"
    assert (result.blocked_by, result.error_codes) == ("unavailable", ("LLM_REFUSED", "LLM_OUT_OF_ROOM"))
    answered = _converse(_guard(ChatScript({})))(case, "Hello", _files())
    assert (answered.blocked_by, answered.error_codes) == (None, ())


def test_every_file_goes_through_its_own_gate(storage: LocalStorage) -> None:
    config = load_use_case(GOAL_A)
    frame = vs.customers(rows=300, seed=3)
    frame.insert(0, f"contact {EMAIL}", [f"c{i}@example.org" for i in range(len(frame))])
    first = vs.loaded("up-1", "customers.csv", vs.customers(rows=300, seed=4), config)
    second = vs.loaded("up-2", f"{EMAIL}.csv", frame, config)
    client = ChatScript({})
    case = open_case(storage, vs.world())
    result = _converse(_guard(client))(case, "What files do I have?", (first, second))
    sent = "\n".join(client.sent_text) + "\n".join(str(item) for item in result.sent)
    assert '"file": 2' in sent, "the second file was shown to the AI"
    assert EMAIL not in sent and "example.org" not in sent, "...masked"
    assert any("get_profile" in str(item) for item in result.sent), "and the screen can say what was sent"


def test_many_wide_files_leave_room_for_the_chat() -> None:
    config = load_use_case(GOAL_A)
    wide = vs.customers(rows=50, seed=5)
    for i in range(60):
        wide[f"extra_measure_number_{i}"] = range(len(wide))
    file = vs.loaded("up-w", "wide.csv", wide, config)
    contexts = [
        AgentContext(
            use_case_id=GOAL_A,
            config=config,
            config_root=None,
            upload_id=file.upload_id,
            mode=RunMode.TRAIN,
            profile=file.profile,
            frame=file.frame,
        )
    ] * 40
    found = file_overviews(contexts)
    assert 0 < len(found) < 40, "files past the limit are left out"
    assert sum(len(json.dumps(overview)) for overview, _sent in found) <= MAX_OVERVIEW_CHARS + 40 * 20


def test_a_case_may_spend_enough_for_a_conversation_and_a_message_takes_a_few_steps() -> None:
    """The chat spends more than the one-shot review did: 2M tokens and 200 calls a case, 8 actions a message."""
    limits = AiLimitsConfig()
    assert (limits.max_tokens_per_case, limits.max_calls_per_case, limits.max_chat_steps) == (
        2_000_000,
        200,
        8,
    )
    assert _guard(ChatScript({})).chat_steps == 8


ALLOWANCES = LlmConfig(max_output_tokens=300, data_agent_max_output_tokens=3000)


def test_a_doctor_turn_asks_the_model_with_the_data_agents_allowance(storage: LocalStorage) -> None:
    """The chat reasons before it replies; an allowance sized for a paragraph left it nothing to write with."""
    client = ChatScript({"missing": [{"action": "describe_missing", "file": "F1", "args": {}}]})
    case = open_case(storage, vs.world())
    result = _converse(_guard(client, llm=ALLOWANCES))(case, "What is missing?", _files())
    sent = {call.max_tokens for call in client.calls if call.kind == "complete"}
    assert client.asked >= 2 and result.ai, "the turn made more than one call"
    assert sent == {3000}, "every one of them was given the data agent's allowance"


@pytest.mark.parametrize(
    ("purpose", "allowance"),
    [
        (GenerativePurpose.DOCTOR, 3000),
        (GenerativePurpose.DATA_AGENT, 3000),
        (GenerativePurpose.DATA_AGENT_ASSIST, 3000),
        (GenerativePurpose.DATA_EXPLAIN, 3000),
        (GenerativePurpose.ASSISTANT_ANSWER, 300),
    ],
)
def test_the_guard_sends_a_calls_own_allowance_like_any_meter(
    purpose: GenerativePurpose, allowance: int
) -> None:
    client = ChatScript({})
    _guard(client, llm=ALLOWANCES).complete(
        render(load_prompt("judge_toxicity"), {"generated": "x"}), purpose
    )
    assert [call.max_tokens for call in client.calls] == [allowance]


class _NoRoom(ChatScript):
    """The chat's model, stopped by the token ceiling in its thinking on every turn, and billed for it."""

    def complete(self, prompt: str, **kwargs: Any) -> LLMCompletion:
        super().complete(prompt, **kwargs)  # recorded in `calls`, like any other
        raise LLMHttpError(
            "out_of_room", model_id=kwargs.get("model_id"), input_tokens=40, output_tokens=3000
        )


def test_a_call_that_ran_out_of_room_and_used_up_the_limit_still_says_it_ran_out_of_room(
    storage: LocalStorage,
) -> None:
    """The billed call takes the case to its limit, but the model never wrote anything: that is the cause."""
    sentence, fix = PROBLEMS["out_of_room"]
    case = open_case(storage, vs.world())
    guard = _guard(_NoRoom({}), tokens=3_000, llm=ALLOWANCES)
    result = _converse(guard)(case, "Hello", _files())
    assert guard.spent(), "the one failed call used up the case's tokens"
    assert result.reply == f"{UNAVAILABLE} {sentence} {fix}"
    assert (result.blocked_by, result.error_codes) == ("unavailable", ("LLM_REFUSED", "LLM_OUT_OF_ROOM"))
    case = case.model_copy(update={"counters": CaseCounters(model_calls=1, tokens=3040)})
    again = _converse(guard)(case, "Hello again", _files())
    assert again.reply == AI_LIMIT_REPLY and (again.blocked_by, again.error_codes) == ("budget", ())


def test_a_turn_that_ran_out_of_room_is_counted_on_the_case_so_trying_again_has_a_limit(
    storage: LocalStorage,
) -> None:
    client = _NoRoom({})
    case = open_case(storage, vs.world())
    guard = _guard(client, tokens=6_000, llm=ALLOWANCES)
    first = _converse(guard)(case, "Hello", _files())
    assert (first.model_calls, first.tokens) == (
        1,
        3040,
    ), "the turn's one call was billed for its whole allowance"
    case = case.model_copy(update={"counters": CaseCounters(model_calls=1, tokens=3040)})
    second = _converse(guard)(case, "Hello again", _files())
    assert (second.model_calls, second.tokens) == (1, 3040)
    case = case.model_copy(update={"counters": CaseCounters(model_calls=2, tokens=6080)})
    third = _converse(guard)(case, "And again", _files())
    assert third.reply == AI_LIMIT_REPLY and not third.ai, "the case's limit now stops the retries"
    assert client.asked == 2
