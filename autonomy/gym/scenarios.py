"""The scenario library: named businesses with planted truths, plus the brief and content they run on.

Each scenario changes one or two knobs of a plain mid-size shop so that a failure points at one
skill: leaving sleeping dogs alone, not wasting discounts, pacing WhatsApp, timing sends, noticing
drift, coping with a small list, doing nothing when nothing works, and stopping when contacts hurt.

Every scenario spells out its uplifts rather than leaning on the world's defaults, so the planted
truth is readable here and does not move if the world's defaults change.

Realism (wave 3a), shared by every scenario here:

* The history holds the client's past campaigns (`history_sends_per_week` = 0.4: a weekly campaign
  that reaches a recent buyer about three weeks in five and a lapsed one about three in ten), with
  their clicks, unsubscribes and the purchases they caused, logged with their probabilities. A real
  client hands over that CRM history; a bare `ScenarioSpec` keeps the old history without it.
* Unsubscribes and complaints cost their lost future value (the `ScenarioSpec` default: 180 days of
  a weekly message at the customer's own uplift), plus `COMPLAINT_COST` ($5) of deliverability per
  complaint. This is what makes `complaint_storm` a fair test: a policy that keeps sending into a
  storm now pays for the list it burns, in the truth and in the realised money.
* Sleeping dogs leave faint clues in the history (`sleeping_dog_signal` 0.5, the default), as
  annoyed people do. `sleeping_dogs_15` uses the default; `sleeping_dogs_hidden` keeps the old,
  invisible coin flip (signal 0) so the hard case stays on the board.
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
from autonomy.gym.calibration import CampaignFit, ClickFit, GroupFit, scenario_from_fit

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

HISTORY_SENDS_PER_WEEK = 0.4
"""The library's past marketing: the weekly campaign's base inclusion probability (see the world)."""

COMPLAINT_COST = 5.0
"""The library's deliverability cost of one spam complaint. Mailbox providers start routing a
sender's mail to spam once complaints pass about 0.1-0.3% of sends; for a shop of a few thousand
customers one complaint is a sizeable step towards that line, and the reach it costs is worth a few
dollars of later margin. A round, deliberately modest figure: it is a knob, not a measurement."""
# --- real-calibrated scenarios -------------------------------------------------------------------
# The scenarios above plant truths we chose. These two plant truths FITTED to real randomised
# campaigns, on the TRAIN half of each (the bench's customer-id-hash split; the test half and the
# sealed datasets are never seen), so the Gym stops encoding our own beliefs. The numbers below are
# the output of `python -m scripts.v2_realbench.gym_fit --literal`; `--check` refits and compares.
# What was fitted, what was not and why: docs/v2/GYM_BAR.md, "Real-calibrated scenarios".

REAL_EMAIL_FIT = CampaignFit(
    provenance="public randomised e-mail campaign (men's e-mail, women's e-mail, no e-mail; 1/3 each; 64,000 customers; outcomes over 2 weeks); fitted on the TRAIN half by customer-id hash",
    window_days=14,
    n_train=32014,
    channel=Channel.EMAIL,
    arms=("mens_email", "womens_email"),
    groups=(
        GroupFit(
            name="new",
            n=16076,
            share=0.502155,
            control_rate_raw=0.00466941,
            control_rate=0.00465965,
            arm_lift_raw=(0.00522789, 0.00315619),
            arm_lift_se=(0.00164256, 0.00152143),
            arm_lift=(0.00612592, 0.0036559),
        ),
        GroupFit(
            name="recent",
            n=5264,
            share=0.164428,
            control_rate_raw=0.0103448,
            control_rate=0.00676523,
            arm_lift_raw=(0.00777522, 0.000462908),
            arm_lift_se=(0.00399479, 0.00345906),
            arm_lift=(0.00612592, 0.0036559),
        ),
        GroupFit(
            name="lapsed",
            n=10674,
            share=0.333417,
            control_rate_raw=0.00359812,
            control_rate=0.00382255,
            arm_lift_raw=(0.00704936, 0.00532547),
            arm_lift_se=(0.00200592, 0.00185972),
            arm_lift=(0.00612592, 0.0036559),
        ),
    ),
    clicks=ClickFit(
        logged=True,
        control_rate=0.106005,
        treated_rate=0.165157,
        floor=0.106005,
        per_extra_purchase=12.1702,
        per_extra_purchase_low=8.778,
        per_extra_purchase_high=19.4434,
        caused_share_of_treated_clicks=0.358154,
        click_purchase_phi=0.226994,
    ),
    aov_mean=104.904,
    margin_rate=0.4,
    channel_cost=0.001,
    fatigue_per_weekly_contact=0.25,
    not_fitted=(
        "contact fatigue: a single-send randomised campaign has no repeated contacts, so it is not identifiable; the Gym's library value is kept (fatigue_per_weekly_contact 0.25)",
        "unsubscribe and complaint rates: the data log none; the Gym's library values are kept",
        "send-time effect: no send-time variation in the data; hour sensitivity is set to 0 (no effect)",
        "offers: no offer variation in the data; an offer multiplies the lift by 1 and still costs its redemption cost",
        "sleeping dogs: not identifiable from a single send; none are planted",
        "customer-to-customer spread inside a group: the Gym's lognormal spread is kept",
    ),
    notes=(
        "groups: new (newbie), recent (existing, last bought within 3 months), lapsed (the rest)",
        "click-like signal: a site visit in the window; the control group's visit rate is the floor",
        "margin 0.4 and cost $0.001 per e-mail are the bench's conventions, not data",
    ),
)
"""A two-arm e-mail campaign (men's, women's, none): base purchase rate and lift per customer group,
the arms' difference, and how clicks relate to the purchases a message causes."""

