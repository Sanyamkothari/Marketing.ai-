"""Enforcement: check one proposed action against a mandate, and pace spend under its daily cap.

`check_action` is the gate an autonomous action has to pass. It is pure maths, no LLM, with time
passed in, and it returns *every* violation rather than the first, so a person reviewing a blocked
action sees all the reasons at once. The codes are a small fixed vocabulary (the constants below),
each prefixed `mandate_` so that, once they land in `Decision.blocked_by`, nobody confuses them with
the decide rules' own codes.

`mandate_to_envelope` turns a mandate into the `Envelope` the decide rules already read, so the
existing per-bid rules keep working unchanged while the mandate becomes the source of the limits.
The envelope is a coarse, never-looser projection (it has no margin floor, template list, cohort
exclusions or exact frequency window); `check_action` is the exact check.

`TokenBucket` paces spend so that no rolling 24 hours ever spends more than `max_daily_spend`.

How decide and the runtime will use these (integration happens in their streams):

* the Strategist (or the runtime) builds each brief's envelope with `mandate_to_envelope`;
* `decide()` calls `check_bid` on each candidate bid after its own hard rules and adds the codes
  to `blocked_by`;
* the dispatcher calls `check_action` once more right before sending, with the spend so far, and
  draws the action's cost from the mandate's `TokenBucket`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, field_validator, model_validator

from autonomy.contracts import (
    NO_OFFER,
    Action,
    Bid,
    Contract,
    CustomerState,
    Envelope,
    OfferOption,
)
from autonomy.mandate.models import Mandate, MandateStatus, to_utc

__all__ = [
    "ALL_CODES",
    "BUDGET_CEILING",
    "CHANNEL_NOT_ALLOWED",
    "COHORT_EXCLUDED",
    "DAILY_SPEND",
    "FREQUENCY_CAP",
    "GOAL_NOT_COVERED",
    "MARGIN_FLOOR",
    "NOT_ACTIVE",
    "OFFER_NOT_APPROVED",
    "OUTSIDE_VALIDITY",
    "QUIET_HOURS",
    "TEMPLATE_NOT_APPROVED",
    "ActionCosts",
    "SpendToDate",
    "TokenBucket",
    "check_action",
    "check_action_against_mandate",
    "check_bid",
    "costs_from_bid",
    "customer_zone",
    "mandate_to_envelope",
    "touches_in_window",
]

NOT_ACTIVE = "mandate_not_active"
OUTSIDE_VALIDITY = "mandate_outside_validity"
GOAL_NOT_COVERED = "mandate_goal_not_covered"
CHANNEL_NOT_ALLOWED = "mandate_channel_not_allowed"
TEMPLATE_NOT_APPROVED = "mandate_template_not_approved"
OFFER_NOT_APPROVED = "mandate_offer_not_approved"
QUIET_HOURS = "mandate_quiet_hours"
FREQUENCY_CAP = "mandate_frequency_cap"
MARGIN_FLOOR = "mandate_margin_floor"
BUDGET_CEILING = "mandate_budget_ceiling"
DAILY_SPEND = "mandate_daily_spend"
COHORT_EXCLUDED = "mandate_cohort_excluded"

ALL_CODES: tuple[str, ...] = (
    NOT_ACTIVE,
    OUTSIDE_VALIDITY,
    GOAL_NOT_COVERED,
    CHANNEL_NOT_ALLOWED,
    TEMPLATE_NOT_APPROVED,
    OFFER_NOT_APPROVED,
    QUIET_HOURS,
    FREQUENCY_CAP,
    MARGIN_FLOOR,
    BUDGET_CEILING,
    DAILY_SPEND,
    COHORT_EXCLUDED,
)
"""Every code `check_action` can return, in the order it reports them."""

_EPS = 1e-9


class ActionCosts(Contract):
    """What one action costs and is expected to earn, in the mandate's currency.

    `revenue_if_converted` and `margin_if_converted` describe the order the action hopes for:
    revenue is what the customer pays before the offer, margin is gross margin before the offer.
    Leave them `None` when unknown (see `check_action` for how that is treated).

    `offer_cost` is what the action commits against the budget. A decide `Bid` carries the
    *expected* offer cost (cost per redemption times the chance of redemption), which is right for
    spend but far too small for the margin: a customer who converts redeems the offer, so the margin
    of that order loses the full cost per redemption. `offer_cost_if_converted` is that full cost;
    `None` means it equals `offer_cost`.
    """

    offer_cost: float = Field(default=0.0, ge=0)
    channel_cost: float = Field(default=0.0, ge=0)
    revenue_if_converted: float | None = Field(default=None, ge=0)
    margin_if_converted: float | None = None
    offer_cost_if_converted: float | None = Field(default=None, ge=0)

    @property
    def spend(self) -> float:
        return self.offer_cost + self.channel_cost

    @property
    def conversion_offer_cost(self) -> float:
        """The offer's cost on an order that converts: what the margin floor is checked against."""
        return self.offer_cost if self.offer_cost_if_converted is None else self.offer_cost_if_converted


