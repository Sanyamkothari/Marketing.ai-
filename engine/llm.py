"""The LLM client: one protocol, a deterministic fake and a Bedrock implementation.

Both implementations ship (DEC-203). `generative.llm.backend` defaults to `fake`, so a developer
with no AWS credentials can build an index, ask it questions, generate copy and watch the
guardrails and the budget work - the parts most likely to be wrong - without spending anything,
and the fast test suite never reaches the network.

The fake is not a stub that returns a fixed string. Two of its behaviours are deliberate and are
what make an offline test worth running:

**Its embeddings carry real lexical signal.** `FakeLLMClient.embed` hashes a text's tokens into a
fixed number of buckets and normalises the result, so two texts that share words really do sit
close together and two that share none really do not. Retrieval, the similarity floor and the MMR
de-duplication are therefore exercised for real rather than against noise, which a random or
hash-of-the-whole-string vector would not do.

**It answers from what it was given.** For a generating purpose the fake reads the input it was
handed - the numbered extracts, the evidence pack, the allowed placeholder list - and builds its
reply out of that, so a grounded answer is grounded, a citation points at a chunk that exists, and
an evidence reference names an id that is in the pack. A fake that invented its output would make
every grounding test pass by accident.

`FakeMode` then asks it to misbehave in one named way at a time, which is how the guardrails get
tested: `UNGROUNDED` cites something that is not there, `PII` puts an e-mail address in the answer,
`OVERLONG` blows the length limit, `BANNED` uses a forbidden phrase, and `MALFORMED` returns text
that is not the JSON the prompt asked for.

Nothing in this module knows what a use case is, and nothing in it writes an artefact: it returns
what a model said and what the call consumed, and `engine.generative.budget` decides what that
costs and whether it was allowed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, runtime_checkable

from engine.utils.logging import get_logger, log_failure

if TYPE_CHECKING:  # pragma: no cover - import cost only, and only for the real client
    from engine.config import LlmConfig

__all__ = [
    "BEDROCK_SERVICE",
    "DEFAULT_FAKE_DIMENSIONS",
    "TOKENS_PER_CHARACTER",
    "BedrockLLMClient",
    "Completion",
    "Embeddings",
    "FakeLLMClient",
    "FakeMode",
    "LLMClient",
    "LLMError",
    "TokenCount",
    "build_client",
]

_LOGGER = get_logger(__name__)

BEDROCK_SERVICE: Final[Literal["bedrock-runtime"]] = "bedrock-runtime"
"""The boto3 service name. A `Literal`, because boto3-stubs resolves the client type from it."""
DEFAULT_FAKE_DIMENSIONS: Final[int] = 256
"""Vector width the fake embeds into. Wide enough to keep unrelated texts apart, small enough to be free."""

TOKENS_PER_CHARACTER: Final[float] = 0.25
"""The fallback token estimate when a model cannot be asked: four characters to a token.

