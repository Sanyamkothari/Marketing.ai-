"""The Gym's shared surface: scenario knobs and the planted-truth API.

Both halves of the Gym code against this file: the world (which plants the truth and simulates
customers) and the scoreboard (which runs policies and scores them against the truth). Change it by
addition only.
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


@runtime_checkable
class GroundTruth(Protocol):
    """What only the Gym knows. Never visible to the policy under test."""

    def persona(self, customer_id: str) -> str: ...

    def is_sleeping_dog(self, customer_id: str) -> bool: ...

    def expected_net(self, customer_id: str, action: Action, now: datetime) -> float:
        """True expected incremental margin minus offer and channel cost, vs doing nothing, given the
        customer's current fatigue. Negative for sleeping dogs and for wasted discounts."""
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
