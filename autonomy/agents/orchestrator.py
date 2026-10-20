"""A goal's lifecycle as a plain state machine. No LLM: it checks each move and records it.

    DRAFT -> BRIEFED -> APPROVED -> LIBRARY_READY -> ACTIVE <-> PAUSED -> DONE

The stage is not stored anywhere of its own. It is read off the workspace every time, so it can
never disagree with what the agents and the marketer actually wrote, and it survives restarts:

- ACTIVE, PAUSED and DONE are the goal's own `status`.
- Otherwise (status DRAFT): no brief is DRAFT; an unapproved brief is BRIEFED; an approved brief
  is APPROVED until the library holds at least one approved item the envelope allows (its channel
  and offer are in the envelope), which makes it LIBRARY_READY.

Each move is a method that takes what the move needs and raises `IllegalTransitionError` when the goal
is in the wrong stage or the objects do not fit. Moves never call an agent; the caller runs the
Strategist, Creative or Critic and hands the result in. The one exception is `weekly_cycle`, the
slow learning loop, which calls the Analyst and then the Strategist's adjustment. Both are injected
as plain callables so this module does not depend on how those agents are built. Every adjusted
brief is checked with `brief_within` before it is stored: a brief that reaches outside the approved
envelope is refused, whatever the Strategist said.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from autonomy.agents.strategist import brief_within
from autonomy.contracts import (
    AutonomyLevel,
    Brief,
    Briefing,
    ContentItem,
    ContentStatus,
    Goal,
    GoalStatus,
    stable_id,
)
from autonomy.interfaces import Workspace

AnalystFn = Callable[[Goal, Brief, datetime], Briefing]
"""(goal, current brief, now) -> the weekly report. Numbers in it come from code."""

AdjustFn = Callable[[Brief, Briefing, datetime], Brief]
"""(current brief, report, now) -> the next brief version, e.g. `Strategist.adjust_brief`."""


class GoalStage(StrEnum):
    DRAFT = "draft"
    BRIEFED = "briefed"
    APPROVED = "approved"
    LIBRARY_READY = "library_ready"
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"


class IllegalTransitionError(RuntimeError):
    """A move the lifecycle does not allow from the goal's current stage, or with these objects."""

    def __init__(self, goal_id: str, stage: GoalStage, move: str, detail: str = "") -> None:
        self.goal_id = goal_id
        self.stage = stage
        self.move = move
        text = f"goal {goal_id}: cannot {move} while {stage.value}"
        super().__init__(f"{text}: {detail}" if detail else text)


class UnknownGoalError(LookupError):
    """No goal with this id in the workspace."""


@dataclass(frozen=True)
class WeeklyOutcome:
    insights: Briefing
    brief: Brief
    previous: Brief

    @property
    def changed(self) -> bool:
        """Whether the envelope or follow-ups moved (a new version is stored either way)."""
        return (self.brief.envelope, self.brief.follow_ups) != (
            self.previous.envelope,
            self.previous.follow_ups,
        )


def _gid(goal: Goal | str) -> str:
    return goal if isinstance(goal, str) else goal.goal_id


