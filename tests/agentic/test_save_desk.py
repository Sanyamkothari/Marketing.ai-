"""The save-desk card on a real trained and scored run, through the public route (Plan K M144 acceptance).

The world is built by the product's own code (`tests/agentic/scenarios/world.py`: an uplift model trained with
`Pipeline.run_train`, a campaign file scored with `Pipeline.run_score`, per-channel consent columns). The treat
list is built by the route a person uses (`GET /runs/{id}/treat_list.csv`), and the card is asked for at
`POST /agentic/save-desk/card`. Nothing is hand-written.

Acceptance: the card's content equals the artefacts' values; the customer key is never in a URL; a Viewer is
refused; with the flag off the route is absent; no row-level data reaches a log or the audit trail. Around them:
the rate limit, the pilot's arm, the refusal codes, and a second world with a value column for the rupee lines.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.access_policy import all_policies, policy_for
from api.main import create_app
from engine.access.roles import Role
from engine.access.users import SqlUserStore
from engine.audit.events import AuditQuery, principal_hash
from engine.audit.store import SqlAuditLog
from engine.decide.reasons import extract_and_map_reasons_from_parquet
from engine.decide.treat_list import TREAT_LIST_PARQUET
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy.config import privacy_salt
from engine.privacy.consent import principal_key
from engine.settings import Settings
from engine.storage import run_key
from tests.agentic.scenarios.world import TRAIN_RUN, World, build_world
from tests.integration.production.access_support import (
    FAST_ITERATIONS,
    bearer,
    live_routes,
    make_user,
)

pytestmark = pytest.mark.integration

CARD = "/agentic/save-desk/card"
NOT_A_CUSTOMER = "ZZ-9981-not-a-customer"


def _copy_world(world: World, target: Path, *, config_edit: Any = None) -> World:
    """The world's data and configuration copied under `target`, so a test can sign users in or edit config."""
    data = target / "data"
    shutil.copytree(world.data_dir, data)
    root = target / "configs"
    shutil.copytree(world.config_root, root)
    if config_edit is not None:
        config_edit(root)
    return World(root, data, world.run_id, world.planned_campaign, world.plan_hash, world.open_campaign)


def _signed_in(world: World) -> FastAPI:
    """The world's app with sign-in on and a fast-hashing user store (as `local_app`, on the world's root)."""
    settings = Settings(auth_mode="local", env="local", data_dir=world.data_dir)
    app = create_app(config_root=world.config_root, data_dir=world.data_dir)
    app.state.settings = settings
    app.state.user_store = SqlUserStore(
        sqlite_engine(world.data_dir / PLATFORM_DB_FILENAME),
        session_ttl_seconds=settings.auth_session_ttl_seconds,
        iterations=FAST_ITERATIONS,
    )
    return app


def _build_treat_list(world: World) -> pd.DataFrame:
    """The treat list through the route a person uses, read back as the CSV they would download."""
    with world.client() as client:
        response = client.get(f"/runs/{world.run_id}/treat_list.csv")
        assert response.status_code == 200, response.text
    return pd.read_csv(io.StringIO(response.text), dtype=str, keep_default_na=False)


@pytest.fixture(scope="module")
def desk(
    agentic_world: World, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[World, pd.DataFrame]]:
    """A copy of the world with the treat list built, and that list as a person would download it."""
    world = _copy_world(agentic_world, tmp_path_factory.mktemp("desk"))
    yield world, _build_treat_list(world)


def _ask(client: TestClient, world: World, key: str, **headers: str) -> Any:
    return client.post(CARD, json={"run_id": world.run_id, "customer_key": key}, headers=headers)


def _card_events(world: World) -> list[Any]:
    """The audit events of card lookups in the world's platform database."""
    log = SqlAuditLog(sqlite_engine(world.data_dir / PLATFORM_DB_FILENAME))
    return list(log.query(AuditQuery(action="agentic.save_desk_card", limit=1_000)))


def _first(frame: pd.DataFrame, mask: pd.Series[bool], count: int = 1) -> list[str]:
    found = frame[mask]["customer_id"].tolist()[:count]
    assert len(found) == count, "the world has no such customer"
    return [str(item) for item in found]


