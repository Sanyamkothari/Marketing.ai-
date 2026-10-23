"""`OpenAICompatibleClient` and `AnthropicClient` against a real local HTTP server (DEC-1141).

Every assertion about what is sent is made on the server's own record of the request; every assertion
about errors checks that nothing the server said - which here deliberately quotes the prompt and the
key - reaches the exception.
"""

from __future__ import annotations

import logging

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
        (402, "out_of_credit", "LLM_UNAVAILABLE"),
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


# --- why a key was refused --------------------------------------------------------------------------
MARKER = "zz-provider-words"
"""Stands for whatever a provider says that we must not repeat; the keyword is in the same sentence."""

RETRIED = {"rate_limited", "server_error"}
"""The kinds that are asked for again; every other failure is final on the first answer."""

# (status, what the provider says, the kind that must come out). Real services word these differently;
# the rows are the phrasings the keyword lists are for, then the precedence, then the cases where a
# word must NOT count (another status), then the plain refusals that stay what they were.
REFUSED_FOR: list[tuple[int, str, str]] = [
    (401, "API key expired. Please renew the API key.", "key_expired"),
    (403, "The API key has EXPIRED", "key_expired"),
    (401, "This API key has been revoked.", "key_disabled"),
    (401, "Your API key was deactivated", "key_disabled"),
    (403, "API key is disabled", "key_disabled"),
    (403, "Consumer 'api_key:x' has been suspended.", "key_disabled"),
    (403, "Your API key has been disabled by an administrator", "key_disabled"),
    (401, "The account has been disabled", "key_disabled"),
    (403, "Project `proj_1` does not have access to model `m-1`", "no_model_access"),
    (403, "You are not allowed to sample from this model", "no_model_access"),
    (403, "Your API key does not have permission to use the specified resource.", "no_model_access"),
    (403, "The caller does not have permission", "no_model_access"),
    (403, "This key has no access to model m-1", "no_model_access"),
    (401, "insufficient_quota", "out_of_credit"),
    (403, "Your account has no credit left", "out_of_credit"),
    (401, "Quota exceeded for this project", "out_of_credit"),
    (403, "Please check your plan and billing details", "out_of_credit"),
    (429, "You exceeded your current quota. type: insufficient_quota", "out_of_credit"),
    (429, "insufficient_quota", "out_of_credit"),
    (429, "Your credit balance is too low", "out_of_credit"),
    # Anthropic words its empty account as a 400, which is not a refused key but is the same cure
    (400, "Your credit balance is too low to access the Anthropic API.", "out_of_credit"),
    # Google's Gemini words a bad key as a 400 (INVALID_ARGUMENT), not a 401: by whole phrases, live-checked
    (400, "API key expired. Please renew the API key.", "key_expired"),
    (400, "API key not valid. Please pass a valid API key.", "key_rejected"),
    (400, "Please pass a valid API key", "key_rejected"),  # its OpenAI-compatible address
    (400, "reason: API_KEY_INVALID", "key_rejected"),
    # precedence: credit, then expired, then disabled, then access
    (401, "credit has expired", "out_of_credit"),
    (403, "Account suspended: insufficient_quota", "out_of_credit"),
    (403, "billing problem, permission denied", "out_of_credit"),
    (401, "key expired and then revoked", "key_expired"),
    (403, "key expired: permission denied", "key_expired"),
    (403, "key disabled, permission denied", "key_disabled"),
    # a word counts only on the statuses it is listed for
    (401, "no permission for this", "key_rejected"),
    (401, "does not have access to model m-1", "key_rejected"),
    (429, "key expired", "rate_limited"),
    (429, "key disabled", "rate_limited"),
    (429, "permission denied", "rate_limited"),
    (400, "credit balance is high enough", "bad_request"),
    (400, "credit expired", "bad_request"),
    (400, "the key expired yesterday", "bad_request"),  # on a 400 only the whole phrases count
    (400, "the api key is wrong", "bad_request"),
    (400, "api key expired and credit balance is too low", "out_of_credit"),
    (404, "key expired", "not_found"),
    (500, "billing is disabled", "server_error"),
    # "disabled" alone says nothing about the key: a new key would fail the same way, so it stays a refused key
    (
        403,
        "Generative Language API has not been used in project 123 before or it is disabled. "
        "Enable it by visiting https://console.developers.google.com/apis/api/x/overview?project=123 then retry. "
        "reason: SERVICE_DISABLED",
        "key_rejected",
    ),
    (403, "Public access is disabled. Please configure private endpoint.", "key_rejected"),
    (403, "This feature is disabled for your plan", "key_rejected"),
    # without a word, the answer is what the status alone said before
    (401, "Incorrect API key provided", "key_rejected"),
    (401, "invalid x-api-key", "key_rejected"),
    (403, "Forbidden", "key_rejected"),
    (403, "Request not allowed", "key_rejected"),  # Anthropic, from a country or organisation it refuses
    (
        403,
        "<h1>Forbidden</h1>You don't have permission to access this resource.",
        "key_rejected",
    ),  # a web server
    (429, "Rate limit reached for requests", "rate_limited"),
    # A throttle that mentions money is still a throttle: waiting works, and "add credit" would be a false lead.
    (
        429,
        "Rate limit reached for gpt-4 on requests per min (RPM): Limit 3, Used 3. Please try again in 20s. "
        "Add a payment method at https://platform.openai.com/account/billing",
        "rate_limited",
    ),
    (
        429,
        "You exceeded your current quota, please check your plan and billing details. retryDelay 37s",
        "rate_limited",
    ),
    (429, "Quota exceeded for metric: free_tier_requests, limit: 15. Please retry in 20s", "rate_limited"),
]


