"""The Strategist: turns a marketer's goal into a typed `Brief`, then narrows it week by week.

The model never writes a `Brief` directly. It answers with a `BriefDraft` whose fields are all
picks from fixed lists (goal kind, objective, audience fields and operators, the business's own
channels and offer ids, horizon, weekly cap, follow-up days). Code then checks every pick and
builds the `Brief`: ids, version, the envelope budget (always the goal's budget) and every default.
A threshold the model writes into an audience filter must come from the marketer's words or from
`STANDARD_NUMBERS`; the model never invents a number.

When the model's answer cannot be read (`AgentOutputError`) or any pick is outside its list, the
Strategist does not patch the draft. It falls back to a deterministic template for the goal kind
and says so in the brief's `rationale` (and `created_by` reads "strategist:template"). The
templates, all with the goal's own objective and budget, every channel the business has, a weekly
cap of 2 (or the business's ceiling if lower), and offers limited to the kinds listed that do not
need approval, plus "no offer":

| Kind            | Audience                                                    | Horizon | Follow-ups          | Offer kinds                        |
|-----------------|-------------------------------------------------------------|---------|---------------------|------------------------------------|
| win_back        | purchases >= 1 and last purchase more than 60 days ago      | 14 days | day 3, no purchase  | any                                |
| repeat_purchase | purchases == 1 and last purchase more than 14 days ago      | 14 days | none                | loyalty, free shipping, %/$ off, content |
| cart_recovery   | seen in the last 3 days (see note)                          | 7 days  | day 1, no purchase  | free shipping, %/$ off             |
| cross_sell      | purchases >= 1 and last purchase less than 60 days ago      | 30 days | none                | content, loyalty, gift             |
| onboarding      | first seen less than 14 days ago                            | 14 days | day 3, no response  | content, free shipping             |
| retention       | purchases >= 2, last purchase between 30 and 90 days ago    | 30 days | none                | loyalty, content, gift             |
| custom          | everyone we may contact (no filters)                        | 14 days | none                | none                               |

Note on cart_recovery: `CustomerState` has no cart field yet, so the template stands in "recent
visitors" for "left something in the cart". The inner loop's event triggers sharpen it.

`adjust_brief` is the weekly slow loop: given the Analyst's report it may only narrow the plan
(keep a subset of channels and offers, lower the weekly cap, drop follow-ups). Anything wider is
ignored by code and listed in the rationale; budget, audience, horizon and the proof split never
move. Re-widening needs the marketer to approve a new brief. `brief_within` is the check the
orchestrator runs on every adjusted brief before storing it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeVar, get_args

from pydantic import BaseModel, ConfigDict, Field

from autonomy.agents.base import AgentOutputError, ask_json, load_prompt
from autonomy.contracts import (
    NO_OFFER,
    Audience,
    AudienceFilter,
    Brief,
    Briefing,
    Channel,
    Contract,
    Envelope,
    FollowUpStep,
    Goal,
    GoalKind,
    Objective,
    OfferKind,
    OfferOption,
    stable_id,
)
from autonomy.contracts.goals import FilterOp
from engine.llm import LLMClient, LLMError

T = TypeVar("T")


# --- What the model may choose from ---------------------------------------------------------------

FieldKind = Literal["date", "number", "text", "flag"]

AUDIENCE_FIELDS: dict[str, FieldKind] = {
    "last_purchase_at": "date",
    "first_seen_at": "date",
    "last_seen_at": "date",
    "last_contact_at": "date",
    "purchases": "number",
    "revenue": "number",
    "margin": "number",
    "contacts_7d": "number",
    "contacts_30d": "number",
    "offer_sensitivity": "number",
    "country": "text",
    "segment": "text",
    "timezone": "text",
    "open_complaint": "flag",
}
"""The `CustomerState` fields an audience may filter on, and what kind of value each holds."""

OPERATORS: dict[FieldKind, tuple[str, ...]] = {
    "date": ("days_since_gt", "days_since_lt", "is_null", "not_null"),
    "number": ("eq", "ne", "gt", "gte", "lt", "lte", "is_null", "not_null"),
    "text": ("eq", "ne", "in", "not_in", "is_null", "not_null"),
    "flag": ("eq", "ne"),
}

STANDARD_NUMBERS: frozenset[float] = frozenset(
    {0, 0.25, 0.5, 0.75, 1, 2, 3, 5, 7, 10, 14, 21, 30, 45, 60, 90, 120, 180, 365}
)
"""Thresholds the model may use without the marketer having said them (counts, day spans, shares)."""

HorizonDays = Literal[7, 14, 30, 60, 90]
WeeklyCap = Literal[1, 2, 3]
FollowUpDays = Literal[1, 2, 3, 5, 7, 14]
FollowUpWhen = Literal["no_response", "no_purchase", "always"]

_TEMPLATE_CAP = 2


class BusinessFacts(Contract):
    """What the business has, as far as a plan is concerned. Code fills this, never the model."""

    business_id: str
    name: str = ""
    channels: tuple[Channel, ...] = Field(description="Channels the business can send on")
    offers: tuple[OfferOption, ...] = Field(default=(), description="The offer catalogue")
    max_contacts_per_week: int = Field(default=2, ge=1, le=7, description="The business's own ceiling")
    customer_count: int | None = Field(default=None, ge=0)
    notes: tuple[str, ...] = ()


class DraftFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    op: FilterOp
    value: str | int | float | bool | list[str] | None = None


class DraftFollowUp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    after_days: FollowUpDays
    when: FollowUpWhen = "no_purchase"
    channel: Channel | None = None


class BriefDraft(BaseModel):
    """The model's answer for a new goal: picks from lists, which code turns into a `Brief`."""

    model_config = ConfigDict(extra="forbid")

    kind: GoalKind
    objective: Objective
    audience_description: str = Field(max_length=300)
    audience_filters: list[DraftFilter] = Field(default_factory=list, max_length=6)
    channels: list[Channel] = Field(min_length=1)
    offer_ids: list[str] = Field(default_factory=list)
    horizon_days: HorizonDays = 14
    max_contacts_per_week: WeeklyCap = 2
    follow_ups: list[DraftFollowUp] = Field(default_factory=list, max_length=3)
    rationale: str = Field(default="", max_length=1500)


