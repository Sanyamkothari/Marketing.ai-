"""Plan J M105 (DEC-1315) through the product's own API: the warnings and the value proven to date
(`GET /campaigns/summary`).

Every campaign here is built by the real code - the Phase 1 scoring stages and the uplift actions for the
scored ones, `POST /campaigns/audit` over the engine's simulators for the audited ones, the campaign routes
for the plans, outcomes and measurements - never by writing a campaign artefact by hand. The scoring-run
artefacts a card reads (a drift report, the ranking a run used, a waiting challenger) are written by the
functions that write them in the product (`compute_drift`, `decide_ranking`, the registry). What is checked is
the milestone's acceptance list:

* the summary of value proven to date equals the sum of the campaigns' measured lower bounds (computed
  here from the stored reports, independently of the engine), per unit, and is labelled as such;
* a campaign random only by the person's statement, a descriptive-only one, a programme, generated data and an
  unfinished result are never added to it, and the summary says why;
* each card appears only when its condition holds: one test per card, both directions;
* every number is a figure that resolves to a measured artefact, every sentence is plain, nothing is
  drawn from a customer row, and an installation with no campaign shows nothing;
* the route is declared before `/campaigns/{campaign_id}`, so "summary" is not read as an id.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import Metric, load_use_case
from engine.contracts import ModelStatus, ModelVersion
from engine.decide.ranking import decide_ranking, ranking_choice
from engine.measurement.simulate import (
    AS_OF,
    OUTCOME_WINDOW_DAYS,
    multi_arm_campaign,
    population,
)
from engine.pilot.plain import jargon_in
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.stages import register, score
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import RANKING_CHOICE_FILENAME
from engine.uplift.metrics import BaselineInput, compare_with_baselines
from engine.utils.time import utc_now
from tests.fixtures.make_uplift_data import make_winback_campaign
from tests.integration.measurement.support import (
    MATURE,
    USE_CASE,
    ScoredRun,
    ok,
    propensity_run,
    uplift_run,
    upload,
)
from tests.integration.pilot.test_proof_pack import (
    HARMED_BAND,
    NEUTRAL_BAND,
    VALUE_INPUTS,
    _banded_outcomes,
    _figures,
    _random_file,
    assert_traced,
)

pytestmark = pytest.mark.integration

RUN_BANDED = "r_20261010_10500001"
RUN_UPLIFT = "r_20261010_10500002"
RUN_SYNTHETIC = "r_20261010_10500003"
RUN_NO_CONTROL = "r_20261010_10500004"
SIM_START = "2026-04-01T00:00:00Z"
PAST_ANALYSIS = "2026-08-30"
TOKEN = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?")
UNDERPOWERED = {"mde_pp": 0.3, "base_rate": 0.15}
POWERED = {"mde_pp": 8.0, "base_rate": 0.15}


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    data_dir: Path
    banded_run: ScoredRun
    ids: dict[str, str]
    """Campaign ids by what each one is for."""


def _create(client: TestClient, run_id: str, name: str) -> str:
    created = ok(client.post("/campaigns", json={"run_id": run_id, "name": name}), 201)
    return str(created["campaign"]["campaign_id"])


def _add_outcomes(
    client: TestClient, campaign_id: str, outcomes: pd.DataFrame, *, synthetic: bool = False
) -> None:
    payload = outcomes.to_csv(index=False, lineterminator="\n").encode()
    response = client.post(
        "/uploads",
        files={"file": ("outcomes.csv", payload, "text/csv")},
        data={"use_case": USE_CASE, "mode": "score", **({"synthetic": "true"} if synthetic else {})},
    )
    upload_id = ok(response, 201)["upload_id"]
    ok(
        client.post(
            f"/campaigns/{campaign_id}/outcomes", json={"upload_id": upload_id, "outcome_column": "converted"}
        )
    )


def _plan(client: TestClient, campaign_id: str, analysis_date: str, **power: Any) -> dict[str, Any]:
    body = {"metric": "Came back", "outcome_column": "converted", "analysis_date": analysis_date, **power}
    return dict(ok(client.post(f"/campaigns/{campaign_id}/plan", json=body), 201))


def _measure(client: TestClient, campaign_id: str) -> dict[str, Any]:
    return dict(ok(client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": MATURE.isoformat()})))


def _scored(
    client: TestClient,
    run_id: str,
    name: str,
    outcomes: pd.DataFrame | None = None,
    *,
    plan: tuple[str, dict[str, Any]] | None = None,
    synthetic: bool = False,
) -> str:
    campaign_id = _create(client, run_id, name)
    if plan is not None:
        _plan(client, campaign_id, plan[0], **plan[1])
    if outcomes is not None:
        _add_outcomes(client, campaign_id, outcomes, synthetic=synthetic)
        _measure(client, campaign_id)
    return campaign_id


def _audit(
    client: TestClient,
    name: str,
    frame: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    basis: str,
    contacts: pd.DataFrame | None = None,
    arm: dict[str, Any] | None = None,
    outcome_column: str = "converted",
) -> str:
    body: dict[str, Any] = {
        "name": name,
        "primary_key": "customer_id",
        "assignment": {
            "upload_id": upload(client, frame, name="assignment.csv"),
            "arm_column": "group",
            **(arm or {}),
        },
        "outcomes": {
            "upload_id": upload(client, outcomes, name="outcomes.csv"),
            "outcome_column": outcome_column,
            "treatment_date_column": "treatment_date",
        },
        "assignment_basis": basis,
        "treatment_start": SIM_START,
        "outcome_window_days": OUTCOME_WINDOW_DAYS,
        "as_of": AS_OF.isoformat(),
    }
    if contacts is not None:
        body["contact"] = {
            "upload_id": upload(client, contacts, name="contacts.csv"),
            "contacted_column": "contacted",
        }
    return str(ok(client.post("/campaigns/audit", json=body), 201)["campaign"]["campaign_id"])


def _simulated_audit(
    client: TestClient, name: str, seed: int, *, contamination: float, outcome_column: str = "converted"
) -> str:
    """A verified-random audit (the file has unrelated details) with a contact file of known leakage."""
    sim = population(
        8_000, 0.10, 0.04, seed=seed, control_share=0.2, compliance=1.0, contamination=contamination
    )
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {
            "customer_id": sim.scores["customer_id"],
            "group": np.where(sim.scores["control_group"], 0, 1),
            "age": rng.integers(18, 80, len(sim.scores)),
            "region": rng.choice(list("ABCD"), len(sim.scores)),
        }
    )
    contacts = pd.DataFrame(
        {"customer_id": sim.scores["customer_id"], "contacted": sim.received_treatment.astype(int)}
    )
    outcomes = sim.outcomes.rename(columns={"converted": outcome_column})
    return _audit(
        client, name, frame, outcomes, basis="random", contacts=contacts, outcome_column=outcome_column
    )


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("summary") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    banded_run = propensity_run(storage, RUN_BANDED, rows=8_000)
    uplift = uplift_run(storage, RUN_UPLIFT, rows=8_000)
    propensity_run(storage, RUN_SYNTHETIC, rows=4_000, seed=31)
    propensity_run(storage, RUN_NO_CONTROL, rows=4_000, seed=33, control_fraction=0.0)
    synthetic_scores = pd.read_parquet(storage.local_path(run_key(RUN_SYNTHETIC, "scores.parquet")))
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        banded_outcomes = _banded_outcomes(banded_run.scores, seed=21)
        ids: dict[str, str] = {}
        ids["banded"] = _scored(client, RUN_BANDED, "Banded win-back", banded_outcomes)
        ids["banded_again"] = _scored(client, RUN_BANDED, "Banded win-back again", banded_outcomes)
        ids["uplift"] = _scored(
            client,
            RUN_UPLIFT,
            "Uplift win-back",
            uplift.outcomes.rename(columns={"reactivated_90d": "converted"}),
        )
        ok(client.put(f"/pilot/roi/{RUN_UPLIFT}", json=VALUE_INPUTS))
        ids["under_final"] = _scored(
            client, RUN_BANDED, "Small plan, read", banded_outcomes, plan=(PAST_ANALYSIS, UNDERPOWERED)
        )
        ids["under"] = _scored(client, RUN_BANDED, "Small plan, waiting", plan=(PAST_ANALYSIS, UNDERPOWERED))
        ids["powered"] = _scored(client, RUN_BANDED, "Big plan, waiting", plan=(PAST_ANALYSIS, POWERED))
        later = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
        ids["early"] = _scored(client, RUN_BANDED, "Read too soon", banded_outcomes, plan=(later, POWERED))
        ids["waiting"] = _scored(client, RUN_BANDED, "Outcomes not in")
        ids["no_control"] = _scored(client, RUN_NO_CONTROL, "Nobody held back")
        ids["synthetic"] = _scored(
            client,
            RUN_SYNTHETIC,
            "Generated data",
            _banded_outcomes(synthetic_scores, seed=4),
            synthetic=True,
        )
        # Audited campaigns: a verified-random one with a clean contact file, one that leaked, two that cannot be
        # added to the total (stated and descriptive), each as a person would upload it.
        ids["clean"] = _simulated_audit(client, "Clean audit", 10501, contamination=0.0)
        ids["leaky"] = _simulated_audit(client, "Leaky audit", 10502, contamination=0.10)
        sim = multi_arm_campaign(9_000, 0.12, (0.04, -0.07), seed=4105)
        offers = pd.DataFrame(
            {
                "customer_id": sim.scores["customer_id"],
                "group": np.where(sim.scores["control_group"], sim.levels[0], sim.scores["offer"]),
            }
        )
        ids["stated"] = _audit(
            client,
            "Said to be random",
            offers,
            sim.outcomes,
            basis="random",
            arm={"control_value": sim.levels[0], "treated_values": list(sim.levels[1:])},
        )
        file, outcomes = _random_file(43, details=True)
        ids["descriptive"] = _audit(client, "Chosen by hand", file, outcomes, basis="not_random")
        for key in ("clean",):
            ok(client.put(f"/pilot/proof/{ids[key]}/value", json=VALUE_INPUTS))
        yield World(client, storage, data_dir, banded_run, ids)


OUTCOMES = "outcomes:converted"
"""The yes/no total of the outcome column every campaign of this world measures."""
PROVEN = ("under_final", "uplift", "clean", "leaky")
"""The campaigns whose lower bounds are added: random by the engine or verified, final, the client's own, and
the latest measured of those that read the same customers."""
SAME_CUSTOMERS = ("banded", "banded_again")
"""Measured on the same scoring run and outcomes as `under_final`, the latest of the three: their effect is the
same effect, so it is listed apart and added once."""


def _summary(world: World) -> dict[str, Any]:
    return dict(ok(world.client.get("/campaigns/summary")))


def _report(world: World, name: str) -> dict[str, Any]:
    return dict(
        json.loads(world.storage.read_bytes(f"campaigns/{world.ids[name]}/incrementality_report.json"))
    )


def _cards(summary: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [card for card in summary["cards"] if card["code"] == code]


def _card_campaigns(world: World, code: str) -> set[str]:
    names = {identifier: name for name, identifier in world.ids.items()}
    return {names[card["campaign_id"]] for card in _cards(_summary(world), code) if card["campaign_id"]}


def _totals(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {total["unit"]: total for total in summary["proven"]["totals"]}


# --- the route ------------------------------------------------------------------------------------------
def test_the_summary_is_declared_before_the_campaign_route() -> None:
    from api.routes.campaigns import router

    paths = [route.path for route in router.routes if hasattr(route, "path")]
    assert "/campaigns/summary" in paths
    assert paths.index("/campaigns/summary") < paths.index("/campaigns/{campaign_id}")


def test_summary_is_not_read_as_a_campaign_id(world: World) -> None:
    response = world.client.get("/campaigns/summary")
    assert response.status_code == 200, response.text
    assert "cards" in response.json() and "proven" in response.json()
    missing = world.client.get("/campaigns/c_20260101_00000000")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "CAMPAIGN_NOT_FOUND"


def test_reading_it_is_a_viewer_action() -> None:
    from api.access_policy import policy_for
    from engine.access.roles import Role

    policy = policy_for("GET", "/campaigns/summary")
    assert policy is not None and policy.role is Role.VIEWER


def test_an_installation_with_no_campaign_shows_nothing(config_root: Path, tmp_path: Path) -> None:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as client:
        summary = ok(client.get("/campaigns/summary"))
    assert summary["cards"] == []
    assert summary["proven"] == {
        **summary["proven"],
        "totals": [],
        "apart": [],
        "excluded": [],
    }
    assert summary["artefacts"] == []


# --- the value proven to date ----------------------------------------------------------------------------
def test_the_total_equals_the_sum_of_the_campaigns_lower_bounds(world: World) -> None:
    summary = _summary(world)
    totals = _totals(summary)
    expected = sum(_report(world, name)["incremental_conversions"]["ci_low"] for name in PROVEN)
    outcomes = totals[OUTCOMES]
    assert outcomes["total"]["value"] == pytest.approx(expected)
    assert {line["campaign_id"] for line in outcomes["campaigns"]} == {world.ids[name] for name in PROVEN}
    for line in outcomes["campaigns"]:
        name = next(key for key, value in world.ids.items() if value == line["campaign_id"])
        assert line["lower_bound"]["value"] == pytest.approx(
            _report(world, name)["incremental_conversions"]["ci_low"]
        )
    assert outcomes["total"]["text"] == f"{round(expected):,}"


def test_the_total_is_labelled_as_what_it_is(world: World) -> None:
    outcomes = _totals(_summary(world))[OUTCOMES]
    assert outcomes["label"] == (
        f"at least {outcomes['total']['text']} extra converted outcomes, the sum of each campaign's lower bound"
    )
    assert "lower bound" in outcomes["label"] and outcomes["label"].startswith("at least ")


def test_the_money_is_the_sum_of_each_campaigns_net_lower_bound_in_its_own_total(world: World) -> None:
    summary = _summary(world)
    rupees = _totals(summary)["rupees"]
    expected = 0.0
    for name in ("uplift", "clean"):
        report = _report(world, name)
        campaign = json.loads(world.storage.read_bytes(f"campaigns/{world.ids[name]}/campaign.json"))
        spent = (
            campaign["counts"]["intended_treated"] * VALUE_INPUTS["contact_cost"]
            + report["treated_conversions"] * VALUE_INPUTS["offer_cost"]
        )
        expected += report["incremental_conversions"]["ci_low"] * VALUE_INPUTS["value_per_outcome"] - spent
    assert rupees["total"]["value"] == pytest.approx(expected)
    assert {line["campaign_id"] for line in rupees["campaigns"]} == {world.ids["uplift"], world.ids["clean"]}
    assert rupees["total"]["format"] == "inr" and rupees["label"].startswith("at least ₹")
    assert rupees["label"].endswith("the sum of each campaign's lower bound")


def test_conversions_and_rupees_are_never_added_together(world: World) -> None:
    summary = _summary(world)
    totals = _totals(summary)
    assert {OUTCOMES, "rupees"} <= set(totals)
    assert len(totals) == len(summary["proven"]["totals"]), "one total per unit"
    outcomes, rupees = totals[OUTCOMES]["total"], totals["rupees"]["total"]
    assert outcomes["format"] == "count" and rupees["format"] == "inr"
    # No figure of the summary is a sum across the two units: the outcomes total is read from reports alone,
    # the rupees total also from the value inputs that priced them.
    assert all(source["artefact"].endswith("incrementality_report.json") for source in outcomes["sources"])
    assert any(source["artefact"].endswith("pilot_roi_inputs.json") for source in rupees["sources"])


def test_stated_random_and_descriptive_campaigns_are_listed_apart_and_never_added(world: World) -> None:
    summary = _summary(world)
    apart = {line["campaign_id"]: line for line in summary["proven"]["apart"]}
    assert apart[world.ids["stated"]]["kind"] == "stated_random"
    assert apart[world.ids["descriptive"]]["kind"] == "descriptive"
    added = {line["campaign_id"] for total in summary["proven"]["totals"] for line in total["campaigns"]}
    assert world.ids["stated"] not in added and world.ids["descriptive"] not in added
    assert apart[world.ids["stated"]]["lower_bounds"], "what it shows is shown, under its statement"
    assert apart[world.ids["descriptive"]]["lower_bounds"] == [], "a descriptive campaign credits nothing"
    for line in apart.values():
        assert line["reason"] and not jargon_in(line["reason"])


def test_generated_and_unfinished_campaigns_are_excluded_with_their_reason(world: World) -> None:
    summary = _summary(world)
    excluded = {line["campaign_id"]: line for line in summary["proven"]["excluded"]}
    assert excluded[world.ids["synthetic"]]["code"] == "PROOF_SYNTHETIC_DATA"
    for name in ("waiting", "early", "under", "powered"):
        assert excluded[world.ids[name]]["code"] == "PROOF_NOT_MATURE", name
        assert "no final result yet" in excluded[world.ids[name]]["reason"], name
    nobody = excluded[world.ids["no_control"]]
    assert nobody["code"] == "CAMPAIGN_NO_CONTROL", "the page's own card says why; it will never be counted"
    assert nobody["results_available_on"] is None and "never counted" in nobody["reason"]
    assert "no final result yet" not in nobody["reason"], "it cannot become final, so it must not say it may"
    added = {line["campaign_id"] for total in summary["proven"]["totals"] for line in total["campaigns"]}
    assert not added & set(excluded), "an excluded campaign is not in any total"
    assert all(line["reason"] for line in excluded.values())
    assert set(added) == {world.ids[name] for name in PROVEN}


def test_an_unfinished_campaign_says_which_day_its_result_can_be_read(world: World) -> None:
    excluded = {line["campaign_id"]: line for line in _summary(world)["proven"]["excluded"]}
    note = excluded[world.ids["early"]]
    assert note["results_available_on"]["format"] == "date", "the plan's day, for a result read early"
    assert note["results_available_label"] == "Day the final result can be read"
    for line in excluded.values():
        assert (line["results_available_label"] is None) == (line["results_available_on"] is None)


# --- customers are counted once --------------------------------------------------------------------------------
def test_campaigns_on_the_same_customers_add_one_lower_bound_not_one_each(world: World) -> None:
    summary = _summary(world)
    outcomes = _totals(summary)[OUTCOMES]
    one = _report(world, "under_final")["incremental_conversions"]["ci_low"]
    for name in SAME_CUSTOMERS:
        assert _report(world, name)["incremental_conversions"]["ci_low"] == pytest.approx(one), "same data"
    added = [line["campaign_id"] for line in outcomes["campaigns"]]
    assert [world.ids[name] in added for name in ("banded", "banded_again", "under_final")] == [
        False,
        False,
        True,
    ]
    assert len(added) == len(set(added)) == len(PROVEN), "every proven campaign once"
    from_banded_run = [
        line["lower_bound"]["value"]
        for line in outcomes["campaigns"]
        if line["campaign_id"] in {world.ids[n] for n in (*SAME_CUSTOMERS, "under_final")}
    ]
    assert from_banded_run == [pytest.approx(one)], "three campaigns on one run add exactly one lower bound"
    apart = {line["campaign_id"]: line for line in summary["proven"]["apart"]}
    for name in SAME_CUSTOMERS:
        line = apart[world.ids[name]]
        assert line["kind"] == "same_customers" and line["lower_bounds"] == []
        assert line["counted_as_id"] == world.ids["under_final"]
        assert line["counted_as"]["text"] == "Small plan, read"
        assert "same customers" in line["reason"] and not jargon_in(line["reason"])


def test_a_campaign_on_a_different_run_or_audit_file_is_still_added(world: World) -> None:
    added = {line["campaign_id"] for line in _totals(_summary(world))[OUTCOMES]["campaigns"]}
    assert {world.ids["uplift"], world.ids["clean"], world.ids["leaky"]} <= added, "other customers"


# --- every number is traced, in plain words, with no customer row --------------------------------------------
def test_every_number_resolves_to_a_measured_artefact_field(world: World) -> None:
    summary = _summary(world)
    assert_traced(world.data_dir, summary)
    assert list(_figures(summary)), "the summary prints numbers"
    assert all(key.endswith(".json") for key in summary["artefacts"])
    assert not [key for key in summary["artefacts"] if "assignment" in key or "outcomes" in key]


def _free_strings(item: Any, key: str = "") -> Iterator[str]:
    if isinstance(item, dict):
        if {"value", "text", "format", "sources"} <= set(item):
            return
        for name, value in item.items():
            if name in {
                "campaign_id",
                "use_case_id",
                "artefacts",
                "read",
                "unit",
                "code",
                "kind",
                "counted_as_id",
                "built_at",
            }:
                continue
            yield from _free_strings(value, name)
    elif isinstance(item, list):
        for value in item:
            yield from _free_strings(value, key)
    elif isinstance(item, str):
        yield item


def test_every_digit_in_the_summarys_words_is_printed_by_a_figure_and_the_words_are_plain(
    world: World,
) -> None:
    summary = _summary(world)
    printed = {token for figure in _figures(summary) for token in TOKEN.findall(figure["text"])}
    for text in _free_strings(summary):
        assert set(TOKEN.findall(text)) <= printed, text
        assert jargon_in(text) == (), (text, jargon_in(text))


def test_no_customer_id_reaches_the_summary(world: World) -> None:
    raw = json.dumps(_summary(world))
    keys = set(world.banded_run.scores["customer_id"].astype(str))
    assert not [key for key in keys if re.search(rf"\b{re.escape(key)}\b", raw)]
    assert "C0000" not in raw


def test_each_card_names_the_artefact_it_read(world: World) -> None:
    summary = _summary(world)
    assert summary["cards"], "the world has things to attend to"
    for card in summary["cards"]:
        assert card["read"], card["code"]
        for key in card["read"]:
            assert world.storage.exists(key) or key.startswith("model registry"), (card["code"], key)
        assert card["title"] and card["text"] and card["next_step"]


# --- the cards: each appears only when its condition holds ---------------------------------------------------
def test_no_control_appears_only_for_a_campaign_with_nobody_held_back(world: World) -> None:
    assert _card_campaigns(world, "CAMPAIGN_NO_CONTROL") == {"no_control"}
    card = _cards(_summary(world), "CAMPAIGN_NO_CONTROL")[0]
    assert any(fact["value"]["value"] == 0 for fact in card["facts"]), "it shows the zero it counted"
    assert card["read"] == [f"campaigns/{world.ids['no_control']}/campaign.json"]


def test_early_look_appears_only_for_a_result_read_before_the_planned_date(world: World) -> None:
    assert _card_campaigns(world, "CAMPAIGN_EARLY_LOOK") == {"early"}
    card = _cards(_summary(world), "CAMPAIGN_EARLY_LOOK")[0]
    assert f"campaigns/{world.ids['early']}/incrementality_report.json" in card["read"]
    assert [fact["value"]["format"] for fact in card["facts"]] == ["date"], "the day it can be read as final"


def test_an_underpowered_plan_warns_only_while_there_is_no_final_result(world: World) -> None:
    assert _card_campaigns(world, "PLAN_UNDERPOWERED") == {
        "under"
    }, "the well-powered plan, and the small plan already read, have no card"
    card = _cards(_summary(world), "PLAN_UNDERPOWERED")[0]
    assert card["read"] == [f"campaigns/{world.ids['under']}/test_plan.json"]
    assert {fact["value"]["format"] for fact in card["facts"]} == {"share"}


def test_contamination_appears_only_when_held_back_customers_were_contacted(world: World) -> None:
    assert _card_campaigns(world, "CONTROL_GROUP_CONTACTED") == {"leaky"}
    card = _cards(_summary(world), "CONTROL_GROUP_CONTACTED")[0]
    assert card["facts"][0]["value"]["value"] == pytest.approx(0.10, abs=0.02)
    assert card["read"] == [f"campaigns/{world.ids['leaky']}/contact_readout.json"]


def test_backfire_appears_for_the_planted_group_and_not_for_the_neutral_one(world: World) -> None:
    summary = _summary(world)
    cards = _cards(summary, "GROUP_BACKFIRED")
    by_campaign = {card["campaign_id"]: card for card in cards}
    assert world.ids["banded"] in by_campaign
    harmed = by_campaign[world.ids["banded"]]
    assert harmed["facts"][0]["value"]["value"] == HARMED_BAND
    assert NEUTRAL_BAND not in json.dumps(cards), "the neutral band is not flagged"
    others = {name for name in world.ids if world.ids[name] in by_campaign} - {
        "banded",
        "banded_again",
        "under_final",
        "stated",
    }
    assert others == set(), f"no other campaign backfired: {others}"
    stated = by_campaign[world.ids["stated"]]
    assert stated["title"].startswith("If the groups were random as you said"), "a statement is not proof"


def test_approving_the_suggestion_clears_its_card(world: World) -> None:
    target = world.ids["banded_again"]
    before = {card["campaign_id"] for card in _cards(_summary(world), "GROUP_BACKFIRED")}
    assert target in before
    ok(
        world.client.post(
            f"/pilot/proof/{target}/suppressions", json={"dimension": "band", "segment": HARMED_BAND}
        ),
        201,
    )
    after = {card["campaign_id"] for card in _cards(_summary(world), "GROUP_BACKFIRED")}
    assert target not in after and world.ids["banded"] in after, "only the approved group's card goes"


FEATURES = [
    "age",
    "tenure_months",
    "visits_30d",
    "monthly_spend",
    "plan",
    "region",
    "support_tickets_90d",
    "noise_a",
]


def _stable_and_drifted() -> tuple[Any, Any]:
    """Phase 1's own comparison of a scored file with a training baseline: the same customers, then a file
    whose numbers have moved a long way."""
    config = load_use_case(USE_CASE)
    base = make_winback_campaign(4_000, seed=61).frame[FEATURES].copy()
    again = make_winback_campaign(4_000, seed=62).frame[FEATURES].copy()
    moved = again.copy()
    for column in FEATURES:
        if pd.api.types.is_numeric_dtype(moved[column]):
            moved[column] = moved[column] * 6.0 + 400.0
    baseline = register.drift_baseline(
        base, config, run_id="r_20261010_10500090", model_version_id="m_summary_1"
    )
    return (
        score.compute_drift(baseline, again, config, run_id=RUN_UPLIFT),
        score.compute_drift(baseline, moved, config, run_id=RUN_UPLIFT),
    )


@contextmanager
def _written(world: World, key: str, model: Any) -> Iterator[None]:
    world.storage.write_model(key, model)
    try:
        yield
    finally:
        Path(world.storage.local_path(key)).unlink(missing_ok=True)


def test_drift_appears_only_when_the_scored_customers_have_moved(world: World) -> None:
    stable, drifted = _stable_and_drifted()
    assert stable is not None and drifted is not None
    assert stable.status.value != "drifted" and drifted.status.value == "drifted"
    key = run_key(RUN_UPLIFT, "drift.json")
    assert _card_campaigns(world, "DRIFT_DRIFTED") == set(), "no drift report, no card"
    with _written(world, key, stable):
        assert _card_campaigns(world, "DRIFT_DRIFTED") == set()
    with _written(world, key, drifted):
        assert _card_campaigns(world, "DRIFT_DRIFTED") == {"uplift"}
        card = _cards(_summary(world), "DRIFT_DRIFTED")[0]
        assert card["read"] == [key]
    assert _card_campaigns(world, "DRIFT_DRIFTED") == set()


def test_a_run_used_by_several_campaigns_is_carded_once(world: World) -> None:
    _, drifted = _stable_and_drifted()
    assert drifted is not None
    key = run_key(RUN_BANDED, "drift.json")
    on_the_run = {
        world.ids[name]
        for name in ("banded", "banded_again", "under_final", "under", "powered", "early", "waiting")
    }
    with _written(world, key, drifted):
        cards = _cards(_summary(world), "DRIFT_DRIFTED")
        assert len(cards) == 1, "seven campaigns read one drifted run: one card, not seven"
        assert cards[0]["campaign_id"] in on_the_run and cards[0]["read"] == [key]
    ranking = run_key(RUN_BANDED, RANKING_CHOICE_FILENAME)
    with _written(world, ranking, _verdict(beats=False)):
        assert len(_cards(_summary(world), "UPLIFT_NOT_BETTER_THAN_RISK")) == 1


def test_a_run_id_with_dots_reads_no_file_outside_the_runs(world: World) -> None:
    """The guard is M104's own: `..` is never a run id, so `runs/../drift.json` is never read."""
    from engine.measurement.summary import build_summary

    _, drifted = _stable_and_drifted()
    store = world.client.app.state.campaign_store  # type: ignore[attr-defined]
    odd = store.get(world.ids["uplift"]).model_copy(update={"run_ids": ("..",)})
    with (
        _written(world, "drift.json", drifted),
        _written(world, RANKING_CHOICE_FILENAME, _verdict(beats=False)),
    ):
        summary = build_summary(world.storage, [odd])
    assert [
        card.code for card in summary.cards if card.kind in {"drift", "uplift_not_better_than_risk"}
    ] == []


