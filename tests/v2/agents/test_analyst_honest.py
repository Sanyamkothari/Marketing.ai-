"""The Analyst's money section states only what the proof measured (docs/v3/HONEST.md changes 3 and 4).

- No return-on-spend multiple: no "Return on spend" sentence and no `roi`, `roi_low`, `roi_high` numbers, even when
  `ProofSummary.roi` is set (it divides profit by the cost the proof managed to charge).
- No "budget left": the proof's spend is the agent group's closed contacts, not what the budget limit counts (the
  runtime's ledger, shown on Live with what is left), so a briefing line on Live never states a second "left" figure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from autonomy.agents.analyst import template_briefing
from autonomy.contracts import Estimate, HealthStatus, ProofSummary
from autonomy.fakes.samples import sample_goal

NOW = datetime(2026, 10, 20, 7, tzinfo=UTC)
MOCK = Path(__file__).resolve().parents[3] / "ui" / "v2" / "mock"
HEALTH = HealthStatus(as_of=NOW, delivery_rate=0.99, complaint_rate=0.0, unsubscribe_rate=0.001)


def _proof() -> ProofSummary:
    return ProofSummary(
        goal_id="goal_winback",
        as_of=NOW,
        agent_n=8000,
        baseline_n=1000,
        holdout_n=1000,
        incremental_revenue=Estimate(point=14200.0, low=6100.0, high=22300.0),
        incremental_profit=Estimate(point=4300.0, low=900.0, high=7700.0),
        lift_vs_baseline=None,
        roi=Estimate(point=111.6, low=20.0, high=200.0),
        spend=17.25,
    )


def _money() -> tuple[str, dict[str, float]]:
    briefing = template_briefing(sample_goal(NOW), _proof(), HEALTH, {}, "daily", NOW)
    section = briefing.sections[0]
    return section.body, dict(section.numbers)


def test_the_money_section_has_no_return_on_spend_multiple() -> None:
    body, numbers = _money()
    assert "Return on spend" not in body and "111.6" not in body and "×" not in body
    assert not {"roi", "roi_low", "roi_high"} & set(numbers)


def test_the_money_section_states_the_proofs_spend_and_no_budget_left() -> None:
    body, numbers = _money()
    assert numbers["spend"] == 17.25
    assert "$17.25" in body and "agent group" in body
    assert "budget_left" not in numbers and "budget" not in numbers
    assert "leaving" not in body and " left" not in body


def test_the_sample_briefing_and_the_live_tile_do_not_state_two_budget_left_figures() -> None:
    """Live shows `today.budget_left` (the ledger's); the sample briefing on the same page states none of its own."""
    today = json.loads((MOCK / "today.json").read_text(encoding="utf-8"))
    briefing = json.loads((MOCK / "briefing.json").read_text(encoding="utf-8"))
    assert today["budget_left"] is not None
    for section in briefing["sections"]:
        assert "budget_left" not in section["numbers"]
        assert "leaving" not in section["body"]
