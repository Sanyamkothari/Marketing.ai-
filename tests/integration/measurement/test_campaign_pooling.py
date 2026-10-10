"""Plan K M136 (DEC-1402) through the product's own API: the Estimated figure of comparable campaigns.

Every campaign is built by the real code - the Phase 1 scoring stages, the campaign routes for the outcomes and the
measurement (`POST /campaigns/{id}/measure`, which reads through `measure_campaign`) - never by writing a report or
a pooled file by hand. With `pooling.enabled: true` in a copy of `configs/measurement.yaml`:

* each measured campaign has `pooled_estimate.json` beside its report, read from the stored reports' registered
  estimates (recomputed here from those reports), labelled Estimated;
* campaigns measuring the same customers enter once, and a campaign read on a different outcome window is in a cell
  of its own, so nothing is pooled for it and the reason is shown (`POOL_TOO_FEW`);
* the summary shows the Estimated figure beside the measured one, every number traced, and the value proven to date
  is exactly the sum of the raw measured lower bounds: the same totals as with pooling off;
* the Value Proof Pack has an "Estimated" line beside the measured difference, never as its last (benefit) line;
* nothing is pooled across epochs of a changed holdout;
* the learn record's optional prior is the cell's average and spread, and absent with pooling off: written by the
  real learn path (`cycle.learn_from_campaign`, the monthly loop's learn step) on a run that engaged the holdout
  service;
* the scheduled measurement (`cycle.measure_step`, the monthly loop's measure step, fired by
  `engine.scheduling.firing`) reads the installation's `configs/measurement.yaml`, as the route does;
* three comparable campaigns are too few: Morris's factor `(k - 3) / (k - 1)` is zero, so nothing is drawn and the
  summary and the pack say so rather than show an "estimate" equal to the measured figure;
* campaigns measured while pooling was off are pooled once it is on, the next time the summary is built.

With pooling off (the shipped configuration) nothing is written and the summary and pack carry no trace of it.
"""

from __future__ import annotations

import json
import math
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import create_app
from engine.access.roles import SYSTEM_SCHEDULER
from engine.config import load_use_case
from engine.contracts import dump_artefact
from engine.measurement import cycle
from engine.measurement.campaign import Campaign, CampaignStore
from engine.measurement.cycle import learn_from_campaign, measure_step
from engine.measurement.learn import LEARN_RECORD_FILENAME, LearnRecord
from engine.measurement.pooling import (
    POOL_TOO_FEW,
    POOLED_FILENAME,
    CellMember,
    cell_entry,
    fit_cell,
    pooled_prior,
    se_from_interval,
)
from engine.measurement.pull import OutcomePullSpec, PullRecord, PullSelection
from engine.measurement.summary import build_summary
from engine.pilot.plain import jargon_in
from engine.pilot.proof import build_proof
from engine.registry import LocalModelRegistry
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import ConfidenceValue
from tests.integration.measurement.support import MATURE, USE_CASE, ok, propensity_run
from tests.integration.pilot.test_proof_pack import assert_traced
from tests.unit.measurement.test_learn import _build
from tests.unit.measurement.test_learn import _world as _learn_world
from tests.unit.production.scheduling_support import RecordingJobs

pytestmark = pytest.mark.integration

RUNS = ("r_20261010_13600001", "r_20261010_13600002", "r_20261010_13600003", "r_20261010_13600004")
RUN_WINDOW = "r_20261010_13600005"
TRIO = ("r_20261010_13600011", "r_20261010_13600012", "r_20261010_13600013")
RUN_LATE = "r_20261010_13600021"
ESTIMATED_LINE = "Estimated difference, drawn towards comparable campaigns (not measured)"


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    root: Path
    pooled: tuple[str, ...]
    """The campaigns that are pooled together: one per scoring run, the latest measured of each."""
    earlier: str
    """Measured on the same run and outcomes as `pooled[0]`, before it: the same customers, so it does not enter."""
    own_window: str
    """Read on a 60-day window: a cell of its own."""


def _upload(client: TestClient, frame: pd.DataFrame) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    response = client.post(
        "/uploads",
        files={"file": ("outcomes.csv", payload, "text/csv")},
        data={"use_case": "win-back-campaign", "mode": "score"},
    )
    return str(ok(response, 201)["upload_id"])


def _campaign(client: TestClient, run_id: str, name: str, outcomes: pd.DataFrame, **measure: Any) -> str:
    created = ok(client.post("/campaigns", json={"run_id": run_id, "name": name}), 201)
    campaign_id = str(created["campaign"]["campaign_id"])
    body = {"upload_id": _upload(client, outcomes), "outcome_column": "converted"}
    ok(client.post(f"/campaigns/{campaign_id}/outcomes", json=body))
    ok(client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": MATURE.isoformat(), **measure}))
    return campaign_id


def _pooling_root(config_root: Path, target: Path, *, enabled: bool) -> Path:
    shutil.copytree(config_root, target)
    (target / "measurement.yaml").write_text(f"pooling:\n  enabled: {str(enabled).lower()}\n")
    return target


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    base = tmp_path_factory.mktemp("pooling")
    root = _pooling_root(config_root, base / "configs", enabled=True)
    data_dir = base / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    runs = [
        propensity_run(storage, run_id, rows=6_000, seed=1360 + index) for index, run_id in enumerate(RUNS)
    ]
    window_run = propensity_run(storage, RUN_WINDOW, rows=6_000, seed=1369)
    with TestClient(create_app(config_root=root, data_dir=data_dir)) as client:
        first_outcomes = runs[0].outcomes.rename(columns={"reactivated_90d": "converted"})
        earlier = _campaign(client, RUNS[0], "Win-back, first reading", first_outcomes)
        pooled = tuple(
            _campaign(
                client,
                run.run_id,
                f"Win-back {index}",
                run.outcomes.rename(columns={"reactivated_90d": "converted"}),
            )
            for index, run in enumerate(runs)
        )
        own_window = _campaign(
            client,
            RUN_WINDOW,
            "Win-back, sixty days",
            window_run.outcomes.rename(columns={"reactivated_90d": "converted"}),
            outcome_window_days=60,
        )
        yield World(client, storage, root, pooled, earlier, own_window)


def _doc(world: World, campaign_id: str, name: str) -> dict[str, Any]:
    return dict(json.loads(world.storage.read_bytes(f"campaigns/{campaign_id}/{name}")))


def _pooled(world: World, campaign_id: str) -> dict[str, Any]:
    return _doc(world, campaign_id, POOLED_FILENAME)


def _campaigns(world: World) -> list[Campaign]:
    listed = ok(world.client.get("/campaigns"))["campaigns"]
    return [Campaign.model_validate(item) for item in listed]


# --- the files ---------------------------------------------------------------------------------------------------
def test_each_comparable_campaign_has_an_estimated_figure_beside_its_report(world: World) -> None:
    for campaign_id in world.pooled:
        pooled, report = _pooled(world, campaign_id), _doc(world, campaign_id, "incrementality_report.json")
        assert pooled["label"] == "Estimated" and pooled["status"] == "pooled"
        assert pooled["pooled_from"] == sorted(world.pooled) and pooled["campaigns_compared"] == len(RUNS)
        assert pooled["estimate_field"] == "absolute_lift"
        assert (
            pooled["measured_effect"] == report["absolute_lift"]["value"]
        ), "the stored estimate, not re-measured"
        assert pooled["report_computed_at"] == report["computed_at"]
        assert pooled["method"] in {"reml", "dl"} and pooled["code"] is None
        assert pooled["cell"]["outcome_column"] == "converted" and pooled["cell"]["holdout"] == "run"


def test_the_figures_are_the_cell_fit_of_the_stored_reports(world: World) -> None:
    members = []
    for campaign_id in sorted(world.pooled):
        lift = ConfidenceValue.model_validate(
            _doc(world, campaign_id, "incrementality_report.json")["absolute_lift"]
        )
        se = se_from_interval(lift)
        assert se is not None
        members.append(CellMember(campaign_id, lift.value, se))
    fit = fit_cell(members)
    for member in fit.members:
        pooled = _pooled(world, member.campaign_id)
        low, high = member.interval()
        assert pooled["pooled_effect"] == pytest.approx(member.pooled_effect, rel=1e-12)
        assert (pooled["pooled_interval"]["ci_low"], pooled["pooled_interval"]["ci_high"]) == pytest.approx(
            (low, high)
        )
        assert pooled["shrinkage"] == pytest.approx(member.shrinkage)
        assert pooled["cell_mean"] == pytest.approx(fit.mean)
        # Four campaigns: Morris's factor (k - 3) / (k - 1) caps the weight on the average at a third.
        assert 0.0 <= pooled["shrinkage"] <= 1 / 3


def test_campaigns_measuring_the_same_customers_enter_once(world: World) -> None:
    assert world.earlier not in _pooled(world, world.pooled[0])["pooled_from"]
    assert not world.storage.exists(f"campaigns/{world.earlier}/{POOLED_FILENAME}")


def test_a_campaign_with_too_few_comparable_ones_is_not_pooled_and_says_why(world: World) -> None:
    pooled = _pooled(world, world.own_window)
    assert pooled["status"] == "not_pooled" and pooled["code"] == POOL_TOO_FEW
    assert pooled["pooled_effect"] is None and pooled["pooled_interval"] is None
    assert pooled["pooled_from"] == [world.own_window] and pooled["reason"]
    view = ok(world.client.get(f"/pilot/proof/{world.own_window}", params={"format": "json"}))
    incremental = next(section for section in view["sections"] if section["key"] == "incremental")
    line = next(line for line in incremental["lines"] if line["label"] == ESTIMATED_LINE)
    assert line["value"] is None and "fewer comparable campaigns" in line["missing"]


# --- the summary: beside the measured figure, never in the total ---------------------------------------------------
def test_the_summary_shows_the_estimate_beside_the_measured_difference(world: World) -> None:
    summary = ok(world.client.get("/campaigns/summary"))
    lines = {line["campaign_id"]: line for line in summary["estimated"]}
    assert set(world.pooled) <= set(lines)
    for campaign_id in world.pooled:
        line, pooled = lines[campaign_id], _pooled(world, campaign_id)
        report = _doc(world, campaign_id, "incrementality_report.json")
        assert line["label"] == "Estimated" and line["code"] is None
        assert line["measured"]["value"] == report["absolute_lift"]["value"]
        assert line["estimated"]["value"] == pooled["pooled_effect"]
        assert line["estimated_low"]["value"] == pooled["pooled_interval"]["ci_low"]
        assert line["campaigns_compared"]["value"] == len(RUNS)
        assert {source["artefact"] for source in line["estimated"]["sources"]} == {
            f"campaigns/{campaign_id}/{POOLED_FILENAME}"
        }
    alone = lines[world.own_window]
    assert (
        alone["code"] == POOL_TOO_FEW and alone["estimated"] is None and "Fewer comparable" in alone["note"]
    )
    assert world.earlier not in lines, "listed apart as the same customers, so it has no line of its own"
    assert all(jargon_in(line["note"]) == () for line in summary["estimated"])


def test_value_to_date_is_the_sum_of_raw_measured_lower_bounds_whatever_pooling_says(
    world: World, config_root: Path
) -> None:
    on = ok(world.client.get("/campaigns/summary"))
    off = build_summary(world.storage, _campaigns(world), root=config_root).model_dump(mode="json")
    assert "estimated" not in off, "with pooling off the summary carries no trace of it"
    assert on["proven"] == off["proven"], "the totals are the raw measured lower bounds either way"
    outcomes = next(total for total in on["proven"]["totals"] if total["unit"] == "outcomes:converted")
    bounds = {
        line["campaign_id"]: _doc(world, line["campaign_id"], "incrementality_report.json")[
            "incremental_conversions"
        ]["ci_low"]
        for line in outcomes["campaigns"]
    }
    assert outcomes["total"]["value"] == pytest.approx(sum(bounds.values()))
    assert all(POOLED_FILENAME not in source["artefact"] for source in outcomes["total"]["sources"])


# --- the Value Proof Pack -------------------------------------------------------------------------------------------
def test_the_proof_pack_has_an_estimated_line_beside_the_measured_difference(
    world: World, config_root: Path
) -> None:
    campaign_id = world.pooled[1]
    view = ok(world.client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"}))
    incremental = next(section for section in view["sections"] if section["key"] == "incremental")
    labels = [line["label"] for line in incremental["lines"]]
    assert (
        labels.index("Difference") < labels.index(ESTIMATED_LINE) < len(labels) - 1
    ), "never the benefit line"
    estimated = incremental["lines"][labels.index(ESTIMATED_LINE)]
    pooled = _pooled(world, campaign_id)
    assert estimated["value"]["value"] == pooled["pooled_effect"]
    assert (estimated["low"]["value"], estimated["high"]["value"]) == (
        pooled["pooled_interval"]["ci_low"],
        pooled["pooled_interval"]["ci_high"],
    )
    assert any("not a measured result" in note for note in incremental["notes"])
    assert_traced(world.storage.root, view)  # the independent resolver of the Proof Pack's own tests
    assert (
        build_proof(world.storage, campaign_id, root=world.root).model_dump(mode="json")["sections"]
        == view["sections"]
    )
    # Off: the pack is the pack without the two Estimated lines and the note, and reads no pooled file.
    plain = build_proof(world.storage, campaign_id, root=config_root).model_dump(mode="json")
    assert f"campaigns/{campaign_id}/{POOLED_FILENAME}" not in plain["artefacts"]
    plain_incremental = next(section for section in plain["sections"] if section["key"] == "incremental")
    on_lines = [
        line for line in incremental["lines"] if not line["label"].startswith(("Estimated", "Comparable"))
    ]
    assert plain_incremental["lines"] == on_lines
    assert plain["headline"] == view["headline"]
    assert [s for s in plain["sections"] if s["key"] != "incremental"] == [
        s for s in view["sections"] if s["key"] != "incremental"
    ]


# --- guards -----------------------------------------------------------------------------------------------------------
def test_nothing_is_pooled_across_epochs_of_a_changed_holdout(world: World) -> None:
    """Two campaigns held back by the same persistent holdout in different epochs are in different cells."""
    campaign = next(item for item in _campaigns(world) if item.campaign_id == world.pooled[0])
    first = campaign.model_copy(
        update={"holdout_scope": "use_case", "holdout_scope_key": "win-back-campaign", "holdout_epoch": 1}
    )
    second = first.model_copy(update={"holdout_epoch": 2})
    one, two, per_run = (cell_entry(world.storage, item) for item in (first, second, campaign))
    assert one is not None and two is not None and per_run is not None
    assert one.cell.holdout == "use_case:win-back-campaign:epoch-1"
    assert len({one.cell.cell_id, two.cell.cell_id, per_run.cell.cell_id}) == 3


def test_the_learn_record_carries_the_cell_prior_only_when_pooling_is_on(
    world: World, config_root: Path
) -> None:
    campaign_id = world.pooled[2]
    prior = pooled_prior(world.storage, campaign_id, root=world.root)
    pooled = _pooled(world, campaign_id)
    assert prior is not None and prior.label == "Estimated"
    assert (prior.mean, prior.between_variance, prior.campaigns) == (
        pooled["cell_mean"],
        pooled["between_variance"],
        len(RUNS),
    )
    assert pooled_prior(world.storage, campaign_id, root=config_root) is None, "off: no prior"
    assert pooled_prior(world.storage, world.own_window, root=world.root) is None, "not pooled: no prior"
    assert math.isfinite(prior.mean_se) and prior.mean_se > 0
    # A learn record built by M106's own code: without a prior its file is what it was; with one it carries it.
    record = _build(*_learn_world(4_000)).record
    assert record.pooled_prior is None and "pooled_prior" not in json.loads(record.model_dump_json())
    carried = record.model_copy(update={"pooled_prior": prior})
    assert LearnRecord.model_validate_json(carried.model_dump_json()).pooled_prior == prior


# --- off ----------------------------------------------------------------------------------------------------------
def test_with_pooling_off_nothing_is_written(config_root: Path, tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    (tmp_path / "data").mkdir(exist_ok=True)
    runs = [
        propensity_run(storage, run_id, rows=3_000, seed=1370 + index)
        for index, run_id in enumerate(RUNS[:3])
    ]
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as client:
        ids = [
            _campaign(
                client,
                run.run_id,
                f"Off {index}",
                run.outcomes.rename(columns={"reactivated_90d": "converted"}),
            )
            for index, run in enumerate(runs)
        ]
        summary = ok(client.get("/campaigns/summary"))
    assert "estimated" not in summary
    assert not [key for key in storage.list_keys("campaigns/") if key.endswith(POOLED_FILENAME)]
    assert all(storage.exists(f"campaigns/{campaign_id}/incrementality_report.json") for campaign_id in ids)


# --- the learn record, written by the real learn path ----------------------------------------------------------------
@pytest.fixture(scope="module")
def m106_frame() -> tuple[pd.DataFrame, LearnRecord]:
    """M106's randomised frame and learn record, built by M106's own code on its own world (`test_learn`)."""
    built = _build(*_learn_world(20_000))
    return built.frame, built.record


def _learnable(root: Path, target: Path) -> Path:
    """A copy of `root` whose use case learns from groups of a few hundred (these campaigns hold back 600)."""
    shutil.copytree(root, target)
    path = target / "use_cases" / "win_back_campaign.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.setdefault("uplift", {})["min_arm_rows"] = 200
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


def _learn(
    world: World, campaign_id: str, root: Path, base: Path, m106: tuple[pd.DataFrame, LearnRecord]
) -> tuple[str, LearnRecord]:
    """`cycle.learn_from_campaign` (the monthly loop's learn step) on a copy of the world's data.

    The world's runs drew their control group per run, so their own frame would carry no learn record; the
    frame step (`cycle._experiment`) hands back M106's randomised frame and record instead. Everything after it
    is the real path: the readiness rule, the training upload and its checks, the learning run's start, and the
    learn record written under the new run with the prior read for this campaign.
    """
    shutil.copytree(world.storage.root, base / "data")
    storage = LocalStorage(base / "data")
    root = _learnable(root, base / "configs")
    campaign = storage.read_model(f"campaigns/{campaign_id}/campaign.json", Campaign)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cycle, "_experiment", lambda *_args, **_kwargs: ("contacted", *m106))
        started = learn_from_campaign(
            storage,
            LocalModelRegistry(base / "registry.db"),
            RecordingJobs(),
            campaign=campaign,
            config_root=root,
            principal=SYSTEM_SCHEDULER,
            client_tag=None,
            now=MATURE,
        )
    text = storage.read_bytes(run_key(started.run_id, LEARN_RECORD_FILENAME)).decode()
    return text, LearnRecord.model_validate_json(text)


def test_the_learn_path_writes_the_cell_prior_only_when_pooling_is_on(
    world: World, config_root: Path, tmp_path: Path, m106_frame: tuple[pd.DataFrame, LearnRecord]
) -> None:
    campaign_id = world.pooled[2]
    on_text, on_record = _learn(world, campaign_id, world.root, tmp_path / "on", m106_frame)
    pooled = _pooled(world, campaign_id)
    assert on_record.pooled_prior is not None and on_record.pooled_prior.label == "Estimated"
    assert (on_record.pooled_prior.mean, on_record.pooled_prior.between_variance) == (
        pooled["cell_mean"],
        pooled["between_variance"],
    )
    assert (
        on_record.pooled_prior.campaigns == len(RUNS) and on_record.pooled_prior.cell_id == pooled["cell_id"]
    )
    assert '"pooled_prior"' in on_text

    off_text, off_record = _learn(world, campaign_id, config_root, tmp_path / "off", m106_frame)
    assert off_record.pooled_prior is None and "pooled_prior" not in off_text
    assert off_text == dump_artefact(
        on_record.model_copy(update={"pooled_prior": None, "uplift_run_id": off_record.uplift_run_id})
    ), "off: the record is exactly the one written before Plan K"


# --- the scheduled measurement, and three campaigns ------------------------------------------------------------------
@dataclass(frozen=True)
class Trio:
    client: TestClient
    storage: LocalStorage
    campaigns: tuple[str, ...]
    scheduled: str
    """Measured by the monthly loop's measure step, not by the route."""


@pytest.fixture(scope="module")
def trio(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Trio]:
    base = tmp_path_factory.mktemp("pooling-trio")
    root = _pooling_root(config_root, base / "configs", enabled=True)
    data_dir = base / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    runs = [
        propensity_run(storage, run_id, rows=3_000, seed=1380 + index) for index, run_id in enumerate(TRIO)
    ]
    outcomes = [run.outcomes.rename(columns={"reactivated_90d": "converted"}) for run in runs]
    with TestClient(create_app(config_root=root, data_dir=data_dir)) as client:
        measured = [
            _campaign(client, run.run_id, f"Trio {index}", outcomes[index])
            for index, run in enumerate(runs[:2])
        ]
        created = ok(client.post("/campaigns", json={"run_id": TRIO[2], "name": "Trio, scheduled"}), 201)
        scheduled = str(created["campaign"]["campaign_id"])
        store: CampaignStore = client.app.state.campaign_store  # type: ignore[attr-defined]
        pulled = outcomes[2].assign(recorded_on=MATURE.date().isoformat())
        record = PullRecord(
            connection_id="c_0123456789ab",
            kind="postgres",
            schema_name="crm",
            table="outcomes",
            rows=len(pulled.index),
            pulled_at=MATURE,
        )
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("engine.measurement.pull.pull_frame", lambda *_args, **_kwargs: (pulled, record))
            step = measure_step(
                storage,
                store,
                None,  # type: ignore[arg-type]  # the pull is the patched one: no connection is opened
                config=load_use_case(USE_CASE),
                client_id=None,
                pull=OutcomePullSpec(
                    connection_id="c_0123456789ab",
                    selection=PullSelection(schema_name="crm", table="outcomes"),
                    date_column="recorded_on",
                    outcome_column="converted",
                ),
                ledger_engine=None,
                now=MATURE,
                config_root=root,
            )
        assert step.campaign_id == scheduled, step
        yield Trio(client, storage, (*measured, scheduled), scheduled)


def test_a_scheduled_measurement_reads_the_installations_pooling_settings(trio: Trio) -> None:
    key = f"campaigns/{trio.scheduled}/{POOLED_FILENAME}"
    assert trio.storage.exists(key), "the measure step passed the installation's configuration root"
    pooled = json.loads(trio.storage.read_bytes(key))
    report = json.loads(trio.storage.read_bytes(f"campaigns/{trio.scheduled}/incrementality_report.json"))
    assert pooled["report_computed_at"] == report["computed_at"]
    assert pooled["pooled_from"] == sorted(trio.campaigns)


def test_three_comparable_campaigns_are_not_drawn_towards_each_other(trio: Trio) -> None:
    for campaign_id in trio.campaigns:
        pooled = json.loads(trio.storage.read_bytes(f"campaigns/{campaign_id}/{POOLED_FILENAME}"))
        assert (pooled["status"], pooled["code"], pooled["pooled_effect"]) == (
            "not_pooled",
            POOL_TOO_FEW,
            None,
        )
        assert "fourth" in pooled["reason"] and pooled["campaigns_compared"] == 3
    summary = ok(trio.client.get("/campaigns/summary"))
    lines = {line["campaign_id"]: line for line in summary["estimated"]}
    assert set(lines) == set(trio.campaigns)
    assert all(line["estimated"] is None and line["code"] == POOL_TOO_FEW for line in lines.values())
    view = ok(trio.client.get(f"/pilot/proof/{trio.campaigns[0]}", params={"format": "json"}))
    incremental = next(section for section in view["sections"] if section["key"] == "incremental")
    line = next(line for line in incremental["lines"] if line["label"] == ESTIMATED_LINE)
    assert line["value"] is None and "fewer comparable campaigns" in line["missing"]
    assert not any("as far towards" in note for note in incremental["notes"])


# --- turned on later ---------------------------------------------------------------------------------------------
def test_campaigns_measured_while_pooling_was_off_are_pooled_once_it_is_on(
    world: World, config_root: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(world.storage.root, data_dir)
    storage = LocalStorage(data_dir)
    late = propensity_run(storage, RUN_LATE, rows=3_000, seed=1390)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as off:
        added = _campaign(
            off,
            RUN_LATE,
            "Win-back, measured with pooling off",
            late.outcomes.rename(columns={"reactivated_90d": "converted"}),
        )
    assert not storage.exists(f"campaigns/{added}/{POOLED_FILENAME}")
    with TestClient(create_app(config_root=world.root, data_dir=data_dir)) as on:
        before = ok(on.get(f"/pilot/proof/{world.pooled[0]}", params={"format": "json"}))
        summary = ok(on.get("/campaigns/summary"))
        after = ok(on.get(f"/pilot/proof/{added}", params={"format": "json"}))
    members = sorted((*world.pooled, added))
    for campaign_id in members:
        pooled = json.loads(storage.read_bytes(f"campaigns/{campaign_id}/{POOLED_FILENAME}"))
        assert pooled["pooled_from"] == members and pooled["status"] == "pooled"
    lines = {line["campaign_id"]: line for line in summary["estimated"]}
    assert lines[added]["campaigns_compared"]["value"] == len(members)
    incremental = next(section for section in after["sections"] if section["key"] == "incremental")
    assert any(line["label"] == ESTIMATED_LINE and line["value"] for line in incremental["lines"])
    # Before the summary caught up, the pack showed the four-campaign figure it had: its members were unchanged.
    first = next(section for section in before["sections"] if section["key"] == "incremental")
    assert any(line["label"] == ESTIMATED_LINE and line["value"] for line in first["lines"])


# --- a bad settings file -------------------------------------------------------------------------------------------
def test_a_mistyped_settings_file_is_answered_with_its_code_not_a_crash(
    config_root: Path, tmp_path: Path
) -> None:
    root = _pooling_root(config_root, tmp_path / "configs", enabled=True)
    (root / "measurement.yaml").write_text("pooling:\n  enabeld: true\n")
    (tmp_path / "data").mkdir()
    with TestClient(create_app(config_root=root, data_dir=tmp_path / "data")) as client:
        response = client.get("/campaigns/summary")
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "CONFIG_INVALID" and detail["path"] == "pooling.enabeld"
    assert "measurement.yaml" in detail["message"]