class BriefAdjustment(BaseModel):
    """The model's weekly answer: what to keep. Code ignores anything outside the current brief."""

    model_config = ConfigDict(extra="forbid")

    keep_channels: list[Channel]
    keep_offer_ids: list[str]
    max_contacts_per_week: int = Field(ge=0, le=7)
    keep_follow_up_days: list[int] | None = Field(
        default=None, description="after_days of the follow-ups to keep; null keeps them all"
    )
    rationale: str = Field(default="", max_length=1500)


# --- Templates ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BriefTemplate:
    """A safe, standard plan for one goal kind. See the module docstring for the table."""

    description: str
    filters: tuple[AudienceFilter, ...]
    horizon_days: int
    follow_ups: tuple[tuple[int, FollowUpWhen], ...]
    offer_kinds: frozenset[OfferKind]
    note: str


_PRICE_OFFERS = frozenset({OfferKind.PERCENT_OFF, OfferKind.AMOUNT_OFF, OfferKind.FREE_SHIPPING})

TEMPLATES: dict[GoalKind, BriefTemplate] = {
    GoalKind.WIN_BACK: BriefTemplate(
        description="Bought before, nothing in the last 60 days",
        filters=(
            AudienceFilter(field="purchases", op="gte", value=1),
            AudienceFilter(field="last_purchase_at", op="days_since_gt", value=60),
        ),
        horizon_days=14,
        follow_ups=((3, "no_purchase"),),
        offer_kinds=frozenset(OfferKind),
        note="Lapsed buyers get a reminder; offers only where they change behaviour.",
    ),
    GoalKind.REPEAT_PURCHASE: BriefTemplate(
        description="Bought exactly once, more than 14 days ago",
        filters=(
            AudienceFilter(field="purchases", op="eq", value=1),
            AudienceFilter(field="last_purchase_at", op="days_since_gt", value=14),
        ),
        horizon_days=14,
        follow_ups=(),
        offer_kinds=_PRICE_OFFERS | {OfferKind.LOYALTY, OfferKind.CONTENT},
        note="One-time buyers are nudged towards a second order.",
    ),
    GoalKind.CART_RECOVERY: BriefTemplate(
        description="Visited in the last 3 days",
        filters=(AudienceFilter(field="last_seen_at", op="days_since_lt", value=3),),
        horizon_days=7,
        follow_ups=((1, "no_purchase"),),
        offer_kinds=_PRICE_OFFERS,
        note="Recent visitors stand in for cart abandoners until customer state carries carts.",
    ),
    GoalKind.CROSS_SELL: BriefTemplate(
        description="Bought in the last 60 days",
        filters=(
            AudienceFilter(field="purchases", op="gte", value=1),
            AudienceFilter(field="last_purchase_at", op="days_since_lt", value=60),
        ),
        horizon_days=30,
        follow_ups=(),
        offer_kinds=frozenset({OfferKind.CONTENT, OfferKind.LOYALTY, OfferKind.GIFT}),
        note="Recent buyers hear about related categories, without discounts.",
    ),
    GoalKind.ONBOARDING: BriefTemplate(
        description="First seen in the last 14 days",
        filters=(AudienceFilter(field="first_seen_at", op="days_since_lt", value=14),),
        horizon_days=14,
        follow_ups=((3, "no_response"),),
        offer_kinds=frozenset({OfferKind.CONTENT, OfferKind.FREE_SHIPPING}),
        note="New people get useful information first; a small incentive at most.",
    ),
    GoalKind.RETENTION: BriefTemplate(
        description="Regular buyers (2 or more orders) whose last order was 30 to 90 days ago",
        filters=(
            AudienceFilter(field="purchases", op="gte", value=2),
            AudienceFilter(field="last_purchase_at", op="days_since_gt", value=30),
            AudienceFilter(field="last_purchase_at", op="days_since_lt", value=90),
        ),
        horizon_days=30,
        follow_ups=(),
        offer_kinds=frozenset({OfferKind.LOYALTY, OfferKind.CONTENT, OfferKind.GIFT}),
        note="Good customers who are slowing down are thanked, not discounted.",
    ),
    GoalKind.CUSTOM: BriefTemplate(
        description="Everyone we may contact",
        filters=(),
        horizon_days=14,
        follow_ups=(),
        offer_kinds=frozenset({OfferKind.NONE}),
        note="An unrecognised goal gets the narrowest plan: no offers, no follow-ups.",
    ),
}

_KIND_WORDS: tuple[tuple[GoalKind, tuple[str, ...]], ...] = (
    (GoalKind.CART_RECOVERY, ("cart", "checkout", "abandon")),
    (
        GoalKind.WIN_BACK,
        ("win back", "winback", "win-back", "lapsed", "dormant", "inactive", "haven't bought", "come back"),
    ),
    (GoalKind.ONBOARDING, ("onboard", "welcome", "new customer", "sign up", "signup", "sign-up")),
    (GoalKind.CROSS_SELL, ("cross-sell", "cross sell", "upsell", "up-sell", "other categor")),
    (
        GoalKind.REPEAT_PURCHASE,
        ("repeat", "second order", "second purchase", "buy again", "reorder", "refill"),
    ),
    (GoalKind.RETENTION, ("retain", "retention", "churn", "loyal")),
)


def guess_kind(goal: Goal, draft: BriefDraft | None = None) -> GoalKind:
    """The goal's kind: the marketer's, else the model's, else a keyword match, else custom."""
    if goal.kind is not GoalKind.CUSTOM:
        return goal.kind
    if draft is not None and draft.kind is not GoalKind.CUSTOM:
        return draft.kind
    text = goal.text.lower()
    for kind, words in _KIND_WORDS:
        if any(w in text for w in words):
            return kind
    return GoalKind.CUSTOM


