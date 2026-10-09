"""Plan J M103 through the API: the programme readout (`POST /campaigns/programme`).

The whole customer base's outcomes over a period are split by the universal holdout - the same salted rule
every scoring run used - and everyone not held back is compared with the members (intent to treat):

* the split is the scoring run's: the customers a real Phase 1 scoring run (`apply_actions` under the
  universal holdout) held back are the readout's holdout members, to the customer;
* a simulated programme effect is recovered inside the interval, with and without a contact file (which
  also recovers the effect on the contacted);
* an amount is read as a plain difference in means: the adjusted estimate (M102) needs a plan registered before
  the outcomes are read, and a programme exists only once its period is over, so a plan or an earlier-amount
  column is refused (409 `TEST_PLAN_INVALID`);
* a universal holdout started or redrawn after the period began cannot say who was held back in it (409
  `CAMPAIGN_EPOCH_MISMATCH`); lists made without the universal holdout during the period
  and a share of held-back customers far from the rule's are said in the notes;
* no universal holdout (or a salt that is not the one it was drawn with) is a 409, a period still running a
  409 `CAMPAIGN_NOT_MATURED`, and the readout lists beside the other campaigns and measures again (an amount
  is still an amount).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from engine.holdout.salt import HoldoutLedger, resolve_holdout, salt_fingerprint, start_epoch
from engine.holdout.spec import HoldoutAssignmentReport, HoldoutConfig, HoldoutSpec
from engine.keys import key_text
from engine.pilot.plain import jargon_in
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.settings import Settings
from engine.stages.actions import apply_actions
from engine.storage import LocalStorage, run_key
from tests.fixtures.make_uplift_data import make_winback_campaign
from tests.integration.measurement.support import SENT, USE_CASE, ok, record, upload

pytestmark = pytest.mark.integration

SALT = "programme-test-salt-0001"
FRACTION = 0.10
ROWS = 30_000
PERIOD = {"start": "2026-01-01", "end": "2026-03-31"}
BEFORE = datetime(2025, 12, 15, 9, 0, tzinfo=UTC)
"""When the universal holdout was first used: before the period, as a programme read needs."""
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
    resolved = resolve_holdout(config, settings, engine, at=BEFORE, record=True)
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


def _amount_outcome(world: World, *, seed: int, covariate: bool = False) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "upload_id": upload(world.client, _revenue(world, seed=seed), name=f"programme_revenue_{seed}.csv"),
        "outcome_column": "revenue",
        "outcome_kind": "continuous",
    }
    if covariate:
        outcome |= {"covariate_column": "pre_revenue", "covariate_date_column": "pre_until"}
    return outcome


def test_an_amount_is_a_plain_difference_in_means_and_is_still_an_amount_when_measured_again(
    world: World,
) -> None:
    view = ok(
        world.client.post(
            "/campaigns/programme",
            json={"period": PERIOD, "primary_key": KEY, "outcome": _amount_outcome(world, seed=4)},
        ),
        201,
    )
    report = view["report"]
    assert report["outcome_kind"] == "continuous" and view["programme"]["outcome_kind"] == "continuous"
    assert report.get("adjusted_interval") is None and report.get("covariate_column") is None
    plain = report["mean_difference_ci"]
    assert plain["ci_low"] <= 6.0 <= plain["ci_high"]
    assert view["verdict"]["kind"] == "added" and view["plan"] is None
    # "Measure now" on the campaign's page posts nothing: the amount must not be read as yes/no
    again = ok(world.client.post(f"/campaigns/{view['campaign']['campaign_id']}/measure", json={}))
    assert again["report"]["outcome_kind"] == "continuous"
    assert again["report"]["mean_difference_ci"] == plain


def test_a_plan_or_an_earlier_amount_is_refused_because_a_finished_period_cannot_pre_register_one(
    world: World,
) -> None:
    before = len(world.client.get("/campaigns").json()["campaigns"])
    outcome = _amount_outcome(world, seed=5, covariate=True)
    for extra, path in (
        ({"plan": _plan()}, "plan"),  # the plan arrives with outcomes that already exist
        ({}, "outcome.covariate_column"),  # a covariate named without a plan
    ):
        response = world.client.post(
            "/campaigns/programme", json={"period": PERIOD, "primary_key": KEY, "outcome": outcome, **extra}
        )
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "TEST_PLAN_INVALID" and detail["path"] == path
        assert "could not have been fixed" in detail["message"]
    assert len(world.client.get("/campaigns").json()["campaigns"]) == before, "nothing stored"
    # re-posting with other covariate columns cannot shop for the narrowest interval: none is ever used
    plain = ok(
        world.client.post(
            "/campaigns/programme",
            json={"period": PERIOD, "primary_key": KEY, "outcome": _amount_outcome(world, seed=5)},
        ),
        201,
    )
    assert plain["report"].get("adjusted_interval") is None


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


# --- the holdout must have been in force for the whole period -------------------------------------------------------
def _universal_config() -> Any:
    config = load_use_case(USE_CASE)
    return config.model_copy(
        update={
            "actions": config.actions.model_copy(
                update={"holdout": HoldoutConfig(scope="universal", fraction=FRACTION)}
            )
        }
    )


def _record_holdout(data_dir: Path, *, at: datetime, salt: str = SALT, fraction: float = FRACTION) -> Any:
    """A data directory whose universal holdout was first used at `at`."""
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = sqlite_engine(data_dir / PLATFORM_DB_FILENAME)
    config = _universal_config()
    config = config.model_copy(
        update={
            "actions": config.actions.model_copy(
                update={"holdout": HoldoutConfig(scope="universal", fraction=fraction)}
            )
        }
    )
    resolve_holdout(
        config, Settings(data_dir=data_dir, holdout_salt=SecretStr(salt)), engine, at=at, record=True
    )
    return engine


def _post_binary(client: TestClient, world: World, **extra: Any) -> Any:
    outcomes, _ = _outcomes(world, seed=11)
    period = extra.pop("period", PERIOD)
    return client.post(
        "/campaigns/programme",
        json={
            "period": period,
            "primary_key": KEY,
            "outcome": {"upload_id": upload(client, outcomes), "outcome_column": "converted"},
            **extra,
        },
    )


def test_a_holdout_first_used_after_the_period_began_cannot_say_who_was_held_back_in_it(
    config_root: Path, tmp_path: Path, world: World
) -> None:
    data_dir = tmp_path / "late"
    _record_holdout(data_dir, at=SENT)  # 1 May 2026: after the first quarter began
    with TestClient(_app(config_root, data_dir)) as client:
        response = _post_binary(client, world)
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "CAMPAIGN_EPOCH_MISMATCH"
        assert "2026-05-01" in detail["message"] and "2026-01-01" in detail["message"]
        assert jargon_in(detail["message"]) == (), detail["message"]
        assert client.get("/campaigns").json()["campaigns"] == [], "nothing stored"
        # a period that began after the holdout was first used is read
        later = {"start": "2026-06-01", "end": "2026-08-31"}
        ok(_post_binary(client, world, period=later), 201)


def test_a_salt_rotated_during_the_period_is_refused_because_the_old_draw_cannot_be_found(
    config_root: Path, tmp_path: Path, world: World
) -> None:
    data_dir = tmp_path / "rotated"
    engine = _record_holdout(data_dir, at=BEFORE)
    new_salt = "programme-test-salt-0002"
    start_epoch(
        HoldoutLedger(engine),
        scope="universal",
        key="universal",
        fraction=FRACTION,
        settings=Settings(data_dir=data_dir, holdout_salt=SecretStr(new_salt)),
        rotate_salt=True,
        at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC),
    )
    entry = HoldoutLedger(engine).entry("universal", "universal")
    assert entry is not None and entry.epoch == 2
    with TestClient(_app(config_root, data_dir, salt=new_salt)) as client:
        response = _post_binary(client, world)
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "CAMPAIGN_EPOCH_MISMATCH" and "2026-02-10" in detail["message"]
        assert client.get("/campaigns").json()["campaigns"] == []


# --- what the universal holdout does not promise -----------------------------------------------------------------
def test_lists_made_without_the_universal_holdout_in_the_period_are_named_and_the_wording_does_not_overreach(
    config_root: Path, tmp_path: Path, world: World
) -> None:
    data_dir = tmp_path / "contaminated"
    _record_holdout(data_dir, at=BEFORE)
    storage = LocalStorage(data_dir)

    def run(run_id: str, finished: datetime, scope: str | None, use_case: str) -> None:
        storage.write_model(
            run_key(run_id, "run.json"),
            record(run_id, problem_type=world_problem(), rows=1_000).model_copy(
                update={"finished_at": finished, "use_case_id": use_case}
            ),
        )
        if scope is None:  # a default run wrote no holdout file: it drew its own control group
            return
        storage.write_model(
            run_key(run_id, "holdout_assignment.json"),
            HoldoutAssignmentReport(
                run_id=run_id,
                use_case_id=use_case,
                spec=HoldoutSpec(
                    scope=scope,  # type: ignore[arg-type]
                    fraction=FRACTION,
                    salt_id="0123456789abcdef",
                    epoch=1,
                    scope_key="universal" if scope == "universal" else use_case,
                ),
                rows=1_000,
                holdout_members=100,
                control_rows=90,
                explore_candidates=0,
                explore_rows=0,
                created_at=finished,
            ),
        )

    inside = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)
    run("r_20260201_00000001", inside, "universal", "uses-universal")  # fine
    run("r_20260202_00000002", inside + timedelta(days=1), "use_case", "winback-b")  # contaminates
    run("r_20260203_00000003", inside + timedelta(days=2), None, "spring-promo")  # contaminates
    run("r_20250601_00000004", datetime(2025, 6, 1, tzinfo=UTC), None, "long-ago")  # before the period
    with TestClient(_app(config_root, data_dir)) as client:
        view = ok(_post_binary(client, world), 201)
    programme = view["programme"]
    notes = " ".join(programme["notes"])
    assert "2 lists made in this period (spring-promo, winback-b)" in notes
    assert "uses-universal" not in notes and "long-ago" not in notes
    assert "outside this tool" in notes and "contact file" in notes
    explanation = programme["explanation"]
    assert "every campaign" not in explanation and "every list scored with it" in explanation
    assert "not of one message" in explanation
    for sentence in (*programme["notes"], explanation):
        assert jargon_in(sentence) == (), sentence


def world_problem() -> Any:
    from engine.config import ProblemType

    return ProblemType.BINARY_CLASSIFICATION


def test_a_file_whose_share_of_held_back_customers_is_far_from_the_rule_is_flagged(world: World) -> None:
    outcomes, _ = _outcomes(world, seed=12)
    members = pd.Series(world.members, index=outcomes.index)
    rng = np.random.default_rng(3)
    # a file kept only for customers who "did something": most held-back customers are missing from it
    keep = ~members | (rng.random(len(outcomes)) < 0.25)
    view = ok(world.client.post("/campaigns/programme", json=_body(world, outcomes[keep.to_numpy()])), 201)
    notes = " ".join(view["programme"]["notes"])
    assert "far from what the rule gives" in notes and "may not hold the whole customer base" in notes
    fair = ok(world.client.post("/campaigns/programme", json=_body(world, outcomes)), 201)
    assert "far from what the rule gives" not in " ".join(fair["programme"]["notes"])
    assert jargon_in(notes) == ()
