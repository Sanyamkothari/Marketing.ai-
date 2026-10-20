"""Ready-made goal, brief and content for tests and demos."""

from __future__ import annotations

from datetime import datetime

from autonomy.contracts import (
    Audience,
    AudienceFilter,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    Envelope,
    FollowUpStep,
    Goal,
    GoalKind,
    Objective,
    OfferKind,
    OfferOption,
    Theme,
    Tone,
)
from autonomy.contracts.goals import NO_OFFER

TEN_OFF = OfferOption(
    offer_id="pct10", label="10% off", kind=OfferKind.PERCENT_OFF, amount=10, cost_per_redemption=6.0
)
FREE_SHIP = OfferOption(
    offer_id="freeship", label="Free shipping", kind=OfferKind.FREE_SHIPPING, cost_per_redemption=4.0
)


def sample_goal(now: datetime, goal_id: str = "goal_winback") -> Goal:
    return Goal(
        goal_id=goal_id,
        business_id="demo",
        text="Win back customers who haven't bought in 60 days, $5,000 this month",
        kind=GoalKind.WIN_BACK,
        objective=Objective.GROSS_PROFIT,
        budget=5000,
        starts_at=now,
        created_at=now,
    )


def sample_brief(now: datetime, goal_id: str = "goal_winback") -> Brief:
    return Brief(
        brief_id=f"brief_{goal_id}_1",
        goal_id=goal_id,
        objective=Objective.GROSS_PROFIT,
        audience=Audience(
            description="Bought before, nothing in the last 60 days",
            filters=(
                AudienceFilter(field="purchases", op="gte", value=1),
                AudienceFilter(field="last_purchase_at", op="days_since_gt", value=60),
            ),
        ),
        horizon_days=14,
        envelope=Envelope(
            channels=(Channel.EMAIL, Channel.WHATSAPP),
            offers=(NO_OFFER, FREE_SHIP, TEN_OFF),
            budget=5000,
            max_contacts_per_week=2,
        ),
        follow_ups=(FollowUpStep(after_days=3, when="no_purchase"),),
        rationale="Lapsed buyers respond to a reminder; offers only where they change behaviour.",
        created_at=now,
        approved_by="founder",
        approved_at=now,
    )


def sample_content(now: datetime, goal_id: str = "goal_winback") -> list[ContentItem]:
    rows = [
        (
            Channel.EMAIL,
            Theme.REMINDER,
            "none",
            "We miss you, {{first_name}}",
            "New in {{top_category}} since your last visit.",
        ),
        (
            Channel.EMAIL,
            Theme.SAVINGS,
            "pct10",
            "10% off, just for you",
            "Come back to {{top_category}} with 10% off this week.",
        ),
        (
            Channel.EMAIL,
            Theme.EDUCATION,
            "none",
            "Getting the most from your order",
            "Three tips for {{top_category}}.",
        ),
        (
            Channel.WHATSAPP,
            Theme.REMINDER,
            "none",
            None,
            "Hi {{first_name}}, there's something new in {{top_category}}.",
        ),
        (
            Channel.WHATSAPP,
            Theme.SAVINGS,
            "freeship",
            None,
            "Hi {{first_name}}, free shipping on your next order this week.",
        ),
    ]
    return [
        ContentItem(
            content_id=f"ci_{i}",
            goal_id=goal_id,
            channel=ch,
            theme=theme,
            tone=Tone.FRIENDLY,
            offer_id=offer,
            subject=subject,
            body=body,
            slots=("first_name", "top_category"),
            template_name=f"winback_{i}" if ch is Channel.WHATSAPP else None,
            status=ContentStatus.APPROVED,
            created_at=now,
        )
        for i, (ch, theme, offer, subject, body) in enumerate(rows)
    ]
