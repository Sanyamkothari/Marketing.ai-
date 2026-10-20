"""The content library: approved message variants, labelled so bandits can learn over labels.

The Creative writes items with `{{slot}}` placeholders; the Critic approves or rejects them. At send
time the dispatcher fills the slots from customer state and turns the item into a `Message`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from autonomy.contracts.base import Channel, Contract


class Theme(StrEnum):
    SAVINGS = "savings"
    QUALITY = "quality"
    URGENCY = "urgency"
    SOCIAL_PROOF = "social_proof"
    EDUCATION = "education"
    CONVENIENCE = "convenience"
    NOVELTY = "novelty"
    LOYALTY = "loyalty"
    REMINDER = "reminder"
    PERSONAL = "personal"


class Tone(StrEnum):
    FRIENDLY = "friendly"
    PROFESSIONAL = "professional"
    PLAYFUL = "playful"
    URGENT = "urgent"
    CALM = "calm"


class Format(StrEnum):
    TEXT = "text"
    TEXT_IMAGE = "text_image"
    CAROUSEL = "carousel"
    VIDEO = "video"


class ContentStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETIRED = "retired"


class ContentItem(Contract):
    content_id: str
    goal_id: str | None = None
    channel: Channel
    format: Format = Format.TEXT
    theme: Theme
    tone: Tone = Tone.FRIENDLY
    offer_id: str = "none"
    subject: str | None = Field(default=None, description="Email only")
    body: str = Field(description="Text with {{slot}} placeholders, e.g. {{first_name}}, {{top_category}}")
    slots: tuple[str, ...] = ()
    asset_url: str | None = None
    template_name: str | None = Field(default=None, description="WhatsApp: the pre-approved template")
    language: str = "en"
    status: ContentStatus = ContentStatus.DRAFT
    critic_notes: tuple[str, ...] = ()
    created_at: AwareDatetime


class Message(Contract):
    """A rendered message, ready for a channel adapter."""

    message_id: str
    decision_id: str
    customer_id: str
    channel: Channel
    to: str = Field(description="Email address, phone number or the client tool's customer id")
    subject: str | None = None
    body: str
    asset_url: str | None = None
    template_name: str | None = None
    template_params: tuple[str, ...] = ()
    send_at: AwareDatetime


class SendReceipt(Contract):
    """What a channel adapter says after trying to send."""

    accepted: bool
    provider_message_id: str | None = None
    error: str | None = None