# ---------------------------------------------------------------------------
# Acceptance: the card equals the artefacts' values
# ---------------------------------------------------------------------------
def test_the_card_equals_the_treat_list_and_the_reasons_artefact_for_a_real_run(
    desk: tuple[World, pd.DataFrame],
) -> None:
    world, treat = desk
    explanations = pq.read_table(  # type: ignore[no-untyped-call]
        world.data_dir / "runs" / world.run_id / "row_explanations.parquet"
    )
    mapped = extract_and_map_reasons_from_parquet(explanations)
    mapped["customer_id"] = explanations["primary_key"].to_pandas().astype(str).to_numpy()
    by_customer = mapped.set_index("customer_id")

    treated = _first(treat, treat["treat"] == "1", 6)
    segments = {
        name: _first(treat, (treat["segment"] == name) & (treat["control_group"] == "0"), 3)
        for name in ("sleeping_dog", "lost_cause", "sure_thing", "persuadable")
    }
    sample = [*treated, *[key for keys in segments.values() for key in keys]]
    with world.client() as client:
        for key in sample:
            response = _ask(client, world, key)
            assert response.status_code == 200, response.text
            card = response.json()
            row = treat[treat["customer_id"] == key].iloc[0]
            assert card["run_id"] == world.run_id and card["use_case_id"] == row["use_case"]
            recommendation = card["recommendation"]
            assert recommendation["make_offer"] is (row["treat"] == "1")
            if row["treat"] == "1":
                assert recommendation["offer"] == row["offer"]
                assert recommendation["channel"] == row["channel"] and recommendation["channel"]
            else:
                assert recommendation["offer"] is None and recommendation["channel"] is None
            shown = [line["text"] for line in card["reasons"]]
            in_list = [row[name] for name in ("reason_1", "reason_2", "reason_3") if row[name]]
            assert shown == in_list, "the same three phrases as the treat list"
            expected = [text for text in by_customer.loc[key, ["reason_1", "reason_2", "reason_3"]] if text]
            assert shown == expected, "and as M98's mapping of the run's own explanations (computed once)"
            channels = tuple(row["contactable_channels"].split(",")) if row["contactable_channels"] else None
            assert recommendation["contactable_channels"] == (list(channels) if channels else None)
            assert card["stay_anyway"]["segment"] == row["segment"]
            assert card["text_source"] == "rules"
            net = card["net_value"]
            assert (
                net["customer_net_value"] is None and net["gross_value"] is None
            ), "the run has no value column"
            assert "not configured" in net["note"]


def test_a_sure_thing_is_flagged_likely_to_stay_anyway_and_never_given_an_offer(
    desk: tuple[World, pd.DataFrame],
) -> None:
    world, treat = desk
    sure = _first(treat, treat["segment"] == "sure_thing", 5)
    others = _first(treat, (treat["segment"] != "sure_thing") & (treat["control_group"] == "0"), 5)
    with world.client() as client:
        for key in sure:
            card = _ask(client, world, key).json()
            assert card["stay_anyway"]["flag"] is True and card["stay_anyway"]["label"] == "Estimated"
            assert "Do not discount" in card["stay_anyway"]["reason"]
            assert card["recommendation"]["make_offer"] is False
            assert "do not discount" in card["recommendation"]["advice"].lower()
        for key in others:
            assert _ask(client, world, key).json()["stay_anyway"]["flag"] is False


def test_the_customers_the_run_kept_back_are_never_offered_anything(desk: tuple[World, pd.DataFrame]) -> None:
    """I4 on the card: the run's own control group gets "make no offer", whatever segment it is in."""
    world, treat = desk
    kept = _first(treat, treat["control_group"] == "1", 8)
    with world.client() as client:
        for key in kept:
            card = _ask(client, world, key).json()
            assert (
                card["recommendation"]["make_offer"] is False and card["kept_back"]["kind"] == "control_group"
            )
            assert "Make no offer" in card["recommendation"]["advice"]
            assert card["net_value"]["customer_net_value"] is None
    sleeping = _first(treat, (treat["segment"] == "sleeping_dog") & (treat["control_group"] == "0"), 3)
    with world.client() as client:
        for key in sleeping:
            advice = _ask(client, world, key).json()["recommendation"]["advice"]
            assert "do not offer" in advice


def test_every_line_has_a_label_and_a_source_and_nothing_is_called_causal(
    desk: tuple[World, pd.DataFrame],
) -> None:
    world, treat = desk
    with world.client() as client:
        for key in treat["customer_id"].head(30):
            card = _ask(client, world, str(key)).json()
            lines = [card["recommendation"], card["net_value"], card["stay_anyway"], *card["reasons"]]
            if card["kept_back"]:
                lines.append(card["kept_back"])
            for line in lines:
                assert line["label"] in ("Estimated", "Descriptive") and line["source"], line
            assert "has not been shown to work" in card["caution"]


