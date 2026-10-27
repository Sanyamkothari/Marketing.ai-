"""The Analyst: turns results into a plain-English briefing, and spots new goals (plan-v2 section 2).

**Every number comes from code.** `briefing()` first builds each section's numbers from the proof,
the health status and the caller's facts, together with how each number should be shown
(`$1,235`, `0.08%`) and notes that state the direction of each result in words (a range that
includes zero is "not clear yet"). Only then is the LLM asked to write the headline and the section
bodies around those numbers. `claims.check_numbers` reads every number back out of what it wrote:

1. clean on the first answer: use it;
2. otherwise ask once more, listing the numbers that did not match;
3. still wrong (or the model's output does not parse): use the template briefing, written entirely
   by code from the same numbers. A briefing always arrives, and it never carries an invented number.

If the model cannot be reached at all (`LLMError`, or any other exception from the provider), the
template briefing is used at once, without a second ask: retrying a provider that is down only
delays the report.

**The words a marketer reads.** The comparison group is "people we never contact", never "the
holdout" (DESIGN.md section 6); the template leads with the sentence a marketer wants (did it make
money, and how sure is that), and money is always written with its likely range. When the caller
knows what the agent changed as it learned, the briefing carries it as a short "What it learned"
section, which Live shows in its own card. `template_briefing()` writes the template briefing
alone, with no model, so the API can rebuild a stored briefing from the measurement it serves now.

**Opportunities** (`opportunities()`) are rule-based scouting over a summary of segments, with no LLM
at all: for example many lapsing customers with real past spend become a DRAFT win-back goal with
a budget computed from their value. The goal's text is the one-line reason. A person approves it,
and the Strategist turns it into a brief, before anything reaches a customer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from autonomy.agents.base import AgentOutputError, ask_json, load_prompt
from autonomy.agents.claims import check_numbers
from autonomy.contracts import (
    DEFAULT_STOP_RULES,
    AutonomyLevel,
    Briefing,
    BriefingSection,
    Contract,
    Estimate,
    Goal,
    GoalKind,
    GoalStatus,
    HealthStatus,
    Objective,
    ProofSummary,
    StopMetric,
    stable_id,
)
from engine.llm import LLMClient, LLMError

if TYPE_CHECKING:
    from autonomy.learning.early_signal import EarlyRead

Period = Literal["daily", "weekly"]
BriefingSource = Literal["llm", "llm_retry", "template"]

INTERVAL_LEVEL = 0.95
"""Every `Estimate` is a 95% interval. Listed as a number so the model may say "95%"."""

MONEY_TITLE = "Extra profit vs people we never contact"
BASELINE_TITLE = "Against the simple rule"
HEALTH_TITLE = "Customer health"
LEARNED_TITLE = "What it learned"
EARLY_TITLE = "Early read"
"""Section titles, shared with Live (it shows one figure per line under these labels)."""

CURRENCY_SYMBOLS: dict[str, str] = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "JPY": "¥"}
"""Currencies written with a symbol; any other is written as `1,200 CAD`."""


# --- how numbers are shown ------------------------------------------------------------------------


def format_money(value: float, currency: str = "USD") -> str:
    """`$1,235`, `$12.50`, `-$300`, `1,200 CAD`. Whole units from 100 up, cents below."""
    amount = f"{abs(value):,.0f}" if abs(value) >= 100 else f"{abs(value):,.2f}"
    code = currency.upper()
    symbol = CURRENCY_SYMBOLS.get(code)
    text = f"{symbol}{amount}" if symbol else f"{amount} {code}"
    return f"-{text}" if value < 0 and amount.strip("0.,") else text


def format_rate(value: float) -> str:
    """A fraction as a percent: `0.08%` for 0.0008, `12.5%` for 0.125."""
    pct = value * 100.0
    return f"{pct:.2f}%" if abs(pct) < 1 else f"{pct:.1f}%"


def format_count(value: float) -> str:
    return f"{value:,.0f}"


def format_ratio(value: float) -> str:
    return f"{value:.2f}"


def _format_fact(name: str, value: float, currency: str) -> str:
    """Facts come as bare numbers; their name says what they are."""
    low = name.lower()
    if low.endswith(("_rate", "_share", "_pct_fraction")):
        return format_rate(value)
    if any(w in low for w in ("revenue", "profit", "spend", "value", "budget", "cost", "margin", "price")):
        return format_money(value, currency)
    if float(value).is_integer():
        return format_count(value)
    return f"{value:,.2f}"


def _humanise(name: str) -> str:
    """A fact's name as words. Words with digits (`90d`, `top_3`) are dropped: every digit in a body is
    checked against its numbers, and a digit from a name is not one of them, so the template would fail."""
    words = " ".join(w for w in re.split(r"[\s_\-]+", name) if w and not re.search(r"\d", w))
    return (words[:1].upper() + words[1:]) if words else "Another figure"


# --- sections built by code -----------------------------------------------------------------------


@dataclass
class SectionDraft:
    """One section as code sees it: the numbers, how to show them, notes, and a template body."""

    key: str
    title: str
    numbers: dict[str, float] = field(default_factory=dict)
    shown: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)

    def add(self, name: str, value: float, text: str) -> str:
        self.numbers[name] = float(value)
        self.shown[name] = text
        return text

    @property
    def template_body(self) -> str:
        return " ".join(self.sentences)

    def section(self, body: str | None = None) -> BriefingSection:
        return BriefingSection(title=self.title, body=body or self.template_body, numbers=dict(self.numbers))


def _verdict(est: Estimate) -> Literal["up", "down", "unclear"]:
    if est.low > 0:
        return "up"
    if est.high < 0:
        return "down"
    return "unclear"


def _estimate(draft: SectionDraft, name: str, est: Estimate, fmt: str, currency: str) -> tuple[str, str, str]:
    """Adds point, low and high; returns them as shown."""

    def show(v: float) -> str:
        return format_money(v, currency) if fmt == "money" else format_ratio(v)

    return (
        draft.add(name, est.point, show(est.point)),
        draft.add(f"{name}_low", est.low, show(est.low)),
        draft.add(f"{name}_high", est.high, show(est.high)),
    )


def _detectable_sentences(d: SectionDraft, proof: ProofSummary, currency: str) -> None:
    """What this size of comparison can see at all, and a warning when the shop margin is not set."""
    det = proof.detectable
    if det is not None and proof.incremental_profit is not None:
        smallest = d.add("detectable_profit", det.profit, format_money(det.profit, currency))
        if det.relative is not None:
            rel = d.add("detectable_lift", det.relative, format_rate(det.relative))
            d.sentences.append(
                f"With this many customers, a real gain below {rel} in profit per customer, about "
                f"{smallest} in all, would not show up yet."
            )
        else:
            d.sentences.append(
                f"With this many customers, a real gain below about {smallest} would not show up yet."
            )
        d.notes.append(
            "Say how small a gain this comparison can see, so a flat result is not read as no effect."
        )
    if proof.margin_note is not None and proof.margin_rate is None:
        d.sentences.append(
            "The shop's margin is not set, so orders with no recorded margin are counted at their full "
            "value and profit is overstated."
        )
        d.notes.append("The shop margin is not set: profit is overstated; say so.")


def _money_section(proof: ProofSummary, currency: str) -> SectionDraft:
    d = SectionDraft("money", MONEY_TITLE)
    agent_n = d.add("agent_customers", proof.agent_n, format_count(proof.agent_n))
    holdout_n = d.add("holdout_customers", proof.holdout_n, format_count(proof.holdout_n))
    if any(e is not None for e in (proof.incremental_profit, proof.incremental_revenue)):
        d.add("interval_level", INTERVAL_LEVEL, format_rate(INTERVAL_LEVEL))

    profit = proof.incremental_profit
    if profit is None:
        d.sentences.append(
            "Not sure yet: there are not enough results to compare profit with the people we never contact."
        )
        d.notes.append("Profit cannot be measured yet; do not suggest a win or a loss.")
    else:
        p, lo, hi = _estimate(d, "incremental_profit", profit, "money", currency)
        verdict = _verdict(profit)
        if verdict == "up":
            d.sentences.append(
                f"It made an extra {p} in profit compared with people we never contact "
                f"(likely range {lo} to {hi})."
            )
            d.notes.append(
                "Profit against the people we never contact is clearly positive: the whole likely range is "
                "above zero."
            )
        elif verdict == "down":
            d.sentences.append(
                f"It made {format_money(abs(profit.point), currency)} less profit than the people we never "
                f"contact (likely range {lo} to {hi})."
            )
            d.notes.append(
                "Profit against the people we never contact is clearly negative: the whole likely range is "
                "below zero."
            )
        else:
            d.sentences.append(
                f"Not sure yet: the best estimate is {p} of extra profit, but the likely range runs from "
                f"{lo} to {hi}, which includes zero."
            )
            d.notes.append("The profit range includes zero: we cannot tell yet whether the agent helped.")

    _detectable_sentences(d, proof, currency)

    if proof.incremental_revenue is not None:
        r, lo, hi = _estimate(d, "incremental_revenue", proof.incremental_revenue, "money", currency)
        d.sentences.append(f"Extra revenue: {r} (likely range {lo} to {hi}).")
        if proof.detectable is not None and proof.detectable.revenue is not None:
            seen = d.add(
                "detectable_revenue",
                proof.detectable.revenue,
                format_money(proof.detectable.revenue, currency),
            )
            d.sentences.append(f"A real revenue gain below about {seen} would not show up yet.")

    # The proof's spend is the agent group's contacts whose results are in. It is not what the budget limit counts
    # (the runtime's ledger: every send and matched discount as it happens, every group), so no "budget left" is
    # derived from it: Live shows the ledger's figure and what is left of the budget.
    spend = d.add("spend", proof.spend, format_money(proof.spend, currency))
    d.sentences.append(
        f"The agent group's contacts whose results are in cost {spend}, messages and the discounts used."
    )

    # No return-on-spend multiple is written. It divides profit by the cost the proof managed to charge, and a
    # discount redeemed under a code the agent did not issue is not charged (ARCHITECTURE_REVIEW section 4 row 4),
    # so the multiple would be larger than the truth. `ProofSummary.roi` is still computed for those who read it.
    d.sentences.append(
        f"It looks after {agent_n} customers; {holdout_n} people we never contact are kept for comparison."
    )
    return d


def _baseline_section(proof: ProofSummary, currency: str) -> SectionDraft:
    d = SectionDraft("baseline", BASELINE_TITLE)
    baseline_n = d.add("baseline_customers", proof.baseline_n, format_count(proof.baseline_n))
    lift = proof.lift_vs_baseline
    if lift is None:
        d.sentences.append(
            f"There are not enough results yet to compare with the simple rule, which looks after "
            f"{baseline_n} customers."
        )
        d.notes.append("The comparison with the simple rule cannot be made yet.")
        return d
    d.add("interval_level", INTERVAL_LEVEL, format_rate(INTERVAL_LEVEL))  # "likely range" is the 95% one
    lift_shown, lo, hi = _estimate(d, "lift_per_customer", lift, "money", currency)
    verdict = _verdict(lift)
    if verdict == "up":
        d.sentences.append(
            f"Compared with the simple rule, it made {lift_shown} more profit per customer "
            f"(likely range {lo} to {hi})."
        )
        d.notes.append("The agent clearly beats the simple rule.")
    elif verdict == "down":
        behind = format_money(abs(lift.point), currency)
        d.sentences.append(
            f"The simple rule is ahead by {behind} of profit per customer (likely range {lo} to {hi})."
        )
        d.notes.append("The simple rule clearly beats the agent; say so plainly.")
    else:
        d.sentences.append(
            f"There is no clear difference from the simple rule yet: the best estimate is {lift_shown} per "
            f"customer, with a likely range from {lo} to {hi}."
        )
        d.notes.append("The range includes zero: no clear difference from the simple rule yet.")
    det = proof.detectable
    if det is not None and det.lift_vs_baseline is not None:
        seen = d.add(
            "detectable_lift_per_customer",
            det.lift_vs_baseline,
            format_money(det.lift_vs_baseline, currency),
        )
        d.sentences.append(
            f"With this many customers, a difference from the simple rule below about {seen} per "
            "customer would not show up yet."
        )
    d.sentences.append(f"The simple rule looks after {baseline_n} customers.")
    return d


def _limit(metric: StopMetric) -> float | None:
    return next((r.threshold for r in DEFAULT_STOP_RULES if r.metric is metric), None)


def _health_section(health: HealthStatus) -> SectionDraft:
    d = SectionDraft("health", HEALTH_TITLE)
    rates = (
        ("delivery_rate", "Delivery rate", health.delivery_rate),
        ("complaint_rate", "Complaint rate", health.complaint_rate),
        ("unsubscribe_rate", "Unsubscribe rate", health.unsubscribe_rate),
        ("block_rate", "Block rate", health.block_rate),
    )
    if health.contacts_per_customer_week is not None:
        c = d.add(
            "contacts_per_customer_week",
            health.contacts_per_customer_week,
            format_ratio(health.contacts_per_customer_week),
        )
        d.sentences.append(f"Each customer got {c} messages a week on average.")
    shown = [f"{label} {d.add(name, v, format_rate(v))}" for name, label, v in rates if v is not None]
    if shown:
        d.sentences.append(". ".join(shown) + ".")

    complaint_limit = _limit(StopMetric.COMPLAINT_RATE)
    if (
        health.complaint_rate is not None
        and complaint_limit is not None
        and health.complaint_rate > complaint_limit
    ):
        shown_limit = d.add("complaint_limit", complaint_limit, format_rate(complaint_limit))
        d.notes.append(f"Complaints are above the safe limit of {shown_limit}; this needs attention.")
    delivery_floor = _limit(StopMetric.DELIVERY_RATE_BELOW)
    if (
        health.delivery_rate is not None
        and delivery_floor is not None
        and health.delivery_rate < delivery_floor
    ):
        shown_floor = d.add("delivery_floor", delivery_floor, format_rate(delivery_floor))
        d.notes.append(f"Delivery is below the safe floor of {shown_floor}; some messages are not arriving.")

    alerts = health.alerts
    critical = sum(1 for a in alerts if a.severity == "critical")
    paused = sum(1 for a in alerts if a.auto_paused)
    n = d.add("alerts", len(alerts), format_count(len(alerts)))
    if not alerts:
        d.sentences.append("No alerts.")
        return d
    k = d.add("critical_alerts", critical, format_count(critical))
    pz = d.add("auto_paused", paused, format_count(paused))
    d.sentences.append(f"Alerts: {n} ({k} critical, {pz} with an automatic pause).")
    for a in alerts:
        d.notes.append(f"Alert ({a.severity}{', paused automatically' if a.auto_paused else ''}): {a.code}.")
    return d


def _facts_section(facts: Mapping[str, float], currency: str) -> SectionDraft | None:
    if not facts:
        return None
    d = SectionDraft("facts", "What else we noticed")
    if any(re.search(r"\d", name) for name in facts):
        d.notes.append("Some fact names contain digits; describe them in words and never write those digits.")
    for name, value in facts.items():
        d.sentences.append(f"{_humanise(name)}: {d.add(name, value, _format_fact(name, value, currency))}.")
    return d


@dataclass(frozen=True)
class LearnedNote:
    """One thing the agent changed as it learned, in words, with the numbers its words use.

    Written by code (the caller knows what moved), e.g. "Free shipping is beating the discount for
    lapsed buyers; the share offered the discount fell from 31% to 18%." with
    `numbers={"discount_share_before": 0.31, "discount_share_now": 0.18}`.
    """

    text: str
    numbers: Mapping[str, float] = field(default_factory=dict)


Learned = Sequence[str | LearnedNote]
"""What the agent learned, as plain lines or as `LearnedNote`s with their numbers."""


def _format_learned(name: str, value: float, currency: str) -> str:
    """A learned number as its words show it: a share or rate as a percent, anything else as a fact."""
    low = name.lower()
    if ("share" in low or "rate" in low) and 0.0 <= value <= 1.0:
        return format_rate(value)
    return _format_fact(name, value, currency)


def _learned_section(learned: Learned, currency: str) -> SectionDraft | None:
    """The "What it learned" lines whose every number is backed; a line with an unchecked number is left out.

    A briefing never carries a number code did not compute. A plain line has no numbers to check a
    digit against, so a plain line with a digit in it is dropped rather than shown; one in words only
    is kept.
    """
    d = SectionDraft("learned", LEARNED_TITLE)
    for i, item in enumerate(learned):
        note = item if isinstance(item, LearnedNote) else LearnedNote(text=str(item))
        text = " ".join(note.text.split())
        if not text or check_numbers(text, note.numbers):
            continue
        for name, value in note.numbers.items():
            key = name if name not in d.numbers else f"{name}_{i}"
            d.numbers[key] = float(value)
            d.shown[key] = _format_learned(name, value, currency)
        d.sentences.append(text if text.endswith((".", "!", "?")) else f"{text}.")
    if not d.sentences:
        return None
    d.notes.append(
        "Say what the agent changed as it learned, in a sentence or two; keep each line's numbers."
    )
    return d


def _template_headline(proof: ProofSummary, health: HealthStatus, money: SectionDraft) -> str:
    """The sentence a marketer wants first: did it make money, and how sure is that (always with the range)."""
    if any(a.severity == "critical" and a.auto_paused for a in health.alerts):
        return "A critical alert paused part of the programme; please take a look."
    profit = proof.incremental_profit
    if profit is None:
        return "Too early to call: results are still coming in."
    p = money.shown["incremental_profit"]
    lo, hi = money.shown["incremental_profit_low"], money.shown["incremental_profit_high"]
    verdict = _verdict(profit)
    if verdict == "up":
        return f"It made an extra {p} in profit compared with people we never contact ({lo} to {hi})."
    if verdict == "down":
        return f"It made less profit than the people we never contact so far ({lo} to {hi})."
    return f"Not sure yet whether it made extra profit: the likely range runs from {lo} to {hi}."


def _early_section(read: EarlyRead, currency: str) -> SectionDraft:
    """The early read (plan-v3 decision 8): a forecast of 90-day margin per customer from their first
    days in the test, agent group against the people we never contact, with the model's own
    backtest. Only built when the early-signal model passed that backtest; the measured results
    confirm it later."""
    d = SectionDraft("early", EARLY_TITLE)
    est = read.difference
    p, lo, hi = _estimate(d, "early_margin_difference", est, "money", currency)
    n_a = d.add("agent_customers", read.n_agent, format_count(read.n_agent))
    n_h = d.add("holdout_customers", read.n_holdout, format_count(read.n_holdout))
    days = d.add("forecast_days", read.horizon_days, format_count(read.horizon_days))
    week = d.add("signal_days", read.signal_days, format_count(read.signal_days))
    start = f"From what they did in their first {week} days in the test"
    later = f"in the {days} days after that"
    verdict = _verdict(est)
    if verdict == "up":
        d.sentences.append(
            f"{start}, customers in the agent group are forecast to bring {p} more margin each {later} "
            f"than people we never contact (likely range {lo} to {hi}; {n_a} and {n_h} customers)."
        )
    elif verdict == "down":
        d.sentences.append(
            f"{start}, customers in the agent group are forecast to bring less margin {later} than people "
            f"we never contact (likely range {lo} to {hi}; {n_a} and {n_h} customers)."
        )
    else:
        d.sentences.append(
            f"{start}, there is no clear difference yet in forecast margin {later} from people we never "
            f"contact: the likely range runs from {lo} to {hi} per customer ({n_a} and {n_h} customers)."
        )
    band = d.add("early_band", read.band, format_rate(read.band))
    within = d.add("early_months_within", read.cohorts_within, format_count(read.cohorts_within))
    checked = d.add("early_months_checked", read.cohorts_checked, format_count(read.cohorts_checked))
    d.sentences.append(
        f"On this shop's own past customers, its forecasts were within {band} of what happened for "
        f"{within} of {checked} months."
    )
    d.sentences.append("This is a forecast; the measured results will confirm or correct it.")
    d.notes.append("This is a forecast from first-week behaviour, not a measured result; say so.")
    return d


def build_sections(
    goal: Goal | None,
    proof: ProofSummary,
    health: HealthStatus,
    facts: Mapping[str, float],
    *,
    learned: Learned = (),
    early: EarlyRead | None = None,
) -> list[SectionDraft]:
    """The briefing's sections with every number, computed by code: money, the simple rule, health,
    then the early read, what it learned and other facts when there are any."""
    currency = goal.currency if goal else "USD"
    drafts = [
        _money_section(proof, currency),
        _baseline_section(proof, currency),
        _health_section(health),
    ]
    if early is not None:
        drafts.append(_early_section(early, currency))
    lesson = _learned_section(learned, currency)
    if lesson is not None:
        drafts.append(lesson)
    extra = _facts_section(facts, currency)
    if extra is not None:
        drafts.append(extra)
    return drafts


def template_briefing(
    goal: Goal | None,
    proof: ProofSummary,
    health: HealthStatus,
    facts: Mapping[str, float],
    period: Period,
    now: datetime,
    *,
    learned: Learned = (),
    early: EarlyRead | None = None,
    briefing_id: str | None = None,
) -> Briefing:
    """The briefing written entirely by code, with no model: what the Analyst falls back to, and what
    the API rebuilds a stored briefing with when the measurement it serves has moved on."""
    drafts = build_sections(goal, proof, health, facts, learned=learned, early=early)
    goal_id = goal.goal_id if goal else proof.goal_id
    return Briefing(
        briefing_id=briefing_id or stable_id("brf", goal_id or "all", period, now.isoformat()),
        goal_id=goal_id,
        as_of=now,
        period=period,
        headline=_template_headline(proof, health, drafts[0]),
        sections=tuple(d.section() for d in drafts),
    )


# --- what the model writes ------------------------------------------------------------------------


class SectionText(BaseModel):
    key: str = Field(description="The section key, exactly as given")
    body: str = Field(min_length=1, max_length=1200, description="Two to four short sentences")


class BriefingText(BaseModel):
    """The only part of a briefing the model writes: words around numbers code chose."""

    headline: str = Field(min_length=1, max_length=200)
    sections: list[SectionText] = Field(min_length=1)


class BriefingResult(Contract):
    """A briefing plus how it was written, for evals and the operator view."""

    briefing: Briefing
    source: BriefingSource
    problems: tuple[str, ...] = Field(default=(), description="What was wrong with the model's drafts")


def _briefing_prompt(goal: Goal | None, period: Period, drafts: Sequence[SectionDraft]) -> str:
    lines = [
        f"Goal: {goal.text if goal else 'The whole programme'}",
        f"Briefing: {period}",
        "",
        "Write a headline and one body for each section below.",
        "Lead with what the marketer most wants to know: did it make extra profit, and how sure is that.",
        'Call the comparison group "people we never contact", never "the holdout".',
        "Whenever you give a money estimate, give its likely range with it.",
    ]
    for d in drafts:
        lines += [
            "",
            f"Section key: {d.key}",
            f"Title: {d.title}",
            "Numbers (use only these, written as shown):",
        ]
        lines += [f"- {name} = {d.numbers[name]!r} (write it as {d.shown[name]})" for name in d.shown]
        if d.notes:
            lines.append("Notes:")
            lines += [f"- {note}" for note in d.notes]
    return "\n".join(lines)


def find_problems(text: BriefingText, drafts: Sequence[SectionDraft]) -> list[str]:
    """Everything wrong with a model draft: missing sections and numbers code did not compute."""
    problems: list[str] = []
    bodies = {s.key: s.body for s in text.sections}
    every: dict[str, float] = {}
    for d in drafts:
        every.update({f"{d.key}.{k}": v for k, v in d.numbers.items()})
        body = bodies.get(d.key)
        if body is None:
            problems.append(f"section '{d.key}' is missing")
            continue
        problems += [
            f"section '{d.key}': '{bad}' is not one of its numbers" for bad in check_numbers(body, d.numbers)
        ]
    problems += [
        f"headline: '{bad}' is not one of the numbers" for bad in check_numbers(text.headline, every)
    ]
    return problems


# --- opportunities --------------------------------------------------------------------------------


class SegmentSummary(BaseModel):
    """What the Analyst needs about one segment. Extra keys in the caller's summary are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    customers: int = Field(ge=0)
    contactable: int | None = Field(default=None, ge=0, description="Reachable with consent; None: unknown")
    avg_value: float = Field(default=0.0, description="Past revenue per customer, in the business currency")
    margin_rate: float | None = Field(default=None, ge=0, le=1, description="Gross margin share, if known")


