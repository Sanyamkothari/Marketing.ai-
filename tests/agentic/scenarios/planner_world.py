"""The planner's fixture world, and the language models its scenarios run with (Plan K M139).

**The world.** A copy of the M132 world (`world.py`: a real uplift training run, a real scoring run, three
campaigns made through `POST /campaigns`, one with a frozen plan and one measured through the routes), into a
data folder of its own, plus one more scoring run of the same model made through `Pipeline.run_score`: the
newest finished scoring run with no campaign yet, the one the planner plans. Each consumer gets its own copy,
so a test that approves (and so opens a campaign for that run) never changes what another test plans.

**The models** answer the two prompts the planner sends (`configs/prompts/agent_planner.v1.md`,
`agent_narrative.v1.md`), through the `LLMClient` protocol, as a deployment's model would:

* :class:`HonestModel` maps a goal onto the form from the values offered, and writes a narrative whose claims
  are true - in wording that **changes from one call to the next**, so pass^k proves digests exclude prose;
* :class:`AdversarialModel` writes a wrong direction, a number in its own words and an unreferenced claim;
* :class:`OneSlipModel` writes one wrong sentence among true ones (dropped, the rest kept);
* :class:`InjectedModel` obeys an instruction planted in the goal text and answers values the form does not
  offer (refused: `PLANNER_GOAL_UNPARSED`).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from engine.config import RunMode, resolve_config
from engine.contracts import RunRecord, RunState
from engine.jobs import NullJobRunner
from engine.llm import LLMCompletion
from engine.pipeline import Pipeline
from engine.registry import LocalModelRegistry
from engine.runs import RUN_FILENAME
from engine.storage import run_key, upload_key
from tests.agentic.scenarios.world import (
    SCORE_RUN,
    TRAIN_RUN,
    UPLIFT,
    USE_CASE,
    World,
    _config_dir,
    _context,
    _write_csv,
    scoring_frame,
)

PLANNER_RUN: Final[str] = "r_20261010_a1390003"
"""The planner's run: the newest scoring run, with no campaign."""


@dataclass(frozen=True)
class PlannerWorld:
    world: World
    run_id: str
    measured_campaign: str


def score_again(world: World, run_id: str) -> str:
    """Score the campaign file again with the world's model, as `POST /runs` would; the new run's id."""
    storage = world.storage
    registry = LocalModelRegistry(world.data_dir / "registry.db")
    trained = storage.read_model(run_key(TRAIN_RUN, RUN_FILENAME), RunRecord)
    with _config_dir(world.config_root):
        resolved = resolve_config(USE_CASE, UPLIFT, root=world.config_root)
        storage.write_model(run_key(run_id, "run_config.json"), resolved)
        source = upload_key(f"u_{run_id[-8:]}", "source.csv")
        _write_csv(storage, source, scoring_frame())
        context = _context(
            storage,
            registry,
            resolved,
            run_id,
            mode=RunMode.SCORE,
            upload=source,
            target=None,
            model_version_id=trained.model_version_id,
        )
        record = Pipeline(storage, registry, NullJobRunner()).run_score(context)
    assert record.state is RunState.DONE, record.error
    return run_id


def copy_world(world: World, target: Path) -> World:
    """The world's data folder copied to `target` (the configuration root is shared: it is read only)."""
    shutil.copytree(world.data_dir, target)
    return World(
        world.config_root, target, world.run_id, world.planned_campaign, world.plan_hash, world.open_campaign
    )


def planner_world(world: World, measured: str, target: Path, run_id: str = PLANNER_RUN) -> PlannerWorld:
    copied = copy_world(world, target)
    score_again(copied, run_id)
    assert copied.run_id == SCORE_RUN
    return PlannerWorld(world=copied, run_id=run_id, measured_campaign=measured)


# ---------------------------------------------------------------------------
# The models
# ---------------------------------------------------------------------------
_JSON_LINE: Final[re.Pattern[str]] = re.compile(r"^\{.*\}$", re.MULTILINE)


