"""The hook by which the runtime runs the selected policy: Shadow first, then live, always reversible.

`AgentPolicy.use_chooser(chooser)` (autonomy/runtime/agent.py) hands every agent-group decision to
the chooser after the agent has made it. `ScientistChooser` holds a policy fitted by the Scientist
(`autonomy.scientist.menu`, refitted on all the customers the loop was allowed to learn from) and:

* **SHADOW** (the default, and where every new selection starts): works out what the selected policy
  would do for the customer, records it in `shadow_log` next to what the agent did, and returns the
  agent's decision UNCHANGED. Nothing the policy chose is ever sent.
* **LIVE** (set explicitly, with a time and a reason, by whoever promotes it): returns the selected
  policy's decision instead.

Either way the policy's choice passes the same hard rules as the agent's bids (`autonomy.decide.rules`:
the signed envelope's channels, offers, quiet hours and weekly cap, consent, unsubscribes, the holdout)
and the setting `weekly_contact_budget`. The policy's probabilities are masked to the options those
rules allow and renormalised, the arm is drawn with a generator seeded by (seed, customer), and the
logged `Decision.probability` is the probability of the option actually taken, so what runs live can
still be evaluated off-policy later. A switch of mode is appended to `mode_log` (when, to what, why),
and switching back to shadow is the same call: nothing is lost.

A chooser never sees a holdout or baseline customer from the agent (it is only called for the agent
group) and returns the agent's decision at once if it is given one anyway. Pure given its inputs: time
and seed come in through the context.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import numpy as np
import numpy.typing as npt

from autonomy.contracts import (
    Action,
    Bid,
    Channel,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Format,
    Group,
)
from autonomy.contracts.base import stable_id
from autonomy.decide import rules
from autonomy.decide.policy import seeded_rng
from autonomy.scientist.campaign import Campaign
from autonomy.scientist.menu import FittedPolicy
from autonomy.scientist.select import Selection
from autonomy.uplift.features import BEHAVIOUR, behaviour_from_state

__all__ = [
    "BUDGET_CODE",
    "ArmAction",
    "ModeChange",
    "ScientistChooser",
    "ShadowRecord",
    "chooser_from_selection",
]

FloatArray = npt.NDArray[np.float64]

BUDGET_CODE = "scientist_weekly_budget"
"""`Decision.blocked_by` code when the setting `weekly_contact_budget` removed the contact."""
VERSION = "scientist-v1"


@dataclass(frozen=True)
class ArmAction:
    """The message an arm of the campaign stands for, as an action template."""

    goal_id: str
    channel: Channel
    content_id: str | None = None
    offer_id: str = "none"
    format: Format = Format.TEXT

    def at(self, send_at: datetime) -> Action:
        return Action(
            goal_id=self.goal_id,
            channel=self.channel,
            offer_id=self.offer_id,
            content_id=self.content_id,
            format=self.format,
            send_at=send_at,
        )


@dataclass(frozen=True)
class ShadowRecord:
    """What the selected policy would have done for one customer, beside what the agent did."""

    customer_id: str
    made_at: datetime
    agent_arm: int
    """The arm the agent's decision amounts to (0: nothing, or a message that is no arm of the campaign)."""
    policy_arm: int
    policy_probability: float
    blocked_by: tuple[str, ...]

    @property
    def differs(self) -> bool:
        return self.agent_arm != self.policy_arm


@dataclass(frozen=True)
class ModeChange:
    at: datetime
    mode: str
    reason: str


