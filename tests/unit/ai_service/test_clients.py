"""`OpenAICompatibleClient` and `AnthropicClient` against a real local HTTP server (DEC-1141).

Every assertion about what is sent is made on the server's own record of the request; every assertion
about errors checks that nothing the server said - which here deliberately quotes the prompt and the
key - reaches the exception.
"""

from __future__ import annotations

import httpx
import pytest

from engine.llm import LLMClient, LLMError
from engine.llm_http import MAX_MODELS, PROBLEMS, AnthropicClient, LLMHttpError, OpenAICompatibleClient
from tests.unit.ai_service.server import Reply, Sent, chat_reply, message_reply, serve

KEY = "zk-test-0123456789abcdef"
PROMPT = "customer 4711 lives at 12 Example Street"


def _openai(url: str, **kw: object) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url=url + "/v1", api_key=KEY, model_id="m-1", embedding_model_id="e-1", backoff_s=0.0, **kw  # type: ignore[arg-type]
    )


def _anthropic(url: str, **kw: object) -> AnthropicClient:
    return AnthropicClient(base_url=url, api_key=KEY, model_id="c-1", backoff_s=0.0, **kw)  # type: ignore[arg-type]


def test_both_clients_are_llm_clients() -> None:
    assert isinstance(_openai("http://x"), LLMClient) and isinstance(_anthropic("http://x"), LLMClient)


# --- OpenAI protocol --------------------------------------------------------------------------
def test_openai_complete_sends_the_key_in_the_header_and_nowhere_else() -> None:
    with serve(lambda _: chat_reply("hello", prompt_tokens=11, completion_tokens=3)) as server:
        result = _openai(server.url).complete(PROMPT, system="be brief", max_tokens=99, temperature=0.3)
    sent = server.requests[0]
    assert (sent.method, sent.path) == ("POST", "/v1/chat/completions")
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert sent.body["model"] == "m-1" and sent.body["max_tokens"] == 99 and sent.body["temperature"] == 0.3
    assert sent.body["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": PROMPT},
    ]
    assert KEY not in sent.raw.decode() and KEY not in sent.path
    assert (result.text, result.model_id, result.input_tokens, result.output_tokens, result.stop_reason) == (
        "hello",
        "m-1",
        11,
        3,
        "stop",
    )


def test_openai_without_a_key_sends_no_authorization_header() -> None:
    with serve(lambda _: chat_reply()) as server:
        OpenAICompatibleClient(base_url=server.url, model_id="m").complete("hi")
    assert "authorization" not in server.requests[0].headers
    assert server.requests[0].path == "/chat/completions"


def test_a_model_id_named_by_the_call_wins_and_an_empty_system_prompt_is_left_out() -> None:
    with serve(lambda _: chat_reply()) as server:
        _openai(server.url).complete("hi", model_id="other")
    assert server.requests[0].body["model"] == "other"
    assert [m["role"] for m in server.requests[0].body["messages"]] == ["user"]


def test_openai_itself_gets_max_completion_tokens() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}], "usage": {}}
        )

    client = OpenAICompatibleClient(
        base_url="https://api.openai.com/v1",
        api_key=KEY,
        model_id="m",
        transport=httpx.MockTransport(handler),
    )
    client.complete("hi", max_tokens=5)
    assert seen[0]["max_completion_tokens"] == 5 and "max_tokens" not in seen[0]


