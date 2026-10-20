"""The base model every v2 contract inherits, plus id helpers."""

from __future__ import annotations

import hashlib
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    """Frozen, and unknown keys are an error: a typo in a field name fails loudly."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Channel(StrEnum):
    """Where a message can go. `WEBHOOK` means "hand the decision to the client's own tool"."""

    EMAIL = "email"
    WHATSAPP = "whatsapp"
    SMS = "sms"
    PUSH = "push"
    WEB = "web"
    WEBHOOK = "webhook"


class Group(StrEnum):
    """The three-way split every customer belongs to, for proof (plan-v2 section 8)."""

    AGENT = "agent"  # the agent decides
    BASELINE = "baseline"  # a simple, transparent rule decides
    HOLDOUT = "holdout"  # never contacted by us


def new_id(prefix: str) -> str:
    """A random id such as `dec_3f2a...`. Use for live objects."""
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def stable_id(prefix: str, *parts: object) -> str:
    """A deterministic id from its parts. Use in the Gym and in tests, where runs must repeat."""
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:20]
    return f"{prefix}_{digest}"
