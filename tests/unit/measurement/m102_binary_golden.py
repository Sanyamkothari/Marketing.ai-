"""Binary measurements whose JSON was recorded before Plan J M102 changed anything (DEC-1312).

M102 adds continuous outcomes and the adjusted estimate to `measure_incrementality` and
`measure_campaign`. A binary measurement must come out exactly as it did: every existing field equal and
no new key in its JSON. `binary_reports()` builds four binary measurements from the simulator (fixed
seeds, so the same reports on every machine): the run's own path with immature rows, a campaign measured
against a registered plan (with a pre-registered covariate, which a yes/no outcome does not use), an
early look, and a campaign of two offers. `tests/fixtures/measurement/m102_binary_reports.json` holds
their JSON as the code wrote it on the commit before M102 (`computed_at` left out: it is the clock).

Re-record only if a binary report is meant to change, which M102 never does:
`python -c "from tests.unit.measurement.m102_binary_golden import record; record()"`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from engine.measurement.campaign import INTENDED_COLUMN, build_assignment
from engine.measurement.measure import measure_campaign
from engine.measurement.plan import TestPlanInput, freeze_plan, realised_population
from engine.measurement.simulate import KEY_COLUMN, multi_arm_campaign, population
from engine.uplift.contracts import IncrementalityReport
from engine.uplift.incrementality import measure_incrementality

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "measurement" / "m102_binary_reports.json"
REGISTERED = datetime(2026, 4, 1, tzinfo=UTC)


def _json(report: IncrementalityReport) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(report.model_dump_json())
    payload.pop("computed_at")
    return payload


def binary_reports() -> dict[str, dict[str, Any]]:
    """The four binary measurements, as JSON objects without `computed_at`."""
    run = population(3_000, 0.06, 0.02, immature_share=0.2, seed=102_001)
    plain = measure_incrementality(run.scores, run.outcomes, **run.measure_kwargs)

    campaign = population(3_000, 0.10, 0.03, seed=102_002)
    assignment = build_assignment(campaign.scores, primary_key=KEY_COLUMN)
    decided = TestPlanInput(
        metric="converted within 30 days",
        outcome_column="converted",
        outcome_window_days=30,
        analysis_date=date(2026, 6, 1),
        covariate_column="tenure_months",
        mde_pp=2.0,
        base_rate=0.1,
    )
    plan = freeze_plan(
        decided,
        realised_population(assignment, intended_column=INTENDED_COLUMN),
        campaign_id="c_m102",
        registered_by="u_1",
        registered_at=REGISTERED,
    )
    planned = measure_campaign(
        assignment,
        campaign.outcomes,
        intended_column=INTENDED_COLUMN,
        plan=plan,
        covariate_column="tenure_months",
        campaign_id="c_m102",
        **campaign.measure_kwargs,
    )
    early_plan = plan.model_copy(update={"analysis_date": date(2026, 7, 31)})
    early = measure_campaign(
        assignment,
        campaign.outcomes,
        intended_column=INTENDED_COLUMN,
        plan=early_plan,
        covariate_column="tenure_months",
        campaign_id="c_m102",
        **campaign.measure_kwargs,
    )
    offers = multi_arm_campaign(3_000, 0.05, (0.02, 0.04), seed=102_003)
    arms = measure_campaign(offers.scores, offers.outcomes, **offers.measure_kwargs)
    return {
        "run": _json(plain),
        "planned": _json(planned),
        "early_look": _json(early),
        "offers": _json(arms),
        "plan": json.loads(plan.model_dump_json()),
    }


def record() -> None:
    """Write the golden file from the code as it is now (used once, on the commit before M102)."""
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(binary_reports(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