def _verdict(*, beats: bool) -> Any:
    """The training hold-out's verdict (`compare_with_baselines`) for a model on a planted effect, then the
    ranking record the scoring run writes from it (`decide_ranking`, `ranking_choice`)."""
    rng = np.random.default_rng(17)
    rows = 6_000
    risk = 0.1 + 0.4 * rng.random(rows)
    tau = 0.25 * rng.random(rows) if beats else 0.5 * (risk - 0.1)
    t = rng.integers(0, 2, rows)
    y = (rng.random(rows) < risk + t * tau).astype(int)
    prediction = tau if beats else 0.35 - risk
    comparison = compare_with_baselines(
        prediction,
        t,
        y,
        [BaselineInput("p_control", risk), BaselineInput("p_treated", risk + tau)],
        samples=200,
        seed=96,
    )
    decision = decide_ranking(comparison, None)
    assert decision is not None
    return ranking_choice(
        decision, run_id=RUN_UPLIFT, model_version_id="m_summary_1", contacts=80, now=utc_now()
    )


def test_not_beating_risk_appears_only_when_the_run_says_so(world: World) -> None:
    key = run_key(RUN_UPLIFT, RANKING_CHOICE_FILENAME)
    passed, failed = _verdict(beats=True), _verdict(beats=False)
    assert passed.beats_risk is True and failed.beats_risk is False
    assert _card_campaigns(world, "UPLIFT_NOT_BETTER_THAN_RISK") == set(), "no ranking record, no card"
    with _written(world, key, passed):
        assert _card_campaigns(world, "UPLIFT_NOT_BETTER_THAN_RISK") == set()
    with _written(world, key, failed):
        assert _card_campaigns(world, "UPLIFT_NOT_BETTER_THAN_RISK") == {"uplift"}
        assert _cards(_summary(world), "UPLIFT_NOT_BETTER_THAN_RISK")[0]["read"] == [key]


