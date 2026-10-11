"""The campaign planner: a warning card or a goal becomes one approvable campaign plan (Plan K M139, design §6).

A **state machine with one interrupt**, not a free tool-use loop:

```
TRIGGER (an M105 warning card | a goal: a form, or words parsed by the language model)
  → FRAME       rules map the trigger to {use case, objective, channel, budget}
  → SIZE        the newest scoring run with no campaign yet; plan_test on everyone it compares (the eligible
                rows and the held-back ones, as the product's preview), at the holdout in force; the outcome
                kind (a yes/no rate or an amount) of the use case's newest measured campaign
  → CANDIDATES  check_contactable / value_segment / evaluate_policy: one candidate per channel
  → SELECT      rules: prefer M137's `safe`, never `not_safe`; the first candidate the policy allows
  → CHECK       the M132 policy's decisions on the one selected
  → DRAFT       Proposal(kind=campaign_plan) citing every tool result, with a claim-checked narrative
  → INTERRUPT   a person approves it (engine.approvals.decide_proposal) ← the only way forward
  → EXECUTE     the treat list, the campaign, the frozen test plan, the measure schedule (if one exists)
```

**Every number is a tool's.** The planner calls only registry tools (`engine.agentic.tools`), so each fact it
uses has an evidence id on the ledger; the proposal cites all of them. **The language model touches at most
two steps**, both optional (K2): it may map goal *words* onto the form, choosing only from enumerated values
(`AGENT_PLANNER`: use cases, objectives, channels, and amounts code read from the words), and it may write the
narrative as structured claims (`AGENT_NARRATIVE`), which `engine.agentic.claims` verifies before rendering.
SELECT is rules only. Without an AI service, typed words are refused with `AI_NOT_CONNECTED`, a card or the
form works unchanged, and the narrative is the rules-only summary. Payload digests exclude the narrative, so a
model that words things differently from one run to the next leaves every digest unchanged (pass^k).

**Persisted per transition** at `agentic_runs/<plan_id>/state.json` (aggregates only: ids, counts, the tool
results' outputs), under a lock per plan, as Plan G keeps a session; a crash between two steps resumes from the
last one written (:func:`resume_plan`, `POST /agentic/plans/{plan_id}/resume`). DRAFT is idempotent: the proposal
id is written to the state before the proposal is drafted, and a resumed DRAFT reuses the stored proposal, so a
crash inside it never leaves two pending proposals for one plan. A plan that cannot go on stops with a code: `PLANNER_NOTHING_TO_PLAN`
(the card asks for waiting or a person's decision, not a campaign), `PLANNER_NO_CANDIDATE` (no scoring run
without a campaign, no channel, nothing within the budget or safe to roll out), `PLANNER_GOAL_UNPARSED`,
`AI_NOT_CONNECTED`.

**Execution** (:func:`execute_plan`) runs only from an approval, through the product's own functions, which the
caller passes in (the `/agentic` route passes `engine.decide.treat_list.build_treat_list` under the approved
limits, `engine.measurement.cycle.open_campaign` - the engine half of `POST /campaigns` - and
`api.routes.campaigns.register_plan`). Plan M M184 (DEC-1614 (a)): what is executed is what the policy checked -
the treat list contacts only the approved channel and at most `planned_contacts` (`engine.decide.campaign_scope`),
or EXECUTE refuses with `CAMPAIGN_LIST_NOT_AS_APPROVED` before anything is opened; with
`agentic.require_sign_in` on, only a signed-in Approver's approval is carried out (DEC-1614 (e)), and a run whose
model is not approved (waiting, turned down, or a candidate nobody approved) is refused with `MODEL_NOT_APPROVED`
while approval is required (DEC-1614 (b), `engine.model_approval`). It is idempotent: each thing it
makes is written to the state before the next, so an approval retried after a failure does not open a second campaign. The campaign's treatment starts at
the approval (nobody is contacted before it), so the drafted analysis date - the later of the run's finish and
the draft, plus the outcome window - must still be at least a window after the approval, or the approval is
refused with `PROPOSAL_STALE` (draft again). No measure schedule is created: one needs a saved connection and a
table a person chooses, so an enabled one is named when it exists and otherwise the hand-off says when to measure.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import AwareDatetime, Field, model_validator

from engine.agentic.claims import (
    ComparisonFact,
    EvidenceIndex,
    Interval,
    Narrative,
    SeriesFact,
    guardrails_for,
    rules_narrative,
    verify_narrative,
)
from engine.agentic.errors import AgenticError
from engine.agentic.policy import (
    CampaignPlanPayload,
    Decision,
    HoldoutTerms,
    ProposalSubject,
    allowed,
    build_context,
    evaluate,
)
from engine.agentic.registry import EvidenceLabel, ToolContext
from engine.config import StrictBase
from engine.measurement.plan import TestPlanInput

if TYPE_CHECKING:
    from engine.agentic.proposals import Proposal
    from engine.agentic.registry import ToolRegistry
    from engine.config import UseCaseConfig
    from engine.contracts import RunRecord
    from engine.generative.budget import Meter
    from engine.measurement.summary import SummaryCard

__all__ = [
    "CAMPAIGN_LIST_NOT_AS_APPROVED",
    "CARD_OBJECTIVES",
    "NOTHING_TO_PLAN",
    "OBJECTIVES",
    "PLANNER_CODES",
    "PLANS_PREFIX",
    "BuiltList",
    "CardRef",
    "ExecuteActions",
    "Frame",
    "GoalForm",
    "ListLimits",
    "PlanRequest",
    "PlannerDeps",
    "PlannerState",
    "amounts_in",
    "evidence_index",
    "execute_plan",
    "find_plan_for_proposal",
    "load_plan",
    "outcome_of",
    "resume_plan",
    "start_plan",
]

PLANS_PREFIX: Final[str] = "agentic_runs/"
STATE_FILENAME: Final[str] = "state.json"

PLANNER_NO_CANDIDATE: Final[str] = "PLANNER_NO_CANDIDATE"
PLANNER_NOTHING_TO_PLAN: Final[str] = "PLANNER_NOTHING_TO_PLAN"
PLANNER_CARD_NOT_FOUND: Final[str] = "PLANNER_CARD_NOT_FOUND"
PLANNER_NOT_FOUND: Final[str] = "PLANNER_NOT_FOUND"
PLANNER_GOAL_UNPARSED: Final[str] = "PLANNER_GOAL_UNPARSED"
CAMPAIGN_LIST_NOT_AS_APPROVED: Final[str] = "CAMPAIGN_LIST_NOT_AS_APPROVED"
"""Plan M M184 (DEC-1614 (a)): EXECUTE's list is not the one the policy checked; nothing is opened."""
PLANNER_CODES: Final[frozenset[str]] = frozenset(
    {
        PLANNER_NO_CANDIDATE,
        PLANNER_NOTHING_TO_PLAN,
        PLANNER_CARD_NOT_FOUND,
        PLANNER_NOT_FOUND,
        PLANNER_GOAL_UNPARSED,
        CAMPAIGN_LIST_NOT_AS_APPROVED,
    }
)

Objective = Literal[
    "next_cycle", "restore_holdout", "larger_test", "protect_holdout", "rank_by_risk", "confirm_effect"
]
OBJECTIVES: Final[tuple[str, ...]] = (
    "next_cycle",
    "restore_holdout",
    "larger_test",
    "protect_holdout",
    "rank_by_risk",
    "confirm_effect",
)

CARD_OBJECTIVES: Final[dict[str, Objective]] = {
    "no_control": "restore_holdout",
    "underpowered": "larger_test",
    "contamination": "protect_holdout",
    "uplift_not_better_than_risk": "rank_by_risk",
    "uplift_fading": "confirm_effect",
}
"""The cards a campaign answers: the next cycle, with the holdout in force kept and a test plan registered."""

