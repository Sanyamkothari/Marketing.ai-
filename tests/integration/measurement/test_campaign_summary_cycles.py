"""Plan J M105 (DEC-1315) through the API: the "effect fading" card.

Each cycle is a scored campaign built by the real code (Phase 1 scoring stages, `POST /campaigns`, an outcomes
upload, `POST /campaigns/{id}/measure`) with the effect planted in its outcomes. A use case's cycles are read in
the order they went out, each effect with the noise its own stored range shows; the card appears only when the
trend over at least three measured cycles lies wholly below zero, and:

* a series whose effect falls cycle by cycle is flagged, naming the reports it read;
* a series whose effect never moves is not (the rate at which noise alone would flag one is checked in
  `tests/statistical/test_fading_false_alarm.py`), and neither is a rising one;
* two cycles are too few to call a trend, however steep;
* a cycle that is only an early look does not count;
* campaigns that measure the same customers are one cycle, not several: repeating a cycle's campaign cannot turn a
  series that does not fall into one that does.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.measurement.simulate import segment_outcomes
from engine.pilot.plain import jargon_in
from engine.storage import LocalStorage
from tests.integration.measurement.support import SENT, ok, propensity_run
from tests.integration.measurement.test_campaign_summary import POWERED, _add_outcomes, _measure, _plan
from tests.integration.pilot.test_proof_pack import assert_traced

pytestmark = pytest.mark.integration


class Series(Protocol):
    def __call__(
        self,
        effects: list[float],
        early: frozenset[int] = frozenset(),
        repeats: dict[int, int] | None = None,
    ) -> tuple[TestClient, LocalStorage, Path, list[str]]: ...


@pytest.fixture
def series(config_root: Path, tmp_path: Path) -> Iterator[Series]:
    clients: list[TestClient] = []

    def build(
        effects: list[float],
        early: frozenset[int] = frozenset(),
        repeats: dict[int, int] | None = None,
    ) -> tuple[TestClient, LocalStorage, Path, list[str]]:
        """One campaign per cycle, each on its own scoring run. `early`: cycles whose plan fixes a day still to
        come, so they are measured early. `repeats`: extra campaigns on the same run and outcomes as a cycle.
        """
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
            for repeat in range((repeats or {}).get(index, 0) + 1):
                created = ok(
                    client.post(
                        "/campaigns",
                        json={
                            "run_id": run.run_id,
                            "name": f"Cycle {index + 1}" + ("" if repeat == 0 else f" again {repeat}"),
                            "treatment_start": (SENT + timedelta(days=7 * index)).isoformat(),
                        },
                    ),
                    201,
                )
                campaign_id = str(created["campaign"]["campaign_id"])
                if index in early:
                    later = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
                    _plan(client, campaign_id, later, **POWERED)
                _add_outcomes(client, campaign_id, outcomes)
                _measure(client, campaign_id)
                if repeat == 0:
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
    """Four cycles fall; the second was read before its planned day, so three are left and the card names those."""
    client, _, _, ids = series([0.12, 0.08, 0.04, 0.0], early=frozenset({1}))
    excluded = {
        note["campaign_id"]: note for note in ok(client.get("/campaigns/summary"))["proven"]["excluded"]
    }
    assert excluded[ids[1]]["code"] == "PROOF_NOT_MATURE", "the early look is not a final result"
    cards = _fading(client)
    assert len(cards) == 1
    assert cards[0]["read"] == [
        f"campaigns/{campaign_id}/incrementality_report.json" for campaign_id in (ids[0], ids[2], ids[3])
    ], "the other three cycles are still read, and the early look is not one of them"


def test_a_cycle_measured_by_several_campaigns_counts_once(series: Series) -> None:
    """The same customers' outcome repeated is not independent evidence: it cannot make a flat series fall."""
    effects = REPEATED_EFFECTS
    client, _, data_dir, _ = series(effects, repeats={0: 2, 1: 1})
    assert (
        _fading(client) == []
    ), "three cycles that do not fall by more than noise, the first said three times and the second twice"
    summary = ok(client.get("/campaigns/summary"))
    apart = [line for line in summary["proven"]["apart"] if line["kind"] == "same_customers"]
    assert len(apart) == 3, "two repeats of the first cycle and one of the second are listed apart"
    counted = {line["counted_as_id"] for line in apart}
    assert len(counted) == 2 and counted.isdisjoint(line["campaign_id"] for line in apart)
    totals = {total["unit"]: total for total in summary["proven"]["totals"]}
    assert len(totals["outcomes:converted"]["campaigns"]) == len(effects), "one lower bound per cycle"
    assert_traced(data_dir, summary)


REPEATED_EFFECTS = [0.12, 0.08, 0.06]
"""Three cycles whose measured trend is inside the noise (about 1.4 of the 1.96 it would need). Counting the first
cycle three times and the second twice, each repeat taken for a cycle of its own, makes it about 2.5: fading."""