class SpendToDate(Contract):
    """Money already committed under this mandate: in total, and on the action's own UTC day."""

    total: float = Field(default=0.0, ge=0)
    today: float = Field(default=0.0, ge=0)


def costs_from_bid(
    bid: Bid,
    *,
    revenue_if_converted: float | None = None,
    margin_if_converted: float | None = None,
    offer_cost_if_converted: float | None = None,
) -> ActionCosts:
    """A bid's costs. `Bid.value_if_converted` is the expected margin, so it is the default margin;
    revenue is not on the bid, so the caller passes it (the customer's average order value is a
    sensible default). `Bid.offer_cost` is the expected offer cost, so the caller also passes the
    offer's cost per redemption as `offer_cost_if_converted` for the margin check (see
    `ActionCosts`); `check_bid` does this from the offer descriptions."""
    return ActionCosts(
        offer_cost=bid.offer_cost,
        channel_cost=bid.channel_cost,
        revenue_if_converted=revenue_if_converted,
        margin_if_converted=bid.value_if_converted if margin_if_converted is None else margin_if_converted,
        offer_cost_if_converted=offer_cost_if_converted,
    )


def customer_zone(state: CustomerState, default: str) -> ZoneInfo:
    """The customer's own time zone; the mandate's default when theirs is blank or not a real zone."""
    name = state.timezone.strip()
    if name:
        try:
            return ZoneInfo(name)
        except (KeyError, ValueError):
            pass
    return ZoneInfo(default)


def touches_in_window(state: CustomerState, at: datetime, window_days: int) -> int:
    """How many messages the customer had in the `window_days` days up to `at`.

    Counted from `recent_contacts` (send times in the last 30 days). Customer state also keeps
    `contacts_7d` and `contacts_30d`. When those counts are higher than the send-time list explains
    (a state whose list was not filled, or was trimmed), the unexplained sends could have happened
    at any moment of their span, so they are all counted whenever the window overlaps that span. A
    short window therefore can never pass a customer just because their send times are missing.
    """
    since = at - timedelta(days=window_days)
    count = sum(1 for t in state.recent_contacts if since < t <= at)
    as_of = state.as_of
    unexplained = 0
    for span_days, reported in ((7, state.contacts_7d), (30, state.contacts_30d)):
        span_start = as_of - timedelta(days=span_days)
        listed = sum(1 for t in state.recent_contacts if span_start < t <= as_of)
        missing = max(0, reported - listed - unexplained)
        if missing and since < as_of and at > span_start:
            count += missing
        unexplained += missing
    return count


def _excluded(state: CustomerState, mandate: Mandate, now: datetime) -> bool:
    if not mandate.cohort_exclusions:
        return False
    # Imported here, not at the top: decide will import this module when it integrates the
    # mandate, and a top-level import of the decide package from here would then be circular.
    from autonomy.decide.audience import passes

    return any(passes(state, f, now) for f in mandate.cohort_exclusions)


def _margin_ok(mandate: Mandate, costs: ActionCosts) -> bool:
    """Expected margin after the offer's cost, over revenue, must be at or above the floor.

    Unknown revenue or margin: an action with no offer cost passes (it cannot push the margin below
    what the business already earns at full price), and one with an offer cost fails, because we
    cannot show it keeps the floor.
    """
    revenue, margin = costs.revenue_if_converted, costs.margin_if_converted
    offer_cost = costs.conversion_offer_cost
    if revenue is None or margin is None or revenue <= 0:
        return offer_cost <= 0 and costs.offer_cost <= 0
    return (margin - offer_cost) / revenue >= mandate.margin_floor_pct - _EPS