_ANTHROPIC_ERROR_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "billing_error",
    403: "permission_error",
    429: "rate_limit_error",
}
"""The `error.type` Anthropic puts in the body of each status; a keyword list must not trip over these."""


def _refusal_body(text: str, *, anthropic: bool = False, status: int = 401) -> dict[str, object]:
    """A provider's refusal that says `text` and, like a real one, quotes the key and the prompt."""
    message = f"{text} {MARKER} key={KEY} prompt={PROMPT}"
    if anthropic:
        kind = _ANTHROPIC_ERROR_TYPE.get(status, "api_error")
        return {"type": "error", "error": {"type": kind, "message": message}}
    return {"error": {"message": message}}


_CODE = {"bad_request": "LLM_INVALID_REQUEST", "not_found": "LLM_INVALID_REQUEST"}


def _assert_only_our_words(exc: LLMHttpError, kind: str, *clients: object) -> None:
    """The exception carries the fixed sentences for `kind` and nothing the provider wrote."""
    assert exc.kind == kind and exc.code == _CODE.get(kind, "LLM_UNAVAILABLE")
    assert (exc.message, exc.fix) == PROBLEMS[kind]
    for text in (
        str(exc),
        exc.message,
        exc.fix,
        repr(exc),
        repr(exc.args),
        repr(vars(exc)),
        *map(repr, clients),
    ):
        assert MARKER not in text and KEY not in text and PROMPT not in text
    assert exc.__cause__ is None and exc.__context__ is None


