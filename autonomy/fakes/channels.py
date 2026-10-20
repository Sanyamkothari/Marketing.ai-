"""A channel adapter that sends nowhere and remembers everything, plus a keyword text classifier."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime

from autonomy.contracts import Channel, Event, EventType, Message, SendReceipt, stable_id


class FakeChannelAdapter:
    """Accepts every message. Webhook bodies are JSON: {"type", "message_id", "decision_id", "customer_id", "ts"}."""

    def __init__(self, channel: Channel, *, secret: str = "test-secret") -> None:
        self.channel = channel
        self.secret = secret
        self.sent: list[Message] = []

    def send(self, message: Message) -> SendReceipt:
        self.sent.append(message)
        return SendReceipt(accepted=True, provider_message_id=stable_id("pm", message.message_id))

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # noqa: ARG002
        return headers.get("x-fake-signature") == self.secret

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[Event]:  # noqa: ARG002
        data = json.loads(body)
        rows = data if isinstance(data, list) else [data]
        return [
            Event(
                event_id=stable_id("ev", self.channel.value, r["message_id"], r["type"]),
                customer_id=r["customer_id"],
                type=EventType(r["type"]),
                ts=datetime.fromisoformat(r["ts"]),
                source=f"fake-{self.channel.value}",
                channel=self.channel,
                decision_id=r.get("decision_id"),
                text=r.get("text"),
            )
            for r in rows
        ]


class KeywordTextSignals:
    """Picks the first label whose name appears in the text; else the last label. Probability 0.9 / 0.5."""

    def classify(
        self, texts: Sequence[str], *, question: str, labels: Sequence[str]  # noqa: ARG002
    ) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for text in texts:
            low = text.lower()
            hit = next((lab for lab in labels if lab.lower() in low), None)
            out.append((hit, 0.9) if hit else (labels[-1], 0.5))
        return out
