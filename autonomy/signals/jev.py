"""Text -> signals through TypeSafe's Jev typed-question API (plan-v2 section 2, the third candidate).

**Unverified:** we have not seen Jev's API reference. Everything this module assumes about the wire
format lives in `jev_exchange` and the constants beside it, so when the real shape is known only
that one function changes. The assumed exchange is one POST per batch:

    POST {base_url}/v1/questions/answer
    Authorization: Bearer <api_key>
    {"question": "...", "answer_type": {"type": "enum", "values": ["stop", ..., "other"]},
     "inputs": [{"id": "0", "text": "..."}, ...]}

    200 {"answers": [{"id": "0", "value": "stop", "confidence": 0.93}, ...]}

The reader is lenient where an API could plausibly differ: the list may be called `answers` or
`results`, the label `value`, `label` or `answer`, the probability `confidence`, `probability` or
`score`, and ids may be strings or numbers. Like the LLM version, an answer outside the labels, or a
missing one, becomes the last label with a low probability. A failed request (network error, timeout,
an HTTP error status, a body that is not JSON) raises `JevError`; callers such as the Conversation
agent already hand a failed classification to a person.

Tests pass an `httpx.MockTransport`; nothing here is ever called over the network in tests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any

import httpx

from autonomy.signals.llm_signals import (
    DEFAULT_FALLBACK_PROBABILITY,
    check_labels,
    clamp_probability,
    match_label,
)

JEV_PATH = "/v1/questions/answer"
_LIST_KEYS = ("answers", "results")
_LABEL_KEYS = ("value", "label", "answer")
_PROB_KEYS = ("confidence", "probability", "score")


class JevError(RuntimeError):
    """Jev could not be reached or gave an answer we cannot read. Never carries the API key."""


def _first(item: Mapping[str, Any], keys: Sequence[str]) -> Any:
    return next((item[k] for k in keys if k in item), None)


def _index(raw: Any) -> int | None:
    """The text index an answer's id names: a whole number or its digits. `int()` alone would read
    1.9 or `true` as 1 and give a text another text's answer."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdecimal():
        return int(raw.strip())
    return None


def jev_exchange(
    client: httpx.Client,
    texts: Sequence[str],
    *,
    question: str,
    labels: Sequence[str],
    fallback_probability: float = DEFAULT_FALLBACK_PROBABILITY,
) -> list[tuple[str, float]]:
    """One batch to Jev and back. The only code that knows Jev's (assumed) request and response."""
    request = {
        "question": question,
        "answer_type": {"type": "enum", "values": list(labels)},
        "inputs": [{"id": str(i), "text": t} for i, t in enumerate(texts)],
    }
    try:
        response = client.post(JEV_PATH, json=request)
    except httpx.HTTPError as err:
        raise JevError(f"Jev request failed: {type(err).__name__}") from err
    if response.status_code >= 400:
        raise JevError(f"Jev answered HTTP {response.status_code}: {response.text[:200]}")
    try:
        payload = response.json()
    except ValueError as err:
        raise JevError("Jev answered with a body that is not JSON") from err

    fallback = (labels[-1], fallback_probability)
    items = _first(payload, _LIST_KEYS) if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise JevError("Jev's answer has no list of answers")
    by_index: dict[int, tuple[str, float]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        index = _index(item.get("id"))
        if index is None or index in by_index or not 0 <= index < len(texts):
            continue
        raw_label = _first(item, _LABEL_KEYS)
        label = match_label(raw_label, labels) if isinstance(raw_label, str) else None
        raw_p = _first(item, _PROB_KEYS)
        if label is None or isinstance(raw_p, bool) or not isinstance(raw_p, int | float):
            by_index[index] = fallback
        else:
            by_index[index] = (label, clamp_probability(float(raw_p), fallback_probability))
    return [by_index.get(i, fallback) for i in range(len(texts))]


class JevTextSignals:
    """`TextSignals` backed by Jev over HTTP. Use as a context manager, or call `close()`."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 10.0,
        *,
        transport: httpx.BaseTransport | None = None,
        batch_size: int = 50,
        fallback_probability: float = DEFAULT_FALLBACK_PROBABILITY,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be above 0")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if not 0.0 <= fallback_probability <= 1.0:
            raise ValueError("fallback_probability must be between 0 and 1")
        self._api_key = api_key
        self._batch_size = batch_size
        self._fallback_p = fallback_probability
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def classify(
        self, texts: Sequence[str], *, question: str, labels: Sequence[str]
    ) -> list[tuple[str, float]]:
        allowed = check_labels(labels)
        out: list[tuple[str, float]] = []
        for start in range(0, len(texts), self._batch_size):
            try:
                answers = jev_exchange(
                    self._client,
                    texts[start : start + self._batch_size],
                    question=question,
                    labels=allowed,
                    fallback_probability=self._fallback_p,
                )
            except JevError as err:
                # An error body may echo the request, Authorization header included.
                message = str(err)
                if self._api_key in message:
                    raise JevError(message.replace(self._api_key, "<redacted>")) from err.__cause__
                raise
            out.extend(answers)
        return out

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JevTextSignals:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
