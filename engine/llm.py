"""The `LLMClient` protocol and the deterministic fake every test runs against.

Phase 1 has no LLM feature (plan section 1.3), and this module deliberately adds none. What it
adds is the seam: one protocol with three methods, so that when Phase 3a writes the RAG assistant,
the root-cause summaries and the win-back copy, the thing it writes them against already exists,
already has a local implementation, and is already the only way the rest of the engine can reach
a model.

Three properties matter more than the surface.

**The fake is deterministic.** `FakeLLMClient` derives every answer from a SHA-256 of the exact
request - model id, prompt, token ceiling, temperature - so the same call returns the same bytes
in the same order on any machine, and a test can assert on a completion without a network, a key
or a tolerance. It is not a simulation of a model and does not try to be: its text is visibly
machine-made, and `FakeCompletion.text` is not a plausible summary of anything.

**The fake records what it was asked.** Every call appends an :class:`LLMCall` to `calls`, so a
test can assert that a prompt was built once rather than per row, that a guardrail ran before the
model rather than after, or that a cache saved a round trip. Prompt text is recorded on the
object, never logged (protocol rule 7: prompts carry customer data).

**No fake output may reach a screen.** Plan section 13.3 forbids fabricated values in code paths
that reach the UI, and a fake completion is exactly that. The rule is not enforceable from here -
a caller could render one - so it is stated at the seam and belongs in the review of any Phase 3a
path that renders generated text: with `llm_backend=fake`, that path must be unreachable, not
merely unused.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import struct
from collections import Counter
from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from engine.contracts import LLMUsage
from engine.utils.logging import get_logger, log_failure

if TYPE_CHECKING:  # pragma: no cover - only the annotation needs it, and config imports contracts
    from engine.config import LlmConfig

_LOGGER = get_logger(__name__)

__all__ = [
    "APPROX_CHARS_PER_TOKEN",
    "BEDROCK_SERVICE",
    "FAKE_MODEL_ID",
    "GROUNDED_FAKE_MODEL_ID",
    "BedrockLLMClient",
    "FakeLLMClient",
    "GroundedFakeLLMClient",
    "GroundedFakeMode",
    "LLMCall",
    "LLMClient",
    "LLMCompletion",
    "LLMError",
    "estimate_tokens",
    "usage_from",
]

APPROX_CHARS_PER_TOKEN: Final[int] = 4
"""The fake's token ratio. A stand-in for a real tokeniser, and named so nobody mistakes it for one."""

FAKE_MODEL_ID: Final[str] = "fake-deterministic-v1"
"""The id `FakeLLMClient` reports when no model is named. Not a real model, and it reads like it."""

_EMBEDDING_DIMENSIONS: Final[int] = 16
"""Small on purpose: an embedding from a hash carries no meaning, so a large one would only mislead."""