def template_brief(
    goal: Goal, facts: BusinessFacts, now: datetime, *, kind: GoalKind | None = None, why: str = ""
) -> Brief:
    """The deterministic brief for a goal kind. Same inputs, same brief, every time."""
    _check_facts(goal, facts)
    if kind is None:
        kind = guess_kind(goal)
    t = TEMPLATES[kind]
    offers = (
        NO_OFFER,
        *(
            o
            for o in facts.offers
            if o.kind in t.offer_kinds and not o.needs_approval and o.offer_id != NO_OFFER.offer_id
        ),
    )
    envelope = Envelope(
        channels=_unique(facts.channels),
        offers=offers,
        budget=goal.budget,
        max_contacts_per_week=min(_TEMPLATE_CAP, facts.max_contacts_per_week),
    )
    rationale = f"Standard {kind.value} plan. {t.note} " + _summary(
        t.description, envelope, t.horizon_days, goal.currency
    )
    if why:
        rationale += f" The model's draft was not used because {why}."
    return Brief(
        brief_id=stable_id("brief", goal.goal_id, 1, now.isoformat()),
        goal_id=goal.goal_id,
        version=1,
        objective=goal.objective,
        audience=Audience(description=t.description, filters=t.filters),
        horizon_days=t.horizon_days,
        envelope=envelope,
        follow_ups=tuple(FollowUpStep(after_days=d, when=w) for d, w in t.follow_ups),
        rationale=rationale,
        created_by="strategist:template",
        created_at=now,
    )


# --- The agent ------------------------------------------------------------------------------------


class Strategist:
    """Goal in, `Brief` out; and once a week, a narrower `Brief` from the Analyst's report."""

    def __init__(self, llm: LLMClient, model_id: str | None = None) -> None:
        self._llm = llm
        self._model_id = model_id

    def draft_brief(self, goal: Goal, *, business_facts: BusinessFacts, now: datetime) -> Brief:
        """A version-1, unapproved brief for `goal`. Falls back to the kind's template on any doubt."""
        facts = business_facts
        _check_facts(goal, facts)
        allowed = STANDARD_NUMBERS | numbers_in(goal.text)
        try:
            draft = ask_json(
                self._llm,
                BriefDraft,
                system=load_prompt("strategist"),
                prompt=_draft_prompt(goal, facts, now),
                model_id=self._model_id,
            )
        except AgentOutputError as err:
            return template_brief(
                goal, facts, now, kind=guess_kind(goal), why=f"its answer could not be read ({_short(err)})"
            )
        except LLMError as err:  # the model is down or refused: a plan must still come out
            return template_brief(
                goal, facts, now, kind=guess_kind(goal), why=f"the model could not be reached ({err.code})"
            )
        problems = _draft_problems(draft, goal, facts, allowed)
        if problems:
            return template_brief(
                goal,
                facts,
                now,
                kind=guess_kind(goal, draft),
                why="it chose values outside the allowed lists: " + "; ".join(problems),
            )
        return _brief_from_draft(goal, facts, draft, now, allowed)

    def adjust_brief(self, brief: Brief, insights: Briefing, now: datetime) -> Brief:
        """The next version of `brief`, narrowed (or unchanged) in light of the Analyst's report."""
        if insights.goal_id is not None and insights.goal_id != brief.goal_id:
            raise ValueError(f"the report is for goal {insights.goal_id}, the brief for {brief.goal_id}")
        try:
            adj = ask_json(
                self._llm,
                BriefAdjustment,
                system=load_prompt("strategist"),
                prompt=_adjust_prompt(brief, insights, now),
                model_id=self._model_id,
            )
        except (AgentOutputError, LLMError) as err:
            return _next_version(
                brief,
                envelope=brief.envelope,
                follow_ups=brief.follow_ups,
                now=now,
                rationale=f"No change this week: the model's suggestion could not be read ({_short(err)}).",
            )
        envelope, follow_ups, changes, ignored = _narrow(brief, adj)
        allowed = STANDARD_NUMBERS | _brief_numbers(brief) | _insight_numbers(insights)
        reason = adj.rationale.strip()
        if reason and not numbers_in(reason) <= allowed:
            reason = "(The model's reasoning quoted numbers not in the report, so it is not shown.)"
        parts = [reason] if reason else []
        parts.append(("Changes: " + "; ".join(changes) + ".") if changes else "No change this week.")
        if ignored:
            parts.append("Ignored, outside the approved envelope: " + "; ".join(ignored) + ".")
        return _next_version(
            brief, envelope=envelope, follow_ups=follow_ups, now=now, rationale=" ".join(parts)
        )


# --- Checking and building ------------------------------------------------------------------------

_NUMBER = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)(?:([kKmM])(?![A-Za-z]))?")
"""Digits even when glued to words ('3x', '14days', 'v2'), so no number slips past the check."""