def test_a_customer_not_in_the_list_is_refused_without_naming_them(desk: tuple[World, pd.DataFrame]) -> None:
    world, _ = desk
    with world.client() as client:
        response = _ask(client, world, NOT_A_CUSTOMER)
        assert (
            response.status_code == 404
            and response.json()["detail"]["code"] == "SAVE_DESK_CUSTOMER_NOT_FOUND"
        )
        assert NOT_A_CUSTOMER not in response.text
        wrong_parts = _ask(client, world, "C0000001|2026-01-31")
        assert (
            wrong_parts.status_code == 422 and wrong_parts.json()["detail"]["code"] == "SAVE_DESK_KEY_INVALID"
        )
        unknown_run = client.post(
            CARD, json={"run_id": "r_20260101_ffffffff", "customer_key": NOT_A_CUSTOMER}
        )
        assert unknown_run.status_code == 404 and unknown_run.json()["detail"]["code"] == "RUN_NOT_FOUND"
        trained = client.post(CARD, json={"run_id": TRAIN_RUN, "customer_key": NOT_A_CUSTOMER})
        assert trained.status_code == 409 and trained.json()["detail"]["code"] == "RUN_NOT_SCORED"
        extra = client.post(CARD, json={"run_id": world.run_id, "customer_key": "C0000001", "extra": 1})
        assert extra.status_code == 422, "a field the route does not know is refused"
        empty = client.post(CARD, json={"run_id": world.run_id, "customer_key": "  "})
        assert empty.status_code == 422 and empty.json()["detail"]["code"] == "SAVE_DESK_KEY_INVALID"
        long_key = "SECRETCUSTOMER-" + "9" * 200
        too_long = _ask(client, world, long_key)
        assert too_long.status_code == 422 and too_long.json()["detail"]["code"] == "SAVE_DESK_KEY_INVALID"
        assert "SECRETCUSTOMER" not in too_long.text, "a refusal never echoes the key it refused"


def test_a_run_whose_treat_list_is_not_built_says_so_and_builds_nothing(
    agentic_world: World, tmp_path: Path
) -> None:
    world = _copy_world(agentic_world, tmp_path)
    assert not world.storage.exists(run_key(world.run_id, TREAT_LIST_PARQUET))
    with world.client() as client:
        response = _ask(client, world, "C0000001")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SAVE_DESK_NO_TREAT_LIST"
    assert not world.storage.exists(run_key(world.run_id, TREAT_LIST_PARQUET)), "a lookup builds nothing"


# ---------------------------------------------------------------------------
# Acceptance: ids never in URLs; Viewer refused; flag off, route absent
# ---------------------------------------------------------------------------
def test_the_customer_key_is_only_ever_in_the_body(desk: tuple[World, pd.DataFrame]) -> None:
    world, treat = desk
    key = str(treat["customer_id"].iloc[3])
    with world.client() as client:
        ours = [route for route in live_routes(client.app) if route.path.startswith("/agentic/save-desk")]  # type: ignore[arg-type]
        assert [(route.method, route.path) for route in ours] == [("POST", CARD)]
        for route in ours:
            outside = [
                p.name for p in (*route.route.dependant.path_params, *route.route.dependant.query_params)
            ]
            assert outside == [], "no path or query parameter could carry a customer key"
            assert "{" not in route.path
        assert client.get(f"{CARD}/{key}").status_code in (404, 405)
        assert client.get(CARD, params={"customer_key": key, "run_id": world.run_id}).status_code == 405
        assert (
            client.post(f"{CARD}?customer_key={key}&run_id={world.run_id}").status_code == 422
        ), "the body is required"
        assert client.post(CARD, json={"run_id": world.run_id, "customer_key": key}).status_code == 200
    policy = policy_for("POST", CARD)
    assert policy is not None and policy.object_param is None


def test_the_route_has_its_policy_and_is_an_analysts(desk: tuple[World, pd.DataFrame]) -> None:
    policy = all_policies()[("POST", CARD)]
    # a POST is audited by the middleware on every call (see the audit tests below); `audit_reads` is for GETs
    assert policy.role is Role.ANALYST and policy.audit_reads is False
    assert policy.action == "agentic.save_desk_card" and policy.purpose


