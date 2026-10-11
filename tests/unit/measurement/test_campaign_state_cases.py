"""The one campaign status rule, pinned to one fixture for both of its implementations (Plan M DEC-1606 (b)).

`engine.measurement.overview.campaign_state` (the Overview, M175) and `ui/modules/campaigns/status.js`
`campaignStatus` (the Campaigns list and page, M176) must give the same status and results date for every case in
`tests/fixtures/campaigns/status_cases.json`; M176's node suite (`rules.test.mjs`) runs the same cases on the JS
side, so a change to either rule that the other does not follow fails one of the two.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from engine.measurement.campaign import Campaign, CampaignKind, CampaignStatus
from engine.measurement.overview import campaign_state, results_date
from engine.measurement.plan import TestPlan

CASES_FILE: Final[Path] = Path(__file__).resolve().parents[2] / "fixtures" / "campaigns" / "status_cases.json"
CASES: Final[list[dict[str, Any]]] = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]


def _campaign(fields: dict[str, Any]) -> Campaign:
    """A record with only the fields the rule reads (as the fixture gives them); nothing else is needed."""
    return Campaign.model_construct(
        kind=CampaignKind(fields["kind"]),
        status=CampaignStatus(fields["status"]),
        treatment_start=datetime.fromisoformat(fields["treatment_start"]),
        outcome_window_days=fields["outcome_window_days"],
        outcomes=fields["outcomes"],
        test_plan_hash=fields["test_plan_hash"],
        measured_at=None,
    )


def _plan(fields: dict[str, Any] | None) -> TestPlan | None:
    if fields is None:
        return None
    return TestPlan.model_construct(analysis_date=date.fromisoformat(fields["analysis_date"]))


def test_the_fixture_has_every_status() -> None:
    assert len(CASES) >= 10
    assert {case["status"] for case in CASES} == {"draft", "live", "waiting_for_results", "measured"}


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_campaign_state_gives_the_shared_case(case: dict[str, Any]) -> None:
    campaign, plan = _campaign(case["campaign"]), _plan(case["plan"])
    status, _step, when = campaign_state(
        campaign, plan, date.fromisoformat(case["today"]), report=case["report"] is not None
    )
    expected = None if case["results_date"] is None else date.fromisoformat(case["results_date"])
    assert status == case["status"]
    assert when == expected
    assert results_date(campaign, plan) == expected


def test_a_stored_report_is_read_from_the_record_when_not_given() -> None:
    fields = next(case["campaign"] for case in CASES if case["status"] == "draft")
    draft = _campaign(fields)
    today = date.fromisoformat("2026-10-02")
    assert campaign_state(draft, None, today)[0] == "draft"
    early = draft.model_copy(update={"measured_at": datetime.fromisoformat("2026-10-02T09:00:00+00:00")})
    assert campaign_state(early, None, today)[0] == "live", "a stored report (an early look) is not a Draft"