def numbers_in(text: str) -> frozenset[float]:
    """Every number written in `text`: '5,000' and '$5k' are both 5000, '10%' is 10."""
    out: set[float] = set()
    for digits, suffix in _NUMBER.findall(text):
        cleaned = digits.replace(",", "").rstrip(".")
        if not cleaned:
            continue
        value = float(cleaned)
        if suffix:
            value *= 1_000 if suffix in "kK" else 1_000_000
        out.add(value)
    return frozenset(out)


def _check_facts(goal: Goal, facts: BusinessFacts) -> None:
    if not facts.channels:
        raise ValueError(f"business {facts.business_id} has no channels to plan with")
    if facts.business_id != goal.business_id:
        raise ValueError(f"goal {goal.goal_id} belongs to {goal.business_id}, not {facts.business_id}")


def _allowed_number(value: float, allowed: Iterable[float]) -> bool:
    return any(abs(value - a) < 1e-9 for a in allowed)


def _filter_problem(f: DraftFilter, allowed: frozenset[float]) -> str | None:
    kind = AUDIENCE_FIELDS.get(f.field)
    if kind is None:
        return f"audience field {f.field!r} is not allowed"
    if f.op not in OPERATORS[kind]:
        return f"operator {f.op} does not apply to {f.field}"
    v = f.value
    if f.op in ("is_null", "not_null"):
        return None if v is None else f"{f.field} {f.op} takes no value"
    if kind in ("date", "number"):
        if isinstance(v, bool) or not isinstance(v, int | float):
            return f"{f.field} {f.op} needs a number"
        if not _allowed_number(float(v), allowed):
            return f"threshold {v} for {f.field} is neither in the goal nor a standard number"
        return None
    if kind == "flag":
        return None if isinstance(v, bool) else f"{f.field} {f.op} needs true or false"
    if f.op in ("in", "not_in"):
        ok = isinstance(v, list) and bool(v) and all(s.strip() for s in v)
        return None if ok else f"{f.field} {f.op} needs a list of words"
    return None if isinstance(v, str) and v.strip() else f"{f.field} {f.op} needs a word"


def _draft_problems(
    draft: BriefDraft, goal: Goal, facts: BusinessFacts, allowed: frozenset[float]
) -> list[str]:
    problems: list[str] = []
    if goal.kind is not GoalKind.CUSTOM and draft.kind is not goal.kind:
        problems.append(f"the goal is {goal.kind.value} but the draft says {draft.kind.value}")
    for f in draft.audience_filters:
        problem = _filter_problem(f, allowed)
        if problem:
            problems.append(problem)
    for c in draft.channels:
        if c not in facts.channels:
            problems.append(f"channel {c.value} is not one the business has")
    catalogue = {o.offer_id for o in facts.offers} | {NO_OFFER.offer_id}
    for offer_id in draft.offer_ids:
        if offer_id not in catalogue:
            problems.append(f"offer {offer_id!r} is not in the catalogue")
    if draft.max_contacts_per_week > facts.max_contacts_per_week:
        problems.append(
            f"weekly cap {draft.max_contacts_per_week} is above the business ceiling "
            f"{facts.max_contacts_per_week}"
        )
    for step in draft.follow_ups:
        if step.after_days > draft.horizon_days:
            problems.append(
                f"follow-up on day {step.after_days} is after the {draft.horizon_days}-day horizon"
            )
        if step.channel is not None and step.channel not in draft.channels:
            problems.append(f"follow-up channel {step.channel.value} is not in the brief's channels")
    return problems