def test_a_viewer_is_refused_and_the_refusal_is_audited(
    desk: tuple[World, pd.DataFrame], tmp_path: Path
) -> None:
    world = _copy_world(desk[0], tmp_path)
    app = _signed_in(world)
    viewer = make_user(app, "vera", [Role.VIEWER])
    analyst = make_user(app, "asha", [Role.ANALYST])
    key = str(desk[1]["customer_id"].iloc[3])
    already = {event.event_id for event in _card_events(world)}
    with TestClient(app, raise_server_exceptions=False) as client:
        assert _ask(client, world, key).status_code == 401, "no sign-in, no card"
        refused = _ask(client, world, key, **bearer(app, viewer))
        assert refused.status_code == 403 and refused.json()["detail"]["code"] == "ROLE_REQUIRED"
        assert "reason_1" not in refused.text and key not in refused.text
        allowed = _ask(client, world, key, **bearer(app, analyst))
        assert allowed.status_code == 200 and allowed.headers["cache-control"] == "no-store"
    outcomes = [event.outcome for event in _card_events(world) if event.event_id not in already]
    assert sorted(outcomes) == [
        "denied",
        "denied",
        "success",
    ], "the 401, the 403 and the lookup are each audited"


def test_with_the_flag_off_the_route_is_absent(config_root: Path, tmp_path: Path) -> None:
    app = create_app(config_root=config_root, data_dir=tmp_path)
    assert not [path for path in app.openapi()["paths"] if "save-desk" in path]
    with TestClient(app) as client:
        response = client.post(CARD, json={"run_id": "r_20260101_ffffffff", "customer_key": "C1"})
        assert response.status_code == 404 and "SAVE_DESK" not in response.text
    before = tmp_path / "configs-before-plan-k"
    shutil.copytree(config_root, before, ignore=shutil.ignore_patterns("agentic"))
    again = create_app(config_root=before, data_dir=tmp_path / "again")
    assert json.dumps(app.openapi(), sort_keys=True) == json.dumps(again.openapi(), sort_keys=True)


def test_with_the_flag_on_the_card_route_is_in_the_documented_api(desk: tuple[World, pd.DataFrame]) -> None:
    with desk[0].client() as client:
        paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    assert list(paths[CARD]) == ["post"]
    assert {"401", "403", "404", "409", "422", "429"} <= set(paths[CARD]["post"]["responses"])


# ---------------------------------------------------------------------------
# Acceptance: no row-level data in logs, and the audit trail holds a hash, not a key
# ---------------------------------------------------------------------------
def test_no_row_level_data_reaches_a_log_or_the_audit_trail(
    desk: tuple[World, pd.DataFrame], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world = _copy_world(desk[0], tmp_path)
    treat = desk[1]
    row = treat[treat["treat"] == "1"].iloc[0]
    key = str(row["customer_id"])
    secrets = [key, NOT_A_CUSTOMER, row["reason_1"], row["reason_2"], row["offer"] + "|" + row["channel"]]
    app = create_app(config_root=world.config_root, data_dir=world.data_dir)
    already = {event.event_id for event in _card_events(world)}
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        assert _ask(client, world, key).status_code == 200
        assert _ask(client, world, NOT_A_CUSTOMER).status_code == 404
        assert client.post(CARD, json={"run_id": world.run_id, "customer_key": "a|b"}).status_code == 422
    logged = "\n".join(
        f"{record.getMessage()} {record.__dict__.get('args', '')} {record.exc_text or ''}"
        for record in caplog.records
    )
    for secret in secrets:
        assert secret not in logged, f"a log line holds {secret!r}"
    engine = sqlite_engine(world.data_dir / PLATFORM_DB_FILENAME)
    events = [event for event in _card_events(world) if event.event_id not in already]
    assert len(events) == 3, "every ask is one audit event, whatever it answered"
    dumped = json.dumps([event.model_dump(mode="json") for event in events], default=str)
    for secret in secrets:
        assert secret not in dumped, f"the audit trail holds {secret!r}"
    salt = privacy_salt(Settings(data_dir=world.data_dir), engine=engine)
    expected = principal_hash(principal_key(key), salt=salt)
    success = next(event for event in events if event.outcome == "success")
    assert success.details["principal_hash"] == expected and success.object_id == world.run_id
    assert success.details["run_id"] == world.run_id
    missing = next(
        event for event in events if event.details.get("reason_code") == "SAVE_DESK_CUSTOMER_NOT_FOUND"
    )
    assert missing.details["principal_hash"] == principal_hash(principal_key(NOT_A_CUSTOMER), salt=salt)


def test_the_card_changes_nothing_in_the_store(desk: tuple[World, pd.DataFrame]) -> None:
    world, treat = desk
    root = world.data_dir / "runs" / world.run_id

    def snapshot() -> dict[str, tuple[int, int]]:
        return {
            p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.iterdir()) if p.is_file()
        }

    before = snapshot()
    with world.client() as client:
        for key in treat["customer_id"].head(10):
            assert _ask(client, world, str(key)).status_code == 200
    assert snapshot() == before


