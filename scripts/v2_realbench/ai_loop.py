"""The AI half of the Scientist in the bench: diagnose -> propose -> evaluate -> judge, answers replayed.

`--policy scientist --ai-answers FILE` adds this to the Scientist's bench. Nothing here calls a model: the
three roles (`autonomy.agents.scientist_roles`) ask through a `ReplayLLM`, which answers each prompt from
a JSON file `{prompt id: answer}` that real Sonnet answers were written into (see `autonomy.scientist.run`
and `autonomy.agents.llm_exchange`). A prompt with no answer is saved to the record file and the dataset
stops there (`AnswerPending`): no answer is ever guessed.

The order of one dataset, and what each step may read:

1. **Profile** (numbers only, no customer rows) of the TRAIN half. TEST is never read.
2. **Data Doctor** and **Proposer** read that profile. The Proposer's up-to-three candidates come from the
   fixed menu; `candidate_from_spec` turns them into typed candidates (nothing is executed).
3. **Evaluate** on TRAIN only: the same inner customer split the fixed-list Scientist uses
   (`autonomy.scientist.run_scientist`), every candidate fitted on one half and scored on the other.
4. **Judge** reads the profile, the typed candidates and that held-out evidence (never the proposer's
   rationale or the diagnosis). It may agree with the evidence rule or veto it; it cannot pick another
   candidate. The final pick is `JudgeDecision.final_pick`.
5. The caller refits the final pick on all of TRAIN and scores it on TEST (`scientist.evaluate_scientist`).

Nothing here knows a dataset: the profile reads the bench's events, the arm ids come from the adapter's
`arm_content`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

from autonomy.agents.llm_exchange import ReplayLLM, prompt_id, role_of
from autonomy.agents.scientist_roles import (
    SAFE_DEFAULT_ID,
    CandidateEvidence,
    CandidateKind,
    DataDoctorDiagnosis,
    EvidenceTable,
    JudgeDecision,
    Limits,
    Proposal,
    Staged,
    candidate_ids,
    run_data_doctor,
    run_judge,
    run_proposer,
)
from autonomy.scientist import Campaign, Candidate, Selection, candidate_from_spec
from autonomy.scientist.menu import AgentRunner
from autonomy.scientist.profile import DataProfile, build_profile
from engine.llm import LLMCompletion
from scripts.v2_realbench.common import BenchData

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

PROFILE_WINDOW_DAYS = 14.0
"""The outcome window of the bench's campaigns (the adapters book a purchase within 14 days)."""

_MENU_KIND = {
    CandidateKind.SEND_TO_ALL: "blanket",
    CandidateKind.SEGMENT_RULE: "segment",
    CandidateKind.UPLIFT_MODEL: "uplift",
    CandidateKind.BANDIT_AGENT: "bandit",
}


class CountingReplay(ReplayLLM):
    """`ReplayLLM` that remembers every answered call as (role, prompt id), in order.

    A role asked more than once in a stage means its first answer did not parse and `ask_json` sent the
    repair prompt."""

    def __init__(self, answers: Mapping[str, Any], **kw: Any) -> None:
        super().__init__(answers, **kw)
        self.calls: list[tuple[str, str]] = []

    def complete(self, prompt: str, **kw: Any) -> LLMCompletion:
        out = super().complete(prompt, **kw)
        system = str(kw.get("system", ""))
        role = role_of(system)
        self.calls.append((role, prompt_id(role, system, prompt)))
        return out


def train_profile(data: BenchData, train: IntArray) -> DataProfile:
    """The numbers-only profile of the TRAIN customers' events (the bench's own adapter inputs)."""
    ids = [str(data.customer_ids[i]) for i in train]
    pre = data.agent.pre_events(ids)
    camp = data.agent.campaign_events(ids)
    events = [e for i in ids for e in (*pre.get(i, []), *camp.get(i, []))]
    features = {c: data.features.iloc[train][c].tolist() for c in data.features.columns}
    return build_profile(events, window_days=PROFILE_WINDOW_DAYS, features=features, as_of=data.agent.now)


