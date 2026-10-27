"""A compact count of what an agent decided and why, printed as a "DIAG " block (docs/v3/DIAGNOSE.md).

CI logs are the only output anyone can read, so the Gym CLI and the real bench print one block per run
from a `DecisionTally`: it is fed every decision the agent makes for the agent group, counts them by kind,
by the reason code of each "do nothing" and by every `blocked_by` code, and records which v3 part held a
customer where the decision says so:

* journeys: a "do nothing" that has a `journey_id` (a customer held between steps), and every
  `journey_ended` code (`never_read`, `no_longer_pays`...);
* frequency: `frequency_plan` in `blocked_by`;
* the weekly cap: `weekly_cap` is one code for two things, the hard cap of the briefs and the agent's own weekly
  budget (a customer's rhythm), so a hold is split by whether the customer's contacts in 7 days had reached the
  cap (`weekly_cap_hold_at_cap`) or were still below it (`weekly_cap_hold_below_cap`: the rhythm, or a send
  already scheduled but not yet on record);
* who and offers leave nothing on the decision (a customer the who book leaves alone reaches the policy as
  "no bids"; the offer price is only in the bids' costs), so `part_snapshot` reads their counters off the
  built policy after the run instead: customers left alone, the offer budget price, journey events.

The tally never changes a decision and never reads the world's truth. `RunRecorder` watches a policy for
the Gym (through `autonomy.gym.score.observing`); the bench feeds its decision loop directly.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from autonomy.contracts import CustomerState, Decision, DecisionContext, DecisionKind
from autonomy.decide import rules
from autonomy.decide.explain import BLOCK_ORDER

__all__ = ["DecisionTally", "RunRecorder", "format_block", "part_snapshot", "reason_code"]

PREFIX = "DIAG "

_STEMS: tuple[tuple[str, str], ...] = (
    ("Left alone this time: testing whether a rest", "rest_draw"),
    ("Left alone this time: resting is still being tested", "explore_rest"),
    ("Left alone this time: not sure enough yet", "restraint_hold"),
    ("Left alone: no message is worth more than the cost of contacting (", "price_beats_value"),
    ("Left alone: no message is worth more than the cost of contacting.", "net_not_positive"),
    ("Left alone: we are slowing down", "pacing"),
    ("Left alone: no active goal has anything", "no_bids"),
)
"""The first words of the "do nothing" sentences `autonomy.decide.explain` writes when no hard rule decided
it, and the short code each stands for. `rest_draw`: the auction drew "do nothing" although a message was
worth sending; `explore_rest`: the exploration floor chose rest; `restraint_hold`: restraint kept the best
message out of the draw; `price_beats_value`: the best message earns something but not more than the
attention price; `net_not_positive`: no message earns more than its own channel and offer cost, price aside; `no_bids`: the bidders
offered nothing (no goal, or a customer left alone before the auction: who, harm, pruning)."""


def reason_code(d: Decision) -> str:
    """One short code for why the decision is what it is: "act", or the cause of a "do nothing"."""
    if d.kind is DecisionKind.ACT:
        return "act"
    if d.journey_id is not None:
        return "journey_hold"
    blocked = set(d.blocked_by)
    if "experiment_arm" in blocked:
        return "experiment_arm"
    for code in (*BLOCK_ORDER, *rules.ALL_CODES):
        if code in blocked:
            return f"blocked:{code}"
    if blocked:
        return f"blocked:{sorted(blocked)[0]}"
    for stem, code in _STEMS:
        if d.reason.startswith(stem):
            return code
    return "other"


@dataclass
class DecisionTally:
    """Counts over the decisions an agent made for the agent group."""

    kinds: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    blocked: Counter[str] = field(default_factory=Counter)
    """Every `blocked_by` code on any decision (an ACT carries the codes of the options that were removed)."""
    parts: Counter[str] = field(default_factory=Counter)
    channels: Counter[str] = field(default_factory=Counter)
    expected_net_sum: float = 0.0
    attention_price_sum: float = 0.0

    def add(
        self, d: Decision, state: CustomerState | None = None, ctx: DecisionContext | None = None
    ) -> None:
        """Count one decision. With the `state` it was made on and its `ctx`, a `weekly_cap` hold is also split
        by whether the customer had reached the briefs' cap: the code is shared by the hard cap and by the
        agent's own weekly budget (a customer's rhythm), which holds earlier."""
        self.kinds[d.kind.value] += 1
        code = reason_code(d)
        self.reasons[code] += 1
        for b in d.blocked_by:
            self.blocked[b] += 1
        if d.kind is DecisionKind.ACT and d.action is not None:
            self.expected_net_sum += float(d.expected_net)
            self.attention_price_sum += float(d.attention_price)
            self.channels[d.action.channel.value] += 1
            if d.journey_id is not None:
                self.parts[
                    "journey_first_step" if d.action.follow_up_step == 0 else "journey_later_step"
                ] += 1
        if (
            rules.WEEKLY_CAP in d.blocked_by
            and d.kind is DecisionKind.NOTHING
            and state
            and ctx
            and ctx.briefs
        ):
            cap = min(b.envelope.max_contacts_per_week for b in ctx.briefs)
            at_cap = state.contacts_7d >= cap
            self.parts["weekly_cap_hold_at_cap" if at_cap else "weekly_cap_hold_below_cap"] += 1
        if rules.FREQUENCY_PLAN in d.blocked_by and d.kind is DecisionKind.NOTHING:
            self.parts["frequency_plan_hold"] += 1
        if code == "journey_hold":
            self.parts["journey_hold"] += 1
        if d.journey_ended:
            self.parts[f"journey_ended:{d.journey_ended.rpartition(':')[2]}"] += 1

    def merge(self, other: DecisionTally) -> None:
        for mine, theirs in (
            (self.kinds, other.kinds),
            (self.reasons, other.reasons),
            (self.blocked, other.blocked),
            (self.parts, other.parts),
            (self.channels, other.channels),
        ):
            mine.update(theirs)
        self.expected_net_sum += other.expected_net_sum
        self.attention_price_sum += other.attention_price_sum

    @property
    def total(self) -> int:
        return sum(self.kinds.values())

    @property
    def acts(self) -> int:
        return self.kinds.get(DecisionKind.ACT.value, 0)

    @property
    def nothing(self) -> int:
        return self.total - self.acts

    @property
    def mean_expected_net(self) -> float | None:
        """The mean `expected_net` of the chosen actions (the agent's own estimate, before the attention price)."""
        return self.expected_net_sum / self.acts if self.acts else None

    @property
    def mean_attention_price(self) -> float | None:
        return self.attention_price_sum / self.acts if self.acts else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "act": self.acts,
            "nothing": self.nothing,
            "reasons": dict(self.reasons.most_common()),
            "blocked_by": dict(self.blocked.most_common()),
            "parts": dict(self.parts.most_common()),
            "channels": dict(self.channels.most_common()),
            "mean_expected_net": self.mean_expected_net,
            "mean_attention_price": self.mean_attention_price,
        }