@dataclass(frozen=True)
class Play:
    """A rule: which segments suggest which goal, and the planning numbers behind its budget."""

    kind: GoalKind
    segments: tuple[str, ...]
    verb: str
    response_rate: float
    """A planning assumption for the share who respond, not a promise; the holdout measures the truth."""
    min_customers: int
    days: int


PLAYS: tuple[Play, ...] = (
    Play(GoalKind.WIN_BACK, ("lapsing", "lapsed", "dormant", "drifting"), "Win back", 0.05, 100, 30),
    Play(GoalKind.RETENTION, ("at_risk", "churn_risk", "slipping"), "Keep", 0.04, 100, 30),
    Play(
        GoalKind.REPEAT_PURCHASE,
        ("one_time", "one_time_buyers", "single_purchase", "new_buyers"),
        "Win a second order from",
        0.06,
        100,
        30,
    ),
    Play(
        GoalKind.CART_RECOVERY, ("cart_abandoners", "open_carts", "abandoned_carts"), "Recover", 0.10, 20, 7
    ),
)

DEFAULT_MARGIN_RATE = 0.4
"""Used when a segment's margin is unknown; a typical retail gross margin."""

BUDGET_SHARE = 0.3
"""The share of the expected extra margin we suggest spending to earn it."""

MIN_BUDGET = 50.0
"""Below this a goal is not worth a marketer's attention."""

OPEN_STATUSES = (GoalStatus.DRAFT, GoalStatus.ACTIVE, GoalStatus.PAUSED)


def _segment_key(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def _round_budget(value: float) -> float:
    """To two significant figures, so a suggestion reads like one ($1,400, not $1,368.21)."""
    if value <= 0:
        return 0.0
    if value < 100:
        return float(round(value / 10.0) * 10)
    digits = len(str(int(value))) - 2
    step = 10.0**digits
    return float(round(value / step) * step)


def suggest_budget(segment: SegmentSummary, play: Play) -> float:
    """reach × past value × margin × planned response × budget share, rounded. All code, no LLM."""
    reach = segment.contactable if segment.contactable is not None else segment.customers
    margin = segment.margin_rate if segment.margin_rate is not None else DEFAULT_MARGIN_RATE
    return _round_budget(reach * max(segment.avg_value, 0.0) * margin * play.response_rate * BUDGET_SHARE)


# --- the agent ------------------------------------------------------------------------------------


class Analyst:
    def __init__(self, llm: LLMClient, *, model_id: str | None = None) -> None:
        self.llm = llm
        self.model_id = model_id

    def sections(
        self,
        goal: Goal | None,
        proof: ProofSummary,
        health: HealthStatus,
        facts: Mapping[str, float],
        *,
        learned: Learned = (),
        early: EarlyRead | None = None,
    ) -> list[SectionDraft]:
        """The briefing's sections with every number, computed by code."""
        return build_sections(goal, proof, health, facts, learned=learned, early=early)

    def briefing(
        self,
        goal: Goal | None,
        proof: ProofSummary,
        health: HealthStatus,
        facts: Mapping[str, float],
        period: Period,
        now: datetime,
        *,
        learned: Learned = (),
        early: EarlyRead | None = None,
    ) -> Briefing:
        """The briefing a marketer reads. Never raises for bad or missing model output; code fills in."""
        return self.write_briefing(
            goal, proof, health, facts, period, now, learned=learned, early=early
        ).briefing

    def write_briefing(
        self,
        goal: Goal | None,
        proof: ProofSummary,
        health: HealthStatus,
        facts: Mapping[str, float],
        period: Period,
        now: datetime,
        *,
        learned: Learned = (),
        early: EarlyRead | None = None,
    ) -> BriefingResult:
        """As `briefing`, also saying whether the model's words or the template were used, and why."""
        drafts = self.sections(goal, proof, health, facts, learned=learned, early=early)
        goal_id = goal.goal_id if goal else proof.goal_id
        briefing_id = stable_id("brf", goal_id or "all", period, now.isoformat())
        system = load_prompt("analyst")
        prompt = _briefing_prompt(goal, period, drafts)
        seen: list[str] = []
        for attempt in (0, 1):
            ask = prompt
            if attempt == 1:
                ask += (
                    "\n\nYour previous briefing had these problems:\n"
                    + "\n".join(f"- {p}" for p in seen)
                    + "\nWrite it again using only the numbers listed, written as shown."
                )
            try:
                text = ask_json(self.llm, BriefingText, system=system, prompt=ask, model_id=self.model_id)
            except AgentOutputError as err:
                seen.append(f"unreadable answer: {err}")
                continue
            except Exception as err:  # P4 LLM unavailable: the template briefing, every number from code
                why = err.code if isinstance(err, LLMError) else type(err).__name__
                seen.append(f"the model could not be reached ({why})")
                break
            problems = find_problems(text, drafts)
            if not problems:
                bodies = {s.key: s.body.strip() for s in text.sections}
                return BriefingResult(
                    briefing=Briefing(
                        briefing_id=briefing_id,
                        goal_id=goal_id,
                        as_of=now,
                        period=period,
                        headline=text.headline.strip(),
                        sections=tuple(d.section(bodies[d.key]) for d in drafts),
                    ),
                    source="llm" if attempt == 0 else "llm_retry",
                    problems=tuple(seen),
                )
            seen += problems
        return BriefingResult(
            briefing=template_briefing(
                goal, proof, health, facts, period, now, learned=learned, early=early, briefing_id=briefing_id
            ),
            source="template",
            problems=tuple(seen),
        )

    def opportunities(
        self,
        states_summary: Mapping[str, SegmentSummary | Mapping[str, float]],
        now: datetime,
        *,
        business_id: str = "default",
        currency: str = "USD",
        existing: Sequence[Goal] = (),
        max_goals: int = 3,
    ) -> list[Goal]:
        """DRAFT goals for segments worth a campaign, biggest suggested budget first, one per kind. No LLM.

        `states_summary` maps a segment name ('lapsing', 'at_risk', 'one_time', 'cart_abandoners', ...)
        to its size and past value. A kind of goal already open in `existing` is not proposed again.
        """
        taken = {g.kind for g in existing if g.status in OPEN_STATUSES}
        found: list[tuple[float, str, Goal]] = []
        for raw_name, raw in states_summary.items():
            name = _segment_key(raw_name)
            play = next((p for p in PLAYS if name in p.segments), None)
            if play is None or play.kind in taken:
                continue
            seg = raw if isinstance(raw, SegmentSummary) else SegmentSummary.model_validate(dict(raw))
            reach = seg.contactable if seg.contactable is not None else seg.customers
            if reach < play.min_customers or seg.avg_value <= 0:
                continue
            budget = suggest_budget(seg, play)
            if budget < MIN_BUDGET:
                continue
            label = name.replace("_", " ")
            text = (
                f"{play.verb} the {format_count(reach)} {label} customers (about "
                f"{format_money(seg.avg_value, currency)} each in past spend); suggested budget "
                f"{format_money(budget, currency)} over {play.days} days."
            )
            goal = Goal(
                goal_id=stable_id("goal", "opportunity", play.kind.value, name, now.date().isoformat()),
                business_id=business_id,
                text=text,
                kind=play.kind,
                objective=Objective.GROSS_PROFIT,
                budget=budget,
                currency=currency,
                starts_at=now,
                ends_at=now + timedelta(days=play.days),
                autonomy=AutonomyLevel.SHADOW,
                status=GoalStatus.DRAFT,
                created_at=now,
            )
            found.append((budget, name, goal))
        found.sort(key=lambda t: (-t[0], t[1]))
        chosen: list[Goal] = []
        for _, _, goal in found:
            if goal.kind not in {g.kind for g in chosen} and len(chosen) < max_goals:
                chosen.append(goal)
        return chosen
