"""The Gym's shared surface: scenario knobs and the planted-truth API.

Both halves of the Gym code against this file: the world (which plants the truth and simulates
customers) and the scoreboard (which runs policies and scores them against the truth). Change it by
addition only.

Realism knobs added in wave 3a, each with the real-world behaviour it stands for (the mechanics are in
`autonomy.gym.truth` and `autonomy.gym.world`):

* `lost_contact_value`, `lost_contact_horizon_days`, `lost_contact_sends_per_week`, `complaint_cost`:
  an unsubscribe is not free. A shop that loses someone's email address loses the margin its future
  messages to them would have made, and every spam complaint hurts inbox placement for the whole list.
  The truth charges both in `expected_net` and the world puts the realised amount on UNSUBSCRIBE and
  COMPLAINT events (`attrs["lost_value"]`). The lost contact value is on by default; the complaint
  cost is 0 on a bare spec and $5 in every library scenario.
* `sleeping_dog_signal`, `sleeping_dog_unsubscribe_multiplier`: people annoyed by marketing do not
  look like everyone else for ever. Their annoyance shows in what a CRM already holds: they ignore
  messages, come in through direct visits, and now and then unsubscribe from a channel. The signal
  sets how long before day 0 the annoyance began, so how much of the history shows it (0 = none: the
  old coin flip, invisible before the first contact).
* `click_floor`, `click_per_extra_purchase`: how clicks relate to the purchases a message causes.
  Added in the real-calibration task with the Gym's original values as defaults (0.02 and 3.0), so
  every earlier scenario is unchanged; the real-calibrated scenarios set them from data.
* `history_sends_per_week`: a real client hands over months of past campaigns with their clicks and
  purchases, so an agent can learn before day 1. The past campaigns follow a documented, randomised
  logging policy and every past send carries `attrs["logged_probability"]`. The bare spec keeps the
  old history (0: no past marketing); every library scenario turns it on.

Moments (plan-v3 decision 2), all off by default so every earlier scenario is unchanged:

* `cart_*`: customers leave carts now and then (the world emits CART events), and a message sent in
  the days after a cart is worth more: its lift is multiplied by `1 + (cart_lift_multiplier - 1)`
  at the cart, halving every `cart_half_life_days`: someone who has just put something in the basket
  is close to buying.
* `refill_*`: a share of customers buy a regular item on a cycle (the world emits those purchases,
  with an item id), and a message sent from `refill_lead_days` before one is due to
  `refill_late_days` after it has its lift multiplied by `refill_lift_multiplier`: the reminder
  lands when the stock is running out.

The numbers are planted beliefs of ours, like every other knob here; nothing is fitted to data.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import Field

from autonomy.contracts import Action, Channel, Contract

PERSONAS: tuple[str, ...] = ("researcher", "price_sensitive", "loyal", "new", "at_risk")


class ScenarioSpec(Contract):
    """Every knob of a simulated business. Defaults describe a plain mid-size online shop."""

    name: str
    description: str = ""
    seed: int = 0
    n_customers: int = Field(default=5000, ge=10)
    history_days: int = Field(default=180, ge=0)
    days: int = Field(default=90, ge=1, description="Decision days after the history")
    persona_mix: dict[str, float] = Field(
        default_factory=lambda: {
            "researcher": 0.2,
            "price_sensitive": 0.25,
            "loyal": 0.15,
            "new": 0.2,
            "at_risk": 0.2,
        }
    )
    daily_purchase_rate: dict[str, float] = Field(
        default_factory=lambda: {
            "researcher": 0.006,
            "price_sensitive": 0.004,
            "loyal": 0.02,
            "new": 0.008,
            "at_risk": 0.002,
        }
    )
    aov_mean: float = 60.0
    margin_rate: float = 0.4
    channel_consent: dict[Channel, float] = Field(
        default_factory=lambda: {Channel.EMAIL: 0.9, Channel.WHATSAPP: 0.5}
    )
    channel_cost: dict[Channel, float] = Field(
        default_factory=lambda: {Channel.EMAIL: 0.001, Channel.WHATSAPP: 0.03}
    )
    channel_uplift: dict[str, dict[Channel, float]] = Field(
        default_factory=dict,
        description="persona -> channel -> added daily purchase probability while the effect lasts",
    )
    offer_uplift: dict[str, dict[str, float]] = Field(
        default_factory=dict, description="persona -> offer kind -> multiplier on the channel uplift"
    )
    theme_uplift: dict[str, dict[str, float]] = Field(
        default_factory=dict, description="persona -> theme -> multiplier on the channel uplift"
    )
    effect_days: int = 7
    sleeping_dog_share: float = Field(
        default=0.0, ge=0, le=1, description="Share of customers a contact puts off"
    )
    sleeping_dog_effect: float = Field(
        default=-0.5, description="Multiplier on their own purchase rate after contact"
    )
    discount_redeemed_by_sure_things: bool = Field(
        default=True,
        description="Customers who would buy anyway still use a discount if offered (cannibalisation)",
    )
    fatigue_per_weekly_contact: float = Field(
        default=0.25, ge=0, description="Uplift shrinks by this share per contact in 7 days"
    )
    unsubscribe_base: float = 0.002
    unsubscribe_per_weekly_contact: float = 0.004
    complaint_share_of_unsubscribes: float = 0.1
    best_hour: dict[str, int] = Field(
        default_factory=lambda: {
            "researcher": 20,
            "price_sensitive": 12,
            "loyal": 9,
            "new": 18,
            "at_risk": 19,
        }
    )
    hour_sensitivity: float = Field(
        default=0.3, ge=0, le=1, description="Uplift lost when sent 12 hours from the best hour"
    )
    timezones: dict[str, float] = Field(default_factory=lambda: {"UTC": 1.0})
    drift_day: int | None = Field(default=None, description="Decision day on which the world changes, if any")
    drift_purchase_multiplier: float = 1.0
    lost_contact_value: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Margin lost when a customer unsubscribes from a channel. None: each customer's own value, "
            "the margin `lost_contact_sends_per_week` plain messages a week on that channel would make "
            "over `lost_contact_horizon_days` at their planted uplift (0 for sleeping dogs)"
        ),
    )
    lost_contact_horizon_days: int = Field(
        default=180, ge=0, description="How far ahead the lost value of an unsubscribe looks"
    )
    lost_contact_sends_per_week: float = Field(
        default=1.0, ge=0, description="The message rhythm the lost value assumes (a weekly newsletter)"
    )
    complaint_cost: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Deliverability cost of one spam complaint, on top of the lost contact value (the library "
            "uses $5; 0 here keeps a bare spec's sleeping dogs costing only their dent)"
        ),
    )
    sleeping_dog_signal: float = Field(
        default=0.5,
        ge=0,
        le=1,
        description=(
            "How visible sleeping dogs are in the history: 0 = not at all (a coin flip), 1 = annoyed "
            "for the whole history (ignore messages, more direct visits, more past unsubscribes)"
        ),
    )
    sleeping_dog_unsubscribe_multiplier: float = Field(
        default=3.0, ge=0, description="Sleeping dogs unsubscribe this many times as often once annoyed"
    )
    click_floor: float = Field(
        default=0.02,
        ge=0,
        le=1,
        description=(
            "Chance a delivered message is clicked by someone it does not move (the click rate of the "
            "channel with no effect at all). The default is the Gym's original 0.02; a real-calibrated "
            "scenario sets it from a control group's click-like rate"
        ),
    )
    click_per_extra_purchase: float = Field(
        default=3.0,
        ge=0,
        description=(
            "Click chance added per expected extra purchase a contact causes (the link between clicks "
            "and money). The default is the Gym's original 3.0; 0 means clicks say nothing about "
            "purchases (a channel that logs no clicks)"
        ),
    )
    history_sends_per_week: float = Field(
        default=0.0,
        ge=0,
        le=1,
        description=(
            "Past marketing in the history: the weekly campaign's base inclusion probability for a "
            "consented customer (0 = no past campaigns; the library uses 0.4)"
        ),
    )
    cart_rate_per_day: float = Field(
        default=0.0, ge=0, le=1, description="Chance per day that a customer leaves a cart (0: no carts)"
    )
    cart_lift_multiplier: float = Field(
        default=1.0, ge=1, description="A message's lift is this many times as big the moment a cart is left"
    )
    cart_half_life_days: float = Field(
        default=1.0, gt=0, description="The extra lift after a cart halves this often"
    )
    refill_share: float = Field(
        default=0.0, ge=0, le=1, description="Share of customers who buy a regular item on a cycle"
    )
    refill_cycle_days: float = Field(
        default=30.0, gt=0, description="Days between those purchases (each customer's own is within 15%)"
    )
    refill_lift_multiplier: float = Field(
        default=1.0, ge=1, description="A message's lift is this many times as big while a refill is due"
    )
    refill_lead_days: float = Field(default=2.0, ge=0, description="The refill window opens this long before")
    refill_late_days: float = Field(
        default=4.0, ge=0, description="... and closes this long after the due day"
    )


@runtime_checkable
class GroundTruth(Protocol):
    """What only the Gym knows. Never visible to the policy under test."""

    def persona(self, customer_id: str) -> str: ...

    def is_sleeping_dog(self, customer_id: str) -> bool: ...

    def expected_net(self, customer_id: str, action: Action, now: datetime) -> float:
        """True expected incremental margin minus offer and channel cost, vs doing nothing, given the
        customer's current fatigue, minus the expected cost of the unsubscribes and complaints the
        contact causes. Negative for sleeping dogs and for wasted discounts."""
        ...

    def best_action(self, customer_id: str, candidates: Sequence[Action], now: datetime) -> Action | None:
        """The oracle's pick among candidates, or None if nothing beats doing nothing."""
        ...


@runtime_checkable
class GymEnvironment(Protocol):
    """An `autonomy.interfaces.Environment` that also exposes its truth and any rule breaks seen."""

    spec: ScenarioSpec
    truth: GroundTruth

    def violations(self) -> list[str]:
        """Plain sentences for every contact that broke a rule: no consent, unsubscribed, quiet hours."""
        ...