def _version(model_id: str, status: ModelStatus) -> ModelVersion:
    return ModelVersion(
        model_id=model_id,
        use_case_id=USE_CASE,
        version=1,
        run_id=f"r_{model_id}",
        created_at=utc_now(),
        status=status,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=0.8,
        model_display_name="LightGBM",
        schema_key=run_key(f"r_{model_id}", "schema.json"),
        run_config_key=run_key(f"r_{model_id}", "run_config.json"),
        predictor_key=run_key(f"r_{model_id}", "model"),
        engine_version="0",
        autogluon_version="0",
    )


def test_a_challenger_appears_only_while_one_waits_for_approval(world: World) -> None:
    registry = LocalModelRegistry(world.data_dir / REGISTRY_FILENAME)
    assert _cards(_summary(world), "CHALLENGER_READY") == []
    champion = registry.register(_version("m_summary_champion", ModelStatus.CHAMPION))
    assert _cards(_summary(world), "CHALLENGER_READY") == [], "a champion is not a challenger"
    challenger = registry.register(
        _version("m_summary_challenger", ModelStatus.PENDING_APPROVAL).model_copy(update={"version": 2})
    )
    cards = _cards(_summary(world), "CHALLENGER_READY")
    assert [card["use_case_id"] for card in cards] == [USE_CASE] and cards[0]["campaign_id"] is None
    assert cards[0]["read"] == [f"model registry: {challenger.model_id}"]
    registry.archive(challenger.model_id)
    assert _cards(_summary(world), "CHALLENGER_READY") == []
    registry.archive(champion.model_id)


def test_a_use_case_with_no_campaign_gets_no_challenger_card(world: World) -> None:
    registry = LocalModelRegistry(world.data_dir / REGISTRY_FILENAME)
    other = _version("m_summary_elsewhere", ModelStatus.PENDING_APPROVAL).model_copy(
        update={"use_case_id": "telco-churn"}
    )
    registry.register(other)
    try:
        assert _cards(_summary(world), "CHALLENGER_READY") == []
    finally:
        registry.archive(other.model_id)