def _brief_from_draft(
    goal: Goal, facts: BusinessFacts, draft: BriefDraft, now: datetime, allowed: frozenset[float]
) -> Brief:
    catalogue = {o.offer_id: o for o in facts.offers}
    offers = (
        NO_OFFER,
        *(catalogue[i] for i in _unique(draft.offer_ids) if i != NO_OFFER.offer_id),
    )
    envelope = Envelope(
        channels=_unique(draft.channels),
        offers=offers,
        budget=goal.budget,
        max_contacts_per_week=draft.max_contacts_per_week,
    )
    filters = tuple(
        AudienceFilter(field=f.field, op=f.op, value=tuple(f.value) if isinstance(f.value, list) else f.value)
        for f in draft.audience_filters
    )
    unique_steps = {(s.after_days, s.when, s.channel): s for s in draft.follow_ups}
    ordered = sorted(
        unique_steps.values(),
        key=lambda s: (s.after_days, s.when, s.channel.value if s.channel is not None else ""),
    )
    follow_ups = tuple(FollowUpStep(after_days=s.after_days, when=s.when, channel=s.channel) for s in ordered)
    text_numbers: set[float] = {goal.budget, float(draft.horizon_days), float(draft.max_contacts_per_week)}
    text_numbers |= {float(s.after_days) for s in follow_ups}
    text_numbers |= {o.amount for o in offers}
    if facts.customer_count is not None:
        text_numbers.add(float(facts.customer_count))
    text_allowed = allowed | text_numbers
    description = draft.audience_description.strip()
    if not description or not numbers_in(description) <= text_allowed:
        description = describe_filters(filters)
    summary = _summary(description, envelope, draft.horizon_days, goal.currency)
    reason = draft.rationale.strip()
    if not reason or not numbers_in(reason) <= text_allowed:
        reason = f"A {draft.kind.value} plan."
    return Brief(
        brief_id=stable_id("brief", goal.goal_id, 1, now.isoformat()),
        goal_id=goal.goal_id,
        version=1,
        objective=draft.objective,
        audience=Audience(description=description, filters=filters),
        horizon_days=draft.horizon_days,
        envelope=envelope,
        follow_ups=follow_ups,
        rationale=f"{reason} {summary}",
        created_by="strategist",
        created_at=now,
    )


def _narrow(
    brief: Brief, adj: BriefAdjustment
) -> tuple[Envelope, tuple[FollowUpStep, ...], list[str], list[str]]:
    """Apply `adj` to the brief, keeping only moves inside its envelope. Returns what changed and why not."""
    env = brief.envelope
    changes: list[str] = []
    ignored: list[str] = []

    ignored += [f"channel {c.value}" for c in _unique(adj.keep_channels) if c not in env.channels]
    channels = tuple(c for c in env.channels if c in adj.keep_channels)
    if not channels:
        channels = env.channels
        ignored.append("dropping every channel (that is a pause, not an adjustment)")
    changes += [f"stopped using {c.value}" for c in env.channels if c not in channels]

    current_ids = {o.offer_id for o in env.offers}
    ignored += [f"offer {i!r}" for i in _unique(adj.keep_offer_ids) if i not in current_ids]
    offers = tuple(
        o for o in env.offers if o.offer_id in adj.keep_offer_ids or o.offer_id == NO_OFFER.offer_id
    )
    if not offers:
        offers = env.offers
        ignored.append("dropping every offer")
    changes += [f"dropped offer {o.label!r}" for o in env.offers if o not in offers]

    cap = adj.max_contacts_per_week
    if cap > env.max_contacts_per_week:
        ignored.append(f"raising the weekly cap to {cap}")
        cap = env.max_contacts_per_week
    elif cap < 1:
        ignored.append("a weekly cap of 0 (that is a pause, not an adjustment)")
        cap = env.max_contacts_per_week
    elif cap < env.max_contacts_per_week:
        changes.append(f"weekly cap lowered from {env.max_contacts_per_week} to {cap}")

    follow_ups = brief.follow_ups
    if adj.keep_follow_up_days is not None:
        keep = set(adj.keep_follow_up_days)
        current_days = {s.after_days for s in brief.follow_ups}
        ignored += [f"a follow-up on day {d}" for d in sorted(keep - current_days)]
        follow_ups = tuple(s for s in brief.follow_ups if s.after_days in keep)
    # A follow-up pinned to a channel that is no longer allowed would send outside the envelope.
    follow_ups = tuple(s for s in follow_ups if s.channel is None or s.channel in channels)
    changes += [f"dropped the day-{s.after_days} follow-up" for s in brief.follow_ups if s not in follow_ups]

    envelope = env.model_copy(update={"channels": channels, "offers": offers, "max_contacts_per_week": cap})
    return envelope, follow_ups, changes, ignored


