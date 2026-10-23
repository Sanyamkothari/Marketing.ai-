"""`run_test`: the connection test asks what the work asks, and an empty answer is not a green light.

A model that reasons before it replies can spend a small allowance on thinking and return nothing. If
the test used a tiny allowance and called that "connected", the first Guided setup or Data Doctor turn
would fail instead. So the test sends the allowance real data-agent calls get, and an answer with no
text is `ok: false` - with our own sentence, never the provider's words.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.ai_service import (
    PROVIDERS,
    TEST_MAX_TOKENS,
    TEST_PROMPT,
    TEST_TIMEOUT_S,
    Candidate,
    build_service_client,
    run_test,
)
from engine.config import LlmConfig
from engine.llm import LLMError
from engine.llm_http import PROBLEMS, LLMHttpError
from tests.unit.ai_service.server import Reply, Sent, chat_reply, message_reply, serve

KEY = "zk-test-0123456789abcdef"


def _candidate(provider: str, url: str = "", *, model: str = "m-1") -> Candidate:
    return Candidate(
        provider=PROVIDERS[provider],
        model=model,
        embedding_model="",
        base_url=url,
        region="us-east-1" if provider == "bedrock" else "",
        api_key=None if provider == "bedrock" else KEY,
    )


def _openai(url: str) -> Candidate:
    return _candidate("openai_compatible", url + "/v1")


class _Stub:
    """A client whose `complete` raises `error`; what it was asked is kept in `asked`."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.asked: list[dict[str, Any]] = []

    def complete(self, prompt: str, **kwargs: Any) -> None:
        self.asked.append({"prompt": prompt, **kwargs})
        raise self.error


def _stubbed(monkeypatch: pytest.MonkeyPatch, error: Exception) -> _Stub:
    stub = _Stub(error)
    monkeypatch.setattr("engine.ai_service.build_service_client", lambda *_a, **_k: stub)
    return stub


# --- the allowance -------------------------------------------------------------------------------
def test_the_default_allowance_is_the_data_agent_one_read_from_the_config() -> None:
    default = LlmConfig.model_fields["data_agent_max_output_tokens"].default
    assert TEST_MAX_TOKENS == default == LlmConfig().data_agent_max_output_tokens
    assert LlmConfig().max_output_tokens < TEST_MAX_TOKENS, "the test must not be stingier than real calls"


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_the_request_carries_the_default_allowance_and_still_asks_for_one_word(protocol: str) -> None:
    reply = chat_reply if protocol == "openai" else message_reply
    with serve(lambda _: reply("ready")) as server:
        candidate = _openai(server.url) if protocol == "openai" else _candidate("anthropic", server.url)
        outcome = run_test(candidate, KEY)
    sent = server.requests[0].body
    assert outcome.ok is True and len(server.requests) == 1
    assert sent["max_tokens"] == TEST_MAX_TOKENS == LlmConfig().data_agent_max_output_tokens
    assert sent["messages"][-1] == {"role": "user", "content": TEST_PROMPT}
    assert (
        "single word" in TEST_PROMPT
    ), "max_tokens is a ceiling, not a cost: the prompt keeps the reply short"


def test_a_caller_can_pass_its_own_allowance() -> None:
    with serve(lambda _: chat_reply("ready")) as server:
        run_test(_openai(server.url), KEY, max_tokens=777)
    assert server.requests[0].body["max_tokens"] == 777


def test_a_model_that_wants_max_completion_tokens_gets_the_same_allowance() -> None:
    def handler(sent: Sent) -> Reply:
        if "max_tokens" in (sent.body or {}):
            return Reply(400, {"error": {"message": "use 'max_completion_tokens' instead of 'max_tokens'"}})
        return chat_reply("ready")

    with serve(handler) as server:
        outcome = run_test(_openai(server.url), KEY, max_tokens=1234)
    assert outcome.ok is True
    assert (
        server.requests[-1].body["max_completion_tokens"] == 1234
        and "max_tokens" not in server.requests[-1].body
    )


