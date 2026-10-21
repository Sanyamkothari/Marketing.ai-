"""Gym spot-check of the Scientist: the production agent with the Scientist's selection in front of it.

    python -m scripts.v2_gym_scientist --scenario baseline_shop --policy scientist --seeds 2 \\
        --customers 1000 --report-json out.json --scientist-log selection.json

Everything after the options of this script is the Gym's own command line (`autonomy.gym.run`). Two
policies are registered here, never in `autonomy/`:

* `scientist` is the agent with one change. At the first `prepare_day`, when the shop's history has just
  been streamed in, the Scientist reads the latest closed campaign in it (`campaign_from_history`, the
  same convention as the agent's uplift warm start), fits the fixed default list on one half
  (segment rule, uplift model) and scores it on the other, and applies its written rule. If a candidate
  beats the safe default after the Holm correction, the candidate (refitted on the whole campaign) is
  promoted to LIVE through `AgentPolicy.use_chooser`, logged with a reason, inside the signed envelope;
  otherwise (default, not enough data, no usable campaign) the agent runs as it is. The learning agent
  is not a candidate here: its off-policy evaluation needs the bench's runner, so the current set-up is
  the incumbent, and the Gym scorecard (not the Scientist's evidence) says whether the promotion paid.
* `scientist_shadow` is the same selection with the chooser left in SHADOW: every decision is the agent's,
  unchanged, and only the log of what the selected policy would have done grows. It exists to show that
  Shadow changes nothing.

`--scientist-log PATH` writes one record per run: the selection (`Selection.to_dict()`), what it ran
and why.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from autonomy.contracts import Channel, ContentItem, CustomerState, Decision, DecisionContext, Event
from autonomy.gym import run as gym_run
from autonomy.interfaces import Policy
from autonomy.runtime.agent import AgentPolicy
from autonomy.scientist import (
    ArmAction,
    Campaign,
    campaign_from_history,
    chooser_from_selection,
    default_candidates,
    run_scientist,
)

VERSION = "scientist-gym-v1"

LOG: list[dict[str, Any]] = []
"""One record per policy built (one per run): what the Scientist saw and did."""


def arm_actions_of(
    campaign: Campaign, content: Sequence[ContentItem], goal_ids: Mapping[Channel, str]
) -> dict[int, ArmAction] | None:
    """The message each arm of a history campaign stands for. An arm's name is `channel|offer|theme[|format]`
    (the agent's action key); the first approved library item with that channel, offer and theme is the
    message. None when an arm has no such item or its channel has no goal (nothing is guessed)."""
    out: dict[int, ArmAction] = {}
    for k, name in enumerate(campaign.arm_names):
        if k == 0:
            continue
        parts = name.split("|")
        if len(parts) < 3:
            return None
        try:
            channel = Channel(parts[0])
        except ValueError:
            return None
        goal = goal_ids.get(channel)
        item = next(
            (
                c
                for c in content
                if c.channel is channel
                and c.offer_id == parts[1]
                and (parts[2] == "*" or c.theme.value == parts[2])
                and (len(parts) < 4 or c.format.value == parts[3])
            ),
            None,
        )
        if goal is None or item is None:
            return None
        out[k] = ArmAction(
            goal_id=goal,
            channel=channel,
            content_id=item.content_id,
            offer_id=item.offer_id,
            format=item.format,
        )
    return out


class ScientistGymPolicy:
    """The agent plus the Scientist's selection (see the module docstring). Anything else is the agent's."""

    version = VERSION

    def __init__(
        self,
        agent: AgentPolicy,
        *,
        scenario: str,
        margin_rate: float,
        costs: Mapping[Channel, float],
        live: bool,
    ) -> None:
        self.agent = agent
        self.costs = dict(costs)
        self.margin_rate = margin_rate
        self.live = live
        self.scenario = scenario
        self.record: dict[str, Any] = {
            "scenario": scenario,
            "mode": "live" if live else "shadow",
            "ran": "agent",
        }
        LOG.append(self.record)
        self._history: dict[str, list[Event]] = {}
        self._selected = False
        self.chooser: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.agent, name)

    def nightly(
        self, decisions: Any, events_by_customer: Mapping[str, Sequence[Event]], now: Any
    ) -> list[str]:
        if not self._selected:  # before the first decision everything handed over is history
            for cid, events in events_by_customer.items():
                self._history.setdefault(cid, []).extend(events)
        return self.agent.nightly(decisions, events_by_customer, now)

    def prepare_day(self, states: Sequence[CustomerState], ctx: DecisionContext) -> None:
        self.agent.prepare_day(states, ctx)
        if not self._selected:
            self._selected = True
            self._select({s.customer_id: s for s in states}, ctx)

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        return self.agent.decide(state, ctx)

    def _select(self, states: Mapping[str, CustomerState], ctx: DecisionContext) -> None:
        by_id = {c.content_id: c for c in ctx.content}
        campaign, notes = campaign_from_history(
            self._history,
            states,
            by_id,
            ctx.now,
            costs=self.costs,
            margin_rate=self.margin_rate,
            horizon_days=float(self.agent.learn_horizon_days),
        )
        self.record["history_notes"] = list(notes)
        self._history = {}
        if campaign is None:
            self.record["outcome"] = "no_campaign"
            return
        selection = run_scientist(campaign, default_candidates(None))
        self.record["selection"] = selection.to_dict()
        self.record["outcome"] = selection.outcome
        self.record["customers_in_campaign"] = campaign.n
        if selection.outcome != "candidate":
            return  # not enough data / the safe default: the incumbent agent keeps running
        goals = {ch: b.goal_id for b in ctx.briefs for ch in b.envelope.channels}
        actions = arm_actions_of(campaign, ctx.content, goals)
        if actions is None:
            self.record["outcome"] = "candidate_not_deployable"
            return
        chooser = chooser_from_selection(selection, campaign, actions)
        if chooser is None:
            return
        if self.live:
            chooser.set_mode(
                "live",
                at=ctx.now,
                reason="the Scientist's candidate beat the safe default on held-out customers",
            )
        self.agent.use_chooser(chooser)
        self.chooser = chooser
        self.record["ran"] = f"scientist: {selection.chosen_label}" + ("" if self.live else " (shadow)")


def _factory(live: bool) -> Any:
    def make(world: Any) -> Policy:
        spec = world.spec
        agent = gym_run.agent_factory(world)
        assert isinstance(agent, AgentPolicy)
        return ScientistGymPolicy(
            agent,
            scenario=spec.name,
            margin_rate=float(spec.margin_rate),
            costs=spec.channel_cost,
            live=live,
        )

    return make


def register() -> None:
    gym_run.register_policy("scientist", _factory(True), replace=True)
    gym_run.register_policy("scientist_shadow", _factory(False), replace=True)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--scientist-log", type=Path, help="write what the Scientist saw and did, per run")
    known, rest = ap.parse_known_args(list(argv) if argv is not None else sys.argv[1:])
    register()
    LOG.clear()
    code = gym_run.main(rest)
    if known.scientist_log:
        known.scientist_log.parent.mkdir(parents=True, exist_ok=True)
        known.scientist_log.write_text(json.dumps(LOG, indent=1, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
