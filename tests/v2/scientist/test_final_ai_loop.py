"""The AI loop inside the bench (`--policy scientist --ai-answers`): recorded answers, held-out evidence.

Tiny inline trial (a message that pays only where x == 1). Answers are scripted JSON, never a model: the
prompts the roles ask are recorded first (`AnswerPending`), answered, and replayed.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from autonomy.agents.llm_exchange import AnswerPending, RecordingLLM
from autonomy.agents.scientist_roles import SAFE_DEFAULT_ID, rule_diagnosis
from autonomy.scientist import BlanketPolicy
from scripts.v2_realbench import adapters, ai_loop, common
from scripts.v2_realbench.run import main
from scripts.v2_realbench.scientist import AI_POLICY_NAME, POLICY_NAME, evaluate_scientist, render_scientist
from tests.v2.scientist.test_menu_bench import synthetic

TIMINGS = common.Timings(quiet=True)


def rich(n: int = 5000, seed: int = 8) -> common.BenchData:
    """Like `synthetic`, but a high base rate, so a real gain is a small share of the current value."""
    rng = np.random.default_rng(seed)
    ids = np.array([f"r{i}" for i in range(n)], dtype=object)
    x = rng.integers(0, 2, n)
    arm = rng.integers(0, 2, n).astype(np.int64)
    conv = (rng.random(n) < 0.35 + 0.25 * arm * x - 0.0 * arm).astype(float)
    feats = pd.DataFrame({"x": x.astype(float), "z": rng.normal(size=n)})
    return adapters._feature_rct(
        "rich", ids, arm, 0.5, feats, conv, None, (("x",), ("z",)), ("inline fixture",), {"rows": n}
    )


def _answers_for(data: common.BenchData, *, proposal: dict[str, Any] | None = None) -> dict[str, str]:
    """Doctor and Proposer answers for the synthetic trial (the rule-based diagnosis, a typed proposal)."""
    train, _ = common.split_by_hash(data.customer_ids)
    profile = ai_loop.train_profile(data, train)
    out: dict[str, str] = {}
    rec = RecordingLLM()
    from autonomy.agents.scientist_roles import Limits, run_data_doctor, run_proposer

    diag = rule_diagnosis(profile)
    for stage in (lambda: run_data_doctor(rec, profile), lambda: run_proposer(rec, profile, diag, Limits())):
        with pytest.raises(AnswerPending):
            stage()
    ids = [r["id"] for r in rec.records]
    # the proposer's prompt only exists once the doctor is answered: answer, then record the next one
    out[ids[0]] = diag.model_dump_json()
    cid = next(iter(data.agent.arm_content.values()))
    out["proposer"] = json.dumps(
        proposal
        or {
            "candidates": [
                {
                    "kind": "send_to_all",
                    "arm": cid,
                    "columns": [],
                    "settings": {"lookback_days": 90, "exploration_floor": 0.01, "weekly_contact_budget": 1},
                    "rationale": "Blanket bar: the message to everyone.",
                },
                {
                    "kind": "segment_rule",
                    "arm": None,
                    "columns": ["x"],
                    "settings": {"lookback_days": 90, "exploration_floor": 0.05, "weekly_contact_budget": 1},
                    "rationale": "Targeted option on one feature.",
                },
            ]
        }
    )
    return out


def _record(data: common.BenchData, answers: dict[str, str]) -> tuple[ai_loop.AiSetup, RecordingLLM]:
    rec = RecordingLLM()
    return ai_loop.AiSetup(ai_loop.CountingReplay(answers, fallback=rec)), rec


def _resolve(data: common.BenchData, answers: dict[str, str]) -> dict[str, str]:
    """Fill in every answer the loop asks for, one prompt at a time (the real two-pass way)."""
    answers = dict(answers)
    answers.pop("proposer", None)
    base = _answers_for(data)
    doctor_id = next(k for k in base if k != "proposer")
    answers[doctor_id] = base[doctor_id]
    for _ in range(4):
        setup, rec = _record(data, answers)
        try:
            evaluate_scientist(data, seeds=1, timings=TIMINGS, with_agent=False, ai=setup)
            return answers
        except AnswerPending as pending:
            record = rec.records[-1]
            assert record["id"] == pending.prompt_id
            if record["role"] == "proposer":
                answers[record["id"]] = base["proposer"]
            elif record["role"] == "judge":
                answers[record["id"]] = _judge_answer(record["prompt"])
            else:  # pragma: no cover - the doctor is answered above
                raise AssertionError(record["role"]) from None
    raise AssertionError("the loop kept asking")


def _judge_answer(prompt: str) -> str:
    """Agree with the evidence rule's pick, which the prompt states, in words with no numbers."""
    text = prompt.split("Evidence rule's pick")[1] if "Evidence rule's pick" in prompt else prompt
    pick = SAFE_DEFAULT_ID
    for cid in ("c1_send_to_all", "c2_segment_rule"):
        if cid in text.split("\n")[0]:
            pick = cid
    return json.dumps(
        {
            "pick": pick,
            "too_good_to_be_true": [],
            "explanation": "The rule's pick stands.",
            "needs_human": None,
        }
    )


