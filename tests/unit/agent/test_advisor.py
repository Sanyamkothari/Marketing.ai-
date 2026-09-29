"""The advisor (Plan G M73): rules only, every claim cited, never a loosened safeguard."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from engine.agent.advisor import ROLE_PRIMARY_KEY, ROLE_TARGET, advise, run_overrides, summarise
from engine.agent.contracts import DecidedBy, ProposalKind, ProposalState
from engine.agent.recommend import NEVER_RECOMMENDED
from engine.agent.scope import agent_available
from engine.config import (
    ADVISORY_PATHS,
    IMMUTABLE_PATHS,
    load_all_use_cases,
    overridable_paths,
    resolve_config,
)
from tests.fixtures.agent_bench.cases import CASES, EXPECTED, run_case
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.fixtures.make_data import GenerationSpec, generate
from tests.unit.agent.helpers import context_for

GOLDEN = json.loads(EXPECTED.read_text(encoding="utf-8"))


def test_the_golden_file_covers_every_case() -> None:
    assert set(GOLDEN) == set(CASES)


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_advisor_matches_the_benchmark(case: str) -> None:
    """A rule change shows up here as a diff; `python -m tests.fixtures.agent_bench.cases --update` accepts it."""
    assert run_case(case) == GOLDEN[case]


def test_every_proposal_cites_evidence_the_advice_holds() -> None:
    advice = advise(context_for(messy_frame()))
    evidence = {result.evidence_id for result in advice.tool_results}
    cited = {e for p in advice.proposals for e in p.evidence_ids}
    cited |= {e for q in advice.questions for e in q.evidence_ids}
    cited |= {e for q in advice.questions for o in q.options if o.proposal for e in o.proposal.evidence_ids}
    assert cited <= evidence
    assert all(p.state is ProposalState.PENDING for p in advice.proposals)


def _trainable() -> list[str]:
    return sorted(uc for uc, config in load_all_use_cases().items() if agent_available(config))


@pytest.mark.parametrize("use_case_id", _trainable())
def test_no_use_case_is_ever_offered_an_unsafe_setting(use_case_id: str) -> None:
    frame = generate(GenerationSpec(use_case_id=use_case_id, rows=3_000))
    advice = advise(context_for(frame, use_case_id))
    config = load_all_use_cases()[use_case_id]
    legal = overridable_paths(config)
    for proposal in advice.proposals:
        if proposal.kind is not ProposalKind.SETTING:
            continue
        assert proposal.path in legal
        assert proposal.path not in ADVISORY_PATHS | NEVER_RECOMMENDED
        assert proposal.path.split(".")[0] not in IMMUTABLE_PATHS
    settings = {p.path: p.value for p in advice.proposals if p.kind is ProposalKind.SETTING}
    resolve_config(use_case_id, settings)  # every suggestion together resolves


def test_the_advisor_never_touches_the_frame() -> None:
    frame = messy_frame()
    before = frame.copy()
    advise(context_for(frame))
    assert frame.equals(before)


def test_answering_a_role_question_fixes_the_role() -> None:
    frame = generate(GenerationSpec(use_case_id="targeted-advertisement", rows=3_000)).rename(
        columns={"converted_30d": "bought", "customer_id": "cid"}
    )
    first = advise(context_for(frame))
    guessed = {p.path: (p.value, p.confidence.value) for p in first.proposals if p.kind is ProposalKind.ROLE}
    # 'cid' is not named like this use case's IDs but is the file's only unique column, so "sure";
    # 'bought' is only a synonym of the outcome, so "check".
    assert guessed == {ROLE_PRIMARY_KEY: ("cid", "sure"), ROLE_TARGET: ("bought", "check")}
    chosen = advise(context_for(frame), primary_key="cid", target="bought")
    roles = {p.path: (p.value, p.confidence.value) for p in chosen.proposals if p.kind is ProposalKind.ROLE}
    assert roles == {ROLE_PRIMARY_KEY: ("cid", "sure"), ROLE_TARGET: ("bought", "sure")}


def _single_two_valued_column_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"customer_id": [f"C{i}" for i in range(1000)], "y": [0, 1] * 500, "spend": range(1000)}
    )


def test_one_two_valued_column_is_proposed_as_the_outcome_to_check_never_asked_about() -> None:
    """A question needs two choices, so the one candidate becomes a `check` proposal the person accepts."""
    advice = advise(context_for(_single_two_valued_column_frame()))
    assert advice.stop_reason is None
    assert not [q for q in advice.questions if "Which column says" in q.text]
    targets = [p for p in advice.proposals if p.kind is ProposalKind.ROLE and p.path == ROLE_TARGET]
    assert [(p.value, p.confidence.value, p.state) for p in targets] == [
        ("y", "check", ProposalState.PENDING)
    ]


def test_two_two_valued_columns_are_still_a_question() -> None:
    frame = _single_two_valued_column_frame().assign(flag=[0, 0, 1, 1] * 250)
    advice = advise(context_for(frame))
    asked = [q for q in advice.questions if "Which column says" in q.text]
    assert len(asked) == 1 and len(asked[0].options) == 2
    assert not [p for p in advice.proposals if p.path == ROLE_TARGET]


def test_no_two_valued_column_stops() -> None:
    frame = _single_two_valued_column_frame().drop(columns=["y"])
    advice = advise(context_for(frame))
    assert advice.stop_reason is not None and "No column says" in advice.stop_reason


def _decide_all(advice_proposals: tuple, *, accept: bool = True) -> list:
    state = ProposalState.ACCEPTED if accept else ProposalState.REJECTED
    return [p.model_copy(update={"state": state, "decided_by": DecidedBy.USER}) for p in advice_proposals]


def test_run_overrides_carry_accepted_settings_and_acknowledgements_only() -> None:
    advice = advise(context_for(messy_frame()))
    accepted = _decide_all(advice.proposals)
    keep = next(o.proposal for o in advice.questions[0].options if o.option_id == "keep")
    assert keep is not None
    accepted.append(keep.model_copy(update={"state": ProposalState.ACCEPTED, "decided_by": DecidedBy.USER}))
    overrides = run_overrides(accepted)
    assert overrides["split.type"] == "time_based"
    assert overrides["validation.acknowledged"] == ["LEAKAGE_SUSPECTED:campaign_result_score"]
    assert ROLE_PRIMARY_KEY not in overrides
    assert run_overrides(_decide_all(advice.proposals, accept=False)) == {}


def test_the_summary_says_what_was_decided_and_what_will_happen() -> None:
    ctx = context_for(messy_frame())
    advice = advise(ctx)
    summary = summarise(
        ctx, _decide_all(advice.proposals), assumptions=advice.assumptions, engine_hidden=advice.engine_hidden
    )
    assert "Turn 'monthly_spend' into numbers" in summary.decisions
    assert any("newest" in line and "snapshot_date" in line for line in summary.intended_actions)
    assert any("'converted_30d'" in line for line in summary.intended_actions)
    assert any(line.startswith("contact_email:") for line in summary.hidden_columns)
    rejected = summarise(ctx, _decide_all(advice.proposals, accept=False), assumptions=(), engine_hidden=())
    assert rejected.decisions == ()
    assert any("at random" in line for line in rejected.intended_actions)


def test_a_scoring_file_without_a_schema_is_not_checked() -> None:
    from engine.config import RunMode

    frame = generate(GenerationSpec(use_case_id="targeted-advertisement", rows=500, variant="scoring"))
    advice = advise(context_for(frame, mode=RunMode.SCORE))
    assert not any(result.tool == "check_data" for result in advice.tool_results)
    assert all(p.kind is not ProposalKind.SETTING for p in advice.proposals)


def test_the_golden_file_is_readable_json(repo_root: Path) -> None:
    assert (repo_root / "tests/fixtures/agent_bench/expected.json").read_text(encoding="utf-8").endswith("\n")


def test_a_multi_select_setting_is_checked_item_by_item() -> None:
    from engine.agent.recommend import setting_allowed, settings_fields
    from engine.config import advanced_settings_schema, load_use_case

    fields = settings_fields(advanced_settings_schema(load_use_case("targeted-advertisement")))
    assert setting_allowed("model_search.candidates", ["XGBoost", "LightGBM"], fields)
    assert not setting_allowed("model_search.candidates", ["XGBoost", "MagicNet"], fields)
    assert not setting_allowed("model_search.candidates", [], fields)
    assert setting_allowed("model_search.strategy", "fast", fields)
    assert not setting_allowed("model_search.strategy", "reckless", fields)
    assert not setting_allowed("governance.approval_required", False, fields)
