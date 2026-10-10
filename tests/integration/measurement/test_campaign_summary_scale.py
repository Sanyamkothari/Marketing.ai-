"""Plan J M105 (DEC-1315): "value proven to date" is the sum over every campaign, not the newest page of them.

The Results list shows the newest hundred campaigns; a total that read only those would lose older proven
campaigns as new ones were made, and fall from one day to the next while still saying "to date". Here one
measured campaign (built by the real code) is followed by more campaigns than the list shows; the total must
still hold it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes.campaigns import CAMPAIGNS_SHOWN
from engine.measurement.campaign import CampaignStore
from engine.storage import LocalStorage
from engine.utils.time import utc_now
from tests.integration.measurement.support import ok, propensity_run
from tests.integration.measurement.test_campaign_summary import _scored
from tests.integration.pilot.test_proof_pack import _banded_outcomes

pytestmark = pytest.mark.integration

RUN = "r_20261012_10520001"


def test_a_proven_campaign_beyond_the_newest_page_is_still_in_the_total(
    config_root: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    run = propensity_run(storage, RUN, rows=6_000)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        proven = _scored(client, RUN, "The oldest", _banded_outcomes(run.scores, seed=21))
        before = ok(client.get("/campaigns/summary"))
        total = before["proven"]["totals"][0]
        assert [line["campaign_id"] for line in total["campaigns"]] == [proven]

        # More campaigns than the list shows, all newer: records only (their files are gone, as after the
        # retention job), so they read nothing but they do push the proven one past the newest page.
        store: CampaignStore = client.app.state.campaign_store  # type: ignore[attr-defined]
        original = store.get(proven)
        assert original is not None
        for number in range(CAMPAIGNS_SHOWN + 5):
            store.create(
                original.model_copy(
                    update={
                        "campaign_id": f"c_20261013_{number:08x}",
                        "created_at": utc_now() + timedelta(minutes=number + 1),
                    }
                )
            )
        assert proven not in {c.campaign_id for c in store.list(limit=CAMPAIGNS_SHOWN)}, "past the page"
        assert len(client.get("/campaigns").json()["campaigns"]) == CAMPAIGNS_SHOWN

        after = ok(client.get("/campaigns/summary"))
    again = {t["unit"]: t for t in after["proven"]["totals"]}[total["unit"]]
    assert [line["campaign_id"] for line in again["campaigns"]] == [proven], "still counted: it is to date"
    assert again["total"]["value"] == pytest.approx(total["total"]["value"])
