"""The fast learning loop: closed outcomes in, updated models out, each outcome counted once.

`update_models` is the nightly entry point for anything that keeps an event log (production, or a
Gym agent that watches the world's events). It builds observations with `observations.py`, feeds
them to the bundle's three models, and records what it used in the bundle, so calling it again with
the same (or a longer) log only learns from what is new.

`learn_from_rewards` is the same loop for callers that only see `Policy.learn(rewards, decisions)`,
as an agent inside the Gym does. Rewards carry conversion and unsubscribe flags but no send times or
purchase list, so it learns response and (when told each decision's `contacts_7d`) fatigue, but not
value.

Use one of these or `AutonomousPolicy.learn`, not both: they feed the same response model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from autonomy.contracts import ContentItem, Decision, DecisionKind, Event, Group, Reward
from autonomy.decide.models import FatigueObservation, ResponseObservation, action_key
from autonomy.learning.bundle import LearnedValueModel, ModelBundle
from autonomy.learning.observations import (
    DEFAULT_IPW_CLIP,
    DEFAULT_TREATED_GROUPS,
    ObservationSet,
    SegmentOf,
    decision_observations,
    format_of,
    holdout_observations,
    overlap_weight,
    segment_lookup,
    theme_of,
    timelines,
    value_observations,
)


@dataclass(frozen=True)
class LearnReport:
    """What one learning pass did. All counts are of new observations only."""

    treated: int = 0
    """Response observations from sends whose window closed."""
    control_holdout: int = 0
    """Control observations from holdout customers (logged decisions or virtual daily windows)."""
    control_agent_ipw: int = 0
    """Control observations from the agent's own do-nothing decisions (only with IPW turned on)."""
    fatigue: int = 0
    value: int = 0
    windows_closed: int = 0
    """Decisions (and virtual holdout windows) closed and consumed by this pass."""
    still_open: int = 0
    already_learned: int = 0
    not_sent: int = 0
    """ACT decisions whose window closed without a SEND or DELIVER event: blocked, failed or shadow."""
    not_delivered: int = 0
    """Sent decisions reported failed, bounced or blocked and never delivered: no treated or fatigue
    observation, since the message never reached the customer."""
    unknown_content: int = 0
    """Sent decisions whose content id is not in the library, so the theme (and key) is unknown."""
    contaminated: int = 0
    """Control windows dropped because the customer was sent something inside them."""
    no_events: int = 0
    """Decisions left for later because their customer was missing from the event mapping."""

    @property
    def response(self) -> int:
        return self.treated + self.control_holdout + self.control_agent_ipw

    def summary(self) -> str:
        """One plain sentence for logs."""
        return (
            f"Learned from {self.windows_closed} closed windows: {self.treated} sends, "
            f"{self.control_holdout + self.control_agent_ipw} untreated, {self.fatigue} fatigue and "
            f"{self.value} purchase observations; {self.still_open} still open, {self.not_sent} "
            f"never sent, {self.not_delivered} undelivered, {self.unknown_content} with unknown "
            "content."
        )

    @classmethod
    def from_set(cls, obs: ObservationSet) -> LearnReport:
        c = obs.counts
        return cls(
            treated=c.get("treated", 0),
            control_holdout=c.get("control_holdout", 0),
            control_agent_ipw=c.get("control_agent_ipw", 0),
            fatigue=c.get("fatigue", 0),
            value=c.get("value", 0),
            windows_closed=len(obs.decisions_closed) + len(obs.holdout_windows),
            still_open=c.get("still_open", 0),
            already_learned=c.get("already_learned", 0),
            not_sent=c.get("not_sent", 0),
            not_delivered=c.get("not_delivered", 0),
            unknown_content=c.get("unknown_content", 0),
            contaminated=c.get("contaminated", 0),
            no_events=c.get("no_events", 0),
        )