class RunRecorder:
    """A Gym decision observer (`autonomy.gym.score.observing`) that tallies one policy's decisions.

    It counts the decisions of the run's policy named `policy` for the agent group, over every run of it (every
    seed), and keeps the last agent it saw so `part_snapshot` can read its counters after the run. It changes
    nothing: the scoreboard shows it each decision after the policy has made it."""

    def __init__(self, policy: str) -> None:
        self.policy = policy
        self.tally = DecisionTally()
        self.agent: Any = None
        self.runs = 0

    def __call__(
        self, name: str, agent: Any, decision: Decision, state: CustomerState, ctx: DecisionContext
    ) -> None:
        if name != self.policy:
            return
        if agent is not self.agent:
            self.agent = agent
            self.runs += 1
        self.tally.add(decision, state, ctx)


def part_snapshot(policy: Any) -> dict[str, object]:
    """What the v3 parts counted, read off a built agent after a run (public attributes only; a part that is
    off or an attribute that is missing leaves its key out)."""
    out: dict[str, object] = {}
    who = getattr(policy, "who", None)
    if who is not None:
        out["who_left_alone"] = who.traits.left_alone()
        out["who_coverage"] = who.coverage()
    planner = getattr(policy, "planner", None)
    if planner is not None:
        out["frequency_plans_chosen"] = dict(planner.chosen)
    runner = getattr(getattr(policy, "policy", None), "journeys", None)
    if runner is not None:
        events: Counter[str] = Counter()
        for e in runner.history:
            events[f"{e.kind}:{e.code}" if e.code else e.kind] += 1
        out["journey_events"] = dict(events.most_common())
    prices = getattr(policy, "offer_prices", None)
    if prices:
        out["offer_budget_price"] = {goal: round(float(p()), 4) for goal, p in prices.items()}
    return out


def _counts(counter: Mapping[str, int], limit: int = 14) -> str:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return " ".join(f"{k}={v}" for k, v in items[:limit]) or "none"


def format_block(
    label: str,
    tally: DecisionTally,
    *,
    changed: Mapping[str, object] | None = None,
    snapshot: Mapping[str, object] | None = None,
    extra: Mapping[str, object] | None = None,
) -> str:
    """The block for one run: every line starts with "DIAG ".

    `label` names the run (scenario or dataset and policy). `changed` is the switches that differ from the
    plain agent (`ablation.changed_switches`), `snapshot` the parts' own counters (`part_snapshot`), and
    `extra` the run's own figures (contacts per customer-week, realised value per action...) as key=value
    pairs on one line."""
    total = tally.total
    share = tally.acts / total if total else 0.0
    nothing_reasons = {k: v for k, v in tally.reasons.items() if k != "act"}
    lines = [
        f"run {label}",
        "switches differing from the plain agent: "
        + (
            "n/a (not the agent)"
            if changed is None
            else ", ".join(f"{k}={v}" for k, v in changed.items()) or "none"
        ),
        f"decisions total={total} act={tally.acts} ({share:.1%}) nothing={tally.nothing}",
        "nothing by reason " + _counts(nothing_reasons),
        "blocked_by (any decision) " + _counts(tally.blocked),
        "holds and runs by part " + _counts(tally.parts),
    ]
    if snapshot:
        lines.append("part counters " + "; ".join(f"{k}={v}" for k, v in snapshot.items()))
    net = tally.mean_expected_net
    price = tally.mean_attention_price
    figures = {
        "mean_expected_net_chosen": "n/a" if net is None else f"{net:.4f}",
        "mean_attention_price_chosen": "n/a" if price is None else f"{price:.4f}",
        **(extra or {}),
    }
    lines.append("figures " + " ".join(f"{k}={v}" for k, v in figures.items()))
    return "\n".join(PREFIX + line for line in lines)