NOTHING_TO_PLAN: Final[dict[str, str]] = {
    "early_look": (
        "This result was read before its analysis date. The next step is to wait for that date and measure "
        "again, not to send another campaign."
    ),
    "drift": (
        "The scoring data has drifted from what the model learned from. Retrain or check the data first; a "
        "campaign planned on these scores would rest on a model that no longer fits."
    ),
    "challenger_ready": (
        "A challenger model is waiting for an Approver on the Models page. That is a person's decision, "
        "not a campaign."
    ),
    "backfire": (
        "A group may have been harmed. Decide the Value Proof Pack's proposal about that group first; a new "
        "campaign would contact it again."
    ),
    # Plan K M138 (DEC-1403), joined at integration: the early harm alarm is a person's decision, never a new plan.
    "harm_alarm": (
        "This campaign may be doing harm. A person decides whether to pause it; a new campaign would add to it, "
        "and its own result is still read only on the planned day."
    ),
}
"""The cards that ask for waiting or a person's decision: no campaign is drafted, and the reason is said."""

Step = Literal[
    "FRAME", "SIZE", "CANDIDATES", "SELECT", "CHECK", "DRAFT", "INTERRUPT", "EXECUTE", "DONE", "STOPPED"
]
_ORDER: Final[tuple[Step, ...]] = ("FRAME", "SIZE", "CANDIDATES", "SELECT", "CHECK", "DRAFT")
_ID: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$"
MAX_GOAL_CHARS: Final[int] = 500
MAX_CYCLES: Final[int] = 6


# ---------------------------------------------------------------------------
# The trigger
# ---------------------------------------------------------------------------
class CardRef(StrictBase):
    """A warning card on the Results page, by what identifies it; the server finds the card itself."""

    code: str = Field(pattern=r"^[A-Z][A-Z_]{2,63}$", description="The card's code, e.g. PLAN_UNDERPOWERED.")
    campaign_id: str | None = Field(default=None, pattern=_ID)
    use_case_id: str | None = Field(default=None, pattern=_ID)


class GoalForm(StrictBase):
    """A goal entered through the form: every field an enumerated value or a number the person typed."""

    use_case_id: str = Field(pattern=_ID)
    objective: Objective = "next_cycle"
    channel: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    budget_inr: float | None = Field(
        default=None, ge=0.0, le=1e12, description="The most the offers may cost."
    )
    mde_pp: float | None = Field(
        default=None, gt=0.0, le=100.0, description="The smallest effect worth detecting, in points."
    )


class PlanRequest(StrictBase):
    """`POST /agentic/plans`: exactly one of a card, the form, or the goal in words."""

    card: CardRef | None = None
    goal: GoalForm | None = None
    goal_text: str | None = Field(default=None, min_length=1, max_length=MAX_GOAL_CHARS)

    @model_validator(mode="after")
    def _one(self) -> PlanRequest:
        given = [item for item in (self.card, self.goal, self.goal_text) if item is not None]
        if len(given) != 1:
            raise ValueError("give exactly one of card, goal or goal_text")
        return self


# ---------------------------------------------------------------------------
# The state
# ---------------------------------------------------------------------------
class Frame(StrictBase):
    use_case_id: str
    objective: Objective
    channel: str | None = None
    budget_inr: float | None = None
    mde_pp: float | None = None
    source: Literal["card", "form", "words"]
    card_code: str | None = None
    card_campaign_id: str | None = None


class Evidence(StrictBase):
    """One tool result the plan rests on (its output is aggregates only)."""

    evidence_id: str
    tool: str
    label: EvidenceLabel
    output: dict[str, Any]


class Size(StrictBase):
    run_id: str
    training_run_id: str | None
    eligible_rows: int = Field(description="Rows neither suppressed nor held back (who may be contacted).")
    held_back_rows: int = Field(default=0, description="Rows held back as the control group.")
    population_rows: int = Field(
        default=0, description="Everyone the campaign compares: the eligible rows plus the held-back ones."
    )
    outcome_kind: Literal["binary", "continuous"] = Field(
        default="binary",
        description="A yes/no outcome (a rate) or an amount, as the newest measured campaign measured it.",
    )
    holdout_fraction: float
    holdout_scope: Literal["run", "use_case", "universal"]
    outcome_column: str
    outcome_window_days: int
    analysis_date: date
    base_rate: float | None
    base_rate_source: str | None
    mde_pp: float | None
    detectable_mde_pp: float | None
    baseline_campaign_id: str | None
    measured_campaigns: tuple[str, ...] = Field(
        description="Measured campaigns read, newest first (by when they were opened)."
    )


class Candidate(StrictBase):
    channel: str
    contactable_rows: int
    planned_contacts: int
    offer_spend_inr: float | None
    ranking: Literal["uplift", "propensity_model", "risk"]
    rollout: Literal["safe", "not_safe", "cannot_tell"] | None
    excluded: str | None = Field(default=None, description="Why SELECT may not choose it; null when it may.")


class Tried(StrictBase):
    channel: str
    codes: tuple[str, ...]


class Execution(StrictBase):
    treat_list_rows: int | None = None
    contacts: int | None = Field(
        default=None,
        description="Plan M M184: customers the treat list contacts, all on the approved channel; null before.",
    )
    campaign_id: str | None = None
    plan_hash: str | None = None
    measure_schedule_id: str | None = None
    measure_on: date | None = None
    note: str | None = None


class Stop(StrictBase):
    code: str
    message: str
    status: int


class Transition(StrictBase):
    step: Step
    at: AwareDatetime


class PlannerState(StrictBase):
    """`agentic_runs/<plan_id>/state.json`: where the plan stands, and everything it rests on."""

    plan_id: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    created_by: str
    actor: str
    request: PlanRequest
    step: Step = Field(description="The last step completed (INTERRUPT: waiting for a person).")
    frame: Frame | None = None
    size: Size | None = None
    evidence: dict[str, Evidence] = Field(default_factory=dict, description="By name, e.g. plan_test.")
    candidates: tuple[Candidate, ...] = ()
    tried: tuple[Tried, ...] = ()
    selected: Candidate | None = None
    checks: tuple[Decision, ...] = ()
    reserved_proposal_id: str | None = Field(
        default=None,
        description="The id DRAFT drafts its proposal under, written before drafting so a resume reuses it.",
    )
    proposal_id: str | None = None
    proposal_status: str | None = None
    payload_digest: str | None = None
    narrative: Narrative | None = None
    narrative_error: str | None = Field(
        default=None,
        description="Why the model wrote no narrative (an AI service or budget error's code); the rules wrote it.",
    )
    llm_calls: int = 0
    llm_cost_usd: float = Field(
        default=0.0, description="What the model calls cost, from the Meter; a call it cannot price adds 0."
    )
    execution: Execution | None = None
    stopped: Stop | None = None
    history: tuple[Transition, ...] = ()

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item.evidence_id for item in self.evidence.values()}))


def state_key(plan_id: str) -> str:
    return f"{PLANS_PREFIX}{plan_id}/{STATE_FILENAME}"


_LOCKS_GUARD: Final[threading.Lock] = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def _plan_lock(plan_id: str) -> Iterator[None]:
    """One plan is advanced by one caller at a time (Plan G's session lock pattern; one API process)."""
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(plan_id, threading.Lock())
    with lock:
        yield


def _safe_plan_id(plan_id: str) -> bool:
    return plan_id.startswith("plan_") and plan_id[5:].isalnum() and len(plan_id) <= 40


def load_plan(ctx: ToolContext, plan_id: str) -> PlannerState:
    from engine.agentic.artefacts import read_stored_model

    found = (
        read_stored_model(ctx.storage, state_key(plan_id), PlannerState) if _safe_plan_id(plan_id) else None
    )
    if found is None:
        raise AgenticError(PLANNER_NOT_FOUND, f"No plan with id {plan_id!r}.", status=404)
    return found