def proposal_candidates(
    proposal: Proposal, data: BenchData, runner: AgentRunner | None
) -> tuple[Candidate, ...]:
    """The proposal's typed candidates, built only through `candidate_from_spec` (menu kinds, ranges)."""
    arm_of = {cid: a for a, cid in data.agent.arm_content.items()}
    out: list[Candidate] = []
    for c in proposal.candidates:
        kind = _MENU_KIND[c.kind]
        s: dict[str, object] = {
            "exploration_floor": c.settings.exploration_floor,
            "weekly_contact_budget": c.settings.weekly_contact_budget,
        }
        if kind in ("segment", "uplift"):
            s["lookback_days"] = c.settings.lookback_days
        if kind == "blanket":
            assert c.arm is not None  # check_proposal refuses a blanket candidate without an arm
            s["arm"] = arm_of[c.arm]
        if kind == "segment":
            s["columns"] = list(c.columns)
        out.append(
            candidate_from_spec(
                {"kind": kind, "settings": s},
                arm_names=data.arm_names,
                columns=list(data.features.columns),
                runner=runner,
            )
        )
    return tuple(out)


def evidence_table(selection: Selection, ids: tuple[str, ...]) -> EvidenceTable:
    """The held-out evidence of `selection` as the Judge's typed input (gains over the safe default)."""
    ev = selection.evaluations
    rows: list[CandidateEvidence] = []
    for i, cid in enumerate(ids):
        v = selection.verdicts[i] if i < len(selection.verdicts) else None
        if ev is None or v is None or v.status != "ok" or v.vs_default is None or v.corrected is None:
            rows.append(
                CandidateEvidence(
                    candidate_id=cid,
                    n_eval=0,
                    gain_estimate=0.0,
                    gain_low=-1e9,
                    gain_high=0.0,
                    split=ev.split.method if ev is not None else "customer",
                )
            )
            continue
        e = ev.candidates[i]
        rows.append(
            CandidateEvidence(
                candidate_id=cid,
                n_eval=ev.n_held,
                gain_estimate=v.vs_default.point,
                gain_low=v.corrected.low,
                gain_high=v.corrected.high,
                adjusted_p=v.p_holm,
                effective_sample_share=min(1.0, e.effective_n / ev.n_held),
                split=ev.split.method,
            )
        )
    return EvidenceTable(
        unit="profit per customer (DR, held-out customers of the TRAIN half)",
        correction="holm",
        comparisons=len(rows),
        current_value=(ev.default.value.point if ev is not None and ev.default.value is not None else None),
        candidates=tuple(rows),
    )


def cached_runner(runner: AgentRunner) -> AgentRunner:
    """The agent runner with its answers remembered by (fit customers, held customers).

    The agent is deterministic given its customers and seeds, so the fixed list and the proposed list
    (both containing the learning agent on the same inner split) share one run, and nothing changes."""
    memo: dict[tuple[int, int, int, int], FloatArray] = {}

    def run(fit: Campaign, held: Campaign) -> FloatArray:
        key = (
            len(fit.customer_ids),
            hash(fit.customer_ids),
            len(held.customer_ids),
            hash(held.customer_ids),
        )
        if key not in memo:
            memo[key] = runner(fit, held)
        return memo[key]

    return run


@dataclass(frozen=True)
class AiSetup:
    """What the bench needs to run the loop: the answered-prompts client and the mandate's limits."""

    llm: CountingReplay
    limits: Limits = field(default_factory=Limits)


@dataclass(frozen=True)
class AiPlan:
    """Steps 1-2: what the Doctor said and what the Proposer proposed (typed, validated)."""

    profile: DataProfile
    diagnosis: Staged[DataDoctorDiagnosis]
    proposal: Staged[Proposal]
    ids: tuple[str, ...]


def plan(data: BenchData, train: IntArray, setup: AiSetup) -> AiPlan:
    """Profile of TRAIN, then the Data Doctor, then the Proposer. Raises `AnswerPending` at the first
    prompt that has no answer (cheap: only the profile has been built)."""
    profile = train_profile(data, train)
    diagnosis = run_data_doctor(setup.llm, profile)
    proposal = run_proposer(setup.llm, profile, diagnosis.value, setup.limits)
    return AiPlan(profile, diagnosis, proposal, candidate_ids(proposal.value))


