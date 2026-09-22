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
import math
import struct
from collections.abc import Sequence
from typing import Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from engine.contracts import LLMUsage

__all__ = [
    "APPROX_CHARS_PER_TOKEN",
    "FAKE_MODEL_ID",
    "FakeLLMClient",
    "LLMCall",
    "LLMClient",
    "LLMCompletion",
    "LLMError",
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
        digest = self._digest(chosen, prompt, str(max_tokens), f"{temperature:.4f}")
        whole = f"[fake completion {digest[:16]}]"
        text = whole[: max_tokens * APPROX_CHARS_PER_TOKEN]
        input_tokens = self._tokens(prompt)
        output_tokens = self._tokens(text)
        self._calls.append(
            LLMCall(
                kind="complete",
                model_id=chosen,
                prompt=prompt,
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