# ---------------------------------------------------------------------------
# Rate limit, and the pilot's arm
# ---------------------------------------------------------------------------
def test_one_person_cannot_page_through_the_customer_list(
    desk: tuple[World, pd.DataFrame], tmp_path: Path
) -> None:
    def three_a_minute(root: Path) -> None:
        (root / "agentic" / "save_desk.yaml").write_text("rate_limit_per_minute: 3\n", encoding="utf-8")

    world = _copy_world(desk[0], tmp_path, config_edit=three_a_minute)
    keys = [str(key) for key in desk[1]["customer_id"].head(6)]
    app = _signed_in(world)
    asha, ravi = make_user(app, "asha", [Role.ANALYST]), make_user(app, "ravi", [Role.ANALYST])
    with TestClient(app, raise_server_exceptions=False) as client:
        answers = [_ask(client, world, key, **bearer(app, asha)) for key in keys]
        assert [a.status_code for a in answers] == [200, 200, 200, 429, 429, 429]
        limited = answers[3]
        assert limited.json()["detail"]["code"] == "SAVE_DESK_RATE_LIMITED"
        assert 1 <= int(limited.headers["retry-after"]) <= 60 and keys[3] not in limited.text
        assert _ask(client, world, keys[0], **bearer(app, ravi)).status_code == 200, "their own allowance"
    codes = [event.details.get("reason_code") for event in _card_events(world)]
    assert codes.count("SAVE_DESK_RATE_LIMITED") == 3


