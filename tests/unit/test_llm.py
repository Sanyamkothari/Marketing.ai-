"""`engine.llm`: the fake answers the same way twice, records what it was asked, and totals honestly.

The fake exists so the fast suite can run generative code with no network and no key. That is only
worth anything if its answers are a function of the request alone - a test that asserts on a
completion has to hold across machines and across runs - and if what it was asked is visible, so a
test can pin *how* a prompt was built rather than only what came back.
"""

from __future__ import annotations

import pytest

from engine.llm import APPROX_CHARS_PER_TOKEN, FAKE_MODEL_ID, FakeLLMClient, LLMClient, LLMError, usage_from


def test_the_fake_satisfies_the_protocol() -> None:
    assert isinstance(FakeLLMClient(), LLMClient)


def test_the_same_request_returns_the_same_answer() -> None:
    first = FakeLLMClient().complete("summarise this")
    second = FakeLLMClient().complete("summarise this")
    assert first.text == second.text
    assert first.model_id == second.model_id == FAKE_MODEL_ID


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": "a different prompt"},
        {"temperature": 0.7},
        {"max_tokens": 64},
        {"model_id": "another-fake"},
    ],
)
def test_a_different_request_returns_a_different_answer(change: dict[str, object]) -> None:
    base: dict[str, object] = {"prompt": "summarise this", "max_tokens": 512, "temperature": 0.0}
    merged = {**base, **change}
    plain = FakeLLMClient().complete(
        str(base["prompt"]),
        max_tokens=int(str(base["max_tokens"])),
        temperature=float(str(base["temperature"])),
    )
    altered = FakeLLMClient().complete(
        str(merged["prompt"]),
        model_id=merged.get("model_id"),  # type: ignore[arg-type]
        max_tokens=int(str(merged["max_tokens"])),
        temperature=float(str(merged["temperature"])),
    )
    assert plain.text != altered.text or plain.model_id != altered.model_id


def test_a_fake_completion_says_it_is_fake() -> None:
    """Plan §13.3: nothing fabricated reaches a screen, so a fake answer must be unmistakable."""
    assert FakeLLMClient().complete("anything").text.startswith("[fake completion ")


def test_a_completion_is_never_longer_than_the_ceiling() -> None:
    truncated = FakeLLMClient().complete("anything", max_tokens=2)
    assert len(truncated.text) <= 2 * APPROX_CHARS_PER_TOKEN
    assert truncated.stop_reason == "max_tokens"


def test_a_completion_that_finished_says_so() -> None:
    """A caller that retries on a cut-off answer has to be able to tell the two apart."""
    assert FakeLLMClient().complete("anything").stop_reason == "end_turn"


def test_a_ceiling_below_one_token_is_refused() -> None:
    with pytest.raises(LLMError) as raised:
        FakeLLMClient().complete("anything", max_tokens=0)
    assert raised.value.code == "LLM_INVALID_REQUEST"


def test_every_call_is_recorded_in_order() -> None:
    client = FakeLLMClient()
    client.complete("one")
    client.embed(["two", "three"])
    client.count_tokens("four")
    assert [call.kind for call in client.calls] == ["complete", "embed", "count_tokens"]
    assert client.calls[0].prompt == "one"
    assert client.calls[1].texts == ("two", "three")
    client.reset()
    assert client.calls == ()


def test_identical_texts_embed_identically_and_different_ones_do_not() -> None:
    client = FakeLLMClient()
    first, second, third = client.embed(["same", "same", "other"])
    assert first == second
    assert first != third


def test_an_embedding_is_a_unit_vector() -> None:
    (vector,) = FakeLLMClient().embed(["anything"])
    assert abs(sum(value * value for value in vector) - 1.0) < 1e-9


def test_counting_tokens_rounds_up() -> None:
    client = FakeLLMClient()
    assert client.count_tokens("") == 0
    assert client.count_tokens("a") == 1
    assert client.count_tokens("a" * (APPROX_CHARS_PER_TOKEN + 1)) == 2


def test_usage_counts_the_calls_that_sent_something() -> None:
    """`count_tokens` measures a prompt; counting it would report a round trip that never happened."""
    client = FakeLLMClient()
    first = client.complete("one")
    client.count_tokens("not a request")
    client.embed(["two"])
    usage = usage_from(client.calls)
    assert usage.calls == 2
    assert usage.output_tokens == first.output_tokens
    assert usage.model_ids == (FAKE_MODEL_ID,)


def test_usage_reports_no_cost_rather_than_zero() -> None:
    """A fabricated zero cannot be told apart from a measurement of something free."""
    assert usage_from(FakeLLMClient().calls).cost_estimate_usd is None
    assert FakeLLMClient().complete("anything").cost_estimate_usd is None


def test_usage_names_every_model_it_used_once_and_sorted() -> None:
    client = FakeLLMClient()
    client.complete("one", model_id="zulu")
    client.complete("two", model_id="alpha")
    client.complete("three", model_id="zulu")
    assert usage_from(client.calls).model_ids == ("alpha", "zulu")
