"""Channel journeys (plan-v3 section 4, decision 4): a short plan of messages per customer.

A journey is an ordered list of steps, each a channel, a wait since the step before and a condition
(`configs/v3/journeys.yaml`): "A: email only", "B: email, wait 3 days, WhatsApp if no purchase",
"C: WhatsApp now, only for customers above a value bar", "D: WhatsApp, then an email reminder".

Opt-in. `AutonomousPolicy(journeys=JourneyRunner(load_journeys()))` switches the layer on; with
`journeys=None` (the default) the policy does exactly what it did before.

**Choosing a journey.** `JourneyRunner.expand` turns the bids the goal bidders offered into journey
bids, one per first-step bid and journey that starts on its channel. A journey is one option in the
auction, priced by what the whole plan is expected to bring minus everything it costs:

    net = first step's net  +  sum over later steps of  P(step still runs) x (that step's net)

`P(step still runs)` is the chance the customer has not bought after the steps before it:
`prod (1 - uplift)` over them. A later step's uplift is multiplied by `follow_up_uplift_factor`
(a second message does less than a first), it is priced as a plain reminder (the offer, if any, is
paid once, in the first step), and its channel cost is paid only when it runs. The bid's uplift,
channel cost, offer cost and spread are combined the same way. Organic purchases are not modelled
here, so the chance a step is reached is slightly overstated; the step's gain and its cost are
overstated together, so the sign of its net does not change.

Channels whose customer has been sent `never_open_after_sends` messages without a click or a reply
count as never read (`ChannelStats` keeps no open count): a journey that would use such a channel is
not offered, and a step that falls on one while a journey runs is skipped.

The chosen option's journey is `Action.journey_id`. The decision's `journey_probability` is the
chance the policy had of choosing that journey, summed over every option of it (offers, content,
formats), so evaluating journeys off-policy works at the level the agent chose at. Later steps are
not drawn again: they carry `probability` 1.0 (given the journey) and the first step's
`journey_probability`.

**Running a journey.** `JourneyRunner.advance` is asked first, each time the policy decides for a
customer in a sweep. It stops the journey at once on a purchase since the first decision, an
unsubscribe or stop reply on any channel of the journey, or an open complaint. It skips steps on
never-read channels. Otherwise the next step is due when its wait has passed (looking ahead by
`lookahead_hours`, so a daily sweep does not make every step a day late). A step whose condition no
longer holds ends the journey. A due step is quoted afresh by the goal's bidder, timed (quiet hours
in the customer's own time zone) and passed through every hard rule, `rules.check_bid` and the gap
between messages, exactly like a first message. What a blocked step does depends on the rule:

* ends the journey: holdout, unsubscribed, open complaint, no brief;
* skips the step (the next one still runs): no consent, no address, channel or offer not allowed;
* waits for the next sweep (for up to `max_defer_days`, then ends): weekly cap, too soon, snooze,
  no budget, quiet hours.

While a journey is mid-way the customer is not put through the auction again in the daily sweep
(`hold_during_journey`): one plan at a time. Cart and reply triggers still reach the auction.

The runner keeps its runs in memory, like the policy keeps its pending sends. Every run is rebuilt
from the decision log by `rebuild`, since a run is made of its decisions.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import AwareDatetime, Field, field_validator, model_validator

from autonomy.contracts import (
    NO_OFFER,
    Bid,
    Channel,
    ChannelStats,
    Contract,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
)
from autonomy.decide import rules
from autonomy.interfaces import Bidder

DEFAULT_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "configs" / "v3" / "journeys.yaml"

# Why a journey ended (`JourneyEvent.code`). Rule codes from `autonomy.decide.rules` are reused as they are.
PURCHASED = "purchased"
UNSUBSCRIBED = "unsubscribed"
COMPLAINT = "open_complaint"
CONDITION_FAILED = "condition_failed"
NOTHING_TO_OFFER = "nothing_to_offer"
NO_LONGER_PAYS = "no_longer_pays"
EXPIRED = "expired"
CANCELLED = "cancelled"
COMPLETED = "completed"
LEFT_AGENT_GROUP = "left_agent_group"
NEVER_READ = "never_read"

_STOP_RULES: Final[frozenset[str]] = frozenset(
    {rules.HOLDOUT, rules.UNSUBSCRIBED, rules.OPEN_COMPLAINT, rules.NO_BRIEF}
)
_SKIP_RULES: Final[frozenset[str]] = frozenset(
    {
        rules.NO_CONSENT,
        rules.NO_ADDRESS,
        rules.CHANNEL_NOT_ALLOWED,
        rules.OFFER_NOT_ALLOWED,
        rules.OFFER_NEEDS_APPROVAL,
        rules.NO_DISCOUNTS,
    }
)

_CHANNEL_NAME: Final[Mapping[str, str]] = {
    "email": "email",
    "whatsapp": "WhatsApp",
    "sms": "SMS",
    "push": "a push notification",
    "web": "a web message",
    "webhook": "the business's own tool",
}


def channel_name(channel: Channel) -> str:
    """How a channel reads inside a sentence."""
    return _CHANNEL_NAME.get(channel.value, channel.value)


# --- the definition (what the YAML holds) ------------------------------------------------------


class StepCondition(StrEnum):
    """When a later step runs. A purchase ends every journey whatever the condition."""

    ALWAYS = "always"
    NO_PURCHASE = "no_purchase"
    NOT_OPENED = "not_opened"
    """The customer has not clicked or replied since the step before went out."""


class JourneyStep(Contract):
    channel: Channel
    wait_days: float = Field(default=0.0, ge=0, le=90, description="Days after the step before")
    when: StepCondition = StepCondition.ALWAYS


class Journey(Contract):
    journey_id: str = Field(min_length=1)
    name: str
    steps: tuple[JourneyStep, ...] = Field(min_length=1)
    value_bar: float | None = Field(
        default=None, ge=0, description="Only offered to customers worth at least this if they buy"
    )

    @model_validator(mode="after")
    def _first_step_goes_now(self) -> Journey:
        first = self.steps[0]
        if first.wait_days != 0 or first.when is not StepCondition.ALWAYS:
            raise ValueError(f"journey {self.journey_id}: the first step starts at once, with no condition")
        return self

    @property
    def channels(self) -> tuple[Channel, ...]:
        return tuple(dict.fromkeys(s.channel for s in self.steps))

    def total_wait_days(self) -> float:
        return sum(s.wait_days for s in self.steps)

    def window_days(self, horizon_days: int) -> float:
        """How long after the first message a purchase counts for the journey: the waits of all
        steps plus the brief's outcome window after the last one."""
        return self.total_wait_days() + horizon_days

    def describe(self) -> str:
        """One plain phrase: "email now, then WhatsApp after 3 days if they have not bought"."""
        parts: list[str] = []
        for i, step in enumerate(self.steps):
            name = channel_name(step.channel)
            if i == 0:
                parts.append(f"{name} now")
                continue
            days = f"{step.wait_days:g} day{'' if step.wait_days == 1 else 's'}"
            tail = {
                StepCondition.ALWAYS: "",
                StepCondition.NO_PURCHASE: " if they have not bought",
                StepCondition.NOT_OPENED: " if they have not opened it",
            }[step.when]
            parts.append(f"{'then ' if i == 1 else ''}{name} after {days}{tail}")
        return ", ".join(parts)