REAL_SMS_FIT = CampaignFit(
    provenance="public randomised SMS campaign of a grocery loyalty programme (SMS or none, 50/50; 100,000 clients of the bench's fixed sample; outcome: bought in the window); fitted on the TRAIN half by client-id hash",
    window_days=14,
    n_train=49956,
    channel=Channel.WHATSAPP,
    arms=("sms",),
    groups=(
        GroupFit(
            name="light",
            n=15297,
            share=0.306209,
            control_rate_raw=0.340742,
            control_rate=0.340879,
            arm_lift_raw=(0.050913,),
            arm_lift_se=(0.00777902,),
            arm_lift=(0.0481742,),
        ),
        GroupFit(
            name="medium",
            n=17592,
            share=0.35215,
            control_rate_raw=0.599358,
            control_rate=0.599377,
            arm_lift_raw=(0.0421707,),
            arm_lift_se=(0.00731151,),
            arm_lift=(0.0407716,),
        ),
        GroupFit(
            name="heavy",
            n=17067,
            share=0.341641,
            control_rate_raw=0.837761,
            control_rate=0.837713,
            arm_lift_raw=(0.0136577,),
            arm_lift_se=(0.00554703,),
            arm_lift=(0.0149048,),
        ),
    ),
    clicks=ClickFit(
        logged=False,
        control_rate=0.0,
        treated_rate=0.0,
        floor=0.0,
        per_extra_purchase=0.0,
        per_extra_purchase_low=0.0,
        per_extra_purchase_high=0.0,
        caused_share_of_treated_clicks=0.0,
        click_purchase_phi=0.0,
    ),
    aov_mean=6.4906,
    margin_rate=0.25,
    channel_cost=0.0305344,
    fatigue_per_weekly_contact=0.25,
    not_fitted=(
        "contact fatigue: a single-send randomised campaign has no repeated contacts, so it is not identifiable; the Gym's library value is kept (fatigue_per_weekly_contact 0.25)",
        "unsubscribe and complaint rates: the data log none; the Gym's library values are kept",
        "send-time effect: no send-time variation in the data; hour sensitivity is set to 0 (no effect)",
        "offers: no offer variation in the data; an offer multiplies the lift by 1 and still costs its redemption cost",
        "sleeping dogs: not identifiable from a single send; none are planted",
        "customer-to-customer spread inside a group: the Gym's lognormal spread is kept",
        "the window length: the data do not state it; 14 days (the bench's proof window) is assumed",
    ),
    notes=(
        "groups: terciles of the number of receipts before the campaign (light, medium, heavy)",
        "the Gym's WhatsApp slot stands in for SMS (the world has no SMS content or history); e-mail consent is 0, so the only channel is the paid per-message one",
        "money in dollars at 65.5 RUB per USD (unit conversion only); order value is the mean receipt of the TRAIN clients; margin 0.25 and 2 RUB per SMS are the bench's conventions",
        "the SMS campaign logged no clicks: click floor 0, click slope 0",
    ),
)
"""A one-arm paid per-message campaign (SMS or none) on a grocery loyalty list: base rate and lift per
buying-frequency group; the campaign logged no clicks."""