class Orchestrator:
    """Moves goals through their lifecycle in one `Workspace`. Methods accept a `Goal` or its id."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        analyst: AnalystFn | None = None,
        adjust: AdjustFn | None = None,
    ) -> None:
        self._ws = workspace
        self._analyst = analyst
        self._adjust = adjust

    # --- reading -------------------------------------------------------------------------------

    def goal(self, goal: Goal | str) -> Goal:
        """The goal as the workspace holds it now (a passed-in `Goal` may be stale)."""
        goal_id = _gid(goal)
        for g in self._ws.goals():
            if g.goal_id == goal_id:
                return g
        raise UnknownGoalError(goal_id)

    def brief(self, goal: Goal | str) -> Brief | None:
        """The latest brief for the goal, or None."""
        found = self._ws.briefs(_gid(goal))
        return max(found, key=lambda b: b.version) if found else None

    def stage(self, goal: Goal | str) -> GoalStage:
        g = self.goal(goal)
        if g.status is GoalStatus.ACTIVE:
            return GoalStage.ACTIVE
        if g.status is GoalStatus.PAUSED:
            return GoalStage.PAUSED
        if g.status is GoalStatus.DONE:
            return GoalStage.DONE
        brief = self.brief(g)
        if brief is None:
            return GoalStage.DRAFT
        if not brief.approved:
            return GoalStage.BRIEFED
        if self.usable_content(g, brief):
            return GoalStage.LIBRARY_READY
        return GoalStage.APPROVED

    def usable_content(self, goal: Goal | str, brief: Brief) -> list[ContentItem]:
        """Approved library items this brief's envelope allows (right channel, right offer)."""
        return _usable(self._ws.content(goal_id=_gid(goal), approved_only=True), _gid(goal), brief)

    # --- moves ---------------------------------------------------------------------------------

    def create(self, goal: Goal) -> GoalStage:
        """Record a new goal. It starts in DRAFT with no brief."""
        if any(g.goal_id == goal.goal_id for g in self._ws.goals()):
            raise IllegalTransitionError(goal.goal_id, self.stage(goal), "create", "the goal already exists")
        if goal.status is not GoalStatus.DRAFT:
            raise IllegalTransitionError(
                goal.goal_id, GoalStage.DRAFT, "create", f"a new goal must be draft, not {goal.status.value}"
            )
        self._ws.put_goal(goal)
        return GoalStage.DRAFT

    def attach_brief(self, goal: Goal | str, brief: Brief) -> Brief:
        """DRAFT or BRIEFED -> BRIEFED. A redraft replaces the unapproved brief as a newer version."""
        g = self.goal(goal)
        stage = self.stage(g)
        if stage not in (GoalStage.DRAFT, GoalStage.BRIEFED):
            raise IllegalTransitionError(g.goal_id, stage, "attach a brief", "only before approval")
        if brief.goal_id != g.goal_id:
            raise IllegalTransitionError(
                g.goal_id, stage, "attach a brief", f"it is for goal {brief.goal_id}"
            )
        if brief.approved:
            raise IllegalTransitionError(g.goal_id, stage, "attach a brief", "approval is its own step")
        if brief.envelope.budget > g.budget:
            raise IllegalTransitionError(g.goal_id, stage, "attach a brief", "its budget is above the goal's")
        current = self.brief(g)
        if current is not None and brief.version <= current.version:
            version = current.version + 1
            brief = brief.model_copy(
                update={
                    "version": version,
                    "brief_id": stable_id("brief", g.goal_id, version, brief.created_at.isoformat()),
                }
            )
        self._ws.put_brief(brief)
        return brief

    def approve(
        self,
        goal: Goal | str,
        *,
        approved_by: str,
        now: datetime,
        autonomy: AutonomyLevel | None = None,
    ) -> Brief:
        """BRIEFED -> APPROVED. The marketer signs off the envelope, and may set the autonomy level."""
        g = self.goal(goal)
        stage = self.stage(g)
        if stage is not GoalStage.BRIEFED:
            raise IllegalTransitionError(g.goal_id, stage, "approve", "there is no unapproved brief")
        if not approved_by.strip():
            raise IllegalTransitionError(g.goal_id, stage, "approve", "an approval needs a name")
        brief = self.brief(g)
        assert brief is not None  # BRIEFED means there is one
        approved = brief.model_copy(update={"approved_by": approved_by, "approved_at": now})
        self._ws.put_brief(approved)
        if autonomy is not None and autonomy is not g.autonomy:
            self._ws.put_goal(g.model_copy(update={"autonomy": autonomy}))
        return approved

    def add_library(self, goal: Goal | str, items: Iterable[ContentItem]) -> GoalStage:
        """APPROVED -> LIBRARY_READY once the library has a usable approved item.

        Also allowed later (LIBRARY_READY, ACTIVE, PAUSED) to refresh the library. Before launch the
        items must leave the goal with at least one usable item, or nothing is stored.
        """
        g = self.goal(goal)
        stage = self.stage(g)
        allowed = (GoalStage.APPROVED, GoalStage.LIBRARY_READY, GoalStage.ACTIVE, GoalStage.PAUSED)
        if stage not in allowed:
            raise IllegalTransitionError(g.goal_id, stage, "add content", "the brief is not approved yet")
        new = list(items)
        foreign = [c.content_id for c in new if c.goal_id not in (g.goal_id, None)]
        if foreign:
            raise IllegalTransitionError(
                g.goal_id, stage, "add content", f"items for another goal: {foreign}"
            )
        brief = self.brief(g)
        assert brief is not None  # approved stages have one
        if stage is GoalStage.APPROVED and not _usable(new, g.goal_id, brief):
            raise IllegalTransitionError(
                g.goal_id, stage, "add content", "no approved item fits the envelope's channels and offers"
            )
        self._ws.put_content(new)
        return self.stage(g)

    def activate(self, goal: Goal | str, *, now: datetime) -> Goal:
        """LIBRARY_READY -> ACTIVE. The inner loop starts deciding for this goal."""
        g = self.goal(goal)
        stage = self.stage(g)
        if stage is not GoalStage.LIBRARY_READY:
            raise IllegalTransitionError(
                g.goal_id, stage, "activate", "it needs an approved brief and library"
            )
        self._check_not_ended(g, stage, "activate", now)
        return self._set_status(g, GoalStatus.ACTIVE)

    def pause(self, goal: Goal | str) -> Goal:
        """ACTIVE -> PAUSED. One click for the marketer, or a stop rule tripping."""
        g = self.goal(goal)
        stage = self.stage(g)
        if stage is not GoalStage.ACTIVE:
            raise IllegalTransitionError(g.goal_id, stage, "pause", "only an active goal can pause")
        return self._set_status(g, GoalStatus.PAUSED)

    def resume(self, goal: Goal | str, *, now: datetime) -> Goal:
        """PAUSED -> ACTIVE, if the goal has not ended and its library is still usable."""
        g = self.goal(goal)
        stage = self.stage(g)
        if stage is not GoalStage.PAUSED:
            raise IllegalTransitionError(g.goal_id, stage, "resume", "only a paused goal can resume")
        self._check_not_ended(g, stage, "resume", now)
        brief = self.brief(g)
        if brief is None or not brief.approved or not self.usable_content(g, brief):
            raise IllegalTransitionError(
                g.goal_id, stage, "resume", "the library has no usable approved item"
            )
        return self._set_status(g, GoalStatus.ACTIVE)

    def finish(self, goal: Goal | str) -> Goal:
        """ACTIVE or PAUSED -> DONE. Final: a finished goal never runs again."""
        g = self.goal(goal)
        stage = self.stage(g)
        if stage not in (GoalStage.ACTIVE, GoalStage.PAUSED):
            raise IllegalTransitionError(
                g.goal_id, stage, "finish", "only a running or paused goal can finish"
            )
        return self._set_status(g, GoalStatus.DONE)

    # --- the weekly slow loop ------------------------------------------------------------------

    def weekly_cycle(self, goal: Goal | str, *, now: datetime) -> WeeklyOutcome:
        """Analyst report, then the Strategist's adjustment, checked and stored as a new version.

        Runs for ACTIVE and PAUSED goals (a paused goal still has results worth reading).
        """
        g = self.goal(goal)
        stage = self.stage(g)
        if stage not in (GoalStage.ACTIVE, GoalStage.PAUSED):
            raise IllegalTransitionError(g.goal_id, stage, "run the weekly cycle", "the goal is not running")
        if self._analyst is None or self._adjust is None:
            raise ValueError("weekly_cycle needs both an analyst and an adjust callable")
        brief = self.brief(g)
        if brief is None or not brief.approved:
            raise IllegalTransitionError(
                g.goal_id, stage, "run the weekly cycle", "there is no approved brief"
            )
        insights = self._analyst(g, brief, now)
        new = self._adjust(brief, insights, now)
        problems = brief_within(new, brief)
        if new.version <= brief.version:
            problems.append(f"version {new.version} is not after {brief.version}")
        if (new.approved_by, new.approved_at) != (brief.approved_by, brief.approved_at):
            problems.append("the approval changed (only the marketer approves)")
        if problems:
            raise IllegalTransitionError(
                g.goal_id,
                stage,
                "store the adjusted brief",
                "it reaches outside the approved envelope: " + "; ".join(problems),
            )
        self._ws.put_brief(new)
        return WeeklyOutcome(insights=insights, brief=new, previous=brief)

    # --- helpers -------------------------------------------------------------------------------

    def _set_status(self, goal: Goal, status: GoalStatus) -> Goal:
        updated = goal.model_copy(update={"status": status})
        self._ws.put_goal(updated)
        return updated

    @staticmethod
    def _check_not_ended(goal: Goal, stage: GoalStage, move: str, now: datetime) -> None:
        if goal.ends_at is not None and now >= goal.ends_at:
            raise IllegalTransitionError(goal.goal_id, stage, move, "the goal's end date has passed")


def _usable(items: Iterable[ContentItem], goal_id: str, brief: Brief) -> list[ContentItem]:
    env = brief.envelope
    offer_ids = {o.offer_id for o in env.offers}
    return [
        c
        for c in items
        if c.status is ContentStatus.APPROVED
        and c.goal_id in (goal_id, None)
        and c.channel in env.channels
        and c.offer_id in offer_ids
    ]