class LLMError(Exception):
    """A completion or embedding failed.

    `code` is one of LLM_UNAVAILABLE | LLM_REFUSED | LLM_TOO_LONG | LLM_INVALID_REQUEST, so a
    caller can tell "try again" from "this prompt will never work" without parsing a message.
    """

    def __init__(self, code: str, message: str, *, model_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.model_id = model_id


class LLMCompletion(BaseModel):
    """One model answer, with what it cost.

    Token counts are what the provider reported, not an estimate, and `cost_estimate_usd` is
    `None` when nothing was billed - the same rule `CostEstimate.estimated_usd` follows, for the
    same reason: a fabricated zero is indistinguishable from a measurement of free compute.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(description="What the model returned, verbatim.")
    model_id: str = Field(description="Model that produced it.")
    input_tokens: int = Field(description="Tokens the provider counted in the prompt.")
    output_tokens: int = Field(description="Tokens the provider counted in the answer.")
    cost_estimate_usd: float | None = Field(
        default=None, description="Billed cost when the provider reports one; null when nothing was billed."
    )
    stop_reason: str = Field(description="Why generation stopped, in the provider's own vocabulary.")


class LLMCall(BaseModel):
    """One recorded request, for tests and for the usage totals a run manifest carries.

    `prompt` is kept so a test can assert on what was sent. It is never logged: a prompt built
    from a customer's rows is customer data (protocol rule 7).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(description="Which method was called: complete, embed or count_tokens.")
    model_id: str = Field(description="Model the call named, or the client's default.")
    prompt: str = Field(default="", description="Prompt as sent; empty for embed and count_tokens.")
    system: str = Field(default="", description="System prompt as sent, when the caller set one.")
    texts: tuple[str, ...] = Field(default=(), description="Inputs of an embed call, in order.")
    max_tokens: int | None = Field(default=None, description="Ceiling the caller set, when it set one.")
    temperature: float | None = Field(default=None, description="Sampling temperature the caller asked for.")
    input_tokens: int = Field(default=0, description="Tokens counted in the request.")
    output_tokens: int = Field(default=0, description="Tokens counted in the answer.")


@runtime_checkable
class LLMClient(Protocol):
    """Everything the engine needs from a language model.

    Three methods and no more: generate, embed, measure. Anything richer - streaming, tool use,
    system prompts as a separate argument - would be a provider's shape leaking into the engine,
    and the two implementations this protocol has to cover (a hash and Bedrock) agree on nothing
    beyond these three.
    """

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion: ...

    def embed(
        self, texts: Sequence[str], *, model_id: str | None = None
    ) -> tuple[tuple[float, ...], ...]: ...

    def count_tokens(self, text: str, *, model_id: str | None = None) -> int: ...


class FakeLLMClient:
    """A deterministic `LLMClient` that reaches nothing and records everything.

    Every answer is a function of the request alone, so two processes on two machines produce the
    same bytes. `calls` is the log, in call order; `reset()` empties it between test cases.
    """

    def __init__(self, model_id: str = FAKE_MODEL_ID) -> None:
        self._model_id = model_id
        self._calls: list[LLMCall] = []

    @property
    def model_id(self) -> str:
        """The model id this client reports when a call names none."""
        return self._model_id

    @property
    def calls(self) -> tuple[LLMCall, ...]:
        """Every call made so far, oldest first."""
        return tuple(self._calls)

    def reset(self) -> None:
        """Forget every recorded call; the answers themselves do not depend on history."""
        self._calls.clear()

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        """A completion derived from a digest of the request; the same request always returns it.

        The text names itself as fake and carries the digest, so a fake answer that escapes into a
        screen or a file is recognisable on sight rather than mistaken for a model's work.
        """
        if max_tokens < 1:
            raise LLMError("LLM_INVALID_REQUEST", "max_tokens must be at least 1.", model_id=model_id)
        chosen = model_id or self._model_id
        digest = self._digest(chosen, system, prompt, str(max_tokens), f"{temperature:.4f}")
        whole = f"[fake completion {digest[:16]}]"
        text = whole[: max_tokens * APPROX_CHARS_PER_TOKEN]
        input_tokens = self._tokens(system) + self._tokens(prompt)
        output_tokens = self._tokens(text)
        self._calls.append(
            LLMCall(
                kind="complete",
                model_id=chosen,
                prompt=prompt,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
        return LLMCompletion(
            text=text,
            model_id=chosen,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate_usd=None,
            # The fake reports the reason it really stopped, not a flattering constant: a caller
            # that retries on a truncated answer has to be able to tell that this one was cut off.
            stop_reason="end_turn" if text == whole else "max_tokens",
        )

    def embed(self, texts: Sequence[str], *, model_id: str | None = None) -> tuple[tuple[float, ...], ...]:
        """One unit-length vector per text, derived from its digest; identical texts embed alike.

        The vectors carry no meaning - a hash has no semantics - so a test may assert that two
        identical strings embed identically and that two different ones do not, and nothing else.
        """
        chosen = model_id or self._model_id
        vectors = tuple(self._vector(chosen, text) for text in texts)
        self._calls.append(
            LLMCall(
                kind="embed",
                model_id=chosen,
                texts=tuple(texts),
                input_tokens=sum(self._tokens(text) for text in texts),
            )
        )
        return vectors

    def count_tokens(self, text: str, *, model_id: str | None = None) -> int:
        """`APPROX_CHARS_PER_TOKEN` characters per token, rounded up. An approximation, not a tokeniser."""
        chosen = model_id or self._model_id
        counted = self._tokens(text)
        self._calls.append(LLMCall(kind="count_tokens", model_id=chosen, input_tokens=counted))
        return counted

    @staticmethod
    def _tokens(text: str) -> int:
        return -(-len(text) // APPROX_CHARS_PER_TOKEN)

    @staticmethod
    def _digest(*parts: str) -> str:
        return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()

    def _vector(self, model_id: str, text: str) -> tuple[float, ...]:
        """A unit vector from the digest: four bytes per dimension, mapped into [-1, 1], normalised."""
        raw = hashlib.sha256(f"{model_id}\x00{text}".encode()).digest()
        while len(raw) < _EMBEDDING_DIMENSIONS * 4:
            raw += hashlib.sha256(raw).digest()
        words = struct.unpack(f">{_EMBEDDING_DIMENSIONS}I", raw[: _EMBEDDING_DIMENSIONS * 4])
        centred = [word / 0x7FFFFFFF - 1.0 for word in words]
        norm = math.sqrt(math.fsum(value * value for value in centred))
        if norm == 0.0:  # unreachable for SHA-256 output; a zero vector would break cosine distance
            return tuple(0.0 for _ in centred)
        return tuple(value / norm for value in centred)


def estimate_tokens(text: str) -> int:
    """`APPROX_CHARS_PER_TOKEN` characters to a token, rounded up.

    The one definition of "how long is this in tokens" that does not need a client: the chunker
    sizes a chunk with it, the meter counts an embedding batch with it, and both fakes answer
    `count_tokens` with it, so three parts of the system that have to agree cannot drift. It is an
    approximation and every caller that stores its result says so.
    """
    return -(-len(text) // APPROX_CHARS_PER_TOKEN)


def usage_from(calls: Sequence[LLMCall], *, cost_estimate_usd: float | None = None) -> LLMUsage:
    """Total `calls` into the `LLMUsage` a run manifest carries.

    Only `complete` and `embed` calls are counted: `count_tokens` measures a prompt, it does not
    send one, and counting it would report round trips that never happened. `cost_estimate_usd`
    stays `None` unless the caller has a billed figure, for the reason `CostEstimate` gives.
    """
    billable = [call for call in calls if call.kind in {"complete", "embed"}]
    return LLMUsage(
        calls=len(billable),
        input_tokens=sum(call.input_tokens for call in billable),
        output_tokens=sum(call.output_tokens for call in billable),
        cost_estimate_usd=cost_estimate_usd,
        model_ids=tuple(sorted({call.model_id for call in billable})),
    )


# ===========================================================================
# Phase 3a implementations. `engine/llm.py` is Phase 3a's outright
# (PARALLEL_WORK_PROTOCOL.md §3), so these sit below the shared seam rather
# than inside a marker block; nothing above this line changed except the
# optional `system` argument, which every existing caller may ignore.
# ===========================================================================
GROUNDED_FAKE_MODEL_ID: Final[str] = "fake-grounded-v1"
"""The id `GroundedFakeLLMClient` reports. Also not a real model, and it reads like one too."""

BEDROCK_SERVICE: Final[Literal["bedrock-runtime"]] = "bedrock-runtime"
"""The boto3 service name. A `Literal`, because boto3-stubs resolves the client type from it."""

_GROUNDED_DIMENSIONS: Final[int] = 1024
"""Wide enough to keep unrelated texts apart, narrow enough that a vector costs nothing.

Every word is hashed into one of these buckets, so the width is really a collision budget: at 256,
enough unrelated words share a bucket that two chunks look alike for reasons neither of them is
about. Measured over the reference set, widening to 1024 retrieves the right document for 40 of the
45 answerable questions where 256 managed 34, and 4096 gains nothing further - the collisions that
mattered are already gone. It is also the width of a real embedding model rather than a toy one,
which keeps the shape of an index the same whichever backend built it.
"""

_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z0-9']+")
_SENTENCE: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s+")
_STOP_WORDS: Final[frozenset[str]] = frozenset(
    # Words in every document, which therefore say nothing about which one to retrieve. Short on
    # purpose: a long stop list is a second vocabulary to keep in step with the corpus.
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


class GroundedFakeMode(StrEnum):
    """How `GroundedFakeLLMClient` should misbehave, so one guardrail at a time can be tested.

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


class GroundedFakeLLMClient:
    """A second fake, for the one thing `FakeLLMClient` deliberately cannot do: be retrieved from.

    `FakeLLMClient` derives everything from a digest, which is exactly right for the seam it was
    built for - it is visibly machine-made, it reaches nothing, and a test can assert on its bytes.
    It is also, by the same design, unusable for testing retrieval: a hash embedding has no
    semantics, so the chunk that answers a question sits no closer to it than any other, and a RAG
    test against one would be asserting that a random number beat another random number. Nor can a
    digest be *grounded*: `[fake completion 3f2a…]` quotes no extract and cites no chunk, so every
    grounding check would pass for the wrong reason (DEC-214).

    So this client, and only this one, reads its input and answers out of it:

    **Embeddings carry lexical signal.** Each text's words are hashed into `_GROUNDED_DIMENSIONS`
    buckets with a log term frequency and the result is normalised, so two texts that share
    vocabulary really do sit close together. Retrieval, the similarity floor and MMR are therefore
    exercised for real.

    **Completions come out of the prompt.** For a generating call it reads the numbered extracts,
    the evidence pack or the allowed placeholder list it was handed and builds its reply from that,
    so a grounded answer is grounded, a citation names an extract that exists, and an evidence
    reference names an id that is in the pack.

    `GroundedFakeMode` then asks it to break exactly one rule at a time, which is how the guardrails
    are tested. Plan §13.3 still applies in full: with a fake backend, no path that renders
    generated text may be reachable.
    """

    def __init__(
        self,
        *,
        mode: GroundedFakeMode = GroundedFakeMode.GROUNDED,
        model_id: str = GROUNDED_FAKE_MODEL_ID,
        dimensions: int = _GROUNDED_DIMENSIONS,
    ) -> None:
        if dimensions < 8:
            raise ValueError(f"dimensions must be at least 8, got {dimensions}")
        self._mode = mode
        self._model_id = model_id
        self._dimensions = dimensions
        self._calls: list[LLMCall] = []

    @property
    def mode(self) -> GroundedFakeMode:
        """Which way this client is asked to misbehave."""
        return self._mode

    @property
    def model_id(self) -> str:
        """The model id this client reports when a call names none."""
        return self._model_id

    @property
    def calls(self) -> tuple[LLMCall, ...]:
        """Every call made so far, oldest first - the same log `FakeLLMClient` keeps."""
        return tuple(self._calls)

    def reset(self) -> None:
        """Forget every recorded call; the answers themselves do not depend on history."""
        self._calls.clear()

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        """Answer the prompt from the prompt, in whichever way `mode` asks for."""
        if max_tokens < 1:
            raise LLMError("LLM_INVALID_REQUEST", "max_tokens must be at least 1.", model_id=model_id)
        chosen = model_id or self._model_id
        text = self._body(system, prompt)
        input_tokens = self._tokens(system) + self._tokens(prompt)
        output_tokens = self._tokens(text)
        self._calls.append(
            LLMCall(
                kind="complete",
                model_id=chosen,
                prompt=prompt,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
        return LLMCompletion(
            text=text,
            model_id=chosen,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate_usd=None,
            stop_reason="end_turn",
        )

    def embed(self, texts: Sequence[str], *, model_id: str | None = None) -> tuple[tuple[float, ...], ...]:
        """Hash each text's words into buckets and normalise, so shared vocabulary means proximity.

        A text with no content word in it gets the zero vector, whose cosine similarity to
        everything is 0 - which is what "nothing to match on" should mean, and is the one case
        where this client returns something that is not unit length.
        """
        chosen = model_id or self._model_id
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            buckets = [0.0] * self._dimensions
            for word, count in Counter(_tokenise(text)).items():
                digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self._dimensions
                # The sign bit spreads collisions in both directions, so two unrelated words that
                # land in one bucket are as likely to cancel as to reinforce.
                sign = 1.0 if digest[4] & 1 else -1.0
                # Log term frequency: a word said ten times is more evidence than a word said once,
                # but not ten times as much - which keeps a long chunk from drowning a short one
                # that is actually about the question.
                buckets[index] += sign * (1.0 + math.log(count))
            norm = math.sqrt(math.fsum(value * value for value in buckets))
            vectors.append(tuple(value / norm for value in buckets) if norm else tuple(buckets))
        self._calls.append(
            LLMCall(
                kind="embed",
                model_id=chosen,
                texts=tuple(texts),
                input_tokens=sum(self._tokens(text) for text in texts),
            )
        )
        return tuple(vectors)

    def count_tokens(self, text: str, *, model_id: str | None = None) -> int:
        """The same approximation `FakeLLMClient` uses, so both fakes measure the same thing."""
        chosen = model_id or self._model_id
        counted = self._tokens(text)
        self._calls.append(LLMCall(kind="count_tokens", model_id=chosen, input_tokens=counted))
        return counted

    @staticmethod
    def _tokens(text: str) -> int:
        return -(-len(text) // APPROX_CHARS_PER_TOKEN)

    # -- reading the prompt back, so the answer can come out of it ----------
    def _body(self, system: str, user: str) -> str:
        if self._mode is GroundedFakeMode.MALFORMED:
            return "Certainly! Here is the answer you asked for, in prose rather than in JSON."
        shape = _prompt_shape(system, user)
        if shape == "judge":
            return self._judge()
        if shape == "copy":
            return self._copy(user)
        if shape == "evidence":
            return self._root_cause(user)
        return self._answer(user)

    def _judge(self) -> str:
        failing = self._mode is GroundedFakeMode.FAILING_JUDGE
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
        if self._mode is GroundedFakeMode.REFUSING or not extracts:
            return json.dumps({"answer": _refusal_sentence(user), "refused": True, "citations": []})
        number, text = extracts[0]
        sentence = _first_sentence(text)
        answer = sentence
        if self._mode is GroundedFakeMode.PII:
            answer = f"{sentence} {_FAKE_PII}"
        elif self._mode is GroundedFakeMode.BANNED:
            answer = f"{sentence} {_FAKE_BANNED}"
        elif self._mode is GroundedFakeMode.OVERLONG:
            answer = " ".join([sentence] * 400)
        citations = [{"chunk": number, "quote": " ".join(sentence.split()[:25])}]
        if self._mode is GroundedFakeMode.UNGROUNDED:
            citations = [{"chunk": len(extracts) + 99, "quote": "a quote from an extract nobody supplied"}]
        return json.dumps({"answer": answer, "refused": False, "citations": citations})

    def _root_cause(self, user: str) -> str:
        ids = _evidence_ids(user)
        refs = [_FAKE_UNGROUNDED_REF] if self._mode is GroundedFakeMode.UNGROUNDED else ids[:2] or ["r1"]
        cause = "These rows share the strongest reason the model weighted, and the complaints echo it."
        if self._mode is GroundedFakeMode.PII:
            cause = f"{cause} {_FAKE_PII}"
        elif self._mode is GroundedFakeMode.OVERLONG:
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

    def _copy(self, user: str) -> str:
        fields = _allowed_fields(user)
        labels = _variant_labels(user)
        opener = "{{" + fields[0] + "}}" if fields else "there"
        required = _required_line(user)
        variants: list[dict[str, str]] = []
        for index, label in enumerate(labels):
            body = f"Hi {opener}, we would be glad to have you back. Reply to this message to talk it over."
            if index:
                body = f"Hi {opener}, your line is ready whenever you are. Reply and we will pick it up."
            if self._mode is GroundedFakeMode.BANNED:
                body = f"{body} {_FAKE_BANNED}"
            elif self._mode is GroundedFakeMode.PII:
                body = f"{body} {_FAKE_PII}"
            elif self._mode is GroundedFakeMode.OVERLONG:
                body = " ".join([body] * 60)
            elif self._mode is GroundedFakeMode.UNGROUNDED:
                body = f"{body} Use code {{{{secret_offer_code}}}} before it expires."
            whole = f"{body}\n{required}"
            variants.append(
                {"label": label, "subject": "A word about your connection", "text": whole, "body": whole}
            )
        return json.dumps({"variants": variants})


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

    The fake is given the same two strings the real client gets, so it decides the same way anybody
    reading them would. Keeping it to what is on the page means a new prompt in an existing family
    needs no change here.
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


class BedrockLLMClient:
    """`LLMClient` over the Bedrock Converse and embedding APIs.

    One client per region, built lazily: `create_app()` runs at documentation-generation time with
    no credentials and no network, so importing this module - and constructing this class - must do
    nothing but remember its arguments. boto3 is not imported until the first call.

    Retries are boto3's own, configured from `generative.llm.max_retries`, because a retry loop
    written here would be a second and worse one. A failure is an `LLMError` whose message names no
    value: a provider's own message routinely quotes the prompt that upset it, and a prompt is
    built from the customer's documents (plan section 13.7).
    """

    def __init__(
        self,
        *,
        region: str,
        model_id: str = "",
        embedding_model_id: str = "",
        timeout_s: int = 60,
        max_retries: int = 2,
        client: Any | None = None,
    ) -> None:
        self._region = region
        self._model_id = model_id
        self._embedding_model_id = embedding_model_id
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._client = client

    @property
    def model_id(self) -> str:
        """The model id this client reports when a call names none."""
        return self._model_id

    @property
    def client(self) -> Any:
        """The boto3 `bedrock-runtime` client, built on first use."""
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:  # pragma: no cover - boto3 is a pinned dependency
                raise LLMError("LLM_UNAVAILABLE", "boto3 is not installed.") from exc
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
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        """One Converse call. The model's text comes back unparsed and unrepaired.

        A caller that has to cope with a malformed answer needs to see the malformed answer, so
        nothing is stripped or fixed up here.
        """
        if max_tokens < 1:
            raise LLMError("LLM_INVALID_REQUEST", "max_tokens must be at least 1.", model_id=model_id)
        chosen = model_id or self._model_id
        try:
            response = self.client.converse(
                modelId=chosen,
                system=[{"text": system}] if system.strip() else [],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"temperature": temperature, "maxTokens": max_tokens},
            )
        except Exception as exc:  # every boto3 ClientError means the same thing here: no answer
            # `log_failure`, never `logger.exception`: the provider's message quotes the prompt.
            log_failure(_LOGGER, "llm.converse", exc)
            raise LLMError("LLM_UNAVAILABLE", "The model could not be reached.", model_id=chosen) from exc
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(str(block.get("text", "")) for block in blocks)
        if not text.strip():
            raise LLMError("LLM_REFUSED", "The model returned nothing to read.", model_id=chosen)
        usage = response.get("usage", {})
        return LLMCompletion(
            text=text,
            model_id=chosen,
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            cost_estimate_usd=None,
            stop_reason=str(response.get("stopReason", "")),
        )

    def embed(self, texts: Sequence[str], *, model_id: str | None = None) -> tuple[tuple[float, ...], ...]:
        """One `invoke_model` call per text: the embedding APIs take one input at a time."""
        chosen = model_id or self._embedding_model_id or self._model_id
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            try:
                response = self.client.invoke_model(
                    modelId=chosen,
                    body=json.dumps({"inputText": text}),
                    contentType="application/json",
                    accept="application/json",
                )
                payload = json.loads(response["body"].read())
            except Exception as exc:
                log_failure(_LOGGER, "llm.embed", exc)
                raise LLMError(
                    "LLM_UNAVAILABLE", "The embedding model could not be reached.", model_id=chosen
                ) from exc
            embedding = payload.get("embedding")
            if not embedding:
                raise LLMError("LLM_REFUSED", "The model returned no vector.", model_id=chosen)
            vectors.append(tuple(float(value) for value in embedding))
        return tuple(vectors)

    def count_tokens(self, text: str, *, model_id: str | None = None) -> int:
        """Ask the provider, and fall back to the approximation when this model cannot be asked.

        `count_tokens` is not offered for every model, and a boto3 without the operation raises
        rather than answering. Neither is worth failing a run over, and the fallback is the same
        `APPROX_CHARS_PER_TOKEN` ratio both fakes use, so a caller that compares them compares
        like with like.
        """
        chosen = model_id or self._model_id
        try:
            response = self.client.count_tokens(
                modelId=chosen,
                input={"converse": {"messages": [{"role": "user", "content": [{"text": text}]}]}},
            )
        except Exception as exc:
            log_failure(_LOGGER, "llm.count_tokens", exc, level=logging.DEBUG)
            return -(-len(text) // APPROX_CHARS_PER_TOKEN)
        return int(response.get("inputTokens", 0))


def build_client(config: LlmConfig, *, fake_mode: GroundedFakeMode = GroundedFakeMode.GROUNDED) -> LLMClient:
    """The client `generative.llm.backend` names.

    The fake backend gets `GroundedFakeLLMClient` rather than `FakeLLMClient`: a generative flow
    retrieves, cites and grounds, and a digest can do none of those (DEC-214). `fake_mode` is read
    only when the backend is the fake, so a caller may pass one unconditionally without accidentally
    asking Bedrock to misbehave.
    """
    from engine.config import LlmBackend

    if config.backend is LlmBackend.FAKE:
        return GroundedFakeLLMClient(mode=fake_mode)
    return BedrockLLMClient(
        region=config.region,
        model_id=config.generation_model,
        embedding_model_id=config.embedding_model,
        timeout_s=config.timeout_s,
        max_retries=config.max_retries,
    )