@pytest.fixture(scope="module")
def trial() -> tuple[common.BenchData, dict[str, str]]:
    data, _ = synthetic(4000, seed=5)
    return data, _resolve(data, {})


def test_the_profile_is_numbers_only_and_built_from_train_alone(
    trial: tuple[common.BenchData, dict[str, str]],
) -> None:
    data, _ = trial
    train, test = common.split_by_hash(data.customer_ids)
    p = ai_loop.train_profile(data, train)
    text = p.to_prompt_json()
    assert p.customers == len(train)
    assert not any(str(cid) in text for cid in data.customer_ids[:50])
    flipped = dataclasses.replace(
        data, conversion=np.where(np.isin(np.arange(data.n), test), 1.0, data.conversion)
    )
    assert ai_loop.train_profile(flipped, train).to_prompt_json() == text  # a TEST outcome never reaches it


def test_a_missing_answer_stops_before_any_expensive_work() -> None:
    data, calls = synthetic(2400)
    setup, rec = _record(data, {})
    with pytest.raises(AnswerPending):
        evaluate_scientist(data, seeds=1, timings=TIMINGS, with_agent=True, ai=setup)
    # only the profile read the campaign (TRAIN customers); the agent was never warm-started
    train, _ = common.split_by_hash(data.customer_ids)
    assert len(calls) == 1 and set(calls[0]) == set(data.customer_ids[train].tolist())
    assert [r["role"] for r in rec.records] == ["data_doctor"]


def test_the_loop_replays_answers_and_scores_the_judges_pick_on_test(
    trial: tuple[common.BenchData, dict[str, str]],
) -> None:
    data, answers = trial
    setup, rec = _record(data, answers)
    out = evaluate_scientist(data, seeds=1, timings=TIMINGS, with_agent=False, ai=setup)
    assert rec.records == []  # nothing was left unanswered
    ai = out["ai"]
    assert [v["kind"] for v in out["selection"]["verdicts"]] == ["segment", "uplift"]  # the fixed list
    assert ai["candidate_ids"] == ["c1_send_to_all", "c2_segment_rule"]
    assert ai["proposal"]["source"] == "model" and ai["diagnosis"]["source"] == "model"
    assert [c["kind"] for c in ai["candidate_list"]] == ["blanket", "segment"]
    j = ai["judge"]
    # the Judge never picks another candidate: its final pick is the rule's, or keep_current after a veto
    assert j["final_pick"] == j["rule_pick"] or (j["final_pick"] == SAFE_DEFAULT_ID and j["vetoed_by"])
    assert ai["agreement"]["judge_pick_equals_evidence_rule"] is (j["final_pick"] == j["rule_pick"])
    assert {c["candidate_id"] for c in ai["evidence"]["candidates"]} == set(ai["candidate_ids"])
    assert ai["evidence"]["comparisons"] >= len(ai["candidate_ids"])
    pol = out["policies"]
    assert POLICY_NAME in pol and AI_POLICY_NAME in pol
    n_other = len(pol) - 2
    assert out["multiplicity"]["m"] == n_other and ai["multiplicity"]["m"] == n_other
    assert (
        pol[AI_POLICY_NAME]["dr"]["low"]
        <= pol[AI_POLICY_NAME]["dr"]["point"]
        <= pol[AI_POLICY_NAME]["dr"]["high"]
    )
    assert (
        ai["validation"]["data_doctor"]["rejected"] is False and ai["validation"]["judge"]["used"] == "model"
    )
    json.dumps(out, default=str)
    md = render_scientist({"datasets": {"synthetic": out}})
    assert "Fixed list vs Sonnet-proposed vs the agent" in md and "**Judge** (model)" in md


def test_the_evidence_and_the_pick_cannot_see_the_test_outcomes(
    trial: tuple[common.BenchData, dict[str, str]],
) -> None:
    data, answers = trial
    _, test = common.split_by_hash(data.customer_ids)
    conversion = data.conversion.copy()
    margin = data.margin_value.copy()
    conversion[test] = 1.0 - conversion[test]
    margin[test] = 20.0 * conversion[test]
    poisoned = dataclasses.replace(data, conversion=conversion, margin_value=margin)
    a = evaluate_scientist(data, seeds=1, timings=TIMINGS, with_agent=False, ai=_record(data, answers)[0])
    b = evaluate_scientist(
        poisoned, seeds=1, timings=TIMINGS, with_agent=False, ai=_record(poisoned, answers)[0]
    )
    for key in ("profile", "diagnosis", "proposal", "evidence", "judge", "deployed", "selection"):
        assert a["ai"][key] == b["ai"][key], key  # same prompts (the answers replayed), same evidence
    assert a["ai"]["value"] != b["ai"]["value"]  # but the TEST scoring did change