@pytest.mark.parametrize(("status", "said", "kind"), REFUSED_FOR)
@pytest.mark.parametrize("make", [_openai, _anthropic])
def test_a_refused_key_or_throttle_says_why_and_no_provider_word_gets_out(
    make: object, status: int, said: str, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    body = _refusal_body(said, anthropic=make is _anthropic, status=status)
    with serve(lambda _: Reply(status, body)) as server:
        client = make(server.url, max_retries=2)  # type: ignore[operator]
        with pytest.raises(LLMHttpError) as caught:
            client.complete(PROMPT)
    assert caught.value.status == status
    _assert_only_our_words(caught.value, kind, client)
    assert len(server.requests) == (3 if kind in RETRIED else 1)
    assert MARKER not in caplog.text and KEY not in caplog.text


@pytest.mark.parametrize(("status", "said", "kind"), REFUSED_FOR)
@pytest.mark.parametrize("make", [OpenAICompatibleClient, AnthropicClient])
def test_the_same_reasons_are_found_in_an_error_inside_a_200(
    make: type[OpenAICompatibleClient] | type[AnthropicClient], status: int, said: str, kind: str
) -> None:
    seen: list[int] = []
    body = {"error": {"code": status, "message": f"{said} {MARKER} key={KEY} prompt={PROMPT}"}}
    client = _answering(body, seen, make=make)
    with pytest.raises(LLMHttpError) as caught:
        client.complete(PROMPT)
    assert caught.value.status == status
    _assert_only_our_words(caught.value, kind, client)
    assert len(seen) == (3 if kind in RETRIED else 1)


@pytest.mark.parametrize("make", [OpenAICompatibleClient, AnthropicClient])
def test_a_refusal_inside_a_200_fails_a_model_list_with_the_reason(
    make: type[OpenAICompatibleClient] | type[AnthropicClient],
) -> None:
    with pytest.raises(LLMHttpError) as listing:
        _answering({"error": {"code": 403, "message": "key expired"}}, make=make).list_models()
    assert listing.value.kind == "key_expired"
    with pytest.raises(LLMHttpError) as embedding:  # the Anthropic client has none and never asks
        _answering({"error": {"code": 402}}, make=make).embed(["hi"])
    assert embedding.value.kind == ("out_of_credit" if make is OpenAICompatibleClient else "no_embeddings")


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize(
    "reply",
    [
        lambda status: Reply(status, {}),
        lambda status: Reply(status, raw=b""),
        lambda status: Reply(status, raw=b"<html><body>Forbidden</body></html>"),
        lambda status: Reply(status, LEAKY),
        lambda status: Reply(status, _refusal_body("invalid_api_key")),
        # Anthropic's own shape: every 403 body names `permission_error`, which says nothing about a model
        lambda status: Reply(status, _refusal_body("Request not allowed", anthropic=True, status=status)),
    ],
    ids=["empty-object", "empty-body", "html", "leaky", "invalid-key", "anthropic-shape"],
)
@pytest.mark.parametrize("make", [_openai, _anthropic])
def test_a_bare_401_or_403_stays_a_rejected_key(make: object, status: int, reply: object) -> None:
    with serve(lambda _: reply(status)) as server:  # type: ignore[operator]
        client = make(server.url, max_retries=2)  # type: ignore[operator]
        with pytest.raises(LLMHttpError) as caught:
            client.complete(PROMPT)
    _assert_only_our_words(caught.value, "key_rejected", client)
    assert len(server.requests) == 1


@pytest.mark.parametrize("said", ["", "anything at all", "key expired", "rate limit"])
@pytest.mark.parametrize("make", [_openai, _anthropic])
def test_a_402_is_out_of_credit_whatever_it_says(make: object, said: str) -> None:
    with serve(lambda _: Reply(402, _refusal_body(said, anthropic=make is _anthropic, status=402))) as server:
        client = make(server.url, max_retries=2)  # type: ignore[operator]
        with pytest.raises(LLMHttpError) as caught:
            client.complete(PROMPT)
    _assert_only_our_words(caught.value, "out_of_credit", client)
    assert caught.value.status == 402 and len(server.requests) == 1


@pytest.mark.parametrize(
    ("said", "kind", "asked"),
    [
        ("Rate limit reached for requests, slow down", "rate_limited", 3),
        (
            "Rate limit reached on requests per min. Add a payment method: platform.openai.com/account/billing",
            "rate_limited",
            3,
        ),
        ("insufficient_quota", "out_of_credit", 1),
    ],
)
@pytest.mark.parametrize("make", [_openai, _anthropic])
def test_a_429_with_no_credit_is_not_asked_again_while_an_ordinary_429_still_is(
    make: object, said: str, kind: str, asked: int
) -> None:
    reply = Reply(
        429, _refusal_body(said, anthropic=make is _anthropic, status=429), headers={"retry-after": "0"}
    )
    with serve(lambda _: reply) as server, pytest.raises(LLMHttpError) as caught:
        make(server.url, max_retries=2).complete("hi")  # type: ignore[operator]
    assert (caught.value.kind, len(server.requests)) == (kind, asked)


def test_only_the_start_of_a_body_is_read_and_a_bad_byte_is_skipped() -> None:
    def refused(raw: bytes) -> str:
        with serve(lambda _: Reply(401, raw=raw)) as server, pytest.raises(LLMHttpError) as caught:
            _openai(server.url, max_retries=0).complete("hi")
        return caught.value.kind

    assert refused(b" " * 3000 + b"key EXPIRED") == "key_expired"
    assert refused(b" " * 5000 + b"key expired") == "key_rejected"  # past the first 4096 bytes
    assert refused(b"\xff\xfe key expired \xc3") == "key_expired"  # not UTF-8 at all


def test_the_new_kinds_say_what_to_do_in_plain_words() -> None:
    for kind in ("key_expired", "key_disabled", "no_model_access", "out_of_credit"):
        message, fix = PROBLEMS[kind]
        assert message.startswith("The AI service") and message.endswith(".") and fix.endswith(".")
        assert not LLMHttpError(kind).retry_after and LLMHttpError(kind).code == "LLM_UNAVAILABLE"


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


# --- an answer with no text: out of room, or just empty ---------------------------------------------
NOTHING_TO_READ = "The model returned nothing to read."


def _openai_without_text(finish_reason: str | None) -> Reply:
    """What a model that reasons first sends when it is stopped in its thinking: an empty `content`."""
    choice: dict[str, object] = {
        "message": {"role": "assistant", "content": "", "reasoning_content": f"thinking about {PROMPT}"}
    }
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return Reply(200, {"choices": [choice], "usage": {"prompt_tokens": 40, "completion_tokens": 800}})


def _anthropic_without_text(stop_reason: str) -> Reply:
    return Reply(
        200,
        {
            "content": [{"type": "thinking", "thinking": f"thinking about {PROMPT}"}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 40, "output_tokens": 800},
        },
    )


@pytest.mark.parametrize(
    ("make", "reply"),
    [(_openai, _openai_without_text("length")), (_anthropic, _anthropic_without_text("max_tokens"))],
)
def test_an_answer_stopped_by_the_token_ceiling_before_any_text_is_out_of_room(
    make: object, reply: Reply
) -> None:
    with serve(lambda _: reply) as server:
        client = make(server.url)  # type: ignore[operator]
        with pytest.raises(LLMError) as caught:
            client.complete(PROMPT, max_tokens=800)
    exc = caught.value
    assert isinstance(exc, LLMHttpError)
    assert (exc.kind, exc.code, exc.model_id) == ("out_of_room", "LLM_REFUSED", client.model_id)
    assert (exc.message, exc.fix) == PROBLEMS["out_of_room"]
    for text in (str(exc), exc.message, exc.fix, repr(exc)):
        assert PROMPT not in text and KEY not in text
    assert len(server.requests) == 1, "asking again with the same allowance would change nothing"


def test_the_out_of_room_fix_names_the_allowance_of_every_purpose() -> None:
    """Any purpose can run out of room, and each is sent one of two settings: the fix names both."""
    _message, fix = PROBLEMS["out_of_room"]
    assert "generative.llm.max_output_tokens" in fix
    assert "generative.llm.data_agent_max_output_tokens" in fix


@pytest.mark.parametrize(
    ("make", "reply"),
    [(_openai, _openai_without_text("length")), (_anthropic, _anthropic_without_text("max_tokens"))],
)
def test_an_out_of_room_failure_carries_the_tokens_the_provider_billed_and_nothing_it_wrote(
    make: object, reply: Reply
) -> None:
    """The whole allowance was spent, so the call is counted from the numbers the answer reports."""
    with serve(lambda _: reply) as server, pytest.raises(LLMError) as caught:
        make(server.url).complete(PROMPT, max_tokens=800)  # type: ignore[operator]
    exc = caught.value
    assert (exc.input_tokens, exc.output_tokens) == (40, 800)
    assert not any(PROMPT in str(value) for value in vars(exc).values())


def test_an_out_of_room_answer_that_reports_no_usage_is_billed_as_zero_not_guessed() -> None:
    reply = Reply(200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    with serve(lambda _: reply) as server, pytest.raises(LLMError) as caught:
        _openai(server.url).complete(PROMPT, max_tokens=800)
    assert (caught.value.input_tokens, caught.value.output_tokens) == (0, 0)


@pytest.mark.parametrize(
    ("make", "reply"),
    [
        (_openai, _openai_without_text("stop")),
        (_openai, _openai_without_text("content_filter")),
        (_openai, _openai_without_text(None)),
        (_anthropic, _anthropic_without_text("end_turn")),
        (_anthropic, _anthropic_without_text("refusal")),
    ],
)
def test_an_empty_answer_that_stopped_for_any_other_reason_is_still_nothing_to_read(
    make: object, reply: Reply
) -> None:
    with serve(lambda _: reply) as server, pytest.raises(LLMError) as caught:
        make(server.url).complete(PROMPT, max_tokens=800)  # type: ignore[operator]
    exc = caught.value
    assert not isinstance(exc, LLMHttpError)
    assert (exc.code, exc.message) == ("LLM_REFUSED", NOTHING_TO_READ)
    assert PROMPT not in str(exc)


def test_text_written_before_the_token_ceiling_is_still_returned_as_cut_off() -> None:
    reply = Reply(200, {"choices": [{"message": {"content": "half an ans"}, "finish_reason": "length"}]})
    with serve(lambda _: reply) as server:
        result = _openai(server.url).complete("hi")
    assert (result.text, result.stop_reason) == ("half an ans", "length")


# --- an error inside a 200 (OpenRouter and others; DEC-1269) ---------------------------------------
def _answering(
    body: object,
    seen: list[int] | None = None,
    *,
    make: type[OpenAICompatibleClient] | type[AnthropicClient] = OpenAICompatibleClient,
) -> OpenAICompatibleClient | AnthropicClient:
    """A client whose every request gets `200 OK` with `body`, through a fake transport."""

    def handler(_request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(1)
        return httpx.Response(200, json=body)

    return make(
        base_url="https://gateway.example.test/v1",
        api_key=KEY,
        model_id="m-1",
        embedding_model_id="e-1",
        max_retries=2,
        backoff_s=0.0,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    ("code", "kind", "attempts"),
    [
        (502, "server_error", 3),
        (503, "server_error", 3),
        (429, "rate_limited", 3),
        (401, "key_rejected", 1),
        (403, "key_rejected", 1),
        (402, "out_of_credit", 1),
        (400, "bad_request", 1),
        (404, "not_found", 1),
    ],
)
def test_an_error_inside_a_200_is_classified_like_the_same_status(
    code: int, kind: str, attempts: int
) -> None:
    seen: list[int] = []
    body = {"error": {"code": code, "message": f"{PROMPT} {KEY} upstream said no"}}
    with pytest.raises(LLMHttpError) as caught:
        _answering(body, seen).complete(PROMPT)
    exc = caught.value
    assert (exc.kind, exc.status) == (kind, code)
    assert (exc.message, exc.fix) == PROBLEMS[kind]
    assert len(seen) == attempts  # only the transient ones are retried, as for a real status
    for text in (str(exc), exc.message, exc.fix, repr(exc)):
        assert KEY not in text and PROMPT not in text and "upstream said no" not in text


@pytest.mark.parametrize(
    "error", [{"message": "the model failed"}, {"code": "provider_down"}, "failed", True]
)
def test_an_error_inside_a_200_without_a_status_is_a_problem_on_the_service_side(error: object) -> None:
    with pytest.raises(LLMHttpError) as caught:
        _answering({"error": error}).complete("hi")
    assert caught.value.kind == "server_error"


def test_an_error_inside_a_200_fails_embeddings_and_model_lists_too() -> None:
    body = {"error": {"code": 401, "message": "no"}}
    with pytest.raises(LLMHttpError) as embedding:
        _answering(body).embed(["hi"])
    with pytest.raises(LLMHttpError) as listing:
        _answering(body).list_models()
    assert embedding.value.kind == listing.value.kind == "key_rejected"


@pytest.mark.parametrize("error", [None, {}, ""])
def test_an_empty_error_beside_a_real_answer_is_not_an_error(error: object) -> None:
    body = {
        "error": error,
        "choices": [{"message": {"content": "fine"}, "finish_reason": "stop"}],
        "usage": {},
    }
    assert _answering(body).complete("hi").text == "fine"


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


def _timing_out(error: type[httpx.TimeoutException], seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        raise error("slow", request=request)

    return httpx.MockTransport(handler)


def _either(transport: httpx.MockTransport) -> list[OpenAICompatibleClient | AnthropicClient]:
    arguments: dict[str, object] = {
        "base_url": "https://x.example/v1",
        "model_id": "m",
        "max_retries": 2,
        "backoff_s": 0.0,
        "transport": transport,
    }
    return [OpenAICompatibleClient(**arguments), AnthropicClient(**arguments)]  # type: ignore[arg-type]


@pytest.mark.parametrize("which", [0, 1], ids=["openai", "anthropic"])
def test_a_completion_that_was_sent_and_not_answered_in_time_is_not_sent_again(which: int) -> None:
    """The service is probably still generating (and billing) it; a second request would only start another."""
    seen: list[str] = []
    client = _either(_timing_out(httpx.ReadTimeout, seen))[which]
    with pytest.raises(LLMHttpError) as caught:
        client.complete("hi", max_tokens=4096)
    assert (caught.value.kind, caught.value.sent) == ("timeout", True)
    assert seen == ["POST"]


@pytest.mark.parametrize("which", [0, 1], ids=["openai", "anthropic"])
def test_a_request_that_never_got_through_is_asked_again(which: int) -> None:
    seen: list[str] = []
    client = _either(_timing_out(httpx.ConnectTimeout, seen))[which]
    with pytest.raises(LLMHttpError) as caught:
        client.complete("hi")
    assert (caught.value.kind, caught.value.sent) == ("timeout", False)
    assert seen == ["POST"] * 3


def test_a_listing_that_times_out_is_still_asked_again_since_asking_costs_nothing() -> None:
    seen: list[str] = []
    client, _ = _either(_timing_out(httpx.ReadTimeout, seen))
    with pytest.raises(LLMHttpError) as caught:
        client.list_models()
    assert (caught.value.kind, caught.value.sent) == ("timeout", False)
    assert seen == ["GET"] * 3


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
