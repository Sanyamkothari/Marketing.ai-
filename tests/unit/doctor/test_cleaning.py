"""The Data Doctor cleans with no AI: a small messy file gets its fixes as items with counts after TREAT, and a
column of rupees and dollars stops with a question instead of being merged."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from pydantic import JsonValue

from engine.agent.advisor import Advice
from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind, SessionStatus, ToolResult
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.case import Case, CaseItem
from engine.doctor.cleaning import _leak_items, _locale_flag, cleaning_detector
from engine.doctor.diagnose import Offer
from engine.doctor.indian_values import LocaleStepProposal
from engine.doctor.prescription import prescription_from_case
from engine.doctor.protocol import Ask
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


def _advice_with_leaks(*columns: str) -> Advice:
    checks: list[JsonValue] = [{"code": "LEAKAGE_SUSPECTED", "column": c} for c in columns]
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


def test_a_suspected_leak_column_becomes_an_unticked_drop_item() -> None:
    (item,) = _leak_items(_advice_with_leaks("refund_issued"), "u1", set(), frozenset(), NOW)
    step = RecipeStep.model_validate(item.payload["step"])
    assert (step.kind, step.column) == (RecipeStepKind.DROP_COLUMN, "refund_issued")
    assert "Remove 'refund_issued'" in item.title and item.state is ProposalState.PENDING


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


# ---------------------------------------------------------------------------
# DD/MM dates in the last-contact column
# ---------------------------------------------------------------------------
def _step_column(item: CaseItem) -> object:
    step = item.payload.get("step")
    return step.get("column") if isinstance(step, dict) else None


def _contact_dates(days: list[int], tmp_path: Path) -> tuple[list[CaseItem], list[Ask]]:
    """What the cleaning detector says about customers whose `last_contacted_at` is written `day/month/2026`."""
    config = load_use_case(vs.GOAL_A)
    frame = (
        vs.customers(rows=len(days))
        .drop(columns="account_no")
        .assign(last_contacted_at=[f"{day:02d}/{1 + i % 12:02d}/2026" for i, day in enumerate(days)])
    )
    frame.loc[0, "last_contacted_at"] = "00/00/2026"
    w = vs.World(
        files={vs.FIRST: vs.loaded(vs.FIRST, "c.csv", frame, config)},
        offers=(Offer(config=config, stage="Acquire"),),
    )
    storage = LocalStorage(tmp_path)
    deps = vs.deps_for(storage, w, detectors=(cleaning_detector(w.offers),))
    case = visit.resume(vs.CLIENT, vs.open_case(storage, w).case_id, deps, actor=vs.ANALYST)
    found = cleaning_detector(w.offers)(case, tuple(w.files.values()), None)
    return (
        [i for i in found.items if _step_column(i) == "last_contacted_at"],
        [a for a in found.asks if a.column == "last_contacted_at"],
    )


def test_a_last_contact_column_with_a_day_above_12_gets_a_day_first_date_fix_with_counts(
    tmp_path: Path,
) -> None:
    items, asks = _contact_dates([14, 3, 21, 5] * 50, tmp_path)
    assert not asks
    (item,) = items
    step = RecipeStep.model_validate(item.payload["step"])
    assert (step.kind, step.params["dayfirst"]) == (RecipeStepKind.PARSE_DATE, True)
    assert item.state is ProposalState.ACCEPTED and "day first" in item.title
    assert "199 of 200 values become dates, 1 could not be read" in step.reason


def test_a_last_contact_column_that_could_be_either_order_asks_with_an_example_both_ways(
    tmp_path: Path,
) -> None:
    items, asks = _contact_dates([5, 3, 8, 11] * 50, tmp_path)
    assert not items
    (ask,) = asks
    assert ask.code == "GUIDED_DATE_ORDER" and "day or the month" in ask.text
    assert "(day first)" in ask.text and "(month first)" in ask.text
