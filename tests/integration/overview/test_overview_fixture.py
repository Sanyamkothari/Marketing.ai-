"""The recorded `GET /overview` of the demo (Plan M M175) and the builder's own rules, without a server.

`tests/fixtures/ui_demo/api.json` is what the page tests draw from. Its `GET /overview` is assembled by
`tests/fixtures/build_overview_fixture.py` from the answers the same recording holds (the summary, the campaign list
and each campaign's plan), through `engine.measurement.overview.build_overview`, the function the route calls. These
tests keep the recording from drifting from the code, and pin the rules that need no data store:

* the headline prefers the rupee total, else the summary's first, else none;
* cards and live campaigns are capped, and the totals say how many there are;
* a campaign's status and next step follow its plan's analysis date and whether outcomes are in.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from engine.measurement.campaign import Campaign, CampaignStatus
from engine.measurement.overview import CAMPAIGNS_SHOWN, CARDS_SHOWN, build_overview, headline_total
from engine.measurement.plan import TestPlan
from engine.measurement.summary import CampaignSummary
from tests.fixtures.build_overview_fixture import KEY, RECORDING, answer_for, overview_from, render

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def recorded() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(RECORDING.read_text(encoding="utf-8"))
    return loaded


def test_the_recording_holds_the_overview_the_builder_gives(recorded: dict[str, Any]) -> None:
    """Re-run `python -m tests.fixtures.build_overview_fixture` after the summary or the campaigns are re-recorded."""
    assert KEY in recorded, "run python -m tests.fixtures.build_overview_fixture"
    assert recorded[KEY] == answer_for(recorded)


def test_the_recording_is_written_as_the_capture_script_writes_it(recorded: dict[str, Any]) -> None:
    assert render(recorded) == RECORDING.read_text(encoding="utf-8"), "sorted keys, one space of indent"


def test_the_demo_overview_is_what_its_own_sources_say(recorded: dict[str, Any]) -> None:
    body = recorded[KEY]["body"]
    summary = recorded["GET /campaigns/summary"]["body"]
    assert body["cards"] == summary["cards"][:CARDS_SHOWN]
    rupees = next(t for t in summary["proven"]["totals"] if t["unit"] == "rupees")
    assert body["headline"]["total"] == rupees["total"] and body["headline"]["label"] == rupees["label"]
    campaigns = recorded["GET /campaigns"]["body"]["campaigns"]
    assert all(c["status"] == "measured" for c in campaigns), "both demo campaigns are measured: none is live"
    assert body["campaigns"] == [] and body["campaigns_total"] == 0
    assert body["checklist"] == {"connected": True, "campaign": True, "results": True}


def _summary(recorded: dict[str, Any]) -> CampaignSummary:
    return CampaignSummary.model_validate(recorded["GET /campaigns/summary"]["body"])


def test_the_headline_prefers_rupees_then_the_first_total_then_none(recorded: dict[str, Any]) -> None:
    summary = _summary(recorded)
    assert [t.unit for t in summary.proven.totals] == [
        "amount:spend",
        "rupees",
        *[t.unit for t in summary.proven.totals[2:]],
    ]
    chosen = headline_total(summary)
    assert chosen is not None and chosen.unit == "rupees", "rupees even when another total comes first"
    no_rupees = summary.model_copy(
        update={
            "proven": summary.proven.model_copy(
                update={"totals": tuple(t for t in summary.proven.totals if t.unit != "rupees")}
            )
        }
    )
    first = headline_total(no_rupees)
    assert first is not None and first.unit == "amount:spend"
    nothing = summary.model_copy(update={"proven": summary.proven.model_copy(update={"totals": ()})})
    assert headline_total(nothing) is None
    built = build_overview(summary=nothing, campaigns=(), plan_of=lambda _id: None, connected=False, now=NOW)
    assert built.headline is None and built.summary_checked is True


def test_cards_are_capped_and_counted(recorded: dict[str, Any]) -> None:
    summary = _summary(recorded)
    many = summary.model_copy(
        update={"cards": tuple(summary.cards[0].model_copy() for _ in range(CARDS_SHOWN + 3))}
    )
    built = build_overview(summary=many, campaigns=(), plan_of=lambda _id: None, connected=False, now=NOW)
    assert len(built.cards) == CARDS_SHOWN and built.cards_total == CARDS_SHOWN + 3


def test_no_summary_means_no_cards_and_no_headline_but_the_rest_stands(recorded: dict[str, Any]) -> None:
    campaigns = tuple(Campaign.model_validate(c) for c in recorded["GET /campaigns"]["body"]["campaigns"])
    built = build_overview(
        summary=None, campaigns=campaigns, plan_of=lambda _id: None, connected=True, now=NOW
    )
    assert (built.summary_checked, built.cards, built.cards_total, built.headline) == (False, (), 0, None)
    assert built.checklist.campaign and built.checklist.results


def _live(recorded: dict[str, Any], n: int, *, outcomes: bool = False) -> Campaign:
    base = Campaign.model_validate(recorded["GET /campaigns"]["body"]["campaigns"][0])
    update: dict[str, Any] = {
        "campaign_id": f"c_20261011_{n:08x}",
        "name": f"Live {n}",
        "status": CampaignStatus.LIVE,
    }
    if not outcomes:
        update["outcomes"] = None
    return base.model_copy(update=update)


def _plan(recorded: dict[str, Any], day: date) -> TestPlan:
    plan = recorded["GET /campaigns/c_20261011_5ee8a5d4/plan"]["body"]["plan"]
    return TestPlan.model_validate({**plan, "analysis_date": day.isoformat()})


def test_status_and_next_step_follow_the_plan_date_and_the_outcomes(recorded: dict[str, Any]) -> None:
    today = NOW.date()
    plans = {
        "c_20261011_00000001": _plan(recorded, today + timedelta(days=1)),
        "c_20261011_00000002": _plan(recorded, today),
        "c_20261011_00000003": _plan(recorded, today - timedelta(days=5)),
        "c_20261011_00000004": None,
    }
    campaigns = (
        _live(recorded, 1),
        _live(recorded, 2),
        _live(recorded, 3, outcomes=True),
        _live(recorded, 4),
    )
    built = build_overview(
        summary=None, campaigns=campaigns, plan_of=lambda cid: plans.get(cid), connected=False, now=NOW
    )
    got = [(c.status, c.next_step, c.results_date) for c in built.campaigns]
    assert got == [
        ("live", "wait", today + timedelta(days=1)),
        ("waiting_for_results", "add_results", today),
        ("waiting_for_results", "measure", today - timedelta(days=5)),
        ("live", "plan", None),
    ]


def test_only_unmeasured_campaigns_are_listed_and_the_list_is_capped(recorded: dict[str, Any]) -> None:
    measured = Campaign.model_validate(recorded["GET /campaigns"]["body"]["campaigns"][0])
    campaigns = (measured, *[_live(recorded, n) for n in range(1, CAMPAIGNS_SHOWN + 3)])
    asked: list[str] = []

    def plan_of(campaign_id: str) -> TestPlan | None:
        asked.append(campaign_id)
        return None

    built = build_overview(summary=None, campaigns=campaigns, plan_of=plan_of, connected=False, now=NOW)
    assert [c.name for c in built.campaigns] == [f"Live {n}" for n in range(1, CAMPAIGNS_SHOWN + 1)]
    assert built.campaigns_total == CAMPAIGNS_SHOWN + 2
    assert len(asked) == CAMPAIGNS_SHOWN, "a plan is read only for a campaign that is listed"


def test_overview_from_is_deterministic(recorded: dict[str, Any]) -> None:
    assert overview_from(recorded) == overview_from(recorded)
