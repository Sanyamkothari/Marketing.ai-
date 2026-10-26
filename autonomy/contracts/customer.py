"""Customer state: one row per customer, rebuilt by folding events, read by `decide()`.

`decide()` reads only this (plus the `DecisionContext`), never raw events, so the Gym and the real
world look identical to it.
"""

from __future__ import annotations

from pydantic import AwareDatetime, Field, field_validator

from autonomy.contracts.base import Channel, Contract, Group


class ChannelStats(Contract):
    address_known: bool = False
    consent: bool = False
    unsubscribed: bool = False
    sends: int = 0
    delivers: int = 0
    clicks: int = 0
    replies: int = 0
    bounces: int = 0
    complaints: int = 0
    last_send_at: AwareDatetime | None = None
    last_engaged_at: AwareDatetime | None = None

    @property
    def contactable(self) -> bool:
        return self.address_known and self.consent and not self.unsubscribed


class CustomerState(Contract):
    customer_id: str
    as_of: AwareDatetime
    version: int = Field(default=0, description="How many events have been folded in")
    group: Group = Group.AGENT
    timezone: str = "UTC"
    country: str | None = None
    first_seen_at: AwareDatetime | None = None
    last_seen_at: AwareDatetime | None = None
    last_purchase_at: AwareDatetime | None = None
    purchases: int = 0
    revenue: float = 0.0
    margin: float = 0.0
    top_categories: tuple[str, ...] = ()
    category_counts: dict[str, int] = Field(default_factory=dict, description="Purchases per category")
    channels: dict[Channel, ChannelStats] = Field(default_factory=dict)
    contacts_7d: int = 0
    contacts_30d: int = 0
    last_contact_at: AwareDatetime | None = None
    recent_contacts: tuple[AwareDatetime, ...] = Field(
        default=(), description="Send times in the last 30 days, oldest first; feeds contacts_7d/30d"
    )
    active_hours: tuple[float, ...] = Field(
        default=(), description="24 shares of engagement by local hour, summing to 1; empty if unknown"
    )
    hour_counts: tuple[int, ...] = Field(default=(), description="24 counts of engagement by local hour")
    offer_sensitivity: float | None = Field(
        default=None, description="0 = buys at full price, 1 = needs an offer"
    )
    preferences: dict[str, str] = Field(
        default_factory=dict, description="Stated or learned, e.g. no_discounts=true"
    )
    open_complaint: bool = False
    scores: dict[str, float] = Field(
        default_factory=dict, description="Model outputs, e.g. churn_risk, clv_90d"
    )
    segment: str | None = None
    features: dict[str, float] = Field(
        default_factory=dict,
        description="The nightly profile (plan-v3 foundation A): feature name -> value, as of features_as_of",
    )
    features_as_of: AwareDatetime | None = Field(
        default=None, description="The instant the profile was built for; no event after it was read"
    )

    @field_validator("active_hours")
    @classmethod
    def _hours(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if v and len(v) != 24:
            raise ValueError("active_hours has 24 entries or none")
        return v

    @property
    def avg_order_value(self) -> float:
        return self.revenue / self.purchases if self.purchases else 0.0

    def contactable_channels(self) -> tuple[Channel, ...]:
        return tuple(c for c, s in self.channels.items() if s.contactable)