def test_code_blocks_a_pick_that_is_too_good_to_be_true_and_the_default_is_deployed(
    trial: tuple[common.BenchData, dict[str, str]],
) -> None:
    data, answers = trial  # here the segment rule's gain is larger than the whole current value
    out = evaluate_scientist(data, seeds=1, timings=TIMINGS, with_agent=False, ai=_record(data, answers)[0])
    ai = out["ai"]
    assert ai["judge"]["rule_pick"] == "c2_segment_rule" and ai["judge"]["final_pick"] == SAFE_DEFAULT_ID
    assert ai["judge"]["vetoed_by"] == "code"
    assert [f["code"] for f in ai["judge"]["code_flags"]] == ["gain_implausible"]
    assert ai["deployed"] == ai["selection"]["default"]["label"]  # the safe default is what runs


def test_an_agreed_pick_is_the_candidate_that_runs() -> None:
    data = rich()
    answers = _resolve(data, {})
    out = evaluate_scientist(data, seeds=1, timings=TIMINGS, with_agent=False, ai=_record(data, answers)[0])
    ai = out["ai"]
    assert ai["judge"]["rule_pick"] == "c2_segment_rule" == ai["judge"]["final_pick"]
    assert ai["judge"]["vetoed_by"] is None and ai["judge"]["code_flags"] == []
    assert ai["deployed_kind"] == "segment" and ai["agreement"]["judge_pick_equals_evidence_rule"] is True
    assert out["policies"][AI_POLICY_NAME]["contact_rate"] < 1.0  # it targets, it does not blanket


def test_the_proposed_candidates_are_built_through_the_menu_only() -> None:
    data, _ = synthetic(2400)
    from autonomy.agents.scientist_roles import CandidateKind, CandidateSpec, Proposal, Settings

    st = Settings(lookback_days=30, exploration_floor=0.05, weekly_contact_budget=1)
    cid = next(iter(data.agent.arm_content.values()))
    prop = Proposal(
        candidates=(
            CandidateSpec(kind=CandidateKind.SEND_TO_ALL, arm=cid, settings=st, rationale="the blanket bar"),
            CandidateSpec(kind=CandidateKind.UPLIFT_MODEL, settings=st, rationale="a targeted model"),
            CandidateSpec(kind=CandidateKind.BANDIT_AGENT, settings=st, rationale="the current agent"),
        )
    )
    cands = ai_loop.proposal_candidates(prop, data, None)
    assert [c.kind for c in cands] == ["blanket", "uplift", "bandit"]
    blanket = cands[0]
    assert (
        isinstance(blanket, BlanketPolicy) and blanket.arm == 1
    )  # the adapter's arm 1 carries that content id
    assert blanket.exploration_floor == pytest.approx(0.05)


def test_the_cli_refuses_ai_answers_where_they_do_not_belong(tmp_path: Path) -> None:
    answers = tmp_path / "a.json"
    answers.write_text("{}")
    base = ["--data-dir", "/nonexistent"]
    assert main(["--dataset", "hillstrom", "--ai-answers", str(answers), *base]) == 2  # agent policy
    assert main(["--dataset", "lenta", "--policy", "scientist", "--ai-answers", str(answers), *base]) == 2
    answers.write_text("[1, 2]")  # not an object: refused before any data is read
    assert main(["--dataset", "hillstrom", "--policy", "scientist", "--ai-answers", str(answers), *base]) == 2
    answers.write_text("not json")
    assert main(["--dataset", "hillstrom", "--policy", "scientist", "--ai-answers", str(answers), *base]) == 2


def test_the_cli_saves_the_prompt_it_waits_for_and_finishes_when_it_is_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.v2_realbench import run as run_module

    data, _ = synthetic(3000, seed=3)
    monkeypatch.setattr(run_module, "load", lambda name, data_dir, timings, limit=None: data)
    answers = tmp_path / "answers.json"
    record = tmp_path / "prompts.json"
    report = tmp_path / "report.json"
    answers.write_text("{}")
    base = [
        "--dataset",
        "hillstrom",
        "--policy",
        "scientist",
        "--no-scientist-agent",
        "--quiet",
        "--ai-answers",
        str(answers),
        "--ai-record",
        str(record),
        "--json",
        str(report),
    ]
    assert main(base) == 3  # nothing answered: the Data Doctor's prompt is saved, never guessed
    saved = json.loads(record.read_text())
    assert [r["role"] for r in saved] == ["data_doctor"] and saved[0]["schema"] is not None
    assert "error" in json.loads(report.read_text())["datasets"]["hillstrom"]
    answers.write_text(json.dumps(_resolve(data, {})))
    assert main(base) == 0
    block = json.loads(report.read_text())["datasets"]["hillstrom"]
    assert block["ai"]["judge"]["source"] == "model" and "pending_prompt" not in block