def _save(ctx: ToolContext, state: PlannerState, step: Step) -> PlannerState:
    now = ctx.clock()
    updated = state.model_copy(
        update={"step": step, "updated_at": now, "history": (*state.history, Transition(step=step, at=now))}
    )
    ctx.storage.write_model(state_key(updated.plan_id), updated)
    return updated


# ---------------------------------------------------------------------------
# Inputs to a run of the planner
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlannerDeps:
    """What the planner may use beyond the tool context: the registry, the cards and, optionally, a model."""

    registry: ToolRegistry
    cards: Callable[[], Sequence[SummaryCard]] | None = None
    """The Results page's warning cards (`engine.measurement.summary.build_summary`); None reads them."""
    meter: Meter | None = None
    """The Product AI through the Meter, for goal words (the use case is not known yet); None means no AI
    service is connected, and words are refused."""
    model_registry: Any = None
    """The model registry for the challenger card; None draws no challenger card."""
    meter_for: Callable[[str], Meter | None] | None = None
    """The Product AI metered under one use case's `generative.llm` and `generative.budget`, for the narrative
    once FRAME knows the use case; None uses `meter`. Returning None writes the rules-only narrative."""

    def narrative_meter(self, use_case_id: str | None) -> Meter | None:
        if self.meter_for is not None and use_case_id is not None:
            return self.meter_for(use_case_id)
        return self.meter


def _stop(code: str, message: str, status: int = 409) -> AgenticError:
    return AgenticError(code, message, status=status)


def start_plan(request: PlanRequest, ctx: ToolContext, deps: PlannerDeps) -> PlannerState:
    """Create a plan for `request` and run it to the interrupt (or to its stop)."""
    from engine.agentic.config import require_enabled

    require_enabled(ctx.config_root)
    if request.goal_text is not None and deps.meter is None:
        raise AgenticError(
            "AI_NOT_CONNECTED",
            "Typing a goal in words needs an AI service. Connect one under Connections, or use the form or a "
            "warning card, which work without one.",
            status=409,
        )
    now = ctx.clock()
    state = PlannerState(
        plan_id=f"plan_{secrets.token_hex(8)}",
        created_at=now,
        updated_at=now,
        created_by=ctx.principal.user_id,
        actor=ctx.actor,
        request=request,
        step="FRAME",
    )
    ctx.storage.write_model(state_key(state.plan_id), state)
    with _plan_lock(state.plan_id):
        return _advance(state, ctx, deps, first=True)


def resume_plan(plan_id: str, ctx: ToolContext, deps: PlannerDeps) -> PlannerState:
    """Continue a plan from the last step it wrote (after a crash); a plan at its interrupt or end is returned."""
    with _plan_lock(plan_id):
        return _advance(load_plan(ctx, plan_id), ctx, deps, first=False)


def _advance(state: PlannerState, ctx: ToolContext, deps: PlannerDeps, *, first: bool) -> PlannerState:
    if state.step in ("INTERRUPT", "EXECUTE", "DONE", "STOPPED"):
        return state
    steps: dict[Step, Callable[[PlannerState, ToolContext, PlannerDeps], PlannerState]] = {
        "FRAME": _frame,
        "SIZE": _size,
        "CANDIDATES": _candidates,
        "SELECT": _select,
        "CHECK": _check,
        "DRAFT": _draft,
    }
    # A new plan has completed nothing; a stored one has completed `state.step` unless it has no frame yet.
    position = 0 if first or state.frame is None else _ORDER.index(state.step) + 1
    for step in _ORDER[position:]:
        try:
            state = steps[step](state, ctx, deps)
        except AgenticError as exc:
            stop = Stop(code=exc.code, message=exc.message, status=exc.status)
            return _save(ctx, state.model_copy(update={"stopped": stop}), "STOPPED")
        state = _save(ctx, state, step)
    if state.proposal_status == "pending":
        return _save(ctx, state, "INTERRUPT")
    return _save(ctx, state, "DONE")


# ---------------------------------------------------------------------------
# FRAME
# ---------------------------------------------------------------------------
_AMOUNT: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(lakhs?|lacs?|crores?|cr|k|thousand|million|mn)?\b", re.IGNORECASE
)
_MULTIPLIER: Final[dict[str, float]] = {
    "lakh": 1e5,
    "lakhs": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "crore": 1e7,
    "crores": 1e7,
    "cr": 1e7,
    "k": 1e3,
    "thousand": 1e3,
    "million": 1e6,
    "mn": 1e6,
}


def amounts_in(text: str) -> tuple[float, ...]:
    """Every amount written in `text`, in rupees (`2 lakh` is 200000, `₹1,50,000` is 150000), sorted.

    Code reads them; the language model may only say which of them is the budget, never write one.
    """
    found: set[float] = set()
    for match in _AMOUNT.finditer(text):
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        word = (match.group(2) or "").lower()
        found.add(round(value * _MULTIPLIER.get(word, 1.0), 2))
    return tuple(sorted(found))


def _use_case_choices(ctx: ToolContext) -> dict[str, tuple[str, ...]]:
    """Use cases a campaign can be planned for, with their configured channels."""
    import yaml

    from engine.config import ConfigError, load_use_case
    from engine.uplift.measure import measure_offered

    out: dict[str, tuple[str, ...]] = {}
    directory = ctx.config_root / "use_cases"
    for path in sorted(directory.glob("*.yaml")):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            config = load_use_case(str(document.get("id", "")), ctx.config_root)
        except (ConfigError, OSError, ValueError, yaml.YAMLError):
            continue
        if not measure_offered(config):
            continue
        out[config.id] = tuple(config.actions.suppression.channels)
    return out


def _frame(state: PlannerState, ctx: ToolContext, deps: PlannerDeps) -> PlannerState:
    request = state.request
    if request.card is not None:
        frame = _frame_from_card(request.card, ctx, deps)
    elif request.goal is not None:
        goal = request.goal
        frame = Frame(
            use_case_id=goal.use_case_id,
            objective=goal.objective,
            channel=goal.channel,
            budget_inr=goal.budget_inr,
            mde_pp=goal.mde_pp,
            source="form",
        )
    else:
        meter = deps.meter
        before = (0, 0.0) if meter is None else (meter.calls, _spent(meter))
        try:
            frame = _frame_from_words(str(request.goal_text), ctx, deps)
        finally:
            if meter is not None:
                state = state.model_copy(
                    update={
                        "llm_calls": state.llm_calls + meter.calls - before[0],
                        "llm_cost_usd": round(state.llm_cost_usd + _spent(meter) - before[1], 6),
                    }
                )
    return state.model_copy(update={"frame": frame})


def _frame_from_card(ref: CardRef, ctx: ToolContext, deps: PlannerDeps) -> Frame:
    cards = deps.cards() if deps.cards is not None else _summary_cards(ctx, deps)
    card = next(
        (
            card
            for card in cards
            if card.code == ref.code
            and (ref.campaign_id is None or card.campaign_id == ref.campaign_id)
            and (ref.use_case_id is None or card.use_case_id == ref.use_case_id)
        ),
        None,
    )
    if card is None:
        raise _stop(
            PLANNER_CARD_NOT_FOUND,
            "That warning is not on the Results page any more (its condition no longer holds), so there is "
            "nothing to plan from it.",
            404,
        )
    if card.kind in NOTHING_TO_PLAN:
        raise _stop(PLANNER_NOTHING_TO_PLAN, NOTHING_TO_PLAN[card.kind])
    use_case = card.use_case_id
    if use_case is None and card.campaign_id is not None and ctx.campaigns is not None:
        campaign = ctx.campaigns.get(card.campaign_id)
        use_case = None if campaign is None else campaign.use_case_id
    if use_case is None:
        raise _stop(PLANNER_NO_CANDIDATE, "The warning names no use case to plan a campaign for.")
    return Frame(
        use_case_id=use_case,
        objective=CARD_OBJECTIVES.get(card.kind, "next_cycle"),
        source="card",
        card_code=card.code,
        card_campaign_id=card.campaign_id,
    )


