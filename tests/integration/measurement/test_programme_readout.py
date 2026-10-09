"""Plan J M103 through the API: the programme readout (`POST /campaigns/programme`).

The whole customer base's outcomes over a period are split by the universal holdout - the same salted rule
every scoring run used - and everyone not held back is compared with the members (intent to treat):

* the split is the scoring run's: the customers a real Phase 1 scoring run (`apply_actions` under the
  universal holdout) held back are the readout's holdout members, to the customer;
* a simulated programme effect is recovered inside the interval, with and without a contact file (which
  also recovers the effect on the contacted);
* an amount and the adjusted estimate (M102) work as for any campaign, but only with a plan registered in the
  same request, before the outcomes are read;
* no universal holdout (or a salt that is not the one it was drawn with) is a 409, a period still running a
  409 `CAMPAIGN_NOT_MATURED`, and the readout lists beside the other campaigns and measures again.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.main import create_app
from engine.config import load_use_case
from engine.holdout.assign import holdout_context, member_flags
from engine.holdout.salt import HoldoutLedger, resolve_holdout, salt_fingerprint
from engine.holdout.spec import HoldoutConfig
from engine.keys import key_text
from engine.pilot.plain import jargon_in
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.settings import Settings
from engine.stages.actions import apply_actions
from engine.storage import LocalStorage
from tests.fixtures.make_uplift_data import make_winback_campaign
from tests.integration.measurement.support import SENT, USE_CASE, ok, upload

pytestmark = pytest.mark.integration

SALT = "programme-test-salt-0001"
FRACTION = 0.10
ROWS = 30_000
PERIOD = {"start": "2026-01-01", "end": "2026-03-31"}
KEY = "customer_id"


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    data_dir: Path
    keys: pd.Series
    members: np.ndarray
    scored: pd.DataFrame


def _app(config_root: Path, data_dir: Path, *, salt: str | None = SALT) -> Any:
    app = create_app(config_root=config_root, data_dir=data_dir)
    app.state.settings = Settings(data_dir=data_dir, holdout_salt=None if salt is None else SecretStr(salt))
    return app


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    """A universal holdout recorded by a real scoring pass over `ROWS` customers (the Phase 1 actions stage)."""
    data_dir = tmp_path_factory.mktemp("programme") / "data"
    data_dir.mkdir()
    settings = Settings(data_dir=data_dir, holdout_salt=SecretStr(SALT))
    config = load_use_case(USE_CASE)
    config = config.model_copy(
        update={
            "actions": config.actions.model_copy(
                update={"holdout": HoldoutConfig(scope="universal", fraction=FRACTION)}
            )
        }
    )
    engine = sqlite_engine(data_dir / PLATFORM_DB_FILENAME)
    resolved = resolve_holdout(config, settings, engine, at=SENT, record=True)
    campaign = make_winback_campaign(ROWS, seed=17)
    frame = campaign.frame.drop(columns=["reactivated_90d", "treatment", "treatment_date"])
    frame["marketing_opt_in"] = [index % 10 != 3 for index in range(ROWS)]
    frame[config.actions.score_field] = campaign.truth["p_treated"].to_numpy()
    with holdout_context(resolved.active):
        scored = apply_actions(frame, config, run_id="r_20261009_10330001", primary_key=KEY, now=SENT)
    members = member_flags(
        key_text(scored[KEY]).tolist(), salt=SALT, scope_key="universal", fraction=FRACTION
    )
    with TestClient(_app(config_root, data_dir)) as client:
        yield World(client, LocalStorage(data_dir), data_dir, scored[KEY], members, scored)


def _outcomes(
    world: World, *, seed: int, contacted_share: float = 0.6, lift: float = 0.05
) -> tuple[pd.DataFrame, np.ndarray]:
    """Outcomes of a programme that reached `contacted_share` of the customers outside the holdout.

    The true intent-to-treat effect is `contacted_share * lift`; the effect on a contacted customer is `lift`.
    """
    rng = np.random.default_rng(seed)
    n = len(world.keys)
    contacted = (~world.members) & (rng.random(n) < contacted_share)
    converted = rng.random(n) < 0.10 + lift * contacted
    frame = pd.DataFrame({KEY: world.keys, "converted": converted.astype(int)})
    return frame, contacted


def _body(world: World, outcomes: pd.DataFrame, **extra: Any) -> dict[str, Any]:
    return {
        "period": PERIOD,
        "primary_key": KEY,
        "outcome": {
            "upload_id": upload(world.client, outcomes, name="programme_outcomes.csv"),
            "outcome_column": "converted",
        },
        **extra,
    }


# --- the split is the scoring run's ----------------------------------------------------------------------
def test_the_programme_is_split_exactly_as_the_scoring_run_held_customers_back(world: World) -> None:
    outcomes, _ = _outcomes(world, seed=1)
    view = ok(world.client.post("/campaigns/programme", json=_body(world, outcomes)), 201)
    scored = world.scored
    # the actions stage's control group is the eligible members: the readout's holdout is every member
    assert int(scored["control_group"].sum()) <= int(world.members.sum())
    assert bool(
        (
            (scored["control_group"]).to_numpy()
            == (world.members & scored["suppressed_reason"].isna().to_numpy())
        ).all()
    )
    programme = view["programme"]
    assert programme["holdout_members"] == int(world.members.sum())
    assert programme["other_customers"] == ROWS - int(world.members.sum())
    assert programme["customers"] == ROWS
    assert programme["holdout_epoch"] == 1 and programme["holdout_fraction"] == FRACTION
    assert programme["salt_id"] == salt_fingerprint(SALT)[:16]
    assert SALT not in str(view)
    assert abs(programme["realised_share"] - FRACTION) < 0.012, "binomial around the fraction"
    campaign = view["campaign"]
    assert (campaign["kind"], campaign["holdout_scope"], campaign["holdout_scope_key"]) == (
        "programme",
        "universal",
        "universal",
    )
    assert campaign["causal"] is True and campaign["causal_basis"] == "engine_random"
    assert campaign["run_ids"] == [] and view["report"]["run_id"] == campaign["campaign_id"]


# --- a simulated programme effect is recovered -------------------------------------------------------------
def test_a_simulated_programme_effect_is_recovered_inside_the_interval(world: World) -> None:
    outcomes, contacted = _outcomes(world, seed=2, contacted_share=0.6, lift=0.05)
    contacts = pd.DataFrame({KEY: world.keys, "contacted": contacted.astype(int)})
    body = _body(
        world,
        outcomes,
        contact={
            "upload_id": upload(world.client, contacts, name="programme_contacts.csv"),
            "contacted_column": "contacted",
        },
    )
    view = ok(world.client.post("/campaigns/programme", json=body), 201)
    lift = view["report"]["absolute_lift"]
    true_itt = 0.6 * 0.05
    assert lift["ci_low"] <= true_itt <= lift["ci_high"], (lift, true_itt)
    assert view["report"]["causal"] is True and view["verdict"]["kind"] == "added"
    assert view["programme"]["label"] == "Causal, whole programme"
    assert "not of one message" in view["programme"]["explanation"]
    # the same programme, read as what it did to the customers it reached
    readout = view["contacts"]
    assert readout["contamination"] == 0.0 and readout["treated_contacted"] == int(contacted.sum())
    assert readout["contact_rate"] == int(contacted.sum()) / (ROWS - int(world.members.sum()))
    effect = readout["complier"]["effect"]
    assert effect["ci_low"] <= 0.05 <= effect["ci_high"], effect


def test_the_period_sets_the_outcome_window_and_a_programme_still_running_is_not_measured(
    world: World,
) -> None:
    outcomes, _ = _outcomes(world, seed=3)
    before = len(world.client.get("/campaigns").json()["campaigns"])
    running = {"start": "2099-01-01", "end": "2099-03-31"}
    response = world.client.post("/campaigns/programme", json={**_body(world, outcomes), "period": running})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["detail"]["code"] == "CAMPAIGN_NOT_MATURED"
    assert body["results_available_on"] == "2099-04-01", "the day after the period ends"
    assert len(world.client.get("/campaigns").json()["campaigns"]) == before, "nothing stored"
    done = ok(world.client.post("/campaigns/programme", json=_body(world, outcomes)), 201)
    assert done["campaign"]["outcome_window_days"] == 90  # 1 Jan to 31 Mar, both days counted
    assert done["campaign"]["treatment_start"].startswith("2026-01-01")
    backwards = world.client.post(
        "/campaigns/programme",
        json={**_body(world, outcomes), "period": {"start": "2026-03-01", "end": "2026-02-01"}},
    )
    assert backwards.status_code == 422


# --- an amount, and the adjusted estimate -----------------------------------------------------------------------
def _revenue(world: World, *, seed: int, rho: float = 0.6, effect: float = 6.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(world.keys)
    before = rng.standard_normal(n)
    after = rho * before + np.sqrt(1.0 - rho * rho) * rng.standard_normal(n)
    revenue = 100.0 + 40.0 * after + np.where(world.members, 0.0, effect)
    return pd.DataFrame(
        {
            KEY: world.keys,
            "revenue": np.round(revenue, 2),
            "pre_revenue": np.round(100.0 + 40.0 * before, 2),
            "pre_until": "2025-12-31",
        }
    )


def _plan() -> dict[str, Any]:
    return {
        "metric": "revenue per customer over the quarter",
        "outcome_column": "revenue",
        "outcome_kind": "continuous",
        "covariate_column": "pre_revenue",
        "analysis_date": "2026-04-01",
        "mde_value": 4.0,
        "outcome_sd": 40.0,
        "expected_rho2": 0.36,
    }


def test_an_amount_is_adjusted_by_the_planned_earlier_amount_only_with_a_plan(world: World) -> None:
    revenue = _revenue(world, seed=4)
    outcome = {
        "upload_id": upload(world.client, revenue, name="programme_revenue.csv"),
        "outcome_column": "revenue",
        "outcome_kind": "continuous",
        "covariate_column": "pre_revenue",
        "covariate_date_column": "pre_until",
    }
    before = len(world.client.get("/campaigns").json()["campaigns"])
    unplanned = world.client.post(
        "/campaigns/programme", json={"period": PERIOD, "primary_key": KEY, "outcome": outcome}
    )
    assert unplanned.status_code == 409, unplanned.text
    assert unplanned.json()["detail"]["code"] == "TEST_PLAN_CHANGED"
    assert len(world.client.get("/campaigns").json()["campaigns"]) == before, "nothing stored"

    view = ok(
        world.client.post(
            "/campaigns/programme",
            json={"period": PERIOD, "primary_key": KEY, "outcome": outcome, "plan": _plan()},
        ),
        201,
    )
    report = view["report"]
    assert report["outcome_kind"] == "continuous" and report["covariate_column"] == "pre_revenue"
    assert abs(report["variance_reduction"] - 0.36) < 0.05, report["variance_reduction"]
    plain, adjusted = report["mean_difference_ci"], report["adjusted_interval"]
    assert adjusted["ci_high"] - adjusted["ci_low"] < plain["ci_high"] - plain["ci_low"]
    assert adjusted["ci_low"] <= 6.0 <= adjusted["ci_high"]
    assert plain["ci_low"] <= 6.0 <= plain["ci_high"]
    plan = view["plan"]
    assert plan["plan_hash"] == report["test_plan_hash"] == view["campaign"]["test_plan_hash"]
    assert plan["campaign_id"] == view["campaign"]["campaign_id"]
    # the plan is on the campaign's page and in its history, as for any campaign
    plans = ok(world.client.get(f"/campaigns/{view['campaign']['campaign_id']}/plan"))
    assert [p["version"] for p in plans["versions"]] == [1]
    again = ok(world.client.post(f"/campaigns/{view['campaign']['campaign_id']}/measure", json={}))
    assert again["report"]["adjusted_interval"] == report["adjusted_interval"]


def test_a_covariate_dated_after_the_period_started_is_refused(world: World) -> None:
    revenue = _revenue(world, seed=5).assign(pre_until="2026-01-01")  # the first day of the period: too late
    outcome = {
        "upload_id": upload(world.client, revenue, name="programme_revenue_late.csv"),
        "outcome_column": "revenue",
        "outcome_kind": "continuous",
        "covariate_column": "pre_revenue",
        "covariate_date_column": "pre_until",
    }
    response = world.client.post(
        "/campaigns/programme",
        json={"period": PERIOD, "primary_key": KEY, "outcome": outcome, "plan": _plan()},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "COVARIATE_NOT_BEFORE_CAMPAIGN"


# --- no holdout to read against ----------------------------------------------------------------------------------
def test_without_a_universal_holdout_or_with_another_salt_nothing_is_computed(
    config_root: Path, tmp_path: Path, world: World
) -> None:
    outcomes, _ = _outcomes(world, seed=6)
    data_dir = tmp_path / "empty"
    data_dir.mkdir()
    for salt in (SALT, None):
        with TestClient(_app(config_root, data_dir, salt=salt)) as client:
            body = {
                "period": PERIOD,
                "primary_key": KEY,
                "outcome": {"upload_id": upload(client, outcomes), "outcome_column": "converted"},
            }
            response = client.post("/campaigns/programme", json=body)
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "PROGRAMME_NO_HOLDOUT"
            assert "universal" in response.json()["detail"]["message"]
    other = tmp_path / "other"
    other.mkdir()
    engine = sqlite_engine(other / PLATFORM_DB_FILENAME)
    config = load_use_case(USE_CASE)
    config = config.model_copy(
        update={
            "actions": config.actions.model_copy(
                update={"holdout": HoldoutConfig(scope="universal", fraction=FRACTION)}
            )
        }
    )
    resolve_holdout(
        config, Settings(data_dir=other, holdout_salt=SecretStr(SALT)), engine, at=SENT, record=True
    )
    assert HoldoutLedger(engine).entry("universal", "universal") is not None
    with TestClient(_app(config_root, other, salt="another-salt-entirely-01")) as client:
        body = {
            "period": PERIOD,
            "primary_key": KEY,
            "outcome": {"upload_id": upload(client, outcomes), "outcome_column": "converted"},
        }
        response = client.post("/campaigns/programme", json=body)
        assert response.status_code == 409 and response.json()["detail"]["code"] == "HOLDOUT_SALT_CHANGED"


def test_a_programme_lists_with_the_campaigns_and_every_sentence_is_plain(world: World) -> None:
    outcomes, _ = _outcomes(world, seed=7)
    view = ok(
        world.client.post("/campaigns/programme", json={**_body(world, outcomes), "name": "All of Q1"}), 201
    )
    listed = ok(world.client.get("/campaigns"))["campaigns"]
    found = next(c for c in listed if c["campaign_id"] == view["campaign"]["campaign_id"])
    assert (found["name"], found["kind"]) == ("All of Q1", "programme")
    for sentence in (
        view["programme"]["explanation"],
        view["programme"]["label"],
        *view["programme"]["notes"],
        view["report"]["summary"],
    ):
        assert jargon_in(sentence) == (), sentence
    now = datetime.now(UTC)
    assert datetime.fromisoformat(view["programme"]["computed_at"]) <= now
