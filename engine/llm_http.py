"""`LLMClient`s over HTTP: any OpenAI-compatible service, and Anthropic's Messages API (DEC-1141).

Two classes, one job each, so a person can connect the service they already pay for: OpenAI itself,
OpenRouter, Hugging Face's router, Ollama, vLLM, LiteLLM, Together, Groq or Azure's compatible
gateway speak the OpenAI chat protocol (`OpenAICompatibleClient`); Claude speaks its own
(`AnthropicClient`). Amazon Bedrock stays `engine.llm.BedrockLLMClient`.

Four rules are enforced here rather than hoped for in the caller.

1. **The key travels in one header and nowhere else.** `Authorization: Bearer <key>` (or `x-api-key`)
   is built per request, never stored on an exception, never logged. A client with no key sends no
   header at all (a local server that needs none).
2. **A provider's words never reach a person or a log.** A provider's error body routinely quotes the
   prompt that upset it, and a prompt is built from a customer's rows. Every failure becomes an
   :class:`LLMHttpError` whose message is one of the sentences in :data:`PROBLEMS` - chosen by the
   status class, not by anything the server said. The body is read only to spot which request
   parameter the server refused (`temperature`, `max_tokens`), and only that fact is kept.
3. **No redirect is ever followed.** A redirect could carry the bearer token to another host; a 3xx is
   a failure that says the address is wrong.
4. **Retries are bounded and honest.** `max_retries` more attempts for a timeout, a refused connection,
   429 and 5xx, with a short back-off (a `Retry-After` is honoured up to ten seconds); 4xx other than
   429 is never retried, because asking again does not change the answer.

The address itself is checked before a client is built (`engine.ai_service.check_base_url`): this
module talks to whatever address it is given.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from typing import Any, Final, Literal

import httpx

from engine.llm import LLMCompletion, LLMError, estimate_tokens
from engine.utils.logging import get_logger, log_failure

__all__ = [
    "ANTHROPIC_VERSION",
    "MAX_MODELS",
    "PROBLEMS",
    "AnthropicClient",
    "LLMHttpError",
    "OpenAICompatibleClient",
    "Problem",
]

_LOGGER = get_logger(__name__)

ANTHROPIC_VERSION: Final[str] = "2023-06-01"
MAX_MODELS: Final[int] = 200
"""The most model ids a listing returns; a router such as OpenRouter lists hundreds."""
_MAX_RESPONSE_BYTES: Final[int] = 16 * 1024 * 1024
_MAX_RETRY_AFTER_S: Final[float] = 10.0
_EMBED_BATCH: Final[int] = 64

Problem = Literal[
    "key_rejected",
    "rate_limited",
    "server_error",
    "unreachable",
    "timeout",
    "tls",
    "not_found",
    "bad_request",
    "too_long",
    "bad_response",
    "redirect",
    "no_embeddings",
]

PROBLEMS: Final[dict[str, tuple[str, str]]] = {
    "key_rejected": (
        "The AI service rejected the key.",
        "Check that the key is active and belongs to this service, then enter it again.",
    ),
    "rate_limited": (
        "The AI service says there were too many requests.",
        "Wait a minute and try again, or check the limits of your plan with the service.",
    ),
    "server_error": (
        "The AI service had a problem on its side.",
        "Try again in a few minutes; check the service's status page if it keeps happening.",
    ),
    "unreachable": (
        "Marketing AI could not reach the AI service.",
        "Check the web address and that the service is running and reachable from this computer.",
    ),
    "timeout": (
        "The AI service did not answer in time.",
        "Try again; if it keeps happening, check the service's status or use a smaller model.",
    ),
    "tls": (
        "Marketing AI could not make a secure connection to the AI service.",
        "Check the web address; a private service needs a certificate this computer trusts.",
    ),
    "not_found": (
        "The AI service did not find the model or the address.",
        "Check the model name (use 'Load models from this service') and the web address.",
    ),
    "bad_request": (
        "The AI service refused the request.",
        "Check the model name; some models are not made for chat or need other settings.",
    ),
    "too_long": (
        "The request was too long for this model.",
        "Choose a model with a longer context, or use a smaller file.",
    ),
    "bad_response": (
        "The AI service answered in a form Marketing AI cannot read.",
        "Check the web address: it should be the service's OpenAI-compatible address, usually ending in /v1.",
    ),
    "redirect": (
        "The web address sends visitors somewhere else.",
        "Use the service's final web address; Marketing AI does not follow redirects.",
    ),
    "no_embeddings": (
        "This AI service has no embeddings.",
        "Leave the embedding model empty; the document assistant will match by keywords.",
    ),
}
"""Problem kind to (what happened, what to do). The only text an HTTP failure ever carries."""

_CODES: Final[dict[str, str]] = {
    "too_long": "LLM_TOO_LONG",
    "bad_request": "LLM_INVALID_REQUEST",
    "not_found": "LLM_INVALID_REQUEST",
    "no_embeddings": "LLM_INVALID_REQUEST",
}
"""Everything else is LLM_UNAVAILABLE ("try again or fix the connection")."""

_REJECTABLE: Final[tuple[str, ...]] = ("temperature", "max_completion_tokens", "max_tokens")


class LLMHttpError(LLMError):
    """An HTTP call to an AI service failed. `message` is fixed text; `fix` says what to do.

    `kind` says why (see :data:`PROBLEMS`); `status` is the HTTP status when there was one;
    `rejected` names the request parameters a 400 said it would not take (names only), which is how a
    client adapts once to a model with different rules.
    """

    def __init__(
        self,
        kind: Problem,
        *,
        status: int | None = None,
        model_id: str | None = None,
        rejected: tuple[str, ...] = (),
    ) -> None:
        message, fix = PROBLEMS[kind]
        super().__init__(_CODES.get(kind, "LLM_UNAVAILABLE"), message, model_id=model_id)
        self.kind: Problem = kind
        self.fix = fix
        self.status = status
        self.rejected = rejected
        self.retry_after: float | None = None


class _HttpClient:
    """What both protocols share: a bounded, redirect-free, retrying JSON round trip."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model_id: str = "",
        embedding_model_id: str = "",
        timeout_s: int = 60,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        backoff_s: float = 0.5,
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._api_key = api_key
        self._model_id = model_id
        self._embedding_model_id = embedding_model_id
        self._timeout_s = timeout_s
        self._max_retries = max(0, max_retries)
        self._transport = transport
        self._backoff_s = backoff_s

    @property
    def model_id(self) -> str:
        """The model id this client reports when a call names none."""
        return self._model_id

    def __repr__(self) -> str:  # a stray repr() must never show a key
        return f"{type(self).__name__}(base_url={self._base_url!r}, model_id={self._model_id!r})"

    # -- to be filled in by a protocol ---------------------------------------
    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    # -- the round trip ------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        model_id: str | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                return self._send(method, path, body, model_id=model_id, params=params)
            except LLMHttpError as exc:
                retryable = exc.kind in {"rate_limited", "server_error", "timeout", "unreachable"}
                if not retryable or attempt + 1 >= attempts:
                    raise
                time.sleep(self._delay(attempt, exc))
        raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises

    def _delay(self, attempt: int, exc: LLMHttpError) -> float:
        if exc.retry_after is not None:
            return min(max(exc.retry_after, 0.0), _MAX_RETRY_AFTER_S)
        return min(self._backoff_s * (2.0**attempt), _MAX_RETRY_AFTER_S)

    def _send(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        model_id: str | None,
        params: dict[str, str] | None,
    ) -> Any:
        timeout = httpx.Timeout(self._timeout_s, connect=min(self._timeout_s, 10))
        try:
            with (
                httpx.Client(timeout=timeout, follow_redirects=False, transport=self._transport) as http,
                http.stream(
                    method, self._url(path), headers=self._headers(), json=body, params=params
                ) as response,
            ):
                status = response.status_code
                raw = self._read(response)
                retry_after = _retry_after(response.headers.get("retry-after"))
        except httpx.TimeoutException as exc:
            log_failure(_LOGGER, "llm_http.request", exc, level=logging.DEBUG)
            raise LLMHttpError("timeout", model_id=model_id) from None
        except httpx.HTTPError as exc:
            # Never `exc_info`, never the message: a URL or a TLS detail is not worth a key's risk.
            log_failure(_LOGGER, "llm_http.request", exc, level=logging.DEBUG)
            kind: Problem = "tls" if _is_tls(exc) else "unreachable"
            raise LLMHttpError(kind, model_id=model_id) from None
        except (ValueError, OSError) as exc:  # an address httpx cannot use, a socket error outside it
            log_failure(_LOGGER, "llm_http.request", exc, level=logging.DEBUG)
            raise LLMHttpError("unreachable", model_id=model_id) from None
        if 200 <= status < 300:
            try:
                payload = json.loads(raw) if raw else {}
            except (ValueError, RecursionError):
                raise LLMHttpError("bad_response", status=status, model_id=model_id) from None
            if isinstance(payload, dict) and "error" in payload:
                err = payload["error"]
                code = err.get("code") if isinstance(err, dict) else None
                err_status = code if isinstance(code, int) else None
                if err_status and err_status >= 500:
                    raise LLMHttpError("server_error", status=err_status, model_id=model_id)
            return payload
        error = _classify(status, raw, model_id)
        error.retry_after = retry_after
        raise error

    @staticmethod
    def _read(response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                raise LLMHttpError("bad_response", status=response.status_code)
            chunks.append(chunk)
        return b"".join(chunks)

    def count_tokens(
        self,
        text: str,
        *,
        model_id: str | None = None,  # noqa: ARG002 - the protocol's signature
    ) -> int:
        """The shared approximation: neither protocol offers a free token count."""
        return estimate_tokens(text)


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def _is_tls(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "certificate" in text or "ssl" in text or "tls" in text


def _classify(status: int, raw: bytes, model_id: str | None) -> LLMHttpError:
    """The failure a non-2xx status means. Only the *names* of refused parameters are read from the body."""
    kind: Problem
    if status in (401, 403):
        kind = "key_rejected"
    elif status == 429:
        kind = "rate_limited"
    elif status == 408:
        kind = "timeout"
    elif status == 404:
        kind = "not_found"
    elif status == 413:
        kind = "too_long"
    elif 300 <= status < 400:
        kind = "redirect"
    elif status >= 500:
        kind = "server_error"
    else:
        kind = "bad_request"
    rejected: tuple[str, ...] = ()
    if kind == "bad_request":
        lowered = raw[:4096].decode("utf-8", errors="ignore").lower()
        rejected = tuple(name for name in _REJECTABLE if name in lowered)
    return LLMHttpError(kind, status=status, model_id=model_id, rejected=rejected)


def _model_ids(payload: Any) -> list[str]:
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise LLMHttpError("bad_response")
    ids: list[str] = []
    for row in rows:
        value = row.get("id") if isinstance(row, dict) else row
        if isinstance(value, str) and value and value not in ids:
            ids.append(value)
    return sorted(ids)[:MAX_MODELS]


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


# ---------------------------------------------------------------------------
# OpenAI chat protocol
# ---------------------------------------------------------------------------
class OpenAICompatibleClient(_HttpClient):
    """`POST {base}/chat/completions` and `{base}/embeddings`, `GET {base}/models`.

    `base_url` includes the version path when the service has one (`https://api.openai.com/v1`).
    The token ceiling is sent as `max_completion_tokens` to OpenAI itself and as `max_tokens` to every
    other server; a model that refuses one of them, or a non-default `temperature`, is asked once more
    without it (the answer is remembered for the client's later calls).
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        host = httpx.URL(self._base_url).host if self._base_url else ""
        self._token_param = "max_completion_tokens" if host == "api.openai.com" else "max_tokens"
        self._send_temperature = True

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        if max_tokens < 1:
            raise LLMError("LLM_INVALID_REQUEST", "max_tokens must be at least 1.", model_id=model_id)
        chosen = model_id or self._model_id
        if not chosen:
            raise LLMError("LLM_INVALID_REQUEST", "No model is set for the AI service.")
        messages: list[dict[str, str]] = []
        if system.strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        def body() -> dict[str, Any]:
            document: dict[str, Any] = {"model": chosen, "messages": messages, self._token_param: max_tokens}
            if self._send_temperature:
                document["temperature"] = temperature
            return document

        try:
            data = self._request("POST", "chat/completions", body(), model_id=chosen)
        except LLMHttpError as exc:
            if not self._adapt(exc):
                raise
            data = self._request("POST", "chat/completions", body(), model_id=chosen)
        return self._completion(data, chosen)

    def _adapt(self, exc: LLMHttpError) -> bool:
        """Learn, once, what this model does not take. True when the request changed."""
        if exc.kind != "bad_request" or not exc.rejected:
            return False
        changed = False
        if "temperature" in exc.rejected and self._send_temperature:
            self._send_temperature = False
            changed = True
        for refused, other in (
            ("max_tokens", "max_completion_tokens"),
            ("max_completion_tokens", "max_tokens"),
        ):
            if refused in exc.rejected and self._token_param == refused:
                self._token_param = other
                changed = True
                break
        return changed

    @staticmethod
    def _completion(data: Any, chosen: str) -> LLMCompletion:
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            status = code if isinstance(code, int) else None
            kind: Problem = "server_error" if status and status >= 500 else "bad_response"
            raise LLMHttpError(kind, status=status, model_id=chosen)
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMHttpError("bad_response", model_id=chosen) from None
        if isinstance(content, list):  # some servers answer in typed parts
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        text = content if isinstance(content, str) else ""
        if not text.strip():
            raise LLMError("LLM_REFUSED", "The model returned nothing to read.", model_id=chosen)
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        return LLMCompletion(
            text=text,
            model_id=chosen,
            input_tokens=_int(usage.get("prompt_tokens")),
            output_tokens=_int(usage.get("completion_tokens")),
            cost_estimate_usd=None,
            stop_reason=str(choice.get("finish_reason") or ""),
        )

    def embed(self, texts: Sequence[str], *, model_id: str | None = None) -> tuple[tuple[float, ...], ...]:
        chosen = model_id or self._embedding_model_id
        if not chosen:
            raise LLMHttpError("no_embeddings")
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(texts), _EMBED_BATCH):
            batch = list(texts[start : start + _EMBED_BATCH])
            data = self._request("POST", "embeddings", {"model": chosen, "input": batch}, model_id=chosen)
            try:
                rows = sorted(data["data"], key=lambda row: row.get("index", 0))
                got = [tuple(float(x) for x in row["embedding"]) for row in rows]
            except (KeyError, TypeError, ValueError, AttributeError):
                raise LLMHttpError("bad_response", model_id=chosen) from None
            if len(got) != len(batch) or not all(got):
                raise LLMHttpError("bad_response", model_id=chosen)
            vectors.extend(got)
        return tuple(vectors)

    def list_models(self) -> list[str]:
        """The model ids `GET {base}/models` lists (at most `MAX_MODELS`, sorted)."""
        return _model_ids(self._request("GET", "models"))


# ---------------------------------------------------------------------------
# Anthropic Messages API
# ---------------------------------------------------------------------------
class AnthropicClient(_HttpClient):
    """`POST {base}/v1/messages` and `GET {base}/v1/models`, with `x-api-key` and a pinned version.

    `base_url` is the host (`https://api.anthropic.com`); a trailing `/v1` is accepted and not doubled.
    There are no embeddings: `embed` says so, and `engine.ai_service` never asks it to.
    """

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "anthropic-version": ANTHROPIC_VERSION}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    def _url(self, path: str) -> str:
        base = self._base_url[:-3] if self._base_url.endswith("/v1") else self._base_url
        return f"{base}/v1/{path.lstrip('/')}"

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        if max_tokens < 1:
            raise LLMError("LLM_INVALID_REQUEST", "max_tokens must be at least 1.", model_id=model_id)
        chosen = model_id or self._model_id
        if not chosen:
            raise LLMError("LLM_INVALID_REQUEST", "No model is set for the AI service.")
        body: dict[str, Any] = {
            "model": chosen,
            "max_tokens": max_tokens,
            "temperature": min(max(temperature, 0.0), 1.0),
            "messages": [{"role": "user", "content": prompt}],
        }
        if system.strip():
            body["system"] = system
        try:
            data = self._request("POST", "messages", body, model_id=chosen)
        except LLMHttpError as exc:
            if exc.kind != "bad_request" or "temperature" not in exc.rejected:
                raise
            body.pop("temperature")
            data = self._request("POST", "messages", body, model_id=chosen)
        try:
            blocks = data["content"]
            if not isinstance(blocks, list):
                raise TypeError("content is not a list")
            text = "".join(
                str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text"
            )
        except (KeyError, TypeError, AttributeError):
            raise LLMHttpError("bad_response", model_id=chosen) from None
        if not text.strip():
            raise LLMError("LLM_REFUSED", "The model returned nothing to read.", model_id=chosen)
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        return LLMCompletion(
            text=text,
            model_id=chosen,
            input_tokens=_int(usage.get("input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
            cost_estimate_usd=None,
            stop_reason=str(data.get("stop_reason") or ""),
        )

    def embed(
        self,
        texts: Sequence[str],  # noqa: ARG002 - the protocol's signature
        *,
        model_id: str | None = None,  # noqa: ARG002
    ) -> tuple[tuple[float, ...], ...]:
        raise LLMHttpError("no_embeddings")

    def list_models(self) -> list[str]:
        """The model ids `GET {base}/v1/models` lists (at most `MAX_MODELS`, sorted)."""
        return _model_ids(self._request("GET", "models", params={"limit": str(MAX_MODELS)}))
