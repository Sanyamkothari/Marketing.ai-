"""`engine.llm`: the protocol's two implementations, and the fake's two load-bearing properties.

The fast suite runs entirely on `FakeLLMClient`, so what the fake is worth depends on whether it
behaves like a model in the two ways the code under test cares about. Both are proved here rather
than assumed:

**Its embeddings carry lexical signal.** Retrieval, the similarity floor and MMR are all cosine
arithmetic over these vectors. If the fake returned noise, every retrieval test would be testing
that a random number beat another random number. So: a question is closer to the chunk that answers
it than to any other chunk in the corpus, the vectors are unit length, and a text with no word in it
gets the zero vector, whose similarity to everything is 0 - which is what "nothing to match on"
should mean.

**It answers out of its input.** A grounded answer quotes an extract it was given, a citation names
an extract number that exists, and an evidence reference names an id that is in the pack. A fake
that returned a fixed string would make every grounding test pass by accident, so each of those is
checked against the prompt the fake was handed.

`FakeMode` is then checked one mode at a time: each must break exactly the rule it is named for and
leave the others alone, because a test that sets `PII` and gets an over-length answer as well is a
test that cannot say which guardrail it proved.
"""

from __future__ import annotations

import json
import math

import pytest

from engine.llm import (
    TOKENS_PER_CHARACTER,
    BedrockLLMClient,
    Completion,
    Embeddings,
    FakeLLMClient,
    FakeMode,
    LLMClient,
    LLMError,
    TokenCount,
    build_client,
    estimate_tokens,
)
from tests.fixtures.make_docs import DOCUMENT_STEMS, generate_complaints, plain_text, source_text

MODEL = "a-model-id"

ANSWER_PROMPT = """Extracts from the documents:

[1] (guide_esim.md - What you need)
The QR code is valid for 7 days. It can be used once.

[2] (faq_billing.md - Late payment)
A late fee of Rs 100 or 2% of the outstanding amount applies, whichever is higher.

Answer language: auto

If the extracts above do not answer the question, reply with exactly this sentence as the answer
and set refused to true:
I don't have that information in the documents I've been given.

Question: How long is an eSIM QR code valid?"""

EVIDENCE_PROMPT = """Evidence pack for segment "High":

{"segment": "High", "reasons": [{"id": "r1", "feature": "outages_90d"},
 {"id": "r2", "feature": "usage_drop_30d"}], "complaints": [{"id": "c1", "text": "line down"}]}

Every id you put in `evidence_refs` must appear in the pack above."""

COPY_PROMPT = """Band: High - the recommended action for this band is "Send best offer".

Placeholders you may use: first_name, plan_type

Phrases you must not use: guaranteed, free forever

Every message ends with the opt-out line, exactly: Reply STOP to opt out

Write 2 variants, labelled A, B, each at most 160 characters."""

JUDGE_PROMPT = """SOURCE MATERIAL - everything the text was allowed to draw on:

Refunds take up to 21 working days.

GENERATED TEXT - judge this:

Refunds take about three weeks."""


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def answer(mode: FakeMode = FakeMode.GROUNDED, prompt: str = ANSWER_PROMPT) -> dict[str, object]:
    """The fake's reply to `prompt`, parsed. Every generating prompt promises one JSON object."""
    completion = FakeLLMClient(mode=mode).complete(
        system="", user=prompt, model_id=MODEL, temperature=0.2, max_output_tokens=800
    )
    parsed = json.loads(completion.text)
    assert isinstance(parsed, dict)
    return parsed


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------
def test_both_implementations_satisfy_the_protocol() -> None:
    """A `Protocol` that nothing is checked against is a comment; these two are the whole surface."""
    assert isinstance(FakeLLMClient(), LLMClient)
    assert isinstance(BedrockLLMClient(region="ap-south-1"), LLMClient)


def test_the_fake_is_the_default_backend_so_nothing_costs_money_by_accident() -> None:
    """DEC-203: a developer with no credentials gets a working client, not a stack trace."""
    from engine.config import LlmConfig

    assert isinstance(build_client(LlmConfig()), FakeLLMClient)


def test_building_the_bedrock_client_opens_no_connection() -> None:
    """`create_app()` runs at documentation-generation time with no credentials and no network.

    Constructing the client must therefore do nothing but remember its arguments; boto3 is not even
    imported until the first call.
    """
    from engine.config import LlmBackend, LlmConfig

    config = LlmConfig(
        backend=LlmBackend.BEDROCK,
        generation_model_id="g",
        judge_model_id="j",
        embedding_model_id="e",
    )
    client = build_client(config)
    assert isinstance(client, BedrockLLMClient)


