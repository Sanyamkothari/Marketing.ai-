"""The Data Doctor cleans with no AI: a small messy file gets its fixes as items with counts after TREAT, and a
column of rupees and dollars stops with a question instead of being merged."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
from pydantic import JsonValue

from engine.agent.advisor import Advice
from engine.agent.contracts import (
    AgentConfidence,
    ProposalState,
    RecipeStep,
    RecipeStepKind,
    SessionStatus,
    ToolResult,
)
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.case import Case
from engine.doctor.cleaning import _leak_items, _locale_flag, cleaning_detector
from engine.doctor.diagnose import Offer
from engine.doctor.indian_values import LocaleStepProposal
from engine.doctor.prescription import prescription_from_case
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs

ROWS = 600


def _messy() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    return pd.DataFrame(
        {
            "customer_id": [f"C{1000 + i}" for i in range(ROWS)],
            "spend": [f"₹{1200 + 3 * i:,}" for i in range(ROWS)],
            "joined": [f"{1 + i % 28:02d}/{1 + i % 12:02d}/2025" for i in range(ROWS)],
            "opted_in": rng.choice(["haan", "nahi"], ROWS),
            "deal_size": ["2 lakh" if i % 3 == 0 else str(50_000 + i) for i in range(ROWS)],
            "price": ["₹500" if i % 2 else "$20" for i in range(ROWS)],
            "converted": rng.integers(0, 2, ROWS),
        }
    )


def _treat(tmp_path: object) -> tuple[Case, vs.World, LocalStorage]:
    config = load_use_case(vs.GOAL_A)
    w = vs.World(
        files={vs.FIRST: vs.loaded(vs.FIRST, "messy.csv", _messy(), config)},
        offers=(Offer(config=config, stage="Acquire"),),
    )
    storage = LocalStorage(tmp_path)  # type: ignore[arg-type]
    deps = vs.deps_for(storage, w, detectors=(cleaning_detector(w.offers),))
    case = vs.open_case(storage, w)
    return visit.resume(vs.CLIENT, case.case_id, deps, actor=vs.ANALYST), w, storage


def _fixes(case: Case) -> dict[tuple[str, RecipeStepKind], tuple[ProposalState, str]]:
    found = {}
    for item in case.items:
        step = item.payload.get("step")
        if item.kind == "recipe_step" and isinstance(step, dict):
            parsed = RecipeStep.model_validate(step)
            found[(parsed.column, parsed.kind)] = (item.state, item.title)
    return found


def test_treat_proposes_the_fixes_with_counts_and_stops_on_two_currencies(tmp_path: object) -> None:
    case, w, storage = _treat(tmp_path)
    fixes = _fixes(case)

    # Money as text and day/month dates: Guided setup's fixes, sure, so ticked.
    state, title = fixes[("spend", RecipeStepKind.PARSE_NUMBER)]
    assert state is ProposalState.ACCEPTED and f"{ROWS} of {ROWS}" in title
    assert fixes[("joined", RecipeStepKind.PARSE_DATE)][0] is ProposalState.ACCEPTED
    # haan / nahi and lakh: the Indian checks, one fix per column and kind (never a second one from the advisor).
    state, title = fixes[("opted_in", RecipeStepKind.MAP_BOOLEAN)]
    assert state is ProposalState.ACCEPTED and f"{ROWS:,} of {ROWS:,} values read" in title
    assert f"{ROWS:,} of {ROWS:,} values read" in fixes[("deal_size", RecipeStepKind.PARSE_NUMBER)][1]
    assert len(fixes) == len([i for i in case.items if i.kind == "recipe_step"])

    # Rupees and dollars in one column: a blocking question, no fix on it, nothing merged.
    assert not [key for key in fixes if key[0] == "price"]
    stop = next(q for q in case.questions if q.code == "CURRENCY_MIXED")
    assert stop.blocking and stop.answer is None and "price" in stop.text
    assert case.state.value == "ask"

    # The ticked fixes are what the prescription applies on approval (the prescription reads the items' steps).
    prescription = prescription_from_case(case, storage=storage, files=tuple(w.files.values()))
    applied = {(step.column, step.kind) for recipe in prescription.recipes() for step in recipe.steps}
    assert applied == {key for key, (state, _) in fixes.items() if state is ProposalState.ACCEPTED}


# ---------------------------------------------------------------------------
# C1: leak columns become drop items; a locale finding with no step is a flag, not silence
# ---------------------------------------------------------------------------
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _advice_with_leaks(*columns: str, severity: str = "warning") -> Advice:
    checks: list[JsonValue] = [
        {"code": "LEAKAGE_SUSPECTED", "column": c, "severity": severity} for c in columns
    ]
    result = ToolResult(evidence_id="e1", tool="check_data", result={"checks": checks}, created_at=NOW)
    return Advice(
        status=SessionStatus.READY,
        tool_results=(result,),
        proposals=(),
        questions=(),
        assumptions=(),
        engine_hidden=(),
        stop_reason=None,
        primary_key=None,
        target=None,
    )


def test_a_leak_only_suspected_becomes_an_unticked_drop_item() -> None:
    (item,) = _leak_items(_advice_with_leaks("refund_issued"), "u1", set(), frozenset(), NOW)
    step = RecipeStep.model_validate(item.payload["step"])
    assert (step.kind, step.column) == (RecipeStepKind.DROP_COLUMN, "refund_issued")
    assert "Remove 'refund_issued'" in item.title and item.state is ProposalState.PENDING


def test_a_leak_the_check_proves_is_a_ticked_default_the_person_can_untick() -> None:
    (item,) = _leak_items(
        _advice_with_leaks("refund_issued", severity="error"), "u1", set(), frozenset(), NOW
    )
    assert item.state is ProposalState.ACCEPTED and item.confidence is AgentConfidence.SURE
    (never,) = _leak_items(
        _advice_with_leaks("refund_issued", severity="error"),
        "u1",
        set(),
        frozenset({RecipeStepKind.DROP_COLUMN}),
        NOW,
    )
    assert never.state is ProposalState.PENDING, "a kind the policy never ticks stays unticked"


def test_a_proven_leak_the_person_already_kept_is_not_pre_ticked() -> None:
    advice = _advice_with_leaks("refund_issued", severity="error")
    check = advice.tool_results[0].result["checks"][0]  # type: ignore[index]
    check["acknowledged"] = True  # type: ignore[index]
    (item,) = _leak_items(advice, "u1", set(), frozenset(), NOW)
    assert item.state is ProposalState.PENDING


def test_an_id_named_or_skipped_leak_column_is_not_proposed_for_removal() -> None:
    advice = _advice_with_leaks("customer_id", "converted", "refund_issued")
    items = _leak_items(advice, "u1", {"converted"}, frozenset(), NOW)
    assert [RecipeStep.model_validate(i.payload["step"]).column for i in items] == ["refund_issued"]


def test_a_locale_finding_with_no_step_is_a_flag_naming_the_column() -> None:
    found = LocaleStepProposal(
        tool="indian_booleans",
        column="consent",
        step=None,
        confidence="check",
        non_empty=10,
        convertible=0,
        failed=0,
        reason="only one side is written",
        unknown_words=3,
    )
    item = _locale_flag(found, "u1", NOW)
    assert item.payload["code"] == "LOCALE_UNREAD" and item.payload["level"] == "flag"
    assert item.payload["column"] == "consent" and "'consent' is left as it is" in item.title