def _summary_cards(ctx: ToolContext, deps: PlannerDeps) -> Sequence[SummaryCard]:
    from engine.measurement.summary import SummaryTraceError, build_summary

    if ctx.campaigns is None:
        return ()
    try:
        summary = build_summary(
            ctx.storage,
            ctx.campaigns.list(limit=None),
            registry=deps.model_registry,
            root=ctx.config_root,
            now=ctx.clock(),
        )
    except SummaryTraceError as exc:
        raise AgenticError(
            "SUMMARY_NOT_TRACEABLE", "The warnings could not be traced to their records.", status=500
        ) from exc
    return summary.cards


def _spent(meter: Meter) -> float:
    """Dollars the Meter has spent so far; 0 for what it cannot price (an unpriced or fake model)."""
    cost = meter.cost_so_far
    return 0.0 if cost is None else float(cost)


def _frame_from_words(text: str, ctx: ToolContext, deps: PlannerDeps) -> Frame:
    """AGENT_PLANNER: the model picks each field from enumerated values; anything else is refused."""
    from engine.agent.untrusted import clean_text
    from engine.generative.contracts import GenerativePurpose
    from engine.generative.prompts import load_prompt, render

    meter = deps.meter
    if meter is None:  # pragma: no cover - start_plan refuses this before a plan exists
        raise AgenticError("AI_NOT_CONNECTED", "Typing a goal in words needs an AI service.", status=409)
    goal = clean_text(text, MAX_GOAL_CHARS)
    choices = _use_case_choices(ctx)
    amounts = amounts_in(goal)
    form = {
        "use_case_id": sorted(choices),
        "objective": list(OBJECTIVES),
        "channel": {"null": True, "by_use_case": {key: list(value) for key, value in choices.items()}},
        "budget_inr": {"null": True, "amounts_in_goal": list(amounts)},
    }
    rendered = render(
        load_prompt("agent_planner", ctx.config_root),
        {"goal": goal, "state": json.dumps(form, sort_keys=True)},
    )
    try:
        answer = meter.complete(rendered, GenerativePurpose.AGENT_PLANNER).text
    except Exception as exc:  # an LLM or budget error: the words cannot be read, the form still works
        raise _stop(
            PLANNER_GOAL_UNPARSED,
            "The AI service could not read the goal just now; use the form instead.",
            422,
        ) from exc
    try:
        parsed = json.loads(answer.strip().strip("`").removeprefix("json").strip())
    except ValueError:
        parsed = None
    unparsed = _stop(
        PLANNER_GOAL_UNPARSED,
        "The goal could not be matched to a use case, an objective and a channel the planner offers; use the "
        "form instead.",
        422,
    )
    if not isinstance(parsed, dict) or set(parsed) != {"use_case_id", "objective", "channel", "budget_inr"}:
        raise unparsed
    use_case = parsed["use_case_id"]
    objective = parsed["objective"]
    channel = parsed["channel"]
    budget = parsed["budget_inr"]
    if use_case not in choices or objective not in OBJECTIVES:
        raise unparsed
    if channel is not None and channel not in choices[use_case]:
        raise unparsed
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, int | float) or float(budget) not in amounts
    ):
        raise unparsed
    return Frame(
        use_case_id=use_case,
        objective=objective,
        channel=channel,
        budget_inr=None if budget is None else float(budget),
        source="words",
    )


# ---------------------------------------------------------------------------
# SIZE
# ---------------------------------------------------------------------------
def _call(
    state: PlannerState, ctx: ToolContext, deps: PlannerDeps, name: str, tool: str, given: Mapping[str, Any]
) -> tuple[PlannerState, Evidence]:
    result = deps.registry.call(tool, given, ctx)
    evidence = Evidence(evidence_id=result.evidence_id, tool=tool, label=result.label, output=result.output)
    return state.model_copy(update={"evidence": {**state.evidence, name: evidence}}), evidence


def _runs(ctx: ToolContext) -> list[RunRecord]:
    from engine.contracts import RunRecord
    from engine.runs import RUN_FILENAME
    from engine.storage import StorageError

    records: list[RunRecord] = []
    for key in sorted(ctx.storage.list_keys("runs/"), reverse=True):
        if not key.endswith(f"/{RUN_FILENAME}"):
            continue
        try:
            records.append(ctx.storage.read_model(key, RunRecord))
        except (StorageError, ValueError):
            continue
    return records


def _plan_run(
    ctx: ToolContext, use_case_id: str, approved: Callable[[str | None], bool] | None = None
) -> tuple[RunRecord | None, RunRecord | None]:
    """The newest finished scoring run of the use case with no campaign yet, and the run that trained its model.

    A list goes out once: a run that already has a campaign is not planned again (the monthly loop's rule,
    `engine.measurement.cycle.treat_list_step`). Plan M M184 (DEC-1614 (b)): `approved`, when given, says whether
    a run's model may be used (`_approval_check`); a run whose model may not is skipped.
    """
    from engine.config import RunMode
    from engine.contracts import RunState

    records = _runs(ctx)
    for record in records:
        if record.mode is not RunMode.SCORE or record.state is not RunState.DONE:
            continue
        if record.use_case_id != use_case_id or record.finished_at is None:
            continue
        if ctx.campaigns is not None and ctx.campaigns.list(run_id=record.run_id, limit=1):
            continue
        if approved is not None and not approved(record.model_version_id):
            continue
        trained = next(
            (
                other
                for other in records
                if other.mode is RunMode.TRAIN
                and other.model_version_id is not None
                and other.model_version_id == record.model_version_id
            ),
            None,
        )
        return record, trained
    return None, None


def _approval_check(config: UseCaseConfig, deps: PlannerDeps) -> Callable[[str | None], bool] | None:
    """Plan M M184 (DEC-1614 (b)): with `governance.approval_required` on, whether a run's model was approved
    (`engine.model_approval`); None - nothing skipped - when approval is not required or the caller passed no
    model registry (the `/agentic` route always passes one, and EXECUTE checks again with it)."""
    from engine.model_approval import unapproved_model_message

    registry = deps.model_registry
    if registry is None or not config.governance.approval_required:
        return None
    return lambda model_version_id: unapproved_model_message(registry, model_version_id) is None


