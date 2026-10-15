"""The plain results summary's server side: the first customers of the list, and the case a run came from.

Nothing here trains a model: the list is a small CSV in the shape `engine.decide.treat_list` writes (best first),
and the campaign runs are records written straight to storage.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.campaign_results import top_customers
from engine.campaign_run import CampaignRunStore
from engine.storage import LocalStorage
from tests.fixtures.node import skip_without_jsdom

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"

LIST = (
    b"customerID,use_case,model_version,band,treat,holdout,explore,suppression_reason,offer,channel,score,reason_1\n"
    b"C7,telco-churn,m1,High,1,0,0,,Call back,phone,0.91,Short contract\n"
    b"C2,telco-churn,m1,High,0,1,0,,,,0.88,\n"  # best-first, but held back as the comparison group
    b"C9,telco-churn,m1,Medium,True,0,0,,Email,email,0.70,\n"
    b"C4,telco-churn,m1,Low,1,0,0,,,,,\n"  # no score
    b"C5,telco-churn,m1,Low,1,0,0,,,,0.10,\n"
)


def test_the_first_customers_are_the_ones_to_contact_in_the_lists_own_order() -> None:
    rows = top_customers(LIST, limit=3)
    assert [r.customer for r in rows] == ["C7", "C9", "C4"]  # C2 is held back, so it is not "to contact"
    assert rows[0].score == pytest.approx(0.91)
    assert (rows[0].priority, rows[0].offer, rows[0].channel, rows[0].reason) == (
        "High",
        "Call back",
        "phone",
        "Short contract",
    )
    assert rows[2].score is None and rows[2].offer is None  # nothing is made up for a missing value


def test_a_list_with_nobody_to_contact_or_no_limit_gives_nothing() -> None:
    nobody = b"customerID,use_case,treat,score\nC1,x,0,0.5\n"
    assert top_customers(nobody) == []
    assert top_customers(LIST, limit=0) == []


def test_two_key_columns_make_one_customer_name() -> None:
    two = b"region,customerID,use_case,treat,score\nN,C1,x,1,0.5\n"
    assert top_customers(two)[0].customer == "N / C1"


@pytest.fixture
def served(config_root: Path, tmp_path: Path) -> Iterator[tuple[TestClient, CampaignRunStore]]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path)) as client:
        yield client, CampaignRunStore(LocalStorage(tmp_path))


def test_a_run_remembers_its_doctor_case_and_can_be_found_by_its_list(
    served: tuple[TestClient, CampaignRunStore],
) -> None:
    client, store = served
    mine = store.create(use_case_id="telco-churn", upload_id="u_1", case_id="case_42")
    store.save(mine.model_copy(update={"score_run_id": "r_score_1"}))
    store.create(use_case_id="telco-churn", upload_id="u_2")  # another run, with no list yet
    found = client.get("/campaign-runs", params={"score_run_id": "r_score_1"}).json()["campaign_runs"]
    assert [(v["record"]["campaign_run_id"], v["record"]["case_id"]) for v in found] == [
        (mine.campaign_run_id, "case_42")
    ]
    assert client.get("/campaign-runs", params={"score_run_id": "r_nobody"}).json()["campaign_runs"] == []


def test_the_first_customers_say_why_when_there_is_no_list_yet(
    served: tuple[TestClient, CampaignRunStore],
) -> None:
    client, store = served
    waiting = store.create(use_case_id="telco-churn", upload_id="u_1")
    answer = client.get(f"/campaign-runs/{waiting.campaign_run_id}/top-customers")
    assert answer.status_code == 200
    assert answer.json()["customers"] == []
    assert "not built yet" in answer.json()["unavailable"]
    assert client.get("/campaign-runs/cr_missing/top-customers").status_code == 404


def test_the_results_summary_in_jsdom() -> None:
    """`production/ui/campaigns/summary.test.mjs` draws the summary, its table, the Doctor link and the folded details."""
    node = skip_without_jsdom(NODE_DIR)
    result = subprocess.run(
        [node, "--test", str(NODE_DIR / "campaigns" / "summary.test.mjs")],
        cwd=NODE_DIR,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout
