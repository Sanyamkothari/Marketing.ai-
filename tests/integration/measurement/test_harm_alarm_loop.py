"""Plan K M138 (DEC-1403) acceptance: the harm alarm in the monthly loop and on demand, and never a win.

The M107 pattern on fakes: a finished scoring run of the win-back use case (the Phase 1 actions stage over a
generated population, `tests/integration/measurement/support.py`), its campaign and test plan recorded through
the product's own routes, its outcomes in the client's database (the SQLite-backed fake of
`tests/unit/connections/fakes.py`, reached through the real PostgreSQL connector), and a `measure` schedule fired
by the real `ScheduleFirer` under a controlled clock. The plan's analysis date lies after the outcome window,
so each reading in between is an early look: exactly where M94 refuses a verdict and the alarm watches for harm.

* a campaign that harms customers raises the `HARM_ALARM` card on the campaigns' summary and one `harm_alarm`
  alert; the next day's reading keeps the alarm (it stands) and raises no second alert; nothing is stopped;
* with the setting off (the shipped default) the same cycle writes no file and raises nothing;
* a planted benefit raises nothing, and no output of an early look - the summary's sentences, the card text,
  the Value Proof Pack, the campaign page, the alert, the check's own file - reads as a win;
* on demand (`POST /campaigns/{id}/measure`) the same check runs; no registered plan, or a plan with no
  smallest effect worth detecting, means no alarm and says why; an amended plan sets the alarm aside;
* while the outcome window is still open (a plan whose analysis date is the day the window closes) the loop
  reads what is in so far, raises the card and one alert, and stores nothing else;
* the card follows the setting and names the offer; a `configs/measurement.yaml` that cannot be read refuses a
  measurement before anything is written (422 on demand, a failed firing with the code in the loop).
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.connections import postgres as postgres_module
from engine.connections import sql as sql_module
from engine.connections.store import ConnectionStore
from engine.measurement.campaign import InMemoryCampaignStore
from engine.measurement.cycle import CycleServices
from engine.measurement.harm import HARM_ALARM, HARM_ALARM_FILENAME, HarmCheck
from engine.measurement.pull import OutcomePullSpec, PullSelection
from engine.measurement.summary import build_summary
from engine.pilot.plain import jargon_in
from engine.scheduling.alerts import AlertKind, AlertQuery
from engine.scheduling.firing import ScheduleFirer
from engine.scheduling.schedules import FiringStatus, ScheduleKind, ScheduleParameters
from engine.settings import Settings
from engine.utils.time import utc_now
from tests.integration.measurement.support import SENT, USE_CASE, ok, propensity_run, upload
from tests.unit.connections.fakes import FakeServer, driver_module
from tests.unit.production.scheduling_support import World, make_world

pytestmark = pytest.mark.integration

RUN_HARM = "r_20260501_13800001"
RUN_GOOD = "r_20260501_13800002"
WINDOW = 30
BASE_RATE = 0.10
MDE_PP = 3.0
HARM = -0.06
BENEFIT = 0.08
CLOCK = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)
LOOP_ANALYSIS = "2026-12-01"
"""After every window has closed and after the loop's readings: each reading is an early look."""
RECORDED_ON = (SENT + timedelta(days=12)).date().isoformat()

WIN_WORDING = re.compile(
    r"\b(?:added about|added|win(?!-back)|wins|won|winner|worked|working|success\w*|beat\w*|proven|caused|"
    r"extra (?:conversions|outcomes)|safe to roll out|improv\w*|cut \S+ by about|outperform\w*|uplift)\b",
    re.IGNORECASE,
)
"""How a verdict reads (`engine.uplift.measure.campaign_verdict`, the Value Proof Pack's headline, M137's words):
none of it may appear in what an early look shows about a campaign."""


def _enabled_root(config_root: Path, target: Path, *, enabled: bool) -> Path:
    shutil.copytree(config_root, target)
    if enabled:
        path = target / "measurement.yaml"
        text = path.read_text(encoding="utf-8")
        assert "  enabled: false" in text
        path.write_text(text.replace("  enabled: false", "  enabled: true", 1), encoding="utf-8")
    return target


def _outcomes(scores: pd.DataFrame, effect: float, *, seed: int) -> pd.DataFrame:
    """Each customer's outcome: contacted customers (eligible, not held back) convert at the base rate + effect."""
    contacted = scores["suppressed_reason"].isna().to_numpy() & ~scores["control_group"].to_numpy(dtype=bool)
    chance = BASE_RATE + effect * contacted
    converted = (np.random.default_rng(seed).random(len(scores.index)) < chance).astype(int)
    return pd.DataFrame({"customer_id": scores["customer_id"].astype(str), "converted": converted})


@dataclass
class Harness:
    world: World
    client: TestClient
    campaigns: InMemoryCampaignStore
    connections: ConnectionStore
    connection_id: str
    server: FakeServer
    root: Path

    def firer(self) -> ScheduleFirer:
        return ScheduleFirer(
            self.world.services(
                cycle=CycleServices(campaigns=self.campaigns, connections=self.connections),
                config_root=self.root,
            )
        )

    def measure_schedule(self) -> Any:
        pull = OutcomePullSpec(
            connection_id=self.connection_id,
            selection=PullSelection(schema_name="crm", table="outcomes"),
            date_column="recorded_on",
            outcome_column="converted",
        )
        return self.world.schedule(
            ScheduleKind.MEASURE,
            client_id=None,
            use_case_id=USE_CASE,
            cron="0 7 * * *",
            parameters=ScheduleParameters(outcomes=pull),
        )

    def measure_schedule_stored(self) -> Any:
        return self.world.store.get("sch_measure")

    def campaign(self, run_id: str, name: str, *, plan: dict[str, Any] | None) -> str:
        created = ok(
            self.client.post(
                "/campaigns", json={"run_id": run_id, "name": name, "outcome_window_days": WINDOW}
            ),
            201,
        )
        campaign_id = str(created["campaign"]["campaign_id"])
        if plan is not None:
            body = {"metric": "Came back", "outcome_column": "converted", **plan}
            ok(self.client.post(f"/campaigns/{campaign_id}/plan", json=body), 201)
        return campaign_id

    def into_crm(self, outcomes: pd.DataFrame) -> None:
        rows = [
            (key, int(value), RECORDED_ON)
            for key, value in zip(outcomes["customer_id"], outcomes["converted"], strict=True)
        ]
        self.server.db.executemany('INSERT INTO "crm"."outcomes" VALUES (?, ?, ?)', rows)

    def check(self, campaign_id: str) -> HarmCheck | None:
        key = f"campaigns/{campaign_id}/{HARM_ALARM_FILENAME}"
        storage = self.world.storage
        return storage.read_model(key, HarmCheck) if storage.exists(key) else None

    def alerts(self) -> tuple[Any, ...]:
        store = self.world.alerts.store
        return tuple(store.query(AlertQuery(kind=AlertKind.HARM_ALARM)))

    def summary(self) -> dict[str, Any]:
        return dict(ok(self.client.get("/campaigns/summary")))


def _harness(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool
) -> Iterator[Harness]:
    root = _enabled_root(config_root, tmp_path / "configs", enabled=enabled)
    world = make_world(tmp_path / "world", root)
    world.clock.now = CLOCK
    server = FakeServer({"crm": {"placeholder": (["id"], [(1,)])}})
    server.db.execute('CREATE TABLE "crm"."outcomes" ("customer_id", "converted", "recorded_on")')
    monkeypatch.setattr(postgres_module, "load_sdk", lambda _name: driver_module("psycopg", server, "Error"))
    monkeypatch.setattr(sql_module, "reach", lambda host, port: None)
    connections = ConnectionStore(world.storage, Settings())
    record = connections.create(
        kind="postgres",
        name="CRM",
        config={"host": "db.example.com", "database": "crm", "user": "reader"},
        secrets={"password": "s3cret-pw"},
    )
    campaigns = InMemoryCampaignStore()
    app = create_app(config_root=root, data_dir=world.storage.root)
    app.state.storage = world.storage
    app.state.campaign_store = campaigns
    with TestClient(app) as client:
        yield Harness(world, client, campaigns, connections, record.connection_id, server, root)


@pytest.fixture
def harness(tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    yield from _harness(tmp_path, config_root, monkeypatch, enabled=True)


@pytest.fixture
def harness_off(tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    yield from _harness(tmp_path, config_root, monkeypatch, enabled=False)


def _harmful_campaign(harness: Harness) -> str:
    run = propensity_run(harness.world.storage, RUN_HARM, rows=8_000, seed=1381)
    campaign_id = harness.campaign(
        RUN_HARM,
        "Harmful win-back",
        plan={"analysis_date": LOOP_ANALYSIS, "mde_pp": MDE_PP, "base_rate": BASE_RATE},
    )
    harness.into_crm(_outcomes(run.scores, HARM, seed=13801))
    return campaign_id


def _card(summary: dict[str, Any], campaign_id: str) -> list[dict[str, Any]]:
    return [
        card for card in summary["cards"] if card["code"] == HARM_ALARM and card["campaign_id"] == campaign_id
    ]


# --- the monthly loop ---------------------------------------------------------------------------------------
def test_a_scheduled_reading_of_a_harmful_campaign_raises_the_card_and_one_alert(harness: Harness) -> None:
    campaign_id = _harmful_campaign(harness)
    schedule = harness.measure_schedule()
    firing = harness.firer().fire(schedule) or pytest.fail("not fired")
    assert (firing.status, firing.result_code) == (FiringStatus.SUCCEEDED, "EARLY_LOOK"), firing

    check = harness.check(campaign_id)
    assert check is not None and check.status == "alarm" and check.early_look is True
    assert check.outcome_is_good is True and check.mixing_sd == pytest.approx(MDE_PP / 100)
    (alarm,) = check.alarms
    assert alarm.arm == "treated" and alarm.fired_at == CLOCK and alarm.difference < 0
    assert alarm.statistic is not None and alarm.statistic >= alarm.threshold == pytest.approx(20.0)
    assert alarm.test_plan_hash == check.test_plan_hash
    assert harness.campaigns.get(campaign_id).status.value == "live", "nothing was stopped"  # type: ignore[union-attr]

    (alert,) = harness.alerts()
    assert alert.severity == "warning" and alert.use_case_id == USE_CASE
    assert alert.schedule_id == schedule.schedule_id
    assert campaign_id in alert.message and "nothing has been stopped" in alert.message
    assert jargon_in(alert.message) == ()

    (card,) = _card(harness.summary(), campaign_id)
    assert card["severity"] == "warning" and card["kind"] == "harm_alarm"
    facts = {fact["label"]: fact["value"] for fact in card["facts"]}
    assert facts["Strength at which the alarm is raised"]["text"] == "20.0x"
    assert facts["Difference so far, contacted minus held back"]["value"] == pytest.approx(alarm.difference)
    assert facts["Contacted customers counted so far"]["value"] == alarm.treated_rows
    assert facts["Day the final result can be read"]["value"] == LOOP_ANALYSIS
    assert f"campaigns/{campaign_id}/{HARM_ALARM_FILENAME}" in card["read"]
    assert all(jargon_in(card[field]) == () for field in ("title", "text", "next_step"))

    # The next day's reading: the alarm stands, and no second alert is raised for it.
    harness.world.clock.now = CLOCK + timedelta(days=1)
    again = harness.firer().fire(schedule)
    assert again is not None and again.result_code == "EARLY_LOOK"
    later = harness.check(campaign_id)
    assert later is not None and later.checked_at == CLOCK + timedelta(days=1)
    assert later.alarms == check.alarms, "the alarm raised first stands, with its own figures"
    assert len(harness.alerts()) == 1, "one alert when it first fired, not one a day"
    assert len(_card(harness.summary(), campaign_id)) == 1


def test_with_the_alarm_off_the_same_cycle_writes_and_raises_nothing(harness_off: Harness) -> None:
    campaign_id = _harmful_campaign(harness_off)
    firing = harness_off.firer().fire(harness_off.measure_schedule()) or pytest.fail("not fired")
    assert (firing.status, firing.result_code) == (FiringStatus.SUCCEEDED, "EARLY_LOOK")
    assert harness_off.check(campaign_id) is None
    assert harness_off.alerts() == ()
    files = sorted(
        key.rsplit("/", 1)[-1] for key in harness_off.world.storage.list_keys(f"campaigns/{campaign_id}/")
    )
    assert HARM_ALARM_FILENAME not in files
    summary = harness_off.summary()
    assert not [card for card in summary["cards"] if card["code"] == HARM_ALARM]
    assert not any(HARM_ALARM_FILENAME in name for name in summary["artefacts"])


# --- never a win -----------------------------------------------------------------------------------------------
def _strings(item: Any) -> Iterator[str]:
    if isinstance(item, str):
        yield item
    elif isinstance(item, dict):
        for value in item.values():
            yield from _strings(value)
    elif isinstance(item, list | tuple):
        for value in item:
            yield from _strings(value)


def _about(summary: dict[str, Any], campaign_id: str) -> list[Any]:
    """Every part of the summary that is about `campaign_id`: its cards and its notes."""
    parts: list[Any] = [card for card in summary["cards"] if card.get("campaign_id") == campaign_id]
    proven = summary["proven"]
    for section in ("apart", "excluded", "unpriced"):
        parts += [note for note in proven[section] if note.get("campaign_id") == campaign_id]
    for total in proven["totals"]:
        parts += [line for line in total["campaigns"] if line.get("campaign_id") == campaign_id]
    return parts


def test_no_output_of_an_early_look_reads_as_a_win_whether_it_harmed_or_helped(harness: Harness) -> None:
    harmful = _harmful_campaign(harness)
    # The scheduled reading comes before the helpful campaign exists: both campaigns go out at SENT, so with
    # both live the step would pick between them by their random ids.
    firing = harness.firer().fire(harness.measure_schedule()) or pytest.fail("not fired")
    assert firing.result_code == "EARLY_LOOK", firing
    first = harness.check(harmful)
    assert (
        first is not None and first.status == "alarm"
    ), "the scheduled reading measured the harmful campaign"
    run = propensity_run(harness.world.storage, RUN_GOOD, rows=8_000, seed=1382)
    later = (utc_now() + timedelta(days=60)).date().isoformat()
    helpful = harness.campaign(
        RUN_GOOD, "Helpful win-back", plan={"analysis_date": later, "mde_pp": MDE_PP, "base_rate": BASE_RATE}
    )
    outcomes = _outcomes(run.scores, BENEFIT, seed=13802)
    uploaded = upload(harness.client, outcomes, name="helpful_outcomes.csv")
    ok(
        harness.client.post(
            f"/campaigns/{helpful}/outcomes", json={"upload_id": uploaded, "outcome_column": "converted"}
        )
    )
    page = ok(harness.client.post(f"/campaigns/{helpful}/measure", json={}))  # on demand: an early look

    good_check = harness.check(helpful)
    assert good_check is not None and good_check.status == "no_alarm" and good_check.alarms == ()
    (look,) = good_check.looks
    assert look.difference is not None and look.difference > 0 and look.fired is False
    assert look.statistic is not None and look.statistic > look.threshold, "strong evidence of a benefit..."
    bad_check = harness.check(harmful)
    assert bad_check is not None and bad_check.status == "alarm"

    summary = harness.summary()
    outputs: list[str] = []
    for campaign_id, check in ((harmful, bad_check), (helpful, good_check)):
        outputs += _strings(_about(summary, campaign_id))
        view = page if campaign_id == helpful else ok(harness.client.get(f"/campaigns/{campaign_id}"))
        assert view.get("verdict") is None, "an early look has no verdict"
        outputs += _strings(view)
        proof = harness.client.get(f"/pilot/proof/{campaign_id}")
        assert proof.status_code == 409, proof.text  # the pack is refused for an early look ...
        outputs += _strings(proof.json())  # ... and its refusal is an output too
        outputs += [check.reason, *(item.reason or "" for item in check.looks)]
        report = json.loads(
            harness.world.storage.read_bytes(f"campaigns/{campaign_id}/incrementality_report.json")
        )
        assert report["early_look"] is True
        outputs.append(report["summary"])
    outputs += [alert.message for alert in harness.alerts()]
    assert [card["code"] for card in _about(summary, helpful) if card.get("code") == HARM_ALARM] == []
    assert len(_card(summary, harmful)) == 1
    found = sorted({match.group(0) for text in outputs for match in WIN_WORDING.finditer(text)})
    assert found == [], f"an early look reads as a win: {found}"


# --- on demand ---------------------------------------------------------------------------------------------------
def test_on_demand_a_harmful_reading_alarms_and_a_missing_plan_or_effect_says_why(harness: Harness) -> None:
    later = (utc_now() + timedelta(days=60)).date().isoformat()
    run = propensity_run(harness.world.storage, RUN_HARM, rows=8_000, seed=1383)
    outcomes = _outcomes(run.scores, HARM, seed=13803)
    cases: dict[str, dict[str, Any]] = {
        "planned": {"analysis_date": later, "mde_pp": MDE_PP, "base_rate": BASE_RATE},
        "no_effect": {"analysis_date": later},
    }
    ids = {name: harness.campaign(RUN_HARM, f"On demand {name}", plan=plan) for name, plan in cases.items()}
    uploaded = upload(harness.client, outcomes)
    for campaign_id in ids.values():
        ok(
            harness.client.post(
                f"/campaigns/{campaign_id}/outcomes",
                json={"upload_id": uploaded, "outcome_column": "converted"},
            )
        )
        ok(harness.client.post(f"/campaigns/{campaign_id}/measure", json={}))
    planned = harness.check(ids["planned"])
    assert planned is not None and planned.status == "alarm"
    (alert,) = harness.alerts()
    assert ids["planned"] in alert.message and alert.schedule_id is None
    no_effect = harness.check(ids["no_effect"])
    assert no_effect is not None and (no_effect.status, no_effect.not_checked) == (
        "not_checked",
        "no_effect_planned",
    )
    assert jargon_in(no_effect.reason) == () and not no_effect.alarms

    # No plan: an early look does not exist, but a reading with outcomes still to come in is checked, and says why.
    dated = outcomes.assign(
        treatment_date=np.where(
            np.arange(len(outcomes.index)) % 2 == 0, "2026-05-01", utc_now().date().isoformat()
        )
    )
    unplanned = harness.campaign(RUN_HARM, "No plan", plan=None)
    dated_upload = upload(harness.client, dated, name="dated.csv")
    ok(
        harness.client.post(
            f"/campaigns/{unplanned}/outcomes",
            json={
                "upload_id": dated_upload,
                "outcome_column": "converted",
                "treatment_date_column": "treatment_date",
            },
        )
    )
    refused = harness.client.post(f"/campaigns/{unplanned}/measure", json={})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "CAMPAIGN_NOT_MATURED"
    none = harness.check(unplanned)
    assert none is not None and (none.status, none.not_checked) == ("not_checked", "no_plan")
    assert len(harness.alerts()) == 1


def test_an_amended_plan_sets_a_standing_alarm_aside(harness: Harness) -> None:
    campaign_id = _harmful_campaign(harness)
    harness.firer().fire(harness.measure_schedule()) or pytest.fail("not fired")
    assert len(_card(harness.summary(), campaign_id)) == 1
    amended = {
        "metric": "Came back",
        "outcome_column": "converted",
        "analysis_date": LOOP_ANALYSIS,
        "mde_pp": 10.0,
        "base_rate": BASE_RATE,
        "reason": "The smallest effect worth acting on was set too low.",
    }
    ok(harness.client.post(f"/campaigns/{campaign_id}/plan/amendments", json=amended), 201)
    assert (
        _card(harness.summary(), campaign_id) == []
    ), "evidence raised under another plan is not this plan's"


def test_the_card_is_traced_and_disappears_once_the_final_result_is_read(harness: Harness) -> None:
    campaign_id = _harmful_campaign(harness)
    harness.firer().fire(harness.measure_schedule()) or pytest.fail("not fired")
    storage = harness.world.storage
    campaigns = list(harness.campaigns.list(limit=None))
    summary = build_summary(
        storage, campaigns, root=harness.root, now=CLOCK
    )  # raises unless every figure traces
    assert [card.code for card in summary.cards if card.campaign_id == campaign_id].count(HARM_ALARM) == 1
    # The final reading, on the analysis date: the verdict speaks now, and the alarm's card goes.
    harness.world.clock.now = datetime.combine(date.fromisoformat(LOOP_ANALYSIS), CLOCK.timetz())
    final = harness.firer().fire(harness.measure_schedule_stored())
    assert final is not None and final.result_code == "CAMPAIGN_MEASURED", final
    after = build_summary(storage, list(harness.campaigns.list(limit=None)), root=harness.root, now=CLOCK)
    assert [card.code for card in after.cards if card.campaign_id == campaign_id].count(HARM_ALARM) == 0
    check = harness.check(campaign_id)
    assert check is not None and check.checked_at == CLOCK, "a final result is never checked"


# --- inside the outcome window (review fix: the loop reads immature data) --------------------------------------
def test_inside_the_outcome_window_the_loop_reads_what_is_in_so_far_and_stores_nothing_else(
    harness: Harness,
) -> None:
    """The natural plan: the analysis date is the day the window closes, so every reading before it is mid-window."""
    run = propensity_run(harness.world.storage, RUN_HARM, rows=8_000, seed=1384)
    closes = SENT + timedelta(days=WINDOW)
    campaign_id = harness.campaign(
        RUN_HARM,
        "Harmful, read mid-window",
        plan={"analysis_date": closes.date().isoformat(), "mde_pp": MDE_PP, "base_rate": BASE_RATE},
    )
    harness.into_crm(_outcomes(run.scores, HARM, seed=13804))  # recorded on day 12 of the window
    schedule = harness.measure_schedule()
    storage = harness.world.storage
    uploads_before = sorted(storage.list_keys("uploads/"))

    # Day 5: nothing is recorded yet. The reading is checked, finds too few customers, and raises nothing.
    harness.world.clock.now = SENT + timedelta(days=5)
    early = harness.firer().fire(schedule) or pytest.fail("not fired")
    assert (early.status, early.result_code) == (FiringStatus.SUCCEEDED, "CAMPAIGN_NOT_MATURED"), early
    quiet = harness.check(campaign_id)
    assert quiet is not None and quiet.status == "no_alarm" and quiet.early_look is True
    (look,) = quiet.looks
    assert (look.treated_rows, look.control_rows, look.fired) == (0, 0, False) and look.reason is not None
    assert harness.alerts() == ()

    # Day 15: the outcomes recorded so far show the harm. The card and one alert; nothing else is stored.
    harness.world.clock.now = SENT + timedelta(days=15)
    firing = harness.firer().fire(schedule) or pytest.fail("not fired")
    assert (firing.status, firing.result_code) == (FiringStatus.SUCCEEDED, "CAMPAIGN_NOT_MATURED"), firing
    check = harness.check(campaign_id)
    assert check is not None and check.status == "alarm" and check.early_look is True
    assert check.as_of == SENT + timedelta(days=15) and check.analysis_date == closes.date()
    (alarm,) = check.alarms
    assert alarm.difference < 0 and alarm.treated_rows > 0 and alarm.control_rows > 0
    (alert,) = harness.alerts()
    assert campaign_id in alert.message and alert.schedule_id == schedule.schedule_id
    assert f"read only on {closes.date().isoformat()}" in alert.message
    (card,) = _card(harness.summary(), campaign_id)
    facts = {fact["label"]: fact["value"] for fact in card["facts"]}
    assert facts["Day the final result can be read"]["value"] == closes.date().isoformat()
    assert "Offer" not in facts, "a campaign of one treated group names no offer"
    files = {key.rsplit("/", 1)[-1] for key in storage.list_keys(f"campaigns/{campaign_id}/")}
    assert HARM_ALARM_FILENAME in files
    assert not files & {"incrementality_report.json", "outcomes.parquet", "segment_effects.json"}, files
    assert sorted(storage.list_keys("uploads/")) == uploads_before, "no copy of the client's rows is kept"
    stored = harness.campaigns.get(campaign_id)
    assert stored is not None and stored.status.value == "live" and stored.outcomes is None

    # Day 16: the alarm stands and raises no second alert.
    harness.world.clock.now = SENT + timedelta(days=16)
    again = harness.firer().fire(schedule) or pytest.fail("not fired")
    assert again.result_code == "CAMPAIGN_NOT_MATURED"
    later = harness.check(campaign_id)
    assert later is not None and later.alarms == check.alarms and len(harness.alerts()) == 1

    # The window closes on the analysis date: the final result is read, and the card goes.
    harness.world.clock.now = closes + timedelta(hours=1)
    final = harness.firer().fire(harness.measure_schedule_stored()) or pytest.fail("not fired")
    assert final.result_code == "CAMPAIGN_MEASURED", final
    assert _card(harness.summary(), campaign_id) == []
    last = harness.check(campaign_id)
    assert last is not None and last.checked_at == SENT + timedelta(
        days=16
    ), "a final result is never checked"


# --- the card follows the setting and names the offer ----------------------------------------------------------
def _set_enabled(root: Path, enabled: bool) -> None:
    path = root / "measurement.yaml"
    text = path.read_text(encoding="utf-8")
    old, new = ("  enabled: false", "  enabled: true") if enabled else ("  enabled: true", "  enabled: false")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_the_card_follows_the_setting_and_names_the_offer_of_a_campaign_of_several(harness: Harness) -> None:
    campaign_id = _harmful_campaign(harness)
    harness.firer().fire(harness.measure_schedule()) or pytest.fail("not fired")
    assert len(_card(harness.summary(), campaign_id)) == 1

    # Turned off after the alarm fired: the file stays, but no card is shown.
    _set_enabled(harness.root, False)
    assert harness.check(campaign_id) is not None
    assert _card(harness.summary(), campaign_id) == []
    _set_enabled(harness.root, True)

    # An alarm raised for one offer of several names that offer on the card.
    key = f"campaigns/{campaign_id}/{HARM_ALARM_FILENAME}"
    document = json.loads(harness.world.storage.read_bytes(key))
    document["alarms"][0]["arm"] = "second_offer"
    harness.world.storage.write_bytes(key, json.dumps(document).encode())
    (card,) = _card(harness.summary(), campaign_id)
    facts = {fact["label"]: fact["value"] for fact in card["facts"]}
    assert facts["Offer"]["text"] == "second_offer"
    assert facts["Offer"]["sources"] == [{"artefact": key, "field": "alarms.0.arm"}]


# --- a settings file that cannot be read --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("harm_alarm: [not closed\n", "CONFIG_YAML_ERROR"),
        ("- harm_alarm\n", "CONFIG_NOT_A_MAPPING"),
        ("harm_alarm: true\n", "CONFIG_INVALID"),
        ("pooling:\n  enabled: false\nharm_alarm:\n  enable: false\n", "CONFIG_INVALID"),
    ],
    ids=["not-yaml", "not-a-mapping", "section-not-a-mapping", "typo"],
)
def test_a_settings_file_that_cannot_be_read_refuses_the_measurement_before_anything_is_written(
    harness_off: Harness, text: str, code: str
) -> None:
    harness = harness_off
    run = propensity_run(harness.world.storage, RUN_HARM, rows=2_000, seed=1385)
    later = (utc_now() + timedelta(days=60)).date().isoformat()
    campaign_id = harness.campaign(
        RUN_HARM, "Bad settings", plan={"analysis_date": later, "mde_pp": MDE_PP, "base_rate": BASE_RATE}
    )
    uploaded = upload(harness.client, _outcomes(run.scores, HARM, seed=13805))
    ok(
        harness.client.post(
            f"/campaigns/{campaign_id}/outcomes", json={"upload_id": uploaded, "outcome_column": "converted"}
        )
    )
    (harness.root / "measurement.yaml").write_text(text, encoding="utf-8")
    storage = harness.world.storage
    before = sorted(storage.list_keys(f"campaigns/{campaign_id}/"))

    refused = harness.client.post(f"/campaigns/{campaign_id}/measure", json={})
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == code
    assert sorted(storage.list_keys(f"campaigns/{campaign_id}/")) == before, "nothing written"

    harness.into_crm(_outcomes(run.scores, HARM, seed=13806))
    firing = harness.firer().fire(harness.measure_schedule()) or pytest.fail("not fired")
    assert (firing.status, firing.error_code) == (FiringStatus.FAILED, code), firing
    assert sorted(storage.list_keys(f"campaigns/{campaign_id}/")) == before, "nothing written"