def check_action(
    mandate: Mandate,
    action: Action,
    costs: ActionCosts | Bid,
    state: CustomerState,
    now: datetime,
    *,
    spend: SpendToDate | None = None,
    pending_touches: int = 0,
    revenue_if_converted: float | None = None,
) -> list[str]:
    """Every way this action would break the mandate, as codes; empty means it may go ahead.

    * `action.send_at` is the moment checked for quiet hours, validity and the frequency window.
    * `costs` is an `ActionCosts`, or a `Bid` (then `revenue_if_converted` feeds the margin check).
    * `spend` is what has been committed so far; `None` means nothing yet.
    * `pending_touches` counts messages already scheduled for this customer but not yet in state.
    """
    c = (
        costs
        if isinstance(costs, ActionCosts)
        else costs_from_bid(costs, revenue_if_converted=revenue_if_converted)
    )
    sp = spend or SpendToDate()
    at = to_utc(action.send_at)
    now_utc = to_utc(now)
    codes: list[str] = []

    if mandate.effective_status(now_utc) is not MandateStatus.ACTIVE:
        codes.append(NOT_ACTIVE)
    if not (mandate.is_valid_at(now_utc) and mandate.is_valid_at(at)):
        codes.append(OUTSIDE_VALIDITY)
    if action.goal_id not in mandate.goal_ids:
        codes.append(GOAL_NOT_COVERED)
    if action.channel not in mandate.channels:
        codes.append(CHANNEL_NOT_ALLOWED)
    if action.content_id is None or action.content_id not in mandate.approved_template_ids:
        codes.append(TEMPLATE_NOT_APPROVED)
    if action.offer_id != NO_OFFER.offer_id and action.offer_id not in mandate.approved_offer_ids:
        codes.append(OFFER_NOT_APPROVED)
    local_hour = at.astimezone(customer_zone(state, mandate.quiet_hours.timezone)).hour
    if mandate.quiet_hours.is_quiet(local_hour):
        codes.append(QUIET_HOURS)
    cap = mandate.frequency_cap
    if touches_in_window(state, at, cap.window_days) + pending_touches + 1 > cap.max_touches:
        codes.append(FREQUENCY_CAP)
    if not _margin_ok(mandate, c):
        codes.append(MARGIN_FLOOR)
    if sp.total + c.spend > mandate.budget_ceiling + _EPS:
        codes.append(BUDGET_CEILING)
    if mandate.max_daily_spend is not None and sp.today + c.spend > mandate.max_daily_spend + _EPS:
        codes.append(DAILY_SPEND)
    if _excluded(state, mandate, now_utc):
        codes.append(COHORT_EXCLUDED)
    return codes


check_action_against_mandate = check_action
"""The name the decide/runtime integration plan uses; the same function."""


def check_bid(
    mandate: Mandate,
    bid: Bid,
    state: CustomerState,
    now: datetime,
    *,
    offers: Sequence[OfferOption] = (),
    spend: SpendToDate | None = None,
    pending_touches: int = 0,
) -> list[str]:
    """`check_action` for a decide bid, with the customer's average order value as the revenue.

    `offers` describes the offers by id (the brief envelope's `offers` is the natural source). The
    bid's own `offer_cost` is the expected cost over everyone who receives the message, which counts
    against spend; the margin floor is checked with the offer's full `cost_per_redemption`, because
    an order that converts redeems it. A bid whose offer is not described here cannot show it keeps
    the margin floor, so it is reported as `mandate_margin_floor`.
    """
    revenue = state.avg_order_value if state.purchases else None
    offer_id = bid.action.offer_id
    undescribed = False
    per_redemption: float | None = None
    if offer_id != NO_OFFER.offer_id:
        option = next((o for o in offers if o.offer_id == offer_id), None)
        if option is None:
            undescribed = True
        else:
            per_redemption = max(option.cost_per_redemption, bid.offer_cost)
    codes = check_action(
        mandate,
        bid.action,
        costs_from_bid(bid, revenue_if_converted=revenue, offer_cost_if_converted=per_redemption),
        state,
        now,
        spend=spend,
        pending_touches=pending_touches,
    )
    if undescribed and MARGIN_FLOOR not in codes:
        codes.append(MARGIN_FLOOR)
        codes.sort(key=ALL_CODES.index)
    return codes


