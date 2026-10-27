"""The production API never says a brief's follow-up steps are sent, v3 on or off (docs/v3/HONEST.md change 7).

Nothing reads `Brief.follow_ups` to send: the plain agent decides once per customer, and the v3 journeys run the
steps of configs/v3/journeys.yaml. So `PlanFacts.follow_ups_run` (from `Services.follow_ups_run`) is False in every
wiring, and the Plan screen lists no follow-up as something that will happen.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize("v3", ["false", "true"])
def test_the_production_services_do_not_claim_follow_ups_are_sent(tmp_path: Path, v3: str) -> None:
    from api.v2.app import build_production

    env = {
        "AUTONOMY_DATABASE_URL": f"sqlite:///{tmp_path / 'autonomy.db'}",
        "AUTONOMY_BUSINESS_ID": "biz_1",
        "AUTONOMY_V3": v3,
    }
    services, _connectors = build_production(env)
    assert services.follow_ups_run is False
