"""The scenario library: named businesses with planted truths, plus the brief and content they run on.

Each scenario changes one or two knobs of a plain mid-size shop so that a failure points at one
skill: leaving sleeping dogs alone, not wasting discounts, pacing WhatsApp, timing sends, noticing
drift, coping with a small list, doing nothing when nothing works, and stopping when contacts hurt.

Every scenario spells out its uplifts rather than leaning on the world's defaults, so the planted
truth is readable here and does not move if the world's defaults change.
"""

from __future__ import annotations

from datetime import UTC, datetime

from autonomy.contracts import (
    NO_OFFER,
    Audience,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    Envelope,
    Format,
    Objective,
    OfferKind,
    OfferOption,
    Theme,
    Tone,
)
from autonomy.gym.api import PERSONAS, ScenarioSpec

GYM_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
"""A fixed timestamp for the library's briefs and content, so runs never depend on the clock."""

GYM_GOAL_ID = "goal_gym"

FREE_SHIPPING = OfferOption(
    offer_id="freeship", label="Free shipping", kind=OfferKind.FREE_SHIPPING, cost_per_redemption=4.0
)
TEN_OFF = OfferOption(
    offer_id="pct10", label="10% off", kind=OfferKind.PERCENT_OFF, amount=10, cost_per_redemption=6.0
)
GYM_OFFERS: tuple[OfferOption, ...] = (NO_OFFER, FREE_SHIPPING, TEN_OFF)

# --- planted uplifts ---------------------------------------------------------------------------
# Added daily purchase probability while a contact's effect lasts (7 days by default). A base email
# uplift of 0.004 over a week is about 2.8 extra purchases per 100 contacts, worth roughly $0.67
# of margin per contact at a $60 basket and 40% margin: clearly worth a $0.001 email, but easily
# wiped out by a discount handed to someone who would have bought anyway.

_BASE_CHANNEL_UPLIFT: dict[str, dict[Channel, float]] = {
    "researcher": {Channel.EMAIL: 0.004, Channel.WHATSAPP: 0.006},
    "price_sensitive": {Channel.EMAIL: 0.003, Channel.WHATSAPP: 0.005},
    "loyal": {Channel.EMAIL: 0.002, Channel.WHATSAPP: 0.002},
    "new": {Channel.EMAIL: 0.006, Channel.WHATSAPP: 0.008},
    "at_risk": {Channel.EMAIL: 0.005, Channel.WHATSAPP: 0.007},
}
_BASE_OFFER_UPLIFT: dict[str, dict[str, float]] = {
    "researcher": {OfferKind.PERCENT_OFF.value: 1.3, OfferKind.FREE_SHIPPING.value: 1.2},
    "price_sensitive": {OfferKind.PERCENT_OFF.value: 2.5, OfferKind.FREE_SHIPPING.value: 1.8},
    "loyal": {OfferKind.PERCENT_OFF.value: 1.0, OfferKind.FREE_SHIPPING.value: 1.0},
    "new": {OfferKind.PERCENT_OFF.value: 1.5, OfferKind.FREE_SHIPPING.value: 1.5},
    "at_risk": {OfferKind.PERCENT_OFF.value: 1.8, OfferKind.FREE_SHIPPING.value: 1.4},
}
_BASE_THEME_UPLIFT: dict[str, dict[str, float]] = {
    "researcher": {Theme.EDUCATION.value: 1.4, Theme.SAVINGS.value: 0.9},
    "price_sensitive": {Theme.SAVINGS.value: 1.3},
    "loyal": {Theme.LOYALTY.value: 1.3},
    "new": {Theme.EDUCATION.value: 1.2},
    "at_risk": {Theme.REMINDER.value: 1.2, Theme.PERSONAL.value: 1.2},
}


def _scaled(
    uplift: dict[str, dict[Channel, float]], factors: dict[Channel, float]
) -> dict[str, dict[Channel, float]]:
    return {p: {ch: v * factors.get(ch, 1.0) for ch, v in row.items()} for p, row in uplift.items()}


def _flat(value: float) -> dict[str, dict[str, float]]:
    """The same offer multiplier for every persona and offer kind."""
    kinds = (OfferKind.PERCENT_OFF.value, OfferKind.FREE_SHIPPING.value)
    return {p: dict.fromkeys(kinds, value) for p in PERSONAS}


def _base(name: str, description: str, **changes: object) -> ScenarioSpec:
    fields: dict[str, object] = {
        "name": name,
        "description": description,
        "channel_uplift": _BASE_CHANNEL_UPLIFT,
        "offer_uplift": _BASE_OFFER_UPLIFT,
        "theme_uplift": _BASE_THEME_UPLIFT,
    }
    fields.update(changes)
    return ScenarioSpec.model_validate(fields)