def test_a_400_naming_a_parameter_is_retried_once_without_it_and_remembered() -> None:
    def handler(sent: Sent) -> Reply:
        if "temperature" in (sent.body or {}):
            return Reply(400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.3"}})
        return chat_reply("ok")

    with serve(handler) as server:
        client = _openai(server.url)
        assert client.complete("hi", temperature=0.3).text == "ok"
        assert client.complete("again", temperature=0.3).text == "ok"
    bodies = [r.body for r in server.requests]
    assert "temperature" in bodies[0] and "temperature" not in bodies[1] and "temperature" not in bodies[2]


def test_a_400_about_the_token_parameter_swaps_it() -> None:
    def handler(sent: Sent) -> Reply:
        if "max_tokens" in (sent.body or {}):
            return Reply(400, {"error": {"message": "use 'max_completion_tokens' instead of 'max_tokens'"}})
        return chat_reply("ok")

    with serve(handler) as server:
        assert _openai(server.url).complete("hi").text == "ok"
    assert "max_completion_tokens" in server.requests[1].body


def test_openai_typed_content_parts_and_an_empty_answer() -> None:
    parts = Reply(
        200,
        {
            "choices": [
                {"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}
            ]
        },
    )
    with serve(lambda _: parts) as server:
        assert _openai(server.url).complete("hi").text == "ab"
    with serve(lambda _: chat_reply("")) as server, pytest.raises(LLMError) as caught:
        _openai(server.url).complete("hi")
    assert caught.value.code == "LLM_REFUSED"


def test_openai_embeddings_are_batched_and_ordered_by_index() -> None:
    def handler(sent: Sent) -> Reply:
        rows = [{"index": i, "embedding": [float(i), 0.5]} for i in range(len(sent.body["input"]))]
        return Reply(200, {"data": list(reversed(rows))})

    texts = [f"t{i}" for i in range(70)]
    with serve(handler) as server:
        vectors = _openai(server.url).embed(texts)
    assert [v[0] for v in vectors] == [float(i % 64) for i in range(70)]
    assert [len(r.body["input"]) for r in server.requests] == [64, 6]
    assert server.requests[0].body["model"] == "e-1" and server.requests[0].path == "/v1/embeddings"


def test_openai_embed_without_a_model_is_a_clear_refusal() -> None:
    with pytest.raises(LLMHttpError) as caught:
        OpenAICompatibleClient(base_url="http://127.0.0.1:1").embed(["x"])
    assert caught.value.kind == "no_embeddings"


def test_openai_lists_models_sorted_deduplicated_and_capped() -> None:
    rows = [{"id": f"m-{i:03d}"} for i in range(300)] + [{"id": "m-001"}, {"nope": 1}]
    with serve(lambda _: Reply(200, {"data": rows})) as server:
        models = _openai(server.url).list_models()
    assert server.requests[0].path == "/v1/models" and server.requests[0].method == "GET"
    assert len(models) == MAX_MODELS and models == sorted(set(models)) and models[0] == "m-000"


# --- Anthropic ---------------------------------------------------------------------------------
def test_anthropic_complete_uses_x_api_key_version_and_a_top_level_system() -> None:
    with serve(lambda _: message_reply("hello")) as server:
        result = _anthropic(server.url).complete(PROMPT, system="be brief", max_tokens=40, temperature=0.2)
    sent = server.requests[0]
    assert (sent.method, sent.path) == ("POST", "/v1/messages")
    assert sent.headers["x-api-key"] == KEY and sent.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in sent.headers
    assert sent.body == {
        "model": "c-1",
        "max_tokens": 40,
        "temperature": 0.2,
        "system": "be brief",
        "messages": [{"role": "user", "content": PROMPT}],
    }
    assert (result.text, result.input_tokens, result.output_tokens, result.stop_reason) == (
        "hello",
        7,
        2,
        "end_turn",
    )


def test_anthropic_base_url_may_end_in_v1_without_doubling_it() -> None:
    with serve(lambda _: message_reply()) as server:
        AnthropicClient(base_url=server.url + "/v1", api_key=KEY, model_id="c").complete("hi")
    assert server.requests[0].path == "/v1/messages"


def test_anthropic_joins_text_blocks_and_ignores_the_rest() -> None:
    reply = Reply(
        200,
        {
            "content": [
                {"type": "thinking", "thinking": "x"},
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b"},
            ],
            "stop_reason": "max_tokens",
        },
    )
    with serve(lambda _: reply) as server:
        result = _anthropic(server.url).complete("hi")
    assert result.text == "ab" and result.stop_reason == "max_tokens" and result.input_tokens == 0


def test_anthropic_has_no_embeddings_and_lists_models() -> None:
    with serve(lambda _: Reply(200, {"data": [{"id": "b"}, {"id": "a"}]})) as server:
        client = _anthropic(server.url)
        assert client.list_models() == ["a", "b"]
        with pytest.raises(LLMHttpError) as caught:
            client.embed(["x"])
    assert caught.value.kind == "no_embeddings"
    assert server.requests[0].path.startswith("/v1/models")


def test_count_tokens_is_the_shared_approximation() -> None:
    assert _anthropic("http://x").count_tokens("a" * 40) == 10 == _openai("http://x").count_tokens("a" * 40)


# --- errors, retries, redirects --------------------------------------------------------------
LEAKY = {"error": {"message": f"Invalid key {KEY} for prompt: {PROMPT}"}}


@pytest.mark.parametrize(
    ("status", "kind", "code"),
    [
        (401, "key_rejected", "LLM_UNAVAILABLE"),
        (403, "key_rejected", "LLM_UNAVAILABLE"),
        (404, "not_found", "LLM_INVALID_REQUEST"),
        (400, "bad_request", "LLM_INVALID_REQUEST"),
        (413, "too_long", "LLM_TOO_LONG"),
        (429, "rate_limited", "LLM_UNAVAILABLE"),
        (500, "server_error", "LLM_UNAVAILABLE"),
        (503, "server_error", "LLM_UNAVAILABLE"),
    ],
)
@pytest.mark.parametrize("make", [_openai, _anthropic])
def test_errors_are_classified_and_never_quote_the_provider(
    make: object, status: int, kind: str, code: str
) -> None:
    with serve(lambda _: Reply(status, LEAKY)) as server:
        client = make(server.url, max_retries=0)  # type: ignore[operator]
        with pytest.raises(LLMHttpError) as caught:
            client.complete(PROMPT)
    exc = caught.value
    assert (exc.kind, exc.code, exc.status) == (kind, code, status)
    for text in (str(exc), exc.message, exc.fix, repr(exc), repr(client)):
        assert KEY not in text and PROMPT not in text and "Invalid key" not in text
    assert (exc.message, exc.fix) == PROBLEMS[kind]


def test_a_rejected_key_says_so_in_plain_words() -> None:
    assert "rejected the key" in PROBLEMS["key_rejected"][0]
    assert "enter it again" in PROBLEMS["key_rejected"][1]


@pytest.mark.parametrize(("status", "expected"), [(429, 3), (500, 3), (401, 1), (404, 1), (400, 1)])
def test_only_transient_failures_are_retried_up_to_max_retries(status: int, expected: int) -> None:
    with serve(lambda _: Reply(status, LEAKY)) as server, pytest.raises(LLMHttpError):
        _openai(server.url, max_retries=2).complete("hi")
    assert len(server.requests) == expected


def test_a_retry_can_succeed() -> None:
    calls = {"n": 0}

    def handler(_: Sent) -> Reply:
        calls["n"] += 1
        return Reply(503, LEAKY) if calls["n"] < 3 else chat_reply("finally")

    with serve(handler) as server:
        assert _openai(server.url, max_retries=2).complete("hi").text == "finally"
    assert len(server.requests) == 3


def test_a_redirect_is_never_followed() -> None:
    def handler(sent: Sent) -> Reply:
        if sent.path.startswith("/elsewhere"):
            return chat_reply()
        return Reply(302, {}, headers={"location": "/elsewhere"})

    with serve(handler) as server, pytest.raises(LLMHttpError) as caught:
        _openai(server.url, max_retries=2).complete("hi")
    assert caught.value.kind == "redirect"
    assert [r.path for r in server.requests] == ["/v1/chat/completions"]  # once, and not the other place


def test_unreadable_answers_are_bad_responses() -> None:
    for reply in (Reply(200, raw=b"<html>oops</html>"), Reply(200, {"choices": []}), Reply(200, {"x": 1})):
        with serve(lambda _, r=reply: r) as server, pytest.raises(LLMHttpError) as caught:
            _openai(server.url).complete("hi")
        assert caught.value.kind == "bad_response"
    with serve(lambda _: Reply(200, {"content": "no"})) as server, pytest.raises(LLMHttpError) as caught:
        _anthropic(server.url).complete("hi")
    assert caught.value.kind == "bad_response"


def test_a_refused_connection_is_unreachable() -> None:
    with pytest.raises(LLMHttpError) as caught:
        OpenAICompatibleClient(base_url="http://127.0.0.1:9", model_id="m", max_retries=0).complete("hi")
    assert caught.value.kind == "unreachable" and "127.0.0.1" not in str(caught.value)


def test_a_timeout_is_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = OpenAICompatibleClient(
        base_url="https://x.example/v1",
        model_id="m",
        max_retries=1,
        backoff_s=0.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMHttpError) as caught:
        client.complete("hi")
    assert caught.value.kind == "timeout"


def test_a_certificate_problem_is_named_as_one() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] self signed", request=request)

    client = OpenAICompatibleClient(
        base_url="https://x.example/v1", model_id="m", max_retries=0, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(LLMHttpError) as caught:
        client.complete("hi")
    assert caught.value.kind == "tls" and "CERTIFICATE" not in str(caught.value)


def test_bad_arguments_are_refused_before_any_call() -> None:
    with pytest.raises(LLMError):
        _openai("http://127.0.0.1:9").complete("hi", max_tokens=0)
    with pytest.raises(LLMError):
        OpenAICompatibleClient(base_url="http://127.0.0.1:9").complete("hi")  # no model at all
