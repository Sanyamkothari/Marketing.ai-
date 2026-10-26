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


class ItemViews(Contract):
    """How often one product was looked at, and when last (see `CustomerState.viewed_items`)."""

    count: int = Field(default=1, ge=1)
    last_at: AwareDatetime


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

    # Moments (plan-v3 decision 2): what the customer did lately that makes a message worth more.
    last_view_at: AwareDatetime | None = Field(default=None, description="Their latest product view")
    last_cart_at: AwareDatetime | None = Field(
        default=None, description="Their latest add-to-cart; a later purchase closes the cart"
    )
    viewed_items: dict[str, ItemViews] = Field(
        default_factory=dict,
        description="Products viewed but not bought since: item id -> views and latest view; at most 8, "
        "the least recently viewed dropped first",
    )
    usual_gap_days: float | None = Field(
        default=None,
        description="Their usual days away between two days of activity (a running average of gaps "
        "of a day or more)",
    )
    purchase_gap_days: float | None = Field(
        default=None, description="Their usual days between two purchases (a running average)"
    )
    last_return_at: AwareDatetime | None = Field(
        default=None, description="Their latest visit, view or cart after a gap much longer than usual"
    )
    last_return_gap_days: float | None = Field(
        default=None, description="How long the gap before last_return_at was, in days"
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