def _library() -> dict[str, ScenarioSpec]:
    specs = [
        _base(
            "baseline_shop",
            "A plain mid-size online shop: contacts help a little, offers help price-sensitive people.",
        ),
        _base(
            "sleeping_dogs_15",
            "15% of customers buy less after any contact; a good agent learns to leave them alone.",
            sleeping_dog_share=0.15,
            sleeping_dog_effect=-0.5,
        ),
        _base(
            "discount_cannibalisation",
            "Offers barely change behaviour and loyal customers buy anyway, so most discounts are "
            "handed to people who would have paid full price.",
            persona_mix={
                "researcher": 0.15,
                "price_sensitive": 0.1,
                "loyal": 0.4,
                "new": 0.15,
                "at_risk": 0.2,
            },
            daily_purchase_rate={
                "researcher": 0.006,
                "price_sensitive": 0.004,
                "loyal": 0.03,
                "new": 0.008,
                "at_risk": 0.002,
            },
            offer_uplift=_flat(1.05),
            discount_redeemed_by_sure_things=True,
        ),
        _base(
            "whatsapp_fatigue",
            "WhatsApp works three times as well as email but tires people fast: every extra weekly "
            "contact costs much of the effect and drives unsubscribes.",
            channel_uplift=_scaled(_BASE_CHANNEL_UPLIFT, {Channel.WHATSAPP: 2.0}),
            fatigue_per_weekly_contact=0.6,
            unsubscribe_per_weekly_contact=0.012,
        ),
        _base(
            "send_time_matters",
            "Customers in four time zones; a message far from someone's best hour loses most of its effect.",
            hour_sensitivity=0.9,
            timezones={
                "Europe/London": 0.3,
                "America/New_York": 0.3,
                "Asia/Kolkata": 0.2,
                "Australia/Sydney": 0.2,
            },
        ),
        _base(
            "drift_midway",
            "On decision day 45 everyone starts buying 40% less on their own; the agent must notice.",
            drift_day=45,
            drift_purchase_multiplier=0.6,
        ),
        _base(
            "small_business",
            "The plain shop with only 1,500 customers: little data, wide intervals.",
            n_customers=1500,
        ),
        _base(
            "null_effect",
            "Contacts change nothing for anyone. A good agent should mostly do nothing.",
            channel_uplift={p: {Channel.EMAIL: 0.0, Channel.WHATSAPP: 0.0} for p in PERSONAS},
            offer_uplift={},
            theme_uplift={},
        ),
        _base(
            "complaint_storm",
            "Planted harm: every contact carries a high unsubscribe and complaint risk. The breaker "
            "should stop sending within two batches.",
            unsubscribe_base=0.02,
            unsubscribe_per_weekly_contact=0.02,
            complaint_share_of_unsubscribes=0.5,
        ),
        _base(
            "smoke",
            "A tiny, fast version of the plain shop for tests and quick checks.",
            n_customers=300,
            history_days=60,
            days=14,
        ),
    ]
    return {s.name: s for s in specs}


SCENARIOS: dict[str, ScenarioSpec] = _library()

PLANTED_HARM: frozenset[str] = frozenset({"complaint_storm"})
"""Scenarios where contacts do clear harm; the breaker criterion is judged only on these."""


def get_scenario(name: str, **overrides: object) -> ScenarioSpec:
    """A scenario by name, optionally with knobs changed (e.g. fewer customers for a quick run)."""
    try:
        spec = SCENARIOS[name]
    except KeyError as exc:
        raise KeyError(f"unknown scenario {name!r}; known: {', '.join(sorted(SCENARIOS))}") from exc
    if not overrides:
        return spec
    return ScenarioSpec.model_validate({**spec.model_dump(), **overrides})


def default_brief(goal_id: str = GYM_GOAL_ID) -> Brief:
    """An approved brief that lets the agent reach anyone by email or WhatsApp, with two offers.

    The audience is everyone: the Gym asks whether the policy can find who is worth contacting, so
    the brief should not do that work for it.
    """
    return Brief(
        brief_id=f"brief_{goal_id}_1",
        goal_id=goal_id,
        objective=Objective.GROSS_PROFIT,
        audience=Audience(description="Every customer"),
        horizon_days=14,
        envelope=Envelope(
            channels=(Channel.EMAIL, Channel.WHATSAPP),
            offers=GYM_OFFERS,
            budget=1_000_000.0,
            max_contacts_per_week=2,
            quiet_start_hour=21,
            quiet_end_hour=9,
            holdout_share=0.10,
            baseline_share=0.10,
        ),
        rationale="Gym brief: everyone is eligible; the policy decides who is worth a contact.",
        created_by="gym",
        created_at=GYM_EPOCH,
        approved_by="gym",
        approved_at=GYM_EPOCH,
    )


def default_content(goal_id: str = GYM_GOAL_ID) -> list[ContentItem]:
    """An approved library covering every channel x offer pair, with a few themes to choose between."""
    rows: list[tuple[Channel, Theme, str, str | None, str]] = [
        (Channel.EMAIL, Theme.REMINDER, "none", "Something new for you", "New in {{top_category}}."),
        (Channel.EMAIL, Theme.EDUCATION, "none", "Getting more from {{top_category}}", "Three tips."),
        (Channel.EMAIL, Theme.LOYALTY, "none", "Thanks for sticking with us", "A note for regulars."),
        (Channel.EMAIL, Theme.CONVENIENCE, "freeship", "Free shipping this week", "On {{top_category}}."),
        (Channel.EMAIL, Theme.SAVINGS, "pct10", "10% off, just for you", "This week only."),
        (Channel.WHATSAPP, Theme.REMINDER, "none", None, "Hi {{first_name}}, new in {{top_category}}."),
        (Channel.WHATSAPP, Theme.PERSONAL, "none", None, "Hi {{first_name}}, we picked these for you."),
        (Channel.WHATSAPP, Theme.CONVENIENCE, "freeship", None, "Free shipping this week, {{first_name}}."),
        (Channel.WHATSAPP, Theme.SAVINGS, "pct10", None, "10% off this week, {{first_name}}."),
    ]
    return [
        ContentItem(
            content_id=f"gym_{ch.value}_{offer}_{theme.value}",
            goal_id=goal_id,
            channel=ch,
            format=Format.TEXT,
            theme=theme,
            tone=Tone.FRIENDLY,
            offer_id=offer,
            subject=subject,
            body=body,
            slots=("first_name", "top_category"),
            template_name=f"gym_{offer}_{theme.value}" if ch is Channel.WHATSAPP else None,
            status=ContentStatus.APPROVED,
            created_at=GYM_EPOCH,
        )
        for ch, theme, offer, subject, body in rows
    ]
