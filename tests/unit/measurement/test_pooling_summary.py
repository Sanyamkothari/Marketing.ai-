"""Plan K M136 (DEC-1402): the campaign summary's Estimated lines, their warning, and how stale files catch up.

Each campaign's report is measured by the real code (`measure_incrementality` on `engine.measurement.simulate`
campaigns) and stored where a measurement stores it; its record is the store's own model. The pooled files are
written by `refresh_pooled_estimates`, the function the measurement path calls, never by hand.

* a cell whose campaigns differ by no more than chance shows its Estimated lines with the `POOL_NO_SPREAD` warning
  and a plain sentence saying so (design §9.2: "full pooling is shown with a warning");
* a cell that does spread shows no warning;
* a file whose member was re-measured, or that was fitted under other settings, is shown as absent;
* with the campaign store, the summary brings stale use cases up to date: a campaign measured while pooling was off
  joins its cell, and changed settings are applied.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from engine.measurement.campaign import Campaign, CampaignStatus, InMemoryCampaignStore, campaign_key
from engine.measurement.pooling import (
    POOL_NO_SPREAD,
    POOL_TOO_FEW,
    POOLED_FILENAME,
    PoolingSettings,
    refresh_pooled_estimates,
)
from engine.measurement.simulate import population
from engine.measurement.summary import SummaryRules, _Builder, _estimated_lines
from engine.pilot.plain import jargon_in
from engine.pilot.proof import check_figures
from engine.storage import LocalStorage
from engine.uplift.incrementality import measure_incrementality
from tests.unit.measurement.test_campaign import campaign

NOW = datetime(2026, 10, 1, tzinfo=UTC)
FLAT = (0.02, 0.02, 0.02, 0.02)
"""Four campaigns with the same true effect: on these seeds REML finds no spread (asserted, not assumed)."""
SPREAD = (0.0, 0.06, 0.12, 0.18)


def _root(tmp_path: Path, text: str) -> Path:
    root = tmp_path / "configs"
    root.mkdir(exist_ok=True)
    (root / "measurement.yaml").write_text(text)
    return root


def _measured(
    storage: LocalStorage, store: InMemoryCampaignStore, index: int, effect: float, seed: int
) -> str:
    cid = f"c_20261001_{index:08x}"
    sim = population(6_000, 0.10, effect, seed=seed)
    report = measure_incrementality(sim.scores, sim.outcomes, **sim.measure_kwargs)
    record = campaign(cid, run_id=f"r_20261001_{index:08x}", minutes=index).model_copy(
        update={"status": CampaignStatus.MEASURED, "measured_at": NOW}
    )
    storage.write_model(campaign_key(cid, "incrementality_report.json"), report)
    storage.write_model(campaign_key(cid, "campaign.json"), record)
    store.create(record)
    return cid


def _world(
    tmp_path: Path, effects: tuple[float, ...], seed: int
) -> tuple[LocalStorage, InMemoryCampaignStore]:
    storage = LocalStorage(tmp_path / "data")
    store = InMemoryCampaignStore()
    for index, effect in enumerate(effects):
        _measured(storage, store, index, effect, seed + index)
    return storage, store


def _lines(
    storage: LocalStorage, store: InMemoryCampaignStore, root: Path, *, catch_up: bool
) -> dict[str, object]:
    builder = _Builder(storage, None, root, NOW, SummaryRules())
    campaigns = store.list(limit=None)
    names = {
        c.campaign_id: builder.reader.fig(campaign_key(c.campaign_id, "campaign.json"), "name", "text")
        for c in campaigns
    }
    assert all(name is not None for name in names.values())
    lines = _estimated_lines(builder, names, campaigns, root, store if catch_up else None, NOW)  # type: ignore[arg-type]
    assert lines is not None
    return {line.campaign_id: line for line in lines}


def _pooled(storage: LocalStorage, cid: str) -> dict[str, object]:
    return dict(json.loads(storage.read_bytes(campaign_key(cid, POOLED_FILENAME))))


def test_campaigns_that_differ_by_no_more_than_chance_carry_the_warning(tmp_path: Path) -> None:
    storage, store = _world(tmp_path, FLAT, seed=13_680)
    settings = PoolingSettings(enabled=True)
    written = refresh_pooled_estimates(storage, store, store.list(limit=None)[0], settings=settings, now=NOW)
    assert written and all(item.warnings == (POOL_NO_SPREAD,) for item in written), "precondition: no spread"
    assert all(item.status == "pooled" and item.between_variance == 0.0 for item in written)
    lines = _lines(storage, store, _root(tmp_path, "pooling:\n  enabled: true\n"), catch_up=False)
    assert len(lines) == len(FLAT)
    for line in lines.values():
        assert line.warning == POOL_NO_SPREAD  # type: ignore[attr-defined]
        assert "no more than chance explains" in line.note  # type: ignore[attr-defined]
        assert jargon_in(line.note) == ()  # type: ignore[attr-defined]
        assert line.estimated is not None  # type: ignore[attr-defined]
    figures = [figure for line in lines.values() for figure in (line.estimated, line.measured)]  # type: ignore[attr-defined]
    assert check_figures(figures, [], storage) == []


def test_campaigns_that_do_differ_carry_no_warning(tmp_path: Path) -> None:
    storage, store = _world(tmp_path, SPREAD, seed=13_670)
    settings = PoolingSettings(enabled=True)
    written = refresh_pooled_estimates(storage, store, store.list(limit=None)[0], settings=settings, now=NOW)
    assert all(item.warnings == () and (item.between_variance or 0.0) > 0.0 for item in written)
    lines = _lines(storage, store, _root(tmp_path, "pooling:\n  enabled: true\n"), catch_up=False)
    assert all(line.warning is None and "chance" not in line.note for line in lines.values())  # type: ignore[attr-defined]


def test_a_stale_file_is_absent_and_the_summary_with_the_store_catches_up(tmp_path: Path) -> None:
    storage, store = _world(tmp_path, SPREAD, seed=13_700)
    on = _root(tmp_path, "pooling:\n  enabled: true\n")
    refresh_pooled_estimates(
        storage, store, store.list(limit=None)[0], settings=PoolingSettings(enabled=True), now=NOW
    )
    first = sorted(c.campaign_id for c in store.list(limit=None))
    assert set(_lines(storage, store, on, catch_up=False)) == set(first)

    # Pooling off: a member is re-measured and a fifth campaign measured, and nothing is pooled.
    member = first[1]
    report = json.loads(storage.read_bytes(campaign_key(member, "incrementality_report.json")))
    report["computed_at"] = (NOW + timedelta(days=1)).isoformat()
    storage.write_bytes(campaign_key(member, "incrementality_report.json"), json.dumps(report).encode())
    fifth = _measured(storage, store, 4, 0.09, 13_689)
    assert not storage.exists(campaign_key(fifth, POOLED_FILENAME))

    # On again: without the store every file read from the old member report is stale, so none is shown.
    assert _lines(storage, store, on, catch_up=False) == {}
    # With it, the use case is rewritten and every campaign, the fifth included, is pooled with the five.
    caught = _lines(storage, store, on, catch_up=True)
    assert set(caught) == {*first, fifth}
    assert all(_pooled(storage, cid)["campaigns_compared"] == 5 for cid in caught)
    stamp = _pooled(storage, member)["member_reports"]
    assert isinstance(stamp, dict) and stamp[member].startswith("2026-10-02")
    before = {cid: storage.read_bytes(campaign_key(cid, POOLED_FILENAME)) for cid in caught}
    _lines(storage, store, on, catch_up=True)
    assert before == {
        cid: storage.read_bytes(campaign_key(cid, POOLED_FILENAME)) for cid in caught
    }, "current: kept"

    # The settings change: the files fitted under the old ones are absent until caught up, then not pooled.
    six = _root(tmp_path, "pooling:\n  enabled: true\n  min_campaigns: 6\n")
    assert _lines(storage, store, six, catch_up=False) == {}
    lines = _lines(storage, store, six, catch_up=True)
    assert {line.code for line in lines.values()} == {POOL_TOO_FEW}  # type: ignore[attr-defined]
    assert all(line.estimated is None for line in lines.values())  # type: ignore[attr-defined]


def test_with_pooling_off_the_store_is_never_read(tmp_path: Path) -> None:
    storage, store = _world(tmp_path, FLAT[:1], seed=13_690)
    builder = _Builder(storage, None, tmp_path, NOW, SummaryRules())

    class Untouchable(InMemoryCampaignStore):
        def list(
            self, *, run_id: str | None = None, use_case_id: str | None = None, limit: int | None = 100
        ) -> tuple[Campaign, ...]:
            raise AssertionError("read with pooling off")

    assert _estimated_lines(builder, {}, store.list(limit=None), tmp_path, Untouchable(), NOW) is None
    assert not [key for key in storage.list_keys("campaigns/") if key.endswith(POOLED_FILENAME)]