class JourneySettings(Contract):
    follow_up_uplift_factor: float = Field(
        default=0.5, ge=0, le=1, description="A later step's uplift is this share of a first message's"
    )
    never_open_after_sends: int = Field(
        default=5,
        ge=0,
        description="Messages with no click or reply after which a channel is skipped (0: never)",
    )
    lookahead_hours: float = Field(default=24.0, ge=0, description="A step this close is due in this sweep")
    max_defer_days: float = Field(default=2.0, ge=0, description="How long a blocked step may wait")
    hold_during_journey: bool = Field(
        default=True, description="No new auction for a customer mid-journey in the daily sweep"
    )
    require_positive_net: bool = Field(
        default=True, description="A step whose fresh price no longer beats its cost is not sent"
    )


class JourneyConfig(Contract):
    settings: JourneySettings = JourneySettings()
    journeys: tuple[Journey, ...] = Field(min_length=1)

    @field_validator("journeys")
    @classmethod
    def _unique_ids(cls, value: tuple[Journey, ...]) -> tuple[Journey, ...]:
        ids = [j.journey_id for j in value]
        if len(set(ids)) != len(ids):
            raise ValueError("journey ids must be unique")
        return value

    def get(self, journey_id: str) -> Journey | None:
        return next((j for j in self.journeys if j.journey_id == journey_id), None)

    def with_value_bar(self, journey_id: str, bar: float | None) -> JourneyConfig:
        """The same config with one journey's value bar replaced (the bar is in the shop's currency)."""
        if self.get(journey_id) is None:
            raise KeyError(journey_id)
        return self.model_copy(
            update={
                "journeys": tuple(
                    j.model_copy(update={"value_bar": bar}) if j.journey_id == journey_id else j
                    for j in self.journeys
                )
            }
        )