def _size(state: PlannerState, ctx: ToolContext, deps: PlannerDeps) -> PlannerState:
    from engine.agentic.policy import _holdout_facts
    from engine.config import ConfigError, load_use_case
    from engine.measurement.cycle import default_outcome_window, target_of
    from engine.uplift.measure import measure_offered

    frame = state.frame
    assert frame is not None
    try:
        config = load_use_case(frame.use_case_id, ctx.config_root)
    except ConfigError as exc:
        raise _stop(PLANNER_NO_CANDIDATE, f"There is no use case {frame.use_case_id!r}.", 404) from exc
    if not measure_offered(config):
        raise _stop(
            PLANNER_NO_CANDIDATE,
            f"{config.name} holds nobody back or contacts nobody, so a campaign of it cannot be measured.",
        )
    window = default_outcome_window(config)
    if window is None:
        raise _stop(
            PLANNER_NO_CANDIDATE,
            f"{config.name} sets no outcome window, so the day a result can be read cannot be planned.",
        )
    approved = _approval_check(config, deps)
    run, trained = _plan_run(ctx, frame.use_case_id, approved)
    if run is None or run.finished_at is None:
        if approved is not None and _plan_run(ctx, frame.use_case_id)[0] is not None:
            raise _stop(
                PLANNER_NO_CANDIDATE,
                f"Every finished scoring run of {config.name} without a campaign was scored with a model that "
                "was never approved (waiting, turned down, or a candidate), and approval is required. Approve the model on the Approvals page, or "
                "score this cycle's file with the model in use; the planner then plans its list.",
            )
        raise _stop(
            PLANNER_NO_CANDIDATE,
            f"Every finished scoring run of {config.name} already has its campaign. Score this cycle's file "
            "first; the planner then plans its list.",
        )
    state, contact = _call(state, ctx, deps, "check_contactable", "check_contactable", {"run_id": run.run_id})
    eligible = int(contact.output.get("eligible_rows") or 0)
    if not contact.output.get("recorded") or eligible <= 0:
        raise _stop(
            PLANNER_NO_CANDIDATE,
            "The scoring run did not record who may be contacted on each channel (its use case sets no "
            "actions.suppression.channels), or nobody on it may be contacted.",
        )
    held_back_value = contact.output.get("held_back_rows")
    if held_back_value is None:
        raise _stop(
            PLANNER_NO_CANDIDATE,
            "The scoring run recorded no count of its held-back group, so the test cannot be sized on the "
            "people it will compare. Score the file again.",
        )
    held_back = int(held_back_value)
    # The test compares everyone the campaign holds back or contacts, as the product's own preview sizes it
    # (`realised.population_rows`): plan_test splits this population by the holdout share itself.
    population = eligible + held_back
    holdout = _holdout_facts(config, ctx)
    # Measured campaigns of the use case, newest first: the outcome kind, the base rate and the narrative.
    state, measured = _measured_campaigns(state, ctx, deps, frame)
    reports = {campaign_id: state.evidence[name].output.get("report") or {} for name, campaign_id in measured}
    kind: Literal["binary", "continuous"] = "binary"
    if measured:
        newest = reports[measured[0][1]]
        kind = "continuous" if newest.get("outcome_kind") == "continuous" else "binary"
    base_rate: float | None = None
    base_source: str | None = None
    baseline: str | None = None
    detectable_pp: float | None = None
    if kind == "binary":
        # The card's own campaign's held-back rate when it has one, else the newest measured campaign's.
        order = [campaign_id for _, campaign_id in measured]
        if frame.card_campaign_id in order:
            order.remove(str(frame.card_campaign_id))
            order.insert(0, str(frame.card_campaign_id))
        for campaign_id in order:
            report = reports[campaign_id]
            if report.get("outcome_kind", "binary") == "binary" and report.get("control_rate") is not None:
                base_rate = float(report["control_rate"])
                base_source = f"the held-back group of campaign {campaign_id}"
                baseline = campaign_id
                break
        share = min(0.5, holdout.in_force)
        state, power = _call(
            state,
            ctx,
            deps,
            "plan_test",
            "plan_test",
            {"eligible": population, "base_rate": base_rate, "holdout_shares": [share], "direction": "up"},
        )
        point = (power.output.get("points") or [{}])[0]
        detectable = point.get("mde_pp")
        detectable_pp = None if detectable is None else _round_up(float(detectable))
        mde = frame.mde_pp if frame.mde_pp is not None else detectable_pp
    else:
        if frame.mde_pp is not None:
            raise _stop(
                PLANNER_NO_CANDIDATE,
                f"{config.name}'s campaigns are measured as an amount, so the smallest effect is given in its "
                "own unit, not in percentage points. Register the plan on the campaign page "
                "(POST /campaigns/{id}/plan) with mde_value and outcome_sd.",
                422,
            )
        mde = None
    # The window counts from the later of the run's finish and now: a list drafted late still has its whole
    # window after the draft (and the approval re-checks it against its own time).
    start = max(run.finished_at, ctx.clock())
    size = Size(
        run_id=run.run_id,
        training_run_id=None if trained is None else trained.run_id,
        eligible_rows=eligible,
        held_back_rows=held_back,
        population_rows=population,
        outcome_kind=kind,
        holdout_fraction=holdout.in_force,
        holdout_scope=holdout.scope,
        outcome_column=target_of(config),
        outcome_window_days=window,
        analysis_date=(start + timedelta(days=window)).date(),
        base_rate=base_rate,
        base_rate_source=base_source,
        mde_pp=mde if base_rate is not None else None,
        detectable_mde_pp=detectable_pp,
        baseline_campaign_id=baseline,
        measured_campaigns=tuple(campaign_id for _, campaign_id in measured),
    )
    return state.model_copy(update={"size": size})


def _round_up(value: float) -> float:
    """Up to the next tenth of a point, so the plan never asks for less than the test can see."""
    import math

    return math.ceil(value * 10.0 - 1e-9) / 10.0


def _measured_campaigns(
    state: PlannerState, ctx: ToolContext, deps: PlannerDeps, frame: Frame
) -> tuple[PlannerState, list[tuple[str, str]]]:
    """`measure` on the use case's campaigns, newest first (by when each was opened), keeping the final measured
    ones. The order is time's alone: the narrative's "last measured campaign" and its series rest on it; the
    card's own campaign is preferred only for the base rate (in SIZE)."""
    if ctx.campaigns is None:
        return state, []
    campaigns = ctx.campaigns.list(use_case_id=frame.use_case_id, limit=None)
    ordered = [
        c.campaign_id
        for c in sorted(campaigns, key=lambda item: (item.created_at, item.campaign_id), reverse=True)
    ]
    found: list[tuple[str, str]] = []
    for campaign_id in ordered:
        if len(found) >= MAX_CYCLES:
            break
        campaign = ctx.campaigns.get(campaign_id)
        if campaign is None or campaign.status != "measured":
            continue
        name = f"measure:{campaign_id}"
        state, result = _call(state, ctx, deps, name, "measure", {"campaign_id": campaign_id})
        report = result.output.get("report") or {}
        if result.output.get("measured") and not report.get("early_look"):
            found.append((name, campaign_id))
        else:
            evidence = dict(state.evidence)
            evidence.pop(name, None)
            state = state.model_copy(update={"evidence": evidence})
    return state, found


# ---------------------------------------------------------------------------
# CANDIDATES and SELECT
# ---------------------------------------------------------------------------
def _ranking(ctx: ToolContext, run: str) -> tuple[Literal["uplift", "propensity_model", "risk"], int | None]:
    from engine.agentic.artefacts import read_stored_model
    from engine.contracts import RunRecord
    from engine.runs import RUN_FILENAME
    from engine.storage import run_key
    from engine.uplift.contracts import RANKING_CHOICE_FILENAME, RankingChoice

    choice = read_stored_model(ctx.storage, run_key(run, RANKING_CHOICE_FILENAME), RankingChoice)
    if choice is not None:
        return choice.ranking, choice.contacts
    record = ctx.storage.read_model(run_key(run, RUN_FILENAME), RunRecord)
    return ("uplift" if record.problem_type.value == "uplift" else "risk"), None


