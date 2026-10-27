"""Goals, briefs and envelopes: what the marketer wants and the limits the agent works within.

A `Goal` is the marketer's request in plain words. The Strategist answers with a `Brief`, which
holds an `Envelope`: the box the agent may act in without asking (channels, offers, caps, budget,
quiet hours, stop rules). The marketer approves the brief once, then chooses the autonomy level.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from autonomy.contracts.base import Channel, Contract


class Objective(StrEnum):
    CONVERSION = "conversion"  # did they buy at all
    REVENUE = "revenue"
    GROSS_PROFIT = "gross_profit"  # the default
    RETENTION = "retention"
    LEAD = "lead"
    ACTIVATION = "activation"


class GoalKind(StrEnum):
    WIN_BACK = "win_back"
    REPEAT_PURCHASE = "repeat_purchase"
    CART_RECOVERY = "cart_recovery"
    CROSS_SELL = "cross_sell"
    ONBOARDING = "onboarding"
    RETENTION = "retention"
    CUSTOM = "custom"


class AutonomyLevel(StrEnum):
    SHADOW = "shadow"  # decide and log only, send nothing
    APPROVE = "approve"  # a person approves each batch
    AUTOPILOT = "autopilot"  # act inside the envelope; a person can pause any time


class GoalStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"


class Goal(Contract):
    goal_id: str
    business_id: str
    text: str = Field(description="The marketer's words, e.g. 'win back lapsed customers, $5k this month'")
    kind: GoalKind = GoalKind.CUSTOM
    objective: Objective = Objective.GROSS_PROFIT
    budget: float = Field(ge=0, description="Total spend allowed on offers and messages, in `currency`")
    currency: str = "USD"
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    autonomy: AutonomyLevel = AutonomyLevel.SHADOW
    status: GoalStatus = GoalStatus.DRAFT
    created_at: AwareDatetime


class OfferKind(StrEnum):
    NONE = "none"
    PERCENT_OFF = "percent_off"
    AMOUNT_OFF = "amount_off"
    FREE_SHIPPING = "free_shipping"
    GIFT = "gift"
    LOYALTY = "loyalty"
    CONTENT = "content"  # useful information, no incentive
    BUNDLE = "bundle"  # a few items bought together at a discount (items from the co-purchase model)
    EARLY_ACCESS = "early_access"  # first look at a launch or sale; costs a configured amount


class OfferOption(Contract):
    offer_id: str
    label: str
    kind: OfferKind
    amount: float = Field(default=0.0, ge=0, description="Percent for percent_off, money for amount_off")
    cost_per_redemption: float = Field(default=0.0, ge=0, description="Expected cost to the business if used")
    needs_approval: bool = False
    # Cost-model inputs for the v3 offer kinds (autonomy.mandate.catalogue.offer_cost). All default to
    # "not set"; an explicit `cost_per_redemption` above always wins over what they would derive.
    # A field still at its default is left out of every dump, so an offer that does not use them
    # serialises (and hashes: catalogue versions, stored briefs) exactly as it did in v2.
    shipping_cost: float = Field(
        default=0.0,
        ge=0,
        description="free_shipping: what shipping one order costs us",
        exclude_if=lambda v: v == 0.0,
    )
    bundle_item_ids: tuple[str, ...] = Field(
        default=(), description="bundle: the items sold together", exclude_if=lambda v: not v
    )
    bundle_discount: float = Field(
        default=0.0, ge=0, description="bundle: money off the bundle", exclude_if=lambda v: v == 0.0
    )
    points: float = Field(
        default=0.0, ge=0, description="loyalty: points granted per redemption", exclude_if=lambda v: v == 0.0
    )
    point_value: float = Field(
        default=0.0,
        ge=0,
        description="loyalty: what one point is worth in money",
        exclude_if=lambda v: v == 0.0,
    )
    redemption_rate: float = Field(
        default=1.0,
        ge=0,
        le=1,
        description="loyalty: share of granted points that are ever redeemed",
        exclude_if=lambda v: v == 1.0,
    )

    # How a purchase's discount code is traced back to this offer (autonomy.contracts.offer_codes).
    # The shop's checkout reports the typed code ("COMEBACK10"), never the agent's offer id
    # ("pct10"), so the offer names the code(s) or code prefix(es) that identify it. Empty by default
    # and then left out of every dump, so an offer that does not use them hashes as before.
    codes: tuple[str, ...] = Field(
        default=(),
        description="Exact discount codes (any case) that redeem this offer, e.g. COMEBACK10",
        exclude_if=lambda v: not v,
    )
    code_prefixes: tuple[str, ...] = Field(
        default=(),
        description="Prefixes (any case) of per-send unique discount codes that redeem this offer",
        exclude_if=lambda v: not v,
    )

    @field_validator("codes", "code_prefixes")
    @classmethod
    def _clean_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(c.strip() for c in value))
        if any(not c for c in cleaned):
            raise ValueError("a discount code or prefix cannot be empty")
        # The connector comma-joins an order's codes and "none" means no offer, so neither could match.
        if any("," in c for c in cleaned):
            raise ValueError("a discount code or prefix cannot contain a comma")
        if any(c.casefold() == "none" for c in cleaned):
            raise ValueError("'none' is not a discount code: it means no offer")
        return cleaned

    @model_validator(mode="after")
    def _price_from_cost_model(self) -> OfferOption:
        # An offer priced only through the v3 fields states its cost in `cost_per_redemption` too,
        # so every reader of that field (the budget ledger, the margin floor, the v2 bidder) sees
        # it. v2 offers leave these fields at their defaults and are unchanged.
        if self.cost_per_redemption == 0:
            derived = self.model_cost()
            if derived > 0:
                object.__setattr__(self, "cost_per_redemption", derived)
                self.__pydantic_fields_set__.add("cost_per_redemption")
        return self

    def model_cost(self) -> float:
        """The cost of one redemption from the v3 cost-model fields alone: free shipping's
        `shipping_cost`, a bundle's `bundle_discount`, loyalty's `points x point_value x
        redemption_rate`; 0 for every other kind (they are priced by `cost_per_redemption`)."""
        if self.kind is OfferKind.FREE_SHIPPING:
            return self.shipping_cost
        if self.kind is OfferKind.BUNDLE:
            return self.bundle_discount
        if self.kind is OfferKind.LOYALTY:
            return self.points * self.point_value * self.redemption_rate
        return 0.0


NO_OFFER = OfferOption(offer_id="none", label="No offer", kind=OfferKind.NONE)


class StopMetric(StrEnum):
    COMPLAINT_RATE = "complaint_rate"  # share of deliveries, e.g. 0.0008
    UNSUBSCRIBE_MULTIPLE = "unsubscribe_multiple"  # vs the account's own baseline, e.g. 3.0
    DELIVERY_RATE_BELOW = "delivery_rate_below"  # e.g. 0.95
    SPEND_PACE = "spend_pace"  # spend / planned spend to date, e.g. 1.2
    WHATSAPP_QUALITY = "whatsapp_quality"  # 1 = yellow, 2 = red


class StopRule(Contract):
    metric: StopMetric
    threshold: float
    action: Literal["pause_goal", "pause_channel"] = "pause_goal"


DEFAULT_STOP_RULES: tuple[StopRule, ...] = (
    StopRule(metric=StopMetric.COMPLAINT_RATE, threshold=0.0008),
    StopRule(metric=StopMetric.UNSUBSCRIBE_MULTIPLE, threshold=3.0),
    StopRule(metric=StopMetric.DELIVERY_RATE_BELOW, threshold=0.95, action="pause_channel"),
    StopRule(metric=StopMetric.SPEND_PACE, threshold=1.2),
    StopRule(metric=StopMetric.WHATSAPP_QUALITY, threshold=1, action="pause_channel"),
)


class Envelope(Contract):
    channels: tuple[Channel, ...]
    offers: tuple[OfferOption, ...] = (NO_OFFER,)
    budget: float = Field(ge=0)
    max_daily_spend: float | None = None
    max_contacts_per_week: int = Field(default=2, ge=0, description="Hard cap per customer across all goals")
    quiet_start_hour: int = Field(default=21, ge=0, le=23, description="Local time; no sends from here...")
    quiet_end_hour: int = Field(default=9, ge=0, le=23, description="...until here")
    exploration_floor: float = Field(default=0.05, ge=0, le=0.5)
    holdout_share: float = Field(default=0.10, ge=0, le=0.5)
    baseline_share: float = Field(default=0.10, ge=0, le=0.5)
    stop_rules: tuple[StopRule, ...] = DEFAULT_STOP_RULES

    @model_validator(mode="after")
    def _sane(self) -> Envelope:
        if not self.channels:
            raise ValueError("an envelope needs at least one channel")
        if self.holdout_share + self.baseline_share >= 1:
            raise ValueError("holdout + baseline must leave room for the agent")
        clash = _code_clash(self.offers)
        if clash is not None:
            raise ValueError(clash)
        return self


def _code_clash(offers: tuple[OfferOption, ...]) -> str | None:
    """Why the offers' discount codes cannot be read one way, or None when they can.

    A code (any case) or a prefix named by two offers, or a code that is another offer's id, would
    send a redemption to whichever offer the code table read first (`autonomy.contracts.offer_codes`)."""
    ids = {o.offer_id.casefold(): o.offer_id for o in offers}
    codes: dict[str, str] = {}
    prefixes: dict[str, str] = {}
    for o in offers:
        for code in o.codes:
            folded = code.casefold()
            owner = codes.setdefault(folded, o.offer_id)
            if owner != o.offer_id:
                return f"discount code {code!r} is named by offers {owner!r} and {o.offer_id!r}"
            other = ids.get(folded)
            if other is not None and other != o.offer_id:
                return f"discount code {code!r} of offer {o.offer_id!r} is the id of offer {other!r}"
        for prefix in o.code_prefixes:
            owner = prefixes.setdefault(prefix.casefold(), o.offer_id)
            if owner != o.offer_id:
                return f"discount code prefix {prefix!r} is named by offers {owner!r} and {o.offer_id!r}"
    return None


FilterOp = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "is_null",
    "not_null",
    "days_since_gt",
    "days_since_lt",
]


class AudienceFilter(Contract):
    """A test on one `CustomerState` field. `days_since_*` compare a datetime field with now."""

    field: str
    op: FilterOp
    value: str | float | int | bool | tuple[str, ...] | None = None


class Audience(Contract):
    description: str
    filters: tuple[AudienceFilter, ...] = ()


class FollowUpStep(Contract):
    after_days: int = Field(ge=1)
    when: Literal["no_response", "no_purchase", "always"] = "no_purchase"
    channel: Channel | None = Field(default=None, description="None: let decide() pick")


class Brief(Contract):
    brief_id: str
    goal_id: str
    version: int = 1
    objective: Objective
    audience: Audience
    horizon_days: int = Field(default=14, ge=1, le=180, description="Outcome window after a contact")
    envelope: Envelope
    follow_ups: tuple[FollowUpStep, ...] = ()
    rationale: str = Field(default="", description="The Strategist's plain-English reasoning")
    created_by: str = "strategist"
    created_at: AwareDatetime
    approved_by: str | None = None
    approved_at: AwareDatetime | None = None

    @property
    def approved(self) -> bool:
        return self.approved_at is not None