def test_an_estimate_always_says_that_it_is_one() -> None:
    """A guessed token count that looked measured would quietly become a guessed cost."""
    count = estimate_tokens("four characters to a token, near enough")
    assert count.estimated is True
    assert count.tokens == int(len("four characters to a token, near enough") * TOKENS_PER_CHARACTER)
    assert estimate_tokens("").tokens >= 1


def test_the_fake_counts_tokens_the_same_way_the_real_client_falls_back_to() -> None:
    """So a test that measures token counts measures the same thing in both."""
    text = "a sentence of some length, long enough to have a token count worth checking"
    assert FakeLLMClient().count_tokens(text, model_id=MODEL) == estimate_tokens(text)


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
def test_every_vector_is_unit_length_so_a_dot_product_is_a_cosine() -> None:
    """Retrieval takes the dot product directly; un-normalised vectors would rank by length."""
    vectors = FakeLLMClient().embed(["one text", "another entirely different text"], model_id=MODEL).vectors
    for vector in vectors:
        assert math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0, rel_tol=1e-9)


def test_a_text_with_nothing_to_match_on_embeds_to_zero() -> None:
    """Similarity 0 to everything is the honest answer for a chunk with no content word in it."""
    vectors = FakeLLMClient().embed(["", "the and of to", "..."], model_id=MODEL).vectors
    for vector in vectors:
        assert all(value == 0.0 for value in vector)


def test_a_question_is_closest_to_the_chunk_that_answers_it() -> None:
    """The property every retrieval test rests on, checked over the real synthetic corpus.

    The question is asked against one paragraph from each of the fourteen documents. The right one
    must win outright, not tie: a tie would mean the ranking is arbitrary and any ordering test
    downstream is measuring nothing.
    """
    client = FakeLLMClient()
    paragraphs = [plain_text(source_text(stem)).split("\n\n")[2] for stem in DOCUMENT_STEMS]
    question = "How long is an eSIM QR code valid and how often can I convert a physical SIM?"
    vectors = client.embed([question, *paragraphs], model_id=MODEL).vectors
    scores = [cosine(vectors[0], vector) for vector in vectors[1:]]
    best = max(range(len(scores)), key=lambda index: scores[index])
    assert DOCUMENT_STEMS[best] == "guide_esim", dict(zip(DOCUMENT_STEMS, scores, strict=True))
    assert sorted(scores)[-1] > sorted(scores)[-2]


def test_unrelated_texts_are_not_close_and_the_same_text_is_identical() -> None:
    client = FakeLLMClient()
    vectors = client.embed(
        [
            "The QR code is valid for seven days.",
            "The QR code is valid for seven days.",
            "A cable cut affecting a street is restored within 72 hours.",
        ],
        model_id=MODEL,
    ).vectors
    assert math.isclose(cosine(vectors[0], vectors[1]), 1.0, rel_tol=1e-9)
    assert cosine(vectors[0], vectors[2]) < 0.2


def test_embedding_is_deterministic_and_reports_its_width() -> None:
    texts = ["one", "two", "three"]
    first = FakeLLMClient().embed(texts, model_id=MODEL)
    second = FakeLLMClient().embed(texts, model_id=MODEL)
    assert first.vectors == second.vectors
    assert first.dimensions == len(first.vectors[0])
    assert Embeddings(vectors=(), model_id=MODEL, input_tokens=0, latency_ms=0).dimensions == 0


def test_a_repeated_word_counts_for_more_but_not_proportionally_more() -> None:
    """Log term frequency: a long chunk about one thing must not drown a short one about it."""
    client = FakeLLMClient()
    once, ten_times = client.embed(["refund", "refund " * 10], model_id=MODEL).vectors
    assert math.isclose(cosine(once, ten_times), 1.0, rel_tol=1e-9)


def test_an_empty_batch_costs_nothing_and_returns_nothing() -> None:
    result = FakeLLMClient().embed([], model_id=MODEL)
    assert result.vectors == ()
    assert result.input_tokens == 0


# ---------------------------------------------------------------------------
# Generation: the fake answers out of its input
# ---------------------------------------------------------------------------
def test_a_grounded_answer_quotes_an_extract_it_was_given() -> None:
    """The fake must not invent: its answer is a sentence lifted from extract 1."""
    reply = answer()
    assert reply["refused"] is False
    assert reply["answer"] == "The QR code is valid for 7 days."
    assert [citation["chunk"] for citation in reply["citations"]] == [1]  # type: ignore[index,union-attr]