def test_an_active_pilot_serves_the_card_to_the_staff_drawn_for_it_only(
    desk: tuple[World, pd.DataFrame], tmp_path: Path
) -> None:
    holder: dict[str, Any] = {}

    def activate(root: Path) -> None:
        document = yaml.safe_load((root / "agentic" / "save_desk_pilot.yaml").read_text(encoding="utf-8"))
        document.update(status="active", card_access=holder["card"], no_card=holder["plain"])
        (root / "agentic" / "save_desk_pilot.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    holder.update(card=["placeholder-card"], plain=["placeholder-plain"])
    world = _copy_world(desk[0], tmp_path, config_edit=activate)
    app = _signed_in(world)
    drawn, plain, never = (make_user(app, name, [Role.ANALYST]) for name in ("drawn", "plain", "never"))
    holder.update(card=[drawn], plain=[plain])
    activate(world.config_root)
    key = str(desk[1]["customer_id"].iloc[2])
    with TestClient(app, raise_server_exceptions=False) as client:
        assert _ask(client, world, key, **bearer(app, drawn)).status_code == 200
        for person in (plain, never):
            refused = _ask(client, world, key, **bearer(app, person))
            assert (
                refused.status_code == 403
                and refused.json()["detail"]["code"] == "SAVE_DESK_NOT_IN_PILOT_ARM"
            )
            assert key not in refused.text


def test_while_the_pilot_is_only_a_template_the_card_is_open_to_analysts(
    desk: tuple[World, pd.DataFrame],
) -> None:
    world, treat = desk
    assert (
        yaml.safe_load((world.config_root / "agentic" / "save_desk_pilot.yaml").read_text())["status"]
        == "example"
    )
    with world.client() as client:
        assert _ask(client, world, str(treat["customer_id"].iloc[0])).status_code == 200


# ---------------------------------------------------------------------------
# The pilot's test plan is one the product registers
# ---------------------------------------------------------------------------
def test_the_examples_test_plan_registers_through_the_campaign_route(
    desk: tuple[World, pd.DataFrame],
) -> None:
    from engine.agentic.save_desk import load_pilot

    world, _ = desk
    design = load_pilot(world.config_root)
    assert design is not None
    body = json.loads(json.dumps(design.test_plan, default=str))
    # The campaign starts when the run finished, so the analysis date must follow a window from now, or this
    # test would start failing the day the template's example date falls inside the window.
    body["analysis_date"] = (date.today() + timedelta(days=design.outcome_window_days + 30)).isoformat()
    with world.client() as client:
        campaign_id = world.new_campaign(client)
        response = client.post(f"/campaigns/{campaign_id}/plan", json=body)
        assert response.status_code == 201, response.text
        plan = response.json()
    assert plan["metric"] == design.test_plan["metric"] and plan["mde_pp"] == design.test_plan["mde_pp"]
    assert plan["outcome_window_days"] == 30


# ---------------------------------------------------------------------------
# The rupee lines, on a second real run that configures a value column
# ---------------------------------------------------------------------------
def _with_training_config(world: World) -> World:
    """Score the world's campaign file again once the training run has its `run_config.json`.

    `Pipeline.run_train` does not write that file (`POST /runs` does, before it starts a job), and the scoring
    run reads the value column the model was trained with from it (`training_value_column`, DEC-1307 (d)): without
    it a list ranked by value has no measured range. This is the route's step, done as the route does it.
    """
    from engine.config import RunMode, resolve_config
    from engine.contracts import RunRecord, RunState
    from engine.jobs import NullJobRunner
    from engine.pipeline import Pipeline
    from engine.registry import LocalModelRegistry
    from engine.storage import upload_key
    from tests.agentic.scenarios import world as scenario

    storage = world.storage
    with scenario._config_dir(world.config_root):
        resolved = resolve_config(scenario.USE_CASE, scenario.UPLIFT, root=world.config_root)
        storage.write_model(run_key(TRAIN_RUN, "run_config.json"), resolved)
        trained = storage.read_model(run_key(TRAIN_RUN, "run.json"), RunRecord)
        context = scenario._context(
            storage,
            LocalModelRegistry(world.data_dir / "registry.db"),
            resolved,
            world.run_id,
            mode=RunMode.SCORE,
            upload=upload_key("u_a13200000002", "source.csv"),
            target=None,
            model_version_id=trained.model_version_id,
        )
        scored = Pipeline(
            storage, LocalModelRegistry(world.data_dir / "registry.db"), NullJobRunner()
        ).run_score(context)
    assert scored.state is RunState.DONE, scored.error
    return world


@pytest.fixture(scope="module")
def valued(
    config_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[World, pd.DataFrame]]:
    """The same world, trained and scored with `uplift.policy.value_column` set, so net value is in rupees."""
    base = tmp_path_factory.mktemp("desk-valued")
    source = base / "source-configs"
    shutil.copytree(config_root, source)
    path = source / "use_cases" / "win_back_campaign.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.setdefault("uplift", {}).setdefault("policy", {}).update(
        {"value_column": "monthly_spend", "margin_pct": 30.0, "horizon_months": 12}
    )
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    world = _with_training_config(build_world(source, base / "world"))
    yield world, _build_treat_list(world)


def test_the_rupee_lines_equal_the_treat_list_and_the_runs_stored_range(
    valued: tuple[World, pd.DataFrame],
) -> None:
    world, treat = valued
    stored = json.loads(world.storage.read_bytes(run_key(world.run_id, "policy_recommendation.json")))
    assert (
        stored["net_value_low"] is not None and stored["net_value_high"] is not None
    ), "the run measured a range"
    assert stored["net_value_low"] < stored["net_value_high"]
    rows = treat[(treat["treat"] == "1") & (treat["net_value"] != "")]
    assert len(rows) >= 3, "the run priced its treated customers"
    with world.client() as client:
        for _, row in rows.head(5).iterrows():
            card = _ask(client, world, str(row["customer_id"])).json()
            money = card["net_value"]
            assert money["unit"] == "rupees" and money["label"] == "Estimated"
            assert money["customer_net_value"] == pytest.approx(float(row["net_value"]))
            assert money["gross_value"] is None
            assert (
                money["list_low"] == stored["net_value_low"]
                and money["list_high"] == stored["net_value_high"]
            )
            assert money["list_contacts"] == stored["contacts_recommended"]
            assert "incremental" in money["note"] and "whole list" in money["note"]
        kept = _first(treat, treat["control_group"] == "1", 1)[0]
        assert _ask(client, world, kept).json()["net_value"]["customer_net_value"] is None