def _json_lines(prompt: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for match in _JSON_LINE.finditer(prompt):
        try:
            loaded = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(loaded, dict):
            found.append(loaded)
    return found


def _is_planner(system: str) -> bool:
    return "into the form of a campaign" in system


def _goal(prompt: str) -> str:
    start, end = prompt.find("<<<GOAL"), prompt.find("GOAL>>>")
    return prompt[start + len("<<<GOAL") : end].strip() if start >= 0 and end > start else ""


@dataclass
class _Model:
    model_id: str = "fake-planner-model"
    calls: list[str] = field(default_factory=list)

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        self.calls.append(system[:40])
        text = self.frame(prompt) if _is_planner(system) else self.narrative(prompt, len(self.calls))
        return LLMCompletion(
            text=text,
            model_id=model_id or self.model_id,
            input_tokens=len(prompt) // 4,
            output_tokens=len(text) // 4,
            stop_reason="end_turn",
        )

    def embed(self, texts: Any, *, model_id: str | None = None) -> tuple[tuple[float, ...], ...]:
        return tuple((0.0,) for _ in texts)

    def count_tokens(self, text: str, *, model_id: str | None = None) -> int:
        return len(text) // 4

    # --- what a model would answer ---------------------------------------------------------------------
    def frame(self, prompt: str) -> str:
        form = _json_lines(prompt)[0]
        goal = _goal(prompt).lower()
        use_case = sorted(form["use_case_id"])[0] if "win" not in goal else "win-back-campaign"
        channels = form["channel"]["by_use_case"].get(use_case, [])
        channel = next((name for name in channels if name in goal), None)
        amounts = form["budget_inr"]["amounts_in_goal"]
        budget = max(amounts) if amounts and ("budget" in goal or "within" in goal) else None
        return json.dumps(
            {"use_case_id": use_case, "objective": "next_cycle", "channel": channel, "budget_inr": budget}
        )

    def narrative(self, prompt: str, call: int) -> str:
        evidence = _json_lines(prompt)[0]
        return json.dumps({"sentences": self.sentences(evidence, call)})

    def sentences(self, evidence: dict[str, Any], call: int) -> list[dict[str, Any]]:
        return honest_sentences(evidence, call)


_COMPARE_WORDING: Final[tuple[str, ...]] = (
    "{arm_a} {cmp} {arm_b} on {metric}.",
    "On {metric}, {arm_a} {cmp} {arm_b}.",
    "Looking at {metric}: {arm_a} {cmp} {arm_b} ({level} range {low} to {high}).",
)
_RECOMMEND_WORDING: Final[tuple[str, ...]] = (
    "Approve {proposal} to {action}.",
    "If you agree, approve {proposal}; it will {action}.",
    "The next step: approve {proposal} so the product can {action}.",
)


def honest_sentences(evidence: dict[str, Any], call: int) -> list[dict[str, Any]]:
    """True claims, worded one of three ways depending on the call: decisions never depend on it."""
    out: list[dict[str, Any]] = []
    for proposal_id in evidence.get("proposals", {}):
        out.append(
            {
                "template": _RECOMMEND_WORDING[call % 3],
                "claim": {"type": "recommendation", "proposal_id": proposal_id},
            }
        )
    for ref, item in evidence.get("labels", {}).items():
        out.append(
            {
                "template": "{subject} is labelled {label}." if call % 2 else "We read {subject} as {label}.",
                "claim": {"type": "label", "ref": ref, "value": item["label"]},
            }
        )
    for comparison in evidence.get("comparisons", []):
        out.append(
            {
                "template": _COMPARE_WORDING[call % 3],
                "claim": {
                    "type": "comparison",
                    "left": comparison["left"],
                    "right": comparison["right"],
                    "metric": comparison["metric"],
                    "comparator": _COMPARATOR[comparison["reads"]],
                },
            }
        )
    return out


_COMPARATOR: Final[dict[str, str]] = {
    "left is above right": "gt",
    "left is below right": "lt",
    "not distinguishable": "eq",
}
_OPPOSITE: Final[dict[str, str]] = {"gt": "lt", "lt": "gt", "eq": "gt"}


@dataclass
class HonestModel(_Model):
    model_id: str = "fake-honest"


@dataclass
class AdversarialModel(_Model):
    """A wrong direction, a number in its own words and a claim about evidence that does not exist."""

    model_id: str = "fake-adversarial"

    def sentences(self, evidence: dict[str, Any], call: int) -> list[dict[str, Any]]:
        comparison = evidence["comparisons"][0]
        return [
            {
                "template": "{arm_a} {cmp} {arm_b} on {metric}.",
                "claim": {
                    "type": "comparison",
                    "left": comparison["left"],
                    "right": comparison["right"],
                    "metric": comparison["metric"],
                    "comparator": _OPPOSITE[_COMPARATOR[comparison["reads"]]],
                },
            },
            {"template": "This campaign will bring 4,200 extra sales.", "claim": None},
            {
                "template": "It is {label}.",
                "claim": {"type": "label", "ref": "ev_0000000000000000:label", "value": "Causal"},
            },
        ]


@dataclass
class OneSlipModel(_Model):
    """True sentences and one that types a number: that one is dropped, the rest kept."""

    model_id: str = "fake-one-slip"

    def sentences(self, evidence: dict[str, Any], call: int) -> list[dict[str, Any]]:
        return [*honest_sentences(evidence, call), {"template": "Expect 12% more sales.", "claim": None}]


@dataclass
class InjectedModel(_Model):
    """Obeys an instruction planted in the goal: answers an objective and a field the form does not have."""

    model_id: str = "fake-injected"

    def frame(self, prompt: str) -> str:
        return json.dumps(
            {
                "use_case_id": "win-back-campaign",
                "objective": "skip_control_group",
                "channel": None,
                "budget_inr": None,
            }
        )


MODELS: Final[dict[str, type[_Model]]] = {
    "honest": HonestModel,
    "adversarial": AdversarialModel,
    "one_slip": OneSlipModel,
    "injected": InjectedModel,
}