def test_bedrock_gets_the_allowance_too(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _stubbed(monkeypatch, LLMError("LLM_UNAVAILABLE", "The model could not be reached."))
    run_test(_candidate("bedrock"), None)
    assert stub.asked[0]["max_tokens"] == TEST_MAX_TOKENS and stub.asked[0]["prompt"] == TEST_PROMPT


def test_the_default_wait_is_the_one_real_calls_get_read_from_the_config() -> None:
    assert TEST_TIMEOUT_S == LlmConfig.model_fields["timeout_s"].default == LlmConfig().timeout_s


def test_the_test_waits_as_long_as_a_real_call_so_a_slow_thinker_is_not_a_false_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer allowance went from 16 to thousands of tokens; the whole generation counts against the
    client's read timeout, so a model that thinks for half a minute must get the 60 s real calls get."""
    waited: list[int] = []

    def spy(info: Any, **kwargs: Any) -> Any:
        waited.append(kwargs["timeout_s"])
        return build_service_client(info, **kwargs)

    monkeypatch.setattr("engine.ai_service.build_service_client", spy)
    with serve(lambda _: chat_reply("ready")) as server:
        assert run_test(_openai(server.url), KEY).ok is True
        assert run_test(_openai(server.url), KEY, timeout_s=5).ok is True
    assert waited == [LlmConfig().timeout_s, 5]
    assert waited[0] > 20, "20 s was the budget of a 16-token test"


# --- a real answer -------------------------------------------------------------------------------
def test_a_normal_answer_is_connected() -> None:
    with serve(lambda _: chat_reply("ready")) as server:
        outcome = run_test(_openai(server.url), KEY)
    assert outcome.ok is True and outcome.fix is None
    assert outcome.message == "Connected. Other / local answered with m-1."
    assert outcome.model == "m-1" and isinstance(outcome.latency_ms, int)
    assert outcome.embeddings_note == "Document assistant will match by keywords."


# --- an answer with no text ----------------------------------------------------------------------
@pytest.mark.parametrize("text", ["", "   \n"])
def test_a_plain_empty_answer_is_not_connected(text: str) -> None:
    with serve(lambda _: chat_reply(text)) as server:
        outcome = run_test(_openai(server.url), KEY)
    assert outcome.ok is False
    assert outcome.message == "Other / local answered with no text, so Marketing AI could not use it."
    assert outcome.fix is not None and "another model" in outcome.fix
    assert "Connected" not in outcome.message
    assert outcome.model == "m-1" and isinstance(
        outcome.latency_ms, int
    ), "what was reached is still reported"
    assert outcome.embeddings_note is None


def test_an_empty_answer_from_claude_is_not_connected_either() -> None:
    with serve(lambda _: message_reply("")) as server:
        outcome = run_test(_candidate("anthropic", server.url), KEY)
    assert outcome.ok is False and outcome.message.startswith("Claude (Anthropic) answered with no text")


@pytest.mark.parametrize("provider", ["bedrock", "openai", "anthropic"])
def test_an_empty_answer_has_our_sentence_never_the_providers_words_or_the_clients(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Amazon Bedrock raises the same empty-answer code, and its generic "could not answer" is wrong for it."""
    _stubbed(monkeypatch, LLMError("LLM_REFUSED", "the provider said: customer 4711 lives at Example Street"))
    outcome = run_test(_candidate(provider), None if provider == "bedrock" else KEY)
    label = PROVIDERS[provider].label
    assert outcome.ok is False
    assert outcome.message == f"{label} answered with no text, so Marketing AI could not use it."
    assert "customer 4711" not in outcome.model_dump_json()
    assert outcome.fix is not None and "another model" in outcome.fix


# --- ran out of room -----------------------------------------------------------------------------
@pytest.mark.parametrize("provider", ["openai", "anthropic", "openai_compatible"])
def test_running_out_of_room_gets_its_own_sentence_and_a_fix_that_fits_the_test(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """The kind shares LLM_REFUSED with an empty answer, so the code alone must not pick the sentence.

    The fix is the test's own: the test sends the default allowance whatever a use case file says, so the
    client's "raise generative.llm.data_agent_max_output_tokens" would send the operator round in a loop.
    """
    error = LLMHttpError("out_of_room", model_id="m-1")
    assert error.code == "LLM_REFUSED"
    _stubbed(monkeypatch, error)
    outcome = run_test(_candidate(provider), KEY)
    assert outcome.ok is False and outcome.message == PROBLEMS["out_of_room"][0]
    assert "answered with no text" not in outcome.message
    assert outcome.fix is not None and outcome.fix != PROBLEMS["out_of_room"][1]
    assert "without extended reasoning" in outcome.fix
    assert (
        f"{TEST_MAX_TOKENS} tokens, the default of generative.llm.data_agent_max_output_tokens" in outcome.fix
    )
    assert (
        "not to this test" in outcome.fix
    ), "a larger setting for a use case must not be promised to help here"
    assert outcome.model == "m-1" and isinstance(outcome.latency_ms, int)


def test_the_out_of_room_fix_names_the_allowance_the_test_actually_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _stubbed(monkeypatch, LLMHttpError("out_of_room", model_id="m-1"))
    outcome = run_test(_candidate("openai"), KEY, max_tokens=777)
    assert stub.asked[0]["max_tokens"] == 777
    assert outcome.fix is not None and "777 tokens" in outcome.fix and "4096" not in outcome.fix
    assert "default of" not in outcome.fix, "777 is nobody's configured default, so it must not be called one"


# --- the rest of what the test says is unchanged -------------------------------------------------
def test_a_rejected_key_is_still_ok_false_with_the_fixed_sentence() -> None:
    quoting = {"error": {"message": f"bad key {KEY}; prompt was: {TEST_PROMPT}"}}
    with serve(lambda _: Reply(401, quoting)) as server:
        outcome = run_test(_openai(server.url), KEY)
    assert outcome.ok is False and (outcome.message, outcome.fix) == PROBLEMS["key_rejected"]
    assert KEY not in outcome.model_dump_json()