REAL_EMAIL_LIKE = "real_email_like"
REAL_SMS_LIKE = "real_sms_like"

REAL_EMAIL_PERSONAS: dict[str, str] = {"new": "new", "recent": "loyal", "lapsed": "at_risk"}
"""Fitted group -> the Gym persona name it is planted under (the Gym's persona set is closed). New
customers are "new", customers who bought within a quarter "loyal", the rest "at_risk" (bought more
earlier in the history, falling to the fitted rate by day 0). Labels only: every behaviour is set from
the fit."""
REAL_SMS_PERSONAS: dict[str, str] = {"light": "price_sensitive", "medium": "researcher", "heavy": "loyal"}
"""As above for buying-frequency groups; arbitrary labels without side effects ("new" and "at_risk" are
avoided because they change the history)."""


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
        "history_sends_per_week": HISTORY_SENDS_PER_WEEK,
        "complaint_cost": COMPLAINT_COST,
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
            "sleeping_dogs_hidden",
            "As sleeping_dogs_15, but the dogs' annoyance began on day 0, so their history looks like "
            "anyone else's: they can only be found by contacting them.",
            sleeping_dog_share=0.15,
            sleeping_dog_effect=-0.5,
            sleeping_dog_signal=0.0,
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
        scenario_from_fit(
            REAL_EMAIL_FIT,
            name=REAL_EMAIL_LIKE,
            description=(
                "Fitted to a real randomised e-mail campaign (TRAIN half): a tiny base purchase rate, a "
                "small absolute lift per e-mail that differs a little between two message arms, "
                "clicks that mostly come from people the e-mail did not move, and no variation in "
                "send time or offers. Nothing here is a belief of ours."
            ),
            history_sends_per_week=HISTORY_SENDS_PER_WEEK,
            complaint_cost=COMPLAINT_COST,
            persona_labels=REAL_EMAIL_PERSONAS,
        ),
        scenario_from_fit(
            REAL_SMS_FIT,
            name=REAL_SMS_LIKE,
            description=(
                "Fitted to a real randomised SMS campaign (TRAIN half): a high base purchase rate, a "
                "small relative lift that shrinks as customers buy more often, a per-message cost, and "
                "no click data at all (the agent must learn from purchases). The Gym's WhatsApp slot "
                "stands in for SMS."
            ),
            history_sends_per_week=HISTORY_SENDS_PER_WEEK,
            complaint_cost=COMPLAINT_COST,
            persona_labels=REAL_SMS_PERSONAS,
        ),
        _base(
            "smoke",
            "A tiny, fast version of the plain shop for tests and quick checks.",
            n_customers=300,
            history_days=60,
            days=14,
        ),
        # Plan-v3 decision 2. Appended at the end of the registry; the numbers are planted beliefs.
        _base(
            "moments_planted",
            "Customers leave carts now and then and a third buy a regular item every month. A message "
            "sent in the day or two after a cart, or in the days before a refill, is worth up to three times "
            "as much as the same message at a quiet moment: the plain shop plus moments to find.",
            cart_rate_per_day=0.02,
            cart_lift_multiplier=3.0,
            cart_half_life_days=1.0,
            refill_share=0.33,
            refill_cycle_days=30.0,
            refill_lift_multiplier=3.0,
            refill_lead_days=3.0,
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
