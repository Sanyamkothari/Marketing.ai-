"""Plan J M104 (DEC-1314): the Value Proof Pack of a programme readout (M103's `POST /campaigns/programme`).

The universal holdout is the one a real Phase 1 scoring pass recorded (the world of
`tests/integration/measurement/test_programme_readout.py`, reused by name). A programme is engine-random, so
its pack is proven; it compares everyone with the universal control group, so it has no groups of customers to
read one by one and its backfire section says so instead of inventing one.
"""

# ruff: noqa: F811, F401 - the imported pytest fixture is used by name
from __future__ import annotations

import json

import pytest

from tests.integration.measurement.support import ok
from tests.integration.measurement.test_programme_readout import World, _body, _outcomes, world
from tests.integration.pilot.test_proof_pack import _section, assert_traced, page_text

pytestmark = pytest.mark.integration


def test_a_programme_readouts_pack_is_proven_traced_and_has_no_groups(world: World) -> None:
    outcomes, _ = _outcomes(world, seed=104)
    campaign_id = ok(world.client.post("/campaigns/programme", json=_body(world, outcomes)), 201)["campaign"][
        "campaign_id"
    ]
    groups = json.loads(world.storage.read_bytes(f"campaigns/{campaign_id}/segment_effects.json"))
    assert groups["cells"] == [] and groups["family_size"] == 0
    view = ok(world.client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"}))
    assert view["kind"] == "programme" and view["claim"] == "proven"
    backfire = _section(view, "backfire")
    assert backfire["status"] == "not_measured"
    assert "has no groups of customers to read one by one" in backfire["reason"]
    assert _section(view, "plan")["status"] == "not_measured"
    assert "programme readout covers a period" in _section(view, "plan")["reason"]
    assert_traced(world.data_dir, view)
    page = page_text(world.client.get(f"/pilot/proof/{campaign_id}").text)
    assert "the effect of the whole programme" in page