def parse_journeys(raw: Mapping[str, Any]) -> JourneyConfig:
    """A `JourneyConfig` from the parsed YAML: `{"settings": {...}, "journeys": [{"id": ..., ...}]}`."""
    entries = raw.get("journeys")
    if not isinstance(entries, Sequence) or isinstance(entries, str) or not entries:
        raise ValueError("journeys.yaml needs a non-empty 'journeys' list")
    journeys: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("each journey is a mapping with an id and steps")
        fields = dict(entry)
        if "id" in fields:
            fields["journey_id"] = fields.pop("id")
        journeys.append(fields)
    return JourneyConfig.model_validate({"settings": raw.get("settings") or {}, "journeys": journeys})


def load_journeys(path: Path | None = None) -> JourneyConfig:
    """The journeys in `path` (default: the shipped `configs/v3/journeys.yaml`)."""
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("journeys.yaml must hold a mapping")
    return parse_journeys(raw)


# --- one customer's journey in progress -------------------------------------------------------


class JourneyRun(Contract):
    """Where one customer is in one journey. Made from the journey's decisions (`JourneyRunner.record`)."""

    customer_id: str
    journey_id: str
    goal_id: str
    first_decision_id: str
    last_decision_id: str
    started_at: AwareDatetime = Field(
        description="When the first decision was made; a purchase after it ends the run"
    )
    probability: float = Field(gt=0, le=1, description="The first decision's `journey_probability`")
    next_step: int = Field(ge=1, description="Index of the next step to run")
    base_at: AwareDatetime = Field(description="The next step's wait counts from here")
    last_sent_at: AwareDatetime
    last_sent_channel: Channel


class TurnKind(StrEnum):
    NONE = "none"  # the customer has no journey in progress
    STOPPED = "stopped"  # the journey ended in this call
    WAIT = "wait"  # a journey is in progress and its next step is not due yet
    STEP = "step"  # the next step is due


@dataclass(frozen=True)
class Turn:
    kind: TurnKind
    run: JourneyRun | None = None
    journey: Journey | None = None
    step: JourneyStep | None = None
    due_at: datetime | None = None
    code: str = ""
    """For STOPPED, why (a code above)."""


@dataclass(frozen=True)
class JourneyEvent:
    """One thing that happened to a journey, for the log and the briefing."""

    customer_id: str
    journey_id: str
    step: int
    kind: str  # started, sent, skipped, deferred, stopped, completed
    code: str
    at: datetime


class Verdict(StrEnum):
    """What a blocked step does (`JourneyRunner.blocked`)."""

    DEFER = "defer"
    SKIP = "skip"
    STOP = "stop"


def never_read(stats: ChannelStats | None, after_sends: int) -> bool:
    """Whether the customer has been sent `after_sends` messages here without a click or a reply.

    `ChannelStats` keeps no count of opens, so clicks and replies are the evidence of reading."""
    if stats is None or after_sends <= 0:
        return False
    return (
        stats.sends >= after_sends
        and stats.clicks == 0
        and stats.replies == 0
        and stats.last_engaged_at is None
    )


def _clamp(x: float, low: float, high: float) -> float:
    return max(low, min(high, x))