Recorded as an estimate wherever it is used (`TokenCount.estimated`), because a guessed count that
looked measured would quietly become a guessed cost (plan section 13.3).
"""

_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z0-9']+")
_SENTENCE: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s+")
_STOP_WORDS: Final[frozenset[str]] = frozenset(
    # Words that appear in every document and so carry no signal about which one to retrieve.
    # Short on purpose: a long stop list is a second vocabulary to keep in step with the corpus.
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "as",
        "not",
        "no",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
    ]
)


class LLMError(Exception):
    """A call to a model failed. `code` is one of the members of `LLM_ERRORS`."""

    def __init__(self, code: str, message: str, *, model_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.model_id = model_id


LLM_ERRORS: Final[dict[str, str]] = {
    "LLM_CALL_FAILED": "The model could not be reached. Check the region and the credentials.",
    "LLM_TIMED_OUT": "The model did not answer in time. Shorten the input or raise generative.llm.timeout_s.",
    "LLM_REFUSED": "The model refused to answer. The prompt may be tripping a provider guardrail.",
    "LLM_EMPTY_RESPONSE": "The model returned nothing to read.",
    "LLM_BACKEND_UNAVAILABLE": "The bedrock backend needs boto3 and credentials; `fake` needs neither.",
}
"""Failure code -> the suggestion a caller shows, in the shape `ENGINE_ERRORS` already uses."""


# ---------------------------------------------------------------------------
# What a call returns
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Completion:
    """What one generation call returned, and what it consumed.

    `text` is exactly what the model said: nothing is stripped, parsed or repaired here, because a
    caller that has to cope with a malformed answer needs to see the malformed answer.
    """

    text: str
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    stop_reason: str
    estimated_tokens: bool = False


@dataclass(frozen=True)
class Embeddings:
    """Vectors for a batch of texts, in the order the texts were given."""

    vectors: tuple[tuple[float, ...], ...]
    model_id: str
    input_tokens: int
    latency_ms: int
    estimated_tokens: bool = False

    @property
    def dimensions(self) -> int:
        """Width of one vector; 0 when the batch was empty."""
        return len(self.vectors[0]) if self.vectors else 0


@dataclass(frozen=True)
class TokenCount:
    """How many tokens a text is, and whether anybody actually counted."""

    tokens: int
    estimated: bool


@runtime_checkable
class LLMClient(Protocol):
    """Everything the generative engine needs from a model provider.

    Three calls, because three are all it makes: say something, embed something, and say how long
    something is. A client is stateless and safe to share; metering, caching and the budget live in
    `engine.generative.budget`, which wraps one of these rather than being one.
    """

    def complete(
        self,
        *,
        system: str,
        user: str,
        model_id: str,
        temperature: float,
        max_output_tokens: int,
    ) -> Completion: ...

    def embed(self, texts: Sequence[str], *, model_id: str) -> Embeddings: ...

    def count_tokens(self, text: str, *, model_id: str) -> TokenCount: ...


def estimate_tokens(text: str) -> TokenCount:
    """The chars-over-four fallback, always marked as the estimate it is."""
    return TokenCount(tokens=max(1, int(len(text) * TOKENS_PER_CHARACTER)), estimated=True)


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------
class FakeMode(StrEnum):
    """How `FakeLLMClient` should misbehave, so one guardrail at a time can be tested.

    `GROUNDED` is the default and is the only mode that should pass every check; each of the others
    breaks exactly one rule, so a test that sets one knows which failure it is asserting.
    """

    GROUNDED = "grounded"
    UNGROUNDED = "ungrounded"
    PII = "pii"
    OVERLONG = "overlong"
    BANNED = "banned"
    MALFORMED = "malformed"
    REFUSING = "refusing"
    FAILING_JUDGE = "failing_judge"


_FAKE_PII: Final[str] = "Write to priya.sharma@example.invalid or call +91 90000 12345."
"""An invented address and number, from ranges that resolve nowhere, for the PII guardrail to catch."""

_FAKE_BANNED: Final[str] = "This offer is guaranteed and it is your last chance."
"""Two phrases `configs/guardrails.yaml` bans globally, for the banned-phrase guardrail to catch."""

_FAKE_UNGROUNDED_REF: Final[str] = "not_in_the_pack"
"""An evidence id no pack ever contains, for the grounding parser to reject."""


def _tokenise(text: str) -> list[str]:
    """Lower-case words worth embedding, stop words dropped."""
    return [word for word in _WORD.findall(text.lower()) if word not in _STOP_WORDS and len(word) > 1]


class FakeLLMClient:
    """A deterministic `LLMClient` that reads its input and answers out of it.

    Every method is a pure function of its arguments and of `mode`: the same call always returns
    the same bytes, so an artefact written under the fake is byte-stable and a test can compare two
    runs. No clock is read beyond the latency it reports, which is always 0.
    """

    def __init__(
        self, *, mode: FakeMode = FakeMode.GROUNDED, dimensions: int = DEFAULT_FAKE_DIMENSIONS
    ) -> None:
        if dimensions < 8:
            raise ValueError(f"dimensions must be at least 8, got {dimensions}")
        self._mode = mode
        self._dimensions = dimensions

    @property
    def mode(self) -> FakeMode:
        """Which way this client is asked to misbehave."""
        return self._mode

    # -- embeddings ---------------------------------------------------------
    def embed(self, texts: Sequence[str], *, model_id: str) -> Embeddings:
        """Hash each text's words into `dimensions` buckets and L2-normalise the result.

        Shared words put two texts close together and different words keep them apart, which is the
        only property retrieval needs from an embedding and the only one a test can check without a
        provider. A text with no word at all gets a zero vector, whose cosine similarity to
        everything is 0 - which is what "nothing to match on" should mean.
        """
        vectors: list[tuple[float, ...]] = []
        characters = 0
        for text in texts:
            characters += len(text)
            buckets = [0.0] * self._dimensions
            for word, count in Counter(_tokenise(text)).items():
                digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self._dimensions
                # The sign bit spreads collisions in both directions, so two unrelated words that
                # land in one bucket are as likely to cancel as to reinforce.
                sign = 1.0 if digest[4] & 1 else -1.0
                # Log term frequency: a word said ten times is more evidence than a word said once,
                # but not ten times as much - which is what keeps a long chunk from drowning a
                # short one that is actually about the question.
                buckets[index] += sign * (1.0 + math.log(count))
            norm = math.sqrt(sum(value * value for value in buckets))
            vectors.append(tuple(value / norm for value in buckets) if norm else tuple(buckets))
        return Embeddings(
            vectors=tuple(vectors),
            model_id=model_id,
            input_tokens=max(1, int(characters * TOKENS_PER_CHARACTER)) if texts else 0,
            latency_ms=0,
            estimated_tokens=True,
        )

    # -- token counting -----------------------------------------------------
    def count_tokens(self, text: str, *, model_id: str) -> TokenCount:
        """The same estimate the real client falls back to, so a test measures the same thing."""
        del model_id
        return estimate_tokens(text)

    # -- generation ---------------------------------------------------------
    def complete(
        self,
        *,
        system: str,
        user: str,
        model_id: str,
        temperature: float,
        max_output_tokens: int,
    ) -> Completion:
        """Answer the prompt from the prompt, in whichever way `mode` asks for."""
        del temperature
        text = self._body(system, user, max_output_tokens)
        return Completion(
            text=text,
            model_id=model_id,
            input_tokens=max(1, int((len(system) + len(user)) * TOKENS_PER_CHARACTER)),
            output_tokens=max(1, int(len(text) * TOKENS_PER_CHARACTER)),
            latency_ms=0,
            stop_reason="end_turn",
            estimated_tokens=True,
        )

    def _body(self, system: str, user: str, max_output_tokens: int) -> str:
        if self._mode is FakeMode.MALFORMED:
            return "Certainly! Here is the answer you asked for, in prose rather than in JSON."
        shape = _prompt_shape(system, user)
        if shape == "judge":
            return self._judge()
        if shape == "copy":
            return self._copy(user, max_output_tokens)
        if shape == "evidence":
            return self._root_cause(user)
        return self._answer(user)

    def _judge(self) -> str:
        failing = self._mode is FakeMode.FAILING_JUDGE
        return json.dumps(
            {
                "score": 0.10 if failing else 1.0,
                "agrees": not failing,
                "supported": 0 if failing else 3,
                "unsupported": 3 if failing else 0,
                "unsupported_claims": ["the text says something its source does not"] if failing else [],
                "contradictions": [],
                "missing": [],
                "conflicting": [],
                "categories": [],
                "failures": [{"rule": "claims", "detail": "an invented figure"}] if failing else [],
                "reason": "a fake judge, returning the verdict the test asked for",
            }
        )

    def _answer(self, user: str) -> str:
        extracts = _numbered_extracts(user)
        if self._mode is FakeMode.REFUSING or not extracts:
            return json.dumps({"answer": _refusal_sentence(user), "refused": True, "citations": []})
        number, text = extracts[0]
        sentence = _first_sentence(text)
        answer = sentence
        if self._mode is FakeMode.PII:
            answer = f"{sentence} {_FAKE_PII}"
        elif self._mode is FakeMode.BANNED:
            answer = f"{sentence} {_FAKE_BANNED}"
        elif self._mode is FakeMode.OVERLONG:
            answer = " ".join([sentence] * 400)
        citations = [{"chunk": number, "quote": " ".join(sentence.split()[:25])}]
        if self._mode is FakeMode.UNGROUNDED:
            citations = [{"chunk": len(extracts) + 99, "quote": "a quote from an extract nobody supplied"}]
        return json.dumps({"answer": answer, "refused": False, "citations": citations})

    def _root_cause(self, user: str) -> str:
        ids = _evidence_ids(user)
        refs = [_FAKE_UNGROUNDED_REF] if self._mode is FakeMode.UNGROUNDED else ids[:2] or ["r1"]
        cause = "These rows share the strongest reason the model weighted, and the complaints echo it."
        if self._mode is FakeMode.PII:
            cause = f"{cause} {_FAKE_PII}"
        elif self._mode is FakeMode.OVERLONG:
            cause = " ".join([cause] * 400)
        return json.dumps(
            {
                "headline": "One reason dominates this segment and the complaints agree with it",
                "root_causes": [
                    {"cause": cause, "evidence_refs": refs, "confidence": "medium" if ids else "low"}
                ],
                "recommended_actions": ["Route these rows to the team that owns the strongest reason."],
                "caveats": ["The model found association, not cause, and this segment is not a trial."],
            }
        )

    def _copy(self, user: str, max_output_tokens: int) -> str:
        del max_output_tokens
        fields = _allowed_fields(user)
        labels = _variant_labels(user)
        opener = "{{" + fields[0] + "}}" if fields else "Hello"
        required = _required_line(user)
        variants: list[dict[str, str]] = []
        for index, label in enumerate(labels):
            body = f"Hi {opener}, we would be glad to have you back. Reply to this message to talk it over."
            if index:
                body = f"Hi {opener}, your line is ready whenever you are. Reply and we will pick it up."
            if self._mode is FakeMode.BANNED:
                body = f"{body} {_FAKE_BANNED}"
            elif self._mode is FakeMode.PII:
                body = f"{body} {_FAKE_PII}"
            elif self._mode is FakeMode.OVERLONG:
                body = " ".join([body] * 60)
            elif self._mode is FakeMode.UNGROUNDED:
                body = f"{body} Use code {{{{secret_offer_code}}}} before it expires."
            variants.append(
                {
                    "label": label,
                    "subject": "A word about your connection",
                    "text": f"{body}\n{required}",
                    "body": f"{body}\n{required}",
                }
            )
        return json.dumps({"variants": variants})


# ---------------------------------------------------------------------------
# Reading a rendered prompt back, so the fake can answer out of it
# ---------------------------------------------------------------------------
_EXTRACT: Final[re.Pattern[str]] = re.compile(
    r"^\[(\d+)\]\s*\(([^)]*)\)\s*\n(.*?)(?=\n\[\d+\]|\Z)", re.S | re.M
)
_EVIDENCE_ID: Final[re.Pattern[str]] = re.compile(r'"id"\s*:\s*"([^"]+)"')
_PLACEHOLDERS: Final[re.Pattern[str]] = re.compile(r"^Placeholders you may use:\s*(.+)$", re.M)
_LABELS: Final[re.Pattern[str]] = re.compile(r"labelled\s+([A-D](?:,\s*[A-D])*)", re.M)
_REQUIRED: Final[re.Pattern[str]] = re.compile(
    r"^Every message ends with the opt-out line[,:]?\s*(.+)$", re.M
)
_REFUSAL: Final[re.Pattern[str]] = re.compile(
    r"^and set refused to true:\s*\n(.+?)\s*\n\s*\nQuestion:", re.M | re.S
)


def _prompt_shape(system: str, user: str) -> str:
    """Which family of prompt this is, judged from the rendered text rather than from a label.

    The fake is given a system prompt and a user prompt, the same two strings the real client gets,
    so it decides the same way anybody reading them would. Keeping it to what is on the page means
    a new prompt in an existing family needs no change here.
    """
    if "TEXT TO CHECK" in user or "GENERATED TEXT" in user or "ANSWER TO MARK" in user:
        return "judge"
    if "TEMPLATE TO REVIEW" in user:
        return "judge"
    if "Placeholders you may use" in user:
        return "copy"
    if "Evidence pack for segment" in user:
        return "evidence"
    del system
    return "answer"


def _numbered_extracts(user: str) -> list[tuple[int, str]]:
    """The `[n] (document - section)` blocks the answering prompt renders, in order."""
    return [(int(number), body.strip()) for number, _meta, body in _EXTRACT.findall(user) if body.strip()]


def _evidence_ids(user: str) -> list[str]:
    """Every `"id"` in the evidence pack, in the order the pack lists them."""
    return _EVIDENCE_ID.findall(user)


def _allowed_fields(user: str) -> list[str]:
    """The placeholder names a copy prompt said were allowed."""
    match = _PLACEHOLDERS.search(user)
    return [field.strip() for field in match.group(1).split(",") if field.strip()] if match else []


def _variant_labels(user: str) -> list[str]:
    """The variant labels a copy prompt asked for; `["A"]` when it did not say."""
    match = _LABELS.search(user)
    return [label.strip() for label in match.group(1).split(",")] if match else ["A"]


def _required_line(user: str) -> str:
    """The opt-out line a channel demands, taken from the prompt that demanded it."""
    match = _REQUIRED.search(user)
    return match.group(1).strip() if match else "Reply STOP to opt out"


def _refusal_sentence(user: str) -> str:
    """The refusal the answering prompt supplied, so the fake refuses in the configured words."""
    match = _REFUSAL.search(user)
    return match.group(1).strip() if match else "I don't have that information."


def _first_sentence(text: str) -> str:
    """The first whole sentence of a chunk, which is what a grounded one-line answer is made of."""
    collapsed = " ".join(text.split())
    parts = _SENTENCE.split(collapsed)
    return parts[0].strip() if parts and parts[0].strip() else collapsed[:200]


# ---------------------------------------------------------------------------
# Bedrock
# ---------------------------------------------------------------------------
class BedrockLLMClient:
    """`LLMClient` over the Bedrock Converse and embedding APIs.

    One client per region, built lazily so importing this module needs neither boto3 credentials
    nor a network. Retries are boto3's own, configured from `generative.llm.max_retries`, because a
    retry loop written here would be a second, worse one.
    """

    def __init__(
        self,
        *,
        region: str,
        timeout_s: int = 60,
        max_retries: int = 2,
        client: Any | None = None,
    ) -> None:
        self._region = region
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._client = client

    @property
    def client(self) -> Any:
        """The boto3 `bedrock-runtime` client, built on first use."""
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:  # pragma: no cover - boto3 is a pinned dependency
                raise LLMError("LLM_BACKEND_UNAVAILABLE", LLM_ERRORS["LLM_BACKEND_UNAVAILABLE"]) from exc
            self._client = boto3.client(
                BEDROCK_SERVICE,
                region_name=self._region,
                config=Config(
                    read_timeout=self._timeout_s,
                    connect_timeout=min(self._timeout_s, 10),
                    retries={"max_attempts": self._max_retries + 1, "mode": "standard"},
                ),
            )
        return self._client

    def complete(
        self,
        *,
        system: str,
        user: str,
        model_id: str,
        temperature: float,
        max_output_tokens: int,
    ) -> Completion:
        """One Converse call. The model's text comes back unparsed and unrepaired."""
        started = time.monotonic()
        try:
            response = self.client.converse(
                modelId=model_id,
                system=[{"text": system}] if system.strip() else [],
                messages=[{"role": "user", "content": [{"text": user}]}],
                inferenceConfig={"temperature": temperature, "maxTokens": max_output_tokens},
            )
        except Exception as exc:  # boto3 raises a family of ClientErrors; they all mean "no answer"
            # `log_failure`, never `logger.exception`: a provider's error message routinely quotes
            # the prompt that upset it, and a prompt carries the customer's own documents
            # (plan section 13.7).
            log_failure(_LOGGER, "llm.converse", exc)
            raise LLMError("LLM_CALL_FAILED", LLM_ERRORS["LLM_CALL_FAILED"], model_id=model_id) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(str(block.get("text", "")) for block in blocks)
        if not text.strip():
            raise LLMError("LLM_EMPTY_RESPONSE", LLM_ERRORS["LLM_EMPTY_RESPONSE"], model_id=model_id)
        usage = response.get("usage", {})
        return Completion(
            text=text,
            model_id=model_id,
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            latency_ms=latency_ms,
            stop_reason=str(response.get("stopReason", "")),
            estimated_tokens=not usage,
        )

    def embed(self, texts: Sequence[str], *, model_id: str) -> Embeddings:
        """One `invoke_model` call per text: the embedding APIs take one input at a time."""
        started = time.monotonic()
        vectors: list[tuple[float, ...]] = []
        input_tokens = 0
        measured = True
        for text in texts:
            try:
                response = self.client.invoke_model(
                    modelId=model_id,
                    body=json.dumps({"inputText": text}),
                    contentType="application/json",
                    accept="application/json",
                )
                payload = json.loads(response["body"].read())
            except Exception as exc:
                log_failure(_LOGGER, "llm.embed", exc)
                raise LLMError("LLM_CALL_FAILED", LLM_ERRORS["LLM_CALL_FAILED"], model_id=model_id) from exc
            embedding = payload.get("embedding")
            if not embedding:
                raise LLMError("LLM_EMPTY_RESPONSE", LLM_ERRORS["LLM_EMPTY_RESPONSE"], model_id=model_id)
            vectors.append(tuple(float(value) for value in embedding))
            if "inputTextTokenCount" in payload:
                input_tokens += int(payload["inputTextTokenCount"])
            else:
                input_tokens += estimate_tokens(text).tokens
                measured = False
        return Embeddings(
            vectors=tuple(vectors),
            model_id=model_id,
            input_tokens=input_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
            estimated_tokens=not measured,
        )

    def count_tokens(self, text: str, *, model_id: str) -> TokenCount:
        """Ask the provider, and fall back to the estimate when this model cannot be asked.

        `count_tokens` is not offered for every model, and a version of boto3 without the operation
        raises rather than answering. Neither is worth failing a run over, so both fall through to
        the estimate - which says so.
        """
        try:
            response = self.client.count_tokens(
                modelId=model_id,
                input={"converse": {"messages": [{"role": "user", "content": [{"text": text}]}]}},
            )
        except Exception as exc:
            log_failure(_LOGGER, "llm.count_tokens", exc, level=logging.DEBUG)
            return estimate_tokens(text)
        return TokenCount(tokens=int(response.get("inputTokens", 0)), estimated=False)


def build_client(config: LlmConfig, *, fake_mode: FakeMode = FakeMode.GROUNDED) -> LLMClient:
    """The client `generative.llm.backend` names.

    `fake_mode` is read only when the backend is the fake, so a caller may pass one unconditionally
    without accidentally asking Bedrock to misbehave.
    """
    from engine.config import LlmBackend

    if config.backend is LlmBackend.FAKE:
        return FakeLLMClient(mode=fake_mode)
    return BedrockLLMClient(region=config.region, timeout_s=config.timeout_s, max_retries=config.max_retries)
