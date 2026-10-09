"""Plan J M104 (DEC-1314): the Value Proof Pack of a scored run that chose the offer per customer (M100 part B).

The run is the real one of `tests/integration/decide/test_offer_choice_run.py` (a model of two offers trained
and scored through the API, reused by name). The campaign is `POST /campaigns` on it, so its population is the
offer choice's own (DEC-1311 (ai)); its outcomes are drawn from the population's planted truth for the offer each
customer was actually given. The pack reads each offer within the customers the policy gave it (held back or
not, `policy_offer_label`), and every number is traced.
"""

# ruff: noqa: F811, F401 - the imported pytest fixtures are used by name
from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tests.integration.decide.test_offer_choice_run import App, Runs, app, make_root, score_twice
from tests.integration.measurement.support import ok, upload
from tests.integration.pilot.test_proof_pack import _section, assert_no_stray_digits, assert_traced
from tests.integration.uplift.test_uplift_api import run_artefact

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def root(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_root(config_root, tmp_path_factory.mktemp("proof-offer-root") / "configs")


@pytest.fixture(scope="module")
def runs(app: App) -> Runs:
    return score_twice(app, "0e1400")


def _outcomes(app: App, runs: Runs) -> pd.DataFrame:
    """Who converted: the planted base rate, plus the effect of the offer the run actually gave."""
    choice = pd.read_parquet(io.BytesIO(run_artefact(app, runs.open.run_id, "offer_choice.parquet")))
    frame = runs.campaign.frame
    given = (
        choice.astype({"customer_id": str})
        .set_index("customer_id")["offer_arm"]
        .reindex(frame["customer_id"])
    )
    arm = given.fillna(0).astype(int).to_numpy()
    dogs = runs.campaign.segment == "sleeping_dogs"
    base = np.where(dogs, 0.35, 0.15) + 0.03 * (frame["tenure_months"].to_numpy() / 72.0)
    lift = np.where(arm > 0, runs.campaign.tau[np.arange(len(arm)), np.maximum(arm - 1, 0)], 0.0)
    rng = np.random.default_rng(104)
    converted = rng.random(len(arm)) < np.clip(base + lift, 0.0, 1.0)
    return pd.DataFrame({"customer_id": frame["customer_id"], "converted": converted.astype(int)})


def test_each_offer_is_read_within_its_own_customers_and_every_number_is_traced(app: App, runs: Runs) -> None:
    client = app.client
    # The run finished moments ago, so the outcome window is closed by asking for none (every outcome is final).
    created = ok(client.post("/campaigns", json={"run_id": runs.open.run_id, "outcome_window_days": 0}), 201)
    campaign_id = created["campaign"]["campaign_id"]
    assert created["campaign"]["intended_source"] == "offer_choice"
    upload_id = upload(client, _outcomes(app, runs))
    ok(
        client.post(
            f"/campaigns/{campaign_id}/outcomes", json={"upload_id": upload_id, "outcome_column": "converted"}
        )
    )
    ok(client.post(f"/campaigns/{campaign_id}/measure", json={}))
    groups = json.loads(app.storage.read_bytes(f"campaigns/{campaign_id}/segment_effects.json"))
    offers = [cell for cell in groups["cells"] if cell["dimension"] == "offer"]
    assert len(offers) == 2 and {cell["comparison"] for cell in offers} == {"within_group"}
    assert all(cell["treated_rows"] > 50 and cell["control_rows"] > 50 for cell in offers)
    view = ok(client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"}))
    table = _section(view, "incremental")["table"]
    assert table is not None and len(table["rows"]) == 2
    assert_traced(app.data_dir, view)
    assert_no_stray_digits(client, campaign_id, view)
