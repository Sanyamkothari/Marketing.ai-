"""The experiment book: the register of every experiment, its log, and the defaults winners become.

Everything that changes an experiment goes through here, so the limits cannot be skipped:

* **Limits (decision V-3).** At most `MAX_PER_GOAL` (2) experiments running for one goal and at most
  `MAX_TOTAL_SHARE` (10%) of the agent group in experiments at once, counted as the sum of the running
  experiments' shares (each share is a share of its own audience, so the sum is an upper bound on the
  share of the agent group). `start` refuses more, with a plain sentence, and logs the refusal.
* **Pre-registration.** `start` checks the arms against the approved brief's envelope, stamps the
  spec with the time and a digest of the spec and its rules, and puts `registered` as the first log line
  of the experiment. No decision can be assigned to an experiment that is not in the book as running.
* **Review.** `review` reads an experiment from its outcomes, applies the stopping and pick rules written
  at registration, and ends the experiment when they say so. Nothing else ends one except `stop`.
* **Promotion.** `promote` makes a winner the default of its goal, only when its arm is inside the brief's
  approved envelope (checked again now: the envelope may have narrowed), with a reason, and `revert` undoes
  it. Promotions never change an envelope; they are a preference inside it that `ExperimentPolicy` applies.
* **The log** is append-only and numbered; `snapshot` and `restore` carry the whole book as plain data.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from autonomy.contracts import Brief, stable_id
from autonomy.experiments.model import (
    MAX_PER_GOAL,
    MAX_TOTAL_SHARE,
    EndKind,
    Experiment,
    ExperimentEvent,
    ExperimentSpec,
    Promotion,
    Status,
    digest_of,
    rule_text,
)
from autonomy.experiments.reading import Judgement, Outcomes, Reading, judge, read
from autonomy.scientist.questions import QuestionError, QuestionKind, check_arm, check_question

__all__ = ["ExperimentBook", "ExperimentRefused", "PromotionRefused", "Review"]

_EPS = 1e-9


class ExperimentRefused(ValueError):  # noqa: N818 (a plain refusal the caller shows to a person)
    """The book will not start this experiment; the message says why in plain words."""


class PromotionRefused(ValueError):  # noqa: N818
    """The book will not promote this winner; the message says why in plain words."""


class Review:
    """What one look at an experiment found and what the book did about it."""

    def __init__(self, experiment: Experiment, reading: Reading, judgement: Judgement) -> None:
        self.experiment = experiment
        self.reading = reading
        self.judgement = judgement

    @property
    def ended(self) -> bool:
        return not self.experiment.running


class ExperimentBook:
    """In-memory register of experiments, with the limits and the log. One per business."""

    def __init__(self, *, max_per_goal: int = MAX_PER_GOAL, max_total_share: float = MAX_TOTAL_SHARE) -> None:
        if max_per_goal > MAX_PER_GOAL or max_total_share > MAX_TOTAL_SHARE + _EPS:
            raise ValueError("a book may be stricter than the plan's limits, never looser")
        self.max_per_goal = max_per_goal
        self.max_total_share = max_total_share
        self._experiments: dict[str, Experiment] = {}
        self._promotions: list[Promotion] = []
        self._log: list[ExperimentEvent] = []

    # --- reading the book -------------------------------------------------------------------------

    @property
    def log(self) -> tuple[ExperimentEvent, ...]:
        return tuple(self._log)

    def get(self, experiment_id: str) -> Experiment:
        try:
            return self._experiments[experiment_id]
        except KeyError:
            raise KeyError(f"no experiment called {experiment_id!r}") from None

    def all(self) -> list[Experiment]:
        return sorted(self._experiments.values(), key=lambda e: (e.registered_at, e.experiment_id))

    def running(self, goal_id: str | None = None) -> list[Experiment]:
        """Running experiments in start order (the order customers are offered to them)."""
        return [e for e in self.all() if e.running and (goal_id is None or e.goal_id == goal_id)]

    def promotions(self) -> tuple[Promotion, ...]:
        return tuple(self._promotions)

    def default_for(self, goal_id: str, kind: QuestionKind) -> str | None:
        """The arm promoted last (and not reverted) for this goal and kind of question, if any."""
        active = [p for p in self._promotions if p.goal_id == goal_id and p.kind is kind and p.active]
        return active[-1].value if active else None

    def defaults(self) -> list[tuple[str, QuestionKind, str]]:
        """Every active default as (goal id, kind, arm id)."""
        keys = dict.fromkeys((p.goal_id, p.kind) for p in self._promotions)
        out: list[tuple[str, QuestionKind, str]] = []
        for goal_id, kind in keys:
            value = self.default_for(goal_id, kind)
            if value is not None:
                out.append((goal_id, kind, value))
        return out

    # --- starting ---------------------------------------------------------------------------------

    def start(self, spec: ExperimentSpec, brief: Brief, now: datetime) -> Experiment:
        """Register and start an experiment, or raise `ExperimentRefused` with the reason.

        Refused when: the brief is not approved or is another goal's; the id is taken; the arms are not a
        valid set for the question or not inside the brief's envelope; the goal already has its quota of
        running experiments; the shares would pass the cap; or the goal has a promoted default that the
        spec does not use as its reference arm.
        """
        problem = self._problem(spec, brief)
        if problem is not None:
            self._append(now, spec.experiment_id, "refused", problem)
            raise ExperimentRefused(problem)
        exp = Experiment(
            spec=spec, registered_at=now, digest=digest_of(spec), rules=rule_text(spec), status=Status.RUNNING
        )
        self._experiments[spec.experiment_id] = exp
        self._append(
            now, spec.experiment_id, "registered", f"{spec.question} Digest {exp.digest}. {exp.rules}"
        )
        self._append(
            now,
            spec.experiment_id,
            "started",
            f"{', '.join(a.label for a in spec.arms)}; {spec.share:.1%} of the audience; reference "
            f"{spec.label_of(spec.reference_arm)}.",
        )
        return exp

    def _problem(self, spec: ExperimentSpec, brief: Brief) -> str | None:
        if spec.experiment_id in self._experiments:
            return f"An experiment called {spec.experiment_id!r} already exists."
        if not brief.approved:
            return "The brief is not approved, so nothing can be tested inside it yet."
        if brief.goal_id != spec.goal_id:
            return "The brief belongs to another goal."
        try:
            check_question(spec.kind, spec.arm_ids, brief.envelope)
        except QuestionError as exc:
            return str(exc)
        running = self.running(spec.goal_id)
        if len(running) >= self.max_per_goal:
            return (
                f"This goal already has {len(running)} experiments running; the limit is {self.max_per_goal}."
            )
        used = sum(e.spec.share for e in self.running())
        if used + spec.share > self.max_total_share + _EPS:
            return (
                f"{used:.1%} of the agent group is already in experiments and this one needs "
                f"{spec.share:.1%}; the limit is {self.max_total_share:.0%}."
            )
        default = self.default_for(spec.goal_id, spec.kind)
        if default is not None and spec.reference_arm != default:
            return f"{default!r} is the promoted default for this goal, so it must be the reference arm."
        return None

    def note_assignment(self, experiment_id: str, at: datetime) -> None:
        """Record the time of the first customer assigned to the experiment (the first send is later)."""
        exp = self._experiments.get(experiment_id)
        if exp is not None and exp.first_assignment_at is None:
            self._experiments[experiment_id] = exp.model_copy(update={"first_assignment_at": at})

    # --- reviewing and ending ---------------------------------------------------------------------

    def review(self, experiment_id: str, outcomes: Outcomes, now: datetime) -> Review:
        """Read the experiment and apply its own rules; end it if they say so. Idempotent once ended."""
        exp = self.get(experiment_id)
        reading = read(exp, outcomes, now)
        if not exp.running:
            return Review(exp, reading, Judgement("continue", exp.winner, exp.losing, exp.end_reason or ""))
        if not exp.digest_matches():
            raise RuntimeError(f"experiment {experiment_id!r} no longer matches what was registered")
        judgement = judge(exp, reading)
        if judgement.action != "continue":
            kind = {
                "winner": EndKind.WINNER,
                "no_winner": EndKind.NO_WINNER,
                "stop_losing": EndKind.STOPPED_LOSING,
            }[judgement.action]
            exp = self._end(exp, now, kind, judgement.reason, judgement.winner, judgement.losing)
        return Review(exp, reading, judgement)

    def stop(self, experiment_id: str, now: datetime, reason: str) -> Experiment:
        """End a running experiment by hand (one-click pause). Nothing is promoted."""
        exp = self.get(experiment_id)
        if not exp.running:
            return exp
        return self._end(exp, now, EndKind.STOPPED_MANUALLY, reason, None, ())

    def _end(
        self,
        exp: Experiment,
        now: datetime,
        kind: EndKind,
        reason: str,
        winner: str | None,
        losing: Sequence[str],
    ) -> Experiment:
        ended = exp.model_copy(
            update={
                "status": Status.ENDED,
                "ended_at": now,
                "end_kind": kind,
                "end_reason": reason,
                "winner": winner,
                "losing": tuple(losing),
            }
        )
        self._experiments[exp.experiment_id] = ended
        self._append(now, exp.experiment_id, "ended", f"{kind.value}: {reason}")
        return ended

    # --- promotion --------------------------------------------------------------------------------

    def promote(
        self, experiment_id: str, brief: Brief, now: datetime, reason: str | None = None
    ) -> Promotion:
        """Make the winner the goal's default, inside the approved envelope, or raise `PromotionRefused`."""
        exp = self.get(experiment_id)
        if exp.running or exp.end_kind is not EndKind.WINNER or exp.winner is None:
            raise PromotionRefused("Only an experiment that ended with a winner can be promoted.")
        if exp.promoted:
            raise PromotionRefused("This winner has already been promoted.")
        if not exp.digest_matches():
            raise PromotionRefused("The experiment no longer matches what was registered.")
        if not brief.approved or brief.goal_id != exp.goal_id:
            raise PromotionRefused("Needs the goal's approved brief; the winner stays inside its envelope.")
        try:
            check_arm(exp.spec.kind, exp.winner, brief.envelope)
        except QuestionError as exc:
            raise PromotionRefused(
                f"{exp.winner!r} is no longer inside the approved envelope: {exc}"
            ) from exc
        promotion = Promotion(
            promotion_id=stable_id("promo", experiment_id, now.isoformat()),
            experiment_id=experiment_id,
            goal_id=exp.goal_id,
            kind=exp.spec.kind,
            value=exp.winner,
            previous=self.default_for(exp.goal_id, exp.spec.kind),
            promoted_at=now,
            reason=reason or exp.end_reason or "The experiment found a winner.",
        )
        self._promotions.append(promotion)
        self._experiments[experiment_id] = exp.model_copy(update={"promoted": True})
        self._append(
            now,
            experiment_id,
            "promoted",
            f"{exp.spec.label_of(exp.winner)} is now the default {exp.spec.kind.value} for goal "
            f"{exp.goal_id} (before: {promotion.previous or 'no default'}). {promotion.reason}",
        )
        return promotion

    def revert(self, promotion_id: str, now: datetime, reason: str) -> Promotion:
        """Undo a promotion: the goal goes back to the default before it (or to none)."""
        for i, p in enumerate(self._promotions):
            if p.promotion_id == promotion_id:
                if not p.active:
                    return p
                undone = p.model_copy(update={"reverted_at": now, "reverted_reason": reason})
                self._promotions[i] = undone
                self._append(
                    now,
                    p.experiment_id,
                    "reverted",
                    f"{p.value} is no longer the default {p.kind.value} for goal {p.goal_id}. {reason}",
                )
                return undone
        raise KeyError(f"no promotion called {promotion_id!r}")

    # --- the log, and carrying the book ------------------------------------------------------------

    def _append(
        self,
        at: datetime,
        experiment_id: str,
        kind: str,
        detail: str,
    ) -> None:
        self._log.append(
            ExperimentEvent.model_validate(
                {
                    "seq": len(self._log) + 1,
                    "at": at,
                    "experiment_id": experiment_id,
                    "kind": kind,
                    "detail": detail,
                }
            )
        )

    def snapshot(self) -> dict[str, Any]:
        """The whole book as plain, JSON-able data."""
        return {
            "experiments": [json.loads(e.model_dump_json()) for e in self.all()],
            "promotions": [json.loads(p.model_dump_json()) for p in self._promotions],
            "log": [json.loads(e.model_dump_json()) for e in self._log],
        }

    @classmethod
    def restore(cls, data: dict[str, Any]) -> ExperimentBook:
        """A book from `snapshot()` data. Registered rules are kept as they were stored."""
        book = cls()
        for raw in data.get("experiments", []):
            exp = Experiment.model_validate(raw)
            book._experiments[exp.experiment_id] = exp
        book._promotions = [Promotion.model_validate(p) for p in data.get("promotions", [])]
        book._log = [ExperimentEvent.model_validate(e) for e in data.get("log", [])]
        return book
