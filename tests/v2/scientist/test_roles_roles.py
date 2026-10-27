"""The three roles: typed outputs, code-enforced limits, safe defaults, the evidence rule (see the P-list
in test_roles_profile.py)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from autonomy.agents.base import ScriptedLLM
from autonomy.agents.scientist_roles import (
    MIN_EVAL_N,
    SAFE_DEFAULT_ID,
    CandidateEvidence,
    CandidateKind,
    CandidateSpec,
    ClickTrust,
    DataDoctorDiagnosis,
    EvidenceTable,
    JudgeReport,
    Limits,
    OutcomeMetric,
    Pitfall,
    PitfallCode,
    Proposal,
    Settings,
    allowed_outcomes,
    candidate_ids,
    check_diagnosis,
    check_proposal,
    click_trust_ceiling,
    code_flags,
    default_proposal,
    detected_pitfalls,
    doctor_prompt,
    enough_data_floor,
    evidence_rule_pick,
    feasible_goal_ceiling,
    judge_prompt,
    menu_text,
    rule_diagnosis,
    run_data_doctor,
    run_judge,
    run_proposer,
)
from autonomy.contracts import Channel, Event, EventType
from autonomy.scientist.profile import DataProfile, build_profile
from engine.llm import LLMError
from tests.v2.scientist.test_roles_profile import SIGNUP, T0, make_events

FEATURES: dict[str, list[object]] = {
    "recency": [1, 2, 3],
    "zip_code": ["a", "b", "c"],
    "channel": ["x", "y", "z"],
}
LIMITS = Limits(max_contacts_per_week=3)


def adequate(p: DataProfile) -> DataProfile:
    """The fixture campaign scaled past the data thresholds (numbers only; no events are made up)."""
    assert p.outcome is not None
    return p.model_copy(
        update={"customers": 5000, "outcome": p.outcome.model_copy(update={"purchases": 500})}
    )


@pytest.fixture(scope="module")
def small() -> DataProfile:
    return build_profile(make_events(), features=FEATURES)


@pytest.fixture(scope="module")
def big(small: DataProfile) -> DataProfile:
    return adequate(small)


def scripted(answers: list[str] | str) -> ScriptedLLM:
    queue = [answers] if isinstance(answers, str) else list(answers)
    return ScriptedLLM(lambda _s, _p: queue.pop(0) if len(queue) > 1 else queue[0])


def dump(model: Any, **changes: Any) -> str:
    data = json.loads(model.model_dump_json())
    data.update(changes)
    return json.dumps(data)


# --- profile-derived ceilings --------------------------------------------------------------------


def test_ceilings_on_the_fixture(small: DataProfile, big: DataProfile) -> None:
    ok, why = enough_data_floor(small)
    assert not ok and "1000 customers" in why
    assert enough_data_floor(big)[0]
    # extra purchases per click 0.14, interval (-0.02, 0.30): above zero, low end below 0.25 -> discount
    ceiling, reason = click_trust_ceiling(big)
    assert ceiling is ClickTrust.DISCOUNT and "0.140" in reason
    assert set(feasible_goal_ceiling(big)) == {
        "win_back",
        "repeat_purchase",
        "cross_sell",
        "onboarding",
        "retention",
    }
    assert allowed_outcomes(big)[0] is OutcomeMetric.NET_PROFIT


def test_ceilings_without_control_or_with_mixed_currencies() -> None:
    none = build_profile(make_events(with_control=False), features=FEATURES)
    assert click_trust_ceiling(none)[0] is ClickTrust.IGNORE
    assert PitfallCode.NO_CONTROL_GROUP in {p.code for p in detected_pitfalls(none)}
    mixed = build_profile(make_events(extra_currency=True), features=FEATURES)
    assert allowed_outcomes(mixed) == (OutcomeMetric.PURCHASE_RATE,)
    assert PitfallCode.MIXED_CURRENCIES in {p.code for p in detected_pitfalls(mixed)}
    skew = build_profile(make_events(skew_split=True), features=FEATURES)
    assert PitfallCode.SRM in {p.code for p in detected_pitfalls(skew)}


def test_rule_diagnosis_is_valid_and_passes_its_own_checks(small: DataProfile, big: DataProfile) -> None:
    for p in (small, big, build_profile(make_events(with_control=False))):
        d = rule_diagnosis(p)
        assert check_diagnosis(d, p) == []
        assert d.outcome_metric in allowed_outcomes(p)


# --- Data Doctor ---------------------------------------------------------------------------------


def good_diagnosis(p: DataProfile) -> DataDoctorDiagnosis:
    return rule_diagnosis(p).model_copy(
        update={"click_trust": ClickTrust.DISCOUNT, "click_trust_reason": "clicks carry a little signal"}
    )


def test_doctor_accepts_a_valid_cautious_answer_and_adds_detected_pitfalls(big: DataProfile) -> None:
    d = good_diagnosis(big).model_copy(
        update={
            "click_trust": ClickTrust.IGNORE,  # more cautious than the ceiling is fine
            "pitfalls": (
                Pitfall(
                    code=PitfallCode.LEAKAGE,
                    evidence="a feature named after the send might carry the outcome",
                ),
            ),
        }
    )
    out = run_data_doctor(scripted(d.model_dump_json()), big)
    assert out.source == "model" and out.problems == ()
    assert out.value.click_trust is ClickTrust.IGNORE
    assert {p.code for p in out.value.pitfalls} >= {PitfallCode.LEAKAGE}


@pytest.mark.parametrize(
    ("changes", "needle"),
    [
        ({"click_trust": "trust"}, "click_trust"),
        ({"feasible_goals": ["cart_recovery"]}, "feasible_goals"),
        ({"outcome_metric": "click_rate"}, "unusable"),  # not in the enum at all
        ({"feasible_goals": ["win_back", "win_back"]}, "duplicates"),
        ({"pitfalls": [{"code": "no_control_group", "evidence": "nobody was held out here"}]}, "absent"),
        ({"pitfalls": [{"code": "made_up", "evidence": "something about the data"}]}, "unusable"),
        ({"pitfalls": [{"code": "leakage", "evidence": ""}]}, "unusable"),
        ({"click_trust_reason": ""}, "unusable"),
        ({"enough_data": "maybe"}, "unusable"),
    ],
)
def test_doctor_answer_that_breaks_a_limit_is_rejected(
    big: DataProfile, changes: dict[str, Any], needle: str
) -> None:
    bad = dump(good_diagnosis(big), **changes)
    out = run_data_doctor(scripted(bad), big)
    assert out.source == "fallback"
    assert any(needle in p for p in out.problems), out.problems
    assert out.value == rule_diagnosis(big)


def test_doctor_cannot_claim_enough_data_below_the_floor(small: DataProfile) -> None:
    claim = dump(rule_diagnosis(small), enough_data=True)
    out = run_data_doctor(scripted(claim), small)
    assert out.source == "fallback" and not out.value.enough_data
    assert any("thresholds" in p for p in out.problems)


def test_doctor_cannot_pick_money_when_currencies_are_mixed() -> None:
    p = adequate(build_profile(make_events(extra_currency=True), features=FEATURES))
    out = run_data_doctor(scripted(dump(rule_diagnosis(p), outcome_metric="net_profit")), p)
    assert out.source == "fallback" and out.value.outcome_metric is OutcomeMetric.PURCHASE_RATE


def test_doctor_unreadable_answer_gets_one_repair_then_the_default(big: DataProfile) -> None:
    llm = scripted("not json at all")
    out = run_data_doctor(llm, big)
    assert out.source == "fallback" and len(llm.calls) == 2  # ask_json: the answer and one repair
    repaired = scripted(["oops", good_diagnosis(big).model_dump_json()])
    assert run_data_doctor(repaired, big).source == "model"


def test_doctor_provider_down_falls_back(big: DataProfile) -> None:
    class Down(ScriptedLLM):
        def complete(self, *a: Any, **k: Any) -> Any:
            raise LLMError("LLM_UNAVAILABLE", "down")

    out = run_data_doctor(Down(lambda _s, _p: ""), big)
    assert out.source == "fallback" and out.value == rule_diagnosis(big)


def test_doctor_prompt_has_the_profile_and_the_checklist(big: DataProfile) -> None:
    llm = scripted(good_diagnosis(big).model_dump_json())
    run_data_doctor(llm, big)
    system, prompt = llm.calls[0]
    for item in (
        "proxy_metric",
        "no_control_group",
        "leakage",
        "mixed_currencies",
        "srm",
        "novelty",
        "tiny_sample",
    ):
        assert item in system
    assert "multiple_comparisons" in system and "JSON schema" in prompt
    assert prompt.startswith("Diagnose") and big.to_prompt_json() in prompt and doctor_prompt(big) in prompt


# --- Proposer ------------------------------------------------------------------------------------


def settings(**kw: Any) -> dict[str, Any]:
    return {"lookback_days": 60, "exploration_floor": 0.05, "weekly_contact_budget": 2} | kw


def cand(kind: str, **kw: Any) -> dict[str, Any]:
    settings_kw = kw.pop("settings", {})
    return {"kind": kind, "settings": settings(**settings_kw), "rationale": "A plain one-line reason.", **kw}


def proposal(*cands: dict[str, Any]) -> str:
    return json.dumps({"candidates": list(cands)})


GOOD = proposal(
    cand("send_to_all", arm="arm_a"),
    cand("segment_rule", columns=["recency", "zip_code"]),
    cand("bandit_agent"),
)


def test_proposer_accepts_a_valid_menu_pick(big: DataProfile) -> None:
    out = run_proposer(scripted(GOOD), big, rule_diagnosis(big), LIMITS)
    assert out.source == "model" and out.problems == ()
    assert candidate_ids(out.value) == ("c1_send_to_all", "c2_segment_rule", "c3_bandit_agent")


@pytest.mark.parametrize(
    ("answer", "needle"),
    [
        (proposal(), "unusable"),  # no candidates
        (proposal(*[cand("bandit_agent", settings={"lookback_days": 30 + i}) for i in range(4)]), "unusable"),
        (proposal(cand("neural_net")), "unusable"),  # not on the menu
        (proposal(cand("bandit_agent", settings={"exploration_floor": 0.5})), "unusable"),
        (proposal(cand("bandit_agent", settings={"exploration_floor": 0.0})), "unusable"),
        (proposal(cand("bandit_agent", settings={"lookback_days": 3})), "unusable"),
        (proposal(cand("bandit_agent", settings={"lookback_days": 4000})), "unusable"),
        (proposal(cand("bandit_agent", settings={"weekly_contact_budget": 0})), "unusable"),
        (proposal(cand("bandit_agent", settings={"weekly_contact_budget": 9})), "unusable"),
        (proposal(cand("bandit_agent", settings={"weekly_contact_budget": 5})), "mandate"),  # LIMITS is 3
        (proposal(cand("send_to_all", arm="arm_z")), "not an arm"),
        (proposal(cand("send_to_all")), "unusable"),  # no arm
        (proposal(cand("segment_rule", columns=["nope"])), "not in the data"),
        (proposal(cand("segment_rule", columns=[])), "unusable"),
        (proposal(cand("segment_rule", columns=["recency", "zip_code", "channel", "recency"])), "unusable"),
        (proposal(cand("bandit_agent", arm="arm_a")), "unusable"),  # arm on a kind that takes none
        (proposal(cand("bandit_agent", rationale="line one\nline two")), "unusable"),
        (proposal(cand("bandit_agent"), cand("bandit_agent")), "duplicates"),
        (proposal(cand("bandit_agent", code="import os")), "unusable"),  # unknown field: no code from a model
    ],
)
def test_proposal_that_breaks_the_menu_is_rejected_and_the_default_used(
    big: DataProfile, answer: str, needle: str
) -> None:
    out = run_proposer(scripted(answer), big, rule_diagnosis(big), LIMITS)
    assert out.source == "fallback"
    assert any(needle in p for p in out.problems), out.problems
    assert out.value == default_proposal(big, LIMITS)


def test_models_needing_features_or_logged_probabilities_are_refused_without_them() -> None:
    bare = adequate(build_profile(make_events()))  # no features listed
    p = Proposal.model_validate_json(proposal(cand("uplift_model")))
    assert any("features" in x for x in check_proposal(p, bare, LIMITS))
    events = [e for e in make_events() if e.type is not EventType.SEND or "logged_probability" in e.attrs]
    stripped = [
        (
            e.model_copy(update={"attrs": {"content_id": e.attrs["content_id"], "campaign_id": "camp1"}})
            if e.type is EventType.SEND
            else e
        )
        for e in events
    ]
    unlogged = adequate(build_profile(stripped, features=FEATURES))
    assert any("probabilities" in x for x in check_proposal(p, unlogged, LIMITS))


def test_default_proposal_is_always_valid(small: DataProfile, big: DataProfile) -> None:
    profiles = [
        small,
        big,
        build_profile(make_events(with_control=False), features=FEATURES),
        build_profile(make_events(), features=None),
        build_profile([]),
    ]
    for p in profiles:
        for limits in (Limits(1), Limits(2), Limits(7)):
            d = default_proposal(p, limits)
            assert check_proposal(d, p, limits) == []
            assert 1 <= len(d.candidates) <= 3
    d = default_proposal(big, LIMITS)
    assert [c.kind for c in d.candidates] == [
        CandidateKind.SEND_TO_ALL,
        CandidateKind.UPLIFT_MODEL,
        CandidateKind.BANDIT_AGENT,
    ]
    assert d.candidates[0].arm == "arm_a"  # the arm with the best purchase rate (0.15 against 0.12)


def test_proposer_prompt_carries_menu_diagnosis_and_lists(big: DataProfile) -> None:
    llm = scripted(GOOD)
    diag = rule_diagnosis(big)
    run_proposer(llm, big, diag, LIMITS)
    system, prompt = llm.calls[0]
    assert menu_text(LIMITS) in prompt and diag.model_dump_json() in prompt
    assert '"max":3' in menu_text(LIMITS) and "arm_a" in prompt and "zip_code" in prompt
    assert "send_to_all" in system and "uplift_model" in system


# --- the evidence rule ---------------------------------------------------------------------------


def ev(
    cid: str, *, n: int = 1000, est: float = 1.0, low: float = 0.5, high: float | None = None, **kw: Any
) -> CandidateEvidence:
    return CandidateEvidence(
        candidate_id=cid,
        n_eval=n,
        gain_estimate=est,
        gain_low=low,
        gain_high=high if high is not None else est + 1,
        **kw,
    )


def table(*rows: CandidateEvidence, current: float | None = 10.0) -> EvidenceTable:
    return EvidenceTable(
        unit="profit per customer",
        correction="holm",
        comparisons=max(1, len(rows)),
        current_value=current,
        candidates=rows,
    )


def test_evidence_rule_hand_computed() -> None:
    a, b, c = "c1_send_to_all", "c2_segment_rule", "c3_bandit_agent"
    # best corrected lower bound above zero wins, not the best estimate
    assert evidence_rule_pick(table(ev(a, est=5, low=0.2), ev(b, est=2, low=0.9), ev(c, est=3, low=0.4))) == b
    # a bound of exactly zero or below does not qualify
    assert evidence_rule_pick(table(ev(a, low=0.0), ev(b, low=-1.0))) == SAFE_DEFAULT_ID
    # too small an evaluation sample does not qualify, however good it looks
    assert evidence_rule_pick(table(ev(a, n=MIN_EVAL_N - 1, est=9, low=8), ev(b, low=0.1))) == b
    assert evidence_rule_pick(table(ev(a, n=MIN_EVAL_N - 1, est=9, low=8))) == SAFE_DEFAULT_ID
    # ties go to the candidate listed first
    assert evidence_rule_pick(table(ev(a, low=0.5), ev(b, low=0.5))) == a
    # no candidates qualify: the safe default
    assert evidence_rule_pick(table()) == SAFE_DEFAULT_ID


def test_code_flags_find_too_good_to_be_true() -> None:
    rows = (
        ev("c1_send_to_all", est=50, low=40, high=60),  # gain 50 against a current value of 10
        ev("c2_segment_rule", n=100, est=1, low=0.5),
        ev("c3_bandit_agent", est=1, low=0.5, effective_sample_share=0.02, forecast_vs_actual=4.0),
    )
    flags = {(f.candidate_id, f.code.value) for f in code_flags(table(*rows))}
    assert flags == {
        ("c1_send_to_all", "gain_implausible"),
        ("c2_segment_rule", "tiny_sample"),
        ("c3_bandit_agent", "low_overlap"),
        ("c3_bandit_agent", "calibration_off"),
    }
    assert (
        code_flags(table(ev("c1_send_to_all", est=1, low=2, high=3)))[0].code.value == "inconsistent_evidence"
    )
    assert code_flags(table(ev("c1_send_to_all", est=1, low=0.5, forecast_vs_actual=1.2))) == ()


# --- Judge ---------------------------------------------------------------------------------------


PROPOSAL = Proposal.model_validate_json(GOOD)
IDS = candidate_ids(PROPOSAL)  # c1_send_to_all, c2_segment_rule, c3_bandit_agent


def clean_table(**over: float) -> EvidenceTable:
    return table(
        ev(IDS[0], est=1.0, low=0.2, high=1.8),
        ev(IDS[1], est=2.0, low=0.9, high=3.1),
        ev(IDS[2], est=0.5, low=-0.4, high=1.4),
    )


def report(pick: str, **kw: Any) -> str:
    return json.dumps(
        {
            "pick": pick,
            "too_good_to_be_true": kw.get("flags", []),
            "explanation": kw.get(
                "explanation", "The segment rule has the best lower bound. It stays in Shadow first."
            ),
            "needs_human": kw.get("needs_human"),
        }
    )


def test_judge_agrees_with_the_rule(big: DataProfile) -> None:
    d = run_judge(scripted(report(IDS[1])), big, PROPOSAL, clean_table())
    assert (d.final_pick, d.rule_pick, d.source, d.vetoed_by, d.problems) == (
        IDS[1],
        IDS[1],
        "model",
        None,
        (),
    )


def test_judge_veto_needs_a_concrete_flag_on_the_rules_pick(big: DataProfile) -> None:
    flag = {
        "candidate_id": IDS[1],
        "code": "leakage_suspected",
        "evidence": "a feature was built after the send",
    }
    ask = "Was the zip code column filled in after the send?"
    d = run_judge(
        scripted(report(SAFE_DEFAULT_ID, flags=[flag], needs_human=ask)), big, PROPOSAL, clean_table()
    )
    assert (d.final_pick, d.source, d.vetoed_by) == (SAFE_DEFAULT_ID, "model", "model")
    assert d.report.needs_human == ask
    # the same veto without a flag is thrown away: the rule's pick stands (code made the decision)
    d2 = run_judge(scripted(report(SAFE_DEFAULT_ID)), big, PROPOSAL, clean_table())
    assert (
        d2.source == "fallback"
        and d2.final_pick == IDS[1]
        and any("without a concrete flag" in p for p in d2.problems)
    )
    # a flag on a different candidate does not license vetoing this one
    other = {"candidate_id": IDS[0], "code": "tiny_sample", "evidence": "small sample behind it"}
    d3 = run_judge(scripted(report(SAFE_DEFAULT_ID, flags=[other])), big, PROPOSAL, clean_table())
    assert d3.source == "fallback" and d3.final_pick == IDS[1]


def test_judge_can_never_pick_another_candidate(big: DataProfile) -> None:
    flag = {"candidate_id": IDS[1], "code": "tiny_sample", "evidence": "small sample behind it"}
    for pick in (IDS[0], IDS[2], "c9_made_up"):
        d = run_judge(scripted(report(pick, flags=[flag])), big, PROPOSAL, clean_table())
        assert d.source == "fallback" and d.final_pick == IDS[1], pick
    # and when the rule says keep_current the Judge cannot upgrade to a candidate
    weak = table(ev(IDS[0], low=-1), ev(IDS[1], low=-1), ev(IDS[2], low=-1))
    d = run_judge(scripted(report(IDS[0])), big, PROPOSAL, weak)
    assert d.final_pick == SAFE_DEFAULT_ID and d.source == "fallback"
    ok = run_judge(
        scripted(report(SAFE_DEFAULT_ID, explanation="Nothing beat the current set-up.")), big, PROPOSAL, weak
    )
    assert ok.source == "model" and ok.final_pick == SAFE_DEFAULT_ID


def test_code_blocks_a_pick_that_is_too_good_to_be_true(big: DataProfile) -> None:
    risky = table(
        ev(IDS[0], est=0.5, low=0.2, high=0.9),
        ev(IDS[1], est=50.0, low=40.0, high=60.0),  # a gain of 50 on a current value of 10
        ev(IDS[2], est=0.1, low=-0.2, high=0.4),
    )
    assert evidence_rule_pick(risky) == IDS[1]
    d = run_judge(scripted(report(IDS[1])), big, PROPOSAL, risky)  # the model agrees with the rule
    assert (d.final_pick, d.vetoed_by, d.source) == (SAFE_DEFAULT_ID, "code", "model")
    assert d.report.needs_human is not None and d.report.needs_human.endswith("?")
    assert {f.code.value for f in d.code_flags} == {"gain_implausible"}


@pytest.mark.parametrize(
    ("answer", "needle"),
    [
        (report(IDS[1], explanation="One. Two. Three. Four."), "unusable"),  # four sentences
        (
            report(IDS[1], explanation="It won by 77 percent over everything."),
            "numbers",
        ),  # a number not in the evidence
        (report(IDS[1], needs_human="Check the data."), "unusable"),  # not a question
        (
            report(
                IDS[1], flags=[{"candidate_id": "zzz", "code": "tiny_sample", "evidence": "small sample"}]
            ),
            "unknown candidate",
        ),
        (
            report(IDS[1], flags=[{"candidate_id": IDS[0], "code": "bad_code", "evidence": "small sample"}]),
            "unusable",
        ),
        (report(IDS[1], flags=[{"candidate_id": IDS[0], "code": "tiny_sample", "evidence": ""}]), "unusable"),
        ("not json", "unusable"),
    ],
)
def test_judge_answer_that_breaks_a_limit_is_rejected(big: DataProfile, answer: str, needle: str) -> None:
    d = run_judge(scripted(answer), big, PROPOSAL, clean_table())
    assert d.source == "fallback" and d.final_pick == IDS[1]
    assert any(needle in p for p in d.problems), d.problems
    assert len(d.report.explanation) > 5 and d.report.pick == d.final_pick


def test_judge_explanation_may_quote_evidence_numbers(big: DataProfile) -> None:
    text = "The segment rule gained 2.0 on average, and its corrected lower bound is 0.9. It runs in Shadow first."
    d = run_judge(scripted(report(IDS[1], explanation=text)), big, PROPOSAL, clean_table())
    assert d.source == "model"


def test_judge_provider_down_uses_the_rule(big: DataProfile) -> None:
    class Down(ScriptedLLM):
        def complete(self, *a: Any, **k: Any) -> Any:
            raise LLMError("LLM_UNAVAILABLE", "down")

    d = run_judge(Down(lambda _s, _p: ""), big, PROPOSAL, clean_table())
    assert (d.source, d.final_pick) == ("fallback", IDS[1])
    assert "Shadow" in d.report.explanation


def test_judge_fallback_keeps_the_default_when_code_flags_the_pick(big: DataProfile) -> None:
    risky = table(ev(IDS[0], low=-1), ev(IDS[1], est=50.0, low=40.0, high=60.0), ev(IDS[2], low=-1))
    d = run_judge(scripted("not json"), big, PROPOSAL, risky)
    assert (d.source, d.final_pick, d.vetoed_by) == ("fallback", SAFE_DEFAULT_ID, "code")
    assert d.report.needs_human is not None


def test_judge_sees_only_inputs_and_evidence(big: DataProfile) -> None:
    secret = "ZZ-PROPOSER-REASONING-ZZ"  # secret-scan: allow (fake test value)
    spec = PROPOSAL.candidates[0].model_copy(update={"rationale": secret})
    proposal_with_secret = Proposal(candidates=(spec, *PROPOSAL.candidates[1:]))
    llm = scripted(report(IDS[1]))
    run_judge(llm, big, proposal_with_secret, clean_table())
    system, prompt = llm.calls[0]
    assert secret not in prompt and secret not in system
    assert "rationale" not in prompt and "Diagnosis" not in prompt and "diagnosis" not in prompt
    assert "arm_a" in prompt and IDS[1] in prompt and "Evidence rule's pick" in prompt
    assert "skeptic" in system.lower() or "sceptic" in system.lower()
    assert (
        judge_prompt(big, proposal_with_secret, clean_table(), IDS[1], ())
        == prompt.split("\n\nAnswer with one JSON")[0]
    )


def test_judge_needs_evidence_for_exactly_the_proposed_candidates(big: DataProfile) -> None:
    with pytest.raises(ValueError, match="do not match"):
        run_judge(scripted(report(IDS[1])), big, PROPOSAL, table(ev(IDS[0])))


def test_typed_models_reject_unknown_fields_and_are_frozen() -> None:
    with pytest.raises(ValueError):
        JudgeReport.model_validate({"pick": "x", "explanation": "Fine words here.", "extra": 1})
    with pytest.raises(ValueError):
        CandidateSpec.model_validate({"kind": "bandit_agent", "settings": settings(), "rationale": "x"})
    s = Settings(lookback_days=60, exploration_floor=0.05, weekly_contact_budget=2)
    with pytest.raises(ValueError):
        s.lookback_days = 10


def test_new_files_name_no_dataset() -> None:
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    pattern = re.compile(r"hillstrom|x5|retailhero|lenta|starbucks|criteo|megafon", re.IGNORECASE)
    files = [
        "autonomy/scientist/profile.py",
        "autonomy/scientist/run.py",
        "autonomy/agents/scientist_roles.py",
        "autonomy/agents/llm_exchange.py",
        "autonomy/agents/prompts/data_doctor.md",
        "autonomy/agents/prompts/proposer.md",
        "autonomy/agents/prompts/judge.md",
    ]
    for name in files:
        hits = [ln for ln in (root / name).read_text(encoding="utf-8").splitlines() if pattern.search(ln)]
        assert hits == [], (name, hits)


_ = (Channel, Event, SIGNUP, T0)


def test_judge_explanation_may_name_candidates_by_id(big: DataProfile) -> None:
    # "c2_segment_rule" has a digit that is a label, not a claim about the evidence
    text = f"{IDS[1]} has the best corrected lower bound, 0.9, and {SAFE_DEFAULT_ID} would give up its gain."
    d = run_judge(scripted(report(IDS[1], explanation=text)), big, PROPOSAL, clean_table())
    assert (d.source, d.problems) == ("model", ())
    # a number that is not in the evidence next to an id is still caught
    bad = f"{IDS[1]} gained 7.7 per customer. It starts in Shadow."
    d = run_judge(scripted(report(IDS[1], explanation=bad)), big, PROPOSAL, clean_table())
    assert d.source == "fallback" and any("7.7" in p for p in d.problems)


def test_evidence_must_be_corrected_for_every_candidate_and_ids_unique() -> None:
    rows = (ev(IDS[0]), ev(IDS[1]), ev(IDS[2]))
    with pytest.raises(ValueError, match="below the number of candidates"):
        EvidenceTable(unit="profit per customer", correction="holm", comparisons=1, candidates=rows)
    with pytest.raises(ValueError, match="repeat"):
        EvidenceTable(
            unit="profit per customer", correction="holm", comparisons=2, candidates=(ev(IDS[0]), ev(IDS[0]))
        )
    EvidenceTable(unit="profit per customer", correction="holm", comparisons=3, candidates=rows)


def test_a_mandate_with_no_contacts_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        Limits(max_contacts_per_week=0)