def _candidates(state: PlannerState, ctx: ToolContext, deps: PlannerDeps) -> PlannerState:
    from engine.config import load_use_case

    frame, size = state.frame, state.size
    assert frame is not None and size is not None
    config = load_use_case(frame.use_case_id, ctx.config_root)
    state, _ = _call(state, ctx, deps, "value_segment", "value_segment", {"run_id": size.run_id})
    rollout: Literal["safe", "not_safe", "cannot_tell"] | None = None
    if size.training_run_id is not None:
        state, policy = _call(
            state, ctx, deps, "evaluate_policy", "evaluate_policy", {"run_id": size.training_run_id}
        )
        comparison = policy.output.get("risk_comparison") or {}
        verdict = comparison.get("rollout") or {}
        rollout = verdict.get("outcome")
    ranking, treat_contacts = _ranking(ctx, size.run_id)
    contact = state.evidence["check_contactable"].output
    cost = config.uplift.policy.cost_per_contact
    out: list[Candidate] = []
    for source in contact.get("channels") or ():
        channel = str(source["channel"])
        contactable = max(0, size.eligible_rows - int(source["not_contactable_eligible_rows"]))
        planned = contactable if treat_contacts is None else min(contactable, treat_contacts)
        spend = None if cost is None else round(planned * float(cost), 2)
        excluded: str | None = None
        if frame.channel is not None and channel != frame.channel:
            excluded = "The goal names another channel."
        elif contactable <= 0:
            excluded = "Nobody on the list may be contacted on this channel."
        elif ranking == "uplift" and rollout == "not_safe":
            excluded = "M137's check says the uplift ranking is not safe to roll out; it is never chosen."
        elif frame.budget_inr is not None and spend is None:
            excluded = "The use case sets no cost per contact, so the budget given cannot be checked."
        elif frame.budget_inr is not None and spend is not None and spend > frame.budget_inr:
            excluded = "Contacting this list would cost more than the budget given."
        out.append(
            Candidate(
                channel=channel,
                contactable_rows=contactable,
                planned_contacts=planned,
                offer_spend_inr=spend,
                ranking=ranking,
                rollout=rollout,
                excluded=excluded,
            )
        )
    preference = {name: index for index, name in enumerate(config.actions.suppression.channels)}
    rank = {"safe": 0, None: 1, "cannot_tell": 1, "not_safe": 2}
    out.sort(key=lambda c: (rank[c.rollout], -c.contactable_rows, preference.get(c.channel, 99), c.channel))
    return state.model_copy(update={"candidates": tuple(out)})


def _payload(state: PlannerState, candidate: Candidate) -> CampaignPlanPayload:
    frame, size = state.frame, state.size
    assert frame is not None and size is not None
    plan = TestPlanInput(
        metric=f"{size.outcome_column} within {size.outcome_window_days} days",
        outcome_column=size.outcome_column,
        # An amount is planned as an amount (never a yes/no plan for revenue): its smallest effect and spread
        # are given in its own unit on the campaign page, so they are left open here.
        outcome_kind=size.outcome_kind,
        outcome_window_days=size.outcome_window_days,
        analysis_date=size.analysis_date,
        mde_pp=size.mde_pp,
        base_rate=size.base_rate,
        base_rate_source=size.base_rate_source,
    )
    return CampaignPlanPayload(
        use_case_id=frame.use_case_id,
        run_id=size.run_id,
        campaign_id=None,
        channel=candidate.channel,
        holdout=HoldoutTerms(fraction=size.holdout_fraction, scope=size.holdout_scope, treat_members=False),
        test_plan_hash=None,
        contacts_per_customer=1,
        planned_contacts=candidate.planned_contacts,
        offer_spend_inr=candidate.offer_spend_inr,
        test_plan=plan,
        objective=frame.objective,
        ranking=candidate.ranking,
        rollout=candidate.rollout,
    )


def _decisions(state: PlannerState, ctx: ToolContext, candidate: Candidate) -> tuple[Decision, ...]:
    from engine.agentic.config import load_policy
    from engine.agentic.policy import RULES

    subject = ProposalSubject(
        kind="campaign_plan",
        payload=_payload(state, candidate).model_dump(mode="json"),
        proposed_by=ctx.principal.user_id,
    )
    rules = ctx.policy_rules if ctx.policy_rules is not None else RULES
    return evaluate(subject, build_context(subject, ctx, load_policy(ctx.config_root)), rules=rules)


def _select(state: PlannerState, ctx: ToolContext, _deps: PlannerDeps) -> PlannerState:
    """Rules only: in the CANDIDATES order, the first candidate every policy rule allows; when none is
    allowed, the first one, so the person sees the refusal and its codes on the ledger."""
    open_ = [candidate for candidate in state.candidates if candidate.excluded is None]
    if not open_:
        reasons = sorted({c.excluded or "" for c in state.candidates}) or ["The run has no channel."]
        raise _stop(PLANNER_NO_CANDIDATE, "No candidate can be planned: " + " ".join(reasons))
    tried: list[Tried] = []
    chosen = open_[0]
    for candidate in open_:
        decisions = _decisions(state, ctx, candidate)
        codes = tuple(sorted({d.code for d in decisions if not d.allowed and d.code}))
        tried.append(Tried(channel=candidate.channel, codes=codes))
        if allowed(decisions):
            chosen = candidate
            break
    return state.model_copy(update={"selected": chosen, "tried": tuple(tried)})


def _check(state: PlannerState, ctx: ToolContext, _deps: PlannerDeps) -> PlannerState:
    assert state.selected is not None
    return state.model_copy(update={"checks": _decisions(state, ctx, state.selected)})


# ---------------------------------------------------------------------------
# DRAFT: the proposal and its narrative
# ---------------------------------------------------------------------------
def _interval(value: Mapping[str, Any] | None) -> Interval | None:
    if not value or value.get("ci_low") is None or value.get("ci_high") is None:
        return None
    return Interval(
        estimate=float(value["value"]),
        low=float(value["ci_low"]),
        high=float(value["ci_high"]),
        level=float(value.get("confidence_level") or 0.95),
    )


def evidence_index(state: PlannerState, proposals: Mapping[str, str]) -> EvidenceIndex:
    """What the narrative may state, from the plan's tool results only (`engine.agentic.claims`)."""
    arms: dict[str, str] = {}
    comparisons: list[ComparisonFact] = []
    labels: dict[str, EvidenceLabel] = {}
    subjects: dict[str, str] = {}
    series: dict[str, SeriesFact] = {}
    size = state.size
    measured = [] if size is None else [f"measure:{c}" for c in size.measured_campaigns]
    if measured:
        newest = state.evidence[measured[0]]
        report = newest.output.get("report") or {}
        lift = _interval(report.get("absolute_lift"))
        if lift is not None:
            treated, control = f"{newest.evidence_id}:arms.treated", f"{newest.evidence_id}:arms.control"
            arms[treated], arms[control] = "the contacted group", "the held-back group"
            column = str(report.get("outcome_column") or "the outcome")
            comparisons.append(
                ComparisonFact(
                    left=treated,
                    right=control,
                    metric=column,
                    metric_label=f"the share with {column}",
                    unit="share",
                    difference=lift,
                )
            )
        labels[f"{newest.evidence_id}:label"] = newest.label
        subjects[f"{newest.evidence_id}:label"] = "the last measured campaign's result"
        card = state.frame.card_campaign_id if state.frame is not None else None
        if card is not None and f"measure:{card}" in measured[1:]:  # an older campaign the warning is about
            about = state.evidence[f"measure:{card}"]
            labels[f"{about.evidence_id}:label"] = about.label
            subjects[f"{about.evidence_id}:label"] = "the result of the campaign the warning is about"
        # Oldest first, in the order the campaigns were opened (`measured` is newest first).
        points = [
            _interval((state.evidence[name].output.get("report") or {}).get("absolute_lift"))
            for name in reversed(measured)
        ]
        if len(points) >= 3 and all(point is not None for point in points):
            series[f"{newest.evidence_id}:series.cycles"] = SeriesFact(
                name="the measured effect of this use case's campaigns",
                metric_label="the difference in the outcome share",
                unit="share",
                points=tuple(point for point in points if point is not None),
            )
    policy = state.evidence.get("evaluate_policy")
    comparison = None if policy is None else policy.output.get("risk_comparison")
    if policy is not None and comparison:
        difference = _interval(comparison.get("difference"))
        if difference is not None:
            uplift, risk = f"{policy.evidence_id}:arms.uplift", f"{policy.evidence_id}:arms.risk"
            arms[uplift], arms[risk] = "the list ranked by campaign effect", "the list ranked by risk"
            comparisons.append(
                ComparisonFact(
                    left=uplift,
                    right=risk,
                    metric="extra_conversions",
                    metric_label="extra conversions at the same budget",
                    unit="count",
                    difference=difference,
                )
            )
        labels[f"{policy.evidence_id}:label"] = policy.label
        subjects[f"{policy.evidence_id}:label"] = "the equal-budget comparison"
    return EvidenceIndex(
        arms=arms,
        comparisons=tuple(comparisons),
        labels=labels,
        label_subjects=subjects,
        series=series,
        proposals=dict(proposals),
    )