def update_models(
    bundle: ModelBundle,
    decisions: Iterable[Decision],
    events_by_customer: Mapping[str, Sequence[Event]],
    content_by_id: Mapping[str, ContentItem],
    now: datetime,
    horizon_days: int,
    *,
    segment_of: SegmentOf | None = None,
    holdout_customers: Iterable[str] = (),
    treated_groups: frozenset[Group] = DEFAULT_TREATED_GROUPS,
    agent_nothing_ipw: bool = False,
    ipw_clip: float = DEFAULT_IPW_CLIP,
    overlap_weighting: bool = True,
    require_send: bool = True,
    learn_value: bool = True,
) -> LearnReport:
    """Learn from every outcome window that has closed by `now`, once; returns what was learned.

    * `decisions`: the decision log (any length; already-learned ids are skipped). ACT decisions give
      treated and fatigue observations; holdout do-nothing decisions give control observations.
    * `events_by_customer`: every event of every customer in `decisions` (an empty list for a
      customer with none); customers missing from it are left for a later pass.
    * `content_by_id`: the content library, to turn a content id into its theme.
    * `holdout_customers`: holdout customers whose do-nothing decisions are not logged (the Gym).
      Each pass closes one virtual daily window per customer. Customers who do have holdout
      decisions in `decisions` are not double counted. Unlike decisions, a holdout customer missing
      from `events_by_customer` is not retried: the night's windows move on without them (counted
      as `no_events`), so pass every holdout customer's events.
    * `segment_of`: a customer's segment; None pools everyone in one segment.

    See `observations.py` for the windows, the control-group rule and the weights. Time is `now`,
    never the clock.
    """
    decisions = list(decisions)
    timeline_of = timelines(events_by_customer)
    obs = decision_observations(
        decisions,
        events_by_customer,
        content_by_id,
        now,
        horizon_days,
        learned=bundle.learned_decisions,
        last_control_start=bundle.last_control_start,
        segment_of=segment_of,
        treated_groups=treated_groups,
        agent_nothing_ipw=agent_nothing_ipw,
        ipw_clip=ipw_clip,
        overlap_weighting=overlap_weighting,
        require_send=require_send,
        timeline_of=timeline_of,
    )
    holdout_ids = list(holdout_customers)
    if holdout_ids:
        logged = {
            d.customer_id for d in decisions if d.kind is DecisionKind.NOTHING and d.group is Group.HOLDOUT
        }
        obs.merge(
            holdout_observations(
                holdout_ids,
                events_by_customer,
                now,
                horizon_days,
                learned=bundle.learned_holdout,
                last_anchor=bundle.holdout_anchor,
                last_control_start={**bundle.last_control_start, **obs.control_starts},
                skip_customers=logged,
                segment_of=segment_of,
                overlap_weighting=overlap_weighting,
                timeline_of=timeline_of,
            )
        )
    if learn_value and isinstance(bundle.value, LearnedValueModel):
        obs.merge(value_observations(events_by_customer, now, learned=bundle.learned_purchases))
    _apply(bundle, obs)
    return LearnReport.from_set(obs)