def _next_version(
    brief: Brief,
    *,
    envelope: Envelope,
    follow_ups: tuple[FollowUpStep, ...],
    now: datetime,
    rationale: str,
) -> Brief:
    """The same brief, one version on. Approval carries over: the envelope can only have narrowed."""
    version = brief.version + 1
    return brief.model_copy(
        update={
            "brief_id": stable_id("brief", brief.goal_id, version, now.isoformat()),
            "version": version,
            "envelope": envelope,
            "follow_ups": follow_ups,
            "rationale": rationale,
            "created_by": "strategist",
            "created_at": now,
        }
    )


def brief_within(new: Brief, approved: Brief) -> list[str]:
    """Every way `new` reaches outside `approved`. Empty means `new` is a safe narrowing."""
    problems: list[str] = []
    a, n = approved.envelope, new.envelope
    if new.goal_id != approved.goal_id:
        problems.append("a different goal")
    if new.objective is not approved.objective:
        problems.append("the objective changed")
    if new.audience != approved.audience:
        problems.append("the audience changed")
    if new.horizon_days != approved.horizon_days:
        problems.append("the horizon changed")
    if not set(n.channels) <= set(a.channels):
        problems.append("channels widened")
    if not all(o in a.offers for o in n.offers):
        problems.append("offers widened or changed")
    if n.budget > a.budget:
        problems.append("budget widened")
    if a.max_daily_spend is not None and (n.max_daily_spend is None or n.max_daily_spend > a.max_daily_spend):
        problems.append("daily spend widened")
    if n.max_contacts_per_week > a.max_contacts_per_week:
        problems.append("weekly cap raised")
    if (n.quiet_start_hour, n.quiet_end_hour) != (a.quiet_start_hour, a.quiet_end_hour):
        problems.append("quiet hours changed")
    if (n.exploration_floor, n.holdout_share, n.baseline_share) != (
        a.exploration_floor,
        a.holdout_share,
        a.baseline_share,
    ):
        problems.append("exploration or the proof split changed")
    if not all(r in n.stop_rules for r in a.stop_rules):
        problems.append("a stop rule was removed or loosened")
    if not all(s in approved.follow_ups for s in new.follow_ups):
        problems.append("follow-ups added")
    if any(s.channel is not None and s.channel not in n.channels for s in new.follow_ups):
        problems.append("a follow-up uses a channel outside the envelope")
    return problems


# --- Words ----------------------------------------------------------------------------------------


def _unique(items: Iterable[T]) -> tuple[T, ...]:
    return tuple(dict.fromkeys(items))


def _short(err: Exception, limit: int = 240) -> str:
    text = " ".join(str(err).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _num(x: float) -> str:
    """'5,000', '120.5', '0.0008': enough decimals that a rate is not shown as 0."""
    return f"{x:,.6f}".rstrip("0").rstrip(".")


_OP_WORDS = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "in": "in",
    "not_in": "not in",
}


def describe_filters(filters: Sequence[AudienceFilter]) -> str:
    """Filters in plain words, written by code so every number in it is the filter's own."""
    if not filters:
        return "Everyone we may contact"
    parts: list[str] = []
    for f in filters:
        name = f.field.replace("_", " ")
        v = f.value
        if f.op == "days_since_gt":
            parts.append(f"{name} more than {v} days ago")
        elif f.op == "days_since_lt":
            parts.append(f"{name} less than {v} days ago")
        elif f.op == "is_null":
            parts.append(f"no {name}")
        elif f.op == "not_null":
            parts.append(f"has a {name}")
        else:
            shown = ", ".join(v) if isinstance(v, tuple) else str(v)
            parts.append(f"{name} {_OP_WORDS[f.op]} {shown}")
    text = " and ".join(parts)
    return text[0].upper() + text[1:]


def _summary(description: str, envelope: Envelope, horizon_days: int, currency: str) -> str:
    offers = ", ".join(o.label for o in envelope.offers)
    channels = ", ".join(c.value for c in envelope.channels)
    return (
        f"Who: {description}. Channels: {channels}. Offers: {offers}. "
        f"Results are judged {horizon_days} days after each contact, with at most "
        f"{envelope.max_contacts_per_week} contacts per customer per week and a budget of "
        f"{_num(envelope.budget)} {currency}."
    )