# --- the envelope projection ------------------------------------------------------------------------------


def _weekly_cap(mandate: Mandate) -> int:
    """A per-week cap no looser than the mandate in any 7 days.

    A window of a week or more allows at most `max_touches` in any week, so that is the cap. A
    shorter window fits whole into a week `7 // window_days` times, so the cap is that multiple:
    the mandate allows at least that many in every week.
    """
    cap = mandate.frequency_cap
    if cap.window_days >= 7:
        return cap.max_touches
    return cap.max_touches * (7 // cap.window_days)


def _quiet_hours_set(start: int, end: int) -> frozenset[int]:
    """The local hours a [start, end) quiet window covers; it may wrap midnight, equal means none."""
    if start == end:
        return frozenset()
    if start < end:
        return frozenset(range(start, end))
    return frozenset((*range(start, 24), *range(0, end)))


def _quiet_union(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int]:
    """One quiet window (start, end) that covers every quiet hour of both `a` and `b`.

    An envelope holds a single window, so when the two do not touch, the result is the smallest
    single window covering both: everything except the longest run of hours neither keeps quiet.
    That can be quieter than either alone, never louder. If together they cover the whole day,
    no single window can say so (equal start and end means *no* quiet hours), so that is an error.
    """
    quiet = _quiet_hours_set(*a) | _quiet_hours_set(*b)
    if not quiet:
        return a
    if len(quiet) == 24:
        raise ValueError("the mandate's and the base's quiet hours together leave no hour to send")
    best_start, best_len = 0, 0
    for start in range(24):
        if start in quiet or (start - 1) % 24 not in quiet:
            continue  # only begin a run of open hours right after a quiet hour
        length = 0
        while (start + length) % 24 not in quiet:
            length += 1
        if length > best_len:
            best_start, best_len = start, length
    # Quiet from the end of the longest open run up to its start.
    return ((best_start + best_len) % 24, best_start)


def mandate_to_envelope(
    mandate: Mandate,
    offers: Sequence[OfferOption] = (),
    *,
    base: Envelope | None = None,
    spent: float = 0.0,
) -> Envelope:
    """The `Envelope` the decide rules read, built from the mandate's limits.

    * `offers` describes the offers by id; only the mandate's approved ones are kept, and "no
      offer" is always there. An approved id with no description is left out (stricter, not looser).
    * `base` (typically the brief's own envelope, approved for one goal) supplies what a mandate
      does not govern (exploration floor, holdout and baseline shares, stop rules). Where both set
      a limit, the tighter one wins, so the result is never looser than either: channels and offers
      are those both allow (the base's offer description, with its `needs_approval`, is kept), the
      budget, daily spend and weekly cap are the smaller, and the quiet hours cover both windows.
      A mandate and base that share no channel cannot make an envelope, which is a `ValueError`.
    * `spent` is subtracted from the budget ceiling, so the envelope's budget is what is left.
    """
    approved = set(mandate.approved_offer_ids)
    base_offer_ids = {o.offer_id for o in base.offers} if base is not None else None
    kept: list[OfferOption] = [NO_OFFER]
    # The base's descriptions come first: they are what the marketer approved for this goal.
    for o in (*(base.offers if base is not None else ()), *offers):
        if o.offer_id not in approved or any(k.offer_id == o.offer_id for k in kept):
            continue
        if base_offer_ids is not None and o.offer_id not in base_offer_ids:
            continue
        kept.append(o)
    # Without a base, the envelope's own defaults stand for what the mandate does not govern.
    ref = base if base is not None else Envelope(channels=mandate.channels, budget=0.0)
    channels = mandate.channels
    weekly = _weekly_cap(mandate)
    daily = mandate.max_daily_spend
    budget = max(0.0, mandate.budget_ceiling - spent)
    quiet = (mandate.quiet_hours.start, mandate.quiet_hours.end)
    if base is not None:
        channels = tuple(c for c in mandate.channels if c in base.channels)
        if not channels:
            raise ValueError("the mandate and the base envelope share no channel")
        weekly = min(weekly, base.max_contacts_per_week)
        if base.max_daily_spend is not None:
            daily = base.max_daily_spend if daily is None else min(daily, base.max_daily_spend)
        budget = min(budget, base.budget)
        quiet = _quiet_union(quiet, (base.quiet_start_hour, base.quiet_end_hour))
    return Envelope(
        channels=channels,
        offers=tuple(kept),
        budget=budget,
        max_daily_spend=daily,
        max_contacts_per_week=weekly,
        quiet_start_hour=quiet[0],
        quiet_end_hour=quiet[1],
        exploration_floor=ref.exploration_floor,
        holdout_share=ref.holdout_share,
        baseline_share=ref.baseline_share,
        stop_rules=ref.stop_rules,
    )


# --- pacing ----------------------------------------------------------------------------------------------


class TokenBucket(Contract):
    """Deterministic spend pacing under a daily cap. Immutable: every call returns the new bucket.

    The bucket holds up to `capacity` money and refills at `refill_per_second`. Built with
    `for_daily_cap`, capacity is a `burst_share` of the daily cap and the refill rate is the rest
    spread over 24 hours, so any rolling 24 hours spends at most `capacity + rate x 24h`, which is
    exactly the daily cap. Time is passed in; a time earlier than the last update adds nothing.
    """

    capacity: float = Field(ge=0)
    refill_per_second: float = Field(ge=0)
    tokens: float = Field(ge=0)
    updated_at: AwareDatetime

    @field_validator("updated_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return to_utc(v)

    @model_validator(mode="after")
    def _within_capacity(self) -> TokenBucket:
        # A bucket holding more than its capacity could spend past the daily cap in one go.
        if self.tokens > self.capacity + _EPS:
            raise ValueError("a bucket cannot hold more than its capacity")
        return self

    @classmethod
    def for_daily_cap(cls, daily_cap: float, now: datetime, *, burst_share: float = 0.25) -> TokenBucket:
        """A full bucket for a daily cap. `burst_share` (0..1, exclusive) is how much may go at once."""
        if not 0 < burst_share < 1:
            raise ValueError("burst_share is between 0 and 1, exclusive")
        if daily_cap < 0:
            raise ValueError("a daily cap is not negative")
        capacity = daily_cap * burst_share
        return cls(
            capacity=capacity,
            refill_per_second=(daily_cap - capacity) / 86_400.0,
            tokens=capacity,
            updated_at=now,
        )

    @classmethod
    def for_mandate(cls, mandate: Mandate, now: datetime, *, burst_share: float = 0.25) -> TokenBucket | None:
        """The pacer for a mandate's `max_daily_spend`; None when it has no daily cap."""
        if mandate.max_daily_spend is None:
            return None
        return cls.for_daily_cap(mandate.max_daily_spend, now, burst_share=burst_share)

    def refill(self, now: datetime) -> TokenBucket:
        elapsed = max(0.0, (to_utc(now) - self.updated_at).total_seconds())
        if elapsed == 0.0:
            return self
        tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        return self.model_copy(update={"tokens": tokens, "updated_at": to_utc(now)})

    def available(self, now: datetime) -> float:
        return self.refill(now).tokens

    def try_spend(self, amount: float, now: datetime) -> tuple[bool, TokenBucket]:
        """(allowed, new bucket). A refused spend takes nothing but still records the refill."""
        if amount < 0:
            raise ValueError("cannot spend a negative amount")
        bucket = self.refill(now)
        if amount > bucket.tokens + _EPS:
            return False, bucket
        return True, bucket.model_copy(update={"tokens": max(0.0, bucket.tokens - amount)})

    def wait_seconds(self, amount: float, now: datetime) -> float | None:
        """How long until `amount` can be spent; None if it never can (more than the capacity)."""
        bucket = self.refill(now)
        if amount <= bucket.tokens + _EPS:
            return 0.0
        if amount > bucket.capacity + _EPS or bucket.refill_per_second <= 0:
            return None
        return (amount - bucket.tokens) / bucket.refill_per_second