@dataclass
class ScientistChooser:
    """Runs a fitted policy beside the agent (shadow) or instead of it (live). See the module docstring."""

    policy: FittedPolicy
    arm_actions: Mapping[int, ArmAction]
    """Arm (1..k) -> the message it stands for. Arm 0 is "contact nobody"."""
    weekly_contact_budget: int = 2
    version: str = VERSION
    mode: Literal["shadow", "live"] = "shadow"
    shadow_log: list[ShadowRecord] = field(default_factory=list)
    mode_log: list[ModeChange] = field(default_factory=list)

    def set_mode(self, mode: Literal["shadow", "live"], *, at: datetime, reason: str) -> None:
        """Switch between shadow and live; logged, and reversible by calling it again."""
        if mode not in ("shadow", "live"):
            raise ValueError("mode is 'shadow' or 'live'")
        if not reason.strip():
            raise ValueError("a change of mode needs a reason")
        if mode != self.mode:
            self.mode = mode
            self.mode_log.append(ModeChange(at, mode, reason))

    # --- the hook ---------------------------------------------------------------------------------

    def __call__(self, state: CustomerState, ctx: DecisionContext, agent: Decision) -> Decision:
        if state.group is not Group.AGENT:
            return agent
        chosen = self._decide(state, ctx, agent)
        if self.mode == "live":
            return chosen
        self.shadow_log.append(
            ShadowRecord(
                customer_id=state.customer_id,
                made_at=ctx.now,
                agent_arm=self._arm_of(agent),
                policy_arm=self._arm_of(chosen),
                policy_probability=chosen.probability,
                blocked_by=chosen.blocked_by,
            )
        )
        return agent

    def _arm_of(self, d: Decision) -> int:
        if d.action is None:
            return 0
        for arm, a in self.arm_actions.items():
            if a.content_id == d.action.content_id and a.channel is d.action.channel:
                return arm
        return 0

    def _decide(self, state: CustomerState, ctx: DecisionContext, agent: Decision) -> Decision:
        k = 1 + max(self.arm_actions, default=0)
        frame = self.policy.row_features(state.preferences)
        beh = np.asarray([behaviour_from_state(state, ctx.now)], dtype=np.float64)
        for j, name in enumerate(BEHAVIOUR):
            # a campaign read from the shop's history carries its behaviour block as feature columns
            # (`behaviour_<name>`); a customer's folded state is where the same numbers live now
            if f"behaviour_{name}" in frame.columns:
                frame[f"behaviour_{name}"] = float(beh[0, j])
        probs = np.asarray(self.policy.probabilities(frame, beh), dtype=np.float64).reshape(-1)
        if probs.shape != (k,):
            raise ValueError(f"the policy gives {probs.shape[0]} arms, the chooser knows {k}")
        send_at = agent.action.send_at if agent.action is not None else ctx.now
        allowed = np.zeros(k, dtype=bool)
        allowed[0] = True
        blocked: dict[int, str] = {}
        if state.contacts_7d >= self.weekly_contact_budget:
            for arm in self.arm_actions:
                blocked[arm] = BUDGET_CODE
        else:
            for arm, template in self.arm_actions.items():
                action = template.at(send_at)
                bid = Bid(
                    goal_id=action.goal_id,
                    action=action,
                    uplift=0.0,
                    value_if_converted=0.0,
                    expected_net=0.0,
                )
                check = rules.check_bid(bid, state, ctx)
                if check.allowed:
                    allowed[arm] = True
                else:
                    blocked[arm] = check.code
        masked = np.where(allowed, np.clip(probs, 0.0, None), 0.0)
        total = float(masked.sum())
        masked = masked / total if total > 0.0 else np.eye(k)[0]
        rng = seeded_rng(ctx.seed, f"{state.customer_id}\x1fscientist")
        arm = int(rng.choice(k, p=masked))
        p = max(float(masked[arm]), 1e-9)
        wanted_blocked = tuple(sorted({blocked[a] for a in blocked if probs[a] > 0.0}))
        did = stable_id("dec", state.customer_id, ctx.now.isoformat(), self.version)
        label = getattr(self.policy, "label", "the selected policy")
        if arm == 0:
            return Decision(
                decision_id=did,
                customer_id=state.customer_id,
                made_at=ctx.now,
                kind=DecisionKind.NOTHING,
                group=state.group,
                probability=p,
                reason=f"Selected policy '{label}' leaves this customer alone"
                + (f" (blocked: {', '.join(wanted_blocked)})." if wanted_blocked else "."),
                blocked_by=wanted_blocked,
                policy_version=self.version,
            )
        action = self.arm_actions[arm].at(send_at)
        return Decision(
            decision_id=did,
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.ACT,
            action=action,
            group=state.group,
            probability=p,
            reason=f"Selected policy '{label}' sends arm {arm} to this customer (chance {p:.0%}).",
            blocked_by=wanted_blocked,
            policy_version=self.version,
        )


def chooser_from_selection(
    selection: Selection,
    train: Campaign,
    arm_actions: Mapping[int, ArmAction],
    *,
    version: str = VERSION,
) -> ScientistChooser | None:
    """The chooser for what a selection will run, refitted on all of `train`, in SHADOW mode.

    `arm_actions` must name the message of every arm 1..k of the campaign. None when the selection
    is the learning agent itself (it already runs: there is nothing to put beside it). Going live is a
    separate, logged `set_mode` by whoever promotes it."""
    run = selection.run
    if run.kind == "bandit":
        return None
    if set(arm_actions) != set(range(1, train.n_arms)):
        raise ValueError(f"arm_actions must cover the arms 1..{train.n_arms - 1} of the campaign")
    settings = run.spec()["settings"]
    assert isinstance(settings, dict)
    budget = settings.get("weekly_contact_budget", 2)
    assert isinstance(budget, int)
    return ScientistChooser(
        policy=run.fit(train), arm_actions=arm_actions, weekly_contact_budget=budget, version=version
    )