_READS: Final[dict[str, str]] = {
    "gt": "left is above right",
    "lt": "left is below right",
    "eq": "not distinguishable",
}


def _direction_of(interval: Interval) -> str:
    return "gt" if interval.low > 0 else ("lt" if interval.high < 0 else "eq")


@dataclass
class _Spoken:
    """What AGENT_NARRATIVE gave: the model's answer (None: no model, or it failed), and its cost."""

    raw: str | None = None
    error: str | None = None
    calls: int = 0
    cost_usd: float = 0.0


def _model_narrative(
    state: PlannerState, index: EvidenceIndex, ctx: ToolContext, meter: Meter | None
) -> _Spoken:
    """AGENT_NARRATIVE: the model's structured narrative. No model: nothing; an AI service or budget error is
    recorded as such (`error`), not as a malformed claim, and the rules-only summary is written."""
    from engine.generative.contracts import GenerativePurpose
    from engine.generative.prompts import load_prompt, render

    if meter is None:
        return _Spoken()
    evidence = {
        "arms": index.arms,
        "comparisons": [
            {
                "left": f.left,
                "right": f.right,
                "metric": f.metric,
                "about": f.metric_label,
                # The interval's reading in words (never its numbers), so an honest model can say it.
                "reads": _READS[_direction_of(f.difference)],
            }
            for f in index.comparisons
        ],
        "labels": {
            ref: {"label": label, "of": index.label_subjects.get(ref)} for ref, label in index.labels.items()
        },
        "series": {ref: {"name": s.name, "points": len(s.points)} for ref, s in index.series.items()},
        "proposals": index.proposals,
    }
    selected = state.selected
    draft = {
        "objective": None if state.frame is None else state.frame.objective,
        "channel": None if selected is None else selected.channel,
        "trigger": None if state.frame is None else (state.frame.card_code or state.frame.source),
    }
    rendered = render(
        load_prompt("agent_narrative", ctx.config_root),
        {"evidence": json.dumps(evidence, sort_keys=True), "state": json.dumps(draft, sort_keys=True)},
    )
    calls, spent = meter.calls, _spent(meter)
    spoken = _Spoken()
    try:
        spoken.raw = meter.complete(rendered, GenerativePurpose.AGENT_NARRATIVE).text
    except Exception as exc:  # an LLM or budget error: the rules-only summary is written instead
        spoken.error = str(getattr(exc, "code", None) or type(exc).__name__)[:64]
    spoken.calls = meter.calls - calls
    spoken.cost_usd = max(0.0, _spent(meter) - spent)
    return spoken


def _stored_draft(ctx: ToolContext, proposal_id: str) -> Proposal | None:
    """The proposal a DRAFT interrupted after drafting it left in the store, or None."""
    from engine.agentic.proposals import ProposalStore

    try:
        return ProposalStore(ctx.storage).get(proposal_id)
    except AgenticError:
        return None


def _draft(state: PlannerState, ctx: ToolContext, deps: PlannerDeps) -> PlannerState:
    from engine.agentic.proposals import new_proposal_id, submit_proposal

    selected = state.selected
    assert selected is not None
    reserved = state.reserved_proposal_id
    if reserved is not None:
        stored = _stored_draft(ctx, reserved)
        if stored is not None:  # a crash after the proposal was drafted: the same proposal, not a second
            narrative = None if stored.narrative is None else Narrative.model_validate(stored.narrative)
            return state.model_copy(
                update={
                    "proposal_id": stored.proposal_id,
                    "proposal_status": stored.status,
                    "payload_digest": stored.payload_digest,
                    "narrative": narrative,
                }
            )
        if ctx.ledger is not None and ctx.ledger.proposal_entries(reserved):
            reserved = None  # recorded on the ledger but never stored: a fresh id, so no id has two drafts
    if reserved is None:
        # Written down before drafting, so a resumed DRAFT finds the proposal this one makes.
        reserved = new_proposal_id()
        state = state.model_copy(update={"reserved_proposal_id": reserved})
        ctx.storage.write_model(state_key(state.plan_id), state)
    payload = _payload(state, selected).model_dump(mode="json")
    holder: dict[str, Any] = {}
    meter = deps.narrative_meter(None if state.frame is None else state.frame.use_case_id)

    def narrate(proposal_id: str, status: str) -> dict[str, Any]:
        action = f"open a campaign for this list on {selected.channel} and register its test plan"
        index = evidence_index(state, {proposal_id: action} if status == "pending" else {})
        spoken = _model_narrative(state, index, ctx, meter)
        narrative = verify_narrative(
            spoken.raw,
            index,
            rules=rules_narrative(index),
            guardrails=guardrails_for(ctx.config_root),
        )
        holder["spoken"], holder["narrative"] = spoken, narrative
        return narrative.model_dump(mode="json")

    proposal = submit_proposal(
        "campaign_plan",
        payload,
        tool_ctx=ctx,
        evidence_ids=state.evidence_ids,
        narrate=narrate,
        ledger_refs={"plan_id": state.plan_id},
        proposal_id=reserved,
    )
    spoken = holder.get("spoken") or _Spoken()
    return state.model_copy(
        update={
            "proposal_id": proposal.proposal_id,
            "proposal_status": proposal.status,
            "payload_digest": proposal.payload_digest,
            "narrative": holder.get("narrative"),
            "narrative_error": spoken.error,
            "llm_calls": state.llm_calls + spoken.calls,
            "llm_cost_usd": round(state.llm_cost_usd + spoken.cost_usd, 6),
        }
    )


# ---------------------------------------------------------------------------
# EXECUTE (only from an approval)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ListLimits:
    """Plan M M184 (DEC-1614 (a)): what the approved plan was checked on - its one channel and its number of
    contacts - which the run's treat list must keep to (`engine.decide.campaign_scope`)."""

    channel: str
    max_contacts: int | None
    proposal_id: str


@dataclass(frozen=True)
class BuiltList:
    """What the run's treat list holds once built within the limits: rows, contacts, and contacts per channel."""

    total_rows: int
    treat_rows: int
    channel_rows: Mapping[str, int]


@dataclass(frozen=True)
class ExecuteActions:
    """The product's own functions an approved plan is carried out with (passed in by the caller)."""

    build_treat_list: Callable[[str, ListLimits], BuiltList]
    """The run's treat list within the approved limits: the limits written (`campaign_scope.json`), then the
    list built under them (`build_treat_list`). Plan M M184: EXECUTE checks what it returns against the limits
    and refuses with `CAMPAIGN_LIST_NOT_AS_APPROVED` when the list contacts anyone else."""
    open_campaign: Callable[[str, int], str]
    """The run's campaign with this outcome window (`open_campaign`, as `POST /campaigns`), its treatment
    starting now (at the approval); returns its id."""
    register_plan: Callable[[str, TestPlanInput], str]
    """The campaign's test plan (`POST /campaigns/{id}/plan`'s function); returns the plan hash."""
    measure_schedule: Callable[[str], str | None] = lambda _use_case: None
    """An enabled `measure` schedule of the use case, if there is one (none is created: it needs a saved
    connection and a table a person chooses)."""
    check_model: Callable[[str], None] | None = None
    """Plan M M184 (DEC-1614 (b)): raises `MODEL_NOT_APPROVED` when the run's model may not be used by a step
    nobody chooses the model of and its run requires approval (`engine.model_approval`); None checks nothing (a caller with no model
    registry - the `/agentic` route always passes one)."""