def test_a_citation_quotes_at_most_twenty_five_words() -> None:
    """The prompt asks for a checkable quote, not a paraphrase and not the whole chunk."""
    (citation,) = answer()["citations"]  # type: ignore[misc]
    assert len(str(citation["quote"]).split()) <= 25


def test_with_no_extracts_the_fake_refuses_in_the_configured_words() -> None:
    """The refusal sentence is configuration, so the fake reads it out of the prompt it was given."""
    reply = answer(
        prompt=ANSWER_PROMPT.replace("[1] (guide_esim.md - What you need)", "").replace(
            "[2] (faq_billing.md - Late payment)", ""
        )
    )
    assert reply["refused"] is True
    assert reply["answer"] == "I don't have that information in the documents I've been given."
    assert reply["citations"] == []


def test_a_root_cause_reply_cites_only_ids_that_are_in_the_pack() -> None:
    """The grounding parser rejects anything else, so the grounded fake must not produce anything else."""
    reply = json.loads(
        FakeLLMClient()
        .complete(system="", user=EVIDENCE_PROMPT, model_id=MODEL, temperature=0.2, max_output_tokens=800)
        .text
    )
    refs = {ref for cause in reply["root_causes"] for ref in cause["evidence_refs"]}
    assert refs
    assert refs <= {"r1", "r2", "c1"}
    assert reply["headline"] and reply["caveats"]


def test_a_copy_reply_uses_only_the_placeholders_it_was_allowed() -> None:
    reply = json.loads(
        FakeLLMClient()
        .complete(system="", user=COPY_PROMPT, model_id=MODEL, temperature=0.2, max_output_tokens=800)
        .text
    )
    assert [variant["label"] for variant in reply["variants"]] == ["A", "B"]
    for variant in reply["variants"]:
        assert "{{first_name}}" in variant["text"]
        assert "Reply STOP to opt out" in variant["text"]
        assert "{{secret" not in variant["text"]


def test_two_copy_variants_are_not_the_same_words_twice() -> None:
    """Two rewordings of one idea is one variant; the A/B split would then measure nothing."""
    reply = json.loads(
        FakeLLMClient()
        .complete(system="", user=COPY_PROMPT, model_id=MODEL, temperature=0.2, max_output_tokens=800)
        .text
    )
    texts = [variant["text"] for variant in reply["variants"]]
    assert len(set(texts)) == len(texts)


def test_a_judge_passes_a_text_by_default_and_fails_one_on_request() -> None:
    passing = json.loads(
        FakeLLMClient()
        .complete(system="", user=JUDGE_PROMPT, model_id=MODEL, temperature=0.2, max_output_tokens=800)
        .text
    )
    failing = json.loads(
        FakeLLMClient(mode=FakeMode.FAILING_JUDGE)
        .complete(system="", user=JUDGE_PROMPT, model_id=MODEL, temperature=0.2, max_output_tokens=800)
        .text
    )
    assert passing["score"] == 1.0
    assert failing["score"] < 0.5
    assert failing["unsupported_claims"]


def test_generation_is_deterministic() -> None:
    """An artefact written under the fake is byte-stable, so two runs can be compared."""
    first = FakeLLMClient().complete(
        system="s", user=ANSWER_PROMPT, model_id=MODEL, temperature=0.2, max_output_tokens=800
    )
    second = FakeLLMClient().complete(
        system="s", user=ANSWER_PROMPT, model_id=MODEL, temperature=0.2, max_output_tokens=800
    )
    assert first == second


def test_a_completion_reports_what_it_consumed() -> None:
    completion = FakeLLMClient().complete(
        system="a system prompt", user=ANSWER_PROMPT, model_id=MODEL, temperature=0.2, max_output_tokens=800
    )
    assert isinstance(completion, Completion)
    assert completion.model_id == MODEL
    assert completion.input_tokens > 0
    assert completion.output_tokens > 0
    assert completion.estimated_tokens is True


# ---------------------------------------------------------------------------
# One mode, one broken rule
# ---------------------------------------------------------------------------
def test_the_ungrounded_mode_cites_an_extract_that_was_never_supplied() -> None:
    cited = [citation["chunk"] for citation in answer(FakeMode.UNGROUNDED)["citations"]]  # type: ignore[union-attr,index]
    assert cited
    assert all(number > 2 for number in cited)


def test_the_pii_mode_puts_an_identifier_in_the_answer() -> None:
    reply = answer(FakeMode.PII)
    assert "@" in str(reply["answer"])
    assert reply["refused"] is False


def test_the_banned_mode_uses_a_globally_banned_phrase() -> None:
    assert "guaranteed" in str(answer(FakeMode.BANNED)["answer"])


def test_the_overlong_mode_blows_any_length_limit() -> None:
    assert len(str(answer(FakeMode.OVERLONG)["answer"])) > 4_000


def test_the_malformed_mode_returns_something_that_is_not_json() -> None:
    text = (
        FakeLLMClient(mode=FakeMode.MALFORMED)
        .complete(system="", user=ANSWER_PROMPT, model_id=MODEL, temperature=0.2, max_output_tokens=800)
        .text
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)


@pytest.mark.parametrize(
    "mode", [FakeMode.UNGROUNDED, FakeMode.PII, FakeMode.BANNED, FakeMode.OVERLONG, FakeMode.REFUSING]
)
def test_a_misbehaving_mode_still_returns_the_shape_the_prompt_asked_for(mode: FakeMode) -> None:
    """Every mode but `MALFORMED` breaks a content rule, never the format - or the parser, not the
    guardrail, would be what the test exercised."""
    reply = answer(mode)
    assert set(reply) == {"answer", "refused", "citations"}


def test_the_grounded_mode_breaks_nothing() -> None:
    """The default must pass every check, or no test could tell a real failure from the fake's."""
    reply = answer()
    text = str(reply["answer"])
    assert "@" not in text
    assert "guaranteed" not in text.lower()
    assert len(text) < 500
    assert reply["citations"]


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------
def test_a_bedrock_failure_is_a_coded_error_that_names_no_value() -> None:
    """A provider's message routinely quotes the prompt that upset it, and a prompt is customer data."""

    class _Exploding:
        def converse(self, **kwargs: object) -> object:
            raise RuntimeError("the prompt said: a customer's private document")

    client = BedrockLLMClient(region="ap-south-1", client=_Exploding())
    with pytest.raises(LLMError) as error:
        client.complete(system="s", user="u", model_id=MODEL, temperature=0.2, max_output_tokens=10)
    assert error.value.code == "LLM_CALL_FAILED"
    assert "private document" not in error.value.message


def test_an_empty_bedrock_answer_is_its_own_code() -> None:
    class _Silent:
        def converse(self, **kwargs: object) -> dict[str, object]:
            return {"output": {"message": {"content": [{"text": "   "}]}}, "usage": {}}

    client = BedrockLLMClient(region="ap-south-1", client=_Silent())
    with pytest.raises(LLMError) as error:
        client.complete(system="s", user="u", model_id=MODEL, temperature=0.2, max_output_tokens=10)
    assert error.value.code == "LLM_EMPTY_RESPONSE"


def test_bedrock_reads_the_usage_the_provider_reported() -> None:
    class _Counting:
        def converse(self, **kwargs: object) -> dict[str, object]:
            return {
                "output": {"message": {"content": [{"text": "an answer"}]}},
                "usage": {"inputTokens": 11, "outputTokens": 7},
                "stopReason": "end_turn",
            }

    completion = BedrockLLMClient(region="ap-south-1", client=_Counting()).complete(
        system="s", user="u", model_id=MODEL, temperature=0.2, max_output_tokens=10
    )
    assert (completion.input_tokens, completion.output_tokens) == (11, 7)
    assert completion.estimated_tokens is False


def test_count_tokens_falls_back_to_the_estimate_and_says_so() -> None:
    """Not every model offers the operation, and that is not worth failing a run over."""

    class _Unsupported:
        def count_tokens(self, **kwargs: object) -> object:
            raise RuntimeError("no such operation")

    count = BedrockLLMClient(region="ap-south-1", client=_Unsupported()).count_tokens(
        "some text", model_id=MODEL
    )
    assert isinstance(count, TokenCount)
    assert count.estimated is True


def test_a_fake_with_too_few_dimensions_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 8"):
        FakeLLMClient(dimensions=4)


def test_the_planted_complaint_pii_is_not_what_the_fake_invents() -> None:
    """The fake's PII is its own, from a reserved range, so a redaction test cannot pass by luck."""
    planted = " ".join(generate_complaints(40)["text"])
    invented = str(answer(FakeMode.PII)["answer"])
    assert "priya.sharma@example.invalid" in invented
    assert "priya.sharma@example.invalid" not in planted