def learn_from_rewards(
    bundle: ModelBundle,
    rewards: Sequence[Reward],
    decisions: Mapping[str, Decision],
    content_by_id: Mapping[str, ContentItem],
    *,
    horizon_days: int,
    segment_of: SegmentOf | None = None,
    contacts_7d_at: Mapping[str, int] | None = None,
    treated_groups: frozenset[Group] = DEFAULT_TREATED_GROUPS,
    agent_nothing_ipw: bool = False,
    ipw_clip: float = DEFAULT_IPW_CLIP,
    overlap_weighting: bool = True,
) -> LearnReport:
    """Learn from closed `Reward`s (what `Policy.learn` receives), once per decision.

    Same rules as `update_models`: ACT rewards are treated observations keyed by theme, holdout
    do-nothing rewards are control, the agent's do-nothing rewards only with `agent_nothing_ipw`.
    A reward is trusted to be for a window that has closed and a message that was sent. Fatigue needs
    the customer's `contacts_7d` when the decision was made, which rewards do not carry: pass
    `contacts_7d_at` (decision id -> count, remembered at decide time) to learn it.
    """
    if horizon_days < 1:
        raise ValueError("horizon_days must be at least 1")
    horizon = timedelta(days=horizon_days)
    segment = segment_lookup(segment_of)
    obs = ObservationSet()
    starts: dict[str, datetime] = dict(bundle.last_control_start)
    agent: list[tuple[Decision, bool, float]] = []
    seen: set[str] = set()

    for r in sorted(rewards, key=lambda r: (r.window_end, r.decision_id)):
        d = decisions.get(r.decision_id)
        if d is None or r.decision_id in seen:
            continue
        seen.add(r.decision_id)
        if r.decision_id in bundle.learned_decisions:
            obs.count("already_learned")
            continue
        if d.kind is DecisionKind.NOTHING:
            if d.group is Group.HOLDOUT:
                w = (
                    overlap_weight(d.made_at, starts.get(d.customer_id), horizon)
                    if overlap_weighting
                    else 1.0
                )
                starts[d.customer_id] = max(d.made_at, starts.get(d.customer_id, d.made_at))
                obs.control_starts[d.customer_id] = starts[d.customer_id]
                obs.response.append(ResponseObservation(segment(d.customer_id), None, r.converted, w))
                obs.count("control_holdout")
                obs.decisions_closed.append(d.decision_id)
            elif d.group is Group.AGENT and agent_nothing_ipw:
                agent.append((d, r.converted, min(1.0 / d.probability, ipw_clip)))
                obs.decisions_closed.append(d.decision_id)
            continue
        if d.group not in treated_groups or d.action is None:
            continue
        obs.decisions_closed.append(d.decision_id)
        a = d.action
        theme = theme_of(a.content_id, content_by_id)
        if theme is None:
            obs.count("unknown_content")
        else:
            key = action_key(a.channel, a.offer_id, theme, format_of(a.content_id, content_by_id))
            obs.response.append(ResponseObservation(segment(d.customer_id), key, r.converted))
            obs.count("treated")
        contacts = (contacts_7d_at or {}).get(d.decision_id)
        if contacts is not None:
            obs.fatigue.append(FatigueObservation(contacts, r.unsubscribed or r.complained))
            obs.count("fatigue")

    if agent:
        total = sum(raw for *_, raw in agent)
        scale = len(agent) / total if total > 0 else 0.0
        for d, converted, raw in sorted(agent, key=lambda t: (t[0].customer_id, t[0].made_at)):
            w = raw * scale
            if overlap_weighting:
                w *= overlap_weight(d.made_at, starts.get(d.customer_id), horizon)
            starts[d.customer_id] = max(d.made_at, starts.get(d.customer_id, d.made_at))
            obs.control_starts[d.customer_id] = starts[d.customer_id]
            obs.response.append(ResponseObservation(segment(d.customer_id), None, converted, w))
            obs.count("control_agent_ipw")

    _apply(bundle, obs)
    return LearnReport.from_set(obs)


def _apply(bundle: ModelBundle, obs: ObservationSet) -> None:
    """Feed the observations to the models and remember what was used."""
    if obs.response:
        bundle.response.update(obs.response)
    if obs.fatigue:
        bundle.fatigue.update(obs.fatigue)
    if obs.value and isinstance(bundle.value, LearnedValueModel):  # only then were any made
        bundle.value.update(obs.value)
    bundle.learned_decisions.update(obs.decisions_closed)
    bundle.learned_purchases.update(obs.purchases_seen)
    bundle.learned_holdout.update(obs.holdout_windows)
    bundle.last_control_start.update(obs.control_starts)
    if obs.holdout_anchor is not None:
        bundle.holdout_anchor = obs.holdout_anchor
