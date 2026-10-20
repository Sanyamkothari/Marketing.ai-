"""Small helpers the dispatcher and the provider adapters share.

`send_event_id` matters most: the dispatcher records a SEND when a provider accepts a message, and
the provider later confirms the same send through its webhook (SES Send, WhatsApp `sent`, a webhook
tool's `send` callback). Both must get the same event id so the event store keeps one SEND per
message; two would double contact counts and trip frequency caps at half the real rate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from autonomy.contracts import EventType, stable_id


def send_event_id(message_id: str) -> str:
    """The one event id for the SEND of a message, whoever records it first."""
    return stable_id("ev", message_id, EventType.SEND.value)


def header(headers: Mapping[str, str], name: str) -> str | None:
    """A header's value, whatever case the web framework handed the names in."""
    wanted = name.lower()
    return next((value for key, value in headers.items() if key.lower() == wanted), None)


def str_or_none(value: Any) -> str | None:
    """A non-empty string from untrusted JSON, or None for anything else."""
    return value if isinstance(value, str) and value else None