def _brief_numbers(brief: Brief) -> frozenset[float]:
    env = brief.envelope
    nums = {env.budget, float(env.max_contacts_per_week), float(brief.horizon_days)}
    nums |= {o.amount for o in env.offers}
    nums |= {float(s.after_days) for s in brief.follow_ups}
    return frozenset(nums | {abs(n) for n in nums})


def _insight_numbers(insights: Briefing) -> frozenset[float]:
    nums: set[float] = set(numbers_in(insights.headline))
    for s in insights.sections:
        # the prompt shows numbers to 6 decimals, so the model may quote them rounded that way
        nums |= {v for x in s.numbers.values() for v in (x, round(x, 6))} | numbers_in(s.body)
    return frozenset(nums | {abs(n) for n in nums})


def _join(items: Iterable[object]) -> str:
    return ", ".join(str(i) for i in items)


def _draft_prompt(goal: Goal, facts: BusinessFacts, now: datetime) -> str:
    offer_lines = [
        f"- {o.offer_id}: {o.label} ({o.kind.value}" + (", needs approval)" if o.needs_approval else ")")
        for o in facts.offers
        if o.offer_id != NO_OFFER.offer_id
    ]
    offers = "\n".join(offer_lines) or "- (no offers)"
    fields = "\n".join(
        f"  - {name} ({kind}): {_join(OPERATORS[kind])}" for name, kind in AUDIENCE_FIELDS.items()
    )
    notes = "\n".join(f"- {n}" for n in facts.notes) or "- none"
    channels = _join(c.value for c in _unique(facts.channels))
    kinds = _join(k.value for k in GoalKind)
    objectives = _join(o.value for o in Objective)
    horizons = _join(get_args(HorizonDays))
    caps = _join(get_args(WeeklyCap))
    days = _join(get_args(FollowUpDays))
    whens = _join(get_args(FollowUpWhen))
    standard = _join(_num(n) for n in sorted(STANDARD_NUMBERS))
    return f"""Draft a brief for this goal.

The marketer's goal: {goal.text!r}
Goal kind set by the marketer: {goal.kind.value} (custom means you choose)
Objective set by the marketer: {goal.objective.value}
Budget: {_num(goal.budget)} {goal.currency} (fixed; not yours to set)
Today: {now.date().isoformat()}

Business: {facts.name or facts.business_id}
Channels it can use: {channels}
Offer catalogue (the id "none", meaning no offer, is always included for you):
{offers}
Weekly contact ceiling: at most {facts.max_contacts_per_week} contacts per customer
Notes:
{notes}

Allowed lists:
- kind: {kinds}
- objective: {objectives}
- audience fields and their operators:
{fields}
- horizon_days: {horizons}
- max_contacts_per_week: {caps} (never above the ceiling)
- follow-up after_days: {days}; when: {whens}
- standard numbers for thresholds: {standard}
- other numbers you may use: only those in the marketer's goal"""


def _adjust_prompt(brief: Brief, insights: Briefing, now: datetime) -> str:
    env = brief.envelope
    offers = "\n".join(f"  - {o.offer_id}: {o.label} ({o.kind.value})" for o in env.offers)
    channels = _join(c.value for c in env.channels)
    follow_ups = _join(f"day {s.after_days} ({s.when})" for s in brief.follow_ups) or "none"
    blocks: list[str] = []
    for sec in insights.sections:
        numbers = _join(f"{k}={_num(v)}" for k, v in sorted(sec.numbers.items())) or "none"
        blocks.append(f"### {sec.title}\n{sec.body}\nNumbers: {numbers}")
    sections = "\n\n".join(blocks) or "(no sections)"
    return f"""Adjust this brief for the coming week, narrowing only.

Today: {now.date().isoformat()}
Current brief (version {brief.version}):
- audience: {brief.audience.description}
- channels: {channels}
- offers:
{offers}
- weekly cap: {env.max_contacts_per_week}
- follow-ups: {follow_ups}
- budget: {_num(env.budget)} (fixed)

Analyst report ({insights.period}, as of {insights.as_of.date().isoformat()}): {insights.headline}

{sections}

Answer with the channels and offer ids to keep (from the current lists only), the weekly cap
(at most {env.max_contacts_per_week}), the after_days of follow-ups to keep (null keeps all), and
a short rationale that quotes only numbers from the report."""
