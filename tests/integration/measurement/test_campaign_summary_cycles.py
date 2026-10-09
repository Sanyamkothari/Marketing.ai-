"""Plan J M105 (DEC-1315) through the API: the "effect fading" card.

Each cycle is a scored campaign built by the real code (Phase 1 scoring stages, `POST /campaigns`, an outcomes
upload, `POST /campaigns/{id}/measure`) with the effect planted in its outcomes. A use case's cycles are read in
the order they went out, each effect with the noise its own stored range shows; the card appears only when the
trend over at least three measured cycles lies wholly below zero, and:

* a series whose effect falls cycle by cycle is flagged, naming the reports it read;
* a series whose effect never moves is not (the rate at which noise alone would flag one is checked in
  `tests/statistical/test_fading_false_alarm.py`), and neither is a rising one;
* two cycles are too few to call a trend, however steep;
* a cycle that is only an early look does not count.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.measurement.simulate import segment_outcomes
from engine.pilot.plain import jargon_in
from engine.storage import LocalStorage
from tests.integration.measurement.support import SENT, ok, propensity_run
from tests.integration.measurement.test_campaign_summary import _add_outcomes, _measure
from tests.integration.pilot.test_proof_pack import assert_traced

pytestmark = pytest.mark.integration

Series = Callable[[list[float]], tuple[TestClient, LocalStorage, Path, list[str]]]


@pytest.fixture
def series(config_root: Path, tmp_path: Path) -> Iterator[Series]:
    clients: list[TestClient] = []

    def build(effects: list[float]) -> tuple[TestClient, LocalStorage, Path, list[str]]:
        data_dir = tmp_path / f"data{len(clients)}"
        data_dir.mkdir()
        storage = LocalStorage(data_dir)
        runs = [
            propensity_run(storage, f"r_20261011_1051{index:04d}", rows=6_000, seed=40 + index)
            for index in range(len(effects))
        ]
        client = TestClient(create_app(config_root=config_root, data_dir=data_dir))
        client.__enter__()
        clients.append(client)
        ids: list[str] = []
        for index, (run, effect) in enumerate(zip(runs, effects, strict=True)):
            scores = run.scores
            contacted = scores["suppressed_reason"].isna().to_numpy() & ~scores["control_group"].to_numpy(
                dtype=bool
            )
            outcomes = segment_outcomes(
                scores["customer_id"],
                scores["band"],
                contacted,
                base_rate=0.15,
                effects={"Low": effect, "Medium": effect},
                seed=1050 + index,
            )
            created = ok(
                client.post(
                    "/campaigns",
                    json={
                        "run_id": run.run_id,
                        "name": f"Cycle {index + 1}",
                        "treatment_start": (SENT + timedelta(days=7 * index)).isoformat(),
                    },
                ),
                201,
            )
            campaign_id = str(created["campaign"]["campaign_id"])
            _add_outcomes(client, campaign_id, outcomes)
            _measure(client, campaign_id)
            ids.append(campaign_id)
        return client, storage, data_dir, ids

    yield build
    for client in clients:
        client.__exit__(None, None, None)


def _fading(client: TestClient) -> list[dict[str, Any]]:
    summary = ok(client.get("/campaigns/summary"))
    return [card for card in summary["cards"] if card["code"] == "EFFECT_FADING"]


def test_an_effect_that_falls_cycle_by_cycle_is_flagged(series: Series) -> None:
    client, _, data_dir, ids = series([0.12, 0.08, 0.04, 0.0])
    cards = _fading(client)
    assert len(cards) == 1
    card = cards[0]
    assert card["campaign_id"] == ids[-1], "the card is on the latest cycle"
    assert card["read"] == [f"campaigns/{campaign_id}/incrementality_report.json" for campaign_id in ids]
    first, latest = card["facts"]
    assert first["value"]["value"] > latest["value"]["value"]
    assert first["value"]["format"] == latest["value"]["format"] == "points"
    assert not jargon_in(card["title"]) and not jargon_in(card["text"]) and not jargon_in(card["next_step"])
    assert_traced(data_dir, ok(client.get("/campaigns/summary")))


@pytest.mark.parametrize(
    "effects",
    [
        pytest.param([0.06, 0.06, 0.06, 0.06], id="never moves"),
        pytest.param([0.0, 0.04, 0.08, 0.12], id="rises"),
        pytest.param([0.12, 0.0], id="two cycles only"),
    ],
)
def test_noise_a_rise_and_too_few_cycles_do_not_raise_it(series: Series, effects: list[float]) -> None:
    client, _, _, _ = series(effects)
    assert _fading(client) == []


def test_a_cycle_that_is_only_an_early_look_does_not_count(series: Series) -> None:
    """Four cycles fall; one of them is an early look, so three are left and the card names those three."""
    client, storage, _, ids = series([0.12, 0.08, 0.04, 0.0])
    assert len(_fading(client)) == 1
    report = f"campaigns/{ids[1]}/incrementality_report.json"
    original = storage.read_bytes(report)
    document = json.loads(original)
    document["early_look"] = True
    storage.write_bytes(report, json.dumps(document).encode())
    try:
        cards = _fading(client)
    finally:
        storage.write_bytes(report, original)
    assert all(report not in card["read"] for card in cards), "an early look is not one of the cycles"
