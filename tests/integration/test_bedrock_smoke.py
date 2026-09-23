"""The opt-in proof that `BedrockLLMClient` works against real Amazon Bedrock, not just a mock.

`tests/unit/generative/test_grounded_fake.py` already proves every line of `BedrockLLMClient`'s
error handling - a `converse` that raises, one that answers with whitespace, one that reports usage
- against a hand-written double that never leaves the process. That is right for the fast suite
(DEC-203: the fake is the default so nothing costs money by accident) and it is also, by
construction, incapable of proving the one thing a mock cannot mock: that a real model's response
actually parses into `LLMCompletion` the way the code assumes it will, and that a real embedding
model places related text near itself and unrelated text away from it. The grounded `FakeLLMClient` was
built to make retrieval testable offline (DEC-214), by hashing words into buckets so shared
vocabulary means proximity - and DEC-219 writes down exactly where that stops being enough: a
question that shares the domain's *vocabulary* without sharing its *meaning* ("How many employees
does Northwind Telecom have?") scores ABOVE several genuine questions under the fake, because a bag
of words cannot tell "the same words" from "the same question" apart. Nothing offline can disprove
that a real embedding model gets this right. Only Bedrock can, which is why this module exists and
why it is the only place in the repository allowed to call it.

Every test below is `pytest.mark.bedrock` and self-skips, loudly, unless a person has both exported
every `BEDROCK_SMOKE_*` variable this module reads (see `docs/AWS_DEPLOYMENT.md`) AND has real AWS
credentials boto3 can find. The two checks are deliberately ordered: the environment variables are
read first, with no import of `boto3` at all, so a machine that never opted in never touches
network or credential resolution and skips in well under a second; only once every variable is
present does the module ask boto3 whether it actually has something to authenticate with, which is
the one check bounded by botocore's own (short) timeouts rather than by us. Nothing in this module
retries a failed call beyond `_MAX_RETRIES`, and every completion caps `max_tokens` in the tens, not
the hundreds, so "opt-in and real" never means "slow" or "open-ended".

**What one full run costs.** Seven tests share two session-scoped fixtures, so the module makes
exactly one `Converse` call (a two-sentence prompt, `max_tokens=32`), one `CountTokens` call (which
Bedrock does not bill as generation), and four embedding calls of a sentence or less each - roughly
a few hundred tokens moved in total, end to end. `CountTokens` and embeddings are priced far below
generation on every Bedrock model this was checked against, so the whole module's cost is
dominated by that single completion; at typical small-model pricing that is a fraction of a US
cent, and even a large judge-grade model would not carry this over a few cents. Nothing here is a
promise of a price - `configs/llm_prices.yaml` ships empty on purpose (DEC-208) and this module
does not second-guess it - it is a description of how the module was written to stay cheap: one
completion, one token count, four short embeddings, nothing repeated, nothing padded.

**Why each test earns its money.** A completion or an embedding is not proven merely by not
raising: `test_a_completion_comes_back_non_empty_and_reports_real_token_counts` is what tells apart
"the call succeeded" from "the call succeeded and returned something a screen could show, with
token counts a budget could actually charge". The embedding tests spend four short calls on the one
property that offline testing cannot buy at any price: that a real model's geometry, not a hash
table's, is what makes retrieval trustworthy. And the error and token-counting tests each stand in
for a whole family of "does this really behave the way `engine/llm.py` promises" questions that a
mock, being written by the same person who wrote the promise, cannot fail to agree with.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Final

import pytest

from engine.config import RagConfig
from engine.generative.vectorstore import cosine
from engine.llm import BedrockLLMClient, LLMCompletion, LLMError

pytestmark = [pytest.mark.integration, pytest.mark.bedrock]

# ---------------------------------------------------------------------------
# Opt-in configuration. Read straight from `os.environ`, deliberately not through
# `engine.settings.Settings`: that module speaks for the whole application's single backend choice
# and one Bedrock model id, where this module needs a generation id and a separate embedding id to
# run independent proofs against, and is happy to be a smaller, louder, test-only vocabulary rather
# than borrow a shape built for something else.
# ---------------------------------------------------------------------------
_REGION_VAR: Final[str] = "BEDROCK_SMOKE_REGION"
_GENERATION_MODEL_VAR: Final[str] = "BEDROCK_SMOKE_GENERATION_MODEL_ID"
_EMBEDDING_MODEL_VAR: Final[str] = "BEDROCK_SMOKE_EMBEDDING_MODEL_ID"
_EMBEDDING_DIMENSIONS_VAR: Final[str] = "BEDROCK_SMOKE_EMBEDDING_DIMENSIONS"
"""Optional. Set it to assert the embedding model's exact width; unset, the tests only assert that
the width is positive and identical across calls - a model id is deployment data (DEC-204), and so,
by the same reasoning, is how wide its vectors are."""

_REQUIRED_VARS: Final[tuple[str, ...]] = (_REGION_VAR, _GENERATION_MODEL_VAR, _EMBEDDING_MODEL_VAR)

_TIMEOUT_S: Final[int] = 30
_MAX_RETRIES: Final[int] = 1
"""Bounded low on purpose: a legitimate call to a reachable model does not need boto3's own retry
loop, and a broken one should say so quickly rather than after several backed-off attempts."""

_SHORT_PROMPT: Final[str] = (
    "In one short sentence, no more than twenty words, say what a SIM card physically is."
)
_MAX_OUTPUT_TOKENS: Final[int] = 32

# The embedding sample: four short strings, one call to `embed`, reused by every embedding test
# below so the module never pays for the same vector twice. `_ANSWERING_TEXT` is written the way
# `chunking.embedding_text` actually embeds a chunk - the document's own heading in front of the
# sentence that answers the question (DEC-217) - both because that is what a real index does and
# because the document's title is where "Northwind Telecom" lives: without it, the off-topic
# question below would share no vocabulary with anything here and the DEC-219 comparison would be
# trivial rather than the real one.
_GENUINE_QUESTION: Final[str] = "How long is an eSIM QR code valid?"
_ANSWERING_TEXT: Final[str] = (
    "Northwind Telecom - eSIM setup\nWhat you need\n\n"
    "The QR code is valid for 7 days and can be used once."
)
_UNRELATED_TEXT: Final[str] = "A cable cut affecting a street is restored within 72 hours."
_OFF_TOPIC_SAME_DOMAIN_QUESTION: Final[str] = "How many employees does Northwind Telecom have?"
"""DEC-219's own example: shares the corpus's brand name with every document and no other
vocabulary with any of them, which is precisely what makes a lexical fake overrate it."""

_EMBED_TEXTS: Final[tuple[str, ...]] = (
    _GENUINE_QUESTION,
    _ANSWERING_TEXT,
    _UNRELATED_TEXT,
    _OFF_TOPIC_SAME_DOMAIN_QUESTION,
)


@dataclass(frozen=True)
class _SmokeConfig:
    """The three required variables, parsed once, plus the one optional one."""

    region: str
    generation_model_id: str
    embedding_model_id: str
    expected_embedding_dimensions: int | None


def _configured() -> _SmokeConfig | None:
    """The parsed configuration, or `None` when any required variable is unset.

    No import of `boto3` happens here and none is needed: an unset variable is "not opted in",
    decided from `os.environ` alone, so a machine with no AWS access and no intention of granting
    any never pays for a credential lookup it never asked for.
    """
    values = {var: os.environ.get(var, "").strip() for var in _REQUIRED_VARS}
    if not all(values.values()):
        return None
    raw_dimensions = os.environ.get(_EMBEDDING_DIMENSIONS_VAR, "").strip()
    return _SmokeConfig(
        region=values[_REGION_VAR],
        generation_model_id=values[_GENERATION_MODEL_VAR],
        embedding_model_id=values[_EMBEDDING_MODEL_VAR],
        expected_embedding_dimensions=int(raw_dimensions) if raw_dimensions else None,
    )


def _credentials_problem(region: str) -> str | None:
    """Why boto3 has nothing to authenticate with in `region`, or `None` when it does.

    Only ever called after `_configured()` has already returned something, i.e. after a person has
    opted in by exporting every `BEDROCK_SMOKE_*` variable - resolving credentials can read the
    filesystem, the environment or, as a last resort, the EC2 instance-metadata service, and none
    of that should run against a machine that gave no sign of wanting it to. The lookup itself makes
    no model call, so the very worst case is botocore's own connect timeout on a metadata endpoint
    nobody can reach - a few seconds, not a hang.
    """
    try:
        import boto3
    except ImportError:
        return "boto3 is not installed in this environment."
    try:
        credentials = boto3.Session(region_name=region).get_credentials()
    except Exception as exc:  # botocore's own hierarchy; anything raised here means "not usable"
        return f"boto3 could not resolve AWS credentials for {region!r}: {exc}"
    if credentials is None:
        return (
            "No AWS credentials found (checked environment variables, the shared config and "
            "credentials files, and an SSO or instance profile). Export AWS_ACCESS_KEY_ID and "
            "AWS_SECRET_ACCESS_KEY, or AWS_PROFILE, before running with -m bedrock."
        )
    return None


@pytest.fixture(scope="session")
def smoke_config() -> _SmokeConfig:
    """The parsed opt-in configuration, or a loud, immediate skip when it is incomplete."""
    config = _configured()
    if config is None:
        pytest.skip(
            "Bedrock smoke tests are opt-in and need "
            f"{_REGION_VAR}, {_GENERATION_MODEL_VAR} and {_EMBEDDING_MODEL_VAR} all set - "
            "see the 'Bedrock smoke tests' section of docs/AWS_DEPLOYMENT.md."
        )
    return config


@pytest.fixture(scope="session")
def client(smoke_config: _SmokeConfig) -> BedrockLLMClient:
    """One real `BedrockLLMClient`, shared by every test, or a loud skip when there are no
    credentials behind the configuration `smoke_config` already proved is present."""
    problem = _credentials_problem(smoke_config.region)
    if problem is not None:
        pytest.skip(problem)
    return BedrockLLMClient(
        region=smoke_config.region,
        model_id=smoke_config.generation_model_id,
        embedding_model_id=smoke_config.embedding_model_id,
        timeout_s=_TIMEOUT_S,
        max_retries=_MAX_RETRIES,
    )


@pytest.fixture(scope="session")
def sample_completion(client: BedrockLLMClient) -> LLMCompletion:
    """The one `complete` call this whole module makes; every completion test reads this."""
    return client.complete(_SHORT_PROMPT, max_tokens=_MAX_OUTPUT_TOKENS, temperature=0.0)


@pytest.fixture(scope="session")
def sample_embeddings(client: BedrockLLMClient) -> dict[str, tuple[float, ...]]:
    """The one `embed` call this whole module makes, keyed back by the text that produced each
    vector so a test can name what it is comparing rather than remember a position in a tuple.

    `model_id` is left unset: `client` was built with `embedding_model_id` already, and `embed`
    falls back to it on its own - naming the id again here would be a second place for the two to
    drift apart.
    """
    vectors = client.embed(_EMBED_TEXTS)
    return dict(zip(_EMBED_TEXTS, vectors, strict=True))


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------
def test_a_completion_comes_back_non_empty_and_reports_real_token_counts(
    sample_completion: LLMCompletion, smoke_config: _SmokeConfig
) -> None:
    """Proves the wire, not the parser - the fake tests already cover every branch of `_parse`
    logic `complete` has, against responses nobody sent. This is the one call in the suite that
    proves a real `Converse` response really does turn into the `LLMCompletion` the rest of the
    engine assumes: non-empty text, and token counts that are Bedrock's own count of a real prompt
    and a real answer, not the character-based estimate the fake and the fallback both use."""
    assert sample_completion.text.strip()
    assert sample_completion.model_id == smoke_config.generation_model_id
    assert sample_completion.input_tokens > 0
    assert sample_completion.output_tokens > 0


def test_an_invalid_model_id_raises_llmerror_not_a_raw_botocore_exception(
    client: BedrockLLMClient,
) -> None:
    """A caller three modules away from here handles `LLMError.code`, never a `botocore` exception
    class it never imported - `BedrockLLMClient.complete` promises that translation for every
    failure, and the one failure the fake cannot rehearse is what a *real* rejection from the
    service looks like on the wire. A model id nobody will ever register is the cheapest way to
    provoke one: AWS refuses it before any inference runs, so this costs nothing to run."""
    with pytest.raises(LLMError) as excinfo:
        client.complete("hi", model_id="not-a-real-bedrock-model-id", max_tokens=5)
    assert excinfo.value.code == "LLM_UNAVAILABLE"
    assert excinfo.value.model_id == "not-a-real-bedrock-model-id"


def test_token_counting_agrees_with_the_api_within_the_documented_approximation(
    client: BedrockLLMClient, sample_completion: LLMCompletion
) -> None:
    """`count_tokens` promises the provider's own count when the model offers the operation, and
    `APPROX_CHARS_PER_TOKEN` otherwise (`engine/llm.py`'s own docstring calls that a fallback, never
    a measurement). Both are checked in one assertion with one generous tolerance, because which of
    the two a given model takes is exactly the fact this test cannot know in advance without
    hard-coding a model id here - the one thing DEC-204 rules out. What must hold either way is
    agreement with what `complete` billed the very same prompt against, to within the approximation
    the docstring already owns up to."""
    counted = client.count_tokens(_SHORT_PROMPT)
    assert counted > 0
    tolerance = max(5, math.ceil(sample_completion.input_tokens * 0.5))
    assert (
        abs(counted - sample_completion.input_tokens) <= tolerance
    ), f"count_tokens()={counted} vs complete()'s own input_tokens={sample_completion.input_tokens}"


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
def test_an_embedding_comes_back_with_the_right_shape_and_every_value_is_finite(
    sample_embeddings: dict[str, tuple[float, ...]], smoke_config: _SmokeConfig
) -> None:
    """A vector retrieval will divide by, sum, and sort on has to be a vector of numbers a real
    machine can do arithmetic on - not `NaN`, not `inf`, and not a width that quietly changes
    between two calls to the model that is supposed to be the same one."""
    widths = {len(vector) for vector in sample_embeddings.values()}
    assert len(widths) == 1, f"one embedding model must not change width mid-batch: saw {widths}"
    (width,) = widths
    assert width > 0
    if smoke_config.expected_embedding_dimensions is not None:
        assert width == smoke_config.expected_embedding_dimensions
    for text, vector in sample_embeddings.items():
        assert all(math.isfinite(value) for value in vector), f"non-finite value in the vector for {text!r}"


def test_a_same_domain_off_topic_question_sits_below_the_genuine_one(
    sample_embeddings: dict[str, tuple[float, ...]],
) -> None:
    """The single most valuable assertion in this file, because it is the one property DEC-219
    says a lexical fake cannot demonstrate. The grounded `FakeLLMClient` hashes words into buckets, so a
    question that merely shares the corpus's vocabulary scores well under the fake regardless of
    whether it shares the corpus's *meaning* - DEC-219 measured "How many employees does Northwind
    Telecom have?" at 0.289 against the fake, ABOVE several genuine questions, purely because the
    brand name and the question words appear in every document. A real embedding model has no such
    blind spot: it separates "about eSIMs" from "shares the word Northwind" on what the words mean,
    not on whether they appear. So the off-topic question, despite sharing real vocabulary with
    `_ANSWERING_TEXT` (the brand name in its heading), must still embed further from it than the
    genuine question that is actually about what that text says."""
    genuine = cosine(sample_embeddings[_GENUINE_QUESTION], sample_embeddings[_ANSWERING_TEXT])
    off_topic = cosine(sample_embeddings[_OFF_TOPIC_SAME_DOMAIN_QUESTION], sample_embeddings[_ANSWERING_TEXT])
    assert (
        off_topic < genuine
    ), f"off-topic similarity {off_topic:.4f} did not sit below the genuine question's {genuine:.4f}"


def test_a_question_embeds_closer_to_its_own_answer_than_to_an_unrelated_passage(
    sample_embeddings: dict[str, tuple[float, ...]],
) -> None:
    """The ordinary retrieval property underneath every RAG test in the fast suite
    (`test_a_question_is_closest_to_the_chunk_that_answers_it`), proved here against a model that
    was never told the answer in advance the way a hash function effectively is."""
    related = cosine(sample_embeddings[_GENUINE_QUESTION], sample_embeddings[_ANSWERING_TEXT])
    unrelated = cosine(sample_embeddings[_GENUINE_QUESTION], sample_embeddings[_UNRELATED_TEXT])
    assert related > unrelated


def test_the_off_topic_question_would_be_refused_by_the_shipped_similarity_floor(
    sample_embeddings: dict[str, tuple[float, ...]],
) -> None:
    """DEC-219's other half: not just that the off-topic question ranks lower, but that it ranks
    low enough to actually trip `generative.rag.min_similarity` - the exact refusal path
    `assistant.answer` takes before ever calling a model - which is the behaviour DEC-218 calibrated
    that floor for and DEC-219 says only Bedrock, never the fake, can be trusted to demonstrate.
    This checks one chunk rather than the whole fourteen-document corpus a real `retrieve()` call
    would score, because a smoke test's job is to show the property holds, not to re-run the
    evaluation `rag_eval.json` already owns; the shipped default (`RagConfig().min_similarity`) is
    read rather than hard-coded so this cannot drift from the value the product actually ships."""
    off_topic = cosine(sample_embeddings[_OFF_TOPIC_SAME_DOMAIN_QUESTION], sample_embeddings[_ANSWERING_TEXT])
    assert off_topic < RagConfig().min_similarity