def judge(setup: AiSetup, ai: AiPlan, selection: Selection) -> tuple[EvidenceTable, JudgeDecision]:
    """Step 4: the Judge on the held-out evidence of the proposed candidates."""
    table = evidence_table(selection, ai.ids)
    return table, run_judge(setup.llm, ai.profile, ai.proposal.value, table)


def pick_index(decision: JudgeDecision, ids: tuple[str, ...]) -> int | None:
    """Index of the candidate the Judge's final pick names, or None for `keep_current`."""
    if decision.final_pick == SAFE_DEFAULT_ID:
        return None
    return ids.index(decision.final_pick)


def deployable_index(decision: JudgeDecision, ids: tuple[str, ...], selection: Selection) -> int | None:
    """The Judge's pick, but only if the selection rule (`autonomy.scientist.select`) also finds that
    candidate eligible; otherwise None (keep the safe default).

    The Judge's rule (`evidence_rule_pick`) is a second copy of the selection rule over the evidence
    table. The two agree whenever a candidate is eligible (the best Bonferroni lower bound above zero is
    the best among the Holm-eligible), but the table does not carry `select`'s "not enough data" outcome
    or its "too good to be true" block (gain above the held-out margin), so on its own it could deploy a
    candidate `select` refused. This guard means the AI loop can only ever deploy `select`'s own choice
    or the safe default, never anything the rule refused."""
    idx = pick_index(decision, ids)
    if idx is None or idx >= len(selection.verdicts) or not selection.verdicts[idx].eligible:
        return None
    return idx


def validation_summary(
    ai: AiPlan, decision: JudgeDecision | None, calls: list[tuple[str, str]]
) -> dict[str, Any]:
    """How many model answers code accepted, repaired or rejected, and why (for the report)."""
    per_role: dict[str, int] = {}
    for role, _ in calls:
        per_role[role] = per_role.get(role, 0) + 1
    stages: dict[str, Staged[Any]] = {"data_doctor": ai.diagnosis, "proposer": ai.proposal}
    rows: dict[str, Any] = {}
    for role, st in stages.items():
        rows[role] = {
            "model_calls": per_role.get(role, 0),
            "repair_rounds": max(0, per_role.get(role, 0) - 1),
            "used": st.source,
            "rejected": st.source == "fallback",
            "problems": list(st.problems),
        }
    if decision is not None:
        rows["judge"] = {
            "model_calls": per_role.get("judge", 0),
            "repair_rounds": max(0, per_role.get("judge", 0) - 1),
            "used": decision.source,
            "rejected": decision.source == "fallback",
            "problems": list(decision.problems),
        }
    return rows


def describe_ai(
    ai: AiPlan,
    candidates: tuple[Candidate, ...],
    selection: Selection,
    table: EvidenceTable | None,
    decision: JudgeDecision | None,
    calls: list[tuple[str, str]],
) -> dict[str, Any]:
    """The JSON-able record of the loop (no model text beyond the typed answers the code accepted)."""
    out: dict[str, Any] = {
        "profile": ai.profile.model_dump(mode="json"),
        "diagnosis": {
            "value": ai.diagnosis.value.model_dump(mode="json"),
            "source": ai.diagnosis.source,
            "problems": list(ai.diagnosis.problems),
        },
        "proposal": {
            "value": ai.proposal.value.model_dump(mode="json"),
            "source": ai.proposal.source,
            "problems": list(ai.proposal.problems),
        },
        "candidate_ids": list(ai.ids),
        "candidate_list": [c.spec() for c in candidates],
        "selection": selection.to_dict(),
        "validation": validation_summary(ai, decision, calls),
    }
    if table is not None and decision is not None:
        out["evidence"] = table.model_dump(mode="json")
        out["judge"] = {
            "final_pick": decision.final_pick,
            "rule_pick": decision.rule_pick,
            "source": decision.source,
            "vetoed_by": decision.vetoed_by,
            "problems": list(decision.problems),
            "code_flags": [f.model_dump(mode="json") for f in decision.code_flags],
            "report": decision.report.model_dump(mode="json"),
        }
    return out