def find_plan_for_proposal(ctx: ToolContext, proposal_id: str) -> PlannerState | None:
    """The planner state that drafted `proposal_id`, or None (a campaign plan drafted some other way)."""
    for key in ctx.storage.list_keys(PLANS_PREFIX):
        if not key.endswith(f"/{STATE_FILENAME}"):
            continue
        try:
            state = ctx.storage.read_model(key, PlannerState)
        except (OSError, ValueError):
            continue
        if state.proposal_id == proposal_id:
            return state
    return None


def execute_plan(proposal: Proposal, ctx: ToolContext, actions: ExecuteActions) -> str:
    """Carry out an approved campaign plan; returns the frozen plan hash (the approval's `result_ref`)."""
    from engine.agentic.config import require_verified_identity
    from engine.agentic.ledger import CAMPAIGN_PLAN_EXECUTED, TEST_PLAN_FROZEN, holdout_commitment

    # Plan M M184 (DEC-1614 (e)): with agentic.require_sign_in on, only a signed-in Approver's approval is acted on.
    require_verified_identity(ctx.principal, ctx.config_root)
    found = find_plan_for_proposal(ctx, proposal.proposal_id)
    if found is None:
        raise AgenticError(
            "PROPOSAL_NOT_EXECUTABLE",
            "This campaign plan was not drafted by the planner, so nothing in the product carries it out; it "
            "stays pending.",
            status=409,
        )
    payload = CampaignPlanPayload.model_validate(proposal.payload)
    if payload.test_plan is None:
        raise AgenticError(
            "PROPOSAL_NOT_EXECUTABLE", "This campaign plan carries no test plan to register.", status=409
        )
    with _plan_lock(found.plan_id):
        state = load_plan(ctx, found.plan_id)
        done = state.execution or Execution()
        if ctx.campaigns is not None and done.campaign_id is None:
            others = ctx.campaigns.list(run_id=payload.run_id, limit=1)
            if others:
                raise AgenticError(
                    "PROPOSAL_STALE",
                    "The run's list already went out as another campaign since this was proposed; draft a "
                    "plan for the next run.",
                    status=409,
                )
        if done.campaign_id is None:
            window = int(payload.test_plan.outcome_window_days or 0)
            earliest = (ctx.clock() + timedelta(days=window)).date()
            if payload.test_plan.analysis_date < earliest:
                raise AgenticError(
                    "PROPOSAL_STALE",
                    f"The campaign would start now, so its {window}-day outcome window closes on "
                    f"{earliest.isoformat()}, after the analysis date this plan fixed "
                    f"({payload.test_plan.analysis_date.isoformat()}). Draft the plan again so the date fits.",
                    status=409,
                )
        if done.campaign_id is None and actions.check_model is not None:
            actions.check_model(payload.run_id)
        state = _save(ctx, state, "EXECUTE")
        if done.treat_list_rows is None:
            # Plan M M184 (DEC-1614 (a)): what is executed is what the policy checked - the approved channel and
            # at most the planned number of contacts - or nothing is.
            limits = ListLimits(
                channel=payload.channel,
                max_contacts=payload.planned_contacts,
                proposal_id=proposal.proposal_id,
            )
            built = actions.build_treat_list(payload.run_id, limits)
            _require_within(built, limits)
            done = done.model_copy(update={"treat_list_rows": built.total_rows, "contacts": built.treat_rows})
            state = _save(ctx, state.model_copy(update={"execution": done}), "EXECUTE")
        if done.campaign_id is None:
            window = int(payload.test_plan.outcome_window_days or 0)
            done = done.model_copy(update={"campaign_id": actions.open_campaign(payload.run_id, window)})
            state = _save(ctx, state.model_copy(update={"execution": done}), "EXECUTE")
        campaign_id = done.campaign_id
        assert campaign_id is not None
        if done.plan_hash is None:
            # `register_plan` answers the plan already frozen when the same body is registered again, so a
            # retry after a crash between it and the state write freezes nothing twice.
            plan_hash = actions.register_plan(campaign_id, payload.test_plan)
            if ctx.ledger is not None:
                ctx.ledger.append(
                    TEST_PLAN_FROZEN,
                    ctx.principal.user_id,
                    {
                        "proposal_id": proposal.proposal_id,
                        "campaign_id": campaign_id,
                        "plan_hash": plan_hash,
                        "plan_id": state.plan_id,
                        **holdout_commitment(ctx, campaign_id),
                    },
                )
            done = done.model_copy(update={"plan_hash": plan_hash})
            state = _save(ctx, state.model_copy(update={"execution": done}), "EXECUTE")
        schedule = actions.measure_schedule(payload.use_case_id)
        note = (
            f"The measure schedule {schedule} measures the campaign once its outcome window closes."
            if schedule is not None
            else "No measure schedule is set for this use case: measure the campaign on its analysis date "
            "(POST /campaigns/{id}/measure), or set one under Schedules with the table its outcomes arrive in."
        )
        done = done.model_copy(
            update={
                "measure_schedule_id": schedule,
                "measure_on": payload.test_plan.analysis_date,
                "note": note,
            }
        )
        if ctx.ledger is not None:
            ctx.ledger.append(
                CAMPAIGN_PLAN_EXECUTED,
                ctx.principal.user_id,
                {
                    "proposal_id": proposal.proposal_id,
                    "plan_id": state.plan_id,
                    "run_id": payload.run_id,
                    "campaign_id": done.campaign_id,
                    "plan_hash": done.plan_hash,
                    "measure_schedule_id": schedule,
                },
            )
        state = state.model_copy(update={"execution": done, "proposal_status": "approved"})
        _save(ctx, state, "DONE")
        assert done.plan_hash is not None
        return done.plan_hash


def _require_within(built: BuiltList, limits: ListLimits) -> None:
    """Plan M M184: refuse a treat list that contacts anyone on another channel, anyone with no channel, or more
    customers than were approved. Nothing has been opened yet when this refuses; the proposal stays pending.
    """
    elsewhere = sorted(
        name for name, rows in built.channel_rows.items() if name != limits.channel and rows > 0
    )
    on_channel = int(built.channel_rows.get(limits.channel, 0))
    over = limits.max_contacts is not None and built.treat_rows > limits.max_contacts
    if not elsewhere and on_channel == built.treat_rows and not over:
        return
    found = f"{built.treat_rows:,} contacts, {on_channel:,} of them on {limits.channel}"
    if elsewhere:
        found += " and others on " + ", ".join(elsewhere)
    allowed = (
        f"on {limits.channel} only"
        if limits.max_contacts is None
        else f"at most {limits.max_contacts:,}, on {limits.channel} only"
    )
    raise AgenticError(
        CAMPAIGN_LIST_NOT_AS_APPROVED,
        f"The contact list built for this plan holds {found}; the approval covers {allowed}. Nothing was opened "
        "or sent, and the proposal stays pending. Draft the plan again.",
        status=409,
    )


def outcome_of(state: PlannerState) -> tuple[str, tuple[str, ...], str | None]:
    """`(status, codes, payload_digest)` of a plan, as the eval harness and the route report it."""
    if state.stopped is not None:
        return "error", (state.stopped.code,), None
    codes = tuple(sorted({d.code for d in state.checks if not d.allowed and d.code}))
    return (
        str(state.proposal_status),
        codes if state.proposal_status == "refused" else (),
        state.payload_digest,
    )