class JourneyRunner:
    """Prices journeys as auction options and carries each customer's journey from step to step."""

    def __init__(self, config: JourneyConfig, *, history: int = 50_000) -> None:
        self.config = config
        self.settings = config.settings
        self._runs: dict[str, JourneyRun] = {}
        self._undo: dict[str, tuple[str, JourneyRun | None]] = {}
        """customer -> (the last journey decision, the run before it), so deciding the same moment again
        replaces that decision instead of moving the run twice."""
        self.history: deque[JourneyEvent] = deque(maxlen=history)

    # --- choosing: journeys as bids ---------------------------------------------------------------

    def expand(self, offered: Sequence[Bid], state: CustomerState) -> list[Bid]:
        """The bids the auction sees: the offered ones, with every bid on a journey's first channel
        replaced by one bid per journey that starts there (and that this customer may have).

        A bid on a channel no journey starts on passes through unchanged. A journey is left out when
        a channel of it is one the customer never reads, when the bid's value is below the journey's
        bar, or when the goal has no bid on one of its later channels."""
        starts = {j.steps[0].channel for j in self.config.journeys}
        pool: dict[tuple[str, Channel], list[Bid]] = {}
        for bid in offered:
            pool.setdefault((bid.goal_id, bid.action.channel), []).append(bid)
        out = [b for b in offered if b.action.channel not in starts]
        limit = self.settings.never_open_after_sends
        for journey in self.config.journeys:
            if any(never_read(state.channels.get(ch), limit) for ch in journey.channels):
                continue
            for first in offered:
                if first.action.channel is not journey.steps[0].channel:
                    continue
                priced = self._price(journey, first, pool)
                if priced is not None:
                    out.append(priced)
        return out

    def _price(
        self, journey: Journey, first: Bid, pool: Mapping[tuple[str, Channel], list[Bid]]
    ) -> Bid | None:
        if journey.value_bar is not None and first.value_if_converted < journey.value_bar:
            return None
        factor = self.settings.follow_up_uplift_factor
        uplift, offer_cost, channel_cost = first.uplift, first.offer_cost, first.channel_cost
        net, variance = first.expected_net, first.uncertainty**2
        reach = 1.0 - _clamp(first.uplift, 0.0, 1.0)  # chance the customer has not bought yet
        for step in journey.steps[1:]:
            quotes = pool.get((first.goal_id, step.channel))
            if not quotes:
                return None
            later = _reminder(quotes)
            gain = factor * later.uplift if later.uplift > 0 else later.uplift
            uplift += reach * gain
            offer_cost += reach * later.offer_cost
            channel_cost += reach * later.channel_cost
            net += (
                reach
                * later.urgency
                * (gain * later.value_if_converted - later.offer_cost - later.channel_cost)
            )
            variance += (reach * factor * later.uncertainty) ** 2
            reach *= 1.0 - _clamp(gain, 0.0, 1.0)
        action = first.action.model_copy(update={"journey_id": journey.journey_id})
        return first.model_copy(
            update={
                "action": action,
                "uplift": _clamp(uplift, -1.0, 1.0),
                "offer_cost": offer_cost,
                "channel_cost": channel_cost,
                "expected_net": net,
                "uncertainty": math.sqrt(variance),
            }
        )

    # --- remembering decisions --------------------------------------------------------------------

    def record(self, decision: Decision) -> None:
        """Fold one decision in: a journey's first step starts a run, a later step moves it on, and any
        other decision made again for the moment of a run's first step cancels that run.

        Safe to call on the whole decision log in order (`rebuild`)."""
        cid = decision.customer_id
        action = decision.action
        if decision.kind is not DecisionKind.ACT or action is None or action.journey_id is None:
            self.undo(cid, decision.decision_id)
            return
        self.undo(cid, decision.decision_id)
        before = self._runs.get(cid)
        if action.follow_up_step == 0:
            journey = self.config.get(action.journey_id)
            if journey is None:
                return
            self._undo[cid] = (decision.decision_id, before)
            self._runs[cid] = JourneyRun(
                customer_id=cid,
                journey_id=journey.journey_id,
                goal_id=action.goal_id,
                first_decision_id=decision.decision_id,
                last_decision_id=decision.decision_id,
                started_at=decision.made_at,
                probability=decision.journey_probability or decision.probability,
                next_step=1,
                base_at=action.send_at,
                last_sent_at=action.send_at,
                last_sent_channel=action.channel,
            )
            self._log(cid, journey.journey_id, 0, "started", "", decision.made_at)
            self._finish_if_last(self._runs[cid], decision.made_at)
            return
        run = before
        if run is None or run.journey_id != action.journey_id or action.follow_up_step < run.next_step:
            return
        self._undo[cid] = (decision.decision_id, run)
        run = run.model_copy(
            update={
                "next_step": action.follow_up_step + 1,
                "base_at": action.send_at,
                "last_sent_at": action.send_at,
                "last_sent_channel": action.channel,
                "last_decision_id": decision.decision_id,
            }
        )
        self._runs[cid] = run
        self._log(cid, run.journey_id, action.follow_up_step, "sent", "", decision.made_at)
        self._finish_if_last(run, decision.made_at)

    def undo(self, customer_id: str, decision_id: str) -> None:
        """Take back the customer's last journey decision when it is `decision_id`: the policy is
        deciding that moment again, and the new decision replaces the old one."""
        last = self._undo.get(customer_id)
        if last is None or last[0] != decision_id:
            return
        del self._undo[customer_id]
        if last[1] is None:
            self._runs.pop(customer_id, None)
        else:
            self._runs[customer_id] = last[1]

    def rebuild(self, decisions: Iterable[Decision]) -> None:
        """Start from nothing and fold the journey decisions of a log, oldest first."""
        self._runs.clear()
        self._undo.clear()
        for d in sorted(decisions, key=lambda x: x.made_at):
            self.record(d)

    def forget(self, decision_ids: Iterable[str], at: datetime | None = None) -> None:
        """Decisions that will never go out (rejected, cancelled): the runs they started or moved end."""
        drop = set(decision_ids)
        for run in list(self._runs.values()):
            if run.first_decision_id in drop or run.last_decision_id in drop:
                self.stop(run, CANCELLED, at or run.last_sent_at)

    def drop(self, customer_id: str, code: str, at: datetime) -> None:
        """End a customer's journey for a reason outside it (they left the agent group)."""
        run = self._runs.get(customer_id)
        if run is not None:
            self.stop(run, code, at)

    def active(self, customer_id: str) -> JourneyRun | None:
        return self._runs.get(customer_id)

    def runs(self) -> list[JourneyRun]:
        return list(self._runs.values())

    def stop(self, run: JourneyRun, code: str, at: datetime) -> None:
        self._runs.pop(run.customer_id, None)
        self._log(run.customer_id, run.journey_id, run.next_step, "stopped", code, at)

    def _finish_if_last(self, run: JourneyRun, at: datetime) -> None:
        journey = self.config.get(run.journey_id)
        if journey is not None and run.next_step >= len(journey.steps):
            self._runs.pop(run.customer_id, None)
            self._log(run.customer_id, run.journey_id, run.next_step, "completed", COMPLETED, at)

    def _log(self, customer_id: str, journey_id: str, step: int, kind: str, code: str, at: datetime) -> None:
        self.history.append(JourneyEvent(customer_id, journey_id, step, kind, code, at))

    # --- running: one customer, one sweep ---------------------------------------------------------

    def advance(self, state: CustomerState, ctx: DecisionContext) -> Turn:
        """Where this customer's journey stands now. Ends it on a purchase, an unsubscribe or a
        complaint, skips steps on never-read channels, and says whether the next step is due."""
        run = self._runs.get(state.customer_id)
        if run is None:
            return Turn(TurnKind.NONE)
        journey = self.config.get(run.journey_id)
        if journey is None:
            self.stop(run, CANCELLED, ctx.now)
            return Turn(TurnKind.STOPPED, run, code=CANCELLED)
        code = self.stop_reason(run, journey, state)
        if code:
            self.stop(run, code, ctx.now)
            return Turn(TurnKind.STOPPED, run, journey, code=code)
        limit = self.settings.never_open_after_sends
        while run.next_step < len(journey.steps):
            step = journey.steps[run.next_step]
            if not never_read(state.channels.get(step.channel), limit):
                break
            self._log(run.customer_id, run.journey_id, run.next_step, "skipped", NEVER_READ, ctx.now)
            run = run.model_copy(
                update={
                    "next_step": run.next_step + 1,
                    "base_at": run.base_at + timedelta(days=step.wait_days),
                }
            )
            self._runs[run.customer_id] = run
        if run.next_step >= len(journey.steps):
            self._runs.pop(run.customer_id, None)
            self._log(run.customer_id, run.journey_id, run.next_step, "completed", COMPLETED, ctx.now)
            return Turn(TurnKind.STOPPED, run, journey, code=COMPLETED)
        step = journey.steps[run.next_step]
        due = run.base_at + timedelta(days=step.wait_days)
        if due - ctx.now > timedelta(hours=self.settings.lookahead_hours):
            return Turn(TurnKind.WAIT, run, journey, step, due)
        if not self._condition_holds(step, run, state):
            self.stop(run, CONDITION_FAILED, ctx.now)
            return Turn(TurnKind.STOPPED, run, journey, step, due, CONDITION_FAILED)
        return Turn(TurnKind.STEP, run, journey, step, due)

    @staticmethod
    def stop_reason(run: JourneyRun, journey: Journey, state: CustomerState) -> str:
        """Why the journey must end now, or "". A purchase since the first decision, an unsubscribe
        (a "stop" reply is one) on any channel of the journey, or an open complaint."""
        if state.last_purchase_at is not None and state.last_purchase_at >= run.started_at:
            return PURCHASED
        if any(
            state.channels.get(ch) is not None and state.channels[ch].unsubscribed for ch in journey.channels
        ):
            return UNSUBSCRIBED
        if state.open_complaint:
            return COMPLAINT
        return ""

    @staticmethod
    def _condition_holds(step: JourneyStep, run: JourneyRun, state: CustomerState) -> bool:
        if step.when is not StepCondition.NOT_OPENED:
            return True  # a purchase was handled in `stop_reason`
        stats = state.channels.get(run.last_sent_channel)
        engaged = (
            stats is not None
            and stats.last_engaged_at is not None
            and stats.last_engaged_at >= run.last_sent_at
        )
        return not engaged

    def plan(
        self, turn: Turn, state: CustomerState, ctx: DecisionContext, bidders: Sequence[Bidder]
    ) -> Bid | str:
        """The due step as a bid, priced afresh by the goal's bidder (a plain reminder if there is one),
        with its journey, step and due time on the action; or the code that ends the journey when the
        bidder has nothing to offer on that channel now or what it offers no longer pays."""
        run, step, due = turn.run, turn.step, turn.due_at
        if run is None or step is None or due is None:
            raise ValueError("only a STEP turn can be planned")
        bidder = next((b for b in bidders if b.goal_id == run.goal_id), None)
        quotes = (
            [] if bidder is None else [b for b in bidder.bids(state, ctx) if b.action.channel is step.channel]
        )
        if not quotes:
            self.stop(run, NOTHING_TO_OFFER, ctx.now)
            return NOTHING_TO_OFFER
        best = _reminder(quotes)
        if self.settings.require_positive_net and best.expected_net <= 0:
            self.stop(run, NO_LONGER_PAYS, ctx.now)
            return NO_LONGER_PAYS
        action = best.action.model_copy(
            update={"journey_id": run.journey_id, "follow_up_step": run.next_step, "send_at": due}
        )
        return best.model_copy(update={"action": action})

    def blocked(self, turn: Turn, code: str, now: datetime) -> Verdict:
        """What a hard rule's block does to the due step (see the module docstring)."""
        run, due = turn.run, turn.due_at
        if run is None or due is None or turn.step is None:
            raise ValueError("only a STEP turn can be blocked")
        if code in _STOP_RULES:
            self.stop(run, code, now)
            return Verdict.STOP
        if code in _SKIP_RULES:
            self._log(run.customer_id, run.journey_id, run.next_step, "skipped", code, now)
            self._runs[run.customer_id] = run.model_copy(
                update={"next_step": run.next_step + 1, "base_at": due}
            )
            journey = turn.journey
            if journey is not None and run.next_step + 1 >= len(journey.steps):
                self._runs.pop(run.customer_id, None)
                self._log(run.customer_id, run.journey_id, run.next_step + 1, "completed", COMPLETED, now)
            return Verdict.SKIP
        if now - due > timedelta(days=self.settings.max_defer_days):
            self.stop(run, f"{EXPIRED}:{code}", now)
            return Verdict.STOP
        self._log(run.customer_id, run.journey_id, run.next_step, "deferred", code, now)
        return Verdict.DEFER

    # --- words ------------------------------------------------------------------------------------

    def describe(self, journey_id: str) -> str:
        journey = self.config.get(journey_id)
        return journey.describe() if journey is not None else journey_id


def _reminder(quotes: Sequence[Bid]) -> Bid:
    """The bid for a later step: the best one without an offer, else the best one."""
    plain = [b for b in quotes if b.action.offer_id == NO_OFFER.offer_id]
    return max(plain or quotes, key=lambda b: b.expected_net)


def journey_probability(options: Sequence[Bid], probabilities: Sequence[float], journey_id: str) -> float:
    """The chance the auction had of choosing `journey_id`: the sum over every option of it.

    `probabilities` is `Pick.probabilities`: do nothing first, then one per option in order."""
    total = sum(
        p for b, p in zip(options, probabilities[1:], strict=True) if b.action.journey_id == journey_id
    )
    return min(1.0, total)
